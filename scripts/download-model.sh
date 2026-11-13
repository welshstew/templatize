#!/bin/sh
# Download the default GGUF for the optional LLM detector, pinned to an exact
# repository revision and verified by SHA-256. Run on the host (build online);
# the containers only ever read the file (run offline).
#
#   Model:   Qwen3-4B-Instruct-2507 (Apache-2.0), Q4_K_M quantisation, ~2.5 GB
#   Source:  https://huggingface.co/unsloth/Qwen3-4B-Instruct-2507-GGUF
set -eu

REPO="unsloth/Qwen3-4B-Instruct-2507-GGUF"
REVISION="a06e946bb6b655725eafa393f4a9745d460374c9"
FILE="Qwen3-4B-Instruct-2507-Q4_K_M.gguf"
SHA256="3605803b982cb64aead44f6c1b2ae36e3acdb41d8e46c8a94c6533bc4c67e597"
DIR="${1:-models}"

sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1
    else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

mkdir -p "$DIR"
target="$DIR/$FILE"
if [ -f "$target" ]; then
    echo "verifying existing $target ..."
    if [ "$(sha256_of "$target")" = "$SHA256" ]; then
        echo "ok: $target is already present and verified"
        exit 0
    fi
    echo "checksum mismatch; downloading again" >&2
fi

url="https://huggingface.co/$REPO/resolve/$REVISION/$FILE"
echo "downloading $FILE (~2.5 GB) from $REPO @ ${REVISION%"${REVISION#????????}"} ..."
# -C - resumes a partial download in $target.part
curl --fail --location --retry 3 -C - --output "$target.part" "$url"
actual="$(sha256_of "$target.part")"
if [ "$actual" != "$SHA256" ]; then
    echo "checksum mismatch for $FILE: expected $SHA256, got $actual" >&2
    rm -f "$target.part"
    exit 1
fi
mv "$target.part" "$target"
echo "ok: saved and verified $target"
