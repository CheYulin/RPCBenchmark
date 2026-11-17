# Actual DataSystem Get/Set capacity: tail regression retained

The bounded singleflight client is integrated in DataSystem commit `7f7e7e34be66eb97bbca4a8643f8c9f1fa589f78`; the measured SDK SHA256 is `74112f13e18ca727216d1f87b06b98b5e775932d9dcdc5c7c44d03af056308c3`. All four SDK functional flows and 27 component tests passed before this experiment. This directory records actual SDK API capacity evidence, not RPC transport latency. **The DataSystem RPC P99 reduction of 30% remains unverified.**

## Fixed workload and complete result

Eight windows ran Get then Set, each in bRPC/coro/coro/bRPC order: 32 persistent synchronous callers, 4 KiB payload, 10 s warmup, 30 s measure, the same SDK and Worker binaries, eight distinct physical CPUs per role, equal nice-10 scheduling, and isolated client/Worker network namespaces. Caller-owned IO is enabled only for coro; bRPC remains the default. The Worker retains its original four coro server IO threads. Every window completed without API errors, Get mismatches, or warmup failures. Actual DSO mappings, transport routes, source/binary bindings and owned process/network cleanup were checked. [All values and paired comparisons](result.json) are retained. Public JSON floating values are rounded to six decimal places; the private raw archive retains full precision.

| SDK metric, arm medians | Get coro / bRPC | Set coro / bRPC |
| --- | ---: | ---: |
| API P50 | 0.500 | 0.819 |
| API P99 | 1.859 | 2.541 |
| QPS | 1.559 | 1.016 |
| CPU per successful API | 0.880 | 0.983 |

Get improved median API latency and throughput, but its tail became worse. Set improved P50 modestly, with little throughput gain and a larger tail regression. Control P99 drift was 1.01% for Get and 0.97% for Set. These are capacity results at equal concurrency and resources; achieved offered rates differ. They do not establish a same-arrival-rate speedup or justify deployment for a P99 target.

A separate fixed-arrival plan retained 5,000 API arrivals/s, 32 callers, queue 256, a 2 s budget, and 10/30 s phases. Its first bRPC Get control rejected 5 of 150,000 measured arrivals; Set rejected 5 of 50,000 warmup arrivals. Generation lateness remained below the original 1 ms limit. Both matrices stopped and later windows stayed unrun. They were not retried, filtered, or counted as valid comparisons. Their original denominators and failures are included in the JSON.

## Reproduction and limits

Use the existing `tests/perf/rpc_compare/coro_backend/sdk_backend.cpp` benchmark from the DataSystem PR, built against the measured SDK. The root RPCBenchmark build is unaffected. With the same resource allocation and a dual-listener Worker, run each mode in the declared order:

```bash
DS_BENCH_WORKER_HOST=192.0.2.1 DS_BENCH_PAYLOAD_BYTES=4096 \
  "$SDK_BACKEND" bench-get "$WORKER_PORT" "$LOG_DIR" 32 10 30 "$WORKER_PID"
```

For coro, add `DATASYSTEM_RPC_BACKEND=coro_rpc`, `DATASYSTEM_CORO_RPC_PORT` and `DATASYSTEM_CORO_CALLER_IO=1`; Set uses `bench-set`. Put the expected SDK directory first in `LD_LIBRARY_PATH` and verify the mapped library identity. The historical run used client CPUs 1/3/5/7/9/11/13/15, Worker CPUs 17/19/21/23/25/27/29/31, etcd CPU 33 and metadata CPU 35. The original 64-key dataset and TCP-only Worker configuration were unchanged.

All Get bytes are checked by this driver. Set checks status per call; write correctness is established separately by the four SDK functional flows, not a per-call readback in this benchmark. Calls started before the measure cutoff and completed afterward remain included. CPU snapshots end at the fixed cutoff and exclude the final drain. The inherited latency-vector reserve can grow during measurement. CPU per successful API includes both SDK and Worker process CPU, including background work; endpoint thread/RSS samples are not continuous peaks. The host was shared and observer overhead was not calibrated. No csend/crecv RPC timing or scheduled-arrival SDK E2E timing was collected in the capacity run.

A source-backed follow-up hypothesis is the server idle timer: DataSystem supplies five seconds, while the native benchmark uses YLT's default zero. The pinned [connection implementation](https://raw.githubusercontent.com/alibaba/yalantinglibs/45c6463c0afbcc46262e373effa30635be3846a0/include/ylt/coro_rpc/impl/coro_connection.hpp) cancels and rearms a timer per request. Timer coalescing has not been implemented or measured here and is not an established cause of the tail regression. Any candidate must preserve idle eviction, long-running request semantics, bounded connection memory and safe cancellation on close.
