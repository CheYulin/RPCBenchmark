from interval_model import split,overlap

def join_waits(rpc,rows):
    if not 0<rpc[0]<rpc[1]:raise ValueError('invalid RPC boundary')
    total=dict(offcpu_ns=0,known_blocked_ns=0,runnable_lower_ns=0,unknown_split_ns=0,overlapping_intervals=0)
    previous=0
    for row in sorted(rows,key=lambda r:r['out_ns']):
        if row['out_ns']<previous:raise ValueError('overlapping offCPU intervals')
        previous=row['in_ns']
        spans=split(row['out_ns'],row['wake_ns'],row['in_ns'],row['state'],row['duplicate'])
        off=overlap(spans['offcpu'],rpc);blocked=overlap(spans['blocked'],rpc);runnable=overlap(spans['runnable'],rpc)
        total['offcpu_ns']+=off
        total['known_blocked_ns']+=blocked or 0
        total['runnable_lower_ns']+=runnable or 0
        total['unknown_split_ns']+=off if blocked is None and runnable is None else 0
        total['overlapping_intervals']+=off>0
    if total['offcpu_ns']>rpc[1]-rpc[0]:raise ValueError('offCPU exceeds RPC')
    if total['offcpu_ns']!=total['known_blocked_ns']+total['runnable_lower_ns']+total['unknown_split_ns']:raise ValueError('wait split not conserved')
    total['unaccounted_rpc_ns']=rpc[1]-rpc[0]-total['offcpu_ns']
    return total
