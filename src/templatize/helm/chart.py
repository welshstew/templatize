"""Build a Helm chart from Kubernetes manifests and accepted variables.

Layout::

    <chart>/
      Chart.yaml
      values.yaml               # every variable, nested by its values path
      values-<label>.yaml       # per example document: only the keys that differ
      .helmignore
      templates/<kind>.yaml     # one per resource, in input order

Round trip: rendering every template with ``values.yaml`` and concatenating them
must reproduce the input byte for byte. For each example, rendering with
``values.yaml`` merged with ``values-<label>.yaml`` must reproduce that example.
Rendering uses what the values file *reads back as*, so a quoting bug in the values
writer fails the check instead of slipping through.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Literal, Protocol

from pydantic import BaseModel

from templatize import typedvalues
from templatize.build import LoopSpec, VariableSpec, make_ir
from templatize.detectors.k8s import camel
from templatize.emitters.base import EmittedFile, TemplateRenderError
from templatize.helm import gotemplate, values
from templatize.helm.coverage import CoverageReport
from templatize.ir import Loop, Span, TemplateIR, ValuesPath, Variable
from templatize.k8s import Resource, parse_resources
from templatize.roundtrip import EXCERPT_RADIUS
from templatize.suggest import check_limits

CHART_NAME_RE = re.compile(r"^[a-z0-9](?:[-a-z0-9]{0,51}[a-z0-9])?$")
LABEL_RE = re.compile(r"^[a-z0-9](?:[-a-z0-9]{0,30}[a-z0-9])?$")
_NON_NAME = re.compile(r"[^a-z0-9-]+")

HELMIGNORE = """\
# Patterns to ignore when building packages.
.DS_Store
.git/
.gitignore
.vscode/
.idea/
*.swp
*.bak
*.tmp
*~
"""


class ChartError(ValueError):
    pass


class ChartTemplate(BaseModel):
    path: str
    kind: str
    name: str
    span: Span
    """The part of the input this template reproduces."""


class ChartRoundTrip(BaseModel):
    label: str
    """``values.yaml`` or the example's values file."""
    document: str
    """``input`` or ``example N``."""
    ok: bool
    expected_bytes: int
    actual_bytes: int | None = None
    first_difference: int | None = None
    expected_excerpt: str | None = None
    actual_excerpt: str | None = None
    error: str | None = None


class FilledValue(BaseModel):
    """A ``values.yaml`` entry templatize set from the manifests (scaffold style)."""

    path: list[str]
    source: str
    """Where the value came from, e.g. ``Deployment storefront``."""


class ChartResult(BaseModel):
    chart_name: str
    files: list[EmittedFile]
    """Paths are relative to the chart directory."""
    templates: list[ChartTemplate]
    variables: list[Variable]
    """With final (unique) ``values_path`` filled in."""
    roundtrips: list[ChartRoundTrip]
    """Exact style: byte-for-byte check per document. Empty for the scaffold style."""
    style: Literal["exact", "scaffold"] = "exact"
    coverage: list[CoverageReport] = []
    """Scaffold style: field-by-field comparison of real ``helm template`` output."""
    notes: list[str] = []
    """Things the user should know (e.g. what the scaffold cannot express)."""
    loops: list[Loop] = []
    """Exact style: lists rendered with ``range``, with their final ``values_path``."""
    converted_from: Literal["compose"] | None = None
    """The input was a Docker Compose file, converted to manifests with Kompose."""
    documents: list[str] = []
    """When converted: the manifests the chart reproduces (input, then examples)."""
    filled: list[FilledValue] = []
    """Scaffold style: values that differ from the ``helm create`` defaults."""

    @property
    def ok(self) -> bool:
        if self.style == "scaffold":  # "not checked" (no helm) is not a failure
            return all(c.error is None or not c.available for c in self.coverage)
        return all(r.ok for r in self.roundtrips)

    def file(self, path: str) -> EmittedFile:
        return next(f for f in self.files if f.path == path)


def default_chart_name(resources: Sequence[Resource]) -> str:
    workload = next(
        (r for r in resources if r.kind in ("Deployment", "StatefulSet", "DaemonSet", "CronJob")),
        resources[0] if resources else None,
    )
    raw = workload.name.lower() if workload else "chart"
    name = _NON_NAME.sub("-", raw).strip("-")[:53].strip("-")
    return name if CHART_NAME_RE.match(name) else "chart"


def _template_names(resources: Sequence[Resource]) -> list[str]:
    kinds = [r.kind.lower() for r in resources]
    names: list[str] = []
    for r, kind in zip(resources, kinds, strict=True):
        base = kind if kinds.count(kind) == 1 else f"{kind}-{r.name.lower()}"
        base = _NON_NAME.sub("-", base).strip("-") or "resource"
        candidate, n = base, 2
        while candidate in names:
            candidate, n = f"{base}-{n}", n + 1
        names.append(candidate)
    return [f"templates/{n}.yaml" for n in names]


class _Named(Protocol):
    @property
    def name(self) -> str: ...
    @property
    def values_path(self) -> ValuesPath | None: ...


def assign_paths(variables: Sequence[_Named], reserved: Iterable[str] = ()) -> list[list[str]]:
    """Final values path per variable: its own, or derived from its name; made unique
    and prefix-free (``image`` and ``image.tag`` cannot both be values). ``reserved``
    top-level keys are already in use (the scaffold's own values)."""
    taken: list[list[str]] = [[k] for k in reserved]
    first = len(taken)

    def clash(p: list[str]) -> list[str] | None:
        return next((t for t in taken if t[: len(p)] == p or p[: len(t)] == t), None)

    for var in variables:
        path = list(var.values_path) if var.values_path else [camel(var.name)]
        candidate, n = path, 2
        while (other := clash(candidate)) is not None:
            if n > len(variables) + len(taken) + 2:  # cannot happen: each suffix is new
                raise ChartError(f"cannot find a free values path for {var.name!r}")
            # Rename at the deepest segment the two paths share, so "image.tag" next to
            # a value "image" becomes "image2.tag", not the still-clashing "image.tag2".
            i = min(len(other), len(path)) - 1
            candidate, n = [*path[:i], f"{path[i]}{n}", *path[i + 1 :]], n + 1
        taken.append(candidate)
    return taken[first:]


def _compare(label: str, document: str, expected: str, rendered: str) -> ChartRoundTrip:
    exp_b, act_b = (
        expected.encode("utf-8", "surrogatepass"),
        rendered.encode("utf-8", "surrogatepass"),
    )
    if exp_b == act_b:
        return ChartRoundTrip(
            label=label,
            document=document,
            ok=True,
            expected_bytes=len(exp_b),
            actual_bytes=len(act_b),
        )
    idx = next(
        (i for i, (x, y) in enumerate(zip(expected, rendered, strict=False)) if x != y),
        min(len(expected), len(rendered)),
    )
    lo = max(0, idx - EXCERPT_RADIUS)
    return ChartRoundTrip(
        label=label,
        document=document,
        ok=False,
        expected_bytes=len(exp_b),
        actual_bytes=len(act_b),
        first_difference=idx,
        expected_excerpt=expected[lo : idx + EXCERPT_RADIUS],
        actual_excerpt=rendered[lo : idx + EXCERPT_RADIUS],
        error="rendered chart differs from the "
        + ("input" if document == "input" else document)
        + (
            ""
            if document == "input"
            else ": it differs outside the accepted variables, or a variable is missing"
        ),
    )


def render_templates(templates: Sequence[EmittedFile], tree: values.Tree) -> list[EmittedFile]:
    return [EmittedFile(path=t.path, content=gotemplate.render(t.content, tree)) for t in templates]


def _check(
    label: str, document: str, expected: str, templates: Sequence[EmittedFile], *texts: str
) -> ChartRoundTrip:
    try:
        tree: values.Tree = {}
        for i, t in enumerate(texts):
            tree = values.merge(tree, values.load(t, label if i else "values.yaml"))
        rendered = "".join(f.content for f in render_templates(templates, tree))
    except (TemplateRenderError, values.ValuesError) as exc:
        return ChartRoundTrip(
            label=label,
            document=document,
            ok=False,
            expected_bytes=len(expected.encode("utf-8", "surrogatepass")),
            error=str(exc),
        )
    return _compare(label, document, expected, rendered)


def build_chart(
    text: str,
    specs: Sequence[VariableSpec],
    variants: Sequence[str] = (),
    *,
    chart_name: str | None = None,
    variant_labels: Sequence[str] | None = None,
    loops: Sequence[LoopSpec] = (),
) -> ChartResult:
    check_limits(text, variants)
    resources = parse_resources(text)
    if not resources:
        raise ChartError(
            "a Helm chart needs Kubernetes manifests: YAML documents with apiVersion, kind "
            "and metadata.name (each resource in its own --- document)"
        )
    name = chart_name or default_chart_name(resources)
    if not CHART_NAME_RE.match(name):
        raise ChartError(
            f"invalid chart name {name!r}: lowercase letters, digits and '-', at most 53 characters"
        )
    labels = list(variant_labels or [f"example-{i + 2}" for i in range(len(variants))])
    if len(labels) != len(variants):
        raise ChartError("give one label per example document")
    for label in labels:
        if not LABEL_RE.match(label):
            raise ChartError(f"invalid example label {label!r}: lowercase letters, digits and '-'")
    if len(set(labels)) != len(labels):
        raise ChartError("example labels must be unique")

    ir: TemplateIR = make_ir(text, specs, loops)
    top = [lp for lp in ir.loops if lp.parent is None]  # nested lists live in their rows
    paths = assign_paths([*ir.variables, *top])
    variables = [
        v.model_copy(update={"values_path": p})
        for v, p in zip(ir.variables, paths[: len(ir.variables)], strict=True)
    ]
    chart_loops = [
        lp.model_copy(update={"values_path": p})
        for lp, p in zip(top, paths[len(ir.variables) :], strict=True)
    ]
    path_of = {v.name: v.values_path or [] for v in variables}

    # Templates: one per resource chunk. Variable spans inside loop items are emitted
    # with the loop (as $.Values references).
    regions = [lp.region for lp in chart_loops]
    occurrences = sorted(
        (
            (s, v.values_path or [])
            for v in variables
            for s in v.spans
            if not any(r.contains(s) for r in regions)
        ),
        key=lambda o: o[0].start,
    )
    templates: list[EmittedFile] = []
    infos: list[ChartTemplate] = []
    for res, path in zip(resources, _template_names(resources), strict=True):
        lo, hi = res.chunk.start, res.chunk.end
        mine: list[tuple[Span, list[str]]] = []
        for span, vpath in occurrences:
            if span.end <= lo or span.start >= hi:
                continue
            if span.start < lo or span.end > hi:
                raise ChartError(
                    f"variable {'.'.join(vpath)!r} at {span.start}-{span.end} crosses the "
                    f"boundary between two resources"
                )
            mine.append((Span(start=span.start - lo, end=span.end - lo), vpath))
        ranges = []
        for lp in chart_loops:
            r = lp.region
            if r.end <= lo or r.start >= hi:
                continue
            if r.start < lo or r.end > hi:
                raise ChartError(f"the list {lp.name!r} crosses the boundary between two resources")
            ranges.append(_range_emit(ir, lp, variables, path_of, lo))
        try:
            content = gotemplate.emit(text[lo:hi], mine, ranges)
        except ValueError as exc:
            raise ChartError(str(exc)) from None
        templates.append(EmittedFile(path=path, content=content))
        infos.append(ChartTemplate(path=path, kind=res.kind, name=res.name, span=res.chunk))

    try:
        main_tree = values.build_tree(
            [
                *((v.values_path or [], v.value) for v in variables),
                # Maps too are lists of {key, value} rows: Go ranges maps in sorted
                # key order, which would reorder the output.
                *((lp.values_path or [], ir.loop_rows(lp)) for lp in chart_loops),
            ]
        )
    except values.ValuesError as exc:
        raise ChartError(str(exc)) from None
    values_yaml = values.dump(
        main_tree,
        header=f"Default values for {name}.\nGenerated by templatize from the input manifests.",
    )

    # Per-example overlays: only the variables whose value differs.
    by_name = {s.name: s for s in specs}
    overlays: list[tuple[str, str]] = []
    for i, label in enumerate(labels):
        diff: list[tuple[list[str], object]] = []
        for v in variables:
            vv = by_name[v.name].variant_values
            if vv is not None and i < len(vv) and vv[i] != v.value:
                diff.append((v.values_path or [], vv[i]))
        for lp, spec in zip(chart_loops, loops, strict=True):
            lv = spec.variant_values
            if lv is not None and i < len(lv) and lv[i] is not None:
                rows = _as_rows(lp, lv[i])
                if rows != ir.loop_rows(lp):
                    diff.append((lp.values_path or [], rows))
        overlays.append(
            (
                f"values-{label}.yaml",
                values.dump(
                    values.build_tree(diff),
                    header=f"Values for {label} (example {i + 2}): only what differs from values.yaml.",
                ),
            )
        )

    hints = typedvalues.variable_hints(
        variables,
        chart_loops,
        ir,
        {
            **{v.name: tuple(v.values_path or []) for v in variables},
            **{lp.name: tuple(lp.values_path or []) for lp in chart_loops},
        },
    )
    schema_file, schema_note = values_schema_file(name, values_yaml, overlays, hints)

    roundtrips = [_check("values.yaml", "input", text, templates, values_yaml)]
    for i, (fname, overlay) in enumerate(overlays):
        roundtrips.append(
            _check(fname, f"example {i + 2}", variants[i], templates, values_yaml, overlay)
        )

    app_version = next(
        (v.value for v in variables if (v.values_path or [])[-2:] == ["image", "tag"]), "1.0.0"
    )
    chart_yaml = (
        "apiVersion: v2\n"
        f"name: {name}\n"
        f"description: A Helm chart for {len(resources)} Kubernetes "
        f"resource{'s' if len(resources) != 1 else ''}, generated by templatize.\n"
        "type: application\n"
        "version: 0.1.0\n"
        f"appVersion: {json.dumps(app_version, ensure_ascii=False)}\n"
    )
    files = [
        EmittedFile(path="Chart.yaml", content=chart_yaml),
        EmittedFile(path="values.yaml", content=values_yaml),
        *([schema_file] if schema_file else []),
        *(EmittedFile(path=f, content=c) for f, c in overlays),
        EmittedFile(path=".helmignore", content=HELMIGNORE),
        *templates,
    ]
    return ChartResult(
        chart_name=name,
        files=files,
        templates=infos,
        variables=variables,
        roundtrips=roundtrips,
        loops=chart_loops,
        notes=[schema_note] if schema_note else [],
    )


def values_schema_file(
    name: str,
    values_yaml: str,
    overlays: Sequence[tuple[str, str]],
    hints: Mapping[typedvalues.Path, typedvalues.Hint],
) -> tuple[EmittedFile | None, str | None]:
    """``values.schema.json``: Helm checks values against it on install, upgrade and
    template. Built from (and checked against) values.yaml and every example file."""
    from jsonschema import Draft7Validator

    base = values.load(values_yaml)
    trees = [base, *(values.merge(base, values.load(o, f)) for f, o in overlays)]
    schema = typedvalues.values_schema(trees, hints, f"Values for the {name} chart")
    validator = Draft7Validator(schema)
    for tree in trees:
        error = next(iter(validator.iter_errors(tree)), None)
        if error is not None:  # never ship a schema the chart's own values fail
            return None, f"values.schema.json left out: the values do not fit it ({error.message})"
    content = json.dumps(schema, indent=2, ensure_ascii=False) + "\n"
    return EmittedFile(path="values.schema.json", content=content), None


def _as_rows(lp: Loop, value: object) -> object:
    """An example's list for ``lp`` as chart rows (a map's mapping -> key/value rows)."""
    if lp.map and isinstance(value, dict):
        key, val = (f.name for f in lp.fields)
        return [{key: k, val: v} for k, v in value.items()]
    return value


def _range_emit(
    ir: TemplateIR,
    lp: Loop,
    variables: Sequence[Variable],
    path_of: dict[str, list[str]],
    offset: int,
    parent: Loop | None = None,
) -> gotemplate.RangeEmit:
    """The loop's first item as a ``range`` body, relative to its resource chunk."""
    first = lp.items[0]
    holes: list[tuple[Span, str | gotemplate.RangeEmit]] = []
    for f in lp.fields:
        ref = f"${lp.item}" if lp.scalar else f"${lp.item}.{f.name}"
        if f.align is not None:  # padded table column; %v, as values may be numbers
            ref = f'printf "%{"-" if f.align == "left" else ""}{f.width}v" {ref}'
        holes.append((f.spans[0], ref))
    kids = [(c.group_region(0), c) for c in ir.children(lp)]
    for _, c in kids:
        holes.append((c.group_region(0), _range_emit(ir, c, variables, path_of, offset, lp)))
    holes += [
        (s, "$.Values." + ".".join(path_of[v.name]))
        for v in variables
        for s in v.spans
        if first.contains(s) and not any(r.contains(s) for r, _ in kids)
    ]
    holes.sort(key=lambda h: h[0].start)

    def rel(s: Span) -> Span:
        return Span(start=s.start - offset, end=s.end - offset)

    sep = next(
        (
            (lp.items[r[0]].end - offset, lp.items[r[1]].start - offset)
            for r in lp.group_ranges()
            if len(r) > 1
        ),
        None,
    )
    return gotemplate.RangeEmit(
        items=[rel(it) for it in lp.items],
        var=lp.item,
        path=lp.values_path or [],
        holes=[(rel(s), a) for s, a in holes],
        source=f"${parent.item}.{lp.name}" if parent is not None else None,
        separator_span=sep,
    )


def values_problems(tree: Mapping[str, Any], schema_text: str, limit: int = 5) -> list[str]:
    """Where ``tree`` does not fit a chart's ``values.schema.json`` (``path: message``),
    as ``helm template`` would refuse it. An unreadable schema is a problem too."""
    from jsonschema import Draft7Validator
    from jsonschema.exceptions import SchemaError as JsonSchemaError

    try:
        schema = json.loads(schema_text)
        validator = Draft7Validator(schema)
        errors = sorted(validator.iter_errors(tree), key=lambda e: list(map(str, e.absolute_path)))
    except (ValueError, JsonSchemaError) as exc:
        return [f"values.schema.json is not a valid schema ({str(exc).splitlines()[0][:120]})"]
    out = []
    for e in errors[:limit]:
        path = ".".join(str(p) for p in e.absolute_path) or "(values)"
        out.append(f"{path}: {_message(e)}")
    if len(errors) > limit:
        out.append(f"and {len(errors) - limit} more")
    return out


_PATTERN_MEANS = {
    p.pattern: what
    for p, what in (
        (typedvalues.PATTERNS["integer"], "an integer"),
        (typedvalues.PATTERNS["number"], "a number"),
        (typedvalues.PATTERNS["boolean"], "true or false"),
    )
}


def _message(e: Any) -> str:
    if e.validator == "pattern" and e.validator_value in _PATTERN_MEANS:
        return f"{e.instance!r} is not {_PATTERN_MEANS[e.validator_value]}"
    return str(e.message)[:200]
