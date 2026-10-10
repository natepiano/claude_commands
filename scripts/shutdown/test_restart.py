#!/usr/bin/env python3
"""Behavioral tests for restoring every session in a shutdown record."""

from __future__ import annotations

import io
import json
import os
import shlex
import subprocess
import tempfile
import threading
import unittest
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Literal, cast, final, override
from unittest.mock import patch

import inventory
import record
import remote as remote_transport
import restart
import conversation_pause
import settle
import showrunner_footer
import shutdown
from account import Account, UnknownAccountName, UnreadableAccount
from record import (
    PassiveSeatReadyToStop,
    ProcessIdentityLost,
    SessionAlreadyGone,
    SessionProgress,
    SessionReadyToStop,
    SessionStopFailed,
    SessionStopped,
    SessionWaiting,
    ShutdownRecord,
    ShutdownSessionEntry,
    ShutdownState,
    TimerRestore,
    Where,
)


LOGIN = "owner@example.com"
LABEL = "claude 2"
STOPPED = "2026-10-09T21:49:10+00:00"
RESTARTED = "2026-10-10T01:02:03+00:00"


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
) -> inventory.UnitSession:
    return inventory.UnitSession(
        **common_session(session_id, run_dirs=run_dirs),
        kind="unit",
        host=inventory.UnitHost(
            kind="unit",
            production="demo",
            unit=f"{session_id}-unit",
            doc="/tmp/demo production.md",
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


def progress(kind: Literal[
    "waiting",
    "ready",
    "passive seat ready",
    "stopped",
    "already gone",
    "process identity lost",
    "stop failed",
]) -> SessionProgress:
    if kind == "waiting":
        return SessionWaiting(kind="waiting")
    if kind == "ready":
        return SessionReadyToStop(kind="ready", at=STOPPED)
    if kind == "passive seat ready":
        return PassiveSeatReadyToStop(kind="passive seat ready", at=STOPPED)
    if kind == "stopped":
        return SessionStopped(kind="stopped", at=STOPPED)
    if kind == "already gone":
        return SessionAlreadyGone(kind="already gone", at=STOPPED)
    if kind == "process identity lost":
        return ProcessIdentityLost(kind="process identity lost", at=STOPPED)
    return SessionStopFailed(kind="stop failed", at=STOPPED, reason="could not stop")


def entry(
    session: inventory.Session,
    *,
    state: SessionProgress | None = None,
    where: Where | None = None,
    timers: list[TimerRestore] | None = None,
) -> ShutdownSessionEntry:
    return ShutdownSessionEntry(
        session=session,
        timers=list(timers or []),
        settle_message=record.SettleMessageNotSent(kind="not sent"),
        where=where or record.WhereSaid(kind="said", text=f"working on {session['name']}", at=STOPPED),
        progress=state or SessionStopped(kind="stopped", at=STOPPED),
        stop_issues=[],
    )


def shutdown_record(
    entries: list[ShutdownSessionEntry],
    *,
    state: ShutdownState = "down",
    machine: str = "natedev",
    requester: str | None = None,
    stopped_at: str = STOPPED,
) -> ShutdownRecord:
    requested_by: record.RequestOrigin = (
        record.FromSession(kind="session", session_id=requester)
        if requester is not None
        else record.FromTerminal(kind="terminal")
    )
    return ShutdownRecord(
        login=LOGIN,
        label=LABEL,
        machine=machine,
        state=state,
        requested_at=stopped_at,
        requested_by=requested_by,
        scope=record.AllAccountSessions(kind="all account sessions"),
        conductor=record.ConductorNotStarted(kind="not started"),
        force="wait for ready",
        entries=entries,
        stop_issues=[],
    )


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
class RestartTests(unittest.TestCase):
    root: Path = Path()
    state_root: Path = Path()
    account = Account("claude", LOGIN, LABEL)

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.state_root = self.root / "shutdown"
        environment_context: object = cast(
            object,
            self.enterContext(
                patch.dict(
                    os.environ,
                    {
                        **os.environ,
                        "SHUTDOWN_STATE_DIR": str(self.state_root),
                        "NOTIFIER_STATE_DIR": str(self.root / "notifier"),
                        "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"),
                        "CONVERSATION_PAUSE_STATE_DIR": str(self.root / "pause"),
                        "PLAN_DELEGATE_HISTORY_DIR": str(self.root / "delegate"),
                        "SHUTDOWN_DELEGATE_ROOT": str(self.root / "delegate"),
                        "AGENT_NOTES_DIR": str(self.root / "notes"),
                    },
                    clear=True,
                )
            ),
        )
        del environment_context

    def stored_record(self) -> ShutdownRecord:
        found = record.find_live(LOGIN)
        self.assertEqual(found["kind"], "live")
        if found["kind"] != "live":
            self.fail("expected a live shutdown record")
        return found["record"]

    def test_notes_use_the_stop_dates_zone_and_explain_a_missing_where(self) -> None:
        october = entry(
            top_level("october"),
            where=record.WhereSaid(
                kind="said",
                text="quoted 'work' and $()",
                at=STOPPED,
            ),
        )
        october_note = restart.restart_note(
            shutdown_record([october]), october, RESTARTED
        )
        self.assertIn("stopped 2026-10-09 14:49 PDT", october_note)
        self.assertIn("restarted 2026-10-09 18:02 PDT", october_note)
        self.assertIn("Before it you wrote: quoted 'work' and $().", october_note)

        january_time = "2026-01-15T17:30:00+00:00"
        january = entry(
            top_level("january"), where=record.WhereNotSaid(kind="not said")
        )
        january_note = restart.restart_note(
            shutdown_record([january], stopped_at=january_time),
            january,
            "2026-01-15T18:00:00+00:00",
        )
        self.assertIn("stopped 2026-01-15 09:30 PST", january_note)
        self.assertIn("restarted 2026-01-15 10:00 PST", january_note)
        self.assertIn("It left no note of where it was", january_note)
        self.assertNotIn("Before it you wrote", january_note)

    def test_restart_note_adds_only_missing_sentence_punctuation(self) -> None:
        for summary in ("finished the work.", "finished the work"):
            with self.subTest(summary=summary):
                restored = entry(
                    top_level("punctuation"),
                    where=record.WhereSaid(kind="said", text=summary, at=STOPPED),
                )
                note = restart.restart_note(
                    shutdown_record([restored]), restored, RESTARTED
                )
                self.assertIn(
                    "Before it you wrote: finished the work. First run",
                    note,
                )
                self.assertNotIn("finished the work.. First run", note)

    def test_restart_prompts_are_scheduled_before_showrunner_and_window_launches(
        self,
    ) -> None:
        restored_entries = [
            entry(showrunner("scheduled-showrunner")),
            entry(
                top_level(
                    "scheduled-window",
                    host=inventory.WindowHost(
                        kind="ghostty",
                        window_shell=10,
                        desktop=inventory.DesktopNotInSnapshot(
                            kind="not in snapshot"
                        ),
                    ),
                )
            ),
            entry(
                top_level(
                    "scheduled-tmux",
                    host=recorded_tmux_host(
                        "scheduled-tmux-host", "%scheduled", 410
                    ),
                )
            ),
        ]
        saved = shutdown_record(restored_entries)
        record.create(saved)
        expected_prompts: dict[str, str] = {}
        for restored in restored_entries:
            session_id = restored["session"]["session_id"]
            expected_prompts[session_id] = (
                "/showrunner:produce /tmp/demo production.md resume"
                if restored["session"]["kind"] == "showrunner"
                else restart.restart_note(saved, restored, RESTARTED)
            )
            conversation_pause.record_scheduled_prompts(
                session_id, (f"wakeup for {session_id}",)
            )
        observed_launches: list[str] = []

        def run(
            argv: list[str], *, dry_run: bool, check: bool = True
        ) -> subprocess.CompletedProcess[str] | None:
            del check
            if dry_run:
                return None
            shell_command = argv[-1]
            if not shell_command.endswith("; exec zsh"):
                return subprocess.CompletedProcess(argv, 0, "", "")
            launched_argv = shlex.split(
                shell_command.removesuffix("; exec zsh")
            )
            if "--resume" not in launched_argv:
                return subprocess.CompletedProcess(argv, 0, "", "")
            resume_index = launched_argv.index("--resume")
            session_id = launched_argv[resume_index + 1]
            prompt = expected_prompts[session_id]
            launched_prompt = launched_argv[-1]
            self.assertEqual(launched_prompt, prompt)
            self.assertEqual(
                conversation_pause.read_scheduled_prompts(session_id),
                (f"wakeup for {session_id}", prompt),
            )
            self.assertIs(
                conversation_pause.prompt_source(
                    launched_prompt,
                    lambda: frozenset(),
                    lambda: conversation_pause.read_scheduled_prompts(
                        session_id
                    ),
                ),
                conversation_pause.PromptSource.SCHEDULED,
            )
            observed_launches.append(session_id)
            return subprocess.CompletedProcess(argv, 0, "", "")

        def probe(
            argv: list[str], *, capture_output: bool, text: bool, check: bool
        ) -> subprocess.CompletedProcess[str]:
            del capture_output, text, check
            if argv[:2] == ["tmux", "display"]:
                return subprocess.CompletedProcess(
                    argv, 0, "scheduled-tmux-host\t410\t0\n", ""
                )
            if argv[:1] == ["pgrep"]:
                return subprocess.CompletedProcess(argv, 1, "", "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        with (
            patch.object(restart, "_run", run),
            patch.object(restart, "_live_session_ids", return_value=frozenset()),
            patch.object(restart, "wait_for_session", return_value=True),
            patch.object(
                settle,
                "send_message",
                return_value=settle.MessageSent(kind="sent"),
            ),
            patch.object(settle, "record_time", return_value=RESTARTED),
            patch("subprocess.run", probe),
        ):
            self.assertEqual(restart.up(LOGIN), 0)

        self.assertCountEqual(
            observed_launches,
            ["scheduled-showrunner", "scheduled-window", "scheduled-tmux"],
        )

    def test_unit_launch_leaves_prompt_recording_to_add_unit(self) -> None:
        restored = entry(unit("scheduled-unit"))
        saved = shutdown_record([restored])
        record.create(saved)
        conversation_pause.record_scheduled_prompts(
            "scheduled-unit", ("existing wakeup",)
        )
        observed_unit_launch = False

        def run(
            argv: list[str], *, dry_run: bool, check: bool = True
        ) -> subprocess.CompletedProcess[str] | None:
            nonlocal observed_unit_launch
            del check
            if any(part.endswith("add_unit.py") for part in argv):
                observed_unit_launch = True
                self.assertEqual(
                    conversation_pause.read_scheduled_prompts("scheduled-unit"),
                    ("existing wakeup",),
                )
            return None if dry_run else subprocess.CompletedProcess(argv, 0, "", "")

        with (
            patch.object(restart, "_run", run),
            patch.object(restart, "_live_session_ids", return_value=frozenset()),
            patch.object(restart, "wait_for_session", return_value=True),
            patch.object(settle, "record_time", return_value=RESTARTED),
        ):
            self.assertEqual(restart.up(LOGIN), 0)

        self.assertTrue(observed_unit_launch)

    def test_dry_run_does_not_record_restart_notes(self) -> None:
        restored = entry(
            top_level(
                "dry-scheduled",
                host=tmux_host("dry-scheduled-host"),
            )
        )
        record.create(shutdown_record([restored]))
        conversation_pause.record_scheduled_prompts(
            "dry-scheduled", ("existing wakeup",)
        )

        with (
            patch.object(restart, "_live_session_ids", return_value=frozenset()),
            patch(
                "subprocess.run",
                return_value=subprocess.CompletedProcess(["probe"], 1, "", ""),
            ),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(restart.up(LOGIN, dry_run=True), 0)

        self.assertEqual(
            conversation_pause.read_scheduled_prompts("dry-scheduled"),
            ("existing wakeup",),
        )

    def test_dry_run_prints_each_host_argv_and_changes_nothing(self) -> None:
        entries = [
            entry(unit("unit")),
            entry(top_level(
                "ghostty",
                host=inventory.WindowHost(
                    kind="ghostty",
                    window_shell=10,
                    desktop=inventory.NamedDesktop(kind="named", name="Work Desk"),
                ),
            )),
            entry(top_level(
                "zed",
                host=inventory.WindowHost(
                    kind="zed",
                    window_shell=11,
                    desktop=inventory.DesktopNotInSnapshot(kind="not in snapshot"),
                ),
            )),
            entry(top_level("terminal", host=inventory.TerminalHost(kind="terminal"))),
            entry(top_level(
                "tmux",
                host=recorded_tmux_host("kept tmux", "%dry", 420),
            )),
            entry(top_level("unknown")),
        ]
        original = shutdown_record(entries)
        record.create(original)

        def tmux_exists(
            argv: list[str],
            *,
            capture_output: bool,
            text: bool,
            check: bool,
            env: dict[str, str] | None = None,
        ) -> subprocess.CompletedProcess[str]:
            del capture_output, text, check, env
            if argv[:2] == ["tmux", "display"]:
                return subprocess.CompletedProcess(
                    argv, 0, "kept tmux\t420\t0\n", ""
                )
            if argv[:1] == ["pgrep"]:
                return subprocess.CompletedProcess(argv, 1, "", "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        output = io.StringIO()
        with (
            patch.object(restart, "_live_session_ids", return_value=frozenset()),
            patch.object(restart, "_desktop_numbers", return_value={"Work Desk": 2}),
            patch("subprocess.run", tmux_exists),
            patch.object(settle, "record_time", return_value=RESTARTED),
            redirect_stdout(output),
        ):
            self.assertEqual(restart.up(LOGIN, dry_run=True), 0)

        lines = output.getvalue().splitlines()
        argvs = [shlex.split(line) for line in lines if not line.startswith("manual restart")]
        unit_argv = next(argv for argv in argvs if any(part.endswith("add_unit.py") for part in argv))
        self.assertEqual(unit_argv[unit_argv.index("--production") + 1], "/tmp/demo production.md")
        self.assertEqual(unit_argv[unit_argv.index("--plan") + 1], "/tmp/unit plan.md")
        self.assertEqual(unit_argv[unit_argv.index("--session-name") + 1], "unit")
        self.assertEqual(unit_argv[unit_argv.index("--tmux-session") + 1], "tmux-unit")
        self.assertTrue(any(argv == ["kdotool", "set_desktop", "2"] for argv in argvs))
        window_argvs = [argv for argv in argvs if argv[:1] == ["systemd-run"]]
        self.assertEqual(len(window_argvs), 2)
        self.assertTrue(all("ghostty" in argv for argv in window_argvs))
        terminal = next(argv for argv in argvs if argv[:1] == ["open"])
        self.assertEqual(terminal[:4], ["open", "-na", "/Applications/Nix Apps/Ghostty.app", "--args"])
        tmux = next(argv for argv in argvs if argv[:2] == ["tmux", "respawn-pane"])
        self.assertIn("%dry", tmux)
        self.assertIn(
            [
                "tmux",
                "display",
                "-p",
                "-t",
                "%dry",
                "#{session_name}\t#{pane_pid}\t#{pane_dead}",
            ],
            argvs,
        )
        self.assertIn(
            ["tmux", "has-session", "-t", "=kept tmux"], argvs
        )
        self.assertIn(["pgrep", "-P", "420"], argvs)
        self.assertTrue(any(line.startswith("manual restart unknown:") for line in lines))
        self.assertEqual(self.stored_record(), original)
        self.assertEqual(list(self.state_root.rglob("restart-note-*.txt")), [])

    def test_window_dry_run_reads_and_restores_the_starting_desktop(self) -> None:
        restored = entry(top_level(
            "window",
            host=inventory.WindowHost(
                kind="ghostty",
                window_shell=10,
                desktop=inventory.NamedDesktop(kind="named", name="Work"),
            ),
        ))
        saved = shutdown_record([restored])
        record.create(saved)
        calls: list[tuple[list[str], bool]] = []

        def run(
            argv: list[str], *, dry_run: bool, check: bool = True
        ) -> subprocess.CompletedProcess[str] | None:
            del check
            calls.append((argv, dry_run))
            if dry_run:
                print(shlex.join(argv))
                return None
            return subprocess.CompletedProcess(argv, 0, "7\n", "")

        output = io.StringIO()
        with (
            patch.object(restart, "_run", run),
            patch.object(restart, "_desktop_numbers", return_value={"Work": 3}),
            patch.object(restart, "_live_session_ids", return_value=frozenset()),
            patch.object(settle, "record_time", return_value=RESTARTED),
            redirect_stdout(output),
        ):
            self.assertEqual(restart.up(LOGIN, dry_run=True), 0)

        displayed = [
            shlex.split(line)
            for line in output.getvalue().splitlines()
            if line.startswith(("kdotool", "systemd-run"))
        ]
        self.assertEqual(displayed[0], ["kdotool", "get_desktop"])
        self.assertEqual(displayed[1], ["kdotool", "set_desktop", "3"])
        self.assertEqual(displayed[2][0], "systemd-run")
        self.assertEqual(displayed[3], ["kdotool", "set_desktop", "7"])
        self.assertEqual(
            [argv for argv, dry_run in calls if not dry_run],
            [["kdotool", "get_desktop"]],
        )
        self.assertEqual(self.stored_record(), saved)

    def test_zsh_parses_recorded_values_as_the_same_claude_arguments(self) -> None:
        cwd = self.root / "cwd with 'quotes' and $()"
        cwd.mkdir()
        name = "name with 'quotes' and $()"
        model = "model with 'quotes' and $()"
        where = "where with 'quotes' and $()"
        restored = entry(
            top_level(
                "quoted-id",
                name=name,
                cwd=str(cwd),
                model=inventory.ModelName(kind="model", name=model),
                host=inventory.WindowHost(
                    kind="ghostty",
                    window_shell=12,
                    desktop=inventory.DesktopNotInSnapshot(kind="not in snapshot"),
                ),
            ),
            where=record.WhereSaid(kind="said", text=where, at=STOPPED),
        )
        saved = shutdown_record([restored])
        record.create(saved)
        output = io.StringIO()
        with (
            patch.object(restart, "_live_session_ids", return_value=frozenset()),
            patch.object(settle, "record_time", return_value=RESTARTED),
            patch(
                "subprocess.run",
                return_value=subprocess.CompletedProcess(
                    ["kdotool", "get_desktop"], 0, "1\n", ""
                ),
            ),
            redirect_stdout(output),
        ):
            self.assertEqual(restart.up(LOGIN, dry_run=True), 0)

        launch_line = next(line for line in output.getvalue().splitlines() if "ghostty" in line)
        outer = shlex.split(launch_line)
        resume_command = outer[outer.index("-ic") + 1]
        binary = self.root / "bin"
        binary.mkdir()
        argv_log = self.root / "claude-argv.json"
        claude = binary / "claude"
        _ = claude.write_text(
            "\n".join((
                "#!/usr/bin/env python3",
                "import json, os, sys",
                "from pathlib import Path",
                "Path(os.environ['ARGV_LOG']).write_text(json.dumps(sys.argv[1:]))",
                "",
            )),
            encoding="utf-8",
        )
        _ = claude.chmod(0o755)
        final_zsh = binary / "zsh"
        _ = final_zsh.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        _ = final_zsh.chmod(0o755)
        zdotdir = self.root / "empty-zdotdir"
        zdotdir.mkdir()
        environment = {
            **os.environ,
            "ARGV_LOG": str(argv_log),
            "HOME": str(self.root),
            "PATH": f"{binary}:/usr/bin:/bin",
            "ZDOTDIR": str(zdotdir),
        }
        parsed = subprocess.run(
            ["/bin/zsh", "-f", "-ic", resume_command],
            cwd=self.root,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        self.assertEqual(parsed.returncode, 0, (parsed.stdout, parsed.stderr))
        actual = cast(
            list[str],
            cast(object, json.loads(argv_log.read_text(encoding="utf-8"))),
        )
        note = restart.restart_note(saved, restored, RESTARTED)
        self.assertEqual(actual, [
            "--resume", "quoted-id",
            "-n", name,
            "--remote-control", name,
            "--model", model,
            "--settings", '{"disableAgentView": true}',
            note,
        ])

    def test_only_one_overlapping_up_claim_processes_the_record(self) -> None:
        saved = shutdown_record([entry(top_level("one", host=inventory.TerminalHost(kind="terminal")))])
        record.create(saved)
        entered = threading.Event()
        release = threading.Event()
        processed: list[str] = []

        def process(
            current: ShutdownRecord,
            current_entry: ShutdownSessionEntry,
            restarted_at: str,
            desktop_state: restart.DesktopLaunchState,
            *,
            dry_run: bool,
        ) -> None:
            del restarted_at, desktop_state, dry_run
            session_id = current_entry["session"]["session_id"]
            processed.append(session_id)
            entered.set()
            self.assertTrue(release.wait(5))

            def finish(changing: ShutdownRecord) -> None:
                changing["entries"][0]["progress"] = record.SessionRestarted(
                    kind="restarted", at=RESTARTED
                )

            _ = record.update(current["login"], finish)
            return

        first_result: list[int] = []

        def first_up() -> None:
            first_result.append(restart.up(LOGIN))

        output = io.StringIO()
        with (
            patch.object(restart, "_process_session_entry", process),
            patch.object(settle, "record_time", return_value=RESTARTED),
            redirect_stdout(output),
        ):
            thread = threading.Thread(target=first_up)
            thread.start()
            self.assertTrue(entered.wait(5))
            second_result = restart.up(LOGIN)
            release.set()
            thread.join(5)

        self.assertFalse(thread.is_alive())
        self.assertEqual(first_result, [0])
        self.assertEqual(second_result, 0)
        self.assertEqual(processed, ["one"])
        self.assertIn("restart of claude 2 already running", output.getvalue())

    def test_an_exception_after_the_claim_leaves_restart_partial(self) -> None:
        record.create(shutdown_record([entry(top_level("one"))]))

        def fail(
            current: ShutdownRecord,
            current_entry: ShutdownSessionEntry,
            restarted_at: str,
            desktop_state: restart.DesktopLaunchState,
            *,
            dry_run: bool,
        ) -> None:
            del current, current_entry, restarted_at, desktop_state, dry_run
            raise RuntimeError("launcher exploded")

        with patch.object(restart, "_process_session_entry", fail), redirect_stderr(io.StringIO()):
            self.assertEqual(restart.up(LOGIN), 1)
        self.assertEqual(self.stored_record()["state"], "restart partial")

    def test_no_record_and_an_archived_record_are_successful_no_ops(self) -> None:
        output = io.StringIO()
        with (
            patch.object(restart, "named_claude_account", return_value=self.account),
            redirect_stdout(output),
        ):
            self.assertEqual(restart.up(LOGIN), 0)
        self.assertIn("no shutdown of claude 2 to restart", output.getvalue())

        completed = shutdown_record([], state="up")
        record.create(completed)
        record.archive(LOGIN)
        output = io.StringIO()
        with (
            patch.object(restart, "named_claude_account", return_value=self.account),
            redirect_stdout(output),
        ):
            self.assertEqual(restart.up(LOGIN), 0)
        self.assertIn("no shutdown of claude 2 to restart", output.getvalue())

    def test_claim_returns_named_results_for_a_record_and_no_record(self) -> None:
        claim = cast(
            Callable[
                [str], restart.RestartClaimed | restart.NothingToRestart
            ],
            cast(object, getattr(restart, "_claim")),
        )
        self.assertEqual(claim(LOGIN), restart.NothingToRestart())

        saved = shutdown_record([entry(top_level("one"))])
        record.create(saved)

        claimed = claim(LOGIN)

        self.assertIsInstance(claimed, restart.RestartClaimed)
        if not isinstance(claimed, restart.RestartClaimed):
            self.fail("expected the restart record to be claimed")
        self.assertEqual(claimed.record["state"], "restarting")

    def test_claim_returns_nothing_when_record_disappears_before_update(
        self,
    ) -> None:
        record.create(shutdown_record([entry(top_level("one"))]))
        claim = cast(
            Callable[
                [str], restart.RestartClaimed | restart.NothingToRestart
            ],
            cast(object, getattr(restart, "_claim")),
        )

        with patch.object(
            restart,
            "update",
            side_effect=record.NoLiveRecord("record was archived"),
        ):
            claimed = claim(LOGIN)

        self.assertEqual(claimed, restart.NothingToRestart())

    def test_record_archived_between_lookup_and_claim_is_a_successful_no_op(self) -> None:
        record.create(shutdown_record([entry(top_level("one"))]))
        original_update = record.update

        def archive_before_update(
            login: str,
            change: Callable[[ShutdownRecord], None],
        ) -> ShutdownRecord:
            del change
            def complete(current: ShutdownRecord) -> None:
                current["state"] = "up"

            _ = original_update(login, complete)
            record.archive(login)
            raise record.NoLiveRecord(f"no live shutdown for {login}")

        output = io.StringIO()
        with (
            patch.object(restart, "update", archive_before_update),
            patch.object(settle, "local_machine", return_value="natedev"),
            redirect_stdout(output),
        ):
            self.assertEqual(restart.up(LOGIN), 0)

        self.assertIn(
            f"natedev: no shutdown of {LABEL} to restart", output.getvalue()
        )

    def test_dry_run_of_a_restarting_record_reports_already_running(self) -> None:
        saved = shutdown_record(
            [entry(top_level("one"))], state="restarting"
        )
        record.create(saved)
        output = io.StringIO()

        with redirect_stdout(output):
            self.assertEqual(restart.up(LOGIN, dry_run=True), 0)

        self.assertIn(
            f"restart of {LABEL} already running", output.getvalue()
        )
        self.assertEqual(self.stored_record(), saved)

    def test_settling_and_stopping_records_are_unchanged_and_refused(self) -> None:
        saved = shutdown_record([entry(top_level("one"))], state="settling")
        record.create(saved)
        states: tuple[ShutdownState, ShutdownState] = ("settling", "stopping")
        for state in states:
            with self.subTest(state=state):
                if self.stored_record()["state"] != state:
                    def change(current: ShutdownRecord) -> None:
                        current["state"] = cast(ShutdownState, state)

                    _ = record.update(LOGIN, change)
                before = self.stored_record()
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(restart.up(LOGIN), 1)
                self.assertIn(f"shutdown of {LABEL} is {state}; nothing restarted", output.getvalue())
                self.assertEqual(self.stored_record(), before)

    def test_every_stop_partial_progress_is_resumed_whether_live_or_stopped(self) -> None:
        kinds: tuple[
            Literal[
                "waiting",
                "ready",
                "passive seat ready",
                "stopped",
                "already gone",
                "process identity lost",
                "stop failed",
            ],
            ...,
        ] = (
            "waiting",
            "ready",
            "passive seat ready",
            "stopped",
            "already gone",
            "process identity lost",
            "stop failed",
        )
        live_ids = frozenset(f"live-{index}" for index in range(len(kinds)))
        entries: list[ShutdownSessionEntry] = []
        for index, kind in enumerate(kinds):
            live_entry = entry(
                top_level(
                    f"live-{index}",
                    host=inventory.TerminalHost(kind="terminal"),
                ),
                state=progress(kind),
            )
            live_entry["stop_issues"] = [record.StillRunningAfterStop(
                kind="still running",
                at=STOPPED,
                reason=f"persist {kind}",
            )]
            entries.append(live_entry)
            entries.append(entry(
                top_level(
                    f"stopped-{index}",
                    host=inventory.TerminalHost(kind="terminal"),
                ),
                state=progress(kind),
            ))
        saved = shutdown_record(entries, state="stop partial")
        saved["stop_issues"] = [record.MachineStopFailed(
            kind="machine stop failed",
            at=STOPPED,
            machine="mac",
            reason="persist orchestration issue",
        )]
        record.create(saved)
        commands = CommandRecorder()
        messages: list[tuple[str, str]] = []

        def send_message(
            recipient: str,
            summary: str,
            text: str,
            *,
            need: Literal["note", "decision", "blocked"] = "note",
            machine: str = "",
            key: str = "",
        ) -> settle.MessageDelivery:
            del summary, need, machine, key
            messages.append((recipient, text))
            return settle.MessageSent(kind="sent")

        with (
            patch.object(restart, "_run", commands),
            patch.object(restart, "_live_session_ids", return_value=live_ids),
            patch.object(restart, "wait_for_session", return_value=True),
            patch.object(settle, "send_message", send_message),
            patch.object(settle, "record_time", return_value=RESTARTED),
            patch.object(restart, "archive"),
        ):
            self.assertEqual(restart.up(LOGIN), 0)

        current = self.stored_record()
        self.assertEqual(current["state"], "up")
        self.assertTrue(all(item["progress"]["kind"] == "restarted" for item in current["entries"]))
        self.assertEqual(current["stop_issues"], saved["stop_issues"])
        for before, after in zip(saved["entries"], current["entries"], strict=True):
            self.assertEqual(after["stop_issues"], before["stop_issues"])
        retire_calls = [argv for argv in commands.calls if "retire" in argv]
        launches = [argv for argv in commands.calls if argv[:1] == ["open"]]
        self.assertEqual(len(retire_calls), 14)
        self.assertEqual(len(launches), 7)
        self.assertEqual(len(messages), 7)
        first_stopped_retire = next(
            index for index, argv in enumerate(commands.calls)
            if "session:stopped-0" in argv
        )
        first_stopped_launch = next(
            index for index, argv in enumerate(commands.calls)
            if argv[:1] == ["open"] and "--resume stopped-0" in argv[-1]
        )
        self.assertLess(first_stopped_retire, first_stopped_launch)

    def test_units_finish_their_live_wait_before_showrunners_and_requester_is_last(self) -> None:
        entries = [
            entry(top_level("requester", host=inventory.TerminalHost(kind="terminal"))),
            entry(showrunner("showrunner")),
            entry(top_level("top", host=inventory.TerminalHost(kind="terminal"))),
            entry(unit("unit")),
        ]
        record.create(shutdown_record(entries, requester="requester"))
        events: list[str] = []
        live: set[str] = set()

        def launch(
            current: ShutdownRecord,
            current_entry: ShutdownSessionEntry,
            note: str,
            desktop_state: restart.DesktopLaunchState,
            *,
            dry_run: bool,
        ) -> restart.SessionLaunched | restart.ManualRestartRequired:
            del current, note, desktop_state, dry_run
            session_id = current_entry["session"]["session_id"]
            if session_id == "showrunner":
                self.assertIn("unit", live)
            events.append(f"launch:{session_id}")
            return restart.SessionLaunched()

        def wait(session_id: str, timeout: float) -> bool:
            self.assertEqual(timeout, 90.0)
            events.append(f"wait:{session_id}")
            live.add(session_id)
            return True

        def send_message(
            recipient: str,
            summary: str,
            text: str,
            *,
            need: Literal["note", "decision", "blocked"] = "note",
            machine: str = "",
            key: str = "",
        ) -> settle.MessageDelivery:
            del recipient, summary, text, need, machine, key
            return settle.MessageSent(kind="sent")

        with (
            patch.object(restart, "_retire"),
            patch.object(restart, "_launch_session", launch),
            patch.object(restart, "_live_session_ids", side_effect=lambda: frozenset(live)),
            patch.object(restart, "wait_for_session", wait),
            patch.object(settle, "send_message", send_message),
            patch.object(settle, "record_time", return_value=RESTARTED),
        ):
            self.assertEqual(restart.up(LOGIN), 0)

        self.assertEqual(events, [
            "launch:unit", "wait:unit",
            "launch:showrunner", "wait:showrunner",
            "launch:top", "wait:top",
            "launch:requester", "wait:requester",
        ])

    def test_tmux_respawns_a_reusable_live_or_dead_pane(self) -> None:
        cases = [
            ("live", "kept\t700\t0\n"),
            ("dead", "kept\t\t1\n"),
        ]
        for label, pane_status in cases:
            with self.subTest(label=label):
                commands = CommandRecorder()
                probes: list[list[str]] = []
                restored = top_level(
                    label,
                    cwd="/tmp/restored",
                    host=recorded_tmux_host("kept", f"%{label}", 700),
                )
                record.create(
                    shutdown_record([entry(restored)], machine="Mac")
                )

                def probe(
                    argv: list[str], *, capture_output: bool, text: bool, check: bool
                ) -> subprocess.CompletedProcess[str]:
                    del capture_output, text, check
                    probes.append(argv)
                    if argv[:2] == ["tmux", "display"]:
                        return subprocess.CompletedProcess(argv, 0, pane_status, "")
                    if argv[:1] == ["pgrep"]:
                        return subprocess.CompletedProcess(argv, 1, "", "")
                    return subprocess.CompletedProcess(argv, 0, "", "")

                with (
                    patch.object(restart, "_run", commands),
                    patch.object(
                        restart, "_live_session_ids", return_value=frozenset()
                    ),
                    patch.object(restart, "wait_for_session", return_value=True),
                    patch("subprocess.run", probe),
                    patch.object(settle, "record_time", return_value=RESTARTED),
                ):
                    self.assertEqual(restart.up(LOGIN), 0)

                launches = [
                    argv
                    for argv in commands.calls
                    if argv[:2] == ["tmux", "respawn-pane"]
                ]
                self.assertEqual(len(launches), 1)
                self.assertEqual(
                    launches[0][:-1],
                    [
                        "tmux",
                        "respawn-pane",
                        "-k",
                        "-t",
                        f"%{label}",
                        "-c",
                        "/tmp/restored",
                        "zsh",
                        "-ic",
                    ],
                )
                self.assertIn(f"--resume {label}", launches[0][-1])
                self.assertNotIn(
                    ["tmux", "has-session", "-t", "=kept"], probes
                )
                if label == "dead":
                    self.assertFalse(any(argv[:1] == ["pgrep"] for argv in probes))

    def test_tmux_respawns_recorded_pane_when_session_name_has_outer_spaces(self) -> None:
        commands = CommandRecorder()
        restored = top_level(
            "spaced",
            host=recorded_tmux_host(" work ", "%8", 700),
        )
        record.create(shutdown_record([entry(restored)], machine="Mac"))

        def probe(
            argv: list[str], *, capture_output: bool, text: bool, check: bool
        ) -> subprocess.CompletedProcess[str]:
            del capture_output, text, check
            if argv[:2] == ["tmux", "display"]:
                return subprocess.CompletedProcess(argv, 0, " work \t700\t0\n", "")
            if argv[:1] == ["pgrep"]:
                return subprocess.CompletedProcess(argv, 1, "", "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        with (
            patch.object(restart, "_run", commands),
            patch.object(restart, "_live_session_ids", return_value=frozenset()),
            patch.object(restart, "wait_for_session", return_value=True),
            patch("subprocess.run", probe),
            patch.object(settle, "record_time", return_value=RESTARTED),
        ):
            self.assertEqual(restart.up(LOGIN), 0)

        self.assertEqual(
            len(
                [
                    argv
                    for argv in commands.calls
                    if argv[:2] == ["tmux", "respawn-pane"]
                ]
            ),
            1,
        )

    def test_tmux_falls_back_when_a_recorded_pane_is_in_use_or_gone(self) -> None:
        recorded = inventory.TmuxPane(kind="pane", pane_id="%7", pane_pid=700)
        fallbacks: list[
            tuple[
                str,
                inventory.TmuxPane | inventory.PaneNotRecorded,
                int,
                str,
                int,
            ]
        ] = [
            ("child process", recorded, 0, "kept\t700\t0\n", 0),
            ("different pane pid", recorded, 0, "kept\t701\t0\n", 1),
            ("another session", recorded, 0, "moved\t700\t0\n", 1),
            ("missing pane", recorded, 1, "", 1),
            (
                "pane not recorded",
                inventory.PaneNotRecorded(kind="not recorded"),
                1,
                "",
                1,
            ),
        ]
        for label, pane, display_status, display_output, pgrep_status in fallbacks:
            for session_survives in (True, False):
                with self.subTest(label=label, session_survives=session_survives):
                    commands = CommandRecorder()
                    probes: list[list[str]] = []
                    restored = top_level(
                        "fallback",
                        cwd="/tmp/fallback",
                        host=inventory.TmuxHost(
                            kind="tmux", tmux_session="kept", pane=pane
                        ),
                    )
                    record.create(
                        shutdown_record([entry(restored)], machine="Mac")
                    )

                    def probe(
                        argv: list[str],
                        *,
                        capture_output: bool,
                        text: bool,
                        check: bool,
                    ) -> subprocess.CompletedProcess[str]:
                        del capture_output, text, check
                        probes.append(argv)
                        if argv[:2] == ["tmux", "display"]:
                            return subprocess.CompletedProcess(
                                argv, display_status, display_output, ""
                            )
                        if argv[:1] == ["pgrep"]:
                            return subprocess.CompletedProcess(
                                argv, pgrep_status, "", ""
                            )
                        return subprocess.CompletedProcess(
                            argv, 0 if session_survives else 1, "", ""
                        )

                    with (
                        patch.object(restart, "_run", commands),
                        patch.object(
                            restart,
                            "_live_session_ids",
                            return_value=frozenset(),
                        ),
                        patch.object(
                            restart, "wait_for_session", return_value=True
                        ),
                        patch("subprocess.run", probe),
                        patch.object(
                            settle, "record_time", return_value=RESTARTED
                        ),
                    ):
                        self.assertEqual(restart.up(LOGIN), 0)

                    launch = next(
                        argv
                        for argv in commands.calls
                        if argv[:1] == ["tmux"]
                    )

                    if session_survives:
                        expected_prefix = [
                            "tmux",
                            "new-window",
                            "-t",
                            "=kept:",
                            "-c",
                            "/tmp/fallback",
                            "zsh",
                            "-ic",
                        ]
                    else:
                        expected_prefix = [
                            "tmux",
                            "new-session",
                            "-d",
                            "-s",
                            "kept",
                            "-c",
                            "/tmp/fallback",
                            "zsh",
                            "-ic",
                        ]
                    self.assertEqual(launch[:-1], expected_prefix)
                    self.assertIn("--resume fallback", launch[-1])
                    pane_probed = any(
                        argv[:2] == ["tmux", "display"] for argv in probes
                    )
                    self.assertEqual(pane_probed, pane["kind"] == "pane")

    def test_an_already_live_session_gets_its_note_timer_and_footer_without_a_launch(self) -> None:
        timers = [
            TimerRestore(
                instance="showrunner-demo",
                was_enabled=True,
                footer=record.ShowrunnerFooter(kind="footer", slug="demo"),
            ),
            TimerRestore(
                instance="disabled-before-stop",
                was_enabled=False,
                footer=record.ShowrunnerFooter(kind="footer", slug="other"),
            ),
        ]
        restored = entry(
            top_level("live", host=inventory.TerminalHost(kind="terminal")),
            where=record.WhereNotSaid(kind="not said"),
            timers=timers,
        )
        record.create(shutdown_record([restored], state="stop partial"))
        commands = CommandRecorder()
        notes: list[str] = []
        started: list[str] = []
        footers: list[str] = []

        def send_message(
            recipient: str,
            summary: str,
            text: str,
            *,
            need: Literal["note", "decision", "blocked"] = "note",
            machine: str = "",
            key: str = "",
        ) -> settle.MessageDelivery:
            del recipient, summary, need, machine, key
            notes.append(text)
            return settle.MessageSent(kind="sent")

        def start_timer(verb: str, instance: str) -> None:
            self.assertEqual(verb, "start")
            started.append(instance)

        def footer(slug: str, state: object) -> None:
            del state
            footers.append(slug)

        with (
            patch.object(restart, "_run", commands),
            patch.object(restart, "_live_session_ids", return_value=frozenset({"live"})),
            patch.object(settle, "send_message", send_message),
            patch.object(settle, "run_notifier", start_timer),
            patch.object(showrunner_footer, "set_footer_state", footer),
            patch.object(settle, "record_time", return_value=RESTARTED),
            patch.object(restart, "archive"),
        ):
            self.assertEqual(restart.up(LOGIN), 0)

        self.assertEqual(len(commands.calls), 1)
        self.assertIn("retire", commands.calls[0])
        self.assertEqual(started, ["showrunner-demo"])
        self.assertEqual(footers, ["demo", "other"])
        self.assertEqual(len(notes), 1)
        self.assertIn("It left no note of where it was", notes[0])
        current = self.stored_record()
        self.assertEqual(current["entries"][0]["progress"]["kind"], "restarted")

    def test_session_becoming_live_while_messages_retire_is_not_launched(
        self,
    ) -> None:
        timer = TimerRestore(
            instance="arriving-timer",
            was_enabled=True,
            footer=record.NoFooter(kind="no footer"),
        )
        restored = entry(
            top_level(
                "arriving", host=inventory.TerminalHost(kind="terminal")
            ),
            timers=[timer],
        )
        saved = shutdown_record([restored])
        record.create(saved)
        session_is_live = False
        notes: list[str] = []
        started: list[str] = []

        def retire(
            current: ShutdownRecord,
            current_entry: ShutdownSessionEntry,
            *,
            dry_run: bool,
        ) -> None:
            nonlocal session_is_live
            del current, current_entry
            self.assertFalse(dry_run)
            session_is_live = True

        def live_session_ids() -> frozenset[str]:
            return frozenset({"arriving"}) if session_is_live else frozenset()

        def send_message(
            recipient: str,
            summary: str,
            text: str,
            *,
            need: Literal["note", "decision", "blocked"] = "note",
            machine: str = "",
            key: str = "",
        ) -> settle.MessageDelivery:
            del recipient, summary, need, machine, key
            notes.append(text)
            return settle.MessageSent(kind="sent")

        def start_timer(verb: str, instance: str) -> None:
            self.assertEqual(verb, "start")
            started.append(instance)

        with (
            patch.object(restart, "_retire", retire),
            patch.object(restart, "_live_session_ids", live_session_ids),
            patch.object(restart, "_launch_session") as launch,
            patch.object(restart, "wait_for_session") as wait,
            patch.object(settle, "send_message", send_message),
            patch.object(settle, "run_notifier", start_timer),
            patch.object(settle, "record_time", return_value=RESTARTED),
            patch.object(restart, "archive"),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(restart.up(LOGIN), 0)

        launch.assert_not_called()
        wait.assert_not_called()
        self.assertEqual(
            notes, [restart.restart_note(saved, restored, RESTARTED)]
        )
        self.assertEqual(started, ["arriving-timer"])
        self.assertEqual(
            self.stored_record()["entries"][0]["progress"]["kind"],
            "restarted",
        )

    def test_a_queued_live_restart_note_is_retried_before_timers_resume(self) -> None:
        timer = TimerRestore(
            instance="showrunner-demo",
            was_enabled=True,
            footer=record.NoFooter(kind="no footer"),
        )
        record.create(shutdown_record([
            entry(
                top_level("live", host=inventory.TerminalHost(kind="terminal")),
                timers=[timer],
            )
        ]))
        deliveries: list[settle.MessageDelivery] = [
            settle.MessageQueued(kind="queued", reason="relay unavailable\nmore"),
            settle.MessageSent(kind="sent"),
        ]
        starts: list[str] = []

        def send_message(
            recipient: str,
            summary: str,
            text: str,
            *,
            need: Literal["note", "decision", "blocked"] = "note",
            machine: str = "",
            key: str = "",
        ) -> settle.MessageDelivery:
            del recipient, summary, text, need, machine, key
            return deliveries.pop(0)

        def start_timer(verb: str, instance: str) -> None:
            self.assertEqual(verb, "start")
            starts.append(instance)

        with (
            patch.object(restart, "_retire"),
            patch.object(restart, "_live_session_ids", return_value=frozenset({"live"})),
            patch.object(settle, "send_message", send_message),
            patch.object(settle, "run_notifier", start_timer),
            patch.object(settle, "record_time", return_value=RESTARTED),
            patch.object(restart, "archive"),
        ):
            self.assertEqual(restart.up(LOGIN), 1)
            current = self.stored_record()
            self.assertEqual(current["state"], "restart partial")
            self.assertEqual(current["entries"][0]["progress"], {
                "kind": "restart failed",
                "at": RESTARTED,
                "reason": "restart note not delivered: relay unavailable",
            })
            self.assertEqual(starts, [])

            self.assertEqual(restart.up(LOGIN), 0)

        current = self.stored_record()
        self.assertEqual(current["state"], "up")
        self.assertEqual(current["entries"][0]["progress"]["kind"], "restarted")
        self.assertEqual(starts, ["showrunner-demo"])

    def test_a_queued_showrunner_note_leaves_its_timers_stopped(self) -> None:
        timer = TimerRestore(
            instance="showrunner-demo",
            was_enabled=True,
            footer=record.NoFooter(kind="no footer"),
        )
        record.create(shutdown_record([entry(showrunner("show"), timers=[timer])]))
        starts: list[str] = []

        def queued(
            recipient: str,
            summary: str,
            text: str,
            *,
            need: Literal["note", "decision", "blocked"] = "note",
            machine: str = "",
            key: str = "",
        ) -> settle.MessageDelivery:
            del recipient, summary, text, need, machine, key
            return settle.MessageQueued(kind="queued", reason="session socket absent")

        def start_timer(verb: str, instance: str) -> None:
            del verb
            starts.append(instance)

        with (
            patch.object(restart, "_retire"),
            patch.object(
                restart, "_launch_session", return_value=restart.SessionLaunched()
            ),
            patch.object(restart, "_live_session_ids", return_value=frozenset()),
            patch.object(restart, "wait_for_session", return_value=True),
            patch.object(settle, "send_message", queued),
            patch.object(
                settle,
                "run_notifier",
                side_effect=start_timer,
            ),
            patch.object(settle, "record_time", return_value=RESTARTED),
        ):
            self.assertEqual(restart.up(LOGIN), 1)

        current = self.stored_record()
        failed = current["entries"][0]["progress"]
        self.assertEqual(failed["kind"], "restart failed")
        if failed["kind"] != "restart failed":
            self.fail("expected restart failure")
        self.assertEqual(
            failed["reason"],
            "restart note not delivered: session socket absent",
        )
        self.assertEqual(starts, [])

    def test_a_failed_retire_marks_only_that_entry_and_later_entries_launch(self) -> None:
        record.create(shutdown_record([
            entry(top_level("first", host=inventory.TerminalHost(kind="terminal"))),
            entry(top_level("second", host=inventory.TerminalHost(kind="terminal"))),
        ]))
        launched: list[str] = []

        def retire(
            current: ShutdownRecord,
            current_entry: ShutdownSessionEntry,
            *,
            dry_run: bool,
        ) -> None:
            del current, dry_run
            if current_entry["session"]["session_id"] == "first":
                raise subprocess.CalledProcessError(
                    3, ["send", "retire"], stderr="queue locked\nmore"
                )

        def launch(
            current: ShutdownRecord,
            current_entry: ShutdownSessionEntry,
            note: str,
            desktop_state: restart.DesktopLaunchState,
            *,
            dry_run: bool,
        ) -> restart.SessionLaunched | restart.ManualRestartRequired:
            del current, note, desktop_state, dry_run
            launched.append(current_entry["session"]["session_id"])
            return restart.SessionLaunched()

        with (
            patch.object(restart, "_retire", retire),
            patch.object(restart, "_launch_session", launch),
            patch.object(restart, "_live_session_ids", return_value=frozenset()),
            patch.object(restart, "wait_for_session", return_value=True),
            patch.object(settle, "record_time", return_value=RESTARTED),
        ):
            self.assertEqual(restart.up(LOGIN), 1)

        by_id = {
            item["session"]["session_id"]: item for item in self.stored_record()["entries"]
        }
        first_progress = by_id["first"]["progress"]
        self.assertEqual(first_progress["kind"], "restart failed")
        if first_progress["kind"] != "restart failed":
            self.fail("expected restart failure")
        self.assertEqual(
            first_progress["reason"],
            "queued shutdown messages not retired: queue locked",
        )
        self.assertEqual(by_id["second"]["progress"]["kind"], "restarted")
        self.assertEqual(launched, ["second"])

    def test_launcher_failure_uses_the_first_stderr_line(self) -> None:
        record.create(shutdown_record([
            entry(top_level("failed", host=inventory.TerminalHost(kind="terminal")))
        ]))

        with (
            patch.object(restart, "_retire"),
            patch.object(restart, "_live_session_ids", return_value=frozenset()),
            patch.object(
                restart,
                "_launch_session",
                side_effect=subprocess.CalledProcessError(
                    9, ["open"], stderr="launcher refused\ndetail"
                ),
            ),
            patch.object(settle, "record_time", return_value=RESTARTED),
        ):
            self.assertEqual(restart.up(LOGIN), 1)

        failed = self.stored_record()["entries"][0]["progress"]
        self.assertEqual(failed["kind"], "restart failed")
        if failed["kind"] != "restart failed":
            self.fail("expected restart failure")
        self.assertEqual(failed["reason"], "launcher refused")

    def test_restart_note_recording_failure_prevents_the_launch(self) -> None:
        record.create(shutdown_record([
            entry(
                top_level(
                    "unrecorded",
                    host=inventory.TerminalHost(kind="terminal"),
                )
            )
        ]))
        commands = CommandRecorder()

        with (
            patch.object(restart, "_retire"),
            patch.object(restart, "_run", commands),
            patch.object(restart, "_live_session_ids", return_value=frozenset()),
            patch.object(
                conversation_pause,
                "record_scheduled_prompts",
                side_effect=OSError("prompt file unavailable\nmore detail"),
            ),
            patch.object(settle, "record_time", return_value=RESTARTED),
        ):
            self.assertEqual(restart.up(LOGIN), 1)

        self.assertEqual(commands.calls, [])
        failed = self.stored_record()["entries"][0]["progress"]
        self.assertEqual(failed["kind"], "restart failed")
        if failed["kind"] != "restart failed":
            self.fail("expected restart failure")
        self.assertEqual(
            failed["reason"],
            "restart prompt not recorded: prompt file unavailable",
        )

    def test_a_session_that_does_not_return_is_the_only_one_retried(self) -> None:
        timers = [TimerRestore(
            instance="timer",
            was_enabled=True,
            footer=record.NoFooter(kind="no footer"),
        )]
        entries = [
            entry(top_level("good", host=inventory.TerminalHost(kind="terminal")), timers=timers),
            entry(top_level("missing", host=inventory.TerminalHost(kind="terminal")), timers=timers),
        ]
        record.create(shutdown_record(entries))
        launches: list[str] = []
        starts: list[str] = []
        missing_returns = False

        def launch(
            current: ShutdownRecord,
            current_entry: ShutdownSessionEntry,
            note: str,
            desktop_state: restart.DesktopLaunchState,
            *,
            dry_run: bool,
        ) -> restart.SessionLaunched | restart.ManualRestartRequired:
            del current, note, desktop_state, dry_run
            launches.append(current_entry["session"]["session_id"])
            return restart.SessionLaunched()

        def wait(session_id: str, timeout: float) -> bool:
            del timeout
            return session_id == "good" or missing_returns

        def start_timer(verb: str, instance: str) -> None:
            self.assertEqual(verb, "start")
            starts.append(instance)

        with (
            patch.object(restart, "_retire"),
            patch.object(restart, "_launch_session", launch),
            patch.object(restart, "_live_session_ids", return_value=frozenset()),
            patch.object(restart, "wait_for_session", wait),
            patch.object(settle, "run_notifier", start_timer),
            patch.object(settle, "record_time", return_value=RESTARTED),
        ):
            self.assertEqual(restart.up(LOGIN), 1)
            current = self.stored_record()
            by_id = {item["session"]["session_id"]: item for item in current["entries"]}
            self.assertEqual(by_id["good"]["progress"]["kind"], "restarted")
            self.assertEqual(by_id["missing"]["progress"]["kind"], "restart failed")
            self.assertEqual(starts, ["timer"])
            self.assertCountEqual(launches, ["good", "missing"])

            launches.clear()
            missing_returns = True
            self.assertEqual(restart.up(LOGIN), 0)

        self.assertEqual(launches, ["missing"])
        self.assertEqual(starts, ["timer", "timer"])
        self.assertEqual(record.find_live(LOGIN)["kind"], "no shutdown")

    def test_manual_unit_holds_its_timer_and_seat_until_the_session_appears(self) -> None:
        timer = TimerRestore(
            instance="showrunner-demo",
            was_enabled=True,
            footer=record.ShowrunnerFooter(kind="footer", slug="demo"),
        )
        unit_entry = entry(
            unit(
                "manual-unit",
                plan=inventory.NoRunRecord(kind="no run record"),
                run_dirs=[str(self.root / "unit-run")],
            ),
            timers=[timer],
        )
        seat_entry = entry(seat(
            "seat",
            owner="manual-unit",
            run_dirs=[str(self.root / "seat-run")],
        ))
        record.create(shutdown_record([unit_entry, seat_entry]))
        commands = CommandRecorder()
        starts: list[str] = []
        footers: list[str] = []
        notes: list[str] = []

        def start_timer(verb: str, instance: str) -> None:
            self.assertEqual(verb, "start")
            starts.append(instance)

        def footer(slug: str, state: object) -> None:
            del state
            footers.append(slug)

        with (
            patch.object(restart, "_run", commands),
            patch.object(restart, "_live_session_ids", return_value=frozenset()),
            patch.object(settle, "record_time", return_value=RESTARTED),
            patch.object(settle, "run_notifier", start_timer),
            patch.object(showrunner_footer, "set_footer_state", footer),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(restart.up(LOGIN), 1)
        current = self.stored_record()
        self.assertEqual(current["state"], "restart partial")
        self.assertEqual(current["entries"][0]["progress"]["kind"], "manual restart")
        self.assertEqual(current["entries"][1]["progress"]["kind"], "seat available on demand")
        self.assertEqual(starts, [])
        self.assertCountEqual(shutdown.held_session_ids(), ["manual-unit", "seat"])

        def send_message(
            recipient: str,
            summary: str,
            text: str,
            *,
            need: Literal["note", "decision", "blocked"] = "note",
            machine: str = "",
            key: str = "",
        ) -> settle.MessageDelivery:
            del recipient, summary, need, machine, key
            notes.append(text)
            return settle.MessageSent(kind="sent")

        with (
            patch.object(restart, "_run", commands),
            patch.object(restart, "_live_session_ids", return_value=frozenset({"manual-unit"})),
            patch.object(settle, "send_message", send_message),
            patch.object(settle, "run_notifier", start_timer),
            patch.object(showrunner_footer, "set_footer_state", footer),
            patch.object(settle, "record_time", return_value=RESTARTED),
        ):
            self.assertEqual(restart.up(LOGIN), 0)

        self.assertEqual(starts, ["showrunner-demo"])
        self.assertEqual(footers, ["demo"])
        self.assertEqual(notes, [])
        self.assertEqual(shutdown.held_session_ids(), [])

    def test_output_and_alert_name_manual_failed_and_timer_work(self) -> None:
        timer = TimerRestore(
            instance="timer-one",
            was_enabled=True,
            footer=record.NoFooter(kind="no footer"),
        )
        record.create(shutdown_record([
            entry(top_level("manual")),
            entry(top_level("missing", host=inventory.TerminalHost(kind="terminal"))),
            entry(
                top_level("live", host=inventory.TerminalHost(kind="terminal")),
                timers=[timer],
            ),
            entry(top_level("later", host=inventory.TerminalHost(kind="terminal"))),
        ]))
        alerts: list[tuple[str, str]] = []

        def alert_user(
            scope: record.ShutdownScope,
            summary: str,
            text: str,
            *,
            machine: str = "",
        ) -> None:
            del machine
            self.assertEqual(scope["kind"], "all account sessions")
            alerts.append((summary, text))

        def start_timer(verb: str, instance: str) -> None:
            del verb
            if instance == "timer-one":
                raise RuntimeError("notifier refused")

        output = io.StringIO()
        with (
            patch.object(restart, "named_claude_account", return_value=self.account),
            patch.object(restart, "_run", CommandRecorder()),
            patch.object(
                restart,
                "_live_session_ids",
                return_value=frozenset({"live", "later"}),
            ),
            patch.object(restart, "wait_for_session", return_value=False),
            patch.object(restart, "run_remote", return_value=(0, "Mac: no shutdown of claude 2 to restart")),
            patch.object(
                settle,
                "send_message",
                return_value=settle.MessageSent(kind="sent"),
            ),
            patch.object(settle, "alert_user", alert_user),
            patch.object(settle, "run_notifier", start_timer),
            patch.object(settle, "record_time", return_value=RESTARTED),
            redirect_stdout(output),
        ):
            self.assertEqual(
                restart.restart(
                    restart.AccountNamed(LABEL),
                    dry_run=False,
                    alerts=restart.AlertWhenNotBack(),
                ),
                1,
            )

        text = output.getvalue()
        self.assertIn("natedev: manual restart manual:", text)
        self.assertIn(
            "natedev: missing restart failed: session not live after 90 seconds",
            text,
        )
        timer_line = "natedev: timer timer-one of live not started: notifier refused"
        self.assertIn(timer_line, text)
        self.assertEqual(alerts[0][0], f"{LABEL} is not fully back")
        self.assertIn("manual restart manual:", alerts[0][1])
        self.assertIn("missing restart failed:", alerts[0][1])
        self.assertIn(timer_line, alerts[0][1])
        self.assertTrue(
            alerts[0][1].endswith(
                "Fix what is named above, then run /shutdown restart again; "
                + "it brings back only what is left."
            )
        )
        current = self.stored_record()
        self.assertEqual(current["state"], "restart partial")
        self.assertEqual(
            current["entries"][2]["progress"],
            record.SessionRestoredTimersPending(
                kind="timers pending",
                at=RESTARTED,
                timers=[
                    record.PendingTimer(
                        instance="timer-one", reason="notifier refused"
                    )
                ],
            ),
        )
        self.assertEqual(
            current["entries"][3]["progress"]["kind"], "restarted"
        )

    def test_pending_timer_counts_live_session_then_retries_only_that_timer(
        self,
    ) -> None:
        timers = [
            TimerRestore(
                instance="pending-timer",
                was_enabled=True,
                footer=record.ShowrunnerFooter(kind="footer", slug="pending"),
            ),
            TimerRestore(
                instance="started-timer",
                was_enabled=True,
                footer=record.ShowrunnerFooter(kind="footer", slug="started"),
            ),
        ]
        record.create(
            shutdown_record(
                [
                    entry(
                        top_level(
                            "live", host=inventory.TerminalHost(kind="terminal")
                        ),
                        timers=timers,
                    )
                ]
            )
        )
        notes: list[str] = []
        starts: list[str] = []
        footers: list[str] = []
        output = io.StringIO()

        def send_message(
            recipient: str,
            summary: str,
            text: str,
            *,
            need: Literal["note", "decision", "blocked"] = "note",
            machine: str = "",
            key: str = "",
        ) -> settle.MessageDelivery:
            del recipient, summary, need, machine, key
            notes.append(text)
            return settle.MessageSent(kind="sent")

        def start_timer(verb: str, instance: str) -> None:
            self.assertEqual(verb, "start")
            starts.append(instance)
            if instance == "pending-timer" and starts.count(instance) == 1:
                raise RuntimeError("notifier unavailable")

        def footer(slug: str, state: object) -> None:
            del state
            footers.append(slug)

        with (
            patch.object(restart, "_retire") as retire,
            patch.object(restart, "_launch_session") as launch,
            patch.object(
                restart, "_session_liveness", return_value=restart.SessionLive()
            ),
            patch.object(
                restart, "_live_session_ids", return_value=frozenset({"live"})
            ),
            patch.object(settle, "send_message", send_message),
            patch.object(settle, "run_notifier", start_timer),
            patch.object(showrunner_footer, "set_footer_state", footer),
            patch.object(settle, "record_time", return_value=RESTARTED),
            redirect_stdout(output),
        ):
            self.assertEqual(restart.up(LOGIN), 1)
            current = self.stored_record()
            self.assertEqual(current["state"], "restart partial")
            self.assertEqual(
                current["entries"][0]["progress"],
                record.SessionRestoredTimersPending(
                    kind="timers pending",
                    at=RESTARTED,
                    timers=[
                        record.PendingTimer(
                            instance="pending-timer",
                            reason="notifier unavailable",
                        )
                    ],
                ),
            )
            self.assertEqual(
                output.getvalue().splitlines(),
                [
                    "natedev: 1 restarted, 0 seats available on demand",
                    "natedev: timer pending-timer of live not started: notifier unavailable",
                ],
            )

            self.assertEqual(restart.up(LOGIN), 0)

        launch.assert_not_called()
        self.assertEqual(retire.call_count, 1)
        self.assertEqual(len(notes), 1)
        self.assertEqual(
            starts, ["pending-timer", "started-timer", "pending-timer"]
        )
        self.assertEqual(footers, ["pending", "started", "pending"])
        self.assertEqual(record.find_live(LOGIN)["kind"], "no shutdown")
        history = (
            self.state_root
            / LOGIN.casefold()
            / "history"
            / f"{STOPPED}.json"
        )
        archived = record.parse_records(f"[{history.read_text(encoding='utf-8')}]")[
            0
        ]
        self.assertEqual(archived["state"], "up")
        self.assertEqual(archived["entries"][0]["progress"]["kind"], "restarted")

    def test_unreadable_registry_keeps_pending_session_unchanged(self) -> None:
        timer = TimerRestore(
            instance="pending-timer",
            was_enabled=True,
            footer=record.NoFooter(kind="no footer"),
        )
        pending = record.SessionRestoredTimersPending(
            kind="timers pending",
            at=RESTARTED,
            timers=[
                record.PendingTimer(
                    instance="pending-timer",
                    reason="notifier unavailable",
                )
            ],
        )
        restored = entry(
            top_level(
                "possibly-live",
                host=inventory.TerminalHost(kind="terminal"),
            ),
            state=pending,
            timers=[timer],
        )
        record.create(shutdown_record([restored], state="restart partial"))
        registry = self.root / "sessions"
        _ = registry.write_text("not a directory", encoding="utf-8")

        with (
            patch.object(restart, "_retire") as retire,
            patch.object(restart, "_launch_session") as launch,
            patch.object(restart, "_restore_timers") as restore_timers,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(restart.up(LOGIN), 1)
            self.assertEqual(
                self.stored_record()["entries"][0]["progress"], pending
            )

            registry.unlink()
            registry.mkdir()
            _ = (registry / "unreadable.json").write_text(
                "not json", encoding="utf-8"
            )
            self.assertEqual(restart.up(LOGIN), 1)

        self.assertEqual(self.stored_record()["entries"][0]["progress"], pending)
        retire.assert_not_called()
        launch.assert_not_called()
        restore_timers.assert_not_called()

    def test_closed_session_with_pending_timers_is_fully_restarted(self) -> None:
        timers = [
            TimerRestore(
                instance="pending-timer",
                was_enabled=True,
                footer=record.NoFooter(kind="no footer"),
            ),
            TimerRestore(
                instance="previously-started-timer",
                was_enabled=True,
                footer=record.NoFooter(kind="no footer"),
            ),
        ]
        restored = entry(
            top_level(
                "closed",
                host=inventory.TerminalHost(kind="terminal"),
            ),
            state=record.SessionRestoredTimersPending(
                kind="timers pending",
                at=RESTARTED,
                timers=[
                    record.PendingTimer(
                        instance="pending-timer",
                        reason="notifier unavailable",
                    )
                ],
            ),
            timers=timers,
        )
        already_done = entry(
            top_level("done"),
            state=record.SessionRestarted(kind="restarted", at=RESTARTED),
        )
        saved = shutdown_record(
            [restored, already_done], state="restart partial"
        )
        expected_note = restart.restart_note(saved, restored, RESTARTED)
        record.create(saved)
        commands = CommandRecorder()
        starts: list[str] = []
        pending_after_relaunch = record.SessionRestoredTimersPending(
            kind="timers pending",
            at=RESTARTED,
            timers=[
                record.PendingTimer(
                    instance=timer["instance"],
                    reason="not yet restored after relaunch",
                )
                for timer in timers
            ],
        )

        def assert_relaunch_checkpoint() -> None:
            self.assertEqual(
                self.stored_record()["entries"][0]["progress"],
                pending_after_relaunch,
            )

        def wait_for_session(session_id: str, timeout: float) -> bool:
            self.assertEqual(session_id, "closed")
            self.assertEqual(timeout, restart.SESSION_WAIT_SECONDS)
            assert_relaunch_checkpoint()
            return True

        def start_timer(verb: str, instance: str) -> None:
            self.assertEqual(verb, "start")
            assert_relaunch_checkpoint()
            starts.append(instance)

        with (
            patch.object(restart, "_run", commands),
            patch.object(
                restart,
                "_session_liveness",
                return_value=restart.SessionNotLive(),
            ),
            patch.object(restart, "_live_session_ids", return_value=frozenset()),
            patch.object(restart, "wait_for_session", wait_for_session),
            patch.object(settle, "run_notifier", start_timer),
            patch.object(settle, "record_time", return_value=RESTARTED),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(restart.up(LOGIN), 0)

        self.assertTrue(
            any(
                "retire" in argv and "session:closed" in argv
                for argv in commands.calls
            )
        )
        self.assertEqual(
            conversation_pause.read_scheduled_prompts("closed"),
            (expected_note,),
        )
        self.assertEqual(
            starts, ["pending-timer", "previously-started-timer"]
        )
        self.assertEqual(record.find_live(LOGIN)["kind"], "no shutdown")
        history = (
            self.state_root
            / LOGIN.casefold()
            / "history"
            / f"{STOPPED}.json"
        )
        archived = record.parse_records(
            f"[{history.read_text(encoding='utf-8')}]"
        )[0]
        self.assertEqual(archived["state"], "up")
        self.assertEqual(
            archived["entries"][0]["progress"]["kind"], "restarted"
        )

    def test_closed_session_with_pending_timers_records_launch_failure(
        self,
    ) -> None:
        timer = TimerRestore(
            instance="pending-timer",
            was_enabled=True,
            footer=record.NoFooter(kind="no footer"),
        )
        restored = entry(
            top_level(
                "closed",
                host=inventory.TerminalHost(kind="terminal"),
            ),
            state=record.SessionRestoredTimersPending(
                kind="timers pending",
                at=RESTARTED,
                timers=[
                    record.PendingTimer(
                        instance="pending-timer",
                        reason="notifier unavailable",
                    )
                ],
            ),
            timers=[timer],
        )
        record.create(shutdown_record([restored], state="restart partial"))

        with (
            patch.object(restart, "_retire"),
            patch.object(
                restart,
                "_session_liveness",
                return_value=restart.SessionNotLive(),
            ),
            patch.object(restart, "_live_session_ids", return_value=frozenset()),
            patch.object(
                restart,
                "_launch_session",
                side_effect=subprocess.CalledProcessError(
                    9, ["open"], stderr="launcher refused\ndetail"
                ),
            ),
            patch.object(settle, "run_notifier") as start_timer,
            patch.object(settle, "record_time", return_value=RESTARTED),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(restart.up(LOGIN), 1)

        start_timer.assert_not_called()
        failed = self.stored_record()["entries"][0]["progress"]
        self.assertEqual(failed["kind"], "restart failed")
        if failed["kind"] != "restart failed":
            self.fail("expected restart failure")
        self.assertEqual(failed["reason"], "launcher refused")

    def test_unknown_host_is_manual_and_printed_again(self) -> None:
        record.create(shutdown_record([entry(top_level("mystery"))]))
        first = io.StringIO()
        second = io.StringIO()
        with (
            patch.object(restart, "_run", CommandRecorder()),
            patch.object(restart, "_live_session_ids", return_value=frozenset()),
            patch.object(settle, "record_time", return_value=RESTARTED),
            redirect_stdout(first),
        ):
            self.assertEqual(restart.up(LOGIN), 1)
        with (
            patch.object(restart, "_run", CommandRecorder()),
            patch.object(restart, "_live_session_ids", return_value=frozenset()),
            patch.object(settle, "record_time", return_value=RESTARTED),
            redirect_stdout(second),
        ):
            self.assertEqual(restart.up(LOGIN), 1)
        self.assertIn("manual restart mystery: cd", first.getvalue())
        self.assertIn("manual restart mystery: cd", second.getvalue())

    def test_orphan_seat_is_available_and_owner_failure_is_reconsidered(self) -> None:
        entries = [
            entry(top_level("owner", host=inventory.TerminalHost(kind="terminal"))),
            entry(seat("owned-seat", owner="owner")),
            entry(seat("orphan-seat", owner=None)),
        ]
        record.create(shutdown_record(entries))
        launched: list[str] = []
        owner_returns = False

        def launch(
            current: ShutdownRecord,
            current_entry: ShutdownSessionEntry,
            note: str,
            desktop_state: restart.DesktopLaunchState,
            *,
            dry_run: bool,
        ) -> restart.SessionLaunched | restart.ManualRestartRequired:
            del current, note, desktop_state, dry_run
            launched.append(current_entry["session"]["session_id"])
            return restart.SessionLaunched()

        def wait(session_id: str, timeout: float) -> bool:
            del session_id, timeout
            return owner_returns

        with (
            patch.object(restart, "_retire"),
            patch.object(restart, "_launch_session", launch),
            patch.object(restart, "_live_session_ids", return_value=frozenset()),
            patch.object(restart, "wait_for_session", wait),
            patch.object(settle, "record_time", return_value=RESTARTED),
            patch.object(restart, "archive"),
        ):
            self.assertEqual(restart.up(LOGIN), 1)
            current = self.stored_record()
            by_id = {item["session"]["session_id"]: item for item in current["entries"]}
            self.assertEqual(by_id["owner"]["progress"]["kind"], "restart failed")
            self.assertEqual(by_id["owned-seat"]["progress"], {
                "kind": "restart failed",
                "at": RESTARTED,
                "reason": "owner owner not back",
            })
            self.assertEqual(by_id["orphan-seat"]["progress"]["kind"], "seat available on demand")
            self.assertEqual(launched, ["owner"])

            owner_returns = True
            self.assertEqual(restart.up(LOGIN), 0)

        current = self.stored_record()
        by_id = {item["session"]["session_id"]: item for item in current["entries"]}
        self.assertEqual(by_id["owner"]["progress"]["kind"], "restarted")
        self.assertEqual(by_id["owned-seat"]["progress"]["kind"], "seat available on demand")
        self.assertEqual(by_id["orphan-seat"]["progress"]["kind"], "seat available on demand")
        self.assertEqual(launched, ["owner", "owner"])

    def test_restart_counts_both_machines_done_when_neither_has_a_record(self) -> None:
        remote_calls: list[list[str]] = []

        def run_remote(
            args: list[str],
            stdin: str = "",
            limit: remote_transport.RemoteCallLimit = remote_transport.STANDARD_TIME_LIMIT,
        ) -> tuple[int, str]:
            del stdin, limit
            remote_calls.append(args)
            return 0, "mac: no shutdown of claude 2 to restart"

        output = io.StringIO()
        with (
            patch.object(restart, "named_claude_account", return_value=self.account),
            patch.object(restart, "run_remote", run_remote),
            patch.object(settle, "alert_user") as alert_user,
            redirect_stdout(output),
        ):
            self.assertEqual(
                restart.restart(
                    restart.AccountNamed(LABEL),
                    dry_run=False,
                    alerts=restart.AlertWhenNotBack(),
                ),
                0,
            )

        self.assertEqual(remote_calls, [["up", LOGIN]])
        self.assertIn("no shutdown of claude 2 to restart", output.getvalue())
        alert_user.assert_not_called()

    def test_up_prints_a_machine_prefixed_failure_to_stdout(self) -> None:
        output = io.StringIO()
        errors = io.StringIO()
        with (
            patch.object(restart, "_up", side_effect=RuntimeError("restore broke")),
            patch.object(settle, "local_machine", return_value="Mac"),
            redirect_stdout(output),
            redirect_stderr(errors),
        ):
            self.assertEqual(restart.up(LOGIN), 1)

        self.assertEqual(output.getvalue(), "Mac: restart failed: restore broke\n")
        self.assertEqual(errors.getvalue(), "")

    def test_empty_remote_failure_is_printed_and_included_in_the_alert(self) -> None:
        alerts: list[tuple[str, str]] = []

        def alert_user(
            scope: record.ShutdownScope,
            summary: str,
            text: str,
            *,
            machine: str = "",
        ) -> None:
            del machine
            self.assertEqual(scope["kind"], "all account sessions")
            alerts.append((summary, text))

        output = io.StringIO()
        with (
            patch.object(restart, "named_claude_account", return_value=self.account),
            patch.object(
                restart,
                "_up",
                return_value=restart.MachineRestartResult(
                    "natedev", LABEL, "nothing"
                ),
            ),
            patch.object(restart, "run_remote", return_value=(2, "")),
            patch.object(restart, "other_machine", return_value="Mac"),
            patch.object(settle, "alert_user", alert_user),
            redirect_stdout(output),
        ):
            self.assertEqual(
                restart.restart(
                    restart.AccountNamed(LABEL),
                    dry_run=False,
                    alerts=restart.AlertWhenNotBack(),
                ),
                1,
            )

        failure = "Mac: restart failed (exit 2)"
        self.assertIn(failure, output.getvalue())
        self.assertEqual(alerts[0][0], f"{LABEL} is not fully back")
        self.assertIn(failure, alerts[0][1])

    def test_an_unreachable_other_machine_leaves_its_record_and_local_work_recovers(self) -> None:
        local_entry = entry(
            top_level("local", host=inventory.TerminalHost(kind="terminal"))
        )
        record.create(shutdown_record([local_entry]))
        commands = CommandRecorder()
        alerts: list[tuple[str, str]] = []
        remote_calls: list[list[str]] = []

        def unreachable(
            args: list[str],
            stdin: str = "",
            limit: remote_transport.RemoteCallLimit = remote_transport.STANDARD_TIME_LIMIT,
        ) -> tuple[int, str]:
            del stdin, limit
            remote_calls.append(args)
            return 255, ""

        def alert_user(
            scope: record.ShutdownScope,
            summary: str,
            text: str,
            *,
            machine: str = "",
        ) -> None:
            del machine
            self.assertEqual(scope["kind"], "all account sessions")
            alerts.append((summary, text))

        output = io.StringIO()
        with (
            patch.object(restart, "named_claude_account", return_value=self.account),
            patch.object(restart, "_run", commands),
            patch.object(restart, "_live_session_ids", return_value=frozenset()),
            patch.object(restart, "wait_for_session", return_value=True),
            patch.object(restart, "run_remote", unreachable),
            patch.object(restart, "other_machine", return_value="mac"),
            patch.object(
                settle,
                "send_message",
                return_value=settle.MessageSent(kind="sent"),
            ),
            patch.object(settle, "alert_user", alert_user),
            patch.object(settle, "record_time", return_value=RESTARTED),
            redirect_stdout(output),
        ):
            self.assertEqual(
                restart.restart(
                    restart.AccountNamed(LABEL),
                    dry_run=False,
                    alerts=restart.AlertWhenNotBack(),
                ),
                0,
            )

        incomplete = (
            "restart incomplete: mac not reached — run /shutdown restart again when it is back"
        )
        self.assertIn(incomplete, output.getvalue())
        self.assertEqual(record.find_live(LOGIN)["kind"], "no shutdown")
        self.assertEqual(remote_calls, [["up", LOGIN]])
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0][0], f"{LABEL} is not fully back")
        self.assertIn(incomplete, alerts[0][1])

    def test_partial_restart_preserves_a_selected_local_scope(self) -> None:
        saved = shutdown_record([])
        saved["scope"] = record.SelectedSessions(
            kind="selected", session_ids=["selected"]
        )
        record.create(saved)
        scopes: list[record.ShutdownScope] = []
        user_alerts: list[str] = []

        def alert_user(
            scope: record.ShutdownScope,
            summary: str,
            text: str,
            *,
            machine: str = "",
        ) -> None:
            del text, machine
            scopes.append(scope)
            if scope["kind"] == "all account sessions":
                user_alerts.append(summary)

        with (
            patch.object(restart, "named_claude_account", return_value=self.account),
            patch.object(
                restart,
                "_up",
                return_value=restart.MachineRestartResult(
                    "natedev", LABEL, "partial"
                ),
            ),
            patch.object(
                restart,
                "run_remote",
                return_value=(0, "Mac: no shutdown to restart"),
            ),
            patch.object(settle, "alert_user", alert_user),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                restart.restart(
                    restart.AccountNamed(LABEL),
                    dry_run=False,
                    alerts=restart.AlertWhenNotBack(),
                ),
                1,
            )

        self.assertEqual(scopes, [saved["scope"]])
        self.assertEqual(user_alerts, [])

    def test_partial_restart_uses_a_selected_scope_from_the_other_machine(self) -> None:
        remote_record = shutdown_record([], machine="Mac")
        remote_record["scope"] = record.SelectedSessions(
            kind="selected", session_ids=["remote-selected"]
        )
        calls: list[list[str]] = []
        scopes: list[record.ShutdownScope] = []
        user_alerts: list[str] = []

        def run_remote(
            args: list[str],
            stdin: str = "",
            limit: remote_transport.RemoteCallLimit = remote_transport.STANDARD_TIME_LIMIT,
        ) -> tuple[int, str]:
            del stdin, limit
            calls.append(args)
            if args == ["up", LOGIN]:
                return 1, "Mac: restart partial"
            self.assertEqual(args, ["records", "--json", "--here"])
            return 0, json.dumps([remote_record])

        def alert_user(
            scope: record.ShutdownScope,
            summary: str,
            text: str,
            *,
            machine: str = "",
        ) -> None:
            del text, machine
            scopes.append(scope)
            if scope["kind"] == "all account sessions":
                user_alerts.append(summary)

        with (
            patch.object(restart, "named_claude_account", return_value=self.account),
            patch.object(
                restart,
                "_up",
                return_value=restart.MachineRestartResult(
                    "natedev", LABEL, "nothing"
                ),
            ),
            patch.object(restart, "run_remote", run_remote),
            patch.object(settle, "alert_user", alert_user),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                restart.restart(
                    restart.AccountNamed(LABEL),
                    dry_run=False,
                    alerts=restart.AlertWhenNotBack(),
                ),
                1,
            )

        self.assertEqual(
            calls, [["up", LOGIN], ["records", "--json", "--here"]]
        )
        self.assertEqual(scopes, [remote_record["scope"]])
        self.assertEqual(user_alerts, [])

    def test_partial_restart_defaults_to_account_wide_when_scope_is_unreadable(self) -> None:
        calls: list[list[str]] = []
        scopes: list[record.ShutdownScope] = []

        def run_remote(
            args: list[str],
            stdin: str = "",
            limit: remote_transport.RemoteCallLimit = remote_transport.STANDARD_TIME_LIMIT,
        ) -> tuple[int, str]:
            del stdin, limit
            calls.append(args)
            return (0, "Mac: no shutdown to restart") if args[0] == "up" else (255, "")

        def alert_user(
            scope: record.ShutdownScope,
            summary: str,
            text: str,
            *,
            machine: str = "",
        ) -> None:
            del summary, text, machine
            scopes.append(scope)

        with (
            patch.object(restart, "named_claude_account", return_value=self.account),
            patch.object(
                restart,
                "_up",
                return_value=restart.MachineRestartResult(
                    "natedev", LABEL, "partial"
                ),
            ),
            patch.object(
                restart,
                "find_live",
                side_effect=record.InvalidRecord("unreadable local record"),
            ),
            patch.object(restart, "run_remote", run_remote),
            patch.object(settle, "alert_user", alert_user),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                restart.restart(
                    restart.AccountNamed(LABEL),
                    dry_run=False,
                    alerts=restart.AlertWhenNotBack(),
                ),
                1,
            )

        self.assertEqual(
            calls, [["records", "--json", "--here"], ["up", LOGIN]]
        )
        self.assertEqual(
            scopes, [record.AllAccountSessions(kind="all account sessions")]
        )

    def test_never_alert_does_not_read_scope_even_when_restart_is_partial(self) -> None:
        calls: list[list[str]] = []

        def run_remote(
            args: list[str],
            stdin: str = "",
            limit: remote_transport.RemoteCallLimit = remote_transport.STANDARD_TIME_LIMIT,
        ) -> tuple[int, str]:
            del stdin, limit
            calls.append(args)
            return 2, ""

        with (
            patch.object(restart, "named_claude_account", return_value=self.account),
            patch.object(
                restart,
                "_up",
                return_value=restart.MachineRestartResult(
                    "natedev", LABEL, "partial"
                ),
            ),
            patch.object(
                restart,
                "find_live",
                side_effect=AssertionError("scope must not be read"),
            ),
            patch.object(restart, "run_remote", run_remote),
            patch.object(settle, "alert_user") as alert_user,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                restart.restart(
                    restart.AccountNamed(LABEL),
                    dry_run=False,
                    alerts=restart.NeverAlert(),
                ),
                1,
            )

        self.assertEqual(calls, [["up", LOGIN]])
        alert_user.assert_not_called()

    def test_restart_cli_builds_a_never_alert_policy(self) -> None:
        received: list[
            tuple[
                restart.AccountNamed | restart.AccountNotNamed,
                bool,
                restart.AlertWhenNotBack | restart.NeverAlert,
            ]
        ] = []

        def run_restart(
            account: restart.AccountNamed | restart.AccountNotNamed,
            *,
            dry_run: bool,
            alerts: restart.AlertWhenNotBack | restart.NeverAlert,
        ) -> int:
            received.append((account, dry_run, alerts))
            return 0

        with patch.object(restart, "restart", run_restart):
            self.assertEqual(
                shutdown.main(["restart", LABEL, "--no-alert"]), 0
            )

        self.assertEqual(
            received,
            [(restart.AccountNamed(LABEL), False, restart.NeverAlert())],
        )

    def test_restart_help_hides_no_alert(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit) as raised:
            _ = shutdown.main(["restart", "--help"])

        self.assertEqual(raised.exception.code, 0)
        self.assertNotIn("--no-alert", output.getvalue())

    def test_unreadable_account_and_unreachable_peer_require_an_account_name(self) -> None:
        saved = shutdown_record([])
        record.create(saved)

        def unreadable() -> Account:
            raise UnreadableAccount("this process has no readable account")

        output = io.StringIO()
        with (
            patch.object(restart, "own_claude_account", unreadable),
            patch.object(restart, "run_remote", return_value=(255, "")),
            patch.object(restart, "other_machine", return_value="mac"),
            patch.object(settle, "alert_user") as alert_user,
            redirect_stdout(output),
            redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(
                restart.restart(
                    restart.AccountNotNamed(),
                    dry_run=False,
                    alerts=restart.AlertWhenNotBack(),
                ),
                1,
            )

        self.assertIn(
            "mac not reached; name the account to restart", output.getvalue()
        )
        self.assertEqual(self.stored_record(), saved)
        alert_user.assert_not_called()

    def test_unknown_named_account_is_a_usage_error(self) -> None:
        with (
            patch.object(
                restart,
                "named_claude_account",
                side_effect=UnknownAccountName("nobody"),
            ),
            patch.object(settle, "alert_user") as alert_user,
            redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(
                restart.restart(
                    restart.AccountNamed("nobody"),
                    dry_run=False,
                    alerts=restart.AlertWhenNotBack(),
                ),
                2,
            )
        alert_user.assert_not_called()

    def test_multiple_record_accounts_need_a_choice(self) -> None:
        record.create(shutdown_record([]))
        remote_record = shutdown_record([], machine="Mac")
        remote_record["login"] = "other@example.com"
        remote_record["label"] = "claude other"

        def unreadable() -> Account:
            raise UnreadableAccount("this process has no readable account")

        output = io.StringIO()
        with (
            patch.object(restart, "own_claude_account", unreadable),
            patch.object(
                restart,
                "run_remote",
                return_value=(0, json.dumps([remote_record])),
            ),
            patch.object(settle, "alert_user") as alert_user,
            redirect_stdout(output),
            redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(
                restart.restart(
                    restart.AccountNotNamed(),
                    dry_run=False,
                    alerts=restart.AlertWhenNotBack(),
                ),
                1,
            )

        self.assertIn(f"natedev: {LABEL} down", output.getvalue())
        self.assertIn("Mac: claude other down", output.getvalue())
        alert_user.assert_not_called()

    def test_an_unreadable_process_account_uses_the_only_down_record_on_either_machine(self) -> None:
        remote_record = shutdown_record([], machine="Mac")
        calls: list[list[str]] = []

        def run_remote(
            args: list[str],
            stdin: str = "",
            limit: remote_transport.RemoteCallLimit = remote_transport.STANDARD_TIME_LIMIT,
        ) -> tuple[int, str]:
            del stdin, limit
            calls.append(args)
            if args == ["records", "--json", "--here"]:
                return 0, json.dumps([remote_record])
            self.assertEqual(args, ["up", LOGIN])
            return 0, "Mac: 0 restarted, 0 seats available on demand"

        def unreadable() -> Account:
            raise UnreadableAccount("this process has no readable account")

        with (
            patch.object(restart, "own_claude_account", unreadable),
            patch.object(restart, "named_claude_account", return_value=self.account),
            patch.object(restart, "run_remote", run_remote),
            patch.object(settle, "alert_user") as alert_user,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                restart.restart(
                    restart.AccountNotNamed(),
                    dry_run=False,
                    alerts=restart.AlertWhenNotBack(),
                ),
                0,
            )

        self.assertEqual(calls, [
            ["records", "--json", "--here"],
            ["up", LOGIN],
        ])
        alert_user.assert_not_called()


if __name__ == "__main__":
    _ = unittest.main()
