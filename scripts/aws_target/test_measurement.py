"""Check cost reconciliation and AWS wait estimates with local evidence."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
import json
import os
import re
import time
import subprocess
from typing import cast
from unittest import mock
import unittest

from scripts.aws_target import aws_target, config as aws_config, ledger, measurement
from scripts.aws_target.measurement_test_support import DAY, MIDNIGHT, NOW, MeasurementFixture, narrow, read_only_evidence
from scripts.aws_target.ledger_test_support import instant
from scripts.aws_target.control_test_support import AwsReplies

from scripts.buildlog import aws_report, index, report


class MeasurementTests(MeasurementFixture):
    def test_missing_charge_tag_refuses_the_guard_before_any_fetch(self) -> None:
        self.seed_cost()
        self.remove("cost_explorer_tag_key")
        decision = self.book().guard(self.settings, "admission", NOW, lambda _event: True)
        decision = narrow(self, decision, ledger.SpendingUnavailable)
        self.assertIn("cost_explorer_tag_key", decision.reason)

    def test_newest_pending_reply_recovers_without_reading_corrupt_older_charges(self) -> None:
        self.seed_previous_day()
        fetch = mock.Mock(return_value=Decimal(2))
        with mock.patch.object(ledger.CostLedger, "_save", side_effect=OSError("confirmation failed")), self.assertRaises(OSError):
            _ = measurement.read_charges(self.settings, self.state, NOW, fetch)
        older = self.state / "charges" / "2026-01-13.json"
        _ = older.write_text("{broken")
        _ = (self.state / "deployment.json").write_text(json.dumps({
            "region": "us-east-1", "outputs": {"InstanceId": "i-fixture"}}))
        replies = AwsReplies()
        controller = aws_target.Controller(self.settings, self.state, self.root / "admission", execute=replies,
            clock=lambda: float(NOW + 1), notify=lambda _event: True, publish=lambda _envelope: True)
        _ = controller.tick()
        fetch.assert_called_once()
        self.assertEqual(replies.count("get-cost-and-usage"), 0)
        self.assertEqual(len(self.book().read_snapshot().reconciliations), 1)
        state = cast(dict[str, object], json.loads((self.state / "controller.json").read_text()))
        self.assertNotIn("charge_error", state)
        self.assertEqual(older.read_text(), "{broken")

    def test_report_day_cost_matches_full_history_with_whole_day_reconciliation(self) -> None:
        self.costs({"cost_explorer_read_usd": "0.01"})
        self.seed_previous_day()
        self.seed_cost()
        self.book().reconcile(ledger.CostReconciliation(NOW, MIDNIGHT - 86400, MIDNIGHT, Decimal(3), "actual"))
        settings = aws_config.read_settings(self.settings)
        full = ledger.costs_from_evidence(self.book().read_snapshot(), settings, NOW, current_window=False)
        for day, start, end in (("2026-01-14", MIDNIGHT - 86400, MIDNIGHT), (DAY, MIDNIGHT, NOW)):
            with self.subTest(day=day):
                result = narrow(self, measurement.daily_usage(self.settings, self.state, day, NOW), measurement.DailyUsage)
                self.assertEqual(result.usd, ledger.period_cost(full.intervals, start, end))

    def test_concurrent_charge_refresh_uses_one_query_and_one_reconciliation(self) -> None:
        self.seed_previous_day()
        fetch = mock.Mock(return_value=Decimal(2))
        def refresh(_number: int) -> measurement.ChargeRead | measurement.MeasurementUnavailable:
            return measurement.read_charges(self.settings, self.state, NOW, fetch)
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(refresh, (0, 1)))
        self.assertEqual(results[0], results[1])
        fetch.assert_called_once()
        self.assertEqual(len(self.book().read_snapshot().reconciliations), 1)
        self.assertFalse((self.state / "charges.lock").exists())

    def test_utc_reconciliation_prices_both_overlapping_new_york_days(self) -> None:
        self.text_setting("accounting_zone", "America/New_York")
        self.costs({"fallback_usd_per_hour": "0", "price_max_age_seconds": "86400"})
        start, end = MIDNIGHT, MIDNIGHT + 86400
        now = end + 6 * 3600
        book = self.book()
        book.record_inventory(ledger.ResourceInventory(
            start, (ledger.PricedResource("volume", Decimal(0)),), "fixture"))
        book.record_price(ledger.PriceSample(start, Decimal(1), "fixture"))
        book.observe(ledger.InstanceObservation(start, "running", "fixture"))
        book.observe(ledger.InstanceObservation(start + 12 * 3600, "stopped", "fixture"))
        book.observe(ledger.InstanceObservation(end, "stopped", "fixture"))
        book.reconcile(ledger.CostReconciliation(now, start, end, Decimal(24), "actual"))
        settings = aws_config.read_settings(self.settings)
        full = ledger.costs_from_evidence(book.read_snapshot(), settings, now, current_window=False)
        with read_only_evidence(self, self.state):
            for day, expected in (("2026-01-14", Decimal("7.5")), (DAY, Decimal("16.5"))):
                with self.subTest(day=day):
                    result = narrow(self, measurement.daily_usage(
                        self.settings, self.state, day, now), measurement.DailyUsage)
                    self.assertEqual(result.usd, expected)
                    left, right = ledger.period_bounds(date.fromisoformat(day), settings.accounting_zone)
                    self.assertEqual(result.usd, ledger.period_cost(full.intervals, left, right))

    def test_uptime_includes_pending_and_stopping_and_caps_stale_evidence(self) -> None:
        self.costs({"observation_max_age_seconds": "60"})
        book = self.book()
        book.record_inventory(ledger.ResourceInventory(MIDNIGHT, (ledger.PricedResource("volume", Decimal(0)),), "fixture"))
        observations: tuple[tuple[int, ledger.InstanceState], ...] = (
            (MIDNIGHT, "pending"), (MIDNIGHT + 30, "running"), (MIDNIGHT + 60, "stopping"))
        for at, state in observations:
            book.observe(ledger.InstanceObservation(at, state, "fixture"))
        result = narrow(self, measurement.daily_usage(self.settings, self.state, DAY, MIDNIGHT + 600), measurement.DailyUsage)
        self.assertEqual(result.uptime_s, 120)

    def test_tick_does_not_persist_an_unused_charge_error(self) -> None:
        self.seed_cost()
        _ = (self.state / "deployment.json").write_text(json.dumps({
            "region": "us-east-1", "outputs": {"InstanceId": "i-fixture"}}))
        replies = AwsReplies()
        controller = aws_target.Controller(self.settings, self.state, self.root / "admission", execute=replies,
            clock=lambda: float(NOW + 1), notify=lambda _event: True, publish=lambda _envelope: True)
        _ = controller.tick()
        self.assertEqual(replies.count("get-cost-and-usage"), 1)
        state = cast(dict[str, object], json.loads((self.state / "controller.json").read_text()))
        self.assertNotIn("charge_error", state)

    def test_charge_difference_is_derived_and_legacy_difference_still_reads(self) -> None:
        self.seed_previous_day()
        reading = narrow(self, measurement.read_charges(self.settings, self.state, NOW, mock.Mock(return_value=Decimal(3))), measurement.ChargeRead)
        path = self.state / "charges" / "2026-01-14.json"
        row = cast(dict[str, object], json.loads(path.read_text()))
        self.assertNotIn("difference_usd", row)
        self.assertEqual(reading.difference_usd, reading.actual_usd - reading.estimated_usd)
        row["difference_usd"] = str(reading.difference_usd)
        _ = path.write_text(json.dumps(row))
        self.assertEqual(measurement.read_charge(path), reading)

    def test_wait_estimate_uses_the_report_local_day_instead_of_the_accounting_day(self) -> None:
        self.text_setting("accounting_zone", "Asia/Tokyo")
        departure = instant("2026-01-16T02:00:00+00:00")
        local = self.local_step("local-day", 300, 100, started_at="2026-01-16T01:00:00+00:00")
        self.write_records(local, self.call("local-day"), self.call("fallback-call"), self.remote_step(),
            self.attempt(queued_at=departure - 100, departed_at=departure))
        _ = index.update()
        try:
            with mock.patch.dict(os.environ, {"TZ": "America/New_York"}):
                time.tzset()
                with closing(index.read_only()) as connection:
                    result = aws_report.estimate_wait_saved(connection, DAY, bounds=report.day_range(DAY))
            self.assertEqual(result.steps, 1)
            self.assertEqual(result.queue_saved_s, Decimal(200))
        finally:
            time.tzset()

    def test_charge_query_uses_whole_utc_days_and_reconciles_the_queried_period(self) -> None:
        cases = (
            ("America/New_York", "2026-11-02T00:00:01-05:00",
             "2026-11-01", "2026-11-02"),
            ("Asia/Tokyo", "2026-01-16T00:00:01+09:00",
             "2026-01-14", "2026-01-15"),
        )
        for zone, when, first, end in cases:
            with self.subTest(zone=zone):
                self.text_setting("accounting_zone", zone)
                directory = self.state / zone.replace("/", "-")
                book = ledger.CostLedger(directory)
                book.record_inventory(ledger.ResourceInventory(
                    instant(first + "T00:00:00+00:00"), (ledger.PricedResource("volume", Decimal(0)),), "fixture"))
                book.observe(ledger.InstanceObservation(instant(first + "T00:00:00+00:00"), "stopped", "fixture"))

                def date_only_api(arguments: Sequence[str], *, capture_output: bool, text: bool,
                                  timeout: float, check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
                    self.assertIsNone(env)
                    self.assertTrue(capture_output and text)
                    self.assertGreater(timeout, 0)
                    self.assertFalse(check)
                    period = arguments[arguments.index("--time-period") + 1]
                    fields = dict(field.split("=", 1) for field in period.split(","))
                    for key in ("Start", "End"):
                        if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", fields[key]) is None:
                            raise ValueError("Cost Explorer requires YYYY-MM-DD")
                    return subprocess.CompletedProcess(arguments, 0, json.dumps({
                        "ResultsByTime": [{"Total": {"UnblendedCost": {"Amount": "2", "Unit": "USD"}}}]}), "")

                execute = mock.Mock(side_effect=date_only_api)
                controller = aws_target.Controller(self.settings, directory, self.root / "admission", execute=execute)
                result = controller.refresh_charges(aws_target.AwsCli('build-scaler', 'us-east-1', 30, execute), instant(when))
                result = narrow(self, result, measurement.ChargeRead)
                arguments = cast(Sequence[str], execute.call_args.args[0])
                period = arguments[arguments.index("--time-period") + 1]
                fields = dict(field.split("=", 1) for field in period.split(","))
                self.assertEqual(fields, {"Start": first, "End": end})
                queried = (instant(fields["Start"] + "T00:00:00+00:00"),
                           instant(fields["End"] + "T00:00:00+00:00"))
                self.assertEqual((result.period_start, result.period_end), queried)
                reconciled = book.read_snapshot().reconciliations
                self.assertEqual(len(reconciled), 1)
                self.assertEqual((reconciled[0].period_start, reconciled[0].period_end), queried)

    def test_saved_reply_reconciles_after_midnight_before_the_next_query(self) -> None:
        self.seed_cost()
        self.seed_previous_day(hours=0)
        fetch = mock.Mock(return_value=Decimal("1.25"))
        before_midnight = MIDNIGHT + 86400 - 1
        with mock.patch.object(ledger.CostLedger, "_save", side_effect=OSError("ledger write failed")), \
             self.assertRaises(OSError):
            _ = measurement.read_charges(self.settings, self.state, before_midnight, fetch)
        _ = measurement.read_charges(self.settings, self.state, before_midnight + 2, fetch)
        fetch.assert_called_once()
        reconciliations = self.book().read_snapshot().reconciliations
        self.assertEqual(len(reconciliations), 1)
        self.assertEqual((reconciliations[0].period_start, reconciliations[0].period_end),
                         (MIDNIGHT - 86400, MIDNIGHT))

    def test_query_fee_stays_in_total_after_actual_charge_reconciliation(self) -> None:
        self.costs({"cost_explorer_read_usd": "0.01"})
        self.seed_previous_day()
        _ = measurement.read_charges(self.settings, self.state, NOW, mock.Mock(return_value=Decimal(2)))
        result = measurement.daily_usage(self.settings, self.state, "2026-01-14", NOW)
        result = narrow(self, result, measurement.DailyUsage)
        self.assertEqual(result.usd, Decimal("2.01"))

    def test_admission_refusal_does_not_count_or_remove_valid_savings(self) -> None:
        self.comparison_rows()
        self.write_records(self.attempt(), self.call("fallback-call"), self.remote_step(),
                           self.attempt("refused", outcome="declined", completion="missing"))
        _ = index.update()
        with closing(index.read_only()) as connection:
            result = aws_report.estimate_wait_saved(connection, DAY, bounds=report.day_range(DAY))
        self.assertEqual(result.steps, 1)
        self.assertEqual(result.queue_saved_s, Decimal(300))
        self.assertEqual(result.run_saved_s, Decimal(-50))

    def test_wait_estimate_matches_repository_kind_day_and_preserves_a_slower_run(self) -> None:
        self.comparison_rows()
        self.write_records(self.attempt(), self.call("fallback-call"), self.remote_step())
        _ = index.update()
        with closing(index.read_only()) as connection:
            result = aws_report.estimate_wait_saved(connection, DAY, bounds=report.day_range(DAY))
        self.assertEqual(result.steps, 1)
        self.assertEqual(result.queue_saved_s, Decimal(300))
        self.assertEqual(result.run_saved_s, Decimal(-50))
        self.assertIsInstance(result.queue_saved_s, Decimal)

    def test_missing_comparisons_never_manufacture_savings(self) -> None:
        self.write_records(self.attempt(), self.call("fallback-call"), self.remote_step())
        _ = index.update()
        with closing(index.read_only()) as connection:
            result = aws_report.estimate_wait_saved(connection, DAY, bounds=report.day_range(DAY))
        self.assertIsInstance(result.queue_saved_s, measurement.MeasurementUnavailable)
        self.assertIsInstance(result.run_saved_s, measurement.MeasurementUnavailable)
        self.assertEqual(result.steps, 1)

    def test_attempt_outcomes_and_targets_survive_indexing_after_local_fallback(self) -> None:
        outcomes = ("passed", "failed", "lost", "declined", "no_tests")
        for number, outcome in enumerate(outcomes):
            self.write_records(self.attempt(str(number), outcome=outcome, slot=number + 1))
        self.write_records(self.call("fallback-call", target="natedev"))
        _ = index.update()
        with closing(index.read_only()) as connection:
            rows = cast(list[tuple[str, str, int]], connection.execute(
                "SELECT target,outcome,slot FROM ssh_attempts ORDER BY id").fetchall())
            final = cast(list[tuple[str]], connection.execute(
                "SELECT target FROM calls WHERE id='fallback-call'").fetchall())
        self.assertEqual(rows, [("aws", outcome, number + 1) for number, outcome in enumerate(outcomes)])
        self.assertEqual(final, [("natedev",)])

    def test_read_measurement_uses_full_month_evidence_without_commands_or_writes(self) -> None:
        self.seed_cost()
        self.seed_previous_day()
        _ = (self.state / "controller.json").write_text(json.dumps({
            "state": "stopped", "at": NOW, "generation": "i-fixture:boot-a", "reason": "idle"}))
        with read_only_evidence(self, self.state):
            result = measurement.read_measurement(self.settings, self.state, NOW)
            usage = measurement.daily_usage(self.settings, self.state, DAY, NOW)
        result = narrow(self, result, measurement.AwsMeasurement)
        costs = narrow(self, result.costs, ledger.CostEstimate)
        self.assertEqual(costs.monthly.usd, Decimal(3))
        self.assertEqual(costs.daily.usd, Decimal(2))
        usage = narrow(self, usage, measurement.DailyUsage)
        self.assertEqual(usage.uptime_s, 7200)

    def test_corrupt_ledger_preserves_evidence_and_returns_unavailable(self) -> None:
        self.seed_cost()
        _ = self.book().path.write_bytes(b'{"observations":broken\n')
        before = self.snapshot_files(self.state)
        result = measurement.read_measurement(self.settings, self.state, NOW)
        self.assertIsInstance(result, measurement.MeasurementUnavailable)
        self.assertEqual(self.snapshot_files(self.state), before)

    def test_missing_state_reader_creates_no_coordination_files(self) -> None:
        absent = self.root / "absent-state"
        result = measurement.read_measurement(self.settings, absent, NOW)
        result = narrow(self, result, measurement.AwsMeasurement)
        self.assertIsInstance(result.costs, ledger.CostEvidenceUnavailable)
        self.assertIsInstance(result.permission, ledger.PermissionUnavailable)
        self.assertFalse(absent.exists())

    def test_daily_charge_read_reconciles_once_and_does_not_add_actual_cost_twice(self) -> None:
        self.costs({"cost_explorer_read_usd": "0.01", "cost_explorer_interval_seconds": "172800"})
        start = MIDNIGHT - 86400
        self.seed_previous_day()
        fetch = mock.Mock(return_value=Decimal(2))

        first = measurement.read_charges(self.settings, self.state, NOW, fetch)
        second = measurement.read_charges(self.settings, self.state, NOW + 1, fetch)
        self.assertEqual(fetch.call_args_list, [mock.call(start, MIDNIGHT)])
        self.assertEqual(second, first)
        first = narrow(self, first, measurement.ChargeRead)
        self.assertEqual((first.actual_usd, first.estimated_usd, first.difference_usd, first.query_usd),
                         (Decimal(2), Decimal("1.01"), Decimal("0.99"), Decimal("0.01")))
        snapshot = ledger.CostLedger(self.state).read_snapshot()
        self.assertEqual(len(snapshot.reconciliations), 1)
        result = measurement.read_measurement(self.settings, self.state, NOW + 1)
        result = narrow(self, result, measurement.AwsMeasurement)
        costs = narrow(self, result.costs, ledger.CostEstimate)
        self.assertEqual(costs.monthly.usd, Decimal("2.01"))

    def test_saved_charge_reply_survives_a_failed_reconciliation_without_another_query(self) -> None:
        self.seed_cost()
        self.seed_previous_day(hours=0)
        fetch = mock.Mock(return_value=Decimal("1.25"))
        with mock.patch.object(ledger.CostLedger, "_save", side_effect=OSError("ledger write failed")), \
             self.assertRaises(OSError):
            _ = measurement.read_charges(self.settings, self.state, NOW, fetch)
        result = measurement.read_charges(self.settings, self.state, NOW + 1, fetch)
        self.assertIsInstance(result, measurement.ChargeRead)
        fetch.assert_called_once()
        self.assertEqual(len(self.book().read_snapshot().reconciliations), 1)

    def test_charge_day_uses_utc_across_a_25_hour_accounting_day(self) -> None:
        self.text_setting("accounting_zone", "America/New_York")
        self.seed_cost()
        at = instant("2026-11-02T00:00:01-05:00")
        fetch = mock.Mock(return_value=Decimal(0))

        result = measurement.read_charges(self.settings, self.state, at, fetch)
        result = narrow(self, result, measurement.ChargeRead)
        self.assertEqual(result.day, "2026-11-01")
        self.assertEqual(fetch.call_args_list, [mock.call(instant("2026-11-01T00:00:00+00:00"), instant("2026-11-02T00:00:00+00:00"))])
        self.assertEqual(result.period_end - result.period_start, 24 * 3600)

    def test_disabled_charge_reader_never_queries_or_creates_state(self) -> None:
        self.text_setting("offload", "off")
        absent = self.root / "disabled-state"
        fetch = mock.Mock(side_effect=AssertionError("disabled AWS must remain inert"))
        result = measurement.read_charges(self.settings, absent, NOW, fetch)
        self.assertIsInstance(result, measurement.MeasurementUnavailable)
        self.assertFalse(absent.exists())
        fetch.assert_not_called()

    def test_controller_schedules_one_tagged_charge_query_and_local_reports_never_repeat_it(self) -> None:
        self.seed_cost()
        self.text_setting("cost_explorer_tag_key", "Project")
        self.text_setting("cost_explorer_tag_value", "fixture-build")
        calls: list[list[str]] = []

        def execute(arguments: Sequence[str], *, capture_output: bool, text: bool,
                    timeout: float, check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
            self.assertIsNone(env)
            self.assertTrue(capture_output and text)
            self.assertGreater(timeout, 0)
            self.assertFalse(check)
            calls.append(list(arguments))
            return subprocess.CompletedProcess(arguments, 0, json.dumps({"ResultsByTime": [
                {"Total": {"UnblendedCost": {"Amount": "2.50", "Unit": "USD"}}}]}), "")

        controller = aws_target.Controller(self.settings, self.state, self.root / "admission",
                                           execute=execute, clock=lambda: float(NOW + 86400))
        first = controller.refresh_charges(aws_target.AwsCli('build-scaler', 'us-east-1', 30, execute), NOW + 86400)
        second = controller.refresh_charges(aws_target.AwsCli('build-scaler', 'us-east-1', 30, execute), NOW + 86401)
        self.assertIsInstance(first, measurement.ChargeRead)
        self.assertEqual(second, first)
        self.assertEqual(len(calls), 1)
        arguments = calls[0]
        self.assertEqual(arguments[arguments.index("--profile") + 1], "build-scaler")
        self.assertEqual(arguments[arguments.index("--region") + 1], "us-east-1")
        self.assertIn("ce", arguments)
        self.assertIn("get-cost-and-usage", arguments)
        self.assertEqual(json.loads(arguments[arguments.index("--filter") + 1]),
                         {"Tags": {"Key": "Project", "Values": ["fixture-build"]}})
        _ = measurement.read_measurement(self.settings, self.state, NOW + 86401)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    _ = unittest.main()
