"""Unit session names, restart records and rename journal completion."""

from __future__ import annotations

import json
from collections.abc import Callable
import os
import signal
import subprocess
import sys
import unittest
from pathlib import Path
from typing import TYPE_CHECKING, cast, final
from types import FrameType
from unittest.mock import patch
import rename_unit
from scripts.shutdown import stop_resume
if TYPE_CHECKING:
    from scripts.shutdown import pane_record
else:
    import pane_record

sys.path.insert(0, str(Path(__file__).resolve().parent))



import fake_showrunner
import fake_unit_host

from scripts.production.rename_unit_test_support import UnitRenameFixture
from scripts.production.rename_rerun_test_support import saved_fixture
from scripts.production import staff


@final
class UnitRenameSessionsTests(UnitRenameFixture):
    real_resume: Callable[..., stop_resume.ClaudeUp | stop_resume.ResumeFailed | stop_resume.SessionNotFound] = staticmethod(stop_resume.resume)

    def test_retained_resume_joins_parent_operation(self) -> None:
        resume = cast(Callable[..., stop_resume.ClaudeUp | stop_resume.ResumeFailed | stop_resume.SessionNotFound], stop_resume.resume)
        rename_pid = os.getpid()
        def lock_wait_timeout(number: int, frame: FrameType | None) -> None:
            raise AssertionError(f"rename waited on its held staff operation (signal {number}, {frame})")
        with staff.operation():
            held_fds = staff.operation_fds()
            self.assertTrue(held_fds)
            def in_process(identity: str, **options: object) -> stop_resume.ClaudeUp | stop_resume.ResumeFailed | stop_resume.SessionNotFound:
                self.assertEqual(os.getpid(), rename_pid)
                self.assertEqual(staff.operation_fds(), held_fds)
                with staff.operation():
                    self.assertIsNotNone(staff.by_path("demo/gamma"))
                return resume(identity, **options)
            previous_handler = signal.signal(signal.SIGALRM, lock_wait_timeout)
            previous_timer = signal.setitimer(signal.ITIMER_REAL, 10)
            try:
                with (patch.dict(self.env, {"STAFF_OPERATION_FD": str(held_fds[0])}),
                      patch.object(stop_resume, "resume", side_effect=in_process) as resumed):
                    self.assert_ok(self.rename("apply"))
            finally:
                _ = signal.setitimer(signal.ITIMER_REAL, *previous_timer)
                _ = signal.signal(signal.SIGALRM, previous_handler)
        resumed.assert_called_once()
        self.assertEqual(resumed.call_args.args, ("sid-alpha",))
        self.assertEqual(cast(stop_resume.RetainedPaneResume, resumed.call_args.kwargs["retained"]).pane_id, "w1:p1")
        self.assertEqual(len(self.director.starts), 1)

    def test_retained_launch_joins_parent_operation_and_clears_session_identity(self) -> None:
        self.env["CLAUDE_CODE_SESSION_ID"] = "parent-session"
        self.assert_ok(self.rename("apply"))
        command = list(self.director.starts[0])
        checkout = Path(__file__).resolve().parents[2]
        index = command.index(str(checkout / "scripts/accounts/accounts.py"))
        command[index:] = ["-c", "import os; print('CLAUDE_CODE_SESSION_ID' in os.environ)"]
        child = subprocess.run(command, env=self.env, capture_output=True, text=True, timeout=3, check=False)
        self.assertEqual(child.returncode, 0, child.stdout + child.stderr)
        self.assertEqual(child.stdout.strip(), "False")

    def test_retained_rename_resumes_each_plan_mode(self) -> None:
        with saved_fixture(self.root) as restore:
            for mode in ("docs/plans/alpha.md", "standby", "permanent"):
                with self.subTest(mode=mode):
                    restore()
                    self.director.reset()
                    self.write(self.doc, self.doc.read_text().replace("alpha-unit | docs/plans/alpha.md", "alpha-unit | " + mode))
                    _ = self.git("add", str(self.doc))
                    _ = self.git("commit", "--allow-empty", "-m", "unit plan mode")
                    with patch.object(stop_resume, "resume", wraps=stop_resume.resume) as resumed:
                        self.assert_ok(self.rename("apply"))
                    resumed.assert_called_once()
                    entry = pane_record.read("agents")["w1:p1"]
                    self.assertEqual((entry["environment"]["SHOWRUNNER_UNIT_ID"], entry["session_id"]),
                                     ("gamma-unit", "sid-alpha"))
                    self.assertEqual(cast(stop_resume.RetainedPaneResume, resumed.call_args.kwargs["retained"]).pane_id, "w1:p1")

    def finish_recordless_apply(self) -> None:
        resume = cast(Callable[..., stop_resume.ClaudeUp | stop_resume.ResumeFailed | stop_resume.SessionNotFound], stop_resume.resume)
        def launch(identity: str, **options: object) -> stop_resume.ClaudeUp | stop_resume.ResumeFailed | stop_resume.SessionNotFound:
            entry = pane_record.read("agents")["w1:p1"]
            self.assertEqual((Path(entry["config_dir"]).name, identity, entry["name"]),
                             (".claude4", "sid-alpha", "gamma"))
            self.assertEqual((self.journal()["account"], self.journal()["session_id"]), ("claude4", "sid-alpha"))
            self.assertEqual(options["retained"], stop_resume.RetainedPaneResume("w1:p1", "terminal-alpha", entry["config_dir"]))
            return resume(identity, **options)
        with patch.object(stop_resume, "resume", side_effect=launch) as launched:
            result = self.rename("apply")
        self.assert_ok(result)
        self.assertEqual(launched.call_count, 1)
        self.assertEqual(len(self.director.starts), 1)
        self.assertIs(self.journal()["done"], True)

    def recordless_stop(self) -> Callable[[str], stop_resume.Stopped]:
        end = self.mark_live("alpha-unit", "sid-alpha")
        self.record_live_launch()
        def stop(identity: str) -> stop_resume.Stopped:
            self.assertEqual(identity, "sid-alpha")
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
            self.assertEqual((self.journal()["account"], self.journal()["session_id"]), ("claude4", "sid-alpha"))
            self.finish_recordless_apply()

    def test_apply_killed_after_its_first_journal_save_resumes_the_same_account_and_conversation(self) -> None:
        # The initial request is step 0; step 1 stops the director.
        self.finish_after_killed_save(0)

    def test_apply_killed_after_its_stop_step_resumes_the_same_account_and_conversation(self) -> None:
        self.finish_after_killed_save(1)

    def test_old_step_eight_journal_without_account_or_launch_refuses_without_changes(self) -> None:
        with patch.object(self, "stop_session", side_effect=self.recordless_stop()):
            self.kill_apply_after_save(8)
        # The old tree removed this entry at step 7 and saved no account before stopping.
        self.assertTrue(pane_record.forget_unit("alpha-unit"))
        journal = self.state / "demo/rename_unit.json"
        fields = cast(dict[str, object], json.loads(journal.read_text()))
        fields["account"] = ""
        self.write(journal, json.dumps(fields))
        before = self.snapshot()
        with (patch.object(self, "stop_session") as stop,
              patch.object(os, "kill") as kill):
            self.assert_refused(self.rename("apply"),
                                "pane launch entry does not match the conversation")
        stop.assert_not_called()
        self.assertTrue(all(call.args[1] == 0 for call in kill.call_args_list),
                        "refused apply must send no process-changing signal")
        self.assertEqual(self.snapshot(), before)

    def moved_unit_with_leftover_marks(self) -> None:
        _ = self.seed_record()
        self.assert_ok(self.rename("apply"))
        fake_unit_host.write(self.env, [
            fake_unit_host.HostedUnit("agents", "w1:p1", self.director.pane.marks(), label="gamma"),
            fake_unit_host.HostedUnit("old", "w3:p3Y", {"SHOWRUNNER_UNIT": "demo", "SHOWRUNNER_UNIT_ID": "alpha-unit"})])
        self.director.pane.parent(self.director.pane.shell)
        self.director.publish()
        fake_unit_host.process(self.env, 991030, 991000, ["python3", "-m", "unittest"],
                               {"HERDR_PANE_ID": "w3:p3Y", "SHOWRUNNER_UNIT": "demo", "SHOWRUNNER_UNIT_ID": "alpha-unit"})

    def test_verify_ignores_leftover_process_marks(self) -> None:
        self.moved_unit_with_leftover_marks()
        self.assert_ok(self.rename("verify"))

    def test_verify_names_the_live_holder_of_a_former_mark(self) -> None:
        self.moved_unit_with_leftover_marks()
        pid = fake_unit_host.live_pid(self)
        old = fake_showrunner.write_session(self.root / "state/sessions", "old", "sid-old", pid)
        self.addCleanup(old.close)
        fake_unit_host.place_process(self.env, pid, "w3:p3Y")
        self.assert_refused(self.rename("verify"),
                            f"a session still carries the mark alpha-unit: pid {pid} (claude) in herdr:old:w3:p3Y")

    def test_a_stopped_unit_relaunches_from_its_rewritten_record(self) -> None:
        log = self.seed_record()
        result = self.rename("apply")
        self.assert_ok(result)
        self.assertEqual((self.journal()["step"], self.journal()["claimed"]), (11, False))
        self.assertEqual([json.loads(line) for line in log.read_text().splitlines()],
                         [["resume", "sid-alpha"]])
        saved = self.record()
        session = cast(dict[str, object], saved["session"])
        host = cast(dict[str, object], session["host"])
        new = self.home / "worktrees/gamma"
        self.assertEqual((host["unit"], host["session_name"], session["name"], session["cwd"]),
                         ("gamma-unit", "gamma", "gamma", f"{new}/sub"))
        self.assertEqual(cast(dict[str, object], host["plan"])["path"], f"{new}/docs/plans/alpha.md")
        self.assertIn(f"you are now gamma-unit, in worktree {new} on branch gamma, and your plan is at "
                      + f"{new}/docs/plans/alpha.md", cast(str, saved["renamed"]))
        # Each paused Codex seat resumes its own thread in the new worktree, and its resume prompt says so.
        seat = cast(list[dict[str, object]], saved["codex_seats"])[0]
        self.assertEqual(cast(list[str], seat["launcher_command"])[5], str(new))
        self.assertEqual((self.root / "runs/r1/impl.resume-prompt.md").read_text(),
                         "Continue the task you were on.\nWhile you were paused the showrunner renamed your unit: "
                         + f"your worktree is now {new} on branch gamma. {self.rust / 'claude-demo-alpha'} no "
                         + f"longer exists; wherever your instructions name it, use {new}.\n")

    def test_a_session_with_its_own_name_takes_the_new_name(self) -> None:
        _ = self.seed_record(name="builder-report")
        self.assert_ok(self.rename("apply"))
        self.assertEqual(cast(dict[str, object], self.record()["session"])["name"], "gamma")
        self.assertEqual(self.journal()["old_session"], "builder-report")

    def test_the_session_name_finds_the_unit_and_only_what_differs_changes(self) -> None:
        # The user names the unit by its session (builder-report); its id already is the new one.
        _ = self.seed_record(name="builder-report", host_session="demo-builder-report")
        planned = self.rename("plan", "builder-report", "alpha")
        self.assert_ok(planned)
        self.assertIn("Claude session builder-report -> alpha", planned.stdout)
        self.assertIn("branch demo-alpha -> alpha", planned.stdout)
        self.assertIn(str(self.home / "worktrees/alpha"), planned.stdout)
        self.assert_ok(self.rename("apply", "builder-report", "alpha"))
        session = cast(dict[str, object], self.record()["session"])
        self.assertEqual((session["name"], cast(dict[str, object], session["host"])["session_name"]),
                         ("alpha", "alpha"))
        repaired = self.home / "worktrees/alpha"
        self.assertEqual(self.git("branch", "--show-current", cwd=repaired), "alpha")
        self.assertIn(f"`{repaired}` | alpha |", self.doc.read_text())
        # The relaunched unit keeps its mark alpha-unit, now under the new session name.
        _ = self.mark_live("alpha-unit", "sid-alpha")
        self.assert_ok(self.rename("apply", "builder-report", "alpha"))
        self.assertEqual(self.journal()["done"], True)

    def resume_with_old_session_name(self, identity: str, **options: object) -> stop_resume.ClaudeUp | stop_resume.ResumeFailed | stop_resume.SessionNotFound:
        result = self.real_resume(identity, **options)
        _ = self.mark_live("gamma-unit", "sid-alpha", name="builder-report")
        return result

    def test_verify_fails_while_the_old_session_name_is_live(self) -> None:
        _ = self.seed_record(name="builder-report")
        self.real_resume = cast(Callable[..., stop_resume.ClaudeUp | stop_resume.ResumeFailed | stop_resume.SessionNotFound], stop_resume.resume)
        with patch.object(stop_resume, "resume", side_effect=self.resume_with_old_session_name):
            result = self.rename("apply")
        self.assert_refused(result, "gamma-unit's session is named builder-report, not gamma")
        self.assertIn("is still labelled builder-report", result.stderr)

    def test_a_verify_that_passes_closes_the_journal(self) -> None:
        _ = self.seed_record(name="builder-report")
        self.real_resume = cast(Callable[..., stop_resume.ClaudeUp | stop_resume.ResumeFailed | stop_resume.SessionNotFound], stop_resume.resume)
        with patch.object(stop_resume, "resume", side_effect=self.resume_with_old_session_name):
            self.assert_refused(self.rename("apply"), "gamma-unit's session is named builder-report, not gamma")
        self.assert_refused(self.rename("verify"), "gamma-unit's session is named builder-report, not gamma")
        self.assertEqual(self.journal()["done"], False)
        _ = self.mark_live("gamma-unit", "sid-alpha")
        self.assert_ok(self.rename("verify"))
        self.assertEqual(self.journal()["done"], True)
        self.assert_ok(self.rename("plan", "gamma", "delta"))


if __name__ == "__main__":
    _ = unittest.main()
