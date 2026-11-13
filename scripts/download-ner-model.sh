#!/bin/sh
# Download the GLiNER ONNX model for the optional NER service, pinned to an exact
# repository revision and verified by SHA-256. Used by docker/ner.Dockerfile at
# build time (build online, run offline) and for local development.
#
#   Model:  GLiNER small v2.1 (urchade/gliner_small-v2.1, Apache-2.0), ONNX export
#           with int8 dynamic quantisation, ~190 MB in total
#   Source: https://huggingface.co/onnx-community/gliner_small-v2.1
set -eu

REPO="onnx-community/gliner_small-v2.1"
REVISION="8142fb00740ccea973e64b1272949ff48653df5e"
DIR="${1:-models/ner}"
BASE="https://huggingface.co/$REPO/resolve/$REVISION"

sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1
    else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

fetch() {  # fetch <remote path> <local name> <sha256>
    target="$DIR/$2"
    if [ -f "$target" ] && [ "$(sha256_of "$target")" = "$3" ]; then
        echo "ok: $target already present and verified"
        return
    fi
    echo "downloading $1 ..."
    curl --fail --location --retry 3 --silent --show-error -o "$target.part" "$BASE/$1"
    actual="$(sha256_of "$target.part")"
    if [ "$actual" != "$3" ]; then
        echo "checksum mismatch for $1: expected $3, got $actual" >&2
        rm -f "$target.part"
        exit 1
    fi
    mv "$target.part" "$target"
    echo "ok: $target"
}

mkdir -p "$DIR"
fetch "onnx/model_int8.onnx" "model.onnx" "c76c90920547fd937aaf505e7f2de5ec73168bf1c25abbb55a298104cb061400"
fetch "tokenizer.json" "tokenizer.json" "677203884d026e721115cf0daccf70ec4239545a13d6619e3e66d7151e0c9ce3"
fetch "gliner_config.json" "gliner_config.json" "8e8b59de124a256a3f3de67879d0da686fe3f73ecc05093506ba525e451b920d"
