"""Read held files from the staff worktree."""
from __future__ import annotations

import os
import unittest
from unittest import mock

from scripts.production.rename_unit_test_support import UnitRenameFixture, SLUG
from scripts.style import unit_files
from scripts.style import style_ledger
from scripts.production.staff_test_support import fixture_unit_location, recorded_showrunner


class UnitRenameStyleTests(UnitRenameFixture):
    def test_held_files_follow_staff_locations(self) -> None:
        fixture = self.style_production()
        worktree, database = fixture[0], fixture[2]
        self.write(worktree / "staff-held.txt", "The staff location holds this file.\n")
        parent = self.root / "parent-production.md"
        self.write(parent, "# Parent\n- **Merge branch:** demo\n")
        with mock.patch.dict(os.environ, self.env):
            _ = recorded_showrunner("parent", parent)
            owner = recorded_showrunner(SLUG, self.doc, parent="parent")
            _ = fixture_unit_location(owner, "names", worktree)
            with style_ledger.Ledger(self.checkout, database) as ledger:
                holders = unit_files.unit_file_holders(self.checkout, self.doc, ledger)
        self.assertIn("staff-held.txt", holders["names"])


if __name__ == "__main__":
    _ = unittest.main()
