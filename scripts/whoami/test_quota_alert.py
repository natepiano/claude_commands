"""Repeat-until-acknowledged delivery and message content of the low-quota alert."""

from __future__ import annotations

import io
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
from scripts.whoami import agent_accounts
from agent_accounts import ClaudeAccountSelection, CodexHomeSource, Quota, Report
import agent_notes
import note_file
from agent_notes import local_reset
from note_file import read_note
from scripts.accounts import accounts

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "message"))
import send
# send puts scripts/notify on the path.
from user_action import ActionRequired, NoActionRequired, UserAction, parse_user_action

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
REFUSAL_ACTION = "Add credits to codex 1 or redeem a limit reset to bring Codex back."


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


class QuotaAlertTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root: Path = Path()
        self.now: datetime = datetime.now(timezone.utc).replace(microsecond=0)
        self.resets: str = local_reset(self.now + timedelta(days=2))
        self.failing: set[str] = set()
        self.sent: list[tuple[str, str]] = []
        self.push_errors: list[str | None] = []
        self.pushed: list[tuple[str, str, quota_alert.Ask | None]] = []

    @override
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        environment_context = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            "HOME": str(self.root), "AGENT_NOTES_DIR": str(self.root),
        })))
        del environment_context
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

    def tell_user(self, summary: str, text: str, ask: quota_alert.Ask | None) -> str | None:
        """Record direct delivery so no test can reach send.py's real user channel."""
        self.assertNotIn("/", text)
        self.assertNotIn("@", text)
        self.pushed.append((summary, text, ask))
        return self.push_errors.pop(0) if self.push_errors else None

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
        seats = accounts.CodexSeatsAccount(label="default", home=home)
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
              selection: ClaudeAccountSelection | quota_alert.ReadCurrentClaudeSelection =
              quota_alert.READ_CURRENT_CLAUDE_SELECTION) -> list[str]:
        checked = now or self.now
        return quota_alert.alert(notes, checked, selection=selection,
                                  codex_readings=self.alert_readings(notes, checked))

    def block(self, notes: Notes, now: datetime | None = None, *,
              selection: ClaudeAccountSelection | quota_alert.ReadCurrentClaudeSelection =
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

    def configure_claude_roles(self) -> None:
        _ = (self.root / ".claude").mkdir()
        _ = (self.root / ".claude.json").write_text(
            json.dumps({"oauthAccount": {"emailAddress": "showrunner@example.invalid"}}), encoding="utf-8",
        )
        for label, login in (("claude 1", "spare@example.invalid"), ("claude 3", "unit@example.invalid")):
            self.assertEqual(accounts.setup(label), [])
            _ = (accounts.account_dir(label) / ".claude.json").write_text(
                json.dumps({"oauthAccount": {"emailAddress": login}}), encoding="utf-8",
            )
        accounts.set_role("unit", "claude 3")

    def test_unit_alert_names_role_and_suggests_the_fit_spare_with_most_remaining(self) -> None:
        self.configure_claude_roles()
        # A second spare tests selection by quota rather than account number.
        self.assertEqual(accounts.setup("claude 4"), [])
        _ = (accounts.account_dir("claude 4") / ".claude.json").write_text(
            json.dumps({"oauthAccount": {"emailAddress": "other-spare@example.invalid"}}), encoding="utf-8",
        )
        notes: Notes = [
            self.note("claude 3.md", "active", "1", login="unit@example.invalid"),
            self.note("claude 2.md", "active", "99", login="showrunner@example.invalid"),
            self.note("claude 1.md", "inactive", "40", login="spare@example.invalid"),
            self.note("claude 4.md", "inactive", "70", login="other-spare@example.invalid"),
        ]
        config = quota_alert.load_config()
        config["threshold_percent"] = 4
        text = self.message(notes[0], notes, config)
        self.assertTrue(text.startswith("Quota alert: claude 3 (unit directors) at 1%"))
        self.assertIn('swap: /showrunner:accounts swap unit "claude 4"', text)
        self.assertIn("Sessions on claude 3 start no new Claude work", text)
        self.assertIn("Showrunners relay this notice to their units on that account.", text)
        self.assertNotIn("@", text)

    def test_unit_alert_rejects_expired_unknown_lower_and_unfit_spares(self) -> None:
        self.configure_claude_roles()
        unit = self.note("claude 3.md", "active", "1", login="unit@example.invalid")
        for left, resets in (("null", self.resets), ("nan", self.resets),
                             ("inf", self.resets), ("101", self.resets), ("1", self.resets),
                             ("0", self.resets), ("90", local_reset(self.now - timedelta(hours=1)))):
            with self.subTest(left=left, resets=resets):
                spare = self.note("claude 1.md", "inactive", left, resets, login="spare@example.invalid")
                text = self.message(unit, [unit, spare], quota_alert.load_config())
                self.assertIn("no spare account has more left", text)
        spare = self.note("claude 1.md", "inactive", "90", login="spare@example.invalid")
        (accounts.account_dir("claude 1") / "projects").unlink()
        self.assertIn("no spare account has more left", self.message(
            unit, [unit, spare], quota_alert.load_config(),
        ))

    def test_showrunner_alert_names_its_role_without_suggesting_a_unit_swap(self) -> None:
        self.configure_claude_roles()
        note = self.note("claude 2.md", "active", "0", login="showrunner@example.invalid")
        _ = self.alert([note], self.now)
        self.assertTrue(self.sent[0][1].startswith("Quota alert: claude 2 (showrunners) at 0%"))
        self.assertNotIn("swap:", self.sent[0][1])
        self.assertIn("claude 2 (showrunners)", self.pushed[0][1])

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
        self.assertTrue(all(text.startswith("Quota alert: claude 2 (showrunners)") for _, text in self.sent))
        with quota_alert.state_file() as state:
            self.assertIn("acknowledged", state["episodes"]["claude 3"])
            self.assertNotIn("acknowledged", state["episodes"]["claude 2"])

    def test_acknowledgement_survives_spare_and_unit_role_changes_until_quota_recovers(self) -> None:
        """A role change does not close or rearm the same account's episode."""
        self.configure_claude_roles()
        unit = self.note("claude 3.md", "active", "0", login="unit@example.invalid")
        spare = self.note("claude 1.md", "inactive", "90", login="spare@example.invalid")
        main = self.note("claude 2.md", "active", "80", login="showrunner@example.invalid")
        _ = self.alert([unit, spare, main], self.now)
        _ = quota_alert.acknowledge("claude 3", now=self.now)
        self.sent.clear()
        self.pushed.clear()

        accounts.set_role("unit", "claude 1")
        spare = self.note("claude 1.md", "active", "90", login="spare@example.invalid")
        for left in ("0", "null"):
            unit = self.note("claude 3.md", "inactive", left, login="unit@example.invalid")
            _ = self.alert([unit, spare, main], self.now + timedelta(minutes=2))
            with quota_alert.state_file() as state:
                self.assertEqual(state["episodes"]["claude 3"].get("acknowledged"), self.now.isoformat(timespec="seconds"))
        _ = self.alert([spare, main], self.now + timedelta(minutes=3))
        accounts.set_role("unit", "claude 3")
        unit = self.note("claude 3.md", "active", "0", login="unit@example.invalid")
        spare = self.note("claude 1.md", "inactive", "90", login="spare@example.invalid")
        _ = self.alert([unit, spare, main], self.now + timedelta(hours=1))
        self.assertEqual((self.sent, self.pushed), ([], []))
        with quota_alert.state_file() as state:
            self.assertIn("acknowledged", state["episodes"]["claude 3"])

        accounts.set_role("unit", "claude 1")
        unit = self.note("claude 3.md", "inactive", "80", login="unit@example.invalid")
        _ = self.alert([unit, spare, main], self.now + timedelta(hours=2))
        self.assertTrue(all(text.startswith("Quota restored: work on claude 3 can resume;") for _, text in self.sent))
        self.assertEqual(len(self.sent), 2)
        with quota_alert.state_file() as state:
            self.assertNotIn("claude 3", state["episodes"])
        self.sent.clear()
        accounts.set_role("unit", "claude 3")
        unit = self.note("claude 3.md", "active", "0", login="unit@example.invalid")
        _ = self.alert([unit, spare, main], self.now + timedelta(hours=3))
        self.assertEqual((len(self.sent), len(self.pushed)), (2, 1))

    def test_a_weekly_reset_closes_the_spare_accounts_acknowledged_episode(self) -> None:
        """An account's own reset ends its episode even while it is spare."""
        self.configure_claude_roles()
        unit = self.note("claude 3.md", "active", "0", login="unit@example.invalid")
        _ = self.alert([unit], self.now)
        _ = quota_alert.acknowledge("claude 3", now=self.now)
        accounts.set_role("unit", "claude 1")
        unit = self.note("claude 3.md", "inactive", "null",
                         local_reset(self.now - timedelta(minutes=1)), login="unit@example.invalid")
        self.sent.clear()
        _ = self.alert([unit], self.now + timedelta(minutes=2))
        with quota_alert.state_file() as state:
            self.assertNotIn("claude 3", state["episodes"])
        self.assertEqual(len(self.sent), 2)
        self.assertTrue(all(text.startswith("Quota restored: work on claude 3 can resume;") for _, text in self.sent))

    def test_one_alert_run_keeps_its_roles_when_a_swap_happens_during_rendering(self) -> None:
        """Every message and spare comparison uses one role value."""
        self.configure_claude_roles()
        notes: Notes = [
            self.note("claude 2.md", "active", "0", login="showrunner@example.invalid"),
            self.note("claude 3.md", "active", "0", login="unit@example.invalid"),
            self.note("claude 1.md", "active", "0", login="spare@example.invalid"),
        ]
        resolve_roles = accounts.claude_account_selection

        def swap_after_resolving() -> ClaudeAccountSelection:
            roles = resolve_roles()
            accounts.set_role("unit", "claude 1")
            return roles

        with mock.patch.object(accounts, "claude_account_selection", side_effect=swap_after_resolving) as resolve:
            _ = self.alert(notes, self.now)
        resolve.assert_called_once_with()
        first_lines = [text.splitlines()[0] for _, text in self.sent]
        self.assertEqual(len(first_lines), 4)
        self.assertEqual(sum(line.startswith("Quota alert: claude 2 (showrunners)") for line in first_lines), 2)
        self.assertEqual(sum(line.startswith("Quota alert: claude 3 (unit directors)") for line in first_lines), 2)
        with quota_alert.state_file() as state:
            self.assertEqual(set(state["episodes"]), {"claude 2", "claude 3"})

    def test_alert_uses_the_updaters_role_value_without_resolving_again(self) -> None:
        """Update and alert can share the same run's role value."""
        self.configure_claude_roles()
        unit = self.note("claude 3.md", "active", "0", login="unit@example.invalid")
        roles = accounts.claude_account_selection()
        accounts.set_role("unit", "claude 1")
        with mock.patch.object(accounts, "claude_account_selection", side_effect=AssertionError("roles resolved twice")):
            _ = self.alert([unit], self.now, selection=roles)
        self.assertTrue(self.sent[0][1].startswith("Quota alert: claude 3 (unit directors)"))

    def test_refusal_uses_supplied_selection_without_resolving_again(self) -> None:
        """Refusal notices accept the same run's frozen role value."""
        self.configure_claude_roles()
        note = self.note("codex 1.md", "active", "0")
        roles = accounts.claude_account_selection()
        with mock.patch.object(accounts, "claude_account_selection", side_effect=AssertionError("roles resolved twice")):
            lines = self.block([note], self.now, selection=roles)
        self.assertEqual(lines[0], "switched to claude for codex 1: delegate, fix")
        self.assertEqual((len(self.sent), len(self.pushed)), (2, 1))

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

    def test_unconfigured_claude_alert_retains_the_original_message(self) -> None:
        note = self.note("claude 2.md", "active", "0")
        text = self.message(note, [note], quota_alert.load_config())
        self.assertEqual(text.splitlines()[0],
                         f"Quota alert: claude 2, the active Claude account, has 0% of its weekly "
                         + f"usage left (threshold 1%); it resets {self.resets}.")
        self.assertNotIn("swap:", text)
        self.assertIn("if you delegate Claude work, start no new Claude work", text)

    def test_quota_config_uses_shared_showrunner_settings(self) -> None:
        self.assertEqual(quota_alert.Config.__module__, "scripts.production.showrunners")
        self.assertEqual(quota_alert.load_settings_from.__module__, "scripts.production.showrunners")

    def test_real_user_delivery_is_patched_out(self) -> None:
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
        self.assertEqual(len(self.alert(notes, self.now)), 3)  # one user push and two session sends
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
        notes: Notes = [self.note("codex 1.md", "active", "1", credit_balance="60498")]
        first = self.alert(notes, self.now)
        self.assertIn("quota push codex 1 -> user: sent", first)
        self.assertEqual(len(self.pushed), 1)
        with quota_alert.state_file() as state:
            self.assertEqual(state["episodes"]["codex 1"].get("push"), self.now.isoformat(timespec="seconds"))

        second = self.alert(notes, self.now + timedelta(minutes=30))
        self.assertNotIn("quota push codex 1 -> user: sent", second)
        self.assertEqual(len(self.pushed), 1)

    def test_failed_episode_push_retries_even_after_acknowledgement_then_stops(self) -> None:
        notes: Notes = [self.note("claude 2.md", "active", "0")]
        self.push_errors.append("channel unavailable")
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

    def test_claude_push_names_refill_and_reset_count_at_decision_priority(self) -> None:
        # Ahead of the real clock, which decides whether a reset is still to come.
        refill = (datetime.now().astimezone() + timedelta(days=2)).replace(second=0, microsecond=0)
        resets = refill.strftime("%Y-%m-%d %H:%M")
        note = self.note("claude 2.md", "active", "0", resets, limit_reset_count="3")
        _ = self.alert([note], self.now)
        self.assertEqual(self.pushed, [(
            "claude 2 weekly limit",
            "claude 2 has 0% of its weekly usage left; it refills "
            + f"{refill.astimezone(ZoneInfo('America/Los_Angeles')):%a %m-%d %H:%M %Z}.",
            quota_alert.Ask("decision", "Log in to another Claude account, or redeem a limit reset (3 available)."),
        )])

    def test_codex_push_names_credits_at_note_priority(self) -> None:
        note = self.note("codex 1.md", "active", "0", credit_balance="60498")
        _ = self.alert([note], self.now)
        summary, text, ask = self.pushed[0]
        self.assertEqual((summary, ask), ("codex 1 weekly limit", None))
        self.assertIn("Codex keeps working on its credits. Credits left on codex 1: 60,498.", text)

    def test_every_user_push_states_its_action_as_send_py_requires(self) -> None:
        claude = self.note("claude 2.md", "active", "0", limit_reset_count="3")
        codex = self.note("codex 1.md", "active", "0")
        sends = self.user_sends(lambda: self.alert([claude, codex], self.now))
        sends |= self.user_sends(lambda: self.block([codex], self.now))
        self.assertEqual(sends, {
            "claude 2 weekly limit": (
                "decision", ActionRequired("Log in to another Claude account, or redeem a limit reset (3 available).")),
            "codex 1 weekly limit": ("note", NoActionRequired()),
            "Codex refused work": ("decision", ActionRequired(REFUSAL_ACTION)),
        })
        ask = quota_alert.Ask("blocked", "Redeem a limit reset on claude 2.")
        self.assertEqual(self.user_sends(lambda: quota_alert.tell_user("title", "text", ask)),
                         {"title": ("blocked", ActionRequired(ask.action))})

    def test_user_send_allows_review_and_post_without_shortening_agent_relays(self) -> None:
        done = subprocess.CompletedProcess(["send.py"], 0, "SENT: to the user", "")
        with mock.patch.object(subprocess, "run", return_value=done) as run:
            self.assertIsNone(REAL_TELL_USER("title", "body", None))
            command = cast(list[str], run.call_args.args[0])
            self.assertEqual(command[command.index("--timeout") + 1], "60")
            self.assertEqual(run.call_args.kwargs["timeout"], 75)
            self.assertEqual(run.call_args.kwargs["input"], "body")
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
        _ = self.alert([self.note("codex 1.md", "inactive", "0"), self.note("codex 2.md", "active", "1")],
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

    def assignments(self) -> dict[str, str]:
        return quota_alert.registry_assignments()

    def refuse(self) -> str:
        """Codex refused work for quota while codex 1 is out of its week; the first log line."""
        return self.block([self.note("codex 1.md", "active", "0")], self.now)[0]

    def test_codex_reaching_the_threshold_switches_nothing_and_says_to_keep_delegating(self) -> None:
        log = self.alert([self.note("codex 1.md", "active", "0")], self.now)
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
        text = self.sent[0][1]
        stamp = self.now.isoformat(timespec="seconds")
        self.assertIn(f"Every function that ran on Codex (delegate, fix) was moved to Claude at {stamp}", text)
        self.assertIn("Codex work already in flight may fail and should be run again", text)
        self.assertNotIn("start no new", text)
        # The user puts fix back on codex mid-episode; the next run does not move it again.
        self.assertIsNone(quota_alert.edit_registry("agents_set_assignment", "fix", "codex"))
        _ = self.alert(notes, self.now + timedelta(minutes=60))
        self.assertEqual(self.assignments()["fix"], "codex")

    def test_a_refusal_tells_every_recipient_at_once_and_a_second_one_moves_nothing(self) -> None:
        notes: Notes = [self.note("codex 1.md", "active", "0")]
        _ = self.alert(notes, self.now)
        self.sent.clear()
        self.pushed.clear()
        later = self.now + timedelta(minutes=5)
        log = self.block(notes, later)
        self.assertEqual(log, ["switched to claude for codex 1: delegate, fix",
                               "quota refusal codex 1 -> natedev: sent", "quota refusal codex 1 -> boss of bosses: sent",
                               "quota refusal codex 1 -> user: sent"])
        self.assertEqual([recipient for recipient, _ in self.sent], ["natedev", "boss of bosses"])
        self.assertIn("Every function that ran on Codex (delegate, fix) was moved to Claude", self.sent[0][1])
        self.assertEqual(self.pushed[0][0::2], ("Codex refused work", quota_alert.Ask("decision", REFUSAL_ACTION)))
        self.assertIn("No credit balance is reported for codex 1.", self.pushed[0][1])
        self.assertIn("The automatic switch moved delegate, fix to Claude", self.pushed[0][1])
        self.sent.clear()
        self.assertEqual(self.block(notes, later + timedelta(minutes=1)),
                         ["codex 1 refused work for quota; assignments already use Claude"])
        self.assertEqual(self.sent, [])
        self.assertEqual(len(self.pushed), 1)

    def test_failed_refusal_push_retries_on_the_next_refusal_then_stops(self) -> None:
        notes: Notes = [self.note("codex 1.md", "active", "0")]
        _ = self.alert(notes, self.now)
        self.pushed.clear()
        self.push_errors.append("phone offline")
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
                             "switched to claude for codex 1: delegate, fix")

        log = self.block([note], self.now + timedelta(minutes=1))
        self.assertEqual(log, ["codex 1 refused work for quota; assignments already use Claude",
                               "quota refusal codex 1 -> natedev: sent", "quota refusal codex 1 -> boss of bosses: sent",
                               "quota refusal codex 1 -> user: sent"])
        self.assertIn("The automatic switch had already moved delegate, fix to Claude", self.pushed[0][1])

    def test_refusal_push_says_when_nothing_was_on_codex(self) -> None:
        registry = quota_alert.REGISTRY
        _ = registry.write_text(REGISTRY.replace("delegate=codex", "delegate=claude").replace(
            "fix=codex", "fix=claude"))
        notes: Notes = [self.note("codex 1.md", "active", "0")]
        _ = self.alert(notes, self.now)
        self.pushed.clear()
        log = self.block(notes, self.now + timedelta(minutes=1))
        self.assertEqual(log, ["switch to claude for codex 1: nothing was on codex",
                               "quota refusal codex 1 -> natedev: sent", "quota refusal codex 1 -> boss of bosses: sent",
                               "quota refusal codex 1 -> user: sent"])
        self.assertIn("Nothing was on Codex, so the automatic switch moved nothing", self.pushed[0][1])

    def test_a_refusal_above_the_threshold_moves_nothing(self) -> None:
        log = self.block([self.note("codex 1.md", "active", "24")], self.now)
        self.assertEqual(len(log), 1)
        self.assertIn("nothing switched", log[0])
        self.assertEqual(self.assignments()["delegate"], "codex")
        self.assertEqual(self.sent, [])
        self.assertEqual(self.pushed, [])
        with quota_alert.state_file() as state:
            self.assertEqual(state["episodes"], {})
        self.assertIsNone(quota_alert.notice(lambda: "natedev"))

    def test_a_refusal_before_the_first_alert_or_after_an_acknowledgement_still_switches(self) -> None:
        notes: Notes = [self.note("codex 1.md", "active", "0")]
        self.assertEqual(self.block(notes, self.now)[0], "switched to claude for codex 1: delegate, fix")
        self.assertEqual(len(self.sent), 2)
        _ = self.alert([self.note("codex 1.md", "active", "100")], self.now + timedelta(minutes=2))
        self.assertEqual(self.assignments()["delegate"], "codex")
        _ = self.alert(notes, self.now + timedelta(minutes=4))
        _ = quota_alert.acknowledge("", "natedev", self.now + timedelta(minutes=5))
        self.sent.clear()
        log = self.block(notes, self.now + timedelta(minutes=6))
        self.assertEqual(log[0], "switched to claude for codex 1: delegate, fix")
        self.assertEqual([recipient for recipient, _ in self.sent], ["natedev", "boss of bosses"])

    def test_a_codex_alert_shows_the_credit_balance_or_says_none_is_reported(self) -> None:
        note = self.note("codex 1.md", "active", "0")
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
        self.assertTrue(log[0].startswith("switch to claude for codex 1 failed: ERROR: [delegate.impl] effort 'ultra'"))
        self.assertIn("quota refusal codex 1 -> user: sent", log)
        self.assertEqual(self.pushed[0][0::2], ("Codex refused work", quota_alert.Ask("decision", REFUSAL_ACTION)))
        self.assertIn("The automatic switch to Claude failed: ERROR: [delegate.impl] effort 'ultra'",
                      self.pushed[0][1])
        second = self.block(notes, self.now)
        self.assertFalse(any(line.startswith("quota push") for line in second))
        self.assertEqual(len(self.pushed), 1)
        self.assertEqual(self.assignments()["delegate"], "codex")
        self.sent.clear()
        _ = self.alert(notes, self.now + timedelta(minutes=30))
        text = self.sent[0][1]
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


if __name__ == "__main__":
    _ = unittest.main()
