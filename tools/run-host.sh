#!/usr/bin/env bash
set -euo pipefail
if [[ $# -ne 3 ]]; then
    echo "usage: $0 <release-build> <evidence-directory> <run-id>" >&2
    exit 2
fi
build=$(realpath "$1")
evidence=$(realpath "$2")
run_id=$3
runner=$(realpath "$(dirname "$0")/run_matrix.py")
[[ "$run_id" =~ ^[a-z0-9-]+$ ]] || exit 2
out="$evidence/$run_id"
mkdir "$out"
monitors=()
cleanup() {
    for pid in "${monitors[@]}"; do kill "$pid" 2>/dev/null || true; done
    for pid in "${monitors[@]}"; do wait "$pid" 2>/dev/null || true; done
}
trap cleanup EXIT
export LC_ALL=C
{
    date -u -Is
    uname -srmo
    rg '^PRETTY_NAME=' /etc/os-release || true
    lscpu | sed -n '/^CPU(s):/p; /^Model name:/p; /^NUMA node(s):/p; /^Thread(s) per core:/p; /^Hypervisor vendor:/p'
    uptime
    free -m
    df -h "$evidence" | sed -n '1,2p'
    g++ --version | head -1
    if command -v cmake >/dev/null; then
        cmake --version | head -1
    fi
    protoc --version || true
    cat /proc/sys/kernel/sched_schedstats
    cat /proc/sys/kernel/perf_event_paranoid
    sha256sum "$build"/rpc_{brpc,coro} | sed 's@ /.*\/rpc_@ ./rpc_@'
} > "$out/environment.txt"
for path in /proc/stat /proc/diskstats /proc/interrupts; do
    if [[ -r "$path" ]]; then cat "$path" > "$out/$(basename "$path").before"; fi
done
if command -v iostat >/dev/null; then
    taskset -c 15 iostat -y -c -d -x 1 > "$out/iostat.txt" 2>&1 &
    monitors+=("$!")
fi
if command -v dstat >/dev/null; then
    taskset -c 15 dstat --nocolor -cdngy 1 > "$out/dstat.txt" 2>&1 &
    monitors+=("$!")
fi
systemd-run --quiet --wait --pipe --collect --unit="rpc-lab-$run_id" \
    --property=Delegate=yes --property=AllowedCPUs=15-31 --property=CPUAccounting=yes \
    --setenv="LD_LIBRARY_PATH=${LD_LIBRARY_PATH:-}" \
    --setenv="PATH=$PATH" \
    python3 "$runner" --build "$build" --out "$out/cases" \
    --server-cpus 16-23 --client-cpus 24-31 --monitor-cpu 15 \
    --rounds "${RPC_ROUNDS:-3}" --seconds "${RPC_SECONDS:-5}" --warmup "${RPC_WARMUP:-2}" \
    --require-cgroup | tee "$out/runner.log"
for path in /proc/stat /proc/diskstats /proc/interrupts; do
    if [[ -r "$path" ]]; then cat "$path" > "$out/$(basename "$path").after"; fi
done
date -u -Is > "$out/finished-utc.txt"
