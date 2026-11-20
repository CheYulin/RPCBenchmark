# Native notify adapter v2 — access correction, source only

V1 source `05fb…` and failed build are immutable. `context_info_t::conn_` is private; retract the earlier public-access
and independent connection-executor comparison claims in v1/SDD93a. Do not extend YLT API or bypass access control.
The helper/header/diagnostic provider/entry are unchanged. V2 captures public `coro_io::get_current()` once at Invoke
entry and passes that pointer to the existing helper. Null/nonrunning/non-native cases retain ordinary start.

This is a narrower source-bound adapter contract, not an equivalent independent connection check. Dispatcher has
anonymous-namespace implementation scope; the core has exactly one InvokeV2 definition and one address registration,
`register_handler<&Dispatcher::InvokeV2>`. There is no direct call. In pinned YLT, acceptors use this Server's pool
(`server.hpp lines 313–344`, `acceptor.hpp lines 46–66,113–142`); connection routing invokes the synchronous registered handler
(`connection.hpp lines 347–391`, router `5e8216a7…`: synchronous route/execution). Every such handler runs on the unchanged
native runner of that Server pool, where run sets current (`pool ec89…:212–258`). Connection startup may remain on
another acceptor context in the same pool; flushing the actual native producer there is correct, not connection affinity.

The fixed path adds no poll/run caller, manual Dispatcher call, coroutine handler, nested foreign loop or callback
that executes business inline; original background enqueue does not switch into business on this native producer.
Thus the captured current belongs to the same live native pool whose normal stop drains/joins the tail callback.
Manual invocation under a misleading outer/foreign current is **not** claimed detected; adding such a call invalidates
this adapter proof and requires review. Existing helper foreign-context tests remain generic helper tests, not proof
of an adapter connection comparison. Off-pool direct use with null current falls back, without an asynchronous tail.

No flush lifetime or counting contract changes: ID-only tail plus native TLS, normal pool.stop(false) outstanding-work
drain/join, unchanged admission/business/Client/Server shutdown. Unit archive `8fcc4b98…` qualifies unchanged helper
source only. Diagnostic helper still needs fresh GREEN8 and CLI9 against actual newly built images. Failed v1 archive
`f6520552…` records 8 closed commands, 0 objects/links/tests/server workload and 1077 unchanged input pins.
This addendum authorizes no build or performance window; root's review and the failed-archive second read precede v2.
