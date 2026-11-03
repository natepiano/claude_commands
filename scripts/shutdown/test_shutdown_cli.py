"""Tests for shutdown commands and records."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.whoami.note_file import note_name

from unit_staff_test_support import unit_host
from scripts.production import staff_sources
import io
from datetime import datetime
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import unittest
from collections.abc import Iterable
from contextlib import redirect_stderr, redirect_stdout
from typing import Literal, cast, final, override
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_support import registry_confirmation, ShutdownTestCase
import inventory
import herdr
from test_shutdown_record import LegacyRecordFixture, LegacyEntryFixture, write_old
import shutdown_record as shutdown_store
import sessions
import remote_control
import shutdown
import restart_support
import remote
from push_texts import RenderedPush
from user_action import ActionRequired
import restart as restart_work
from scripts.whoami.account import Account, UnreadableAccount


# Hang guard: natedev reached load 136 on 2026-10-29; scheduling delays are not failures.
HANG_GUARD_SECONDS = 300

LOGIN = "owner@example.com"
LABEL = "claude2"
OCTOBER = "2026-10-09T21:49:10+00:00"
JANUARY = "2026-01-15T17:30:00+00:00"
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
        host=unit_host(
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


    def test_elapsed_text_uses_seconds_minutes_hours_and_days(self) -> None:
        cases = ((-1, "0s"), (0, "0s"), (59, "59s"), (60, "1m"),
                 (3599, "59m"), (3600, "1h"), (86399, "23h"),
                 (86400, "1d"), (172801, "2d"))
        for seconds, expected in cases:
            with self.subTest(seconds=seconds):
                self.assertEqual(restart_support.elapsed_text(seconds), expected)


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
        third = 'third@example.com'
        _ = (Path(self.environment['AGENT_NOTES_DIR']) / 'claude3.md').write_text(f'---\nlogin: {third}\n---\n')

        def reports(login: str) -> inventory.Inventory:
            return inventory.Inventory(machine='natedev', login=login, label=LABEL if login == LOGIN else 'claude3', sessions=[], unattributed=[])
        for login, label in [(LOGIN, LABEL), (third, 'claude3')]:
            saved_hold = account_hold()
            saved_hold.update(login=login, label=label)
            shutdown_store.set_account_hold(saved_hold)
        with patch.dict(os.environ, {**self.environment, 'CLAUDE_CODE_SESSION_ID': ''}), patch.object(shutdown, 'inventory', side_effect=reports), patch.object(shutdown, 'run_remote', side_effect=AssertionError('--here read peer')), redirect_stdout(io.StringIO()):
            self.assertEqual({hold['login'] for hold in shutdown_store.all_account_holds()}, {LOGIN, third})
            self.assertEqual(shutdown.main(['restart', '--here', '--no-alert']), 0)
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
        selected = Account('claude', LOGIN, LABEL)
        report = inventory.Inventory(machine='natedev', login=LOGIN, label=LABEL, sessions=[], unattributed=[])
        with patch.dict(os.environ, {'CLAUDE_CODE_SESSION_ID': 'caller-id'}), patch.object(shutdown, 'own_claude_account', return_value=selected), patch.object(shutdown, 'inventory', return_value=report), patch.object(shutdown, '_terminal_accounts', side_effect=AssertionError('expanded accounts')), patch.object(restart_work, 'restart', return_value=0) as resume, redirect_stdout(io.StringIO()):
            self.assertEqual(shutdown.main(['status', '--here', '--json']), 0)
            self.assertEqual(shutdown.main(['restart', '--here']), 0)
        self.assertEqual(resume.call_args.args[0], restart_work.AccountNotNamed())


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
                for text in (status_text,):
                    self.assertIn("\n  last screen of the failed prompt submit:\n    Claude fixture\n    ❯ unsent opening prompt\n", text)
                    self.assertEqual(text.count("last screen of the failed prompt submit:"), 1)
                    if isinstance(liveness, restart_work.SessionLive):
                        command = restart_support.shutdown_command("forget", "session-123", "--account", LOGIN)
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

                def read_peer(arguments: list[str]) -> tuple[int, str]:
                    if arguments == ["status", LOGIN, "--json", "--here"]:
                        return 0, json.dumps(peer)
                    self.assertEqual(arguments, ["records", "--json", "--here"])
                    return 0, json.dumps([saved])

                with (patch.object(remote, "local_machine", return_value=local_machine),
                      patch.object(shutdown, "other_machine", return_value=peer_machine),
                      patch.object(shutdown, "inventory", return_value=local),
                      patch.object(shutdown, "run_remote", side_effect=read_peer) as remote_read,
                      patch.object(shutdown, "named_claude_account", return_value=Account("claude", LOGIN, LABEL)),
                      patch.object(restart_work, "_owner_liveness",
                                   side_effect=AssertionError("peer liveness must come from its inventory")),
                      patch.object(restart_work, "_session_liveness",
                                   side_effect=AssertionError("peer liveness must come from its inventory")),
                      redirect_stdout(io.StringIO()) as output):
                    self.assertEqual(shutdown.main(["status", LOGIN]), 0)
                self.assertEqual([call.args[0] for call in remote_read.call_args_list],
                                 [["status", LOGIN, "--json", "--here"], ["records", "--json", "--here"]])
                command = restart_support.shutdown_command("forget", "peer-session", "--account", LOGIN)
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
        self.assertNotIn("last screen of the failed prompt submit:", "\n".join(lines))

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
        self.assertEqual(printed, restart_support.shutdown_command("forget", "session-123", "--account", LOGIN))
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


    def write_record(self, live: LegacyRecordFixture) -> None:
        self.clear_state()
        write_old(live)


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


    def test_status_reads_filename_conflict_without_migrating_records(self) -> None:
        _ = self.conflicting_legacy_record('session ids')
        before = self.store_contents()
        report = inventory.Inventory(machine='natedev', login=LOGIN, label=LABEL, sessions=[], unattributed=[])
        output, error = (io.StringIO(), io.StringIO())
        with patch.object(shutdown, 'inventory', return_value=report), redirect_stdout(output), redirect_stderr(error):
            result = shutdown.main(['status', LOGIN, '--here'])
        self.assertEqual(result, 0)
        self.assertEqual(error.getvalue(), '')
        self.assertEqual(output.getvalue().count('  alpha: waiting'), 2)
        self.assertIn(f'{LABEL} · {LOGIN}\n', output.getvalue())
        self.assertNotIn('Traceback', output.getvalue())
        self.assertEqual(self.store_contents(), before)


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


    def test_removed_writer_verbs_are_rejected(self) -> None:
        removed = ("down", "now", "ready", "cancel", "conduct", "follow", "begin", "refresh", "retry-stop", "claim-stop", "close-failed-stop")
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
        _ = cast(object, self.enterContext(patch.dict(os.environ, {'HOME': str(self.root), 'SHUTDOWN_STATE_DIR': str(self.root / 'state')})))
        _ = self.enterContext(patch.object(restart_work, '_folder_exists', return_value=True))
        _ = self.enterContext(patch.object(restart_work, '_prepare_restore_server'))
        _ = self.enterContext(patch.object(herdr.Herdr, 'close_restore_placeholders'))
        _ = self.enterContext(patch.object(sessions, 'remote_control', return_value='active'))
        self.owners = []
        for identity, name in (('alpha-id', 'Alpha'), ('beta-id', 'Beta'), ('gamma-id', 'Gamma')):
            owner = unit_session(identity)
            owner['name'] = name
            self.owners.append(owner)
        selected = Account('claude', LOGIN, LABEL)
        _ = self.enterContext(patch.object(shutdown, 'named_claude_account', return_value=selected))
        _ = self.enterContext(patch.object(restart_work, 'named_claude_account', return_value=selected))
        report = inventory.Inventory(machine='natedev', login=LOGIN, label=LABEL, sessions=self.owners, unattributed=[])

        def listed(_login: str) -> list[inventory.ListedSession]:
            return [inventory.ListedSession(owner['session_id'], owner['pid'], owner['proc_start'], owner['name'], owner['kind'], self.root / '.claude', LOGIN, staff_sources.unit_parent(owner['host']['unit']).slug if owner['kind'] == 'unit' else '', '') for owner in self.owners]
        _ = self.enterContext(patch.object(shutdown, 'listing', side_effect=listed))

        def observed(login: str, only: Iterable[str]=()) -> inventory.Inventory:
            self.assertEqual(login, LOGIN)
            ids = tuple(only)
            return {**report, 'sessions': [owner for owner in self.owners if not ids or owner['session_id'] in ids]}
        _ = self.enterContext(patch.object(shutdown, 'inventory', side_effect=observed))


        _ = self.enterContext(patch.object(restart_support, 'send_message', return_value=restart_support.MessageSent(kind='sent')))
        _ = self.enterContext(patch.object(restart_work, '_retire'))
        _ = self.enterContext(patch.object(restart_work, '_send_restart_note', return_value=restart_support.MessageSent(kind='sent')))
        _ = self.enterContext(patch.object(restart_work, '_live_session_ids', return_value=frozenset()))

        def confirmed(ids: frozenset[str], _timeout: float) -> restart_work.RegistryConfirmation:
            return registry_confirmation(ids)
        _ = self.enterContext(patch.object(restart_work, 'wait_for_confirmation', side_effect=confirmed))
        _ = self.enterContext(patch.object(restart_work, '_launch_session', return_value=restart_work.SessionLaunched(herdr.PromptSubmitted(datetime.fromisoformat(OCTOBER)))))
        _ = self.enterContext(patch.object(restart_work, '_prompt_outcome', return_value=herdr.PromptSubmitted(datetime.fromisoformat(OCTOBER))))

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
        for selection in ("beta-id", "Beta"):
            with self.subTest(selection=selection):
                state = self.root / "state"
                if state.exists():
                    shutil.rmtree(state)
                before = self.save_three({"kind": "stopped", "at": OCTOBER})
                status, _output, error = self.action("restart", selection)
                self.assertEqual((status, error), (0, ""))
                self.assertEqual(shutdown_store.find_session(LOGIN, "alpha-id"), before[0])
                self.assertEqual(shutdown_store.find_session(LOGIN, "gamma-id"), before[2])
                self.assertIsNone(shutdown_store.find_session(LOGIN, "beta-id"))

    def test_terminal_only_routes_selection_to_its_account_for_down_restart_and_status(self) -> None:
        selected = [Account('claude', LOGIN, LABEL), Account('claude', 'other@example.com', 'claude3')]
        real_resolve = shutdown.resolve_sessions

        def resolve(login: str, items: Iterable[str], *, here: bool=False, recovery: bool=False) -> shutdown.ResolvedSessions:
            if login != LOGIN:
                raise shutdown.SessionNotFound('Beta: matches no session')
            return real_resolve(login, items, here=here, recovery=recovery)
        for verb in ('restart', 'status'):
            with self.subTest(verb=verb), patch.dict(os.environ, {'CLAUDE_CODE_SESSION_ID': ''}), patch.object(shutdown, '_terminal_accounts', return_value=shutdown.TerminalAccountSelection(tuple(selected))), patch.object(shutdown, 'resolve_sessions', side_effect=resolve), patch.object(restart_work, 'restart', return_value=0) as resume, patch.object(shutdown, 'run_remote', side_effect=AssertionError('--here read peer')), redirect_stdout(io.StringIO()) as output:
                args = [verb, '--only', 'Beta', '--here']
                if verb == 'status':
                    args.append('--json')
                self.assertEqual(shutdown.main(args), 0)
                if verb == 'restart':
                    self.assertEqual(resume.call_args.args[0], restart_work.AccountNamed(LOGIN))
                    self.assertEqual(resume.call_args.kwargs['only'], ['beta-id'])
                else:
                    reports = cast(list[dict[str, object]], json.loads(output.getvalue()))
                    self.assertEqual([report['login'] for report in reports], [LOGIN])
                    owners = cast(list[dict[str, object]], reports[0]['sessions'])
                    self.assertEqual([owner['session_id'] for owner in owners], ['beta-id'])


    def test_selected_status_filters_peer_sessions_without_requiring_a_peer_match(self) -> None:
        peer = inventory.Inventory(machine="Mac", login=LOGIN, label=LABEL, sessions=self.owners, unattributed=[])
        with patch.object(shutdown, "run_remote", return_value=(0, json.dumps(peer))) as remote:
            reports = shutdown._status(Account("claude", LOGIN, LABEL), False, ("beta-id",))  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(remote.call_args.args[0], ["status", LOGIN, "--json", "--here"])
        self.assertEqual(len(reports), 2)
        self.assertIn("sessions", reports[1])
        peer_report = cast(inventory.Inventory, reports[1])
        self.assertEqual([owner["session_id"] for owner in peer_report["sessions"]], ["beta-id"])


    def test_only_restart_refuses_waiting_ready_and_stopping_with_exact_line(self) -> None:
        cases: list[shutdown_store.SessionProgress] = [{"kind": "waiting"}, {"kind": "ready", "at": OCTOBER}, {"kind": "stopping", "at": OCTOBER}, shutdown_store.SessionHeldByUsageLimit(kind="held by usage limit", at=OCTOBER)]
        for progress in cases:
            with self.subTest(progress=progress["kind"]):
                state = self.root / "state"
                if state.exists():
                    shutil.rmtree(state)
                before = self.save_three(progress)
                with (patch.object(restart_work, "_owner_liveness", return_value=restart_work.SessionLive()),
                      patch.object(restart_work, "_launch_session") as launch):
                    status, output, error = self.action("restart", "Beta", "--no-alert")
                self.assertEqual((status, error), (1, ""))
                self.assertIn(f"Beta is {progress['kind']}, not stopped; nothing to restart\n", output)
                launch.assert_not_called()
                self.assertEqual(shutdown_store.session_records(LOGIN), before)


    def test_only_help_is_public_for_each_action(self) -> None:
        for verb in ('restart',):
            with self.subTest(verb=verb), redirect_stdout((output := io.StringIO())):
                with self.assertRaises(SystemExit) as exit_status:
                    _ = shutdown.main([verb, '--help'])
                self.assertEqual(exit_status.exception.code, 0)
                self.assertIn('--only NAME[,NAME...]', output.getvalue())
                self.assertNotIn('==SUPPRESS==', output.getvalue())

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
            self.assertEqual(shutdown.main(["status", LOGIN, "--only", "Missing"]), 2)
        self.assertIn("Missing", output.getvalue() + error.getvalue())
        self.assertIn("Mac", error.getvalue())
        self.assertIn("not reached", error.getvalue())


    def test_only_verbs_route_peer_id_without_changing_local_records(self) -> None:
        before = self.save_three({'kind': 'waiting'})
        remote_owner = unit_session('remote-id')
        remote_owner['name'] = 'Remote'
        remote_record = session_shutdown({'kind': 'stopped', 'at': OCTOBER}, 'remote-id')
        remote_record['session'], remote_record['machine'] = (remote_owner, 'Mac')
        report = inventory.Inventory(machine='Mac', login=LOGIN, label=LABEL, sessions=[remote_owner], unattributed=[])
        for verb in ('restart',):
            for selection in ('remote-id', 'Remote'):
                with self.subTest(verb=verb, selection=selection):
                    calls: list[list[str]] = []

                    def peer(arguments: list[str], **_keywords: object) -> tuple[int, str]:
                        calls.append(arguments)
                        if arguments[0] == 'records':
                            return (0, json.dumps([remote_record]))
                        if arguments[0] == 'status':
                            return (0, json.dumps(report))
                        return (0, 'Remote: action completed')
                    output, error = (io.StringIO(), io.StringIO())
                    with patch.object(shutdown, 'run_remote', side_effect=peer), patch.object(restart_support, 'run_remote', side_effect=peer), patch.object(restart_work, 'run_remote', side_effect=peer), redirect_stdout(output), redirect_stderr(error):
                        self.assertEqual(shutdown.main([verb, LOGIN, '--only', selection, '--no-alert'] if verb == 'restart' else [verb, LOGIN, '--only', selection]), 0)
                    action = calls[-1]
                    self.assertEqual(action[0], 'up' if verb == 'restart' else verb)
                    self.assertEqual(action[1], LOGIN)
                    self.assertEqual(action[action.index('--only') + 1], 'remote-id')
                    if verb != 'restart':
                        self.assertIn('--here', action)
                    self.assertEqual(shutdown_store.session_records(LOGIN), before)

    def test_selected_live_local_session_without_record_answers_without_peer(self) -> None:
        for verb in ('restart',):
            with self.subTest(verb=verb), patch.object(shutdown, 'run_remote') as selection_peer, patch.object(restart_support, 'run_remote') as action_peer, patch.object(restart_work, 'run_remote') as restart_peer, patch.object(restart_support, 'inventory', return_value=inventory.Inventory(machine='natedev', login=LOGIN, label=LABEL, sessions=[self.owners[1]], unattributed=[])) as observed:
                output, error = (io.StringIO(), io.StringIO())
                with redirect_stdout(output), redirect_stderr(error):
                    status = shutdown.main([verb, LOGIN, '--only', 'beta-id'])
                self.assertEqual((status, output.getvalue(), error.getvalue()), (1, 'Beta: no shutdown in progress\n', ''))
                selection_peer.assert_not_called()
                action_peer.assert_not_called()
                restart_peer.assert_not_called()
                if verb == 'restart':
                    observed.assert_called_once_with(LOGIN, frozenset({'beta-id'}))

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
        with patch.object(restart_work, "_launch_session", side_effect=OSError("launch failed")), patch.object(restart_work, "run_remote") as peer, patch.object(restart_support, "alert_user") as alert:
            status, _output, error = self.action("restart", "beta-id")
        self.assertEqual((status, error), (1, ""))
        alert.assert_not_called()
        peer.assert_not_called()
        self.assertEqual(shutdown_store.find_session(LOGIN, "alpha-id"), before[0])
        self.assertEqual(shutdown_store.find_session(LOGIN, "gamma-id"), before[2])

    def test_selected_restart_refusal_has_no_alert_count_or_summary(self) -> None:
        _ = self.save_three({"kind": "waiting"})
        with patch.object(restart_work, "_session_liveness", return_value=restart_work.SessionLive()), patch.object(restart_support, "alert_user") as alert, patch.object(restart_work, "_print_result") as summary:
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
        with patch.object(restart_work, "_launch_session", side_effect=OSError("window launch failed")), patch.object(restart_work, "_session_liveness", side_effect=liveness), patch.object(restart_support, "alert_user") as alert:
            status, output, error = self.action("restart", "alpha-id,beta-id")
        self.assertEqual((status, error), (1, ""))
        self.assertIn("Alpha is waiting, not stopped; nothing to restart\n", output)
        alert.assert_called_once()
        push = cast(RenderedPush, alert.call_args.args[0])
        self.assertEqual(push.id, "shutdown.restart_failed")
        self.assertEqual(push.values["stopped"], "1 session (Beta)")
        self.assertIn("window launch failed", output)

    def test_selected_restart_of_a_failed_move_pushes_a_retry_that_keeps_the_target(self) -> None:
        _ = self.save_three({"kind": "stopped", "at": OCTOBER})
        def move(current: shutdown_store.SessionShutdown) -> None:
            current["target"], current["source_label"] = "claude3", current["label"]
        _ = shutdown_store.update_session(LOGIN, "beta-id", move)
        with (patch.object(restart_work, "_launch_session", side_effect=OSError("pane still holds a live agent")),
              patch.object(restart_work, "_session_liveness", return_value=restart_work.SessionNotLive()),
              patch.object(restart_support, "alert_user") as alert):
            status, _output, error = self.action("restart", "beta-id")
        self.assertEqual((status, error), (1, ""))
        alert.assert_called_once()
        push = cast(RenderedPush, alert.call_args.args[0])
        self.assertEqual(push.id, "shutdown.restart_failed")
        self.assertEqual(push.values["stopped"], "1 session (Beta)")
        self.assertIn('/shutdown restart "claude2"', cast(ActionRequired, push.action).text)
        saved = shutdown_store.find_session(LOGIN, "beta-id")
        assert saved is not None
        self.assertEqual(saved.get("target"), "claude3")


    def test_selected_restart_fresh_claim_names_existing_worker_without_launch_or_alert(self) -> None:
        _ = self.save_three({"kind": "stopped", "at": OCTOBER})
        def claim(current: shutdown_store.SessionShutdown) -> None:
            current["progress"] = {"kind": "restarting", "at": restart_support.record_time(), "prior": {"kind": "stopped", "at": OCTOBER}}
        saved = shutdown_store.update_session(LOGIN, "beta-id", claim)
        with patch.object(restart_work, "_launch_session") as launch, patch.object(restart_support, "alert_user") as alert:
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
              patch.object(restart_support, "alert_user") as alert):
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
                      redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as error):
                    code = shutdown.main(args)
                self.assertEqual(code, 1)
                self.assertIn("Alpha: matches more than one session", error.getvalue())
                self.assertIn("natedev: Alpha (alpha-id)", error.getvalue())
                self.assertIn("Mac: Alpha (peer-alpha-id)", error.getvalue())
                launched = [cast(shutdown_store.SessionShutdown, call.args[0])["session"]["name"] for call in launch.call_args_list]
                self.assertCountEqual(launched, ["Beta", "Gamma"])
                self.assertEqual([saved["session"]["name"] for saved in shutdown_store.session_records(LOGIN)], ["Alpha"])



if __name__ == "__main__":
    _ = unittest.main()
