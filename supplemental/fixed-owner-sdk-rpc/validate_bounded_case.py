"""Read-only qualification of one bounded-admission driver output directory."""
import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from fixed_case_pod import decode_pod_rows, merge_prefix_rows
from validate_bounded_arrival import ADMISSION_PROFILE, validate_arrivals, validate_rpc_rows

BUDGET, RESERVE, CALLERS, MAX_RPC = 128 * 1024 * 1024, 1024 * 1024, 32, 16
ENCODING = "native-POD; frozenABI2; no pointer fields in rows"


def need(ok, reason):
    if not ok:
        raise ValueError(reason)


def integer(value, minimum=0, maximum=(1 << 64) - 1):
    return type(value) is int and minimum <= value <= maximum


def exact_fields(value, fields, name):
    need(isinstance(value, dict) and set(value) == set(fields.split()), name + " fields")


def _ready_admission_profile(ready, metric, arrivals):
    profile = ready.get("admission_profile")
    exact_fields(profile, " ".join(ADMISSION_PROFILE), "ready admission profile")
    need(all(type(profile[key]) is type(value) and profile[key] == value
             for key, value in ADMISSION_PROFILE.items()), "ready admission profile values")
    need(profile == metric.get("admission_profile") == arrivals.get("profile", {}).get("admission_profile"),
         "ready/metric/validated admission profile mismatch")


def pairs(items):
    value = {}
    for key, item in items:
        need(key not in value, "duplicate JSON key: " + key)
        value[key] = item
    return value


def json_value(data):
    def nonfinite(value):
        raise ValueError("nonfinite JSON number: " + value)
    return json.loads(data, object_pairs_hook=pairs, parse_constant=nonfinite)


class Inputs:
    def __init__(self, root):
        self.root = root
        self.hashes = {}

    def read(self, name, maximum=BUDGET):
        path = self.root / name
        need(path.is_file() and not path.is_symlink(), "missing/nonregular input: " + name)
        need(path.stat().st_size <= maximum, "input budget: " + name)
        data = path.read_bytes()
        need(len(data) <= maximum, "input grew beyond budget: " + name)
        self.hashes[name] = hashlib.sha256(data).hexdigest()
        return data

    def load(self, name):
        return json_value(self.read(name, 4 * 1024 * 1024))


def sdk_identity(inputs, expected, identity, expected_stat):
    exact_fields(identity, "sdk_path inode device sdk_base getter same_image_function_table sdk_images SDK_mapping_rows SDK_zero_offset_images anchor_mapped_inode_match new_provider_dso", "SDK identity")
    need(identity["sdk_path"] == str(expected), "SDK identity path")
    need(integer(identity["inode"], 1) and identity["inode"] == expected_stat.st_ino
         and integer(identity["device"]) and identity["device"] == expected_stat.st_dev, "SDK identity inode/device")
    need(identity["same_image_function_table"] is True and identity["anchor_mapped_inode_match"] is True
         and identity["new_provider_dso"] is False, "SDK anchor/function-table proof")
    need(type(identity["sdk_images"]) is int and identity["sdk_images"] == 1
         and type(identity["SDK_zero_offset_images"]) is int and identity["SDK_zero_offset_images"] == 1, "SDK unique loaded image")
    addresses = []
    for key in ("sdk_base", "getter"):
        need(isinstance(identity[key], str) and re.fullmatch(r"0x[0-9a-fA-F]+", identity[key]), "SDK pointer encoding")
        addresses.append(int(identity[key], 16))
    base, getter = addresses
    need(0 < base <= getter, "SDK pointer range")
    rows, zeros, getter_rows = [], 0, 0
    for line in inputs.read("capture/sdk-maps.txt", 8 * 1024 * 1024).decode().splitlines():
        values = line.split(None, 5)
        need(len(values) >= 5, "SDK maps shape")
        try:
            begin, end = (int(v, 16) for v in values[0].split("-"))
            offset = int(values[2], 16)
            major, minor = (int(v, 16) for v in values[3].split(":"))
            inode = int(values[4])
        except (ValueError, TypeError):
            raise ValueError("SDK maps encoding") from None
        need(begin < end and re.fullmatch(r"[r-][w-][x-][ps]", values[1]) is not None, "SDK maps range/permissions")
        path = values[5] if len(values) == 6 else ""
        removed = path.endswith(" (deleted)")
        clean = path[:-10] if removed else path
        basename = Path(clean).name
        named = basename == "libdatasystem.so" or basename.startswith("libdatasystem.so.")
        owned = inode == expected_stat.st_ino and (major, minor) == (os.major(expected_stat.st_dev), os.minor(expected_stat.st_dev))
        if owned or named:
            need(owned and not removed and Path(clean).resolve(strict=True) == expected, "SDK maps foreign/deleted image")
            rows.append(line)
            if offset == 0:
                zeros += 1
                need(begin == base, "SDK maps base")
            if begin <= getter < end:
                getter_rows += 1
                need("x" in values[1], "SDK getter not executable")
    need(integer(identity["SDK_mapping_rows"], 1) and len(rows) == identity["SDK_mapping_rows"]
         and zeros == 1 and getter_rows == 1, "SDK unique map/getter count")
    return {"sdk_path": str(expected), "inode": expected_stat.st_ino, "device": expected_stat.st_dev,
            "mapped_rows": len(rows), "unique_image": True,
            "anchor_proof": "frozen helper assertion plus unique inode/base/getter maps"}


def validate_case(case_directory, operation, backend, *, smoke, expected_sdk_path, expected_sdk_sha256):
    """Qualify exactly this driver directory; controller role/routes remain external."""
    need(operation in ("get", "set") and backend in ("brpc", "coro") and type(smoke) is bool, "case arguments")
    need(isinstance(expected_sdk_sha256, str) and re.fullmatch(r"[0-9a-f]{64}", expected_sdk_sha256), "expected SDK SHA format")
    expected = Path(expected_sdk_path).resolve(strict=True)
    expected_stat = expected.stat()
    need(stat.S_ISREG(expected_stat.st_mode), "expected SDK regular file")
    digest = hashlib.sha256()
    with expected.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    need(digest.hexdigest() == expected_sdk_sha256, "expected SDK SHA mismatch")
    root = Path(case_directory)
    need(root.is_dir(), "driver output directory")
    inputs = Inputs(root)
    metric, ready = inputs.load("metric.json"), inputs.load("ready.json")
    ledger = [json_value(line) for line in inputs.read("all-arrivals.jsonl", 64 * 1024 * 1024).splitlines()]
    arrivals = validate_arrivals(metric, ledger, operation, smoke=smoke)
    need(arrivals.get("valid") is True, "arrival validation: " + ",".join(arrivals.get("errors", [])))
    need(metric.get("backend") == backend and metric.get("smoke") is smoke, "case backend/smoke")
    for key, value in (("RPC30_accepted", False), ("fixed_offered_load", True),
                       ("scheduled_equals_api_begin", False), ("arrival_E2E_available", True)):
        need(metric.get(key) is value, "metric scope: " + key)
    warm, measure = (4, 8) if smoke else (128, 1024)
    maximum, total = warm + measure, (warm + measure) * CALLERS
    lent = CALLERS * (((maximum * MAX_RPC + 256) * 64) + ((maximum + 256) * 80))
    need(lent + RESERVE <= BUDGET, "fixed capture budget")
    plan = inputs.load("fixed-owner-plan.json")
    expected_plan = dict(owner_stride=CALLERS, warm_per_owner=warm, measure_per_owner=measure,
                         warm_planned=warm * CALLERS, measure_planned=measure * CALLERS, rate=500,
                         seed=17, modulus=1, max_selected_per_owner=maximum,
                         lent_array_bound_bytes=lent, metadata_reserve_bytes=RESERVE)
    need(plan == expected_plan and all(type(v) is int for v in plan.values()), "fixed-owner truthful plan/capacity")
    final = inputs.load("fixed-owner-final.json")
    exact_fields(final, "closed control_status all_ids_ended_and_owners_released", "fixed-owner final")
    need(final["closed"] is True and type(final["control_status"]) is int and final["control_status"] == 0
         and final["all_ids_ended_and_owners_released"] is True, "fixed-owner sequence/close")
    exact_fields(ready, "pid worker_pid callers capture_mode rate_per_second admission_profile tids lent_capture_bytes ledger_bytes caller_state_bytes monotonic_ns", "ready")
    _ready_admission_profile(ready, metric, arrivals)
    tids = ready["tids"]
    need(isinstance(tids, list) and len(tids) == CALLERS and all(integer(t, 1) for t in tids)
         and len(set(tids)) == CALLERS, "ready TID set")
    need(ready["pid"] == metric["sdk_pid"] and ready["worker_pid"] == metric["worker_pid"]
         and type(ready["pid"]) is type(ready["worker_pid"]) is int, "ready role PID binding")
    need(type(ready["callers"]) is int and ready["callers"] == CALLERS
         and type(ready["rate_per_second"]) is int and ready["rate_per_second"] == 500
         and ready["capture_mode"] == metric["capture_mode"] and integer(ready["monotonic_ns"], 1), "ready workload")
    need(all(row["tid"] == tids[row["caller"]] for row in ledger), "ledger/ready TID binding")
    need(integer(metric.get("ledger_row_bytes"), 1) and metric.get("ledger_bytes") == total * metric["ledger_row_bytes"]
         and type(metric["ledger_bytes"]) is int and ready["ledger_bytes"] == metric["ledger_bytes"], "ledger allocation accounting")
    need(integer(metric.get("caller_state_bytes"), 1) and ready["caller_state_bytes"] == metric["caller_state_bytes"], "caller allocation accounting")
    expected_lent = lent if metric["capture_mode"] == "enabled" else 0
    need(type(metric.get("capture_lent_bytes")) is int and metric["capture_lent_bytes"] == expected_lent
         and type(ready["lent_capture_bytes"]) is int and ready["lent_capture_bytes"] == expected_lent, "lent capture allocation accounting")
    result = dict(arrivals, diagnostic_valid=True, RPC_available=False, rpc_valid=False,
                  rpc_container_qualified=False, external_role_proofs_required=True,
                  backend_route_attested=False, RPC30_accepted=False,
                  expected_sdk_sha256=expected_sdk_sha256,
                  external_proofs=["owned SDK/Worker PID birth and lifecycle", "role live maps and resources",
                                   "configured backend, ports/route and source-bound no-fallback checks"])
    if metric["capture_mode"] == "off":
        need(not (root / "capture").exists(), "off created capture directory")
    else:
        expected_files = {"config.json", "final.json", "sdk-identity.json", "sdk-maps.txt",
                          "warmup-prefix.json", "measure-prefix.json"}
        expected_files.update(f"{label}-caller-{caller}.{suffix}" for label in ("warmup", "measure")
                              for caller in range(CALLERS) for suffix in ("rpc.bin", "ends.bin"))
        need({path.name for path in (root / "capture").iterdir()} == expected_files,
             "unknown or missing capture container files")
        config = inputs.load("capture/config.json")
        expected_config = dict(mode="enabled", abi=2, warm_planned=warm * CALLERS, measure_planned=measure * CALLERS,
                               callers=CALLERS, sample_seed=17, sample_modulus=1, selected_ids_both_phases=total,
                               max_rpc_per_api=MAX_RPC, normal_records_per_caller=maximum * MAX_RPC,
                               error_records_per_caller=256, normal_ends_per_caller=maximum, error_ends_per_caller=256,
                               lent_array_bytes=lent, memory_budget_bytes=BUDGET, maximum_planned_apis=2_000_000,
                               metadata_reserve_bytes=RESERVE, sample_bytes=64, end_bytes=80,
                               phase_ids={"warmup": 1, "measure": 2}, normal_prefixes_reset_between_phases=False)
        need(config == expected_config, "capture config ABI/global-vs-owner capacity")
        need(config["normal_prefixes_reset_between_phases"] is False
             and all(type(value) is int for value in config["phase_ids"].values()),
             "capture config nested integer/boolean types")
        for key, value in expected_config.items():
            if type(value) is int:
                need(type(config[key]) is int, "capture config integer: " + key)
        final = inputs.load("capture/final.json")
        exact_fields(final, "mode control_status resource_sealed core_valid_capture capture_or_control_ok rpc30_budget_verified close_ok", "capture final")
        need(final["mode"] == "enabled" and type(final["control_status"]) is int and final["control_status"] == 0
             and final["rpc30_budget_verified"] is False
             and all(final[k] is True for k in ("resource_sealed", "core_valid_capture", "capture_or_control_ok", "close_ok")), "capture normal seal/close")
        identity = sdk_identity(inputs, expected, inputs.load("capture/sdk-identity.json"), expected_stat)
        prefix, decoded, blobs = {}, {}, {}
        for label, count in (("warmup", warm), ("measure", maximum)):
            meta = inputs.load("capture/" + label + "-prefix.json")
            prefix[label], decoded[label], blobs[label] = meta, {"rpc": [], "end": []}, {}
            exact_fields(meta, "abi snapshot_mode state generation global_errors global_error_generation control_status sample_bytes end_bytes byte_order encoding slots", "prefix")
            for key, value in (("abi", 2), ("snapshot_mode", 1), ("state", 2), ("sample_bytes", 64), ("end_bytes", 80),
                               ("global_errors", 0), ("global_error_generation", 0), ("control_status", 0)):
                need(type(meta[key]) is int and meta[key] == value, "prefix metadata: " + key)
            need(integer(meta["generation"], 1) and meta["byte_order"] == "little" and meta["encoding"] == ENCODING, "prefix generation/encoding")
            slots = meta["slots"]
            need(isinstance(slots, list) and len(slots) == CALLERS, "prefix slot count")
            seen = set()
            for slot in slots:
                exact_fields(slot, "caller cached_tid record_count end_count errors error_generation active first_driver_status registered sealed counters_available rpc_count api_count rpc_failures api_failures method_counts", "slot")
                caller = slot["caller"]
                need(integer(caller, 0, CALLERS - 1) and caller not in seen, "prefix slot owner")
                seen.add(caller)
                need(type(slot["cached_tid"]) is int and slot["cached_tid"] == tids[caller], "prefix slot TID")
                for field, value in (("errors", 0), ("error_generation", 0), ("active", 0), ("first_driver_status", 0),
                                     ("registered", 1), ("sealed", 0), ("rpc_failures", 0), ("api_failures", 0), ("api_count", count), ("end_count", count)):
                    need(type(slot[field]) is int and slot[field] == value, "prefix slot field: " + field)
                need(slot["counters_available"] is True and integer(slot["rpc_count"], count, count * MAX_RPC)
                     and slot["record_count"] == slot["rpc_count"] and type(slot["record_count"]) is int
                     and slot["record_count"] <= maximum * MAX_RPC and slot["end_count"] <= maximum, "prefix slot count/capacity")
                local_rpc = []
                for kind, suffix, width, number in (("rpc", "rpc.bin", 64, slot["record_count"]), ("end", "ends.bin", 80, slot["end_count"])):
                    name = f"capture/{label}-caller-{caller}.{suffix}"
                    blob = inputs.read(name, (maximum * MAX_RPC + 256) * 64 if kind == "rpc" else (maximum + 256) * 80)
                    need(len(blob) == number * width, "prefix exact byte count: " + name)
                    rows = decode_pod_rows(blob, kind, "little")
                    need(all(row["caller_index"] == caller and row["phase_id"] in ((1,) if label == "warmup" else (1, 2)) for row in rows), "prefix row owner/phase")
                    blobs[label][caller, kind] = blob
                    decoded[label][kind].extend(rows)
                    if kind == "rpc":
                        local_rpc = rows
                methods = slot["method_counts"]
                wanted = Counter(row["method"] for row in local_rpc)
                need(isinstance(methods, list) and len(methods) == 11 and all(integer(v) for v in methods)
                     and methods[0] == 0 and sum(methods) == slot["rpc_count"]
                     and all(methods[index] == wanted[index] for index in range(11)), "prefix method count conservation")
        need(prefix["warmup"]["generation"] == prefix["measure"]["generation"], "prefix generation changed")
        for key, warm_blob in blobs["warmup"].items():
            need(blobs["measure"][key].startswith(warm_blob), "warm raw prefix changed or absent")
        ends = merge_prefix_rows(decoded["warmup"]["end"], decoded["measure"]["end"], "end")
        rpcs = merge_prefix_rows(decoded["warmup"]["rpc"], decoded["measure"]["rpc"], "rpc")
        rpc = validate_rpc_rows(arrivals, ledger, ends, rpcs, prefix["measure"]["slots"], operation)
        need(rpc.get("valid") is True and rpc.get("rpc_valid") is True, "RPC join validation: " + ",".join(rpc.get("errors", [])))
        result.update(rpc)
        result.update(diagnostic_valid=True, RPC_available=True, rpc_container_qualified=True,
                      external_role_proofs_required=True, backend_route_attested=False, RPC30_accepted=False,
                      sdk_identity=identity)
    result["input_sha256"] = dict(inputs.hashes)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", required=True)
    parser.add_argument("--operation", choices=("get", "set"), required=True)
    parser.add_argument("--backend", choices=("brpc", "coro"), required=True)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--expected-sdk-path", required=True)
    parser.add_argument("--expected-sdk-sha256", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    need(not Path(args.output).exists(), "validation output already exists")
    try:
        result = validate_case(args.case, args.operation, args.backend, smoke=args.smoke,
                               expected_sdk_path=args.expected_sdk_path, expected_sdk_sha256=args.expected_sdk_sha256)
    except (ValueError, OSError, KeyError, TypeError, UnicodeError) as error:
        result = dict(valid=False, diagnostic_valid=False, RPC30_accepted=False,
                      external_role_proofs_required=True, errors=[str(error)])
    with Path(args.output).open("x") as stream:
        json.dump(result, stream, indent=2, allow_nan=False)
        stream.write("\n")
    return 0 if result.get("valid") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
