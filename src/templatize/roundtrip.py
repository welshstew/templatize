"""Round-trip guarantee: template + extracted values must reproduce the input byte for byte."""

from __future__ import annotations

from typing import Any

import yaml
from pydantic import BaseModel

from templatize.emitters import Emitter, TemplateRenderError
from templatize.envfile import parse_env, serialize_env
from templatize.ir import TemplateIR
from templatize.valuesfile import parse_values, serialize_values

EXCERPT_RADIUS = 40


class RoundTripResult(BaseModel):
    ok: bool
    engine: str
    expected_bytes: int
    actual_bytes: int | None = None
    first_difference: int | None = None
    """Character offset (code points) of the first mismatch."""
    expected_excerpt: str | None = None
    actual_excerpt: str | None = None
    error: str | None = None


def _encode(s: str) -> bytes:
    return s.encode("utf-8", "surrogatepass")


def check_roundtrip(
    ir: TemplateIR, emitter: Emitter, template: str | None = None
) -> RoundTripResult:
    """Emit (unless ``template`` is given), serialise and re-parse the values (``.env``,
    or ``values.yaml`` when there are loops), render, compare.

    Going through the real serialiser/parser is deliberate: quoting bugs would
    otherwise slip past an in-memory comparison.
    """
    expected = _encode(ir.text)
    if template is None:
        template = emitter.emit(ir)
    values: dict[str, Any]
    try:
        if ir.loops:  # lists live in values.yaml
            values = parse_values(serialize_values(ir))
        else:
            values = {k.lower(): v for k, v in parse_env(serialize_env(ir)).items()}
    except (ValueError, yaml.YAMLError) as exc:
        return RoundTripResult(
            ok=False, engine=emitter.engine, expected_bytes=len(expected), error=str(exc)
        )
    try:
        rendered = emitter.render(template, values)
    except TemplateRenderError as exc:
        return RoundTripResult(
            ok=False, engine=emitter.engine, expected_bytes=len(expected), error=str(exc)
        )
    return compare(emitter.engine, ir.text, rendered)


def compare(engine: str, expected_text: str, rendered: str) -> RoundTripResult:
    """Byte-for-byte comparison, with the first difference and excerpts around it."""
    expected = _encode(expected_text)
    actual = _encode(rendered)
    if actual == expected:
        return RoundTripResult(
            ok=True, engine=engine, expected_bytes=len(expected), actual_bytes=len(actual)
        )
    idx = next(
        (i for i, (x, y) in enumerate(zip(expected_text, rendered, strict=False)) if x != y),
        min(len(expected_text), len(rendered)),
    )
    lo = max(0, idx - EXCERPT_RADIUS)
    return RoundTripResult(
        ok=False,
        engine=engine,
        expected_bytes=len(expected),
        actual_bytes=len(actual),
        first_difference=idx,
        expected_excerpt=expected_text[lo : idx + EXCERPT_RADIUS],
        actual_excerpt=rendered[lo : idx + EXCERPT_RADIUS],
        error="rendered output differs from the original input",
    )
