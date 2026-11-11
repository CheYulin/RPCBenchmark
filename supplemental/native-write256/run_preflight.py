#!/usr/bin/env python3
"""Build and verify the two write caps; leave performance windows to the gate runner."""
import argparse
import hashlib
import json
import pathlib
import shutil
import subprocess
import time

from prepare_overlay import prepare

ROOT = pathlib.Path(__file__).resolve().parents[2]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def step(output, name, command, expected=0, timeout=120):
    start = time.monotonic()
    process = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    (output / (name + '.stdout')).write_text(process.stdout)
    (output / (name + '.stderr')).write_text(process.stderr)
    record = {'name': name, 'command': [str(s) for s in command], 'exit': process.returncode,
              'expected_exit': expected, 'seconds': time.monotonic() - start}
    print(json.dumps(record), flush=True)
    if process.returncode != expected:
        raise RuntimeError(f'{name}: expected exit {expected}, got {process.returncode}; see saved logs')
    return record


def flags(cxx, overlay, enabled):
    includes = overlay / 'include'
    result = [cxx, '-std=c++20', '-O2', '-DNDEBUG', '-pthread', '-Wall', '-Wextra', '-Werror',
              '-I' + str(ROOT / 'src'), '-isystem', str(includes),
              '-isystem', str(includes / 'ylt/thirdparty'), '-isystem', str(includes / 'ylt/standalone')]
    if enabled:
        result.append('-DRPC_LAB_CORO_WRITE_256K')
    return result


def unit(args, output):
    output.mkdir(parents=True, exist_ok=False)
    overlay = output / 'ylt-overlay'
    prepare(args.archive, overlay)
    records = []
    for arm, enabled in [('64k', False), ('256k', True)]:
        binary = output / ('write-unit-' + arm)
        command = flags(args.cxx, overlay, enabled) + [str(ROOT / 'tests/coro_write_chunk_test.cpp'), '-o', str(binary)]
        records.append(step(output, 'compile-unit-' + arm, command))
    records.append(step(output, 'unit-old-red', [str(output / 'write-unit-64k')], expected=1))
    red = json.loads((output / 'unit-old-red.stdout').read_text().splitlines()[0])
    if red != {'case': 'ideal', 'calls': 16, 'expected_calls': 4, 'bytes': 1048576}:
        raise RuntimeError('old-cap RED must identify the 16-to-4 call count requirement')
    records.append(step(output, 'unit-old-contract', [str(output / 'write-unit-64k'), '65536']))
    records.append(step(output, 'unit-new-green', [str(output / 'write-unit-256k')]))
    for name in ['unit-old-contract', 'unit-new-green']:
        proof = json.loads((output / (name + '.stdout')).read_text().splitlines()[-1])
        if not proof.get('verified') or proof.get('cases') != 6:
            raise RuntimeError('incomplete fake-stream contract checks')
    (output / 'unit-results.json').write_text(json.dumps(records, indent=2) + '\n')


def native(args, output):
    overlay = output / 'ylt-overlay'
    overlay_manifest = json.loads((overlay / 'overlay-manifest.json').read_text())
    if sha(overlay / 'include/ylt/coro_io/coro_io.hpp') != overlay_manifest['patched_header_sha256']:
        raise RuntimeError('overlay changed since preparation')
    if not (output / 'unit-results.json').exists():
        raise RuntimeError('run the unit stage first')
    entry = args.entry.resolve() if args.entry else ROOT / 'src/coro_pipeline.cpp'
    entry_relative = entry.relative_to(ROOT)
    sources = ['src/lab.cpp', 'src/lab.h', 'src/coro_pipeline.cpp', 'tests/pipeline_smoke.py',
               'tests/pipeline_lifecycle.py', 'tests/coro_write_chunk_test.cpp',
               'supplemental/native-write256/prepare_overlay.py',
               'supplemental/native-write256/run_preflight.py',
               'supplemental/native-write256/coro-write256.patch', str(entry_relative)]
    source_hashes = {name: sha(ROOT / name) for name in sorted(set(sources))}
    records, binaries = [], {}
    for arm, enabled in [('64k', False), ('256k', True)]:
        binary = output / ('rpc_coro_write-' + arm)
        command = flags(args.cxx, overlay, enabled) + [str(ROOT / 'src/lab.cpp'), str(entry), '-o', str(binary)]
        records.append(step(output, 'compile-native-' + arm, command))
        for check in ['pipeline_smoke.py', 'pipeline_lifecycle.py']:
            records.append(step(output, arm + '-' + check, ['python3', str(ROOT / 'tests' / check), str(binary)], timeout=45))
        binaries[arm] = binary
    if source_hashes != {name: sha(ROOT / name) for name in source_hashes}:
        raise RuntimeError('source changed during compilation/preflight')
    (output / 'native-results.json').write_text(json.dumps(records, indent=2) + '\n')
    frozen = output / 'frozen'
    frozen.mkdir(exist_ok=False)
    for arm, binary in binaries.items():
        shutil.copy2(binary, frozen / ('rpc_coro_write-' + arm))
    for name in source_hashes:
        target = frozen / 'source' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, target)
    manifest = {'base_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                'source_sha256': source_hashes, 'overlay': overlay_manifest,
                'binary_sha256': {arm: sha(frozen / ('rpc_coro_write-' + arm)) for arm in binaries},
                'compiler': subprocess.check_output([args.cxx, '--version'], text=True).splitlines()[0],
                'native_entry': str(entry_relative), 'native_preflight': records,
                'unit_preflight': json.loads((output / 'unit-results.json').read_text()),
                'performance_windows_run': False}
    (frozen / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print('FROZEN ' + str(frozen), flush=True)
    print(json.dumps(manifest['binary_sha256']), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--stage', required=True, choices=['unit', 'native'])
    parser.add_argument('--archive', type=pathlib.Path)
    parser.add_argument('--build-dir', required=True, type=pathlib.Path)
    parser.add_argument('--entry', type=pathlib.Path)
    parser.add_argument('--cxx', default='/usr/bin/g++')
    args = parser.parse_args()
    version = subprocess.check_output([args.cxx, '-dumpfullversion'], text=True).strip()
    if not version.startswith('11.'):
        raise RuntimeError('this controlled experiment requires GCC 11; found ' + version)
    output = args.build_dir.resolve()
    if args.stage == 'unit':
        if args.archive is None:
            parser.error('--archive is required for unit preparation')
        unit(args, output)
    else:
        native(args, output)


if __name__ == '__main__':
    main()
