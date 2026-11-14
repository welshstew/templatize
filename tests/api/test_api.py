from __future__ import annotations

import io
import logging
import zipfile
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from templatize_api.app import create_app
from templatize_api.settings import Settings
from tests.conftest import read_fixture, variants_for

SECRET = "zz-top-secret-marker-9137"


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(Settings()), raise_server_exceptions=False)


def specs(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"name": c["name"], "spans": c["spans"], "type": c["type"]} for c in candidates]


def test_healthz_and_info(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}
    info = client.get("/info").json()
    assert info["engines"] == ["jinja2"]
    assert info["limits"]["max_variants"] == 3
    assert info["optional_detectors"] == {"ner": False, "llm": False}


def test_docs_ui_disabled_but_openapi_available(client: TestClient) -> None:
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    paths = client.get("/openapi.json").json()["paths"]
    assert {"/suggest", "/build", "/render", "/download"} <= set(paths)


def test_fixture_flow_via_api(client: TestClient, fixture_path: Path) -> None:
    text = read_fixture(fixture_path)
    sug = client.post("/suggest", json={"text": text, "variants": variants_for(fixture_path)})
    assert sug.status_code == 200
    cands = sug.json()["candidates"]

    built = client.post("/build", json={"text": text, "variables": specs(cands)})
    assert built.status_code == 200
    body = built.json()
    assert body["roundtrip"]["ok"], body["roundtrip"]
    assert body["template"]["path"] == "template.j2"
    assert body["env"]["path"] == ".env"
    assert [f["path"] for f in body["runner"]] == ["render.py"]

    values = {v["name"]: v["value"] for v in body["variables"]}
    rendered = client.post(
        "/render", json={"template": body["template"]["content"], "values": values}
    )
    assert rendered.json() == {"ok": True, "output": text, "error": None}

    dl = client.post("/download", json={"text": text, "variables": specs(cands)})
    assert dl.status_code == 200
    assert dl.headers["content-type"] == "application/zip"
    assert dl.headers["x-roundtrip"] == "pass"
    with zipfile.ZipFile(io.BytesIO(dl.content)) as zf:
        assert sorted(zf.namelist()) == [".env", "README.md", "render.py", "template.j2"]


def test_render_live_preview_with_edited_values(client: TestClient) -> None:
    text = "server db01 port 5432\n"
    built = client.post(
        "/build",
        json={
            "text": text,
            "variables": [
                {"name": "host", "spans": [{"start": 7, "end": 11}]},
                {"name": "port", "spans": [{"start": 17, "end": 21}]},
            ],
        },
    ).json()
    out = client.post(
        "/render",
        json={"template": built["template"]["content"], "values": {"HOST": "db02", "port": "6543"}},
    ).json()
    assert out == {"ok": True, "output": "server db02 port 6543\n", "error": None}


def test_render_errors_are_reported_not_raised(client: TestClient) -> None:
    r = client.post("/render", json={"template": "{{ missing }}", "values": {}})
    assert r.status_code == 200
    assert r.json()["ok"] is False and "undefined" in r.json()["error"]
    r = client.post("/render", json={"template": "{% if %}", "values": {}})
    assert r.json()["ok"] is False
    r = client.post("/render", json={"template": '{{ "a" * 999999999 }}', "values": {}})
    assert r.json()["ok"] is False


def test_render_respects_newline_param(client: TestClient) -> None:
    r = client.post("/render", json={"template": "a\nb\n", "values": {}, "newline": "\r\n"})
    assert r.json()["output"] == "a\r\nb\r\n"


def test_build_validation_errors(client: TestClient) -> None:
    bad_name = client.post(
        "/build",
        json={
            "text": "abc",
            "variables": [{"name": "Bad Name", "spans": [{"start": 0, "end": 1}]}],
        },
    )
    assert bad_name.status_code == 422 and "invalid variable name" in bad_name.json()["detail"]

    overlap = client.post(
        "/build",
        json={
            "text": "abcdef",
            "variables": [
                {"name": "a", "spans": [{"start": 0, "end": 3}]},
                {"name": "b", "spans": [{"start": 2, "end": 4}]},
            ],
        },
    )
    assert overlap.status_code == 422 and "overlapping" in overlap.json()["detail"]

    mismatch = client.post(
        "/build",
        json={
            "text": "abc",
            "variables": [{"name": "a", "value": "zz", "spans": [{"start": 0, "end": 1}]}],
        },
    )
    assert mismatch.status_code == 422 and "does not match" in mismatch.json()["detail"]

    engine = client.post("/build", json={"text": "abc", "variables": [], "engine": "nope"})
    assert engine.status_code == 422 and "unknown engine" in engine.json()["detail"]


def test_too_many_variants_rejected(client: TestClient) -> None:
    r = client.post("/suggest", json={"text": "a", "variants": ["b", "c", "d", "e"]})
    assert r.status_code == 422


def test_request_validation_does_not_echo_input(client: TestClient) -> None:
    r = client.post("/suggest", json={"text": SECRET, "variants": [SECRET] * 4})
    assert r.status_code == 422
    assert SECRET not in r.text


def test_body_limit() -> None:
    client = TestClient(create_app(Settings(max_body_bytes=1000)))
    r = client.post("/suggest", json={"text": "x" * 2000})
    assert r.status_code == 413
    assert client.post("/suggest", json={"text": "small"}).status_code == 200


def test_security_headers(client: TestClient) -> None:
    r = client.post("/suggest", json={"text": "a"})
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-content-type-options"] == "nosniff"


def test_user_text_never_logged(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    payload = f"host: {SECRET}.example.com\n"
    client.post("/suggest", json={"text": payload})
    client.post(
        "/build",
        json={"text": payload, "variables": [{"name": "Bad", "spans": [{"start": 0, "end": 1}]}]},
    )
    client.post("/render", json={"template": "{{ " + SECRET + " }}", "values": {}})
    client.post("/download", json={"text": payload, "variables": []})
    assert SECRET not in caplog.text


def test_unhandled_errors_log_type_only(
    client: TestClient, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_: object, **__: object) -> None:
        raise RuntimeError(f"failure mentioning {SECRET}")

    monkeypatch.setattr("templatize_api.app.suggest", boom)
    caplog.set_level(logging.DEBUG)
    r = client.post("/suggest", json={"text": SECRET})
    assert r.status_code == 500 and r.json() == {"detail": "internal error"}
    assert "RuntimeError" in caplog.text
    assert SECRET not in caplog.text and SECRET not in r.text


def test_build_with_loops_and_render_lists(client: TestClient) -> None:
    text = "hosts:\n- a.example\n- b.example\n"
    a, b = text.index("a.example"), text.index("b.example")
    loop = {
        "name": "hosts",
        "items": [{"start": a, "end": a + 9}, {"start": b, "end": b + 9}],
        "scalar": True,
    }
    built = client.post("/build", json={"text": text, "loops": [loop]}).json()
    assert built["roundtrip"]["ok"] is True
    assert built["env"]["path"] == "values.yaml"
    assert built["loops"][0]["item"] == "host"
    rendered = client.post(
        "/render",
        json={"template": built["template"]["content"], "values": {"hosts": ["x", "y", "z"]}},
    ).json()
    assert rendered == {"ok": True, "output": "hosts:\n- x\n- y\n- z\n", "error": None}
    bad = client.post("/build", json={"text": text, "loops": [{**loop, "name": "for"}]})
    assert bad.status_code == 422 and "reserved" in bad.json()["detail"]


def test_suggest_returns_loops(client: TestClient) -> None:
    text = (
        "INSERT INTO users (email, role) VALUES ('a@x.example', 'admin'), ('b@x.example', 'dev');\n"
    )
    res = client.post("/suggest", json={"text": text}).json()
    (loop,) = res["loops"]
    assert (loop["name"], loop["item"], len(loop["items"])) == ("users", "user", 2)
    assert [f["name"] for f in loop["fields"]] == ["email", "role"]
