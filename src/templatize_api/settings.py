"""Configuration from environment variables (no config files, no network lookups)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from templatize.ir import MAX_INPUT_CHARS, MAX_VARIANTS

# JSON may escape each character as \uXXXX (6 bytes); 4 documents plus overhead.
DEFAULT_MAX_BODY_BYTES = (MAX_VARIANTS + 1) * MAX_INPUT_CHARS * 6 + 1_000_000


# DNS-rebinding protection for /mcp: accepted Host headers. "app:*" is the compose
# service name (used by the offline checker). Add e.g. "myhost.tailnet.ts.net:*" via
# TEMPLATIZE_MCP_ALLOWED_HOSTS when exposing the server beyond localhost.
DEFAULT_MCP_ALLOWED_HOSTS = ("127.0.0.1:*", "localhost:*", "[::1]:*", "app:*")


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _secret(name: str) -> str | None:
    """A secret from NAME or from the file named by NAME_FILE (Docker-secrets style)."""
    path = _opt(f"{name}_FILE")
    if path:
        try:
            value = Path(path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise SystemExit(f"cannot read {name}_FILE ({path}): {exc.strerror}") from None
        return value or None
    return _opt(name)


def _opt(name: str) -> str | None:
    raw = os.environ.get(name, "").strip()
    return raw or None


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 8000
    access_log: bool = False
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES
    static_dir: Path | None = None
    # Optional services (later milestones). Absent/unreachable => features hidden.
    ner_url: str | None = None
    ner_timeout: float = 30.0
    ner_max_chars: int = 200_000
    llm_url: str | None = None
    llm_timeout: float = 300.0
    llm_api_key: str | None = field(default=None, repr=False)
    llm_max_chars: int = 24_000
    # Rate limiting (per client IP). "off" disables a limit.
    rate_limit: str = "300/minute"
    llm_rate_limit: str = "20/hour"
    llm_concurrency: int = 1
    """LLM reviews run at the same time (one GPU/CPU does one review well)."""
    llm_queue: int = 2
    """Reviews allowed to wait for a slot; beyond that callers get 429."""
    trusted_proxies: tuple[str, ...] = ()
    """Reverse proxies whose X-Forwarded-For is trusted for the client IP."""
    # MCP over streamable HTTP at /mcp (needs the `mcp` extra).
    mcp_http: bool = True
    mcp_allowed_hosts: tuple[str, ...] = DEFAULT_MCP_ALLOWED_HOSTS
    extra: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> Settings:
        static = _opt("TEMPLATIZE_STATIC_DIR")
        return cls(
            host=os.environ.get("TEMPLATIZE_HOST", "127.0.0.1"),
            port=int(os.environ.get("TEMPLATIZE_PORT", "8000")),
            access_log=_bool("TEMPLATIZE_ACCESS_LOG", False),
            max_body_bytes=int(
                os.environ.get("TEMPLATIZE_MAX_BODY_BYTES", str(DEFAULT_MAX_BODY_BYTES))
            ),
            static_dir=Path(static) if static else None,
            ner_url=_opt("TEMPLATIZE_NER_URL"),
            ner_timeout=float(os.environ.get("TEMPLATIZE_NER_TIMEOUT", "30")),
            ner_max_chars=int(os.environ.get("TEMPLATIZE_NER_MAX_CHARS", "200000")),
            llm_url=_opt("TEMPLATIZE_LLM_URL"),
            llm_timeout=float(os.environ.get("TEMPLATIZE_LLM_TIMEOUT", "300")),
            llm_api_key=_secret("TEMPLATIZE_LLM_API_KEY"),
            llm_max_chars=int(os.environ.get("TEMPLATIZE_LLM_MAX_CHARS", "24000")),
            rate_limit=os.environ.get("TEMPLATIZE_RATE_LIMIT", "300/minute"),
            llm_rate_limit=os.environ.get("TEMPLATIZE_LLM_RATE_LIMIT", "20/hour"),
            llm_concurrency=int(os.environ.get("TEMPLATIZE_LLM_CONCURRENCY", "1")),
            llm_queue=int(os.environ.get("TEMPLATIZE_LLM_QUEUE", "2")),
            trusted_proxies=tuple(
                p.strip()
                for p in os.environ.get("TEMPLATIZE_TRUSTED_PROXIES", "").split(",")
                if p.strip()
            ),
            mcp_http=_bool("TEMPLATIZE_MCP_HTTP", True),
            mcp_allowed_hosts=DEFAULT_MCP_ALLOWED_HOSTS
            + tuple(
                h.strip()
                for h in os.environ.get("TEMPLATIZE_MCP_ALLOWED_HOSTS", "").split(",")
                if h.strip()
            ),
        )
