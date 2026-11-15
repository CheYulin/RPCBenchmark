"""Build the isolated, rejected singleflight experiment; root CMake is unchanged."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

BASE_SHA = 'c616782568336a0398640b80eed3fd501a72291098c95e468dd3a5730a16e923'
MODULE = Path(__file__).resolve().parent
REPO = MODULE.parents[1]

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ylt-include', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--cxx', default='g++')
    args = parser.parse_args()
    include = args.ylt_include.resolve()
    header = include / 'ylt/coro_rpc/impl/coro_rpc_client.hpp'
    if not header.is_file():
        parser.error('--ylt-include must contain ylt/coro_rpc/impl/coro_rpc_client.hpp')
    if sha(header) != BASE_SHA:
        raise SystemExit('Wrong YLT base header; no output directory created.')
    out = args.out.resolve()
    if out.exists():
        parser.error('--out must be a new directory; existing files are preserved')
    out.mkdir(parents=True, exist_ok=False)
    shutil.copytree(include, out / 'include', symlinks=False)
    subprocess.run([sys.executable, str(MODULE / 'apply_overlay.py'), '--header', str(header),
                    '--output-dir', str(out / 'generated')], check=True)
    shutil.copy2(out / 'generated/coro_rpc_client-single.hpp', out / 'include/ylt/coro_rpc/impl/coro_rpc_client.hpp')
    sources = {'client.cpp': MODULE / 'client.cpp', 'singleflight_test.cpp': MODULE / 'singleflight_test.cpp',
               'coro.cpp': REPO / 'src/coro.cpp', 'lab.cpp': REPO / 'src/lab.cpp', 'lab.h': REPO / 'src/lab.h',
               'caller_io_drain.h': REPO / 'supplemental/caller-owned-io/caller_io_drain.h',
               'run_matrix.py': REPO / 'tools/run_matrix.py', 'run_screen.py': MODULE / 'run_screen.py',
               'screen-plan.json': MODULE / 'screen-plan.json'}
    for name, source in sources.items():
        shutil.copy2(source, out / name)
    common = [args.cxx, '-std=c++20', '-O2', '-DNDEBUG', '-g1', '-fno-omit-frame-pointer',
              '-include', 'cstdint', '-include', 'algorithm', '-I', '.', '-I', 'include',
              '-isystem', 'include/ylt/thirdparty', '-isystem', 'include/ylt/standalone', '-pthread']
    commands = [common + ['singleflight_test.cpp', '-o', 'singleflight_test'],
                common + ['client.cpp', 'lab.cpp', '-o', 'perf-client'],
                common + ['coro.cpp', 'lab.cpp', '-o', 'perf-server']]
    (out / 'build-commands.json').write_text(json.dumps(commands, indent=2) + '\n')
    for index, command in enumerate(commands):
        with (out / f'compile-{index}.stdout').open('w') as stdout, (out / f'compile-{index}.stderr').open('w') as stderr:
            subprocess.run(command, cwd=out, stdout=stdout, stderr=stderr, check=True)
    names = [*sources, 'include/ylt/coro_rpc/impl/coro_rpc_client.hpp', 'perf-client', 'perf-server']
    manifest = {name: sha(out / name) for name in names}
    (out / 'perf-source-inputs-v2.json').write_text(json.dumps(manifest, indent=2) + '\n')
    if sha(header) != BASE_SHA:
        raise RuntimeError('Input header changed during the build.')
    print(json.dumps({'status': 'BUILT_NOT_RUN_OR_PROMOTED', 'out': str(out), 'base_sha256': BASE_SHA}))

if __name__ == '__main__':
    main()
