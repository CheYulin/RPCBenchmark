"""Full-cohort synthetic counterexamples; no SDK, network or process actions."""
import copy
import hashlib
from pathlib import Path
import unittest

import validate_bounded_arrival as new
import validate_fixed_arrival as old

HERE = Path(__file__).resolve().parent
import test_validate_fixed_arrival as fixtures


PROFILE = dict(protocol="fixed-owner-bounded-admission-v1", owner_period_ns=64_000_000,
               hard_dispatch_lateness_ns_max_inclusive=63_999_999,
               soft_late_threshold_ns_exclusive=1_000_000,
               soft_late_budget_numerator=1, soft_late_budget_denominator=1000,
               no_debt_policy="observer_end_strictly_before_scheduled_plus_owner_period_including_last")


def summarize(metric, ledger):
    fixtures.summarize(metric, ledger)
    for phase in metric["phases"]:
        rows = [r for r in ledger if r["phase_id"] == phase["phase_id"] and r["state"] == "returned"]
        late = sum(r["api_begin_ns"] - r["scheduled_ns"] > 1_000_000 for r in rows)
        phase.update(soft_late_count=late, soft_late_budget_ok=1000 * late <= phase["planned"],
                     debt_violations=sum(r["observer_end_ns"] - r["scheduled_ns"] >= 64_000_000
                                         for r in rows))


def fixture(**kwargs):
    metric, ledger = fixtures.fixture(**kwargs)
    metric["admission_profile"] = dict(PROFILE)
    for row in ledger:
        row["dispatch_checked_ns"] = row["api_begin_ns"]
    summarize(metric, ledger)
    return metric, ledger


def arrival(**kwargs):
    metric, ledger = fixture(**kwargs)
    return new.validate_arrivals(metric, ledger, kwargs.get("operation", "get"),
                                 smoke=kwargs.get("smoke", True))


def delay_rows(ledger, ids, delay):
    for row in ledger:
        if (row["phase_id"], row["arrival_id"]) in ids:
            previous = row["api_begin_ns"] - row["scheduled_ns"]
            for key in ("api_begin_ns", "api_end_ns", "validation_end_ns", "observer_end_ns"):
                row[key] += delay - previous
            row["dispatch_checked_ns"] = row["api_begin_ns"]


def rpc_result(backend="brpc", primary=100_000, extra=0, smoke=True):
    metric, ledger = fixture(operation="set", mode="enabled", service=1_000_000, backend=backend, smoke=smoke)
    ends, rpcs, slots = [], [], []
    for row in ledger:
        send = row["api_begin_ns"] + 1_000
        spans = [(3, extra), (4, primary)] if extra else [(4, primary)]
        methods = 0
        for ordinal, (method, duration) in enumerate(spans, 1):
            duration = duration(row) if callable(duration) else duration
            rpcs.append(dict(phase_id=row["phase_id"], arrival_id=row["arrival_id"],
                             caller_index=row["caller"], rpc_ordinal=ordinal, method=method,
                             send_ns=send, recv_ns=send+duration, controller_error=0, ds_error=0, flags=1))
            methods |= 1 << method
            send += duration + 1_000
        ends.append(dict(phase_id=row["phase_id"], arrival_id=row["arrival_id"], caller_index=row["caller"],
                         scheduled_ns=row["scheduled_ns"], api_begin_ns=row["api_begin_ns"],
                         api_end_ns=row["api_end_ns"], validation_end_ns=row["validation_end_ns"],
                         observed_methods=methods, rpc_count=len(spans), capture_errors=0, outcome=0, flags=1))
    for caller in range(32):
        slots.append(dict(caller=caller, cached_tid=10000+caller, counters_available=True,
                          errors=0, error_generation=0, first_driver_status=0, active=0,
                          api_failures=0, rpc_failures=0, api_count=12 if smoke else 1152,
                          rpc_count=(12 if smoke else 1152)*(2 if extra else 1)))
    a = new.validate_arrivals(metric, ledger, "set", smoke=smoke)
    return new.validate_rpc_rows(a, ledger, ends, rpcs, slots, "set"), (a, ledger, ends, rpcs, slots)


class BoundedArrivals(unittest.TestCase):
    def test_legacy_source_and_old_default_untouched(self):
        self.assertEqual(hashlib.sha256((HERE/"validate_fixed_arrival.py").read_bytes()).hexdigest(),
                         "323275cac5978684cc88d29853c3f2154c96d35ec1f02ef89946c36b30a0ff04")
        metric, ledger = fixture(smoke=False)
        delay_rows(ledger, {(1, 0)}, 2_000_000)
        summarize(metric, ledger)
        self.assertIn("dispatch_budget", old.validate_arrivals(metric, ledger, "get")["errors"])

    def test_explicit_new_profile_required_and_typed(self):
        metric, ledger = fixture()
        for bad in (None, {}, {**PROFILE, "soft_late_budget_numerator": True},
                    {**PROFILE, "protocol": "fixed-owner-arrival-v1"}, {**PROFILE, "extra": 0}):
            metric["admission_profile"] = bad
            self.assertFalse(new.validate_arrivals(metric, ledger, "get", smoke=True)["valid"], bad)

    def test_valid_smoke_profile_and_no_input_mutation(self):
        metric, ledger = fixture()
        before = copy.deepcopy((metric, ledger))
        result = new.validate_arrivals(metric, ledger, "get", smoke=True)
        self.assertTrue(result["valid"], result)
        self.assertEqual(result["profile"]["admission_profile"], PROFILE)
        self.assertFalse(result["RPC30_accepted"])
        self.assertEqual((metric, ledger), before)

    def test_formal_four_and_thirty_two_late_allowed_five_or_thirty_three_rejected(self):
        for warm, measure, valid in ((4,32,True),(5,32,False),(4,33,False)):
            with self.subTest(warm=warm,measure=measure):
                metric, ledger = fixture(smoke=False)
                ids = {(1, i) for i in range(warm)} | {(2, i) for i in range(measure)}
                delay_rows(ledger, ids, 2_000_000)
                summarize(metric, ledger)
                result = new.validate_arrivals(metric, ledger, "get")
                self.assertEqual(result["valid"], valid, result["errors"])
                self.assertEqual(metric["phases"][0]["soft_late_count"], warm)
                self.assertEqual(metric["phases"][1]["soft_late_count"], measure)
                if valid:
                    burst = result["phases"]["2"]["admission"]["buckets"]["1000000"]
                    self.assertEqual(burst["max_sliding_scheduled"],1)
                    self.assertEqual(burst["max_sliding_admitted"],2)

    def test_smoke_zero_budget_and_exclusive_one_ms_threshold(self):
        for delay, valid in ((1_000_000,True),(1_000_001,False)):
            metric, ledger = fixture()
            delay_rows(ledger, {(1,0)}, delay); summarize(metric, ledger)
            self.assertEqual(new.validate_arrivals(metric, ledger, "get", smoke=True)["valid"], valid)

    def test_hard_dispatch_boundary_and_last_observer_debt_boundary(self):
        for delay, valid in ((63_999_999,True),(64_000_000,False)):
            metric, ledger = fixture(smoke=False, service=0)
            row = ledger[0]; delay_rows(ledger, {(1,0)}, delay)
            row["api_end_ns"] = row["validation_end_ns"] = row["observer_end_ns"] = row["api_begin_ns"]
            summarize(metric, ledger)
            result = new.validate_arrivals(metric, ledger, "get")
            self.assertEqual(result["valid"], valid, result["errors"])
        for elapsed, valid in ((63_999_999,True),(64_000_000,False)):
            metric, ledger = fixture()
            row = ledger[-1]
            row["api_end_ns"] = row["validation_end_ns"] = row["observer_end_ns"] = row["scheduled_ns"] + elapsed
            summarize(metric, ledger)
            result = new.validate_arrivals(metric, ledger, "get", smoke=True)
            self.assertEqual(result["valid"], valid, result["errors"])

    def test_driver_phase_counters_cannot_lie_or_be_boolean(self):
        for key, bad in (("soft_late_count",1),("soft_late_count",False),
                         ("soft_late_budget_ok",1),("debt_violations",True)):
            metric, ledger = fixture(); metric["phases"][1][key] = bad
            self.assertFalse(new.validate_arrivals(metric, ledger, "get", smoke=True)["valid"], key)

    def test_only_old_dispatch_error_may_be_replaced(self):
        metric, ledger = fixture(smoke=False)
        delay_rows(ledger, {(1,0)}, 2_000_000); ledger[1]["status"] = 7
        summarize(metric, ledger)
        result = new.validate_arrivals(metric, ledger, "get")
        self.assertFalse(result["valid"])
        self.assertIn("API_outcome", result["errors"])
        self.assertNotIn("dispatch_budget", result["errors"])
        for kind in ("missing","duplicate","cancelled","wrong_owner"):
            metric, ledger = fixture()
            if kind == "missing": ledger.pop()
            elif kind == "duplicate": ledger[-1] = copy.deepcopy(ledger[-2])
            elif kind == "cancelled": ledger[-1]["state"] = "cancelled_window"
            else: ledger[-1]["caller"] = 0
            self.assertFalse(new.validate_arrivals(metric, ledger, "get", smoke=True)["valid"], kind)

    def test_exact_cdf_buckets_sliding_burst_and_margin(self):
        metric, ledger = fixture(delay=0)
        delay_rows(ledger, {(2,0)}, 1_000_000); summarize(metric, ledger)
        result = new.validate_arrivals(metric, ledger, "get", smoke=True)
        self.assertTrue(result["valid"], result)
        audit = result["phases"]["2"]["admission"]
        self.assertEqual(audit["dispatch_cdf_ns"], [[0,255],[1_000_000,256]])
        self.assertEqual(audit["minimum_observer_margin_ns"], 62_680_000)
        one = audit["buckets"]["1000000"]
        self.assertEqual(one["scheduled_counts"][:3], [1,0,1])
        self.assertEqual(one["admitted_counts"][:3], [0,1,1])
        self.assertEqual(one["max_sliding_scheduled"],1)
        self.assertEqual(one["max_sliding_admitted"],1)
        self.assertEqual(set(audit["buckets"]), {"1000000","10000000","100000000"})

    def test_malformed_shapes_fail_closed_without_exceptions(self):
        for kind in ("null_phases","not_list_phases","unhashable_state","null_metric","null_ledger"):
            metric,ledger=fixture()
            if kind=="null_phases":metric["phases"]=None
            elif kind=="not_list_phases":metric["phases"]={}
            elif kind=="unhashable_state":ledger[0]["state"]={}
            elif kind=="null_metric":metric=None
            else:ledger=None
            with self.subTest(kind=kind):
                try:result=new.validate_arrivals(metric,ledger,"get",smoke=True)
                except Exception as error:self.fail("public validation raised instead of invalid: "+repr(error))
                self.assertFalse(result["valid"])

    def test_old_full_cohort_fixture_is_not_new_protocol(self):
        metric,ledger=fixtures.fixture()
        self.assertTrue(old.validate_arrivals(metric,ledger,"get",smoke=True)["valid"])
        result=new.validate_arrivals(metric,ledger,"get",smoke=True)
        self.assertFalse(result["valid"])
        self.assertIn("bounded_admission_profile",result["errors"])

    def test_real_producer_rejection_and_exception_counters_are_not_false_findings(self):
        for kind in ("rejected_late","execution_exception","exception_before_schedule","warm_rejected"):
            with self.subTest(kind=kind):
                metric,ledger=fixture()
                phase_id=1 if kind=="warm_rejected" else 2
                rows=[row for row in ledger if row["phase_id"]==phase_id]
                for row in rows:
                    row.update(state="cancelled_window",attempted=False,status=None,
                               dispatch_checked_ns=None,api_begin_ns=None,api_end_ns=None,
                               validation_end_ns=None,observer_end_ns=None)
                row=rows[0];scheduled=row["scheduled_ns"]
                exception = kind in ("execution_exception","exception_before_schedule")
                if exception:
                    finished=scheduled-1 if kind=="exception_before_schedule" else scheduled+64_000_000
                    row.update(state="execution_exception",attempted=True,status=-(1<<31),
                               dispatch_checked_ns=scheduled+2_000_000,api_begin_ns=scheduled+2_000_000,
                               api_end_ns=finished,validation_end_ns=finished,observer_end_ns=finished)
                else:row.update(state="rejected_late",dispatch_checked_ns=scheduled+64_000_000)
                fixtures.summarize(metric,ledger)
                phase=metric["phases"][phase_id-1]
                phase.update(soft_late_count=1,soft_late_budget_ok=False,
                             debt_violations=1 if exception else 0)
                metric.update(quality_valid=False,capture_close_ok=False)
                if kind=="warm_rejected":
                    metric["phases"][1].update(phase_started=False,epoch_created_ns=None,epoch_ns=None,
                                               planned_end_ns=None,soft_late_count=0,soft_late_budget_ok=False,
                                               debt_violations=0)
                    for r in ledger:
                        if r["phase_id"]==2:
                            r.update(state="unattempted_prior_phase",attempted=False,status=None,
                                     scheduled_ns=None,dispatch_checked_ns=None,api_begin_ns=None,
                                     api_end_ns=None,validation_end_ns=None,observer_end_ns=None)
                result=new.validate_arrivals(metric,ledger,"get",smoke=True)
                self.assertFalse(result["valid"])
                for code in ("bounded_soft_late_count_counter","bounded_debt_violations_counter",
                             "bounded_soft_late_budget_counter"):
                    self.assertNotIn(code,result["errors"],(kind,code,result["errors"]))

    def test_same_producer_timestamp_cannot_hide_a_soft_late_arrival(self):
        metric,ledger=fixture(smoke=False)
        delay_rows(ledger,{(1,0)},2_000_000);summarize(metric,ledger)
        ledger[0]["dispatch_checked_ns"]=ledger[0]["scheduled_ns"]
        result=new.validate_arrivals(metric,ledger,"get")
        self.assertFalse(result["valid"])
        self.assertIn("bounded_checked_begin_identity",result["errors"])


class BoundedRpcAndDecisions(unittest.TestCase):
    def test_rpc_exact_ranks_join_and_input_immutability(self):
        result, args = rpc_result()
        before = copy.deepcopy(args)
        self.assertTrue(result["rpc_valid"],result)
        for key in ("primary_rpc","per_api_rpc_sum"):
            self.assertEqual(result["phases"]["2"][key]["p989_ns"],100_000)
            self.assertEqual(result["phases"]["2"][key]["p991_ns"],100_000)
        self.assertEqual(new.nearest_rank_sensitivity_ns(list(range(1,1001))),
                         {"count":1000,"p989_ns":989,"p991_ns":991})
        rerun = new.validate_rpc_rows(*args,"set")
        self.assertTrue(rerun["valid"],rerun)
        self.assertEqual(args,before)
        args[3].pop()
        self.assertFalse(new.validate_rpc_rows(*args,"set")["rpc_valid"])

    def test_actual_formal_raw_rpc_cohort_has_distinct_exact_ranks(self):
        result,_=rpc_result(primary=lambda row:row["arrival_id"]+1,smoke=False)
        self.assertTrue(result["rpc_valid"],result["errors"])
        for name in ("primary_rpc","per_api_rpc_sum"):
            stats=result["phases"]["2"][name]
            self.assertEqual((stats["count"],stats["p989_ns"],stats["p99_ns"],stats["p991_ns"]),
                             (32768,32408,32441,32474))

    def test_rpc_bad_arrival_shape_is_invalid_not_exception(self):
        try:result=new.validate_rpc_rows(None,[],[],[],[],"set")
        except Exception as error:self.fail("public RPC validation raised instead of invalid: "+repr(error))
        self.assertFalse(result["rpc_valid"])

    def test_old_or_mixed_profiles_rejected_in_both_decisions(self):
        off = arrival(operation="set",service=1_000_000)
        on,_ = rpc_result()
        calibration = [off,on,copy.deepcopy(on),copy.deepcopy(off)]
        self.assertTrue(new.decide_calibration(calibration)["pass"])
        for index in (None,1):
            bad=copy.deepcopy(calibration)
            for i in (range(4) if index is None else [index]):bad[i]["profile"].pop("admission_profile")
            self.assertFalse(new.decide_calibration(bad)["pass"])
        parent,_=rpc_result();candidate,_=rpc_result(backend="coro",primary=70_000)
        base=[parent,candidate,copy.deepcopy(candidate),copy.deepcopy(parent)]
        self.assertTrue(new.decide_backend_comparison(base)["pass"])
        base[2]["profile"].pop("admission_profile")
        self.assertFalse(new.decide_backend_comparison(base)["pass"])

    def test_sensitivity_cannot_hide_behind_p99_both_metrics_directions_and_medians(self):
        parent,_=rpc_result(primary=100_000,extra=100_000)
        candidate,_=rpc_result(backend="coro",primary=70_000,extra=70_000)
        base=[parent,candidate,copy.deepcopy(candidate),copy.deepcopy(parent)]
        good=new.decide_backend_comparison(base)
        self.assertTrue(good["pass"],good)
        self.assertFalse(good["RPC30_accepted"])
        self.assertEqual(set(good["rank_sensitivity"]["medians"]),{"primary_rpc","per_api_rpc_sum"})
        for index in (1,2):
            for field in ("primary_rpc","per_api_rpc_sum"):
                bad=copy.deepcopy(base)
                bad[index]["phases"]["2"][field]["p991_ns"]+=1
                result=new.decide_backend_comparison(bad)
                self.assertFalse(result["pass"],(index,field,result))
        for field in ("p989_ns","p991_ns"):
            bad=copy.deepcopy(base);del bad[0]["phases"]["2"]["primary_rpc"][field]
            self.assertFalse(new.decide_backend_comparison(bad)["pass"])

    def test_sensitivity_missing_or_boolean_population_fails_closed(self):
        parent,_=rpc_result();candidate,_=rpc_result(backend="coro",primary=70_000)
        for count in (None,False,0):
            bad=copy.deepcopy([parent,candidate,candidate,parent])
            bad[0]["phases"]["2"]["primary_rpc"]["count"]=count
            self.assertFalse(new.decide_backend_comparison(bad)["pass"],count)

    def test_legacy_secondary_and_three_percent_calibration_gates_preserved(self):
        off=arrival(operation="set",service=1_000_000);on,_=rpc_result()
        base=[off,on,copy.deepcopy(on),copy.deepcopy(off)]
        self.assertTrue(new.decide_calibration(base)["pass"])
        base[1]["phases"]["2"]["CPU_us_per_success"]*=1.0300001
        self.assertFalse(new.decide_calibration(base)["pass"])
        parent,_=rpc_result();candidate,_=rpc_result(backend="coro",primary=70_000)
        base=[parent,candidate,copy.deepcopy(candidate),copy.deepcopy(parent)]
        base[2]["phases"]["2"]["qps"]*=.969999
        self.assertFalse(new.decide_backend_comparison(base)["pass"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
