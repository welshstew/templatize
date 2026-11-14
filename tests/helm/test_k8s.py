"""Kubernetes recognition and the Kubernetes-aware detector."""

from __future__ import annotations

import itertools

from templatize.detectors.base import DetectionContext
from templatize.detectors.k8s import KubernetesDetector, camel
from templatize.k8s import detect_profile, parse_resources
from templatize.suggest import suggest
from tests.conftest import FIXTURES, VARIANTS, read_fixture

STACK = read_fixture(FIXTURES / "k8s-app-stack.yaml")
STAGING = read_fixture(VARIANTS / "k8s-app-stack.v1.yaml")


def paths(text: str, variants: tuple[str, ...] = ()) -> dict[str, str]:
    res = KubernetesDetector().detect(DetectionContext(text=text, variants=variants))
    return {".".join(c.values_path or []): c.value for c in res.candidates}


def test_profile_lists_resources_in_order() -> None:
    p = detect_profile(STACK)
    assert p.format == "kubernetes" and p.helm_suggested
    assert [(r.kind, r.name) for r in p.resources] == [
        ("ConfigMap", "storefront-config"),
        ("Deployment", "storefront"),
        ("Service", "storefront"),
        ("Ingress", "storefront"),
    ]
    assert {r.namespace for r in p.resources} == {"shop-prod"}


def test_chunks_tile_the_input() -> None:
    chunks = [r.chunk for r in parse_resources(STACK)]
    assert chunks[0].start == 0 and chunks[-1].end == len(STACK)
    for a, b in itertools.pairwise(chunks):
        assert a.end == b.start
    # The leading comment belongs to the first resource's chunk.
    assert STACK[chunks[0].start : chunks[0].end].startswith("# Storefront")


def test_not_kubernetes() -> None:
    for text in (
        "server { listen 80; }\n",
        "a: 1\nb: [unclosed\n",  # invalid YAML
        "apiVersion: v1\nkind: Thing\n",  # no metadata.name
        '{"apiVersion": "v1", "kind": "x"}',
        "",
    ):
        assert detect_profile(text).format is None, text


def test_json_manifest_and_crlf_are_kubernetes() -> None:
    crlf = STACK.replace("\n", "\r\n")
    p = detect_profile(crlf)
    assert len(p.resources) == 4
    assert paths(crlf)["image.tag"] == "3.9.1"
    js = (
        '{"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "c"}, "data": {"A": "b"}}\n'
    )
    assert detect_profile(js).format == "kubernetes"
    assert paths(js)["config.A"] == "b"


def test_existing_chart_is_not_offered_again() -> None:
    text = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: {{ .Values.name }}\n"
    # Not valid YAML for the composer ({{ is a flow mapping), so not recognised at all.
    assert not detect_profile(text).helm_suggested
    quoted = 'apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: "{{ .Values.name }}"\n'
    p = detect_profile(quoted)
    assert p.format == "kubernetes" and not p.helm_suggested and p.reason


def test_two_resources_in_one_document_list_is_one_resource() -> None:
    text = "apiVersion: v1\nkind: List\nmetadata:\n  name: all\nitems: []\n"
    assert len(parse_resources(text)) == 1


def test_detector_paths_for_single_workload() -> None:
    p = paths(STACK)
    assert p["replicaCount"] == "4"
    assert p["image.repository"] == "ghcr.io/acme/storefront"
    assert p["image.tag"] == "3.9.1"
    assert p["image.pullPolicy"] == "IfNotPresent"
    assert p["containerPort"] == "8080"
    assert p["resources.limits.memory"] == "1Gi"
    assert p["resources.limits.cpu"] == "1"  # inside quotes
    assert p["service.port"] == "80" and p["service.targetPort"] == "8080"
    assert p["ingress.host"] == "shop.example.com"
    assert p["ingress.className"] == "nginx"
    assert p["config.LOG_LEVEL"] == "info"
    assert p["namespace"] == "shop-prod"


def test_detector_prefixes_when_several_workloads() -> None:
    two = (
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: api\nspec:\n  replicas: 2\n"
        "  template:\n    spec:\n      containers:\n        - name: api\n          image: r/api:1\n"
        "---\n"
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: worker-pool\nspec:\n"
        "  replicas: 5\n  template:\n    spec:\n      containers:\n        - name: w\n"
        "          image: r/worker@sha256:abc\n"
    )
    p = paths(two)
    assert p["api.replicaCount"] == "2"
    assert p["workerPool.replicaCount"] == "5"
    assert p["api.image.tag"] == "1"
    assert p["workerPool.image.digest"] == "sha256:abc"
    assert p["workerPool.image.repository"] == "r/worker"


def test_detector_skips_escaped_and_multiline_scalars() -> None:
    text = (
        'apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: c\ndata:\n  A: "x\\ty"\n'
        "  B: |\n    line\n  C: plain\n"
    )
    p = paths(text)
    assert "config.A" not in p and "config.B" not in p
    assert p["config.C"] == "plain"


def test_detector_reads_variant_values_per_path() -> None:
    res = KubernetesDetector().detect(DetectionContext(text=STACK, variants=(STAGING,)))
    tag = next(c for c in res.candidates if c.values_path == ["image", "tag"])
    assert tag.variant_values == ["3.10.0-rc.2"]
    repo = next(c for c in res.candidates if c.values_path == ["image", "repository"])
    assert repo.variant_values is None  # unchanged


def test_suggest_drops_structure_and_whole_images() -> None:
    res = suggest(STACK, [STAGING])
    values = {c.value for c in res.candidates}
    assert "apps/v1" not in values and "Deployment" not in values
    assert "app.kubernetes.io" not in values  # label key, not a hostname
    assert "ghcr.io/acme/storefront:3.9.1" not in values
    by_path = {".".join(c.values_path): c for c in res.candidates if c.values_path}
    assert by_path["image.tag"].variant_values == ["3.10.0-rc.2"]
    assert by_path["replicaCount"].name == "replica_count"
    assert res.profile.helm_suggested


def test_camel() -> None:
    assert camel("payments-api") == "paymentsApi"
    assert camel("ephemeral-storage") == "ephemeralStorage"
    assert camel("9lives") == "v9lives"
    assert camel("---") == "value"
