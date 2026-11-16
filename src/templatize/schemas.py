"""Schema rules: what a known document type's fields mean.

Drawn from the official specs (Kubernetes API, the Compose specification, GitHub
Actions workflow syntax, cloud-init modules), as compact rules keyed by the field's
path in the YAML document rather than the full JSON Schemas (the Kubernetes OpenAPI
spec alone is megabytes). A rule says that a field is

* ``structure``: never a value (``imagePullPolicy``, ``restart: always``, the
  ``permissions:`` of a workflow and the actions its steps use, ``package_update:
  true``);
* ``name``: a value, named after the field (``runs_on``, ``timeout_minutes``);
* ``suggest``: a value worth a variable even when no detector found it (a one-item
  ``branches: [main]``, the only package in ``packages:``);
* ``env``: a ``KEY=value`` list item whose value is named after the key.

They apply when the type is confirmed, or detected with high confidence
(:data:`APPLY_CONFIDENCE`); :func:`apply` rewrites the raw candidates before they are
merged. Formats that are not YAML (HCL, systemd units) mark their structure by
pattern instead, in :func:`structure_regions`.

These rules, not the optional LLM, decide what is fixed text: asked the same
question, the models tested could not tell a format keyword from a parameter.

Paths are key names joined by ``.``; ``[]`` is a list item, ``*`` any one segment and
``**`` any number of segments: ``services.*.restart``, ``**.imagePullPolicy``.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass
from typing import Literal

import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from templatize.doctypes import DocumentType
from templatize.ir import Candidate, Source, Span
from templatize.naming import infer_type, snake_case

APPLY_CONFIDENCE = 0.9
"""A detected (not confirmed) type's rules apply from this confidence on."""
MAX_CHARS = 500_000
SCHEMA_CONFIDENCE = 0.7

Action = Literal["structure", "name", "suggest", "env"]


@dataclass(frozen=True)
class Rule:
    path: str
    action: Action
    name: str | None = None
    """For name/suggest: the variable name (default: the field's key, snake_case)."""
    only: frozenset[str] | None = None
    """For structure: only these values (``dockerfile: Dockerfile`` is the default)."""


def _structure(*paths: str, only: tuple[str, ...] | None = None) -> list[Rule]:
    return [Rule(p, "structure", only=frozenset(only) if only else None) for p in paths]


RULES: dict[str, list[Rule]] = {
    "kubernetes": [
        *_structure(
            "**.imagePullPolicy",
            "**.protocol",
            "**.restartPolicy",
            "**.pathType",
            "**.concurrencyPolicy",
            "**.dnsPolicy",
            "**.podManagementPolicy",
            "**.externalTrafficPolicy",
            "**.internalTrafficPolicy",
            "**.sessionAffinity",
            "**.accessModes[]",
            "**.strategy.type",
            "**.updateStrategy.type",
            "**.scaleTargetRef.kind",
            "**.scaleTargetRef.apiVersion",
            "**.metrics[].type",
            "**.target.type",
            "**.operator",
            "**.effect",
            # Not spec.type: a Service's ClusterIP / LoadBalancer is a chart value.
        ),
        Rule("**.storageClassName", "name", "storage_class_name"),
        Rule("**.averageUtilization", "name", "average_utilization"),
    ],
    "docker-compose": [
        *_structure(
            "services.*.restart",
            "services.*.pull_policy",
            "services.*.network_mode",
            "services.*.deploy.restart_policy.condition",
            "networks.*.driver",
            "volumes.*.driver",
        ),
        *_structure("services.*.build.dockerfile", only=("Dockerfile",)),
        Rule("services.*.environment[]", "env"),
    ],
    "github-actions": [
        *_structure(
            "permissions.*",
            "jobs.*.permissions.*",
            "jobs.*.strategy.fail-fast",
            "jobs.*.continue-on-error",
            "jobs.*.steps[].continue-on-error",
            "jobs.*.steps[].shell",
            # actions/checkout@v4: the workflow's own machinery, bumped by Dependabot.
            "jobs.*.steps[].uses",
            "concurrency.cancel-in-progress",
        ),
        # Not on.*.branches: a workflow's trigger branch is rarely a parameter.
        Rule("jobs.*.runs-on", "name", "runs_on"),
        Rule("jobs.*.timeout-minutes", "name", "timeout_minutes"),
        Rule("jobs.*.environment", "name", "environment"),
        Rule("jobs.*.steps[].with.go-version", "name", "go_version"),
        Rule("jobs.*.steps[].with.node-version", "name", "node_version"),
        Rule("jobs.*.steps[].with.python-version", "name", "python_version"),
        Rule("jobs.*.steps[].with.java-version", "name", "java_version"),
    ],
    "cloud-init": [
        *_structure(
            "package_update",
            "package_upgrade",
            "package_reboot_if_required",
            "ssh_pwauth",
            "disable_root",
            "power_state.mode",
            "users[].lock_passwd",
            "users[].shell",
            "manage_etc_hosts",
            "preserve_hostname",
        ),
        Rule("hostname", "suggest", "hostname"),
        Rule("fqdn", "suggest", "fqdn"),
        Rule("timezone", "suggest", "timezone"),
        Rule("packages[]", "suggest", "package"),
        Rule("users[].name", "suggest", "user_name"),
        Rule("ntp.servers[]", "suggest", "ntp_server"),
    ],
}


def rules_for(doc_type: DocumentType | None) -> list[Rule]:
    if doc_type is None or not (doc_type.confirmed or doc_type.confidence >= APPLY_CONFIDENCE):
        return []
    return RULES.get(doc_type.id, [])


# --- the document's fields ---------------------------------------------------------


@dataclass(frozen=True)
class Field:
    path: tuple[str, ...]
    span: Span
    value: str
    list_size: int = 0
    """Items in the nearest enclosing list (0: not in a list). A list of several items
    is offered as a loop, so ``suggest`` rules on list items only add a one-item list."""
    doc: int = 0
    """Index of the YAML document (``---``) the field is in."""


def _value_span(node: ScalarNode) -> Span | None:
    start, end = node.start_mark.index, node.end_mark.index
    if node.style in ('"', "'"):
        start, end = start + 1, end - 1
    return Span(start=start, end=end) if end > start else None


def fields(text: str) -> list[Field]:
    """Every scalar value in the YAML documents of ``text``, with its path. Quoted
    values are spanned without their quotes; block scalars (``|``) are left out."""
    out: list[Field] = []

    doc = 0

    def walk(node: Node, path: tuple[str, ...], size: int) -> None:
        if isinstance(node, MappingNode):
            for k, v in node.value:
                if isinstance(k, ScalarNode):
                    walk(v, (*path, str(k.value)), size)
        elif isinstance(node, SequenceNode):
            for v in node.value:
                walk(v, (*path, "[]"), len(node.value))
        elif isinstance(node, ScalarNode) and node.style not in ("|", ">"):
            span = _value_span(node)
            if span is not None and text[span.start : span.end] == node.value:
                out.append(Field(path, span, node.value, size, doc))

    if len(text) > MAX_CHARS:
        return []
    try:
        for i, root in enumerate(yaml.compose_all(text, Loader=yaml.SafeLoader)):
            doc = i
            if root is not None:
                walk(root, (), 0)
    except (yaml.YAMLError, ValueError, RecursionError):
        return []
    return out


def _segments(pattern: str) -> list[str]:
    return [s for s in re.split(r"\.|(?=\[\])", pattern) if s]


def _match(pattern: list[str], path: tuple[str, ...]) -> bool:
    if not pattern:
        return not path
    head, rest = pattern[0], pattern[1:]
    if head == "**":
        return any(_match(rest, path[i:]) for i in range(len(path) + 1))
    if not path:
        return False
    return (head == "*" or head == path[0]) and _match(rest, path[1:])


def _rule_for(rules: list[Rule], path: tuple[str, ...]) -> Rule | None:
    return next((r for r in rules if _match(_segments(r.path), path)), None)


# --- applying the rules ----------------------------------------------------------------


def _candidate(text: str, start: int, end: int, name: str, why: str) -> Candidate:
    value = text[start:end]
    return Candidate(
        name=name,
        name_origin="schema",
        type=infer_type(value),
        value=value,
        spans=[Span(start=start, end=end)],
        sources=[Source.KEY_VALUE],
        detectors=["schema"],
        confidence=SCHEMA_CONFIDENCE,
        reason=why,
    )


_ENV_ITEM = re.compile(r"([A-Za-z_][A-Za-z0-9_.]*)=(.+)")


# --- HCL: Terraform, Vault policies, Vault Agent ------------------------------------

HCL_TYPES = frozenset({"terraform", "vault-policy", "vault-agent"})
HCL_CONFIDENCE = 0.8
"""Labelled HCL blocks (packer, vault) are detected at 0.8; the HCL rules apply there."""
# A block and its labels: resource "aws_instance" "web" {, method "kubernetes" {.
_HCL_LABELS = re.compile(r'^[ \t]*([A-Za-z_][\w-]*)((?:[ \t]+"[^"\n]*")+)[ \t]*\{', re.MULTILINE)
_HCL_LABEL = re.compile(r'"([^"\n]*)"')
# Vault policy capabilities: permission keywords, not values.
_CAPABILITIES = re.compile(r"\bcapabilities[ \t]*=[ \t]*\[[^\]\n]*\]")
# An unquoted expression: data.vault_kv_secret_v2.db.data["username"], var.region, local.x.
_HCL_REFERENCE = re.compile(
    r"^[ \t]*[A-Za-z_][\w-]*[ \t]*=[ \t]*((?:var|local|data|module|each|count|path|self|"
    r"terraform)\.[^\n#]*?)[ \t]*$",
    re.MULTILINE,
)
_HEREDOC = re.compile(r"=[ \t]*(<<-?[A-Z_]+)[ \t]*$", re.MULTILINE)
# Labels that are the value itself (a Vault policy's path).
_VALUE_LABELS = frozenset({"path"})


# --- systemd units ------------------------------------------------------------------

SYSTEMD_CONFIDENCE = 0.9
# Directives whose value is a keyword of systemd's own (Type=simple, Restart=on-failure,
# booleans and sandboxing levels), not something a deployment chooses.
_SYSTEMD_KEYWORDS = re.compile(
    r"^[ \t]*(?:Type|Restart|KillMode|NotifyAccess|ExitType|StandardInput|StandardOutput|"
    r"StandardError|ProtectSystem|ProtectHome|PrivateTmp|PrivateDevices|NoNewPrivileges|"
    r"RemainAfterExit|DefaultDependencies)[ \t]*=([^\n]*)$",
    re.MULTILINE,
)
# Dependencies: only the targets (network-online.target, multi-user.target) are
# systemd's; a dependency on another service may well be a parameter.
_SYSTEMD_DEPS = re.compile(
    r"^[ \t]*(?:After|Before|Wants|Requires|Requisite|BindsTo|PartOf|Upholds|Conflicts|"
    r"WantedBy|RequiredBy|UpheldBy)[ \t]*=([^\n]*)$",
    re.MULTILINE,
)
_TARGET = re.compile(r"[^\s]+\.target(?=\s|$)")


def _systemd_regions(text: str) -> list[Span]:
    out: list[Span] = []
    for m in _SYSTEMD_KEYWORDS.finditer(text):
        raw = m.group(1)
        value = raw.strip()
        if value:
            start = m.start(1) + (len(raw) - len(raw.lstrip()))
            out.append(Span(start=start, end=start + len(value)))
    for m in _SYSTEMD_DEPS.finditer(text):
        base = m.start(1)
        out.extend(
            Span(start=base + t.start(), end=base + t.end()) for t in _TARGET.finditer(m.group(1))
        )
    return out


def structure_regions(text: str, doc_type: DocumentType | None) -> list[Span]:
    """Parts of a document that are never values. HCL: block labels (except a policy's
    path), capability lists, unquoted references and heredoc markers. systemd units:
    systemd's own keywords (``Type=simple``) and targets (``multi-user.target``)."""
    if (
        doc_type is not None
        and doc_type.id == "systemd"
        and (doc_type.confirmed or doc_type.confidence >= SYSTEMD_CONFIDENCE)
    ):
        return _systemd_regions(text)
    if (
        doc_type is None
        or doc_type.id not in HCL_TYPES
        or not (doc_type.confirmed or doc_type.confidence >= HCL_CONFIDENCE)
    ):
        return []
    out: list[Span] = []
    for m in _HCL_LABELS.finditer(text):
        if m.group(1) in _VALUE_LABELS:
            continue
        base = m.start(2)
        for lm in _HCL_LABEL.finditer(m.group(2)):
            out.append(Span(start=base + lm.start(1), end=base + lm.end(1)))
    for pattern in (_CAPABILITIES, _HCL_REFERENCE, _HEREDOC):
        group = 0 if pattern is _CAPABILITIES else 1
        out.extend(Span(start=m.start(group), end=m.end(group)) for m in pattern.finditer(text))
    return [s for s in out if s.end > s.start]


_POLICY_PATH = re.compile(r'^path[ \t]+"([^"\n]+)"[ \t]*\{', re.MULTILINE)


def _policy_path_names(text: str) -> dict[tuple[int, int], str]:
    """A Vault policy's paths named after their mount: secret/data/... -> secret_data_path."""
    out: dict[tuple[int, int], str] = {}
    for m in _POLICY_PATH.finditer(text):
        parts = [p for p in m.group(1).split("/") if p and p != "*"][:2]
        if parts:
            out[(m.start(1), m.end(1))] = snake_case("_".join(parts)) + "_path"
    return out


def _inside(span: Span, regions: list[Span]) -> bool:
    return any(r.start <= span.start and span.end <= r.end for r in regions)


def apply(text: str, doc_type: DocumentType | None, raw: list[Candidate]) -> list[Candidate]:
    """``raw`` with the type's schema rules applied: structure removed, values renamed,
    and the values the schema knows about suggested."""
    regions = structure_regions(text, doc_type)
    if regions:
        raw = [
            c.model_copy(update={"spans": kept}) if len(kept) != len(c.spans) else c
            for c in raw
            if (kept := [s for s in c.spans if not _inside(s, regions)])
        ]
    if regions and doc_type is not None and doc_type.id == "vault-policy":
        names_by_span = _policy_path_names(text)
        raw = [
            c.model_copy(update={"name": n, "name_origin": "schema"})
            if len(c.spans) == 1 and (n := names_by_span.get((c.spans[0].start, c.spans[0].end)))
            else c
            for c in raw
        ]
    rules = rules_for(doc_type)
    if not rules:
        return raw
    structure: set[tuple[int, int]] = set()
    names: dict[tuple[int, int], str] = {}
    added: list[Candidate] = []
    why = f"a field of a {doc_type.label}" if doc_type else "a schema field"
    for f in fields(text):
        rule = _rule_for(rules, f.path)
        if rule is None:
            continue
        key = (f.span.start, f.span.end)
        if rule.action == "structure":
            if rule.only is None or f.value in rule.only:
                structure.add(key)
        elif rule.action in ("name", "suggest"):
            names[key] = rule.name or snake_case(f.path[-1])
            if rule.action == "suggest" and f.list_size <= 1:
                added.append(_candidate(text, *key, names[key], why))
        elif rule.action == "env" and (m := _ENV_ITEM.fullmatch(f.value)):
            start = f.span.start + m.start(2)
            added.append(_candidate(text, start, f.span.end, snake_case(m.group(1)), why))
    # Parts of a structure field go too (bin/bash of shell: /bin/bash). Field values
    # never overlap, so the one starting last at or before a span is the only container.
    starts = sorted(structure)
    firsts = [st for st, _ in starts]

    def is_structure(s: Span) -> bool:
        i = bisect_right(firsts, s.start) - 1
        return i >= 0 and s.end <= starts[i][1]

    out: list[Candidate] = []
    for c in raw:
        spans = [s for s in c.spans if not is_structure(s)]
        if not spans:
            continue
        update: dict[str, object] = {}
        if len(spans) != len(c.spans):
            update["spans"] = spans
        named = {names.get((s.start, s.end)) for s in spans}
        if len(named) == 1 and (name := named.pop()) is not None and c.name_origin != "manual":
            update.update(name=name, name_origin="schema")
        out.append(c.model_copy(update=update) if update else c)
    return out + added
