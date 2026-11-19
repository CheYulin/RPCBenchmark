"""Builder input tests; no C++ compiler, SDK workflow, or service is run."""
import contextlib
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import build_local as builder


class InputTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.sdk = self.root / "libdatasystem.so"
        self.include = self.root / "include"
        for name in builder.SDK_HEADERS:
            path = self.include / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("// Header fixture only.\n")
        self.output = self.root / "output"

    def arguments(self, extra=()):
        return builder.parser().parse_args([
            "--sdk-dso", str(self.sdk), "--sdk-include", str(self.include),
            "--output", str(self.output), *extra,
        ])

    def test_help_does_not_build(self):
        result = subprocess.run([sys.executable, str(Path(builder.__file__)), "--help"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn("--sdk-dso", result.stdout)
        self.assertIn("--helper-base", result.stdout)
        self.assertFalse(self.output.exists())

    def test_missing_sdk_does_not_create_output(self):
        with patch.object(builder.subprocess, "run") as run:
            with self.assertRaises(FileNotFoundError):
                builder.build(self.arguments())
        run.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_non_elf_sdk_rejected_before_any_command(self):
        self.sdk.write_bytes(b"fixture is not a real SDK")
        with patch.object(builder.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "SDK DSO must be ELF"):
                builder.build(self.arguments())
        run.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_missing_public_header_rejected_before_any_command(self):
        self.sdk.write_bytes(b"\x7fELFfixture")
        (self.include / builder.SDK_HEADERS[1]).unlink()
        with patch.object(builder.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "public header is missing"):
                builder.build(self.arguments())
        run.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_unsupported_linker_path_rejected(self):
        self.sdk.write_bytes(b"\x7fELFfixture")
        library = self.root / "comma,library"
        library.mkdir()
        with patch.object(builder.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "unsupported linker path characters"):
                builder.build(self.arguments(["--library-dir", str(library)]))
        run.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_mismatched_original_helper_does_not_stage(self):
        base = self.root / "base"
        base.mkdir()
        for name in builder.HELPER_HASHES:
            (base / name).write_bytes(b"untrusted fixture")
        self.output.mkdir()
        with patch.object(builder.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "capture helper source mismatch"):
                builder.stage_sources(self.output, base)
        run.assert_not_called()
        self.assertEqual(list(self.output.iterdir()), [])

    def test_existing_output_is_never_overwritten(self):
        self.sdk.write_bytes(b"\x7fELFfixture")
        self.output.mkdir()
        sentinel = self.output / "keep"
        sentinel.write_bytes(b"existing user data")
        with patch.object(builder, "source_inputs", return_value=({}, b"patch", sys.executable)), \
                patch.object(builder.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "output already exists"):
                builder.build(self.arguments(["--compiler", sys.executable]))
        run.assert_not_called()
        self.assertEqual(sentinel.read_bytes(), b"existing user data")
        self.assertEqual(list(self.output.iterdir()), [sentinel])

    def test_cli_invalid_sdk_returns_two_without_output(self):
        self.sdk.write_bytes(b"not ELF")
        argv = ["build_local.py", "--sdk-dso", str(self.sdk), "--sdk-include", str(self.include),
                "--output", str(self.output)]
        with patch.object(sys, "argv", argv), contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(builder.main(), 2)
        self.assertIn("SDK DSO must be ELF", errors.getvalue())
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
