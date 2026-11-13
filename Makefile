# Prefer docker, fall back to podman.
CONTAINER ?= $(shell command -v docker >/dev/null 2>&1 && echo docker || echo podman)
IMAGE ?= templatize:dev
PLATFORMS ?= linux/amd64,linux/arm64

.PHONY: install test lint format typecheck check image image-multiarch offline-test clean

install:
	uv sync

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
	$(CONTAINER) build -f docker/app.Dockerfile -t $(IMAGE) .

# Requires docker buildx with QEMU/binfmt for the non-native platform.
image-multiarch:
	docker buildx build -f docker/app.Dockerfile --platform $(PLATFORMS) -t $(IMAGE) .

# Every fixture through suggest -> build -> round-trip, with networking disabled
# and a read-only root filesystem. Must pass before a milestone counts as done.
offline-test: image
	$(CONTAINER) run --rm --network none --read-only --cap-drop ALL \
		--security-opt no-new-privileges \
		-v "$(CURDIR)/tests/fixtures:/fixtures:ro" \
		--entrypoint offline-fixtures $(IMAGE) /fixtures

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache .hypothesis dist
