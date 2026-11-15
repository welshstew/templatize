"""Typed values for generated charts and roles: Helm ``values.schema.json`` and
Ansible ``meta/argument_specs.yml``.

Both tools check values against these when the chart or role is used (``helm
install``/``template``, the start of a role run), so ``replicas: three`` is rejected
before anything is rendered. The types come from two places:

* what the detectors found (an ``int``, a ``port``, a ``bool``);
* what the shipped values actually are, in ``values.yaml`` and every example file.

A type is only ever widened by the second, never narrowed: the defaults and examples
always pass, so a generated chart or role never rejects its own values.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from templatize.ir import Loop, TemplateIR, Variable, VarType

ITEM = "[]"
"""Path segment for "each item of a list"."""

Path = tuple[str, ...]


@dataclass(frozen=True)
class Hint:
    type: VarType | None = None
    description: str | None = None
    choices: tuple[str, ...] | None = None
    """What the schema allows (``enum``); kept only if every shipped value is one."""
    accepts: tuple[str, ...] | None = None
    """The JSON types the field's schema allows; with "string" among them the value
    is not held to the detected type (a targetPort can be 8080 or ``http``)."""

    @property
    def open_string(self) -> bool:
        return bool(self.accepts) and "string" in (self.accepts or ())


# Reason parts that say how a value was found, not what it is.
_MECHANICAL = re.compile(
    r"^(?:value of (?:key|column|--)|looks like|appears \d+ times|also part of|"
    r"lines starting|items of the|rows of a|entries of|a field of|repeated|"
    r"differs between examples)",
)


def describe(reason: str | None) -> str | None:
    """A short description from a suggestion's reason: what the value is ("Deployment
    name") and what its schema says ("Number of desired pods."), not how it was found."""
    if not reason:
        return None
    context: str | None = None
    schema: str | None = None
    for part in (p.strip() for p in reason.split("; ")):
        if part.startswith("schema: "):
            text = part[len("schema: ") :].strip()
            if schema is None and text and not text.endswith("…"):
                schema = text
        elif context is None and part and not _MECHANICAL.match(part):
            context = part
    if context and schema:
        return f"{context[0].upper()}{context[1:]}. {schema}"
    if context:
        return context[0].upper() + context[1:]
    return schema


def variable_hints(
    variables: Iterable[Variable],
    loops: Iterable[Loop],
    ir: TemplateIR,
    path_of: Mapping[str, Path],
) -> dict[Path, Hint]:
    """Hints keyed by values path. ``path_of``: each variable's and top-level loop's
    path (Helm: its values path; Ansible: its name)."""
    hints: dict[Path, Hint] = {}
    for v in variables:
        if v.name in path_of:
            hints[path_of[v.name]] = Hint(
                v.type,
                describe(v.description),
                tuple(v.choices) if v.choices else None,
                tuple(v.accepts) if v.accepts else None,
            )

    def loop(lp: Loop, path: Path) -> None:
        hints[path] = Hint(None, describe(lp.description))
        kids = ir.children(lp)
        if lp.scalar and not kids:
            hints[(*path, ITEM)] = Hint(lp.fields[0].type)
            return
        for f in lp.fields:
            hints[(*path, ITEM, f.name)] = Hint(f.type)
        for c in kids:
            loop(c, (*path, ITEM, c.name))

    for lp in loops:
        if lp.parent is None and lp.name in path_of:
            loop(lp, path_of[lp.name])
    return hints


# ---------------------------------------------------------------- Helm (JSON Schema)

_JSON_TYPE = {
    VarType.INT: "integer",
    VarType.PORT: "integer",
    VarType.FLOAT: "number",
    VarType.BOOL: "boolean",
}


# What a quoted number or boolean must look like (applies to strings only).
PATTERNS = {
    "integer": re.compile(r"^[+-]?[0-9]+$"),
    "number": re.compile(r"^[+-]?(?:[0-9]+[.]?[0-9]*|[.][0-9]+)(?:[eE][+-]?[0-9]+)?$"),
    "boolean": re.compile(r"^(?:true|false|True|False|TRUE|FALSE)$"),
}


def _json_type(v: Any) -> str | None:
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, int):
        return "integer"
    if isinstance(v, float):
        return "number"
    if isinstance(v, str):
        return "string"
    return None


def _schema(path: Path, observed: list[Any], hints: Mapping[Path, Hint]) -> dict[str, Any]:
    hint = hints.get(path, Hint())
    out: dict[str, Any] = {}
    vals = [v for v in observed if v is not None]
    if vals and all(isinstance(v, Mapping) for v in vals):
        out["type"] = "object"
        keys: list[str] = []
        for v in vals:
            keys += [k for k in v if k not in keys]
        if keys:
            out["properties"] = {
                k: _schema((*path, k), [v[k] for v in vals if k in v], hints) for k in keys
            }
    elif vals and all(isinstance(v, list) for v in vals):
        out["type"] = "array"
        items = [i for v in vals for i in v]
        if items or (*path, ITEM) in hints:
            item_schema = _schema((*path, ITEM), items, hints)
            if item_schema:
                out["items"] = item_schema
    elif all(_json_type(v) is not None for v in vals) and (vals or hint.type is not None):
        types = {t for v in vals if (t := _json_type(v))}
        want = _JSON_TYPE.get(hint.type) if hint.type is not None else None
        if hint.open_string:
            types |= set(hint.accepts or ())
        elif want is not None:
            # The same value quoted ("5672") renders the same: accepted as a string
            # when it looks like one, so only "three" is turned away.
            types |= {want, "string"}
            if all(PATTERNS[want].match(v) for v in vals if isinstance(v, str)):
                out["pattern"] = PATTERNS[want].pattern
        elif hint.type is not None:
            types.add("string")
        if "number" in types:
            types.discard("integer")  # a JSON Schema integer is a number
        ordered = [t for t in ("integer", "number", "boolean", "string") if t in types]
        # A string nothing detected (a helm create default such as a probe's
        # ``port: http``) may well take a number too: left open.
        if hint.type is not None or ordered != ["string"]:
            out["type"] = ordered[0] if len(ordered) == 1 else ordered
        ints = [v for v in vals if _json_type(v) == "integer"]
        if hint.type is VarType.PORT and all(1 <= i <= 65535 for i in ints):
            out["minimum"], out["maximum"] = 1, 65535  # applies to numbers only
        if _fits_choices(hint, vals):
            out["enum"] = list(hint.choices or ())
    if hint.description:
        out["description"] = hint.description
    return out


def values_schema(
    trees: Sequence[Mapping[str, Any]], hints: Mapping[Path, Hint], title: str
) -> dict[str, Any]:
    """A draft-07 JSON Schema (what Helm reads from ``values.schema.json``) that every
    one of ``trees`` (the values as Helm reads them) satisfies."""
    body = _schema((), list(trees), hints)
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": title,
        **body,
    }


# ---------------------------------------------------------- Ansible (argument_specs)

_ANSIBLE_TRUE = {"y", "yes", "on", "1", "true", "t", "1.0"}
_ANSIBLE_FALSE = {"n", "no", "off", "0", "false", "f", "0.0"}


def _converts(v: Any, kind: str) -> bool:
    """Would Ansible's argument spec accept ``v`` as ``kind``?"""
    if isinstance(v, bool):
        return kind == "bool"
    if kind == "int":
        if isinstance(v, int):
            return True
        if isinstance(v, str):
            try:
                int(v)
            except ValueError:
                return False
            return True
        return False
    if kind == "float":
        if isinstance(v, int | float):
            return True
        if isinstance(v, str):
            try:
                float(v)
            except ValueError:
                return False
            return v.strip().lower() not in {"nan", "inf", "-inf", "+inf", "infinity"}
        return False
    if kind == "bool":
        return isinstance(v, str | int | float) and str(v).lower() in _ANSIBLE_TRUE | _ANSIBLE_FALSE
    return isinstance(v, str)


_ANSIBLE_TYPE = {
    VarType.INT: "int",
    VarType.PORT: "int",
    VarType.FLOAT: "float",
    VarType.BOOL: "bool",
}


def _option(path: Path, observed: list[Any], hints: Mapping[Path, Hint]) -> dict[str, Any]:
    hint = hints.get(path, Hint())
    out: dict[str, Any] = {}
    vals = [v for v in observed if v is not None]
    if vals and all(isinstance(v, Mapping) for v in vals):
        out["type"] = "dict"
    elif vals and all(isinstance(v, list) for v in vals):
        out["type"] = "list"
        items = [i for v in vals for i in v]
        if items and all(isinstance(i, Mapping) for i in items):
            out["elements"] = "dict"
            keys: list[str] = []
            for i in items:
                keys += [k for k in i if k not in keys]
            out["options"] = {
                k: _option((*path, ITEM, k), [i[k] for i in items if k in i], hints) for k in keys
            }
        elif all(not isinstance(i, Mapping | list) for i in items):
            out["elements"] = _scalar_kind((*path, ITEM), items, hints)
    elif all(not isinstance(v, Mapping | list) for v in vals):
        out["type"] = _scalar_kind(path, vals, hints)
        if out["type"] == "str" and _fits_choices(hint, vals):
            out["choices"] = list(hint.choices or ())
    else:
        out["type"] = "raw"
    if hint.description:
        out["description"] = hint.description
    return out


def _fits_choices(hint: Hint, vals: list[Any]) -> bool:
    allowed = hint.choices or ()
    return bool(allowed) and all(isinstance(v, str) and v in allowed for v in vals)


def _scalar_kind(path: Path, vals: list[Any], hints: Mapping[Path, Hint]) -> str:
    hint = hints.get(path, Hint())
    kind = _ANSIBLE_TYPE.get(hint.type, "str") if hint.type is not None else "str"
    if hint.open_string:
        kind = "str"
    if kind != "str" and all(_converts(v, kind) for v in vals):
        return kind
    if all(isinstance(v, str) for v in vals):
        return "str"
    return "raw"  # a mix Ansible would not convert: accept anything


def argument_options(
    observed: Sequence[Mapping[str, Any]], hints: Mapping[Path, Hint]
) -> dict[str, Any]:
    """``options`` of an Ansible argument spec that every one of ``observed`` (the
    role's variables as Ansible reads them) satisfies."""
    names: list[str] = []
    for o in observed:
        names += [k for k in o if k not in names]
    return {n: _option((n,), [o[n] for o in observed if n in o], hints) for n in names}
