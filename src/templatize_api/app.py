"""FastAPI application.

Privacy: request bodies are never logged or stored. Errors are logged by exception
type and path only, because exception messages may quote user text. Validation
errors are returned without the ``input`` echo FastAPI includes by default.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError

from templatize import __version__
from templatize.build import BuildResult, build
from templatize.bundle import make_zip
from templatize.detectors import HEURISTIC_DETECTORS
from templatize.emitters import EMITTERS, TemplateRenderError, get_emitter
from templatize.emitters.base import Emitter
from templatize.emitters.jinja2 import Jinja2Emitter
from templatize.ir import MAX_INPUT_CHARS, MAX_VARIANTS, IRValidationError
from templatize.suggest import InputLimitError, SuggestResult, suggest
from templatize_api.middleware import BodyLimitMiddleware, SecurityHeadersMiddleware
from templatize_api.schemas import (
    MAX_VARIABLES,
    BuildRequest,
    BuildResponse,
    ErrorResponse,
    InfoResponse,
    Limits,
    RenderRequest,
    RenderResponse,
    SuggestRequest,
)
from templatize_api.settings import Settings

log = logging.getLogger("templatize.api")

ERRORS: dict[int | str, dict[str, Any]] = {
    413: {"model": ErrorResponse, "description": "Input too large"},
    422: {"model": ErrorResponse, "description": "Invalid input"},
}


class ApiError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        self.status = status
        self.detail = detail


def _validation_message(exc: ValidationError) -> str:
    msgs = []
    for err in exc.errors():
        msg = str(err.get("msg", "invalid"))
        msgs.append(msg.removeprefix("Value error, "))
    return "; ".join(msgs)


def _run_build(req: BuildRequest) -> BuildResult:
    try:
        return build(req.text, req.variables, engine=req.engine)
    except InputLimitError as exc:
        raise ApiError(413, str(exc)) from None
    except ValidationError as exc:
        raise ApiError(422, _validation_message(exc)) from None
    except (IRValidationError, ValueError) as exc:  # includes UnknownEngineError
        raise ApiError(422, str(exc)) from None


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    app = FastAPI(
        title="templatize",
        version=__version__,
        summary="Turn plain text into templates with a round-trip guarantee.",
        # Swagger UI/ReDoc load assets from a CDN: disabled for offline use.
        docs_url=None,
        redoc_url=None,
    )
    app.state.settings = settings

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def _request_validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {"loc": list(e.get("loc", ())), "msg": e.get("msg"), "type": e.get("type")}
            for e in exc.errors()
        ]
        return JSONResponse({"detail": errors}, status_code=422)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.error("unhandled %s on %s %s", type(exc).__name__, request.method, request.url.path)
        return JSONResponse({"detail": "internal error"}, status_code=500)

    @app.get("/healthz", tags=["meta"])
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/info", tags=["meta"])
    def info() -> InfoResponse:
        return InfoResponse(
            name="templatize",
            version=__version__,
            engines=sorted(EMITTERS),
            detectors=list(HEURISTIC_DETECTORS),
            optional_detectors={"ner": False, "llm": False},
            limits=Limits(
                max_input_chars=MAX_INPUT_CHARS,
                max_variants=MAX_VARIANTS,
                max_variables=MAX_VARIABLES,
            ),
        )

    @app.post("/suggest", tags=["templating"], responses=ERRORS)
    def suggest_endpoint(req: SuggestRequest) -> SuggestResult:
        """Analyse text (plus optional variant documents) and return parameter candidates.

        Spans are Unicode code-point offsets into ``text``."""
        try:
            return suggest(req.text, req.variants)
        except InputLimitError as exc:
            raise ApiError(413, str(exc)) from None

    @app.post("/build", tags=["templating"], responses=ERRORS)
    def build_endpoint(req: BuildRequest) -> BuildResponse:
        """Build template, .env and render script from chosen variables, and verify the round trip."""
        result = _run_build(req)
        return BuildResponse(
            engine=result.engine,
            template=result.template,
            env=result.env,
            runner=result.runner,
            variables=result.ir.variables,
            roundtrip=result.roundtrip,
        )

    @app.post("/render", tags=["templating"], responses=ERRORS)
    def render_endpoint(req: RenderRequest) -> RenderResponse:
        """Render a template with values (live preview). Rendering is sandboxed and bounded.

        Value keys may be variable names or .env keys (case-insensitive)."""
        try:
            emitter: Emitter = get_emitter(req.engine)
        except ValueError as exc:
            raise ApiError(422, str(exc)) from None
        values = {k.lower(): v for k, v in req.values.items()}
        try:
            if isinstance(emitter, Jinja2Emitter):
                output = emitter.render(req.template, values, newline=req.newline)
            else:
                output = emitter.render(req.template, values)
        except TemplateRenderError as exc:
            return RenderResponse(ok=False, error=str(exc))
        return RenderResponse(ok=True, output=output)

    @app.post(
        "/download",
        tags=["templating"],
        responses={
            **ERRORS,
            200: {"content": {"application/zip": {}}, "description": "Zip bundle"},
        },
        response_class=Response,
    )
    def download_endpoint(req: BuildRequest) -> Response:
        """Same input as /build; returns a zip with the template, .env, render.py and README.

        Nothing is stored server-side, so the request carries everything needed."""
        result = _run_build(req)
        return Response(
            content=make_zip(result),
            media_type="application/zip",
            headers={
                "content-disposition": 'attachment; filename="template-bundle.zip"',
                "x-roundtrip": "pass" if result.roundtrip.ok else "fail",
            },
        )

    # Outermost first: headers wrap everything, body limit applies before routing.
    app.add_middleware(BodyLimitMiddleware, max_bytes=settings.max_body_bytes)
    app.add_middleware(SecurityHeadersMiddleware)
    return app
