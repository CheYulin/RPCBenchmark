"""Source-shaped synthetic helper/driver files; never an SDK execution result."""
from collections import Counter
from pathlib import Path
import hashlib
import json
import os
import struct

from test_validate_fixed_arrival import fixture

RPC_FIELDS = ('arrival_id send_ns recv_ns server_processing_ns server_execution_ns '
              'phase_id rpc_ordinal caller_index method controller_error ds_error flags').split()
END_FIELDS = ('phase_id arrival_id scheduled_ns api_begin_ns api_end_ns validation_end_ns '
              'observed_methods capture_errors rpc_count caller_index outcome flags').split()


def dump_json(path, value):
    Path(path).write_text(json.dumps(value, separators=(',', ':')) + '\n')


def pack_rows(rows, kind):
    layout, fields = ('<5Q2I2H2iI', RPC_FIELDS) if kind == 'rpc' else ('<8Q2IiI', END_FIELDS)
    return b''.join(struct.pack(layout, *(row[name] for name in fields)) for row in rows)


def capture_shape(smoke=True):
    warm, measure = (4, 8) if smoke else (128, 1024)
    per_owner = warm + measure
    lent = 32 * ((per_owner * 16 + 256) * 64 + (per_owner + 256) * 80)
    return warm, measure, per_owner, lent


def create_case(root, operation='get', backend='brpc', mode='enabled', smoke=True):
    root = Path(root)
    case = root / 'driver'
    case.mkdir()
    sdk = root / 'libdatasystem.so'
    sdk.write_bytes(b'Synthetic expected SDK identity file. No executable SDK code.\n')
    expected_sha = hashlib.sha256(sdk.read_bytes()).hexdigest()
    metric, ledger = fixture(operation=operation, mode=mode, backend=backend, smoke=smoke)
    warm, measure, per_owner, lent = capture_shape(smoke)
    metric.update(smoke=smoke, ledger_bytes=len(ledger)*56, ledger_row_bytes=56,
                  caller_state_bytes=4096, capture_lent_bytes=lent if mode == 'enabled' else 0,
                  RPC30_accepted=False, fixed_offered_load=True,
                  scheduled_equals_api_begin=False, arrival_E2E_available=True)
    dump_json(case/'metric.json', metric)
    (case/'all-arrivals.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in ledger))
    tids = [10_000+owner for owner in range(32)]
    dump_json(case/'ready.json', dict(pid=4000, worker_pid=5000, callers=32, capture_mode=mode,
              rate_per_second=500, tids=tids, lent_capture_bytes=lent if mode == 'enabled' else 0,
              ledger_bytes=len(ledger)*56, caller_state_bytes=4096, monotonic_ns=700_000_000))
    dump_json(case/'fixed-owner-plan.json', dict(owner_stride=32, warm_per_owner=warm,
              measure_per_owner=measure, warm_planned=warm*32, measure_planned=measure*32,
              rate=500, seed=17, modulus=1, max_selected_per_owner=per_owner,
              lent_array_bound_bytes=lent, metadata_reserve_bytes=1_048_576))
    dump_json(case/'fixed-owner-final.json', dict(closed=True, control_status=0,
              all_ids_ended_and_owners_released=True))
    ends, rpcs = [], []
    primary = 1 if operation == 'get' else 4
    for row in ledger:
        rpcs.append(dict(arrival_id=row['arrival_id'], send_ns=row['api_begin_ns']+1000,
                         recv_ns=row['api_begin_ns']+101000, server_processing_ns=0,
                         server_execution_ns=0, phase_id=row['phase_id'], rpc_ordinal=1,
                         caller_index=row['caller'], method=primary,
                         controller_error=0, ds_error=0, flags=1))
        ends.append(dict(phase_id=row['phase_id'], arrival_id=row['arrival_id'],
                         scheduled_ns=row['scheduled_ns'], api_begin_ns=row['api_begin_ns'],
                         api_end_ns=row['api_end_ns'], validation_end_ns=row['validation_end_ns'],
                         observed_methods=1 << primary, capture_errors=0, rpc_count=1,
                         caller_index=row['caller'], outcome=0, flags=1))
    if mode == 'enabled':
        cap = case/'capture'
        cap.mkdir()
        dump_json(cap/'config.json', dict(mode=mode, abi=2, warm_planned=warm*32,
                  measure_planned=measure*32, callers=32, sample_seed=17, sample_modulus=1,
                  selected_ids_both_phases=per_owner*32, max_rpc_per_api=16,
                  normal_records_per_caller=per_owner*16, error_records_per_caller=256,
                  normal_ends_per_caller=per_owner, error_ends_per_caller=256,
                  lent_array_bytes=lent, memory_budget_bytes=134_217_728,
                  maximum_planned_apis=2_000_000, metadata_reserve_bytes=1_048_576,
                  sample_bytes=64, end_bytes=80, phase_ids=dict(warmup=1, measure=2),
                  normal_prefixes_reset_between_phases=False))
        stat = sdk.stat()
        dev = f'{os.major(stat.st_dev):02x}:{os.minor(stat.st_dev):02x}'
        (cap/'sdk-maps.txt').write_text(
            f'10000000-10001000 r--p 00000000 {dev} {stat.st_ino} {sdk.resolve()}\n'
            f'10001000-10002000 r-xp 00001000 {dev} {stat.st_ino} {sdk.resolve()}\n')
        dump_json(cap/'sdk-identity.json', dict(sdk_path=str(sdk.resolve()), inode=stat.st_ino,
                  device=stat.st_dev, sdk_base='0x10000000', getter='0x10001100',
                  same_image_function_table=True, sdk_images=1, SDK_mapping_rows=2,
                  SDK_zero_offset_images=1, anchor_mapped_inode_match=True, new_provider_dso=False))
        for label in ('warmup', 'measure'):
            included = {1} if label == 'warmup' else {1, 2}
            slots = []
            for owner in range(32):
                owner_rpcs = [row for row in rpcs if row['caller_index'] == owner and row['phase_id'] in included]
                owner_ends = [row for row in ends if row['caller_index'] == owner and row['phase_id'] in included]
                methods = Counter(row['method'] for row in owner_rpcs)
                slots.append(dict(caller=owner, cached_tid=tids[owner], record_count=len(owner_rpcs),
                                  end_count=len(owner_ends), errors=0, error_generation=0,
                                  active=0, first_driver_status=0, registered=1, sealed=0,
                                  counters_available=True, rpc_count=len(owner_rpcs), api_count=len(owner_ends),
                                  rpc_failures=0, api_failures=0,
                                  method_counts=[methods.get(method, 0) for method in range(11)]))
                (cap/f'{label}-caller-{owner}.rpc.bin').write_bytes(pack_rows(owner_rpcs, 'rpc'))
                (cap/f'{label}-caller-{owner}.ends.bin').write_bytes(pack_rows(owner_ends, 'end'))
            dump_json(cap/f'{label}-prefix.json', dict(abi=2, snapshot_mode=1, state=2, generation=37,
                      global_errors=0, global_error_generation=0, control_status=0, sample_bytes=64,
                      end_bytes=80, byte_order='little',
                      encoding='native-POD; frozenABI2; no pointer fields in rows', slots=slots))
        dump_json(cap/'final.json', dict(mode='enabled', control_status=0, resource_sealed=True,
                  core_valid_capture=True, capture_or_control_ok=True, rpc30_budget_verified=False, close_ok=True))
    return dict(case=case, sdk=sdk, sha=expected_sha, metric=metric, ledger=ledger, rpcs=rpcs, ends=ends)
