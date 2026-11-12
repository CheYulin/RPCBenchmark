"""Pure scalar decoding and explicit all-arrival/observation contracts."""
import collections
import bisect
import struct
from pathlib import Path
from selection import selected
EVENT=struct.Struct('<5Q3I2iH3B7x')
EVENT_BYTES=EVENT.size
FIELDS=('observed_ns send_ns recv_ns server_processing_ns server_execution_ns tgid tid cpu controller_error ds_error method failed processing_state execution_state').split()


def decode_payload(raw):
    if len(raw)!=EVENT_BYTES or any(raw[65:72]):raise ValueError('unproved event length or nonzero reserved bytes')
    return dict(zip(FIELDS,EVENT.unpack(raw)))


def decode_file(path):
    """Retain malformed rows and file offsets instead of silently dropping them."""
    path=Path(path)
    if path.stat().st_size>128*1024*1024:raise ValueError('raw byte bound exceeded')
    events=[];errors=[]
    with path.open('rb')as stream:
        while raw:=stream.read(EVENT_BYTES):
            index=len(events)
            if index>=250000:raise ValueError('raw record bound exceeded')
            if len(raw)!=EVENT_BYTES:
                errors.append(dict(file_offset=index*EVENT_BYTES,error='truncated_event',bytes=len(raw)));break
            try:row=decode_payload(raw)
            except ValueError as error:
                row=dict(zip(FIELDS,EVENT.unpack(raw)),decode_error=str(error));errors.append(dict(file_offset=index*EVENT_BYTES,error=str(error)))
            events.append(row)
    return dict(events=events,errors=errors,record_count=len(events),eligible=not errors)


def counters_check(counters,decoded,lost):
    names=('allowed_seen','read_failed','submit_succeeded','submit_failed');errors=[]
    if any(type(counters.get(k))is not int or counters[k]<0 for k in names)or type(decoded)is not int or type(lost)is not int or min(decoded,lost)<0:
        return dict(eligible=False,conserved=False,errors=['invalid_counter_shape'],raw=dict(counters),decoded=decoded,lost=lost)
    conserved=counters['allowed_seen']==sum(counters[k]for k in names[1:])
    if not conserved:errors.append('counter_conservation')
    for k in ('read_failed','submit_failed'):
        if counters[k]:errors.append(k)
    if lost:errors.append('lost_callbacks')
    if decoded!=counters['submit_succeeded']:errors.append('decoded_not_submitted')
    return dict(eligible=not errors,conserved=conserved,errors=errors,raw=dict(counters),decoded=decoded,lost=lost,lost_not_added_to_submit_failed=True)


def join_events(events,arrivals,tgid,callers):
    intervals=collections.defaultdict(list);by_key={};errors=[]
    if len(set(callers.values()))!=len(callers):errors.append('caller_mapping_not_unique')
    for a in arrivals:
        key=(a['phase_id'],a['id'])
        if key in by_key:errors.append('duplicate_arrival')
        by_key[key]=a
        if a['phase_id']not in(1,2):errors.append('unexpected_arrival_phase')
        if a['api_start_ns']>=0:
            if a['linux_tid']not in callers:errors.append('API_owner_not_registered')
            if a['api_end_ns']<a['api_start_ns']:errors.append('API_interval_invalid')
            else:intervals[a['linux_tid']].append(a)
    indexes={}
    for tid,values in intervals.items():
        values.sort(key=lambda a:a['api_start_ns']);maximum=-1;prefix=[]
        for a in values:maximum=max(maximum,a['api_end_ns']);prefix.append(maximum)
        indexes[tid]=(values,[a['api_start_ns']for a in values],prefix)
    result=[];seen=set();groups=collections.defaultdict(list)
    for i,event in enumerate(events):
        row=dict(event,source='BPF_OBSERVE_ENTRY_ALL_RPC',event_index=i,arrival_id=None,phase_id=None,caller_index=None,rpc_ordinal=None,issues=[])
        def issue(name):row['issues'].append(name);errors.append(name)
        if row.get('decode_error'):issue('malformed_event')
        if row['tgid']!=tgid:issue('wrong_tgid')
        if row['tid']not in callers:issue('wrong_tid')
        if row['failed']not in(0,1)or row['processing_state']not in(0,1,2)or row['execution_state']not in(0,1,2):issue('invalid_scalar_state')
        if not 0<row['method']<11:issue('unknown_method')
        if not 0<row['send_ns']<=row['recv_ns']:issue('bad_endpoint')
        if not row['send_ns']<=row['recv_ns']<=row['observed_ns']:issue('observation_before_CRecv')
        identity=tuple(row[k]for k in ('tgid','tid','send_ns','recv_ns','method','controller_error','ds_error','failed'))
        if identity in seen:issue('duplicate_RPC_identity')
        seen.add(identity)
        matches=[]
        if row['tid']in indexes and 0<row['send_ns']<=row['recv_ns']:
            values,starts,prefix=indexes[row['tid']];pos=bisect.bisect_right(starts,row['send_ns'])-1
            while pos>=0 and prefix[pos]>=row['recv_ns']:
                if values[pos]['api_end_ns']>=row['recv_ns']:matches.append(values[pos])
                if len(matches)>1:break
                pos-=1
        if len(matches)!=1:issue('ambiguous'if len(matches)>1 else'unmatched')
        elif row['tgid']==tgid and row['tid']in callers:
            a=matches[0];row.update(arrival_id=a['id'],phase_id=a['phase_id'],caller_index=callers[row['tid']])
            if not a['api_start_ns']<=row['observed_ns']<=a['api_end_ns']:issue('observation_outside_API')
            groups[(a['phase_id'],a['id'],callers[row['tid']])].append(row)
        result.append(row)
    for key,group in groups.items():
        if len(group)>16:errors.append('over16_RPC')
        group.sort(key=lambda x:x['observed_ns'])
        if len({x['observed_ns']for x in group})!=len(group):errors.append('ordinal_ambiguous');continue
        for ordinal,row in enumerate(group,1):row['rpc_ordinal']=ordinal
        if any(a['recv_ns']>b['send_ns']for a,b in zip(group,group[1:])):errors.append('RPC_overlap_or_order')
    return dict(events=result,arrivals=arrivals,arrival_count=len(arrivals),state_counts=dict(collections.Counter(a['state']for a in arrivals)),errors=sorted(set(errors)),callers=callers,tgid=tgid)


def normalized_rpc(row):
    """Mirror the pinned core's scalar/diagnostic flags; never alter the raw row."""
    flags=2 if row['failed'] or row['controller_error'] or row['ds_error'] else 0
    if not 0<row['method']<11:flags|=8
    if not row['send_ns'] or not row['recv_ns'] or row['recv_ns']<row['send_ns']:flags|=4
    result={}
    for which,valid,invalid in [('processing',256,512),('execution',1024,2048)]:
        state=row[which+'_state'];value=row['server_'+which+'_ns']
        result['server_'+which+'_ns']=value if state==1 else 0
        if state==1:flags|=valid
        elif state==2 or value:flags|=invalid
        if state not in(0,1,2):flags|=invalid|32
    if flags&256 and flags&1024 and result['server_execution_ns']>result['server_processing_ns']:
        flags=(flags&~1024)|2048;result['server_execution_ns']=0
    if flags&256 and row['recv_ns']>=row['send_ns'] and result['server_processing_ns']>row['recv_ns']-row['send_ns']:flags|=4096
    result['flags']=flags
    return result


def api_outcome(a):
    return a['status_code'] if a['status_code'] else (-1 if a['state'] in ('late_success','mismatch') else 0)


def crosscheck_prefixes(joined,prefixes):
    errors=list(joined['errors']);previous_rpc={};previous_ends={};seen_phases=set();previous_record_order={};previous_end_order={}
    all_apis={(a['phase_id'],a['id']):a for a in joined['arrivals']if a['api_start_ns']>=0}
    for p in prefixes:
        phase=p['through_phase']
        if phase not in(1,2)or p['global_errors']or p['global_error_generation']or not p['normal_snapshot']:errors.append('original_prefix_global_or_partial_error')
        if phase in seen_phases or seen_phases and phase<=max(seen_phases):errors.append('prefix_phase_order')
        if phase==2 and 1 not in seen_phases:errors.append('warm_prefix_missing')
        seen_phases.add(phase)
        events=[e for e in joined['events']if e['phase_id']is not None and e['phase_id']<=phase]
        groups=collections.defaultdict(list)
        for e in events:groups[(e['phase_id'],e['arrival_id'],e['caller_index'])].append(e)
        for a in all_apis.values():
            if a['phase_id']>phase:continue
            required=a.get('required_methods')
            if type(required)is not int or not required or required&~(((1<<11)-1)&~1):
                errors.append('source_bound_required_methods_missing_or_invalid');continue
            caller=joined['callers'].get(a['linux_tid'])
            es=groups.get((a['phase_id'],a['id'],caller),[])
            mask=0
            for e in es:
                if 0<e['method']<11:mask|=1<<e['method']
            if api_outcome(a)==0 and not mask&required:errors.append('successful_API_missing_required_primary')
        slots={s['caller']:s for s in p['slots']}
        if len(slots)!=len(p['slots']):errors.append('duplicate_slot')
        if set(slots)!=set(joined['callers'].values()):errors.append('slot_denominator')
        for tid,caller in joined['callers'].items():
            caller_apis=[a for a in all_apis.values()if a['phase_id']<=phase and a['linux_tid']==tid]
            es=[e for e in events if e['caller_index']==caller]
            s=slots.get(caller)
            if not s or s['cached_tid']!=tid or not s['counters_available']or s['errors']:
                errors.append('slot_owner_or_error');continue
            counts=[sum(e['method']==method for e in es)for method in range(11)]
            failures=sum(bool(e['failed'] or e['controller_error'] or e['ds_error'])for e in es)
            if s['rpc_count']!=len(es)or s['api_count']!=len(caller_apis)or s['method_counts']!=counts or s['rpc_failures']!=failures or s['api_failures']!=sum(api_outcome(a)!=0 for a in caller_apis):errors.append('all_RPC_caller_phase_method_counter_mismatch')
        rpc_keys={};end_keys={}
        for row in p['rpc_records']:
            key=(row['phase_id'],row['arrival_id'],row['caller_index'],row['rpc_ordinal'])
            if key in rpc_keys:errors.append('duplicate_selected_RPC')
            rpc_keys[key]=row
            if row['flags']&(4|8|16|32):errors.append('selected_RPC_primary_error')
            match=[e for e in groups.get(key[:3],[])if e['rpc_ordinal']==key[3]]
            if len(match)!=1:errors.append('selected_RPC_missing_or_duplicate');continue
            e=match[0]
            if any(row[k]!=e[k]for k in ('send_ns','recv_ns','method','controller_error','ds_error')):errors.append('selected_RPC_scalar_mismatch')
            if bool(row['flags']&2)!=bool(e['failed']or e['controller_error']or e['ds_error']):errors.append('selected_RPC_failure_mismatch')
            normalized=normalized_rpc(e)
            expected_flags=normalized['flags']|int(selected(key[0],key[1]))
            if row['flags']!=expected_flags or any(row[k]!=normalized[k]for k in ('server_processing_ns','server_execution_ns')):errors.append('selected_RPC_optional_span_or_flags_mismatch')
        for row in p['api_summaries']:
            key=(row['phase_id'],row['arrival_id'],row['caller_index'])
            if key in end_keys:errors.append('duplicate_selected_End')
            end_keys[key]=row;a=all_apis.get(key[:2]);es=groups.get(key,[])
            if a is None or joined['callers'].get(a['linux_tid'])!=key[2]:errors.append('selected_End_API_owner');continue
            mask=0
            for e in es:
                if 0<=e['method']<64:mask|=1<<e['method']
            diagnostic=0
            for e in es:diagnostic|=normalized_rpc(e)['flags']&(512|2048|4096)
            expected_flags=int(selected(key[0],key[1]))|diagnostic|(2 if any(e['failed']or e['controller_error']or e['ds_error']for e in es)else 0)
            if row['rpc_count']!=len(es)or row['observed_methods']!=mask or row['capture_errors']or row['flags']!=expected_flags:errors.append('selected_End_RPC_count_mask_or_error')
            for captured,original in [('scheduled_ns','scheduled_ns'),('api_begin_ns','api_start_ns'),('api_end_ns','api_end_ns'),('validation_end_ns','validation_end_ns')]:
                if row[captured]!=a[original]:errors.append('selected_End_API_time_mismatch')
            expected_outcome=api_outcome(a)
            if row['outcome']!=expected_outcome:errors.append('selected_End_API_outcome')
        expected_ends={(a['phase_id'],a['id'],joined['callers'][a['linux_tid']])for a in all_apis.values()if a['phase_id']<=phase and a['linux_tid']in joined['callers']and (selected(a['phase_id'],a['id'])or api_outcome(a)!=0 or any(normalized_rpc(e)['flags']&(2|4|8|32|512|2048|4096)for e in groups.get((a['phase_id'],a['id'],joined['callers'][a['linux_tid']]),[])))}
        if set(end_keys)!=expected_ends:errors.append('selected_End_denominator_mismatch')
        expected_rpc={(e['phase_id'],e['arrival_id'],e['caller_index'],e['rpc_ordinal'])for e in events if selected(e['phase_id'],e['arrival_id'])or normalized_rpc(e)['flags']&(2|4|8|32|512|2048|4096)}
        if set(rpc_keys)!=expected_rpc:errors.append('selected_RPC_denominator_mismatch')
        if any(key not in rpc_keys or rpc_keys[key]!=value for key,value in previous_rpc.items()):errors.append('warm_RPC_prefix_changed_or_missing')
        if any(key not in end_keys or end_keys[key]!=value for key,value in previous_ends.items()):errors.append('warm_End_prefix_changed_or_missing')
        for records,previous,name in [(p['rpc_records'],previous_record_order,'RPC'),(p['api_summaries'],previous_end_order,'End')]:
            current={caller:[r for r in records if r['caller_index']==caller]for caller in joined['callers'].values()}
            if any(current.get(caller,[])[:len(rows)]!=rows for caller,rows in previous.items()):errors.append('warm_'+name+'_physical_prefix_changed')
            previous.clear();previous.update(current)
        previous_rpc,previous_ends=rpc_keys,end_keys
    if not prefixes:errors.append('no_original_prefix')
    expected_phases={a['phase_id']for a in joined['arrivals']}|{e['phase_id']for e in joined['events']if e['phase_id']is not None}
    if expected_phases and (not seen_phases or max(seen_phases)<max(expected_phases)):errors.append('final_prefix_missing_for_actual_phase')
    return dict(eligible=not errors,errors=sorted(set(errors)),arrival_count=joined['arrival_count'],observed_events=len(joined['events']),cumulative_prefixes_not_added=True)


def coverage_check(ready_ns,plans,stop_ns,closed,partial,arrivals):
    errors=[]
    if not plans or ready_ns is None or stop_ns is None:errors.append('coverage_anchor_missing')
    elif ready_ns>=min(p['epoch_ns']for p in plans)or stop_ns<max([p['end_ns']for p in plans]+[a['api_end_ns']for a in arrivals]):errors.append('coverage_bounds')
    if not closed:errors.append('SDK_or_capture_not_safely_closed')
    if partial:errors.append('partial_target_exit')
    return dict(eligible=not errors,errors=errors)
