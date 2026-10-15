"""Launch gating tests for shutdown records and their process-safe barrier."""

from __future__ import annotations

from scripts.whoami.note_file import note_name

import io
import json
import os
import shutil
import subprocess
import sys
import time
import tempfile
import unittest
from collections.abc import Iterable
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Literal, cast, final, override
from unittest.mock import patch

from test_support import registry_confirmation, ShutdownTestCase
import inventory
from test_record import LegacyRecordFixture, LegacyEntryFixture, write_old
import launch_permission
import record
import sessions
import remote_control
import shutdown
import settle
from scripts.accounts import accounts
from push_texts import RenderedPush
import restart as restart_work
from account import Account, UnreadableAccount


LOGIN = "owner@example.com"
LABEL = "claude2"
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
        updated_at=0, probe=False,
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
            session_name="alpha",
            session_host="tmux",
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
        then="restart", force="wait for ready", alerts="user", probe=False,
        session=item["session"], workers=[], timers=[],
        settle_message=item["settle_message"], where=item["where"],
        progress=progress, trail=[], stop_issues=[],
    )


def account_hold() -> record.AccountHold:
    return record.AccountHold(
        login=LOGIN, label=LABEL, machine="natedev", requested_at=OCTOBER,
        requested_by=record.FromTerminal(kind="terminal"), then="restart", request_id="request-one",
    )


def setUpModule() -> None:
    """The times these tests expect are Pacific; the user's zone setting must not change them."""
    unittest.enterModuleContext(patch.dict(os.environ, {"USER_ZONE": "America/Los_Angeles"}))


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
        _ = (notes / note_name(LABEL)).write_text(
            f"---\nlogin: {LOGIN}\nsessions: 1\n---\n",
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

    def test_down_rejects_non_numbered_targets_before_creating_records_requests_or_a_conductor(self) -> None:
        moves_file = self.home / ".local/state/accounts/moves.jsonl"
        selected = Account("claude", LOGIN, LABEL)
        report = inventory.Inventory(machine="natedev", login=LOGIN, label=LABEL,
            sessions=[unit_session("session-123")], unattributed=[])
        for target in ("default", "~/.claude", "claude", "codex1"):
            self.clear_state()
            moves_file.unlink(missing_ok=True)
            with (self.subTest(target=target),
                  patch.object(shutdown, "named_claude_account", return_value=selected),
                  patch.object(shutdown, "inventory", return_value=report) as observed,
                  patch.object(shutdown, "run_remote") as peer,
                  patch.object(settle, "run_remote", return_value=(0, "")) as forwarded,
                  patch.object(settle, "_warm", return_value=True),
                  patch.object(settle, "send_message", return_value=settle.MessageSent(kind="sent")),
                  patch.object(settle, "_launch_conductor") as conductor):
                output, error = io.StringIO(), io.StringIO()
                with redirect_stdout(output), redirect_stderr(error):
                    try:
                        status = shutdown.main(["down", LABEL, "--restart", "--to", target])
                    except SystemExit as exit_error:
                        status = exit_error.code
                self.assertNotEqual(status, 0)
                if target == "default":
                    self.assertIn("name the account by number", error.getvalue())
                    self.assertIn("~/.claude is claude2", error.getvalue())
                else:
                    self.assertIn(target, error.getvalue())
                self.assertFalse(self.state.exists() and any(self.state.iterdir()))
                self.assertFalse(moves_file.exists() and moves_file.stat().st_size)
                conductor.assert_not_called()
                peer.assert_not_called()
                forwarded.assert_not_called()
                observed.assert_not_called()

    def test_down_rejects_invalid_move_origins_before_observing_or_writing_shutdown_state(self) -> None:
        invalid = (("--refused", "bogus"),
                   ("--refused", "weekly", "--reset", "2026-10-20T12:00:00"),
                   ("--reset", "2026-10-20T12:00:00+00:00"))
        for arguments in invalid:
            with (self.subTest(arguments=arguments), patch.object(settle, "down") as down,
                  patch.object(shutdown, "resolve_sessions") as resolve,
                  patch.object(shutdown, "run_remote") as peer,
                  redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
                with self.assertRaises(SystemExit) as refused:
                    _ = shutdown.main(["down", LABEL, "--restart", "--to", "claude3", *arguments])
                self.assertEqual(refused.exception.code, 2)
                down.assert_not_called()
                resolve.assert_not_called()
                peer.assert_not_called()
                self.assertFalse(self.state.exists() and any(self.state.iterdir()))
                journal = self.home / ".local/state/accounts/moves.jsonl"
                self.assertFalse(journal.exists() and journal.stat().st_size)

    def test_down_passes_refusal_window_and_aware_reset_or_hand_origin_to_settle(self) -> None:
        from scripts.accounts import accounts
        from datetime import datetime
        selected = Account("claude", LOGIN, LABEL)
        for arguments, refusal in ((("--refused", "weekly", "--reset", "2026-10-20T12:00:00+03:00"), True), ((), False)):
            with (self.subTest(arguments=arguments),
                  patch.object(shutdown, "named_claude_account", return_value=selected),
                  patch.object(settle, "down", return_value=0) as down):
                self.assertEqual(shutdown.main(["down", LABEL, "--here", "--restart", "--to", "claude3", *arguments]), 0)
                origin = cast(object, down.call_args.kwargs["origin"])
                if refusal:
                    self.assertIsInstance(origin, accounts.RefusalOrigin)
                    assert isinstance(origin, accounts.RefusalOrigin)
                    self.assertEqual(origin.window, "weekly")
                    self.assertEqual(origin.reset, accounts.KnownReset(datetime.fromisoformat("2026-10-20T12:00:00+03:00")))
                else:
                    self.assertIsInstance(origin, accounts.HandOrigin)

    def test_remote_control_cli_passes_identity_without_an_alert_option(self) -> None:
        arguments = ["remote-control", "owner", "--pid", "321",
            "--proc-start", "original", "--who", "trunk unit", "--session", "Owner"]
        with patch.object(remote_control, "fix", return_value=remote_control.AlreadyOn()) as fix:
            self.assertEqual(shutdown.main(arguments), 0)
        fix.assert_called_once_with("owner", 321, "original", "trunk unit", "Owner")
        with patch.object(remote_control, "fix") as fix, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as rejected:
                _ = shutdown.main([*arguments, "--alerts", "user"])
        self.assertEqual(rejected.exception.code, 2)
        fix.assert_not_called()
        with redirect_stdout(help_text := io.StringIO()), self.assertRaises(SystemExit):
            _ = shutdown.main(["remote-control", "--help"])
        self.assertNotIn("--alerts", help_text.getvalue())

    def follow_request(self, output: io.StringIO | None = None) -> tuple[int, str]:
        output = output if output is not None else io.StringIO()
        with (
            patch.object(shutdown, "named_claude_account", return_value=Account("claude", LOGIN, LABEL)),
            patch.object(shutdown, "inventory", side_effect=AssertionError("follow inventories live sessions")),
            patch.object(shutdown, "run_remote", side_effect=AssertionError("follow reads the peer")),
            redirect_stdout(output),
        ):
            result = shutdown.main(["follow", LOGIN, "--request", "request-one"])
        return result, output.getvalue()

    def test_recovered_not_ready_status_offers_cancel_and_force_while_follow_keeps_waiting(self) -> None:
        saved = session_shutdown(record.SessionStopFailed(kind="stop failed", at=OCTOBER,
            reason="session or worker is not idle and ready"))
        saved["stop_issues"] = [record.NotReadyToStop(kind="not ready to stop", at=OCTOBER,
            status="busy", progress="waiting")]
        directory = self.state / LOGIN / "sessions"
        directory.mkdir(parents=True)
        _ = (directory / "session-123.json").write_text(json.dumps(saved))
        output = io.StringIO()
        with (
            patch.object(shutdown, "named_claude_account", return_value=Account("claude", LOGIN, LABEL)),
            redirect_stdout(output),
        ):
            self.assertEqual(shutdown.main(["status", LOGIN, "--here"]), 0)
        text = output.getvalue()
        self.assertIn("waiting", text)
        self.assertIn("not stopped: busy, waiting", text)
        self.assertIn(f"cancel {LOGIN} --here --only session-123", text)
        self.assertIn(f"now {LOGIN} --here --only session-123", text)
        self.assertIn("in a terminal on natedev", text)
        self.assertNotIn("stop failed", text)
        self.assertNotIn("bring back:", text)
        with patch.object(time, "sleep", side_effect=KeyboardInterrupt) as wait:
            status, text = self.follow_request()
        wait.assert_called_once()
        self.assertEqual(status, 130)
        self.assertIn("the shutdown keeps running", text)
        self.assertNotIn("stop failed", text)
        current = record.find_session(LOGIN, "session-123")
        assert current is not None
        self.assertEqual(current["progress"], {"kind": "waiting"})

    def test_follow_prints_each_new_step_once_and_waits_for_pending_restart(self) -> None:
        saved = session_shutdown(record.SessionWaiting(kind="waiting"))
        saved["session"]["name"] = "Alpha"
        # Legacy-spelled stored targets must print as canonical labels.
        saved["target"] = "claude 3"
        saved["trail"] = [{"at": OCTOBER, "kind": "waiting", "detail": "pause sent"}]
        self.assertTrue(record.create_session(saved))
        unrelated = session_shutdown(record.SessionStopFailed(kind="stop failed", at=OCTOBER, reason="other request"), "unrelated")
        unrelated["request_id"] = "another-request"
        self.assertTrue(record.create_session(unrelated))
        stages: list[record.SessionProgress] = [
            record.SessionReadyToStop(kind="ready", at=OCTOBER),
            record.SessionStopped(kind="stopped", at=OCTOBER),
            record.SessionAlreadyGone(kind="already gone", at=OCTOBER),
            record.SessionRestarting(kind="restarting", at=OCTOBER,
                prior=record.SessionStopped(kind="stopped", at=OCTOBER)),
            record.SessionRestarted(kind="restarted", at=OCTOBER),
        ]
        polls: list[float] = []
        live_output = io.StringIO()
        observed_kinds = ["waiting"]
        def advance(seconds: float) -> None:
            polls.append(seconds)
            for kind in observed_kinds:
                self.assertEqual(live_output.getvalue().count(f"Alpha: {kind}"), 1,
                    "follow must print new steps before sleeping and keep old steps once")
            if len(polls) == 1:
                return  # An unchanged file on the next poll must not repeat the pause.
            self.assertTrue(stages, "follow never finishes after the restart")
            progress = stages.pop(0)
            observed_kinds.append(progress["kind"])
            def change(current: record.SessionShutdown) -> None:
                current["progress"] = progress
            _ = record.update_session(LOGIN, saved["session"]["session_id"], change)
        with patch.object(time, "sleep", side_effect=advance):
            status, output = self.follow_request(live_output)
        self.assertEqual(status, 0)
        self.assertEqual(polls, [1, 1, 1, 1, 1, 1])
        self.assertIn("14:49:10 Alpha: waiting pause sent", output)
        for kind in ("waiting", "ready", "stopped", "already gone", "restarting", "restarted"):
            self.assertEqual(output.count(f"Alpha: {kind}"), 1, output)
        self.assertNotIn("unrelated", output)
        self.assertTrue(output.rstrip().endswith("all 1 back on claude3"), output)

    def test_follow_waits_for_every_session_and_reports_total_back_on_target(self) -> None:
        first = session_shutdown(record.SessionRestarted(kind="restarted", at=OCTOBER), "first")
        second = session_shutdown(record.SessionWaiting(kind="waiting"), "second")
        for saved in (first, second):
            saved["target"] = "claude 3"
            saved["session"]["name"] = saved["session"]["session_id"]
            self.assertTrue(record.create_session(saved))
        record.archive_session(LOGIN, "first")
        def complete(_seconds: float) -> None:
            def restarted(current: record.SessionShutdown) -> None:
                current["progress"] = record.SessionRestarted(kind="restarted", at=OCTOBER)
            _ = record.update_session(LOGIN, "second", restarted)
            record.archive_session(LOGIN, "second")
        with patch.object(time, "sleep", side_effect=complete) as polls:
            status, output = self.follow_request()
        self.assertEqual(status, 0)
        self.assertEqual(polls.call_count, 1)
        self.assertEqual(output.count("first: restarted"), 1)
        self.assertEqual(output.count("second: restarted"), 1)
        self.assertTrue(output.rstrip().endswith("all 2 back on claude3"), output)

    def test_follow_finds_completed_request_in_history_before_starting(self) -> None:
        for kind in ("stopped", "already gone", "restarted"):
            with self.subTest(kind=kind):
                self.clear_state()
                progress: record.SessionProgress
                if kind == "restarted":
                    progress = record.SessionRestarted(kind="restarted", at=OCTOBER)
                elif kind == "already gone":
                    progress = record.SessionAlreadyGone(kind="already gone", at=OCTOBER)
                else:
                    progress = record.SessionStopped(kind="stopped", at=OCTOBER)
                saved = session_shutdown(progress)
                saved["then"] = "restart" if kind == "restarted" else "stop"
                self.assertTrue(record.create_session(saved))
                record.archive_session(LOGIN, saved["session"]["session_id"])
                with patch.object(time, "sleep", side_effect=AssertionError("completed request waits")):
                    status, output = self.follow_request()
                self.assertEqual(status, 0)
                self.assertIn(f"alpha: {kind}", output)
                self.assertIn("all 1 back on" if kind == "restarted" else "all 1 stopped", output)

    def test_follow_keeps_trail_when_record_is_archived_between_polls(self) -> None:
        saved = session_shutdown(record.SessionWaiting(kind="waiting"))
        saved["then"] = "stop"
        self.assertTrue(record.create_session(saved))
        def finish(_seconds: float) -> None:
            def stopped(current: record.SessionShutdown) -> None:
                current["progress"] = record.SessionStopped(kind="stopped", at=OCTOBER)
            _ = record.update_session(LOGIN, saved["session"]["session_id"], stopped)
            record.archive_session(LOGIN, saved["session"]["session_id"])
        with patch.object(time, "sleep", side_effect=finish) as polls:
            status, output = self.follow_request()
        self.assertEqual(status, 0)
        self.assertEqual(polls.call_count, 1)
        self.assertEqual(output.count("alpha: waiting"), 1)
        self.assertEqual(output.count("alpha: stopped"), 1)
        self.assertTrue(output.rstrip().endswith("all 1 stopped"))

    def test_follow_counts_taken_out_sessions_while_waiting_for_restart(self) -> None:
        removed = session_shutdown(record.SessionWaiting(kind="waiting"), "removed")
        removed["session"]["name"] = "Removed"
        remaining = session_shutdown(record.SessionWaiting(kind="waiting"), "remaining")
        remaining["target"] = "claude 3"
        for saved in (removed, remaining):
            self.assertTrue(record.create_session(saved))
        polls = 0
        output = io.StringIO()
        def advance(_seconds: float) -> None:
            nonlocal polls
            polls += 1
            if polls == 1:
                _ = record.withdraw_session(LOGIN, "removed", lambda saved: None)
            elif polls == 2:
                self.assertEqual(output.getvalue().count("Removed: taken out"), 1)
            elif polls == 3:
                def restarted(current: record.SessionShutdown) -> None:
                    current["progress"] = record.SessionRestarted(kind="restarted", at=OCTOBER)
                _ = record.update_session(LOGIN, "remaining", restarted)
                record.archive_session(LOGIN, "remaining")
            else:
                self.fail("follow did not finish after withdrawal and restart")
        with patch.object(time, "sleep", side_effect=advance):
            status, text = self.follow_request(output)
        self.assertEqual(status, 0)
        self.assertEqual(text.count("Removed: taken out"), 1)
        self.assertTrue(text.rstrip().endswith("all 2 done: 1 back on claude3, 1 taken out"), text)

    def test_follow_finishes_when_every_session_was_taken_out_before_start(self) -> None:
        progress: list[record.SessionProgress] = [
            record.SessionWaiting(kind="waiting"),
            record.SessionReadyToStop(kind="ready", at=OCTOBER),
            record.SessionHeldByUsageLimit(kind="held by usage limit", at=OCTOBER),
            record.SessionReaping(kind="reaping", at=OCTOBER, because="account out of usage", detail="weekly limit"),
            record.SessionStopFailed(kind="stop failed", at=OCTOBER, reason="still live"),
        ]
        for index, state in enumerate(progress):
            identity = f"removed-{index}"
            saved = session_shutdown(state, identity)
            saved["session"]["name"] = identity
            self.assertTrue(record.create_session(saved))
            if state["kind"] == "stop failed":
                _ = record.withdraw_failed_stop(LOGIN, identity, lambda saved: True, lambda saved: None)
            else:
                _ = record.withdraw_session(LOGIN, identity, lambda saved: None)
        with patch.object(time, "sleep", side_effect=AssertionError("taken out request waits")):
            status, text = self.follow_request()
        self.assertEqual(status, 0)
        for index in range(len(progress)):
            self.assertEqual(text.count(f"removed-{index}: taken out"), 1)
        self.assertTrue(text.rstrip().endswith("all 5 done: 5 taken out"), text)

    def test_follow_reads_each_history_file_once_across_polls(self) -> None:
        unrelated = session_shutdown(record.SessionWaiting(kind="waiting"), "unrelated")
        unrelated["request_id"] = "another-request"
        self.assertTrue(record.create_session(unrelated))
        record.archive_session(LOGIN, "unrelated")
        archived = next((self.state / LOGIN / "history").glob("*.json"))
        self.assertTrue(record.create_session(session_shutdown(record.SessionWaiting(kind="waiting"))))
        read_text = Path.read_text
        history_reads = 0
        def read(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
            nonlocal history_reads
            if path == archived:
                history_reads += 1
                if history_reads > 1:
                    raise PermissionError("immutable history must not be read twice")
            return read_text(path, encoding=encoding, errors=errors)
        polls = 0
        def advance(_seconds: float) -> None:
            nonlocal polls
            polls += 1
            if polls == 3:
                def stopped(current: record.SessionShutdown) -> None:
                    current["then"] = "stop"
                    current["progress"] = record.SessionStopped(kind="stopped", at=OCTOBER)
                _ = record.update_session(LOGIN, "session-123", stopped)
                record.archive_session(LOGIN, "session-123")
            elif polls > 3:
                self.fail("follow did not read the newly archived result")
        with patch.object(Path, "read_text", read), patch.object(time, "sleep", side_effect=advance):
            status, text = self.follow_request()
        self.assertEqual(status, 0)
        self.assertEqual(history_reads, 1)
        self.assertTrue(text.rstrip().endswith("all 1 stopped"), text)

    def test_follow_needing_user_names_account_session_and_terminal(self) -> None:
        for progress in (
            record.SessionStopFailed(kind="stop failed", at=OCTOBER, reason="still alive"),
            record.SessionRestartFailed(kind="restart failed", at=OCTOBER, reason="launch refused"),
            record.ProcessIdentityLost(kind="process identity lost", at=OCTOBER),
        ):
            with self.subTest(kind=progress["kind"]):
                self.clear_state()
                self.assertTrue(record.create_session(session_shutdown(progress)))
                sibling = session_shutdown(record.SessionReadyToStop(kind="ready", at=OCTOBER), "z-sibling")
                sibling["session"]["name"] = "Sibling"
                sibling["trail"] = [
                    {"at": OCTOBER, "kind": "waiting", "detail": "pause sent"},
                    {"at": OCTOBER, "kind": "ready", "detail": "checkpoint saved"},
                ]
                self.assertTrue(record.create_session(sibling))
                with patch.object(time, "sleep", side_effect=AssertionError("user action waits")):
                    status, output = self.follow_request()
                self.assertEqual(status, 1)
                self.assertEqual(output.count("14:49:10 Sibling: waiting pause sent"), 1)
                self.assertEqual(output.count("14:49:10 Sibling: ready checkpoint saved"), 1)
                self.assertLess(output.index("Sibling: ready"), output.index("in a terminal on natedev"))
                self.assertIn(progress["kind"], output)
                self.assertIn("in a terminal on natedev", output)
                self.assertIn("~/.claude/scripts/lib/py ~/.claude/scripts/shutdown/shutdown.py", output)
                self.assertIn(LOGIN, output)
                self.assertIn("--only session-123", output)

    def test_follow_detach_leaves_shutdown_running_and_prints_resume_command(self) -> None:
        self.assertTrue(record.create_session(session_shutdown(record.SessionWaiting(kind="waiting"))))
        before = record.session_records(LOGIN)
        with patch.object(time, "sleep", side_effect=KeyboardInterrupt):
            status, output = self.follow_request()
        self.assertEqual(status, 130)
        self.assertEqual(record.session_records(LOGIN), before)
        self.assertIn("the shutdown keeps running", output)
        self.assertIn("in this terminal: ~/.claude/scripts/lib/py ~/.claude/scripts/shutdown/shutdown.py follow", output)
        self.assertIn(LOGIN, output)
        self.assertIn("--request request-one", output)

    def test_follow_alerted_holdout_shows_stop_action_and_exits_one(self) -> None:
        saved = session_shutdown(record.SessionWaiting(kind="waiting"))
        saved["alerted_at"] = OCTOBER
        self.assertTrue(record.create_session(saved))
        with patch.object(time, "sleep", side_effect=AssertionError("alerted holdout waits")):
            status, output = self.follow_request()
        self.assertEqual(status, 1)
        self.assertIn("has not answered the pause", output)
        self.assertIn("in a terminal on natedev", output)
        self.assertIn(f"now {LOGIN} --here --only session-123", output)

    def test_quota_notice_and_import_modes_preserve_module_package_metadata(self) -> None:
        quota = Path(__file__).resolve().parents[1] / "whoami/quota_alert.py"
        for mode in ("script", "quota_alert", "scripts.whoami.quota_alert"):
            with self.subTest(mode=mode):
                command = [sys.executable, "-Wdefault"]
                if mode == "script":
                    command.extend((str(quota), "notice"))
                else:
                    command.extend(("-c", "import importlib, sys; "
                        + f"sys.path.insert(0, {str(quota.parent)!r}); "
                        + f"sys.path.insert(0, {str(quota.parents[2])!r}); "
                        + f"module = importlib.import_module({mode!r}); "
                        + "assert module.__package__ == module.__spec__.parent"))
                result = subprocess.run(command, capture_output=True, text=True, check=False,
                    timeout=10, cwd=self.root,
                    env={**self.environment, "XDG_STATE_HOME": str(self.root / "state")})
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(result.stderr, "")
                self.assertEqual(result.stdout, "")

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

    def conflicting_legacy_record(self, conflict: str) -> str:
        self.clear_state()
        old = shutdown_record("settling", session_id="id/one")
        directory = self.state / LOGIN
        if conflict == "session ids":
            current = session_shutdown(record.SessionWaiting(kind="waiting"), "id-one")
            self.assertTrue(record.create_session(current))
        write_old(old)
        (directory / "lock").touch()
        if conflict == "history names":
            partial = directory / "partial"
            partial.mkdir()
            _ = (partial / "record.json").write_text(json.dumps(old))
            return f"{directory / 'history'} has conflicting legacy history names"
        if conflict == "existing target":
            history = directory / "history"
            history.mkdir()
            target = history / f"{OCTOBER}-record.json"
            _ = target.write_text("retained history")
            return f"{target} already exists"
        if conflict == "session names":
            other = shutdown_record("settling", session_id="id-one")
            old["entries"].extend(other["entries"])
            write_old(old)
            return f"{directory / 'sessions'} has conflicting session names"
        return f"{directory / 'sessions' / 'id-one.json'} has conflicting session ids 'id-one' and 'id/one'"

    def store_contents(self) -> dict[Path, bytes]:
        return {path.relative_to(self.state): path.read_bytes()
                for path in self.state.rglob("*") if path.is_file()}

    def test_migration_conflicts_print_one_error_and_leave_each_requested_verb_untouched(self) -> None:
        for conflict in ("history names", "existing target", "session ids", "session names"):
            for verb in ("down", "cancel", "now", "conduct"):
                with self.subTest(conflict=conflict, verb=verb):
                    diagnostic = self.conflicting_legacy_record(conflict)
                    before = self.store_contents()
                    directories = sorted(path.relative_to(self.state) for path in self.state.rglob("*") if path.is_dir())
                    output, error = io.StringIO(), io.StringIO()
                    with patch.object(settle, "_warm", return_value=True), patch.object(settle, "send_message") as sender, patch.object(settle, "_launch_conductor") as conductor, redirect_stdout(output), redirect_stderr(error):
                        result = shutdown.main([verb, LOGIN, "--here"])
                    self.assertEqual(result, 3)
                    self.assertEqual(output.getvalue(), "")
                    self.assertEqual(error.getvalue(), f"shutdown: {diagnostic}\n")
                    self.assertNotIn("Traceback", error.getvalue())
                    self.assertEqual(self.store_contents(), before)
                    self.assertEqual(sorted(path.relative_to(self.state) for path in self.state.rglob("*") if path.is_dir()), directories)
                    sender.assert_not_called()
                    conductor.assert_not_called()

    def test_ready_filename_conflict_prints_one_error_without_changing_records(self) -> None:
        _ = self.conflicting_legacy_record("session ids")
        before = self.store_contents()
        report = inventory.Inventory(machine="natedev", login=LOGIN, label=LABEL,
                                     sessions=[unit_session("id/one")], unattributed=[])
        output, error = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "id/one"}), patch.object(settle, "run_inventory", return_value=report), patch.object(settle, "blocker", return_value=record.ReadinessClear(kind="clear")), redirect_stdout(output), redirect_stderr(error):
            result = shutdown.main(["ready", "--where", "waiting for the user"])
        self.assertEqual(result, 3)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(error.getvalue(), f"shutdown: {self.state / LOGIN / 'sessions' / 'id-one.json'} is used by a different session id\n")
        self.assertEqual(self.store_contents(), before)

    def test_status_reads_filename_conflict_without_migrating_records(self) -> None:
        _ = self.conflicting_legacy_record("session ids")
        before = self.store_contents()
        report = inventory.Inventory(machine="natedev", login=LOGIN, label=LABEL,
                                     sessions=[], unattributed=[])
        output, error = io.StringIO(), io.StringIO()
        with patch.object(shutdown, "inventory", return_value=report), patch.object(shutdown, "conductor_status", return_value=record.ConductorNotRunning()), redirect_stdout(output), redirect_stderr(error):
            result = shutdown.main(["status", LOGIN, "--here"])
        self.assertEqual(result, 0)
        self.assertEqual(error.getvalue(), "")
        self.assertEqual(output.getvalue().count("  alpha: waiting"), 2)
        self.assertIn(f"{LABEL} · {LOGIN}\n", output.getvalue())
        self.assertNotIn("Traceback", output.getvalue())
        self.assertEqual(self.store_contents(), before)

    def test_down_to_passes_target_through_the_cli_to_settle(self) -> None:
        selected = Account("claude", LOGIN, LABEL)
        report = inventory.Inventory(machine="natedev", login=LOGIN, label=LABEL,
            sessions=[unit_session("move-id")], unattributed=[])
        with (
            patch.object(shutdown, "named_claude_account", return_value=selected),
            patch.object(settle, "down", return_value=0) as down,
            patch.object(shutdown, "resolve_sessions", return_value=shutdown.ResolvedSessions(("move-id",), report)),
        ):
            self.assertEqual(shutdown.main(["down", LABEL, "--here", "--only", "move-id",
                "--restart", "--to", "claude3"]), 0)
        down.assert_called_once_with(selected, True, ("move-id",), then="restart", alerts="user",
            requested_by=None, request_id="", target="claude3", why="", report=report, origin=accounts.HandOrigin(), move_id="")

    def test_down_to_passes_custom_move_reason_through_the_cli_to_settle(self) -> None:
        selected = Account("claude", LOGIN, LABEL)
        report = inventory.Inventory(machine="natedev", login=LOGIN, label=LABEL,
            sessions=[unit_session("move-id")], unattributed=[])
        with (
            patch.object(shutdown, "named_claude_account", return_value=selected),
            patch.object(settle, "down", return_value=0) as down,
            patch.object(shutdown, "resolve_sessions", return_value=shutdown.ResolvedSessions(("move-id",), report)),
        ):
            self.assertEqual(shutdown.main(["down", LABEL, "--here", "--only", "move-id",
                "--restart", "--to", "claude3", "--why", "weekly limit reached"]), 0)
        down.assert_called_once_with(selected, True, ("move-id",), then="restart", alerts="user",
            requested_by=None, request_id="", target="claude3", why="weekly limit reached", report=report, origin=accounts.HandOrigin(), move_id="")

    def test_down_restart_passes_the_selected_scope_and_intent(self) -> None:
        selected = Account("claude", LOGIN, LABEL)
        report = inventory.Inventory(machine="natedev", login=LOGIN, label=LABEL,
            sessions=[unit_session("unit-a"), unit_session("unit-b")], unattributed=[])
        with (
            patch.object(shutdown, "named_claude_account", return_value=selected),
            patch.object(settle, "down", return_value=0) as down,
            patch.object(shutdown, "resolve_sessions", return_value=shutdown.ResolvedSessions(("unit-a", "unit-b"), report)),
        ):
            self.assertEqual(shutdown.main([
                "down", LABEL, "--restart", "--only", "unit-a,unit-b,unit-a", "--here",
            ]), 0)
        down.assert_called_once_with(
            selected, True, ("unit-a", "unit-b"), then="restart", alerts="user", requested_by=None, request_id="", target="", why="", report=report, origin=accounts.HandOrigin(), move_id=""
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
        report = inventory.Inventory(machine="natedev", login=LOGIN, label=LABEL, sessions=[], unattributed=[])
        with (
            patch.object(shutdown, "named_claude_account", return_value=selected),
            patch.object(settle, "down", return_value=0) as down,
            patch.object(shutdown, "inventory", return_value=report),
        ):
            self.assertEqual(shutdown.main(["down", LABEL]), 0)
        down.assert_called_once_with(selected, False, (), then="stop", alerts="user", requested_by=None, request_id="", target="", why="", report=report, origin=accounts.HandOrigin(), move_id="")



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

    def test_held_sessions_includes_owners_and_workers_sorted_once(self) -> None:
        saved = session_shutdown(record.SessionWaiting(kind="waiting"), "z-owner")
        owner = saved["session"]
        saved["workers"] = [inventory.WorkerSession(
            session_id="a-worker", pid=23456, proc_start="worker-start", name="worker",
            cwd=owner["cwd"], status="idle", updated_at=0, probe=False, model=owner["model"], checkout=owner["checkout"],
            run_dirs=["/tmp/run"], codex_servers=[], timers=[], kind="worker",
            host=inventory.UnknownHost(kind="unknown"),
            owner=inventory.DirectorOwner(kind="director", session_id="z-owner"),
        )]
        self.assertTrue(record.create_session(saved))
        self.assertTrue(record.create_session(session_shutdown(record.SessionStopped(kind="stopped", at=OCTOBER), "m-owner")))
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(shutdown.main(["held-sessions"]), 0)
        self.assertEqual(output.getvalue(), "a-worker\nm-owner\nz-owner\n")

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
            "test-job: skipped: claude2 is held by a shutdown "
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
        report = inventory.Inventory(machine="natedev", login=LOGIN, label=LABEL, sessions=[], unattributed=[])
        with patch.object(shutdown, "named_claude_account", return_value=selected), patch.object(settle, "down", return_value=0) as down, patch.object(shutdown, "inventory", return_value=report):
            self.assertEqual(shutdown.main(["down", LOGIN, "--here", "--restart", "--no-alert", "--requested-by", "requester", "--request-id", "same-request"]), 0)
        down.assert_called_once_with(selected, True, (), then="restart", alerts="none", requested_by="requester", request_id="same-request", target="", why="", report=report, origin=accounts.HandOrigin(), move_id="")

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
        _ = self.enterContext(patch.object(sessions, "remote_control", return_value="active"))
        self.owners = []
        for identity, name in (("alpha-id", "Alpha"), ("beta-id", "Beta"), ("gamma-id", "Gamma")):
            owner = unit_session(identity)
            owner["name"] = name
            self.owners.append(owner)
        selected = Account("claude", LOGIN, LABEL)
        _ = self.enterContext(patch.object(shutdown, "named_claude_account", return_value=selected))
        _ = self.enterContext(patch.object(restart_work, "named_claude_account", return_value=selected))
        report = inventory.Inventory(machine="natedev", login=LOGIN, label=LABEL, sessions=self.owners, unattributed=[])
        def listed(_login: str) -> list[inventory.ListedSession]:
            return [inventory.ListedSession(owner["session_id"], owner["pid"], owner["proc_start"],
                owner["name"], owner["kind"], self.root / ".claude", LOGIN,
                owner["host"]["production"] if owner["kind"] == "unit" else "", "")
                for owner in self.owners]
        _ = self.enterContext(patch.object(shutdown, "listing", side_effect=listed))
        def observed(login: str, only: Iterable[str] = ()) -> inventory.Inventory:
            self.assertEqual(login, LOGIN)
            ids = tuple(only)
            return {**report, "sessions": [owner for owner in self.owners if not ids or owner["session_id"] in ids]}
        _ = self.enterContext(patch.object(shutdown, "inventory", side_effect=observed))
        _ = self.enterContext(patch.object(settle, "run_inventory", side_effect=observed))
        _ = self.enterContext(patch.object(settle, "inventory", side_effect=observed))
        _ = self.enterContext(patch.object(settle, "inventory", side_effect=observed))
        _ = self.enterContext(patch.object(settle, "_warm", return_value=True))
        _ = self.enterContext(patch.object(settle, "_launch_conductor"))
        _ = self.enterContext(patch.object(settle, "_retire_shutdown_messages"))
        _ = self.enterContext(patch.object(settle, "send_message", return_value=settle.MessageSent(kind="sent")))
        _ = self.enterContext(patch.object(restart_work, "_retire"))
        _ = self.enterContext(patch.object(restart_work, "_send_restart_note", return_value=settle.MessageSent(kind="sent")))
        _ = self.enterContext(patch.object(restart_work, "_live_session_ids", return_value=frozenset()))
        def confirmed(ids: frozenset[str], _timeout: float) -> restart_work.RegistryConfirmation:
            return registry_confirmation(ids)
        _ = self.enterContext(patch.object(restart_work, "wait_for_confirmation", side_effect=confirmed))
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

    def test_down_exact_name_builds_one_filtered_inventory_and_keeps_owned_workers(self) -> None:
        worker = cast(inventory.WorkerSession, cast(object, {
            **unit_session("owned-worker"), "kind": "worker", "name": "Worker",
            "host": inventory.UnknownHost(kind="unknown"),
            "owner": inventory.DirectorOwner(kind="director", session_id="beta-id"),
        }))
        self.owners.append(worker)
        def filtered(login: str, *, only: frozenset[str]) -> inventory.Inventory:
            self.assertEqual(login, LOGIN)
            self.assertEqual(only, frozenset({"beta-id"}))
            return inventory.Inventory(machine="natedev", login=LOGIN, label=LABEL,
                sessions=[self.owners[1], worker], unattributed=[])
        with (patch.object(shutdown, "inventory", side_effect=filtered) as read,
              patch.object(settle, "run_inventory", side_effect=AssertionError("down inventories twice")),
              patch.object(settle, "inventory", side_effect=AssertionError("begin inventories twice"))):
            status, _, error = self.action("down", "Beta")
        self.assertEqual((status, error), (0, ""))
        read.assert_called_once_with(LOGIN, only=frozenset({"beta-id"}))
        saved = record.session_records(LOGIN)
        self.assertEqual([item["session"]["session_id"] for item in saved], ["beta-id"])
        self.assertEqual(saved[0]["workers"], [worker])

    def test_down_ambiguous_name_builds_no_full_inventory_and_starts_nothing(self) -> None:
        self.owners[0]["name"] = self.owners[1]["name"] = "Duplicate"
        with (patch.object(shutdown, "inventory", side_effect=AssertionError("ambiguous name builds inventory")) as read,
              patch.object(settle, "_launch_conductor") as launch):
            status, output, error = self.action("down", "Duplicate")
        self.assertEqual(status, 2)
        self.assertIn("matches more than one session", output + error)
        self.assertIn("alpha-id", output + error)
        self.assertIn("beta-id", output + error)
        read.assert_not_called()
        launch.assert_not_called()
        self.assertEqual(record.session_records(LOGIN), [])

    def test_down_without_selection_reuses_one_full_inventory(self) -> None:
        report = inventory.Inventory(machine="natedev", login=LOGIN, label=LABEL,
            sessions=self.owners, unattributed=[])
        with (patch.object(shutdown, "inventory", return_value=report) as read,
              patch.object(settle, "run_inventory", side_effect=AssertionError("begin inventories twice")),
              patch.object(settle, "inventory", side_effect=AssertionError("down inventories twice")),
              redirect_stdout(io.StringIO())):
            status = shutdown.main(["down", LOGIN, "--here"])
        self.assertEqual(status, 0)
        read.assert_called_once_with(LOGIN)
        self.assertEqual({saved["session"]["session_id"] for saved in record.session_records(LOGIN)},
            {owner["session_id"] for owner in self.owners})

    def test_only_unknown_item_exits_two_without_starting_work(self) -> None:
        before = self.save_three({"kind": "waiting"})
        with patch.object(settle, "_launch_conductor") as conductor:
            status, output, error = self.action("now", "absent")
        self.assertEqual(status, 2)
        self.assertIn("absent", output + error)
        conductor.assert_not_called()
        self.assertEqual(record.session_records(LOGIN), before)

    def test_only_restart_refuses_waiting_ready_and_stopping_with_exact_line(self) -> None:
        cases: list[record.SessionProgress] = [{"kind": "waiting"}, {"kind": "ready", "at": OCTOBER}, {"kind": "stopping", "at": OCTOBER}, record.SessionHeldByUsageLimit(kind="held by usage limit", at=OCTOBER)]
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
        self.assertEqual(output, f"Beta was not cancelled: stopping; in a terminal on natedev: ~/.claude/scripts/lib/py ~/.claude/scripts/shutdown/shutdown.py restart {LOGIN} --here --only beta-id brings it back\n")

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
            self.assertEqual(shutdown.resolve_sessions(LOGIN, ["Beta", "beta-id", "Beta"], here=True).session_ids, ("beta-id",))
        peer.assert_not_called()

    def test_resolve_local_id_does_not_wait_for_the_peer(self) -> None:
        _ = self.save_three({"kind": "waiting"})
        with patch.object(shutdown, "run_remote") as peer:
            self.assertEqual(shutdown.resolve_sessions(LOGIN, ["beta-id"]).session_ids, ("beta-id",))
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
            self.assertEqual(shutdown.resolve_sessions(LOGIN, ["Remote"]).session_ids, ("remote-id",))

    def test_resolve_name_with_unreachable_peer_uses_local_and_warns(self) -> None:
        _ = self.save_three({"kind": "waiting"})
        error = io.StringIO()
        with patch.object(shutdown, "run_remote", return_value=(255, "")), patch.object(shutdown, "other_machine", return_value="Mac"), redirect_stderr(error):
            self.assertEqual(shutdown.resolve_sessions(LOGIN, ["Beta"]).session_ids, ("beta-id",))
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
            with self.subTest(verb=verb), patch.object(shutdown, "run_remote") as selection_peer, patch.object(settle, "run_remote") as action_peer, patch.object(restart_work, "run_remote") as restart_peer, patch.object(settle, "inventory", return_value=inventory.Inventory(machine="natedev", login=LOGIN, label=LABEL, sessions=[self.owners[1]], unattributed=[])) as observed, patch.object(settle, "_launch_conductor") as conductor:
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
        push = cast(RenderedPush, alert.call_args.args[0])
        self.assertEqual(push.id, "shutdown.restart_failed")
        self.assertEqual(push.values["stopped"], "One session")
        self.assertIn("window launch failed", output)

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

    def test_selected_now_forces_usage_limit_held_session_and_names_it(self) -> None:
        before = self.save_three(record.SessionHeldByUsageLimit(kind="held by usage limit", at=OCTOBER))
        with patch.object(settle, "_launch_conductor") as conductor:
            self.assertEqual(self.action("now", "beta-id"), (0, "Beta: stopping now\n", ""))
        conductor.assert_called_once_with(LOGIN, LABEL)
        latest = record.find_session(LOGIN, "beta-id")
        assert latest is not None
        self.assertEqual(latest["force"], "now")
        self.assertEqual(record.find_session(LOGIN, "alpha-id"), before[0])
        self.assertEqual(record.find_session(LOGIN, "gamma-id"), before[2])

    def test_status_stop_now_action_accepts_reaping_session_by_name(self) -> None:
        progress = record.SessionReaping(kind="reaping", at=OCTOBER,
            because="account out of usage", detail="weekly limit")
        before = self.save_three(progress)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(shutdown.main(["status", LOGIN, "--here"]), 0)
        self.assertIn(f"  stop now: in a terminal on natedev: ~/.claude/scripts/lib/py ~/.claude/scripts/shutdown/shutdown.py now {LOGIN} --here --only alpha-id,beta-id,gamma-id · Alpha, Beta, Gamma\n", output.getvalue())
        with patch.object(settle, "_launch_conductor") as conductor:
            self.assertEqual(self.action("now", "Beta"), (0, "Beta: stopping now\n", ""))
        conductor.assert_called_once_with(LOGIN, LABEL)
        latest = record.find_session(LOGIN, "beta-id")
        assert latest is not None
        self.assertEqual(latest["force"], "now")
        self.assertEqual(latest["progress"], progress)
        self.assertEqual(record.find_session(LOGIN, "alpha-id"), before[0])
        self.assertEqual(record.find_session(LOGIN, "gamma-id"), before[2])

    def test_selected_cancel_takes_usage_limit_held_session_out_and_names_it(self) -> None:
        before = self.save_three(record.SessionHeldByUsageLimit(kind="held by usage limit", at=OCTOBER))
        self.assertEqual(self.action("cancel", "beta-id"), (0, "Beta: taken out\n", ""))
        self.assertIsNone(record.find_session(LOGIN, "beta-id"))
        self.assertEqual(record.find_session(LOGIN, "alpha-id"), before[0])
        self.assertEqual(record.find_session(LOGIN, "gamma-id"), before[2])

    def test_status_actions_include_usage_limit_held_session(self) -> None:
        _ = self.save_three(record.SessionHeldByUsageLimit(kind="held by usage limit", at=OCTOBER))
        output = io.StringIO()
        with patch.object(settle, "_showing_form", return_value=True), patch.object(settle, "merge_in_progress", return_value=False), redirect_stdout(output):
            self.assertEqual(shutdown.main(["status", LOGIN, "--here"]), 0)
        self.assertIn(f"  take out: in a terminal on natedev: ~/.claude/scripts/lib/py ~/.claude/scripts/shutdown/shutdown.py cancel {LOGIN} --here --only alpha-id,beta-id,gamma-id · Alpha, Beta, Gamma\n", output.getvalue())
        self.assertIn(f"  stop now: in a terminal on natedev: ~/.claude/scripts/lib/py ~/.claude/scripts/shutdown/shutdown.py now {LOGIN} --here --only alpha-id,beta-id,gamma-id · Alpha, Beta, Gamma\n", output.getvalue())

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
            self.assertEqual(shutdown.resolve_sessions(LOGIN, ["remote-id"]).session_ids, ("remote-id",))
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

    def test_up_json_returns_one_object_with_structured_refusals(self) -> None:
        _ = self.save_three({"kind": "waiting"})
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(shutdown.main(["up", LOGIN, "--here", "--json", "--only", "beta-id"]), 1)
        result = restart_work.parse_peer_result(output.getvalue())
        self.assertEqual(result["outcome"], "nothing")
        self.assertEqual(result["lines"], ["Beta is waiting, not stopped; nothing to restart"])
        self.assertEqual(cast(dict[str, object], json.loads(output.getvalue()))["no_remote_control"], [])
        self.assertEqual(result["refused"], [{"kind": "not stopped", "session_id": "beta-id", "name": "Beta", "progress": "waiting"}])

    def test_up_json_keeps_remote_control_quiet_and_emits_legacy_empty_key(self) -> None:
        _ = self.save_three({"kind": "stopped", "at": OCTOBER})
        def quiet(current: record.SessionShutdown) -> None:
            current["alerts"] = "none"
        _ = record.update_session(LOGIN, "beta-id", quiet)
        with patch.object(restart_work, "wait_for_remote_control", return_value=frozenset()), patch.object(remote_control, "launch_fixer") as fixer, redirect_stdout(output := io.StringIO()):
            self.assertEqual(shutdown.main(["up", LOGIN, "--here", "--json", "--only", "beta-id"]), 0)
        result = restart_work.parse_peer_result(output.getvalue())
        self.assertFalse(any("Remote Control" in line for line in result["lines"]))
        fixer.assert_called_once()
        self.assertEqual(fixer.call_args.args[0], "beta-id")
        self.assertEqual(fixer.call_args.args[-2:], ("alpha unit", "Beta"))
        self.assertEqual(cast(dict[str, object], json.loads(output.getvalue()))["no_remote_control"], [])
        self.assertIsNone(record.find_session(LOGIN, "beta-id"))



if __name__ == "__main__":
    _ = unittest.main()
