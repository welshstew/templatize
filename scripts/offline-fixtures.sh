#!/bin/sh
# Run every fixture through suggest -> build -> round-trip check (with variants
# when present). Designed to run inside the app container with --network none.
set -eu
dir=${1:-/fixtures}
fail=0
count=0
for f in "$dir"/*.*; do
    [ -f "$f" ] || continue
    base=$(basename "$f")
    stem=${base%%.*}
    suffix=${base#*.}
    set --
    for v in "$dir/variants/$stem".v*."$suffix"; do
        [ -f "$v" ] && set -- "$@" --variant "$v"
    done
    count=$((count + 1))
    if out=$(templatize build "$f" --accept-all "$@" 2>&1 >/dev/null); then
        echo "PASS $base${1:+ (with $(($# / 2)) variant(s))}"
    else
        echo "FAIL $base"
        echo "$out" | sed 's/^/    /'
        fail=1
    fi
done
[ "$count" -gt 0 ] || { echo "no fixtures found in $dir"; exit 1; }
[ "$fail" -eq 0 ] && echo "all $count fixtures round-trip offline"
exit "$fail"
