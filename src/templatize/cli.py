"""Command-line interface for quick manual testing.

    templatize suggest file.txt [--variant other.txt ...] [--json]
    templatize build file.txt --accept-all [-o outdir] [--zip bundle.zip]
    templatize build file.txt --vars vars.json

Exit status: 0 on success, 1 on usage/input errors, 2 if the round-trip check fails.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from templatize.ansible import RoleError, build_role
from templatize.build import (
    BuildResult,
    LoopSpec,
    VariableSpec,
    accept_suggestions,
    build,
    specs_from_candidates,
)
from templatize.bundle import make_chart_zip, make_role_zip, make_zip
from templatize.detectors.llm import LlmConfig, LlmUnavailable, apply_review, review_candidates
from templatize.doctypes import TYPES, UnknownDocumentTypeError
from templatize.emitters import EMITTERS
from templatize.helm import ChartError
from templatize.helm import build as build_helm_chart
from templatize.ir import Candidate, IRValidationError
from templatize.suggest import InputLimitError, SuggestResult, suggest
from templatize.userschema import SchemaError


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
    for lp in res.loops:
        if lp.confidence < min_conf:
            continue
        fields = "scalar" if lp.scalar else ", ".join(f.name for f in lp.fields)
        print(
            f"loop {lp.confidence:.2f}  {lp.name} ({len(lp.items)} x {lp.item}: {fields})"
            f"  {','.join(lp.detectors)}"
        )
    for n in res.notices:
        where = f" at {n.span.start}" if n.span else ""
        print(f"note: {n.kind}{where}: {n.message}")
    print(f"{len(rows)} candidate(s)")
    d = res.document_type
    if d is not None:
        how = "confirmed" if d.confirmed else f"detected, {d.confidence:.2f}"
        print(f"document type: {d.label} ({d.id}, {how})")
    si = res.schema_info
    if si is not None:
        if si.reference:
            print(f"schema reference: {si.reference.url} (not downloaded)")
        if si.provided:
            print(f"schema: {si.title or 'supplied'}, {si.matched_fields} field(s) matched")
        for note in si.notes:
            print(f"note: {note}")


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


def _suggest(
    text: str,
    variants: list[str],
    llm_url: str | None,
    document_type: str | None = None,
    schema: str | None = None,
) -> SuggestResult:
    res = suggest(text, variants, document_type=document_type, schema=schema)
    if not llm_url:
        return res
    # The key comes from the environment, never a flag (shell history, `ps`).
    api_key = os.environ.get("TEMPLATIZE_LLM_API_KEY") or None
    try:
        review = asyncio.run(
            review_candidates(
                LlmConfig(url=llm_url, api_key=api_key), text, res.candidates, variants
            )
        )
    except LlmUnavailable as exc:
        print(f"warning: LLM review skipped: {exc}", file=sys.stderr)
        return res
    return res.model_copy(
        update={
            "candidates": apply_review(res.candidates, review),
            "notices": [*res.notices, *review.notices],
        }
    )


TYPE_HELP = "confirm the document type (its format rules then apply): " + ", ".join(
    t.id for t in TYPES
)


SCHEMA_HELP = (
    "a JSON Schema (JSON or YAML) the document follows; a schema it references by URL "
    "is never downloaded"
)


def cmd_suggest(args: argparse.Namespace) -> int:
    res = _suggest(
        _read(args.file),
        [_read(v) for v in args.variant],
        args.llm_url,
        args.document_type,
        _read(args.schema) if args.schema else None,
    )
    if args.json:
        print(res.model_dump_json(indent=2))
    else:
        _print_candidates(res, args.min_confidence)
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    text = _read(args.file)
    specs: list[VariableSpec]
    loops: list[LoopSpec] = []
    if args.vars:
        specs = TypeAdapter(list[VariableSpec]).validate_json(Path(args.vars).read_bytes())
    else:
        res = _suggest(
            text,
            [_read(v) for v in args.variant],
            args.llm_url,
            args.document_type,
            _read(args.schema) if args.schema else None,
        )
        chosen = _select(res, args.min_confidence)
        if args.no_loops or args.loops:
            specs = specs_from_candidates(chosen)
        else:
            found = [
                lp for lp in res.loops if lp.auto_accept and lp.confidence >= args.min_confidence
            ]
            specs, loops = accept_suggestions(chosen, found)
    if args.loops:
        loops = TypeAdapter(list[LoopSpec]).validate_json(Path(args.loops).read_bytes())
    result = build(text, specs, engine=args.engine, loops=loops)
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

    for note in result.notes:
        print(f"note: {note}", file=sys.stderr)
    rt = result.roundtrip
    verdict = "PASS" if rt.ok else "FAIL"
    print(
        f"round-trip: {verdict} ({len(result.ir.variables)} variables"
        + (f", {len(result.ir.loops)} loops" if result.ir.loops else "")
        + f", {rt.expected_bytes} bytes)"
        + (f": {rt.error} at char {rt.first_difference}" if not rt.ok else ""),
        file=sys.stderr,
    )
    return 0 if rt.ok else 2


def cmd_chart(args: argparse.Namespace) -> int:
    text = _read(args.file)
    variants = [_read(v) for v in args.variant]
    specs: list[VariableSpec]
    loops: list[LoopSpec] = []
    if args.vars:
        specs = TypeAdapter(list[VariableSpec]).validate_json(Path(args.vars).read_bytes())
    else:
        res = _suggest(text, variants, args.llm_url, getattr(args, "document_type", None))
        chosen = _select(res, args.min_confidence)
        if args.no_loops or args.style == "scaffold":
            specs = specs_from_candidates(chosen)
        else:
            found = [
                lp for lp in res.loops if lp.auto_accept and lp.confidence >= args.min_confidence
            ]
            specs, loops = accept_suggestions(chosen, found)
    if args.loops:
        loops = TypeAdapter(list[LoopSpec]).validate_json(Path(args.loops).read_bytes())
    labels = args.label or None
    result = build_helm_chart(
        args.style, text, specs, variants, chart_name=args.name, variant_labels=labels, loops=loops
    )
    if args.output:
        root = args.output / result.chart_name
        for f in result.files:
            target = root / f.path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(f.content.encode("utf-8", "surrogatepass"))
        print(
            f"wrote chart {result.chart_name!r} ({len(result.files)} files) to {root}",
            file=sys.stderr,
        )
    if args.zip:
        args.zip.write_bytes(make_chart_zip(result))
        print(f"wrote {args.zip}", file=sys.stderr)
    if args.json:
        print(result.model_dump_json(indent=2))
    elif not (args.output or args.zip):
        for f in result.files:
            sys.stdout.write(f"# --- {f.path}\n{f.content}")
            if not f.content.endswith("\n"):
                sys.stdout.write("\n")
    for rt in result.roundtrips:
        verdict = "PASS" if rt.ok else "FAIL"
        print(
            f"round-trip {rt.document} with {rt.label}: {verdict}"
            + (f": {rt.error}" if not rt.ok else ""),
            file=sys.stderr,
        )
    for cov in result.coverage:
        if not cov.available:
            print(f"coverage {cov.document}: not checked ({cov.error})", file=sys.stderr)
        elif cov.error:
            print(f"coverage {cov.document} with {cov.label}: ERROR {cov.error}", file=sys.stderr)
        else:
            print(
                f"coverage {cov.document} with {cov.label}: {cov.percent:.0f}% of fields identical "
                f"({cov.lost} lost, {cov.changed} changed, {cov.added} added by the scaffold)",
                file=sys.stderr,
            )
    for note in result.notes:
        print(f"note: {note}", file=sys.stderr)
    return 0 if result.ok else 2


def make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="templatize", description="Turn text into a template.")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("suggest", help="list parameter candidates")
    s.add_argument("file", help="input file ('-' for stdin)")
    s.add_argument("--variant", action="append", default=[], help="another example of the document")
    s.add_argument("--type", dest="document_type", metavar="ID", help=TYPE_HELP)
    s.add_argument("--schema", metavar="FILE", help=SCHEMA_HELP)
    s.add_argument("--json", action="store_true")
    s.add_argument("--min-confidence", type=float, default=0.0)
    s.add_argument("--llm-url", help="llama.cpp server URL for the optional LLM review")
    s.set_defaults(func=cmd_suggest)

    b = sub.add_parser("build", help="generate template, .env and render script")
    b.add_argument("file", help="input file ('-' for stdin)")
    b.add_argument("--variant", action="append", default=[])
    b.add_argument("--type", dest="document_type", metavar="ID", help=TYPE_HELP)
    b.add_argument("--schema", metavar="FILE", help=SCHEMA_HELP)
    sel = b.add_mutually_exclusive_group(required=True)
    sel.add_argument("--accept-all", action="store_true", help="accept every suggestion")
    sel.add_argument("--vars", help="JSON file with a list of variable specs")
    b.add_argument(
        "--loops",
        help="JSON file with a list of loop specs (repeated blocks; values go to values.yaml)",
    )
    b.add_argument(
        "--no-loops",
        action="store_true",
        help="with --accept-all: keep repeated blocks as separate variables",
    )
    b.add_argument("--min-confidence", type=float, default=0.0)
    b.add_argument("--llm-url", help="llama.cpp server URL for the optional LLM review")
    b.add_argument("--engine", default="jinja2", choices=sorted(EMITTERS))
    b.add_argument("-o", "--output", type=Path, help="write files to this directory")
    b.add_argument("--zip", type=Path, help="write a zip bundle")
    b.add_argument("--print", choices=["template", "env", "script"])
    b.add_argument("--json", action="store_true", help="print the full build result as JSON")
    b.set_defaults(func=cmd_build)

    c = sub.add_parser("chart", help="generate a Helm chart from Kubernetes manifests")
    c.add_argument("file", help="manifests file ('-' for stdin)")
    c.add_argument(
        "--variant",
        action="append",
        default=[],
        help="the same manifests for another environment (becomes values-<label>.yaml)",
    )
    c.add_argument(
        "--label", action="append", default=[], help="label for each --variant, e.g. staging"
    )
    csel = c.add_mutually_exclusive_group(required=True)
    csel.add_argument("--accept-all", action="store_true", help="accept every suggestion")
    csel.add_argument("--vars", help="JSON file with a list of variable specs")
    c.add_argument("--loops", help="JSON file with a list of loop specs (exact style)")
    c.add_argument(
        "--no-loops",
        action="store_true",
        help="with --accept-all: keep repeated blocks as separate variables",
    )
    c.add_argument("--name", help="chart name (default: the first workload's name)")
    c.add_argument(
        "--style",
        choices=["exact", "scaffold"],
        default="exact",
        help="exact: your manifests as templates (byte-for-byte); scaffold: the helm create "
        "chart filled in from them (coverage checked with helm)",
    )
    c.add_argument("--min-confidence", type=float, default=0.0)
    c.add_argument("--llm-url", help="llama.cpp server URL for the optional LLM review")
    c.add_argument("-o", "--output", type=Path, help="write the chart directory under this path")
    c.add_argument("--zip", type=Path, help="write the chart as a zip")
    c.add_argument("--json", action="store_true", help="print the full result as JSON")
    c.set_defaults(func=cmd_chart)

    r = sub.add_parser("role", help="generate an Ansible role that renders the document")
    r.add_argument("file", help="input file ('-' for stdin)")
    r.add_argument("--variant", action="append", default=[], help="another example of the document")
    r.add_argument("--label", action="append", default=[], help="label for each --variant")
    rsel = r.add_mutually_exclusive_group(required=True)
    rsel.add_argument("--accept-all", action="store_true", help="accept every suggestion")
    rsel.add_argument("--vars", help="JSON file with a list of variable specs")
    r.add_argument("--loops", help="JSON file with a list of loop specs")
    r.add_argument(
        "--no-loops",
        action="store_true",
        help="with --accept-all: keep repeated blocks as separate variables",
    )
    r.add_argument("--name", default="config", help="role name, also the variable prefix")
    r.add_argument("--file-name", help="rendered file name (default: the input file's name)")
    r.add_argument("--dest", help="default destination path (role variable <name>_dest)")
    r.add_argument("--min-confidence", type=float, default=0.0)
    r.add_argument("--llm-url", help="llama.cpp server URL for the optional LLM review")
    r.add_argument("-o", "--output", type=Path, help="write the role (and playbook.yml) here")
    r.add_argument("--zip", type=Path, help="write the role as a zip")
    r.add_argument("--json", action="store_true", help="print the full result as JSON")
    r.set_defaults(func=cmd_role)

    k = sub.add_parser(
        "check",
        help="check a rendered document against its JSON Schema",
        description=(
            "Check a document against the schema it should follow: --schema, else the "
            "bundled schema its $schema names, else the bundled schema of its type "
            "(Kubernetes per resource kind, Compose, GitHub Actions, cloud-init, ...). "
            "Offline. Exit status: 0 valid, 2 problems, 3 no schema applies."
        ),
    )
    k.add_argument("file", help="the document ('-' for stdin)")
    k.add_argument("--type", dest="document_type", metavar="ID", help=TYPE_HELP)
    k.add_argument("--schema", metavar="FILE", help="a JSON Schema (JSON or YAML) to check against")
    k.add_argument("--json", action="store_true", help="print the result as JSON")
    k.set_defaults(func=cmd_check)
    return p


def cmd_role(args: argparse.Namespace) -> int:
    text = _read(args.file)
    variants = [_read(v) for v in args.variant]
    specs: list[VariableSpec]
    loops: list[LoopSpec] = []
    if args.vars:
        specs = TypeAdapter(list[VariableSpec]).validate_json(Path(args.vars).read_bytes())
    else:
        res = _suggest(text, variants, args.llm_url, getattr(args, "document_type", None))
        chosen = _select(res, args.min_confidence)
        if args.no_loops:
            specs = specs_from_candidates(chosen)
        else:
            found = [
                lp for lp in res.loops if lp.auto_accept and lp.confidence >= args.min_confidence
            ]
            specs, loops = accept_suggestions(chosen, found)
    if args.loops:
        loops = TypeAdapter(list[LoopSpec]).validate_json(Path(args.loops).read_bytes())
    filename = args.file_name or (Path(args.file).name if args.file != "-" else "config")
    result = build_role(
        text,
        specs,
        loops,
        role_name=args.name,
        filename=filename,
        dest=args.dest,
        variants=variants,
        variant_labels=args.label or None,
    )
    if args.output:
        for f in result.files:
            target = args.output / f.path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(f.content.encode("utf-8", "surrogatepass"))
        print(f"wrote role {result.role_name!r} and playbook.yml to {args.output}", file=sys.stderr)
    if args.zip:
        args.zip.write_bytes(make_role_zip(result))
        print(f"wrote {args.zip}", file=sys.stderr)
    if args.json:
        print(result.model_dump_json(indent=2))
    elif not (args.output or args.zip):
        for f in result.files:
            sys.stdout.write(f"# --- {f.path}\n{f.content}")
    checks = [("defaults", result.roundtrip)] + [
        (f"example {i + 2}", rt) for i, rt in enumerate(result.example_roundtrips)
    ]
    for label, rt in checks:
        verdict = "PASS" if rt.ok else f"FAIL: {rt.error}"
        print(f"round-trip ({label}): {verdict}", file=sys.stderr)
    return 0 if all(rt.ok for _, rt in checks) else 1


def cmd_check(args: argparse.Namespace) -> int:
    """Check a (rendered) document against its JSON Schema: 0 valid, 2 problems,
    3 no schema applies."""
    from templatize.schemacheck import check_output

    text = _read(args.file)
    result = check_output(
        text,
        schema=_read(args.schema) if args.schema else None,
        document_type=args.document_type,
    )
    if args.json:
        print(result.model_dump_json(indent=2) if result else "null")
    if result is None:
        if not args.json:
            print(
                "no schema applies: give one with --schema, or the document type with --type",
                file=sys.stderr,
            )
        return 3
    if not args.json:
        multi = any(p.document for p in result.problems)
        for prob in result.problems:
            where = f"document {prob.document + 1}: " if multi else ""
            print(f"{args.file}: {where}{prob.path}: {prob.message}")
        against = ", ".join(result.schemas) or "its schema"
        if result.note:
            print(f"note: {result.note}", file=sys.stderr)
        verdict = (
            f"valid against {against}"
            if result.ok
            else f"{len(result.problems)} problem{'s' if len(result.problems) != 1 else ''} "
            f"against {against}"
            if result.problems
            else "not checked"
        )
        print(f"schema check: {verdict}", file=sys.stderr)
    return 0 if result.ok else 2


def main(argv: Sequence[str] | None = None) -> int:
    args = make_parser().parse_args(argv)
    try:
        rc: int = args.func(args)
        return rc
    except (
        InputLimitError,
        IRValidationError,
        ChartError,
        RoleError,
        ValidationError,
        UnicodeDecodeError,
        OSError,
        UnknownDocumentTypeError,
        SchemaError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
