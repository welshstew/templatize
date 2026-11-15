"""Check a rendered document against its JSON Schema.

A template's output is checked against the schema that describes it: one the user
supplied, else the bundled schema its ``$schema`` URL names, else the bundled schema
of its (confirmed or confidently detected) document type, one per Kubernetes resource
kind. The preview runs this on every edit, so a value edited to something the schema
does not allow (``replicas: three``, a missing required field) shows up before the
file is used.

YAML is read with YAML 1.2 rules: only ``true``/``false`` are booleans (``on:`` in a
GitHub workflow stays the key "on") and timestamps stay strings, as the schemas expect.
"""

from __future__ import annotations

import re
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError as JsonSchemaError
from jsonschema.exceptions import best_match
from jsonschema.validators import validator_for
from pydantic import BaseModel

from templatize import catalog
from templatize.doctypes import detect_type
from templatize.userschema import CATALOG_CONFIDENCE, find_reference, load_schema

MAX_PROBLEMS = 20
MAX_CHARS = 2_000_000


class SchemaProblem(BaseModel):
    document: int
    """Index of the YAML document (``---``) the problem is in."""
    path: str
    """Where in the document: ``spec.replicas``, ``jobs.build.steps.0``."""
    message: str


class SchemaCheck(BaseModel):
    ok: bool
    schemas: list[str]
    """Titles of the schemas checked against."""
    problems: list[SchemaProblem] = []
    note: str | None = None
    """Why nothing could be checked, or that problems were cut off."""


class _Yaml12Loader(yaml.SafeLoader):
    pass


# YAML 1.2 core schema: drop the 1.1 boolean words (on/off/yes/no) and timestamps.
_Yaml12Loader.yaml_implicit_resolvers = {
    ch: [
        (tag, rx)
        for tag, rx in resolvers
        if tag not in ("tag:yaml.org,2002:bool", "tag:yaml.org,2002:timestamp")
    ]
    for ch, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_Yaml12Loader.add_implicit_resolver(  # type: ignore[no-untyped-call]
    "tag:yaml.org,2002:bool", re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF")
)


def _documents(text: str) -> list[Any]:
    return list(yaml.load_all(text, Loader=_Yaml12Loader))


def _plan(
    text: str, docs: list[Any], schema: str | None, document_type: str | None
) -> list[tuple[str, dict[str, Any], list[int]]]:
    """(title, schema, document indexes) to check."""
    all_docs = list(range(len(docs)))
    if schema:
        s = load_schema(schema)
        title = s.get("title") if isinstance(s.get("title"), str) else "supplied schema"
        return [(str(title), s, all_docs)]
    ref = find_reference(text)
    entry = catalog.for_url(ref.url) if ref else None
    if entry is not None:
        return [(entry.title, catalog.load(entry.id), all_docs)]
    doc_type = detect_type(text, confirmed=document_type)
    if not (doc_type.confirmed or doc_type.confidence >= CATALOG_CONFIDENCE):
        return []
    if doc_type.id == "kubernetes":
        out = []
        for i, d in enumerate(docs):
            if isinstance(d, dict):
                e = catalog.for_k8s(str(d.get("apiVersion", "")), str(d.get("kind", "")))
                if e is not None:
                    out.append((e.title, catalog.load(e.id), [i]))
        return out
    entry = catalog.for_type(doc_type.id)
    return [(entry.title, catalog.load(entry.id), all_docs)] if entry else []


def _message(e: Any) -> str:
    """The error's message; a type list loses ``null`` (Kubernetes schemas allow it on
    every field, which reads as noise: "is not of type 'integer', 'null'")."""
    if e.validator == "type" and isinstance(e.validator_value, list):
        types = [t for t in e.validator_value if t != "null"] or e.validator_value
        return f"{e.instance!r} is not of type " + ", ".join(repr(t) for t in types)
    return str(e.message)


def check_output(
    text: str, *, schema: str | None = None, document_type: str | None = None
) -> SchemaCheck | None:
    """Problems of ``text`` against its schema(s); ``None`` when no schema applies."""
    if len(text) > MAX_CHARS:
        return None
    try:
        docs = _documents(text)
    except (yaml.YAMLError, ValueError, RecursionError) as exc:
        if (
            schema
            or find_reference(text)
            or detect_type(text, confirmed=document_type).id
            in (
                "kubernetes",
                "docker-compose",
                "github-actions",
                "gitlab-ci",
                "cloud-init",
                "helm-chart",
                "prometheus",
                "prometheus-rules",
            )
        ):
            msg = str(exc).splitlines()[0][:200]
            return SchemaCheck(ok=False, schemas=[], note=f"not valid YAML/JSON: {msg}")
        return None
    plan = _plan(text, docs, schema, document_type)
    if not plan:
        return None
    problems: list[SchemaProblem] = []
    titles: list[str] = []
    note = None
    for title, s, indexes in plan:
        if title not in titles:
            titles.append(title)
        try:
            cls = validator_for(s, default=Draft202012Validator)
            validator = cls(s)
        except JsonSchemaError as exc:
            note = f"{title}: the schema itself is invalid ({exc.message[:120]})"
            continue
        for i in indexes:
            if docs[i] is None:
                continue
            try:
                errors = sorted(
                    validator.iter_errors(docs[i]), key=lambda e: list(map(str, e.absolute_path))
                )
            except Exception as exc:  # a pathological schema (unresolvable $ref, ...)
                note = f"{title}: could not be checked ({type(exc).__name__})"
                break
            for e in errors:
                # oneOf/anyOf failures ("not valid under any of the given schemas") name
                # the closest alternative's actual problem instead.
                e = best_match(e.context) if e.context else e
                path = ".".join(str(p) for p in e.absolute_path) or "(document)"
                problems.append(SchemaProblem(document=i, path=path, message=_message(e)[:300]))
    if len(problems) > MAX_PROBLEMS:
        note = f"{len(problems)} problems; the first {MAX_PROBLEMS} are shown"
        problems = problems[:MAX_PROBLEMS]
    return SchemaCheck(ok=not problems, schemas=titles, problems=problems, note=note)


class DocumentCheck(BaseModel):
    """A document checked on its own (the MCP ``check_document`` tool, ``POST /check``)."""

    checked: bool
    """Whether a schema applied. When false, nothing was checked (see ``note``)."""
    ok: bool | None = None
    """Valid against every schema applied; ``None`` when nothing was checked."""
    schemas: list[str] = []
    problems: list[SchemaProblem] = []
    note: str | None = None


def check_document(
    text: str, *, schema: str | None = None, document_type: str | None = None
) -> DocumentCheck:
    """:func:`check_output`, saying so when no schema applies."""
    result = check_output(text, schema=schema, document_type=document_type)
    if result is None:
        return DocumentCheck(
            checked=False,
            note=(
                "no schema applies: pass the schema, or the document type if it is one "
                "with a bundled schema (Kubernetes, Compose, GitHub Actions, GitLab CI, "
                "cloud-init, Helm Chart.yaml, Prometheus, Dependabot, package.json)"
            ),
        )
    return DocumentCheck(checked=True, **result.model_dump())
