"""Pure explicit-field perf-script aggregation; identities and addresses never return."""
from collections import Counter
import re
import struct

HEADER=re.compile(r'^\s*(\d+)/\s*(\d+)\s+(\d+)\s+([^\s]+):\s*(.*)$')
FRAME=re.compile(r'^\s*(?:0x)?[0-9a-fA-F]+\s+(.+?)\s*$')


def symbol_name(raw):
    value=raw.rsplit(' (',1)[0].strip()
    value=re.sub(r'\+0x[0-9a-fA-F]+(?:/0x[0-9a-fA-F]+)?$','',value)
    if value in ('[unknown]','[unknown symbol]','??','')or re.fullmatch(r'0x[0-9a-fA-F]+|[0-9]+|[0-9a-fA-F]{8,}',value):return '[unknown]'
    if len(value)>240:return '[symbol_too_long]'
    if any(c in value for c in ('/','\\','0x','\x1b'))or not all(32<=ord(c)<127 for c in value):return '[redacted_symbol]'
    return value


def summarize_script(lines, roles, max_symbols=64, max_stack=127):
    if (len(roles)!=2 or set(roles.values())!={'SDK','Worker'}or any(type(k)is not int or k<=0 for k in roles)
            or not 1<=max_symbols<=128 or not 1<=max_stack<=127):raise ValueError('invalid role or output bound')
    results={name:dict(sample_count=0,period_weight=0,unknown_leaf_samples=0,empty_chain_samples=0,
        single_frame_samples=0,depth_limit_samples=0,unknown_frames=0,redacted_frames=0,leaf={},inclusive={})for name in roles.values()}
    excluded=Counter(foreign_samples=0,foreign_period_weight=0,unexpected_event_samples=0,unexpected_event_period_weight=0)
    counts=Counter(headers=0,script_lost_records=0,script_lost_samples=0,script_lost_without_count=0);foreign=set();current=None
    def add(table,name,period):
        value=table.setdefault(name,[0,0]);value[0]+=1;value[1]+=period
    def finish():
        nonlocal current
        if current is None:return
        pid,period,event,frames=current;current=None
        if event not in ('cpu-clock:u','cpu-clock'):
            excluded['unexpected_event_samples']+=1;excluded['unexpected_event_period_weight']+=period;return
        if pid not in roles:
            excluded['foreign_samples']+=1;excluded['foreign_period_weight']+=period;foreign.add(pid);return
        row=results[roles[pid]];row['sample_count']+=1;row['period_weight']+=period
        row['empty_chain_samples']+=not frames;row['single_frame_samples']+=len(frames)==1;row['depth_limit_samples']+=len(frames)>=max_stack
        if not frames:frames=['[unknown]']
        row['unknown_frames']+=frames.count('[unknown]');row['redacted_frames']+=sum(v in ('[redacted_symbol]','[symbol_too_long]')for v in frames)
        row['unknown_leaf_samples']+=frames[0]=='[unknown]';add(row['leaf'],frames[0],period)
        for name in set(frames):add(row['inclusive'],name,period)
    def frame(text):
        match=FRAME.fullmatch(text)
        if current is None or match is None:raise ValueError('unrecognized or orphan perf frame')
        if len(current[3])>=max_stack:raise ValueError('callchain exceeds declared output depth')
        current[3].append(symbol_name(match[1]))
    for line in lines:
        if len(line)>16384:raise ValueError('perf output line exceeds bound')
        if not line.strip():finish();continue
        if line.startswith('#'):continue
        if 'PERF_RECORD_LOST' in line and HEADER.fullmatch(line.rstrip('\n'))is None and FRAME.fullmatch(line.rstrip('\n'))is None:
            finish();counts['script_lost_records']+=1;m=re.search(r'\blost\s+(\d+)\b',line,re.I)
            if m:counts['script_lost_samples']+=int(m[1])
            else:counts['script_lost_without_count']+=1
            continue
        match=HEADER.fullmatch(line.rstrip('\n'))
        if match:
            finish();pid,tid,period=map(int,match.group(1,2,3))
            if pid>2**31-1 or tid>2**31-1 or period>2**64-1:raise ValueError('perf scalar out of range')
            current=[pid,period,match[4],[]];counts['headers']+=1
            if match[5]:frame(match[5])
        else:frame(line.rstrip('\n'))
    finish()
    for role in results.values():
        for name in ('leaf','inclusive'):
            ordered=sorted(role[name].items(),key=lambda item:(-item[1][1],-item[1][0],item[0]));shown,rest=ordered[:max_symbols],ordered[max_symbols:]
            role[name]=dict(rows=[dict(symbol=k,samples=v[0],period_weight=v[1])for k,v in shown],
                other=dict(symbols=len(rest),samples=sum(v[0]for k,v in rest),period_weight=sum(v[1]for k,v in rest)))
    excluded['foreign_TGID_count']=len(foreign)
    return dict(roles=results,excluded=dict(excluded),format_counts=dict(counts),inclusive_nonadditive=True,
        inclusive_definition='Each distinct symbol contributes once per sample callchain; symbols overlap.',
        leaf_order='Default callee-first perf script; no inline expansion.',unwind_completeness_proven=False)


def record_metadata(stream):
    stream.seek(0,2);length=stream.tell();stream.seek(0);header=stream.read(72)
    if len(header)<72 or header[:8]not in (b'PERFILE2',b'2ELIFREP'):raise ValueError('unsupported perf file header')
    endian='<'if header[:8]==b'PERFILE2'else'>';size=struct.unpack_from(endian+'Q',header,8)[0]
    offset,wanted=struct.unpack_from(endian+'QQ',header,40)
    if not 72<=size<=length or not size<=offset<=length:raise ValueError('invalid perf data section')
    end=min(length,offset+wanted);position=offset;types=Counter();lost=0;bad=0;tails=0
    while position<end:
        stream.seek(position);raw=stream.read(min(8,end-position))
        if len(raw)<8:tails+=1;break
        kind,misc,width=struct.unpack(endian+'IHH',raw)
        if width<8 or position+width>end:tails+=1;break
        types[kind]+=1
        if kind in (2,13):
            needed=24 if kind==2 else 16
            if width<needed:bad+=1
            else:
                stream.seek(position+(16 if kind==2 else 8));lost+=struct.unpack(endian+'Q',stream.read(8))[0]
        position+=width
    complete=offset+wanted<=length and not tails and not bad and not types[81]
    return dict(file_bytes=length,data_declared_bytes=wanted,data_available_bytes=end-offset,record_count=sum(types.values()),
        sample_records=types[9],lost_records=types[2]+types[13],lost_samples=lost,throttle_records=types[5],unthrottle_records=types[6],
        opaque_compressed_records=types[81],malformed_loss_records=bad,truncated_record_tails=tails,
        record_stream_complete=complete,counts_are_observed_lower_bounds=not complete)


def warning_counts(text):
    lines=[line for line in text.splitlines()if line.strip()]
    return dict(stderr_nonempty_lines=len(lines),unwind_related_lines=sum(bool(re.search('unwind|libunwind',v,re.I))for v in lines),
        truncation_related_lines=sum(bool(re.search('truncat|stack.*(?:invalid|fail)',v,re.I))for v in lines),
        loss_related_lines=sum(bool(re.search(r'\blost\b',v,re.I))for v in lines),throttle_related_lines=sum(bool(re.search('throttl',v,re.I))for v in lines),
        symbol_resolution_related_lines=sum(bool(re.search('symbol|build.?id|dso|mmap',v,re.I))for v in lines),unwind_completeness_proven=False)
