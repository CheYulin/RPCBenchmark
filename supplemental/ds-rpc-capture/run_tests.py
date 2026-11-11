#!/usr/bin/env python3
"""Standalone pure-core semantic checks. Run only inside an externally authorized exclusive BUILD slot."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", type=Path, required=True)
    a = p.parse_args()
    a.out.mkdir(parents=True, exist_ok=False)
    out = a.out.resolve()
    source = Path(__file__).resolve().parent
    inputs = [source / name for name in ["capture_abi.h", "capture_observe.h", "capture.cpp",
        "fixtures/fake_sdk.cpp", "fixtures/core_bridge.cpp", "fixtures/wrong_abi.cpp", "fixtures/exports.map",
        "tests/driver.cpp", "tests/header_c.c", "run_tests.py", "README.md"]]
    hashes = {str(x.relative_to(source)): sha(x) for x in inputs}
    cxx = shlex.split(os.environ.get("CXX", "/usr/bin/g++"))
    cc = shlex.split(os.environ.get("CC", "/usr/bin/gcc"))
    records = []

    def run(name, command, expected=0, stderr_contains=None, stdout_contains=None):
        record = {"name": name, "command": [str(x) for x in command], "expected_exit": expected}
        records.append(record)
        (out / "commands.json").write_text(json.dumps(records, indent=2) + "\n")
        process = subprocess.Popen(record["command"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, start_new_session=True)
        timed_out = False
        try:
            stdout, stderr = process.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            timed_out = True
            try: os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError: pass
            stdout, stderr = process.communicate()
        result = subprocess.CompletedProcess(record["command"], process.returncode, stdout, stderr)
        (out / (name + ".stdout")).write_text(stdout)
        (out / (name + ".stderr")).write_text(stderr)
        record["actual_exit"] = result.returncode
        record["timed_out"] = timed_out
        record["valid"] = not timed_out and result.returncode == expected
        if stderr_contains is not None: record["valid"] &= stderr_contains in stderr
        if stdout_contains is not None: record["valid"] &= stdout_contains in stdout
        if not record["valid"]: raise RuntimeError(name + " failed; compiler/loader failure is never semantic RED")
        return result

    passed, reason = False, None
    try:
        for label, compiler in [("cxx", cxx), ("cc", cc)]:
            version = run(label + "-version", compiler + ["--version"])
            major = run(label + "-major", compiler + ["-dumpversion"]).stdout.strip().split(".")[0]
            if major != "11": raise RuntimeError("GNU11 required; refusing an unlabelled toolchain change: " + label)
            (out / (label + "-compiler.txt")).write_text(version.stdout)
        common = ["-std=c++17", "-O2", "-pthread", "-Wall", "-Wextra", "-Werror", "-I", str(source)]
        shared = common + ["-fPIC", "-fvisibility=hidden", "-shared", "-Wl,-z,defs", "-Wl,-Bsymbolic",
                           "-Wl,--version-script=" + str(source / "fixtures/exports.map")]
        core, sdk, bridge = source / "capture.cpp", source / "fixtures/fake_sdk.cpp", source / "fixtures/core_bridge.cpp"
        run("build-c-header", cc + ["-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", "-I", str(source),
            str(source / "tests/header_c.c"), "-o", str(out / "header_c")])
        run("c-header-green", [out / "header_c"])
        for name, sources, defines in [
            ("single", [core, sdk, bridge], ["-DDS_CAPTURE_TESTING", "-Wl,-soname,libsingle.so"]),
            ("old-admission", [core, sdk, bridge], ["-DDS_CAPTURE_TESTING", "-DDS_CAPTURE_OLD_ADMISSION"]),
            ("allocation", [core, sdk, bridge], ["-DDS_CAPTURE_TESTING", "-DDS_CAPTURE_FAULT_ALLOC"]),
            ("wrong-abi", [source / "fixtures/wrong_abi.cpp"], [])]:
            run("build-" + name, cxx + shared + defines + [str(x) for x in sources] + ["-o", str(out / ("lib" + name + ".so"))])
        run("build-split", cxx + shared + ["-DDS_CAPTURE_TESTING", "-DDS_CAPTURE_SPLIT_TLS", str(sdk),
            str(out / "libsingle.so"), "-Wl,-rpath,$ORIGIN", "-o", str(out / "libsplit.so")])
        driver = out / "driver"
        run("build-driver", cxx + common + ["-DDS_CAPTURE_TESTING", str(source / "tests/driver.cpp"), "-rdynamic", "-ldl", "-o", str(driver)])
        run("old-admission-semantic-red", [driver, "--admission", out / "libold-admission.so"], expected=1,
            stderr_contains="FAIL exceptional admission seal contract", stdout_contains="counterexample=1")
        run("admission-seal-green", [driver, "--admission", out / "libsingle.so"],
            stdout_contains="PASS exceptional admission seal contract")
        red = run("split-tls-semantic-red", [driver, "--contract", out / "libsingle.so", out / "libsplit.so"], expected=1,
            stderr_contains="FAIL cross-DSO primary sample contract", stdout_contains="records=0")
        if "same_provider=1" not in red.stdout: raise RuntimeError("negative changed more than the SDK TLS route")
        run("allocation-semantic-red", [driver, "--contract", out / "liballocation.so", out / "liballocation.so"], expected=1,
            stderr_contains="FAIL RPC allocation contract")
        run("wrong-abi-rejected", [driver, "--contract", out / "libwrong-abi.so", out / "libwrong-abi.so"], expected=3,
            stderr_contains="getter ABI/bytes mismatch")
        run("single-tls-contract-green", [driver, "--contract", out / "libsingle.so", out / "libsingle.so"],
            stdout_contains="PASS cross-DSO primary sample contract")
        suite = run("single-tls-suite-green", [driver, "--suite", out / "libsingle.so"])
        if suite.stdout.count("PASS ") != 12 or "FAIL " in suite.stderr: raise RuntimeError("suite count/error mismatch")
        split_symbols = run("split-defined-symbols", ["nm", "-C", "--defined-only", out / "libsplit.so"]).stdout
        if "ds_bench_capture::(anonymous namespace)::" in split_symbols or "current_run" in split_symbols:
            raise RuntimeError("split fixture duplicates collector instead of only SDK TLS")
        split_deps = run("split-shared-provider", ["ldd", out / "libsplit.so"]).stdout
        if "libsingle.so" not in split_deps or "not found" in split_deps: raise RuntimeError("split provider dependency missing")
        symbols = run("dynamic-symbols", ["nm", "-D", "--defined-only", out / "libsingle.so"]).stdout
        expected_symbols = {"DsBenchRpcCaptureV2", "DsBenchFakeSdkEmitV2", "DsBenchFakeInstallHookV2",
                            "DsBenchTestObserveFromSlotV2", "DsBenchTestInstallHookV2"}
        exported = {line.split()[-1] for line in symbols.splitlines() if line.split()}
        if exported != expected_symbols: raise RuntimeError("fixture export set mismatch; private Observe/TLS must remain hidden")
        if {str(x.relative_to(source)): sha(x) for x in inputs} != hashes: raise RuntimeError("source changed during validation")
        passed = True
    except BaseException as e:
        reason = type(e).__name__ + ": " + str(e)
        raise
    finally:
        binaries = {p.name: sha(p) for p in out.iterdir() if p.is_file() and (p.suffix == ".so" or p.name in {"driver", "header_c"})}
        receipt = {"passed": passed, "reason": reason, "source_sha256": hashes, "binary_sha256": binaries,
                   "results": records, "semantic_red_is_new_contract_negative_not_old_SDK_bug": True,
                   "actual_SDK_hook_build_or_RPC30_evidence": False}
        (out / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")


if __name__ == "__main__":
    main()
