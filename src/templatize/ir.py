"""Engine-neutral intermediate representation (IR).

Offsets are half-open ``[start, end)`` indices into the original text, counted in
Unicode code points (i.e. Python ``str`` indices). Clients in other languages must
convert (JavaScript strings index in UTF-16 code units).

Values are always strings: the exact substring of the source text. ``VarType`` is
metadata used for naming and UI hints only, which keeps round-tripping exact
(``port: 0080`` stays ``0080``).
"""

from __future__ import annotations

import hashlib
import itertools
import keyword
import re
from collections.abc import Iterable
from enum import StrEnum
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

IR_VERSION: Final = 2
MAX_INPUT_CHARS = 1_000_000
MAX_LOOP_ITEMS = 10_000
MAX_VARIANTS = 3

NAME_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
# One segment of a nested values path (Helm ``.Values.a.b``): a Go identifier.
VALUES_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MAX_VALUES_DEPTH = 8

ValuesPath = Annotated[list[str], Field(min_length=1, max_length=MAX_VALUES_DEPTH)]
"""Where a variable lives in a nested values file, e.g. ``["image", "tag"]``.
Optional metadata: engines with flat values (Jinja2 + ``.env``) ignore it."""

# Names that would collide with Jinja2 literals, operators or default globals, or
# that are Python keywords (render.py passes values as keyword-style context).
RESERVED_NAMES: frozenset[str] = frozenset(
    {
        "true",
        "false",
        "none",
        "and",
        "or",
        "not",
        "in",
        "is",
        "if",
        "else",
        "elif",
        "endif",
        "for",
        "endfor",
        "block",
        "endblock",
        "extends",
        "include",
        "import",
        "macro",
        "call",
        "filter",
        "set",
        "with",
        "without",
        "context",
        "raw",
        "endraw",
        "recursive",
        "loop",
        "self",
        "super",
        "varargs",
        "kwargs",
        "caller",
        "range",
        "dict",
        "lipsum",
        "cycler",
        "joiner",
        "namespace",
    }
    | {k.lower() for k in keyword.kwlist}
)


class Source(StrEnum):
    REGEX = "regex"
    KEY_VALUE = "key_value"
    REPETITION = "repetition"
    DIFF = "diff"
    NER = "ner"
    LLM = "llm"
    MANUAL = "manual"


class VarType(StrEnum):
    STRING = "string"
    INT = "int"
    FLOAT = "float"
    BOOL = "bool"
    IPV4 = "ipv4"
    IPV6 = "ipv6"
    CIDR = "cidr"
    EMAIL = "email"
    URL = "url"
    HOSTNAME = "hostname"
    PORT = "port"
    UUID = "uuid"
    DATE = "date"
    DATETIME = "datetime"
    TIME = "time"
    ADDRESS = "address"
    PHONE = "phone"
    SEMVER = "semver"
    PATH = "path"
    MAC = "mac"


NameOrigin = Literal["manual", "schema", "key", "llm", "context", "type", "generic"]

# Higher is better when choosing between names proposed by different detectors.
# "schema": the field's meaning is known (e.g. a Kubernetes Deployment's replicas).
NAME_ORIGIN_RANK: dict[str, int] = {
    "manual": 7,
    "schema": 6,
    "key": 5,
    "llm": 4,
    "context": 3,
    "type": 2,
    "generic": 1,
}


class Span(BaseModel):
    model_config = ConfigDict(frozen=True)

    start: int = Field(ge=0)
    end: int = Field(ge=0)

    @model_validator(mode="after")
    def _non_empty(self) -> Span:
        if self.end <= self.start:
            raise ValueError(f"span end ({self.end}) must be greater than start ({self.start})")
        return self

    def overlaps(self, other: Span) -> bool:
        return self.start < other.end and other.start < self.end

    def contains(self, other: Span) -> bool:
        return self.start <= other.start and other.end <= self.end

    @property
    def length(self) -> int:
        return self.end - self.start


class Candidate(BaseModel):
    """A suggestion produced by one or more detectors."""

    id: str = ""
    name: str
    name_origin: NameOrigin = "generic"
    type: VarType = VarType.STRING
    value: str
    spans: list[Span]
    sources: list[Source]
    detectors: list[str]
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str | None = None
    variant_values: list[str] | None = None
    values_path: ValuesPath | None = None
    choices: list[str] | None = None
    """The values its schema allows (an ``enum``), when known."""
    accepts: list[str] | None = None
    """The JSON types its schema allows (``["integer", "string"]`` for a Kubernetes
    targetPort), when known; they override the type guessed from the value."""

    @model_validator(mode="after")
    def _fill_id(self) -> Candidate:
        if not self.id:
            self.id = candidate_id(self.value, self.spans)
        return self


class LoopFieldCandidate(BaseModel):
    name: str
    type: VarType = VarType.STRING
    spans: list[Span] = Field(min_length=1)
    """One per item, in item order."""
    align: Literal["left", "right"] | None = None
    """A padded table column (see :class:`LoopField`)."""


LoopValues = list[dict[str, Any]] | list[str] | dict[str, str]
"""A loop's values: rows (with nested lists inside), strings, or a mapping."""


class LoopCandidate(BaseModel):
    """A suggested repeated block (see :class:`Loop`)."""

    id: str = ""
    name: str
    item: str
    items: list[Span] = Field(min_length=1)
    fields: list[LoopFieldCandidate] = Field(default_factory=list)
    """Empty only when the items differ just in their nested lists."""
    scalar: bool = False
    map: bool = False
    """Key/value entries (see :class:`Loop`)."""
    children: list[LoopCandidate] = []
    """Nested lists inside each item (one level)."""
    groups: list[int] | None = None
    """For a nested list: items per parent item."""
    auto_accept: bool = True
    """False for loops that change the shape of the output a lot (maps): offered,
    but not taken by "accept all"."""
    sources: list[Source]
    detectors: list[str]
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str | None = None
    variant_values: list[LoopValues | None] | None = None
    """The list in each example document (None where the example has no such loop)."""

    @model_validator(mode="after")
    def _fill_id(self) -> LoopCandidate:
        if not self.id:
            self.id = "loop-" + candidate_id(self.name, self.items)
        return self

    @property
    def region(self) -> Span:
        return Span(start=self.items[0].start, end=self.items[-1].end)


def candidate_id(value: str, spans: list[Span]) -> str:
    key = value + "|" + ",".join(f"{s.start}-{s.end}" for s in sorted(spans, key=_span_key))
    return hashlib.sha1(key.encode("utf-8", "surrogatepass")).hexdigest()[:12]


def _span_key(s: Span) -> tuple[int, int]:
    return (s.start, s.end)


class Notice(BaseModel):
    """Something the user should know about that is not a candidate."""

    kind: Literal["structural_difference", "detector_unavailable", "info"]
    message: str
    span: Span | None = None


class Variable(BaseModel):
    """An accepted parameter."""

    name: str
    type: VarType = VarType.STRING
    value: str
    spans: list[Span] = Field(min_length=1)
    sources: list[Source] = Field(default_factory=lambda: [Source.MANUAL])
    confidence: float | None = None
    description: str | None = None
    values_path: ValuesPath | None = None
    choices: list[str] | None = None
    """The values its schema allows; charts and roles refuse others."""
    accepts: list[str] | None = None
    """The JSON types its schema allows (see :class:`Candidate`)."""

    @property
    def env_key(self) -> str:
        return self.name.upper()


class LoopField(BaseModel):
    """One hole of a loop's items whose value differs per item (``user.email``)."""

    name: str
    type: VarType = VarType.STRING
    spans: list[Span] = Field(min_length=1)
    """Absolute, one per item, in item order."""
    align: Literal["left", "right"] | None = None
    """A padded table column: each span is the whole slot (same width in every row)
    and the value is the cell without its padding spaces; rendered with
    ``"%-Ns"|format(...)`` (left) or ``"%Ns"`` (right), N = the slot width."""

    def value_at(self, text: str, i: int) -> str:
        raw = text[self.spans[i].start : self.spans[i].end]
        if self.align == "left":
            return raw.rstrip(" ")
        if self.align == "right":
            return raw.lstrip(" ")
        return raw

    @property
    def width(self) -> int:
        return self.spans[0].end - self.spans[0].start


class Loop(BaseModel):
    """A repeated block: ``items`` share one shape (the skeleton) and are joined by an
    identical separator. Rendered as ``{% for item in name %}...{% endfor %}``.

    Holes in an item are the loop's fields (one span per item) and outer variables
    (a :class:`Variable` with a span at the same place in every item). A *scalar*
    loop has one field covering each whole item: its values are a list of strings.
    """

    name: str
    item: str
    items: list[Span] = Field(min_length=1, max_length=MAX_LOOP_ITEMS)
    fields: list[LoopField] = Field(default_factory=list)
    """Empty only for a loop whose items differ just in their nested lists."""
    scalar: bool = False
    map: bool = False
    """Items are key/value entries (``LABEL: value``): two fields, the key then the
    value; the values file holds a mapping and Jinja renders
    ``{% for key, value in name.items() %}``. Their names are the loop variables."""
    sources: list[Source] = Field(default_factory=lambda: [Source.MANUAL])
    confidence: float | None = None
    description: str | None = None
    values_path: ValuesPath | None = None
    parent: str | None = None
    """Nested list: the loop whose items contain this one; ``name`` is then a key of
    the parent's rows (``server.ports``) rather than a template variable."""
    groups: list[int] | None = None
    """Nested list: how many of ``items`` belong to each parent item, in order (>= 1)."""

    @property
    def region(self) -> Span:
        return Span(start=self.items[0].start, end=self.items[-1].end)

    def group_ranges(self) -> list[range]:
        """Item indexes per parent item (one range covering all items if not nested)."""
        if self.groups is None:
            return [range(len(self.items))]
        out, pos = [], 0
        for n in self.groups:
            out.append(range(pos, pos + n))
            pos += n
        return out

    def group_region(self, g: int) -> Span:
        r = self.group_ranges()[g]
        return Span(start=self.items[r[0]].start, end=self.items[r[-1]].end)

    def separator(self, text: str) -> str:
        """The text between consecutive items (within one parent item, if nested)."""
        for r in self.group_ranges():
            if len(r) > 1:
                return text[self.items[r[0]].end : self.items[r[1]].start]
        return ""

    def rows_of(self, text: str) -> list[dict[str, str]] | list[str]:
        """One dict per item (or one string, if scalar); a map's entries as
        ``{key_field: ..., value_field: ...}`` rows."""
        if self.scalar:
            return [self.fields[0].value_at(text, i) for i in range(len(self.items))]
        return [{f.name: f.value_at(text, i) for f in self.fields} for i in range(len(self.items))]

    def values_of(self, text: str) -> list[dict[str, str]] | list[str] | dict[str, str]:
        """What this loop renders from: a list of dicts (or strings, if scalar), or a
        mapping for a map loop."""
        if self.map:
            k, v = self.fields
            return {k.value_at(text, i): v.value_at(text, i) for i in range(len(self.items))}
        if self.scalar:
            return [self.fields[0].value_at(text, i) for i in range(len(self.items))]
        return [{f.name: f.value_at(text, i) for f in self.fields} for i in range(len(self.items))]


class IRValidationError(ValueError):
    pass


class TemplateIR(BaseModel):
    ir_version: Literal[1, 2] = IR_VERSION
    text: str
    variables: list[Variable]
    loops: list[Loop] = []

    @model_validator(mode="after")
    def _check(self) -> TemplateIR:
        validate_ir(self)
        return self

    def sorted_occurrences(self) -> list[tuple[Span, Variable]]:
        occ = [(s, v) for v in self.variables for s in v.spans]
        occ.sort(key=lambda p: p[0].start)
        return occ

    def values(self) -> dict[str, Any]:
        """Render context that reproduces the text: variables and loop lists."""
        out: dict[str, Any] = {v.name: v.value for v in self.variables}
        for lp in self.loops:
            if lp.parent is None:
                out[lp.name] = self.loop_value(lp)
        return out

    def children(self, lp: Loop) -> list[Loop]:
        return [c for c in self.loops if c.parent == lp.name]

    def loop_rows(self, lp: Loop, which: Iterable[int] | None = None) -> list[Any]:
        """Rows of ``lp`` (all items, or ``which``), nested lists included as fields."""
        idx = list(range(len(lp.items)) if which is None else which)
        kids = self.children(lp)
        if lp.scalar and not kids:
            return [lp.fields[0].value_at(self.text, i) for i in idx]
        rows: list[Any] = []
        for i in idx:
            row: dict[str, Any] = {f.name: f.value_at(self.text, i) for f in lp.fields}
            for c in kids:
                row[c.name] = self.loop_rows(c, c.group_ranges()[i])
            rows.append(row)
        return rows

    def loop_value(self, lp: Loop) -> Any:
        """What ``lp`` renders from: its rows, or a mapping for a map loop."""
        return lp.values_of(self.text) if lp.map else self.loop_rows(lp)


def validate_name(name: str) -> None:
    if not NAME_RE.match(name):
        raise IRValidationError(
            f"invalid variable name {name!r}: use lowercase snake_case (a-z, 0-9, _), "
            "not starting with a digit"
        )
    if name in RESERVED_NAMES:
        raise IRValidationError(f"variable name {name!r} is reserved")


def validate_values_path(path: list[str]) -> None:
    for key in path:
        if not VALUES_KEY_RE.match(key):
            raise IRValidationError(
                f"invalid values key {key!r} in {'.'.join(path)!r}: use letters, digits and _, "
                "not starting with a digit"
            )


def validate_ir(ir: TemplateIR) -> None:
    if len(ir.text) > MAX_INPUT_CHARS:
        raise IRValidationError(f"input exceeds {MAX_INPUT_CHARS} characters")
    seen: set[str] = set()
    all_spans: list[tuple[Span, str]] = []
    for var in ir.variables:
        validate_name(var.name)
        if var.values_path is not None:
            validate_values_path(var.values_path)
        if var.name in seen:
            raise IRValidationError(f"duplicate variable name {var.name!r}")
        seen.add(var.name)
        for span in var.spans:
            if span.end > len(ir.text):
                raise IRValidationError(
                    f"span {span.start}-{span.end} of {var.name!r} is outside the text "
                    f"(length {len(ir.text)})"
                )
            actual = ir.text[span.start : span.end]
            if actual != var.value:
                raise IRValidationError(
                    f"span {span.start}-{span.end} of {var.name!r} contains {actual!r}, "
                    f"expected {var.value!r}"
                )
            all_spans.append((span, var.name))
    all_spans.sort(key=lambda p: (p[0].start, p[0].end))
    for (a, an), (b, bn) in itertools.pairwise(all_spans):
        if a.overlaps(b):
            raise IRValidationError(
                f"overlapping spans: {an!r} {a.start}-{a.end} and {bn!r} {b.start}-{b.end}"
            )
    _validate_loops(ir, seen, [sp for sp, _ in all_spans])


def _validate_loops(ir: TemplateIR, names: set[str], var_spans: list[Span]) -> None:
    regions: list[tuple[Span, str]] = []
    by_name = {lp.name: lp for lp in ir.loops if lp.parent is None}
    for lp in ir.loops:  # nested lists' groups first: parents' shapes depend on them
        if lp.parent is not None and lp.groups is not None and sum(lp.groups) != len(lp.items):
            raise IRValidationError(f"nested loop {lp.name!r}: groups do not add up")
    for lp in ir.loops:
        parent = by_name.get(lp.parent) if lp.parent is not None else None
        if lp.parent is not None:
            if parent is None or parent.parent is not None:
                raise IRValidationError(
                    f"nested loop {lp.name!r}: its parent {lp.parent!r} must be a top-level loop"
                )
            if not VALUES_KEY_RE.match(lp.name) or lp.name in {f.name for f in parent.fields}:
                raise IRValidationError(f"invalid nested list name {lp.name!r} in {parent.name!r}")
            groups = lp.groups or []
            if len(groups) != len(parent.items) or any(n < 1 for n in groups):
                raise IRValidationError(
                    f"nested loop {lp.name!r} needs >= 1 item in each of the "
                    f"{len(parent.items)} items of {parent.name!r}"
                )
            if sum(groups) != len(lp.items):
                raise IRValidationError(f"nested loop {lp.name!r}: groups do not add up")
            for g, r in enumerate(lp.group_ranges()):
                if not all(parent.items[g].contains(lp.items[i]) for i in r):
                    raise IRValidationError(
                        f"nested loop {lp.name!r}: items must lie in their {parent.item!r}"
                    )
            to_check: tuple[str, ...] = (lp.item,)
        else:
            if lp.groups is not None:
                raise IRValidationError(f"loop {lp.name!r}: groups need a parent loop")
            to_check = (lp.name, lp.item)
        for n in to_check:
            validate_name(n)
            if n in names:
                raise IRValidationError(f"loop name {n!r} is already used")
            names.add(n)
        if lp.values_path is not None:
            validate_values_path(lp.values_path)
        if not lp.fields and not ir.children(lp):
            raise IRValidationError(f"loop {lp.name!r} has no fields")
        if lp.scalar and len(lp.fields) != 1:
            raise IRValidationError(f"scalar loop {lp.name!r} must have exactly one field")
        if lp.map:
            if lp.scalar or len(lp.fields) != 2:
                raise IRValidationError(f"map loop {lp.name!r} needs a key field and a value field")
            keys = [ir.text[s.start : s.end] for s in lp.fields[0].spans]
            if len(set(keys)) != len(keys):
                raise IRValidationError(f"map loop {lp.name!r} has duplicate keys")
            for f in lp.fields:  # they are template variables in the loop
                validate_name(f.name)
                if f.name in names:
                    raise IRValidationError(f"loop variable {f.name!r} is already used")
        for a, b in itertools.pairwise(lp.items):
            if b.start < a.end:
                raise IRValidationError(f"items of loop {lp.name!r} overlap or are out of order")
        if lp.items[-1].end > len(ir.text):
            raise IRValidationError(f"loop {lp.name!r} is outside the text")
        seps = {
            ir.text[lp.items[a].end : lp.items[b].start]
            for r in lp.group_ranges()
            for a, b in itertools.pairwise(r)
        }
        if len(seps) > 1:
            raise IRValidationError(
                f"loop {lp.name!r}: the text between items differs ({sorted(seps)!r})"
            )
        field_names = [f.name for f in lp.fields]
        if len(set(field_names)) != len(field_names):
            raise IRValidationError(f"loop {lp.name!r} has duplicate field names")
        for f in lp.fields:
            if not VALUES_KEY_RE.match(f.name) or f.name in RESERVED_NAMES:
                raise IRValidationError(f"invalid field name {f.name!r} in loop {lp.name!r}")
            if len(f.spans) != len(lp.items):
                raise IRValidationError(
                    f"field {f.name!r} of loop {lp.name!r} needs one span per item "
                    f"({len(lp.items)}), got {len(f.spans)}"
                )
            for sp, it in zip(f.spans, lp.items, strict=True):
                if not it.contains(sp):
                    raise IRValidationError(
                        f"field {f.name!r} of loop {lp.name!r}: span {sp.start}-{sp.end} is "
                        f"outside its item {it.start}-{it.end}"
                    )
            if f.align is not None and len({sp.end - sp.start for sp in f.spans}) != 1:
                raise IRValidationError(
                    f"padded field {f.name!r} of loop {lp.name!r} needs the same width in every item"
                )
            if lp.scalar and any(sp != it for sp, it in zip(f.spans, lp.items, strict=True)):
                raise IRValidationError(
                    f"scalar loop {lp.name!r}: the field must cover each whole item"
                )
        group_regions = [lp.group_region(g) for g in range(len(lp.group_ranges()))]
        for sp in var_spans:
            if any(r.overlaps(sp) for r in group_regions) and not any(
                it.contains(sp) for it in lp.items
            ):
                raise IRValidationError(
                    f"a variable at {sp.start}-{sp.end} crosses the items of loop {lp.name!r}"
                )
        skeletons = {_skeleton(ir, lp, i) for i in range(len(lp.items))}
        if len(skeletons) != 1:
            raise IRValidationError(
                f"items of loop {lp.name!r} do not have the same shape outside their fields"
            )
        if lp.parent is None:
            regions.append((lp.region, lp.name))
    regions.sort(key=lambda p: p[0].start)
    for (a, an), (b, bn) in itertools.pairwise(regions):
        if a.overlaps(b):
            raise IRValidationError(f"loops {an!r} and {bn!r} overlap")


def _skeleton(ir: TemplateIR, lp: Loop, i: int) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Item ``i`` as (literal pieces, hole names): equal for every item of a valid loop."""
    item = lp.items[i]
    holes: list[tuple[Span, str]] = [(f.spans[i], "." + f.name) for f in lp.fields]
    nested = [(c.group_region(i), "." + c.name) for c in ir.children(lp)]
    holes += nested
    for v in ir.variables:
        holes += [
            (sp, v.name)
            for sp in v.spans
            if item.contains(sp) and not any(r.contains(sp) for r, _ in nested)
        ]
    holes.sort(key=lambda h: h[0].start)
    for (a, an), (b, bn) in itertools.pairwise(holes):
        if a.overlaps(b):
            raise IRValidationError(f"{an!r} and {bn!r} overlap in loop {lp.name!r}")
    pieces: list[str] = []
    pos = item.start
    for sp, _ in holes:
        pieces.append(ir.text[pos : sp.start])
        pos = sp.end
    pieces.append(ir.text[pos : item.end])
    return tuple(pieces), tuple(n for _, n in holes)
