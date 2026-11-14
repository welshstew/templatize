"""Download a pinned Helm release, verify its SHA-256 and extract the binary.

Build-time only (the app image runs offline). Usage:

    python fetch-helm.py <dest-dir> [arch]     # arch: amd64 | arm64 (default: this machine)
"""

from __future__ import annotations

import hashlib
import platform
import sys
import tarfile
import urllib.request
from pathlib import Path

VERSION = "4.3.2"
SHA256 = {
    "amd64": "d1c0278d46beab79582d7e50f5b3dc206944ba8e577cde618513fd07daa53983",
    "arm64": "df31cb61680ef728f82c770e5280dd565617bee6b039d37e85c677288be4bbb0",
}


def main() -> int:
    dest = Path(sys.argv[1])
    arch = (sys.argv[2] if len(sys.argv) > 2 else "") or {"x86_64": "amd64", "aarch64": "arm64"}.get(
        platform.machine(), platform.machine()
    )
    if arch not in SHA256:
        print(f"unsupported architecture {arch!r}", file=sys.stderr)
        return 1
    url = f"https://get.helm.sh/helm-v{VERSION}-linux-{arch}.tar.gz"
    archive = dest / "helm.tar.gz"
    dest.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=120) as resp:  # noqa: S310 - fixed https URL
        archive.write_bytes(resp.read())
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if digest != SHA256[arch]:
        print(f"checksum mismatch for {url}: {digest}", file=sys.stderr)
        return 1
    member = f"linux-{arch}/helm"
    with tarfile.open(archive) as tf:
        src = tf.extractfile(member)
        if src is None:
            print(f"{member} missing from the archive", file=sys.stderr)
            return 1
        (dest / "helm").write_bytes(src.read())
    (dest / "helm").chmod(0o755)
    archive.unlink()
    print(f"helm v{VERSION} ({arch}) verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
