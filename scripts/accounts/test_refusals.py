"""An observed refusal outranks saved room until its own recovery is observed."""
from __future__ import annotations

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast, override

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.accounts.home_test_support import scratch_account_home
from scripts.accounts import accounts, refusals
from scripts.whoami import usage_store


WEEKLY_REFUSAL_WINDOW = refusals.KnownWindow("weekly")
UNKNOWN_REFUSAL_RESET = refusals.NoResetKnown()


class AccountRefusalTests(unittest.TestCase):
    now: datetime = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)
    home: Path = Path()

    @override
    def setUp(self) -> None:
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = cast(object, self.enterContext(scratch_account_home(self.home, {}, clear=True)))

    def refusal(self, *, window: refusals.KnownWindow | refusals.UnknownWindow = WEEKLY_REFUSAL_WINDOW,
                reset: refusals.KnownReset | refusals.NoResetKnown = UNKNOWN_REFUSAL_RESET,
                login: str = "owner@example.invalid", tool: str = "claude") -> refusals.AccountRefusal:
        self.assertIn(tool, ("claude", "codex"))
        return refusals.AccountRefusal("codex" if tool == "codex" else "claude", login, window, reset, self.now)

    def reading(self, *, at: datetime, weekly: float = 80, five: float = 80,
                reset: datetime, tool: str = "claude", include_five: bool = True
                ) -> accounts.FreshReading | accounts.StaleReading | accounts.NoReading:
        usage = usage_store.StoredUsage(weekly=usage_store.StoredWindow(left=weekly,
            resets=reset.isoformat(), at=at.isoformat()))
        if include_five:
            usage["five_hour"] = usage_store.StoredWindow(left=five, resets=reset.isoformat(), at=at.isoformat())
        file = usage_store.store_file()
        file.parent.mkdir(parents=True, exist_ok=True)
        _ = file.write_text(json.dumps({usage_store.key(tool.capitalize(), "owner@example.invalid"): usage}))
        return refusals.stored_reading("codex" if tool == "codex" else "claude", "owner@example.invalid", self.now + timedelta(minutes=2))

    def test_refusal_round_trips_semantic_window_reset_and_timestamp(self) -> None:
        original = self.refusal(reset=refusals.KnownReset(self.now + timedelta(hours=3)))
        refusals.write_refusal(original)
        self.assertEqual(refusals.load(), {refusals.key("claude", original.login): original})
        self.assertEqual(refusals.store_file(), self.home / ".local/state/accounts/refusals.json")

    def test_refusal_unknown_window_and_reset_round_trip_without_bare_optionals(self) -> None:
        original = self.refusal(window=refusals.UnknownWindow())
        refusals.write_refusal(original)
        self.assertEqual(next(iter(refusals.load().values())), original)

    def test_refusal_store_keys_same_login_independently_for_each_tool(self) -> None:
        claude = self.refusal(login="OWNER@example.invalid")
        codex = self.refusal(tool="codex")
        refusals.write_refusal(claude)
        refusals.write_refusal(codex)
        self.assertEqual(len(refusals.load()), 2)
        self.assertEqual(refusals.load()[refusals.key("claude", "owner@example.invalid")], claude)
        self.assertEqual(refusals.load()[refusals.key("codex", "OWNER@example.invalid")], codex)

    def test_concurrent_refusals_preserve_each_accounts_record_under_the_lock(self) -> None:
        records = [self.refusal(login=f"account-{number}@example.invalid") for number in range(12)]
        with ThreadPoolExecutor(max_workers=4) as workers:
            _ = list(workers.map(refusals.write_refusal, records))
        self.assertEqual(set(refusals.load()), {refusals.key(item.tool, item.login) for item in records})

    def test_known_reset_resolves_at_its_reset_without_a_new_reading(self) -> None:
        reset = self.now + timedelta(minutes=15)
        refused = self.refusal(reset=refusals.KnownReset(reset))
        self.assertFalse(refusals.resolved(refused, accounts.NoReading(), reset - timedelta(microseconds=1)))
        self.assertTrue(refusals.resolved(refused, accounts.NoReading(), reset))

    def test_weekly_refusal_resolves_when_a_later_reading_moves_weekly_reset_later(self) -> None:
        reset = self.now + timedelta(hours=3)
        refused = self.refusal(reset=refusals.KnownReset(reset))
        reading = self.reading(at=self.now + timedelta(seconds=1), reset=reset + timedelta(days=7))
        self.assertTrue(refusals.resolved(refused, reading, self.now + timedelta(minutes=2)))

    def test_five_hour_refusal_resolves_when_a_later_reading_moves_five_hour_reset_later(self) -> None:
        reset = self.now + timedelta(hours=3)
        refused = self.refusal(window=refusals.KnownWindow("five_hour"), reset=refusals.KnownReset(reset))
        reading = self.reading(at=self.now + timedelta(seconds=1), reset=reset + timedelta(hours=5))
        self.assertTrue(refusals.resolved(refused, reading, self.now + timedelta(minutes=2)))

    def test_positive_reading_before_refusal_never_resolves_it(self) -> None:
        reset = self.now + timedelta(hours=3)
        refused = self.refusal(reset=refusals.KnownReset(reset))
        reading = self.reading(at=self.now - timedelta(seconds=1), reset=reset + timedelta(days=7))
        self.assertFalse(refusals.resolved(refused, reading, self.now + timedelta(minutes=2)))

    def test_reading_at_same_time_as_refusal_never_resolves_it(self) -> None:
        refused = self.refusal(window=refusals.UnknownWindow())
        reading = self.reading(at=self.now, reset=self.now + timedelta(days=7))
        self.assertFalse(refusals.resolved(refused, reading, self.now + timedelta(minutes=2)))

    def test_same_reset_with_usage_left_keeps_the_refusal_for_both_tools(self) -> None:
        reset = self.now + timedelta(hours=3)
        for tool in ("claude", "codex"):
            for name in ("weekly", "five_hour"):
                for left in (1, 100):
                    with self.subTest(tool=tool, window=name, left=left):
                        refused = self.refusal(tool=tool, window=refusals.KnownWindow(name),
                                               reset=refusals.KnownReset(reset))
                        reading = self.reading(at=self.now + timedelta(seconds=1), reset=reset,
                                               weekly=left, five=left, tool=tool)
                        self.assertFalse(refusals.resolved(refused, reading, self.now + timedelta(minutes=2)))

    def test_unknown_window_resolves_only_after_both_windows_have_positive_later_readings(self) -> None:
        refused = self.refusal(window=refusals.UnknownWindow())
        for weekly, five, expected in ((80, 0, False), (0, 80, False), (1, 1, True)):
            with self.subTest(weekly=weekly, five=five):
                reading = self.reading(at=self.now + timedelta(seconds=1), reset=self.now + timedelta(hours=3),
                                       weekly=weekly, five=five)
                self.assertEqual(refusals.resolved(refused, reading, self.now + timedelta(minutes=2)), expected)

    def test_known_weekly_refusal_needs_only_its_own_window_to_recover(self) -> None:
        refused = self.refusal(window=refusals.KnownWindow("weekly"))
        reading = self.reading(at=self.now + timedelta(seconds=1), reset=self.now + timedelta(hours=3), five=0)
        self.assertTrue(refusals.resolved(refused, reading, self.now + timedelta(minutes=2)))
        reading = self.reading(at=self.now + timedelta(seconds=1), reset=self.now + timedelta(hours=3), five=1)
        self.assertTrue(refusals.resolved(refused, reading, self.now + timedelta(minutes=2)))

    def test_unknown_refusal_without_five_hour_window_resolves_only_on_positive_later_weekly_reading(self) -> None:
        refused = self.refusal(window=refusals.UnknownWindow())
        for at, weekly, expected in ((self.now, 80, False),
                                     (self.now + timedelta(seconds=1), 0, False),
                                     (self.now + timedelta(seconds=1), 1, True)):
            with self.subTest(at=at, weekly=weekly):
                reading = self.reading(at=at, weekly=weekly,
                    reset=self.now + timedelta(hours=3), include_five=False)
                self.assertEqual(refusals.resolved(refused, reading, self.now + timedelta(minutes=2)), expected)

    def test_stale_or_missing_reading_does_not_resolve_an_unexpired_refusal(self) -> None:
        refused = self.refusal(reset=refusals.KnownReset(self.now + timedelta(hours=3)))
        for reading in (accounts.NoReading(), accounts.StaleReading()):
            with self.subTest(reading=reading):
                self.assertFalse(refusals.resolved(refused, reading, self.now))

    def test_unknown_refusal_window_requires_new_readings_even_when_a_reset_was_recorded(self) -> None:
        refused = self.refusal(window=refusals.UnknownWindow(), reset=refusals.KnownReset(self.now))
        self.assertFalse(refusals.resolved(refused, accounts.NoReading(), self.now))

    def test_weekly_refusal_does_not_resolve_when_only_five_hour_reset_moves_later(self) -> None:
        reset = self.now + timedelta(hours=3)
        refused = self.refusal(reset=refusals.KnownReset(reset))
        at = self.now + timedelta(seconds=1)
        reading = accounts.FreshReading(
            accounts.ReadingWindow(0, refusals.KnownReset(reset), at),
            accounts.ReadingWindow(80, refusals.KnownReset(reset + timedelta(hours=5)), at), "0")
        self.assertFalse(refusals.resolved(refused, reading, self.now + timedelta(minutes=2)))

    def test_reading_with_only_one_window_observed_after_refusal_does_not_resolve_unknown_window(self) -> None:
        refused = self.refusal(window=refusals.UnknownWindow())
        reset = refusals.KnownReset(self.now + timedelta(hours=3))
        reading = accounts.FreshReading(accounts.ReadingWindow(80, reset, self.now + timedelta(seconds=1)),
            accounts.ReadingWindow(80, reset, self.now - timedelta(seconds=1)), "0")
        self.assertFalse(refusals.resolved(refused, reading, self.now + timedelta(minutes=2)))

    def test_later_positive_refused_window_resolves_without_a_later_reset(self) -> None:
        at = self.now + timedelta(seconds=1)
        for name in ("weekly", "five_hour"):
            refused = self.refusal(window=refusals.KnownWindow(name),
                reset=refusals.KnownReset(self.now + timedelta(days=3)))
            for reset in (refusals.NoResetKnown(), refusals.KnownReset(self.now + timedelta(hours=1))):
                for left in (0, 100):
                    with self.subTest(window=name, reset=reset, left=left):
                        window = accounts.ReadingWindow(left, reset, at)
                        other = accounts.ReadingWindow(0, refusals.NoResetKnown(), at)
                        reading = accounts.FreshReading(window if name == "weekly" else other,
                            window if name == "five_hour" else other, "0")
                        self.assertEqual(refusals.resolved(refused, reading, at), left > 0)
