"""Real process boundaries for unit stops, relaunches and merge locks."""

from __future__ import annotations

import fcntl
import json
import subprocess
import sys
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import cast, final

sys.path.insert(0, str(Path(__file__).resolve().parent))



from scripts.production.rename_unit_test_support import UnitRenameFixture, SLUG, TEST_HANG_GUARD_SECS


@final
class UnitRenameWaitsTests(UnitRenameFixture):

    def test_apply_waits_while_the_unit_is_stopping(self) -> None:
        log = self.seed_record(progress="waiting")
        result = self.rename("apply")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("alpha-unit is still waiting after 0 s; rerun apply once it has stopped", result.stderr)
        self.assertEqual(self.journal()["step"], 0)
        # A stop that waits for the unit to go idle is forced at once.
        self.assertEqual([json.loads(line) for line in log.read_text().splitlines()],
                         [["now", "--only", "sid-alpha", "--here"]])


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

    def test_one_apply_stops_the_unit_waits_and_relaunches_it_with_no_rerun(self) -> None:
        log = self.seed_record()
        record = self.home / ".local/state/shutdown/owner@example.com/sessions/sid-alpha.json"
        stopped = cast(dict[str, object], json.loads(record.read_text()))
        record.unlink()
        # Write the stopping record before down returns. Otherwise apply can print the
        # same stopping event for a missing record, before it has read our state.
        stopping = {**stopped, "progress": {"kind": "stopping", "at": "2026-10-09T21:49:30+00:00"}}
        stub = Path(self.env["RENAME_UNIT_SHUTDOWN"])
        self.write(stub, stub.read_text() + "from pathlib import Path\n"
                   + "if sys.argv[1] == 'down':\n"
                   + f"    _ = Path({str(record)!r}).write_text({json.dumps(stopping)!r})\n"
                   + "if sys.argv[1] == 'restart':\n"
                   + "    with open(os.environ['RENAME_UNIT_SHUTDOWN_LOG'], 'a') as log:\n"
                   + "        _ = log.write(json.dumps(['restart exiting']) + '\\n')\n")
        end = self.mark_live("alpha-unit", "sid-alpha")
        self.env.update({"RENAME_UNIT_STOP_WAIT": str(TEST_HANG_GUARD_SECS),
                         "RENAME_UNIT_LIVE_WAIT": str(TEST_HANG_GUARD_SECS)})
        process = self.start_apply()
        calls: Callable[[], list[list[str]]] = lambda: ([cast(list[str], json.loads(line)) for line in
                                                         log.read_text().splitlines()] if log.exists() else [])
        waited = self.wait_for_output(process, "stop: waiting — alpha-unit is stopping")
        end()
        self.assertIsNone(process.poll())
        # Publish stopped atomically so apply cannot read a partially written record.
        next_record = record.with_suffix(".next")
        self.write(next_record, json.dumps(stopped))
        _ = next_record.replace(record)
        # The restart stand-in logs its exit after recording the command.
        self.wait_for(lambda: ["restart exiting"] in calls())
        _ = self.mark_live("gamma-unit", "sid-alpha")
        stdout, stderr = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        stdout = waited + stdout
        self.assertEqual(process.returncode, 0, stdout + stderr)
        self.assertIn("stop: waiting — alpha-unit is stopping", stdout)
        self.assertIn("alpha-unit is now gamma-unit, relaunched in", stdout)
        self.assertEqual((self.journal()["done"], self.journal()["claimed"]), (True, False))
        self.assertEqual(calls(), [["down", "--only", "sid-alpha", "--here"],
                                   ["restart", "--only", "sid-alpha", "--here"], ["restart exiting"]])

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
        planned = self.rename("plan")
        self.assert_ok(planned)
        self.assertIn("1. pause alpha-unit and its Codex workers through the shutdown path", planned.stdout)
        applied = self.rename("apply")
        self.assertEqual(applied.returncode, 2, applied.stdout + applied.stderr)
        self.assertIn("alpha-unit is still stopping after 0 s; rerun apply once it has stopped", applied.stderr)
        self.assertEqual([json.loads(line) for line in log.read_text().splitlines()],
                         [["down", "--only", "sid-alpha", "--here"]])
        self.assertEqual(self.journal()["step"], 0)

    def test_a_running_new_id_is_refused(self) -> None:
        _ = self.mark_live("gamma-unit", "sid-gamma")
        self.assert_refused(self.rename("plan"), "gamma-unit already runs in session demo-gamma-unit")


if __name__ == "__main__":
    _ = unittest.main()
