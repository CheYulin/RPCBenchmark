"""Offline real-container adapter counterexamples; generated rows are synthetic."""
from pathlib import Path
import json
import tempfile
import unittest

from fixed_case_fixture import create_case, dump_json, pack_rows, capture_shape
from validate_fixed_case import validate_case


class FixedCaseTests(unittest.TestCase):
    def make(self, **kwargs):
        temp = tempfile.TemporaryDirectory(prefix='fixed-case-test-')
        self.addCleanup(temp.cleanup)
        return create_case(Path(temp.name), **kwargs)

    def validate(self, f, **kwargs):
        return validate_case(f['case'], kwargs.pop('operation', f['metric']['operation']),
                             kwargs.pop('backend', f['metric']['backend']), smoke=kwargs.pop('smoke', True),
                             expected_sdk_path=kwargs.pop('sdk', f['sdk']),
                             expected_sdk_sha256=kwargs.pop('sha', f['sha']), **kwargs)

    def rejected(self, f, **kwargs):
        try:
            result = self.validate(f, **kwargs)
        except (ValueError, OSError):
            return
        self.assertFalse(result['valid'], result)
        self.assertFalse(result.get('RPC30_accepted', False))

    def json_mutate(self, f, relative, change):
        target = f['case']/relative
        value = json.loads(target.read_text())
        change(value)
        dump_json(target, value)

    def test_smoke_both_operations_and_backends(self):
        for operation in ('get', 'set'):
            for backend in ('brpc', 'coro'):
                with self.subTest(operation=operation, backend=backend):
                    result = self.validate(self.make(operation=operation, backend=backend))
                    self.assertTrue(result['valid'], result)
                    self.assertTrue(result['RPC_available'])
                    self.assertTrue(result['rpc_valid'])
                    self.assertTrue(result['rpc_container_qualified'])
                    self.assertTrue(result['external_role_proofs_required'])
                    self.assertFalse(result['RPC30_accepted'])

    def test_off_has_full_arrival_validation_and_no_rpc_claim(self):
        f = self.make(mode='off')
        result = self.validate(f)
        self.assertTrue(result['valid'], result)
        self.assertFalse(result['RPC_available'])
        self.assertFalse(result.get('rpc_valid', False))
        self.assertTrue(result['external_role_proofs_required'])
        self.assertFalse(result['RPC30_accepted'])
        self.assertFalse((f['case']/'capture').exists())
        lines = (f['case']/'all-arrivals.jsonl').read_text().splitlines()
        (f['case']/'all-arrivals.jsonl').write_text('\n'.join(lines[:-1])+'\n')
        self.rejected(f)

    def test_off_rejects_invented_capture_and_owner_receipt_failure(self):
        f = self.make(mode='off')
        (f['case']/'capture').mkdir()
        self.rejected(f)
        f = self.make(mode='off')
        self.json_mutate(f, 'fixed-owner-final.json', lambda v: v.update(all_ids_ended_and_owners_released=False))
        self.rejected(f)

    def test_global_selected_counts_are_not_per_owner_capacity(self):
        self.assertEqual(capture_shape(False), (128, 1024, 1152, 41_877_504))
        for key, value in [('selected_ids_both_phases', 12), ('normal_ends_per_caller', 384),
                           ('normal_records_per_caller', 384*16), ('lent_array_bytes', 41_877_504)]:
            with self.subTest(key=key):
                f = self.make()
                self.json_mutate(f, 'capture/config.json', lambda v: v.update({key: value}))
                self.rejected(f)

    def test_frozen_abi_and_container_metadata(self):
        for key, value in [('abi', 3), ('sample_bytes', 63), ('end_bytes', 79),
                           ('byte_order', 'big'), ('encoding', 'json'), ('snapshot_mode', 2),
                           ('generation', 0), ('global_errors', 1)]:
            with self.subTest(key=key):
                f = self.make()
                self.json_mutate(f, 'capture/measure-prefix.json', lambda v: v.update({key: value}))
                self.rejected(f)

    def test_exact_bin_length_and_declared_count(self):
        for kind, change in [('rpc.bin', lambda b: b[:-1]), ('ends.bin', lambda b: b+b'\0')]:
            with self.subTest(kind=kind):
                f = self.make()
                path = f['case']/'capture'/('measure-caller-0.'+kind)
                path.write_bytes(change(path.read_bytes()))
                self.rejected(f)
        for field, value in [('record_count', 11), ('end_count', 13), ('record_count', 449), ('end_count', 269)]:
            with self.subTest(field=field, value=value):
                f = self.make()
                self.json_mutate(f, 'capture/measure-prefix.json', lambda v: v['slots'][0].update({field: value}))
                self.rejected(f)

    def test_cumulative_prefix_cannot_change_reorder_or_drop(self):
        for mutation in ('changed', 'reordered', 'missing'):
            with self.subTest(mutation=mutation):
                f = self.make()
                path = f['case']/'capture'/'measure-caller-0.rpc.bin'
                blob = path.read_bytes()
                if mutation == 'changed':
                    blob = blob[:24]+(1).to_bytes(8, 'little')+blob[32:]
                elif mutation == 'reordered':
                    blob = blob[64:128]+blob[:64]+blob[128:]
                else:
                    blob = blob[64:]
                path.write_bytes(blob)
                self.rejected(f)

    def test_missing_rows_and_duplicate_slots_cannot_be_filtered(self):
        for relative in ('capture/measure-caller-0.rpc.bin', 'capture/warmup-caller-2.ends.bin',
                         'capture/sdk-identity.json', 'fixed-owner-final.json'):
            with self.subTest(relative=relative):
                f = self.make()
                (f['case']/relative).unlink()
                self.rejected(f)
        f = self.make()
        self.json_mutate(f, 'capture/measure-prefix.json', lambda v: v['slots'].__setitem__(31, v['slots'][0]))
        self.rejected(f)

    def test_owner_tid_counters_methods_and_lifecycle(self):
        for field, value in [('cached_tid', 999), ('api_count', 13), ('rpc_count', 11),
                             ('active', 1), ('registered', 0), ('sealed', 1), ('counters_available', False),
                             ('api_failures', 1), ('first_driver_status', 1), ('method_counts', [0]*11)]:
            with self.subTest(field=field):
                f = self.make()
                self.json_mutate(f, 'capture/measure-prefix.json', lambda v: v['slots'][0].update({field: value}))
                self.rejected(f)
        for field in ('resource_sealed', 'core_valid_capture', 'capture_or_control_ok', 'close_ok'):
            f = self.make()
            self.json_mutate(f, 'capture/final.json', lambda v: v.update({field: False}))
            self.rejected(f)

    def test_expected_sdk_sha_path_inode_and_actual_maps(self):
        f = self.make()
        self.rejected(f, sha='0'*64)
        for field, value in [('sdk_path', '/unrelated/libdatasystem.so'), ('inode', 1), ('device', 0),
                             ('same_image_function_table', False), ('sdk_images', 2),
                             ('SDK_zero_offset_images', 2), ('anchor_mapped_inode_match', False),
                             ('new_provider_dso', True), ('getter', '0x30000000')]:
            with self.subTest(field=field):
                f = self.make()
                self.json_mutate(f, 'capture/sdk-identity.json', lambda v: v.update({field: value}))
                self.rejected(f)
        for kind in ('deleted', 'duplicate', 'wrong_inode'):
            with self.subTest(kind=kind):
                f = self.make()
                path = f['case']/'capture/sdk-maps.txt'
                raw = path.read_text()
                if kind == 'deleted': raw = raw.replace(str(f['sdk']), str(f['sdk'])+' (deleted)')
                elif kind == 'duplicate': raw += raw.splitlines()[0]+'\n'
                else: raw = raw.replace(str(f['sdk'].stat().st_ino), '1')
                path.write_text(raw)
                self.rejected(f)

    def test_server_metadata_flags_are_not_backend_tags_or_population_filter(self):
        f = self.make(backend='coro')
        for label in ('warmup', 'measure'):
            for owner in range(32):
                rows = [dict(row) for row in f['rpcs'] if row['caller_index'] == owner
                        and (label == 'measure' or row['phase_id'] == 1)]
                for row in rows:
                    row.update(flags=1|256|1024, server_processing_ns=80000, server_execution_ns=50000)
                (f['case']/'capture'/f'{label}-caller-{owner}.rpc.bin').write_bytes(pack_rows(rows, 'rpc'))
        result = self.validate(f)
        self.assertTrue(result['valid'], result)
        self.assertFalse(result['RPC30_accepted'])
        self.rejected(f, backend='brpc')

    def test_metric_ledger_and_ready_bindings_are_required(self):
        for relative, field, value in [('ready.json', 'pid', 1), ('ready.json', 'tids', list(range(32))),
                                       ('metric.json', 'smoke', False), ('metric.json', 'backend', 'coro'),
                                       ('fixed-owner-plan.json', 'modulus', 2)]:
            with self.subTest(relative=relative, field=field):
                f = self.make()
                self.json_mutate(f, relative, lambda v: v.update({field: value}))
                self.rejected(f)

    def test_validation_reads_raw_without_rewriting(self):
        import hashlib
        f = self.make()
        before = {p.relative_to(f['case']): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in f['case'].rglob('*') if p.is_file()}
        self.assertTrue(self.validate(f)['valid'])
        after = {p.relative_to(f['case']): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in f['case'].rglob('*') if p.is_file()}
        self.assertEqual(after, before)


if __name__ == '__main__':
    unittest.main()
