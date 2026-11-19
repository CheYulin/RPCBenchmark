# Coro core echo component diagnostics

## Scope and profiles

This optional module calls the actual DataSystem coro unary `Client` and `Server`
with a memory-only echo handler. It does **not** call SDK Get/Set or compare
bRPC. The fixed profile has 32 native owners, 4 KiB attachments, a global rate of
500 calls/s, and 128 warm plus 256 measured calls per owner. It retains every ID,
byte check and original timestamp. Dispatch or observer completion at or beyond
the next owner arrival, 64 ms, aborts the cohort. Lateness above 1 ms is recorded
without a soft-budget abort; this is a separate component diagnostic, not a pass
under either historical SDK admission protocol.

`core_shared_owner_probe.cpp` compares one shared Client with 32 per-owner
Clients. A cooperative `diag.prime` barrier establishes 32 simultaneous calls.
The paired dispatch screen instead keeps the same shared client process and
compares two private server cores. Both add the same counter/getter; only the
inline variant adds the exact `diag.echo` branch after admission and attachment
ownership. `diag.prime` and other methods retain production bthread dispatch.
The inline handler only moves in-memory bytes: **never use it for real Worker or
potentially blocking business operations**. No production flag is added.

## Stage and build on Linux

Supply the exact DataSystem core source identified in `design.json`, its matching
public/private headers and C++20 dependency tree, a real SDK DSO, and its actual
bRPC dependency. Python 3, GNU C++15, Bash and system `patch` are required.
The following stages sources only; it does not compile or run them:

```sh
MODULE=$PWD/supplemental/coro-core-component
DS_SOURCE=/path/to/matching-datasystem-source
BUILD=/path/to/new-component-build
python3 "$MODULE/stage_sources.py" --datasystem-source "$DS_SOURCE" \
  --core-compile-command /path/to/core-command.json --output "$BUILD"
```

The command JSON contains `argv` and `cwd` from the actual core compilation.
Alternatively use `--compile-commands` with a database containing
`coro_unary.cpp`. Original defines, forced includes and O2/C++20/PIC options are
retained; source, output and dependency-output arguments are removed. Paths must
refer to the current machine. The stager calls the existing
`fixed-owner-sdk-rpc.build_local.stage_sources` exactly once into the same
directory. Plan, ABI and patched helper headers therefore have a single staged
identity. No existing module is edited. The generator verifies source hashes and
produces two cores; the repository does not carry three full core copies.

Example manual build, using trusted, mutually compatible SDK dependencies:

```sh
set -euo pipefail
CXX=${CXX:-g++}
SDK=/path/to/sdk/libdatasystem.so
BRPC=/path/to/matching/libbrpc.so
export LD_LIBRARY_PATH=/path/to/sdk:/path/to/dependency/lib:${LD_LIBRARY_PATH:-}
mapfile -d '' -t CORE_FLAGS < <(python3 - "$BUILD/compile-flags.json" <<'PY'
import json,sys
for item in json.load(open(sys.argv[1])):
    sys.stdout.buffer.write(item.encode()+b'\0')
PY
)
mapfile -d '' -t DEPS < <(python3 - "$SDK" <<'PY'
import subprocess,sys
text=subprocess.check_output(['ldd',sys.argv[1]],text=True)
if 'not found' in text: raise SystemExit('unresolved SDK dependency')
for line in text.splitlines():
    row=line.split()
    if len(row)>=3 and row[1]=='=>' and row[2].startswith('/'):
        if not row[0].startswith(('libc.so','libm.so','libgcc_s.so','libstdc++.so','ld-linux')):
            sys.stdout.buffer.write(row[2].encode()+b'\0')
PY
)
LINK_DIRS=()
for library in "$SDK" "$BRPC" "${DEPS[@]}"; do
  directory=$(dirname "$library")
  LINK_DIRS+=("-L$directory" "-Wl,-rpath,$directory" "-Wl,-rpath-link,$directory")
done
cd "$BUILD"
for source in core_shared_owner_probe fixed_owner_plan echo_server_only base-private-core inline-private-core; do
  "$CXX" -I"$BUILD" -I"$DS_SOURCE/src" -I"$DS_SOURCE/include" \
    "${CORE_FLAGS[@]}" -c "$source.cpp" -o "$source.o"
done
"$CXX" -pthread -fPIC -pie core_shared_owner_probe.o fixed_owner_plan.o \
  "$SDK" "$BRPC" "${DEPS[@]}" -ldl "${LINK_DIRS[@]}" -o core-owner-probe
for arm in base inline; do
  "$CXX" -pthread -fPIC -pie echo_server_only.o "$arm-private-core.o" \
    "$SDK" "$BRPC" "${DEPS[@]}" -ldl "${LINK_DIRS[@]}" -o "echo-server-$arm"
  nm --defined-only "echo-server-$arm" | grep -E 'DSCoroDiagInlineEchoCount|Server5Start'
done
sha256sum "$SDK" "$BRPC" core-owner-probe echo-server-base echo-server-inline
```

These public staging/build instructions have not been runtime-qualified. The
verified private probe ELF is identified in `evidence.json`; a rebuild is a new
ELF and does not inherit that qualification. `capture_helper.cpp`, fake providers
and unit anchors are not linked into these component executables. This is not a
full compiler, header or dependency-closure attestation.

The source-generation checks need the real pinned source, not a fake SDK:

```sh
cd "$MODULE"
DS_CORE_SOURCE_FILE="$DS_SOURCE/src/datasystem/common/rpc/coro_unary.cpp" \
  python3 -m unittest -v test_generate_core_variants
```

## Run and retain evidence

The probe CLI is `serve HOST READY_FILE` or
`call HOST PORT shared|per-owner 500 NEW_OUTPUT_DIR`. The separate echo server
accepts only `serve 127.0.0.1 READY_FILE`; it has no client mode. Configure
`DATASYSTEM_CORO_CALLER_IO=1` and `DS_CORE_SERVER_PID` for the client. Start the
server, obtain its dynamic port from its ready file, then start the client.
After client READY, verify owned process/TID identities, library maps, roles and
namespaces before sending `GO\n`. Server `QUIT\n` or EOF releases prime before
`Server.Stop`. The paired server prints `inline_echo_count` **after Stop**:
expect 0 for base, 12,288 for inline after a complete client cohort; 32 prime
calls are excluded. The counter counts branch entries, not successful replies.

The observed experiment used eight distinct physical NUMA1 cores per role,
normal SCHED_OTHER/nice0 for client/server, and separate metadata CPU/nice10 in a
private loopback network and mount namespace. No launch/controller or host
isolation is installed by this module. Retain ready/primed topology, full
`all-calls.jsonl`, metric/CPU brackets, post-destruction topology and normal owned
cleanup. Do not silently drop late/error IDs or reuse an output directory.

## Recorded evidence and limits

The verified shared/per-owner/shared-control order was
shared / per-owner / per-owner / shared, once only. All 49,152 warm+measure rows
were complete and byte-correct, with eight normal process closures. Measured
core Call P99 median was 2.07% lower with per-owner Clients, while P50 median was
2.28% higher. Same-mode P99 repeat variation was 9.70% and 11.85%. Both arms had
32 socket FDs and 131 total FDs at prime, but 36 versus 67 tasks: 31 extra cleanup
threads are a confound. A prime snapshot does not establish active connection
distribution throughout measurement.

This weakens the claim that shared-lane locality dominates this low-load echo
profile; it does not justify a production sticky-lane policy or exclude
high-load contention. This checkpoint contains no paired dispatch results.
Its net echo-path difference cannot separate both handoffs, guarantee a
mathematical lower bound, or establish SDK Get/Set/RPC30 benefit. The added
relaxed atomic and branch/layout changes remain part of that screen. Historical
calibration failures stay rejected. There is no production deployment,
feature-OFF/Bazel/package or full-CI qualification here.
