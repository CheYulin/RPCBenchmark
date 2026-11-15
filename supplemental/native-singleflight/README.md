# Singleflight client experiment: rejected

## Contract and decision

This opt-in experiment removes the pipeline Promise/Future, response-handler map and unused request timer from an exclusively leased client with one outstanding request. The native **20% P99 screening gate failed**. It is preserved as an experiment and is not integrated into DataSystem. The repository's default build and existing benchmarks do not apply the patch.

The constructor tag fixes the client mode. A singleflight client rejects pipeline calls; a normal client rejects singleflight calls. Only bounded plain TCP calls with an external deadline and a negative transport timeout are accepted. An atomic admission guard rejects overlapping calls before writing. Framing, sequence, body/attachment limits, typed decoding, error closure and response ownership remain checked. Connect retains its original timer.

The overlay requires the exact DataSystem-patched YLT `45c6463c0afbcc46262e373effa30635be3846a0` header, including `third_party/patches/yalantinglibs/connect-lock-and-socket-retry.patch` from DataSystem `04804feb0`. Its SHA256 is `c616782568336a0398640b80eed3fd501a72291098c95e468dd3a5730a16e923`. Other headers are refused. `apply_overlay.py` generates the patch without changing its input. The prototype copies the typed decoder body to leave the original pipeline decoder untouched; this is not a maintained upstream API.

## Native validation

Thirty cases passed in release mode and under ASan/UBSan, including leak and stack-after-return detection: connection reuse, wrong mode and timeout rejection before write, malformed headers and lengths, truncated bodies/attachments, overlapping calls, partial read/write cancellation, stale FIN and reconnect, destruction during a live read, small borrowed views after owner destruction, external attachment buffers and fallback, attachment-size boundary, owning/string-view/void/structured results, and application errors followed by connection reuse. FD counts returned to baseline.

The tests require caller-owned arguments and executor/client lifetimes through completion and drain. They do not establish arbitrary concurrent connect/reset or OOM recovery. Wider transport modes, production fault injection, and DataSystem mixed-load, peak-capacity and memory qualification remain unrun. Do not deploy this rejected overlay.

## Reproduction

From this repository's root, use a fresh output directory and the exact prepared YLT include tree:

```bash
python3 supplemental/native-singleflight/build_native.py \
  --ylt-include /path/to/prepared/ylt/include --out /path/to/new-build --cxx g++
/path/to/new-build/singleflight_test
```

The tested compiler was GCC 15.2.1-7.1 with C++20/O2/NDEBUG. Sanitizer validation used O1, `-fsanitize=address,undefined`, `-static-libasan -static-libubsan`, leak checking and stack-after-return detection. Signed Red Hat UBI GCC 15.2.1-7.4 sanitizer archives were extracted into the private experiment directory; no system package was installed. Fixture formatting preserves every non-whitespace token and quoted literal of the tested source.

`run_screen.py` expects its generated build directory, delegated cgroup v2 accounting, and metadata placement on CPU 35 at nice 10. The historical run used a transient systemd scope and private mount/network namespaces with only loopback enabled. Client CPUs were 1/3/5/7 and server CPUs 17/19/21/23; the script verifies distinct physical cores. CPU assignments are intentionally explicit. The script creates `screen-v1` once, stops at the first invalid window, and does not retry or add windows. Its resource setup is not a portable default benchmark runner. The public plan retains the preparation date; the private raw archive retains its precise clock anchor.

## Eight-window result and limits

Each concurrency used pipeline/singleflight/singleflight/pipeline, 4 KiB echo, 1 s warmup, 3 s measurement, four server I/O workers, the same client binary, pre-resolved numeric endpoints, negative request timers and a 1 s caller deadline. Every response was byte checked, all calls completed successfully, histograms conserved counts, physical connections matched concurrency, source hashes stayed fixed, and role groups were empty after cleanup.

At concurrency 32, paired P99 changed from **213 to 182 µs** and **199 to 183 µs**: improvements of 14.6% and 8.0%. Arm-median throughput rose 12.4%, and whole-lifecycle CPU per successful call fell 8.0%. Both P99 pairs failed the required 20% reduction; the decision is rejected. Concurrency-1 P99 was diagnostic, including one regression. Full values and both directional pairs are in [result-20261115.json](result-20261115.json).

CPU is the sum of two role cgroup deltas from server READY/before client spawn through client exit/before server stop, divided by warmup plus measurement successes. It includes client setup, phase leads, drain/teardown and server waiting; it is not pure RPC CPU. The host was shared, observer overhead was not calibrated, and concurrency-32 control P99 drifted 6.57%. Fine histogram buckets are 1 µs; screening uses conservative bounds. Native echo does not establish a DataSystem or bRPC speedup, and the DataSystem 30% goal remains unmet.
