"""Production rename: units behavior."""

from __future__ import annotations

import json
import re
import unittest
from typing import cast, final

from scripts.production import rename_test_support as support
from scripts.production.rename_test_support import INTERRUPT_UNIT_GIT, NEW, OLD


@final
class ProductionRenameTests(support.ProductionRenameFixture):
    def test_unit_repoints_stopped_restore_and_prints_its_relaunch(self) -> None:
        self.prepare_unit_moves()
        path = self.seed_shutdown_record()
        self.assert_success(self.run_rename("apply"))
        result = self.run_command("unit", NEW, "alpha-unit")
        self.assert_success(result)
        self.assert_unit_moved("alpha-unit")
        saved = cast(dict[str, object], json.loads(path.read_text()))
        session = cast(dict[str, object], saved["session"])
        host = cast(dict[str, object], session["host"])
        new = self.unit_worktree(NEW, "alpha-unit")
        self.assertEqual((session["session_id"], session["name"], host["unit"]),
                         ("sid-alpha", "alpha", "alpha-unit"))
        self.assertEqual(session["cwd"], f"{new}/sub")
        self.assertEqual(cast(list[dict[str, object]], saved["workers"])[0]["cwd"], f"{new}/worker")
        self.assertEqual(cast(dict[str, object], host["plan"])["path"], f"{new}/docs/plans/alpha-unit.md")
        self.assertEqual((host["session_name"], host["production"], host["doc"]),
                         ("new-alpha-unit", NEW, str(self.new_doc)))
        worker = cast(list[dict[str, object]], saved["codex_seats"])[0]
        self.assertEqual(worker["launcher_command"],
                         ["bash", "/scripts/direct/implement.sh", str(new), f"{new}/prompt.md"])
        note = (self.root / "runs/r1/impl.resume-prompt.md").read_text()
        self.assertTrue(note.startswith("Continue the task.\n"))
        self.assertIn(f"your worktree is now {new} on branch new-alpha-unit", note)
        self.assertIn("shutdown.py restart --only sid-alpha", result.stdout)

    def test_unit_refuses_an_unstopped_restore_before_any_change(self) -> None:
        self.prepare_unit_moves()
        _ = self.seed_shutdown_record(progress="waiting")
        self.assert_success(self.run_rename("apply"))
        before = self.snapshot()
        result = self.run_command("unit", NEW, "alpha-unit")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("shutdown record reads waiting", result.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_unit_without_restore_moves_and_reports_no_relaunch(self) -> None:
        self.prepare_unit_moves()
        self.assert_success(self.run_rename("apply"))
        result = self.run_command("unit", NEW, "alpha-unit")
        self.assert_success(result)
        self.assert_unit_moved("alpha-unit")
        self.assertIn("alpha-unit has no shutdown record to relaunch from", result.stdout)
        self.assertNotIn("shutdown.py restart", result.stdout)

    def test_unit_resume_after_record_step_does_not_repoint_again(self) -> None:
        self.prepare_unit_moves()
        path = self.seed_shutdown_record()
        self.assert_success(self.run_rename("apply"))
        failed = self.run_command("unit", NEW, "alpha-unit", wrapper=INTERRUPT_UNIT_GIT,
                                  RENAME_TEST_REFUSE="unit-doc")
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("simulated unit-doc failure", failed.stderr)
        moves = cast(dict[str, dict[str, object]], self.journal()["unit_moves"])
        self.assertIs(moves["alpha-unit"]["restore_repointed"], True)
        before = path.read_bytes()
        prompt = self.root / "runs/r1/impl.resume-prompt.md"
        note = prompt.read_bytes()
        wrapper = """import runpy, sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]).parent))
import rename_unit
def refuse(*args):
    raise AssertionError("restore repeated")
rename_unit.repoint_shutdown_worktree = refuse
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
"""
        result = self.run_command("unit", NEW, "alpha-unit", wrapper=wrapper)
        self.assert_success(result)
        self.assert_unit_moved("alpha-unit")
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(prompt.read_bytes(), note)
        self.assertIn("shutdown.py restart --only sid-alpha", result.stdout)

    def test_a_journal_that_counted_seven_steps_finishes_with_the_new_steps_only(self) -> None:
        self.prepare_unit_moves()
        self.marked_unit("alpha-unit")
        self.assert_success(self.run_rename("apply"))
        journal = self.journal()
        del journal["completed"]
        journal["step"] = 7
        _ = (self.state / "new/rename.json").write_text(json.dumps(journal))
        branch_tip = self.git("rev-parse", NEW)
        refused = self.run_command("unit", NEW, "alpha-unit")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("apply must complete _rename_unit_workers", refused.stdout + refused.stderr)
        self.assert_success(self.run_rename("apply"))
        completed = cast(list[str], self.journal()["completed"])
        self.assertEqual(completed[:8], [
            "_rename_doc", "_rewrite_plans", "_rename_timer", "_rename_showrunner_state", "_rename_stall_watch",
            "legacy_review_pool_retired", "_rename_branch", "_rename_unit_workers"])
        self.assertEqual(len(completed), 9)
        self.assertEqual(self.git("rev-parse", NEW), branch_tip)
        self.assert_success(self.run_command("unit", NEW, "alpha-unit"))

    def test_unit_moves_punctuated_transcript_paths_and_default_and_named_account_trust(self) -> None:
        self.prepare_unit_moves()
        configs = [self.home / ".claude", self.home / ".claude-other"]
        for directory in configs:
            _ = self.seed_unit_config(directory, "alpha-unit")
        self.marked_unit("alpha-unit")
        self.assert_success(self.run_rename("apply"))
        base = self.git("rev-parse", NEW)
        moved = self.run_command("unit", NEW, "alpha-unit")
        self.assert_success(moved)
        self.assertIn("herdr mark", moved.stdout)
        self.assertIn("next launch", moved.stdout)
        self.assert_unit_moved("alpha-unit")
        old, new = self.unit_worktree(OLD, "alpha-unit"), self.unit_worktree(NEW, "alpha-unit")
        for directory in configs:
            self.assertFalse((directory / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(old))).exists())
            self.assertEqual((directory / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(new)) / "session.jsonl").read_text(),
                             "historical transcript for old\n")
            config = self.home / ".claude.json" if directory == self.home / ".claude" else directory / ".claude.json"
            fields = cast(dict[str, object], json.loads(config.read_text()))
            projects = cast(dict[str, object], fields["projects"])
            self.assertEqual(projects[str(new)], projects[str(old)])
            self.assertEqual(projects[str(new)], {"hasTrustDialogAccepted": True, "allowedTools": ["Read"]})
            self.assertEqual(projects[str(self.root / "unrelated")], {"hasTrustDialogAccepted": False})
            self.assertEqual(fields["theme"], "dark")
        self.assertEqual(self.git("rev-parse", "HEAD^"), base)
        self.assertEqual(self.git("log", "-1", "--format=%s"), "chore(new): rename unit alpha-unit from old")
        self.assertEqual(self.git("diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"),
                         "docs/plans/new-production.md")
        self.assertEqual(self.git("ls-remote", "origin", "refs/heads/new"),
                         f"{self.git('rev-parse', NEW)}\trefs/heads/new")
        self.assertTrue(self.unit_worktree(OLD, "beta-unit").exists())
        marks = (self.root / "proc/991001/environ").read_text().split("\0")
        self.assertIn(f"SHOWRUNNER_UNIT={OLD}", marks)
        self.assertIn("SHOWRUNNER_UNIT_ID=alpha-unit", marks)
        self.assertIs(self.journal()["done"], False)
        self.assertFalse((self.home / ".claude/.claude.json").exists())

    def test_unit_refuses_when_herdr_discovery_is_unreadable(self) -> None:
        self.prepare_unit_moves()
        self.marked_unit("alpha-unit")
        self.live_unit()
        self.assert_success(self.run_rename("apply"))
        before = self.snapshot()
        refused = self.run_command("unit", NEW, "alpha-unit", HERDR_BIN=str(self.root / "missing-herdr"))
        self.assertNotEqual(refused.returncode, 0, refused.stdout + refused.stderr)
        self.assertIn("cannot tell", refused.stderr)
        self.assertIn("herdr", refused.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_unit_resume_uses_current_merge_tip_after_an_unrelated_commit(self) -> None:
        self.prepare_unit_moves()
        self.assert_success(self.run_rename("apply"))
        failed = self.run_command("unit", NEW, "alpha-unit", wrapper=INTERRUPT_UNIT_GIT,
                                  RENAME_TEST_REFUSE="unit-doc")
        self.assertNotEqual(failed.returncode, 0, failed.stdout + failed.stderr)
        self.assertIn("simulated unit-doc failure", failed.stderr)
        moves = cast(dict[str, dict[str, object]], self.journal()["unit_moves"])
        self.assertEqual(moves["alpha-unit"]["step"], 3)
        self.write(self.checkout / "README.md", "Unrelated progress on merge branch.\n")
        _ = self.git("add", "README.md")
        _ = self.git("commit", "-m", "Merge unrelated progress")
        base = self.git("rev-parse", "HEAD")
        self.assert_success(self.run_command("unit", NEW, "alpha-unit"))
        self.assert_unit_moved("alpha-unit")
        self.assertEqual(self.git("rev-parse", "HEAD^"), base)

    def test_unit_resume_recognizes_a_committed_row_after_later_merge_progress(self) -> None:
        self.prepare_unit_moves()
        self.assert_success(self.run_rename("apply"))
        failed = self.run_command("unit", NEW, "alpha-unit", wrapper=INTERRUPT_UNIT_GIT,
                                  RENAME_TEST_REFUSE="merge-push")
        self.assertNotEqual(failed.returncode, 0, failed.stdout + failed.stderr)
        self.assertIn("simulated merge-push failure", failed.stderr)
        self.write(self.checkout / "README.md", "More merge progress after row commit.\n")
        _ = self.git("add", "README.md")
        _ = self.git("commit", "-m", "Merge after unit row commit")
        tip = self.git("rev-parse", "HEAD")
        self.assert_success(self.run_command("unit", NEW, "alpha-unit"))
        self.assert_unit_moved("alpha-unit")
        self.assertEqual(self.git("rev-parse", "HEAD"), tip)

    def test_unit_retry_refuses_an_advanced_old_branch_before_changing_the_worktree(self) -> None:
        self.prepare_unit_moves()
        self.assert_success(self.run_rename("apply"))
        failed = self.run_command("unit", NEW, "alpha-unit", wrapper=INTERRUPT_UNIT_GIT,
                                  RENAME_TEST_REFUSE="unit-push")
        self.assertNotEqual(failed.returncode, 0, failed.stdout + failed.stderr)
        old = self.unit_worktree(OLD, "alpha-unit")
        self.write(old / "README.md", "Unit made progress after the interruption.\n")
        _ = self.git("-C", str(old), "add", "README.md")
        _ = self.git("-C", str(old), "commit", "-m", "Unit progress")
        before = self.snapshot()
        refused = self.run_command("unit", NEW, "alpha-unit")
        self.assertNotEqual(refused.returncode, 0, refused.stdout + refused.stderr)
        self.assertIn("old-alpha-unit moved", refused.stderr)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.git("-C", str(old), "branch", "--show-current"), "old-alpha-unit")

    def test_unit_resume_refuses_a_missing_branch_column_without_a_traceback(self) -> None:
        self.prepare_unit_moves()
        self.assert_success(self.run_rename("apply"))
        failed = self.run_command("unit", NEW, "alpha-unit", wrapper=INTERRUPT_UNIT_GIT,
                                  RENAME_TEST_REFUSE="unit-doc")
        self.assertNotEqual(failed.returncode, 0, failed.stdout + failed.stderr)
        lines = self.new_doc.read_text().splitlines(keepends=True)
        self.write(self.new_doc, "".join(line.rsplit("|", 2)[0] + "|\n" if line.startswith("|") else line
                                        for line in lines))
        _ = self.git("add", str(self.new_doc))
        _ = self.git("commit", "-m", "Remove Branch column")
        refused = self.run_command("unit", NEW, "alpha-unit")
        self.assertNotEqual(refused.returncode, 0, refused.stdout + refused.stderr)
        self.assertIn("Units table lacks Branch column", refused.stderr)
        self.assertNotIn("Traceback", refused.stderr)

    def test_unit_refuses_live_and_unknown_claude_then_moves_when_stopped(self) -> None:
        self.prepare_unit_moves()
        self.marked_unit("alpha-unit")
        self.live_unit()
        self.assert_success(self.run_rename("apply"))
        before = self.snapshot()
        live = self.run_command("unit", NEW, "alpha-unit")
        self.assertNotEqual(live.returncode, 0, live.stdout + live.stderr)
        self.assertIn("live Claude", live.stderr)
        self.assertEqual(self.snapshot(), before)
        (self.root / "state/sessions/live.json").unlink()
        self.write(self.root / "state/sessions/broken.json", "{")
        before = self.snapshot()
        unknown = self.run_command("unit", NEW, "alpha-unit")
        self.assertNotEqual(unknown.returncode, 0, unknown.stdout + unknown.stderr)
        self.assertIn("cannot tell", unknown.stderr)
        self.assertEqual(self.snapshot(), before)
        (self.root / "state/sessions/broken.json").unlink()
        self.assert_success(self.run_command("unit", NEW, "alpha-unit"))
        self.assert_unit_moved("alpha-unit")

    def test_unit_and_finish_wait_for_apply_to_complete(self) -> None:
        failed = self.run_rename("apply", fail_state=True)
        self.assertNotEqual(failed.returncode, 0, failed.stdout + failed.stderr)
        self.assertEqual(self.completed_steps(), 3)
        for arguments in (("unit", NEW, "alpha-unit"), ("finish", NEW)):
            with self.subTest(arguments=arguments):
                before = self.snapshot()
                refused = self.run_command(*arguments)
                self.assertNotEqual(refused.returncode, 0, refused.stdout + refused.stderr)
                self.assertIn("apply must complete _rename_showrunner_state, ", refused.stderr)
                self.assertEqual(self.snapshot(), before)

    def test_unit_retry_repairs_a_worktree_already_moved_before_interruption(self) -> None:
        self.prepare_unit_moves()
        self.assert_success(self.run_rename("apply"))
        base = self.git("rev-parse", NEW)
        failed = self.run_command("unit", NEW, "alpha-unit", wrapper=INTERRUPT_UNIT_GIT,
                                  RENAME_TEST_REFUSE="repair")
        self.assertNotEqual(failed.returncode, 0, failed.stdout + failed.stderr)
        self.assertIn("simulated repair failure", failed.stderr)
        self.assertFalse(self.unit_worktree(OLD, "alpha-unit").exists())
        self.assertTrue(self.unit_worktree(NEW, "alpha-unit").exists())
        self.assertEqual(self.git("rev-parse", "old-alpha-unit"), self.old_tip)
        self.assertIn("refs/heads/old-alpha-unit", self.git("ls-remote", "origin", "refs/heads/old-alpha-unit"))
        self.assertIn("refs/heads/new-alpha-unit", self.git("ls-remote", "origin", "refs/heads/new-alpha-unit"))
        self.assert_success(self.run_command("unit", NEW, "alpha-unit"))
        self.assert_unit_moved("alpha-unit")
        self.assertEqual(self.git("rev-list", "--count", f"{base}..HEAD"), "1")
        tip = self.git("rev-parse", NEW)
        self.assert_success(self.run_command("unit", NEW, "alpha-unit"))
        self.assertEqual(self.git("rev-parse", NEW), tip)

    def test_unit_push_refusal_keeps_the_old_branch_and_worktree(self) -> None:
        self.prepare_unit_moves()
        self.assert_success(self.run_rename("apply"))
        failed = self.run_command("unit", NEW, "alpha-unit", wrapper=INTERRUPT_UNIT_GIT,
                                  RENAME_TEST_REFUSE="unit-push")
        self.assertNotEqual(failed.returncode, 0, failed.stdout + failed.stderr)
        self.assertIn("simulated unit-push failure", failed.stderr)
        self.assertTrue(self.unit_worktree(OLD, "alpha-unit").exists())
        self.assertFalse(self.unit_worktree(NEW, "alpha-unit").exists())
        self.assertEqual(self.git("rev-parse", "old-alpha-unit"), self.old_tip)
        self.assertEqual(self.git("ls-remote", "origin", "refs/heads/old-alpha-unit"),
                         f"{self.old_tip}\trefs/heads/old-alpha-unit")
        self.assertEqual(self.git("ls-remote", "origin", "refs/heads/new-alpha-unit"), "")
        self.assert_success(self.run_command("unit", NEW, "alpha-unit"))
        self.assert_unit_moved("alpha-unit")


if __name__ == "__main__":
    _ = unittest.main()
