"""NER service (templatize-ner) + client + API integration."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from templatize.build import build, specs_from_candidates
from templatize.detectors import DetectionContext
from templatize.detectors.ner import (
    NerConfig,
    NerDetector,
    check_health,
    entities_to_candidates,
)
from templatize.ir import Source
from templatize_api.app import create_app
from templatize_api.settings import Settings
from tests.conftest import FIXTURES, read_fixture

pytest.importorskip("onnxruntime")
from templatize_ner.gliner_onnx import GlinerOnnx
from templatize_ner.server import make_handler

MODEL_DIR = Path(__file__).resolve().parents[2] / "models" / "ner"
needs_model = pytest.mark.skipif(
    not (MODEL_DIR / "model.onnx").exists(), reason="GLiNER model not downloaded"
)
LETTER = read_fixture(FIXTURES / "business_letter.txt")
PRESS = (
    "Kestrel Robotics today opened its plant at Science Park 14 in Eindhoven.\n\n"
    '"We are delighted," said Sofia van Dijk, Chief Operating Officer.\n'
)


@pytest.fixture(scope="module")
def ner_url() -> Iterator[str]:
    if not (MODEL_DIR / "model.onnx").exists():
        pytest.skip("GLiNER model not downloaded")
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(GlinerOnnx(MODEL_DIR)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def _post(url: str, body: object) -> tuple[int, dict[str, object]]:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers={"content-type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


@needs_model
def test_service_endpoints(ner_url: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert check_health(ner_url)
    status, body = _post(ner_url + "/entities", {"text": LETTER})
    assert status == 200
    ents = body["entities"]
    assert isinstance(ents, list)
    found = {(e["text"], e["label"]) for e in ents}
    assert ("Samuel Okafor", "person") in found and ("GBP 18,450.00", "money") in found
    for e in ents:
        assert LETTER[e["start"] : e["end"]] == e["text"]

    assert _post(ner_url + "/entities", {"text": "x", "threshold": 5})[0] == 422
    assert _post(ner_url + "/entities", {"nope": 1})[0] == 422
    assert _post(ner_url + "/other", {})[0] == 404
    out = capsys.readouterr()
    assert "Okafor" not in out.out + out.err  # requests are never logged


@needs_model
def test_detector_and_api_integration(ner_url: str) -> None:
    det = NerDetector(NerConfig(url=ner_url))
    res = det.detect(DetectionContext(text=LETTER))
    names = {c.value: c.name for c in res.candidates}
    assert names["Ravi Patel"] == "person_name" and names["GBP 18,450.00"] == "amount"
    assert all(c.sources == [Source.NER] for c in res.candidates)

    client = TestClient(create_app(Settings(ner_url=ner_url)))
    assert client.get("/info").json()["optional_detectors"]["ner"] is True
    # The heuristics already find the letter's people: NER only fills gaps, so it
    # adds nothing there and changes nothing.
    with_ner = client.post("/suggest", json={"text": LETTER}).json()["candidates"]
    without = TestClient(create_app(Settings(ner_url=None)))
    assert with_ner == without.post("/suggest", json={"text": LETTER}).json()["candidates"]
    # In prose the heuristics miss, it adds the people, places and titles.
    cands = client.post("/suggest", json={"text": PRESS}).json()["candidates"]
    by_value = {c["value"]: c for c in cands}
    for value in ("Sofia van Dijk", "Chief Operating Officer", "Science Park 14"):
        assert by_value[value]["sources"] == ["ner"]
    # Merged with heuristics and still a valid, round-tripping build
    from templatize.suggest import SuggestResult

    parsed = SuggestResult.model_validate({"candidates": cands, "notices": []})
    assert build(PRESS, specs_from_candidates(parsed.candidates)).roundtrip.ok


def test_api_without_ner_is_unchanged() -> None:
    with_none = TestClient(create_app(Settings(ner_url=None)))
    dead = TestClient(create_app(Settings(ner_url="http://127.0.0.1:9")))
    assert dead.get("/info").json()["optional_detectors"]["ner"] is False
    a = with_none.post("/suggest", json={"text": LETTER}).json()
    b = dead.post("/suggest", json={"text": LETTER}).json()
    assert a == b  # an unreachable NER service adds nothing and costs nothing


def test_detector_degrades_and_validates() -> None:
    det = NerDetector(NerConfig(url="http://127.0.0.1:9", timeout=1))
    res = det.detect(DetectionContext(text="Alice"))
    assert res.candidates == [] and res.notices[0].kind == "detector_unavailable"
    long = NerDetector(NerConfig(url="http://127.0.0.1:9", max_chars=3)).detect(
        DetectionContext(text="Alice")
    )
    assert "longer than" in long.notices[0].message
    # malformed / out-of-range entities from the service are ignored
    cands = entities_to_candidates(
        "Alice",
        [
            {"start": 0, "end": 5, "label": "person", "score": 0.9},
            {"start": 3, "end": 99, "label": "person", "score": 0.9},
            {"start": "x"},
        ],
    )
    assert [(c.value, c.name, c.confidence) for c in cands] == [("Alice", "person_name", 0.81)]
