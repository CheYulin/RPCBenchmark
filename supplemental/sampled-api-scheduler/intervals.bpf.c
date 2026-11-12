/* SPDX-License-Identifier: Apache-2.0
 * Derived from rpc-scheduler-b runq.bpf.c; exact intervals replace time bins. */
#include <uapi/linux/bpf.h>
#include <linux/sched.h>
#include <uapi/linux/ptrace.h>
struct config_t { u32 enabled; u32 tgid; u64 minimum_ns; };
struct key_t { u32 tgid; u32 tid; u64 generation; u64 phase; u64 arrival; };
struct active_t { u64 begin_ns; u64 phase; u64 arrival; u32 selected; };
struct pending_t { u64 out_ns; u64 wake_ns; u64 state; u32 duplicate; u32 preempt; };
struct interval_key { struct key_t task; u64 out_ns; u64 wake_ns; u64 in_ns; };
struct interval_value { u64 state; u32 duplicate; u32 preempt; u64 raw_wake_ns; u32 flags; };
BPF_ARRAY(config,struct config_t,1);
BPF_HASH(allowed,u32,u64,128);
BPF_HASH(pending,struct key_t,struct pending_t,128);
BPF_HASH(wakes,struct key_t,struct pending_t,128);
BPF_HASH(intervals,struct interval_key,struct interval_value,32768);
BPF_HASH(active,u32,struct active_t,128);
BPF_PERCPU_ARRAY(counters,u64,32);
static inline void count(u32 i) { u64 *v=counters.lookup(&i); if(v)(*v)++; }
static inline int target(struct task_struct *task,struct key_t *k) {
    u32 zero=0; struct config_t *cfg=config.lookup(&zero);
    if(!cfg || !cfg->enabled || !task)return 0;
    if(bpf_probe_read_kernel(&k->tgid,sizeof(k->tgid),&task->tgid)){count(7);return 0;}
    if(k->tgid!=cfg->tgid)return 0;
    if(bpf_probe_read_kernel(&k->tid,sizeof(k->tid),&task->pid)){count(7);return 0;}
    u64 *generation=allowed.lookup(&k->tid);if(!generation)return 0;
    struct active_t *api=active.lookup(&k->tid);if(!api || !api->selected)return 0;
    k->phase=api->phase;k->arrival=api->arrival;
    if(bpf_probe_read_kernel(&k->generation,sizeof(k->generation),&task->start_time) || !k->generation){count(7);return 0;}
    if(!*generation)*generation=k->generation;
    if(*generation!=k->generation){count(8);return 0;}
    return 1;
}
int on_wake(struct bpf_raw_tracepoint_args *ctx) {
    u64 now=bpf_ktime_get_ns();struct key_t k={};if(!target((struct task_struct *)ctx->args[0],&k))return 0;
    count(0);struct pending_t *p=wakes.lookup(&k);
    if(p){p->duplicate=1;count(5);return 0;}
    struct pending_t v={};v.wake_ns=now;if(wakes.insert(&k,&v))count(9);
    if(!pending.lookup(&k))count(4);
    return 0;
}
int on_switch(struct bpf_raw_tracepoint_args *ctx) {
    u64 now=bpf_ktime_get_ns();struct key_t prev={};struct key_t next={};
    struct task_struct *task=(struct task_struct *)ctx->args[1];
    if(target(task,&prev)) {
        count(1);struct pending_t value={};value.out_ns=now;value.preempt=(u32)ctx->args[0];
        if(bpf_probe_read_kernel(&value.state,sizeof(task->STATE_FIELD),&task->STATE_FIELD)){count(7);return 0;}
        if(pending.lookup(&prev))count(6);
        if(pending.update(&prev,&value))count(9);
    }
    if(!target((struct task_struct *)ctx->args[2],&next))return 0;
    count(2);struct pending_t *found=pending.lookup(&next);
    if(!found){wakes.delete(&next);count(3);return 0;}
    struct pending_t p=*found;struct pending_t *wake=wakes.lookup(&next);
    if(wake){p.wake_ns=wake->wake_ns;p.duplicate=wake->duplicate;}
    pending.delete(&next);wakes.delete(&next);
    if(!p.out_ns){count(3);return 0;}
    if(now<p.out_ns){count(10);return 0;}
    u64 raw_wake=p.wake_ns;u32 flags=0;
    if(p.wake_ns && p.wake_ns<p.out_ns){count(22);flags=1;p.wake_ns=0;p.duplicate=1;}
    if(p.wake_ns>now){count(23);flags=2;p.wake_ns=0;p.duplicate=1;}
    if(!p.wake_ns && p.state!=0)count(15);
    count(11);u32 zero=0;struct config_t *cfg=config.lookup(&zero);if(!cfg)return 0;
    if(now-p.out_ns<cfg->minimum_ns){count(12);return 0;}
    struct interval_key k={};k.task=next;k.out_ns=p.out_ns;k.wake_ns=p.wake_ns;k.in_ns=now;
    struct interval_value v={};v.state=p.state;v.duplicate=p.duplicate;v.preempt=p.preempt;v.raw_wake_ns=raw_wake;v.flags=flags;
    if(intervals.insert(&k,&v))count(13);else count(14);
    return 0;
}

static inline u64 mix(u64 value) {
    value=(value^(value>>30))*0xbf58476d1ce4e5b9ULL;
    value=(value^(value>>27))*0x94d049bb133111ebULL;
    return value^(value>>31);
}
static inline int api_boundary(u32 begin,u64 phase,u64 arrival) {
    u32 zero=0;struct config_t *cfg=config.lookup(&zero);if(!cfg || !cfg->enabled)return 0;
    u64 id=bpf_get_current_pid_tgid();u32 tgid=id>>32;u32 tid=id;
    if(tgid!=cfg->tgid || !allowed.lookup(&tid))return 0;
    if(begin) {
        count(16);if(active.lookup(&tid)){count(18);return 0;}
        if(phase!=1 && phase!=2){count(26);return 0;}
        struct active_t value={};value.begin_ns=bpf_ktime_get_ns();value.phase=phase;value.arrival=arrival;
        value.selected=(mix(17ULL^mix(phase)^mix(arrival+0x9e3779b97f4a7c15ULL))%64)==0;
        if(value.selected)count(24);
        if(active.insert(&tid,&value))count(20);
    }else {
        count(17);struct active_t *api=active.lookup(&tid);
        if(!api)count(19);else {if(api->selected)count(25);active.delete(&tid);}
    }
    return 0;
}
int on_api_begin(struct pt_regs *ctx) {return api_boundary(1,PT_REGS_PARM3(ctx),PT_REGS_PARM4(ctx));}
int on_api_end(struct pt_regs *ctx) {return api_boundary(0,0,0);}
