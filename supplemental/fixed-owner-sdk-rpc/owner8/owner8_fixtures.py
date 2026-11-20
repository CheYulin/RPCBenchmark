"""Synthetic invariants for the fixed-owner validator; no SDK or host IO."""
import copy
import unittest

from validate_owner8_geometry import SCOPE


CALLERS, RATE, NS, LEAD = 8, 500, 1_000_000_000, 250_000_000
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
    counts = (16, 32) if smoke else (512, 4096)
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
                               scheduled_ns=scheduled, dispatch_checked_ns=begin,
                               api_begin_ns=begin, api_end_ns=begin+elapsed,
                               validation_end_ns=begin+elapsed+10_000,
                               observer_end_ns=begin+elapsed+20_000,
                               state="returned", attempted=True, status=0,
                               selected=True, mismatch=False,
                               required_methods=1 << (1 if operation == "get" else 4)))
    metric.update(admission_profile=dict(PROFILE), smoke=smoke, RPC30_accepted=False, fixed_offered_load=True, scheduled_equals_api_begin=False, arrival_E2E_available=True)
    refresh(metric, ledger)
    return metric, ledger



PROFILE = dict(protocol="fixed-owner8-bounded-admission-v1", owner_period_ns=16_000_000,
    hard_dispatch_lateness_ns_max_inclusive=15_999_999, soft_late_threshold_ns_exclusive=1_000_000,
    soft_late_budget_numerator=1, soft_late_budget_denominator=1000,
    no_debt_policy="observer_end_strictly_before_scheduled_plus_owner_period_including_last",
    pacing_policy="early_wake_then_spin_v1", early_wake_ns=1_000_000)


def refresh(metric, ledger):
    summarize(metric, ledger)
    for phase in metric["phases"]:
        rows = [r for r in ledger if r["phase_id"] == phase["phase_id"]]
        late = sum(type(r.get("dispatch_checked_ns")) is int and r["dispatch_checked_ns"]-r["scheduled_ns"]>1_000_000 for r in rows)
        debt = sum(r.get("attempted") is True and not r["scheduled_ns"]<=r["observer_end_ns"]<r["scheduled_ns"]+16_000_000 for r in rows)
        phase.update(soft_late_count=late,soft_late_budget_ok=1000*late<=phase["planned"],debt_violations=debt)


def delay(ledger, keys, value):
    for row in ledger:
        if (row["phase_id"],row["arrival_id"]) in keys:
            delta=value-(row["api_begin_ns"]-row["scheduled_ns"])
            for key in TIME_FIELDS[1:]: row[key]+=delta


def rpc_fixture(operation="get", mode="enabled", primary=100_000, backend="brpc", smoke=True, service=300_000):
    metric,ledger=fixture(operation=operation,mode=mode,backend=backend,smoke=smoke,service=service)
    method=1 if operation=="get" else 4
    ends,rpcs=[],[]
    for row in ledger:
        duration=primary(row) if callable(primary) else primary
        rpcs.append(dict(arrival_id=row["arrival_id"],send_ns=row["api_begin_ns"]+1000,recv_ns=row["api_begin_ns"]+1000+duration,
            server_processing_ns=0,server_execution_ns=0,phase_id=row["phase_id"],rpc_ordinal=1,caller_index=row["caller"],method=method,controller_error=0,ds_error=0,flags=1))
        ends.append(dict(phase_id=row["phase_id"],arrival_id=row["arrival_id"],scheduled_ns=row["scheduled_ns"],api_begin_ns=row["api_begin_ns"],
            api_end_ns=row["api_end_ns"],validation_end_ns=row["validation_end_ns"],observed_methods=1<<method,capture_errors=0,rpc_count=1,caller_index=row["caller"],outcome=0,flags=1))
    count=metric["warm_calls_per_caller"]+metric["measure_calls_per_caller"]
    slots=[dict(caller=c,cached_tid=10000+c,errors=0,error_generation=0,first_driver_status=0,active=0,counters_available=True,api_count=count,rpc_count=count,api_failures=0,rpc_failures=0,registered=1,sealed=0) for c in range(8)]
    return metric,ledger,ends,rpcs,slots
