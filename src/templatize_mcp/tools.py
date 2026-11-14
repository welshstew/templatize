"""Tool implementations, independent of the MCP SDK (plain functions, unit-tested).

All of them are pure computations over their arguments: nothing is logged, stored or
fetched from the network.
"""

from __future__ import annotations

import os
from typing import Literal

from pydantic import BaseModel

from templatize.ansible import RoleError, build_role
from templatize.build import (
    LoopSpec,
    VariableInput,
    VariableResolutionError,
    accept_suggestions,
    build,
    resolve_inputs,
    specs_from_candidates,
)
from templatize.detectors import Detector, default_detectors
from templatize.detectors import ner as ner_detector
from templatize.emitters import TemplateRenderError, get_emitter
from templatize.emitters.jinja2 import Jinja2Emitter
from templatize.helm import ChartError, ChartRoundTrip, ChartStyle
from templatize.helm import build as build_helm_chart
from templatize.helm.coverage import CoverageReport
from templatize.ir import MAX_INPUT_CHARS, Candidate, IRValidationError, Notice, VarType
from templatize.k8s import DocumentProfile
from templatize.loops import loop_values
from templatize.roundtrip import RoundTripResult
from templatize.suggest import SuggestResult, check_limits, suggest
from templatize.validate import validate_template as core_validate

Newline = Literal["\n", "\r\n", "\r"]


def _suggest(text: str, variants: list[str]) -> SuggestResult:
    """Heuristics, plus the optional NER service when TEMPLATIZE_NER_URL is set and up."""
    url = os.environ.get("TEMPLATIZE_NER_URL", "").strip()
    detectors: list[Detector] = default_detectors()
    if url and ner_detector.check_health(url):
        detectors.append(ner_detector.NerDetector(ner_detector.NerConfig(url=url)))
    return suggest(text, variants, detectors=detectors)


RenderValue = str | list[str] | list[dict[str, str]]
"""A variable's string, or a loop's list (strings, or one mapping per item)."""


class ToolInputError(ValueError):
    """Raised for bad arguments; the SDK reports it to the client as a tool error."""


NEXT_STEP = (
    "Call generate_template with the text and the variables to keep, e.g. "
    '[{"name": "db_host", "value": "db01.example.com"}]. Rename freely.'
)
NEXT_STEP_HELM = (
    "These are Kubernetes manifests: offer the user a Helm chart (generate_helm_chart), "
    "or a Jinja2 template (generate_template). Candidates carry a values_path for the chart."
)


class SuggestedLoop(BaseModel):
    name: str
    item: str
    items: int
    """How many repeated items were found."""
    fields: list[str]
    """Keys of each item (empty for a list of plain values)."""
    values: list[dict[str, str]] | list[str]
    confidence: float
    reason: str | None = None


class SuggestParametersResult(BaseModel):
    candidates: list[Candidate]
    loops: list[SuggestedLoop] = []
    """Repeated blocks (list items, SQL rows): generate_template turns them into
    {% for %} loops when called without `variables`."""
    notices: list[Notice]
    profile: DocumentProfile = DocumentProfile()
    """format="kubernetes" with helm_suggested=true means a Helm chart can be generated."""
    next_step: str = NEXT_STEP


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
    """Contents of the values file: .env (KEY="value" lines), or values.yaml when the
    template has loops (see values_filename)."""
    values_filename: str = ".env"
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
        res = _suggest(text, variants)
    except ValueError as exc:
        raise ToolInputError(str(exc)) from None
    return SuggestParametersResult(
        candidates=[c for c in res.candidates if c.confidence >= min_confidence],
        loops=[
            SuggestedLoop(
                name=lp.name,
                item=lp.item,
                items=len(lp.items),
                fields=[] if lp.scalar else [f.name for f in lp.fields],
                values=loop_values(text, lp),
                confidence=lp.confidence,
                reason=lp.reason,
            )
            for lp in res.loops
            if lp.confidence >= min_confidence
        ],
        notices=res.notices,
        profile=res.profile,
        next_step=NEXT_STEP_HELM if res.profile.helm_suggested else NEXT_STEP,
    )


class ChartFile(BaseModel):
    path: str
    content: str


class GenerateHelmChartResult(BaseModel):
    chart_name: str
    files: list[ChartFile]
    """Chart.yaml, values.yaml, values-<label>.yaml per variant, .helmignore, templates/*."""
    variables: list[GeneratedVariable]
    values_paths: dict[str, str]
    """Variable name -> dotted values.yaml path."""
    style: ChartStyle
    roundtrips: list[ChartRoundTrip]
    """Exact style: ok=true for each means the chart renders back to the input (and to
    each variant) byte for byte."""
    coverage: list[CoverageReport]
    """Scaffold style: per document, which fields render identically (percent), and
    which are lost, changed or added by the helm create scaffold."""
    notes: list[str]
    used_suggestions: bool


def generate_helm_chart(
    text: str,
    variables: list[VariableInput] | None = None,
    variants: list[str] | None = None,
    chart_name: str | None = None,
    variant_labels: list[str] | None = None,
    min_confidence: float = 0.5,
    style: ChartStyle = "exact",
) -> GenerateHelmChartResult:
    variants = variants or []
    try:
        check_limits(text, variants)
        loops: list[LoopSpec] = []
        if variables is None:
            res = _suggest(text, variants)
            cands = [c for c in res.candidates if c.confidence >= min_confidence]
            if style == "exact":
                found = [lp for lp in res.loops if lp.confidence >= min_confidence]
                specs, loops = accept_suggestions(cands, found)
            else:
                specs = specs_from_candidates(cands)
        else:
            specs = resolve_inputs(text, variables)
        result = build_helm_chart(
            style,
            text,
            specs,
            variants,
            chart_name=chart_name,
            variant_labels=variant_labels,
            loops=loops,
        )
    except (VariableResolutionError, IRValidationError, ChartError, ValueError) as exc:
        raise ToolInputError(str(exc)) from None
    return GenerateHelmChartResult(
        chart_name=result.chart_name,
        files=[ChartFile(path=f.path, content=f.content) for f in result.files],
        variables=[
            GeneratedVariable(
                name=v.name, env_key=v.env_key, type=v.type, value=v.value, occurrences=len(v.spans)
            )
            for v in result.variables
        ],
        values_paths={v.name: ".".join(v.values_path or []) for v in result.variables},
        style=result.style,
        roundtrips=result.roundtrips,
        coverage=result.coverage,
        notes=result.notes,
        used_suggestions=variables is None,
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
        loops: list[LoopSpec] = []
        if variables is None:
            res = _suggest(text, variants)
            cands = [c for c in res.candidates if c.confidence >= min_confidence]
            found = [lp for lp in res.loops if lp.confidence >= min_confidence]
            specs, loops = accept_suggestions(cands, found)
        else:
            specs = resolve_inputs(text, variables)
        result = build(text, specs, engine=engine, loops=loops)
    except (VariableResolutionError, IRValidationError, ValueError) as exc:
        raise ToolInputError(str(exc)) from None
    runner = result.runner[0].content if result.runner else ""
    return GenerateTemplateResult(
        engine=result.engine,
        template_filename=result.template.path,
        template=result.template.content,
        env=result.env.content,
        values_filename=result.env.path,
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
    values: dict[str, RenderValue] | None = None,
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
    values: dict[str, RenderValue] | None = None,
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


class GenerateAnsibleRoleResult(BaseModel):
    role_name: str
    files: list[ChartFile]
    """<role>/defaults/main.yml, tasks/main.yml, templates/<file>.j2, meta/main.yml,
    README.md, examples/<label>.yml per variant, and playbook.yml."""
    roundtrip: RoundTripResult
    """ok=true: rendering the role's template with defaults/main.yml (as Ansible reads
    it, with byte-exact template options) reproduces the input."""
    example_roundtrips: list[RoundTripResult]
    used_suggestions: bool


def generate_ansible_role(
    text: str,
    variables: list[VariableInput] | None = None,
    variants: list[str] | None = None,
    role_name: str = "config",
    filename: str = "config",
    dest: str | None = None,
    variant_labels: list[str] | None = None,
    min_confidence: float = 0.5,
) -> GenerateAnsibleRoleResult:
    variants = variants or []
    try:
        check_limits(text, variants)
        loops: list[LoopSpec] = []
        if variables is None:
            res = _suggest(text, variants)
            cands = [c for c in res.candidates if c.confidence >= min_confidence]
            found = [lp for lp in res.loops if lp.confidence >= min_confidence]
            specs, loops = accept_suggestions(cands, found)
        else:
            specs = resolve_inputs(text, variables)
        result = build_role(
            text,
            specs,
            loops,
            role_name=role_name,
            filename=filename,
            dest=dest,
            variants=variants,
            variant_labels=variant_labels,
        )
    except (VariableResolutionError, IRValidationError, RoleError, ValueError) as exc:
        raise ToolInputError(str(exc)) from None
    return GenerateAnsibleRoleResult(
        role_name=result.role_name,
        files=[ChartFile(path=f.path, content=f.content) for f in result.files],
        roundtrip=result.roundtrip,
        example_roundtrips=result.example_roundtrips,
        used_suggestions=variables is None,
    )
