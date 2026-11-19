"""Synthetic invariants for the fixed-owner validator; no SDK or host IO."""
import copy
import unittest

from validate_fixed_arrival import (
    SCOPE, nearest_rank_ns, validate_arrivals, validate_rpc_rows,
    decide_calibration, decide_backend_comparison,
)


CALLERS, RATE, NS, LEAD = 32, 500, 1_000_000_000, 250_000_000
TIME_FIELDS = ("scheduled_ns", "dispatch_checked_ns", "api_begin_ns", "api_end_ns",
               "validation_end_ns", "observer_end_ns")


def rank(values, percent):
    values = sorted(values)
    return values[(len(values) * percent + 99) // 100 - 1]


def summarize(metric, rows):
    """Independent fixture writer for the declared wire schema."""
    for phase in metric["phases"]:
        selected = [r for r in rows if r["phase_id"] == phase["phase_id"]]
        attempted = [r for r in selected if r["attempted"]]
        returned = [r for r in attempted if r["state"] == "returned"]
        successes = [r for r in returned if r["status"] == 0 and not r["mismatch"]]
        phase.update(attempts=len(attempted), success=len(successes),
                     errors=sum(r["status"] not in (0, None) for r in attempted),
                     mismatches=sum(r["mismatch"] for r in attempted),
                     cancelled=sum(r["state"] == "cancelled_window" for r in selected),
                     rejected_early=sum(r["state"] == "rejected_early" for r in selected),
                     rejected_late=sum(r["state"] == "rejected_late" for r in selected),
                     exceptions=sum(r["state"] == "execution_exception" for r in selected),
                     returned_bytes=len(successes)*4096 if metric["operation"] == "get" else 0,
                     validated_bytes=len(successes)*4096 if metric["operation"] == "get" else 0)
        if not returned:
            continue
        durations = {"api": [r["api_end_ns"]-r["api_begin_ns"] for r in returned],
                     "e2e": [r["api_end_ns"]-r["scheduled_ns"] for r in returned],
                     "dispatch": [r["api_begin_ns"]-r["scheduled_ns"] for r in returned],
                     "observer": [r["observer_end_ns"]-r["api_begin_ns"] for r in returned]}
        for name, values in durations.items():
            phase[name+"_p50_ns"] = rank(values, 50)
            phase[name+"_p99_ns"] = rank(values, 99)
        phase["max_dispatch_lateness_ns"] = max(durations["dispatch"])
        phase["completion_end_ns"] = max(phase["planned_end_ns"], max(r["api_end_ns"] for r in returned))
        phase["qps"] = len(successes)*NS/(phase["completion_end_ns"]-phase["epoch_ns"])
        before, after = phase["cpu_before"], phase["cpu_after"]
        after["read_begin_ns"] = max(r["observer_end_ns"] for r in returned)+1_000_000
        after["read_end_ns"] = after["read_begin_ns"]+1_000_000
        ticks = after["sdk_ticks"]+after["worker_ticks"]-before["sdk_ticks"]-before["worker_ticks"]
        phase["CPU_us_per_success"] = ticks*1_000_000/metric["clock_ticks_per_second"]/len(successes)


def fixture(operation="get", mode="off", service=300_000, delay=10_000,
            ticks=200, smoke=True, backend="brpc"):
    counts = (4, 8) if smoke else (128, 1024)
    first_epoch = NS
    warm_duration = (counts[0]*CALLERS*NS+RATE-1)//RATE
    epochs = (first_epoch, max(3*NS, first_epoch+warm_duration+NS))
    metric = dict(scope=SCOPE, operation=operation, backend=backend, payload_bytes=4096,
                  rate_per_second=RATE, callers=CALLERS,
                  warm_calls_per_caller=counts[0], measure_calls_per_caller=counts[1],
                  sdk_pid=4000, worker_pid=5000, clock_ticks_per_second=100,
                  sdk_shutdown_ok=True, capture_close_ok=True, quality_valid=True,
                  capture_mode=mode, rpc_capture_available=mode == "enabled",
                  readback_keys=64, readback_errors=0, phases=[])
    ledger = []
    for phase_id, count in enumerate(counts, 1):
        epoch = epochs[phase_id-1]
        planned = count*CALLERS
        duration = (planned*NS+RATE-1)//RATE
        before_ticks = 1000*phase_id
        phase = dict(phase_id=phase_id, planned=planned, phase_started=True,
                     epoch_created_ns=epoch-LEAD, epoch_ns=epoch,
                     planned_end_ns=epoch+duration, planned_duration_ns=duration,
                     offered_rate=planned*NS/duration,
                     cpu_before=dict(read_begin_ns=epoch-LEAD+100_000,
                                     read_end_ns=epoch-LEAD+200_000,
                                     sdk_ticks=before_ticks, worker_ticks=before_ticks),
                     cpu_after=dict(read_begin_ns=0, read_end_ns=0,
                                    sdk_ticks=before_ticks+ticks//2,
                                    worker_ticks=before_ticks+ticks-ticks//2))
        metric["phases"].append(phase)
        for arrival in range(planned):
            scheduled = epoch+arrival*NS//RATE
            wait = delay(phase_id, arrival) if callable(delay) else delay
            elapsed = service(phase_id, arrival) if callable(service) else service
            begin = scheduled+wait
            ledger.append(dict(caller=arrival%CALLERS, tid=10_000+arrival%CALLERS,
                               phase_id=phase_id, arrival_id=arrival,
                               scheduled_ns=scheduled, dispatch_checked_ns=scheduled+min(wait, 5000),
                               api_begin_ns=begin, api_end_ns=begin+elapsed,
                               validation_end_ns=begin+elapsed+10_000,
                               observer_end_ns=begin+elapsed+20_000,
                               state="returned", attempted=True, status=0,
                               selected=True, mismatch=False,
                               required_methods=1 << (1 if operation == "get" else 4)))
    summarize(metric, ledger)
    return metric, ledger


def arrival_result(operation="get", mode="off", **kwargs):
    metric, ledger = fixture(operation=operation, mode=mode, **kwargs)
    return validate_arrivals(metric, ledger, operation, smoke=kwargs.get("smoke", True))


def rpc_fixture(operation="get", spans=None, **kwargs):
    metric, ledger = fixture(operation=operation, mode="enabled", **kwargs)
    arrivals = validate_arrivals(metric, ledger, operation, smoke=kwargs.get("smoke", True))
    primary = 1 if operation == "get" else 4
    spans = spans or [(primary, 100_000)]
    ends, rpcs = [], []
    for row in ledger:
        send = row["api_begin_ns"]+1000
        methods = 0
        for ordinal, (method, duration) in enumerate(spans, 1):
            rpcs.append(dict(phase_id=row["phase_id"], arrival_id=row["arrival_id"],
                             caller_index=row["caller"], rpc_ordinal=ordinal, method=method,
                             send_ns=send, recv_ns=send+duration,
                             controller_error=0, ds_error=0, flags=1))
            send += duration+1000
            methods |= 1 << method
        ends.append(dict(phase_id=row["phase_id"], arrival_id=row["arrival_id"],
                         caller_index=row["caller"], scheduled_ns=row["scheduled_ns"],
                         api_begin_ns=row["api_begin_ns"], api_end_ns=row["api_end_ns"],
                         validation_end_ns=row["validation_end_ns"], observed_methods=methods,
                         capture_errors=0, rpc_count=len(spans), outcome=0, flags=1))
    per_owner = metric["warm_calls_per_caller"]+metric["measure_calls_per_caller"]
    slots = [dict(caller=c, cached_tid=10_000+c, errors=0, error_generation=0,
                  first_driver_status=0, active=0, counters_available=True,
                  api_count=per_owner, rpc_count=per_owner*len(spans),
                  api_failures=0, rpc_failures=0, registered=True, sealed=False)
             for c in range(CALLERS)]
    return arrivals, ledger, ends, rpcs, slots


def rpc_result(operation="get", spans=None, **kwargs):
    return validate_rpc_rows(*rpc_fixture(operation, spans, **kwargs), operation)


def calibration_result(mode, **kwargs):
    return rpc_result(**kwargs) if mode == "enabled" else arrival_result(mode="off", **kwargs)


class NearestRankTests(unittest.TestCase):
    def test_exact_rank_and_no_interpolation(self):
        stats = nearest_rank_ns([1000, 1, 3000, 2])
        self.assertEqual(stats["p50_ns"], 2)
        self.assertEqual(stats["p99_ns"], 3000)
        self.assertEqual(nearest_rank_ns([0])["p99_ns"], 0)
        self.assertIsNone(nearest_rank_ns([])["p99_ns"])

    def test_noninteger_boolean_negative_or_clock_overflow_rejected(self):
        for value in (True, -1, 1.0, float("nan"), 1 << 63):
            with self.subTest(value=repr(value)), self.assertRaises(ValueError):
                nearest_rank_ns([value])


class ArrivalTests(unittest.TestCase):
    def test_actual_driver_metadata_cannot_be_inferred(self):
        for key, bad in (("backend", "coro_rpc"), ("operation", "set"), ("payload_bytes", 1)):
            metric, ledger = fixture()
            metric[key] = bad
            self.assertFalse(validate_arrivals(metric, ledger, "get", smoke=True)["valid"])
        metric, ledger = fixture()
        del metric["backend"]
        self.assertFalse(validate_arrivals(metric, ledger, "get", smoke=True)["valid"])
        metric, ledger = fixture()
        metric["phases"][1]["offered_rate"] = RATE+1
        self.assertFalse(validate_arrivals(metric, ledger, "get", smoke=True)["valid"])
    def valid(self, metric, ledger, operation="get", smoke=True):
        result = validate_arrivals(metric, ledger, operation, smoke=smoke)
        self.assertTrue(result["valid"], result["errors"])
        return result

    def test_smoke_and_formal_have_complete_fixed_id_populations(self):
        for smoke, count in ((True, 384), (False, 36864)):
            metric, ledger = fixture(smoke=smoke)
            self.assertEqual(len(ledger), count)
            result = self.valid(metric, ledger, smoke=smoke)
            self.assertEqual(result["phases"]["2"]["service"]["count"], metric["phases"][1]["planned"])
            self.assertFalse(result["RPC30_accepted"])

    def test_smoke_may_not_silently_qualify_as_formal(self):
        metric, ledger = fixture()
        self.assertFalse(validate_arrivals(metric, ledger, "get")["valid"])

    def test_missing_duplicate_wrong_owner_and_tid_reuse(self):
        for kind in ("missing", "duplicate", "owner", "tid"):
            metric, ledger = fixture()
            if kind == "missing": ledger.pop()
            elif kind == "duplicate": ledger[-1] = copy.deepcopy(ledger[-2])
            elif kind == "owner": ledger[-1]["caller"] = 0
            else:
                for row in ledger:
                    if row["caller"] == 1: row["tid"] = 10_000
            self.assertFalse(validate_arrivals(metric, ledger, "get", smoke=True)["valid"], kind)

    def test_invalid_states_and_errors_are_never_success_subsets(self):
        for state in ("planned", "rejected_early", "rejected_late", "cancelled_window",
                      "execution_exception", "invalid_time", "unattempted_prior_phase"):
            metric, ledger = fixture(); ledger[0]["state"] = state
            self.assertFalse(validate_arrivals(metric, ledger, "get", smoke=True)["valid"], state)
        for key, value in (("status", 1), ("mismatch", True), ("selected", False), ("attempted", False)):
            metric, ledger = fixture(); ledger[0][key] = value
            self.assertFalse(validate_arrivals(metric, ledger, "get", smoke=True)["valid"], key)

    def test_required_method_and_capture_mode_cannot_be_inferred(self):
        for key, value in (("required_methods", 1 << 3), ("required_methods", True)):
            metric, ledger = fixture("set")
            ledger[0][key] = value
            self.assertFalse(validate_arrivals(metric, ledger, "set", smoke=True)["valid"])
        for key, value in (("scope", "FINITE_CLOSED_LOOP"), ("quality_valid", False),
                           ("readback_errors", 1), ("rpc_capture_available", True)):
            metric, ledger = fixture(); metric[key] = value
            self.assertFalse(validate_arrivals(metric, ledger, "get", smoke=True)["valid"], key)

    def test_slow_owner_debt_and_schedule_reset_cannot_be_hidden(self):
        metric, ledger = fixture()
        slow = next(r for r in ledger if r["phase_id"] == 2 and r["arrival_id"] == 0)
        following = next(r for r in ledger if r["phase_id"] == 2 and r["arrival_id"] == 32)
        slow.update(api_end_ns=slow["api_begin_ns"]+70_000_000,
                    validation_end_ns=slow["api_begin_ns"]+70_010_000,
                    observer_end_ns=slow["api_begin_ns"]+70_020_000)
        begin = slow["observer_end_ns"]
        following.update(dispatch_checked_ns=begin, api_begin_ns=begin, api_end_ns=begin+300_000,
                         validation_end_ns=begin+310_000, observer_end_ns=begin+320_000)
        summarize(metric, ledger)
        self.assertFalse(validate_arrivals(metric, ledger, "get", smoke=True)["valid"])
        following["scheduled_ns"] = begin
        summarize(metric, ledger)
        self.assertFalse(validate_arrivals(metric, ledger, "get", smoke=True)["valid"])

    def test_e2e_quantile_is_not_sum_of_service_and_dispatch_quantiles(self):
        service = lambda phase, arrival: 100_000 if arrival % 2 else 300_000
        delay = lambda phase, arrival: 900_000 if arrival % 2 else 0
        metric, ledger = fixture(service=service, delay=delay)
        result = self.valid(metric, ledger)
        phase = result["phases"]["2"]
        self.assertEqual(phase["e2e"]["p99_ns"], 1_000_000)
        self.assertNotEqual(phase["e2e"]["p99_ns"], phase["service"]["p99_ns"]+phase["dispatch"]["p99_ns"])
        metric["phases"][1]["e2e_p99_ns"] = 1_200_000
        self.assertFalse(validate_arrivals(metric, ledger, "get", smoke=True)["valid"])

    def test_completion_tail_changes_qps_denominator(self):
        service = lambda phase, arrival: 10_000_000 if phase == 2 and arrival == 255 else 300_000
        metric, ledger = fixture(service=service)
        result = self.valid(metric, ledger)
        phase = metric["phases"][1]
        self.assertGreater(phase["completion_end_ns"], phase["planned_end_ns"])
        self.assertLess(result["phases"]["2"]["qps"], 500)
        phase["qps"] = 500
        self.assertFalse(validate_arrivals(metric, ledger, "get", smoke=True)["valid"])

    def test_cpu_ticks_brackets_no_idle_subtraction_and_nan(self):
        for kind in ("ticks", "bracket", "subtract", "nan", "hz"):
            metric, ledger = fixture(); phase = metric["phases"][1]
            if kind == "ticks": phase["cpu_after"]["sdk_ticks"] = phase["cpu_before"]["sdk_ticks"]-1
            elif kind == "bracket": phase["cpu_before"]["read_end_ns"] = next(r["api_begin_ns"] for r in ledger if r["phase_id"] == 2)+1
            elif kind == "subtract": phase["CPU_us_per_success"] *= .9
            elif kind == "nan": phase["qps"] = float("nan")
            else: metric["clock_ticks_per_second"] = 0
            self.assertFalse(validate_arrivals(metric, ledger, "get", smoke=True)["valid"], kind)

    def test_abort_keeps_full_unstarted_phase_and_never_mutates_input(self):
        metric, ledger = fixture()
        metric["quality_valid"] = False
        metric["phases"][1].update(phase_started=False, epoch_created_ns=None, epoch_ns=None, planned_end_ns=None)
        for row in ledger:
            if row["phase_id"] == 2:
                row.update(state="unattempted_prior_phase", attempted=False, status=None)
                for name in TIME_FIELDS: row[name] = None
        before = copy.deepcopy((metric, ledger))
        result = validate_arrivals(metric, ledger, "get", smoke=True)
        self.assertFalse(result["valid"])
        self.assertEqual((metric, ledger), before)
        self.assertEqual(len(ledger), 384)


class RpcTests(unittest.TestCase):
    def test_actual_abi_one_based_ordinals_and_optional_metadata(self):
        args = rpc_fixture("set", [(3, 80_000), (4, 100_000)], service=500_000)
        result = validate_rpc_rows(*args, "set")
        self.assertTrue(result["rpc_valid"], result["errors"])
        self.assertEqual(result["phases"]["2"]["per_api_rpc_sum"]["p99_ns"], 180_000)
        args[3][0].update(flags=1|512, server_processing_ns=999_999_999)
        self.assertTrue(validate_rpc_rows(*args, "set")["rpc_valid"])

    def test_missing_ordinal_overlap_outside_api_and_unknown_method(self):
        for kind in ("missing", "zero_based", "overlap", "outside", "method", "status", "duplicate_end"):
            args = rpc_fixture("set", [(3, 80_000), (4, 100_000)], service=500_000)
            if kind == "missing": args[3].pop()
            elif kind == "zero_based": args[3][0]["rpc_ordinal"] = 0
            elif kind == "overlap": args[3][1]["send_ns"] = args[3][0]["recv_ns"]-1
            elif kind == "outside": args[3][0]["send_ns"] = args[1][0]["api_begin_ns"]-1
            elif kind == "method": args[3][0]["method"] = 0
            elif kind == "status": args[3][0]["ds_error"] = 1
            else: args[2].append(copy.deepcopy(args[2][0]))
            self.assertFalse(validate_rpc_rows(*args, "set")["rpc_valid"], kind)

    def test_primary_cannot_be_missing_and_seventeenth_rpc_is_invalid(self):
        boundary = rpc_fixture("set", [(4, 10_000)]*16, service=1_000_000)
        self.assertTrue(validate_rpc_rows(*boundary, "set")["rpc_valid"])
        for spans in ([(3, 20_000)], [(4, 10_000)]*17):
            args = rpc_fixture("set", spans, service=1_000_000)
            self.assertFalse(validate_rpc_rows(*args, "set")["rpc_valid"])

    def test_required_methods_is_not_replaced_by_observed_methods(self):
        args = rpc_fixture("set", [(3, 80_000), (4, 100_000)], service=500_000)
        args[1][0]["required_methods"] = 1 << 3
        self.assertFalse(validate_rpc_rows(*args, "set")["rpc_valid"])


class DecisionTests(unittest.TestCase):
    def test_calibration_inclusive_three_percent_and_unknown(self):
        quartet = [calibration_result("off", service=1_000_000),
                   calibration_result("enabled", service=1_030_000, ticks=206),
                   calibration_result("enabled", service=1_030_000, ticks=206),
                   calibration_result("off", service=1_000_000)]
        result = decide_calibration(quartet)
        self.assertTrue(result["pass"], result)
        self.assertFalse(result["RPC30_accepted"])
        quartet[1] = calibration_result("enabled", service=1_030_001, ticks=206)
        self.assertFalse(decide_calibration(quartet)["pass"])
        quartet[1]["valid"] = False
        self.assertFalse(decide_calibration(quartet)["pass"])

    def test_calibration_cannot_mix_modes_operations_or_large_control_drift(self):
        base = [calibration_result(m, service=1_000_000) for m in ("off", "enabled", "enabled", "off")]
        wrong = copy.deepcopy(base); wrong[1]["capture_mode"] = "off"
        self.assertFalse(decide_calibration(wrong)["pass"])
        wrong = copy.deepcopy(base); wrong[1]["operation"] = "set"
        self.assertFalse(decide_calibration(wrong)["pass"])
        base[3] = calibration_result("off", service=1_040_000)
        self.assertFalse(decide_calibration(base)["pass"])

    def test_calibration_requires_on_rpc_rows_and_both_sides_of_three_percent(self):
        base = [calibration_result(mode, service=1_000_000)
                for mode in ("off", "enabled", "enabled", "off")]
        self.assertTrue(decide_calibration(base)["pass"])
        missing = copy.deepcopy(base)
        missing[1] = arrival_result(mode="enabled", service=1_000_000)
        self.assertFalse(decide_calibration(missing)["pass"])
        below = copy.deepcopy(base)
        below[1] = calibration_result("enabled", service=969_999)
        self.assertFalse(decide_calibration(below)["pass"])

    def test_rpc_thirty_percent_boundaries_and_create_sum_trap(self):
        parent = rpc_result("set", [(3, 100_000), (4, 100_000)], service=1_000_000)
        candidate = rpc_result("set", [(3, 70_000), (4, 70_000)], service=1_000_000, backend="coro")
        result = decide_backend_comparison([parent, candidate, candidate, parent])
        self.assertTrue(result["pass"], result)
        self.assertFalse(result["RPC30_accepted"])
        hidden = rpc_result("set", [(3, 200_000), (4, 70_000)], service=1_000_000, backend="coro")
        self.assertFalse(decide_backend_comparison([parent, hidden, hidden, parent])["pass"])
        above = rpc_result("set", [(3, 70_001), (4, 70_001)], service=1_000_000, backend="coro")
        self.assertFalse(decide_backend_comparison([parent, above, above, parent])["pass"])

    def test_backend_secondary_and_control_drift_cannot_hide_behind_primary(self):
        parent = rpc_result("set", [(3, 100_000), (4, 100_000)], service=1_000_000)
        candidate = rpc_result("set", [(3, 70_000), (4, 70_000)], service=1_040_000, backend="coro")
        self.assertFalse(decide_backend_comparison([parent, candidate, candidate, parent])["pass"])
        candidate = rpc_result("set", [(3, 70_000), (4, 70_000)], service=1_000_000, backend="coro")
        drifting = rpc_result("set", [(3, 104_000), (4, 104_000)], service=1_000_000)
        self.assertFalse(decide_backend_comparison([parent, candidate, candidate, drifting])["pass"])

    def test_backend_identity_order_is_explicit_not_argument_position(self):
        parent = rpc_result("set", [(3, 100_000), (4, 100_000)], service=1_000_000)
        candidate = rpc_result("set", [(3, 70_000), (4, 70_000)], service=1_000_000, backend="coro")
        wrong = copy.deepcopy(candidate); wrong["backend"] = "brpc"
        self.assertFalse(decide_backend_comparison([parent, wrong, candidate, parent])["pass"])
        on = rpc_result(service=1_000_000)
        off = arrival_result(mode="off", service=1_000_000)
        on_other = copy.deepcopy(on); on_other["backend"] = "coro"
        self.assertFalse(decide_calibration([off, on_other, on, off])["pass"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
