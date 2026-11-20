"""Distinct queued owner8 protocol; no strict-validator error filtering."""
from collections import Counter
import copy
from decimal import Decimal
import statistics

import validate_owner8_queued_geometry as legacy

ADMISSION_PROFILE = dict(
    protocol="fixed-owner8-queued-arrival-v1",owner_period_ns=16_000_000,
    pacing_policy="early_wake_then_spin_v1",early_wake_ns=1_000_000,
    queue_policy="per_owner_fifo_one_inflight",late_policy="record_and_execute",
    debt_policy="record_and_continue",soft_late_threshold_ns_exclusive=1_000_000,
    schedule_policy="fixed_phase_epoch_offsets_no_rebase")


def _profile(value):
    return (type(value) is dict and value.keys() == ADMISSION_PROFILE.keys()
            and all(type(value[key]) is type(wanted) and value[key] == wanted
                    for key, wanted in ADMISSION_PROFILE.items()))


def _owner_profile(profile):
    if type(profile) is not dict or not _profile(profile.get("admission_profile")):
        return False
    if not all(type(profile.get(key)) is int and profile[key] == value for key, value in
               (("rate_per_second", 500), ("callers", 8), ("payload_bytes", 4096))):
        return False
    counts = tuple(profile.get(key) for key in ("warm_calls_per_caller", "measure_calls_per_caller"))
    return all(type(value) is int for value in counts) and counts in ((16, 32), (512, 4096))


def _counts(result):
    return Counter(result.get("error_counts", {key: 1 for key in result.get("errors", [])}))


def _finish(result, errors):
    return {**result, "valid": not errors, "errors": list(errors), "error_counts": dict(errors),
            "RPC30_accepted": False, "full_goal_achieved": False, "performance_eligible": False}


def _sliding_max(values, width):
    left = maximum = 0
    for right, stamp in enumerate(values):
        while stamp - values[left] >= width:
            left += 1
        maximum = max(maximum, right-left+1)
    return maximum


GROUPS = {
    "sdk_api": lambda r:r["api_end_ns"]-r["kv_call_begin_ns"],
    "fifo_head_delay": lambda r:max(0,r["head_enter_ns"]-r["scheduled_ns"]),
    "pacing_wait": lambda r:r["api_begin_ns"]-r["head_enter_ns"],
    "head_ready_lag": lambda r:r["api_begin_ns"]-max(r["scheduled_ns"],r["head_enter_ns"]),
    "pre_call_setup": lambda r:r["kv_call_begin_ns"]-r["api_begin_ns"],
    "validation": lambda r:r["validation_end_ns"]-r["api_end_ns"],
    "observer_tail": lambda r:r["observer_end_ns"]-r["validation_end_ns"],
    "observed_e2e": lambda r:r["observer_end_ns"]-r["scheduled_ns"]}


def _distribution(values):
    values=sorted(values)
    if any(not legacy._integer(value)for value in values):raise ValueError("nonmonotonic duration")
    total=0;cdf=[]
    for value,count in sorted(Counter(values).items()):
        total+=count;cdf.append([value,total])
    return {**legacy.nearest_rank_ns(values),**nearest_rank_sensitivity_ns(values),"cdf_ns":cdf}


def _occupancy(spans,begin,end):
    events=Counter({begin:0,end:0})
    for a,b in spans:
        if a<b:events[a]+=1;events[b]-=1
    active=maximum=busy=0;prior=begin;changes=[];durations=Counter()
    for stamp,delta in sorted(events.items()):
        elapsed=stamp-prior;durations[active]+=elapsed
        if active:busy+=elapsed
        active+=delta;maximum=max(maximum,active);changes.append([stamp,active]);prior=stamp
        if active<0:raise ValueError("occupancy underflow")
    if active:raise ValueError("occupancy did not drain")
    return dict(maximum=maximum,busy_ns=busy,changes=changes,duration_by_depth_ns=sorted(durations.items()),
                convention="half-open; tied endpoints coalesced; zero-length calls have no occupancy")


def _audit(rows,epoch,duration):
    end=max(epoch+duration,max(row["observer_end_ns"]for row in rows))
    planned=sorted(row["scheduled_ns"]for row in rows)
    admitted=sorted(row["api_begin_ns"]for row in rows)
    sdk=sorted(row["kv_call_begin_ns"]for row in rows)
    buckets={}
    for width in (1_000_000,10_000_000,100_000_000):
        count=max((end-epoch+width-1)//width,(admitted[-1]-epoch)//width+1)
        if count>180_001:raise ValueError("audit exceeds existing180s workflow ceiling")
        values={"scheduled_counts":[0]*count,"admitted_counts":[0]*count,"sdk_call_counts":[0]*count}
        for name,stamps in (("scheduled_counts",planned),("admitted_counts",admitted),("sdk_call_counts",sdk)):
            for stamp in stamps:values[name][(stamp-epoch)//width]+=1
        buckets[str(width)]=dict(origin_ns=epoch,width_ns=width,**values,
            max_sliding_scheduled=_sliding_max(planned,width),max_sliding_admitted=_sliding_max(admitted,width))
    gaps=[]
    for owner in range(8):
        owner_rows=sorted((row for row in rows if row["caller"]==owner),key=lambda r:r["arrival_id"])
        gaps.extend(b["scheduled_ns"]-a["observer_end_ns"]for a,b in zip(owner_rows,owner_rows[1:]))
    distributions={name:_distribution([fn(row)for row in rows])for name,fn in GROUPS.items()}
    for name,fn in (("legacy_api",lambda r:r["api_end_ns"]-r["api_begin_ns"]),
                    ("scheduled_e2e",lambda r:r["api_end_ns"]-r["scheduled_ns"]),
                    ("dispatch",lambda r:r["api_begin_ns"]-r["scheduled_ns"])):
        distributions[name]=_distribution([fn(row)for row in rows])
    return dict(full_cohort_count=len(rows),queue_equations_verified_rows=len(rows),distributions=distributions,
        minimum_signed_previous_gap_ns=min(gaps)if gaps else None,
        signed_previous_gap_cdf_ns=_signed_cdf(gaps),
        inter_API_begin_gaps=_distribution([b-a for a,b in zip(admitted,admitted[1:])]),
        inter_SDK_call_begin_gaps=_distribution([b-a for a,b in zip(sdk,sdk[1:])]),
        API_concurrency=_occupancy([(r["api_begin_ns"],r["api_end_ns"])for r in rows],epoch,end),
        SDK_concurrency=_occupancy([(r["kv_call_begin_ns"],r["api_end_ns"])for r in rows],epoch,end),
        logical_pending_depth=_occupancy([(r["scheduled_ns"],r["observer_end_ns"])for r in rows],epoch,end),
        drain_api_ns=max(0,max(r["api_end_ns"]for r in rows)-(epoch+duration)),
        drain_observer_ns=max(0,max(r["observer_end_ns"]for r in rows)-(epoch+duration)),
        buckets=buckets,bucket_convention="epoch-relative half-open, zero buckets and observer-drain tail retained",
        same_planned_trace=True,same_instantaneous_offered=False,
        scope="owner-head delay may include scheduling; pre_call_setup includes CaptureBegin and driver, not pure capture")


def _signed_cdf(values):
    total=0;result=[]
    for value,count in sorted(Counter(values).items()):total+=count;result.append([value,total])
    return result


def validate_arrivals(metric,ledger,operation,*,smoke=False):
    try:result=legacy.validate_arrivals(metric,ledger,operation,smoke=smoke)
    except (KeyError,ValueError,TypeError,ArithmeticError):return _finish({},Counter(queued_input_shape=1))
    errors=_counts(result)
    if not isinstance(metric,dict)or not isinstance(ledger,list):return _finish(result,errors)
    if not _profile(metric.get("admission_profile")):errors["queued_admission_profile"]+=1
    if metric.get("late_and_debt_are_observations")is not True:errors["queued_observation_flag"]+=1
    if metric.get("fixed_planned_offered_trace")is not True or metric.get("same_instantaneous_RPC_send_trace_claimed")is not False or "fixed_offered_load"in metric:errors["queued_offered_trace_semantics"]+=1
    phase_rows=metric.get("phases")
    phases={r["phase_id"]:r for r in (phase_rows if isinstance(phase_rows,list)else[])if isinstance(r,dict)and type(r.get("phase_id"))is int}
    for phase_id in (1,2):
        phase=phases.get(phase_id)
        if phase is None:continue
        rows=[r for r in ledger if isinstance(r,dict)and r.get("phase_id")==phase_id]
        late=debt=0;previous={};last_id={}
        for row in rows:
            owner=row.get("caller");arrival=row.get("arrival_id")
            if type(owner)is not int or not 0<=owner<8 or type(arrival)is not int:continue
            S,H,B,K,E,V,O=(row.get(k)for k in ("scheduled_ns","head_enter_ns","api_begin_ns","kv_call_begin_ns","api_end_ns","validation_end_ns","observer_end_ns"))
            if owner in last_id and arrival<=last_id[owner]:errors["queued_FIFO_ID_order"]+=1
            last_id[owner]=arrival
            scheduled=row.get("scheduled_ns");checked=row.get("dispatch_checked_ns");observed=row.get("observer_end_ns")
            if legacy._integer(scheduled)and legacy._integer(checked,1):late+=checked>=scheduled and checked-scheduled>1_000_000
            if row.get("attempted")is True:debt+=not(legacy._integer(scheduled)and legacy._integer(observed)and scheduled<=observed<scheduled+16_000_000)
            if row.get("state")!="returned":continue
            if not all(legacy._integer(x)for x in (S,H,B,K,E,V,O))or not H<=B<=K<=E<=V<=O or S>B:
                errors["queued_timeline"]+=1;continue
            if not legacy._integer(phase.get("epoch_created_ns"))or H<phase["epoch_created_ns"]:errors["queued_head_before_phase"]+=1
            if row.get("dispatch_checked_ns")!=B or type(row.get("dispatch_checked_ns"))is not int:errors["queued_checked_begin_identity"]+=1
            if owner in previous and previous[owner]>H:errors["queued_owner_overlap"]+=1
            previous[owner]=O
            if B-S!=max(0,H-S)+B-max(S,H):errors["queued_queue_equation"]+=1
            if O-S!=max(0,H-S)+B-max(S,H)+(K-B)+(E-K)+(V-E)+(O-V):errors["queued_E2E_equation"]+=1
        for name,value in (("soft_late_count",late),("debt_violations",debt)):
            if type(phase.get(name))is not int or phase[name]!=value:errors["queued_"+name+"_counter"]+=1
        if type(phase.get("soft_late_budget_ok"))is not bool or phase["soft_late_budget_ok"]!=(1000*late<=phase.get("planned",0)):
            errors["queued_soft_budget_counter"]+=1
        full=len(rows)==phase.get("planned")and all(r.get("state")=="returned"and r.get("status")==0 and r.get("attempted")is True and r.get("mismatch")is False for r in rows)
        if type(phase.get("statistics_full_cohort"))is not bool or phase["statistics_full_cohort"]is not full:errors["queued_statistics_cohort"]+=1
        if full and not errors:
            for name,fn in GROUPS.items():
                values=[fn(r)for r in rows];stats=legacy.nearest_rank_ns(values)
                for q in ("p50_ns","p99_ns"):
                    key=name+"_"+q
                    if type(phase.get(key))is not int or phase[key]!=stats[q]:errors["queued_quantile_"+name]+=1
            for name,field in (("drain_api_ns","api_end_ns"),("drain_observer_ns","observer_end_ns")):
                expected=max(0,max(r[field]for r in rows)-phase["planned_end_ns"])
                if type(phase.get(name))is not int or phase[name]!=expected:errors["queued_"+name]+=1
    result=copy.deepcopy(result)
    result["profile"]={**result.get("profile",{}),"admission_profile":copy.deepcopy(metric.get("admission_profile"))}
    if not errors:
        try:
            for phase_id,phase in phases.items():
                rows=[r for r in ledger if r["phase_id"]==phase_id]
                result["phases"][str(phase_id)]["queued"]=_audit(rows,phase["epoch_ns"],phase["planned_duration_ns"])
        except (KeyError,ValueError,TypeError,ArithmeticError):errors["queued_audit_shape_or_resource_bound"]+=1
    if errors:
        result["phases"]={key:{name:value for name,value in phase.items()if name in ("count","phase_started","state_counts")}
                          for key,phase in result.get("phases",{}).items()}
    result.update(complete_cohort_distributions=not errors,same_planned_trace=True,same_instantaneous_offered=False,
                  observer_calibration_qualified=False)
    return _finish(result,errors)


def nearest_rank_sensitivity_ns(values):
    values = sorted(values)
    if any(not legacy._integer(value) for value in values):
        raise ValueError("duration must be a nonnegative signed-clock integer")
    n = len(values)
    return dict(count=n, p989_ns=values[(n*989+999)//1000-1] if n else None,
                p991_ns=values[(n*991+999)//1000-1] if n else None)


def validate_rpc_rows(arrivals, ledger, ends, rpcs, slots, operation):
    try:
        result = legacy.validate_rpc_rows(arrivals, ledger, ends, rpcs, slots, operation)
    except (KeyError, ValueError, TypeError, ArithmeticError):
        return {**_finish({}, Counter(bounded_RPC_input_shape=1)), "rpc_valid": False}
    errors = _counts(result)
    profile = arrivals.get("profile") if isinstance(arrivals, dict) else None
    if not _owner_profile(profile):
        errors["queued_admission_profile"] += 1
    if not errors:
        groups = {}
        for row in rpcs:
            key = tuple(row[field] for field in ("phase_id", "arrival_id", "caller_index"))
            groups.setdefault(key, []).append(row)
        primary = 1 if operation == "get" else 4
        for phase in (1, 2):
            spans, sums = [], []
            for key, group in groups.items():
                if key[0] == phase:
                    spans.extend(row["recv_ns"]-row["send_ns"] for row in group if row["method"] == primary)
                    sums.append(sum(row["recv_ns"]-row["send_ns"] for row in group))
            for name, values in (("primary_rpc", spans), ("per_api_rpc_sum", sums)):
                result["phases"][str(phase)][name].update(_distribution(values))
            result["phases"][str(phase)]["per_api_rpc_union"]=_distribution(sums)
            sends=sorted(row["send_ns"]for row in rpcs if row["phase_id"]==phase)
            result["phases"][str(phase)]["inter_RPC_send_gaps"]=_distribution([b-a for a,b in zip(sends,sends[1:])])
            epoch=result["phases"][str(phase)]["epoch_ns"]
            timeline_end=max(row["observer_end_ns"]for row in ledger if row["phase_id"]==phase)
            timeline_end=max(timeline_end,result["phases"][str(phase)]["planned_end_ns"])
            actual=[row for row in rpcs if row["phase_id"]==phase]
            result["phases"][str(phase)]["RPC_concurrency"]=_occupancy([(r["send_ns"],r["recv_ns"])for r in actual],epoch,timeline_end)
            rpc_buckets={}
            for width in (1_000_000,10_000_000,100_000_000):
                count=max((timeline_end-epoch+width-1)//width,(sends[-1]-epoch)//width+1)
                if count>180_001:raise ValueError("RPC audit exceeds existing180s workflow ceiling")
                values=[0]*count
                for stamp in sends:values[(stamp-epoch)//width]+=1
                rpc_buckets[str(width)]=dict(origin_ns=epoch,width_ns=width,send_counts=values,max_sliding_send=_sliding_max(sends,width))
            result["phases"][str(phase)]["RPC_send_buckets"]=rpc_buckets
        indexed={(r["phase_id"],r["arrival_id"],r["caller"]):r for r in ledger}
        for row in rpcs:
            api=indexed[row["phase_id"],row["arrival_id"],row["caller_index"]]
            if not api["kv_call_begin_ns"]<=row["send_ns"]<=row["recv_ns"]<=api["api_end_ns"]:errors["queued_RPC_callsite_endpoints"]+=1
    if errors:
        for phase in result.get("phases",{}).values():
            for key in ("primary_rpc","per_api_rpc_sum","per_api_rpc_union","inter_RPC_send_gaps","RPC_concurrency","RPC_send_buckets"):
                phase.pop(key,None)
    return {**_finish(result, errors), "rpc_valid": not errors,"rpc_distributions_complete":not errors}


def _decide(quartet, calibration):
    decision = legacy.decide_calibration if calibration else legacy.decide_backend_comparison
    result = decision(quartet)
    if (not isinstance(quartet, (list, tuple)) or len(quartet) != 4
            or any(not isinstance(row, dict) or not _owner_profile(row.get("profile")) for row in quartet)):
        return {**result, "pass": False, "errors": [*result["errors"], "queued_admission_profile"]}
    if calibration or result["errors"]:
        return result
    sensitivity = dict(directional=[], medians={},
                       interpretation="preregistered rank sensitivity, not proven contamination correction")
    try:
        phases = [row["phases"]["2"] for row in quartet]
        for phase in phases:
            for name in ("primary_rpc", "per_api_rpc_sum"):
                stats = phase[name]
                if (not legacy._integer(stats.get("count"), 1)
                        or not all(legacy._integer(stats[key]) for key in ("p50_ns", "p989_ns", "p99_ns", "p991_ns"))
                        or not stats["p50_ns"] <= stats["p989_ns"] <= stats["p99_ns"] <= stats["p991_ns"]):
                    raise ValueError("invalid exact rank inputs")
        for label, candidate, control in (("forward", phases[1], phases[0]), ("reverse", phases[2], phases[3])):
            metrics = {name: legacy._ratio(candidate[name]["p991_ns"], control[name]["p989_ns"], None, .70)
                       for name in ("primary_rpc", "per_api_rpc_sum")}
            sensitivity["directional"].append(dict(kind=label, metrics=metrics,
                                                   **{"pass": all(value["pass"] for value in metrics.values())}))
        for name in ("primary_rpc", "per_api_rpc_sum"):
            candidate = statistics.median(Decimal(phases[i][name]["p991_ns"]) for i in (1, 2))
            control = statistics.median(Decimal(phases[i][name]["p989_ns"]) for i in (0, 3))
            sensitivity["medians"][name] = legacy._ratio(candidate, control, None, .70)
        result["pass"] &= (all(row["pass"] for row in sensitivity["directional"])
                           and all(row["pass"] for row in sensitivity["medians"].values()))
    except (KeyError, ValueError, TypeError, ArithmeticError):
        result["pass"] = False
        result["errors"].append("unknown_or_invalid_rank_sensitivity_input")
    return {**result, "rank_sensitivity": sensitivity}


def decide_calibration(quartet):
    return {**_decide(quartet,True),"performance_eligible":False,"observer_calibration_qualified":False}


def decide_backend_comparison(quartet):
    return {**_decide(quartet,False),"performance_eligible":False,"observer_calibration_qualified":False,
            "same_planned_trace":True,"same_instantaneous_offered":False,
            "scope":"PURE_QUEUED_WORKLOAD_NUMERIC_REPORT_NOT_PRODUCTION_QUALIFICATION"}
