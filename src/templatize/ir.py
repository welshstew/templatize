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
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

IR_VERSION = 1
MAX_INPUT_CHARS = 1_000_000
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


class IRValidationError(ValueError):
    pass


class TemplateIR(BaseModel):
    ir_version: Literal[1] = 1
    text: str
    variables: list[Variable]

    @model_validator(mode="after")
    def _check(self) -> TemplateIR:
        validate_ir(self)
        return self

    def sorted_occurrences(self) -> list[tuple[Span, Variable]]:
        occ = [(s, v) for v in self.variables for s in v.spans]
        occ.sort(key=lambda p: p[0].start)
        return occ


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
