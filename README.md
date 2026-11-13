# templatize (jinja-ninja)

Turn plain text (a config file, an email, a contract, a Kubernetes manifest) into a
parameterised template. The tool suggests which parts should become variables, then
produces:

1. a Jinja2 template with `{{ variable }}` placeholders,
2. a `.env` file with the extracted values,
3. a `render.py` script (jinja2 + python-dotenv) that renders the template with the values.

Every build is verified by a **round-trip check**: rendering the template with the
extracted values must reproduce the input **byte for byte**.

Everything runs offline. No external services, API keys, telemetry or update checks.

> Status: milestone 2 (core library, CLI, web API). Frontend, MCP server and optional
> NER/LLM detectors follow.

## Quick start

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```sh
uv sync --all-extras        # install (locked) dependencies, incl. the web API
make check                  # ruff + mypy --strict + pytest

uv run templatize suggest tests/fixtures/nginx.conf
uv run templatize build tests/fixtures/nginx.conf --accept-all -o out/
cd out && uv run render.py > rendered.conf      # identical to the input
```

### CLI

```text
templatize suggest FILE [--variant FILE]... [--json] [--min-confidence X]
templatize build   FILE [--variant FILE]... (--accept-all | --vars vars.json)
                   [--min-confidence X] [-o DIR] [--zip bundle.zip]
                   [--print template|env|script] [--json]
```

- `--variant` adds another example of the same document (up to 3). The regions
  that differ between examples become very-high-confidence suggestions.
- `--vars` takes a JSON list of variable specs:
  `[{"name": "db_host", "spans": [{"start": 10, "end": 18}]}]`.
- `build` exits with code 2 if the round-trip check fails.

### Web API

```sh
make serve                      # http://127.0.0.1:8000 (uv run templatize-api)
```

| Endpoint | Body | Returns |
|---|---|---|
| `POST /suggest` | `{text, variants?: [≤3]}` | `{candidates, notices}` (spans in code points) |
| `POST /build` | `{text, variables: [{name, spans, type?, value?}], engine?}` | `{template, env, runner, variables, roundtrip}` |
| `POST /render` | `{template, values: {name\|ENV_KEY: value}, newline?}` | `{ok, output, error}`: errors are reported, not raised |
| `POST /download` | same as `/build` | `application/zip` (`template.j2`, `.env`, `render.py`, `README.md`); header `x-roundtrip: pass\|fail` |
| `GET /healthz`, `GET /info` | | health; version, engines, detectors, limits |

The OpenAPI schema is at `/openapi.json`. Swagger UI and ReDoc are disabled because
they load assets from a CDN.

`/download` is a POST and not the GET in the original brief. The server keeps nothing
between requests, so the request has to carry everything needed to build the zip.

Hardening:
- Request body limit (413).
- Strict same-origin CSP: the browser can't fetch anything from the internet.
- `Cache-Control: no-store` on API responses.
- `/render` uses a sandboxed Jinja environment that also bounds string/list repetition,
  exponents and output size (10M characters).

Configuration (environment variables):

| Variable | Default | |
|---|---|---|
| `TEMPLATIZE_HOST` / `TEMPLATIZE_PORT` | `127.0.0.1` / `8000` | the container uses `0.0.0.0` |
| `TEMPLATIZE_ACCESS_LOG` | `0` | method/path/status only; bodies are never logged |
| `TEMPLATIZE_MAX_BODY_BYTES` | ~25 MB | 4 documents × 1M chars × JSON escaping |
| `TEMPLATIZE_NER_URL`, `TEMPLATIZE_LLM_URL` | empty | optional services (later milestones) |

### Containers (offline)

```sh
make up               # docker compose up: app on 127.0.0.1:8000 (TEMPLATIZE_BIND/PORT to change)
make down
make offline-test     # full stack on an internal-only network, see below
make offline-test-cli # CLI only: single container, --network none
make image-multiarch  # docker buildx, linux/amd64 + linux/arm64
```

The Makefile uses docker if it's installed and otherwise podman (`CONTAINER=podman`
to force it).

The image is built online (dependencies from `uv.lock`) and runs offline:
- slim base;
- non-root user (uid 10001);
- read-only root filesystem with a small `/tmp` tmpfs;
- all capabilities dropped and `no-new-privileges` set;
- health check via `templatize-healthcheck`.

**Offline verification.** `make offline-test` brings up the stack with
`compose.offline.yaml`, which makes the compose network `internal` (no route out). A
checker container then:
1. proves that outbound TCP and DNS fail (if the internet is reachable, the test fails);
2. waits for the app to be healthy;
3. runs every fixture through `/suggest` → `/build` (round-trip must pass) →
   `/render` (must reproduce the input) → `/download` (zip must be complete).

This must pass before a milestone counts as done.

## Architecture

```text
             ┌────────────── templatize (core library, no web deps) ──────────────┐
 text ──►    │ detectors/            merge.py           ir.py          emitters/  │
 variants ─► │  regex, key_value ──► unify, resolve ──► TemplateIR ──► jinja2 ... │──► template
             │  repetition, diff     overlaps, names    (validated)       │       │    .env
             │  (ner, llm: later)                                         ▼       │    render.py
             │                                         roundtrip.py ◄── render    │    PASS/FAIL
             └────────────────────────────────────────────────────────────────────┘
                  ▲ CLI         ▲ templatize_api (FastAPI)   ▲ MCP server (milestone 4)
```

| Module | Role |
|---|---|
| `ir.py` | IR models (`Span`, `Candidate`, `Variable`, `TemplateIR`) and their invariants |
| `detectors/` | Produce scored `Candidate`s. Never emit template syntax |
| `merge.py` | Unify same-value candidates, resolve overlaps, de-duplicate names |
| `suggest.py` | Runs detectors and merges them, enforcing input limits |
| `build.py` | Variable specs → IR → template, `.env`, runner and round-trip result |
| `emitters/` | Engine-specific code: IR → template, plus a renderer for verification |
| `envfile.py` | `.env` serialisation that survives python-dotenv exactly |
| `roundtrip.py` | Renders through the real `.env` serialise/parse path and compares bytes |
| `bundle.py` | Deterministic zip with template, `.env`, `render.py` and a README |

### The IR

```python
class Span:      start: int; end: int            # half-open, Unicode code points (Python str indices)
class Variable:  name; type: VarType; value: str; spans: list[Span]; sources; confidence; description
class TemplateIR: ir_version = 1; text: str; variables: list[Variable]
class Candidate: id; name; name_origin; type; value; spans; sources; detectors; confidence; reason;
                 variant_values   # what this region contains in each variant document
```

Invariants (checked on construction):
- `text[span] == variable.value` for every span.
- No spans overlap.
- Names are unique lowercase snake_case and not Jinja/Python reserved words.
- `.env` keys are `name.upper()`.

Values are always the exact source substring. `VarType` is metadata for naming and UI
hints, so `port: 0080` stays `0080`.

> JavaScript clients index strings in UTF-16 code units, not code points. Convert at the
> API boundary.

### Detectors (v1)

| Detector | What it finds | Confidence |
|---|---|---|
| `regex` | IPv4/IPv6, CIDR, email, URL, hostname/FQDN, port, UUID, ISO/numeric/text dates, semver, paths, MAC | 0.6–0.9 |
| `key_value` | values in `key: value`, `key = value`, `KEY=value`, `"key": "value"`, `directive value;`. Names come from the key, YAML parents qualify generic keys (`database_host`), and k8s `name:`/`value:` pairs are recognised | 0.45–0.7 |
| `repetition` | identical non-trivial tokens appearing ≥2 times → one variable covering all occurrences | 0.5–0.7 |
| `diff` | regions that differ between the document and its variants | 0.95 |

**Merging.** Candidates with the same non-trivial value are unified into one
variable covering every occurrence; agreement between detectors raises confidence.
Overlaps are resolved greedily by confidence plus a small length bonus. Names come from
the best source (manual > key > LLM > context > type > generic) and are de-duplicated
with `_2`, `_3`, …

**Multi-example diffing:**
1. A line-level diff runs first, then a token-level diff inside changed blocks.
2. Change regions are widened to delimiter boundaries (`10.0.0.1`→`10.0.0.2` gives the
   whole IP) and snapped to overlapping dates, emails and URLs (`12 March 2025` stays
   whole). Commas inside numbers don't split.
3. Insertions are slid to word boundaries and widened to the enclosing word
   (`Dear Bob` vs `Dear Dr. Bob` → `Bob` / `Dr. Bob`).
4. Insertions or deletions that would need more than a one-line substitution (e.g. an added
   line) are reported as `structural_difference` notices; conditionals and loops are
   out of scope for v1.
5. Regions are unioned across all variants, and each region's value in every variant is
   reported as `variant_values`.

**Extension point.** `detectors/base.py` defines `Detector` (sync) and
`AsyncDetector` (service-backed, e.g. GLiNER or llama.cpp; must degrade gracefully).
Register new detectors in `detectors/__init__.py`.

### Jinja2 fidelity rules

- Literal `{{`, `{%`, `{#` are emitted as string expressions (`{{ "{%" }}`), not
  `{% raw %}`, which can't contain a literal `{% endraw %}`. A literal `{`
  directly before a generated `{{` is escaped too.
- Jinja normalises every newline in template data to `newline_sequence`. The emitter
  picks the document's dominant line ending and emits other line endings in mixed files
  as expressions (`{{ "\r\n" }}`). `render.py` embeds the same setting and reads the
  template with `newline=""`.
- Environment: `autoescape=False`, `keep_trailing_newline=True`, no
  `trim_blocks`/`lstrip_blocks`, `StrictUndefined`. Server-side rendering uses Jinja's
  `SandboxedEnvironment`.
- `.env` values are double-quoted with `\\ \" \n \r \t \v \f \a \b` escapes and read with
  `interpolate=False`, so `${HOME}`, `#`, quotes and CR/LF survive.

## Adding an engine emitter

1. Create `src/templatize/emitters/<engine>.py` with a class satisfying the
   `Emitter` protocol in `emitters/base.py`:
   - `engine`, `template_filename`
   - `emit(ir) -> str`: escape every literal piece so the engine outputs it verbatim,
     and preserve newlines exactly.
   - `render(template, values, newline=None) -> str`: used by the round-trip check.
     If the engine isn't available in Python (e.g. Go templates), shell out to a
     bundled binary or embed a minimal renderer. Verification isn't optional.
   - `runner_files(ir)` and `run_command()` for the downloadable bundle.
2. Register it in `EMITTERS` in `emitters/__init__.py`.
3. Run the fixture suite with the new engine (`tests/core/test_fixtures.py`).
   Every fixture must round-trip.

## Tests

`make check` runs ruff, `mypy --strict` and pytest. The suite includes:

- **Fixtures** (`tests/fixtures/`): nginx, docker-compose, business letter (with 2
  variants), JSON config, Kubernetes manifest (with a variant), literal Jinja syntax,
  `{% endraw %}`, CRLF, mixed newlines, Unicode, and no trailing newline. Each one is
  built, round-tripped, zipped, and rendered with the generated `render.py` in a
  subprocess.
- **Property tests** (Hypothesis): random text heavy in braces, newlines of all kinds,
  quotes, `$`, NUL, BOM and Unicode separators, with random variable spans, must always
  round-trip. Random config-like documents with variants go through suggest → build.

## Privacy

User text is never logged or persisted:
- The core library has no logging.
- The API keeps nothing between requests and has no access log by default. When the
  access log is enabled it records only method, path and status.
- Unhandled errors are logged by exception type and path only, because exception
  messages can quote user text.
- 422 responses omit FastAPI's default echo of the input.

Tests check this by sending a marker string through every endpoint and asserting it
never shows up in captured logs.
