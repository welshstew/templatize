"""Jinja2 emitter: escaping, whitespace and newline fidelity."""

from __future__ import annotations

import pytest

from templatize.build import VariableSpec, build, find_occurrences
from templatize.emitters import TemplateRenderError, get_emitter
from templatize.emitters.jinja2 import Jinja2Emitter, dominant_newline
from templatize.ir import Span


def var(name: str, text: str, value: str) -> VariableSpec:
    spans = find_occurrences(text, value, whole_word=False)
    assert spans, value
    return VariableSpec(name=name, spans=spans)


def roundtrip(text: str, *specs: VariableSpec) -> str:
    result = build(text, list(specs))
    assert result.roundtrip.ok, result.roundtrip
    return result.template.content


@pytest.mark.parametrize(
    "text",
    [
        "{{ x }}",
        "{% if a %}b{% endif %}",
        "{# comment #}",
        "{% raw %}{{ x }}{% endraw %}",
        "literal {% endraw %} inside",
        "{{{ triple }}}",
        "{{{{",
        "{%- trim -%} {{- x -}} {#- c -#}",
        "{",
        "}}",
        "{{",
        "a { b } c {{ d",
        "{{ '\\n' }}",
        "%} #} }}",
    ],
)
def test_existing_jinja_syntax_is_escaped(text: str) -> None:
    roundtrip(text)
    roundtrip(text + "\n")


def test_escapes_use_string_expressions_not_raw_blocks() -> None:
    tpl = roundtrip("a {{ b }} c")
    assert "{% raw %}" not in tpl
    assert tpl == 'a {{ "{{" }} b }} c'


def test_brace_before_variable_is_escaped() -> None:
    text = "x{10.0.0.1}"
    tpl = roundtrip(text, VariableSpec(name="ip", spans=[Span(start=2, end=10)]))
    assert tpl == 'x{{ "{" }}{{ ip }}}'


def test_variable_followed_by_braces() -> None:
    text = "v}}}"
    roundtrip(text, VariableSpec(name="v", spans=[Span(start=0, end=1)]))


def test_values_containing_template_syntax_render_literally() -> None:
    text = "a={{ x }} b={% y %}"
    roundtrip(
        text,
        VariableSpec(name="first", spans=[Span(start=2, end=9)]),
        VariableSpec(name="second", spans=[Span(start=12, end=19)]),
    )


@pytest.mark.parametrize(
    "text",
    [
        "",
        "\n",
        "\n\n\n",
        "no newline at end",
        "trailing\n",
        "two trailing\n\n",
        "crlf\r\nfile\r\n",
        "cr\rfile\r",
        "mixed\r\nends\nhere\rand\r\n\n",
        "  indented\n\tTabbed  \n   ",
        "trailing spaces   \n   ",
        "﻿bom start\n",
        "unicode ☃ 日本 🎉\n",
    ],
)
def test_whitespace_and_newlines_preserved(text: str) -> None:
    roundtrip(text)


def test_mixed_newlines_use_dominant_and_escape_others() -> None:
    text = "a\r\nb\r\nc\nd\r\n"
    em = Jinja2Emitter()
    tpl = roundtrip(text)
    assert tpl == 'a\r\nb\r\nc{{ "\\n" }}d\r\n'
    result = build(text, [])
    assert em.newline_for(result.ir) == "\r\n"
    assert (
        'NEWLINE = "\\r\\n"' in result.runner[0].content
        or "NEWLINE = '\\r\\n'" in result.runner[0].content
    )


def test_dominant_newline_tie_break() -> None:
    assert dominant_newline(["a\nb\r\n"]) == "\n"
    assert dominant_newline(["none"]) == "\n"
    # one lone CR and one CRLF: tie between them, CRLF ranks before CR
    assert dominant_newline(["\r\r\n"]) == "\r\n"


def test_variable_values_with_newlines_and_quotes() -> None:
    text = 'msg="line1\r\nline2\t\\ "quoted" ${HOME} # not a comment"'
    roundtrip(text, VariableSpec(name="msg", spans=[Span(start=5, end=len(text) - 1)]))


def test_variable_spanning_newline_boundaries() -> None:
    text = "a\r\nb\r\nc"
    # variable covers the "\r" of the first CRLF only
    roundtrip(text, VariableSpec(name="v", spans=[Span(start=1, end=2)]))
    roundtrip(text, VariableSpec(name="v", spans=[Span(start=2, end=4)]))


def test_render_reports_errors() -> None:
    em = get_emitter("jinja2")
    with pytest.raises(TemplateRenderError, match="undefined"):
        em.render("{{ missing }}", {})
    with pytest.raises(TemplateRenderError):
        em.render("{% if %}", {})


def test_render_is_sandboxed() -> None:
    em = get_emitter("jinja2")
    with pytest.raises(TemplateRenderError, match=r"SecurityError|unsafe"):
        em.render("{{ x.__class__.__mro__[1].__subclasses__() }}", {"x": "a"})


def test_unknown_engine() -> None:
    with pytest.raises(ValueError, match="unknown engine"):
        get_emitter("handlebars")
