# Selected API scheduler diagnostics

This optional Linux/BCC tool records all completed off-CPU intervals for a deterministic 1/64 sample of the existing DataSystem SDK capture driver's API calls. It helps distinguish waiting while blocked from waiting for CPU after a wake. It does not change either RPC backend and is outside the default benchmark build.

## Validation

Run the pure tests without BCC or compiler dependencies:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s supplemental/sampled-api-scheduler -p 'test_*.py'
```

The manual native fixture requires Linux x86_64, GCC with C++20, BCC, matching kernel headers, permission to load BPF programs, and available CPUs 3 and 37. It uses only its own child process. Configure `BCC_KERNEL_SOURCE` according to the host's BCC setup, then run:

```sh
g++ -std=c++20 -O2 -g supplemental/sampled-api-scheduler/fixture.cpp -o supplemental/sampled-api-scheduler/fixture-native
timeout -s INT -k 5s 45s taskset -c 37 python3 supplemental/sampled-api-scheduler/fixture.py
```

The fixture compares selection against the existing `ds-latency30/capture_helper.h` for all 200,000 planned IDs. Its 8,192 marker calls include selected and unselected APIs plus idle sleeps. It verifies selected-ID accounting, intervals shorter than 500µs, an injected stale wake that remains unknown, target exit before event-ID detachment, and descriptor restoration. Generated binaries and result JSON are local runtime artifacts.

## Use with the SDK driver

`ExactProfile(output, headers, driver, expected_driver_sha256)` prepares the BPF program disabled. The caller must supply a positively owned driver process and exactly 32 persistent caller TIDs, verified against its process/session identity. `SampledObserver` implements the existing driver phase hooks. Prepare before starting the driver; bind before the original warmup epoch; check periodically; close in a `finally` block; preserve any failure. The expected digest is mandatory and must come from the experiment's frozen binary manifest.

The target must be the existing x86_64 System V `benchmark_capture::Session::Begin(caller, phase, arrival, scheduled, begin, methods)` marker. At function entry, phase and arrival are arguments three and four including `this`. Phase is 1 or 2. Selection exactly matches the existing driver with seed 17 and modulus 64; use those SDK capture settings too. The native fixture proves register argument selection, but does not prove an arbitrary driver has the same ABI. Confirm the actual target symbol, binary digest and disassembly before attaching.

The validation workload fixes the caller affinity to CPUs 3,5,7,9,11,13,15. The BPF program tracks every Begin/End for pairing, stores intervals only inside selected APIs, and uses task start time plus phase and arrival in event keys. Its map is capped at 32,768 intervals, capture at 75 seconds, and caller IDs at 128 entries. Overflow, read/clock/identity errors and mismatched API boundaries fail the capture. They are not reasons to enlarge limits or discard rows. Cleanup uses only the recorder's own uprobe event IDs and raw tracepoints, including after target exit. Keep the SDK/Worker lifecycle and immutable-input checks in the surrounding harness.

## Interpretation and recorded result

Join rows by PID, TID, task generation, phase and arrival to that same selected API's RPC endpoints. `wait_join.py` clips disjoint intervals to each RPC's half-open span, conserves known blocked, known runnable and unknown split time, and rejects overlapping intervals. A missing wake is unknown rather than zero. First verify complete capture coverage, stable counters, no outstanding intervals, actual SDK ledger conservation and original generator quality.

The sanitized `diagnostic-20261113.json` records two independent Get/Publish windows: 200,000 successful API calls each, with 3,056 selected APIs each. Measured selected RPC P99 was 311.135µs for Get and 427.882µs for Publish; known wake-to-run P99 was 3.883µs and 4.569µs. These are descriptive diagnostics, with uncalibrated observer overhead and no matched bRPC reference. They establish neither a 30% improvement nor full population quantiles.

Blocked time includes waiting for remote processing, transport and coordination. The RPC wall residual after off-CPU time is not independently measured CPU. Do not add unrelated server/CPU percentiles, call the whole IO-pump stack scheduler self cost, or label all blocked time network latency. The retained result does not validate mixed load, peak throughput, memory/lifetime gates or production readiness.
