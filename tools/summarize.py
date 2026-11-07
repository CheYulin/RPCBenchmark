#!/usr/bin/env python3
"""Audit completed rounds and publish their metrics without merging per-round percentiles."""
import argparse
import csv
import json
import math
import pathlib
import statistics
from collections import Counter, defaultdict

LABELS = {
    "64B-c1": "64 B · 1 并发",
    "64B-c8": "64 B · 8 并发",
    "64B-c32": "64 B · 32 并发",
    "64B-c128": "64 B · 128 并发",
    "4KiB-c32": "4 KiB · 32 并发",
    "64KiB-c32": "64 KiB · 32 并发",
    "64B-c32-spin25": "64 B · 32 并发 · handler 忙等 25 µs",
    "64B-c32-8workers": "64 B · 32 并发 · 每端 8 worker",
}
COUNTERS = ("voluntary_ctxt_switches", "nonvoluntary_ctxt_switches")


def normalized_threads(telemetry, role, success):
    samples = telemetry["samples"]
    first, last = samples[0][role], samples[-1][role]
    shared = set(first) & set(last)
    if not shared:
        return None
    delta = sum(last[tid][name] - first[tid][name] for tid in shared for name in COUNTERS)
    return delta * 1000 / success


def steal_fraction(telemetry):
    start, end = (telemetry[key]["cpu_ticks"] for key in ("host_begin", "host_end"))
    deltas = [[b - a for a, b in zip(start[cpu], end[cpu])] for cpu in start if cpu in end]
    total = sum(sum(counters[:8]) for counters in deltas)
    return sum(counters[7] for counters in deltas) / total if total else None


def aggregate(rows):
    by_key = defaultdict(list)
    for row in rows:
        by_key[(row["case"], row["framework"])].append(row)
    data = []
    for (case, framework), items in by_key.items():
        summary = {"case": case, "framework": framework, "n": len(items),
                   "success": sum(row["success"] for row in items),
                   "failed": sum(row["failed"] for row in items)}
        for field in ("success_rps", "p50_us", "p95_us", "p99_us", "client_cpu_us_per_rpc",
                      "server_cpu_us_per_rpc", "server_ctxswitches_per_krpc",
                      "server_threads", "server_rss_mib", "client_rss_mib"):
            values = [row[field] for row in items if row[field] is not None]
            summary[field] = statistics.median(values) if values else None
            if values:
                summary[field + "_min"] = min(values)
                summary[field + "_max"] = max(values)
        data.append(summary)
    return data


def build_results(root, expected_rounds):
    data = (root / "results.jsonl").read_text().splitlines()
    rows = []
    for line in data:
        item = json.loads(line)
        case = item["case"]
        run = root / f'r{item["round"]}-{case}-{item["engine"]}'
        telemetry = json.loads((run / "telemetry.json").read_text())
        count = item["success"]
        assert item["attempts"] == count + item["errors"]
        assert item["errors"] == item["rpc_errors"] + item["mismatches"]
        assert item["active_lanes"] == item["connections_observed"] == item["concurrency"]
        assert count > 0 and item["errors"] == 0
        assert sum(bin_count for _, bin_count in item["histogram"]) == item["samples"] == count
        assert 0 < item["p50_us"] <= item["p95_us"] <= item["p99_us"] <= item["p999_us"] <= math.ceil(item["max_us"])
        assert abs(item["rps"] - count / item["elapsed_s"]) / item["rps"] < 0.001
        server = telemetry["samples"]
        rss = {role: max((s[f"{role}_rss_kib"] for s in server if s[f"{role}_rss_kib"] is not None),
                         default=0) / 1024 for role in ("server", "client")}
        rows.append({
            "case": case, "scenario": LABELS.get(case, case), "framework": item["framework"],
            "payload_bytes": item["payload"], "concurrency": item["concurrency"],
            "repeat": item["round"], "workers": item["workers"], "handler_us": item["handler_us"],
            "success": count, "failed": item["errors"], "samples": item["samples"],
            "connections": item["connections_observed"], "elapsed_s": item["elapsed_s"],
            "drained": item["drained"], "success_rps": item["rps"],
            "p50_us": item["p50_us"], "p95_us": item["p95_us"], "p99_us": item["p99_us"],
            "p999_us": item["p999_us"], "max_us": item["max_us"],
            "server_cpu_us_per_rpc": item["server_usage_usec"] / count,
            "client_cpu_us_per_rpc": item["client_usage_usec"] / count,
            "server_user_us_per_rpc": item["server_user_usec"] / count,
            "server_kernel_us_per_rpc": item["server_system_usec"] / count,
            "client_user_us_per_rpc": item["client_user_usec"] / count,
            "client_kernel_us_per_rpc": item["client_system_usec"] / count,
            "server_cpu_cores": item["server_usage_usec"] / (item["elapsed_s"] * 1e6),
            "client_cpu_cores": item["client_usage_usec"] / (item["elapsed_s"] * 1e6),
            "server_ctxswitches_per_krpc": normalized_threads(telemetry, "server", count),
            "server_threads": max(len(s["server"]) for s in server),
            "client_threads": max(len(s["client"]) for s in server),
            "server_rss_mib": rss["server"],
            "client_rss_mib": rss["client"],
            "host_steal_fraction": steal_fraction(telemetry),
            "start_utc": item["start_utc"],
        })
    expected = Counter((case, framework, repeat) for case in LABELS
                       for framework in ("brpc", "coro_rpc")
                       for repeat in range(1, expected_rounds + 1))
    actual = Counter((row["case"], row["framework"], row["repeat"]) for row in rows)
    assert actual == expected, (expected - actual, actual - expected)
    rows.sort(key=lambda row: (list(LABELS).index(row["case"]), row["repeat"], row["framework"]))
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runs", type=pathlib.Path)
    parser.add_argument("output", type=pathlib.Path)
    parser.add_argument("--rounds", type=int, default=3)
    args = parser.parse_args()
    rows = build_results(args.runs, args.rounds)
    args.output.mkdir(parents=True, exist_ok=True)
    data = {
        "host": "xqyun-32c32g",
        "run_date": "2026-11-07",
        "rounds": rows,
        "summary": aggregate(rows),
        "audit": {"rounds": len(rows), "success": sum(row["success"] for row in rows),
                  "failed": sum(row["failed"] for row in rows),
                  "all_connection_counts_verified": True},
    }
    (args.output / "results.json").write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    for name, records in (("results", rows), ("medians", data["summary"])):
        with (args.output / f"{name}.csv").open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(dict.fromkeys(key for row in records for key in row)))
            writer.writeheader()
            writer.writerows(records)
    print(json.dumps(data["audit"]))


if __name__ == "__main__":
    main()
