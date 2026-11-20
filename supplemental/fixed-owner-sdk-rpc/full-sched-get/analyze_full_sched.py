"""Replay owned-task observations without kernel, process or workload actions."""
import argparse
from bisect import bisect_right
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import struct

RECORD = struct.Struct('<QQIIHHI')
WAKE, WAKE_NEW, SWITCH_IN, SWITCH_OUT, GET_ENTRY, GET_RETURN = range(1, 7)
STATES = ('unknown', 'nonrunnable', 'runnable', 'scheduled')


def integer(value, minimum=0):
    return type(value) is int and value >= minimum


def analyze(metadata, event_bytes, ledger_rows):
    issues, duplicates, migrations, reordered = [], [], [], []
    def problem(code, **details):
        issues.append(dict(code=code, **details))
    required = dict(schema='owner8-full-sched-snapshot-v1', record_bytes=32,
                    byte_order='little', layout='owner-major-stored-prefixes', owner_capacity=65536)
    if type(metadata) is not dict or any(type(metadata.get(k)) is not type(v) or metadata.get(k) != v for k,v in required.items()):
        raise ValueError('unknown event metadata/layout')
    for name, length in [('owner_tids',8), ('owner_attempted_counts',8), ('owner_stored_counts',8),
                         ('get_next',8), ('get_pending',8), ('bad',4)]:
        values = metadata.get(name)
        if type(values) is not list or len(values) != length or not all(integer(v) for v in values):
            raise ValueError('invalid metadata array: '+name)
    tids = metadata['owner_tids']
    if len(set(tids)) != 8 or min(tids) <= 0:
        raise ValueError('owner TID cohort')
    cookies = metadata.get('owner_cookies')
    cookie_keys = {'slot','tid','tgid','task_start_boottime_ns','leader_start_boottime_ns'}
    if (type(cookies) is not list or len(cookies) != 8 or any(type(v) is not dict or v.keys() != cookie_keys for v in cookies)):
        raise ValueError('unknown cookie metadata')
    for owner,cookie in enumerate(cookies):
        if (not integer(cookie['slot']) or cookie['slot'] != owner or cookie['tid'] != tids[owner]
                or not all(integer(cookie[k],1) for k in cookie_keys-{'slot'})):
            raise ValueError('cookie identity metadata')
    if len({c['tgid'] for c in cookies}) != 1 or len({c['leader_start_boottime_ns'] for c in cookies}) != 1:
        raise ValueError('leader cookie cohort')
    stamp_names = ['enabled_begin_ns','enabled_end_ns','data_deadline_ns','disabled_begin_ns',
                   'disabled_end_ns','seal_begin_ns','seal_end_ns']
    if not all(integer(metadata.get(k),1) for k in stamp_names):
        raise ValueError('unknown clock/ROI boundaries')
    if not (metadata['enabled_begin_ns'] <= metadata['enabled_end_ns'] < metadata['data_deadline_ns']
            and metadata['disabled_begin_ns'] <= metadata['seal_begin_ns'] <= metadata['seal_end_ns'] <= metadata['disabled_end_ns']):
        raise ValueError('invalid enable/seal clock order')
    start = metadata['enabled_end_ns']; end = min(metadata['data_deadline_ns'],metadata['disabled_begin_ns'])
    if end <= start:
        raise ValueError('empty guaranteed observation interval')
    if metadata.get('gate_closed_drained') is not True:
        problem('gate_not_closed_drained')
    if type(metadata.get('detach_errors')) is not list:
        raise ValueError('unknown detach proof')
    if metadata['detach_errors']:
        problem('detach_errors', values=metadata['detach_errors'])
    aligned = metadata.get('clock_alignment_verified') is True
    if not aligned:
        problem('clock_alignment_unverified')
    if any(metadata['bad']):
        problem('kernel_bad_counters', values=metadata['bad'])
    owner_loss = [metadata.get('gate_closed_drained') is not True]*8
    for owner,(attempted,stored) in enumerate(zip(metadata['owner_attempted_counts'], metadata['owner_stored_counts'])):
        if stored > 65536:
            raise ValueError('stored prefix exceeds capacity')
        if attempted > 65536:
            problem('record_overflow', owner=owner, attempted=attempted); owner_loss[owner]=True
        if stored != min(attempted,65536):
            problem('stored_attempted_conservation',owner=owner,attempted=attempted,stored=stored); owner_loss[owner]=True
    if any(metadata['bad'][:2]):
        owner_loss = [True]*8
    if metadata['bad'][3] and not any(owner_loss):
        owner_loss = [True]*8
    expected_bytes = sum(metadata['owner_stored_counts'])*32
    if len(event_bytes) != expected_bytes:
        problem('binary_prefix_length',expected=expected_bytes,actual=len(event_bytes)); owner_loss=[True]*8
    events=[[] for _ in range(8)]; offset=0
    for owner,count in enumerate(metadata['owner_stored_counts']):
        for index in range(count):
            chunk=event_bytes[offset:offset+32]; offset+=32
            if len(chunk) != 32:
                problem('truncated_record',owner=owner,index=index); continue
            stamp,commit,slot,cpu,kind,flags,value=RECORD.unpack(chunk)
            record=dict(event_ns=stamp,commit=commit,owner=slot,cpu=cpu,kind=kind,flags=flags,value=value,index=index)
            if commit != index+1 or slot != owner:
                problem('missing_or_torn_commit',owner=owner,index=index,commit=commit,recorded_owner=slot)
                owner_loss[owner]=True; continue
            events[owner].append(record)
            allowed_flags=1 if kind==SWITCH_OUT else 2 if kind in (GET_ENTRY,GET_RETURN) else 0
            if kind not in range(1,7) or flags & ~allowed_flags or cpu>=256 or (kind in (WAKE,WAKE_NEW,SWITCH_IN) and value):
                problem('invalid_event_fields',owner=owner,index=index); owner_loss[owner]=True
            if not metadata['enabled_begin_ns'] <= stamp < min(metadata['data_deadline_ns'],metadata['disabled_end_ns']):
                problem('event_outside_cutoff',owner=owner,index=index,event_ns=stamp); owner_loss[owner]=True
        if any(a['event_ns']>b['event_ns'] for a,b in zip(events[owner],events[owner][1:])):
            reordered.append(owner)
        events[owner].sort(key=lambda v:(v['event_ns'],v['index']))
    intervals=[]
    for owner,population in enumerate(events):
        segments=[]; state='unknown'; cursor=start; last_cpu=None; current_cpu=None
        groups=defaultdict(list)
        for event in population:
            if event['kind'] in (WAKE,WAKE_NEW,SWITCH_IN,SWITCH_OUT) and start <= event['event_ns'] < end:
                groups[event['event_ns']].append(event)
        def append(a,b,label,cpu=None):
            if a>=b: return
            if segments and segments[-1]['state']==label and segments[-1]['cpu']==cpu and segments[-1]['end_ns']==a:
                segments[-1]['end_ns']=b
            else:
                segments.append(dict(start_ns=a,end_ns=b,state=label,cpu=cpu))
        for stamp,group in sorted(groups.items()):
            append(cursor,stamp,state,current_cpu);cursor=stamp
            if len(group)>1:
                problem('tied_scheduler_events',owner=owner,event_ns=stamp,commits=[e['commit']for e in group])
                state='unknown';current_cpu=None;continue
            event=group[0];kind=event['kind']
            if kind in (WAKE,WAKE_NEW):
                if state=='runnable':
                    duplicates.append(dict(owner=owner,event_ns=stamp,commit=event['commit'],waker_cpu=event['cpu']))
                elif state=='scheduled':
                    problem('wake_while_scheduled',owner=owner,event_ns=stamp)
                    if segments:segments[-1]['state']='unknown';segments[-1]['cpu']=None
                    state='unknown';current_cpu=None
                else:
                    state='runnable';current_cpu=None
            elif kind==SWITCH_IN:
                if state=='scheduled':
                    problem('double_switch_in',owner=owner,event_ns=stamp)
                    if segments:segments[-1]['state']='unknown';segments[-1]['cpu']=None
                    state='unknown';current_cpu=None;continue
                if state=='nonrunnable':
                    problem('missing_wake_before_switch_in',owner=owner,event_ns=stamp)
                    if segments:segments[-1]['state']='unknown';segments[-1]['cpu']=None
                if last_cpu is not None and last_cpu != event['cpu']:
                    migrations.append(dict(owner=owner,event_ns=stamp,from_cpu=last_cpu,to_cpu=event['cpu']))
                current_cpu=last_cpu=event['cpu'];state='scheduled'
            else:
                if state not in ('scheduled','unknown'):
                    problem('switch_out_without_switch_in',owner=owner,event_ns=stamp)
                    if segments:segments[-1]['state']='unknown';segments[-1]['cpu']=None
                if state=='scheduled' and current_cpu != event['cpu']:
                    problem('switch_cpu_mismatch',owner=owner,event_ns=stamp)
                    if segments:segments[-1]['state']='unknown';segments[-1]['cpu']=None
                last_cpu=event['cpu'];current_cpu=None
                state='runnable' if event['flags']&1 or event['value']==0 else 'nonrunnable'
        append(cursor,end,state,current_cpu)
        if owner_loss[owner]:
            segments=[dict(start_ns=start,end_ns=end,state='unknown',cpu=None)]
        intervals.append(segments)
    # A target-thread uprobe cannot execute inside a known off-CPU interval.
    for owner,population in enumerate(events):
        original=[dict(segment)for segment in intervals[owner]]
        starts=[segment['start_ns']for segment in original]
        for event in population:
            if event['kind']not in(GET_ENTRY,GET_RETURN)or not start<=event['event_ns']<end:
                continue
            index=bisect_right(starts,event['event_ns'])-1
            segment=original[index]
            if segment['state']in('nonrunnable','runnable')or(segment['state']=='scheduled'and segment['cpu']!=event['cpu']):
                problem('Get_anchor_state_conflict',owner=owner,event_ns=event['event_ns'],commit=event['commit'],
                        get_cpu=event['cpu'],claimed_state=segment['state'],claimed_cpu=segment['cpu'],
                        interval_start_ns=segment['start_ns'],interval_end_ns=segment['end_ns'])
                intervals[owner][index]['state']='unknown';intervals[owner][index]['cpu']=None
    # Exact pair IDs are checked before any ledger containment; timestamps never guess an ID.
    pairs=[]; tied_get=[set()for _ in range(8)]
    for owner,population in enumerate(events):
        groups=defaultdict(list)
        for event in population:groups[event['event_ns']].append(event)
        for stamp,group in groups.items():
            if len(group)>1 and any(e['kind']in(GET_ENTRY,GET_RETURN)for e in group):
                problem('tied_Get_event_time',owner=owner,event_ns=stamp)
                tied_get[owner].update(e['value']for e in group if e['kind']in(GET_ENTRY,GET_RETURN))
        by_id=defaultdict(list);active=set();nested=set();orphan=set()
        for event in population:
            if event['kind']not in(GET_ENTRY,GET_RETURN):continue
            pair=event['value'];by_id[pair].append(event)
            if event['kind']==GET_ENTRY:
                if active:nested.update(active|{pair})
                active.add(pair)
            elif pair not in active:orphan.add(pair)
            else:active.remove(pair)
        for pair,pop in sorted(by_id.items()):
            entry=[e for e in pop if e['kind']==GET_ENTRY];ret=[e for e in pop if e['kind']==GET_RETURN]
            result=dict(owner=owner,pair_id=pair,entry_ns=entry[0]['event_ns']if len(entry)==1 else None,
                        return_ns=ret[0]['event_ns']if len(ret)==1 else None,
                        commits=[e['commit']for e in pop],status='ambiguous')
            invalid=not pair or pair in nested or pair in orphan or pair in tied_get[owner] or any(e['flags']&2 for e in pop) or owner_loss[owner]
            if not invalid and len(entry)==len(ret)==1 and entry[0]['event_ns']<ret[0]['event_ns']:
                result['status']='complete'
            elif not invalid and len(entry)==1 and not ret and metadata['get_pending'][owner]==pair:
                result['status']='right_censored'
            elif not entry and len(ret)==1:
                result['status']='left_censored'
            if result['status']=='ambiguous':problem('ambiguous_Get_pair',owner=owner,pair_id=pair)
            pairs.append(result)
        if metadata['get_pending'][owner] and metadata['get_pending'][owner]not in active:
            problem('Get_pending_missing_entry',owner=owner,pair_id=metadata['get_pending'][owner])
        observed_max=max([e['value']for e in population if e['kind']==GET_ENTRY]or[0])
        if observed_max != metadata['get_next'][owner]:
            problem('Get_counter_conservation',owner=owner,observed_max=observed_max,next=metadata['get_next'][owner])
    ledger_keys=set(); output_rows=[]; candidates={}; references={}
    for index,row in enumerate(ledger_rows):
        if type(row)is not dict or not all(integer(row.get(k))for k in ['phase_id','arrival_id','caller'])or not 0<=row['caller']<8:
            raise ValueError('unknown ledger identity')
        identity=(row['phase_id'],row['arrival_id'],row['caller'])
        if identity in ledger_keys:problem('duplicate_ledger_ID',key=identity)
        ledger_keys.add(identity)
        if row.get('tid')!=tids[row['caller']]:problem('ledger_tid_mismatch',key=identity)
        if row.get('state')=='rejected_late' and integer(row.get('dispatch_checked_ns')):
            phase=row['phase_id'];references[phase]=min(references.get(phase,row['dispatch_checked_ns']),row['dispatch_checked_ns'])
    api_rows=[[] for _ in range(8)]
    for index,row in enumerate(ledger_rows):
        if row.get('attempted')is True and integer(row.get('api_begin_ns'))and integer(row.get('api_end_ns')):
            api_rows[row['caller']].append((row['api_begin_ns'],row['api_end_ns'],index))
    searches=[]
    for population in api_rows:
        population.sort(); maximum=[]
        for _,stop,_ in population:maximum.append(max(stop,maximum[-1]if maximum else stop))
        searches.append(([v[0]for v in population],maximum,population))
    for pair in pairs:
        if pair['status']!='complete' or not aligned:continue
        starts,maximum,population=searches[pair['owner']];contained=[]
        cursor=bisect_right(starts,pair['entry_ns'])-1
        while cursor>=0 and maximum[cursor]>=pair['return_ns']:
            begin,stop,index=population[cursor]
            if begin<=pair['entry_ns']<pair['return_ns']<=stop:contained.append(index)
            cursor-=1
        if len(contained)==1:candidates.setdefault(contained[0],[]).append(pair)
        else:problem('Get_pair_outside_API_or_ambiguous',owner=pair['owner'],pair_id=pair['pair_id'],contained_rows=contained)
    tables=[]
    for segments in intervals:
        prefix={state:[0]for state in STATES}
        for segment in segments:
            for state in STATES:prefix[state].append(prefix[state][-1]+(segment['end_ns']-segment['start_ns']if segment['state']==state else 0))
        tables.append(dict(starts=[s['start_ns']for s in segments],segments=segments,prefix=prefix))
    def overlap(owner,a,b,reference):
        if a is None or b is None:return dict(available=False,reason='no_endpoint')
        if not integer(a)or not integer(b)or b<a:
            problem('invalid_ledger_interval',owner=owner,start=a,end=b);return dict(available=False,reason='invalid_interval')
        result=dict(available=True,start_ns=a,end_ns=b,duration_ns=b-a,left_censored=a<start,right_censored=b>end,
                    after_rejection_check_ns=max(0,b-max(a,reference))if reference is not None else None,
                    before_rejection_check_ns=max(0,min(b,reference)-a)if reference is not None else None,
                    rejection_check_is_abort_time=False)
        if not aligned:
            result.update(states_ns=None,attribution_qualified=False,reason='clock_alignment_unverified');return result
        table=tables[owner]
        def area(t,state):
            t=min(max(t,start),end);i=bisect_right(table['starts'],t)-1
            if i<0:return 0
            segment=table['segments'][i]
            return table['prefix'][state][i]+(min(t,segment['end_ns'])-segment['start_ns']if segment['state']==state else 0)
        counts={state:area(b,state)-area(a,state)for state in STATES}
        counts['unknown']+=max(0,min(b,start)-a)+max(0,b-max(a,end))
        assert sum(counts.values())==b-a
        initial_unknown=max(0,min(b,first_anchor[owner])-max(a,start))
        result['left_censored']=result['left_censored']or bool(initial_unknown)
        result['ambiguous']=owner_loss[owner]or counts['unknown']>initial_unknown+max(0,min(b,start)-a)+max(0,b-max(a,end))
        result.update(states_ns=counts,owner_interval_ref=dict(owner=owner,clip_ns=([max(a,start),min(b,end)]if max(a,start)<min(b,end)else None)),
                      attribution_qualified=not owner_loss[owner])
        return result
    pairs_by_owner=[[]for _ in range(8)]
    for pair in pairs:pairs_by_owner[pair['owner']].append(pair)
    partial_by_owner=[[p for p in population if p['status']=='right_censored']for population in pairs_by_owner]
    ambiguous_by_owner=[[p for p in population if p['status']=='ambiguous']for population in pairs_by_owner]
    first_anchor=[min([e['event_ns']for e in population if e['kind']in(WAKE,WAKE_NEW,SWITCH_IN,SWITCH_OUT)and start<=e['event_ns']<end]or[end])for population in events]
    previous={}
    ordered=sorted(enumerate(ledger_rows),key=lambda item:(item[1]['phase_id'],item[1]['arrival_id'],item[0]))
    prior_by_index={}
    for index,row in ordered:
        prior_by_index[index]=previous.get(row['caller'])
        if integer(row.get('observer_end_ns')):previous[row['caller']]=row
    for index,row in enumerate(ledger_rows):
        owner=row['caller'];prior=prior_by_index[index];reference=references.get(row['phase_id'])
        found=candidates.get(index,[]);get=dict(status='missing')
        if row.get('attempted')is not True or row.get('api_begin_ns')is None:get=dict(status='no_API')
        elif not aligned:get=dict(status='clock_unverified')
        elif len(found)==1:get=dict(status='matched',**{k:found[0][k]for k in ['pair_id','entry_ns','return_ns']})
        elif len(found)>1:get=dict(status='ambiguous',pair_ids=[p['pair_id']for p in found])
        else:
            partial=[p for p in partial_by_owner[owner] if p['owner']==owner and p['entry_ns']is not None
                     and row['api_begin_ns']<=p['entry_ns']<=row['api_end_ns']]
            ambiguous=[p for p in ambiguous_by_owner[owner] if p['owner']==owner and any(t is not None and row['api_begin_ns']<=t<=row['api_end_ns']for t in [p['entry_ns'],p['return_ns']])]
            if partial:get=dict(status='right_censored',pair_ids=[p['pair_id']for p in partial])
            elif ambiguous:get=dict(status='ambiguous',pair_ids=[p['pair_id']for p in ambiguous])
            elif row['api_begin_ns']<start or row['api_end_ns']>end:get=dict(status='coverage_partial')
            else:problem('missing_Get_pair_for_API',row_index=index)
        entry=get.get('entry_ns');ret=get.get('return_ns')
        spans={'scheduled_to_checked':(row.get('scheduled_ns'),row.get('dispatch_checked_ns')),
               'api_begin_to_get_entry':(row.get('api_begin_ns'),entry),
               'get_entry_to_return':(entry,ret),'get_return_to_api_end':(ret,row.get('api_end_ns')),
               'api_end_to_observer':(row.get('api_end_ns'),row.get('observer_end_ns')),
               'previous_observer_to_scheduled':(prior.get('observer_end_ns')if prior else None,row.get('scheduled_ns'))}
        output_rows.append(dict(key=[row['phase_id'],row['arrival_id'],owner],ledger=row,state=row.get('state'),get=get,
            previous_observer_key=[prior['phase_id'],prior['arrival_id'],owner]if prior else None,
            previous_observer_is_cancel_time=False,
            intervals={name:overlap(owner,a,b,reference)for name,(a,b)in spans.items()}))
    complete=not issues
    for row in output_rows:
        for interval in row['intervals'].values():
            if 'attribution_qualified'in interval:interval['attribution_qualified']=complete and interval['attribution_qualified']
    return dict(integrity=dict(complete=complete,issues=issues),metadata=metadata,
        raw_binary_sha256=hashlib.sha256(event_bytes).hexdigest(),raw_binary_bytes=len(event_bytes),
        coverage=dict(start_ns=start,end_ns=end,convention='half-open; excludes enable/disable brackets',
                      initial_state_assumed=False),owner_intervals=intervals,Get_pairs=pairs,
        duplicate_wakes=duplicates,migrations=migrations,reordered_owners=reordered,rows=output_rows,
        raw_switch_out=[dict(owner=owner,event_ns=e['event_ns'],commit=e['commit'],cpu=e['cpu'],raw_state=e['value'],preempt=bool(e['flags']&1))for owner,population in enumerate(events)for e in population if e['kind']==SWITCH_OUT],
        owner_censor=[dict(owner=owner,first_anchor_ns=first_anchor[owner],initial_left_censored=first_anchor[owner]>start,final_state_right_censored=True)for owner in range(8)],
        raw_out_state_and_preempt_retained_in_input=True,raw_record_schema='<QQIIHHI',
        causal_attribution=False,performance_qualified=False,RPC30_accepted=False,
        limits='Scheduled residence is not verified user execution. No network, bridge, business or VM cause; rejection check is not an atomic abort timestamp.')


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ['metadata','events','ledger','output']:parser.add_argument('--'+name,type=Path,required=True)
    args=parser.parse_args(argv)
    if args.output.exists():raise ValueError('output exists')
    metadata=json.loads(args.metadata.read_text());raw=args.events.read_bytes()
    ledger=[json.loads(line)for line in args.ledger.read_text().splitlines()]
    result=analyze(metadata,raw,ledger)
    result['inputs_sha256']={name:hashlib.sha256(getattr(args,name).read_bytes()).hexdigest()for name in ['metadata','events','ledger']}
    with args.output.open('x')as output:json.dump(result,output,allow_nan=False,separators=(',',':'));output.write('\n')
    return 0 if result['integrity']['complete']else 2


if __name__=='__main__':
    raise SystemExit(main())
