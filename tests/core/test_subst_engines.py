"""The envsubst and Terraform templatefile engines: escaping, values files, round trips
(in process, and with the real tools when they are installed)."""

from __future__ import annotations

import glob
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from templatize.build import VariableSpec, accept_suggestions, build, build_ir, make_ir
from templatize.emitters import get_emitter
from templatize.emitters.envsubst import parse_values_env, shell_quote, substitute
from templatize.emitters.terraform import hcl_string, parse_tfvars, render_template
from templatize.ir import Span
from templatize.suggest import suggest

ROOT = Path(__file__).resolve().parents[2]
ENGINES = ("envsubst", "terraform")
ENVSUBST = shutil.which("envsubst")
TERRAFORM = shutil.which("terraform") or shutil.which("tofu")

AWKWARD_VALUES = [
    "plain",
    'it\'s "quoted"',
    "back\\slash \\n not a newline",
    "two\nlines\r\nand CRLF\r",
    "${HOME} $HOME $$ $${x} %{ if } %%{",
    "$",
    "%",
    "trailing $",
    "tab\tand \x01 control",
    "ünïcødé ✓ 🙂",
    "",
    "'",
    "''\\'",
]


def _docs() -> dict[str, str]:
    docs = {
        Path(p).name: Path(p).read_bytes().decode("utf-8")
        for p in sorted(glob.glob(str(ROOT / "tests/fixtures/*.*")))
        + sorted(glob.glob(str(ROOT / "tests/quality/corpus/*")))
    }
    for s in json.loads((ROOT / "tests/quality/samples.json").read_text("utf-8")):
        docs["sample:" + s["id"]] = s["text"]
    return docs


DOCS = _docs()


def _built(text: str, engine: str):  # type: ignore[no-untyped-def]
    res = suggest(text)
    specs, loops = accept_suggestions(res.candidates, res.loops)
    return build_ir(make_ir(text, specs, loops), engine)


@pytest.mark.parametrize("engine", ENGINES)
@pytest.mark.parametrize("name", list(DOCS))
def test_every_document_round_trips(engine: str, name: str) -> None:
    result = _built(DOCS[name], engine)
    assert result.roundtrip.ok, (result.roundtrip.error, result.notes)


@pytest.mark.parametrize("engine", ENGINES)
@pytest.mark.parametrize("value", AWKWARD_VALUES)
def test_awkward_values_round_trip(engine: str, value: str) -> None:
    text = f"before {value} after\n" if value else "before  after\n"
    start = text.index(" ") + 1
    spans = [Span(start=start, end=start + len(value))] if value else []
    if not spans:
        pytest.skip("an empty value has no span")
    result = build(text, [VariableSpec(name="thing", spans=spans)], engine=engine)
    assert result.roundtrip.ok, result.roundtrip


@pytest.mark.parametrize("engine", ENGINES)
@pytest.mark.parametrize(
    "literal",
    ["${x}", "$${x}", "$$${x}", "%{ for }", "%%{", "a $ b", "%", "$", "100%{", "${", "}", "\r\n"],
)
def test_awkward_literal_text_round_trips(engine: str, literal: str) -> None:
    text = f"{literal}V{literal}$V%V\n"
    i = text.index("V")
    result = build(text, [VariableSpec(name="v", spans=[Span(start=i, end=i + 1)])], engine=engine)
    assert result.roundtrip.ok, (result.template.content, result.roundtrip)


def test_literal_dollar_before_a_placeholder() -> None:
    text = "budget cap $4,000/month\n"
    i = text.index("4,000")
    result = build(
        text, [VariableSpec(name="cap", spans=[Span(start=i, end=i + 5)])], engine="terraform"
    )
    assert result.template.content == 'budget cap ${"$"}${cap}/month\n'
    assert result.roundtrip.ok


def test_terraform_escapes() -> None:
    assert hcl_string('a"b\\c\nd${e}%{f}') == '"a\\"b\\\\c\\nd$${e}%%{f}"'
    assert render_template("$${x} %%{y} ${a}", {"a": "1"}) == "${x} %{y} 1"
    assert parse_tfvars('values = {\n  "a" = "x$${y}\\n"\n  "b" = "${"$"}${"$"}"\n}\n') == {
        "a": "x${y}\n",
        "b": "$$",
    }


def test_envsubst_quoting_and_selective_substitution() -> None:
    assert shell_quote("it's") == "'it'\\''s'"
    assert parse_values_env("# c\nA='x'\\''y'\nB='two\nlines'\n") == {"A": "x'y", "B": "two\nlines"}
    assert substitute("$A ${B} $AB ${C} $", {"A": "1", "B": "2"}, ["A", "B"]) == "1 2 $AB ${C} $"


def test_envsubst_renames_keys_the_text_or_the_shell_uses() -> None:
    text = "registry: ghcr.io/acme\nrun: docker push $REGISTRY/app\nuid: 4f2a\nwait 30 seconds\n"
    specs = [
        VariableSpec(name="registry", spans=[Span(start=10, end=22)]),
        VariableSpec(
            name="uid", spans=[Span(start=text.index("4f2a"), end=text.index("4f2a") + 4)]
        ),
        VariableSpec(
            name="seconds", spans=[Span(start=text.index("30"), end=text.index("30") + 2)]
        ),
    ]
    result = build(text, specs, engine="envsubst")
    assert "${REGISTRY_VALUE}" in result.template.content
    assert "$REGISTRY/app" in result.template.content  # the text's own reference stays
    assert "UID_VALUE=" in result.env.content and "SECONDS_VALUE=" in result.env.content
    assert any("REGISTRY -> REGISTRY_VALUE" in n for n in result.notes)
    assert result.roundtrip.ok
    # The live preview sends values by variable name: they fill the renamed slots.
    emitter = get_emitter("envsubst")
    out = emitter.render(
        result.template.content, {"registry": "quay.io/x", "uid": "1", "seconds": "5"}
    )
    assert out.startswith("registry: quay.io/x\nrun: docker push $REGISTRY/app\n")


def test_engines_without_loops_keep_the_loop_text() -> None:
    text = "hosts:\n  - web-01.example.com\n  - web-02.example.com\n  - web-03.example.com\n"
    res = suggest(text)
    specs, loops = accept_suggestions(res.candidates, res.loops)
    assert loops, "the list should be offered as a loop"
    for engine in ENGINES:
        result = build_ir(make_ir(text, specs, loops), engine)
        assert result.roundtrip.ok
        assert any("no loops" in n for n in result.notes)
        assert not result.ir.loops


@pytest.mark.skipif(ENVSUBST is None, reason="envsubst not installed")
@pytest.mark.parametrize("name", list(DOCS))
def test_render_sh_with_real_envsubst(tmp_path: Path, name: str) -> None:
    text = DOCS[name]
    result = _built(text, "envsubst")
    for f in result.files:
        (tmp_path / f.path).write_bytes(f.content.encode("utf-8", "surrogatepass"))
    out = subprocess.run(
        ["sh", str(tmp_path / "render.sh")],
        capture_output=True,
        env={"PATH": "/usr/local/bin:/usr/bin:/bin"},
        check=True,
    )
    assert out.stdout == text.encode("utf-8", "surrogatepass")


@pytest.mark.skipif(ENVSUBST is None, reason="envsubst not installed")
def test_awkward_values_with_real_envsubst(tmp_path: Path) -> None:
    values = [v for v in AWKWARD_VALUES if v]
    text = "".join(f"k{i}: {v}|\n" for i, v in enumerate(values))
    specs = []
    for i, v in enumerate(values):
        start = text.index(f"k{i}: ") + len(f"k{i}: ")
        specs.append(VariableSpec(name=f"v{i}", spans=[Span(start=start, end=start + len(v))]))
    result = build(text, specs, engine="envsubst")
    for f in result.files:
        (tmp_path / f.path).write_bytes(f.content.encode("utf-8"))
    out = subprocess.run(["sh", str(tmp_path / "render.sh")], capture_output=True, check=True)
    assert out.stdout == text.encode("utf-8")


@pytest.mark.skipif(TERRAFORM is None, reason="terraform / tofu not installed")
@pytest.mark.parametrize("name", ["nginx.conf", "business_letter.txt", "jinja_literals.txt"])
def test_main_tf_with_real_terraform(tmp_path: Path, name: str) -> None:
    text = DOCS[name]
    result = _built(text, "terraform")
    for f in result.files:
        (tmp_path / f.path).write_bytes(f.content.encode("utf-8", "surrogatepass"))
    assert TERRAFORM is not None
    run = {"cwd": tmp_path, "capture_output": True, "check": True}
    subprocess.run([TERRAFORM, "init", "-input=false"], **run)  # type: ignore[call-overload]
    subprocess.run([TERRAFORM, "apply", "-auto-approve", "-input=false"], **run)  # type: ignore[call-overload]
    out = subprocess.run([TERRAFORM, "output", "-raw", "rendered"], **run)  # type: ignore[call-overload]
    assert out.stdout == text.encode("utf-8", "surrogatepass")


@pytest.mark.parametrize(
    ("engine", "template", "values"),
    [
        ("envsubst", "template.tmpl", "values.env"),
        ("terraform", "template.tftpl", "terraform.tfvars"),
    ],
)
def test_cli_build_with_an_engine(tmp_path: Path, engine: str, template: str, values: str) -> None:
    from templatize.cli import main

    src = tmp_path / "nginx.conf"
    src.write_bytes((ROOT / "tests/fixtures/nginx.conf").read_bytes())
    out = tmp_path / "out"
    assert main(["build", str(src), "--accept-all", "--engine", engine, "-o", str(out)]) == 0
    assert (out / template).is_file() and (out / values).is_file()
