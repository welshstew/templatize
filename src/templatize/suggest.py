"""Run detectors and merge their output."""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel

from templatize.compose import compose_profile
from templatize.detectors import DetectionContext, Detector, default_detectors
from templatize.detectors.k8s import split_spans
from templatize.ir import MAX_INPUT_CHARS, MAX_VARIANTS, Candidate, LoopCandidate, Notice
from templatize.k8s import DocumentProfile, detect_profile, structural_spans
from templatize.loops import detect_loops, explained_notices
from templatize.merge import compose_candidates, merge_candidates


class InputLimitError(ValueError):
    pass


class SuggestResult(BaseModel):
    candidates: list[Candidate]
    notices: list[Notice]
    profile: DocumentProfile = DocumentProfile()
    """What kind of document this is (e.g. Kubernetes manifests: a Helm chart can be built)."""
    loops: list[LoopCandidate] = []
    """Repeated blocks (list items, SQL rows, keyword lines) that can become loops."""


def check_limits(text: str, variants: Sequence[str] = ()) -> None:
    if len(variants) > MAX_VARIANTS:
        raise InputLimitError(f"at most {MAX_VARIANTS} variants are supported")
    for i, t in enumerate([text, *variants]):
        if len(t) > MAX_INPUT_CHARS:
            label = "input" if i == 0 else f"variant {i}"
            raise InputLimitError(f"{label} exceeds {MAX_INPUT_CHARS} characters")


def suggest(
    text: str,
    variants: Sequence[str] = (),
    detectors: Sequence[Detector] | None = None,
) -> SuggestResult:
    check_limits(text, variants)
    ctx = DetectionContext(text=text, variants=tuple(variants))
    raw: list[Candidate] = []
    notices: list[Notice] = []
    for det in detectors if detectors is not None else default_detectors():
        res = det.detect(ctx)
        raw.extend(res.candidates)
        notices.extend(res.notices)
    profile = detect_profile(text)
    if profile.format is None:
        profile = compose_profile(text) or profile
    if profile.format == "kubernetes":
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
    candidates, composed = compose_candidates(text, merge_candidates(raw))
    notices.extend(composed)
    loops = detect_loops(text, variants, taken_names=[c.name for c in candidates])
    return SuggestResult(
        candidates=candidates,
        notices=explained_notices(notices, loops),
        profile=profile,
        loops=loops,
    )
