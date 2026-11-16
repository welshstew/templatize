"""How many labelled values each detector setup finds: detectors alone, plus GLiNER,
plus an LLM review. Produces the "Which to run" table in the README.

Three sets of documents, each with want / reject labels:

  samples  tests/quality/samples.json + expectations.yaml (the editor's samples)
  corpus   tests/quality/corpus/ + corpus.yaml
  holdout  tests/quality/holdout/ + holdout.yaml: labelled before any detector ran
           on them, and not covered by pytest. Don't write rules to pass them;
           they only stay a fair measure of unseen documents while nobody does.

uv run python scripts/eval-detectors.py [--ner URL] [--llm URL] [--set NAME ...]
                                        [--json OUT] [--verbose]

--ner adds the GLiNER service (e.g. http://127.0.0.1:8081), --llm runs the LLM review
on top (e.g. http://127.0.0.1:8080). --json writes every document's misses, noise,
LLM additions and renames, for judging them by hand.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import yaml

from templatize.detectors import default_detectors
from templatize.detectors.llm import LlmConfig, LlmUnavailable, apply_review, review_candidates
from templatize.detectors.ner import NerConfig, NerDetector, check_health
from templatize.ir import Candidate
from templatize.suggest import suggest

Q = Path(__file__).resolve().parent.parent / "tests" / "quality"
SETS = ("samples", "corpus", "holdout")


def documents(name: str) -> Iterator[tuple[str, str, list[str], dict[str, Any]]]:
    """(id, text, variants, labels) for every document in a set."""
    if name == "samples":
        labels = yaml.safe_load((Q / "expectations.yaml").read_text(encoding="utf-8"))
        for s in json.loads((Q / "samples.json").read_text(encoding="utf-8")):
            yield s["id"], s["text"], s["variants"], labels[s["id"]]
        return
    labels = yaml.safe_load((Q / f"{name}.yaml").read_text(encoding="utf-8"))
    for doc in sorted(labels):
        yield doc, (Q / name / doc).read_text(encoding="utf-8"), [], labels[doc]


def score(cands: Sequence[Candidate], labels: dict[str, Any]) -> dict[str, list[str]]:
    values = {c.value for c in cands}
    want = [str(v) for v in labels.get("want") or []]
    reject = [str(v) for v in labels.get("reject") or []]
    return {
        "found": [v for v in want if v in values],
        "missing": [v for v in want if v not in values],
        "noise": [v for v in reject if v in values],
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--ner", metavar="URL", help="GLiNER service to add to the detectors")
    ap.add_argument("--llm", metavar="URL", help="llama.cpp server for the LLM review")
    ap.add_argument("--set", dest="sets", action="append", choices=SETS, help="default: all")
    ap.add_argument("--json", metavar="OUT", type=Path, help="write per-document results")
    ap.add_argument("--verbose", "-v", action="store_true", help="list misses and noise")
    args = ap.parse_args()

    detectors = None
    if args.ner:
        if not check_health(args.ner):
            ap.error(f"no NER service at {args.ner}")
        detectors = [*default_detectors(), NerDetector(NerConfig(url=args.ner))]
    llm = LlmConfig(url=args.llm, timeout=300) if args.llm else None

    setup = "detectors" + (" + GLiNER" if args.ner else "") + (" + LLM" if args.llm else "")
    print(f"{setup}\n")
    print(
        f"{'set':<9} {'docs':>5} {'found':>11} {'noise':>6} {'added':>6} {'renamed':>8} {'secs':>7}"
    )
    report: dict[str, list[dict[str, Any]]] = {}
    for name in args.sets or SETS:
        rows = []
        for doc, text, variants, labels in documents(name):
            cands = list(suggest(text, variants, detectors=detectors).candidates)
            row: dict[str, Any] = {"id": doc}
            started = time.monotonic()
            if llm:
                try:
                    review = asyncio.run(review_candidates(llm, text, cands, variants))
                except LlmUnavailable as exc:
                    raise SystemExit(f"{name}/{doc}: {exc}") from exc
                reviewed = apply_review(cands, review)
                before = {c.value: c.name for c in cands}
                row["added"] = [
                    {"value": c.value, "name": c.name} for c in reviewed if c.value not in before
                ]
                row["renamed"] = [
                    {"value": c.value, "from": before[c.value], "to": c.name}
                    for c in reviewed
                    if c.value in before and c.name != before[c.value]
                ]
                cands = reviewed
            row["secs"] = round(time.monotonic() - started, 2)
            row.update(score(cands, labels))
            rows.append(row)
            if args.verbose and (row["missing"] or row["noise"]):
                print(f"  {name}/{doc}: missing {row['missing']} noise {row['noise']}")
        report[name] = rows
        found = sum(len(r["found"]) for r in rows)
        total = found + sum(len(r["missing"]) for r in rows)
        print(
            f"{name:<9} {len(rows):>5} {f'{found}/{total}':>11} "
            f"{sum(len(r['noise']) for r in rows):>6} "
            f"{sum(len(r.get('added', [])) for r in rows):>6} "
            f"{sum(len(r.get('renamed', [])) for r in rows):>8} "
            f"{sum(r['secs'] for r in rows):>7.1f}"
        )
    if args.json:
        args.json.write_text(
            json.dumps({"setup": setup, "sets": report}, indent=1, ensure_ascii=False),
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
