"""``.env`` serialisation that survives a python-dotenv round trip exactly.

Every value is double-quoted. python-dotenv decodes ``\\\\ \\" \\' \\a \\b \\f \\n \\r
\\t \\v`` inside double quotes, so we escape exactly those characters. Escaping
CR/LF matters because ``.env`` files are read in text mode (universal newlines),
which would otherwise turn ``\\r\\n`` inside a value into ``\\n``. Consumers must
disable interpolation (``interpolate=False``) so ``${...}`` stays literal.
"""

from __future__ import annotations

import io

from dotenv import dotenv_values

from templatize.ir import TemplateIR

_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
    "\v": "\\v",
    "\f": "\\f",
    "\a": "\\a",
    "\b": "\\b",
}

HEADER = (
    "# Values extracted by templatize. Edit and re-run render.py.\n"
    '# Values are double-quoted; \\n, \\r, \\t, \\" and \\\\ are escapes.\n'
)


def quote(value: str) -> str:
    return '"' + "".join(_ESCAPES.get(c, c) for c in value) + '"'


def serialize_env(ir: TemplateIR) -> str:
    lines = [HEADER]
    for var in ir.variables:
        lines.append(f"{var.env_key}={quote(var.value)}\n")
    return "".join(lines)


def parse_env(text: str, *, as_file: bool = True) -> dict[str, str]:
    """Parse ``.env`` content the way render.py will.

    With ``as_file`` (default) universal-newline translation is applied first, which
    is what happens when the file is opened in text mode.
    """
    if as_file:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
    parsed = dotenv_values(stream=io.StringIO(text), interpolate=False)
    return {k: (v if v is not None else "") for k, v in parsed.items()}
