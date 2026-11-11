# Explicit per-caller priming

## 1. Evidence, scope and fixed control

This is a benchmark harness change, not a DataSystem SDK/transport optimization. Keep the fc8 stage and original
f41 binary unchanged. The previous 45115/s BRPC warmup remains invalid: 80 queue rejects at 6.7605..18.6413ms,
first-per-caller API P50 6359us versus later API about 95us. This association does not prove an initialization cause.
The new controlled policy explicitly primes each persistent caller before warmup; it does not discard bad arrivals.
`--prime-per-caller 0|1` defaults to 0. Both backends use the same policy. This policy does not change the
45115/s schedule, queue 256, spin 100us, 32 callers, 7 SDK + 1 producer / 8 Worker cores, deadlines or generator
thresholds. The validation below used separately declared 1000/s and finite semantic plans, not a 45115/s rerun.

## 2. State, ownership and measurement

Each caller registers its cached Linux TID, then (only when enabled) executes exactly one API with the configured
Get/Set operation and full payload size on that same persistent thread. Get validates every returned byte.
Set has no returned payload, so its oracle is a separate same-thread Get/readback; record that extra operation
explicitly. It is setup work, never an arrival or throughput sample. Each caller has its own key during priming.

An externally synchronized readiness gate distinguishes registered, priming-complete-success/failure and released.
Main may claim readiness once only after every caller completed successfully. An unfinished or failed caller
prevents any warmup epoch/producer publication. Caller buffers/TIDs/client remain alive through both cohorts.
Per-caller proof records operation start/end, readback start/end, status codes, payload bytes and byte-validation.
Failure stops before warmup, joins the persistent callers and shuts down the shared SDK; the run is invalid.
Failed setup also skips the final Set readback oracle, so shutdown sends no extra verification APIs after failure.
When disabled, all callers mark setup skipped/ready without additional API calls, preserving the old control policy.
Actual priming never runs under the readiness mutex. Gate updates and proof publication use the existing mutex;
main reads proof only after all callers complete. No new request-path lock, allocation or product flag is introduced.

## 3. TDD and failure accounting

Before GREEN, preserve the old registration-only ready policy as an explicit negative model of the new contract.
The same test must compile and fail when one caller is registered but its priming has not completed, and when a
completed caller reports failure. This is new-contract RED, not a claim that the default 0 fc8 control was defective.
Tests exercise the actual gate used by the driver: no sleep, networking or mirrored Boolean implementation.
Cover one pending caller, failed completion, all complete with one readiness claim, duplicate registration/completion,
and immediate completed setup. The disabled API-call count is a driver functional check, not a gate-unit claim.
Compiler failure is not semantic RED. In the authorized GNU15.2.1 slot, the negative compiled and produced the
expected 2 FAIL / 3 PASS (exit1); the actual gate passed 5/5 (exit0). Both healthy and failure-only SDK drivers
compiled with the original C++17/O2/strict-warning recipe and unchanged B3 SDK/public headers. No local toolchain
installation, product change or extra driver CLI was needed for the private failure-only build.

The driver writes priming proof even on API/byte failure. Failed priming yields no warmup/measurement plan or
producer, and final validity is false. All previous warmup/admission/deadline/drain accounting remains unchanged.
No priming API is retried or silently replaced. Public SDK fixed timeout and an external supervisor bound setup.
The failure-only derivative changed caller 0's priming Set key to empty and counted the final oracle's actual Get
call site. It exercised real public SDK K_INVALID=2; it is parameter-failure coverage, not disconnect recovery.

## 4. Executable validation, freeze and rollback

Reproduction uses the negative gate contract (expected exit1), then the real gate (expected exit0), and the
GNU15/C++17/O2 SDK recipe in a **new** driver output directory with all source hashes including the gate. Do not
overwrite f41. Example commands:

```sh
CXX=g++ python3 supplemental/ds-latency30/run_priming_tests.py --out "$EVIDENCE/priming-contract"
CXX=g++ python3 supplemental/ds-latency30/build_sdk_driver.py \
  --sdk-include "$SDK_INCLUDE" --sdk-lib "$B3_SDK_LIBRARY" --out "$EVIDENCE/primed-sdk-build"
# Functional invocation adds the explicit policy to the previously frozen options:
"$EVIDENCE/primed-sdk-build/paced_sdk" ... --prime-per-caller 1
```

Serial1 used 1000/s, default0/prime1 x Get/Set x both backends, 1s warmup+2s measure. Four bRPC healthy windows
passed 12000 timely arrivals; its empty-key Set failure blocked both phases and made 0 final oracle calls. Coro
default0 Get warmup was **invalid** for max generation lateness 5.054053ms>1ms despite 1000 timely/full-byte APIs;
its measure and remaining Coro cases stayed NOT_RUN. Stop-first-invalid retained this result without tuning.
Archive SHA256 `77014563fcdeeb715870eaddb7e9de00c102bb4330c2955cbe5d63e65b52992b`.

An independent focused plan reused the same two binaries, no recompile: only Coro prime1 Get/Set and expected
Set failure. Each healthy case completed 32 persistent-thread/full-byte primers (Set plus 32 Get readbacks), then
one scheduled API per 1ms phase at rate 1000. All four scheduled APIs passed: **two warmup + two measurement
samples** total. The failure case blocked both phases and final oracle calls stayed 0. Those finite contracts
passed with original queue/spin/deadline/generation limits, exact TIDs/epochs/accounting and clean SDK shutdown.
They do not fill serial1's NOT_RUN cases, establish sustained capacity or support any P99/QPS/RPC30 comparison.
Archive SHA256 `93bff6dcf97d72909acdc377706744d71ca102a02eae8971f173f3af08ccb30e`.

Focused evidence retains successful guard timestamps, scan boundaries, active-monitor begin/end and paired
realtime/monotonic anchors. Main guard 81 snapshots/0 hits; actual active maximum gaps were 261.283ms (Get),
109.972ms (Set), 102.927ms (failure), so sampling does not prove continuous 100ms or global CPU exclusivity. Actual
B3 DSOs/thread masks, post-source/binary hashes and empty owned-process/netns cleanup passed. Six code inputs
remain byte-identical to serial1; only README and this design record were updated after validation. CMake/Bazel
integration is not established by these standalone builds. Roll back with preserved f41/default0; keep every
old invalid receipt. Further high-rate/performance runs require a separately frozen plan and exclusive host slot.
