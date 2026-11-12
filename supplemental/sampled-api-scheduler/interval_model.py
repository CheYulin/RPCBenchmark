def split(out_ns,wake_ns,in_ns,state,duplicate):
    if not 0<out_ns<=in_ns or (wake_ns and not out_ns<=wake_ns<=in_ns):
        raise ValueError('invalid scheduler endpoints')
    blocked=runnable=None
    if state==0:
        runnable=(out_ns,in_ns)
    elif wake_ns and not duplicate:
        blocked=(out_ns,wake_ns);runnable=(wake_ns,in_ns)
    return dict(offcpu=(out_ns,in_ns),blocked=blocked,runnable=runnable)
def overlap(interval,rpc):
    if interval is None:return None
    return max(0,min(interval[1],rpc[1])-max(interval[0],rpc[0]))

def publication(events):
    out=wake=0;duplicate=False
    for kind,stamp in events:
        if kind=='out':out=stamp
        elif kind=='wake':
            if wake:duplicate=True
            else:wake=stamp
        elif kind=='in':return split(out,wake,stamp,1,duplicate)
    raise ValueError('no completed interval')

def normalize_wake(out_ns,wake_ns,in_ns):
    if wake_ns and wake_ns<out_ns:return 0,1
    if wake_ns>in_ns:return 0,2
    return wake_ns,0
