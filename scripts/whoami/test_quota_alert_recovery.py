"""Quota alerts: recovery."""

from __future__ import annotations

import unittest
from datetime import timedelta
from typing import cast
from scripts.whoami import quota_alert_test_support as support
from scripts.whoami import quota_alert

setUpModule = support.setUpModule


class QuotaAlertTests(support.SeededQuotaReminderFixture):
    def test_recovery_moves_back_only_what_is_still_on_claude(self) -> None:
        _ = self.alert([self.note("codex1.md", "active", "0")], self.now)
        _ = self.refuse()
        self.assertIsNone(quota_alert.edit_registry("agents_set_assignment", "design_roles", "codex"))
        self.sent.clear()
        log = self.alert([self.note("codex1.md", "active", "100")], self.now + timedelta(hours=1))
        self.assertIn("switch to claude undone: moved back from Claude to Codex: direct; left alone, "
                      + "re-assigned since the switch: design_roles", log)
        self.assertEqual(self.assignments(),
                         {"direct": "codex", "cli": "claude", "design_roles": "codex", "ask_a_friend": "caller"})
        self.assertEqual(self.sent, [])
        self.assertEqual(self.routing_entries()[-1]["kind"], "routed back")

    def test_fallback_recovery_after_state_reload_keeps_its_pairing_time_and_journals_once(self) -> None:
        notes = [self.note("codex1.md", "active", "0")]
        _ = self.block(notes, self.now)
        _ = self.block(notes, self.now)
        opened = self.routing_entries()
        self.assertEqual(len(opened), 1)
        self.assertEqual((opened[0]["kind"], opened[0]["owner"], opened[0]["cause"]),
                         ("routed to claude", "codex1", "refusal"))
        self.assertEqual(set(cast(list[str], opened[0]["moved"])), {"direct", "design_roles"})
        # Reload through the persisted updater state, then recover in another pass.
        with quota_alert.state_file() as state:
            fallback = state["fallback"]
            self.assertIsInstance(fallback, quota_alert.AccountCodexQuotaFallback)
            assert isinstance(fallback, quota_alert.AccountCodexQuotaFallback)
            self.assertEqual(fallback.at, opened[0]["fallback_at"])
        _ = quota_alert.edit_registry("agents_set_assignment", "design_roles", "codex")
        later = self.now + timedelta(hours=1)
        _ = self.alert([self.note("codex1.md", "active", "100")], later)
        _ = self.alert([self.note("codex1.md", "active", "100")], later)
        entries = self.routing_entries()
        self.assertEqual([entry["kind"] for entry in entries], ["routed to claude", "routed back"])
        self.assertEqual(entries[0]["fallback_at"], entries[1]["fallback_at"])
        self.assertEqual(entries[1]["restored"], ["direct"])
        self.assertEqual(entries[1]["kept"], ["design_roles"])
        self.assertNotIn("move_id", entries[1])


if __name__ == "__main__":
    _ = unittest.main()
