"""Account attribution and stale-data behavior of the vault writer."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import override
from unittest.mock import patch

import agent_notes
from agent_accounts import CodexRateLimits, Quota, Report, codex_credits, reset_credits
from agent_notes import Note, apply, local_reset, read_note


class AgentNotesTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root: Path = Path()
        self.now: datetime = datetime.now(timezone.utc).replace(microsecond=0)
        self.future: datetime = self.now + timedelta(days=2)

    @override
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        _ = self.enterContext(patch.object(agent_notes, "READINGS_LOG", self.root / "readings.jsonl", create=True))

    def note(self, name: str, email: str, extra: str = "") -> Note:
        path = self.root / name
        _ = path.write_text(f'---\nlogin: {email}\nstate: inactive\n{extra}---\nBody stays.\n')
        path.chmod(0o644)
        note = read_note(path)
        if note is None:
            self.fail(f"{name} did not parse as a note")
        return note

    def test_live_percentage_is_written_only_to_matching_tool_and_email(self):
        active = self.note("claude 1.md", "a@example.com")
        other = self.note("claude 2.md", "b@example.com")
        codex = self.note("codex 1.md", "a@example.com")
        original_codex = codex.path.read_text()
        report = Report("Claude", email="A@example.com", quotas=[Quota("Weekly", 99, self.future)])
        _ = apply([active, other, codex], report, self.now)
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
        _ = apply([old, new], Report("Codex", email="old@example.com", quotas=[Quota("Weekly", 20, self.future)]), self.now)
        checked = old.get("weekly_usage_checked_at")
        _ = apply([old, new], Report("Codex", email="new@example.com", quotas=[Quota("Weekly", 5, self.future)]), self.now)
        self.assertEqual(old.get("state"), "inactive")
        self.assertEqual(old.get("weekly_remaining_usage"), "80")
        self.assertEqual(old.get("weekly_usage_checked_at"), checked)
        self.assertEqual(new.get("weekly_remaining_usage"), "95")

    def test_expired_inactive_usage_is_unknown(self):
        past = local_reset(self.now - timedelta(days=1))
        note = self.note("claude 1.md", "old@example.com", f"resets: {past}\nweekly_remaining_usage: 30\n")
        _ = apply([note], Report("Claude", email="new@example.com"), self.now)
        self.assertEqual(note.get("weekly_remaining_usage"), "null")
        self.assertEqual(note.get("resets"), past)

    def test_failed_usage_is_not_zero_and_does_not_get_a_fresh_timestamp(self):
        note = self.note("claude 1.md", "a@example.com")
        _ = apply([note], Report("Claude", email="a@example.com", quotas=[Quota("Weekly", 100, self.future)]), self.now)
        self.assertEqual(note.get("weekly_remaining_usage"), "0")
        checked = note.get("weekly_usage_checked_at")
        _ = apply([note], Report("Claude", email="a@example.com", quota_problem="Unavailable"), self.now + timedelta(minutes=2))
        self.assertEqual(note.get("weekly_remaining_usage"), "null")
        self.assertEqual(note.get("weekly_usage_checked_at"), checked)

    def test_unknown_identity_does_not_mark_account_inactive(self):
        note = self.note("codex 1.md", "a@example.com")
        _ = apply([note], Report("Codex", email="a@example.com", quotas=[Quota("Weekly", 5, self.future)]), self.now)
        original = note.path.read_text()
        _ = apply([note], Report("Codex", problem="Unavailable"), self.now)
        self.assertEqual(note.path.read_text(), original)

    def test_reset_expiration_uses_eastern_date_and_summary_count(self):
        note = self.note("codex 1.md", "a@example.com")
        report = Report("Codex", email="a@example.com")
        reset_credits(report, {"availableCount": 2, "credits": [
            {"status": "available", "expiresAt": "2026-10-23T01:00:00Z"},
            {"status": "redeemed", "expiresAt": "2026-10-01T01:00:00Z"},
        ]})
        _ = apply([note], report, self.now)
        self.assertEqual(note.get("limit_reset_count"), "2")
        self.assertEqual(note.get("limit_reset"), "2026-10-22T21:00:00-04:00")
        # An unavailable API answer must not erase a known reset.
        _ = apply([note], Report("Codex", email="a@example.com"), self.now)
        self.assertEqual(note.get("limit_reset_count"), "2")
        reset_credits(report, {"availableCount": 0, "credits": []})
        _ = apply([note], report, self.now)
        self.assertEqual(note.get("limit_reset_count"), "0")
        self.assertEqual(note.get("limit_reset"), "null")

    def test_credit_balance_is_read_from_the_main_bucket(self):
        def usage(credits: object) -> CodexRateLimits:
            return {"rateLimits": {"credits": credits}}  # pyright: ignore[reportReturnType]

        self.assertEqual(codex_credits(usage({"hasCredits": True, "unlimited": False,
                                              "balance": "60498.0534770000"})), "60498")
        self.assertEqual(codex_credits(usage({"hasCredits": False, "unlimited": False, "balance": None})), "0")
        self.assertEqual(codex_credits(usage({"hasCredits": True, "unlimited": True, "balance": None})), "unlimited")
        self.assertEqual(codex_credits(usage({"hasCredits": True, "unlimited": False, "balance": None})), "null")
        self.assertEqual(codex_credits(usage(None)), "null")
        self.assertEqual(codex_credits({}), "null")

    def test_credit_balance_is_written_and_an_unread_one_is_kept(self):
        note = self.note("codex 1.md", "a@example.com")
        _ = apply([note], Report("Codex", email="a@example.com", credit_balance="60498"), self.now)
        self.assertEqual(note.get("credit_balance"), "60498")
        # A failed read must not erase the last balance seen.
        _ = apply([note], Report("Codex", email="a@example.com"), self.now)
        self.assertEqual(note.get("credit_balance"), "60498")
        _ = apply([note], Report("Codex", email="a@example.com", credit_balance="null"), self.now)
        self.assertEqual(note.get("credit_balance"), "null")

    def test_claude_manual_reset_fields_are_preserved(self):
        note = self.note("claude 1.md", "a@example.com", "limit_reset: 2026-10-22\nlimit_reset_count: 1\n")
        _ = apply([note], Report("Claude", email="a@example.com"), self.now)
        self.assertEqual(note.get("limit_reset"), "2026-10-22")
        self.assertEqual(note.get("limit_reset_count"), "1")

    def test_reading_for_each_active_value_and_none_for_null_or_inactive(self):
        checked = datetime(2026, 10, 5, 19, 45, tzinfo=timezone.utc)
        first = self.note("claude 1.md", "a@example.com")
        second = self.note("claude 2.md", "a@example.com")
        inactive = self.note("claude 3.md", "b@example.com")
        weekly = Quota("Weekly", 40, checked + timedelta(days=2))
        _ = apply([second, inactive, first], Report("Claude", email="a@example.com", quotas=[weekly]), checked)
        path = self.root / "readings.jsonl"
        records = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertCountEqual(records, [
            {"account": "claude 1", "at": "2026-10-05T19:45:00+00:00", "remaining": 60},
            {"account": "claude 2", "at": "2026-10-05T19:45:00+00:00", "remaining": 60},
        ])
        _ = apply([first, second, inactive], Report("Claude", email="a@example.com"), checked + timedelta(minutes=2))
        self.assertEqual(len(path.read_text().splitlines()), 2)
        self.assertEqual(inactive.get("state"), "inactive")

    def test_reading_log_keeps_only_the_last_eight_days(self):
        checked = datetime(2026, 10, 5, 19, 45, tzinfo=timezone.utc)
        note = self.note("claude 1.md", "a@example.com")
        path = self.root / "readings.jsonl"
        old = {"account": "claude 1", "at": "2026-09-27T19:44:59+00:00", "remaining": 70}
        recent = {"account": "claude 1", "at": "2026-09-27T19:45:00+00:00", "remaining": 65}
        _ = path.write_text(json.dumps(old) + "\n" + json.dumps(recent) + "\n")
        _ = apply([note], Report("Claude", email="a@example.com", quotas=[
            Quota("Weekly", 40, checked + timedelta(days=2))
        ]), checked)
        records = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(records, [recent, {
            "account": "claude 1", "at": "2026-10-05T19:45:00+00:00", "remaining": 60,
        }])

    def test_unwritable_log_reports_once_and_keeps_note_write(self):
        checked = datetime(2026, 10, 5, 19, 45, tzinfo=timezone.utc)
        note = self.note("claude 1.md", "a@example.com")
        (self.root / "readings.jsonl").mkdir()
        messages = apply([note], Report("Claude", email="a@example.com", quotas=[
            Quota("Weekly", 40, checked + timedelta(days=2))
        ]), checked)
        self.assertEqual(note.get("weekly_remaining_usage"), "60")
        self.assertEqual(note.get("weekly_usage_checked_at"), "2026-10-05T19:45:00+00:00")
        self.assertEqual(len(messages), 2)
        self.assertEqual(sum("log" in message.lower() or "readings" in message.lower() for message in messages), 1)


if __name__ == "__main__":
    _ = unittest.main()
