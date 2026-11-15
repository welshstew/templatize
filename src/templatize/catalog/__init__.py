"""The bundled schema catalogue: pinned JSON Schemas shipped with the package.

``manifest.json`` lists each schema: where it came from (a pinned commit or release),
its SHA-256, licence, and how a document is matched to it:

* ``names``: the file name a ``$schema`` URL ends with (``github-workflow``,
  ``deployment-apps-v1``), wherever the URL points;
* ``doc_type``: the document type it describes (:mod:`templatize.doctypes`);
* ``k8s``: the ``apiVersion`` and ``kind`` of a Kubernetes resource.

Schemas are gzipped JSON, read on first use and cached. Nothing is downloaded at
runtime; ``scripts/fetch-schemas.py`` (re)makes the files.
"""

from __future__ import annotations

import gzip
import json
import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Entry:
    id: str
    title: str
    file: str
    names: tuple[str, ...]
    source: str
    license: str
    doc_type: str | None = None
    k8s: tuple[str, str] | None = None
    """(apiVersion, kind) of a Kubernetes resource."""


@cache
def entries() -> tuple[Entry, ...]:
    manifest = json.loads((HERE / "manifest.json").read_text("utf-8"))
    out = []
    for e in manifest["schemas"]:
        k8s = e.get("k8s")
        out.append(
            Entry(
                id=e["id"],
                title=e["title"],
                file=e["file"],
                names=tuple(n.lower() for n in e["names"]),
                source=e["source"],
                license=e["license"],
                doc_type=e.get("doc_type"),
                k8s=(k8s["apiVersion"], k8s["kind"]) if k8s else None,
            )
        )
    return tuple(out)


@cache
def load(entry_id: str) -> dict[str, Any]:
    entry = next(e for e in entries() if e.id == entry_id)
    data = json.loads(gzip.decompress((HERE / entry.file).read_bytes()))
    if not isinstance(data, dict):
        raise ValueError(f"catalogue schema {entry_id} is not an object")
    return data


_STEM = re.compile(r"([^/]+?)(?:\.schema)?(?:\.json)?$")


def for_url(url: str) -> Entry | None:
    """The bundled schema a ``$schema`` URL points at, by its file name."""
    path = url.split("#", 1)[0].split("?", 1)[0].rstrip("/")
    m = _STEM.search(path)
    if m is None:
        return None
    stem = m.group(1).lower()
    return next((e for e in entries() if stem in e.names), None)


def for_type(doc_type: str) -> Entry | None:
    """The bundled schema of a (non-Kubernetes) document type."""
    return next((e for e in entries() if e.doc_type == doc_type and e.k8s is None), None)


def for_k8s(api_version: str, kind: str) -> Entry | None:
    return next((e for e in entries() if e.k8s == (api_version, kind)), None)
