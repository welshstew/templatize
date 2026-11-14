"""Recognise Kubernetes manifests in the input.

The text is parsed as multi-document YAML with PyYAML's *composer*, which keeps the
exact start/end offset of every node. Offsets are code points, like the IR's spans,
so detectors can point at a scalar without regex guessing.

A document is a Kubernetes resource if its root is a mapping with string
``apiVersion`` and ``kind`` and a ``metadata.name``. Anything that does not parse,
or has no resources, simply yields an empty profile.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

import yaml
from pydantic import BaseModel
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from templatize.ir import Span

try:  # libyaml is much faster; both report identical code-point offsets.
    _Loader: type = yaml.CSafeLoader
except AttributeError:  # pragma: no cover - pure-Python fallback
    _Loader = yaml.SafeLoader

# A line starting with "---" at column 0 is always a document marker: block scalar
# content is indented and flow content cannot start a line with "---".
_DOC_MARKER = re.compile(r"^---(?=[ \t]|\r\n|\r|\n|$)", re.MULTILINE)
# Template syntax that suggests the input already is a chart or other template.
_TEMPLATE_SYNTAX = re.compile(r"\{\{-?\s*(?:\.Values|\.Release|\.Chart|include\s|toYaml\b)")


class ResourceInfo(BaseModel):
    kind: str
    name: str
    namespace: str | None = None
    api_version: str
    span: Span
    """The resource's chunk of the input: from its ``---`` line (or the start of the
    text) to the next chunk. Chunks tile the whole input with no gaps."""


class DocumentProfile(BaseModel):
    format: Literal["kubernetes", "compose"] | None = None
    resources: list[ResourceInfo] = []
    services: list[str] = []
    """Docker Compose: the service names (converted to manifests with Kompose)."""
    helm_suggested: bool = False
    reason: str | None = None
    """Why a chart is (not) suggested, for display."""


@dataclass(frozen=True)
class Resource:
    kind: str
    name: str
    namespace: str | None
    api_version: str
    root: MappingNode
    chunk: Span


def get(node: Node | None, *path: str | int) -> Node | None:
    """Follow mapping keys / sequence indexes; ``None`` if any step is missing."""
    cur = node
    for step in path:
        if isinstance(step, int):
            if not isinstance(cur, SequenceNode) or not 0 <= step < len(cur.value):
                return None
            cur = cur.value[step]
        else:
            if not isinstance(cur, MappingNode):
                return None
            cur = next(
                (v for k, v in cur.value if isinstance(k, ScalarNode) and k.value == step),
                None,
            )
    return cur


def scalar(node: Node | None) -> str | None:
    return node.value if isinstance(node, ScalarNode) else None


def items(node: Node | None) -> list[tuple[str, Node]]:
    """String-keyed entries of a mapping node (others are skipped)."""
    if not isinstance(node, MappingNode):
        return []
    return [(k.value, v) for k, v in node.value if isinstance(k, ScalarNode)]


def seq(node: Node | None) -> list[Node]:
    return list(node.value) if isinstance(node, SequenceNode) else []


def chunk_bounds(text: str) -> list[int]:
    """Start offsets of the document chunks (always includes 0)."""
    starts = [m.start() for m in _DOC_MARKER.finditer(text)]
    if not starts or starts[0] != 0:
        starts.insert(0, 0)
    return starts


@lru_cache(maxsize=8)
def parse_resources(text: str) -> tuple[Resource, ...]:
    """Kubernetes resources in ``text``, each with its chunk; empty if not k8s YAML."""
    if "apiVersion" not in text or "kind" not in text:
        return ()
    try:
        roots = [d for d in yaml.compose_all(text, Loader=_Loader) if d is not None]
    except yaml.YAMLError:
        return ()
    starts = chunk_bounds(text)
    ends = [*starts[1:], len(text)]
    found: list[tuple[int, MappingNode, str, str, str | None, str]] = []
    for root in roots:
        if not isinstance(root, MappingNode):
            continue
        kind = scalar(get(root, "kind"))
        api = scalar(get(root, "apiVersion"))
        name = scalar(get(root, "metadata", "name"))
        if not (kind and api and name):
            continue
        found.append(
            (
                root.start_mark.index,
                root,
                kind,
                name,
                scalar(get(root, "metadata", "namespace")),
                api,
            )
        )
    if not found:
        return ()
    # Assign each resource to the chunk containing its root; chunks without a
    # resource (comments, empty documents) join the previous resource's chunk.
    out: list[Resource] = []
    owner: list[int | None] = [None] * len(starts)
    for i, (pos, *_rest) in enumerate(found):
        idx = max(j for j, s in enumerate(starts) if s <= pos)
        if owner[idx] is not None:
            return ()  # two resources in one chunk: cannot split per resource
        owner[idx] = i
    first = next(j for j, o in enumerate(owner) if o is not None)
    for j in range(first):  # leading chunks without a resource join the first one
        owner[j] = owner[first]
    for j in range(len(owner)):
        if owner[j] is None:
            owner[j] = owner[j - 1]
    for i, (_pos, root, kind, name, ns, api) in enumerate(found):
        mine = [j for j, o in enumerate(owner) if o == i]
        chunk = Span(start=starts[mine[0]], end=ends[mine[-1]])
        out.append(
            Resource(kind=kind, name=name, namespace=ns, api_version=api, root=root, chunk=chunk)
        )
    return tuple(out)


def _value_span(node: Node | None) -> Span | None:
    if not isinstance(node, ScalarNode):
        return None
    start, end = node.start_mark.index, node.end_mark.index
    if node.style in ('"', "'"):
        start, end = start + 1, end - 1
    return Span(start=start, end=end) if end > start else None


def structural_spans(text: str) -> list[Span]:
    """Never parameters in a chart: ``apiVersion``/``kind`` values and every mapping
    key (so ``app.kubernetes.io/name:`` is not mistaken for a hostname)."""
    out: list[Span] = []

    def keys(node: Node) -> None:
        if isinstance(node, MappingNode):
            for k, v in node.value:
                if (span := _value_span(k)) is not None:
                    out.append(span)
                keys(v)
        elif isinstance(node, SequenceNode):
            for v in node.value:
                keys(v)

    for r in parse_resources(text):
        for key in ("apiVersion", "kind"):
            if (span := _value_span(get(r.root, key))) is not None:
                out.append(span)
        keys(r.root)
    return out


def detect_profile(text: str) -> DocumentProfile:
    resources = parse_resources(text)
    if not resources:
        return DocumentProfile()
    infos = [
        ResourceInfo(
            kind=r.kind, name=r.name, namespace=r.namespace, api_version=r.api_version, span=r.chunk
        )
        for r in resources
    ]
    if _TEMPLATE_SYNTAX.search(text):
        return DocumentProfile(
            format="kubernetes",
            resources=infos,
            reason="the input already contains Helm template syntax",
        )
    return DocumentProfile(format="kubernetes", resources=infos, helm_suggested=True)
