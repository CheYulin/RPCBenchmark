"""Run against the actual Linux ELF: invalid inputs never construct clients or servers."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

BINARY = Path(os.environ["DS_CORE_PROBE_BINARY"]).resolve(strict=True)


class CliConfiguration(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="core-closed-config-")
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name)/"new-output"

    def rejected(self, arguments, message, *, io="1", server_pid="1"):
        environment = dict(os.environ)
        for key, value in (("DATASYSTEM_CORO_CALLER_IO", io), ("DS_CORE_SERVER_PID", server_pid)):
            if value is None:
                environment.pop(key, None)
            else:
                environment[key] = value
        result = subprocess.run([str(BINARY), *arguments], capture_output=True, text=True,
                                env=environment, timeout=5)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn(message, result.stderr)
        self.assertNotIn("READY CORE_OWNER_", result.stdout)
        self.assertNotIn("RESULT ", result.stdout)
        return result

    def call(self, port="12345", mode="shared", concurrency="32"):
        return ["call-closed", "127.0.0.1", port, mode, concurrency, str(self.output)]

    def test_unknown_command_stops_before_any_output(self):
        self.rejected(["unknown"], "usage:")
        self.assertFalse(self.output.exists())

    def test_wrong_argument_counts(self):
        for arguments in ([], ["serve"], ["serve", "127.0.0.1"], self.call()+["extra"]):
            with self.subTest(arguments=arguments):
                self.rejected(arguments, "usage:")
        self.assertFalse(self.output.exists())

    def test_port_is_a_whole_valid_uint16(self):
        for port in ("0", "65536", "1junk", "-1"):
            with self.subTest(port=port):
                self.rejected(self.call(port=port), "invalid integer")
        self.assertFalse(self.output.exists())

    def test_only_shared_client_topology(self):
        for mode in ("other", "per-owner"):
            with self.subTest(mode=mode):
                self.rejected(self.call(mode=mode), "closed-loop screen requires shared client")
        self.assertFalse(self.output.exists())

    def test_only_exact_concurrency32(self):
        for concurrency in ("0", "31", "33", "500", "32junk"):
            with self.subTest(concurrency=concurrency):
                self.rejected(self.call(concurrency=concurrency), "invalid integer")
        self.assertFalse(self.output.exists())

    def test_old_rate_cli_is_not_an_alias(self):
        self.rejected(["call", "127.0.0.1", "12345", "shared", "500", str(self.output)], "usage:")
        self.assertFalse(self.output.exists())

    def test_no_implicit_caller_io_fallback(self):
        self.rejected(self.call(), "DATASYSTEM_CORO_CALLER_IO=1 required", io=None)
        self.assertFalse(self.output.exists())

    def test_server_pid_is_required_before_outputs(self):
        self.rejected(self.call(), "DS_CORE_SERVER_PID required", server_pid=None)
        self.assertFalse(self.output.exists())

    def test_existing_output_is_preserved(self):
        self.output.mkdir()
        sentinel = self.output/"sentinel"
        sentinel.write_bytes(b"existing evidence")
        self.rejected(self.call(), "output must be new")
        self.assertEqual(list(self.output.iterdir()), [sentinel])
        self.assertEqual(sentinel.read_bytes(), b"existing evidence")


if __name__ == "__main__":
    unittest.main(verbosity=2)
