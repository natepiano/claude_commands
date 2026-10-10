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
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import cast, final, override
from unittest.mock import patch

import inventory
import launch_permission
import record
import shutdown
from account import Account


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
    state: record.ShutdownState,
    *,
    requested_at: str = OCTOBER,
    progress: record.SessionProgress | None = None,
    session_id: str = "session-123",
) -> record.ShutdownRecord:
    return record.ShutdownRecord(
        login=LOGIN,
        label=LABEL,
        machine="natedev",
        state=state,
        requested_at=requested_at,
        requested_by=record.FromTerminal(kind="terminal"),
        scope=record.AllAccountSessions(kind="all account sessions"),
        conductor=record.ConductorNotStarted(kind="not started"),
        force="wait for ready",
        entries=[
            record.ShutdownSessionEntry(
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


@final
class ShutdownLaunchCliTests(unittest.TestCase):
    root = Path()
    state = Path()
    home = Path()
    environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
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

    def write_record(self, live: record.ShutdownRecord) -> None:
        self.clear_state()
        record.create(live)

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
                self.write_record(shutdown_record(cast(record.ShutdownState, state)))
                status, output, error = self.run_cli(LOGIN)
                self.assertEqual(status, 0)
                self.assertEqual(
                    output,
                    f"{LABEL} is held by a shutdown ({state} since 2026-10-09 14:49 PDT)\n",
                )
                self.assertEqual(error, "")

    def test_launch_blocked_uses_pst_for_a_january_request(self) -> None:
        self.write_record(shutdown_record("down", requested_at=JANUARY))

        status, output, error = self.run_cli(LOGIN)

        self.assertEqual(status, 0)
        self.assertEqual(
            output,
            f"{LABEL} is held by a shutdown (down since 2026-01-15 09:30 PST)\n",
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

    def test_launch_blocked_fails_closed_for_terminal_live_record(self) -> None:
        for state in ("cancelled", "up"):
            with self.subTest(state=state):
                self.write_record(shutdown_record(cast(record.ShutdownState, state)))

                status, output, error = self.run_cli(LOGIN)

                self.assertEqual(status, 3)
                self.assertEqual(output, "")
                self.assertEqual(
                    error,
                    f"shutdown state unreadable: live record in state {state} was not archived\n",
                )

    def test_launch_blocked_fails_closed_when_record_rejects_the_login(self) -> None:
        status, output, error = self.run_cli(".hidden@example.com")

        self.assertEqual(status, 3)
        self.assertEqual(output, "")
        self.assertTrue(error.startswith("shutdown state unreadable: "), error)
        self.assertEqual(error.count("shutdown state unreadable: "), 1)

    def test_launch_blocked_fails_closed_when_account_resolution_crashes(self) -> None:
        self.write_record(shutdown_record("down"))
        notes = Path(self.environment["AGENT_NOTES_DIR"])
        _ = (notes / f"{LABEL}.md").write_bytes(b"\xff")

        result = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).with_name("shutdown.py")),
                "launch-blocked",
                LOGIN,
            ],
            env=self.environment,
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )

        self.assertEqual(result.returncode, 3)
        self.assertEqual(result.stdout, "")
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertTrue(
            result.stderr.startswith("shutdown state unreadable: UnicodeDecodeError: "),
            result.stderr,
        )

    def test_restart_permission_matches_record_state_and_progress(self) -> None:
        allowed = (
            (
                "restarting",
                record.SessionWaiting(kind="waiting"),
            ),
            (
                "restarting",
                record.SessionRestoredTimersPending(
                    kind="timers pending", at=OCTOBER, timers=[]
                ),
            ),
            (
                "restart partial",
                record.SessionNeedsManualRestart(
                    kind="manual restart", command="claude --resume session-123"
                ),
            ),
        )
        for state, progress in allowed:
            with self.subTest(state=state, progress=progress["kind"]):
                self.write_record(
                    shutdown_record(cast(record.ShutdownState, state), progress=progress)
                )

                status, output, error = self.run_cli(
                    LOGIN, "--restart-of", "session-123"
                )

                self.assertEqual((status, output, error), (1, "", ""))

    def test_restart_permission_refuses_entries_the_restart_will_not_launch(self) -> None:
        refused = (
            (
                "restarting",
                record.SessionRestarted(kind="restarted", at=OCTOBER),
                "session-123",
            ),
            (
                "restart partial",
                record.SessionRestartFailed(
                    kind="restart failed", at=OCTOBER, reason="window did not open"
                ),
                "session-123",
            ),
            ("restarting", record.SessionWaiting(kind="waiting"), "not-recorded"),
            ("down", record.SessionWaiting(kind="waiting"), "session-123"),
        )
        for state, progress, requested_session in refused:
            with self.subTest(
                state=state,
                progress=progress["kind"],
                session=requested_session,
            ):
                self.write_record(
                    shutdown_record(cast(record.ShutdownState, state), progress=progress)
                )

                status, output, error = self.run_cli(
                    LOGIN, "--restart-of", requested_session
                )

                self.assertEqual(status, 0)
                self.assertEqual(error, "")
                self.assertIn(f"({state} since ", output)

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
            + "(down since 2026-10-09 14:49 PDT)\n",
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


if __name__ == "__main__":
    _ = unittest.main()
