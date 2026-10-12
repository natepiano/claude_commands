"""Launch gating tests for shutdown records and their process-safe barrier."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Iterable
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Literal, cast, final, override
from unittest.mock import patch

from test_support import ShutdownTestCase
import inventory
from test_record import LegacyRecordFixture, LegacyEntryFixture, write_old
import launch_permission
import record
import shutdown
import settle
import restart as restart_work
from account import Account, UnreadableAccount


LOGIN = "owner@example.com"
LABEL = "claude 2"
OCTOBER = "2026-10-09T21:49:10+00:00"
JANUARY = "2026-01-15T17:30:00+00:00"
PERMISSION_SCRIPT = Path(__file__).with_name("launch_permission.py")


def unit_session(session_id: str) -> inventory.UnitSession:
    return inventory.UnitSession(
        kind="unit",
        session_id=session_id,
        pid=12_345,
        proc_start="process-start",
        name="alpha",
        cwd="/tmp/alpha",
        status="idle",
        model=inventory.NoReplyYet(kind="no reply yet"),
        checkout=inventory.NotACheckout(kind="not a checkout"),
        run_dirs=[],
        codex_servers=[],
        timers=[],
        host=inventory.UnitHost(
            kind="unit",
            production="demo",
            unit="alpha-unit",
            doc="/tmp/production.md",
            tmux_session="alpha",
            plan=inventory.PlanFromRunRecord(kind="plan", path="/tmp/alpha.md"),
        ),
    )


def shutdown_record(
    state: str,
    *,
    requested_at: str = OCTOBER,
    progress: record.SessionProgress | None = None,
    session_id: str = "session-123",
) -> LegacyRecordFixture:
    return LegacyRecordFixture(
        login=LOGIN,
        label=LABEL,
        machine="natedev",
        state=state,
        requested_at=requested_at,
        requested_by=record.FromTerminal(kind="terminal"),
        scope={"kind": "all account sessions"},
        conductor={"kind": "not started"},
        force="wait for ready",
        entries=[
            LegacyEntryFixture(
                session=unit_session(session_id),
                timers=[],
                settle_message=record.SettleMessageNotSent(kind="not sent"),
                where=record.WhereNotSaid(kind="not said"),
                progress=progress or record.SessionWaiting(kind="waiting"),
                stop_issues=[],
            )
        ],
        stop_issues=[],
    )


def session_shutdown(
    progress: record.SessionProgress,
    session_id: str = "session-123",
    *,
    hold: Literal["account", "session"] = "session",
) -> record.SessionShutdown:
    old = shutdown_record("down", progress=progress, session_id=session_id)
    item = old["entries"][0]
    return record.SessionShutdown(
        login=LOGIN, label=LABEL, machine="natedev", requested_at=OCTOBER,
        requested_by=old["requested_by"], hold=hold, request_id="request-one",
        then="restart", force="wait for ready", alerts="user",
        session=item["session"], seats=[], timers=[],
        settle_message=item["settle_message"], where=item["where"],
        progress=progress, stop_issues=[],
    )


def account_hold() -> record.AccountHold:
    return record.AccountHold(
        login=LOGIN, label=LABEL, machine="natedev", requested_at=OCTOBER,
        requested_by=record.FromTerminal(kind="terminal"), then="restart", request_id="request-one",
    )


@final
class ShutdownLaunchCliTests(ShutdownTestCase):
    root = Path()
    state = Path()
    home = Path()
    environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        super().setUp()
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.state = self.root / "shutdown"
        self.home = self.root / "home"
        self.home.mkdir()
        (self.home / ".claude").mkdir()
        _ = (self.home / ".claude.json").write_text(
            json.dumps({"oauthAccount": {"emailAddress": LOGIN}}),
            encoding="utf-8",
        )
        notes = self.root / "notes"
        notes.mkdir()
        _ = (notes / f"{LABEL}.md").write_text(
            f"---\nlogin: {LOGIN}\nstate: active\n---\n",
            encoding="utf-8",
        )
        self.environment = {
            **os.environ,
            "HOME": str(self.home),
            "AGENT_NOTES_DIR": str(notes),
            "SHUTDOWN_STATE_DIR": str(self.state),
        }
        _ = self.environment.pop("CLAUDE_CONFIG_DIR", None)
        self.enterContext(patch.dict(os.environ, self.environment, clear=True))

    def clear_state(self) -> None:
        if self.state.exists():
            shutil.rmtree(self.state)

    def run_cli(self, *arguments: str) -> tuple[int, str, str]:
        output = io.StringIO()
        error = io.StringIO()

        def resolve(requested: str) -> Account:
            return (
                Account("claude", LOGIN, LABEL)
                if requested == LOGIN
                else Account("claude", requested, requested)
            )

        with (
            patch.object(
                shutdown,
                "named_claude_account",
                side_effect=resolve,
            ),
            redirect_stdout(output),
            redirect_stderr(error),
        ):
            status = shutdown.main(["launch-blocked", *arguments])
        return status, output.getvalue(), error.getvalue()

    def write_record(self, live: LegacyRecordFixture) -> None:
        self.clear_state()
        write_old(live)

    def run_exec(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(PERMISSION_SCRIPT),
                "exec-new-work",
                "--name",
                "test-job",
                "--",
                *command,
            ],
            env=self.environment,
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )

    def test_down_restart_passes_the_selected_scope_and_intent(self) -> None:
        selected = Account("claude", LOGIN, LABEL)
        with (
            patch.object(shutdown, "named_claude_account", return_value=selected),
            patch.object(settle, "down", return_value=0) as down,
            patch.object(shutdown, "resolve_sessions", return_value=["unit-a", "unit-b"]),
        ):
            self.assertEqual(shutdown.main([
                "down", LABEL, "--restart", "--only", "unit-a,unit-b,unit-a", "--here",
            ]), 0)
        down.assert_called_once_with(
            selected, True, ("unit-a", "unit-b"), then="restart", alerts="user", requested_by=None, request_id=""
        )


    def test_internal_holds_preserves_empty_old_account_shutdowns(self) -> None:
        saved = shutdown_record("down")
        saved["entries"] = []
        self.write_record(saved)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(shutdown.main(["holds", "--json", "--here"]), 0)
        expected = account_hold()
        expected["request_id"] = f"legacy-natedev-{OCTOBER}"
        expected["then"] = "stop"
        self.assertEqual(output.getvalue(), json.dumps([expected], sort_keys=True) + "\n")
        self.assertEqual(record.session_records(LOGIN), [])

    def test_internal_holds_refuses_unreadable_state(self) -> None:
        broken = self.state / LOGIN / "hold.json"
        broken.parent.mkdir(parents=True)
        _ = broken.write_text("{", encoding="utf-8")
        output = io.StringIO()
        error = io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            self.assertEqual(shutdown.main(["holds", "--json", "--here"]), 3)
        self.assertEqual(output.getvalue(), "")
        self.assertTrue(error.getvalue().startswith("shutdown: "))
        self.assertIn(str(broken), error.getvalue())



    def test_down_without_restart_keeps_the_existing_dispatch(self) -> None:
        selected = Account("claude", LOGIN, LABEL)
        with (
            patch.object(shutdown, "named_claude_account", return_value=selected),
            patch.object(settle, "down", return_value=0) as down,
        ):
            self.assertEqual(shutdown.main(["down", LABEL]), 0)
        down.assert_called_once_with(selected, False, (), then="stop", alerts="user", requested_by=None, request_id="")



    def test_launch_blocked_holds_all_six_shutdown_states(self) -> None:
        for state in (
            "settling",
            "stopping",
            "down",
            "stop partial",
            "restarting",
            "restart partial",
        ):
            with self.subTest(state=state):
                self.write_record(shutdown_record(cast(str, state)))
                status, output, error = self.run_cli(LOGIN)
                self.assertEqual(status, 0)
                self.assertEqual(
                    output,
                    f"{LABEL} is held by a shutdown (since 2026-10-09 14:49 PDT)\n",
                )
                self.assertEqual(error, "")

    def test_launch_blocked_uses_pst_for_a_january_request(self) -> None:
        self.write_record(shutdown_record("down", requested_at=JANUARY))

        status, output, error = self.run_cli(LOGIN)

        self.assertEqual(status, 0)
        self.assertEqual(
            output,
            f"{LABEL} is held by a shutdown (since 2026-01-15 09:30 PST)\n",
        )
        self.assertEqual(error, "")

    def test_launch_blocked_is_silent_when_no_record_exists(self) -> None:
        status, output, error = self.run_cli(LOGIN)

        self.assertEqual((status, output, error), (1, "", ""))

    def test_launch_blocked_fails_closed_for_malformed_and_corrupt_paths(self) -> None:
        cases = ("malformed record", "account file", "broken account symlink")
        for case in cases:
            with self.subTest(case=case):
                self.clear_state()
                self.state.mkdir()
                account_path = self.state / LOGIN
                if case == "malformed record":
                    account_path.mkdir()
                    _ = (account_path / "record.json").write_text("{", encoding="utf-8")
                elif case == "account file":
                    _ = account_path.write_text("not a directory", encoding="utf-8")
                else:
                    account_path.symlink_to(self.state / "missing")

                status, output, error = self.run_cli(LOGIN)

                self.assertEqual(status, 3)
                self.assertEqual(output, "")
                self.assertEqual(len(error.splitlines()), 1)
                self.assertTrue(error.startswith("shutdown state unreadable: "), error)
                self.assertNotIn(
                    "shutdown state unreadable: shutdown state unreadable:", error
                )

    def test_terminal_old_record_no_longer_holds_the_account(self) -> None:
        for state in ("cancelled", "up"):
            with self.subTest(state=state):
                self.write_record(shutdown_record(cast(str, state)))
                self.assertEqual(self.run_cli(LOGIN), (1, "", ""))

    def test_account_hold_blocks_even_without_a_session(self) -> None:
        record.set_account_hold(account_hold())
        self.assertEqual(self.run_cli(LOGIN), (
            0, f"{LABEL} is held by a shutdown (since 2026-10-09 14:49 PDT)\n", "",
        ))

    def test_selected_shutdown_does_not_hold_new_work(self) -> None:
        for old_format in (False, True):
            with self.subTest(old_format=old_format):
                self.clear_state()
                if old_format:
                    saved = shutdown_record("settling")
                    saved["scope"] = {"kind": "selected", "session_ids": ["session-123"]}
                    write_old(saved)
                else:
                    self.assertTrue(record.create_session(session_shutdown(record.SessionWaiting(kind="waiting"))))
                self.assertEqual(self.run_cli(LOGIN), (1, "", ""))

    def test_unreadable_session_or_hold_refuses_new_work_and_restore(self) -> None:
        for name in ("hold.json", "sessions/session-123.json"):
            with self.subTest(name=name):
                self.clear_state()
                broken = self.state / LOGIN / name
                broken.parent.mkdir(parents=True)
                _ = broken.write_text("{", encoding="utf-8")
                for arguments in ((LOGIN,), (LOGIN, "--restart-of", "session-123")):
                    status, output, error = self.run_cli(*arguments)
                    self.assertEqual((status, output), (3, ""))
                    self.assertIn("shutdown state unreadable:", error)

    def test_launch_blocked_fails_closed_when_record_rejects_the_login(self) -> None:
        status, output, error = self.run_cli(".hidden@example.com")

        self.assertEqual(status, 3)
        self.assertEqual(output, "")
        self.assertTrue(error.startswith("shutdown state unreadable: "), error)
        self.assertEqual(error.count("shutdown state unreadable: "), 1)

    def test_launch_blocked_fails_closed_when_account_resolution_crashes(self) -> None:
        output = io.StringIO()
        error = io.StringIO()
        with (
            patch.object(
                shutdown,
                "_selected_account",
                side_effect=RuntimeError("fixture resolver failed"),
            ) as resolve,
            redirect_stdout(output),
            redirect_stderr(error),
        ):
            status = shutdown.main(["launch-blocked", LOGIN])

        resolve.assert_called_once_with(LOGIN)
        self.assertEqual(status, 3)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(len(error.getvalue().splitlines()), 1)
        self.assertTrue(
            error.getvalue().startswith("shutdown state unreadable: RuntimeError: "),
            error.getvalue(),
        )

    def test_restart_permission_uses_its_own_progress_while_another_waits(self) -> None:
        allowed: tuple[record.SessionProgress, ...] = (
            record.SessionStopped(kind="stopped", at=OCTOBER),
            record.SessionAlreadyGone(kind="already gone", at=OCTOBER),
            record.ProcessIdentityLost(kind="process identity lost", at=OCTOBER),
            record.SessionRestarting(kind="restarting", at=OCTOBER, prior=record.SessionStopped(kind="stopped", at=OCTOBER)),
            record.SessionRestartFailed(kind="restart failed", at=OCTOBER, reason="window did not open"),
            record.SessionNeedsManualRestart(kind="manual restart", command="claude --resume session-123"),
        )
        for progress in allowed:
            for kind in ("unit", "top-level", "showrunner"):
                with self.subTest(progress=progress["kind"], kind=kind):
                    self.clear_state()
                    saved = session_shutdown(progress)
                    if kind != "unit":
                        saved["session"] = cast(inventory.Session, cast(object, {
                            **saved["session"], "kind": kind, "host": {"kind": "unknown"},
                            **({"production": "demo", "doc": "/tmp/demo.md"} if kind == "showrunner" else {}),
                        }))
                    self.assertTrue(record.create_session(saved))
                    self.assertTrue(record.create_session(session_shutdown(
                        record.SessionWaiting(kind="waiting"), "session-456", hold="account",
                    )))
                    record.set_account_hold(account_hold())
                    self.assertEqual(self.run_cli(LOGIN, "--restart-of", "session-123"), (1, "", ""))
                    self.assertEqual(self.run_cli(LOGIN)[0], 0)
                    self.assertEqual(self.run_cli(LOGIN, "--restart-of", "session-456")[0], 0)
                    self.assertEqual(self.run_cli(LOGIN, "--restart-of", "missing")[0], 0)

    def test_old_restarting_record_allows_its_stopped_unit_and_holds_new_work(self) -> None:
        self.write_record(shutdown_record("restarting", progress=record.SessionStopped(kind="stopped", at=OCTOBER)))
        self.assertEqual(self.run_cli(LOGIN, "--restart-of", "session-123"), (1, "", ""))
        self.assertEqual(self.run_cli(LOGIN), (
            0, f"{LABEL} is held by a shutdown (since 2026-10-09 14:49 PDT)\n", "",
        ))



    def test_selected_waiting_session_restore_is_held_without_an_account_hold(self) -> None:
        saved = session_shutdown(record.SessionWaiting(kind="waiting"))
        self.assertTrue(record.create_session(saved))
        self.assertIsNone(record.account_hold(LOGIN))
        self.assertEqual(self.run_cli(LOGIN, "--restart-of", "session-123"), (
            0, "alpha is held by a shutdown (since 2026-10-09 14:49 PDT)\n", "",
        ))

    def test_selected_ready_session_restore_is_held_without_an_account_hold(self) -> None:
        saved = session_shutdown(record.SessionReadyToStop(kind="ready", at=OCTOBER))
        saved["requested_at"] = JANUARY
        self.assertTrue(record.create_session(saved))
        self.assertIsNone(record.account_hold(LOGIN))
        self.assertEqual(self.run_cli(LOGIN, "--restart-of", "session-123"), (
            0, "alpha is held by a shutdown (since 2026-01-15 09:30 PST)\n", "",
        ))

    def test_stopped_session_restore_is_allowed_under_an_account_hold(self) -> None:
        self.assertTrue(record.create_session(session_shutdown(
            record.SessionStopped(kind="stopped", at=OCTOBER),
        )))
        record.set_account_hold(account_hold())
        self.assertEqual(self.run_cli(LOGIN, "--restart-of", "session-123"), (1, "", ""))

    def test_missing_session_restore_is_allowed_without_an_account_hold(self) -> None:
        self.assertEqual(self.run_cli(LOGIN, "--restart-of", "missing"), (1, "", ""))

    def test_restart_permission_for_other_progress_is_held_by_its_own_shutdown(self) -> None:
        refused: tuple[record.SessionProgress, ...] = (
            record.SessionWaiting(kind="waiting"),
            record.SessionReadyToStop(kind="ready", at=OCTOBER),
            record.SessionStopFailed(kind="stop failed", at=OCTOBER, reason="busy"),
            record.SessionRestarted(kind="restarted", at=OCTOBER),
            record.SessionRestoredTimersPending(kind="timers pending", at=OCTOBER, timers=[]),
        )
        for progress in refused:
            with self.subTest(progress=progress["kind"]):
                self.clear_state()
                self.assertTrue(record.create_session(session_shutdown(progress)))
                expected = (0, "alpha is held by a shutdown (since 2026-10-09 14:49 PDT)\n", "")
                self.assertEqual(self.run_cli(LOGIN, "--restart-of", "session-123"), expected)
                record.set_account_hold(account_hold())
                self.assertEqual(self.run_cli(LOGIN, "--restart-of", "session-123"), expected)

    def test_held_sessions_includes_owners_and_seats_sorted_once(self) -> None:
        saved = session_shutdown(record.SessionWaiting(kind="waiting"), "z-owner")
        owner = saved["session"]
        saved["seats"] = [inventory.SeatSession(
            session_id="a-seat", pid=23456, proc_start="seat-start", name="worker",
            cwd=owner["cwd"], status="idle", model=owner["model"], checkout=owner["checkout"],
            run_dirs=["/tmp/run"], codex_servers=[], timers=[], kind="seat",
            host=inventory.UnknownHost(kind="unknown"),
            owner=inventory.DirectorOwner(kind="director", session_id="z-owner"),
        )]
        self.assertTrue(record.create_session(saved))
        self.assertTrue(record.create_session(session_shutdown(record.SessionStopped(kind="stopped", at=OCTOBER), "m-owner")))
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(shutdown.main(["held-sessions"]), 0)
        self.assertEqual(output.getvalue(), "a-seat\nm-owner\nz-owner\n")

    def test_launch_permission_maps_exit_statuses(self) -> None:
        with patch.object(
            subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 1, "", ""),
        ):
            allowed = launch_permission.launch_permission(
                self.environment, launch_permission.NewWorkLaunchPurpose()
            )
        self.assertIsInstance(allowed, launch_permission.AllowedByShutdownState)

        with patch.object(
            subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                [], 0, "held because down\n", ""
            ),
        ):
            blocked = launch_permission.launch_permission(
                self.environment, launch_permission.NewWorkLaunchPurpose()
            )
        self.assertEqual(
            blocked, launch_permission.BlockedByShutdown("held because down")
        )

        with patch.object(
            subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                [], 3, "", "shutdown state unreadable: broken JSON\n"
            ),
        ):
            unreadable = launch_permission.launch_permission(
                self.environment, launch_permission.NewWorkLaunchPurpose()
            )
        self.assertEqual(
            unreadable, launch_permission.ShutdownStateUnreadable("broken JSON")
        )

    def test_launch_permission_fails_closed_when_launch_blocked_crashes(self) -> None:
        copied_scripts = self.root / "copied-scripts"
        copied_scripts.mkdir()
        _ = shutil.copy2(PERMISSION_SCRIPT, copied_scripts / PERMISSION_SCRIPT.name)
        _ = (copied_scripts / "shutdown.py").write_text(
            'raise RuntimeError("import exploded")\n', encoding="utf-8"
        )
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import os\n"
                    "import launch_permission\n"
                    "permission = launch_permission.launch_permission(\n"
                    "    os.environ, launch_permission.NewWorkLaunchPurpose()\n"
                    ")\n"
                    "print(type(permission).__name__)\n"
                    "print(getattr(permission, 'detail', ''))\n"
                ),
            ],
            cwd=copied_scripts,
            env=self.environment,
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.splitlines(),
            [
                "ShutdownStateUnreadable",
                "launch-blocked exited 1: RuntimeError: import exploded",
            ],
        )

    def test_launch_permission_reports_missing_interpreter(self) -> None:
        missing = self.root / "missing-python"
        with patch.object(sys, "executable", str(missing)):
            permission = launch_permission.launch_permission(
                self.environment, launch_permission.NewWorkLaunchPurpose()
            )

        self.assertIsInstance(permission, launch_permission.ShutdownStateUnreadable)
        if isinstance(permission, launch_permission.ShutdownStateUnreadable):
            self.assertIn(str(missing), permission.detail)

    def test_launch_permission_passes_restart_session_to_cli(self) -> None:
        commands: list[list[str]] = []

        def run(
            command: list[str], **_arguments: object
        ) -> subprocess.CompletedProcess[str]:
            commands.append(command)
            return subprocess.CompletedProcess(command, 1, "", "")

        with patch.object(subprocess, "run", side_effect=run):
            permission = launch_permission.launch_permission(
                self.environment,
                launch_permission.ShutdownUnitRestoreLaunchPurpose("session-123"),
            )

        self.assertIsInstance(permission, launch_permission.AllowedByShutdownState)
        self.assertEqual(commands[0][-2:], ["--restart-of", "session-123"])

    def test_real_launch_permission_removes_unreadable_prefix(self) -> None:
        account_directory = self.state / LOGIN
        account_directory.mkdir(parents=True)
        _ = (account_directory / "record.json").write_text("{", encoding="utf-8")

        permission = launch_permission.launch_permission(
            self.environment, launch_permission.NewWorkLaunchPurpose()
        )

        self.assertIsInstance(permission, launch_permission.ShutdownStateUnreadable)
        if isinstance(permission, launch_permission.ShutdownStateUnreadable):
            self.assertNotIn("shutdown state unreadable:", permission.detail)
            self.assertIn("not valid JSON", permission.detail)

    def test_exec_new_work_executes_only_when_allowed_and_releases_barrier(self) -> None:
        command = [
            sys.executable,
            "-c",
            (
                "import fcntl, os\n"
                "from pathlib import Path\n"
                "path = Path(os.environ['SHUTDOWN_STATE_DIR']) / 'launch.lock'\n"
                "with path.open('a+') as lock:\n"
                " fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
                " print('command ran with barrier free')\n"
            ),
        ]

        result = self.run_exec(command)

        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        self.assertEqual(result.stdout, "command ran with barrier free\n")
        self.assertEqual(result.stderr, "")

    def test_exec_new_work_skips_blocked_and_unreadable_state(self) -> None:
        self.write_record(shutdown_record("down"))
        blocked = self.run_exec([sys.executable, "-c", "raise SystemExit(91)"])
        self.assertEqual(blocked.returncode, 0)
        self.assertEqual(
            blocked.stdout,
            "test-job: skipped: claude 2 is held by a shutdown "
            + "(since 2026-10-09 14:49 PDT)\n",
        )
        self.assertEqual(blocked.stderr, "")

        self.clear_state()
        account_directory = self.state / LOGIN
        account_directory.mkdir(parents=True)
        _ = (account_directory / "record.json").write_text("{", encoding="utf-8")
        unreadable = self.run_exec([sys.executable, "-c", "raise SystemExit(92)"])
        self.assertEqual(unreadable.returncode, 0)
        self.assertTrue(
            unreadable.stdout.startswith(
                "test-job: skipped: shutdown state unreadable: "
            ),
            unreadable.stdout,
        )
        self.assertEqual(unreadable.stdout.count("shutdown state unreadable: "), 1)
        self.assertEqual(unreadable.stderr, "")

    def test_exec_new_work_skips_when_launch_barrier_cannot_be_opened(self) -> None:
        self.clear_state()
        _ = self.state.write_text("not a directory", encoding="utf-8")

        result = self.run_exec([sys.executable, "-c", "raise SystemExit(93)"])

        self.assertEqual(result.returncode, 0)
        self.assertTrue(
            result.stdout.startswith(
                "test-job: skipped: shutdown state unreadable: launch barrier: "
            ),
            result.stdout,
        )
        self.assertEqual(result.stdout.count("shutdown state unreadable: "), 1)
        self.assertEqual(result.stderr, "")


    def test_conduct_dispatches_only_the_local_conductor(self) -> None:
        with patch.object(settle, "conduct", return_value=0) as conduct:
            self.assertEqual(shutdown.main(["conduct", LOGIN]), 0)
        conduct.assert_called_once_with(LOGIN)

    def test_peer_down_receives_request_identity_origin_and_alert_policy(self) -> None:
        selected = Account("claude", LOGIN, LABEL)
        with patch.object(shutdown, "named_claude_account", return_value=selected), patch.object(settle, "down", return_value=0) as down:
            self.assertEqual(shutdown.main(["down", LOGIN, "--here", "--restart", "--no-alert", "--requested-by", "requester", "--request-id", "same-request"]), 0)
        down.assert_called_once_with(selected, True, (), then="restart", alerts="none", requested_by="requester", request_id="same-request")

    def test_stopping_session_restore_is_held_by_name(self) -> None:
        self.assertTrue(record.create_session(session_shutdown(record.SessionStopping(kind="stopping", at=OCTOBER))))
        self.assertEqual(self.run_cli(LOGIN, "--restart-of", "session-123"), (0, "alpha is held by a shutdown (since 2026-10-09 14:49 PDT)\n", ""))

    def test_removed_writer_verbs_are_rejected(self) -> None:
        removed = ("begin", "refresh", "retry-stop", "stop", "claim-stop", "close-failed-stop")
        for verb in removed:
            with self.subTest(verb=verb), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                _ = shutdown.main([verb, LOGIN])
            self.assertEqual(raised.exception.code, 2)


@final
class OnlySessionCliTests(ShutdownTestCase):
    root = Path()
    owners: list[inventory.Session] = []

    @override
    def setUp(self) -> None:
        super().setUp()
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"HOME": str(self.root), "SHUTDOWN_STATE_DIR": str(self.root / "state")})))
        self.owners = []
        for identity, name in (("alpha-id", "Alpha"), ("beta-id", "Beta"), ("gamma-id", "Gamma")):
            owner = unit_session(identity)
            owner["name"] = name
            self.owners.append(owner)
        selected = Account("claude", LOGIN, LABEL)
        _ = self.enterContext(patch.object(shutdown, "named_claude_account", return_value=selected))
        _ = self.enterContext(patch.object(restart_work, "named_claude_account", return_value=selected))
        report = inventory.Inventory(machine="natedev", login=LOGIN, label=LABEL, sessions=self.owners, unattributed=[])
        _ = self.enterContext(patch.object(shutdown, "inventory", return_value=report))
        def observed(login: str, only: Iterable[str] = ()) -> inventory.Inventory:
            self.assertEqual(login, LOGIN)
            ids = tuple(only)
            return {**report, "sessions": [owner for owner in self.owners if not ids or owner["session_id"] in ids]}
        _ = self.enterContext(patch.object(settle, "run_inventory", side_effect=observed))
        _ = self.enterContext(patch.object(settle, "inventory", side_effect=observed))
        _ = self.enterContext(patch.object(restart_work, "inventory", side_effect=observed))
        _ = self.enterContext(patch.object(settle, "_warm", return_value=True))
        _ = self.enterContext(patch.object(settle, "_launch_conductor"))
        _ = self.enterContext(patch.object(settle, "_retire_shutdown_messages"))
        _ = self.enterContext(patch.object(settle, "send_message", return_value=settle.MessageSent(kind="sent")))
        _ = self.enterContext(patch.object(restart_work, "_retire"))
        _ = self.enterContext(patch.object(restart_work, "_send_restart_note", return_value=settle.MessageSent(kind="sent")))
        _ = self.enterContext(patch.object(restart_work, "_live_session_ids", return_value=frozenset()))
        def confirmed(ids: frozenset[str], _timeout: float) -> frozenset[str]:
            return ids
        _ = self.enterContext(patch.object(restart_work, "wait_for_sessions", side_effect=confirmed))
        _ = self.enterContext(patch.object(restart_work, "_launch_session", return_value=restart_work.SessionLaunched()))

    def save_three(self, progress: record.SessionProgress) -> list[record.SessionShutdown]:
        saved: list[record.SessionShutdown] = []
        for owner in self.owners:
            item = session_shutdown(progress, owner["session_id"])
            item["session"] = owner
            self.assertTrue(record.create_session(item))
            saved.append(item)
        return saved

    def action(self, verb: str, selection: str, *flags: str) -> tuple[int, str, str]:
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            status = shutdown.main([verb, LOGIN, "--only", selection, "--here", *flags])
        return status, output.getvalue(), error.getvalue()

    def test_only_verbs_by_id_or_exact_name_change_one_of_three_sessions(self) -> None:
        for verb in ("down", "cancel", "now", "restart"):
            for selection in ("beta-id", "Beta"):
                with self.subTest(verb=verb, selection=selection):
                    state = self.root / "state"
                    if state.exists():
                        shutil.rmtree(state)
                    progress: record.SessionProgress = {"kind": "stopped", "at": OCTOBER} if verb == "restart" else {"kind": "waiting"}
                    before = self.save_three(progress)
                    if verb == "down":
                        record.archive_session(LOGIN, "beta-id")
                    status, output, error = self.action(verb, selection)
                    self.assertEqual((status, error), (0, ""))
                    self.assertEqual(record.find_session(LOGIN, "alpha-id"), before[0])
                    self.assertEqual(record.find_session(LOGIN, "gamma-id"), before[2])
                    selected = record.find_session(LOGIN, "beta-id")
                    if verb in {"cancel", "restart"}:
                        self.assertIsNone(selected)
                    else:
                        assert selected is not None
                        self.assertEqual(selected["force"], "now" if verb == "now" else "wait for ready")
                        self.assertIn(f"Beta: {'stopping now' if verb == 'now' else 'added'}\n", output)

    def test_only_ambiguous_name_lists_candidates_and_changes_no_record(self) -> None:
        self.owners[0]["name"] = self.owners[1]["name"] = "Duplicate"
        before = self.save_three({"kind": "waiting"})
        status, output, error = self.action("now", "Duplicate")
        self.assertEqual(status, 2)
        diagnostic = output + error
        self.assertIn("alpha-id", diagnostic)
        self.assertIn("beta-id", diagnostic)
        self.assertEqual(record.session_records(LOGIN), sorted(before, key=lambda item: item["session"]["name"]))

    def test_only_unknown_item_exits_two_without_starting_work(self) -> None:
        before = self.save_three({"kind": "waiting"})
        with patch.object(settle, "_launch_conductor") as conductor:
            status, output, error = self.action("now", "absent")
        self.assertEqual(status, 2)
        self.assertIn("absent", output + error)
        conductor.assert_not_called()
        self.assertEqual(record.session_records(LOGIN), before)

    def test_only_restart_refuses_waiting_ready_and_stopping_with_exact_line(self) -> None:
        cases: list[record.SessionProgress] = [{"kind": "waiting"}, {"kind": "ready", "at": OCTOBER}, {"kind": "stopping", "at": OCTOBER}]
        for progress in cases:
            with self.subTest(progress=progress["kind"]):
                state = self.root / "state"
                if state.exists():
                    shutil.rmtree(state)
                before = self.save_three(progress)
                with patch.object(restart_work, "_launch_session") as launch:
                    status, output, error = self.action("restart", "Beta", "--no-alert")
                self.assertEqual((status, error), (1, ""))
                self.assertIn(f"Beta is {progress['kind']}, not stopped; nothing to restart\n", output)
                launch.assert_not_called()
                self.assertEqual(record.session_records(LOGIN), before)

    def test_only_cancel_retained_record_names_the_selected_restart_action(self) -> None:
        _ = self.save_three({"kind": "stopping", "at": OCTOBER})
        status, output, error = self.action("cancel", "Beta")
        self.assertEqual((status, error), (1, ""))
        self.assertEqual(output, "Beta was not cancelled: stopping; /shutdown restart --only Beta brings it back\n")

    def test_only_down_resumes_existing_record_without_replacing_request_fields(self) -> None:
        before = self.save_three({"kind": "waiting"})
        with patch.object(settle, "_launch_conductor") as conductor:
            status, output, error = self.action("down", "Beta", "--restart", "--no-alert")
        self.assertEqual((status, error), (0, ""))
        self.assertIn("Beta: already in progress", output)
        conductor.assert_called_once_with(LOGIN, LABEL)
        self.assertEqual(record.session_records(LOGIN), before)

    def test_only_help_is_public_for_each_action(self) -> None:
        for verb in ("down", "cancel", "now", "restart"):
            with self.subTest(verb=verb), redirect_stdout(output := io.StringIO()):
                with self.assertRaises(SystemExit) as exit_status:
                    _ = shutdown.main([verb, "--help"])
                self.assertEqual(exit_status.exception.code, 0)
                self.assertIn("--only NAME[,NAME...]", output.getvalue())
                self.assertNotIn("==SUPPRESS==", output.getvalue())

    def test_resolve_sessions_here_deduplicates_record_and_inventory_without_peer(self) -> None:
        _ = self.save_three({"kind": "waiting"})
        with patch.object(shutdown, "run_remote") as peer:
            self.assertEqual(shutdown.resolve_sessions(LOGIN, ["Beta", "beta-id", "Beta"], here=True), ["beta-id"])
        peer.assert_not_called()

    def test_resolve_local_id_does_not_wait_for_the_peer(self) -> None:
        _ = self.save_three({"kind": "waiting"})
        with patch.object(shutdown, "run_remote") as peer:
            self.assertEqual(shutdown.resolve_sessions(LOGIN, ["beta-id"]), ["beta-id"])
        peer.assert_not_called()

    def test_resolve_peer_name_counts_its_record_and_inventory_once(self) -> None:
        remote_owner = unit_session("remote-id")
        remote_owner["name"] = "Remote"
        remote_record = session_shutdown({"kind": "waiting"}, "remote-id")
        remote_record["session"], remote_record["machine"] = remote_owner, "Mac"
        remote_inventory = inventory.Inventory(machine="Mac", login=LOGIN, label=LABEL, sessions=[remote_owner], unattributed=[])
        def peer(arguments: list[str]) -> tuple[int, str]:
            if arguments == ["records", "--json", "--here"]:
                return 0, json.dumps([remote_record])
            self.assertEqual(arguments, ["status", LOGIN, "--json", "--here"])
            return 0, json.dumps(remote_inventory)
        with patch.object(shutdown, "run_remote", side_effect=peer), patch.object(shutdown, "other_machine", return_value="Mac"):
            self.assertEqual(shutdown.resolve_sessions(LOGIN, ["Remote"]), ["remote-id"])

    def test_resolve_name_with_unreachable_peer_uses_local_and_warns(self) -> None:
        _ = self.save_three({"kind": "waiting"})
        error = io.StringIO()
        with patch.object(shutdown, "run_remote", return_value=(255, "")), patch.object(shutdown, "other_machine", return_value="Mac"), redirect_stderr(error):
            self.assertEqual(shutdown.resolve_sessions(LOGIN, ["Beta"]), ["beta-id"])
        self.assertIn("Mac: not reached; names checked on natedev only\n", error.getvalue())

    def test_only_missing_name_with_unreachable_peer_exits_two_and_names_peer(self) -> None:
        _ = self.save_three({"kind": "waiting"})
        output, error = io.StringIO(), io.StringIO()
        with patch.object(shutdown, "run_remote", return_value=(255, "")), patch.object(shutdown, "other_machine", return_value="Mac"), redirect_stdout(output), redirect_stderr(error):
            self.assertEqual(shutdown.main(["now", LOGIN, "--only", "Missing"]), 2)
        self.assertIn("Missing", output.getvalue() + error.getvalue())
        self.assertIn("Mac", error.getvalue())
        self.assertIn("not reached", error.getvalue())

    def test_only_down_creates_selected_session_from_three_live_sessions(self) -> None:
        status, output, error = self.action("down", "Beta")
        self.assertEqual((status, error), (0, ""))
        self.assertEqual([saved["session"]["session_id"] for saved in record.session_records(LOGIN)], ["beta-id"])
        self.assertIn("Beta: added\n", output)
        self.assertIsNone(record.account_hold(LOGIN))

    def test_only_verbs_route_peer_id_without_changing_local_records(self) -> None:
        before = self.save_three({"kind": "waiting"})
        remote_owner = unit_session("remote-id")
        remote_owner["name"] = "Remote"
        remote_record = session_shutdown({"kind": "stopped", "at": OCTOBER}, "remote-id")
        remote_record["session"], remote_record["machine"] = remote_owner, "Mac"
        report = inventory.Inventory(machine="Mac", login=LOGIN, label=LABEL, sessions=[remote_owner], unattributed=[])
        for verb in ("down", "cancel", "now", "restart"):
            for selection in ("remote-id", "Remote"):
                with self.subTest(verb=verb, selection=selection):
                    calls: list[list[str]] = []
                    def peer(arguments: list[str], **_keywords: object) -> tuple[int, str]:
                        calls.append(arguments)
                        if arguments[0] == "records":
                            return 0, json.dumps([remote_record])
                        if arguments[0] == "status":
                            return 0, json.dumps(report)
                        return 0, "Remote: action completed"
                    output, error = io.StringIO(), io.StringIO()
                    with patch.object(shutdown, "run_remote", side_effect=peer), patch.object(settle, "run_remote", side_effect=peer), patch.object(restart_work, "run_remote", side_effect=peer), redirect_stdout(output), redirect_stderr(error):
                        self.assertEqual(shutdown.main([verb, LOGIN, "--only", selection, "--no-alert"] if verb == "restart" else [verb, LOGIN, "--only", selection]), 0)
                    action = calls[-1]
                    self.assertEqual(action[0], "up" if verb == "restart" else verb)
                    self.assertEqual(action[1], LOGIN)
                    self.assertEqual(action[action.index("--only") + 1], "remote-id")
                    if verb != "restart":
                        self.assertIn("--here", action)
                    self.assertEqual(record.session_records(LOGIN), before)

    def test_selected_live_local_session_without_record_answers_without_peer(self) -> None:
        for verb in ("cancel", "now", "restart"):
            with self.subTest(verb=verb), patch.object(shutdown, "run_remote") as selection_peer, patch.object(settle, "run_remote") as action_peer, patch.object(restart_work, "run_remote") as restart_peer, patch.object(restart_work, "inventory", return_value=inventory.Inventory(machine="natedev", login=LOGIN, label=LABEL, sessions=[self.owners[1]], unattributed=[])) as observed, patch.object(settle, "_launch_conductor") as conductor:
                output, error = io.StringIO(), io.StringIO()
                with redirect_stdout(output), redirect_stderr(error):
                    status = shutdown.main([verb, LOGIN, "--only", "beta-id"])
                self.assertEqual((status, output.getvalue(), error.getvalue()), (1, "Beta: no shutdown in progress\n", ""))
                selection_peer.assert_not_called()
                action_peer.assert_not_called()
                restart_peer.assert_not_called()
                conductor.assert_not_called()
                if verb == "restart":
                    observed.assert_called_once_with(LOGIN, frozenset({"beta-id"}))

    def test_selected_restart_infers_account_from_records_and_holds_when_own_account_is_unreadable(self) -> None:
        for source in ("record", "hold"):
            with self.subTest(source=source):
                state = self.root / "state"
                if state.exists():
                    shutil.rmtree(state)
                if source == "record":
                    _ = self.save_three({"kind": "stopped", "at": OCTOBER})
                else:
                    record.set_account_hold(record.AccountHold(login=LOGIN, label=LABEL, machine="natedev", requested_at=OCTOBER, requested_by={"kind": "terminal"}, request_id="held", then="stop"))
                output, error = io.StringIO(), io.StringIO()
                with patch.object(shutdown, "own_claude_account", side_effect=UnreadableAccount("own account unreadable")), patch.object(restart_work, "own_claude_account", side_effect=UnreadableAccount("own account unreadable")), patch.object(restart_work, "run_remote", return_value=(0, "[]")), patch.object(restart_work, "_up", return_value=restart_work.MachineRestartResult("natedev", LABEL, "done")) as resume, redirect_stdout(output), redirect_stderr(error):
                    self.assertEqual(shutdown.main(["restart", "--only", "beta-id", "--here", "--no-alert"]), 0)
                self.assertEqual(error.getvalue(), "")
                resume.assert_called_once_with(LOGIN, False, ("beta-id",))

    def test_selected_restart_silent_failure_ignores_sibling_policy_and_never_reads_peer_here(self) -> None:
        before = self.save_three({"kind": "stopped", "at": OCTOBER})
        def silence(current: record.SessionShutdown) -> None:
            current["alerts"] = "none"
        _ = record.update_session(LOGIN, "beta-id", silence)
        with patch.object(restart_work, "_launch_session", side_effect=OSError("launch failed")), patch.object(restart_work, "run_remote") as peer, patch.object(settle, "alert_user") as alert:
            status, _output, error = self.action("restart", "beta-id")
        self.assertEqual((status, error), (1, ""))
        alert.assert_not_called()
        peer.assert_not_called()
        self.assertEqual(record.find_session(LOGIN, "alpha-id"), before[0])
        self.assertEqual(record.find_session(LOGIN, "gamma-id"), before[2])

    def test_selected_restart_refusal_has_no_alert_count_or_summary(self) -> None:
        _ = self.save_three({"kind": "waiting"})
        with patch.object(settle, "alert_user") as alert, patch.object(restart_work, "_print_result") as summary:
            status, output, error = self.action("restart", "beta-id")
        self.assertEqual((status, output, error), (1, "Beta is waiting, not stopped; nothing to restart\n", ""))
        alert.assert_not_called()
        summary.assert_not_called()

    def test_selected_restart_refusal_beside_real_failure_alerts_for_failed_session(self) -> None:
        _ = self.save_three({"kind": "stopped", "at": OCTOBER})
        def wait(current: record.SessionShutdown) -> None:
            current["progress"] = {"kind": "waiting"}
        _ = record.update_session(LOGIN, "alpha-id", wait)
        with patch.object(restart_work, "_launch_session", side_effect=OSError("window launch failed")), patch.object(restart_work, "_session_liveness", return_value=restart_work.SessionNotLive()), patch.object(settle, "alert_user") as alert:
            status, output, error = self.action("restart", "alpha-id,beta-id")
        self.assertEqual((status, error), (1, ""))
        self.assertIn("Alpha is waiting, not stopped; nothing to restart\n", output)
        alert.assert_called_once()
        body = cast(str, alert.call_args.args[1])
        self.assertIn("Beta", body)
        self.assertIn("window launch failed", body)
        self.assertNotIn("2 not back", body)

    def test_selected_now_refuses_in_progress_and_clean_stopped_records_without_work(self) -> None:
        cases: list[record.SessionProgress] = [{"kind": "stopping", "at": OCTOBER}, {"kind": "stopped", "at": OCTOBER}]
        for progress in cases:
            with self.subTest(progress=progress["kind"]):
                state = self.root / "state"
                if state.exists():
                    shutil.rmtree(state)
                before = self.save_three(progress)
                with patch.object(settle, "_launch_conductor") as conductor:
                    status, output, error = self.action("now", "beta-id")
                self.assertEqual((status, output, error), (1, f"Beta is {progress['kind']}; nothing to stop\n", ""))
                conductor.assert_not_called()
                self.assertEqual(record.session_records(LOGIN), before)

    def test_selected_cancel_names_the_waiting_session_taken_out(self) -> None:
        _ = self.save_three({"kind": "waiting"})
        self.assertEqual(self.action("cancel", "beta-id"), (0, "Beta: taken out\n", ""))

    def test_selected_restart_fresh_claim_names_existing_worker_without_launch_or_alert(self) -> None:
        _ = self.save_three({"kind": "stopped", "at": OCTOBER})
        def claim(current: record.SessionShutdown) -> None:
            current["progress"] = {"kind": "restarting", "at": settle.record_time(), "prior": {"kind": "stopped", "at": OCTOBER}}
        saved = record.update_session(LOGIN, "beta-id", claim)
        with patch.object(restart_work, "_launch_session") as launch, patch.object(settle, "alert_user") as alert:
            self.assertEqual(self.action("restart", "beta-id"), (0, "Beta is already restarting\n", ""))
        launch.assert_not_called()
        alert.assert_not_called()
        self.assertEqual(record.find_session(LOGIN, "beta-id"), saved)

    def test_resolve_peer_stopped_record_when_inventory_is_unavailable_keeps_id_candidate(self) -> None:
        saved = session_shutdown({"kind": "stopped", "at": OCTOBER}, "remote-id")
        saved["session"]["name"], saved["machine"] = "Remote", "Mac"
        def peer(arguments: list[str]) -> tuple[int, str]:
            return (0, json.dumps([saved])) if arguments[0] == "records" else (255, "")
        error = io.StringIO()
        with patch.object(shutdown, "run_remote", side_effect=peer), patch.object(shutdown, "other_machine", return_value="Mac"), redirect_stderr(error):
            self.assertEqual(shutdown.resolve_sessions(LOGIN, ["remote-id"]), ["remote-id"])
        self.assertEqual(error.getvalue(), "Mac: not reached; names checked on natedev only\n")

    def test_selected_now_mixed_force_and_refusal_exits_one_and_only_forces_eligible_session(self) -> None:
        before = self.save_three({"kind": "waiting"})
        def stopped(current: record.SessionShutdown) -> None:
            current["progress"] = {"kind": "stopped", "at": OCTOBER}
        refused = record.update_session(LOGIN, "beta-id", stopped)
        with patch.object(settle, "_launch_conductor") as conductor:
            status, output, error = self.action("now", "alpha-id,beta-id")
        self.assertEqual((status, output, error), (1, "Alpha: stopping now\nBeta is stopped; nothing to stop\n", ""))
        conductor.assert_called_once_with(LOGIN, LABEL)
        changed = record.find_session(LOGIN, "alpha-id")
        assert changed is not None
        self.assertEqual(changed["force"], "now")
        self.assertEqual(record.find_session(LOGIN, "beta-id"), refused)
        self.assertEqual(record.find_session(LOGIN, "gamma-id"), before[2])



if __name__ == "__main__":
    _ = unittest.main()
