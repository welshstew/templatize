"""Optional NER detector: client for the GLiNER service (templatize-ner).

Finds semantic values that regexes cannot: person and company names, locations,
street addresses, phone numbers, money amounts, job titles, products. Standard
library HTTP only. If the service is absent or fails, the detector returns a
``detector_unavailable`` notice and no candidates, so suggestions still work.

GLiNER only fills gaps the other detectors leave:

* it runs on prose (letters, emails, Markdown, HTML, plain text). In config files,
  data and logs it mostly tagged keywords (``User``, ``entryPoints``, ``spring``);
* an entity inside a larger token is dropped (``shop`` of ``shop.example.com``);
* an entity overlapping another detector's candidate is dropped, even an equal one
  (:func:`fill_gaps_only`), so ``Ms Hughes`` never replaces ``Hughes``.

Measured on the samples and corpus (882 labelled values), letting entities compete
in the merge instead lost 44 of them; filling gaps lost none and raised recall on
unseen prose.
"""

from __future__ import annotations

import bisect
import json
import re
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass, field

from templatize.detectors.base import DetectionContext, DetectorResult
from templatize.doctypes import is_prose
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


# Characters that continue a token: an entity next to one is part of a hostname, path,
# address or identifier. A "." or ":" only continues it when a word character follows
# (shop.example.com), not at the end of a sentence (Acme Ltd.).
_TOKEN = re.compile(r"[\w@/\\-]")
_JOINER = re.compile(r"[.:]\w")
_TOKEN_BEFORE = re.compile(r"[\w@/\\.:-]")


def _inside_token(text: str, start: int, end: int) -> bool:
    if start > 0 and _TOKEN_BEFORE.match(text[start - 1]):
        return True
    return end < len(text) and bool(_TOKEN.match(text[end]) or _JOINER.match(text, end))


def entities_to_candidates(text: str, entities: list[dict[str, object]]) -> list[Candidate]:
    out: list[Candidate] = []
    for e in entities:
        try:
            start, end = int(e["start"]), int(e["end"])  # type: ignore[call-overload]
            label = str(e["label"])
            score = float(e["score"])  # type: ignore[arg-type]
        except (KeyError, TypeError, ValueError):
            continue
        if not 0 <= start < end <= len(text) or _inside_token(text, start, end):
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
        if ctx.doc_type is not None and not is_prose(ctx.doc_type.id):
            return DetectorResult()  # configs, data and logs: keywords, not entities
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


def fill_gaps_only(raw: Sequence[Candidate]) -> list[Candidate]:
    """``raw`` with every NER span that overlaps another detector's span removed, equal
    ones included: GLiNER fills gaps and never takes part in a conflict. Its fragments
    and wider boundaries won merges they should not have, and agreeing with one side
    (``Bristol`` found by repetition) tipped the merge against the other (the address
    around it)."""
    others = [s for c in raw if Source.NER not in c.sources for s in c.spans]
    if not others:
        return list(raw)
    starts = sorted(s.start for s in others)
    ends = sorted(s.end for s in others)

    def displaces(s: Span) -> bool:
        # Spans starting before s ends, minus those ending before s starts: the overlaps.
        return bisect.bisect_left(starts, s.end) - bisect.bisect_right(ends, s.start) > 0

    out: list[Candidate] = []
    for c in raw:
        if Source.NER not in c.sources:
            out.append(c)
            continue
        kept = [s for s in c.spans if not displaces(s)]
        if kept:
            out.append(c if len(kept) == len(c.spans) else c.model_copy(update={"spans": kept}))
    return out
