"""Request/response models. Core models (Candidate, VariableSpec, ...) are reused as-is."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from templatize.build import VariableSpec
from templatize.detectors.llm import LlmReview
from templatize.emitters import EmittedFile
from templatize.ir import MAX_INPUT_CHARS, MAX_VARIANTS, Variable
from templatize.roundtrip import RoundTripResult

Document = Annotated[str, Field(max_length=MAX_INPUT_CHARS)]
MAX_VARIABLES = 2_000


class SuggestRequest(BaseModel):
    text: Document
    variants: list[Document] = Field(default_factory=list, max_length=MAX_VARIANTS)


class LlmReviewResponse(BaseModel):
    available: bool
    """False when no LLM is configured or it is unreachable/loading: show nothing extra."""
    review: LlmReview | None = None
    detail: str | None = None


class BuildRequest(BaseModel):
    text: Document
    variables: list[VariableSpec] = Field(default_factory=list, max_length=MAX_VARIABLES)
    engine: str = "jinja2"


class BuildResponse(BaseModel):
    engine: str
    template: EmittedFile
    env: EmittedFile
    runner: list[EmittedFile]
    variables: list[Variable]
    roundtrip: RoundTripResult


class RenderRequest(BaseModel):
    template: Annotated[str, Field(max_length=MAX_INPUT_CHARS * 2)]
    values: dict[str, Annotated[str, Field(max_length=MAX_INPUT_CHARS)]] = Field(
        default_factory=dict, max_length=MAX_VARIABLES
    )
    engine: str = "jinja2"
    newline: Literal["\n", "\r\n", "\r"] | None = None
    """Line ending for template newlines; detected from the template when omitted."""


class RenderResponse(BaseModel):
    ok: bool
    output: str | None = None
    error: str | None = None


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
    mcp_http: str | None
    """Path of the MCP streamable HTTP endpoint, if enabled."""
    limits: Limits


class ErrorResponse(BaseModel):
    detail: str
