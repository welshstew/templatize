from __future__ import annotations

import itertools

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from templatize.build import VariableSpec, build, make_ir
from templatize.ir import (
    MAX_INPUT_CHARS,
    Candidate,
    IRValidationError,
    Source,
    Span,
    TemplateIR,
    Variable,
)
from templatize.merge import _Occupied, merge_candidates
from templatize.naming import dedupe_names, infer_type, snake_case
from templatize.suggest import InputLimitError, suggest


def cand(value: str, start: int, conf: float, name: str = "v", **kw: object) -> Candidate:
    return Candidate(
        name=name,
        value=value,
        spans=[Span(start=start, end=start + len(value))],
        sources=kw.pop("sources", [Source.REGEX]),  # type: ignore[arg-type]
        detectors=["t"],
        confidence=conf,
        **kw,  # type: ignore[arg-type]
    )


def test_merge_prefers_higher_scoring_overlap() -> None:
    url = cand("http://10.0.0.1/x", 0, 0.8, "url")
    ip = cand("10.0.0.1", 7, 0.9, "ip")
    merged = merge_candidates([url, ip])
    assert len(merged) == 1


def test_merge_length_breaks_near_ties() -> None:
    long = cand("abcdefghijklmnopqrstuvwxyz0123456789", 0, 0.7, "long")
    short = cand("abc", 0, 0.72, "short")
    (m,) = merge_candidates([long, short])
    assert m.name == "long"


def test_merge_unifies_same_value_and_sources() -> None:
    a = cand("db01.example.com", 0, 0.65, "hostname", name_origin="type")
    b = cand("db01.example.com", 0, 0.7, "db_host", name_origin="key", sources=[Source.KEY_VALUE])
    c = cand("db01.example.com", 40, 0.65, "hostname", name_origin="type")
    (m,) = merge_candidates([a, b, c])
    assert m.name == "db_host"
    assert len(m.spans) == 2
    assert set(m.sources) == {Source.REGEX, Source.KEY_VALUE}
    assert m.confidence == pytest.approx(0.75)


def test_merge_does_not_unify_trivial_values() -> None:
    a = cand("1", 0, 0.7, "replicas")
    b = cand("1", 10, 0.7, "version")
    merged = merge_candidates([a, b])
    assert [m.name for m in merged] == ["replicas", "version"]


def test_merge_dedupes_names() -> None:
    merged = merge_candidates([cand("aaaa", 0, 0.9, "x"), cand("bbbb", 10, 0.8, "x")])
    assert sorted(m.name for m in merged) == ["x", "x_2"]


def test_suggest_output_is_buildable() -> None:
    text = "host: a.example.com\nurl: http://a.example.com/x\nip 10.0.0.1 10.0.0.1\n"
    res = suggest(text)
    names = [c.name for c in res.candidates]
    assert len(names) == len(set(names))
    spans = sorted(s for c in res.candidates for s in ((x.start, x.end) for x in c.spans))
    assert all(a[1] <= b[0] for a, b in itertools.pairwise(spans))


# --- IR -----------------------------------------------------------------------


def test_ir_rejects_overlap() -> None:
    with pytest.raises(ValidationError, match="overlapping"):
        TemplateIR(
            text="abcdef",
            variables=[
                Variable(name="a", value="abc", spans=[Span(start=0, end=3)]),
                Variable(name="b", value="cde", spans=[Span(start=2, end=5)]),
            ],
        )


def test_ir_rejects_value_mismatch() -> None:
    with pytest.raises(ValidationError, match="expected"):
        TemplateIR(
            text="abc", variables=[Variable(name="a", value="x", spans=[Span(start=0, end=1)])]
        )


@pytest.mark.parametrize("name", ["Bad", "1x", "with-dash", "", "loop", "true", "class", "range"])
def test_ir_rejects_bad_names(name: str) -> None:
    with pytest.raises((ValidationError, IRValidationError)):
        make_ir("abc", [VariableSpec(name=name, spans=[Span(start=0, end=1)])])


def test_ir_rejects_duplicates_and_out_of_range() -> None:
    with pytest.raises(ValidationError, match="duplicate"):
        make_ir(
            "abcabc",
            [
                VariableSpec(name="a", spans=[Span(start=0, end=1)]),
                VariableSpec(name="a", spans=[Span(start=3, end=4)]),
            ],
        )
    with pytest.raises(IRValidationError, match="outside"):
        make_ir("abc", [VariableSpec(name="a", spans=[Span(start=2, end=9)])])


def test_spans_must_share_value() -> None:
    with pytest.raises(ValidationError, match="expected"):
        make_ir("ab", [VariableSpec(name="a", spans=[Span(start=0, end=1), Span(start=1, end=2)])])


def test_spec_value_checked() -> None:
    with pytest.raises(IRValidationError, match="does not match"):
        make_ir("abc", [VariableSpec(name="a", value="z", spans=[Span(start=0, end=1)])])


def test_empty_span_rejected() -> None:
    with pytest.raises(ValidationError):
        Span(start=3, end=3)


def test_input_limits() -> None:
    with pytest.raises(InputLimitError):
        suggest("x" * (MAX_INPUT_CHARS + 1))
    with pytest.raises(InputLimitError):
        suggest("x", ["a", "b", "c", "d"])


def test_build_result_ir_is_json_roundtrippable() -> None:
    result = build("hi Bob", [VariableSpec(name="who", spans=[Span(start=3, end=6)])])
    again = TemplateIR.model_validate_json(result.ir.model_dump_json())
    assert again == result.ir


# --- naming -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("serverName", "server_name"),
        ("Content-Type", "content_type"),
        ("db.host", "db_host"),
        ("HTTPServerURL", "http_server_url"),
        ("9lives", "v_9lives"),
        ("---", "value"),
        ("loop", "loop_value"),
        ("class", "class_value"),
    ],
)
def test_snake_case(raw: str, expected: str) -> None:
    assert snake_case(raw) == expected


def test_dedupe_names() -> None:
    assert dedupe_names(["a", "a", "b", "a"], taken=["b"]) == ["a", "a_2", "b_2", "a_3"]


@pytest.mark.parametrize(
    ("value", "vtype"),
    [
        ("42", "int"),
        ("3.14", "float"),
        ("true", "bool"),
        ("10.0.0.1", "ipv4"),
        ("::1", "ipv6"),
        ("10.0.0.0/24", "cidr"),
        ("a@b.co", "email"),
        ("https://x.y", "url"),
        ("2024-01-31", "date"),
        ("1.2.3", "semver"),
        ("/etc/x", "path"),
        ("db.example.com", "hostname"),
        ("hello", "string"),
    ],
)
def test_infer_type(value: str, vtype: str) -> None:
    assert infer_type(value).value == vtype


# --- performance-motivated structures keep the old behaviour ---------------------


@settings(max_examples=300, deadline=None)
@given(st.lists(st.tuples(st.integers(0, 60), st.integers(1, 8)), max_size=40))
def test_occupied_index_matches_linear_scan(raw: list[tuple[int, int]]) -> None:
    occupied, linear = _Occupied(), []
    for start, length in raw:
        s = Span(start=start, end=start + length)
        expected = any(s.overlaps(o) for o in linear)
        assert occupied.overlaps(s) == expected
        if not expected:
            occupied.add(s)
            linear.append(s)


def test_dedupe_names_suffixes() -> None:
    assert dedupe_names(["a", "a", "a_2", "a", "b"], taken=["a_3"]) == [
        "a",
        "a_2",
        "a_2_2",
        "a_4",
        "b",
    ]
    assert dedupe_names(["x"] * 5) == ["x", "x_2", "x_3", "x_4", "x_5"]
