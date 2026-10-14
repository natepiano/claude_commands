"""Repeat-until-acknowledged delivery and message content of the low-quota alert."""

from __future__ import annotations

import io
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Callable
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast, override
from unittest import mock
from zoneinfo import ZoneInfo

import quota_alert
import review
from push_texts import RenderedPush, render
from scripts.whoami import agent_accounts
from agent_accounts import ClaudeAccountsInUse, CodexHomeSource, Quota, Report
import agent_notes
import note_file
import account
from agent_notes import local_reset
from note_file import read_note
from scripts.accounts import accounts, moves, refusals
from scripts.production import showrunners

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "message"))
import send
# send puts scripts/notify on the path.
from user_action import ActionRequired, UserAction, parse_user_action

Notes = list[quota_alert.AgentNote]

REGISTRY = """\
[assignments]
delegate=codex
cli=claude
fix=codex   # a trailing comment
ask_a_friend=caller

[delegate.codex]
impl=gpt-6-sol:high
[delegate.claude]
impl=opus:high
[cli.codex]
commit_prep=gpt-6-sol:high
[cli.claude]
commit_prep=sonnet:high
[fix.codex]
style_fix=gpt-6-sol:xhigh
[fix.claude]
style_fix=opus:max
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
REFUSAL_ACTION = "At chatgpt.com/codex/settings/usage, add credits for account codex 1."


def all_live(names: list[str]) -> list[str]:
    """Stands in for the live session lookup: every configured name is a live session."""
    del names
    return []


def natedev_gone(names: list[str]) -> list[str]:
    return [name for name in names if name == "natedev"]


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
    root = Path(unittest.enterModuleContext(tempfile.TemporaryDirectory()))
    _ = unittest.enterModuleContext(mock.patch.object(showrunners, "NOTIFIER_STATE_DIR", root))


class QuotaAlertTests(unittest.TestCase):
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
        self.root = Path(directory.name)
        environment_context = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            "HOME": str(self.root), "AGENT_NOTES_DIR": str(self.root),
        })))
        del environment_context
        (self.root / ".claude").mkdir()
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
                            ("tell_user", self.tell_user), ("REGISTRY", registry), ("not_live", all_live)):
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
                            + f"state: {state}\nweekly_remaining_usage: {left}\n{extras}---\n")
        note = read_note(path)
        assert note is not None
        return note

    def alert_readings(self, notes: Notes, now: datetime) -> quota_alert.CodexAlertPassReadings:
        selected = next((note for note in notes if note.tool == "codex" and note.get("state") == "active"), None)
        home = self.root / ".codex"
        seats = accounts.CodexSeatsAccount(label=selected.path.stem if selected is not None else "~/.codex", home=home)
        if selected is None:
            return quota_alert.CodexAlertPassReadings(seats,
                agent_accounts.UnknownCodexAccount(home, "fixture has no selected Codex account"), ())
        account = agent_accounts.KnownCodexAccount(home, selected.get("login") or "someone@example.com",
                                               selected.path.stem)
        left = selected.get("weekly_remaining_usage") or "null"
        reset = datetime.fromisoformat(selected.get("resets") or self.resets).astimezone(timezone.utc)
        live = Report("Codex", email=account.login, source=CodexHomeSource(home),
            quotas=[Quota("Weekly", 100 - float(left), reset)] if left != "null" else [],
            five_hour=Quota("5-hour", 0, reset), credit_balance=selected.get("credit_balance"))
        return quota_alert.CodexAlertPassReadings(seats, account,
            (quota_alert.CodexHomeReading(home, live, now),))

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
        return quota_alert.blocked(notes, readings.selected_account, checked,
                                    selection=selection, codex_readings=readings)

    def refresh(self, notes: Notes, here: str | None, now: datetime | None = None) -> list[str]:
        checked = now or self.now
        return quota_alert.refresh(notes, here, checked, codex_readings=self.alert_readings(notes, checked))

    def message(self, note: quota_alert.AgentNote, notes: Notes, config: quota_alert.Config) -> str:
        return quota_alert.message(note, notes, config, codex_readings=self.alert_readings(notes, self.now))

    def set_claude_activity(self, label: str) -> None:
        from scripts.message import sessions
        identity = accounts.account_identity(accounts.account_dir(label))
        assert isinstance(identity, agent_accounts.KnownClaudeAccount)
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
        for label, login in (("claude 1", "spare@example.invalid"), ("claude 3", "unit@example.invalid")):
            self.assertEqual(accounts.setup(label), [])
            _ = (self.root / f"{label}.md").write_text(f"---\nlogin: {login}\n---\n")
            _ = (accounts.account_dir(label) / ".claude.json").write_text(
                json.dumps({"oauthAccount": {"emailAddress": login}}), encoding="utf-8",
            )
        self.set_claude_activity("claude 3")

    def test_low_account_advice_uses_soonest_reset_and_moves_only_when_refused(self) -> None:
        self.configure_claude_roles()
        self.stored_room("unit@example.invalid", 1, 7)
        self.stored_room("spare@example.invalid", 1, 2)
        self.stored_room("showrunner@example.invalid", 80, 5)
        note = self.note("claude 3.md", "active", "1", login="unit@example.invalid")
        with (mock.patch.object(accounts, "move") as move,
              mock.patch.object(accounts, "move_group") as move_group):
            text = self.message(note, [note], quota_alert.load_config())
        move.assert_not_called()
        move_group.assert_not_called()
        self.assertIn("units: move to claude 1", text)
        self.assertIn("showrunners: move to default", text)
        self.assertIn("ungrouped sessions: move to claude 1", text)
        self.assertNotIn("swap unit", text)

    def test_low_account_advice_distinguishes_group_rankings_and_ungrouped_picker(self) -> None:
        self.configure_claude_roles()
        self.stored_room("unit@example.invalid", 1, 7)
        self.stored_room("spare@example.invalid", 0, 2)
        self.stored_room("showrunner@example.invalid", 80, 5)
        note = self.note("claude 3.md", "active", "1", login="unit@example.invalid")
        self.assertFalse(accounts.groups_file().exists())
        with mock.patch.object(accounts, "move_sessions") as move:
            text = quota_alert.unit_swap_line(note, [note], accounts.claude_account_selection())
        self.assertIn("units: move to claude 1", text)
        self.assertIn("showrunners: move to default", text)
        self.assertIn("ungrouped sessions: move to claude 1", text)
        self.assertNotIn("move to claude 3", text)
        self.assertFalse(accounts.groups_file().exists())
        move.assert_not_called()

    def test_low_account_advice_names_no_room_and_earliest_reset(self) -> None:
        self.configure_claude_roles()
        self.stored_room("unit@example.invalid", 1, 7)
        self.stored_room("spare@example.invalid", 0, 2)
        self.stored_room("showrunner@example.invalid", 0, 5)
        now = datetime.now(timezone.utc)
        for login, days in (("spare@example.invalid", 2), ("showrunner@example.invalid", 5)):
            refusals.write_refusal(refusals.AccountRefusal("claude", login,
                refusals.KnownWindow("weekly"), refusals.KnownReset(now + timedelta(days=days)), now))
        note = self.note("claude 3.md", "active", "1", login="unit@example.invalid")
        text = self.message(note, [note], quota_alert.load_config())
        self.assertIn("no other account has room until", text)
        self.assertNotIn("swap unit", text)

    def test_sessions_on_multiple_accounts_each_get_their_accounts_alerts(self) -> None:
        from scripts.message import sessions
        self.configure_claude_roles()
        owners = [sessions.SessionRecord(pid=pid, sessionId=str(pid), name=str(pid),
                  messagingSocketPath="/fixture.sock", updatedAt=1, tmux="", formerNames=[])
                  for pid in (10001, 10003)]
        identities = {10001: account.Account("claude", "spare@example.invalid", "claude 1"),
                      10003: account.Account("claude", "unit@example.invalid", "claude 3")}
        notes: Notes = [self.note("claude 1.md", "active", "0", login="spare@example.invalid"),
                        self.note("claude 2.md", "active", "0", login="showrunner@example.invalid"),
                        self.note("claude 3.md", "active", "0", login="unit@example.invalid")]
        def observed_account(pid: int) -> account.Account | None:
            return identities.get(pid)
        with (mock.patch.object(sessions, "live_sessions", return_value=owners),
              mock.patch.object(account, "account_of", side_effect=observed_account)):
            _ = self.alert(notes, self.now)
        for label in ("claude 1", "claude 2", "claude 3"):
            self.assertTrue(any(text.startswith(f"Quota alert: {label}") for _, text in self.sent))
        with quota_alert.state_file() as state:
            self.assertEqual(set(state["episodes"]), {"claude 1", "claude 2", "claude 3"})

    def test_live_unfit_and_external_accounts_receive_alerts_from_collected_readings(self) -> None:
        from scripts.message import sessions
        import five_hour
        self.configure_claude_roles()
        unfit = accounts.account_dir("claude 3")
        (unfit / "projects").unlink()
        external = self.root / "retained account"
        external.mkdir()
        owners = [sessions.SessionRecord(pid=pid, sessionId=str(pid), name=str(pid),
            messagingSocketPath="/fixture.sock", updatedAt=1, tmux="", formerNames=[]) for pid in (10001, 10003)]
        directories = {10001: external, 10003: unfit}
        logins = {self.root / ".claude": "showrunner@example.invalid", unfit: "unit@example.invalid",
                  external: "external@example.invalid", accounts.account_dir("claude 1"): "spare@example.invalid"}
        _ = self.note("claude 4.md", "inactive", "90", login="external@example.invalid")
        _ = self.note("claude 3.md", "inactive", "90", login="unit@example.invalid")
        _ = self.note("claude 2.md", "active", "90", login="showrunner@example.invalid")
        def process_directory(pid: int) -> Path | None:
            return directories.get(pid)
        def process_account(pid: int) -> account.Account | None:
            directory = directories.get(pid)
            return account.Account("claude", logins[directory], "fixture") if directory is not None else None
        def live(directory: Path) -> Report:
            return Report("Claude", email=logins[directory], quotas=[Quota("Weekly", 100,
                datetime.now(timezone.utc) + timedelta(days=2))], source=agent_accounts.ClaudeDirectorySource(directory))
        with (mock.patch.object(sessions, "live_sessions", return_value=owners),
              mock.patch.object(account, "account_of", side_effect=process_account),
              mock.patch.object(account, "process_config_dir", side_effect=process_directory)):
            selection = accounts.claude_account_selection()
        with (mock.patch.object(agent_accounts, "claude_live", side_effect=live),
              mock.patch.object(agent_accounts, "codex_homes", return_value=[]),
              mock.patch.object(five_hour, "watch", return_value=[])):
            _ = agent_notes.update_alert_pass(selection, self.root / ".codex")
        _ = self.alert(list(note_file.read_notes()), datetime.now(timezone.utc), selection=selection)
        for label in ("claude 3", "claude 4"):
            self.assertTrue(any(text.startswith("Quota alert: " + label) for _, text in self.sent), self.sent)

    def test_acknowledging_one_claude_account_keeps_the_other_account_alerting(self) -> None:
        self.configure_claude_roles()
        notes: Notes = [
            self.note("claude 2.md", "active", "0", login="showrunner@example.invalid"),
            self.note("claude 3.md", "active", "0", login="unit@example.invalid"),
        ]
        _ = self.alert(notes, self.now)
        _ = quota_alert.acknowledge("claude 3", now=self.now)
        self.sent.clear()
        _ = self.alert(notes, self.now + timedelta(minutes=30))
        self.assertEqual(len(self.sent), 2)
        self.assertTrue(all(text.startswith("Quota alert: claude 2") for _, text in self.sent))
        with quota_alert.state_file() as state:
            self.assertIn("acknowledged", state["episodes"]["claude 3"])
            self.assertNotIn("acknowledged", state["episodes"]["claude 2"])

    def test_acknowledgement_survives_sessions_moving_until_the_accounts_quota_recovers(self) -> None:
        """A role change does not close or rearm the same account's episode."""
        self.configure_claude_roles()
        unit = self.note("claude 3.md", "active", "0", login="unit@example.invalid")
        spare = self.note("claude 1.md", "inactive", "90", login="spare@example.invalid")
        main = self.note("claude 2.md", "active", "80", login="showrunner@example.invalid")
        _ = self.alert([unit, spare, main], self.now)
        _ = quota_alert.acknowledge("claude 3", now=self.now)
        self.sent.clear()
        self.pushed.clear()

        self.set_claude_activity("claude 1")
        spare = self.note("claude 1.md", "active", "90", login="spare@example.invalid")
        for left in ("0", "null"):
            unit = self.note("claude 3.md", "inactive", left, login="unit@example.invalid")
            _ = self.alert([unit, spare, main], self.now + timedelta(minutes=2))
            with quota_alert.state_file() as state:
                self.assertEqual(state["episodes"]["claude 3"].get("acknowledged"), self.now.isoformat(timespec="seconds"))
        _ = self.alert([spare, main], self.now + timedelta(minutes=3))
        self.set_claude_activity("claude 3")
        unit = self.note("claude 3.md", "active", "0", login="unit@example.invalid")
        spare = self.note("claude 1.md", "inactive", "90", login="spare@example.invalid")
        _ = self.alert([unit, spare, main], self.now + timedelta(hours=1))
        self.assertEqual((self.sent, self.pushed), ([], []))
        with quota_alert.state_file() as state:
            self.assertIn("acknowledged", state["episodes"]["claude 3"])

        self.set_claude_activity("claude 1")
        unit = self.note("claude 3.md", "inactive", "80", login="unit@example.invalid")
        _ = self.alert([unit, spare, main], self.now + timedelta(hours=2))
        self.assertTrue(all(text.startswith("Quota restored: work on claude 3 can resume;") for _, text in self.sent))
        self.assertEqual(len(self.sent), 2)
        with quota_alert.state_file() as state:
            self.assertNotIn("claude 3", state["episodes"])
        self.sent.clear()
        self.set_claude_activity("claude 3")
        unit = self.note("claude 3.md", "active", "0", login="unit@example.invalid")
        _ = self.alert([unit, spare, main], self.now + timedelta(hours=3))
        self.assertEqual((len(self.sent), len(self.pushed)), (2, 1))

    def test_a_weekly_reset_closes_the_spare_accounts_acknowledged_episode(self) -> None:
        """An account's own reset ends its episode even while it is spare."""
        self.configure_claude_roles()
        unit = self.note("claude 3.md", "active", "0", login="unit@example.invalid")
        _ = self.alert([unit], self.now)
        _ = quota_alert.acknowledge("claude 3", now=self.now)
        self.set_claude_activity("claude 1")
        unit = self.note("claude 3.md", "inactive", "null",
                         local_reset(self.now - timedelta(minutes=1)), login="unit@example.invalid")
        self.sent.clear()
        _ = self.alert([unit], self.now + timedelta(minutes=2))
        with quota_alert.state_file() as state:
            self.assertNotIn("claude 3", state["episodes"])
        self.assertEqual(len(self.sent), 2)
        self.assertTrue(all(text.startswith("Quota restored: work on claude 3 can resume;") for _, text in self.sent))

    def test_one_alert_run_keeps_its_activity_snapshot_when_sessions_move_during_rendering(self) -> None:
        """Every message and spare comparison uses one role value."""
        self.configure_claude_roles()
        notes: Notes = [
            self.note("claude 2.md", "active", "0", login="showrunner@example.invalid"),
            self.note("claude 3.md", "active", "0", login="unit@example.invalid"),
            self.note("claude 1.md", "active", "0", login="spare@example.invalid"),
        ]
        resolve_roles = accounts.claude_account_selection

        def swap_after_resolving() -> ClaudeAccountsInUse:
            roles = resolve_roles()
            self.set_claude_activity("claude 1")
            return roles

        with mock.patch.object(accounts, "claude_account_selection", side_effect=swap_after_resolving) as resolve:
            _ = self.alert(notes, self.now)
        resolve.assert_called_once_with()
        first_lines = [text.splitlines()[0] for _, text in self.sent]
        self.assertEqual(len(first_lines), 4)
        self.assertEqual(sum(line.startswith("Quota alert: claude 2") for line in first_lines), 2)
        self.assertEqual(sum(line.startswith("Quota alert: claude 3") for line in first_lines), 2)
        with quota_alert.state_file() as state:
            self.assertEqual(set(state["episodes"]), {"claude 2", "claude 3"})

    def test_alert_uses_the_updaters_role_value_without_resolving_again(self) -> None:
        """Update and alert can share the same run's role value."""
        self.configure_claude_roles()
        unit = self.note("claude 3.md", "active", "0", login="unit@example.invalid")
        roles = accounts.claude_account_selection()
        self.set_claude_activity("claude 1")
        with mock.patch.object(accounts, "claude_account_selection", side_effect=AssertionError("roles resolved twice")):
            _ = self.alert([unit], self.now, selection=roles)
        self.assertTrue(self.sent[0][1].startswith("Quota alert: claude 3"))

    def test_refusal_uses_supplied_selection_without_resolving_again(self) -> None:
        """Refusal notices accept the same run's frozen role value."""
        self.configure_claude_roles()
        note = self.note("codex 1.md", "active", "0")
        roles = accounts.claude_account_selection()
        with mock.patch.object(accounts, "claude_account_selection", side_effect=AssertionError("roles resolved twice")):
            lines = self.block([note], self.now, selection=roles)
        self.assertIn("switched to claude for codex 1: delegate, fix", lines)
        self.assertEqual((len(self.sent), len(self.pushed)), (2, 0))

    def test_claude_recovery_names_only_the_recovered_account(self) -> None:
        self.configure_claude_roles()
        notes: Notes = [
            self.note("claude 2.md", "active", "0", login="showrunner@example.invalid"),
            self.note("claude 3.md", "active", "0", login="unit@example.invalid"),
        ]
        _ = self.alert(notes, self.now)
        self.sent.clear()
        notes[1] = self.note("claude 3.md", "active", "80", login="unit@example.invalid")
        _ = self.alert(notes, self.now + timedelta(minutes=2))
        self.assertEqual(len(self.sent), 2)
        for _, text in self.sent:
            self.assertTrue(text.startswith("Quota restored: work on claude 3 can resume;"))
            self.assertNotIn("claude 2", text)
        with quota_alert.state_file() as state:
            self.assertEqual(set(state["episodes"]), {"claude 2"})

    def test_recovery_does_not_announce_a_different_healthy_claude_account(self) -> None:
        self.configure_claude_roles()
        unit = self.note("claude 3.md", "active", "0", login="unit@example.invalid")
        main = self.note("claude 2.md", "active", "80", login="showrunner@example.invalid")
        _ = self.alert([unit, main], self.now)
        self.sent.clear()
        unit = self.note("claude 3.md", "inactive", "0", login="unit@example.invalid")
        _ = self.alert([unit, main], self.now + timedelta(minutes=2))
        self.assertEqual(self.sent, [])

    def test_default_only_claude_alert_names_sessions_and_future_move_advice(self) -> None:
        note = self.note("claude 2.md", "active", "0")
        text = self.message(note, [note], quota_alert.load_config())
        self.assertEqual(text.splitlines()[0],
                         f"Quota alert: claude 2, the active Claude account, has 0% of its weekly "
                         + f"usage left (threshold 1%); it resets {self.resets}.")
        self.assertNotIn("swap:", text)
        self.assertIn("Sessions on claude 2 start no new Claude work", text)
        self.assertIn("claude 2 is low; no other account has room", text)

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

    def test_repeats_to_every_recipient_until_acknowledged(self) -> None:
        notes: Notes = [self.note("codex 1.md", "active", "1")]
        self.assertEqual(len(self.alert(notes, self.now)), 3)  # one status note and two session sends
        self.assertEqual(self.recipients(), ["boss of bosses", "natedev"])
        _ = self.alert(notes, self.now + timedelta(minutes=10))
        self.assertEqual(self.recipients(), [])
        _ = self.alert(notes, self.now + timedelta(minutes=30))
        self.assertEqual(self.recipients(), ["boss of bosses", "natedev"])
        _ = quota_alert.acknowledge(here="natedev")
        _ = self.recipients()
        _ = self.alert(notes, self.now + timedelta(hours=5))
        self.assertEqual(self.recipients(), [])

    def test_new_episode_pushes_once_and_records_success(self) -> None:
        notes: Notes = [self.note("claude 2.md", "active", "1"),
                        self.note("claude 1.md", "inactive", "80", login="spare@example.invalid")]
        first = self.alert(notes, self.now)
        self.assertIn("quota push claude 2 -> user: sent", first)
        self.assertEqual(len(self.pushed), 1)
        with quota_alert.state_file() as state:
            self.assertEqual(state["episodes"]["claude 2"].get("push"), self.now.isoformat(timespec="seconds"))

        second = self.alert(notes, self.now + timedelta(minutes=30))
        self.assertNotIn("quota push claude 2 -> user: sent", second)
        self.assertEqual(len(self.pushed), 1)

    def test_failed_episode_push_retries_even_after_acknowledgement_then_stops(self) -> None:
        notes: Notes = [self.note("claude 2.md", "active", "0"),
                        self.note("claude 1.md", "inactive", "80", login="spare@example.invalid")]
        self.push_errors.append(quota_alert.PushFailed("channel unavailable"))
        first = self.alert(notes, self.now)
        self.assertIn("quota push claude 2 -> user: not delivered: channel unavailable", first)
        with quota_alert.state_file() as state:
            self.assertEqual(state["episodes"]["claude 2"].get("push"), "due")

        _ = quota_alert.acknowledge("claude 2", now=self.now + timedelta(minutes=1))
        second = self.alert(notes, self.now + timedelta(minutes=2))
        self.assertIn("quota push claude 2 -> user: sent", second)
        self.assertEqual(len(self.pushed), 2)
        _ = self.alert(notes, self.now + timedelta(minutes=4))
        self.assertEqual(len(self.pushed), 2)

    def test_episode_without_push_marker_never_sends_old_news(self) -> None:
        stamp = self.now.isoformat(timespec="seconds")
        _ = quota_alert.STATE.write_text(json.dumps({
            "episodes": {"claude 2": {"since": stamp, "last": {}}},
        }))
        _ = self.alert([self.note("claude 2.md", "active", "0")], self.now)
        self.assertEqual(self.pushed, [])
        with quota_alert.state_file() as state:
            self.assertNotIn("push", state["episodes"]["claude 2"])

    def test_claude_push_names_refill_and_offers_no_redeem_at_decision_priority(self) -> None:
        # Ahead of the real clock, which decides whether a reset is still to come.
        refill = (datetime.now().astimezone() + timedelta(days=2)).replace(second=0, microsecond=0)
        resets = refill.strftime("%Y-%m-%d %H:%M")
        note = self.note("claude 2.md", "active", "0", resets, limit_reset_count="3")
        spare = self.note("claude 1.md", "inactive", "80", login="spare@example.invalid")
        _ = self.alert([note, spare], self.now)
        summary, text, ask = self.pushed[0]
        self.assertEqual(summary, "Coding may stop: Claude account out of usage")
        self.assertIn(f"{refill.astimezone(ZoneInfo('America/Los_Angeles')):%Y-%m-%d %H:%M %Z}", text)
        self.assertIn("claude 1", ask.action)
        self.assertNotIn("redeem", text)
        self.assertEqual(ask, quota_alert.Ask("decision", cast(ActionRequired, render("quota.claude", account="claude 2", remaining="0%", reset="2026-10-17 09:00 EDT", target="claude 1", target_remaining="80%").action).text))

    def test_codex_weekly_zero_status_pushes_once_and_suppresses_the_low_episode(self) -> None:
        note = self.note("codex 1.md", "active", "0", credit_balance="60498")
        _ = self.alert([note], self.now)
        self.assertEqual(self.pushed, [])
        self.assertEqual(len(self.noted), 1)
        self.assertIn("60,498", self.noted[0].body)
        with quota_alert.state_file() as state:
            self.assertNotIn("codex 1", state["episodes"])
            self.assertEqual(state.get("codex_zero", {})["codex 1"]["phone"], self.now.isoformat(timespec="seconds"))
        _ = self.alert([note], self.now + timedelta(minutes=30))
        self.assertEqual(len(self.noted), 1)
        self.assertEqual(self.pushed, [])

    def test_outgoing_account_refusal_is_logged_without_a_phone_push(self) -> None:
        """F005: a refusal from an account no longer used needs no user action."""
        outgoing = self.note("codex 1.md", "inactive", "0", login="old@example.invalid")
        current = self.note("codex 2.md", "active", "90", login="current@example.invalid")
        readings = self.alert_readings([outgoing, current], self.now)
        refusing = agent_accounts.KnownCodexAccount(self.root / "outgoing", "old@example.invalid", "codex 1")
        lines = quota_alert.blocked([outgoing, current], refusing, self.now, codex_readings=readings)
        self.assertEqual((self.pushed, self.sent), ([], []))
        self.assertIn("quota status codex 1: Codex refused work on codex 1, the outgoing account. Nothing switched to Claude.", lines)
        with quota_alert.state_file() as state:
            self.assertIsInstance(state["fallback"], quota_alert.NoCodexQuotaFallback)
            notice = state.get("refusal_notices", {}).get(str(refusing.home))
            assert notice is not None and notice["kind"] == "known"
            self.assertEqual(notice.get("push"), self.now.isoformat(timespec="seconds"))

    def test_internal_switch_failure_details_do_not_leak_into_the_phone_push(self) -> None:
        note = self.note("codex 1.md", "active", "0")
        push = quota_alert.refusal_push(note, quota_alert.NoCodexQuotaFallback(),
                                        "Could not read /private/account/registry " + "unavailable " * 100, False)
        self.assertIsInstance(push, quota_alert.UserPush)
        self.assertNotIn("/private", push.text)
        self.assertNotIn("registry", push.text)
        self.assertIn("the work is paused", push.text)

    def test_actionable_quota_pushes_fit_phone_body_and_name_where_to_act(self) -> None:
        notes: Notes = [self.note("claude 2.md", "active", "0", limit_reset_count="0"),
                        self.note("codex 1.md", "active", "0", credit_balance="60498")]
        _ = self.alert(notes, self.now)
        _ = self.block(notes, self.now)
        for summary, text, ask in self.pushed:
            self.assertLess(len(summary), 45)
            self.assertLess(len("Action: " + ask.action + "\n" + text), 500)
            self.assertTrue("natedev" in ask.action or "chatgpt.com/codex/settings/usage" in ask.action)
            self.assertLessEqual(text.count("recommended"), 1)

    def test_quota_delivery_roles_separate_log_notes_from_actionable_pushes(self) -> None:
        claude = self.note("claude 2.md", "active", "0", limit_reset_count="3")
        codex = self.note("codex 1.md", "active", "0", credit_balance="10")
        selection = accounts.claude_account_selection()
        status = quota_alert.run_out_push(codex, selection, [codex, claude])
        self.assertIsInstance(status, quota_alert.QuotaStatusNote)
        self.assertNotIsInstance(status, quota_alert.UserPush)
        assert isinstance(status, quota_alert.QuotaStatusNote)
        self.assertIn("Codex keeps working on its credits", status.text)
        spare = self.note("claude 1.md", "inactive", "80", login="spare@example.invalid")
        push = quota_alert.run_out_push(claude, selection, [codex, claude, spare])
        self.assertIsInstance(push, quota_alert.UserPush)
        assert isinstance(push, quota_alert.UserPush)
        self.assertIsInstance(push.ask, quota_alert.Ask)
        self.assertIn("/login", push.ask.action)

    def test_quota_choices_explain_what_restores_work_and_what_costs_money(self) -> None:
        _ = quota_alert.REGISTRY.write_text(REGISTRY.replace("impl=opus:high", "impl=opus:ultra"))
        """F016: usage choices say how work resumes and when credits must be bought."""
        claude = self.note("claude 2.md", "active", "0", limit_reset_count="3")
        codex = self.note("codex 1.md", "active", "0", credit_balance="0")
        spare = self.note("claude 1.md", "inactive", "80", login="spare@example.invalid")
        _ = self.alert([claude, codex, spare], self.now)
        _ = self.block([claude, codex], self.now)
        usage = next(body for title, body, _ in self.pushed if title == "Coding may stop: Claude account out of usage")
        refusal = next(body for title, body, _ in self.pushed if title == "Codex work stopped for lack of usage")
        self.assertIn("If you switch to it, the work can continue", usage)
        self.assertNotIn("limit reset", usage)
        self.assertIn("Adding credits lets Codex accept jobs again", refusal)
        self.assertIn("hit its usage limit", refusal)

    def test_every_user_push_states_its_action_as_send_py_requires(self) -> None:
        _ = quota_alert.REGISTRY.write_text(REGISTRY.replace("impl=opus:high", "impl=opus:ultra"))
        claude = self.note("claude 2.md", "active", "0", limit_reset_count="3")
        codex = self.note("codex 1.md", "active", "0")
        spare = self.note("claude 1.md", "inactive", "80", login="spare@example.invalid")
        sends = self.user_sends(lambda: self.alert([claude, codex, spare], self.now))
        sends |= self.user_sends(lambda: self.block([codex], self.now))
        notes = [title for title, (need, _action) in sends.items() if need == "note"]
        self.assertEqual(len(notes), 1)
        _ = sends.pop(notes[0])
        self.assertEqual(sends, {
            "Coding may stop: Claude account out of usage": (
                "decision", render("quota.claude", account="claude 2", remaining="0%", reset="2026-10-17 09:00 EDT", target="claude 1", target_remaining="80%").action),
            "Codex work stopped for lack of usage": ("decision", ActionRequired(REFUSAL_ACTION)),
        })

    def test_claude_quota_push_targets_the_other_account_with_most_weekly_usage(self) -> None:
        low = self.note("claude 2.md", "active", "0")
        duplicate_login = self.note("claude 9.md", "inactive", "100")
        spare = self.note("claude 1.md", "inactive", "80", login="spare@example.invalid")
        less = self.note("claude 3.md", "active", "20", login="other@example.invalid")
        unknown = self.note("claude 4.md", "inactive", "null", login="unknown@example.invalid")
        with mock.patch.object(quota_alert, "tell_user", wraps=self.tell_user) as phone:
            _ = self.alert([low, less, duplicate_login, unknown, spare], self.now)
        phone.assert_called_once()
        push = cast(RenderedPush, phone.call_args.args[0])
        self.assertEqual(push.values["target"], "claude 1")
        self.assertEqual(push.values["target_remaining"], "80%")
        self.assertIn("claude 1", cast(ActionRequired, push.action).text)

    def test_run_out_push_renders_the_selected_accounts_formatted_usage(self) -> None:
        low = self.note("claude 2.md", "active", "0")
        spare = self.note("claude 1.md", "inactive", "80.50", login="spare@example.invalid")
        less = self.note("claude 3.md", "active", "20", login="other@example.invalid")
        push = quota_alert.run_out_push(low, accounts.claude_account_selection(), [low, spare, less])
        self.assertIsInstance(push, quota_alert.UserPush)
        assert isinstance(push, quota_alert.UserPush)
        self.assertEqual(push.rendered.values["target"], "claude 1")
        self.assertEqual(push.rendered.values["target_remaining"], "80.5%")
        self.assertIn("Account claude 1 still has 80.5% of its weekly usage.",
                      push.text)

    def test_claude_quota_without_an_account_with_usage_only_relays_a_session_note(self) -> None:
        low = self.note("claude 2.md", "active", "0")
        zero = self.note("claude 1.md", "inactive", "0", login="spare@example.invalid")
        unknown = self.note("claude 3.md", "inactive", "null", login="unknown@example.invalid")
        _ = self.alert([low, zero, unknown], self.now)
        self.assertEqual(self.pushed, [])
        self.assertEqual({recipient for recipient, _ in self.sent}, {"natedev", "boss of bosses"})
        self.assertTrue(all("No other Claude account has weekly usage left" in text for _, text in self.sent))
        self.sent.clear()
        _ = self.alert([low, zero, unknown], self.now + timedelta(minutes=2))
        self.assertEqual((self.sent, self.pushed), ([], []))

    def test_user_delivery_names_success_process_failure_and_launch_failure(self) -> None:
        push = render("quota.claude", account="claude 2", remaining="0%", reset="2026-10-17 09:00 EDT", target="claude 1", target_remaining="80%")
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
            self.assertIsInstance(REAL_TELL_USER(render("quota.claude", account="claude 2", remaining="0%", reset="2026-10-17 09:00 EDT", target="claude 1", target_remaining="80%")), quota_alert.PushSent)
            command = cast(list[str], run.call_args.args[0])
            self.assertEqual(float(command[command.index("--timeout") + 1]), review.user_send_seconds())
            self.assertEqual(run.call_args.kwargs["timeout"], review.user_send_seconds() + 15)
            self.assertNotIn("input", run.call_args.kwargs)
        self.assertEqual(quota_alert.RELAY_TIMEOUT, 40)

    def test_failed_delivery_retries_on_the_same_cadence(self) -> None:
        notes: Notes = [self.note("codex 1.md", "active", "1")]
        self.failing.add("boss of bosses")
        log = self.alert(notes, self.now)
        self.assertIn("quota alert codex 1 -> boss of bosses: not delivered: not reachable", log)
        _ = self.recipients()
        _ = self.alert(notes, self.now + timedelta(minutes=30))
        self.assertEqual(self.recipients(), ["boss of bosses", "natedev"])

    def test_recovery_rearms_but_an_unknown_reading_does_not(self) -> None:
        _ = self.alert([self.note("claude 2.md", "active", "0")], self.now)
        _ = quota_alert.acknowledge("claude 2")
        _ = self.recipients()
        _ = self.alert([self.note("claude 2.md", "active", "null")], self.now)
        _ = self.alert([self.note("claude 2.md", "active", "1")], self.now + timedelta(hours=1))
        self.assertEqual(self.recipients(), [])
        _ = self.alert([self.note("claude 2.md", "active", "100")], self.now + timedelta(hours=2))
        self.assertEqual(self.recipients(), ["boss of bosses", "natedev"])
        _ = self.alert([self.note("claude 2.md", "active", "1")], self.now + timedelta(hours=3))
        self.assertEqual(self.recipients(), ["boss of bosses", "natedev"])

    def test_recovery_sends_restored_to_everyone_once(self) -> None:
        _ = self.alert([self.note("codex 1.md", "active", "1"), self.note("claude 2.md", "active", "64")],
                              self.now)
        _ = self.recipients()
        self.pushed.clear()
        notes: Notes = [self.note("codex 1.md", "active", "100"), self.note("claude 2.md", "active", "64")]
        log = self.alert(notes, self.now + timedelta(minutes=2))
        self.assertEqual(log, ["quota restored codex 1 -> natedev: sent", "quota restored codex 1 -> boss of bosses: sent"])
        self.assertEqual(self.pushed, [])
        text = self.sent[0][1]
        self.assertTrue(text.startswith("Quota restored: delegation on Codex can resume; the codex 1 alert is closed."))
        self.assertIn("codex 1, the active Codex account, has 100% of its weekly usage left", text)
        self.assertNotIn("claude 2", text)
        _ = self.recipients()
        self.assertEqual(self.alert(notes, self.now + timedelta(hours=1)), [])

    def test_always_then_showrunners_receive_alert_restored_and_echo_once_by_name(self) -> None:
        with mock.patch.object(quota_alert, "recipients", REAL_RECIPIENTS), mock.patch.object(
                quota_alert, "registered_showrunners", lambda: running("natedev", "boss of bosses", "new director")):
            low_note = self.note("claude 2.md", "active", "0")
            expected = ["natedev", "boss of bosses", "new director"]
            self.assertEqual(quota_alert.recipients(quota_alert.load_config()), expected)
            _ = self.alert([low_note], self.now)
            self.assertCountEqual([recipient for recipient, _ in self.sent], expected)
            _ = self.recipients()
            _ = self.alert([self.note("claude 2.md", "active", "100")], self.now + timedelta(minutes=2))
            self.assertCountEqual([recipient for recipient, _ in self.sent], expected)
            _ = self.recipients()
            _ = quota_alert.tell_others("user action", "natedev")
            self.assertCountEqual([recipient for recipient, _ in self.sent], ["boss of bosses", "new director"])

    def test_an_always_recipient_is_told_when_it_runs_no_production(self) -> None:
        with mock.patch.object(quota_alert, "recipients", REAL_RECIPIENTS), mock.patch.object(
                quota_alert, "registered_showrunners", lambda: running("boss of bosses")):
            self.assertEqual(quota_alert.recipients(quota_alert.load_config()), ["natedev", "boss of bosses"])
            _ = self.alert([self.note("claude 2.md", "active", "0")], self.now)
            self.assertCountEqual([recipient for recipient, _ in self.sent], ["natedev", "boss of bosses"])
            _ = self.recipients()
            _ = self.alert([self.note("claude 2.md", "active", "100")],
                                  self.now + timedelta(minutes=2))
            self.assertCountEqual([recipient for recipient, _ in self.sent], ["natedev", "boss of bosses"])
            _ = self.recipients()
            _ = quota_alert.tell_others("user action", "boss of bosses")
            self.assertEqual([recipient for recipient, _ in self.sent], ["natedev"])

    def test_switch_to_a_low_account_is_not_a_restore(self) -> None:
        _ = self.alert([self.note("codex 1.md", "active", "0")], self.now)
        _ = self.recipients()
        _ = self.alert([self.note("codex 1.md", "inactive", "0", login="old@example.invalid"), self.note("codex 2.md", "active", "1")],
                              self.now + timedelta(minutes=2))
        self.assertTrue(all(text.startswith("Quota alert: codex 2") for _, text in self.sent))

    def test_refresh_announces_to_the_others_even_after_the_timer_closed_it(self) -> None:
        _ = self.alert([self.note("codex 1.md", "active", "1")], self.now)
        notes: Notes = [self.note("codex 1.md", "active", "100")]
        _ = self.alert(notes, self.now + timedelta(minutes=2))
        _ = self.recipients()
        lines = self.refresh(notes, "natedev", self.now + timedelta(minutes=3))
        self.assertEqual(lines[-2:], [f"codex 1: 100% left, resets {self.resets}", "told boss of bosses"])
        self.assertEqual([recipient for recipient, _ in self.sent], ["boss of bosses"])
        self.assertTrue(self.sent[0][1].startswith("Quota restored: delegation on Codex can resume; the user reported"))

    def test_refresh_announces_live_allowance_even_with_low_weekly_quota(self) -> None:
        lines = self.refresh([self.note("codex 1.md", "active", "1")], "natedev", self.now)
        self.assertEqual(lines[-1], "told boss of bosses")
        self.assertTrue(self.sent)
        self.assertTrue(all("codex 1" in text for _, text in self.sent))

    def test_acknowledgement_during_delivery_is_kept(self) -> None:
        def acknowledging_relay(recipient: str, text: str, key: str | None) -> str | None:
            _ = quota_alert.acknowledge()
            return self.relay(recipient, text, key)

        notes: Notes = [self.note("codex 1.md", "active", "1")]
        with mock.patch.object(quota_alert, "relay", acknowledging_relay):
            _ = self.alert(notes, self.now)
        _ = self.recipients()
        _ = self.alert(notes, self.now + timedelta(hours=1))
        self.assertEqual(self.recipients(), [])

    def test_acknowledge_names_one_episode_or_reports_none(self) -> None:
        self.assertEqual(quota_alert.acknowledge(), ["no quota alert is open"])
        _ = self.alert([self.note("codex 1.md", "active", "1"), self.note("claude 2.md", "active", "0")],
                              self.now)
        self.assertEqual(quota_alert.acknowledge("codex 2"), ["no open quota alert named codex 2"])
        _ = quota_alert.acknowledge("codex 1", now=self.now)
        self.assertCountEqual(quota_alert.acknowledge(here="natedev"), [
            "codex 1: already acknowledged at " + self.now.isoformat(timespec="seconds"),
            "claude 2: acknowledged; quiet until it is back above the threshold",
            "told boss of bosses"])

    def test_acknowledgement_is_echoed_to_every_other_configured_session(self) -> None:
        _ = self.alert([self.note("codex 1.md", "active", "1")], self.now)
        _ = self.recipients()
        self.failing.add("boss of bosses")
        self.assertEqual(quota_alert.acknowledge(here="natedev")[-1], "could not tell boss of bosses: not reachable")
        recipient, text = self.sent.pop()
        self.assertEqual((recipient, self.sent), ("boss of bosses", []))
        self.assertTrue(text.startswith('Quota alert acknowledged: the user silenced codex 1 in session "natedev" at '))
        self.assertEqual(quota_alert.acknowledge(here="natedev"), ["codex 1: already acknowledged at "
                                                                   + text.split(" at ", 1)[1].split(". ", 1)[0]])
        self.assertEqual(self.sent, [])

    def test_acknowledgement_outside_a_session_tells_everyone(self) -> None:
        _ = self.alert([self.note("codex 1.md", "active", "1")], self.now)
        _ = self.recipients()
        _ = quota_alert.acknowledge()
        self.assertEqual(self.recipients(), ["boss of bosses", "natedev"])

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
        notes: Notes = [self.note("codex 2.md", "inactive", "0"), self.note("claude 1.md", "active", "2"),
                        self.note("claude 2.md", "active", "null"), self.note("codex 1.md", "active", "0", past)]
        self.assertEqual(self.alert(notes, self.now), [])
        self.assertEqual(self.sent, [])

    def test_a_configured_name_that_is_no_live_session_is_named_in_the_alert_the_others_get(self) -> None:
        notes: Notes = [self.note("codex 1.md", "active", "1")]
        with mock.patch.object(quota_alert, "not_live", natedev_gone):
            _ = self.alert(notes, self.now)
        self.assertIn("natedev, which", self.sent[0][1])
        self.assertTrue(self.sent[0][1].endswith("is not a live session and has not seen it."))

    def test_message_names_the_note_and_its_siblings_but_no_login(self) -> None:
        notes: Notes = [self.note("codex 1.md", "active", "1"), self.note("codex 2.md", "inactive", "40"),
                        self.note("claude 1.md", "inactive", "3")]
        _ = self.alert(notes, self.now)
        text = self.sent[0][1]
        self.assertTrue(text.startswith("Quota alert: codex 1, the active Codex account, has 1% "))
        self.assertIn("codex 2: inactive, 40% weekly usage left", text)
        self.assertIn("every 30 minutes until the user acknowledges it by typing /quota_ack", text)
        self.assertNotIn("claude 1", text)
        self.assertNotIn("@", text)

    def test_a_run_killed_while_sending_does_not_send_again_on_the_next_run(self) -> None:
        note = self.note("claude 2.md", "active", "1")

        def killed(recipient: str, text: str, key: str | None) -> str | None:
            _ = (recipient, text, key)
            raise KeyboardInterrupt  # stands in for the snapshot timer's kill mid-delivery

        with mock.patch.object(quota_alert, "relay", killed), self.assertRaises(KeyboardInterrupt):
            _ = self.alert([note], self.now)
        _ = self.alert([note], self.now + timedelta(minutes=2))
        self.assertEqual(self.sent, [])

    def test_a_claude_alert_never_states_a_recorded_reset_count(self) -> None:
        note = self.note("claude 2.md", "active", "1", limit_reset_count="1")
        text = self.message(note, [note], quota_alert.load_config())
        self.assertNotIn("Limit resets available", text)
        self.assertNotIn("redeem", text)

    def test_a_codex_alert_states_the_reset_count_its_refresh_read(self) -> None:
        note = self.note("codex 1.md", "active", "1", limit_reset_count="2")
        text = self.message(note, [note], quota_alert.load_config())
        self.assertIn("Limit resets available on codex 1: 2,", text)

    def assignments(self) -> dict[str, str]:
        return quota_alert.registry_assignments()

    def refuse(self) -> str:
        """Codex refused work for quota while codex 1 is out of its week; the first log line."""
        return next(line for line in self.block([self.note("codex 1.md", "active", "0")], self.now)
                    if line.startswith(("switched to claude", "switch to claude")))

    def test_explicit_refusal_time_is_recorded_without_replacing_it_with_pass_time(self) -> None:
        notes = [self.note("codex 1.md", "active", "0")]
        readings = self.alert_readings(notes, self.now)
        identity = readings.selected_account
        assert isinstance(identity, agent_accounts.KnownCodexAccount)
        earlier = self.now - timedelta(minutes=1)
        _ = quota_alert.blocked(notes, identity, self.now, codex_readings=readings,
            refused_at=quota_alert.RefusedAt(earlier), window=refusals.KnownWindow("weekly"))
        self.assertEqual(refusals.load()[refusals.key("codex", identity.login)].at, earlier)

    def test_unknown_refusal_time_uses_pass_time_and_ignores_inherited_handoff_environment(self) -> None:
        notes = [self.note("codex 1.md", "active", "0")]
        readings = self.alert_readings(notes, self.now)
        identity = readings.selected_account
        assert isinstance(identity, agent_accounts.KnownCodexAccount)
        with mock.patch.dict(os.environ, {"ACCOUNT_REFUSAL_AT": (self.now - timedelta(days=1)).isoformat()}):
            _ = quota_alert.blocked(notes, identity, self.now, codex_readings=readings,
                refused_at=quota_alert.RefusalTimeUnknown(), window=refusals.KnownWindow("weekly"))
        self.assertEqual(refusals.load()[refusals.key("codex", identity.login)].at, self.now)

    def test_codex_reaching_the_threshold_switches_nothing_and_says_to_keep_delegating(self) -> None:
        log = self.alert([self.note("codex 1.md", "active", "1")], self.now)
        self.assertEqual([line for line in log if "switch" in line], [])
        self.assertEqual(self.assignments(),
                         {"delegate": "codex", "cli": "claude", "fix": "codex", "ask_a_friend": "caller"})
        text = self.sent[0][1]
        self.assertIn("nothing was switched to Claude: keep delegating Codex work", text)
        self.assertIn("if Codex refuses work for quota, bring that to them at once", text)
        self.assertNotIn("start no new", text)
        self.assertIsNone(quota_alert.notice(lambda: "natedev"))

    def test_a_quota_refusal_moves_every_codex_function_to_claude_once(self) -> None:
        notes: Notes = [self.note("codex 1.md", "active", "0")]
        _ = self.alert(notes, self.now)
        self.assertEqual(self.refuse(), "switched to claude for codex 1: delegate, fix")
        self.assertEqual(self.assignments(),
                         {"delegate": "claude", "cli": "claude", "fix": "claude", "ask_a_friend": "caller"})
        self.sent.clear()
        _ = self.alert(notes, self.now + timedelta(minutes=30))
        self.assertEqual(self.sent, [])
        with quota_alert.state_file() as state:
            switch = state["fallback"]
            self.assertIsInstance(switch, quota_alert.AccountCodexQuotaFallback)
            assert isinstance(switch, quota_alert.AccountCodexQuotaFallback)
            self.assertIsInstance(switch.credits_off_hold, quota_alert.NoCreditsOffHold)
        saved = cast(quota_alert.StoredState, json.loads(quota_alert.STATE.read_text()))
        assert "switch" in saved
        self.assertNotIn("credits_off_until", saved["switch"])
        text = quota_alert.message(notes[0], notes, quota_alert.load_config(), switch,
                                  codex_readings=self.alert_readings(notes, self.now))
        stamp = self.now.isoformat(timespec="seconds")
        self.assertIn(f"Every function that ran on Codex (delegate, fix) was moved to Claude at {stamp}", text)
        self.assertIn("Codex work already in flight may fail and should be run again", text)
        self.assertNotIn("start no new", text)
        # The user puts fix back on codex mid-episode; the next run does not move it again.
        self.assertIsNone(quota_alert.edit_registry("agents_set_assignment", "fix", "codex"))
        _ = self.alert(notes, self.now + timedelta(minutes=60))
        self.assertEqual(self.assignments()["fix"], "codex")

    def test_successful_claude_fallback_is_logged_and_relayed_without_a_phone_push(self) -> None:
        notes: Notes = [self.note("codex 1.md", "active", "0")]
        _ = self.alert(notes, self.now)
        self.sent.clear()
        self.pushed.clear()
        later = self.now + timedelta(minutes=5)
        log = self.block(notes, later)
        self.assertIn("switched to claude for codex 1: delegate, fix", log)
        self.assertTrue(any(line.startswith("quota status codex 1:") for line in log))
        self.assertEqual([recipient for recipient, _ in self.sent], ["natedev", "boss of bosses"])
        self.assertIn("Every function that ran on Codex (delegate, fix) was moved to Claude", self.sent[0][1])
        self.assertEqual(self.pushed, [])
        self.sent.clear()
        _ = self.block(notes, later + timedelta(minutes=1))
        self.assertEqual(self.sent, [])
        self.assertEqual(self.pushed, [])

    def test_failed_refusal_push_retries_on_the_next_refusal_then_stops(self) -> None:
        _ = quota_alert.REGISTRY.write_text(REGISTRY.replace("impl=opus:high", "impl=opus:ultra"))
        notes: Notes = [self.note("codex 1.md", "active", "0")]
        _ = self.alert(notes, self.now)
        self.pushed.clear()
        self.push_errors.append(quota_alert.PushFailed("phone offline"))
        first = self.block(notes, self.now + timedelta(minutes=1))
        self.assertIn("quota refusal codex 1 -> user: not delivered: phone offline", first)
        second = self.block(notes, self.now + timedelta(minutes=2))
        self.assertIn("quota refusal codex 1 -> user: sent", second)
        third = self.block(notes, self.now + timedelta(minutes=3))
        self.assertFalse(any(line.endswith("-> user: sent") for line in third))
        self.assertEqual(len(self.pushed), 2)

    def test_refusal_push_says_when_the_switch_had_already_happened(self) -> None:
        note = self.note("codex 1.md", "active", "0")
        _ = self.alert([note], self.now)
        self.pushed.clear()
        stamp = self.now.isoformat(timespec="seconds")
        owner = self.alert_readings([note], self.now).selected_account
        assert isinstance(owner, agent_accounts.KnownCodexAccount)
        with quota_alert.state_file() as state:
            self.assertEqual(quota_alert.switch_to_claude(state, note, stamp, owner),
                             quota_alert.CodexWorkMovedToClaude("codex 1", ("delegate", "fix")))

        log = self.block([note], self.now + timedelta(minutes=1))
        self.assertTrue(any("New work had already moved to Claude" in line for line in log))
        self.assertEqual(self.pushed, [])

    def test_refusal_push_says_when_nothing_was_on_codex(self) -> None:
        registry = quota_alert.REGISTRY
        _ = registry.write_text(REGISTRY.replace("delegate=codex", "delegate=claude").replace(
            "fix=codex", "fix=claude"))
        notes: Notes = [self.note("codex 1.md", "active", "0")]
        _ = self.alert(notes, self.now)
        self.pushed.clear()
        log = self.block(notes, self.now + timedelta(minutes=1))
        self.assertTrue(any("No delegated work needed to move" in line for line in log))
        self.assertEqual(self.pushed, [])

    def test_a_refusal_above_weekly_threshold_still_uses_fallback_when_no_codex_has_room(self) -> None:
        log = self.block([self.note("codex 1.md", "active", "24")], self.now)
        self.assertTrue(any("switched to claude" in line for line in log), log)
        self.assertEqual(self.assignments()["delegate"], "claude")
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(len(self.pushed), 0)
        with quota_alert.state_file() as state:
            self.assertEqual(state["episodes"], {})

    def test_a_refusal_before_the_first_alert_or_after_an_acknowledgement_still_switches(self) -> None:
        notes: Notes = [self.note("codex 1.md", "active", "0")]
        self.assertIn("switched to claude for codex 1: delegate, fix", self.block(notes, self.now))
        self.assertEqual(len(self.sent), 2)
        _ = self.alert([self.note("codex 1.md", "active", "100")], self.now + timedelta(minutes=2))
        self.assertEqual(self.assignments()["delegate"], "codex")
        _ = self.alert(notes, self.now + timedelta(minutes=4))
        _ = quota_alert.acknowledge("", "natedev", self.now + timedelta(minutes=5))
        self.sent.clear()
        log = self.block(notes, self.now + timedelta(minutes=6))
        self.assertIn("switched to claude for codex 1: delegate, fix", log)
        self.assertEqual([recipient for recipient, _ in self.sent], ["natedev", "boss of bosses"])

    def test_a_codex_alert_shows_the_credit_balance_or_says_none_is_reported(self) -> None:
        note = self.note("codex 1.md", "active", "1")
        _ = self.alert([note], self.now)
        self.assertEqual(self.sent[0][1].splitlines()[1], "No credit balance is reported for codex 1.")
        for value, line in (("60498", "Credits left on codex 1: 60,498."), ("0", "Credits left on codex 1: 0."),
                            ("unlimited", "Credits on codex 1: unlimited."),
                            ("null", "No credit balance is reported for codex 1.")):
            _ = note.path.write_text(note.path.read_text().replace("---\n", f"---\ncredit_balance: {value}\n", 1))
            read = read_note(note.path)
            assert read is not None
            self.assertEqual(quota_alert.credit_line(read), line)
            _ = note.path.write_text(note.path.read_text().replace(f"credit_balance: {value}\n", "", 1))
        self.sent.clear()
        _ = self.alert([self.note("claude 2.md", "active", "0")], self.now)
        self.assertNotIn("redit", self.sent[0][1])

    def test_claude_running_out_switches_nothing(self) -> None:
        _ = self.alert([self.note("claude 2.md", "active", "0")], self.now)
        self.assertEqual(self.assignments()["delegate"], "codex")
        self.assertIn("start no new Claude work", self.sent[0][1])
        self.assertIsNone(quota_alert.notice(lambda: "natedev"))

    def test_recovery_moves_back_only_what_is_still_on_claude(self) -> None:
        _ = self.alert([self.note("codex 1.md", "active", "0")], self.now)
        _ = self.refuse()
        self.assertIsNone(quota_alert.edit_registry("agents_set_assignment", "fix", "codex"))
        self.sent.clear()
        log = self.alert([self.note("codex 1.md", "active", "100")], self.now + timedelta(hours=1))
        self.assertIn("switch to claude undone: moved back from Claude to Codex: delegate; left alone, "
                      + "re-assigned since the switch: fix", log)
        self.assertEqual(self.assignments(),
                         {"delegate": "codex", "cli": "claude", "fix": "codex", "ask_a_friend": "caller"})
        self.assertIn("The automatic switch is undone: moved back from Claude to Codex: delegate;", self.sent[0][1])

    def test_a_refused_switch_still_alerts_and_says_why(self) -> None:
        registry = quota_alert.REGISTRY
        _ = registry.write_text(REGISTRY.replace("impl=opus:high", "impl=opus:ultra"))
        notes: Notes = [self.note("codex 1.md", "active", "0")]
        _ = self.alert(notes, self.now)
        self.pushed.clear()
        log = self.block(notes, self.now)
        self.assertTrue(any(line.startswith("switch to claude for codex 1 failed: ERROR: [delegate.impl] effort 'ultra'") for line in log))
        self.assertIn("quota refusal codex 1 -> user: sent", log)
        self.assertEqual(self.pushed[0][0::2], ("Codex work stopped for lack of usage", quota_alert.Ask("decision", REFUSAL_ACTION)))
        self.assertIn("Moving those jobs to Claude", self.pushed[0][1])
        second = self.block(notes, self.now)
        self.assertFalse(any(line.startswith("quota push") for line in second))
        self.assertEqual(len(self.pushed), 1)
        self.assertEqual(self.assignments()["delegate"], "codex")
        self.sent.clear()
        _ = self.alert(notes, self.now + timedelta(minutes=30))
        self.assertEqual(self.sent, [])
        with quota_alert.state_file() as state:
            failed = state.get("refusal_notices", {})[str(self.root / ".codex")].get("switch_failed")
        text = quota_alert.message(notes[0], notes, quota_alert.load_config(), failed=failed,
                                  codex_readings=self.alert_readings(notes, self.now))
        self.assertIn("The automatic switch from Codex to Claude failed: ERROR: [delegate.impl] effort 'ultra'", text)
        self.assertIn("start no new Codex work", text)
        # A registry fixed mid-episode is not switched by the next run.
        _ = registry.write_text(REGISTRY)
        _ = self.alert(notes, self.now + timedelta(minutes=60))
        self.assertEqual(self.assignments()["delegate"], "codex")

    def test_a_stopped_switch_leaves_the_registry_alone(self) -> None:
        _ = self.alert([self.note("codex 1.md", "active", "0")], self.now)
        with mock.patch.object(quota_alert, "EDIT_TIMEOUT", 0.01):
            line = self.refuse()
        self.assertEqual(line, "switch to claude for codex 1 failed: agents_set_all_assignments took longer "
                         + "than 0.01 s and was stopped")
        # The editor's process group died with it: nothing it started lands later.
        time.sleep(2)
        self.assertEqual(self.assignments()["delegate"], "codex")
        self.assertEqual([path.name for path in self.root.glob("agents.conf.*")], [])

    def test_selected_allowance_restores_assignments_despite_low_weekly_episode(self) -> None:
        _ = self.alert([self.note("codex 1.md", "active", "0")], self.now)
        _ = self.refuse()
        self.sent.clear()
        log = self.alert([self.note("codex 1.md", "inactive", "0"), self.note("codex 2.md", "active", "1")],
                                self.now + timedelta(minutes=2))
        self.assertIn("switch to claude undone: moved back from Claude to Codex: delegate, fix", log)
        self.assertEqual(self.assignments()["delegate"], "codex")
        self.assertTrue(any("keep delegating Codex work" in text for _, text in self.sent))
        self.assertTrue(any(text.startswith("Quota restored:")
            and "moved back from Claude to Codex: delegate, fix" in text for _, text in self.sent))
        _ = self.alert([self.note("codex 1.md", "inactive", "0"), self.note("codex 2.md", "active", "90")],
                              self.now + timedelta(minutes=4))
        self.assertEqual(self.assignments()["delegate"], "codex")

    def test_refresh_moves_back_and_says_so(self) -> None:
        _ = self.alert([self.note("codex 1.md", "active", "0")], self.now)
        _ = self.refuse()
        self.sent.clear()
        lines = self.refresh([self.note("codex 1.md", "active", "100")], "natedev",
                                    self.now + timedelta(minutes=3))
        self.assertIn("switch to claude undone: moved back from Claude to Codex: delegate, fix", lines)
        self.assertEqual(self.assignments()["fix"], "codex")
        self.assertEqual([recipient for recipient, _ in self.sent], ["boss of bosses"])
        self.assertIn("The automatic switch is undone: moved back from Claude to Codex: delegate, fix.", self.sent[0][1])

    def test_notice_shows_each_switch_once_and_only_in_the_owner_session(self) -> None:
        self.assertIsNone(quota_alert.notice(lambda: "natedev"))
        _ = self.alert([self.note("codex 1.md", "active", "0")], self.now)
        _ = self.refuse()
        self.assertIsNone(quota_alert.notice(lambda: "boss of bosses"))
        text = quota_alert.notice(lambda: "natedev")
        assert text is not None
        self.assertTrue(text.startswith(f"Codex refused work for quota (codex 1 at 0%, resets {self.resets}): delegate, "
                                        + "fix moved to Claude at "))
        self.assertIsNone(quota_alert.notice(lambda: "natedev"))
        _ = self.alert([self.note("codex 1.md", "active", "100")], self.now + timedelta(hours=1))
        text = quota_alert.notice(lambda: "natedev")
        assert text is not None
        self.assertTrue(text.startswith("Codex recovered at "))
        self.assertTrue(text.endswith(": moved back from Claude to Codex: delegate, fix."))


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
        _ = self.enterContext(mock.patch.object(quota_alert, "not_live", return_value=[]))
        self.phone = self.enterContext(mock.patch.object(quota_alert, "tell_user", return_value=quota_alert.PushSent()))
        self.relay = self.enterContext(mock.patch.object(quota_alert, "relay", return_value=None))
        self.swap = self.enterContext(mock.patch.object(accounts, "swap", return_value=["workers move to codex 2"]))
        _ = self.enterContext(mock.patch.object(agent_accounts, "prepare_codex_home", return_value=agent_accounts.CodexHomePrepared()))
        for number, folder in ((1, ".codex1"), (2, ".codex"), (3, ".codex3")):
            home = self.root / folder
            home.mkdir()
            claims = base64.urlsafe_b64encode(json.dumps({"email": f"account{number}@example.invalid"}).encode()).decode().rstrip("=")
            _ = (home / "auth.json").write_text(json.dumps({"tokens": {"id_token": f"header.{claims}.signature"}}))
            _ = self.note(number)
        accounts.set_role("seats", "codex 1")

    def note(self, number: int, setting: str = "on", *, left: int = 0) -> quota_alert.AgentNote:
        path = self.root / f"codex {number}.md"
        _ = path.write_text(f"---\nlogin: account{number}@example.invalid\nstate: active\n"
            + f"weekly_remaining_usage: {left}\ncredit_balance: 6370\n"
            + f"resets: {(self.now + timedelta(days=2)).isoformat()}\n"
            + (f"use_credits: {setting}\n" if setting else "") + "---\n")
        note = read_note(path)
        assert note is not None
        return note

    def readings(self, now: datetime, *, selected: int = 1, left: int = 0,
                 balance: str | None = "6370", spare_left: int = 50,
                 spare_accounts: tuple[tuple[int, int, str], ...] = (),
                 spare_five_hour: tuple[tuple[int, int], ...] = ()) -> quota_alert.CodexAlertPassReadings:
        homes = {1: self.root / ".codex1", 2: self.root / ".codex", 3: self.root / ".codex3"}
        allowances = {number: (remaining, credits) for number, remaining, credits in spare_accounts}
        five_hour = dict(spare_five_hour)
        identity = agent_accounts.KnownCodexAccount(homes[selected], f"account{selected}@example.invalid", f"codex {selected}")
        reports = tuple(quota_alert.CodexHomeReading(home, Report("Codex",
            email=f"account{number}@example.invalid", source=CodexHomeSource(home),
            quotas=[Quota("Weekly", 100 - (left if number == selected else allowances.get(number, (spare_left, "0"))[0]),
                self.now + timedelta(days=number + 1))],
            five_hour=Quota("5-hour", 100 - five_hour.get(number, 100), now + timedelta(days=2)),
            credit_balance=balance if number == selected else allowances.get(number, (spare_left, "0"))[1]), now) for number, home in homes.items())
        return quota_alert.CodexAlertPassReadings(accounts.CodexSeatsAccount(identity.label, identity.home), identity, reports)

    def alert(self, now: datetime | None = None, *, selected: int = 1, left: int = 0,
              balance: str | None = "6370", spare_left: int = 50,
              spare_accounts: tuple[tuple[int, int, str], ...] = (),
              spare_five_hour: tuple[tuple[int, int], ...] = ()) -> list[str]:
        checked = now or self.now
        notes = [read_note(self.root / f"codex {number}.md") for number in (1, 2, 3)]
        lines = quota_alert.alert([note for note in notes if note is not None], checked,
            selection=ClaudeAccountsInUse(agent_accounts.UnreadableClaudeAccount(), False, (), frozenset()),
            codex_readings=self.readings(checked, selected=selected, left=left, balance=balance,
                                        spare_left=spare_left, spare_accounts=spare_accounts,
                                        spare_five_hour=spare_five_hour))
        for call in self.phone.call_args_list:
            push = cast(RenderedPush, call.args[0])
            self.assertNotIn("default", (push.title + push.body).lower())
            self.assertNotIn("seats", (push.title + push.body).lower())
        for text in self.relayed_texts():
            self.assertNotIn("default", text.lower())
            self.assertNotIn("seats", text.lower())
        return lines

    def relayed_texts(self) -> list[str]:
        return [cast(str, call.args[1]) for call in self.relay.call_args_list]

    def history(self, balances: list[tuple[int, float]]) -> None:
        _ = (self.root / "readings.jsonl").write_text("".join(json.dumps({"account": "codex 1",
            "at": (self.now + timedelta(minutes=minute)).isoformat(), "remaining": 0, "credits": value})
            + "\n" for minute, value in balances))

    def test_zero_credit_allowance_sends_one_identical_phone_and_showrunner_alert(self) -> None:
        _ = self.alert()
        self.phone.assert_called_once()
        push = cast(RenderedPush, self.phone.call_args.args[0])
        self.assertEqual(sorted((call.args[0], call.args[1]) for call in self.relay.call_args_list),
                         [(name, push.title + "\n" + push.body) for name in ("director", "runner")])
        self.assertIn("6,370", push.body)
        self.assertIn("codex 2", push.body)
        self.assertEqual(push.need, "note")
        self.assertIsInstance(push.action, ActionRequired)
        assert isinstance(push.action, ActionRequired)
        self.assertIn("use_credits: off", push.action.text)
        self.assertIn("codex 1.md", push.action.text)
        self.assertIn("natedev", push.action.text)
        self.assertRegex(push.body, r"(?i)\b(?:within|in) (?:(?:a few|two|2) )?minutes\b")
        self.assertIn("paid credits", push.title + push.body)
        for minute in range(2, 32, 2):
            _ = self.alert(self.now + timedelta(minutes=minute))
        self.assertEqual((self.phone.call_count, self.relay.call_count), (1, 2))
        self.swap.assert_not_called()
        with quota_alert.state_file() as state:
            self.assertNotIn("codex 1", state["episodes"])
            episode = state.get("codex_zero", {})["codex 1"]
            self.assertEqual(episode["credits"], "on")
            self.assertNotEqual(episode["phone"], "due")
            self.assertEqual(episode["sessions"], self.now.isoformat(timespec="seconds"))

    def test_zero_phone_failure_retries_only_the_phone(self) -> None:
        self.phone.side_effect = [quota_alert.PushFailed("offline"), quota_alert.PushSent()]
        _ = self.alert()
        with quota_alert.state_file() as state:
            self.assertEqual(state.get("codex_zero", {})["codex 1"]["phone"], "due")
        _ = self.alert(self.now + timedelta(minutes=2))
        _ = self.alert(self.now + timedelta(minutes=4))
        self.assertEqual((self.phone.call_count, self.relay.call_count), (2, 2))

    def test_zero_session_stamp_survives_an_interrupted_relay_without_resending(self) -> None:
        def interrupted(_recipient: str, _text: str, _key: str | None) -> str | None:
            with quota_alert.state_file() as state:
                self.assertEqual(state.get("codex_zero", {})["codex 1"]["sessions"], self.now.isoformat(timespec="seconds"))
            raise RuntimeError("timer interrupted")
        self.relay.side_effect = interrupted
        with self.assertRaisesRegex(RuntimeError, "timer interrupted"):
            _ = self.alert()
        count = self.relay.call_count
        self.relay.side_effect = None
        _ = self.alert(self.now + timedelta(minutes=2))
        self.assertEqual(self.relay.call_count, count)

    def test_zero_credits_off_moves_once_to_the_soonest_reset_and_records_its_policy_cause(self) -> None:
        _ = self.note(1, "off")
        _ = self.alert()
        _ = self.alert(self.now + timedelta(minutes=2))
        self.swap.assert_called_once_with("seats", "codex 2")
        entries = [entry for entry in moves.read_moves() if entry["kind"] == "requested"]
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        assert entry["kind"] == "requested"
        self.assertEqual((entry["from"], entry["to"], entry["window"], entry.get("cause")),
                         ("codex 1", "codex 2", "weekly", "credits off"))
        self.assertEqual(entry["why"], "codex 1 weekly usage used; credits off")
        self.assertIn("codex 2", cast(RenderedPush, self.phone.call_args.args[0]).body)

    def test_zero_credits_off_without_room_switches_to_claude_on_the_same_pass_and_records_one_notice(self) -> None:
        _ = self.note(1, "off")
        with mock.patch.object(quota_alert, "switch_to_claude", wraps=quota_alert.switch_to_claude) as fallback:
            _ = self.alert(spare_left=0)
            fallback.assert_called_once()
            self.assertEqual(quota_alert.registry_assignments()["delegate"], "claude")
            self.assertEqual(quota_alert.registry_assignments()["fix"], "claude")
            for minute in (2, 30):
                _ = self.alert(self.now + timedelta(minutes=minute), spare_left=0)
            fallback.assert_called_once()
        self.swap.assert_not_called()
        self.assertEqual(len([entry for entry in moves.read_moves() if entry["kind"] == "no room"]), 1)
        push = cast(RenderedPush, self.phone.call_args.args[0])
        self.assertIn("Claude", push.body)
        self.assertIn("codex 1", push.body)
        self.assertNotIn("when Codex refuses", push.body)
        self.assertEqual((self.phone.call_count, self.relay.call_count), (1, 2))

    def test_zero_credits_on_without_another_account_names_claude_as_the_destination(self) -> None:
        _ = self.alert(spare_left=0)
        push = cast(RenderedPush, self.phone.call_args.args[0])
        self.assertIn("Claude", push.body)
        self.assertIn("6,370", push.body)
        self.swap.assert_not_called()

    def test_zero_failed_swap_reports_the_failure_and_switches_to_claude_on_the_same_pass(self) -> None:
        _ = self.note(1, "off")
        self.swap.side_effect = accounts.AccountsError("server could not stop")
        with mock.patch.object(quota_alert, "switch_to_claude", wraps=quota_alert.switch_to_claude) as fallback:
            _ = self.alert()
            fallback.assert_called_once()
            for minute in (2, 4, 30, 60):
                _ = self.alert(self.now + timedelta(minutes=minute))
                self.assertEqual(quota_alert.registry_assignments()["delegate"], "claude")
            fallback.assert_called_once()
        self.assertEqual(quota_alert.registry_assignments()["delegate"], "claude")
        entries = moves.read_moves()
        requested = [entry for entry in entries if entry["kind"] == "requested"]
        failed = [entry for entry in entries if entry["kind"] == "failed"]
        self.assertEqual((len(requested), len(failed)), (1, 1))
        self.assertEqual(failed[0]["move_id"], requested[0]["move_id"])
        self.assertIn("server could not stop", cast(RenderedPush, self.phone.call_args.args[0]).body)
        self.assertIn("Claude", cast(RenderedPush, self.phone.call_args.args[0]).body)
        self.swap.assert_called_once()

    def test_credits_off_fallback_hold_survives_reload_and_blocks_recovery_until_the_earliest_refill(self) -> None:
        _ = self.note(1, "off")
        log = self.alert(spare_left=0)
        until = self.now + timedelta(days=2)
        with quota_alert.state_file() as state:
            fallback = state["fallback"]
            self.assertIsInstance(fallback, quota_alert.AccountCodexQuotaFallback)
            assert isinstance(fallback, quota_alert.AccountCodexQuotaFallback)
            self.assertEqual(fallback.credits_off_hold, quota_alert.CreditsOffHold(until))
        saved = cast(quota_alert.StoredState, json.loads(quota_alert.STATE.read_text()))
        assert "switch" in saved and "credits_off_until" in saved["switch"]
        self.assertEqual(saved["switch"]["credits_off_until"], until.isoformat())
        self.assertFalse(any("provider refusal" in line.lower() for line in log))
        # A spare regaining room does not undo the policy fallback before its hold expires.
        for checked in (self.now + timedelta(minutes=2), until - timedelta(minutes=1)):
            _ = self.alert(checked)
            self.assertEqual(quota_alert.registry_assignments()["delegate"], "claude")
            self.swap.assert_not_called()
        _ = self.alert(until)
        self.swap.assert_called_once_with("seats", "codex 2")
        self.assertEqual(quota_alert.registry_assignments()["delegate"], "codex")
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
        self.assertEqual(quota_alert.registry_assignments()["delegate"], "codex")
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
                checked: list[quota_alert.CodexHomeReading] = []
                for home in fresh.checked_homes:
                    if home.home == fresh.seats.home:
                        if availability == "missing":
                            continue
                        if availability == "unknown":
                            home.report.problem = "owner reading unavailable"
                        home = quota_alert.CodexHomeReading(home.home, home.report,
                            self.now if availability == "stale" else later)
                    checked.append(home)
                readings = quota_alert.CodexAlertPassReadings(fresh.seats, fresh.selected_account, tuple(checked))
                with mock.patch.object(self, "readings", return_value=readings):
                    _ = self.alert(later)
                self.swap.assert_not_called()
                self.assertFalse(any(entry["kind"] == "requested" for entry in moves.read_moves()))
                self.assertEqual(quota_alert.registry_assignments()["delegate"], "claude")
                with quota_alert.state_file() as state:
                    self.assertIsInstance(state["fallback"], quota_alert.AccountCodexQuotaFallback)

    def test_failed_claude_switch_reports_work_staying_on_codex_without_internal_logs(self) -> None:
        """F006: a failed routing update cannot claim that new work moved to Claude."""
        _ = self.note(1, "off")
        with mock.patch.object(quota_alert, "edit_registry", return_value="agents_set_all_assignments: permission denied"):
            _ = self.alert(spare_left=0)
        push = cast(RenderedPush, self.phone.call_args.args[0])
        self.assertIn("could not move", push.title)
        self.assertIn("switch to Claude failed", push.body)
        self.assertIn("permission", push.body)
        self.assertIn("Codex work stays on codex 1", push.body)
        self.assertNotIn("New coding work uses Claude", push.body)
        self.assertNotIn("agents_set_all_assignments", push.body)
        self.assertNotIn("switch to claude for", push.body)
        self.assertEqual(quota_alert.registry_assignments()["delegate"], "codex")

    def test_failed_swap_names_the_account_and_user_cause_without_accounts_diagnostics(self) -> None:
        """F006: account command diagnostics belong in the journal, not on the phone."""
        _ = self.note(1, "off")
        cause = "accounts: cannot use codex 2 (~/.codex): usage reading is unavailable"
        self.swap.side_effect = accounts.AccountsError(cause)
        _ = self.alert()
        push = cast(RenderedPush, self.phone.call_args.args[0])
        self.assertIn("could not move to codex 2", push.body)
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
        ids = {"quota.codex_on_credits", "quota.codex_on_credits_last", "quota.codex_workers_moved",
               "quota.codex_workers_stay", "quota.codex_credits_forecast"}
        with mock.patch.dict(os.environ, {"USER_ZONE": "America/New_York"}):
            for entry in catalog():
                if entry.id not in ids:
                    continue
                for example in entry.examples:
                    with self.subTest(id=entry.id, example=example.name):
                        quota_alert.STATE.unlink(missing_ok=True)
                        moves.store_file().unlink(missing_ok=True)
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
                        accounts.set_role("seats", f"codex {source}")
                        balance = "6370" if source == 1 else "2114"
                        if example.name == "unlimited":
                            balance = "unlimited"
                        unknown_balance = example.name in ("unknown", "unknown_balance")
                        no_room = (entry.id == "quota.codex_on_credits_last"
                                   or example.name in ("no_room", "unknown_use"))
                        target_left = 0 if source == 2 or no_room else 97
                        target_credits = "0" if no_room else "6370" if source == 2 else "2114"
                        used = 120 if source == 1 else 40
                        if example.name == "zero_use":
                            used = 0
                        observed_minutes = 20 if example.name == "unknown_use" else 60
                        numeric_balance = 6370 if source == 1 else 2114
                        _ = run_out.READINGS_LOG.write_text("".join(json.dumps({
                            "account": f"codex {source}", "at": at.isoformat(), "remaining": 0, "credits": credits,
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
                                "accounts: cannot use codex 1 (~/.codex1): usage reading is unavailable")
                        with mock.patch.object(self, "readings", side_effect=scenario_readings):
                            if entry.id == "quota.codex_credits_forecast":
                                _ = self.alert(self.now - timedelta(hours=1))
                            _ = self.alert(self.now)
                        with quota_alert.state_file() as state:
                            episode = state.get("codex_zero", {})[f"codex {source}"]
                            if entry.id == "quota.codex_credits_forecast":
                                assert "forecast_notice" in episode
                                notice = episode["forecast_notice"]
                            else:
                                notice = episode["notice"]
                        self.assertEqual(notice["id"], entry.id)
                        self.assertEqual(notice["values"], example.values)
                        self.assertEqual(cast(RenderedPush, self.phone.call_args.args[0]), render_entry(entry, example.values))

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
            self.assertEqual(fallback.credits_off_hold, quota_alert.CreditsOffHold(until))
        self.assertIn(until.strftime("%Y-%m-%d %H:%M"), cast(RenderedPush, self.phone.call_args.args[0]).body)
        _ = self.alert(until - timedelta(minutes=1))
        self.assertEqual(quota_alert.registry_assignments()["delegate"], "claude")
        self.swap.assert_not_called()
        _ = self.alert(until)
        self.swap.assert_called_once_with("seats", "codex 2")
        self.assertEqual(quota_alert.registry_assignments()["delegate"], "codex")

    def test_credits_off_hold_releases_on_explicit_account_change(self) -> None:
        _ = self.note(1, "off")
        _ = self.alert(spare_left=0)
        accounts.set_role("seats", "codex 2")
        _ = self.alert(self.now + timedelta(minutes=2), selected=2, left=50)
        self.assertEqual(quota_alert.registry_assignments()["delegate"], "codex")
        self.swap.assert_not_called()
        with quota_alert.state_file() as state:
            self.assertIsInstance(state["fallback"], quota_alert.NoCodexQuotaFallback)

    def test_on_credits_notice_distinguishes_weekly_first_turn_off_from_leave_on_destination(self) -> None:
        _ = self.alert(spare_accounts=((2, 0, "6370"), (3, 50, "0")))
        body = cast(RenderedPush, self.phone.call_args.args[0]).body
        turn_off = next(line for line in body.splitlines() if line.lower().startswith("turn off"))
        leave_on = next(line for line in body.splitlines() if line.lower().startswith("leave on"))
        self.assertIn("codex 3", turn_off)
        self.assertIn("50%", turn_off)
        self.assertIn("codex 2", leave_on)
        self.assertIn("paid credits", leave_on)
        self.swap.assert_not_called()

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
            return pick("codex", now, exclude=exclude, keep=keep, prefer_weekly_room=prefer_weekly_room)

        with mock.patch.object(accounts, "pick", side_effect=room_changes):
            _ = self.alert()
        push = cast(RenderedPush, self.phone.call_args.args[0])
        self.assertEqual(push.id, "quota.codex_on_credits_last")
        self.assertIn("new work uses Claude", push.body)
        self.swap.assert_not_called()

    def test_zero_credits_off_prefers_weekly_room_over_a_sooner_reset_on_paid_credits(self) -> None:
        _ = self.note(1, "off")
        _ = self.alert(spare_accounts=((2, 0, "6370"), (3, 50, "0")))
        self.swap.assert_called_once_with("seats", "codex 3")
        self.assertIn("codex 3", cast(RenderedPush, self.phone.call_args.args[0]).body)
        requests = [entry for entry in moves.read_moves() if entry["kind"] == "requested"]
        self.assertEqual(len(requests), 1)
        request = requests[0]
        assert request["kind"] == "requested"
        self.assertEqual(request["to"], "codex 3")

    def test_zero_credits_off_uses_a_paid_target_only_when_no_account_has_weekly_room(self) -> None:
        _ = self.note(1, "off")
        _ = self.alert(spare_accounts=((2, 0, "6370"), (3, 0, "0")))
        self.swap.assert_called_once_with("seats", "codex 2")
        body = cast(RenderedPush, self.phone.call_args.args[0]).body
        self.assertIn("codex 2", body)
        self.assertIn("paid credits", body)

    def test_credits_off_prefers_both_windows_with_room_over_weekly_room_funded_by_credits(self) -> None:
        _ = self.note(1, "off")
        _ = self.alert(spare_accounts=((2, 50, "6370"), (3, 40, "0")), spare_five_hour=((2, 0), (3, 100)))
        self.swap.assert_called_once_with("seats", "codex 3")
        body = cast(RenderedPush, self.phone.call_args.args[0]).body
        self.assertIn("codex 3", body)
        self.assertIn("40%", body)

    def test_weekly_room_with_no_five_hour_allowance_is_described_as_paid_funding(self) -> None:
        _ = self.alert(spare_accounts=((2, 50, "6370"), (3, 0, "0")), spare_five_hour=((2, 0),))
        body = cast(RenderedPush, self.phone.call_args.args[0]).body
        off = next(line for line in body.splitlines() if line.lower().startswith("turn off"))
        on = next(line for line in body.splitlines() if line.lower().startswith("leave on"))
        self.assertIn("codex 2", on)
        self.assertIn("paid credits", on)
        self.assertIn("paid credits", off)
        self.assertNotIn("50%", off + on)
        self.assertNotIn("no paid", body.lower())
        self.assertNotIn("without paid", body.lower())
        _ = self.note(1, "off")
        _ = self.alert(self.now + timedelta(minutes=2),
            spare_accounts=((2, 50, "6370"), (3, 0, "0")), spare_five_hour=((2, 0),))
        self.swap.assert_called_once_with("seats", "codex 2")
        requests = [entry for entry in moves.read_moves() if entry["kind"] == "requested"]
        self.assertEqual(len(requests), 1)
        request = requests[0]
        assert request["kind"] == "requested"
        self.assertEqual(request.get("cause"), "credits off")

    def test_zero_reads_the_selected_accounts_setting_and_invalid_values_log_once(self) -> None:
        _ = self.note(2, "off")
        _ = self.note(1, "sometimes")
        lines = self.alert()
        self.assertEqual(lines.count("codex 1: use_credits must be on or off; reading it as on"), 1)
        self.swap.assert_not_called()
        self.assertIn("6,370", cast(RenderedPush, self.phone.call_args.args[0]).body)

    def test_unknown_selected_identity_preserves_an_open_zero_episode_without_resending_it(self) -> None:
        _ = self.alert()
        with quota_alert.state_file() as state:
            original = state.get("codex_zero", {})["codex 1"].copy()
        reading = self.readings(self.now + timedelta(minutes=2))
        unknown = quota_alert.CodexAlertPassReadings(reading.seats,
            agent_accounts.UnknownCodexAccount(reading.seats.home, "sign-in temporarily unreadable"), reading.checked_homes)
        with mock.patch.object(self, "readings", return_value=unknown):
            _ = self.alert(self.now + timedelta(minutes=2))
        with quota_alert.state_file() as state:
            self.assertEqual(state.get("codex_zero", {})["codex 1"], original)
        _ = self.alert(self.now + timedelta(minutes=4))
        self.assertEqual((self.phone.call_count, self.relay.call_count), (1, 2))

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
        opening = cast(RenderedPush, self.phone.call_args.args[0])
        _ = self.note(1, "off")
        _ = self.alert(self.now + timedelta(minutes=2))
        self.swap.assert_called_once_with("seats", "codex 2")
        with quota_alert.state_file() as state:
            forecast_at = state.get("codex_zero", {})["codex 1"]["forecast_at"]
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
            episode = state.get("codex_zero", {})["codex 1"]
            self.assertEqual(episode["credits"], "off")
            self.assertEqual(episode["forecast_at"], forecast_at)
            self.assertNotIn("forecast_phone", episode)

    def test_zero_edited_from_on_to_off_without_room_switches_to_claude_on_the_next_pass(self) -> None:
        _ = self.alert(spare_left=0)
        _ = self.note(1, "off")
        with mock.patch.object(quota_alert, "switch_to_claude", wraps=quota_alert.switch_to_claude) as fallback:
            _ = self.alert(self.now + timedelta(minutes=2), spare_left=0)
            fallback.assert_called_once()
            self.assertEqual(quota_alert.registry_assignments()["delegate"], "claude")
            _ = self.alert(self.now + timedelta(hours=1), spare_left=0)
            fallback.assert_called_once()
        self.swap.assert_not_called()
        self.assertEqual(self.phone.call_count, 1)
        with quota_alert.state_file() as state:
            self.assertNotIn("forecast_phone", state.get("codex_zero", {})["codex 1"])

    def test_zero_edited_from_credits_off_to_on_does_not_start_a_forecast_or_another_move(self) -> None:
        _ = self.note(1, "off")
        _ = self.alert()
        _ = self.note(1, "on")
        for minutes in (2, 60, 120):
            _ = self.alert(self.now + timedelta(minutes=minutes))
        self.swap.assert_called_once_with("seats", "codex 2")
        self.assertEqual(self.phone.call_count, 1)
        self.assertEqual(self.relay.call_count, 2)
        with quota_alert.state_file() as state:
            episode = state.get("codex_zero", {})["codex 1"]
            self.assertEqual(episode["credits"], "off")
            self.assertNotIn("forecast_phone", episode)

    def test_zero_invalid_credit_setting_mid_episode_still_reads_as_on(self) -> None:
        _ = self.alert()
        _ = self.note(1, "invalid")
        log = self.alert(self.now + timedelta(minutes=2))
        self.assertEqual(log.count("codex 1: use_credits must be on or off; reading it as on"), 1)
        self.swap.assert_not_called()
        self.history([(0, 6370), (60, 6370)])
        _ = self.alert(self.now + timedelta(hours=1))
        self.assertEqual(self.phone.call_count, 2)
        self.assertIn("forecast", cast(RenderedPush, self.phone.call_args.args[0]).id)
        with quota_alert.state_file() as state:
            self.assertEqual(state.get("codex_zero", {})["codex 1"]["credits"], "on")

    def test_zero_does_not_open_from_a_stale_selected_snapshot(self) -> None:
        readings = self.readings(self.now)
        note = self.note(1)
        _ = quota_alert.alert([note], self.now + timedelta(minutes=10, seconds=1),
            selection=ClaudeAccountsInUse(agent_accounts.UnreadableClaudeAccount(), False, (), frozenset()),
            codex_readings=readings)
        self.phone.assert_not_called()
        self.relay.assert_not_called()
        self.swap.assert_not_called()
        with quota_alert.state_file() as state:
            self.assertEqual(state.get("codex_zero", {}), {})

    def test_zero_opening_names_unlimited_or_unknown_balance_truthfully(self) -> None:
        for balance in ("unlimited", None):
            with self.subTest(balance=balance):
                quota_alert.STATE.unlink(missing_ok=True)
                self.phone.reset_mock()
                _ = self.alert(balance=balance)
                body = cast(RenderedPush, self.phone.call_args.args[0]).body
                if balance == "unlimited":
                    self.assertTrue("unlimited" in body or "no limit" in body)
                else:
                    self.assertIn("credit balance", body)
                    self.assertTrue("no credit balance" in body or "unknown" in body)

    def test_zero_enters_from_a_low_episode_without_restored_acknowledgement_or_repeats(self) -> None:
        _ = self.alert(left=1)
        with quota_alert.state_file() as state:
            self.assertIn("codex 1", state["episodes"])
        self.phone.reset_mock()
        self.relay.reset_mock()
        _ = self.alert(self.now + timedelta(minutes=2))
        _ = self.alert(self.now + timedelta(minutes=32))
        self.assertEqual((self.phone.call_count, self.relay.call_count), (1, 2))
        self.assertFalse(any("Quota restored:" in text or "/quota_ack" in text for text in self.relayed_texts()))
        with quota_alert.state_file() as state:
            self.assertNotIn("codex 1", state["episodes"])

    def test_zero_closes_silently_after_recovery_swap_away_or_weekly_reset(self) -> None:
        for close in ("recovered", "swapped", "reset"):
            with self.subTest(close=close):
                quota_alert.STATE.unlink(missing_ok=True)
                accounts.set_role("seats", "codex 1")
                _ = self.alert()
                self.phone.reset_mock()
                self.relay.reset_mock()
                if close == "recovered":
                    _ = self.alert(self.now + timedelta(minutes=2), left=80)
                elif close == "swapped":
                    accounts.set_role("seats", "codex 2")
                    _ = self.alert(self.now + timedelta(minutes=2), selected=2, left=80)
                else:
                    _ = self.alert(self.now + timedelta(days=2))
                self.phone.assert_not_called()
                self.relay.assert_not_called()
                with quota_alert.state_file() as state:
                    self.assertNotIn("codex 1", state.get("codex_zero", {}))

    def test_zero_forecasts_at_an_hour_and_only_the_first_forecast_goes_to_the_phone(self) -> None:
        _ = self.alert()
        self.phone.reset_mock()
        self.relay.reset_mock()
        self.history([(0, 6370), (30, 6340), (60, 6310)])
        _ = self.alert(self.now + timedelta(minutes=58), balance="6310")
        self.phone.assert_not_called()
        self.relay.assert_not_called()
        _ = self.alert(self.now + timedelta(hours=1), balance="6310")
        self.assertEqual((self.phone.call_count, self.relay.call_count), (1, 2))
        push = cast(RenderedPush, self.phone.call_args.args[0])
        self.assertIn("60", push.body)
        self.assertIn("6,310", push.body)
        self.assertIn("codex 2", push.body)
        self.assertEqual(self.relayed_texts(), [push.title + "\n" + push.body] * 2)
        self.history([(0, 6370), (60, 6310), (90, 6280), (120, 6250)])
        _ = self.alert(self.now + timedelta(hours=2), balance="6250")
        _ = self.alert(self.now + timedelta(hours=2, minutes=2), balance="6250")
        self.assertEqual((self.phone.call_count, self.relay.call_count), (1, 4))

    def test_zero_first_forecast_phone_failure_retries_without_an_extra_forecast_relay(self) -> None:
        _ = self.alert()
        self.phone.reset_mock()
        self.relay.reset_mock()
        self.phone.side_effect = [quota_alert.PushFailed("offline"), quota_alert.PushSent()]
        self.history([(0, 6370), (60, 6370)])
        _ = self.alert(self.now + timedelta(hours=1))
        _ = self.alert(self.now + timedelta(hours=1, minutes=2))
        _ = self.alert(self.now + timedelta(hours=1, minutes=4))
        self.assertEqual((self.phone.call_count, self.relay.call_count), (2, 2))
        self.assertIn("no credits", cast(RenderedPush, self.phone.call_args.args[0]).body)

    def test_zero_forecast_names_unknown_usage_when_observations_do_not_cover_the_hour(self) -> None:
        _ = self.alert()
        self.phone.reset_mock()
        self.history([(40, 6370), (60, 6360)])
        _ = self.alert(self.now + timedelta(hours=1), balance="6360")
        self.assertIn("unknown", cast(RenderedPush, self.phone.call_args.args[0]).body)

    def test_zero_forecast_uses_the_observed_half_hour_for_its_run_out_rate(self) -> None:
        _ = self.alert()
        self.phone.reset_mock()
        self.history([(30, 120), (60, 90)])
        _ = self.alert(self.now + timedelta(hours=1), balance="90")
        text = cast(RenderedPush, self.phone.call_args.args[0]).body
        self.assertIn("30", text)
        self.assertIn((self.now + timedelta(hours=2, minutes=30)).strftime("%Y-%m-%d %H:%M"), text)

    def test_zero_forecast_names_unlimited_and_unknown_current_credit_balances(self) -> None:
        for balance, expected in (("unlimited", "unlimited"), (None, "no credit balance")):
            with self.subTest(balance=balance):
                quota_alert.STATE.unlink(missing_ok=True)
                _ = self.alert()
                self.phone.reset_mock()
                _ = self.alert(self.now + timedelta(hours=1), balance=balance)
                self.assertIn(expected, cast(RenderedPush, self.phone.call_args.args[0]).body)


if __name__ == "__main__":
    _ = unittest.main()
