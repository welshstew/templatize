"""Nested loops: a list inside each item of a list (one level)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from templatize.ansible import build_role
from templatize.build import LoopSpec, accept_suggestions, build, specs_from_loop_candidates
from templatize.emitters.jinja2 import Jinja2Emitter
from templatize.helm import build_chart
from templatize.ir import IRValidationError, Span
from templatize.suggest import suggest

SERVERS = (
    "servers:\n"
    "  - name: web\n    host: 10.0.0.1\n    ports:\n      - 80\n      - 443\n"
    "  - name: api\n    host: 10.0.0.2\n    ports:\n      - 8080\n"
)
USERS = (
    "users:\n"
    "  - login: ana\n    keys:\n      - type: ed25519\n        id: k1\n"
    "  - login: bo\n    keys:\n      - type: rsa\n        id: k2\n      - type: ed25519\n        id: k3\n"
)


def test_nested_list_is_one_loop_with_a_child() -> None:
    (lp,) = suggest(SERVERS).loops
    assert (lp.name, lp.item, [f.name for f in lp.fields]) == (
        "servers",
        "server",
        ["name", "host"],
    )
    (child,) = lp.children
    assert (child.name, child.item, child.groups, child.scalar) == ("ports", "port", [2, 1], True)
    (users,) = suggest(USERS).loops
    (keys,) = users.children
    assert [f.name for f in keys.fields] == ["type", "id"] and keys.groups == [1, 2]


def test_nested_loop_renders_any_number_of_inner_items() -> None:
    res = suggest(SERVERS)
    specs, loops = accept_suggestions(res.candidates, res.loops)
    built = build(SERVERS, specs, loops=loops)
    assert built.roundtrip.ok
    assert "{% for port in server.ports %}" in built.template.content
    assert yaml.safe_load(built.env.content)["servers"][0]["ports"] == ["80", "443"]
    out = Jinja2Emitter().render(
        built.template.content,
        {"servers": [{"name": "x", "host": "h", "ports": ["1", "2", "3"]}]},
    )
    assert (
        out == "servers:\n  - name: x\n    host: h\n    ports:\n      - 1\n      - 2\n      - 3\n"
    )


def test_examples_with_other_inner_counts() -> None:
    example = SERVERS.replace("      - 8080\n", "      - 8080\n      - 8443\n")
    (lp,) = suggest(SERVERS, [example]).loops
    assert lp.confidence >= 0.95
    assert lp.variant_values == [
        [
            {"name": "web", "host": "10.0.0.1", "ports": ["80", "443"]},
            {"name": "api", "host": "10.0.0.2", "ports": ["8080", "8443"]},
        ]
    ]


def test_nested_validation() -> None:
    def at(sub: str) -> Span:
        i = SERVERS.index(sub)
        return Span(start=i, end=i + len(sub))

    items = [
        Span(start=at("name: web").start, end=at("- 443").end),
        Span(start=at("name: api").start, end=at("- 8080").end),
    ]
    child = LoopSpec(
        name="ports", items=[at("80"), at("443"), at("8080")], scalar=True, groups=[1, 1]
    )
    with pytest.raises((IRValidationError, ValueError), match="groups do not add up"):
        build(SERVERS, [], loops=[LoopSpec(name="servers", items=items, children=[child])])
    lonely = LoopSpec(name="servers", items=items)
    with pytest.raises((IRValidationError, ValueError), match="has no fields"):
        build(SERVERS, [], loops=[lonely])


DEPLOY = (
    "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: app\nspec:\n  template:\n"
    "    spec:\n      containers:\n"
    "        - name: web\n          image: web:1\n          ports:\n"
    "            - containerPort: 80\n            - containerPort: 443\n"
    "        - name: metrics\n          image: exporter:2\n          ports:\n"
    "            - containerPort: 9100\n"
)


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_helm_nested_range(tmp_path: Path) -> None:
    res = suggest(DEPLOY)
    chart = build_chart(DEPLOY, [], loops=specs_from_loop_candidates(res.loops))
    assert chart.ok, chart.roundtrips
    files = {f.path: f.content for f in chart.files}
    assert "{{ range $j, $port := $container.ports }}" in files["templates/deployment.yaml"]
    root = tmp_path / chart.chart_name
    for p, c in files.items():
        (root / p).parent.mkdir(parents=True, exist_ok=True)
        (root / p).write_text(c, encoding="utf-8", newline="")
    (tmp_path / "more.yaml").write_text(
        "containers:\n  - name: x\n    image: x:1\n"
        "    ports: [{container_port: 1}, {container_port: 2}, {container_port: 3}]\n"
    )
    helm = shutil.which("helm")
    assert helm
    out = subprocess.run(
        [helm, "template", "r", str(root)], capture_output=True, text=True, check=True
    ).stdout
    assert out.split("\n", 2)[2] == DEPLOY
    more = subprocess.run(
        [helm, "template", "r", str(root), "-f", str(tmp_path / "more.yaml")],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert (
        "          ports:\n            - containerPort: 1\n            - containerPort: 2\n"
        "            - containerPort: 3\n"
    ) in more


@pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="ansible not installed")
def test_ansible_nested_loop(tmp_path: Path) -> None:
    res = suggest(SERVERS)
    specs, loops = accept_suggestions(res.candidates, res.loops)
    role = build_role(SERVERS, specs, loops)
    assert role.roundtrip.ok
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
    out = tmp_path / "out"
    subprocess.run(
        ["ansible-playbook", "playbook.yml", "-e", f"config_dest={out}"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        check=True,
        timeout=120,
    )
    assert out.read_text() == SERVERS
