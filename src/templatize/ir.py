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
from enum import StrEnum
from typing import Annotated, Final, Literal

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


LoopValues = list[dict[str, str]] | list[str] | dict[str, str]


class LoopCandidate(BaseModel):
    """A suggested repeated block (see :class:`Loop`)."""

    id: str = ""
    name: str
    item: str
    items: list[Span] = Field(min_length=1)
    fields: list[LoopFieldCandidate] = Field(min_length=1)
    scalar: bool = False
    map: bool = False
    """Key/value entries (see :class:`Loop`)."""
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

    @property
    def env_key(self) -> str:
        return self.name.upper()


class LoopField(BaseModel):
    """One hole of a loop's items whose value differs per item (``user.email``)."""

    name: str
    type: VarType = VarType.STRING
    spans: list[Span] = Field(min_length=1)
    """Absolute, one per item, in item order."""


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
    fields: list[LoopField] = Field(min_length=1)
    scalar: bool = False
    map: bool = False
    """Items are key/value entries (``LABEL: value``): two fields, the key then the
    value; the values file holds a mapping and Jinja renders
    ``{% for key, value in name.items() %}``. Their names are the loop variables."""
    sources: list[Source] = Field(default_factory=lambda: [Source.MANUAL])
    confidence: float | None = None
    description: str | None = None
    values_path: ValuesPath | None = None

    @property
    def region(self) -> Span:
        return Span(start=self.items[0].start, end=self.items[-1].end)

    def separator(self, text: str) -> str:
        return text[self.items[0].end : self.items[1].start] if len(self.items) > 1 else ""

    def rows_of(self, text: str) -> list[dict[str, str]] | list[str]:
        """One dict per item (or one string, if scalar); a map's entries as
        ``{key_field: ..., value_field: ...}`` rows."""
        if self.scalar:
            return [text[s.start : s.end] for s in self.fields[0].spans]
        return [
            {f.name: text[f.spans[i].start : f.spans[i].end] for f in self.fields}
            for i in range(len(self.items))
        ]

    def values_of(self, text: str) -> list[dict[str, str]] | list[str] | dict[str, str]:
        """What this loop renders from: a list of dicts (or strings, if scalar), or a
        mapping for a map loop."""
        if self.map:
            k, v = self.fields
            return {
                text[k.spans[i].start : k.spans[i].end]: text[v.spans[i].start : v.spans[i].end]
                for i in range(len(self.items))
            }
        if self.scalar:
            return [text[s.start : s.end] for s in self.fields[0].spans]
        return [
            {f.name: text[f.spans[i].start : f.spans[i].end] for f in self.fields}
            for i in range(len(self.items))
        ]


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

    def values(self) -> dict[str, str | list[dict[str, str]] | list[str] | dict[str, str]]:
        """Render context that reproduces the text: variables and loop lists."""
        out: dict[str, str | list[dict[str, str]] | list[str] | dict[str, str]] = {
            v.name: v.value for v in self.variables
        }
        for lp in self.loops:
            out[lp.name] = lp.values_of(self.text)
        return out


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
    for lp in ir.loops:
        for n in (lp.name, lp.item):
            validate_name(n)
            if n in names:
                raise IRValidationError(f"loop name {n!r} is already used")
            names.add(n)
        if lp.values_path is not None:
            validate_values_path(lp.values_path)
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
        seps = {ir.text[a.end : b.start] for a, b in itertools.pairwise(lp.items)}
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
            if lp.scalar and any(sp != it for sp, it in zip(f.spans, lp.items, strict=True)):
                raise IRValidationError(
                    f"scalar loop {lp.name!r}: the field must cover each whole item"
                )
        for sp in var_spans:
            if lp.region.overlaps(sp) and not any(it.contains(sp) for it in lp.items):
                raise IRValidationError(
                    f"a variable at {sp.start}-{sp.end} crosses the items of loop {lp.name!r}"
                )
        skeletons = {_skeleton(ir, lp, i) for i in range(len(lp.items))}
        if len(skeletons) != 1:
            raise IRValidationError(
                f"items of loop {lp.name!r} do not have the same shape outside their fields"
            )
        regions.append((lp.region, lp.name))
    regions.sort(key=lambda p: p[0].start)
    for (a, an), (b, bn) in itertools.pairwise(regions):
        if a.overlaps(b):
            raise IRValidationError(f"loops {an!r} and {bn!r} overlap")


def _skeleton(ir: TemplateIR, lp: Loop, i: int) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Item ``i`` as (literal pieces, hole names): equal for every item of a valid loop."""
    item = lp.items[i]
    holes: list[tuple[Span, str]] = [(f.spans[i], "." + f.name) for f in lp.fields]
    for v in ir.variables:
        holes += [(sp, v.name) for sp in v.spans if item.contains(sp)]
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
