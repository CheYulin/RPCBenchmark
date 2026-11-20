# Native notification batching: retained core component screen

## Result and scope

The four windows completed in the preregistered base / notify / notify / base order. The candidate did not demonstrate a latency benefit and is not promoted into DataSystem. Candidate/base arm-median ratios were P50 0.99654, P99 1.03898, completed QPS 0.99859, and server process CPU ticks 0.93471. Baseline P99 drift between its two windows was 5.37%; the small tail difference is not a demonstrated causal regression.

These are fixed-concurrency memory-echo component observations using the actual DataSystem client core. Actual QPS differs between arms. They do not qualify a same-offered-load bRPC comparison, real Worker business execution, SDK Get/Set or the 30% RPC target. CPU is the component server process, not the production Worker.

## Sources and build

Generate the pinned baseline core with the existing `../../generate_core_variants.py`, then apply `base-to-notify-adapter.patch` without fuzz. The core change uses the helper from the parent `native-notify` directory and preserves admission, business execution and Server shutdown. The adapter captures the public current context at synchronous handler entry; read `access-and-origin-addendum.md` for the source-bound same-pool invariant and its limits.

The first adapter attempted private `context_info_t::conn_` access and failed compilation before linking or tests. Its archive remains retained. The revised build used the existing baseline core object, rebuilt one shared entry/provider, compiled the candidate and new client, and linked both server images with that same entry/provider. All 40 commands closed normally; the diagnostic helper passed eight fresh-process cases and the client passed nine CLI checks. Full precompile dependencies and input hashes were checked. A public rebuild has no inherited private ELF qualification.

For the diagnostic variant, compile `native_bthread_notify_batch_diagnostic.cpp` with its provider instead of the pure helper implementation, never both. The external DataSystem header, patched YLT headers, bRPC library, core generator and image identities are recorded in `source-identities.json`. Reuse the existing manual CLI checks with the new client binary. These sources are optional manual experiments, with no default build change.

## Notification and lifetime evidence

The diagnostic adds one relaxed histogram atomic per nonempty helper flush, rather than per RPC. Both images link the same provider; baseline counters are zero. Candidate windows each recorded exactly 294944 successful NOSIGNAL submissions, including 32 prime, 32768 warm and 262144 measured calls. Mean logical submissions per helper flush were 2.3503 and 2.3641.

The 17 histogram buckets count successful submissions since a helper flush. They do not count OS wakeups, tasks newly made runnable, or tasks held until a flush. Tasks can execute earlier. Stop snapshots are read only after normal server/pool drain and native runner join; job count zero alone is not sufficient. The helper still depends on bRPC's unstable flush API.

## Evidence and disposition

All 1179648 warm/measured calls across four windows completed without API errors, mismatched bytes, exceptions or cancellations. All eight role processes and the stage exited normally, and the original build files remained unchanged. The complete-lifetime client RSS budget includes ledger formatting and does not establish a measured-interval memory benefit.

Full archive and independent-reader identities are recorded alongside the exact window metrics. The pure helper unit checkpoint is retained separately. No failed workload is rerun or reclassified. This experiment leaves the DataSystem production checkout unchanged; subsequent Get/Set measurement uses the existing production candidate.
