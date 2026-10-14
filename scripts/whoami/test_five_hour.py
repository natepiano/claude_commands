"""Tests for session warnings, repeat suppression and old-hold cleanup."""
from __future__ import annotations

import tempfile
import subprocess
import os
import base64
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast, override
from unittest.mock import patch

import agent_accounts
from scripts.whoami.agent_accounts import KnownCodexAccount, UnknownCodexAccount
import five_hour
from agent_accounts import CodexHomeSource, CodexRateLimits, Quota, Report
from scripts.accounts import accounts

import escalate  # five_hour puts scripts/message on the path
import quota_alert
import send
import sessions
from quota_alert import Config, Showrunner
from user_action import NoActionRequired

RESETS = (datetime.now(timezone.utc) + timedelta(days=1)).replace(hour=21, minute=20, second=0, microsecond=0)
CONFIG = cast(Config, cast(object, {
    "threshold_percent": 2, "repeat_minutes": 30, "stall_minutes": 5, "faults_to": "hana", "always": ["natedev"],
}))
SHOWRUNNERS = [Showrunner(session="hana", socket="/hana.sock", slug="show", zone="America/Los_Angeles", doc="")]


def report(tool: str, used: float | None) -> Report:
    return Report(tool, five_hour=None if used is None else Quota("5-hour", used, RESETS))


class WatchTests(unittest.TestCase):
    sent: list[tuple[str, str, str | None]] = []
    state: Path = Path()

    @override
    def setUp(self) -> None:
        self.sent = []
        self.state = Path(self.enterContext(tempfile.TemporaryDirectory()))

        def relay(recipient: str, text: str, key: str | None) -> str | None:
            self.sent.append((recipient, text, key))
            return "QUEUED: no live session" if recipient == "hana" else None

        def config() -> Config:
            return CONFIG

        def schedule() -> None:
            return None

        def showrunners() -> list[Showrunner]:
            return SHOWRUNNERS

        for module, name, stand_in in ((five_hour, "relay", relay), (five_hour, "load_config", config),
                                       (quota_alert, "registered_showrunners", showrunners),
                                       (escalate, "schedule", schedule), (escalate, "STATE", self.state / "holds"),
                                       (five_hour, "STATE", self.state / "notices")):
            _ = self.enterContext(patch.object(module, name, stand_in))

    def watch(self, reports: list[Report]) -> list[str]:
        home = self.state / ".codex"
        identity = KnownCodexAccount(home, "fixture@example.invalid", "codex 1")
        now = datetime.now(timezone.utc)
        for live in reports:
            if live.tool == "Codex":
                live.email = identity.login
                live.source = CodexHomeSource(home)
        readings = quota_alert.CodexAlertPassReadings(
            accounts.CodexSeatsAccount(label="default", home=home), identity,
            tuple(quota_alert.CodexHomeReading(home, live, now) for live in reports if live.tool == "Codex"))
        return five_hour.watch(reports, codex_readings=readings)

    def test_a_low_window_tells_the_alert_list_once_without_a_phone_hold(self) -> None:
        with patch.object(escalate, "hold", side_effect=AssertionError("phone hold")):
            lines = self.watch([report("Claude", 82.4), report("Codex", 10)])
        self.assertEqual(lines, ["5-hour limit: Claude 18% left -> natedev",
                                 "5-hour limit: Claude 18% left -> hana: NOT sent: QUEUED: no live session"])
        self.assertEqual([(name, key) for name, _, key in self.sent],
                         [("natedev", "5-hour Claude"), ("hana", "5-hour Claude")])
        self.assertEqual(self.sent[0][1],
                         "5-hour limit: Claude has 18% of its 5-hour limit left; it resets at 14:20 PDT.\n"
                         + "Tell the user in one line and change nothing else.\n"
                         + f"Protocol: {quota_alert.PROTOCOL}")
        self.assertIsInstance(escalate.read(escalate.held_path("five-hour-claude")), escalate.NoHeld)
        self.assertEqual(five_hour.notice_path("five-hour-claude").read_text(), "Claude")
        self.assertEqual(self.watch([report("Claude", 90)]), [])
        self.assertEqual(len(self.sent), 2)

    def test_old_phone_hold_closes_once_on_recovery(self) -> None:
        _ = escalate.hold("five-hour-claude", escalate.SessionPush("Claude", "Usage low", NoActionRequired(), "note"))
        self.assertEqual(self.watch([report("Claude", 3)]), ["5-hour limit: Claude is back above 20%"])
        self.assertEqual(self.watch([report("Claude", 3)]), [])
        self.assertIsInstance(escalate.read(escalate.held_path("five-hour-claude")), escalate.NoHeld)
        self.assertEqual(self.sent, [])

    def test_recovery_clears_the_warning_record_and_the_next_low_spell_is_new(self) -> None:
        _ = self.watch([report("Codex", 95)])
        self.assertEqual(self.watch([report("Codex", 3)]), ["5-hour limit: codex 1 is back above 20%"])
        self.assertIsInstance(escalate.read(escalate.held_path("five-hour-codex")), escalate.NoHeld)
        self.assertEqual(self.watch([report("Codex", 3)]), [])
        self.assertEqual(len(self.watch([report("Codex", 80)])), 2)

    def test_no_reading_changes_nothing(self) -> None:
        _ = self.watch([report("Claude", 99)])
        self.assertEqual(self.watch([report("Claude", None), Report("Codex", problem="Not logged in")]), [])
        self.assertTrue(five_hour.notice_path("five-hour-claude").exists())


class AccountHoldTests(unittest.TestCase):
    root: Path = Path()
    sent: list[str] = []
    now: datetime = datetime.now(timezone.utc)

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.sent = []
        self.now = datetime.now(timezone.utc)
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"HOME": str(self.root), "AGENT_NOTES_DIR": str(self.root)})))
        _ = self.enterContext(patch.object(escalate, "STATE", self.root / "holds"))
        _ = self.enterContext(patch.object(five_hour, "STATE", self.root / "notices"))
        _ = self.enterContext(patch.object(escalate, "schedule"))
        _ = self.enterContext(patch.object(escalate, "load_settings", return_value=CONFIG))
        _ = self.enterContext(patch.object(escalate, "registered_showrunners", return_value=SHOWRUNNERS))
        _ = self.enterContext(patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")))
        _ = self.enterContext(patch.object(five_hour, "load_config", return_value=CONFIG))
        _ = self.enterContext(patch.object(quota_alert, "registered_showrunners", return_value=SHOWRUNNERS))
        _ = self.enterContext(patch.object(five_hour, "relay", side_effect=self.relay))

    def relay(self, recipient: str, text: str, key: str | None) -> None:
        del recipient, key
        self.sent.append(text)

    def readings(self, number: int, used: float | None = 95) -> quota_alert.CodexAlertPassReadings:
        home = self.root / f".codex{number}"
        home.mkdir(exist_ok=True)
        login = f"a{number}@example.invalid"
        claims = base64.urlsafe_b64encode(json.dumps({"email": login}).encode()).decode().rstrip("=")
        _ = (home / "auth.json").write_text(json.dumps({"tokens": {"id_token": f"x.{claims}.y"}}))
        _ = (self.root / f"codex {number}.md").write_text(f"---\nlogin: {login}\n---\n")
        self.assertEqual(accounts.main(["set-role", "seats", f"codex {number}"]), 0)
        identity = KnownCodexAccount(home, f"a{number}@example.invalid", f"codex {number}")
        live = Report("Codex", email=identity.login, source=CodexHomeSource(home),
            five_hour=None if used is None else Quota("5-hour", used, self.now + timedelta(hours=2)))
        return quota_alert.CodexAlertPassReadings(accounts.role_account("seats"), identity,
            (quota_alert.CodexHomeReading(home, live, self.now),))

    def test_swap_cancels_outgoing_and_legacy_holds_without_recovery_and_keeps_other_tools(self) -> None:
        a = self.readings(1)
        _ = five_hour.watch([a.checked_homes[0].report], codex_readings=a)
        _ = escalate.hold("five-hour-codex", escalate.SessionPush("Old Codex", "old", NoActionRequired(), "note"))
        _ = escalate.hold("five-hour-claude", escalate.SessionPush("Claude", "other", NoActionRequired(), "note"))
        self.sent.clear()
        b = self.readings(2, None)
        _ = five_hour.watch([b.checked_homes[0].report], codex_readings=b)
        self.assertEqual(self.sent, [])
        held = [escalate.read(path) for path in (escalate.STATE / "held").glob("*.json")]
        current = [entry for entry in held if isinstance(entry, escalate.HeldMessage)]
        self.assertEqual([entry.summary for entry in current], ["Claude"])
        low_b = self.readings(2)
        _ = five_hour.watch([low_b.checked_homes[0].report], codex_readings=low_b)
        self.assertTrue(self.sent)
        self.assertTrue(all("codex 2" in text for text in self.sent))

    def test_known_swap_retires_an_offline_relay_even_without_a_usage_reading(self) -> None:
        with (patch.object(send, "STATE", self.root / "messages"),
              patch.object(sessions, "live_sessions", return_value=[])):
            def queue(recipient: str, text: str, key: str | None) -> str:
                send.enqueue(send.Message(recipient, "quota_alert", "", text, key), "offline", self.now)
                return "offline"
            a = self.readings(1)
            with patch.object(five_hour, "relay", side_effect=queue):
                _ = five_hour.watch([a.checked_homes[0].report], codex_readings=a)
            self.assertTrue(send.read_queue(send.queue_path("hana")))
            chosen = self.readings(2, None)
            b = quota_alert.CodexAlertPassReadings(chosen.seats, chosen.selected_account, ())
            def retire(command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
                _ = send.retire(command[command.index("--to") + 1], "quota_alert", command[command.index("--key") + 1])
                return subprocess.CompletedProcess(command, 0, "", "")
            with patch.object(subprocess, "run", side_effect=retire):
                self.assertEqual(five_hour.watch([], codex_readings=b), [])
            self.assertEqual(send.read_queue(send.queue_path("hana")), [])

    def test_unknown_selected_identity_preserves_warning_records_and_sends_nothing(self) -> None:
        a = self.readings(1)
        _ = five_hour.watch([a.checked_homes[0].report], codex_readings=a)
        before = {path.name: path.read_text() for path in five_hour.STATE.glob("*.notice")}
        self.sent.clear()
        b = self.readings(2, None)
        unknown = quota_alert.CodexAlertPassReadings(b.seats,
            UnknownCodexAccount(b.seats.home, "unreadable login"), b.checked_homes)
        _ = five_hour.watch([], codex_readings=unknown)
        self.assertEqual(self.sent, [])
        self.assertTrue(before)
        self.assertEqual({path.name: path.read_text() for path in five_hour.STATE.glob("*.notice")}, before)

    def test_spare_used_up_window_is_not_relayed_or_held(self) -> None:
        a = self.readings(1, 10)
        spare = Report("Codex", email="spare@example.invalid", source=CodexHomeSource(self.root / ".codex2"),
            five_hour=Quota("5-hour", 100, self.now + timedelta(hours=1)), credit_balance="0")
        readings = quota_alert.CodexAlertPassReadings(a.seats, a.selected_account,
            (*a.checked_homes, quota_alert.CodexHomeReading(spare.source.home, spare, self.now))
            if isinstance(spare.source, CodexHomeSource) else a.checked_homes)
        _ = five_hour.watch([a.checked_homes[0].report, spare], codex_readings=readings)
        self.assertEqual(self.sent, [])
        self.assertFalse(list((escalate.STATE / "held").glob("*.json")))


class ReaderTests(unittest.TestCase):
    def test_claude_five_hour_window(self) -> None:
        usage: dict[str, object] = {"five_hour": {"utilization": 41.0, "resets_at": RESETS.isoformat()},
                                    "seven_day": {"utilization": 78.0, "resets_at": None}}
        self.assertEqual(agent_accounts.claude_five_hour(usage), Quota("5-hour", 41.0, RESETS))
        self.assertIsNone(agent_accounts.claude_five_hour({"seven_day": {"utilization": 78.0}}))

    def test_codex_five_hour_window(self) -> None:
        week = {"usedPercent": 60.0, "windowDurationMins": 10080, "resetsAt": None}
        usage = cast(CodexRateLimits, cast(object, {"rateLimitsByLimitId": {
            "codex": {"primary": {"usedPercent": 12.0, "windowDurationMins": 300, "resetsAt": int(RESETS.timestamp())},
                      "secondary": week},
            "other": {"primary": {"usedPercent": 99.0, "windowDurationMins": 300, "resetsAt": None}}}}))
        self.assertEqual(agent_accounts.codex_five_hour(usage), Quota("5-hour", 12.0, RESETS))
        self.assertIsNone(agent_accounts.codex_five_hour(cast(CodexRateLimits, cast(object, {
            "rateLimits": {"primary": week, "secondary": None}}))))


if __name__ == "__main__":
    _ = unittest.main()
