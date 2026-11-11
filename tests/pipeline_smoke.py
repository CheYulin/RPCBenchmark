#!/usr/bin/env python3
"""Bounded native pipeline, full-byte mismatch, RPC-error and histogram checks."""
import contextlib
import json
import pathlib
import selectors
import socket
import subprocess
import sys


def parse_output(stdout):
    records = [json.loads(line) for line in stdout.splitlines() if line.startswith("{")]
    config = next(row for row in records if row.get("record") == "pipeline")
    return config, records[-1]


def validate(config, result, depth, concurrency, payload):
    assert config["pipeline"] == depth
    assert config["connections"] == concurrency // depth
    assert len(config["peak_native"]) == concurrency // depth
    assert all(0 < peak <= depth for peak in config["peak_native"])
    if depth > 1:
        assert max(config["peak_native"]) > 1
    assert result["payload"] == payload and result["concurrency"] == concurrency
    assert result["active_lanes"] == concurrency
    assert result["attempts"] == result["success"] + result["rpc_errors"] + result["mismatches"]
    assert result["errors"] == result["rpc_errors"] + result["mismatches"] == 0
    assert result["samples"] == result["success"] == sum(n for _, n in result["histogram"])
    assert result["success"] > 0 and result["elapsed_s"] >= 1
    assert 0 < result["p50_us"] <= result["p95_us"] <= result["p99_us"] <= result["p999_us"]


@contextlib.contextmanager
def server(binary, flags=()):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = str(probe.getsockname()[1])
    process = subprocess.Popen([binary, "server", "--port", port, "--workers", "4", *flags],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            assert selector.select(4) and process.stdout.readline().strip() == "READY"
        yield port
    finally:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    assert process.returncode == 0


def client(binary, port, *extra):
    return subprocess.run([binary, "client", "--port", port, "--workers", "4", "--concurrency", "32",
                           "--payload", "4096", "--pipeline", "8", "--warmup", "1", "--seconds", "1", *extra],
                          capture_output=True, text=True, timeout=6)


def counters(process):
    line = next(line for line in process.stderr.splitlines() if line.startswith("WARMUP_COUNTERS "))
    return json.loads(line.removeprefix("WARMUP_COUNTERS "))


def main(binary):
    verified = subprocess.run([binary, "verify"], capture_output=True, text=True, timeout=6, check=True)
    proof = json.loads(verified.stdout.splitlines()[-1])
    assert proof == {"verified": True, "requests": 100, "waves": 12}
    with server(binary) as port:
        normal = client(binary, port)
        assert normal.returncode == 0, (normal.stdout, normal.stderr)
        validate(*parse_output(normal.stdout), 8, 32, 4096)
        synchronous = client(binary, port, "--pipeline", "1", "--caller", "sync")
        assert synchronous.returncode == 0, (synchronous.stdout, synchronous.stderr)
        config, result = parse_output(synchronous.stdout)
        validate(config, result, 1, 32, 4096)
        assert config["caller"] == "sync"
    with server(binary, ["--corrupt-response", "1"]) as port:
        corrupted = client(binary, port)
        counted = counters(corrupted)
        assert corrupted.returncode != 0 and counted["mismatches"] > 0 and counted["rpc_errors"] == 0
    with server(binary, ["--handler-us", "10000"]) as port:
        timed_out = client(binary, port, "--timeout-ms", "1")
        assert timed_out.returncode != 0 and counters(timed_out)["rpc_errors"] > 0
    for depth in ("0", "3", "17"):
        invalid = subprocess.run([binary, "client", "--pipeline", depth], capture_output=True, timeout=2)
        assert invalid.returncode != 0
    print("native pipeline barrier/reverse responses, full bytes, errors and histograms: PASS")


if __name__ == "__main__":
    main(str(pathlib.Path(sys.argv[1]).resolve()))
