"""Strict container and exclusive-output checks; synthetic files only."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from fixed_case_fixture import create_case, dump_json
from validate_fixed_case import validate_case, main


class CliTests(unittest.TestCase):
    def make(self, **kwargs):
        temp = tempfile.TemporaryDirectory(prefix="fixed-cli-test-")
        self.addCleanup(temp.cleanup)
        return create_case(Path(temp.name), **kwargs)

    def validate(self, f, smoke=True):
        return validate_case(f["case"], f["metric"]["operation"], f["metric"]["backend"], smoke=smoke,
                             expected_sdk_path=f["sdk"], expected_sdk_sha256=f["sha"])

    def args(self, f, output):
        return ["--case", str(f["case"]), "--operation", f["metric"]["operation"],
                "--backend", f["metric"]["backend"], "--smoke", "--expected-sdk-path", str(f["sdk"]),
                "--expected-sdk-sha256", f["sha"], "--output", str(output)]

    def test_unknown_capture_files_cannot_be_silently_ignored(self):
        f = self.make()
        (f["case"] / "capture/unexpected-caller-32.rpc.bin").write_bytes(b"anomalous unindexed rows")
        with self.assertRaises(ValueError):
            self.validate(f)

    def test_nested_integer_fields_cannot_accept_json_booleans(self):
        for field in ("phase_ids", "normal_prefixes_reset_between_phases"):
            with self.subTest(field=field):
                f = self.make()
                path = f["case"] / "capture/config.json"
                value = json.loads(path.read_text())
                value[field] = {"warmup": True, "measure": 2} if field == "phase_ids" else 0
                dump_json(path, value)
                with self.assertRaises(ValueError):
                    self.validate(f)

    def test_cli_exclusive_output_and_no_raw_changes(self):
        f = self.make(mode="off")
        before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in f["case"].rglob("*") if p.is_file()}
        output = f["case"].parent / "validation.json"
        self.assertEqual(main(self.args(f, output)), 0)
        result = json.loads(output.read_text())
        self.assertTrue(result["valid"])
        self.assertTrue(result["external_role_proofs_required"])
        self.assertFalse(result["RPC30_accepted"])
        self.assertEqual(before, {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in f["case"].rglob("*") if p.is_file()})
        frozen = output.read_bytes()
        with self.assertRaises(ValueError):
            main(self.args(f, output))
        self.assertEqual(frozen, output.read_bytes())

    def test_cli_unknown_fields_or_duplicate_json_fail_closed(self):
        for content in ('{"scope":1,"scope":2}', '{"value":NaN}'):
            with self.subTest(content=content):
                f = self.make()
                (f["case"] / "metric.json").write_text(content)
                output = f["case"].parent / "invalid.json"
                self.assertEqual(main(self.args(f, output)), 1)
                result = json.loads(output.read_text())
                self.assertFalse(result["valid"])
                self.assertFalse(result["RPC30_accepted"])
        f = self.make()
        path = f["case"] / "capture/measure-prefix.json"
        value = json.loads(path.read_text())
        value["unknown_container"] = 1
        dump_json(path, value)
        with self.assertRaises(ValueError):
            self.validate(f)

    def test_formal_profile_global_36864_and_owner_1152(self):
        f = self.make(smoke=False)
        config = json.loads((f["case"] / "capture/config.json").read_text())
        self.assertEqual(config["selected_ids_both_phases"], 36_864)
        self.assertEqual(config["normal_ends_per_caller"], 1_152)
        self.assertEqual(config["normal_records_per_caller"], 18_432)
        result = self.validate(f, smoke=False)
        self.assertTrue(result["valid"])
        self.assertEqual(result["input_rows"], 36_864)
        self.assertTrue(result["rpc_container_qualified"])
        self.assertFalse(result["RPC30_accepted"])


if __name__ == "__main__":
    unittest.main()
