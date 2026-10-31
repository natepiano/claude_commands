"""Style unit identities, plans, tickets and restart records."""

from __future__ import annotations

import os
import shlex
import sqlite3
import sys
import unittest
from contextlib import closing
from pathlib import Path
from typing import cast, final
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import rename_unit


from scripts.production.rename_unit_test_support import UnitRenameFixture, SLUG


@final
class UnitRenameStyleTests(UnitRenameFixture):

    def test_style_rename_keeps_git_paths_plan_names_and_ticket_ids(self) -> None:
        worktree, plan, database = self.style_production()
        before = self.snapshot()
        result = self.rename("plan", "names-unit", "demo-names-unit")
        self.assert_ok(result)
        self.assertEqual(self.snapshot(), before)
        destination = self.home / "worktrees/demo-names"
        self.assertIn(f"git: keep branch demo-names; worktree {worktree} -> {destination}", result.stdout)

        self.assert_ok(self.rename("apply", "names-unit", "demo-names-unit"))
        self.assertFalse(worktree.exists())
        worktree = destination
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
        before = self.snapshot()

        result = self.invoke(
            ["plan", slug, f"{slug}-names-unit", f"{slug}-words-names-unit",
             "--scratch", str(self.root / "scratch")], cwd=checkout)

        self.assert_ok(result)
        self.assertIn(f"branch {slug}-names -> {slug}-words-names; worktree {worktree} -> "
                      + str(self.home / f"worktrees/{slug}-words-names"), result.stdout)
        self.assertEqual(self.snapshot(), before)

    def test_restart_record_keeps_one_prefix_for_a_full_style_id(self) -> None:
        slug = "cargo-liner-style"
        _ = self.seed_record(name="names", host_session=f"{slug}-names", unit="names-unit", slug=slug)
        self.write(self.doc, self.doc.read_text().replace("| alpha-unit |", "| names-unit |"))
        worktree = str(self.rust / "claude-demo-alpha")
        rename = rename_unit.UnitRename(
            slug, "names-unit", f"{slug}-names-unit", str(self.doc), str(self.checkout), SLUG,
            f"{slug}-names", f"{slug}-names", worktree, worktree, "", str(self.root / "scratch"),
            account="owner@example.com", session_id="sid-alpha")

        with mock.patch.dict(os.environ, self.env):
            rename_unit._restart_record(rename, lambda: None)  # pyright: ignore[reportPrivateUsage]

        session = cast(dict[str, object], self.record()["session"])
        host = cast(dict[str, object], session["host"])
        self.assertEqual(host["session_name"], "cargo-liner-style-names")
        self.assertEqual((host["unit"], session["name"]), ("cargo-liner-style-names-unit", "cargo-liner-style-names"))

    def test_restart_record_moves_a_feature_host_to_its_new_stem(self) -> None:
        _ = self.seed_record()
        rename = rename_unit.UnitRename(
            SLUG, "alpha-unit", "beta-unit", str(self.doc), str(self.checkout), SLUG,
            "demo-alpha", "demo-beta", str(self.rust / "claude-demo-alpha"),
            str(self.rust / "claude-demo-beta"), "", str(self.root / "scratch"),
            account="owner@example.com", session_id="sid-alpha")

        with mock.patch.dict(os.environ, self.env):
            rename_unit._restart_record(rename, lambda: None)  # pyright: ignore[reportPrivateUsage]

        session = cast(dict[str, object], self.record()["session"])
        self.assertEqual(cast(dict[str, object], session["host"])["session_name"], "beta")


if __name__ == "__main__":
    _ = unittest.main()
