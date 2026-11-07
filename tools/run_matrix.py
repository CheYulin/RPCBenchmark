#!/usr/bin/env python3
"""Measure native RPC echo with matched role affinities and process/cgroup evidence."""
import argparse
import json
import os
import pathlib
import re
import selectors
import shutil
import signal
import socket
import subprocess
import time

CLOCK_HZ = os.sysconf("SC_CLK_TCK")
CASES = {
    "64B-c1": (64, 1, 4, 0),
    "64B-c8": (64, 8, 4, 0),
    "64B-c32": (64, 32, 4, 0),
    "64B-c128": (64, 128, 4, 0),
    "4KiB-c32": (4096, 32, 4, 0),
    "64KiB-c32": (65536, 32, 4, 0),
    "64B-c32-spin25": (64, 32, 4, 25),
    "64B-c32-8workers": (64, 32, 8, 0),
}
ENGINES = ("brpc", "coro")
CGROOT = pathlib.Path("/sys/fs/cgroup")


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def integer_fields(path):
    return {key: int(value) for key, value in
            (line.split() for line in path.read_text().splitlines())}


def proc_threads(pid):
    records = {}
    for path in pathlib.Path(f"/proc/{pid}/task").glob("*"):
        try:
            stat = (path / "stat").read_text()
            name = stat[stat.index("(") + 1:stat.rindex(")")]
            fields = stat[stat.rindex(")") + 2:].split()
            status = (path / "status").read_text()
            context = re.findall(r"(?m)^((?:non)?voluntary_ctxt_switches):\s+(\d+)", status)
            sched = (path / "schedstat").read_text().split()
            records[path.name] = {
                "name": name,
                "state": fields[0],
                "user_ticks": int(fields[11]),
                "sys_ticks": int(fields[12]),
                "cpu": int(fields[36]),
                "sched_runtime_ns": int(sched[0]),
                "sched_wait_ns": int(sched[1]),
                "sched_slices": int(sched[2]),
                **{key: int(value) for key, value in context},
            }
        except (FileNotFoundError, ProcessLookupError):
            continue
    return records


def process_rss(pid):
    try:
        status = pathlib.Path(f"/proc/{pid}/status").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return None
    match = re.search(r"(?m)^VmRSS:\s+(\d+) kB$", status)
    return int(match.group(1)) if match else None


def host_counters(cpus):
    cpu = {}
    for line in pathlib.Path("/proc/stat").read_text().splitlines():
        fields = line.split()
        if fields[0].startswith("cpu") and fields[0][3:].isdigit() and int(fields[0][3:]) in cpus:
            cpu[fields[0]] = list(map(int, fields[1:]))
    return {
        "cpu_ticks": cpu,
        "pressure": {kind: pathlib.Path(f"/proc/pressure/{kind}").read_text()
                     for kind in ("cpu", "io", "memory")
                     if pathlib.Path(f"/proc/pressure/{kind}").exists()},
    }


class Groups:
    def __init__(self, required):
        entry = pathlib.Path("/proc/self/cgroup").read_text().strip().split("::")[-1]
        self.root = CGROOT / entry.lstrip("/")
        self.roles = {}
        if (self.root / "cgroup.procs").exists() and (self.root / "cgroup.controllers").exists():
            for role in ("server", "client"):
                path = self.root / f"rpc-{role}"
                try:
                    path.mkdir()
                except OSError:
                    break
                self.roles[role] = path
        if required and len(self.roles) != 2:
            raise RuntimeError("Run under a delegated systemd unit for cgroup telemetry")
        if len(self.roles) != 2:
            self.roles = {}

    def launch(self, role, cpus, cmd, **kwargs):
        def place():
            os.sched_setaffinity(0, cpus)
            if role in self.roles:
                (self.roles[role] / "cgroup.procs").write_text(str(os.getpid()))

        return subprocess.Popen(cmd, preexec_fn=place, **kwargs)

    def cpu(self):
        return {role: integer_fields(path / "cpu.stat") for role, path in self.roles.items()}


def socket_count(port):
    cmd = ["ss", "-Htn", "state", "established", f"( sport = :{port} )"]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return len(result.stdout.splitlines())


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_ready(process, seconds=15):
    with selectors.DefaultSelector() as wait:
        wait.register(process.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and wait.select(0.25):
            line = process.stdout.readline()
            if b"READY" in line:
                return line.decode(errors="replace").strip()
            if not line:
                break
        while time.monotonic() < deadline and process.poll() is None:
            if wait.select(0.25):
                line = process.stdout.readline()
                if b"READY" in line:
                    return line.decode(errors="replace").strip()
                if not line:
                    break
    raise RuntimeError("server READY timed out")


def stop(process):
    if process and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=6)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=6)


def wait_for_warmup(process, timeout):
    with selectors.DefaultSelector() as wait:
        wait.register(process.stderr, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout
        diagnostics = []
        while time.monotonic() < deadline:
            if not wait.select(0.25):
                continue
            line = process.stderr.readline()
            if not line:
                break
            diagnostics.append(line)
            if b"WARMUP_DONE" in line:
                return b"".join(diagnostics)
    raise RuntimeError(f"client WARMUP_DONE timed out; captured {diagnostics!r}")


def sample(server, client):
    return {
        "monotonic_ns": time.monotonic_ns(),
        "server": proc_threads(server.pid),
        "client": proc_threads(client.pid),
        "server_rss_kib": process_rss(server.pid),
        "client_rss_kib": process_rss(client.pid),
    }


def run_case(args, groups, case, engine, round_number):
    payload, concurrency, workers, handler_us = CASES[case]
    server_cpus = set(args.server_cpus[:workers])
    client_cpus = set(args.client_cpus[:workers])
    target_cpus = server_cpus | client_cpus
    run = args.out / f"r{round_number}-{case}-{engine}"
    run.mkdir()
    port = free_port()
    binary = str((args.build / f"rpc_{engine}").resolve())
    server_cmd = [binary, "server", "--port", str(port), "--workers", str(workers)]
    if handler_us:
        server_cmd += ["--handler-us", str(handler_us)]
    client_cmd = [binary, "client", "--port", str(port), "--workers", str(workers),
                  "--concurrency", str(concurrency), "--payload", str(payload), "--warmup", str(args.warmup),
                  "--seconds", str(args.seconds), "--timeout-ms", str(args.timeout_ms)]
    control = {
        "engine": engine, "case": case, "round": round_number, "payload": payload,
        "handler_us": handler_us, "concurrency": concurrency, "workers": workers,
        "server_cpus": sorted(server_cpus), "client_cpus": sorted(client_cpus), "port": port,
        "server_cmd": ["./" + pathlib.Path(binary).name, *server_cmd[1:]],
        "client_cmd": ["./" + pathlib.Path(binary).name, *client_cmd[1:]],
        "start_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    write_json(run / "config.json", control)
    server = client = None
    ready = None
    warmup = b""
    try:
        with (run / "server.stderr").open("wb") as server_log:
            server = groups.launch("server", server_cpus, server_cmd,
                                   stdout=subprocess.PIPE, stderr=server_log, bufsize=0)
            ready = wait_ready(server)
            (run / "server.stdout").write_text(ready + "\n")
            client = groups.launch("client", client_cpus, client_cmd,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
            warmup = wait_for_warmup(client, args.warmup + 20)
            cpu_begin = groups.cpu()
            host_begin = host_counters(target_cpus)
            telemetry = [sample(server, client)]
            if not args.no_ss:
                connections = socket_count(port)
            else:
                connections = None
            deadline = time.monotonic() + args.seconds + 15
            while client.poll() is None and time.monotonic() < deadline:
                try:
                    client.wait(timeout=0.9)
                except subprocess.TimeoutExpired:
                    telemetry.append(sample(server, client))
            cpu_end = groups.cpu()
            host_end = host_counters(target_cpus)
            telemetry.append(sample(server, client))
            stdout, stderr = client.communicate(timeout=4)
            (run / "client.stderr").write_bytes(warmup + stderr)
            (run / "client.stdout").write_bytes(stdout)
            write_json(run / "telemetry.json", {
                "clock_hz": CLOCK_HZ,
                "connections_observed": connections,
                "cpu_begin": cpu_begin, "cpu_end": cpu_end,
                "host_begin": host_begin, "host_end": host_end,
                "samples": telemetry,
            })
            if client.returncode:
                raise RuntimeError(f"client exited {client.returncode}; inspect {run.name}")
            if connections is not None and connections != concurrency:
                raise RuntimeError(f"{run.name} expected {concurrency} TCP connections, got {connections}")
            result = json.loads(stdout.decode().strip().splitlines()[-1])
            summary = {**result, **control, "connections_observed": connections}
            for role in cpu_begin:
                start, end = cpu_begin[role], cpu_end[role]
                for counter in ("usage_usec", "user_usec", "system_usec"):
                    summary[f"{role}_{counter}"] = end[counter] - start[counter]
            write_json(run / "result.json", summary)
            return summary
    finally:
        if client and not (run / "client.stderr").exists():
            stop(client)
            stdout, stderr = client.communicate(timeout=4)
            (run / "client.stderr").write_bytes(warmup + stderr)
            (run / "client.stdout").write_bytes(stdout)
        stop(server)


def cpu_list(value):
    cpus = []
    for part in value.split(","):
        endpoints = part.split("-")
        cpus.extend(range(int(endpoints[0]), int(endpoints[-1]) + 1))
    if not cpus or len(set(cpus)) != len(cpus):
        raise argparse.ArgumentTypeError("provide unique CPUs")
    return cpus


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", type=pathlib.Path, required=True)
    parser.add_argument("--out", type=pathlib.Path, required=True)
    parser.add_argument("--server-cpus", type=cpu_list, required=True)
    parser.add_argument("--client-cpus", type=cpu_list, required=True)
    parser.add_argument("--monitor-cpu", type=int, required=True)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--seconds", type=int, default=5)
    parser.add_argument("--warmup", type=int, default=2)
    parser.add_argument("--timeout-ms", type=int, default=1000)
    parser.add_argument("--require-cgroup", action="store_true")
    parser.add_argument("--no-ss", action="store_true")
    parser.add_argument("--engine", choices=ENGINES, action="append")
    parser.add_argument("--case", choices=CASES, action="append")
    args = parser.parse_args()
    cases = args.case or list(CASES)
    engines = args.engine or list(ENGINES)
    workers = max(CASES[case][2] for case in cases)
    selected = args.server_cpus + args.client_cpus + [args.monitor_cpu]
    if not set(selected) <= os.sched_getaffinity(0) or len(selected) != len(set(selected)):
        parser.error("CPU groups must be allowed, distinct and disjoint")
    if min(len(args.server_cpus), len(args.client_cpus)) < workers:
        parser.error(f"selected scenarios need at least {workers} CPUs per role")
    if not args.no_ss and not shutil.which("ss"):
        parser.error("ss required for connection validation, or specify --no-ss")
    args.out.mkdir(parents=True)
    os.sched_setaffinity(0, {args.monitor_cpu})
    groups = Groups(args.require_cgroup)
    with (args.out / "results.jsonl").open("w") as stream:
        for round_number in range(1, args.rounds + 1):
            for index, case in enumerate(cases):
                ordered = engines if (index + round_number) % 2 else list(reversed(engines))
                for engine in ordered:
                    summary = run_case(args, groups, case, engine, round_number)
                    record = json.dumps(summary, ensure_ascii=False)
                    stream.write(record + "\n")
                    stream.flush()
                    print(f"{case} {engine} round {round_number} OK", flush=True)


if __name__ == "__main__":
    main()
