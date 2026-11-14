"""Map loops: open-ended key/value blocks (environment:, labels:) as .items() loops."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from templatize.ansible import build_role
from templatize.build import accept_suggestions, build, specs_from_loop_candidates
from templatize.helm import build_chart
from templatize.suggest import suggest

COMPOSE = (
    "services:\n  web:\n    image: app:1\n    environment:\n"
    "      REDIS_HOST: cache\n      LOG_LEVEL: info\n"
)
COMPOSE_EXAMPLE = COMPOSE.replace("info\n", 'debug\n      FEATURE_X: "on"\n').replace('"on"', "on")


def test_open_ended_maps_are_offered_not_auto_accepted() -> None:
    res = suggest(COMPOSE)
    (lp,) = res.loops
    assert (lp.name, lp.map, lp.auto_accept) == ("web_environment", True, False)
    assert [f.name for f in lp.fields] == ["key", "value"]
    _, loops = accept_suggestions(res.candidates, res.loops)
    assert loops == []  # "accept all" keeps per-key variables


def test_fixed_settings_maps_are_not_loops() -> None:
    # Lower-case keys under a non-open parent are settings, not data.
    assert suggest("database:\n  host: db\n  port: 5432\n").loops == []
    # A list of {name, value} records is a list loop, not a map per item.
    text = "env:\n  - name: A\n    value: x\n  - name: B\n    value: y\n"
    assert [(lp.name, lp.map) for lp in suggest(text).loops] == [("env", False)]


def test_map_loop_renders_items_and_examples() -> None:
    res = suggest(COMPOSE, [COMPOSE_EXAMPLE])
    (lp,) = res.loops
    assert lp.variant_values == [{"REDIS_HOST": "cache", "LOG_LEVEL": "debug", "FEATURE_X": "on"}]
    built = build(COMPOSE, [], loops=specs_from_loop_candidates([lp]))
    assert built.roundtrip.ok
    assert "{% for key, value in web_environment.items() %}" in built.template.content
    assert yaml.safe_load(built.env.content)["web_environment"] == {
        "REDIS_HOST": "cache",
        "LOG_LEVEL": "info",
    }


K8S = (
    "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: app\n  labels:\n"
    "    tier: web\n    team: shop\ndata:\n  B_KEY: two\n  A_KEY: one\n"
)
K8S_EXAMPLE = K8S.replace("  A_KEY: one\n", "  A_KEY: uno\n  C_KEY: three\n")


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_helm_keeps_map_order(tmp_path: Path) -> None:
    """Go ranges maps in sorted key order; charts keep maps as key/value rows so the
    unsorted B_KEY, A_KEY order survives, with real helm."""
    res = suggest(K8S, [K8S_EXAMPLE])
    loops = specs_from_loop_candidates(res.loops)
    assert {lp.name for lp in loops} == {"metadata_labels", "data"}
    chart = build_chart(K8S, [], [K8S_EXAMPLE], loops=loops)
    assert chart.ok, chart.roundtrips
    root = tmp_path / chart.chart_name
    for f in chart.files:
        (root / f.path).parent.mkdir(parents=True, exist_ok=True)
        (root / f.path).write_text(f.content, encoding="utf-8", newline="")
    helm = shutil.which("helm")
    assert helm
    for expected, extra in [(K8S, []), (K8S_EXAMPLE, ["-f", str(root / "values-example-2.yaml")])]:
        out = subprocess.run(
            [helm, "template", "r", str(root), *extra], capture_output=True, text=True, check=True
        ).stdout
        body = out.split("\n", 2)[2]  # drop "---" and "# Source:" lines
        assert body == expected


@pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="ansible not installed")
def test_ansible_role_with_map_loop(tmp_path: Path) -> None:
    res = suggest(COMPOSE, [COMPOSE_EXAMPLE])
    role = build_role(
        COMPOSE, [], specs_from_loop_candidates(res.loops), variants=[COMPOSE_EXAMPLE]
    )
    assert role.roundtrip.ok and role.example_roundtrips[0].ok
    for f in role.files:
        (tmp_path / f.path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / f.path).write_text(f.content, encoding="utf-8", newline="")
    env = {
        **os.environ,
        "ANSIBLE_LOCALHOST_WARNING": "False",
        "ANSIBLE_INVENTORY_UNPARSED_WARNING": "False",
        "ANSIBLE_LOCAL_TEMP": str(tmp_path / ".t"),
        "ANSIBLE_REMOTE_TEMP": str(tmp_path / ".t"),
    }
    for expected, extra in [
        (COMPOSE, []),
        (COMPOSE_EXAMPLE, ["-e", "@config/examples/example-2.yml"]),
    ]:
        out = tmp_path / "out"
        subprocess.run(
            ["ansible-playbook", "playbook.yml", "-e", f"config_dest={out}", *extra],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            check=True,
            timeout=120,
        )
        assert out.read_text() == expected
