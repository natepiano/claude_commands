"""Unit session names, restart records and rename journal completion."""

from __future__ import annotations


from collections.abc import Callable
import os
import signal
import subprocess
import sys
import unittest
from pathlib import Path
from typing import cast, final
from unittest.mock import patch
import rename_unit
from scripts.shutdown import stop_resume

sys.path.insert(0, str(Path(__file__).resolve().parent))


import fake_showrunner
import fake_unit_host

from scripts.production.rename_unit_test_support import UnitRenameFixture
from scripts.production import staff


@final
class UnitRenameSessionsTests(UnitRenameFixture):


    def test_restart_child_joins_parent_operation_and_clears_session_identity(self) -> None:
        child_entry = self.root / "restart_lock_probe.py"
        checkout = Path(__file__).resolve().parents[2]
        self.write(child_entry, f"""import os, sys
sys.path.insert(0, {str(checkout)!r})
from scripts.production import staff
assert 'CLAUDE_CODE_SESSION_ID' not in os.environ
with staff.operation():
    staff.ensure('showrunner', name='restart-child', runtime_id='9caee2d3-7095-5955-98a8-c402165f26d1')
""")
        run = cast(Callable[..., subprocess.CompletedProcess[str]], subprocess.run)

        def launch(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
            options["timeout"] = 3
            return run(command, **options)

        with (patch.dict(os.environ, {**self.env, "CLAUDE_CODE_SESSION_ID": "parent-session"}),
              staff.operation(), patch.object(rename_unit, "SHUTDOWN", child_entry),
              patch.object(subprocess, "run", side_effect=launch)):
            rename_unit.run_shutdown("restart", "--only", "fixture-session")
            self.assertIsNotNone(staff.by_path("restart-child"))
            self.assertTrue(staff.operation_fds())


    def finish_recordless_apply(self) -> None:
        run = cast(Callable[..., subprocess.CompletedProcess[str]], subprocess.run)
        launches: list[list[str]] = []
        def launch(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
            if len(command) > 1 and command[1] == str(rename_unit.SHUTDOWN):
                self.assertEqual(command[2:], ["restart", "--only", "5ad615f2-4e04-5121-a102-9d6b2e2a6eb3", "--here"])
                launches.append(command)
                _ = self.mark_live("gamma-unit", "5ad615f2-4e04-5121-a102-9d6b2e2a6eb3")
                return subprocess.CompletedProcess(command, 0, "", "")
            return run(command, **options)
        with patch.object(subprocess, "run", side_effect=launch):
            result = self.rename("apply")
        self.assert_ok(result)
        self.assertEqual(len(launches), 1)
        self.assertIs(self.journal()["done"], True)

    def recordless_stop(self) -> Callable[[str], stop_resume.Stopped]:
        end = self.mark_live("alpha-unit", "5ad615f2-4e04-5121-a102-9d6b2e2a6eb3")
        self.record_live_launch()
        def stop(identity: str) -> stop_resume.Stopped:
            self.assertEqual(identity, "5ad615f2-4e04-5121-a102-9d6b2e2a6eb3")
            self.assertEqual(self.journal()["account"], "claude4")
            end()
            return stop_resume.Stopped(identity, 1)
        return stop

    def test_live_recordless_rename_journals_its_account_and_resumes_the_same_conversation(self) -> None:
        with patch.object(self, "stop_session", side_effect=self.recordless_stop()):
            self.finish_recordless_apply()

    def kill_apply_after_save(self, step: int) -> None:
        """Kill only the fixture child, immediately after its real atomic journal write."""
        save = rename_unit._save  # pyright: ignore[reportPrivateUsage]
        def save_then_kill(path: Path, rename: rename_unit.UnitRename) -> None:
            save(path, rename)
            if rename.step == step:
                os.kill(os.getpid(), signal.SIGKILL)
        child = os.fork()
        if child == 0:
            try:
                with patch.object(rename_unit, "_save", side_effect=save_then_kill):
                    result = self.rename("apply")
                print(result.stdout + result.stderr, file=sys.__stderr__, flush=True)
            finally:
                os._exit(1)
        status = os.waitpid(child, 0)[1]
        self.assertTrue(os.WIFSIGNALED(status), f"apply exited instead of reaching journal step {step}: {status}")
        self.assertEqual(os.WTERMSIG(status), signal.SIGKILL)
        self.assertEqual(self.journal()["step"], step)

    def finish_after_killed_save(self, step: int) -> None:
        with patch.object(self, "stop_session", side_effect=self.recordless_stop()):
            self.kill_apply_after_save(step)
            self.assertEqual((self.journal()["account"], self.journal()["session_id"]), ("claude4", "5ad615f2-4e04-5121-a102-9d6b2e2a6eb3"))
            self.finish_recordless_apply()

    def test_apply_killed_after_its_first_journal_save_resumes_the_same_account_and_conversation(self) -> None:
        # The initial request is step 0; step 1 stops the director.
        self.finish_after_killed_save(0)

    def test_apply_killed_after_its_stop_step_resumes_the_same_account_and_conversation(self) -> None:
        self.finish_after_killed_save(1)


    def moved_unit_with_leftover_marks(self) -> None:
        _ = self.seed_record()
        self.assert_refused(self.rename("apply"), "gamma-unit is not running")
        fake_unit_host.write(self.env, [
            fake_unit_host.HostedUnit("old", "w3:p3Y", {"SHOWRUNNER_UNIT": "demo", "SHOWRUNNER_UNIT_ID": "alpha-unit"}),
            fake_unit_host.HostedUnit("moved", "w4:p1", {"SHOWRUNNER_UNIT": "demo", "SHOWRUNNER_UNIT_ID": "gamma-unit"}, label="gamma")])
        held = fake_showrunner.write_session(self.root / "state/sessions", "gamma", "5ad615f2-4e04-5121-a102-9d6b2e2a6eb3")
        self.addCleanup(held.close)
        fake_unit_host.place_process(self.env, os.getpid(), "w4:p1")
        fake_unit_host.process(self.env, 991030, 991000, ["python3", "-m", "unittest"],
                               {"HERDR_PANE_ID": "w3:p3Y", "SHOWRUNNER_UNIT": "demo", "SHOWRUNNER_UNIT_ID": "alpha-unit"})

    def test_verify_ignores_leftover_process_marks(self) -> None:
        self.moved_unit_with_leftover_marks()
        self.assert_ok(self.rename("verify"))

    def test_verify_names_the_live_holder_of_a_former_mark(self) -> None:
        self.moved_unit_with_leftover_marks()
        pid = fake_unit_host.live_pid(self)
        old = fake_showrunner.write_session(self.root / "state/sessions", "old",
                                           "d01f29c0-228f-4d37-b886-f7b752cc4118", pid)
        self.addCleanup(old.close)
        fake_unit_host.place_process(self.env, pid, "w3:p3Y")
        self.assert_refused(self.rename("verify"),
                            f"a session still carries the mark alpha-unit: pid {pid} (claude) in herdr:old:w3:p3Y")


    def test_verify_fails_while_the_old_session_name_is_live(self) -> None:
        _ = self.seed_record(name="builder-report")
        self.assert_refused(self.rename("apply"), "gamma-unit is not running")
        _ = self.mark_live("gamma-unit", "5ad615f2-4e04-5121-a102-9d6b2e2a6eb3", name="builder-report")
        result = self.rename("apply")
        self.assert_refused(result, "gamma-unit's session is named builder-report, not gamma")
        self.assertIn("is still labelled builder-report", result.stderr)

    def test_a_verify_that_passes_closes_the_journal(self) -> None:
        _ = self.seed_record(name="builder-report")
        self.assert_refused(self.rename("apply"), "gamma-unit is not running")
        end = self.mark_live("gamma-unit", "5ad615f2-4e04-5121-a102-9d6b2e2a6eb3", name="builder-report")
        self.assert_refused(self.rename("apply"), "gamma-unit's session is named builder-report, not gamma")
        self.assertEqual(self.journal()["done"], False)
        end()
        _ = self.mark_live("gamma-unit", "5ad615f2-4e04-5121-a102-9d6b2e2a6eb3")
        self.assert_ok(self.rename("verify"))
        self.assertEqual(self.journal()["done"], True)
        self.assert_ok(self.rename("plan", "gamma", "delta"))


if __name__ == "__main__":
    _ = unittest.main()
