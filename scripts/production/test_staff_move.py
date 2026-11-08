"""Move staff membership and delegate tab placement through the exact row ID."""
from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import FrozenInstanceError, replace
from functools import cached_property
from typing import cast, override
from unittest import mock

from scripts.production import staff, staff_move, staff_sources
from scripts.production.staff_test_support import StaffTestCase, fixture_member
from scripts.herdr import herdr


class StaffMoveTests(StaffTestCase):
    members: dict[str, staff.StaffRow] = {}

    @cached_property
    def place(self) -> mock.MagicMock:
        return cast(mock.MagicMock, self.enterContext(mock.patch.object(herdr.Herdr, "move_row_tabs", return_value=True)))

    @override
    def setUp(self) -> None:
        super().setUp()
        source_doc = self.production("sdlc", workspace="tools")
        destination_doc = self.production("target", workspace="new-tools")
        self.members = {}
        self.members["source"] = fixture_member("showrunner", name="tools", doc=staff.LiveDoc("natedev", source_doc))
        self.members["destination"] = fixture_member("showrunner", name="new-tools", doc=staff.LiveDoc("natedev", destination_doc))
        self.members["unit"] = fixture_member("unit", name="panes", parent=staff.ReportsTo(self.members["source"].id))
        self.members["worker"] = fixture_member("worker", name="impl1", parent=staff.ReportsTo(self.members["unit"].id))
        self.pane("w1:p1", session_id=self.members["unit"].current_session, environment={
            "CLAUDE_CODE_SESSION_ID": self.members["unit"].current_session, ('' + 'SHOWRUNNER_' + 'UNIT'): "sdlc", ('' + 'SHOWRUNNER_' + 'UNIT_ID'): "models",
        })
        _ = self.place

    def test_old_launch_labels_cannot_select_a_renamed_unit(self) -> None:
        before = self.staff_file.read_bytes()
        with self.assertRaisesRegex(staff.StaffRefused, "no matching unit staff row"):
            _ = staff_move.move_unit("tools", "models", "new-tools")
        self.assertEqual(self.staff_file.read_bytes(), before)
        self.place.assert_not_called()

    def test_move_changes_only_parent_and_moves_running_pane_to_parent_workspace(self) -> None:
        before = staff.snapshot()
        with mock.patch.object(staff, "snapshot", wraps=staff.snapshot) as snapshot:
            result = staff_move.move_unit("tools", "panes", "new-tools")
        snapshot.assert_called_once_with()
        self.assertEqual(result.row, replace(self.members["unit"], parent=staff.ReportsTo(self.members["destination"].id)))
        self.assertTrue(result.changed)
        self.assertEqual(staff.snapshot(), replace(before, rows=tuple(
            result.row if row.id == self.members["unit"].id else row for row in before.rows)))
        self.place.assert_called_once_with(self.members["unit"].id, record=mock.ANY)
        with self.assertRaises(FrozenInstanceError):
            setattr(result, "row", self.members["unit"])

    def test_staff_move_reports_done_then_unchanged_without_a_second_staff_write(self) -> None:
        self.place.side_effect = [True, False]
        for repeated in (False, True):
            with self.subTest(repeated=repeated):
                before, modified = self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns
                with (redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors,
                      mock.patch.object(staff, "move", wraps=staff.move) as move):
                    code = staff.main(["move", "tools/panes", "new-tools"])
                self.assertEqual(code, 0, errors.getvalue())
                self.assertEqual(output.getvalue(), ("unchanged" if repeated else "done") + ": panes under new-tools\n")
                self.assertEqual(errors.getvalue(), "")
                self.assertEqual(staff.by_id(self.members["unit"].id), replace(self.members["unit"], parent=staff.ReportsTo(self.members["destination"].id)))
                self.assertEqual(self.place.call_args_list, [mock.call(self.members["unit"].id, record=mock.ANY)] * (1 + repeated))
                if repeated:
                    move.assert_not_called()
                    self.assertEqual(self.staff_file.read_bytes(), before)
                    self.assertEqual(self.staff_file.stat().st_mtime_ns, modified)

    def test_placement_and_retry_read_the_parent_after_the_staff_write(self) -> None:
        parents: list[staff.ReportsTo | staff.StaffRoot] = []
        def place(row_id: str, *, record: staff.StaffSnapshot) -> bool:
            row = record.by_id(staff.StaffId(row_id))
            assert row is not None
            parents.append(row.parent)
            if len(parents) == 1:
                raise herdr.HerdrFailure("move refused")
            return True
        self.place.side_effect = place
        with self.assertRaisesRegex(staff.StaffUnreadable, "tab was not moved"):
            _ = staff_move.move_unit("tools", "panes", "new-tools")
        before = self.staff_file.read_bytes()
        result = staff_move.move_unit("tools", "panes", "new-tools")
        self.assertTrue(result.changed)
        self.assertEqual(parents, [staff.ReportsTo(self.members["destination"].id)] * 2)
        self.assertEqual(self.staff_file.read_bytes(), before)
        self.assertEqual(self.place.call_args_list, [mock.call(self.members["unit"].id, record=mock.ANY)] * 2)

    def test_rerun_keeps_identity_after_the_source_row_is_removed(self) -> None:
        target_doc = self.source_root / "target-production.md"
        target_text = target_doc.read_text()
        for pinned in (True, False):
            with self.subTest(pinned=pinned):
                _ = target_doc.write_text(target_text)
                _ = (self.source_root / "sdlc-production.md").write_text("# Fixture production\n- **Showrunner session:** `tools`\n")
                _ = staff.move(self.members["unit"].id, staff.ReportsTo(self.members["source"].id))
                self.members["unit"] = staff.rename(self.members["unit"].id, "models" if not pinned else "panes")
                identity = staff_move.PinnedUnit(self.members["unit"].id, self.members["destination"].id) if pinned else staff_move.LocateUnit()
                self.place.reset_mock()
                self.place.return_value = True
                first = staff_move.move_unit("tools", "models", "new-tools", identity=identity)
                _ = (self.source_root / "sdlc-production.md").write_text("# Fixture production\n- **Showrunner session:** `tools`\n")
                if pinned:
                    (self.source_root / "target-production.md").unlink()
                before = self.staff_file.read_bytes()
                self.place.return_value = False
                repeated = staff_move.move_unit("tools", "models", "new-tools", identity=identity)
                self.assertEqual(repeated.row, first.row)
                self.assertEqual(repeated.row.id, self.members["unit"].id)
                self.assertEqual(self.staff_file.read_bytes(), before)
                self.assertFalse(repeated.changed)
                self.assertEqual(self.place.call_args_list, [mock.call(self.members["unit"].id, record=mock.ANY)] * 2)

    def test_mac_moves_owner_row_and_local_tab_without_a_local_staff_copy(self) -> None:
        for key, production_name in (("source", "sdlc"), ("destination", "target")):
            self.members[key] = staff.set_doc(self.members[key].id, staff.LiveDoc(
                "mac", self.root / "mac-sources" / f"{production_name}-production.md"))
        with self.machine("mac"):
            _ = self.production("sdlc", workspace="tools")
            _ = self.production("target", workspace="new-tools")
            self.pane("w1:p1", session_id=self.members["unit"].current_session, environment={
                "CLAUDE_CODE_SESSION_ID": self.members["unit"].current_session, ('' + 'SHOWRUNNER_' + 'UNIT'): "sdlc", ('' + 'SHOWRUNNER_' + 'UNIT_ID'): "models",
            })
            before = len(self.ssh_commands())
            result = staff_move.move_unit("tools", "panes", "new-tools")
            self.assertEqual(result.row, replace(self.members["unit"], parent=staff.ReportsTo(self.members["destination"].id)))
            self.assertFalse(self.mac_file.exists())
        self.assertEqual(self.ssh_commands()[before:], [
            ["show", "--snapshot", "--json"], ["move", self.members["unit"].id, f"--parent={self.members["destination"].id}", "--json"],
        ])
        self.assertEqual(staff.by_id(self.members["unit"].id), result.row)
        self.place.assert_called_once_with(self.members["unit"].id, record=mock.ANY)

    def test_invalid_pinned_identity_never_changes_staff_or_selects_a_replacement(self) -> None:
        replacement = fixture_member("unit", name="models", parent=staff.ReportsTo(self.members["destination"].id))
        before = self.staff_file.read_bytes()
        missing = staff.StaffId("s00000000000")
        for unit, parent, reason in [(missing, self.members["destination"].id, ""),
                *((self.members["unit"].id, parent, "no matching showrunner")
                  for parent in (missing, self.members["unit"].id, self.members["worker"].id))]:
            with self.subTest(unit=unit, parent=parent):
                with self.assertRaisesRegex(staff.StaffRefused, reason):
                    _ = staff_move.move_unit("tools", "models", "new-tools", identity=staff_move.PinnedUnit(unit, parent))
                self.assertEqual(staff.by_id(replacement.id), replacement)
                self.assertEqual(self.staff_file.read_bytes(), before)
        self.place.assert_not_called()

    def test_missing_member_refuses_before_any_staff_or_tab_change(self) -> None:
        before = self.staff_file.read_bytes()
        for target, destination, cause in (("tools/missing", "new-tools", "tools/missing"),
                                            ("tools/panes", "missing", "missing")):
            with self.subTest(target=target, destination=destination):
                completed = self.cli("move", target, destination, code=2)
                self.assertIn(cause, completed.stderr)
                self.assertEqual(len(completed.stderr.splitlines()), 1)
                self.assertEqual(self.staff_file.read_bytes(), before)
        self.place.assert_not_called()

    def test_wrong_member_kinds_refuse_before_any_change(self) -> None:
        before = self.staff_file.read_bytes()
        for unit, parent in ((self.members["source"], self.members["destination"]), (self.members["unit"], self.members["worker"])):
            with self.subTest(unit=unit.kind, parent=parent.kind):
                with mock.patch.object(staff_sources, "member", side_effect=[
                    staff_sources.MemberFound(unit), staff_sources.MemberFound(parent),
                ]):
                    with self.assertRaises(staff.StaffRefused):
                        _ = staff_move.move_unit("tools", "models", "new-tools")
                self.assertEqual(self.staff_file.read_bytes(), before)
        self.place.assert_not_called()

    def test_taken_unit_name_refuses_before_tab_move(self) -> None:
        _ = fixture_member("unit", name=self.members["unit"].name, parent=staff.ReportsTo(self.members["destination"].id))
        before = self.staff_file.read_bytes()
        with self.assertRaises(staff.StaffRefused):
            _ = staff_move.move_unit("tools", "models", "new-tools")
        self.assertEqual(self.staff_file.read_bytes(), before)
        self.place.assert_not_called()


if __name__ == "__main__":
    _ = unittest.main()
