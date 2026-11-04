"""Production rename: units behavior."""

from __future__ import annotations

import unittest
from typing import final
from pathlib import Path
from unittest import mock

from scripts.production import staff, unit_config
from scripts.production import rename_test_support as support
from scripts.production.rename_test_support import NEW, OLD
from scripts.production.rename_rerun_test_support import final_files


@final
class ProductionRenameTests(support.ProductionRenameFixture):
    def test_completed_unit_rename_follows_a_later_staff_folder(self) -> None:
        self.prepare_unit_moves()
        self.assert_success(self.run_rename("apply"))
        self.assert_success(self.run_command("unit", NEW, "alpha-unit"))
        original = self.unit_worktree(NEW, "alpha-unit")
        destination = self.root / "later-staff-folder"
        _ = self.git("worktree", "move", str(original), str(destination))
        row = staff.snapshot().by_path(f"{NEW}/alpha")
        assert row is not None
        _ = staff.set_worktree(row.id, staff.PresentWorktree("natedev", destination), expected=row.worktree)
        before = final_files(self.root)
        rerun = self.run_command("unit", NEW, "alpha-unit")
        self.assert_success(rerun)
        self.assertIn("unchanged", rerun.stdout)
        self.assertEqual(final_files(self.root), before)

    def test_retry_after_unit_folder_move_updates_the_pinned_row_and_preserves_its_config(self) -> None:
        self.prepare_unit_moves()
        row = staff.snapshot().by_path(f"{OLD}/alpha")
        assert row is not None
        old, new = self.unit_worktree(OLD, "alpha-unit"), self.unit_worktree(NEW, "alpha-unit")
        _ = staff.set_worktree(row.id, staff.PresentWorktree("natedev", old), expected=row.worktree)
        _ = unit_config.write(old, unit_config.UnitConfig(unit_config.PlanFile(Path("docs/plans/alpha-unit.md")),
                                                    unit_config.AppPort(8080), ("review",)))
        config = (old / ".unit.toml").read_bytes()
        self.assert_success(self.run_rename("apply"))
        with mock.patch.object(staff, "set_worktree", side_effect=OSError("stopped before staff path write")):
            interrupted = self.run_command("unit", NEW, "alpha-unit")
        self.assertEqual(interrupted.returncode, 1, interrupted.stdout + interrupted.stderr)
        self.assertIn("stopped before staff path write", interrupted.stderr)
        self.assertFalse(old.exists())
        self.assertTrue(new.is_dir())
        retained = staff.by_id(row.id)
        assert retained is not None
        self.assertEqual(retained.worktree, staff.PresentWorktree("natedev", old))
        self.assert_success(self.run_command("unit", NEW, "alpha-unit"))
        moved = staff.by_id(row.id)
        assert moved is not None
        self.assertEqual((moved.id, moved.name, moved.parent, moved.worktree),
                         (row.id, row.name, row.parent, staff.PresentWorktree("natedev", new)))
        self.assertEqual((new / ".unit.toml").read_bytes(), config)
        self.assertEqual(self.git("-C", str(new), "check-ignore", ".unit.toml"), ".unit.toml")
        before = final_files(self.root)
        self.assert_success(self.run_command("unit", NEW, "alpha-unit"))
        self.assertEqual(final_files(self.root), before)


if __name__ == "__main__":
    _ = unittest.main()
