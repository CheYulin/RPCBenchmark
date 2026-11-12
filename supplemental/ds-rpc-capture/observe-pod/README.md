# All-RPC POD observer

This diagnostic library observes the private SDK `Observe(RpcObservation const&)` entry before sample selection. It adds no SDK API, changes no capture ABI or selector, and launches no SDK or service. The SDK integration and latency acceptance remain pending. [validation.json](validation.json) records a successful standalone Linux fixture; it is not a performance result.

## Input and lifecycle

`PodObserver(output, image, callback_bytes, controller_pid, controller_startticks)` requires one explicit non-init controller. The reader and target must descend from that controller. `image` supplies the target's canonical path, SHA256, normal entry value/size/file offset, and 64-byte prologue SHA256. The caller must establish the actual POD layout against the target binary; compiling a similar structure alone is insufficient.

Call `prepare()` inside the reader before starting the SDK. It opens output with the target disabled. `bind(pid, startticks, callers, warm_epoch_ns)` requires exactly32 existing caller TIDs, positive-PID attachment, stable process identities and the exact mapped image. READY must precede the unchanged warm epoch. Poll until actual API completion and safe SDK closure, then `finish()` detaches its saved registration before bounded drain and audits its own perf FDs. A disappeared target does not bypass cleanup.

The BPF program reads ten typed members, totaling45 meaningful source bytes, into a zero-initialized72-byte record. It emits no source padding, pointer, string, endpoint or payload. The approved x86_64/BCC0.35 fixture observed a76-byte native callback; only72 event bytes are stored. Supply an exact independently established callback size; the SDK path has no automatic fallback.

Bounds are16 perf pages per online CPU, at most128 new perf FDs,250,000 records and128MiB raw storage. Per-CPU counters satisfy `allowed_seen = read_failed + submit_succeeded + submit_failed`; lost callbacks remain separate. Loss and monotonic read/submit failures stop polling. Full conservation and decoded/submitted equality are checked after detach/drain, when legitimate in-flight events have finished.

## Offline checks

`decode_file`, `join_events`, `crosscheck_prefixes` and `coverage_check` retain all arrivals, including rejected and uninvoked APIs. Join only unique same-TID intervals with `api_start <= send <= recv <= observed <= api_end`. Preserve errors, ambiguous joins and partial exits. Derive `required_methods` from the original frozen Begin/configuration, never observed events; the current Get/Set cohort uses Get=2 and Publish=16. Successful invoked APIs require a matching primary, allowing up to16 sequential RPCs.

Pass every original slot, RPC and End row, including unselected diagnostics. Compare per-caller cumulative phase/method counters, exact selected tuples/flags and immutable physical warm prefixes. A final prefix must cover every phase actually present; never add warm and cumulative final arrays. Aggregate totals cannot independently establish arbitrary unsampled per-API exact RPC distributions. These checks do not replace the original API/generator assessment or establish performance validity.

## Verification and limits

Run the47 pure/fake tests without BCC or a service:

```sh
cd supplemental/ds-rpc-capture/observe-pod
python3 -B -m unittest discover -v
```

The native fixture verified positive-PID coverage of32 listed threads, rejection of one unlisted thread and a separate own process, all scalar/signed values, poisoned source padding, zero destination reserved bytes, exact76-byte framing, expected NULL-read failure, and cleanup after target exit. It left no own processes, registrations or perf FDs. An initial CPU-binding command error stopped before compilation/BPF; only that argument was corrected before the successful attempt. Missing support files in an early old-version test invocation were setup errors; the complete old source closure produced seven assertion failures with the same eight regression fixtures, and the fixed source passed all eight.

`fixture.cpp` is a standalone input producer, using the existing parent capture headers and no SDK library:

```sh
g++ -std=gnu++17 -O2 -g1 -Wall -Wextra -Werror -fPIE -pie -fno-plt -pthread fixture.cpp -o /tmp/observe-pod-fixture
```

It prints33 persistent TIDs, accepts `emit`, `null` and `exit` on stdin, and does not attach probes itself. The native witness used the same C++ body and header bytes with a private bounded controller. Its host wrapper and raw process/maps logs are not published here. Real saturation/loss injection, complete SDK collection, observer overhead controls and the30% RPC target remain unverified.
