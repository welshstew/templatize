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


def test_envsubst_keeps_the_loop_text() -> None:
    text = "hosts:\n  - web-01.example.com\n  - web-02.example.com\n  - web-03.example.com\n"
    res = suggest(text)
    specs, loops = accept_suggestions(res.candidates, res.loops)
    assert loops, "the list should be offered as a loop"
    result = build_ir(make_ir(text, specs, loops), "envsubst")
    assert result.roundtrip.ok
    assert any("no loops" in n for n in result.notes)
    assert not result.ir.loops


def test_terraform_loops() -> None:
    text = "hosts:\n  - web-01.example.com\n  - web-02.example.com\n  - web-03.example.com\n"
    res = suggest(text)
    specs, loops = accept_suggestions(res.candidates, res.loops)
    result = build_ir(make_ir(text, specs, loops), "terraform")
    assert result.roundtrip.ok and result.ir.loops and not result.notes
    assert "%{ for host_index, host in hosts }" in result.template.content
    assert "%{ if host_index > 0 }" in result.template.content
    assert '"hosts" = [' in result.env.content
    # Edited rows render: one more host, one fewer.
    emitter = get_emitter("terraform")
    out = emitter.render(result.template.content, {"hosts": ["a.example.com", "b.example.com"]})
    assert out == "hosts:\n  - a.example.com\n  - b.example.com\n"


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


def _terraform_output(tmp_path: Path, result) -> bytes:  # type: ignore[no-untyped-def]
    for f in result.files:
        (tmp_path / f.path).write_bytes(f.content.encode("utf-8", "surrogatepass"))
    assert TERRAFORM is not None
    run = {"cwd": tmp_path, "capture_output": True, "check": True}
    subprocess.run([TERRAFORM, "init", "-input=false"], **run)  # type: ignore[call-overload]
    subprocess.run([TERRAFORM, "apply", "-auto-approve", "-input=false"], **run)  # type: ignore[call-overload]
    out = subprocess.run([TERRAFORM, "output", "-raw", "rendered"], **run)  # type: ignore[call-overload]
    return bytes(out.stdout)


@pytest.mark.skipif(TERRAFORM is None, reason="terraform / tofu not installed")
@pytest.mark.parametrize("name", list(DOCS))
def test_main_tf_with_real_terraform(tmp_path: Path, name: str) -> None:
    text = DOCS[name]
    assert _terraform_output(tmp_path, _built(text, "terraform")) == text.encode(
        "utf-8", "surrogatepass"
    )


@pytest.mark.skipif(TERRAFORM is None, reason="terraform / tofu not installed")
@pytest.mark.parametrize(
    "literal", ["$${x}", "$$${x}", "%%{", "$", "$$", "%", "${", "%{ for }", "a\rb", "\r", "\r\n"]
)
def test_awkward_literal_text_with_real_terraform(tmp_path: Path, literal: str) -> None:
    text = f"{literal}V{literal}$V%V\n"
    i = text.index("V")
    result = build(
        text, [VariableSpec(name="v", spans=[Span(start=i, end=i + 1)])], engine="terraform"
    )
    assert _terraform_output(tmp_path, result) == text.encode("utf-8")


@pytest.mark.skipif(TERRAFORM is None, reason="terraform / tofu not installed")
def test_awkward_values_with_real_terraform(tmp_path: Path) -> None:
    values = [v for v in AWKWARD_VALUES if v]
    text = "".join(f"k{i}: {v}|\n" for i, v in enumerate(values))
    specs = []
    for i, v in enumerate(values):
        start = text.index(f"k{i}: ") + len(f"k{i}: ")
        specs.append(VariableSpec(name=f"v{i}", spans=[Span(start=start, end=start + len(v))]))
    assert _terraform_output(tmp_path, build(text, specs, engine="terraform")) == text.encode(
        "utf-8"
    )


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


# --- Terraform loops: maps and nested lists ---------------------------------------

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
CONFIGMAP = (  # keys out of order: a map loop must keep the document's order
    "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: app\ndata:\n"
    "  B_KEY: two\n  A_KEY: one\n  C_KEY: three\n"
)
COMPOSE_ENV = (
    "services:\n  web:\n    image: app:1\n    environment:\n"
    "      REDIS_HOST: cache\n      LOG_LEVEL: info\n"
)


def _loop_build(text: str, *, maps: bool = False):  # type: ignore[no-untyped-def]
    from templatize.build import specs_from_loop_candidates

    res = suggest(text)
    if maps:
        return build(text, [], engine="terraform", loops=specs_from_loop_candidates(res.loops))
    specs, loops = accept_suggestions(res.candidates, res.loops)
    return build(text, specs, engine="terraform", loops=loops)


@pytest.mark.parametrize(
    ("text", "maps"), [(SERVERS, False), (USERS, False), (CONFIGMAP, True), (COMPOSE_ENV, True)]
)
def test_terraform_nested_and_map_loops(text: str, maps: bool) -> None:
    result = _loop_build(text, maps=maps)
    assert result.ir.loops and result.roundtrip.ok, result.roundtrip


def test_terraform_nested_loop_renders_other_counts() -> None:
    result = _loop_build(SERVERS)
    assert "%{ for port_index, port in server.ports }" in result.template.content
    out = get_emitter("terraform").render(
        result.template.content, {"servers": [{"name": "x", "host": "h", "ports": ["1", "2", "3"]}]}
    )
    assert (
        out == "servers:\n  - name: x\n    host: h\n    ports:\n      - 1\n      - 2\n      - 3\n"
    )


def test_terraform_map_loop_keeps_order_and_accepts_a_mapping() -> None:
    result = _loop_build(CONFIGMAP, maps=True)
    tfvars = result.env.content
    assert tfvars.index('"B_KEY"') < tfvars.index('"A_KEY"') < tfvars.index('"C_KEY"')
    # The UI preview sends a map loop as an object (insertion-ordered).
    (lp,) = [lp for lp in result.ir.loops if lp.map]
    out = get_emitter("terraform").render(
        result.template.content, {lp.name: {"Z": "26", "Y": "25"}, "name": "app"}
    )
    assert out.endswith("data:\n  Z: 26\n  Y: 25\n")


@pytest.mark.skipif(TERRAFORM is None, reason="terraform / tofu not installed")
@pytest.mark.parametrize(
    ("text", "maps"), [(SERVERS, False), (USERS, False), (CONFIGMAP, True), (COMPOSE_ENV, True)]
)
def test_terraform_nested_and_map_loops_with_real_terraform(
    tmp_path: Path, text: str, maps: bool
) -> None:
    assert _terraform_output(tmp_path, _loop_build(text, maps=maps)) == text.encode("utf-8")
