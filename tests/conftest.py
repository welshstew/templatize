from __future__ import annotations

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
