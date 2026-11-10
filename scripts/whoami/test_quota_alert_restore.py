"""Quota alerts: restore."""

from __future__ import annotations

import unittest
import json
import shutil
from datetime import timedelta
from typing import cast
from scripts.whoami import quota_alert_test_support as support
from scripts.whoami import quota_alert

setUpModule = support.setUpModule


class QuotaAlertTests(support.SeededQuotaReminderFixture):
    def test_selected_allowance_restores_assignments_despite_low_weekly_episode(self) -> None:
        for enabled in (True, False):
            with self.subTest(no_worker_mode=enabled):
                shutil.rmtree(self.root)
                _ = shutil.copytree(self.seed, self.root)
                _ = self.alert([self.note("codex1.md", "active", "0")], self.now)
                _ = self.refuse()
                saved = cast(quota_alert.StoredState, json.loads(quota_alert.STATE.read_text()))
                saved["no_worker_mode"] = enabled
                _ = quota_alert.STATE.write_text(json.dumps(saved))
                self.broadcasts.clear()
                self.sent.clear()
                log = self.alert([self.note("codex1.md", "inactive", "0"), self.note("codex2.md", "active", "1")],
                                        self.now + timedelta(minutes=2))
                self.assertIn("switch to claude undone: moved back from Claude to Codex: direct, design_roles", log)
                self.assertEqual(self.assignments()["direct"], "codex")
                self.assertFalse(any(text.startswith("Quota alert:") for _, text in self.sent))
                self.assertEqual(self.sent, [])
                self.assertEqual(self.routing_entries()[-1]["kind"], "routed back")
                _ = self.alert([self.note("codex1.md", "inactive", "0"), self.note("codex2.md", "active", "90")],
                                      self.now + timedelta(minutes=4))
                self.assertEqual(self.assignments()["direct"], "codex")
                self.assertEqual(len(self.broadcasts), int(enabled))
                if enabled:
                    self.assertIn("Use workers again.", self.broadcasts[0][3])
                with quota_alert.state_file() as state:
                    self.assertFalse(state.get("no_worker_mode", False))

    def test_refresh_restores_assignments_and_records_recovery_without_session_delivery(self) -> None:
        for enabled in (False, True):
            with self.subTest(no_worker_mode=enabled):
                shutil.rmtree(self.root)
                _ = shutil.copytree(self.seed, self.root)
                _ = self.alert([self.note("codex1.md", "active", "0")], self.now)
                _ = self.refuse()
                saved = cast(quota_alert.StoredState, json.loads(quota_alert.STATE.read_text()))
                saved["no_worker_mode"] = enabled
                _ = quota_alert.STATE.write_text(json.dumps(saved))
                self.broadcasts.clear()
                self.sent.clear()
                lines = self.refresh([self.note("codex1.md", "active", "100")], "natedev",
                                     self.now + timedelta(minutes=3))
                self.assertIn("switch to claude undone: moved back from Claude to Codex: direct, design_roles", lines)
                self.assertEqual(self.assignments()["design_roles"], "codex")
                self.assertEqual(self.sent, [])
                self.assertEqual([row["kind"] for row in self.routing_entries()], ["routed to claude", "routed back"])
                self.assertEqual(len(self.broadcasts), int(enabled))
                if enabled:
                    self.assertIn("notes refreshed; workers-back notice sent", lines)
                    self.assertIn("Use workers again.", self.broadcasts[0][3])
                else:
                    self.assertIn("notes refreshed; nobody was told", lines)

    def test_notice_shows_each_switch_once_and_only_in_the_owner_session(self) -> None:
        self.assertIsNone(quota_alert.notice(lambda: "natedev"))
        _ = self.alert([self.note("codex1.md", "active", "0")], self.now)
        _ = self.refuse()
        self.assertIsNone(quota_alert.notice(lambda: "boss of bosses"))
        text = quota_alert.notice(lambda: "natedev")
        assert text is not None
        self.assertTrue(text.startswith(f"Codex refused work for quota (codex1 at 0%, resets {self.resets}): direct, "
                                        + "design_roles moved to Claude at "))
        self.assertIsNone(quota_alert.notice(lambda: "natedev"))
        _ = self.alert([self.note("codex1.md", "active", "100")], self.now + timedelta(hours=1))
        text = quota_alert.notice(lambda: "natedev")
        assert text is not None
        self.assertTrue(text.startswith("Codex recovered at "))
        self.assertTrue(text.endswith(": moved back from Claude to Codex: direct, design_roles."))


if __name__ == "__main__":
    _ = unittest.main()
