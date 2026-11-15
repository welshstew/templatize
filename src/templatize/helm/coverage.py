"""Render a chart with the real ``helm`` binary and compare it with the manifests.

Used by the ``helm create``-style chart, whose templates (``include``, ``toYaml``,
helpers) need a real Go template engine. The comparison is semantic: documents are
matched by ``kind`` + ``metadata.name`` and compared field by field, giving

* **covered**: the field renders exactly as in the input;
* **changed**: present in both with a different value;
* **lost**: in the input but not rendered (the scaffold cannot express it);
* **added**: rendered but not in the input (scaffold labels, a ServiceAccount, ...).

If ``helm`` is not installed the report says so instead of failing the build.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, computed_field

from templatize.emitters.base import EmittedFile

HELM_TIMEOUT = 60
MAX_DIFFS = 200


class FieldDiff(BaseModel):
    path: str
    expected: str | None = None
    actual: str | None = None


class ResourceCoverage(BaseModel):
    kind: str
    name: str
    status: Literal["matched", "lost", "added"]
    covered: int = 0
    lost: list[FieldDiff] = []
    changed: list[FieldDiff] = []
    added: list[FieldDiff] = []


class CoverageReport(BaseModel):
    label: str
    """``values.yaml`` or the example's values file."""
    document: str
    """``input`` or ``example N``."""
    available: bool
    error: str | None = None
    resources: list[ResourceCoverage] = []
    covered: int = 0
    expected_fields: int = 0
    lost: int = 0
    changed: int = 0
    added: int = 0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def percent(self) -> float:
        """Share of the input's fields that render identically."""
        return (
            round(100.0 * self.covered / self.expected_fields, 1) if self.expected_fields else 100.0
        )


class HelmUnavailable(RuntimeError):
    pass


def helm_binary() -> str | None:
    """``TEMPLATIZE_HELM`` (a path, or ``0`` to disable) or ``helm`` on PATH."""
    configured = os.environ.get("TEMPLATIZE_HELM", "").strip()
    if configured == "0":
        return None
    if configured:
        return configured if Path(configured).is_file() else None
    return shutil.which("helm")


def helm_template(
    files: Sequence[EmittedFile], chart_name: str, overlay: str | None = None
) -> list[EmittedFile]:
    """Run ``helm template`` on the chart; returns rendered files keyed by template path.

    Runs with private, empty Helm home directories: no repositories, plugins or
    registry credentials are read, and nothing is fetched."""
    helm = helm_binary()
    if helm is None:
        raise HelmUnavailable("helm is not installed")
    with tempfile.TemporaryDirectory(prefix="templatize-helm-") as tmp:
        root = Path(tmp) / chart_name
        for f in files:
            target = (root / f.path).resolve()
            if not target.is_relative_to(root.resolve()):
                raise ValueError(f"invalid chart path {f.path!r}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f.content, encoding="utf-8", newline="")
        args = [helm, "template", chart_name, str(root), "--skip-tests"]
        if overlay is not None:
            (Path(tmp) / "overlay.yaml").write_text(overlay, encoding="utf-8", newline="")
            args += ["-f", str(Path(tmp) / "overlay.yaml")]
        home = Path(tmp) / "home"
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(home),
            "HELM_CACHE_HOME": str(home / "cache"),
            "HELM_CONFIG_HOME": str(home / "config"),
            "HELM_DATA_HOME": str(home / "data"),
        }
        try:
            proc = subprocess.run(
                args, capture_output=True, text=True, timeout=HELM_TIMEOUT, env=env, check=False
            )
        except subprocess.TimeoutExpired:
            raise ValueError("helm template timed out") from None
        if proc.returncode != 0:
            # Helm's error quotes template paths, not document text.
            msg = proc.stderr.strip().replace(str(root), chart_name)
            raise ValueError(f"helm template failed: {msg[:2000]}")
    return split_rendered(proc.stdout, chart_name)


def split_rendered(output: str, chart_name: str) -> list[EmittedFile]:
    """Split ``helm template`` output on its ``---`` + ``# Source: <chart>/<path>``
    headers. A template that renders several documents appears once per document."""
    header = re.compile(
        r"^---\r?\n# Source: " + re.escape(chart_name) + r"/(.+?)\r?\n", re.MULTILINE
    )
    parts = header.split(output)
    files: dict[str, str] = {}
    for path, body in zip(parts[1::2], parts[2::2], strict=True):
        files[path] = files.get(path, "") + ("---\n" if path in files else "") + body
    return [EmittedFile(path=p, content=c) for p, c in files.items()]


def _docs(text: str) -> list[dict[str, Any]]:
    try:
        return [d for d in yaml.safe_load_all(text) if isinstance(d, dict)]
    except (yaml.YAMLError, ValueError, RecursionError) as exc:
        raise ValueError(f"cannot parse rendered YAML: {exc}") from None


def _key(doc: dict[str, Any]) -> tuple[str, str]:
    meta = doc.get("metadata") or {}
    return str(doc.get("kind", "?")), str(meta.get("name", "?") if isinstance(meta, dict) else "?")


def flatten(node: Any, prefix: str = "") -> dict[str, str]:
    """Leaf paths -> JSON-encoded values; empty maps/lists count as leaves."""
    out: dict[str, str] = {}
    if isinstance(node, dict) and node:
        for k, v in node.items():
            key = str(k)
            seg = key if key.replace("_", "a").isalnum() else json.dumps(key)
            out.update(flatten(v, f"{prefix}.{seg}" if prefix else seg))
    elif isinstance(node, list) and node:
        for i, v in enumerate(node):
            out.update(flatten(v, f"{prefix}[{i}]"))
    else:
        out[prefix] = json.dumps(node, sort_keys=True, default=str)
    return out


def compare(expected_text: str, rendered_text: str, label: str, document: str) -> CoverageReport:
    expected = {_key(d): d for d in _docs(expected_text)}
    rendered = {_key(d): d for d in _docs(rendered_text)}
    report = CoverageReport(label=label, document=document, available=True)
    for key, doc in expected.items():
        exp = flatten(doc)
        report.expected_fields += len(exp)
        if key not in rendered:
            report.resources.append(
                ResourceCoverage(
                    kind=key[0],
                    name=key[1],
                    status="lost",
                    lost=[FieldDiff(path=p, expected=v) for p, v in list(exp.items())[:MAX_DIFFS]],
                )
            )
            report.lost += len(exp)
            continue
        act = flatten(rendered[key])
        rc = ResourceCoverage(kind=key[0], name=key[1], status="matched")
        for p, v in exp.items():
            if p not in act:
                rc.lost.append(FieldDiff(path=p, expected=v))
            elif act[p] != v:
                rc.changed.append(FieldDiff(path=p, expected=v, actual=act[p]))
            else:
                rc.covered += 1
        rc.added = [FieldDiff(path=p, actual=v) for p, v in act.items() if p not in exp]
        report.covered += rc.covered
        report.lost += len(rc.lost)
        report.changed += len(rc.changed)
        report.added += len(rc.added)
        rc.lost, rc.changed, rc.added = (
            rc.lost[:MAX_DIFFS],
            rc.changed[:MAX_DIFFS],
            rc.added[:MAX_DIFFS],
        )
        report.resources.append(rc)
    for key, doc in rendered.items():
        if key not in expected:
            act = flatten(doc)
            report.added += len(act)
            report.resources.append(
                ResourceCoverage(
                    kind=key[0],
                    name=key[1],
                    status="added",
                    added=[FieldDiff(path=p, actual=v) for p, v in list(act.items())[:MAX_DIFFS]],
                )
            )
    return report


def coverage(
    files: Sequence[EmittedFile],
    chart_name: str,
    expected_text: str,
    label: str,
    document: str,
    overlay: str | None = None,
) -> CoverageReport:
    try:
        rendered = helm_template(files, chart_name, overlay)
    except HelmUnavailable as exc:
        return CoverageReport(label=label, document=document, available=False, error=str(exc))
    except ValueError as exc:
        return CoverageReport(label=label, document=document, available=True, error=str(exc))
    try:
        return compare(expected_text, "---\n".join(f.content for f in rendered), label, document)
    except ValueError as exc:
        return CoverageReport(label=label, document=document, available=True, error=str(exc))
