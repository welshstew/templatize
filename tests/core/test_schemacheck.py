"""Checking rendered output against its JSON Schema."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from templatize.schemacheck import MAX_PROBLEMS, _documents, check_output
from templatize.userschema import SchemaError

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = {s["id"]: s["text"] for s in json.loads((ROOT / "quality/samples.json").read_text())}


def test_yaml_is_read_with_yaml_1_2_rules() -> None:
    (doc,) = _documents("on: push\nyes: 1\nwhen: 2026-01-01\nflag: true\n")
    assert doc == {"on": "push", "yes": 1, "when": "2026-01-01", "flag": True}


@pytest.mark.parametrize("sample", ["k8s", "k8s-stack", "compose", "gha", "cloud-init"])
def test_samples_are_valid_against_their_bundled_schema(sample: str) -> None:
    check = check_output(SAMPLES[sample])
    assert check is not None and check.ok, check
    assert check.schemas


def test_a_wrong_type_and_a_misspelt_key_are_reported() -> None:
    check = check_output(SAMPLES["k8s"].replace("replicas: 3", "replicas: three"))
    assert check is not None and not check.ok
    assert [p.path for p in check.problems] == ["spec.replicas"]
    check = check_output(SAMPLES["k8s"].replace("containerPort", "containerport"))
    assert check is not None and any("containerport" in p.message for p in check.problems)


def test_each_kubernetes_document_uses_its_kind() -> None:
    check = check_output(SAMPLES["k8s-stack"].replace("replicas: 4", "replicas: four"))
    assert check is not None and not check.ok
    assert {p.document for p in check.problems} == {1}
    assert len(check.schemas) >= 3


def test_the_analysed_type_decides_when_detection_on_the_output_would_not() -> None:
    edited = SAMPLES["gha"].replace("    runs-on: ubuntu-24.04\n", "")
    assert check_output(edited) is None  # no longer confidently a workflow
    check = check_output(edited, document_type="github-actions")
    assert check is not None and not check.ok
    assert any("runs-on" in p.message for p in check.problems)


def test_a_supplied_schema_and_a_referenced_one() -> None:
    schema = '{"title": "Mine", "type": "object", "required": ["port"], "properties": {"port": {"type": "integer"}}}'
    check = check_output('{"port": "eighty"}', schema=schema)
    assert check is not None and check.schemas == ["Mine"]
    assert [p.path for p in check.problems] == ["port"]
    referenced = (
        "# yaml-language-server: $schema=https://json.schemastore.org/chart.json\nname: x\n"
    )
    check = check_output(referenced)
    assert check is not None and check.schemas == ["Helm Chart.yaml"] and not check.ok


def test_unrelated_documents_are_not_checked() -> None:
    assert check_output("Dear Sarah,\n\nThanks.\n") is None
    assert check_output("server {\n  listen 80;\n}\n") is None


def test_broken_yaml_is_reported_for_schema_documents() -> None:
    check = check_output("apiVersion: v1\nkind: Service\nspec: [\n")
    assert check is not None and not check.ok and "not valid YAML" in (check.note or "")


def test_an_invalid_supplied_schema_is_an_error() -> None:
    with pytest.raises(SchemaError):
        check_output("a: 1\n", schema="[1]")


def test_problems_are_capped() -> None:
    schema = json.dumps({"type": "object", "additionalProperties": {"type": "integer"}})
    doc = "\n".join(f"k{i}: x" for i in range(50)) + "\n"
    check = check_output(doc, schema=schema)
    assert check is not None and len(check.problems) == MAX_PROBLEMS
    assert check.note and "50 problems" in check.note


def test_checking_never_touches_the_network(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    assert check_output(SAMPLES["k8s-stack"]) is not None
    draft7 = '{"$schema": "http://json-schema.org/draft-07/schema#", "properties": {"a": {"type": "integer"}}}'
    check = check_output('{"a": "x"}', schema=draft7)
    assert check is not None and not check.ok
