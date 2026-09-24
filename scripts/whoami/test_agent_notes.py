"""Account attribution and stale-data behavior of the vault writer."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent_accounts import Quota, Report, reset_credits
from agent_notes import apply, local_reset, read_note


class AgentNotesTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.future = self.now + timedelta(days=2)

    def note(self, name, email, extra=""):
        path = Path(self.directory.name) / name
        path.write_text(f'---\nlogin: {email}\nstate: inactive\n{extra}---\nBody stays.\n')
        path.chmod(0o644)
        return read_note(path)

    def test_live_percentage_is_written_only_to_matching_tool_and_email(self):
        active = self.note("claude 1.md", "a@example.com")
        other = self.note("claude 2.md", "b@example.com")
        codex = self.note("codex 1.md", "a@example.com")
        original_codex = codex.path.read_text()
        report = Report("Claude", email="A@example.com", quotas=[Quota("Weekly", 99, self.future)])
        apply([active, other, codex], report, self.now)
        self.assertEqual(active.get("weekly_remaining_usage"), "1")
        self.assertEqual(active.get("state"), "active")
        self.assertEqual(active.get("resets"), local_reset(self.future))
        self.assertEqual(other.get("weekly_remaining_usage"), "null")
        self.assertEqual(codex.path.read_text(), original_codex)
        self.assertTrue(active.path.read_text().endswith("---\nBody stays.\n"))
        self.assertEqual(active.path.stat().st_mode & 0o777, 0o644)

    def test_switch_preserves_inactive_observation_until_window_expires(self):
        old = self.note("codex 1.md", "old@example.com")
        new = self.note("codex 2.md", "new@example.com")
        apply([old, new], Report("Codex", email="old@example.com", quotas=[Quota("Weekly", 20, self.future)]), self.now)
        checked = old.get("weekly_usage_checked_at")
        apply([old, new], Report("Codex", email="new@example.com", quotas=[Quota("Weekly", 5, self.future)]), self.now)
        self.assertEqual(old.get("state"), "inactive")
        self.assertEqual(old.get("weekly_remaining_usage"), "80")
        self.assertEqual(old.get("weekly_usage_checked_at"), checked)
        self.assertEqual(new.get("weekly_remaining_usage"), "95")

    def test_expired_inactive_usage_is_unknown(self):
        past = local_reset(self.now - timedelta(days=1))
        note = self.note("claude 1.md", "old@example.com", f"resets: {past}\nweekly_remaining_usage: 30\n")
        apply([note], Report("Claude", email="new@example.com"), self.now)
        self.assertEqual(note.get("weekly_remaining_usage"), "null")
        self.assertEqual(note.get("resets"), past)

    def test_failed_usage_is_not_zero_and_does_not_get_a_fresh_timestamp(self):
        note = self.note("claude 1.md", "a@example.com")
        apply([note], Report("Claude", email="a@example.com", quotas=[Quota("Weekly", 100, self.future)]), self.now)
        self.assertEqual(note.get("weekly_remaining_usage"), "0")
        checked = note.get("weekly_usage_checked_at")
        apply([note], Report("Claude", email="a@example.com", quota_problem="Unavailable"), self.now + timedelta(minutes=2))
        self.assertEqual(note.get("weekly_remaining_usage"), "null")
        self.assertEqual(note.get("weekly_usage_checked_at"), checked)

    def test_unknown_identity_does_not_mark_account_inactive(self):
        note = self.note("codex 1.md", "a@example.com")
        apply([note], Report("Codex", email="a@example.com", quotas=[Quota("Weekly", 5, self.future)]), self.now)
        original = note.path.read_text()
        apply([note], Report("Codex", problem="Unavailable"), self.now)
        self.assertEqual(note.path.read_text(), original)

    def test_reset_expiration_uses_eastern_date_and_summary_count(self):
        note = self.note("codex 1.md", "a@example.com")
        report = Report("Codex", email="a@example.com")
        reset_credits(report, {"availableCount": 2, "credits": [
            {"status": "available", "expiresAt": "2026-10-23T01:00:00Z"},
            {"status": "redeemed", "expiresAt": "2026-10-01T01:00:00Z"},
        ]})
        apply([note], report, self.now)
        self.assertEqual(note.get("limit_reset_count"), "2")
        self.assertEqual(note.get("limit_reset"), "2026-10-22T21:00:00-04:00")
        # An unavailable API answer must not erase a known reset.
        apply([note], Report("Codex", email="a@example.com"), self.now)
        self.assertEqual(note.get("limit_reset_count"), "2")
        reset_credits(report, {"availableCount": 0, "credits": []})
        apply([note], report, self.now)
        self.assertEqual(note.get("limit_reset_count"), "0")
        self.assertEqual(note.get("limit_reset"), "null")

    def test_claude_manual_reset_fields_are_preserved(self):
        note = self.note("claude 1.md", "a@example.com", "limit_reset: 2026-10-22\nlimit_reset_count: 1\n")
        apply([note], Report("Claude", email="a@example.com"), self.now)
        self.assertEqual(note.get("limit_reset"), "2026-10-22")
        self.assertEqual(note.get("limit_reset_count"), "1")


if __name__ == "__main__":
    unittest.main()
