"""Change the staff parent and move the Units row without publishing work."""
from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path
from typing import override

from scripts.production import staff, staff_move, unit_config
from scripts.production.staff_test_support import unit_membership
from scripts.production.transfer_unit_test_support import UnitTransferFixture


class UnitTransferTests(UnitTransferFixture):
    def test_transfer_uses_staff_after_the_worktree_column_is_removed(self) -> None:
        from scripts.production.staff_test_support import without_worktree_column
        document = self.giving / "docs/plans/demo-production.md"
        without_worktree_column(document)
        _ = self.git("add", str(document), cwd=self.giving)
        _ = self.git("commit", "-m", "Remove the display column", cwd=self.giving)
        self.assert_ok(self.transfer("apply"))
        self.assertTrue(self.unit.is_dir())
        self.assertFalse((self.root / "state/showrunner/demo/transfer_unit.json").exists())
        row = unit_membership("other", "alpha-unit")
        self.assertEqual(row.worktree, staff.PresentWorktree("natedev", self.unit))

    def test_transfer_uses_the_absolute_plan_when_the_director_runs_elsewhere(self) -> None:
        document = self.giving / "docs/plans/demo-production.md"
        plan = self.unit / "docs/plans/alpha.md"
        _ = document.write_text(document.read_text().replace("docs/plans/alpha.md", str(plan)))
        _ = self.git("add", str(document), cwd=self.giving)
        _ = self.git("commit", "-m", "Record the branch plan", cwd=self.giving)
        director = self.root / "director-folder"
        director.mkdir()
        row = unit_membership("demo", "alpha-unit")
        _ = staff.set_worktree(row.id, staff.PresentWorktree("natedev", director), expected=row.worktree)
        self.assert_ok(self.transfer("apply"))
        retained = unit_membership("other", "alpha-unit")
        self.assertEqual(retained.id, row.id)
        self.assertEqual(retained.worktree, staff.PresentWorktree("natedev", director))
        self.assertTrue(self.unit.is_dir())

    @override
    def setUp(self) -> None:
        super().setUp()
        for slug, checkout in (("demo", self.giving), ("other", self.receiving)):
            owner = next(row for row in staff.find(kind="showrunner") if row.name == slug)
            _ = staff.set_doc(owner.id, staff.LiveDoc("natedev", checkout / f"docs/plans/{slug}-production.md"))

    def test_transfer_changes_only_parent_and_preserves_locations_and_config_bytes(self) -> None:
        row = next(row for row in staff.find(kind="unit") if row.name == "alpha")
        row = staff.set_worktree(row.id, staff.PresentWorktree("natedev", self.unit), expected=row.worktree)
        receiving = next(row for row in staff.find(kind="showrunner") if row.name == "other")
        assert row.parent is not None
        giving = staff.by_id(row.parent)
        assert giving is not None
        parents = tuple(staff.set_doc(parent.id, staff.LiveDoc(
            "natedev", checkout / "docs/plans" / f"{parent.name}-production.md"))
            for parent, checkout in ((giving, self.giving), (receiving, self.receiving)))
        _ = unit_config.write(self.unit, unit_config.UnitConfig(
            unit_config.PlanFile(Path("docs/plans/alpha.md")), unit_config.AppPort(8123), ("review",)))
        path = self.unit / ".unit.toml"
        before = path.read_bytes(), path.stat().st_mtime_ns
        self.assert_ok(self.transfer("apply"))
        self.assertEqual(staff.by_id(row.id), replace(row, parent=receiving.id))
        self.assertEqual(tuple(staff.by_id(parent.id) for parent in parents), parents)
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        self.assertTrue(self.unit.is_dir())
        self.assertEqual(self.git("check-ignore", ".unit.toml", cwd=self.unit), ".unit.toml")
        self.assert_ok(self.transfer("apply"))
        self.assertEqual(staff.by_id(row.id), replace(row, parent=receiving.id))
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_plan_then_apply_finishes_source_updates_after_a_staff_move(self) -> None:
        row = unit_membership("demo", "alpha-unit")
        parent = next(row for row in staff.find(kind="showrunner") if row.name == "other")
        _ = staff_move.move_unit("demo", "alpha-unit", "other")
        before = self.staff_file.read_bytes()
        planned = self.transfer("plan")
        self.assert_ok(planned)
        self.assertEqual(self.staff_file.read_bytes(), before)
        self.assert_ok(self.transfer("apply"))
        self.assertEqual(staff.snapshot().by_id(row.id), staff.StaffRow(row.id, "unit", row.name, parent.id, current_session=row.id,
                                worktree=staff.PresentWorktree("natedev", self.unit), doc=staff.DocUnfilled()))
        self.assertNotIn("alpha-unit", (self.giving / "docs/plans/demo-production.md").read_text())
        self.assertIn("alpha-unit", (self.receiving / "docs/plans/other-production.md").read_text())
        self.assert_ok(self.transfer("verify"))

    def test_transfer_changes_only_the_staff_parent_and_the_units_row(self) -> None:
        row = unit_membership("demo", "alpha-unit")
        parent = next(item for item in staff.find(kind="showrunner") if item.name == "other")
        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.assert_ok(self.transfer("apply"))
        self.assertEqual(staff.by_id(row.id), replace(row, parent=parent.id))
        giving = self.giving / "docs/plans/demo-production.md"
        receiving = self.receiving / "docs/plans/other-production.md"
        self.assertNotIn("| alpha-unit |", giving.read_text())
        self.assertIn("| alpha-unit |", receiving.read_text())
        changed = {path for path in before if path.read_bytes() != before[path]}
        self.assertEqual(changed, {self.staff_file, giving, receiving})
        self.assertEqual(self.messages(), [])
        self.assertFalse(any("shutdown.py" in word or "launch_agent" in word
                             for command in self.commands for word in command))
        settled = {path: path.read_bytes() for path in before}
        self.assert_ok(self.transfer("apply"))
        self.assertEqual({path: path.read_bytes() for path in before}, settled)

    def test_transfer_preserves_branch_tips_and_plan_bytes(self) -> None:
        plan = self.unit / "docs/plans/alpha.md"
        before = plan.read_bytes()
        tips = self.git("rev-parse", "demo", "other", "demo-alpha", "origin/demo-alpha")
        self.assert_ok(self.transfer("apply"))
        receiving = (self.receiving / "docs/plans/other-production.md").read_text()
        self.assertIn("| alpha-unit | docs/plans/alpha.md | — | demo-alpha | — | — |", receiving)
        giving = (self.giving / "docs/plans/demo-production.md").read_text()
        self.assertNotIn("alpha-unit", giving)
        self.assertIn("beta-unit", giving)
        self.assertEqual(self.git("rev-parse", "demo", "other", "demo-alpha", "origin/demo-alpha"), tips)
        self.assertEqual(plan.read_bytes(), before)
        self.assertTrue(self.unit.is_dir())
        self.assertEqual(self.stall_path("demo", "alpha-unit").read_text(), '{"stretch": 17}\n')
        self.assertFalse(self.stall_path("other", "alpha-unit").exists())
        self.assert_ok(self.transfer("verify"))


if __name__ == "__main__":
    _ = unittest.main()
