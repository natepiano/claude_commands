"""Production metadata follows the staff row, git, and unit configuration."""
from __future__ import annotations

import io
from pathlib import Path
import subprocess
from contextlib import redirect_stderr
from unittest import mock

from scripts.production import staff, unit_lookup
from scripts.production.unit_table import cell_value
from scripts.production.staff_test_support import StaffTestCase, fixture_member


class RowProductionMetadataTests(StaffTestCase):
    def test_one_production_does_not_read_another_productions_remote_facts(self) -> None:
        fixture = self.row_production()
        other = fixture_member("showrunner", name="remote-owner",
                               doc=staff.LiveDoc("mac", Path("/remote/production.md")))
        _ = fixture_member("unit", name="remote-member", parent=other.id,
                           worktree=staff.PresentWorktree("mac", Path("/remote/member")))
        run = subprocess.run

        def local_only(arguments: list[str], *, capture_output: bool,
                       text: bool, check: bool) -> subprocess.CompletedProcess[str]:
            if arguments[0] == "ssh":
                raise AssertionError("another production triggered SSH")
            return run(arguments, capture_output=capture_output, text=text, check=check)

        with mock.patch.object(subprocess, "run", side_effect=local_only):
            rows = unit_lookup.live_unit_table(fixture.doc.read_text().splitlines(), fixture.slug,
                                               record=staff.snapshot())
        self.assertEqual([row["Unit"] for row in rows], ["member-unit"])
        self.assertEqual(rows[0]["Branch"], "branch-from-git")

    def test_units_under_one_parent_share_one_document_read(self) -> None:
        fixture = self.row_production()
        folder = fixture.worktree.parent / "second"
        _ = self.fixture_git(fixture.worktree, "worktree", "add", "-b", "second", str(folder))
        from scripts.lib.atomic_file import write_text_atomic

        write_text_atomic(folder / ".unit.toml", 'plan = "standby"\nresponsibilities = []\n')
        second = fixture_member("unit", name="second", parent=fixture.runner.id,
                                worktree=staff.PresentWorktree("natedev", folder))
        sources = unit_lookup.member_sources()
        with mock.patch.object(Path, "read_text", autospec=True, side_effect=Path.read_text) as reads:
            metadata = unit_lookup.unit_metadata(staff.snapshot(), sources, slug=fixture.slug)
        self.assertEqual(set(metadata.cells), {fixture.unit.id, second.id})
        self.assertEqual(metadata.problems, ())
        documents = [call.args[0] for call in reads.call_args_list if call.args[0] == fixture.doc]
        self.assertEqual(documents, [fixture.doc])

    def test_current_metadata_uses_the_row_and_config_instead_of_conflicting_table_cells(self) -> None:
        fixture = self.row_production()
        record = staff.snapshot()
        before = self.staff_file.read_bytes()
        rows = unit_lookup.live_unit_table(fixture.doc.read_text().splitlines(), fixture.slug, record=record)
        self.assertEqual(len(rows), 1)
        cells = rows[0]
        self.assertEqual(cell_value(cells["Unit"]), "member-unit")
        self.assertEqual(cell_value(cells["Worktree"]), str(fixture.worktree))
        self.assertEqual(cell_value(cells["Branch"]), "branch-from-git")
        self.assertEqual(cell_value(cells["Plan"]), str(fixture.worktree / "docs/plans/from-config.md"))
        self.assertEqual(cell_value(cells["Port"]), "4317")
        self.assertEqual(self.staff_file.read_bytes(), before)

    def test_missing_worktree_evidence_keeps_the_member_and_names_the_problem(self) -> None:
        fixture = self.row_production()
        missing = self.root / "unreadable-worktree"
        _ = staff.set_worktree(fixture.unit.id, staff.PresentWorktree("natedev", missing))
        errors = io.StringIO()
        with redirect_stderr(errors):
            rows = unit_lookup.live_unit_table(fixture.doc.read_text().splitlines(), fixture.slug,
                                              record=staff.snapshot())
        self.assertEqual(len(rows), 1)
        self.assertEqual(cell_value(rows[0]["Unit"]), "member-unit")
        self.assertNotEqual(cell_value(rows[0]["Branch"]), "wrong-table-branch")
        self.assertIn("member", errors.getvalue())
        self.assertIn(str(missing), errors.getvalue())
        self.assertEqual(len(errors.getvalue().splitlines()), 1)


    def test_unfilled_locations_keep_the_unit_visible_with_one_problem_line(self) -> None:
        runner, unit, _ = self.tree()
        errors = io.StringIO()
        with redirect_stderr(errors):
            metadata = unit_lookup.unit_metadata(staff.snapshot(), unit_lookup.MemberSources({}, ()))
        self.assertIn(unit.id, metadata.cells)
        self.assertIn("unavailable:", metadata.cells[unit.id]["Worktree"])
        self.assertIn("unavailable:", metadata.cells[unit.id]["Plan"])
        self.assertEqual(len(metadata.problems), 1)
        self.assertEqual((metadata.problems[0].unit_id, metadata.problems[0].name), (unit.id, unit.name))
        self.assertIn("the worktree is not recorded", metadata.problems[0].reason)
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(unit.parent, runner.id)
