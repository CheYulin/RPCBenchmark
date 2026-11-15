import hashlib,json,os,re,signal,socket,statistics,subprocess,time
from pathlib import Path
import run_matrix as lab
r=Path(__file__).parent;out=r/'screen-v1';out.mkdir();groups=lab.Groups(True)
CLIENT={1,3,5,7};SERVER={17,19,21,23}
plan=json.loads((r/'screen-plan.json').read_text());manifest=json.loads((r/'perf-source-inputs-v2.json').read_text())
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def write(p,d):p.write_text(json.dumps(d,indent=2)+'\n')
def pins():
 for n,h in manifest.items():
  if sha(r/n)!=h:raise RuntimeError('frozen input changed: '+n)
def owner(pid):
 f=Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split();return dict(pid=pid,startticks=int(f[19]),pgrp=int(f[2]),session=int(f[3]),exe=os.readlink(f'/proc/{pid}/exe'))
def parse_cpus(v):
 result=set()
 for part in v.split(','):
  a=part.split('-');result.update(range(int(a[0]),int(a[-1])+1))
 return result
identities={}
def launch(role,cpus,args,stdout,stderr):
 p=groups.launch(role,cpus,args,stdout=stdout,stderr=stderr,start_new_session=True);own=owner(p.pid)
 if own['pgrp']!=p.pid or own['session']!=p.pid or Path(own['exe']).resolve()!=Path(args[0]).resolve():raise RuntimeError('child owner/exe proof failed')
 identities[p.pid]=own;return p

def stop(p):
 if p is None:return
 if p.poll()is None:
  if owner(p.pid)!=identities[p.pid]:raise RuntimeError('child identity changed; no signal')
  os.killpg(p.pid,signal.SIGTERM)
  try:p.wait(timeout=5)
  except subprocess.TimeoutExpired:
   if owner(p.pid)!=identities[p.pid]:raise RuntimeError('child identity changed before owned kill')
   os.killpg(p.pid,signal.SIGKILL);p.wait(timeout=3);raise RuntimeError('owned child required SIGKILL')
 if Path(f'/proc/{p.pid}').exists():raise RuntimeError('owned child remains')

def tasks(p,cpus):
 result=[]
 for d in Path(f'/proc/{p.pid}/task').glob('[0-9]*'):
  try:
   f=(d/'stat').read_text().rsplit(')',1)[1].split();status=dict(line.split(':',1)for line in(d/'status').read_text().splitlines()if ':'in line);allowed=parse_cpus(status['Cpus_allowed_list'].strip())
   if allowed!=cpus or int(f[16])!=10:raise RuntimeError('task affinity/nice differs from frozen roles')
   result.append(dict(tid=int(d.name),startticks=int(f[19]),affinity=sorted(allowed),nice=int(f[16]),CPU_ticks=int(f[11])+int(f[12])))
  except FileNotFoundError:pass
 return result

def tcp_count(port):
 q=subprocess.run(['ss','-Htn','state','established',f'( sport = :{port} )'],capture_output=True,text=True,check=True,timeout=2);return len(q.stdout.splitlines())

pins();write(out/'plan.json',plan)
physical=[]
for cpu in sorted(CLIENT|SERVER):
 path=Path(f'/sys/devices/system/cpu/cpu{cpu}/topology');physical.append((int((path/'physical_package_id').read_text()),int((path/'core_id').read_text())))
if len(set(physical))!=8:raise RuntimeError('role CPUs reuse SMT siblings')
if os.sched_getaffinity(0)!={35}or os.getpriority(os.PRIO_PROCESS,0)!=10:raise RuntimeError('metadata placement changed')
for group in groups.roles.values():
 if (group/'cgroup.procs').read_text().strip():raise RuntimeError('role group not initially empty')
results=[];error=None
try:
 for concurrency in [1,32]:
  for index,single in enumerate([0,1,1,0]):
   pins();case=out/f'c{concurrency}-{index}-s{single}';case.mkdir();port=lab.free_port();server=client=None;handles=[]
   try:
    for name in ['server.stdout','server.stderr','client.stdout','client.stderr']:handles.append((case/name).open('w'))
    sf,se,cf,ce=handles;server_args=[str(r/'perf-server'),'server','--port',str(port),'--workers','4'];client_args=[str(r/'perf-client'),'client','--singleflight',str(single),'--port',str(port),'--concurrency',str(concurrency),'--payload','4096','--warmup','1','--seconds','3','--timeout-ms','1000']
    write(case/'commands.json',dict(server=server_args,client=client_args))
    server=launch('server',SERVER,server_args,subprocess.PIPE,se);ready=lab.wait_ready(server,8);sf.write(ready+'\n');sf.flush()
    before_ns=time.monotonic_ns();before=groups.cpu();after_ns=time.monotonic_ns();host_before=lab.host_counters(CLIENT|SERVER)
    client=launch('client',CLIENT,client_args,cf,ce);until=time.monotonic()+15;observed=[]
    while client.poll()is None:
     if time.monotonic()>until:raise RuntimeError('client duration bound exceeded')
     if server.poll()is not None:raise RuntimeError('server exited during window')
     snapshot=dict(monotonic_ns=time.monotonic_ns(),connections=tcp_count(port),client_tasks=tasks(client,CLIENT),server_tasks=tasks(server,SERVER));observed.append(snapshot)
     time.sleep(.1)
    end_before=time.monotonic_ns();after=groups.cpu();end_after=time.monotonic_ns();host_after=lab.host_counters(CLIENT|SERVER)
    if client.returncode!=0:raise RuntimeError('native client failed')
    for f in handles:f.flush()
    stdout=(case/'client.stdout').read_text();stderr=(case/'client.stderr').read_text();records=[json.loads(line)for line in stdout.splitlines()if line.startswith('{')]
    if len(records)!=2:raise RuntimeError('native metadata/result count mismatch')
    meta,metric=records;warm_match=re.search(r'WARMUP_DONE successes=(\d+)',stderr)
    if not warm_match or f'SINGLEFLIGHT_MODE {single} TRANSPORT_TIMEOUT_MS -1 EXTERNAL_DEADLINE_MS 1000'not in stderr:raise RuntimeError('warm/model configuration proof missing')
    warm=int(warm_match.group(1));warm_json=re.search(r'WARMUP_COUNTERS (\{[^\n]+\})',stderr)
    if not warm_json:raise RuntimeError('warm counters missing')
    warm_data=json.loads(warm_json.group(1))
    if warm_data['success']!=warm or warm_data['rpc_errors']or warm_data['mismatches']:raise RuntimeError('warm counters invalid')
    success=metric['success'];hist=metric['histogram']
    if metric['errors']or metric['rpc_errors']or metric['mismatches']or metric['attempts']!=success or metric['active_lanes']!=concurrency or sum(v for _,v in hist)!=success:raise RuntimeError('native byte/cohort/histogram quality failure')
    if max(v['connections']for v in observed)!=concurrency or any(v['connections']>concurrency for v in observed):raise RuntimeError('physical connection count differs')
    if not set(meta['owner_tids'])<={t['tid']for v in observed for t in v['client_tasks']}:raise RuntimeError('caller TIDs not covered by actual role snapshots')
    cpu={role:{key:after[role][key]-before[role][key]for key in ['usage_usec','user_usec','system_usec']}for role in before}
    if any(value<0 for role in cpu.values()for value in role.values()):raise RuntimeError('CPU counter reversed')
    denominator=warm+success;normalized=sum(role['usage_usec']for role in cpu.values())/denominator
    result=dict(concurrency=concurrency,index=index,singleflight=bool(single),metric=metric,warm_success=warm,CPU=cpu,CPU_us_per_success=normalized,CPU_denominator=denominator,CPU_begin=before,CPU_end=after,CPU_anchor_ns=[before_ns,after_ns,end_before,end_after],host_begin=host_before,host_end=host_after,metadata=meta,observed=observed,source_hashes=manifest)
    write(case/'result.json',result);results.append(result)
    print(json.dumps(dict(c=concurrency,slot=index,single=single,p50_us=metric['p50_us'],p99_us=metric['p99_us'],qps=metric['rps'],CPU_us_per_success=normalized)),flush=True)
   finally:
    try:
     try:stop(client)
     finally:stop(server)
    finally:
     for f in handles:f.close()
     if server is not None and server.stdout is not None:server.stdout.close()
    if server is not None and server.returncode!=0:raise RuntimeError('server did not stop normally')
    for path in groups.roles.values():
     if (path/'cgroup.procs').read_text().strip():raise RuntimeError('owned role cgroup still has members')
    pins()
 decision={'native_screen_pass':False,'RPC30_accepted':False,'pairs':[],'medians':[]}
 for concurrency in [1,32]:
  window=[v for v in results if v['concurrency']==concurrency]
  for control,candidate in [(window[0],window[1]),(window[3],window[2])]:
   a=control['metric'];b=candidate['metric'];ratio=b['p99_us']/a['p99_us'];conservative=b['p99_us']/(a['p99_us']-1)if 1<a['p99_us']<=1024 and b['p99_us']<=1024 else None;qps=b['rps']/a['rps'];cpu=candidate['CPU_us_per_success']/control['CPU_us_per_success'];ok=qps>=.97 and cpu<=1.03 and(concurrency==1 or conservative is not None and conservative<=.80)
   decision['pairs'].append(dict(c=concurrency,P99_ratio=ratio,conservative_P99_ratio=conservative,QPS_ratio=qps,CPU_ratio=cpu,pass_gate=ok))
  controls=[window[i]for i in [0,3]];candidates=[window[i]for i in [1,2]];med=lambda vs,key:statistics.median(v['metric'][key]for v in vs);a=med(controls,'p99_us');b=med(candidates,'p99_us');q=med(candidates,'rps')/med(controls,'rps');cpu=statistics.median(v['CPU_us_per_success']for v in candidates)/statistics.median(v['CPU_us_per_success']for v in controls);conservative=b/(a-1)if 1<a<=1024 and b<=1024 else None
  decision['medians'].append(dict(c=concurrency,P99_ratio=b/a,conservative_P99_ratio=conservative,QPS_ratio=q,CPU_ratio=cpu,pass_gate=q>=.97 and cpu<=1.03 and(concurrency==1 or conservative is not None and conservative<=.80),control_P99_drift=controls[1]['metric']['p99_us']/controls[0]['metric']['p99_us']-1))
 decision['native_screen_pass']=all(p['pass_gate']for p in decision['pairs']+decision['medians']);write(out/'decision.json',decision);print(json.dumps(decision),flush=True)
except BaseException as e:
 error=repr(e);raise
finally:
 empty={role:not(path/'cgroup.procs').read_text().strip()for role,path in groups.roles.items()}
 write(out/'terminal.json',dict(error=error,completed_slots=len(results),all_role_groups_empty=empty,RPC30_accepted=False))
 if all(empty.values()):
  for path in groups.roles.values():path.rmdir()
