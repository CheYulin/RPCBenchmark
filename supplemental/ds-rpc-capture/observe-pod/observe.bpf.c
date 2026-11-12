#include <uapi/linux/ptrace.h>
struct Observation {
    u64 observed_ns, send_ns, recv_ns, server_processing_ns, server_execution_ns;
    u32 tgid, tid, cpu;
    s32 controller_error, ds_error;
    u16 method;
    u8 failed, processing_state, execution_state;
    u8 reserved[7];
};
typedef char event_size_must_be_72[(sizeof(struct Observation) == 72) ? 1 : -1];
BPF_ARRAY(target, u32, 1);
BPF_HASH(callers, u32, u8, 32);
BPF_PERCPU_ARRAY(counts, u64, 4);
BPF_PERF_OUTPUT(events);
static __always_inline void increment(u32 key)
{
    u64 *value = counts.lookup(&key);
    if (value) __sync_fetch_and_add(value, 1);
}
int observe_entry(struct pt_regs *ctx)
{
    u64 identity = bpf_get_current_pid_tgid();
    u32 tgid = identity >> 32, tid = identity, zero = 0;
    u32 *wanted = target.lookup(&zero);
    if (!wanted || !*wanted || tgid != *wanted || !callers.lookup(&tid)) return 0;
    increment(0);
    struct Observation event = {};
    event.observed_ns = bpf_ktime_get_ns();
    event.tgid = tgid; event.tid = tid; event.cpu = bpf_get_smp_processor_id();
    const char *source = (const char *)PT_REGS_PARM1(ctx);
#define READ_MEMBER(field, offset) \
    if (bpf_probe_read_user(&event.field, sizeof(event.field), source + offset)) { increment(1); return 0; }
    READ_MEMBER(send_ns, 0);
    READ_MEMBER(recv_ns, 8);
    READ_MEMBER(method, 16);
    READ_MEMBER(failed, 18);
    READ_MEMBER(controller_error, 20);
    READ_MEMBER(ds_error, 24);
    READ_MEMBER(server_processing_ns, 32);
    READ_MEMBER(server_execution_ns, 40);
    READ_MEMBER(processing_state, 48);
    READ_MEMBER(execution_state, 49);
#undef READ_MEMBER
    if (events.perf_submit(ctx, &event, sizeof(event))) increment(3);
    else increment(2);
    return 0;
}
