"""Docker Compose -> Kubernetes manifests with Kompose, ready for a Helm chart.

Kompose (https://kompose.io) converts each service to a Deployment, a Service for
its ports, and claims for its volumes. Two things make the result runnable rather
than just converted:

* Compose services reach each other on the project network without publishing
  ports, so Kompose creates no Service for a database that only has ``image:``.
  Before converting, services that others reference (``@db:5432`` in a URL,
  ``REDIS_HOST: cache``, ``depends_on``) get an ``expose:`` port: the one in the
  reference, else the well-known port of the image (postgres 5432, redis 6379...).
* Kompose's warnings (host paths it cannot keep, restart policies it changes) are
  returned as notes, together with the ports that were added.

Kompose runs offline with a private HOME and a timeout, like ``helm`` does here.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

KOMPOSE_TIMEOUT = 60
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
# Well-known ports of common images, for services other services talk to.
WELL_KNOWN_PORTS = {
    "postgres": 5432,
    "postgis": 5432,
    "mysql": 3306,
    "mariadb": 3306,
    "redis": 6379,
    "valkey": 6379,
    "mongo": 27017,
    "mongodb": 27017,
    "rabbitmq": 5672,
    "memcached": 11211,
    "elasticsearch": 9200,
    "opensearch": 9200,
    "minio": 9000,
    "kafka": 9092,
    "zookeeper": 2181,
    "nats": 4222,
    "etcd": 2379,
    "consul": 8500,
    "nginx": 80,
    "httpd": 80,
}


class KomposeUnavailable(RuntimeError):
    pass


class ComposeError(ValueError):
    pass


class ComposeConversion(BaseModel):
    manifests: str
    """Kubernetes manifests, one resource per ``---`` document."""
    notes: list[str] = []
    services: list[str] = []
    exposed: dict[str, int] = {}
    """Ports added so other services can reach these (service -> port)."""


def kompose_binary() -> str | None:
    """``TEMPLATIZE_KOMPOSE`` (a path, or ``0`` to disable) or ``kompose`` on PATH."""
    configured = os.environ.get("TEMPLATIZE_KOMPOSE", "").strip()
    if configured == "0":
        return None
    if configured:
        return configured if Path(configured).is_file() else None
    return shutil.which("kompose")


def compose_services(text: str) -> dict[str, Any] | None:
    """The ``services`` mapping if ``text`` is a Compose file, else None."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict) or "apiVersion" in data:
        return None
    services = data.get("services")
    if not isinstance(services, dict) or not services:
        return None
    if not all(isinstance(s, dict) and ("image" in s or "build" in s) for s in services.values()):
        return None
    return services


def _strings(node: Any) -> list[str]:
    if isinstance(node, str):
        return [node]
    if isinstance(node, Mapping):
        return [s for v in node.values() for s in _strings(v)]
    if isinstance(node, list):
        return [s for v in node for s in _strings(v)]
    return []


def _image_port(image: str) -> int | None:
    name = image.split("@")[0].rsplit("/", 1)[-1].split(":")[0].lower()
    for base, port in WELL_KNOWN_PORTS.items():
        if name == base or name.startswith(base + "-"):
            return port
    return None


def _references(services: dict[str, Any], target: str) -> tuple[bool, int | None]:
    """Is ``target`` referenced by another service, and with which port?"""
    pattern = re.compile(r"(?<![\w.-])" + re.escape(target) + r"(?::(\d{1,5}))?(?![\w.-])")
    found, port = False, None
    for name, svc in services.items():
        if name == target:
            continue
        deps = svc.get("depends_on")
        if (isinstance(deps, list) and target in deps) or (
            isinstance(deps, dict) and target in deps
        ):
            found = True
        for s in _strings(
            {k: v for k, v in svc.items() if k in ("environment", "command", "entrypoint")}
        ):
            for m in pattern.finditer(s):
                found = True
                if m.group(1) and port is None:
                    port = int(m.group(1))
    return found, port


def prepare(text: str) -> tuple[str, dict[str, int], list[str]]:
    """The compose file with ``expose:`` added where needed, the ports added, notes."""
    services = compose_services(text)
    if services is None:
        raise ComposeError("not a Docker Compose file: it needs a services: mapping with images")
    data = yaml.safe_load(text)
    exposed: dict[str, int] = {}
    notes: list[str] = []
    for name, svc in services.items():
        if svc.get("ports") or svc.get("expose"):
            continue
        referenced, port = _references(services, name)
        if not referenced:
            continue
        port = port or _image_port(str(svc.get("image", "")))
        if port is None:
            notes.append(
                f"service {name!r} is used by other services but has no ports: add "
                "expose: to it so the chart gets a Service for it"
            )
            continue
        data["services"][name]["expose"] = [str(port)]
        exposed[name] = port
    for name, svc in services.items():
        if "build" in svc and "image" not in svc:
            notes.append(
                f"service {name!r} is built from source: push an image and set image: for the chart"
            )
    if exposed:
        notes.append(
            "added Services so the services can reach each other: "
            + ", ".join(f"{n} (port {p})" for n, p in exposed.items())
        )
    return yaml.safe_dump(data, sort_keys=False), exposed, notes


def convert(text: str) -> ComposeConversion:
    """Kubernetes manifests for a Compose file (see the module docstring)."""
    kompose = kompose_binary()
    if kompose is None:
        raise KomposeUnavailable("kompose is not installed")
    prepared, exposed, notes = prepare(text)
    with tempfile.TemporaryDirectory(prefix="templatize-kompose-") as tmp:
        src = Path(tmp) / "compose.yaml"
        src.write_text(prepared, encoding="utf-8")
        env = {"PATH": os.environ.get("PATH", ""), "HOME": tmp}
        try:
            proc = subprocess.run(
                [kompose, "convert", "-f", str(src), "--stdout", "--with-kompose-annotation=false"],
                capture_output=True,
                text=True,
                timeout=KOMPOSE_TIMEOUT,
                cwd=tmp,
                env=env,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise ComposeError("kompose timed out") from None
    stderr = _ANSI.sub("", proc.stderr).replace(str(src), "compose.yaml").replace(tmp, ".")
    if proc.returncode != 0 or not proc.stdout.strip():
        raise ComposeError(f"kompose could not convert the file: {stderr.strip()[:2000]}")
    for line in stderr.splitlines():
        line = line.strip()
        # Kompose stats every volume source as a host path, even named volumes;
        # its "isn't supported" warning for real host paths is the useful one.
        if line.startswith("WARN") and "failed to check if the directory is empty" not in line:
            notes.append("kompose: " + line.removeprefix("WARN").strip())
    manifests = proc.stdout.lstrip("-\n")  # starts with "---"
    services = list((compose_services(text) or {}).keys())
    return ComposeConversion(manifests=manifests, notes=notes, services=services, exposed=exposed)


def compose_profile(text: str) -> Any:
    """A DocumentProfile for a Compose file (a chart can be built with Kompose)."""
    from templatize.k8s import DocumentProfile

    services = compose_services(text)
    if services is None:
        return None
    available = kompose_binary() is not None
    return DocumentProfile(
        format="compose",
        services=list(services),
        helm_suggested=available,
        reason=None if available else "converting Docker Compose needs kompose on the server",
    )
