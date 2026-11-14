from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from templatize.cli import main
from tests.conftest import FIXTURES, VARIANTS


def test_suggest_table(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["suggest", str(FIXTURES / "nginx.conf")]) == 0
    out = capsys.readouterr().out
    assert "10.20.30.41" in out and "candidate(s)" in out


def test_suggest_json_with_variants(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(
        [
            "suggest",
            str(FIXTURES / "k8s-deployment.yaml"),
            "--variant",
            str(VARIANTS / "k8s-deployment.v2.yaml"),
            "--json",
        ]
    )
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert any(c["variant_values"] for c in data["candidates"])


def test_build_accept_all_outputs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(
        [
            "build",
            str(FIXTURES / "docker-compose.yml"),
            "--accept-all",
            "--no-loops",
            "-o",
            str(tmp_path / "out"),
            "--zip",
            str(tmp_path / "b.zip"),
        ]
    )
    assert rc == 0
    assert "round-trip: PASS" in capsys.readouterr().err
    assert (tmp_path / "out" / "template.j2").exists()
    assert (tmp_path / "out" / ".env").read_text().count("=") >= 5
    with zipfile.ZipFile(tmp_path / "b.zip") as zf:
        assert sorted(zf.namelist()) == [".env", "README.md", "render.py", "template.j2"]


def test_build_accept_all_turns_lists_into_loops(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "out"
    assert (
        main(["build", str(FIXTURES / "docker-compose.yml"), "--accept-all", "-o", str(out)]) == 0
    )
    assert "1 loops" in capsys.readouterr().err
    assert "{% for depends_on_item in depends_on %}" in (out / "template.j2").read_text()
    assert "depends_on:\n- db\n- cache\n" in (out / "values.yaml").read_text()


def test_build_prints_template_by_default(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["build", str(FIXTURES / "crlf.ini"), "--accept-all"]) == 0
    assert "{{ " in capsys.readouterr().out


def test_build_with_vars_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    src = tmp_path / "in.txt"
    src.write_text("hello world\n")
    spec = tmp_path / "vars.json"
    spec.write_text(json.dumps([{"name": "who", "spans": [{"start": 6, "end": 11}]}]))
    assert main(["build", str(src), "--vars", str(spec), "--print", "env"]) == 0
    assert 'WHO="world"' in capsys.readouterr().out


def test_build_reports_invalid_vars(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    src = tmp_path / "in.txt"
    src.write_text("hello\n")
    spec = tmp_path / "vars.json"
    spec.write_text(json.dumps([{"name": "Bad Name", "spans": [{"start": 0, "end": 1}]}]))
    assert main(["build", str(src), "--vars", str(spec)]) == 1
    assert "error:" in capsys.readouterr().err


def test_build_with_loops_file(tmp_path: Path) -> None:
    doc = tmp_path / "hosts.txt"
    doc.write_text("[a, b]\n")
    loops = tmp_path / "loops.json"
    loops.write_text(
        json.dumps(
            [
                {
                    "name": "names",
                    "scalar": True,
                    "items": [{"start": 1, "end": 2}, {"start": 4, "end": 5}],
                }
            ]
        )
    )
    vars_ = tmp_path / "vars.json"
    vars_.write_text("[]")
    out = tmp_path / "out"
    assert (
        main(["build", str(doc), "--vars", str(vars_), "--loops", str(loops), "-o", str(out)]) == 0
    )
    assert (out / "values.yaml").read_text().endswith("names:\n- a\n- b\n")
    assert "{% for name in names %}" in (out / "template.j2").read_text()


def test_role_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "role"
    rc = main(
        ["role", str(FIXTURES / "nginx.conf"), "--accept-all", "--name", "web", "-o", str(out)]
    )
    assert rc == 0
    assert "round-trip (defaults): PASS" in capsys.readouterr().err
    assert (out / "web" / "templates" / "nginx.conf.j2").exists()
    assert (out / "playbook.yml").read_text().count("- web") == 1
