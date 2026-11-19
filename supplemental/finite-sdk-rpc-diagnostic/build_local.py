#!/usr/bin/env python3
"""Build the isolated finite-work driver; never runs it or changes default targets."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

MODULE = Path(__file__).resolve().parent
HELPERS = MODULE.parent / "ds-latency30"
LOCAL_SOURCES = (
    "closed_loop_sdk.cpp",
    "closed_loop_plan.h",
    "plan_test.cpp",
    "anchor_probe.cpp",
)
HELPER_HASHES = {
    "capture_helper.h": "7ebb1c47d967af8ae250fea081f1469e9573c1badf98cf76691dc4f71303b35a",
    "capture_helper.cpp": "17d2eb3838ae9072fb14a8efc415fdd1f2603abf70642a6dabb6c565dccecd63",
    "capture_abi.h": "d8a60b80a6cfbe4b2055a0ac8c1848a25ead7057556a73f3489bd4c378ca1a2c",
}
SDK_HEADERS = ("datasystem/kv_client.h", "datasystem/utils/status.h")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def require(value: bool, message: str) -> None:
    if not value:
        raise ValueError(message)


def directory(value: Path, label: str) -> Path:
    path = value.expanduser().resolve(strict=True)
    require(path.is_dir(), label + " must be a directory")
    # RPATH uses ':' separators; -Wl passes comma-separated linker options.
    require(not any(char in str(path) for char in (":", ",", "\n", "\r", "\0")),
            label + " contains unsupported linker path characters")
    return path


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--sdk-dso", type=Path, required=True,
                        help="ELF SDK DSO; its directory must also resolve its SONAME")
    result.add_argument("--sdk-include", type=Path, required=True,
                        help="public SDK include directory")
    result.add_argument("--output", type=Path, required=True,
                        help="new build directory; its parent must already exist")
    result.add_argument("--compiler", default="c++", help="one C++ compiler executable")
    result.add_argument("--library-dir", type=Path, action="append", default=[],
                        help="additional link and runtime library directory; repeatable")
    return result


def build(args: argparse.Namespace) -> int:
    sdk = args.sdk_dso.expanduser().resolve(strict=True)
    require(sdk.is_file(), "SDK DSO must be a regular file")
    with sdk.open("rb") as stream:
        require(stream.read(4) == b"\x7fELF", "SDK DSO must be ELF")
    sdk_include = directory(args.sdk_include, "SDK include")
    sdk_dir = directory(sdk.parent, "SDK library")
    library_dirs = [sdk_dir]
    for value in args.library_dir:
        path = directory(value, "additional library")
        if path not in library_dirs:
            library_dirs.append(path)
    headers = {}
    for name in SDK_HEADERS:
        path = sdk_include / name
        require(path.is_file(), "required SDK public header is missing: " + name)
        headers[name] = file_digest(path)
    sources = {}
    for name in LOCAL_SOURCES:
        path = MODULE / name
        require(path.is_file(), "experiment source is missing: " + name)
        sources[name] = path.read_bytes()
    for name, expected in HELPER_HASHES.items():
        path = HELPERS / name
        require(path.is_file(), "capture helper is missing: " + name)
        data = path.read_bytes()
        require(digest(data) == expected, "capture helper source mismatch: " + name)
        sources[name] = data
    compiler_name = os.path.expanduser(args.compiler)
    compiler = shutil.which(compiler_name)
    require(compiler is not None, "C++ compiler executable is unavailable")
    compiler = str(Path(compiler).resolve(strict=True))
    original_output = args.output.expanduser().absolute()
    require(not os.path.lexists(original_output), "output already exists; no files overwritten")
    output = original_output.resolve(strict=False)
    require(output.parent.is_dir(), "output parent must already exist")
    sdk_hash = file_digest(sdk)

    # All explicit inputs have been validated before creating any output.
    output.mkdir()
    for name, data in sources.items():
        (output / name).write_bytes(data)
    source_hashes = {name: digest(data) for name, data in sources.items()}
    report = {
        "success": False,
        "scope": "isolated_driver_build_only_no_runtime_or_performance_qualification",
        "sdk_dso": str(sdk),
        "sdk_sha256": sdk_hash,
        "sdk_include": str(sdk_include),
        "sdk_public_header_sha256": headers,
        "source_sha256": source_hashes,
        "builder_sha256": file_digest(MODULE / "build_local.py"),
        "commands": [],
        "binary_sha256": {},
        "runtime_executed": False,
        "environment_recorded": False,
        "PIC_reason": (
            "Use PIC/PIE for dladdr of the SDK function anchor. The retained non-PIC "
            "probe attributed its PLT address to the executable; the PIC/PIE probe "
            "resolved the expected SDK without weakening the identity check."
        ),
        "limits": [
            "SDK SONAME and transitive dependencies must resolve through the supplied library directories.",
            "Only copied experiment sources and the two named SDK public headers are hashed; this is not a full toolchain dependency closure.",
            "No driver, test, anchor probe, or service is run by this builder.",
        ],
    }

    def save() -> None:
        (output / "build-result.json").write_text(json.dumps(report, indent=2) + "\n")

    common = [
        compiler, "-std=c++20", "-O2", "-pthread", "-Wall", "-Wextra", "-Werror",
        "-fPIC", "-pie", "-I", str(sdk_include), "-I", str(output),
    ]
    links = []
    for path in library_dirs:
        links += ["-L", str(path), "-Wl,-rpath," + str(path), "-Wl,-rpath-link," + str(path)]
    save()
    for label, binary, definitions in [
        ("formal", "closed-loop-sdk", []),
        ("smoke", "closed-loop-sdk-smoke", ["-DDS_CLOSED_LOOP_SMOKE"]),
    ]:
        command = common + definitions + [
            "closed_loop_sdk.cpp", "capture_helper.cpp", str(sdk), "-o", binary, "-ldl",
        ] + links
        row = {"label": label, "argv": command, "cwd": str(output), "returncode": None}
        report["commands"].append(row)
        save()
        try:
            with (output / (label + "-build.stdout")).open("wb") as stdout:
                with (output / (label + "-build.stderr")).open("wb") as stderr:
                    result = subprocess.run(command, cwd=output, stdout=stdout, stderr=stderr, check=False)
            row["returncode"] = result.returncode
            save()
            if result.returncode:
                print(json.dumps({"status": "BUILD_FAILED", "step": label, "returncode": result.returncode}))
                return result.returncode if 0 < result.returncode < 126 else 1
            require((output / binary).is_file(), "compiler reported success without an output binary")
            report["binary_sha256"][binary] = file_digest(output / binary)
            require(file_digest(sdk) == sdk_hash, "SDK DSO changed while building")
            require(all(file_digest(sdk_include / name) == expected for name, expected in headers.items()),
                    "SDK public headers changed while building")
            require(all(file_digest(output / name) == expected for name, expected in source_hashes.items()),
                    "copied sources changed while building")
        except (OSError, ValueError) as error:
            row["error_type"] = type(error).__name__
            save()
            print(json.dumps({"status": "BUILD_FAILED", "step": label, "error_type": type(error).__name__}))
            return 1
        save()
    report["success"] = True
    save()
    print(json.dumps({"status": "FORMAL_AND_SMOKE_BUILT_NOT_RUN", "binaries": list(report["binary_sha256"])}))
    return 0


def main() -> int:
    args = parser().parse_args()
    try:
        return build(args)
    except (OSError, ValueError) as error:
        print("Input validation or build setup failed: " + str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
