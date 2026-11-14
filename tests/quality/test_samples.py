"""Suggestion quality on the editor's samples: wanted values are suggested whole,
known noise is not. See expectations.yaml."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from templatize.suggest import suggest

HERE = Path(__file__).parent
SAMPLES: list[dict[str, Any]] = json.loads((HERE / "samples.json").read_text(encoding="utf-8"))
EXPECT: dict[str, dict[str, list[Any]]] = yaml.safe_load(
    (HERE / "expectations.yaml").read_text(encoding="utf-8")
)


def test_every_sample_has_expectations() -> None:
    assert sorted(s["id"] for s in SAMPLES) == sorted(EXPECT)


@pytest.mark.parametrize("sample", SAMPLES, ids=lambda s: s["id"])
def test_sample_quality(sample: dict[str, Any]) -> None:
    exp = EXPECT[sample["id"]]
    res = suggest(sample["text"], sample["variants"])
    values = {c.value for c in res.candidates}
    missing = [str(v) for v in exp.get("want") or [] if str(v) not in values]
    noise = [str(v) for v in exp.get("reject") or [] if str(v) in values]
    assert not missing, f"not suggested: {missing}\nsuggested: {sorted(values)}"
    assert not noise, f"should not be suggested: {noise}"
    spans = {c.value: len(c.spans) for c in res.candidates}
    for value, n in (exp.get("occurrences") or {}).items():
        assert spans.get(str(value)) == n, (
            f"{value!r}: {spans.get(str(value))} occurrences, want {n}"
        )
    names = {c.value: c.name for c in res.candidates}
    for value, name in (exp.get("names") or {}).items():
        assert names.get(str(value)) == name, f"{value!r} is named {names.get(str(value))!r}"
    found = {lp.name: len(lp.items) for lp in res.loops}
    assert found == (exp.get("loops") or {}), f"loops: {found}"
