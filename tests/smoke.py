#!/usr/bin/env python3
import json
import selectors
import socket
import subprocess
import sys


def main(binary, server_flags):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = str(probe.getsockname()[1])

    server = subprocess.Popen(
        [binary, "server", "--port", port, "--workers", "4", "--handler-us", "10", *server_flags],
        stdout=subprocess.PIPE, text=True
    )
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(server.stdout, selectors.EVENT_READ)
            if not selector.select(timeout=5) or server.stdout.readline().strip() != "READY":
                raise RuntimeError("server failed to become ready")
        client = subprocess.run(
            [binary, "client", "--port", port, "--workers", "4",
             "--concurrency", "4", "--payload", "256", "--warmup", "1", "--seconds", "1"],
            capture_output=True, text=True, timeout=10,
        )
        if client.returncode:
            raise RuntimeError(f"client failed: {client.stdout}\n{client.stderr}")
        assert "WARMUP_DONE" in client.stderr
        stats = json.loads(client.stdout)
        assert stats["payload"] == 256 and stats["active_lanes"] == 4
        assert stats["errors"] == 0 and stats["attempts"] == stats["success"] > 0
        assert stats["samples"] == sum(count for _, count in stats["histogram"])
        assert 0 < stats["p50_us"] <= stats["p95_us"] <= stats["p99_us"]
        print(f'{stats["framework"]}: {stats["success"]} valid replies')
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()
    assert server.returncode == 0


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2:])
