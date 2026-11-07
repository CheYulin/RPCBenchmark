#!/usr/bin/env bash
# An observational run separate from the throughput/latency matrix.
set -euo pipefail
if [[ $# -ne 2 ]]; then
    echo "usage: $0 <release-build> <output-directory>" >&2
    exit 2
fi
build=$(realpath "$1")
out=$2
mkdir "$out"
server=
client=
cleanup() {
    for pid in "$client" "$server"; do
        if [[ -n "$pid" ]]; then kill "$pid" 2>/dev/null || true; fi
    done
}
trap cleanup EXIT
for engine in brpc coro; do
    port=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')
    taskset -c 16-19 "$build/rpc_$engine" server --port "$port" --workers 4 \
        > "$out/$engine-server.stdout" 2> "$out/$engine-server.stderr" &
    server=$!
    for _ in {1..80}; do
        if grep -q READY "$out/$engine-server.stdout"; then break; fi
        sleep 0.05
    done
    grep -q READY "$out/$engine-server.stdout"
    taskset -c 24-27 "$build/rpc_$engine" client --port "$port" --workers 4 --concurrency 32 \
        --payload 64 --warmup 2 --seconds 6 --timeout-ms 1000 \
        > "$out/$engine-client.json" 2> "$out/$engine-client.stderr" &
    client=$!
    for _ in {1..180}; do
        if grep -q WARMUP_DONE "$out/$engine-client.stderr"; then break; fi
        sleep 0.05
    done
    grep -q WARMUP_DONE "$out/$engine-client.stderr"
    taskset -c 15 perf record -e cpu-clock -F 149 -p "$server" \
        -o "$out/$engine.perf.data" -- sleep 4 > "$out/$engine-perf.log" 2>&1
    wait "$client"
    client=
    kill "$server"
    wait "$server"
    server=
    perf report --stdio --no-children --sort comm,dso,symbol --percent-limit 0.5 \
        -i "$out/$engine.perf.data" > "$out/$engine-symbols.txt" 2>> "$out/$engine-perf.log"
done
