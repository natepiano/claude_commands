"""The change-review trailer is required on checkpoints, and a merge adds one row per visible change.

Every test works in a temporary git repository and a temporary list; none reads or writes hanadocs.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from typing import override

sys.path.insert(0, str(Path(__file__).resolve().parent))
import change_review
from change_review import Invisible, Malformed, Visible

LIST = """# Change review list

## To review

| # | Change | Merged | On the Mac build | How to check |
| --- | --- | --- | --- | --- |
| C76 | Panels keep shields | 2026-10-14 | not yet | Launch Hana |
| C77 | Fonts redraw | 2026-10-14 | not yet | Change a font |

## Questions: later

| C80 | a closed row elsewhere still counts | 2026-10-01 | yes | - |
"""
SEE = "Change-Review: The grid fades more gently | Look across the floor"


class ParseTest(unittest.TestCase):
    def test_a_visible_line_a_none_line_and_a_malformed_line(self) -> None:
        self.assertEqual(change_review.entries(f"checkpoint(x): y\n\n{SEE}\nChange-Review: none — scripts only\n"
                                               + "Change-Review: something\n"),
                         [Visible("The grid fades more gently", "Look across the floor"), Invisible("scripts only"),
                          Malformed("something")])

    def test_only_a_checkpoint_that_changes_more_than_markdown_needs_a_line(self) -> None:
        self.assertTrue(change_review.needs_trailer("checkpoint(x): y", ("src/a.rs",)))
        self.assertFalse(change_review.needs_trailer("checkpoint(x): y", ("docs/a.md", "b.md")))
        for subject in ("ci(x): fix", "as-built(x): docs", "merge(x): origin", "production(x): row", "fix the vault"):
            self.assertFalse(change_review.needs_trailer(subject, ("src/a.rs",)), subject)

    def test_a_missing_or_malformed_line_is_named(self) -> None:
        self.assertIn("no Change-Review line", change_review.problem("checkpoint(x): y", "checkpoint(x): y\n", ("a.py",)))
        self.assertIn("neither", change_review.problem("checkpoint(x): y", "Change-Review: grid\n", ("a.py",)))
        self.assertEqual(change_review.problem("checkpoint(x): y", SEE, ("a.py",)), "")


class ListTest(unittest.TestCase):
    path: Path = Path()

    @override
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "change-review.md"
        _ = self.path.write_text(LIST, encoding="utf-8")

    def test_rows_take_the_next_number_in_the_whole_file_and_end_the_table(self) -> None:
        rows = change_review.append_rows(self.path, [Visible("A | B", "Look"), Visible("C", "D")],
                                         date(2026, 10, 14), "61b65bbf")
        self.assertEqual(rows, ["| C81 | A \\| B | 2026-10-14 · 61b65bbf | not yet | Look |",
                                "| C82 | C | 2026-10-14 · 61b65bbf | not yet | D |"])
        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[lines.index("| C77 | Fonts redraw | 2026-10-14 | not yet | Change a font |") + 1], rows[0])

    def test_a_list_without_the_section_is_refused_and_left_alone(self) -> None:
        _ = self.path.write_text("# list\n", encoding="utf-8")
        self.assertRaises(change_review.ChangeReviewError, change_review.append_rows, self.path,
                          [Visible("A", "B")], date(2026, 10, 14), "abc")
        self.assertEqual(self.path.read_text(encoding="utf-8"), "# list\n")

    def test_a_production_doc_names_its_list_or_none(self) -> None:
        doc = self.path.with_name("production.md")
        _ = doc.write_text("# p\n\n- **Change review:** `~/rust/hanadocs/change-review.md`\n", encoding="utf-8")
        self.assertEqual(change_review.list_path(doc), Path.home() / "rust/hanadocs/change-review.md")
        _ = doc.write_text("# p\n", encoding="utf-8")
        self.assertIsNone(change_review.list_path(doc))


class RepositoryTest(unittest.TestCase):
    """A unit branch with its own checkpoints and a merge from the merge branch."""

    repo: Path = Path()
    previous: str = ""

    def git(self, *arguments: str, day: str = "2026-10-15T12:00:00-04:00") -> str:
        environment = {**os.environ, "GIT_AUTHOR_DATE": day, "GIT_COMMITTER_DATE": day,
                       "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
        return subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=t", "-c", "user.email=t@t",
                               "-c", "commit.gpgsign=false", *arguments],
                              capture_output=True, text=True, check=True, env=environment).stdout.strip()

    def commit(self, name: str, message: str, day: str = "2026-10-15T12:00:00-04:00") -> str:
        _ = (self.repo / name).write_text(message, encoding="utf-8")
        _ = self.git("add", name)
        _ = self.git("commit", "-q", "-m", message, day=day)
        return self.git("rev-parse", "HEAD")

    @override
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.repo = Path(directory.name)
        _ = self.git("init", "-q", "-b", "merge")
        self.previous = self.commit("base.py", "checkpoint(u): base\n\nChange-Review: none — base")
        _ = self.git("checkout", "-q", "-b", "unit")
        _ = self.git("checkout", "-q", "merge")
        _ = self.commit("other.py", "checkpoint(other): no trailer, merged into the unit")
        _ = self.git("update-ref", "refs/remotes/origin/merge", "HEAD")
        _ = self.git("checkout", "-q", "unit")
        _ = self.git("merge", "-q", "--no-ff", "-m", "merge(u): origin/merge", "origin/merge")

    def test_only_the_units_own_checkpoints_are_read(self) -> None:
        checkpoint = self.commit("a.py", f"checkpoint(u): grid\n\n{SEE}")
        commits = change_review.unit_commits(self.repo, checkpoint, self.previous, "merge")
        self.assertEqual([commit.subject for commit in commits], ["checkpoint(u): grid"])
        self.assertEqual(change_review.missing(commits), [])
        self.assertEqual(change_review.visible(commits), [Visible("The grid fades more gently", "Look across the floor")])

    def test_a_checkpoint_without_a_line_is_refused_even_when_the_named_checkpoint_is_a_merge(self) -> None:
        _ = self.commit("a.py", "checkpoint(u): grid")
        _ = self.git("checkout", "-q", "-b", "side")
        _ = self.commit("b.md", "docs only")
        _ = self.git("checkout", "-q", "unit")
        _ = self.git("merge", "-q", "--no-ff", "-m", "merge(u): side", "side")
        refused = change_review.missing(change_review.unit_commits(self.repo, "HEAD", self.previous, "merge"))
        self.assertEqual(len(refused), 1)
        self.assertIn("checkpoint(u): grid: no Change-Review line", refused[0])

    def test_a_later_checkpoint_covers_one_pushed_without_its_line(self) -> None:
        early = self.commit("a.py", "checkpoint(u): grid")
        self.assertEqual(len(change_review.missing(change_review.unit_commits(self.repo, "HEAD", self.previous, "merge"))), 1)
        _ = self.commit("b.py", f"checkpoint(u): change review\n\n{SEE}\nChange-Review-Covers: {early[:8]}")
        commits = change_review.unit_commits(self.repo, "HEAD", self.previous, "merge")
        self.assertEqual(change_review.missing(commits), [])
        self.assertEqual(change_review.visible(commits), [Visible("The grid fades more gently", "Look across the floor")])

    def test_a_checkpoint_made_before_the_rule_landed_is_not_read(self) -> None:
        _ = self.commit("a.py", "checkpoint(u): before the rule", day="2026-10-14T23:59:00-04:00")
        checkpoint = self.commit("b.py", f"checkpoint(u): grid\n\n{SEE}")
        commits = change_review.unit_commits(self.repo, checkpoint, self.previous, "merge")
        self.assertEqual([commit.subject for commit in commits], ["checkpoint(u): grid"])

    def test_drift_names_a_merge_with_a_visible_change_and_no_row(self) -> None:
        _ = self.commit("a.py", f"checkpoint(u): grid\n\n{SEE}")
        _ = self.git("checkout", "-q", "merge")
        _ = self.git("merge", "-q", "--no-ff", "-m", "Merge u", "unit")
        _ = self.git("update-ref", "refs/remotes/origin/merge", "HEAD")
        short = self.git("rev-parse", "--short=8", "HEAD")
        self.assertEqual(change_review.drift(self.repo, "merge", LIST, "2026-10-01"),
                         [f"{short}: 1 visible change(s) and no row in the list"])
        self.assertEqual(change_review.drift(self.repo, "merge", LIST + f"| C81 | g | 2026-10-14 · {short} | not yet | x |\n",
                                             "2026-10-01"), [])


if __name__ == "__main__":
    _ = unittest.main()
