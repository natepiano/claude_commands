"""Unit admission preserves its staff identity and requires an explicit parent move."""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from scripts.production import staff
from scripts.production.staff_test_support import isolated_staff_file, unit_membership, fixture_runtime_id
from scripts.production.add_unit_test_support import AddUnitTestCase, configure_test_zone
from scripts.production import add_unit
from scripts.production.production_doc import read_production


def setUpModule() -> None:
    configure_test_zone()


class AddUnitStaffRowTests(AddUnitTestCase):
    def test_unit_under_another_showrunner_refuses_with_move_command_without_adding_rows_or_tabs(self) -> None:
        row = unit_membership("sdlc", "alpha")
        before = self.staff.staff_file.read_bytes()
        repository = self.repository_state()

        result = self.cli("alpha", "--plan", "docs/plans/given.md")

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(result.stderr,
                         "add_unit: alpha is under sdlc; to move it, run: staff.py move sdlc/alpha build-followups\n")
        self.assertEqual(staff.snapshot().find(kind="unit"), [row])
        self.assertEqual(self.staff.staff_file.read_bytes(), before)
        self.assertEqual(self.repository_state(), repository)
        self.assertFalse(any(command[2] in ("agent", "tab") or command[2:4] in (
            ["pane", "close"], ["workspace", "create"],
        ) for command in self.herdr_calls()))

    def test_same_showrunner_returns_the_existing_unit_without_writing(self) -> None:
        with (patch.dict(os.environ, self.env),
              isolated_staff_file(self.staff.staff_file, self.env)):
            production = read_production(self.production_name)
            row = unit_membership(self.production_name, "alpha")
            before = self.staff.staff_file.read_bytes(), self.staff.staff_file.stat().st_mtime_ns
            with patch.object(staff, "ensure", wraps=staff.ensure) as ensure:
                found = add_unit.ensure_staff_unit(production, "alpha")
            self.assertEqual(found, row)
            ensure.assert_not_called()
            self.assertEqual((self.staff.staff_file.read_bytes(), self.staff.staff_file.stat().st_mtime_ns), before)
        self.assertEqual(self.herdr_calls(), [])

    def test_missing_unit_is_added_under_the_requested_showrunner(self) -> None:
        with (patch.dict(os.environ, self.env),
              isolated_staff_file(self.staff.staff_file, self.env)):
            production = read_production(self.production_name)
            _ = add_unit.ensure_staff_showrunner(production)
            before = staff.snapshot()
            worktree = self.root / "project-alpha"
            _ = self.git("worktree", "add", "-b", "build-followups-alpha", str(worktree), "build-followups")
            row = add_unit.ensure_staff_unit(production, "alpha",
                                             worktree=staff.PresentWorktree("natedev", worktree),
                                             runtime_id=fixture_runtime_id("new-alpha-unit-backend"))
            parent = before.by_path(self.production_name)
            self.assertIsNotNone(parent)
            assert parent is not None
            self.assertEqual((row.kind, row.name, row.parent), ("unit", "alpha", staff.ReportsTo(parent.id)))
            self.assertCountEqual(staff.snapshot().rows, (*before.rows, row))
        self.assertEqual(self.herdr_calls(), [])


if __name__ == "__main__":
    _ = unittest.main()
