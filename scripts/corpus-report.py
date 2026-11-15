"""Detection quality on tests/quality/corpus: what is missed, what is noise, per document.

uv run python scripts/corpus-report.py [name ...]
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

from templatize.suggest import suggest

HERE = Path(__file__).resolve().parent.parent / "tests" / "quality"
EXPECT = yaml.safe_load((HERE / "corpus.yaml").read_text(encoding="utf-8"))


def check(name: str, exp: dict) -> list[str]:
    text = (HERE / "corpus" / name).read_text(encoding="utf-8")
    res = suggest(text)
    values = {c.value: c for c in res.candidates}
    problems = [f"missing {v!r}" for v in map(str, exp.get("want") or []) if v not in values]
    problems += [f"noise {v!r}" for v in map(str, exp.get("reject") or []) if v in values]
    for v, n in (exp.get("occurrences") or {}).items():
        got = len(values[str(v)].spans) if str(v) in values else 0
        if got != n:
            problems.append(f"{v!r}: {got} occurrences, want {n}")
    for v, want in (exp.get("names") or {}).items():
        got = values[str(v)].name if str(v) in values else None
        if got != want:
            problems.append(f"{v!r} named {got!r}, want {want!r}")
    if "loops" in exp:
        loops = {lp.name: len(lp.items) for lp in res.loops}
        if loops != exp["loops"]:
            problems.append(f"loops {loops}, want {exp['loops']}")
    return problems


def main() -> None:
    names = sys.argv[1:] or list(EXPECT)
    total = 0
    for name in names:
        problems = check(name, EXPECT[name])
        total += len(problems)
        print(f"{'✓' if not problems else '✗'} {name}")
        for p in problems:
            print(f"    {p}")
        if problems and len(names) == 1:
            text = (HERE / "corpus" / name).read_text(encoding="utf-8")
            for c in suggest(text).candidates:
                print(
                    f"      {c.confidence:.2f} {c.name} = {c.value!r} x{len(c.spans)} {c.detectors}"
                )
    print(f"\n{total} problems in {len(names)} documents")


if __name__ == "__main__":
    main()
