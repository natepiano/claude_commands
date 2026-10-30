"""Quota alerts: restore."""

from __future__ import annotations

import unittest
from datetime import timedelta
from scripts.whoami import quota_alert_test_support as support
import quota_alert

setUpModule = support.setUpModule


class QuotaAlertTests(support.SeededQuotaReminderFixture):
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


if __name__ == "__main__":
    _ = unittest.main()
