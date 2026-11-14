"""Helm chart output for Kubernetes manifests.

Two styles: ``exact`` (your manifests as templates, byte-for-byte round trip) and
``scaffold`` (the ``helm create`` chart filled in from the manifests, checked field
by field with real Helm).
"""

from collections.abc import Sequence
from typing import Literal

from templatize.build import LoopSpec, VariableSpec
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
    loops: Sequence[LoopSpec] = (),
) -> ChartResult:
    """Build a chart in the given style. Loops become ``range`` over values lists in the
    exact style; the helm create style keeps their text as it is."""
    if style == "scaffold":
        from templatize.helm.scaffold import build_scaffold_chart

        result = build_scaffold_chart(
            text, specs, variants, chart_name=chart_name, variant_labels=variant_labels
        )
        if loops:
            names = ", ".join(lp.name for lp in loops)
            result.notes.append(
                f"lists ({names}) are not loops in the helm create style: their text is kept "
                "as it is (use the exact style for range loops)"
            )
        return result
    return build_chart(
        text, specs, variants, chart_name=chart_name, variant_labels=variant_labels, loops=loops
    )


__all__ = ["ChartError", "ChartResult", "ChartRoundTrip", "ChartStyle", "build", "build_chart"]
