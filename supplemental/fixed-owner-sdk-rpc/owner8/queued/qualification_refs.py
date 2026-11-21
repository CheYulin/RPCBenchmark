"""Immutable full case evidence; compact rows contain only a path and SHA."""
import hashlib
import json
from pathlib import Path
import re

from validate_owner8_queued_arrival import ADMISSION_PROFILE

MAX_BYTES = 128 * 1024 * 1024


def _case(evidence_dir, row):
    from campaign import planned_rows
    if type(row) is not dict or type(row.get('index')) is not int or not 1 <= row['index'] <= 12:
        raise ValueError('queued case identity')
    expected = planned_rows()[row['index'] - 1]
    if not all(_typed_equal(row.get(key), expected[key]) for key in ('backend', 'operation', 'mode', 'smoke')):
        raise ValueError('queued campaign ordinal identity')
    suffix = '-smoke' if row['smoke'] else ''
    relative = f"{row['index']}-{row['backend']}-{row['operation']}-{row['mode']}{suffix}/case-validation.json"
    path = Path(evidence_dir) / relative
    if not path.parent.is_dir() or path.parent.is_symlink() or path.is_symlink():
        raise ValueError('case evidence must be a regular owned path')
    return relative, path


def _typed_equal(actual, expected):
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(_typed_equal(actual[key], value) for key, value in expected.items())
    return actual == expected


def _qualified(value, row, *, expected_sdk_sha256):
    if type(expected_sdk_sha256) is not str or re.fullmatch('[0-9a-f]{64}', expected_sdk_sha256) is None:
        raise ValueError('expected SDK SHA256')
    expected_profile = dict(rate_per_second=500, callers=8, payload_bytes=4096,
        warm_calls_per_caller=16 if row['smoke'] else 512, measure_calls_per_caller=32 if row['smoke'] else 4096, admission_profile=ADMISSION_PROFILE)
    enabled = row['mode'] == 'enabled'
    expected = dict(valid=True, diagnostic_valid=True, external_role_proofs_required=True,
        RPC30_accepted=False, backend=row['backend'], operation=row['operation'], capture_mode=row['mode'],
        RPC_available=enabled, rpc_valid=enabled, rpc_container_qualified=enabled,
        input_rows=384 if row['smoke'] else 36864, expected_sdk_sha256=expected_sdk_sha256, profile=expected_profile)
    if type(value) is not dict or not all(_typed_equal(value.get(key), wanted) for key, wanted in expected.items()):
        raise ValueError('qualified file identity/profile/runtime flags')


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('duplicate qualification JSON key')
        result[key] = value
    return result


def store_qualification(evidence_dir, row, qualification):
    relative, path = _case(evidence_dir, row)
    if type(qualification) is not dict:
        raise ValueError('qualification must be a dictionary')
    data = (json.dumps(qualification, indent=2, allow_nan=False) + '\n').encode()
    if len(data) > MAX_BYTES:
        raise ValueError('full qualification exceeds existing input budget')
    with path.open('xb') as stream:
        stream.write(data)
    return dict(path=relative, sha256=hashlib.sha256(data).hexdigest())


def load_qualification(evidence_dir, row, *, expected_sdk_sha256):
    relative, path = _case(evidence_dir, row)
    reference = row.get('qualification_ref')
    if (type(reference) is not dict or reference.keys() != {'path', 'sha256'}
            or reference['path'] != relative or type(reference['sha256']) is not str
            or re.fullmatch('[0-9a-f]{64}', reference['sha256']) is None):
        raise ValueError('case qualification reference mismatch')
    if not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise ValueError('missing/nonregular/oversize full qualification')
    data = path.read_bytes()
    if len(data) > MAX_BYTES or hashlib.sha256(data).hexdigest() != reference['sha256']:
        raise ValueError('full qualification SHA mismatch')
    def nonfinite(value):
        raise ValueError('nonfinite qualification JSON')
    value = json.loads(data, object_pairs_hook=_pairs, parse_constant=nonfinite)
    _qualified(value, row, expected_sdk_sha256=expected_sdk_sha256)
    return value
