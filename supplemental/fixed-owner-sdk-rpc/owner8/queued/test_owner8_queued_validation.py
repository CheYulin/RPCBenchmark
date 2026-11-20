"""Only synthetic queued populations; no SDK/process/network actions."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
import validate_owner8_queued_arrival as validate
import validate_owner8_queued_case as case
from queued_fixtures import PROFILE,fixture,refresh,rpc_fixture
from queued_case_fixture import write_case


def qualify(metric,rows):return validate.validate_arrivals(metric,rows,metric['operation'],smoke=metric['smoke'])


class QueuedArrivals(unittest.TestCase):
    def test_complete_late_and_forty_seven_ms_debt_are_observations(self):
        m,r=fixture(delay=2_000_000,service=lambda p,i:47_000_000 if i in (0,127,255)else 300_000)
        before=copy.deepcopy((m,r));q=qualify(m,r)
        self.assertTrue(q['valid'],q.get('error_counts'))
        self.assertEqual((m,r),before)
        self.assertFalse(m['phases'][0]['soft_late_budget_ok'])
        self.assertGreater(m['phases'][0]['debt_violations'],0)
        self.assertEqual(q['input_rows'],384)
        self.assertFalse(q['performance_eligible']);self.assertFalse(q['RPC30_accepted'])

    def test_original_trace_never_rebased_missing_duplicate_or_owner(self):
        for kind in ('schedule','missing','duplicate','owner'):
            m,r=fixture()
            if kind=='schedule':r[8]['scheduled_ns']+=1
            elif kind=='missing':r.pop()
            elif kind=='duplicate':r[-1]=copy.deepcopy(r[0])
            else:r[8]['caller']=1
            self.assertFalse(qualify(m,r)['valid'],kind)

    def test_FIFO_head_requires_previous_observer_and_ID_order(self):
        for kind in ('overlap','order'):
            m,r=fixture()
            if kind=='overlap':r[8]['head_enter_ns']=r[0]['observer_end_ns']-1
            else:r[0],r[8]=r[8],r[0]
            self.assertFalse(qualify(m,r)['valid'],kind)

    def test_H_can_precede_schedule_and_queued_head_can_follow_it(self):
        m,r=fixture(service=lambda p,i:47_000_000 if i==0 else 300_000)
        q=qualify(m,r);self.assertTrue(q['valid'],q.get('error_counts'))
        self.assertLess(r[0]['head_enter_ns'],r[0]['scheduled_ns'])
        self.assertGreater(r[8]['head_enter_ns'],r[8]['scheduled_ns'])
        audit=q['phases']['1'].get('queued',{})
        self.assertEqual(audit.get('full_cohort_count'),128)
        self.assertEqual(audit.get('queue_equations_verified_rows'),128)
        self.assertEqual(audit.get('distributions',{}).get('fifo_head_delay',{}).get('count'),128)

    def test_new_clock_fields_and_early_or_reversed_API_fail(self):
        for kind in ('head','K','early','checked','boolean'):
            m,r=fixture()
            if kind=='head':r[0]['head_enter_ns']=r[0]['api_begin_ns']+1
            elif kind=='K':r[0]['kv_call_begin_ns']=r[0]['api_end_ns']+1
            elif kind=='early':r[0]['api_begin_ns']=r[0]['scheduled_ns']-1
            elif kind=='checked':r[0]['dispatch_checked_ns']-=1
            else:r[0]['kv_call_begin_ns']=True
            self.assertFalse(qualify(m,r)['valid'],kind)

    def test_distinct_typed_profile_and_observation_flag(self):
        for kind in ('strict8','strict32','missing','bool','flag','strict_offered_flag'):
            m,r=fixture()
            if kind=='strict8':m['admission_profile']['protocol']='fixed-owner8-bounded-admission-v1'
            elif kind=='strict32':m['callers']=32
            elif kind=='missing':del m['admission_profile']['queue_policy']
            elif kind=='bool':m['admission_profile']['early_wake_ns']=True
            elif kind=='strict_offered_flag':m['fixed_offered_load']=True;del m['fixed_planned_offered_trace']
            else:m['late_and_debt_are_observations']=False
            self.assertFalse(qualify(m,r)['valid'],kind)

    def test_observation_counters_drain_and_new_quantiles_exact(self):
        for field in ('soft_late_count','debt_violations','soft_late_budget_ok','drain_api_ns','sdk_api_p99_ns'):
            m,r=fixture(delay=2_000_000)
            m['phases'][0][field]=True if field!='soft_late_budget_ok' else True
            self.assertFalse(qualify(m,r)['valid'],field)

    def test_partial_or_error_population_has_no_complete_distributions(self):
        for kind in ('status','bytes','exception','missing'):
            m,r=fixture()
            if kind=='status':r[0]['status']=1
            elif kind=='bytes':r[0]['mismatch']=True
            elif kind=='exception':r[0]['state']='execution_exception'
            else:r.pop()
            refresh(m,r);q=qualify(m,r)
            self.assertFalse(q['valid'],kind)
            self.assertFalse(q.get('complete_cohort_distributions',True))
            self.assertNotIn('queued',q.get('phases',{}).get('1',{}))

    def test_full_CDF_zero_tail_bucket_drain_and_signed_overlap(self):
        m,r=fixture(service=lambda p,i:47_000_000 if i in (0,255)else 300_000)
        q=qualify(m,r);self.assertTrue(q['valid'],q.get('error_counts'))
        audit=q['phases']['2'].get('queued',{})
        dist=audit.get('distributions',{}).get('fifo_head_delay',{})
        self.assertEqual(dist.get('count'),256)
        self.assertEqual(dist.get('cdf_ns',[None])[0][0],0)
        self.assertEqual(dist.get('cdf_ns',[[None,None]])[-1][1],256)
        self.assertGreater(audit.get('drain_observer_ns',0),0)
        self.assertLess(audit.get('minimum_signed_previous_gap_ns',0),0)
        for bucket in audit.get('buckets',{}).values():
            self.assertEqual(sum(bucket['scheduled_counts']),256)
            self.assertEqual(sum(bucket['admitted_counts']),256)
            if bucket['width_ns']==1_000_000:self.assertIn(0,bucket['admitted_counts'])
        self.assertEqual(audit.get('distributions',{}).get('sdk_api',{}).get('p991_ns'),300000)

    def test_exception_checked_lateness_retains_truthful_failed_counters(self):
        m,r=fixture(delay=2_000_000)
        r[0]['state']='execution_exception'
        refresh(m,r)
        q=qualify(m,r)
        self.assertFalse(q['valid'])
        self.assertNotIn('queued_soft_late_count_counter',q['errors'])
        self.assertNotIn('queued_debt_violations_counter',q['errors'])

    def test_formal_full_IDs_and_malformed_input_fail_closed(self):
        m,r=fixture(smoke=False,delay=lambda p,i:i%16384)
        q=qualify(m,r)
        self.assertTrue(q['valid'],q.get('error_counts'))
        self.assertEqual(q['input_rows'],36864)
        self.assertEqual(q['phases']['2']['queued']['distributions']['dispatch']['cdf_ns'][-1][1],32768)
        for bad in (None,{},dict(m,phases=None)):
            self.assertFalse(validate.validate_arrivals(bad,r,'get')['valid'])



class RPCAndContainers(unittest.TestCase):
    def result(self,backend='brpc',primary=100000):
        m,r,e,p,s=rpc_fixture(backend=backend,primary=primary)
        a=qualify(m,r);return validate.validate_rpc_rows(a,r,e,p,s,'get'),(m,r,e,p,s)

    def test_all_API_RPC_End_rows_full_CDF_and_callsite_containment(self):
        q,data=self.result();self.assertTrue(q.get('rpc_valid'),q.get('error_counts'))
        stats=q['phases']['2']['primary_rpc']
        self.assertEqual(stats.get('cdf_ns'),[[100000,256]])
        self.assertEqual(q['phases']['2'].get('per_api_rpc_union',{}).get('count'),256)
        m,r,e,p,s=data;p[0]['send_ns']=r[0]['kv_call_begin_ns']-1
        q=validate.validate_rpc_rows(qualify(m,r),r,e,p,s,'get')
        self.assertFalse(q['rpc_valid'])

    def test_RPC_errors_population_flags_order_and_rank_gates(self):
        for kind in ('missing','owner','flags','ABIend'):
            q,data=self.result();m,r,e,p,s=data
            if kind=='missing':p.pop()
            elif kind=='owner':p[0]['caller_index']=7
            elif kind=='flags':p[0]['flags']=3
            else:e[0]['api_begin_ns']+=1
            self.assertFalse(validate.validate_rpc_rows(qualify(m,r),r,e,p,s,'get')['rpc_valid'])
        quartet=[self.result(b,p)[0]for b,p in [('brpc',100000),('coro',70000),('coro',70000),('brpc',100000)]]
        self.assertTrue(validate.decide_backend_comparison(quartet)['pass'])
        quartet[1]['phases']['2']['primary_rpc']['p991_ns']=70001
        self.assertFalse(validate.decide_backend_comparison(quartet)['pass'])

    def test_synthetic_GetSet_enabled_off_containers_and_capacity(self):
        for operation in ('get','set'):
            for mode in ('enabled','off'):
                with tempfile.TemporaryDirectory()as directory:
                    root,sdk,digest=write_case(directory,mode,operation)
                    try:
                        q=case.validate_case(root,operation,'brpc',smoke=True,expected_sdk_path=sdk,expected_sdk_sha256=digest)
                    except ValueError as error:self.fail('valid queued container rejected: '+str(error))
                    self.assertTrue(q['valid']);self.assertIs(q['RPC_available'],mode=='enabled')
                    self.assertFalse(q['RPC30_accepted']);self.assertTrue(q['external_role_proofs_required'])
                    if mode=='enabled':
                        conf=json.loads((root/'capture/config.json').read_text());conf['normal_ends_per_caller']=384
                        (root/'capture/config.json').write_text(json.dumps(conf))
                        with self.assertRaises(ValueError):case.validate_case(root,operation,'brpc',smoke=True,expected_sdk_path=sdk,expected_sdk_sha256=digest)


if __name__=='__main__':unittest.main()
