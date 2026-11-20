#!/usr/bin/env python3
"""Stage the optional queued profile or compile real-SDK drivers; never run them."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

HERE = Path(__file__).resolve().parent
LOCAL = ('owner8_queued_callers_sdk.cpp', 'owner8_queued_admission.h', 'owner8_queued_admission_test.cpp')


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def parent_builder():
    identities = json.loads((HERE / 'source-identities.json').read_text())
    for name, expected in identities['parent_inputs'].items():
        if digest(HERE / name) != expected:
            raise ValueError('parent source mismatch: ' + name)
    spec = importlib.util.spec_from_file_location('queued_owner8_parent_builder', HERE.parent / 'build_owner8.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def stage_sources(output, helper_base=None):
    parent = parent_builder()
    shared = parent.parent_builder()
    identities = json.loads((HERE / 'source-identities.json').read_text())
    for name in LOCAL:
        shared.require(digest(HERE / name) == identities['queued_sources'][name], 'queued source mismatch: ' + name)
    staged = parent.stage_sources(output, helper_base=helper_base)
    for name in ('owner8_callers_sdk.cpp', 'owner8_admission.h', 'owner8_profile_test.cpp'):
        (output / name).unlink()
        staged['source_sha256'].pop(name)
    shutil.rmtree(output / 'red-semantic-v1')
    for name, expected in identities['staged_common_sources'].items():
        shared.require(digest(output / name) == expected, 'common staged source mismatch: ' + name)
    for name in LOCAL:
        shutil.copyfile(HERE / name, output / name)
        staged['source_sha256'][name] = digest(output / name)
    red = output / 'red-semantic-v1'
    red.mkdir()
    header = (output / 'owner8_queued_admission.h').read_text()
    before = '            if (row.head_enter_ns > row.begin_ns) {'
    injected = """            if (row.begin_ns - row.scheduled_ns > OwnerPeriodNs - 1) {
                row.state = State::RejectedLate;
                abort.store(true, std::memory_order_release);
                continue;
            }
"""
    shared.require(header.count(before) == 1, 'RED late-policy anchor')
    header = header.replace(before, injected + before)
    before = '            call(row);\n'
    injected = """            if (!ObserverWithinPeriod(row)) {
                abort.store(true, std::memory_order_release);
            }
"""
    shared.require(header.count(before) == 1, 'RED observer-policy anchor')
    (red / 'owner8_queued_admission.h').write_text(header.replace(before, before + injected))
    shutil.copyfile(output / 'owner8_queued_admission_test.cpp', red / 'owner8_queued_admission_test.cpp')
    staged['semantic_RED_sha256'] = {path.name: digest(path) for path in sorted(red.iterdir())}
    shared.require(staged['semantic_RED_sha256'] == identities['semantic_RED_sources'], 'RED source mismatch')
    staged['scope'] = 'optional_queued_source_staging_only_not_runtime_qualification'
    return staged


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--output', type=Path, required=True, help='new isolated output directory')
    result.add_argument('--stage-only', action='store_true', help='stage sources and RED fixture without compiling')
    result.add_argument('--sdk-dso', type=Path, help='real ELF SDK DSO; required unless stage-only')
    result.add_argument('--sdk-include', type=Path, help='public SDK headers; required unless stage-only')
    result.add_argument('--compiler', default='c++', help='one C++20 compiler executable')
    result.add_argument('--helper-base', type=Path, help='original pinned ds-latency30 helper directory')
    result.add_argument('--library-dir', type=Path, action='append', default=[], help='extra link/runtime library directory; repeatable')
    return result


def build(args):
    shared = parent_builder().parent_builder()
    output = args.output.expanduser().absolute()
    shared.require(not os.path.lexists(output), 'output already exists; no files overwritten')
    shared.require(output.parent.is_dir(), 'output parent must already exist')
    sdk, headers, library_dirs, compiler = None, {}, [], None
    if not args.stage_only:
        shared.require(args.sdk_dso is not None and args.sdk_include is not None, 'SDK DSO and headers are required')
        sdk = args.sdk_dso.expanduser().resolve(strict=True)
        shared.require(sdk.is_file(), 'SDK DSO must be a regular file')
        with sdk.open('rb') as stream:
            shared.require(stream.read(4) == b'\x7fELF', 'SDK DSO must be ELF')
        include = shared.directory(args.sdk_include, 'SDK include')
        library_dirs = [shared.directory(sdk.parent, 'SDK library')]
        for value in args.library_dir:
            directory = shared.directory(value, 'additional library')
            if directory not in library_dirs:
                library_dirs.append(directory)
        for name in shared.SDK_HEADERS:
            shared.require((include / name).is_file(), 'required SDK public header is missing: ' + name)
            headers[name] = digest(include / name)
        compiler = shutil.which(os.path.expanduser(args.compiler))
        shared.require(compiler is not None, 'C++ compiler executable is unavailable')
        compiler = str(Path(compiler).resolve(strict=True))
    output.mkdir()
    staged = stage_sources(output, args.helper_base)
    report = dict(success=False, profile='fixed-owner8-queued-arrival-v1', **staged,
                  commands=[], binary_sha256={}, runtime_executed=False, supplied_sdk_provenance_qualified=False,
                  unit_fixture_sources_linked=False, complete_toolchain_closure_claimed=False, RPC30_accepted=False)
    def save():
        (output / 'build-result.json').write_text(json.dumps(report, indent=2) + '\n')
    save()
    if args.stage_only:
        report['success'] = True
        save()
        return 0
    sdk_hash = digest(sdk)
    report.update(sdk_sha256=sdk_hash, sdk_public_header_sha256=headers)
    common = [compiler, '-std=c++20', '-O2', '-pthread', '-Wall', '-Wextra', '-Werror', '-fPIC', '-pie',
              '-I', str(include), '-I', str(output)]
    links = []
    for directory in library_dirs:
        links += ['-L', str(directory), '-Wl,-rpath,' + str(directory), '-Wl,-rpath-link,' + str(directory)]
    for label, suffix, defines in [('formal', '', []), ('smoke', '-smoke', ['-DDS_FIXED_OWNER_SMOKE'])]:
        binary = 'owner8-queued-sdk' + suffix
        command = common + defines + ['owner8_queued_callers_sdk.cpp', 'fixed_owner_plan.cpp', 'capture_helper.cpp',
                                      str(sdk), '-o', binary, '-ldl', *links]
        row = dict(label=label, argv=command, returncode=None)
        report['commands'].append(row)
        save()
        with (output / (label + '.stdout')).open('wb') as out, (output / (label + '.stderr')).open('wb') as err:
            process = subprocess.run(command, cwd=output, stdout=out, stderr=err, check=False)
        row['returncode'] = process.returncode
        save()
        if process.returncode:
            return process.returncode if 0 < process.returncode < 126 else 1
        shared.require((output / binary).is_file(), 'compiler returned success without an output binary')
        report['binary_sha256'][binary] = digest(output / binary)
        shared.require(digest(sdk) == sdk_hash, 'SDK changed while building')
        shared.require(all(digest(include / name) == expected for name, expected in headers.items()), 'SDK headers changed')
        shared.require(all(digest(output / name) == expected for name, expected in staged['source_sha256'].items()), 'staged source changed')
        shared.require(all(digest(HERE / name) == expected for name, expected in json.loads((HERE / 'source-identities.json').read_text())['queued_sources'].items()), 'queued source changed')
        parent_builder()
        save()
    report['success'] = True
    save()
    print('OWNER8_QUEUED_FORMAL_AND_SMOKE_BUILT_NOT_RUN')
    return 0


def main():
    try:
        return build(parser().parse_args())
    except (OSError, ValueError) as error:
        print('Input validation or build setup failed: ' + str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
