"""Turn text + chosen variables into template, .env, runner script and a round-trip verdict."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Literal

from pydantic import BaseModel, Field

from templatize.emitters import EmittedFile, get_emitter
from templatize.envfile import serialize_env
from templatize.ir import (
    Candidate,
    IRValidationError,
    Source,
    Span,
    TemplateIR,
    Variable,
    VarType,
)
from templatize.naming import infer_type
from templatize.roundtrip import RoundTripResult, check_roundtrip
from templatize.suggest import check_limits


class VariableSpec(BaseModel):
    """A variable as chosen by a user/client. ``value`` is optional and, if given,
    must equal the text at every span."""

    name: str
    spans: list[Span] = Field(min_length=1)
    type: VarType | None = None
    sources: list[Source] = Field(default_factory=lambda: [Source.MANUAL])
    value: str | None = None
    confidence: float | None = None
    description: str | None = None


class BuildResult(BaseModel):
    engine: str
    ir: TemplateIR
    template: EmittedFile
    env: EmittedFile
    runner: list[EmittedFile]
    roundtrip: RoundTripResult

    @property
    def files(self) -> list[EmittedFile]:
        return [self.template, self.env, *self.runner]


def make_ir(text: str, specs: Sequence[VariableSpec]) -> TemplateIR:
    check_limits(text)
    variables: list[Variable] = []
    for spec in specs:
        first = spec.spans[0]
        if first.end > len(text):
            raise IRValidationError(
                f"span {first.start}-{first.end} of {spec.name!r} is outside the text"
            )
        value = text[first.start : first.end]
        if spec.value is not None and spec.value != value:
            raise IRValidationError(
                f"value for {spec.name!r} ({spec.value!r}) does not match the text at "
                f"{first.start}-{first.end} ({value!r})"
            )
        variables.append(
            Variable(
                name=spec.name,
                type=spec.type or infer_type(value),
                value=value,
                spans=sorted(spec.spans, key=lambda s: s.start),
                sources=spec.sources,
                confidence=spec.confidence,
                description=spec.description,
            )
        )
    return TemplateIR(text=text, variables=variables)


def specs_from_candidates(candidates: Iterable[Candidate]) -> list[VariableSpec]:
    return [
        VariableSpec(
            name=c.name,
            spans=c.spans,
            type=c.type,
            sources=c.sources,
            value=c.value,
            confidence=c.confidence,
            description=c.reason,
        )
        for c in candidates
    ]


def build_ir(ir: TemplateIR, engine: str = "jinja2") -> BuildResult:
    emitter = get_emitter(engine)
    template = emitter.emit(ir)
    return BuildResult(
        engine=engine,
        ir=ir,
        template=EmittedFile(path=emitter.template_filename, content=template),
        env=EmittedFile(path=".env", content=serialize_env(ir)),
        runner=emitter.runner_files(ir),
        roundtrip=check_roundtrip(ir, emitter, template),
    )


def build(text: str, variables: Sequence[VariableSpec], engine: str = "jinja2") -> BuildResult:
    return build_ir(make_ir(text, variables), engine)


_WORDISH = re.compile(r"\w")


def find_occurrences(text: str, value: str, *, whole_word: bool = True) -> list[Span]:
    """All non-overlapping occurrences of ``value`` (helper for "apply to all occurrences")."""
    if not value:
        return []
    spans: list[Span] = []
    pos = 0
    while (idx := text.find(value, pos)) != -1:
        end = idx + len(value)
        ok = True
        if whole_word:
            before = text[idx - 1] if idx > 0 else ""
            after = text[end] if end < len(text) else ""
            if (_WORDISH.match(value[0]) and before and _WORDISH.match(before)) or (
                _WORDISH.match(value[-1]) and after and _WORDISH.match(after)
            ):
                ok = False
        if ok:
            spans.append(Span(start=idx, end=end))
            pos = end
        else:
            pos = idx + 1
    return spans


class VariableInput(BaseModel):
    """A variable identified by *value* rather than offsets: friendlier for LLM/MCP
    clients, which are unreliable at counting characters.

    Exactly one of ``value`` or ``spans`` must be given. With ``value``, the library
    finds the occurrences in the text (whole-word where the value starts/ends with a
    word character; falls back to substring matching if that finds nothing) and keeps
    ``"all"`` of them or the listed 0-based occurrence indexes."""

    name: str
    value: str | None = None
    occurrences: Literal["all"] | list[int] = "all"
    spans: list[Span] | None = None
    type: VarType | None = None
    description: str | None = None


class VariableResolutionError(ValueError):
    pass


def resolve_inputs(text: str, inputs: Sequence[VariableInput]) -> list[VariableSpec]:
    specs: list[VariableSpec] = []
    claimed: list[Span] = []
    for inp in inputs:
        if (inp.value is None) == (inp.spans is None):
            raise VariableResolutionError(f"{inp.name!r}: give exactly one of 'value' or 'spans'")
        if inp.spans is not None:
            spans = list(inp.spans)
        else:
            assert inp.value is not None
            found = find_occurrences(text, inp.value) or find_occurrences(
                text, inp.value, whole_word=False
            )
            if not found:
                raise VariableResolutionError(
                    f"{inp.name!r}: value {inp.value!r} does not occur in the text"
                )
            # Skip occurrences already taken by earlier variables (e.g. a longer value).
            free = [s for s in found if not any(s.overlaps(c) for c in claimed)]
            if inp.occurrences == "all":
                spans = free
            else:
                bad = [i for i in inp.occurrences if not 0 <= i < len(found)]
                if bad:
                    raise VariableResolutionError(
                        f"{inp.name!r}: occurrence index {bad[0]} out of range "
                        f"(value occurs {len(found)} times)"
                    )
                spans = [found[i] for i in inp.occurrences]
            if not spans:
                raise VariableResolutionError(
                    f"{inp.name!r}: every occurrence of {inp.value!r} overlaps another variable"
                )
        claimed.extend(spans)
        specs.append(
            VariableSpec(
                name=inp.name,
                spans=spans,
                type=inp.type,
                value=inp.value,
                description=inp.description,
            )
        )
    return specs
