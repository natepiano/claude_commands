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
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Literal, cast, final, override
from unittest.mock import patch

from test_support import ShutdownTestCase
import inventory
import launch_permission
import record
import restart as restart_work
import shutdown
import settle
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
        requested_by=old["requested_by"], hold=hold,
        then="restart", force="wait for ready", alerts="user",
        session=item["session"], seats=[], timers=[],
        settle_message=item["settle_message"], where=item["where"],
        progress=progress, stop_issues=[],
    )


def account_hold() -> record.AccountHold:
    return record.AccountHold(
        login=LOGIN, label=LABEL, machine="natedev", requested_at=OCTOBER,
        requested_by=record.FromTerminal(kind="terminal"), then="restart",
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

    def test_down_restart_passes_the_selected_scope_and_intent(self) -> None:
        selected = Account("claude", LOGIN, LABEL)
        with (
            patch.object(shutdown, "named_claude_account", return_value=selected),
            patch.object(settle, "down", return_value=0) as down,
        ):
            self.assertEqual(shutdown.main([
                "down", LABEL, "--restart", "--only", "unit-a,unit-b,unit-a", "--here",
            ]), 0)
        down.assert_called_once_with(
            selected, True, ("unit-a", "unit-b"), then="restart"
        )

    def test_internal_slot_records_preserves_empty_old_writer_records(self) -> None:
        saved = shutdown_record("down")
        saved["entries"] = []
        self.write_record(saved)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(shutdown.main(["slot-records", "--json", "--here"]), 0)
        self.assertEqual(json.loads(output.getvalue()), [saved])
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(shutdown.main(["records", "--json", "--here"]), 0)
        self.assertEqual(json.loads(output.getvalue()), [])

    def test_internal_holds_preserves_empty_old_account_shutdowns(self) -> None:
        saved = shutdown_record("down")
        saved["entries"] = []
        self.write_record(saved)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(shutdown.main(["holds", "--json", "--here"]), 0)
        expected = account_hold()
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

    def test_settle_old_writer_reads_the_internal_slot_transport(self) -> None:
        saved = shutdown_record("stop partial")
        saved["machine"] = "Mac"
        with (
            patch.object(settle, "run_remote", return_value=(0, json.dumps([saved]))) as remote,
        ):
            read_peer = cast(Callable[[str], settle.RemoteRecordOutcome], getattr(settle, "_live_on_other"))
            lookup = read_peer(LOGIN)
        self.assertEqual(lookup, settle.RemoteRecordFound(kind="live", record=saved))
        remote.assert_called_once_with(["slot-records", "--json", "--here"])

    def test_conduct_keeps_the_old_peer_state_check(self) -> None:
        for peer_state in ("down", "stop partial"):
            with self.subTest(peer_state=peer_state):
                local = shutdown_record("down")
                local["then"] = "restart"
                self.write_record(local)
                peer = shutdown_record(cast(record.ShutdownState, peer_state))
                peer["then"] = "restart"
                with (
                    patch.object(settle, "conduct", return_value=0),
                    patch.object(shutdown, "run_remote", return_value=(0, json.dumps([peer]))) as remote,
                    patch.object(restart_work, "restart", return_value=0) as restart,
                ):
                    self.assertEqual(shutdown.main(["conduct", LOGIN]), 0)
                remote.assert_called_once_with(["slot-records", "--json", "--here"])
                if peer_state == "down":
                    restart.assert_called_once_with(
                        restart_work.AccountNamed(LOGIN), dry_run=False,
                        alerts=restart_work.AlertWhenNotBack(),
                    )
                else:
                    restart.assert_not_called()

    def test_down_without_restart_keeps_the_existing_dispatch(self) -> None:
        selected = Account("claude", LOGIN, LABEL)
        with (
            patch.object(shutdown, "named_claude_account", return_value=selected),
            patch.object(settle, "down", return_value=0) as down,
        ):
            self.assertEqual(shutdown.main(["down", LABEL]), 0)
        down.assert_called_once_with(selected, False, ())

    def test_begin_accepts_the_remote_restart_intent(self) -> None:
        with patch.object(settle, "begin") as begin:
            self.assertEqual(shutdown.main([
                "begin", LOGIN, "--requested-by", "outside", "--only", "unit-a",
                "--then", "restart",
            ]), 0)
        begin.assert_called_once_with(LOGIN, "outside", ("unit-a",), then="restart")

    def test_selected_restart_record_never_includes_unselected_sessions(self) -> None:
        selected = unit_session("unit-a")
        unselected = unit_session("unit-b")
        sessions = [selected, unselected]
        seen_scopes: list[record.ShutdownScope] = []

        def scoped_inventory(login: str, scope: record.ShutdownScope) -> inventory.Inventory:
            self.assertEqual(login, LOGIN)
            seen_scopes.append(scope)
            wanted = scope["session_ids"] if scope["kind"] == "selected" else []
            return inventory.Inventory(
                machine="natedev", login=LOGIN, label=LABEL,
                sessions=[item for item in sessions if not wanted or item["session_id"] in wanted],
                unattributed=[],
            )

        def finish(login: str, here: bool) -> int:
            self.assertEqual((login, here), (LOGIN, True))
            def stopped(current: record.ShutdownRecord) -> None:
                current["state"] = "down"
            _ = record.update(login, stopped)
            return 0

        def resume(login: str) -> int:
            found = record.find_live(login)
            if found["kind"] != "live":
                self.fail("expected the selected shutdown record")
            self.assertEqual([item["session"]["session_id"] for item in found["record"]["entries"]], ["unit-a"])
            self.assertEqual(found["record"]["scope"], record.SelectedSessions(kind="selected", session_ids=["unit-a"]))
            return 0

        with (
            patch.object(shutdown, "named_claude_account", return_value=Account("claude", LOGIN, LABEL)),
            patch.object(settle, "_warm", return_value=True),
            patch.object(settle, "run_inventory", side_effect=scoped_inventory),
            patch.object(settle, "_session_timer_snapshot", return_value=[]),
            patch.object(settle, "_launch_conductor"),
            patch.object(settle, "conduct", side_effect=finish),
            patch.object(restart_work, "up", side_effect=resume) as up,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(shutdown.main(["down", LABEL, "--here", "--only", "unit-a", "--restart"]), 0)
            self.assertEqual(shutdown.main(["conduct", LOGIN, "--here"]), 0)
        up.assert_called_once_with(LOGIN)
        self.assertTrue(all(scope == record.SelectedSessions(kind="selected", session_ids=["unit-a"]) for scope in seen_scopes))

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
                self.write_record(shutdown_record(cast(record.ShutdownState, state)))
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
                    saved["scope"] = record.SelectedSessions(kind="selected", session_ids=["session-123"])
                    record.create(saved)
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
            record.SessionRestarting(kind="restarting", at=OCTOBER),
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

    def test_a_parked_partial_passes_its_manual_unit_and_holds_new_work(self) -> None:
        self.write_record(shutdown_record(
            "restart partial",
            progress=record.SessionNeedsManualRestart(
                kind="manual restart", command="claude --resume session-123"
            ),
        ))
        _ = record.park(LOGIN)
        record.create(shutdown_record(
            "restarting", requested_at="2026-11-15T17:30:00+00:00", session_id="session-456",
        ))

        self.assertEqual(self.run_cli(LOGIN, "--restart-of", "session-123"), (1, "", ""))
        self.assertEqual(self.run_cli(LOGIN, "--restart-of", "session-456"), (1, "", ""))
        self.assertCountEqual(shutdown.held_session_ids(), ["session-123", "session-456"])
        (self.state / LOGIN / "record.json").unlink()
        status, output, error = self.run_cli(LOGIN)
        self.assertEqual((status, error), (0, ""))
        self.assertEqual(
            output,
            f"{LABEL} is held by a shutdown (since 2026-10-09 14:49 PDT)\n",
        )
        self.assertEqual(shutdown.held_session_ids(), ["session-123"])
        held = io.StringIO()
        with redirect_stdout(held):
            self.assertEqual(shutdown.main(["held-sessions"]), 0)
        self.assertEqual(held.getvalue(), "session-123\n")

    def test_the_newest_old_request_for_a_session_decides_its_restore(self) -> None:
        self.write_record(shutdown_record(
            "restart partial",
            progress=record.SessionNeedsManualRestart(
                kind="manual restart", command="claude --resume session-123"
            ),
        ))
        _ = record.park(LOGIN)
        record.create(shutdown_record("settling", requested_at="2026-11-15T17:30:00+00:00"))

        self.assertEqual(
            self.run_cli(LOGIN, "--restart-of", "session-123"),
            (0, "alpha is held by a shutdown (since 2026-11-15 09:30 PST)\n", ""),
        )

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


if __name__ == "__main__":
    _ = unittest.main()
