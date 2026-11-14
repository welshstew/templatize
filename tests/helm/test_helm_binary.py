"""Conformance with real Helm: the chart must render to the same manifests.

Skipped unless ``helm`` is on PATH (the offline-check image and CI install a pinned
release). ``helm template`` re-splits and trims manifests and adds ``# Source:``
headers, so this compares the parsed YAML documents; byte exactness is covered by
the round-trip check, which renders the same templates with the same values.
What this catches is a difference in how Helm *reads values* (typing, quoting,
number formatting) or parses the templates.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from templatize.build import specs_from_candidates
from templatize.helm import build_chart
from templatize.suggest import suggest
from tests.conftest import FIXTURE_FILES, VARIANTS, read_fixture

HELM = shutil.which("helm")
K8S = [p for p in FIXTURE_FILES if "apiVersion" in read_fixture(p)]

pytestmark = pytest.mark.skipif(HELM is None, reason="helm not installed")


def _docs(text: str) -> list[object]:
    """Parsed documents in a canonical order: ``helm template`` emits manifests in
    install order (ConfigMap before Service before Deployment ...), not file order."""
    docs = [d for d in yaml.safe_load_all(text) if d is not None]
    return sorted(docs, key=lambda d: json.dumps(d, sort_keys=True, default=str))


@pytest.mark.parametrize("path", K8S, ids=lambda p: p.name)
def test_helm_template_matches_input(path: Path, tmp_path: Path) -> None:
    assert HELM is not None
    text = read_fixture(path)
    stem, _, suffix = path.name.partition(".")
    variants = [read_fixture(p) for p in sorted(VARIANTS.glob(f"{stem}.v*.{suffix}"))]
    res = suggest(text, variants)
    chart = build_chart(text, specs_from_candidates(res.candidates), variants)
    assert chart.ok
    root = tmp_path / chart.chart_name
    for f in chart.files:
        (root / f.path).parent.mkdir(parents=True, exist_ok=True)
        (root / f.path).write_text(f.content, encoding="utf-8", newline="")
    for expected, extra in [
        (text, []),
        *((v, ["-f", str(root / f"values-example-{i + 2}.yaml")]) for i, v in enumerate(variants)),
    ]:
        out = subprocess.run(
            [HELM, "template", "release", str(root), *extra],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        ).stdout
        assert _docs(out) == _docs(expected)
    lint = subprocess.run([HELM, "lint", str(root)], capture_output=True, text=True, timeout=60)
    assert lint.returncode == 0, lint.stdout + lint.stderr
