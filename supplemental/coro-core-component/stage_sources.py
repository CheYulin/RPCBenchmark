#!/usr/bin/env python3
"""Stage one existing fixed-owner closure and the optional echo component sources."""
from pathlib import Path
import argparse
import hashlib
import importlib.util
import json
import os
import shlex

from generate_core_variants import variants, write_variants

HERE = Path(__file__).resolve().parent
HEADER_SHA256 = '91d46287b36a964bf88aa42ef42a454ad36507a9cc8bac11f15f399dac653210'
BUILDER_SHA256 = '22b18f5fb595d45dafb89071b77e63ec773c44500b67ea9718fe3acbfa714e56'
SOURCES = {
    'core_shared_owner_probe.cpp': 'a306ee69d08db3a0d86ffdda6485ff16d765808bad90cae1a10217fc0e123dd8',
    'echo_server_only.cpp': '261da19853436e114fe84f7c87d29b7b8205da4788d333f0cbd3d4c3ec9b78c3',
}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def compile_arguments(database, command_record):
    if not database and not command_record:
        return []
    if database:
        rows = json.loads(database.read_text())
        row = next(row for row in rows if row['file'].endswith('/coro_unary.cpp'))
        argv = row.get('arguments') or shlex.split(row['command'])
        directory = Path(row['directory']).resolve(strict=True)
    else:
        row = json.loads(command_record.read_text())
        argv = row['argv']
        directory = Path(row['cwd']).resolve(strict=True)
    result = []
    index = 1
    while index < len(argv):
        token = argv[index]
        kind, path = None, None
        if token in ('-o', '-MF', '-MT', '-MQ'):
            index += 2
            continue
        if token in ('-c', '-MD', '-MMD', '-MP') or (not token.startswith('-') and token.endswith(('.cpp', '.cc', '.cxx'))):
            index += 1
            continue
        if token in ('-I', '-isystem', '-iquote'):
            index += 1
            kind, path = token, argv[index]
        elif token.startswith('-I'):
            kind, path = '-I', token[2:]
        if kind:
            value = Path(path)
            value = value if value.is_absolute() else directory / value
            value = value.resolve(strict=True)
            if not value.is_dir():
                raise ValueError('compiler include is not a directory')
            result.extend([kind, str(value)])
        else:
            result.append(token)
        index += 1
    if '-O2' not in result or not any(arg in result for arg in ('-std=c++20', '-std=gnu++20')) or '-fPIC' not in result:
        raise ValueError('matching core command must retain C++20, O2 and PIC flags')
    return result


def stage(args):
    source = args.datasystem_source.resolve(strict=True)
    core = source / 'src/datasystem/common/rpc/coro_unary.cpp'
    header = source / 'src/datasystem/common/rpc/coro_unary.h'
    variants(core.read_bytes())
    if sha(header.read_bytes()) != HEADER_SHA256:
        raise ValueError('DataSystem core header SHA256 mismatch')
    component = {name: (HERE / name).read_bytes() for name in SOURCES}
    if any(sha(data) != SOURCES[name] for name, data in component.items()):
        raise ValueError('component source SHA256 mismatch')
    flags = compile_arguments(args.compile_commands, args.core_compile_command)
    fixed = args.fixed_owner_module.resolve(strict=True)
    if sha((fixed / 'build_local.py').read_bytes()) != BUILDER_SHA256:
        raise ValueError('fixed-owner builder SHA256 mismatch')
    spec = importlib.util.spec_from_file_location('component_fixed_owner_builder', fixed / 'build_local.py')
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    builder.source_inputs(args.helper_base)
    output = args.output.absolute()
    if os.path.lexists(output) or not output.parent.is_dir():
        raise ValueError('output must be new, with an existing parent')
    output.mkdir()
    closure = builder.stage_sources(output, args.helper_base)
    if (closure['source_sha256']['fixed_owner_plan.h'] != '56c6cd83d3c840976da517977d79979204044d639bdfa479059baf12340eae2d'
            or closure['source_sha256']['fixed_owner_plan.cpp'] != '64ef183f73dc7d79e096b66a28be5bc568c1ae2a441083def4a521a51df71962'):
        raise ValueError('fixed-owner plan SHA256 mismatch')
    for name, data in {**component, 'coro_unary.h': header.read_bytes()}.items():
        with (output / name).open('xb') as stream:
            stream.write(data)
    generated = write_variants(core, output)
    (output / 'compile-flags.json').write_text(json.dumps(flags, indent=2) + '\n')
    report = dict(status='SOURCES_STAGED_NOT_COMPILED_OR_RUN', fixed_owner_closure=closure,
        fixed_owner_stage_invocations=1, component_source_sha256=SOURCES, generated_core_sha256=generated,
        DataSystem_header_sha256=HEADER_SHA256, matching_core_flags_reused=bool(flags),
        compiler_arguments=flags, runtime_executed=False, supplied_sdk_qualified=False,
        RPC30_accepted=False)
    (output / 'component-stage-result.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'status': report['status'], 'fixed_owner_stage_invocations': 1}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--datasystem-source', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    commands = parser.add_mutually_exclusive_group()
    commands.add_argument('--compile-commands', type=Path, help='matching DataSystem compile database containing coro_unary.cpp')
    commands.add_argument('--core-compile-command', type=Path, help='matching core command JSON with argv and cwd')
    parser.add_argument('--fixed-owner-module', type=Path, default=HERE.parent / 'fixed-owner-sdk-rpc')
    parser.add_argument('--helper-base', type=Path, default=HERE.parent / 'ds-latency30')
    args = parser.parse_args()
    try:
        stage(args)
    except (OSError, ValueError, KeyError, IndexError, StopIteration) as error:
        parser.exit(2, str(error) + '\n')


if __name__ == '__main__':
    main()
