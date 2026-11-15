#!/usr/bin/env bash
# The Playwright suite against a fresh server: the built UI (frontend/dist) served from
# source, heuristic detectors only. Used by CI; `make e2e` runs against a stack you started.
#
#   scripts/e2e.sh                     # system Google Chrome
#   E2E_CHANNEL= scripts/e2e.sh        # Playwright's bundled Chromium (npx playwright install chromium)
set -euo pipefail
cd "$(dirname "$0")/.."

port="${E2E_PORT:-8765}"
if curl -s -o /dev/null "http://127.0.0.1:$port/"; then
  echo "port $port is already in use; set E2E_PORT to a free port" >&2
  exit 1
fi
log="$(mktemp)"
TEMPLATIZE_STATIC_DIR=frontend/dist TEMPLATIZE_PORT="$port" uv run templatize-api >"$log" 2>&1 &
server=$!
trap 'kill "$server" 2>/dev/null || true; rm -f "$log"' EXIT

for _ in $(seq 60); do
  curl -sf "http://127.0.0.1:$port/healthz" >/dev/null && break
  if ! kill -0 "$server" 2>/dev/null; then
    cat "$log"
    echo "server exited before becoming healthy" >&2
    exit 1
  fi
  sleep 1
done
curl -sf "http://127.0.0.1:$port/healthz" >/dev/null || { cat "$log"; echo "server not healthy after 60 s" >&2; exit 1; }

cd frontend
TEMPLATIZE_URL="http://127.0.0.1:$port" npx playwright test "$@"
