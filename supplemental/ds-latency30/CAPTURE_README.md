# Private paced SDK capture checkpoint

## 1. Scope and verified state

This opt-in driver integrates the unchanged ABI2 collector with an actual measurement `libdatasystem.so`.
It preserves the existing fixed-arrival planner, producer, public SDK options, queue/deadline accounting and
three API begin/end/validation clock statements. The collector/provider and its sole TLS live in the SDK;
the executable links no `capture.cpp`, fake SDK, Observe implementation or second provider. The copied
`capture_abi.h` and selector-test `capture_observe.h` are byte-identical declarations from
`../ds-rpc-capture/`; that core's ten code inputs are unchanged.

GNU15.2.1 C++17/O2 with `-Wall -Wextra -Werror -fPIE -pie -fno-plt` compiled and linked this exact driver
against stock C1 SDK headers/library. Driver SHA256:
`461f1049faa0b68badc9404ab4724ef0d2709204f281d53f0a9811f90ea60ffa`.
The deliberately naive selector compiled and completed its core lifecycle, then failed at the selector
comparison with exit1; the actual selector/core passed all 34 independently frozen goldens with exit0.
An earlier source using `decltype(&dlclose)` failed GNU15 strict compilation with `ignored-attributes`;
it is preserved as a build failure, not semantic RED. The named noexcept deleter fixes that cold owner type
without suppressing warnings or changing handle lifetime. Build/unit archive SHA256:
`70bec5bb95ea7315a64825bf777d828b4e1278fd54fdded14c4858cbc7afb502`.

Two actual SDK functional stages subsequently passed:

| Stage | Observed evidence |
|---|---|
| Finite SDK, bRPC/coro × 4KiB/1MiB × disabled/enabled | 8/8 processes passed; each enabled case captured one Get and one Publish with exact ordinal/End/method accounting. Disabled cases had two End rows and no RPC coverage. Full-byte and final Set oracle, SDK shutdown/destruction/Close passed. |
| This paced driver, bRPC/coro × Get/Set × stock/disabled/enabled, 4KiB | 12/12 passed; 32 same-thread primers per case, Set adds same-thread Get readbacks; one warm API and one measured API per case, 24 total, all timely with no rejection/error/mismatch/pending request. |

The paced stage preserved 32 registered TID slots per measurement-SDK run, append-only warm→measure byte
prefixes, exact API/End timestamp joins and per-slot method/counter/ordinal accounting. Final prefixes contained
16 End rows and eight enabled RPC rows across the eight disabled/enabled cases. Stock had no getter or RPC
prefix; disabled capture never fabricated valid RPC coverage. Actual SDK inode/base and getter/table/all nine
functions matched the one expected SDK; Worker remained stock. All SDK processes exited0. In paced12, Worker
exited0 after its bounded termination request; finite8 required verified own SIGKILL under its shorter cleanup
allowance, so no graceful Worker exit is claimed there. Owned process/network-namespace cleanup passed. Raw archives:
finite `69f6ae6104075b52895948126df7d1d688fa5e49e7a093ea373c82a50bd6e254`,
paced `f26d28d9f6dcee18e9b5a9cd913049f3be08542d135794bc9c876e044b43afa6`.
The paced startup-v1 socket-directory length failure and its 12 NOT_RUN cases remain separate evidence.

These were functional coexistence runs, not quiet performance runs. One API per phase establishes no P99,
throughput, CPU/request, 3% observer budget or RPC30 improvement. Paced 1MiB/mixed coverage and sustained
matched-load acceptance are not established by this checkpoint.

The first actual RPC diagnostic screen is retained as **INVALID**: its first bRPC Get warmup completed
all5000 APIs successfully, but maximum generation lateness was4.319ms, exceeding the unchanged1ms gate.
Measurement and the other three arms remained NOT_RUN. Its287 captured RPCs are diagnostic evidence from
that failed warmup, not a backend comparison, performance gain or observer-budget result. Raw archive:
`de12c3a8bbaddb488d93de6e84555d47f519132d4c3ca222139e1cefe019bf2e`.

## 2. Identity, bounds and ownership

`DS_BENCH_RPC_CAPTURE_MODE=off|stock|disabled|enabled` defaults off. Off performs no SDK getter lookup,
capture allocation or CABI hot call. Each opt-in mode requires `--prime-per-caller 1` and
`DS_BENCH_RPC_CAPTURE_SDK` naming the actual already-loaded SDK. The public static CUDA registration function
is an address anchor only and is never called. Full maps reject a second/deleted SDK image, wrong inode/base
or loader interposition; `RTLD_NOLOAD` handle-only lookup rejects a getter/table/function supplied by another
DSO. Stock requires an absent getter; disabled/enabled require the exact ABI2 table in the actual SDK.

Only the declared Bound/default TCP route is eligible: Get requires GET and Set requires PUBLISH. The outer
fixture must exclude IPC/SHM/RDMA and prove role-specific maps/environment. The unchanged SDK bridge counts
all ten exact business methods; unknown or failed/malformed controls stay conservative failures. This driver
does not widen masks to conceal route changes. Opt-in Get clears its caller-owned output before API begin to
reject stale bytes while retaining capacity.

The deterministic selector uses phase/arrival ID, never completion order. Default seed is
`0x4d595df4d0f33173`, modulus1024; decimal environment overrides must be frozen before a run. Before Arm,
enumerate both finite plans' selected IDs and reserve their entire count ×16 RPC rows per caller, plus256
error rows, and the entire selected End count plus256 error Ends. This assumes no balanced assignment.
Reject zero-selected plans, more than2,000,000 planned APIs, core per-array limits or arrays plus1MiB metadata
reserve exceeding128MiB. No ring, overwrite, runtime growth or reserve borrowing hides loss.

Each persistent stdcaller registers its cached Linux TID while UNARMED, completes its successful same-operation
primer, then participates in the original readiness gate. Begin consumes existing scheduled/API-begin scalars;
End consumes existing end/validated scalars before `working--`. Under the shared dispatch mutex,
producerDone/queueempty/working0 precede NORMAL snapshot and final Disarm. Warm prefixes never reset or
Disarm. Stale/BUSY tokens remain sticky invalid; one fresh token may only seal retained invalid resources.
Normal order is seal → owner-thread unregister → join → final Set oracle → SDK ShutDown → Close → release
buffers/SDK pin. Unresolved work retains owners under the existing supervisor/_Exit policy; no live caller is
detached. Partial output exports immutable prefixes and null unavailable counters.

## 3. Build, selector tests and finite output

Use an authorized non-timing build slot, GNU15 and fresh output directories. The build script records exact
commands, sources, compiler, SDK hash and result, including failed builds:

```sh
CXX="$GNU15_CXX" python3 supplemental/ds-latency30/build_sdk_driver.py \
  --sdk-include "$SDK_INCLUDE" --sdk-lib "$STOCK_SDK_LIBRARY" --out "$FRESH_BUILD"

"$GNU15_CXX" -std=c++17 -O2 -pthread -Wall -Wextra -Werror \
  -I supplemental/ds-latency30 -I supplemental/ds-rpc-capture \
  -DDS_TEST_BAD_DRIVER_SELECTION supplemental/ds-latency30/selection_test.cpp \
  supplemental/ds-rpc-capture/capture.cpp -o "$OUT/selection-red"
# Require actual exit1 at the final selector comparison, not arbitrary failure.

"$GNU15_CXX" -std=c++17 -O2 -pthread -Wall -Wextra -Werror \
  -I supplemental/ds-latency30 -I supplemental/ds-rpc-capture \
  supplemental/ds-latency30/selection_test.cpp \
  supplemental/ds-rpc-capture/capture.cpp -o "$OUT/selection-green"
# Require exit0 and all34 frozen goldens. Compiler/loader failures are not semantic RED.
```

The separately validated SDK overlay adds the collector and thin trace bridge to the actual SDK, never to
the driver or Worker. Its tested DataSystem source was64c666b00f4dec5e9a706a6b2d6726aad5e34f45.
Stock SDK SHA is `d7d12605135f7da3e6a337d8acc08aed5ae2e653773d0dfc57bae7d7e05773ed`;
disabled/enabled share `b465597bc1285936416ac2f483b6008f0925780fede2c1f12faceb516b060dff`.
Actual static closure found204 source objects,42 shadow-header users, one core/bridge/TLS and only the getter
added to dynamic exports. Its verifier remains NEEDS_REVIEW for one explicit cold `GIT_HASH` metadata
difference; registration/logging can differ. The source-reviewed exception permitted functional testing,
not a claim of byte-identical performance controls. SDK overlay construction is separate from this driver stage.

Run the same frozen binary/arguments with the desired SDK selected by the controlled runtime environment:

```sh
DS_BENCH_RPC_CAPTURE_MODE=enabled DS_BENCH_RPC_CAPTURE_SDK="$MEASUREMENT_SDK" \
DS_BENCH_RPC_CAPTURE_MODULUS=1 LD_LIBRARY_PATH="$CONTROLLED_RUNTIME" \
  "$FRESH_BUILD/paced_sdk" ORIGINAL_PACED_ARGUMENTS --prime-per-caller 1
```

No capture implementation is linked into the executable. `capture/*-prefix.json` describes native-POD64/80B
RPC/End files with byte order, caller TIDs, counts, flags and availability. The final measured prefix already
contains warm rows; never pool the warm export again. Join phase+arrival+caller/ordinal to the original API
ledger and keep unselected anomalies separate from selected quantiles. Server diagnostic flags never remove
primary selected RPC tail samples. `capture/final.json`, `driver-final.json`, raw ledgers and external closure
evidence jointly determine eligibility; a resource seal alone is not a benchmark pass.

## 4. Measurements and acceptance limits

CSend→CRecv is the primary per-method RPC quantity supplied by the trace hook. The driver's existing external
KVClient API begin→end and scheduled-arrival→API-end are distinct metrics. Optional server elapsed durations
are not CPU time; their residual is not pure network or pure context-switch overhead. Missing/invalid optional
diagnostics do not justify changing the primary cohort. No per-RPC clock, allocation, string or logging is added
by this helper; existing SDK/API costs are not claimed absent.

Cold outer CPU snapshots run after priming/Register and before Arm, then after seal/unregister/join/oracle/
SDK ShutDown/Close. Their denominator is warm+measure completed APIs. This proxy includes lead/drain/oracle/
shutdown/capture control/export and excludes seed/Register plus final KVClient destructor; it is not exact
per-API CPU. Original phase snapshots do not move. Tick-limited functional results cannot pass a3% budget.

Both backends still need valid fixed offered load, matched selected IDs, adequate per-method samples,
stock→disabled→enabled observer controls and payload/read-write mixed guards before accepting
RPC P99≤0.70×bRPC. Zero selected rows mean unavailable, not zero latency. Functional delivery, native echo and
single-op API P99 do not satisfy that goal. No production backend default, installed SDK API, public export
contract or committed collector protocol changes here; rollback is leaving capture off and using the stock SDK.
