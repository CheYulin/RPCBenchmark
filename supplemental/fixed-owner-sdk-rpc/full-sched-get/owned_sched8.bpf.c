// SPDX-License-Identifier: GPL-2.0
#include <uapi/linux/ptrace.h>
#include <uapi/linux/bpf.h>
#define TASKS 8
#define CPU_SLOTS 256
#define OWNER_CAPACITY 65536
#define GET_PAIR_MAX 0xffffffffULL
#define CAS_ATTEMPTS 16
#define OPEN (1ULL << 63)
#define PHASE (1ULL << 62)
#define FAULT (1ULL << 61)
#define COUNT (FAULT - 1)
// COUNT is simultaneous callbacks, not events; active depth must remain below 2^61.
struct owner { u32 tgid; u32 slot; };
struct birth { u64 task; u64 leader; };
struct gate { u64 word; u64 padding[7]; };
struct bounds { u64 begin; u64 end; };
struct event { u64 event_ns; u64 commit_index_plus_one; u32 owner_slot; u32 cpu; u16 kind; u16 flags; u32 value; };
_Static_assert(sizeof(struct event) == 32, "event ABI");
BPF_HASH(allowed, u32, struct owner, TASKS);
BPF_ARRAY(births, struct birth, TASKS);
BPF_ARRAY(gates, struct gate, CPU_SLOTS);
BPF_ARRAY(requested_mode, u32, 1);
BPF_ARRAY(control, u64, 1);
BPF_ARRAY(deadline, u64, 1);
BPF_ARRAY(seal_bounds, struct bounds, 3);
BPF_ARRAY(bad, u64, 4);
BPF_ARRAY(seen, u64, TASKS);
BPF_ARRAY(next_record, u64, TASKS);
BPF_ARRAY(get_next, u64, TASKS);
BPF_ARRAY(get_pending, u64, TASKS);
BPF_ARRAY(records, struct event, TASKS * OWNER_CAPACITY);

static __always_inline int eligible(u64 word, u32 mode) {
    return (word & OPEN) && !(word & FAULT) && !!(word & PHASE) == (mode == 2);
}
static __always_inline struct gate *enter(u32 mode) {
    u32 cpu = bpf_get_smp_processor_id();
    struct gate *gate = gates.lookup(&cpu); if (!gate) return 0;
    u64 before = __sync_val_compare_and_swap(&gate->word, 0, 0);
    if (!eligible(before, mode)) return 0;
    if ((before & COUNT) == COUNT) { __sync_fetch_and_or(&gate->word, FAULT); return 0; }
    __sync_fetch_and_add(&gate->word, 1);
    u64 after = __sync_val_compare_and_swap(&gate->word, 0, 0);
    if (after & FAULT) {
        __sync_fetch_and_sub(&gate->word, 1);
        return 0;
    }
    if (eligible(after, mode)) return gate;
    __sync_fetch_and_sub(&gate->word, 1);
    return 0;
}
static __always_inline void leave(struct gate *gate) { __sync_fetch_and_sub(&gate->word, 1); }
static __always_inline void failed(u32 code) {
    u64 *value = bad.lookup(&code); if (value) __sync_lock_test_and_set(value, 1);
}
static __always_inline int reserve(u64 *counter, u64 *index, u32 error) {
    u64 before = __sync_val_compare_and_swap(counter, 0, 0);
    for (u32 attempt = 0; attempt < CAS_ATTEMPTS; ++attempt) {
        if (before == ~0ULL) { failed(error); return 0; }
        u64 actual = __sync_val_compare_and_swap(counter, before, before + 1);
        if (actual == before) { *index = before; return 1; }
        before = actual;
    }
    failed(error); return 0;
}
static __always_inline u32 mode(u64 *event_ns) {
    u32 zero = 0;
    u64 *value = control.lookup(&zero);
    u64 *limit = deadline.lookup(&zero);
    *event_ns = bpf_ktime_get_ns();
    if (!value || !limit || (*limit && *event_ns >= *limit)) return 0;
    return (u32)*value;
}
static __always_inline int field(void *task, u32 offset, void *value, u32 size) {
    return bpf_probe_read_kernel(value, size, (char *)task + offset);
}
static __always_inline int cookies(void *task, u64 *start, u64 *leader_start) {
    void *leader = 0;
    if (field(task, @START@, start, sizeof(*start)) || field(task, @LEADER@, &leader, sizeof(leader))
        || !leader || field(leader, @START@, leader_start, sizeof(*leader_start))) return -1;
    return 0;
}
static __always_inline struct owner *owned(void *task) {
    u32 tid = 0, tgid = 0;
    if (field(task, @PID@, &tid, sizeof(tid))) { failed(0); return 0; }
    struct owner *owner = allowed.lookup(&tid); if (!owner) return 0;
    u64 start = 0, leader = 0;
    if (owner->slot >= TASKS || field(task, @TGID@, &tgid, sizeof(tgid))
        || cookies(task, &start, &leader)) { failed(0); return 0; }
    struct birth *saved = births.lookup(&owner->slot);
    if (!saved || !saved->task || !saved->leader || tgid != owner->tgid
        || start != saved->task || leader != saved->leader) { failed(1); return 0; }
    u64 *visits = seen.lookup(&owner->slot); if (visits) __sync_fetch_and_add(visits, 1);
    return owner;
}
static __always_inline void emit(struct owner *owner, u64 now, u16 kind, u16 flags, u32 value) {
    u64 *next = next_record.lookup(&owner->slot); if (!next) { failed(0); return; }
    u64 index = 0; if (!reserve(next, &index, 3)) return;
    if (index >= OWNER_CAPACITY) { failed(3); return; }
    u32 key = owner->slot * OWNER_CAPACITY + index;
    struct event *entry = records.lookup(&key); if (!entry) { failed(0); return; }
    entry->event_ns = now; entry->owner_slot = owner->slot;
    entry->cpu = bpf_get_smp_processor_id(); entry->kind = kind; entry->flags = flags; entry->value = value;
    if (__sync_val_compare_and_swap(&entry->commit_index_plus_one, 0, index + 1)) failed(2);
}

int open_gate(struct __sk_buff *unused) {
    u32 zero = 0, *requested = requested_mode.lookup(&zero);
    u64 *mode_value = control.lookup(&zero);
    if (!requested || !mode_value || (*requested != 1 && *requested != 2) || *mode_value) return 2;
    u32 selected = *requested;
    struct bounds *bounds = seal_bounds.lookup(&selected);
    if (!bounds || bounds->begin) return 2;
    for (u64 cpu = 0; cpu < CPU_SLOTS; ++cpu) {
        u32 key = (u32)cpu;
        struct gate *gate = gates.lookup(&key);
        if (!gate || (gate->word & (OPEN | FAULT))) return 2;
    }
    u64 phase = selected == 2 ? PHASE : 0;
    for (u64 cpu = 0; cpu < CPU_SLOTS; ++cpu) {
        u32 key = (u32)cpu;
        struct gate *gate = gates.lookup(&key); if (!gate) return 2;
        __sync_fetch_and_or(&gate->word, OPEN | phase);
        u64 after = __sync_val_compare_and_swap(&gate->word, 0, 0);
        if (!eligible(after, selected)) return 2;
    }
    __sync_lock_test_and_set(mode_value, selected);
    return 0;
}
int seal_gate(struct __sk_buff *unused) {
    u32 zero = 0, *requested = requested_mode.lookup(&zero);
    u64 *mode_value = control.lookup(&zero);
    if (!requested || !mode_value || (*requested != 1 && *requested != 2)) return 2;
    u32 selected = *requested;
    struct bounds *bounds = seal_bounds.lookup(&selected); if (!bounds) return 2;
    __sync_val_compare_and_swap(&bounds->begin, 0, bpf_ktime_get_ns());
    __sync_lock_test_and_set(mode_value, 0);
    u32 busy = 0, fault = 0;
    for (u64 cpu = 0; cpu < CPU_SLOTS; ++cpu) {
        u32 key = (u32)cpu;
        struct gate *gate = gates.lookup(&key); if (!gate) { fault = 1; continue; }
        __sync_fetch_and_and(&gate->word, ~OPEN);
        u64 after = __sync_val_compare_and_swap(&gate->word, 0, 0);
        busy |= (after & COUNT) != 0; fault |= (after & FAULT) != 0;
    }
    __sync_val_compare_and_swap(&bounds->end, 0, bpf_ktime_get_ns());
    return fault ? 2 : busy;
}
int bootstrap(struct bpf_perf_event_data *ctx) {
    struct gate *lease = enter(1); if (!lease) return 0;
    u64 now = 0; if (mode(&now) != 1) goto out;
    u64 id = bpf_get_current_pid_tgid(); u32 tid = id, tgid = id >> 32;
    struct owner *owner = allowed.lookup(&tid);
    if (!owner || owner->tgid != tgid || owner->slot >= TASKS) goto out;
    void *task = (void *)bpf_get_current_task();
    u32 actual_tid = 0; u64 start = 0, leader = 0;
    if (field(task, @PID@, &actual_tid, sizeof(actual_tid)) || actual_tid != tid
        || cookies(task, &start, &leader) || !start || !leader) { failed(0); goto out; }
    struct birth *saved = births.lookup(&owner->slot); if (!saved) { failed(0); goto out; }
    u64 old = __sync_val_compare_and_swap(&saved->task, 0, start);
    if (old && old != start) { failed(1); goto out; }
    old = __sync_val_compare_and_swap(&saved->leader, 0, leader);
    if (old && old != leader) failed(1);
out:
    leave(lease); return 0;
}
int on_wakeup(struct bpf_raw_tracepoint_args *ctx) {
    struct gate *lease = enter(2); if (!lease) return 0;
    u64 now = 0; if (mode(&now) != 2) goto out;
    struct owner *owner = owned((void *)ctx->args[0]); if (owner) emit(owner, now, 1, 0, 0);
out:
    leave(lease); return 0;
}
int on_wakeup_new(struct bpf_raw_tracepoint_args *ctx) {
    struct gate *lease = enter(2); if (!lease) return 0;
    u64 now = 0; if (mode(&now) != 2) goto out;
    struct owner *owner = owned((void *)ctx->args[0]); if (owner) emit(owner, now, 2, 0, 0);
out:
    leave(lease); return 0;
}
int on_switch(struct bpf_raw_tracepoint_args *ctx) {
    struct gate *lease = enter(2); if (!lease) return 0;
    u64 now = 0; if (mode(&now) != 2) goto out;
    struct owner *before = owned((void *)ctx->args[1]);
    if (before) {
        u32 state = 0;
        if (field((void *)ctx->args[1], @STATE@, &state, sizeof(state))) failed(0);
        else emit(before, now, 4, ctx->args[0] ? 1 : 0, state);
    }
    struct owner *after = owned((void *)ctx->args[2]); if (after) emit(after, now, 3, 0, 0);
out:
    leave(lease); return 0;
}
static __always_inline void get_event(u16 kind) {
    struct gate *lease = enter(2); if (!lease) return;
    u64 now = 0; if (mode(&now) != 2) goto out;
    struct owner *owner = owned((void *)bpf_get_current_task()); if (!owner) goto out;
    u64 *next = get_next.lookup(&owner->slot), *pending = get_pending.lookup(&owner->slot);
    if (!next || !pending) { failed(0); goto out; }
    u64 pair = 0; u16 flags = 0;
    if (kind == 5) {
        u64 prior = 0; if (!reserve(next, &prior, 2)) goto out;
        if (prior >= GET_PAIR_MAX) { failed(2); flags = 2; }
        else {
            pair = prior + 1;
            if (__sync_val_compare_and_swap(pending, 0, pair)) { failed(2); flags = 2; }
        }
    } else {
        pair = __sync_lock_test_and_set(pending, 0);
        if (!pair || pair > GET_PAIR_MAX) { failed(2); flags = 2; pair = 0; }
    }
    emit(owner, now, kind, flags, (u32)pair);
out:
    leave(lease);
}
int on_get_entry(struct pt_regs *ctx) { get_event(5); return 0; }
int on_get_return(struct pt_regs *ctx) { get_event(6); return 0; }
