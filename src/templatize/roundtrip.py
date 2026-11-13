"""Round-trip guarantee: template + extracted values must reproduce the input byte for byte."""

from __future__ import annotations

from pydantic import BaseModel

from templatize.emitters import Emitter, TemplateRenderError
from templatize.envfile import parse_env, serialize_env
from templatize.ir import TemplateIR

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
    """Emit (unless ``template`` is given), serialise and re-parse the ``.env``, render, compare.

    Going through the real ``.env`` serialiser/parser is deliberate: quoting bugs
    would otherwise slip past an in-memory comparison.
    """
    expected = _encode(ir.text)
    if template is None:
        template = emitter.emit(ir)
    env_values = parse_env(serialize_env(ir))
    values = {k.lower(): v for k, v in env_values.items()}
    try:
        rendered = emitter.render(template, values)
    except TemplateRenderError as exc:
        return RoundTripResult(
            ok=False, engine=emitter.engine, expected_bytes=len(expected), error=str(exc)
        )
    actual = _encode(rendered)
    if actual == expected:
        return RoundTripResult(
            ok=True, engine=emitter.engine, expected_bytes=len(expected), actual_bytes=len(actual)
        )
    idx = next(
        (i for i, (x, y) in enumerate(zip(ir.text, rendered, strict=False)) if x != y),
        min(len(ir.text), len(rendered)),
    )
    lo = max(0, idx - EXCERPT_RADIUS)
    return RoundTripResult(
        ok=False,
        engine=emitter.engine,
        expected_bytes=len(expected),
        actual_bytes=len(actual),
        first_difference=idx,
        expected_excerpt=ir.text[lo : idx + EXCERPT_RADIUS],
        actual_excerpt=rendered[lo : idx + EXCERPT_RADIUS],
        error="rendered output differs from the original input",
    )
