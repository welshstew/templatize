"""Every fixture must pass suggest -> build -> round-trip, and render.py must reproduce it."""

from __future__ import annotations

import subprocess
import sys
import zipfile
from io import BytesIO
from pathlib import Path

from templatize.build import build, specs_from_candidates
from templatize.bundle import make_zip
from templatize.suggest import suggest
from tests.conftest import read_fixture, variants_for


def test_fixture_roundtrips_with_all_suggestions(fixture_path: Path) -> None:
    text = read_fixture(fixture_path)
    res = suggest(text)
    assert res.candidates, "expected at least one suggestion"
    result = build(text, specs_from_candidates(res.candidates))
    assert result.roundtrip.ok, result.roundtrip


def test_fixture_roundtrips_with_variants(fixture_path: Path) -> None:
    variants = variants_for(fixture_path)
    if not variants:
        return
    text = read_fixture(fixture_path)
    res = suggest(text, variants)
    assert any(c.variant_values for c in res.candidates)
    result = build(text, specs_from_candidates(res.candidates))
    assert result.roundtrip.ok, result.roundtrip


def test_fixture_roundtrips_with_no_variables(fixture_path: Path) -> None:
    text = read_fixture(fixture_path)
    assert build(text, []).roundtrip.ok


def test_render_script_reproduces_fixture(fixture_path: Path, tmp_path: Path) -> None:
    """End to end: unzip the bundle and run the generated render.py in a subprocess."""
    text = read_fixture(fixture_path)
    result = build(text, specs_from_candidates(suggest(text).candidates))
    with zipfile.ZipFile(BytesIO(make_zip(result))) as zf:
        zf.extractall(tmp_path)
    assert {p.name for p in tmp_path.iterdir()} == {"template.j2", ".env", "render.py", "README.md"}
    out = tmp_path / "out.bin"
    proc = subprocess.run(
        [sys.executable, str(tmp_path / "render.py"), "-o", str(out)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert out.read_bytes() == fixture_path.read_bytes()
