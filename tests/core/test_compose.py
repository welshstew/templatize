"""Docker Compose -> Kompose manifests -> Helm chart."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from templatize.build import specs_from_candidates
from templatize.compose import compose_services, prepare
from templatize.helm import build as build_chart
from templatize.suggest import suggest
from templatize_api.app import create_app
from templatize_api.settings import Settings
from tests.conftest import FIXTURES, read_fixture

KOMPOSE = shutil.which("kompose")
HELM = shutil.which("helm")
needs_kompose = pytest.mark.skipif(KOMPOSE is None, reason="kompose not installed")
COMPOSE = read_fixture(FIXTURES / "docker-compose.yml")


def test_recognises_compose_files_only() -> None:
    assert list(compose_services(COMPOSE) or {}) == ["web", "db", "cache"]
    assert compose_services(read_fixture(FIXTURES / "k8s-deployment.yaml")) is None
    assert compose_services("services: [a, b]\n") is None
    assert compose_services("services:\n  x:\n    command: run\n") is None  # no image/build


def test_prepare_exposes_services_others_use() -> None:
    _, exposed, notes = prepare(COMPOSE)
    assert exposed == {"db": 5432, "cache": 6379}  # @db:5432 in a URL; redis well-known
    assert any("db (port 5432)" in n for n in notes)
    text = (
        "services:\n  app:\n    image: app:1\n    ports: ['80:80']\n"
        "    environment:\n      SEARCH_URL: http://search\n    depends_on: [worker]\n"
        "  search:\n    image: acme/search:2\n  worker:\n    build: ./worker\n"
    )
    prepared, exposed, notes = prepare(text)
    assert exposed == {}
    assert any("'search' is used by other services but has no ports" in n for n in notes)
    assert any("'worker' is built from source" in n for n in notes)
    assert "expose" not in yaml.safe_load(prepared)["services"]["search"]


def test_compose_profile_offers_a_chart() -> None:
    profile = suggest(COMPOSE).profile
    assert profile.format == "compose" and profile.services == ["web", "db", "cache"]
    assert profile.helm_suggested is (KOMPOSE is not None)


@needs_kompose
def test_chart_from_compose_with_variables_by_value(tmp_path: Path) -> None:
    res = suggest(COMPOSE)
    chart = build_chart("exact", COMPOSE, specs_from_candidates(res.candidates))
    assert chart.converted_from == "compose" and chart.chart_name == "web"
    assert chart.ok, chart.roundtrips
    files = {f.path: f.content for f in chart.files}
    assert any(p.startswith("templates/service-db") for p in files)  # reachable database
    web = next(c for p, c in files.items() if p.startswith("templates/deployment-web"))
    assert "@{{ .Values.databaseHost }}:{{ .Values.databasePort }}/{{ .Values.postgresDb }}" in web
    values = yaml.safe_load(files["values.yaml"])
    assert values["webImage"] == "ghcr.io/acme/storefront:2.14.3" and values["databasePort"] == 5432
    assert any("added Services" in n for n in chart.notes)
    assert any(n.startswith("kompose:") for n in chart.notes)
    if HELM:
        root = tmp_path / chart.chart_name
        for p, c in files.items():
            (root / p).parent.mkdir(parents=True, exist_ok=True)
            (root / p).write_text(c, encoding="utf-8", newline="")
        lint = subprocess.run(
            [HELM, "lint", str(root)], capture_output=True, text=True, check=False
        )
        assert lint.returncode == 0, lint.stdout + lint.stderr
        out = subprocess.run(
            [HELM, "template", "r", str(root)], capture_output=True, text=True, check=True
        ).stdout
        docs = [d for d in yaml.safe_load_all(out) if d]
        assert sorted(d["kind"] + "/" + d["metadata"]["name"] for d in docs) == sorted(
            d["kind"] + "/" + d["metadata"]["name"]
            for d in yaml.safe_load_all(chart.documents[0])
            if d
        )


@needs_kompose
def test_chart_from_compose_with_an_example() -> None:
    staging = COMPOSE.replace("2.14.3", "2.15.0-rc.1").replace(
        "LOG_LEVEL: info", "LOG_LEVEL: debug"
    )
    res = suggest(COMPOSE, [staging])
    chart = build_chart(
        "exact",
        COMPOSE,
        specs_from_candidates(res.candidates),
        [staging],
        variant_labels=["staging"],
    )
    assert chart.ok, chart.roundtrips
    overlay = yaml.safe_load(
        next(f.content for f in chart.files if f.path == "values-staging.yaml")
    )
    assert overlay["webImage"] == "ghcr.io/acme/storefront:2.15.0-rc.1"
    assert overlay["logLevel"] == "debug"


@needs_kompose
def test_api_chart_from_compose() -> None:
    client = TestClient(create_app(Settings()))
    assert client.get("/info").json()["kompose"] is True
    res = client.post("/chart", json={"text": COMPOSE, "variables": []}).json()
    assert res["ok"] and res["converted_from"] == "compose"
    assert res["documents"][0].startswith("apiVersion")
