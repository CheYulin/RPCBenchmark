#!/usr/bin/env python3
"""Compile the preserved completion-driven RED and offered-load GREEN contracts."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    source = Path(__file__).resolve().parent
    compiler = shlex.split(os.environ.get("CXX", "g++"))
    flags = ["-std=c++17", "-O2", "-g", "-Wall", "-Wextra", "-Werror"]
    jobs = [
        ("old-contract-red", "planner_contract_test.cpp", ["-DPACER_OLD_COMPLETION_DRIVEN"], 1),
        ("offered-contract-green", "planner_contract_test.cpp", [], 0),
        ("fake-clock-green", "pacer_test.cpp", [], 0),
    ]
    results = []
    version = subprocess.run(compiler + ["--version"], capture_output=True, text=True, check=True)
    (args.out / "compiler.txt").write_text(version.stdout)
    for name, entry, extra, expected in jobs:
        binary = args.out.resolve() / name
        command = compiler + flags + extra + [str(source / entry), "-o", str(binary)]
        build = subprocess.run(command, capture_output=True, text=True, timeout=60)
        (args.out / (name + ".build.stdout")).write_text(build.stdout)
        (args.out / (name + ".build.stderr")).write_text(build.stderr)
        if build.returncode:
            raise RuntimeError(f"{name}: compilation failed; this is not semantic RED")
        run = subprocess.run([str(binary)], capture_output=True, text=True, timeout=5)
        (args.out / (name + ".stdout")).write_text(run.stdout)
        (args.out / (name + ".stderr")).write_text(run.stderr)
        passed = run.returncode == expected
        if expected == 1:
            passed = passed and "FAIL completion independence/absolute arrivals fast=10 slow=3 expected=3" in run.stderr
        results.append({"name": name, "compile": command, "expected_exit": expected,
                        "actual_exit": run.returncode, "expected_semantics": passed})
        print(f"{name}: exit={run.returncode}, expected={expected}, pass={passed}", flush=True)
        if not passed:
            break
    inputs = ["pacer.h", "old_completion_planner.h", "planner_contract_test.cpp", "pacer_test.cpp",
              "run_fake_clock_tests.py"]
    receipt = {"schema": 1, "source_sha256": {
        name: hashlib.sha256((source / name).read_bytes()).hexdigest() for name in inputs},
        "results": results, "passed": len(results) == 3 and all(row["expected_semantics"] for row in results),
        "sdk_driver_compiled": False, "network_or_performance_run": False}
    (args.out / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return 0 if receipt["passed"] else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(error, file=sys.stderr)
        sys.exit(2)
