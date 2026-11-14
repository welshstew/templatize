# syntax=docker/dockerfile:1.7
# Optional NER service: GLiNER on ONNX Runtime (CPU). No PyTorch.
# Build online, run offline: the model is downloaded and checksum-verified at
# build time and baked in (override by mounting another model dir and setting
# TEMPLATIZE_NER_MODEL_DIR).
ARG PYTHON_VERSION=3.12
ARG UV_VERSION=0.13.8

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS model
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY scripts/download-ner-model.sh /usr/local/bin/download-ner-model
RUN download-ner-model /models/ner

# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS builder
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra ner --no-install-project
COPY README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra ner --no-editable

# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime
LABEL org.opencontainers.image.title="templatize-ner" \
      org.opencontainers.image.description="GLiNER NER service (ONNX Runtime CPU) for templatize"
RUN useradd --system --uid 10001 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin app
COPY --from=builder /app/.venv /app/.venv
COPY --from=model /models/ner /models/ner
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    DO_NOT_TRACK=1 \
    ORT_DISABLE_TELEMETRY=1 \
    TEMPLATIZE_NER_HOST=0.0.0.0 \
    TEMPLATIZE_NER_PORT=8081 \
    TEMPLATIZE_NER_MODEL_DIR=/models/ner
USER 10001
EXPOSE 8081
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=5 \
    CMD ["templatize-ner", "--check"]
CMD ["templatize-ner"]
