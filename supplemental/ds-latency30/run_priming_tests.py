#!/usr/bin/env python3
"""Standalone readiness contract: registration-only negative, then completed-priming policy."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    source = Path(__file__).resolve().parent
    compiler = shlex.split(os.environ.get("CXX", "g++"))
    records = []
    for name, flags, expected in [("registered-ready-negative", ["-DPRIMING_LEGACY_READY"], 1),
                                  ("priming-ready-green", [], 0)]:
        binary = args.out.resolve() / name
        command = compiler + ["-std=c++17", "-O2", "-Wall", "-Wextra", "-Werror", *flags,
                              str(source / "priming_gate_test.cpp"), "-o", str(binary)]
        build = subprocess.run(command, text=True, capture_output=True, timeout=60)
        (args.out / (name + ".build.stderr")).write_text(build.stderr)
        if build.returncode:
            raise RuntimeError("compile failure is not semantic RED: " + name)
        result = subprocess.run([str(binary)], text=True, capture_output=True, timeout=5)
        (args.out / (name + ".stdout")).write_text(result.stdout)
        (args.out / (name + ".stderr")).write_text(result.stderr)
        valid = result.returncode == expected
        if expected:
            valid &= "FAIL unfinished priming blocks epoch" in result.stderr
            valid &= "FAIL failed priming blocks epoch" in result.stderr
            valid &= result.stderr.count("FAIL ") == 2 and result.stdout.count("PASS ") == 3
        else:
            valid &= not result.stderr and result.stdout.count("PASS ") == 5
        records.append({"name": name, "command": command, "expected_exit": expected,
                        "actual_exit": result.returncode, "expected_semantics": bool(valid)})
        if not valid:
            break
    inputs = ["priming_gate.h", "legacy_priming_ready.h", "priming_gate_test.cpp", "run_priming_tests.py"]
    receipt = {"compiler": subprocess.check_output(compiler + ["--version"], text=True), "results": records,
               "negative_model": "fc8 caller registration counted as ready; new priming contract not satisfied",
               "source_sha256": {n: hashlib.sha256((source / n).read_bytes()).hexdigest() for n in inputs},
               "passed": len(records) == 2 and all(r["expected_semantics"] for r in records)}
    (args.out / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    raise SystemExit(0 if receipt["passed"] else 1)


if __name__ == "__main__":
    main()
