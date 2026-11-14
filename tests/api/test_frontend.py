"""Frontend serving and the "no external URLs in the built bundle" guarantee."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from templatize_api.app import create_app
from templatize_api.settings import Settings

DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"

# Same rules as frontend/scripts/check-no-external-urls.mjs.
ALLOWED = [
    re.compile(r"^https?://www\.w3\.org/(1999|2000)/(xhtml|svg|xlink)$"),
    re.compile(r"^https?://www\.w3\.org/1998/Math/MathML$"),
    re.compile(r"^https?://www\.w3\.org/XML/1998/namespace$"),
    # RFC 2606 documentation domains, used only as text in the built-in samples.
    re.compile(r"^https?://(?:[^@/\s]+@)?(?:[a-z0-9-]+\.)*example\.(?:com|net|org)(?:[:/?#]|$)"),
]
PATTERNS = [
    re.compile(r"\bhttps?://[^\s\"'`)<>\\]+"),
    re.compile(r"(?:src|href)\s*=\s*[\"']//[^\"']+"),
    re.compile(r"url\(\s*[\"']?//[^)]+\)"),
    re.compile(r"@import\s+(?:url\()?[\"']?(?:https?:)?//"),
]
TEXT_SUFFIXES = {".html", ".js", ".mjs", ".css", ".svg", ".json", ".txt", ".webmanifest"}


def external_references(root: Path) -> list[str]:
    found = []
    for path in root.rglob("*"):
        if path.suffix not in TEXT_SUFFIXES or not path.is_file():
            continue
        text = path.read_text("utf-8", errors="replace")
        for pat in PATTERNS:
            for m in pat.finditer(text):
                if not any(a.match(m.group(0)) for a in ALLOWED):
                    found.append(f"{path.relative_to(root)}: {m.group(0)}")
    return found


@pytest.mark.skipif(not (DIST / "index.html").exists(), reason="frontend not built (make frontend)")
def test_built_frontend_has_no_external_urls() -> None:
    assert external_references(DIST) == []


def test_scanner_catches_external_references(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text(
        '<link href="https://fonts.googleapis.com/css2?family=Inter" rel="stylesheet">'
        '<script src="//cdn.example.com/x.js"></script>'
    )
    (tmp_path / "a.css").write_text(
        "@import url('https://x.io/a.css'); .x{background:url(//c.dn/i.png)}"
    )
    (tmp_path / "ok.js").write_text(
        'createElementNS("http://www.w3.org/2000/svg","svg"); "https://app.example.com/x"'
    )
    (tmp_path / "lookalike.js").write_text(
        '"https://example.com.evil.net/" "https://notexample.com/"'
    )
    found = "\n".join(external_references(tmp_path))
    for ref in (
        "fonts.googleapis.com",
        "//cdn.example.com",
        "https://x.io/a.css",
        "//c.dn/i.png",
        "example.com.evil.net",
        "notexample.com",
    ):
        assert ref in found, found
    assert "app.example.com" not in found
    assert "w3.org" not in found  # XML namespaces are not fetches


@pytest.fixture
def fake_dist(tmp_path: Path) -> Path:
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text(
        '<!doctype html><script type="module" src="/assets/app.js"></script>'
    )
    (tmp_path / "assets" / "app.js").write_text("console.log('hi')")
    (tmp_path / "favicon.svg").write_text("<svg/>")
    return tmp_path


def test_serves_spa_and_assets(fake_dist: Path) -> None:
    client = TestClient(create_app(Settings(static_dir=fake_dist)))
    index = client.get("/")
    assert index.status_code == 200 and "/assets/app.js" in index.text
    assert index.headers["cache-control"] == "no-store"
    asset = client.get("/assets/app.js")
    assert asset.status_code == 200
    assert "immutable" in asset.headers["cache-control"]
    assert "default-src 'self'" in asset.headers["content-security-policy"]
    assert client.get("/favicon.svg").status_code == 200
    # API routes still work alongside the SPA
    assert client.post("/suggest", json={"text": "a"}).status_code == 200


def test_no_frontend_configured() -> None:
    client = TestClient(create_app(Settings(static_dir=None)))
    assert client.get("/").status_code == 404


def test_static_dir_without_index(tmp_path: Path) -> None:
    client = TestClient(create_app(Settings(static_dir=tmp_path)))
    assert client.get("/").status_code == 404
