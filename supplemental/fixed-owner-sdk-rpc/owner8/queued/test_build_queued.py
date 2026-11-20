import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import build_queued as builder


class QueuedBuilder(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sdk = self.root / 'libdatasystem.so'
        self.sdk.write_bytes(b'\x7fELFsynthetic fixture never executed')
        self.include = self.root / 'include'
        for name in builder.parent_builder().parent_builder().SDK_HEADERS:
            path = self.include / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('// header fixture only\n')

    def args(self, output='build'):
        return builder.parser().parse_args(['--sdk-dso', str(self.sdk), '--sdk-include', str(self.include),
            '--compiler', sys.executable, '--output', str(self.root / output)])

    def test_single_parent_stage_and_exact_common_source_closure(self):
        output = self.root / 'stage'
        output.mkdir()
        shared = builder.parent_builder()
        with patch.object(builder, 'parent_builder', return_value=shared), patch.object(shared, 'stage_sources', wraps=shared.stage_sources) as stage:
            result = builder.stage_sources(output)
        self.assertEqual(stage.call_count, 1)
        self.assertFalse((output / 'owner8_callers_sdk.cpp').exists())
        self.assertFalse((output / 'red-semantic-v1/arrival_loop.h').exists())
        self.assertEqual(len(result['source_sha256']), 11)
        self.assertEqual(builder.digest(output / 'owner8_queued_callers_sdk.cpp'),
            '6a2cab7f48f5c3846c26aaa16102f622a510e6309e4a5138be3a5f4e79ee8161')
        self.assertEqual(builder.digest(output / 'capture_helper.cpp'),
            'ff3af0bb8fc8c7c2e05222ec490e74b3922241fb61e01205f70f5da24377a51d')

    def test_stage_only_does_not_need_or_run_compiler(self):
        args = builder.parser().parse_args(['--stage-only', '--compiler', 'missing-compiler', '--output', str(self.root / 'stage')])
        real_run = builder.subprocess.run
        commands = []
        def record(argv, **kwargs):
            commands.append(argv)
            return real_run(argv, **kwargs)
        with patch.object(builder.subprocess, 'run', side_effect=record):
            self.assertEqual(builder.build(args), 0)
        self.assertEqual(len(commands), 1)
        self.assertIn('--fuzz=0', commands[0])
        self.assertFalse(json.loads((args.output / 'build-result.json').read_text())['runtime_executed'])

    def test_existing_output_not_overwritten(self):
        args = self.args()
        args.output.mkdir()
        (args.output / 'keep').write_text('original')
        with self.assertRaisesRegex(ValueError, 'output already exists'):
            builder.build(args)
        self.assertEqual((args.output / 'keep').read_text(), 'original')

    def test_invalid_SDK_is_rejected_before_output_or_process(self):
        args = self.args()
        self.sdk.write_bytes(b'not ELF')
        with patch.object(builder.subprocess, 'run') as run, self.assertRaisesRegex(ValueError, 'must be ELF'):
            builder.build(args)
        run.assert_not_called()
        self.assertFalse(args.output.exists())

    def test_mock_links_only_real_driver_common_plan_and_helper(self):
        args = self.args()
        commands = []
        real_run = builder.subprocess.run
        def run(argv, **kwargs):
            if '--fuzz=0' in argv:
                return real_run(argv, **kwargs)
            commands.append(argv)
            (Path(kwargs['cwd']) / argv[argv.index('-o') + 1]).write_bytes(b'\x7fELFmock output')
            return subprocess.CompletedProcess(argv, 0)
        with patch.object(builder.subprocess, 'run', side_effect=run), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(builder.build(args), 0)
        self.assertEqual(len(commands), 2)
        for command in commands:
            self.assertEqual([name for name in command if name.endswith('.cpp')],
                ['owner8_queued_callers_sdk.cpp', 'fixed_owner_plan.cpp', 'capture_helper.cpp'])
            for flag in ['-std=c++20', '-O2', '-fPIC', '-pie']:
                self.assertIn(flag, command)
            self.assertFalse(any(token in ' '.join(command) for token in ['--wrap', 'provider', 'red-semantic']))
        report = json.loads((args.output / 'build-result.json').read_text())
        self.assertFalse(report['runtime_executed'])
        self.assertFalse(report['supplied_sdk_provenance_qualified'])
        self.assertFalse(report['unit_fixture_sources_linked'])
        self.assertEqual(set(report['binary_sha256']), {'owner8-queued-sdk', 'owner8-queued-sdk-smoke'})


if __name__ == '__main__':
    unittest.main()
