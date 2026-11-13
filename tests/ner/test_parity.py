"""Parity with the reference `gliner` package (golden outputs produced by
scripts/dev/gliner_golden.py on the same ONNX file), plus unit tests of the
re-implemented pre/post-processing."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODEL_DIR = ROOT / "models" / "ner"
GOLDEN = json.loads((Path(__file__).parent / "golden.json").read_text(encoding="utf-8"))

pytest.importorskip("onnxruntime")
from templatize_ner.gliner_onnx import (  # noqa: E402
    GlinerOnnx,
    ModelMissing,
    greedy_flat,
    split_words,
)

needs_model = pytest.mark.skipif(
    not (MODEL_DIR / "model.onnx").exists(),
    reason="GLiNER model not downloaded (scripts/download-ner-model.sh)",
)


@pytest.fixture(scope="module")
def model() -> GlinerOnnx:
    return GlinerOnnx(MODEL_DIR)


# fp32 inference is near-deterministic across CPUs (ARM vs x86 differ by ~1e-5), but
# a span scored right at the threshold can still flip; such borderline spans may be
# present on one side only. Everything else must match exactly.
SCORE_TOL = 0.005
BORDERLINE = 0.02


@needs_model
@pytest.mark.parametrize("case", sorted(GOLDEN["cases"]))
def test_matches_reference_gliner(model: GlinerOnnx, case: str) -> None:
    expected = GOLDEN["cases"][case]
    threshold = GOLDEN["threshold"]
    got = model.predict(expected["text"], GOLDEN["labels"], threshold)
    exp = {(e["start"], e["end"], e["label"]): e["score"] for e in expected["entities"]}
    act = {(e.start, e.end, e.label): e.score for e in got}
    for k in exp.keys() ^ act.keys():
        score = exp.get(k, act.get(k))
        assert score is not None and abs(score - threshold) < BORDERLINE, (case, k, score)
    for k in exp.keys() & act.keys():
        assert act[k] == pytest.approx(exp[k], abs=SCORE_TOL), (case, k)
    for e in got:
        assert expected["text"][e.start : e.end] == e.text


def test_word_splitter_matches_gliner() -> None:
    words = [w.text for w in split_words("Jean-Luc's e-mail: a_b@c.d, 18,450.00 🎉")]
    assert words == [
        "Jean-Luc",
        "'",
        "s",
        "e-mail",
        ":",
        "a_b",
        "@",
        "c",
        ".",
        "d",
        ",",
        "18",
        ",",
        "450",
        ".",
        "00",
        "🎉",
    ]


def test_greedy_flat_prefers_higher_score_and_is_inclusive() -> None:
    spans = [(0, 1, "person", 0.6), (1, 2, "org", 0.9), (3, 3, "loc", 0.7), (2, 3, "x", 0.5)]
    assert greedy_flat(spans) == [(1, 2, "org", 0.9), (3, 3, "loc", 0.7)]


@needs_model
def test_long_text_windows_find_entities_everywhere(model: GlinerOnnx) -> None:
    filler = "The service restarted without incident and the logs were rotated as planned. " * 60
    text = "Ravi Patel opened the ticket. " + filler + "It was closed by Samuel Okafor."
    found = {e.text for e in model.predict(text, ["person"])}
    assert {"Ravi Patel", "Samuel Okafor"} <= found
    assert len(text.split()) > 700  # several windows


@needs_model
def test_empty_inputs(model: GlinerOnnx) -> None:
    assert model.predict("", ["person"]) == []
    assert model.predict("Alice", []) == []


def test_missing_model_is_a_clear_error(tmp_path: Path) -> None:
    with pytest.raises(ModelMissing, match=r"model\.onnx.*download-ner-model"):
        GlinerOnnx(tmp_path)


def test_onnxruntime_telemetry_is_disabled() -> None:
    import os

    assert os.environ.get("ORT_DISABLE_TELEMETRY") == "1"
    src = (ROOT / "src" / "templatize_ner" / "gliner_onnx.py").read_text()
    assert "ort.disable_telemetry_events()" in src
    assert "ORT_DISABLE_TELEMETRY=1" in (ROOT / "docker" / "ner.Dockerfile").read_text()
