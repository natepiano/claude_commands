"""Tests for the saved usage readings."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast, final, override
from unittest.mock import patch

import usage_store
from agent_accounts import Quota, Report

READ = datetime(2026, 10, 12, 14, 30, tzinfo=timezone.utc)
RESETS = datetime(2026, 10, 15, 16, 0, tzinfo=timezone.utc)


@final
class UsageStoreTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.home = Path()

    @override
    def setUp(self) -> None:
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        environment: object = cast(object, self.enterContext(patch.dict(os.environ, {"HOME": str(self.home)})))
        del environment

    def test_save_keeps_both_windows_under_the_login_in_lower_case(self) -> None:
        usage_store.save([Report(
            "Claude", email="Main@Example.invalid",
            quotas=[Quota("Weekly (Opus)", 1.0, None), Quota("Weekly", 29.5, RESETS)],
            five_hour=Quota("5-hour", 36.0, READ + timedelta(hours=2)),
        )], READ)

        self.assertEqual(usage_store.load(), {"main@example.invalid": {
            "five_hour": {"left": 64.0, "resets": "2026-10-12T16:30:00+00:00", "at": "2026-10-12T14:30:00+00:00"},
            "weekly": {"left": 70.5, "resets": "2026-10-15T16:00:00+00:00", "at": "2026-10-12T14:30:00+00:00"},
        }})
        self.assertEqual(usage_store.store_file(), self.home / ".local/state/agent-notes/usage.json")

    def test_save_with_labels_writes_each_label_s_readings_and_no_login(self) -> None:
        usage_store.save([Report(
            "Claude", email="Main@Example.invalid", quotas=[Quota("Weekly", 98.0, RESETS)],
            five_hour=Quota("5-hour", 36.0, READ + timedelta(hours=2)),
        )], READ, {"claude 2": "main@example.invalid", "claude 3": "spare@example.invalid",
                   "main@example.invalid": "main@example.invalid"})

        text = usage_store.label_file().read_text(encoding="utf-8")
        self.assertEqual(json.loads(text), {"claude 2": {
            "five_hour": {"left": 64.0, "resets_at": int((READ + timedelta(hours=2)).timestamp()),
                          "at": int(READ.timestamp())},
            "weekly": {"left": 2.0, "resets_at": int(RESETS.timestamp()), "at": int(READ.timestamp())},
        }})
        self.assertNotIn("@", text)
        self.assertEqual(usage_store.label_file(), self.home / ".local/state/agent-notes/by-label.json")

    def test_a_refused_read_keeps_the_label_s_last_reading(self) -> None:
        logins = {"claude 2": "main@example.invalid"}
        usage_store.save([Report("Claude", email="main@example.invalid", quotas=[Quota("Weekly", 10.0, RESETS)])],
                         READ, logins)
        usage_store.save([Report("Claude", email="main@example.invalid", problem="HTTP 429")],
                         READ + timedelta(minutes=2), logins)

        held = cast(dict[str, dict[str, dict[str, float]]], json.loads(usage_store.label_file().read_text(encoding="utf-8")))
        self.assertEqual(held["claude 2"]["weekly"]["at"], int(READ.timestamp()))

    def test_a_none_open_five_hour_window_has_no_reset(self) -> None:
        usage_store.save([Report("Claude", email="main@example.invalid", quotas=[Quota("Weekly", 10.0, RESETS)])],
                         READ, {"claude 2": "main@example.invalid"})

        held = cast(dict[str, dict[str, dict[str, object]]], json.loads(usage_store.label_file().read_text(encoding="utf-8")))
        self.assertEqual(held["claude 2"]["five_hour"], {"left": 100.0, "resets_at": None, "at": int(READ.timestamp())})

    def test_a_read_with_no_five_hour_window_stores_all_of_it_left(self) -> None:
        usage_store.save([Report("Claude", email="main@example.invalid", quotas=[Quota("Weekly", 10.0, RESETS)])], READ)

        self.assertEqual(
            usage_store.load()["main@example.invalid"].get("five_hour"),
            {"left": 100.0, "resets": "", "at": "2026-10-12T14:30:00+00:00"},
        )

    def test_a_codex_login_keeps_its_credits_apart_from_a_claude_login_at_the_same_address(self) -> None:
        usage_store.save([
            Report("Claude", email="Main@Example.invalid", quotas=[Quota("Weekly", 10.0, RESETS)]),
            Report("Codex", email="Main@Example.invalid", quotas=[Quota("Weekly", 100.0, RESETS)],
                   five_hour=Quota("5-hour", 25.0, READ + timedelta(hours=1)), credit_balance="13446"),
        ], READ)

        stored = usage_store.load()

        self.assertEqual(sorted(stored), ["codex:main@example.invalid", "main@example.invalid"])
        self.assertNotIn("credits", stored["main@example.invalid"])
        self.assertEqual(stored["codex:main@example.invalid"], {
            "five_hour": {"left": 75.0, "resets": "2026-10-12T15:30:00+00:00", "at": "2026-10-12T14:30:00+00:00"},
            "weekly": {"left": 0.0, "resets": "2026-10-15T16:00:00+00:00", "at": "2026-10-12T14:30:00+00:00"},
            "credits": {"balance": "13446", "at": "2026-10-12T14:30:00+00:00"},
        })

    def test_a_codex_account_with_no_five_hour_window_stores_none(self) -> None:
        with_window = Report("Codex", email="main@example.invalid", quotas=[Quota("Weekly", 100.0, RESETS)],
                             five_hour=Quota("5-hour", 25.0, READ + timedelta(hours=1)), credit_balance="unlimited")
        without = Report("Codex", email="main@example.invalid", quotas=[Quota("Weekly", 81.0, RESETS)],
                         credit_balance="unlimited")
        usage_store.save([with_window], READ)

        usage_store.save([without], READ + timedelta(minutes=2))

        self.assertEqual(usage_store.load()["codex:main@example.invalid"], {
            "weekly": {"left": 19.0, "resets": "2026-10-15T16:00:00+00:00", "at": "2026-10-12T14:32:00+00:00"},
            "credits": {"balance": "unlimited", "at": "2026-10-12T14:32:00+00:00"},
        })

    def test_a_refused_read_changes_nothing(self) -> None:
        usage_store.save([Report("Claude", email="main@example.invalid", quotas=[Quota("Weekly", 10.0, RESETS)])], READ)
        before = usage_store.store_file().read_text(encoding="utf-8")

        usage_store.save([
            Report("Claude", email="main@example.invalid",
                   quota_problem="Weekly quota: unavailable (HTTP 429; try Claude /usage)"),
            Report("Claude", problem="Not logged in"),
            Report("Codex", email="codex@example.invalid",
                   quota_problem="Weekly quota: unavailable (Codex usage API failed)"),
        ], READ + timedelta(minutes=2))

        self.assertEqual(usage_store.store_file().read_text(encoding="utf-8"), before)

    def test_load_drops_what_it_cannot_read(self) -> None:
        self.assertEqual(usage_store.load(), {})
        path = usage_store.store_file()
        path.parent.mkdir(parents=True)
        for contents in ("not json", "[]"):
            _ = path.write_text(contents, encoding="utf-8")
            self.assertEqual(usage_store.load(), {})
        _ = path.write_text(json.dumps({
            "a@example.invalid": {"weekly": {"left": True, "resets": "", "at": ""}, "five_hour": "junk",
                                  "credits": {"balance": 5, "at": ""}},
            "b@example.invalid": "junk",
        }), encoding="utf-8")
        self.assertEqual(usage_store.load(), {"a@example.invalid": {}})
