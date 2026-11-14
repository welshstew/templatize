"""Ansible role output: layout, vars files, round trip, and real ansible-playbook."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from templatize.ansible import RoleError, build_role, dump_vars, load_vars
from templatize.build import LoopSpec, VariableSpec, accept_suggestions
from templatize.ir import Span
from templatize.suggest import suggest
from tests.conftest import FIXTURES, read_fixture, variants_for

ANSIBLE = shutil.which("ansible-playbook")


def at(text: str, sub: str, n: int = 0) -> Span:
    i = -1
    for _ in range(n + 1):
        i = text.index(sub, i + 1)
    return Span(start=i, end=i + len(sub))


def files(result: object) -> dict[str, str]:
    return {f.path: f.content for f in result.files}  # type: ignore[attr-defined]


def test_role_layout_and_prefixed_variables() -> None:
    text = "host = db.example.com\nport = 5432\n"
    specs = [
        VariableSpec(name="host", spans=[at(text, "db.example.com")]),
        VariableSpec(name="port", spans=[at(text, "5432")]),
    ]
    res = build_role(text, specs, role_name="db", filename="app.ini")
    assert res.roundtrip.ok
    out = files(res)
    assert sorted(out) == [
        "db/README.md",
        "db/defaults/main.yml",
        "db/meta/main.yml",
        "db/tasks/main.yml",
        "db/templates/app.ini.j2",
        "playbook.yml",
    ]
    assert out["db/templates/app.ini.j2"] == "host = {{ db_host }}\nport = {{ db_port }}\n"
    assert load_vars(out["db/defaults/main.yml"]) == {
        "db_host": "db.example.com",
        "db_port": "5432",
        "db_dest": "/tmp/app.ini",
    }
    tasks = out["db/tasks/main.yml"]
    assert "trim_blocks: false" in tasks and 'newline_sequence: "\\n"' in tasks


def test_template_syntax_in_values_is_unsafe() -> None:
    text = "greeting: {{ hello }} and {% raw %}\n"
    spec = VariableSpec(name="greeting", spans=[Span(start=10, end=len(text) - 1)])
    res = build_role(text, [spec])
    defaults = files(res)["config/defaults/main.yml"]
    assert '!unsafe "{{ hello }} and {% raw %}"' in defaults
    assert res.roundtrip.ok


def test_vars_files_roundtrip_tricky_values() -> None:
    values = {"a": "yes", "b": "0080", "c": "{{ x }}", "d": ["1e3", "{% y %}"], "e": [{"k": "~"}]}
    assert load_vars(dump_vars(values, "h")) == values


def test_crlf_document_sets_newline_sequence() -> None:
    text = read_fixture(FIXTURES / "crlf.ini")
    res = suggest(text)
    specs, loops = accept_suggestions(res.candidates, res.loops)
    role = build_role(text, specs, loops)
    assert role.roundtrip.ok
    assert 'newline_sequence: "\\r\\n"' in files(role)["config/tasks/main.yml"]


def test_loops_and_example_vars_files() -> None:
    text = "users:\n  - name: ana\n    role: admin\n  - name: bo\n    role: dev\n"
    example = "users:\n  - name: cy\n    role: admin\n  - name: di\n    role: dev\n  - name: ed\n    role: ops\n"
    res = suggest(text, [example])
    specs, loops = accept_suggestions(res.candidates, res.loops)
    role = build_role(text, specs, loops, variants=[example], variant_labels=["staging"])
    assert role.roundtrip.ok and role.example_roundtrips[0].ok, role.example_roundtrips
    staging = load_vars(files(role)["config/examples/staging.yml"])
    assert staging == {
        "config_users": [
            {"name": "cy", "role": "admin"},
            {"name": "di", "role": "dev"},
            {"name": "ed", "role": "ops"},
        ]
    }


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"role_name": "Bad-Name"}, "invalid role name"),
        ({"filename": "../x"}, "invalid file name"),
        ({"variants": ["x\n"], "variant_labels": ["Bad"]}, "invalid example label"),
    ],
)
def test_invalid_role_options(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(RoleError, match=message):
        build_role("x\n", [], **kwargs)  # type: ignore[arg-type]


def test_loop_spec_without_suggestions() -> None:
    text = "[a, b]\n"
    loop = LoopSpec(name="names", items=[at(text, "a"), at(text, "b")], scalar=True)
    role = build_role(text, [], [loop], role_name="r")
    assert "{% for name in r_names %}" in files(role)["r/templates/config.j2"]
    assert role.roundtrip.ok


# --- real Ansible -------------------------------------------------------------------

REAL = [
    "crlf.ini",
    "jinja_literals.txt",
    "endraw.txt",
    "docker-compose.yml",
    "k8s-deployment.yaml",
    "no_trailing_newline.env",
    "unicode.txt",
    "mixed_newlines.txt",
    "business_letter.txt",
    "nginx.conf",
]


@pytest.mark.skipif(ANSIBLE is None, reason="ansible-playbook not installed")
@pytest.mark.parametrize("name", REAL)
def test_ansible_playbook_renders_byte_exact(name: str, tmp_path: Path) -> None:
    """Run the generated playbook with real Ansible: the file it writes must be the
    input, byte for byte (and each example's vars file must give the example)."""
    assert ANSIBLE is not None
    path = FIXTURES / name
    text = read_fixture(path)
    variants = variants_for(path)
    res = suggest(text, variants)
    specs, loops = accept_suggestions(res.candidates, res.loops)
    role = build_role(text, specs, loops, filename=name, variants=variants)
    assert role.roundtrip.ok, role.roundtrip
    for f in role.files:
        target = tmp_path / f.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(f.content.encode("utf-8", "surrogatepass"))
    env = {
        **os.environ,
        "ANSIBLE_LOCALHOST_WARNING": "False",
        "ANSIBLE_INVENTORY_UNPARSED_WARNING": "False",
        "ANSIBLE_NOCOLOR": "1",
        "ANSIBLE_LOCAL_TEMP": str(tmp_path / ".ansible-tmp"),
        "ANSIBLE_REMOTE_TEMP": str(tmp_path / ".ansible-tmp"),
    }
    runs = [(path.read_bytes(), [])] + [
        (v.encode("utf-8", "surrogatepass"), ["-e", f"@config/examples/example-{i + 2}.yml"])
        for i, v in enumerate(variants)
    ]
    for k, (expected, extra) in enumerate(runs):
        out = tmp_path / f"out-{k}"
        proc = subprocess.run(
            [ANSIBLE, "playbook.yml", "-e", f"config_dest={out}", *extra],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
            check=False,
        )
        assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
        assert out.read_bytes() == expected
