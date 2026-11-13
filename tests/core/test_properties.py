"""Property tests: arbitrary text with arbitrary accepted spans must always round-trip."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from templatize.build import VariableSpec, build, find_occurrences
from templatize.envfile import parse_env, quote
from templatize.ir import Span

# Bias towards characters that stress escaping, quoting and newline handling.
TRICKY = [
    *"{}%#-\r\n\t \"'\\$=:`",
    *["{{", "{%", "{#", "}}", "%}", "#}", "\r\n", "${X}", "\u00e9", "\U0001f389"],
    # separators/controls that some newline-splitting code treats as line breaks
    *["\u2028", "\x0b", "\x0c", "\x85", "\x00", "\ufeff"],
]
text_st = st.lists(
    st.one_of(st.sampled_from(TRICKY), st.text(min_size=1, max_size=4)), max_size=40
).map("".join)


@settings(max_examples=400, deadline=None)
@given(text=text_st, data=st.data())
def test_random_spans_roundtrip(text: str, data: st.DataObject) -> None:
    specs: list[VariableSpec] = []
    if text:
        cuts = sorted(set(data.draw(st.lists(st.integers(0, len(text)), max_size=8), label="cuts")))
        # pair consecutive cut points into non-overlapping, non-empty spans
        for i, (a, b) in enumerate(zip(cuts[::2], cuts[1::2], strict=False)):
            if b > a:
                specs.append(VariableSpec(name=f"v{i}", spans=[Span(start=a, end=b)]))
    result = build(text, specs)
    assert result.roundtrip.ok, (repr(text), result.roundtrip)


@settings(max_examples=200, deadline=None)
@given(text=text_st, value=st.sampled_from(["{{", "\r\n", "x", "{", "%", "é", "\n"]))
def test_all_occurrences_of_a_value_roundtrip(text: str, value: str) -> None:
    spans = find_occurrences(text, value, whole_word=False)
    specs = [VariableSpec(name="v", spans=spans)] if spans else []
    assert build(text, specs).roundtrip.ok


@settings(max_examples=500, deadline=None)
@given(value=text_st)
def test_env_quoting_roundtrips(value: str) -> None:
    content = f"KEY={quote(value)}\nOTHER={quote('x')}\n"
    assert parse_env(content) == {"KEY": value, "OTHER": "x"}


config_line = st.sampled_from(
    [
        "host: db01.example.com",
        "ip=10.0.0.{n}",
        '"key": "va{{lue"',
        "listen 80{n};",
        "url: https://a.example.io/p{n}",
        "date 2024-01-0{n}",
        "{{ literal }}",
        "user{n}@example.com",
    ]
)
doc_st = st.lists(
    st.tuples(config_line, st.integers(1, 9), st.sampled_from(["\n", "\r\n", "\r", " ", ""])),
    max_size=12,
).map(lambda rows: "".join(line.replace("{n}", str(n)) + sep for line, n, sep in rows))


@settings(max_examples=150, deadline=None)
@given(text=doc_st, variant=doc_st)
def test_suggest_accept_all_roundtrips(text: str, variant: str) -> None:
    from templatize.build import specs_from_candidates
    from templatize.suggest import suggest

    res = suggest(text, [variant])
    assert build(text, specs_from_candidates(res.candidates)).roundtrip.ok
