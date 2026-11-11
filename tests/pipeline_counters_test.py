#!/usr/bin/env python3
"""Local checks that the result gate rejects false-fast counters."""
import copy
import unittest
from pipeline_smoke import validate


class Counters(unittest.TestCase):
    def setUp(self):
        self.config = {"pipeline": 8, "connections": 4, "peak_native": [8, 8, 8, 8]}
        self.result = {"payload": 4096, "concurrency": 32, "active_lanes": 32, "attempts": 100,
                       "success": 100, "rpc_errors": 0, "mismatches": 0, "errors": 0, "samples": 100,
                       "histogram": [[5, 50], [6, 50]], "elapsed_s": 1, "p50_us": 5, "p95_us": 6,
                       "p99_us": 6, "p999_us": 6}

    def test_valid(self):
        validate(self.config, self.result, 8, 32, 4096)

    def test_rejects_missing_samples_error_mismatch_and_inactive_lanes(self):
        for key, value in (("samples", 99), ("attempts", 101), ("errors", 1), ("rpc_errors", 1),
                           ("mismatches", 1), ("active_lanes", 31)):
            with self.subTest(key=key):
                broken = copy.deepcopy(self.result)
                broken[key] = value
                with self.assertRaises(AssertionError):
                    validate(self.config, broken, 8, 32, 4096)

    def test_rejects_serialized_or_excessive_pipeline(self):
        for peaks in ([1] * 4, [17] * 4):
            with self.assertRaises(AssertionError):
                validate({**self.config, "peak_native": peaks}, self.result, 8, 32, 4096)


if __name__ == "__main__":
    unittest.main()
