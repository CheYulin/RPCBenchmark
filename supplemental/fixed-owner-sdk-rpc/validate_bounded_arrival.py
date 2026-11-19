"""Distinct bounded-admission contract over the unchanged full-cohort validator."""
from collections import Counter
import copy
from decimal import Decimal
import statistics

import validate_fixed_arrival as legacy

ADMISSION_PROFILE = dict(
    protocol="fixed-owner-bounded-admission-v1", owner_period_ns=64_000_000,
    hard_dispatch_lateness_ns_max_inclusive=63_999_999,
    soft_late_threshold_ns_exclusive=1_000_000,
    soft_late_budget_numerator=1, soft_late_budget_denominator=1000,
    no_debt_policy="observer_end_strictly_before_scheduled_plus_owner_period_including_last")


def _profile(value):
    return (type(value) is dict and value.keys() == ADMISSION_PROFILE.keys()
            and all(type(value[key]) is type(wanted) and value[key] == wanted
                    for key, wanted in ADMISSION_PROFILE.items()))


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


def _audit(rows, epoch, duration):
    dispatch = sorted(row["api_begin_ns"] - row["scheduled_ns"] for row in rows)
    cdf, total = [], 0
    for value, count in sorted(Counter(dispatch).items()):
        total += count
        cdf.append([value, total])
    planned = sorted(row["scheduled_ns"] for row in rows)
    admitted = sorted(row["api_begin_ns"] for row in rows)
    buckets = {}
    for width in (1_000_000, 10_000_000, 100_000_000):
        count = max((duration+width-1)//width, (admitted[-1]-epoch)//width+1)
        scheduled_counts, admitted_counts = [0]*count, [0]*count
        for stamp in planned:
            scheduled_counts[(stamp-epoch)//width] += 1
        for stamp in admitted:
            admitted_counts[(stamp-epoch)//width] += 1
        buckets[str(width)] = dict(origin_ns=epoch, width_ns=width,
                                  scheduled_counts=scheduled_counts, admitted_counts=admitted_counts,
                                  max_sliding_scheduled=_sliding_max(planned, width),
                                  max_sliding_admitted=_sliding_max(admitted, width))
    return dict(full_cohort_count=len(rows), dispatch_cdf_ns=cdf,
                maximum_dispatch_lateness_ns=dispatch[-1],
                soft_late_count=sum(value > 1_000_000 for value in dispatch),
                minimum_observer_margin_ns=min(row["scheduled_ns"]+64_000_000-row["observer_end_ns"]
                                               for row in rows),
                bucket_convention="epoch-relative half-open; zero buckets and last-admission tail retained",
                sliding_algorithm="sorted two-pointer maximum half-open window, version1",
                buckets=buckets)


def validate_arrivals(metric, ledger, operation, *, smoke=False):
    try:
        result = legacy.validate_arrivals(metric, ledger, operation, smoke=smoke)
    except (KeyError, ValueError, TypeError, ArithmeticError):
        return _finish({}, Counter(bounded_input_shape=1))
    errors = _counts(result)
    errors.pop("dispatch_budget", None)
    if not isinstance(metric, dict) or not isinstance(ledger, list):
        return _finish(result, errors)
    if not _profile(metric.get("admission_profile")):
        errors["bounded_admission_profile"] += 1
    phase_rows = metric.get("phases")
    phases = {row["phase_id"]: row for row in (phase_rows if isinstance(phase_rows, list) else [])
              if isinstance(row, dict) and type(row.get("phase_id")) is int}
    counts = {1: 128 if smoke else 4096, 2: 256 if smoke else 32768}
    for phase_id, count in counts.items():
        phase = phases.get(phase_id)
        if phase is None:
            continue
        late = debt = 0
        for row in ledger:
            if not isinstance(row, dict) or row.get("phase_id") != phase_id:
                continue
            scheduled, checked, begin, observer = (row.get(key) for key in
                ("scheduled_ns", "dispatch_checked_ns", "api_begin_ns", "observer_end_ns"))
            if legacy._integer(scheduled) and legacy._integer(checked, 1):
                late += checked >= scheduled and checked-scheduled > 1_000_000
                if not 0 <= checked-scheduled <= 63_999_999:
                    errors["bounded_dispatch_budget"] += 1
            if row.get("attempted") is True:
                if legacy._integer(checked) and legacy._integer(begin) and checked != begin:
                    errors["bounded_checked_begin_identity"] += 1
                if legacy._integer(scheduled) and legacy._integer(observer):
                    debt += not scheduled <= observer < scheduled+64_000_000
        budget_ok = 1000*late <= count
        if not budget_ok:
            errors["bounded_soft_late_budget"] += 1
        if debt:
            errors["bounded_observer_debt"] += debt
        for key, value in (("soft_late_count", late), ("debt_violations", debt)):
            if type(phase.get(key)) is not int or phase[key] != value:
                errors["bounded_"+key+"_counter"] += 1
        advertised_budget_ok = phase.get("phase_started") is True and budget_ok
        if (type(phase.get("soft_late_budget_ok")) is not bool
                or phase["soft_late_budget_ok"] is not advertised_budget_ok):
            errors["bounded_soft_late_budget_counter"] += 1
    result = copy.deepcopy(result)
    result["profile"] = {**result.get("profile", {}),
                         "admission_profile": copy.deepcopy(metric.get("admission_profile"))}
    if not errors:
        for phase_id, phase in phases.items():
            rows = [row for row in ledger if row["phase_id"] == phase_id]
            result["phases"][str(phase_id)]["admission"] = _audit(
                rows, phase["epoch_ns"], phase["planned_duration_ns"])
    return _finish(result, errors)


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
    if not isinstance(profile, dict) or not _profile(profile.get("admission_profile")):
        errors["bounded_admission_profile"] += 1
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
                result["phases"][str(phase)][name].update(nearest_rank_sensitivity_ns(values))
    return {**_finish(result, errors), "rpc_valid": not errors}


def _decide(quartet, calibration):
    decision = legacy.decide_calibration if calibration else legacy.decide_backend_comparison
    result = decision(quartet)
    if (not isinstance(quartet, (list, tuple)) or len(quartet) != 4
            or any(not isinstance(row, dict) or not isinstance(row.get("profile"), dict)
                   or not _profile(row["profile"].get("admission_profile")) for row in quartet)):
        return {**result, "pass": False, "errors": [*result["errors"], "bounded_admission_profile"]}
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
    return _decide(quartet, True)


def decide_backend_comparison(quartet):
    return _decide(quartet, False)
