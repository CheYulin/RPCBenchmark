#!/usr/bin/env python3
"""Build/run five fake-provider unit groups; never build a real SDK driver."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import platform
import shlex
import shutil
import signal
import subprocess

HERE = Path(__file__).resolve().parent
UNIT = HERE / "tests" / "unit"
GROUPS = (
    ("arrival", "arrival_loop_test.cpp", "PASS "),
    ("plan", "plan_test.cpp", "FIXED_OWNER_PLAN_TESTS_PASS"),
    ("end-boundary", "end_boundary_test.cpp", "FIXED_OWNER_END_CEIL_PASS"),
    ("session", "session_test.cpp", "FIXED_OWNER_SESSION_TESTS_PASS"),
    ("mode-snapshot", "mode_snapshot_test.cpp", "FIXED_OWNER_MODE_SNAPSHOT_PASS"),
)


def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write(path: Path, value) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk-include", type=Path, required=True,
                        help="directory containing datasystem/kv_client.h; headers only")
    parser.add_argument("--output", type=Path, required=True, help="new output directory")
    parser.add_argument("--compiler", required=True, help="Linux C++20 compiler executable")
    parser.add_argument("--helper-base", type=Path,
                        help="optional pinned ds-latency30 helper directory for stage_sources")
    parser.add_argument("--build-only", action="store_true", help="compile units without running them")
    return parser.parse_args(argv)


def identity(pid):
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return dict(pid=pid, pgrp=int(fields[2]), session=int(fields[3]), startticks=int(fields[19]))


def same(owner):
    try:
        return identity(owner["pid"]) == owner
    except (FileNotFoundError, ProcessLookupError):
        return False


def session_members(owner):
    found = []
    for path in Path("/proc").iterdir():
        if not path.name.isdecimal():
            continue
        try:
            value = identity(int(path.name))
        except (OSError, ProcessLookupError):
            continue
        if value["session"] == owner["session"] and value["startticks"] >= owner["startticks"]:
            found.append(value)
    return found


def run(command, output, label, env, timeout):
    command = list(map(str, command))
    write(output / (label + "-command.json"), dict(argv=command, timeout_seconds=timeout))
    timed_out, sent = False, []
    with (output / (label + ".stdout")).open("xb") as out, (output / (label + ".stderr")).open("xb") as err:
        process = subprocess.Popen(command, cwd=output, env=env, stdin=subprocess.DEVNULL,
                                   stdout=out, stderr=err, start_new_session=True)
        owner = identity(process.pid)
        if owner["pid"] != owner["pgrp"] or owner["pid"] != owner["session"]:
            raise RuntimeError("unit child has no private session")
        write(output / (label + "-owner.json"), owner)
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            if process.poll() is None:
                for sig in (signal.SIGTERM, signal.SIGKILL):
                    if same(owner):
                        os.killpg(owner["pgrp"], sig)
                        sent.append(int(sig))
                    else:
                        for child in session_members(owner):
                            if same(child):
                                os.kill(child["pid"], sig)
                                sent.append(int(sig))
                    try:
                        process.wait(timeout=2)
                        break
                    except subprocess.TimeoutExpired:
                        continue
                process.wait(timeout=2)
    remaining = session_members(owner)
    result = dict(exit_code=process.returncode, timed_out=timed_out, signals=sent,
                  leader_gone=not same(owner), remaining_session=remaining)
    write(output / (label + "-result.json"), result)
    if process.returncode != 0 or timed_out or sent or remaining or not result["leader_gone"]:
        raise RuntimeError(label + " failed; inspect the output command/result logs")
    return result


def stage_sources(output, helper_base):
    spec = importlib.util.spec_from_file_location("fixed_owner_public_builder", HERE / "build_local.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.stage_sources(output, helper_base=helper_base)


def build_unit(args):
    if platform.system() != "Linux":
        raise RuntimeError("C++ unit builds require Linux; --help and parser checks are portable")
    sdk_include = args.sdk_include.resolve()
    if not (sdk_include / "datasystem" / "kv_client.h").is_file():
        raise ValueError("--sdk-include must contain datasystem/kv_client.h")
    compiler = shutil.which(args.compiler)
    if compiler is None:
        raise ValueError("--compiler executable is unavailable")
    compiler = str(Path(compiler).resolve())
    manifest = json.loads((UNIT / "source-bindings.json").read_text())
    unit_sources = {}
    for relative, digest in manifest["source_sha256"].items():
        name = PurePosixPath(relative)
        if name.is_absolute() or ".." in name.parts or name.parts[:2] != ("tests", "unit"):
            raise ValueError("unit source binding is outside tests/unit")
        path = HERE.joinpath(*name.parts)
        if path.is_symlink() or not path.is_file() or sha(path) != digest:
            raise ValueError("unit source binding changed")
        unit_sources[path] = digest
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    staged = output / "sources"
    staged.mkdir()
    staging = stage_sources(staged, args.helper_base)
    staged_sources = {staged / name: digest for name, digest in staging["source_sha256"].items()}
    all_sources = {**unit_sources, **staged_sources}
    if not all(path.is_file() and sha(path) == digest for path, digest in all_sources.items()):
        raise RuntimeError("staged unit source closure changed")
    write(output / "unit-source-bindings.json", dict(
        unit_sources=manifest["source_sha256"], staging=staging,
        fake_provider_only=True, real_SDK_network_or_performance=False))
    fake = output / "unit-fake-provider"
    binaries = output / "bin"
    results = output / "test-output"
    fake.mkdir(); binaries.mkdir(); results.mkdir()
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("DATASYSTEM_", "DS_BENCH_", "GTEST_"))
           and key not in ("LD_PRELOAD", "LD_AUDIT")}
    previous_ld = env.get("LD_LIBRARY_PATH", "")
    env["LD_LIBRARY_PATH"] = str(fake) + (os.pathsep + previous_ld if previous_ld else "")
    run([compiler, "--version"], output, "compiler-version", env, 10)
    common = [compiler, "-std=c++20", "-O2", "-g1", "-pthread", "-Wall", "-Wextra", "-Werror",
              "-I" + str(staged), "-I" + str(UNIT / "provider"), "-I" + str(sdk_include)]
    provider = UNIT / "provider"
    library = fake / "libdatasystem.so"
    provider_command = common + ["-fPIC", "-fvisibility=hidden", "-shared", "-Wl,-z,defs",
        "-Wl,-Bsymbolic", "-DDS_CAPTURE_TESTING", str(provider / "capture.cpp"),
        str(provider / "fake_sdk.cpp"), str(provider / "core_bridge.cpp"),
        str(provider / "anchor.cpp"), "-Wl,--version-script=" + str(provider / "exports-anchor.map"),
        "-Wl,-soname,libdatasystem.so", "-o", str(library)]
    run(provider_command, output, "unit-provider-build", env, 120)
    images, tests = {}, []
    for name, source, marker in GROUPS:
        binary = binaries / (name + "-test")
        command = common + ["-fPIC", "-pie", "-MD", "-MF", str(output / (name + ".d")), str(UNIT / source)]
        if name != "arrival":
            command.append(str(staged / "fixed_owner_plan.cpp"))
        if name in ("session", "mode-snapshot"):
            command += [str(staged / "capture_helper.cpp"), str(library),
                        "-Wl,-rpath,$ORIGIN/../unit-fake-provider", "-ldl"]
        command += ["-o", str(binary)]
        run(command, output, name + "-build", env, 120)
        images[name] = dict(path=str(binary), sha256=sha(binary), bytes=binary.stat().st_size)
        if args.build_only:
            tests.append(dict(group=name, status="NOT_RUN_BUILD_ONLY"))
            continue
        argv = [binary]
        if name in ("session", "mode-snapshot"):
            argv += [results / name, library]
        run(argv, output, name + "-run", env, 8)
        text = (output / (name + "-run.stdout")).read_text()
        if marker not in text:
            raise RuntimeError(name + " passed without its expected unit marker")
        tests.append(dict(group=name, status="PASS", deadline_seconds=8))
    if not all(sha(path) == digest for path, digest in all_sources.items()):
        raise RuntimeError("unit source inputs changed during build/run")
    dependencies = {}
    for depfile in output.glob("*.d"):
        text = depfile.read_text().replace("\\\n", " ").split(":", 1)[1]
        for name in shlex.split(text):
            path = Path(name)
            path = path if path.is_absolute() else output / path
            if path.is_file():
                dependencies[str(path.resolve())] = sha(path)
    receipt = dict(status="UNIT_BUILD_ONLY_NOT_RUN" if args.build_only else "FIVE_UNIT_GROUPS_PASS",
        tests=tests, images=images, fake_provider_sha256=sha(library), fake_provider_only=True,
        real_SDK_library_linked=False, real_SDK_network_or_performance=False,
        production_driver_compiled=False, source_inputs_post_match=True,
        compiler=compiler, dependency_files_recorded_after_compile=dependencies,
        dependency_scope="Dependency headers are recorded after compile, not a full precompile snapshot.")
    write(output / "unit-result.json", receipt)
    return receipt


def main(argv=None):
    args = parse_args(argv)
    try:
        result = build_unit(args)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(json.dumps(dict(status="UNIT_BUILD_OR_RUN_FAILED", error=str(error),
                              fake_provider_only=True, real_SDK_network_or_performance=False)))
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
