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
from scripts.aws_target.ledger_test_support import account_reply, instant
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
        fetch = self.charge_fetch(Decimal(2))
        with mock.patch.object(ledger.CostLedger, "_save", side_effect=OSError("confirmation failed")), self.assertRaises(OSError):
            _ = measurement.read_charges(self.settings, self.state, NOW, fetch)
        older = self.state / "charges" / "2026-01-13.json"
        _ = older.write_text("{broken")
        _ = (self.state / "deployment.json").write_text(json.dumps({
            "region": "us-east-1", "outputs": {"InstanceId": "i-fixture"}}))
        replies = AwsReplies()
        controller = aws_target.AccountRefresh(self.settings, self.state, execute=replies, clock=lambda: float(NOW + 1))
        _ = controller.tick()
        fetch.assert_called_once()
        self.assertEqual(replies.count("get-cost-and-usage"), 0)
        self.assertEqual(len(self.book().read_snapshot().reconciliations), 1)
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
        fetch = self.charge_fetch(Decimal(2))
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

        reading = ledger.ChargeRead(DAY, now, start, end, Decimal(24), Decimal(0), Decimal(1),
                                   (ledger.UsageTypeCost('storage', Decimal(24)),))
        _ = book.refresh_charges(self.settings, now, lambda _start, _end: (reading,))
        with read_only_evidence(self, self.state):
            for day, expected in (('2026-01-14', Decimal(25)), (DAY, Decimal(24)), ('2026-01-16', Decimal(1))):
                with self.subTest(day=day, source='account bill'):
                    result = narrow(self, measurement.daily_usage(
                        self.settings, self.state, day, now), measurement.DailyUsage)
                    self.assertEqual(result.usd, expected)

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


    def test_charge_difference_is_derived_and_legacy_difference_still_reads(self) -> None:
        self.seed_previous_day()
        reading = narrow(self, measurement.read_charges(self.settings, self.state, NOW, self.charge_fetch(Decimal(3))), measurement.ChargeRead)
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
                    return subprocess.CompletedProcess(arguments, 0, json.dumps(account_reply(arguments, "2")), "")

                execute = mock.Mock(side_effect=date_only_api)
                controller = aws_target.AccountRefresh(self.settings, directory, execute=execute)
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
        fetch = self.charge_fetch(Decimal("1.25"))
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
        _ = measurement.read_charges(self.settings, self.state, NOW, self.charge_fetch(Decimal(2)))
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
        fetch = self.charge_fetch(Decimal(2))

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
        fetch = self.charge_fetch(Decimal("1.25"))
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
        fetch = self.charge_fetch(Decimal(0))

        result = measurement.read_charges(self.settings, self.state, at, fetch)
        result = narrow(self, result, measurement.ChargeRead)
        self.assertEqual(result.day, "2026-11-01")
        self.assertEqual(fetch.call_args_list, [mock.call(instant("2026-11-01T00:00:00+00:00"), instant("2026-11-02T00:00:00+00:00"))])
        self.assertEqual(result.period_end - result.period_start, 24 * 3600)


    def test_controller_schedules_one_account_charge_query_and_local_reports_never_repeat_it(self) -> None:
        self.seed_cost()
        self.costs({'cost_explorer_lookback_days': '7', 'cost_explorer_read_usd': '0.01'})
        calls: list[list[str]] = []
        amount = '0'

        def execute(arguments: Sequence[str], *, capture_output: bool, text: bool,
                    timeout: float, check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
            self.assertIsNone(env)
            self.assertTrue(capture_output and text)
            self.assertGreater(timeout, 0)
            self.assertFalse(check)
            calls.append(list(arguments))
            reply = account_reply(arguments, amount)
            if '--next-page-token' not in arguments:
                reply['NextPageToken'] = 'second-page'
                for row in ledger.rows(reply, 'ResultsByTime'):
                    row['Estimated'] = True
            else:
                self.assertEqual(arguments[arguments.index('--next-page-token') + 1], 'second-page')
            return subprocess.CompletedProcess(arguments, 0, json.dumps(reply), '')

        controller = aws_target.AccountRefresh(self.settings, self.state, execute=execute)
        api = aws_target.AwsCli('build-scaler', 'us-east-1', 30, execute)
        first = narrow(self, controller.refresh_charges(api, NOW + 86400), measurement.ChargeRead)
        second = controller.refresh_charges(api, NOW + 86401)
        self.assertEqual(second, first)
        self.assertEqual(len(calls), 2)
        self.assertEqual(first.actual_usd, Decimal(0))
        self.assertEqual(first.query_usd, Decimal('0.02'))
        self.assertTrue(first.provisional)
        self.assertEqual(len(list((self.state / 'charges').glob('*.json'))), 7)
        arguments = calls[0]
        self.assertEqual(arguments[arguments.index('--profile') + 1], 'build-scaler')
        self.assertEqual(arguments[arguments.index('--region') + 1], 'us-east-1')
        self.assertEqual(arguments[arguments.index('--time-period') + 1], 'Start=2026-01-09,End=2026-01-16')
        self.assertIn('ce', arguments)
        self.assertIn('get-cost-and-usage', arguments)
        self.assertEqual(arguments[arguments.index('--filter') + 1], aws_target.USAGE_ONLY)
        self.assertIn('--no-paginate', arguments)
        self.assertEqual(arguments[arguments.index('--metrics') + 1:arguments.index('--group-by')],
                         ['UnblendedCost', 'UsageQuantity'])
        self.assertEqual(arguments[arguments.index('--group-by') + 1], 'Type=DIMENSION,Key=USAGE_TYPE')
        _ = measurement.read_measurement(self.settings, self.state, NOW + 86401)
        self.assertEqual(len(calls), 2)

        amount = '1.25'
        _ = controller.refresh_charges(api, NOW + 2 * 86400)
        replaced = ledger.read_charge(self.state / 'charges' / '2026-01-15.json')
        self.assertEqual(replaced.actual_usd, Decimal('2.50'))
        self.assertEqual(replaced.usage_types, (ledger.UsageTypeCost('USE2-SpotUsage:m7a.4xlarge', Decimal('2.50'), Decimal(2), 'Hrs'),))
        self.assertEqual(replaced.at, NOW + 2 * 86400)
        self.assertEqual(len(calls), 4)
        self.assertEqual(sum(row.query_usd for row in self.book().read_snapshot().reconciliations), Decimal('0.04'))
        _ = controller.refresh_charges(api, NOW + 2 * 86400 + 1)
        self.assertEqual(len(calls), 4)
        self.assertEqual(sum(row.query_usd for row in self.book().read_snapshot().reconciliations), Decimal('0.04'))

        with mock.patch.object(api, 'call', return_value={'ResultsByTime': []}), self.assertRaisesRegex(ValueError, 'daily reply is unavailable'):
            _ = controller.refresh_charges(api, NOW + 3 * 86400)


if __name__ == "__main__":
    _ = unittest.main()
