"""``values.yaml`` for templates with loops: ``.env`` cannot hold lists.

Scalars are strings, loops are lists (of mappings, or of strings for scalar loops).
Every value is written as a YAML string, so ``0080``, ``yes`` or ``1e3`` stay text,
and the file is read back with ``yaml.safe_load``; the round-trip check goes through
both, like it does for ``.env``.
"""

from __future__ import annotations

from typing import Any

import yaml

from templatize.ir import TemplateIR

HEADER = (
    "# Values extracted by templatize. Edit and re-run render.py.\n"
    "# Lists are the template's loops: add or remove items to change the output.\n"
)


_LINE_BREAKS = frozenset("\x85\u2028\u2029\ufeff\r\t")


def _needs_escapes(s: str) -> bool:
    """Characters YAML would fold, strip or reject unless escaped in double quotes
    (NEL and U+2028 are line breaks to YAML; controls and surrogates are not printable)."""
    return any(
        c in _LINE_BREAKS
        or (ord(c) < 32 and c != "\n")
        or 0x7F <= ord(c) <= 0x9F
        or 0xD800 <= ord(c) <= 0xDFFF
        or c in "\ufffe\uffff"
        for c in s
    )


class _Dumper(yaml.SafeDumper):
    pass


def _str(dumper: yaml.SafeDumper, data: str) -> yaml.ScalarNode:
    style = '"' if _needs_escapes(data) else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


_Dumper.add_representer(str, _str)


def serialize_values(ir: TemplateIR) -> str:
    # The safe dumper quotes any string that would read back as another type.
    body = yaml.dump(
        ir.values(),
        Dumper=_Dumper,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=1_000_000,
    )
    return HEADER + body


def parse_values(text: str) -> dict[str, Any]:
    data = yaml.safe_load(text)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError("values.yaml must be a mapping")
    return data
