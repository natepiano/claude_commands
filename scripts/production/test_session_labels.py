"""Session labels use the native runtime id and never infer a row from a folder."""
from __future__ import annotations

from dataclasses import replace

from scripts.production import session_labels, staff
from scripts.production.staff_test_support import StaffTestCase, fixture_runtime_id


class NativeSessionLabelTests(StaffTestCase):
    def test_runtime_identity_selects_the_member_despite_a_stale_mark_and_display_name(self) -> None:
        runner, unit, worker = self.tree()
        labels = session_labels.StaffLabels(staff.snapshot(), {}, {})
        environment = {"STAFF_ID": worker.id, "SHOWRUNNER_UNIT_ID": "models-unit"}
        selected = labels.environment(environment, unit.id, worker.name, str(self.root))
        self.assertEqual(selected["CLAUDE_CODE_SESSION_ID"], unit.id)
        self.assertEqual(selected["SHOWRUNNER_UNIT"], runner.name)
        self.assertEqual(selected["SHOWRUNNER_UNIT_ID"], "models-unit")
        self.assertNotIn("STAFF_ID", selected)
        self.assertEqual(labels.environment(environment, worker.id)["CLAUDE_CODE_SESSION_ID"], worker.id)

    def test_unknown_runtime_is_not_assigned_by_its_unique_folder_or_display_name(self) -> None:
        runner, unit, _ = self.tree()
        located = replace(unit, worktree=staff.PresentWorktree("natedev", self.root))
        labels = session_labels.StaffLabels(staff.StaffSnapshot((runner, located)), {}, {})
        before = self.staff_file.read_bytes()
        selected = labels.environment({"STAFF_ID": unit.id}, fixture_runtime_id("unpublished"), unit.name, str(self.root))
        self.assertNotIn("CLAUDE_CODE_SESSION_ID", selected)
        self.assertNotIn("STAFF_ID", selected)
        self.assertNotIn("SHOWRUNNER_UNIT", selected)
        self.assertEqual(self.staff_file.read_bytes(), before)
