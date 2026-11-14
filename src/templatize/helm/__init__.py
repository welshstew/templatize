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
    exact style; the helm create style keeps their text as it is. A Docker Compose
    file is first converted to manifests with Kompose (see ``templatize.compose``)."""
    from templatize.compose import compose_services

    if compose_services(text) is not None:
        return _from_compose(style, text, specs, variants, chart_name, variant_labels)
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


def _from_compose(
    style: ChartStyle,
    text: str,
    specs: Sequence[VariableSpec],
    variants: Sequence[str],
    chart_name: str | None,
    variant_labels: Sequence[str] | None,
) -> ChartResult:
    """Compose -> Kompose manifests -> chart. Variables chosen on the compose file are
    carried over by value (their occurrences in the manifests); with none, the
    manifests' own suggestions are accepted."""
    from templatize.build import (
        VariableInput,
        VariableResolutionError,
        accept_suggestions,
        resolve_inputs,
    )
    from templatize.compose import ComposeError, KomposeUnavailable, compose_services, convert
    from templatize.suggest import suggest

    try:
        main = convert(text)
        others = [convert(v) for v in variants]
    except KomposeUnavailable as exc:
        raise ChartError(
            f"{exc}: converting a Docker Compose file to a chart needs kompose on the server"
        ) from None
    except ComposeError as exc:
        raise ChartError(str(exc)) from None
    manifests = main.manifests
    examples = [o.manifests for o in others]
    notes = list(main.notes)
    if specs:
        found: list[VariableSpec] = []
        missing: list[str] = []
        for spec in specs:
            value = text[spec.spans[0].start : spec.spans[0].end]
            try:
                (resolved,) = resolve_inputs(
                    manifests, [VariableInput(name=spec.name, value=value)]
                )
            except VariableResolutionError:
                missing.append(spec.name)
                continue
            # Occurrences already taken by an earlier (longer) variable are skipped.
            free = [
                s for s in resolved.spans if not any(s.overlaps(o) for f in found for o in f.spans)
            ]
            if free:
                found.append(
                    resolved.model_copy(
                        update={"spans": free, "variant_values": spec.variant_values}
                    )
                )
        if missing:
            notes.append(
                "not in the Kubernetes manifests, so not in the chart: " + ", ".join(missing)
            )
        chart_specs: list[VariableSpec] = found
        chart_loops: list[LoopSpec] = []
    else:
        res = suggest(manifests, examples)
        chart_specs, chart_loops = accept_suggestions(res.candidates, res.loops)
    services = compose_services(text) or {}
    published = [n for n, s in services.items() if s.get("ports")]
    name = chart_name or _chart_name((published or list(services))[0])
    if style == "scaffold":
        from templatize.helm.scaffold import build_scaffold_chart

        result = build_scaffold_chart(
            manifests, chart_specs, examples, chart_name=name, variant_labels=variant_labels
        )
    else:
        result = build_chart(
            manifests,
            chart_specs,
            examples,
            chart_name=name,
            variant_labels=variant_labels,
            loops=chart_loops,
        )
    result.notes = [*notes, *result.notes]
    result.converted_from = "compose"
    result.documents = [manifests, *examples]
    return result


def _chart_name(service: str) -> str:
    import re

    from templatize.helm.chart import CHART_NAME_RE

    name = re.sub(r"[^a-z0-9-]+", "-", service.lower()).strip("-")[:53].strip("-")
    return name if CHART_NAME_RE.match(name) else "chart"


__all__ = ["ChartError", "ChartResult", "ChartRoundTrip", "ChartStyle", "build", "build_chart"]
