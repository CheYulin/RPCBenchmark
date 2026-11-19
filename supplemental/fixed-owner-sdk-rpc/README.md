# Fixed-owner SDK RPC diagnostic

## Profile and scheduling

This optional module exercises real DataSystem Get or Set APIs with 32 fixed caller
threads, 4 KiB values, 64 keys and a global offered rate of 500 API calls/s. The
formal binary plans 128 warm-up and 1,024 measured calls per caller; the smoke
binary plans 4 and 8. Each phase starts after a 250 ms lead. Arrival `id` belongs
to caller `id % 32`, with schedule `epoch + floor(id * 1e9 / 500)`.

There is no producer thread or admission queue. Each caller waits for its own
scheduled arrival. Dispatch more than 1 ms late invalidates the case: IDs are
never skipped, debt is never hidden by resetting the epoch, and every planned ID
remains in the ledger even after a controlled abort. The full-capture profile uses
ABI2, modulus 1, seed 17 and at most 16 sequential RPC spans per API.

The shared `ds-latency30` helper remains unchanged. `build_local.py` verifies its
three original source hashes, copies them into a new build directory, applies
`capture_helper.patch` with system `patch` and requires exact resulting helper
hashes. The C ABI is unchanged. `closed_loop_plan.h` is an exact snapshot of the
finite diagnostic's histogram implementation; existing builders and targets are
unaffected. `source-identities.json` records the preserved source bytes.

## Build and READY/GO

Linux, Python 3, a C++20 compiler, system `patch`, the real SDK DSO and its public
headers are required. This builder creates formal and smoke binaries with
`-fPIC -pie`; it never runs them or starts services. Unit providers and anchors
are excluded from both link commands.

```sh
python3 build_local.py --sdk-dso /path/to/sdk/libdatasystem.so \
  --sdk-include /path/to/sdk/include --output /path/to/new-build \
  --compiler c++ --library-dir /path/to/dependency/lib
```

Repeat `--library-dir` for dependencies. The default helper base is the sibling
`ds-latency30` directory; `--helper-base` accepts another directory only when its
original hashes match. The output directory must be new. `build-result.json`
records inputs, patch application, exact command arguments and binary hashes.

The driver takes `get|set WORKER_PORT LOG_DIR OUTPUT_DIR`; its output directory
must be new and its log directory must already exist. Before launch, configure
`DS_BENCH_WORKER_PID`, `DS_BENCH_WORKER_HOST`, `DS_BENCH_RPC_CAPTURE_SDK` (the real
SDK path), `DS_BENCH_RPC_CAPTURE_MODE=off|enabled`,
`DS_BENCH_RPC_CAPTURE_MODULUS=1` and `DS_BENCH_RPC_CAPTURE_SEED=17`.
`DATASYSTEM_RPC_BACKEND=brpc|coro_rpc` selects the SDK backend; coro also requires
its configured `DATASYSTEM_CORO_RPC_PORT` and `DATASYSTEM_CORO_CALLER_IO=1`.
Use a worker configured to provide the selected backend. The SDK DSO and its
dependencies must resolve to the intended files at runtime.

After `ready.json` and `READY FIXED_OWNER_ARRIVAL` appear, an external controller
must verify PID/TID ownership, all 32 caller thread placements, actual SDK/Worker
maps and network routes before writing exactly `GO\n` to standard input. Preserve
the full output directory and `RESULT` line. This module does not supply service
deployment, CPU isolation, network namespace setup or backend route attestation.

## Validation and comparison cohorts

```sh
python3 -m unittest -v test_build_local test_validate_fixed_arrival \
  test_validate_fixed_case test_fixed_case_cli
python3 build_unit.py --sdk-include /path/to/sdk/include \
  --output /path/to/new-unit-build --compiler c++
python3 validate_fixed_case.py --case /path/to/driver-output --operation get \
  --backend brpc --smoke --expected-sdk-path /path/to/sdk/libdatasystem.so \
  --expected-sdk-sha256 EXPECTED_SHA256 --output /path/to/new-validation.json
```

The Python tests use synthetic records. The C++ unit builder uses a fake capture
provider and public SDK headers; it never calls real KV services and its binaries
must never replace the production driver or SDK. Its five groups cover arrival,
plan, phase-end rounding, typed Session and mode snapshot behavior.

Validation requires every ID exactly once, successful API returns, byte equality,
valid timing, complete capture prefixes, ABI capacity bounds, matching SDK
identity, successful shutdown and close, and no late or unattempted arrivals.
API and scheduled end-to-end quantiles use nearest rank on original nanoseconds.
QPS counts the full completion cohort over
`max(planned_end, last_api_end) - epoch`. CPU uses actual process ticks for both
SDK and Worker over recorded read brackets, including the phase lead. RPC
analysis requires the primary Get/Publish method and separately sums **all**
sequential RPC spans for each API, including Create spans if emitted by Set.

`decide_calibration` requires an off/on/on/off quartet per backend and operation;
the frozen 3% bounds include both directions, medians and control drift.
`decide_backend_comparison` requires a brpc/coro/coro/brpc quartet: primary RPC
P99 and per-API RPC-sum P99 must each be at most 0.70 in both directions and the
median; API/end-to-end tails, P50, CPU, QPS and baseline drift also have gates.
External ownership, role, map, route and input protections remain required.
Even a numerical comparison pass leaves `RPC30_accepted=false` until those
external qualifications are satisfied; the low-load diagnostic cannot promote
overall DataSystem performance by itself.

## Evidence and limits

The initial eight real-SDK smoke cases cover both backends, Get/Set and capture
off/enabled. All eight passed with 16 actual SDK/Worker map proofs, 24 normal role
closures and unchanged inputs. They used DataSystem checkpoint
`b81a93a18336589dd1c7761fa5cd9f2fb13f0641`, SDK SHA256
`56d6a4dceb98a62128900147e0d57ade5fa3105551a603117340ce6b1cc31791`
and Worker SHA256
`6302bc84d50c3ee0020207f3f9c175397c3faa477f86d42aaf67322eddf8b162`.
SDK and Worker each used eight distinct physical NUMA1 cores, with metadata and
etcd assigned separate cores. This functional pass made no DataSystem code change.

These smoke cases do not qualify observer overhead, compare backend latency or
establish a 30% RPC improvement. No smoke quantile is used as performance evidence.
The fixed 500 API/s profile is a low-load diagnostic, not saturation throughput.
There is no eBPF observer in this module. ABI2 records have no backend tag, so
live port presence alone cannot establish per-method backend selection. SDK
provenance, no-fallback behavior and source-based backend qualification remain
external requirements. Building and passing these scoped tests is not full
DataSystem CI, Bazel, feature-OFF or packaging qualification.
