"""Run detectors and merge their output."""

from __future__ import annotations

import re
from bisect import bisect_right
from collections.abc import Sequence

from pydantic import BaseModel

from templatize.compose import compose_profile
from templatize.detectors import DetectionContext, Detector, default_detectors
from templatize.detectors.k8s import split_spans
from templatize.doctypes import DocumentType, detect_type
from templatize.ir import MAX_INPUT_CHARS, MAX_VARIANTS, Candidate, LoopCandidate, Notice, Span
from templatize.k8s import DocumentProfile, detect_profile, structural_spans
from templatize.loops import detect_loops, explained_notices
from templatize.merge import compose_candidates, merge_candidates
from templatize.schemas import apply as apply_schema


class InputLimitError(ValueError):
    pass


class SuggestResult(BaseModel):
    candidates: list[Candidate]
    notices: list[Notice]
    profile: DocumentProfile = DocumentProfile()
    """What kind of document this is (e.g. Kubernetes manifests: a Helm chart can be built)."""
    loops: list[LoopCandidate] = []
    """Repeated blocks (list items, SQL rows, keyword lines) that can become loops."""
    document_type: DocumentType | None = None
    """What kind of document this is (detected, or confirmed by the caller)."""


def check_limits(text: str, variants: Sequence[str] = ()) -> None:
    if len(variants) > MAX_VARIANTS:
        raise InputLimitError(f"at most {MAX_VARIANTS} variants are supported")
    for i, t in enumerate([text, *variants]):
        if len(t) > MAX_INPUT_CHARS:
            label = "input" if i == 0 else f"variant {i}"
            raise InputLimitError(f"{label} exceeds {MAX_INPUT_CHARS} characters")


# Template syntax already in the document (GitHub Actions ${{ github.sha }}, Jinja,
# Helm): what is inside it is an expression, not a value to extract.
# Bounded, so an unclosed "{{" does not scan to the end of the text from every "{{".
_TEMPLATE_EXPR = re.compile(r"\$?\{\{.{0,496}?\}\}|\{%.{0,496}?%\}", re.DOTALL)


def _outside_template_syntax(text: str, raw: list[Candidate]) -> list[Candidate]:
    exprs = [(m.start(), m.end()) for m in _TEMPLATE_EXPR.finditer(text)]
    if not exprs:
        return raw
    starts = [a for a, _ in exprs]  # disjoint and sorted: finditer does not overlap

    def touches(s: Span) -> bool:
        """Inside an expression, or containing one (``tags: ${{ env.REGISTRY }}/...``
        is already templated: a variable would replace the expressions with text)."""
        i = bisect_right(starts, s.start) - 1
        if i >= 0 and s.start < exprs[i][1]:
            return True
        return i + 1 < len(exprs) and exprs[i + 1][0] < s.end

    out = []
    for c in raw:
        kept = [s for s in c.spans if not touches(s)]
        if kept:
            out.append(c if len(kept) == len(c.spans) else c.model_copy(update={"spans": kept}))
    return out


def suggest(
    text: str,
    variants: Sequence[str] = (),
    detectors: Sequence[Detector] | None = None,
    document_type: str | None = None,
) -> SuggestResult:
    """Suggest variables and loops. ``document_type`` (an id from
    :data:`templatize.doctypes.TYPES`) confirms the type: its format rules apply."""
    check_limits(text, variants)
    doc_type = detect_type(text, confirmed=document_type)
    ctx = DetectionContext(text=text, variants=tuple(variants), doc_type=doc_type)
    raw: list[Candidate] = []
    notices: list[Notice] = []
    for det in detectors if detectors is not None else default_detectors():
        res = det.detect(ctx)
        raw.extend(res.candidates)
        notices.extend(res.notices)
    profile = detect_profile(text)
    if profile.format is None:
        profile = compose_profile(text) or profile
    if profile.format == "kubernetes" and ctx.profile.kubernetes is not False:
        # apiVersion/kind and mapping keys are structure, never parameters; whole
        # images are superseded by their repository/tag parts.
        fixed = structural_spans(text)
        split = set(split_spans(text))
        raw = [
            c.model_copy(update={"spans": kept})
            for c in raw
            if (
                kept := [
                    s
                    for s in c.spans
                    if not any(s.overlaps(f) for f in fixed)
                    and ("k8s" in c.detectors or s not in split)
                ]
            )
        ]
    raw = _outside_template_syntax(text, raw)
    raw = apply_schema(text, doc_type, raw)
    candidates, composed = compose_candidates(text, merge_candidates(raw))
    notices.extend(composed)
    loops = detect_loops(text, variants, taken_names=[c.name for c in candidates])
    return SuggestResult(
        candidates=candidates,
        notices=explained_notices(notices, loops),
        profile=profile,
        loops=loops,
        document_type=doc_type,
    )
