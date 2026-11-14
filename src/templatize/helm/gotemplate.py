"""Go ``text/template`` emitting and a renderer for the subset we emit.

Go templates copy literal text byte for byte (no newline normalisation, no
whitespace trimming unless ``{{-``/``-}}`` is used, which we never emit), so the
only escaping needed is for ``{{`` in the literal text:

* every literal ``{{`` becomes ``{{ "{{" }}``;
* a literal ``{`` directly before a generated action becomes ``{{ "{" }}``, since
  ``{{{`` would otherwise start an action one character early.

The renderer understands exactly two action forms, ``{{ .Values.a.b }}`` and
``{{ "string" }}``. That is everything :func:`emit` produces, so it is exact for
generated templates and refuses anything else rather than guessing. Real Helm is
checked against it in ``tests/helm/test_helm_binary.py``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from templatize.emitters.base import TemplateRenderError
from templatize.ir import VALUES_KEY_RE, Span

_LBRACE = '{{ "{" }}'
_LBRACES = '{{ "{{" }}'
_VALUES_ACTION = re.compile(r"^\.Values((?:\.[A-Za-z_][A-Za-z0-9_]*)+)$")
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


def emit(text: str, occurrences: Sequence[tuple[Span, Sequence[str]]]) -> str:
    """Template for ``text`` with each span replaced by a ``.Values`` reference.

    ``occurrences`` must be sorted and non-overlapping (the IR guarantees this)."""
    if "\0" in text or "\1" in text:
        raise ValueError("NUL and SOH characters are not supported in Helm templates")
    parts: list[str] = []
    pos = 0
    for span, path in occurrences:
        parts.append(_escape_literal(text[pos : span.start], before_action=True))
        parts.append(values_ref(path))
        pos = span.end
    parts.append(_escape_literal(text[pos:], before_action=False))
    return "".join(parts)


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


def render(template: str, values: Mapping[str, Any]) -> str:
    out: list[str] = []
    pos = 0
    while (start := template.find("{{", pos)) != -1:
        end = template.find("}}", start + 2)
        if end == -1:
            raise TemplateRenderError(f"unclosed action at offset {start}")
        out.append(template[pos:start])
        inner = template[start + 2 : end]
        if inner.startswith("-") or inner.endswith("-"):
            raise TemplateRenderError("whitespace-trimming actions are not supported")
        action = inner.strip()
        if m := _VALUES_ACTION.match(action):
            path = m.group(1)[1:].split(".")
            out.append(format_value(_lookup(values, path), ".Values." + ".".join(path)))
        elif m := _STRING_ACTION.match(action):
            out.append(json.loads(action))
        else:
            raise TemplateRenderError(
                f"unsupported action {{{{{inner}}}}}: only .Values references are rendered here"
            )
        pos = end + 2
    out.append(template[pos:])
    return "".join(out)
