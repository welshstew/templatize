"""Offline verification against a running stack (stdlib only).

    python -m templatize_api.offline_check --base-url http://app:8000 --fixtures /fixtures

1. Proves the network is isolated: outbound connections and DNS must fail. If the
   internet is reachable this is not an offline test, so it fails.
2. Every fixture (with variants where present) goes through
   /suggest -> /build -> round-trip, /render must reproduce the input, and
   /download must return a complete zip. Kubernetes fixtures also go through
   /chart -> /chart/render -> /chart/download (Helm chart, round trip per variant).
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
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
    suggested = json.loads(body)
    cands = suggested["candidates"]
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
    notes = [f"{len(variants)} variant(s)"] if variants else []
    if suggested.get("profile", {}).get("helm_suggested"):
        err = check_chart(base, text, variants, cands)
        if err:
            return f"Helm chart: {err}"
        notes.append("Helm chart")
    return f"ok:{', '.join(notes)}" if notes else None


def check_chart(base: str, text: str, variants: list[str], cands: list[Any]) -> str | None:
    """/chart round-trips (input and every variant), /chart/render reproduces the
    input, /chart/download returns a chart directory."""
    specs = [
        {
            "name": c["name"],
            "spans": c["spans"],
            "values_path": c.get("values_path"),
            "variant_values": c.get("variant_values"),
        }
        for c in cands
    ]
    payload = {"text": text, "variants": variants, "variables": specs}
    status, body, _ = _post(base, "/chart", payload)
    if status != 200:
        return f"/chart -> {status}: {body[:200]!r}"
    chart = json.loads(body)
    if not chart["ok"]:
        return f"round-trip failed: {[r for r in chart['roundtrips'] if not r['ok']]}"
    files = {f["path"]: f["content"] for f in chart["files"]}
    templates = [{"path": p, "content": c} for p, c in files.items() if p.startswith("templates/")]
    status, body, _ = _post(
        base, "/chart/render", {"templates": templates, "values_yaml": files["values.yaml"]}
    )
    rendered = json.loads(body)
    if status != 200 or not rendered["ok"]:
        return f"/chart/render failed: {rendered.get('error')}"
    if "".join(f["content"] for f in rendered["files"]) != text:
        return "/chart/render did not reproduce the input"
    status, body, headers = _post(base, "/chart/download", payload)
    if status != 200 or headers.get("x-roundtrip", headers.get("X-Roundtrip")) != "pass":
        return f"/chart/download -> {status}"
    name = chart["chart_name"]
    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        names = set(zf.namelist())
    want = {f"{name}/Chart.yaml", f"{name}/values.yaml", *(f"{name}/{p}" for p in files)}
    if not want <= names:
        return f"chart zip is missing {sorted(want - names)}"
    return check_scaffold(base, payload)


def check_scaffold(base: str, payload: dict[str, Any]) -> str | None:
    """helm create style: when the app has helm, coverage must be computed (offline)."""
    info = _get_json(base, "/info")
    status, body, _ = _post(base, "/chart", {**payload, "style": "scaffold"})
    if status == 422 and b"exactly one Deployment" in body:
        return None  # not a single-application input
    if status != 200:
        return f"/chart (scaffold) -> {status}: {body[:200]!r}"
    chart = json.loads(body)
    for rep in chart["coverage"]:
        if info.get("helm") and (not rep["available"] or rep["error"]):
            return f"scaffold coverage failed: {rep['error']}"
    return None


# Same rules as frontend/scripts/check-no-external-urls.mjs.
_URL_PATTERNS = [
    re.compile(r"\bhttps?://[^\s\"'`)<>\\]+"),
    re.compile(r"(?:src|href)\s*=\s*[\"']//[^\"']+"),
    re.compile(r"url\(\s*[\"']?//[^)]+\)"),
    re.compile(r"@import\s+(?:url\()?[\"']?(?:https?:)?//"),
]
_URL_ALLOWED = re.compile(
    r"^https?://www\.w3\.org/((1999|2000)/(xhtml|svg|xlink)|1998/Math/MathML|XML/1998/namespace)$"
    # RFC 2606 documentation domains: text inside the built-in sample documents.
    r"|^https?://(?:[^@/\s]+@)?(?:[a-z0-9-]+\.)*example\.(?:com|net|org)(?:[:/?#]|$)"
)
_ASSET_REF = re.compile(r"(?:src|href)=\"(/[^\"]+)\"")


def _external_refs(text: str) -> list[str]:
    return [
        m.group(0)
        for pat in _URL_PATTERNS
        for m in pat.finditer(text)
        if not _URL_ALLOWED.match(m.group(0))
    ]


def check_ui(base: str) -> str | None:
    """The single-page app is served, loads only same-origin assets, and those assets
    reference nothing external. Returns an error message or None."""
    try:
        with urllib.request.urlopen(base + "/", timeout=10) as resp:
            html = resp.read().decode("utf-8")
            csp = resp.headers.get("content-security-policy", "")
    except urllib.error.HTTPError as exc:
        return f"GET / -> {exc.code} (frontend not bundled?)"
    if "default-src 'self'" not in csp:
        return "index.html is served without the same-origin CSP"
    problems = _external_refs(html)
    assets = _ASSET_REF.findall(html)
    if not any(a.endswith(".js") for a in assets):
        return "index.html references no JavaScript bundle"
    for path in assets:
        try:
            with urllib.request.urlopen(base + path, timeout=10) as resp:
                problems += [f"{path}: {u}" for u in _external_refs(resp.read().decode("utf-8"))]
        except urllib.error.HTTPError as exc:
            return f"GET {path} -> {exc.code}"
    if problems:
        return "external references: " + ", ".join(problems)
    return None


def _mcp_rpc(base: str, method: str, params: dict[str, Any], rid: int) -> dict[str, Any]:
    body = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
    req = urllib.request.Request(
        base + "/mcp",
        data=json.dumps(body).encode(),
        headers={
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        payload: dict[str, Any] = json.loads(resp.read())
    if "error" in payload:
        raise RuntimeError(f"{method}: {payload['error']}")
    result: dict[str, Any] = payload["result"]
    return result


def check_mcp(base: str, fixture: Path) -> str | None:
    """MCP over streamable HTTP: list tools, then generate a template for a fixture and
    require a passing round trip. Returns an error message or None."""
    try:
        _mcp_rpc(
            base,
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "offline-check", "version": "1"},
            },
            1,
        )
        listed = {t["name"] for t in _mcp_rpc(base, "tools/list", {}, 2)["tools"]}
        expected = {
            "suggest_parameters",
            "generate_template",
            "generate_helm_chart",
            "render_template",
            "validate_template",
        }
        if listed != expected:
            return f"unexpected tools {sorted(listed)}"
        text = fixture.read_bytes().decode("utf-8")
        res = _mcp_rpc(
            base, "tools/call", {"name": "generate_template", "arguments": {"text": text}}, 3
        )
    except (OSError, RuntimeError, KeyError, ValueError) as exc:
        return f"{type(exc).__name__}: {exc}"
    if res.get("isError"):
        return f"generate_template failed: {res.get('content')}"
    structured = res.get("structuredContent") or {}
    if not structured.get("roundtrip", {}).get("ok"):
        return f"round-trip failed via MCP for {fixture.name}"
    return None


def _get_json(base: str, path: str) -> Any:
    with urllib.request.urlopen(base + path, timeout=10) as resp:
        return json.loads(resp.read())


def check_llm(base: str, fixture: Path, expect: bool, wait: float = 900.0) -> tuple[bool, str]:
    """Without ``expect``: /suggest/llm must degrade gracefully (200, available=false is
    fine). With ``expect``: wait for the model to load, run a real review, merge it and
    require the round trip to still pass. Returns (ok, message)."""
    text = fixture.read_bytes().decode("utf-8")
    if expect:
        deadline = time.monotonic() + wait
        while not _get_json(base, "/info")["optional_detectors"]["llm"]:
            if time.monotonic() > deadline:
                return False, f"LLM did not become available within {wait:.0f}s"
            time.sleep(5)
    status, body, _ = _post(base, "/suggest/llm", {"text": text, "variants": []})
    if status != 200:
        return False, f"/suggest/llm -> {status}"
    res = json.loads(body)
    if not expect:
        state = "available" if res["available"] else f"unavailable ({res.get('detail')})"
        return True, f"/suggest/llm degrades gracefully: {state}"
    if not res["available"] or res["review"] is None:
        return False, f"LLM review unavailable: {res.get('detail')}"

    from templatize.build import build, specs_from_candidates
    from templatize.detectors.llm import LlmReview, apply_review
    from templatize.suggest import suggest

    review = LlmReview.model_validate(res["review"])
    merged = apply_review(suggest(text).candidates, review)
    result = build(text, specs_from_candidates(merged))
    if not result.roundtrip.ok:
        return False, "round-trip failed after merging the LLM review"
    return True, (
        f"LLM review: {len(review.renames)} renamed, {len(review.fixed)} flagged fixed, "
        f"{len(review.additions)} added, {review.discarded} discarded; round-trip ok"
    )


def check_ner(base: str, fixture: Path, wait: float = 120.0) -> tuple[bool, str]:
    """The NER service must come up and contribute candidates to /suggest, and the
    merged suggestions must still round-trip."""
    deadline = time.monotonic() + wait
    while not _get_json(base, "/info")["optional_detectors"]["ner"]:
        if time.monotonic() > deadline:
            return False, f"NER did not become available within {wait:.0f}s"
        time.sleep(2)
    text = fixture.read_bytes().decode("utf-8")
    status, body, _ = _post(base, "/suggest", {"text": text, "variants": []})
    if status != 200:
        return False, f"/suggest -> {status}"
    cands = json.loads(body)["candidates"]
    ner = [c for c in cands if "ner" in c["sources"]]
    if not ner:
        return False, "no NER candidates in /suggest"
    specs = [{"name": c["name"], "spans": c["spans"], "type": c["type"]} for c in cands]
    status, body, _ = _post(base, "/build", {"text": text, "variables": specs})
    if status != 200 or not json.loads(body)["roundtrip"]["ok"]:
        return False, "round-trip failed with NER candidates"
    return True, f"NER contributes {len(ner)} candidate(s) to /suggest; round-trip ok"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    ap.add_argument("--base-url", default="http://app:8000")
    ap.add_argument("--fixtures", type=Path, default=Path("/fixtures"))
    ap.add_argument(
        "--expect-ner", action="store_true", help="require a working NER service (ner profile)"
    )
    ap.add_argument(
        "--expect-llm", action="store_true", help="require a working LLM service (llm profile)"
    )
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

    ui_error = check_ui(base)
    if ui_error:
        print(f"FAIL web UI: {ui_error}")
        return 1
    print("PASS web UI served; all assets same-origin, no external references")

    mcp_fixture = args.fixtures / "nginx.conf"
    if mcp_fixture.exists():
        mcp_error = check_mcp(base, mcp_fixture)
        if mcp_error:
            print(f"FAIL MCP over streamable HTTP: {mcp_error}")
            return 1
        print("PASS MCP over streamable HTTP: 5 tools; generate_template round-trips")

    if args.expect_ner or os.environ.get("TEMPLATIZE_OFFLINE_EXPECT_NER") == "1":
        ok, msg = check_ner(base, args.fixtures / "business_letter.txt")
        print(f"{'PASS' if ok else 'FAIL'} {msg}")
        if not ok:
            return 1

    expect_llm = args.expect_llm or os.environ.get("TEMPLATIZE_OFFLINE_EXPECT_LLM") == "1"
    llm_fixture = args.fixtures / "business_letter.txt"
    if llm_fixture.exists():
        ok, msg = check_llm(base, llm_fixture, expect_llm)
        print(f"{'PASS' if ok else 'FAIL'} {msg}")
        if not ok:
            return 1

    fixtures = sorted(p for p in args.fixtures.iterdir() if p.is_file())
    if not fixtures:
        print(f"FAIL no fixtures in {args.fixtures}")
        return 1
    failures = 0
    for path in fixtures:
        err = check_fixture(base, path)
        if err is None or err.startswith("ok:"):
            extra = f" (with {err[3:]})" if err else ""
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
