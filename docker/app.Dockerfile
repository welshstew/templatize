# syntax=docker/dockerfile:1.7
# Build online, run offline: every dependency is resolved from uv.lock at build time.
ARG PYTHON_VERSION=3.12
ARG UV_VERSION=0.13.8
ARG NODE_VERSION=24

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

# ---------------------------------------------------------------------------
# Frontend: built once on the build platform (output is architecture-independent).
# `npm run build` type-checks, bundles everything locally and fails if the output
# references any external URL.
FROM --platform=$BUILDPLATFORM node:${NODE_VERSION}-slim AS frontend
WORKDIR /fe
COPY frontend/package.json frontend/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build && npm test

# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS builder
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
# Dependencies first for layer caching.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra api --no-install-project
COPY README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra api --no-editable

# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime
LABEL org.opencontainers.image.title="templatize" \
      org.opencontainers.image.description="Turn plain text into templates, offline"
RUN useradd --system --uid 10001 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin app
COPY --from=builder /app/.venv /app/.venv
COPY --from=frontend /fe/dist /app/static
COPY scripts/offline-fixtures.sh /usr/local/bin/offline-fixtures
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    DO_NOT_TRACK=1 \
    TEMPLATIZE_HOST=0.0.0.0 \
    TEMPLATIZE_PORT=8000 \
    TEMPLATIZE_STATIC_DIR=/app/static
USER 10001
WORKDIR /work
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=5 \
    CMD ["templatize-healthcheck"]
# Default: the web API. CLI: `docker run --rm -i IMAGE templatize suggest -`.
CMD ["templatize-api"]

# ---------------------------------------------------------------------------
# Test-only image: runtime + fixtures baked in. Used by `make offline-test*`.
# Fixtures are copied from the build context rather than bind-mounted, because CI
# runners that talk to the host's Docker daemon cannot bind-mount their workspace.
FROM runtime AS offline-check
COPY --chown=10001:10001 tests/fixtures /fixtures
CMD ["python", "-m", "templatize_api.offline_check", "--base-url", "http://app:8000", "--fixtures", "/fixtures"]
