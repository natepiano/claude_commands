"""The change-review trailer is required on checkpoints, and a merge adds one row per visible change.

Every test works in a temporary git repository and a temporary list; none reads or writes hanadocs.
"""
from __future__ import annotations

import fcntl
import io
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path
from typing import override
from unittest import mock

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

## Reviewed

| # | Change | Verdict | Date |
| --- | --- | --- | --- |
| C70 | Older | works | 2026-10-13 |
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


def run(*arguments: str) -> tuple[int, str, str]:
    """The command line's exit code, standard output and standard error."""
    output, error = io.StringIO(), io.StringIO()
    with redirect_stdout(output), redirect_stderr(error):
        code = change_review.main(list(arguments))
    return code, output.getvalue(), error.getvalue()


class ActionTest(unittest.TestCase):
    """add, show, verdict and list-path, on a Hana-shaped list and on a list not yet written."""

    path: Path = Path()
    fresh: Path = Path()

    @override
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "change-review.md"
        _ = self.path.write_text(LIST, encoding="utf-8")
        self.fresh = Path(directory.name) / "change-review" / "slug.md"

    def test_the_first_row_creates_a_missing_list_from_the_skeleton(self) -> None:
        rows = change_review.append_rows(self.fresh, [Visible("A", "B")], date(2026, 10, 15), "")
        self.assertEqual(rows, ["| C1 | A | 2026-10-15 | B |"])
        self.assertEqual(self.fresh.read_text(encoding="utf-8"), change_review.SKELETON.replace(
            "| --- | --- | --- | --- |\n\n## Reviewed", "| --- | --- | --- | --- |\n| C1 | A | 2026-10-15 | B |\n\n## Reviewed"))

    def test_add_takes_the_next_number_and_fills_the_mac_build_column_only_where_the_table_has_one(self) -> None:
        self.assertEqual(run("add", "--list", str(self.path), "--change", "Tabs | spaces", "--check", "Look",
                             "--merged", "216c4e1d"), (0, "C81\n", ""))
        day = change_review.today().isoformat()
        self.assertIn(f"| C81 | Tabs \\| spaces | {day} · 216c4e1d | not yet | Look |\n\n## Questions",
                      self.path.read_text(encoding="utf-8"))
        self.assertEqual(run("add", "--list", str(self.fresh), "--change", "A", "--check", "B")[1], "C1\n")
        self.assertIn(f"| C1 | A | {day} | B |", self.fresh.read_text(encoding="utf-8"))

    def test_add_refuses_an_empty_or_multi_line_part_and_writes_nothing(self) -> None:
        for change, check in (("", "Look"), ("A", " "), ("A\nB", "Look")):
            code, _, error = run("add", "--list", str(self.path), "--change", change, "--check", check)
            self.assertEqual(code, 1)
            self.assertIn("needs one line of words", error)
        self.assertEqual(self.path.read_text(encoding="utf-8"), LIST)

    def test_show_walks_the_rows_to_review_and_reads_only(self) -> None:
        self.assertEqual(run("show", "--list", str(self.path)), (0, (
            "C76 Panels keep shields\n  How to check: Launch Hana\n  Merged: 2026-10-14\n  On the Mac build: not yet\n\n"
            + "C77 Fonts redraw\n  How to check: Change a font\n  Merged: 2026-10-14\n  On the Mac build: not yet\n"), ""))
        self.assertEqual(run("show", "--list", str(self.fresh)), (0, "nothing to review\n", ""))
        self.assertFalse(self.fresh.parent.exists())

    def test_a_verdict_moves_the_row_to_the_top_of_reviewed(self) -> None:
        self.assertEqual(change_review.record_verdict(self.path, "C76", "Works, thanks", date(2026, 10, 15)), "2026-10-14")
        text = self.path.read_text(encoding="utf-8")
        self.assertNotIn("| C76 | Panels keep shields | 2026-10-14", text)
        self.assertIn("| --- | --- | --- | --- |\n| C76 | Panels keep shields | Works, thanks | 2026-10-15 |\n| C70 |", text)

    def test_works_or_fine_lands_and_any_other_verdict_returns_to_the_unit(self) -> None:
        self.assertEqual(run("verdict", "--list", str(self.path), "--row", "C76", "--verdict", "fine"),
                         (0, "C76 landed\n", ""))
        self.assertEqual(run("verdict", "--list", str(self.path), "--row", "C77", "--verdict", "still the old font | x"),
                         (0, "C77 returns to its unit (merged 2026-10-14)\n", ""))
        self.assertIn("| C77 | Fonts redraw | still the old font \\| x |", self.path.read_text(encoding="utf-8"))
        self.assertTrue(change_review.lands("Works: it does"))
        self.assertFalse(change_review.lands("not fine"))

    def test_a_verdict_for_a_row_not_to_review_is_refused(self) -> None:
        for row in ("C70", "C99", "76"):
            code, output, error = run("verdict", "--list", str(self.path), "--row", row, "--verdict", "works")
            self.assertEqual((code, output), (1, ""), row)
            self.assertEqual(len(error.splitlines()), 1, row)
        self.assertEqual(self.path.read_text(encoding="utf-8"), LIST)

    def test_a_write_waits_for_the_lock_and_concurrent_writes_keep_every_row(self) -> None:
        held = os.open(self.path.parent, os.O_RDONLY)
        fcntl.flock(held, fcntl.LOCK_EX)
        writers = [threading.Thread(target=change_review.append_rows,
                                    args=(self.path, [Visible(f"Row {index}", "Look")], date(2026, 10, 15), ""))
                   for index in range(8)]
        for writer in writers:
            writer.start()
        writers[0].join(0.3)
        self.assertTrue(writers[0].is_alive())
        self.assertEqual(self.path.read_text(encoding="utf-8"), LIST)
        os.close(held)
        for writer in writers:
            writer.join(10)
        rows = [row for row in self.path.read_text(encoding="utf-8").splitlines() if "| Row " in row]
        self.assertEqual(sorted(row.split(" ")[1] for row in rows), [f"C{number}" for number in range(81, 89)])

    def test_list_path_prints_the_list_a_production_doc_names(self) -> None:
        doc = self.path.with_name("production.md")
        _ = doc.write_text(f"# p\n\n- **Change review:** `{self.fresh}`\n", encoding="utf-8")
        self.assertEqual(run("list-path", "--production", str(doc)), (0, f"{self.fresh}\n", ""))
        _ = doc.write_text("# p\n", encoding="utf-8")
        self.assertEqual(run("list-path", "--production", str(doc))[:2], (1, ""))
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": ""}):
            self.assertIn("pass --production", run("list-path")[2])


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
