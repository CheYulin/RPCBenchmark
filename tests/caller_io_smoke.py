#!/usr/bin/env python3
"""Private opt-in caller-owned IO functional and teardown checks, not a performance run."""
import contextlib
import json
import pathlib
import selectors
import socket
import struct
import subprocess
import sys
import threading

from pipeline_smoke import counters, parse_output, server, validate


def client(binary, port, *extra):
    return subprocess.run([binary, "client", "--port", str(port), "--workers", "4", "--concurrency", "1",
                           "--payload", "4096", "--warmup", "1", "--seconds", "1", *extra],
                          capture_output=True, text=True, timeout=10)


@contextlib.contextmanager
def first_reset_proxy(upstream):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(0.1)
    stop = threading.Event()
    workers = []
    errors = []

    def relay(peer):
        try:
            with peer, socket.create_connection(("127.0.0.1", int(upstream)), timeout=2) as remote:
                with selectors.DefaultSelector() as poll:
                    poll.register(peer, selectors.EVENT_READ, remote)
                    poll.register(remote, selectors.EVENT_READ, peer)
                    while not stop.is_set():
                        for event, _ in poll.select(0.1):
                            data = event.fileobj.recv(65536)
                            if not data:
                                return
                            event.data.sendall(data)
        except OSError as error:
            if not stop.is_set():
                errors.append(str(error))

    def accept():
        first = True
        while not stop.is_set():
            try:
                peer, _ = listener.accept()
            except socket.timeout:
                continue
            if first:
                first = False
                with peer:
                    peer.settimeout(2)
                    peer.recv(1)
                    peer.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            else:
                task = threading.Thread(target=relay, args=(peer,))
                workers.append(task)
                task.start()

    task = threading.Thread(target=accept)
    task.start()
    try:
        yield listener.getsockname()[1]
    finally:
        stop.set()
        task.join(timeout=3)
        listener.close()
        for worker in workers:
            worker.join(timeout=3)
        assert not task.is_alive() and all(not worker.is_alive() for worker in workers)
        assert not errors, errors


def main(owned, baseline):
    checks = []
    with server(baseline) as port:
        for concurrency in (1, 32):
            result = client(owned, port, "--concurrency", str(concurrency))
            assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
            config, metrics = parse_output(result.stdout)
            validate(config, metrics, 1, concurrency, 4096)
            assert config["caller"] == "owned" and config["io_workers"] == 0
            assert metrics["workers"] == 0 and config["requested_workers"] == 4
            assert config["cleanup"] == "natural_idle_twice"
            assert len(set(config["owner_tids"])) == config["owner_threads"] == concurrency
            assert config["process_threads_ready"] >= concurrency + 1
            checks.append(f"bytes/route/threads/c{concurrency}")
        result = client(owned, port, "--verify-rounds", "64")
        assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
        proof = json.loads(result.stdout.splitlines()[-1])
        assert proof["verified"] and proof["rounds"] == 64 and proof["cleanup"] == "natural_idle_twice"
        checks.append("reconnect/restart/immediate-dtor/repeated-fd-thread")
        with first_reset_proxy(port) as reset_port:
            result = client(owned, reset_port, "--verify-rounds", "2", "--verify-first-error", "1")
            assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
            assert json.loads(result.stdout.splitlines()[-1])["verified"]
            checks.append("disconnect/error-drain/reconnect")
    with server(baseline, ["--corrupt-response", "1"]) as port:
        result = client(owned, port)
        count = counters(result)
        assert result.returncode == 1 and count["mismatches"] > 0 and count["rpc_errors"] == 0
        checks.append("mismatch counted")
    with server(baseline, ["--handler-us", "10000"]) as port:
        result = client(owned, port, "--timeout-ms", "1")
        count = counters(result)
        assert result.returncode == 1 and count["rpc_errors"] > 0 and count["mismatches"] == 0
        checks.append("native timeout/error-drain/immediate-dtor")
    with socket.socket() as unavailable:
        unavailable.bind(("127.0.0.1", 0))
        result = client(owned, unavailable.getsockname()[1])
        assert result.returncode == 1 and "native caller IO connection failed" in result.stderr
        checks.append("connect-refused/dtor")
    print(json.dumps({"verified": True, "checks": checks, "performance_claim": False}))


if __name__ == "__main__":
    main(*(str(pathlib.Path(value).resolve()) for value in sys.argv[1:3]))
