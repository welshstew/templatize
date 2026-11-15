"""Request/response models. Core models (Candidate, VariableSpec, ...) are reused as-is."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from templatize.build import LoopSpec, VariableSpec
from templatize.detectors.llm import LlmReview
from templatize.emitters import EmittedFile
from templatize.helm import ChartStyle
from templatize.helm.chart import ChartRoundTrip, ChartTemplate, FilledValue
from templatize.helm.coverage import CoverageReport
from templatize.ir import MAX_INPUT_CHARS, MAX_LOOP_ITEMS, MAX_VARIANTS, Loop, Variable
from templatize.roundtrip import RoundTripResult
from templatize.schemacheck import SchemaCheck

Document = Annotated[str, Field(max_length=MAX_INPUT_CHARS)]
MAX_VARIABLES = 2_000
MAX_LOOPS = 200

Scalar = Annotated[str, Field(max_length=MAX_INPUT_CHARS)]
NestedList = Annotated[list[dict[str, Scalar]] | list[Scalar], Field(max_length=MAX_LOOP_ITEMS)]
"""A nested list inside a row (``server.ports``, ``user.ssh_authorized_keys``)."""
LoopValue = (
    Annotated[list[dict[str, Scalar | NestedList]] | list[Scalar], Field(max_length=MAX_LOOP_ITEMS)]
    | Annotated[dict[str, Scalar], Field(max_length=MAX_LOOP_ITEMS)]
)
"""A loop's value: one mapping per item (``{"email": ..., "role": ...}``, whose values
may be a nested list), one string per item for a scalar loop, or a map loop's
key -> value mapping."""


class SuggestRequest(BaseModel):
    text: Document
    variants: list[Document] = Field(default_factory=list, max_length=MAX_VARIANTS)
    document_type: Annotated[str, Field(max_length=64)] | None = None
    """Confirm the document's type (an id from GET /document-types): its format rules
    then apply. Omit to let it be detected."""
    json_schema: str | None = Field(default=None, max_length=2_000_000, alias="schema")
    """A JSON Schema (JSON or YAML text) the document follows: its formats, consts,
    defaults and titles feed the suggestions. A schema the document references by URL
    is never downloaded; pass its text here."""

    model_config = {"populate_by_name": True}


class DocumentTypeInfo(BaseModel):
    id: str
    label: str
    category: str


class LlmReviewResponse(BaseModel):
    available: bool
    """False when no LLM is configured or it is unreachable/loading: show nothing extra."""
    review: LlmReview | None = None
    detail: str | None = None


class BuildRequest(BaseModel):
    text: Document
    variables: list[VariableSpec] = Field(default_factory=list, max_length=MAX_VARIABLES)
    loops: list[LoopSpec] = Field(default_factory=list, max_length=MAX_LOOPS)
    """Repeated blocks rendered with ``{% for %}``; their values go to values.yaml."""
    engine: str = "jinja2"


class BuildResponse(BaseModel):
    engine: str
    template: EmittedFile
    env: EmittedFile
    """``.env``, or ``values.yaml`` when the template has loops."""
    runner: list[EmittedFile]
    variables: list[Variable]
    loops: list[Loop] = []
    roundtrip: RoundTripResult
    notes: list[str] = []
    """What the user should know: loops an engine cannot express, renamed variables."""


class RenderRequest(BaseModel):
    template: Annotated[str, Field(max_length=MAX_INPUT_CHARS * 2)]
    values: dict[str, Scalar | LoopValue] = Field(default_factory=dict, max_length=MAX_VARIABLES)
    engine: str = "jinja2"
    newline: Literal["\n", "\r\n", "\r"] | None = None
    """Line ending for template newlines; detected from the template when omitted."""
    check_schema: bool = False
    """Check the output against its JSON Schema (supplied, or bundled for its type)."""
    json_schema: str | None = Field(default=None, max_length=2_000_000, alias="schema")
    """The schema to check against; omit to use the bundled one for the document."""
    document_type: str | None = Field(default=None, max_length=64)
    """The document's type (as analysed): picks the bundled schema."""

    model_config = {"populate_by_name": True}


class RenderResponse(BaseModel):
    ok: bool
    output: str | None = None
    error: str | None = None
    schema_check: SchemaCheck | None = None
    """With check_schema: problems of the output against its schema (None: no schema)."""


class Limits(BaseModel):
    max_input_chars: int
    max_variants: int
    max_variables: int


class InfoResponse(BaseModel):
    name: str
    version: str
    engines: list[str]
    detectors: list[str]
    optional_detectors: dict[str, bool]
    helm: bool
    """``helm`` is installed: the helm create chart style can be checked and previewed."""
    kompose: bool = False
    """``kompose`` is installed: Docker Compose files can be converted to Helm charts."""
    mcp_http: str | None
    """Path of the MCP streamable HTTP endpoint, if enabled."""
    limits: Limits


class ErrorResponse(BaseModel):
    detail: str


class ChartRequest(BaseModel):
    text: Document
    variants: list[Document] = Field(default_factory=list, max_length=MAX_VARIANTS)
    variables: list[VariableSpec] = Field(default_factory=list, max_length=MAX_VARIABLES)
    chart_name: Annotated[str, Field(max_length=53)] | None = None
    """Defaults to the first workload's name."""
    variant_labels: list[Annotated[str, Field(max_length=32)]] | None = Field(
        default=None, max_length=MAX_VARIANTS
    )
    """Names for the per-example values files (``values-<label>.yaml``)."""
    style: ChartStyle = "exact"
    """``exact``: your manifests as templates. ``scaffold``: the ``helm create`` chart
    filled in from them (needs ``helm`` on the server for the coverage check)."""
    loops: list[LoopSpec] = Field(default_factory=list, max_length=MAX_LOOPS)
    """Exact style: lists rendered with ``range`` over values.yaml lists."""


class ChartResponse(BaseModel):
    chart_name: str
    style: ChartStyle
    files: list[EmittedFile]
    templates: list[ChartTemplate]
    variables: list[Variable]
    roundtrips: list[ChartRoundTrip]
    coverage: list[CoverageReport]
    notes: list[str]
    filled: list[FilledValue] = []
    loops: list[Loop] = []
    converted_from: Literal["compose"] | None = None
    """The input was a Docker Compose file, converted with Kompose."""
    documents: list[str] = []
    """When converted: the manifests the chart reproduces (input, then examples)."""
    ok: bool


MAX_CHART_FILES = 500


class ChartRenderRequest(BaseModel):
    templates: list[EmittedFile] = Field(max_length=MAX_CHART_FILES)
    values_yaml: Annotated[str, Field(max_length=MAX_INPUT_CHARS * 2)]
    overlay_yaml: Annotated[str, Field(max_length=MAX_INPUT_CHARS * 2)] | None = None
    """An example's values file, merged over ``values_yaml`` like ``helm -f``."""
    style: ChartStyle = "exact"
    chart_name: Annotated[str, Field(max_length=53)] = "chart"
    files: list[EmittedFile] = Field(default_factory=list, max_length=MAX_CHART_FILES)
    """Scaffold style: the whole chart (helpers included), rendered with real Helm."""


class ChartRenderResponse(BaseModel):
    ok: bool
    files: list[EmittedFile] = []
    error: str | None = None


class RoleRequest(BaseModel):
    text: Document
    variants: list[Document] = Field(default_factory=list, max_length=MAX_VARIANTS)
    variables: list[VariableSpec] = Field(default_factory=list, max_length=MAX_VARIABLES)
    loops: list[LoopSpec] = Field(default_factory=list, max_length=MAX_LOOPS)
    role_name: Annotated[str, Field(max_length=40)] = "config"
    """Role name; also the prefix of its variables (``config_db_host``)."""
    filename: Annotated[str, Field(max_length=100)] = "config"
    """Name of the rendered file (the template is ``templates/<filename>.j2``)."""
    dest: Annotated[str, Field(max_length=4096)] | None = None
    """Default for ``<role>_dest``, where the file is written."""
    variant_labels: list[Annotated[str, Field(max_length=32)]] | None = Field(
        default=None, max_length=MAX_VARIANTS
    )


class RoleResponse(BaseModel):
    role_name: str
    files: list[EmittedFile]
    roundtrip: RoundTripResult
    example_roundtrips: list[RoundTripResult]
    ok: bool
