"""``helm create``-style chart: the standard scaffold, filled in from the manifests.

The chart is what ``helm create`` (Helm 4.3.2, bundled in ``scaffold/``) generates,
with its ``values.yaml`` placeholders (``nginx``, ``chart-example.local``) replaced
by what the manifests actually say: image, replicas, ports, resources, probes,
env, ingress hosts and TLS, autoscaling, security contexts, scheduling.

The scaffold models one application (a Deployment with its Service, Ingress, HPA
and ServiceAccount). Anything else (ConfigMaps, Secrets, extra Services, ...) is
carried over as an exact template with the accepted variables, as in the exact
chart style, so nothing is silently dropped.

Because the scaffold adds labels, names things through helpers and has fields of
its own, the output is not byte-identical to the input. Instead
:mod:`templatize.helm.coverage` renders it with real Helm and reports, field by
field, what is covered, changed, lost and added.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml

from templatize import typedvalues
from templatize.build import VariableSpec, make_ir
from templatize.emitters.base import EmittedFile
from templatize.helm import coverage as cov
from templatize.helm import gotemplate
from templatize.helm import values as helm_values
from templatize.helm.chart import (
    CHART_NAME_RE,
    LABEL_RE,
    ChartError,
    ChartResult,
    ChartTemplate,
    FilledValue,
    assign_paths,
    default_chart_name,
    values_schema_file,
)
from templatize.ir import Span, Variable
from templatize.k8s import Resource, parse_resources
from templatize.suggest import check_limits

SCAFFOLD_DIR = Path(__file__).parent / "scaffold"
PLACEHOLDER = "tzscaffold"
HELM_VERSION = "4.3.2"
# Labels the scaffold sets itself; copying them into podLabels would duplicate keys.
SCAFFOLD_LABELS = frozenset(
    {
        "helm.sh/chart",
        "app.kubernetes.io/name",
        "app.kubernetes.io/instance",
        "app.kubernetes.io/version",
        "app.kubernetes.io/managed-by",
    }
)
MAPPED_KINDS = frozenset(
    {"Deployment", "Service", "Ingress", "HorizontalPodAutoscaler", "ServiceAccount"}
)


def scaffold_files(chart_name: str) -> list[EmittedFile]:
    """The bundled scaffold's templates and .helmignore, renamed to ``chart_name``."""
    out: list[EmittedFile] = []
    for path in sorted(SCAFFOLD_DIR.rglob("*")):
        rel = path.relative_to(SCAFFOLD_DIR).as_posix()
        if not path.is_file() or rel in ("Chart.yaml", "values.yaml", "NOTICE"):
            continue
        out.append(
            EmittedFile(path=rel, content=path.read_text("utf-8").replace(PLACEHOLDER, chart_name))
        )
    order = {".helmignore": 0}
    out.sort(key=lambda f: (order.get(f.path, 1), f.path.count("/"), f.path))
    return out


def scaffold_defaults() -> dict[str, Any]:
    data = yaml.safe_load((SCAFFOLD_DIR / "values.yaml").read_text("utf-8"))
    assert isinstance(data, dict)
    return data


def scaffold_comments() -> dict[str, list[str]]:
    """Comment lines directly above each top-level key of the scaffold's values.yaml."""
    comments: dict[str, list[str]] = {}
    pending: list[str] = []
    for line in (SCAFFOLD_DIR / "values.yaml").read_text("utf-8").splitlines():
        if line.startswith("#"):
            pending.append(line)
        elif line and not line[0].isspace() and ":" in line:
            comments[line.split(":", 1)[0]] = pending
            pending = []
        elif not line.strip():
            pending = [] if not pending or pending[-1] != "#" else pending
    return comments


def _load(text: str, r: Resource) -> dict[str, Any]:
    try:
        data = yaml.safe_load(text[r.chunk.start : r.chunk.end])
    except (yaml.YAMLError, ValueError, RecursionError):
        return {}  # not parseable: treated as a resource without fields
    return data if isinstance(data, dict) else {}


def _get(d: Any, *path: str) -> Any:
    for k in path:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def _pod_spec(dep: dict[str, Any]) -> dict[str, Any]:
    pod = _get(dep, "spec", "template", "spec")
    return pod if isinstance(pod, dict) else {}


def map_values(text: str) -> tuple[dict[str, Any], set[int], list[str]]:
    """Scaffold values from the manifests: (values, indexes of mapped resources, notes)."""
    resources = parse_resources(text)
    objs = [_load(text, r) for r in resources]
    deps = [i for i, r in enumerate(resources) if r.kind == "Deployment"]
    if len(deps) != 1:
        raise ChartError(
            "the helm create chart style models one application: it needs exactly one "
            f"Deployment (found {len(deps)}). Use the exact chart style for these manifests."
        )
    v = scaffold_defaults()
    notes: list[str] = []
    handled = {deps[0]}
    dep = objs[deps[0]]
    pod = _pod_spec(dep)
    containers = [c for c in pod.get("containers") or [] if isinstance(c, dict)]
    if not containers:
        raise ChartError("the Deployment has no containers")
    c = containers[0]
    if len(containers) > 1:
        notes.append(
            f"only the first container ({c.get('name')!r}) fits the scaffold; "
            f"{len(containers) - 1} more are not part of the chart"
        )

    if (replicas := _get(dep, "spec", "replicas")) is not None:
        v["replicaCount"] = replicas
    image = str(c.get("image") or "")
    at, slash, colon = image.find("@"), image.rfind("/"), image.rfind(":")
    if at != -1:
        v["image"]["repository"], v["image"]["tag"] = image[:at], ""
        notes.append("the image is pinned by digest; the scaffold uses repository:tag")
    elif colon > slash:
        v["image"]["repository"], v["image"]["tag"] = image[:colon], image[colon + 1 :]
    else:
        v["image"]["repository"], v["image"]["tag"] = image, ""
    if c.get("imagePullPolicy"):
        v["image"]["pullPolicy"] = c["imagePullPolicy"]
    v["imagePullSecrets"] = pod.get("imagePullSecrets") or []

    v["fullnameOverride"] = _get(dep, "metadata", "name") or ""
    pod_labels = _get(dep, "spec", "template", "metadata", "labels") or {}
    if isinstance(pod_labels, dict):
        if "app.kubernetes.io/name" in pod_labels:
            v["nameOverride"] = str(pod_labels["app.kubernetes.io/name"])
        v["podLabels"] = {k: val for k, val in pod_labels.items() if k not in SCAFFOLD_LABELS}
    v["podAnnotations"] = _get(dep, "spec", "template", "metadata", "annotations") or {}
    v["podSecurityContext"] = pod.get("securityContext") or {}
    v["securityContext"] = c.get("securityContext") or {}
    v["containerName"] = c.get("name") or ""
    ports = [p for p in c.get("ports") or [] if isinstance(p, dict)]
    if ports and ports[0].get("containerPort") is not None:
        v["containerPort"] = ports[0]["containerPort"]
    for key in ("command", "args", "env", "envFrom"):
        if c.get(key):
            v[key] = c[key]
    v["resources"] = c.get("resources") or {}
    v["livenessProbe"] = c.get("livenessProbe") or {}
    v["readinessProbe"] = c.get("readinessProbe") or {}
    v["volumeMounts"] = c.get("volumeMounts") or []
    v["volumes"] = pod.get("volumes") or []
    for key in ("nodeSelector", "tolerations", "affinity"):
        v[key] = pod.get(key) or v[key]

    sa_name = pod.get("serviceAccountName") or ""
    sa_idx = next(
        (
            i
            for i, r in enumerate(resources)
            if r.kind == "ServiceAccount" and (not sa_name or r.name == sa_name)
        ),
        None,
    )
    v["serviceAccount"]["create"] = sa_idx is not None
    v["serviceAccount"]["name"] = sa_name or (resources[sa_idx].name if sa_idx is not None else "")
    if sa_idx is not None:
        handled.add(sa_idx)
        sa = objs[sa_idx]
        v["serviceAccount"]["annotations"] = _get(sa, "metadata", "annotations") or {}
        if "automountServiceAccountToken" in sa:
            v["serviceAccount"]["automount"] = sa["automountServiceAccountToken"]

    svc_idx = next((i for i, r in enumerate(resources) if r.kind == "Service"), None)
    if svc_idx is not None:
        handled.add(svc_idx)
        svc = objs[svc_idx]
        v["service"]["type"] = _get(svc, "spec", "type") or "ClusterIP"
        sports = [p for p in _get(svc, "spec", "ports") or [] if isinstance(p, dict)]
        if sports:
            v["service"]["port"] = sports[0].get("port", v["service"]["port"])
            if sports[0].get("targetPort") is not None:
                v["service"]["targetPort"] = sports[0]["targetPort"]
        if len(sports) > 1:
            notes.append(
                f"the scaffold's Service has one port; {len(sports) - 1} more are not part of the chart"
            )

    ing_idx = next((i for i, r in enumerate(resources) if r.kind == "Ingress"), None)
    if ing_idx is not None:
        handled.add(ing_idx)
        ing = objs[ing_idx]
        v["ingress"]["enabled"] = True
        v["ingress"]["className"] = _get(ing, "spec", "ingressClassName") or ""
        v["ingress"]["annotations"] = _get(ing, "metadata", "annotations") or {}
        hosts = []
        for rule in _get(ing, "spec", "rules") or []:
            if not isinstance(rule, dict):
                continue
            paths = [
                {k: p[k] for k in ("path", "pathType") if k in p}
                for p in _get(rule, "http", "paths") or []
                if isinstance(p, dict)
            ]
            hosts.append({"host": rule.get("host", ""), "paths": paths})
        v["ingress"]["hosts"] = hosts
        v["ingress"]["tls"] = _get(ing, "spec", "tls") or []

    hpa_idx = next(
        (i for i, r in enumerate(resources) if r.kind == "HorizontalPodAutoscaler"), None
    )
    if hpa_idx is not None:
        handled.add(hpa_idx)
        hpa = objs[hpa_idx]
        a = v["autoscaling"]
        a["enabled"] = True
        for key in ("minReplicas", "maxReplicas"):
            if _get(hpa, "spec", key) is not None:
                a[key] = _get(hpa, "spec", key)
        a.pop("targetCPUUtilizationPercentage", None)
        for m in _get(hpa, "spec", "metrics") or []:
            name = _get(m, "resource", "name")
            util = _get(m, "resource", "target", "averageUtilization")
            if name in ("cpu", "memory") and util is not None:
                a[f"target{'CPU' if name == 'cpu' else 'Memory'}UtilizationPercentage"] = util

    namespaces = {r.namespace for r in resources if r.namespace}
    if namespaces:
        notes.append(
            "the scaffold does not set metadata.namespace: install with "
            f"`helm install ... -n {sorted(namespaces)[0]}`"
        )
    return v, handled, notes


def diff_tree(base: Any, other: Any) -> Any:
    """What ``other`` changes relative to ``base`` (maps recurse; anything else replaces)."""
    if isinstance(base, dict) and isinstance(other, dict):
        out = {}
        for k, val in other.items():
            if k not in base:
                out[k] = val
            elif (d := diff_tree(base[k], val)) is not _SAME:
                out[k] = d
        return out if out else _SAME
    return _SAME if base == other else other


_SAME: Any = object()


def filled_paths(base: Any, other: Any, prefix: tuple[str, ...] = ()) -> list[list[str]]:
    """Paths of ``other`` that differ from ``base``: a new key or a replaced value is
    one entry; a map that only partly changed is descended into (unless the default
    is an empty map, which the manifests fill as a whole)."""
    if isinstance(base, dict) and isinstance(other, dict) and base:
        out: list[list[str]] = []
        for k, val in other.items():
            if k not in base:
                out.append([*prefix, str(k)])
            elif base[k] != val:
                out.extend(filled_paths(base[k], val, (*prefix, str(k))))
        return out
    return [] if base == other else [list(prefix)]


def _sources(resources: Sequence[Resource], handled: set[int]) -> dict[str, str]:
    """Top-level values key -> the resource its value was read from."""

    def first(kind: str) -> str | None:
        r = next((resources[i] for i in sorted(handled) if resources[i].kind == kind), None)
        return f"{r.kind} {r.name}" if r else None

    dep = first("Deployment") or "Deployment"
    out = {
        "service": first("Service"),
        "ingress": first("Ingress"),
        "autoscaling": first("HorizontalPodAutoscaler"),
        "serviceAccount": first("ServiceAccount") or dep,
    }
    return {k: v for k, v in out.items() if v} | {"*": dep}


def dump_values(
    tree: dict[str, Any], header: str, comments: dict[str, list[str]] | None = None
) -> str:
    lines = [f"# {h}" for h in header.splitlines()]
    for key, val in tree.items():
        lines.append("")
        lines.extend((comments or {}).get(key, []))
        dumped = yaml.safe_dump(
            {key: val}, sort_keys=False, default_flow_style=False, allow_unicode=True, width=10_000
        )
        lines.append(dumped.rstrip("\n"))
    if not tree:
        lines.append("{}")
    return "\n".join(lines) + "\n"


def _carried(
    text: str,
    resources: Sequence[Resource],
    keep: list[int],
    variables: list[Variable],
    reserved: set[str],
) -> tuple[list[EmittedFile], list[ChartTemplate], list[Variable]]:
    """Exact templates for the resources the scaffold does not model."""
    lo_hi = [resources[i].chunk for i in keep]
    # Variables keep the occurrences inside carried resources (a namespace used
    # everywhere still parameterises the ConfigMap that is carried over).
    inside = []
    for v in variables:
        spans = [s for s in v.spans if any(c.start <= s.start and s.end <= c.end for c in lo_hi)]
        if spans:
            inside.append(v.model_copy(update={"spans": spans}))
    paths = assign_paths(inside, reserved=reserved)
    inside = [v.model_copy(update={"values_path": p}) for v, p in zip(inside, paths, strict=True)]
    kinds = [resources[i].kind.lower() for i in keep]
    files: list[EmittedFile] = []
    infos: list[ChartTemplate] = []
    used = {
        "deployment",
        "service",
        "ingress",
        "hpa",
        "serviceaccount",
        "httproute",
        "NOTES",
        "_helpers",
    }
    for i, kind in zip(keep, kinds, strict=True):
        r = resources[i]
        base = kind if kind not in used and kinds.count(kind) == 1 else f"{kind}-{r.name.lower()}"
        base = (
            "".join(ch if ch.isalnum() or ch == "-" else "-" for ch in base).strip("-")
            or "resource"
        )
        name, n = base, 2
        while name in used:
            name, n = f"{base}-{n}", n + 1
        used.add(name)
        occ = sorted(
            (
                (
                    Span(start=s.start - r.chunk.start, end=s.end - r.chunk.start),
                    v.values_path or [],
                )
                for v in inside
                for s in v.spans
                if r.chunk.start <= s.start and s.end <= r.chunk.end
            ),
            key=lambda o: o[0].start,
        )
        path = f"templates/{name}.yaml"
        files.append(
            EmittedFile(path=path, content=gotemplate.emit(text[r.chunk.start : r.chunk.end], occ))
        )
        infos.append(ChartTemplate(path=path, kind=r.kind, name=r.name, span=r.chunk))
    return files, infos, inside


def build_scaffold_chart(
    text: str,
    specs: Sequence[VariableSpec],
    variants: Sequence[str] = (),
    *,
    chart_name: str | None = None,
    variant_labels: Sequence[str] | None = None,
    check: bool = True,
) -> ChartResult:
    check_limits(text, variants)
    resources = parse_resources(text)
    if not resources:
        raise ChartError(
            "a Helm chart needs Kubernetes manifests (apiVersion, kind, metadata.name)"
        )
    name = chart_name or default_chart_name(resources)
    if not CHART_NAME_RE.match(name):
        raise ChartError(
            f"invalid chart name {name!r}: lowercase letters, digits and '-', at most 53 characters"
        )
    labels = list(variant_labels or [f"example-{i + 2}" for i in range(len(variants))])
    if len(labels) != len(variants) or len(set(labels)) != len(labels):
        raise ChartError("give one unique label per example document")
    for label in labels:
        if not LABEL_RE.match(label):
            raise ChartError(f"invalid example label {label!r}: lowercase letters, digits and '-'")

    values, handled, notes = map_values(text)
    keep = [i for i in range(len(resources)) if i not in handled]
    ir = make_ir(text, specs)
    carried, carried_infos, carried_vars = _carried(
        text, resources, keep, ir.variables, set(values)
    )
    try:
        carried_tree = helm_values.build_tree(
            (v.values_path or [], helm_values.typed(v.value)) for v in carried_vars
        )
    except helm_values.ValuesError as exc:
        raise ChartError(str(exc)) from None
    values = {**values, **carried_tree}

    comments = scaffold_comments()
    values_yaml = dump_values(
        values,
        header=(
            f"Default values for {name}: the `helm create` scaffold (Helm {HELM_VERSION}),\n"
            "filled in by templatize from the input manifests."
        ),
        comments=comments,
    )

    defaults = scaffold_defaults()
    sources = _sources(resources, handled)
    filled = [
        FilledValue(path=p, source=sources.get(p[0], sources["*"]))
        for p in filled_paths(defaults, {k: v for k, v in values.items() if k not in carried_tree})
        if p
    ]
    for v in carried_vars:
        r = next(
            (r for r in resources if v.spans and r.chunk.start <= v.spans[0].start < r.chunk.end),
            None,
        )
        filled.append(
            FilledValue(
                path=list(v.values_path or []),
                source=f"variable {v.name}" + (f" ({r.kind} {r.name})" if r else ""),
            )
        )

    by_name = {s.name: s for s in specs}
    overlays: list[tuple[str, str]] = []
    for i, (label, variant) in enumerate(zip(labels, variants, strict=True)):
        try:
            vvalues, _, _ = map_values(variant)
        except ChartError as exc:
            raise ChartError(f"example {i + 2}: {exc}") from None
        diff = diff_tree({k: v for k, v in values.items() if k not in carried_tree}, vvalues)
        overlay = {} if diff is _SAME else dict(diff)
        for v in carried_vars:
            vv = by_name[v.name].variant_values
            if vv is not None and i < len(vv) and vv[i] != v.value:
                node = overlay
                path = v.values_path or []
                for k in path[:-1]:
                    node = node.setdefault(k, {})
                node[path[-1]] = helm_values.typed(vv[i])
        overlays.append(
            (
                f"values-{label}.yaml",
                dump_values(
                    overlay,
                    header=f"Values for {label} (example {i + 2}): only what differs from values.yaml.",
                ),
            )
        )

    schema_file, schema_note = values_schema_file(
        name,
        values_yaml,
        overlays,
        typedvalues.variable_hints(
            carried_vars, [], ir, {v.name: tuple(v.values_path or []) for v in carried_vars}
        ),
    )
    if schema_note:
        notes = [*notes, schema_note]

    app_version = values["image"].get("tag") or "1.16.0"
    chart_yaml = (
        "apiVersion: v2\n"
        f"name: {name}\n"
        "description: A Helm chart for Kubernetes, from the helm create scaffold filled in by templatize.\n"
        "type: application\n"
        "version: 0.1.0\n"
        f"appVersion: {json.dumps(str(app_version), ensure_ascii=False)}\n"
    )
    scaffold = scaffold_files(name)
    files = [
        EmittedFile(path="Chart.yaml", content=chart_yaml),
        EmittedFile(path="values.yaml", content=values_yaml),
        *([schema_file] if schema_file else []),
        *(EmittedFile(path=f, content=c) for f, c in overlays),
        *scaffold,
        *carried,
    ]
    templates = [
        ChartTemplate(
            path=f.path,
            kind="(scaffold)",
            name=f.path.split("/")[-1],
            span=Span(start=0, end=max(len(text), 1)),
        )
        for f in scaffold
        if f.path.startswith("templates/")
    ] + carried_infos
    reports: list[cov.CoverageReport] = []
    if check:
        reports.append(cov.coverage(files, name, text, "values.yaml", "input"))
        for i, (fname, overlay_text) in enumerate(overlays):
            reports.append(
                cov.coverage(files, name, variants[i], fname, f"example {i + 2}", overlay_text)
            )
    variables = [
        *(v for v in ir.variables if v.name not in {c.name for c in carried_vars}),
        *carried_vars,
    ]
    return ChartResult(
        chart_name=name,
        files=files,
        templates=templates,
        variables=variables,
        roundtrips=[],
        style="scaffold",
        coverage=reports,
        notes=notes,
        filled=filled,
    )


__all__ = ["build_scaffold_chart", "map_values", "scaffold_files"]
