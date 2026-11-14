"""Go ``text/template`` emitting and a renderer for the subset we emit.

Go templates copy literal text byte for byte (no newline normalisation, no
whitespace trimming unless ``{{-``/``-}}`` is used, which we never emit), so the
only escaping needed is for ``{{`` in the literal text:

* every literal ``{{`` becomes ``{{ "{{" }}``;
* a literal ``{`` directly before a generated action becomes ``{{ "{" }}``, since
  ``{{{`` would otherwise start an action one character early.

Loops (repeated blocks) are emitted as ::

    {{ range $i, $user := .Values.users }}{{ if $i }}SEP{{ end }}...{{ $user.email }}...{{ end }}

The separator comes before every item but the first. Inside ``range`` the dot is
rebound to the item, so variables there are written ``{{ $.Values.x }}``.

The renderer understands exactly what :func:`emit` produces: ``.Values.a.b`` and
``$.Values.a.b``, ``"string"``, and that ``range``/``if $i``/``$item.field`` form. It
is exact for generated templates and refuses anything else rather than guessing.
Real Helm is checked against it in ``tests/helm/test_helm_binary.py``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from templatize.emitters.base import TemplateRenderError
from templatize.ir import VALUES_KEY_RE, Span

_LBRACE = '{{ "{" }}'
_LBRACES = '{{ "{{" }}'
_VALUES_ACTION = re.compile(r"^\$?\.Values((?:\.[A-Za-z_][A-Za-z0-9_]*)+)$")
_RANGE_ACTION = re.compile(
    r"^range \$([A-Za-z_][A-Za-z0-9_]*), \$([A-Za-z_][A-Za-z0-9_]*) := "
    r"\$?\.Values((?:\.[A-Za-z_][A-Za-z0-9_]*)+)$"
)
_IF_ACTION = re.compile(r"^if \$([A-Za-z_][A-Za-z0-9_]*)$")
_VAR_ACTION = re.compile(r"^\$([A-Za-z_][A-Za-z0-9_]*)(?:\.([A-Za-z_][A-Za-z0-9_]*))?$")
_PRINTF_ACTION = re.compile(r'^printf "%(-?)([1-9][0-9]{0,3})v" (\$[A-Za-z_][A-Za-z0-9_.]*)$')
_STRING_ACTION = re.compile(r'^"((?:[^"\\]|\\.)*)"$')


def values_ref(path: Sequence[str]) -> str:
    if not path or not all(VALUES_KEY_RE.match(k) for k in path):
        raise ValueError(f"invalid values path {'.'.join(path)!r}")
    return "{{ .Values." + ".".join(path) + " }}"


def _escape_literal(text: str, before_action: bool) -> str:
    out = text.replace("{{", "\0")  # placeholder; "\0" cannot be in YAML text we accept
    if before_action and out.endswith("{"):
        out = out[:-1] + "\1"
    return out.replace("\0", _LBRACES).replace("\1", _LBRACE)


@dataclass(frozen=True)
class RangeEmit:
    """A loop to emit as ``range``: spans are relative to the emitted text."""

    items: Sequence[Span]
    var: str
    """Loop variable without ``$`` (``user``)."""
    path: Sequence[str]
    """Values path of the list."""
    holes: Sequence[tuple[Span, str]]
    """Holes of the first item and their action (``$user.email``, ``$user``,
    ``$.Values.tenantId``), sorted."""


def _index_var(var: str) -> str:
    return "index" if var == "i" else "i"


def emit(
    text: str,
    occurrences: Sequence[tuple[Span, Sequence[str]]],
    ranges: Sequence[RangeEmit] = (),
) -> str:
    """Template for ``text`` with each span replaced by a ``.Values`` reference and
    each loop by a ``range`` over a values list.

    ``occurrences`` (outside the loops) and ``ranges`` must not overlap (the IR
    guarantees this)."""
    if "\0" in text or "\1" in text:
        raise ValueError("NUL and SOH characters are not supported in Helm templates")
    events: list[tuple[int, int, Sequence[str] | RangeEmit]] = [
        (sp.start, sp.end, path) for sp, path in occurrences
    ]
    events += [(r.items[0].start, r.items[-1].end, r) for r in ranges]
    events.sort(key=lambda e: e[0])
    parts: list[str] = []
    pos = 0
    for start, end, what in events:
        parts.append(_escape_literal(text[pos:start], before_action=True))
        if isinstance(what, RangeEmit):
            parts.append(_range(text, what))
        else:
            parts.append(values_ref(what))
        pos = end
    parts.append(_escape_literal(text[pos:], before_action=False))
    return "".join(parts)


def _range(text: str, r: RangeEmit) -> str:
    if not all(VALUES_KEY_RE.match(k) for k in r.path) or not VALUES_KEY_RE.match(r.var):
        raise ValueError(f"invalid loop {r.var!r} over {'.'.join(r.path)!r}")
    idx = _index_var(r.var)
    out = ["{{ range $" + idx + ", $" + r.var + " := .Values." + ".".join(r.path) + " }}"]
    if len(r.items) > 1:
        sep = text[r.items[0].end : r.items[1].start]
        if sep:
            out += ["{{ if $" + idx + " }}", _escape_literal(sep, before_action=True), "{{ end }}"]
    first = r.items[0]
    pos = first.start
    for sp, action in r.holes:
        out.append(_escape_literal(text[pos : sp.start], before_action=True))
        out.append("{{ " + action + " }}")
        pos = sp.end
    out.append(_escape_literal(text[pos : first.end], before_action=True))
    out.append("{{ end }}")
    return "".join(out)


def format_value(value: Any, where: str) -> str:
    """How Helm prints a value with ``{{ .Values.x }}`` (for the types we write)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        # Helm reads values via JSON, so numbers are float64 and Go prints large ones
        # in exponent form (1e+06). The values writer only emits ints below 10**6.
        if abs(value) >= 1_000_000:
            raise TemplateRenderError(
                f"{where}: {value} would render as a float in Helm; quote it in values.yaml"
            )
        return str(value)
    if isinstance(value, str):
        return value
    if value is None:
        raise TemplateRenderError(f"{where} is null")
    raise TemplateRenderError(
        f"{where} is a {type(value).__name__}; the preview renders strings, ints and bools"
    )


def _lookup(values: Mapping[str, Any], path: list[str]) -> Any:
    cur: Any = values
    for i, k in enumerate(path):
        if not isinstance(cur, Mapping) or k not in cur:
            raise TemplateRenderError(f"missing value .Values.{'.'.join(path[: i + 1])}")
        cur = cur[k]
    return cur


def _actions(template: str) -> list[tuple[str, str]]:
    """Split into ("text", ...) and ("action", inner) tokens."""
    out: list[tuple[str, str]] = []
    pos = 0
    while (start := template.find("{{", pos)) != -1:
        end = template.find("}}", start + 2)
        if end == -1:
            raise TemplateRenderError(f"unclosed action at offset {start}")
        if start > pos:
            out.append(("text", template[pos:start]))
        inner = template[start + 2 : end]
        if inner.startswith("-") or inner.endswith("-"):
            raise TemplateRenderError("whitespace-trimming actions are not supported")
        out.append(("action", inner.strip()))
        pos = end + 2
    if pos < len(template):
        out.append(("text", template[pos:]))
    return out


def _block(tokens: list[tuple[str, str]], i: int, nested: bool) -> tuple[list[Any], int]:
    """Parse tokens from ``i`` into nodes, up to a matching ``end`` if ``nested``."""
    nodes: list[Any] = []
    while i < len(tokens):
        kind, val = tokens[i]
        i += 1
        if kind == "text":
            nodes.append(("text", val))
        elif val == "end":
            if not nested:
                raise TemplateRenderError("unexpected {{ end }}")
            return nodes, i
        elif m := _RANGE_ACTION.match(val):
            body, i = _block(tokens, i, nested=True)
            nodes.append(("range", m.group(1), m.group(2), m.group(3)[1:].split("."), body))
        elif m := _IF_ACTION.match(val):
            body, i = _block(tokens, i, nested=True)
            nodes.append(("if", m.group(1), body))
        else:
            nodes.append(("expr", val))
    if nested:
        raise TemplateRenderError("missing {{ end }}")
    return nodes, i


def _eval(
    nodes: list[Any], values: Mapping[str, Any], scope: dict[str, Any], out: list[str]
) -> None:
    for node in nodes:
        kind = node[0]
        if kind == "text":
            out.append(node[1])
        elif kind == "range":
            _, idx, var, path, body = node
            items = _lookup(values, path)
            if items is None:
                continue
            if not isinstance(items, list):
                raise TemplateRenderError(f".Values.{'.'.join(path)} is not a list")
            for n, item in enumerate(items):
                _eval(body, values, {**scope, idx: n, var: item}, out)
        elif kind == "if":
            _, name, body = node
            if name not in scope:
                raise TemplateRenderError(f"undefined variable ${name}")
            if scope[name]:
                _eval(body, values, scope, out)
        else:
            out.append(_expr(node[1], values, scope))


def _expr(action: str, values: Mapping[str, Any], scope: Mapping[str, Any]) -> str:
    if m := _VALUES_ACTION.match(action):
        if scope and not action.startswith("$"):
            raise TemplateRenderError("inside range, values are read as $.Values")
        path = m.group(1)[1:].split(".")
        return format_value(_lookup(values, path), ".Values." + ".".join(path))
    if m := _STRING_ACTION.match(action):
        return str(json.loads(action))
    if m := _PRINTF_ACTION.match(action):  # a padded table column
        value = _expr(m.group(3), values, scope)
        width = int(m.group(2))
        return value.ljust(width) if m.group(1) else value.rjust(width)
    if m := _VAR_ACTION.match(action):
        name, field = m.group(1), m.group(2)
        if name not in scope:
            raise TemplateRenderError(f"undefined variable ${name}")
        value = scope[name]
        where = f"${name}"
        if field is not None:
            if not isinstance(value, Mapping) or field not in value:
                raise TemplateRenderError(f"missing value ${name}.{field}")
            value, where = value[field], f"${name}.{field}"
        return format_value(value, where)
    raise TemplateRenderError(
        f"unsupported action {{{{ {action} }}}}: only .Values references and loops are rendered here"
    )


def render(template: str, values: Mapping[str, Any]) -> str:
    nodes, _ = _block(_actions(template), 0, nested=False)
    out: list[str] = []
    _eval(nodes, values, {}, out)
    return "".join(out)
