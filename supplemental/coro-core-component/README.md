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
The paired dispatch screen instead keeps the same shared-client source and topology and
compares two private server cores. Both add the same counter/getter; only the
inline variant adds the exact `diag.echo` branch after admission and attachment
ownership. `diag.prime` and other methods retain production bthread dispatch.
The inline handler only moves in-memory bytes: **never use it for real Worker or
potentially blocking business operations**. No production flag is added.

`core_closed_probe.cpp` asks a separate closed-loop question with one shared
Client and at most 32 outstanding calls. It keeps 128 warm and 256 measured calls
per owner, but each owner starts its next call after its previous observer
returns. There is no offered rate, arrival schedule or debt gate;
`scheduled_ns` and `dispatch_checked_ns` are null. Each echo request retains the
core's one-second deadline. API/byte errors or exceptions abort while retaining
all planned IDs. This profile does not rescue the rejected fixed-R500 screen.

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

"$CXX" -I"$BUILD" -I"$DS_SOURCE/src" -I"$DS_SOURCE/include" \
  "${CORE_FLAGS[@]}" -c core_closed_probe.cpp -o core_closed_probe.o
"$CXX" -pthread -fPIC -pie core_closed_probe.o \
  "$SDK" "$BRPC" "${DEPS[@]}" -ldl "${LINK_DIRS[@]}" -o core-closed-probe
sha256sum core-closed-probe
```

The closed client reuses the same base/inline server recipe and sources above.
Do not link `fixed_owner_plan.o` into `core-closed-probe`; it has no pacing plan.
The DataSystem source and header must match the hashes in `design.json` and
`source-identities.json` (core `23bb0057...`, header `91d46287...`). All three
component C++ sources are copied into the same single fixed-owner staging closure.

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

The nine CLI rejection checks require the actual newly built Linux closed-client
ELF and its compatible libraries. Run them manually; the filename deliberately
does not match automatic `test*.py` discovery:

```sh
DS_CORE_PROBE_BINARY="$BUILD/core-closed-probe" \
  python3 -B "$MODULE/manual_cli_config.py"
```

These checks reject invalid inputs before client/server construction or outputs;
they do not run a workload. The private ELF passed all nine checks, but a public
rebuild must establish its own result.

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

The separate closed client accepts
`call-closed HOST PORT shared 32 NEW_OUTPUT_DIR`; the old `call ... 500` syntax
is rejected rather than aliased. It uses the same READY/GO, cooperative prime,
owned cleanup and server QUIT sequence. Its phase IDs are finite call ordinals,
not offered arrivals. Retain the null schedule fields and all actual preparation,
core-call and observer timestamps. QPS is the completed count divided by the
first core begin through last observer end, including finite startup/drain.
Native owners join before client destruction and final evidence is written.
Do not manufacture scheduled E2E, an offered rate or SDK admission qualification.

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
high-load contention.
The previous source-only checkpoint's pending paired status is retained as
history in `evidence.json`; the following are later, separate raw results.

The fixed-R500 paired dispatch attempt stopped after its first base window.
Warm 4,096 calls succeeded; measure had 7,204 successes and 988 cancelled IDs,
with one observer debt violation. That row combined 23.972940 ms dispatch
lateness and 48.996414 ms core-call time (72.984208 ms scheduled-to-observer).
The prior same-owner call had already completed 63.511080 ms before its schedule.
No inline window ran. No partial-measure quantile or QPS is published, and this
observation does not identify a scheduler, network, bthread, VM or adapter cause.
The original rejected matrix stays rejected.

The independent closed-loop32 base / inline / inline / base campaign completed
all 49,152 warm+measure calls with zero errors, byte mismatches or cancellations.
Post-Stop branch counts were 0 / 12,288 / 12,288 / 0. Inline/base arm-median ratios
were core-call P99 `0.7363652113652114`, P50 `0.7624884537847988`, and actual finite
cohort QPS `1.3077632586446206`. Each measure cohort lasted only about 29–40 ms;
the arms have different actual rates and queueing. This is not same-offered-load
or sustained-capacity acceptance. Process CPU ticks have 10 ms resolution and
are too coarse here for precise per-RPC CPU attribution. All three full raw
archives and independent reader identities are bound in `evidence.json`.

The net echo-path difference cannot separate both handoffs, guarantee a
mathematical lower bound, or establish SDK Get/Set/RPC30 benefit. The added
relaxed atomic and branch/layout changes remain part of that screen. Historical
calibration failures stay rejected. There is no production deployment,
feature-OFF/Bazel/package or full-CI qualification here.
