"""Retained records remove copied labels and keep native identity evidence."""
from __future__ import annotations

import os
from unittest.mock import patch

from scripts.production.staff_test_support import StaffTestCase
from scripts.herdr import pane_launch
from scripts.agents import pane_record


class NativeLabelFilteringTests(StaffTestCase):
    def test_unavailable_process_record_still_removes_legacy_labels(self) -> None:
        legacy = {"STAFF_ID": "old-owner", "SHOWRUNNER_" + "UNIT": "old-parent",
                  "SHOWRUNNER_" + "UNIT_ID": "old", "CODEX_THREAD_ID": "native-id",
                  "CLAUDE_CONFIG_DIR": "/private/account"}
        before = legacy.copy()
        labels = pane_launch.RecordedLabels(pane_launch.StaffLabelsRead.UNAVAILABLE)
        self.assertEqual(labels.resolve(991099, legacy), {
            "CODEX_THREAD_ID": "native-id", "CLAUDE_CONFIG_DIR": "/private/account"})
        self.assertEqual(legacy, before)

    def test_save_and_write_remove_old_labels_without_changing_the_input(self) -> None:
        fixture = self.row_production()
        old = pane_record.launch("w1:p1", fixture.unit.id, str(fixture.worktree), {}, ["claude"])
        old["environment"] = {
            "STAFF_ID": fixture.unit.id,
            "SHOWRUNNER_" + "UNIT": "old-parent",
            "SHOWRUNNER_" + "UNIT_ID": "old",
            "CLAUDE_CODE_SESSION_ID": fixture.unit.current_session,
            "ENABLE_TOOL_SEARCH": "true",
        }
        before = old["environment"].copy()
        expected = {"CLAUDE_CODE_SESSION_ID": fixture.unit.current_session, "ENABLE_TOOL_SEARCH": "true"}
        with (patch.dict(os.environ, {"HERDR_BIN": "/nonexistent/herdr"}),
              patch.object(pane_record, "RECORD_DIR", self.root / "filtered-records")):
            pane_record.save("agents", {old["pane_id"]: old})
            self.assertEqual(pane_record.read("agents")[old["pane_id"]]["environment"], expected)
            pane_record.write("agents", old)
            self.assertEqual(pane_record.read("agents")[old["pane_id"]]["environment"], expected)
        self.assertEqual(old["environment"], before)

    def test_history_filters_old_labels_and_keeps_only_the_same_conversation(self) -> None:
        fixture = self.row_production()
        retained = pane_record.launch("old", fixture.unit.id, str(fixture.worktree), {}, ["claude"])
        retained["environment"] = {
            "STAFF_ID": fixture.unit.id,
            "SHOWRUNNER_" + "UNIT": "old-parent",
            "SHOWRUNNER_" + "UNIT_ID": "old",
            "CLAUDE_CODE_SESSION_ID": fixture.unit.current_session,
            "ENABLE_TOOL_SEARCH": "true",
        }
        current = pane_record.launch("new", fixture.unit.id, str(fixture.worktree), {}, ["claude"])
        before = retained["environment"].copy()
        self.assertEqual(pane_record.retained_environment(current, [retained]), {
            "CLAUDE_CODE_SESSION_ID": fixture.unit.current_session, "ENABLE_TOOL_SEARCH": "true"})
        current["session_id"] = fixture.worker.current_session
        self.assertEqual(pane_record.retained_environment(current, [retained]), {})
        self.assertEqual(retained["environment"], before)
