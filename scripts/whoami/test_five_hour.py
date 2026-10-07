"""Tests for the 5-hour limit watch: who is told, once per low spell, and what is held for the user."""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import cast, override
from unittest.mock import patch

import agent_accounts
import five_hour
from agent_accounts import CodexRateLimits, Quota, Report

import escalate  # five_hour puts scripts/message on the path
from quota_alert import Config

RESETS = datetime(2026, 10, 7, 21, 20, tzinfo=timezone.utc)
CONFIG = cast(Config, cast(object, {
    "threshold_percent": 2, "repeat_minutes": 30, "stall_minutes": 5, "faults_to": "hana", "always": ["natedev"],
    "showrunners": [{"session": "hana", "zone": "America/Los_Angeles", "units": []}],
}))


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

        for module, name, stand_in in ((five_hour, "relay", relay), (five_hour, "load_config", config),
                                       (escalate, "schedule", schedule), (escalate, "STATE", self.state)):
            _ = self.enterContext(patch.object(module, name, stand_in))

    def test_a_low_window_tells_the_alert_list_once_and_holds_it_for_the_user(self) -> None:
        lines = five_hour.watch([report("Claude", 82.4), report("Codex", 10)])
        self.assertEqual(lines, ["5-hour limit: Claude 18% left -> natedev",
                                 "5-hour limit: Claude 18% left -> hana: NOT sent: QUEUED: no live session"])
        self.assertEqual([(name, key) for name, _, key in self.sent],
                         [("natedev", "5-hour Claude"), ("hana", "5-hour Claude")])
        self.assertTrue(self.sent[0][1].startswith(
            "5-hour limit: Claude has 18% of its 5-hour limit left; it resets at 14:20 PDT.\nTell the user"))
        held = escalate.read(escalate.held_path("five-hour-claude"))
        assert held is not None
        self.assertEqual((held["summary"], held["text"], held["minutes"], held["need"], held["outcome"]),
                         ("Claude 5-hour limit", "Claude has 18% of its 5-hour limit left; it resets at 14:20 PDT.",
                          15, "decision", "waiting"))
        self.assertEqual(five_hour.watch([report("Claude", 90)]), [])
        self.assertEqual(len(self.sent), 2)

    def test_a_window_back_above_the_threshold_closes_the_hold_and_the_next_low_spell_is_new(self) -> None:
        _ = five_hour.watch([report("Codex", 95)])
        self.assertEqual(five_hour.watch([report("Codex", 3)]), ["5-hour limit: Codex is back above 20%"])
        self.assertIsNone(escalate.read(escalate.held_path("five-hour-codex")))
        self.assertEqual(five_hour.watch([report("Codex", 3)]), [])
        self.assertEqual(len(five_hour.watch([report("Codex", 80)])), 2)

    def test_no_reading_changes_nothing(self) -> None:
        _ = five_hour.watch([report("Claude", 99)])
        self.assertEqual(five_hour.watch([report("Claude", None), Report("Codex", problem="Not logged in")]), [])
        self.assertIsNotNone(escalate.read(escalate.held_path("five-hour-claude")))


class ReaderTests(unittest.TestCase):
    def test_claude_five_hour_window(self) -> None:
        usage: dict[str, object] = {"five_hour": {"utilization": 41.0, "resets_at": "2026-10-07T21:20:00+00:00"},
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
