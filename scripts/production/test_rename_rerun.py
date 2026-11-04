"""Production rename reruns after a failed step."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest
from typing import final
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scripts.production.rename_test_support import ProductionRenameFixture, stop_at_staff_write
from scripts.production.rename_rerun_test_support import final_files, saved_fixture
import unit_move


@final
class ProductionRenameRerunTests(ProductionRenameFixture):
    def test_rename_rerun(self) -> None:
        self.env.update(GIT_AUTHOR_DATE="2026-10-29T12:00:00Z", GIT_COMMITTER_DATE="2026-10-29T12:00:00Z")
        with saved_fixture(self.root) as restore:
            self.assert_success(self.run_rename("apply"))
            expected = self.outcome()
            for after in (False, True):
                with self.subTest(after=after):
                    restore()

                    with stop_at_staff_write("rename", after=after):
                        failed = self.run_rename("apply")
                    self.assertNotEqual(failed.returncode, 0)
                    self.assertIn("stopped at staff step", failed.stderr)
                    self.assert_success(self.run_rename("apply"))
                    self.assertEqual(self.outcome(), expected)
                    finished = self.run_rename("apply")
                    self.assert_success(finished)
                    self.assertIn("unchanged", finished.stdout)
                    self.assertEqual(self.outcome(), expected)

    def outcome(self) -> tuple[dict[str, bytes], str, str, str]:
        return (final_files(self.root), self.git("show-ref"), self.git("status", "--porcelain"),
                self.git("--git-dir", str(self.origin), "show-ref"))

    def test_rerun_after_a_failed_timer_move(self) -> None:
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
            self.assertIn("stopped after timer move", failed.stderr)
            self.assertFalse((self.notifier / "showrunner-old").exists())
            self.assertTrue((self.notifier / "showrunner-new/conf").exists())
            self.assert_success(self.run_rename("apply"))
            self.assert_applied()
            self.assertEqual(self.outcome(), expected)

    def test_kept_branch_reruns_after_a_failed_commit_or_push(self) -> None:
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
                    self.assertIn("failed rename push" if stop == "push" else "stopped after rename commit", failed.stderr)
                    tip = self.git("rev-parse", "HEAD")
                    self.assertEqual(self.git("log", "-1", "--format=%s"), "chore(new): rename production from old")
                    self.assertNotEqual(self.git("rev-parse", "origin/merge-main"), tip)
                    self.assert_success(self.run_rename("apply"))
                    self.assertEqual(self.git("rev-parse", "HEAD"), tip)
                    self.assertEqual(self.git("rev-parse", "origin/merge-main"), tip)
                    self.assertEqual(self.outcome(), expected)


if __name__ == "__main__":
    _ = unittest.main()
