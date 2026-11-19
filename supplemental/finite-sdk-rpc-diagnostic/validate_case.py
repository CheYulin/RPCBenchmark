from pathlib import Path
import json,math
from analyze_closed import analyze_rows,expected_selected_keys,decode_pod_rows,merge_prefix_rows

def need(ok,message):
 if not ok:raise ValueError(message)
def load(path):return json.loads(Path(path).read_text())
def quantile(bins,p):
 total=sum(n for _,n in bins);rank=(total*p+99)//100;seen=0
 for bound,n in bins:
  seen+=n
  if seen>=rank:return bound
 return 0

def validate_metric(m,case,smoke,mode,op):
 root=Path(case)/'driver';need(load(root/'metric.json')==m,'RESULT/file metric mismatch');ready=load(root/'ready.json');expected_counts=(8,16)if smoke else(8192,49152)
 need(all(m[k]is True for k in ['quality_valid','sdk_shutdown_ok','capture_close_ok']),'driver lifecycle/quality')
 need(not m['RPC30_accepted'] and not m['fixed_offered_load'] and not m['arrival_E2E_available'],'measurement scope')
 need(m['operation']==op and m['capture_mode']==mode and m['callers']==32 and m['payload_bytes']==4096,'workload fields')
 need((m['warm_calls_per_caller'],m['measure_calls_per_caller'])==expected_counts,'finite shape')
 need(m['readback_keys']==64 and m['readback_errors']==0,'final key readback')
 need(m['sdk_pid']==ready['pid'] and m['worker_pid']==ready['worker_pid'],'ready owner binding')
 need(len(set(ready['tids']))==32 and len(ready['tids'])==32,'caller TIDs')
 need(len(m['phases'])==2,'duplicate/extra phases');phases={v['phase_id']:v for v in m['phases']};need(set(phases)=={1,2},'phase IDs')
 summary={'phases':{}}
 for phase,count in enumerate(expected_counts,1):
  p=phases[phase];n=count*32
  need(p['planned']==p['attempts']==p['success']==n and p['errors']==p['mismatches']==p['error_ledger_rows']==0,'full API success/count')
  for name in ['api','observer']:
   bins=p[name+'_histogram_upper_us'];need(bins and all(type(a)is int and type(b)is int and a>0 and b>0 for a,b in bins),'histogram types')
   need(all(a<=8192 or (a>=16384 and a<=2**55 and a&(a-1)==0)for a,_ in bins),'histogram bucket domain');need([a for a,_ in bins]==sorted(set(a for a,_ in bins)) and sum(b for _,b in bins)==n,'histogram conservation')
   need(p[name+'_p50_upper_us']==quantile(bins,50) and p[name+'_p99_upper_us']==quantile(bins,99),'quantile rank')
  need(p['first_api_begin_ns']<=p['first_caller_finish_ns']<=p['last_caller_finish_ns'],'taper order')
  need(p['taper_ns']==p['last_caller_finish_ns']-p['first_caller_finish_ns'],'taper conservation')
  need(abs(p['elapsed_s']-(p['last_caller_finish_ns']-p['first_api_begin_ns'])/1e9)<1e-9,'duration definition')
  need(math.isclose(p['qps'],n/p['elapsed_s'],rel_tol=1e-10),'QPS denominator')
  a=p['cpu_before'];b=p['cpu_after'];need(a['read_begin_ns']<=a['read_end_ns']<=b['read_begin_ns']<=b['read_end_ns'],'CPU read order');need(a['read_end_ns']<=p['first_api_begin_ns']<=p['last_caller_finish_ns']<=b['read_begin_ns'],'CPU cohort coverage')
  need(min(p['sdk_cpu_s'],p['worker_cpu_s'])>=0 and math.isclose(p['CPU_us_per_success'],(p['sdk_cpu_s']+p['worker_cpu_s'])*1e6/n,rel_tol=1e-10),'CPU denominator')
  for role in ['sdk','worker']:
   need(b[role+'_ticks']>=a[role+'_ticks'] and math.isclose(p[role+'_cpu_s'],(b[role+'_ticks']-a[role+'_ticks'])/m['clock_ticks_per_second'],rel_tol=1e-10),'CPU ticks')
  need((p['returned_bytes'],p['validated_bytes'])==((n*4096,n*4096)if op=='get'else(0,0)),'Get bytes or Set status-only accounting')
  summary['phases'][phase]={'attempts':n,'success':n,'errors':0,'histogram_count':sum(b for _,b in p['api_histogram_upper_us'])}
 need(phases[1]['cpu_after']['read_end_ns']<=phases[2]['cpu_before']['read_begin_ns'],'phase CPU order');need(len(m['caller_phases'])==64,'caller phase count');seen=set()
 for r in m['caller_phases']:
  key=(r['phase_id'],r['caller']);need(key not in seen and 0<=r['caller']<32 and r['phase_id']in(1,2),'caller phase set');seen.add(key)
  need(r['api_begin_ns']<=r['observer_end_ns'],'caller time order');need(r['tid']==ready['tids'][r['caller']] and r['attempts']==expected_counts[r['phase_id']-1],'caller count/TID')
 for phase in [1,2]:
  records=[r for r in m['caller_phases']if r['phase_id']==phase];p=phases[phase]
  need(min(r['api_begin_ns']for r in records)==p['first_api_begin_ns'] and min(r['observer_end_ns']for r in records)==p['first_caller_finish_ns'] and max(r['observer_end_ns']for r in records)==p['last_caller_finish_ns'],'caller/aggregate temporal boundaries')
 plan=dict(callers=32,warm_per_caller=expected_counts[0],measure_per_caller=expected_counts[1],seed=17,modulus=512,operation=op)
 expected=expected_selected_keys(plan);ledgers=[json.loads(s)for s in(root/'sampled-api-ledger.jsonl').read_text().splitlines()]
 keys=[]
 for r in ledgers:
  key=(r['phase_id'],r['arrival_id'],r['caller']);keys.append(key)
  need(key in expected and r['selected'] and r['status']==0 and not r['mismatch'],'ledger selection/error')
  need(r['caller']==r['arrival_id']%32 and r['tid']==ready['tids'][r['caller']],'ledger caller identity')
  need(r['scheduled_ns']==r['api_begin_ns']<=r['api_end_ns']<=r['validation_end_ns']<=r['observer_end_ns'],'ledger times')
  r['caller_index']=r['caller'];r['linux_tid']=r['tid'];r['status_code']=r['status']
 need(len(keys)==len(set(keys)) and set(keys)==expected,'complete preselected ledger population')
 caller_phases={(r['phase_id'],r['caller']):r for r in m['caller_phases']}
 for row in ledgers:
  bounds=caller_phases[row['phase_id'],row['caller']]
  need(bounds['api_begin_ns']<=row['api_begin_ns']<=row['observer_end_ns']<=bounds['observer_end_ns'],'sample outside caller phase')
 for phase in [1,2]:need(phases[phase]['selected_ledger_rows']==sum(k[0]==phase for k in expected),'selected ledger aggregate count')
 for phase in [1,2]:
  for name,end_name in [('api','api_end_ns'),('observer','observer_end_ns')]:
   sampled={}
   for row in ledgers:
    if row['phase_id']!=phase:continue
    us=max(1,(row[end_name]-row['api_begin_ns']+999)//1000)
    bound=us if us<=8192 else 1<<(us-1).bit_length()
    sampled[bound]=sampled.get(bound,0)+1
   full=dict(phases[phase][name+'_histogram_upper_us'])
   need(all(full.get(bound,0)>=count for bound,count in sampled.items()),'sample/full histogram lower-bound mismatch')
 if mode=='off':
  need(not m['rpc_capture_available'] and not(root/'capture').exists(),'off must not open capture')
  result=dict(diagnostic_valid=True,RPC30_accepted=False,RPC_available=False,independent_ledger_rows=len(ledgers),limits=['Finite closed loop with taper; API diagnostics only; external owner/maps/routes verified separately'])
 else:
  need(m['rpc_capture_available'],'enabled missing RPC data');cap=root/'capture';final=load(cap/'final.json')
  need(final['mode']==mode,'capture final mode');need(all(final[k]is True for k in ['resource_sealed','core_valid_capture','capture_or_control_ok','close_ok'])and final['control_status']==0,'capture seal/close')
  rows={};prefix={}
  for label in ['warmup','measure']:
   meta=load(cap/(label+'-prefix.json'));prefix[label]=meta
   need(meta['abi']==2 and meta['sample_bytes']==64 and meta['end_bytes']==80 and meta['snapshot_mode']==1 and meta['state']==2 and meta['global_errors']==meta['global_error_generation']==meta['control_status']==0,'prefix ABI/error')
   need(len(meta['slots'])==32 and {v['caller']for v in meta['slots']}==set(range(32)),'prefix callers')
   rows[label]={'rpc':[],'end':[]}
   for slot in meta['slots']:
    caller=slot['caller'];need(slot['cached_tid']==ready['tids'][caller],'capture slot TID')
    need(slot['registered'] and not slot['sealed'] and slot['active']==0 and slot['counters_available'],'capture slot lifecycle')
    need(all(slot[k]==0 for k in ['errors','error_generation','first_driver_status','api_failures','rpc_failures']),'capture slot error')
    count=expected_counts[0]+(expected_counts[1]if label=='measure'else 0)
    need(slot['api_count']==count and count<=slot['rpc_count']<=count*16 and len(slot['method_counts'])==11 and slot['method_counts'][0]==0 and sum(slot['method_counts'])==slot['rpc_count'],'capture all method/count conservation')
    for kind,suffix,count_name in [('rpc','rpc.bin','record_count'),('end','ends.bin','end_count')]:
     values=decode_pod_rows((cap/f'{label}-caller-{caller}.{suffix}').read_bytes(),kind,meta['byte_order'])
     need(len(values)==slot[count_name] and all(v['caller_index']==caller and (v['phase_id']==1 if label=='warmup'else v['phase_id']in(1,2))for v in values),'prefix byte/count/owner/phase')
     rows[label][kind].extend(values)
  need(prefix['warmup']['generation']>0 and prefix['warmup']['generation']==prefix['measure']['generation'],'capture generation')
  ends=merge_prefix_rows(rows['warmup']['end'],rows['measure']['end'],'end');rpcs=merge_prefix_rows(rows['warmup']['rpc'],rows['measure']['rpc'],'rpc')
  summary.update(global_errors=prefix['measure']['global_errors'],control_status=prefix['measure']['control_status'])
  result=analyze_rows(plan,ledgers,ends,rpcs,prefix['measure']['slots'],summary)
 (Path(case)/'diagnosis.json').write_text(json.dumps(result,indent=2)+'\n')
 need(result['diagnostic_valid'],'independent capture join failed; inspect diagnosis.json')
 return result
