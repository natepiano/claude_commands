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
import showrunners
import showrunner_tree
from change_review import Invisible, Malformed, Visible

# Importable once change_review has put scripts/lib on the path.
import scratch
import urgency

# Hang guards allow for load 136 on natedev (2026-10-29).
TEST_HANG_GUARD_SECS = 300

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
GRID = Visible("The grid fades more gently", "Run `hana` in a natedev terminal", "The grid lines fade out farther away")
SEE = f"Change-Review: {GRID.change} | {GRID.check} | {GRID.see}"
# A production doc as the vault records it; a write never reads the doc itself.
PRODUCTION = Path.home() / "worktrees/demo-trunk/docs/plans/demo-production.md"
DOC = str(PRODUCTION)


def setUpModule() -> None:
    """The pin on the row `show` printed and the scratch folders a verdict removes are in a temporary
    folder, never ~/.local/state/next, /tmp/claude/scratch or ~/rust/repro."""
    folder = tempfile.TemporaryDirectory()
    unittest.addModuleCleanup(folder.cleanup)
    unittest.enterModuleContext(mock.patch.dict(os.environ, {
        "NEXT_STATE_DIR": folder.name, scratch.ROOT_ENV: f"{folder.name}/scratch",
        scratch.VISIBLE_ROOT_ENV: f"{folder.name}/repro"}))
    _ = unittest.enterModuleContext(mock.patch.object(showrunners, "timer_docs", return_value={}))


class ParseTest(unittest.TestCase):
    def test_a_visible_line_a_none_line_and_a_malformed_line(self) -> None:
        self.assertEqual(change_review.entries(f"checkpoint(x): y\n\n{SEE}\nChange-Review: none — scripts only\n"
                                               + "Change-Review: something\n"),
                         [GRID, Invisible("scripts only"),
                          Malformed("something")])

    def test_only_a_checkpoint_that_changes_more_than_markdown_needs_a_line(self) -> None:
        self.assertTrue(change_review.needs_trailer("checkpoint(x): y", ("src/a.rs",)))
        self.assertFalse(change_review.needs_trailer("checkpoint(x): y", ("docs/a.md", "b.md")))
        for subject in ("ci(x): fix", "as-built(x): docs", "merge(x): origin", "production(x): row", "fix the vault"):
            self.assertFalse(change_review.needs_trailer(subject, ("src/a.rs",)), subject)

    def test_a_missing_or_malformed_line_is_named(self) -> None:
        self.assertIn("no Change-Review line", change_review.problem("checkpoint(x): y", "checkpoint(x): y\n", ("a.py",)))
        self.assertIn(change_review.FORM, change_review.problem("checkpoint(x): y", "checkpoint(x): y\n", ("a.py",)))
        self.assertIn(change_review.FORM, change_review.problem("checkpoint(x): y", "Change-Review: grid\n", ("a.py",)))
        self.assertEqual(change_review.problem("checkpoint(x): y", SEE, ("a.py",)), "")

    def test_a_line_without_exactly_three_full_parts_is_refused(self) -> None:
        for value in ("The grid fades | Look across the floor", "A | B | ", " | B | C", "A | B | C | D"):
            self.assertEqual(change_review.parse(value), Malformed(value.strip()), value)
            problem = change_review.problem("checkpoint(x): y", f"Change-Review: {value}\n", ("a.py",))
            self.assertIn("needs three parts", problem, value)
            self.assertIn(change_review.FORM, problem, value)

    def test_the_check_cell_is_how_to_try_it_then_what_you_see(self) -> None:
        self.assertEqual(GRID.how(), "Run `hana` in a natedev terminal. You see: The grid lines fade out farther away")
        self.assertEqual(Visible("A", "Type `x`:", "B").how(), "Type `x`: You see: B")


class ListTest(unittest.TestCase):
    path: Path = Path()

    @override
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "change-review.md"
        _ = self.path.write_text(LIST, encoding="utf-8")

    def test_rows_take_the_next_number_in_the_whole_file_and_end_the_table(self) -> None:
        rows = change_review.append_rows(self.path, [Visible("A | B", "Look", "It"), Visible("C", "D.", "E")],
                                         date(2026, 10, 14), "61b65bbf", PRODUCTION)
        self.assertEqual(rows, ["| C81 | A \\| B | 2026-10-14 · 61b65bbf | not yet | Look. You see: It | demo |",
                                "| C82 | C | 2026-10-14 · 61b65bbf | not yet | D. You see: E | demo |"])
        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[lines.index("| C77 | Fonts redraw | 2026-10-14 | not yet | Change a font | |") + 1], rows[0])

    def test_a_list_without_the_section_is_refused_and_left_alone(self) -> None:
        _ = self.path.write_text("# list\n", encoding="utf-8")
        self.assertRaises(change_review.ChangeReviewError, change_review.append_rows, self.path,
                          [Visible("A", "B", "C")], date(2026, 10, 14), "abc", PRODUCTION)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "# list\n")

    def test_a_production_doc_names_its_list_or_none(self) -> None:
        doc = self.path.with_name("production.md")
        _ = doc.write_text("# p\n\n- **Change review:** `~/rust/hanadocs/change-review.md`\n", encoding="utf-8")
        self.assertEqual(change_review.list_path(doc), Path.home() / "rust/hanadocs/change-review.md")
        _ = doc.write_text("# p\n", encoding="utf-8")
        self.assertIsNone(change_review.list_path(doc))

    def test_the_list_is_named_for_the_running_top_level_showrunner(self) -> None:
        doc = self.path.with_name("demo-production.md")
        mine = change_review.list_file("natedev")
        _ = doc.write_text(f"- **Change review:** `{mine}` — changes for the user to check\n", encoding="utf-8")
        with mock.patch.object(change_review, "showrunner_of", return_value="natedev"):
            self.assertEqual(change_review.list_path(doc), mine)
        self.assertEqual(mine.name, "change-review - natedev.md")
        stale = Path.home() / "rust/hanadocs/change-review/build-followups.md"
        _ = doc.write_text(f"- **Change review:** `{stale}`\n", encoding="utf-8")
        error = io.StringIO()
        with mock.patch.object(change_review, "showrunner_of", return_value="natedev"), redirect_stderr(error):
            self.assertEqual(change_review.list_path(doc), mine)
        self.assertIn(f"names {stale}", error.getvalue())
        down = change_review.ChangeReviewError("the showrunner of demo is not running")
        with mock.patch.object(change_review, "showrunner_of", side_effect=down):
            self.assertEqual(change_review.list_path(doc), stale)

    def test_a_child_production_takes_its_root_showrunners_name(self) -> None:
        import showrunners

        names = {"build-followups": "natedev"}

        def name_of(slug: str) -> str:
            return names.get(slug, "")

        with (mock.patch.object(showrunners, "canonical_slug", side_effect=str),
              mock.patch.object(showrunner_tree, "root_of", return_value="build-followups") as root,
              mock.patch.object(showrunners, "current_name", side_effect=name_of)):
            self.assertEqual(change_review.showrunner_of(Path("/p/child-production.md")), "natedev")
            root.assert_called_once_with("child")
            names.clear()
            self.assertRaises(change_review.ChangeReviewError, change_review.showrunner_of,
                              Path("/p/child-production.md"))


class AttachedListTest(unittest.TestCase):
    root: Path = Path()
    doc: Path = Path()
    current: Path = Path()
    earlier: Path = Path()

    @override
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.doc = self.root / "child-production.md"
        _ = self.doc.write_text("# Child\n")
        self.current = self.root / "change-review - parent.md"
        self.earlier = self.root / "change-review - child.md"
        _ = self.current.write_text("## To review\n\n| # | Change | Production | How to check | Merged |\n" +
            "| --- | --- | --- | --- | --- |\n" +
            "| C9 | Current child | child | Check current | 2026-10-20 |\n" +
            "| C10 | Parent-only legacy row | | Check parent | 2026-10-01 |\n")
        _ = self.earlier.write_text("## To review\n\n| # | Change | How to check | Merged | Urgency |\n" +
            "| --- | --- | --- | --- | --- |\n" +
            "| C9 | Earlier child | Check earlier | 2026-10-02 | |\n" +
            "| C11 | Dependent child | Check dependent | 2026-10-03 | blocks, after C9 |\n")
        _ = self.enterContext(mock.patch.object(change_review, "LISTS", self.root))
        _ = self.enterContext(mock.patch.object(showrunner_tree, "root_of", return_value="parent"))

    def test_columnless_earlier_rows_belong_to_the_attached_production_without_list_edits(self) -> None:
        before = {path: path.read_bytes() for path in (self.current, self.earlier)}
        rows = change_review.scope_rows(self.current, self.doc)
        self.assertEqual([(row["#"], row["Change"]) for row in rows],
                         [("C9", "Current child"), ("C9", "Earlier child"), ("C11", "Dependent child")])
        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_merged_prerequisite_names_its_own_list_despite_a_repeated_row_number(self) -> None:
        entries = change_review.scope_entries(self.current, self.doc)
        self.assertEqual({entry.key: entry.after for entry in entries},
                         {"C9": "", "child/C9": "", "child/C11": "child/C9"})
        ordered = [entry.key for entry in urgency.queue_order(entries, "attached-list-prerequisites")]
        self.assertLess(ordered.index("child/C9"), ordered.index("child/C11"))

    def test_broken_current_list_keeps_earlier_rows_and_reports_one_problem(self) -> None:
        _ = self.current.write_text("# Missing table\n")
        errors = io.StringIO()
        with redirect_stderr(errors):
            rows = change_review.scope_rows(self.current, self.doc)
        self.assertEqual([row["Change"] for row in rows], ["Earlier child", "Dependent child"])
        self.assertEqual(len(errors.getvalue().splitlines()), 1)
        self.assertIn(str(self.current), errors.getvalue())

    def test_absent_earlier_list_preserves_current_row_keys_and_filter(self) -> None:
        self.earlier.unlink()
        entries = change_review.scope_entries(self.current, self.doc)
        self.assertEqual([(entry.key, entry.item["Change"]) for entry in entries], [("C9", "Current child")])

    def test_show_for_the_calling_production_includes_earlier_rows_and_scoped_count(self) -> None:
        before = {path: path.read_bytes() for path in (self.current, self.earlier)}
        with mock.patch.object(change_review, "production_doc", return_value=self.doc), \
                mock.patch.object(change_review, "production_list", return_value=self.current):
            code, output, error = run("show")
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(output.splitlines()[0], "child/C9 Earlier child")
        self.assertEqual(output.splitlines()[-1], "2 more to review")
        self.assertNotIn("Parent-only", output)
        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_current_list_prerequisites_keep_their_local_row_ids_after_attachment(self) -> None:
        _ = self.current.write_text("## To review\n\n| # | Change | Production | How to check | Merged | Urgency |\n" +
            "| --- | --- | --- | --- | --- | --- |\n" +
            "| C9 | Current child | child | Check current | 2026-10-20 | after C12 |\n" +
            "| C12 | Current prerequisite | child | Check prerequisite | 2026-10-21 | |\n")
        entries = change_review.scope_entries(self.current, self.doc)
        self.assertEqual({entry.key: entry.after for entry in entries},
                         {"C9": "C12", "C12": "", "child/C9": "", "child/C11": "child/C9"})
        ordered = [entry.key for entry in urgency.queue_order(entries, "attached-current-prerequisites")]
        self.assertLess(ordered.index("C12"), ordered.index("C9"))


class StampTest(unittest.TestCase):
    """Every write sets date_modified and adds its production once, keeping every other key."""

    path: Path = Path()

    @override
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "change-review - natedev.md"
        _ = self.path.write_text("\n".join([
            "---", 'date_created: "[[2026-10-14]]"', 'date_modified: "[[2026-10-14]]"', "productions:",
            f'  - "{PRODUCTION}"', "showrunner: natedev", "tags:", "  - review", "---", "",
            "# natedev showrunner: changes to review", "", change_review.BODY]), encoding="utf-8")

    def frontmatter(self) -> list[str]:
        lines = self.path.read_text(encoding="utf-8").splitlines()
        return lines[:lines.index("---", 1) + 1]

    def test_a_write_moves_date_modified_and_keeps_a_listed_production_in_either_form(self) -> None:
        _ = change_review.append_rows(self.path, [Visible("A", "B", "C")], date(2026, 10, 15), "",
                                      Path("~/worktrees/demo-trunk/docs/plans/demo-production.md"))
        self.assertEqual(self.frontmatter(), [
            "---", 'date_created: "[[2026-10-14]]"', 'date_modified: "[[2026-10-15]]"', "productions:",
            f'  - "{PRODUCTION}"', "showrunner: natedev", "tags:", "  - review", "---"])

    def test_a_new_production_is_added_after_the_listed_ones(self) -> None:
        other = Path.home() / "rust/cargo-liner/docs/child-production.md"
        _ = change_review.append_rows(self.path, [Visible("A", "B", "C")], date(2026, 10, 15), "", other)
        _ = change_review.record_verdict(self.path, "C1", "works", date(2026, 10, 16), other)
        self.assertEqual(self.frontmatter()[2:6], [
            'date_modified: "[[2026-10-16]]"', "productions:", f'  - "{PRODUCTION}"',
            '  - "~/rust/cargo-liner/docs/child-production.md"'])

    def test_a_list_without_frontmatter_gains_it_on_its_next_write(self) -> None:
        _ = self.path.write_text(change_review.BODY, encoding="utf-8")
        _ = change_review.append_rows(self.path, [Visible("A", "B", "C")], date(2026, 10, 15), "", PRODUCTION)
        self.assertEqual(self.frontmatter(), [
            "---", 'date_created: "[[2026-10-15]]"', 'date_modified: "[[2026-10-15]]"', "productions:",
            '  - "~/worktrees/demo-trunk/docs/plans/demo-production.md"', "showrunner: natedev", "---"])


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
        self.fresh = Path(directory.name) / "change-review" / "change-review - natedev.md"

    def test_the_first_row_creates_a_missing_list_from_the_skeleton(self) -> None:
        rows = change_review.append_rows(self.fresh, [Visible("A", "B", "C")], date(2026, 10, 15), "", PRODUCTION)
        self.assertEqual(rows, ["| C1 | A | 2026-10-15 | B. You see: C | demo |"])
        self.assertEqual(self.fresh.read_text(encoding="utf-8").split("\n\n## To review")[0], "\n".join([
            "---", 'date_created: "[[2026-10-15]]"', 'date_modified: "[[2026-10-15]]"', "productions:",
            '  - "~/worktrees/demo-trunk/docs/plans/demo-production.md"', "showrunner: natedev", "---", "",
            "# natedev showrunner: changes to review", "",
            'Ask for "change review" any time to walk it one change at a time. A reviewed row moves to Reviewed '
            + "with your verdict."]))
        self.assertIn("| --- | --- | --- | --- | --- |\n| C1 | A | 2026-10-15 | B. You see: C | demo |\n\n## Reviewed", self.fresh.read_text(encoding="utf-8"))

    def test_add_takes_the_next_number_and_fills_the_mac_build_column_only_where_the_table_has_one(self) -> None:
        self.assertEqual(run("add", "--production", DOC, "--list", str(self.path), "--change", "Tabs | spaces", "--check", "Look", "--see", "It",
                             "--merged", "216c4e1d"), (0, "C81\n", ""))
        day = change_review.today().isoformat()
        self.assertIn(f"| C81 | Tabs \\| spaces | {day} · 216c4e1d | not yet | Look. You see: It | demo |\n\n## Questions",
                      self.path.read_text(encoding="utf-8"))
        self.assertEqual(run("add", "--production", DOC, "--list", str(self.fresh), "--change", "A", "--check", "B", "--see", "C")[1], "C1\n")
        self.assertIn(f"| C1 | A | {day} | B. You see: C |", self.fresh.read_text(encoding="utf-8"))

    def test_add_refuses_a_second_row_for_the_same_merge_and_change_in_a_short_or_long_hash(self) -> None:
        add = ("add", "--production", DOC, "--list", str(self.path), "--change", "Tabs", "--check", "Look", "--see", "It")
        self.assertEqual(run(*add, "--merged", "216c4e1")[:2], (0, "C81\n"))
        before = self.path.read_text(encoding="utf-8")
        for merged in ("216c4e1d", "216c4e1"):
            with self.subTest(merged=merged):
                code, output, error = run(*add, "--merged", merged)
                self.assertEqual((code, output), (1, ""))
                self.assertIn(f"C81 already holds that change from merge {merged}", error)
                self.assertEqual(self.path.read_text(encoding="utf-8"), before)
        # Another merge of the same change, and another change of the same merge, are new rows.
        self.assertEqual(run(*add, "--merged", "9e03c9ad")[:2], (0, "C82\n"))
        self.assertEqual(run(*add[:-6], "--change", "Spaces", "--check", "Look", "--see", "It", "--merged", "216c4e1d")[:2],
                         (0, "C83\n"))

    def test_a_style_productions_change_is_credited_to_its_parent(self) -> None:
        parent = self.path.parent / "parent-production.md"
        _ = parent.write_text("- **Merge branch:** main\n", encoding="utf-8")
        style = self.path.parent / "parent-style-production.md"
        _ = style.write_text(f"- **Kind:** style\n- **Parent production:** {parent}\n", encoding="utf-8")
        self.assertEqual(change_review.credited(style), parent)
        self.assertEqual(change_review.credited(parent), parent)
        rows = change_review.append_rows(self.path, [Visible("A", "B", "C")], date(2026, 10, 27), "216c4e1d", style,
                                         owners=[style])
        self.assertTrue(rows[0].endswith("| parent |"), rows)
        rows = change_review.append_rows(self.path, [Visible("D", "B", "C")], date(2026, 10, 27), "", style)
        self.assertTrue(rows[0].endswith("| parent |"), rows)

    def test_add_holds_each_folder_by_the_new_row(self) -> None:
        held = self.path.parent / "shown-repo"
        held.mkdir()
        with mock.patch.dict(os.environ, {scratch.HOLDS_ENV: str(self.path.parent / "holds.json")}):
            code, output, _ = run("add", "--production", DOC, "--list", str(self.path), "--change", "A", "--check", "B",
                                  "--see", "C", "--hold", str(held))
            self.assertEqual(code, 0)
            row = output.splitlines()[0]
            self.assertEqual(scratch.holds()[os.path.realpath(held)]["task"], f"row:{change_review._owner(self.path)}-{row}")  # pyright: ignore[reportPrivateUsage]

    def test_back_to_back_adds_in_one_shell_each_append_the_next_number(self) -> None:
        changes = [Visible("cargo-handler nests a showrunner | inside its parent", "open `cargo-handler` on natedev",
                           "the showrunner tile sits inside its parent"),
                   Visible("tile titles carry the role: showrunner, unit director", "read the tile titles",
                           "each title names its role"),
                   Visible("cargo-tile no longer panics at startup", "start it in a natedev terminal",
                           "~/.config/cargo-tile/panic.log gains nothing"),
                   Visible("cargo-berth gives each phase its own reservation", "run `cargo-berth board | grep phase`",
                           "one line per phase")]
        script = str(Path(change_review.__file__))
        for path, first in ((self.fresh, 1), (self.path, 81)):
            numbers = [subprocess.run([sys.executable, script, "add", "--production", DOC, "--list", str(path),
                                       "--change", item.change, "--check", item.check, "--see", item.see,
                                       "--merged", "4ad20245"],
                                      capture_output=True, text=True, check=True).stdout.strip()
                       for item in changes]
            self.assertEqual(numbers, [f"C{number}" for number in range(first, first + len(changes))])
            rows = [row for row in change_review.pending(path) if row["#"] in numbers]
            self.assertEqual([(row["#"], row["Change"], row[change_review.CHECK]) for row in rows],
                             [(number, item.change, item.how()) for number, item in zip(numbers, changes)])

    def test_add_refuses_an_empty_or_multi_line_part_and_writes_nothing(self) -> None:
        for change, check, see in (("", "Look", "It"), ("A", " ", "It"), ("A\nB", "Look", "It"), ("A", "Look", "")):
            code, _, error = run("add", "--production", DOC, "--list", str(self.path), "--change", change, "--check", check,
                                 "--see", see)
            self.assertEqual(code, 1)
            self.assertIn("needs one line of words", error)
        self.assertEqual(self.path.read_text(encoding="utf-8"), LIST)

    def test_add_without_see_is_refused_and_writes_nothing(self) -> None:
        code, output, error = run("add", "--production", DOC, "--list", str(self.path), "--change", "A", "--check", "Look")
        self.assertEqual((code, output), (1, ""))
        self.assertIn("--see needs one line of words", error)
        self.assertEqual(self.path.read_text(encoding="utf-8"), LIST)

    def test_edit_rewrites_one_row_in_place_and_keeps_its_id_merged_cell_and_place(self) -> None:
        self.assertEqual(run("edit", "--production", DOC, "--list", str(self.path), "--row", "C76",
                             "--change", "Panels | shields stay", "--check", "Run `hana` in a natedev terminal",
                             "--see", "Each panel keeps its shield"),
                         (0, "| C76 | Panels \\| shields stay | 2026-10-14 | not yet | Run `hana` in a natedev terminal. "
                          + "You see: Each panel keeps its shield |\n", ""))
        # The write stamps frontmatter above the list; every line below it is the list with the one row changed.
        self.assertEqual(self.path.read_text(encoding="utf-8").split("\n---\n\n", 1)[1], LIST.replace(
            "| C76 | Panels keep shields | 2026-10-14 | not yet | Launch Hana |",
            "| C76 | Panels \\| shields stay | 2026-10-14 | not yet | Run `hana` in a natedev terminal. "
            + "You see: Each panel keeps its shield |"))

    def test_edit_refuses_a_row_not_to_review_and_a_missing_part(self) -> None:
        for row in ("C70", "C99", "76"):
            code, output, error = run("edit", "--production", DOC, "--list", str(self.path), "--row", row,
                                      "--change", "A", "--check", "B", "--see", "C")
            self.assertEqual((code, output), (1, ""), row)
            self.assertEqual(len(error.splitlines()), 1, row)
        self.assertIn("C99 is not on the list to review", run("edit", "--production", DOC, "--list", str(self.path),
                                                              "--row", "C99", "--change", "A", "--check", "B",
                                                              "--see", "C")[2])
        self.assertIn("--see needs one line of words", run("edit", "--production", DOC, "--list", str(self.path),
                                                           "--row", "C76", "--change", "A", "--check", "B")[2])
        self.assertEqual(self.path.read_text(encoding="utf-8"), LIST)

    def test_show_prints_only_the_first_row_to_review_and_leaves_the_list_as_it_is(self) -> None:
        self.assertEqual(run("show", "--list", str(self.path)), (0, (
            "C76 Panels keep shields\n  How to check: Launch Hana\n  Merged: 2026-10-14\n  On the Mac build: not yet\n\n"
            + "1 more to review\n"), ""))
        self.assertEqual(self.path.read_text(encoding="utf-8"), LIST)
        self.assertEqual(run("show", "--list", str(self.fresh)), (0, "nothing to review\n", ""))
        self.assertFalse(self.fresh.parent.exists())

    def test_the_most_urgent_row_shows_first_and_the_shown_row_stays_until_its_verdict(self) -> None:
        def add(*rank: str) -> str:
            code, out, error = run("add", "--production", DOC, "--list", str(self.fresh), "--change", f"change {len(rank)}",
                                   "--check", "B", "--see", "C", *rank)
            self.assertEqual((code, error), (0, ""))
            return out.strip()

        def shown() -> str:
            return run("show", "--list", str(self.fresh))[1].split(" ", 1)[0]

        normal = add()
        self.assertEqual(shown(), normal)
        blocks = add("--urgency", "blocks")
        soon = add("--urgency", "soon", "--due", "2026-10-22")
        # The shown row stays current while a more urgent one waits.
        self.assertEqual(shown(), normal)
        text = self.fresh.read_text(encoding="utf-8")
        self.assertLess(text.index(f"| {normal} |"), text.index(f"| {blocks} |"))
        self.assertLess(text.index(f"| {blocks} |"), text.index(f"| {soon} |"))
        self.assertIn(f"| {soon} | change 4 | ", text)
        self.assertIn("| soon, due 2026-10-22 |", text)
        _ = run("verdict", "--production", DOC, "--list", str(self.fresh), "--row", normal, "--verdict", "works")
        self.assertEqual(shown(), blocks)
        # The user's mark takes the slot after the shown row.
        self.assertEqual(run("mark-important", "--production", DOC, "--list", str(self.fresh), "--row", soon)[0], 0)
        self.assertIn(f"| {soon} | change 4 |", self.fresh.read_text(encoding="utf-8"))
        self.assertEqual(shown(), blocks)
        _ = run("verdict", "--production", DOC, "--list", str(self.fresh), "--row", blocks, "--verdict", "works")
        self.assertEqual(shown(), soon)

    def test_a_row_waits_for_its_prerequisite_and_a_bad_level_is_refused(self) -> None:
        _ = run("add", "--production", DOC, "--list", str(self.path), "--change", "A", "--check", "B", "--see", "C",
                "--urgency", "blocks", "--after", "C77")
        text = self.path.read_text(encoding="utf-8")
        self.assertLess(text.index("| C77 |"), text.index("| C81 |"))
        self.assertLess(text.index("| C81 |"), text.index("| C76 |"))
        self.assertIn("| Urgency | Production |\n| --- | --- | --- | --- | --- | --- | --- |", text)
        self.assertIn("| C76 | Panels keep shields | 2026-10-14 | not yet | Launch Hana | |", text)
        self.assertEqual(run("add", "--production", DOC, "--list", str(self.path), "--change", "A", "--check", "B",
                             "--see", "C", "--urgency", "soon")[0], 1)
        self.assertEqual(run("add", "--production", DOC, "--list", str(self.path), "--change", "A", "--check", "B",
                             "--see", "C", "--after", "later")[0], 1)

    def test_a_verdict_moves_the_row_to_the_top_of_reviewed(self) -> None:
        self.assertEqual(change_review.record_verdict(self.path, "C76", "Works, thanks", date(2026, 10, 15), PRODUCTION),
                         change_review.VerdictRecorded(change_review.RecordedMerge("2026-10-14")))
        text = self.path.read_text(encoding="utf-8")
        self.assertNotIn("| C76 | Panels keep shields | 2026-10-14", text.split("## Reviewed")[0])
        self.assertIn("| # | Change | Verdict | Date | Merged |", text)
        self.assertIn("| --- | --- | --- | --- | --- |\n| C76 | Panels keep shields | Works, thanks | 2026-10-15 | 2026-10-14 |\n| C70 |", text)

    def test_edit_and_verdict_replacement_failures_keep_the_list_and_rerun_once(self) -> None:
        for action in ("edit", "verdict"):
            with self.subTest(action=action):
                _ = self.path.write_text(LIST, encoding="utf-8")
                before = self.path.read_bytes()
                options = (("--change", "Updated panels", "--check", "Open the panel", "--see", "Updated shield")
                           if action == "edit" else ("--verdict", "works"))
                command = (action, "--production", DOC, "--list", str(self.path), "--row", "C76", *options)
                real_replace = os.replace

                def interrupted(source: Path, destination: Path) -> None:
                    if Path(destination) == self.path:
                        raise OSError("list replacement stopped")
                    real_replace(source, destination)

                with mock.patch.object(os, "replace", side_effect=interrupted):
                    code, _, error = run(*command)
                self.assertEqual(code, 1)
                self.assertIn("list replacement stopped", error)
                self.assertEqual(self.path.read_bytes(), before)
                self.assertEqual(run(*command)[0], 0)
                self.assertEqual(self.path.read_text().count("| C76 |"), 1)
                self.assertEqual([row["#"] for row in change_review.pending(self.path)],
                                 ["C76", "C77"] if action == "edit" else ["C77"])

    def test_verdict_rerun_finishes_scratch_and_replays_merged_result(self) -> None:
        for verdict in ("works", "still missing the shield"):
            with self.subTest(verdict=verdict), mock.patch.dict(os.environ, {
                scratch.ROOT_ENV: str(self.path.parent / "scratch"),
                scratch.HOLDS_ENV: str(self.path.parent / "holds.json"),
            }):
                _ = self.path.write_text(LIST, encoding="utf-8")
                task = f"row:{self.path.stem}-C76"
                folder = scratch.new(task, "verdict")
                _ = change_review.hold_for_row(self.path, "C76", [str(folder)])
                command = ("verdict", "--production", DOC, "--list", str(self.path), "--row", "C76", "--verdict", verdict)
                with mock.patch.object(change_review, "end_row_scratch", side_effect=KeyboardInterrupt):
                    with self.assertRaises(KeyboardInterrupt):
                        _ = run(*command)
                self.assertTrue(folder.is_dir())
                self.assertIn(os.path.realpath(folder), scratch.holds())
                self.assertNotIn("C76", [row["#"] for row in change_review.pending(self.path)])
                before = self.path.read_bytes()
                code, output, error = run(*command)
                self.assertEqual((code, error), (0, ""))
                self.assertIn("C76 already has this verdict (", output)
                self.assertIn("C76 landed\n" if verdict == "works" else
                              "C76 returns to its unit (merged 2026-10-14)\n", output)
                self.assertIn(f"removed {folder}\n", output)
                self.assertFalse(folder.exists())
                self.assertNotIn(os.path.realpath(folder), scratch.holds())
                self.assertEqual(self.path.read_bytes(), before)
                self.assertEqual(self.path.read_text().split("## Reviewed")[1].count("| C76 |"), 1)

    def test_conflicting_verdict_keeps_reviewed_row_and_finishes_scratch(self) -> None:
        with mock.patch.dict(os.environ, {scratch.ROOT_ENV: str(self.path.parent / "scratch"),
                                          scratch.HOLDS_ENV: str(self.path.parent / "holds.json")}):
            folder = scratch.new(f"row:{self.path.stem}-C76", "conflict")
            _ = change_review.hold_for_row(self.path, "C76", [str(folder)])
            with mock.patch.object(change_review, "end_row_scratch", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    _ = run("verdict", "--production", DOC, "--list", str(self.path), "--row", "C76", "--verdict", "works")
            before = self.path.read_bytes()
            code, output, error = run("verdict", "--production", DOC, "--list", str(self.path), "--row", "C76", "--verdict", "still broken")
            self.assertEqual(code, 1)
            self.assertIn('C76 has the verdict "works" from ', error)
            self.assertIn("this verdict is not recorded", error)
            self.assertIn(f"removed {folder}", output)
            self.assertEqual(self.path.read_bytes(), before)
            self.assertFalse(folder.exists())
            self.assertNotIn(os.path.realpath(folder), scratch.holds())

    def test_missing_merge_is_explicit_for_new_and_earlier_reviewed_rows(self) -> None:
        _ = self.path.write_text(LIST.replace("Panels keep shields | 2026-10-14", "Panels keep shields |"))
        recorded = change_review.record_verdict(self.path, "C76", "not yet", date(2026, 10, 15), PRODUCTION)
        self.assertEqual(recorded, change_review.VerdictRecorded(change_review.MergeNotRecorded()))
        repeated = change_review.record_verdict(self.path, "C76", "not yet", date(2026, 10, 16), PRODUCTION)
        self.assertIsInstance(repeated, change_review.VerdictAlreadyRecorded)
        if isinstance(repeated, change_review.VerdictAlreadyRecorded):
            self.assertIsInstance(repeated.merged, change_review.MergeNotRecorded)
        code, output, error = run("verdict", "--production", DOC, "--list", str(self.path), "--row", "C76", "--verdict", "not yet")
        self.assertEqual((code, error), (0, ""))
        self.assertIn("C76 returns to its unit (merged not recorded)", output)
        _ = self.path.write_text(LIST)
        earlier = change_review.record_verdict(self.path, "C70", "works", date(2026, 10, 16), PRODUCTION)
        self.assertIsInstance(earlier, change_review.VerdictAlreadyRecorded)
        if isinstance(earlier, change_review.VerdictAlreadyRecorded):
            self.assertIsInstance(earlier.merged, change_review.MergeNotRecorded)
        self.assertEqual(self.path.read_text(), LIST)

    def test_works_or_fine_lands_and_any_other_verdict_returns_to_the_unit(self) -> None:
        self.assertEqual(run("verdict", "--production", DOC, "--list", str(self.path), "--row", "C76", "--verdict", "fine"),
                         (0, "C76 landed\n", ""))
        self.assertEqual(run("verdict", "--production", DOC, "--list", str(self.path), "--row", "C77", "--verdict", "still the old font | x"),
                         (0, "C77 returns to its unit (merged 2026-10-14)\n", ""))
        self.assertIn("| C77 | Fonts redraw | still the old font \\| x |", self.path.read_text(encoding="utf-8"))
        self.assertTrue(change_review.lands("Works: it does"))
        self.assertFalse(change_review.lands("not fine"))
        for verdict in ("okay c17 works", "ok fine", "yes works", "Works: it does"):
            self.assertTrue(change_review.lands(verdict), verdict)
        for verdict in ("still pushes", "it works but still pushes", "no, broken", "doesn't work"):
            self.assertFalse(change_review.lands(verdict), verdict)

    def test_a_verdict_removes_the_rows_scratch_folders_either_way(self) -> None:
        demo = scratch.new("row:natedev-C1", "demo", visible=True)
        copy = scratch.new("row:natedev-C2", "copy")
        kept = scratch.new("row:natedev-C3")
        _ = change_review.append_rows(self.fresh, [Visible(name, "B", "C") for name in "XYZ"], date(2026, 10, 15), "",
                                      PRODUCTION)
        self.assertEqual(run("verdict", "--production", DOC, "--list", str(self.fresh), "--row", "C1", "--verdict", "works"),
                         (0, f"C1 landed\nremoved {demo}\n", ""))
        self.assertEqual(run("verdict", "--production", DOC, "--list", str(self.fresh), "--row", "C2", "--verdict", "no"),
                         (0, f"C2 returns to its unit (merged 2026-10-15)\nremoved {copy}\n", ""))
        self.assertFalse(demo.exists() or copy.exists())
        self.assertTrue(kept.is_dir())

    def test_a_verdict_for_a_row_not_to_review_is_refused(self) -> None:
        for row in ("C99", "76"):
            code, output, error = run("verdict", "--production", DOC, "--list", str(self.path), "--row", row, "--verdict", "works")
            self.assertEqual((code, output), (1, ""), row)
            self.assertEqual(len(error.splitlines()), 1, row)
        self.assertEqual(self.path.read_text(encoding="utf-8"), LIST)

    def attached_lists(self) -> Path:
        earlier = self.path.with_name("change-review - child.md")
        _ = self.path.write_text(LIST.replace("C76", "C9"), encoding="utf-8")
        _ = earlier.write_text(LIST.replace("C76", "C9").replace("Panels keep shields", "Earlier change"),
                               encoding="utf-8")
        _ = self.enterContext(mock.patch.object(change_review, "LISTS", self.path.parent))
        _ = self.enterContext(mock.patch.object(change_review, "production_list", return_value=self.path))
        return earlier

    def test_list_named_verdict_changes_only_that_list_and_ends_its_scratch(self) -> None:
        earlier = self.attached_lists()
        before = self.path.read_bytes()
        scratch_folder = scratch.new("row:child-C9", "earlier")
        current_scratch = scratch.new("row:current-C9", "current")
        self.assertEqual(run("verdict", "--production", DOC, "--row", "child/C9", "--verdict", "works"),
                         (0, f"child/C9 landed\nremoved {scratch_folder}\n", ""))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertNotIn("C9", {row["#"] for row in change_review.pending(earlier)})
        self.assertIn("| C9 | Earlier change | works |", earlier.read_text())
        self.assertNotIn("child/C9", earlier.read_text())
        self.assertFalse(scratch_folder.exists())
        self.assertTrue(current_scratch.exists())

    def test_list_named_important_mark_changes_only_that_lists_local_row(self) -> None:
        earlier = self.attached_lists()
        before = self.path.read_bytes()
        code, output, error = run("mark-important", "--production", DOC, "--row", "child/C9")
        self.assertEqual((code, error), (0, ""))
        self.assertTrue(output.startswith("| C9 | Earlier change |"))
        self.assertEqual(self.path.read_bytes(), before)
        entry = next(entry for entry in change_review.row_entries(change_review.pending(earlier)) if entry.key == "C9")
        self.assertEqual(entry.urgency.level, urgency.USER)
        self.assertNotIn("child/C9", earlier.read_text())

    def test_list_named_edit_accepts_a_matching_list_and_keeps_local_row_text(self) -> None:
        earlier = self.attached_lists()
        before = self.path.read_bytes()
        code, output, error = run("edit", "--production", DOC, "--list", str(earlier), "--row", "child/C9",
                                  "--change", "Revised change", "--check", "Run it", "--see", "Revised result")
        self.assertEqual((code, error), (0, ""))
        self.assertTrue(output.startswith("| C9 | Revised change |"))
        self.assertEqual(self.path.read_bytes(), before)
        row = next(row for row in change_review.pending(earlier) if row["#"] == "C9")
        self.assertEqual((row["Change"], row[change_review.CHECK]), ("Revised change", "Run it. You see: Revised result"))
        self.assertNotIn("child/C9", earlier.read_text())

    def test_list_named_row_refuses_a_different_explicit_list_without_writes(self) -> None:
        earlier = self.attached_lists()
        before = {path: path.read_bytes() for path in (self.path, earlier)}
        for action, arguments in (("verdict", ("--verdict", "works")), ("mark-important", ()),
                                  ("edit", ("--change", "Changed", "--check", "Run it", "--see", "A result"))):
            with self.subTest(action=action):
                code, output, error = run(action, "--production", DOC, "--list", str(self.path),
                                          "--row", "child/C9", *arguments)
                self.assertEqual((code, output), (1, ""))
                self.assertEqual(len(error.splitlines()), 1)
                self.assertIn("differs from --list", error)
                self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_a_write_waits_for_the_lock_and_concurrent_writes_keep_every_row(self) -> None:
        held = os.open(self.path.parent, os.O_RDONLY)
        fcntl.flock(held, fcntl.LOCK_EX)
        writers = [threading.Thread(target=change_review.append_rows,
                                    args=(self.path, [Visible(f"Row {index}", "Look", "It")], date(2026, 10, 15), "", PRODUCTION))
                   for index in range(8)]
        blocked = threading.Event()
        real_flock = fcntl.flock

        def observe_lock(descriptor: int, operation: int) -> None:
            if operation == fcntl.LOCK_EX:
                try:
                    real_flock(descriptor, operation | fcntl.LOCK_NB)
                except BlockingIOError:
                    blocked.set()
                else:
                    return
            real_flock(descriptor, operation)

        with mock.patch.object(fcntl, "flock", side_effect=observe_lock):
            try:
                for writer in writers:
                    writer.start()
                self.assertTrue(blocked.wait(TEST_HANG_GUARD_SECS), "writer did not reach the held lock")
                self.assertTrue(writers[0].is_alive())
                self.assertEqual(self.path.read_text(encoding="utf-8"), LIST)
            finally:
                os.close(held)
                for writer in writers:
                    writer.join(TEST_HANG_GUARD_SECS)
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


class CommitTest(unittest.TestCase):
    """Each write commits the list alone when it sits in a git work tree, in a scratch repository."""

    repo: Path = Path()
    path: Path = Path()

    def git(self, *arguments: str) -> str:
        return subprocess.run(["git", "-C", str(self.repo), *arguments], capture_output=True, text=True,
                              check=True).stdout.strip()

    @override
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.repo = Path(directory.name) / "vault"
        self.repo.mkdir()
        _ = self.git("init", "-q", "-b", "main")
        for key, value in (("user.name", "t"), ("user.email", "t@t"), ("commit.gpgsign", "false")):
            _ = self.git("config", key, value)
        _ = (self.repo / "note.md").write_text("note\n", encoding="utf-8")
        _ = self.git("add", "note.md")
        _ = self.git("commit", "-q", "-m", "note")
        self.path = self.repo / "change-review" / "change-review - hana.md"

    def last(self) -> tuple[str, list[str]]:
        """The newest commit's subject and the files it changes."""
        return self.git("log", "-1", "--format=%s"), self.git("show", "--name-only", "--format=", "HEAD").splitlines()

    def test_add_commits_a_new_list_and_then_the_existing_one(self) -> None:
        self.assertEqual(run("add", "--production", DOC, "--list", str(self.path), "--change", "A", "--check", "B", "--see", "C"), (0, "C1\n", ""))
        self.assertEqual(self.last(), ("change-review: hana add C1", ["change-review/change-review - hana.md"]))
        self.assertEqual(run("add", "--production", DOC, "--list", str(self.path), "--change", "C", "--check", "D", "--see", "E", "--merged", "4ad20245"),
                         (0, "C2\n", ""))
        self.assertEqual(self.last(), ("change-review: hana row C2 from 4ad20245", ["change-review/change-review - hana.md"]))
        _ = change_review.append_rows(self.path, [Visible("E", "F", "S"), Visible("G", "H", "S")], date(2026, 10, 15), "61b65bbf", PRODUCTION)
        self.assertEqual(self.last(), ("change-review: hana rows C3–C4 from 61b65bbf", ["change-review/change-review - hana.md"]))
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_a_verdict_commits_the_list(self) -> None:
        _ = run("add", "--production", DOC, "--list", str(self.path), "--change", "A", "--check", "B", "--see", "C")
        self.assertEqual(run("verdict", "--production", DOC, "--list", str(self.path), "--row", "C1", "--verdict", "works"),
                         (0, "C1 landed\n", ""))
        self.assertEqual(self.last(), ("change-review: hana verdict C1", ["change-review/change-review - hana.md"]))
        self.assertIn("| C1 | A | works |", self.git("show", "HEAD:change-review/change-review - hana.md"))

    def test_unchanged_edit_keeps_list_bytes_timestamp_and_git_history(self) -> None:
        _ = change_review.append_rows(self.path, [GRID], date(2026, 10, 15), "216c4e1d", PRODUCTION)
        before = self.path.read_bytes(), self.path.stat().st_mtime_ns, self.git("rev-parse", "HEAD")
        _ = change_review._edit(self.path, PRODUCTION, date(2026, 10, 20),  # pyright: ignore[reportPrivateUsage]
                               lambda lines: len(lines), lambda count: f"unchanged {count}")
        self.assertEqual((self.path.read_bytes(), self.path.stat().st_mtime_ns, self.git("rev-parse", "HEAD")), before)
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_verdict_rerun_commits_the_list_saved_before_the_first_commit_stopped(self) -> None:
        _ = change_review.append_rows(self.path, [GRID], date(2026, 10, 15), "216c4e1d", PRODUCTION)
        previous = self.git("rev-parse", "HEAD")
        with (mock.patch.object(change_review, "_commit", side_effect=KeyboardInterrupt),
              self.assertRaises(KeyboardInterrupt)):
            _ = run("verdict", "--production", DOC, "--list", str(self.path), "--row", "C1", "--verdict", "works")
        saved = self.path.read_bytes(), self.path.stat().st_mtime_ns
        self.assertNotEqual(self.git("status", "--porcelain", "--", str(self.path)), "")
        self.assertEqual(self.git("rev-parse", "HEAD"), previous)
        code, printed, error = run("verdict", "--production", DOC, "--list", str(self.path),
                                   "--row", "C1", "--verdict", "works")
        self.assertEqual((code, error), (0, ""))
        self.assertIn("already has this verdict", printed)
        self.assertEqual((self.path.read_bytes(), self.path.stat().st_mtime_ns), saved)
        self.assertNotEqual(self.git("rev-parse", "HEAD"), previous)
        self.assertEqual(self.git("rev-list", "--count", f"{previous}..HEAD"), "1")
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertIn("| C1 | " + GRID.change + " | works |", self.git("show", "HEAD:change-review/change-review - hana.md"))

    def test_an_edit_commits_the_list(self) -> None:
        _ = run("add", "--production", DOC, "--list", str(self.path), "--change", "A", "--check", "B", "--see", "C")
        _ = run("edit", "--production", DOC, "--list", str(self.path), "--row", "C1", "--change", "D", "--check", "E",
                "--see", "F")
        self.assertEqual(self.last(), ("change-review: hana edit C1", ["change-review/change-review - hana.md"]))
        self.assertIn("| C1 | D |", self.git("show", "HEAD:change-review/change-review - hana.md"))
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_another_staged_file_stays_staged_and_uncommitted(self) -> None:
        _ = (self.repo / "note.md").write_text("staged edit\n", encoding="utf-8")
        _ = (self.repo / "draft.md").write_text("new\n", encoding="utf-8")
        _ = self.git("add", "note.md", "draft.md")
        _ = run("add", "--production", DOC, "--list", str(self.path), "--change", "A", "--check", "B", "--see", "C")
        self.assertEqual(self.last(), ("change-review: hana add C1", ["change-review/change-review - hana.md"]))
        self.assertEqual(self.git("diff", "--cached", "--name-only").splitlines(), ["draft.md", "note.md"])
        self.assertEqual(self.git("show", ":note.md"), "staged edit")

    def test_a_list_outside_any_repository_is_written_and_not_committed(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        outside = Path(directory.name) / "change-review" / "hana.md"
        self.assertEqual(run("add", "--production", DOC, "--list", str(outside), "--change", "A", "--check", "B", "--see", "C"), (0, "C1\n", ""))
        self.assertIn("| C1 | A |", outside.read_text(encoding="utf-8"))
        self.assertEqual(self.git("rev-list", "--count", "HEAD"), "1")

    def test_a_held_index_lock_warns_writes_and_the_next_add_commits_both_rows(self) -> None:
        lock = self.repo / ".git" / "index.lock"
        _ = lock.write_text("", encoding="utf-8")
        code, output, error = run("add", "--production", DOC, "--list", str(self.path), "--change", "A", "--check", "B", "--see", "C")
        self.assertEqual((code, output), (0, "C1\n"))
        self.assertEqual(len(error.splitlines()), 1)
        self.assertIn(f"change review: {self.path} written but not committed: ", error)
        self.assertIn("; the next write commits it", error)
        self.assertIn("| C1 | A |", self.path.read_text(encoding="utf-8"))
        self.assertEqual(self.git("rev-list", "--count", "HEAD"), "1")
        lock.unlink()
        self.assertEqual(run("add", "--production", DOC, "--list", str(self.path), "--change", "C", "--check", "D", "--see", "E"), (0, "C2\n", ""))
        self.assertEqual(self.last(), ("change-review: hana add C2", ["change-review/change-review - hana.md"]))
        committed = self.git("show", "HEAD:change-review/change-review - hana.md")
        self.assertIn("| C1 | A |", committed)
        self.assertIn("| C2 | C |", committed)
        self.assertEqual(self.git("status", "--porcelain"), "")


class ProductionMergeCommitsTest(unittest.TestCase):
    repo: Path = Path()
    own_doc: Path = Path()
    other_doc: Path = Path()
    previous: str = ""
    foreign: str = ""

    def git(self, *arguments: str, cwd: Path | None = None) -> str:
        environment = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
                       "GIT_AUTHOR_DATE": "2026-10-29T12:00:00Z", "GIT_COMMITTER_DATE": "2026-10-29T12:00:00Z"}
        return subprocess.run(["git", "-C", str(cwd or self.repo), "-c", "user.name=t",
                               "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *arguments],
                              capture_output=True, text=True, check=True, env=environment).stdout.strip()

    def commit(self, name: str, message: str) -> str:
        _ = (self.repo / name).write_text(message, encoding="utf-8")
        _ = self.git("add", name)
        _ = self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    @override
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        origin, seed = root / "origin.git", root / "seed"
        seed.mkdir()
        _ = self.git("init", "--bare", str(origin), cwd=root)
        _ = self.git("init", "-q", "-b", "own-merge", cwd=seed)
        self.repo = seed
        self.previous = self.commit("base.py", "base")
        _ = self.git("remote", "add", "origin", str(origin))
        _ = self.git("push", "-q", "origin", "own-merge")
        _ = self.git("clone", "-q", "-b", "own-merge", str(origin), str(root / "clone"), cwd=root)
        self.repo = root / "clone"
        _ = self.git("checkout", "-q", "-b", "unit")
        _ = self.git("checkout", "-q", "-b", "other-merge")
        self.foreign = self.commit("foreign.py", "checkpoint(other): already reviewed in its production")
        _ = self.git("push", "-q", "origin", "other-merge")
        _ = self.git("checkout", "-q", "unit")
        _ = self.git("merge", "-q", "--no-ff", "-m", "merge other production", "origin/other-merge")
        self.own_doc, self.other_doc = root / "own-production.md", root / "other-production.md"
        for doc, branch in ((self.own_doc, "own-merge"), (self.other_doc, "other-merge")):
            _ = doc.write_text(f"- **Repository:** `{self.repo}`\n- **Merge branch:** `{branch}`\n"
                               + f"- **Change review:** `{root / 'review.md'}`\n", encoding="utf-8")
        _ = self.enterContext(mock.patch.object(change_review, "showrunner_of",
                                               side_effect=change_review.ChangeReviewError("not running")))

    def commits(self) -> list[change_review.Commit]:
        return change_review.unit_commits(self.repo, "HEAD", self.previous, "own-merge",
                                          productions=lambda: (self.own_doc, self.other_doc))

    def test_imported_production_commits_leave_only_the_units_missing_line(self) -> None:
        own = self.commit("unit.py", "checkpoint(unit): own work")
        refused = change_review.missing(self.commits())
        self.assertEqual(len(refused), 1)
        self.assertTrue(refused[0].startswith(own[:8]))
        self.assertNotIn(self.foreign[:8], refused[0])

    def test_imported_production_commits_need_no_cover_when_unit_has_its_line(self) -> None:
        own = self.commit("unit.py", f"checkpoint(unit): own work\n\n{SEE}")
        commits = self.commits()
        self.assertEqual([commit.sha for commit in commits], [own])
        self.assertEqual(change_review.missing(commits), [])

    def test_production_without_review_rows_keeps_its_commits_checked(self) -> None:
        _ = self.other_doc.write_text(f"- **Repository:** `{self.repo}`\n- **Merge branch:** `other-merge`\n")
        _ = self.commit("unit.py", f"checkpoint(unit): own work\n\n{SEE}")
        self.assertEqual([item.split()[0] for item in change_review.missing(self.commits())], [self.foreign[:8]])

    def test_production_in_another_repository_keeps_its_commits_checked(self) -> None:
        other = self.repo.parent / "unrelated"
        other.mkdir()
        _ = self.git("init", "-q", cwd=other)
        _ = self.other_doc.write_text(self.other_doc.read_text().replace(str(self.repo), str(other)))
        _ = self.commit("unit.py", f"checkpoint(unit): own work\n\n{SEE}")
        self.assertEqual([item.split()[0] for item in change_review.missing(self.commits())], [self.foreign[:8]])

    def test_unreadable_production_doc_keeps_the_existing_exclusions(self) -> None:
        own = self.commit("unit.py", f"checkpoint(unit): own work\n\n{SEE}")
        with mock.patch.object(showrunners, "timer_docs", return_value={
            "own": self.own_doc, "other": self.other_doc, "missing": self.repo.parent / "missing.md",
        }):
            commits = change_review.unit_commits(self.repo, "HEAD", self.previous, "own-merge")
        self.assertEqual([commit.sha for commit in commits], [own])
        self.assertEqual(change_review.missing(commits), [])

    def test_linked_worktree_excludes_commits_reviewed_in_the_common_repository(self) -> None:
        checkout = self.repo.parent / "unit-checkout"
        _ = self.git("worktree", "add", "-q", "-b", "linked-unit", str(checkout), "unit")
        self.repo = checkout
        own = self.commit("unit.py", f"checkpoint(unit): own work\n\n{SEE}")
        self.assertEqual([commit.sha for commit in self.commits()], [own])
        self.assertEqual(change_review.missing(self.commits()), [])

    def test_invalid_production_repository_keeps_valid_production_exclusions(self) -> None:
        own = self.commit("unit.py", f"checkpoint(unit): own work\n\n{SEE}")
        invalid_doc = self.repo.parent / "invalid-production.md"
        not_git = self.repo.parent / "not-git"
        not_git.mkdir()
        for repository in (self.repo.parent / "missing-repository", not_git):
            with self.subTest(repository=repository):
                _ = invalid_doc.write_text(f"- **Repository:** `{repository}`\n"
                                          + "- **Merge branch:** `invalid-merge`\n"
                                          + f"- **Change review:** `{self.repo.parent / 'review.md'}`\n",
                                          encoding="utf-8")
                commits = change_review.unit_commits(
                    self.repo, "HEAD", self.previous, "own-merge",
                    productions=lambda: (self.own_doc, invalid_doc, self.other_doc))
                self.assertEqual([commit.sha for commit in commits], [own])
                self.assertEqual(change_review.missing(commits), [])

    def test_incomplete_production_doc_keeps_valid_production_exclusions(self) -> None:
        own = self.commit("unit.py", f"checkpoint(unit): own work\n\n{SEE}")
        invalid_doc = self.repo.parent / "incomplete-production.md"
        _ = invalid_doc.write_text(f"- **Repository:** `{self.repo}`\n"
                                  + f"- **Change review:** `{self.repo.parent / 'review.md'}`\n",
                                  encoding="utf-8")
        commits = change_review.unit_commits(
            self.repo, "HEAD", self.previous, "own-merge",
            productions=lambda: (self.own_doc, invalid_doc, self.other_doc))
        self.assertEqual([commit.sha for commit in commits], [own])
        self.assertEqual(change_review.missing(commits), [])

    def test_registered_productions_are_read_by_default(self) -> None:
        own = self.commit("unit.py", f"checkpoint(unit): own work\n\n{SEE}")
        with mock.patch.object(showrunners, "timer_docs", return_value={"own": self.own_doc, "other": self.other_doc}):
            commits = change_review.unit_commits(self.repo, "HEAD", self.previous, "own-merge")
        self.assertEqual([commit.sha for commit in commits], [own])

    def test_repository_subdirectory_resolves_to_the_same_git_top_level(self) -> None:
        subdirectory = self.repo / "src"
        subdirectory.mkdir()
        _ = self.other_doc.write_text(self.other_doc.read_text().replace(str(self.repo), str(subdirectory)))
        own = self.commit("unit.py", f"checkpoint(unit): own work\n\n{SEE}")
        self.assertEqual([commit.sha for commit in self.commits()], [own])

    def test_absent_origin_branch_does_not_exclude_its_local_commits(self) -> None:
        _ = self.git("update-ref", "-d", "refs/remotes/origin/other-merge")
        _ = self.commit("unit.py", f"checkpoint(unit): own work\n\n{SEE}")
        self.assertEqual([item.split()[0] for item in change_review.missing(self.commits())], [self.foreign[:8]])

    def test_unreadable_timer_registry_keeps_the_existing_exclusions(self) -> None:
        _ = self.commit("unit.py", f"checkpoint(unit): own work\n\n{SEE}")
        with mock.patch.object(showrunners, "timer_docs", side_effect=OSError("cannot read production registry")):
            commits = change_review.unit_commits(self.repo, "HEAD", self.previous, "own-merge")
        self.assertEqual([item.split()[0] for item in change_review.missing(commits)], [self.foreign[:8]])


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
        self.assertEqual(change_review.visible(commits), [GRID])

    def test_a_markdown_only_checkpoint_needs_no_line_but_its_line_is_read(self) -> None:
        _ = self.commit("a.md", "checkpoint(u): doc without a line")
        checkpoint = self.commit("b.md", f"checkpoint(u): doc with a line\n\n{SEE}")
        commits = change_review.unit_commits(self.repo, checkpoint, self.previous, "merge")
        self.assertEqual(change_review.missing(commits), [])
        self.assertEqual(change_review.visible(commits), [GRID])

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
        self.assertEqual(change_review.visible(commits), [GRID])

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


    def merged(self, branch: str, message: str) -> str:
        """Merge `branch` into the merge branch, move origin/merge there and return the merge's short hash."""
        _ = self.git("checkout", "-q", "merge")
        _ = self.git("merge", "-q", "--no-ff", "-m", message, branch)
        _ = self.git("update-ref", "refs/remotes/origin/merge", "HEAD")
        return self.git("rev-parse", "--short=8", "HEAD")

    def test_drift_knows_a_settled_merge_by_its_reviewed_row_and_leaves_out_another_trees_checkpoint(self) -> None:
        _ = self.commit("a.py", f"checkpoint(u): grid\n\n{SEE}")
        short = self.merged("unit", "Merge u")
        self.assertEqual(len(change_review.drift(self.repo, "merge", LIST, "2026-10-01")), 1)
        # A reviewed row keeps no Merged cell; its change still names the merge.
        settled = LIST + f"\n## Reviewed\n\n| # | Change | Verdict | Date |\n| --- | --- | --- | --- |\n| C81 | {GRID.change} | works | 2026-10-16 |\n"
        self.assertEqual(change_review.drift(self.repo, "merge", settled, "2026-10-01"), [])
        # A merge of main brings in another production's checkpoint: that row is in another list.
        self.assertEqual(change_review.drift(self.repo, "merge", LIST, "2026-10-01", {"u"}), [])
        self.assertNotIn(short, LIST)

    def test_backfill_fills_older_rows_from_their_merges_and_names_a_row_it_cannot_trace(self) -> None:
        _ = self.commit("a.py", f"checkpoint(u): grid\n\n{SEE}")
        unit_merge = self.merged("unit", "Merge u")
        _ = self.git("checkout", "-q", "-b", "side", "merge")
        _ = self.commit("b.py", "a plain commit")
        side_merge = self.merged("side", "Merge side")
        folder = self.repo / "docs"
        folder.mkdir()
        top = folder / "top-production.md"
        _ = top.write_text(f"- **Repository:** {self.repo}\n- **Merge branch:** merge\n\n## Units\n\n", encoding="utf-8")
        kid = folder / "kid-production.md"
        _ = kid.write_text(f"- **Repository:** {self.repo}\n- **Merge branch:** kid\n\n## Units\n\n"
                           + "| Unit | Plan | Worktree | Branch |\n| --- | --- | --- | --- |\n"
                           + "| u-unit | docs/u-plan.md | /nowhere/u | `u` (deleted) |\n", encoding="utf-8")
        path = folder / "change-review - top.md"
        _ = path.write_text("## To review\n\n| # | Change | Merged | How to check |\n| --- | --- | --- | --- |\n"
                            + f"| C1 | {GRID.change} | 2026-10-15 · {unit_merge} | x |\n"
                            + f"| C2 | side change | 2026-10-15 · {side_merge} | x |\n"
                            + "| C3 | no hash | 2026-10-15 | x |\n\n"
                            + "## Reviewed\n\n| # | Change | Verdict | Date |\n| --- | --- | --- | --- |\n", encoding="utf-8")
        before = path.read_text(encoding="utf-8")
        tree = change_review.Tree([top, kid])
        expected = ({"C1": "kid", "C2": "top"}, {"C3": "no merge hash"})
        self.assertEqual(change_review.backfill(path, tree, write=False), expected)
        self.assertEqual(path.read_text(encoding="utf-8"), before)
        self.assertEqual(change_review.backfill(path, tree, write=True), expected)
        rows = {row["#"]: row[change_review.PRODUCTION] for row in change_review.pending(path)}
        self.assertEqual(rows, {"C1": "kid", "C2": "top", "C3": ""})
        # A second run finds nothing left to fill.
        self.assertEqual(change_review.backfill(path, tree, write=False), ({}, {"C3": "no merge hash"}))

    def test_backfill_credits_a_style_childs_hand_off_change_to_the_parent_that_merged_it(self) -> None:
        _ = self.commit("a.py", f"checkpoint(u): grid\n\n{SEE}")
        handoff = self.merged("unit", "Merge style hand-off")
        folder = self.repo / "docs"
        folder.mkdir()
        top = folder / "top-production.md"
        _ = top.write_text(f"- **Repository:** {self.repo}\n- **Merge branch:** merge\n\n## Units\n\n", encoding="utf-8")
        style = folder / "top-style-production.md"
        _ = style.write_text(f"- **Repository:** {self.repo}\n- **Merge branch:** style/main\n- **Kind:** style\n"
                             + f"- **Parent production:** {top}\n\n## Units\n\n"
                             + "| Unit | Plan | Worktree | Branch |\n| --- | --- | --- | --- |\n"
                             + "| u-unit | docs/u-plan.md | /nowhere/u | `u` |\n", encoding="utf-8")
        path = folder / "change-review - top.md"
        _ = path.write_text("## To review\n\n| # | Change | Merged | How to check |\n| --- | --- | --- | --- |\n"
                            + f"| C1 | {GRID.change} | 2026-10-15 · {handoff} | x |\n\n"
                            + "## Reviewed\n\n| # | Change | Verdict | Date |\n| --- | --- | --- | --- |\n", encoding="utf-8")
        self.assertEqual(change_review.backfill(path, change_review.Tree([top, style]), write=True), ({"C1": "top"}, {}))

    def test_a_root_merge_stamps_each_change_with_the_production_of_its_checkpoints_unit(self) -> None:
        folder = self.repo / "docs"
        folder.mkdir()
        top, kid = folder / "top-production.md", folder / "kid-production.md"
        _ = kid.write_text("## Units\n\n| Unit | Plan | Worktree | Branch |\n| --- | --- | --- | --- |\n"
                           + "| u-unit | docs/u-plan.md | /nowhere/u | u |\n", encoding="utf-8")
        _ = top.write_text("## Units\n\n", encoding="utf-8")
        commits = [change_review.Commit("1", "checkpoint(u-plan): grid", f"checkpoint(u-plan): grid\n\n{SEE}", ()),
                   change_review.Commit("2", "checkpoint(own): grid", f"checkpoint(own): grid\n\n{SEE}", ())]
        with mock.patch.object(change_review, "owner_docs", return_value=[top, kid]):
            self.assertEqual(change_review.visible_owners(commits, top), [top, kid])


if __name__ == "__main__":
    _ = unittest.main()
