"""Isolated account, registry and delivery fixtures for quota-alert tests."""

from __future__ import annotations

import io
import base64
import json
import os
import subprocess
import shutil
import sys
import tempfile
import unittest
from collections.abc import Callable
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import ClassVar, cast, override
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.lib.test_agents_registry import ExactRows, registry_text, write_registry
from scripts.whoami.note_file import note_name

sys.path.insert(0, str(Path(__file__).resolve().parent))

import quota_alert
from push_texts import RenderedPush
from scripts.whoami import agent_accounts
from scripts.whoami.agent_accounts import ClaudeAccountsInUse, CodexHomeSource, Quota, Report
import agent_notes
from scripts.whoami import session_table
from scripts.whoami import note_file
from scripts.whoami import account
from scripts.whoami import usage_store
from agent_notes import local_reset
from scripts.whoami.note_file import read_note
from scripts.accounts import accounts, moves, next_moves, role_map
from scripts.production import showrunners

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "message"))
import send
# send puts scripts/notify on the path.
from user_action import ActionRequired, UserAction, parse_user_action

Notes = list[quota_alert.AgentNote]

REGISTRY = registry_text({
    'assignments': ExactRows({'direct': 'codex', 'cli': 'claude',
                              'module_review': 'codex   # a trailing comment', 'ask_a_friend': 'caller'}),
    'direct.codex': ExactRows({'impl': 'gpt-6-sol:high'}),
    'direct.claude': ExactRows({'impl': 'opus:high'}),
    'cli.codex': ExactRows({'commit_prep': 'gpt-6-sol:high'}),
    'cli.claude': ExactRows({'commit_prep': 'sonnet:high'}),
    'module_review.codex': ExactRows({'review': 'gpt-6-sol:xhigh'}),
    'module_review.claude': ExactRows({'review': 'opus:max'}),
    'ask_a_friend.codex': ExactRows({'consultation': 'gpt-6-sol:max'}),
    'ask_a_friend.claude': ExactRows({'consultation': 'opus:max'}),
    'codex.agents': ExactRows({'gpt-6-sol': 'low,medium,high,xhigh,max'}),
    'claude.agents': ExactRows({'opus': 'low,medium,high,xhigh,max', 'sonnet': 'low,medium,high'}),
})

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


class QuotaReminderFixture(unittest.TestCase):
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
        self.root = self.fixture_root()
        _ = self.enterContext(mock.patch.object(session_table, "live_sessions", return_value=session_table.ReadSessions({}, frozenset())))
        environment_context = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            "HOME": str(self.root), "AGENT_NOTES_DIR": str(self.root),
        })))
        del environment_context
        self.prepare_files()
        _ = self.enterContext(mock.patch.object(note_file, "AGENTS_DIR", self.root))
        _ = self.enterContext(mock.patch.object(agent_notes, "READINGS_LOG", self.root / "readings.jsonl"))
        config = self.root / "showrunners.json"
        registry = self.root / "agents.conf"
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
        for key, value in (("CODEX_CATALOG_SYNC_STATE_FILE", synced), ("CODEX_CONFIG_FILE", self.root / "absent"),
                           ("CODEX_MODELS_CACHE_FILE", self.root / "absent")):
            self.addCleanup(self.restore_environment, key, os.environ.get(key))
            os.environ[key] = str(value)

    def fixture_root(self) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return Path(directory.name).resolve()

    def prepare_files(self) -> None:
        """Write the immutable starting account and registry documents."""
        (self.root / ".claude/config").mkdir(parents=True)
        _ = (self.root / ".claude/config/accounts.json").write_text(
            json.dumps({"workers": "codex1"}))
        _ = (self.root / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "someone@example.com"}}))
        codex_home = self.root / ".codex"
        codex_home.mkdir()
        claims = base64.urlsafe_b64encode(json.dumps({"email": "someone@example.com"}).encode()).decode().rstrip("=")
        _ = (codex_home / "auth.json").write_text(json.dumps({"tokens": {"id_token": f"header.{claims}.signature"}}))
        _ = (self.root / "showrunners.json").write_text(json.dumps({
            "threshold_percent": 1, "repeat_minutes": 30, "stall_minutes": 5,
            "faults_to": "natedev", "always": ["natedev"]}))
        _ = write_registry((self.root / "agents.conf"), base_text=REGISTRY)
        (self.root / "catalog synced").touch()

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
        from scripts.whoami import usage_store
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

    def assignments(self) -> dict[str, str]:
        return quota_alert.registry_assignments()

    def refuse(self) -> str:
        """Codex refused work for quota while codex1 is out of its week; the first log line."""
        return next(line for line in self.block([self.note("codex1.md", "active", "0")], self.now)
                    if line.startswith(("switched to claude", "switch to claude")))

    def routing_entries(self) -> list[dict[str, object]]:
        path = moves.store_file()
        if not path.exists():
            return []
        entries = [cast(dict[str, object], json.loads(line)) for line in path.read_text().splitlines()]
        return [entry for entry in entries if entry.get("kind") in
                ("routed to claude", "routed back", "routing not restored")]

class SeededQuotaReminderFixture(QuotaReminderFixture):
    """Copy a class's account and registry seed to the same private path per test.

    Python alert logic and the real Bash registry editor remain unchanged. Other
    users of QuotaReminderFixture keep their original per-test construction.
    """

    seed: ClassVar[Path]
    case: ClassVar[Path]

    @classmethod
    @override
    def setUpClass(cls) -> None:
        super().setUpClass()
        directory = tempfile.TemporaryDirectory(prefix="quota-reminder-test-")
        cls.addClassCleanup(directory.cleanup)
        root = Path(directory.name).resolve()
        cls.seed, cls.case = root / "seed", root / "case"
        cls.seed.mkdir()
        builder = cls()
        builder.root = cls.seed
        QuotaReminderFixture.prepare_files(builder)

    @override
    def fixture_root(self) -> Path:
        _ = shutil.copytree(self.seed, self.case)
        self.addCleanup(shutil.rmtree, self.case)
        return self.case

    @override
    def prepare_files(self) -> None:
        # fixture_root has restored the class seed, including its sync stamp.
        pass


class CodexZeroEpisodeFixture(unittest.TestCase):
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
        _ = write_registry(registry, base_text=REGISTRY)
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

    def zero_pushes(self) -> list[RenderedPush]:
        return [push for call in self.phone.call_args_list
                if (push := cast(RenderedPush, call.args[0])).id != "quota.codex_next_move"]

    def relayed_texts(self) -> list[str]:
        return [cast(str, call.args[1]) for call in self.relay.call_args_list]

    def history(self, balances: list[tuple[int, float]]) -> None:
        _ = (self.root / "readings.jsonl").write_text("".join(json.dumps({"account": "codex1",
            "at": (self.now + timedelta(minutes=minute)).isoformat(), "remaining": 0, "credits": value})
            + "\n" for minute, value in balances))

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


class CodexRerunFixture(CodexZeroEpisodeFixture):
    """Persist fake assignments while the real alert state and journal survive a stop."""

    @override
    def setUp(self) -> None:
        super().setUp()
        _ = (self.root / "rerun-assignments.json").write_text(json.dumps({
            "direct": "codex", "module_review": "codex", "cli": "claude", "ask_a_friend": "caller"}))
        _ = self.enterContext(mock.patch.object(quota_alert, "registry_assignments", side_effect=self.read_assignments))
        _ = self.enterContext(mock.patch.object(quota_alert, "edit_registry", side_effect=self.edit_assignments))
        _ = self.enterContext(mock.patch.object(accounts, "check_codex_target", return_value=
            accounts.SelectableCodexAccount(self.root / ".codex3", "account3@example.invalid", "codex3")))

    def read_assignments(self) -> dict[str, str]:
        return cast(dict[str, str], json.loads((self.root / "rerun-assignments.json").read_text()))

    def edit_assignments(self, editor: str, *arguments: str) -> str | None:
        assignments = self.read_assignments()
        if editor == "agents_set_all_assignments":
            assignments = {key: arguments[0] if family in ("codex", "claude") else family
                           for key, family in assignments.items()}
        elif editor == "agents_set_assignment":
            assignments[arguments[0]] = arguments[1]
        else:
            raise AssertionError("unexpected registry editor")
        _ = (self.root / "rerun-assignments.json").write_text(json.dumps(assignments))
        return None
