"""Real verification hold waits: lint."""

from __future__ import annotations

import subprocess
import shutil
import unittest
from pathlib import Path

from scripts.direct.verify_lint_cache_test_support import VerifyLintCacheFixture
from scripts.direct.verify_hold_wait_test_support import (
    VERIFY,
    VerifyHoldWaitFixture,
    grant_call,
    read_cycle_file,
)


class VerifyHoldWaitTests(VerifyHoldWaitFixture):
    def test_lint_step_without_a_call_id_waits_for_the_release(self) -> None:
        # The release wakes the wait; no poll comes in time.
        self.fixture.environment["BUILD_TARGETS_POLL_S"] = "3600"
        probe = self.fixture.root / "step-call-ids"
        for caller, speaker in (("cargo-port", "lint (cargo-port)"), ("", "lint")):
            with self.subTest(caller=caller):
                self.fixture.cargo_events_path.unlink(missing_ok=True)
                self.hold()
                process = self.start_lint_step(caller, probe=probe)
                _ = self.wait_for_call(process, f"{speaker}: natedev is held by hana for render checks; waiting")
                self.assertEqual(self.fixture.cargo_events(), [])
                grant_call(self.cycle_file)
                code, output = self.finish(process)
                self.assertEqual(code, 0, output)
                self.assertNotIn("verify.sh", output)
                self.assertNotIn("Mac", output)
                self.assertEqual([event["args"][0] for event in self.fixture.cargo_events()], ["check"])
                self.assertEqual(read_cycle_file(self.cycle_file)["entries"], [])
                # Lint keeps that id out of the build log: the caller has no call record.
                self.assertEqual(probe.read_text(), "|")

    def test_lint_step_with_no_hold_starts_no_hold_check(self) -> None:
        holders = Path(self.fixture.environment["BUILD_HOLD_DIR"])
        starts = self.count_python_starts()
        for state in ("missing", "empty"):
            with self.subTest(holders=state):
                starts.unlink(missing_ok=True)
                if state == "empty":
                    holders.mkdir()
                code, output = self.finish(self.start_lint_step("cargo-port"))
                self.assertEqual(code, 0, output)
                self.assertEqual(self.hold_commands(starts), [])

    def test_holder_session_lint_step_passes_its_own_hold(self) -> None:
        self.hold("holder-session")
        before = self.cycle_file.read_bytes()
        process = self.start_lint_step("cargo-port", {"CLAUDE_CODE_SESSION_ID": "holder-session"})
        code, output = self.finish(process)
        self.assertEqual(code, 0, output)
        self.assertNotIn("natedev is held", output)
        self.assertEqual(self.cycle_file.read_bytes(), before)
        self.assertEqual([event["args"][0] for event in self.fixture.cargo_events()], ["check"])

    def test_holder_session_lint_step_waits_while_another_hold_is_in_force(self) -> None:
        self.hold()
        holders = Path(self.fixture.environment["BUILD_HOLD_DIR"])
        _ = (holders / "holder-session").write_text((holders / "hana").read_text())
        process = self.start_lint_step("cargo-port", {"CLAUDE_CODE_SESSION_ID": "holder-session"})
        _ = self.wait_for_call(process, "lint (cargo-port): natedev is held by hana")
        self.assertEqual(self.fixture.cargo_events(), [])
        grant_call(self.cycle_file)
        code, output = self.finish(process)
        self.assertEqual(code, 0, output)
        self.assertEqual([event["args"][0] for event in self.fixture.cargo_events()], ["check"])

    def test_nextest_selects_its_width_after_the_hold_release(self) -> None:
        self.hold()
        clock = self.fixture.root / "width-clock"
        _ = clock.write_text("0")
        invoke = VERIFY.parent.parent / "lint" / "invoke.sh"
        environment = {
            **self.fixture.environment, "BUILDLOG_CALLER": "verify", "BUILDLOG_CALL_ID": "width-call",
            "BUILDLOG_HOLD_WAIT_FILE": str(self.fixture.root / "width-wait"),
            "BUILDLOG_HOLD_WAITER_FILE": str(self.fixture.root / "width-waiter"),
            "TEST_WIDTH_CLOCK": str(clock),
        }
        script = '''set -euo pipefail
source "$1"
unset EPOCHSECONDS NEXTEST_TEST_THREADS
LINT_PHYSICAL_CORES=16
LINT_LOGICAL_CORES=32
date() { if [[ "${1:-}" == +%s ]]; then cat "$TEST_WIDTH_CLOCK"; else command date "$@"; fi; }
sweep_after_step() { :; }
run_nextest --package sample
'''
        process = subprocess.Popen(
            ["bash", "-c", script, "bash", str(invoke)], cwd=self.fixture.root, env=environment,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
        )
        self.addCleanup(self.stop_process, process)
        _ = self.wait_for_call(process)
        self.assertEqual(self.fixture.local_runs(), [])
        _ = clock.write_text("6000")
        grant_call(self.cycle_file)
        code, output = self.finish(process)
        self.assertEqual(code, 0, output)
        runs = self.fixture.local_runs()
        self.assertEqual(len(runs), 1)
        arguments = runs[0]["args"]
        self.assertEqual(arguments[arguments.index("--test-threads") + 1], "32")


class VerifyLintRerunTests(VerifyLintCacheFixture):
    def test_stopped_failure_publication_never_reuses_the_older_pass(self) -> None:
        cache = self.enable_cache()
        real_mv = shutil.which("mv")
        self.assertIsNotNone(real_mv)
        fault = self.root / "stop-failure-publication"
        self.write_executable(self.root / "bin" / "mv", f'''import os, signal, sys
from pathlib import Path
fault = Path({str(fault)!r})
if sys.argv[-1].endswith(".fail") and fault.exists():
    if fault.read_text() == "after":
        import subprocess
        subprocess.run([{real_mv!r}, *sys.argv[1:]], check=True)
    os.kill(os.getppid(), signal.SIGKILL)
    raise SystemExit(1)
os.execv({real_mv!r}, [{real_mv!r}, *sys.argv[1:]])
''')
        for stop_at in ("before", "after"):
            with self.subTest(stop_at=stop_at):
                _ = self.verify("lint", "sample", "--no-cache")
                self.assertEqual(len(list(cache.glob("*.pass"))), 1)
                _ = fault.write_text(stop_at)
                stopped = self.invoke_verify("lint", "sample", "--no-cache", cwd=self.root, taplo_status=7)
                self.assertLess(stopped.returncode, 0, stopped.stdout + stopped.stderr)
                fault.unlink()

                rerun = self.invoke_verify("lint", "sample", cwd=self.root, taplo_status=7)
                self.assertEqual(rerun.returncode, 7, rerun.stdout + rerun.stderr)
                self.assertNotIn("PASS (recorded)", rerun.stdout)
                if stop_at == "before":
                    self.assert_workspace_taplo_check()
                else:
                    self.assertIn("FAIL (recorded)", rerun.stdout)
                    self.assertEqual(self.taplo_calls(), [])
                replay = self.invoke_verify("lint", "sample", cwd=self.root, taplo_status=7)
                self.assertEqual(replay.returncode, 7, replay.stdout + replay.stderr)
                self.assertIn("FAIL (recorded)", replay.stdout)
                self.assertEqual(self.taplo_calls(), [])


if __name__ == "__main__":
    _ = unittest.main()
