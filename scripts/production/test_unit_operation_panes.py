"""Guarded pane closure observes a clean stop, exact owner and fresh exit."""
from __future__ import annotations

import copy
import os
import shutil
import sys
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import final, override
from unittest import mock

from scripts.production.fake_unit_host import PANE_OCCUPANT_MEMBERSHIPS, PaneShellFixture, process, stopped_record
sys.path.insert(0, str(Path(__file__).resolve().parent))
import unit_move
import herdr
import launch_permission
import pane_record
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

    def test_unknown_director_or_missing_shell_evidence_refuses_before_shutdown(self) -> None:
        for unknown_director in (True, False):
            with self.subTest(unknown_director=unknown_director):
                self.pane.found = unit_lookup.MarkedUnit('alpha-unit',
                    unit_lookup.ClaudeUnknown('registry unreadable') if unknown_director else unit_lookup.ClaudeNotRunning(),
                    self.pane.place)
                with (mock.patch.object(pane_record, 'pane_shell_pid', side_effect=ValueError('shell unreadable')),
                      mock.patch.object(rename_unit, 'run_shutdown') as shutdown):
                    with self.assertRaises(RenameRefused):
                        self.stop()
                shutdown.assert_not_called()
                self.assertEqual(self.pane.close_calls(), [])

    def test_claude_execing_the_pane_shell_needs_no_close(self) -> None:
        self.pane.found = unit_lookup.MarkedUnit('alpha-unit',
            unit_lookup.LiveClaude('alpha', 'sid-alpha', 'socket', self.pane.shell), self.pane.place)
        self.assertIsNone(unit_move.unit_pane(self.pane.found, 'demo'))
        self.assertEqual(self.pane.close_calls(), [])

    def test_unknown_claude_refuses_without_querying_or_acting_on_the_terminal(self) -> None:
        unknown = unit_lookup.MarkedUnit('alpha-unit', unit_lookup.ClaudeUnknown('unreadable'), self.pane.place)
        with mock.patch.object(herdr.Herdr, 'result') as terminal:
            with self.assertRaisesRegex(RenameRefused, 'cannot tell whether Claude is running'):
                _ = unit_move.unit_pane(unknown, 'demo')
        terminal.assert_not_called()

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

    def test_admitted_launch_prevents_close_even_before_director_publication(self) -> None:
        claim = unit_move.unit_pane(self.pane.found, 'demo')
        pending = launch_permission.PendingSessionLaunch('sid-alpha', 'owner@example.com', 'alpha', 4242,
                                                         self.pane.place.session, self.pane.place.pane)
        with mock.patch.object(launch_permission, 'pending_launches', return_value=(pending,)):
            with self.assertRaisesRegex(RenameRefused, 'launch in progress'):
                unit_move.close_unit_pane(claim, self.saved)
        self.assertEqual(self.pane.close_calls(), [])

    def test_admitted_launch_with_empty_or_new_conversation_id_keeps_its_pane(self) -> None:
        claim = unit_move.unit_pane(self.pane.found, 'demo')
        for session_id in ('', 'new-conversation'):
            with self.subTest(session_id=session_id):
                process(self.pane.environment, self.pane.shell, 991000, ['shell'], self.pane.marks(),
                        session=self.pane.shell, cwd=self.worktree)
                with launch_permission.admitted_launch('owner@example.com', session_id, 'new-name', lambda: None):
                    launch_permission.claim_launch_pane(self.pane.place.session, self.pane.place.pane)
                    with self.assertRaisesRegex(RenameRefused, 'launch in progress'):
                        unit_move.close_unit_pane(claim, self.saved)
                self.assertEqual(self.pane.close_calls(), [])

    def test_admitted_unit_launch_keeps_the_pane_before_pane_publication(self) -> None:
        claim = unit_move.unit_pane(self.pane.found, 'demo')
        for session_id in ('', 'new-conversation'):
            with self.subTest(session_id=session_id):
                process(self.pane.environment, self.pane.shell, 991000, ['shell'], self.pane.marks(),
                        session=self.pane.shell, cwd=self.worktree)
                # add_unit admits its exact unit id before preflight/open_tab can publish a pane.
                with launch_permission.admitted_launch('owner@example.com', session_id, 'alpha-unit', lambda: None):
                    with self.assertRaisesRegex(RenameRefused, 'launch in progress'):
                        unit_move.close_unit_pane(claim, self.saved)
                self.assertEqual(self.pane.close_calls(), [])

    def test_foreign_uid_occupants_block_the_final_close_scan(self) -> None:
        claim = unit_move.unit_pane(self.pane.found, 'demo')
        stat = Path.stat
        read = Path.read_text
        foreign = self.root / 'proc/991551'

        def observed_uid(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
            result = stat(path, follow_symlinks=follow_symlinks)
            if path == foreign:
                fields = list(result)
                fields[4] = os.getuid() + 1
                return os.stat_result(fields)
            return result

        def observed_stat(path: Path, encoding: str | None = None, errors: str | None = None, *,
                          newline: str | None = None) -> str:
            if path == foreign / 'stat' and not readable:
                raise PermissionError('foreign stat unavailable')
            return read(path, encoding=encoding, errors=errors, newline=newline)

        for membership in PANE_OCCUPANT_MEMBERSHIPS:
            for readable in (True, False):
                with self.subTest(membership=membership, readable=readable):
                    self.pane.unmarked_occupant_scenario(991551, membership)
                    with (mock.patch.object(Path, 'stat', observed_uid),
                          mock.patch.object(Path, 'read_text', observed_stat)):
                        self.assertNotIn(991551, [row.pid for row in herdr.herdr_processes()])
                        with self.assertRaisesRegex(RenameRefused, 'occupant|foreign stat unavailable'):
                            unit_move.close_unit_pane(claim, self.saved)
                    self.assertEqual(self.pane.close_calls(), [])
                    self.assertTrue(foreign.exists())

    def test_unrelated_admitted_launch_does_not_block_the_clean_stop(self) -> None:
        claim = unit_move.unit_pane(self.pane.found, 'demo')
        with launch_permission.admitted_launch('other@example.com', 'other-conversation', 'beta-unit', lambda: None):
            launch_permission.claim_launch_pane('other', self.pane.place.pane)
            unit_move.close_unit_pane(claim, self.saved)
        self.assertEqual(self.pane.close_calls(), [['--session', 'agents', 'pane', 'close', 'w1:p1']])
        self.assertFalse((self.root / 'proc' / str(self.pane.shell)).exists())

    def test_macos_membership_and_generation_do_not_depend_on_linux_cgroups(self) -> None:
        shell = self.pane.shell
        with (mock.patch.dict(os.environ, {'HERDR_PLATFORM':'darwin','HERDR_PS':'/fixture/ps'}),
              mock.patch.object(pane_record, 'pane_shell_pid', return_value=shell),
              mock.patch.object(herdr, 'darwin_process_identity', return_value=herdr.DarwinProcessIdentity('birth', 1)),
              mock.patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess(['ps'], 0,
                  f'{shell} 10 20 ttys1\n991551 {shell} 30 ttys2\n991552 1 20 ttys3\n991553 1 40 ttys1\n991554 1 50 ttys5\n', '')) as observe):
            state = pane_record.pane_shell_state(self.pane.place)
        self.assertEqual(state, pane_record.PaneShellState(shell, 'birth', frozenset({shell, 991551, 991552, 991553})))
        observe.assert_called_once_with(['/fixture/ps', '-axo', 'pid=,ppid=,sess=,tty='],
                                        capture_output=True, text=True, check=True, timeout=5)

    def test_director_resuming_during_the_fresh_process_scan_refuses_close(self) -> None:
        observe = herdr.herdr_processes
        reads = 0
        def scan() -> list[herdr.HerdrProcess]:
            nonlocal reads
            reads += 1
            rows = observe()
            if reads == 2:
                self.pane.parent(self.pane.shell)
                self.pane.found = unit_lookup.MarkedUnit('alpha-unit', unit_lookup.LiveClaude('alpha','sid-alpha','socket',4242), self.pane.place)
            return rows
        with mock.patch.object(herdr, 'herdr_processes', side_effect=scan):
            with self.assertRaisesRegex(RenameRefused, 'resumed|unknown'):
                self.stop()
        self.assertEqual(self.pane.close_calls(), [])

    def test_process_that_exits_during_the_scan_is_not_an_occupant(self) -> None:
        read_groups = herdr.systemd_cgroups
        job = self.root / 'proc/991551'
        for error in (FileNotFoundError, ProcessLookupError, PermissionError):
            with self.subTest(error=error.__name__):
                self.pane.unmarked_occupant_scenario(991551, 'scope')

                def exiting_groups(process: Path, error: type[OSError] = error) -> list[tuple[str, Path]]:
                    if process != job:
                        return read_groups(process)
                    if error is not PermissionError:
                        shutil.rmtree(job)
                    raise error(str(job / 'cgroup'))

                with mock.patch.object(herdr, 'systemd_cgroups', side_effect=exiting_groups):
                    if error is PermissionError:
                        with self.assertRaisesRegex(RenameRefused, 'cannot prove its pane shell safe to close'):
                            _ = unit_move.unit_pane(self.pane.found, 'demo')
                    else:
                        self.assertIsNotNone(unit_move.unit_pane(self.pane.found, 'demo'))
                self.assertEqual(self.pane.close_calls(), [])

    def test_member_that_exits_before_the_herdr_read_is_not_an_occupant(self) -> None:
        listed = herdr.herdr_processes
        job = self.root / 'proc/991551'

        def after_exit() -> list[herdr.HerdrProcess]:
            shutil.rmtree(job)
            return listed()

        self.pane.unmarked_occupant_scenario(991551, 'session')
        with mock.patch.object(herdr, 'herdr_processes', side_effect=after_exit):
            self.assertIsNotNone(unit_move.unit_pane(self.pane.found, 'demo'))
        self.assertEqual(self.pane.close_calls(), [])

    def test_foreign_or_unmarked_occupant_in_owned_pane_blocks_without_close(self) -> None:
        for marked in (True,False):
            with self.subTest(marked=marked):
                self.pane.add_occupant(991551, unit='beta-unit', marked=marked)
                with self.assertRaisesRegex(RenameRefused, 'foreign|unknown'):
                    self.stop()
                self.assertEqual(self.pane.close_calls(), [])

    def test_unmarked_tree_session_or_dedicated_scope_occupant_is_not_invisible(self) -> None:
        for membership in ('descendant', 'session', 'scope'):
            with self.subTest(membership=membership):
                scope = f'/user.slice/{herdr.PANE_SLICE}/pane.scope' if membership == 'scope' else ''
                process(self.pane.environment, self.pane.shell, 991000, ['arbitrary-shell-name'], self.pane.marks(),
                        session=self.pane.shell, cwd=self.worktree, scope=scope)
                process(self.pane.environment, 991551, self.pane.shell if membership == 'descendant' else 1,
                        ['unmarked'], {}, session=self.pane.shell if membership == 'session' else 991551,
                        cwd=self.worktree, scope=scope)
                with self.assertRaisesRegex(RenameRefused, 'unknown occupant'):
                    self.stop()
                self.assertEqual(self.pane.close_calls(), [])

    def test_shells_own_marks_are_required_even_with_an_owned_child(self) -> None:
        self.pane.add_occupant(self.pane.shell, unit='beta-unit')
        self.pane.add_occupant(991552, unit='alpha-unit')
        with self.assertRaisesRegex(RenameRefused, 'own marks'):
            self.stop()
        self.assertEqual(self.pane.close_calls(), [])

    def test_changed_shell_generation_or_stop_request_never_closes(self) -> None:
        observed = pane_record.pane_shell_state
        def replaced(place: herdr.HerdrPlace) -> pane_record.PaneShellState:
            result = observed(place)
            return result._replace(started='new')
        with mock.patch.object(rename_unit, '_stop_runs', return_value=None):
            claim = unit_move.unit_pane(self.pane.found, 'demo')
            with mock.patch.object(pane_record, 'pane_shell_state', side_effect=replaced):
                with self.assertRaisesRegex(RenameRefused, 'generation'):
                    unit_move.close_unit_pane(claim, self.saved)
            expected = copy.deepcopy(self.saved)
            expected['request_id'] = 'other'
            with self.assertRaisesRegex(RenameRefused, 'same completed'):
                unit_move.close_unit_pane(claim, expected)
        self.assertEqual(self.pane.close_calls(), [])
