#!/usr/bin/env bash
set -euo pipefail
baseline_sha=${1:?frozen64KbinarySHA256}
candidate_sha=${2:?frozen256KbinarySHA256}
out=${3:?newEvidenceDirectory}
repo=/home/worktrees/rpcbenchmark-coro-bulk-io-20261111
frozen="$repo/build-write256/frozen"
lab=/opt/rpc-framework-lab-tiantiyun-20261108
export PATH="$lab/sysroot/usr/bin:$PATH"
export LD_LIBRARY_PATH="$lab/bin/lib:$lab/sysroot/usr/lib64"
export PYTHONDONTWRITEBYTECODE=1
cgroup=/sys/fs/cgroup$(cut -d: -f3 /proc/self/cgroup)
group="$cgroup/native-write256-gate-$$"
mkdir "$group"
cleanup() {
    rmdir "$group/rpc-server" "$group/rpc-client" "$group" 2>/dev/null || true
}
trap cleanup EXIT
(
    printf '%s\n' "$BASHPID" > "$group/cgroup.procs"
    exec timeout --kill-after=10s 180s python3 "$repo/supplemental/native-bulk-io/run_gate.py" \
        --baseline "$frozen/rpc_coro_write-64k" --baseline-sha256 "$baseline_sha" \
        --candidate "$frozen/rpc_coro_write-256k" --candidate-sha256 "$candidate_sha" \
        --lab-root "$repo" --out "$out"
)
