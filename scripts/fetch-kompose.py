"""Download a pinned Kompose release binary and verify its SHA-256.

Build-time only (the app image runs offline). Usage:

    python fetch-kompose.py <dest-dir> [arch]     # arch: amd64 | arm64 (default: this machine)

Checksums are from the release's SHA256_SUM file.
"""

from __future__ import annotations

import hashlib
import platform
import sys
import urllib.request
from pathlib import Path

VERSION = "1.38.0"
SHA256 = {
    "amd64": "65a6a720605bead3964e8b22d423a0763de451a236fe03de902e366cf3d9c147",
    "arm64": "24faf6212ec34d325e4724118098d34b9dd321cda2d9ca22efa0f04cdb909894",
}


def main() -> int:
    dest = Path(sys.argv[1])
    arch = (sys.argv[2] if len(sys.argv) > 2 else "") or {
        "x86_64": "amd64",
        "aarch64": "arm64",
    }.get(platform.machine(), platform.machine())
    if arch not in SHA256:
        print(f"unsupported architecture {arch!r}", file=sys.stderr)
        return 1
    url = f"https://github.com/kubernetes/kompose/releases/download/v{VERSION}/kompose-linux-{arch}"
    dest.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=120) as resp:  # noqa: S310 - fixed https URL
        data = resp.read()
    digest = hashlib.sha256(data).hexdigest()
    if digest != SHA256[arch]:
        print(f"checksum mismatch for {url}: {digest}", file=sys.stderr)
        return 1
    target = dest / "kompose"
    target.write_bytes(data)
    target.chmod(0o755)
    print(f"kompose v{VERSION} ({arch}) verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
