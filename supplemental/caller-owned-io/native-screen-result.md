# Native calling-model screen: NOT_MET

## 1. Fixed conditions and evidence

On 2026-11-11, C (shared-pool coroutine), S (shared-pool sync caller), and O (caller-owned io_context) used the same GNU11.5/C++20/O2/NDEBUG strict build, unchanged native Echo server, four server IO workers, four physical client cores and four physical server cores. Each fresh window used localhost TCP, 4 KiB payload, depth1, 1 s warmup and 3 s measurement. At c1 then c32, the fixed order was C/S/O/O/S/C, 12 windows total. Physical TCP connections equalled concurrency; full bytes and cohort/histogram accounting passed.

There were **4,225,950 measured and 1,416,681 warmup successes**, 0 RPC errors and 0 mismatches. Frozen binary, seven-script package and private raw-archive hashes are in [native-screen-result.json](native-screen-result.json). Raw private paths/PIDs/source packages are not published. C1 is diagnostic only. The distinct author/reviewer recomputation identities are recorded under `verification_provenance` in the JSON, with their respective SHA256 values; neither role overwrites the other artifact.

## 2. Whole-client CPU and quality contract

CPU starts after observed server READY and before client launch, and ends after observed client exit and before server stop. For each window, divide that window's two-role cgroup CPU delta by its **warmup.success+measured.success**. Across the 12 windows those denominators sum to **5,642,631**; the table reports medians of the per-window CPU/RPC values. This includes client application setup, warmup/lead/measurement/drain/teardown and observed server wait CPU. It excludes server startup/final destruction and external observer/launcher CPU; child preexec/exec after role placement is included. It is not pure-phase CPU.

All raw CPU reads and bounded clock anchors are retained. The 495 known-workload guard samples had no foreign/uncertain hit, maximum gap 128.361 ms. All 652 child and 560 parent clock anchors passed the fixed width/step/boot checks. Source43/two ELF and runtime package bindings held, actual dynamic library paths/inodes/hashes matched pre/post/terminal, and the private delegated scope and two role groups were empty/removed. These sampled checks do not prove the absence of every possible host task.

The earlier V1 stopped in its first c1/C window because its marker-based CPU-start proof failed: zero valid windows, remaining 11 NOT_RUN. The unchanged old package/raw stay separate. V2 predeclared the whole-client CPU definition before any valid comparison; it did not enlarge the 30 ms condition or combine old results.

## 3. Results and locked decision

| Concurrency / model | Median QPS | Median P50 us | Median P99 us | Whole-client CPU us/completed RPC |
| --- | ---: | ---: | ---: | ---: |
| 1 / C | 32,244.7 | 31.0 | 43.0 | 30.671 |
| 1 / S | 23,316.6 | 43.0 | 48.5 | 49.921 |
| 1 / O | 31,087.7 | 32.0 | 37.0 | 32.491 |
| 32 / C | 292,766.1 | 109.0 | 116.5 | 22.056 |
| 32 / S | 151,546.2 | 206.0 | 386.5 | 35.718 |
| 32 / O | 173,327.2 | 184.0 | 233.5 | 32.122 |

The predeclared c32 O/S gate required P99 ratio <=0.80, QPS >=0.97 and CPU/RPC <=1.03 in **both pairs and the ratio of arm medians**. P99 improved 10.27% / 54.71% per pair and 39.59% at arm medians. The first pair failed 20%, so the decision is **NOT_MET; no promotion, no added windows**. QPS improved 12.85% / 15.93% (median 14.37%); whole-client CPU/RPC fell 9.47% / 10.66% (median 10.07%). These favorable metrics cannot override the failed paired P99 gate. S P99 was 263/510 us versus O 236/231 us; no unmeasured fluctuation cause is asserted. Primary P99 values are in the <=1024us fine buckets.

## 4. Resource and interpretation limits

At c32 the sampled client peak thread counts C/S/O were **10/42/65**, and FDs were **47/47/131**. Zero explicitly created O IO runners does not mean fewer total library threads or lower CPU budget. All recorded thread affinities stayed inside each fixed role mask. Old sync creates/joins N caller threads per phase; Owned retains them across warmup/measurement. Compare complete calling models, not isolated context-switch/wakeup costs; no first samples were removed.

Native echo is different from DataSystem's protobuf/attachment bridge and Worker business path. This closed-loop, n=2 engineering screen proves neither a production speedup nor DataSystem RPC P99 <=70% of bRPC. Exact absolute client phase endpoints are not emitted by these binaries; process envelopes and bounded dual-clock anchors are retained without fabricating those endpoints. Full per-window ratios, raw histogram/CPU accounting and all failed attempts remain available by their frozen hashes.
