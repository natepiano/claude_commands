"""Unit rename recovery after doc writes and a successful but unjournaled restart."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import sys
import unittest
from typing import cast, final
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scripts.production.rename_unit_test_support import UnitRenameFixture
from scripts.production.rename_test_support import legacy_staff_journal, stop_at_staff_write
from scripts.production.rename_rerun_test_support import final_files, saved_fixture
import rename_unit


@final
class UnitRenameRerunTests(UnitRenameFixture):
    def test_legacy_journal_resumes_after_the_units_row_changed(self) -> None:
        save = rename_unit._save  # pyright: ignore[reportPrivateUsage]

        def interrupted(path: Path, renamed: rename_unit.UnitRename) -> None:
            if renamed.step == 5:
                raise OSError("stopped after Units commit")
            save(path, renamed)

        with mock.patch.object(rename_unit, "_save", side_effect=interrupted):
            self.assert_refused(self.rename("apply"), "stopped after Units commit")
        legacy_staff_journal(self.root / "state/showrunner/demo/rename_unit.json")
        self.assert_ok(self.rename("apply"))
        self.assertIs(self.journal()["done"], True)
        self.assertTrue(self.journal()["staff_id"])

    def test_completed_legacy_journal_migrates_once_and_reports_done(self) -> None:
        self.assert_ok(self.rename("apply"))
        expected = self.outcome()
        legacy_staff_journal(self.root / "state/showrunner/demo/rename_unit.json")
        finished = self.rename("apply")
        self.assert_ok(finished)
        self.assertIn("done", finished.stdout)
        self.assertEqual(self.outcome(), expected)

    def test_rename_unit_rerun(self) -> None:
        self.env.update(GIT_AUTHOR_DATE="2026-10-29T12:00:00Z", GIT_COMMITTER_DATE="2026-10-29T12:00:00Z")
        with saved_fixture(self.root) as restore:
            self.assert_ok(self.rename("apply"))
            expected = self.outcome()
            for after in (False, True):
                with self.subTest(after=after):
                    restore()

                    with stop_at_staff_write("rename", after=after):
                        self.assertNotEqual(self.rename("apply").returncode, 0)
                    self.assert_ok(self.rename("apply"))
                    self.assertEqual(self.outcome(), expected)
                    finished = self.rename("apply")
                    self.assert_ok(finished)
                    self.assertIn("done", finished.stdout)
                    self.assertEqual(self.outcome(), expected)

    def outcome(self) -> tuple[dict[str, bytes], str, str, str]:
        return (final_files(self.root), self.git("show-ref"), self.git("status", "--porcelain"),
                self.git("--git-dir", str(self.origin), "show-ref"))

    def test_resume_after_doc_or_plan_write_before_commit(self) -> None:
        self.env.update(GIT_AUTHOR_DATE="2026-10-29T12:00:00Z", GIT_COMMITTER_DATE="2026-10-29T12:00:00Z")
        with saved_fixture(self.root) as restore:
            self.assert_ok(self.rename("apply"))
            expected = self.outcome()
            for path in (self.doc, self.checkout / "docs/plans/alpha.md"):
                with self.subTest(path=path):
                    restore()
                    write = cast(Callable[[Path, str], None], getattr(rename_unit, "write_text"))

                    def interrupted(destination: Path, content: str) -> None:
                        write(destination, content)
                        if destination == path:
                            raise OSError("stopped after doc write")

                    with mock.patch.object(rename_unit, "write_text", side_effect=interrupted):
                        failed = self.rename("apply")
                    self.assert_refused(failed, "stopped after doc write")
                    self.assertEqual(self.journal()["step"], 4)
                    self.assertEqual(self.journal()["doc_commit"], "")
                    self.assertTrue(self.git("status", "--porcelain", "--untracked-files=no"))
                    self.assert_ok(self.rename("apply"))
                    self.assertIs(self.journal()["done"], True)
                    self.assertEqual(self.outcome(), expected)

    def test_resume_after_restart_before_relaunch_progress_save(self) -> None:
        self.env.update(GIT_AUTHOR_DATE="2026-10-29T12:00:00Z", GIT_COMMITTER_DATE="2026-10-29T12:00:00Z")
        _ = self.seed_record()
        shutdown = rename_unit.run_shutdown
        live: list[Callable[[], None]] = []

        def restart(*arguments: str) -> None:
            shutdown(*arguments)
            # The private shutdown stand-in publishes the live unit only once.
            if not live:
                live.append(self.mark_live("gamma-unit", "sid-alpha"))

        with saved_fixture(self.root) as restore:
            with mock.patch.object(rename_unit, "run_shutdown", side_effect=restart):
                self.assert_ok(self.rename("apply"))
            expected = self.outcome()
            # Remove the live fixture socket before restoring the saved files.
            end = live.pop()
            end()
            restore()
            def interrupted(*arguments: str) -> None:
                restart(*arguments)
                raise OSError("stopped after restart")

            with mock.patch.object(rename_unit, "run_shutdown", side_effect=interrupted):
                failed = self.rename("apply")
            self.assert_refused(failed, "stopped after restart")
            self.assertEqual(self.journal()["step"], 9)
            self.assertEqual(len(live), 1)
            with mock.patch.object(rename_unit, "run_shutdown", side_effect=restart):
                self.assert_ok(self.rename("apply"))
            self.assertEqual(len(live), 1, "rerun must not launch another Claude")
            self.assertIs(self.journal()["done"], True)
            self.assertEqual(self.outcome(), expected)

    def test_resume_after_staging_doc_edits_before_commit(self) -> None:
        self.env.update(GIT_AUTHOR_DATE="2026-10-29T12:00:00Z", GIT_COMMITTER_DATE="2026-10-29T12:00:00Z")
        with saved_fixture(self.root) as restore:
            self.assert_ok(self.rename("apply"))
            expected = self.outcome()
            restore()
            git = rename_unit.git  # pyright: ignore[reportPrivateLocalImportUsage]

            def interrupted(checkout: Path, *arguments: str) -> str:
                result = git(checkout, *arguments)
                if arguments[0] == "add":
                    raise OSError("stopped after staging doc edits")
                return result

            with mock.patch.object(rename_unit, "git", side_effect=interrupted):
                self.assert_refused(self.rename("apply"), "stopped after staging doc edits")
            self.assertTrue(self.git("diff", "--cached", "--name-only"))
            self.assertEqual(self.journal()["step"], 4)
            self.assert_ok(self.rename("apply"))
            self.assertEqual(self.outcome(), expected)

    def test_doc_resume_refuses_unrelated_content_paths_and_modes(self) -> None:
        with saved_fixture(self.root) as restore:
            for damage in ("doc", "plan", "unrelated", "mode", "index"):
                with self.subTest(damage=damage):
                    restore()
                    write = cast(Callable[[Path, str], None], getattr(rename_unit, "write_text"))

                    def interrupted(destination: Path, content: str) -> None:
                        write(destination, content)
                        if destination == self.doc:
                            raise OSError("stopped after doc write")

                    with mock.patch.object(rename_unit, "write_text", side_effect=interrupted):
                        self.assert_refused(self.rename("apply"), "stopped after doc write")
                    path = (self.doc if damage in ("doc", "mode", "index") else self.checkout /
                            ("docs/plans/alpha.md" if damage == "plan" else "NOTES.md"))
                    if damage == "mode":
                        path.chmod(path.stat().st_mode | 0o111)
                    elif damage == "index":
                        own = path.read_text()
                        self.write(path, own + "unrelated staged edit\n")
                        _ = self.git("add", str(path))
                        self.write(path, own)
                    else:
                        self.write(path, path.read_text() + "unrelated edit\n")
                    before = final_files(self.root)
                    index = self.git("diff", "--cached")
                    self.assert_refused(self.rename("apply"), "checkout must be clean")
                    self.assertEqual(self.journal()["step"], 4)
                    self.assertEqual(final_files(self.root), before)
                    self.assertEqual(self.git("diff", "--cached"), index)

    def test_relaunch_resume_refuses_another_live_session(self) -> None:
        _ = self.seed_record()
        save = rename_unit._save  # pyright: ignore[reportPrivateUsage]

        def interrupted(path: Path, renamed: rename_unit.UnitRename) -> None:
            if renamed.step == 10:
                raise OSError("stopped before relaunch progress save")
            save(path, renamed)

        with mock.patch.object(rename_unit, "_save", side_effect=interrupted):
            self.assert_refused(self.rename("apply"), "stopped before relaunch progress save")
        _ = self.mark_live("gamma-unit", "sid-unrelated")
        self.assert_refused(self.rename("apply"), "gamma-unit has a live Claude")
        self.assertEqual(self.journal()["step"], 9)


if __name__ == "__main__":
    _ = unittest.main()
