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


def _changed(value: str) -> str:
    """The value with its last letter or digit changed (an example's value)."""
    i = next((k for k in range(len(value) - 1, -1, -1) if value[k].isalnum()), None)
    if i is None:
        return value + "q"
    c = value[i]
    new = ("7" if c != "7" else "8") if c.isdigit() else ("q" if c != "q" else "z")
    return value[:i] + new + value[i + 1 :]


@pytest.mark.parametrize("name", sorted(EXPECT))
def test_corpus_with_an_example(name: str) -> None:
    """A second version of the document with every suggested value changed: with it as
    an example, accepting every suggestion renders both documents byte for byte."""
    from templatize.ansible import build_role

    text = (CORPUS / name).read_text(encoding="utf-8")
    edits = sorted((s.start, s.end, c.value) for c in suggest(text).candidates for s in c.spans)
    out, pos = [], 0
    for a, b, v in edits:
        if a >= pos:
            out += [text[pos:a], _changed(v)]
            pos = b
    example = "".join([*out, text[pos:]])
    res = suggest(text, [example])
    specs, loops = accept_suggestions(res.candidates, res.loops)
    role = build_role(text, specs, loops, filename="out", variants=[example])
    assert role.roundtrip.ok
    assert role.example_roundtrips[0].ok, role.example_roundtrips[0]


def test_every_holdout_document_is_labelled() -> None:
    # The holdout set is scored by scripts/eval-detectors.py, not asserted here.
    labels = yaml.safe_load((HERE / "holdout.yaml").read_text(encoding="utf-8"))
    assert sorted(p.name for p in (HERE / "holdout").iterdir()) == sorted(labels)
    for name, exp in labels.items():
        assert exp.get("want"), name
