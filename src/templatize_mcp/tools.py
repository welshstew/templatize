"""Tool implementations, independent of the MCP SDK (plain functions, unit-tested).

All of them are pure computations over their arguments: nothing is logged, stored or
fetched from the network.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from templatize.build import (
    VariableInput,
    VariableResolutionError,
    build,
    resolve_inputs,
    specs_from_candidates,
)
from templatize.emitters import TemplateRenderError, get_emitter
from templatize.emitters.jinja2 import Jinja2Emitter
from templatize.ir import MAX_INPUT_CHARS, Candidate, IRValidationError, Notice, VarType
from templatize.roundtrip import RoundTripResult
from templatize.suggest import check_limits, suggest
from templatize.validate import validate_template as core_validate

Newline = Literal["\n", "\r\n", "\r"]


class ToolInputError(ValueError):
    """Raised for bad arguments; the SDK reports it to the client as a tool error."""


class SuggestParametersResult(BaseModel):
    candidates: list[Candidate]
    notices: list[Notice]
    next_step: str = (
        "Call generate_template with the text and the variables to keep, e.g. "
        '[{"name": "db_host", "value": "db01.example.com"}]. Rename freely.'
    )


class GeneratedVariable(BaseModel):
    name: str
    env_key: str
    type: VarType
    value: str
    occurrences: int


class GenerateTemplateResult(BaseModel):
    engine: str
    template_filename: str
    template: str
    env: str
    """Contents of the .env file (KEY="value" lines)."""
    render_script: str
    """render.py: `uv run render.py > output.txt` renders template + .env."""
    variables: list[GeneratedVariable]
    roundtrip: RoundTripResult
    """ok=true means rendering the template with the .env values reproduces the input byte for byte."""
    used_suggestions: bool
    """True when no variables were given and suggestions were accepted automatically."""


class RenderTemplateResult(BaseModel):
    ok: bool
    output: str | None = None
    error: str | None = None


class ValidateTemplateResult(BaseModel):
    ok: bool
    valid_syntax: bool
    variables: list[str]
    missing_values: list[str]
    unused_values: list[str]
    rendered: bool
    matches_expected: bool | None = None
    first_difference: int | None = None
    expected_excerpt: str | None = None
    actual_excerpt: str | None = None
    error: str | None = None


def suggest_parameters(
    text: str, variants: list[str] | None = None, min_confidence: float = 0.0
) -> SuggestParametersResult:
    variants = variants or []
    try:
        res = suggest(text, variants)
    except ValueError as exc:
        raise ToolInputError(str(exc)) from None
    return SuggestParametersResult(
        candidates=[c for c in res.candidates if c.confidence >= min_confidence],
        notices=res.notices,
    )


def generate_template(
    text: str,
    variables: list[VariableInput] | None = None,
    variants: list[str] | None = None,
    min_confidence: float = 0.5,
    engine: str = "jinja2",
) -> GenerateTemplateResult:
    variants = variants or []
    try:
        check_limits(text, variants)
        if variables is None:
            cands = [
                c for c in suggest(text, variants).candidates if c.confidence >= min_confidence
            ]
            specs = specs_from_candidates(cands)
        else:
            specs = resolve_inputs(text, variables)
        result = build(text, specs, engine=engine)
    except (VariableResolutionError, IRValidationError, ValueError) as exc:
        raise ToolInputError(str(exc)) from None
    runner = result.runner[0].content if result.runner else ""
    return GenerateTemplateResult(
        engine=result.engine,
        template_filename=result.template.path,
        template=result.template.content,
        env=result.env.content,
        render_script=runner,
        variables=[
            GeneratedVariable(
                name=v.name, env_key=v.env_key, type=v.type, value=v.value, occurrences=len(v.spans)
            )
            for v in result.ir.variables
        ],
        roundtrip=result.roundtrip,
        used_suggestions=variables is None,
    )


def render_template(
    template: str,
    values: dict[str, str] | None = None,
    newline: Newline | None = None,
    engine: str = "jinja2",
) -> RenderTemplateResult:
    if len(template) > MAX_INPUT_CHARS * 2:
        raise ToolInputError("template too large")
    try:
        emitter = get_emitter(engine)
    except ValueError as exc:
        raise ToolInputError(str(exc)) from None
    lowered = {k.lower(): v for k, v in (values or {}).items()}
    try:
        if isinstance(emitter, Jinja2Emitter):
            output = emitter.render(template, lowered, newline=newline)
        else:
            output = emitter.render(template, lowered)
    except TemplateRenderError as exc:
        return RenderTemplateResult(ok=False, error=str(exc))
    return RenderTemplateResult(ok=True, output=output)


def validate_template(
    template: str,
    values: dict[str, str] | None = None,
    expected_text: str | None = None,
    engine: str = "jinja2",
) -> ValidateTemplateResult:
    if len(template) > MAX_INPUT_CHARS * 2 or (
        expected_text is not None and len(expected_text) > MAX_INPUT_CHARS
    ):
        raise ToolInputError("input too large")
    try:
        report = core_validate(template, values or {}, expected_text, engine)
    except ValueError as exc:
        raise ToolInputError(str(exc)) from None
    return ValidateTemplateResult(ok=report.ok, **report.model_dump())


__all__ = [
    "GenerateTemplateResult",
    "RenderTemplateResult",
    "SuggestParametersResult",
    "ToolInputError",
    "ValidateTemplateResult",
    "generate_template",
    "render_template",
    "suggest_parameters",
    "validate_template",
]
