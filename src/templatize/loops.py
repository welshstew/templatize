"""Loop suggestions: repeated blocks (list items, SQL rows, keyword lines).

Finding a loop has two steps:

1. A *finder* proposes item boundaries for one kind of structure:
   YAML/JSON sequences (parser node offsets), ``INSERT ... VALUES`` rows, inline
   lists (``BROKERS = ["a", "b"]``) and runs of lines that start with the same
   keyword (``server 10.0.0.1:8080;``).
2. :func:`infer_fields` compares the items with the diff machinery: what differs
   between them are the fields, the rest is the skeleton, which must be identical in
   every item (the same check :class:`templatize.ir.Loop` validates).

Examples (variants) are searched the same way; a loop found in the main text and an
example is matched by name, and its lists become ``variant_values``.
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from templatize.detectors.diff import _char_segments, _map_end, _map_start, _name_for, _widen
from templatize.detectors.sql import insert_statements
from templatize.ir import (
    Candidate,
    LoopCandidate,
    LoopFieldCandidate,
    LoopValues,
    Notice,
    Source,
    Span,
    VarType,
)
from templatize.naming import dedupe_names, infer_type, snake_case

try:
    _Loader: type = yaml.CSafeLoader
except AttributeError:  # pragma: no cover
    _Loader = yaml.SafeLoader

MIN_ITEMS = 2
MAX_LOOPS = 50
VARIANT_CONFIDENCE = 0.97


@dataclass
class Found:
    """Item boundaries proposed by a finder."""

    name: str
    items: list[Span]
    detector: str
    confidence: float
    reason: str
    names: list[tuple[Span, str]] = field(default_factory=list)
    """Named regions of the first item (absolute): a field inside one takes its name
    (SQL column names)."""
    fields: list[list[Span]] | None = None
    """Known fields (one span per item), instead of inferring them by diffing."""
    map: bool = False
    aligns: list[str | None] | None = None
    """Per known field: "left"/"right" for padded table columns."""
    field_names: list[str] | None = None
    children: list[Found] = field(default_factory=list)
    """Nested lists inside each item."""
    groups: list[int] | None = None
    """For a nested list: items per parent item."""


# --- field inference -------------------------------------------------------------


def infer_fields(text: str, items: Sequence[Span]) -> list[list[Span]] | None:
    """Field spans (one list per field, one span per item) or None if the items do
    not share one skeleton."""
    if len(items) < MIN_ITEMS:
        return None
    first = items[0]
    a = text[first.start : first.end]
    per_item = []
    regions: list[tuple[int, int]] = []
    for it in items[1:]:
        b = text[it.start : it.end]
        segs = _widen(a, b, _char_segments(a, b))
        per_item.append(segs)
        for s in segs:
            if s.equal:
                continue
            if s.a1 == s.a2 or s.b1 == s.b2:
                return None  # text inserted/removed: the shapes differ
            regions.append((s.a1, s.a2))
    if not regions:
        return None
    regions.sort()
    union: list[list[int]] = []
    for r1, r2 in regions:
        if union and r1 <= union[-1][1]:
            union[-1][1] = max(union[-1][1], r2)
        else:
            union.append([r1, r2])
    fields: list[list[Span]] = []
    for r1, r2 in union:
        spans = [Span(start=first.start + r1, end=first.start + r2)]
        for it, segs in zip(items[1:], per_item, strict=True):
            b1, b2 = _map_start(segs, r1), _map_end(segs, r2)
            if b2 <= b1:
                return None
            spans.append(Span(start=it.start + b1, end=it.start + b2))
        fields.append(spans)
    skeletons = {_pieces(text, it, [f[i] for f in fields]) for i, it in enumerate(items)}
    return fields if len(skeletons) == 1 else None


def _pieces(text: str, item: Span, holes: list[Span]) -> tuple[str, ...]:
    out, pos = [], item.start
    for h in holes:
        out.append(text[pos : h.start])
        pos = h.end
    out.append(text[pos : item.end])
    return tuple(out)


def _same_separator(text: str, items: Sequence[Span]) -> bool:
    seps = {text[a.end : b.start] for a, b in itertools.pairwise(items)}
    return len(seps) <= 1 and all(a.end <= b.start for a, b in itertools.pairwise(items))


# --- finders ---------------------------------------------------------------------


def _trimmed(text: str, start: int, end: int) -> Span | None:
    while end > start and text[end - 1] in " \t\r\n":
        end -= 1
    return Span(start=start, end=end) if end > start else None


def _scalar_span(text: str, node: ScalarNode) -> Span | None:
    start, end = node.start_mark.index, node.end_mark.index
    if node.style in ("'", '"') and end - start >= 2:
        return Span(start=start + 1, end=end - 1) if end - start > 2 else None
    if node.style in ("|", ">"):
        return None
    return _trimmed(text, start, end)


def find_yaml(text: str) -> list[Found]:
    """Sequences of >= 2 scalars, or of >= 2 mappings with the same keys."""
    try:
        docs = list(yaml.compose_all(text, Loader=_Loader))
    except (yaml.YAMLError, ValueError):
        return []
    out: list[Found] = []

    def walk(node: Node | None, key: str | None) -> None:
        if isinstance(node, MappingNode):
            for k, v in node.value:
                walk(v, k.value if isinstance(k, ScalarNode) else None)
        elif isinstance(node, SequenceNode):
            kids = node.value
            if len(kids) >= MIN_ITEMS:
                items: list[Span | None] = []
                if all(isinstance(k, ScalarNode) for k in kids):
                    # Same quoting for every item: the quotes are part of the separator.
                    if len({k.style for k in kids if isinstance(k, ScalarNode)}) == 1:
                        items = [_scalar_span(text, k) for k in kids]
                elif all(isinstance(k, MappingNode) for k in kids):
                    keysets = {tuple(sorted(str(kk.value) for kk, _ in k.value)) for k in kids}
                    if len(keysets) == 1:
                        nested = _nested(text, kids, key)
                        if nested is not None:
                            out.append(nested)
                            for k in kids:
                                walk(k, key)
                            return
                        items = [_trimmed(text, k.start_mark.index, k.end_mark.index) for k in kids]
                if items and all(items):
                    spans = [s for s in items if s is not None]
                    prose = all(isinstance(k, ScalarNode) and " " in str(k.value) for k in kids)
                    out.append(
                        Found(
                            name=snake_case(key or "items"),
                            items=spans,
                            detector="loop.yaml",
                            confidence=0.65 if prose else 0.8,
                            reason=f"items of the list {key!r}" if key else "list items",
                        )
                    )
            for k in kids:
                walk(k, key)

    for d in docs:
        walk(d, None)
        _maps(text, d, None, out, ())
    return out


def _nested(text: str, kids: list[Node], key: str | None) -> Found | None:
    """Mappings with the same keys where some keys hold a list in every item (with
    >= 1 entry): an outer loop whose scalar values that differ are fields and whose
    lists are nested loops (``servers[].ports[]``). One level; inner items are
    scalars with one quoting style, or flat mappings of scalars with the same keys."""
    maps = [k for k in kids if isinstance(k, MappingNode)]
    keys = [str(kk.value) for kk, _ in maps[0].value]
    list_keys = [
        k
        for k in keys
        if all(
            isinstance(dict((str(a.value), b) for a, b in m.value)[k], SequenceNode) for m in maps
        )
    ]
    if not list_keys:
        return None
    items = [_trimmed(text, m.start_mark.index, m.end_mark.index) for m in maps]
    if not all(items):
        return None
    outer_items = [it for it in items if it is not None]
    entries = [dict((str(a.value), b) for a, b in m.value) for m in maps]
    fields: list[list[Span]] = []
    names: list[str] = []
    for k in keys:
        if k in list_keys:
            continue
        nodes = [e[k] for e in entries]
        if not all(isinstance(n, ScalarNode) for n in nodes):
            return None
        if len({str(n.value) for n in nodes}) == 1:
            continue  # the same in every item: literal text
        spans = [_scalar_span(text, n) for n in nodes if isinstance(n, ScalarNode)]
        if not all(spans) or len({n.style for n in nodes if isinstance(n, ScalarNode)}) != 1:
            return None
        fields.append([sp for sp in spans if sp is not None])
        names.append(snake_case(k))
    children: list[Found] = []
    for k in list_keys:
        seqs = [e[k] for e in entries]
        child = _child_list(text, seqs, k)
        if child is None:
            return None
        children.append(child)
    return Found(
        name=snake_case(key or "items"),
        items=outer_items,
        detector="loop.yaml_nested",
        # Above a plain list loop (0.8): it covers the inner lists too.
        confidence=0.85,
        reason=f"items of the list {key!r}, each with " + ", ".join(repr(k) for k in list_keys),
        fields=fields,
        field_names=names,
        children=children,
    )


def _child_list(text: str, seqs: list[Node], key: str) -> Found | None:
    groups: list[int] = []
    items: list[Span] = []
    fields: list[list[Span]] = []
    names: list[str] = []
    scalar = True
    inner_keys: list[str] | None = None
    styles: set[str | None] = set()
    for seq in seqs:
        if not isinstance(seq, SequenceNode) or not seq.value:
            return None  # an empty inner list has no text to template
        groups.append(len(seq.value))
        for n in seq.value:
            if isinstance(n, ScalarNode):
                if inner_keys is not None:
                    return None
                sp = _scalar_span(text, n)
                if sp is None:
                    return None
                styles.add(n.style)
                items.append(sp)
            elif isinstance(n, MappingNode):
                scalar = False
                ks = [str(a.value) for a, _ in n.value]
                if inner_keys is None:
                    inner_keys = ks
                    fields = [[] for _ in ks]
                if ks != inner_keys or not all(isinstance(b, ScalarNode) for _, b in n.value):
                    return None
                it = _trimmed(text, n.start_mark.index, n.end_mark.index)
                if it is None:
                    return None
                items.append(it)
                for f, (_, b) in zip(fields, n.value, strict=True):
                    sp = _scalar_span(text, b)
                    if sp is None:
                        return None
                    f.append(sp)
            else:
                return None
    if len(styles) > 1:
        return None
    if scalar:
        fields, names = [items], ["value"]
    else:
        names = [snake_case(k) for k in inner_keys or []]
    return Found(
        name=snake_case(key),
        items=items,
        detector="loop.yaml_nested",
        confidence=0.75,
        reason=f"the list {key!r} in each item",
        fields=fields,
        field_names=names,
        groups=groups,
    )


# Mappings whose entries are open-ended data, not a fixed set of settings.
OPEN_MAP_KEYS = frozenset(
    {
        "environment",
        "env",
        "labels",
        "annotations",
        "tags",
        "data",
        "stringdata",
        "headers",
        "variables",
        "vars",
        "extra_vars",
        "build_args",
        "args",
        "nodeselector",
        "selector",
        "matchlabels",
        "podlabels",
        "podannotations",
        "commonlabels",
        "sysctls",
    }
)
_ENV_KEY = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _maps(
    text: str, node: Node | None, key: str | None, out: list[Found], path: tuple[str, ...]
) -> None:
    """Mappings of >= 2 plain keys to scalars, under an open-ended parent key
    (``environment:``, ``labels:``) or with environment-variable keys: map loops."""
    if isinstance(node, SequenceNode):
        for k in node.value:  # a list item is a record, not the open map itself
            _maps(text, k, None, out, path)
        return
    if not isinstance(node, MappingNode):
        return
    for k, v in node.value:
        child = k.value if isinstance(k, ScalarNode) else None
        _maps(text, v, child, out, (*path, str(child)) if child is not None else path)
    entries = node.value
    if len(entries) < MIN_ITEMS:
        return
    if not all(isinstance(k, ScalarNode) and isinstance(v, ScalarNode) for k, v in entries):
        return
    keys = [str(k.value) for k, _ in entries]
    open_ended = (key or "").lower().replace("_", "") in {m.replace("_", "") for m in OPEN_MAP_KEYS}
    if not (open_ended or all(_ENV_KEY.match(k) for k in keys)):
        return
    if len({k.style for k, _ in entries}) != 1 or len({v.style for _, v in entries}) != 1:
        return
    key_spans = [_scalar_span(text, k) for k, _ in entries]
    value_spans = [_scalar_span(text, v) for _, v in entries]
    if not all(key_spans) or not all(value_spans):
        return
    ks = [s for s in key_spans if s is not None]
    vs = [s for s in value_spans if s is not None]
    items = [
        Span(start=entries[i][0].start_mark.index, end=entries[i][1].end_mark.index)
        for i in range(len(entries))
    ]
    items = [_trimmed(text, it.start, it.end) or it for it in items]
    out.append(
        Found(
            # web: {environment: ...} -> web_environment (one per compose service)
            name=snake_case(f"{path[-2]}_{key}" if key and len(path) >= 2 else key or "entries"),
            items=items,
            detector="loop.yaml_map",
            confidence=0.6,
            reason=f"entries of {key!r}" if key else "key/value entries",
            fields=[ks, vs],
            map=True,
        )
    )


def find_sql(text: str) -> list[Found]:
    out: list[Found] = []
    for stmt in insert_statements(text):
        if len(stmt.rows) < MIN_ITEMS:
            continue
        first = stmt.rows[0]
        out.append(
            Found(
                name=snake_case(stmt.table),
                items=[Span(start=r.span[0], end=r.span[1]) for r in stmt.rows],
                detector="loop.sql",
                confidence=0.85,
                reason=f"rows inserted into {stmt.table}",
                names=[
                    (Span(start=s, end=e), stmt.columns[c]) for c, (s, e) in first.values.items()
                ],
            )
        )
    return out


_LIST_START = re.compile(r"([A-Za-z_][\w.-]*)[\"']?[ \t]*[:=][ \t]*\[")
_LIST_ITEM = re.compile(
    r"\s*(?:\"((?:[^\"\\\n]|\\.)*)\"|'((?:[^'\\\n]|\\.)*)'|(-?\d+(?:\.\d+)?))\s*"
)


def find_inline_lists(text: str) -> list[Found]:
    """``KEY = ["a", "b"]`` / ``key: [1, 2]`` lists of literals (Python, HCL, TOML,
    JSON and YAML flow lists)."""
    out: list[Found] = []
    for m in _LIST_START.finditer(text):
        pos = m.end()
        items: list[Span] = []
        kinds: set[int] = set()
        while True:
            lm = _LIST_ITEM.match(text, pos)
            if not lm:
                break
            g = next(i for i in (1, 2, 3) if lm.group(i) is not None)
            if lm.end(g) <= lm.start(g):
                break
            items.append(Span(start=lm.start(g), end=lm.end(g)))
            kinds.add(g)
            pos = lm.end()
            if text.startswith(",", pos):
                pos += 1
                continue
            break
        rest = text[pos : pos + 1]
        if rest == "]" and len(items) >= MIN_ITEMS and len(kinds) == 1:
            out.append(
                Found(
                    name=snake_case(m.group(1)),
                    items=items,
                    detector="loop.list",
                    confidence=0.75,
                    reason=f"items of the list {m.group(1)}",
                )
            )
    return out


_KEYWORD_LINE = re.compile(r"^([ \t]*)([A-Za-z][\w-]*)(?=[ \t])(?![ \t]*[:=])", re.MULTILINE)


def find_keyword_lines(text: str) -> list[Found]:
    """Runs of consecutive lines with the same indent that start with the same keyword
    (``server 10.0.0.1:8080 weight=5;``)."""
    out: list[Found] = []
    run: list[tuple[str, str, Span]] = []

    def flush() -> None:
        if len(run) >= MIN_ITEMS:
            kw = run[0][1]
            out.append(
                Found(
                    name=_plural(snake_case(kw)),
                    items=[s for _, _, s in run],
                    detector="loop.lines",
                    confidence=0.6,
                    reason=f"lines starting with {kw!r}",
                )
            )
        run.clear()

    pos = 0
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        m = _KEYWORD_LINE.match(body)
        if m and body.strip():
            start = pos + len(m.group(1))
            span = Span(start=start, end=pos + len(body.rstrip()))
            key = (m.group(1), m.group(2))
            if run and (run[-1][0], run[-1][1]) == key and run[-1][2].end < start:
                gap = text[run[-1][2].end : start]
                if gap.strip() == "" and gap.count("\n") + gap.count("\r") - gap.count("\r\n") == 1:
                    run.append((*key, span))
                    pos += len(line)
                    continue
            flush()
            run.append((*key, span))
        else:
            flush()
        pos += len(line)
    flush()
    return out


_CELL = re.compile(r"\S+(?: \S+)*")  # cells are separated by two or more spaces
_RULE = re.compile(r"^[\s\-=+|:_~*]+$")
TABLE_GAP = 2


def find_tables(text: str) -> list[Found]:
    """Runs of >= 2 lines with the same columns (cells separated by 2+ spaces), each
    column left-aligned (same start) or right-aligned (same end): table rows. Fields
    are padded slots, so rows of different widths share one skeleton."""
    out: list[Found] = []
    lines: list[tuple[int, str]] = []
    pos = 0
    for raw in text.splitlines(keepends=True):
        lines.append((pos, raw.rstrip("\r\n")))
        pos += len(raw)
    cells = [[(m.start(), m.end()) for m in _CELL.finditer(body)] for _, body in lines]
    k = 0
    while k < len(lines):
        n = len(cells[k])
        j = k
        while j < len(lines) and len(cells[j]) == n and n >= 2 and not _RULE.match(lines[j][1]):
            j += 1
        if j - k >= MIN_ITEMS:
            found = _table(text, lines, cells, k, j)
            if found is not None:
                out.append(found)
        k = max(j, k + 1)
    return out


_NUMBER = re.compile(r"^[-+]?[\d,]*\.?\d+%?$")


def _table(
    text: str, lines: list[tuple[int, str]], cells: list[list[tuple[int, int]]], k: int, j: int
) -> Found | None:
    rows = range(k, j)
    n = len(cells[k])
    header = _header_cells(lines, cells, k, j, n)
    aligns: list[str] = []
    for c in range(n):
        starts = {cells[r][c][0] for r in rows}
        ends = {cells[r][c][1] for r in rows}
        numeric = all(_NUMBER.match(lines[r][1][slice(*cells[r][c])]) for r in rows)
        if len(starts) == 1 and not (len(ends) == 1 and numeric):
            aligns.append("left")
        elif len(ends) == 1:
            aligns.append("right")  # including numbers of one width: they grow leftwards
        else:
            return None
    widths = {c: {cells[r][c][1] - cells[r][c][0] for r in rows} for c in range(n)}
    if all(len(w) == 1 for w in widths.values()) and header is None:
        return None  # nothing is padded: other finders handle uniform rows

    def left_edge(c: int) -> int:
        edge = min(cells[r][c][0] for r in rows)
        return min(edge, header[c][0]) if header else edge

    # Slots: fixed column positions. A left column's slot reaches TABLE_GAP short of
    # the next column's left edge (its header or leftmost cell); a right column's slot
    # starts TABLE_GAP after the previous slot, so numbers can grow leftwards.
    slots: list[tuple[int, int]] = []
    for c in range(n):
        widest = max(cells[r][c][1] for r in rows)
        if aligns[c] == "left":
            start = cells[k][c][0]
            end = widest if c == n - 1 else left_edge(c + 1) - TABLE_GAP
            if end < widest:
                return None
        else:
            start = slots[-1][1] + TABLE_GAP if slots else left_edge(c)
            end = cells[k][c][1]
        if end <= start:
            return None
        slots.append((start, end))
    field_aligns: list[str | None] = [
        None if (c == n - 1 and aligns[c] == "left") else aligns[c] for c in range(n)
    ]
    fields: list[list[Span]] = [[] for _ in range(n)]
    items: list[Span] = []
    for r in rows:
        base, body = lines[r]
        for c, (a, b) in enumerate(slots):
            ca, cb = cells[r][c]
            if field_aligns[c] is None:
                a, b = ca, cb  # the last left column is not padded
            if not (a <= ca and cb <= b) or body[a:b].strip(" ") != body[ca:cb]:
                return None
            fields[c].append(Span(start=base + a, end=base + b))
        items.append(Span(start=base + slots[0][0], end=base + len(body)))
    names = None
    if header:
        h_line = lines[_header_index(lines, k)][1]
        names = [snake_case(h_line[a:b]) for a, b in header]
    return Found(
        name="rows",
        items=items,
        detector="loop.table",
        confidence=0.7,
        reason="rows of a table with aligned columns",
        fields=fields,
        aligns=field_aligns,
        field_names=names,
    )


def _header_index(lines: list[tuple[int, str]], k: int) -> int:
    h = k - 1
    while h >= 0 and lines[h][1].strip() and _RULE.match(lines[h][1]):
        h -= 1
    return h


def _header_cells(
    lines: list[tuple[int, str]], cells: list[list[tuple[int, int]]], k: int, j: int, n: int
) -> list[tuple[int, int]] | None:
    """The header line above rows ``k..j`` (skipping a ---- rule), if it has one cell
    per column, each overlapping its column's cells and ending before the next."""
    h = _header_index(lines, k)
    if h < 0 or len(cells[h]) != n:
        return None
    for c, (a, b) in enumerate(cells[h]):
        lo = min(cells[r][c][0] for r in range(k, j))
        hi = max(cells[r][c][1] for r in range(k, j))
        if not (a < hi and lo < b):
            return None
        if c + 1 < n and b > min(cells[r][c + 1][0] for r in range(k, j)):
            return None
    return cells[h]


FINDERS = (find_sql, find_yaml, find_inline_lists, find_keyword_lines, find_tables)


# --- candidates ------------------------------------------------------------------


def _groups(n: int, groups: list[int] | None) -> list[range]:
    if groups is None:
        return [range(n)]
    out, pos = [], 0
    for g in groups:
        out.append(range(pos, pos + g))
        pos += g
    return out


def _candidate(text: str, found: Found) -> LoopCandidate | None:
    ranges = _groups(len(found.items), found.groups)
    seps = {
        text[found.items[a].end : found.items[b].start]
        for r in ranges
        for a, b in zip(r, list(r)[1:], strict=False)
    }
    if len(seps) > 1:
        return None
    children: list[LoopCandidate] = []
    for ch in found.children:
        c = _candidate(text, ch)
        if c is None:
            return None
        children.append(c)
    child_regions = [
        [
            Span(start=ch.items[r[0]].start, end=ch.items[r[-1]].end)
            for r in _groups(len(ch.items), ch.groups)
        ]
        for ch in found.children
    ]
    if found.fields is not None:
        fields: list[list[Span]] | None = found.fields
        skeletons = set()
        for i, it in enumerate(found.items):
            holes = sorted(
                [f[i] for f in found.fields] + [regions[i] for regions in child_regions],
                key=lambda sp: sp.start,
            )
            skeletons.add(_pieces(text, it, holes))
        if len(skeletons) != 1:
            return None
    else:
        fields = infer_fields(text, found.items)
    if fields is None:
        return None
    scalar = len(fields) == 1 and fields[0] == found.items and not children
    names: list[str] = []
    types: list[VarType] = []
    for spans in fields:
        first = spans[0]
        value = text[first.start : first.end].strip(" " if found.aligns else "")
        named = next((n for region, n in found.names if region.contains(first)), None)
        if found.field_names is not None:
            name = found.field_names[len(names)]
        elif found.map:
            name = ("key", "value")[len(names)]
        elif scalar:
            name = "value"
        elif named:
            name = snake_case(named)
        else:
            name, _, _ = _name_for(text, first.start, value)
        names.append(name)
        types.append(infer_type(value))
    names = dedupe_names(names)
    aligns = found.aligns or [None] * len(fields)
    return LoopCandidate(
        name=found.name,
        item=found.name,  # placeholder; final names in detect_loops
        items=found.items,
        fields=[
            LoopFieldCandidate(name=n, type=t, spans=s, align=al)
            for n, t, s, al in zip(names, types, fields, aligns, strict=True)
        ],
        scalar=scalar and not found.map,
        map=found.map,
        auto_accept=not found.map,
        sources=[Source.REPETITION],
        detectors=[found.detector],
        confidence=found.confidence,
        reason=found.reason,
        children=children,
        groups=found.groups,
    )


def find_loops(text: str) -> list[LoopCandidate]:
    """Non-overlapping loop candidates in ``text``, most confident first wins."""
    cands = [
        c for finder in FINDERS for f in finder(text) if (c := _candidate(text, f)) is not None
    ]
    cands.sort(key=lambda c: (c.confidence, len(c.items)), reverse=True)
    kept: list[LoopCandidate] = []
    for c in cands:
        if not any(c.region.overlaps(k.region) for k in kept):
            kept.append(c)
        if len(kept) >= MAX_LOOPS:
            break
    kept.sort(key=lambda c: c.region.start)
    return kept


def _cell(text: str, f: LoopFieldCandidate, i: int) -> str:
    raw = text[f.spans[i].start : f.spans[i].end]
    return raw.rstrip(" ") if f.align == "left" else raw.lstrip(" ") if f.align == "right" else raw


def loop_values(text: str, loop: LoopCandidate, which: Sequence[int] | None = None) -> LoopValues:
    idx = list(range(len(loop.items)) if which is None else which)
    if loop.map:
        k, v = loop.fields
        return {_cell(text, k, i): _cell(text, v, i) for i in idx}
    if loop.scalar:
        return [_cell(text, loop.fields[0], i) for i in idx]
    rows: list[dict[str, Any]] = []
    for i in idx:
        row: dict[str, Any] = {f.name: _cell(text, f, i) for f in loop.fields}
        for c in loop.children:  # nested lists: this item's group
            row[c.name] = loop_values(text, c, _groups(len(c.items), c.groups)[i])
        rows.append(row)
    return rows


def _plural(name: str) -> str:
    return name if name.endswith("s") else name + "s"


def singular(name: str) -> str:
    if name.endswith("ies") and len(name) > 4:
        return name[:-3] + "y"
    if name.endswith("s") and not name.endswith("ss") and len(name) > 3:
        return name[:-1]
    return f"{name}_item"


def _item_names(names: Sequence[str], taken: Iterable[str]) -> list[str]:
    """``users`` -> ``user``; if that is taken (a scalar suggestion with the same name),
    ``user_item`` reads better than ``user_2``."""
    used = set(taken)
    out: list[str] = []
    for n in names:
        options = [singular(n), f"{n}_item"]
        pick = next((o for o in options if o not in used), None)
        if pick is None:
            pick = dedupe_names([options[1]], taken=used)[0]
        used.add(pick)
        out.append(pick)
    return out


def detect_loops(
    text: str, variants: Sequence[str], taken_names: Iterable[str] = ()
) -> list[LoopCandidate]:
    loops = find_loops(text)
    if not loops:
        return []
    in_variants = [find_loops(v) for v in variants]
    names = dedupe_names([lp.name for lp in loops], taken=taken_names)
    items = _item_names(names, taken=[*taken_names, *names])
    out: list[LoopCandidate] = []
    for lp, name, item in zip(loops, names, items, strict=True):
        variant_values: list[LoopValues | None] = []
        for vtext, vloops in zip(variants, in_variants, strict=True):
            match = next(
                (
                    v
                    for v in vloops
                    if v.name == lp.name and v.detectors == lp.detectors and v.scalar == lp.scalar
                ),
                None,
            )
            variant_values.append(_aligned(vtext, match, lp) if match else None)
        confidence = lp.confidence
        main = loop_values(text, lp)
        if any(v is not None and v != main for v in variant_values):
            confidence = max(confidence, VARIANT_CONFIDENCE)
        # Nested lists: their loop variable must not clash with anything visible.
        taken = [*taken_names, *names, *items]
        children = []
        for c in lp.children:
            (citem,) = _item_names([c.name], taken=taken)
            taken.append(citem)
            children.append(c.model_dump() | {"item": citem, "id": ""})
        out.append(
            LoopCandidate.model_validate(
                lp.model_dump()
                | {
                    "id": "",
                    "name": name,
                    "item": item,
                    "confidence": confidence,
                    "variant_values": variant_values if variants else None,
                    "children": children,
                }
            )
        )
    return out


def _aligned(text: str, other: LoopCandidate, ref: LoopCandidate) -> LoopValues | None:
    """An example's list, keyed by the main loop's field names (fields are matched by
    their inferred names; a field the example lacks makes the list unusable)."""
    vals = loop_values(text, other)
    if ref.scalar:
        return vals
    if ref.map:
        if not isinstance(vals, dict):
            return None
        return dict(vals)
    want = [f.name for f in ref.fields] + [c.name for c in ref.children]
    rows = [v for v in vals if isinstance(v, dict)]
    if any(set(want) - set(r) for r in rows):
        return None
    return [{k: r[k] for k in want} for r in rows]


def explained_notices(notices: Sequence[Notice], loops: Sequence[LoopCandidate]) -> list[Notice]:
    """Drop "structural difference" notices inside a loop: an example with more or
    fewer items is what the loop expresses."""
    return [
        n
        for n in notices
        if not (
            n.kind == "structural_difference"
            and n.span is not None
            and any(_near(lp.region, n.span) for lp in loops)
        )
    ]


def _near(region: Span, span: Span) -> bool:
    return region.start <= span.start <= region.end + 1


# --- accepting loops ---------------------------------------------------------------


def compatible_candidates(
    candidates: Sequence[Candidate], loops: Sequence[LoopCandidate]
) -> list[Candidate]:
    """Scalar candidates that can be accepted together with ``loops``: spans inside a
    loop are kept only if the candidate sits at the same place in every item (an outer
    variable such as a tenant id in every row) and not on a field."""
    out: list[Candidate] = []
    for c in candidates:
        spans = list(c.spans)
        for lp in loops:
            inside = [s for s in spans if lp.region.overlaps(s)]
            if not inside:
                continue
            fields = [s for f in lp.fields for s in f.spans] + [
                s for ch in lp.children for f in ch.fields for s in f.spans
            ]
            offsets = set()
            ok = len(inside) == len(lp.items)
            for s in inside:
                it = next((it for it in lp.items if it.contains(s)), None)
                if it is None or any(s.overlaps(fs) for fs in fields):
                    ok = False
                    break
                offsets.add((s.start - it.start, s.end - it.start))
            hit = {i for i, it in enumerate(lp.items) for s in inside if it.contains(s)}
            ok = ok and len(offsets) == 1 and len(hit) == len(lp.items)
            if not ok:
                spans = [s for s in spans if s not in inside]
        if spans:
            out.append(c if spans == c.spans else c.model_copy(update={"spans": spans}))
    return out
