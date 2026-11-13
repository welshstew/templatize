from __future__ import annotations

import pytest

from templatize.build import VariableInput, VariableResolutionError, build, resolve_inputs
from templatize.ir import Span
from templatize.validate import validate_template

TEXT = "host: db01.example.com\nbackup: db01.example.com\nport: 5432\nid: 54321\n"


def test_resolve_by_value_all_occurrences() -> None:
    (spec,) = resolve_inputs(TEXT, [VariableInput(name="host", value="db01.example.com")])
    assert [TEXT[s.start : s.end] for s in spec.spans] == ["db01.example.com"] * 2


def test_resolve_selected_occurrences() -> None:
    (spec,) = resolve_inputs(
        TEXT, [VariableInput(name="h", value="db01.example.com", occurrences=[1])]
    )
    assert spec.spans == [Span(start=TEXT.rindex("db01"), end=TEXT.rindex("db01") + 16)]


def test_resolve_whole_word_then_substring_fallback() -> None:
    (port,) = resolve_inputs(TEXT, [VariableInput(name="port", value="5432")])
    assert len(port.spans) == 1  # not inside 54321
    (frag,) = resolve_inputs(TEXT, [VariableInput(name="frag", value="example")])
    assert len(frag.spans) == 2  # "." is a word boundary, so it matches inside both hostnames
    (sub,) = resolve_inputs(TEXT, [VariableInput(name="sub", value="xample")])
    assert len(sub.spans) == 2  # never a whole word: falls back to substring matching


def test_resolve_skips_occurrences_claimed_earlier() -> None:
    text = "api.example.com and example.com\n"
    host, domain = resolve_inputs(
        text,
        [
            VariableInput(name="host", value="api.example.com"),
            VariableInput(name="domain", value="example.com"),
        ],
    )
    assert [text[s.start : s.end] for s in host.spans] == ["api.example.com"]
    # the occurrence inside api.example.com is already taken; only the standalone one is left
    assert domain.spans == [Span(start=20, end=31)]
    assert build(text, [host, domain]).roundtrip.ok


def test_resolve_errors() -> None:
    with pytest.raises(VariableResolutionError, match="does not occur"):
        resolve_inputs(TEXT, [VariableInput(name="x", value="nope")])
    with pytest.raises(VariableResolutionError, match="out of range"):
        resolve_inputs(TEXT, [VariableInput(name="x", value="5432", occurrences=[3])])
    with pytest.raises(VariableResolutionError, match="exactly one"):
        resolve_inputs(TEXT, [VariableInput(name="x")])
    with pytest.raises(VariableResolutionError, match="overlaps"):
        resolve_inputs(
            TEXT,
            [
                VariableInput(name="host", value="db01.example.com"),
                VariableInput(name="d", value="example.com"),
            ],
        )


def test_resolved_inputs_build_and_roundtrip() -> None:
    specs = resolve_inputs(
        TEXT,
        [
            VariableInput(name="host", value="db01.example.com"),
            VariableInput(name="port", value="5432"),
        ],
    )
    result = build(TEXT, specs)
    assert result.roundtrip.ok
    assert result.template.content.count("{{ host }}") == 2


def test_validate_template_reports() -> None:
    ok = validate_template("a {{ x }} b\n", {"x": "1"}, expected="a 1 b\n")
    assert ok.ok and ok.matches_expected and ok.variables == ["x"]

    mismatch = validate_template("a {{ x }} b\n", {"X": "2"}, expected="a 1 b\n")
    assert not mismatch.ok and mismatch.matches_expected is False and mismatch.first_difference == 2

    missing = validate_template("{{ x }}{{ y }}", {"x": "1", "z": "0"})
    assert missing.missing_values == ["y"] and missing.unused_values == ["z"]
    assert not missing.rendered and not missing.ok

    syntax = validate_template("{% if %}", {})
    assert not syntax.valid_syntax and syntax.error and not syntax.ok

    no_expected = validate_template("plain", {})
    assert no_expected.ok and no_expected.matches_expected is None
