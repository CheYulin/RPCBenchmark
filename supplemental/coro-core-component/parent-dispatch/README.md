# Retained persistent-parent dispatch experiment

## Scope and ownership

This opt-in diagnostic preserves an unpromoted DataSystem coro dispatch candidate.
It does not change the parent module's default sources or production DataSystem.
One listener has 64 persistent normal parent bthreads. Each accepted transport Job
runs on a fresh normal child started with `bthread_start_urgent`; its parent joins
the child, including bthread TLS destruction, destroys the Job, frees its slot and
retires Dispatcher admission. Business never runs on Asio or a reused parent.
Stop closes admission, waits for reserved factories and joins parents without a
Dispatcher data lock. Child-start failure responds with an error and retires once.

The helper, header, eleven-case unit source and long client are exact tested
private bytes. The small patch transforms the existing generated base core into
the tested adapter; no full additional core is stored. This experiment keeps a
64-job admission limit, allocates a Job, signals a parent, creates a fresh child
and joins it. Neither lower overhead nor a safe production optimization follows
from using urgent start. The production candidate is not promoted.

## Stage the existing closure once

Use the Linux dependencies and matching O2/C++20/PIC command described in the
parent README. Stage into a new directory with the existing parent stager first:

```sh
MODULE=$PWD/supplemental/coro-core-component
EXPERIMENT=$MODULE/parent-dispatch
DS_SOURCE=/path/to/matching-datasystem-source
BUILD=/path/to/new-parent-experiment-build
python3 -B "$MODULE/stage_sources.py" --datasystem-source "$DS_SOURCE" \
  --core-compile-command /path/to/core-command.json --output "$BUILD"
python3 -B - "$EXPERIMENT" "$BUILD" <<'PY'
from pathlib import Path
import hashlib,json,subprocess,sys
experiment,build=map(Path,sys.argv[1:])
ids=json.loads((experiment/'source-identities.json').read_text())
def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()
if sha(build/'base-private-core.cpp') != ids['generated_base_core_sha256']:
    raise SystemExit('generated base core mismatch')
if sha(build/'coro_unary.h') != ids['external_DataSystem_header_sha256']:
    raise SystemExit('DataSystem header mismatch')
sources={name:(experiment/name).read_bytes() for name in ids['source_sha256']}
if any(hashlib.sha256(data).hexdigest()!=ids['source_sha256'][name]
       for name,data in sources.items()):
    raise SystemExit('experiment source mismatch')
destinations=[build/name for name in sources]+[build/'parent-adapter-core.cpp']
if any(path.exists() or path.is_symlink() for path in destinations):
    raise SystemExit('experiment output already exists')
for name,data in sources.items():
    with (build/name).open('xb') as stream: stream.write(data)
target=build/'parent-adapter-core.cpp'
with target.open('xb') as stream:
    stream.write((build/'base-private-core.cpp').read_bytes())
subprocess.run(['patch','--fuzz=0',str(target),'--input',
                str(build/'base-to-parent-adapter.patch')],check=True)
if sha(target) != ids['patched_parent_core_sha256']:
    raise SystemExit('parent adapter mismatch')
print('parent experiment staged; no compilation or runtime qualification')
PY
```

The parent stager owns the single existing fixed-owner helper/header closure.
This overlay calls no second stager and changes none of those files. The unused
fixed-owner and inline diagnostic sources can remain staged; they are not linked
into this experiment's client or parent server.

## Build and run explicitly

First follow the parent README's manual build recipe to establish `CXX`,
`CORE_FLAGS`, `SDK`, `BRPC`, `DEPS`, `LINK_DIRS`, `echo_server_only.o` and the
baseline `echo-server-base`. Use mutually compatible real SDK/bRPC dependencies
and retain the compiler command. Then, in that same staged build directory:

```sh
for source in parent-adapter-core parent_bthread_dispatcher core_parent_probe parent_bthread_dispatcher_test; do
  "$CXX" -I"$BUILD" -I"$DS_SOURCE/src" -I"$DS_SOURCE/include" \
    "${CORE_FLAGS[@]}" -c "$source.cpp" -o "$source.o"
done
"$CXX" -pthread -fPIC -pie echo_server_only.o parent-adapter-core.o \
  parent_bthread_dispatcher.o "$SDK" "$BRPC" "${DEPS[@]}" -ldl \
  "${LINK_DIRS[@]}" -o echo-server-parent
"$CXX" -pthread -fPIC -pie core_parent_probe.o \
  "$SDK" "$BRPC" "${DEPS[@]}" -ldl "${LINK_DIRS[@]}" -o core-parent-probe
"$CXX" -pthread -fPIC -pie parent_bthread_dispatcher_test.o \
  parent_bthread_dispatcher.o "$SDK" "$BRPC" "${DEPS[@]}" -ldl \
  "${LINK_DIRS[@]}" -o parent-dispatcher-test
for case in fresh_child_tls capacity64 stop_held_child stop_reserved_factory \
  retirement_capacity partial_start failed_copy_init child_start_failure \
  join_includes_tls_destruction run_exceptions_drain concurrent_stop_progress; do
  timeout 8s ./parent-dispatcher-test "$case"
done
nm -C --defined-only echo-server-parent | grep ParentBthreadDispatcher
sha256sum echo-server-base echo-server-parent core-parent-probe parent-dispatcher-test
```

Do not link `fixed_owner_plan.o`, fake providers or unit anchors into the client
or servers. Link each server's own core definitions, and verify those definitions
and live SDK/bRPC maps instead of assuming interposition selected the candidate.
The public recipe has not been runtime-qualified; a rebuilt ELF needs its own
correctness, dependency and lifecycle evidence.

Use `echo-server-base` or `echo-server-parent` with the unchanged server CLI
`serve 127.0.0.1 NEW_READY_FILE`. The client accepts
`call-closed HOST PORT shared 32 NEW_OUTPUT_DIR` and retains READY/GO, cooperative
prime, full rows, CPU brackets and normal destruction. Set
`DATASYSTEM_CORO_CALLER_IO=1` and `DS_CORE_SERVER_PID` as described in the parent
README. This long profile has 1,024 warm and 8,192 measured calls per owner,
32 owners, 4 KiB attachments and a one-second core deadline per call. Both server
arms keep the diagnostic inline counter at zero; each complete cohort has 32
prime calls. Keep the finite IDs and null schedule fields. API/byte errors stop
the cohort. There is no offered rate or arrival-debt acceptance criterion.

The declared order is base/parent/parent/base on eight client and eight server
CPUs at SCHED_OTHER/nice0, with metadata on a separate CPU/nice10 and private
loopback/mount isolation. These scripts do not create isolation or an automated
performance campaign. Retain all rows, process births, maps, RSS, CPU brackets and
owned normal cleanup. QPS uses the actual finite cohort's first core begin through
last observer end; it does not establish equal offered load.

## Evidence and no-promotion decision

`source-identities.json` records the exact source and private image hashes. The
private helper's eleven intentional semantic RED cases and eleven GREEN cases
are retained in archive `2b63d15d99eb9e3fd514feb63b628b0a445620026f5d6ab094c0929067a18cc3`.
The RED source was a test comparator, not a claim that production had those bugs.
The separate actual build-only archive
`dc9f3c78c1c4ab7d011268a095f736399ddb1516f94c381f59c2e3985df462dd`
contains fourteen normal build/check commands and nine CLI configuration checks;
it started no service or performance window.

The complete performance archive
`4ddbd5fc9b9fa659cf949c7a80b9c829d3e4d2f24d1f5cb0fcbff4c97d1309d5`
was independently read twice: all 204 regular members/203 payload hashes and
1,179,648 call rows were verified. All warm/measured calls completed without API
errors, byte mismatches, exceptions or cancellation. Eight owned processes closed
normally; the 1,107 pinned bindings and 1,072 build inputs remained unchanged.
The source ledger binds both reader receipts.

| Measured arm | P50 (ns) | P99 (ns) | Actual cohort QPS | Component server CPU ticks |
| --- | ---: | ---: | ---: | ---: |
| base 1 | 144432 | 176902 | 217300.7032213134 | 574 |
| parent 2 | 258090 | 532725 | 117526.82050777311 | 1382 |
| parent 3 | 260516 | 524067 | 116788.71004594961 | 1391 |
| base 4 | 141388 | 173999 | 221617.5724108982 | 568 |

Each measurement contains 262,144 calls. Parent/base arm-median ratios are
P99 **3.011652859353493**, P50 **1.8144496536281576**, QPS
**0.533847742421338**, and component server process CPU per completed call
**2.4281961471103326**. CPU uses aggregate process user+system ticks, not
production Worker CPU. Client maximum RSS was about 262–263 MB over its complete
lifetime, including ledger formatting; it is not measurement-only RSS or a memory
improvement. Full byte values and process-CPU evidence are retained in the ledger.

The decision is **REJECT_PARENT_PROMOTION**. The production candidate patch
`72c546f95f37b524932f2d0ca4cc9825f741306f38149a8ac37a1f5c4ea33be9`
is not applied; DataSystem stays at its original implementation. Unit correctness
and successful linking did not yield a benefit in this declared experiment.
This is an echo-only finite closed-loop mechanism screen with different realized
QPS between arms, not SDK Get/Set, a new bRPC comparison, matched offered load,
production runtime qualification or the user's RPC P99 30% target. Historical
failed windows and their quality gates remain unchanged.
