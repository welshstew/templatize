"""Terraform emitter: a ``templatefile()`` template (``.tftpl``) and the module to render it.

Fidelity rules
--------------
* Placeholders are ``${name}`` (snake_case names are HCL identifiers).
* HCL templates have two escapes: ``$${`` is a literal ``${`` and ``%%{`` a literal
  ``%{``. A literal ``${`` / ``%{`` in the text is written that way. A ``$`` (``%``)
  followed by another one, or a ``$`` just before a placeholder, is written as the
  expression ``${"$"}`` (``${"%"}``), so no run of them can be read two ways.
* Backslashes, quotes and newlines are literal in a template file; every other
  character is copied as it is.
* Values go to ``terraform.tfvars`` as a map of HCL strings, with the same escapes plus
  the string escapes (``\\\\``, ``\\"``, ``\\n``, ``\\r``, ``\\t``, ``\\uNNNN``).
* No loops in this version: HCL's ``%{ for }`` is not generated (``supports_loops``).

The renderer here implements the subset this emitter writes (literal text, the two
escapes, ``${ name }`` and ``${"string"}``), so the round trip runs without Terraform;
the tests also run real Terraform when it is installed.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from templatize.emitters.base import EmittedFile, TemplateRenderError
from templatize.ir import TemplateIR

IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
_INTERP = re.compile(r'\$\{~?[ \t]*(?:([A-Za-z_][A-Za-z0-9_-]*)|"((?:[^"\\]|\\.)*)")[ \t]*~?\}')

VALUES_FILE = "terraform.tfvars"
TEMPLATE_FILE = "template.tftpl"
_STRING_ESCAPES = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r", "\t": "\\t"}
_UNESCAPES = {"\\": "\\", '"': '"', "n": "\n", "r": "\r", "t": "\t"}


def _escape_pieces(s: str, following: str = "") -> list[tuple[str, bool]]:
    """``s`` as template pieces: (literal text, False) or (expression, True).
    ``following`` is the character written just after ``s`` ("$" for a placeholder).

    A ``$`` (or ``%``) written as text is never followed by the same character or by
    ``{`` except in the documented escape, so nothing can be read two ways:

    * before ``{``: doubled, the escape (``$${`` is a literal ``${``);
    * before another ``$`` (``%``), in the text or a placeholder: the expression
      ``${"$"}`` (``$`` + ``${x}`` would otherwise read as the escape ``$${``);
    * anywhere else: as it is.
    """
    out: list[tuple[str, bool]] = []
    for i, c in enumerate(s):
        nxt = s[i + 1] if i + 1 < len(s) else following
        if c in "$%" and nxt == "{":
            out.append((c + c, False))
        elif c in "$%" and nxt == c:
            out.append((f'${{"{c}"}}', True))
        else:
            out.append((c, False))
    return out


def escape_template(s: str, *, following: str = "") -> str:
    """Literal text for an HCL template file, followed by ``following``."""
    return "".join(piece for piece, _ in _escape_pieces(s, following))


def hcl_string(value: str) -> str:
    """A quoted HCL string whose value is exactly ``value``."""
    body: list[str] = []
    for piece, is_expr in _escape_pieces(value):
        if is_expr:
            body.append(piece)  # ${"$"}: a nested string, kept as written
            continue
        for c in piece:
            if c in _STRING_ESCAPES:
                body.append(_STRING_ESCAPES[c])
            elif ord(c) < 0x20 or ord(c) == 0x7F:
                body.append(f"\\u{ord(c):04x}")
            else:
                body.append(c)
    return '"' + "".join(body) + '"'


def _unescape_string(body: str) -> str:
    """The value of a quoted HCL string body (escapes, then template escapes)."""
    out: list[str] = []
    i = 0
    while i < len(body):
        c = body[i]
        if c == "\\":
            e = body[i + 1 : i + 2]
            if e in _UNESCAPES:
                out.append(_UNESCAPES[e])
                i += 2
            elif e == "u" and re.fullmatch(r"[0-9a-fA-F]{4}", body[i + 2 : i + 6]):
                out.append(chr(int(body[i + 2 : i + 6], 16)))
                i += 6
            else:
                raise TemplateRenderError(f"unsupported escape \\{e} in a string")
        else:
            out.append(c)
            i += 1
    return render_template("".join(out), {}, strings_only=True)


def render_template(template: str, values: Mapping[str, Any], *, strings_only: bool = False) -> str:
    out: list[str] = []
    i, n = 0, len(template)
    while i < n:
        if template.startswith(("$${", "%%{"), i):
            out.append(template[i + 1 : i + 3])
            i += 3
        elif template.startswith("${", i):
            m = _INTERP.match(template, i)
            if m is None:
                raise TemplateRenderError(
                    f"unsupported expression at offset {i}: only ${{name}} and "
                    '${"text"} are rendered here'
                )
            if m.group(1) is not None:
                if strings_only:
                    raise TemplateRenderError(f"a variable inside a value at offset {i}")
                if m.group(1) not in values:
                    raise TemplateRenderError(f"vars map does not contain key {m.group(1)!r}")
                out.append(str(values[m.group(1)]))
            else:
                out.append(_unescape_string(m.group(2)))
            i = m.end()
        elif template.startswith("%{", i):
            raise TemplateRenderError(
                f"template directives (%{{ ... }}) are not supported here (offset {i})"
            )
        else:
            j = min(
                (k for k in (template.find("$", i), template.find("%", i)) if k >= 0),
                default=n,
            )
            if j == i:  # a lone $ or % (not before "{")
                out.append(template[i])
                i += 1
            else:
                out.append(template[i:j])
                i = j
    return "".join(out)


def _string_end(content: str, i: int) -> int:
    """Offset of the closing quote of the HCL string whose body starts at ``i``: skips
    backslash escapes, the template escapes and this emitter's ``${"$"}`` pieces."""
    n = len(content)
    while i < n:
        if content[i] == "\\":
            i += 2
        elif content.startswith(("$${", "%%{"), i):
            i += 3
        elif content.startswith(('${"$"}', '${"%"}'), i):
            i += 6
        elif content[i] == '"':
            return i
        elif content[i] == "\n":
            break
        else:
            i += 1
    raise ValueError("terraform.tfvars: unterminated string")


def parse_tfvars(content: str) -> dict[str, str]:
    """Read back the ``values = { "name" = "value" ... }`` map written by this emitter."""
    start = content.find("values = {")
    if start < 0:
        raise ValueError("terraform.tfvars: no values = { ... } map")
    out: dict[str, str] = {}
    for line in content[start:].splitlines()[1:]:
        stripped = line.strip()
        if stripped == "}":
            return out
        if not stripped.startswith('"'):
            raise ValueError(f"terraform.tfvars: unexpected line {line!r}")
        body = line.index('"') + 1
        k_end = _string_end(line, body)
        rest = line[k_end + 1 :]
        eq = rest.index("=")
        v_start = k_end + 1 + eq + 1
        while line[v_start] in " \t":
            v_start += 1
        if line[v_start] != '"':
            raise ValueError(f"terraform.tfvars: value of {line[body:k_end]} is not a string")
        v_end = _string_end(line, v_start + 1)
        try:
            out[_unescape_string(line[body:k_end])] = _unescape_string(line[v_start + 1 : v_end])
        except TemplateRenderError as exc:
            raise ValueError(f"terraform.tfvars: {exc}") from exc
    raise ValueError("terraform.tfvars: the values map is not closed")


class TerraformEmitter:
    engine = "terraform"
    template_filename = TEMPLATE_FILE
    supports_loops = False

    def emit(self, ir: TemplateIR) -> str:
        parts: list[str] = []
        pos = 0
        for span, var in ir.sorted_occurrences():
            chunk = ir.text[pos : span.start]
            parts.append(escape_template(chunk, following="$"))
            parts.append("${" + var.name + "}")
            pos = span.end
        parts.append(escape_template(ir.text[pos:]))
        return "".join(parts)

    def notes(self, ir: TemplateIR) -> list[str]:
        bad = sorted(v.name for v in ir.variables if not IDENT.fullmatch(v.name))
        return [f"not valid Terraform identifiers: {', '.join(bad)}; rename them"] if bad else []

    def render(self, template: str, values: Mapping[str, Any], newline: str | None = None) -> str:
        vals = {str(k).lower(): v for k, v in values.items()}
        for k, v in vals.items():
            if not isinstance(v, str):
                raise TemplateRenderError(f"{k}: values are strings here, got {type(v).__name__}")
        return render_template(template, vals)

    def variables_in(self, template: str) -> set[str]:
        return {m.group(1) for m in _INTERP.finditer(template) if m.group(1)}

    def values_file(self, ir: TemplateIR) -> EmittedFile:
        lines = [
            "# Values extracted by templatize, passed to templatefile() by main.tf.\n",
            "# Edit them and re-run: terraform apply, then terraform output -raw rendered\n",
            "values = {\n",
            *(f"  {hcl_string(v.name)} = {hcl_string(v.value)}\n" for v in ir.variables),
            "}\n",
        ]
        return EmittedFile(path=VALUES_FILE, content="".join(lines))

    def parse_values_file(self, content: str) -> dict[str, Any]:
        return dict(parse_tfvars(content))

    def runner_files(self, ir: TemplateIR) -> list[EmittedFile]:
        return [EmittedFile(path="main.tf", content=MAIN_TF)]

    def run_command(self) -> str:
        return (
            "terraform init -input=false && terraform apply -auto-approve -input=false"
            " && terraform output -raw rendered > output.txt"
        )

    def requirements(self) -> str:
        return "Terraform 1.x (or OpenTofu); no providers are downloaded"


MAIN_TF = """\
# Renders template.tftpl with the values in terraform.tfvars. Generated by templatize.
#
#   terraform init && terraform apply -auto-approve
#   terraform output -raw rendered > output.txt
#
# In your own module: templatefile("${path.module}/template.tftpl", { name = "...", ... })

variable "values" {
  description = "The template's variables (see terraform.tfvars)."
  type        = map(string)
}

output "rendered" {
  value = templatefile("${path.module}/template.tftpl", var.values)
}
"""
