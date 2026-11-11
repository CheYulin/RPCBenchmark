#!/usr/bin/env python3
"""Build the Linux public-API driver against an explicitly selected SDK."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sdk-include", required=True, type=Path)
    parser.add_argument("--sdk-lib", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    if not (args.sdk_include / "datasystem/kv_client.h").is_file() or not args.sdk_lib.is_file():
        parser.error("SDK headers and library must exist")
    args.out.mkdir(parents=True, exist_ok=False)
    source = Path(__file__).resolve().parent
    library = args.sdk_lib.resolve()
    binary = args.out.resolve() / "paced_sdk"
    compiler = shlex.split(os.environ.get("CXX", "g++"))
    version = subprocess.check_output(compiler + ["--version"], text=True)
    command = compiler + [
        "-std=c++17", "-O2", "-pthread", "-Wall", "-Wextra", "-Werror",
        "-I", str(args.sdk_include.resolve()), str(source / "paced_sdk.cpp"),
        str(library), "-Wl,-rpath-link," + str(library.parent),
        "-Wl,-rpath," + str(library.parent), "-o", str(binary)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    (args.out / "compile.stdout").write_text(result.stdout)
    (args.out / "compile.stderr").write_text(result.stderr)
    receipt = {"schema": 1, "command": command, "compiler_version": version, "compile_exit": result.returncode,
               "source_sha256": {name: hashlib.sha256((source / name).read_bytes()).hexdigest()
                                 for name in ["pacer.h", "priming_gate.h", "paced_sdk.cpp", "build_sdk_driver.py"]},
               "sdk_sha256": hashlib.sha256(library.read_bytes()).hexdigest(),
               "functional_or_performance_run": False}
    if result.returncode == 0:
        receipt["binary_sha256"] = hashlib.sha256(binary.read_bytes()).hexdigest()
    (args.out / "build.json").write_text(json.dumps(receipt, indent=2) + "\n")
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
