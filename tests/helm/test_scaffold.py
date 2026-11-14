"""helm create-style charts: mapping, layout, overlays and the coverage report."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from templatize.build import specs_from_candidates
from templatize.helm import ChartError
from templatize.helm import build as build_helm_chart
from templatize.helm.coverage import compare, flatten, split_rendered
from templatize.helm.scaffold import diff_tree, map_values, scaffold_files
from templatize.helm.values import load
from templatize.suggest import suggest
from tests.conftest import FIXTURES, VARIANTS, read_fixture

STACK = read_fixture(FIXTURES / "k8s-app-stack.yaml")
STAGING = read_fixture(VARIANTS / "k8s-app-stack.v1.yaml")
HELM = shutil.which("helm")
needs_helm = pytest.mark.skipif(HELM is None, reason="helm not installed")


def scaffold(text: str = STACK, variants: list[str] | None = None, **kw: object):  # type: ignore[no-untyped-def]
    variants = variants or []
    specs = specs_from_candidates(suggest(text, variants).candidates)
    return build_helm_chart("scaffold", text, specs, variants, **kw)  # type: ignore[arg-type]


def test_map_values_fills_the_scaffold() -> None:
    v, handled, notes = map_values(STACK)
    assert v["replicaCount"] == 4
    assert v["image"] == {
        "repository": "ghcr.io/acme/storefront",
        "pullPolicy": "IfNotPresent",
        "tag": "3.9.1",
    }
    assert v["fullnameOverride"] == "storefront" and v["nameOverride"] == "storefront"
    assert v["containerName"] == "web" and v["containerPort"] == 8080
    assert v["envFrom"] == [{"configMapRef": {"name": "storefront-config"}}]
    assert v["resources"]["limits"] == {"cpu": "1", "memory": "1Gi"}
    assert v["service"] == {"type": "ClusterIP", "port": 80, "targetPort": 8080}
    assert v["ingress"]["enabled"] is True
    assert v["ingress"]["hosts"] == [
        {"host": "shop.example.com", "paths": [{"path": "/", "pathType": "Prefix"}]}
    ]
    assert v["ingress"]["tls"][0]["secretName"] == "storefront-tls"
    assert v["livenessProbe"] == {} and v["readinessProbe"] == {}  # none in the input
    assert v["serviceAccount"]["create"] is False
    assert v["podLabels"] == {}  # app.kubernetes.io/name is set by the scaffold itself
    assert len(handled) == 3  # Deployment, Service, Ingress; the ConfigMap is carried
    assert any("namespace" in n for n in notes)


def test_layout_and_renaming() -> None:
    chart = scaffold()
    paths = [f.path for f in chart.files]
    assert paths[:2] == ["Chart.yaml", "values.yaml"]
    for p in (
        "templates/_helpers.tpl",
        "templates/deployment.yaml",
        "templates/NOTES.txt",
        "templates/configmap.yaml",
    ):
        assert p in paths
    assert all("tzscaffold" not in f.content for f in chart.files)
    assert 'include "storefront.fullname"' in chart.file("templates/deployment.yaml").content
    assert chart.style == "scaffold" and chart.roundtrips == []
    assert 'appVersion: "3.9.1"' in chart.file("Chart.yaml").content
    values = load(chart.file("values.yaml").content)
    assert values["config"]["LOG_LEVEL"] == "info"  # carried ConfigMap variable
    assert "{{ .Values.config.LOG_LEVEL }}" in chart.file("templates/configmap.yaml").content


def test_staging_overlay() -> None:
    chart = scaffold(STACK, [STAGING], variant_labels=["staging"])
    overlay = load(chart.file("values-staging.yaml").content)
    assert overlay["replicaCount"] == 1
    assert overlay["image"] == {"tag": "3.10.0-rc.2"}
    assert overlay["ingress"]["hosts"][0]["host"] == "shop.staging.example.com"
    assert overlay["config"] == {"LOG_LEVEL": "debug"}
    assert overlay["namespace"] == "shop-staging"  # the carried ConfigMap's namespace
    assert "service" not in overlay


def test_needs_exactly_one_deployment() -> None:
    two = (
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: a\n---\n"
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: b\n"
    )
    with pytest.raises(ChartError, match="exactly one Deployment"):
        build_helm_chart("scaffold", two, [])
    cm = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: c\n"
    with pytest.raises(ChartError, match="found 0"):
        build_helm_chart("scaffold", cm, [])


def test_scaffold_files_are_bundled() -> None:
    names = {f.path for f in scaffold_files("demo")}
    assert {
        "templates/deployment.yaml",
        "templates/_helpers.tpl",
        "templates/tests/test-connection.yaml",
        ".helmignore",
    } <= names
    assert (Path(__file__).resolve().parents[2] / "src/templatize/helm/scaffold/NOTICE").is_file()


def test_diff_tree() -> None:
    base = {"a": 1, "b": {"c": 2, "d": [1]}, "e": "x"}
    assert diff_tree(base, {"a": 1, "b": {"c": 3, "d": [1]}, "e": "x"}) == {"b": {"c": 3}}
    assert diff_tree(base, {"a": 1, "b": {"c": 2, "d": [2]}, "e": "x"}) == {"b": {"d": [2]}}


def test_compare_and_flatten() -> None:
    exp = "kind: A\nmetadata:\n  name: x\nspec:\n  n: 1\n  gone: true\n---\nkind: Lost\nmetadata:\n  name: y\n"
    act = "kind: A\nmetadata:\n  name: x\n  labels:\n    app.kubernetes.io/name: x\nspec:\n  n: 2\n---\nkind: New\nmetadata:\n  name: z\n"
    rep = compare(exp, act, "values.yaml", "input")
    by = {r.kind: r for r in rep.resources}
    assert by["A"].status == "matched" and [d.path for d in by["A"].changed] == ["spec.n"]
    assert [d.path for d in by["A"].lost] == ["spec.gone"]
    assert [d.path for d in by["A"].added] == ['metadata.labels."app.kubernetes.io/name"']
    assert by["Lost"].status == "lost" and by["New"].status == "added"
    assert rep.covered == 2 and rep.expected_fields == 6
    assert flatten({"a": [], "b": {}}) == {"a": "[]", "b": "{}"}


def test_split_rendered() -> None:
    out = "---\n# Source: c/templates/a.yaml\nx: 1\n---\n# Source: c/templates/b.yaml\ny: 2\n---\nz: 3\n"
    files = split_rendered(out, "c")
    assert [(f.path, f.content) for f in files] == [
        ("templates/a.yaml", "x: 1\n"),
        ("templates/b.yaml", "y: 2\n---\nz: 3\n"),
    ]


def test_without_helm_the_report_says_not_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEMPLATIZE_HELM", "0")
    chart = scaffold()
    assert chart.ok
    assert [c.available for c in chart.coverage] == [False]
    assert "not installed" in (chart.coverage[0].error or "")


@needs_helm
def test_coverage_with_real_helm(tmp_path: Path) -> None:
    chart = scaffold(STACK, [STAGING], variant_labels=["staging"])
    assert chart.ok
    for rep in chart.coverage:
        assert rep.available and rep.error is None
        assert rep.percent >= 90, rep
        assert rep.changed == 0
        lost = [d.path for r in rep.resources for d in r.lost]
        assert set(lost) == {"metadata.namespace"}, lost
    root = tmp_path / chart.chart_name
    for f in chart.files:
        (root / f.path).parent.mkdir(parents=True, exist_ok=True)
        (root / f.path).write_text(f.content, encoding="utf-8", newline="")
    assert HELM is not None
    lint = subprocess.run([HELM, "lint", str(root)], capture_output=True, text=True, timeout=60)
    assert lint.returncode == 0, lint.stdout + lint.stderr
