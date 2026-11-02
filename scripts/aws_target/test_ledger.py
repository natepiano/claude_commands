"""Cost evidence must survive retries and refuse unsafe spending."""
from __future__ import annotations

from contextlib import redirect_stderr
from decimal import Decimal
import fcntl
import io
import json
from pathlib import Path
import subprocess
from typing import cast, override
from unittest import mock
import unittest

from scripts.aws_target import config, ledger
from scripts.aws_target.ledger_test_support import CostFixture, git_only, instant
from scripts.lib.atomic_file import AtomicDurabilityUnconfirmed, write_text_atomic


class LedgerTests(CostFixture):
    book: ledger.CostLedger = ledger.CostLedger(Path())
    notices: list[ledger.LimitEvent] = []
    start: int = instant("2026-01-15T00:00:00+00:00")

    @override
    def setUp(self) -> None:
        super().setUp()
        self.book = ledger.CostLedger(self.state)
        self.notices = []
        _ = self.enterContext(mock.patch.object(subprocess, "run", side_effect=git_only))

    def inventory(self, at: int, monthly: str = "0") -> None:
        self.book.record_inventory(ledger.ResourceInventory(
            at, (ledger.PricedResource("retained-storage", Decimal(monthly)),), "priced fixture inventory"))

    def observe(self, at: int, state: ledger.InstanceState) -> None:
        self.book.observe(ledger.InstanceObservation(at, state, "fake DescribeInstances"))

    def notice(self, event: ledger.LimitEvent) -> bool:
        document = cast(dict[str, object], json.loads(self.book.path.read_text()))
        decision = cast(dict[str, object], document["decision"])
        self.assertEqual(decision["status"], "refused", "Publish refusal before delivery")
        self.notices.append(event)
        return True

    def guard(self, at: int, purpose: ledger.SpendingPurpose = "admission") -> ledger.SpendingDecision:
        return self.book.guard(self.settings, purpose, at, self.notice)

    def estimate(self, at: int) -> ledger.CostEstimate:
        return self.book.estimate(config.read_settings(self.settings), at)

    def running(self, at: int) -> None:
        self.inventory(at)
        self.observe(at, "running")

    def test_repeated_observations_and_restart_charge_each_running_second_once(self) -> None:
        self.running(self.start)
        sample = ledger.PriceSample(self.start, Decimal("0.75"), "Spot fixture")
        self.book.record_price(sample)
        self.book.record_price(sample)
        self.observe(self.start + 1800, "running")
        self.observe(self.start + 1800, "running")
        self.observe(self.start + 3600, "stopped")
        before = self.estimate(self.start + 3600)
        self.book = ledger.CostLedger(self.state)
        self.observe(self.start + 3600, "stopped")
        after = self.estimate(self.start + 3600)
        self.assertEqual(before.monthly.usd, Decimal("0.75"))
        self.assertEqual(after, before)
        self.assertEqual(sum(r.end - r.start for r in after.intervals if r.kind == "instance"), 3600)

    def test_unknown_transition_refuses_every_spending_entry(self) -> None:
        self.running(self.start)
        self.observe(self.start + 1800, "unknown")
        self.observe(self.start + 3600, "stopped")
        for purpose in ("deployment", "start", "admission"):
            with self.subTest(purpose=purpose):
                result = self.guard(self.start + 3600, purpose)
                self.assertIsInstance(result, ledger.SpendingUnavailable)
                if isinstance(result, ledger.SpendingUnavailable):
                    self.assertIn("transition", result.reason)
        self.assertEqual(self.notices, [])

    def test_first_resource_requires_a_priced_inventory(self) -> None:
        for purpose in ("deployment", "start", "admission"):
            with self.subTest(purpose=purpose):
                self.assertIsInstance(self.guard(self.start, purpose), ledger.SpendingUnavailable)
        self.inventory(self.start)
        self.assertIsInstance(self.guard(self.start, "deployment"), ledger.SpendingAllowed)
        self.assertIsInstance(self.guard(self.start, "start"), ledger.SpendingUnavailable)
        self.observe(self.start, "stopped")
        self.assertIsInstance(self.guard(self.start, "start"), ledger.SpendingAllowed)

    def test_first_resource_reserves_known_fixed_costs_and_headroom(self) -> None:
        self.inventory(self.start, "398")
        self.observe(self.start, "stopped")
        for purpose in ("deployment", "start", "admission"):
            with self.subTest(purpose=purpose):
                self.assertIsInstance(self.guard(self.start, purpose), ledger.SpendingRefused)

    def test_stopped_instance_keeps_storage_charges_without_instance_time(self) -> None:
        start = instant("2026-01-15T00:00:00+00:00")
        self.inventory(start, "31")
        self.observe(start, "stopped")
        total = self.estimate(start + 43200)
        self.assertEqual(total.daily.usd, Decimal("0.5"))
        self.assertEqual(total.monthly.usd, Decimal("0.5"))
        self.assertFalse(any(r.kind == "instance" for r in total.intervals))

    def test_public_storage_components_remain_charged_after_stop(self) -> None:
        self.costs({"retained_storage_usd_per_month": "15", "root_storage_usd_per_month": "5",
                    "ipv4_usd_per_month": "3", "gp3_iops_usd_per_month": "4",
                    "gp3_throughput_usd_per_month": "4"})
        self.inventory(self.start)
        self.observe(self.start, "stopped")
        self.assertEqual(self.estimate(self.start + 43200).monthly.usd, Decimal("0.5"))

    def test_price_history_splits_time_and_uses_fallback_after_expiry(self) -> None:
        self.running(self.start)
        self.replace("price_max_age_seconds", "3600")
        self.book.record_price(ledger.PriceSample(self.start, Decimal("0.5"), "first Spot sample"))
        self.book.record_price(ledger.PriceSample(self.start + 1800, Decimal("0.25"), "second Spot sample"))
        total = self.estimate(self.start + 7200)
        self.assertEqual(total.monthly.usd, Decimal("1.00"))
        self.assertTrue(any("fallback_usd_per_hour" in row.source for row in total.intervals))

    def test_no_price_history_uses_the_configured_fallback(self) -> None:
        self.replace("fallback_usd_per_hour", "0.625")
        self.running(self.start)
        self.assertEqual(self.estimate(self.start + 3600).monthly.usd, Decimal("0.625"))

    def test_price_refresh_uses_the_configured_interval_across_restart(self) -> None:
        calls: list[int] = []

        def prices(now: int) -> tuple[ledger.PriceSample, ...]:
            calls.append(now)
            return (ledger.PriceSample(now, Decimal("0.25"), "fake price history"),)

        self.assertTrue(self.book.refresh_prices(self.settings, self.start, prices))
        self.book = ledger.CostLedger(self.state)
        self.assertFalse(self.book.refresh_prices(self.settings, self.start + 3599, prices))
        self.assertTrue(self.book.refresh_prices(self.settings, self.start + 3600, prices))
        self.assertEqual(calls, [self.start, self.start + 3600])
        self.replace("price_refresh_seconds", "60")
        self.assertTrue(self.book.refresh_prices(self.settings, self.start + 3660, prices))

    def test_day_and_month_boundaries_use_the_accounting_zone(self) -> None:
        self.text_setting("accounting_zone", "America/Los_Angeles")
        start = instant("2026-02-01T07:30:00+00:00")
        self.running(start)
        self.observe(start + 3600, "stopped")
        total = self.estimate(start + 3600)
        self.assertEqual(total.daily.day, "2026-02-01")
        self.assertEqual(total.monthly.month, "2026-02")
        self.assertEqual(total.daily.usd, Decimal("0.5"))
        self.assertEqual(total.monthly.usd, Decimal("0.5"))
        earlier = [r.usd for r in total.intervals if r.kind == "instance" and r.start == start]
        self.assertEqual(earlier, [Decimal("0.5")])

    def test_daylight_saving_day_uses_elapsed_seconds_without_duplicate_charges(self) -> None:
        self.text_setting("accounting_zone", "America/New_York")
        start = instant("2026-03-08T00:00:00-05:00")
        self.running(start)
        end = instant("2026-03-08T23:00:00-04:00")
        self.observe(end, "stopped")
        total = self.estimate(end)
        self.assertEqual(total.daily.usd, Decimal("22"))
        self.assertEqual(sum(r.end - r.start for r in total.intervals if r.kind == "instance"), 22 * 3600)

    def test_exact_daily_limit_refuses_every_entry_with_live_evidence(self) -> None:
        self.replace("limit_reserve_usd", "0")
        self.running(self.start)
        now = self.start + 20 * 3600
        self.observe(now, "stopped")
        for purpose in ("deployment", "start", "admission"):
            with self.subTest(purpose=purpose):
                result = self.guard(now, purpose)
                self.assertIsInstance(result, ledger.SpendingRefused)
                self.assertEqual(result.at, now)
                self.assertEqual(result.evidence.purpose, purpose)
                self.assertEqual({r.key: r.origin for r in result.evidence.limits},
                                 {"daily_limit_usd": "live", "monthly_limit_usd": "live"})
                if isinstance(result, ledger.SpendingRefused):
                    self.assertEqual(result.reason, "daily_limit")
                    self.assertEqual(result.action, "drain")
        self.assertEqual(len(self.notices), 1)

    def test_exact_monthly_limit_refuses_when_the_daily_limit_has_capacity(self) -> None:
        self.replace("monthly_limit_usd", "5")
        self.replace("limit_reserve_usd", "0")
        self.running(self.start)
        self.observe(self.start + 5 * 3600, "stopped")
        for purpose in ("deployment", "start", "admission"):
            with self.subTest(purpose=purpose):
                result = self.guard(self.start + 5 * 3600, purpose)
                self.assertIsInstance(result, ledger.SpendingRefused)
                if isinstance(result, ledger.SpendingRefused):
                    self.assertEqual(result.reason, "monthly_limit")

    def test_reserve_equality_refuses_and_changed_headroom_is_read_at_use(self) -> None:
        self.running(self.start)
        now = self.start + 17 * 3600
        self.observe(now, "stopped")
        self.assertIsInstance(self.guard(now), ledger.SpendingRefused)
        self.replace("limit_reserve_usd", "2.99")
        self.assertIsInstance(self.guard(now), ledger.SpendingAllowed)
        self.replace("limit_reserve_usd", "3")
        self.replace("monthly_limit_usd", "20")
        result = self.guard(now, "start")
        self.assertIsInstance(result, ledger.SpendingRefused)
        self.assertEqual(len(self.notices), 2)

    def test_missing_and_malformed_live_limits_never_use_committed_permission(self) -> None:
        self.replace("daily_limit_usd", "20")
        self.replace("monthly_limit_usd", "400")
        self.commit_settings()
        self.replace("daily_limit_usd", "10")
        self.running(self.start)
        for key in ("daily_limit_usd", "monthly_limit_usd"):
            for fault in ("missing", "broken", "NaN", "Infinity", "-1"):
                with self.subTest(key=key, fault=fault):
                    if fault == "missing":
                        original = self.settings.read_text()
                        self.remove(key)
                    else:
                        original = self.settings.read_text()
                        self.replace(key, fault)
                    for purpose in ("deployment", "start", "admission"):
                        with redirect_stderr(io.StringIO()):
                            result = self.guard(self.start, purpose)
                        self.assertIsInstance(result, ledger.SpendingUnavailable)
                        if isinstance(result, ledger.SpendingUnavailable):
                            self.assertIn(key, result.reason)
                        self.assertTrue(any(r.key == key and r.origin != "live" for r in result.evidence.limits))
                    _ = self.settings.write_text(original)
        self.assertEqual(self.notices, [])

    def test_corrupted_ledger_preserves_its_evidence_and_refuses_all_entry_points(self) -> None:
        self.running(self.start)
        damaged = self.book.path.read_bytes()[:35] + b"\xff unfinished"
        _ = self.book.path.write_bytes(damaged)
        for purpose in ("deployment", "start", "admission"):
            with self.subTest(purpose=purpose):
                self.assertIsInstance(self.guard(self.start, purpose), ledger.SpendingUnavailable)
                self.assertEqual(self.book.path.read_bytes(), damaged)
        self.assertEqual(self.notices, [])

    def test_failed_decision_write_preserves_evidence_and_sends_no_notice(self) -> None:
        self.running(self.start)
        now = self.start + 17 * 3600
        self.observe(now, "stopped")
        before = self.book.path.read_bytes()
        with mock.patch.object(ledger, "write_text_atomic", side_effect=OSError("fixture disk full")):
            self.assertIsInstance(self.guard(now), ledger.SpendingUnavailable)
        self.assertEqual(self.book.path.read_bytes(), before)
        self.assertEqual(self.notices, [])
        self.book = ledger.CostLedger(self.state)
        self.assertIsInstance(self.guard(now), ledger.SpendingRefused)
        self.assertEqual(len(self.notices), 1)

    def test_limit_event_survives_restart_and_a_later_failed_write(self) -> None:
        self.running(self.start)
        now = self.start + 17 * 3600
        self.observe(now, "stopped")
        self.assertIsInstance(self.guard(now), ledger.SpendingRefused)
        first = self.notices[0]
        self.book = ledger.CostLedger(self.state)
        with mock.patch.object(ledger, "write_text_atomic", side_effect=OSError("fixture disk full")):
            self.assertIsInstance(self.guard(now), ledger.SpendingUnavailable)
        self.book = ledger.CostLedger(self.state)
        self.assertIsInstance(self.guard(now), ledger.SpendingRefused)
        self.assertEqual(self.notices, [first])

    def test_scheduled_cost_explorer_reads_are_charged_once(self) -> None:
        self.replace("cost_explorer_read_usd", "0.01")
        self.replace("cost_explorer_interval_seconds", "3600")
        self.inventory(self.start)
        self.observe(self.start, "stopped")
        now = self.start + 7200
        first = self.estimate(now)
        self.book = ledger.CostLedger(self.state)
        self.assertEqual(self.estimate(now), first)
        self.assertEqual(first.monthly.usd, Decimal("0.02"))

    def test_repeated_bill_reconciliation_never_reduces_or_duplicates_charges(self) -> None:
        self.running(self.start)
        now = self.start + 3600
        self.observe(now, "stopped")
        reading = ledger.CostReconciliation(now, self.start, now, Decimal("2"), "fake Cost Explorer")
        self.book.reconcile(reading)
        self.book.reconcile(reading)
        self.assertEqual(self.estimate(now).monthly.usd, Decimal("2"))
        self.book.reconcile(ledger.CostReconciliation(now, self.start, now, Decimal("0.5"), "lower late bill"))
        self.assertEqual(self.estimate(now).monthly.usd, Decimal("2"))

    def test_stale_or_future_observations_refuse_new_spending(self) -> None:
        self.running(self.start)
        self.replace("observation_max_age_seconds", "60")
        self.assertIsInstance(self.guard(self.start + 61), ledger.SpendingUnavailable)
        self.assertIsInstance(self.guard(self.start - 1), ledger.SpendingUnavailable)

    def saved_decision(self) -> dict[str, object]:
        document = cast(dict[str, object], json.loads(self.book.path.read_text()))
        return cast(dict[str, object], document["decision"])

    def saved_events(self) -> list[dict[str, object]]:
        document = cast(dict[str, object], json.loads(self.book.path.read_text()))
        return cast(list[dict[str, object]], document["events"])

    def reach_limit(self) -> int:
        self.running(self.start)
        now = self.start + 17 * 3600
        self.observe(now, "stopped")
        return now

    def test_guard_publishes_unavailable_with_recovered_limit_evidence(self) -> None:
        self.commit_settings()
        self.running(self.start)
        self.assertIsInstance(self.guard(self.start), ledger.SpendingAllowed)
        self.replace("daily_limit_usd", "broken")
        self.assertIsInstance(self.guard(self.start + 1), ledger.SpendingUnavailable)
        saved = self.saved_decision()
        self.assertEqual(saved["status"], "unavailable")
        self.assertEqual(saved["at"], self.start + 1)
        self.assertIn("daily_limit_usd", str(saved["reason"]))
        limits = cast(list[dict[str, object]], saved["limits"])
        self.assertEqual(limits[0]["origin"], "recovered")

    def test_guard_publishes_unavailable_when_state_evidence_expires(self) -> None:
        self.running(self.start)
        self.assertIsInstance(self.guard(self.start), ledger.SpendingAllowed)
        self.replace("observation_max_age_seconds", "60")
        self.assertIsInstance(self.guard(self.start + 61), ledger.SpendingUnavailable)
        self.assertEqual(self.saved_decision()["status"], "unavailable")
        self.assertIn("stale", str(self.saved_decision()["reason"]))

    def test_guard_retries_pending_delivery_and_preserves_delivered_state(self) -> None:
        now = self.reach_limit()
        attempts: list[ledger.LimitEvent] = []

        def refused(event: ledger.LimitEvent) -> bool:
            attempts.append(event)
            return False

        self.assertIsInstance(self.book.guard(self.settings, "admission", now, refused), ledger.SpendingRefused)
        self.assertEqual(self.saved_events()[0]["delivery"], "pending")
        self.book = ledger.CostLedger(self.state)
        self.assertIsInstance(self.guard(now), ledger.SpendingRefused)
        self.assertEqual([event.event_id for event in attempts], [event.event_id for event in self.notices])
        self.assertEqual(self.saved_events()[0]["delivery"], "delivered")
        self.assertIsInstance(self.guard(now), ledger.SpendingRefused)
        self.assertEqual(len(self.notices), 1)

    def test_guard_retries_delivery_after_notifier_exception(self) -> None:
        now = self.reach_limit()
        with mock.patch.object(self, "notice", side_effect=RuntimeError("fixture delivery failure")):
            self.assertIsInstance(self.guard(now), ledger.SpendingRefused)
        self.assertEqual(self.saved_events()[0]["delivery"], "pending")
        self.assertIsInstance(self.guard(now), ledger.SpendingRefused)
        self.assertEqual(len(self.notices), 1)

    def test_guard_retries_delivery_after_interruption(self) -> None:
        now = self.reach_limit()
        with mock.patch.object(self, "notice", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                _ = self.guard(now)
        self.book = ledger.CostLedger(self.state)
        self.assertIsInstance(self.guard(now), ledger.SpendingRefused)
        self.assertEqual(len(self.notices), 1)

    def test_guard_retries_delivery_after_visible_unconfirmed_publication(self) -> None:
        now = self.reach_limit()
        def unconfirmed(path: Path, text: str) -> None:
            write_text_atomic(path, text)
            raise AtomicDurabilityUnconfirmed("fixture directory sync failure")

        with mock.patch.object(ledger, "write_text_atomic", side_effect=unconfirmed):
            self.assertIsInstance(self.guard(now), ledger.SpendingUnavailable)
        self.assertEqual(self.notices, [])
        self.assertEqual(self.saved_events()[0]["delivery"], "pending")
        self.book = ledger.CostLedger(self.state)
        self.assertIsInstance(self.guard(now), ledger.SpendingRefused)
        self.assertEqual(len(self.notices), 1)

    def test_guard_sends_notices_without_holding_the_ledger_lock(self) -> None:
        now = self.reach_limit()

        def unlocked_notice(event: ledger.LimitEvent) -> bool:
            with (self.state / "cost-ledger.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.notices.append(event)
                fcntl.flock(lock, fcntl.LOCK_UN)
            return True

        self.assertIsInstance(self.book.guard(self.settings, "admission", now, unlocked_notice), ledger.SpendingRefused)
        self.assertEqual(len(self.notices), 1)
        self.assertEqual(self.saved_events()[0]["delivery"], "delivered")

    def test_guard_reconciles_only_the_overlapping_charge_seconds(self) -> None:
        self.replace("daily_limit_usd", "3.50")
        self.replace("limit_reserve_usd", "0")
        self.running(self.start)
        now = self.start + 7200
        self.observe(now, "stopped")
        self.book.reconcile(ledger.CostReconciliation(
            now, self.start, self.start + 3600, Decimal("3"), "first hour bill"))
        result = self.guard(now)
        self.assertIsInstance(result, ledger.SpendingRefused)
        self.assertIsInstance(result.evidence.costs, ledger.CostEstimate)
        if isinstance(result.evidence.costs, ledger.CostEstimate):
            self.assertEqual(result.evidence.costs.daily.usd, Decimal("4"))

    def test_guard_charges_stopped_evidence_gaps_at_the_fallback_price(self) -> None:
        self.replace("observation_max_age_seconds", "300")
        self.replace("limit_reserve_usd", "0")
        self.inventory(self.start)
        self.observe(self.start, "stopped")
        self.book.record_price(ledger.PriceSample(self.start, Decimal("0"), "stale cheap price"))
        now = self.start + 21 * 3600
        self.observe(now, "stopped")
        result = self.guard(now)
        self.assertIsInstance(result, ledger.SpendingRefused)
        if isinstance(result.evidence.costs, ledger.CostEstimate):
            expected = Decimal(21 * 3600 - 300) / Decimal(3600)
            self.assertEqual(result.evidence.costs.daily.usd, expected)

    def test_guard_keeps_close_stopped_observations_free_of_instance_charges(self) -> None:
        self.replace("observation_max_age_seconds", "300")
        self.inventory(self.start)
        self.observe(self.start, "stopped")
        self.observe(self.start + 299, "stopped")
        self.assertEqual(self.estimate(self.start + 299).daily.usd, Decimal(0))

    def test_guard_limits_published_intervals_without_changing_current_totals(self) -> None:
        old = instant("2025-12-31T23:30:00+00:00")
        current = instant("2026-01-01T00:00:00+00:00")
        now = current + 1800
        self.running(old)
        self.book.record_price(ledger.PriceSample(old, Decimal("0.5"), "carry-in price"))
        self.observe(now, "stopped")
        self.book.reconcile(ledger.CostReconciliation(
            now, old, now, Decimal("2"), "cross-month bill"))
        full = self.estimate(now)
        result = self.guard(now)
        if isinstance(result.evidence.costs, ledger.CostEstimate):
            self.assertEqual(result.evidence.costs.daily, full.daily)
            self.assertEqual(result.evidence.costs.monthly, full.monthly)
        else:
            self.fail("Current accounting evidence is unavailable")
        published = cast(list[dict[str, object]], self.saved_decision()["intervals"])
        self.assertTrue(published)
        self.assertTrue(all(cast(int, row["start"]) >= current for row in published))
        self.assertEqual(len(cast(list[object], json.loads(self.book.path.read_text())["observations"])), 2)


if __name__ == "__main__":
    _ = unittest.main()
