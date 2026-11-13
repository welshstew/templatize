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

set -- --model "$LLM_MODEL" --host 0.0.0.0 --port 8080 \
    --ctx-size "$LLM_CTX" --parallel 1 --n-gpu-layers "$LLM_GPU_LAYERS" \
    --offline --no-slots --jinja --temp 0
[ "$LLM_THREADS" -gt 0 ] && set -- "$@" --threads "$LLM_THREADS"
exec /app/llama-server "$@"
