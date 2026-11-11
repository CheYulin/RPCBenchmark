#!/usr/bin/env python3
"""Validate one bounded mixed functional run per frozen arm; do not compare timings."""
import argparse
import hashlib
import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tests'))
sys.path.insert(0, str(ROOT / 'supplemental/native-bulk-io'))
from pipeline_smoke import counters, server
from run_gate import validate_output


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--frozen', required=True, type=pathlib.Path)
    parser.add_argument('--out', required=True, type=pathlib.Path)
    args = parser.parse_args()
    frozen, output = args.frozen.resolve(), args.out.resolve()
    output.mkdir(parents=True, exist_ok=False)
    manifest = json.loads((frozen / 'manifest.json').read_text())
    records = []
    for arm in ['64k', '256k']:
        binary = frozen / ('rpc_coro_write-' + arm)
        if sha(binary) != manifest['binary_sha256'][arm]:
            raise RuntimeError('frozen binary hash drift')
        with server(str(binary)) as port:
            command = [str(binary), 'client', '--port', port, '--workers', '4', '--concurrency', '32',
                       '--payload', '4096', '--pipeline', '1', '--caller', 'coroutine', '--warmup', '1',
                       '--seconds', '1', '--timeout-ms', '1000', '--mixed']
            process = subprocess.run(command, capture_output=True, text=True, timeout=8)
            (output / (arm + '.stdout')).write_text(process.stdout)
            (output / (arm + '.stderr')).write_text(process.stderr)
            if process.returncode != 0:
                raise RuntimeError(f'{arm} mixed functional exit {process.returncode}')
            config, groups = validate_output(process.stdout, 'mixed', 1)
            warmup = counters(process)
            if warmup['rpc_errors'] or warmup['mismatches']:
                raise RuntimeError('mixed warmup failure')
            records.append({'arm': arm, 'command': command, 'binary_sha256': sha(binary),
                            'exit': process.returncode, 'config': config, 'groups': groups, 'warmup': warmup})
    proof = {'functional_only': True, 'comparison_performed': False, 'records': records,
             'frozen_manifest_sha256': sha(frozen / 'manifest.json'),
             'validator_sha256': sha(ROOT / 'supplemental/native-bulk-io/run_gate.py'),
             'check_source_sha256': sha(pathlib.Path(__file__))}
    (output / 'mixed-preflight.json').write_text(json.dumps(proof, indent=2) + '\n')
    print(json.dumps({'verified': True, 'arms': 2, 'groups_each': 2, 'functional_only': True}))


if __name__ == '__main__':
    main()
