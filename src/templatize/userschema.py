"""JSON Schemas: the one a document references, and one the user supplies.

A reference (``"$schema": "https://..."`` in JSON, ``$schema:`` or the YAML language
server's ``# yaml-language-server: $schema=...`` in YAML) is recognised and reported,
never downloaded: the tool runs offline, a fetch would tell a third party what was
pasted, and a server fetching user-supplied URLs could be pointed at internal
addresses. To use a schema, the user supplies its text.

A supplied schema is walked along the document's fields (``properties``,
``patternProperties``, ``additionalProperties``, ``items``/``prefixItems``, local
``$ref``, ``allOf``/``anyOf``/``oneOf``) and what it says about a field feeds the
suggestions:

* ``const``: the field is structure, never a value;
* ``format`` (uri, email, hostname, ipv4/ipv6, date, date-time) or an integer whose
  maximum is 65535: the value's type, and a suggestion even if no detector found it;
* a value equal to ``default``: a weaker candidate (nobody changed it);
* a short ``title``: the variable's name, unless a key already named it;
* ``enum``: kept as a value (log levels are enums too), with the choices in the reason.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any, Literal

import yaml
from pydantic import BaseModel

from templatize import catalog
from templatize.doctypes import DocumentType
from templatize.ir import Candidate, Source, Span, VarType
from templatize.naming import snake_case
from templatize.schemas import Field, fields

MAX_SCHEMA_CHARS = 2_000_000
MAX_REF_DEPTH = 32
MAX_BRANCHES = 64
SUGGEST_CONFIDENCE = 0.7
DEFAULT_FACTOR = 0.6


class SchemaError(ValueError):
    pass


class SchemaReference(BaseModel):
    url: str
    source: Literal["$schema", "modeline"]
    span: Span
    """Where the URL is in the document: structure, never a value."""


class SchemaInfo(BaseModel):
    """What the analysis knows about the document's JSON Schema."""

    reference: SchemaReference | None = None
    """A schema the document points at (reported, not downloaded)."""
    provided: bool = False
    """A schema was applied: the caller's, or a bundled one (see ``source``)."""
    source: Literal["supplied", "catalog"] | None = None
    catalog: list[str] = []
    """Ids of the bundled schemas applied (one per Kubernetes resource kind)."""
    title: str | None = None
    matched_fields: int = 0
    """Document fields the applied schema(s) describe."""
    notes: list[str] = []


# --- references ------------------------------------------------------------------

_MODELINE = re.compile(r"^#\s*yaml-language-server:\s*\$schema=(\S+)", re.MULTILINE)
_JSON_SCHEMA_KEY = re.compile(r'\A\s*\{[^{}]{0,2000}?"\$schema"\s*:\s*"([^"\\]+)"')
_YAML_SCHEMA_KEY = re.compile(r"^\$schema:\s*[\"']?([^\s\"'#]+)", re.MULTILINE)


def find_reference(text: str) -> SchemaReference | None:
    head = text[:20_000]
    for pattern, source in (
        (_MODELINE, "modeline"),
        (_JSON_SCHEMA_KEY, "$schema"),
        (_YAML_SCHEMA_KEY, "$schema"),
    ):
        m = pattern.match(head) if pattern is _JSON_SCHEMA_KEY else pattern.search(head)
        if m:
            span = Span(start=m.start(1), end=m.end(1))
            return SchemaReference(url=m.group(1), source=source, span=span)
    return None


def without_reference(raw: list[Candidate], ref: SchemaReference | None) -> list[Candidate]:
    """The candidates without the schema URL (and anything inside it)."""
    if ref is None:
        return raw
    out = []
    for c in raw:
        spans = [s for s in c.spans if not (ref.span.start <= s.start and s.end <= ref.span.end)]
        if spans:
            out.append(c if len(spans) == len(c.spans) else c.model_copy(update={"spans": spans}))
    return out


# --- loading and resolving -------------------------------------------------------


def load_schema(text: str) -> dict[str, Any]:
    """A JSON Schema from JSON or YAML text."""
    if len(text) > MAX_SCHEMA_CHARS:
        raise SchemaError(f"the schema is larger than {MAX_SCHEMA_CHARS} characters")
    try:
        data = json.loads(text)
    except ValueError:
        try:
            data = yaml.safe_load(text)
        except (yaml.YAMLError, ValueError, RecursionError) as exc:
            raise SchemaError(f"the schema is neither JSON nor YAML: {exc}") from None
    if not isinstance(data, dict):
        raise SchemaError("a JSON Schema must be an object")
    return data


class _Schema:
    def __init__(self, root: dict[str, Any]) -> None:
        self.root = root
        self.external_refs: set[str] = set()

    def resolve(self, node: Any, depth: int = 0) -> dict[str, Any] | None:
        """``node`` with a local ``$ref`` followed (``#/$defs/x``, ``#/definitions/x``)."""
        if not isinstance(node, dict):
            return None
        while isinstance(node.get("$ref"), str) and depth < MAX_REF_DEPTH:
            ref = node["$ref"]
            if not ref.startswith("#"):
                self.external_refs.add(ref)
                return None
            target: Any = self.root
            for part in ref[1:].split("/"):
                if not part:
                    continue
                part = part.replace("~1", "/").replace("~0", "~")
                if isinstance(target, dict) and part in target:
                    target = target[part]
                elif isinstance(target, list) and part.isdigit() and int(part) < len(target):
                    target = target[int(part)]
                else:
                    return None
            if not isinstance(target, dict):
                return None
            node, depth = target, depth + 1
        return node if isinstance(node, dict) else None

    def branches(self, node: Any) -> Iterator[dict[str, Any]]:
        """``node`` and the subschemas its allOf/anyOf/oneOf combine (bounded)."""
        seen = 0
        stack = [node]
        while stack and seen < MAX_BRANCHES:
            s = self.resolve(stack.pop())
            if s is None:
                continue
            seen += 1
            yield s
            for key in ("allOf", "anyOf", "oneOf"):
                if isinstance(s.get(key), list):
                    stack.extend(reversed(s[key]))

    def child(self, nodes: list[dict[str, Any]], segment: str) -> list[dict[str, Any]]:
        out: list[Any] = []
        for node in nodes:
            for s in self.branches(node):
                if segment == "[]":
                    items = s.get("items")
                    if isinstance(items, dict):
                        out.append(items)
                    elif isinstance(items, list):
                        out.extend(items)
                    if isinstance(s.get("prefixItems"), list):
                        out.extend(s["prefixItems"])
                    continue
                props = s.get("properties")
                if isinstance(props, dict) and segment in props:
                    out.append(props[segment])
                    continue
                patterns = s.get("patternProperties")
                matched = False
                if isinstance(patterns, dict):
                    for pattern, sub in patterns.items():
                        try:
                            if re.search(pattern, segment):
                                out.append(sub)
                                matched = True
                        except re.error:
                            continue
                if not matched and isinstance(s.get("additionalProperties"), dict):
                    out.append(s["additionalProperties"])
        return [r for n in out if (r := self.resolve(n)) is not None]

    def at(self, path: tuple[str, ...]) -> list[dict[str, Any]]:
        nodes = [self.root]
        for segment in path:
            nodes = self.child(nodes, segment)
            if not nodes:
                return []
        return [b for n in nodes for b in self.branches(n)]

    def named(self, path: tuple[str, ...]) -> bool:
        """Is the field's last key a named property (not an open map's value or a list
        item)? Only those have descriptions worth showing."""
        if not path or path[-1] == "[]":
            return False
        parents = self.at(path[:-1])
        return any(
            isinstance(b.get("properties"), dict) and path[-1] in b["properties"]
            for p in parents
            for b in self.branches(p)
        )


# --- what a field's schema says --------------------------------------------------

_FORMAT_TYPES = {
    "uri": VarType.URL,
    "url": VarType.URL,
    "iri": VarType.URL,
    "email": VarType.EMAIL,
    "idn-email": VarType.EMAIL,
    "hostname": VarType.HOSTNAME,
    "idn-hostname": VarType.HOSTNAME,
    "ipv4": VarType.IPV4,
    "ipv6": VarType.IPV6,
    "date": VarType.DATE,
    "date-time": VarType.DATETIME,
}


# Kubernetes schemas list a string field's values in prose, not as an enum:
# "One of Always, Never, IfNotPresent", "Valid options are ExternalName, ClusterIP,
# NodePort, and LoadBalancer", "Must be UDP, TCP, or SCTP". Only lists of two or more
# capitalised words count ("Exactly one of A and B" names fields, so it does not).
_CHOICE = r"""["']?[A-Z][A-Za-z]+["']?"""
_CHOICE_SEP = r"(?:,\s*(?:and\s+|or\s+)?|\s+or\s+|\s+and\s+)"
_PROSE_CHOICES = re.compile(
    r"(?<!xactly )(?:[Oo]ne of|Valid options are|Valid values are|[Mm]ust be|"
    r"[Mm]ay be one of|[Cc]an be one of|[Ss]upported values(?: are)?)[:\s]+"
    rf"({_CHOICE}(?:{_CHOICE_SEP}{_CHOICE})+)"
)


def _choices(schemas: list[dict[str, Any]]) -> list[str] | None:
    """The values a string field allows: its ``enum`` (strings only; ``null`` is
    dropped), else a list in its description."""
    for s in schemas:
        enum = s.get("enum")
        if isinstance(enum, list):
            vals = [v for v in enum if v is not None]
            if vals and all(isinstance(v, str) for v in vals):
                return list(dict.fromkeys(vals))
            return None
    for s in schemas:
        desc, typ = s.get("description"), s.get("type")
        if isinstance(desc, str) and (
            typ == "string" or (isinstance(typ, list) and "string" in typ)
        ):
            m = _PROSE_CHOICES.search(desc)
            if m:
                return list(dict.fromkeys(re.findall(r"[A-Z][A-Za-z]+", m.group(1))))
    return None


_JSON_TYPES = ("integer", "number", "boolean", "string")


def _accepts(schemas: list[dict[str, Any]]) -> list[str] | None:
    """The scalar JSON types the field allows (``type``, or the members of a
    ``oneOf``/``anyOf``), ``null`` left out; ``None`` when not stated or not scalar."""
    found: set[str] = set()
    stated = False

    def take(t: Any) -> bool:
        types = [t] if isinstance(t, str) else t if isinstance(t, list) else None
        if types is None:
            return False
        found.update(x for x in types if x != "null")
        return True

    for s in schemas:
        if take(s.get("type")):
            stated = True
        for key in ("oneOf", "anyOf"):
            members = s.get(key)
            if isinstance(members, list):
                for m in members:
                    if isinstance(m, dict) and take(m.get("type")):
                        stated = True
    if not stated or not found or not found <= set(_JSON_TYPES):
        return None
    return [t for t in _JSON_TYPES if t in found]


class _Facts:
    def __init__(self, schemas: list[dict[str, Any]], value: str) -> None:
        self.const = any("const" in s for s in schemas)
        self.enum = next((s["enum"] for s in schemas if isinstance(s.get("enum"), list)), None)
        self.choices = _choices(schemas)
        self.accepts = _accepts(schemas)
        fmt = next((s["format"] for s in schemas if isinstance(s.get("format"), str)), None)
        self.vtype: VarType | None = _FORMAT_TYPES.get(fmt) if fmt else None
        if self.vtype is None and any(
            s.get("type") == "integer" and s.get("maximum") == 65535 for s in schemas
        ):
            self.vtype = VarType.PORT
        self.default = any("default" in s and str(s["default"]) == value for s in schemas)
        title = next((s["title"] for s in schemas if isinstance(s.get("title"), str)), None)
        self.name = snake_case(title) if title and len(title.split()) <= 3 else None
        desc = next(
            (s["description"] for s in schemas if isinstance(s.get("description"), str)), None
        )
        self.description = _first_sentence(desc) if desc else None


def _first_sentence(text: str, limit: int = 140) -> str:
    """The description's first sentence, short enough for a tooltip."""
    s = " ".join(text.split())
    m = re.match(r"(.+?[.!?])(?:\s|$)", s)
    s = m.group(1) if m else s
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def _candidate(text: str, span: Span, name: str, vtype: VarType, why: str) -> Candidate:
    return Candidate(
        name=name,
        name_origin="schema",
        type=vtype,
        value=text[span.start : span.end],
        spans=[span],
        sources=[Source.KEY_VALUE],
        detectors=["user_schema"],
        confidence=SUGGEST_CONFIDENCE,
        reason=why,
    )


def apply(
    text: str, schema: dict[str, Any], raw: list[Candidate], *, doc: int | None = None
) -> tuple[list[Candidate], SchemaInfo]:
    """``raw`` with a schema applied, and what was matched. ``doc`` limits it to one
    YAML document (``---``) of a multi-document file."""
    s = _Schema(schema)
    title = schema.get("title") if isinstance(schema.get("title"), str) else None
    structure: set[tuple[int, int]] = set()
    facts: dict[tuple[int, int], tuple[_Facts, Field]] = {}
    for f in fields(text):
        if doc is not None and f.doc != doc:
            continue
        subs = s.at(f.path)
        if subs:
            fact = _Facts(subs, f.value)
            if fact.description and not s.named(f.path):
                fact.description = None
            facts[(f.span.start, f.span.end)] = (fact, f)
    for key, (fact, _) in facts.items():
        if fact.const:
            structure.add(key)
    covered: list[Span] = []
    described: set[tuple[int, int]] = set()
    # A name from a key (server.port -> server_port) is kept over the schema's title.
    keyed = {(sp.start, sp.end) for c in raw if c.name_origin == "key" for sp in c.spans}
    out: list[Candidate] = []
    for c in raw:
        spans = [sp for sp in c.spans if (sp.start, sp.end) not in structure]
        if not spans:
            continue
        update: dict[str, Any] = {"spans": spans} if len(spans) != len(c.spans) else {}
        here = [facts[k] for sp in spans if (k := (sp.start, sp.end)) in facts]
        if here and len(here) == len(spans):
            fact = here[0][0]
            if fact.vtype is not None:
                update["type"] = fact.vtype
            keyed_here = any((sp.start, sp.end) in keyed for sp in spans)
            if fact.name and not keyed_here and c.name_origin in ("type", "generic", "context"):
                update.update(name=fact.name, name_origin="schema")
            if fact.default:
                update["confidence"] = round(c.confidence * DEFAULT_FACTOR, 3)
            # One candidate per span carries the description (the merge joins reasons).
            first = (spans[0].start, spans[0].end) not in described
            notes = [f"schema: {fact.description}"] if fact.description and first else []
            described.update((sp.start, sp.end) for sp in spans)
            if fact.default:
                notes.append("the schema's default")
            if fact.enum:
                notes.append("one of: " + ", ".join(map(str, fact.enum[:8])))
            if fact.choices and c.value in fact.choices:
                update["choices"] = fact.choices
            if fact.accepts:
                update["accepts"] = fact.accepts
            if notes:
                update["reason"] = "; ".join([*(filter(None, [c.reason])), *notes])
        out.append(c.model_copy(update=update) if update else c)
        covered.extend(spans)
    for (start, end), (fact, f) in facts.items():
        span = Span(start=start, end=end)
        if fact.const or fact.vtype is None or any(span.overlaps(o) for o in covered):
            continue
        field_key = next((p for p in reversed(f.path) if p != "[]"), "item")
        name = fact.name or snake_case(field_key)
        why = f"{fact.vtype.value} by the schema" + (
            f"; schema: {fact.description}" if fact.description else ""
        )
        out.append(_candidate(text, span, name, fact.vtype, why))
    notes = []
    if s.external_refs:
        shown = ", ".join(sorted(s.external_refs)[:3])
        notes.append(f"external $ref not followed (offline): {shown}")
    info = SchemaInfo(
        provided=True, source="supplied", title=title, matched_fields=len(facts), notes=notes
    )
    return out, info


__all__ = ["SchemaError", "SchemaInfo", "SchemaReference", "apply", "find_reference", "load_schema"]


# --- the bundled catalogue ---------------------------------------------------------

CATALOG_CONFIDENCE = 0.9
"""A detected (not confirmed) type's bundled schema applies from this confidence on."""


def apply_catalog(
    text: str,
    doc_type: DocumentType | None,
    reference: SchemaReference | None,
    raw: list[Candidate],
) -> tuple[list[Candidate], SchemaInfo | None]:
    """Apply the bundled schema the document references, or the one of its (confirmed
    or confidently detected) type; Kubernetes manifests get one schema per resource
    kind. ``None`` info when no bundled schema applies."""
    plan: list[tuple[catalog.Entry, int | None]] = []
    referenced = catalog.for_url(reference.url) if reference else None
    if referenced is not None:
        plan.append((referenced, None))
    elif doc_type is not None and (doc_type.confirmed or doc_type.confidence >= CATALOG_CONFIDENCE):
        if doc_type.id == "kubernetes":
            heads: dict[int, dict[str, str]] = {}
            for f in fields(text):
                if f.path in (("apiVersion",), ("kind",)):
                    heads.setdefault(f.doc, {})[f.path[0]] = f.value
            for i, head in sorted(heads.items()):
                entry = catalog.for_k8s(head.get("apiVersion", ""), head.get("kind", ""))
                if entry is not None:
                    plan.append((entry, i))
        elif (entry := catalog.for_type(doc_type.id)) is not None:
            plan.append((entry, None))
    if not plan:
        return raw, None
    matched = 0
    notes: list[str] = []
    used: list[str] = []
    for entry, doc in plan:
        raw, info = apply(text, catalog.load(entry.id), raw, doc=doc)
        matched += info.matched_fields
        notes += [n for n in info.notes if n not in notes]
        if entry.id not in used:
            used.append(entry.id)
    titles = list(dict.fromkeys(e.title for e, _ in plan))
    title = (
        titles[0]
        if len(titles) == 1
        else "Kubernetes: "
        + ", ".join(t.removeprefix("Kubernetes ").split(" (")[0] for t in titles)
    )
    return raw, SchemaInfo(
        reference=reference,
        provided=True,
        source="catalog",
        catalog=used,
        title=title,
        matched_fields=matched,
        notes=notes,
    )
