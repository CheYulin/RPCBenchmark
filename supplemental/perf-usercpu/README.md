# Offline user-CPU sample aggregation

This directory contains a standard-library Python parser and nine synthetic tests. It does not record events, attach to processes, launch a workload, or validate a profiler's ownership and cleanup. No actual profiles, identities, runtime images, or environment-specific manifests are included. Synthetic path/address strings in the tests exercise redaction; they are not captured data.

The output describes sampled user-mode CPU stacks. It cannot explain wall waits or RPC P99 by itself, establish a causal latency change, or promise a Get/Set optimization. A complete record stream and a successful workload are separate from performance acceptance.

Separate actual SDK/Worker Get diagnostics for bRPC and coro were decoded with this field layout. SDK unknown-leaf shares were about 61.3% and 67.3%, respectively. Those format checks establish no full-unwind proof or RPC P99 cause; the diagnostic supplies no Get/Set optimization claim.

## Declared input format

The text parser expects this explicit `perf script` field list:

```text
pid,tid,period,event,ip,sym,dso
```

Use the default **callee-first** callchain order, no inline expansion, and a declared maximum stack depth. Do not feed caller-first output or a different layout and assume it will be detected automatically. Isolate perf configuration so a user's callgraph settings cannot change the meaning of the first frame. For an existing private profile, an offline formatting example is:

```sh
env LC_ALL=C LANG=C TERM=dumb PERF_CONFIG=/dev/null \
  PERF_PAGER=cat PAGER=cat DEBUGINFOD_URLS="" \
  PERF_BUILDID_DIR="<PRIVATE_BUILD_ID_CACHE>" \
  perf script -i "<PRIVATE_PROFILE>" \
  -F pid,tid,period,event,ip,sym,dso \
  --no-inline --max-stack 127 --show-lost-events \
  > "<PRIVATE_SCRIPT_TEXT>" 2> "<PRIVATE_WARNING_TEXT>"
```

Replace every placeholder before use. Keep the profile, formatted stacks, addresses, maps, stderr, and role-to-process mapping in a private workspace. Review symbol aggregates before publishing them. This example formats existing data; it does not collect or qualify a profile.

## Python API

`summarize_script(lines, roles, max_symbols=64, max_stack=127)` consumes text lines and a private mapping from exactly two positive process IDs to `SDK` and `Worker`. It accepts `cpu-clock:u` and `cpu-clock` labels; the caller must establish that the capture used the intended user-only event. Foreign processes, including inherited fork descendants, are excluded and counted without returning their identities or symbols. Unexpected events are counted separately.

Per role, `leaf` attributes each sample to its first frame. `inclusive` counts each distinct symbol once per sample callchain, so recursive appearances do not multiply that sample. Inclusive symbols overlap: their counts and period weights are **not additive**. Neither table is a function-call counter. Period weights come from the recorded samples and are not wall-time or RPC-latency measurements.

The parser removes frame addresses, symbol offsets and DSO paths, and redacts path-like or unsuitable symbol text. It retains unknown leaves/frames, empty or single-frame chains, depth-limit observations, and bounded-table remainder counts. These observations do not prove complete unwinding; `unwind_completeness_proven` remains false.

`record_metadata(binary_stream)` reads a seekable `PERFILE2` stream in either byte order and counts sample, loss, throttle and other record types. Truncated tails, malformed loss records and opaque compressed records remain explicit; incomplete counts are labelled lower bounds. It is not a full perf-file validator or compressed-record decoder. `warning_counts(text)` returns only warning-category counts, without returning private warning text. No warnings does not establish complete unwinding.

The parser supplies no PID discovery, recording controller, symbol server, or benchmark gate. Keep raw data private and report unknown, lost, throttled and truncated observations alongside any aggregate.

## Tests

From this directory:

```sh
python3 -B -m unittest -v test_parse_perf_script
```

All fixtures are invented. The tests cover leaf/inclusive accounting, role exclusion, redaction, bounded output, loss/throttle records, byte order, truncation/compression, and warning uncertainty. Passing them does not certify an actual SDK or profiler run.
