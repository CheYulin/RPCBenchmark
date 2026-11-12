from pathlib import Path
import ctypes as ct,json,os,subprocess,sys,time,select,hashlib
from interval_model import split
from selection import selected
from bcc import BPF
j=Path(__file__).resolve().parent
write=lambda n,v:(j/n).write_text(json.dumps(v,indent=2))
def start(pid):
 r=Path('/proc/'+str(pid)+'/stat').read_text();return int(r[r.rindex(')')+2:].split()[19])
def fds():
 out={}
 for p in Path('/proc/self/fd').iterdir():
  try:out[p.name]=os.readlink(p)
  except FileNotFoundError:pass
 return out
assert os.sched_getaffinity(0)=={37}
assert BPF.support_raw_tracepoint() and hasattr(BPF,'detach_raw_tracepoint')
assert BPF.kernel_struct_has_field(b'task_struct',b'start_time')==1
state='__state' if BPF.kernel_struct_has_field(b'task_struct',b'__state')==1 else 'state'
assert BPF.kernel_struct_has_field(b'task_struct',state.encode())==1
code=(j/'intervals.bpf.c').read_text().replace('STATE_FIELD',state)
native=subprocess.check_output([str(j/'fixture-native'),'selector'],text=True)
actual=[tuple(map(int,line.split()))for line in native.splitlines()]
expected=[(phase,i)for phase,n in [(1,50000),(2,150000)]for i in range(n)if selected(phase,i)]
assert actual==expected
base=fds();p=None;b=None;attached=[];initial=None;data={'scope':'OWN_FIXTURE_ONLY_NO_DATA_SYSTEM_OR_PERFORMANCE_ACCEPTANCE','state_field':state,'source_sha256':hashlib.sha256((j/'intervals.bpf.c').read_bytes()).hexdigest()}
def line(expected,seconds):
 assert select.select([p.stdout],[],[],seconds)[0],expected+' timeout'
 assert p.stdout.readline().strip()==expected
try:
 p=subprocess.Popen(['taskset','-c','3',str(j/'fixture-native')],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,start_new_session=True);initial=start(p.pid);line('READY',3)
 b=BPF(text=code);cfg=b['config'].Leaf();cfg.enabled=0;cfg.tgid=p.pid;cfg.minimum_ns=0;b['config'][ct.c_int(0)]=cfg
 b['allowed'][ct.c_uint(p.pid)]=ct.c_ulonglong(0)
 b.attach_uprobe(name=str(j/'fixture-native'),sym='ds_fixture_begin',fn_name='on_api_begin',pid=p.pid)
 b.attach_uprobe(name=str(j/'fixture-native'),sym='ds_fixture_end',fn_name='on_api_end',pid=p.pid)
 for point,fn in (('sched_wakeup','on_wake'),('sched_switch','on_switch')):
  b.attach_raw_tracepoint(tp=point,fn_name=fn);attached.append(point)
 cfg.enabled=1;b['config'][ct.c_int(0)]=cfg;begin=time.monotonic_ns();p.stdin.write('GO\n');p.stdin.flush()
 deadline=time.monotonic()+.5;injected=False
 while time.monotonic()<deadline:
  pending=list(b['pending'].items())
  if pending:
   key,value=pending[0];fake=b['wakes'].Leaf();fake.wake_ns=int(value.out_ns)-1;b['wakes'][key]=fake;injected=True;break
  time.sleep(.0005)
 assert injected
 line('DONE',10);assert start(p.pid)==initial
 cfg.enabled=0;b['config'][ct.c_int(0)]=cfg
 p.stdin.write('STOP\n');p.stdin.flush();p.wait(timeout=3);assert not Path('/proc/'+str(p.pid)).exists()
 for event in list(b.uprobe_fds):b.detach_uprobe_event(event)
 for point in reversed(attached):b.detach_raw_tracepoint(tp=point)
 attached=[];end=time.monotonic_ns();counts=lambda:[sum(b['counters'][ct.c_int(i)])for i in range(32)];before=counts();rows=[]
 for k,v in b['intervals'].items():
  assert k.task.tgid==p.pid and k.task.tid==p.pid and k.task.generation>0
  row=dict(tgid=int(k.task.tgid),tid=int(k.task.tid),generation=int(k.task.generation),phase=int(k.task.phase),arrival=int(k.task.arrival),out_ns=int(k.out_ns),wake_ns=int(k.wake_ns),in_ns=int(k.in_ns),state=int(v.state),duplicate=bool(v.duplicate),preempt=int(v.preempt),raw_wake_ns=int(v.raw_wake_ns),flags=int(v.flags));row['split']=split(row['out_ns'],row['wake_ns'],row['in_ns'],row['state'],row['duplicate']);rows.append(row)
 assert counts()==before
 assert all(before[i]==0 for i in (6,7,8,9,10,13,18,19,20,26)),before
 assert before[11]==before[12]+before[14]+before[13]
 expected={(phase,i)for phase in (1,2)for i in range(4096)if selected(phase,i)}
 assert before[14]==len(rows) and len(rows)>=len(expected)
 assert {(r['phase'],r['arrival'])for r in rows}==expected
 assert any(r['in_ns']-r['out_ns']<500000 for r in rows)
 assert before[24]==before[25]==len(expected)
 assert before[16]==before[17]==8192 and not list(b['active'].items())
 assert not list(b['wakes'].items()) and not list(b['pending'].items())
 assert before[22]>=1 and sum(r['flags']==1 for r in rows)>=1
 assert all(r['wake_ns']==0 and r['raw_wake_ns']<r['out_ns'] and r['split']['runnable']is None for r in rows if r['flags']==1)
 assert sum(r['split']['runnable'] is not None and r['state']!=0 for r in rows)>=30
 data.update(selector_concordance_200k=True,selected_fixture_APIs=len(expected),injected_own_fixture_stale_wake=True,status='FIXTURE_PASS',capture_begin_ns=begin,capture_end_ns=end,counters=before,intervals=rows,pending_count=len(list(b['pending'].items())),all_tracepoints_detached=True,target_startticks=initial)
finally:
 if b is not None:
  try:
   cfg.enabled=0;b['config'][ct.c_int(0)]=cfg
   for point in reversed(attached):b.detach_raw_tracepoint(tp=point)
  finally:b.cleanup()
 if p is not None:
  try:
   if p.poll()is None:
    assert start(p.pid)==initial;p.stdin.write('STOP\n');p.stdin.flush()
   p.wait(timeout=3)
  finally:
   if p.poll()is None and start(p.pid)==initial:p.kill();p.wait(timeout=2)
   for stream in (p.stdin,p.stdout,p.stderr):stream.close()
  data['child_exit']=p.returncode;data['child_gone']=not Path('/proc/'+str(p.pid)).exists()
 data['FDs_before']=base;data['FDs_after']=fds();data['FDs_restored']=data['FDs_after']==base;write('fixture-result.json',data)
assert data['FDs_restored'] and data['child_exit']==0 and data['child_gone']
print(json.dumps({k:v for k,v in data.items()if k not in ('intervals','FDs_before','FDs_after')}),flush=True)
