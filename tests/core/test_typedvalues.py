"""Typed values: Helm values.schema.json and Ansible meta/argument_specs.yml."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft7Validator

from templatize.ansible import build_role
from templatize.build import VariableSpec, accept_suggestions
from templatize.helm import build as build_helm
from templatize.helm import build_chart
from templatize.helm.chart import values_problems
from templatize.ir import Span, VarType
from templatize.suggest import suggest
from templatize.typedvalues import ITEM, Hint, argument_options, describe, values_schema

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = {s["id"]: s["text"] for s in json.loads((ROOT / "quality/samples.json").read_text())}
HELM = shutil.which("helm")
ANSIBLE = shutil.which("ansible-playbook")


def at(text: str, sub: str) -> Span:
    i = text.index(sub)
    return Span(start=i, end=i + len(sub))


def accepted(text: str) -> tuple[list[Any], list[Any]]:
    res = suggest(text)
    return accept_suggestions(res.candidates, res.loops)


def valid(schema: dict[str, Any], value: Any) -> bool:
    return not list(Draft7Validator(schema).iter_errors(value))


def test_describe_keeps_what_a_value_is_not_how_it_was_found() -> None:
    reason = "value of key 'replicas'; schema: Number of desired pods.; replica count"
    assert describe(reason) == "Replica count. Number of desired pods."
    assert describe("looks like email; appears 3 times") is None
    assert describe("schema: Variable references are expanded…; env LOG_LEVEL") == "Env LOG_LEVEL"
    assert describe(None) is None


def test_types_come_from_the_detectors_and_fit_every_shipped_value() -> None:
    hints = {
        ("replicas",): Hint(VarType.INT),
        ("port",): Hint(VarType.PORT, "Service port"),
        ("ratio",): Hint(VarType.FLOAT),
        ("debug",): Hint(VarType.BOOL),
        ("name",): Hint(VarType.STRING),
    }
    trees = [
        {"replicas": 3, "port": "0080", "ratio": "0.5", "debug": True, "name": "web"},
        {"replicas": 5, "port": 8080, "ratio": "1", "debug": False, "name": "api"},
    ]
    schema = values_schema(trees, hints, "Values")
    assert all(valid(schema, t) for t in trees)
    props = schema["properties"]
    assert props["port"]["description"] == "Service port"
    good = {**trees[0], "replicas": "7", "port": 443}  # a quoted number renders the same
    assert valid(schema, good)
    for bad in (
        {"replicas": "three"},
        {"port": 70000},
        {"port": "http"},
        {"ratio": "half"},
        {"debug": "maybe"},
        {"name": 1.10},  # YAML would print 1.1
    ):
        assert not valid(schema, {**trees[0], **bad}), bad


def test_strings_nothing_detected_are_left_open() -> None:
    # helm create's probe ``port: http`` also takes 8080.
    schema = values_schema([{"probe": {"port": "http"}, "count": 2}], {}, "Values")
    assert valid(schema, {"probe": {"port": 8080}, "count": 3})
    assert not valid(schema, {"probe": {"port": 8080}, "count": "two"})


def test_lists_of_rows_scalars_and_nested_lists() -> None:
    hints = {
        ("hosts", ITEM, "port"): Hint(VarType.PORT),
        ("hosts", ITEM, "name"): Hint(VarType.STRING),
        ("hosts", ITEM, "aliases", ITEM): Hint(VarType.STRING),
        ("ports",): Hint(None, "Published ports"),
        ("ports", ITEM): Hint(VarType.INT),
    }
    tree = {
        "hosts": [
            {"name": "a", "port": 1, "aliases": ["x"]},
            {"name": "b", "port": 2, "aliases": []},
        ],
        "ports": [80, 443],
    }
    schema = values_schema([tree], hints, "Values")
    assert valid(schema, tree)
    assert valid(schema, {"hosts": [], "ports": []})
    assert not valid(schema, {**tree, "ports": [80, "https"]})
    assert not valid(schema, {**tree, "hosts": [{"name": "c", "port": "x", "aliases": []}]})
    assert not valid(schema, {**tree, "hosts": [{"name": "c", "port": 3, "aliases": "x"}]})
    assert schema["properties"]["ports"]["description"] == "Published ports"


def test_exact_chart_ships_a_schema_its_values_and_examples_fit() -> None:
    text = SAMPLES["k8s"]
    variant = text.replace("replicas: 3", "replicas: 5").replace("1.14.2", "1.15.0")
    res = suggest(text, [variant])
    specs, loops = accept_suggestions(res.candidates, res.loops)
    chart = build_chart(text, specs, [variant], loops=loops)
    assert chart.ok and not chart.notes
    schema_text = chart.file("values.schema.json").content
    schema = json.loads(schema_text)
    assert schema["properties"]["replicaCount"]["type"] == ["integer", "string"]
    assert "Number of desired pods." in schema["properties"]["replicaCount"]["description"]
    tree = yaml.safe_load(chart.file("values.yaml").content)
    assert values_problems(tree, schema_text) == []
    tree["replicaCount"] = "three"
    assert values_problems(tree, schema_text) == ["replicaCount: 'three' is not an integer"]
    assert values_problems(tree, "{") and "not a valid schema" in values_problems(tree, "{")[0]


def test_scaffold_chart_ships_a_schema() -> None:
    specs, _ = accepted(SAMPLES["k8s"])
    chart = build_helm("scaffold", SAMPLES["k8s"], specs)
    schema = json.loads(chart.file("values.schema.json").content)
    assert schema["properties"]["replicaCount"]["type"] in ("integer", ["integer", "string"])
    assert valid(schema, yaml.safe_load(chart.file("values.yaml").content))


def _write(files: Any, root: Path) -> None:
    for f in files:
        target = root / f.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f.content, encoding="utf-8", newline="")


@pytest.mark.skipif(HELM is None, reason="helm not installed")
def test_helm_refuses_values_the_schema_does_not_allow(tmp_path: Path) -> None:
    assert HELM is not None
    specs, loops = accepted(SAMPLES["k8s"])
    chart = build_chart(SAMPLES["k8s"], specs, loops=loops)
    _write(chart.files, tmp_path / "c")

    def template(*args: str) -> subprocess.CompletedProcess[str]:
        cmd = [HELM, "template", "x", str(tmp_path / "c"), *args]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)

    assert template().returncode == 0
    assert template("--set", "replicaCount=5", "--set-string", "containerPort=9090").returncode == 0
    bad = template("--set", "replicaCount=three")
    assert bad.returncode != 0 and "replicaCount" in bad.stderr


def _ansible(tmp_path: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    assert ANSIBLE is not None
    env = {
        **os.environ,
        "ANSIBLE_LOCALHOST_WARNING": "False",
        "ANSIBLE_INVENTORY_UNPARSED_WARNING": "False",
        "ANSIBLE_NOCOLOR": "1",
        "ANSIBLE_LOCAL_TEMP": str(tmp_path / ".ansible-tmp"),
        "ANSIBLE_REMOTE_TEMP": str(tmp_path / ".ansible-tmp"),
    }
    return subprocess.run(
        [ANSIBLE, "playbook.yml", *extra],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        check=False,
    )


def test_role_argument_specs_follow_the_types() -> None:
    text = "port = 0080\nworkers = 4\nratio = 0.5\ndebug = yes\nhost = db\n"
    specs = [
        VariableSpec(name="port", spans=[at(text, "0080")], type=VarType.PORT),
        VariableSpec(
            name="workers", spans=[at(text, "4")], type=VarType.INT, description="schema: How many."
        ),
        VariableSpec(name="ratio", spans=[at(text, "0.5")], type=VarType.FLOAT),
        VariableSpec(name="debug", spans=[at(text, "yes")], type=VarType.BOOL),
        VariableSpec(name="host", spans=[at(text, "db")], type=VarType.HOSTNAME),
    ]
    role = build_role(text, specs, role_name="app")
    files = {f.path: f.content for f in role.files}
    options = yaml.safe_load(files["app/meta/argument_specs.yml"])["argument_specs"]["main"][
        "options"
    ]
    assert {k: v["type"] for k, v in options.items()} == {
        "app_port": "int",
        "app_workers": "int",
        "app_ratio": "float",
        "app_debug": "bool",
        "app_host": "str",
        "app_dest": "str",
    }
    assert options["app_workers"]["description"] == "How many."


def test_role_argument_specs_for_lists_and_values_that_do_not_convert() -> None:
    observed = [
        {
            "x_users": [{"name": "a", "uid": "1"}],
            "x_tags": ["a"],
            "x_env": {"A": "1"},
            "x_n": "many",
        }
    ]
    hints = {("x_users", ITEM, "uid"): Hint(VarType.INT), ("x_n",): Hint(VarType.INT)}
    options = argument_options(observed, hints)
    assert options["x_users"] == {
        "type": "list",
        "elements": "dict",
        "options": {"name": {"type": "str"}, "uid": {"type": "int"}},
    }
    assert options["x_tags"] == {"type": "list", "elements": "str"}
    assert options["x_env"] == {"type": "dict"}
    assert options["x_n"] == {"type": "str"}  # the default would not pass as an int


@pytest.mark.skipif(ANSIBLE is None, reason="ansible-playbook not installed")
def test_ansible_refuses_a_bad_value_before_rendering(tmp_path: Path) -> None:
    text = "port = 0080\nhost = db\n"
    specs = [
        VariableSpec(name="port", spans=[at(text, "0080")], type=VarType.PORT),
        VariableSpec(name="host", spans=[at(text, "db")]),
    ]
    role = build_role(text, specs, filename="app.ini")
    _write(role.files, tmp_path)
    ok = _ansible(tmp_path, "-e", f"config_dest={tmp_path / 'out'}")
    assert ok.returncode == 0, ok.stdout[-2000:]
    assert (tmp_path / "out").read_text() == text  # validation does not change "0080"
    bad = _ansible(tmp_path, "-e", f"config_dest={tmp_path / 'bad'}", "-e", "config_port=http")
    assert bad.returncode != 0 and "config_port" in bad.stdout
    assert not (tmp_path / "bad").exists()
