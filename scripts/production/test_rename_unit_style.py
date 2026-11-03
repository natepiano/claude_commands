"""Read held files from the staff worktree."""
from __future__ import annotations

import os
import unittest
from unittest import mock

from scripts.style import unit_files
from scripts.production.rename_unit_test_support import UnitRenameFixture, SLUG
import style_ledger
from scripts.production.staff_test_support import fixture_unit_location, recorded_showrunner


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


if __name__ == "__main__":
    _ = unittest.main()
