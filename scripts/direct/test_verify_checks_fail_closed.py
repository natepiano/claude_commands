"""Broken admission and hold checks must refuse local build steps."""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import unittest
from typing import cast, override
from scripts.build_queue.admission_test_support import AdmissionFixture, caller_admission
from scripts.build_queue import admission_protocol as wire

from scripts.direct import test_verify_mac_offload as offload_tests
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


CHECK_STUB = '''
import os
from pathlib import Path
import sys

command = sys.argv[1]
if command == os.environ.get("TEST_BROKEN_CHECK"):
    mode = os.environ["TEST_CHECK_FAILURE"]
    if mode == "usage":
        print("usage: invalid choice: check-admission", file=sys.stderr)
        raise SystemExit(2)
    if mode == "crash":
        raise RuntimeError("check failed")
    if mode == "empty":
        raise SystemExit(0)
    if mode == "invalid":
        print("not a target table")
        raise SystemExit(0)
    if mode == "tabbed-settings":
        print("mac_allowed")
        print("mac\\teligible\\t-\\t/tmp/mac\\tsettings.conf\\tmacos")
        raise SystemExit(0)
    if mode == "held":
        raise SystemExit(10)
    if mode == "error":
        raise SystemExit(12)
if command == "eligible":
    print("mac_allowed" if os.environ.get("TEST_ELIGIBLE_MAC") else "natedev_only test target")
    print("natedev\\teligible\\t-\\t-\\t-")
    if os.environ.get("TEST_ELIGIBLE_MAC"):
        print("mac\\teligible\\t-\\t" + os.environ["MAC_TEST_CONFIG"] + "\\tmacos")
if command == "wait-local" and os.environ.get("TEST_REMOVE_CHECK") == "1":
    Path(__file__).unlink()
raise SystemExit(0)
'''



class VerifyChecksFailClosedTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.fixture: offload_tests.VerifyMacOffloadTests = offload_tests.VerifyMacOffloadTests("runTest")
        self.targets: Path = Path()
        self.memory: Path = Path()
        self.admission: AdmissionFixture = cast(AdmissionFixture, cast(object, None))
        self.invoke: Path = Path()

    @override
    def setUp(self) -> None:
        self.fixture.setUp()
        self.addCleanup(self.clean_fixture)
        scripts = self.fixture.home / ".claude/scripts"
        lint = scripts / "lint"
        lint.unlink()
        lint.mkdir()
        source = offload_tests.VERIFY.parent.parent / "lint"
        for path in source.iterdir():
            target = lint / path.name
            if path.name in {"invoke.sh", "memory_gate.sh"}:
                _ = shutil.copyfile(path, target)
            else:
                target.symlink_to(path, target_is_directory=path.is_dir())
        queue = scripts / "build_queue"
        queue.unlink()
        queue.mkdir()
        for path in (source.parent / "build_queue").iterdir():
            if path.name != "admission_client.py":
                (queue / path.name).symlink_to(path, target_is_directory=path.is_dir())
        self.memory = queue / "admission_client.py"
        self.restore_client()
        self.invoke = lint / "invoke.sh"
        self.targets = self.fixture.root / "check.py"
        _ = self.targets.write_text(CHECK_STUB)
        holders = Path(self.fixture.environment["BUILD_HOLD_DIR"])
        holders.mkdir()
        _ = (holders / "holder").write_text("test hold")
        self.fixture.environment.update({
            "BUILD_TARGETS_SCRIPT": str(self.targets),
            "TEST_RESERVATION": str(self.fixture.root / "step.reservation"),
            "TEST_BROKEN_CHECK": "", "TEST_CHECK_FAILURE": "",
            "TEST_MEMORY_FAILURE": "", "TEST_REMOVE_CHECK": "",
            "TEST_ELIGIBLE_MAC": "",
            "BUILDLOG_MAC_IDLE_HOOK": "", "LINT_QUEUE_LEAVE_HOOK": "",
            "UNIT_RUN_DIR": "", "UNIT_SESSION_DIR": "", "UNIT_TEAM_ROLE": "",
            "BUILDLOG_CHECK_FAILED_FILE": "", "LINT_SKIP_SWEEP": "1",
        })
        self.admission = caller_admission(self, self.fixture.root, self.fixture.environment)
        self.admission.server.line_filter = self.failure_line

    def restore_client(self) -> None:
        shim = str(Path(__file__).resolve().parents[1] / "lib/py")
        _ = self.memory.write_text(
            f"import os, sys\nos.execv({shim!r}, [{shim!r}, '-c', "
            + "\"from scripts.build_queue.admission_client import main; raise SystemExit(main())\", *sys.argv[1:]])\n"
        )

    def failure_line(self, line: str) -> str:
        mode = self.fixture.environment.get("TEST_MEMORY_FAILURE", "")
        if not mode:
            return line
        if mode in ("usage", "crash"):
            return wire.encode_reply(wire.Refused("memory check failed"))
        return {"empty": "\n", "invalid": "admit\n", "unknown": '{"kind":"deny"}\n',
                "numeric": '{"kind":"admitted","need":"bad"}\n',
                "untracked": '{"kind":"admitted"}\n'}.get(mode, line)

    def clean_fixture(self) -> None:
        self.assertTrue(self.fixture.doCleanups(), "fixture cleanup failed")

    def reset_observations(self) -> None:
        self.fixture.cargo_events_path.unlink(missing_ok=True)
        _ = self.targets.write_text(CHECK_STUB)
        self.restore_client()

    def run_step(self) -> subprocess.CompletedProcess[str]:
        # A conditional disables errexit inside the function. The checks must
        # still return their error before cargo starts.
        command = ('source "$1" || exit $?; sweep_after_step() { :; }; '
                   'if run_once cargo check; then exit 0; else exit $?; fi')
        return subprocess.run(
            ["bash", "-c", command, "test", str(self.invoke)],
            cwd=self.fixture.root, env=self.fixture.environment,
            capture_output=True, text=True, check=False,
            timeout=TEST_HANG_GUARD_SECS,
        )

    def assert_refused(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("step refused", result.stdout + result.stderr)
        self.assertEqual([event for event in self.fixture.cargo_events()
                          if event["args"][:1] != ["metadata"]], [])

    def test_hold_usage_errors_and_crashes_refuse_before_cargo(self) -> None:
        for command in ("wait-local", "check-local"):
            for failure in ("usage", "crash", "error"):
                with self.subTest(command=command, failure=failure):
                    self.reset_observations()
                    self.fixture.environment.update({"TEST_BROKEN_CHECK": command,
                                                     "TEST_CHECK_FAILURE": failure})
                    self.assert_refused(self.run_step())

    def test_missing_hold_script_refuses_before_cargo(self) -> None:
        for stage in ("wait-local", "check-local"):
            with self.subTest(stage=stage):
                self.reset_observations()
                self.fixture.environment["TEST_REMOVE_CHECK"] = "1" if stage == "check-local" else ""
                if stage == "wait-local":
                    self.targets.unlink()
                self.assert_refused(self.run_step())

    def test_memory_errors_and_invalid_answers_refuse_before_cargo(self) -> None:
        for failure in ("usage", "crash", "empty", "invalid", "unknown", "numeric", "untracked", "missing"):
            with self.subTest(failure=failure):
                self.reset_observations()
                self.fixture.environment["TEST_MEMORY_FAILURE"] = failure
                if failure == "missing":
                    self.memory.unlink()
                self.assert_refused(self.run_step())

    def test_missing_or_invalid_memory_reading_refuses_before_cargo(self) -> None:
        meminfo = Path(self.fixture.environment["BUILDLOG_MEMINFO"])
        for contents in ("", "MemTotal: 67108864 kB\n", "missing"):
            with self.subTest(contents=contents):
                self.reset_observations()
                if contents == "missing":
                    meminfo.unlink()
                else:
                    _ = meminfo.write_text(contents)
                self.assert_refused(self.run_step())

    def test_explicit_memory_and_hold_allow_runs_cargo(self) -> None:
        result = self.run_step()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([event["args"] for event in self.fixture.cargo_events()], [["check"]])

    def test_verify_wait_check_error_and_missing_path_refuse_before_cargo(self) -> None:
        for failure in ("usage", "crash", "missing"):
            with self.subTest(failure=failure):
                self.reset_observations()
                self.fixture.environment.update({"TEST_BROKEN_CHECK": "wait-local",
                                                 "TEST_CHECK_FAILURE": failure})
                if failure == "missing":
                    self.targets.unlink()
                self.assert_refused(self.fixture.verify("check", "sample", "--no-cache"))

    def test_verify_eligibility_errors_and_invalid_output_refuse_before_cargo(self) -> None:
        for failure in ("usage", "crash", "empty", "invalid", "missing"):
            with self.subTest(failure=failure):
                self.reset_observations()
                self.fixture.environment.update({"TEST_BROKEN_CHECK": "eligible",
                                                 "TEST_CHECK_FAILURE": failure})
                if failure == "missing":
                    self.targets.unlink()
                self.assert_refused(self.fixture.verify("test", "sample", "--filter", "smoke", "--no-cache"))

    def test_verify_routing_hold_error_refuses_before_cargo(self) -> None:
        for failure in ("usage", "crash"):
            with self.subTest(failure=failure):
                self.reset_observations()
                self.fixture.environment.update({"TEST_BROKEN_CHECK": "register-local",
                                                 "TEST_CHECK_FAILURE": failure})
                self.assert_refused(self.fixture.verify("test", "sample", "--filter", "smoke", "--no-cache"))

    def test_settings_path_with_a_tab_refuses_before_the_step(self) -> None:
        self.fixture.environment.update({
            "TEST_BROKEN_CHECK": "eligible", "TEST_CHECK_FAILURE": "tabbed-settings",
        })
        result = self.fixture.verify("test", "sample", "--filter", "smoke", "--no-cache")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("target eligibility check returned invalid targets; step refused", result.stderr)
        self.assertEqual(self.fixture.runner_events(), [])
        self.assertEqual([event for event in self.fixture.cargo_events()
                          if event["args"][:1] != ["metadata"]], [])

    def test_routing_hold_error_refuses_before_a_passing_mac_runner(self) -> None:
        self.fixture.environment.update({
            "TEST_BROKEN_CHECK": "register-local", "TEST_CHECK_FAILURE": "error",
            "TEST_ELIGIBLE_MAC": "1", "TEST_RUNNER_MAC": "passed", "TEST_RUNNER_STATUS": "0",
        })
        result = self.fixture.verify("test", "sample", "--filter", "smoke", "--no-cache")
        self.assert_refused(result)
        self.assertEqual(result.returncode, 12, result.stdout + result.stderr)
        self.assertIn("build hold check register-local failed (exit 12); step refused", result.stderr)
        self.assertEqual(self.fixture.runner_events(), [])

    def test_verify_memory_check_error_refuses_before_cargo(self) -> None:
        self.fixture.environment["TEST_MEMORY_FAILURE"] = "crash"
        self.assert_refused(self.fixture.verify("check", "sample", "--no-cache"))

    def test_missing_check_library_refuses_before_cargo(self) -> None:
        source = offload_tests.VERIFY.parent.parent / "lint"
        for name in ("invoke.sh", "memory_gate.sh"):
            with self.subTest(library=name):
                target = self.invoke.parent / name
                target.unlink()
                try:
                    self.assert_refused(self.fixture.verify("check", "sample", "--no-cache"))
                finally:
                    _ = shutil.copyfile(source / name, target)

    def test_routing_release_check_error_refuses_before_local_build(self) -> None:
        release = self.fixture.root / "broken-release.py"
        _ = release.write_text('raise RuntimeError("release check failed")\n')
        self.fixture.environment.update({
            "VERIFY_LOCAL_RELEASE_WAIT_SCRIPT": str(release),
            "TEST_ELIGIBLE_MAC": "1", "TEST_BROKEN_CHECK": "register-local",
            "TEST_CHECK_FAILURE": "held", "TEST_RUNNER_MAC": "declined",
            "TEST_RUNNER_REASON": "busy", "TEST_RUNNER_STATUS": "75",
        })
        self.assert_refused(self.fixture.verify("test", "sample", "--filter", "smoke", "--no-cache"))

    def test_check_failure_cannot_continue_to_a_format_step(self) -> None:
        refused = self.fixture.root / "check-failed"
        self.fixture.environment.update({"BUILDLOG_CHECK_FAILED_FILE": str(refused),
                                         "TEST_MEMORY_FAILURE": "crash"})
        command = ('source "$1" || exit $?; sweep_after_step() { :; }; '
                   'run_once cargo check || :; '
                   'if run_once cargo fmt; then exit 0; else exit $?; fi')
        result = subprocess.run(
            ["bash", "-c", command, "test", str(self.invoke)],
            cwd=self.fixture.root, env=self.fixture.environment,
            capture_output=True, text=True, check=False,
            timeout=TEST_HANG_GUARD_SECS,
        )
        self.assert_refused(result)
        self.assertTrue(refused.exists())

    def test_verify_explicit_allow_runs_the_requested_step(self) -> None:
        result = self.fixture.verify("check", "sample", "--no-cache")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(any(event["args"][:1] == ["check"] for event in self.fixture.cargo_events()))


if __name__ == "__main__":
    _ = unittest.main()
