"""FastAPI application.

Privacy: request bodies are never logged or stored. Errors are logged by exception
type and path only, because exception messages may quote user text. Validation
errors are returned without the ``input`` echo FastAPI includes by default.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from templatize import __version__
from templatize.build import BuildResult, build
from templatize.bundle import make_chart_zip, make_zip
from templatize.detectors import HEURISTIC_DETECTORS, Detector, default_detectors
from templatize.detectors import ner as ner_detector
from templatize.detectors.llm import LlmConfig, LlmUnavailable, check_health, review_candidates
from templatize.emitters import EMITTERS, TemplateRenderError, get_emitter
from templatize.emitters.base import Emitter
from templatize.emitters.jinja2 import Jinja2Emitter
from templatize.helm import ChartError, ChartResult, build_chart
from templatize.helm import values as helm_values
from templatize.helm.chart import render_templates
from templatize.ir import MAX_INPUT_CHARS, MAX_VARIANTS, IRValidationError
from templatize.suggest import InputLimitError, SuggestResult, suggest
from templatize_api.middleware import BodyLimitMiddleware, SecurityHeadersMiddleware
from templatize_api.schemas import (
    MAX_VARIABLES,
    BuildRequest,
    BuildResponse,
    ChartRenderRequest,
    ChartRenderResponse,
    ChartRequest,
    ChartResponse,
    ErrorResponse,
    InfoResponse,
    Limits,
    LlmReviewResponse,
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


def _run_chart(req: ChartRequest) -> ChartResult:
    try:
        return build_chart(
            req.text,
            req.variables,
            req.variants,
            chart_name=req.chart_name,
            variant_labels=req.variant_labels,
        )
    except InputLimitError as exc:
        raise ApiError(413, str(exc)) from None
    except ValidationError as exc:
        raise ApiError(422, _validation_message(exc)) from None
    except (ChartError, IRValidationError, ValueError) as exc:
        raise ApiError(422, str(exc)) from None


def _mount_frontend(app: FastAPI, settings: Settings) -> None:
    """Serve the built single-page app (frontend/dist) if configured."""
    static = settings.static_dir
    if static is None:
        return
    index = static / "index.html"
    if not index.is_file():
        log.warning("TEMPLATIZE_STATIC_DIR has no index.html; UI not served")
        return
    if (static / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=static / "assets"), name="assets")

    @app.get("/", include_in_schema=False)
    def spa_index() -> FileResponse:
        return FileResponse(index, media_type="text/html")

    favicon = static / "favicon.svg"
    if favicon.is_file():

        @app.get("/favicon.svg", include_in_schema=False)
        def spa_favicon() -> FileResponse:
            return FileResponse(favicon, media_type="image/svg+xml")


def _mcp_http(settings: Settings) -> tuple[list[Any], AbstractAsyncContextManager[Any] | None]:
    """Routes and lifespan for MCP streamable HTTP at /mcp, or nothing if disabled or the
    `mcp` extra is not installed (the API then works exactly as before)."""
    if not settings.mcp_http:
        return [], None
    try:
        from templatize_mcp.server import create_server, transport_security
    except ImportError:
        log.info("mcp package not installed; /mcp not served")
        return [], None
    server = create_server()
    http_app = server.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,  # every tool is a pure function: no session state needed
        json_response=True,
        max_request_body_size=settings.max_body_bytes,
        transport_security=transport_security(list(settings.mcp_allowed_hosts)),
    )
    return list(http_app.routes), server.session_manager.run()


class _HealthCache:
    """Probe an optional service at most every ``ttl`` seconds."""

    def __init__(
        self,
        url: str | None,
        ttl: float = 15.0,
        probe: Callable[..., bool] = check_health,
    ) -> None:
        self.url = url
        self.ttl = ttl
        self.probe = probe
        self._at = -1e9
        self._ok = False

    def ok(self) -> bool:
        if not self.url:
            return False
        now = time.monotonic()
        if now - self._at > self.ttl:
            self._ok = self.probe(self.url, timeout=1.5)
            self._at = now
        return self._ok


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    llm_health = _HealthCache(settings.llm_url)
    ner_health = _HealthCache(settings.ner_url, probe=ner_detector.check_health)

    def run_suggest(text: str, variants: list[str]) -> SuggestResult:
        """Heuristics, plus the NER service when it is up (it answers in milliseconds;
        if it fails mid-request the result just carries a notice)."""
        detectors: list[Detector] = default_detectors()
        if settings.ner_url and ner_health.ok():
            detectors.append(
                ner_detector.NerDetector(
                    ner_detector.NerConfig(
                        url=settings.ner_url,
                        timeout=settings.ner_timeout,
                        max_chars=settings.ner_max_chars,
                    )
                )
            )
        return suggest(text, variants, detectors=detectors)

    mcp_routes, mcp_lifespan = _mcp_http(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if mcp_lifespan is None:
            yield
        else:
            async with mcp_lifespan:
                yield

    app = FastAPI(
        title="templatize",
        version=__version__,
        summary="Turn plain text into templates with a round-trip guarantee.",
        # Swagger UI/ReDoc load assets from a CDN: disabled for offline use.
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
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
            optional_detectors={"ner": ner_health.ok(), "llm": llm_health.ok()},
            mcp_http="/mcp" if mcp_routes else None,
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
            return run_suggest(req.text, req.variants)
        except InputLimitError as exc:
            raise ApiError(413, str(exc)) from None

    @app.post("/suggest/llm", tags=["templating"], responses=ERRORS)
    async def suggest_llm_endpoint(req: SuggestRequest) -> LlmReviewResponse:
        """Ask the optional local LLM (llama.cpp) to review the heuristic suggestions:
        better names, variable-vs-fixed verdicts and missed variables. Slow (seconds to
        minutes on CPU): call it after /suggest and merge the result when it arrives.

        Never fails because the LLM is absent: returns ``available: false`` instead."""
        if not settings.llm_url:
            return LlmReviewResponse(available=False, detail="no LLM configured")
        try:
            heuristic = run_suggest(req.text, req.variants)
        except InputLimitError as exc:
            raise ApiError(413, str(exc)) from None
        config = LlmConfig(
            url=settings.llm_url, timeout=settings.llm_timeout, max_chars=settings.llm_max_chars
        )
        try:
            review = await review_candidates(config, req.text, heuristic.candidates, req.variants)
        except LlmUnavailable as exc:
            return LlmReviewResponse(available=False, detail=str(exc))
        return LlmReviewResponse(available=True, review=review)

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

    @app.post("/chart", tags=["helm"], responses=ERRORS)
    def chart_endpoint(req: ChartRequest) -> ChartResponse:
        """Build a Helm chart from Kubernetes manifests and the chosen variables.

        One template per resource, a nested ``values.yaml`` (from each variable's
        ``values_path``) and a ``values-<label>.yaml`` per example document. The round
        trip is checked for the input and for every example."""
        result = _run_chart(req)
        return ChartResponse(**result.model_dump(), ok=result.ok)

    @app.post("/chart/render", tags=["helm"], responses=ERRORS)
    def chart_render_endpoint(req: ChartRenderRequest) -> ChartRenderResponse:
        """Render chart templates with (edited) values, for the live preview.

        Supports the ``{{ .Values.x }}`` references templatize generates."""
        try:
            tree = helm_values.load(req.values_yaml)
            if req.overlay_yaml is not None:
                tree = helm_values.merge(tree, helm_values.load(req.overlay_yaml, "overlay"))
            files = render_templates(req.templates, tree)
        except (TemplateRenderError, helm_values.ValuesError) as exc:
            return ChartRenderResponse(ok=False, error=str(exc))
        return ChartRenderResponse(ok=True, files=files)

    @app.post(
        "/chart/download",
        tags=["helm"],
        responses={
            **ERRORS,
            200: {"content": {"application/zip": {}}, "description": "Chart directory as a zip"},
        },
        response_class=Response,
    )
    def chart_download_endpoint(req: ChartRequest) -> Response:
        """Same input as /chart; returns ``<chart>/...`` as a zip."""
        result = _run_chart(req)
        return Response(
            content=make_chart_zip(result),
            media_type="application/zip",
            headers={
                "content-disposition": f'attachment; filename="{result.chart_name}-chart.zip"',
                "x-roundtrip": "pass" if result.ok else "fail",
            },
        )

    app.router.routes.extend(mcp_routes)
    _mount_frontend(app, settings)

    # Outermost first: headers wrap everything, body limit applies before routing.
    app.add_middleware(BodyLimitMiddleware, max_bytes=settings.max_body_bytes)
    app.add_middleware(SecurityHeadersMiddleware)
    return app
