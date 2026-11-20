"""Distinct queued geometry; clock/ID/CPU/RPC rules retain their original checks."""
from decimal import Decimal
import math
import statistics
from collections import Counter

SCOPE = "OWNER8_QUEUED_ARRIVAL_DIAGNOSTIC_NOT_OVERALL_PROMOTION"
MAX_NS = (1 << 63) - 1
RATE, CALLERS, LEAD_NS, MAX_LATENESS_NS = 500, 8, 250_000_000, 15_999_999
VALID_STATE = "returned"
STATES = {"planned", "returned", "rejected_early", "rejected_late", "cancelled_window",
          "execution_exception", "invalid_time", "unattempted_prior_phase"}


class _Errors:
    def __init__(self):
        self.counts = Counter()

    def check(self, condition, code):
        if not condition:
            self.counts[code] += 1
        return bool(condition)

    def result(self):
        return {"valid": not self.counts, "errors": list(self.counts), "error_counts": dict(self.counts)}


def _integer(value, minimum=0, maximum=MAX_NS):
    return type(value) is int and minimum <= value <= maximum


def _finite(value, positive=False):
    return type(value) in (int, float, Decimal) and math.isfinite(value) and (value > 0 if positive else value >= 0)


def _close(actual, expected):
    return _finite(actual) and _finite(expected) and math.isclose(actual, expected, rel_tol=1e-10, abs_tol=1e-9)


def nearest_rank_ns(values):
    """Exact nearest-rank integer nanoseconds, including zero durations."""
    values = sorted(values)
    if any(type(value) is not int or not 0 <= value <= MAX_NS for value in values):
        raise ValueError("duration must be a nonnegative signed-clock integer")
    count = len(values)
    return {"count": count, "p50_ns": values[(count * 50 + 99) // 100 - 1] if count else None,
            "p99_ns": values[(count * 99 + 99) // 100 - 1] if count else None}


def validate_arrivals(metric, ledger, operation, *, smoke=False):
    """Validate every full-ID row; return valid/errors and derived phase stats."""
    errors = _Errors()
    if not isinstance(metric, dict) or not isinstance(ledger, list):
        return {"valid": False, "errors": ["input_shape"], "phases": {}, "operation": operation}
    counts = {1: 16 if smoke else 512, 2: 32 if smoke else 4096}
    profile = {"rate_per_second": RATE, "callers": CALLERS, "payload_bytes": 4096,
               "warm_calls_per_caller": counts[1], "measure_calls_per_caller": counts[2]}
    errors.check(operation in ("get", "set"), "operation")
    errors.check(metric.get("operation") == operation, "metric_operation")
    backend = metric.get("backend")
    errors.check(backend in ("brpc", "coro"), "backend")
    errors.check(metric.get("scope") == SCOPE, "scope")
    for key, expected in profile.items():
        errors.check(type(metric.get(key)) is int and metric[key] == expected, "profile_" + key)
    for key in ("sdk_pid", "worker_pid", "clock_ticks_per_second"):
        errors.check(_integer(metric.get(key), 1), "identity_" + key)
    errors.check(metric.get("sdk_pid") != metric.get("worker_pid"), "role_pid_alias")
    mode = metric.get("capture_mode")
    errors.check(mode in ("off", "enabled"), "capture_mode")
    errors.check(metric.get("rpc_capture_available") is (mode == "enabled"), "capture_availability")
    for key in ("sdk_shutdown_ok", "capture_close_ok", "quality_valid"):
        errors.check(metric.get(key) is True, key)
    errors.check(metric.get("readback_keys") == 64 and type(metric.get("readback_keys")) is int
                 and metric.get("readback_errors") == 0 and type(metric.get("readback_errors")) is int,
                 "readback")
    phase_list = metric.get("phases")
    phases = {}
    if not isinstance(phase_list, list):
        errors.check(False, "phase_set")
        phase_list = []
    for phase in phase_list:
        if not isinstance(phase, dict) or type(phase.get("phase_id")) is not int or phase["phase_id"] not in counts:
            errors.check(False, "phase_shape")
            continue
        errors.check(phase["phase_id"] not in phases, "phase_duplicate")
        phases[phase["phase_id"]] = phase
    errors.check(set(phases) == {1, 2} and len(phase_list) == 2, "phase_set")
    errors.check(len(ledger) == sum(counts.values()) * CALLERS, "ledger_count")
    indexed, by_phase, tids = {}, {1: [], 2: []}, {}
    time_keys = ("scheduled_ns", "dispatch_checked_ns", "api_begin_ns", "api_end_ns",
                 "validation_end_ns", "observer_end_ns")
    row_keys = ("caller", "tid", "phase_id", "arrival_id", "required_methods", *time_keys, "state", "attempted",
                "status", "selected", "mismatch")
    for row in ledger:
        if not isinstance(row, dict):
            errors.check(False, "ledger_row_shape")
            continue
        errors.check(all(key in row for key in row_keys), "ledger_fields")
        phase, arrival, caller = row.get("phase_id"), row.get("arrival_id"), row.get("caller")
        key_ok = (_integer(phase, 1, 2) and _integer(caller, 0, CALLERS - 1)
                  and _integer(arrival, 0, counts.get(phase, 0) * CALLERS - 1))
        if not errors.check(key_ok, "ledger_identity"):
            continue
        errors.check(arrival % CALLERS == caller and arrival // CALLERS < counts[phase], "ledger_owner")
        key = (phase, arrival)
        errors.check(key not in indexed, "ledger_duplicate")
        indexed[key] = row
        by_phase[phase].append(row)
        tid = row.get("tid")
        if errors.check(_integer(tid, 1), "ledger_tid"):
            errors.check(caller not in tids or tids[caller] == tid, "caller_tid_changed")
            tids.setdefault(caller, tid)
        state = row.get("state")
        errors.check(state in STATES, "ledger_unknown_state")
        errors.check(state == VALID_STATE, "ledger_not_returned")
        errors.check(type(row.get("attempted")) is bool and type(row.get("mismatch")) is bool,
                     "ledger_boolean")
        errors.check(row.get("selected") is True, "ledger_selector")
        errors.check(type(row.get("required_methods")) is int
                     and row["required_methods"] == (2 if operation == "get" else 16), "required_methods")
        for key_name in time_keys:
            errors.check(row.get(key_name) is None or _integer(row.get(key_name)), "ledger_time_type")
        if state == VALID_STATE:
            errors.check(row.get("attempted") is True and type(row.get("status")) is int, "returned_attempt")
            errors.check(row.get("status") == 0 and row.get("mismatch") is False, "API_outcome")
        elif state in ("cancelled_window", "unattempted_prior_phase", "planned", "rejected_early", "rejected_late"):
            errors.check(row.get("attempted") is False and row.get("status") is None, "unattempted_outcome")
            errors.check(all(row.get(key_name) is None for key_name in time_keys[2:]), "unattempted_API_times")
    errors.check(set(tids) == set(range(CALLERS)) and len(set(tids.values())) == CALLERS, "caller_tid_set")
    expected_keys = {(phase, arrival) for phase, per_owner in counts.items()
                     for arrival in range(per_owner * CALLERS)}
    errors.check(set(indexed) == expected_keys, "ledger_coverage")
    derived = {}
    for phase_id, per_owner in counts.items():
        phase = phases.get(phase_id)
        if phase is None:
            continue
        rows = by_phase[phase_id]
        n = per_owner * CALLERS
        duration = (n * 1_000_000_000 + RATE - 1) // RATE
        errors.check(type(phase.get("planned")) is int and phase["planned"] == n, "phase_planned")
        errors.check(type(phase.get("phase_started")) is bool, "phase_started_type")
        states = Counter(row.get("state") for row in rows)
        if phase.get("phase_started") is not True:
            errors.check(False, "phase_not_started")
            errors.check(phase_id == 2 and phases.get(1, {}).get("success") != counts[1] * CALLERS,
                         "unstarted_phase_reason")
            errors.check(all(phase.get(key) is None for key in ("epoch_created_ns", "epoch_ns", "planned_end_ns")),
                         "unstarted_epoch")
            errors.check(all(row.get("state") == "unattempted_prior_phase"
                             and all(row.get(key) is None for key in time_keys) for row in rows),
                         "unstarted_rows")
            derived[str(phase_id)] = {"count": n, "phase_started": False, "state_counts": dict(states)}
            continue
        epoch, created, planned_end = phase.get("epoch_ns"), phase.get("epoch_created_ns"), phase.get("planned_end_ns")
        epoch_ok = (_integer(epoch, 1) and _integer(created, 1) and _integer(planned_end, 1)
                    and epoch == created + LEAD_NS and epoch + duration <= MAX_NS
                    and planned_end == epoch + duration)
        errors.check(epoch_ok, "phase_epoch")
        errors.check(type(phase.get("planned_duration_ns")) is int and phase["planned_duration_ns"] == duration,
                     "planned_duration")
        errors.check(_close(phase.get("offered_rate"), n * 1_000_000_000 / duration), "offered_rate")
        distributions = {key: [] for key in ("service", "e2e", "dispatch", "observer")}
        attempts = sum(row.get("attempted") is True for row in rows)
        successful = sum(row.get("state") == VALID_STATE and row.get("attempted") is True
                         and type(row.get("status")) is int and row["status"] == 0
                         and row.get("mismatch") is False for row in rows)
        errors.check(type(phase.get("attempts")) is int and phase["attempts"] == attempts, "phase_attempts")
        errors.check(type(phase.get("success")) is int and phase["success"] == successful, "phase_success")
        errors.check(attempts == successful == n and len(rows) == n, "phase_complete")
        for key in ("errors", "mismatches", "cancelled", "rejected_early", "rejected_late", "exceptions"):
            errors.check(type(phase.get(key)) is int and phase[key] == 0, "phase_" + key)
        valid_rows = []
        for row in rows:
            scheduled = epoch + row["arrival_id"] * 1_000_000_000 // RATE if epoch_ok else None
            errors.check(epoch_ok and row.get("scheduled_ns") == scheduled, "arrival_schedule")
            if row.get("state") == "rejected_early":
                errors.check(_integer(row.get("dispatch_checked_ns")) and scheduled is not None
                             and row["dispatch_checked_ns"] < scheduled, "early_reason")
            if row.get("state") == "rejected_late":
                errors.check(_integer(row.get("dispatch_checked_ns")) and scheduled is not None
                             and row["dispatch_checked_ns"] - scheduled > MAX_LATENESS_NS, "late_reason")
            if row.get("state") != VALID_STATE:
                continue
            values = [row.get(key) for key in time_keys]
            ordered = all(_integer(value) for value in values) and values == sorted(values)
            if not errors.check(ordered and scheduled is not None, "arrival_timeline"):
                continue
            scheduled_row, checked, begin, end, validated, observer_end = values
            distributions["service"].append(end - begin)
            distributions["e2e"].append(end - scheduled_row)
            distributions["dispatch"].append(begin - scheduled_row)
            distributions["observer"].append(observer_end - begin)
            valid_rows.append(row)
        stats = {key: nearest_rank_ns(values) for key, values in distributions.items()}
        errors.check(all(value["count"] == n for value in stats.values()), "distribution_coverage")
        for prefix, name in (("api", "service"), ("e2e", "e2e"), ("dispatch", "dispatch"), ("observer", "observer")):
            for quantile in ("p50_ns", "p99_ns"):
                errors.check(type(phase.get(prefix + "_" + quantile)) is int
                             and phase[prefix + "_" + quantile] == stats[name][quantile], "quantile_" + prefix)
        last_api = max((row["api_end_ns"] for row in valid_rows), default=None)
        first_api = min((row["api_begin_ns"] for row in valid_rows), default=None)
        last_observer = max((row["observer_end_ns"] for row in valid_rows), default=None)
        max_lateness = max(distributions["dispatch"], default=None)
        errors.check(type(phase.get("max_dispatch_lateness_ns")) is int
                     and phase["max_dispatch_lateness_ns"] == max_lateness, "max_dispatch_lateness")
        completion_end = max(planned_end, last_api) if epoch_ok and last_api is not None else None
        qps = successful * 1_000_000_000 / (completion_end - epoch) if completion_end is not None else None
        errors.check(phase.get("completion_end_ns") == completion_end and completion_end is not None,
                     "completion_cohort_end")
        errors.check(_close(phase.get("qps"), qps), "completion_cohort_qps")
        wanted_bytes = successful * 4096 if operation == "get" else 0
        errors.check(type(phase.get("returned_bytes")) is int and type(phase.get("validated_bytes")) is int
                     and phase["returned_bytes"] == phase["validated_bytes"] == wanted_bytes, "byte_conservation")
        before, after = phase.get("cpu_before"), phase.get("cpu_after")
        cpu = None
        if isinstance(before, dict) and isinstance(after, dict):
            time_valid = all(_integer(sample.get(key)) for sample in (before, after)
                             for key in ("read_begin_ns", "read_end_ns"))
            time_valid = time_valid and (before["read_begin_ns"] <= before["read_end_ns"] <= after["read_begin_ns"] <= after["read_end_ns"])
            bracket = (time_valid and first_api is not None and last_observer is not None
                       and before["read_end_ns"] <= first_api and last_observer <= after["read_begin_ns"]
                       and 0 <= first_api - before["read_begin_ns"] <= 300_000_000
                       and 0 <= after["read_end_ns"] - last_api <= 50_000_000)
            errors.check(bracket, "cpu_brackets")
            ticks_valid = all(_integer(sample.get(key), 0, (1 << 64) - 1) for sample in (before, after)
                              for key in ("sdk_ticks", "worker_ticks"))
            ticks_valid = ticks_valid and all(after[key] >= before[key] for key in ("sdk_ticks", "worker_ticks"))
            errors.check(ticks_valid, "cpu_ticks")
            hz = metric.get("clock_ticks_per_second")
            if ticks_valid and _integer(hz, 1) and successful:
                cpu = sum(after[key] - before[key] for key in ("sdk_ticks", "worker_ticks")) * 1_000_000 / hz / successful
        else:
            errors.check(False, "cpu_sample")
        errors.check(_close(phase.get("CPU_us_per_success"), cpu), "cpu_denominator")
        derived[str(phase_id)] = {"count": n, "phase_started": True, "state_counts": dict(states), **stats,
                                  "CPU_us_per_success": cpu, "qps": qps, "epoch_ns": epoch,
                                  "planned_end_ns": planned_end, "completion_end_ns": completion_end}
    if "1" in derived and "2" in derived and derived["2"].get("phase_started"):
        first, second = phases.get(1, {}), phases.get(2, {})
        if isinstance(first.get("cpu_after"), dict):
            errors.check(_integer(second.get("epoch_created_ns")) and _integer(first["cpu_after"].get("read_end_ns"))
                         and first["cpu_after"]["read_end_ns"] <= second["epoch_created_ns"], "phase_cpu_order")
    return {**errors.result(), "phases": derived, "operation": operation, "capture_mode": mode,
            "backend": backend, "profile": profile, "input_rows": len(ledger), "RPC30_accepted": False}


def validate_rpc_rows(arrivals, ledger, ends, rpcs, slots, operation):
    """Join decoded ABI2 rows without filtering anomalies or optional metadata."""
    errors = _Errors()
    if not isinstance(arrivals, dict) or not all(isinstance(value, list) for value in (ledger, ends, rpcs, slots)):
        return {"valid": False, "rpc_valid": False, "errors": ["RPC_input_shape"], "operation": operation}
    errors.counts.update(arrivals.get("error_counts", {}))
    errors.check(arrivals.get("valid") is True, "arrival_invalid")
    errors.check(arrivals.get("operation") == operation and operation in ("get", "set"), "RPC_operation")
    errors.check(arrivals.get("capture_mode") == "enabled", "RPC_capture_not_enabled")
    primary = 1 if operation == "get" else 4
    required = 1 << primary
    expected, tids = {}, {}
    for row in ledger:
        if not isinstance(row, dict):
            errors.check(False, "RPC_ledger_shape")
            continue
        key = tuple(row.get(field) for field in ("phase_id", "arrival_id", "caller"))
        if not all(type(value) is int for value in key):
            errors.check(False, "RPC_ledger_key")
            continue
        errors.check(key not in expected, "RPC_ledger_duplicate")
        expected[key] = row
        tids.setdefault(key[2], row.get("tid"))
        errors.check(type(row.get("required_methods")) is int and row["required_methods"] == required,
                     "RPC_required_methods")
    def index(values, label, multiple=False):
        result = {}
        for row in values:
            if not isinstance(row, dict):
                errors.check(False, label + "_shape")
                continue
            key = tuple(row.get(field) for field in ("phase_id", "arrival_id", "caller_index"))
            if not all(type(value) is int for value in key):
                errors.check(False, label + "_key")
                continue
            errors.check(key in expected, label + "_unexpected")
            if multiple:
                result.setdefault(key, []).append(row)
            else:
                errors.check(key not in result, label + "_duplicate")
                result[key] = row
        return result
    end_index, groups = index(ends, "End"), index(rpcs, "RPC", True)
    errors.check(set(end_index) == set(expected) and set(groups) == set(expected), "RPC_population")
    slot_index = {}
    for slot in slots:
        if not isinstance(slot, dict) or not _integer(slot.get("caller"), 0, CALLERS - 1):
            errors.check(False, "slot_shape")
            continue
        caller = slot["caller"]
        errors.check(caller not in slot_index, "slot_duplicate")
        slot_index[caller] = slot
        errors.check(slot.get("cached_tid") == tids.get(caller), "slot_tid")
        errors.check(slot.get("counters_available") is True, "slot_counters_unavailable")
        for field in ("errors", "error_generation", "first_driver_status", "active", "api_failures", "rpc_failures"):
            errors.check(type(slot.get(field)) is int and slot[field] == 0, "slot_" + field)
        actual_apis = sum(key[2] == caller for key in expected)
        actual_rpcs = sum(len(group) for key, group in groups.items() if key[2] == caller)
        errors.check(type(slot.get("api_count")) is int and slot["api_count"] == actual_apis, "slot_api_count")
        errors.check(type(slot.get("rpc_count")) is int and slot["rpc_count"] == actual_rpcs, "slot_rpc_count")
    errors.check(set(slot_index) == set(range(CALLERS)), "slot_set")
    phase_values = {phase: {"primary_rpc": [], "per_api_rpc_sum": []} for phase in (1, 2)}
    known_flags, failure_flags = 1 | 2 | 4 | 8 | 16 | 32 | 256 | 512 | 1024 | 2048 | 4096, 2 | 4 | 8 | 16 | 32
    def flags_valid(value):
        return type(value) is int and bool(value & 1) and not value & failure_flags and not value & ~known_flags
    for key, ledger_row in expected.items():
        end, group = end_index.get(key), groups.get(key, [])
        if end is None:
            continue
        count = end.get("rpc_count")
        count_valid = _integer(count, 1, 16)
        errors.check(count_valid and len(group) == count, "RPC_count")
        ordinals = [row.get("rpc_ordinal") for row in group]
        ordinal_valid = (count_valid and all(type(value) is int for value in ordinals)
                         and len(set(ordinals)) == len(ordinals) and set(ordinals) == set(range(1, count + 1)))
        errors.check(ordinal_valid, "RPC_ordinal_prefix")
        for field in ("scheduled_ns", "api_begin_ns", "api_end_ns", "validation_end_ns"):
            errors.check(type(end.get(field)) is int and end[field] == ledger_row.get(field), "End_timestamp")
        errors.check(type(end.get("outcome")) is int and end["outcome"] == ledger_row.get("status") == 0,
                     "End_outcome")
        errors.check(type(end.get("capture_errors")) is int and end["capture_errors"] == 0, "End_capture_errors")
        errors.check(flags_valid(end.get("flags")), "End_flags")
        methods = 0
        durations = []
        primary_durations = []
        ordered_group = sorted(group, key=lambda row: row.get("rpc_ordinal", 0)) if ordinal_valid else group
        previous_receive = None
        for row in ordered_group:
            method = row.get("method")
            method_ok = _integer(method, 1, 10)
            errors.check(method_ok, "RPC_method")
            if method_ok:
                methods |= 1 << method
            errors.check(type(row.get("controller_error")) is int and type(row.get("ds_error")) is int
                         and row["controller_error"] == row["ds_error"] == 0, "RPC_outcome")
            errors.check(flags_valid(row.get("flags")), "RPC_flags")
            send, receive = row.get("send_ns"), row.get("recv_ns")
            begin, finish = ledger_row.get("api_begin_ns"), ledger_row.get("api_end_ns")
            time_ok = (all(_integer(value) for value in (begin, finish, send, receive))
                       and begin <= send <= receive <= finish)
            errors.check(time_ok, "RPC_endpoints")
            if time_ok:
                errors.check(previous_receive is None or previous_receive <= send, "RPC_overlap_or_order")
                previous_receive = receive
                durations.append(receive - send)
                if method == primary:
                    primary_durations.append(receive - send)
        errors.check(type(end.get("observed_methods")) is int and end["observed_methods"] == methods
                     and methods & required == required and bool(primary_durations), "RPC_method_conservation")
        if key[0] in phase_values and len(durations) == count and ordinal_valid:
            phase_values[key[0]]["primary_rpc"].extend(primary_durations)
            phase_values[key[0]]["per_api_rpc_sum"].append(sum(durations))
    phases = {key: dict(value) for key, value in arrivals.get("phases", {}).items()}
    for phase, values in phase_values.items():
        if str(phase) not in phases:
            errors.check(False, "RPC_phase_missing")
            continue
        wanted = sum(key[0] == phase for key in expected)
        errors.check(len(values["per_api_rpc_sum"]) == wanted and len(values["primary_rpc"]) >= wanted,
                     "RPC_phase_population")
        for name, durations in values.items():
            phases[str(phase)][name] = nearest_rank_ns(durations)
    return {**arrivals, **errors.result(), "phases": phases, "rpc_valid": not errors.counts,
            "rpc_container_qualified": False, "RPC30_accepted": False}


def decide_calibration(quartet):
    """Four validated results in off/on/on/off order for one backend/operation."""
    return _decide(quartet, calibration=True)


def decide_backend_comparison(quartet):
    """Four validated RPC results in bRPC/coro/coro/bRPC order for one operation."""
    return _decide(quartet, calibration=False)


def _decimal(value):
    if type(value) not in (int, float, Decimal):
        raise ValueError("nonnumeric")
    result = Decimal(str(value))
    if not result.is_finite() or result < 0:
        raise ValueError("nonfinite or negative")
    return result


def _ratio(numerator, denominator, minimum, maximum):
    n, d = _decimal(numerator), _decimal(denominator)
    if d <= 0:
        raise ValueError("nonpositive denominator")
    ratio = n / d
    passed = (minimum is None or n >= d * Decimal(str(minimum)))
    passed &= maximum is None or n <= d * Decimal(str(maximum))
    return {"ratio": str(ratio), "pass": passed}


def _decide(quartet, calibration):
    result = {"pass": False, "errors": [], "directional": [], "medians": {}, "control_drift": {},
              "RPC30_accepted": False, "full_goal_achieved": False,
              "scope": "PURE_NUMERIC_GATE_ONLY_EXTERNAL_RUNTIME_PROOFS_REQUIRED"}
    try:
        if not isinstance(quartet, (list, tuple)) or len(quartet) != 4:
            raise ValueError("quartet")
        if any(not isinstance(row, dict) or row.get("valid") is not True for row in quartet):
            raise ValueError("invalid input")
        if any(row.get("profile") != quartet[0].get("profile") or row.get("operation") != quartet[0].get("operation")
               for row in quartet):
            raise ValueError("profile or operation")
        backends = [row.get("backend") for row in quartet]
        if calibration:
            if backends[0] not in ("brpc", "coro") or any(backend != backends[0] for backend in backends):
                raise ValueError("calibration backend")
        elif backends != ["brpc", "coro", "coro", "brpc"]:
            raise ValueError("backend order")
        expected_modes = ["off", "enabled", "enabled", "off"] if calibration else ["enabled"] * 4
        if [row.get("capture_mode") for row in quartet] != expected_modes:
            raise ValueError("capture modes")
        if any(row.get("rpc_valid") is not True for row in (quartet[1:3] if calibration else quartet)):
            raise ValueError("RPC rows unqualified")
        phases = [row["phases"]["2"] for row in quartet]
        metrics = {"API_P50": ("service", "p50_ns"), "API_P99": ("service", "p99_ns"),
                   "E2E_P50": ("e2e", "p50_ns"), "E2E_P99": ("e2e", "p99_ns"),
                   "CPU_per_API": (None, "CPU_us_per_success"), "QPS": (None, "qps")}
        if not calibration:
            metrics.update({"primary_RPC_P50": ("primary_rpc", "p50_ns"),
                            "primary_RPC_P99": ("primary_rpc", "p99_ns"),
                            "per_API_RPC_sum_P50": ("per_api_rpc_sum", "p50_ns"),
                            "per_API_RPC_sum_P99": ("per_api_rpc_sum", "p99_ns")})
        def value(phase, name):
            group, field = metrics[name]
            return _decimal(phase[group][field] if group else phase[field])
        def bounds(name):
            if calibration:
                return .97, 1.03
            if name in ("primary_RPC_P99", "per_API_RPC_sum_P99"):
                return None, .70
            return (.97, None) if name == "QPS" else (None, 1.03)
        for label, candidate, control in (("forward", phases[1], phases[0]), ("reverse", phases[2], phases[3])):
            comparisons = {name: _ratio(value(candidate, name), value(control, name), *bounds(name)) for name in metrics}
            result["directional"].append({"kind": label, "metrics": comparisons,
                                           "pass": all(row["pass"] for row in comparisons.values())})
        for name in metrics:
            candidate = statistics.median([value(phases[index], name) for index in (1, 2)])
            control = statistics.median([value(phases[index], name) for index in (0, 3)])
            result["medians"][name] = _ratio(candidate, control, *bounds(name))
        drift_names = ("API_P99", "E2E_P99") if calibration else ("primary_RPC_P99", "per_API_RPC_sum_P99", "API_P99", "E2E_P99")
        for name in drift_names:
            controls = [value(phases[index], name) for index in (0, 3)]
            drift = _ratio(max(controls), min(controls), None, 1.03)
            drift["fractional_drift"] = str(max(controls) / min(controls) - 1)
            result["control_drift"][name] = drift
        result["pass"] = (all(row["pass"] for row in result["directional"])
                          and all(row["pass"] for row in result["medians"].values())
                          and all(row["pass"] for row in result["control_drift"].values()))
    except (KeyError, ValueError, TypeError, ArithmeticError):
        result["errors"].append("unknown_or_invalid_numeric_gate_input")
    return result
