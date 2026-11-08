"""Rename the displays without changing the staff session."""
from __future__ import annotations

import unittest
from collections.abc import Mapping
from dataclasses import replace
from unittest import mock

from scripts.herdr import herdr
from scripts.production import staff
from scripts.production.rename_unit_test_support import UnitRenameFixture


class UnitRenameSessionsTests(UnitRenameFixture):
    def test_recordless_rename_keeps_the_staff_session_without_a_launch(self) -> None:
        row = next(row for row in staff.find(kind="unit") if row.name == "alpha")
        panes: list[herdr.PaneIdentity] = [
            {"pane_id": "w1:p1", "workspace_id": "w1", "terminal_id": "t1",
             "label": "alpha", "session_id": row.current_session},
            {"pane_id": "w1:p2", "workspace_id": "w1", "terminal_id": "t2",
             "label": "other", "session_id": "unrelated-session"},
        ]
        def native_process(pane: str, kind: str, timeout: float, *,
                           pane_row: Mapping[str, object] | herdr.PaneNotRead = herdr.PANE_NOT_READ) -> herdr.NativeAgentProcess:
            del timeout, pane_row
            session = row.current_session if pane == "w1:p1" else "unrelated-session"
            identity = herdr.RolloutThread(session) if kind == "codex" else herdr.EnvironmentThread(session)
            return herdr.NativeAgentProcess(identity, "fixture-generation", {})

        with (mock.patch.object(herdr.Herdr, "panes", return_value=panes),
              mock.patch.object(herdr.Herdr, "native_pane_process", side_effect=native_process),
              mock.patch.object(herdr.Herdr, "retitle", return_value=True) as displays,
              mock.patch.object(herdr.Herdr, "start_agent") as launches):
            self.assert_ok(self.rename("apply"))
        displays.assert_called_once_with("w1:p1", "gamma")
        launches.assert_not_called()
        self.assertEqual(staff.by_id(row.id), replace(
            row, name="gamma", worktree=staff.PresentWorktree("natedev", self.rust / "claude-demo-gamma")))


if __name__ == "__main__":
    _ = unittest.main()
