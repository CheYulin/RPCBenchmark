#!/usr/bin/env python3
"""Repeat 128-client normal, request-timeout and connect-error teardown."""
import json
import pathlib
import socket
import sys

from pipeline_smoke import client, counters, parse_output, server, validate


def main(binary):
    with server(binary) as port:
        for _ in range(3):
            normal = client(binary, port, "--concurrency", "128", "--pipeline", "1")
            assert normal.returncode == 0, (normal.returncode, normal.stdout, normal.stderr)
            validate(*parse_output(normal.stdout), 1, 128, 4096)
    with server(binary, ["--handler-us", "10000"]) as port:
        for _ in range(3):
            timed_out = client(binary, port, "--concurrency", "128", "--pipeline", "1", "--timeout-ms", "1")
            assert timed_out.returncode == 1, (timed_out.returncode, timed_out.stdout, timed_out.stderr)
            counted = counters(timed_out)
            assert counted["rpc_errors"] > 0 and counted["mismatches"] == 0
    with socket.socket() as unavailable:
        unavailable.bind(("127.0.0.1", 0))
        port = str(unavailable.getsockname()[1])
        for _ in range(3):
            failed = client(binary, port, "--concurrency", "128", "--pipeline", "1")
            assert failed.returncode == 1, (failed.returncode, failed.stdout, failed.stderr)
            assert "native pipeline connection failed" in failed.stderr
    print(json.dumps({"verified": True, "connections": 128, "normal": 3, "timeout": 3, "connect_error": 3}))


if __name__ == "__main__":
    main(str(pathlib.Path(sys.argv[1]).resolve()))
