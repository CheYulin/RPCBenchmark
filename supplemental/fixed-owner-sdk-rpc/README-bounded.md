# Opt-in bounded-admission SDK RPC diagnostic

## Contract and scope

The existing driver and validator keep their strict max-1-ms default. Select
`--admission-profile bounded-v1` explicitly to build this separate protocol;
`bounded_callers_sdk.cpp` and `validate_bounded_arrival.py` do not replace
`paced_callers_sdk.cpp` or `validate_fixed_arrival.py`.

The nominal load remains 500 API/s, 32 fixed callers, 4 KiB values, 64 keys,
128/1,024 warm/measured calls per caller, a 250 ms phase lead, modulus 1 and
seed 17. Smoke uses 4/8 calls. IDs, epochs and owner assignment remain immutable;
there is no admission queue, skipped ID, retry, reassignment or epoch reset.

Dispatch must occur before `scheduled + 64 ms`; the maximum admitted dispatch
lateness is 63,999,999 ns. Every attempted call's observer completion must also
precede that boundary, including the owner's last call. Violations retain the
full ledger and cancel subsequent calls. Dispatches over 1 ms are counted across
the whole phase: at most `floor(planned / 1000)` are allowed, evaluated at the
phase barrier. Formal warm/measure budgets are 4/32; both smoke budgets are zero.
All API errors, mismatches, missing IDs and incomplete capture still invalidate
the result. A rejected or partially completed case provides no accepted latency
quantiles or QPS.

The new profile is included in READY, metrics and validated identities. Validation
retains a full dispatch CDF, aligned 1/10/100-ms admission counts, half-open sliding
burst maxima and observer margins. Backend comparison keeps all original gates,
including primary RPC and all-RPC-sum P99 ratios at most 0.70. It additionally
requires candidate P99.1 / baseline P98.9 at most 0.70 for both metrics, in both
directions and the arm medians. This sensitivity check does not prove a bound on
queue contamination or equal actual arrival spacing. Calibration's original 3%
directional, median and control-drift gates remain required.

## Build the separate drivers

From this directory, supply your own real SDK, public headers and dependency
directories:

```sh
python3 build_local.py --admission-profile bounded-v1 \
  --sdk-dso /path/to/sdk/libdatasystem.so \
  --sdk-include /path/to/sdk/include --output /path/to/new-bounded-build \
  --compiler c++ --library-dir /path/to/dependency/lib
```

Repeat `--library-dir` when needed. Output must be new. The builder reuses the
same pinned capture-helper patch, ABI2, fixed-owner plan and arrival loop. It
links only the chosen driver, plan and patched capture helper against the supplied
SDK; no unit anchor, fake SDK or provider is linked. Formal and smoke images are
`bounded-owner-sdk` and `bounded-owner-sdk-smoke`. C++20, O2, PIC and PIE settings,
source hashes, SDK/header hashes, link arguments and new ELF hashes are recorded
in `build-result.json`. Compilation does not run either image.

ELF magic and successful linking do not establish SDK provenance or qualify the
new binaries. Compiler versions, paths and linking can change ELF bytes. The
private evidence below is not automatically inherited by this portable build.
Omitting the new option retains the original strict sources and image names.

## Validate and reproduce scoped tests

```sh
python3 -B -m unittest -v test_build_bounded_profile \
  test_validate_bounded_arrival test_bounded_case_ready
python3 -B -m unittest -v test_build_local test_validate_fixed_arrival \
  test_validate_fixed_case test_fixed_case_cli
c++ -std=c++20 -O2 -pthread -Wall -Wextra -Werror \
  bounded_admission_test.cpp -o /path/to/new-bounded-unit-test
timeout 8s /path/to/new-bounded-unit-test
python3 validate_bounded_case.py --case /path/to/driver-output \
  --operation get --backend brpc --smoke \
  --expected-sdk-path /path/to/sdk/libdatasystem.so \
  --expected-sdk-sha256 EXPECTED_SHA256 --output /path/to/new-validation.json
```

The C++ command is for a Linux C++20 toolchain and tests eight fake-clock policy
cases without an SDK. Python builder tests stage sources and mock compiler output;
they never compile C++ or run an SDK. The 20 numeric and five READY tests use
synthetic records, including the existing public strict fixtures; they contain
no private archive dependency. Their two fixture adaptations are recorded in
`bounded-source-identities.json`.

Actual drivers use the existing [READY/GO launch contract](README.md#build-and-readygo):
configure the SDK/Worker, backend and capture environment, verify the owned cold
processes and all 32 threads, then release GO. Use symmetric SDK/Worker resources
and record actual scheduler policy, priority, affinities, mapped libraries and
routes. The original runtime evidence used SCHED_OTHER/0 and absolute nice0 for
SDK/Worker; metadata and etcd used nice10. This module does not deploy services,
change host priorities, create namespaces or attest backend selection. ABI2 has
no per-RPC backend tag. Numerical helper passes leave `RPC30_accepted=false`;
ownership, source, no-fallback, routing and fresh observer qualifications remain
external requirements. Formal comparisons require full populations and the
predeclared quartet order; smoke timing is never performance evidence.

## Evidence and rollback

`bounded-evidence.json` records the original source and archive identities.
The matching private build produced formal ELF `6dc9b2befb09` and smoke ELF
`5ae4440d5400`. Four enabled smoke workflows (bRPC/coro, Get/Set) passed against
SDK `56d6a4dceb98` and Worker `6302bc84d50c`: 1,536 API/RPC/End joins, 256 readback
checks with no errors, zero soft-late/debt events and 12 normal role closures.
The full archive SHA256 is
`26aefd7d716a08dd14334237960c1b521b6a9cc1b524110675f32d9f954d08d5`.
This establishes only those private smoke workflows, not portable ELF execution,
observer overhead, matched-load backend benefit or overall system performance.

The original strict nice10 and nice0 failures remain rejected and immutable.
Bounded admission is a distinct workload protocol, not a reclassification or
rescue of those runs. Get/Set RPC P99 at least 30% below bRPC remains unachieved;
no formal observer calibration or backend comparison is established by this
checkpoint. Full DataSystem CI, ON/OFF, Bazel, package and mixed/peak-load
qualification are outside these scoped results. To roll back this experiment,
omit the option and use the existing strict module. No production RPC path,
default bRPC selection, wire format or capture ownership is changed here.
