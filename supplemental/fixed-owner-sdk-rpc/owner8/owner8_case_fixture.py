"""Synthetic ABI2 container writer, never a real SDK/runtime qualification."""
import hashlib
import json
import os
from pathlib import Path
import struct

from owner8_fixtures import PROFILE, rpc_fixture


def write_case(directory, mode="enabled", operation="get"):
    root = Path(directory) / "driver"
    root.mkdir()
    sdk = Path(directory) / "libdatasystem.so"
    sdk.write_bytes(b"synthetic identity fixture, not ELF")
    metric, ledger, ends, rpcs, _ = rpc_fixture(operation=operation, mode=mode)
    warm, measure, callers = 16, 32, 8
    maximum, total = warm + measure, 384
    lent = callers * ((maximum * 16 + 256) * 64 + (maximum + 256) * 80)
    metric.update(ledger_row_bytes=128, ledger_bytes=total * 128,
                  caller_state_bytes=4096, capture_lent_bytes=lent if mode == "enabled" else 0)

    def save(name, value):
        (root / name).write_text(json.dumps(value, allow_nan=False) + "\n")

    save("metric.json", metric)
    (root / "all-arrivals.jsonl").write_text("".join(json.dumps(row) + "\n" for row in ledger))
    save("ready.json", dict(pid=4000, worker_pid=5000, callers=8, capture_mode=mode,
         rate_per_second=500, admission_profile=dict(PROFILE), tids=list(range(10000, 10008)),
         lent_capture_bytes=metric["capture_lent_bytes"], ledger_bytes=total * 128,
         caller_state_bytes=4096, monotonic_ns=100))
    save("fixed-owner-plan.json", dict(owner_stride=8, warm_per_owner=warm, measure_per_owner=measure,
         warm_planned=128, measure_planned=256, rate=500, seed=17, modulus=1,
         max_selected_per_owner=48, lent_array_bound_bytes=lent, metadata_reserve_bytes=1048576))
    save("fixed-owner-final.json", dict(closed=True, control_status=0, all_ids_ended_and_owners_released=True))
    if mode == "enabled":
        (root / "capture").mkdir()
        save("capture/config.json", dict(mode="enabled", abi=2, warm_planned=128, measure_planned=256,
             callers=8, sample_seed=17, sample_modulus=1, selected_ids_both_phases=384,
             max_rpc_per_api=16, normal_records_per_caller=48 * 16, error_records_per_caller=256,
             normal_ends_per_caller=48, error_ends_per_caller=256, lent_array_bytes=lent,
             memory_budget_bytes=128 * 1024 * 1024, maximum_planned_apis=2000000,
             metadata_reserve_bytes=1048576, sample_bytes=64, end_bytes=80,
             phase_ids={"warmup": 1, "measure": 2}, normal_prefixes_reset_between_phases=False))
        save("capture/final.json", dict(mode="enabled", control_status=0, resource_sealed=True,
             core_valid_capture=True, capture_or_control_ok=True, rpc30_budget_verified=False, close_ok=True))
        inode = sdk.stat()
        save("capture/sdk-identity.json", dict(sdk_path=str(sdk.resolve()), inode=inode.st_ino,
             device=inode.st_dev, sdk_base="0x1000", getter="0x1100", same_image_function_table=True,
             sdk_images=1, SDK_mapping_rows=1, SDK_zero_offset_images=1,
             anchor_mapped_inode_match=True, new_provider_dso=False))
        (root / "capture/sdk-maps.txt").write_text(
            f"1000-2000 r-xp 00000000 {os.major(inode.st_dev):02x}:{os.minor(inode.st_dev):02x} {inode.st_ino} {sdk.resolve()}\n")
        layouts = {"rpc": ("5Q2I2H2iI", "arrival_id send_ns recv_ns server_processing_ns server_execution_ns phase_id rpc_ordinal caller_index method controller_error ds_error flags"),
                   "ends": ("8Q2IiI", "phase_id arrival_id scheduled_ns api_begin_ns api_end_ns validation_end_ns observed_methods capture_errors rpc_count caller_index outcome flags")}
        primary = 1 if operation == "get" else 4
        for label, count in (("warmup", warm), ("measure", maximum)):
            slots = []
            for caller in range(8):
                for kind, rows in (("rpc", rpcs), ("ends", ends)):
                    selected = [row for row in rows if row["caller_index"] == caller
                                and (label == "measure" or row["phase_id"] == 1)]
                    layout, fields = layouts[kind]
                    blob = b"".join(struct.pack("<" + layout, *(row[key] for key in fields.split())) for row in selected)
                    (root / f"capture/{label}-caller-{caller}.{kind}.bin").write_bytes(blob)
                methods = [0] * 11
                methods[primary] = count
                slots.append(dict(caller=caller, cached_tid=10000 + caller, record_count=count,
                     end_count=count, errors=0, error_generation=0, active=0, first_driver_status=0,
                     registered=1, sealed=0, counters_available=True, rpc_count=count, api_count=count,
                     rpc_failures=0, api_failures=0, method_counts=methods))
            save(f"capture/{label}-prefix.json", dict(abi=2, snapshot_mode=1, state=2, generation=42,
                 global_errors=0, global_error_generation=0, control_status=0, sample_bytes=64,
                 end_bytes=80, byte_order="little", encoding="native-POD; frozenABI2; no pointer fields in rows", slots=slots))
    return root, sdk, hashlib.sha256(sdk.read_bytes()).hexdigest()
