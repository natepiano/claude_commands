"""Cost evidence must survive retries and refuse unsafe spending."""
from __future__ import annotations

from collections.abc import Sequence
from contextlib import redirect_stderr
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
import fcntl
import io
import json
from pathlib import Path
import subprocess
from threading import Event
from typing import cast, override
from unittest import mock
import unittest
from zoneinfo import ZoneInfo

from scripts.aws_target import config, gate_trial, ledger
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

    def test_shutting_down_persists_without_compute_charges(self) -> None:
        self.running(self.start)
        self.book.record_price(ledger.PriceSample(self.start, Decimal("0.75"), "Spot fixture"))
        self.observe(self.start + 60, "shutting-down")
        self.book = ledger.CostLedger(self.state)
        self.assertEqual(self.book.read_snapshot().observations[-1].state, "shutting-down")
        self.observe(self.start + 120, "terminated")
        costs = self.estimate(self.start + 120)
        self.assertEqual(sum(r.end - r.start for r in costs.intervals if r.kind == "instance"), 60)
        self.assertEqual(costs.monthly.usd, Decimal("0.0125"))
        self.assertEqual(self.estimate(self.start + 86400).monthly.usd, costs.monthly.usd)

    def test_unknown_observation_rejection_keeps_evidence_and_allows_later_charging(self) -> None:
        self.running(self.start)
        before = self.book.path.read_bytes()
        with self.assertRaises(ledger.ObservationRejected):
            self.observe(self.start + 1800, "unknown")
        self.assertEqual(self.book.path.read_bytes(), before)
        self.observe(self.start + 3600, "stopped")
        self.assertEqual(self.estimate(self.start + 3600).monthly.usd, Decimal("1"))
        self.assertIsInstance(self.guard(self.start + 3600), ledger.SpendingAllowed)
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

        self.book.record_inventory(ledger.ResourceInventory(self.start + 43200,
            (ledger.PricedResource('gate-shared', Decimal(1)),), 'builder removed', builder_removed=True))
        self.observe(self.start + 43200, 'terminated')
        self.book = ledger.CostLedger(self.state)
        self.assertTrue(self.book.read_snapshot().inventories[-1].builder_removed)
        self.assertEqual(self.estimate(self.start + 86400).monthly.usd, Decimal('0.5') + Decimal(1) / Decimal(62))
        self.book.record_inventory(ledger.ResourceInventory(self.start + 86400, (), 'no resources', builder_removed=True))
        self.assertEqual(self.estimate(self.start + 90000).monthly.usd, self.estimate(self.start + 86400).monthly.usd)

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

    def test_daily_spend_preserves_provisional_bill_state(self) -> None:
        self.inventory(self.start - 86400)
        self.observe(self.start - 86400, 'stopped')
        reading = ledger.ChargeRead('2026-01-14', self.start, self.start - 86400, self.start,
            Decimal(1), Decimal(0), Decimal(0), (ledger.UsageTypeCost('storage', Decimal(1)),))
        directory = self.state / 'charges'
        directory.mkdir()
        for provisional in (True, False):
            with self.subTest(provisional=provisional):
                _ = (directory / '2026-01-14.json').write_text(json.dumps(
                    ledger.charge_record(replace(reading, provisional=provisional))))
                days = {row.day: row for row in self.book.spend_days(config.read_settings(self.settings), self.start + 120)}
                self.assertEqual(days['2026-01-14'].provisional_bill, provisional)
                self.assertEqual(days['2026-01-14'].source, 'AWS bill')
                self.assertFalse(days['2026-01-15'].provisional_bill)
                self.assertEqual(days['2026-01-15'].source, 'estimated from the newest bill')

    def rate_bill(self, day: str, rate: str) -> ledger.ChargeRead:
        start = instant(day + 'T00:00:00+00:00')
        reading = ledger.ChargeRead(day, start + 86400, start, start + 86400,
            Decimal(rate), Decimal(0), Decimal(0), (
                ledger.UsageTypeCost('USE2-SpotUsage:m7a.8xlarge', Decimal(rate), Decimal(1), 'Hrs'),))
        directory = self.state / 'charges'
        directory.mkdir(exist_ok=True)
        _ = (directory / (day + '.json')).write_text(json.dumps(ledger.charge_record(reading)))
        return reading

    def allowance_bill(self, day: str, prefix: str = 'USE2-', *, multiplier: Decimal | int = 1) -> ledger.ChargeRead:
        start = instant(day + 'T00:00:00+00:00')
        reading = ledger.ChargeRead(day, start + 86400, start, start + 86400,
            Decimal('1.554') * multiplier, Decimal(0), Decimal(0), (
                ledger.UsageTypeCost(prefix + 'SpotUsage:m7a.8xlarge', Decimal('.8'), Decimal(1), 'Hrs'),
                ledger.UsageTypeCost(prefix + 'PublicIPv4:InUseAddress', Decimal('.6') * multiplier, Decimal(100), 'Hrs'),
                ledger.UsageTypeCost(prefix + 'EBS:VolumeUsage.gp3', Decimal('148.8') * multiplier, Decimal(200), 'GB-Month'),
                ledger.UsageTypeCost('Requests-Tier1', Decimal('.001') * multiplier, Decimal(1), 'Requests'),
                ledger.UsageTypeCost('USE2-Requests-Tier2', Decimal('.001') * multiplier, Decimal(1), 'Requests'),
                ledger.UsageTypeCost('TimedStorage-ByteHrs', Decimal('.001') * multiplier, Decimal(1), 'GB-Month'),
                ledger.UsageTypeCost('USW2-DataTransfer-Out-Bytes', Decimal('.001') * multiplier, Decimal(1), 'GB')))
        directory = self.state / 'charges'
        directory.mkdir(exist_ok=True)
        _ = (directory / (day + '.json')).write_text(json.dumps(ledger.charge_record(reading)))
        return reading

    def allowance_gate(self, *, region: str = 'us-east-2') -> ledger.GateReservation:
        self.inventory(self.start)
        self.observe(self.start, 'stopped')
        _ = (self.root / 'cloud_gate.conf').write_text('regions=us-east-2 us-west-2\n# whole: 1..\nroot_gib=40\n')
        request = ledger.GateReservation('allowance', self.start + 3600, self.start + 7200, Decimal('.8'), Decimal('.02'),
                                         instance_type='m7a.8xlarge', region=region)
        self.assertIsInstance(self.book.reserve_gate(self.settings, request), ledger.GateReserved)
        _ = (self.state / 'cloud-gate-launches.jsonl').write_text(''.join(json.dumps(
            {'attempt': 'allowance' if number == 0 else str(number), 'time': request.at,
             'instance_type': request.instance_type, 'region': region, 'result': 'launched'}) + '\n'
            for number in range(4)))
        return request

    def test_bill_does_not_remove_the_open_allowance_from_admission(self) -> None:
        self.inventory(self.start)
        self.observe(self.start, 'stopped')
        self.costs({'daily_limit_usd': '.28', 'monthly_limit_usd': '400', 'limit_reserve_usd': '0'})
        _ = self.rate_bill('2026-01-14', '.8')
        request = ledger.GateReservation('open', self.start, self.start + 1200, Decimal('.8'), Decimal('.02'),
                                         instance_type='m7a.8xlarge', region='us-east-2')
        self.assertEqual(self.book.reserve_gate(self.settings, request), ledger.GateCapReached('daily_limit'))

    def test_reservation_rejects_closed_attempt_ids(self) -> None:
        request = self.allowance_gate()
        for outcome in (ledger.GateAbsent(request.expires), ledger.GateTerminated(request.expires, 'i-allowance')):
            with self.subTest(outcome=outcome):
                self.book.resolve_gate(request.attempt, outcome)
                before = self.book.path.read_bytes()
                self.assertIsInstance(self.book.reserve_gate(self.settings, request), ledger.GateAttemptEnded)
                self.assertEqual(self.book.path.read_bytes(), before)

    def test_confirmed_termination_preserves_its_end_and_cost_for_every_later_outcome(self) -> None:
        request = self.allowance_gate()
        ended = ledger.GateTerminated(request.expires, 'i-allowance')
        self.book.resolve_gate(request.attempt, ended)
        before = self.book.path.read_bytes()
        cost = gate_trial.trial_cost(self.book, {request.attempt}, self.settings, request.expires + 120)
        self.assertGreater(cost[0], Decimal(0))
        for outcome in (ledger.GateRunning('i-other'), ledger.GateTerminated(request.expires + 60, 'i-other'),
                        ledger.GateTerminated(request.expires - 60, 'i-allowance'), ledger.GateAbsent(request.expires + 60)):
            with self.subTest(outcome=outcome):
                self.book.resolve_gate(request.attempt, outcome, instance_type='m7a.4xlarge')
                self.assertEqual(self.book.path.read_bytes(), before)
                self.assertEqual(gate_trial.trial_cost(self.book, {request.attempt}, self.settings, request.expires + 120), cost)

    def test_stale_absence_preserves_confirmed_termination_and_cost(self) -> None:
        request = self.allowance_gate()
        ended = ledger.GateTerminated(request.expires, 'i-allowance')
        self.book.resolve_gate(request.attempt, ended)
        before = self.book.read_snapshot().gate_reservations[0]
        cost = gate_trial.trial_cost(self.book, {request.attempt}, self.settings, request.expires + 60)
        self.assertGreater(cost[0], Decimal(0))
        self.book.resolve_gate(request.attempt, ledger.GateAbsent(request.expires + 60))
        after = self.book.read_snapshot().gate_reservations[0]
        self.assertEqual(after, before)
        self.assertEqual(after.state, ended)
        self.assertEqual(gate_trial.trial_cost(self.book, {request.attempt}, self.settings, request.expires + 60), cost)

    def test_bill_allowance_corrects_the_saved_estimate_once(self) -> None:
        request = self.allowance_gate()
        _ = (self.state / 'cloud-gate-launches.jsonl').write_text(json.dumps(
            {'attempt': request.attempt, 'time': request.at, 'instance_type': request.instance_type,
             'result': 'launched'}) + '\n')
        self.book.resolve_gate(request.attempt, ledger.GateTerminated(request.expires, 'i-allowance'))
        end = self.start + 86400
        now = end + 60
        self.observe(end, 'stopped')
        self.observe(now, 'stopped')
        self.book.reconcile(ledger.CostReconciliation(now, self.start, end, Decimal('.801'), 'Cost Explorer'))
        self.assertEqual(self.estimate(now).monthly.usd, Decimal('.82'))
        self.assertEqual(self.book.read_snapshot().reconciliations[0].established_days,
                         (ledger.EstablishedDayCost('2026-01-15', Decimal('.82')),))
        own = replace(self.rate_bill('2026-01-15', '.8'), actual_usd=Decimal('.801'),
            usage_types=(ledger.UsageTypeCost('USE2-SpotUsage:m7a.8xlarge', Decimal('.8'), Decimal(1), 'Hrs'),
                         ledger.UsageTypeCost('Requests-Tier1', Decimal('.001'), Decimal(1), 'Requests')))
        _ = (self.state / 'charges' / '2026-01-15.json').write_text(json.dumps(ledger.charge_record(own)))
        for repeat in range(2):
            with self.subTest(repeat=repeat):
                self.assertEqual(self.estimate(now).monthly.usd, Decimal('.801'))
                self.assertEqual(self.book.read_snapshot().reconciliations[0].established_days,
                                 (ledger.EstablishedDayCost('2026-01-15', Decimal('.801')),))
        _ = self.rate_bill('2026-01-16', '8')
        later = now + 86400
        self.observe(later, 'stopped')
        self.assertEqual(self.estimate(later).monthly.usd, Decimal('.801'))

    def test_completed_gate_books_its_own_bill_and_keeps_it_after_a_later_bill(self) -> None:
        request = self.allowance_gate()
        self.book.resolve_gate(request.attempt, ledger.GateTerminated(request.expires, 'i-allowance'))
        self.assertEqual(ledger.gate_allowance(self.book.read_snapshot().gate_reservations[0]),
                         ledger.GateAllowance(Decimal('.02'), 'attempt_allowance_usd'))
        own = self.allowance_bill('2026-01-15')
        now = own.period_end + 60
        self.observe(now, 'stopped')
        estimate = self.estimate(now)
        saved = self.book.read_snapshot().gate_reservations[0]
        self.assertEqual(saved.allowance_usd, Decimal('.02'))
        expected = ledger.GateAllowance(Decimal('.047'), 'AWS bill 2026-01-15')
        self.assertEqual(ledger.gate_allowance(saved), expected)
        self.assertEqual(sum((row.usd for row in estimate.intervals if row.source.startswith('gate storage and transfer:')),
                             Decimal(0)), expected.usd)
        cost, complete = gate_trial.trial_cost(self.book, {request.attempt}, self.settings, now)
        self.assertTrue(complete)
        self.assertEqual(cost, Decimal('.847'))
        later = self.allowance_bill('2026-01-16', multiplier=Decimal(10))
        self.assertEqual(ledger.gate_allowance(self.book.priced_snapshot(later.period_end).gate_reservations[0]), expected)

    def test_gate_allowance_uses_the_day_rule_and_retains_the_open_reserve(self) -> None:
        request = self.allowance_gate()
        old = self.allowance_bill('2026-01-14')
        # The S3 share uses the bill day's launches.
        with (self.state / 'cloud-gate-launches.jsonl').open('a') as stream:
            for number in range(4):
                _ = stream.write(json.dumps({'attempt': 'old' + str(number), 'time': old.period_start + 3600,
                    'instance_type': request.instance_type, 'result': 'launched'}) + '\n')
        self.assertEqual(ledger.gate_allowance(self.book.priced_snapshot(self.start + 60).gate_reservations[0]).usd,
                         Decimal('.02'))
        self.book.resolve_gate(request.attempt, ledger.GateTerminated(request.expires, 'i-allowance'))
        expected = ledger.GateAllowance(Decimal('.047'), 'AWS bill 2026-01-14')
        self.assertEqual(ledger.gate_allowance(self.book.read_snapshot().gate_reservations[0]), expected)
        before = replace(request, attempt='before', at=old.period_start - 3600, expires=old.period_start,
                         state=ledger.GateTerminated(old.period_start, 'i-before'))
        with mock.patch.object(self.book, 'read_snapshot', return_value=ledger.LedgerEvidence(gate_reservations=[before])):
            self.assertEqual(ledger.gate_allowance(self.book.priced_snapshot(old.period_end).gate_reservations[0]), expected)

    def test_gate_allowance_uses_west_region_rates_and_shared_s3_costs(self) -> None:
        request = self.allowance_gate(region='us-west-2')
        reading = self.allowance_bill('2026-01-15', 'USW2-')
        reading = replace(reading, usage_types=(*reading.usage_types,
            ledger.UsageTypeCost('USE2-PublicIPv4:InUseAddress', Decimal(1000), Decimal(1), 'Hrs'),
            ledger.UsageTypeCost('USE2-EBS:VolumeUsage.gp3', Decimal(1000), Decimal(1), 'GB-Month')))
        self.book.resolve_gate(request.attempt, ledger.GateTerminated(request.expires, 'i-west'))
        with mock.patch.object(self.book, 'read_bills', return_value=(reading,)):
            saved = self.book.priced_snapshot(reading.period_end).gate_reservations[0]
        self.assertEqual(ledger.gate_allowance(saved), ledger.GateAllowance(Decimal('.047'), 'AWS bill 2026-01-15'))

    def test_gate_allowance_missing_rows_cost_zero_or_use_the_configured_fallback(self) -> None:
        request = self.allowance_gate()
        self.book.resolve_gate(request.attempt, ledger.GateTerminated(request.expires, 'i-allowance'))
        reading = self.allowance_bill('2026-01-15')
        for rows, expected in (((), '.02'), ((ledger.UsageTypeCost('USE2-PublicIPv4:InUseAddress',
                Decimal('.006'), Decimal(1), 'Hrs'),), '.006'),
                ((ledger.UsageTypeCost('Requests-Tier1', Decimal(0), Decimal(1), 'Requests'),), '0')):
            with self.subTest(expected=expected), mock.patch.object(self.book, 'read_bills',
                    return_value=(replace(reading, usage_types=rows),)):
                saved = self.book.priced_snapshot(reading.period_end).gate_reservations[0]
                self.assertEqual(ledger.gate_allowance(saved).usd, Decimal(expected))

    def test_gate_allowance_splits_hours_at_midnight_and_uses_the_bill_month(self) -> None:
        request = self.allowance_gate()
        january = self.allowance_bill('2026-01-31')
        february = self.allowance_bill('2026-02-01')
        crossing = replace(request, attempt='crossing', at=january.period_end - 1800,
                           expires=january.period_end + 1800,
                           state=ledger.GateTerminated(january.period_end + 1800, 'i-crossing'))
        _ = (self.state / 'cloud-gate-launches.jsonl').write_text(''.join(json.dumps(
            {'attempt': str(number), 'time': crossing.at, 'instance_type': request.instance_type,
             'result': 'launched'}) + '\n' for number in range(4)))
        with mock.patch.object(self.book, 'read_snapshot', return_value=ledger.LedgerEvidence(gate_reservations=[crossing])):
            saved = self.book.priced_snapshot(february.period_end).gate_reservations[0]
        expected = Decimal('.006') + Decimal('.744') * Decimal(40) / Decimal(744) / 2
        expected += Decimal('.744') * Decimal(40) / Decimal(672) / 2 + Decimal('.001')
        allowance = ledger.gate_allowance(saved)
        self.assertEqual(allowance.usd, expected)
        self.assertEqual(allowance.source, 'AWS bill 2026-01-31, AWS bill 2026-02-01')

    def test_day_rate_keeps_its_bill_and_replaces_only_unbilled_estimates(self) -> None:
        older = self.rate_bill('2026-01-14', '.1')
        newer = self.rate_bill('2026-01-16', '.8')
        now = self.start + 3 * 86400
        def rate(readings: Sequence[ledger.ChargeRead], day: str) -> ledger.BoxRate:
            return ledger.box_rate(readings, 'm7a.8xlarge', Decimal('.9'), now, day, region='us-east-2')
        for day in ('2026-01-13', '2026-01-14', '2026-01-15'):
            with self.subTest(day=day):
                self.assertEqual(rate((older, newer), day), ledger.BoxRate(Decimal('.1'), 'AWS bill 2026-01-14'))
        own = self.rate_bill('2026-01-15', '.3')
        self.assertEqual(rate((older, own, newer), '2026-01-15'), ledger.BoxRate(Decimal('.3'), 'AWS bill 2026-01-15'))
        self.assertEqual(rate((older, own, newer), '2026-01-17'), ledger.BoxRate(Decimal('.8'), 'AWS bill 2026-01-16'))
        self.assertEqual(rate((), '2026-01-15'), ledger.BoxRate(Decimal('.9'), 'spot_max_usd_per_hour'))

    def test_saved_gate_day_keeps_its_rate_after_a_newer_bill(self) -> None:
        self.inventory(self.start)
        self.observe(self.start, 'stopped')
        _ = self.rate_bill('2026-01-14', '.1')
        request = ledger.GateReservation('old', self.start, self.start + 3600, Decimal('.9'), Decimal(0),
                                         instance_type='m7a.8xlarge', region='us-east-2')
        self.assertIsInstance(self.book.reserve_gate(self.settings, request), ledger.GateReserved)
        self.book.resolve_gate('old', ledger.GateTerminated(self.start + 3600, 'i-old'))
        for number, day, price in ((1, '2026-01-15', '.3'), (2, '2026-01-16', '.8')):
            with self.subTest(bill_day=day):
                reading = self.rate_bill(day, price)
                now = reading.period_end + 1
                refresh = ledger.GateReservation('refresh' + str(number), now, now + 60, Decimal('.9'), Decimal(0),
                                                 instance_type='m7a.8xlarge', region='us-east-2')
                self.assertIsInstance(self.book.reserve_gate(self.settings, refresh), ledger.GateReserved)
                saved = ledger.CostLedger(self.state).read_snapshot().gate_reservations[0]
                self.assertEqual(saved.rate, ledger.BoxRate(Decimal('.3'), 'AWS bill 2026-01-15'))
                self.assertEqual(dict(saved.day_rates)['2026-01-15'], saved.rate)
                days = {row.day: row for row in self.book.spend_days(config.read_settings(self.settings), now)}
                self.assertEqual(days['2026-01-15'].usd, Decimal('.3'))

    def test_future_gate_reserves_compute_before_its_start(self) -> None:
        settings = replace(config.read_settings(self.settings), accounting_zone=ZoneInfo('UTC'))
        expected = Decimal('.8') / Decimal(3)
        for delay in (60, 86400):
            with self.subTest(delay=delay):
                at = self.start + delay
                request = ledger.GateReservation('future', at, at + 1200, Decimal('.8'), Decimal(0))
                state = ledger.LedgerEvidence(gate_reservations=[request])
                intervals = ledger.gate_compute_intervals(state, self.start, 60, reserve=True)
                self.assertEqual([(row.start, row.end) for row in intervals], [(at, at + 1200)])
                self.assertEqual(sum((row.usd for row in intervals), Decimal(0)), expected)
                self.assertEqual(ledger.gate_compute_intervals(state, self.start, 60), ())
                self.assertEqual(ledger.gate_future_costs(state, settings, self.start),
                                 (expected if delay < 86400 else Decimal(0), expected))

    def test_midnight_uses_each_days_rate_and_one_lifetime_minimum(self) -> None:
        self.inventory(self.start)
        self.observe(self.start, 'stopped')
        first = self.rate_bill('2026-01-15', '.6')
        second = self.rate_bill('2026-01-16', '1.2')
        midnight = self.start + 86400
        request = ledger.GateReservation('crossing', midnight - 5, midnight + 5, Decimal('.9'), Decimal(0),
                                         instance_type='m7a.8xlarge', region='us-east-2')
        self.assertIsInstance(self.book.reserve_gate(self.settings, request), ledger.GateReserved)
        self.book.resolve_gate('crossing', ledger.GateTerminated(midnight + 5, 'i-crossing'))
        refresh = ledger.GateReservation('refresh', second.period_end, second.period_end + 60, Decimal('.9'), Decimal(0))
        self.assertIsInstance(self.book.reserve_gate(self.settings, refresh), ledger.GateReserved)
        saved = self.book.read_snapshot().gate_reservations[0]
        state = ledger.LedgerEvidence(gate_reservations=[saved])
        gate_intervals = ledger.gate_compute_intervals(state, second.period_end, 60)
        builder = ledger.older_compute_intervals((ledger.ChargedInterval(midnight - 5, midnight + 5,
            Decimal(0), 'fixture', 'instance'),), (first, second),
            replace(config.read_settings(self.settings), instance_type='m7a.8xlarge'), second.period_end)
        for name, intervals in (('gate', gate_intervals), ('builder', builder)):
            with self.subTest(kind=name):
                self.assertEqual(len(intervals), 2)
                self.assertEqual(ledger.period_cost(intervals, midnight - 86400, midnight), Decimal('.005'))
                self.assertEqual(ledger.period_cost(intervals, midnight, midnight + 86400), Decimal('.010'))
                self.assertEqual(sum((row.usd for row in intervals), Decimal(0)), Decimal('.015'))
        self.assertEqual([row.source for row in builder], ['AWS bill 2026-01-15', 'AWS bill 2026-01-16'])

    def test_pricing_reuses_bill_history_and_reads_new_bills_on_the_next_pass(self) -> None:
        self.inventory(self.start)
        self.observe(self.start, 'stopped')
        _ = (self.root / 'cloud_gate.conf').write_text('regions=us-east-2 us-west-2\n')
        state = self.book.read_snapshot()
        for number in range(1000):
            state.gate_reservations.append(ledger.GateReservation(str(number), self.start, self.start + 1200,
                Decimal('.8') if number % 2 else Decimal('.9'), Decimal(0),
                instance_type='m7a.8xlarge' if number % 3 else 'm7a.4xlarge',
                region=('', 'us-east-2', 'us-west-2')[number % 3]))
        for price in ('.1', '.2'):
            with self.subTest(price=price):
                _ = self.rate_bill('2026-01-14', price)
                with mock.patch.object(self.book, 'read_snapshot', return_value=state), mock.patch.object(
                        ledger, 'is_billed_day', wraps=ledger.is_billed_day) as bill_checks:
                    _ = self.book.spend_days(config.read_settings(self.settings), self.start + 60)
                self.assertLess(bill_checks.call_count, 20)
                for reservation in state.gate_reservations:
                    expected = Decimal(price) if reservation.region == 'us-east-2' else reservation.usd_per_hour
                    self.assertEqual(reservation.rate.usd_per_hour, expected)

    def test_gate_prices_use_the_saved_region_and_highest_unknown_region_rate(self) -> None:
        self.inventory(self.start)
        self.observe(self.start, 'stopped')
        _ = (self.root / 'cloud_gate.conf').write_text('regions=us-east-2 us-west-2\n')
        reading = ledger.ChargeRead('2026-01-14', self.start, self.start - 86400, self.start,
            Decimal('.6'), Decimal(0), Decimal(0), (
                ledger.UsageTypeCost('USE2-SpotUsage:m7a.8xlarge', Decimal('.1'), Decimal(1), 'Hrs'),
                ledger.UsageTypeCost('USW2-SpotUsage:m7a.8xlarge', Decimal('.5'), Decimal(1), 'Hrs')))
        directory = self.state / 'charges'
        directory.mkdir()
        _ = (directory / '2026-01-14.json').write_text(json.dumps(ledger.charge_record(reading)))
        for region, expected in (('us-east-2', '.1'), ('us-west-2', '.5'), ('', '.5')):
            with self.subTest(region=region):
                reply = self.book.reserve_gate(self.settings, ledger.GateReservation(
                    region or 'unknown', self.start, self.start + 3600, Decimal('.8'), Decimal(0),
                    instance_type='m7a.8xlarge', region=region))
                self.assertIsInstance(reply, ledger.GateReserved)
                saved = self.book.read_snapshot().gate_reservations[-1]
                self.assertEqual(saved.rate.usd_per_hour, Decimal(expected))
        # A region without a usable bill retains the configured ceiling.
        self.assertEqual(ledger.gate_rate((reading,), 'm7a.8xlarge', Decimal('.8'), self.start, '2026-01-15', 'eu-north-1'),
                         ledger.BoxRate(Decimal('.8'), 'spot_max_usd_per_hour'))

    def test_gate_cap_uses_bills_and_reprices_existing_boxes(self) -> None:
        _ = (self.root / 'cloud_gate.conf').write_text('regions=us-east-2\n')
        now = self.start + 120
        self.inventory(self.start - 86400)
        self.observe(self.start - 86400, 'stopped')
        self.observe(self.start, 'stopped')
        bills = (
            ledger.ChargeRead('2026-01-13', now, self.start - 2 * 86400, self.start - 86400,
                Decimal('.5'), Decimal(0), Decimal(0), (
                    ledger.UsageTypeCost('USE2-SpotUsage:m7a.4xlarge', Decimal('.3'), Decimal(1), 'Hrs'),
                    ledger.UsageTypeCost('USE2-EBS:VolumeUsage.gp3', Decimal('.2')))),
            ledger.ChargeRead('2026-01-14', now, self.start - 86400, self.start,
                Decimal(1), Decimal(0), Decimal(0), (
                    ledger.UsageTypeCost('USE2-SpotUsage:m7a.8xlarge', Decimal('.6'), Decimal(1), 'Hrs'),
                    ledger.UsageTypeCost('USE2-EBS:VolumeUsage.gp3', Decimal('.4')))),
        )
        directory = self.state / 'charges'
        directory.mkdir()
        for bill in bills:
            _ = (directory / f'{bill.day}.json').write_text(json.dumps(ledger.charge_record(bill)))
        # These records use the existing ledger format and have no saved instance type.
        document = ledger.mapping(cast(object, json.loads(self.book.path.read_text())))
        document['gate_reservations'] = [
            {'attempt': attempt, 'at': start, 'expires': now + 60, 'usd_per_hour': '.9', 'allowance_usd': '.02', 'state': state}
            for attempt, start, state in (
                ('billed', self.start - 86400, {'kind': 'terminated', 'at': self.start - 86400 + 7200, 'instance_id': 'i-billed'}),
                ('short', self.start, {'kind': 'terminated', 'at': self.start + 10, 'instance_id': 'i-short'}),
                ('running', self.start + 60, {'kind': 'running', 'instance_id': 'i-running'}),
                ('unknown', self.start, {'kind': 'terminated', 'at': self.start + 10, 'instance_id': 'i-unknown'}))]
        _ = self.book.path.write_text(json.dumps(document))
        _ = (self.state / 'cloud-gate-launches.jsonl').write_text(''.join(json.dumps(
            {'attempt': attempt, 'time': start, 'instance_type': size, 'result': 'launched'}) + '\n'
            for attempt, start, size in (('short', self.start, 'm7a.8xlarge'),
                ('running', self.start + 60, 'm7a.4xlarge'), ('unknown', self.start, 'm7a.16xlarge'))))
        days = {row.day: row for row in self.book.spend_days(config.read_settings(self.settings), now)}
        self.assertEqual(days['2026-01-14'].usd, Decimal(1))
        self.assertEqual(days['2026-01-14'].source, 'AWS bill')
        self.assertEqual(days['2026-01-15'].usd, Decimal('.430'))
        self.assertEqual(days['2026-01-15'].source, 'estimated from the newest bill')
        self.assertEqual(days['2026-01-15'].runs, 3)
        request = ledger.GateReservation('next', now, now + 60, Decimal('.9'), Decimal('.02'), instance_type='m7a.8xlarge')
        # The passing daily cap includes both open allowances.
        for daily, monthly, reason in (('3.445', '400', 'daily_limit'), ('3.50', '9.745', 'monthly_limit')):
            with self.subTest(reason=reason):
                self.costs({'daily_limit_usd': daily, 'monthly_limit_usd': monthly})
                self.assertEqual(self.book.reserve_gate(self.settings, request), ledger.GateCapReached(reason))
                saved = self.book.read_snapshot().gate_reservations
                self.assertEqual([row.rate for row in saved], [
                    ledger.BoxRate(Decimal('.9'), 'spot_max_usd_per_hour'),
                    ledger.BoxRate(Decimal('.6'), 'AWS bill 2026-01-14'),
                    ledger.BoxRate(Decimal('.3'), 'AWS bill 2026-01-13'),
                    ledger.BoxRate(Decimal('.9'), 'spot_max_usd_per_hour')])
        self.costs({'monthly_limit_usd': '400'})
        self.assertIsInstance(self.book.reserve_gate(self.settings, request), ledger.GateReserved)
        for seconds, usd in ((0, '.01'), (10, '.01'), (59, '.01'), (60, '.01'), (120, '.02')):
            with self.subTest(seconds=seconds):
                self.assertEqual(ledger.box_cost(ledger.BoxRate(Decimal('.6'), 'fixture'), seconds, 60), Decimal(usd))
        zero = ledger.ChargeRead('2026-01-15', self.start + 86400, self.start, self.start + 86400,
            Decimal(0), Decimal(10), Decimal(0), (
                ledger.UsageTypeCost('USE2-SpotUsage:m7a.8xlarge', Decimal(6), Decimal(1), 'Hrs'),
                ledger.UsageTypeCost('Credit', Decimal(-6))))
        self.assertEqual(ledger.box_rate((zero, *bills), 'm7a.8xlarge', Decimal('.9'), self.start + 86400, '2026-01-16'),
                         ledger.BoxRate(Decimal('.6'), 'AWS bill 2026-01-14'))
        boundary = self.start + 86400
        crossing = ledger.LedgerEvidence(gate_reservations=[ledger.GateReservation(
            'crossing', boundary - 5, boundary + 5, Decimal('.9'), Decimal(0),
            ledger.GateTerminated(boundary + 5, 'i-crossing'), 'm7a.8xlarge', ledger.BoxRate(Decimal('.6'), 'fixture'))])
        intervals = ledger.gate_compute_intervals(crossing, boundary + 5, 60)
        self.assertEqual(ledger.period_cost(intervals, boundary - 86400, boundary), Decimal('.005'))
        self.assertEqual(ledger.period_cost(intervals, boundary, boundary + 86400), Decimal('.005'))
        for spot_usd in (Decimal(0), Decimal(-1)):
            with self.subTest(spot_usd=spot_usd):
                invalid_rate = replace(zero, actual_usd=Decimal(1), usage_types=(
                    ledger.UsageTypeCost('USE2-SpotUsage:m7a.8xlarge', spot_usd, Decimal(1), 'Hrs'),
                    ledger.UsageTypeCost('USE2-EBS:VolumeUsage.gp3', Decimal(1) - spot_usd)))
                self.assertEqual(ledger.box_rate((invalid_rate, *bills), 'm7a.8xlarge', Decimal('.9'), boundary, '2026-01-16'),
                                 ledger.BoxRate(Decimal('.6'), 'AWS bill 2026-01-14'))
                self.assertEqual(ledger.box_rate((invalid_rate,), 'm7a.8xlarge', Decimal('.9'), boundary, '2026-01-16'),
                                 ledger.BoxRate(Decimal('.9'), 'spot_max_usd_per_hour'))
        older = ledger.LedgerEvidence(
            observations=[ledger.InstanceObservation(self.start, 'stopped', 'fixture'),
                          ledger.InstanceObservation(boundary - 5, 'running', 'fixture'),
                          ledger.InstanceObservation(boundary + 5, 'stopped', 'fixture')],
            inventories=[ledger.ResourceInventory(self.start, (ledger.PricedResource('storage', Decimal(0)),), 'fixture')])
        older_settings = replace(config.read_settings(self.settings), instance_type='m7a.8xlarge')
        older_days = ledger.gate_spend_days(older, older_settings, bills, boundary + 5)
        for day in ('2026-01-15', '2026-01-16'):
            with self.subTest(older_day=day):
                spend = next(row for row in older_days if row.day == day)
                self.assertEqual(next(row.usd for row in spend.groups if row.group == 'gate compute at 32 CPU'), Decimal('.005'))
        for amount in (Decimal('-0.0000036'), Decimal('0.0000036'), Decimal('.0049')):
            with self.subTest(amount=amount):
                dust = replace(zero, actual_usd=amount, usage_types=(ledger.UsageTypeCost('storage', amount),))
                dust_days = ledger.gate_spend_days(older, older_settings, (*bills, dust), boundary + 5)
                for day in ('2026-01-15', '2026-01-16'):
                    spend = next(row for row in dust_days if row.day == day)
                    self.assertEqual(spend.source, 'estimated from the newest bill')
                    self.assertEqual(next(row.usd for row in spend.groups if row.group == 'storage'), Decimal('.4'))
        self.text_setting('accounting_zone', 'America/New_York')
        for stamp, daily, monthly, reason in (
                ('2026-01-16T02:00:00+00:00', '20', '400', 'daily_limit'),
                ('2026-02-01T02:00:00+00:00', '400', '20', 'monthly_limit')):
            with self.subTest(local_cap=reason):
                local_now = instant(stamp)
                self.costs({'daily_limit_usd': daily, 'monthly_limit_usd': monthly})
                local_book = ledger.CostLedger(self.state / reason)
                local_book.record_inventory(ledger.ResourceInventory(local_now - 12 * 3600,
                    (ledger.PricedResource('storage', Decimal(0)),), 'fixture'))
                local_book.observe(ledger.InstanceObservation(local_now - 12 * 3600, 'stopped', 'fixture'))
                document = ledger.mapping(cast(object, json.loads(local_book.path.read_text())))
                document['gate_reservations'] = [
                    {'attempt': str(hours), 'at': local_now - hours * 3600,
                     'expires': local_now - (hours - 5) * 3600, 'usd_per_hour': '2.2', 'allowance_usd': '0',
                     'state': {'kind': 'terminated', 'at': local_now - (hours - 5) * 3600, 'instance_id': str(hours)}}
                    for hours in (12, 5)]
                _ = local_book.path.write_text(json.dumps(document))
                local_request = ledger.GateReservation('local-next', local_now, local_now + 60, Decimal('.1'), Decimal(0))
                self.assertEqual(local_book.reserve_gate(self.settings, local_request), ledger.GateCapReached(reason))

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
        now += config.read_settings(self.settings).notification_retry_s
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
        now += config.read_settings(self.settings).notification_retry_s
        self.assertIsInstance(self.guard(now), ledger.SpendingRefused)
        self.assertEqual(len(self.notices), 1)

    def test_guard_retries_delivery_after_interruption(self) -> None:
        now = self.reach_limit()
        with mock.patch.object(self, "notice", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                _ = self.guard(now)
        self.book = ledger.CostLedger(self.state)
        now += config.read_settings(self.settings).notification_retry_s
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
        now += config.read_settings(self.settings).notification_retry_s
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

    def test_concurrent_guards_deliver_a_pending_notice_once(self) -> None:
        now = self.reach_limit()
        entered, release = Event(), Event()
        competing_notices: list[ledger.LimitEvent] = []

        def blocked_notice(event: ledger.LimitEvent) -> bool:
            self.notices.append(event)
            entered.set()
            if not release.wait(timeout=5):
                raise AssertionError("The test did not release the notifier")
            return True

        def competing_notice(event: ledger.LimitEvent) -> bool:
            competing_notices.append(event)
            return True

        with ThreadPoolExecutor(max_workers=1) as executor:
            first = executor.submit(self.book.guard, self.settings, "admission", now, blocked_notice)
            try:
                self.assertTrue(entered.wait(timeout=5), "The first guard did not enter the notifier")
                other = ledger.CostLedger(self.state)
                after_deadline = now + config.read_settings(self.settings).notification_retry_s + 1
                self.assertIsInstance(other.guard(self.settings, "start", after_deadline, competing_notice), ledger.SpendingRefused)
                self.assertEqual(competing_notices, [])
                self.assertEqual(self.saved_events()[0]["delivery"], "pending")
            finally:
                release.set()
            self.assertIsInstance(first.result(timeout=5), ledger.SpendingRefused)
        self.assertEqual(len(self.notices), 1)
        self.assertEqual(self.saved_events()[0]["delivery"], "delivered")

    def test_failed_notice_waits_for_its_saved_retry_deadline_across_restart(self) -> None:
        now = self.reach_limit()
        attempts: list[ledger.LimitEvent] = []

        def failed_notice(event: ledger.LimitEvent) -> bool:
            attempts.append(event)
            return False

        self.assertIsInstance(self.book.guard(self.settings, "admission", now, failed_notice), ledger.SpendingRefused)
        event_id = self.saved_events()[0]["event_id"]
        self.book = ledger.CostLedger(self.state)
        self.replace("notification_retry_s", "120")
        for at in (now, now + 59):
            self.assertIsInstance(self.book.guard(self.settings, "start", at, failed_notice), ledger.SpendingRefused)
        self.assertEqual(len(attempts), 1)
        self.assertIsInstance(self.book.guard(self.settings, "admission", now + 60, failed_notice), ledger.SpendingRefused)
        self.assertEqual(len(attempts), 2)
        self.assertEqual([event.event_id for event in attempts], [event_id, event_id])
        self.book = ledger.CostLedger(self.state)
        self.assertIsInstance(self.guard(now + 179), ledger.SpendingRefused)
        self.assertEqual(self.notices, [])
        self.assertIsInstance(self.guard(now + 180), ledger.SpendingRefused)
        self.assertEqual([event.event_id for event in self.notices], [event_id])
        self.assertEqual(self.saved_events()[0]["delivery"], "delivered")

    def test_failed_delivery_state_write_does_not_allow_an_immediate_second_notice(self) -> None:
        now = self.reach_limit()

        def fail_delivery_commit(path: Path, text: str) -> None:
            document = cast(dict[str, object], json.loads(text))
            events = cast(list[dict[str, object]], document["events"])
            if any(event["delivery"] == "delivered" for event in events):
                raise OSError("The ledger disk is full")
            write_text_atomic(path, text)

        with mock.patch.object(ledger, "write_text_atomic", side_effect=fail_delivery_commit):
            self.assertIsInstance(self.guard(now), ledger.SpendingRefused)
        saved = self.saved_events()[0]
        self.book = ledger.CostLedger(self.state)
        with mock.patch.object(ledger, "write_text_atomic", side_effect=OSError("The ledger disk is full")):
            self.assertIsInstance(self.guard(now + 1), ledger.SpendingUnavailable)
        self.assertEqual(self.saved_events()[0], saved)
        self.assertIsInstance(self.guard(now + 59), ledger.SpendingRefused)
        self.assertEqual(len(self.notices), 1)

    def test_message_key_prevents_duplicate_delivery_after_a_failed_confirmation_and_restart(self) -> None:
        now = self.reach_limit()
        retry_s = config.read_settings(self.settings).notification_retry_s
        clock = [now]
        received: list[str] = []
        attempts: list[str] = []
        replies: list[str] = []
        sent: dict[tuple[str, str, str], int] = {}

        def message(arguments: Sequence[str], **options: object) -> subprocess.CompletedProcess[str]:
            if arguments[0] == "git":
                return git_only(arguments, **options)
            self.assertEqual(Path(arguments[1]).name, "send.py")
            self.assertIn("--key", arguments)
            key = arguments[arguments.index("--key") + 1]
            self.assertTrue(key)
            recipient = arguments[arguments.index("--to") + 1]
            sender = arguments[arguments.index("--from") + 1]
            identity = recipient, sender, key
            repeat_minutes = (Decimal(arguments[arguments.index("--repeat-minutes") + 1])
                              if "--repeat-minutes" in arguments else Decimal(0))
            attempts.append(key)
            if identity in sent and Decimal(clock[0] - sent[identity]) < repeat_minutes * 60:
                reply = "SKIPPED: inside the repeat window\n"
            else:
                received.append(key)
                sent[identity] = clock[0]
                reply = "SENT: fixture recipient\n"
            replies.append(reply)
            return subprocess.CompletedProcess(list(arguments), 0, reply, "")

        def fail_confirmation(path: Path, text: str) -> None:
            document = cast(dict[str, object], json.loads(text))
            events = cast(list[dict[str, object]], document["events"])
            if any(event["delivery"] == "delivered" for event in events):
                raise OSError("The confirmation write failed")
            write_text_atomic(path, text)

        with mock.patch.object(subprocess, "run", side_effect=message):
            with mock.patch.object(ledger, "write_text_atomic", side_effect=fail_confirmation):
                self.assertIsInstance(self.book.guard(self.settings, "admission", now,
                                                     clock=lambda: float(clock[0])), ledger.SpendingRefused)
            event_id = self.saved_events()[0]["event_id"]
            self.assertEqual(self.saved_events()[0]["delivery"], "pending")
            self.book = ledger.CostLedger(self.state)
            clock[0] += retry_s + 1
            self.assertIsInstance(self.book.guard(self.settings, "start", clock[0],
                                                 clock=lambda: float(clock[0])), ledger.SpendingRefused)
            self.assertEqual(self.saved_events()[0]["event_id"], event_id)
            self.assertEqual(self.saved_events()[0]["delivery"], "delivered")
            self.assertIsInstance(self.book.guard(self.settings, "admission", clock[0] + retry_s,
                                                 clock=lambda: float(clock[0] + retry_s)),
                                  ledger.SpendingRefused)
        self.assertEqual(len(received), 1)
        self.assertEqual(attempts, [f"aws-limit:{event_id}", f"aws-limit:{event_id}"])
        self.assertEqual(replies, ["SENT: fixture recipient\n", "SKIPPED: inside the repeat window\n"])

    def test_each_pending_notice_sets_its_retry_deadline_from_its_own_attempt(self) -> None:
        self.replace("monthly_limit_usd", "20")
        now = self.reach_limit()
        retry_s = config.read_settings(self.settings).notification_retry_s
        clock = [now]
        attempts: list[tuple[str, int, int]] = []

        def slow_first_notice(event: ledger.LimitEvent) -> bool:
            attempts.append((event.event_id, clock[0], event.retry_after))
            if len(attempts) == 1:
                clock[0] += retry_s + 1
                return True
            return False

        def current_time() -> float:
            return float(clock[0])

        self.assertIsInstance(ledger.spending_guard(
            self.settings, self.state, clock=current_time, notify=slow_first_notice), ledger.SpendingRefused)
        self.assertEqual(len(attempts), 2)
        first, second = attempts
        self.assertNotEqual(first[0], second[0])
        self.assertIsInstance(ledger.spending_guard(
            self.settings, self.state, clock=current_time, notify=slow_first_notice), ledger.SpendingRefused)
        self.assertEqual([attempt[0] for attempt in attempts].count(second[0]), 1)
        self.assertEqual(first[2], first[1] + retry_s)
        self.assertEqual(second[2], second[1] + retry_s)
        saved = next(event for event in self.saved_events() if event["event_id"] == second[0])
        self.assertEqual(saved["delivery"], "pending")
        self.assertEqual(saved["retry_after"], second[1] + retry_s)

    def test_legacy_unknown_observations_do_not_block_later_confirmed_charging(self) -> None:
        self.running(self.start)
        document = cast(dict[str, object], json.loads(self.book.path.read_text()))
        observations = cast(list[dict[str, object]], document["observations"])
        observations.append({"at": self.start + 1800, "state": "unknown", "source": "legacy evidence"})
        _ = self.book.path.write_text(json.dumps(document))
        self.book = ledger.CostLedger(self.state)
        self.observe(self.start + 3600, "stopped")
        self.observe(self.start + 7200, "running")
        self.observe(self.start + 10800, "stopped")
        result = self.guard(self.start + 10800)
        self.assertIsInstance(result, ledger.SpendingAllowed)
        self.assertIsInstance(result.evidence.costs, ledger.CostEstimate)
        if isinstance(result.evidence.costs, ledger.CostEstimate):
            self.assertGreaterEqual(result.evidence.costs.monthly.usd, Decimal("1"))
            charged = [row for row in result.evidence.costs.intervals
                       if row.kind == "instance" and row.start >= self.start + 7200]
            self.assertEqual(sum(row.usd for row in charged), Decimal("1"))

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


class SpotPriceEvidenceTests(CostFixture):
    def test_regional_reply_selects_the_latest_price_for_each_requested_zone_and_type(self) -> None:
        first: dict[str, object] = dict(InstanceType='m7a.8xlarge', AvailabilityZone='eu-south-2a',
            SpotPrice='.3', Timestamp='2026-01-15T00:00:00Z')
        reply: dict[str, object] = {'SpotPriceHistory': [
            {**first, 'SpotPrice': '.2', 'Timestamp': '2026-01-15T01:00:00Z'}, first,
            {**first, 'InstanceType': 'c7a.8xlarge'}, {**first, 'AvailabilityZone': 'eu-south-2b'}]}
        self.assertEqual(ledger.regional_spot_prices('eu-south-2', reply, ('m7a.8xlarge',), 123), (
            ledger.RegionalSpotPrice('eu-south-2', 'eu-south-2a', 'm7a.8xlarge', Decimal('.2'), 123),
            ledger.RegionalSpotPrice('eu-south-2', 'eu-south-2b', 'm7a.8xlarge', Decimal('.3'), 123)))

    def test_regional_reply_rejects_incomplete_pages_and_invalid_prices(self) -> None:
        first: dict[str, object] = dict(InstanceType='m7a.8xlarge', AvailabilityZone='eu-south-2a',
            SpotPrice='.3', Timestamp='2026-01-15T00:00:00Z')
        replies: tuple[dict[str, object], ...] = (
            {'SpotPriceHistory': [first], 'NextToken': 'next'},
            *({'SpotPriceHistory': [{**first, 'SpotPrice': price}]} for price in ('-1', 'NaN', 'Infinity')),
            {'SpotPriceHistory': [{**first, 'Timestamp': 'invalid'}]},
            {'SpotPriceHistory': [{**first, 'AvailabilityZone': ''}]})
        for reply in replies:
            with self.subTest(reply=reply), self.assertRaises(ValueError):
                _ = ledger.regional_spot_prices('eu-south-2', reply, ('m7a.8xlarge',), 123)

    def test_regional_prices_round_trip_without_changing_cost_evidence(self) -> None:
        prices = (ledger.RegionalSpotPrice('us-east-2', 'us-east-2a', 'm7a.8xlarge', Decimal('.425'), 1700000000),)
        self.assertEqual(ledger.read_spot_prices(self.state), ())
        ledger.write_spot_prices(self.state, prices)
        self.assertEqual(ledger.read_spot_prices(self.state), prices)
        self.assertFalse((self.state / 'cost-ledger.json').exists())
        ledger.write_spot_prices(self.state, (prices[0], replace(prices[0], region='us-west-2', zone='us-west-2a')))
        self.assertEqual(len(ledger.read_spot_prices(self.state)), 2)

    def test_malformed_price_snapshots_are_rejected(self) -> None:
        for document in ('{', '{}', '{"prices": [{}]}', '{"prices": "invalid"}'):
            with self.subTest(document=document):
                _ = (self.state / 'spot-prices.json').write_text(document)
                with self.assertRaises(ValueError):
                    _ = ledger.read_spot_prices(self.state)

    def test_history_reads_only_the_tail_needed_for_the_window(self) -> None:
        path = self.state / 'cloud-gate-launches.jsonl'
        old = json.dumps(dict(time=1, result='finished', padding='x' * 100)) + '\n'
        recent = [dict(time=100 + number, result='refused') for number in range(3)]
        _ = path.write_text(old * 5000 + ''.join(json.dumps(row) + '\n' for row in recent))
        with path.open('rb') as stream:
            read = stream.read
            counts: list[int] = []
            def counted_read(size: int = -1) -> bytes:
                counts.append(size)
                return read(size)
            with (mock.patch.object(stream, 'read', side_effect=counted_read),
                  mock.patch.object(Path, 'open', return_value=stream)):
                self.assertEqual(ledger.recent_gate_launches(self.state, 100), tuple(reversed(recent)))
            self.assertEqual(counts, [65536])
        self.assertEqual(ledger.recent_gate_launches(self.root / 'absent', 100), ())

    def test_history_reassembles_rows_across_blocks_and_ignores_partial_rows(self) -> None:
        path = self.state / 'cloud-gate-launches.jsonl'
        recent = dict(time=100, result='finished', padding='x' * 70000)
        _ = path.write_text(json.dumps(dict(time=1)) + '\n' + json.dumps(recent) + '\n{"time":')
        self.assertEqual(ledger.recent_gate_launches(self.state, 100), (recent,))


if __name__ == "__main__":
    _ = unittest.main()
