"""Loops (repeated blocks): IR validation, Jinja2 emission, values.yaml and round trip."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from templatize.build import LoopFieldSpec, LoopSpec, VariableSpec, build, singular
from templatize.ir import IRValidationError, Span
from templatize.valuesfile import parse_values, serialize_values
from tests.core.test_properties import TRICKY

# IR errors surface as IRValidationError, or wrapped by pydantic's model validation.
INVALID = (IRValidationError, ValidationError)


def at(text: str, sub: str, n: int = 0) -> Span:
    i = -1
    for _ in range(n + 1):
        i = text.index(sub, i + 1)
    return Span(start=i, end=i + len(sub))


def items_of(text: str, *subs: str) -> list[Span]:
    out, pos = [], 0
    for sub in subs:
        i = text.index(sub, pos)
        out.append(Span(start=i, end=i + len(sub)))
        pos = i + len(sub)
    return out


SQL = (
    "INSERT INTO users (tenant_id, email, role)\nVALUES\n"
    "  ('t-1', 'a@x.example', 'admin'),\n"
    "  ('t-1', 'b@x.example', 'member');\n"
)


def sql_loop(text: str = SQL) -> LoopSpec:
    rows = items_of(text, "  ('t-1', 'a@x.example', 'admin')", "  ('t-1', 'b@x.example', 'member')")
    return LoopSpec(
        name="users",
        items=rows,
        fields=[
            LoopFieldSpec(name="email", spans=[at(text, "a@x.example"), at(text, "b@x.example")]),
            LoopFieldSpec(name="role", spans=[at(text, "admin"), at(text, "member")]),
        ],
    )


def tenant(text: str = SQL) -> VariableSpec:
    return VariableSpec(name="tenant_id", spans=[at(text, "t-1", i) for i in range(2)])


def test_sql_rows_render_as_a_loop() -> None:
    result = build(SQL, [tenant()], loops=[sql_loop()])
    assert result.roundtrip.ok, result.roundtrip
    assert result.template.content == (
        "INSERT INTO users (tenant_id, email, role)\nVALUES\n"
        "{% for user in users %}{% if not loop.first %},\n{% endif %}"
        "  ('{{ tenant_id }}', '{{ user.email }}', '{{ user.role }}'){% endfor %};\n"
    )
    assert result.env.path == "values.yaml"
    assert parse_values(result.env.content) == {
        "tenant_id": "t-1",
        "users": [
            {"email": "a@x.example", "role": "admin"},
            {"email": "b@x.example", "role": "member"},
        ],
    }


def test_more_items_extend_the_output() -> None:
    from templatize.emitters.jinja2 import Jinja2Emitter

    result = build(SQL, [tenant()], loops=[sql_loop()])
    users = [{"email": f"u{i}@x.example", "role": "member"} for i in range(3)]
    out = Jinja2Emitter().render(result.template.content, {"tenant_id": "t-9", "users": users})
    assert out.count("('t-9', 'u") == 3 and out.endswith("'member');\n")
    empty = Jinja2Emitter().render(result.template.content, {"tenant_id": "t", "users": []})
    assert empty == "INSERT INTO users (tenant_id, email, role)\nVALUES\n;\n"


def test_scalar_loop_json_list() -> None:
    text = 'KAFKA = ["10.0.0.1:9092", "10.0.0.2:9092", "10.0.0.3:9092"]\n'
    items = [at(text, f"10.0.0.{i}:9092") for i in (1, 2, 3)]
    result = build(text, [], loops=[LoopSpec(name="brokers", items=items, scalar=True)])
    assert result.roundtrip.ok
    assert '{% for broker in brokers %}{% if not loop.first %}", "{% endif %}{{ broker }}' in (
        result.template.content
    )
    assert parse_values(result.env.content) == {"brokers": [f"10.0.0.{i}:9092" for i in (1, 2, 3)]}


def test_single_item_loop_and_crlf() -> None:
    text = "servers:\r\n  - host: a.example\r\n    port: '80'\r\nend\r\n"
    item = at(text, "  - host: a.example\r\n    port: '80'")
    loop = LoopSpec(
        name="servers",
        items=[item],
        fields=[
            LoopFieldSpec(name="host", spans=[at(text, "a.example")]),
            LoopFieldSpec(name="port", spans=[at(text, "80")]),
        ],
    )
    result = build(text, [], loops=[loop])
    assert result.roundtrip.ok, result.roundtrip
    assert "if not loop.first" not in result.template.content  # no separator known


def test_template_syntax_in_items_is_escaped() -> None:
    text = "x{[{{a}}|{%b%}],[{{c}}|{%d%}]}\n"
    items = items_of(text, "[{{a}}|{%b%}]", "[{{c}}|{%d%}]")
    loop = LoopSpec(
        name="pairs",
        items=items,
        fields=[
            LoopFieldSpec(name="left", spans=[at(text, "a"), at(text, "c")]),
            LoopFieldSpec(name="right", spans=[at(text, "b"), at(text, "d")]),
        ],
    )
    assert build(text, [], loops=[loop]).roundtrip.ok


def test_values_yaml_strings_roundtrip() -> None:
    text = "[0080|yes|1e3|q: r|~|tab\t|  x  |é|\\n|'\"|\x85]\n"
    vals = ["0080", "yes", "1e3", "q: r", "~", "tab\t", "  x  ", "é", "\\n", "'\"", "\x85"]
    items, pos = [], 1
    for v in vals:
        items.append(Span(start=pos, end=pos + len(v)))
        pos += len(v) + 1
    result = build(text, [], loops=[LoopSpec(name="things", items=items, scalar=True)])
    assert result.roundtrip.ok, result.roundtrip
    assert yaml.safe_load(result.env.content)["things"] == vals


@pytest.mark.parametrize(
    ("loop", "message"),
    [
        (
            LoopSpec(
                name="xs",
                items=[Span(start=0, end=1), Span(start=2, end=3), Span(start=5, end=6)],
                scalar=True,
            ),
            "text between items differs",
        ),
        (
            LoopSpec(
                name="xs",
                items=[Span(start=0, end=3), Span(start=4, end=7)],
                fields=[
                    LoopFieldSpec(name="v", spans=[Span(start=0, end=1), Span(start=4, end=5)])
                ],
            ),
            "same shape",
        ),
        (
            LoopSpec(
                name="xs",
                items=[Span(start=0, end=3), Span(start=4, end=7)],
                fields=[LoopFieldSpec(name="v", spans=[Span(start=0, end=1)])],
            ),
            "one span per item",
        ),
        (
            LoopSpec(
                name="xs",
                items=[Span(start=0, end=3), Span(start=4, end=7)],
                fields=[
                    LoopFieldSpec(name="v", spans=[Span(start=0, end=1), Span(start=8, end=9)])
                ],
            ),
            "outside its item",
        ),
        (LoopSpec(name="loop", items=[Span(start=0, end=1)], scalar=True), "reserved"),
        (LoopSpec(name="xs", item="xs", items=[Span(start=0, end=1)], scalar=True), "already used"),
    ],
)
def test_invalid_loops(loop: LoopSpec, message: str) -> None:
    text = "a,b,,c,d|efgh\n"  # items 0-1 / 2-3 / 5-6: separators "," and ",,"
    if message == "same shape":
        text = "a-1 b+1\n"
    if message in ("one span per item", "outside its item"):
        text = "a-1 b-1 c\n"
    with pytest.raises(INVALID, match=message):
        build(text, [], loops=[loop])


def test_outer_variable_must_be_in_every_item() -> None:
    text = "[x,1] [y,2]\n"
    loop = LoopSpec(
        name="rows",
        items=items_of(text, "[x,1]", "[y,2]"),
        fields=[LoopFieldSpec(name="n", spans=[at(text, "1"), at(text, "2")])],
    )
    with pytest.raises(INVALID, match="same shape"):
        build(text, [VariableSpec(name="first", spans=[at(text, "x")])], loops=[loop])


def test_variable_cannot_cross_items() -> None:
    text = "a1 b2\n"
    loop = LoopSpec(
        name="pairs",
        items=items_of(text, "a1", "b2"),
        fields=[LoopFieldSpec(name="n", spans=[at(text, "1"), at(text, "2")])],
    )
    with pytest.raises(INVALID, match="crosses"):
        build(text, [VariableSpec(name="gap", spans=[Span(start=1, end=4)])], loops=[loop])


def test_overlapping_loops_rejected() -> None:
    text = "a b c\n"
    loops = [
        LoopSpec(name="xs", items=items_of(text, "a", "b"), scalar=True),
        LoopSpec(name="ys", items=items_of(text, "b", "c"), scalar=True),
    ]
    with pytest.raises(INVALID, match="overlap"):
        build(text, [], loops=loops)


def test_singular() -> None:
    assert [singular(n) for n in ("users", "entries", "address", "env", "brokers")] == [
        "user",
        "entry",
        "address_item",
        "env_item",
        "broker",
    ]


def test_render_script_reads_values_yaml(tmp_path: Path) -> None:
    result = build(SQL, [tenant()], loops=[sql_loop()])
    for f in result.files:
        (tmp_path / f.path).write_text(f.content, encoding="utf-8", newline="")
    out = subprocess.run(
        [sys.executable, "-I", str(tmp_path / "render.py"), "--set", "tenant_id=t-2"],
        capture_output=True,
        check=True,
    ).stdout.decode()
    assert out == SQL.replace("t-1", "t-2")


def test_ir_version_1_documents_still_validate() -> None:
    from templatize.ir import TemplateIR

    ir = TemplateIR.model_validate({"ir_version": 1, "text": "a\n", "variables": []})
    assert ir.loops == [] and serialize_values(ir).endswith("{}\n")


# --- property: any skeleton/separator/values round-trips -------------------------

piece = st.lists(
    st.one_of(st.sampled_from(TRICKY), st.text(min_size=1, max_size=3)), max_size=4
).map("".join)
value = st.lists(
    st.one_of(st.sampled_from(TRICKY), st.text(min_size=1, max_size=3)), min_size=1, max_size=3
).map("".join)


@settings(max_examples=300, deadline=None)
@given(
    prefix=piece,
    suffix=piece,
    sep=piece,
    skeleton=st.lists(piece, min_size=2, max_size=4),
    data=st.data(),
)
def test_random_loops_roundtrip(
    prefix: str, suffix: str, sep: str, skeleton: list[str], data: st.DataObject
) -> None:
    n_items = data.draw(st.integers(1, 4), label="items")
    n_fields = len(skeleton) - 1
    text, items, fields = prefix, [], [[] for _ in range(n_fields)]
    for i in range(n_items):
        if i:
            text += sep
        start = len(text)
        for k in range(n_fields):
            text += skeleton[k]
            v = data.draw(value, label=f"v{i}{k}")
            fields[k].append(Span(start=len(text), end=len(text) + len(v)))
            text += v
        text += skeleton[-1]
        if len(text) == start:
            return  # empty item: not a valid span
        items.append(Span(start=start, end=len(text)))
    text += suffix
    loop = LoopSpec(
        name="items",
        items=items,
        fields=[LoopFieldSpec(name=f"f{k}", spans=sp) for k, sp in enumerate(fields)],
    )
    try:
        result = build(text, [], loops=[loop])
    except INVALID:
        # A separator that differs only by chance (e.g. empty sep with adjacent items) is fine.
        return
    assert result.roundtrip.ok, (repr(text), result.roundtrip)


def test_bundle_readme_mentions_values_yaml() -> None:
    import io
    import zipfile

    from templatize.bundle import make_zip

    zf = zipfile.ZipFile(io.BytesIO(make_zip(build(SQL, [tenant()], loops=[sql_loop()]))))
    assert sorted(zf.namelist()) == ["README.md", "render.py", "template.j2", "values.yaml"]
    readme = zf.read("README.md").decode()
    assert "`values.yaml`: the extracted values (1 variable, 1 loop)" in readme
    assert "pip install jinja2 pyyaml" in readme
