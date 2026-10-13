#!/usr/bin/env python3
"""Behavioral tests for restoring every session in a shutdown record."""

from __future__ import annotations

import copy
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Callable, Iterable, Sequence
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Literal, cast, final, override
from unittest.mock import patch

from test_support import registry_confirmation, ShutdownSendLeaf, ShutdownTestCase
import inventory
import record
import sessions
from user_action import ActionRequired
import restart
import conversation_pause
import settle
import showrunner_footer
import shutdown
from account import Account, UnknownAccountName, UnreadableAccount
from record import SessionShutdown, SessionProgress, TimerRestore, Where

LOGIN = "owner@example.com"
LABEL = "claude 2"
STOPPED = "2026-10-09T21:49:10+00:00"
RESTARTED = "2026-10-10T01:02:03+00:00"
EMPTY_SESSION_IDS: frozenset[str] = frozenset()

def named_prefix(config_dir: str) -> str:
    """The assignments a session resumed on a named account's directory starts with."""
    return (
        f"CLAUDE_CONFIG_DIR={shlex.quote(config_dir)} "
        f"CLAUDE_CODE_REMOTE_MEMORY_DIR={shlex.quote(str(Path.home() / '.claude'))} "
    )


def checkout() -> inventory.GitCheckout:
    return inventory.GitCheckout(
        kind="git",
        path="/tmp/checkout",
        head=inventory.OnBranch(kind="branch", name="work"),
        upstream=inventory.Tracking(kind="tracking", ahead=0),
        dirty=[],
    )


def tmux_host(tmux_session: str) -> inventory.TmuxHost:
    return inventory.TmuxHost(
        kind="tmux",
        tmux_session=tmux_session,
        pane=inventory.PaneNotRecorded(kind="not recorded"),
    )


def recorded_tmux_host(
    tmux_session: str, pane_id: str, pane_pid: int
) -> inventory.TmuxHost:
    return inventory.TmuxHost(
        kind="tmux",
        tmux_session=tmux_session,
        pane=inventory.TmuxPane(
            kind="pane", pane_id=pane_id, pane_pid=pane_pid
        ),
    )


def common_session(
    session_id: str,
    name: str | None = None,
    *,
    cwd: str | None = None,
    model: inventory.LastModel | None = None,
    run_dirs: list[str] | None = None,
) -> inventory.SessionFields:
    return {
        "session_id": session_id,
        "pid": 1000 + len(session_id),
        "proc_start": f"start-{session_id}",
        "name": name or session_id,
        "cwd": cwd or f"/tmp/{session_id}",
        "status": "idle",
        "model": model or inventory.NoReplyYet(kind="no reply yet"),
        "checkout": checkout(),
        "run_dirs": list(run_dirs or []),
        "codex_servers": [],
        "timers": [],
    }


def top_level(
    session_id: str,
    *,
    name: str | None = None,
    cwd: str | None = None,
    model: inventory.LastModel | None = None,
    host: inventory.SessionHost | None = None,
    run_dirs: list[str] | None = None,
) -> inventory.TopLevelSession:
    return inventory.TopLevelSession(
        **common_session(
            session_id,
            name,
            cwd=cwd,
            model=model,
            run_dirs=run_dirs,
        ),
        kind="top-level",
        host=host or inventory.UnknownHost(kind="unknown"),
    )


def showrunner(
    session_id: str,
    *,
    host: inventory.SessionHost | None = None,
) -> inventory.ShowrunnerSession:
    return inventory.ShowrunnerSession(
        **common_session(session_id),
        kind="showrunner",
        host=host or tmux_host(f"tmux-{session_id}"),
        production="demo",
        doc="/tmp/demo production.md",
    )


def unit(
    session_id: str,
    *,
    plan: inventory.UnitPlan | None = None,
    tmux_session: str | None = None,
    run_dirs: list[str] | None = None,
    doc: str = "/tmp/demo production.md",
) -> inventory.UnitSession:
    return inventory.UnitSession(
        **common_session(session_id, run_dirs=run_dirs),
        kind="unit",
        host=inventory.UnitHost(
            kind="unit",
            production="demo",
            unit=f"{session_id}-unit",
            doc=doc,
            tmux_session=tmux_session or f"tmux-{session_id}",
            plan=plan or inventory.PlanFromRunRecord(kind="plan", path=f"/tmp/{session_id} plan.md"),
        ),
    )


def seat(
    session_id: str,
    *,
    owner: str | None,
    run_dirs: list[str] | None = None,
) -> inventory.SeatSession:
    seat_owner: inventory.SeatOwner = (
        inventory.DirectorOwner(kind="director", session_id=owner)
        if owner is not None
        else inventory.NoDirector(kind="no director")
    )
    return inventory.SeatSession(
        **common_session(session_id, run_dirs=run_dirs),
        kind="seat",
        host=inventory.UnknownHost(kind="unknown"),
        owner=seat_owner,
    )


def entry(
    session: inventory.Session, *, state: SessionProgress | None = None,
    where: Where | None = None, timers: list[TimerRestore] | None = None,
) -> SessionShutdown:
    return SessionShutdown(
        login=LOGIN, label=LABEL, machine="natedev", requested_at=STOPPED,
        request_id="request", requested_by=record.FromTerminal(kind="terminal"),
        hold="session", then="restart", force="wait for ready", alerts="user",
        session=session, seats=[], timers=list(timers or []),
        settle_message=record.SettleMessageNotSent(kind="not sent"),
        where=where or record.WhereSaid(kind="said", text=f"working on {session['name']}", at=STOPPED),
        progress=state or record.SessionStopped(kind="stopped", at=STOPPED), stop_issues=[],
    )


def records_for_test(
    records: list[SessionShutdown], *, machine: str = "natedev",
    requester: str | None = None, stopped_at: str = STOPPED,
) -> list[SessionShutdown]:
    for item in records:
        item["machine"] = machine
        item["requested_at"] = stopped_at
        if requester is not None:
            item["requested_by"] = record.FromSession(kind="session", session_id=requester)
    return records


def session_shutdown(
    session_id: str, *, machine: str = "natedev", hold: Literal["account", "session"] = "account",
) -> SessionShutdown:
    item = entry(top_level(session_id))
    item["machine"] = machine
    item["hold"] = hold
    return item


def peer_json(*, outcome: str = "done", lines: Sequence[str] = (), missing: Sequence[record.NoRemoteControl] = (), refused: Sequence[restart.RestartRefusal] = ()) -> str:
    return json.dumps({"machine": "Mac", "label": LABEL, "outcome": outcome, "lines": lines,
        "no_remote_control": missing, "refused": refused})


def setUpModule() -> None:
    """The times these tests expect are Pacific; the user's zone setting must not change them."""
    unittest.enterModuleContext(patch.dict(os.environ, {"USER_ZONE": "America/Los_Angeles"}))


@final
class CommandRecorder:
    calls: list[list[str]]

    def __init__(self) -> None:
        self.calls = []

    def __call__(
        self, argv: list[str], *, dry_run: bool, check: bool = True
    ) -> subprocess.CompletedProcess[str] | None:
        del check
        self.calls.append(argv)
        if dry_run:
            print(shlex.join(argv))
            return None
        return subprocess.CompletedProcess(argv, 0, "", "")


@final
class RestartTests(ShutdownTestCase):
    root: Path = Path()
    state_root: Path = Path()
    desktop_state: restart.DesktopLaunchState = restart.DesktopLaunchState()
    account = Account("claude", LOGIN, LABEL)

    @override
    def setUp(self) -> None:
        super().setUp()
        _ = self.enterContext(patch.object(sessions, "remote_control", return_value="active"))
        self.desktop_state = restart.DesktopLaunchState()
        self.addCleanup(self.desktop_state.restore, dry_run=True)
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.state_root = self.root / "shutdown"
        environment = cast(object, self.enterContext(patch.dict(os.environ, {
            "HOME": str(self.root), "SHUTDOWN_STATE_DIR": str(self.state_root),
            "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"),
            "CONVERSATION_PAUSE_STATE_DIR": str(self.root / "pause"),
            "AGENT_NOTES_DIR": str(self.root / "notes"),
        })))
        del environment
        _ = self.enterContext(patch.object(settle, "record_time", return_value=RESTARTED))
        _ = self.enterContext(patch.object(settle, "inventory", return_value=inventory.Inventory(machine="natedev", login=LOGIN, label=LABEL, sessions=[], unattributed=[])))

    def create(self, records: list[SessionShutdown]) -> None:
        for item in records:
            self.assertTrue(record.create_session(item))

    def stored_record(self) -> SessionShutdown:
        return record.session_records(LOGIN)[0]

    @staticmethod
    def all_sessions_live(ids: frozenset[str], _timeout: float) -> restart.RegistryConfirmation:
        return registry_confirmation(ids)

    def test_notes_use_the_stop_dates_zone_and_explain_a_missing_where(self) -> None:
        october = entry(top_level('october'), where=record.WhereSaid(kind='said', text="quoted 'work' and $()", at=STOPPED))
        october_note = restart.restart_note(records_for_test([october])[0], RESTARTED)
        self.assertIn('stopped 2026-10-09 14:49 PDT', october_note)
        self.assertIn('restarted 2026-10-09 18:02 PDT', october_note)
        self.assertIn("Before it you wrote: quoted 'work' and $().", october_note)
        january_time = '2026-01-15T17:30:00+00:00'
        january = entry(top_level('january'), where=record.WhereNotSaid(kind='not said'))
        january_note = restart.restart_note(records_for_test([january], stopped_at=january_time)[0], '2026-01-15T18:00:00+00:00')
        self.assertIn('stopped 2026-01-15 09:30 PST', january_note)
        self.assertIn('restarted 2026-01-15 10:00 PST', january_note)
        self.assertIn('It left no note of where it was', january_note)
        self.assertNotIn('Before it you wrote', january_note)

    def test_restart_note_adds_only_missing_sentence_punctuation(self) -> None:
        for summary in ('finished the work.', 'finished the work'):
            with self.subTest(summary=summary):
                restored = entry(top_level('punctuation'), where=record.WhereSaid(kind='said', text=summary, at=STOPPED))
                note = restart.restart_note(records_for_test([restored])[0], RESTARTED)
                self.assertIn('Before it you wrote: finished the work. First run', note)
                self.assertNotIn('finished the work.. First run', note)

    def test_restart_prompts_are_scheduled_before_showrunner_and_window_launches(self) -> None:
        self.allow_leaf(ShutdownSendLeaf.RESTART_RETIRE)
        restored_entries = [entry(showrunner('scheduled-showrunner')), entry(top_level('scheduled-window', host=inventory.WindowHost(kind='ghostty', window_shell=10, desktop=inventory.DesktopNotInSnapshot(kind='not in snapshot')))), entry(top_level('scheduled-tmux', host=recorded_tmux_host('scheduled-tmux-host', '%scheduled', 410)))]
        saved = records_for_test(restored_entries)
        self.create(saved)
        expected_prompts: dict[str, str] = {}
        for restored in restored_entries:
            session_id = restored['session']['session_id']
            expected_prompts[session_id] = '/showrunner:produce /tmp/demo production.md resume' if restored['session']['kind'] == 'showrunner' else restart.restart_note(restored, RESTARTED)
            conversation_pause.record_scheduled_prompts(session_id, (f'wakeup for {session_id}',))
        observed_launches: list[str] = []

        def run(argv: list[str], *, dry_run: bool, check: bool=True) -> subprocess.CompletedProcess[str] | None:
            del check
            if dry_run:
                return None
            shell_command = argv[-1]
            if not shell_command.endswith('; exec zsh'):
                return subprocess.CompletedProcess(argv, 0, '', '')
            launched_argv = shlex.split(shell_command.removesuffix('; exec zsh'))
            if '--resume' not in launched_argv:
                return subprocess.CompletedProcess(argv, 0, '', '')
            resume_index = launched_argv.index('--resume')
            session_id = launched_argv[resume_index + 1]
            prompt = expected_prompts[session_id]
            launched_prompt = launched_argv[-1]
            self.assertEqual(launched_prompt, prompt)
            self.assertEqual(conversation_pause.read_scheduled_prompts(session_id), (f'wakeup for {session_id}', prompt))
            self.assertIs(conversation_pause.prompt_source(launched_prompt, lambda: frozenset(), lambda: conversation_pause.read_scheduled_prompts(session_id)), conversation_pause.PromptSource.SCHEDULED)
            observed_launches.append(session_id)
            return subprocess.CompletedProcess(argv, 0, '', '')

        def probe(argv: list[str], *, capture_output: bool, text: bool, check: bool) -> subprocess.CompletedProcess[str]:
            del capture_output, text, check
            if argv[:2] == ['tmux', 'display']:
                return subprocess.CompletedProcess(argv, 0, 'scheduled-tmux-host\t410\t0\n', '')
            if argv[:1] == ['pgrep']:
                return subprocess.CompletedProcess(argv, 1, '', '')
            return subprocess.CompletedProcess(argv, 0, '', '')
        with patch.object(restart, '_run', run), patch.object(restart, '_live_session_ids', return_value=frozenset()), patch.object(restart, 'wait_for_confirmation', side_effect=self.all_sessions_live), patch.object(settle, 'send_message', return_value=settle.MessageSent(kind='sent')), patch.object(settle, 'record_time', return_value=RESTARTED), patch('subprocess.run', probe):
            self.assertEqual(restart.up(LOGIN), 0)
        self.assertCountEqual(observed_launches, ['scheduled-showrunner', 'scheduled-window', 'scheduled-tmux'])

    def test_unit_launch_leaves_prompt_recording_to_add_unit(self) -> None:
        self.allow_leaf(ShutdownSendLeaf.RESTART_RETIRE)
        restored = entry(unit('scheduled-unit'))
        saved = records_for_test([restored])
        self.create(saved)
        conversation_pause.record_scheduled_prompts('scheduled-unit', ('existing wakeup',))
        observed_unit_launch = False

        def run(argv: list[str], *, dry_run: bool, check: bool=True) -> subprocess.CompletedProcess[str] | None:
            nonlocal observed_unit_launch
            del check
            if any((part.endswith('add_unit.py') for part in argv)):
                observed_unit_launch = True
                self.assertEqual(conversation_pause.read_scheduled_prompts('scheduled-unit'), ('existing wakeup',))
            return None if dry_run else subprocess.CompletedProcess(argv, 0, '', '')
        with patch.object(restart, '_run', run), patch.object(restart, '_live_session_ids', return_value=frozenset()), patch.object(restart, 'wait_for_confirmation', side_effect=self.all_sessions_live), patch.object(settle, 'record_time', return_value=RESTARTED):
            self.assertEqual(restart.up(LOGIN), 0)
        self.assertTrue(observed_unit_launch)

    def test_dry_run_does_not_record_restart_notes(self) -> None:
        restored = entry(top_level('dry-scheduled', host=tmux_host('dry-scheduled-host')))
        self.create(records_for_test([restored]))
        conversation_pause.record_scheduled_prompts('dry-scheduled', ('existing wakeup',))
        with patch.object(restart, '_live_session_ids', return_value=frozenset()), patch('subprocess.run', return_value=subprocess.CompletedProcess(['probe'], 1, '', '')), redirect_stdout(io.StringIO()):
            self.assertEqual(restart.up(LOGIN, dry_run=True), 0)
        self.assertEqual(conversation_pause.read_scheduled_prompts('dry-scheduled'), ('existing wakeup',))

    def test_dry_run_prints_each_host_argv_and_changes_nothing(self) -> None:
        entries = [entry(unit('unit')), entry(top_level('ghostty', host=inventory.WindowHost(kind='ghostty', window_shell=10, desktop=inventory.NamedDesktop(kind='named', name='Work Desk')))), entry(top_level('zed', host=inventory.WindowHost(kind='zed', window_shell=11, desktop=inventory.DesktopNotInSnapshot(kind='not in snapshot')))), entry(top_level('terminal', host=inventory.TerminalHost(kind='terminal'))), entry(top_level('tmux', host=recorded_tmux_host('kept tmux', '%dry', 420))), entry(top_level('unknown'))]
        original = records_for_test(entries)
        self.create(original)

        def tmux_exists(argv: list[str], *, capture_output: bool, text: bool, check: bool, env: dict[str, str] | None=None) -> subprocess.CompletedProcess[str]:
            del capture_output, text, check, env
            if argv[:2] == ['tmux', 'display']:
                return subprocess.CompletedProcess(argv, 0, 'kept tmux\t420\t0\n', '')
            if argv[:1] == ['pgrep']:
                return subprocess.CompletedProcess(argv, 1, '', '')
            return subprocess.CompletedProcess(argv, 0, '', '')
        output = io.StringIO()
        with patch.object(restart, '_live_session_ids', return_value=frozenset()), patch.object(restart, '_desktop_numbers', return_value={'Work Desk': 2}), patch('subprocess.run', tmux_exists), patch.object(settle, 'record_time', return_value=RESTARTED), redirect_stdout(output):
            self.assertEqual(restart.up(LOGIN, dry_run=True), 0)
        lines = output.getvalue().splitlines()
        argvs = [shlex.split(line) for line in lines if not line.startswith('manual restart')]
        unit_argv = next((argv for argv in argvs if any((part.endswith('add_unit.py') for part in argv))))
        self.assertEqual(unit_argv[unit_argv.index('--production') + 1], '/tmp/demo production.md')
        self.assertEqual(unit_argv[unit_argv.index('--plan') + 1], '/tmp/unit plan.md')
        self.assertEqual(unit_argv[unit_argv.index('--session-name') + 1], 'unit')
        self.assertEqual(unit_argv[unit_argv.index('--tmux-session') + 1], 'tmux-unit')
        self.assertTrue(any((argv == ['kdotool', 'set_desktop', '2'] for argv in argvs)))
        window_argvs = [argv for argv in argvs if argv[:1] == ['systemd-run']]
        self.assertEqual(len(window_argvs), 2)
        self.assertTrue(all(('ghostty' in argv for argv in window_argvs)))
        terminal = next((argv for argv in argvs if argv[:1] == ['open']))
        self.assertEqual(terminal[:4], ['open', '-na', '/Applications/Nix Apps/Ghostty.app', '--args'])
        tmux = next((argv for argv in argvs if argv[:2] == ['tmux', 'respawn-pane']))
        self.assertIn('%dry', tmux)
        self.assertIn(['tmux', 'display', '-p', '-t', '%dry', '#{session_name}\t#{pane_pid}\t#{pane_dead}'], argvs)
        self.assertIn(['tmux', 'has-session', '-t', '=kept tmux'], argvs)
        self.assertIn(['pgrep', '-P', '420'], argvs)
        self.assertTrue(any((line.startswith('manual restart unknown:') for line in lines)))
        self.assertCountEqual(record.session_records(LOGIN), original)
        self.assertEqual(list(self.state_root.rglob('restart-note-*.txt')), [])

    def test_unit_resumes_with_its_units_row_plan_when_its_run_record_names_another(self) -> None:
        doc = self.root / 'production.md'
        _ = doc.write_text('\n'.join(['# Demo', '', '## Units', '', '| Unit | Plan | Worktree | Branch | Port | Owns |', '| --- | --- | --- | --- | --- | --- |', '| tables-unit | docs/as-built/tables.md | /w/tables | demo-tables | — | `x/` |', '']), encoding='utf-8')
        moved = entry(unit('tables', doc=str(doc), plan=inventory.PlanFromRunRecord(kind='plan', path='docs/plans/demo-tables-followups.md')))
        self.create(records_for_test([moved]))
        output = io.StringIO()
        with patch.object(restart, '_live_session_ids', return_value=frozenset()), patch.object(settle, 'record_time', return_value=RESTARTED), redirect_stdout(output):
            self.assertEqual(restart.up(LOGIN, dry_run=True), 0)
        argv = next((shlex.split(line) for line in output.getvalue().splitlines() if 'add_unit.py' in line))
        self.assertEqual(argv[argv.index('--plan') + 1], 'docs/as-built/tables.md')

    def test_dry_run_uses_recorded_dir_for_each_non_unit_host(self) -> None:
        config_dir = str(self.root / "account with 'quotes' and $()")
        restored_entries = [entry(showrunner('named-showrunner')), entry(top_level('named-tmux', host=tmux_host('named-tmux-host'))), entry(top_level('named-terminal', host=inventory.TerminalHost(kind='terminal'))), entry(top_level('named-window', host=inventory.WindowHost(kind='ghostty', window_shell=10, desktop=inventory.DesktopNotInSnapshot(kind='not in snapshot')))), entry(top_level('named-manual')), entry(unit('named-unit'))]
        for restored in restored_entries:
            restored['session']['config_dir'] = config_dir
        saved = records_for_test(restored_entries)
        self.create(saved)
        commands = CommandRecorder()
        output = io.StringIO()
        with patch.object(restart, '_run', commands), patch.object(restart, '_live_session_ids', return_value=frozenset()), patch.object(settle, 'record_time', return_value=RESTARTED), redirect_stdout(output):
            self.assertEqual(restart.up(LOGIN, dry_run=True), 0)
        resume_commands = [argv[-1] for argv in commands.calls if '-ic' in argv]
        self.assertEqual(len(resume_commands), 4)
        for command in resume_commands:
            self.assertIn(f'&& {named_prefix(config_dir)}ENABLE_TOOL_SEARCH=true command claude', command)
        self.assertIn(f'{named_prefix(config_dir)}ENABLE_TOOL_SEARCH=true command claude', output.getvalue().split('manual restart named-manual:', 1)[1])
        unit_argv = next((argv for argv in commands.calls if any((part.endswith('add_unit.py') for part in argv))))
        self.assertNotIn(config_dir, unit_argv)
        self.assertNotIn('CLAUDE_CONFIG_DIR', shlex.join(unit_argv))
        self.assertCountEqual(record.session_records(LOGIN), saved)

    def test_default_and_legacy_resume_commands_are_byte_identical(self) -> None:
        restored = entry(top_level('default-command'))
        expected = 'cd /tmp/default-command && ENABLE_TOOL_SEARCH=true command claude --resume default-command -n default-command --remote-control default-command --settings \'{"disableAgentView": true}\' prompt; exec zsh'
        resume_command = cast(Callable[[inventory.Session, str], str], getattr(restart, '_resume_command'))
        self.assertEqual(resume_command(restored['session'], 'prompt'), expected)
        restored['session']['config_dir'] = str(Path.home() / '.claude')
        self.assertEqual(resume_command(restored['session'], 'prompt'), expected)

    def test_window_dry_run_reads_and_restores_the_starting_desktop(self) -> None:
        restored = entry(top_level('window', host=inventory.WindowHost(kind='ghostty', window_shell=10, desktop=inventory.NamedDesktop(kind='named', name='Work'))))
        saved = records_for_test([restored])
        self.create(saved)
        calls: list[tuple[list[str], bool]] = []

        def run(argv: list[str], *, dry_run: bool, check: bool=True) -> subprocess.CompletedProcess[str] | None:
            del check
            calls.append((argv, dry_run))
            if dry_run:
                print(shlex.join(argv))
                return None
            return subprocess.CompletedProcess(argv, 0, '7\n', '')
        output = io.StringIO()
        with patch.object(restart, '_run', run), patch.object(restart, '_desktop_numbers', return_value={'Work': 3}), patch.object(restart, '_live_session_ids', return_value=frozenset()), patch.object(settle, 'record_time', return_value=RESTARTED), redirect_stdout(output):
            self.assertEqual(restart.up(LOGIN, dry_run=True), 0)
        displayed = [shlex.split(line) for line in output.getvalue().splitlines() if line.startswith(('kdotool', 'systemd-run'))]
        self.assertEqual(displayed[0], ['kdotool', 'get_desktop'])
        self.assertEqual(displayed[1], ['kdotool', 'set_desktop', '3'])
        self.assertEqual(displayed[2][0], 'systemd-run')
        self.assertEqual(displayed[3], ['kdotool', 'set_desktop', '7'])
        self.assertEqual([argv for argv, dry_run in calls if not dry_run], [['kdotool', 'get_desktop']])
        self.assertCountEqual(record.session_records(LOGIN), saved)

    def test_zsh_parses_recorded_values_as_the_same_claude_arguments(self) -> None:
        cwd = self.root / "cwd with 'quotes' and $()"
        cwd.mkdir()
        name = "name with 'quotes' and $()"
        model = "model with 'quotes' and $()"
        where = "where with 'quotes' and $()"
        restored = entry(top_level('quoted-id', name=name, cwd=str(cwd), model=inventory.ModelName(kind='model', name=model), host=inventory.WindowHost(kind='ghostty', window_shell=12, desktop=inventory.DesktopNotInSnapshot(kind='not in snapshot'))), where=record.WhereSaid(kind='said', text=where, at=STOPPED))
        config_dir = str(self.root / "account with 'quotes' and $()")
        restored['session']['config_dir'] = config_dir
        saved = records_for_test([restored])
        self.create(saved)
        output = io.StringIO()
        with patch.object(restart, '_live_session_ids', return_value=frozenset()), patch.object(settle, 'record_time', return_value=RESTARTED), patch('subprocess.run', return_value=subprocess.CompletedProcess(['kdotool', 'get_desktop'], 0, '1\n', '')), redirect_stdout(output):
            self.assertEqual(restart.up(LOGIN, dry_run=True), 0)
        memory_base = str(Path.home() / '.claude')
        launch_line = next((line for line in output.getvalue().splitlines() if 'ghostty' in line))
        outer = shlex.split(launch_line)
        resume_command = outer[outer.index('-ic') + 1]
        binary = self.root / 'bin'
        binary.mkdir()
        argv_log = self.root / 'claude-argv.json'
        claude = binary / 'claude'
        _ = claude.write_text('\n'.join(('#!/usr/bin/env python3', 'import json, os, sys', 'from pathlib import Path', "Path(os.environ['ARGV_LOG']).write_text(json.dumps(sys.argv[1:]))", "Path(os.environ['CONFIG_LOG']).write_text(os.environ['CLAUDE_CONFIG_DIR'])", "Path(os.environ['MEMORY_LOG']).write_text(os.environ['CLAUDE_CODE_REMOTE_MEMORY_DIR'])", '')), encoding='utf-8')
        _ = claude.chmod(493)
        final_zsh = binary / 'zsh'
        _ = final_zsh.write_text('#!/bin/sh\nexit 0\n', encoding='utf-8')
        _ = final_zsh.chmod(493)
        zdotdir = self.root / 'empty-zdotdir'
        zdotdir.mkdir()
        environment = {**os.environ, 'ARGV_LOG': str(argv_log), 'CONFIG_LOG': str(self.root / 'claude-config-dir'), 'MEMORY_LOG': str(self.root / 'claude-memory-dir'), 'CLAUDE_CONFIG_DIR': '/another-account-in-the-shell', 'HOME': str(self.root), 'PATH': f'{binary}:/usr/bin:/bin', 'ZDOTDIR': str(zdotdir)}
        parsed = subprocess.run(['/bin/zsh', '-f', '-ic', resume_command], cwd=self.root, env=environment, capture_output=True, text=True, check=False, timeout=60)
        self.assertEqual(parsed.returncode, 0, (parsed.stdout, parsed.stderr))
        self.assertEqual((self.root / 'claude-config-dir').read_text(encoding='utf-8'), config_dir)
        self.assertEqual((self.root / 'claude-memory-dir').read_text(encoding='utf-8'), memory_base)
        actual = cast(list[str], cast(object, json.loads(argv_log.read_text(encoding='utf-8'))))
        note = restart.restart_note(restored, RESTARTED)
        self.assertEqual(actual, ['--resume', 'quoted-id', '-n', name, '--remote-control', name, '--model', model, '--settings', '{"disableAgentView": true}', note])

    def test_confirmation_keeps_only_final_poll_records_for_remote_control(self) -> None:
        ids = frozenset({"first", "second"})
        polls = [registry_confirmation(frozenset({"first"})).records, registry_confirmation(ids).records]
        clock = [0.0]
        def sleep(seconds: float) -> None:
            clock[0] += seconds
        with patch.object(sessions, "live_sessions", side_effect=polls) as registry, patch.object(time, "monotonic", side_effect=lambda: clock[0]), patch.object(time, "sleep", side_effect=sleep):
            result = restart.wait_for_confirmation(ids, 1.0)
            self.assertEqual(result.records, polls[-1])
            self.assertEqual(result.live_ids, ids)
            with patch.object(sessions, "live_sessions", side_effect=AssertionError("extra liveness scan")):
                self.assertEqual(restart.wait_for_remote_control(result.records, 1.0), ids)
        self.assertEqual(registry.call_count, 2)

    def test_confirmation_returns_only_confirmed_ids_at_the_shared_deadline(self) -> None:
        clock = [0.0]
        def sleep(seconds: float) -> None:
            clock[0] += seconds
        with patch.object(sessions, "live_sessions", return_value=registry_confirmation(frozenset({"live", "unrelated"})).records), patch.object(time, "monotonic", side_effect=lambda: clock[0]), patch.object(time, "sleep", side_effect=sleep) as sleeping:
            self.assertEqual(restart.wait_for_confirmation(frozenset({"live", "missing"}), 0.2).live_ids, frozenset({"live"}))
        sleeping.assert_called_once_with(0.2)

    def test_remote_control_wait_returns_immediately_when_all_are_active(self) -> None:
        records = registry_confirmation(frozenset({"one", "two"})).records
        with patch.object(time, "sleep") as sleep:
            self.assertEqual(restart.wait_for_remote_control(records, 10), frozenset({"one", "two"}))
        sleep.assert_not_called()

    def test_remote_control_wait_returns_when_last_session_activates_or_deadline_expires(self) -> None:
        records = registry_confirmation(frozenset({"one", "two"})).records
        for activates in (True, False):
            with self.subTest(activates=activates):
                clock = [0.0]
                def sleep(seconds: float) -> None:
                    clock[0] += seconds
                def probe(item: sessions.SessionRecord) -> str:
                    return "active" if item["sessionId"] == "one" or (activates and clock[0] >= 1) else "absent"
                with patch.object(sessions, "remote_control", side_effect=probe), patch.object(time, "monotonic", side_effect=lambda: clock[0]), patch.object(time, "sleep", side_effect=sleep):
                    self.assertEqual(restart.wait_for_remote_control(records, 2), frozenset({"one", "two"} if activates else {"one"}))
                    self.assertEqual(clock[0], 1 if activates else 2)

    def test_remote_control_slow_probe_starts_no_poll_after_deadline(self) -> None:
        records = registry_confirmation(frozenset({"one", "two"})).records
        clock = [0.0]
        def probe(_item: sessions.SessionRecord) -> str:
            clock[0] += 6
            return "absent"
        with patch.object(sessions, "remote_control", side_effect=probe) as read, patch.object(time, "monotonic", side_effect=lambda: clock[0]), patch.object(time, "sleep") as sleep:
            self.assertEqual(restart.wait_for_remote_control(records, 10), frozenset())
        self.assertEqual(read.call_count, 2)
        sleep.assert_not_called()
        self.assertEqual(clock[0], 12)

    def test_parallel_tmux_creation_recovers_by_opening_a_window(self) -> None:
        self.create(records_for_test([entry(top_level(name, host=tmux_host('shared'))) for name in ('alpha', 'bravo')]))
        initial_probes = threading.Barrier(2)
        created = threading.Event()
        lock = threading.Lock()
        probe_count = 0
        attempts: list[list[str]] = []
        commands: list[list[str]] = []

        def probe(argv: list[str], *, dry_run: bool) -> subprocess.CompletedProcess[str]:
            nonlocal probe_count
            self.assertFalse(dry_run)
            self.assertEqual(argv, ['tmux', 'has-session', '-t', '=shared'])
            with lock:
                initial = probe_count < 2
                probe_count += 1
            if initial:
                _ = initial_probes.wait(1)
                return subprocess.CompletedProcess(argv, 1, '', '')
            return subprocess.CompletedProcess(argv, 0 if created.is_set() else 1, '', '')

        def run(argv: list[str], *, dry_run: bool, check: bool=True) -> subprocess.CompletedProcess[str]:
            self.assertFalse(dry_run)
            self.assertTrue(check)
            with lock:
                attempts.append(argv)
                if argv[:2] == ['tmux', 'new-session']:
                    if created.is_set():
                        raise subprocess.CalledProcessError(1, argv, stderr='duplicate session: shared')
                    created.set()
                else:
                    self.assertEqual(argv[:4], ['tmux', 'new-window', '-t', '=shared:'])
                    self.assertTrue(created.is_set())
                commands.append(argv)
            return subprocess.CompletedProcess(argv, 0, '', '')

        def resume(session: inventory.Session, prompt: str) -> str:
            del prompt
            return f"claude --resume {session['session_id']}"
        with patch.object(restart, '_retire'), patch.object(restart, '_record_scheduled_restart_prompt'), patch.object(restart, '_resume_command', resume), patch.object(restart, '_probe', probe), patch.object(restart, '_run', run), patch.object(restart, '_session_liveness', return_value=restart.SessionNotLive()), patch.object(restart, '_live_session_ids', return_value=frozenset()), patch.object(restart, 'wait_for_confirmation', side_effect=self.all_sessions_live) as wait, patch.object(settle, 'record_time', return_value=RESTARTED), patch.object(restart, 'archive_session'), redirect_stdout(io.StringIO()):
            self.assertEqual(restart.up(LOGIN), 0)
        wait.assert_called_once_with(frozenset({'alpha', 'bravo'}), restart.SESSION_WAIT_SECONDS)
        self.assertTrue(all((item['progress']['kind'] == 'restarted' for item in record.session_records(LOGIN))))
        self.assertEqual(probe_count, 3)
        self.assertEqual([argv[1] for argv in attempts], ['new-session', 'new-session', 'new-window'])
        self.assertEqual([argv[1] for argv in commands], ['new-session', 'new-window'])
        self.assertEqual(commands[0][2:6], ['-d', '-s', 'shared', '-c'])
        self.assertEqual(commands[1][2:5], ['-t', '=shared:', '-c'])
        self.assertCountEqual([argv[-1] for argv in commands], ['claude --resume alpha', 'claude --resume bravo'])

    def test_tmux_respawns_a_reusable_live_or_dead_pane(self) -> None:
        self.allow_leaf(ShutdownSendLeaf.RESTART_RETIRE)
        cases = [('live', 'kept\t700\t0\n'), ('dead', 'kept\t\t1\n')]
        for label, pane_status in cases:
            with self.subTest(label=label):
                commands = CommandRecorder()
                probes: list[list[str]] = []
                restored = top_level(label, cwd='/tmp/restored', host=recorded_tmux_host('kept', f'%{label}', 700))
                self.create(records_for_test([entry(restored)], machine='Mac'))

                def probe(argv: list[str], *, capture_output: bool, text: bool, check: bool) -> subprocess.CompletedProcess[str]:
                    del capture_output, text, check
                    probes.append(argv)
                    if argv[:2] == ['tmux', 'display']:
                        return subprocess.CompletedProcess(argv, 0, pane_status, '')
                    if argv[:1] == ['pgrep']:
                        return subprocess.CompletedProcess(argv, 1, '', '')
                    return subprocess.CompletedProcess(argv, 0, '', '')
                with patch.object(restart, '_run', commands), patch.object(restart, '_live_session_ids', return_value=frozenset()), patch.object(restart, 'wait_for_confirmation', side_effect=self.all_sessions_live), patch('subprocess.run', probe), patch.object(settle, 'record_time', return_value=RESTARTED):
                    self.assertEqual(restart.up(LOGIN), 0)
                launches = [argv for argv in commands.calls if argv[:2] == ['tmux', 'respawn-pane']]
                self.assertEqual(len(launches), 1)
                self.assertEqual(launches[0][:-1], ['tmux', 'respawn-pane', '-k', '-t', f'%{label}', '-c', '/tmp/restored', 'zsh', '-ic'])
                self.assertIn(f'--resume {label}', launches[0][-1])
                self.assertNotIn(['tmux', 'has-session', '-t', '=kept'], probes)
                if label == 'dead':
                    self.assertFalse(any((argv[:1] == ['pgrep'] for argv in probes)))

    def test_tmux_respawns_recorded_pane_when_session_name_has_outer_spaces(self) -> None:
        self.allow_leaf(ShutdownSendLeaf.RESTART_RETIRE)
        commands = CommandRecorder()
        restored = top_level('spaced', host=recorded_tmux_host(' work ', '%8', 700))
        self.create(records_for_test([entry(restored)], machine='Mac'))

        def probe(argv: list[str], *, capture_output: bool, text: bool, check: bool) -> subprocess.CompletedProcess[str]:
            del capture_output, text, check
            if argv[:2] == ['tmux', 'display']:
                return subprocess.CompletedProcess(argv, 0, ' work \t700\t0\n', '')
            if argv[:1] == ['pgrep']:
                return subprocess.CompletedProcess(argv, 1, '', '')
            return subprocess.CompletedProcess(argv, 0, '', '')
        with patch.object(restart, '_run', commands), patch.object(restart, '_live_session_ids', return_value=frozenset()), patch.object(restart, 'wait_for_confirmation', side_effect=self.all_sessions_live), patch('subprocess.run', probe), patch.object(settle, 'record_time', return_value=RESTARTED):
            self.assertEqual(restart.up(LOGIN), 0)
        self.assertEqual(len([argv for argv in commands.calls if argv[:2] == ['tmux', 'respawn-pane']]), 1)

    def test_tmux_falls_back_when_a_recorded_pane_is_in_use_or_gone(self) -> None:
        self.allow_leaf(ShutdownSendLeaf.RESTART_RETIRE)
        recorded = inventory.TmuxPane(kind='pane', pane_id='%7', pane_pid=700)
        fallbacks: list[tuple[str, inventory.TmuxPane | inventory.PaneNotRecorded, int, str, int]] = [('child process', recorded, 0, 'kept\t700\t0\n', 0), ('different pane pid', recorded, 0, 'kept\t701\t0\n', 1), ('another session', recorded, 0, 'moved\t700\t0\n', 1), ('missing pane', recorded, 1, '', 1), ('pane not recorded', inventory.PaneNotRecorded(kind='not recorded'), 1, '', 1)]
        for label, pane, display_status, display_output, pgrep_status in fallbacks:
            for session_survives in (True, False):
                with self.subTest(label=label, session_survives=session_survives), patch.dict(os.environ, {'SHUTDOWN_STATE_DIR': str(self.root / (label + str(session_survives)))}):
                    commands = CommandRecorder()
                    probes: list[list[str]] = []
                    restored = top_level('fallback', cwd='/tmp/fallback', host=inventory.TmuxHost(kind='tmux', tmux_session='kept', pane=pane))
                    self.create(records_for_test([entry(restored)], machine='Mac'))

                    def probe(argv: list[str], *, capture_output: bool, text: bool, check: bool) -> subprocess.CompletedProcess[str]:
                        del capture_output, text, check
                        probes.append(argv)
                        if argv[:2] == ['tmux', 'display']:
                            return subprocess.CompletedProcess(argv, display_status, display_output, '')
                        if argv[:1] == ['pgrep']:
                            return subprocess.CompletedProcess(argv, pgrep_status, '', '')
                        return subprocess.CompletedProcess(argv, 0 if session_survives else 1, '', '')
                    with patch.object(restart, '_run', commands), patch.object(restart, '_live_session_ids', return_value=frozenset()), patch.object(restart, 'wait_for_confirmation', side_effect=self.all_sessions_live), patch('subprocess.run', probe), patch.object(settle, 'record_time', return_value=RESTARTED):
                        self.assertEqual(restart.up(LOGIN), 0)
                    launch = next((argv for argv in commands.calls if argv[:1] == ['tmux']))
                    if session_survives:
                        expected_prefix = ['tmux', 'new-window', '-t', '=kept:', '-c', '/tmp/fallback', 'zsh', '-ic']
                    else:
                        expected_prefix = ['tmux', 'new-session', '-d', '-s', 'kept', '-c', '/tmp/fallback', 'zsh', '-ic']
                    self.assertEqual(launch[:-1], expected_prefix)
                    self.assertIn('--resume fallback', launch[-1])
                    pane_probed = any((argv[:2] == ['tmux', 'display'] for argv in probes))
                    self.assertEqual(pane_probed, pane['kind'] == 'pane')

    def test_up_prints_a_machine_prefixed_failure_to_stdout(self) -> None:
        output = io.StringIO()
        errors = io.StringIO()
        with patch.object(restart, '_up', side_effect=RuntimeError('restore broke')), patch.object(settle, 'local_machine', return_value='Mac'), redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(restart.up(LOGIN), 1)
        self.assertEqual(output.getvalue(), 'Mac: restart failed: restore broke\n')
        self.assertEqual(errors.getvalue(), '')

    def test_incomplete_restart_alert_emits_the_named_recovery_action(self) -> None:
        self.allow_leaf(ShutdownSendLeaf.DELIVER_MESSAGE)
        sent: list[tuple[list[str], str]] = []

        def send(argv: list[str], *, input: str, capture_output: bool, text: bool, check: bool, timeout: int) -> subprocess.CompletedProcess[str]:
            del capture_output, text, check, timeout
            sent.append((argv, input))
            return subprocess.CompletedProcess(argv, 0, '', '')
        output = io.StringIO()
        with patch.object(restart, 'named_claude_account', return_value=self.account), patch.object(restart, '_up', return_value=restart.MachineRestartResult('natedev', LABEL, 'nothing')), patch.object(restart, 'run_remote', return_value=(2, '')), patch.object(restart, 'other_machine', return_value='Mac'), patch.object(settle, '_send_command', return_value=['fake-send']), patch.object(subprocess, 'run', send), redirect_stdout(output):
            self.assertEqual(restart.restart(restart.AccountNamed(LABEL), dry_run=False, alerts=restart.AlertWhenNotBack()), 0)
        failure = 'restart incomplete: Mac not reached'
        self.assertIn(failure, output.getvalue())
        self.assertEqual(len(sent), 1)
        args, body = sent[0]
        action_index = args.index('--action')
        self.assertEqual(args[action_index + 1], 'Fix what is named, then run /shutdown restart again.')
        self.assertIn(f'{LABEL} is not fully back', args)
        self.assertIn(failure, body)

    def test_restart_cli_builds_a_never_alert_policy(self) -> None:
        received: list[tuple[restart.AccountNamed | restart.AccountNotNamed, bool, restart.AlertWhenNotBack | restart.NeverAlert]] = []

        def run_restart(account: restart.AccountNamed | restart.AccountNotNamed, *, dry_run: bool, alerts: restart.AlertWhenNotBack | restart.NeverAlert, only: Iterable[str], here: bool) -> int:
            self.assertEqual(tuple(only), ())
            self.assertFalse(here)
            received.append((account, dry_run, alerts))
            return 0
        with patch.object(restart, 'restart', run_restart):
            self.assertEqual(shutdown.main(['restart', LABEL, '--no-alert']), 0)
        self.assertEqual(received, [(restart.AccountNamed(LABEL), False, restart.NeverAlert())])

    def test_restart_help_hides_no_alert(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit) as raised:
            _ = shutdown.main(['restart', '--help'])
        self.assertEqual(raised.exception.code, 0)
        self.assertNotIn('--no-alert', output.getvalue())

    def test_unknown_named_account_is_a_usage_error(self) -> None:
        with patch.object(restart, 'named_claude_account', side_effect=UnknownAccountName('nobody')), patch.object(settle, 'alert_user') as alert_user, redirect_stderr(io.StringIO()):
            self.assertEqual(restart.restart(restart.AccountNamed('nobody'), dry_run=False, alerts=restart.AlertWhenNotBack()), 2)
        alert_user.assert_not_called()

    def run_restarts(self, *, live: frozenset[str] = EMPTY_SESSION_IDS, confirmed: frozenset[str] = EMPTY_SESSION_IDS) -> tuple[int, list[str]]:
        launches: list[str] = []

        def launch(current: SessionShutdown, _note: str, _desktop: restart.DesktopLaunchState, *, dry_run: bool) -> restart.SessionLaunched:
            self.assertFalse(dry_run)
            claim = record.find_session(LOGIN, current["session"]["session_id"])
            assert claim is not None
            self.assertEqual(claim["progress"]["kind"], "restarting")
            launches.append(current["session"]["session_id"])
            return restart.SessionLaunched()

        def wait(ids: frozenset[str], _timeout: float) -> restart.RegistryConfirmation:
            return registry_confirmation(ids & confirmed)

        with (
            patch.object(restart, "_retire"),
            patch.object(restart, "_launch_session", launch),
            patch.object(restart, "_live_session_ids", return_value=live),
            patch.object(restart, "wait_for_confirmation", side_effect=wait),
            patch.object(restart, "_send_restart_note", return_value=settle.MessageSent(kind="sent")),
            redirect_stdout(io.StringIO()),
        ):
            result = restart.up(LOGIN)
        return result, launches

    def test_six_tmux_launches_finish_in_under_one_second(self) -> None:
        saved = [entry(top_level(str(index), host=tmux_host(str(index)))) for index in range(6)]
        self.create(saved)
        threads: set[int] = set()
        lock = threading.Lock()

        def launch(_record: SessionShutdown, _note: str, _state: restart.DesktopLaunchState, *, dry_run: bool) -> restart.SessionLaunched:
            self.assertFalse(dry_run)
            with lock:
                threads.add(threading.get_ident())
            time.sleep(0.2)
            return restart.SessionLaunched()

        with patch.object(restart, "_retire"), patch.object(restart, "_live_session_ids", return_value=frozenset()), patch.object(restart, "_launch_session", launch), patch.object(restart, "wait_for_confirmation", side_effect=self.all_sessions_live), redirect_stdout(io.StringIO()):
            start = time.monotonic()
            self.assertEqual(restart.up(LOGIN), 0)
            self.assertLess(time.monotonic() - start, 1.0)
        self.assertEqual(restart.RESTART_LAUNCH_WORKERS, 6)
        self.assertEqual(len(threads), 6)
        self.assertNotIn(threading.get_ident(), threads)
        self.assertEqual(record.session_records(LOGIN), [])

    def test_seven_missing_registry_entries_do_not_delay_the_eighth_launch(self) -> None:
        self.create([entry(top_level(str(index), host=tmux_host(str(index)))) for index in range(8)])
        launched: list[str] = []

        def launch(current: SessionShutdown, _note: str, _state: restart.DesktopLaunchState, *, dry_run: bool) -> restart.SessionLaunched:
            del dry_run
            launched.append(current["session"]["session_id"])
            return restart.SessionLaunched()

        def wait(ids: frozenset[str], timeout: float) -> restart.RegistryConfirmation:
            self.assertCountEqual(launched, [str(index) for index in range(8)])
            self.assertEqual(ids, frozenset(launched))
            self.assertEqual(timeout, restart.SESSION_WAIT_SECONDS)
            return registry_confirmation(frozenset({"7"}))

        with patch.object(restart, "_retire"), patch.object(restart, "_live_session_ids", return_value=frozenset()), patch.object(restart, "_launch_session", launch), patch.object(restart, "wait_for_confirmation", wait), redirect_stdout(io.StringIO()):
            self.assertEqual(restart.up(LOGIN), 1)
        remaining = record.session_records(LOGIN)
        self.assertEqual(len(remaining), 7)
        self.assertTrue(all(item["progress"]["kind"] == "restart failed" for item in remaining))
        self.assertIsNone(record.find_session(LOGIN, "7"))

    def test_one_launcher_exception_marks_only_its_session_failed(self) -> None:
        self.create([entry(top_level(name, host=tmux_host(name))) for name in ("alpha", "broken", "zeta")])

        def launch(current: SessionShutdown, _note: str, _state: restart.DesktopLaunchState, *, dry_run: bool) -> restart.SessionLaunched:
            del dry_run
            if current["session"]["session_id"] == "broken":
                raise RuntimeError("launch failed\nmore")
            return restart.SessionLaunched()

        with patch.object(restart, "_retire"), patch.object(restart, "_live_session_ids", return_value=frozenset()), patch.object(restart, "_launch_session", launch), patch.object(restart, "wait_for_confirmation", side_effect=self.all_sessions_live), redirect_stdout(io.StringIO()):
            self.assertEqual(restart.up(LOGIN), 1)
        remaining = record.session_records(LOGIN)
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["session"]["session_id"], "broken")
        self.assertEqual(remaining[0]["progress"], {"kind": "restart failed", "at": RESTARTED, "reason": "launch failed"})

    def test_requester_launches_after_other_sessions_are_confirmed(self) -> None:
        saved = records_for_test([
            entry(top_level("requester", host=tmux_host("requester"))),
            entry(showrunner("showrunner")), entry(unit("unit")),
        ], requester="requester")
        self.create(saved)
        events: list[str] = []

        def launch(current: SessionShutdown, _note: str, _state: restart.DesktopLaunchState, *, dry_run: bool) -> restart.SessionLaunched:
            del dry_run
            name = current["session"]["session_id"]
            if name == "showrunner":
                self.assertNotIn("wait", events)
            if name == "requester":
                self.assertIsNone(record.find_session(LOGIN, "unit"))
                self.assertIsNone(record.find_session(LOGIN, "showrunner"))
            events.append(name)
            return restart.SessionLaunched()

        def wait(ids: frozenset[str], _timeout: float) -> restart.RegistryConfirmation:
            events.append("wait")
            return registry_confirmation(ids)

        with patch.object(restart, "_retire"), patch.object(restart, "_launch_session", launch), patch.object(restart, "_live_session_ids", return_value=frozenset()), patch.object(restart, "wait_for_confirmation", wait), patch.object(restart, "_send_restart_note", return_value=settle.MessageSent(kind="sent")), redirect_stdout(io.StringIO()):
            self.assertEqual(restart.up(LOGIN), 0)
        self.assertCountEqual(events[:2], ["unit", "showrunner"])
        self.assertEqual(events[2:], ["wait", "requester", "wait"])

    def test_requester_of_a_separate_request_does_not_wait_on_another_request(self) -> None:
        waiting = entry(top_level("busy"), state=record.SessionWaiting(kind="waiting"))
        waiting["request_id"] = "other"
        requester = entry(top_level("requester", host=tmux_host("requester")))
        requester["requested_by"] = record.FromSession(kind="session", session_id="requester")
        requester["request_id"] = "ready-request"
        self.create([waiting, requester])
        self.assertEqual(self.run_restarts(confirmed=frozenset({"requester"})), (0, ["requester"]))
        self.assertEqual(record.session_records(LOGIN), [waiting])

    def test_recent_relaunch_claim_is_left_to_its_owner(self) -> None:
        saved = entry(top_level("owner", host=tmux_host("owner")))
        saved["progress"] = record.SessionRestarting(kind="restarting", at=RESTARTED, prior=record.SessionStopped(kind="stopped", at=STOPPED))
        self.create([saved])
        with patch.object(restart, "launch_entry") as launch:
            self.assertEqual(restart.relaunch_session(saved, dry_run=False, desktop_state=self.desktop_state), saved)
        launch.assert_not_called()

    def test_stale_relaunch_claim_reuses_its_prior_progress(self) -> None:
        saved = entry(top_level("owner", host=tmux_host("owner")))
        prior = record.SessionStopped(kind="stopped", at=STOPPED)
        saved["progress"] = record.SessionRestarting(kind="restarting", at=STOPPED, prior=prior)
        self.create([saved])
        with patch.object(restart, "_retire"), patch.object(restart, "_live_session_ids", return_value=frozenset()), patch.object(restart, "_launch_session", return_value=restart.SessionLaunched()), patch.object(restart, "wait_for_confirmation") as wait:
            current = restart.relaunch_session(saved, dry_run=False, desktop_state=self.desktop_state)
        wait.assert_not_called()
        self.assertEqual(current["progress"], {"kind": "restarting", "at": RESTARTED, "prior": prior})

    def test_launch_and_confirmation_are_separate_stages(self) -> None:
        saved = entry(top_level("owner", host=tmux_host("owner")))
        self.create([saved])
        with patch.object(restart, "_retire"), patch.object(restart, "_live_session_ids", return_value=frozenset()), patch.object(restart, "_launch_session", return_value=restart.SessionLaunched()), patch.object(restart, "wait_for_confirmation") as wait:
            current = restart.relaunch_session(saved, dry_run=False, desktop_state=self.desktop_state)
            wait.assert_not_called()
            self.assertEqual(current["progress"]["kind"], "restarting")
            restart.confirm_entry(current, RESTARTED, True)
        finished = record.find_session(LOGIN, "owner")
        assert finished is not None
        self.assertEqual(finished["progress"]["kind"], "restarted")

    def test_restarted_record_is_archived_without_another_launch(self) -> None:
        saved = entry(top_level("done"), state=record.SessionRestarted(kind="restarted", at=RESTARTED))
        self.create([saved])
        result, launched = self.run_restarts()
        self.assertEqual((result, launched), (0, []))
        self.assertEqual(record.session_records(LOGIN), [])

    def test_up_accepts_already_gone_and_identity_lost_progress(self) -> None:
        for state in (
            record.SessionAlreadyGone(kind="already gone", at=STOPPED),
            record.ProcessIdentityLost(kind="process identity lost", at=STOPPED),
        ):
            with self.subTest(state=state["kind"]), patch.dict(os.environ, {"SHUTDOWN_STATE_DIR": str(self.root / state["kind"])}):
                saved = entry(top_level("owner", host=tmux_host("owner")), state=state)
                self.create([saved])
                self.assertEqual(self.run_restarts(confirmed=frozenset({"owner"})), (0, ["owner"]))

    def test_manually_resumed_session_restores_timers_and_is_archived_without_launch(self) -> None:
        saved = entry(top_level("manual"), state=record.SessionNeedsManualRestart(kind="manual restart", command="resume"))
        saved["timers"] = [TimerRestore(instance="timer", was_enabled=True, footer=record.NoFooter(kind="no footer"))]
        self.create([saved])
        with patch.object(settle, "run_notifier") as timers:
            self.assertEqual(self.run_restarts(live=frozenset({"manual"})), (0, []))
        timers.assert_called_once_with("start", "timer")
        self.assertEqual(record.session_records(LOGIN), [])

    def test_manual_session_not_live_keeps_its_resume_command(self) -> None:
        saved = entry(top_level("manual"), state=record.SessionNeedsManualRestart(kind="manual restart", command="resume"))
        self.create([saved])
        self.assertEqual(self.run_restarts(), (1, []))
        self.assertEqual(record.find_session(LOGIN, "manual"), saved)

    def test_pending_timers_only_retry_the_failed_timer_when_live(self) -> None:
        saved = entry(top_level("owner"), state=record.SessionRestoredTimersPending(
            kind="timers pending", at=STOPPED, timers=[record.PendingTimer(instance="pending", reason="failed")],
        ), timers=[
            TimerRestore(instance=name, was_enabled=True, footer=record.NoFooter(kind="no footer"))
            for name in ("done", "pending")
        ])
        self.create([saved])
        with patch.object(restart, "_session_liveness", return_value=restart.SessionLive()), patch.object(settle, "run_notifier") as timers:
            self.assertEqual(self.run_restarts(), (0, []))
        timers.assert_called_once_with("start", "pending")

    def test_unreadable_registry_preserves_pending_timer_progress(self) -> None:
        saved = entry(top_level("owner"), state=record.SessionRestoredTimersPending(kind="timers pending", at=STOPPED, timers=[record.PendingTimer(instance="pending", reason="failed")]))
        self.create([saved])
        with patch.object(restart, "_session_liveness", return_value=restart.SessionLivenessUnknown("unreadable")), patch.object(settle, "run_notifier") as timers:
            self.assertEqual(self.run_restarts(), (1, []))
        timers.assert_not_called()
        self.assertEqual(record.find_session(LOGIN, "owner"), saved)

    def test_failed_launcher_with_unknown_registry_preserves_prior_progress(self) -> None:
        saved = entry(top_level("owner", host=tmux_host("owner")))
        self.create([saved])
        with patch.object(restart, "_retire"), patch.object(restart, "_live_session_ids", return_value=frozenset()), patch.object(restart, "_launch_session", side_effect=OSError("failed")), patch.object(restart, "_session_liveness", return_value=restart.SessionLivenessUnknown("unreadable")), redirect_stdout(io.StringIO()):
            self.assertEqual(restart.relaunch_session(saved, dry_run=False, desktop_state=self.desktop_state), saved)
        self.assertEqual(record.find_session(LOGIN, "owner"), saved)

    def test_failed_launcher_with_live_registry_is_confirmed(self) -> None:
        saved = entry(top_level("owner", host=tmux_host("owner")))
        self.create([saved])
        with patch.object(restart, "_retire"), patch.object(restart, "_live_session_ids", return_value=frozenset()), patch.object(restart, "_launch_session", side_effect=OSError("failed")), patch.object(restart, "_session_liveness", return_value=restart.SessionLive()), patch.object(restart, "wait_for_confirmation", side_effect=self.all_sessions_live), redirect_stdout(io.StringIO()):
            self.assertEqual(restart.up(LOGIN), 0)
        self.assertIsNone(record.find_session(LOGIN, "owner"))

    def test_queued_showrunner_note_keeps_its_timers_stopped(self) -> None:
        saved = entry(showrunner("owner"))
        saved["timers"] = [TimerRestore(instance="timer", was_enabled=True, footer=record.NoFooter(kind="no footer"))]
        self.create([saved])
        with patch.object(restart, "_send_restart_note", return_value=settle.MessageQueued(kind="queued", reason="not ready")), patch.object(settle, "run_notifier") as timers:
            restart.confirm_entry(saved, RESTARTED, True)
        timers.assert_not_called()
        self.assertEqual(self.stored_record()["progress"]["kind"], "restart failed")

    def test_real_up_clears_a_stop_only_request_hold_after_last_restore(self) -> None:
        saved = entry(top_level("owner", host=tmux_host("owner")))
        saved["then"] = "stop"
        saved["hold"] = "account"
        self.create([saved])
        record.set_account_hold(record.AccountHold(
            login=LOGIN, label=LABEL, machine="natedev", requested_at=STOPPED,
            request_id=saved["request_id"], requested_by=saved["requested_by"], then="stop",
        ))
        self.assertEqual(self.run_restarts(confirmed=frozenset({"owner"})), (0, ["owner"]))
        self.assertIsNone(record.account_hold(LOGIN))

    def test_real_up_clears_an_account_hold_without_session_records(self) -> None:
        record.set_account_hold(record.AccountHold(
            login=LOGIN, label=LABEL, machine="natedev", requested_at=STOPPED,
            request_id="empty", requested_by=record.FromTerminal(kind="terminal"), then="stop",
        ))
        self.assertEqual(restart.up(LOGIN), 0)
        self.assertIsNone(record.account_hold(LOGIN))

    def test_up_keeps_a_hold_while_its_own_request_still_has_a_record(self) -> None:
        waiting = entry(top_level("waiting"), state=record.SessionWaiting(kind="waiting"))
        self.create([waiting])
        record.set_account_hold(record.AccountHold(
            login=LOGIN, label=LABEL, machine="natedev", requested_at=STOPPED,
            request_id=waiting["request_id"], requested_by=waiting["requested_by"], then="stop",
        ))
        self.assertEqual(restart.up(LOGIN), 0)
        self.assertIsNotNone(record.account_hold(LOGIN))

    def test_dry_run_leaves_session_files_and_holds_unchanged(self) -> None:
        saved = entry(top_level("owner"))
        self.create([saved])
        record.set_account_hold(record.AccountHold(
            login=LOGIN, label=LABEL, machine="natedev", requested_at=STOPPED,
            request_id="request", requested_by=saved["requested_by"], then="restart",
        ))
        before = self.files()
        with patch.object(restart, "_live_session_ids", return_value=frozenset()), patch.object(restart, "migrate") as migrate, patch.object(restart, "update_session") as update, patch.object(restart, "archive_session") as archive, patch.object(restart, "clear_finished_account_hold") as clear, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(restart.up(LOGIN, dry_run=True), 0)
        for mutation in (migrate, update, archive, clear):
            mutation.assert_not_called()
        self.assertNotIn("converted", output.getvalue())
        self.assertNotIn("restarted 0 in", output.getvalue())
        self.assertEqual(self.files(), before)

    def files(self) -> dict[str, bytes]:
        return {str(path.relative_to(self.state_root)): path.read_bytes() for path in self.state_root.rglob("*") if path.is_file()}

    def test_restart_dry_run_fixture_preserves_output_and_files(self) -> None:
        saved = [
            entry(top_level("zeta", host=inventory.TerminalHost(kind="terminal"))),
            entry(top_level("alpha", host=inventory.TerminalHost(kind="terminal"))),
            entry(top_level("manual")),
        ]
        self.create(saved)
        before = self.files()
        output = io.StringIO()
        def resume(session: inventory.Session, _prompt: str) -> str:
            return f"claude --resume {session['session_id']}"
        with patch.object(restart, "_resume_command", side_effect=resume), patch.object(restart, "_live_session_ids", return_value=frozenset()), redirect_stdout(output):
            self.assertEqual(restart.up(LOGIN, dry_run=True), 0)
        self.assertEqual(output.getvalue().replace(str(restart.SCRIPTS), "<scripts>").splitlines(), [
            f"{sys.executable} <scripts>/message/send.py retire --to session:alpha --from shutdown --key shutdown-owner@example.com-alpha",
            "open -na '/Applications/Nix Apps/Ghostty.app' --args -e zsh -ic 'claude --resume alpha'",
            f"{sys.executable} <scripts>/message/send.py retire --to session:manual --from shutdown --key shutdown-owner@example.com-manual",
            "manual restart manual: claude --resume manual",
            f"{sys.executable} <scripts>/message/send.py retire --to session:zeta --from shutdown --key shutdown-owner@example.com-zeta",
            "open -na '/Applications/Nix Apps/Ghostty.app' --args -e zsh -ic 'claude --resume zeta'",
        ])
        self.assertEqual(self.files(), before)

    def test_restart_account_choice_includes_a_hold_without_sessions(self) -> None:
        hold = record.AccountHold(
            login=LOGIN, label=LABEL, machine="natedev", requested_at=STOPPED,
            request_id="empty", requested_by=record.FromTerminal(kind="terminal"), then="stop",
        )
        record.set_account_hold(hold)
        with patch.object(restart, "own_claude_account", side_effect=UnreadableAccount("unreadable")), patch.object(restart, "run_remote", return_value=(0, "[]")), patch.object(restart, "_up", return_value=restart.MachineRestartResult("natedev", LABEL, "nothing")) as up, redirect_stdout(io.StringIO()):
            self.assertEqual(restart.restart(restart.AccountNotNamed(), dry_run=False, alerts=restart.NeverAlert()), 0)
        up.assert_called_once_with(LOGIN, False, ())

    def test_restart_account_choice_includes_a_peer_hold_without_sessions(self) -> None:
        hold = record.AccountHold(
            login=LOGIN, label=LABEL, machine="Mac", requested_at=STOPPED,
            request_id="empty", requested_by=record.FromTerminal(kind="terminal"), then="stop",
        )
        def remote(args: list[str], **_kwargs: object) -> tuple[int, str]:
            return (0, json.dumps([hold])) if args[0] == "holds" else (0, "[]") if args[0] == "records" else (0, "")
        with patch.object(restart, "own_claude_account", side_effect=UnreadableAccount("unreadable")), patch.object(restart, "run_remote", remote), patch.object(restart, "_up", return_value=restart.MachineRestartResult("natedev", LABEL, "nothing")) as up, redirect_stdout(io.StringIO()):
            self.assertEqual(restart.restart(restart.AccountNotNamed(), dry_run=False, alerts=restart.NeverAlert()), 0)
        up.assert_called_once_with(LOGIN, False, ())

    def test_window_hosts_launch_serially_on_the_calling_thread_with_spacing(self) -> None:
        saved = [
            entry(top_level("c-terminal", host=inventory.TerminalHost(kind="terminal"))),
            entry(top_level("b-zed", host=inventory.WindowHost(kind="zed", window_shell=11, desktop=inventory.DesktopNotInSnapshot(kind="not in snapshot")))),
            entry(top_level("a-ghostty", host=inventory.WindowHost(kind="ghostty", window_shell=10, desktop=inventory.DesktopNotInSnapshot(kind="not in snapshot")))),
        ]
        self.create(saved)
        caller = threading.get_ident()
        events: list[str] = []

        def run(argv: list[str], *, dry_run: bool, check: bool = True) -> subprocess.CompletedProcess[str]:
            del check
            self.assertFalse(dry_run)
            self.assertEqual(threading.get_ident(), caller)
            if argv[0] in {"systemd-run", "open"}:
                events.append(next(item["session"]["name"] for item in saved if item["session"]["session_id"] in argv[-1]))
            return subprocess.CompletedProcess(argv, 0, "0", "")

        def sleep(seconds: float) -> None:
            self.assertEqual(threading.get_ident(), caller)
            self.assertEqual(seconds, restart.WINDOW_DELAY_SECONDS)
            events.append("delay")

        with patch.object(restart, "_retire"), patch.object(restart, "_record_scheduled_restart_prompt"), patch.object(restart, "_run", run), patch.object(restart, "_live_session_ids", return_value=frozenset()), patch.object(restart, "wait_for_confirmation", side_effect=self.all_sessions_live), patch.object(time, "sleep", sleep), redirect_stdout(io.StringIO()):
            self.assertEqual(restart.up(LOGIN), 0)
        self.assertEqual(events, ["a-ghostty", "delay", "b-zed", "delay", "c-terminal"])

    def test_a_competing_relaunch_claim_is_not_confirmed_by_the_losing_caller(self) -> None:
        saved = entry(top_level("owner", host=tmux_host("owner")))
        self.create([saved])
        entered = threading.Event()
        release = threading.Event()
        results: list[SessionShutdown] = []

        def launch(_record: SessionShutdown, _note: str, _state: restart.DesktopLaunchState, *, dry_run: bool) -> restart.SessionLaunched:
            del dry_run
            entered.set()
            self.assertTrue(release.wait(0.8))
            return restart.SessionLaunched()

        with patch.object(restart, "_retire"), patch.object(restart, "_live_session_ids", return_value=frozenset()), patch.object(restart, "_launch_session", launch), patch.object(restart, "wait_for_confirmation") as wait:
            worker = threading.Thread(target=lambda: results.append(restart.relaunch_session(saved, dry_run=False, desktop_state=self.desktop_state)))
            worker.start()
            try:
                self.assertTrue(entered.wait(0.8))
                losing = restart.relaunch_session(saved, dry_run=False, desktop_state=self.desktop_state)
                self.assertEqual(losing["progress"], saved["progress"])
                wait.assert_not_called()
            finally:
                release.set()
                worker.join(0.8)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["progress"]["kind"], "restarting")

    def test_closed_session_resets_all_pending_timers_before_confirmation(self) -> None:
        saved = entry(top_level("owner", host=tmux_host("owner")), state=record.SessionRestoredTimersPending(
            kind="timers pending", at=STOPPED, timers=[record.PendingTimer(instance="pending", reason="failed")],
        ), timers=[
            TimerRestore(instance=name, was_enabled=True, footer=record.NoFooter(kind="no footer"))
            for name in ("done", "pending")
        ])
        self.create([saved])
        with patch.object(restart, "_retire"), patch.object(restart, "_live_session_ids", return_value=frozenset()), patch.object(restart, "_session_liveness", return_value=restart.SessionNotLive()), patch.object(restart, "_launch_session", return_value=restart.SessionLaunched()):
            current = restart.relaunch_session(saved, dry_run=False, desktop_state=self.desktop_state)
        progress = current["progress"]
        assert progress["kind"] == "restarting"
        prior = progress["prior"]
        assert prior["kind"] == "timers pending"
        self.assertEqual([timer["instance"] for timer in prior["timers"]], ["done", "pending"])

    def write_old_file(self, saved: list[SessionShutdown]) -> Path:
        account = self.state_root / LOGIN
        account.mkdir(parents=True, exist_ok=True)
        _ = (account / "lock").write_text("")
        entries: list[dict[str, object]] = []
        for item in saved:
            fields = cast(dict[str, object], cast(object, item))
            entries.append({key: fields[key] for key in ("session", "timers", "settle_message", "where", "progress", "stop_issues")})
        wire: dict[str, object] = {
            "login": LOGIN, "label": LABEL, "machine": "natedev",
            "state": "down", "requested_at": STOPPED,
            "requested_by": {"kind": "terminal"},
            "scope": {"kind": "all account sessions"},
            "conductor": {"kind": "not started"}, "force": "wait for ready", "then": "stop",
            "entries": entries, "stop_issues": [],
        }
        path = account / "record.json"
        _ = path.write_text(json.dumps(wire))
        return path

    def test_dry_run_reads_old_files_without_migrating_or_changing_any_path(self) -> None:
        old = self.write_old_file([entry(top_level("owner"))])
        before = self.files()
        directories = sorted(str(path) for path in self.state_root.rglob("*") if path.is_dir())
        with patch.object(restart, "_live_session_ids", return_value=frozenset()), patch.object(restart, "migrate") as migrate, patch.object(restart, "update_session") as update, patch.object(restart, "archive_session") as archive, patch.object(restart, "clear_finished_account_hold") as clear, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(restart.up(LOGIN, dry_run=True), 0)
        for mutation in (migrate, update, archive, clear):
            mutation.assert_not_called()
        self.assertTrue(old.is_file())
        self.assertEqual(self.files(), before)
        self.assertEqual(sorted(str(path) for path in self.state_root.rglob("*") if path.is_dir()), directories)
        self.assertNotIn("converted", output.getvalue())
        self.assertIn("manual restart owner:", output.getvalue())

    def test_real_restart_migrates_a_stop_only_record_and_clears_its_inferred_hold(self) -> None:
        old = self.write_old_file([entry(top_level("owner", host=tmux_host("owner")))])
        self.assertEqual(self.run_restarts(confirmed=frozenset({"owner"})), (0, ["owner"]))
        self.assertFalse(old.exists())
        self.assertEqual(record.session_records(LOGIN), [])
        self.assertIsNone(record.account_hold(LOGIN))

    def test_real_restart_of_an_empty_old_hold_clears_it(self) -> None:
        old = self.write_old_file([])
        with redirect_stdout(io.StringIO()):
            self.assertEqual(restart.up(LOGIN), 0)
        self.assertFalse(old.exists())
        self.assertIsNone(record.account_hold(LOGIN))

    def test_timer_failure_records_pending_work_without_another_launch(self) -> None:
        saved = entry(top_level("owner", host=tmux_host("owner")), timers=[
            TimerRestore(instance="timer", was_enabled=True, footer=record.NoFooter(kind="no footer")),
        ])
        self.create([saved])
        with patch.object(settle, "run_notifier", side_effect=RuntimeError("notifier unavailable")):
            self.assertEqual(self.run_restarts(confirmed=frozenset({"owner"})), (1, ["owner"]))
        current = self.stored_record()
        self.assertEqual(current["progress"], {
            "kind": "timers pending", "at": RESTARTED,
            "timers": [{"instance": "timer", "reason": "notifier unavailable"}],
        })
        with patch.object(restart, "_session_liveness", return_value=restart.SessionLive()), patch.object(settle, "run_notifier") as timer:
            self.assertEqual(self.run_restarts(), (0, []))
        timer.assert_called_once_with("start", "timer")
        self.assertEqual(record.session_records(LOGIN), [])

    def test_disabled_timer_keeps_its_footer_restore_without_starting_the_timer(self) -> None:
        saved = entry(top_level("owner"), timers=[TimerRestore(
            instance="disabled", was_enabled=False, footer=record.ShowrunnerFooter(kind="footer", slug="demo"),
        )])
        self.create([saved])
        with patch.object(settle, "run_notifier") as timer, patch.object(showrunner_footer, "set_footer_state") as footer:
            restart.confirm_entry(saved, RESTARTED, True)
        timer.assert_not_called()
        footer.assert_called_once_with("demo", showrunner_footer.FooterState.ON)

    def test_restart_with_alerts_disabled_does_not_send_a_phone_alert(self) -> None:
        saved = entry(top_level("owner"))
        saved["alerts"] = "none"
        self.create([saved])
        with patch.object(restart, "named_claude_account", return_value=self.account), patch.object(restart, "_up", return_value=restart.MachineRestartResult("natedev", LABEL, "partial")), patch.object(restart, "run_remote", return_value=(1, "failed")), patch.object(settle, "alert_user") as alert, redirect_stdout(io.StringIO()):
            self.assertEqual(restart.restart(restart.AccountNamed(LABEL), dry_run=False, alerts=restart.AlertWhenNotBack()), 1)
        alert.assert_not_called()

    def test_restart_with_an_unreachable_peer_uses_all_local_alert_policies(self) -> None:
        for peer_records in ((255, ""), (0, "invalid JSON")):
            for progress in (
                record.SessionWaiting(kind="waiting"),
                record.SessionReadyToStop(kind="ready", at=STOPPED),
                record.SessionRestarted(kind="restarted", at=RESTARTED),
            ):
                for policy in ("user", "none", "absent"):
                    with self.subTest(peer_records=peer_records, progress=progress, policy=policy):
                        saved = entry(top_level("owner"), state=progress)
                        saved["alerts"] = "none" if policy == "none" else "user"
                        local_records = [] if policy == "absent" else [saved]

                        def remote(args: list[str], **_kwargs: object) -> tuple[int, str]:
                            return peer_records if args[0] == "records" else (255, "")

                        with (
                            patch.object(restart, "named_claude_account", return_value=self.account),
                            patch.object(restart, "_up", return_value=restart.MachineRestartResult("natedev", LABEL, "nothing")),
                            patch.object(restart, "session_records", return_value=local_records),
                            patch.object(restart, "run_remote", remote),
                            patch.object(settle, "alert_user") as alert,
                            redirect_stdout(io.StringIO()),
                        ):
                            self.assertEqual(restart.restart(restart.AccountNamed(LABEL), dry_run=False, alerts=restart.AlertWhenNotBack()), 0)
                        self.assertEqual(alert.call_count, 0 if policy == "none" else 1)

    def test_restart_uses_the_peer_incomplete_records_alert_policy(self) -> None:
        for policy in ("none", "user"):
            with self.subTest(policy=policy):
                saved = entry(top_level("peer"), state=record.SessionRestartFailed(kind="restart failed", at=RESTARTED, reason="not live"))
                saved["machine"] = "Mac"
                saved["alerts"] = "none" if policy == "none" else "user"
                unrelated = copy.deepcopy(saved)
                unrelated["login"] = "other@example.com"
                unrelated["alerts"] = "user"
                calls: list[list[str]] = []

                def remote(args: list[str], **_kwargs: object) -> tuple[int, str]:
                    calls.append(args)
                    if args == ["records", "--json", "--here"]:
                        return 0, json.dumps([saved, unrelated])
                    return 1, peer_json(outcome="partial", lines=["Mac: peer restart failed: not live"])

                with (
                    patch.object(restart, "named_claude_account", return_value=self.account),
                    patch.object(restart, "_up", return_value=restart.MachineRestartResult("natedev", LABEL, "nothing")),
                    patch.object(restart, "run_remote", remote),
                    patch.object(settle, "alert_user") as alert,
                    redirect_stdout(io.StringIO()),
                ):
                    self.assertEqual(restart.restart(restart.AccountNamed(LABEL), dry_run=False, alerts=restart.AlertWhenNotBack()), 1)
                self.assertEqual(alert.call_count, 0 if policy == "none" else 1)
                self.assertEqual(calls, [["up", LOGIN, "--here", "--json"], ["records", "--json", "--here"]])

    def test_unreached_restore_uses_local_waiting_policy_without_reading_peer_records(self) -> None:
        replies = [(0, "Mac: restarted"), (0, "{}"), (255, ""), (2, peer_json())]
        for policy in ("user", "none"):
            for reply in replies:
                with self.subTest(policy=policy, reply=reply):
                    saved = entry(top_level("waiting"), state={"kind": "waiting"})
                    saved["alerts"] = policy
                    calls: list[list[str]] = []
                    def remote(args: list[str], **_kwargs: object) -> tuple[int, str]:
                        calls.append(args)
                        return (0, "[]") if args[0] == "records" else reply
                    with (
                        patch.object(restart, "named_claude_account", return_value=self.account),
                        patch.object(restart, "_up", return_value=restart.MachineRestartResult("natedev", LABEL, "nothing")),
                        patch.object(restart, "session_records", return_value=[saved]),
                        patch.object(restart, "run_remote", side_effect=remote),
                        patch.object(settle, "alert_user") as alert,
                        redirect_stdout(io.StringIO()) as output,
                    ):
                        self.assertEqual(restart.restart(restart.AccountNamed(LOGIN), dry_run=False, alerts=restart.AlertWhenNotBack()), 0)
                    self.assertIn("not reached", output.getvalue())
                    self.assertEqual(alert.call_count, int(policy == "user"))
                    self.assertEqual(calls, [["up", LOGIN, "--here", "--json"]])

    def test_unreached_selected_restore_policy_uses_only_chosen_local_records(self) -> None:
        chosen = entry(top_level("chosen"), state={"kind": "waiting"})
        chosen["alerts"] = "none"
        sibling = entry(top_level("sibling"), state={"kind": "waiting"})
        with (
            patch.object(restart, "named_claude_account", return_value=self.account),
            patch.object(restart, "_up", return_value=restart.MachineRestartResult("natedev", LABEL, "nothing")),
            patch.object(restart, "session_records", return_value=[chosen, sibling]),
            patch.object(restart, "run_remote", return_value=(0, "Mac: restarted")) as peer,
            patch.object(settle, "alert_user") as alert,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(restart.restart(restart.AccountNamed(LOGIN), dry_run=False,
                alerts=restart.AlertWhenNotBack(), only=["chosen", "remote"]), 0)
        peer.assert_called_once_with(["up", LOGIN, "--here", "--json", "--only", "chosen,remote"], limit={"kind": "while link alive"})
        alert.assert_not_called()

    def test_restart_alert_policy_ignores_successfully_restored_user_records(self) -> None:
        complete = entry(top_level("complete"))
        incomplete = entry(top_level("incomplete"), state=record.SessionRestartFailed(kind="restart failed", at=RESTARTED, reason="not live"))
        incomplete["alerts"] = "none"
        self.create([complete, incomplete])

        def up(_login: str, _dry_run: bool, _only: Iterable[str]) -> restart.MachineRestartResult:
            record.archive_session(LOGIN, "complete")
            return restart.MachineRestartResult("natedev", LABEL, "partial", failed=1)

        def remote(args: list[str], **_kwargs: object) -> tuple[int, str]:
            return (0, "[]") if args[0] == "records" else (0, peer_json())

        with (
            patch.object(restart, "named_claude_account", return_value=self.account),
            patch.object(restart, "_up", up), patch.object(restart, "run_remote", remote),
            patch.object(settle, "alert_user") as alert, redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(restart.restart(restart.AccountNamed(LABEL), dry_run=False, alerts=restart.AlertWhenNotBack()), 1)
        alert.assert_not_called()

    def test_direct_relaunch_requires_the_callers_desktop_state(self) -> None:
        saved = entry(top_level("window", host=inventory.TerminalHost(kind="terminal")))
        call = cast(Callable[..., SessionShutdown], restart.relaunch_session)
        with patch.object(restart, "launch_entry") as launch:
            with self.assertRaisesRegex(TypeError, "desktop_state"):
                _ = call(saved, dry_run=True)
        launch.assert_not_called()


    def test_dry_run_of_an_old_record_without_a_lock_adds_no_file(self) -> None:
        old = self.write_old_file([entry(top_level("owner"))])
        (old.parent / "lock").unlink()
        before = self.files()
        with patch.object(restart, "_live_session_ids", return_value=frozenset()), redirect_stdout(io.StringIO()):
            self.assertEqual(restart.up(LOGIN, dry_run=True), 0)
        self.assertEqual(self.files(), before)

    def test_relaunch_retries_unconfirmed_owner_cleanup_before_launch(self) -> None:
        saved = entry(unit("owner"))
        saved["stop_issues"] = [record.UnitTmuxLeftRunning(
            kind="unit tmux session left running", at=STOPPED,
            tmux_session="tmux-owner", cause="stop not confirmed",
        )]
        self.create([saved])
        events: list[str] = []

        def cleanup(current: SessionShutdown) -> SessionShutdown:
            events.append("cleanup")
            def clean(changing: SessionShutdown) -> None:
                changing["stop_issues"] = []
            return record.update_session(current["login"], current["session"]["session_id"], clean)

        def launch(current: SessionShutdown, _note: str, _state: restart.DesktopLaunchState, *, dry_run: bool) -> restart.SessionLaunched:
            del dry_run
            events.append("launch")
            self.assertEqual(current["stop_issues"], [])
            return restart.SessionLaunched()

        import stop as stop_work
        with patch.object(stop_work, "stop_session", cleanup), patch.object(restart, "_retire"), patch.object(restart, "_live_session_ids", return_value=frozenset()), patch.object(restart, "_launch_session", launch):
            _ = restart.relaunch_session(saved, dry_run=False, desktop_state=self.desktop_state)
        self.assertEqual(events, ["cleanup", "launch"])


    def test_unconfirmed_cleanup_prevents_launch_and_timer_restore(self) -> None:
        saved = entry(unit("owner"), timers=[TimerRestore(instance="timer", was_enabled=True, footer=record.NoFooter(kind="no footer"))])
        saved["stop_issues"] = [record.UnitTmuxLeftRunning(
            kind="unit tmux session left running", at=STOPPED,
            tmux_session="tmux-owner", cause="stop not confirmed",
        )]
        self.create([saved])
        import stop as stop_work
        with patch.object(stop_work, "stop_session", return_value=saved), patch.object(restart, "_live_session_ids", return_value=frozenset()), patch.object(restart, "_launch_session") as launch, patch.object(settle, "run_notifier") as timers:
            current = restart.relaunch_session(saved, dry_run=False, desktop_state=self.desktop_state)
        launch.assert_not_called()
        timers.assert_not_called()
        self.assertEqual(current["progress"]["kind"], "restart failed")

    def test_cleanup_exception_does_not_prevent_other_sessions_from_returning(self) -> None:
        saved = entry(unit("broken"))
        saved["stop_issues"] = [record.UnitTmuxLeftRunning(
            kind="unit tmux session left running", at=STOPPED,
            tmux_session="tmux-broken", cause="stop not confirmed",
        )]
        self.create([saved, entry(top_level("good", host=tmux_host("good")))])
        import stop as stop_work
        with patch.object(stop_work, "stop_session", side_effect=RuntimeError("cleanup failed")):
            self.assertEqual(self.run_restarts(confirmed=frozenset({"good"})), (1, ["good"]))
        self.assertEqual(record.session_records(LOGIN)[0]["progress"], {
            "kind": "restart failed", "at": RESTARTED, "reason": "cleanup failed",
        })

    def test_an_already_live_owner_is_not_cleaned_up_before_reconciliation(self) -> None:
        saved = entry(unit("owner"))
        saved["stop_issues"] = [record.UnitTmuxLeftRunning(
            kind="unit tmux session left running", at=STOPPED,
            tmux_session="tmux-owner", cause="stop not confirmed",
        )]
        self.create([saved])
        import stop as stop_work
        with patch.object(stop_work, "stop_session") as cleanup:
            self.assertEqual(self.run_restarts(live=frozenset({"owner"})), (0, []))
        cleanup.assert_not_called()


    def test_up_does_not_confirm_a_stale_snapshot_after_losing_its_relaunch_claim(self) -> None:
        for host_kind in ("tmux", "terminal"):
            for requester in (False, True):
                with self.subTest(host=host_kind, requester=requester), patch.dict(os.environ, {
                    "SHUTDOWN_STATE_DIR": str(self.root / f"race-{host_kind}-{requester}"),
                }):
                    host = tmux_host("owner") if host_kind == "tmux" else inventory.TerminalHost(kind="terminal")
                    saved = entry(top_level("owner", host=host))
                    if requester:
                        saved["requested_by"] = record.FromSession(kind="session", session_id="owner")
                    saved["progress"] = record.SessionRestarting(
                        kind="restarting", at=STOPPED,
                        prior=record.SessionStopped(kind="stopped", at=STOPPED),
                    )
                    self.create([saved])
                    entered = threading.Event()
                    release = threading.Event()
                    failures: list[BaseException] = []

                    def launch(_saved: SessionShutdown, _note: str, _desktop: restart.DesktopLaunchState, *, dry_run: bool) -> restart.SessionLaunched:
                        del dry_run
                        entered.set()
                        self.assertTrue(release.wait(0.8))
                        return restart.SessionLaunched()

                    def owner() -> None:
                        try:
                            _ = restart.relaunch_session(saved, dry_run=False, desktop_state=self.desktop_state)
                        except BaseException as error:
                            failures.append(error)

                    reads = 0
                    def read_snapshot(login: str) -> list[SessionShutdown]:
                        nonlocal reads
                        reads += 1
                        return [saved] if reads == 1 else record.session_records(login)

                    with patch.object(restart, "_retire"), patch.object(restart, "_live_session_ids", return_value=frozenset()), patch.object(restart, "_launch_session", launch):
                        worker = threading.Thread(target=owner)
                        worker.start()
                        try:
                            self.assertTrue(entered.wait(0.8))
                            with patch.object(restart, "session_records", read_snapshot), patch.object(restart, "wait_for_confirmation", return_value=registry_confirmation(frozenset())) as wait, redirect_stdout(io.StringIO()):
                                self.assertEqual(restart.up(LOGIN), 0)
                            wait.assert_not_called()
                            current = record.find_session(LOGIN, "owner")
                            assert current is not None
                            self.assertEqual(current["progress"], {
                                "kind": "restarting", "at": RESTARTED,
                                "prior": record.SessionStopped(kind="stopped", at=STOPPED),
                            })
                        finally:
                            release.set()
                            worker.join(0.8)
                    self.assertFalse(worker.is_alive())
                    self.assertEqual(failures, [])

    def test_up_confirms_a_stale_relaunch_claim_won_by_its_own_worker(self) -> None:
        saved = entry(top_level("owner", host=tmux_host("owner")))
        saved["progress"] = record.SessionRestarting(
            kind="restarting", at=STOPPED,
            prior=record.SessionStopped(kind="stopped", at=STOPPED),
        )
        self.create([saved])
        self.assertEqual(self.run_restarts(confirmed=frozenset({"owner"})), (0, ["owner"]))
        self.assertIsNone(record.find_session(LOGIN, "owner"))


    def test_confirmation_leaves_a_replacement_claim_and_its_timers_unchanged(self) -> None:
        for live in (False, True):
            with self.subTest(live=live), patch.dict(os.environ, {"SHUTDOWN_STATE_DIR": str(self.root / f"confirm-{live}")}):
                saved = entry(top_level("owner"), timers=[TimerRestore(
                    instance="timer", was_enabled=True, footer=record.NoFooter(kind="no footer"),
                )])
                saved["progress"] = record.SessionRestarting(
                    kind="restarting", at=STOPPED, prior=record.SessionStopped(kind="stopped", at=STOPPED),
                )
                self.create([saved])
                replacement = record.SessionRestarting(
                    kind="restarting", at=RESTARTED, prior=record.SessionStopped(kind="stopped", at=STOPPED),
                )
                def replace(current: SessionShutdown) -> None:
                    current["progress"] = replacement
                _ = record.update_session(LOGIN, "owner", replace)
                with patch.object(settle, "run_notifier") as timer:
                    restart.confirm_entry(saved, STOPPED, live)
                timer.assert_not_called()
                self.assertEqual(self.stored_record()["progress"], replacement)

    def test_timer_completion_does_not_overwrite_a_replacement_claim(self) -> None:
        saved = entry(top_level("owner"), timers=[TimerRestore(
            instance="timer", was_enabled=True, footer=record.NoFooter(kind="no footer"),
        )])
        saved["progress"] = record.SessionRestarting(
            kind="restarting", at=STOPPED, prior=record.SessionStopped(kind="stopped", at=STOPPED),
        )
        self.create([saved])
        replacement = record.SessionRestarting(
            kind="restarting", at=RESTARTED, prior=record.SessionStopped(kind="stopped", at=STOPPED),
        )
        def timer(_verb: str, _instance: str) -> None:
            def replace(current: SessionShutdown) -> None:
                current["progress"] = replacement
            _ = record.update_session(LOGIN, "owner", replace)
        with patch.object(settle, "run_notifier", timer):
            restart.confirm_entry(saved, STOPPED, True)
        self.assertEqual(self.stored_record()["progress"], replacement)

    def test_launcher_does_not_return_a_replacement_claim_for_confirmation(self) -> None:
        saved = entry(top_level("owner", host=tmux_host("owner")))
        self.create([saved])
        replacement = record.SessionRestarting(
            kind="restarting", at="2026-10-10T01:04:03+00:00",
            prior=record.SessionStopped(kind="stopped", at=STOPPED),
        )
        def launch(_saved: SessionShutdown, _note: str, _desktop: restart.DesktopLaunchState, *, dry_run: bool) -> restart.SessionLaunched:
            del dry_run
            def replace(current: SessionShutdown) -> None:
                current["progress"] = replacement
            _ = record.update_session(LOGIN, "owner", replace)
            return restart.SessionLaunched()
        with patch.object(restart, "_retire"), patch.object(restart, "_live_session_ids", return_value=frozenset()), patch.object(restart, "_launch_session", launch):
            result = restart.relaunch_session(saved, dry_run=False, desktop_state=self.desktop_state)
        self.assertFalse(restart.owns_relaunch_claim(saved, result))
        self.assertEqual(self.stored_record()["progress"], replacement)


    def test_selected_up_restores_requester_while_same_request_siblings_wait(self) -> None:
        requester = entry(top_level("requester", name="Requester"))
        requester["requested_by"] = record.FromSession(kind="session", session_id="requester")
        waiting = entry(top_level("waiting"), state={"kind": "waiting"})
        another_stopped = entry(top_level("another"))
        self.create([requester, waiting, another_stopped])
        launched: list[str] = []
        def launch(saved: SessionShutdown, _note: str, _desktop: restart.DesktopLaunchState, *, dry_run: bool) -> restart.SessionLaunched:
            self.assertFalse(dry_run)
            launched.append(saved["session"]["session_id"])
            return restart.SessionLaunched()
        with patch.object(restart, "_launch_session", side_effect=launch), patch.object(restart, "_live_session_ids", return_value=frozenset()), patch.object(restart, "wait_for_confirmation", side_effect=self.all_sessions_live), patch.object(restart, "_retire"), patch.object(restart, "_send_restart_note", return_value=settle.MessageSent(kind="sent")), redirect_stdout(io.StringIO()):
            self.assertEqual(restart.up(LOGIN, only=["requester"]), 0)
        self.assertEqual(launched, ["requester"])
        self.assertIsNone(record.find_session(LOGIN, "requester"))
        self.assertEqual(record.find_session(LOGIN, "waiting"), waiting)
        self.assertEqual(record.find_session(LOGIN, "another"), another_stopped)

    def test_selected_restart_routes_only_peer_id_and_preserves_flags(self) -> None:
        waiting = entry(top_level("waiting"), state={"kind": "waiting"})
        self.create([waiting])
        with patch.object(restart, "named_claude_account", return_value=self.account), patch.object(restart, "run_remote", return_value=(0, peer_json(lines=["Mac: restored Remote"]))) as peer, patch.object(restart, "_launch_session") as launch, patch.object(settle, "alert_user") as alert, redirect_stdout(io.StringIO()):
            self.assertEqual(restart.restart(restart.AccountNamed(LABEL), only=["remote-id"], dry_run=True, alerts=restart.NeverAlert()), 0)
        peer.assert_called_once_with(["up", LOGIN, "--here", "--json", "--dry-run", "--only", "remote-id"], limit={"kind": "while link alive"})
        launch.assert_not_called()
        alert.assert_not_called()
        self.assertEqual(record.find_session(LOGIN, "waiting"), waiting)

    def test_selected_restart_dry_run_changes_no_file_or_other_session(self) -> None:
        selected, other = entry(unit("selected")), entry(unit("other"))
        self.create([selected, other])
        before = {path.relative_to(self.state_root): path.read_bytes() for path in self.state_root.rglob("*") if path.is_file()}
        output = io.StringIO()
        with patch.object(restart, "_run", side_effect=CommandRecorder()), patch.object(restart, "_live_session_ids", return_value=frozenset()), redirect_stdout(output):
            self.assertEqual(restart.up(LOGIN, dry_run=True, only=["selected"]), 0)
        self.assertIn("--resume selected", output.getvalue())
        self.assertNotIn("--resume other", output.getvalue())
        self.assertEqual({path.relative_to(self.state_root): path.read_bytes() for path in self.state_root.rglob("*") if path.is_file()}, before)

    def test_selected_up_refusal_adds_no_failed_or_restarted_count(self) -> None:
        saved = entry(top_level("waiting", name="Waiting"), state={"kind": "waiting"})
        self.create([saved])
        output = io.StringIO()
        with patch.object(restart, "_print_result") as summary, redirect_stdout(output):
            result = restart._up(LOGIN, False, ["waiting"])  # pyright: ignore[reportPrivateUsage]
        self.assertEqual((result.restarted, result.seats, result.manual, result.failed), (0, 0, 0, 0))
        self.assertEqual(result.failed_sessions, ())
        self.assertEqual(output.getvalue(), "Waiting is waiting, not stopped; nothing to restart\n")
        summary.assert_not_called()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(restart.up(LOGIN, only=["waiting"]), 1)

    def test_selected_peer_restart_refusal_exits_one_without_phone_alert_or_summary(self) -> None:
        line = "Peer is waiting, not stopped; nothing to restart"
        output = io.StringIO()
        with patch.object(restart, "named_claude_account", return_value=self.account), patch.object(settle, "inventory", return_value=inventory.Inventory(machine="natedev", login=LOGIN, label=LABEL, sessions=[], unattributed=[])), patch.object(restart, "run_remote", return_value=(1, peer_json(lines=[line], refused=[restart.RestartNotStopped(kind="not stopped", session_id="peer-id", name="Peer", progress="waiting")])) ) as peer, patch.object(settle, "alert_user") as alert, patch.object(restart, "_print_result") as summary, redirect_stdout(output):
            self.assertEqual(restart.restart(restart.AccountNamed(LABEL), only=["peer-id"], dry_run=False, alerts=restart.AlertWhenNotBack()), 1)
        self.assertEqual(output.getvalue(), line + "\n")
        peer.assert_called_once_with(["up", LOGIN, "--here", "--json", "--only", "peer-id"], limit={"kind": "while link alive"})
        alert.assert_not_called()
        summary.assert_not_called()

    def test_selected_peer_silent_restart_failure_ignores_untouched_local_alert_policy(self) -> None:
        sibling = entry(top_level("sibling"))
        self.create([sibling])
        chosen = entry(top_level("peer", name="Peer"), state={"kind": "restart failed", "at": RESTARTED, "reason": "not live"})
        chosen["machine"], chosen["alerts"] = "Mac", "none"
        def remote(arguments: list[str], **_keywords: object) -> tuple[int, str]:
            return (0, json.dumps([chosen])) if arguments[0] == "records" else (1, peer_json(outcome="partial", lines=["Mac: Peer restart failed: not live"]))
        with patch.object(restart, "named_claude_account", return_value=self.account), patch.object(settle, "inventory", return_value=inventory.Inventory(machine="natedev", login=LOGIN, label=LABEL, sessions=[], unattributed=[])), patch.object(restart, "run_remote", side_effect=remote), patch.object(settle, "alert_user") as alert, redirect_stdout(io.StringIO()):
            self.assertEqual(restart.restart(restart.AccountNamed(LABEL), only=["peer"], dry_run=False, alerts=restart.AlertWhenNotBack()), 1)
        alert.assert_not_called()
        self.assertEqual(record.find_session(LOGIN, "sibling"), sibling)
    def test_remote_control_missing_prints_diagnostic_archives_restart_and_alerts_action(self) -> None:
        for policy in ("user", "none"):
            for command_alerts in (restart.AlertWhenNotBack(), restart.NeverAlert()):
                with self.subTest(policy=policy, command_alerts=type(command_alerts).__name__):
                    saved = entry(top_level("owner", name="Owner", host=tmux_host("owner")))
                    saved["alerts"] = policy
                    saved["requested_at"] = "2026-10-09T21:49:" + str(10 + (2 if policy == "none" else 0) + int(isinstance(command_alerts, restart.NeverAlert))) + "+00:00"
                    self.create([saved])
                    output = io.StringIO()
                    with patch.object(restart, "named_claude_account", return_value=self.account), patch.object(restart, "_retire"), patch.object(restart, "_launch_session", return_value=restart.SessionLaunched()), patch.object(restart, "_live_session_ids", return_value=frozenset()), patch.object(restart, "wait_for_confirmation", side_effect=self.all_sessions_live), patch.object(restart, "wait_for_remote_control", return_value=frozenset()) as check, patch.object(restart, "_send_restart_note", return_value=settle.MessageSent(kind="sent")), patch.object(settle, "alert_user") as alert, redirect_stdout(output):
                        self.assertEqual(restart.restart(restart.AccountNamed(LOGIN), dry_run=False, alerts=command_alerts, here=True), 0)
                    self.assertIn("Owner: running, no Remote Control\n", output.getvalue())
                    check.assert_called_once()
                    self.assertIsNone(record.find_session(LOGIN, "owner"))
                    history = list((self.state_root / LOGIN / "history").glob("*.json"))
                    self.assertEqual(cast(dict[str, object], json.loads(history[-1].read_text()))["progress"], {"kind": "restarted", "at": RESTARTED})
                    self.assertEqual(alert.call_count, int(policy == "user" and isinstance(command_alerts, restart.AlertWhenNotBack)))
                    if alert.called:
                        self.assertIn("Owner", cast(str, cast(object, alert.call_args.args[1])))
                        self.assertIn("/remote-control", cast(ActionRequired, cast(object, alert.call_args.args[2])).text)
                        self.assertNotIn("run /shutdown restart again", cast(str, cast(object, alert.call_args.args[1])))

    def test_remote_control_active_restart_sends_no_alert(self) -> None:
        saved = entry(top_level("owner", name="Owner", host=tmux_host("owner")))
        self.create([saved])
        with patch.object(restart, "named_claude_account", return_value=self.account), patch.object(restart, "_retire"), patch.object(restart, "_launch_session", return_value=restart.SessionLaunched()), patch.object(restart, "_live_session_ids", return_value=frozenset()), patch.object(restart, "wait_for_confirmation", side_effect=self.all_sessions_live), patch.object(restart, "_send_restart_note", return_value=settle.MessageSent(kind="sent")), patch.object(settle, "alert_user") as alert, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(restart.restart(restart.AccountNamed(LOGIN), dry_run=False, alerts=restart.AlertWhenNotBack(), here=True), 0)
        self.assertNotIn("no Remote Control", output.getvalue())
        self.assertIsNone(record.find_session(LOGIN, "owner"))
        alert.assert_not_called()

    def test_remote_control_peer_policy_and_name_survive_archived_record(self) -> None:
        for policy in ("user", "none"):
            with self.subTest(policy=policy):
                diagnostic = "Remote: running, no Remote Control"
                missing = record.NoRemoteControl(session_id="peer", name="Remote", alerts=policy)
                with patch.object(restart, "named_claude_account", return_value=self.account), patch.object(restart, "run_remote", return_value=(0, peer_json(lines=[diagnostic], missing=[missing]))), patch.object(settle, "alert_user") as alert, redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(restart.restart(restart.AccountNamed(LOGIN), dry_run=False, alerts=restart.AlertWhenNotBack()), 0)
                self.assertIn(diagnostic, output.getvalue())
                self.assertEqual(alert.call_count, int(policy == "user"))
                if alert.called:
                    self.assertIn("Remote", cast(str, cast(object, alert.call_args.args[1])))
                    self.assertIn("/remote-control", cast(ActionRequired, cast(object, alert.call_args.args[2])).text)

    def test_peer_no_shutdown_refusal_keeps_line_without_alert(self) -> None:
        refusal = restart.RestartNoShutdown(kind="no shutdown", session_id="peer", name="Remote")
        with patch.object(restart, "named_claude_account", return_value=self.account), patch.object(restart, "run_remote", return_value=(1, peer_json(lines=[restart.refusal_line(refusal)], refused=[refusal]))), patch.object(settle, "alert_user") as alert, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(restart.restart(restart.AccountNamed(LOGIN), dry_run=False, alerts=restart.AlertWhenNotBack(), only=["peer"]), 1)
        self.assertEqual(output.getvalue(), "Remote: no shutdown in progress\n")
        alert.assert_not_called()

    def test_peer_refusal_beside_real_failure_alerts_only_the_failure(self) -> None:
        refused = restart.RestartNotStopped(kind="not stopped", session_id="waiting", name="Waiting", progress="waiting")
        lines = [restart.refusal_line(refused), "Mac: Failed restart failed: not live"]
        def remote(args: list[str], **_kwargs: object) -> tuple[int, str]:
            return (0, "[]") if args[0] == "records" else (1, peer_json(outcome="partial", lines=lines, refused=[refused]))
        with patch.object(restart, "named_claude_account", return_value=self.account), patch.object(restart, "run_remote", side_effect=remote), patch.object(settle, "alert_user") as alert, redirect_stdout(io.StringIO()):
            self.assertEqual(restart.restart(restart.AccountNamed(LOGIN), dry_run=False, alerts=restart.AlertWhenNotBack()), 1)
        alert.assert_called_once()
        self.assertIn("Failed", cast(str, cast(object, alert.call_args.args[1])))
        self.assertNotIn("Waiting", cast(str, cast(object, alert.call_args.args[1])))

    def test_peer_result_refuses_old_output_and_invalid_nested_fields(self) -> None:
        valid = cast(dict[str, object], json.loads(peer_json()))
        invalid: list[str] = ["Mac: restarted", "[]", "{}"]
        for key, value in (("outcome", []), ("lines", [3]), ("no_remote_control", [{"session_id": "x", "name": "X", "alerts": "other"}]), ("refused", [{"kind": "not stopped", "session_id": "x", "name": "X", "progress": "stopped"}])):
            invalid.append(json.dumps({**valid, key: value}))
        for response in invalid:
            with self.subTest(response=response), self.assertRaises(ValueError):
                _ = restart.parse_peer_result(response)



if __name__ == "__main__":
    _ = unittest.main()
