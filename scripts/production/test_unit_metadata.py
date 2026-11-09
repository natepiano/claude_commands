"""Production metadata follows the staff row, git, and unit configuration."""
from __future__ import annotations

import io
from pathlib import Path
from contextlib import redirect_stderr
from unittest import mock

from scripts.production import staff, unit_lookup
from scripts.production.staff_test_support import StaffTestCase, fixture_member


class RowProductionMetadataTests(StaffTestCase):
    def test_units_under_one_parent_share_one_document_read(self) -> None:
        fixture = self.row_production()
        folder = fixture.worktree.parent / "second"
        _ = self.fixture_git(fixture.worktree, "worktree", "add", "-b", "second", str(folder))
        from scripts.lib.atomic_file import write_text_atomic

        write_text_atomic(folder / ".unit.toml", 'plan = "standby"\nresponsibilities = []\n')
        second = fixture_member("unit", name="second", parent=fixture.runner.id,
                                worktree=staff.PresentWorktree("natedev", folder))
        with mock.patch.object(Path, "read_text", autospec=True, side_effect=Path.read_text) as reads:
            metadata = unit_lookup.unit_metadata(staff.snapshot(), name=fixture.name)
        self.assertEqual(set(metadata.cells), {fixture.unit.id, second.id})
        self.assertEqual(metadata.problems, ())
        documents = [call.args[0] for call in reads.call_args_list if call.args[0] == fixture.doc]
        self.assertEqual(documents, [fixture.doc])

    def test_unfilled_locations_keep_the_unit_visible_with_one_problem_line(self) -> None:
        runner, unit, _ = self.tree()
        errors = io.StringIO()
        with redirect_stderr(errors):
            metadata = unit_lookup.unit_metadata(staff.snapshot())
        self.assertIn(unit.id, metadata.cells)
        self.assertIn("unavailable:", metadata.cells[unit.id]["Worktree"])
        self.assertIn("unavailable:", metadata.cells[unit.id]["Plan"])
        self.assertEqual(len(metadata.problems), 1)
        self.assertEqual((metadata.problems[0].unit_id, metadata.problems[0].name), (unit.id, unit.name))
        self.assertIn("the worktree is not recorded", metadata.problems[0].reason)
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(unit.parent, staff.ReportsTo(runner.id))
