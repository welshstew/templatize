"""Helm chart through the API, the CLI and the MCP tool."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from templatize.cli import main
from templatize_api.app import create_app
from templatize_api.settings import Settings
from templatize_mcp import tools
from tests.conftest import FIXTURES, VARIANTS, read_fixture

STACK_PATH = FIXTURES / "k8s-app-stack.yaml"
STAGING_PATH = VARIANTS / "k8s-app-stack.v1.yaml"
STACK = read_fixture(STACK_PATH)
STAGING = read_fixture(STAGING_PATH)


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(Settings()), raise_server_exceptions=False)


def _variables(client: TestClient) -> list[dict[str, object]]:
    res = client.post("/suggest", json={"text": STACK, "variants": [STAGING]}).json()
    assert res["profile"]["format"] == "kubernetes" and res["profile"]["helm_suggested"]
    return [
        {
            "name": c["name"],
            "spans": c["spans"],
            "values_path": c["values_path"],
            "variant_values": c["variant_values"],
        }
        for c in res["candidates"]
    ]


def test_api_chart_and_render(client: TestClient) -> None:
    body = {
        "text": STACK,
        "variants": [STAGING],
        "variables": _variables(client),
        "variant_labels": ["staging"],
    }
    r = client.post("/chart", json=body)
    assert r.status_code == 200, r.text
    chart = r.json()
    assert chart["ok"] and chart["chart_name"] == "storefront"
    files = {f["path"]: f["content"] for f in chart["files"]}
    assert "values-staging.yaml" in files
    templates = [{"path": p, "content": c} for p, c in files.items() if p.startswith("templates/")]

    out = client.post(
        "/chart/render", json={"templates": templates, "values_yaml": files["values.yaml"]}
    ).json()
    assert out["ok"] and "".join(f["content"] for f in out["files"]) == STACK

    edited = files["values.yaml"].replace("replicaCount: 4", "replicaCount: 9")
    out = client.post(
        "/chart/render",
        json={
            "templates": templates,
            "values_yaml": edited,
            "overlay_yaml": files["values-staging.yaml"],
        },
    ).json()
    assert out["ok"] and "replicas: 1\n" in out["files"][1]["content"]  # overlay wins

    bad = client.post("/chart/render", json={"templates": templates, "values_yaml": "a: ["}).json()
    assert not bad["ok"] and "values.yaml" in bad["error"]


def test_api_chart_errors_and_download(client: TestClient) -> None:
    r = client.post("/chart", json={"text": "hello\n"})
    assert r.status_code == 422 and "Kubernetes" in r.json()["detail"]
    r = client.post("/chart/download", json={"text": STACK, "variables": _variables(client)})
    assert r.status_code == 200
    assert r.headers["x-roundtrip"] == "pass"
    assert 'filename="storefront-chart.zip"' in r.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        assert "storefront/values.yaml" in zf.namelist()


def test_api_suggest_profile_absent_for_plain_text(client: TestClient) -> None:
    res = client.post("/suggest", json={"text": "server { listen 80; }\n"}).json()
    assert res["profile"] == {
        "format": None,
        "resources": [],
        "helm_suggested": False,
        "reason": None,
    }


def test_cli_chart(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(
        [
            "chart",
            str(STACK_PATH),
            "--variant",
            str(STAGING_PATH),
            "--label",
            "staging",
            "--accept-all",
            "-o",
            str(tmp_path),
            "--zip",
            str(tmp_path / "c.zip"),
        ]
    )
    err = capsys.readouterr().err
    assert rc == 0, err
    assert "round-trip input with values.yaml: PASS" in err
    assert "round-trip example 2 with values-staging.yaml: PASS" in err
    assert (tmp_path / "storefront" / "templates" / "ingress.yaml").is_file()
    assert (tmp_path / "c.zip").is_file()


def test_cli_chart_rejects_non_kubernetes(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["chart", str(FIXTURES / "nginx.conf"), "--accept-all"]) == 1
    assert "Kubernetes" in capsys.readouterr().err


def test_mcp_tool() -> None:
    s = tools.suggest_parameters(STACK)
    assert s.profile.helm_suggested and "generate_helm_chart" in s.next_step
    r = tools.generate_helm_chart(STACK, variants=[STAGING], variant_labels=["staging"])
    assert all(rt.ok for rt in r.roundtrips)
    assert r.values_paths["image_tag"] == "image.tag"
    by_value = tools.generate_helm_chart(
        STACK,
        variables=[
            tools.VariableInput(name="tag", value="3.9.1", values_path=["image", "tag"]),
        ],
    )
    assert by_value.values_paths == {"tag": "image.tag"}
    with pytest.raises(tools.ToolInputError):
        tools.generate_helm_chart("not yaml: [\n")
    assert json.loads(r.model_dump_json())["chart_name"] == "storefront"


def test_api_scaffold_chart_and_preview(client: TestClient) -> None:
    import shutil

    info = client.get("/info").json()
    assert info["helm"] is (shutil.which("helm") is not None)
    body = {
        "text": STACK,
        "variants": [STAGING],
        "variables": _variables(client),
        "variant_labels": ["staging"],
        "style": "scaffold",
    }
    chart = client.post("/chart", json=body).json()
    assert chart["style"] == "scaffold" and chart["ok"]
    assert chart["roundtrips"] == [] and len(chart["coverage"]) == 2
    paths = [f["path"] for f in chart["files"]]
    assert "templates/_helpers.tpl" in paths and "templates/configmap.yaml" in paths
    if not info["helm"]:
        assert chart["coverage"][0]["available"] is False
        return
    assert chart["coverage"][0]["percent"] >= 90
    files = {f["path"]: f["content"] for f in chart["files"]}
    out = client.post(
        "/chart/render",
        json={
            "style": "scaffold",
            "chart_name": chart["chart_name"],
            "files": chart["files"],
            "templates": [],
            "values_yaml": files["values.yaml"].replace("replicaCount: 4", "replicaCount: 7"),
            "overlay_yaml": None,
        },
    ).json()
    assert out["ok"], out
    dep = next(f["content"] for f in out["files"] if f["path"] == "templates/deployment.yaml")
    assert "replicas: 7" in dep and 'image: "ghcr.io/acme/storefront:3.9.1"' in dep
    bad = client.post(
        "/chart/render",
        json={
            "style": "scaffold",
            "chart_name": "storefront",
            "files": chart["files"],
            "templates": [],
            "values_yaml": "a: [",
        },
    ).json()
    assert not bad["ok"] and "helm template failed" in bad["error"]
    r = client.post("/chart/download", json=body)
    assert r.status_code == 200 and r.headers["x-roundtrip"] == "pass"


def test_mcp_scaffold_style() -> None:
    r = tools.generate_helm_chart(STACK, style="scaffold")
    assert r.style == "scaffold" and r.roundtrips == []
    assert any(f.path == "templates/_helpers.tpl" for f in r.files)
    assert r.coverage and r.notes


def test_api_chart_with_loops(client: TestClient) -> None:
    text = (
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: hosts\ndata:\n"
        "  hosts: |\n    a.example\n    b.example\n"
        "---\napiVersion: v1\nkind: Service\nmetadata:\n  name: web\nspec:\n  ports:\n"
        "    - port: 80\n      name: http\n    - port: 443\n      name: https\n"
    )
    sug = client.post("/suggest", json={"text": text}).json()
    loop = next(lp for lp in sug["loops"] if lp["name"] == "ports")
    spec = {k: loop[k] for k in ("name", "item", "items", "fields", "scalar")}
    res = client.post("/chart", json={"text": text, "loops": [spec]}).json()
    assert res["ok"], res["roundtrips"]
    assert [lp["values_path"] for lp in res["loops"]] == [["ports"]]
    files = {f["path"]: f["content"] for f in res["files"]}
    assert "{{ range $i, $port := .Values.ports }}" in files["templates/service.yaml"]
    assert 'ports:\n  - port: 80\n    name: "http"\n' in files["values.yaml"]
    # The live preview renders range with edited values.
    templates = [{"path": p, "content": c} for p, c in files.items() if p.startswith("templates/")]
    edited = files["values.yaml"].replace('  - port: 443\n    name: "https"\n', "")
    out = client.post("/chart/render", json={"templates": templates, "values_yaml": edited}).json()
    assert out["ok"], out
    service = next(f["content"] for f in out["files"] if f["path"] == "templates/service.yaml")
    assert "443" not in service and "- port: 80" in service
    # The helm create style keeps lists as text and says so.
    scaffold = client.post("/chart", json={"text": text, "loops": [spec], "style": "scaffold"})
    assert scaffold.status_code == 422  # no Deployment: the scaffold needs one
