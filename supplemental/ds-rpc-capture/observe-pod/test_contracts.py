"""Synthetic LOCAL contracts only; never SDK/BPF runtime or production RED."""
import copy
import struct
import tempfile
from pathlib import Path
import unittest
from contracts import decode_payload,decode_file,counters_check,join_events,crosscheck_prefixes,coverage_check,normalized_rpc
from selection import selected
FMT=struct.Struct('<5Q3I2iH3B7x')
ID=next(i for i in range(1000)if selected(1,i))
def api(id=ID,tid=11,phase=1,start=100,end=200,state='timely_success'):
 return dict(phase_id=phase,id=id,linux_tid=tid,scheduled_ns=90,api_start_ns=start,api_end_ns=end,validation_end_ns=end+2,state=state,status_code=0,required_methods=1<<1)
def event(tid=11,send=110,recv=120,observed=125,method=1):
 return dict(observed_ns=observed,send_ns=send,recv_ns=recv,server_processing_ns=3,server_execution_ns=2,tgid=101,tid=tid,cpu=3,controller_error=0,ds_error=0,method=method,failed=0,processing_state=1,execution_state=1)
def payload(e):return FMT.pack(*[e[k]for k in ('observed_ns','send_ns','recv_ns','server_processing_ns','server_execution_ns','tgid','tid','cpu','controller_error','ds_error','method','failed','processing_state','execution_state')])
def joined(ev=None,arr=None):return join_events(ev or[event()],arr or[api()],101,{11:0,12:1})
def prefix(ev=None,arr=None,phase=1):
 ev=ev or[event()];a=(arr or[api()])[0];mask=0
 for e in ev:mask|=1<<e['method']
 counts=[0]*11
 for e in ev:counts[e['method']]+=1
 records=[dict(phase_id=a['phase_id'],arrival_id=a['id'],caller_index=0,rpc_ordinal=i+1,send_ns=e['send_ns'],recv_ns=e['recv_ns'],method=e['method'],controller_error=e['controller_error'],ds_error=e['ds_error'],server_processing_ns=e['server_processing_ns'],server_execution_ns=e['server_execution_ns'],flags=1|256|1024)for i,e in enumerate(ev)]
 end=dict(phase_id=a['phase_id'],arrival_id=a['id'],caller_index=0,scheduled_ns=a['scheduled_ns'],api_begin_ns=a['api_start_ns'],api_end_ns=a['api_end_ns'],validation_end_ns=a['validation_end_ns'],observed_methods=mask,rpc_count=len(ev),capture_errors=0,outcome=0,flags=1)
 slots=[dict(caller=0,cached_tid=11,rpc_count=len(ev),rpc_failures=0,api_failures=0,api_count=1,method_counts=counts,counters_available=True,errors=0),dict(caller=1,cached_tid=12,rpc_count=0,rpc_failures=0,api_failures=0,api_count=0,method_counts=[0]*11,counters_available=True,errors=0)]
 return dict(through_phase=phase,global_errors=0,global_error_generation=0,normal_snapshot=True,slots=slots,rpc_records=records,api_summaries=[end])

class Contracts(unittest.TestCase):
 def test_wire_fields_padding_and_no_pointer(self):
  self.assertEqual(decode_payload(payload(event())),event())
  raw=bytearray(payload(event()));raw[65]=0xa5
  with self.assertRaises(ValueError):decode_payload(raw)
  with self.assertRaises(ValueError):decode_payload(payload(event())[:-1])
 def test_file_decode_keeps_bad_row_and_truncated_suffix(self):
  with tempfile.TemporaryDirectory()as directory:
   path=Path(directory)/'events.bin';bad=bytearray(payload(event()));bad[65]=1
   path.write_bytes(payload(event())+bad+b'X')
   r=decode_file(path);self.assertEqual(r['record_count'],2);self.assertEqual([e['file_offset']for e in r['errors']],[72,144])
   self.assertIn('decode_error',r['events'][1]);self.assertFalse(r['eligible'])
   self.assertIn('malformed_event',join_events(r['events'],[api()],101,{11:0})['errors'])
 def test_counter_conservation_and_loss_overlap(self):
  ok=dict(allowed_seen=2,read_failed=0,submit_succeeded=2,submit_failed=0)
  self.assertTrue(counters_check(ok,2,0)['eligible'])
  for c,n,l in [(dict(ok,read_failed=1,submit_succeeded=1),1,0),(dict(ok,submit_failed=1,submit_succeeded=1),1,1),(ok,1,0),(ok,2,1)]:self.assertFalse(counters_check(c,n,l)['eligible'])
 def test_unique_join_and_all_arrival_denominator(self):
  rejected=dict(api(id=999),state='queue_rejected',linux_tid=-1,api_start_ns=-1,api_end_ns=-1)
  r=joined(arr=[api(),rejected]);self.assertEqual(r['arrival_count'],2);self.assertEqual(r['state_counts']['queue_rejected'],1);self.assertEqual(r['events'][0]['arrival_id'],ID)
 def test_ambiguous_intervals_never_first_match(self):
  r=joined(arr=[api(),api(id=ID+1)]);self.assertIn('ambiguous',r['errors']);self.assertIsNone(r['events'][0]['arrival_id'])
 def test_observed_after_API_before_validation_is_invalid(self):
  e=event(observed=201);r=joined([e]);self.assertIn('observation_outside_API',r['errors'])
 def test_wrong_tgid_tid_unmatched_and_bad_endpoints_retained(self):
  cases=[dict(event(),tgid=999),event(tid=99),event(send=80),event(send=130,recv=120)]
  for e in cases:
   r=joined([e]);self.assertEqual(len(r['events']),1);self.assertTrue(r['errors'])
 def test_duplicate_not_silently_deduplicated(self):
  r=joined([event(),dict(event(),observed_ns=126)]);self.assertEqual(len(r['events']),2);self.assertIn('duplicate_RPC_identity',r['errors'])
 def test_out_of_order_delivery_sixteen_RPC_and_overflow(self):
  ev=[event(send=110+3*i,recv=111+3*i,observed=112+3*i)for i in range(16)]
  r=joined(list(reversed(ev)));self.assertFalse(r['errors']);self.assertEqual(sorted(e['rpc_ordinal']for e in r['events']),list(range(1,17)))
  r=joined(ev+[event(send=170,recv=171,observed=172)]);self.assertIn('over16_RPC',r['errors'])
 def test_tied_observation_cannot_forge_ordinal(self):
  r=joined([event(),event(send=121,recv=122,observed=125)]);self.assertIn('ordinal_ambiguous',r['errors'])
 def test_tail_drain_keeps_arrival_phase(self):
  r=joined([event(send=210,recv=220,observed=225)],[api(start=200,end=250)]);self.assertFalse(r['errors']);self.assertEqual(r['events'][0]['phase_id'],1)
 def test_prefix_exact_counts_selected_End_and_RPC(self):
  self.assertFalse(crosscheck_prefixes(joined(),[prefix()])['errors'])
  for path in ('rpc_count','method_counts','rpc_records','selected_end'):
   p=prefix()
   if path=='rpc_count':p['slots'][0]['rpc_count']+=1
   if path=='method_counts':p['slots'][0]['method_counts'][1]=0;p['slots'][0]['method_counts'][4]=1
   if path=='rpc_records':p['rpc_records'][0]['recv_ns']+=1
   if path=='selected_end':p['api_summaries'][0]['observed_methods']|=16
   self.assertTrue(crosscheck_prefixes(joined(),[p])['errors'],path)
 def test_missing_selected_and_malformed_status_are_errors(self):
  p=prefix();p['rpc_records']=[];self.assertTrue(crosscheck_prefixes(joined(),[p])['errors'])
  p=prefix();p['api_summaries']=[];self.assertTrue(crosscheck_prefixes(joined(),[p])['errors'])
  p=prefix();p['rpc_records'][0]['controller_error']=5;self.assertTrue(crosscheck_prefixes(joined(),[p])['errors'])
 def test_cumulative_prefix_is_not_added_and_warm_is_immutable(self):
  p=prefix();final=copy.deepcopy(p);final['through_phase']=2
  self.assertFalse(crosscheck_prefixes(joined(),[p,final])['errors'])
  final['rpc_records'][0]['send_ns']+=1;self.assertTrue(crosscheck_prefixes(joined(),[p,final])['errors'])
 def test_optional_invalid_keeps_primary_record(self):
  e=dict(event(),processing_state=2,server_processing_ns=5);r=joined([e]);self.assertEqual(len(r['events']),1)
 def test_global_partial_or_unregistered_API_cannot_pass(self):
  for field,value in [('global_errors',1),('global_error_generation',1),('normal_snapshot',False)]:
   p=prefix();p[field]=value;self.assertFalse(crosscheck_prefixes(joined(),[p])['eligible'])
  r=joined(arr=[api(tid=99)]);self.assertIn('API_owner_not_registered',r['errors'])
 def test_final_prefix_must_cover_actual_measure_arrivals_and_events(self):
  a=api(id=0,phase=2,start=200,end=300);e=event(send=210,recv=220,observed=225)
  r=joined([event(),e],[api(),a])
  self.assertFalse(crosscheck_prefixes(r,[prefix()])['eligible'])
 def test_same_global_total_cannot_hide_caller_phase_or_method_swap(self):
  for kind in ('caller','phase','method','rpc_failures','api_failures'):
   p=prefix();r=joined()
   if kind=='caller':p['slots'][0]['rpc_count']=0;p['slots'][1]['rpc_count']=1
   if kind=='phase':p['rpc_records'][0]['phase_id']=2
   if kind=='method':p['slots'][0]['method_counts'][1]=0;p['slots'][0]['method_counts'][4]=1
   if kind in ('rpc_failures','api_failures'):p['slots'][0][kind]=1
   self.assertFalse(crosscheck_prefixes(r,[p])['eligible'],kind)
 def test_optional_flags_normalize_exactly_without_discarding_primary(self):
  e=dict(event(),processing_state=1,server_processing_ns=30,execution_state=1,server_execution_ns=31)
  n=normalized_rpc(e);self.assertEqual(n['server_processing_ns'],30);self.assertEqual(n['server_execution_ns'],0)
  self.assertEqual(n['flags'],256|2048|4096)
  p=prefix([e]);p['rpc_records'][0].update(n,flags=n['flags']|1);p['api_summaries'][0]['flags']=1|2048|4096
  self.assertTrue(crosscheck_prefixes(joined([e]),[p])['eligible'])
  p['rpc_records'][0]['flags']^=4096;self.assertFalse(crosscheck_prefixes(joined([e]),[p])['eligible'])
 def test_unselected_diagnostic_records_and_Ends_are_not_trimmed(self):
  id=next(i for i in range(1000)if not selected(1,i));a=api(id=id)
  e=dict(event(),processing_state=2,server_processing_ns=100)
  n=normalized_rpc(e);p=prefix([e],[a]);p['rpc_records'][0].update(n);p['api_summaries'][0]['flags']=512
  self.assertTrue(crosscheck_prefixes(joined([e],[a]),[p])['eligible'])
  p['rpc_records']=[];self.assertFalse(crosscheck_prefixes(joined([e],[a]),[p])['eligible'])
 def test_warm_physical_prefix_cannot_be_reordered(self):
  ev=[event(),event(send=130,recv=140,observed=145)];p=prefix(ev);final=copy.deepcopy(p);final['through_phase']=2
  final['rpc_records'].reverse()
  self.assertIn('warm_RPC_physical_prefix_changed',crosscheck_prefixes(joined(ev),[p,final])['errors'])
 def test_snapshot_must_cover_tail_drain_APIend(self):
  plan=[dict(epoch_ns=100,end_ns=200)];a=api(start=200,end=260)
  self.assertFalse(coverage_check(90,plan,210,True,False,[a])['eligible'])
  self.assertTrue(coverage_check(90,plan,270,True,False,[a])['eligible'])
 def _distribution_case(self,nonzero_wrong_method):
  a=api(id=0);b=api(id=1,start=300,end=400)
  self.assertFalse(selected(1,0));self.assertFalse(selected(1,1))
  mid=next(i for i in range(1000)if selected(2,i));c=api(id=mid,phase=2,start=700,end=800)
  c_event=event(send=710,recv=720,observed=725)
  original=[event(),event(send=310,recv=320,observed=325),c_event]
  observed=[event(),event(send=130,recv=140,observed=145),c_event]
  warm=prefix();warm['rpc_records']=[];warm['api_summaries']=[];warm['slots'][0].update(api_count=2,rpc_count=2,method_counts=[0,2]+[0]*9)
  if nonzero_wrong_method:
   original.insert(1,event(send=130,recv=140,observed=145,method=2))
   observed.insert(2,event(send=310,recv=320,observed=325,method=2))
   warm['slots'][0]['rpc_count']=3;warm['slots'][0]['method_counts'][2]=1
  final=copy.deepcopy(warm);final['through_phase']=2
  final['slots'][0]['api_count']+=1;final['slots'][0]['rpc_count']+=1;final['slots'][0]['method_counts'][1]+=1
  selected_final=prefix([c_event],[c]);final['rpc_records']=selected_final['rpc_records'];final['api_summaries']=selected_final['api_summaries']
  self.assertTrue(crosscheck_prefixes(joined(original,[a,b,c]),[warm,final])['eligible'])
  self.assertFalse(crosscheck_prefixes(joined(observed,[a,b,c]),[warm,final])['eligible'])
 def test_unsampled_zero_and_two_RPC_cannot_cancel(self):
  self._distribution_case(False)
 def test_unsampled_nonzero_wrong_primary_cannot_cancel(self):
  self._distribution_case(True)
 def test_required_methods_missing_invalid_and_sixteen_RPC_positive(self):
  for value in (None,0,1,1<<12):
   a=api();a['required_methods']=value
   self.assertFalse(crosscheck_prefixes(joined(arr=[a]),[prefix(arr=[a])])['eligible'])
  ev=[dict(event(send=110+4*i,recv=112+4*i,observed=113+4*i),server_processing_ns=1,server_execution_ns=1)for i in range(16)]
  self.assertTrue(crosscheck_prefixes(joined(ev),[prefix(ev)])['eligible'])
 def test_partial_exit_and_late_ready_cannot_claim_coverage(self):
  plan=[dict(epoch_ns=100,end_ns=200)]
  self.assertTrue(coverage_check(90,plan,210,True,False,[])['eligible'])
  for ready,stop,closed,partial in [(100,210,True,False),(90,199,True,False),(90,210,False,True)]:self.assertFalse(coverage_check(ready,plan,stop,closed,partial,[])['eligible'])
if __name__=='__main__':unittest.main()
