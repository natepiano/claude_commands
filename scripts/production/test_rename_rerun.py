"""Production rename recovery at side effects whose progress has not been saved."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest
from typing import cast, final
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scripts.production.rename_test_support import ProductionRenameFixture
from scripts.production.rename_rerun_test_support import final_files, saved_fixture
import unit_move


@final
class ProductionRenameRerunTests(ProductionRenameFixture):
    def outcome(self) -> tuple[dict[str, bytes], str, str, str]:
        return (final_files(self.root), self.git("show-ref"), self.git("status", "--porcelain"),
                self.git("--git-dir", str(self.origin), "show-ref"))

    def test_resume_after_timer_move_before_progress_save(self) -> None:
        self.env.update(GIT_AUTHOR_DATE="2026-10-29T12:00:00Z", GIT_COMMITTER_DATE="2026-10-29T12:00:00Z")
        with saved_fixture(self.root) as restore:
            self.assert_success(self.run_rename("apply"))
            expected = self.outcome()
            restore()
            move = unit_move.move

            def interrupted(path_move: unit_move.PathMove) -> None:
                move(path_move)
                if path_move.source == self.notifier / "showrunner-old":
                    raise OSError("stopped after timer move")

            with mock.patch.object(unit_move, "move", side_effect=interrupted):
                failed = self.run_rename("apply")
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn("stopped after timer move", failed.stderr)
            self.assertEqual(self.completed_steps(), 2)
            self.assertFalse((self.notifier / "showrunner-old").exists())
            self.assertTrue((self.notifier / "showrunner-new/conf").exists())
            self.assert_success(self.run_rename("apply"))
            self.assert_applied()
            self.assertEqual(self.outcome(), expected)

    def test_kept_branch_resumes_after_commit_before_save_or_failed_push(self) -> None:
        self.env.update(GIT_AUTHOR_DATE="2026-10-29T12:00:00Z", GIT_COMMITTER_DATE="2026-10-29T12:00:00Z")
        _ = self.git("branch", "-m", "merge-main")
        self.write(self.doc, self.doc.read_text().replace("**Merge branch:** `old`", "**Merge branch:** `merge-main`"))
        _ = self.git("add", ".")
        _ = self.git("commit", "-m", "Keep merge-main")
        _ = self.git("push", "origin", "merge-main")
        with saved_fixture(self.root) as restore:
            self.assert_success(self.run_rename("apply"))
            expected = self.outcome()
            for stop in ("commit", "push"):
                with self.subTest(stop=stop):
                    restore()
                    git = unit_move.git

                    def interrupted(checkout: Path, *arguments: str) -> str:
                        if arguments[0] == "push" and stop == "push":
                            raise OSError("failed rename push")
                        result = git(checkout, *arguments)
                        if arguments[0] == "commit" and stop == "commit":
                            raise OSError("stopped after rename commit")
                        return result

                    with mock.patch.object(unit_move, "git", side_effect=interrupted):
                        failed = self.run_rename("apply")
                    self.assertNotEqual(failed.returncode, 0)
                    self.assertIn("failed rename push" if stop == "push" else "stopped after rename commit", failed.stderr)
                    self.assertNotIn("_rename_branch", cast(list[str], self.journal()["completed"]))
                    tip = self.git("rev-parse", "HEAD")
                    self.assertEqual(self.git("log", "-1", "--format=%s"), "chore(new): rename production from old")
                    self.assertNotEqual(self.git("rev-parse", "origin/merge-main"), tip)
                    self.assert_success(self.run_rename("apply"))
                    self.assertEqual(self.git("rev-parse", "HEAD"), tip)
                    self.assertEqual(self.git("rev-parse", "origin/merge-main"), tip)
                    self.assertEqual(self.outcome(), expected)

    def test_kept_branch_still_refuses_an_unrelated_commit(self) -> None:
        _ = self.git("branch", "-m", "merge-main")
        self.write(self.doc, self.doc.read_text().replace("**Merge branch:** `old`", "**Merge branch:** `merge-main`"))
        _ = self.git("add", ".")
        _ = self.git("commit", "-m", "Keep merge-main")
        git = unit_move.git

        def interrupted(checkout: Path, *arguments: str) -> str:
            if arguments[0] == "push":
                raise OSError("failed rename push")
            return git(checkout, *arguments)

        with mock.patch.object(unit_move, "git", side_effect=interrupted):
            self.assertNotEqual(self.run_rename("apply").returncode, 0)
        _ = self.git("commit", "--amend", "-m", "Unrelated commit")
        refused = self.run_rename("apply")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("branch merge-main moved during the rename", refused.stderr)


if __name__ == "__main__":
    _ = unittest.main()
