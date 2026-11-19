"""Opt-in builder tests: source patching and mock links, no compiler or SDK run."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import build_local as builder


class BoundedBuilder(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bounded-builder-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sdk = self.root/"libdatasystem.so"
        self.sdk.write_bytes(b"\x7fELFsynthetic identity, never executable")
        self.include = self.root/"include"
        for name in builder.SDK_HEADERS:
            path = self.include/name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("// Public-header fixture only.\n")

    def args(self, profile=None):
        argv = ["--sdk-dso", str(self.sdk), "--sdk-include", str(self.include),
                "--output", str(self.root/"output"), "--compiler", sys.executable]
        if profile is not None:
            argv += ["--admission-profile", profile]
        return builder.parser().parse_args(argv)

    def test_default_is_still_strict_and_exact_old_producer(self):
        self.assertEqual(self.args().admission_profile,"strict")
        sources,_,_ = builder.source_inputs()
        self.assertEqual(hashlib.sha256(sources["paced_callers_sdk.cpp"]).hexdigest(),
                         "09d995be2b38e1b0c6a09194d1abd5b0c0a0f7eab178e99f276f914e1414f78f")
        self.assertNotIn("bounded_admission.h",sources)

    def test_explicit_profile_selects_exact_new_sources_only(self):
        sources,_,_ = builder.source_inputs(admission_profile="bounded-v1")
        self.assertNotIn("paced_callers_sdk.cpp",sources)
        for name,digest in {
            "bounded_callers_sdk.cpp":"41cf09f1fd96f78c85905fadea69ec30e6a34493805a435d9a802d502f822e64",
            "bounded_admission.h":"5a9ad8423b9a510245a61341ea6715084ac0f275820e4bb43160b4538fa3ed45",
        }.items():self.assertEqual(hashlib.sha256(sources[name]).hexdigest(),digest)

    def test_cli_accepts_explicit_profile_without_starting_a_build(self):
        with contextlib.redirect_stderr(io.StringIO()):
            try:args=self.args("bounded-v1")
            except SystemExit as error:self.fail("explicit bounded profile rejected: "+str(error))
        self.assertEqual(args.admission_profile,"bounded-v1")
        self.assertFalse(args.output.exists())

    def test_patch_reuses_exact_helper_abi_plan_and_arrival(self):
        staged=self.root/"staged";staged.mkdir()
        result=builder.stage_sources(staged,admission_profile="bounded-v1")
        for name,digest in {
            "capture_helper.cpp":"ff3af0bb8fc8c7c2e05222ec490e74b3922241fb61e01205f70f5da24377a51d",
            "capture_abi.h":"d8a60b80a6cfbe4b2055a0ac8c1848a25ead7057556a73f3489bd4c378ca1a2c",
            "fixed_owner_plan.cpp":"64ef183f73dc7d79e096b66a28be5bc568c1ae2a441083def4a521a51df71962",
            "arrival_loop.h":"9ab8b4fcb4be173bf46556ff4f513735d3e500365de19e02a8af783dd755a3dd",
        }.items():self.assertEqual(result["source_sha256"][name],digest)
        self.assertFalse((staged/"paced_callers_sdk.cpp").exists())

    def test_unknown_profile_fails_before_source_staging(self):
        with self.assertRaisesRegex(ValueError,"unknown admission profile"):
            builder.source_inputs(admission_profile="bounded-v2")
        self.assertFalse((self.root/"output").exists())

    def test_existing_output_never_overwritten(self):
        output=self.root/"output";output.mkdir();(output/"sentinel").write_text("keep")
        with patch.object(builder.subprocess,"run") as run:
            with self.assertRaisesRegex(ValueError,"output already exists"):
                builder.build(self.args("bounded-v1"))
        run.assert_not_called()
        self.assertEqual((output/"sentinel").read_text(),"keep")

    def test_mock_links_choose_distinct_images_and_never_unit_provider(self):
        real_run=builder.subprocess.run
        for profile in (None,"bounded-v1"):
            with self.subTest(profile=profile):
                args=self.args(profile);args.output=self.root/(profile or "strict")
                commands=[]
                def run(argv,**kwargs):
                    if "--fuzz=0" in argv:return real_run(argv,**kwargs)
                    commands.append(argv)
                    (Path(kwargs["cwd"])/argv[argv.index("-o")+1]).write_bytes(b"\x7fELFmockcompileroutput")
                    return subprocess.CompletedProcess(argv,0)
                if profile is None:del args.admission_profile
                with patch.object(builder.subprocess,"run",side_effect=run),contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(builder.build(args),0)
                self.assertEqual(len(commands),2)
                report=json.loads((args.output/"build-result.json").read_text())
                prefix="fixed-owner-sdk" if profile is None else "bounded-owner-sdk"
                source="paced_callers_sdk.cpp" if profile is None else "bounded_callers_sdk.cpp"
                self.assertEqual(set(report["binary_sha256"]),{prefix,prefix+"-smoke"})
                self.assertFalse(report["runtime_executed"])
                self.assertFalse(report["supplied_sdk_provenance_qualified"])
                for command in commands:
                    self.assertIn(source,command)
                    self.assertEqual([v for v in command if v.endswith(".cpp")],
                                     [source,"fixed_owner_plan.cpp","capture_helper.cpp"])
                    for flag in ("-O2","-std=c++20","-fPIC","-pie"):self.assertIn(flag,command)


if __name__ == "__main__":
    unittest.main()
