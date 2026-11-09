"""Change the staff parent without publishing work."""
from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path
from typing import override
from unittest import mock

from scripts.message import send
from scripts.production import staff, staff_move, unit_config
from scripts.production import stall_watch
from scripts.production.staff_test_support import unit_membership
from scripts.production.transfer_unit_test_support import UnitTransferFixture


class UnitTransferTests(UnitTransferFixture):
    def test_transfer_keeps_the_staff_folder_when_the_director_runs_elsewhere(self) -> None:
        director = self.root / "director-folder"
        director.mkdir()
        row = unit_membership("demo", "alpha")
        _ = staff.set_worktree(row.id, staff.PresentWorktree("natedev", director), expected=row.worktree)
        self.assert_ok(self.transfer("apply"))
        retained = unit_membership("other", "alpha")
        self.assertEqual(retained.id, row.id)
        self.assertEqual(retained.worktree, staff.PresentWorktree("natedev", director))
        self.assertTrue(self.unit.is_dir())

    @override
    def setUp(self) -> None:
        super().setUp()
        for production_name, checkout in (("demo", self.giving), ("other", self.receiving)):
            owner = next(row for row in staff.find(kind="showrunner") if row.name == production_name)
            _ = staff.set_doc(owner.id, staff.LiveDoc("natedev", checkout / f"docs/plans/{production_name}-production.md"))

    def test_transfer_changes_only_parent_and_preserves_locations_and_config_bytes(self) -> None:
        row = next(row for row in staff.find(kind="unit") if row.name == "alpha")
        row = staff.set_worktree(row.id, staff.PresentWorktree("natedev", self.unit), expected=row.worktree)
        receiving = next(row for row in staff.find(kind="showrunner") if row.name == "other")
        assert isinstance(row.parent, staff.ReportsTo)
        giving = staff.by_id(row.parent.id)
        assert giving is not None
        parents = tuple(staff.set_doc(parent.id, staff.LiveDoc(
            "natedev", checkout / "docs/plans" / f"{parent.name}-production.md"))
            for parent, checkout in ((giving, self.giving), (receiving, self.receiving)))
        _ = unit_config.write(self.unit, unit_config.UnitConfig(
            unit_config.PlanFile(Path("docs/plans/alpha.md")), unit_config.AppPort(8123), ("review",)))
        path = self.unit / ".unit.toml"
        before = path.read_bytes(), path.stat().st_mtime_ns
        self.assert_ok(self.transfer("apply"))
        self.assertEqual(staff.by_id(row.id), replace(row, parent=staff.ReportsTo(receiving.id)))
        self.assertEqual(tuple(staff.by_id(parent.id) for parent in parents), parents)
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        self.assertTrue(self.unit.is_dir())
        self.assertEqual(self.git("check-ignore", ".unit.toml", cwd=self.unit), ".unit.toml")
        self.assert_ok(self.transfer("apply"))
        self.assertEqual(staff.by_id(row.id), replace(row, parent=staff.ReportsTo(receiving.id)))
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_transfer_preserves_staff_keyed_stall_files(self) -> None:
        row = unit_membership("demo", "alpha")
        directory = self.root / "state/stall-watch"
        directory.mkdir(parents=True, exist_ok=True)
        with mock.patch.object(stall_watch, "STATE_DIR", directory):
            watch = stall_watch.watched_staff_unit("demo", "alpha", row)
            stretch, prompt = watch.stretch_path, watch.prompt_path
            _ = stretch.write_text('{"since": 17}\n')
            _ = prompt.write_text('{"key": "stall-watch:prompt:' + row.id + '"}\n')
            before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (stretch, prompt)}
            self.assert_ok(self.transfer("apply"))
            moved = unit_membership("other", "alpha")
            watch = stall_watch.watched_staff_unit("other", "alpha", moved)
            self.assertEqual(watch.stretch_path, stretch)
            self.assertEqual(watch.prompt_path, prompt)
            self.assertEqual({path: (path.read_bytes(), path.stat().st_mtime_ns) for path in before}, before)

    def test_plan_then_apply_finishes_source_updates_after_a_staff_move(self) -> None:
        row = unit_membership("demo", "alpha")
        parent = next(row for row in staff.find(kind="showrunner") if row.name == "other")
        _ = staff_move.move_unit("demo", "alpha", "other")
        before = self.staff_file.read_bytes()
        planned = self.transfer("plan")
        self.assert_ok(planned)
        self.assertEqual(self.staff_file.read_bytes(), before)
        self.assert_ok(self.transfer("apply"))
        self.assertEqual(staff.snapshot().by_id(row.id), staff.StaffRow(row.id, "unit", row.name, staff.ReportsTo(parent.id), current_session=row.id,
                                worktree=staff.PresentWorktree("natedev", self.unit), doc=staff.DocUnfilled(), home=row.home))
        self.assert_ok(self.transfer("verify"))

    def test_transfer_changes_only_the_staff_parent(self) -> None:
        row = unit_membership("demo", "alpha")
        parent = next(item for item in staff.find(kind="showrunner") if item.name == "other")
        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        with mock.patch.object(staff_move, "move_unit", wraps=staff_move.move_unit) as move_tab:
            result = self.transfer("apply")
            self.assert_ok(result)
        move_tab.assert_called_once_with("demo", "alpha", "other",
                                         identity=staff_move.PinnedUnit(row.id, parent.id))
        self.assertEqual(staff.by_id(row.id), replace(row, parent=staff.ReportsTo(parent.id)))
        changed = {path for path in before if path.read_bytes() != before[path]}
        self.assertEqual(changed, {self.staff_file})
        expected = [["--to", str(row.id), "--from", "showrunners", "--key",
                     f"transfer:{row.id}:{parent.id}", "--text", "Your unit now belongs to production other."]]
        self.assertEqual(self.messages(), expected)
        self.assertIn("told alpha", result.stdout)
        self.assertFalse(any("shutdown.py" in word or "launch_agent" in word
                             for command in self.commands for word in command))
        settled = {path: path.read_bytes() for path in before}
        self.assert_ok(self.transfer("apply"))
        self.assertEqual({path: path.read_bytes() for path in before}, settled)
        self.assertEqual(self.messages(), expected)

    def test_tab_move_failure_keeps_one_notice_after_the_parent_changes(self) -> None:
        row = unit_membership("demo", "alpha")
        parent = next(item for item in staff.find(kind="showrunner") if item.name == "other")
        with mock.patch.object(staff_move, "move_unit", side_effect=OSError("tab move failed")):
            result = self.transfer("apply")
        self.assertEqual(result.returncode, 1)
        self.assertIn("tab move failed", result.stderr)
        self.assertEqual(staff.by_id(row.id), replace(row, parent=staff.ReportsTo(parent.id)))
        expected = [["--to", str(row.id), "--from", "showrunners", "--key",
                     f"transfer:{row.id}:{parent.id}", "--text", "Your unit now belongs to production other."]]
        self.assertEqual(self.messages(), expected)
        self.assertIn("told alpha", result.stdout)
        self.assert_ok(self.transfer("apply"))
        self.assertEqual(self.messages(), expected)

    def test_transfer_notice_address_resolves_to_the_moved_staff_row(self) -> None:
        row = unit_membership("demo", "alpha")
        parent = next(item for item in staff.find(kind="showrunner") if item.name == "other")
        self.assert_ok(self.transfer("apply"))
        message = self.messages()[0]
        target = message[message.index("--to") + 1]
        self.assertEqual(target, str(row.id))
        self.assertEqual(send.resolve_staff_recipient(target, staff.snapshot()),
                         replace(row, parent=staff.ReportsTo(parent.id)))

    def test_transfer_preserves_branch_tips_and_plan_bytes(self) -> None:
        plan = self.unit / "docs/plans/alpha.md"
        before = plan.read_bytes()
        tips = self.git("rev-parse", "demo", "other", "demo-alpha", "origin/demo-alpha")
        self.assert_ok(self.transfer("apply"))
        self.assertEqual(self.git("rev-parse", "demo", "other", "demo-alpha", "origin/demo-alpha"), tips)
        self.assertEqual(plan.read_bytes(), before)
        self.assertTrue(self.unit.is_dir())
        self.assert_ok(self.transfer("verify"))


if __name__ == "__main__":
    _ = unittest.main()
