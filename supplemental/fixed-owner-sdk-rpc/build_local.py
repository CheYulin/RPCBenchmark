#!/usr/bin/env python3
"""Build isolated fixed-owner real-SDK drivers; never run either binary."""
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
    "paced_callers_sdk.cpp", "fixed_owner_plan.h", "fixed_owner_plan.cpp",
    "arrival_loop.h", "closed_loop_plan.h",
)
HELPER_HASHES = {
    "capture_helper.h": "7ebb1c47d967af8ae250fea081f1469e9573c1badf98cf76691dc4f71303b35a",
    "capture_helper.cpp": "17d2eb3838ae9072fb14a8efc415fdd1f2603abf70642a6dabb6c565dccecd63",
    "capture_abi.h": "d8a60b80a6cfbe4b2055a0ac8c1848a25ead7057556a73f3489bd4c378ca1a2c",
}
PATCHED_HASHES = {
    "capture_helper.h": "4c6645ef682e98990636247854cc50929353ffb86a4b990bd619613388d346d8",
    "capture_helper.cpp": "ff3af0bb8fc8c7c2e05222ec490e74b3922241fb61e01205f70f5da24377a51d",
}
PATCH_SHA256 = "7a8d510d063c796b07a18d41505e1f09bf32a23d6e02885c610d1ce95f000917"
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
    require(not any(char in str(path) for char in (":", ",", "\n", "\r", "\0")),
            label + " contains unsupported linker path characters")
    return path


def source_inputs(helper_base: Path | None = None, *, admission_profile="strict") -> tuple[dict[str, bytes], bytes, str]:
    base = directory(helper_base if helper_base is not None else HELPERS, "capture helper base")
    require(admission_profile in ("strict", "bounded-v1"), "unknown admission profile")
    local = LOCAL_SOURCES if admission_profile == "strict" else (
        "bounded_callers_sdk.cpp", "bounded_admission.h", *LOCAL_SOURCES[1:])
    sources = {name: (MODULE / name).read_bytes() for name in local}
    for name, expected in HELPER_HASHES.items():
        data = (base / name).read_bytes()
        require(digest(data) == expected, "capture helper source mismatch: " + name)
        sources[name] = data
    patch = (MODULE / "capture_helper.patch").read_bytes()
    require(digest(patch) == PATCH_SHA256, "capture helper patch source mismatch")
    executable = shutil.which("patch")
    require(executable is not None, "system patch executable is unavailable")
    return sources, patch, str(Path(executable).resolve(strict=True))


def stage_sources(output: Path, helper_base: Path | None = None, *, admission_profile="strict") -> dict:
    """Stage only hashed base sources and apply the isolated typed-owner patch."""
    sources, patch, executable = source_inputs(helper_base, admission_profile=admission_profile)
    require(output.is_dir(), "staging output must be an existing directory")
    require(not any(output.iterdir()), "staging output must be empty; no files overwritten")
    for name, data in sources.items():
        (output / name).write_bytes(data)
    (output / "capture_helper.patch").write_bytes(patch)
    command = [executable, "--batch", "--forward", "--fuzz=0", "--no-backup-if-mismatch",
               "-p0", "-i", "capture_helper.patch"]
    with (output / "patch.stdout").open("wb") as stdout, (output / "patch.stderr").open("wb") as stderr:
        result = subprocess.run(command, cwd=output, stdout=stdout, stderr=stderr, check=False)
    require(result.returncode == 0, "typed-owner helper patch failed; staging retained")
    for name, expected in {**PATCHED_HASHES, "capture_abi.h": HELPER_HASHES["capture_abi.h"]}.items():
        require(file_digest(output / name) == expected, "patched helper source mismatch: " + name)
    return {"source_sha256": {name: file_digest(output / name) for name in sources},
            "helper_base_sha256": dict(HELPER_HASHES), "patch_sha256": digest(patch),
            "patch_command": command, "patch_returncode": result.returncode}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--sdk-dso", type=Path, required=True, help="real ELF SDK DSO")
    result.add_argument("--sdk-include", type=Path, required=True, help="public SDK include directory")
    result.add_argument("--output", type=Path, required=True, help="new isolated build directory")
    result.add_argument("--compiler", default="c++", help="one C++ compiler executable")
    result.add_argument("--library-dir", type=Path, action="append", default=[],
                        help="additional link and runtime library directory; repeatable")
    result.add_argument("--helper-base", type=Path, default=HELPERS,
                        help="unmodified ds-latency30 helper directory; exact source hashes are required")
    result.add_argument("--admission-profile", choices=("strict", "bounded-v1"), default="strict",
                        help="strict keeps max1ms; bounded-v1 builds distinct opt-in sources/binaries")
    return result


def build(args: argparse.Namespace) -> int:
    sdk = args.sdk_dso.expanduser().resolve(strict=True)
    require(sdk.is_file(), "SDK DSO must be a regular file")
    with sdk.open("rb") as stream:
        require(stream.read(4) == b"\x7fELF", "SDK DSO must be ELF")
    sdk_include = directory(args.sdk_include, "SDK include")
    library_dirs = [directory(sdk.parent, "SDK library")]
    for value in args.library_dir:
        path = directory(value, "additional library")
        if path not in library_dirs:
            library_dirs.append(path)
    headers = {}
    for name in SDK_HEADERS:
        path = sdk_include / name
        require(path.is_file(), "required SDK public header is missing: " + name)
        headers[name] = file_digest(path)
    profile = getattr(args, "admission_profile", "strict")
    sources, patch, _ = source_inputs(args.helper_base, admission_profile=profile)
    compiler = shutil.which(os.path.expanduser(args.compiler))
    require(compiler is not None, "C++ compiler executable is unavailable")
    compiler = str(Path(compiler).resolve(strict=True))
    original_output = args.output.expanduser().absolute()
    require(not os.path.lexists(original_output), "output already exists; no files overwritten")
    output = original_output.resolve(strict=False)
    require(output.parent.is_dir(), "output parent must already exist")
    sdk_hash = file_digest(sdk)
    output.mkdir()
    staged = stage_sources(output, args.helper_base, admission_profile=profile)
    require(staged["patch_sha256"] == digest(patch), "patch changed during staging")
    for name, data in sources.items():
        expected = PATCHED_HASHES.get(name, digest(data))
        require(staged["source_sha256"][name] == expected, "source changed during staging: " + name)
    report = {
        "success": False,
        "scope": "fixed_owner_real_sdk_driver_build_only_not_runtime_or_performance_qualification",
        "admission_profile": profile,
        "sdk_dso": str(sdk), "sdk_sha256": sdk_hash, "sdk_include": str(sdk_include),
        "sdk_public_header_sha256": headers, **staged,
        "builder_sha256": file_digest(MODULE / "build_local.py"),
        "commands": [], "binary_sha256": {}, "runtime_executed": False,
        "unit_fixture_sources_linked": False, "supplied_sdk_provenance_qualified": False,
        "RPC30_accepted": False,
        "limits": [
            "The supplied SDK must be real; this builder cannot establish its provenance from ELF magic alone.",
            "SDK SONAME and transitive dependencies must resolve through the supplied library directories.",
            "Named sources and two public SDK headers are hashed; this is not a full toolchain dependency closure.",
            "No driver, test, service, or calibration is run by this builder.",
        ],
    }

    def save() -> None:
        (output / "build-result.json").write_text(json.dumps(report, indent=2) + "\n")

    common = [compiler, "-std=c++20", "-O2", "-pthread", "-Wall", "-Wextra", "-Werror",
              "-fPIC", "-pie", "-I", str(sdk_include), "-I", str(output)]
    links = []
    for path in library_dirs:
        links += ["-L", str(path), "-Wl,-rpath," + str(path), "-Wl,-rpath-link," + str(path)]
    save()
    driver = "paced_callers_sdk.cpp" if profile == "strict" else "bounded_callers_sdk.cpp"
    binary_prefix = "fixed-owner-sdk" if profile == "strict" else "bounded-owner-sdk"
    for label, binary, definitions in [("formal", binary_prefix, []),
                                       ("smoke", binary_prefix+"-smoke", ["-DDS_FIXED_OWNER_SMOKE"])]:
        command = common + definitions + [driver, "fixed_owner_plan.cpp",
                                         "capture_helper.cpp", str(sdk), "-o", binary, "-ldl"] + links
        row = {"label": label, "argv": command, "cwd": str(output), "returncode": None}
        report["commands"].append(row)
        save()
        try:
            with (output / (label + "-build.stdout")).open("wb") as stdout, (output / (label + "-build.stderr")).open("wb") as stderr:
                result = subprocess.run(command, cwd=output, stdout=stdout, stderr=stderr, check=False)
            row["returncode"] = result.returncode
            save()
            if result.returncode:
                return result.returncode if 0 < result.returncode < 126 else 1
            require((output / binary).is_file(), "compiler reported success without an output binary")
            report["binary_sha256"][binary] = file_digest(output / binary)
            require(file_digest(sdk) == sdk_hash, "SDK DSO changed while building")
            require(all(file_digest(sdk_include / name) == expected for name, expected in headers.items()),
                    "SDK public headers changed while building")
            require(all(file_digest(output / name) == expected for name, expected in staged["source_sha256"].items()),
                    "staged sources changed while building")
            current_sources, current_patch, _ = source_inputs(args.helper_base, admission_profile=profile)
            require(current_sources == sources and current_patch == patch, "original source inputs changed while building")
        except (OSError, ValueError) as error:
            row["error_type"] = type(error).__name__
            save()
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
