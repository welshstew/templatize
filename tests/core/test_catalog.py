"""The bundled schema catalogue: integrity, lookups and how it is applied."""

from __future__ import annotations

import gzip
import hashlib
import json
import socket

import pytest

from templatize import catalog
from templatize.suggest import suggest

ENTRIES = catalog.entries()


@pytest.mark.parametrize("entry", ENTRIES, ids=[e.id for e in ENTRIES])
def test_every_schema_is_present_verified_and_licensed(entry: catalog.Entry) -> None:
    manifest = json.loads((catalog.HERE / "manifest.json").read_text("utf-8"))
    recorded = next(e for e in manifest["schemas"] if e["id"] == entry.id)
    data = (catalog.HERE / entry.file).read_bytes()
    assert hashlib.sha256(data).hexdigest() == recorded["file_sha256"]
    assert isinstance(json.loads(gzip.decompress(data)), dict)
    assert entry.license and entry.source.startswith("https://")
    assert "/master/" not in entry.source and "/main/" not in entry.source  # pinned


def test_licence_notice_lists_every_file() -> None:
    notice = (catalog.HERE / "LICENSES.md").read_text("utf-8")
    assert all(f"`{e.file}`" in notice for e in ENTRIES)


@pytest.mark.parametrize(
    ("url", "entry_id"),
    [
        ("https://json.schemastore.org/github-workflow.json", "github-workflow"),
        ("https://json.schemastore.org/github-workflow", "github-workflow"),
        (
            "https://raw.githubusercontent.com/compose-spec/compose-spec/main/schema/compose-spec.json",
            "compose-spec",
        ),
        (
            "https://raw.githubusercontent.com/yannh/kubernetes-json-schema/master/"
            "v1.31.0-standalone-strict/deployment-apps-v1.json",
            "k8s-deployment-apps-v1",
        ),
        ("https://json.schemastore.org/chart.json#", "helm-chart"),
    ],
)
def test_lookup_by_url(url: str, entry_id: str) -> None:
    entry = catalog.for_url(url)
    assert entry is not None and entry.id == entry_id


def test_unknown_url_and_lookups_by_type_and_kind() -> None:
    assert catalog.for_url("https://schemas.acme.example.com/app.json") is None
    assert catalog.for_type("cloud-init") is not None
    assert catalog.for_type("kubernetes") is None  # per kind instead
    entry = catalog.for_k8s("autoscaling/v2", "HorizontalPodAutoscaler")
    assert entry is not None and entry.id == "k8s-horizontalpodautoscaler-autoscaling-v2"


K8S = """apiVersion: v1
kind: Service
metadata:
  name: web
spec:
  ports:
    - port: 80
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: web
spec:
  replicas: 3
"""


def test_kubernetes_documents_get_their_own_kinds_schema() -> None:
    res = suggest(K8S)
    info = res.schema_info
    assert info is not None and info.source == "catalog"
    assert info.catalog == ["k8s-service-v1", "k8s-deployment-apps-v1"]
    assert info.title == "Kubernetes: Service, Deployment"
    reasons = {c.value: c.reason or "" for c in res.candidates}
    assert "Number of desired pods" in reasons["3"]


def test_a_referenced_bundled_schema_is_applied() -> None:
    text = (
        "# yaml-language-server: $schema=https://json.schemastore.org/github-workflow.json\n"
        "name: CI\non: push\njobs:\n  test:\n    runs-on: ubuntu-24.04\n    steps:\n"
        "      - run: make test\n"
    )
    info = suggest(text).schema_info
    assert info is not None and info.catalog == ["github-workflow"]
    assert info.reference is not None


def test_a_supplied_schema_wins_over_the_catalogue() -> None:
    schema = '{"title": "Mine", "properties": {"hostname": {"type": "string"}}}'
    info = suggest("#cloud-config\nhostname: web-01\n", schema=schema).schema_info
    assert info is not None and (info.source, info.title) == ("supplied", "Mine")


def test_a_reference_outside_the_catalogue_is_only_reported() -> None:
    info = suggest('{"$schema": "https://schemas.acme.example.com/app.json", "a": 1}').schema_info
    assert info is not None and info.source is None
    assert "not in the bundled catalogue" in info.notes[0]


def test_a_weakly_detected_type_does_not_get_a_schema() -> None:
    info = suggest("name: x\nvalue: y\n").schema_info  # generic YAML
    assert info is None


def test_the_catalogue_never_touches_the_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    for e in ENTRIES:
        catalog.load(e.id)
    assert suggest(K8S).schema_info is not None
