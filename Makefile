# Prefer docker, fall back to podman.
CONTAINER ?= $(shell command -v docker >/dev/null 2>&1 && echo docker || echo podman)
COMPOSE ?= $(CONTAINER) compose
IMAGE ?= templatize:dev
CHECK_IMAGE ?= templatize:offline-check
LLM_IMAGE ?= templatize-llm:dev
NER_IMAGE ?= templatize-ner:dev
# Separate project name: never touches a stack started with `make up`.
OFFLINE = $(COMPOSE) -p templatize-offline -f compose.yaml -f compose.offline.yaml
PLATFORMS ?= linux/amd64,linux/arm64

PHONY_EXTRA = samples frontend frontend-dev frontend-test check-all up-llm up-llm-gpu up-llm-gpu-large model model-large llm-image offline-test-llm ner-image ner-model up-ner
.PHONY: $(PHONY_EXTRA) install test lint format typecheck check image check-image image-multiarch offline-test offline-test-cli up down serve clean

install:
	uv sync --all-extras

serve: frontend
	TEMPLATIZE_STATIC_DIR=frontend/dist uv run templatize-api

frontend:
	cd frontend && npm ci --no-audit --no-fund && npm run build

frontend-dev:
	cd frontend && npm run dev

frontend-test:
	cd frontend && npm test

# Refresh tests/quality/samples.json from the editor's samples (frontend/src/sample.ts).
samples:
	cd frontend && node scripts/export-samples.mjs

test:
	uv run pytest -q

lint:
	uv run ruff check src tests
	uv run ruff format --check src tests

format:
	uv run ruff format src tests
	uv run ruff check --fix src tests

typecheck:
	uv run mypy

check: lint typecheck test

check-all: check frontend frontend-test

image:
	$(CONTAINER) build -f docker/app.Dockerfile --target runtime -t $(IMAGE) .

check-image:
	$(CONTAINER) build -f docker/app.Dockerfile --target offline-check -t $(CHECK_IMAGE) .

# Requires docker buildx with QEMU/binfmt for the non-native platform.
image-multiarch:
	docker buildx build -f docker/app.Dockerfile --target runtime --platform $(PLATFORMS) -t $(IMAGE) .
	docker buildx build -f docker/llm.Dockerfile --platform $(PLATFORMS) -t $(LLM_IMAGE) .
	docker buildx build -f docker/ner.Dockerfile --platform $(PLATFORMS) -t $(NER_IMAGE) .

# Full stack (app + NER) with networking disabled: the compose network is
# internal-only, the checker proves outbound access fails, then drives every fixture
# through the API (suggest -> build -> round-trip -> render -> download), checks the
# UI, MCP and NER, and that the absent LLM degrades gracefully. Must pass before a
# milestone counts as done.
offline-test: image check-image ner-image
	COMPOSE_PROFILES=ner TEMPLATIZE_OFFLINE_EXPECT_NER=1 $(OFFLINE) up --no-build \
		--abort-on-container-exit --exit-code-from offline-check app ner offline-check; \
		rc=$$?; COMPOSE_PROFILES=ner $(OFFLINE) down --remove-orphans >/dev/null 2>&1; exit $$rc

# CLI only, single container: --network none, read-only rootfs.
offline-test-cli: check-image
	$(CONTAINER) run --rm --network none --read-only --cap-drop ALL \
		--security-opt no-new-privileges \
		--entrypoint offline-fixtures $(CHECK_IMAGE) /fixtures

up:
	$(COMPOSE) up -d --build app

# App + local LLM detector (needs `make model` first).
up-llm: model
	COMPOSE_PROFILES=llm $(COMPOSE) up -d --build

# App + local LLM detector on an NVIDIA GPU (needs the NVIDIA Container Toolkit).
# --force-recreate: podman-compose keeps old containers when only the image or env changes.
GPU_LLM = COMPOSE_PROFILES=llm $(COMPOSE) -f compose.yaml -f compose.gpu.yaml
up-llm-gpu: model
	$(GPU_LLM) up -d --build --force-recreate

# Same, with the larger model (needs ~21 GB of VRAM).
up-llm-gpu-large: model-large
	TEMPLATIZE_LLM_MODEL=$(LARGE_MODEL) $(GPU_LLM) up -d --build --force-recreate

# Default GGUF for the LLM detector, pinned revision + SHA-256 verified (~2.5 GB).
model:
	./scripts/download-model.sh models

# Larger GGUF for a 24 GB GPU, pinned revision + SHA-256 verified (~18.6 GB).
LARGE_MODEL = Qwen3-30B-A3B-Instruct-2507-Q4_K_M.gguf
model-large:
	./scripts/download-model.sh models large

ner-image:
	$(CONTAINER) build -f docker/ner.Dockerfile -t $(NER_IMAGE) .

# GLiNER model for running/testing the NER service outside a container (~190 MB).
ner-model:
	./scripts/download-ner-model.sh models/ner

# App + NER detector.
up-ner:
	COMPOSE_PROFILES=ner $(COMPOSE) up -d --build

llm-image:
	$(CONTAINER) build -f docker/llm.Dockerfile -t $(LLM_IMAGE) .

# Full offline test including a real LLM review inside the isolated network.
offline-test-llm: image check-image llm-image ner-image model
	COMPOSE_PROFILES=ner,llm TEMPLATIZE_OFFLINE_EXPECT_NER=1 TEMPLATIZE_OFFLINE_EXPECT_LLM=1 \
		$(OFFLINE) up --no-build --abort-on-container-exit --exit-code-from offline-check \
		app ner llm offline-check; \
		rc=$$?; COMPOSE_PROFILES=ner,llm $(OFFLINE) down --remove-orphans >/dev/null 2>&1; exit $$rc

down:
	$(COMPOSE) down --remove-orphans

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache .hypothesis dist
