"""Configuration from environment variables (no config files, no network lookups)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from templatize.ir import MAX_INPUT_CHARS, MAX_VARIANTS

# JSON may escape each character as \uXXXX (6 bytes); 4 documents plus overhead.
DEFAULT_MAX_BODY_BYTES = (MAX_VARIANTS + 1) * MAX_INPUT_CHARS * 6 + 1_000_000


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


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
    llm_url: str | None = None
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
            llm_url=_opt("TEMPLATIZE_LLM_URL"),
        )
