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

from jinja2 import Environment, StrictUndefined, TemplateError
from jinja2.sandbox import SandboxedEnvironment

from templatize.emitters.base import EmittedFile, TemplateRenderError
from templatize.ir import TemplateIR

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


def make_environment(newline: str = "\n", *, sandboxed: bool = True) -> Environment:
    opts = environment_options(newline)
    cls = SandboxedEnvironment if sandboxed else Environment
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
        # A raw "{" directly before "{{" would form "{{{" and break lexing.
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
        chunks: list[str] = []
        pos = 0
        for span, _ in ir.sorted_occurrences():
            chunks.append(ir.text[pos : span.start])
            pos = span.end
        chunks.append(ir.text[pos:])
        return chunks

    def newline_for(self, ir: TemplateIR) -> str:
        return dominant_newline(self.literal_chunks(ir))

    def emit(self, ir: TemplateIR) -> str:
        newline = self.newline_for(ir)
        b = _Builder()
        pos = 0
        for span, var in ir.sorted_occurrences():
            self._literal(b, ir.text[pos : span.start], newline)
            b.expr("{{ " + var.name + " }}")
            pos = span.end
        self._literal(b, ir.text[pos:], newline)
        return b.text()

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

    def render(self, template: str, values: Mapping[str, str], newline: str | None = None) -> str:
        if newline is None:
            newline = dominant_newline([template])
        env = make_environment(newline, sandboxed=True)
        try:
            return env.from_string(template).render(dict(values))
        except TemplateError as exc:
            lineno = getattr(exc, "lineno", None)
            location = f" (line {lineno})" if lineno else ""
            raise TemplateRenderError(f"{type(exc).__name__}: {exc.message}{location}") from exc

    def runner_files(self, ir: TemplateIR) -> list[EmittedFile]:
        return [EmittedFile(path="render.py", content=render_script(self.newline_for(ir)))]

    def run_command(self) -> str:
        return "uv run render.py > output.txt"


RENDER_SCRIPT = '''\
# /// script
# requires-python = ">=3.9"
# dependencies = ["jinja2>=3.1", "python-dotenv>=1.0"]
# ///
"""Render {template} with the values in .env. Generated by templatize.

    uv run render.py > output.txt          # uv installs the dependencies
    uv run render.py -o output.txt
    pip install jinja2 python-dotenv && python render.py -o output.txt

Override a value without editing .env:  --set HOSTNAME=example.org
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import dotenv_values
from jinja2 import Environment, StrictUndefined, TemplateError

HERE = Path(__file__).resolve().parent
# Line ending of the original document; Jinja normalises template newlines to this.
NEWLINE = {newline}


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--template", type=Path, default=HERE / "{template}")
    parser.add_argument("--env", type=Path, default=HERE / ".env")
    parser.add_argument("-o", "--output", type=Path, help="write here instead of stdout")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    args = parser.parse_args(argv)

    values = {{k: (v or "") for k, v in dotenv_values(args.env, interpolate=False).items()}}
    for item in args.set:
        key, sep, value = item.partition("=")
        if not sep:
            parser.error(f"--set expects KEY=VALUE, got {{item!r}}")
        values[key.upper()] = value

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
        output = env.from_string(source).render({{k.lower(): v for k, v in values.items()}})
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


def render_script(newline: str, template: str = Jinja2Emitter.template_filename) -> str:
    return RENDER_SCRIPT.format(newline=repr(newline), template=template)
