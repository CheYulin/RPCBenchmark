# Native bthread notification batching helper

## Scope

This retained experiment keeps each accepted business call in a fresh normal bthread. On the existing native YLT runner, the helper uses `BTHREAD_NOSIGNAL`, flushes after at most 16 successful submissions, and posts one remainder flush to that same native context. Tasks are already runnable before a flush. The policy does not count kernel wakeups or promise batches of 16.

The stack attempt surrounds callable construction. Factory, start and rejected-attempt failures flush earlier submissions. A failed post after successful enqueue flushes immediately while preserving accepted-task ownership. Queued callbacks capture only the native thread ID and use native TLS. Generic null, nonrunning, foreign-context and bthread callers use the ordinary background-task path.

## Unit evidence

The pinned private build completed all eight GREEN cases and all eight preregistered RED outcomes: four intentional failures and four successful inherited/fallback behaviors. Each case ran in a fresh process with an eight-second limit. All 29 commands and their stage closed normally, with no timeout or signal. The full archive and independent reader identities are in `source-identities.json`.

The first attempt stopped during preprocessing because Asio include paths were missing. It executed no tests and is retained separately. The successful attempt used the unchanged helper/test sources and the actual patched YLT include paths, with GNU C++20/O2, actual bRPC linking, precompile dependency hashes and compile dependency equality. A public rebuild has not inherited that private binary's runtime qualification.

## Build and lifetime prerequisites

Compile the test with either the GREEN implementation or the RED implementation, never both. Supply the matching DataSystem `bthread_utils.h`, patched YLT/Asio headers and bRPC headers/library. Invoke each case name from `TDD-plan.json` as a separate process; compare its exit status against that variant's map. This is a manual external-dependency experiment rather than a default repository test.

`bthread_flush` comes from bRPC's unstable API. The recorded library's ABI and behavior were exercised, but its exact upstream producer revision is unproven. Tail lifetime is justified only for the existing successful native runner and normal `pool.stop(false)` outstanding-work drain/join. Job count zero alone is insufficient. Force-stop, added poll/run callers, library unloading and synchronous stop on the IO callback need separate review.

## Limits and next step

The 64-held-task case checks helper cleanup; it does not establish the actual Dispatcher's 64-job admission gate. This checkpoint contains no DataSystem production change, RPC service execution, SDK Get/Set result or performance acceptance.

The initial adapter tried to access the private `context_info_t::conn_` member and failed compilation. That access is not part of this helper and must not be copied. The revised adapter captures the public current context at entry and relies on a separately reviewed invariant: the anonymous Dispatcher is reached only through its synchronous registration on the same native server pool. It does not independently verify connection affinity or detect arbitrary manual foreign-loop invocation.
