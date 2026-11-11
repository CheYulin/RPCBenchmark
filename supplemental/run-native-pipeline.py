#!/usr/bin/env python3
"""Native pipeline matrix at fixed total logical concurrency; private lab evidence only."""
import argparse
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))
import run_matrix as lab
from pipeline_smoke import parse_output, validate


def run_case(args, groups, payload, concurrency, depth, caller, repeat):
    count = concurrency // depth
    case = args.out / f"r{repeat}-p{payload}-c{concurrency}-d{depth}-{caller}"
    case.mkdir()
    port = lab.free_port()
    server_cmd = [str(args.binary), "server", "--port", str(port), "--workers", "4"]
    client_cmd = [str(args.binary), "client", "--port", str(port), "--workers", "4",
                  "--concurrency", str(concurrency), "--pipeline", str(depth), "--caller", caller,
                  "--payload", str(payload),
                  "--warmup", str(args.warmup), "--seconds", str(args.seconds), "--timeout-ms", str(args.timeout_ms)]
    control = {"payload": payload, "concurrency": concurrency, "pipeline": depth, "caller": caller, "connections": count,
               "workers": 4, "round": repeat, "server_cpus": args.server_cpus, "client_cpus": args.client_cpus,
               "server_cmd": ["./" + args.binary.name, *server_cmd[1:]],
               "client_cmd": ["./" + args.binary.name, *client_cmd[1:]]}
    lab.write_json(case / "config.json", control)
    server = client = None
    warmup = b""
    try:
        with (case / "server.stderr").open("wb") as log:
            server = groups.launch("server", set(args.server_cpus), server_cmd,
                                   stdout=subprocess.PIPE, stderr=log, bufsize=0)
            ready = lab.wait_ready(server, 8)
            (case / "server.stdout").write_text(ready + "\n")
            client = groups.launch("client", set(args.client_cpus), client_cmd,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
            warmup = lab.wait_for_warmup(client, args.warmup + 12)
            cpu_begin = groups.cpu()
            samples = [lab.sample(server, client)]
            observed = lab.socket_count(port)
            deadline = time.monotonic() + args.seconds + 12
            while client.poll() is None and time.monotonic() < deadline:
                try:
                    client.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    samples.append(lab.sample(server, client))
            cpu_end = groups.cpu()
            samples.append(lab.sample(server, client))
            stdout, stderr = client.communicate(timeout=2)
            (case / "client.stdout").write_bytes(stdout)
            (case / "client.stderr").write_bytes(warmup + stderr)
            lab.write_json(case / "telemetry.json", {"clock_hz": lab.CLOCK_HZ,
                "connections_observed": observed, "cpu_begin": cpu_begin, "cpu_end": cpu_end, "samples": samples})
            if client.returncode != 0:
                raise RuntimeError(f"{case.name} exited {client.returncode}")
            config, result = parse_output(stdout.decode())
            validate(config, result, depth, concurrency, payload)
            if config["caller"] != caller:
                raise RuntimeError(f"{case.name}: caller mode did not match the request")
            if observed != count:
                raise RuntimeError(f"{case.name}: expected {count} real TCP connections, observed {observed}")
            client_threads = max(len(snapshot["client"]) for snapshot in samples)
            if caller == "sync" and client_threads < concurrency + 4:
                raise RuntimeError(f"{case.name}: expected logical caller threads plus four IO threads")
            summary = {**result, **config, **control, "connections_observed": observed,
                       "observed_client_threads_peak": client_threads}
            for role in cpu_begin:
                for name in ("usage_usec", "user_usec", "system_usec"):
                    summary[f"{role}_{name}"] = cpu_end[role][name] - cpu_begin[role][name]
            lab.write_json(case / "result.json", summary)
            return summary
    finally:
        if client and not (case / "client.stderr").exists():
            lab.stop(client)
            stdout, stderr = client.communicate(timeout=2)
            (case / "client.stdout").write_bytes(stdout)
            (case / "client.stderr").write_bytes(warmup + stderr)
        lab.stop(server)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=pathlib.Path, required=True)
    parser.add_argument("--out", type=pathlib.Path, required=True)
    parser.add_argument("--server-cpus", type=lab.cpu_list, required=True)
    parser.add_argument("--client-cpus", type=lab.cpu_list, required=True)
    parser.add_argument("--monitor-cpu", type=int, required=True)
    parser.add_argument("--rounds", type=int, default=4)
    parser.add_argument("--seconds", type=int, default=3)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--timeout-ms", type=int, default=1000)
    parser.add_argument("--require-cgroup", action="store_true")
    parser.add_argument("--depth", type=int, choices=(1, 8, 16), action="append")
    parser.add_argument("--concurrency", type=int, choices=(32, 128, 256), action="append")
    parser.add_argument("--payload", type=int, choices=(4, 64, 4096), action="append")
    parser.add_argument("--caller", choices=("coroutine", "sync"), action="append")
    args = parser.parse_args()
    args.binary = args.binary.resolve()
    args.depth = args.depth or [1, 8, 16]
    args.concurrency = args.concurrency or [32, 128, 256]
    args.payload = args.payload or [4, 4096]
    args.caller = args.caller or ["coroutine"]
    chosen = args.server_cpus + args.client_cpus + [args.monitor_cpu]
    if len(args.server_cpus) != 4 or len(args.client_cpus) != 4:
        parser.error("exactly four distinct physical CPUs per role are required")
    if len(chosen) != len(set(chosen)) or not set(chosen) <= os.sched_getaffinity(0):
        parser.error("CPU sets must be allowed and disjoint")
    if args.rounds < 1 or not 1 <= args.seconds <= 600 or not 1 <= args.warmup <= 600:
        parser.error("invalid repetition or phase duration")
    if not shutil.which("ss"):
        parser.error("ss is required to validate physical connection counts")
    args.out.mkdir(parents=True)
    lab.write_json(args.out / "experiment.json", {"logical_concurrencies": args.concurrency, "depths": args.depth,
        "payloads": args.payload, "callers": args.caller, "workers": 4, "rounds": args.rounds, "warmup_s": args.warmup,
        "measurement_s": args.seconds, "binary_sha256": hashlib.sha256(args.binary.read_bytes()).hexdigest(),
        "cpu_window": "after WARMUP_DONE through completion, including phase lead and drain"})
    os.sched_setaffinity(0, {args.monitor_cpu})
    groups = lab.Groups(args.require_cgroup)
    with (args.out / "results.jsonl").open("w") as stream:
        for repeat in range(1, args.rounds + 1):
            for payload in args.payload:
                for index, concurrency in enumerate(args.concurrency):
                    depths = args.depth if (repeat + index) % 2 else list(reversed(args.depth))
                    for depth in depths:
                        callers = args.caller if repeat % 2 else list(reversed(args.caller))
                        for caller in callers:
                            if caller == "sync" and depth != 1:
                                continue
                            result = run_case(args, groups, payload, concurrency, depth, caller, repeat)
                            stream.write(json.dumps(result) + "\n")
                            stream.flush()
                            print(f"r{repeat} p{payload} logical{concurrency} pipeline{depth} {caller} OK", flush=True)


if __name__ == "__main__":
    main()
