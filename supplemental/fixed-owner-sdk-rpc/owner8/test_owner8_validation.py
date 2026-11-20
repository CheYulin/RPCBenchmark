"""Fresh eight-owner synthetic behavior fixtures; no process/network/SDK actions."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import validate_owner8_arrival as validate
import validate_owner8_case as case
from owner8_fixtures import PROFILE, fixture, refresh, delay, rpc_fixture
from owner8_case_fixture import write_case


class Arrivals(unittest.TestCase):
    def test_valid_new_geometry_and_input_immutable(self):
        metric, rows = fixture()
        original = copy.deepcopy((metric, rows))
        result = validate.validate_arrivals(metric, rows, "get", smoke=True)
        self.assertTrue(result["valid"], result.get("error_counts"))
        self.assertEqual(result["profile"]["callers"], 8)
        self.assertEqual(result["input_rows"], 384)
        self.assertEqual(result["profile"]["admission_profile"], PROFILE)
        self.assertFalse(result["RPC30_accepted"])
        self.assertEqual((metric, rows), original)

    def test_explicit_pacing_profile_typed_and_old_profile_rejected(self):
        metric, rows = fixture()
        for changes in ({"protocol": "fixed-owner-bounded-admission-v1"}, {"owner_period_ns": 64000000},
                        {"early_wake_ns": True}, {"pacing_policy": "sleep_until"}):
            metric["admission_profile"] = {**PROFILE, **changes}
            self.assertFalse(validate.validate_arrivals(metric, rows, "get", smoke=True)["valid"])
        metric["admission_profile"] = {key: value for key, value in PROFILE.items() if key != "pacing_policy"}
        self.assertFalse(validate.validate_arrivals(metric, rows, "get", smoke=True)["valid"])

    def test_owner_stride_schedule_and_loss_fail_closed(self):
        for kind in ("owner", "schedule", "missing", "error"):
            metric, rows = fixture()
            if kind == "owner": rows[8]["caller"] = 1
            elif kind == "schedule": rows[8]["scheduled_ns"] += 1
            elif kind == "missing": rows.pop()
            else: rows[8]["status"] = 1
            result = validate.validate_arrivals(metric, rows, "get", smoke=True)
            self.assertFalse(result["valid"], kind)

    def test_soft_threshold_and_smoke_zero_budget(self):
        for elapsed, valid in ((1000000, True), (1000001, False)):
            metric, rows = fixture()
            delay(rows, {(1, 0)}, elapsed)
            refresh(metric, rows)
            self.assertEqual(validate.validate_arrivals(metric, rows, "get", smoke=True)["valid"], valid)

    def test_formal_four_and_thirty_two_budget_boundaries(self):
        for warm, measured, valid in ((4, 32, True), (5, 32, False), (4, 33, False)):
            metric, rows = fixture(smoke=False)
            delay(rows, {(1, x) for x in range(warm)} | {(2, x) for x in range(measured)}, 2000000)
            refresh(metric, rows)
            result = validate.validate_arrivals(metric, rows, "get")
            self.assertEqual(result["valid"], valid, result.get("error_counts"))
            self.assertEqual(result["input_rows"], 36864)

    def test_hard_sixteen_ms_boundary_preserves_base_error(self):
        for elapsed, valid in ((15999999, True), (16000000, False)):
            metric, rows = fixture(smoke=False)
            row = next(r for r in rows if r["phase_id"] == 1 and r["arrival_id"] == 4095)
            delay(rows, {(1, 4095)}, elapsed)
            row["api_end_ns"] = row["validation_end_ns"] = row["observer_end_ns"] = row["api_begin_ns"]
            refresh(metric, rows)
            result = validate.validate_arrivals(metric, rows, "get")
            self.assertEqual(result["valid"], valid, result.get("error_counts"))
            if not valid: self.assertIn("dispatch_budget", result["errors"])

    def test_last_call_observer_debt_includes_equality(self):
        for elapsed, valid in ((15999999, True), (16000000, False)):
            metric, rows = fixture()
            row = rows[-1]
            row["api_end_ns"] = row["validation_end_ns"] = row["observer_end_ns"] = row["scheduled_ns"] + elapsed
            refresh(metric, rows)
            self.assertEqual(validate.validate_arrivals(metric, rows, "get", smoke=True)["valid"], valid)

    def test_checked_time_cannot_hide_soft_lateness_and_counters_typed(self):
        metric, rows = fixture()
        rows[0]["dispatch_checked_ns"] -= 1
        self.assertIn("bounded_checked_begin_identity", validate.validate_arrivals(metric, rows, "get", smoke=True)["errors"])
        metric, rows = fixture()
        metric["phases"][0]["soft_late_count"] = False
        self.assertFalse(validate.validate_arrivals(metric, rows, "get", smoke=True)["valid"])

    def test_hard_rejection_uses_real_checked_shape_without_false_counter_error(self):
        metric, rows = fixture()
        row = rows[0]
        row.update(state="rejected_late", attempted=False, status=None,
                   dispatch_checked_ns=row["scheduled_ns"] + 16000000,
                   api_begin_ns=None, api_end_ns=None, validation_end_ns=None, observer_end_ns=None)
        refresh(metric, rows)
        result = validate.validate_arrivals(metric, rows, "get", smoke=True)
        self.assertFalse(result["valid"])
        self.assertEqual(metric["phases"][0]["soft_late_count"], 1)
        self.assertNotIn("bounded_soft_late_count_counter", result["errors"])


def rpc_result(backend="brpc", primary=100000):
    metric, rows, ends, rpcs, slots = rpc_fixture(backend=backend, primary=primary)
    a = validate.validate_arrivals(metric, rows, "get", smoke=True)
    return validate.validate_rpc_rows(a, rows, ends, rpcs, slots, "get"), (a, rows, ends, rpcs, slots)


class RPCs(unittest.TestCase):
    def test_all_eight_slots_and_384_rows_join_exactly(self):
        result, _ = rpc_result()
        self.assertTrue(result["rpc_valid"], result.get("error_counts"))
        self.assertEqual(result["phases"]["2"]["primary_rpc"]["count"], 256)
        self.assertEqual(result["phases"]["2"]["primary_rpc"]["p991_ns"], 100000)

    def test_missing_wrong_owner_and_error_rpc_never_filtered(self):
        for kind in ("missing", "owner", "error"):
            _, data = rpc_result()
            arrivals, rows, ends, rpcs, slots = data
            if kind == "missing": rpcs.pop()
            elif kind == "owner": rpcs[0]["caller_index"] = 7
            else: rpcs[0]["controller_error"] = 1
            self.assertFalse(validate.validate_rpc_rows(arrivals, rows, ends, rpcs, slots, "get")["rpc_valid"])

    def test_seventy_percent_gate_and_rank_sensitivity_fail_closed(self):
        quartet = [rpc_result(backend=b, primary=p)[0] for b, p in
                   (("brpc", 100000), ("coro", 70000), ("coro", 70000), ("brpc", 100000))]
        self.assertTrue(validate.decide_backend_comparison(quartet)["pass"])
        quartet[1]["phases"]["2"]["primary_rpc"]["p991_ns"] = 70001
        self.assertFalse(validate.decide_backend_comparison(quartet)["pass"])

    def test_numeric_gate_rejects_forged_32_profile(self):
        quartet = [rpc_result(backend=b, primary=p)[0] for b, p in
                   (("brpc", 100000), ("coro", 70000), ("coro", 70000), ("brpc", 100000))]
        for row in quartet: row["profile"]["callers"] = 32
        self.assertFalse(validate.decide_backend_comparison(quartet)["pass"])

    def test_observer_calibration_retains_exact_three_percent_boundary(self):
        for service, passes in ((309000, True), (309001, False)):
            quartet = []
            for mode in ("off", "enabled", "enabled", "off"):
                if mode == "off":
                    metric, rows = fixture()
                    result = validate.validate_arrivals(metric, rows, "get", smoke=True)
                else:
                    metric, rows, ends, rpcs, slots = rpc_fixture(service=service)
                    result = validate.validate_arrivals(metric, rows, "get", smoke=True)
                    result = validate.validate_rpc_rows(result, rows, ends, rpcs, slots, "get")
                quartet.append(result)
            self.assertEqual(validate.decide_calibration(quartet)["pass"], passes)


class Containers(unittest.TestCase):
    def qualify(self, root, sdk, digest):
        return case.validate_case(root, "get", "brpc", smoke=True,
                                  expected_sdk_path=sdk, expected_sdk_sha256=digest)

    def test_full_synthetic_enabled_container_and_off_scope(self):
        for mode in ("enabled", "off"):
            with tempfile.TemporaryDirectory() as directory:
                root, sdk, digest = write_case(directory, mode)
                try:
                    result = self.qualify(root, sdk, digest)
                except ValueError as error:
                    self.fail("valid synthetic8 container rejected: " + str(error))
                self.assertTrue(result["valid"])
                self.assertIs(result["RPC_available"], mode == "enabled")
                self.assertTrue(result["external_role_proofs_required"])
                self.assertFalse(result["RPC30_accepted"])

    def test_ready_old_owner_count_and_profile_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root, sdk, digest = write_case(directory, "off")
            ready = json.loads((root / "ready.json").read_text())
            ready["callers"] = 32
            (root / "ready.json").write_text(json.dumps(ready))
            with self.assertRaises(ValueError): self.qualify(root, sdk, digest)

    def test_all_sixty_four_readbacks_remain_required(self):
        with tempfile.TemporaryDirectory() as directory:
            root, sdk, digest = write_case(directory, "off")
            metric = json.loads((root / "metric.json").read_text())
            metric["readback_keys"] = 32
            (root / "metric.json").write_text(json.dumps(metric))
            with self.assertRaisesRegex(ValueError, "readback"): self.qualify(root, sdk, digest)

    def test_capacity_is_per_owner_not_global_and_no_bool_metadata(self):
        for key, value in (("normal_ends_per_caller", 384), ("callers", True)):
            with tempfile.TemporaryDirectory() as directory:
                root, sdk, digest = write_case(directory)
                config = json.loads((root / "capture/config.json").read_text())
                config[key] = value
                (root / "capture/config.json").write_text(json.dumps(config))
                with self.assertRaisesRegex(ValueError, "capture config"): self.qualify(root, sdk, digest)

    def test_prefix_missing_byte_unknown_file_and_seal_fail_closed(self):
        for kind in ("byte", "unknown", "seal"):
            with tempfile.TemporaryDirectory() as directory:
                root, sdk, digest = write_case(directory)
                if kind == "byte":
                    file = root / "capture/measure-caller-0.rpc.bin"
                    file.write_bytes(file.read_bytes()[:-1])
                elif kind == "unknown": (root / "capture/extra.bin").write_bytes(b"x")
                else:
                    value = json.loads((root / "capture/final.json").read_text())
                    value["close_ok"] = False
                    (root / "capture/final.json").write_text(json.dumps(value))
                with self.assertRaises(ValueError): self.qualify(root, sdk, digest)

    def test_cumulative_warm_prefix_must_preserve_exact_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root, sdk, digest = write_case(directory)
            file = root / "capture/measure-caller-0.rpc.bin"
            blob = bytearray(file.read_bytes())
            blob[0] ^= 1
            file.write_bytes(blob)
            with self.assertRaisesRegex(ValueError, "warm raw prefix"): self.qualify(root, sdk, digest)


if __name__ == "__main__":
    unittest.main()
