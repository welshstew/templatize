from __future__ import annotations

import socket
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from templatize_api import healthcheck, offline_check
from templatize_api.app import create_app
from templatize_api.settings import Settings
from tests.conftest import FIXTURES


class _FakeConn:
    def __enter__(self) -> _FakeConn:
        return self

    def __exit__(self, *_: object) -> None:
        return None


def test_isolation_check_fails_when_network_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: _FakeConn())
    ok, detail = offline_check.network_is_isolated()
    assert not ok and "succeeded" in detail


def test_isolation_check_passes_when_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_: object, **__: object) -> None:
        raise OSError("network unreachable")

    monkeypatch.setattr(socket, "create_connection", refuse)
    assert offline_check.network_is_isolated()[0]


def test_check_fixture_against_app(monkeypatch: pytest.MonkeyPatch) -> None:
    client = TestClient(create_app(Settings()))

    def post(_: str, path: str, payload: dict[str, Any]) -> tuple[int, bytes, dict[str, str]]:
        r = client.post(path, json=payload)
        return r.status_code, r.content, dict(r.headers)

    monkeypatch.setattr(offline_check, "_post", post)
    for path in sorted(p for p in FIXTURES.iterdir() if p.is_file()):
        result = offline_check.check_fixture("http://test", path)
        assert result is None or result.startswith("ok:"), (path.name, result)


def test_check_fixture_reports_roundtrip_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "doc.txt").write_text("host: a.example.com\n")

    def post(_: str, path: str, payload: dict[str, Any]) -> tuple[int, bytes, dict[str, str]]:
        if path == "/suggest":
            return (
                200,
                b'{"candidates": [{"name": "h", "spans": [{"start": 6, "end": 19}], "type": "string"}]}',
                {},
            )
        return 200, b'{"roundtrip": {"ok": false}}', {}

    monkeypatch.setattr(offline_check, "_post", post)
    err = offline_check.check_fixture("http://test", tmp_path / "doc.txt")
    assert err is not None and "round-trip failed" in err


def test_healthcheck_exit_code_when_down(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEMPLATIZE_PORT", "1")  # nothing listens there
    assert healthcheck.main() == 1


def test_check_ui(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import io
    import urllib.request

    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text('<script type="module" src="/assets/a.js"></script>')
    js = tmp_path / "assets" / "a.js"
    client = TestClient(create_app(Settings(static_dir=tmp_path)))

    class Resp(io.BytesIO):
        def __init__(self, r: Any) -> None:
            super().__init__(r.content)
            self.headers = r.headers
            self.status = r.status_code

        def __enter__(self) -> Resp:
            return self

    def urlopen(url: str, timeout: float = 0) -> Resp:
        return Resp(client.get(url.removeprefix("http://test")))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    js.write_text("console.log(1)")
    assert offline_check.check_ui("http://test") is None
    # Sample documents mention RFC 2606 domains as text: allowed.
    js.write_text('const sample = "url: https://app.example.com/x";')
    assert offline_check.check_ui("http://test") is None
    js.write_text('fetch("https://telemetry.acme.io/x")')
    err = offline_check.check_ui("http://test")
    assert err is not None and "telemetry.acme.io" in err
