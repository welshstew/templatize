"""Terraform emitter: a ``templatefile()`` template (``.tftpl``) and the module to render it.

Fidelity rules
--------------
* Placeholders are ``${name}`` (snake_case names are HCL identifiers).
* HCL templates have two escapes: ``$${`` is a literal ``${`` and ``%%{`` a literal
  ``%{``. A literal ``${`` / ``%{`` in the text is written that way. A ``$`` (``%``)
  followed by another one, or just before a placeholder (directive), is written as the
  expression ``${"$"}`` (``${"%"}``), so no run of them can be read two ways.
* Backslashes, quotes and newlines are literal in a template file; every other
  character is copied as it is, except a lone carriage return (old Mac line ending),
  written ``${"\\r"}``: after a raw one Terraform stops interpolating.
* Loops are ``%{ for user_index, user in users }...%{ endfor }`` with the separator in
  ``%{ if user_index > 0 }...%{ endif }`` (no ``~`` strip markers, so whitespace is
  exactly the text's). A nested list loops over ``server.ports``; a padded table column
  is ``${format("%-12s", row.name)}``. A map loop's entries are a list of
  ``{ key, value }`` objects, because Terraform iterates a map in key order.
* Values go to ``terraform.tfvars`` as one object: strings (with the template escapes
  plus ``\\\\``, ``\\"``, ``\\n``, ``\\r``, ``\\t``, ``\\uNNNN``) and, for loops, lists.

The renderer here implements the subset this emitter writes (literal text, the two
escapes, references, ``${"string"}``, ``format("%-Ns", ...)``, ``for`` and ``if``), so
the round trip runs without Terraform; the tests also run real Terraform.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from templatize.emitters.base import EmittedFile, TemplateRenderError
from templatize.ir import Loop, Span, TemplateIR, Variable

IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
_PATH = r"[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*)*"
_STR = r'"((?:[^"\\]|\\.)*)"'
_INTERP = re.compile(
    r"\$\{[ \t]*(?:(?P<path>" + _PATH + r")|" + _STR + r"|format\([ \t]*\"(?P<fmt>%-?\d*s)\"[ \t]*,"
    r"[ \t]*(?P<arg>" + _PATH + r")[ \t]*\))[ \t]*\}"
)
_FOR = re.compile(
    r"%\{[ \t]*for[ \t]+(?P<idx>[A-Za-z_][\w-]*)[ \t]*,[ \t]*(?P<var>[A-Za-z_][\w-]*)[ \t]+in[ \t]+"
    r"(?P<src>" + _PATH + r")[ \t]*\}"
)
_IF = re.compile(r"%\{[ \t]*if[ \t]+(?P<idx>[A-Za-z_][\w-]*)[ \t]*>[ \t]*0[ \t]*\}")
_END = re.compile(r"%\{[ \t]*(?P<kw>endfor|endif)[ \t]*\}")

VALUES_FILE = "terraform.tfvars"
TEMPLATE_FILE = "template.tftpl"
_STRING_ESCAPES = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r", "\t": "\\t"}
_UNESCAPES = {"\\": "\\", '"': '"', "n": "\n", "r": "\r", "t": "\t"}
_KEYWORDS = frozenset({"null"})  # the others (true, for, if, ...) are reserved names already


# --- escaping ------------------------------------------------------------------------


def _escape_pieces(s: str, following: str = "") -> list[tuple[str, bool]]:
    """``s`` as template pieces: (literal text, False) or (expression, True).
    ``following`` is the character written just after ``s`` (``$`` before a
    placeholder, ``%`` before a directive).

    A ``$`` (or ``%``) written as text is never followed by the same character or by
    ``{`` except in the documented escape, so nothing can be read two ways:

    * before ``{``: doubled, the escape (``$${`` is a literal ``${``);
    * before another ``$`` (``%``), in the text or a placeholder (directive): the
      expression ``${"$"}`` (``$`` + ``${x}`` would otherwise read as ``$${``);
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
    """Literal text for an HCL template file, followed by ``following``.

    A lone carriage return (not part of CR LF) is written ``${"\\r"}``: after a raw one,
    Terraform (checked with 1.17) stops recognising ``${...}`` for the rest of the file.
    """
    out: list[str] = []
    pieces = _escape_pieces(s, following)
    for i, (piece, _) in enumerate(pieces):
        nxt = s[i + 1] if i + 1 < len(s) else following
        out.append('${"\\r"}' if piece == "\r" and nxt != "\n" else piece)
    return "".join(out)


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


def hcl_value(value: Any, indent: int = 2) -> str:
    """Strings, lists and objects as HCL (the shapes of a template's values)."""
    if isinstance(value, str):
        return hcl_string(value)
    pad, inner = " " * (indent - 2), " " * indent
    if isinstance(value, list):
        if not value:
            return "[]"
        return "[\n" + "".join(f"{inner}{hcl_value(v, indent + 2)},\n" for v in value) + pad + "]"
    if isinstance(value, dict):
        if not value:
            return "{}"
        rows = "".join(
            f"{inner}{hcl_string(str(k))} = {hcl_value(v, indent + 2)}\n" for k, v in value.items()
        )
        return "{\n" + rows + pad + "}"
    raise TypeError(f"cannot write {type(value).__name__} as an HCL value")


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
    nodes = _parse("".join(out), 0, None, strings_only=True)[0]
    return _render(nodes, [{}])


# --- rendering -----------------------------------------------------------------------


@dataclass
class _Text:
    text: str


@dataclass
class _Expr:
    path: str | None = None
    string: str | None = None
    fmt: str | None = None


@dataclass
class _For:
    idx: str
    var: str
    src: str
    body: list[Any] = field(default_factory=list)


@dataclass
class _If:
    idx: str
    body: list[Any] = field(default_factory=list)


def _parse(
    template: str, i: int, stop: str | None, *, strings_only: bool = False
) -> tuple[list[Any], int]:
    """Nodes from ``i`` up to the ``%{ end<stop> }`` closing them (or the end)."""
    nodes: list[Any] = []
    text: list[str] = []
    n = len(template)

    def flush() -> None:
        if text:
            nodes.append(_Text("".join(text)))
            text.clear()

    while i < n:
        if template.startswith(("$${", "%%{"), i):
            text.append(template[i + 1 : i + 3])
            i += 3
        elif template.startswith("${", i):
            m = _INTERP.match(template, i)
            if m is None:
                raise TemplateRenderError(f"unsupported expression at offset {i}")
            if strings_only and m.group(2) is None:
                raise TemplateRenderError(f"a reference inside a value at offset {i}")
            flush()
            if m.group("path"):
                nodes.append(_Expr(path=m.group("path")))
            elif m.group("fmt"):
                nodes.append(_Expr(path=m.group("arg"), fmt=m.group("fmt")))
            else:
                nodes.append(_Expr(string=_unescape_string(m.group(2))))
            i = m.end()
        elif template.startswith("%{", i):
            if strings_only:
                raise TemplateRenderError(f"a directive inside a value at offset {i}")
            flush()
            if m := _FOR.match(template, i):
                body, i = _parse(template, m.end(), "endfor")
                nodes.append(_For(m.group("idx"), m.group("var"), m.group("src"), body))
            elif m := _IF.match(template, i):
                body, i = _parse(template, m.end(), "endif")
                nodes.append(_If(m.group("idx"), body))
            else:
                end = _END.match(template, i)
                if end is not None and stop is not None and end.group("kw") == stop:
                    return nodes, end.end()  # stop is "endfor" / "endif"
                raise TemplateRenderError(
                    f"unsupported directive at offset {i}: only for/if/endfor/endif are rendered here"
                )
        else:
            j = min(
                (k for k in (template.find("$", i), template.find("%", i)) if k >= 0), default=n
            )
            if j == i:  # a lone $ or % (not before "{")
                text.append(template[i])
                i += 1
            else:
                text.append(template[i:j])
                i = j
    if stop:
        raise TemplateRenderError(f"%{{ {stop} }} is not closed")
    flush()
    return nodes, i


def _lookup(path: str, scopes: list[dict[str, Any]]) -> Any:
    head, *rest = path.split(".")
    for scope in reversed(scopes):
        if head in scope:
            value = scope[head]
            break
    else:
        raise TemplateRenderError(f"vars map does not contain key {head!r}")
    for attr in rest:
        if not isinstance(value, dict) or attr not in value:
            raise TemplateRenderError(f"{path}: no attribute {attr!r}")
        value = value[attr]
    return value


def _string(value: Any, what: str) -> str:
    if not isinstance(value, str):
        raise TemplateRenderError(f"{what}: a string is needed here, got {type(value).__name__}")
    return value


def _format(fmt: str, value: str) -> str:
    m = re.fullmatch(r"%(-?)(\d*)s", fmt)
    assert m is not None
    width = int(m.group(2) or 0)
    return value.ljust(width) if m.group(1) else value.rjust(width)


def _rows(value: Any, what: str) -> list[Any]:
    """A loop's rows: a list, or a map loop's mapping as ``{key, value}`` entries."""
    if isinstance(value, dict):
        return [{"key": k, "value": v} for k, v in value.items()]
    if not isinstance(value, list):
        raise TemplateRenderError(
            f"{what}: a list is needed to loop over, got {type(value).__name__}"
        )
    return value


def _render(nodes: list[Any], scopes: list[dict[str, Any]]) -> str:
    out: list[str] = []
    for node in nodes:
        if isinstance(node, _Text):
            out.append(node.text)
        elif isinstance(node, _Expr):
            if node.string is not None:
                out.append(node.string)
            else:
                assert node.path is not None
                value = _string(_lookup(node.path, scopes), node.path)
                out.append(_format(node.fmt, value) if node.fmt else value)
        elif isinstance(node, _For):
            for i, row in enumerate(_rows(_lookup(node.src, scopes), node.src)):
                out.append(_render(node.body, [*scopes, {node.idx: i, node.var: row}]))
        elif isinstance(node, _If) and _lookup(node.idx, scopes) > 0:
            out.append(_render(node.body, scopes))
    return "".join(out)


def render_template(template: str, values: Mapping[str, Any]) -> str:
    return _render(_parse(template, 0, None)[0], [dict(values)])


# --- the values file -----------------------------------------------------------------


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


def _skip(content: str, i: int) -> int:
    while i < len(content) and content[i] in " \t\r\n,":
        i += 1
    return i


def _parse_value(content: str, i: int) -> tuple[Any, int]:
    i = _skip(content, i)
    if i >= len(content):
        raise ValueError("terraform.tfvars: unexpected end")
    c = content[i]
    if c == '"':
        end = _string_end(content, i + 1)
        try:
            return _unescape_string(content[i + 1 : end]), end + 1
        except TemplateRenderError as exc:
            raise ValueError(f"terraform.tfvars: {exc}") from exc
    if c == "[":
        items: list[Any] = []
        i = _skip(content, i + 1)
        while content[i] != "]":
            v, i = _parse_value(content, i)
            items.append(v)
            i = _skip(content, i)
        return items, i + 1
    if c == "{":
        obj: dict[str, Any] = {}
        i = _skip(content, i + 1)
        while content[i] != "}":
            key, i = _parse_value(content, i)
            i = _skip(content, i)
            if content[i] != "=":
                raise ValueError(f"terraform.tfvars: expected = after {key!r}")
            obj[str(key)], i = _parse_value(content, i + 1)
            i = _skip(content, i)
        return obj, i + 1
    raise ValueError(f"terraform.tfvars: unexpected {c!r} at offset {i}")


def parse_tfvars(content: str) -> dict[str, Any]:
    """Read back the ``values = { ... }`` object written by this emitter."""
    m = re.search(r"^values[ \t]*=", content, re.MULTILINE)
    if m is None:
        raise ValueError("terraform.tfvars: no values = { ... } object")
    value, _ = _parse_value(content, m.end())
    if not isinstance(value, dict):
        raise ValueError("terraform.tfvars: values must be an object")
    return value


# --- the emitter ---------------------------------------------------------------------


class TerraformEmitter:
    engine = "terraform"
    template_filename = TEMPLATE_FILE
    supports_loops = True

    def emit(self, ir: TemplateIR) -> str:
        tokens: list[tuple[str, str]] = []  # ("text", literal) or ("code", placeholder/directive)
        regions = [(lp.region, lp) for lp in ir.loops if lp.parent is None]
        events: list[tuple[Span, Variable | Loop]] = [
            (span, var)
            for span, var in ir.sorted_occurrences()
            if not any(r.contains(span) for r, _ in regions)
        ]
        events += regions
        events.sort(key=lambda e: e[0].start)
        pos = 0
        for span, what in events:
            tokens.append(("text", ir.text[pos : span.start]))
            if isinstance(what, Loop):
                self._loop(tokens, ir, what)
            else:
                tokens.append(("code", "${" + what.name + "}"))
            pos = span.end
        tokens.append(("text", ir.text[pos:]))
        out: list[str] = []
        for i, (kind, s) in enumerate(tokens):
            if kind == "code":
                out.append(s)
            else:
                following = next((t[1][0] for t in tokens[i + 1 :] if t[1]), "")
                out.append(escape_template(s, following=following))
        return "".join(out)

    def _loop(self, tokens: list[tuple[str, str]], ir: TemplateIR, lp: Loop) -> None:
        source = lp.name
        if lp.parent is not None:  # a nested list: a field of the parent's item
            parent = next(p for p in ir.loops if p.name == lp.parent and p.parent is None)
            source = f"{parent.item}.{lp.name}"
        index = f"{lp.item}_index"
        tokens.append(("code", f"%{{ for {index}, {lp.item} in {source} }}"))
        sep = lp.separator(ir.text)
        if sep:
            tokens.append(("code", f"%{{ if {index} > 0 }}"))
            tokens.append(("text", sep))
            tokens.append(("code", "%{ endif }"))
        first = lp.items[0]
        holes: list[tuple[Span, str | Loop]] = []
        for n, f in enumerate(lp.fields):
            if lp.map:
                ref = f"{lp.item}.{'key' if n == 0 else 'value'}"
            else:
                ref = lp.item if lp.scalar else f"{lp.item}.{f.name}"
            if f.align is not None:  # a padded table column
                flag = "-" if f.align == "left" else ""
                ref = f'format("%{flag}{f.width}s", {ref})'
            holes.append((f.spans[0], ref))
        nested = [(c.group_region(0), c) for c in ir.children(lp)]
        holes += nested
        holes += [
            (sp, v.name)
            for v in ir.variables
            for sp in v.spans
            if first.contains(sp) and not any(r.contains(sp) for r, _ in nested)
        ]
        holes.sort(key=lambda h: h[0].start)
        pos = first.start
        for sp, what in holes:
            tokens.append(("text", ir.text[pos : sp.start]))
            if isinstance(what, Loop):
                self._loop(tokens, ir, what)
            else:
                tokens.append(("code", "${" + what + "}"))
            pos = sp.end
        tokens.append(("text", ir.text[pos : first.end]))
        tokens.append(("code", "%{ endfor }"))

    def notes(self, ir: TemplateIR) -> list[str]:
        names = [v.name for v in ir.variables] + [lp.name for lp in ir.loops if lp.parent is None]
        bad = sorted(n for n in names if not IDENT.fullmatch(n) or n in _KEYWORDS)
        return (
            [f"not usable as Terraform template variables: {', '.join(bad)}; rename them"]
            if bad
            else []
        )

    def render(self, template: str, values: Mapping[str, Any], newline: str | None = None) -> str:
        return render_template(template, {str(k).lower(): v for k, v in values.items()})

    def variables_in(self, template: str) -> set[str]:
        out = {m.group("path").split(".")[0] for m in _INTERP.finditer(template) if m.group("path")}
        loop_vars = {m.group(g) for m in _FOR.finditer(template) for g in ("idx", "var")}
        out |= {m.group("src").split(".")[0] for m in _FOR.finditer(template)}
        return out - loop_vars

    @staticmethod
    def values(ir: TemplateIR) -> dict[str, Any]:
        out: dict[str, Any] = {v.name: v.value for v in ir.variables}
        for lp in ir.loops:
            if lp.parent is None:
                rows = ir.loop_value(lp)
                out[lp.name] = (
                    [{"key": k, "value": v} for k, v in rows.items()]
                    if isinstance(rows, dict)
                    else rows
                )
        return out

    def values_file(self, ir: TemplateIR) -> EmittedFile:
        content = (
            "# Values extracted by templatize, passed to templatefile() by main.tf.\n"
            "# Edit them and re-run: terraform apply, then terraform output -raw rendered\n"
            + (
                "# Lists are the template's loops: add or remove items to change the output.\n"
                if ir.loops
                else ""
            )
            + "values = "
            + hcl_value(self.values(ir))
            + "\n"
        )
        return EmittedFile(path=VALUES_FILE, content=content)

    def parse_values_file(self, content: str) -> dict[str, Any]:
        return parse_tfvars(content)

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
  description = "The template's variables and loop lists (see terraform.tfvars)."
  type        = any
}

output "rendered" {
  value = templatefile("${path.module}/template.tftpl", var.values)
}
"""
