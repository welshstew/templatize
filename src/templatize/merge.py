"""Merge candidates from all detectors into a non-overlapping, uniquely named list."""

from __future__ import annotations

import re
from bisect import bisect_right
from collections import defaultdict
from collections.abc import Iterable, Sequence

from templatize.ir import NAME_ORIGIN_RANK, Candidate, Notice, Source, Span, VarType, candidate_id
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
    diffed = [c for c in group if Source.DIFF in c.sources and c.variant_values]
    if diffed:
        # The examples say exactly which occurrences change: another occurrence of the
        # same text that stays the same in the examples is not this variable.
        changing = {_sk(s) for c in diffed for s in c.spans}
        spans = [s for s in spans if _sk(s) in changing]
    sources: list[Source] = []
    detectors: list[str] = []
    for c in group:
        sources.extend(s for s in c.sources if s not in sources)
        detectors.extend(d for d in c.detectors if d not in detectors)
    # At equal rank another detector's name beats the diff's, which only knows the type
    # ("443" stays port_keyword's "port", not the diff's "number").
    best_name = max(
        group,
        key=lambda c: (NAME_ORIGIN_RANK[c.name_origin], Source.DIFF not in c.sources, c.confidence),
    )
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


class _Occupied:
    """Disjoint spans kept sorted by start: an overlap test only has to look at the
    span starting just before ``s`` and the next one (O(log n) instead of O(n))."""

    def __init__(self) -> None:
        self._starts: list[int] = []
        self._spans: list[tuple[int, int]] = []

    def overlaps(self, s: Span) -> bool:
        i = bisect_right(self._starts, s.start) - 1
        for j in (i, i + 1):
            if 0 <= j < len(self._spans):
                a, b = self._spans[j]
                if a < s.end and s.start < b:
                    return True
        return False

    def add(self, s: Span) -> None:
        i = bisect_right(self._starts, s.start)
        self._starts.insert(i, s.start)
        self._spans.insert(i, (s.start, s.end))


def merge_candidates(
    candidates: Iterable[Candidate], taken_names: Iterable[str] = ()
) -> list[Candidate]:
    groups: dict[tuple[str, ...], list[Candidate]] = defaultdict(list)
    for c in candidates:
        groups[_group_key(c)].append(c)
    combined = [_combine(g) for g in groups.values()]

    accepted: list[Candidate] = []
    occupied = _Occupied()
    for c in sorted(combined, key=_score, reverse=True):
        free = [s for s in c.spans if not occupied.overlaps(s)]
        if not free:
            continue
        if c.detectors == ["repetition"] and len(free) < 2 <= len(c.spans):
            continue  # no longer repeated: its other occurrence is part of a longer value
        if len(free) != len(c.spans):
            c = c.model_copy(update={"spans": free})
        for s in free:
            occupied.add(s)
        accepted.append(c)

    # Higher-scoring candidates keep the plain name; others get suffixes.
    names = dedupe_names((c.name for c in accepted), taken=taken_names)
    final = [
        c.model_copy(update={"name": n, "id": candidate_id(c.value, c.spans)})
        for c, n in zip(accepted, names, strict=True)
    ]
    final.sort(key=lambda c: min(s.start for s in c.spans))
    return final


# --- composite values -------------------------------------------------------------

MIN_PART = 3
MIN_PARTS = 2
_PART_ORIGINS = frozenset({"manual", "schema", "key", "llm", "context"})


# Separators that delimit the fields of a URL, DSN or connection string. "-" and "."
# are not among them: "ops-admin" or "acme-marketing-assets-prod" are names, and a
# word inside them matching another value is a coincidence.
_FIELD_SEPARATORS = frozenset(":/@?&=;,#'\" \t")


_MAX_FIELDS_PER_PART = 3


def _parts_in(
    text: str,
    span: Span,
    whole: Candidate,
    by_value: dict[str, Candidate],
    composed: dict[str, list[Candidate]],
) -> list[tuple[Span, Candidate]]:
    """Parts of ``whole`` at ``span``: the value is cut into fields at separators, and
    runs of up to three fields are looked up by value (a part may contain a separator,
    like ``host:port``). Longest parts win; dictionary lookups, not a scan of every
    candidate, so this stays linear in the number of candidates."""
    bounds = [span.start]
    bounds += [
        i + d for i in range(span.start, span.end) if text[i] in _FIELD_SEPARATORS for d in (0, 1)
    ]
    bounds.append(span.end)
    starts = bounds[0::2]
    ends = bounds[1::2]
    found: list[tuple[Span, Candidate]] = []
    for a in range(len(starts)):
        for b in range(a, min(a + _MAX_FIELDS_PER_PART, len(ends))):
            lo, hi = starts[a], ends[b]
            if hi - lo < MIN_PART or hi - lo >= span.end - span.start:
                continue
            p = by_value.get(text[lo:hi])
            if p is not None and p.id != whole.id and p.id not in composed:
                found.append((Span(start=lo, end=hi), p))
    found.sort(key=lambda h: -(h[0].end - h[0].start))
    taken: list[tuple[Span, Candidate]] = []
    for s, p in found:
        if not any(s.overlaps(t) for t, _ in taken):
            taken.append((s, p))
    return taken


def compose_candidates(
    text: str, candidates: Sequence[Candidate]
) -> tuple[list[Candidate], list[Notice]]:
    """A value built from other suggested values (``postgres://shop:secret@db/store``
    next to ``POSTGRES_USER: shop`` and ``POSTGRES_PASSWORD: secret``) is suggested as
    its parts: each part's candidate gains the occurrences inside it, and the whole
    value is no longer a candidate. It needs at least two distinct parts, each a whole
    field between URL/DSN separators (``:/@?&=;,``, whitespace, quotes or the ends of
    the value; one shared word is a coincidence), and with examples, the parts must
    explain how the whole changes."""
    by_value: dict[str, Candidate] = {}
    for c in candidates:
        # Parts must be named values (POSTGRES_USER: shop), not repeated prose words.
        if (
            len(c.value) >= MIN_PART
            and c.value.strip() == c.value
            and c.name_origin in _PART_ORIGINS
        ):
            by_value.setdefault(c.value, c)
    extra: dict[str, list[Span]] = defaultdict(list)
    composed: dict[str, list[Candidate]] = {}
    new_fields: list[Candidate] = []
    for whole in sorted(candidates, key=lambda c: -len(c.value)):
        if whole.id in composed or any(whole.id in [p.id for p in ps] for ps in composed.values()):
            continue
        if not any(ch in _FIELD_SEPARATORS for ch in whole.value):
            continue  # a single field cannot be built from two parts
        hits: list[tuple[Span, Candidate]] = []
        for span in whole.spans:
            hits += _parts_in(text, span, whole, by_value, composed)
        used = {p.id for _, p in hits}
        if len(used) < MIN_PARTS:
            continue
        # The URL's own fields that no part covers (host, port) become variables
        # too, so the whole value is templated, not half of it.
        fields = _url_fields(text, whole, hits, candidates)
        hits_all = hits + [(s, f) for f in fields for s in f.spans if whole_span_of(whole, s)]
        if not _variants_agree(whole, hits_all, text):
            continue
        composed[whole.id] = [p for p in candidates if p.id in used] + fields
        new_fields.extend(fields)
        for s, p in hits:
            extra[p.id].append(s)
    if not composed:
        return list(candidates), []
    out: list[Candidate] = []
    names = {c.id: c.name for c in candidates}
    part_of: dict[str, list[str]] = defaultdict(list)
    for wid, ps in composed.items():
        for p in ps:
            part_of[p.id].append(names[wid])
    for c in candidates:
        if c.id in composed:
            continue
        if c.id in extra:
            spans = sorted([*c.spans, *extra[c.id]], key=_sk)
            reason = "; ".join(filter(None, [c.reason, f"also part of {', '.join(part_of[c.id])}"]))
            c = c.model_copy(
                update={"spans": spans, "reason": reason, "id": candidate_id(c.value, spans)}
            )
        out.append(c)
    out.extend(new_fields)
    out.sort(key=lambda c: min(sp.start for sp in c.spans))
    taken_names = dedupe_names([c.name for c in out])
    out = [
        c if c.name == n else c.model_copy(update={"name": n})
        for c, n in zip(out, taken_names, strict=True)
    ]
    notices = [
        Notice(
            kind="info",
            message=(
                f"{names[wid]} is built from {', '.join(p.name for p in ps)}: "
                "suggested as those parts, so the value stays consistent with them"
            ),
            span=next(c.spans[0] for c in candidates if c.id == wid),
        )
        for wid, ps in composed.items()
    ]
    return out, notices


def _variants_agree(whole: Candidate, hits: list[tuple[Span, Candidate]], text: str) -> bool:
    """With examples: substituting each part's example value into the whole must give
    the whole's example value (otherwise the parts do not explain the change)."""
    if not whole.variant_values:
        return all(not p.variant_values for _, p in hits)
    span = whole.spans[0]
    inside = sorted(((s, p) for s, p in hits if span.contains(s)), key=lambda h: h[0].start)
    for k, expected in enumerate(whole.variant_values):
        out, pos = [], span.start
        for s, p in inside:
            out.append(text[pos : s.start])
            out.append(p.variant_values[k] if p.variant_values else p.value)
            pos = s.end
        out.append(text[pos : span.end])
        if "".join(out) != expected:
            return False
    return True


# scheme://user:password@host:port/path: positions of host and port in a URL value.
_URL = re.compile(
    r"^[A-Za-z][\w+.-]*://(?:[^:@/?#\s]*(?::[^@/?#\s]*)?@)?"
    r"(?P<host>\[[^\]\s]+\]|[^:/?#@\s]+)(?::(?P<port>\d{1,5}))?(?=[/?#]|$)"
)
_URL_SUFFIX = re.compile(r"_(?:url|uri|dsn|connection_string|conn|connection)$")


def whole_span_of(whole: Candidate, s: Span) -> bool:
    return any(w.contains(s) for w in whole.spans)


def _url_fields(
    text: str,
    whole: Candidate,
    hits: list[tuple[Span, Candidate]],
    candidates: Sequence[Candidate],
) -> list[Candidate]:
    """Host and port of a URL being split into parts, when no part covers them:
    ``database_url`` -> ``database_host`` (also covering other places where the host
    is a whole value or key, e.g. a compose service name) and ``database_port``."""
    m = _URL.match(whole.value)
    if not m:
        return []
    base = _URL_SUFFIX.sub("", whole.name) or whole.name
    occupied = [s for c in candidates for s in c.spans if c.id != whole.id] + [s for s, _ in hits]
    variants = [_URL.match(v) for v in whole.variant_values or []]
    if any(v is None for v in variants):
        return []
    out: list[Candidate] = []
    for group, suffix in (("host", "host"), ("port", "port")):
        if m.group(group) is None:
            continue
        value = m.group(group)
        rel = m.start(group)
        spans = [Span(start=w.start + rel, end=w.start + rel + len(value)) for w in whole.spans]
        if any(sp.overlaps(o) for sp in spans for o in occupied):
            continue
        vvals = [v.group(group) or "" for v in variants if v is not None] if variants else None
        if group == "host" and not variants:
            spans += [
                s
                for s in _whole_scalars(text, value)
                if not any(s.overlaps(o) for o in occupied + spans)
            ]
        vtype = VarType.PORT if group == "port" else infer_type(value)
        if group == "host" and vtype is VarType.STRING:
            vtype = VarType.HOSTNAME
        out.append(
            Candidate(
                name=f"{base}_{suffix}",
                name_origin="context",
                type=vtype,
                value=value,
                spans=sorted(spans, key=_sk),
                sources=[*whole.sources],
                detectors=["compose.url_field"],
                confidence=round(min(whole.confidence, 0.8), 3),
                reason=f"the {group} of {whole.name}",
                variant_values=vvals,
            )
        )
    return out


def _whole_scalars(text: str, value: str) -> list[Span]:
    """Other places where ``value`` is a whole YAML/flow value or key on its own
    (``db:``, ``[db]``, ``- db``, ``host: db``), not a word inside something else."""
    out: list[Span] = []
    for m in re.finditer(r"(?<![\w./@:-])" + re.escape(value) + r"(?![\w./@-])", text):
        line_start = text.rfind("\n", 0, m.start()) + 1
        before = text[line_start : m.start()].rstrip(" \t\"'")
        after = text[m.end() :].lstrip(" \t\"'")[:1]
        if (before == "" or before.endswith(("[", ",", ":", "-"))) and after in (
            "",
            ":",
            "]",
            ",",
            "\n",
            "\r",
        ):
            out.append(Span(start=m.start(), end=m.end()))
    return out
