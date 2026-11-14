"""MCP server tests through the official SDK client: in-process, stdio subprocess, and
streamable HTTP served by the API app under a real uvicorn server."""

from __future__ import annotations

import logging
import socket
import sys
import threading
import time
from collections.abc import Iterator
from typing import Any

import anyio
import httpx
import pytest
import uvicorn
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from templatize_api.app import create_app
from templatize_api.settings import Settings
from templatize_mcp import tools
from templatize_mcp.server import create_server
from tests.conftest import FIXTURES, read_fixture

SECRET = "zz-mcp-secret-4711"
TEXT = "host: db01.example.com\nbackup: db01.example.com\nport: 5432\n"


def run(coro_fn: Any) -> Any:
    return anyio.run(coro_fn)


async def _call(client: Client, name: str, args: dict[str, Any]) -> Any:
    res = await client.call_tool(name, args)
    return res


def test_lists_six_read_only_tools() -> None:
    async def main() -> None:
        async with Client(create_server()) as c:
            listed = (await c.list_tools()).tools
            assert sorted(t.name for t in listed) == [
                "generate_ansible_role",
                "generate_helm_chart",
                "generate_template",
                "render_template",
                "suggest_parameters",
                "validate_template",
            ]
            for t in listed:
                assert t.annotations is not None and t.annotations.read_only_hint is True
                assert t.annotations.open_world_hint is False
                assert t.output_schema is not None

    run(main)


def test_suggest_then_generate_by_value() -> None:
    async def main() -> None:
        async with Client(create_server()) as c:
            sug = await c.call_tool("suggest_parameters", {"text": TEXT})
            assert not sug.is_error
            names = {cand["value"] for cand in sug.structured_content["candidates"]}
            assert "db01.example.com" in names

            gen = await c.call_tool(
                "generate_template",
                {"text": TEXT, "variables": [{"name": "db_host", "value": "db01.example.com"}]},
            )
            out = gen.structured_content
            assert not gen.is_error and out["roundtrip"]["ok"]
            assert out["template"].count("{{ db_host }}") == 2
            assert 'DB_HOST="db01.example.com"' in out["env"]
            assert "dotenv_values" in out["render_script"]
            assert out["variables"] == [
                {
                    "name": "db_host",
                    "env_key": "DB_HOST",
                    "type": "hostname",
                    "value": "db01.example.com",
                    "occurrences": 2,
                }
            ]

    run(main)


def test_generate_auto_accepts_suggestions_for_every_fixture() -> None:
    async def main() -> None:
        async with Client(create_server()) as c:
            for path in sorted(p for p in FIXTURES.iterdir() if p.is_file()):
                gen = await c.call_tool("generate_template", {"text": read_fixture(path)})
                assert not gen.is_error, path.name
                out = gen.structured_content
                assert out["used_suggestions"] is True
                assert out["roundtrip"]["ok"], (path.name, out["roundtrip"])

    run(main)


def test_render_and_validate() -> None:
    async def main() -> None:
        async with Client(create_server()) as c:
            r = await c.call_tool(
                "render_template", {"template": "a={{ x }}\n", "values": {"X": "1"}}
            )
            assert r.structured_content == {"ok": True, "output": "a=1\n", "error": None}
            r = await c.call_tool("render_template", {"template": "{{ missing }}"})
            assert r.structured_content["ok"] is False and not r.is_error
            r = await c.call_tool("render_template", {"template": "a\nb", "newline": "\r\n"})
            assert r.structured_content["output"] == "a\r\nb"
            r = await c.call_tool(
                "render_template",
                {
                    "template": "{% for u in users %}{{ u.name }};{% endfor %}{{ n }}",
                    "values": {"users": [{"name": "a"}, {"name": "b"}], "n": "1"},
                },
            )
            assert r.structured_content["output"] == "a;b;1"

            v = await c.call_tool(
                "validate_template",
                {"template": "a={{ x }}\n", "values": {"x": "1"}, "expected_text": "a=1\n"},
            )
            assert v.structured_content["ok"] is True and v.structured_content["matches_expected"]
            v = await c.call_tool(
                "validate_template", {"template": "{{ x }} {{ y }}", "values": {"x": "1"}}
            )
            assert (
                v.structured_content["missing_values"] == ["y"]
                and v.structured_content["ok"] is False
            )

    run(main)


def test_input_errors_are_reported_to_client() -> None:
    async def main() -> None:
        async with Client(create_server()) as c:
            bad = await c.call_tool(
                "generate_template", {"text": "abc", "variables": [{"name": "v", "value": "nope"}]}
            )
            assert bad.is_error and "does not occur in the text" in bad.content[0].text
            bad = await c.call_tool(
                "generate_template", {"text": "abc", "variables": [{"name": "Bad", "value": "abc"}]}
            )
            assert bad.is_error and "invalid variable name" in bad.content[0].text

    run(main)


def test_tool_failures_never_log_user_text(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    caplog.set_level(logging.DEBUG)

    def crash(*_: object, **__: object) -> None:
        raise RuntimeError(f"crash quoting {SECRET}")

    async def main() -> None:
        async with Client(create_server()) as c:
            await c.call_tool(
                "generate_template",
                {"text": SECRET, "variables": [{"name": "v", "value": f"x{SECRET}"}]},
            )
            await c.call_tool(
                "render_template", {"template": "{{ " + SECRET.replace("-", "_") + " }}"}
            )
            monkeypatch.setattr(tools, "suggest_parameters", crash)
            res = await c.call_tool("suggest_parameters", {"text": SECRET})
            assert res.is_error and "RuntimeError" in res.content[0].text
            assert SECRET not in res.content[0].text

    run(main)
    assert "RuntimeError" in caplog.text
    assert SECRET not in caplog.text
    assert SECRET.replace("-", "_") not in caplog.text


def test_stdio_transport() -> None:
    params = StdioServerParameters(command=sys.executable, args=["-m", "templatize_mcp.server"])

    async def main() -> None:
        async with Client(params) as c:
            assert len((await c.list_tools()).tools) == 6
            gen = await c.call_tool(
                "generate_template",
                {"text": TEXT, "variables": [{"name": "port", "value": "5432"}]},
            )
            assert gen.structured_content["roundtrip"]["ok"]

    run(main)


# --- streamable HTTP via the API app -------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


@pytest.fixture(scope="module")
def http_server() -> Iterator[str]:
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(create_app(Settings()), host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


def test_streamable_http_on_api_app(http_server: str) -> None:
    assert httpx.get(f"{http_server}/info").json()["mcp_http"] == "/mcp"

    async def main() -> None:
        async with Client(f"{http_server}/mcp") as c:
            assert len((await c.list_tools()).tools) == 6
            gen = await c.call_tool(
                "generate_template",
                {"text": TEXT, "variables": [{"name": "db_host", "value": "db01.example.com"}]},
            )
            assert gen.structured_content["roundtrip"]["ok"]

    run(main)
    # The rest of the API is unaffected.
    assert httpx.post(f"{http_server}/suggest", json={"text": TEXT}).status_code == 200


def test_http_rejects_foreign_host_header(http_server: str) -> None:
    """DNS-rebinding protection: a request whose Host is not allow-listed is refused."""
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    headers = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
    evil = httpx.post(
        f"{http_server}/mcp", json=body, headers={**headers, "host": "evil.example:80"}
    )
    assert evil.status_code in (400, 403, 421)
    origin = httpx.post(
        f"{http_server}/mcp", json=body, headers={**headers, "origin": "http://evil.example"}
    )
    assert origin.status_code in (400, 403, 421)


def test_mcp_http_can_be_disabled() -> None:
    from fastapi.testclient import TestClient

    client = TestClient(create_app(Settings(mcp_http=False)))
    assert client.get("/info").json()["mcp_http"] is None
    assert client.post("/mcp", json={}).status_code in (404, 405)


def test_offline_checker_mcp_step(http_server: str) -> None:
    from templatize_api.offline_check import check_mcp

    assert check_mcp(http_server, FIXTURES / "nginx.conf") is None


def test_tools_suggest_and_generate_loops() -> None:
    text = 'BROKERS = ["10.0.0.1:9092", "10.0.0.2:9092"]\n'
    sug = tools.suggest_parameters(text)
    (loop,) = sug.loops
    assert loop.name == "brokers" and loop.values == ["10.0.0.1:9092", "10.0.0.2:9092"]
    gen = tools.generate_template(text)
    assert gen.roundtrip.ok and gen.values_filename == "values.yaml"
    assert "{% for broker in brokers %}" in gen.template


def test_tools_generate_ansible_role() -> None:
    text = 'BROKERS = ["10.0.0.1:9092", "10.0.0.2:9092"]\nREGION = "eu-west-2"\n'
    res = tools.generate_ansible_role(text, role_name="kafka", filename="settings.py")
    assert res.roundtrip.ok
    paths = {f.path for f in res.files}
    assert {"kafka/templates/settings.py.j2", "kafka/defaults/main.yml", "playbook.yml"} <= paths
    defaults = next(f.content for f in res.files if f.path == "kafka/defaults/main.yml")
    assert "kafka_brokers:\n- 10.0.0.1:9092\n" in defaults
