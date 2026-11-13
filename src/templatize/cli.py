"""Command-line interface for quick manual testing.

    templatize suggest file.txt [--variant other.txt ...] [--json]
    templatize build file.txt --accept-all [-o outdir] [--zip bundle.zip]
    templatize build file.txt --vars vars.json

Exit status: 0 on success, 1 on usage/input errors, 2 if the round-trip check fails.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from templatize.build import BuildResult, VariableSpec, build, specs_from_candidates
from templatize.bundle import make_zip
from templatize.emitters import EMITTERS
from templatize.ir import Candidate, IRValidationError
from templatize.suggest import InputLimitError, SuggestResult, suggest


def _read(path: str) -> str:
    data = sys.stdin.buffer.read() if path == "-" else Path(path).read_bytes()
    return data.decode("utf-8")


def _short(value: str, width: int = 40) -> str:
    r = repr(value)[1:-1]
    return r if len(r) <= width else r[: width - 1] + "…"


def _print_candidates(res: SuggestResult, min_conf: float) -> None:
    rows = [c for c in res.candidates if c.confidence >= min_conf]
    print(f"{'conf':>5}  {'type':<9} {'name':<24} {'n':>2}  {'sources':<22} value")
    for c in rows:
        print(
            f"{c.confidence:5.2f}  {c.type.value:<9} {c.name:<24} {len(c.spans):>2}  "
            f"{','.join(s.value for s in c.sources):<22} {_short(c.value)}"
        )
        if c.variant_values:
            print(f"{'':>45}variants: {', '.join(_short(v, 20) for v in c.variant_values)}")
    for n in res.notices:
        where = f" at {n.span.start}" if n.span else ""
        print(f"note: {n.kind}{where}: {n.message}")
    print(f"{len(rows)} candidate(s)")


def _select(res: SuggestResult, min_conf: float) -> list[Candidate]:
    return [c for c in res.candidates if c.confidence >= min_conf]


def _write_outputs(result: BuildResult, outdir: Path | None, zip_path: Path | None) -> None:
    if outdir:
        outdir.mkdir(parents=True, exist_ok=True)
        for f in result.files:
            (outdir / f.path).write_bytes(f.content.encode("utf-8", "surrogatepass"))
        print(f"wrote {', '.join(f.path for f in result.files)} to {outdir}", file=sys.stderr)
    if zip_path:
        zip_path.write_bytes(make_zip(result))
        print(f"wrote {zip_path}", file=sys.stderr)


def cmd_suggest(args: argparse.Namespace) -> int:
    res = suggest(_read(args.file), [_read(v) for v in args.variant])
    if args.json:
        print(res.model_dump_json(indent=2))
    else:
        _print_candidates(res, args.min_confidence)
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    text = _read(args.file)
    specs: list[VariableSpec]
    if args.vars:
        specs = TypeAdapter(list[VariableSpec]).validate_json(Path(args.vars).read_bytes())
    else:
        res = suggest(text, [_read(v) for v in args.variant])
        specs = specs_from_candidates(_select(res, args.min_confidence))
    result = build(text, specs, engine=args.engine)
    _write_outputs(result, args.output, args.zip)

    if args.json:
        print(result.model_dump_json(indent=2))
    elif args.print:
        target = {"template": result.template, "env": result.env}.get(args.print)
        content = target.content if target else result.runner[0].content
        sys.stdout.write(content)
    elif not (args.output or args.zip):
        sys.stdout.write(result.template.content)
        if not result.template.content.endswith("\n"):
            sys.stdout.write("\n")

    rt = result.roundtrip
    verdict = "PASS" if rt.ok else "FAIL"
    print(
        f"round-trip: {verdict} ({len(result.ir.variables)} variables, {rt.expected_bytes} bytes)"
        + (f": {rt.error} at char {rt.first_difference}" if not rt.ok else ""),
        file=sys.stderr,
    )
    return 0 if rt.ok else 2


def make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="templatize", description="Turn text into a template.")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("suggest", help="list parameter candidates")
    s.add_argument("file", help="input file ('-' for stdin)")
    s.add_argument("--variant", action="append", default=[], help="another example of the document")
    s.add_argument("--json", action="store_true")
    s.add_argument("--min-confidence", type=float, default=0.0)
    s.set_defaults(func=cmd_suggest)

    b = sub.add_parser("build", help="generate template, .env and render script")
    b.add_argument("file", help="input file ('-' for stdin)")
    b.add_argument("--variant", action="append", default=[])
    sel = b.add_mutually_exclusive_group(required=True)
    sel.add_argument("--accept-all", action="store_true", help="accept every suggestion")
    sel.add_argument("--vars", help="JSON file with a list of variable specs")
    b.add_argument("--min-confidence", type=float, default=0.0)
    b.add_argument("--engine", default="jinja2", choices=sorted(EMITTERS))
    b.add_argument("-o", "--output", type=Path, help="write files to this directory")
    b.add_argument("--zip", type=Path, help="write a zip bundle")
    b.add_argument("--print", choices=["template", "env", "script"])
    b.add_argument("--json", action="store_true", help="print the full build result as JSON")
    b.set_defaults(func=cmd_build)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = make_parser().parse_args(argv)
    try:
        rc: int = args.func(args)
        return rc
    except (
        InputLimitError,
        IRValidationError,
        ValidationError,
        UnicodeDecodeError,
        OSError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
