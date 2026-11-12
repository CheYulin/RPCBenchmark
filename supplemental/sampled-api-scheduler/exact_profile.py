import ctypes as ct,json,os,time,hashlib,platform
from pathlib import Path
from interval_model import split

def validate_targets(pid,tids):
    if type(pid)is not int or pid<=1 or len(tids)!=32 or len(set(tids))!=32 or any(type(t)is not int or t<=1 for t in tids):
        raise ValueError('owned SDK PID and exactly32 distinct positive caller TIDs required')

def task(pid,tid):
    p=Path(f'/proc/{pid}/task/{tid}');status=dict(l.split(':',1)for l in (p/'status').read_text().splitlines()if ':'in l)
    if int(status['Tgid'])!=pid or int(status['Pid'])!=tid:raise RuntimeError('caller TID ownership changed')
    fields=(p/'stat').read_text().rsplit(')',1)[1].split()
    return dict(tid=tid,startticks=int(fields[19]),affinity=status['Cpus_allowed_list'].strip())

def fds():
    result={}
    for p in Path('/proc/self/fd').iterdir():
        try:result[p.name]=os.readlink(p)
        except FileNotFoundError:pass
    return result

class ExactProfile:
    def __init__(self,out,headers,driver,expected_driver_sha256):
        if platform.machine()!='x86_64':raise RuntimeError('requires verified x86_64 driver marker ABI')
        self.driver=Path(driver)
        if type(expected_driver_sha256)is not str or len(expected_driver_sha256)!=64 or any(ch not in '0123456789abcdef'for ch in expected_driver_sha256):raise ValueError('requires exact owned driver SHA256')
        if hashlib.sha256(self.driver.read_bytes()).hexdigest()!=expected_driver_sha256:raise RuntimeError('owned driver bytes changed')
        from bcc import BPF
        self.uprobes=[]
        self.out=Path(out);self.out.mkdir();self.b=None;self.attached=[];self.bound=False;self.stopped=False;self.last_check=0
        self.record=dict(selection_seed=17,selection_modulus=64,all_intervals_of_selected_API=True,scope='OWN32CALLER_SELECTED_1OF64_ALL_OFFCPU',minimum_ns=0,capacity=32768,performance_accepted=False)
        assert BPF.support_raw_tracepoint() and hasattr(BPF,'detach_raw_tracepoint')
        assert BPF.kernel_struct_has_field(b'task_struct',b'start_time')==1
        field='__state' if BPF.kernel_struct_has_field(b'task_struct',b'__state')==1 else 'state'
        assert BPF.kernel_struct_has_field(b'task_struct',field.encode())==1
        os.environ['BCC_KERNEL_SOURCE']=str(headers);self.base=fds()
        try:
            code=Path(__file__).with_name('intervals.bpf.c').read_text().replace('STATE_FIELD',field);self.b=BPF(text=code)
            self.cfg=self.b['config'].Leaf();self.cfg.minimum_ns=0;self.b['config'][ct.c_int(0)]=self.cfg
            for point,fn in (('sched_wakeup','on_wake'),('sched_switch','on_switch')):
                self.b.attach_raw_tracepoint(tp=point,fn_name=fn);self.attached.append(point)
            self.record.update(precompiled_disabled=True,state_field=field,FDs_before=self.base,FDs_loaded=fds(),precompile_done_ns=time.monotonic_ns())
            assert len(self.record['FDs_loaded'])-len(self.base)<=32
        except BaseException:
            self.close(partial=True);raise
    def bind(self,pid,tids,epoch):
        validate_targets(pid,tids)
        if self.bound:raise RuntimeError('cannot rebind scheduling target')
        before=[task(pid,t)for t in tids]
        if any(r['affinity']!='3,5,7,9,11,13,15' for r in before):raise RuntimeError('caller CPU mask changed')
        for tid in tids:self.b['allowed'][ct.c_uint(tid)]=ct.c_ulonglong(0)
        for sym,fn in (('_ZN17benchmark_capture7Session5BeginEmmmmmm','on_api_begin'),('_ZN17benchmark_capture7Session3EndEmmmi','on_api_end')):
            prior=set(self.b.uprobe_fds)
            self.b.attach_uprobe(name=str(self.driver),sym=sym,fn_name=fn,pid=pid)
            events=set(self.b.uprobe_fds)-prior;assert len(events)==1
            self.uprobes.extend(events)
        self.cfg.tgid=pid;self.cfg.enabled=1;self.b['config'][ct.c_int(0)]=self.cfg;now=time.monotonic_ns()
        after=[task(pid,t)for t in tids]
        if before!=after or now>=epoch:raise RuntimeError('target identity or pre-epoch bind failed')
        bound_fds=fds();assert len(bound_fds)-len(self.base)<=40
        self.bound=True;self.record.update(FDs_bound=bound_fds,pid=pid,caller_tasks=before,capture_start_ns=now,original_warm_epoch_ns=epoch)
    def check(self):
        if self.bound and time.monotonic_ns()-self.record['capture_start_ns']>75_000_000_000:raise RuntimeError('exact scheduler capture deadline')
        if self.b and time.monotonic()-self.last_check>.5:
            self.last_check=time.monotonic()
            if any(sum(self.b['counters'][ct.c_int(i)]) for i in (6,7,8,9,10,13,18,19,20,26)):raise RuntimeError('exact scheduler counter failure')
    def close(self,partial=False):
        if self.stopped:return
        self.stopped=True;valid=False
        try:
            if self.b is not None:
                self.cfg.enabled=0;self.b['config'][ct.c_int(0)]=self.cfg
                detach_owned_uprobes(self.b,self.uprobes)
                self.uprobes=[]
                for point in reversed(self.attached):self.b.detach_raw_tracepoint(tp=point)
                self.attached=[];stop=time.monotonic_ns();counts=lambda:[sum(self.b['counters'][ct.c_int(i)])for i in range(32)];first=counts();rows=[]
                for k,v in self.b['intervals'].items():
                    row=dict(tgid=int(k.task.tgid),tid=int(k.task.tid),generation=int(k.task.generation),phase=int(k.task.phase),arrival=int(k.task.arrival),out_ns=int(k.out_ns),wake_ns=int(k.wake_ns),in_ns=int(k.in_ns),state=int(v.state),duplicate=bool(v.duplicate),preempt=int(v.preempt),raw_wake_ns=int(v.raw_wake_ns),flags=int(v.flags))
                    split(row['out_ns'],row['wake_ns'],row['in_ns'],row['state'],row['duplicate']);rows.append(row)
                pending=[dict(tgid=int(k.tgid),tid=int(k.tid),generation=int(k.generation),phase=int(k.phase),arrival=int(k.arrival),out_ns=int(v.out_ns),wake_ns=int(v.wake_ns))for k,v in self.b['pending'].items()]
                active=[dict(tid=int(k.value),begin_ns=int(v.begin_ns),phase=int(v.phase),arrival=int(v.arrival),selected=bool(v.selected))for k,v in self.b['active'].items()]
                wake_pending=[dict(tgid=int(k.tgid),tid=int(k.tid),generation=int(k.generation),phase=int(k.phase),arrival=int(k.arrival),wake_ns=int(v.wake_ns))for k,v in self.b['wakes'].items()]
                stable=first==counts();valid=stable and not any(first[i]for i in (6,7,8,9,10,13,18,19,20,26)) and first[11]==first[12]+first[14] and first[14]==len(rows) and first[1]==first[11]+sum(bool(r['out_ns'])for r in pending) and first[16]==first[17]+len(active) and first[24]==first[25]+sum(x['selected']for x in active)
                self.record.update(capture_stop_ns=stop,counters=first,intervals=rows,pending_right_censored=pending,wake_pending_right_censored=wake_pending,API_active_at_stop=active,API_uprobes_detached=True,counters_stable_after_detach=stable,kernel_accounting_valid=valid,all_raw_tracepoints_detached=True,partial=partial)
        finally:
            if self.b is not None:self.b.cleanup();self.b=None
            after=fds();kernel=lambda entries:{k:v for k,v in entries.items()if 'bpf' in v.lower() or 'raw_tracepoint' in v.lower() or 'perf_event' in v.lower()}
            self.record.update(FDs_after=after,BPF_FDs_restored=kernel(after)==kernel(self.base),FD_scope='BPF/raw-tracepoint/perf-event descriptors only; outer SDK/reader log FDs may legitimately differ')
            (self.out/'exact-scheduler.json').write_text(json.dumps(self.record,indent=2))
        if not partial and (not valid or not self.record['BPF_FDs_restored'] or not self.bound):raise RuntimeError('exact scheduler capture/closure invalid')

class JoinedObserver:
    def __init__(self,pod_observer,exact):self.inner=pod_observer;self.pod=pod_observer.pod;self.exact=exact
    def check(self,target_exited=False):self.inner.check(target_exited=target_exited);self.exact.check()
    def phase(self,sdk,original,tids,phase):
        if phase['phase_id']==1:self.exact.bind(sdk.pid,tids,phase['plan']['epoch_ns'])
        self.inner.phase(sdk,original,tids,phase)
    def stop(self,partial):
        try:self.exact.close(partial=partial)
        finally:self.inner.stop(partial)

def detach_owned_uprobes(bpf,events):
    for event in reversed(events):bpf.detach_uprobe_event(event)
