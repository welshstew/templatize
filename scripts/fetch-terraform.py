"""Download a pinned Terraform release, verify its SHA-256 and extract the binary.

Test-time only: the tests run the generated main.tf (templatefile engine) with it.
Usage:

    python fetch-terraform.py <dest-dir> [arch]     # arch: amd64 | arm64 (default: this machine)

Checksums are from the release's terraform_<version>_SHA256SUMS file.
"""

from __future__ import annotations

import hashlib
import io
import platform
import sys
import urllib.request
import zipfile
from pathlib import Path

VERSION = "1.17.2"
SHA256 = {
    "amd64": "e6f16b2f0b137ee5729caa8c4e79a3bae4ee261acd198a1fcfa8584a6296605b",
    "arm64": "858e08c181a76b6b223e8c767a8e983922eebe2d062db949a2de66ce040c368c",
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
    url = (
        f"https://releases.hashicorp.com/terraform/{VERSION}/"
        f"terraform_{VERSION}_linux_{arch}.zip"
    )
    dest.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=120) as resp:  # noqa: S310 - fixed https URL
        data = resp.read()
    digest = hashlib.sha256(data).hexdigest()
    if digest != SHA256[arch]:
        print(f"checksum mismatch for {url}: {digest}", file=sys.stderr)
        return 1
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        try:
            binary = zf.read("terraform")
        except KeyError:
            print("terraform missing from the archive", file=sys.stderr)
            return 1
    target = dest / "terraform"
    target.write_bytes(binary)
    target.chmod(0o755)
    print(f"terraform v{VERSION} ({arch}) verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
