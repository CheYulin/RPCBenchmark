#!/usr/bin/env bash
set -euo pipefail

if (( $# != 1 )); then
    printf 'Usage: %s <path-to-yalantinglibs-source>\n' "$0" >&2
    exit 2
fi

probe_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ylt_source=$(cd -- "$1" && pwd)
probe_build=$(mktemp -d)
trap 'rm -f -- "$probe_build/coro-thread-probe"; rmdir -- "$probe_build"' EXIT

"${CXX:-g++}" -std=c++20 -O0 -DNDEBUG \
    -I"$ylt_source/include" \
    -I"$ylt_source/include/ylt/thirdparty" \
    -I"$ylt_source/include/ylt/standalone" \
    "$probe_dir/coro-thread-probe.cpp" -pthread -ldl \
    -o "$probe_build/coro-thread-probe"

for repeat in {1..5}; do
    timeout 20 "$probe_build/coro-thread-probe" direct
    timeout 20 "$probe_build/coro-thread-probe" offload
done
