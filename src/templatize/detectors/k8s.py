"""Kubernetes-aware detector: knows what the fields of common resources mean.

Works on the YAML node tree from :mod:`templatize.k8s`, so spans are exact. Every
candidate carries a Helm-style ``values_path`` (``image.tag``, ``replicaCount``,
``service.port``) and a ``schema`` name origin, which outranks names derived from
the bare YAML key. Values are only proposed for single-line scalars whose source
text equals their value (no escapes, no folding), which keeps round trips exact.

Paths are prefixed with the resource (and container) name only when the input has
more than one of that kind, so a single Deployment gets plain ``image.tag``.
"""

from __future__ import annotations

import re
from collections import Counter

from yaml.nodes import Node, ScalarNode

from templatize.detectors.base import DetectionContext, DetectorResult
from templatize.ir import VALUES_KEY_RE, Candidate, Source, Span, VarType
from templatize.k8s import Resource, get, items, parse_resources, scalar, seq
from templatize.naming import infer_type, snake_case

# Where the pod spec lives, per workload kind.
POD_SPEC: dict[str, tuple[str, ...]] = {
    "Deployment": ("spec", "template", "spec"),
    "StatefulSet": ("spec", "template", "spec"),
    "DaemonSet": ("spec", "template", "spec"),
    "ReplicaSet": ("spec", "template", "spec"),
    "Job": ("spec", "template", "spec"),
    "CronJob": ("spec", "jobTemplate", "spec", "template", "spec"),
    "Pod": ("spec",),
}
SCALABLE = frozenset({"Deployment", "StatefulSet", "ReplicaSet"})

CONF_FIELD = 0.9
CONF_IMAGE = 0.92
CONF_NAME = 0.6
CONF_SECRET_DATA = 0.5
CONF_DIFF = 0.99

_SPLIT = re.compile(r"[^A-Za-z0-9]+")


def camel(raw: str) -> str:
    """``payments-api`` -> ``paymentsApi``; always a valid values key."""
    parts = [p for p in _SPLIT.split(raw) if p]
    if not parts:
        return "value"
    out = parts[0][:1].lower() + parts[0][1:] + "".join(p[:1].upper() + p[1:] for p in parts[1:])
    return out if VALUES_KEY_RE.match(out) else f"v{out}"


def key(raw: str) -> str:
    """Keep keys that already are identifiers (``UPSTREAM_URL``); camelCase the rest."""
    return raw if VALUES_KEY_RE.match(raw) else camel(raw)


def scalar_span(text: str, node: Node | None) -> Span | None:
    """Span of a scalar's value in ``text``, inside quotes; ``None`` unless the source
    text is exactly the value (single line, no escapes)."""
    if not isinstance(node, ScalarNode) or node.style in ("|", ">"):
        return None
    start, end = node.start_mark.index, node.end_mark.index
    if node.style in ('"', "'"):
        start, end = start + 1, end - 1
    if end <= start or text[start:end] != node.value or "\n" in node.value or "\r" in node.value:
        return None
    return Span(start=start, end=end)


class _Collector:
    def __init__(self, text: str) -> None:
        self.text = text
        self.out: list[Candidate] = []

    def add(
        self,
        node: Node | None,
        path: list[str],
        reason: str,
        confidence: float = CONF_FIELD,
        sub: tuple[int, int] | None = None,
        port: bool = False,
    ) -> None:
        span = scalar_span(self.text, node)
        if span is None:
            return
        if sub is not None:
            span = Span(start=span.start + sub[0], end=span.start + sub[1])
        value = self.text[span.start : span.end]
        if not value.strip() or value.lower() in ("null", "~"):
            return
        vtype = infer_type(value)
        if port and vtype is VarType.INT:  # a number in a port field (not a name)
            vtype = VarType.PORT
        self.out.append(
            Candidate(
                name=snake_case("_".join(path)),
                name_origin="schema",
                type=vtype,
                value=value,
                spans=[span],
                sources=[Source.KEY_VALUE],
                detectors=["k8s"],
                confidence=confidence,
                reason=reason,
                values_path=path,
            )
        )


class KubernetesDetector:
    name = "k8s"

    def detect(self, ctx: DetectionContext) -> DetectorResult:
        main = collect(ctx.text)
        if not main:
            return DetectorResult()
        # Examples: read the same paths from each variant, so a changed image tag is
        # reported on the tag itself instead of the whole image string.
        by_variant: list[dict[tuple[str, ...], str]] = []
        main_kinds = [r.kind for r in parse_resources(ctx.text)]
        for variant in ctx.variants:
            if [r.kind for r in parse_resources(variant)] != main_kinds:
                by_variant = []
                break
            by_variant.append({tuple(c.values_path or []): c.value for c in collect(variant)})
        out: list[Candidate] = []
        for c in main:
            path = tuple(c.values_path or [])
            vals = [m.get(path) for m in by_variant]
            if by_variant and all(v is not None for v in vals) and any(v != c.value for v in vals):
                c = c.model_copy(
                    update={
                        "variant_values": vals,
                        "confidence": CONF_DIFF,
                        "sources": [Source.DIFF, Source.KEY_VALUE],
                        "reason": f"{c.reason}; differs between examples",
                    }
                )
            out.append(c)
        return DetectorResult(candidates=out)


def split_spans(text: str) -> list[Span]:
    """Whole-value spans this detector splits into parts (``repo:tag`` images). Other
    detectors' candidates on exactly these spans are superseded by the parts."""
    out: list[Span] = []
    for r in parse_resources(text):
        if r.kind not in POD_SPEC:
            continue
        pod = get(r.root, *POD_SPEC[r.kind])
        for c in seq(get(pod, "containers")) + seq(get(pod, "initContainers")):
            node = get(c, "image")
            image = scalar(node)
            span = scalar_span(text, node)
            if image and span and ("@" in image or image.rfind(":") > image.rfind("/")):
                out.append(span)
    return out


def collect(text: str) -> list[Candidate]:
    resources = parse_resources(text)
    if not resources:
        return []
    kinds = Counter(r.kind for r in resources)
    workloads = sum(kinds[k] for k in POD_SPEC)
    col = _Collector(text)
    for r in resources:
        col.add(get(r.root, "metadata", "namespace"), ["namespace"], "resource namespace", 0.75)
        if r.kind in POD_SPEC:
            _Rules.workload(col, r, [] if workloads == 1 else [camel(r.name)])
            continue
        own = [] if kinds[r.kind] == 1 else [camel(r.name)]
        col.add(
            get(r.root, "metadata", "name"),
            [*own, camel(r.kind) + "Name"],
            f"{r.kind} name",
            CONF_NAME,
        )
        if r.kind == "Service":
            _Rules.service(col, r, own)
        elif r.kind == "Ingress":
            _Rules.ingress(col, r, own)
        elif r.kind == "ConfigMap":
            for k, v in items(get(r.root, "data")):
                col.add(v, [*own, "config", key(k)], f"ConfigMap {r.name} key {k}")
        elif r.kind == "Secret":
            for k, v in items(get(r.root, "stringData")):
                col.add(v, [*own, "secrets", key(k)], f"Secret {r.name} key {k}")
            for k, v in items(get(r.root, "data")):
                col.add(
                    v,
                    [*own, "secrets", key(k)],
                    f"Secret {r.name} key {k} (base64-encoded)",
                    CONF_SECRET_DATA,
                )
        elif r.kind == "HorizontalPodAutoscaler":
            for f in ("minReplicas", "maxReplicas"):
                col.add(get(r.root, "spec", f), ["autoscaling", f], f"HPA {f}")
        elif r.kind == "PersistentVolumeClaim":
            col.add(
                get(r.root, "spec", "resources", "requests", "storage"),
                [*own, "persistence", "size"],
                "volume size",
            )
            col.add(
                get(r.root, "spec", "storageClassName"),
                [*own, "persistence", "storageClass"],
                "storage class",
            )
    return col.out


class _Rules:
    @staticmethod
    def workload(col: _Collector, r: Resource, base: list[str]) -> None:
        col.add(get(r.root, "metadata", "name"), [*base, "name"], f"{r.kind} name", CONF_NAME)
        if r.kind in SCALABLE:
            col.add(get(r.root, "spec", "replicas"), [*base, "replicaCount"], "replica count")
        if r.kind == "CronJob":
            col.add(get(r.root, "spec", "schedule"), [*base, "schedule"], "cron schedule")
        pod = get(r.root, *POD_SPEC[r.kind])
        containers = seq(get(pod, "containers"))
        init = seq(get(pod, "initContainers"))
        for c in containers + init:
            cname = scalar(get(c, "name")) or "container"
            if c in init:
                cbase = [*base, camel(f"init-{cname}")]
            else:
                cbase = base if len(containers) == 1 else [*base, camel(cname)]
            _container(col, c, cbase, cname)
        for i, vct in enumerate(seq(get(r.root, "spec", "volumeClaimTemplates"))):
            suffix = "" if i == 0 else str(i + 1)
            col.add(
                get(vct, "spec", "resources", "requests", "storage"),
                [*base, f"persistence{suffix}", "size"],
                "volume claim size",
            )

    @staticmethod
    def service(col: _Collector, r: Resource, base: list[str]) -> None:
        svc = [*base, "service"]
        col.add(get(r.root, "spec", "type"), [*svc, "type"], "Service type")
        ports = seq(get(r.root, "spec", "ports"))
        for i, p in enumerate(ports):
            if len(ports) == 1:
                pbase = svc
            else:
                pbase = [*svc, "ports", camel(scalar(get(p, "name")) or f"port{i + 1}")]
            col.add(get(p, "port"), [*pbase, "port"], "Service port", port=True)
            col.add(get(p, "targetPort"), [*pbase, "targetPort"], "Service target port", port=True)
            col.add(get(p, "nodePort"), [*pbase, "nodePort"], "Service node port", port=True)

    @staticmethod
    def ingress(col: _Collector, r: Resource, base: list[str]) -> None:
        ing = [*base, "ingress"]
        col.add(get(r.root, "spec", "ingressClassName"), [*ing, "className"], "ingress class")
        rules = seq(get(r.root, "spec", "rules"))
        for i, rule in enumerate(rules):
            path = [*ing, "host"] if len(rules) == 1 else [*ing, "hosts", f"host{i + 1}"]
            col.add(get(rule, "host"), path, "Ingress host")
        tls = seq(get(r.root, "spec", "tls"))
        for i, t in enumerate(tls):
            path = (
                [*ing, "tls", "secretName"]
                if len(tls) == 1
                else [*ing, "tls", f"secretName{i + 1}"]
            )
            col.add(get(t, "secretName"), path, "TLS secret")


def _container(col: _Collector, c: Node, base: list[str], cname: str) -> None:
    image_node = get(c, "image")
    image = scalar(image_node)
    if image:
        at = image.find("@")
        slash = image.rfind("/")
        colon = image.rfind(":")
        if at != -1:
            repo_end, ref_start, ref_key = at, at + 1, "digest"
        elif colon > slash:
            repo_end, ref_start, ref_key = colon, colon + 1, "tag"
        else:
            repo_end, ref_start, ref_key = len(image), len(image), ""
        col.add(
            image_node,
            [*base, "image", "repository"],
            f"image of {cname}",
            CONF_IMAGE - 0.02,
            (0, repo_end),
        )
        if ref_key and ref_start < len(image):
            col.add(
                image_node,
                [*base, "image", ref_key],
                f"image {ref_key} of {cname}",
                CONF_IMAGE,
                (ref_start, len(image)),
            )
    col.add(get(c, "imagePullPolicy"), [*base, "image", "pullPolicy"], "image pull policy")
    for section in ("limits", "requests"):
        for k, v in items(get(c, "resources", section)):
            col.add(v, [*base, "resources", section, key(k)], f"{section} {k} of {cname}")
    ports = seq(get(c, "ports"))
    for i, p in enumerate(ports):
        if len(ports) == 1:
            path = [*base, "containerPort"]
        else:
            path = [*base, "ports", camel(scalar(get(p, "name")) or f"port{i + 1}")]
        col.add(get(p, "containerPort"), path, f"port of {cname}", port=True)
    for e in seq(get(c, "env")):
        ename = scalar(get(e, "name"))
        if ename:
            col.add(get(e, "value"), [*base, "env", key(ename)], f"environment variable {ename}")


__all__ = ["KubernetesDetector", "camel", "collect", "scalar_span", "split_spans"]
