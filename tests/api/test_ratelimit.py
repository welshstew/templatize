from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from templatize_api.app import create_app
from templatize_api.ratelimit import Rate, TokenBuckets, client_ip, parse_networks
from templatize_api.settings import Settings
from tests.llm.fake_llama import fake_llama


def test_rate_parse() -> None:
    assert Rate.parse("300/minute") == Rate(300, 60)
    assert Rate.parse("20 / hours") == Rate(20, 3600)
    for off in ("off", "0", "", "none"):
        assert Rate.parse(off) is None
    for bad in ("fast", "10/week", "-1/minute", "0/minute"):
        with pytest.raises(ValueError):
            Rate.parse(bad)


def test_bucket_allows_burst_then_refills() -> None:
    b = TokenBuckets(Rate(3, 60))
    assert [b.take("a", now=0) for _ in range(3)] == [0, 0, 0]
    wait = b.take("a", now=0)
    assert wait == pytest.approx(20)  # one token per 20 s
    assert b.take("b", now=0) == 0  # other clients unaffected
    assert b.take("a", now=21) == 0  # refilled


def test_bucket_memory_is_bounded() -> None:
    b = TokenBuckets(Rate(1, 60), max_keys=100)
    for i in range(1000):
        b.take(f"ip{i}", now=0)
    assert len(b._buckets) == 100


def test_client_ip_trusts_forwarded_for_only_from_trusted_proxies() -> None:
    trusted = parse_networks(["10.0.0.0/8"])
    scope = {
        "client": ("10.0.0.2", 1),
        "headers": [(b"x-forwarded-for", b"6.6.6.6, 203.0.113.9, 10.0.0.7")],
    }
    assert client_ip(scope, trusted) == "203.0.113.9"  # rightmost untrusted hop
    direct = {"client": ("198.51.100.4", 1), "headers": [(b"x-forwarded-for", b"1.2.3.4")]}
    assert client_ip(direct, trusted) == "198.51.100.4"  # spoofed header ignored
    assert client_ip(direct, ()) == "198.51.100.4"
    only_proxies = {"client": ("10.0.0.2", 1), "headers": [(b"x-forwarded-for", b"10.0.0.9")]}
    assert client_ip(only_proxies, trusted) == "10.0.0.2"


def test_api_limit_returns_429_with_retry_after() -> None:
    client = TestClient(create_app(Settings(rate_limit="3/minute")))
    codes = [client.post("/suggest", json={"text": "a"}).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]
    r = client.post("/render", json={"template": "x"})  # same "api" bucket
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) >= 1
    assert "rate limit" in r.json()["detail"]
    assert "default-src 'self'" in r.headers["content-security-policy"]
    # health, info and the UI are never limited
    assert all(client.get(p).status_code == 200 for p in ["/healthz", "/info", "/healthz"])


def test_limits_are_per_client_behind_trusted_proxy() -> None:
    app = create_app(Settings(rate_limit="2/minute", trusted_proxies=("127.0.0.0/8",)))
    proxy = TestClient(app, client=("127.0.0.1", 5000))

    def call(ip: str) -> int:
        return proxy.post(
            "/suggest", json={"text": "a"}, headers={"x-forwarded-for": ip}
        ).status_code

    assert [call("203.0.113.1") for _ in range(3)] == [200, 200, 429]
    assert call("203.0.113.2") == 200  # a different client has its own bucket


def test_limits_can_be_disabled() -> None:
    client = TestClient(create_app(Settings(rate_limit="off")))
    assert all(client.post("/suggest", json={"text": "a"}).status_code == 200 for _ in range(50))


def test_llm_rate_limit() -> None:
    with fake_llama() as fake:
        client = TestClient(create_app(Settings(llm_url=fake.url, llm_rate_limit="2/hour")))
        codes = [
            client.post("/suggest/llm", json={"text": "host: a.example.com\n"}).status_code
            for _ in range(3)
        ]
    assert codes == [200, 200, 429]


# --- LLM concurrency (real server, concurrent requests) --------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


@pytest.fixture
def slow_llm_app() -> Iterator[str]:
    with fake_llama() as fake:

        def slow(_req: dict[str, Any]) -> str:
            time.sleep(1.5)
            return '{"candidates": [], "missed": []}'

        fake.responder = slow
        settings = Settings(llm_url=fake.url, llm_concurrency=1, llm_queue=1, llm_rate_limit="off")
        port = _free_port()
        server = uvicorn.Server(
            uvicorn.Config(create_app(settings), host="127.0.0.1", port=port, log_level="warning")
        )
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        while not server.started:
            time.sleep(0.05)
        yield f"http://127.0.0.1:{port}"
        server.should_exit = True
        thread.join(timeout=10)


def test_llm_concurrency_cap_and_queue(slow_llm_app: str) -> None:
    def review(_: int) -> int:
        return httpx.post(
            f"{slow_llm_app}/suggest/llm", json={"text": "host: a.example.com\n"}, timeout=30
        ).status_code

    with ThreadPoolExecutor(max_workers=4) as pool:
        codes = sorted(pool.map(review, range(4)))
    # 1 running + 1 queued succeed; the rest are told the LLM is busy
    assert codes == [200, 200, 429, 429]
