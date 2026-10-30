"""One preflight decision identifies pane-shell units without excluding work."""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import final, override
from unittest import mock

from scripts.production.unit_operation_test_support import PaneShellFixture
import herdr
import pane_record
import rename_unit
import retire_unit
import unit_lookup
import unit_move
from rename_state import RenameRefused


@final
class UnitOperationPaneTests(unittest.TestCase):
    worktree: Path = Path()
    pane: PaneShellFixture  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        self.worktree = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.pane = PaneShellFixture(self, self.worktree, self.worktree / "proc", live=True)

    def test_director_parent_must_be_the_exact_panes_reported_shell(self) -> None:
        self.assertIn(f"pane shell {self.pane.shell}",
                      unit_move.pane_shell_refusal(self.pane.found, self.worktree))
        self.pane.parent(self.pane.shell + 1)
        self.assertEqual(unit_move.pane_shell_refusal(self.pane.found, self.worktree), "")

    def test_refusal_uses_the_launch_recordings_process_parent_reader(self) -> None:
        with mock.patch.object(pane_record, "process_parent", return_value=self.pane.shell + 1, create=True) as parent:
            self.assertEqual(unit_move.pane_shell_refusal(self.pane.found, self.worktree), "")
            parent.assert_called_once_with(self.pane.director)

    def test_claude_execing_the_pane_shell_can_proceed(self) -> None:
        self.pane.info["shell_pid"] = self.pane.director
        self.assertEqual(unit_move.pane_shell_refusal(self.pane.found, self.worktree), "")

    def test_stopped_shell_refuses_only_while_inside_the_worktree(self) -> None:
        stopped = unit_lookup.MarkedUnit("alpha-unit", unit_lookup.ClaudeNotRunning(), self.pane.place)
        self.assertIn(f"pane shell {self.pane.shell}", unit_move.pane_shell_refusal(stopped, self.worktree))
        self.pane.inside.clear()
        self.assertEqual(unit_move.pane_shell_refusal(stopped, self.worktree), "")

    def test_unmarked_work_remains_a_raw_process_blocker(self) -> None:
        self.assertEqual(unit_move.pane_shell_refusal(None, self.worktree), "")
        target = rename_unit.UnitRename("demo", "alpha-unit", "gamma-unit", "", "", "", "", "",
                                        str(self.worktree), str(self.worktree), "", "")
        with (mock.patch.object(unit_lookup, "marked_units", return_value={}),
              mock.patch.object(rename_unit, "_refuse_detached_worktree"),
              mock.patch.object(rename_unit, "_retained_command_sessions", return_value=set()),
              mock.patch.object(retire_unit, "run_processes", return_value={}),
              mock.patch.object(os, "getsid", return_value=123)):
            with self.assertRaisesRegex(RenameRefused, f"processes {self.pane.shell}, {self.pane.director} work inside"):
                rename_unit.check_stoppable(target)

    def test_darwin_parent_observation_uses_the_same_pane_shell_rule(self) -> None:
        with (mock.patch.dict(os.environ, {"HERDR_PLATFORM": "darwin", "HERDR_PS": "/fixture/ps"}),
              mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess(
                  ["ps"], 0, str(self.pane.shell), "")) as parent):
            self.assertIn(f"pane shell {self.pane.shell}",
                          unit_move.pane_shell_refusal(self.pane.found, self.worktree))
            parent.assert_called_once_with(["/fixture/ps", "-o", "ppid=", "-p", str(self.pane.director)],
                                           capture_output=True, text=True, check=True, timeout=5)

    def test_unreadable_shell_identity_or_parent_refuses(self) -> None:
        self.pane.info.clear()
        with self.assertRaisesRegex(RenameRefused, "cannot read its pane shell"):
            _ = unit_move.pane_shell_refusal(self.pane.found, self.worktree)
        self.pane.info["shell_pid"] = self.pane.shell
        (Path(self.pane.environment["HERDR_PROC"]) / str(self.pane.director) / "stat").unlink()
        with self.assertRaisesRegex(RenameRefused, "cannot read its pane shell"):
            _ = unit_move.pane_shell_refusal(self.pane.found, self.worktree)

    def test_unknown_claude_refuses_without_querying_or_acting_on_the_terminal(self) -> None:
        unknown = unit_lookup.MarkedUnit("alpha-unit", unit_lookup.ClaudeUnknown("unreadable"), self.pane.place)
        with mock.patch.object(herdr.Herdr, "result") as terminal:
            with self.assertRaisesRegex(RenameRefused, "cannot tell whether Claude is running"):
                _ = unit_move.pane_shell_refusal(unknown, self.worktree)
            terminal.assert_not_called()
