"""Claude account-move reminders and Codex quota delivery use isolated state."""

from __future__ import annotations

from scripts.whoami.note_file import note_name

import io
import fcntl
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast, override
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import quota_alert
import review
from push_texts import RenderedPush, render
from scripts.whoami import agent_accounts
from agent_accounts import ClaudeAccountsInUse, CodexHomeSource, Quota, Report
import agent_notes
import session_table
import note_file
import account
import usage_store
from agent_notes import local_reset
from note_file import read_note
from scripts.accounts import accounts, moves, next_moves, refusals, role_map
from scripts.production import showrunners

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "message"))
import send
# send puts scripts/notify on the path.
from user_action import ActionRequired, UserAction, parse_user_action

Notes = list[quota_alert.AgentNote]

REGISTRY = """\
[assignments]
direct=codex
cli=claude
module_review=codex   # a trailing comment
ask_a_friend=caller

[direct.codex]
impl=gpt-6-sol:high
[direct.claude]
impl=opus:high
[cli.codex]
commit_prep=gpt-6-sol:high
[cli.claude]
commit_prep=sonnet:high
[module_review.codex]
review=gpt-6-sol:xhigh
[module_review.claude]
review=opus:max
[ask_a_friend.codex]
consultation=gpt-6-sol:max
[ask_a_friend.claude]
consultation=opus:max

[codex.agents]
gpt-6-sol=low,medium,high,xhigh,max
[claude.agents]
opus=low,medium,high,xhigh,max
sonnet=low,medium,high
"""

REAL_RECIPIENTS = quota_alert.recipients
REAL_TELL_USER = quota_alert.tell_user
REFUSAL_ACTION = "At chatgpt.com/codex/settings/usage, add credits for account codex1."


def running(*names: str) -> list[quota_alert.Showrunner]:
    """Running showrunners called `names`, as their update timers and session records would give them."""
    return [quota_alert.Showrunner(session=name, socket=f"/{name}.sock", slug=name.replace(" ", "-"),
                                   zone="America/Los_Angeles", doc="") for name in names]


def configured_recipients(config: quota_alert.Config, here: str | None = None) -> list[str]:
    del config
    return [name for name in ("natedev", "boss of bosses") if name != here]


def setUpModule() -> None:
    """The times these tests expect are Pacific; the user's zone setting must not change them."""
    unittest.enterModuleContext(mock.patch.dict(os.environ, {"USER_ZONE": "America/Los_Angeles"}))
    _ = unittest.enterModuleContext(mock.patch.object(role_map, "is_role_map_host", return_value=True))
    _ = unittest.enterModuleContext(mock.patch.object(role_map, "ssh_role_map", side_effect=AssertionError("unexpected ssh")))
    root = Path(unittest.enterModuleContext(tempfile.TemporaryDirectory())).resolve()
    _ = unittest.enterModuleContext(mock.patch.object(showrunners, "NOTIFIER_STATE_DIR", root))


class QuotaAlertTests(unittest.TestCase):
    def test_shutdown_hold_has_its_own_blocked_head_line(self) -> None:
        detail = "accounts: claude4 is held by a shutdown; no session was stopped"
        reason = quota_alert.TargetHeldByShutdown(detail)
        self.assertEqual(quota_alert.blocked_next_move_line("units", "claude4", "head", reason),
                         "units 1 → claude4 due, " + detail)
        self.assertNotEqual(quota_alert.blocked_next_move_reason_key(reason),
                            quota_alert.blocked_next_move_reason_key(quota_alert.TargetPreparationRefused(detail)))

    def test_failed_login_renewal_has_its_own_blocked_head_line(self) -> None:
        reason = quota_alert.TargetLoginRenewalFailed("timed out after 120 s")
        self.assertEqual(quota_alert.blocked_next_move_line("units", "claude4", "head", reason),
            "units 1 → claude4 due, claude4's login expired, and a probe session on it failed "
            + "(timed out after 120 s). Log in on claude4 by hand.")
        self.assertNotEqual(quota_alert.blocked_next_move_reason_key(reason),
            quota_alert.blocked_next_move_reason_key(quota_alert.TargetReadingUnavailable(quota_alert.NoTargetReading())))

    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root: Path = Path()
        self.now: datetime = datetime.now(timezone.utc).replace(microsecond=0)
        self.resets: str = local_reset(self.now + timedelta(days=2))
        self.failing: set[str] = set()
        self.sent: list[tuple[str, str]] = []
        self.push_errors: list[quota_alert.PushDelivery] = []
        self.pushed: list[tuple[str, str, quota_alert.Ask]] = []
        self.noted: list[RenderedPush] = []

    @override
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        _ = self.enterContext(mock.patch.object(session_table, "live_sessions", return_value=session_table.ReadSessions({}, frozenset())))
        environment_context = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            "HOME": str(self.root), "AGENT_NOTES_DIR": str(self.root),
        })))
        del environment_context
        (self.root / ".claude/config").mkdir(parents=True)
        _ = (self.root / ".claude/config/accounts.json").write_text(
            json.dumps({"workers": "codex1"}))
        _ = (self.root / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "someone@example.com"}}))
        codex_home = self.root / ".codex"
        codex_home.mkdir()
        claims = base64.urlsafe_b64encode(json.dumps({"email": "someone@example.com"}).encode()).decode().rstrip("=")
        _ = (codex_home / "auth.json").write_text(json.dumps({"tokens": {"id_token": f"header.{claims}.signature"}}))
        _ = self.enterContext(mock.patch.object(note_file, "AGENTS_DIR", self.root))
        _ = self.enterContext(mock.patch.object(agent_notes, "READINGS_LOG", self.root / "readings.jsonl"))
        config = self.root / "showrunners.json"
        _ = config.write_text(json.dumps({"threshold_percent": 1, "repeat_minutes": 30,
                                          "stall_minutes": 5, "faults_to": "natedev", "always": ["natedev"]}))
        registry = self.root / "agents.conf"
        _ = registry.write_text(REGISTRY)
        for name, value in (("CONFIG", config), ("STATE", self.root / "state.json"), ("relay", self.relay),
                            ("tell_user", self.tell_user), ("REGISTRY", registry)):
            patcher = mock.patch.object(quota_alert, name, value)
            _ = patcher.start()
            self.addCleanup(patcher.stop)
        recipients_patch = mock.patch.object(quota_alert, "recipients", configured_recipients)
        _ = recipients_patch.start()
        self.addCleanup(recipients_patch.stop)
        # A fresh sync stamp and no Codex config keep agents_config.sh from syncing the catalog into the fixture.
        synced = self.root / "catalog synced"
        synced.touch()
        for key, value in (("CODEX_CATALOG_SYNC_STATE_FILE", synced), ("CODEX_CONFIG_FILE", self.root / "absent"),
                           ("CODEX_MODELS_CACHE_FILE", self.root / "absent")):
            self.addCleanup(self.restore_environment, key, os.environ.get(key))
            os.environ[key] = str(value)

    @staticmethod
    def restore_environment(key: str, value: str | None) -> None:
        if value is None:
            _ = os.environ.pop(key, None)
        else:
            os.environ[key] = value

    def relay(self, recipient: str, text: str, key: str | None) -> str | None:
        del key
        self.sent.append((recipient, text))
        return "not reachable" if recipient in self.failing else None

    def tell_user(self, push: RenderedPush) -> quota_alert.PushDelivery:
        """Record direct delivery so no test can reach send.py's real user channel."""
        self.assertNotIn("/", push.body)
        self.assertNotIn("@", push.body)
        if push.need == "note":
            self.noted.append(push)
            return self.push_errors.pop(0) if self.push_errors else quota_alert.PushSent()
        assert isinstance(push.action, ActionRequired)
        assert push.need in ("decision", "blocked")
        self.pushed.append((push.title, push.body, quota_alert.Ask(push.need, push.action.text)))
        return self.push_errors.pop(0) if self.push_errors else quota_alert.PushSent()

    def user_sends(self, deliver: Callable[[], object]) -> dict[str, tuple[str, UserAction]]:
        """Each user message the real tell_user sends during `deliver`, by title, as send.py's parser takes it.

        subprocess.run is faked, so nothing is sent; a command line send.py refuses fails the test with its reason.
        """
        commands: list[list[str]] = []

        def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, "SENT: to the user", "")

        with mock.patch.object(quota_alert, "tell_user", REAL_TELL_USER), mock.patch.object(subprocess, "run", run):
            _ = deliver()
        sends: dict[str, tuple[str, UserAction]] = {}
        for command in commands:
            self.assertEqual(command[:2], [sys.executable, str(quota_alert.SEND)])
            with redirect_stderr(io.StringIO()) as refusal:
                try:
                    options = send.parse(command[2:])
                except SystemExit:
                    self.fail(f"send.py refused {command[2:]}: {refusal.getvalue().strip().splitlines()[-1]}")
            from push_texts import render
            values = dict(value.split("=", 1) for value in command[command.index("--text-id") + 2:] if "=" in value)
            push = render(command[command.index("--text-id") + 1], **values)
            options = options._replace(summary=push.title, action=push.action)
            assert options.summary is not None and not isinstance(options.action, send.NoUserRecipient)
            if isinstance(options.action, ActionRequired):
                # Placeholder text such as "none" or "n/a" is refused.
                self.assertEqual(parse_user_action(options.action.text, no_action=False), options.action)
            sends[options.summary] = (options.need, options.action)
        return sends

    def recipients(self) -> list[str]:
        found = sorted(recipient for recipient, _ in self.sent)
        self.sent.clear()
        return found

    def note(self, name: str, state: str, left: str, resets: str | None = None, *,
             credit_balance: str | None = None, limit_reset_count: str | None = None,
             login: str = "someone@example.com") -> quota_alert.AgentNote:
        path = self.root / name
        extras = "".join(f"{key}: {value}\n" for key, value in (
            ("credit_balance", credit_balance), ("limit_reset_count", limit_reset_count)) if value is not None)
        _ = path.write_text(f"---\nlogin: {login}\nresets: {resets or self.resets}\n"
                            + f"role: {'worker' if state == 'active' and name.startswith('codex') else 'unit' if state == 'active' else '[]'}\nsessions: 0\nweekly_remaining_usage: {left}\n{extras}---\n")
        note = read_note(path)
        assert isinstance(note, note_file.Note)
        return note

    def alert_readings(self, notes: Notes, now: datetime) -> quota_alert.CodexAlertPassReadings:
        selected = next((note for note in notes if note.tool == "codex" and "worker" in note.get_list("role")), None)
        home = self.root / ".codex"
        workers = accounts.CodexWorkersAccount(label=selected.label if selected is not None else "~/.codex", home=home)
        if selected is None:
            return quota_alert.CodexAlertPassReadings(workers,
                agent_accounts.UnknownCodexAccount(home, "fixture has no selected Codex account"), ())
        account = agent_accounts.KnownCodexAccount(home, selected.get("login") or "someone@example.com",
                                               selected.label)
        left = selected.get("weekly_remaining_usage") or "null"
        reset = datetime.fromisoformat(selected.get("resets") or self.resets).astimezone(timezone.utc)
        live = Report("Codex", email=account.login, source=CodexHomeSource(home),
            quotas=[Quota("Weekly", 100 - float(left), reset)] if left != "null" else [],
            five_hour=Quota("5-hour", 0, reset), credit_balance=selected.get("credit_balance"))
        return quota_alert.CodexAlertPassReadings(workers, account,
            (quota_alert.CodexHomeReading(home, live, now),))

    def test_a_claim_during_a_codex_save_keeps_both_results(self) -> None:
        home = self.root / ".codex"
        login = "reader@example.invalid"
        worker = agent_accounts.KnownCodexAccount(home, "worker@example.invalid", "codex1")
        report = Report("Codex", email=worker.login, quotas=[Quota("Weekly", 40, None)])
        readings = quota_alert.CodexAlertPassReadings(
            accounts.CodexWorkersAccount(worker.label, home), worker,
            (quota_alert.CodexHomeReading(home, report, self.now),))
        merging, claiming = Event(), Event()
        merge = usage_store.merge

        def merge_while_claim_starts(stored: usage_store.UsageReadings,
                                    reports: list[Report], at: datetime) -> bool:
            merging.set()
            with usage_store.store_file().with_name("usage.lock").open("a") as lock:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertTrue(claiming.wait(timeout=2))
            return merge(stored, reports, at)

        def claim() -> agent_accounts.UsageReadClaim | agent_accounts.UsageReadHeld:
            self.assertTrue(merging.wait(timeout=2))
            claiming.set()
            return usage_store.claim_read(login, self.now)

        with (mock.patch.object(usage_store, "merge", side_effect=merge_while_claim_starts),
              ThreadPoolExecutor(max_workers=2) as pool):
            saved = pool.submit(quota_alert._save_codex_readings, readings, self.now)  # pyright: ignore[reportPrivateUsage]
            claimed = pool.submit(claim)
            saved.result(timeout=3)
            result = claimed.result(timeout=3)
        self.assertIsInstance(result, agent_accounts.UsageReadClaim)
        stored = usage_store.load()
        self.assertEqual(stored[login].get("attempted_at"), self.now.isoformat())
        self.assertEqual(stored[usage_store.key("Codex", worker.login)].get("weekly", {}).get("left"), 60)
        self.assertIsInstance(usage_store.claim_read(login, self.now + timedelta(seconds=1)),
                              agent_accounts.UsageReadHeld)

    def alert(self, notes: Notes, now: datetime | None = None, *,
              selection: ClaudeAccountsInUse | quota_alert.ReadCurrentClaudeSelection =
              quota_alert.READ_CURRENT_CLAUDE_SELECTION) -> list[str]:
        checked = now or self.now
        return quota_alert.alert(notes, checked, selection=selection,
                                  codex_readings=self.alert_readings(notes, checked))

    def block(self, notes: Notes, now: datetime | None = None, *,
              selection: ClaudeAccountsInUse | quota_alert.ReadCurrentClaudeSelection =
              quota_alert.READ_CURRENT_CLAUDE_SELECTION) -> list[str]:
        checked = now or self.now
        readings = self.alert_readings(notes, checked)
        assert not isinstance(readings.selected_account, agent_accounts.NoWorkersHome)
        return quota_alert.blocked(notes, readings.selected_account, checked,
                                    selection=selection, codex_readings=readings)

    def refresh(self, notes: Notes, here: str | None, now: datetime | None = None) -> list[str]:
        checked = now or self.now
        return quota_alert.refresh(notes, here, checked, codex_readings=self.alert_readings(notes, checked))

    def set_claude_activity(self, label: str) -> None:
        from scripts.message import sessions
        identity = accounts.account_identity(accounts.account_dir(label))
        assert isinstance(identity, agent_accounts.NumberedAccount)
        _ = self.enterContext(mock.patch.object(sessions, "live_sessions", return_value=[sessions.SessionRecord(
            pid=43210, sessionId="activity-fixture", name="live unit", messagingSocketPath="/fixture.sock",
            updatedAt=1, tmux="", formerNames=[])]))
        _ = self.enterContext(mock.patch.object(account, "account_of",
            return_value=account.Account("claude", identity.login, identity.label)))

    def stored_room(self, login: str, left: float, days: float) -> None:
        import usage_store
        saved = usage_store.load()
        saved[usage_store.key("Claude", login)] = usage_store.StoredUsage(weekly=usage_store.StoredWindow(
            left=left, at=datetime.now(timezone.utc).isoformat(),
            resets=(datetime.now(timezone.utc) + timedelta(days=days)).isoformat()))
        file = usage_store.store_file()
        file.parent.mkdir(parents=True, exist_ok=True)
        _ = file.write_text(json.dumps(saved))

    def configure_claude_roles(self) -> None:
        _ = (self.root / ".claude").mkdir(exist_ok=True)
        _ = (self.root / ".claude.json").write_text(
            json.dumps({"oauthAccount": {"emailAddress": "showrunner@example.invalid"}}), encoding="utf-8",
        )
        _ = (self.root / "claude2.md").write_text("---\nlogin: showrunner@example.invalid\n---\n")
        for label, login in (("claude1", "spare@example.invalid"), ("claude2", "showrunner@example.invalid"), ("claude3", "unit@example.invalid")):
            self.assertEqual(accounts.setup(label), [])
            _ = (self.root / note_name(label)).write_text(f"---\nlogin: {login}\n---\n")
            _ = (accounts.account_dir(label) / ".claude.json").write_text(
                json.dumps({"oauthAccount": {"emailAddress": login}}), encoding="utf-8",
            )
        self.set_claude_activity("claude3")

    def test_claude_reminder_pushes_once_at_two_percent_without_session_repeat_or_restore(self) -> None:
        config = quota_alert.load_config()
        config["threshold_percent"] = 2
        _ = quota_alert.CONFIG.write_text(json.dumps(config))
        _ = self.alert([self.note("claude2.md", "active", "3")], self.now)
        self.assertEqual(self.noted, [])
        _ = self.alert([self.note("claude2.md", "active", "2")], self.now + timedelta(minutes=2))
        self.assertEqual(len(self.noted), 1)
        self.assertEqual(self.noted[0].body,
            "claude2 has 2% of its weekly usage left, and no move is set up for the units or showrunners on it. When it runs out, they move to a Claude account with room, or stop if none has any.")
        self.assertEqual(self.noted[0].title, "Claude work on claude2 may stop")
        self.assertEqual(self.noted[0].action,
                         ActionRequired("In the natedev session, type /account_moves and the move you want, for example: when units reach 1% switch to claude2."))
        for minute, left in ((4, "1"), (32, "1"), (120, "null"), (122, "1")):
            _ = self.alert([self.note("claude2.md", "active", left)], self.now + timedelta(minutes=minute))
        self.assertEqual(len(self.noted), 1)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.pushed, [])
        _ = self.alert([self.note("claude2.md", "active", "80")], self.now + timedelta(minutes=124))
        self.assertEqual((self.sent, len(self.noted)), ([], 1))
        _ = self.alert([self.note("claude2.md", "active", "2")], self.now + timedelta(minutes=126))
        self.assertEqual(len(self.noted), 2)
        self.assertEqual(self.sent, [])

    def test_self_targeting_entry_does_not_silence_the_two_percent_claude_reminder(self) -> None:
        config = quota_alert.load_config()
        config["threshold_percent"] = 2
        _ = quota_alert.CONFIG.write_text(json.dumps(config))
        entry = next_moves.add_next_move("units", 1, "claude2")
        note = self.note("claude2.md", "active", "2")
        for minute in (0, 2, 32):
            _ = self.alert([note], self.now + timedelta(minutes=minute))
        self.assertEqual([push.values["account"] for push in self.noted], ["claude2"])
        self.assertEqual((self.sent, self.pushed), ([], []))
        self.assertEqual(next_moves.read_next_moves(), (entry,))

    def test_claude_account_used_only_by_ungrouped_sessions_sends_no_phone_reminder(self) -> None:
        note = self.note("claude2.md", "inactive", "1")
        _ = note.path.write_text(note.path.read_text().replace("sessions: 0", "sessions: 2"))
        note = note_file.read_note(note.path)
        assert isinstance(note, note_file.Note)
        self.assertTrue(note_file.in_use(note))
        self.assertEqual(note.get_list("role"), ())
        self.assertEqual(next_moves.read_next_moves(), ())
        for minute in (0, 2, 32):
            _ = self.alert([note], self.now + timedelta(minutes=minute))
        self.assertEqual((self.noted, self.pushed, self.sent), ([], [], []))

    def test_self_targeting_claude_head_with_a_later_move_still_sends_the_reminder(self) -> None:
        head = next_moves.add_next_move("units", 1, "claude2")
        later = next_moves.add_next_move("units", 1, "claude3")
        note = self.note("claude2.md", "active", "1")
        for minute in (0, 2, 32):
            _ = self.alert([note], self.now + timedelta(minutes=minute))
        self.assertEqual([push.id for push in self.noted], ["quota.claude_next_move"])
        self.assertEqual(self.noted[0].values["account"], "claude2")
        self.assertEqual(next_moves.read_next_moves(), (head, later))
        self.assertEqual((self.sent, self.pushed), ([], []))

    def test_claude_reminder_is_suppressed_only_when_every_group_on_the_account_has_an_entry(self) -> None:
        note = self.note("claude2.md", "active", "1")
        _ = note.path.write_text(note.path.read_text().replace("role: unit", "role: [units, showrunners]"))
        note = note_file.read_note(note.path)
        assert isinstance(note, note_file.Note)
        unit = next_moves.add_next_move("units", 1, "claude3")
        _ = next_moves.add_next_move("showrunners", 1, "claude1")
        _ = self.alert([note], self.now)
        _ = self.alert([note], self.now + timedelta(minutes=30))
        self.assertEqual((self.noted, self.sent, self.pushed), ([], [], []))
        next_moves.remove_next_move("showrunners", next_moves.GroupPosition(1))
        _ = self.alert([note], self.now + timedelta(minutes=32))
        self.assertEqual(len(self.noted), 1)
        _ = self.alert([note], self.now + timedelta(minutes=64))
        self.assertEqual(len(self.noted), 1)
        self.assertEqual(self.sent, [])
        self.assertEqual(next_moves.read_next_moves(), (unit,))

    def test_legacy_acknowledgement_does_not_silence_a_new_claude_next_move_reminder(self) -> None:
        _ = quota_alert.STATE.write_text(json.dumps({"episodes": {"claude2": {
            "since": self.now.isoformat(), "acknowledged": self.now.isoformat(), "push": "due", "last": {}}}}))
        _ = self.alert([self.note("claude2.md", "active", "1")], self.now)
        self.assertEqual(len(self.noted), 1)
        self.assertEqual(self.sent, [])
        document = cast(dict[str, object], json.loads(quota_alert.STATE.read_text()))
        episodes = cast(dict[str, dict[str, object]], document["episodes"])
        self.assertNotIn("acknowledged", episodes["claude2"])

    def test_claude_phone_failure_retries_delivery_without_sending_sessions_anything(self) -> None:
        self.push_errors.append(quota_alert.PushFailed("fixture offline"))
        note = self.note("claude2.md", "active", "1")
        first = self.alert([note], self.now)
        self.assertIn("quota push claude2 -> user: not delivered: fixture offline", first)
        _ = self.alert([note], self.now + timedelta(minutes=2))
        _ = self.alert([note], self.now + timedelta(minutes=32))
        self.assertEqual(len(self.noted), 2)
        self.assertEqual(self.sent, [])

    def test_low_notes_include_roles_or_sessions_but_exclude_unused_accounts(self) -> None:
        role_only = self.note("claude1.md", "active", "1")
        session_only = self.note("claude2.md", "inactive", "1")
        spare = self.note("claude3.md", "inactive", "1")
        _ = session_only.path.write_text(session_only.path.read_text().replace("sessions: 0", "sessions: 2"))
        session_only = read_note(session_only.path)
        assert isinstance(session_only, note_file.Note)
        notes = [role_only, session_only, spare]
        self.assertEqual([note.label for note in quota_alert.low(notes, 5)], ["claude1", "claude2"])

    def test_alert_opens_episodes_for_role_only_and_peer_sessions_only_accounts(self) -> None:
        """F001: a note's role or peer session count suffices without a local session."""
        role_only = self.note("claude1.md", "active", "1", login="role@example.invalid")
        peer_only = self.note("claude2.md", "inactive", "1", login="peer@example.invalid")
        _ = peer_only.path.write_text(peer_only.path.read_text().replace("sessions: 0", "sessions: 2"))
        peer_only = read_note(peer_only.path)
        assert isinstance(peer_only, note_file.Note)
        selection = ClaudeAccountsInUse(
            agent_accounts.NumberedAccount("claude3", Path.home() / ".claude", "local@example.invalid"),
            True, (), frozenset(),
        )

        _ = self.alert([role_only, peer_only], self.now, selection=selection)

        with quota_alert.state_file() as state:
            self.assertEqual(set(state["episodes"]), {"claude1", "claude2"})
        self.assertEqual(self.sent, [])
        self.assertEqual({push.values["account"] for push in self.noted}, {"claude1"})

    def test_alert_does_not_open_an_episode_for_an_unused_default_account(self) -> None:
        """F001: the default login alone does not make an unused note eligible."""
        note = self.note("claude1.md", "inactive", "1")
        selection = ClaudeAccountsInUse(
            agent_accounts.NumberedAccount("claude1", Path.home() / ".claude", "someone@example.com"),
            False, (), frozenset(),
        )

        _ = self.alert([note], self.now, selection=selection)

        with quota_alert.state_file() as state:
            self.assertEqual(state["episodes"], {})
        self.assertEqual(self.sent, [])
        self.assertEqual(self.pushed, [])
        self.assertEqual(self.noted, [])

    def test_claude_role_changes_do_not_repeat_the_phone_reminder_before_quota_recovers(self) -> None:
        for minute, state, left in ((0, "active", "1"), (2, "inactive", "1"),
                                    (4, "active", "1"), (6, "active", "80"), (8, "active", "1")):
            _ = self.alert([self.note("claude1.md", state, left)], self.now + timedelta(minutes=minute))
            self.assertEqual(len(self.noted), 2 if minute == 8 else 1)
        self.assertEqual(self.sent, [])

    def test_sessions_on_multiple_accounts_each_get_their_accounts_alerts(self) -> None:
        from scripts.message import sessions
        self.configure_claude_roles()
        owners = [sessions.SessionRecord(pid=pid, sessionId=str(pid), name=str(pid),
                  messagingSocketPath="/fixture.sock", updatedAt=1, tmux="", formerNames=[])
                  for pid in (10001, 10003)]
        identities = {10001: account.Account("claude", "spare@example.invalid", "claude1"),
                      10003: account.Account("claude", "unit@example.invalid", "claude3")}
        notes: Notes = [self.note("claude1.md", "active", "0", login="spare@example.invalid"),
                        self.note("claude2.md", "active", "0", login="showrunner@example.invalid"),
                        self.note("claude3.md", "active", "0", login="unit@example.invalid")]
        def observed_account(pid: int, *, home_state: account.LegacyHomeLogin | account.ConfigOnlyHome) -> account.Account | None:
            identity = identities.get(pid)
            if (identity is not None and isinstance(home_state, account.LegacyHomeLogin)
                    and identity.login.casefold() == home_state.account.login.casefold()):
                return home_state.account
            return identity
        with (mock.patch.object(sessions, "live_sessions", return_value=owners),
              mock.patch.object(account, "account_of", side_effect=observed_account)):
            _ = self.alert(notes, self.now)
        for label in ("claude1", "claude2", "claude3"):
            self.assertTrue(any(push.values["account"] == label for push in self.noted))
        with quota_alert.state_file() as state:
            self.assertEqual(set(state["episodes"]), {"claude1", "claude2", "claude3"})

    def test_collected_readings_remind_the_unfit_group_account_but_not_ungrouped_external_sessions(self) -> None:
        from scripts.message import sessions
        import five_hour
        self.configure_claude_roles()
        _ = accounts.set_group_account("units", "claude3", why="fixture units", now=self.now)
        unfit = accounts.account_dir("claude3")
        (unfit / "projects").unlink()
        external = self.root / "retained account"
        external.mkdir()
        owners = [sessions.SessionRecord(pid=pid, sessionId=str(pid), name=str(pid),
            messagingSocketPath="/fixture.sock", updatedAt=1, tmux="", formerNames=[]) for pid in (10001, 10003)]
        directories = {10001: external, 10003: unfit}
        logins = {self.root / ".claude": "showrunner@example.invalid", accounts.account_dir("claude2"): "showrunner@example.invalid",
                  unfit: "unit@example.invalid",
                  external: "external@example.invalid", accounts.account_dir("claude1"): "spare@example.invalid"}
        _ = self.note("claude4.md", "inactive", "90", login="external@example.invalid")
        _ = self.note("claude3.md", "inactive", "90", login="unit@example.invalid")
        _ = self.note("claude2.md", "active", "90", login="showrunner@example.invalid")
        def process_directory(pid: int) -> account.ConfiguredClaudeFolder | account.ProcessEnvironmentUnread:
            directory = directories.get(pid)
            return account.ConfiguredClaudeFolder(directory) if directory is not None else account.ProcessEnvironmentUnread("fixture")
        def process_account(pid: int, *, home_state: account.LegacyHomeLogin | account.ConfigOnlyHome) -> account.Account | None:
            directory = directories.get(pid)
            if directory == Path.home() / ".claude" and isinstance(home_state, account.LegacyHomeLogin):
                return home_state.account
            return account.Account("claude", logins[directory], "fixture") if directory is not None else None
        def live(directory: Path, *, use_disk_identity: bool = False) -> Report:
            self.assertTrue(use_disk_identity)
            return Report("Claude", email=logins[directory], quotas=[Quota("Weekly", 100,
                datetime.now(timezone.utc) + timedelta(days=2))], source=agent_accounts.ClaudeDirectorySource(directory))
        with (mock.patch.object(sessions, "live_sessions", return_value=owners),
              mock.patch.object(account, "account_of", side_effect=process_account),
              mock.patch.object(account, "process_config_dir", side_effect=process_directory)):
            selection = accounts.claude_account_selection()
        with (mock.patch.object(agent_accounts, "claude_live", side_effect=live),
              mock.patch.object(agent_accounts, "codex_homes", return_value=[]),
              mock.patch.object(five_hour, "watch", return_value=[]),
              mock.patch.object(session_table, "live_sessions", return_value=session_table.ReadSessions({
                  label: (session_table.SessionRow(label, "freestanding", "natedev", "~/project", label),)
                  for label in ("claude3", "claude4")}, frozenset()))):
            _ = agent_notes.update_alert_pass(selection, self.root / ".codex")
        _ = self.alert(list(note_file.read_notes()), datetime.now(timezone.utc), selection=selection)
        self.assertEqual({push.values["account"] for push in self.noted}, {"claude3"})
        with quota_alert.state_file() as state:
            self.assertIn("claude4", state["episodes"])
        self.assertEqual(self.sent, [])

    def test_alert_uses_the_updaters_role_value_without_resolving_again(self) -> None:
        """Update and alert can share the same run's role value."""
        self.configure_claude_roles()
        unit = self.note("claude3.md", "active", "0", login="unit@example.invalid")
        roles = accounts.claude_account_selection()
        self.set_claude_activity("claude1")
        with mock.patch.object(accounts, "claude_account_selection", side_effect=AssertionError("roles resolved twice")):
            _ = self.alert([unit], self.now, selection=roles)
        self.assertEqual(self.sent, [])
        self.assertEqual([push.values["account"] for push in self.noted], ["claude3"])

    def test_refusal_uses_supplied_selection_without_resolving_again(self) -> None:
        """Refusal notices accept the same run's frozen role value."""
        self.configure_claude_roles()
        note = self.note("codex1.md", "active", "0")
        roles = accounts.claude_account_selection()
        with mock.patch.object(accounts, "claude_account_selection", side_effect=AssertionError("roles resolved twice")):
            lines = self.block([note], self.now, selection=roles)
        self.assertIn("switched to claude for codex1: direct, module_review", lines)
        self.assertEqual((len(self.sent), len(self.pushed)), (0, 0))

    def test_recovery_does_not_announce_a_different_healthy_claude_account(self) -> None:
        self.configure_claude_roles()
        unit = self.note("claude3.md", "active", "0", login="unit@example.invalid")
        main = self.note("claude2.md", "active", "80", login="showrunner@example.invalid")
        _ = self.alert([unit, main], self.now)
        self.sent.clear()
        unit = self.note("claude3.md", "inactive", "0", login="unit@example.invalid")
        _ = self.alert([unit, main], self.now + timedelta(minutes=2))
        self.assertEqual(self.sent, [])

    def test_quota_config_uses_shared_showrunner_settings(self) -> None:
        self.assertEqual(quota_alert.Config.__module__, "scripts.production.showrunners")
        self.assertEqual(quota_alert.load_settings_from.__module__, "scripts.production.showrunners")

    def test_setup_blocks_real_phone_sends(self) -> None:
        """Guard the fixture patch that prevents real phone sends."""
        self.assertIsNot(quota_alert.tell_user, REAL_TELL_USER)

    def test_load_config_reads_the_named_object_layout_registry(self) -> None:
        named_config = self.root / "named-showrunners.json"
        _ = named_config.write_text(json.dumps({
            "threshold_percent": 7,
            "repeat_minutes": 45,
            "stall_minutes": 9,
            "faults_to": "fault-handler",
            "always": ["always-there"],
            "showrunners": [{
                "session": "named-director",
                "zone": "America/New_York",
                "units": [{"session": "named-unit", "status": "run-finished"}],
            }],
        }), encoding="utf-8")

        with mock.patch.object(quota_alert, "CONFIG", named_config):
            config = quota_alert.load_config()

        self.assertEqual(config["threshold_percent"], 7)
        self.assertEqual(config["always"], ["always-there"])
        # A list of showrunners left in the file is not read: they are found by their update timers.
        self.assertNotIn("showrunners", config)

    def test_codex_two_percent_reminder_pushes_once_per_episode_without_session_repeat(self) -> None:
        config = quota_alert.load_config()
        config["threshold_percent"] = 2
        _ = quota_alert.CONFIG.write_text(json.dumps(config))
        for minute, left in ((0, "3"), (2, "2"), (10, "1"), (32, "1")):
            _ = self.alert([self.note("codex1.md", "active", left)], self.now + timedelta(minutes=minute))
            self.assertEqual(len(self.noted), 0 if minute == 0 else 1)
        push = self.noted[0]
        self.assertEqual(push.id, "quota.codex_next_move")
        self.assertEqual(push.title, "codex1 Codex workers will use paid credits")
        self.assertEqual(push.body,
            "codex1 has 2% of its weekly usage left, and no move is set up for its workers. When it runs out, they spend its paid credits, then move to Claude. Nothing is needed.")
        self.assertEqual(push.action, ActionRequired(
            "Optional: to move them at 1% instead, type in the natedev session: /account_moves when workers reach 1% switch to Claude"))
        self.assertEqual(self.sent, [])
        _ = self.alert([self.note("codex1.md", "active", "80")], self.now + timedelta(minutes=34))
        self.sent.clear()  # Restoration notices remain in Phase54.
        _ = self.alert([self.note("codex1.md", "active", "2")], self.now + timedelta(minutes=36))
        self.assertEqual(len(self.noted), 2)
        self.assertEqual(self.sent, [])

    def test_workers_self_target_keeps_the_low_reminder_due_without_pushing_a_fallback(self) -> None:
        config = quota_alert.load_config()
        config["threshold_percent"] = 2
        _ = quota_alert.CONFIG.write_text(json.dumps(config))
        entry = next_moves.add_next_move("workers", 1, "codex1")
        note = self.note("codex1.md", "active", "2")
        for minute in (0, 2, 32):
            _ = self.alert([note], self.now + timedelta(minutes=minute))
        self.assertEqual(quota_alert.codex_next_move_push(note, now=self.now),
                         quota_alert.QuotaStatusNote("codex1", "the queued next entry targets the current account"))
        self.assertEqual(self.noted, [])
        self.assertEqual(self.sent, [])
        self.assertEqual(next_moves.read_next_moves(), (entry,))
        with quota_alert.state_file() as state:
            self.assertEqual(state["episodes"]["codex1"].get("push"), "due")

    def test_runnable_workers_entry_keeps_the_reminder_due_until_removed(self) -> None:
        config = quota_alert.load_config()
        config["threshold_percent"] = 2
        _ = quota_alert.CONFIG.write_text(json.dumps(config))
        _ = next_moves.add_next_move("workers", 1, "codex2")
        note = self.note("codex1.md", "active", "2")
        _ = self.alert([note], self.now)
        self.assertEqual((self.noted, self.sent), ([], []))
        with quota_alert.state_file() as state:
            self.assertEqual(state["episodes"]["codex1"].get("push"), "due")
        next_moves.remove_next_move("workers", next_moves.GroupPosition(1))
        _ = self.alert([note], self.now + timedelta(minutes=2))
        self.assertEqual([push.id for push in self.noted], ["quota.codex_next_move"])
        self.assertEqual(self.sent, [])

    def test_unreadable_claude_history_makes_log_coverage_empty_for_the_reminder(self) -> None:
        entry = next_moves.add_next_move("units", 1, "claude3")
        history = accounts.roles_history_file()
        history.parent.mkdir(parents=True, exist_ok=True)
        _ = history.write_bytes(b"not JSON\n")
        _ = self.alert([self.note("claude2.md", "active", "1")], self.now)
        self.assertEqual([push.id for push in self.noted], ["quota.claude_next_move"])
        self.assertEqual(self.sent, [])
        self.assertEqual(next_moves.read_next_moves(), (entry,))

    def test_episode_without_push_marker_never_sends_old_news(self) -> None:
        stamp = self.now.isoformat(timespec="seconds")
        _ = quota_alert.STATE.write_text(json.dumps({
            "episodes": {"claude2": {"since": stamp, "last": {}}},
        }))
        _ = self.alert([self.note("claude2.md", "active", "0")], self.now)
        self.assertEqual((self.pushed, self.noted, self.sent), ([], [], []))
        with quota_alert.state_file() as state:
            self.assertNotIn("push", state["episodes"]["claude2"])

    def test_codex_weekly_zero_status_pushes_once_without_a_next_move_reminder(self) -> None:
        note = self.note("codex1.md", "active", "0", credit_balance="60498")
        _ = self.alert([note], self.now)
        self.assertEqual(self.noted, [])
        self.assertEqual(len(self.pushed), 1)
        title, body, ask = self.pushed[0]
        self.assertEqual(title, "quota_alert: codex1 now uses paid credits")
        self.assertIn("60,498", body)
        self.assertEqual(ask.need, "decision")
        with quota_alert.state_file() as state:
            self.assertIn("codex1", state["episodes"])
            self.assertEqual(state.get("codex_zero", {})["codex1"]["phone"], self.now.isoformat(timespec="seconds"))
        _ = self.alert([note], self.now + timedelta(minutes=30))
        self.assertEqual(len(self.pushed), 1)
        self.assertEqual(self.noted, [])

    def test_outgoing_account_refusal_is_logged_without_a_phone_push(self) -> None:
        """F005: a refusal from an account no longer used needs no user action."""
        outgoing = self.note("codex1.md", "inactive", "0", login="old@example.invalid")
        current = self.note("codex2.md", "active", "90", login="someone@example.com")
        accounts.set_role("workers", "codex2")
        readings = self.alert_readings([outgoing, current], self.now)
        refusing = agent_accounts.KnownCodexAccount(self.root / "outgoing", "old@example.invalid", "codex1")
        lines = quota_alert.blocked([outgoing, current], refusing, self.now, codex_readings=readings)
        self.assertEqual((self.pushed, self.sent), ([], []))
        self.assertIn("quota status codex1: Codex refused work on codex1, the outgoing account. Nothing switched to Claude.", lines)
        with quota_alert.state_file() as state:
            self.assertIsInstance(state["fallback"], quota_alert.NoCodexQuotaFallback)
            notice = state.get("refusal_notices", {}).get(str(refusing.home))
            assert notice is not None and notice["kind"] == "known"
            self.assertEqual(notice.get("push"), self.now.isoformat(timespec="seconds"))

    def test_internal_switch_failure_details_do_not_leak_into_the_phone_push(self) -> None:
        note = self.note("codex1.md", "active", "0")
        push = quota_alert.refusal_push(note, quota_alert.NoCodexQuotaFallback(),
                                        "Could not read /private/account/registry " + "unavailable " * 100, False)
        self.assertIsInstance(push, quota_alert.UserPush)
        self.assertNotIn("/private", push.text)
        self.assertNotIn("registry", push.text)
        self.assertIn("the work is paused", push.text)

    def test_actionable_quota_pushes_fit_phone_body_and_name_where_to_act(self) -> None:
        notes: Notes = [self.note("claude2.md", "active", "0", limit_reset_count="0"),
                        self.note("codex1.md", "active", "0", credit_balance="60498")]
        _ = self.alert(notes, self.now)
        _ = self.block(notes, self.now)
        for summary, text, ask in self.pushed:
            self.assertLess(len(summary), 45)
            self.assertLess(len("Action: " + ask.action + "\n" + text), 500)
            self.assertTrue("natedev" in ask.action or "chatgpt.com/codex/settings/usage" in ask.action)
            self.assertLessEqual(text.count("recommended"), 1)

    def test_every_user_push_states_its_action_as_send_py_requires(self) -> None:
        _ = quota_alert.REGISTRY.write_text(REGISTRY.replace("impl=opus:high", "impl=opus:ultra"))
        claude = self.note("claude2.md", "active", "0", limit_reset_count="3")
        codex = self.note("codex1.md", "active", "0")
        spare = self.note("claude1.md", "inactive", "80", login="spare@example.invalid")
        sends = self.user_sends(lambda: self.alert([claude, codex, spare], self.now))
        sends |= self.user_sends(lambda: self.block([codex], self.now))
        notes = [title for title, (need, _action) in sends.items() if need == "note"]
        self.assertEqual(len(notes), 1)
        for title in notes:
            _ = sends.pop(title)
        self.assertEqual(sends, {
            "quota_alert: codex1 now uses paid credits": ("decision", ActionRequired(
                "To stop it, set use_credits: off in ~/rust/hanadocs/agents/codex1.md on natedev")),
            "Codex work stopped for lack of usage": ("decision", ActionRequired(REFUSAL_ACTION)),
        })

    def test_user_delivery_names_success_process_failure_and_launch_failure(self) -> None:
        push = render("quota.claude_next_move", account="claude2", remaining="0%")
        for code, line, expected in ((0, "SENT: to the user", quota_alert.PushSent()),
                                     (3, "FAILED: channel unavailable", quota_alert.PushFailed("FAILED: channel unavailable"))):
            with self.subTest(code=code), mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], code, line, "")):
                self.assertEqual(REAL_TELL_USER(push), expected)
        with mock.patch.object(subprocess, "run", side_effect=OSError("sender unavailable")):
            self.assertEqual(REAL_TELL_USER(push), quota_alert.PushFailed("sender unavailable"))

    def test_user_send_uses_configured_review_limit_without_shortening_agent_relays(self) -> None:
        config = self.root / "notify.conf"
        _ = config.write_text("REVIEW_TIMEOUT_SECONDS=90\n")
        done = subprocess.CompletedProcess(["send.py"], 0, "SENT: to the user", "")
        with (mock.patch.object(review, "NOTIFY_CONFIG", config),
              mock.patch.object(subprocess, "run", return_value=done) as run):
            self.assertIsInstance(REAL_TELL_USER(render("quota.claude_next_move", account="claude2", remaining="0%")), quota_alert.PushSent)
            command = cast(list[str], run.call_args.args[0])
            self.assertEqual(float(command[command.index("--timeout") + 1]), review.user_send_seconds())
            self.assertEqual(run.call_args.kwargs["timeout"], review.user_send_seconds() + 15)
            self.assertNotIn("input", run.call_args.kwargs)
        self.assertEqual(quota_alert.RELAY_TIMEOUT, 40)

    def test_failed_codex_reminder_retries_phone_delivery_without_session_alerts(self) -> None:
        config = quota_alert.load_config()
        config["threshold_percent"] = 2
        _ = quota_alert.CONFIG.write_text(json.dumps(config))
        note = self.note("codex1.md", "active", "2")
        self.push_errors.append(quota_alert.PushFailed("fixture offline"))
        log = self.alert([note], self.now)
        self.assertIn("quota push codex1 -> user: not delivered: fixture offline", log)
        _ = self.alert([note], self.now + timedelta(minutes=2))
        _ = self.alert([note], self.now + timedelta(minutes=32))
        self.assertEqual(len(self.noted), 2)
        self.assertEqual(self.sent, [])

    def test_switch_to_a_low_account_is_not_a_restore(self) -> None:
        _ = self.alert([self.note("codex1.md", "active", "0")], self.now)
        _ = self.recipients()
        _ = self.alert([self.note("codex1.md", "inactive", "0", login="old@example.invalid"), self.note("codex2.md", "active", "1")],
                              self.now + timedelta(minutes=2))
        self.assertEqual(self.sent, [])
        self.assertEqual(self.noted[-1].values["account"], "codex2")

    def test_retained_notice_recipients_include_configured_sessions_and_showrunners_once(self) -> None:
        with (mock.patch.object(quota_alert, "recipients", REAL_RECIPIENTS),
              mock.patch.object(quota_alert, "registered_showrunners",
                  return_value=running("natedev", "boss of bosses", "new director"))):
            self.assertEqual(quota_alert.recipients(quota_alert.load_config()),
                             ["natedev", "boss of bosses", "new director"])
            self.assertEqual(quota_alert.recipients(quota_alert.load_config(), "natedev"),
                             ["boss of bosses", "new director"])

    def test_low_recovery_closes_the_episode_without_telling_sessions(self) -> None:
        _ = self.alert([self.note("codex1.md", "active", "1")], self.now)
        _ = self.alert([self.note("codex1.md", "active", "100")], self.now + timedelta(minutes=2))
        self.assertEqual(self.sent, [])
        with quota_alert.state_file() as state:
            self.assertNotIn("codex1", state["episodes"])

    def test_refresh_after_recovery_tells_no_session(self) -> None:
        _ = self.alert([self.note("codex1.md", "active", "1")], self.now)
        notes: Notes = [self.note("codex1.md", "active", "100")]
        _ = self.alert(notes, self.now + timedelta(minutes=2))
        lines = self.refresh(notes, "natedev", self.now + timedelta(minutes=3))
        self.assertIn("notes refreshed; nobody was told", lines)
        self.assertEqual(self.sent, [])

    def test_refresh_low_allowance_tells_no_session(self) -> None:
        lines = self.refresh([self.note("codex1.md", "active", "1")], "natedev", self.now)
        self.assertIn("notes refreshed; nobody was told", lines)
        self.assertEqual(self.sent, [])

    def test_legacy_blocked_cli_finds_the_canonical_episode_and_saves_no_spaced_label(self) -> None:
        notes: Notes = [self.note("codex1.md", "active", "1")]
        _ = self.alert(notes, self.now)
        _ = self.block(notes, self.now)
        with quota_alert.state_file() as state:
            self.assertIsInstance(state["fallback"], quota_alert.AccountCodexQuotaFallback)
            self.assertEqual(state["episodes"]["codex1"].get("refusal_push"), self.now.isoformat(timespec="seconds"))
            # Only the episode's delivery marker may suppress this retry.
            _ = state.get("refusal_notices", {})[str(self.root / ".codex")].pop("push", None)
        self.pushed.clear()
        self.sent.clear()
        readings = self.alert_readings(notes, self.now)
        output = io.StringIO()

        with (mock.patch.object(sys, "argv", ["agent_notes.py", "blocked", str(self.root / ".codex"),
                  "someone@example.com", "codex 1"]),
              mock.patch.object(note_file, "read_notes", return_value=notes),
              mock.patch.object(quota_alert, "resolve_codex_readings", return_value=readings),
              redirect_stdout(output)):
            agent_notes.main()

        self.assertEqual(self.pushed, [], "the canonical episode already records its refusal push")
        self.assertEqual(self.sent, [], "the canonical episode must suppress another refusal delivery")
        self.assertNotIn("codex 1", output.getvalue())
        saved = cast(dict[str, object], json.loads(quota_alert.STATE.read_text()))
        notices = cast(dict[str, dict[str, object]], saved["refusal_notices"])
        self.assertEqual(notices[str(self.root / ".codex")]["label"], "codex1")

    def test_current_session_is_found_up_the_process_tree(self) -> None:
        sessions = self.root / "sessions"
        sessions.mkdir()
        parent = os.getppid()
        grandparent = parent + 1000
        with mock.patch.object(quota_alert, "SESSIONS", sessions), mock.patch.object(
            subprocess, "run", side_effect=[
                subprocess.CompletedProcess(["ps"], 0, str(grandparent), ""),
                subprocess.CompletedProcess(["ps"], 0, "1", ""),
                subprocess.CompletedProcess(["ps"], 0, str(grandparent), ""),
            ]
        ):
            self.assertIsNone(quota_alert.current_session())
            # Two levels up, so the walk past a parent with no session file is exercised.
            _ = (sessions / f"{grandparent}.json").write_text(json.dumps({"name": "boss of bosses"}))
            self.assertEqual(quota_alert.current_session(), "boss of bosses")

    def test_inactive_above_threshold_unknown_or_reset_notes_are_quiet(self) -> None:
        past = local_reset(self.now - timedelta(hours=1))
        notes: Notes = [self.note("codex2.md", "inactive", "0"), self.note("claude1.md", "active", "2"),
                        self.note("claude2.md", "active", "null"), self.note("codex1.md", "active", "0", past)]
        self.assertEqual(self.alert(notes, self.now), [])
        self.assertEqual(self.sent, [])

    def assignments(self) -> dict[str, str]:
        return quota_alert.registry_assignments()

    def refuse(self) -> str:
        """Codex refused work for quota while codex1 is out of its week; the first log line."""
        return next(line for line in self.block([self.note("codex1.md", "active", "0")], self.now)
                    if line.startswith(("switched to claude", "switch to claude")))

    def test_explicit_refusal_time_is_recorded_without_replacing_it_with_pass_time(self) -> None:
        notes = [self.note("codex1.md", "active", "0")]
        readings = self.alert_readings(notes, self.now)
        identity = readings.selected_account
        assert isinstance(identity, agent_accounts.KnownCodexAccount)
        earlier = self.now - timedelta(minutes=1)
        _ = quota_alert.blocked(notes, identity, self.now, codex_readings=readings,
            refused_at=quota_alert.RefusedAt(earlier), window=refusals.KnownWindow("weekly"))
        self.assertEqual(refusals.load()[refusals.key("codex", identity.login)].at, earlier)

    def test_unknown_refusal_time_uses_pass_time_and_ignores_inherited_handoff_environment(self) -> None:
        notes = [self.note("codex1.md", "active", "0")]
        readings = self.alert_readings(notes, self.now)
        identity = readings.selected_account
        assert isinstance(identity, agent_accounts.KnownCodexAccount)
        with mock.patch.dict(os.environ, {"ACCOUNT_REFUSAL_AT": (self.now - timedelta(days=1)).isoformat()}):
            _ = quota_alert.blocked(notes, identity, self.now, codex_readings=readings,
                refused_at=quota_alert.RefusalTimeUnknown(), window=refusals.KnownWindow("weekly"))
        self.assertEqual(refusals.load()[refusals.key("codex", identity.login)].at, self.now)

    def test_codex_reaching_the_threshold_switches_nothing_and_only_reminds_the_phone(self) -> None:
        log = self.alert([self.note("codex1.md", "active", "1")], self.now)
        self.assertEqual([line for line in log if "switch" in line], [])
        self.assertEqual(self.assignments(),
                         {"direct": "codex", "cli": "claude", "module_review": "codex", "ask_a_friend": "caller"})
        self.assertEqual(self.sent, [])
        self.assertEqual([push.id for push in self.noted], ["quota.codex_next_move"])
        self.assertIsNone(quota_alert.notice(lambda: "natedev"))

    def test_a_quota_refusal_moves_every_codex_function_to_claude_once(self) -> None:
        notes: Notes = [self.note("codex1.md", "active", "0")]
        _ = self.alert(notes, self.now)
        self.assertEqual(self.refuse(), "switched to claude for codex1: direct, module_review")
        self.assertEqual(self.assignments(),
                         {"direct": "claude", "cli": "claude", "module_review": "claude", "ask_a_friend": "caller"})
        self.sent.clear()
        _ = self.alert(notes, self.now + timedelta(minutes=30))
        self.assertEqual(self.sent, [])
        with quota_alert.state_file() as state:
            switch = state["fallback"]
            self.assertIsInstance(switch, quota_alert.AccountCodexQuotaFallback)
            assert isinstance(switch, quota_alert.AccountCodexQuotaFallback)
            self.assertIsInstance(switch.hold, quota_alert.NoFallbackHold)
        saved = cast(quota_alert.StoredState, json.loads(quota_alert.STATE.read_text()))
        assert "switch" in saved
        self.assertNotIn("credits_off_until", saved["switch"])
        self.assertEqual(self.routing_entries()[-1]["kind"], "routed to claude")
        # The user puts module_review back on codex mid-episode; the next run does not move it again.
        self.assertIsNone(quota_alert.edit_registry("agents_set_assignment", "module_review", "codex"))
        _ = self.alert(notes, self.now + timedelta(minutes=60))
        self.assertEqual(self.assignments()["module_review"], "codex")

    def test_legacy_low_quota_episode_does_not_send_a_second_opening_alert(self) -> None:
        notes: Notes = [self.note("codex1.md", "active", "1")]
        _ = self.alert(notes, self.now)
        self.assertEqual(self.sent, [])
        self.assertEqual([push.id for push in self.noted], ["quota.codex_next_move"])
        original = quota_alert.STATE.read_text()
        self.assertIn('"codex1":', original)
        _ = quota_alert.STATE.write_text(original.replace('"codex1":', '"codex 1":'))
        self.sent.clear()
        self.pushed.clear()
        _ = self.alert(notes, self.now + timedelta(minutes=2))
        self.assertEqual(self.sent, [])
        self.assertEqual(self.pushed, [])
        self.assertEqual(len(self.noted), 1)
        with quota_alert.state_file() as state:
            self.assertIn("codex1", state["episodes"])
            self.assertNotIn("codex 1", state["episodes"])

    def test_legacy_fallback_owner_and_refusal_notice_load_canonically(self) -> None:
        notes: Notes = [self.note("codex1.md", "active", "0")]
        _ = self.alert(notes, self.now)
        _ = self.refuse()
        original = quota_alert.STATE.read_text()
        self.assertIn('"label": "codex1"', original)
        _ = quota_alert.STATE.write_text(original.replace('"label": "codex1"', '"label": "codex 1"'))
        with quota_alert.state_file() as state:
            fallback = state["fallback"]
            assert isinstance(fallback, quota_alert.AccountCodexQuotaFallback)
            self.assertEqual(fallback.owner.label, "codex1")
            notices = state.get("refusal_notices", {})
            self.assertTrue(notices)
            for notice in notices.values():
                assert notice["kind"] == "known"
                self.assertEqual(notice["label"], "codex1")

    def test_successful_claude_fallback_is_logged_without_a_session_or_phone_push(self) -> None:
        notes: Notes = [self.note("codex1.md", "active", "0")]
        _ = self.alert(notes, self.now)
        self.sent.clear()
        self.pushed.clear()
        later = self.now + timedelta(minutes=5)
        log = self.block(notes, later)
        self.assertIn("switched to claude for codex1: direct, module_review", log)
        self.assertTrue(any(line.startswith("quota status codex1:") for line in log))
        self.assertEqual(self.sent, [])
        self.assertEqual(self.pushed, [])
        self.sent.clear()
        _ = self.block(notes, later + timedelta(minutes=1))
        self.assertEqual(self.sent, [])
        self.assertEqual(self.pushed, [])

    def test_failed_refusal_push_retries_on_the_next_refusal_then_stops(self) -> None:
        _ = quota_alert.REGISTRY.write_text(REGISTRY.replace("impl=opus:high", "impl=opus:ultra"))
        notes: Notes = [self.note("codex1.md", "active", "0")]
        _ = self.alert(notes, self.now)
        self.pushed.clear()
        self.push_errors.append(quota_alert.PushFailed("phone offline"))
        first = self.block(notes, self.now + timedelta(minutes=1))
        self.assertIn("quota refusal codex1 -> user: not delivered: phone offline", first)
        second = self.block(notes, self.now + timedelta(minutes=2))
        self.assertIn("quota refusal codex1 -> user: sent", second)
        third = self.block(notes, self.now + timedelta(minutes=3))
        self.assertFalse(any(line.endswith("-> user: sent") for line in third))
        self.assertEqual(len(self.pushed), 2)

    def test_refusal_push_says_when_the_switch_had_already_happened(self) -> None:
        note = self.note("codex1.md", "active", "0")
        _ = self.alert([note], self.now)
        self.pushed.clear()
        stamp = self.now.isoformat(timespec="seconds")
        owner = self.alert_readings([note], self.now).selected_account
        assert isinstance(owner, agent_accounts.KnownCodexAccount)
        with quota_alert.state_file() as state:
            self.assertEqual(quota_alert.switch_to_claude(state, note, stamp, owner),
                             quota_alert.CodexWorkMovedToClaude("codex1", ("direct", "module_review")))

        log = self.block([note], self.now + timedelta(minutes=1))
        self.assertTrue(any("New work had already moved to Claude" in line for line in log))
        self.assertEqual(self.pushed, [])

    def test_refusal_push_says_when_nothing_was_on_codex(self) -> None:
        registry = quota_alert.REGISTRY
        _ = registry.write_text(REGISTRY.replace("direct=codex", "direct=claude").replace(
            "module_review=codex", "module_review=claude"))
        notes: Notes = [self.note("codex1.md", "active", "0")]
        _ = self.alert(notes, self.now)
        self.pushed.clear()
        log = self.block(notes, self.now + timedelta(minutes=1))
        self.assertTrue(any("No delegated work needed to move" in line for line in log))
        self.assertEqual(self.pushed, [])

    def test_a_refusal_above_weekly_threshold_still_uses_fallback_when_no_codex_has_room(self) -> None:
        log = self.block([self.note("codex1.md", "active", "24")], self.now)
        self.assertTrue(any("switched to claude" in line for line in log), log)
        self.assertEqual(self.assignments()["direct"], "claude")
        self.assertEqual(self.sent, [])
        self.assertEqual(len(self.pushed), 0)
        with quota_alert.state_file() as state:
            self.assertEqual(state["episodes"], {})

    def test_a_refusal_before_the_first_alert_or_after_an_earlier_episode_still_switches(self) -> None:
        notes: Notes = [self.note("codex1.md", "active", "0")]
        self.assertIn("switched to claude for codex1: direct, module_review", self.block(notes, self.now))
        self.assertEqual(self.sent, [])
        _ = self.alert([self.note("codex1.md", "active", "100")], self.now + timedelta(minutes=2))
        self.assertEqual(self.assignments()["direct"], "codex")
        _ = self.alert(notes, self.now + timedelta(minutes=4))
        self.sent.clear()
        log = self.block(notes, self.now + timedelta(minutes=6))
        self.assertIn("switched to claude for codex1: direct, module_review", log)
        self.assertEqual(self.sent, [])

    def test_claude_running_out_switches_nothing(self) -> None:
        _ = self.alert([self.note("claude2.md", "active", "0")], self.now)
        self.assertEqual(self.assignments()["direct"], "codex")
        self.assertEqual(self.sent, [])
        self.assertEqual(len(self.noted), 1)
        self.assertIsNone(quota_alert.notice(lambda: "natedev"))

    def test_recovery_moves_back_only_what_is_still_on_claude(self) -> None:
        _ = self.alert([self.note("codex1.md", "active", "0")], self.now)
        _ = self.refuse()
        self.assertIsNone(quota_alert.edit_registry("agents_set_assignment", "module_review", "codex"))
        self.sent.clear()
        log = self.alert([self.note("codex1.md", "active", "100")], self.now + timedelta(hours=1))
        self.assertIn("switch to claude undone: moved back from Claude to Codex: direct; left alone, "
                      + "re-assigned since the switch: module_review", log)
        self.assertEqual(self.assignments(),
                         {"direct": "codex", "cli": "claude", "module_review": "codex", "ask_a_friend": "caller"})
        self.assertEqual(self.sent, [])
        self.assertEqual(self.routing_entries()[-1]["kind"], "routed back")

    def routing_entries(self) -> list[dict[str, object]]:
        path = moves.store_file()
        if not path.exists():
            return []
        entries = [cast(dict[str, object], json.loads(line)) for line in path.read_text().splitlines()]
        return [entry for entry in entries if entry.get("kind") in
                ("routed to claude", "routed back", "routing not restored")]

    def test_fallback_recovery_after_state_reload_keeps_its_pairing_time_and_journals_once(self) -> None:
        notes = [self.note("codex1.md", "active", "0")]
        _ = self.block(notes, self.now)
        _ = self.block(notes, self.now)
        opened = self.routing_entries()
        self.assertEqual(len(opened), 1)
        self.assertEqual((opened[0]["kind"], opened[0]["owner"], opened[0]["cause"]),
                         ("routed to claude", "codex1", "refusal"))
        self.assertEqual(set(cast(list[str], opened[0]["moved"])), {"direct", "module_review"})
        # Reload through the persisted updater state, then recover in another pass.
        with quota_alert.state_file() as state:
            fallback = state["fallback"]
            self.assertIsInstance(fallback, quota_alert.AccountCodexQuotaFallback)
            assert isinstance(fallback, quota_alert.AccountCodexQuotaFallback)
            self.assertEqual(fallback.at, opened[0]["fallback_at"])
        _ = quota_alert.edit_registry("agents_set_assignment", "module_review", "codex")
        later = self.now + timedelta(hours=1)
        _ = self.alert([self.note("codex1.md", "active", "100")], later)
        _ = self.alert([self.note("codex1.md", "active", "100")], later)
        entries = self.routing_entries()
        self.assertEqual([entry["kind"] for entry in entries], ["routed to claude", "routed back"])
        self.assertEqual(entries[0]["fallback_at"], entries[1]["fallback_at"])
        self.assertEqual(entries[1]["restored"], ["direct"])
        self.assertEqual(entries[1]["kept"], ["module_review"])
        self.assertNotIn("move_id", entries[1])

    def test_partial_routing_restoration_records_failed_keys_and_never_claims_full_recovery(self) -> None:
        _ = self.block([self.note("codex1.md", "active", "0")], self.now)
        edit = quota_alert.edit_registry
        def fail_one(function: str, *arguments: str) -> str | None:
            if function == "agents_set_assignment" and arguments[0] == "module_review":
                return "registry locked"
            return edit(function, *arguments)
        with mock.patch.object(quota_alert, "edit_registry", side_effect=fail_one):
            _ = self.alert([self.note("codex1.md", "active", "100")], self.now + timedelta(hours=1))
        entries = self.routing_entries()
        self.assertEqual([entry["kind"] for entry in entries], ["routed to claude", "routing not restored"])
        self.assertEqual(entries[1]["outcome"], "partial")
        self.assertEqual(entries[1]["restored"], ["direct"])
        self.assertEqual(entries[1]["failed"], ["module_review (registry locked)"])
        self.assertEqual(entries[1]["fallback_at"], entries[0]["fallback_at"])
        self.assertEqual(self.assignments()["module_review"], "claude")

    def test_unreadable_registry_records_failed_recovery_for_every_owed_key(self) -> None:
        _ = self.block([self.note("codex1.md", "active", "0")], self.now)
        with mock.patch.object(quota_alert, "registry_assignments", side_effect=OSError("registry unreadable")):
            _ = self.alert([self.note("codex1.md", "active", "100")], self.now + timedelta(hours=1))
        entries = self.routing_entries()
        self.assertEqual([entry["kind"] for entry in entries], ["routed to claude", "routing not restored"])
        self.assertEqual(entries[1]["outcome"], "failed")
        self.assertEqual(entries[1]["restored"], [])
        self.assertEqual(set(cast(list[str], entries[1]["failed"])),
                         {"direct (registry unreadable)", "module_review (registry unreadable)"})

    def test_unknown_fallback_owner_stays_unknown_after_reload_and_recovery_on_a_noted_home(self) -> None:
        _ = self.block([self.note("codex1.md", "active", "0")], self.now)
        with quota_alert.state_file() as state:
            prior = state["fallback"]
            assert isinstance(prior, quota_alert.AccountCodexQuotaFallback)
            state["fallback"] = quota_alert.UnknownOwnerCodexQuotaFallback(prior.at, prior.moved)
        later = self.now + timedelta(hours=1)
        _ = self.alert([self.note("codex1.md", "active", "100")], later)
        recovered = self.routing_entries()[-1]
        self.assertEqual(recovered["kind"], "routed back")
        self.assertEqual(recovered["owner"], "unknown")
        self.assertEqual(recovered["fallback_at"], prior.at)

    def test_mixed_routing_journal_closes_legacy_worker_wait_and_no_room_only_once(self) -> None:
        for kind in ("waited", "no room"):
            common = moves.MoveCommon(at=self.now.isoformat(), tool="codex", subject="workers", move_id=kind)
            entry: moves.MoveEntry = (moves.WaitedMove(**common, kind="waited", window="five_hour", reset=self.now.isoformat())
                if kind == "waited" else moves.NoRoomMove(**common, kind="no room", earliest_reset=self.now.isoformat()))
            path = moves.store_file()
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as output:
                _ = output.write(json.dumps({**entry, "subject": "seats"}) + "\n")
        moves.append_routing(moves.RoutedToClaude(kind="routed to claude", at=self.now.isoformat(), tool="codex",
            fallback_at=self.now.isoformat(), owner="codex1", cause="refusal", why="Codex refused work for quota", moved=["direct"]))
        _ = self.note("codex1.md", "active", "100")
        for _ in range(2):
            quota_alert._finish_workers_transitions(self.now)  # pyright: ignore[reportPrivateUsage]
            quota_alert._room_again(self.now)  # pyright: ignore[reportPrivateUsage]
        closing = [entry for entry in moves.read_moves() if entry["kind"] in ("wait ended", "room again")]
        self.assertCountEqual([(entry["kind"], entry["move_id"]) for entry in closing],
                              [("wait ended", "waited"), ("room again", "no room")])

    def test_a_refused_switch_still_alerts_and_says_why(self) -> None:
        registry = quota_alert.REGISTRY
        _ = registry.write_text(REGISTRY.replace("impl=opus:high", "impl=opus:ultra"))
        notes: Notes = [self.note("codex1.md", "active", "0")]
        _ = self.alert(notes, self.now)
        self.pushed.clear()
        log = self.block(notes, self.now)
        self.assertTrue(any(line.startswith("switch to claude for codex1 failed: ERROR: [direct.impl] effort 'ultra'") for line in log))
        self.assertIn("quota refusal codex1 -> user: sent", log)
        self.assertEqual(self.pushed[0][0::2], ("Codex work stopped for lack of usage", quota_alert.Ask("decision", REFUSAL_ACTION)))
        self.assertIn("Moving those jobs to Claude", self.pushed[0][1])
        second = self.block(notes, self.now)
        self.assertFalse(any(line.startswith("quota push") for line in second))
        self.assertEqual(len(self.pushed), 1)
        self.assertEqual(self.assignments()["direct"], "codex")
        self.sent.clear()
        _ = self.alert(notes, self.now + timedelta(minutes=30))
        self.assertEqual(self.sent, [])
        with quota_alert.state_file() as state:
            failed = state.get("refusal_notices", {})[str(self.root / ".codex")].get("switch_failed")
        self.assertIsInstance(failed, str)
        # A registry fixed mid-episode is not switched by the next run.
        _ = registry.write_text(REGISTRY)
        _ = self.alert(notes, self.now + timedelta(minutes=60))
        self.assertEqual(self.assignments()["direct"], "codex")

    def test_a_stopped_switch_leaves_the_registry_alone(self) -> None:
        _ = self.alert([self.note("codex1.md", "active", "0")], self.now)
        with mock.patch.object(quota_alert, "EDIT_TIMEOUT", 0.01):
            line = self.refuse()
        self.assertEqual(line, "switch to claude for codex1 failed: agents_set_all_assignments took longer "
                         + "than 0.01 s and was stopped")
        # The editor's process group died with it: nothing it started lands later.
        time.sleep(2)
        self.assertEqual(self.assignments()["direct"], "codex")
        self.assertEqual([path.name for path in self.root.glob("agents.conf.*")], [])

    def test_selected_allowance_restores_assignments_despite_low_weekly_episode(self) -> None:
        _ = self.alert([self.note("codex1.md", "active", "0")], self.now)
        _ = self.refuse()
        self.sent.clear()
        log = self.alert([self.note("codex1.md", "inactive", "0"), self.note("codex2.md", "active", "1")],
                                self.now + timedelta(minutes=2))
        self.assertIn("switch to claude undone: moved back from Claude to Codex: direct, module_review", log)
        self.assertEqual(self.assignments()["direct"], "codex")
        self.assertFalse(any(text.startswith("Quota alert:") for _, text in self.sent))
        self.assertEqual(self.sent, [])
        self.assertEqual(self.routing_entries()[-1]["kind"], "routed back")
        _ = self.alert([self.note("codex1.md", "inactive", "0"), self.note("codex2.md", "active", "90")],
                              self.now + timedelta(minutes=4))
        self.assertEqual(self.assignments()["direct"], "codex")

    def test_refresh_restores_assignments_and_records_recovery_without_session_delivery(self) -> None:
        _ = self.alert([self.note("codex1.md", "active", "0")], self.now)
        _ = self.refuse()
        self.sent.clear()
        lines = self.refresh([self.note("codex1.md", "active", "100")], "natedev",
                             self.now + timedelta(minutes=3))
        self.assertIn("switch to claude undone: moved back from Claude to Codex: direct, module_review", lines)
        self.assertEqual(self.assignments()["module_review"], "codex")
        self.assertEqual(self.sent, [])
        self.assertEqual([row["kind"] for row in self.routing_entries()], ["routed to claude", "routed back"])

    def test_notice_shows_each_switch_once_and_only_in_the_owner_session(self) -> None:
        self.assertIsNone(quota_alert.notice(lambda: "natedev"))
        _ = self.alert([self.note("codex1.md", "active", "0")], self.now)
        _ = self.refuse()
        self.assertIsNone(quota_alert.notice(lambda: "boss of bosses"))
        text = quota_alert.notice(lambda: "natedev")
        assert text is not None
        self.assertTrue(text.startswith(f"Codex refused work for quota (codex1 at 0%, resets {self.resets}): direct, "
                                        + "module_review moved to Claude at "))
        self.assertIsNone(quota_alert.notice(lambda: "natedev"))
        _ = self.alert([self.note("codex1.md", "active", "100")], self.now + timedelta(hours=1))
        text = quota_alert.notice(lambda: "natedev")
        assert text is not None
        self.assertTrue(text.startswith("Codex recovered at "))
        self.assertTrue(text.endswith(": moved back from Claude to Codex: direct, module_review."))


class CodexZeroEpisodeTests(unittest.TestCase):
    root: Path = Path()
    now: datetime = datetime.now(timezone.utc)

    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.phone: mock.MagicMock = mock.MagicMock()
        self.relay: mock.MagicMock = mock.MagicMock()
        self.swap: mock.MagicMock = mock.MagicMock()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            "HOME": str(self.root), "AGENT_NOTES_DIR": str(self.root), "USER_ZONE": "UTC",
            "CODEX_CONFIG_FILE": str(self.root / "absent"), "CODEX_MODELS_CACHE_FILE": str(self.root / "absent"),
            "CODEX_CATALOG_SYNC_STATE_FILE": str(self.root / "synced"),
        })))
        (self.root / "synced").touch()
        _ = self.enterContext(mock.patch.object(note_file, "AGENTS_DIR", self.root))
        _ = self.enterContext(mock.patch.object(showrunners, "NOTIFIER_STATE_DIR", self.root / "notifier"))
        _ = self.enterContext(mock.patch.object(session_table, "live_sessions", return_value=session_table.ReadSessions({}, frozenset())))
        config = self.root / "showrunners.json"
        _ = config.write_text(json.dumps({"threshold_percent": 1, "repeat_minutes": 30,
            "stall_minutes": 5, "faults_to": "director", "always": ["director"]}))
        registry = self.root / "agents.conf"
        _ = registry.write_text(REGISTRY)
        _ = self.enterContext(mock.patch.object(quota_alert, "CONFIG", config))
        _ = self.enterContext(mock.patch.object(quota_alert, "REGISTRY", registry))
        _ = self.enterContext(mock.patch.object(quota_alert, "STATE", self.root / "alerts.json"))
        _ = self.enterContext(mock.patch.object(os, "uname", return_value=os.uname_result(
            ("Linux", "natedev", "fixture", "fixture", "x86_64"))))
        from scripts.whoami import run_out
        _ = self.enterContext(mock.patch.object(run_out, "READINGS_LOG", self.root / "readings.jsonl"))
        _ = self.enterContext(mock.patch.object(quota_alert, "showrunners_now", return_value=running("runner")))
        self.phone = self.enterContext(mock.patch.object(quota_alert, "tell_user", return_value=quota_alert.PushSent()))
        self.relay = self.enterContext(mock.patch.object(quota_alert, "relay", return_value=None))
        self.swap = self.enterContext(mock.patch.object(accounts, "swap", return_value=["workers move to codex2"]))
        _ = self.enterContext(mock.patch.object(agent_accounts, "prepare_codex_home", return_value=agent_accounts.CodexHomePrepared()))
        for number, folder in ((1, ".codex1"), (2, ".codex"), (3, ".codex3")):
            home = self.root / folder
            home.mkdir()
            claims = base64.urlsafe_b64encode(json.dumps({"email": f"account{number}@example.invalid"}).encode()).decode().rstrip("=")
            _ = (home / "auth.json").write_text(json.dumps({"tokens": {"id_token": f"header.{claims}.signature"}}))
            _ = self.note(number)
        accounts.set_role("workers", "codex1")

    def note(self, number: int, setting: str = "on", *, left: int = 0) -> quota_alert.AgentNote:
        path = self.root / f"codex{number}.md"
        _ = path.write_text(f"---\nlogin: account{number}@example.invalid\nrole: worker\nsessions: 0\n"
            + f"weekly_remaining_usage: {left}\ncredit_balance: 6370\n"
            + f"resets: {(self.now + timedelta(days=2)).isoformat()}\n"
            + (f"use_credits: {setting}\n" if setting else "") + "---\n")
        note = read_note(path)
        assert isinstance(note, note_file.Note)
        return note

    def readings(self, now: datetime, *, selected: int = 1, left: int = 0,
                 balance: str | None = "6370", spare_left: int = 0,
                 spare_accounts: tuple[tuple[int, int, str], ...] = (),
                 spare_five_hour: tuple[tuple[int, int], ...] = ()) -> quota_alert.CodexAlertPassReadings:
        homes = {1: self.root / ".codex1", 2: self.root / ".codex", 3: self.root / ".codex3"}
        allowances = {number: (remaining, credits) for number, remaining, credits in spare_accounts}
        five_hour = dict(spare_five_hour)
        identity = agent_accounts.KnownCodexAccount(homes[selected], f"account{selected}@example.invalid", f"codex{selected}")
        reports = tuple(quota_alert.CodexHomeReading(home, Report("Codex",
            email=f"account{number}@example.invalid", source=CodexHomeSource(home),
            quotas=[Quota("Weekly", 100 - (left if number == selected else allowances.get(number, (spare_left, "0"))[0]),
                self.now + timedelta(days=number + 1))],
            five_hour=Quota("5-hour", 100 - five_hour.get(number, 100), now + timedelta(days=2)),
            credit_balance=balance if number == selected else allowances.get(number, (spare_left, "0"))[1]), now) for number, home in homes.items())
        return quota_alert.CodexAlertPassReadings(accounts.CodexWorkersAccount(identity.label, identity.home), identity, reports)

    def alert(self, now: datetime | None = None, *, selected: int = 1, left: int = 0,
              balance: str | None = "6370", spare_left: int = 0,
              spare_accounts: tuple[tuple[int, int, str], ...] = (),
              spare_five_hour: tuple[tuple[int, int], ...] = ()) -> list[str]:
        checked = now or self.now
        notes = [read_note(self.root / f"codex{number}.md") for number in (1, 2, 3)]
        lines = quota_alert.alert([note for note in notes if isinstance(note, note_file.Note)], checked,
            selection=ClaudeAccountsInUse(agent_accounts.UnreadableIdentity(Path.home() / ".claude", "fixture identity unavailable"), False, (), frozenset()),
            codex_readings=self.readings(checked, selected=selected, left=left, balance=balance,
                                        spare_left=spare_left, spare_accounts=spare_accounts,
                                        spare_five_hour=spare_five_hour))
        for call in self.phone.call_args_list:
            push = cast(RenderedPush, call.args[0])
            self.assertNotIn("default", (push.title + push.body).lower())
        for text in self.relayed_texts():
            self.assertNotIn("default", text.lower())
        return lines

    def stale_workers_target_readings(self, now: datetime) -> quota_alert.CodexAlertPassReadings:
        fresh = self.readings(now, left=0)
        checked = tuple(quota_alert.CodexHomeReading(home.home, home.report,
            self.now - timedelta(minutes=11) if home.home == self.root / ".codex3" else now)
            for home in fresh.checked_homes)
        stale = next(home for home in checked if home.home == self.root / ".codex3")
        usage_store.save([stale.report], stale.observed_at)
        return quota_alert.CodexAlertPassReadings(fresh.workers, fresh.selected_account, checked)

    def test_queued_claude_account_routes_workers_and_overrides_only_worker_launches(self) -> None:
        _ = (self.root / ".claude.json").write_text("{}")
        _ = (self.root / "claude4.md").write_text("---\nlogin: workers@example.invalid\n---\n")
        self.assertEqual(accounts.setup("claude4"), [])
        directory = accounts.account_dir("claude4")
        _ = (directory / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "workers@example.invalid"}}))
        _ = next_moves.add_next_move("workers", 1, "claude4")
        _ = self.alert(left=1)
        self.swap.assert_not_called()
        self.assertEqual(next_moves.read_next_moves(), ())
        self.assertEqual(quota_alert.registry_assignments()["direct"], "claude")
        with quota_alert.state_file() as state:
            fallback = state["fallback"]
            assert isinstance(fallback, quota_alert.AccountCodexQuotaFallback)
            self.assertEqual(fallback.claude_account, next_moves.ClaudeWorkersTarget("claude4"))
        routed = [row for row in moves.read_journal() if row["kind"] == "routed to claude"]
        self.assertEqual(len(routed), 1)
        self.assertEqual(routed[0].get("claude_account"), "claude4")
        self.assertEqual(role_map.read_role_map().workers_claude, role_map.NamedWorkersClaudeAccount("claude4", self.now + timedelta(days=2)))
        # A caller with an invalid account must not replace the workers route.
        selected = accounts.account_for_unscoped_launch({"CLAUDE_CONFIG_DIR": str(self.root / ".claude99")}, workers=True)
        self.assertEqual(selected, accounts.NamedAccount("claude4", directory))
        with self.assertRaises(accounts.AccountsError):
            _ = accounts.account_for_unscoped_launch({"CLAUDE_CONFIG_DIR": str(self.root / ".claude99")})
        (directory / ".claude.json").unlink()
        with self.assertRaisesRegex(accounts.AccountsError, "claude and /login"):
            _ = accounts.account_for_unscoped_launch({}, workers=True)

        # Recovery closes the shared route as well as the local fallback.
        with quota_alert.state_file() as state:
            back = quota_alert.switch_back(state, self.now + timedelta(days=8),
                self.readings(self.now + timedelta(days=8), left=100))
        self.assertIsInstance(back, quota_alert.SwitchBack)
        self.assertIsInstance(role_map.read_role_map().workers_claude, role_map.DefaultWorkersClaudeAccount)

    def test_failed_named_claude_switch_keeps_the_previous_workers_route(self) -> None:
        _ = (self.root / ".claude.json").write_text("{}")
        _ = (self.root / "claude4.md").write_text("---\nlogin: workers@example.invalid\n---\n")
        self.assertEqual(accounts.setup("claude4"), [])
        _ = (accounts.account_dir("claude4") / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "workers@example.invalid"}}))
        _ = next_moves.add_next_move("workers", 1, "claude4")
        with mock.patch.object(quota_alert, "edit_registry", return_value="fixture switch failed"):
            _ = self.alert(left=1)
        self.assertEqual(quota_alert.registry_assignments()["direct"], "codex")
        self.assertIsInstance(role_map.read_role_map().workers_claude, role_map.DefaultWorkersClaudeAccount)
        self.assertEqual(len(next_moves.read_next_moves()), 1)
        with quota_alert.state_file() as state:
            self.assertIsInstance(state["fallback"], quota_alert.NoCodexQuotaFallback)

    def test_failed_route_rollback_keeps_both_errors_and_the_unseen_switch_notice(self) -> None:
        owner = self.readings(self.now).selected_account
        assert isinstance(owner, agent_accounts.KnownCodexAccount)
        until = self.now + timedelta(days=2)
        note = self.note(1)
        original_write = role_map.write_role_map
        for failure in (role_map.RoleMapWriteRefused("fixture rollback refused"),
                        role_map.RoleMapUnavailable("fixture map unavailable"),
                        OSError("fixture disk full")):
            with self.subTest(failure=type(failure).__name__):
                original_write(role_map.WorkersClaudeRouteChange(
                    role_map.DEFAULT_WORKERS_CLAUDE_ACCOUNT, "reset the test route", self.now.isoformat()))

                def write_route(change: role_map.RoleMapChange | role_map.WorkersClaudeRouteChange) -> None:
                    if isinstance(change, role_map.WorkersClaudeRouteChange) and isinstance(
                            change.account, role_map.DefaultWorkersClaudeAccount):
                        raise failure
                    original_write(change)

                with quota_alert.state_file() as state:
                    state["episodes"][note.label] = {"since": self.now.isoformat(), "last": {}}
                    state["unseen"] = []
                    with (mock.patch.object(quota_alert, "edit_registry", return_value="fixture edit failed"),
                          mock.patch.object(role_map, "write_role_map", side_effect=write_route)):
                        result = quota_alert.switch_to_claude(state, note, self.now.isoformat(), owner,
                            hold=quota_alert.ScheduledMoveHold(until),
                            claude_account=next_moves.ClaudeWorkersTarget("claude4"))
                    assert isinstance(result, quota_alert.CodexWorkSwitchFailed)
                    self.assertIn("fixture edit failed", result.cause)
                    self.assertIn(f"route rollback failed: {failure}", result.cause)
                    self.assertEqual(state["episodes"][note.label].get("switch_failed"), result.cause)
                with quota_alert.state_file() as saved:
                    unseen = saved.get("unseen", [])
                    self.assertEqual(len(unseen), 1)
                    self.assertIn(result.cause, unseen[0])
                self.assertEqual(quota_alert.registry_assignments()["direct"], "codex")

    def test_uncertain_registry_switch_keeps_the_named_route_until_the_weekly_reset(self) -> None:
        for failed_step in ("edit", "confirmation"):
            with self.subTest(failed_step=failed_step):
                _ = quota_alert.REGISTRY.write_text(REGISTRY)
                owner = self.readings(self.now).selected_account
                assert isinstance(owner, agent_accounts.KnownCodexAccount)
                until = self.now + timedelta(days=2)
                note = self.note(1)
                original_edit = quota_alert.edit_registry

                def edit_then_lose_confirmation(editor: str, *arguments: str) -> str | None:
                    result = original_edit(editor, *arguments)
                    if failed_step == "edit":
                        raise OSError("fixture edit reply lost")
                    return result

                original_read = quota_alert.registry_assignments
                with quota_alert.state_file() as state:
                    with (mock.patch.object(quota_alert, "edit_registry", side_effect=edit_then_lose_confirmation),
                          mock.patch.object(quota_alert, "registry_assignments", side_effect=[
                              original_read(), OSError("fixture confirmation failed")])):
                        result = quota_alert.switch_to_claude(state, note, self.now.isoformat(), owner,
                            hold=quota_alert.ScheduledMoveHold(until),
                            claude_account=next_moves.ClaudeWorkersTarget("claude4"))
                    self.assertIsInstance(result, quota_alert.CodexWorkSwitchFailed)
                    self.assertTrue(any("failed" in line for line in state.get("unseen", [])))
                self.assertEqual(original_read()["direct"], "claude")
                self.assertEqual(role_map.read_role_map().workers_claude,
                                 role_map.NamedWorkersClaudeAccount("claude4", until))

    def test_lost_quota_state_does_not_keep_the_named_workers_account_past_its_reset(self) -> None:
        _ = (self.root / ".claude.json").write_text("{}")
        for label in ("claude2", "claude4"):
            _ = (self.root / f"{label}.md").write_text(f"---\nlogin: {label}@example.invalid\n---\n")
            self.assertEqual(accounts.setup(label), [])
            _ = (accounts.account_dir(label) / ".claude.json").write_text(json.dumps({
                "oauthAccount": {"emailAddress": f"{label}@example.invalid"}}))
        _ = next_moves.add_next_move("workers", 1, "claude4")
        _ = self.alert(left=1)
        caller = {"CLAUDE_CONFIG_DIR": str(accounts.account_dir("claude2"))}
        self.assertEqual(accounts.account_for_unscoped_launch(caller, workers=True),
                         accounts.NamedAccount("claude4", accounts.account_dir("claude4")))
        _ = quota_alert.STATE.write_text("invalid quota state")
        until = self.now + timedelta(days=2)
        with quota_alert.state_file() as state:
            self.assertIsInstance(state["fallback"], quota_alert.NoCodexQuotaFallback)
            self.assertIsInstance(quota_alert.switch_back(state, until, self.readings(until, left=100)),
                                  quota_alert.NoCodexFallbackRestoration)
        with mock.patch.object(accounts, "datetime", wraps=datetime, now=mock.Mock(return_value=until)):
            self.assertEqual(accounts.account_for_unscoped_launch(caller, workers=True),
                             accounts.NamedAccount("claude2", accounts.account_dir("claude2")))

    def test_queued_claude_account_with_no_folder_waits_with_setup_step(self) -> None:
        _ = (self.root / "claude4.md").write_text("---\nlogin: workers@example.invalid\n---\n")
        head = next_moves.add_next_move("workers", 1, "claude4")
        _ = self.alert(left=1)
        self.assertEqual(next_moves.read_next_moves(), (head,))
        self.swap.assert_not_called()
        self.assertEqual(quota_alert.registry_assignments()["direct"], "codex")
        self.assertTrue(any("accounts.py setup claude4" in text for text in self.relayed_texts()))

    def test_stale_workers_head_blocks_a_fresh_later_entry_and_notifies_once(self) -> None:
        head = next_moves.add_next_move("workers", 1, "codex3")
        later = next_moves.add_next_move("workers", 1, "codex2")
        with (mock.patch.object(quota_alert, "recipients", return_value=["director"]),
              mock.patch.object(accounts, "check_codex_target", wraps=accounts.check_codex_target) as check,
              redirect_stdout(io.StringIO()) as output):
            for minute in (0, 2):
                now = self.now + timedelta(minutes=minute)
                returned = quota_alert.run_workers_next_moves(self.stale_workers_target_readings(now), now)
                self.assertEqual(returned.workers, accounts.CodexWorkersAccount("codex1", self.root / ".codex1"))
        self.swap.assert_not_called()
        self.assertEqual([call.args[0] for call in check.call_args_list], ["codex3", "codex3"])
        self.assertEqual(next_moves.read_next_moves(), (head, later))
        self.assertEqual(moves.read_moves(), [])
        self.relay.assert_called_once()
        recipient, text, _ = cast(tuple[str, str, str | None], self.relay.call_args.args)
        self.assertEqual(recipient, "director")
        self.assertNotIn(head.id, text)
        for detail in ("workers 1", "codex3", "due", "no fresh reading",
                       (self.now - timedelta(minutes=11)).strftime("%H:%M")):
            self.assertIn(detail, text)
        self.assertIn(text, output.getvalue())
        self.phone.assert_not_called()

    def test_queued_workers_head_blocks_a_provider_refusal_fallback_swap(self) -> None:
        head = next_moves.add_next_move("workers", 1, "codex3")
        readings = self.readings(self.now, left=80)
        identity = readings.selected_account
        assert isinstance(identity, agent_accounts.KnownCodexAccount)
        with mock.patch.object(accounts, "pick", wraps=accounts.pick) as pick:
            _ = quota_alert.blocked([self.note(1, left=80)], identity, self.now,
                codex_readings=readings, window=refusals.KnownWindow("weekly"))
        pick.assert_not_called()
        self.swap.assert_not_called()
        self.assertEqual(accounts.role_account("workers").label, "codex1")
        self.assertEqual(next_moves.read_next_moves(), (head,))
        self.assertEqual(quota_alert.registry_assignments()["direct"], "codex")
        waits = moves.read_moves()
        self.assertEqual([row["kind"] for row in waits], ["waited"])
        waited = waits[0]
        assert waited["kind"] == "waited"
        self.assertEqual((waited["window"], waited["subject"]), ("weekly", "workers"))
        self.assertIn(refusals.key("codex", identity.login), refusals.load())

    def test_queued_workers_head_blocks_a_credits_off_fallback_swap(self) -> None:
        head = next_moves.add_next_move("workers", 1, "codex3")
        notes = [self.note(1, "off"), self.note(2), self.note(3)]
        with (mock.patch.object(quota_alert, "recipients", return_value=["director"]),
              redirect_stdout(io.StringIO())):
            _ = quota_alert.alert(notes, self.now,
                selection=ClaudeAccountsInUse(agent_accounts.UnreadableIdentity(
                    self.root / ".claude", "fixture identity unavailable"), False, (), frozenset()),
                codex_readings=self.stale_workers_target_readings(self.now))
        self.swap.assert_not_called()
        self.assertEqual(accounts.role_account("workers").label, "codex1")
        self.assertEqual(next_moves.read_next_moves(), (head,))
        self.assertEqual(quota_alert.registry_assignments()["direct"], "codex")
        self.assertEqual(moves.read_moves(), [])

    def test_removing_a_queued_head_releases_the_still_zero_credits_off_fallback_once(self) -> None:
        _ = self.note(1, "off")
        head = next_moves.add_next_move("workers", 1, "codex1")
        _ = self.alert(spare_left=50)
        self.swap.assert_not_called()
        self.assertEqual(next_moves.read_next_moves(), (head,))
        next_moves.remove_next_move("workers", next_moves.GroupPosition(1))
        _ = self.alert(self.now + timedelta(minutes=2), spare_left=50)
        _ = self.alert(self.now + timedelta(minutes=4), spare_left=50)
        self.swap.assert_called_once_with("workers", "codex2", why="codex1 weekly usage used; credits off", cause="credits off")
        self.assertEqual(len([row for row in moves.read_moves() if row["kind"] == "requested"]), 1)

    def test_workers_target_allowance_changes_below_the_same_threshold_send_one_notice(self) -> None:
        head = next_moves.add_next_move("workers", 20, "codex3")
        with mock.patch.object(quota_alert, "recipients", return_value=["director"]):
            for minute, left in ((0, 10), (2, 9)):
                now = self.now + timedelta(minutes=minute)
                readings = self.readings(now, left=1, spare_accounts=((3, left, "0"),))
                _ = quota_alert.run_workers_next_moves(readings, now)
        self.relay.assert_called_once()
        text = cast(str, self.relay.call_args.args[1])
        self.assertIn("10% left, at or below 20%", text)
        self.assertEqual(next_moves.read_next_moves(), (head,))
        self.swap.assert_not_called()
        self.phone.assert_not_called()

    def test_workers_successor_becomes_eligible_only_after_its_head_is_consumed(self) -> None:
        head = next_moves.add_next_move("workers", 1, "codex3")
        later = next_moves.add_next_move("workers", 80, "codex2")
        readings = self.readings(self.now, left=1,
            spare_accounts=((3, 50, "0"), (2, 90, "0")))
        with mock.patch.object(accounts, "check_codex_target", wraps=accounts.check_codex_target) as check:
            returned = quota_alert.run_workers_next_moves(readings, self.now)
            self.swap.assert_called_once_with("workers", "codex3", why=f"auto: {head.id}", cause="auto")
            self.assertEqual(next_moves.read_next_moves(), (later,))
            self.assertEqual([call.args[0] for call in check.call_args_list], ["codex3"])
            self.assertEqual(returned.workers, accounts.CodexWorkersAccount("codex3", self.root / ".codex3"))
            self.swap.reset_mock()
            check.reset_mock()
            now = self.now + timedelta(minutes=2)
            readings = self.readings(now, selected=3, left=50,
                spare_accounts=((2, 90, "0"),))
            returned = quota_alert.run_workers_next_moves(readings, now)
        self.swap.assert_called_once_with("workers", "codex2", why=f"auto: {later.id}", cause="auto")
        self.assertEqual([call.args[0] for call in check.call_args_list], ["codex2"])
        self.assertEqual(next_moves.read_next_moves(), ())
        self.assertEqual(returned.workers, accounts.CodexWorkersAccount("codex2", self.root / ".codex"))
        self.assertEqual([row["why"] for row in moves.read_moves() if row["kind"] == "requested"],
                         [f"auto: {head.id}", f"auto: {later.id}"])
        self.relay.assert_not_called()

    def test_old_paid_notices_are_rebuilt_before_retry_from_the_current_reading(self) -> None:
        from push_texts import render
        for suffix in ("_credits", "_credits_last"):
            for queued in (False, True):
                with self.subTest(suffix=suffix, queued=queued):
                    quota_alert.STATE.unlink(missing_ok=True)
                    next_moves.next_moves_file().unlink(missing_ok=True)
                    _ = self.alert()
                    with quota_alert.state_file() as state:
                        episode = state.get("codex_zero", {})["codex1"]
                        episode["phone"] = "due"
                        episode["notice"] = quota_alert.ZeroEpisodeNotice(
                            id="quota." + "codex_on" + suffix, values={"file": "codex 1.md"})
                        opened = episode["since"]
                    if queued:
                        _ = next_moves.add_next_move("workers", 1, "codex1")
                    self.phone.reset_mock()
                    self.relay.reset_mock()
                    _ = self.alert(self.now + timedelta(minutes=2), balance="6310")
                    push, = self.zero_pushes()
                    expected_id = "quota.paid_credits_queued" if queued else "quota.paid_credits"
                    self.assertEqual(push.id, expected_id)
                    self.assertEqual(push, render(expected_id, **push.values))
                    self.assertEqual(push.values["credits"], "6,310")
                    with quota_alert.state_file() as state:
                        episode = state.get("codex_zero", {})["codex1"]
                        self.assertEqual(episode["since"], opened)
                        self.assertEqual(episode["notice"]["id"], "quota.paid_credits")
                        self.assertNotEqual(episode["phone"], "due")

    def test_old_paid_notice_keeps_the_earlier_reset_of_a_paid_only_spare(self) -> None:
        from push_texts import render

        _ = self.alert(selected=2, spare_accounts=((1, 0, "6370"),))
        fresh, = self.zero_pushes()
        earlier_reset = quota_alert.reset_display(refusals.KnownReset(self.now + timedelta(days=2)))
        self.assertEqual(fresh.values["next_reset"], earlier_reset)
        self.assertNotEqual(fresh.values["refill"], earlier_reset)
        with quota_alert.state_file() as state:
            episode = state.get("codex_zero", {})["codex2"]
            episode["phone"] = "due"
            episode["notice"] = quota_alert.ZeroEpisodeNotice(
                id="quota." + "codex_on" + "_credits_last", values={"file": "codex2.md"})
        self.phone.reset_mock()

        _ = self.alert(self.now + timedelta(minutes=2), selected=2, spare_accounts=((1, 0, "6370"),))

        rebuilt, = self.zero_pushes()
        self.assertEqual(rebuilt, fresh)
        self.assertEqual(rebuilt, render("quota.paid_credits", **rebuilt.values))
        with quota_alert.state_file() as state:
            self.assertEqual(state.get("codex_zero", {})["codex2"]["notice"]["values"]["next_reset"], earlier_reset)
        self.swap.assert_not_called()

    def test_legacy_episode_keys_continue_without_a_second_opening_alert(self) -> None:
        _ = self.alert()
        original = quota_alert.STATE.read_text()
        _ = quota_alert.STATE.write_text(original.replace('"codex1":', '"codex 1":'))
        self.phone.reset_mock()
        self.relay.reset_mock()
        _ = self.alert(self.now + timedelta(minutes=2))
        self.phone.assert_not_called()
        self.relay.assert_not_called()
        with quota_alert.state_file() as state:
            self.assertIn("codex1", state.get("codex_zero", {}))
            self.assertNotIn("codex 1", state.get("codex_zero", {}))
            state["unseen"] = ["next authorized write"]
        self.assertNotIn('"codex 1":', quota_alert.STATE.read_text())

    def test_legacy_pending_notice_retry_changes_only_account_spelling(self) -> None:
        _ = self.alert()
        self.phone.reset_mock()
        self.relay.reset_mock()
        with quota_alert.state_file() as state:
            episode = state.get("codex_zero", {})["codex1"]
            episode["phone"] = "due"
            notice = episode["notice"]
            notice["values"]["credits"] = "codex 1 :: preserve  two spaces; claude 2, codex 2!\nNext line."
            expected = render(notice["id"], **{**notice["values"],
                "credits": "codex1 :: preserve  two spaces; claude2, codex2!\nNext line."})
            episode["forecast_notice"] = quota_alert.ZeroEpisodeNotice(id="quota.codex_credits_forecast", values={
                "account": "codex 1", "horizon": "until 03:00", "forecast": "codex 1:  70 credits; keep punctuation!",
                "destination": "then claude 2 / codex 2\nnext"})
            opened = episode["since"]
        _ = self.alert(self.now + timedelta(minutes=2))
        self.phone.assert_called_once_with(expected)
        self.relay.assert_not_called()
        with quota_alert.state_file() as state:
            episode = state.get("codex_zero", {})["codex1"]
            self.assertEqual(episode["since"], opened)
            self.assertEqual(episode["notice"]["values"]["credits"],
                             "codex1 :: preserve  two spaces; claude2, codex2!\nNext line.")
            assert "forecast_notice" in episode
            self.assertEqual(episode["forecast_notice"]["values"], {
                "account": "codex1", "horizon": "until 03:00", "forecast": "codex1:  70 credits; keep punctuation!",
                "destination": "then claude2 / codex2\nnext"})

    def zero_pushes(self) -> list[RenderedPush]:
        return [push for call in self.phone.call_args_list
                if (push := cast(RenderedPush, call.args[0])).id != "quota.codex_next_move"]

    def relayed_texts(self) -> list[str]:
        return [cast(str, call.args[1]) for call in self.relay.call_args_list]

    def history(self, balances: list[tuple[int, float]]) -> None:
        _ = (self.root / "readings.jsonl").write_text("".join(json.dumps({"account": "codex1",
            "at": (self.now + timedelta(minutes=minute)).isoformat(), "remaining": 0, "credits": value})
            + "\n" for minute, value in balances))

    def test_no_room_uses_the_real_paid_credits_template_once(self) -> None:
        from push_texts import catalog, render_entry, user_send_argv
        entry = next(entry for entry in catalog() if entry.id == "quota.paid_credits")
        _ = self.alert()
        _ = self.alert(self.now + timedelta(minutes=2))
        push, = self.zero_pushes()
        self.assertEqual(push.id, "quota.paid_credits")
        self.assertEqual(set(push.values), {"sender", "account", "machine", "credits", "refill", "next_reset"})
        self.assertEqual(push.values["sender"], "quota_alert")
        self.assertEqual(push, render_entry(entry, push.values))
        self.assertEqual(push.values["account"], "codex1")
        self.assertIn("6,370", push.values["credits"])
        reset = quota_alert.reset_display(refusals.KnownReset(self.now + timedelta(days=2)))
        self.assertEqual(push.values["refill"], reset)
        self.assertEqual(push.values["next_reset"], reset)
        argv = user_send_argv(push)
        self.assertIn("quota.paid_credits", argv)
        self.assertIn("--text-id", argv)
        for key, value in push.values.items():
            self.assertIn(f"{key}={value}", argv)
        self.swap.assert_not_called()

    def test_real_paid_credits_notice_keeps_the_queued_move_in_control(self) -> None:
        head = next_moves.add_next_move("workers", 1, "codex1")
        with mock.patch.object(quota_alert, "switch_to_claude", side_effect=AssertionError("queued credits routed work")):
            log = self.alert()
            _ = self.note(1, "off")
            _ = self.alert(self.now + timedelta(minutes=2))
        push, = self.zero_pushes()
        self.assertTrue(any(push.body in line for line in log), log)
        self.assertEqual(push.id, "quota.paid_credits_queued")
        self.assertNotIn("next_reset", push.values)
        self.assertNotIn("Claude", push.body)
        self.assertEqual(next_moves.read_next_moves(), (head,))
        self.assertEqual(accounts.role_account("workers").label, "codex1")
        self.swap.assert_not_called()

    def test_unmoved_included_room_during_an_open_fallback_uses_paid_credits(self) -> None:
        from push_texts import render
        owner = self.readings(self.now).selected_account
        assert isinstance(owner, agent_accounts.KnownCodexAccount)
        with quota_alert.state_file() as state:
            state["fallback"] = quota_alert.AccountCodexQuotaFallback(owner, self.now.isoformat(), ("delegate",))
        _ = self.alert(spare_left=50)
        push, = self.zero_pushes()
        self.assertEqual(push.id, "quota.paid_credits")
        self.assertEqual(push, render("quota.paid_credits", **push.values))
        self.swap.assert_not_called()

    def test_credits_on_moves_to_room_without_a_phone_or_session_notice(self) -> None:
        _ = self.alert(selected=2, spare_accounts=((1, 100, "0"),))
        self.swap.assert_called_once()
        self.assertEqual(self.swap.call_args.args, ("workers", "codex1"))
        self.phone.assert_not_called()
        self.relay.assert_not_called()
        with quota_alert.state_file() as state:
            self.assertNotIn("codex2", state.get("codex_zero", {}))

    def test_zero_accounts_with_only_paid_credits_keep_the_workers_on_one_account(self) -> None:
        for minute in (0, 2, 4):
            _ = self.alert(self.now + timedelta(minutes=minute),
                           spare_accounts=((2, 0, "6370"), (3, 0, "6370")))
        self.swap.assert_not_called()
        push, = self.zero_pushes()
        self.assertEqual(push.values["account"], "codex1")
        self.assertFalse(any(row["kind"] == "requested" for row in moves.read_moves()))

    def test_open_paid_episode_moves_to_new_included_room_on_the_next_pass(self) -> None:
        _ = self.alert()
        self.swap.assert_not_called()
        self.phone.reset_mock()
        self.relay.reset_mock()
        _ = self.alert(self.now + timedelta(minutes=2), spare_accounts=((2, 100, "0"),))
        self.swap.assert_called_once_with("workers", "codex2",
            why="codex1 weekly usage used; move to room", cause="auto", included_only=True)
        self.phone.assert_not_called()
        self.relay.assert_not_called()
        with quota_alert.state_file() as state:
            self.assertNotIn("codex1", state.get("codex_zero", {}))

    def test_credits_on_does_not_move_to_a_paid_only_five_hour_window(self) -> None:
        _ = self.alert(spare_accounts=((2, 50, "6370"),), spare_five_hour=((2, 0),))
        self.swap.assert_not_called()
        self.assertEqual(len(self.zero_pushes()), 1)

    def test_credits_turned_on_during_a_queued_wait_use_room_when_the_queue_is_removed(self) -> None:
        _ = self.note(1, "off")
        head = next_moves.add_next_move("workers", 1, "codex1")
        _ = self.alert(spare_left=50)
        _ = self.note(1, "on")
        _ = self.alert(self.now + timedelta(minutes=2), spare_left=50)
        self.assertEqual(next_moves.read_next_moves(), (head,))
        self.swap.assert_not_called()
        next_moves.remove_next_move("workers", next_moves.GroupPosition(1))
        self.phone.reset_mock()
        self.relay.reset_mock()
        _ = self.alert(self.now + timedelta(minutes=4), spare_left=50)
        self.swap.assert_called_once_with("workers", "codex2",
            why="codex1 weekly usage used; move to room", cause="auto", included_only=True)
        self.phone.assert_not_called()
        self.relay.assert_not_called()
        with quota_alert.state_file() as state:
            self.assertNotIn("codex1", state.get("codex_zero", {}))

    def test_zero_credit_allowance_sends_one_phone_push_and_logs_without_session_delivery(self) -> None:
        log = self.alert()
        self.assertEqual(len(self.zero_pushes()), 1)
        push = self.zero_pushes()[-1]
        self.relay.assert_not_called()
        self.assertIn("6,370", push.body)
        self.assertTrue(any(push.body in line for line in log), log)
        self.assertIn("Claude", push.body)
        self.assertEqual(push.need, "decision")
        self.assertIsInstance(push.action, ActionRequired)
        assert isinstance(push.action, ActionRequired)
        self.assertIn("use_credits: off", push.action.text)
        self.assertIn("codex1.md", push.action.text)
        self.assertIn("natedev", push.action.text)
        self.assertIn("Its plan refills", push.body)
        self.assertIn("paid credits", push.title + push.body)
        for minute in range(2, 32, 2):
            _ = self.alert(self.now + timedelta(minutes=minute))
        self.assertEqual((len(self.zero_pushes()), self.relay.call_count), (1, 0))
        self.swap.assert_not_called()
        with quota_alert.state_file() as state:
            self.assertIn("codex1", state["episodes"])
            episode = state.get("codex_zero", {})["codex1"]
            self.assertEqual(episode["credits"], "on")
            self.assertNotEqual(episode["phone"], "due")
            self.assertNotIn("sessions", episode)

    def assert_queued_credits_wait_without_a_fallback_promise(self, target: str) -> None:
        head = next_moves.add_next_move("workers", 1, target)
        with mock.patch.object(quota_alert, "switch_to_claude", side_effect=AssertionError("queued credits routed work")):
            # An alternative target has no current reading, so the head cannot run.
            for minute in (0, 2):
                now = self.now + timedelta(minutes=minute)
                _ = quota_alert.alert([self.note(1), self.note(2), self.note(3)], now,
                    selection=ClaudeAccountsInUse(agent_accounts.UnreadableIdentity(
                        self.root / ".claude", "fixture identity unavailable"), False, (), frozenset()),
                    codex_readings=self.stale_workers_target_readings(now))
            push, = self.zero_pushes()
            self.assertEqual(push.id, "quota.paid_credits_queued")
            self.assertIn("codex1", push.body)
            for forbidden in ("codex2", "codex3", "Claude", "Within minutes", "workers move"):
                self.assertNotIn(forbidden, push.body)
            from push_texts import render
            self.assertEqual(push, render("quota.paid_credits_queued", **push.values))
            self.assertEqual(set(push.values), {"sender", "account", "machine", "credits", "refill"})
            self.assertIn("workers stop and wait for your queued move", push.body)
            _ = self.note(1, "off")
            _ = self.alert(self.now + timedelta(minutes=4),
                spare_accounts=((3, 0, "0"),))
        self.swap.assert_not_called()
        self.assertFalse(any(row["kind"] == "requested" for row in moves.read_moves()))
        self.assertEqual(accounts.role_account("workers").label, "codex1")
        self.assertEqual(next_moves.read_next_moves(), (head,))

    def test_paid_credits_with_a_self_targeting_workers_head_promise_only_the_queue(self) -> None:
        self.assert_queued_credits_wait_without_a_fallback_promise("codex1")

    def test_paid_credits_with_a_blocked_other_workers_head_promise_only_the_queue(self) -> None:
        self.assert_queued_credits_wait_without_a_fallback_promise("codex3")

    def test_queued_workers_credit_forecast_promises_only_the_entry_and_moves_nothing(self) -> None:
        head = next_moves.add_next_move("workers", 1, "codex1")
        with mock.patch.object(quota_alert, "switch_to_claude", side_effect=AssertionError("queued forecast routed work")):
            _ = self.alert()
            self.phone.reset_mock()
            self.history([(0, 6370), (30, 6340), (60, 6310)])
            _ = self.alert(self.now + timedelta(hours=1), balance="6310")
        forecast, = self.zero_pushes()
        self.assertEqual(forecast.id, "quota.codex_credits_forecast")
        self.assertIn("60", forecast.body)
        self.assertIn("6,310", forecast.body)
        self.assertEqual(forecast.values["destination"],
                         "When credits run out, workers wait for the account move you queued.")
        for forbidden in ("codex2", "codex3", "Claude"):
            self.assertNotIn(forbidden, forecast.body)
        self.assertEqual(next_moves.read_next_moves(), (head,))
        self.swap.assert_not_called()

    def test_failed_workers_blocked_head_delivery_retries_until_it_is_delivered(self) -> None:
        head = next_moves.add_next_move("workers", 1, "codex1")
        self.relay.side_effect = ["fixture recipient unavailable", None]
        with mock.patch.object(quota_alert, "recipients", return_value=["director"]):
            for minute in (0, 2, 4):
                now = self.now + timedelta(minutes=minute)
                _ = quota_alert.run_workers_next_moves(self.readings(now), now)
                with quota_alert.state_file() as state:
                    self.assertEqual(head.id in state.get("workers_move_notices", {}), minute > 0)
        self.assertEqual(self.relay.call_count, 2)
        self.assertEqual(self.relayed_texts()[0], self.relayed_texts()[1])
        self.swap.assert_not_called()
        self.phone.assert_not_called()

    def test_paid_credit_retry_after_queue_removal_restores_the_never_queued_notice(self) -> None:
        _ = self.alert()
        expected_push, = self.zero_pushes()
        with quota_alert.state_file() as state:
            expected_notice = state.get("codex_zero", {})["codex1"]["notice"]
        for opened_while_queued in (False, True):
            with self.subTest(opened_while_queued=opened_while_queued):
                quota_alert.STATE.unlink()
                usage_store.store_file().unlink(missing_ok=True)
                next_moves.next_moves_file().unlink(missing_ok=True)
                self.phone.reset_mock()
                self.phone.side_effect = [quota_alert.PushFailed("fixture offline"),
                    quota_alert.PushFailed("fixture offline"), quota_alert.PushSent()]
                if opened_while_queued:
                    _ = next_moves.add_next_move("workers", 1, "codex1")
                _ = self.alert()
                with quota_alert.state_file() as state:
                    self.assertEqual(state.get("codex_zero", {})["codex1"]["notice"], expected_notice)
                if not opened_while_queued:
                    _ = next_moves.add_next_move("workers", 1, "codex1")
                self.assertEqual(len(next_moves.read_next_moves()), 1)
                _ = self.alert(self.now + timedelta(minutes=2))
                queued_push = self.zero_pushes()[-1]
                self.assertEqual(queued_push.id, "quota.paid_credits_queued")
                self.assertNotIn("next_reset", queued_push.values)
                from push_texts import render
                self.assertEqual(queued_push, render("quota.paid_credits_queued", **queued_push.values))
                self.assertIn("wait for your queued move", queued_push.body)
                with quota_alert.state_file() as state:
                    episode = state.get("codex_zero", {})["codex1"]
                    self.assertEqual(episode["phone"], "due")
                    self.assertEqual(episode["notice"], expected_notice)
                next_moves.remove_next_move("workers", next_moves.GroupPosition(1))
                _ = self.alert(self.now + timedelta(minutes=4))
                self.assertEqual(self.zero_pushes()[-1], expected_push)
                with quota_alert.state_file() as state:
                    episode = state.get("codex_zero", {})["codex1"]
                    self.assertNotEqual(episode["phone"], "due")
                    self.assertEqual(episode["notice"], expected_notice)
                self.phone.side_effect = None
        self.swap.assert_not_called()

    def test_credit_forecast_retry_after_queue_removal_restores_the_never_queued_notice(self) -> None:
        self.history([(0, 6370), (30, 6340), (60, 6310)])
        _ = self.alert()
        _ = self.alert(self.now + timedelta(hours=1), balance="6310")
        expected_push = self.zero_pushes()[-1]
        self.assertEqual(expected_push.id, "quota.codex_credits_forecast")
        with quota_alert.state_file() as state:
            episode = state.get("codex_zero", {})["codex1"]
            assert "forecast_notice" in episode
            expected_notice = episode["forecast_notice"]
        for opened_while_queued in (False, True):
            with self.subTest(opened_while_queued=opened_while_queued):
                quota_alert.STATE.unlink()
                usage_store.store_file().unlink(missing_ok=True)
                next_moves.next_moves_file().unlink(missing_ok=True)
                self.phone.reset_mock()
                self.phone.side_effect = None
                _ = self.alert()
                self.phone.reset_mock()
                self.phone.side_effect = [quota_alert.PushFailed("fixture offline"),
                    quota_alert.PushFailed("fixture offline"), quota_alert.PushSent()]
                if opened_while_queued:
                    _ = next_moves.add_next_move("workers", 1, "codex1")
                _ = self.alert(self.now + timedelta(hours=1), balance="6310")
                with quota_alert.state_file() as state:
                    episode = state.get("codex_zero", {})["codex1"]
                    assert "forecast_notice" in episode
                    self.assertEqual(episode["forecast_notice"], expected_notice)
                if not opened_while_queued:
                    _ = next_moves.add_next_move("workers", 1, "codex1")
                self.assertEqual(len(next_moves.read_next_moves()), 1)
                _ = self.alert(self.now + timedelta(minutes=62), balance="6310")
                self.assertIn("wait for the account move you queued", self.zero_pushes()[-1].body)
                with quota_alert.state_file() as state:
                    episode = state.get("codex_zero", {})["codex1"]
                    assert "forecast_phone" in episode and "forecast_notice" in episode
                    self.assertEqual(episode["forecast_phone"], "due")
                    self.assertEqual(episode["forecast_notice"], expected_notice)
                next_moves.remove_next_move("workers", next_moves.GroupPosition(1))
                _ = self.alert(self.now + timedelta(minutes=64), balance="6310")
                self.assertEqual(self.zero_pushes()[-1], expected_push)
                with quota_alert.state_file() as state:
                    episode = state.get("codex_zero", {})["codex1"]
                    assert "forecast_phone" in episode and "forecast_notice" in episode
                    self.assertNotEqual(episode["forecast_phone"], "due")
                    self.assertEqual(episode["forecast_notice"], expected_notice)
                self.phone.side_effect = None
        self.swap.assert_not_called()

    def test_zero_phone_failure_retries_only_the_phone(self) -> None:
        attempts = 0

        def fail_zero_once(push: RenderedPush) -> quota_alert.PushDelivery:
            nonlocal attempts
            if push.id == "quota.codex_next_move":
                return quota_alert.PushSent()
            attempts += 1
            return quota_alert.PushFailed("offline") if attempts == 1 else quota_alert.PushSent()

        self.phone.side_effect = fail_zero_once
        _ = self.alert()
        with quota_alert.state_file() as state:
            self.assertEqual(state.get("codex_zero", {})["codex1"]["phone"], "due")
        _ = self.alert(self.now + timedelta(minutes=2))
        _ = self.alert(self.now + timedelta(minutes=4))
        self.assertEqual((len(self.zero_pushes()), self.relay.call_count), (2, 0))

    def test_zero_credits_off_moves_once_to_the_soonest_reset_and_records_its_policy_cause(self) -> None:
        _ = self.note(1, "off")
        _ = self.alert(spare_left=50)
        _ = self.alert(self.now + timedelta(minutes=2), spare_left=50)
        self.swap.assert_called_once_with("workers", "codex2", why="codex1 weekly usage used; credits off", cause="credits off")
        entries = [entry for entry in moves.read_moves() if entry["kind"] == "requested"]
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        assert entry["kind"] == "requested"
        self.assertEqual((entry["from"], entry["to"], entry["window"], entry.get("cause")),
                         ("codex1", "codex2", "weekly", "credits off"))
        self.assertEqual(entry["why"], "codex1 weekly usage used; credits off")
        self.assertIn("codex2", self.zero_pushes()[-1].body)

    def test_zero_credits_off_without_room_switches_to_claude_on_the_same_pass_and_records_one_notice(self) -> None:
        _ = self.note(1, "off")
        with mock.patch.object(quota_alert, "switch_to_claude", wraps=quota_alert.switch_to_claude) as fallback:
            _ = self.alert(spare_left=0)
            fallback.assert_called_once()
            self.assertEqual(quota_alert.registry_assignments()["direct"], "claude")
            self.assertEqual(quota_alert.registry_assignments()["module_review"], "claude")
            for minute in (2, 30):
                _ = self.alert(self.now + timedelta(minutes=minute), spare_left=0)
            fallback.assert_called_once()
        self.swap.assert_not_called()
        self.assertEqual(len([entry for entry in moves.read_moves() if entry["kind"] == "no room"]), 1)
        routed = [entry for entry in moves.read_journal() if entry["kind"] == "routed to claude"]
        self.assertEqual(len(routed), 1)
        self.assertEqual(routed[0]["cause"], "credits off")
        self.assertEqual(set(routed[0]["moved"]), {"direct", "module_review"})
        push = self.zero_pushes()[-1]
        self.assertIn("Claude", push.body)
        self.assertIn("codex1", push.body)
        self.assertNotIn("when Codex refuses", push.body)
        self.assertEqual((len(self.zero_pushes()), self.relay.call_count), (1, 0))

    def test_zero_credits_on_without_another_account_names_claude_as_the_destination(self) -> None:
        _ = self.alert(spare_left=0)
        push = self.zero_pushes()[-1]
        self.assertIn("Claude", push.body)
        self.assertIn("6,370", push.body)
        self.swap.assert_not_called()

    def test_zero_failed_swap_reports_the_failure_and_switches_to_claude_on_the_same_pass(self) -> None:
        _ = self.note(1, "off")
        self.swap.side_effect = accounts.AccountsError("server could not stop")
        with mock.patch.object(quota_alert, "switch_to_claude", wraps=quota_alert.switch_to_claude) as fallback:
            _ = self.alert(spare_left=50)
            fallback.assert_called_once()
            for minute in (2, 4, 30, 60):
                _ = self.alert(self.now + timedelta(minutes=minute), spare_left=50)
                self.assertEqual(quota_alert.registry_assignments()["direct"], "claude")
            fallback.assert_called_once()
        self.assertEqual(quota_alert.registry_assignments()["direct"], "claude")
        entries = moves.read_moves()
        requested = [entry for entry in entries if entry["kind"] == "requested"]
        failed = [entry for entry in entries if entry["kind"] == "failed"]
        self.assertEqual((len(requested), len(failed)), (1, 1))
        self.assertEqual(failed[0]["move_id"], requested[0]["move_id"])
        self.assertIn("server could not stop", self.zero_pushes()[-1].body)
        self.assertIn("Claude", self.zero_pushes()[-1].body)
        self.swap.assert_called_once()

    def test_credits_off_fallback_hold_survives_reload_and_blocks_recovery_until_the_earliest_refill(self) -> None:
        _ = self.note(1, "off")
        log = self.alert(spare_left=0)
        until = self.now + timedelta(days=2)
        with quota_alert.state_file() as state:
            fallback = state["fallback"]
            self.assertIsInstance(fallback, quota_alert.AccountCodexQuotaFallback)
            assert isinstance(fallback, quota_alert.AccountCodexQuotaFallback)
            self.assertEqual(fallback.hold, quota_alert.CreditsOffHold(until))
        saved = cast(quota_alert.StoredState, json.loads(quota_alert.STATE.read_text()))
        assert "switch" in saved and "credits_off_until" in saved["switch"]
        self.assertEqual(saved["switch"]["credits_off_until"], until.isoformat())
        self.assertFalse(any("provider refusal" in line.lower() for line in log))
        # A spare regaining room does not undo the policy fallback before its hold expires.
        for checked in (self.now + timedelta(minutes=2), until - timedelta(minutes=1)):
            _ = self.alert(checked, spare_left=50)
            self.assertEqual(quota_alert.registry_assignments()["direct"], "claude")
            self.swap.assert_not_called()
        _ = self.alert(until, spare_left=50)
        self.swap.assert_called_once_with("workers", "codex2", why="Codex has room again", cause="credits off")
        self.assertEqual(quota_alert.registry_assignments()["direct"], "codex")
        requests = [entry for entry in moves.read_moves() if entry["kind"] == "requested"]
        self.assertEqual(len(requests), 1)
        request = requests[0]
        assert request["kind"] == "requested"
        self.assertEqual(request.get("cause"), "credits off")

    def test_credits_off_hold_releases_on_owner_allowance_recovery(self) -> None:
        """F001: a fresh positive owner reading releases the policy hold."""
        _ = self.note(1, "off")
        _ = self.alert(spare_left=0)
        _ = self.alert(self.now + timedelta(minutes=2), left=50)
        self.assertEqual(quota_alert.registry_assignments()["direct"], "codex")
        self.swap.assert_not_called()
        with quota_alert.state_file() as state:
            self.assertIsInstance(state["fallback"], quota_alert.NoCodexQuotaFallback)

    def test_credits_off_hold_keeps_fallback_when_owner_readings_are_stale_unknown_or_missing(self) -> None:
        """F001: uncertainty about the owner cannot restore work onto a fresh spare."""
        _ = self.note(1, "off")
        _ = self.alert(spare_left=0)
        later = self.now + timedelta(minutes=12)
        for availability in ("stale", "unknown", "missing"):
            with self.subTest(availability=availability):
                fresh = self.readings(later, left=50)
                assert isinstance(fresh.workers, accounts.CodexWorkersAccount)
                checked: list[quota_alert.CodexHomeReading] = []
                for home in fresh.checked_homes:
                    if home.home == fresh.workers.home:
                        if availability == "missing":
                            continue
                        if availability == "unknown":
                            home.report.problem = "owner reading unavailable"
                        home = quota_alert.CodexHomeReading(home.home, home.report,
                            self.now if availability == "stale" else later)
                    checked.append(home)
                readings = quota_alert.CodexAlertPassReadings(fresh.workers, fresh.selected_account, tuple(checked))
                with mock.patch.object(self, "readings", return_value=readings):
                    _ = self.alert(later)
                self.swap.assert_not_called()
                self.assertFalse(any(entry["kind"] == "requested" for entry in moves.read_moves()))
                self.assertEqual(quota_alert.registry_assignments()["direct"], "claude")
                with quota_alert.state_file() as state:
                    self.assertIsInstance(state["fallback"], quota_alert.AccountCodexQuotaFallback)

    def test_failed_claude_switch_reports_work_staying_on_codex_without_internal_logs(self) -> None:
        """F006: a failed routing update cannot claim that new work moved to Claude."""
        _ = self.note(1, "off")
        with mock.patch.object(quota_alert, "edit_registry", return_value="agents_set_all_assignments: permission denied"):
            _ = self.alert(spare_left=0)
        push = self.zero_pushes()[-1]
        self.assertIn("could not move", push.title)
        self.assertIn("switch to Claude failed", push.body)
        self.assertIn("permission", push.body)
        self.assertIn("Codex work stays on codex1", push.body)
        self.assertNotIn("New coding work uses Claude", push.body)
        self.assertNotIn("agents_set_all_assignments", push.body)
        self.assertNotIn("switch to claude for", push.body)
        self.assertEqual(quota_alert.registry_assignments()["direct"], "codex")

    def test_failed_swap_names_the_account_and_user_cause_without_accounts_diagnostics(self) -> None:
        """F006: account command diagnostics belong in the journal, not on the phone."""
        _ = self.note(1, "off")
        cause = "accounts: cannot use codex2 (~/.codex): usage reading is unavailable"
        self.swap.side_effect = accounts.AccountsError(cause)
        _ = self.alert(spare_left=50)
        push = self.zero_pushes()[-1]
        self.assertIn("could not move to codex2", push.body)
        self.assertIn("its usage reading is unavailable", push.body)
        self.assertNotIn("accounts:", push.body)
        self.assertNotIn("~/.codex", push.body)
        self.assertTrue(any(entry["kind"] == "failed" and entry["reason"] == cause for entry in moves.read_moves()))

    def test_codex_catalog_examples_equal_the_notices_emitted_for_their_scenarios(self) -> None:
        """F005: reviewed example values must be the values the real alert pass emits."""
        from push_texts import catalog, render_entry
        from scripts.whoami import run_out

        self.now = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)
        owner_resets = {1: datetime(2026, 10, 20, 4, 58, tzinfo=timezone.utc),
                        2: datetime(2026, 10, 22, 11, tzinfo=timezone.utc)}
        spare_reset = datetime(2026, 10, 17, 11, tzinfo=timezone.utc)
        base_readings = self.readings
        ids = {"quota.codex_workers_moved",
               "quota.codex_workers_stay", "quota.codex_credits_forecast"}
        with mock.patch.dict(os.environ, {"USER_ZONE": "America/Curacao"}):
            for entry in catalog():
                if entry.id not in ids:
                    continue
                for example in entry.examples:
                    with self.subTest(id=entry.id, example=example.name):
                        quota_alert.STATE.unlink(missing_ok=True)
                        moves.store_file().unlink(missing_ok=True)
                        next_moves.next_moves_file().unlink(missing_ok=True)
                        usage_store.store_file().unlink(missing_ok=True)
                        usage_store.label_file().unlink(missing_ok=True)
                        _ = quota_alert.REGISTRY.write_text(REGISTRY)
                        self.phone.reset_mock()
                        self.relay.reset_mock()
                        self.swap.reset_mock()
                        self.swap.side_effect = None
                        source = 2 if example.name in ("codex_two", "failed_move", "unknown_use") else 1
                        target = 3 - source
                        _ = self.note(1)
                        _ = self.note(2)
                        _ = self.note(3)
                        off = entry.id in ("quota.codex_workers_moved", "quota.codex_workers_stay")
                        _ = self.note(source, "off" if off else "on")
                        accounts.set_role("workers", f"codex{source}")
                        if example.name == "queued":
                            _ = next_moves.add_next_move("workers", 1, f"codex{source}")
                        balance = "6370" if source == 1 else "2114"
                        if example.name == "unlimited":
                            balance = "unlimited"
                        unknown_balance = example.name in ("unknown", "unknown_balance")
                        no_room = example.name in ("no_room", "unknown_use")
                        target_left = 0 if source == 2 or no_room else 97
                        target_credits = "0" if no_room else "6370" if source == 2 else "2114"
                        used = 120 if source == 1 else 40
                        if example.name == "zero_use":
                            used = 0
                        observed_minutes = 20 if example.name == "unknown_use" else 60
                        numeric_balance = 6370 if source == 1 else 2114
                        _ = run_out.READINGS_LOG.write_text("".join(json.dumps({
                            "account": f"codex{source}", "at": at.isoformat(), "remaining": 0, "credits": credits,
                        }) + "\n" for at, credits in (
                            (self.now - timedelta(minutes=observed_minutes), numeric_balance + used),
                            (self.now, numeric_balance))))

                        def scenario_readings(checked: datetime, **_ignored: object) -> quota_alert.CodexAlertPassReadings:
                            readings = base_readings(checked, selected=source, balance=None if unknown_balance else balance,
                                spare_left=0, spare_accounts=((target, target_left, target_credits),))
                            for home in readings.checked_homes:
                                number = 2 if home.home.name == ".codex" else int(home.home.name.removeprefix(".codex"))
                                reset = (owner_resets[source] if number == source else
                                         spare_reset if number == 2 else owner_resets[1]
                                         if number == 1 else owner_resets[2] + timedelta(days=1))
                                left = 0 if number == source or number == 3 else target_left
                                home.report.quotas = [Quota("Weekly", 100 - left, reset)]
                                home.report.five_hour = Quota("5-hour", 0, reset)
                            return readings

                        if example.name == "failed_move":
                            self.swap.side_effect = accounts.AccountsError(
                                "accounts: cannot use codex1 (~/.codex1): usage reading is unavailable")
                        with mock.patch.object(self, "readings", side_effect=scenario_readings):
                            if entry.id == "quota.codex_credits_forecast":
                                _ = self.alert(self.now - timedelta(hours=1))
                            _ = self.alert(self.now)
                            self.phone.side_effect = None
                        if (entry.id == "quota.codex_credits_forecast"
                                and example.name != "queued" and not no_room and target_left > 0):
                            # These old examples now move to room with no notice.
                            self.swap.assert_called()
                            self.assertEqual(self.zero_pushes(), [])
                            continue
                        with quota_alert.state_file() as state:
                            episode = state.get("codex_zero", {})[f"codex{source}"]
                            if entry.id == "quota.codex_credits_forecast":
                                assert "forecast_notice" in episode
                                notice = episode["forecast_notice"]
                            else:
                                notice = episode["notice"]
                        self.assertEqual(notice["id"], entry.id)
                        if example.name == "queued":
                            self.assertNotIn("account move you queued", " ".join(notice["values"].values()))
                        else:
                            self.assertEqual(notice["values"], example.values)
                        expected_values = dict(example.values)
                        self.assertEqual(self.zero_pushes()[-1].values, expected_values)
                        self.assertEqual(self.zero_pushes()[-1], render_entry(entry, expected_values))

    def test_credits_off_fallback_hold_uses_an_earlier_five_hour_refill(self) -> None:
        _ = self.note(1, "off")
        until = self.now + timedelta(hours=1)
        reading = self.readings(self.now, spare_left=0)
        for home in reading.checked_homes:
            home.report.five_hour = Quota("5-hour", 100, until)
        with mock.patch.object(self, "readings", return_value=reading):
            _ = self.alert(spare_left=0)
        with quota_alert.state_file() as state:
            fallback = state["fallback"]
            assert isinstance(fallback, quota_alert.AccountCodexQuotaFallback)
            self.assertEqual(fallback.hold, quota_alert.CreditsOffHold(until))
        self.assertIn(until.strftime("%Y-%m-%d %H:%M"), self.zero_pushes()[-1].body)
        _ = self.alert(until - timedelta(minutes=1))
        self.assertEqual(quota_alert.registry_assignments()["direct"], "claude")
        self.swap.assert_not_called()
        _ = self.alert(until, spare_left=50)
        self.swap.assert_called_once_with("workers", "codex2", why="Codex has room again", cause="credits off")
        self.assertEqual(quota_alert.registry_assignments()["direct"], "codex")

    def test_credits_off_hold_releases_on_explicit_account_change(self) -> None:
        _ = self.note(1, "off")
        _ = self.alert(spare_left=0)
        accounts.set_role("workers", "codex2")
        _ = self.alert(self.now + timedelta(minutes=2), selected=2, left=50)
        self.assertEqual(quota_alert.registry_assignments()["direct"], "codex")
        self.swap.assert_not_called()
        with quota_alert.state_file() as state:
            self.assertIsInstance(state["fallback"], quota_alert.NoCodexQuotaFallback)

    def test_credits_on_moves_to_included_room_before_another_accounts_credits(self) -> None:
        _ = self.alert(spare_accounts=((2, 0, "6370"), (3, 50, "0")))
        self.assertEqual(self.swap.call_args.args, ("workers", "codex3"))
        self.phone.assert_not_called()

    def test_on_credits_notice_handles_a_destination_losing_room_during_the_pass(self) -> None:
        """F009: an account disappearing between placement reads does not abort the alert."""
        pick = accounts.pick
        chosen = False

        def room_changes(tool: str, now: datetime, *, exclude: frozenset[str], keep: accounts.NoKeep,
                         prefer_weekly_room: bool = False) -> accounts.PickedAccount | accounts.NoAccountWithRoom:
            nonlocal chosen
            self.assertEqual(tool, "codex")
            if chosen:
                return accounts.NoAccountWithRoom(accounts.EarliestReset(self.now + timedelta(days=2)), ())
            chosen = True
            first = pick("codex", now, exclude=exclude, keep=keep, prefer_weekly_room=prefer_weekly_room)
            self.assertIsInstance(first, accounts.PickedAccount)
            assert isinstance(first, accounts.PickedAccount)
            self.assertGreater(first.weekly_left, 0)
            return first

        with mock.patch.object(accounts, "pick", side_effect=room_changes) as changing_pick:
            _ = self.alert(spare_left=50)
        self.assertGreaterEqual(changing_pick.call_count, 2)
        push = self.zero_pushes()[-1]
        self.assertEqual(push.id, "quota.paid_credits")
        self.assertIn("work moves to Claude", push.body)
        self.swap.assert_not_called()

    def test_zero_credits_off_prefers_weekly_room_over_a_sooner_reset_on_paid_credits(self) -> None:
        _ = self.note(1, "off")
        _ = self.alert(spare_accounts=((2, 0, "6370"), (3, 50, "0")))
        self.swap.assert_called_once_with("workers", "codex3", why="codex1 weekly usage used; credits off", cause="credits off")
        self.assertIn("codex3", self.zero_pushes()[-1].body)
        requests = [entry for entry in moves.read_moves() if entry["kind"] == "requested"]
        self.assertEqual(len(requests), 1)
        request = requests[0]
        assert request["kind"] == "requested"
        self.assertEqual(request["to"], "codex3")

    def test_zero_credits_off_uses_a_paid_target_only_when_no_account_has_weekly_room(self) -> None:
        _ = self.note(1, "off")
        _ = self.alert(spare_accounts=((2, 0, "6370"), (3, 0, "0")))
        self.swap.assert_called_once_with("workers", "codex2", why="codex1 weekly usage used; credits off", cause="credits off")
        body = self.zero_pushes()[-1].body
        self.assertIn("codex2", body)
        self.assertIn("paid credits", body)

    def test_credits_off_prefers_both_windows_with_room_over_weekly_room_funded_by_credits(self) -> None:
        _ = self.note(1, "off")
        _ = self.alert(spare_accounts=((2, 50, "6370"), (3, 40, "0")), spare_five_hour=((2, 0), (3, 100)))
        self.swap.assert_called_once_with("workers", "codex3", why="codex1 weekly usage used; credits off", cause="credits off")
        body = self.zero_pushes()[-1].body
        self.assertIn("codex3", body)
        self.assertIn("40%", body)

    def test_weekly_room_with_no_five_hour_allowance_is_described_as_paid_funding(self) -> None:
        _ = self.note(1, "off")
        _ = self.alert(spare_accounts=((2, 50, "6370"), (3, 0, "0")), spare_five_hour=((2, 0),))
        body = self.zero_pushes()[-1].body
        self.assertIn("paid credits", body)
        self.assertNotIn("50%", body)
        self.swap.assert_called_once_with("workers", "codex2", why="codex1 weekly usage used; credits off", cause="credits off")
        requests = [entry for entry in moves.read_moves() if entry["kind"] == "requested"]
        self.assertEqual(len(requests), 1)
        request = requests[0]
        assert request["kind"] == "requested"
        self.assertEqual(request.get("cause"), "credits off")

    def test_zero_reads_the_selected_accounts_setting_and_invalid_values_log_once(self) -> None:
        _ = self.note(2, "off")
        _ = self.note(1, "sometimes")
        lines = self.alert()
        self.assertEqual(lines.count("codex1: use_credits must be on or off; reading it as on"), 1)
        self.swap.assert_not_called()
        self.assertIn("6,370", self.zero_pushes()[-1].body)

    def test_unknown_selected_identity_preserves_an_open_zero_episode_without_resending_it(self) -> None:
        _ = self.alert()
        with quota_alert.state_file() as state:
            original = state.get("codex_zero", {})["codex1"].copy()
        reading = self.readings(self.now + timedelta(minutes=2))
        assert isinstance(reading.workers, accounts.CodexWorkersAccount)
        unknown = quota_alert.CodexAlertPassReadings(reading.workers,
            agent_accounts.UnknownCodexAccount(reading.workers.home, "sign-in temporarily unreadable"), reading.checked_homes)
        with mock.patch.object(self, "readings", return_value=unknown):
            _ = self.alert(self.now + timedelta(minutes=2))
        with quota_alert.state_file() as state:
            self.assertEqual(state.get("codex_zero", {})["codex1"], original)
        _ = self.alert(self.now + timedelta(minutes=4))
        self.assertEqual((len(self.zero_pushes()), self.relay.call_count), (1, 0))

    def test_zero_reading_without_a_timezone_on_its_reset_opens_no_episode(self) -> None:
        reading = self.readings(self.now)
        for home in reading.checked_homes:
            home.report.quotas = [Quota("Weekly", 100, (self.now + timedelta(days=2)).replace(tzinfo=None))]
        with mock.patch.object(self, "readings", return_value=reading):
            _ = self.alert()
        with quota_alert.state_file() as state:
            self.assertEqual(state.get("codex_zero", {}), {})
        self.swap.assert_not_called()
        self.phone.assert_not_called()

    def test_zero_edited_from_credits_on_to_off_moves_once_on_the_next_pass_without_forecasts(self) -> None:
        _ = self.alert()
        opening = self.zero_pushes()[-1]
        _ = self.note(1, "off")
        _ = self.alert(self.now + timedelta(minutes=2), spare_left=50)
        self.swap.assert_called_once_with("workers", "codex2", why="codex1 weekly usage used; credits off", cause="credits off")
        with quota_alert.state_file() as state:
            forecast_at = state.get("codex_zero", {})["codex1"]["forecast_at"]
        requests = [entry for entry in moves.read_moves() if entry["kind"] == "requested"]
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].get("cause"), "credits off")
        for minutes in (4, 60, 120):
            _ = self.alert(self.now + timedelta(minutes=minutes))
        self.swap.assert_called_once()
        self.assertEqual(len([entry for entry in moves.read_moves() if entry["kind"] == "requested"]), 1)
        pushes = [cast(RenderedPush, call.args[0]) for call in self.phone.call_args_list]
        self.assertEqual(sum(push.id == opening.id for push in pushes), 1)
        self.assertFalse(any("forecast" in push.id for push in pushes))
        with quota_alert.state_file() as state:
            episode = state.get("codex_zero", {})["codex1"]
            self.assertEqual(episode["credits"], "off")
            self.assertEqual(episode["forecast_at"], forecast_at)
            self.assertNotIn("forecast_phone", episode)

    def test_zero_edited_from_on_to_off_without_room_switches_to_claude_on_the_next_pass(self) -> None:
        _ = self.alert(spare_left=0)
        _ = self.note(1, "off")
        with mock.patch.object(quota_alert, "switch_to_claude", wraps=quota_alert.switch_to_claude) as fallback:
            _ = self.alert(self.now + timedelta(minutes=2), spare_left=0)
            fallback.assert_called_once()
            self.assertEqual(quota_alert.registry_assignments()["direct"], "claude")
            _ = self.alert(self.now + timedelta(hours=1), spare_left=0)
            fallback.assert_called_once()
        self.swap.assert_not_called()
        self.assertEqual(len(self.zero_pushes()), 1)
        with quota_alert.state_file() as state:
            self.assertNotIn("forecast_phone", state.get("codex_zero", {})["codex1"])

    def test_credit_edit_after_workers_move_does_not_start_a_forecast_or_another_move(self) -> None:
        _ = self.note(1, "off")
        _ = self.alert(spare_left=50)
        accounts.set_role("workers", "codex2")
        _ = self.note(1, "on")
        for minutes in (2, 60, 120):
            _ = self.alert(self.now + timedelta(minutes=minutes), selected=2, left=50)
        self.swap.assert_called_once_with("workers", "codex2", why="codex1 weekly usage used; credits off", cause="credits off")
        self.assertEqual(len(self.zero_pushes()), 1)
        self.relay.assert_not_called()
        with quota_alert.state_file() as state:
            self.assertNotIn("codex1", state.get("codex_zero", {}))

    def test_zero_invalid_credit_setting_mid_episode_still_reads_as_on(self) -> None:
        _ = self.alert()
        _ = self.note(1, "invalid")
        log = self.alert(self.now + timedelta(minutes=2))
        self.assertEqual(log.count("codex1: use_credits must be on or off; reading it as on"), 1)
        self.swap.assert_not_called()
        self.history([(0, 6370), (60, 6370)])
        _ = self.alert(self.now + timedelta(hours=1))
        self.assertEqual(len(self.zero_pushes()), 2)
        self.assertIn("forecast", self.zero_pushes()[-1].id)
        with quota_alert.state_file() as state:
            self.assertEqual(state.get("codex_zero", {})["codex1"]["credits"], "on")

    def test_zero_does_not_open_from_a_stale_selected_snapshot(self) -> None:
        readings = self.readings(self.now)
        note = self.note(1)
        _ = quota_alert.alert([note], self.now + timedelta(minutes=10, seconds=1),
            selection=ClaudeAccountsInUse(agent_accounts.UnreadableIdentity(Path.home() / ".claude", "fixture identity unavailable"), False, (), frozenset()),
            codex_readings=readings)
        self.phone.assert_not_called()
        self.relay.assert_not_called()
        self.swap.assert_not_called()
        with quota_alert.state_file() as state:
            self.assertEqual(state.get("codex_zero", {}), {})

    def test_zero_opening_names_unlimited_or_unknown_balance(self) -> None:
        for balance in ("unlimited", None):
            with self.subTest(balance=balance):
                quota_alert.STATE.unlink(missing_ok=True)
                self.phone.reset_mock()
                _ = self.alert(balance=balance)
                body = self.zero_pushes()[-1].body
                if balance == "unlimited":
                    self.assertTrue("unlimited" in body or "no limit" in body)
                else:
                    self.assertIn("unknown paid credits left", body)

    def test_zero_enters_from_a_low_episode_without_restored_acknowledgement_or_repeats(self) -> None:
        _ = self.alert(left=1)
        with quota_alert.state_file() as state:
            self.assertIn("codex1", state["episodes"])
        self.phone.reset_mock()
        self.relay.reset_mock()
        _ = self.alert(self.now + timedelta(minutes=2))
        _ = self.alert(self.now + timedelta(minutes=32))
        self.assertEqual((len(self.zero_pushes()), self.relay.call_count), (1, 0))
        self.assertFalse(any("Quota restored:" in text or "/quota_ack" in text for text in self.relayed_texts()))
        with quota_alert.state_file() as state:
            self.assertIn("codex1", state["episodes"])

    def test_zero_closes_silently_after_recovery_swap_away_or_weekly_reset(self) -> None:
        for close in ("recovered", "swapped", "reset"):
            with self.subTest(close=close):
                quota_alert.STATE.unlink(missing_ok=True)
                accounts.set_role("workers", "codex1")
                _ = self.alert()
                self.phone.reset_mock()
                self.relay.reset_mock()
                if close == "recovered":
                    _ = self.alert(self.now + timedelta(minutes=2), left=80)
                elif close == "swapped":
                    accounts.set_role("workers", "codex2")
                    _ = self.alert(self.now + timedelta(minutes=2), selected=2, left=80)
                else:
                    _ = self.alert(self.now + timedelta(days=2))
                self.phone.assert_not_called()
                self.relay.assert_not_called()
                with quota_alert.state_file() as state:
                    self.assertNotIn("codex1", state.get("codex_zero", {}))

    def test_zero_forecasts_at_an_hour_and_only_the_first_forecast_goes_to_the_phone(self) -> None:
        _ = self.alert()
        self.phone.reset_mock()
        self.relay.reset_mock()
        self.history([(0, 6370), (30, 6340), (60, 6310)])
        _ = self.alert(self.now + timedelta(minutes=58), balance="6310")
        self.phone.assert_not_called()
        self.relay.assert_not_called()
        log = self.alert(self.now + timedelta(hours=1), balance="6310")
        self.assertEqual((len(self.zero_pushes()), self.relay.call_count), (1, 0))
        push = self.zero_pushes()[-1]
        self.assertTrue(any(push.body in line for line in log), log)
        self.assertIn("60", push.body)
        self.assertIn("6,310", push.body)
        self.assertIn("Claude", push.body)
        self.assertEqual(self.relayed_texts(), [])
        self.history([(0, 6370), (60, 6310), (90, 6280), (120, 6250)])
        _ = self.alert(self.now + timedelta(hours=2), balance="6250")
        _ = self.alert(self.now + timedelta(hours=2, minutes=2), balance="6250")
        self.assertEqual((len(self.zero_pushes()), self.relay.call_count), (1, 0))

    def test_zero_first_forecast_phone_failure_retries_without_an_extra_forecast_relay(self) -> None:
        _ = self.alert()
        self.phone.reset_mock()
        self.relay.reset_mock()
        self.phone.side_effect = [quota_alert.PushFailed("offline"), quota_alert.PushSent()]
        self.history([(0, 6370), (60, 6370)])
        _ = self.alert(self.now + timedelta(hours=1))
        _ = self.alert(self.now + timedelta(hours=1, minutes=2))
        _ = self.alert(self.now + timedelta(hours=1, minutes=4))
        self.assertEqual((len(self.zero_pushes()), self.relay.call_count), (2, 0))
        self.assertIn("no credits", self.zero_pushes()[-1].body)

    def test_zero_forecast_names_unknown_usage_when_observations_do_not_cover_the_hour(self) -> None:
        _ = self.alert()
        self.phone.reset_mock()
        self.history([(40, 6370), (60, 6360)])
        _ = self.alert(self.now + timedelta(hours=1), balance="6360")
        self.assertIn("unknown", self.zero_pushes()[-1].body)

    def test_zero_forecast_uses_the_observed_half_hour_for_its_run_out_rate(self) -> None:
        _ = self.alert()
        self.phone.reset_mock()
        self.history([(30, 120), (60, 90)])
        _ = self.alert(self.now + timedelta(hours=1), balance="90")
        text = self.zero_pushes()[-1].body
        self.assertIn("30", text)
        self.assertIn((self.now + timedelta(hours=2, minutes=30)).strftime("%Y-%m-%d %H:%M"), text)

    def test_zero_forecast_names_unlimited_and_unknown_current_credit_balances(self) -> None:
        for balance, expected in (("unlimited", "unlimited"), (None, "no credit balance")):
            with self.subTest(balance=balance):
                quota_alert.STATE.unlink(missing_ok=True)
                usage_store.store_file().unlink(missing_ok=True)
                _ = self.alert()
                self.phone.reset_mock()
                _ = self.alert(self.now + timedelta(hours=1), balance=balance)
                self.assertIn(expected, self.zero_pushes()[-1].body)


if __name__ == "__main__":
    _ = unittest.main()
