"""Detection quality on tests/quality/corpus: documents the detectors were not written
against, with expectations in corpus.yaml (see scripts/corpus-report.py)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from templatize.build import accept_suggestions, make_ir
from templatize.emitters.jinja2 import Jinja2Emitter
from templatize.suggest import suggest

HERE = Path(__file__).parent
CORPUS = HERE / "corpus"
EXPECT: dict[str, dict[str, Any]] = yaml.safe_load(
    (HERE / "corpus.yaml").read_text(encoding="utf-8")
)


def test_every_document_has_expectations() -> None:
    assert sorted(p.name for p in CORPUS.iterdir()) == sorted(EXPECT)


@pytest.mark.parametrize("name", sorted(EXPECT))
def test_corpus_quality(name: str) -> None:
    exp = EXPECT[name]
    text = (CORPUS / name).read_text(encoding="utf-8")
    res = suggest(text)
    found = {c.value: c for c in res.candidates}
    missing = [str(v) for v in exp.get("want") or [] if str(v) not in found]
    noise = [str(v) for v in exp.get("reject") or [] if str(v) in found]
    assert not missing, f"not suggested: {missing}\nsuggested: {sorted(found)}"
    assert not noise, f"should not be suggested: {noise}"
    for value, n in (exp.get("occurrences") or {}).items():
        assert len(found[str(value)].spans) == n, f"{value!r}: want {n} occurrences"
    for value, want in (exp.get("names") or {}).items():
        assert found[str(value)].name == want, f"{value!r} is named {found[str(value)].name!r}"
    if "loops" in exp:
        assert {lp.name: len(lp.items) for lp in res.loops} == exp["loops"]


@pytest.mark.parametrize("name", sorted(EXPECT))
def test_corpus_round_trip(name: str) -> None:
    """Accepting every suggestion gives a template that renders the document back."""
    text = (CORPUS / name).read_text(encoding="utf-8")
    res = suggest(text)
    specs, loops = accept_suggestions(res.candidates, res.loops)
    ir = make_ir(text, specs, loops)
    emitter = Jinja2Emitter()
    assert emitter.render(emitter.emit(ir), ir.values()) == text
