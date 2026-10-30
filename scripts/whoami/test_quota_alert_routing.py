"""Quota alerts: routing."""

from __future__ import annotations

from scripts.lib.test_agents_registry import write_registry

import json
import unittest
from datetime import timedelta
from typing import cast
from scripts.whoami import quota_alert_test_support as support
import quota_alert
from scripts.whoami import agent_accounts
from scripts.whoami.quota_alert_test_support import Notes, REGISTRY as REGISTRY

setUpModule = support.setUpModule


class QuotaAlertTests(support.SeededQuotaReminderFixture):
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
        _ = write_registry(registry, {"assignments": {"direct": "claude", "module_review": "claude"}}, base_text=REGISTRY)
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


if __name__ == "__main__":
    _ = unittest.main()
