# Owned eight-caller scheduler and Get diagnostic

## Scope and status

This optional module records complete bounded caller observations, then joins them to the original eight-owner SDK ledger. It does not change the default benchmark build, DataSystem, its RPC backend, pacing profile or acceptance gates. The source is pinned to the existing eight-owner driver and SDK identities; it is not a generic host tracing launcher.

The v6 synthetic kernel mechanism passed complete raw verification by two independent readers: 24,997 events, 512 exact fixture pairs (64 per owner), zero pairing/commit/state-CPU issues and 21 preserved migrations. All eight programs, 13 maps and three owned process lifetimes closed normally. No v6 actual SDK diagnostic has run at this checkpoint. Synthetic qualification would validate the tracing mechanism, not Get/Set performance or the 30% RPC target. Public source copies and locally repeated Python tests do not inherit private runtime qualification. The public loader removes two final empty lines for Git whitespace validation; its Python AST and all other loader bytes are unchanged. See [contract.json](contract.json) for exact identities, ABI, bounds and preserved negative evidence.

The failed v5 mechanism attached an entry/return pair for each of eight TIDs. Actual raw data showed eight BPF entry/return callbacks inside each of five complete physical function calls. Upstream Linux v5.14 source provides a shared-address-space prefilter mechanism consistent with that observed duplication; the exact EL9 kernel producer is not byte-attested. A positive per-TID perf attachment was not a proven current-TID-only BPF execution filter. v6 instead uses exactly one entry and one return consumer attached with the positive owned SDK leader PID, while the C program retains all eight TID and task/leader birth-cookie checks. It never uses a wildcard symbol or systemwide PID. The old failures remain rejected.

## Pinned collection interface

The loader targets x86-64 little-endian Linux `5.14.0-514.el9.x86_64`, BCC `0.35.0`, pinned Python/libbcc hashes, and validated runtime BTF/trace prototypes. Eight callers keep the whole odd-CPU mask `1,3,5,7,9,11,13,15`, SCHED_OTHER/nice0; the native observer uses CPU35/nice10. Bounds are 20MiB maps, 256MiB observer RSS, 65,536 records per owner and an 80s data horizon measured from SDK pre-warm GO. There is no new thread, affinity or system-setting modification in the loader.

An external owned-process controller must hold cold READY, provide the exact receipt/allowlist and keep the SDK unreaped until probe closure and observer exit. Inspect the plan without loading BCC/BPF:

```sh
python3 -B owned_sched8.py --receipt RECEIPT.json --allowlist ALLOWLIST.json   --output NEW_RESULT.json --events-output NEW_EVENTS.bin --duration 80
```

Only a separately authorized, qualified owned-process run may add `--execute`. After `READY_BOOTSTRAP_FDS`, the controller sends SDK `GO` and then `GO <same_monotonic_ns>` to the observer. `FULL_SCHED_ROI_ENABLED` must report eight cookies and two Get links. Bootstrap/attachment gaps count against the horizon and remain censored. `STOP` closes collection; the controller waits for `PROBER_STOPPED`, writer drain and complete process closure. This module does not supply SDK/service launch, authorization or automatic qualification.

## Offline replay and tests

```sh
python3 -B -m unittest -v test_gate_and_links test_analyze_full_sched
python3 -B analyze_full_sched.py --metadata SNAPSHOT.json --events EVENTS.bin   --ledger all-arrivals.jsonl --output NEW_REPORT.json
```

The copied tests comprise 15 pure gate/loader-helper checks and 12 synthetic replay checks. They run without BCC, kernel activity or SDK/network workloads. The packed record is 32 bytes (`<QQIIHHI`), stored as eight owner prefixes with explicit commits and per-owner Get pair IDs. Replay preserves every original ledger row, cancelled/unattempted IDs, partial pairs, raw switch state and boundary uncertainty. It uses half-open state unions and exact containment/pair IDs, rather than nearest timestamps, retained maxima or subtraction of quantiles.

## Ownership and interpretation limits

Writer authorization follows nonreturning increment then atomic OPEN/phase read; accepted writers remain counted through their last protected write. The 61-bit count bounds simultaneous callbacks, not cumulative events. Finite active depth is a stated precondition, not an unconditional proof for forged overflowing words. Record/Get reservations use at most 16 CAS attempts under a lease; exhaustion marks the trace incomplete and counters become lower bounds. Detach/FD close alone is not a writer fence.

Initial/final censoring, missing commits, overflow, unknown clock alignment and contradictory scheduling/Get observations remain explicit. SDK clock alignment requires its actual mapped C++ runtime plus the live initial-time-namespace check; a C fixture cannot establish it. Scheduled residence includes kernel/IRQ/other effects. These observations cannot alone establish server bridge cost, network delay, a global VM pause or an RPC performance benefit. Keep replay outside the foreground collection window. Rollback is to omit this optional directory; no existing module or production behavior changes.
