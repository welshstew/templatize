# syntax=docker/dockerfile:1.7
# llama.cpp server for templatize's optional LLM detector.
# CPU by default; compose.gpu.yaml switches LLAMA_IMAGE to the CUDA build.
ARG LLAMA_IMAGE=ghcr.io/ggml-org/llama.cpp:server-b11946
FROM ${LLAMA_IMAGE}
COPY --chmod=755 docker/llm/entrypoint.sh /usr/local/bin/templatize-llm
ENV HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    DO_NOT_TRACK=1
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=5s --start-period=120s --retries=5 \
    CMD ["curl", "-sf", "http://127.0.0.1:8080/health"]
ENTRYPOINT ["/usr/local/bin/templatize-llm"]
