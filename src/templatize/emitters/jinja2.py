"""Jinja2 emitter.

Fidelity rules
--------------
* Literal ``{{``, ``{%`` and ``{#`` are emitted as string expressions
  (``{{ "{%" }}``). This is used instead of ``{% raw %}`` because raw blocks
  cannot contain a literal ``{% endraw %}``.
* A literal ``{`` immediately before a generated ``{{`` is escaped too.
* Jinja normalises every newline in template data to ``newline_sequence``. We
  choose the document's dominant line ending as ``newline_sequence`` and emit any
  other line endings (mixed files) as expressions such as ``{{ "\\r\\n" }}``.
* Environment: ``autoescape=False``, ``keep_trailing_newline=True``,
  ``trim_blocks``/``lstrip_blocks`` off, ``StrictUndefined``.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping
from typing import Any

from jinja2 import Environment, StrictUndefined, TemplateError, meta
from jinja2.exceptions import SecurityError
from jinja2.runtime import Context
from jinja2.sandbox import ImmutableSandboxedEnvironment

from templatize.emitters.base import EmittedFile, TemplateRenderError
from templatize.ir import Loop, Span, TemplateIR, Variable

NEWLINES = ("\n", "\r\n", "\r")
_NL = re.compile(r"\r\n|\r|\n")
_DELIM = re.compile(r"\{[{%#]")
_NL_EXPR = {"\n": '{{ "\\n" }}', "\r\n": '{{ "\\r\\n" }}', "\r": '{{ "\\r" }}'}
_BRACE_EXPR = '{{ "{" }}'


def environment_options(newline: str) -> dict[str, object]:
    if newline not in NEWLINES:
        raise ValueError(f"unsupported newline sequence {newline!r}")
    return {
        "autoescape": False,
        "keep_trailing_newline": True,
        "trim_blocks": False,
        "lstrip_blocks": False,
        "newline_sequence": newline,
        "undefined": StrictUndefined,
    }


# Limits for rendering untrusted, user-edited templates (POST /render, MCP).
MAX_RENDER_CHARS = 10_000_000
MAX_POWER_EXPONENT = 64


class GuardedSandboxedEnvironment(ImmutableSandboxedEnvironment):
    """Jinja's sandbox blocks unsafe attribute access but not resource exhaustion such
    as ``{{ "a" * 10**9 }}``. This variant also bounds sequence repetition and powers.
    (``range`` is already capped by the sandbox; output size is capped in ``render``.)"""

    intercepted_binops = frozenset({"*", "**"})

    def call_binop(self, context: Context, operator: str, left: Any, right: Any) -> Any:
        if operator == "**" and isinstance(right, int | float) and abs(right) > MAX_POWER_EXPONENT:
            raise SecurityError(f"exponent larger than {MAX_POWER_EXPONENT} is not allowed")
        if operator == "*":
            for seq, n in ((left, right), (right, left)):
                if (
                    isinstance(seq, str | bytes | list | tuple)
                    and isinstance(n, int)
                    and len(seq) * n > MAX_RENDER_CHARS
                ):
                    raise SecurityError("result of repetition is too large")
        return super().call_binop(context, operator, left, right)


def make_environment(newline: str = "\n", *, sandboxed: bool = True) -> Environment:
    opts = environment_options(newline)
    cls = GuardedSandboxedEnvironment if sandboxed else Environment
    return cls(**opts)  # type: ignore[arg-type]


def dominant_newline(chunks: list[str]) -> str:
    counts: Counter[str] = Counter()
    for chunk in chunks:
        counts.update(_NL.findall(chunk))
    if not counts:
        return "\n"
    # Deterministic tie-break: \n, \r\n, \r.
    return max(NEWLINES, key=lambda nl: (counts[nl], -NEWLINES.index(nl)))


class _Builder:
    def __init__(self) -> None:
        self.parts: list[str] = []

    def raw(self, s: str) -> None:
        if s:
            self.parts.append(s)

    def expr(self, s: str) -> None:
        # A raw "{" directly before "{{" or "{%" would form "{{{" / "{{%" and break lexing.
        if self.parts and self.parts[-1].endswith("{"):
            self.parts[-1] = self.parts[-1][:-1]
            self.parts.append(_BRACE_EXPR)
        self.parts.append(s)

    def text(self) -> str:
        return "".join(self.parts)


class Jinja2Emitter:
    engine = "jinja2"
    template_filename = "template.j2"

    def literal_chunks(self, ir: TemplateIR) -> list[str]:
        holes = sorted(
            [s for v in ir.variables for s in v.spans]
            + [s for lp in ir.loops for f in lp.fields for s in f.spans],
            key=lambda s: s.start,
        )
        chunks: list[str] = []
        pos = 0
        for span in holes:
            chunks.append(ir.text[pos : span.start])
            pos = span.end
        chunks.append(ir.text[pos:])
        return chunks

    def newline_for(self, ir: TemplateIR) -> str:
        return dominant_newline(self.literal_chunks(ir))

    def emit(self, ir: TemplateIR) -> str:
        newline = self.newline_for(ir)
        b = _Builder()
        regions = [(lp.region, lp) for lp in ir.loops]
        events: list[tuple[Span, Variable | Loop]] = [
            (span, var)
            for span, var in ir.sorted_occurrences()
            if not any(r.contains(span) for r, _ in regions)
        ]
        events += regions
        events.sort(key=lambda e: e[0].start)
        pos = 0
        for span, what in events:
            self._literal(b, ir.text[pos : span.start], newline)
            if isinstance(what, Loop):
                self._loop(b, ir, what, newline)
            else:
                b.expr("{{ " + what.name + " }}")
            pos = span.end
        self._literal(b, ir.text[pos:], newline)
        return b.text()

    def _loop(self, b: _Builder, ir: TemplateIR, lp: Loop, newline: str) -> None:
        """``{% for item in name %}{% if not loop.first %}SEP{% endif %}BODY{% endfor %}``:
        the body is the first item with its holes as expressions."""
        b.expr("{% for " + lp.item + " in " + lp.name + " %}")
        sep = lp.separator(ir.text)
        if sep:
            b.expr("{% if not loop.first %}")
            self._literal(b, sep, newline)
            b.expr("{% endif %}")
        first = lp.items[0]
        holes: list[tuple[Span, str]] = [
            (f.spans[0], lp.item if lp.scalar else f"{lp.item}.{f.name}") for f in lp.fields
        ]
        holes += [(sp, v.name) for v in ir.variables for sp in v.spans if first.contains(sp)]
        holes.sort(key=lambda h: h[0].start)
        pos = first.start
        for sp, expr in holes:
            self._literal(b, ir.text[pos : sp.start], newline)
            b.expr("{{ " + expr + " }}")
            pos = sp.end
        self._literal(b, ir.text[pos : first.end], newline)
        b.expr("{% endfor %}")

    def _literal(self, b: _Builder, chunk: str, newline: str) -> None:
        pos = 0
        for m in _NL.finditer(chunk):
            self._escape_line(b, chunk[pos : m.start()])
            nl = m.group(0)
            if nl == newline:
                b.raw(nl)
            else:
                b.expr(_NL_EXPR[nl])
            pos = m.end()
        self._escape_line(b, chunk[pos:])

    @staticmethod
    def _escape_line(b: _Builder, s: str) -> None:
        pos = 0
        for m in _DELIM.finditer(s):
            b.raw(s[pos : m.start()])
            b.expr('{{ "' + m.group(0) + '" }}')
            pos = m.end()
        b.raw(s[pos:])

    def render(self, template: str, values: Mapping[str, Any], newline: str | None = None) -> str:
        if newline is None:
            newline = dominant_newline([template])
        env = make_environment(newline, sandboxed=True)
        try:
            parts: list[str] = []
            size = 0
            for chunk in env.from_string(template).generate(dict(values)):
                size += len(chunk)
                if size > MAX_RENDER_CHARS:
                    raise TemplateRenderError(
                        f"output exceeds {MAX_RENDER_CHARS} characters; rendering stopped"
                    )
                parts.append(chunk)
            return "".join(parts)
        except TemplateRenderError:
            raise
        except TemplateError as exc:
            lineno = getattr(exc, "lineno", None)
            location = f" (line {lineno})" if lineno else ""
            raise TemplateRenderError(f"{type(exc).__name__}: {exc.message}{location}") from exc
        except Exception as exc:  # user templates can raise anything (1/0, range overflow, ...)
            raise TemplateRenderError(f"{type(exc).__name__}: {exc}") from exc

    def variables_in(self, template: str) -> set[str]:
        env = make_environment(dominant_newline([template]), sandboxed=True)
        try:
            return set(meta.find_undeclared_variables(env.parse(template)))
        except TemplateError as exc:
            lineno = getattr(exc, "lineno", None)
            location = f" (line {lineno})" if lineno else ""
            raise TemplateRenderError(f"{type(exc).__name__}: {exc.message}{location}") from exc

    def runner_files(self, ir: TemplateIR) -> list[EmittedFile]:
        return [
            EmittedFile(
                path="render.py",
                content=render_script(self.newline_for(ir), loops=bool(ir.loops)),
            )
        ]

    def run_command(self) -> str:
        return "uv run render.py > output.txt"


RENDER_SCRIPT = '''\
# /// script
# requires-python = ">=3.9"
# dependencies = ["jinja2>=3.1", "{loader_dep}"]
# ///
"""Render {template} with the values in {values_file}. Generated by templatize.

    uv run render.py > output.txt          # uv installs the dependencies
    uv run render.py -o output.txt
    pip install jinja2 {loader_pip} && python render.py -o output.txt

Override a value without editing {values_file}:  --set HOSTNAME=example.org
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

{loader_import}
from jinja2 import Environment, StrictUndefined, TemplateError

HERE = Path(__file__).resolve().parent
# Line ending of the original document; Jinja normalises template newlines to this.
NEWLINE = {newline}


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--template", type=Path, default=HERE / "{template}")
    parser.add_argument("--values", "--env", dest="values", type=Path, default=HERE / "{values_file}")
    parser.add_argument("-o", "--output", type=Path, help="write here instead of stdout")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    args = parser.parse_args(argv)

{load_values}
    for item in args.set:
        key, sep, value = item.partition("=")
        if not sep:
            parser.error(f"--set expects KEY=VALUE, got {{item!r}}")
        values[key.lower()] = value

    # newline="" keeps the template's line endings exactly as written.
    with open(args.template, encoding="utf-8", newline="") as fh:
        source = fh.read()

    env = Environment(
        autoescape=False,
        keep_trailing_newline=True,
        trim_blocks=False,
        lstrip_blocks=False,
        newline_sequence=NEWLINE,
        undefined=StrictUndefined,
    )
    try:
        output = env.from_string(source).render(values)
    except TemplateError as exc:
        print(f"render failed: {{exc}}", file=sys.stderr)
        return 1

    data = output.encode("utf-8")
    if args.output:
        args.output.write_bytes(data)
    else:
        sys.stdout.buffer.write(data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


_ENV_LOADER = {
    "loader_dep": "python-dotenv>=1.0",
    "loader_pip": "python-dotenv",
    "loader_import": "from dotenv import dotenv_values",
    "values_file": ".env",
    "load_values": (
        '    values = {k.lower(): (v or "")'
        " for k, v in dotenv_values(args.values, interpolate=False).items()}"
    ),
}
_YAML_LOADER = {
    "loader_dep": "pyyaml>=6.0",
    "loader_pip": "pyyaml",
    "loader_import": "import yaml",
    "values_file": "values.yaml",
    "load_values": (
        "    # Strings and lists of items: the lists are the template's loops.\n"
        '    values = yaml.safe_load(args.values.read_text(encoding="utf-8")) or {}'
    ),
}


def render_script(
    newline: str, template: str = Jinja2Emitter.template_filename, *, loops: bool = False
) -> str:
    loader = _YAML_LOADER if loops else _ENV_LOADER
    return RENDER_SCRIPT.format(newline=repr(newline), template=template, **loader)
