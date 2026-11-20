# Optional native server I/O polling experiment

This separate source checkpoint adds an opt-in polling loop to the existing native `coro_io::io_context_pool` worker. It changes no default benchmark entry, DataSystem source, SDK binary, wire format, or bthread dispatch policy. The default macro value is zero: the worker executes the original `io_context::run()` branch.

With `DS_CORO_IO_NATIVE_POLL_US=2000`, each native worker blocks in `run_one()`, then polls its own context until one fixed deadline 2000 microseconds later. Empty polls and callbacks do not renew that deadline. It then returns to `run_one()`. Existing native TLS, executor ownership, work guards, thread creation/join, promise completion and stop behavior remain in place. There is no external executor, nested run loop, bthread polling runner, or forced-stop shortcut.

## Prerequisites and patch

Use an existing YLT include tree with its normal Asio/async_simple dependencies. The unmodified `include/ylt/coro_io/io_context_pool.hpp` must have SHA256:

```text
ec89b5a52612c4db06cfe7d0f65d71c07c0017bcf5a847ebcf0ab2f21e980160
```

The pool patch is an additional patch for the existing dependency tree. A DataSystem component consumer still requires its previously patched YLT configuration, including connection recovery and bounded singleflight support; this directory supplies neither those changes nor a replacement dependency build.

Apply the patch to a separate include-tree copy. Set `NATIVE_POLL_YLT_INCLUDE` to that copy's `include` directory:

```sh
patch -d "$NATIVE_POLL_YLT_INCLUDE" -p1 < io_context_pool-native-poll.patch
```

The resulting pool header has SHA256:

```text
e0848a6f00af8692a2d5f6d07b16ea99f45f45993db1fff69e12a798b5e49bd4
```

## Build the actual consumer

Build the relevant consumer translation unit twice with the existing project's C++20 compiler, dependency includes and link libraries. Use these target-specific definitions:

| Variant | Compiler definition |
| --- | --- |
| Control | `-DDS_CORO_IO_NATIVE_POLL_US=0` |
| Candidate | `-DDS_CORO_IO_NATIVE_POLL_US=2000` |

Use one coherent physical YLT include tree for every header in a build. Point the YLT root, thirdparty and standalone include paths at that same patched tree, and remove competing paths to the old tree. Merely prepending a directory containing the modified pool header is insufficient: quoted includes can still reach the original file while angle includes reach the overlay, so `#pragma once` sees two physical files and duplicate definitions result. An undefined macro selects the original zero branch. Keep the macro consistent in all translation units that instantiate the selected pool; then relink the consumer.

A definition supplied only to a test, client, or unused object does not prove that the server uses the candidate. Record the actual header dependency, compiler arguments, link inputs and selected executable/DSO implementation. Inline/weak symbol selection can choose another definition, so inspect the native worker loop in the resulting ELF before calling it the 2000-microsecond variant. Public rebuilding inherits no qualification from any other binary.

## Three real-Asio test cases

Build `native_poll_pool_test.cpp` twice against the patched tree, once with each definition above, using the existing YLT consumer's dependency configuration. This test translation unit defines `DS_CORO_IO_NATIVE_POLL_TEST_PHASE` before including the pool header. The hook observes only the three polling phase edges; leave it undefined in component and runtime builds.

Run each case in a fresh process under an external eight-second watchdog:

```sh
timeout 8s ./pool-zero 1
timeout 8s ./pool-zero 2
timeout 8s ./pool-zero 3
timeout 8s ./pool-2000 1
timeout 8s ./pool-2000 2
timeout 8s ./pool-2000 3
```

| Case | Behavior checked | Zero exit | 2000 exit |
| --- | --- | --- | --- |
| 1 | Immediate/deferred callbacks retain native context/TLS identity; executor-owned data and native TLS teardown finish before return | 0 | 0 |
| 2 | A fixed idle polling window ends without renewal, returns to blocking, and accepts newly posted work | 1 | 0 |
| 3 | Normal stop waits for a held callback and an outstanding real timer, then joins its native worker | 0 | 0 |

Case 2 deliberately expects a behavioral failure for the control, whose original `run()` has no active polling phase. A compiler/link failure or watchdog termination is not that expected result. These cases use real Asio tasks and timers but no network, SDK workload or server request handler.

## Evidence boundary and rollback

This directory contains source and expected test outcomes. It contains no actual trace, ledger, process identity, host configuration, private build inputs, runtime configuration or measured benefit. Any later execution results should be reported separately as aggregates with their own source/build binding.

The fixed polling window is not a hard CPU cap or a real-time deadline: polling consumes CPU, repeated activity can sustain polling, and an individual callback can run beyond the deadline. The tests establish lifecycle/policy behavior only. They do not qualify observer overhead, a server component, Get/Set performance, causal network or scheduling improvements, or the requested 30% RPC-P99 reduction.

Select zero or leave the macro undefined to restore the original loop. Remove the patch to restore the original dependency header. Existing default benchmarks remain unchanged.
