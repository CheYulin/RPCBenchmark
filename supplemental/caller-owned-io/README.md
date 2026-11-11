# Caller-owned IO experiment (private; functionally validated)

## 1. Model and ownership

This opt-in C++20 client owns one io_context/executor/client per native caller thread and calls the unchanged pipeline Echo server at depth 1. Existing coroutine/shared-pool sync source, route, statistics and original targets remain unchanged. There are no DataSystem or YLT changes. This native experiment is not the DataSystem RPC30 acceptance test.

`Owner` does not call `io_context.stop()` or create a work guard. New submissions end before teardown: close the client, pump until natural idle with zero root operations/native requests, reset the client, then pump to natural idle again before destroying the executor/context. `done`, `poll()==0`, or native request count alone is insufficient. A 2 s cleanup deadline exits nonzero with `CALLER_IO_DRAIN_FAILURE` while the owner is still alive; it never abandons the owner and continues. An external supervisor also bounds the entire test. A nonzero exit invalidates a performance window.

Each request retains YLT's initial `.via(executor).start()` post; the same caller executes it using `run_one_until`. This model creates no separate background IO runner and differs from the rejected initial-directStart experiment. Logging/global helper threads may still exist. JSON reports actual process thread counts, owner TIDs, ready FD count and configured IO runner count separately. Top-level callbacks check both owner thread identity and the active owned executor. Socket/connect/request timer/recv/close followups must remain on the owned context; a new global-executor followup would invalidate the drain proof.

The small Echo definition matches the old function's anonymous namespace/name/signature. Interoperability with the old server, native send/response two-await semantics and all-byte validation are covered by the functional tests. `workers=0` reports model-owned background IO workers, while `requested_workers` preserves the compatibility CLI input. It does not mean the entire process has zero threads. Linux `/proc` is required for the FD/TID checks.

## 2. Executed build and functional checks

On 2026-11-11, a fresh hash-bound snapshot built with GNU C/C++ 11.5.0, C++20, `-O2 -DNDEBUG -Wall -Wextra -Werror`, and the original YLT `45c6463` archive (SHA256 `baa6b5c9ffcc0af8252742bba8f0fb9c6d37387c13b2ff5b26d29c6aaff6ee37`). The old pipeline and new caller-owned client used the same compiler and flags.

The first compile attempt exposed a missing vendored Asio include path for the new Asio test target. Adding its `include/ylt/thirdparty` path fixed the build closure. The failed compile remains recorded and is not semantic RED. An earlier attempt stopped before compilation when a startup guard detected foreign IPC processes; none were killed.

Actual results:

- The deliberate naive done-only policy returned exit 1: 4 failing checks and 1 passing check. This is the semantic negative control, not a claim that the old pipeline is broken.
- The real drain helper passed 5/5 checks; real Asio passed 9/9, including queued followups, timer cancellation, context restart, unresolved work, retained-owner cleanup after deadline, a continuously ready callback chain, and owner-thread callbacks.
- CTest passed 6/6: the positive/negative helper tests, Asio test, new caller lifecycle and both old pipeline suites. Old native coroutine and shared-pool sync modes were covered.
- The new lifecycle script checked 4 KiB c1/c32 route/bytes/owner-TID reporting, 64 repeated owner constructions with stable FD/thread counts, reconnect/restart, a deliberate peer RST followed by explicit reconstruction, mismatch, 1 ms timeout against a 10 ms handler, and refused connection.

Reproduction commands (use an existing exact archive and an exclusive functional slot):

```sh
cmake -S . -B build-caller-io -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_C_COMPILER=/usr/bin/gcc -DCMAKE_CXX_COMPILER=/usr/bin/g++ \
  -DCMAKE_CXX_FLAGS='-Wall -Wextra -Werror' \
  -DCMAKE_CXX_FLAGS_RELEASE='-O2 -DNDEBUG' -DCMAKE_C_FLAGS_RELEASE='-O2 -DNDEBUG' \
  -DRPC_LAB_BUILD_BRPC=OFF -DRPC_LAB_BUILD_CORO=ON \
  -DRPC_LAB_BUILD_PIPELINE_EXPERIMENT=ON -DRPC_LAB_BUILD_CALLER_IO_EXPERIMENT=ON \
  -DRPC_LAB_YLT_ARCHIVE=/path/to/exact-ylt-45c6463.tar.gz
cmake --build build-caller-io --parallel 4 --target \
  rpc_caller_io_drain_negative rpc_caller_io_drain_test rpc_caller_io_asio_test \
  rpc_coro_caller_io rpc_coro_pipeline
# Expected exit 1, with the failing semantic checks retained.
build-caller-io/rpc_caller_io_drain_negative
build-caller-io/rpc_caller_io_drain_test
build-caller-io/rpc_caller_io_asio_test
ctest --test-dir build-caller-io -R 'rpc_caller_io_|rpc_coro_pipeline_' --output-on-failure
```

The real run bound six code/build inputs by hash. This README was updated afterwards; it is a documentation-only difference from that tested snapshot. Any future code/build change requires new binding and validation. The prior missing local CLT discovery was not counted as RED.

## 3. Memcheck and FD evidence

Four functional Memcheck arms passed: normal repeated-FD/reconnect, peer-reset/reconnect, timeout/destruction and connect-refused/destruction. Each raw log reported `ERROR SUMMARY: 0`, all heap blocks freed and 0 bytes/blocks in use at exit. Thus definite, indirect, possible and reachable allocations were all zero for these four standalone-client runs. This does not describe the separate DataSystem Worker/SDK cohorts or prove every workload leak-free.

Each process exited with three inherited descriptors. The normal and RST in-process verification outputs recorded stable `live_fds=4` and `process_threads=1` after respectively four and two repeated two-stage teardowns; this is a stability check, not an assertion that a running process has zero descriptors. The CTest lifecycle also passed 64 repeated constructions. Sanitizers were not run.

The Memcheck command used `--error-exitcode=86 --leak-check=full --show-leak-kinds=all --errors-for-leak-kinds=definite,indirect --track-fds=yes` and a 45 s external per-arm bound. Normal/RST returned 0; the intentional timeout/refused-connect cases returned the expected 1. `--verify-rounds N` performs two requests per iteration with close/reconstruction on the same owner context; it is not a performance mode. `--verify-first-error 1` is only for the deliberate first-RST case.

## 4. Integration limits

The CMake option `RPC_LAB_BUILD_CALLER_IO_EXPERIMENT` remains OFF by default. No Bazel target was added or tested for this prototype. The functional CMake run enabled both opt-in native experiments; it does not establish packaging parity or future toolchain compatibility.

Startup guards inspect known IPC/boost/iceoryx, native/DataSystem, probe and build processes, stop on a match or uncertain read and never terminate foreign processes. They are snapshots, not proof of continuous host exclusivity. The successful functional attempt finished with no matched workload/compiler/probe process or network namespace remaining.

The fixed native screen of 12 calling-model windows completed with **NOT_MET**: its favorable median did not override the first paired P99 improvement of 10.27%, below the predeclared 20%. See [the result and limits](native-screen-result.md) and [sanitized data/hash bindings](native-screen-result.json). No mixed-load or DataSystem RPC30 acceptance was run. Functional/Memcheck success and this native result do not imply a production gain or authorize extra timing windows. Keep exact bindings and all failed attempts when integrating this stage.
