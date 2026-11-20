# Eight-owner SDK arrival diagnostic

## Profile and evidence

This independent profile keeps global planned500QPS and the former4096/32768 warm/measure totals, using8 callers with512/4096 calls each. Smoke uses16/32 calls per caller. Calls keep the old global-ID Set32-key/Get64-key trace and64 final key checks. Each caller retains the full SDK8-CPU mask; the Worker uses a separate8-CPU mask. No per-caller pin or production source change is introduced.

The pacer sleeps until1ms before the fixed target, then spins until the monotonic clock reaches it. Dispatch and observer completion must remain strictly within the16ms owner period, including the last call. The unchanged >1ms soft budgets are4/32 formal and0/0 smoke. About0.5CPUcore from500×1ms is a theoretical estimate, not a measured value or bound; SDK CPU includes pacing. Planned rate does not prove identical physical RPC send timing across backends.

The source-identical private GNU15 build passed the declared pure RED (3 PASS/5 FAIL) and GREEN (8 PASS). The new four enabled bRPC/coro Get/Set functional cases passed, and two readers verified the complete raw archive (`6ea5e880…`). They joined all1536 API/RPC/End rows, checked256 final key readbacks (128 in the Set cases), and found zero soft-late/debt violations. Formal observer/backend comparison has not run. Old32 failures and qualification remain separate. No RPC30 or performance acceptance is claimed. See [design-evidence.json](design-evidence.json) and [source-identities.json](source-identities.json).

## Build and READY/GO

Run from this directory on Linux with a real SDK DSO, its public headers and transitive link/runtime libraries:

```sh
python3 build_owner8.py --sdk-dso <SDK_DSO> --sdk-include <SDK_HEADERS> \
  --compiler <CXX20_COMPILER> --library-dir <SDK_DEPENDENCY_LIBDIR> --output <NEW_BUILD_DIR>
```

Repeat `--library-dir` as needed. The wrapper calls the existing parent `build_local.stage_sources` once, applies its pinned helper patch to the original `ds-latency30` sources, and stages a single common-header closure. It compiles formal/smoke only, without running either binary. It cannot prove a supplied SDK's provenance; public recompiled ELFs need fresh functional and observer qualification.

The binary takes `get|set WORKER_PORT LOG_DIR NEW_CASE_DIR`. Set `DS_BENCH_RPC_CAPTURE_SDK` to the absolute DSO supplied to `--sdk-dso`. Also set `DS_BENCH_WORKER_PID`, `DS_BENCH_RPC_CAPTURE_MODE=off|enabled`, `DS_BENCH_RPC_CAPTURE_MODULUS=1`, `DS_BENCH_RPC_CAPTURE_SEED=17` and the backend's existing environment before starting it. Bind and verify the owned roles and actual SDK/Worker mappings at cold `READY`, then send exactly `GO` on stdin. There is no internal service launcher or calibration authorization in this wrapper.

## Tests and raw validation

```sh
PYTHONPATH=.. python3 -B -m unittest -v test_owner8_validation test_build_owner8
python3 build_owner8.py --stage-only --output <NEW_STAGE_DIR>
c++ -std=c++20 -O2 -pthread -Wall -Wextra -Werror -I<NEW_STAGE_DIR> \
  <NEW_STAGE_DIR>/owner8_profile_test.cpp -o <NEW_STAGE_DIR>/unit-green
c++ -std=c++20 -O2 -pthread -Wall -Wextra -Werror -I<NEW_STAGE_DIR> \
  <NEW_STAGE_DIR>/red-semantic-v1/owner8_profile_test.cpp -o <NEW_STAGE_DIR>/unit-red
```

Run each pure unit binary with an8s watchdog: GREEN expects exit0/all8 PASS; semantic RED expects exit1 with3 PASS/5 FAIL. RED changes only direct sleep and the64ms admission period, preserving compilable types and fixture source. It is never linked into an SDK binary. Python fixtures are synthetic ABI2 data and perform no SDK/network workload.

Use `validate_owner8_case.validate_case` with the same explicit SDK path/hash arguments as the parent adapter. Set `PYTHONPATH=..` so the unchanged parent `fixed_case_pod` decoder is used. Keep the complete arrival IDs, raw prefix containers, dispatch CDF/bursts, scheduled E2E, API/RPC timestamps and actual completion rates. Observer/backend decision functions retain all existing gates, including rank sensitivity; no quantile subtraction or sample filtering is used.

## Limits and rollback

Eight owners and early wake may still fail quality gates. The first quality/source/closure failure stops the campaign; a rejected observer quartet stops later quartets, with no rescue window. Backend comparison needs fresh accepted observer qualification. The new protocol never reinterprets prior32-owner results. The parent default build and all old32 files remain unchanged; selecting them is the source rollback.
