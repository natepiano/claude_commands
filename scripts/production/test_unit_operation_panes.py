"""Unit operations close only the unit's exact session-qualified pane after stopping."""
from __future__ import annotations

import json
import io
from contextlib import redirect_stderr
import math
import os
import tempfile
import unittest
from pathlib import Path
from typing import final, override
from unittest import mock

from scripts.production.staff_test_support import StaffTestCase, unit_membership
from scripts.herdr import herdr_types
from scripts.production.fake_unit_host import PaneShellFixture
from scripts.production import unit_move
from scripts.herdr import herdr
from scripts.production import unit_lookup
from scripts.production.rename_state import RenameRefused


@final
class UnitOperationPaneTests(unittest.TestCase):
    @override
    def __init__(self, methodName: str = 'runTest') -> None:
        super().__init__(methodName)
        self.root: Path = Path()
        self.worktree: Path = Path()
        self._pane: PaneShellFixture | None = None

    @property
    def pane(self) -> PaneShellFixture:
        assert self._pane is not None
        return self._pane

    @override
    def setUp(self) -> None:
        staff_fixture = StaffTestCase()
        staff_fixture.setUp()
        self.addCleanup(staff_fixture.doCleanups)
        _ = unit_membership("demo", "alpha")
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.worktree = self.root / 'worktree'
        self.worktree.mkdir()
        self._pane = PaneShellFixture(self, self.worktree, self.root / 'proc', live=False,
                                      env={"XDG_CONFIG_HOME": os.environ["XDG_CONFIG_HOME"]})
        self.enterContext(mock.patch.dict(os.environ, {'SHUTDOWN_STATE_DIR': str(self.root / 'shutdown')}))





    def test_claude_execing_the_pane_shell_needs_no_close(self) -> None:
        self.pane.found = unit_lookup.LabeledSession('alpha',
            unit_lookup.LiveClaude('alpha', self.pane.native_id, 'socket', self.pane.shell), self.pane.place)
        self.assertIsNone(unit_move.unit_pane(self.pane.found, 'demo'))
        self.assertEqual(self.pane.close_calls(), [])



    def test_no_unit_pane_needs_no_close(self) -> None:
        self.assertIsNone(unit_move.unit_pane(None, 'demo'))
        unit_move.close_unit_pane(None)
        self.assertEqual(self.pane.close_calls(), [])

    def test_unknown_director_and_foreign_occupant_do_not_require_pane_proof(self) -> None:
        self.pane.found = unit_lookup.LabeledSession('alpha', unit_lookup.ClaudeUnknown('unreadable'), self.pane.place)
        self.pane.add_occupant(991551, unit='beta')
        with mock.patch.object(herdr, 'herdr_processes', side_effect=AssertionError('no occupant scan')):
            pane = unit_move.unit_pane(self.pane.found, 'demo')
            with self.assertRaisesRegex(RenameRefused, 'other live work'):
                unit_move.close_unit_pane(pane)
        self.assertEqual(self.pane.close_calls(), [])

    def test_close_uses_saved_place_without_record_launch_or_generation_rechecks(self) -> None:
        pane = unit_move.unit_pane(self.pane.found, 'demo')
        self.pane.found = unit_lookup.LabeledSession('alpha',
            unit_lookup.LiveClaude('alpha', 'new-session', 'socket', self.pane.director), self.pane.place)
        with (mock.patch.object(herdr, 'process_action', side_effect=AssertionError('no process proof'))):
            unit_move.close_unit_pane(pane)
        self.assertEqual(self.pane.close_calls(), [['--session', 'agents', 'pane', 'close', 'w1:p1']])

    def test_a_pane_already_gone_counts_as_closed(self) -> None:
        pane = unit_move.unit_pane(self.pane.found, 'demo')
        gone = herdr_types.HerdrFailure('herdr pane close: pane_not_found: pane w1:p1 not found', 'pane_not_found')
        with (mock.patch.object(herdr.Herdr, 'pane_activity', return_value=herdr.PaneAtShell()),
              mock.patch.object(herdr.Herdr, 'panes', side_effect=[
                  [{'pane_id': 'w1:p1', 'terminal_id': 'unit-terminal', 'workspace_id': 'w1', 'session_id': None}], []]),
              mock.patch.object(herdr.Herdr, 'run', side_effect=[
                json.dumps({"result": {"panes": [
                    {"pane_id": "w1:p1", "terminal_id": "unit-terminal", "tab_id": "w1:t1", "workspace_id": "w1"}]}}),
                json.dumps({"result": {"process_info": {
                    "shell_pid": self.pane.shell, "foreground_process_group_id": self.pane.shell,
                    "foreground_processes": [{"pid": self.pane.shell, "name": "zsh", "argv": ["zsh"]}]}}}),
                gone]) as close):
            unit_move.close_unit_pane(pane)
        self.assertEqual(close.call_args_list, [
            mock.call('pane', 'list'), mock.call('pane', 'process-info', '--pane', 'w1:p1'),
            mock.call('pane', 'close', 'w1:p1')])

    def test_herdr_close_error_is_reported_for_the_units_exact_place(self) -> None:
        pane = unit_move.unit_pane(self.pane.found, 'demo')
        failure = herdr_types.HerdrFailure('fixture close failed')
        run = herdr.Herdr.run
        def fail_close(client: herdr.Herdr, *arguments: str, deadline: float = math.inf) -> str:
            if arguments == ('pane', 'close', 'w1:p1'):
                raise failure
            return run(client, *arguments, deadline=deadline)
        with mock.patch.object(herdr.Herdr, 'run', autospec=True, side_effect=fail_close) as close:
            with redirect_stderr(io.StringIO()) as errors:
                unit_move.close_unit_pane(pane)
        self.assertEqual(len(errors.getvalue().splitlines()), 1)
        self.assertIn('fixture close failed', errors.getvalue())
        close.assert_called_with(mock.ANY, 'pane', 'close', 'w1:p1')
        self.assertEqual(close.call_args_list.count(mock.call(mock.ANY, 'pane', 'close', 'w1:p1')), 1)
