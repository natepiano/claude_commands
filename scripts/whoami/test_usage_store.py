"""Tests for the saved usage readings."""

from __future__ import annotations

import sys
import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.request
from email.message import Message
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast, final, override
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.whoami import usage_store
from scripts.whoami import agent_accounts
from scripts.whoami.agent_accounts import NoWeeklyReadSkip, Quota, Report, StaleTokenSkip, UsageReadOutcome

# Hang guards allow for load 136 on natedev (2026-10-29).
TEST_HANG_GUARD_SECS = 300

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

    def test_timer_reuse_keeps_original_time_windows_and_failed_read(self) -> None:
        reports = [Report("Codex", email="timer@example.invalid",
            source=agent_accounts.CodexHomeSource(self.home / ".codex"),
            quotas=[Quota("Weekly", 20, RESETS)], five_hour=Quota("5-hour", 30, RESETS), credit_balance="25"),
            Report("Claude", problem="identity unavailable")]
        usage_store.save_timer_usage("selection", reports, READ)
        recent = usage_store.recent_timer_usage("selection", READ + timedelta(minutes=2))
        self.assertIsInstance(recent, usage_store.RecentTimerUsage)
        assert isinstance(recent, usage_store.RecentTimerUsage)
        self.assertEqual(recent.observed_at, READ)
        self.assertEqual(recent.reports[0].weekly_remaining, 80)
        self.assertEqual(recent.reports[0].credit_balance, "25")
        self.assertEqual(recent.reports[0].source, reports[0].source)
        self.assertEqual(recent.reports[0].five_hour, reports[0].five_hour)
        self.assertEqual(recent.reports[1].problem, "identity unavailable")
        self.assertEqual(usage_store.timer_file().stat().st_mode & 0o777, 0o600)

    def test_timer_cache_rejects_expiry_clock_reversal_selection_change_and_damage(self) -> None:
        usage_store.save_timer_usage("selection", [], READ)
        for signature, at in (("other", READ), ("selection", READ - timedelta(seconds=1)),
                              ("selection", READ + timedelta(minutes=10))):
            self.assertIsInstance(usage_store.recent_timer_usage(signature, at), usage_store.TimerUsageDue)
        for text in ("bad json", "[]", '{}', '{"signature":"selection","at":42}',
                     json.dumps({"signature": "selection", "at": READ.isoformat(), "reports": [None]})):
            _ = usage_store.timer_file().write_text(text)
            self.assertIsInstance(usage_store.recent_timer_usage("selection", READ), usage_store.TimerUsageDue)

    def test_timer_cache_cannot_hide_a_reset(self) -> None:
        usage_store.save_timer_usage("selection", [Report("Codex",
            quotas=[Quota("Weekly", 20, RESETS)],
            five_hour=Quota("5-hour", 25, READ + timedelta(minutes=1)))], READ)
        self.assertIsInstance(usage_store.recent_timer_usage("selection", READ + timedelta(minutes=2)),
                              usage_store.TimerUsageDue)

    def test_save_keeps_both_windows_under_the_login_in_lower_case(self) -> None:
        usage_store.save([Report(
            "Claude", email="Main@Example.invalid",
            quotas=[Quota("Weekly (Opus)", 1.0, None), Quota("Weekly", 29.5, RESETS)],
            five_hour=Quota("5-hour", 36.0, READ + timedelta(hours=2)),
        )], READ)

        self.assertEqual(usage_store.load(), {"main@example.invalid": {
            "five_hour": {"left": 64.0, "resets": "2026-10-12T16:30:00+00:00", "at": "2026-10-12T14:30:00+00:00"},
            "read_outcome": UsageReadOutcome.OK,
            "weekly": {"left": 70.5, "resets": "2026-10-15T16:00:00+00:00", "at": "2026-10-12T14:30:00+00:00"},
        }})
        self.assertEqual(usage_store.store_file(), self.home / ".local/state/agent-notes/usage.json")

    def test_save_with_labels_writes_each_label_s_readings_and_no_login(self) -> None:
        usage_store.save([Report(
            "Claude", email="Main@Example.invalid", quotas=[Quota("Weekly", 98.0, RESETS)],
            five_hour=Quota("5-hour", 36.0, READ + timedelta(hours=2)),
        )], READ, {"claude 2": "main@example.invalid", "claude3": "spare@example.invalid",
                   "main@example.invalid": "main@example.invalid"})

        text = usage_store.label_file().read_text(encoding="utf-8")
        self.assertEqual(json.loads(text), {"claude2": {
            "five_hour": {"left": 64.0, "resets_at": int((READ + timedelta(hours=2)).timestamp()),
                          "at": int(READ.timestamp())},
            "weekly": {"left": 2.0, "resets_at": int(RESETS.timestamp()), "at": int(READ.timestamp())},
        }})
        self.assertNotIn("@", text)
        self.assertEqual(usage_store.label_file(), self.home / ".local/state/agent-notes/by-label.json")

    def test_a_refused_read_keeps_the_label_s_last_reading(self) -> None:
        logins = {"claude2": "main@example.invalid"}
        usage_store.save([Report("Claude", email="main@example.invalid", quotas=[Quota("Weekly", 10.0, RESETS)])],
                         READ, logins)
        usage_store.save([Report("Claude", email="main@example.invalid", problem="HTTP 429")],
                         READ + timedelta(minutes=2), logins)

        held = cast(dict[str, dict[str, dict[str, float]]], json.loads(usage_store.label_file().read_text(encoding="utf-8")))
        self.assertEqual(held["claude2"]["weekly"]["at"], int(READ.timestamp()))

    def test_a_none_open_five_hour_window_has_no_reset(self) -> None:
        usage_store.save([Report("Claude", email="main@example.invalid", quotas=[Quota("Weekly", 10.0, RESETS)])],
                         READ, {"claude2": "main@example.invalid"})

        held = cast(dict[str, dict[str, dict[str, object]]], json.loads(usage_store.label_file().read_text(encoding="utf-8")))
        self.assertEqual(held["claude2"]["five_hour"], {"left": 100.0, "resets_at": None, "at": int(READ.timestamp())})

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
            "read_outcome": UsageReadOutcome.OK,
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
            "read_outcome": UsageReadOutcome.OK,
            "weekly": {"left": 19.0, "resets": "2026-10-15T16:00:00+00:00", "at": "2026-10-12T14:32:00+00:00"},
            "credits": {"balance": "unlimited", "at": "2026-10-12T14:32:00+00:00"},
        })

    def test_a_refused_read_records_failure_but_keeps_the_last_good_windows(self) -> None:
        usage_store.save([Report("Claude", email="main@example.invalid", quotas=[Quota("Weekly", 10.0, RESETS)])], READ)
        before = usage_store.load()["main@example.invalid"]

        usage_store.save([
            Report("Claude", email="main@example.invalid",
                   quota_problem="Weekly quota: unavailable (HTTP 429; try Claude /usage)"),
            Report("Claude", problem="Not logged in"),
            Report("Codex", email="codex@example.invalid",
                   quota_problem="Weekly quota: unavailable (Codex usage API failed)"),
        ], READ + timedelta(minutes=2))

        held = usage_store.load()["main@example.invalid"]
        self.assertEqual(held.get("weekly"), before.get("weekly"))
        self.assertEqual(held.get("five_hour"), before.get("five_hour"))
        self.assertEqual(usage_store.latest_read_outcome(held), UsageReadOutcome.ERROR)

    def test_stale_token_skip_round_trips_without_changing_last_good_readings(self) -> None:
        usage_store.save([Report("Claude", email="Main@Example.invalid",
            quotas=[Quota("Weekly", 90, RESETS)])], READ, {"claude2": "main@example.invalid"})
        before = usage_store.load()["main@example.invalid"].copy()
        labels = cast(object, json.loads(usage_store.label_file().read_text()))
        usage_store.save([Report("Claude", email="Main@Example.invalid",
            quota_problem="display wording can change", weekly_read_skip=StaleTokenSkip())],
            RESETS + timedelta(minutes=2), {"claude2": "main@example.invalid"})
        held = usage_store.load()["main@example.invalid"]
        self.assertIsInstance(usage_store.weekly_read_skip(held), StaleTokenSkip)
        self.assertEqual(held.get("weekly"), before.get("weekly"))
        self.assertEqual(held.get("five_hour"), before.get("five_hour"))
        self.assertEqual(usage_store.latest_read_outcome(held), UsageReadOutcome.STALE_TOKEN)
        self.assertEqual(json.loads(usage_store.label_file().read_text()), labels)
        self.assertFalse(usage_store.merge(usage_store.load(), [Report("Claude", email="main@example.invalid",
            weekly_read_skip=StaleTokenSkip())], RESETS + timedelta(minutes=4)))

    def test_another_read_failure_clears_the_stale_token_reason_without_refreshing_usage(self) -> None:
        usage_store.save([Report("Claude", email="main@example.invalid",
            quotas=[Quota("Weekly", 90, RESETS)])], READ)
        before = usage_store.load()["main@example.invalid"].copy()
        usage_store.save([Report("Claude", email="main@example.invalid", weekly_read_skip=StaleTokenSkip())], RESETS)
        usage_store.save([Report("Claude", email="main@example.invalid",
            quota_problem="Weekly quota: not read (stale token; a Claude session on this account refreshes it)")],
            RESETS + timedelta(minutes=2))
        held = usage_store.load()["main@example.invalid"]
        self.assertIsInstance(usage_store.weekly_read_skip(held), NoWeeklyReadSkip)
        self.assertEqual(held.get("weekly"), before.get("weekly"))
        self.assertEqual(held.get("five_hour"), before.get("five_hour"))
        self.assertEqual(usage_store.latest_read_outcome(held), UsageReadOutcome.ERROR)

    def test_a_successful_read_clears_the_stale_token_skip_and_saves_new_usage(self) -> None:
        usage_store.save([Report("Claude", email="main@example.invalid", weekly_read_skip=StaleTokenSkip())], READ)
        usage_store.save([Report("Claude", email="main@example.invalid",
            quotas=[Quota("Weekly", 37, RESETS)])], READ + timedelta(minutes=2))
        held = usage_store.load()["main@example.invalid"]
        self.assertIsInstance(usage_store.weekly_read_skip(held), NoWeeklyReadSkip)
        assert "weekly" in held
        self.assertEqual(held["weekly"]["left"], 63)
        self.assertEqual(held["weekly"]["at"], (READ + timedelta(minutes=2)).isoformat())

    def test_http_failures_keep_their_cause_and_a_good_read_clears_it(self) -> None:
        for code, expected in ((401, UsageReadOutcome.REFUSED_401),
                               (429, UsageReadOutcome.RATE_LIMITED_429),
                               (503, UsageReadOutcome.ERROR)):
            with self.subTest(code=code):
                usage_store.store_file().unlink(missing_ok=True)
                good = Report("Claude", email="main@example.invalid", quotas=[Quota("Weekly", 10, RESETS)])
                usage_store.save([good], READ)
                with (patch.object(agent_accounts, "command_json", return_value={
                        "loggedIn": True, "email": "main@example.invalid", "authMethod": "claude.ai"}),
                      patch.object(agent_accounts, "claude_credentials", return_value={
                        "claudeAiOauth": {"accessToken": "fixture"}}),
                      patch.object(urllib.request, "urlopen", side_effect=urllib.error.HTTPError(
                        "https://fixture.invalid", code, "fixture failure", Message(), None))):
                    failed = agent_accounts.claude_live(self.home / ".claude2")
                usage_store.save([failed], READ + timedelta(minutes=2))
                held = usage_store.load()["main@example.invalid"]
                assert "weekly" in held
                self.assertEqual(usage_store.latest_read_outcome(held), expected)
                self.assertEqual(held["weekly"]["at"], READ.isoformat())
                self.assertEqual(held["weekly"]["left"], 90)
                usage_store.save([good], READ + timedelta(minutes=4))
                held = usage_store.load()["main@example.invalid"]
                assert "weekly" in held
                self.assertEqual(usage_store.latest_read_outcome(held), UsageReadOutcome.OK)
                self.assertEqual(held["weekly"]["at"], (READ + timedelta(minutes=4)).isoformat())

    def test_identity_failure_marks_the_last_login_read_in_that_directory(self) -> None:
        source = agent_accounts.ClaudeDirectorySource(self.home / ".claude2")
        usage_store.save([Report("Claude", email="main@example.invalid", source=source,
            quotas=[Quota("Weekly", 10, RESETS)])], READ)
        usage_store.save([Report("Claude", source=source, problem="auth status failed")], READ + timedelta(minutes=2))
        held = usage_store.load()["main@example.invalid"]
        assert "weekly" in held
        self.assertEqual(usage_store.latest_read_outcome(held), UsageReadOutcome.ERROR)
        self.assertEqual(held["weekly"]["at"], READ.isoformat())
        # After a directory changes login, later failures must not affect its old login.
        usage_store.save([Report("Claude", email="new@example.invalid", source=source,
            quotas=[Quota("Weekly", 20, RESETS)])], READ + timedelta(minutes=4))
        usage_store.save([Report("Claude", source=source, problem="auth status failed")], READ + timedelta(minutes=6))
        self.assertEqual(usage_store.latest_read_outcome(usage_store.load()["new@example.invalid"]), UsageReadOutcome.ERROR)
        stored = usage_store.load()
        self.assertEqual(stored.directory_logins[usage_store.directory_key("Claude", str(source.directory))],
                         "new@example.invalid")

    def test_timer_identity_failure_uses_its_known_account_with_an_old_store(self) -> None:
        directory = self.home / ".claude2"
        login = "main@example.invalid"
        path = usage_store.store_file()
        path.parent.mkdir(parents=True)
        window = {"left": 50, "resets": RESETS.isoformat(), "at": READ.isoformat()}
        _ = path.write_text(json.dumps({login: {"weekly": window}}))
        identity = agent_accounts.NumberedAccount("claude2", directory, login)
        selection = agent_accounts.ClaudeAccountsInUse(identity, True,
            (agent_accounts.CheckedNamedClaudeAccount("claude2", directory, identity),),
            frozenset())
        with (patch.object(agent_accounts, "claude_on_disk", return_value=Report("Claude")),
              patch.object(agent_accounts, "codex_homes", return_value=[]),
              patch.object(agent_accounts, "claude_live", return_value=Report("Claude",
                  source=agent_accounts.ClaudeDirectorySource(directory), problem="identity unavailable"))):
            reports = asyncio.run(agent_accounts.live_reports(selection, self.home / ".codex"))
        usage_store.save(reports, READ + timedelta(minutes=2))
        stored = usage_store.load()
        self.assertEqual(usage_store.latest_read_outcome(stored[login]), UsageReadOutcome.ERROR)
        self.assertEqual(stored[login].get("weekly"), window)

    def test_unlinked_identity_failure_does_not_guess_a_login(self) -> None:
        usage_store.save([Report("Claude", email="main@example.invalid",
            quotas=[Quota("Weekly", 10, RESETS)])], READ)
        before = usage_store.load()
        usage_store.save([Report("Claude", source=agent_accounts.ClaudeDirectorySource(self.home / ".unknown"),
            problem="identity unavailable")], READ + timedelta(minutes=2))
        self.assertEqual(usage_store.load(), before)

    def test_old_and_unknown_outcomes_decode_without_losing_good_readings(self) -> None:
        path = usage_store.store_file()
        path.parent.mkdir(parents=True)
        window = {"left": 50, "resets": RESETS.isoformat(), "at": READ.isoformat()}
        _ = path.write_text(json.dumps({
            "old": {"weekly": window}, "skipped": {"weekly": window, "weekly_read_skip": "stale_token"},
            "unknown": {"weekly": window, "read_outcome": "new unknown result"}}))
        held = usage_store.load()
        self.assertEqual(usage_store.latest_read_outcome(held["old"]), UsageReadOutcome.OK)
        self.assertEqual(usage_store.latest_read_outcome(held["skipped"]), UsageReadOutcome.STALE_TOKEN)
        self.assertEqual(usage_store.latest_read_outcome(held["unknown"]), UsageReadOutcome.ERROR)
        unknown = held["unknown"]
        assert "weekly" in unknown
        self.assertEqual(unknown["weekly"]["at"], READ.isoformat())

    def test_held_save_keeps_readings_outcomes_and_attempt_fields_byte_identical(self) -> None:
        claim = usage_store.claim_read("main@example.invalid", READ)
        assert isinstance(claim, agent_accounts.UsageReadClaim)
        report = Report("Claude", email="main@example.invalid", usage_read_outcome=UsageReadOutcome.RATE_LIMITED_429)
        usage_store.finish_read("main@example.invalid", claim, report)
        usage_store.save([Report("Claude", email="main@example.invalid",
            quotas=[Quota("Weekly", 10, RESETS)])], READ)
        before = usage_store.store_file().read_bytes()
        held = Report("Claude", email="main@example.invalid",
            source=agent_accounts.ClaudeDirectorySource(self.home / ".claude3"),
            usage_read_outcome=UsageReadOutcome.HELD)
        stored = usage_store.load()
        self.assertFalse(usage_store.merge(stored, [held], READ + timedelta(seconds=1)))
        usage_store.save([held], READ + timedelta(seconds=1))
        self.assertEqual(usage_store.store_file().read_bytes(), before)
        self.assertEqual(stored["main@example.invalid"].get("attempted_at"), READ.isoformat())
        self.assertEqual(stored["main@example.invalid"].get("attempt_outcome"), UsageReadOutcome.RATE_LIMITED_429)

    def test_a_late_save_of_a_good_read_keeps_a_newer_failed_attempt(self) -> None:
        login = "main@example.invalid"
        first = usage_store.claim_read(login, READ)
        assert isinstance(first, agent_accounts.UsageReadClaim)
        good = Report("Claude", email=login, usage_read_claim=first,
            usage_read_outcome=UsageReadOutcome.OK, quotas=[Quota("Weekly", 30, RESETS)])
        usage_store.finish_read(login, first, good)
        later = READ + timedelta(seconds=91)
        second = usage_store.claim_read(login, later)
        assert isinstance(second, agent_accounts.UsageReadClaim)
        failed = Report("Claude", email=login, usage_read_claim=second,
            usage_read_outcome=UsageReadOutcome.RATE_LIMITED_429)
        usage_store.finish_read(login, second, failed)
        before = usage_store.store_file().read_bytes()
        usage_store.save([good], later + timedelta(seconds=1), {"claude2": login})
        self.assertEqual(usage_store.store_file().read_bytes(), before)
        stored = usage_store.load()[login]
        self.assertEqual(stored.get("read_outcome"), UsageReadOutcome.RATE_LIMITED_429)
        self.assertEqual(stored.get("weekly", {}).get("at"), READ.isoformat())
        labelled = cast(dict[str, dict[str, dict[str, object]]], json.loads(usage_store.label_file().read_text()))
        self.assertEqual(labelled["claude2"]["weekly"]["at"], int(READ.timestamp()))

    def test_two_saves_keep_both_logins_under_the_same_lock(self) -> None:
        start = Barrier(2)

        def save(login: str) -> None:
            _ = start.wait(timeout=TEST_HANG_GUARD_SECS)
            usage_store.save([Report("Claude", email=login,
                quotas=[Quota("Weekly", 10, RESETS)])], READ)

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(save, "first@example.invalid")
            second = pool.submit(save, "second@example.invalid")
            first.result(timeout=TEST_HANG_GUARD_SECS)
            second.result(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(set(usage_store.load()), {"first@example.invalid", "second@example.invalid"})

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
