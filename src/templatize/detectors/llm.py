"""Optional LLM detector backed by a llama.cpp server (OpenAI-compatible API).

The model never writes the template. It returns JSON only, constrained by a JSON
schema (llama.cpp turns ``response_format.json_schema`` into a grammar, so the output
always parses). In priority order it:

1. names the heuristic candidates (referenced by short ids such as ``c3``),
2. classifies each as a real variable or fixed text,
3. reports variables the heuristics missed as ``{substring, occurrence, name, type,
   reason}``. Character offsets are never requested; this code locates each substring
   and discards any that do not occur in the input.

Uses only the standard library (HTTP via urllib in a worker thread), so the core
package keeps no web dependencies. Nothing is logged; the document is only sent to
the configured llama.cpp server, which runs locally.
"""

from __future__ import annotations

import asyncio
import json
import re
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from templatize.detectors.base import DetectionContext, DetectorResult
from templatize.ir import (
    NAME_ORIGIN_RANK,
    RESERVED_NAMES,
    Candidate,
    Notice,
    Source,
    Span,
    VarType,
    candidate_id,
)
from templatize.merge import merge_candidates
from templatize.naming import infer_type, snake_case

NAME_PATTERN = r"^[a-z][a-z0-9_]{0,39}$"
MAX_CANDIDATES_SENT = 80
MAX_MISSED = 20
CONTEXT_CHARS = 60
ADDITION_CONFIDENCE = 0.6
FIXED_PENALTY = 0.5
MAX_FIXED_SHARE = 0.5


class LlmUnavailable(Exception):
    """The llama.cpp server is not configured, unreachable, still loading, or failed."""


@dataclass(frozen=True)
class LlmConfig:
    url: str
    timeout: float = 300.0
    max_chars: int = 24_000
    """Documents longer than this are not sent (the model's context is limited)."""
    max_tokens: int = 2048
    api_key: str | None = None
    """Bearer token for a llama-server started with --api-key / --api-key-file."""
    temperature: float = 0.0


class Rename(BaseModel):
    candidate_id: str
    name: str
    reason: str | None = None


class FixedText(BaseModel):
    candidate_id: str
    reason: str | None = None


class LlmReview(BaseModel):
    renames: list[Rename] = Field(default_factory=list)
    fixed: list[FixedText] = Field(default_factory=list)
    """Heuristic candidates the model thinks are fixed text, not parameters."""
    additions: list[Candidate] = Field(default_factory=list)
    """Variables the heuristics missed, located in the text."""
    discarded: int = 0
    """Model suggestions dropped because the substring/occurrence does not exist."""
    notices: list[Notice] = Field(default_factory=list)


# --- model I/O -------------------------------------------------------------------


class _CandidateVerdict(BaseModel):
    id: str
    name: str
    fixed_text: bool = False
    reason: str | None = None


class _Missed(BaseModel):
    substring: str
    occurrence: int = 0
    name: str
    type: str = "string"
    reason: str | None = None


class _ModelOutput(BaseModel):
    candidates: list[_CandidateVerdict] = Field(default_factory=list)
    missed: list[_Missed] = Field(default_factory=list)


def response_schema(n_candidates: int) -> dict[str, Any]:
    name = {"type": "string", "pattern": NAME_PATTERN}
    reason = {"type": "string", "maxLength": 100}
    return {
        "type": "object",
        "properties": {
            "candidates": {
                "type": "array",
                "maxItems": n_candidates,
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "name": name,
                        "fixed_text": {"type": "boolean"},
                        "reason": reason,
                    },
                    "required": ["id", "name", "fixed_text"],
                },
            },
            "missed": {
                "type": "array",
                "maxItems": MAX_MISSED,
                "items": {
                    "type": "object",
                    "properties": {
                        "substring": {"type": "string", "minLength": 1, "maxLength": 200},
                        "occurrence": {"type": "integer", "minimum": 0},
                        "name": name,
                        "type": {"type": "string", "enum": [t.value for t in VarType]},
                        "reason": reason,
                    },
                    "required": ["substring", "occurrence", "name", "type", "reason"],
                },
            },
        },
        "required": ["candidates", "missed"],
    }


SYSTEM_PROMPT = """\
You help turn a document into a reusable template. A parameter is a value that would \
change between deployments, recipients or instances. Fixed text is structure, keywords \
and wording that stays the same in every copy.

You get the document and a list of candidate parameters found by simple heuristics, \
each with an id, its value and the line it is on. Answer with compact JSON only, on \
a single line without indentation; omit "reason" unless it adds information.

Task 1, "candidates": one entry per candidate id. "name" is a short, descriptive \
snake_case variable name (e.g. db_host, recipient_name, invoice_date, upstream_port), \
unique across the answer. "fixed_text" is true ONLY for values that belong to the \
document's format and would be identical in every copy of it (e.g. apiVersion, kind, \
protocol keywords, the sender's own letterhead); hosts, names, images, sizes, ports, \
counts, dates and ids are parameters, so "fixed_text" is false. When unsure, use false.

Task 2, "missed": the heuristics only recognise things like IP addresses, dates, \
emails, URLs and key: value pairs, so they usually MISS parameters such as person \
names, company or product names, order/invoice/reference numbers, amounts of money, \
quantities, phone numbers, addresses and job titles. Read the whole document and list \
every such parameter that is not already a candidate. "substring" must be copied \
exactly from the document (the shortest text that is the value, without surrounding \
words or punctuation); "occurrence" is which occurrence of that exact substring you \
mean, counting from 0. Never invent text that is not in the document."""


def _line_context(text: str, span: Span) -> str:
    start = max(text.rfind("\n", 0, span.start) + 1, span.start - CONTEXT_CHARS)
    end_nl = text.find("\n", span.end)
    end = min(end_nl if end_nl != -1 else len(text), span.end + CONTEXT_CHARS)
    return text[start:end].strip()


def build_messages(
    text: str, candidates: Sequence[Candidate], variants: Sequence[str] = ()
) -> tuple[list[dict[str, str]], dict[str, Candidate]]:
    short: dict[str, Candidate] = {}
    lines = []
    for i, c in enumerate(candidates[:MAX_CANDIDATES_SENT], start=1):
        sid = f"c{i}"
        short[sid] = c
        entry = {
            "id": sid,
            "value": c.value,
            "type": c.type.value,
            "current_name": c.name,
            "occurrences": len(c.spans),
            "line": _line_context(text, c.spans[0]),
        }
        if c.variant_values:
            entry["values_in_other_examples"] = c.variant_values  # type: ignore[assignment]
        lines.append(json.dumps(entry, ensure_ascii=False))
    variant_note = (
        f"\n\n({len(variants)} other example(s) of this document were provided; candidates "
        "with values_in_other_examples differ between them and are very likely parameters.)"
        if variants
        else ""
    )
    user = (
        f"DOCUMENT:\n<<<\n{text}\n>>>\n\nCANDIDATES (one JSON object per line):\n"
        + ("\n".join(lines) if lines else "(none)")
        + variant_note
    )
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}], short


def _http_json(
    url: str, payload: dict[str, Any] | None, timeout: float, api_key: str | None = None
) -> Any:
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"content-type": "application/json"} if data else {}
    if api_key:
        headers["authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        exc.close()  # the error carries the open response; release the socket
        if exc.code == 401:
            raise LlmUnavailable("llama.cpp server rejected the API key (HTTP 401)") from None
        raise LlmUnavailable(f"llama.cpp server returned HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise LlmUnavailable(f"llama.cpp server unreachable ({type(reason).__name__})") from None
    except json.JSONDecodeError:
        raise LlmUnavailable("llama.cpp server sent invalid JSON") from None


def check_health(url: str, timeout: float = 2.0, api_key: str | None = None) -> bool:
    """True when the server is up and the model is loaded (GET /health -> 200)."""
    try:
        res = _http_json(url.rstrip("/") + "/health", None, timeout, api_key)
    except LlmUnavailable:
        return False
    return isinstance(res, dict) and res.get("status") == "ok"


def call_model(config: LlmConfig, messages: list[dict[str, str]], schema: dict[str, Any]) -> str:
    payload = {
        "messages": messages,
        "temperature": config.temperature,
        "max_tokens": config.max_tokens,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "template_review", "strict": True, "schema": schema},
        },
        "cache_prompt": True,
    }
    res = _http_json(
        config.url.rstrip("/") + "/v1/chat/completions", payload, config.timeout, config.api_key
    )
    try:
        content = res["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise LlmUnavailable("unexpected response shape from llama.cpp server") from None
    if not isinstance(content, str):
        raise LlmUnavailable("unexpected response shape from llama.cpp server")
    return content


# --- turning model output into a review ------------------------------------------


def _occurrences(text: str, sub: str) -> list[int]:
    out = []
    pos = 0
    while (idx := text.find(sub, pos)) != -1:
        out.append(idx)
        pos = idx + len(sub)
    return out


def _clean_name(raw: str) -> str | None:
    name = snake_case(raw)
    if not re.match(r"^[a-z_][a-z0-9_]*$", name) or name in RESERVED_NAMES:
        return None
    return name


def _vtype(raw: str, value: str) -> VarType:
    try:
        return VarType(raw)
    except ValueError:
        return infer_type(value)


def parse_review(text: str, content: str, short_ids: dict[str, Candidate]) -> LlmReview:
    try:
        out = _ModelOutput.model_validate_json(content)
    except ValidationError:
        raise LlmUnavailable("model output did not match the schema") from None

    review = LlmReview()
    # Small models occasionally collapse into calling everything fixed text; a verdict
    # that sweeping is not informative, so it is ignored rather than burying parameters.
    n_fixed = sum(1 for v in out.candidates if v.fixed_text and v.id in short_ids)
    trust_fixed = n_fixed <= max(2, int(MAX_FIXED_SHARE * len(short_ids)))
    if not trust_fixed:
        review.notices.append(
            Notice(kind="info", message="LLM fixed-text verdicts looked unreliable; ignored")
        )
    for v in out.candidates:
        cand = short_ids.get(v.id)
        if cand is None:
            continue
        if v.fixed_text and trust_fixed:
            review.fixed.append(FixedText(candidate_id=cand.id, reason=v.reason))
        name = _clean_name(v.name)
        if name and name != cand.name:
            review.renames.append(Rename(candidate_id=cand.id, name=name, reason=v.reason))

    taken = [s for c in short_ids.values() for s in c.spans]
    for m in out.missed:
        sub = m.substring
        hits = _occurrences(text, sub)
        if not hits and sub.strip() != sub and sub.strip():
            sub = sub.strip()
            hits = _occurrences(text, sub)
        name = _clean_name(m.name)
        if not hits or not 0 <= m.occurrence < len(hits) or name is None:
            review.discarded += 1
            continue
        start = hits[m.occurrence]
        span = Span(start=start, end=start + len(sub))
        if any(span.overlaps(t) for t in taken):
            continue  # already covered by a heuristic candidate
        taken.append(span)
        review.additions.append(
            Candidate(
                name=name,
                name_origin="llm",
                type=_vtype(m.type, sub),
                value=sub,
                spans=[span],
                sources=[Source.LLM],
                detectors=["llm"],
                confidence=ADDITION_CONFIDENCE,
                reason=m.reason or "suggested by the LLM",
            )
        )
    if review.discarded:
        review.notices.append(
            Notice(
                kind="info",
                message=f"LLM proposed {review.discarded} value(s) not found in the text; discarded",
            )
        )
    return review


def apply_review(candidates: Sequence[Candidate], review: LlmReview) -> list[Candidate]:
    """Merge an LLM review into heuristic candidates.

    LLM names replace names derived from types/context/generic rules, but not names
    taken from a key (``db_host: ...``) or chosen manually. Candidates the model calls
    fixed text keep their place but lose confidence. Additions are merged like any
    other detector output (overlaps resolved, names de-duplicated)."""
    renames = {r.candidate_id: r for r in review.renames}
    fixed = {f.candidate_id: f for f in review.fixed}
    out: list[Candidate] = []
    for c in candidates:
        update: dict[str, Any] = {}
        sources = list(c.sources)
        r = renames.get(c.id)
        if r and NAME_ORIGIN_RANK[c.name_origin] < NAME_ORIGIN_RANK["key"]:
            update.update(name=r.name, name_origin="llm")
        f = fixed.get(c.id)
        if f:
            update["confidence"] = round(c.confidence * FIXED_PENALTY, 3)
            update["reason"] = "; ".join(
                x
                for x in [
                    c.reason,
                    f"LLM: likely fixed text ({f.reason})"
                    if f.reason
                    else "LLM: likely fixed text",
                ]
                if x
            )
        if (r or f) and Source.LLM not in sources:
            sources.append(Source.LLM)
            update["sources"] = sources
        out.append(c.model_copy(update=update) if update else c)
    merged = merge_candidates([*out, *review.additions])
    return [c.model_copy(update={"id": candidate_id(c.value, c.spans)}) for c in merged]


async def review_candidates(
    config: LlmConfig,
    text: str,
    candidates: Sequence[Candidate],
    variants: Sequence[str] = (),
) -> LlmReview:
    """Ask the model to review heuristic candidates. Raises LlmUnavailable."""
    if len(text) > config.max_chars:
        review = LlmReview()
        review.notices.append(
            Notice(
                kind="detector_unavailable",
                message=f"document longer than {config.max_chars} characters; LLM review skipped",
            )
        )
        return review
    messages, short = build_messages(text, candidates, variants)
    schema = response_schema(max(1, len(short)))
    content = await asyncio.to_thread(call_model, config, messages, schema)
    return parse_review(text, content, short)


class LlmDetector:
    """:class:`~templatize.detectors.base.AsyncDetector` wrapper around the review."""

    name = "llm"

    def __init__(self, config: LlmConfig) -> None:
        self.config = config

    async def detect_async(
        self, ctx: DetectionContext, existing: list[Candidate]
    ) -> DetectorResult:
        try:
            review = await review_candidates(self.config, ctx.text, existing, ctx.variants)
        except LlmUnavailable as exc:
            return DetectorResult(
                notices=[Notice(kind="detector_unavailable", message=f"LLM: {exc}")]
            )
        return DetectorResult(candidates=apply_review(existing, review), notices=review.notices)
