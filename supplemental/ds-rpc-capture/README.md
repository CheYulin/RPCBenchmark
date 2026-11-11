# Private RPC capture core

## 1. Scope, ABI and current status

This standalone C++17 module implements the pure collector contract from SDDv2 SHA
`f2f5b60ea27953df335b72773b6845cada9b09a5ff1ac4367f56585c7c927b9e`, with the unpublished
64-byte sample layout refined in SDDv3 SHA
`ff0efff95b11a7dad69131fb2ddafdca1a755b4cab9f749fb0c9fd1778485e1a`.
Server span diagnostics preserve the primary RPC quantity.
**GNU11 pure-core build and semantic tests passed; no actual SDK hook, performance or RPC30 evidence.**
It changes no DataSystem source, public SDK header, root build target, native adapter or default backend.
Actual trace conversion/method-full-name mapping, SDK-only CMake/Bazel closure, private production export map,
matched-load driver integration and stock/disabled/enabled overhead controls are later stages.

`capture_abi.h` is pure C ABI2 with exact version/bytes checks. Sample rows are 64-byte PODs; they contain
arrival/phase identity, RPC ordinal/method, supplied CSend/CRecv, optional server durations and error scalars. They contain no payload,
string/view or endpoint. Timestamps and cached TID are supplied; the collector makes no clock/TID syscall.
Only the private getter is a collector export. The fixtures export emit/hook/TLS-bridge **test-only** entrypoints;
these are not an installed observability API. The driver links no collector object. Cross-DSO tests dlopen real
shared libraries; the negative keeps the same ABI provider, Run, registry and caller buffers, but its SDK adapter
reads a different DSO's independent TLS. A test-only bridge lets that same core reject the wrong owner. The split
SDK contains no collector object; source/link/nm/ldd and runtime getter identity must prove this isolation.
That new-contract failure is not a claim that an old SDK had this API or bug.

## 2. Ownership, bounds and publication

One out-of-line TLS pointer in `capture.cpp` identifies a registered caller. Open allocates bounded slot metadata;
callers lend fixed disjoint record/End-summary arrays, pre-touched during registration and retained through Close.
Control/output POD storage must be valid, independently owned and must not overlap those arrays. Caller buffers
must not be mutated while leased. Register rejects duplicate IDs/TIDs and overlapping/misaligned buffers. Fixture
TID values are deterministic identities; checking real Linux TIDs belongs to the later real-driver integration.

The sole run starts UNARMED. Setup Observe calls are ignored; all configured slots must register before Arm.
The real driver's existing priming gate must prove setup success before Arm; pure collector registration alone
does not prove application priming or publish an arrival epoch. Begin/End require same-thread TLS ownership.
Warmup/measurement IDs share append-only prefixes, never reset. Selection hashes seed/phase/arrival, independent
of caller assignment/completion. Every RPC updates owner-local counters/ordinal; selected RPCs and unselected
failures are retained. Every selected/error API retains an End-summary. A required-primary mask is one-of allowed
methods; missing primary, malformed endpoints, unknown method, bad ABI or bad ordinal invalidates capture.
Global ledger uniqueness/completeness and method-name correctness remain real-driver obligations.

Each caller has separate normal/error capacity budgets within contiguous immutable prefixes. A skewed caller
cannot borrow another caller's storage or silently drop a selected ID; overflow is sticky invalid with the
committed prefix retained. Normal writes use no heap allocation, global lock/RMW, string work or clock/TID call.
Scheduled time is joined by phase/arrival to the original ledger or End-summary. Sample phase IDs are checked
against uint32 range; caller index fits uint16 under the128-caller bound. Missing server durations have no valid
flag; valid0 is distinct, invalid spans are flagged/zeroed, and execution exceeding processing becomes invalid.
Processing exceeding the client RPC interval marks residual unavailable without unsigned wrap or clamping.
These optional diagnostics never replace CSend/CRecv. Server elapsed is not CPU, and residual is not pure network;
the real four-getter/server-clock boundary mapping remains later SDK work, with no new clock call in this core.
Unselected diagnostic anomalies use the explicit error reserve and End-summary. Those extra rows are not members
of the selected-ID latency cohort and must not be mixed into its quantiles.

Data is fully copied before a release count store; partial snapshot acquires only immutable prefixes and atomic
flags. It marks plain counters unavailable. A relaxed-signal fixture reads a published row while its writer is
still active, relying on the prefix acquire rather than a CV handoff for the record publication.

Begin publishes slot.active with seq_cst, then checks the global state with seq_cst before writing plain data.
Cold normal Snapshot/Disarm sets freeze with seq_cst and checks active with seq_cst. If Begin's second check sees
the old ARMED state, its earlier active publication precedes freeze and the reader must see active or that API's
release inactive after all plain writes. If publication follows the reader check, Begin sees freeze and writes no
plain data, or sees restored ARMED after the snapshot read is finished. End completes all writes before release
inactive. This is the cross-atomic total-order argument; acquire/release double-check alone is insufficient.
Deterministic pauses cover both sides of this handshake. Tests do not replace the memory-order proof.

## 3. Seal, errors and teardown

Normal snapshot requires the real driver to exclude new dispatch and drain all work; core additionally freezes
Begin and rejects active writers. Tokens include all-API/all-RPC/method counters, both prefixes, errors and their
generation, so even an unselected API with no new rows invalidates an older token. Disarm rejects partial/stale
tokens and active writers. Unknown-thread/scope errors increment an exceptional publisher count with seq_cst
before their first eligibility state load. Only enabled ARMED admits them; an admitted publisher pins the run
and publishes sticky errors under the cold mutex without a later state check, then decrements the count.
Disarm's seq_cst DISARMING store closes exceptional admission. It then checks the publisher count and active
slots; pending work returns BUSY and restores ARMED, never waits while holding the mutex. This narrows the
unpublished v2 protocol: a hook first eligible after that closing point is not admitted and never touches the run.
The real driver must already exclude new dispatch. Normal active-owner Observe uses no global RMW or mutex.

Let I be the exceptional count increment, L its ARMED eligibility load, F the closing state store, and Q the count
check, all seq_cst. An admitted I<L<F<Q cannot be missed unless it already decremented after publishing; then
the token/error generation catches it. If Q<I, L sees the closed state and cannot publish. Tests pause an admitted
publisher before mutex acquisition: Disarm must return BUSY, then the old token must return CHANGED after
publication. This is a synchronous registered-caller scope, not arbitrary async/bthread context propagation.

Changed tokens do not seal. After an invalid run drains, a new complete snapshot can safely seal its retained
**invalid** result for cleanup; sticky errors never reset and valid_capture remains false. A lifecycle OK status
means the resources were sealed, not that a benchmark succeeded. Disabled control also reports valid_capture=false
and no RPC coverage, while exercising the same buffer/TLS/lifecycle setup. No user API result is changed by Observe.

After disarm: stop/unregister/join callers, run final oracle/SDK shutdown while DISARMED, then Close. The core
checks no exceptional publishers, inactive/sealed owners and unchanged final prefixes/errors; joining and buffer
lifetime are explicit driver obligations. Active/unsealed owners return BUSY and cannot be freed. Partial capture retains
owners until the existing external supervisor/_Exit contract terminates it. This pure module does not detach work,
run an event loop or implement that SDK shutdown path. No caller should dlclose its DSO while a registered owner lives.

## 4. Standalone TDD recipe and acceptance limits

After an exclusive BUILD authorization, use existing GNU11 and fresh output; no downloads or installs:

```sh
CC=/usr/bin/gcc CXX=/usr/bin/g++ python3 supplemental/ds-rpc-capture/run_tests.py --out "$EVIDENCE/capture-core"
```

The runner compiles the C11 header, real fixture DSOs and the same driver. A TEST-only old-admission fixture
pauses after its unprotected ARMED read and must produce a false-valid seal, failing the same admission
contract (semantic RED exit1); the revised core must pass. It also expects split-TLS missing-primary semantic
RED (exit1), a deliberate allocation fault RED (exit1), rejects an actually loaded wrong-ABI getter (exit3), then
requires single-TLS contract GREEN and twelve contract bundles. Compiler,
linker, missing-library, missing-symbol or timeout failures are never semantic RED. Each command/log/source/binary
hash is retained in a receipt, including partial build failures. Test hooks and fault allocation are private fixture
macros; neither is an actual SDK integration or a future measurement-library performance configuration.

Coverage includes two callers/skewed assignment, every RPC ordinal, sampled/unselected failures, zero/full/error
reserves and overflow, phase prefix retention, partial/normal lifetime, release/acquire publication, wrong owner,
duplicate Begin/End/lifecycle, ABI/size/range failures, normal Snapshot freeze races, and snapshot-to-error sealing.
On 2026-11-11, GNU11 C11/C++17 with O2, pthread, Wall/Wextra/Werror completed all 22 runner steps:

| Contract | Observed result |
|---|---|
| Old exceptional admission | False-valid seal reproduced; same contract rejected it, exit1. |
| Revised exceptional admission | Paused publisher returns BUSY; old token CHANGED; retained error seals invalid, exit0. |
| SDK-only independent TLS | Same provider/Run, zero primary rows; contract exit1. |
| Allocation fault / correct provider | Two trapped allocations rejected, exit1; correct provider zero allocations, exit0. |
| Wrong getter ABI | Loaded library rejected, exit3. |
| Original suite / binary closure | Twelve bundles, C header, exact fixture exports and one-provider nm/ldd checks passed. |

Earlier attempts are retained separately: a guard stdin self-hash error, a wrapper that misclassified its own
cross-session compiler, a missing driver TESTING define and a GNU11 inlined replacement-delete diagnostic.
None is semantic RED. The driver now explicitly enables TESTING and keeps replacement allocation interceptors
out of line; O2/Werror and all assertions remain enabled. The successful slot's source hashes matched before/after,
and its 13 startup/active/release guard snapshots found no foreign workload or uncertainty (maximum gap 574ms).
All owned processes were gone at release. No Memcheck, TSAN or actual SDK test was run in this pure-core stage.

Independent CMake/Bazel/real SDK linker/export/role checks remain unrun. A raw count or fake timestamp here proves
no real RPC latency. Both backends must later pass stock→disabled→enabled 3% observer controls before any genuine
CSend→CRecv 30% acceptance; API/native P99 and these fixtures cannot substitute for that evidence.
