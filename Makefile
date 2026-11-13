# Prefer docker, fall back to podman.
CONTAINER ?= $(shell command -v docker >/dev/null 2>&1 && echo docker || echo podman)
COMPOSE ?= $(CONTAINER) compose
IMAGE ?= templatize:dev
CHECK_IMAGE ?= templatize:offline-check
OFFLINE = $(COMPOSE) -f compose.yaml -f compose.offline.yaml
PLATFORMS ?= linux/amd64,linux/arm64

.PHONY: install test lint format typecheck check image check-image image-multiarch offline-test offline-test-cli up down serve clean

install:
	uv sync --all-extras

serve:
	uv run templatize-api

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

image:
	$(CONTAINER) build -f docker/app.Dockerfile --target runtime -t $(IMAGE) .

check-image:
	$(CONTAINER) build -f docker/app.Dockerfile --target offline-check -t $(CHECK_IMAGE) .

# Requires docker buildx with QEMU/binfmt for the non-native platform.
image-multiarch:
	docker buildx build -f docker/app.Dockerfile --target runtime --platform $(PLATFORMS) -t $(IMAGE) .

# Full stack with networking disabled: the compose network is internal-only, the
# checker proves outbound access fails, then drives every fixture through the API
# (suggest -> build -> round-trip -> render -> download). Must pass before a
# milestone counts as done.
offline-test: image check-image
	$(OFFLINE) up --no-build --abort-on-container-exit --exit-code-from offline-check \
		app offline-check; rc=$$?; $(OFFLINE) down --remove-orphans >/dev/null 2>&1; exit $$rc

# CLI only, single container: --network none, read-only rootfs.
offline-test-cli: check-image
	$(CONTAINER) run --rm --network none --read-only --cap-drop ALL \
		--security-opt no-new-privileges \
		--entrypoint offline-fixtures $(CHECK_IMAGE) /fixtures

up:
	$(COMPOSE) up -d --build app

down:
	$(COMPOSE) down --remove-orphans

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache .hypothesis dist
