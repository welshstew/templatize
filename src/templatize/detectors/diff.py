"""Multi-example diffing.

Given the primary document and 1-3 variants of it, align them and suggest the
regions that differ as variables (very high confidence).

Algorithm
---------
1. Line-level ``SequenceMatcher`` between primary and variant; inside each
   replaced block of lines, a token-level ``SequenceMatcher`` (tokens are words,
   single punctuation characters, whitespace runs and newlines). This keeps large
   inputs fast while still producing token-precise regions.
2. Each change region is *widened* to delimiter boundaries (whitespace, quotes,
   brackets, ``,;=<>|``), so ``10.0.0.1`` vs ``10.0.0.2`` yields the whole address,
   and an insertion (``Dear Bob`` -> ``Dear Dr. Bob``) becomes a substitution of the
   enclosing word (``Bob`` -> ``Dr. Bob``).
3. Insertions/deletions that cannot be widened into a single-line substitution
   are reported as ``structural_difference`` notices instead of candidates
   (conditionals/loops are out of scope for v1).
4. Regions from all variants are unioned in primary-text space and each merged
   span is mapped back into every variant to collect ``variant_values``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from templatize.detectors.base import DetectionContext, DetectorResult
from templatize.detectors.regex import RegexDetector
from templatize.ir import Candidate, NameOrigin, Notice, Source, Span, VarType
from templatize.naming import infer_type, snake_case, type_name

_TOKEN = re.compile(r"\r\n|\r|\n|[^\S\r\n]+|\w+|[^\w\s]")
_DELIMS = frozenset(" \t\r\n\f\v,;\"'`()[]{}<>=|")
_CONTEXT_KEY = re.compile(r"([A-Za-z_][\w.-]*)[\"']?[ \t]*[:=][ \t]*[\"']?$")
_PROPER = re.compile(r"^[A-Z][\w'.-]*(?: [A-Z][\w'.-]*){0,3}$")

_TRAILING = frozenset(".:!?")
_REFERENCE = re.compile(r"^[A-Z]{1,6}[-/#]?\d[\w/-]*$")
_AMOUNT = re.compile(r"^\d{1,3}(?:,\d{3})+(?:\.\d+)?$|^\d+\.\d{2}$")
# Regex detections that diff regions snap to, so "12 March" -> "2 April" becomes the whole date.
_ANCHOR_TYPES = frozenset(
    {
        VarType.DATE,
        VarType.DATETIME,
        VarType.TIME,
        VarType.EMAIL,
        VarType.URL,
        VarType.IPV4,
        VarType.IPV6,
        VarType.CIDR,
        VarType.UUID,
        VarType.MAC,
        VarType.SEMVER,
    }
)

CONFIDENCE = 0.95


@dataclass
class Segment:
    a1: int
    a2: int
    b1: int
    b2: int
    equal: bool
    widened: bool = False


def _is_delim(ch: str) -> bool:
    return ch in _DELIMS or ch.isspace()


def _delim_at(s: str, i: int) -> bool:
    """Delimiter test with context: a comma between digits (``18,450``) is part of a number."""
    ch = s[i]
    if ch == "," and 0 < i < len(s) - 1 and s[i - 1].isdigit() and s[i + 1].isdigit():
        return False
    return _is_delim(ch)


def _tokens(s: str) -> list[str]:
    return _TOKEN.findall(s)


def _char_segments(a: str, b: str) -> list[Segment]:
    """Diff ``a`` and ``b`` into alternating equal / change segments in char space."""
    a_lines = a.splitlines(keepends=True)
    b_lines = b.splitlines(keepends=True)
    a_off = _offsets(a_lines)
    b_off = _offsets(b_lines)
    raw: list[Segment] = []
    lm = SequenceMatcher(None, a_lines, b_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in lm.get_opcodes():
        ca1, ca2, cb1, cb2 = a_off[i1], a_off[i2], b_off[j1], b_off[j2]
        if tag == "equal":
            raw.append(Segment(ca1, ca2, cb1, cb2, True))
        elif tag == "replace":
            raw.extend(_token_segments(a[ca1:ca2], b[cb1:cb2], ca1, cb1))
        else:
            raw.append(Segment(ca1, ca2, cb1, cb2, False))
    return _coalesce(_absorb_gaps(a, _coalesce(raw)))


def _absorb_gaps(a: str, segs: list[Segment]) -> list[Segment]:
    """Two changes separated only by spaces on one line are one change: the token
    diff pairs up the spaces of "Procurement Manager" and "Head of Operations",
    which would otherwise split a changed phrase into one variable per word."""
    out: list[Segment] = []
    for i, s in enumerate(segs):
        gap = a[s.a1 : s.a2]
        if (
            s.equal
            and out
            and not out[-1].equal
            and i + 1 < len(segs)
            and not segs[i + 1].equal
            and gap
            and gap in (" ", "\t")
        ):
            out[-1].a2, out[-1].b2 = s.a2, s.b2
            continue
        out.append(Segment(s.a1, s.a2, s.b1, s.b2, s.equal, s.widened))
    return out


def _offsets(lines: list[str]) -> list[int]:
    out = [0]
    for line in lines:
        out.append(out[-1] + len(line))
    return out


def _token_segments(a: str, b: str, a_base: int, b_base: int) -> list[Segment]:
    at, bt = _tokens(a), _tokens(b)
    ao, bo = _offsets(at), _offsets(bt)
    tm = SequenceMatcher(None, at, bt, autojunk=False)
    return [
        Segment(a_base + ao[i1], a_base + ao[i2], b_base + bo[j1], b_base + bo[j2], tag == "equal")
        for tag, i1, i2, j1, j2 in tm.get_opcodes()
    ]


def _coalesce(segs: list[Segment]) -> list[Segment]:
    out: list[Segment] = []
    for s in segs:
        if s.a1 == s.a2 and s.b1 == s.b2:
            continue
        if out and out[-1].equal == s.equal:
            out[-1].a2, out[-1].b2 = s.a2, s.b2
            out[-1].widened = out[-1].widened or s.widened
        else:
            out.append(Segment(s.a1, s.a2, s.b1, s.b2, s.equal, s.widened))
    return out


def _widen(a: str, b: str, segs: list[Segment]) -> list[Segment]:
    """Grow change segments to delimiter boundaries, eating into neighbouring equal segments."""
    changes = [s for s in segs if not s.equal]
    for idx, ch in enumerate(changes):
        lo = changes[idx - 1].a2 if idx > 0 else 0
        hi = changes[idx + 1].a1 if idx + 1 < len(changes) else len(a)
        needs = ch.a1 == ch.a2 or ch.b1 == ch.b2
        a1, a2, b1, b2 = ch.a1, ch.a2, ch.b1, ch.b2
        # Slide whitespace-led insertions/deletions right so they attach to the
        # following word: "Dear| Dr.| Bob" -> "Dear |Dr. |Bob".
        if a1 == a2:
            while a1 < hi and b2 < len(b) and b[b1] == b[b2] == a[a1] and b[b1].isspace():
                a1, a2, b1, b2 = a1 + 1, a2 + 1, b1 + 1, b2 + 1
        elif b1 == b2:
            while a2 < hi and a[a1] == a[a2] and a[a1].isspace():
                a1, a2, b1, b2 = a1 + 1, a2 + 1, b1 + 1, b2 + 1
        # Grow only a change that cuts a word: one that already starts/ends on a
        # delimiter (column padding before "1") keeps its edge.
        if needs or not _delim_at(a, a1):
            while a1 > lo and not _delim_at(a, a1 - 1):
                a1 -= 1
                b1 -= 1
        if needs or not _delim_at(a, a2 - 1):
            while a2 < hi and not _delim_at(a, a2):
                a2 += 1
                b2 += 1
        if a1 == a2 and needs:
            # Inserted text between delimiters: take the following word as the anchor.
            while a2 < hi and _delim_at(a, a2) and a[a2] not in "\r\n":
                a2 += 1
                b2 += 1
            while a2 < hi and not _delim_at(a, a2):
                a2 += 1
                b2 += 1
        # Sentence punctuation picked up by widening is shared text, not part of the value.
        while a2 - a1 > 1 and a2 > ch.a2 and a[a2 - 1] in _TRAILING:
            a2 -= 1
            b2 -= 1
        if (a1, a2) != (ch.a1, ch.a2):
            ch.widened = ch.widened or needs
        elif needs:
            ch.widened = True
        ch.a1, ch.a2, ch.b1, ch.b2 = a1, a2, b1, b2
    # Rebuild the full segment list with recomputed equal gaps, merging touching changes.
    merged: list[Segment] = []
    for ch in changes:
        if merged and ch.a1 <= merged[-1].a2:
            last = merged[-1]
            last.a2, last.b2 = max(last.a2, ch.a2), max(last.b2, ch.b2)
            last.widened = last.widened or ch.widened
        else:
            merged.append(Segment(ch.a1, ch.a2, ch.b1, ch.b2, False, ch.widened))
    out: list[Segment] = []
    pa = pb = 0
    for ch in merged:
        if ch.a1 > pa:
            out.append(Segment(pa, ch.a1, pb, ch.b1, True))
        out.append(ch)
        pa, pb = ch.a2, ch.b2
    if pa < len(a):
        out.append(Segment(pa, len(a), pb, pb + (len(a) - pa), True))
    return out


def _map_start(segs: list[Segment], p: int) -> int:
    for s in segs:
        if s.a1 == p or s.a1 <= p < s.a2:
            return s.b1 + (p - s.a1) if s.equal else s.b1
    return segs[-1].b2 if segs else p


def _map_end(segs: list[Segment], p: int) -> int:
    for s in reversed(segs):
        if s.a2 == p or s.a1 < p <= s.a2:
            return s.b1 + (p - s.a1) if s.equal else s.b2
    return segs[0].b1 if segs else p


def _name_for(text: str, start: int, value: str) -> tuple[str, NameOrigin, VarType]:
    vtype = infer_type(value)
    line_start = max(text.rfind("\n", 0, start), text.rfind("\r", 0, start)) + 1
    m = _CONTEXT_KEY.search(text, line_start, start)
    if m:
        return snake_case(m.group(1)), "context", vtype
    if _AMOUNT.match(value):
        return "amount", "type", VarType.FLOAT
    if vtype is not VarType.STRING:
        return type_name(vtype), "type", vtype
    if _REFERENCE.match(value):
        return "reference", "type", vtype
    if _PROPER.match(value):
        return "name", "generic", vtype
    return "value", "generic", vtype


def _snap_to_anchors(text: str, spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    anchors = [
        c.spans[0]
        for c in RegexDetector().detect(DetectionContext(text=text)).candidates
        if c.type in _ANCHOR_TYPES
    ]
    out: list[tuple[int, int]] = []
    for s1, s2 in spans:
        for an in anchors:
            if an.start < s2 and s1 < an.end:
                s1, s2 = min(s1, an.start), max(s2, an.end)
        out.append((s1, s2))
    return out


Region = tuple[int, int, tuple[str, ...]]


def _split_on_known(regions: list[Region]) -> list[Region]:
    """Split a region that starts or ends with another region's value, space-separated,
    in the document and in every example: with "Dear Sarah" (-> "Tomasz") elsewhere,
    "Sarah Okafor" (-> "Tomasz Nowak") becomes "Sarah" + "Okafor", so the first name
    is one variable everywhere instead of the full name hiding it."""
    known = {key for _, _, key in regions}
    out: list[Region] = []
    for s1, s2, key in regions:
        for other in known:
            if other == key or not all(other):
                continue
            if all(v.startswith(o + " ") for v, o in zip(key, other, strict=True)):
                cut = len(other[0])
                rest = tuple(v[len(o) + 1 :] for v, o in zip(key, other, strict=True))
                out += [(s1, s1 + cut, other), (s1 + cut + 1, s2, rest)]
                break
            if all(v.endswith(" " + o) for v, o in zip(key, other, strict=True)):
                cut = len(key[0]) - len(other[0])
                rest = tuple(v[: len(v) - len(o) - 1] for v, o in zip(key, other, strict=True))
                out += [(s1, s1 + cut - 1, rest), (s1 + cut, s2, other)]
                break
        else:
            out.append((s1, s2, key))
    return out


class DiffDetector:
    name = "diff"

    def detect(self, ctx: DetectionContext) -> DetectorResult:
        if not ctx.variants:
            return DetectorResult()
        a = ctx.text
        per_variant: list[list[Segment]] = []
        spans: list[tuple[int, int]] = []
        notices: list[Notice] = []
        for k, b in enumerate(ctx.variants):
            segs = _widen(a, b, _char_segments(a, b))
            per_variant.append(segs)
            for s in segs:
                if s.equal:
                    continue
                av, bv = a[s.a1 : s.a2], b[s.b1 : s.b2]
                structural = (
                    s.a1 == s.a2
                    or not av.strip()
                    or (s.widened and any(c in av + bv for c in "\r\n"))
                )
                if structural:
                    notices.append(
                        Notice(
                            kind="structural_difference",
                            message=(
                                f"variant {k + 1} inserts or removes text that cannot be "
                                "expressed as a single-line substitution; not templated in v1"
                            ),
                            span=Span(start=s.a1, end=max(s.a2, s.a1 + 1))
                            if s.a1 < len(a)
                            else None,
                        )
                    )
                    continue
                spans.append((s.a1, s.a2))

        spans = _snap_to_anchors(a, spans)
        spans.sort()
        union: list[list[int]] = []
        for s1, s2 in spans:
            if union and s1 <= union[-1][1]:
                union[-1][1] = max(union[-1][1], s2)
            else:
                union.append([s1, s2])

        # Regions with the same primary value and the same values in every variant are
        # one variable occurring several times ("Dear Ms. Johnson" / "Ms. Alice Johnson").
        regions = [
            (
                s1,
                s2,
                (
                    a[s1:s2],
                    *(
                        ctx.variants[k][_map_start(segs, s1) : _map_end(segs, s2)]
                        for k, segs in enumerate(per_variant)
                    ),
                ),
            )
            for s1, s2 in union
        ]
        grouped: dict[tuple[str, ...], list[Span]] = {}
        for s1, s2, key in _split_on_known(regions):
            grouped.setdefault(key, []).append(Span(start=s1, end=s2))

        out: list[Candidate] = []
        for (value, *values_by_variant), group_spans in grouped.items():
            name, origin, vtype = _name_for(a, group_spans[0].start, value)
            out.append(
                Candidate(
                    name=name,
                    name_origin=origin,
                    type=vtype,
                    value=value,
                    spans=group_spans,
                    sources=[Source.DIFF],
                    detectors=["diff"],
                    confidence=CONFIDENCE,
                    reason="differs between examples",
                    variant_values=list(values_by_variant),
                )
            )
        return DetectorResult(candidates=out, notices=notices)
