# bthread initialization order: source checkpoint

This directory contains a standalone Linux diagnostic and a private opt-in Factory patch. It does not add a benchmark runner, product flag, production default, or SDK performance result. The [accompanying report](https://yche.me/design/yalanting-coro-rpc-adaptation-20261111.html#native500-wall-spans-init-order) separates the observed initialization-order behavior from the existing SDK comparison, whose performance target remains unmet.

The files do not certify SDK/Worker smoke results. The historical Factory producer inside earlier binary archives is unknown, so the patch also does not establish an exact historical source delta or a causal old/new scheduler improvement.

## Standalone diagnostic

`bthread_concurrency_probe.cpp` accepts `early8` or `late8` and a fresh absolute destination for a **private copy of its own** `/proc/self/maps`:

- `early8` requests eight before this program initializes a channel or starts its warm bthread.
- `late8` initializes the channel and joins a warm bthread before requesting eight.

The request must precede **any** bthread initialization for an early-initialization experiment; running `early8` does not prove an externally loaded component has not initialized bthread already. Setter behavior and initial getter values depend on the matched bRPC version and configuration. Do not assume a default of nine everywhere or require a particular late-set return value as a portable contract. Inspect the returned fields.

The getter reports configured concurrency. `native_carriers_observed_lower_bound` counts distinct native thread IDs observed executing the diagnostic callbacks. Process native-thread counts also include unrelated runtime threads. Neither quantity enumerates the actual Worker carrier total, and this standalone program does not execute the SDK.

Callbacks deliberately use a native `std::condition_variable`, which blocks carriers. The main thread waits at most 100 ms for callback entry, releases the callbacks, and joins every successfully started bthread before a normal return. A join error terminates the process immediately. This diagnostic changes scheduling and is unsuitable for latency, throughput, CPU, or business-cost measurement. The 100 ms wait is not a whole-process timeout; use an external owner with a bounded runtime and cleanup policy. No runner is supplied here.

An exit of zero means the diagnostic API/copy/completion checks succeeded. It does not establish a particular concurrency result, total carrier count, SDK correctness, or performance acceptance. Raw maps can contain private paths and runtime identities; keep them local and do not commit them.

## External build and runtime

Use Linux, a C++17 compiler, and one compatible external bRPC build. Its headers, library, SSL, crypto, gflags, protobuf, and any remaining dependencies must match. In particular, link the matching SSL and crypto providers explicitly; a bRPC library alone can leave symbols unresolved. Replace **every** placeholder below with your private installation/output paths before use. This is a build sketch, not a portable dependency recipe or a claimed compilation result.

```sh
"<CXX>" -O2 -std=gnu++17 -pthread \
  -I"<BRPC_INCLUDE_DIRECTORY>" \
  -I"<GFLAGS_INCLUDE_DIRECTORY>" \
  -I"<PROTOBUF_INCLUDE_DIRECTORY>" \
  bthread_concurrency_probe.cpp \
  "<MATCHED_BRPC_LIBRARY>" \
  "<MATCHED_SSL_LIBRARY>" "<MATCHED_CRYPTO_LIBRARY>" \
  "<MATCHED_GFLAGS_LIBRARY>" "<MATCHED_PROTOBUF_LIBRARY>" \
  <MATCHED_REMAINING_LINK_FLAGS> -ldl -lrt \
  -o "<PRIVATE_OUTPUT_DIRECTORY>/bthread-init-order-probe"

LD_LIBRARY_PATH="<MATCHED_RUNTIME_DIRECTORY_LIST>" \
  "<PRIVATE_OUTPUT_DIRECTORY>/bthread-init-order-probe" \
  early8 "<ABSOLUTE_PRIVATE_DIRECTORY>/early8.maps"

LD_LIBRARY_PATH="<MATCHED_RUNTIME_DIRECTORY_LIST>" \
  "<PRIVATE_OUTPUT_DIRECTORY>/bthread-init-order-probe" \
  late8 "<ABSOLUTE_PRIVATE_DIRECTORY>/late8.maps"
```

The destination must not already exist. The program requests exclusive creation, rejects a final symlink, and uses mode 0600. Use a private parent directory. Verify the actually loaded runtime locally rather than assuming a successful link selected the intended libraries. No actual maps, link recipes, runtime manifests, hashes, logs, or identity fixtures are included.

## Private Factory hook

`private-bthread8-cold-hook.patch` targets the DataSystem Factory shape shown in the diff. Check that it applies to your exact source before using it. `DS_PRIVATE_BTHREAD_CONCURRENCY` defaults to zero, accepts only zero/eight, and is a private compile-time choice rather than a product configuration flag.

At eight, the existing `EnsureGlobalInitialized()` once-body calls getter/setter/getter before its authentication registration, event-dispatch setup, and local `Channel::Init`. Setter failure or a getter other than eight fails closed through `CHECK_EQ`; this is not a live scheduler-shrink mechanism. The private stderr marker records the scalar request and configuration result, not a measured carrier total. At zero, the added calls and marker are compiled out.

The setting belongs to the process-wide bRPC runtime, including whichever SDK or Worker components use it. It is not a server-only intervention. A private comparison must use the same newly built SDK/Worker pair for both backends and establish the cold scalar checks in both processes before accepting formal windows. New functional/data smoke failures must block formal execution. This checkpoint supplies no such runtime validation and promises no 30% or CPU improvement. Rollback means selecting the original images in fresh processes, not reconfiguring a running scheduler.
