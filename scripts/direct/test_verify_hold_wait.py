"""Real verification hold waits: wait."""

from __future__ import annotations

from scripts.production.staff_test_support import fixture_runtime_id

import json
import unittest
from pathlib import Path
from typing import cast

from scripts.direct.verify_mac_offload_test_support import CARGO_STUB
from scripts.direct.verify_hold_wait_test_support import (
    VerifyHoldWaitFixture,
    WaitObservation,
    grant_call,
    read_cycle_file as read_cycle_file,
    release_registered_call as release_registered_call,
)


class VerifyHoldWaitTests(VerifyHoldWaitFixture):
    def test_step_failure_with_hold_refusal_text_stays_a_failure(self) -> None:
        hold_scripts = Path(__file__).resolve().parents[1] / "build_hold"
        failing_step = f'''if arguments[:2] == ["nextest", "run"]:
    failed = Path(os.environ["TEST_RUNNER_STATE"]) / "failed-once"
    if not failed.exists():
        failed.touch()
        sys.path.insert(0, {str(hold_scripts)!r})
        import build_hold
        from datetime import datetime
        build_hold.start_hold(
            Path(os.environ["BUILD_HOLD_DIR"]), build_hold.HolderIdentity("hana", "hana"),
            "render checks", build_hold.NoReleaseEta(), datetime.now().astimezone(),
        )
        print("natedev builds and tests are held by hana for render checks; this run was stopped. "
              "You get a notice at the release; run it again then.", file=sys.stderr)
        raise SystemExit(9)
'''
        self.fixture.write_executable(
            self.fixture.bin_directory / "cargo",
            CARGO_STUB.replace('if arguments[:2] == ["nextest", "run"]:\n', failing_step),
        )
        with release_registered_call(self.cycle_file):
            result = self.fixture.verify("test", "sample", "--local", "step failure", "--filter", "smoke")
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 9, output)
        self.assertIn("this run was stopped", output)
        self.assertNotIn("hold stopped this step", output)
        self.assertNotIn("; waiting", output)
        self.assertEqual(len(self.fixture.local_runs()), 1)

    def test_unheld_local_call_needs_no_release(self) -> None:
        result = self.run_unblocked_step("test")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("natedev is held", result.stdout + result.stderr)
        self.assertEqual(len(self.fixture.local_runs()), 1)
        self.assertFalse(self.cycle_file.exists())

    def test_call_with_an_empty_holder_directory_starts_no_hold_check(self) -> None:
        Path(self.fixture.environment["BUILD_HOLD_DIR"]).mkdir()
        starts = self.count_python_starts()
        for arguments in (("check", "sample"), ("test", "sample", "--local", "local admission test", "--filter", "smoke")):
            with self.subTest(arguments=arguments):
                starts.unlink(missing_ok=True)
                result = self.run_unblocked_step(arguments[0])
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.hold_commands(starts), [])
                self.assertEqual(self.recorded_hold_wait(), 0)

    def test_held_call_starts_the_same_hold_checks_as_before(self) -> None:
        self.hold()
        starts = self.count_python_starts()
        observation = self.observe_waits()
        process = self.start("test", "sample", "--local", "local admission test", "--filter", "smoke")
        _ = self.wait_for_call(process)
        self.assertEqual(self.fixture.local_runs(), [])
        grant_call(self.cycle_file)
        code, output = self.finish(process)
        self.assertEqual(code, 0, output)
        self.assertIn("held by hana for render checks; waiting", output)
        self.assertEqual(output.count("; waiting (tests that can run on the Mac"), 1)
        self.assertEqual(len(self.fixture.local_runs()), 1)
        observed = cast(WaitObservation, json.loads(observation.read_text()))
        self.assertGreater(float(observed["total"]), 0)
        self.assertAlmostEqual(float(observed["total"]), sum(float(value) for value in observed["waits"]), places=5)
        self.assertAlmostEqual(self.recorded_hold_wait(), float(observed["total"]), places=5)
        commands = self.hold_commands(starts)
        self.assertTrue(any(command[1] == "wait-local" for command in commands))
        self.assertFalse(any(command[1] in {"mark", "check-admission", "finish-local"} for command in commands))

    def test_held_call_waits_before_compiling_then_runs_on_release(self) -> None:
        history = self.record_phase()
        self.hold()
        self.fixture.environment["BUILD_TARGETS_POLL_S"] = "3600"
        process = self.start("test", "sample", "--local", "local admission test", "--filter", "smoke")
        _ = self.wait_for_call(process)
        self.assertEqual(self.fixture.local_runs(), [])
        grant_call(self.cycle_file)
        code, output = self.finish(process)
        self.assertEqual(code, 0, output)
        self.assertIn("held by hana for render checks; waiting", output)
        self.assertEqual(len(self.fixture.local_runs()), 1)
        self.assertFalse((Path(self.fixture.environment["BUILD_HOLD_DIR"]) / "hana").exists())
        rows = [cast(dict[str, object], json.loads(line)) for line in history.read_text().splitlines()]
        started, = [row for row in rows if row["event_type"] == "wait_started"]
        finished, = [row for row in rows if row["event_type"] == "wait_finished"]
        self.assertEqual((started["wait_kind"], started["wait_reason"], started["owner_session"]),
                         ("WaitingForMemoryOrSlot", "build hold", fixture_runtime_id("thread-impl")))
        self.assertEqual(started["wait_id"], finished["wait_id"])
        self.assertEqual(cast(dict[str, object], finished["wait_end"])["state"], "Finished")

    def test_unreadable_hold_state_starts_no_local_step(self) -> None:
        _ = Path(self.fixture.environment["BUILD_HOLD_DIR"]).write_text("not a directory")
        result = self.run_unblocked_step("test")
        self.assertEqual(result.returncode, 12, result.stdout + result.stderr)
        self.assertIn("hold state cannot be read", result.stdout + result.stderr)
        self.assertEqual(self.fixture.local_runs(), [])


if __name__ == "__main__":
    _ = unittest.main()
