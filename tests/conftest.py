from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
VARIANTS = FIXTURES / "variants"

FIXTURE_FILES = sorted(p for p in FIXTURES.iterdir() if p.is_file())


def read_fixture(path: Path) -> str:
    # Bytes -> str without newline translation.
    return path.read_bytes().decode("utf-8")


def variants_for(path: Path) -> list[str]:
    stem, _, suffix = path.name.partition(".")
    return [read_fixture(p) for p in sorted(VARIANTS.glob(f"{stem}.v*.{suffix}"))]


@pytest.fixture(params=FIXTURE_FILES, ids=lambda p: p.name)
def fixture_path(request: pytest.FixtureRequest) -> Path:
    path: Path = request.param
    return path


# External tools some tests run for real (helm template, ansible-playbook, kompose,
# envsubst).
# Locally they are optional and those tests skip; CI sets TEMPLATIZE_REQUIRE_TOOLS=1 so
# a missing tool fails the run instead of quietly skipping the tests.
REQUIRED_TOOLS = ("helm", "ansible-playbook", "kompose", "envsubst")


def pytest_sessionstart(session: pytest.Session) -> None:
    if os.environ.get("TEMPLATIZE_REQUIRE_TOOLS") != "1":
        return
    missing = [t for t in REQUIRED_TOOLS if shutil.which(t) is None]
    if missing:
        raise pytest.UsageError(f"TEMPLATIZE_REQUIRE_TOOLS=1 but not on PATH: {', '.join(missing)}")
