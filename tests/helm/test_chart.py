"""Chart building: layout, values paths, per-example values files, round trips."""

from __future__ import annotations

import io
import zipfile

import pytest

from templatize.build import VariableSpec, specs_from_candidates
from templatize.bundle import make_chart_zip
from templatize.helm import ChartError, build_chart
from templatize.helm import values as helm_values
from templatize.helm.chart import render_templates
from templatize.ir import Span
from templatize.suggest import suggest
from tests.conftest import FIXTURE_FILES, FIXTURES, VARIANTS, read_fixture

STACK = read_fixture(FIXTURES / "k8s-app-stack.yaml")
STAGING = read_fixture(VARIANTS / "k8s-app-stack.v1.yaml")
K8S_FIXTURES = [p for p in FIXTURE_FILES if read_fixture(p).find("apiVersion") != -1]


def chart_for(text: str, variants: list[str] | None = None, **kw: object):  # type: ignore[no-untyped-def]
    variants = variants or []
    res = suggest(text, variants)
    return build_chart(text, specs_from_candidates(res.candidates), variants, **kw)  # type: ignore[arg-type]


@pytest.mark.parametrize("path", K8S_FIXTURES, ids=lambda p: p.name)
def test_k8s_fixtures_roundtrip_as_charts(path) -> None:  # type: ignore[no-untyped-def]
    text = read_fixture(path)
    stem, _, suffix = path.name.partition(".")
    variants = [read_fixture(p) for p in sorted(VARIANTS.glob(f"{stem}.v*.{suffix}"))]
    for vs in ([], variants):
        result = chart_for(text, vs)
        assert result.ok, result.roundtrips
        assert len(result.roundtrips) == 1 + len(vs)
    # No variables at all is a valid (if dull) chart too.
    assert build_chart(text, []).ok


def test_layout_and_names() -> None:
    result = chart_for(STACK, [STAGING], variant_labels=["staging"])
    assert result.chart_name == "storefront"
    assert [f.path for f in result.files] == [
        "Chart.yaml",
        "values.yaml",
        "values-staging.yaml",
        ".helmignore",
        "templates/configmap.yaml",
        "templates/deployment.yaml",
        "templates/service.yaml",
        "templates/ingress.yaml",
    ]
    assert [t.kind for t in result.templates] == ["ConfigMap", "Deployment", "Service", "Ingress"]
    chart = result.file("Chart.yaml").content
    assert "apiVersion: v2\n" in chart and "name: storefront\n" in chart
    assert 'appVersion: "3.9.1"\n' in chart
    dep = result.file("templates/deployment.yaml").content
    assert "image: {{ .Values.image.repository }}:{{ .Values.image.tag }}\n" in dep
    assert "replicas: {{ .Values.replicaCount }}\n" in dep
    assert "apiVersion: apps/v1\n" in dep  # structure stays literal


def test_staging_overlay_has_only_differences() -> None:
    result = chart_for(STACK, [STAGING], variant_labels=["staging"])
    overlay = helm_values.load(result.file("values-staging.yaml").content)
    assert overlay["image"] == {"tag": "3.10.0-rc.2"}
    assert overlay["replicaCount"] == 1
    assert overlay["ingress"] == {"host": "shop.staging.example.com"}
    assert "service" not in overlay
    base = helm_values.load(result.file("values.yaml").content)
    rendered = render_templates(
        [f for f in result.files if f.path.startswith("templates/")],
        helm_values.merge(base, overlay),
    )
    assert "".join(f.content for f in rendered) == STAGING


def test_variant_roundtrip_fails_when_a_difference_is_not_a_variable() -> None:
    res = suggest(STACK, [STAGING])
    specs = [s for s in specs_from_candidates(res.candidates) if s.values_path != ["image", "tag"]]
    result = build_chart(STACK, specs, [STAGING])
    main, example = result.roundtrips
    assert main.ok and not example.ok
    assert example.label == "values-example-2.yaml" and example.document == "example 2"
    assert "3.10.0-rc.2" in (example.expected_excerpt or "")


def test_paths_are_made_unique_and_prefix_free() -> None:
    text = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: c\ndata:\n  a: x\n  b: y\n  c: z\n"
    specs = [
        VariableSpec(name="one", spans=[Span(start=50, end=51)], values_path=["image"]),
        VariableSpec(name="two", spans=[Span(start=57, end=58)], values_path=["image", "tag"]),
        VariableSpec(name="three", spans=[Span(start=64, end=65)]),
    ]
    result = build_chart(text, specs)
    assert [v.values_path for v in result.variables] == [["image"], ["image2", "tag"], ["three"]]
    assert result.ok


def test_errors() -> None:
    with pytest.raises(ChartError, match="Kubernetes"):
        build_chart("hello\n", [])
    with pytest.raises(ChartError, match="chart name"):
        build_chart(STACK, [], chart_name="Bad_Name")
    with pytest.raises(ChartError, match="label"):
        build_chart(STACK, [], [STAGING], variant_labels=["Staging!"])
    with pytest.raises(ChartError, match="one label per"):
        build_chart(STACK, [], [STAGING], variant_labels=["a", "b"])
    boundary = STACK.index("---\napiVersion: apps/v1")
    crossing = VariableSpec(name="x", spans=[Span(start=boundary - 3, end=boundary + 3)])
    with pytest.raises(ChartError, match="crosses the boundary"):
        build_chart(STACK, [crossing])


def test_template_syntax_in_manifests_is_escaped() -> None:
    text = (
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: c\n"
        'data:\n  tpl: "Hello {{ .Name }}"\n  brace: "{x}"\n'
    )
    result = build_chart(
        text,
        [VariableSpec(name="x", spans=[Span(start=text.index("x}"), end=text.index("x}") + 1)])],
    )
    assert result.ok, result.roundtrips
    assert '{{ "{{" }}' in result.file("templates/configmap.yaml").content


def test_zip_layout() -> None:
    result = chart_for(STACK, [STAGING], variant_labels=["staging"])
    with zipfile.ZipFile(io.BytesIO(make_chart_zip(result))) as zf:
        names = zf.namelist()
        readme = zf.read("storefront/README.md").decode()
    assert "storefront/Chart.yaml" in names and "storefront/templates/ingress.yaml" in names
    assert all(n.startswith("storefront/") for n in names)
    assert "-f values-staging.yaml" in readme and "PASS" in readme
