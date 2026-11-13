"""``templatize-healthcheck``: container health probe (exit 0 if /healthz answers).

A console script rather than ``python -c "..."`` because some compose/runtime
combinations (docker-compose driving podman) re-split exec-form arguments."""

from __future__ import annotations

import os
import sys
import urllib.request


def main() -> int:
    port = os.environ.get("TEMPLATIZE_PORT", "8000")
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as resp:
            return 0 if resp.status == 200 else 1
    except OSError:
        return 1


if __name__ == "__main__":
    sys.exit(main())
