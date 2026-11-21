# Optional queued SDK profile

## Profile and status

This separate diagnostic keeps eight owners, a fixed global planned rate of 500 calls/s, 4KiB payloads, and 512/4096 warm/measure calls per owner. Smoke uses 16/32 calls per owner. Each owner executes its preplanned IDs once in FIFO order with one call in flight. Late dispatch and previous-call debt are recorded and executed; neither rebases the epoch nor discards a sample. API errors, byte mismatches, exceptions and invalid clocks still invalidate the data. The original strict profile and its rejected results remain unchanged.

The key trace remains global-ID based: Get uses `(id/32 + id%32)%64`, Set uses `id%32`, and every case checks all 64 final keys. Early wake by 1ms followed by spinning remains part of the generator. The approximately 0.5-core pacing cost from `500 × 1ms` is a theoretical estimate, not a measured bound; SDK CPU includes pacing. The same planned trace does **not** imply the same instantaneous RPC sends, concurrency or server load.

**Status: PENDING functional results and complete independent evidence.** This source checkpoint claims no SDK performance result, observer calibration, causal backend improvement or RPC30 acceptance. Public copies and rebuilt ELFs inherit no private runtime qualification. It contains no real trace, ledger, process cookies, host configuration or runtime JSON evidence.

## Build and READY/GO

Run on Linux from this directory with a real SDK and its dependencies:

```sh
python3 build_queued.py --sdk-dso <SDK_DSO> --sdk-include <SDK_HEADERS> \
  --compiler <CXX20_COMPILER> --library-dir <DEPENDENCY_LIBDIR> --output <NEW_BUILD_DIR>
```

Repeat `--library-dir` as needed. The wrapper invokes the existing `owner8.stage_sources` once, which invokes the shared parent staging once. It retains one copy of the seven common sources and the unchanged owner8 pacing header, replaces only the queued driver/policy/unit source, and derives the compilable semantic RED fixture. It builds formal/smoke binaries but never runs either or launches a service. Default parent builds remain strict.

The binary accepts `get|set WORKER_PORT LOG_DIR NEW_CASE_DIR`. Before starting, set `DS_BENCH_RPC_CAPTURE_SDK` to the absolute supplied DSO, `DS_BENCH_WORKER_PID`, `DS_BENCH_RPC_CAPTURE_MODE=enabled`, `DS_BENCH_RPC_CAPTURE_MODULUS=1`, `DS_BENCH_RPC_CAPTURE_SEED=17`, and the backend's existing environment. Independently verify roles, mappings and routes at cold `READY`, then send exactly `GO` on stdin. There is no private campaign launcher, host setup, automatic authorization or retry in this public module.

## Synthetic tests and complete data

```sh
PYTHONPATH=../.. python3 -B -m unittest -v test_owner8_queued_validation test_build_queued test_queued_refs
python3 build_queued.py --stage-only --output <NEW_STAGE_DIR>
c++ -std=c++20 -O2 -pthread -Wall -Wextra -Werror -I<NEW_STAGE_DIR> \
  <NEW_STAGE_DIR>/owner8_queued_admission_test.cpp -o <NEW_STAGE_DIR>/unit-green
c++ -std=c++20 -O2 -pthread -Wall -Wextra -Werror -I<NEW_STAGE_DIR> \
  <NEW_STAGE_DIR>/red-semantic-v1/owner8_queued_admission_test.cpp -o <NEW_STAGE_DIR>/unit-red
```

Run each pure unit binary under an 8-second watchdog: GREEN expects all eight PASS/exit0; semantic RED restores the period rejection and observer-debt abort, expecting five PASS/three FAIL/exit1. The C++ wake-delay fixture is the wholly synthetic 17,000,000ns value. Python populations and ABI2 containers are generated synthetically; their fake SDK identity is not an executable SDK. Unit sources, fake fixtures and RED code are never linked into production driver binaries.

Use `validate_owner8_queued_case.validate_case` with explicit expected SDK path/hash, with `PYTHONPATH=../..` for the unchanged shared POD decoder. Preserve every planned ID and full successful cohort. The added `head_enter_ns` and `kv_call_begin_ns` distinguish FIFO backlog, pacing, pre-call work, SDK call, validation and observer tail. Full CDFs include zeros, ties, late calls and drain. Incomplete cohorts cannot provide successful-subset performance ranks. Existing 0.70 RPC-P99 and rank-sensitivity numerical gates remain descriptive; they do not establish observer overhead or identical actual send patterns.

`qualification_refs.py` stores complete case qualifications behind immutable path/SHA references. Loading requires an explicit `expected_sdk_sha256` from the caller's frozen per-arm SDK selection, after checking the row identity against `campaign.planned_rows()`; the helper has no default SDK identity. `campaign.py` provides case ordering and result callbacks. The nine `test_queued_refs` cases use invented SDK hashes and metadata, including different baseline/candidate identities, and establish no runtime or SDK performance result.

## Limits and rollback

A complete queued dataset can guide a later optimization; it cannot qualify an old strict failure or prove a production benefit. Capture overhead has not been qualified for this protocol. A campaign must predeclare all windows and report both Get and Set, including negative results, without replacement: smoke correctness failure prevents formal work; formal data failures remain explicit; source, identity or cleanup failures stop execution. Numerical Get rejection does not justify skipping Set.

Selecting the parent strict entry restores the original profile. This directory changes neither DataSystem code nor wire format, bRPC defaults, the shared capture ABI, resource admission, or the old validators. See [source-identities.json](source-identities.json) for source pins and [design-evidence.json](design-evidence.json) for the evidence boundary.
