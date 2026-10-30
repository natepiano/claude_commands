"""Launch gating tests for shutdown records and their process-safe barrier."""

from __future__ import annotations

from scripts.whoami.note_file import note_name

import io
from datetime import datetime
import json
import os
import shlex
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
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_support import linux_process_fixture, registry_confirmation, ShutdownTestCase
import inventory
import herdr
from test_shutdown_record import LegacyRecordFixture, LegacyEntryFixture, write_old
import launch_permission
import shutdown_record as shutdown_store
import sessions
import remote_control
import shutdown
import settle
import stop as stop_work
from scripts.accounts import accounts
from push_texts import RenderedPush
import restart as restart_work
from account import Account, UnreadableAccount


# Hang guard: natedev reached load 136 on 2026-10-29; scheduling delays are not failures.
HANG_GUARD_SECONDS = 300

LOGIN = "owner@example.com"
LABEL = "claude2"
OCTOBER = "2026-10-09T21:49:10+00:00"
JANUARY = "2026-01-15T17:30:00+00:00"
PERMISSION_SCRIPT = Path(__file__).with_name("launch_permission.py")
EMPTY_HERDR_SESSIONS = json.dumps(herdr.SessionListResult(sessions=[]))


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
            session_host="herdr",
            plan=inventory.PlanFromRunRecord(kind="plan", path="/tmp/alpha.md"),
        ),
    )


def shutdown_record(
    state: str,
    *,
    requested_at: str = OCTOBER,
    progress: shutdown_store.SessionProgress | None = None,
    session_id: str = "session-123",
) -> LegacyRecordFixture:
    return LegacyRecordFixture(
        login=LOGIN,
        label=LABEL,
        machine="natedev",
        state=state,
        requested_at=requested_at,
        requested_by=shutdown_store.FromTerminal(kind="terminal"),
        scope={"kind": "all account sessions"},
        conductor={"kind": "not started"},
        force="wait for ready",
        entries=[
            LegacyEntryFixture(
                session=unit_session(session_id),
                timers=[],
                settle_message=shutdown_store.SettleMessageNotSent(kind="not sent"),
                where=shutdown_store.WhereNotSaid(kind="not said"),
                progress=progress or shutdown_store.SessionWaiting(kind="waiting"),
                stop_issues=[],
            )
        ],
        stop_issues=[],
    )


def session_shutdown(
    progress: shutdown_store.SessionProgress,
    session_id: str = "session-123",
    *,
    hold: Literal["account", "session"] = "session",
) -> shutdown_store.SessionShutdown:
    old = shutdown_record("down", progress=progress, session_id=session_id)
    item = old["entries"][0]
    return shutdown_store.SessionShutdown(
        login=LOGIN, label=LABEL, machine="natedev", requested_at=OCTOBER,
        requested_by=old["requested_by"], hold=hold, request_id="request-one",
        then="restart", force="wait for ready", alerts="user", probe=False,
        session=item["session"], workers=[], timers=[],
        settle_message=item["settle_message"], where=item["where"],
        progress=progress, trail=[], stop_issues=[],
    )


def account_hold() -> shutdown_store.AccountHold:
    return shutdown_store.AccountHold(
        login=LOGIN, label=LABEL, machine="natedev", requested_at=OCTOBER,
        requested_by=shutdown_store.FromTerminal(kind="terminal"), then="restart", request_id="request-one",
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
        # Keep this CLI fixture focused on selection; missing-folder behavior has separate restore tests.
        _ = self.enterContext(patch.object(restart_work, "_folder_exists", return_value=True))
        _ = self.enterContext(patch.object(restart_work, "_prepare_restore_server"))
        _ = self.enterContext(patch.object(herdr.Herdr, "close_restore_placeholders"))
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











    def test_snapshot_without_tmux_never_builds_a_host_binary(self) -> None:
        linux_process_fixture(self)
        proc = self.root / "proc"
        boot = proc / "sys/kernel/random/boot_id"
        boot.parent.mkdir(parents=True)
        _ = boot.write_text("boot-one\n")
        registry = self.root / "registry"
        registry.mkdir()
        calls: list[list[str]] = []

        def run(argv: list[str], **options: object) -> subprocess.CompletedProcess[str]:
            del options
            calls.append(argv)
            if argv[1:] == ["--session", "default", "session", "list", "--json"]:
                return subprocess.CompletedProcess(argv, 0, EMPTY_HERDR_SESSIONS, "")
            raise FileNotFoundError(f"no executable: {argv[0]}")

        with (patch.dict(os.environ, {
                  "HERDR_PROC": str(proc), "HERDR_BIN": "/missing/herdr",

                  "NOTIFIER_SESSIONS_DIR": str(registry),
                  "NOTIFIER_STATE_DIR": str(self.root / "notifier"),
              }), patch.object(shutil, "which", return_value=None),
              patch.object(subprocess, "run", side_effect=run)):
            observed = inventory.snapshot_inventory()
        self.assertIsInstance(observed, inventory.CompleteInventory)
        self.assertFalse(any(Path(argv[0]).name in {"tmux", "nix"} for argv in calls), calls)

    def test_elapsed_text_uses_seconds_minutes_hours_and_days(self) -> None:
        cases = ((-1, "0s"), (0, "0s"), (59, "59s"), (60, "1m"),
                 (3599, "59m"), (3600, "1h"), (86399, "23h"),
                 (86400, "1d"), (172801, "2d"))
        for seconds, expected in cases:
            with self.subTest(seconds=seconds):
                self.assertEqual(settle.elapsed_text(seconds), expected)

    def recovery(self, *identities: str) -> shutdown_store.RecoverySnapshot:
        entries: list[shutdown_store.SnapshotSession] = []
        for identity in identities:
            owner = unit_session(identity)
            owner["name"] = identity + "-name"
            entries.append(shutdown_store.SnapshotClaudeSession(agent="claude", session=owner,
                workers=[], timers=[], last_seen=OCTOBER, state="unresolved", crash="host gone"))
        saved = shutdown_store.RecoverySnapshot(machine="natedev", login=LOGIN, label=LABEL,
            taken_at=OCTOBER, sessions=entries)
        shutdown_store.write_snapshot(saved)
        return saved

    def test_partial_snapshot_names_failed_sources_and_keeps_every_recovery_file(self) -> None:
        saved = self.recovery("recoverable")
        path = self.state / "snapshots" / (LOGIN + ".json")
        before = path.read_bytes()
        with (patch.object(inventory, "snapshot_inventory", return_value=inventory.PartialInventory(
                ("session registry unreadable", "production marks unreadable"))), redirect_stderr(io.StringIO()) as error):
            self.assertEqual(shutdown.main(["snapshot"]), 1)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(shutdown_store.read_snapshot(LOGIN), saved)
        self.assertIn("session registry unreadable", error.getvalue())
        self.assertIn("production marks unreadable", error.getvalue())
        attempts = shutdown_store.read_snapshot_attempts()
        assert not isinstance(attempts, shutdown_store.NoSnapshotAttempt)
        self.assertEqual(attempts["latest"]["kind"], "failed")
        self.assertEqual(attempts["latest"].get("reasons"), ["session registry unreadable", "production marks unreadable"])
        self.assertEqual(attempts["last_success"], {"kind": "not recorded"})

    def test_snapshot_command_retains_a_crash_confirmed_entry_during_an_empty_observation(self) -> None:
        _ = self.recovery("recoverable")
        with (patch.object(inventory, "snapshot_inventory", return_value=inventory.CompleteInventory(
                "natedev", {}, {})), patch.object(settle, "record_time", return_value=JANUARY),
              patch.object(settle, "local_machine", return_value="snapshot-host"),
              redirect_stdout(io.StringIO())):
            self.assertEqual(shutdown.main(["snapshot"]), 0)
        saved = shutdown_store.read_snapshot(LOGIN)
        assert not isinstance(saved, shutdown_store.NoRecoverySnapshot)
        self.assertEqual(saved["taken_at"], JANUARY)
        attempts = shutdown_store.read_snapshot_attempts()
        assert not isinstance(attempts, shutdown_store.NoSnapshotAttempt)
        self.assertEqual(attempts["machine"], "snapshot-host")
        self.assertEqual(attempts["latest"], {"kind": "taken", "at": JANUARY, "accounts": 1})
        self.assertEqual(attempts["last_success"], {"kind": "taken", "at": JANUARY})
        self.assertEqual([(item["session"]["session_id"], item["last_seen"], item["state"])
                          for item in saved["sessions"]], [("recoverable", OCTOBER, "unresolved")])

    def test_snapshot_merge_errors_record_failure_and_keep_the_original_exit_code(self) -> None:
        for failure in (shutdown_store.InvalidRecord("invalid recovery file"), OSError("disk unavailable")):
            with (self.subTest(failure=failure),
                patch.object(inventory, "snapshot_inventory", return_value=inventory.CompleteInventory("natedev", {}, {})),
                patch.object(shutdown_store, "merge_snapshots", side_effect=failure),
                patch.object(settle, "record_time", return_value=OCTOBER),
                redirect_stderr(io.StringIO()) as error,
            ):
                self.assertEqual(shutdown.main(["snapshot"]), 1)
            attempts = shutdown_store.read_snapshot_attempts()
            assert not isinstance(attempts, shutdown_store.NoSnapshotAttempt)
            self.assertEqual(attempts["latest"], {"kind": "failed", "at": OCTOBER, "reasons": [str(failure)]})
            self.assertIn(str(failure), error.getvalue())

    def test_snapshot_outcome_write_failure_keeps_success_and_partial_exit_codes(self) -> None:
        observations = (inventory.CompleteInventory("natedev", {}, {}), inventory.PartialInventory(("registry failed",)))
        for observed, expected in zip(observations, (0, 1)):
            with (self.subTest(expected=expected),
                patch.object(inventory, "snapshot_inventory", return_value=observed),
                patch.object(shutdown_store, "record_snapshot_attempt", side_effect=OSError("outcome disk full")),
                redirect_stdout(io.StringIO()) as output,
                redirect_stderr(io.StringIO()) as error,
            ):
                self.assertEqual(shutdown.main(["snapshot"]), expected)
            self.assertIn("snapshot outcome not recorded: outcome disk full", error.getvalue())
            if expected == 0:
                self.assertIn("recovery snapshot saved for 0 accounts", output.getvalue())
            else:
                self.assertIn("snapshot incomplete: registry failed", error.getvalue())

    def test_status_without_outcome_still_includes_existing_recovery_and_explicit_json_state(self) -> None:
        _ = self.recovery("crashed")
        report = inventory.Inventory(machine="natedev", login=LOGIN, label=LABEL, sessions=[], unattributed=[])
        with patch.object(shutdown, "inventory", return_value=report), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(shutdown.main(["status", LABEL, "--here"]), 0)
        self.assertIn("recovery snapshot: no successful snapshot recorded", output.getvalue())
        self.assertIn("crashed-name: unresolved", output.getvalue())
        with patch.object(shutdown, "inventory", return_value=report), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(shutdown.main(["status", LABEL, "--here", "--json"]), 0)
        payload = cast(dict[str, object], json.loads(output.getvalue()))
        self.assertEqual(payload["snapshot"], {"kind": "no outcome recorded"})
        self.assertEqual(len(cast(list[object], payload["recovery"])), 1)

    def test_status_reports_taken_then_failed_in_user_zone_only_under_the_local_machine(self) -> None:
        local = inventory.Inventory(machine="natedev", login=LOGIN, label=LABEL, sessions=[], unattributed=[])
        remote = inventory.Inventory(machine="Mac", login=LOGIN, label=LABEL, sessions=[], unattributed=[])
        shutdown_store.record_snapshot_attempt(shutdown_store.SnapshotTaken(kind="taken", at=OCTOBER, accounts=2), "natedev")
        failed_at = "2026-10-09T21:50:10+00:00"
        for failed in (False, True):
            if failed:
                shutdown_store.record_snapshot_attempt(shutdown_store.SnapshotFailed(kind="failed", at=failed_at,
                    reasons=["registry unreadable", "host unreadable"]), "natedev")
            with (self.subTest(failed=failed),
                patch.object(shutdown, "inventory", return_value=local),
                patch.object(shutdown, "run_remote", return_value=(0, json.dumps(remote))),
                patch.object(shutdown, "_remote_shutdown_records", return_value=shutdown.AccountShutdownRecords(kind="records", records=[])),
                patch.object(shutdown, "user_zone", return_value=ZoneInfo("America/New_York")),
                patch.object(settle, "record_time", return_value=failed_at),
                redirect_stdout(io.StringIO()) as output,
            ):
                self.assertEqual(shutdown.main(["status", LABEL]), 0)
            text = output.getvalue()
            line = "recovery snapshot: last taken 2026-10-09 17:49:10 EDT (1m ago)"
            self.assertIn(line, text)
            self.assertLess(text.index(line), text.index("Mac"))
            self.assertEqual(text.count("recovery snapshot: last taken"), 1)
            if failed:
                self.assertIn("recovery snapshot failed 2026-10-09 17:50:10 EDT: registry unreadable; host unreadable", text)
            else:
                self.assertNotIn("recovery snapshot failed", text)
            with patch.object(shutdown, "inventory", return_value=local), redirect_stdout(io.StringIO()) as output:
                self.assertEqual(shutdown.main(["status", LABEL, "--here", "--json"]), 0)
            payload = cast(dict[str, object], json.loads(output.getvalue()))
            status = cast(dict[str, object], payload["snapshot"])
            self.assertEqual(status["kind"], "recorded")
            self.assertEqual(status["last_success"], {"kind": "taken", "at": OCTOBER})
            self.assertEqual(cast(dict[str, object], status["latest"])["kind"], "failed" if failed else "taken")
            self.assertEqual(payload["recovery"], [])

    def test_json_status_accepts_a_remote_report_without_snapshot_metadata(self) -> None:
        local = inventory.Inventory(machine="natedev", login=LOGIN, label=LABEL, sessions=[], unattributed=[])
        remote = inventory.Inventory(machine="Mac", login=LOGIN, label=LABEL, sessions=[], unattributed=[])
        with (patch.object(shutdown, "inventory", return_value=local),
              patch.object(shutdown, "run_remote", return_value=(0, json.dumps(remote))),
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(shutdown.main(["status", LABEL, "--json"]), 0)
        reports = cast(list[dict[str, object]], json.loads(output.getvalue()))
        self.assertEqual(reports[0]["snapshot"], {"kind": "no outcome recorded"})
        self.assertEqual(reports[0]["recovery"], [])
        self.assertEqual(reports[1], remote)

    def test_invalid_or_unreadable_snapshot_outcome_does_not_fail_status(self) -> None:
        report = inventory.Inventory(machine="natedev", login=LOGIN, label=LABEL, sessions=[], unattributed=[])
        self.state.mkdir(exist_ok=True)
        _ = (self.state / "snapshot_attempt.json").write_text("{")
        for as_json in (False, True):
            with self.subTest(as_json=as_json), patch.object(shutdown, "inventory", return_value=report), redirect_stdout(io.StringIO()) as output:
                self.assertEqual(shutdown.main(["status", LABEL, "--here", *(["--json"] if as_json else [])]), 0)
            if as_json:
                payload = cast(dict[str, object], json.loads(output.getvalue()))
                status = cast(dict[str, object], payload["snapshot"])
                self.assertEqual(status["kind"], "unreadable")
                self.assertIn("cannot be read", str(status["error"]))
            else:
                self.assertIn("recovery snapshot: outcome unreadable:", output.getvalue())
        with (patch.object(shutdown_store, "read_snapshot_attempts", side_effect=OSError("permission denied")),
              patch.object(shutdown, "inventory", return_value=report), redirect_stdout(io.StringIO()) as output):
            self.assertEqual(shutdown.main(["status", LABEL, "--here"]), 0)
        self.assertIn("outcome unreadable: permission denied", output.getvalue())

    def test_codex_status_includes_snapshot_outcome_in_text_and_json(self) -> None:
        shutdown_store.record_snapshot_attempt(shutdown_store.SnapshotFailed(kind="failed", at=OCTOBER, reasons=["Codex unreadable"]), "natedev")
        selection = shutdown.TerminalAccountSelection((Account("codex", "codex-work", "codex-work"),))
        with (patch.object(shutdown, "_terminal_accounts", return_value=selection),
              patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": ""}), redirect_stdout(io.StringIO()) as output):
            self.assertEqual(shutdown.main(["status", "--here"]), 0)
        self.assertIn("recovery snapshot: no successful snapshot recorded", output.getvalue())
        self.assertIn("Codex unreadable", output.getvalue())
        with (patch.object(shutdown, "_terminal_accounts", return_value=selection),
              patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": ""}), redirect_stdout(io.StringIO()) as output):
            self.assertEqual(shutdown.main(["status", "--here", "--json"]), 0)
        reports = cast(list[dict[str, object]], json.loads(output.getvalue()))
        status = cast(dict[str, object], reports[0]["snapshot"])
        self.assertEqual(status["last_success"], {"kind": "not recorded"})
        self.assertEqual(status["latest"], {"kind": "failed", "at": OCTOBER, "reasons": ["Codex unreadable"]})
        self.assertEqual(reports[0]["recovery"], [])

    def test_forget_removes_only_the_named_recovery_entry_and_keeps_shutdown_record(self) -> None:
        _ = self.recovery("first", "second")
        stopped = session_shutdown({"kind": "stopped", "at": OCTOBER}, "unrelated")
        self.assertTrue(shutdown_store.create_session(stopped))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(shutdown.main(["forget", "first-name"]), 0)
        saved = shutdown_store.read_snapshot(LOGIN)
        assert not isinstance(saved, shutdown_store.NoRecoverySnapshot)
        self.assertEqual([item["session"]["session_id"] for item in saved["sessions"]], ["second"])
        self.assertEqual(shutdown_store.find_session(LOGIN, "unrelated"), stopped)

    def test_forget_failed_restart_removes_it_from_status_and_bring_back(self) -> None:
        failed = session_shutdown(shutdown_store.SessionRestartFailed(kind="restart failed", at=OCTOBER,
            reason="agent exited"), "failed-id")
        failed["session"]["name"] = "leg2probe"
        sibling = session_shutdown(shutdown_store.SessionStopped(kind="stopped", at=OCTOBER), "sibling-id")
        sibling["session"]["name"] = "keep-me"
        self.assertTrue(shutdown_store.create_session(failed))
        self.assertTrue(shutdown_store.create_session(sibling))
        report = inventory.Inventory(machine="natedev", login=LOGIN, label=LABEL, sessions=[], unattributed=[])
        with patch.object(shutdown, "inventory", return_value=report), redirect_stdout(io.StringIO()) as before:
            self.assertEqual(shutdown.main(["status", LABEL, "--here"]), 0)
        self.assertIn("leg2probe", before.getvalue())
        self.assertIn("restart failed", before.getvalue())
        self.assertIn("failed-id", before.getvalue())
        with redirect_stdout(io.StringIO()) as forgotten:
            self.assertEqual(shutdown.main(["forget", "failed-id", "--account", LABEL]), 0)
        self.assertEqual(forgotten.getvalue(), "leg2probe: forgotten\n")
        with patch.object(shutdown, "inventory", return_value=report), redirect_stdout(io.StringIO()) as after:
            self.assertEqual(shutdown.main(["status", LABEL, "--here"]), 0)
        self.assertNotIn("leg2probe", after.getvalue())
        self.assertNotIn("failed-id", after.getvalue())
        self.assertNotIn("restart failed", after.getvalue())
        self.assertIn("bring back:", after.getvalue())
        self.assertIn("sibling-id", after.getvalue())
        self.assertEqual(shutdown_store.session_records(LOGIN), [sibling])

    def test_forget_one_session_removes_both_its_snapshot_and_stopped_record(self) -> None:
        _ = self.recovery("first", "second")
        stopped = session_shutdown(shutdown_store.SessionStopped(kind="stopped", at=OCTOBER), "first")
        self.assertTrue(shutdown_store.create_session(stopped))
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(shutdown.main(["forget", "first-name"]), 0)
        self.assertEqual(output.getvalue(), "first-name: forgotten\n")
        self.assertEqual(shutdown_store.session_records(LOGIN), [])
        snapshot = shutdown_store.read_snapshot(LOGIN)
        assert not isinstance(snapshot, shutdown_store.NoRecoverySnapshot)
        self.assertEqual([item["session"]["session_id"] for item in snapshot["sessions"]], ["second"])

    def test_forget_ambiguous_shutdown_name_lists_matches_without_removing_either(self) -> None:
        for identity in ("first-id", "second-id"):
            saved = session_shutdown(shutdown_store.SessionNeedsManualRestart(kind="manual restart", command="resume"), identity)
            saved["session"]["name"] = "same-name"
            self.assertTrue(shutdown_store.create_session(saved))
        before = shutdown_store.session_records(LOGIN)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as error:
            self.assertEqual(shutdown.main(["forget", "same-name"]), 2)
        self.assertIn("more than one local session", error.getvalue())
        for identity in ("first-id", "second-id"):
            self.assertIn(f"same-name ({identity})", error.getvalue())
        self.assertEqual(shutdown_store.session_records(LOGIN), before)

    def test_forget_exact_shutdown_id_wins_over_another_sessions_name(self) -> None:
        for identity, name in (("first-id", "second-id"), ("second-id", "second")):
            saved = session_shutdown(shutdown_store.SessionNeedsManualRestart(kind="manual restart", command="resume"), identity)
            saved["session"]["name"] = name
            self.assertTrue(shutdown_store.create_session(saved))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(shutdown.main(["forget", "second-id"]), 0)
        self.assertEqual([item["session"]["session_id"] for item in shutdown_store.session_records(LOGIN)], ["first-id"])

    def test_forget_does_not_remove_a_snapshot_while_its_shutdown_is_still_running(self) -> None:
        snapshot = self.recovery("active")
        active = session_shutdown(shutdown_store.SessionRestarting(kind="restarting", at=OCTOBER,
            prior=shutdown_store.SessionStopped(kind="stopped", at=OCTOBER)), "active")
        self.assertTrue(shutdown_store.create_session(active))
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as error:
            self.assertEqual(shutdown.main(["forget", "active"]), 1)
        self.assertIn("cannot forget shutdown in progress (restarting)", error.getvalue())
        self.assertEqual(shutdown_store.read_snapshot(LOGIN), snapshot)
        self.assertEqual(shutdown_store.find_session(LOGIN, "active"), active)

    def test_forget_account_selector_retires_one_of_two_identical_cross_account_ids(self) -> None:
        _ = (self.root / "notes/claude3.md").write_text("---\nlogin: third@example.invalid\n---\n")
        original = self.recovery("shared-id")
        other: shutdown_store.RecoverySnapshot = {**original, "login": "third@example.invalid", "label": "claude3"}
        shutdown_store.write_snapshot(other)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(shutdown.main(["forget", "shared-id", "--account", "claude3"]), 0)
        self.assertEqual(shutdown_store.read_snapshot(LOGIN), original)
        retired = shutdown_store.read_snapshot("third@example.invalid")
        assert not isinstance(retired, shutdown_store.NoRecoverySnapshot)
        self.assertEqual(retired["sessions"], [])

    def test_forget_one_session_id_moved_between_accounts_forgets_it_on_both(self) -> None:
        _ = (self.root / "notes/claude3.md").write_text("---\nlogin: third@example.invalid\n---\n")
        original = self.recovery("shared-id")
        shutdown_store.write_snapshot({**original, "login": "third@example.invalid", "label": "claude3"})
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(shutdown.main(["forget", "shared-id"]), 0)
        self.assertEqual(sorted(output.getvalue().splitlines()),
                         ["shared-id-name: forgotten on claude2", "shared-id-name: forgotten on claude3"])
        for login in (LOGIN, "third@example.invalid"):
            snapshot = shutdown_store.read_snapshot(login)
            assert not isinstance(snapshot, shutdown_store.NoRecoverySnapshot)
            self.assertEqual(snapshot["sessions"], [])

    def test_forget_spaced_account_label_resolves_to_the_recorded_login(self) -> None:
        _ = self.recovery("failed-id")
        failed = session_shutdown(shutdown_store.SessionRestartFailed(kind="restart failed", at=OCTOBER,
            reason="agent exited"), "failed-id")
        self.assertTrue(shutdown_store.create_session(failed))
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(shutdown.main(["forget", "--account", "claude 2", "failed-id"]), 0)
        self.assertEqual(output.getvalue(), "failed-id-name: forgotten\n")
        self.assertEqual(shutdown_store.session_records(LOGIN), [])
        snapshot = shutdown_store.read_snapshot(LOGIN)
        assert not isinstance(snapshot, shutdown_store.NoRecoverySnapshot)
        self.assertEqual(snapshot["sessions"], [])

    def test_forget_unknown_account_uses_the_status_refusal_and_keeps_the_session(self) -> None:
        saved = self.recovery("retained")
        messages: list[str] = []
        for arguments in (["status", "claude999", "--here"],
                          ["forget", "retained", "--account", "claude999"]):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as error:
                self.assertEqual(shutdown.main(arguments), 2)
            messages.append(error.getvalue())
        self.assertEqual(messages[1], messages[0])
        self.assertIn("shutdown: unknown account claude999: give a note label such as claude2, or a login", messages[1])
        self.assertEqual(shutdown_store.read_snapshot(LOGIN), saved)

    def test_snapshot_only_name_is_not_a_down_now_cancel_or_status_selection(self) -> None:
        _ = self.recovery("crashed")
        for verb in ("down", "now", "cancel", "status"):
            with (self.subTest(verb=verb), patch.object(shutdown, "listing", return_value=[]),
                  patch.object(settle, "down") as down, patch.object(settle, "now") as now,
                  patch.object(settle, "cancel") as cancel,
                  redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as error):
                self.assertEqual(shutdown.main([verb, LABEL, "--here", "--only", "crashed-name"]), 2)
            self.assertIn("matches no session", error.getvalue())
            down.assert_not_called()
            now.assert_not_called()
            cancel.assert_not_called()

    def test_restart_only_resolves_a_snapshot_only_name(self) -> None:
        _ = self.recovery("crashed")
        report = inventory.Inventory(machine="natedev", login=LOGIN, label=LABEL, sessions=[], unattributed=[])
        with (patch.object(shutdown, "listing", return_value=[]),
              patch.object(shutdown, "inventory", return_value=report),
              patch.object(restart_work, "restart", return_value=0) as restore):
            self.assertEqual(shutdown.main(["restart", LABEL, "--here", "--only", "crashed-name"]), 0)
        self.assertEqual(restore.call_args.kwargs["only"], ["crashed"])
        self.assertTrue(restore.call_args.kwargs["here"])

    def test_status_lists_unresolved_snapshot_last_seen_in_user_zone(self) -> None:
        _ = self.recovery("crashed")
        report = inventory.Inventory(machine="natedev", login=LOGIN, label=LABEL, sessions=[], unattributed=[])
        with (patch.object(shutdown, "inventory", return_value=report),
              patch.object(shutdown, "user_zone", return_value=ZoneInfo("America/New_York")),
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(shutdown.main(["status", LABEL, "--here"]), 0)
        self.assertIn("crashed-name: unresolved; last seen 2026-10-09 17:49:10 EDT", output.getvalue())

    def test_status_places_local_recovery_before_the_remote_machine_section(self) -> None:
        _ = self.recovery("crashed")
        local = inventory.Inventory(machine="natedev", login=LOGIN, label=LABEL, sessions=[], unattributed=[])
        remote = inventory.Inventory(machine="Mac", login=LOGIN, label=LABEL, sessions=[], unattributed=[])
        with (patch.object(shutdown, "_status", return_value=[local, remote]),
              patch.object(shutdown, "_remote_shutdown_records", return_value=shutdown.AccountShutdownRecords(
                  kind="records", records=[])),
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(shutdown.main(["status", LABEL]), 0)
        text = output.getvalue()
        self.assertLess(text.index("natedev"), text.index("crashed-name: unresolved"))
        self.assertLess(text.index("crashed-name: unresolved"), text.index("Mac"))
        self.assertEqual(text.count("crashed-name: unresolved"), 1)

    def test_json_status_keeps_recovery_last_seen_as_an_iso_timestamp(self) -> None:
        _ = self.recovery("crashed")
        report = inventory.Inventory(machine="natedev", login=LOGIN, label=LABEL, sessions=[], unattributed=[])
        with (patch.object(shutdown, "inventory", return_value=report),
              patch.object(shutdown, "user_zone", return_value=ZoneInfo("America/New_York")),
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(shutdown.main(["status", LABEL, "--here", "--json"]), 0)
        payload = cast(dict[str, object], json.loads(output.getvalue()))
        recovery = cast(list[dict[str, object]], payload["recovery"])
        self.assertEqual(recovery[0]["last_seen"], OCTOBER)

    def test_terminal_restart_only_finds_codex_snapshot_by_herdr_name_without_claude_account(self) -> None:
        owner = inventory.parse_session({**unit_session("code-id"), "kind": "top-level", "name": "Code writing",
            "host": {"kind": "herdr", "herdr_session": "code-writing", "pane_id": "w1:p1", "presentation": "detached"}}, "fixture")
        assert owner["kind"] == "top-level"
        codex = shutdown_store.SnapshotCodexSession(agent="codex", session=owner,
            codex=shutdown_store.CodexLaunch(home=str(self.home / ".codex"), resume=shutdown_store.CodexSessionId(
                kind="session id", session_id="code-id")), workers=[], timers=[], last_seen=OCTOBER, state="unresolved", crash="host gone")
        shutdown_store.write_snapshot(shutdown_store.RecoverySnapshot(machine="natedev", login="codex-.codex", label="codex-.codex",
            taken_at=OCTOBER, sessions=[codex]))
        with (patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": ""}),
              patch.object(shutdown, "listing", return_value=[]),
              patch.object(restart_work, "up", return_value=0) as restore,
              patch.object(restart_work, "restart") as claude_restore):
            self.assertEqual(shutdown.main(["restart", "--here", "--only", "code-writing"]), 0)
        restore.assert_called_once_with("codex-.codex", False, ("code-id",))
        claude_restore.assert_not_called()

    def test_peer_up_json_restores_snapshot_and_keeps_the_result_protocol(self) -> None:
        _ = self.recovery("crashed")
        launched = sessions.SessionRecord(pid=23456, sessionId="crashed", name="crashed-name",
            procStart="fixture-recovered-process", messagingSocketPath="", updatedAt=1, tmux="", formerNames=[])
        def confirmed(ids: frozenset[str], _timeout: float) -> restart_work.RegistryConfirmation:
            return restart_work.RegistryConfirmation((launched,) if launched["sessionId"] in ids else ())
        # Unit recovery uses its normal launcher; confirmation and launch are isolated here.
        with (patch.object(restart_work, "_session_liveness", return_value=restart_work.SessionNotLive()),
              patch.object(restart_work, "_live_session_ids", return_value=frozenset()),
              patch.object(sessions, "running_sessions", return_value=[launched]),
              patch.object(restart_work, "_retire"),
              patch.object(restart_work, "_launch_session", return_value=restart_work.SessionLaunched(herdr.PromptSubmitted(
                  datetime.fromisoformat(OCTOBER), herdr.PromptRecipientProcess(launched["pid"], launched.get("procStart", ""))))),
              patch.object(restart_work, "wait_for_confirmation", side_effect=confirmed),
              patch.object(restart_work, "remote_control_missing"),
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(shutdown.main(["up", LOGIN, "--here", "--json"]), 0)
        result = restart_work.parse_peer_result(output.getvalue())
        self.assertEqual(result["outcome"], "done")
        self.assertEqual(result["refused"], [])
        self.assertTrue(any("crashed-name: from snapshot taken" in line for line in result["lines"]))
        remaining = shutdown_store.read_snapshot(LOGIN)
        assert not isinstance(remaining, shutdown_store.NoRecoverySnapshot)
        self.assertEqual(remaining["sessions"], [])

    def test_terminal_down_without_account_starts_each_configured_account(self) -> None:
        _ = (Path(self.environment["AGENT_NOTES_DIR"]) / "claude3.md").write_text(
            "---\nlogin: third@example.com\n---\n")
        def reports(login: str) -> inventory.Inventory:
            return inventory.Inventory(machine="natedev", login=login, label=login, sessions=[], unattributed=[])
        with (patch.dict(os.environ, {**self.environment, "CLAUDE_CODE_SESSION_ID": ""}),
              patch.object(shutdown, "inventory", side_effect=reports),
              patch.object(settle, "down", return_value=0) as down,
              patch.object(shutdown, "own_claude_account", side_effect=AssertionError("terminal account inferred"))):
            self.assertEqual(shutdown.main(["down", "--here", "--restart", "--no-alert"]), 0)
        self.assertEqual([cast(Account, call.args[0]).login for call in down.call_args_list], [LOGIN, "third@example.com"])
        self.assertTrue(all(call.args[1] for call in down.call_args_list))
        self.assertTrue(all(call.kwargs["then"] == "restart" and call.kwargs["alerts"] == "none" for call in down.call_args_list))

    def test_terminal_status_without_account_emits_one_json_array_for_all_accounts(self) -> None:
        _ = (Path(self.environment["AGENT_NOTES_DIR"]) / "claude3.md").write_text(
            "---\nlogin: third@example.com\n---\n")
        def reports(login: str) -> inventory.Inventory:
            return inventory.Inventory(machine="natedev", login=login, label=login, sessions=[], unattributed=[])
        output = io.StringIO()
        with (patch.dict(os.environ, {**self.environment, "CLAUDE_CODE_SESSION_ID": ""}),
              patch.object(shutdown, "inventory", side_effect=reports),
              patch.object(shutdown, "run_remote", side_effect=AssertionError("--here read peer")),
              redirect_stdout(output)):
            self.assertEqual(shutdown.main(["status", "--here", "--json"]), 0)
        result = cast(list[dict[str, object]], json.loads(output.getvalue()))
        self.assertEqual([report["login"] for report in result], [LOGIN, "third@example.com"])

    def test_terminal_restart_releases_both_empty_accounts_after_down(self) -> None:
        third = "third@example.com"
        _ = (Path(self.environment["AGENT_NOTES_DIR"]) / "claude3.md").write_text(
            f"---\nlogin: {third}\n---\n")

        def reports(login: str) -> inventory.Inventory:
            return inventory.Inventory(machine="natedev", login=login,
                label=LABEL if login == LOGIN else "claude3", sessions=[], unattributed=[])

        with (patch.dict(os.environ, {**self.environment, "CLAUDE_CODE_SESSION_ID": ""}),
              patch.object(shutdown, "inventory", side_effect=reports),
              patch.object(settle, "_warm", return_value=True),
              patch.object(settle, "_launch_conductor"),
              patch.object(shutdown, "run_remote", side_effect=AssertionError("--here read peer")),
              redirect_stdout(io.StringIO())):
            self.assertEqual(shutdown.main(["down", "--here", "--no-alert"]), 0)
            self.assertEqual({hold["login"] for hold in shutdown_store.all_account_holds()}, {LOGIN, third})
            self.assertEqual(shutdown.main(["restart", "--here", "--no-alert"]), 0)
        self.assertEqual(shutdown_store.all_account_holds(), [])

    def test_terminal_restart_includes_a_peer_hold_without_session_records(self) -> None:
        held = account_hold()
        held["login"], held["label"], held["machine"] = "third@example.com", "claude3", "Mac"

        def peer(args: list[str]) -> tuple[int, str]:
            return 0, json.dumps([held] if args[0] == "holds" else [])

        with (patch.dict(os.environ, {**self.environment, "CLAUDE_CODE_SESSION_ID": ""}),
              patch.object(shutdown, "run_remote", side_effect=peer),
              patch.object(restart_work, "restart", return_value=0) as resume):
            self.assertEqual(shutdown.main(["restart", "--no-alert"]), 0)
        resume.assert_called_once()
        self.assertEqual(resume.call_args.args[0], restart_work.AccountNamed(held["login"]))
        self.assertFalse(resume.call_args.kwargs["here"])

    def test_terminal_restart_without_account_uses_accounts_with_records_on_either_machine(self) -> None:
        local = session_shutdown(shutdown_store.SessionStopped(kind="stopped", at=OCTOBER))
        peer = session_shutdown(shutdown_store.SessionStopped(kind="stopped", at=OCTOBER), "peer-id")
        peer["login"], peer["label"], peer["machine"] = "third@example.com", "claude3", "Mac"
        self.assertTrue(shutdown_store.create_session(local))
        with (patch.dict(os.environ, {**self.environment, "CLAUDE_CODE_SESSION_ID": ""}),
              patch.object(shutdown, "run_remote", side_effect=[(0, json.dumps([peer])), (0, "[]")]),
              patch.object(restart_work, "restart", side_effect=[1, 0]) as resume):
            self.assertEqual(shutdown.main(["restart", "--dry-run", "--no-alert"]), 1)
        self.assertEqual([call.args[0] for call in resume.call_args_list],
            [restart_work.AccountNamed(LOGIN), restart_work.AccountNamed("third@example.com")])
        self.assertTrue(all(call.kwargs["dry_run"] and not call.kwargs["here"] for call in resume.call_args_list))

    def test_terminal_restart_attempts_local_accounts_when_peer_record_discovery_fails(self) -> None:
        self.assertTrue(shutdown_store.create_session(session_shutdown(shutdown_store.SessionStopped(kind="stopped", at=OCTOBER))))
        with (patch.dict(os.environ, {**self.environment, "CLAUDE_CODE_SESSION_ID": ""}),
              patch.object(shutdown, "run_remote", return_value=(255, "peer unavailable")),
              patch.object(restart_work, "restart", return_value=0) as resume,
              redirect_stderr(io.StringIO()) as error):
            self.assertEqual(shutdown.main(["restart", "--no-alert"]), 1)
        self.assertEqual(resume.call_args.args[0], restart_work.AccountNamed(LOGIN))
        self.assertFalse(resume.call_args.kwargs["here"])
        self.assertIn("shutdown records not reached", error.getvalue())
        self.assertIn("agents restart --here", error.getvalue())

    def test_in_session_no_account_keeps_own_account_for_down_status_and_restart(self) -> None:
        selected = Account("claude", LOGIN, LABEL)
        report = inventory.Inventory(machine="natedev", login=LOGIN, label=LABEL, sessions=[], unattributed=[])
        with (patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "caller-id"}),
              patch.object(shutdown, "own_claude_account", return_value=selected),
              patch.object(shutdown, "inventory", return_value=report),
              patch.object(shutdown, "_terminal_accounts", side_effect=AssertionError("expanded accounts")),
              patch.object(settle, "down", return_value=0) as down,
              patch.object(restart_work, "restart", return_value=0) as resume,
              redirect_stdout(io.StringIO())):
            self.assertEqual(shutdown.main(["down", "--here"]), 0)
            self.assertEqual(shutdown.main(["status", "--here", "--json"]), 0)
            self.assertEqual(shutdown.main(["restart", "--here"]), 0)
        self.assertEqual(down.call_args.args[0], selected)
        self.assertEqual(resume.call_args.args[0], restart_work.AccountNotNamed())

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

    def test_a_stop_covers_this_machine_unless_the_other_machine_or_a_session_is_named(self) -> None:
        selected = Account("claude", LOGIN, LABEL)
        report = inventory.Inventory(machine="natedev", login=LOGIN, label=LABEL, sessions=[], unattributed=[])
        cases: tuple[tuple[tuple[str, ...], bool], ...] = (
            ((), True), (("--here",), True), (("--also", "mac"), False),
            (("--restart", "--to", "claude3"), False),
        )
        for arguments, here in cases:
            for verb in ("down", "now"):
                if verb == "now" and "--to" in arguments:
                    continue
                with (self.subTest(verb=verb, arguments=arguments),
                      patch.object(shutdown, "named_claude_account", return_value=selected),
                      patch.object(shutdown, "inventory", return_value=report),
                      patch.object(settle, verb, return_value=0) as stop):
                    self.assertEqual(shutdown.main([verb, LABEL, *arguments]), 0)
                    self.assertEqual(stop.call_args.args[1], here)
        with (patch.object(shutdown, "named_claude_account", return_value=selected),
              patch.object(shutdown, "resolve_sessions", return_value=shutdown.ResolvedSessions(("peer-id",), report)),
              patch.object(settle, "down", return_value=0) as down):
            self.assertEqual(shutdown.main(["down", LABEL, "--only", "peer-id"]), 0)
        self.assertFalse(down.call_args.args[1])
        for arguments in (("--also", "natedev"), ("--also", "mac", "--here")):
            errors = io.StringIO()
            with (self.subTest(arguments=arguments),
                  patch.object(shutdown, "named_claude_account", return_value=selected),
                  patch.object(settle, "down") as down, redirect_stderr(errors)):
                self.assertEqual(shutdown.main(["down", LABEL, *arguments]), 2)
            down.assert_not_called()
            self.assertIn("--also names the other machine (mac)", errors.getvalue())

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
        saved = session_shutdown(shutdown_store.SessionStopFailed(kind="stop failed", at=OCTOBER,
            reason="session or worker is not idle and ready"))
        saved["stop_issues"] = [shutdown_store.NotReadyToStop(kind="not ready to stop", at=OCTOBER,
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
        current = shutdown_store.find_session(LOGIN, "session-123")
        assert current is not None
        self.assertEqual(current["progress"], {"kind": "waiting"})

    def test_follow_prints_each_new_step_once_and_waits_for_pending_restart(self) -> None:
        saved = session_shutdown(shutdown_store.SessionWaiting(kind="waiting"))
        saved["session"]["name"] = "Alpha"
        # Legacy-spelled stored targets must print as canonical labels.
        saved["target"] = "claude 3"
        saved["trail"] = [{"at": OCTOBER, "kind": "waiting", "detail": "pause sent"}]
        self.assertTrue(shutdown_store.create_session(saved))
        unrelated = session_shutdown(shutdown_store.SessionStopFailed(kind="stop failed", at=OCTOBER, reason="other request"), "unrelated")
        unrelated["request_id"] = "another-request"
        self.assertTrue(shutdown_store.create_session(unrelated))
        stages: list[shutdown_store.SessionProgress] = [
            shutdown_store.SessionReadyToStop(kind="ready", at=OCTOBER),
            shutdown_store.SessionStopped(kind="stopped", at=OCTOBER),
            shutdown_store.SessionAlreadyGone(kind="already gone", at=OCTOBER),
            shutdown_store.SessionRestarting(kind="restarting", at=OCTOBER,
                prior=shutdown_store.SessionStopped(kind="stopped", at=OCTOBER)),
            shutdown_store.SessionRestarted(kind="restarted", at=OCTOBER),
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
            def change(current: shutdown_store.SessionShutdown) -> None:
                current["progress"] = progress
            _ = shutdown_store.update_session(LOGIN, saved["session"]["session_id"], change)
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
        first = session_shutdown(shutdown_store.SessionRestarted(kind="restarted", at=OCTOBER), "first")
        second = session_shutdown(shutdown_store.SessionWaiting(kind="waiting"), "second")
        for saved in (first, second):
            saved["target"] = "claude 3"
            saved["session"]["name"] = saved["session"]["session_id"]
            self.assertTrue(shutdown_store.create_session(saved))
        shutdown_store.archive_session(LOGIN, "first")
        def complete(_seconds: float) -> None:
            def restarted(current: shutdown_store.SessionShutdown) -> None:
                current["progress"] = shutdown_store.SessionRestarted(kind="restarted", at=OCTOBER)
            _ = shutdown_store.update_session(LOGIN, "second", restarted)
            shutdown_store.archive_session(LOGIN, "second")
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
                progress: shutdown_store.SessionProgress
                if kind == "restarted":
                    progress = shutdown_store.SessionRestarted(kind="restarted", at=OCTOBER)
                elif kind == "already gone":
                    progress = shutdown_store.SessionAlreadyGone(kind="already gone", at=OCTOBER)
                else:
                    progress = shutdown_store.SessionStopped(kind="stopped", at=OCTOBER)
                saved = session_shutdown(progress)
                saved["then"] = "restart" if kind == "restarted" else "stop"
                self.assertTrue(shutdown_store.create_session(saved))
                shutdown_store.archive_session(LOGIN, saved["session"]["session_id"])
                with patch.object(time, "sleep", side_effect=AssertionError("completed request waits")):
                    status, output = self.follow_request()
                self.assertEqual(status, 0)
                self.assertIn(f"alpha: {kind}", output)
                self.assertIn("all 1 back on" if kind == "restarted" else "all 1 stopped", output)

    def test_follow_keeps_trail_when_record_is_archived_between_polls(self) -> None:
        saved = session_shutdown(shutdown_store.SessionWaiting(kind="waiting"))
        saved["then"] = "stop"
        self.assertTrue(shutdown_store.create_session(saved))
        def finish(_seconds: float) -> None:
            def stopped(current: shutdown_store.SessionShutdown) -> None:
                current["progress"] = shutdown_store.SessionStopped(kind="stopped", at=OCTOBER)
            _ = shutdown_store.update_session(LOGIN, saved["session"]["session_id"], stopped)
            shutdown_store.archive_session(LOGIN, saved["session"]["session_id"])
        with patch.object(time, "sleep", side_effect=finish) as polls:
            status, output = self.follow_request()
        self.assertEqual(status, 0)
        self.assertEqual(polls.call_count, 1)
        self.assertEqual(output.count("alpha: waiting"), 1)
        self.assertEqual(output.count("alpha: stopped"), 1)
        self.assertTrue(output.rstrip().endswith("all 1 stopped"))

    def test_follow_counts_taken_out_sessions_while_waiting_for_restart(self) -> None:
        removed = session_shutdown(shutdown_store.SessionWaiting(kind="waiting"), "removed")
        removed["session"]["name"] = "Removed"
        remaining = session_shutdown(shutdown_store.SessionWaiting(kind="waiting"), "remaining")
        remaining["target"] = "claude 3"
        for saved in (removed, remaining):
            self.assertTrue(shutdown_store.create_session(saved))
        polls = 0
        output = io.StringIO()
        def advance(_seconds: float) -> None:
            nonlocal polls
            polls += 1
            if polls == 1:
                _ = shutdown_store.withdraw_session(LOGIN, "removed", lambda saved: None)
            elif polls == 2:
                self.assertEqual(output.getvalue().count("Removed: taken out"), 1)
            elif polls == 3:
                def restarted(current: shutdown_store.SessionShutdown) -> None:
                    current["progress"] = shutdown_store.SessionRestarted(kind="restarted", at=OCTOBER)
                _ = shutdown_store.update_session(LOGIN, "remaining", restarted)
                shutdown_store.archive_session(LOGIN, "remaining")
            else:
                self.fail("follow did not finish after withdrawal and restart")
        with patch.object(time, "sleep", side_effect=advance):
            status, text = self.follow_request(output)
        self.assertEqual(status, 0)
        self.assertEqual(text.count("Removed: taken out"), 1)
        self.assertTrue(text.rstrip().endswith("all 2 done: 1 back on claude3, 1 taken out"), text)

    def test_follow_finishes_when_every_session_was_taken_out_before_start(self) -> None:
        progress: list[shutdown_store.SessionProgress] = [
            shutdown_store.SessionWaiting(kind="waiting"),
            shutdown_store.SessionReadyToStop(kind="ready", at=OCTOBER),
            shutdown_store.SessionHeldByUsageLimit(kind="held by usage limit", at=OCTOBER),
            shutdown_store.SessionReaping(kind="reaping", at=OCTOBER, because="account out of usage", detail="weekly limit"),
            shutdown_store.SessionStopFailed(kind="stop failed", at=OCTOBER, reason="still live"),
        ]
        for index, state in enumerate(progress):
            identity = f"removed-{index}"
            saved = session_shutdown(state, identity)
            saved["session"]["name"] = identity
            self.assertTrue(shutdown_store.create_session(saved))
            if state["kind"] == "stop failed":
                _ = shutdown_store.withdraw_failed_stop(LOGIN, identity, lambda saved: True, lambda saved: None)
            else:
                _ = shutdown_store.withdraw_session(LOGIN, identity, lambda saved: None)
        with patch.object(time, "sleep", side_effect=AssertionError("taken out request waits")):
            status, text = self.follow_request()
        self.assertEqual(status, 0)
        for index in range(len(progress)):
            self.assertEqual(text.count(f"removed-{index}: taken out"), 1)
        self.assertTrue(text.rstrip().endswith("all 5 done: 5 taken out"), text)

    def test_follow_reads_each_history_file_once_across_polls(self) -> None:
        unrelated = session_shutdown(shutdown_store.SessionWaiting(kind="waiting"), "unrelated")
        unrelated["request_id"] = "another-request"
        self.assertTrue(shutdown_store.create_session(unrelated))
        shutdown_store.archive_session(LOGIN, "unrelated")
        archived = next((self.state / LOGIN / "history").glob("*.json"))
        self.assertTrue(shutdown_store.create_session(session_shutdown(shutdown_store.SessionWaiting(kind="waiting"))))
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
                def stopped(current: shutdown_store.SessionShutdown) -> None:
                    current["then"] = "stop"
                    current["progress"] = shutdown_store.SessionStopped(kind="stopped", at=OCTOBER)
                _ = shutdown_store.update_session(LOGIN, "session-123", stopped)
                shutdown_store.archive_session(LOGIN, "session-123")
            elif polls > 3:
                self.fail("follow did not read the newly archived result")
        with patch.object(Path, "read_text", read), patch.object(time, "sleep", side_effect=advance):
            status, text = self.follow_request()
        self.assertEqual(status, 0)
        self.assertEqual(history_reads, 1)
        self.assertTrue(text.rstrip().endswith("all 1 stopped"), text)

    def test_failed_restart_status_and_follow_recheck_liveness_before_offering_a_command(self) -> None:
        saved = session_shutdown(shutdown_store.SessionRestartFailed(kind="restart failed", at=OCTOBER,
            reason="restart prompt not submitted"))
        self.assertTrue(shutdown_store.create_session(saved))
        restart_work._save_prompt_outcome(saved, herdr.PromptNotSubmitted(  # pyright: ignore[reportPrivateUsage]
            "not submitted", "Claude fixture\n❯ unsent opening prompt"))
        report = inventory.Inventory(machine="natedev", login=LOGIN, label=LABEL, sessions=[], unattributed=[])
        for liveness in (restart_work.SessionLive(), restart_work.SessionNotLive(),
                         restart_work.SessionLivenessUnknown("scratch registry unreadable")):
            with self.subTest(liveness=liveness):
                with (patch.object(restart_work, "_session_liveness", return_value=liveness),
                      patch.object(restart_work, "_owner_liveness", return_value=liveness),
                      patch.object(shutdown, "inventory", return_value=report),
                      patch.object(shutdown, "named_claude_account", return_value=Account("claude", LOGIN, LABEL)),
                      redirect_stdout(io.StringIO()) as output):
                    self.assertEqual(shutdown.main(["status", LOGIN, "--here"]), 0)
                status_text = output.getvalue()
                with (patch.object(restart_work, "_session_liveness", return_value=liveness),
                      patch.object(restart_work, "_owner_liveness", return_value=liveness),
                      patch.object(time, "sleep", side_effect=AssertionError("follow must return its action"))):
                    code, follow_text = self.follow_request()
                self.assertEqual(code, 1)
                for text in (status_text, follow_text):
                    self.assertIn("\n  last screen of the failed prompt submit:\n    Claude fixture\n    ❯ unsent opening prompt\n", text)
                    self.assertEqual(text.count("last screen of the failed prompt submit:"), 1)
                    if isinstance(liveness, restart_work.SessionLive):
                        command = settle.shutdown_command("forget", "session-123", "--account", LOGIN)
                        self.assertIn("restart prompt not confirmed; session is running — check it, " +
                                      f"then close its record in a terminal on natedev: {command}", text)
                        self.assertNotIn("shutdown.py restart", text)
                        self.assertNotIn("bring back:", text)
                    else:
                        self.assertIn(f"restart {LOGIN} --here --only session-123", text)
                        if isinstance(liveness, restart_work.SessionLivenessUnknown):
                            self.assertIn("scratch registry unreadable", text)
                self.assertEqual(shutdown_store.find_session(LOGIN, "session-123"), saved)

    def test_status_names_the_peer_terminal_for_a_running_sessions_closing_command(self) -> None:
        for local_machine, peer_machine in (("natedev", "Mac"), ("Mac", "natedev")):
            with self.subTest(local=local_machine, peer=peer_machine):
                saved = session_shutdown(shutdown_store.SessionRestartFailed(kind="restart failed", at=OCTOBER,
                    reason="restart prompt not submitted"), "peer-session")
                saved["machine"] = peer_machine
                restart_work._save_prompt_outcome(saved, herdr.PromptNotSubmitted(  # pyright: ignore[reportPrivateUsage]
                    "not submitted", "local receipt must not describe a peer"))
                local = inventory.Inventory(machine=local_machine, login=LOGIN, label=LABEL,
                                            sessions=[], unattributed=[])
                peer = inventory.Inventory(machine=peer_machine, login=LOGIN, label=LABEL,
                                           sessions=[saved["session"]], unattributed=[])

                def remote(arguments: list[str]) -> tuple[int, str]:
                    if arguments == ["status", LOGIN, "--json", "--here"]:
                        return 0, json.dumps(peer)
                    self.assertEqual(arguments, ["records", "--json", "--here"])
                    return 0, json.dumps([saved])

                with (patch.object(settle, "local_machine", return_value=local_machine),
                      patch.object(shutdown, "other_machine", return_value=peer_machine),
                      patch.object(shutdown, "inventory", return_value=local),
                      patch.object(shutdown, "run_remote", side_effect=remote) as remote_read,
                      patch.object(shutdown, "named_claude_account", return_value=Account("claude", LOGIN, LABEL)),
                      patch.object(restart_work, "_owner_liveness",
                                   side_effect=AssertionError("peer liveness must come from its inventory")),
                      patch.object(restart_work, "_session_liveness",
                                   side_effect=AssertionError("peer liveness must come from its inventory")),
                      redirect_stdout(io.StringIO()) as output):
                    self.assertEqual(shutdown.main(["status", LOGIN]), 0)
                self.assertEqual([call.args[0] for call in remote_read.call_args_list],
                                 [["status", LOGIN, "--json", "--here"], ["records", "--json", "--here"]])
                command = settle.shutdown_command("forget", "peer-session", "--account", LOGIN)
                text = output.getvalue()
                self.assertIn("  alpha: restart prompt not confirmed; session is running — check it, " +
                              f"then close its record in a terminal on {peer_machine}: {command}", text)
                self.assertNotIn(f"then close its record in a terminal on {local_machine}:", text)
                self.assertNotIn("shutdown.py restart", text)
                self.assertNotIn("bring back:", text)
                self.assertNotIn("liveness unknown", text)
                self.assertNotIn("last screen of the failed prompt submit:", text)
                self.assertNotIn("local receipt must not describe a peer", text)

    def test_peer_failed_status_never_reads_a_local_submit_receipt(self) -> None:
        saved = session_shutdown(shutdown_store.SessionRestartFailed(kind="restart failed", at=OCTOBER,
            reason="restart prompt not submitted"))
        saved["machine"] = "Mac"
        for observed in (restart_work.SessionLive(), restart_work.SessionNotLive()):
            with (self.subTest(observed=observed),
                  patch.object(shutdown, "_restart_failed_liveness", return_value=observed),
                  patch.object(restart_work, "saved_prompt_screen", side_effect=AssertionError("peer has no receipt here"))):
                lines = shutdown._shutdown_record_lines(  # pyright: ignore[reportPrivateUsage]
                    {"kind": "records", "records": [saved]}, local_observation=False)
            self.assertNotIn("last screen of the failed prompt submit:", "\n".join(lines))

    def test_status_and_follow_omit_screen_when_submit_never_read_one(self) -> None:
        saved = session_shutdown(shutdown_store.SessionRestartFailed(kind="restart failed", at=OCTOBER,
            reason="restart prompt not submitted"))
        self.assertTrue(shutdown_store.create_session(saved))
        restart_work._save_prompt_outcome(saved, herdr.PromptNotSubmitted("no pane read"))  # pyright: ignore[reportPrivateUsage]
        with patch.object(shutdown, "_restart_failed_liveness", return_value=restart_work.SessionNotLive()):
            lines = shutdown._shutdown_record_lines({"kind": "records", "records": [saved]})  # pyright: ignore[reportPrivateUsage]
            code, follow_text = self.follow_request()
        self.assertEqual(code, 1)
        self.assertNotIn("last screen of the failed prompt submit:", "\n".join(lines) + follow_text)

    def test_printed_closing_command_forgets_the_running_sessions_failed_record(self) -> None:
        saved = session_shutdown(shutdown_store.SessionRestartFailed(kind="restart failed", at=OCTOBER,
            reason="restart prompt not submitted"))
        self.assertTrue(shutdown_store.create_session(saved))
        report = inventory.Inventory(machine="natedev", login=LOGIN, label=LABEL, sessions=[], unattributed=[])
        with (patch.object(restart_work, "_owner_liveness", return_value=restart_work.SessionLive()),
              patch.object(restart_work, "_session_liveness", return_value=restart_work.SessionLive()),
              patch.object(shutdown, "inventory", return_value=report),
              patch.object(shutdown, "named_claude_account", return_value=Account("claude", LOGIN, LABEL)),
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(shutdown.main(["status", LOGIN, "--here"]), 0)
        closing_prefix = "then close its record in a terminal on natedev: "
        lines = [line for line in output.getvalue().splitlines() if closing_prefix in line]
        self.assertEqual(len(lines), 1, output.getvalue())
        printed = lines[0].split(closing_prefix, 1)[1]
        self.assertEqual(printed, settle.shutdown_command("forget", "session-123", "--account", LOGIN))
        self.assertEqual(printed, "~/.claude/scripts/lib/py ~/.claude/scripts/shutdown/shutdown.py " +
                         f"forget session-123 --account {LOGIN}")
        arguments = shlex.split(printed)
        self.assertTrue(arguments[1].endswith("/shutdown/shutdown.py"))
        with redirect_stdout(io.StringIO()) as closed:
            self.assertEqual(shutdown.main(arguments[2:]), 0)
        self.assertIn("forgotten", closed.getvalue())
        self.assertIsNone(shutdown_store.find_session(LOGIN, "session-123"))
        archived = list((self.state / LOGIN / "history").glob("*.json"))
        self.assertEqual([json.loads(path.read_text()) for path in archived], [saved])
        self.assertEqual(list((self.state / LOGIN / "sessions").glob("*.json")), [])
        self.assertEqual(len(list((self.state / LOGIN / "history").glob("*.json"))), 1)

    def test_bring_back_command_keeps_dead_sibling_and_excludes_live_failed_session(self) -> None:
        live = session_shutdown(shutdown_store.SessionRestartFailed(kind="restart failed", at=OCTOBER,
            reason="restart prompt not submitted"), "live-failed")
        dead = session_shutdown(shutdown_store.SessionRestartFailed(kind="restart failed", at=OCTOBER,
            reason="agent exited"), "dead-failed")
        self.assertTrue(shutdown_store.create_session(live))
        self.assertTrue(shutdown_store.create_session(dead))
        def liveness(identity: str) -> restart_work.SessionLive | restart_work.SessionNotLive:
            return restart_work.SessionLive() if identity == "live-failed" else restart_work.SessionNotLive()
        def owner_liveness(owner: inventory.Session) -> restart_work.SessionLive | restart_work.SessionNotLive:
            return liveness(owner["session_id"])
        report = inventory.Inventory(machine="natedev", login=LOGIN, label=LABEL, sessions=[], unattributed=[])
        with (patch.object(restart_work, "_session_liveness", side_effect=liveness),
              patch.object(restart_work, "_owner_liveness", side_effect=owner_liveness),
              patch.object(shutdown, "inventory", return_value=report),
              patch.object(shutdown, "named_claude_account", return_value=Account("claude", LOGIN, LABEL)),
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(shutdown.main(["status", LOGIN, "--here"]), 0)
        actions = [line for line in output.getvalue().splitlines() if "bring back:" in line]
        self.assertEqual(len(actions), 1)
        self.assertIn("--only dead-failed", actions[0])
        self.assertNotIn("live-failed", actions[0])

    def test_follow_needing_user_names_account_session_and_terminal(self) -> None:
        for progress in (
            shutdown_store.SessionStopFailed(kind="stop failed", at=OCTOBER, reason="still alive"),
            shutdown_store.SessionRestartFailed(kind="restart failed", at=OCTOBER, reason="launch refused"),
            shutdown_store.ProcessIdentityLost(kind="process identity lost", at=OCTOBER),
        ):
            with self.subTest(kind=progress["kind"]):
                self.clear_state()
                self.assertTrue(shutdown_store.create_session(session_shutdown(progress)))
                sibling = session_shutdown(shutdown_store.SessionReadyToStop(kind="ready", at=OCTOBER), "z-sibling")
                sibling["session"]["name"] = "Sibling"
                sibling["trail"] = [
                    {"at": OCTOBER, "kind": "waiting", "detail": "pause sent"},
                    {"at": OCTOBER, "kind": "ready", "detail": "checkpoint saved"},
                ]
                self.assertTrue(shutdown_store.create_session(sibling))
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
        self.assertTrue(shutdown_store.create_session(session_shutdown(shutdown_store.SessionWaiting(kind="waiting"))))
        before = shutdown_store.session_records(LOGIN)
        with patch.object(time, "sleep", side_effect=KeyboardInterrupt):
            status, output = self.follow_request()
        self.assertEqual(status, 130)
        self.assertEqual(shutdown_store.session_records(LOGIN), before)
        self.assertIn("the shutdown keeps running", output)
        self.assertIn("in this terminal: ~/.claude/scripts/lib/py ~/.claude/scripts/shutdown/shutdown.py follow", output)
        self.assertIn(LOGIN, output)
        self.assertIn("--request request-one", output)

    def test_follow_alerted_holdout_shows_stop_action_and_exits_one(self) -> None:
        saved = session_shutdown(shutdown_store.SessionWaiting(kind="waiting"))
        saved["alerted_at"] = OCTOBER
        self.assertTrue(shutdown_store.create_session(saved))
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
                    timeout=HANG_GUARD_SECONDS, cwd=self.root,
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
            timeout=HANG_GUARD_SECONDS,
        )

    def conflicting_legacy_record(self, conflict: str) -> str:
        self.clear_state()
        old = shutdown_record("settling", session_id="id/one")
        directory = self.state / LOGIN
        if conflict == "session ids":
            current = session_shutdown(shutdown_store.SessionWaiting(kind="waiting"), "id-one")
            self.assertTrue(shutdown_store.create_session(current))
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
                    with patch.object(sys, "platform", "darwin"), patch.object(herdr, "_PLATFORM", "darwin"), patch.object(settle, "_warm", return_value=True), patch.object(settle, "send_message") as sender, patch.object(settle, "_launch_conductor") as conductor, redirect_stdout(output), redirect_stderr(error):
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
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "id/one"}), patch.object(settle, "run_inventory", return_value=report), patch.object(settle, "blocker", return_value=shutdown_store.ReadinessClear(kind="clear")), redirect_stdout(output), redirect_stderr(error):
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
        with patch.object(shutdown, "inventory", return_value=report), patch.object(shutdown, "conductor_status", return_value=shutdown_store.ConductorNotRunning()), redirect_stdout(output), redirect_stderr(error):
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
            requested_by=None, request_id="", target="claude3", why="", report=report, origin=accounts.HandOrigin(), move_id="", checkout_move=None)

    def test_down_checkout_move_reaches_settle_and_needs_one_here_restart_session(self) -> None:
        selected = Account("claude", LOGIN, LABEL)
        report = inventory.Inventory(machine="natedev", login=LOGIN, label=LABEL,
            sessions=[unit_session("showrunner")], unattributed=[])
        with (
            patch.object(shutdown, "named_claude_account", return_value=selected),
            patch.object(settle, "down", return_value=0) as down,
            patch.object(shutdown, "resolve_sessions", return_value=shutdown.ResolvedSessions(("showrunner",), report)),
            patch("sys.stderr", new_callable=io.StringIO) as error,
        ):
            self.assertEqual(shutdown.main(["down", LABEL, "--here", "--only", "showrunner",
                "--checkout-move", "/w/old", "/w/new"]), 2)
            self.assertIn("--checkout-move needs", error.getvalue())
            self.assertEqual(shutdown.main(["down", LABEL, "--here", "--only", "showrunner", "--restart",
                "--checkout-move", "/w/old", "/w/new"]), 0)
        down.assert_called_once_with(selected, True, ("showrunner",), then="restart", alerts="user",
            requested_by=None, request_id="", target="", why="", report=report, origin=accounts.HandOrigin(), move_id="",
            checkout_move={"old": "/w/old", "new": "/w/new"})

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
            requested_by=None, request_id="", target="claude3", why="weekly limit reached", report=report, origin=accounts.HandOrigin(), move_id="", checkout_move=None)

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
            selected, True, ("unit-a", "unit-b"), then="restart", alerts="user", requested_by=None, request_id="", target="", why="", report=report, origin=accounts.HandOrigin(), move_id="", checkout_move=None
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
        self.assertEqual(shutdown_store.session_records(LOGIN), [])

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
        down.assert_called_once_with(selected, True, (), then="stop", alerts="user", requested_by=None, request_id="", target="", why="", report=report, origin=accounts.HandOrigin(), move_id="", checkout_move=None)



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
                    f"{LABEL} is held by a shutdown (since 2026-10-09 14:49 PDT), requested in a terminal\n",
                )
                self.assertEqual(error, "")

    def test_launch_blocked_uses_pst_for_a_january_request(self) -> None:
        self.write_record(shutdown_record("down", requested_at=JANUARY))

        status, output, error = self.run_cli(LOGIN)

        self.assertEqual(status, 0)
        self.assertEqual(
            output,
            f"{LABEL} is held by a shutdown (since 2026-01-15 09:30 PST), requested in a terminal\n",
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

    def test_a_registering_account_hold_blocks_before_its_sessions_are_recorded(self) -> None:
        held = account_hold()
        held["registration"] = "in progress"
        held["requested_by"] = shutdown_store.FromSession(kind="session", session_id="b1f5ca98-45aa-4604-8e2b-33ed0f071bb1")
        shutdown_store.set_account_hold(held)
        self.assertEqual(self.run_cli(LOGIN), (
            0, f"{LABEL} is held by a shutdown (since 2026-10-09 14:49 PDT), requested by session b1f5ca98\n", "",
        ))

    def test_launch_check_releases_a_finished_stop_hold_and_lets_the_launch_go(self) -> None:
        """The 10-18 claude2 case: a stop hold whose sessions all finished outlived its shutdown."""
        held = account_hold()
        held["then"] = "stop"
        held["requested_by"] = shutdown_store.FromSession(kind="session", session_id="b1f5ca98-45aa-4604-8e2b-33ed0f071bb1")
        shutdown_store.set_account_hold(held)
        saved = session_shutdown(shutdown_store.SessionRestartFailed(kind="restart failed", at=OCTOBER, reason="restart prompt not submitted"))
        self.assertTrue(shutdown_store.create_session(saved))
        shutdown_store.archive_session(LOGIN, saved["session"]["session_id"])
        status, output, error = self.run_cli(LOGIN)
        self.assertEqual((status, error), (1, ""))
        self.assertTrue(output.startswith(launch_permission.RELEASED_HOLD_PREFIX), output)
        self.assertEqual(cast(object, json.loads(output.removeprefix(launch_permission.RELEASED_HOLD_PREFIX))), held)
        self.assertIsNone(shutdown_store.account_hold(LOGIN))
        self.assertEqual(self.run_cli(LOGIN), (1, "", ""))

    def test_selected_shutdown_does_not_hold_new_work(self) -> None:
        for old_format in (False, True):
            with self.subTest(old_format=old_format):
                self.clear_state()
                if old_format:
                    saved = shutdown_record("settling")
                    saved["scope"] = {"kind": "selected", "session_ids": ["session-123"]}
                    write_old(saved)
                else:
                    self.assertTrue(shutdown_store.create_session(session_shutdown(shutdown_store.SessionWaiting(kind="waiting"))))
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
        allowed: tuple[shutdown_store.SessionProgress, ...] = (
            shutdown_store.SessionStopped(kind="stopped", at=OCTOBER),
            shutdown_store.SessionAlreadyGone(kind="already gone", at=OCTOBER),
            shutdown_store.ProcessIdentityLost(kind="process identity lost", at=OCTOBER),
            shutdown_store.SessionRestarting(kind="restarting", at=OCTOBER, prior=shutdown_store.SessionStopped(kind="stopped", at=OCTOBER)),
            shutdown_store.SessionRestartFailed(kind="restart failed", at=OCTOBER, reason="window did not open"),
            shutdown_store.SessionNeedsManualRestart(kind="manual restart", command="claude --resume session-123"),
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
                    self.assertTrue(shutdown_store.create_session(saved))
                    self.assertTrue(shutdown_store.create_session(session_shutdown(
                        shutdown_store.SessionWaiting(kind="waiting"), "session-456", hold="account",
                    )))
                    self.assertEqual(self.run_cli(LOGIN, "--restart-of", "session-123"), (1, "", ""))
                    shutdown_store.set_account_hold(account_hold())
                    self.assertEqual(self.run_cli(LOGIN, "--restart-of", "session-123"), (1, "", ""))
                    self.assertEqual(self.run_cli(LOGIN)[0], 0)
                    self.assertEqual(self.run_cli(LOGIN, "--restart-of", "session-456")[0], 0)
                    self.assertEqual(self.run_cli(LOGIN, "--restart-of", "missing")[0], 0)

    def test_old_restarting_record_allows_its_stopped_unit_and_holds_new_work(self) -> None:
        self.write_record(shutdown_record("restarting", progress=shutdown_store.SessionStopped(kind="stopped", at=OCTOBER)))
        self.assertEqual(self.run_cli(LOGIN, "--restart-of", "session-123"), (1, "", ""))
        self.assertEqual(self.run_cli(LOGIN), (
            0, f"{LABEL} is held by a shutdown (since 2026-10-09 14:49 PDT), requested in a terminal\n", "",
        ))



    def test_selected_waiting_session_restore_is_held_without_an_account_hold(self) -> None:
        saved = session_shutdown(shutdown_store.SessionWaiting(kind="waiting"))
        self.assertTrue(shutdown_store.create_session(saved))
        self.assertIsNone(shutdown_store.account_hold(LOGIN))
        self.assertEqual(self.run_cli(LOGIN, "--restart-of", "session-123"), (
            0, "alpha is held by a shutdown (since 2026-10-09 14:49 PDT)\n", "",
        ))

    def test_selected_ready_session_restore_is_held_without_an_account_hold(self) -> None:
        saved = session_shutdown(shutdown_store.SessionReadyToStop(kind="ready", at=OCTOBER))
        saved["requested_at"] = JANUARY
        self.assertTrue(shutdown_store.create_session(saved))
        self.assertIsNone(shutdown_store.account_hold(LOGIN))
        self.assertEqual(self.run_cli(LOGIN, "--restart-of", "session-123"), (
            0, "alpha is held by a shutdown (since 2026-01-15 09:30 PST)\n", "",
        ))

    def test_stopped_session_restore_is_allowed_under_an_account_hold(self) -> None:
        self.assertTrue(shutdown_store.create_session(session_shutdown(
            shutdown_store.SessionStopped(kind="stopped", at=OCTOBER),
        )))
        shutdown_store.set_account_hold(account_hold())
        self.assertEqual(self.run_cli(LOGIN, "--restart-of", "session-123"), (1, "", ""))

    def test_missing_session_restore_is_allowed_without_an_account_hold(self) -> None:
        self.assertEqual(self.run_cli(LOGIN, "--restart-of", "missing"), (1, "", ""))

    def test_restart_permission_for_other_progress_is_held_by_its_own_shutdown(self) -> None:
        refused: tuple[shutdown_store.SessionProgress, ...] = (
            shutdown_store.SessionWaiting(kind="waiting"),
            shutdown_store.SessionReadyToStop(kind="ready", at=OCTOBER),
            shutdown_store.SessionStopFailed(kind="stop failed", at=OCTOBER, reason="busy"),
            shutdown_store.SessionRestarted(kind="restarted", at=OCTOBER),
            shutdown_store.SessionRestoredTimersPending(kind="timers pending", at=OCTOBER, timers=[]),
        )
        for progress in refused:
            with self.subTest(progress=progress["kind"]):
                self.clear_state()
                self.assertTrue(shutdown_store.create_session(session_shutdown(progress)))
                expected = (0, "alpha is held by a shutdown (since 2026-10-09 14:49 PDT)\n", "")
                self.assertEqual(self.run_cli(LOGIN, "--restart-of", "session-123"), expected)
                shutdown_store.set_account_hold(account_hold())
                self.assertEqual(self.run_cli(LOGIN, "--restart-of", "session-123"), expected)

    def test_held_sessions_includes_owners_and_workers_sorted_once(self) -> None:
        saved = session_shutdown(shutdown_store.SessionWaiting(kind="waiting"), "z-owner")
        owner = saved["session"]
        saved["workers"] = [inventory.WorkerSession(
            session_id="a-worker", pid=23456, proc_start="worker-start", name="worker",
            cwd=owner["cwd"], status="idle", updated_at=0, probe=False, model=owner["model"], checkout=owner["checkout"],
            run_dirs=["/tmp/run"], codex_servers=[], timers=[], kind="worker",
            host=inventory.UnknownHost(kind="unknown"),
            owner=inventory.DirectorOwner(kind="director", session_id="z-owner"),
        )]
        self.assertTrue(shutdown_store.create_session(saved))
        self.assertTrue(shutdown_store.create_session(session_shutdown(shutdown_store.SessionStopped(kind="stopped", at=OCTOBER), "m-owner")))
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

        held = account_hold()
        held["requested_by"] = shutdown_store.FromSession(kind="session", session_id="b1f5ca98-45aa-4604-8e2b-33ed0f071bb1")
        released_line = launch_permission.RELEASED_HOLD_PREFIX + json.dumps(held) + "\n"
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 1, released_line, "")):
            released = launch_permission.launch_permission(self.environment, launch_permission.NewWorkLaunchPurpose())
        self.assertEqual(released, launch_permission.AllowedByShutdownState(launch_permission.ReleasedHold(
            LABEL, "restart", OCTOBER, "b1f5ca98-45aa-4604-8e2b-33ed0f071bb1")))
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess(
                [], 1, launch_permission.RELEASED_HOLD_PREFIX + "{\n", "")):
            broken = launch_permission.launch_permission(self.environment, launch_permission.NewWorkLaunchPurpose())
        self.assertIsInstance(broken, launch_permission.ShutdownStateUnreadable)

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
            timeout=HANG_GUARD_SECONDS,
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
                launch_permission.RecordedSessionRestoreLaunchPurpose("session-123"),
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
            + "(since 2026-10-09 14:49 PDT), requested in a terminal\n",
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
        down.assert_called_once_with(selected, True, (), then="restart", alerts="none", requested_by="requester", request_id="same-request", target="", why="", report=report, origin=accounts.HandOrigin(), move_id="", checkout_move=None)

    def test_stopping_session_restore_is_held_by_name(self) -> None:
        self.assertTrue(shutdown_store.create_session(session_shutdown(shutdown_store.SessionStopping(kind="stopping", at=OCTOBER))))
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
        real_bounded_stop = stop_work.bounded_stop
        def stop_selected(saved: list[shutdown_store.SessionShutdown], *, started: float, grace_seconds: float) -> list[shutdown_store.SessionShutdown]:
            # Selection fixtures have no real processes; supply a complete exit observation.
            return real_bounded_stop(saved, started=started, grace_seconds=grace_seconds,
                observe=lambda _budget: inventory.ProcessObservation({}),
                identity=lambda _pid, _budget: inventory.ProcessGone())
        _ = self.enterContext(patch.object(stop_work, "bounded_stop", side_effect=stop_selected))
        # Keep this CLI fixture focused on selection; missing-folder behavior has separate restore tests.
        _ = self.enterContext(patch.object(restart_work, "_folder_exists", return_value=True))
        _ = self.enterContext(patch.object(restart_work, "_prepare_restore_server"))
        _ = self.enterContext(patch.object(herdr.Herdr, "close_restore_placeholders"))
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
        _ = self.enterContext(patch.object(restart_work, "_launch_session", return_value=restart_work.SessionLaunched(herdr.PromptSubmitted(datetime.fromisoformat(OCTOBER)))))
        _ = self.enterContext(patch.object(restart_work, "_prompt_outcome", return_value=herdr.PromptSubmitted(datetime.fromisoformat(OCTOBER))))

    def save_three(self, progress: shutdown_store.SessionProgress) -> list[shutdown_store.SessionShutdown]:
        saved: list[shutdown_store.SessionShutdown] = []
        for owner in self.owners:
            item = session_shutdown(progress, owner["session_id"])
            item["session"] = owner
            self.assertTrue(shutdown_store.create_session(item))
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
                    progress: shutdown_store.SessionProgress = {"kind": "stopped", "at": OCTOBER} if verb == "restart" else {"kind": "waiting"}
                    before = self.save_three(progress)
                    if verb == "down":
                        shutdown_store.archive_session(LOGIN, "beta-id")
                    status, output, error = self.action(verb, selection)
                    self.assertEqual((status, error), (0, ""))
                    self.assertEqual(shutdown_store.find_session(LOGIN, "alpha-id"), before[0])
                    self.assertEqual(shutdown_store.find_session(LOGIN, "gamma-id"), before[2])
                    selected = shutdown_store.find_session(LOGIN, "beta-id")
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
        self.assertEqual(shutdown_store.session_records(LOGIN), sorted(before, key=lambda item: item["session"]["name"]))

    def test_terminal_only_routes_selection_to_its_account_for_down_restart_and_status(self) -> None:
        selected = [Account("claude", LOGIN, LABEL), Account("claude", "other@example.com", "claude3")]
        real_resolve = shutdown.resolve_sessions
        def resolve(login: str, items: Iterable[str], *, here: bool = False, recovery: bool = False) -> shutdown.ResolvedSessions:
            if login != LOGIN:
                raise shutdown.SessionNotFound("Beta: matches no session")
            return real_resolve(login, items, here=here, recovery=recovery)
        for verb in ("down", "restart", "status"):
            with (self.subTest(verb=verb),
                  patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": ""}),
                  patch.object(shutdown, "_terminal_accounts", return_value=shutdown.TerminalAccountSelection(tuple(selected))),
                  patch.object(shutdown, "resolve_sessions", side_effect=resolve),
                  patch.object(settle, "down", return_value=0) as down,
                  patch.object(restart_work, "restart", return_value=0) as resume,
                  patch.object(shutdown, "run_remote", side_effect=AssertionError("--here read peer")),
                  redirect_stdout(io.StringIO()) as output):
                args = [verb, "--only", "Beta", "--here"]
                if verb == "status":
                    args.append("--json")
                self.assertEqual(shutdown.main(args), 0)
                if verb == "down":
                    self.assertEqual(down.call_args.args[:3], (selected[0], True, ("beta-id",)))
                elif verb == "restart":
                    self.assertEqual(resume.call_args.args[0], restart_work.AccountNamed(LOGIN))
                    self.assertEqual(resume.call_args.kwargs["only"], ["beta-id"])
                else:
                    reports = cast(list[dict[str, object]], json.loads(output.getvalue()))
                    self.assertEqual([report["login"] for report in reports], [LOGIN])
                    owners = cast(list[dict[str, object]], reports[0]["sessions"])
                    self.assertEqual([owner["session_id"] for owner in owners], ["beta-id"])

    def terminal_now(self, selection: str) -> tuple[int, str, str, MagicMock]:
        """`now --only` from a terminal on another account, as a rename does: no session is live, only records."""
        showrunner = Account("claude", "showrunner@example.com", "claude2")
        with (patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": ""}),
              patch.object(shutdown, "own_claude_account", return_value=showrunner),
              patch.object(shutdown, "listing", return_value=[]),
              patch.object(settle, "run_inventory", return_value=inventory.Inventory(
                  machine="natedev", login=LOGIN, label=LABEL, sessions=[], unattributed=[])),
              patch.object(settle, "_launch_conductor") as conductor,
              redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as error):
            status = shutdown.main(["now", "--only", selection, "--here"])
        return status, output.getvalue(), error.getvalue(), conductor

    def test_terminal_now_forces_an_earlier_record_held_by_another_account(self) -> None:
        before = self.save_three({"kind": "waiting"})
        status, output, error, conductor = self.terminal_now("beta-id")
        self.assertEqual((status, output, error), (0, "Beta: stopping now\n", ""))
        conductor.assert_called_once_with(LOGIN, LABEL)
        latest = shutdown_store.find_session(LOGIN, "beta-id")
        assert latest is not None
        self.assertEqual(latest["force"], "now")
        self.assertEqual(shutdown_store.find_session(LOGIN, "alpha-id"), before[0])
        self.assertEqual(shutdown_store.find_session(LOGIN, "gamma-id"), before[2])

    def test_terminal_now_refuses_an_id_with_no_session_and_no_record(self) -> None:
        before = self.save_three({"kind": "waiting"})
        status, output, error, conductor = self.terminal_now("unknown-id")
        self.assertEqual((status, output, error), (2, "", "shutdown: unknown-id: matches no session\n"))
        conductor.assert_not_called()
        self.assertEqual(shutdown_store.session_records(LOGIN), sorted(before, key=lambda item: item["session"]["name"]))

    def test_terminal_down_starts_both_accounts_without_following_the_first(self) -> None:
        class InteractiveTerminalOutput(io.StringIO):
            @override
            def isatty(self) -> bool:
                return True

        selected = [Account("claude", LOGIN, LABEL), Account("claude", "other@example.com", "claude3")]
        by_login = {account.login: account for account in selected}
        owners = {LOGIN: self.owners[0], "other@example.com": unit_session("other-id")}
        def report(login: str, _only: tuple[str, ...] = ()) -> inventory.Inventory:
            return inventory.Inventory(machine="natedev", login=login, label=by_login[login].label,
                sessions=[owners[login]], unattributed=[])
        def account_for_login(login: str) -> Account:
            return by_login[login]
        output = InteractiveTerminalOutput()
        with (patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": ""}),
              patch.object(shutdown, "_terminal_accounts", return_value=shutdown.TerminalAccountSelection(tuple(selected))),
              patch.object(shutdown, "named_claude_account", side_effect=account_for_login),
              patch.object(shutdown, "inventory", side_effect=report),
              patch.object(settle, "run_inventory", side_effect=report),
              patch.object(settle, "_launch_conductor") as conduct,
              patch.object(shutdown, "follow", side_effect=AssertionError("following first account blocks second")),
              redirect_stdout(output)):
            self.assertEqual(shutdown.main(["down", "--here", "--no-alert"]), 0)
        self.assertEqual([call.args[0] for call in conduct.call_args_list], [LOGIN, "other@example.com"])
        self.assertEqual([saved["session"]["session_id"] for saved in shutdown_store.session_records(LOGIN)], ["alpha-id"])
        self.assertEqual([saved["session"]["session_id"] for saved in shutdown_store.session_records("other@example.com")], ["other-id"])
        self.assertIn("follow owner@example.com --request", output.getvalue())
        self.assertIn("follow other@example.com --request", output.getvalue())

    def test_status_only_shows_the_selected_live_session_and_its_shutdown(self) -> None:
        _ = self.save_three({"kind": "waiting"})
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            self.assertEqual(shutdown.main(["status", LOGIN, "--only", "Beta", "--here"]), 0)
        self.assertEqual(error.getvalue(), "")
        self.assertIn("Beta", output.getvalue())
        self.assertNotIn("Alpha", output.getvalue())
        self.assertNotIn("Gamma", output.getvalue())

    def test_selected_status_filters_peer_sessions_without_requiring_a_peer_match(self) -> None:
        peer = inventory.Inventory(machine="Mac", login=LOGIN, label=LABEL, sessions=self.owners, unattributed=[])
        with patch.object(shutdown, "run_remote", return_value=(0, json.dumps(peer))) as remote:
            reports = shutdown._status(Account("claude", LOGIN, LABEL), False, ("beta-id",))  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(remote.call_args.args[0], ["status", LOGIN, "--json", "--here"])
        self.assertEqual(len(reports), 2)
        self.assertIn("sessions", reports[1])
        peer_report = cast(inventory.Inventory, reports[1])
        self.assertEqual([owner["session_id"] for owner in peer_report["sessions"]], ["beta-id"])

    def test_terminal_only_ambiguous_across_accounts_starts_no_shutdown(self) -> None:
        selected = [Account("claude", LOGIN, LABEL), Account("claude", "other@example.com", "claude3")]
        resolved = shutdown.ResolvedSessions(("beta-id",), inventory.Inventory(machine="natedev", login=LOGIN, label=LABEL, sessions=self.owners, unattributed=[]))
        with (patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": ""}),
              patch.object(shutdown, "_terminal_accounts", return_value=shutdown.TerminalAccountSelection(tuple(selected))),
              patch.object(shutdown, "resolve_sessions", return_value=resolved),
              patch.object(settle, "down") as down,
              redirect_stderr(io.StringIO()) as error):
            self.assertEqual(shutdown.main(["down", "--only", "Beta", "--here"]), 2)
        down.assert_not_called()
        self.assertIn("claude2, claude3", error.getvalue())

    def test_down_exact_name_rescans_after_revocation_and_keeps_owned_workers(self) -> None:
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
              patch.object(settle, "run_inventory", return_value=inventory.Inventory(
                  machine="natedev", login=LOGIN, label=LABEL, sessions=[self.owners[1], worker], unattributed=[])) as rescan,
              patch.object(settle, "inventory", side_effect=AssertionError("begin inventories twice"))):
            status, _, error = self.action("down", "Beta")
        self.assertEqual((status, error), (0, ""))
        read.assert_called_once_with(LOGIN, only=frozenset({"beta-id"}))
        rescan.assert_called_once_with(LOGIN, ("beta-id",))
        saved = shutdown_store.session_records(LOGIN)
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
        self.assertEqual(shutdown_store.session_records(LOGIN), [])

    def test_down_without_selection_rescans_after_revocation(self) -> None:
        report = inventory.Inventory(machine="natedev", login=LOGIN, label=LABEL,
            sessions=self.owners, unattributed=[])
        with (patch.object(shutdown, "inventory", return_value=report) as read,
              patch.object(settle, "run_inventory", return_value=report) as rescan,
              patch.object(settle, "inventory", side_effect=AssertionError("down inventories twice")),
              redirect_stdout(io.StringIO())):
            status = shutdown.main(["down", LOGIN, "--here"])
        self.assertEqual(status, 0)
        read.assert_called_once_with(LOGIN)
        rescan.assert_called_once_with(LOGIN, ())
        self.assertEqual({saved["session"]["session_id"] for saved in shutdown_store.session_records(LOGIN)},
            {owner["session_id"] for owner in self.owners})

    def test_restart_list_skips_live_and_keeps_going_after_unknown_name(self) -> None:
        from test_restart import SUBMITTED_PROMPT

        for all_accounts in (False, True):
            with self.subTest(all_accounts=all_accounts):
                state = self.root / "state"
                if state.exists():
                    shutil.rmtree(state)
                self.owners = self.owners[:2]
                _ = self.save_three(shutdown_store.SessionStopped(kind="stopped", at=OCTOBER))
                live = sessions.SessionRecord(pid=99, sessionId="elsewhere", name="Running", messagingSocketPath="",
                    updatedAt=1, tmux="", formerNames=[])
                args = ["restart", *([] if all_accounts else [LABEL]), "--only", "Running,Unknown,Alpha,Beta", "--here", "--no-alert"]
                with (patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": ""}),
                      patch.object(sessions, "live_sessions", return_value=[live]),
                      patch.object(shutdown, "_terminal_accounts", return_value=shutdown.TerminalAccountSelection((Account("claude", LOGIN, LABEL),))),
                      patch.object(restart_work, "_launch_session", return_value=restart_work.SessionLaunched(SUBMITTED_PROMPT)) as launch,
                      patch.object(launch_permission, "launch_permission", return_value=launch_permission.AllowedByShutdownState()),
                      redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as error):
                    code = shutdown.main(args)
                self.assertEqual(code, 1)
                self.assertIn("Running: already running; skipped", output.getvalue())
                self.assertIn("Unknown: matches no session", error.getvalue())
                self.assertEqual(launch.call_count, 2)
                self.assertEqual(shutdown_store.session_records(LOGIN), [])

    def test_restart_skips_a_recordless_live_session_on_the_selected_account(self) -> None:
        for all_accounts in (False, True):
            with self.subTest(all_accounts=all_accounts):
                owner = self.owners[0]
                live = sessions.SessionRecord(pid=owner["pid"], sessionId=owner["session_id"], name=owner["name"],
                    messagingSocketPath="", updatedAt=1, tmux="", formerNames=[])
                args = ["restart", *([] if all_accounts else [LABEL]), "--only", owner["name"], "--here"]
                with (patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": ""}),
                      patch.object(sessions, "live_sessions", return_value=[live]),
                      patch.object(shutdown, "_terminal_accounts", return_value=shutdown.TerminalAccountSelection((Account("claude", LOGIN, LABEL),))),
                      patch.object(restart_work, "_launch_session", side_effect=AssertionError("live session relaunched")),
                      patch.object(launch_permission, "launch_permission", return_value=launch_permission.AllowedByShutdownState()),
                      redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as error):
                    code = shutdown.main(args)
                self.assertEqual((code, error.getvalue()), (0, ""))
                self.assertEqual(output.getvalue(), f"{owner['name']}: already running; skipped\n")

    def test_restart_stopped_record_wins_over_a_different_live_session_with_the_same_name(self) -> None:
        from test_restart import SUBMITTED_PROMPT

        for all_accounts in (False, True):
            with self.subTest(all_accounts=all_accounts):
                state = self.root / "state"
                if state.exists():
                    shutil.rmtree(state)
                _ = self.save_three(shutdown_store.SessionStopped(kind="stopped", at=OCTOBER))
                live = sessions.SessionRecord(pid=99, sessionId="different-alpha", name="Alpha",
                    messagingSocketPath="", updatedAt=1, tmux="", formerNames=[])
                args = ["restart", *([] if all_accounts else [LABEL]), "--only", "Alpha", "--here", "--no-alert"]
                with (patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": ""}),
                      patch.object(sessions, "live_sessions", return_value=[live]),
                      patch.object(shutdown, "_terminal_accounts", return_value=shutdown.TerminalAccountSelection((Account("claude", LOGIN, LABEL),))),
                      patch.object(restart_work, "_launch_session", return_value=restart_work.SessionLaunched(SUBMITTED_PROMPT)) as launch,
                      patch.object(launch_permission, "launch_permission", return_value=launch_permission.AllowedByShutdownState()),
                      redirect_stdout(io.StringIO()) as output):
                    code = shutdown.main(args)
                self.assertEqual(code, 0)
                launch.assert_called_once()
                self.assertEqual(cast(shutdown_store.SessionShutdown, launch.call_args.args[0])["session"]["session_id"], "alpha-id")
                self.assertNotIn("Alpha: already running; skipped", output.getvalue())
                self.assertNotIn("alpha-id", [saved["session"]["session_id"] for saved in shutdown_store.session_records(LOGIN)])

    def test_restart_reports_stopped_name_ambiguity_across_machines_and_restarts_other_names(self) -> None:
        from test_restart import SUBMITTED_PROMPT

        for all_accounts in (False, True):
            with self.subTest(all_accounts=all_accounts):
                state = self.root / "state"
                if state.exists():
                    shutil.rmtree(state)
                _ = self.save_three(shutdown_store.SessionStopped(kind="stopped", at=OCTOBER))
                peer_alpha = session_shutdown(shutdown_store.SessionStopped(kind="stopped", at=OCTOBER), "peer-alpha-id")
                peer_alpha["machine"] = "Mac"
                peer_alpha["session"]["name"] = "Alpha"
                report = inventory.Inventory(machine="Mac", login=LOGIN, label=LABEL, sessions=[], unattributed=[])
                args = ["restart", *([] if all_accounts else [LABEL]), "--only", "Alpha,Beta,Gamma", "--no-alert"]
                with (patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": ""}),
                      patch.object(sessions, "live_sessions", return_value=[]),
                      patch.object(shutdown, "_terminal_accounts", return_value=shutdown.TerminalAccountSelection((Account("claude", LOGIN, LABEL),))),
                      patch.object(shutdown, "_remote_shutdown_records", return_value=shutdown.AccountShutdownRecords(kind="records", records=[peer_alpha])),
                      patch.object(shutdown, "run_remote", return_value=(0, json.dumps(report))),
                      patch.object(restart_work, "_launch_session", return_value=restart_work.SessionLaunched(SUBMITTED_PROMPT)) as launch,
                      patch.object(launch_permission, "launch_permission", return_value=launch_permission.AllowedByShutdownState()),
                      redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as error):
                    code = shutdown.main(args)
                self.assertEqual(code, 1)
                self.assertIn("Alpha: matches more than one session", error.getvalue())
                self.assertIn("natedev: Alpha (alpha-id)", error.getvalue())
                self.assertIn("Mac: Alpha (peer-alpha-id)", error.getvalue())
                launched = [cast(shutdown_store.SessionShutdown, call.args[0])["session"]["name"] for call in launch.call_args_list]
                self.assertCountEqual(launched, ["Beta", "Gamma"])
                self.assertEqual([saved["session"]["name"] for saved in shutdown_store.session_records(LOGIN)], ["Alpha"])

    def test_only_unknown_item_exits_two_without_starting_work(self) -> None:
        before = self.save_three({"kind": "waiting"})
        with patch.object(settle, "_launch_conductor") as conductor:
            status, output, error = self.action("now", "absent")
        self.assertEqual(status, 2)
        self.assertIn("absent", output + error)
        conductor.assert_not_called()
        self.assertEqual(shutdown_store.session_records(LOGIN), before)

    def test_only_restart_refuses_waiting_ready_and_stopping_with_exact_line(self) -> None:
        cases: list[shutdown_store.SessionProgress] = [{"kind": "waiting"}, {"kind": "ready", "at": OCTOBER}, {"kind": "stopping", "at": OCTOBER}, shutdown_store.SessionHeldByUsageLimit(kind="held by usage limit", at=OCTOBER)]
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
                self.assertEqual(shutdown_store.session_records(LOGIN), before)

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
        self.assertEqual(shutdown_store.session_records(LOGIN), before)

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
        self.assertEqual([saved["session"]["session_id"] for saved in shutdown_store.session_records(LOGIN)], ["beta-id"])
        self.assertIn("Beta: added\n", output)
        self.assertIsNone(shutdown_store.account_hold(LOGIN))

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
                    self.assertEqual(shutdown_store.session_records(LOGIN), before)

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
                    shutdown_store.set_account_hold(shutdown_store.AccountHold(login=LOGIN, label=LABEL, machine="natedev", requested_at=OCTOBER, requested_by={"kind": "terminal"}, request_id="held", then="stop"))
                output, error = io.StringIO(), io.StringIO()
                with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "caller-id"}), patch.object(shutdown, "own_claude_account", side_effect=UnreadableAccount("own account unreadable")), patch.object(restart_work, "own_claude_account", side_effect=UnreadableAccount("own account unreadable")), patch.object(restart_work, "run_remote", return_value=(0, "[]")), patch.object(restart_work, "_up", return_value=restart_work.MachineRestartResult("natedev", LABEL, "done")) as resume, redirect_stdout(output), redirect_stderr(error):
                    self.assertEqual(shutdown.main(["restart", "--only", "beta-id", "--here", "--no-alert"]), 0)
                self.assertEqual(error.getvalue(), "")
                resume.assert_called_once_with(LOGIN, False, ("beta-id",))

    def test_selected_restart_silent_failure_ignores_sibling_policy_and_never_reads_peer_here(self) -> None:
        before = self.save_three({"kind": "stopped", "at": OCTOBER})
        def silence(current: shutdown_store.SessionShutdown) -> None:
            current["alerts"] = "none"
        _ = shutdown_store.update_session(LOGIN, "beta-id", silence)
        with patch.object(restart_work, "_launch_session", side_effect=OSError("launch failed")), patch.object(restart_work, "run_remote") as peer, patch.object(settle, "alert_user") as alert:
            status, _output, error = self.action("restart", "beta-id")
        self.assertEqual((status, error), (1, ""))
        alert.assert_not_called()
        peer.assert_not_called()
        self.assertEqual(shutdown_store.find_session(LOGIN, "alpha-id"), before[0])
        self.assertEqual(shutdown_store.find_session(LOGIN, "gamma-id"), before[2])

    def test_selected_restart_refusal_has_no_alert_count_or_summary(self) -> None:
        _ = self.save_three({"kind": "waiting"})
        with patch.object(restart_work, "_session_liveness", return_value=restart_work.SessionLive()), patch.object(settle, "alert_user") as alert, patch.object(restart_work, "_print_result") as summary:
            status, output, error = self.action("restart", "beta-id")
        self.assertEqual((status, output, error), (1, "Beta: already running; skipped\nBeta is waiting, not stopped; nothing to restart\n", ""))
        alert.assert_not_called()
        summary.assert_not_called()

    def test_selected_restart_refusal_beside_real_failure_alerts_for_failed_session(self) -> None:
        _ = self.save_three({"kind": "stopped", "at": OCTOBER})
        def wait(current: shutdown_store.SessionShutdown) -> None:
            current["progress"] = {"kind": "waiting"}
        _ = shutdown_store.update_session(LOGIN, "alpha-id", wait)
        def liveness(identity: str) -> restart_work.SessionLive | restart_work.SessionNotLive:
            return restart_work.SessionLive() if identity == "alpha-id" else restart_work.SessionNotLive()
        with patch.object(restart_work, "_launch_session", side_effect=OSError("window launch failed")), patch.object(restart_work, "_session_liveness", side_effect=liveness), patch.object(settle, "alert_user") as alert:
            status, output, error = self.action("restart", "alpha-id,beta-id")
        self.assertEqual((status, error), (1, ""))
        self.assertIn("Alpha is waiting, not stopped; nothing to restart\n", output)
        alert.assert_called_once()
        push = cast(RenderedPush, alert.call_args.args[0])
        self.assertEqual(push.id, "shutdown.restart_failed")
        self.assertEqual(push.values["stopped"], "One session (Beta)")
        self.assertIn("window launch failed", output)

    def test_selected_restart_of_a_failed_move_pushes_the_move_alert_not_a_restart_onto_the_source(self) -> None:
        _ = self.save_three({"kind": "stopped", "at": OCTOBER})
        def move(current: shutdown_store.SessionShutdown) -> None:
            current["target"], current["source_label"] = "claude3", current["label"]
        _ = shutdown_store.update_session(LOGIN, "beta-id", move)
        with (patch.object(restart_work, "_launch_session", side_effect=OSError("pane still holds a live agent")),
              patch.object(restart_work, "_session_liveness", return_value=restart_work.SessionNotLive()),
              patch.object(settle, "alert_user") as alert):
            status, _output, error = self.action("restart", "beta-id")
        self.assertEqual((status, error), (1, ""))
        alert.assert_called_once()
        push = cast(RenderedPush, alert.call_args.args[0])
        self.assertEqual(push.id, "shutdown.move_failed")
        self.assertEqual((push.values["session"], push.values["target"]), ("Beta", "claude3"))

    def test_selected_now_stops_live_owners_with_old_stopping_or_stopped_records(self) -> None:
        cases: list[shutdown_store.SessionProgress] = [{"kind": "stopping", "at": OCTOBER}, {"kind": "stopped", "at": OCTOBER}]
        for progress in cases:
            with self.subTest(progress=progress["kind"]):
                state = self.root / "state"
                if state.exists():
                    shutil.rmtree(state)
                before = self.save_three(progress)
                with patch.object(settle, "_launch_conductor") as conductor:
                    status, output, error = self.action("now", "beta-id")
                self.assertEqual((status, error), (0, ""))
                self.assertIn("Beta: stopping now\n", output)
                self.assertRegex(output, r"\d\d:\d\d:\d\d  stopped  Beta \(by itself, [\d.]+ s\)\nall 1 stopped\n")
                conductor.assert_called_once_with(LOGIN, LABEL)
                current = shutdown_store.find_session(LOGIN, "beta-id")
                assert current is not None
                self.assertEqual(current["progress"]["kind"], "stopped")
                self.assertEqual(current["force"], "now")
                self.assertEqual(shutdown_store.find_session(LOGIN, "alpha-id"), before[0])
                self.assertEqual(shutdown_store.find_session(LOGIN, "gamma-id"), before[2])

    def test_selected_cancel_names_the_waiting_session_taken_out(self) -> None:
        _ = self.save_three({"kind": "waiting"})
        self.assertEqual(self.action("cancel", "beta-id"), (0, "Beta: taken out\n", ""))

    def test_selected_now_forces_usage_limit_held_session_and_names_it(self) -> None:
        before = self.save_three(shutdown_store.SessionHeldByUsageLimit(kind="held by usage limit", at=OCTOBER))
        with patch.object(settle, "_launch_conductor") as conductor:
            status, output, error = self.action("now", "beta-id")
            self.assertEqual((status, error), (0, ""))
            self.assertIn("Beta: stopping now\n", output)
            self.assertRegex(output, r"\d\d:\d\d:\d\d  stopped  Beta \(by itself, [\d.]+ s\)\nall 1 stopped\n")
        conductor.assert_called_once_with(LOGIN, LABEL)
        latest = shutdown_store.find_session(LOGIN, "beta-id")
        assert latest is not None
        self.assertEqual(latest["force"], "now")
        self.assertEqual(shutdown_store.find_session(LOGIN, "alpha-id"), before[0])
        self.assertEqual(shutdown_store.find_session(LOGIN, "gamma-id"), before[2])

    def test_status_stop_now_action_accepts_reaping_session_by_name(self) -> None:
        progress = shutdown_store.SessionReaping(kind="reaping", at=OCTOBER,
            because="account out of usage", detail="weekly limit")
        before = self.save_three(progress)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(shutdown.main(["status", LOGIN, "--here"]), 0)
        self.assertIn(f"  stop now: in a terminal on natedev: ~/.claude/scripts/lib/py ~/.claude/scripts/shutdown/shutdown.py now {LOGIN} --here --only alpha-id,beta-id,gamma-id · Alpha, Beta, Gamma\n", output.getvalue())
        with patch.object(settle, "_launch_conductor") as conductor:
            status, output, error = self.action("now", "Beta")
            self.assertEqual((status, error), (0, ""))
            self.assertIn("Beta: stopping now\n", output)
            self.assertRegex(output, r"\d\d:\d\d:\d\d  stopped  Beta \(by itself, [\d.]+ s\)\nall 1 stopped\n")
        conductor.assert_called_once_with(LOGIN, LABEL)
        latest = shutdown_store.find_session(LOGIN, "beta-id")
        assert latest is not None
        self.assertEqual(latest["force"], "now")
        self.assertEqual(latest["progress"]["kind"], "stopped")
        self.assertEqual(shutdown_store.find_session(LOGIN, "alpha-id"), before[0])
        self.assertEqual(shutdown_store.find_session(LOGIN, "gamma-id"), before[2])

    def test_selected_cancel_takes_usage_limit_held_session_out_and_names_it(self) -> None:
        before = self.save_three(shutdown_store.SessionHeldByUsageLimit(kind="held by usage limit", at=OCTOBER))
        self.assertEqual(self.action("cancel", "beta-id"), (0, "Beta: taken out\n", ""))
        self.assertIsNone(shutdown_store.find_session(LOGIN, "beta-id"))
        self.assertEqual(shutdown_store.find_session(LOGIN, "alpha-id"), before[0])
        self.assertEqual(shutdown_store.find_session(LOGIN, "gamma-id"), before[2])

    def test_status_actions_include_usage_limit_held_session(self) -> None:
        _ = self.save_three(shutdown_store.SessionHeldByUsageLimit(kind="held by usage limit", at=OCTOBER))
        output = io.StringIO()
        with patch.object(settle, "_showing_form", return_value=True), patch.object(settle, "merge_in_progress", return_value=False), redirect_stdout(output):
            self.assertEqual(shutdown.main(["status", LOGIN, "--here"]), 0)
        self.assertIn(f"  take out: in a terminal on natedev: ~/.claude/scripts/lib/py ~/.claude/scripts/shutdown/shutdown.py cancel {LOGIN} --here --only alpha-id,beta-id,gamma-id · Alpha, Beta, Gamma\n", output.getvalue())
        self.assertIn(f"  stop now: in a terminal on natedev: ~/.claude/scripts/lib/py ~/.claude/scripts/shutdown/shutdown.py now {LOGIN} --here --only alpha-id,beta-id,gamma-id · Alpha, Beta, Gamma\n", output.getvalue())

    def test_selected_restart_fresh_claim_names_existing_worker_without_launch_or_alert(self) -> None:
        _ = self.save_three({"kind": "stopped", "at": OCTOBER})
        def claim(current: shutdown_store.SessionShutdown) -> None:
            current["progress"] = {"kind": "restarting", "at": settle.record_time(), "prior": {"kind": "stopped", "at": OCTOBER}}
        saved = shutdown_store.update_session(LOGIN, "beta-id", claim)
        with patch.object(restart_work, "_launch_session") as launch, patch.object(settle, "alert_user") as alert:
            self.assertEqual(self.action("restart", "beta-id"), (0, "Beta is already restarting\n", ""))
        launch.assert_not_called()
        alert.assert_not_called()
        self.assertEqual(shutdown_store.find_session(LOGIN, "beta-id"), saved)

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
        def stopped(current: shutdown_store.SessionShutdown) -> None:
            current["progress"] = {"kind": "stopped", "at": OCTOBER}
        refused = shutdown_store.update_session(LOGIN, "beta-id", stopped)
        report = inventory.Inventory(machine="natedev", login=LOGIN, label=LABEL,
            sessions=[owner for owner in self.owners if owner["session_id"] != "beta-id"], unattributed=[])
        with (patch.object(settle, "_launch_conductor") as conductor,
              patch.object(settle, "run_inventory", return_value=report)):
            status, output, error = self.action("now", "alpha-id,beta-id")
        self.assertEqual((status, error), (1, ""))
        self.assertRegex(output, r"\AAlpha: already in progress\nAlpha: stopping now\n\d\d:\d\d:\d\d  stopped  Alpha \(by itself, [\d.]+ s\)\nall 1 stopped\nBeta is stopped; nothing to stop\n\Z")
        conductor.assert_called_once_with(LOGIN, LABEL)
        changed = shutdown_store.find_session(LOGIN, "alpha-id")
        assert changed is not None
        self.assertEqual(changed["force"], "now")
        self.assertEqual(shutdown_store.find_session(LOGIN, "beta-id"), refused)
        self.assertEqual(shutdown_store.find_session(LOGIN, "gamma-id"), before[2])

    def test_up_json_returns_one_object_with_structured_refusals(self) -> None:
        _ = self.save_three({"kind": "waiting"})
        output = io.StringIO()
        with patch.object(restart_work, "_session_liveness", return_value=restart_work.SessionLive()), redirect_stdout(output):
            self.assertEqual(shutdown.main(["up", LOGIN, "--here", "--json", "--only", "beta-id"]), 1)
        result = restart_work.parse_peer_result(output.getvalue())
        self.assertEqual(result["outcome"], "nothing")
        self.assertEqual(result["lines"], ["Beta: already running; skipped",
                                         "Beta is waiting, not stopped; nothing to restart"])
        self.assertEqual(cast(dict[str, object], json.loads(output.getvalue()))["no_remote_control"], [])
        self.assertEqual(result["refused"], [{"kind": "not stopped", "session_id": "beta-id", "name": "Beta", "progress": "waiting"}])

    def test_unit_restart_without_prompt_submission_reports_the_named_failure_and_push(self) -> None:
        _ = self.save_three(shutdown_store.SessionStopped(kind="stopped", at=OCTOBER))
        with (patch.object(restart_work, "_launch_session", return_value=restart_work.SessionLaunched()) as launch,
              patch.object(settle, "alert_user") as alert):
            code, output, error = self.action("restart", "beta-id")
        self.assertEqual(code, 1, (output, error))
        self.assertEqual(launch.call_count, 1)
        self.assertIn("Beta", output)
        self.assertIn("restart prompt not submitted", output)
        saved = shutdown_store.find_session(LOGIN, "beta-id")
        assert saved is not None
        self.assertEqual(saved["progress"]["kind"], "restart failed")
        alert.assert_called_once()
        push = cast(RenderedPush, alert.call_args.args[0])
        self.assertIn("Beta", push.title + "\n" + push.body)

    def test_up_json_keeps_remote_control_quiet_and_emits_legacy_empty_key(self) -> None:
        _ = self.save_three({"kind": "stopped", "at": OCTOBER})
        def quiet(current: shutdown_store.SessionShutdown) -> None:
            current["alerts"] = "none"
        _ = shutdown_store.update_session(LOGIN, "beta-id", quiet)
        with patch.object(sessions, "remote_control", return_value="absent"), patch.object(restart_work, "wait_for_remote_control", return_value=frozenset()), patch.object(remote_control, "launch_fixer") as fixer, redirect_stdout(output := io.StringIO()):
            self.assertEqual(shutdown.main(["up", LOGIN, "--here", "--json", "--only", "beta-id"]), 0)
        result = restart_work.parse_peer_result(output.getvalue())
        self.assertFalse(any("Remote Control" in line for line in result["lines"]))
        fixer.assert_called_once()
        self.assertEqual(fixer.call_args.args[0], "beta-id")
        self.assertEqual(fixer.call_args.args[-2:], ("alpha unit", "Beta"))
        self.assertEqual(cast(dict[str, object], json.loads(output.getvalue()))["no_remote_control"], [])
        self.assertIsNone(shutdown_store.find_session(LOGIN, "beta-id"))



if __name__ == "__main__":
    _ = unittest.main()
