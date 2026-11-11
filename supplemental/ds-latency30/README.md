# Matched offered-load SDK preflight

This independent Linux C++17 tool prepares a fixed-arrival API workload for the latency goal. It changes neither
the DataSystem transport nor the existing native benchmark. The same driver binary, SDK binary, Worker binary,
flags, CPU masks, payload, rate and accounting contract must be frozen before selecting the backend externally.

Current status: pure-helper C++17 compilation and fake-clock RED/GREEN passed with GCC11.5. The preserved old
contract compiled then failed as expected (exit1, fast10/slow3); the new contract and all six fake-clock scenarios
passed (exit0). Source/evidence archive SHA256:
`cb54115349a4ce89b10a420e1cd8d80e993d2c0983b181e3fa1931186f56555f`.
The SDK driver compiled with the original SDK driver's GCC15.2.1/C++17/O2/strict-warning flags against frozen B3
public headers and library. Four low-rate functional windows (Get/Set x bRPC/coro, 1000ops/s, 1s warmup + 2s
measurement, 32 callers) passed using **explicit 100us bounded spin**: 8000/8000 measured arrivals and 4000/4000
warmup arrivals timely, zero bad/pending outcomes, full-byte checks and clean shutdown. Measured generation P99
was 1/1/34/29us; maximum lateness 43.654/28.791/57.115/50.414us; burst always 1.
Actual B3 DSOs and SDK phase affinities/TIDs were checked. An initial 20us-spin warmup was correctly rejected for
generation lateness (P99 2058us/max 6.707ms), despite all 1000 calls succeeding. It is preserved as invalid.
The four-window source/evidence archive SHA256 is
`9164613d9fd91ed63d200e849f5498d3dddc105c47659ef05a03296f4ad67474`.
No matched-load performance or RPC probe experiment has run. Functional delivery at 1000/s does not establish
generator precision at future 50%/80% capacity rates or a 30% latency improvement. These standalone compiler
checks do not establish CMake/Bazel integration. No local toolchain installation was performed.

## Bounded scheduling and ownership

One producer plans `epoch + floor(id * 1e9 / rate)` independently of completion. IDs are exactly those scheduled
in `[epoch,end)`; 128-bit multiplication and time range are checked. There are at most 2,000,000 records, 65,536
queued IDs and 128 callers; defaults are 256 queued IDs and 32 callers. Record/FIFO storage and per-caller Get
buffers are allocated before arrivals. Histograms and raw serialization occur after the phase. A shared SDK and
caller pool persist across warmup and measurement. Each caller owns one deterministic key; Get validates every
returned byte. Set checks status on every call plus a final read of every key. The latter verifies final state,
not every intermediate Set value.
Seed 64 keys (or the caller count if larger). Get selects `arrival_id modulo key_count`, independent of completion;
Set uses its caller's exclusive key to avoid concurrent writers, as in the previous SDK driver.

The mutex protects the active cohort pointer, records, FIFO and counters. A caller's `working` reservation keeps
the stack-owned cohort alive through its SDK call and bookkeeping. The producer owns the schedule, not SDK work.
Separate work/accounting condition variables avoid waking idle callers for each completion. Actual API starts
have an atomic side channel so pending callers remain visible in the cutoff receipt. Each caller's Linux TID
is read once and persisted beside every sample.

An ID not enqueued before `end` is rejected with its original scheduled timestamp. Already queued work may drain,
subject to its original deadline. No retry, replacement arrival, caller creation or growing queue hides overload.
`deadline = scheduled + budget` includes queue wait and a second expiry check immediately before API entry.
Public `KVClient::Get` / `Set` lack a per-call timeout: `ConnectOptions.requestTimeoutMs` is fixed for the client.
The driver **does not pass remaining budget into the SDK**. A dispatched success after its original deadline is
late; real SDK arrival-deadline enforcement needs a separate API change.

Generation P99 lateness, maximum lateness and consecutive catch-up burst are validity gates. Defaults are
100us / 1ms / 64 IDs; freeze explicit tolerances before timing. Failed target delivery is an invalid generator,
not reduced load or a product win. Producer observation and enqueue lateness have separate histograms.

## Resources, measurements and failure

Launch under the existing eight-physical-core SDK mask. Pass one reserved producer core and the other seven SDK
cores; reused SMT siblings, extra/missing CPUs and outside-mask CPUs fail startup. Existing and future SDK
threads use those seven; the producer pins itself to its one core. Total resources remain eight SDK cores.
Worker uses its separately frozen eight-core mask. Absolute sleep is followed by fixed bounded spin
(CLI default 20us, maximum 100us; the validated functional recipe explicitly uses 100us).
Whole-process SDK CPU includes producer spin.

Every planned ID is serialized, including unissued/rejected/expired/pending IDs:
`id, linux_tid, scheduled, deadline, observed, enqueue, dequeue, logical_dispatch, api_start, api_end, validation_end`.
Missing timestamps are `-1` and absent API status is `null`, never a zero-duration success. Outcomes are disjoint:

```
planned = producer_observed + producer_unissued
producer_observed = queue_admitted + queue_rejected
queue_admitted = dispatched + expired_in_queue + pending_in_queue
dispatched = timely_success + late_success + rpc_error + mismatch + pending_inflight
```

The scheduled-arrival cohort retains completions after `end`. API service is `api_end-api_start`; arrival E2E is
`api_end-scheduled`. Same-ID queue delay plus service equals E2E. Post-API byte-validation cost is separate.
Histograms use 1us upper bounds through 2048us and approximately 0.1% maximum relative width above that.
Exact nanosecond sums use 128-bit integers. Pool matching bins/counts/sums across rounds; a median of window
P99s is not a pooled P99.

CPU snapshots retain read-before/read-after bounds, ticks, threads and RSS for SDK and Worker. Separate endpoints
cover pre-arrival, fixed-phase begin/end and cohort-drain. Fixed-CPU-interval starts/completions use the interior
read bounds; nominal phase starts/completions are separate. Do not normalize fixed-window CPU by all eventual
cohort completions as an exact cost. Pre-arrival-to-drain includes the intentional one-second readiness lead,
producer work and active drain. Seed, warmup and final Set verification lie outside measured cohort snapshots.

The initial plan closes before publishing a cohort. Normal completion writes raw IDs and a summary.
Drain is bounded by `end+drain`; timeout persists a pending checkpoint then exits3, instead of detaching SDK calls
from their shared client. Unexpected cohort exceptions exit4 and retain the initial denominator as unresolved.
A crash or missing final receipt is invalid/unresolved. Acceptance requires exit0, valid warmup and measurement,
a valid final receipt, conservation, and all planned IDs timely: no rejection/expiry/error/mismatch/pending ID.
Exit2 is a completed invalid preflight; exit1 is startup/configuration failure. The supervisor also needs a hard
timeout. Receipts include actual checkpoint time; scheduler delay at cutoff must remain visible.
If the supervisor is descheduled across the cutoff, recorded completions after it invalidate the window even
when the condition-variable predicate is already satisfied on return; these late records are retained.

## TDD and build recipe

The preserved RED model extracts the original `sdk_backend.cpp::Benchmark` loop:
`for (start=Clock::now(); start<measureEnd; start=Clock::now()) { synchronous SDK call; ... }`.
Its fake RPC advances time; fast/slow completions yield 10/3 starts instead of the same three fixed arrivals.
The identical completion-independence test must compile successfully then exit1 against the old model.
GREEN switches planner selection, then covers rational schedule/overflow, lateness, bounded FIFO/no duplicates,
expiry/errors/late success, drain/pending conservation and pooled statistics without networking.

Run only in the authorized non-timing build slot:

```sh
CXX=g++ python3 supplemental/ds-latency30/run_fake_clock_tests.py --out "$EVIDENCE/pacer-tests"
CXX=g++ python3 supplemental/ds-latency30/build_sdk_driver.py \
  --sdk-include "$SDK_INCLUDE" --sdk-lib "$SDK_LIBRARY" --out "$EVIDENCE/paced-sdk-build"
```

Receipts bind exact sources, compiler, commands, expected RED and GREEN exits. SDK build paths are explicit.
Use the existing controlled runtime library environment. Before A/B timing, run a short functional preflight:

```sh
taskset -c "$SDK_EIGHT_CPU_LIST" "$EVIDENCE/paced-sdk-build/paced_sdk" \
  --host "$WORKER_HOST" --port "$WORKER_PORT" --worker-pid "$WORKER_PID" \
  --out "$EVIDENCE/preflight" --log-dir "$SDK_LOG_DIR" \
  --producer-cpu "$PRODUCER_CPU" --sdk-cpus "$SDK_SEVEN_CPU_LIST" \
  --op get --rate "$OFFERED_API_OPS_S" --payload 4096 --callers 32 --queue 256 \
  --measure-ms 4000 --warmup-ms 1000 --budget-ms 2000 --drain-ms 3000 \
  --sdk-timeout-ms 2000 --spin-us 100 --p99-lateness-us 100 --max-lateness-us 1000 --max-burst 64
```

Use fresh output directories. Preserve failed preflights. Do not lower observed rate, retime arrivals, add cores
or tune the generator after one arm. A necessary harness fix requires a new frozen stage and both arms rerun.
No production flags or persistent formats change; rollback is simply not using this tool.

## External RPC probe boundary

The driver reports API and arrival timing only and always marks RPC goal eligibility false without a companion
probe. It never invents CSend/CRecv. Associate a common per-method span with the same Linux TID and
`api_start <= CSend <= api_end` using exact monotonic nanoseconds and an explicit RPC ordinal. Retain drain
Create/Publish spans for a Set scheduled before the endpoint; filtering only `CSend<end` changes the cohort.
One Set may contain multiple RPC spans: API ops/s and RPC calls/s are distinct. Require observed/lost event counts,
unique association and all timestamps; missing/lost/ambiguous spans make the RPC comparison ineligible.
Client/server local-clock intervals stay within their own clock domain.
