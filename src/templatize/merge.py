"""Merge candidates from all detectors into a non-overlapping, uniquely named list."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from templatize.ir import NAME_ORIGIN_RANK, Candidate, Source, Span, VarType, candidate_id
from templatize.naming import dedupe_names, infer_type

AGREEMENT_BONUS = 0.05
LENGTH_BONUS_MAX = 0.1
LENGTH_BONUS_CHARS = 50


def _is_trivial(value: str) -> bool:
    """Short numbers and booleans are not unified across locations (replicas: 1 vs version: 1)."""
    v = value.strip()
    if len(v) < 3:
        return True
    if v.isdigit() and len(v) < 4:
        return True
    return infer_type(v) is VarType.BOOL


def _group_key(c: Candidate) -> tuple[str, ...]:
    if _is_trivial(c.value):
        return ("exact", c.value, *(f"{s.start}-{s.end}" for s in sorted(c.spans, key=_sk)))
    return ("value", c.value)


def _sk(s: Span) -> tuple[int, int]:
    return (s.start, s.end)


def _combine(group: list[Candidate]) -> Candidate:
    if len(group) == 1:
        return group[0]
    spans = sorted({_sk(s): s for c in group for s in c.spans}.values(), key=_sk)
    sources: list[Source] = []
    detectors: list[str] = []
    for c in group:
        sources.extend(s for s in c.sources if s not in sources)
        detectors.extend(d for d in c.detectors if d not in detectors)
    best_name = max(group, key=lambda c: (NAME_ORIGIN_RANK[c.name_origin], c.confidence))
    typed = [c for c in group if c.type is not VarType.STRING]
    vtype = max(typed, key=lambda c: c.confidence).type if typed else VarType.STRING
    variant_values = next((c.variant_values for c in group if c.variant_values), None)
    pathed = [c for c in group if c.values_path]
    values_path = (
        max(pathed, key=lambda c: (NAME_ORIGIN_RANK[c.name_origin], c.confidence)).values_path
        if pathed
        else None
    )
    confidence = min(0.99, max(c.confidence for c in group) + AGREEMENT_BONUS * (len(sources) - 1))
    reasons = [c.reason for c in group if c.reason]
    return Candidate(
        name=best_name.name,
        name_origin=best_name.name_origin,
        type=vtype,
        value=group[0].value,
        spans=spans,
        sources=sources,
        detectors=detectors,
        confidence=round(confidence, 3),
        reason="; ".join(dict.fromkeys(reasons)) or None,
        variant_values=variant_values,
        values_path=values_path,
    )


def _score(c: Candidate) -> float:
    length = max(s.length for s in c.spans)
    return c.confidence + LENGTH_BONUS_MAX * min(length, LENGTH_BONUS_CHARS) / LENGTH_BONUS_CHARS


def merge_candidates(
    candidates: Iterable[Candidate], taken_names: Iterable[str] = ()
) -> list[Candidate]:
    groups: dict[tuple[str, ...], list[Candidate]] = defaultdict(list)
    for c in candidates:
        groups[_group_key(c)].append(c)
    combined = [_combine(g) for g in groups.values()]

    accepted: list[Candidate] = []
    occupied: list[Span] = []
    for c in sorted(combined, key=_score, reverse=True):
        free = [s for s in c.spans if not any(s.overlaps(o) for o in occupied)]
        if not free:
            continue
        if len(free) != len(c.spans):
            c = c.model_copy(update={"spans": free})
        occupied.extend(free)
        accepted.append(c)

    # Higher-scoring candidates keep the plain name; others get suffixes.
    names = dedupe_names((c.name for c in accepted), taken=taken_names)
    final = [
        c.model_copy(update={"name": n, "id": candidate_id(c.value, c.spans)})
        for c, n in zip(accepted, names, strict=True)
    ]
    final.sort(key=lambda c: min(s.start for s in c.spans))
    return final
