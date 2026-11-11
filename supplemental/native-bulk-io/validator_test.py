#!/usr/bin/env python3
import copy
import importlib.util
import json
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('gate', Path(__file__).with_name('run_gate.py'))
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def stats(framework, concurrency, payload):
    return {'framework': framework, 'workers': 4, 'concurrency': concurrency, 'payload': payload,
            'active_lanes': concurrency, 'elapsed_s': 4.001, 'attempts': 100, 'success': 100,
            'samples': 100, 'rpc_errors': 0, 'mismatches': 0, 'errors': 0, 'histogram': [[10, 100]],
            'p50_us': 10, 'p95_us': 10, 'p99_us': 10, 'p999_us': 10, 'rps': 25}


def mixed():
    return [dict(record='mixed', pipeline=1, connections=32, caller='coroutine', peak_native=[1] * 32,
                 tiny_lanes=24, bulk_lanes=8, tiny_payload=4096, bulk_payload=1048576,
                 phase_start_ns=10**9, phase_end_ns=5 * 10**9, common_drain_end_ns=5001000000),
            stats('coro_mixed_tiny', 24, 4096), stats('coro_mixed_bulk', 8, 1048576)]


def text(records):
    return '\n'.join(json.dumps(row) for row in records)


class CounterGateTest(unittest.TestCase):
    def test_mixed_accepts_common_drain(self):
        _, groups = gate.validate_output(text(mixed()), 'mixed', 4)
        self.assertEqual(set(groups), {'tiny', 'bulk'})

    def test_rejects_asymmetric_group_denominators(self):
        rows = mixed()
        rows[2]['elapsed_s'] = 4.002
        with self.assertRaises(ValueError):
            gate.validate_output(text(rows), 'mixed', 4)

    def test_rejects_zero_lane_and_false_samples(self):
        for field, value in [('active_lanes', 23), ('samples', 99), ('histogram', [[10, 99]])]:
            rows = mixed()
            rows[1][field] = value
            with self.assertRaises(ValueError):
                gate.validate_output(text(rows), 'mixed', 4)

    def test_rejects_missing_class_and_hidden_mismatch(self):
        with self.assertRaises(ValueError):
            gate.validate_output(text(mixed()[:2]), 'mixed', 4)
        rows = mixed()
        rows[1].update(attempts=101, mismatches=1, errors=1)
        with self.assertRaises(ValueError):
            gate.validate_output(text(rows), 'mixed', 4)

    def test_rejects_incomplete_matrix(self):
        with self.assertRaises(ValueError):
            gate.evaluate([])

    def test_tiny_regression_rejects_despite_large_gain(self):
        rows = []
        for case in gate.CASES:
            for variant in gate.ORDER:
                group_names = ('tiny', 'bulk') if case == 'mixed' else ('all',)
                candidate = variant == 'candidate256k'
                groups = {name: {'rps': 120 if candidate else 100, 'p50_us': 10, 'p99_us': 100}
                          for name in group_names}
                rows.append({'case': case, 'variant': variant, 'groups': groups,
                             'total_cpu_us_per_success': 8 if candidate else 10})
        self.assertEqual(gate.evaluate(rows)['candidate_gate'], 'consider_separate_DS_pilot')
        changed = copy.deepcopy(rows)
        for row in changed:
            if row['case'] == 'mixed' and row['variant'] == 'candidate256k':
                row['groups']['tiny']['p99_us'] = 115
        self.assertEqual(gate.evaluate(changed)['candidate_gate'], 'reject_migration')

    def test_coarse_tiny_quantiles_do_not_prove_tail_margin(self):
        rows = []
        for case in gate.CASES:
            for variant in gate.ORDER:
                names = ('tiny', 'bulk') if case == 'mixed' else ('all',)
                candidate = variant == 'candidate256k'
                rows.append({'case': case, 'variant': variant,
                             'groups': {name: {'rps': 120 if candidate else 100, 'p50_us': 100,
                                               'p99_us': 2048 if case == 'mixed' else 100} for name in names},
                             'total_cpu_us_per_success': 8 if candidate else 10})
        result = gate.evaluate(rows)
        self.assertEqual(result['candidate_gate'], 'reject_migration')
        self.assertFalse(result['checks']['mixed_tiny_p99_fine_resolution'])


if __name__ == '__main__':
    unittest.main()
