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

    def test_a_read_with_no_five_hour_window_stores_all_of_it_left(self) -> None:
        usage_store.save([Report("Claude", email="main@example.invalid", quotas=[Quota("Weekly", 10.0, RESETS)])], READ)

        self.assertEqual(
            usage_store.load()["main@example.invalid"].get("five_hour"),
            {"left": 100.0, "resets": "", "at": "2026-10-12T14:30:00+00:00"},
        )

    def test_a_refused_read_and_other_tools_change_nothing(self) -> None:
        usage_store.save([Report("Claude", email="main@example.invalid", quotas=[Quota("Weekly", 10.0, RESETS)])], READ)
        before = usage_store.store_file().read_text(encoding="utf-8")

        usage_store.save([
            Report("Claude", email="main@example.invalid",
                   quota_problem="Weekly quota: unavailable (HTTP 429; try Claude /usage)"),
            Report("Claude", problem="Not logged in"),
            Report("Codex", email="codex@example.invalid", quotas=[Quota("Weekly", 50.0, RESETS)]),
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
            "a@example.invalid": {"weekly": {"left": True, "resets": "", "at": ""}, "five_hour": "junk"},
            "b@example.invalid": "junk",
        }), encoding="utf-8")
        self.assertEqual(usage_store.load(), {"a@example.invalid": {}})
