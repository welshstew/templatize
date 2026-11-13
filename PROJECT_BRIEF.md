I want to build the first version of a web tool that turns plain text into a Jinja2 template. I'll describe the product, the architecture, and what "done" means. Before writing code, read this whole prompt, propose a short implementation plan, and ask me about anything that's ambiguous. Then build it in the milestones below, committing after each one.

## The product

A user pastes text (a config file, an email, a contract, a YAML manifest, etc.) into a web page. The tool analyzes it and highlights segments that look like good candidates for parameters (IP addresses, hostnames, names, dates, repeated tokens, etc.). The user can accept or reject suggestions, manually select any other span of text to turn into a variable, and rename variables. The tool then produces:

1. A Jinja2 template with {{ variable }} placeholders
2. A .env file containing the extracted values (KEY=value)
3. A small Python render script (render.py) using jinja2 and python-dotenv that renders the template with the .env values

The user can download all three as a zip and run it locally with one command. The page also has a live preview: the user can edit values and see the rendered output update.

## Core design principles

- **Engine-neutral core.** Do NOT generate Jinja2 strings directly from the analysis. The analyzer produces an intermediate representation (IR): the original text plus a list of variables, each with name, type (string/int/ip/date/etc.), default value, list of character spans where it occurs, detection source, and confidence. A separate "emitter" turns IR into Jinja2. Later I'll add emitters for Go text/template (Helm), Handlebars, Liquid, etc., so keep the emitter interface clean and pluggable.
- **Round-trip guarantee.** After generating a template, the tool must render it with the extracted values and verify the output is byte-for-byte identical to the original input. Show this check prominently in the UI (pass/fail). Treat a round-trip failure as a bug.
- **Escaping.** Any existing Jinja syntax in the input ({{, {%, {#) must be escaped (e.g. wrapped in {% raw %} blocks) so the template still renders correctly. Include tests for this.
- **Whitespace fidelity.** Preserve trailing newlines, indentation and line endings exactly. Configure the Jinja environment explicitly (keep_trailing_newline=True, etc.).
- **Core logic is a library.** The analyzer, IR, emitters and round-trip checker live in a Python package with no web dependencies, so a web API and a future MCP server can both import it.

## Suggestion engine (v1)

Implement these detectors, each producing scored candidates:

1. Regex patterns: IPv4/IPv6, CIDR, emails, URLs, hostnames/FQDNs, ports, UUIDs, ISO dates and common date formats, semantic version numbers, file paths, MAC addresses.
2. Key/value heuristics: values on the right side of `key: value`, `key = value`, `KEY=value`, `"key": "value"` lines; derive the variable name from the key.
3. Repetition: identical non-trivial tokens appearing multiple times should be suggested as one variable covering all occurrences.
4. Multi-example diffing: the user can optionally paste 2–3 variants of the same document. Align them (token-level diff) and suggest the differing regions as variables, with very high confidence. This is a key differentiator, so make it solid.

Merge overlapping candidates sensibly (prefer longer/higher-confidence spans). Generate readable snake_case variable names, uppercase for .env keys, and de-duplicate names. Leave a clear extension point for an optional LLM-based detector later, but don't implement it now.

## Tech stack

- Backend: Python 3.11+, FastAPI, Jinja2, python-dotenv. Use uv or pip with a pyproject.toml.
- Frontend: simple and dependency-light. A single-page app with Vite + TypeScript is fine; plain TypeScript or a lightweight framework is preferred over a heavy one. Clean, usable UI: input pane, highlighted suggestions with accept/reject, a variables sidebar (name, value, type), output tabs (template / .env / render.py / preview), and a download button.
- Tests: pytest for the core library, with fixture documents (nginx config, docker-compose.yml, a business letter, a JSON config, a Kubernetes manifest, text containing literal {{ }}). Every fixture must pass the round-trip check.

## Milestones

1. **Core library**: IR, detectors, Jinja2 emitter, .env and render.py generation, round-trip checker, escaping. Full test suite passing. A small CLI (`templatize suggest file.txt`, `templatize build file.txt --accept-all`) for quick manual testing.
2. **Web API**: FastAPI endpoints: POST /suggest (text plus optional variant texts → candidates), POST /build (text + chosen variables → template, env, script, round-trip result), POST /render (template + values → output or error), GET /download (zip).
3. **Frontend**: the UI described above, wired to the API. Manual selection of arbitrary text spans to create a variable is required, not just accepting suggestions.
4. **MCP server**: using the official Python MCP SDK, expose tools suggest_parameters, generate_template, render_template and validate_template, all backed by the same core library. Include instructions in the README for adding it to Claude Desktop / Claude Code.

## Constraints

- No external services or API keys needed to run anything.
- Never log or persist user-pasted text on the server.
- README with setup, running, architecture overview, and how to add a new engine emitter.
- Keep code well-structured and typed (mypy-clean core library).

Start by proposing the plan and the IR data structure for my review.

## Additional hard requirements: portable, offline, containerised

The whole system must run fully offline inside containers. Principle: build online, run offline. Nothing may be downloaded at runtime.

- Frontend: bundle all JS, CSS and fonts. No CDN references anywhere. Add a test that greps the built output for external URLs.
- Python dependencies installed at image build time. Pin versions (lockfile).
- Any model weights are downloaded during image build or mounted from a volume. Set HF_HUB_OFFLINE=1 and TRANSFORMERS_OFFLINE=1, and fail with a clear error if a model is missing rather than attempting a download.
- No telemetry, analytics, or update checks.

Container layout (docker compose with profiles):
- `app`: FastAPI backend serving the built frontend, heuristic detectors, and the MCP server (support both stdio and streamable HTTP transports). Must be fully functional on its own. Slim base image, non-root user, health check, read-only root filesystem where practical.
- `ner` profile: optional semantic span detector using GLiNER. Prefer an ONNX Runtime CPU build over PyTorch if feasible, to keep the image small; report the trade-off to me if not.
- `llm` profile: optional llama.cpp server running a quantised GGUF model (path configurable via env var, mounted from a volume). CPU by default; provide a separate compose override file for GPU.
- Use multi-stage Dockerfiles. Build multi-arch images (linux/amd64 and linux/arm64) with docker buildx.
- The app discovers optional services via env vars and degrades gracefully: if the NER or LLM service is absent or unreachable, the UI still works and simply doesn't show those suggestions.

## Additional milestone: optional LLM detector (after milestone 3)

- Implement the LLM detector as a pluggable detector that calls the llama.cpp server's API.
- The model never produces the template. It returns JSON suggestions only: exact substring, occurrence index, suggested variable name, type, short reason. Never ask for character offsets.
- Use constrained decoding (JSON schema / grammar) so output is always valid.
- Code locates each suggested substring, discards any that don't exist in the input, merges with heuristic candidates, and the round-trip check runs as normal.
- Tasks for the model, in priority order: name variables; classify heuristic candidates as variable vs fixed; find semantic variables the heuristics missed.
- In the UI, run heuristics first and show results instantly; merge LLM suggestions in when they arrive, visibly labelled by source.

## Offline verification

Add a CI job (and a `make offline-test` target) that brings up the full stack with networking disabled and runs every fixture through suggest → build → round-trip check. This job must pass before any milestone counts as done.
