"""Download the pinned JSON Schemas of the bundled catalogue, verify them, store them.

The catalogue (src/templatize/catalog/) ships with the package, so the tool never
fetches a schema at runtime. This script is how the files are (re)made:

    python scripts/fetch-schemas.py            # check every stored file against the manifest
    python scripts/fetch-schemas.py --update   # download the pinned sources, write .json.gz,
                                               # record their SHA-256 in the manifest

Sources are pinned to a commit or release tag; the manifest records the SHA-256 of
each downloaded schema, and a download whose digest differs from a recorded one is
refused (``--update`` only fills in missing digests).
"""

from __future__ import annotations

import gzip
import hashlib
import json
import sys
import urllib.request
from pathlib import Path

CATALOG = Path(__file__).resolve().parents[1] / "src" / "templatize" / "catalog"
MANIFEST = CATALOG / "manifest.json"


def _download(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as resp:  # noqa: S310 - pinned https URL
        data: bytes = resp.read()
    json.loads(data)  # must be JSON
    return data


def main() -> int:
    update = "--update" in sys.argv[1:]
    manifest = json.loads(MANIFEST.read_text("utf-8"))
    bad = 0
    for entry in manifest["schemas"]:
        path = CATALOG / entry["file"]
        if update:
            data = _download(entry["source"])
            digest = hashlib.sha256(data).hexdigest()
            if entry.get("sha256") and entry["sha256"] != digest:
                print(f"{entry['id']}: digest changed upstream ({digest}); refusing", file=sys.stderr)
                bad += 1
                continue
            entry["sha256"] = digest
            # Compact JSON, then gzip with a fixed mtime: reproducible files.
            compact = json.dumps(json.loads(data), separators=(",", ":"), ensure_ascii=False)
            stored = gzip.compress(compact.encode("utf-8"), mtime=0)
            path.write_bytes(stored)
            entry["file_sha256"] = hashlib.sha256(stored).hexdigest()
            print(f"{entry['id']}: {len(data):>8} bytes -> {len(stored):>7} gz")
        elif not path.is_file():
            print(f"{entry['id']}: missing {path.name}", file=sys.stderr)
            bad += 1
        elif hashlib.sha256(path.read_bytes()).hexdigest() != entry.get("file_sha256"):
            print(f"{entry['id']}: {path.name} does not match the manifest", file=sys.stderr)
            bad += 1
    if update:
        MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n", "utf-8")
    print(f"{len(manifest['schemas'])} schemas, {bad} problem(s)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
