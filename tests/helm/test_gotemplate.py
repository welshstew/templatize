"""Go template emitting/escaping and the subset renderer."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from templatize.emitters.base import TemplateRenderError
from templatize.helm import gotemplate
from templatize.ir import Span


def test_emit_and_render() -> None:
    text = "image: repo:1.2\n"
    tpl = gotemplate.emit(
        text,
        [
            (Span(start=7, end=11), ["image", "repository"]),
            (Span(start=12, end=15), ["image", "tag"]),
        ],
    )
    assert tpl == "image: {{ .Values.image.repository }}:{{ .Values.image.tag }}\n"
    assert gotemplate.render(tpl, {"image": {"repository": "repo", "tag": "1.2"}}) == text


def test_literal_braces_are_escaped() -> None:
    text = "a: '{{ x }}' {{{ b"
    tpl = gotemplate.emit(text, [])
    assert "{{ x" not in tpl.replace('{{ "{{" }}', "")
    assert gotemplate.render(tpl, {}) == text


def test_brace_before_action() -> None:
    text = "x: {v}"
    tpl = gotemplate.emit(text, [(Span(start=4, end=5), ["v"])])
    assert tpl == 'x: {{ "{" }}{{ .Values.v }}}'
    assert gotemplate.render(tpl, {"v": "v"}) == text


@given(
    st.text(alphabet=st.sampled_from(list("{}ab \n\r\t%#-\"'")), max_size=60),
    st.data(),
)
def test_roundtrip_any_text(text: str, data: st.DataObject) -> None:
    occ: list[tuple[Span, list[str]]] = []
    pos = 0
    vals: dict[str, str] = {}
    while pos < len(text):
        start = data.draw(st.integers(pos, len(text)))
        if start >= len(text):
            break
        end = data.draw(st.integers(start + 1, len(text)))
        key = f"v{len(occ)}"
        occ.append((Span(start=start, end=end), [key]))
        vals[key] = text[start:end]
        pos = end
        if data.draw(st.booleans()):
            break
    tpl = gotemplate.emit(text, occ)
    assert gotemplate.render(tpl, vals) == text


def test_render_errors() -> None:
    with pytest.raises(TemplateRenderError, match=r"missing value \.Values\.a\.b"):
        gotemplate.render("{{ .Values.a.b }}", {"a": {}})
    with pytest.raises(TemplateRenderError, match="unsupported action"):
        gotemplate.render('{{ include "x" . }}', {})
    with pytest.raises(TemplateRenderError, match="trimming"):
        gotemplate.render("{{- .Values.a }}", {"a": "x"})
    with pytest.raises(TemplateRenderError, match="unclosed"):
        gotemplate.render("{{ .Values.a", {"a": "x"})
    with pytest.raises(TemplateRenderError, match="float"):
        gotemplate.render("{{ .Values.n }}", {"n": 10**7})
    with pytest.raises(TemplateRenderError, match="list"):
        gotemplate.render("{{ .Values.l }}", {"l": [1]})


def test_invalid_path() -> None:
    with pytest.raises(ValueError):
        gotemplate.values_ref(["a-b"])
