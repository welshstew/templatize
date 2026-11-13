"""Optional NER detector: client for the GLiNER service (templatize-ner).

Finds semantic values that regexes cannot: person and company names, locations,
street addresses, phone numbers, money amounts, job titles, products. Standard
library HTTP only. If the service is absent or fails, the detector returns a
``detector_unavailable`` notice and no candidates, so suggestions still work.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from templatize.detectors.base import DetectionContext, DetectorResult
from templatize.ir import Candidate, Notice, Source, Span, VarType

# label -> (variable name, type)
LABELS: dict[str, tuple[str, VarType]] = {
    "person": ("person_name", VarType.STRING),
    "organization": ("organization", VarType.STRING),
    "location": ("location", VarType.STRING),
    "street address": ("street_address", VarType.STRING),
    "phone number": ("phone_number", VarType.STRING),
    "money": ("amount", VarType.STRING),
    "job title": ("job_title", VarType.STRING),
    "product": ("product", VarType.STRING),
}


@dataclass(frozen=True)
class NerConfig:
    url: str
    timeout: float = 30.0
    max_chars: int = 200_000
    threshold: float = 0.5
    labels: tuple[str, ...] = field(default_factory=lambda: tuple(LABELS))


def check_health(url: str, timeout: float = 1.5) -> bool:
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/health", timeout=timeout) as r:
            return bool(r.status == 200 and json.loads(r.read()).get("status") == "ok")
    except (OSError, ValueError):
        return False


class NerUnavailable(Exception):
    pass


def fetch_entities(config: NerConfig, text: str) -> list[dict[str, object]]:
    body = json.dumps(
        {"text": text, "labels": list(config.labels), "threshold": config.threshold}
    ).encode()
    req = urllib.request.Request(
        config.url.rstrip("/") + "/entities",
        data=body,
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=config.timeout) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as exc:
        exc.close()  # the error carries the open response; release the socket
        raise NerUnavailable(f"NER service returned HTTP {exc.code}") from None
    except (OSError, ValueError) as exc:
        raise NerUnavailable(f"NER service unreachable ({type(exc).__name__})") from None
    ents = data.get("entities") if isinstance(data, dict) else None
    if not isinstance(ents, list):
        raise NerUnavailable("unexpected response from NER service")
    return ents


def entities_to_candidates(text: str, entities: list[dict[str, object]]) -> list[Candidate]:
    out: list[Candidate] = []
    for e in entities:
        try:
            start, end = int(e["start"]), int(e["end"])  # type: ignore[call-overload]
            label = str(e["label"])
            score = float(e["score"])  # type: ignore[arg-type]
        except (KeyError, TypeError, ValueError):
            continue
        if not 0 <= start < end <= len(text):
            continue
        value = text[start:end]
        name, vtype = LABELS.get(label, (label.replace(" ", "_"), VarType.STRING))
        out.append(
            Candidate(
                name=name,
                name_origin="type",
                type=vtype,
                value=value,
                spans=[Span(start=start, end=end)],
                sources=[Source.NER],
                detectors=[f"ner.{label.replace(' ', '_')}"],
                confidence=round(min(0.9, 0.45 + 0.4 * score), 3),
                reason=f"{label} (NER {score:.2f})",
            )
        )
    return out


class NerDetector:
    """Synchronous detector (the service answers in milliseconds)."""

    name = "ner"

    def __init__(self, config: NerConfig) -> None:
        self.config = config

    def detect(self, ctx: DetectionContext) -> DetectorResult:
        if len(ctx.text) > self.config.max_chars:
            return DetectorResult(
                notices=[
                    Notice(
                        kind="detector_unavailable",
                        message=f"document longer than {self.config.max_chars} characters; NER skipped",
                    )
                ]
            )
        try:
            ents = fetch_entities(self.config, ctx.text)
        except NerUnavailable as exc:
            return DetectorResult(
                notices=[Notice(kind="detector_unavailable", message=f"NER: {exc}")]
            )
        return DetectorResult(candidates=entities_to_candidates(ctx.text, ents))
