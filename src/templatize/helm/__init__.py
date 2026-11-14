"""Helm chart output for Kubernetes manifests.

Two styles: ``exact`` (your manifests as templates, byte-for-byte round trip) and
``scaffold`` (the ``helm create`` chart filled in from the manifests, checked field
by field with real Helm).
"""

from collections.abc import Sequence
from typing import Literal

from templatize.build import VariableSpec
from templatize.helm.chart import ChartError, ChartResult, ChartRoundTrip, build_chart

ChartStyle = Literal["exact", "scaffold"]


def build(
    style: ChartStyle,
    text: str,
    specs: Sequence[VariableSpec],
    variants: Sequence[str] = (),
    *,
    chart_name: str | None = None,
    variant_labels: Sequence[str] | None = None,
) -> ChartResult:
    """Build a chart in the given style."""
    if style == "scaffold":
        from templatize.helm.scaffold import build_scaffold_chart

        return build_scaffold_chart(
            text, specs, variants, chart_name=chart_name, variant_labels=variant_labels
        )
    return build_chart(text, specs, variants, chart_name=chart_name, variant_labels=variant_labels)


__all__ = ["ChartError", "ChartResult", "ChartRoundTrip", "ChartStyle", "build", "build_chart"]
