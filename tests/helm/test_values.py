"""values.yaml writer/reader: what is written must read back as the same text in Helm."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from templatize.helm import gotemplate, values


@pytest.mark.parametrize(
    ("raw", "written"),
    [
        ("3", "3"),
        ("-1", "-1"),
        ("999999", "999999"),
        ("1000000", '"1000000"'),  # Helm would print 1e+06
        ("0080", '"0080"'),
        ("+1", '"+1"'),
        ("1e3", '"1e3"'),
        ("1.0", '"1.0"'),
        ("true", "true"),
        ("True", '"True"'),
        ("on", '"on"'),
        ("~", '"~"'),
        ("null", '"null"'),
        ("1:30", '"1:30"'),
        ('a "q" \\ b', '"a \\"q\\" \\\\ b"'),
        ("tab\there", '"tab\\there"'),
        ("line\u2028sep", '"line\\u2028sep"'),
    ],
)
def test_yaml_scalar(raw: str, written: str) -> None:
    assert values.yaml_scalar(raw) == written


@given(st.text(max_size=40))
def test_any_value_reads_back_and_renders_as_itself(raw: str) -> None:
    tree = values.load(values.dump({"k": raw}))
    assert tree["k"] == values.typed(raw)
    assert gotemplate.format_value(tree["k"], "k") == raw


def test_tree_conflicts() -> None:
    with pytest.raises(values.ValuesError, match="both a value and a section"):
        values.build_tree([(["image"], "x"), (["image", "tag"], "1")])
    with pytest.raises(values.ValuesError, match="defined twice"):
        values.build_tree([(["a"], "x"), (["a"], "y")])


def test_dump_nested_and_special_keys() -> None:
    out = values.dump(values.build_tree([(["a", "b"], "1"), (["a", "on"], "x"), (["c"], "true")]))
    assert out == 'a:\n  b: 1\n  "on": "x"\nc: true\n'
    assert values.load(out) == {"a": {"b": 1, "on": "x"}, "c": True}


def test_load_rejects_aliases_and_non_mappings() -> None:
    with pytest.raises(values.ValuesError, match="aliases"):
        values.load("a: &x [1]\nb: *x\n")
    with pytest.raises(values.ValuesError, match="mapping"):
        values.load("- 1\n")
    with pytest.raises(values.ValuesError):
        values.load("a: [\n")
    assert values.load("") == {}


def test_merge_is_deep() -> None:
    base = {"image": {"repository": "r", "tag": "1"}, "n": 1}
    assert values.merge(base, {"image": {"tag": "2"}}) == {
        "image": {"repository": "r", "tag": "2"},
        "n": 1,
    }
