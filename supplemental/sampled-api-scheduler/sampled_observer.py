import time
from exact_profile import task

class SampledObserver:
    def __init__(self,exact):
        self.exact=exact;self.record=exact.record;self.phases=[];self.sdk=None;self.done=False
    def check(self,target_exited=False):self.exact.check()
    def phase(self,sdk,owner,tids,phase):
        self.sdk=sdk
        if task(sdk.pid,sdk.pid)['startticks']!=owner['identity']['startticks'] or phase['target']['pid']!=sdk.pid:
            raise RuntimeError('SDK marker owner changed')
        if phase['phase_id']!=len(self.phases)+1:raise RuntimeError('phase order changed')
        if phase['phase_id']==1:self.exact.bind(sdk.pid,tids,phase['plan']['epoch_ns'])
        elif [task(sdk.pid,t)for t in tids]!=self.record['caller_tasks']:raise RuntimeError('caller identities changed')
        if time.monotonic_ns()>=phase['plan']['epoch_ns']:raise RuntimeError('original phase epoch missed')
        self.phases.append(phase);self.record['phases']=self.phases
    def stop(self):
        if self.done:return
        self.done=True
        partial=self.sdk is None or self.sdk.poll()not in (0,2)
        try:self.exact.close(partial=partial)
        finally:
            measure=next((p['plan']for p in self.phases if p['phase_id']==2),None)
            self.record['full_measure_coverage']=bool(not partial and measure and self.record.get('kernel_accounting_valid') and self.record.get('capture_start_ns',measure['epoch_ns'])<measure['epoch_ns'] and self.record.get('capture_stop_ns',0)>=measure['end_ns'] and not self.record.get('API_active_at_stop') and not self.record.get('pending_right_censored') and not self.record.get('wake_pending_right_censored'))
            self.record['status']='NOT_STARTED_NO_MEASURE' if measure is None else 'EXPORTED_MEASURE_COVERED' if self.record['full_measure_coverage']else'UNRESOLVED_COVERAGE'
            self.persist()
    def persist(self):
        import json
        (self.exact.out/'exact-scheduler.json').write_text(json.dumps(self.record,indent=2))
    def export(self):self.persist();return self.record
