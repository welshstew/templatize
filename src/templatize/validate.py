"""Validate a template: syntax, referenced variables, rendering, and (optionally) that
it reproduces an expected document byte for byte."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

from templatize.emitters import TemplateRenderError, get_emitter

EXCERPT_RADIUS = 40


class ValidationReport(BaseModel):
    valid_syntax: bool
    variables: list[str]
    """Variables the template references."""
    missing_values: list[str]
    unused_values: list[str]
    rendered: bool
    matches_expected: bool | None = None
    """None when no expected text was given."""
    first_difference: int | None = None
    expected_excerpt: str | None = None
    actual_excerpt: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return (
            self.valid_syntax
            and self.rendered
            and not self.missing_values
            and self.matches_expected is not False
        )


def validate_template(
    template: str,
    values: Mapping[str, Any],
    expected: str | None = None,
    engine: str = "jinja2",
) -> ValidationReport:
    emitter = get_emitter(engine)
    values = {k.lower(): v for k, v in values.items()}
    try:
        referenced = sorted(emitter.variables_in(template))
    except TemplateRenderError as exc:
        return ValidationReport(
            valid_syntax=False,
            variables=[],
            missing_values=[],
            unused_values=sorted(values),
            rendered=False,
            error=str(exc),
        )
    missing = [v for v in referenced if v not in values]
    unused = sorted(set(values) - set(referenced))
    try:
        output = emitter.render(template, values)
    except TemplateRenderError as exc:
        return ValidationReport(
            valid_syntax=True,
            variables=referenced,
            missing_values=missing,
            unused_values=unused,
            rendered=False,
            error=str(exc),
        )
    report = ValidationReport(
        valid_syntax=True,
        variables=referenced,
        missing_values=missing,
        unused_values=unused,
        rendered=True,
    )
    if expected is not None:
        if output.encode("utf-8", "surrogatepass") == expected.encode("utf-8", "surrogatepass"):
            report.matches_expected = True
        else:
            idx = next(
                (i for i, (a, b) in enumerate(zip(expected, output, strict=False)) if a != b),
                min(len(expected), len(output)),
            )
            lo = max(0, idx - EXCERPT_RADIUS)
            report.matches_expected = False
            report.first_difference = idx
            report.expected_excerpt = expected[lo : idx + EXCERPT_RADIUS]
            report.actual_excerpt = output[lo : idx + EXCERPT_RADIUS]
    return report
