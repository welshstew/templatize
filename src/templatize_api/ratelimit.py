"""Per-client rate limiting (pure ASGI, in memory, nothing logged).

Token buckets keyed by (rule, client IP):

* ``llm``  POST /suggest/llm, expensive (a model run): default 20/hour
* ``api``  every other API call incl. /mcp: default 300/minute
* exempt: static UI files, /healthz, /info, /openapi.json

Behind a reverse proxy every request comes from the proxy's address, so the client
IP is taken from ``X-Forwarded-For``, but only when the direct peer is a trusted
proxy (``TEMPLATIZE_TRUSTED_PROXIES``); otherwise the header is ignored so clients
cannot spoof their way around the limit. Over-limit requests get ``429`` with
``Retry-After``. State is per process and bounded (least-recently-seen clients are
evicted).
"""

from __future__ import annotations

import ipaddress
import json
import math
import re
import time
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass

from starlette.types import ASGIApp, Receive, Scope, Send

_UNITS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}
_RATE_RE = re.compile(r"^\s*(\d+)\s*/\s*(second|minute|hour|day)s?\s*$")
MAX_TRACKED_CLIENTS = 10_000

API_PATHS = frozenset({"/suggest", "/build", "/render", "/download"})
LLM_PATHS = frozenset({"/suggest/llm"})


@dataclass(frozen=True)
class Rate:
    count: int
    per_seconds: float

    @classmethod
    def parse(cls, spec: str) -> Rate | None:
        """``"300/minute"`` -> Rate; ``"0"``, ``"off"`` or empty -> None (no limit)."""
        if spec.strip().lower() in {"", "0", "off", "none", "unlimited"}:
            return None
        m = _RATE_RE.match(spec.lower())
        if not m or int(m.group(1)) <= 0:
            raise ValueError(f"invalid rate {spec!r}; use e.g. '300/minute' or 'off'")
        return cls(int(m.group(1)), float(_UNITS[m.group(2)]))

    @property
    def refill_per_second(self) -> float:
        return self.count / self.per_seconds


class TokenBuckets:
    def __init__(self, rate: Rate, max_keys: int = MAX_TRACKED_CLIENTS) -> None:
        self.rate = rate
        self.max_keys = max_keys
        self._buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()

    def take(self, key: str, now: float | None = None) -> float:
        """Consume one token. Returns 0 if allowed, else seconds until one is available."""
        now = time.monotonic() if now is None else now
        tokens, last = self._buckets.pop(key, (float(self.rate.count), now))
        tokens = min(float(self.rate.count), tokens + (now - last) * self.rate.refill_per_second)
        if tokens >= 1:
            self._buckets[key] = (tokens - 1, now)
            wait = 0.0
        else:
            self._buckets[key] = (tokens, now)
            wait = (1 - tokens) / self.rate.refill_per_second
        while len(self._buckets) > self.max_keys:
            self._buckets.popitem(last=False)
        return wait


Network = ipaddress.IPv4Network | ipaddress.IPv6Network


def parse_networks(specs: Iterable[str]) -> tuple[Network, ...]:
    return tuple(ipaddress.ip_network(s.strip(), strict=False) for s in specs if s.strip())


def _in(ip: str, nets: tuple[Network, ...]) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in n for n in nets)


def client_ip(scope: Scope, trusted: tuple[Network, ...]) -> str:
    peer = (scope.get("client") or ("unknown", 0))[0]
    if not trusted or not _in(peer, trusted):
        return str(peer)
    forwarded = ""
    for name, value in scope.get("headers", []):
        if name == b"x-forwarded-for":
            forwarded = value.decode("latin-1")
    # Walk from the right: the first address that is not a trusted proxy is the client.
    for hop in reversed([h.strip() for h in forwarded.split(",") if h.strip()]):
        if not _in(hop, trusted):
            return hop
    return str(peer)


def rule_for(path: str) -> str | None:
    if path in LLM_PATHS:
        return "llm"
    if path in API_PATHS or path == "/mcp" or path.startswith("/mcp/"):
        return "api"
    return None


class RateLimitMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        api: Rate | None,
        llm: Rate | None,
        trusted_proxies: tuple[Network, ...] = (),
    ) -> None:
        self.app = app
        self.trusted = trusted_proxies
        self.buckets: dict[str, TokenBuckets] = {}
        if api:
            self.buckets["api"] = TokenBuckets(api)
        if llm:
            self.buckets["llm"] = TokenBuckets(llm)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            rule = rule_for(scope.get("path", ""))
            buckets = self.buckets.get(rule) if rule else None
            if buckets is not None:
                wait = buckets.take(client_ip(scope, self.trusted))
                if wait > 0:
                    await self._reject(send, rule or "api", wait)
                    return
        await self.app(scope, receive, send)

    @staticmethod
    async def _reject(send: Send, rule: str, wait: float) -> None:
        retry = max(1, math.ceil(wait))
        what = "LLM reviews" if rule == "llm" else "requests"
        body = json.dumps(
            {"detail": f"rate limit exceeded for {what}; try again in {retry} s"}
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 429,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"retry-after", str(retry).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
