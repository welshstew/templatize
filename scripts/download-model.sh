#!/bin/sh
# Download a GGUF for the optional LLM detector, pinned to an exact repository
# revision and verified by SHA-256. Run on the host (build online); the
# containers only ever read the file (run offline).
#
#   download-model.sh [DIR] [default|large]
#
#   default: Qwen3-4B-Instruct-2507 (Apache-2.0), Q4_K_M, ~2.5 GB. CPU or GPU.
#            https://huggingface.co/unsloth/Qwen3-4B-Instruct-2507-GGUF
#   large:   Qwen3-30B-A3B-Instruct-2507 (Apache-2.0), Q4_K_M, ~18.6 GB.
#            Mixture of experts (~3B active per token); needs a 24 GB GPU.
#            https://huggingface.co/unsloth/Qwen3-30B-A3B-Instruct-2507-GGUF
set -eu

DIR="${1:-models}"
VARIANT="${2:-default}"

case "$VARIANT" in
    default)
        REPO="unsloth/Qwen3-4B-Instruct-2507-GGUF"
        REVISION="a06e946bb6b655725eafa393f4a9745d460374c9"
        FILE="Qwen3-4B-Instruct-2507-Q4_K_M.gguf"
        SHA256="3605803b982cb64aead44f6c1b2ae36e3acdb41d8e46c8a94c6533bc4c67e597"
        SIZE="~2.5 GB"
        ;;
    large)
        REPO="unsloth/Qwen3-30B-A3B-Instruct-2507-GGUF"
        REVISION="eea7b2be5805a5f151f8847ede8e5f9a9284bf77"
        FILE="Qwen3-30B-A3B-Instruct-2507-Q4_K_M.gguf"
        SHA256="6c997b8af17debdfb01d890214400ccbab00db6acc0ba8da5de1cc906c4774d0"
        SIZE="~18.6 GB"
        ;;
    *)
        echo "unknown model variant: $VARIANT (expected: default or large)" >&2
        exit 2
        ;;
esac

sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1
    else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

mkdir -p "$DIR"
target="$DIR/$FILE"

# One download per file at a time: two runs appending to the same .part corrupt it,
# and the losing run's checksum failure then deletes the other's file mid-write.
lock="$target.lock"
if ! mkdir "$lock" 2>/dev/null; then
    echo "another download of $FILE is running (lock: $lock)." >&2
    echo "If none is, remove the lock directory and run again." >&2
    exit 1
fi
trap 'rmdir "$lock" 2>/dev/null' EXIT INT TERM
if [ -f "$target" ]; then
    echo "verifying existing $target ..."
    if [ "$(sha256_of "$target")" = "$SHA256" ]; then
        echo "ok: $target is already present and verified"
        exit 0
    fi
    echo "checksum mismatch; downloading again" >&2
fi

# A complete .part (e.g. an earlier run was interrupted after curl finished) must
# not be "resumed": curl would append the server's 416 error body to it.
if [ -f "$target.part" ] && [ "$(sha256_of "$target.part")" = "$SHA256" ]; then
    mv "$target.part" "$target"
    echo "ok: completed earlier download verified, saved $target"
    exit 0
fi

url="https://huggingface.co/$REPO/resolve/$REVISION/$FILE"
echo "downloading $FILE ($SIZE) from $REPO @ ${REVISION%"${REVISION#????????}"} ..."
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
