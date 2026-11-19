"""Synthetic filesystem tests. These fixtures are not SDK/runtime evidence."""
import copy
import json
import struct
import tempfile
import unittest
from pathlib import Path

from validate_case import validate_metric


def write(path, value):
    path.write_text(json.dumps(value) + '\n')


def make_case(case, mode='enabled'):
    driver = case / 'driver'; driver.mkdir()
    tids = list(range(100, 132))
    ready = dict(pid=1000, worker_pid=2000, callers=32, capture_mode=mode, tids=tids,
                 lent_capture_bytes=1214976 if mode == 'enabled' else 0,
                 ledger_bytes=524352, caller_state_bytes=8500000, monotonic_ns=500000)
    write(driver / 'ready.json', ready)
    metric = dict(quality_valid=True, sdk_shutdown_ok=True, capture_close_ok=True,
                  RPC30_accepted=False, fixed_offered_load=False, arrival_E2E_available=False,
                  scheduled_equals_api_begin=True, operation='get', capture_mode=mode,
                  callers=32, payload_bytes=4096, warm_calls_per_caller=8,
                  measure_calls_per_caller=16, readback_keys=64, readback_errors=0,
                  sdk_pid=1000, worker_pid=2000, clock_ticks_per_second=100,
                  rpc_capture_available=mode == 'enabled', phases=[], caller_phases=[],
                  capture_lent_bytes=ready['lent_capture_bytes'], ledger_bytes=524352,
                  ledger_row_bytes=64, caller_state_bytes=8500000)
    for phase, count in [(1, 8), (2, 16)]:
        population = count * 32; start = phase * 1000000
        first_finish, last_finish = start + 1000, start + 1062
        elapsed = (last_finish - start) / 1e9
        metric['phases'].append(dict(
            phase_id=phase, planned=population, attempts=population, success=population,
            errors=0, mismatches=0, error_ledger_rows=0,
            selected_ledger_rows=int(phase == 2), returned_bytes=population * 4096,
            validated_bytes=population * 4096, api_histogram_upper_us=[[1, population]],
            observer_histogram_upper_us=[[1, population]], api_p50_upper_us=1,
            api_p99_upper_us=1, observer_p50_upper_us=1, observer_p99_upper_us=1,
            first_api_begin_ns=start, first_caller_finish_ns=first_finish,
            last_caller_finish_ns=last_finish, taper_ns=last_finish-first_finish,
            elapsed_s=elapsed, qps=population/elapsed, sdk_cpu_s=0, worker_cpu_s=0,
            CPU_us_per_success=0,
            cpu_before=dict(read_begin_ns=start-100, read_end_ns=start-1,
                            sdk_ticks=100, worker_ticks=200),
            cpu_after=dict(read_begin_ns=last_finish+1, read_end_ns=last_finish+100,
                           sdk_ticks=100, worker_ticks=200)))
        for caller in range(32):
            metric['caller_phases'].append(dict(
                caller=caller, tid=tids[caller], phase_id=phase, attempts=count,
                api_begin_ns=start + caller*2, observer_end_ns=start + 1000 + caller*2,
                ledger_rows_both_phases=int(caller == 12),
                ledger_capacity=256 + int(caller == 12)))
    begin = 2000500
    # Exact seed17/mod512 oracle for smoke: no warm sample; only measure ID236.
    ledger = dict(caller=12, tid=112, phase_id=2, arrival_id=236,
                  api_begin_ns=begin, api_end_ns=begin+100,
                  validation_end_ns=begin+120, observer_end_ns=begin+150,
                  scheduled_ns=begin, status=0, selected=True, mismatch=False)
    (driver / 'sampled-api-ledger.jsonl').write_text(json.dumps(ledger) + '\n')
    write(driver / 'metric.json', metric)
    if mode == 'enabled':
        cap = driver / 'capture'; cap.mkdir()
        write(cap / 'final.json', dict(mode='enabled', control_status=0,
              resource_sealed=True, core_valid_capture=True, capture_or_control_ok=True,
              rpc30_budget_verified=False, close_ok=True))
        for label, total_per_caller in [('warmup', 8), ('measure', 24)]:
            slots = []
            for caller in range(32):
                sampled = label == 'measure' and caller == 12
                slots.append(dict(caller=caller, cached_tid=tids[caller], record_count=int(sampled),
                                  end_count=int(sampled), errors=0, error_generation=0,
                                  active=0, first_driver_status=0, registered=1, sealed=0,
                                  counters_available=True, rpc_count=total_per_caller,
                                  api_count=total_per_caller, rpc_failures=0, api_failures=0,
                                  method_counts=[0, total_per_caller] + [0]*9))
                rpc = struct.pack('<5Q2I2H2iI', 236, begin+10, begin+90, 20, 10,
                                  2, 1, 12, 1, 0, 0, 1 | 256 | 1024) if sampled else b''
                end = struct.pack('<8Q2IiI', 2, 236, begin, begin, begin+100, begin+120,
                                  1 << 1, 0, 1, 12, 0, 1) if sampled else b''
                (cap / f'{label}-caller-{caller}.rpc.bin').write_bytes(rpc)
                (cap / f'{label}-caller-{caller}.ends.bin').write_bytes(end)
            write(cap / (label+'-prefix.json'), dict(abi=2, sample_bytes=64, end_bytes=80,
                  snapshot_mode=1, state=2, generation=7, global_errors=0,
                  global_error_generation=0, control_status=0, byte_order='little', slots=slots))
        write(cap / 'config.json', dict(mode='enabled', abi=2, warm_planned=256,
              measure_planned=512, callers=32, sample_seed=17, sample_modulus=512,
              selected_ids_both_phases=1, max_rpc_per_api=16, normal_records_per_caller=16,
              error_records_per_caller=256, normal_ends_per_caller=1, error_ends_per_caller=256,
              lent_array_bytes=1214976, memory_budget_bytes=134217728,
              maximum_planned_apis=2000000, metadata_reserve_bytes=1048576,
              sample_bytes=64, end_bytes=80, phase_ids=dict(warmup=1, measure=2),
              normal_prefixes_reset_between_phases=False))
    return metric


class FileValidatorTests(unittest.TestCase):
    def run_fixture(self, mode='enabled', mutate=None, must_fail=False, after_write=None):
        with tempfile.TemporaryDirectory(prefix='closed-validator-unit-') as directory:
            case = Path(directory); metric = make_case(case, mode)
            if mutate: mutate(case, metric)
            # Keep driver file and passed stdout metric identical unless a test explicitly tests this binding.
            write(case/'driver/metric.json', metric)
            if after_write: after_write(case, metric)
            if must_fail:
                with self.assertRaises((ValueError, OSError, KeyError, ZeroDivisionError)):
                    validate_metric(metric, case, True, mode, 'get')
            else:
                result = validate_metric(metric, case, True, mode, 'get')
                self.assertTrue(result['diagnostic_valid'])
                self.assertFalse(result['RPC30_accepted'])
                self.assertTrue((case/'diagnosis.json').is_file())

    def edit(self, case, relative, fn):
        path = case/'driver'/relative; value = json.loads(path.read_text()); fn(value); write(path, value)

    def test_off_complete_files(self): self.run_fixture(mode='off')
    def test_enabled_complete_files(self): self.run_fixture()
    def test_capture_tid_tamper(self):
        self.run_fixture(mutate=lambda c,m: self.edit(c, 'capture/measure-prefix.json',
                         lambda x: x['slots'][12].update(cached_tid=999)), must_fail=True)
    def test_truncated_pod(self):
        def change(c,m):
            p=c/'driver/capture/measure-caller-12.rpc.bin';p.write_bytes(p.read_bytes()[:-1])
        self.run_fixture(mutate=change, must_fail=True)
    def test_shifted_independent_ledger(self):
        def change(c,m):
            p=c/'driver/sampled-api-ledger.jsonl';x=json.loads(p.read_text());x['api_end_ns']+=1;write(p,x)
        self.run_fixture(mutate=change, must_fail=True)
    def test_corrupt_histogram_count(self):
        self.run_fixture(mutate=lambda c,m: m['phases'][1]['api_histogram_upper_us'][0].__setitem__(1, 511), must_fail=True)
    def test_missing_final_seal(self):
        self.run_fixture(mutate=lambda c,m: (c/'driver/capture/final.json').unlink(), must_fail=True)
    def test_false_final_seal(self):
        self.run_fixture(mutate=lambda c,m: self.edit(c, 'capture/final.json',
                         lambda x: x.update(resource_sealed=False)), must_fail=True)
    def test_final_record_count_tamper(self):
        self.run_fixture(mutate=lambda c,m: self.edit(c, 'capture/measure-prefix.json',
                         lambda x: x['slots'][12].update(record_count=2)), must_fail=True)
    def test_unknown_prefix_state(self):
        self.run_fixture(mutate=lambda c,m: self.edit(c, 'capture/measure-prefix.json',
                         lambda x: x.update(state=999)), must_fail=True)
    def test_warm_slot_error_cannot_be_hidden_by_final(self):
        self.run_fixture(mutate=lambda c,m: self.edit(c, 'capture/warmup-prefix.json',
                         lambda x: x['slots'][0].update(errors=1)), must_fail=True)
    def test_final_mode_must_match(self):
        self.run_fixture(mutate=lambda c,m: self.edit(c, 'capture/final.json',
                         lambda x: x.update(mode='disabled')), must_fail=True)
    def test_duplicate_phase_not_overwritten(self):
        self.run_fixture(mutate=lambda c,m: m['phases'].append(copy.deepcopy(m['phases'][1])), must_fail=True)
    def test_invalid_histogram_bound_even_if_ranks_match(self):
        def change(c,m):
            p=m['phases'][1];p['api_histogram_upper_us']=[[9999,512]]
            p['api_p50_upper_us']=p['api_p99_upper_us']=9999
        self.run_fixture(mutate=change, must_fail=True)
    def test_caller_phase_finish_must_match_aggregate(self):
        self.run_fixture(mutate=lambda c,m: m['caller_phases'][0].update(observer_end_ns=1), must_fail=True)

    def test_metric_file_and_result_must_match(self):
        self.run_fixture(after_write=lambda c,m: self.edit(c, 'metric.json',
                         lambda x: x['phases'][1].update(mismatches=1)), must_fail=True)
    def test_quality_flag_is_boolean(self):
        self.run_fixture(mutate=lambda c,m: m.update(quality_valid='false'), must_fail=True)
    def test_sampled_latency_must_exist_in_full_histogram(self):
        def change(c,m):
            p=m['phases'][1];p['api_histogram_upper_us']=[[2,512]]
            p['api_p50_upper_us']=p['api_p99_upper_us']=2
        self.run_fixture(mutate=change, must_fail=True)


if __name__ == '__main__': unittest.main()
