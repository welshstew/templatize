"""Offline verification against a running stack (stdlib only).

    python -m templatize_api.offline_check --base-url http://app:8000 --fixtures /fixtures

1. Proves the network is isolated: outbound connections and DNS must fail. If the
   internet is reachable this is not an offline test, so it fails.
2. Every fixture (with variants where present) goes through
   /suggest -> /build -> round-trip, /render must reproduce the input, and
   /download must return a complete zip.
"""

from __future__ import annotations

import argparse
import io
import json
import socket
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

PROBES = [("1.1.1.1", 443), ("8.8.8.8", 53), ("pypi.org", 443)]


def network_is_isolated(timeout: float = 2.0) -> tuple[bool, str]:
    for host, port in PROBES:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return False, f"outbound connection to {host}:{port} succeeded"
        except OSError:
            continue
    return True, "outbound connections and DNS blocked"


def _post(base: str, path: str, payload: dict[str, Any]) -> tuple[int, bytes, dict[str, str]]:
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode(),
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


def wait_healthy(base: str, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            with urllib.request.urlopen(base + "/healthz", timeout=2) as resp:
                if resp.status == 200:
                    return
        except OSError:
            pass
        if time.monotonic() > deadline:
            raise SystemExit(f"app at {base} did not become healthy")
        time.sleep(0.5)


def check_fixture(base: str, path: Path) -> str | None:
    """Return an error message, or None on success."""
    text = path.read_bytes().decode("utf-8")
    stem, _, suffix = path.name.partition(".")
    variants = [
        p.read_bytes().decode("utf-8")
        for p in sorted((path.parent / "variants").glob(f"{stem}.v*.{suffix}"))
    ]
    status, body, _ = _post(base, "/suggest", {"text": text, "variants": variants})
    if status != 200:
        return f"/suggest -> {status}: {body[:200]!r}"
    cands = json.loads(body)["candidates"]
    if not cands:
        return "/suggest returned no candidates"
    specs = [{"name": c["name"], "spans": c["spans"], "type": c["type"]} for c in cands]

    status, body, _ = _post(base, "/build", {"text": text, "variables": specs})
    if status != 200:
        return f"/build -> {status}: {body[:200]!r}"
    built = json.loads(body)
    if not built["roundtrip"]["ok"]:
        return f"round-trip failed: {built['roundtrip']}"

    values = {v["name"]: v["value"] for v in built["variables"]}
    status, body, _ = _post(
        base, "/render", {"template": built["template"]["content"], "values": values}
    )
    rendered = json.loads(body)
    if status != 200 or not rendered["ok"] or rendered["output"] != text:
        return f"/render did not reproduce the input: {rendered.get('error')}"

    status, body, headers = _post(base, "/download", {"text": text, "variables": specs})
    if status != 200 or headers.get("x-roundtrip", headers.get("X-Roundtrip")) != "pass":
        return f"/download -> {status}"
    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        names = sorted(zf.namelist())
    if names != [".env", "README.md", "render.py", "template.j2"]:
        return f"unexpected zip contents {names}"
    return None if not variants else f"ok:{len(variants)}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    ap.add_argument("--base-url", default="http://app:8000")
    ap.add_argument("--fixtures", type=Path, default=Path("/fixtures"))
    ap.add_argument(
        "--allow-network", action="store_true", help="skip the isolation check (local debugging)"
    )
    args = ap.parse_args(argv)
    base = args.base_url.rstrip("/")

    isolated, detail = network_is_isolated()
    print(f"{'PASS' if isolated else 'FAIL'} network isolation: {detail}")
    if not isolated and not args.allow_network:
        return 1

    wait_healthy(base)
    print(f"PASS {base}/healthz")

    fixtures = sorted(p for p in args.fixtures.iterdir() if p.is_file())
    if not fixtures:
        print(f"FAIL no fixtures in {args.fixtures}")
        return 1
    failures = 0
    for path in fixtures:
        err = check_fixture(base, path)
        if err is None or err.startswith("ok:"):
            extra = f" (with {err[3:]} variant(s))" if err else ""
            print(f"PASS {path.name}{extra}")
        else:
            failures += 1
            print(f"FAIL {path.name}: {err}")
    if failures:
        print(f"{failures} fixture(s) failed")
        return 1
    print(
        f"all {len(fixtures)} fixtures pass suggest -> build -> round-trip -> render -> download offline"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
