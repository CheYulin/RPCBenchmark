"""Invented SDK hashes and qualification metadata; no actual runtime fixtures."""
import hashlib
from pathlib import Path
import tempfile
import unittest

from campaign import planned_rows
from qualification_refs import ADMISSION_PROFILE, _case, load_qualification, store_qualification


SYNTHETIC_BASELINE_SHA = '56' * 32
SYNTHETIC_NATIVE_SHA = '1770' + 'c' * 60
SYNTHETIC_UNKNOWN_SHA = 'e' * 64


def value(row, sdk_sha):
    smoke = row['smoke']
    return dict(valid=True, diagnostic_valid=True, external_role_proofs_required=True,
        RPC30_accepted=False, backend=row['backend'], operation=row['operation'], capture_mode='enabled',
        RPC_available=True, rpc_valid=True, rpc_container_qualified=True,
        input_rows=384 if smoke else 36864, expected_sdk_sha256=sdk_sha,
        profile=dict(rate_per_second=500, callers=8, payload_bytes=4096,
            warm_calls_per_caller=16 if smoke else 512,
            measure_calls_per_caller=32 if smoke else 4096, admission_profile=ADMISSION_PROFILE))


class Refs(unittest.TestCase):
    def prepare(self, root, row, qualification):
        suffix = '-smoke' if row['smoke'] else ''
        path = Path(root) / f"{row['index']}-{row['backend']}-{row['operation']}-enabled{suffix}"
        path.mkdir()
        row['qualification_ref'] = store_qualification(root, row, qualification)
        return path / 'case-validation.json'

    def selected_load(self, root, row, expected_sdk_sha256):
        try:
            return load_qualification(root, row, expected_sdk_sha256=expected_sdk_sha256)
        except TypeError as error:
            self.fail(f'explicit selected SDK hash API is required: {error}')

    def test_synthetic_baseline_sdk_smoke_and_formal_roundtrip(self):
        with tempfile.TemporaryDirectory() as root:
            for index in (0, 4):
                row = planned_rows()[index]
                result = value(row, SYNTHETIC_BASELINE_SHA)
                self.prepare(root, row, result)
                self.assertEqual(self.selected_load(root, row, SYNTHETIC_BASELINE_SHA), result)

    def test_synthetic_native_sdk_smoke_and_formal_roundtrip(self):
        with tempfile.TemporaryDirectory() as root:
            for index in (2, 5):
                row = planned_rows()[index]
                result = value(row, SYNTHETIC_NATIVE_SHA)
                path = self.prepare(root, row, result)
                original = path.read_bytes()
                self.assertEqual(self.selected_load(root, row, SYNTHETIC_NATIVE_SHA), result)
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(hashlib.sha256(original).hexdigest(), row['qualification_ref']['sha256'])

    def test_swapped_arm_sdk_hashes_are_rejected(self):
        for index, actual, expected in ((4, SYNTHETIC_BASELINE_SHA, SYNTHETIC_NATIVE_SHA),
                                         (5, SYNTHETIC_NATIVE_SHA, SYNTHETIC_BASELINE_SHA)):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as root:
                row = planned_rows()[index]
                self.prepare(root, row, value(row, actual))
                with self.assertRaisesRegex(ValueError, 'identity/profile/runtime'):
                    self.selected_load(root, row, expected)

    def test_unknown_artifact_sdk_hash_is_rejected_with_valid_reference(self):
        with tempfile.TemporaryDirectory() as root:
            row = planned_rows()[5]
            self.prepare(root, row, value(row, SYNTHETIC_UNKNOWN_SHA))
            with self.assertRaisesRegex(ValueError, 'identity/profile/runtime'):
                self.selected_load(root, row, SYNTHETIC_NATIVE_SHA)

    def test_expected_sdk_hash_must_be_typed_lowercase_hex(self):
        with tempfile.TemporaryDirectory() as root:
            row = planned_rows()[4]
            self.prepare(root, row, value(row, SYNTHETIC_BASELINE_SHA))
            for expected in (None, True, 56, '', 'a' * 63, 'A' * 64, 'g' * 64):
                with self.subTest(expected=expected), self.assertRaisesRegex(ValueError, 'expected SDK SHA'):
                    self.selected_load(root, row, expected)

    def test_expected_sdk_hash_is_required_and_keyword_only(self):
        with tempfile.TemporaryDirectory() as root:
            row = planned_rows()[4]
            self.prepare(root, row, value(row, SYNTHETIC_BASELINE_SHA))
            with self.assertRaises(TypeError):
                load_qualification(root, row)
            with self.assertRaises(TypeError):
                load_qualification(root, row, SYNTHETIC_BASELINE_SHA)

    def test_campaign_identity_rejects_wrong_backend_for_ordinal(self):
        with tempfile.TemporaryDirectory() as root:
            row = planned_rows()[4]
            row['backend'] = row['arm'] = 'coro'
            with self.assertRaises(ValueError):
                _case(root, row)

    def test_tampered_reference_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            row = planned_rows()[4]
            path = self.prepare(root, row, value(row, SYNTHETIC_BASELINE_SHA))
            path.write_text('{}\n')
            with self.assertRaisesRegex(ValueError, 'SHA'):
                self.selected_load(root, row, SYNTHETIC_BASELINE_SHA)

    def test_smoke_cannot_borrow_formal_counts_with_matching_reference(self):
        with tempfile.TemporaryDirectory() as root:
            row = planned_rows()[0]
            result = value(row, SYNTHETIC_BASELINE_SHA)
            result['input_rows'] = 36864
            self.prepare(root, row, result)
            with self.assertRaisesRegex(ValueError, 'profile'):
                self.selected_load(root, row, SYNTHETIC_BASELINE_SHA)


if __name__ == '__main__':
    unittest.main()
