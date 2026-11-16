"""Run detectors and merge their output."""

from __future__ import annotations

import bisect
import re
from bisect import bisect_right
from collections.abc import Callable, Iterable, Sequence

from pydantic import BaseModel

from templatize.compose import compose_profile
from templatize.detectors import DetectionContext, Detector, default_detectors
from templatize.detectors.k8s import split_spans
from templatize.detectors.ner import fill_gaps_only
from templatize.doctypes import DocumentType, detect_type
from templatize.ir import (
    MAX_INPUT_CHARS,
    MAX_LOOP_ITEMS,
    MAX_VARIANTS,
    Candidate,
    LoopCandidate,
    Notice,
    Span,
)
from templatize.k8s import DocumentProfile, detect_profile, structural_spans
from templatize.loops import detect_loops, explained_notices
from templatize.merge import compose_candidates, merge_candidates
from templatize.schemas import apply as apply_schema
from templatize.schemas import structure_regions
from templatize.userschema import (
    SchemaInfo,
    apply_catalog,
    find_reference,
    load_schema,
    without_reference,
)
from templatize.userschema import apply as apply_user_schema


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
    schema_info: SchemaInfo | None = None
    """The JSON Schema the document references (not downloaded) and/or the one the
    caller supplied; None when there is neither."""


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


def _longest(lp: LoopCandidate) -> int:
    """Items of the loop or of any nested list (a nested list's items are counted
    across all its parents)."""
    return max([len(lp.items), *(_longest(c) for c in lp.children)])


def _overlap_index(spans: Iterable[Span]) -> Callable[[Span], bool]:
    """``s -> does s overlap any of spans``, in O(log n) per query (a manifest with
    hundreds of resources has tens of thousands of structural spans)."""
    ordered = sorted(spans, key=lambda f: f.start)
    starts = [f.start for f in ordered]
    reach: list[int] = []  # furthest end among the spans so far
    for f in ordered:
        reach.append(max(f.end, reach[-1]) if reach else f.end)

    def hits(s: Span) -> bool:
        i = bisect.bisect_left(starts, s.end)  # spans starting before s ends
        return i > 0 and reach[i - 1] > s.start

    return hits


def suggest(
    text: str,
    variants: Sequence[str] = (),
    detectors: Sequence[Detector] | None = None,
    document_type: str | None = None,
    schema: str | None = None,
) -> SuggestResult:
    """Suggest variables and loops. ``document_type`` (an id from
    :data:`templatize.doctypes.TYPES`) confirms the type: its format rules apply."""
    check_limits(text, variants)
    user_schema = load_schema(schema) if schema else None  # SchemaError before any work
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
        hits_fixed = _overlap_index(structural_spans(text))
        split = set(split_spans(text))
        raw = [
            c.model_copy(update={"spans": kept})
            for c in raw
            if (
                kept := [
                    s
                    for s in c.spans
                    if not hits_fixed(s) and ("k8s" in c.detectors or s not in split)
                ]
            )
        ]
    raw = _outside_template_syntax(text, raw)
    raw = apply_schema(text, doc_type, raw)
    reference = find_reference(text)
    raw = without_reference(raw, reference)
    schema_info = SchemaInfo(reference=reference) if reference else None
    if user_schema is not None:
        raw, schema_info = apply_user_schema(text, user_schema, raw)
        schema_info.reference = reference
    elif (bundled := apply_catalog(text, doc_type, reference, raw))[1] is not None:
        raw, schema_info = bundled
    elif reference is not None:
        schema_info = SchemaInfo(
            reference=reference,
            notes=[
                "the document references a schema that is not in the bundled catalogue; "
                "it is not downloaded (offline). Supply its text to use it."
            ],
        )
    candidates, composed = compose_candidates(text, merge_candidates(fill_gaps_only(raw)))
    notices.extend(composed)
    loops = detect_loops(text, variants, taken_names=[c.name for c in candidates])
    if regions := structure_regions(text, doc_type):
        # A list made only of structure (a policy's capabilities) is not a loop.
        loops = [
            lp
            for lp in loops
            if not all(
                any(r.start <= s.start and s.end <= r.end for r in regions) for s in lp.items
            )
        ]
    # A loop over the item limit could never be built: say so instead of offering it.
    too_long = [lp for lp in loops if _longest(lp) > MAX_LOOP_ITEMS]
    if too_long:
        loops = [lp for lp in loops if lp not in too_long]
        for lp in too_long:
            notices.append(
                Notice(
                    kind="info",
                    message=(
                        f"the list {lp.name!r} has {_longest(lp):,} items, more than the "
                        f"{MAX_LOOP_ITEMS:,} a loop can hold; it is left as text"
                    ),
                    span=lp.region,
                )
            )
    return SuggestResult(
        candidates=candidates,
        notices=explained_notices(notices, loops),
        profile=profile,
        loops=loops,
        document_type=doc_type,
        schema_info=schema_info,
    )
