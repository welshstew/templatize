# templatize (jinja-ninja)

Turn plain text (a config file, an email, a contract, a Kubernetes manifest) into a
parameterised template. The tool suggests which parts should become variables, then
produces:

1. a Jinja2 template with `{{ variable }}` placeholders,
2. a `.env` file with the extracted values,
3. a `render.py` script (jinja2 + python-dotenv) that renders the template with the values.

Or, with `--engine` (or the engine choice in the UI), an **envsubst** template
(`${KEY}`, `values.env`, `render.sh`) or a **Terraform `templatefile()`** template
(`template.tftpl`, `terraform.tfvars`, `main.tf`); see [Engines](#engines).

Every build is verified by a **round-trip check**: rendering the template with the
extracted values must reproduce the input **byte for byte**.

Everything runs offline. No external services, API keys, telemetry or update checks.

> Status: all milestones complete: core library, CLI, web API, web UI, MCP server, and
> optional NER (GLiNER) and LLM (llama.cpp) detectors.

## Quick start

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/). Building the UI also needs Node.js 22+ (24 recommended); the container image builds it for you.

```sh
uv sync --all-extras        # install (locked) dependencies, incl. web API and MCP extras
make check                  # ruff + mypy --strict + pytest

uv run templatize suggest tests/fixtures/nginx.conf
uv run templatize build tests/fixtures/nginx.conf --accept-all -o out/
cd out && uv run render.py > rendered.conf      # identical to the input
```

### CLI

```text
templatize suggest FILE [--variant FILE]... [--json] [--min-confidence X]
templatize build   FILE [--variant FILE]... (--accept-all | --vars vars.json)
                   [--engine jinja2|envsubst|terraform] [--min-confidence X] [-o DIR] [--zip bundle.zip]
                   [--print template|env|script] [--json]
```

- `--variant` adds another example of the same document (up to 3). The regions
  that differ between examples become very-high-confidence suggestions.
- `--vars` takes a JSON list of variable specs:
  `[{"name": "db_host", "spans": [{"start": 10, "end": 18}]}]`.
- `--engine` picks the template engine (see [Engines](#engines)); its notes go to stderr.
- `build` exits with code 2 if the round-trip check fails.

### Web UI

```sh
make serve          # builds frontend/ and serves UI + API at http://127.0.0.1:8000
make frontend-dev   # Vite dev server with hot reload (run `make serve` alongside; API is proxied)
```

1. Paste a document and press **Analyze**. You can also **add examples**: up to 3 other
   versions of the same document. Regions that differ between them are suggested with
   high confidence.
2. Suggestions are highlighted by source: from the examples, key/value, pattern, or
   repetition. Accept or reject them one at a time or all at once.
3. **Select any text** in the document to turn it into a variable. You can apply it to
   all occurrences, and selections that overlap an existing variable are refused.
4. Rename variables and set their types in the sidebar. Editing a variable's *value*
   drives the **live preview**.
5. The **round-trip badge** in the header shows PASS/FAIL for the current variable set.
   **Download .zip** gives you `template.j2`, `.env`, `render.py` and a README; the
   **template engine** choice in the output panel switches to envsubst or Terraform
   `templatefile()` (see [Engines](#engines)).

**Load sample…** has a document for each kind of input. Some open with another engine
selected, to show it off:
- **cloud-init `user-data`** (Terraform): packages, and users with their SSH keys, as
  nested loops.
- **HAProxy backend pool** (Terraform): one `server` line per backend, as a loop.
- **nginx Docker image template** (envsubst): nginx's own `$host` stays untouched.

Accept a sample's lists ("Accept list …") to see them as loops.

Three samples show HashiCorp Vault:
- **A Vault policy:** the paths are the values, named after their mount
  (`secret_data_path`); capabilities are permission keywords.
- **A Vault Agent config:** it fetches a database secret into a file. The agent's own
  `{{ with secret … }}` template is kept as literal text.
- **Terraform reading a secret from Vault:** block labels and `data.…` references are
  never values.

The frontend is Preact + TypeScript, built with Vite, with no other runtime dependencies
(about 37 KB of JS, 14 KB gzipped) and system fonts only. `npm run build` type-checks,
bundles, and fails if the output references any external URL. `npm test` runs the
client-side logic tests (selection, offsets, naming) with Node's built-in test runner.

Offsets: the API uses Unicode code points and the browser uses UTF-16 code units.
`frontend/src/offsets.ts` converts between them, so selections after emoji or other
astral characters map exactly.

### Web API

```sh
TEMPLATIZE_STATIC_DIR=frontend/dist uv run templatize-api   # what `make serve` runs
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
| `TEMPLATIZE_STATIC_DIR` | unset (`/app/static` in the image) | built UI to serve at `/` |
| `TEMPLATIZE_MCP_HTTP` | `1` | serve MCP streamable HTTP at `/mcp` (needs the `mcp` extra) |
| `TEMPLATIZE_MCP_ALLOWED_HOSTS` | (none) | extra allowed `Host` values for `/mcp`, comma-separated, e.g. `myhost.ts.net:*` |
| `TEMPLATIZE_NER_URL`, `TEMPLATIZE_LLM_URL` | empty | optional services (later milestones) |

### Optional NER detector (GLiNER on ONNX Runtime, CPU)

A small named-entity model finds values that regexes can't: person and company names,
locations, street addresses, phone numbers, money amounts, job titles and products.
Labels map to variable names, e.g. `person` → `person_name` and `money` → `amount`. It
is fast (well under a second for the business-letter fixture on CPU), so it runs inside
`/suggest` itself, and its candidates are merged like any other detector's (labelled
**NER** in the UI).

```sh
make up-ner    # app + ner (COMPOSE_PROFILES=ner docker compose up -d --build)
```

**Model:** [GLiNER small v2.1](https://huggingface.co/urchade/gliner_small-v2.1)
(Apache-2.0), using the full-precision (fp32) ONNX export from
[`onnx-community/gliner_small-v2.1`](https://huggingface.co/onnx-community/gliner_small-v2.1)
pinned to revision `8142fb00`. That repo has no licence field of its own; it's a format
conversion of the Apache-2.0 model. `scripts/download-ner-model.sh` downloads the model
**at image build time** and checks each file's SHA-256, so the image runs offline. To use
another span-mode GLiNER model, mount it and set `TEMPLATIZE_NER_MODEL_DIR`.

**ONNX Runtime instead of PyTorch.** The `gliner` package needs PyTorch even for ONNX
inference. `templatize_ner/gliner_onnx.py` therefore reimplements GLiNER's span-mode
pre- and post-processing on `onnxruntime` + `tokenizers` + `numpy` (about 200 lines):
- the word splitter;
- the `<<ENT>> label … <<SEP>>` prompt;
- the first-subtoken word mask;
- span enumeration and masking;
- sigmoid, threshold and greedy non-overlapping decoding;
- overlapping windows for long texts.

The trade-off:

| | ONNX Runtime (this) | PyTorch (`gliner` package) |
|---|---|---|
| Image | **~960 MB** (611 MB model, 63 MB onnxruntime, 67 MB numpy, base OS) | ~2.5 GB (CPU torch wheel ~700 MB+, transformers, the same 611 MB of weights) |
| Startup / latency | ~1 s load, well under 1 s per short document | slower start, similar inference |
| Risk | our own pre/post-processing could drift from upstream | reference implementation |

**Why fp32 and not the 3× smaller int8 export.** In the same repo, int8 is measurably
worse. Against fp32 it changes the detected spans in 11 of 15 test cases, with a mean
score shift of 0.14. On the business letter it misses `Contoso Pharmaceuticals` and
`Head of Sales` and mislabels the street address. It's also CPU-dependent: ARM and x86
runners gave different results, which is how CI caught it. fp32 behaves the same on
both.

The reimplementation risk is covered by a **parity test**. `scripts/dev/gliner_golden.py` runs the
reference `gliner` 0.2.30, in a throwaway environment with torch, on the **same ONNX
file** across all fixtures plus edge cases (hyphenated and apostrophe names, phone and
address, emoji and CJK). It stores the results in `tests/ner/golden.json`. Our
implementation matches them: identical spans and labels, with scores equal within
0.005. A span within 0.02 of the threshold may flip between CPUs and is tolerated. To regenerate:
`uv run --no-project --python 3.12 --with gliner==0.2.30 --with onnxruntime scripts/dev/gliner_golden.py MODEL_DIR > tests/ner/golden.json`.

ONNX Runtime ships a telemetry component, which is turned off: `ORT_DISABLE_TELEMETRY=1`
plus `onnxruntime.disable_telemetry_events()`, and a test checks both. The service
(`templatize-ner`, stdlib HTTP) never logs requests and reports only the type of an
inference error. It publishes no host port and runs as non-root with a read-only root
filesystem.

| Variable | Default | |
|---|---|---|
| `TEMPLATIZE_NER_URL` | `http://ner:8081` (compose) / unset | absent or unreachable: no NER candidates, nothing else changes |
| `TEMPLATIZE_NER_TIMEOUT` | `30` | seconds |
| `TEMPLATIZE_NER_MAX_CHARS` | `200000` | longer documents skip NER |
| `TEMPLATIZE_NER_THREADS` | auto | ONNX Runtime intra-op threads |

RAM: about 1 GB for the NER service with the fp32 model.

**Limits.** GLiNER small sometimes tags short generic tokens (for example a single
lowercase word as an `organization` in config files). Its confidence (0.45 + 0.4 × score) keeps
it below the strong heuristics. The LLM review, if enabled, sees NER candidates too.

### Optional LLM detector (llama.cpp, fully local)

A local model reviews the heuristic suggestions. It runs on CPU by default, or on a GPU
with an override file. In the brief's priority order, it:
1. gives candidates descriptive names (`ip_address` → `backend_ip_1`, `date_2` →
   `letter_date`);
2. classifies each candidate as a variable or as fixed text;
3. finds parameters the heuristics miss: person and company names, order numbers,
   amounts, quantities (`PO-55821` → `order_reference_number`,
   `GBP 18,450.00` → `total_amount`, `Ravi Patel` → `account_manager_name`).

**The model never writes the template.** It returns JSON only, and llama.cpp constrains
decoding to a JSON schema (`response_format.json_schema` becomes a grammar), so the
output always parses. Missed variables come back as an exact substring plus an occurrence
index, never as character offsets. The code locates each substring and discards any that
don't occur in the input, merges the rest with the heuristic candidates, and the
round-trip check runs as usual. In the UI, heuristic suggestions appear instantly. The
LLM review is merged in when it arrives, labelled **LLM**, and without overriding
anything you changed in the meantime.

**Model:** [Qwen3-4B-Instruct-2507](https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507),
Q4_K_M GGUF (~2.5 GB, Apache-2.0), from
[`unsloth/Qwen3-4B-Instruct-2507-GGUF`](https://huggingface.co/unsloth/Qwen3-4B-Instruct-2507-GGUF)
pinned to revision `a06e946b`. Why this one:
- **No reasoning step:** it never emits `<think>` blocks, so grammar-constrained JSON is
  clean. Qwen3.5-4B reasons by default.
- **Text-only:** a long-supported llama.cpp architecture, with no vision projector to
  manage.
- **Small enough for CPU:** Gemma 4 E4B is 5 GB at the same quantisation.

```sh
make model     # download to ./models and verify SHA-256 (resumable; no-op if already verified)
make up-llm    # app + llm (COMPOSE_PROFILES=llm docker compose up -d --build)
```

Or manually: download
`https://huggingface.co/unsloth/Qwen3-4B-Instruct-2507-GGUF/resolve/a06e946bb6b655725eafa393f4a9745d460374c9/Qwen3-4B-Instruct-2507-Q4_K_M.gguf`
into `./models/` and check that its SHA-256 is
`3605803b982cb64aead44f6c1b2ae36e3acdb41d8e46c8a94c6533bc4c67e597`.

**GPU** (NVIDIA; needs the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/)):

```sh
make up-llm-gpu          # app + llm on the GPU (CUDA build of llama.cpp, all layers offloaded)
make up-llm-gpu-large    # the same with Qwen3-30B-A3B-Instruct-2507 (make model-large, ~18.6 GB; ~21 GB VRAM)
```

The override file `compose.gpu.yaml` switches to the CUDA image and requests the GPU. With
Podman, podman-compose (1.1 or later) turns that request into a CDI device
(`nvidia.com/gpu=all`), so run `sudo nvidia-ctk cdi generate` once, or let the toolkit's
`nvidia-cdi-refresh` service do it. The targets pass `--force-recreate` because
podman-compose keeps existing containers when only the image or the environment changed.

**AMD or Intel GPU via Vulkan** (e.g. a Radeon RX 5700 XT):
`COMPOSE_PROFILES=llm docker compose -f compose.yaml -f compose.vulkan.yaml up -d --build`.
The host needs the GPU driver loaded and `/dev/dri` present, so the card must not be
bound to `vfio-pci` for a VM. Vulkan is used instead of ROCm because ROCm doesn't
officially support RDNA1 cards. It uses llama.cpp's `server-vulkan` build and offloads
every layer; the default model plus a 16k context needs about 5 GB of VRAM.

**LLM on another machine** (e.g. a GPU in a home server, reached over Tailscale).
`deploy/llm-host/` is a standalone compose file for that machine. It **requires** an API
key, read from a file so it doesn't show in `docker inspect` or `ps`, and an explicit
bind address, normally the machine's Tailscale IP:

```sh
# on the GPU machine
cd deploy/llm-host && cp .env.example .env        # set LLM_BIND and MODELS_DIR
../../scripts/download-model.sh "$MODELS_DIR"
openssl rand -hex 32 > llm_api_key && chmod 600 llm_api_key
docker compose up -d --build

# where the app runs
TEMPLATIZE_LLM_URL=http://<gpu-host>:8080 TEMPLATIZE_LLM_API_KEY_FILE=/path/to/llm_api_key make up
```

If that machine is off or unreachable, the app reports the LLM as unavailable and
carries on with heuristics and NER. A Tailscale ACL limiting port 8080 to the app's host
is a good extra layer.

**Your own model:** put any instruct GGUF in `./models` and set
`TEMPLATIZE_LLM_MODEL=<file>`. Or point `TEMPLATIZE_LLM_URL` at any llama.cpp server.

| Variable | Default | |
|---|---|---|
| `TEMPLATIZE_LLM_URL` | `http://llm:8080` (compose) / unset | llama.cpp server; absent or unreachable means the feature is hidden |
| `TEMPLATIZE_LLM_MODEL` | `Qwen3-4B-Instruct-2507-Q4_K_M.gguf` | file in `TEMPLATIZE_MODELS_DIR` (`./models`) |
| `TEMPLATIZE_LLM_CTX` | `16384` | context size; ~5 GB RAM in total with the default model |
| `TEMPLATIZE_LLM_THREADS` | auto | CPU threads |
| `TEMPLATIZE_LLM_TIMEOUT` | `300` | seconds per review |
| `TEMPLATIZE_LLM_API_KEY` / `TEMPLATIZE_LLM_API_KEY_FILE` | unset | Bearer key for a llama-server started with an API key (the file form is preferred) |
| `TEMPLATIZE_LLM_MAX_CHARS` | `24000` | longer documents are not sent; heuristics only |

The llm container:
- uses llama.cpp `server-b11946`, pinned;
- starts with `--offline` and `--no-slots`, so the prompt state endpoint is off;
- publishes no host port and runs with a read-only root filesystem;
- exits with a clear message (code 64) if the model file is missing, and never
  downloads anything.

llama-server logs timings only; tests check that no document text reaches either
service's logs. The CLI takes `--llm-url` too: `templatize suggest file --llm-url
http://127.0.0.1:8090`.

**Memory:** the default model with a 16k context uses about 5 GB of RAM. Make sure the
Docker or Podman VM has that free. Running a second copy, for example
`make offline-test-llm` while `make up-llm` is running, needs double, and on an 11 GB VM
the kernel OOM-kills one of them.

**Measured** on the 4-core Podman VM of an Apple Silicon Mac (CPU only): about 30 s to
load the model, then 40–110 s per review of the fixture documents. On an RTX 5090 Laptop
GPU (24 GB) the default model loads in about 2.5 s and uses about 5 GB of VRAM, and a
review takes 3–6 s. The large model (`make up-llm-gpu-large`, a mixture of experts with
about 3B parameters active per token) loads in about 3 s, uses about 19.5 GB of VRAM, and
reviews in 1.5–7 s, so it is no slower. It is noticeably better at naming: on `nginx.conf`
the 4B model proposed no renames while the 30B one proposed 13 sensible ones
(`billing_backend_ip_1`, `listen_port`, `ssl_certificate_key_path`), and on the
Kubernetes stack 7 against none. On the business letter both found the same missed
values (`PO-55821`, `GBP 18,450.00`, `Ravi Patel`). It still makes occasional mistakes,
e.g. naming an Ingress backend port `service_target_port`.

**Limitations.** The variable/fixed-text verdict from a 4B model is unreliable. An
earlier prompt flagged correctly on one run and flagged every k8s field on the next. The
prompt therefore asks for `fixed_text: true` only for format constants and defaults to
"parameter". The code also ignores the verdicts when more than half the candidates are
flagged, and a verdict only halves the confidence, never auto-rejects. In practice it
now rarely flags anything. Naming and missed-parameter detection are the useful parts.
The MCP server doesn't use the LLM, because its client already is one.

### Helm charts from Kubernetes manifests

Paste Kubernetes manifests (one resource per `---` document) and templatize recognises
them: `/suggest` returns a `profile` listing the resources, and the UI offers **Build Helm
chart**. The chart is built from the same accepted variables:

```text
storefront/
  Chart.yaml                appVersion from the image tag
  values.yaml               every variable, nested: image.tag, replicaCount, service.port, …
  values-staging.yaml       per example document: only the values that differ
  .helmignore
  templates/configmap.yaml  one template per resource, in input order
  templates/deployment.yaml
  …
```

**Kubernetes-aware suggestions.** For manifests a dedicated detector reads the YAML node
tree (exact offsets, no regex guessing) and knows what common fields mean, so suggestions
carry Helm-style `values_path`s: `replicaCount`, `image.repository` and `image.tag` (an
image is split in two; digests become `image.digest`), `image.pullPolicy`,
`resources.limits.memory`, `containerPort`, `env.<NAME>`, `service.type`/`port`/`targetPort`,
`ingress.host`/`className`/`tls.secretName`, `config.<key>` (ConfigMap data),
`secrets.<key>`, `autoscaling.minReplicas`, `persistence.size`, `namespace`. Paths are
prefixed with the resource (or container) name only when there is more than one of that
kind. `apiVersion`, `kind` and mapping keys are never suggested. With example documents
(say production and staging), each path is read from every example, so a changed tag shows
up on `image.tag` rather than on the whole image string.

**Exact, and checked twice.** Templates are plain Go `text/template` with
`{{ .Values.x }}` references; literal `{{` in a manifest is escaped. `values.yaml` writes
only canonical integers below 10^6 and `true`/`false` bare, and quotes everything else,
because Helm reads values as JSON numbers and would print `1000000` as `1e+06` or turn `on`
into a boolean. The round trip renders every template with what `values.yaml` *reads back
as* and must reproduce the input byte for byte, and for each example `values.yaml` merged
with `values-<label>.yaml` must reproduce that example. If an example fails, a difference
is not covered by a variable (accept the "examples" suggestions). With `helm` on PATH,
`tests/helm/test_helm_binary.py` also runs real `helm template` and `helm lint` on every
Kubernetes fixture.

**Chart view.** The output panel becomes an IDE-style view: a file explorer with the
chart's directory structure, tabs, line numbers and syntax highlighting. Click a
`{{ .Values.… }}` reference to jump to its line in `values.yaml`. Values files can be
edited for the live preview (rendered manifests, with `values.yaml` alone or plus an
example's values file); those edits are not part of the download. ⤢ maximises the view.
Each variable's values path can be edited in the Variables list.

```sh
templatize chart k8s.yaml --variant k8s-staging.yaml --label staging --accept-all -o charts/
helm template storefront charts/storefront -f charts/storefront/values-staging.yaml
```

API: `POST /chart` (same inputs as `/build`, plus `variants`, `chart_name`,
`variant_labels`), `POST /chart/render` (templates + values for the preview) and
`POST /chart/download` (zip of the chart directory). MCP: `generate_helm_chart`, and
`suggest_parameters` returns the profile so the client can offer a chart.

**Two chart styles.** The chart view has an **Exact | helm create** switch (CLI
`--style exact|scaffold`, API and MCP `style`):

- **Exact** (above): your manifests are the templates, and the round trip is byte for byte.
- **helm create**: the standard chart that `helm create` generates (Helm 4.3.2, bundled in
  `src/templatize/helm/scaffold/` with its Apache-2.0 notice), with the scaffold's
  `values.yaml` placeholders replaced by what your manifests say: `replicaCount`,
  `image.repository`/`tag`/`pullPolicy`, `service.type`/`port`, `ingress.enabled`,
  `className`, `hosts`, `tls` and annotations, `resources`, probes, `autoscaling` (from an
  HPA), `serviceAccount`, `podAnnotations`/`podLabels`, security contexts,
  `nodeSelector`/`tolerations`/`affinity`, volumes, and `fullnameOverride`/`nameOverride`
  so the names match. The deployment template is extended with optional `env`, `envFrom`,
  `command`, `args`, `containerName` and `containerPort`, and the service template with
  `service.targetPort`. Resources the scaffold does not model (ConfigMaps, Secrets, extra
  Services, ...) are carried over as exact templates with your variables. Examples give a
  `values-<label>.yaml` with only the differences, as before. The style needs exactly one
  Deployment; for anything else use the exact style.

  The scaffold adds labels and fields of its own, so a byte-for-byte round trip is not
  possible. Instead the chart is rendered with real `helm template` and compared with your
  manifests field by field: the **coverage report** (an entry in the explorer, and a badge
  in the top bar) lists what renders identically, what is **lost** (in your manifests but
  not in the chart, e.g. `metadata.namespace`, which the scaffold leaves to
  `helm install -n`, or custom labels), **changed**, and **added** by the scaffold
  (standard labels, a Service, port names). For the sample app stack, 93% of the fields
  render identically. The preview is rendered with Helm too. The app image includes a
  pinned, checksum-verified `helm` (`scripts/fetch-helm.py`); without one (set
  `TEMPLATIZE_HELM=0`, or run outside the image without Helm installed) the chart is still
  built, but marked "coverage not checked".

**Not in the exact style:** Helm idioms that change the output's structure
(`include "chart.fullname"`, `.Release.Namespace`, `toYaml` blocks). They depend on the
release name or re-serialise YAML, so the exact round trip would not hold. The helm create
style uses them, and is checked by coverage instead.

### Helm charts from Docker Compose

A Docker Compose file (`services:` with images) is recognised too, and the UI offers
**Build Helm chart**. It is converted with [Kompose](https://kompose.io) (pinned and
checksum-verified in the image, like Helm; `kompose` on PATH, or `TEMPLATIZE_KOMPOSE`,
otherwise), and the resulting Deployments, Services and volume claims go through
the same chart builder: your variables carry over by value (an image, a password,
the `DATABASE_URL` parts), and an example compose file (staging) becomes
`values-<label>.yaml`. The round trip checks the chart against Kompose's manifests.

Kompose alone does not make a runnable chart from most compose files: services reach
each other on the compose network without published ports, so a database with only
`image:` gets no Service. Before converting, a service that others use (`@db:5432`
in a URL, `REDIS_HOST: cache`, `depends_on`) gets an `expose:` port, from the
reference or the image's well-known port (postgres 5432, redis 6379, ...). What was
added, and what Kompose changed or dropped (host paths, restart policies), is listed
above the chart. CLI: `templatize chart docker-compose.yml --accept-all -o out/`.

### Ansible roles

The same template, packaged as an Ansible role that renders the document:

    templatize role nginx.conf --accept-all --name web -o out/    # or --zip web.zip
    cd out && ansible-playbook playbook.yml -e web_dest=/tmp/nginx.conf

```
web/
  defaults/main.yml     # web_server_name, web_servers (a list per loop), web_dest
  tasks/main.yml        # ansible.builtin.template with byte-exact options
  templates/nginx.conf.j2
  meta/main.yml
  README.md
  examples/<label>.yml  # per --variant: only the values that differ (-e @...)
playbook.yml
```

Variables are prefixed with the role name, the usual Ansible convention; loop item
variables and fields keep their names. Out of the box Ansible would not reproduce the
document: the template module trims the newline after block tags (`trim_blocks`
defaults to true) and Ansible re-templates any string variable. So the task sets
`trim_blocks: false`, `lstrip_blocks: false` and the document's `newline_sequence`, and
values containing `{{`, `{%` or `{#` are tagged `!unsafe` in `defaults/main.yml`. The
round trip renders the template with the vars files as Ansible reads them, and the
test suite runs real `ansible-playbook` (when installed) on the fixtures, including
CRLF, mixed line endings and literal `{{`. API: `POST /role` and `POST /role/download`.
In the UI: the **Ansible role** tab of the output panel.

### Running it publicly: rate limits and proxies

templatize has **no login**. On a public server, put it behind your reverse proxy, with
authentication if you want it private. It also limits each client's usage itself:

| Variable | Default | |
|---|---|---|
| `TEMPLATIZE_RATE_LIMIT` | `300/minute` | per client IP, all API calls incl. `/mcp` (`off` to disable) |
| `TEMPLATIZE_LLM_RATE_LIMIT` | `20/hour` | per client IP, LLM reviews (expensive) |
| `TEMPLATIZE_LLM_CONCURRENCY` | `1` | LLM reviews running at once |
| `TEMPLATIZE_LLM_QUEUE` | `2` | reviews allowed to wait for a slot; beyond that, 429 "busy" |
| `TEMPLATIZE_TRUSTED_PROXIES` | (none) | proxy IPs/CIDRs whose `X-Forwarded-For` gives the client IP, e.g. `172.16.0.0/12` |

How it behaves:
- Over-limit requests get `429` with `Retry-After`. The UI shows the message; for the LLM,
  the review is simply skipped.
- Static UI files, `/healthz` and `/info` are never limited.
- Limits are kept in memory per process and never logged; IPs aren't stored beyond the
  bucket state.
- `X-Forwarded-For` is ignored unless the direct peer is a trusted proxy, so clients
  can't spoof their way around a limit.

### MCP server

The same core library is exposed as an MCP server (official Python SDK, `mcp` 2.x)
with five read-only tools:

| Tool | What it does |
|---|---|
| `suggest_parameters(text, variants?, min_confidence?)` | Scored candidate variables (spans are code points) |
| `generate_template(text, variables?, variants?, min_confidence?, engine?)` | Template, values file, runner script and the round-trip verdict (`engine`: `jinja2`, `envsubst` or `terraform`), plus `notes` to pass on. Give variables **by value**, e.g. `[{"name": "db_host", "value": "db01.example.com", "occurrences": "all"}]`, so the model never has to count characters (spans also work). Omit `variables` to accept suggestions automatically |
| `generate_helm_chart(text, variables?, variants?, chart_name?, variant_labels?, min_confidence?, style?)` | Helm chart from Kubernetes manifests: Chart.yaml, nested values.yaml, templates, and a values file per variant. `style="exact"` checks a byte-for-byte round trip per document; `style="scaffold"` fills in the `helm create` chart and returns a coverage report. Variables take an optional `values_path` |
| `generate_ansible_role(text, variables?, variants?, role_name?, filename?, dest?, variant_labels?, min_confidence?)` | The template packaged as an Ansible role (defaults, tasks, template, meta, README, a vars file per variant) plus `playbook.yml`, with the round-trip verdict |
| `render_template(template, values?, newline?)` | Sandboxed render. Errors come back in `error`, not as an exception |
| `validate_template(template, values?, expected_text?)` | Syntax, referenced, missing and unused variables, renders?, and reproduces `expected_text` byte for byte? |

Transports:
- **stdio:** `templatize-mcp`, or `docker run --rm -i IMAGE templatize-mcp`.
- **Streamable HTTP:** `/mcp` on the running app (stateless, JSON responses), or standalone
  with `templatize-mcp --transport http --port 8001`.

**Claude Code**: pick one of these.

```sh
# HTTP: the app must be running (make up, or make serve)
claude mcp add --transport http templatize http://127.0.0.1:8000/mcp

# stdio from a checkout (no server needed)
claude mcp add templatize -- uv --directory /path/to/jinja-ninja run --extra mcp templatize-mcp

# stdio from the container image (no Python needed; fully offline)
claude mcp add templatize -- docker run --rm -i --network none --read-only templatize:dev templatize-mcp
```

Add `--scope user` to make it available in every project. Check it with `claude mcp list`
or `/mcp` inside a session.

**Claude Desktop**: edit `claude_desktop_config.json` (Settings → Developer → Edit
Config; on macOS it lives at `~/Library/Application Support/Claude/claude_desktop_config.json`)
and restart Claude Desktop:

```json
{
  "mcpServers": {
    "templatize": {
      "command": "uv",
      "args": ["--directory", "/path/to/jinja-ninja", "run", "--extra", "mcp", "templatize-mcp"]
    }
  }
}
```

or, with the container image (use `podman` instead of `docker` if that's your runtime; the
GUI app may need the full path, e.g. `/opt/podman/bin/podman`):

```json
{
  "mcpServers": {
    "templatize": {
      "command": "docker",
      "args": ["run", "--rm", "-i", "--network", "none", "--read-only", "templatize:dev", "templatize-mcp"]
    }
  }
}
```

Then ask, for example: *"Use templatize to turn this nginx config into a template with the
server name and upstream IP as variables."*

**Security and privacy.** `/mcp` has DNS-rebinding protection: only `127.0.0.1`,
`localhost`, `[::1]` and the compose service name `app` are accepted as `Host` (and
`Origin`). If you expose the server elsewhere, for example over Tailscale, add hosts with
`TEMPLATIZE_MCP_ALLOWED_HOSTS=myhost.tailnet.ts.net:*`. There's no authentication, so
don't expose it beyond networks you trust.

Tool errors reach the client with a useful message but are never logged. Crashes are
logged by exception type only, and the SDK's own loggers are kept at WARNING because its
INFO lines echo error text. Set `TEMPLATIZE_MCP_HTTP=0` to turn off `/mcp`.

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
3. loads the web UI and every asset it references, and checks that nothing points
   outside the origin;
4. speaks MCP to `/mcp`: lists the 4 tools and checks that `generate_template`
   round-trips a fixture;
5. runs every fixture through `/suggest` → `/build` (round-trip must pass) →
   `/render` (must reproduce the input) → `/download` (zip must be complete).

This must pass before a milestone counts as done. It runs the app **with the NER
service**: the checker waits for NER, requires NER candidates in `/suggest`, and requires
the round trip to pass. Without the `llm` profile it also verifies that `/suggest/llm`
degrades gracefully (`available: false`).

`make offline-test-llm` (needs `make model`) adds the llm service to the isolated stack.
The checker waits for the model to load, runs a real review of the business letter,
merges it, and requires the round trip to pass. CI doesn't run it, because the runner
would download 2.5 GB per run.

The fixtures are baked into a separate `offline-check` image target instead of being
bind-mounted. That way the test also works on CI runners that drive the host's Docker
daemon from inside a job container, where workspace bind mounts don't resolve. The
runtime image (`--target runtime`) contains no test data.

## Architecture

```text
             ┌────────────── templatize (core library, no web deps) ──────────────┐
 text ──►    │ detectors/            merge.py           ir.py          emitters/  │
 variants ─► │  regex, key_value ──► unify, resolve ──► TemplateIR ──► jinja2 ... │──► template
             │  repetition, diff     overlaps, names    (validated)       │       │    .env
             │  (ner, llm: later)                                         ▼       │    render.py
             │                                         roundtrip.py ◄── render    │    PASS/FAIL
             └────────────────────────────────────────────────────────────────────┘
                  ▲ CLI    ▲ templatize_api (FastAPI) ◄── frontend/ (Preact UI)   ▲ templatize_mcp (stdio, /mcp)
   optional services (compose profiles):  ner ── templatize_ner (GLiNER, ONNX Runtime)
                                          llm ── llama.cpp server (Qwen3-4B GGUF)
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
class LoopField: name; type; spans: list[Span]   # one per item
class Loop:      name; item; items: list[Span]; fields: list[LoopField]; scalar: bool
class TemplateIR: ir_version = 2; text: str; variables: list[Variable]; loops: list[Loop]
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

**Loops** (repeated blocks: SQL rows, list items, host blocks). A loop's items share
one shape and are joined by the same separator; each item's holes are the loop's
fields (`user.email`) or variables with a span at the same place in every item
(`tenant_id`). Jinja2 renders it as

```jinja
{% for user in users %}{% if not loop.first %},
{% endif %}  ('{{ tenant_id }}', '{{ user.email }}', '{{ user.role }}'){% endfor %}
```

and the values go to `values.yaml` instead of `.env` (lists of mappings, or of strings
for a scalar loop), which `render.py` reads. Add or remove items there to change the
output. Loops are accepted by `POST /build` (`loops`), `templatize build --loops
loops.json`, and lists are accepted as values by `/render` and the MCP render/validate
tools.

Loops are suggested (`loops` in `/suggest`, `templatize suggest`, MCP
`suggest_parameters`) for YAML/JSON lists of values or of same-keyed mappings,
`INSERT ... VALUES` rows (fields named after the columns), inline lists such as
`BROKERS = ["a", "b"]`, and runs of lines starting with the same keyword
(`server 10.0.0.1:8080;`). Items must share one shape: what differs between them
becomes the fields. Examples with more or fewer items confirm a loop (confidence
0.97, and their lists are kept). `templatize build --accept-all` and MCP
`generate_template` without `variables` accept loops too (`--no-loops` to keep
separate variables).

In the web UI a suggested loop is a bracketed region of the document with a badge
(`users × 2`) and a card under **Lists**: accept it to make it a loop, rename the list,
the loop variable and the fields, and add, remove or edit rows to see the template
take any number of items in the preview. Accepting a loop sets aside the suggestions
on its fields (they come back if you remove the loop); variables that sit at the same
place in every item, such as a tenant id in every row, still work inside it.

**Nested loops.** A YAML list of mappings with the same keys, where some keys hold a
list in every item (`servers[].ports[]`, a Deployment's `containers[].ports[]`), is
one loop with nested lists: `{% for server in servers %}...{% for port in
server.ports %}...{% endfor %}{% endfor %}`, Helm `{{ range $j, $port :=
$server.ports }}`, and each row of the values holds its own list, of any length. One
level of nesting; inner items are scalars or flat mappings. In the UI a nested list
is a read-only column of the list's table.

**Table loops.** Rows of a table with aligned columns (an invoice's line items, a
hosts list) are a loop whose fields are padded slots: a column whose cells share a
start is left-aligned, one whose cells share an end (or numbers of one width) is
right-aligned, and field names come from a header line above (skipping a `----`
rule). Jinja renders `{{ "%-34s"|format(row.description) }}  {{ "%3s"|format(row.qty) }}`,
Helm `{{ printf "%-34v" $row.description }}`, so a new row lines up under the
header. A value longer than its column pushes the rest of the row right (columns
never truncate); two spaces always separate the columns.

**Map loops.** Open-ended key/value blocks (`environment:`, `labels:`,
`annotations:`, `tags:`, `data:`, or keys that look like environment variables) are
offered as map loops: `{% for key, value in web_environment.items() %}` over a
mapping in values.yaml, so adding an entry adds a line. They are named after the
enclosing key (`web_environment`, `db_environment`). "Accept all" leaves them out,
because accepting one replaces the per-key variables (and, in compose, the
`DATABASE_URL` parts); accept them one by one. In Helm charts a map is written as a
list of `{key, value}` rows, because Go ranges maps in sorted key order and would
reorder the output.

In an exact-style Helm chart a loop is a `range` over a list in values.yaml
(`{{ range $i, $port := .Values.ports }}{{ if $i }}SEP{{ end }}...{{ end }}`; inside it,
variables are `{{ $.Values.x }}`), and an example with more or fewer items gets its
list in `values-<label>.yaml`. The helm create style keeps lists as plain text. Design
notes: `docs/design/loops.md`.

> JavaScript clients index strings in UTF-16 code units, not code points. Convert at the
> API boundary.

### Detectors (v1)

| Detector | What it finds | Confidence |
|---|---|---|
| `regex` | IPv4/IPv6, CIDR, email, URL, hostname/FQDN, `host:port` endpoints, port, UUID, ISO/numeric/text dates (weekday and year optional), times, date-and-time, `start_`/`end_` ranges, semver, paths, MAC, phone numbers, AWS ARNs and regions, references (`INC-20931`), IDs (`P7X2KQ1`), amounts, IBANs, API tokens and JWTs, access and verification codes, durations, data sizes (`512Mi`, `1.2 TB`), log timestamps (Apache/nginx `[03/Dec/2026:14:22:09 +0000]`, syslog `Dec  3 14:30:01`), container images (`registry/path:tag`, Docker Hub `grafana/grafana:11.3.1`), AWS resource IDs (`snap-…`, `sg-…`, named `snapshot_id`, `security_group_id`), company registration numbers, card/account endings, bank payment references, hex checksums (not `SHA-256` or `RSA-2048` as references), port mappings (`18080:8080`, `-p 3000:3000` → `host_port`/`container_port`), Vault paths (`secret/billing/stripe`), and two-part versions after a product (`Elasticsearch 7.17` → `elasticsearch_version`; `from 14.11 to 16.6`). A whole value (URL, email, endpoint, timestamp, image, phone) wins over matches inside it; references, IDs and amounts are named after the word before them, phones and codes after a line label (`dial_in_pin`); amount ranges are `salary_min`/`salary_max` | 0.4–0.9 |
| `key_value` | values in `key: value`, `key = value`, `KEY=value`, `"key": "value"`, `directive value;`, `Keyword value` (ssh_config, Apache, Fluent Bit) and `keyword value` (redis.conf, haproxy) lines; Dockerfiles, Makefiles, XML, TOML inline tables, iCalendar/vCard, Ansible inventory host lines, Terraform variable blocks and `terraform plan` output, Markdown `**Key:** value` lines; `key=value` strings (`Server=…;Database=…`, `host=… port=…`) are split into their parts. Names come from the key, YAML parents qualify generic keys (`database_host`), and k8s `name:`/`value:` pairs are recognised | 0.45–0.75 |
| `address` | postal addresses over several lines: street line(s) down to the town and postcode (UK, US, Canada, Europe); `sender_address`/`recipient_address` in a letter | 0.75 |
| `people` | names a heuristic can be sure of: the name matching an email in the text, a letter's signature and recipient block (first/last name, title, company), the greeting's name, the letter date, companies above addresses, brands in stock phrases ("shopping with X", "The X team"), "Name (Title)" and "the X role", "As a X at Y"; attendee and reviewer lists (first and last name apart when the first name is used alone), people after "report to", "Questions to", "approved by", and services named in prose ("the Ledger Service", "Payments API"), contract parties named by their defined term (`client_company`, and `client_address` for the address before it), signatories, companies with a legal suffix in prose (`payer_name` after "payment from"; a letterhead is `sender_company`), a letter's sign-off without a closing line, doctors (`Dr. …`) | 0.55–0.85 |
| `commands` | values of long flags (`--namespace billing`), database client options (`psql -h … -U …`), `ssh user@host`, `-p 3100`, and Markdown code spans next to the noun they name | 0.6–0.9 |
| `sql` | literals in `INSERT … (cols) VALUES (…)` rows, `UPDATE … SET col = …` and `WHERE col = …`, named after their column; roles, passwords, databases and schemas of `CREATE USER`/`GRANT` | 0.7–0.8 |
| `repetition` | identical non-trivial tokens (or multi-word names such as `Bluewater Logistics`) appearing ≥2 times → one variable covering all occurrences. Words that only ever start a line (`Host`, `INSERT`), markup tag names, file names, protocol versions, `$VAR` references, a contract's defined terms (`(the "Agreement")`) and well-known constants (`Mozilla/5.0`, `x86_64`, log levels) are not values | 0.5–0.7 |
| `diff` | regions that differ between the document and its variants; a changed phrase (`Procurement Manager` → `Head of Operations`) stays one value, and a region snaps to the address or date around it | 0.95 |

Text inside template syntax already in the document (`${{ github.sha }}`, `{{ }}`) is
never a value. Loops are also found for CSV files (fields named by the header).

**Suggestion quality tests.** `tests/quality/` runs the detectors on every sample in the editor and checks `expectations.yaml`: values that must be suggested whole, and known noise that must not be. `make samples` refreshes the exported samples after editing `frontend/src/sample.ts`.

**Detection corpus.** `tests/quality/corpus/` holds 109 documents the detectors were not
written against (configs, CI files, IaC, emails, letters, logs), with expectations in
`corpus.yaml`. Each is checked for its expected values and noise, for a round trip
with every suggestion accepted, and for a round trip with a generated example in which
every suggested value changes. `make corpus-report` lists what is missed and what is
noise.

**UI end-to-end tests.** `make e2e` (against a running app, e.g. `make up`) drives the UI
in Chrome with Playwright: every sample and corpus document is analyzed, every
suggestion accepted, and the round trip must pass; what was suggested is written to
`frontend/e2e-report/`. `make e2e-ci` runs the same suite against a fresh server started
from source (after `make frontend`), as CI does; set `E2E_CHANNEL=` to use Playwright's
bundled Chromium instead of Google Chrome.

**Names from context.** A generic key inside a keyed block or INI section is
qualified by it: `DATABASE = {"host": ...}` gives `database_host`, `[server] port`
gives `server_port`, like YAML parents already did. In the UI, tick several variables
to give them a common prefix: the prefix they share is replaced (`database_host` →
`db_host`), otherwise one is added.

**Composite values.** When a suggested value is built from two or more other
suggested values, each a whole field between URL/DSN separators (`:/@?&=;,`), it is
suggested as those parts: `DATABASE_URL: postgres://shop:s3cretPass@db:5432/storefront`
next to `POSTGRES_USER: shop`, `POSTGRES_PASSWORD` and `POSTGRES_DB` becomes
`postgres://{{ postgres_user }}:{{ postgres_password }}@{{ database_host }}:{{ database_port }}/{{ postgres_db }}`,
so the URL cannot drift from them (a note says so). The URL's own fields that no
other value covers (host, port) become variables named after it (`database_url` →
`database_host`); the host also covers the places where it is a whole value or key
on its own, such as the compose service `db:` and `depends_on: [db]`. Words inside names (`ops-admin`,
`acme-marketing-assets-prod`) do not count, parts must be named values, and with
examples the parts must explain how the whole changes.

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

## Document types

Each analysis says what kind of document it thinks the text is: one of about 40 types
(Kubernetes manifest, Docker Compose file, GitHub Actions workflow, cloud-init, nginx,
HAProxy, ssh_config, systemd unit, `.env`, INI, TOML, CSV, SQL, access log, letter,
email, …), with the runners-up. The list is `GET /document-types`.

**Confirming a type** applies its format rules:
- **Configs read as configs:** a two-line ssh_config is read as `Keyword value` lines,
  and a `redis.conf` as `keyword value` lines.
- **Prose never read as config:** a letter, email or Markdown file is never read as
  configuration lines.
- **Kubernetes structure rules:** these apply only to a confirmed Kubernetes manifest.
- **Detected types change nothing:** a type that is only detected leaves
  auto-detection as it is.

How to confirm it:
- **UI:** the **Document type** picker next to the document shows "Detected: …".
  Choosing a type analyses the document again with it.
- **API:** `document_type` on `/suggest` and `/suggest/llm`.
- **MCP:** `document_type` on `suggest_parameters` and `generate_template`.
- **CLI:** `--type ID`.

Detection is cheap pattern matching on the first 200,000 characters. Every fixture,
sample and corpus document has its expected type in `tests/core/test_doctypes.py`.

### Schema rules

Kubernetes, Compose, GitHub Actions and cloud-init have **schema rules**
(`src/templatize/schemas.py`): what their fields mean, drawn from the official specs and
keyed by the field's path in the YAML. A rule takes one of four actions:

| Action | Example |
|---|---|
| structure: never a value | `**.imagePullPolicy`, `services.*.restart`, `permissions.*`, `package_update` |
| name the value | `jobs.*.runs-on` → `runs_on`, `jobs.*.timeout-minutes` → `timeout_minutes` |
| suggest it even if no detector did | cloud-init `hostname`, `timezone`, a one-item `packages:` list |
| `KEY=value` item | Compose `environment: [QUEUE_NAME=emails]` → `queue_name` |

**When they apply:** when the type is confirmed, or detected with confidence ≥ 0.9.

**How they were measured:** on eight unseen documents (`tests/quality/corpus`, batch
nine), the rules took the problems from 17 to 0. On every earlier document they only
removed noise (`restart:`, `imagePullPolicy:`) and improved names.

**Two judgements that run the other way:**
- **A Service's `type`** (ClusterIP, LoadBalancer) stays a value, as in Helm charts.
- **A workflow's trigger branch** stays fixed.

### JSON Schemas

**References.** A schema the document references is recognised and reported: JSON
`"$schema": "https://…"`, YAML `$schema:`, or the YAML language server's
`# yaml-language-server: $schema=…`. It is **never downloaded**, for three reasons:
- **Offline:** the tool runs offline.
- **Privacy:** a fetch would tell a third party what was pasted.
- **Security:** a server that fetches user-supplied URLs can be pointed at internal
  addresses (SSRF).

The schema URL itself is never suggested as a value.

**Supplying a schema.** To use a schema, supply its text, as JSON or YAML:
- **UI:** the **Schema** tab next to the document.
- **API:** `schema` on `/suggest` and `/suggest/llm`.
- **MCP:** `json_schema`, as an object or text.
- **CLI:** `--schema FILE`.

**How it's applied.** The schema is walked along the document's fields (`properties`,
`patternProperties`, `additionalProperties`, `items`/`prefixItems`, local `$ref`,
`allOf`/`anyOf`/`oneOf`):

| Keyword | Effect |
|---|---|
| `const` | the field is structure, never a value |
| `format` (uri, email, hostname, ipv4/ipv6, date, date-time), integer with `maximum: 65535` | the value's type, and a suggestion even if no detector found it |
| value equals `default` | a weaker candidate (confidence × 0.6): nobody changed it |
| short `title` | the name, when no key named the value |
| `enum` | stays a value (log levels are enums too); the choices are given in the reason |

**What it doesn't do:**
- **External `$ref`s** are not followed; a note says which.
- **Size and cycles:** schemas are limited to 2 MB, and `$ref` cycles and deep nesting
  are cut off.

The analysis reports `schema_info`: the reference, whether a schema was supplied, its
title, and how many fields it matched.

## Engines

| Engine | Template | Values | Renders with | Loops |
|---|---|---|---|---|
| `jinja2` (default) | `template.j2`, `{{ name }}` | `.env` (`values.yaml` with loops) | `uv run render.py` | yes |
| `envsubst` | `template.tmpl`, `${KEY}` | `values.env` (single-quoted, sourced) | `sh render.sh` (GNU gettext `envsubst`) | no |
| `terraform` | `template.tftpl`, `${name}` | `terraform.tfvars` | `main.tf`: `templatefile()`; `terraform apply`, `terraform output -raw rendered` | yes |

**envsubst.**
- **No escape syntax, and none needed:** `render.sh` runs `envsubst '${HOST} ${PORT}'`
  with the template's own variables, so every other `$name` / `${name}` in the text
  stays as it is.
- **Renamed variables:** a variable whose key the text already uses (`$REGISTRY` in a
  CI file) gets `_VALUE` appended, as does one the shell treats specially (`UID`,
  `SECONDS`, `IFS`, `PATH`, `LD_*`, `LC_*`, `BASH_*`, …). The build's notes list every
  rename.
- **Values file:** `values.env` holds `KEY='value'` lines, with `'` written as `'\''`.
- **Safe to source:** `render.sh` finds `envsubst` before sourcing the values, so a
  value can't hide it.

**Terraform.**
- **Escapes:** a literal `${` / `%{` is written `$${` / `%%{`. A `$` followed by
  another `$`, or just before a placeholder (`$4,000`), is written `${"$"}`, so nothing
  can be read two ways.
- **Lone carriage returns:** a lone CR (an old Mac line ending) is written `${"\r"}`.
  After a raw one, Terraform stops interpolating for the rest of the file.
- **Loops:** `%{ for user_index, user in users }…%{ endfor }`, with the separator
  inside `%{ if user_index > 0 }…%{ endif }`. There are no `~` strip markers, so
  whitespace is exactly the text's.
  - **Nested lists:** loop over `server.ports`.
  - **Padded table columns:** `${format("%-12s", row.name)}`.
  - **Map loops** (`environment:`, `labels:`): written as a list of `{ key, value }`
    objects, because Terraform iterates a map in key order and would reorder them.
- **Values:** `terraform.tfvars` holds one object: HCL strings, and lists for the loops
  (`main.tf` takes `type = any`).
- **Running it:** `main.tf` declares no providers, so `terraform init` downloads nothing.

**Loops in envsubst.** `envsubst` has no loops, so accepted lists stay literal text,
and the build says so in `notes` (API, MCP, CLI and UI).

**Verification.**
- **In-process round trip:** both engines round-trip in-process, through their real
  values file and parser, for every fixture, sample and corpus document.
- **Real envsubst:** the tests also run the generated `render.sh` with the real
  `envsubst` for every document.
- **Real Terraform:** when Terraform or OpenTofu is installed, the tests run the
  generated `main.tf` for every document and every awkward-literal and value case
  (checked with Terraform 1.17.2). CI fetches that version with
  `scripts/fetch-terraform.py`, which is pinned and checksum-verified, so these tests
  always run there.

## Adding an engine emitter

1. Create `src/templatize/emitters/<engine>.py` with a class satisfying the
   `Emitter` protocol in `emitters/base.py`:
   - `engine`, `template_filename`, `supports_loops`
   - `emit(ir) -> str`: escape every literal piece so the engine outputs it verbatim,
     and preserve newlines exactly.
   - `render(template, values, newline=None) -> str`: used by the round-trip check.
     If the engine isn't available in Python (e.g. Go templates), shell out to a
     bundled binary or embed a minimal renderer. Verification isn't optional.
   - `values_file(ir)` and `parse_values_file(content)`: the values in the engine's
     own format. The round trip writes and reads them, so a quoting bug fails it.
   - `notes(ir)`: what the user should know (renamed variables, unsupported names).
   - `runner_files(ir)`, `run_command()` and `requirements()` for the downloadable bundle.
2. Register it in `EMITTERS` in `emitters/__init__.py`.
3. Run the fixture suite with the new engine (`tests/core/test_fixtures.py`).
   Every fixture must round-trip.

## Website

`site/` is a static introduction page for GitHub Pages: no JavaScript, and no
third-party scripts, fonts or trackers. `.github/workflows/pages.yml` publishes it, and
`.github/FUNDING.yml` adds GitHub's Sponsor button for Buy Me a Coffee. The Buy Me a
Coffee page is <https://buymeacoffee.com/welshstew>. The repository is
<https://github.com/welshstew/templatize>; Pages serves the site at
<https://welshstew.github.io/templatize/>. The Buy Me a Coffee link is a plain link, not their widget
script, so visitors aren't tracked.

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
