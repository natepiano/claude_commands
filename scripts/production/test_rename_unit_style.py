"""Style unit identities, plans, tickets and restart records."""

from __future__ import annotations

import os
import shlex
import sqlite3
import sys
import unittest
from contextlib import closing
from pathlib import Path
from typing import final
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "style"))
import style_ledger
from scripts.style import unit_files
from scripts.production.staff_test_support import fixture_unit_location, recorded_showrunner

sys.path.insert(0, str(Path(__file__).resolve().parent))


from scripts.production.rename_unit_test_support import UnitRenameFixture, SLUG


@final
class UnitRenameStyleTests(UnitRenameFixture):

    def test_held_files_follow_staff_locations_when_worktree_cells_disagree(self) -> None:
        fixture = self.style_production()
        worktree, database = fixture[0], fixture[2]
        stale = self.root / "stale-names"
        _ = self.git("worktree", "add", "-b", "stale-names", str(stale), SLUG)
        self.write(worktree / "staff-held.txt", "The staff location holds this file.\n")
        self.write(stale / "cell-held.txt", "The stale cell holds this file.\n")
        parent = self.root / "parent-production.md"
        self.write(parent, "# Parent\n- **Merge branch:** demo\n\n## Units\n\n"
                   + "| Unit | Plan | Worktree | Branch |\n| --- | --- | --- | --- |\n")
        self.write(self.doc, self.doc.read_text().replace(f"`{worktree}`", f"`{stale}`")
                   + f"\n- **Parent production:** `{parent}`\n")
        with mock.patch.dict(os.environ, self.env):
            _ = recorded_showrunner("parent", parent)
            owner = recorded_showrunner(SLUG, self.doc)
            _ = fixture_unit_location(owner, "names-unit", worktree)
            with style_ledger.Ledger(self.checkout, database) as ledger:
                holders = unit_files.unit_file_holders(self.checkout, self.doc, ledger)
        self.assertIn("staff-held.txt", holders["names-unit"])
        self.assertNotIn("cell-held.txt", holders["names-unit"])

    def test_style_rename_keeps_git_paths_plan_names_and_ticket_ids(self) -> None:
        worktree, plan, database = self.style_production()
        before = self.snapshot()
        result = self.rename("plan", "names-unit", "demo-names-unit")
        self.assert_ok(result)
        self.assertEqual(self.snapshot(), before)
        self.assertIn(f"git: keep branch demo-names; keep worktree {worktree}", result.stdout)
        self.assertNotIn(" -> ", next(line for line in result.stdout.splitlines() if "git:" in line))

        self.assert_ok(self.rename("apply", "names-unit", "demo-names-unit"))
        self.assertEqual(self.git("branch", "--show-current", cwd=worktree), "demo-names")
        self.assertEqual(self.git("show", "--format=%s", "--stat", "HEAD"),
                         "chore(demo): rename unit names-unit to demo-names-unit")
        self.assertIn(f"| demo-names-unit | `{plan}` | `{worktree}` | demo-names |", self.doc.read_text())
        self.assertEqual(self.git("ls-files", "--", str(self.doc)), "")
        for directory in (self.checkout, worktree):
            self.assertIn("> **Production:** demo (demo-names-unit)", (directory / plan).read_text())
            self.assertIn("names-unit history stays.", (directory / plan).read_text())
            self.assertFalse((directory / plan.with_name("demo-names-unit.md")).exists())
        self.assertIn('unit: "demo-names-unit"', (self.root / "issues/style.md").read_text())
        with closing(sqlite3.connect(database)) as connection:
            self.assertEqual(connection.execute("SELECT id,unit FROM tickets ORDER BY id").fetchall(), [
                ("names-unit-1", "names-unit"), ("names-unit-2", "demo-names-unit"),
                ("names-unit-3", "demo-names-unit"),
            ])
            self.assertEqual(connection.execute("SELECT unit FROM sync_waits").fetchall(), [("demo-names-unit",)])
        self.assert_ok(self.rename("verify", "names-unit", "demo-names-unit"))

    def test_style_rename_moves_only_its_productions_proposals(self) -> None:
        _ = self.style_production()
        other_doc = self.root / "other/docs/other-style-production.md"
        self.write(other_doc, "# Other style production\n\n- **Kind:** style\n"
                   + f"- **Proposals:** `{self.root / 'issues'}`\n\n## Units\n\n"
                   + "| Unit | Plan | Worktree | Branch |\n| --- | --- | --- | --- |\n"
                   + "| names-unit | docs/style-production/units/names-unit.md | /other/names | other-names |\n")
        with mock.patch.dict(os.environ, self.env):
            other_owner = recorded_showrunner("other-style", other_doc)
            _ = fixture_unit_location(other_owner, "names-unit", self.root / "other/names")
        owned = self.root / "issues/style.md"
        other = self.root / "issues/other-style.md"
        self.write(other, owned.read_text().replace("proposed_by: demo", "proposed_by: other-style"))
        other_before = other.read_bytes()
        unit_proposal = self.root / "issues/style-unit.md"
        self.write(unit_proposal, owned.read_text().replace("proposed_by: demo", "proposed_by: names-unit (demo)"))

        self.assert_ok(self.rename("apply", "names-unit", "demo-names-unit"))

        for issue in (owned, unit_proposal):
            self.assertIn('unit: "demo-names-unit"', issue.read_text())
        self.assertEqual(other.read_bytes(), other_before)

    def test_full_style_id_can_be_renamed_to_another_full_id(self) -> None:
        slug = "demo-style"
        checkout = self.rust / slug
        worktree = self.rust / f"{slug}-names"
        _ = self.git("worktree", "add", "-b", slug, str(checkout), SLUG)
        _ = self.git("worktree", "add", "-b", f"{slug}-names", str(worktree), SLUG)
        doc = checkout / f"docs/{slug}-production.md"
        plan = "docs/style-production/units/names-unit.md"
        self.write(doc, f"# Production — {slug}\n\n- **Kind:** style\n- **Repository:** `{self.checkout}`\n"
                   + f"- **Merge branch:** `{slug}`\n- **Showrunner checkout:** `{checkout}`\n"
                   + "- **Log:** production.log\n\n## Units\n\n"
                   + "| Unit | Plan | Worktree | Branch |\n| --- | --- | --- | --- |\n"
                   + f"| {slug}-names-unit | {plan} | `{worktree}` | {slug}-names |\n")
        self.write(checkout / plan, f"# Names\n\n> **Production:** {slug} ({slug}-names-unit)\n")
        _ = self.git("add", "docs", cwd=checkout)
        _ = self.git("commit", "-m", "full style unit fixture", cwd=checkout)
        _ = self.git("push", "origin", slug, f"{slug}-names")
        self.write(self.root / f"state/notifier/showrunner-{slug}/conf",
                   "EVERY=15\nTARGET=session:test-showrunner\nCHECK="
                   + shlex.join(["zsh", "/scripts/production_check.sh", str(doc)]) + "\n")
        self.env["STYLE_LEDGER_STATE_DIR"] = str(self.root / "state/style")
        with mock.patch.dict(os.environ, self.env):
            owner = recorded_showrunner(slug, doc)
            _ = fixture_unit_location(owner, f"{slug}-names-unit", worktree)
        before = self.snapshot()

        result = self.invoke(
            ["plan", slug, f"{slug}-names-unit", f"{slug}-words-names-unit",
             "--scratch", str(self.root / "scratch")], cwd=checkout)

        self.assert_ok(result)
        self.assertIn(f"branch {slug}-names -> {slug}-words-names; worktree {worktree} -> "
                      + str(self.rust / f"{slug}-words-names"), result.stdout)
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    _ = unittest.main()
