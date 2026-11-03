"""Quota alerts: routing repeat."""

from __future__ import annotations

import unittest
from datetime import timedelta
from scripts.whoami import quota_alert_test_support as support
from scripts.whoami.quota_alert_test_support import Notes

setUpModule = support.setUpModule


class QuotaAlertTests(support.SeededQuotaReminderFixture):
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


if __name__ == "__main__":
    _ = unittest.main()
