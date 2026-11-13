#!/bin/sh
# Start llama-server for templatize. Never downloads anything: the model must be
# mounted at $LLM_MODEL (see `make model` and the README).
set -eu

: "${LLM_MODEL:=/models/Qwen3-4B-Instruct-2507-Q4_K_M.gguf}"
: "${LLM_CTX:=16384}"
: "${LLM_GPU_LAYERS:=0}"
: "${LLM_THREADS:=0}"

if [ ! -f "$LLM_MODEL" ]; then
    cat >&2 <<MSG
templatize-llm: model file not found: $LLM_MODEL

The LLM detector needs a GGUF model mounted into this container; nothing is
downloaded at runtime. To fetch the default model (Qwen3-4B-Instruct-2507,
Q4_K_M, ~2.5 GB, checksum-verified) run on the host:

    make model

or put your own GGUF in ./models and set TEMPLATIZE_LLM_MODEL=<file name>.
MSG
    exit 64
fi

# Authentication. Prefer a key file (not visible in `docker inspect` or `ps`); a
# plain LLM_API_KEY is passed via llama-server's own env var, never on argv.
key_args=""
if [ -n "${LLM_API_KEY_FILE:-}" ]; then
    if [ ! -r "$LLM_API_KEY_FILE" ]; then
        echo "templatize-llm: LLM_API_KEY_FILE is not readable: $LLM_API_KEY_FILE" >&2
        exit 64
    fi
    key_args="--api-key-file $LLM_API_KEY_FILE"
elif [ -n "${LLM_API_KEY:-}" ]; then
    export LLAMA_API_KEY="$LLM_API_KEY"
    unset LLM_API_KEY
elif [ "${LLM_REQUIRE_API_KEY:-0}" = "1" ]; then
    echo "templatize-llm: an API key is required (set LLM_API_KEY_FILE or LLM_API_KEY)" >&2
    exit 64
fi

# shellcheck disable=SC2086  # key_args is intentionally word-split
set -- --model "$LLM_MODEL" --host 0.0.0.0 --port 8080 $key_args \
    --ctx-size "$LLM_CTX" --parallel 1 --n-gpu-layers "$LLM_GPU_LAYERS" \
    --offline --no-slots --jinja --temp 0
[ "$LLM_THREADS" -gt 0 ] && set -- "$@" --threads "$LLM_THREADS"
exec /app/llama-server "$@"
