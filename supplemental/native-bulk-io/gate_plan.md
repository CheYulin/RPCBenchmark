# Native bulk-write candidate gate

This is a private, bounded native echo experiment, not a DataSystem score or rollout approval. Product B3df and historical f92/e31 artifacts remain immutable. Exactly one candidate changes write completion granularity from default64KiB to256KiB; reads, protocol, handler, payload validation, logical concurrency, connections and admission are unchanged. Both binaries use the same mixed entry,lab.cpp,pinned headers,compiler/link flags, differing only by the opt-in write macro. Record all source/overlay/binary hashes and commands before timing; do not replace old artifacts.

Use one server process/4IO executors pinned to physical CPUs4,6,8,10 and one client process/4IO executors pinned to20,22,24,26; monitor CPU36, with no physical sibling overlap. Each case uses32 TCP connections,depth1,coroutine callers. A fresh server starts per window. Every call keeps full-byte comparison,per-call histogram,error counters and bounded timeout. Submission owns128-concurrency/lifecycle/error preflight plus new mixed-entry smoke checks; all must pass before root GO for timing.

Run exactly12 clean windows:1MiB/c32,4KiB/c32,and mixed4KiB/c24+1MiB/c8. Each case has two paired rounds AB/BA:64K,256K,256K,64K. Warmup1s and measure4s follow the fixed harness. No extra chunk sizes, profiling,syscall tracing or perf events in clean windows. Record individual windows,paired deltas and medians; do not pool payload classes into one primary QPS/P99.

Mixed uses a single Runtime and the exact existing Echo/requestOnce/runPhase functions. The first24 lanes carry4KiB and the last8 carry1MiB, interleaving round-robin so each executor handles6tiny+2bulk connections. All32 connections are established before a common warmup phase. One measurement Phase defines the same start/end for both classes, then all requests drain. Both class reports use `max(scheduled_end,all32lastCompletion)` as a common denominator end. Their latency histograms remain separate. Save phase start/end/common drain-end,verify32real connections,24/8active lanes and valid hist totals. One process avoids two independent warmup clocks or doubling client IO to8threads.

Use the existing cgroup role accounting from WARMUP_DONE through process completion, explicitly including phase lead/drain and reporting actual window length. For pure cases compare total(server+client)CPU microseconds/success. For mixed report per-roleCPU cores and CPU/GiB of weighted application payload, not a fictitious CPU attribution to tiny/bulk. Mixed class QPS and tinyP99 are primary fairness results; bulkQPS and itsP99 remain visible. Counter-only/entry-only instruments count EAGAIN and lifecycle work and must never be mixed into these clean values.

Predeclare a conservative screening gate, not statistical significance from two samples:

- Hard reject on any warmup/RPC/mismatch failure,missing/zero active lane,wrong connection count,phase/histogram inconsistency,timeout,crash,hash drift or incomplete window.
- Pure1MiB: candidate medianQPS at least5%higher and totalCPU/success at least5%lower; both pairedQPS deltas must bepositive and both pairedCPU deltas negative.
- Pure4KiB and mixedtiny: candidate median tinyQPS no more than3%lower; median tinyP99 no more than5%higher and neither paired tinyP99 more than10%higher. Report both paired tails even when gate passes.
- The fixed histogram uses1us buckets through1024us and coarse power-of-two buckets above it. If any tinyP99 exceeds1024us, this bounded screen cannot establish a5%tail margin; conservatively reject migration as unresolved rather than claiming equality from matching coarse buckets.
- Mixed bulkQPS is reported independently; do not accept an apparent totalQPS gain produced by starving bulk. Require median mixedbulkQPS no more than3%lower as well.
- Ambiguous/noisy/borderline outcomes reject migration under this bounded screen; do not expand or tune the threshold after results. Passing only makes a separately reviewed DS pilot worth considering, not authorized production adoption.

Optional syscall density is a separately labelled diagnostic arm only if root authorizes it after clean timing. Otherwise use existing packet/granularity source evidence and leave native syscall density unmeasured. No kernel-wide offload change or read-side experiment is part of this gate.
