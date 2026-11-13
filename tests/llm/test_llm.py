from __future__ import annotations

import asyncio
import json
import logging
import re
import socket
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from templatize.build import build, specs_from_candidates
from templatize.cli import main as cli_main
from templatize.detectors import DetectionContext
from templatize.detectors.llm import (
    LlmConfig,
    LlmDetector,
    LlmUnavailable,
    apply_review,
    check_health,
    review_candidates,
)
from templatize.ir import Source
from templatize.suggest import suggest
from templatize_api.app import create_app
from templatize_api.settings import Settings
from tests.llm.fake_llama import FakeLlama, fake_llama

TEXT = (
    "Dear Ms. Alice Johnson,\n"
    "your order PO-55821 ships from our Bristol warehouse via host db01.example.com.\n"
    "Contact: support desk, extension 4417.\n"
    "version: 2.14.3\n"
)


def ids_by_value(req: dict[str, Any]) -> dict[str, str]:
    """Map candidate values to the short ids the prompt used (as the model would see them)."""
    user = req["messages"][1]["content"]
    out = {}
    for line in user.splitlines():
        if line.startswith('{"id"'):
            entry = json.loads(line)
            out[entry["value"]] = entry["id"]
    return out


def answer(cands: list[dict[str, Any]], missed: list[dict[str, Any]]) -> str:
    return json.dumps({"candidates": cands, "missed": missed})


def review(fake: FakeLlama, text: str = TEXT, **cfg: Any) -> Any:
    cands = suggest(text).candidates
    return asyncio.run(review_candidates(LlmConfig(url=fake.url, **cfg), text, cands)), cands


def test_request_uses_constrained_json_schema_and_never_offsets() -> None:
    with fake_llama() as fake:
        review(fake)
        req = fake.requests[0]
    rf = req["response_format"]
    assert rf["type"] == "json_schema"
    schema = rf["json_schema"]["schema"]
    assert set(schema["required"]) == {"candidates", "missed"}
    missed_props = schema["properties"]["missed"]["items"]["properties"]
    assert set(missed_props) == {"substring", "occurrence", "name", "type", "reason"}
    blob = json.dumps(req).lower()
    assert "offset" not in blob and '"start"' not in blob
    assert re.search(r'"id": "c1"', req["messages"][1]["content"])
    assert TEXT in req["messages"][1]["content"]


def test_renames_fixed_and_missed() -> None:
    with fake_llama() as fake:

        def respond(req: dict[str, Any]) -> str:
            ids = ids_by_value(req)
            return answer(
                [
                    {"id": ids["db01.example.com"], "fixed_text": False, "name": "warehouse_host"},
                    {
                        "id": ids["2.14.3"],
                        "fixed_text": True,
                        "name": "version",
                        "reason": "doc version",
                    },
                ],
                [
                    {
                        "substring": "PO-55821",
                        "occurrence": 0,
                        "name": "order_number",
                        "type": "string",
                        "reason": "order id",
                    },
                    {
                        "substring": "Bristol",
                        "occurrence": 0,
                        "name": "warehouse_city",
                        "type": "string",
                        "reason": "city",
                    },
                    {
                        "substring": "Alice Johnson",
                        "occurrence": 0,
                        "name": "recipient_name",
                        "type": "string",
                        "reason": "person",
                    },
                    {
                        "substring": "Atlantis",
                        "occurrence": 0,
                        "name": "city",
                        "type": "string",
                        "reason": "made up",
                    },
                    {
                        "substring": "Bristol",
                        "occurrence": 5,
                        "name": "bad_index",
                        "type": "string",
                        "reason": "x",
                    },
                    {
                        "substring": "Contact",
                        "occurrence": 3,
                        "name": "contact",
                        "type": "string",
                        "reason": "bad index",
                    },
                ],
            )

        fake.responder = respond
        rv, cands = review(fake)

    by_id = {c.id: c for c in cands}
    renamed = {by_id[r.candidate_id].value: r.name for r in rv.renames}
    assert renamed["db01.example.com"] == "warehouse_host"
    assert [by_id[f.candidate_id].value for f in rv.fixed] == ["2.14.3"]
    added = {a.value: (a.name, a.type.value, a.sources) for a in rv.additions}
    assert added["Bristol"][0] == "warehouse_city"
    assert added["Alice Johnson"][:2] == ("recipient_name", "string")
    assert all(a.sources == [Source.LLM] for a in rv.additions)
    # a hallucinated substring and two out-of-range occurrence indexes: all discarded
    assert "Atlantis" not in added
    assert rv.discarded == 3
    assert any("discarded" in n.message for n in rv.notices)
    for a in rv.additions:
        assert TEXT[a.spans[0].start : a.spans[0].end] == a.value


def test_missed_items_already_covered_are_skipped() -> None:
    with fake_llama() as fake:
        fake.responder = lambda req: answer(
            [],
            [
                {
                    "substring": "db01.example.com",
                    "occurrence": 0,
                    "name": "host",
                    "type": "hostname",
                    "reason": "dup",
                }
            ],
        )
        rv, _ = review(fake)
    assert rv.additions == [] and rv.discarded == 0


def test_occurrence_index_selects_the_right_one() -> None:
    text = "a=red b=red c=red\n"
    with fake_llama() as fake:
        fake.responder = lambda req: answer(
            [],
            [
                {
                    "substring": "red",
                    "occurrence": 2,
                    "name": "colour",
                    "type": "string",
                    "reason": "x",
                }
            ],
        )
        rv = asyncio.run(review_candidates(LlmConfig(url=fake.url), text, []))
    (add,) = rv.additions
    assert add.spans[0].start == text.rindex("red")


def test_apply_review_respects_key_names_and_merges() -> None:
    text = "db_host: 10.0.0.5\nserver at 192.168.1.20 ok\n"
    with fake_llama() as fake:

        def respond(req: dict[str, Any]) -> str:
            ids = ids_by_value(req)
            return answer(
                [
                    {"id": ids["10.0.0.5"], "fixed_text": False, "name": "database_ip"},
                    {"id": ids["192.168.1.20"], "fixed_text": False, "name": "app_server_ip"},
                ],
                [
                    {
                        "substring": "ok",
                        "occurrence": 0,
                        "name": "status",
                        "type": "string",
                        "reason": "x",
                    }
                ],
            )

        fake.responder = respond
        cands = suggest(text).candidates
        rv = asyncio.run(review_candidates(LlmConfig(url=fake.url), text, cands))
    merged = {c.value: c for c in apply_review(cands, rv)}
    assert merged["10.0.0.5"].name == "db_host"  # key-derived name kept
    assert merged["192.168.1.20"].name == "app_server_ip"  # type name replaced
    assert merged["192.168.1.20"].name_origin == "llm"
    assert Source.LLM in merged["192.168.1.20"].sources
    assert merged["ok"].sources == [Source.LLM]
    result = build(text, specs_from_candidates(list(merged.values())))
    assert result.roundtrip.ok


def test_fixed_verdict_lowers_confidence() -> None:
    text = "version: 2.14.3\n"
    with fake_llama() as fake:
        fake.responder = lambda req: answer(
            [{"id": "c1", "fixed_text": True, "name": "version", "reason": "constant"}], []
        )
        cands = suggest(text).candidates
        rv = asyncio.run(review_candidates(LlmConfig(url=fake.url), text, cands))
    (after,) = apply_review(cands, rv)
    assert after.confidence == pytest.approx(cands[0].confidence * 0.5, abs=0.01)
    assert "likely fixed" in (after.reason or "")


def test_unavailable_and_bad_output() -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        dead = f"http://127.0.0.1:{s.getsockname()[1]}"
    with pytest.raises(LlmUnavailable, match="unreachable"):
        asyncio.run(review_candidates(LlmConfig(url=dead, timeout=2), "x: 1\n", []))
    assert check_health(dead) is False

    with fake_llama() as fake:
        fake.responder = lambda req: "not json"
        with pytest.raises(LlmUnavailable, match="schema"):
            review(fake)
        fake.raw_override = b"{}"
        with pytest.raises(LlmUnavailable, match="shape"):
            review(fake)
        fake.raw_override = None
        assert check_health(fake.url) is True
        fake.health_status = 503  # model still loading
        assert check_health(fake.url) is False


def test_long_documents_are_not_sent() -> None:
    with fake_llama() as fake:
        rv, _ = review(fake, text="x: 1\n" * 100, max_chars=50)
        assert fake.requests == []
    assert rv.notices[0].kind == "detector_unavailable"


def test_async_detector_wrapper_degrades() -> None:
    det = LlmDetector(LlmConfig(url="http://127.0.0.1:9", timeout=1))
    res = asyncio.run(det.detect_async(DetectionContext(text="a: b\n"), []))
    assert res.candidates == [] and res.notices[0].kind == "detector_unavailable"


# --- API & CLI ---------------------------------------------------------------------


def test_api_suggest_llm(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    with fake_llama() as fake:

        def respond(req: dict[str, Any]) -> str:
            ids = ids_by_value(req)
            return answer(
                [{"id": ids["db01.example.com"], "fixed_text": False, "name": "warehouse_host"}],
                [
                    {
                        "substring": "Bristol",
                        "occurrence": 0,
                        "name": "warehouse_city",
                        "type": "string",
                        "reason": "city",
                    }
                ],
            )

        fake.responder = respond
        client = TestClient(create_app(Settings(llm_url=fake.url)))
        assert client.get("/info").json()["optional_detectors"]["llm"] is True
        heuristic_ids = {
            c["id"] for c in client.post("/suggest", json={"text": TEXT}).json()["candidates"]
        }
        r = client.post("/suggest/llm", json={"text": TEXT}).json()
    assert r["available"] is True
    assert {
        x["candidate_id"] for x in r["review"]["renames"]
    } <= heuristic_ids  # ids line up with /suggest
    assert [a["value"] for a in r["review"]["additions"]] == ["Bristol"]
    assert "Bristol warehouse" not in caplog.text and "db01.example.com" not in caplog.text


def test_api_degrades_without_llm() -> None:
    client = TestClient(create_app(Settings(llm_url=None)))
    assert client.get("/info").json()["optional_detectors"]["llm"] is False
    assert client.post("/suggest/llm", json={"text": TEXT}).json()["available"] is False

    client = TestClient(create_app(Settings(llm_url="http://127.0.0.1:9", llm_timeout=2)))
    assert client.get("/info").json()["optional_detectors"]["llm"] is False
    r = client.post("/suggest/llm", json={"text": TEXT}).json()
    assert r["available"] is False and "unreachable" in r["detail"]


def test_cli_llm_url(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    src = tmp_path / "doc.txt"
    src.write_text(TEXT)
    with fake_llama() as fake:
        fake.responder = lambda req: answer(
            [],
            [
                {
                    "substring": "Bristol",
                    "occurrence": 0,
                    "name": "warehouse_city",
                    "type": "string",
                    "reason": "city",
                }
            ],
        )
        assert cli_main(["suggest", str(src), "--llm-url", fake.url]) == 0
    out = capsys.readouterr().out
    assert "warehouse_city" in out and "llm" in out
    assert cli_main(["suggest", str(src), "--llm-url", "http://127.0.0.1:9"]) == 0
    assert "LLM review skipped" in capsys.readouterr().err


def test_sweeping_fixed_verdicts_are_ignored() -> None:
    """A model that calls (nearly) everything fixed text is not trusted."""
    text = "host: a.example.com\nport: 8443\nimage: nginx:1.27\nreplicas: 3\nregion: eu-west-1\n"
    with fake_llama() as fake:
        fake.responder = lambda req: answer(
            [
                {"id": sid, "fixed_text": True, "name": f"n{sid}"}
                for sid in ids_by_value(req).values()
            ],
            [],
        )
        cands = suggest(text).candidates
        rv = asyncio.run(review_candidates(LlmConfig(url=fake.url), text, cands))
    assert len(cands) >= 4
    assert rv.fixed == []
    assert any("unreliable" in n.message for n in rv.notices)
