# Bounded read-cache singleflight experiment

This follow-up to the rejected [singleflight experiment](../native-singleflight/README.md) passed the frozen **native 20% P99 screening gate at concurrency 32**. It has not established the DataSystem Get/Set versus bRPC 30% target. The root build and existing benchmarks remain unchanged.

## Contract and ownership

The exact pinned, DataSystem-patched YLT base from the earlier experiment is required. The generator first applies that experiment, then adds a lazily allocated read cache of at most 8 KiB per connection and removes the separate coroutine-lambda frame. The client mode remains immutable. Only bounded plain TCP requests with an external caller deadline and a negative transport timeout are accepted; normal pipeline clients keep their original path.

The shared connection control owns the reader after the socket wrapper, so the reader is destroyed first. Singleflight connect and call operations require the owner executor and reject overlap. Reset awaits socket closure/drain, destroys the cached reader, and then replaces the socket. An actual-socket visitor refuses unsupported transport types. Response and attachment storage remain separate from the cache, so returned views survive later calls and owner destruction.

Only the header is read through the cache. Payload reads first consume bytes already in the cache, with synchronous `read_some` called only while `in_avail()` is positive under the exclusive executor. Any remaining payload is read directly into result storage through the raw socket. Large responses are not forced through repeated 8 KiB fills. Header sequence, magic, version, type and response caps remain validated before result allocation. At 64 connections the cache adds at most 512 KiB per channel; wider production memory and capacity qualification remain required. No persistence format changes.

## Validation and reproduction

Thirty-nine release and ASan/UBSan tests passed, including leak and stack-after-return detection; FD counts returned to baseline. New tests cover rejecting connect during a live call, rejecting concurrent connects, fragmented header/body/attachment, large-to-small reuse, a 1 MiB response, tiny positive limits and empty values, cancellation during partial cache fill, and reconnect after a coalesced malformed response. The active-connect regression failed on the prior implementation. Deliberately removing cache destruction from reset failed the unread-cache regression before another read was attempted.

Use the exact prepared base include tree described in the preceding experiment, a fresh output directory, and GCC 15 with C++20:

```bash
python3 supplemental/native-buffered-singleflight/build_native.py \
  --ylt-include /path/to/prepared/ylt/include --out /path/to/new-build --cxx g++
/path/to/new-build/singleflight_test
```

The test build enables a narrowly scoped cache-state accessor. Performance builds do not enable it. `apply_overlay.py --omit-reset` generates the deliberate mutation for the regression test; do not use it outside that test. Sanitizer compiler/runtime provenance is the same as the preceding experiment. One sanitizer SSH connection closed after the remote test completed; retained stdout recorded all 39 tests passing, stderr was empty, and independent inspection found no remaining build/test process. No test or performance window was rerun because of this transport failure. Public fixture formatting preserves all tested non-whitespace tokens and quoted literals.

## Fixed eight-window native result

The same binary runs both modes. Each concurrency uses pipeline/singleflight/singleflight/pipeline, 4 KiB bidirectional echo, 1 s warmup, 3 s measure, four server I/O workers, numeric pre-resolved endpoints and a 1 s external deadline with a negative transport timeout in both arms. Resources, quality gates and CPU accounting match the preceding experiment. All eight windows completed, every response and count was checked, source hashes stayed fixed, and owned role groups were empty after cleanup. See [the complete result](result-20261115.json).

At concurrency 32, P99 changed **224 to 174 us** and **205 to 161 us**, reductions of 22.3% and 21.5%. Arm-median throughput increased 28.1%, while whole-lifecycle CPU per successful request fell 17.2%. Both conservative P99 pairs passed the 20% screen; QPS and CPU gates passed at both concurrencies. Concurrency-1 P99 was diagnostic and includes one regression, 37 to 46 us. Control P99 drift was 8.48% at concurrency 32 and 9.76% at concurrency 1.

This was a shared host with uncalibrated observer overhead. CPU includes client setup, phase leads, drain and teardown plus server waiting, normalized by warmup plus measurement successes; it is not pure RPC or measure-only CPU. There is no bRPC arm or actual DataSystem API in this native screen. Passing it only permits the next integration experiment. DataSystem P50/P99, SDK end-to-end latency, throughput/CPU, mixed/peak load, aggregate memory, OOM recovery and production lifetime qualification remain unproven.
