"""Unit operations close only the unit's exact session-qualified pane after stopping."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import final, override
from unittest import mock

from scripts.production.fake_unit_host import PaneShellFixture, stopped_record
sys.path.insert(0, str(Path(__file__).resolve().parent))
import unit_move
import herdr
import launch_permission
import rename_unit
import retire_unit
import shutdown_record
import unit_lookup
from rename_state import RenameRefused


@final
class UnitOperationPaneTests(unittest.TestCase):
    @override
    def __init__(self, methodName: str = 'runTest') -> None:
        super().__init__(methodName)
        self.root: Path = Path()
        self.worktree: Path = Path()
        self._pane: PaneShellFixture | None = None
        self.saved: shutdown_record.SessionShutdown = stopped_record(Path(), Path())

    @property
    def pane(self) -> PaneShellFixture:
        assert self._pane is not None
        return self._pane

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.worktree = self.root / 'worktree'
        self.worktree.mkdir()
        self._pane = PaneShellFixture(self, self.worktree, self.root / 'proc', live=False)
        self.enterContext(mock.patch.dict(os.environ, {'SHUTDOWN_STATE_DIR': str(self.root / 'shutdown')}))
        self.saved = stopped_record(self.root, self.worktree)
        self.assertTrue(shutdown_record.create_session(self.saved))

    def target(self) -> rename_unit.UnitRename:
        return rename_unit.UnitRename('demo','alpha-unit','gamma-unit','','','','','',str(self.worktree),'','','')

    def stop(self) -> None:
        def records(_target: rename_unit.StopTarget) -> list[shutdown_record.SessionShutdown]:
            return shutdown_record.session_records('owner@example.com')
        def cleanup(_target: rename_unit.StopTarget) -> None:
            self.pane.lifecycle.append('workers cleaned')
        with (mock.patch.object(rename_unit, '_refuse_detached_worktree'),
              mock.patch.object(rename_unit, '_unit_records', side_effect=records),
              mock.patch.object(rename_unit, '_stop_runs', side_effect=cleanup)):
            rename_unit.stop_unit(self.target(), lambda: None)

    def test_clean_stop_closes_only_the_exact_session_pane_after_worker_cleanup(self) -> None:
        self.pane.add_occupant(991555, unit='beta-unit', session='other')
        self.assertEqual([row.place for row in herdr.herdr_processes() if row.pid == 991555],
                         [herdr.HerdrPlace('other', 'w1:p1')])
        run = herdr.Herdr.run
        def close_after_cleanup(client: herdr.Herdr, *arguments: str) -> str:
            if arguments[:2] == ('pane', 'close'):
                self.assertEqual(self.pane.lifecycle, ['workers cleaned'])
            return run(client, *arguments)
        with mock.patch.object(herdr.Herdr, 'run', close_after_cleanup):
            self.stop()
        self.assertEqual(self.pane.close_calls(), [['--session','agents','pane','close','w1:p1']])
        self.assertEqual(self.pane.lifecycle, ['workers cleaned'])
        self.assertTrue((self.root / 'proc/991555').exists(), 'another session reuses this pane id')

    def test_identity_lost_with_worker_cleanup_issues_never_closes_the_pane(self) -> None:
        def incomplete(saved: shutdown_record.SessionShutdown) -> None:
            saved['progress'] = {'kind':'process identity lost','at':'2026-10-09T21:50:00+00:00'}
            saved['stop_issues'] = [{'kind':'codex server left running','at':'2026-10-09T21:50:00+00:00','run_dir':'/fixture/run','cause':'stop not confirmed'}]
        _ = shutdown_record.update_session('owner@example.com','sid-alpha', incomplete)
        self.pane.add_occupant(991551, unit='alpha-unit')
        with self.assertRaisesRegex(RenameRefused, 'cleanup|clean stop'):
            self.stop()
        self.assertEqual(self.pane.close_calls(), [])
        self.assertTrue((self.root / 'proc/991551').exists())

    def test_claude_execing_the_pane_shell_needs_no_close(self) -> None:
        self.pane.found = unit_lookup.MarkedUnit('alpha-unit',
            unit_lookup.LiveClaude('alpha', 'sid-alpha', 'socket', self.pane.shell), self.pane.place)
        self.assertIsNone(unit_move.unit_pane(self.pane.found, 'demo'))
        self.assertEqual(self.pane.close_calls(), [])

    def test_unmarked_work_remains_a_raw_process_blocker(self) -> None:
        self.pane.parent(self.pane.shell)
        with (mock.patch.object(unit_lookup, 'marked_units', return_value={}),
              mock.patch.object(rename_unit, '_refuse_detached_worktree'),
              mock.patch.object(rename_unit, '_retained_command_sessions', return_value=set()),
              mock.patch.object(retire_unit, 'run_processes', return_value={}),
              mock.patch.object(os, 'getsid', return_value=123)):
            with self.assertRaisesRegex(RenameRefused, f'processes {self.pane.director}, {self.pane.shell} work inside'):
                rename_unit.check_stoppable(self.target())
        self.assertEqual(self.pane.close_calls(), [])

    def test_unrelated_admitted_launch_does_not_block_the_clean_stop(self) -> None:
        claim = unit_move.unit_pane(self.pane.found, 'demo')
        with launch_permission.admitted_launch('other@example.com', 'other-conversation', 'beta-unit', lambda: None):
            launch_permission.claim_launch_pane('other', self.pane.place.pane)
            unit_move.close_unit_pane(claim)
        self.assertEqual(self.pane.close_calls(), [['--session', 'agents', 'pane', 'close', 'w1:p1']])
        self.assertFalse((self.root / 'proc' / str(self.pane.shell)).exists())

    def test_no_unit_pane_needs_no_close(self) -> None:
        self.assertIsNone(unit_move.unit_pane(None, 'demo'))
        unit_move.close_unit_pane(None)
        self.assertEqual(self.pane.close_calls(), [])

    def test_unknown_director_and_foreign_occupant_do_not_require_pane_proof(self) -> None:
        self.pane.found = unit_lookup.MarkedUnit('alpha-unit', unit_lookup.ClaudeUnknown('unreadable'), self.pane.place)
        self.pane.add_occupant(991551, unit='beta-unit')
        with mock.patch.object(herdr, 'herdr_processes', side_effect=AssertionError('no occupant scan')):
            pane = unit_move.unit_pane(self.pane.found, 'demo')
            unit_move.close_unit_pane(pane)
        self.assertEqual(self.pane.close_calls(), [['--session', 'agents', 'pane', 'close', 'w1:p1']])

    def test_close_uses_saved_place_without_record_launch_or_generation_rechecks(self) -> None:
        pane = unit_move.unit_pane(self.pane.found, 'demo')
        self.pane.found = unit_lookup.MarkedUnit('alpha-unit',
            unit_lookup.LiveClaude('alpha', 'new-session', 'socket', self.pane.director), self.pane.place)
        self.saved['progress'] = {'kind': 'waiting'}
        with (launch_permission.admitted_launch('owner@example.com', 'sid-alpha', 'alpha-unit', lambda: None),
              mock.patch.object(shutdown_record, 'update_session', side_effect=AssertionError('no record proof')),
              mock.patch.object(herdr, 'process_action', side_effect=AssertionError('no process proof'))):
            launch_permission.claim_launch_pane(self.pane.place.session, self.pane.place.pane)
            unit_move.close_unit_pane(pane)
        self.assertEqual(self.pane.close_calls(), [['--session', 'agents', 'pane', 'close', 'w1:p1']])

    def test_a_pane_already_gone_counts_as_closed(self) -> None:
        pane = unit_move.unit_pane(self.pane.found, 'demo')
        gone = herdr.HerdrFailure('herdr pane close: pane_not_found: pane w1:p1 not found', 'pane_not_found')
        with mock.patch.object(herdr.Herdr, 'run', side_effect=gone) as close:
            unit_move.close_unit_pane(pane)
        close.assert_called_once_with('pane', 'close', 'w1:p1')

    def test_herdr_close_error_is_reported_for_the_units_exact_place(self) -> None:
        pane = unit_move.unit_pane(self.pane.found, 'demo')
        failure = herdr.HerdrFailure('fixture close failed')
        with mock.patch.object(herdr.Herdr, 'run', side_effect=failure) as close:
            with self.assertRaisesRegex(RenameRefused, 'alpha-unit: pane close failed: fixture close failed'):
                unit_move.close_unit_pane(pane)
        close.assert_called_once_with('pane', 'close', 'w1:p1')
