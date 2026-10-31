"""Real process boundaries for unit stops, relaunches and merge locks."""

from __future__ import annotations

import fcntl
import subprocess
import sys
import unittest
from unittest.mock import patch
from pathlib import Path
from typing import final

sys.path.insert(0, str(Path(__file__).resolve().parent))



from scripts.production.rename_unit_test_support import UnitRenameFixture, SLUG, TEST_HANG_GUARD_SECS
from scripts.shutdown import stop_resume


@final
class UnitRenameWaitsTests(UnitRenameFixture):



    @unittest.skipIf(sys.platform == "darwin", "the Mac reads working directories with lsof")
    def test_verify_fails_while_a_process_works_in_the_old_worktree(self) -> None:
        _ = self.seed_record()
        self.assert_refused(self.rename("apply"), "gamma-unit is not running")
        _ = self.mark_live("gamma-unit", "sid-alpha")
        old = self.rust / "claude-demo-alpha"
        old.mkdir()
        child = subprocess.Popen(
            [sys.executable, "-c", "import sys; print('ready', flush=True); sys.stdin.read()"],
            cwd=old, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(child.wait, timeout=TEST_HANG_GUARD_SECS)
        self.addCleanup(child.kill)
        assert child.stdin is not None
        assert child.stdout is not None
        self.addCleanup(child.stdin.close)
        self.addCleanup(child.stdout.close)
        # Python must finish startup before its cwd disappears; otherwise getpath can exit first.
        self.assertEqual(self.wait_for_output(child, "ready"), "ready\n")
        # The folder is gone, but the process still has it as its working directory.
        old.rmdir()
        result = self.rename("verify")
        self.assert_refused(result, f"processes {child.pid} still work in {old}")
        child.kill()
        _ = child.wait(timeout=TEST_HANG_GUARD_SECS)
        self.assert_ok(self.rename("verify"))


    def test_apply_waits_for_a_merge_gate_before_it_writes_the_merge_branch(self) -> None:
        _ = self.seed_record()
        common = subprocess.run(["git", "-C", str(self.checkout), "rev-parse", "--path-format=absolute",
                                 "--git-common-dir"], capture_output=True, text=True, check=True).stdout.strip()
        with (Path(common) / f"showrunner-merge-{SLUG}.lock").open("a") as gate:
            fcntl.flock(gate, fcntl.LOCK_EX)
            process = self.start_apply()
            # Hold the gate until apply says it waits: a fixed sleep let a slow start under
            # load reach the lock after it was released, so it never waited.
            assert process.stdout is not None
            waited = self.wait_for_output(process, "merge lock: waiting —")
            # The gate holds the merge branch: the rename has not even checked the checkout yet.
            self.assertIsNone(process.poll())
            self.assertFalse((self.state / SLUG / "rename_unit.json").exists())
            fcntl.flock(gate, fcntl.LOCK_UN)
        stdout, stderr = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertIn("merge lock: waiting — a merge gate or a unit rename is writing demo's merge branch",
                      waited + stdout)
        # The fake restart starts nothing; every step before verify ran once the gate was done.
        self.assertIn("gamma-unit is not running", stderr)
        self.assertEqual(self.journal()["step"], 10)

    def test_a_running_unit_plans_and_apply_stops_it_through_the_shutdown_path(self) -> None:
        log = self.seed_record()
        (self.home / ".local/state/shutdown/owner@example.com/sessions/sid-alpha.json").unlink()
        _ = self.mark_live("alpha-unit", "sid-alpha")
        self.record_live_launch()
        planned = self.rename("plan")
        self.assert_ok(planned)
        self.assertIn("1. pause alpha-unit and its Codex workers through the shutdown path", planned.stdout)
        with patch.object(self, "stop_session", return_value=stop_resume.StopFailed("sid-alpha", "fixture stop refused")) as stop:
            applied = self.rename("apply")
        self.assert_refused(applied, "stop refused: fixture stop refused")
        stop.assert_called_once_with("sid-alpha")
        self.assertEqual(log.read_text(), "")
        self.assertEqual(self.journal()["step"], 0)

    def test_a_running_new_id_is_refused(self) -> None:
        _ = self.mark_live("gamma-unit", "sid-gamma")
        self.assert_refused(self.rename("plan"), "gamma-unit already runs in session demo-gamma-unit")


if __name__ == "__main__":
    _ = unittest.main()
