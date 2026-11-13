"""Exercise lifecycle recovery with private journals and one fake provider."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import fcntl
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
from typing import cast, override
from unittest import mock

from scripts.aws_target import aws_target, hana_box, hana_spend, ledger
from scripts.aws_target.control_test_support import TestClock
from scripts.aws_target.ledger_test_support import CostFixture, git_only, text_setting
from scripts.lib import conf


REPOSITORY = Path(__file__).resolve().parents[2]
IDENTITY = hana_box.DeploymentIdentity('fixture-deployment', 'fixture-instance', 'fixture-spot-request', 'us-east-2')


@dataclass
class FakeProvider:
    directory: Path
    clock: TestClock
    state: ledger.InstanceState = 'stopped'
    boot: hana_box.VerifiedBoot | hana_box.BootUnverified = hana_box.BootUnverified()
    cause: hana_box.StopCause = hana_box.StopCause.UNKNOWN
    calls: list[hana_box.PendingOperation] = field(default_factory=list)
    starts: set[str] = field(default_factory=set)
    in_flight: set[str] = field(default_factory=set)
    crash_after_start: bool = False
    bad_ack: bool = False
    crash_after_mutations: set[hana_box.Action] = field(default_factory=set)
    recoveries: list[hana_box.PendingOperation] = field(default_factory=list)
    published: list[hana_box.PendingOperation] = field(default_factory=list)
    during_call: Callable[[], None] = lambda: None

    def check_intent(self, operation: hana_box.PendingOperation) -> None:
        path = self.directory / 'hana/lifecycle.json'
        saved = ledger.mapping(cast(object, json.loads(path.read_text())))
        intent = ledger.mapping(saved['operation'])
        if intent['token'] != operation.token or intent['action'] != operation.action.value:
            raise AssertionError('Persist the operation before its provider call')
        for name in ('controller.lock', 'cost-ledger.lock'):
            with (self.directory / name).open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(lock, fcntl.LOCK_UN)

    def step(self, identity: hana_box.DeploymentIdentity, start: hana_box.DemandStart | hana_box.NoDemandStart,
             operation: hana_box.PendingOperation) -> hana_box.ProviderReply:
        self.calls.append(operation)
        self.check_intent(operation)
        self.in_flight.add(operation.token)
        try:
            return self.mutate(identity, start, operation)
        finally:
            self.in_flight.discard(operation.token)

    def mutate(self, identity: hana_box.DeploymentIdentity, start: hana_box.DemandStart | hana_box.NoDemandStart,
               operation: hana_box.PendingOperation) -> hana_box.ProviderReply:
        self.during_call()
        if operation.action == hana_box.Action.OBSERVE:
            return hana_box.BoxObservation(identity, self.clock.now, self.state, self.boot, self.cause)
        if operation.action == hana_box.Action.START:
            assert isinstance(start, hana_box.DemandStart)
            self.starts.add(operation.token)
            self.state, self.boot = 'running', hana_box.VerifiedBoot('fixture-boot-' + start.token, start.token)
            if self.crash_after_start:
                self.crash_after_start = False
                raise RuntimeError('The start response was lost')
        if operation.action == hana_box.Action.STOP:
            self.state = 'stopped'
        if operation.action == hana_box.Action.PUBLISH:
            self.published.append(operation)
        if operation.action in self.crash_after_mutations:
            self.crash_after_mutations.remove(operation.action)
            raise RuntimeError('The mutation response was lost')
        if operation.action == hana_box.Action.PUBLISH:
            assert isinstance(self.boot, hana_box.VerifiedBoot)
            return hana_box.PermitAccepted(operation.token, identity, self.boot,
                operation.generation + int(self.bad_ack), operation.execution)
        return hana_box.OperationAccepted(operation.token)

    def recover(self, identity: hana_box.DeploymentIdentity, start: hana_box.DemandStart | hana_box.NoDemandStart,
                operation: hana_box.PendingOperation) -> hana_box.MutationRecovery:
        _ = start
        self.recoveries.append(operation)
        self.check_intent(operation)
        observed = hana_box.BoxObservation(identity, self.clock.now, self.state, self.boot, self.cause)
        if operation.token in self.in_flight:
            return hana_box.MutationUnresolved(operation.token, observed)
        return hana_box.MutationSettled(operation.token, observed)


class HanaBoxTests(CostFixture):
    clock: TestClock = TestClock()
    workload_path: Path = Path()
    provider: FakeProvider
    controller: hana_box.HanaController
    book: ledger.CostLedger = ledger.CostLedger(Path())

    def __init__(self, methodName: str = 'runTest'):
        super().__init__(methodName)
        self.provider = FakeProvider(Path(), TestClock())
        self.controller = hana_box.HanaController(Path(), Path(), Path(), self.provider)

    @override
    def setUp(self) -> None:
        super().setUp()
        _ = self.enterContext(mock.patch.object(subprocess, 'run', side_effect=git_only))
        self.clock = TestClock()
        self.workload_path = self.root / 'hana_aws.conf'
        _ = self.workload_path.write_text((REPOSITORY / 'config/hana_aws.conf').read_text())
        text_setting(self.workload_path, 'enabled', 'on')
        self.book = ledger.CostLedger(self.state)
        self.book.record_inventory(ledger.ResourceInventory(self.clock.now, (), 'private inventory', True))
        self.book.observe(ledger.InstanceObservation(self.clock.now, 'terminated', 'private account inventory'))
        self.provider = FakeProvider(self.state, self.clock)
        self.controller = hana_box.HanaController(self.state, self.settings, self.workload_path, self.provider, clock=self.clock)
        self.controller.register(IDENTITY)

    def record(self) -> dict[str, object]:
        return ledger.mapping(cast(object, json.loads((self.state / 'hana/lifecycle.json').read_text())))

    def generation(self) -> int:
        return max((row.generation for row in self.book.read_snapshot().hana_compute), default=0)

    def tick(self) -> hana_box.Lifecycle:
        self.clock.advance(1)
        return self.controller.tick()

    def ready(self) -> None:
        self.assertIsInstance(self.controller.request('execution-one'), hana_box.ClaimCommitted)
        for _step in range(4):
            _ = self.tick()
        self.assertEqual(self.record()['state'], hana_box.Lifecycle.READY.value)

    def test_start_reserves_before_bootstrap_and_grants_only_verified_claim_permission(self) -> None:
        self.assertIsInstance(self.controller.request('execution-one'), hana_box.ClaimCommitted)
        self.assertEqual(self.tick(), hana_box.Lifecycle.STARTING)
        exposure = self.book.read_snapshot().hana_compute[-1]
        self.assertGreater(exposure.expires, self.clock.now)
        self.assertEqual(self.provider.calls[0].action, hana_box.Action.BOOTSTRAP)
        self.assertIsInstance(self.controller.permission('execution-one', 'unverified', exposure.generation), hana_box.ClaimRefused)
        for _step in range(3):
            _ = self.tick()
        assert isinstance(self.provider.boot, hana_box.VerifiedBoot)
        self.assertIsInstance(self.controller.permission('execution-one', self.provider.boot.boot_id, self.generation()),
                              hana_box.CommandPermission)
        self.assertEqual(len(self.provider.starts), 1)

    def test_spending_refusal_makes_no_provider_mutation(self) -> None:
        workload = hana_spend.read_settings(conf.read(self.workload_path))
        self.costs({'daily_limit_usd': str(workload.storage_usd_per_month / 60)})
        self.assertIsInstance(self.controller.request('execution-one'), hana_box.ClaimCommitted)
        self.assertEqual(self.tick(), hana_box.Lifecycle.IDLE_STOPPED)
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.book.read_snapshot().hana_compute, [])
        self.assertEqual(self.controller.claim_status('execution-one'), hana_box.ClaimRefused('daily_limit'))

    def test_start_timeout_keeps_the_unresolved_claim(self) -> None:
        _ = self.controller.request('execution-one')
        _ = self.tick()
        timeout = hana_box.read_settings(conf.read(self.workload_path)).start_timeout_seconds
        self.clock.advance(timeout)
        result = self.controller.claim_status('execution-one')
        self.assertIsInstance(result, hana_box.ClaimWaiting)
        assert isinstance(result, hana_box.ClaimWaiting)
        self.assertTrue(result.timed_out)
        self.assertFalse(self.controller.release('execution-one'))
        self.assertIsInstance(self.controller.request('execution-two'), hana_box.ClaimRefused)
        self.assertEqual(len(ledger.rows(self.record(), 'claims')), 1)

    def test_start_intent_recovers_after_crash_before_response(self) -> None:
        _ = self.controller.request('execution-one')
        _ = self.tick()
        self.provider.crash_after_start = True
        with self.assertRaises(RuntimeError):
            _ = self.tick()
        operation = ledger.mapping(self.record()['operation'])
        self.assertEqual(operation['action'], hana_box.Action.START.value)
        self.controller = hana_box.HanaController(self.state, self.settings, self.workload_path, self.provider, clock=self.clock)
        for _step in range(3):
            _ = self.tick()
        self.assertEqual(len(self.provider.starts), 1)
        self.assertEqual(self.record()['state'], hana_box.Lifecycle.READY.value)
        starts = [row for row in self.provider.calls if row.action == hana_box.Action.START]
        self.assertEqual(starts[0], starts[1])

    def test_cap_refusal_during_start_keeps_the_intent_and_compute_exposure(self) -> None:
        _ = self.controller.request('execution-one')
        _ = self.tick()
        start_intent = ledger.mapping(self.record()['operation'])
        self.assertEqual(start_intent['action'], hana_box.Action.START.value)
        concurrent_intents: list[object] = []

        def refuse_spending_during_start() -> None:
            self.provider.during_call = lambda: None
            workload = hana_spend.read_settings(conf.read(self.workload_path))
            self.costs({'daily_limit_usd': str(workload.storage_usd_per_month / 60)})
            other = hana_box.HanaController(self.state, self.settings, self.workload_path,
                                           self.provider, clock=self.clock)
            for _step in range(2):
                self.clock.advance(1)
                _ = other.tick()
                concurrent_intents.append(self.record()['operation'])

        self.provider.during_call = refuse_spending_during_start
        _ = self.tick()
        self.assertEqual(self.provider.state, 'running')
        self.assertIsInstance(self.book.read_snapshot().hana_compute[-1].state, ledger.HanaExposureOpen)
        self.assertEqual(concurrent_intents, [start_intent, start_intent])
        self.assertEqual(ledger.mapping(self.record()['operation'])['action'], hana_box.Action.OBSERVE.value)
        self.assertEqual(self.provider.starts, {str(start_intent['token'])})

    def lose_mutation_response(self, action: hana_box.Action) -> None:
        _ = self.controller.request('execution-one')
        preceding = {hana_box.Action.BOOTSTRAP: 0, hana_box.Action.START: 1, hana_box.Action.PUBLISH: 3}
        for _step in range(preceding[action]):
            _ = self.tick()
        self.provider.crash_after_mutations.add(action)
        with self.assertRaisesRegex(RuntimeError, 'The mutation response was lost'):
            _ = self.tick()
        self.assertEqual(ledger.mapping(self.record()['operation'])['action'], action.value)
        self.clock.now = self.book.read_snapshot().hana_compute[-1].expires + 1
        self.controller = hana_box.HanaController(self.state, self.settings, self.workload_path,
                                                self.provider, clock=self.clock)

    def test_expired_bootstrap_reads_recovery_and_releases_the_stopped_claim(self) -> None:
        self.lose_mutation_response(hana_box.Action.BOOTSTRAP)
        calls = list(self.provider.calls)
        _ = self.tick()
        self.assertEqual(len(self.provider.recoveries), 1)
        self.assertEqual(self.provider.calls, calls)
        self.assertIsNone(self.record()['operation'])
        self.assertIsInstance(self.book.read_snapshot().hana_compute[-1].state, ledger.HanaExposureClosed)
        self.assertTrue(self.controller.release('execution-one'))
        self.assertIsInstance(self.controller.request('execution-two'), hana_box.ClaimRefused)

    def test_expired_start_recovers_a_running_box_and_restores_exposure_before_stopping(self) -> None:
        self.lose_mutation_response(hana_box.Action.START)
        spending = hana_spend.HanaSpending(self.book, self.settings, hana_spend.read_settings(conf.read(self.workload_path)))
        spending.reconcile(self.clock.now, 'stopped', generation=self.generation())
        self.assertIsInstance(self.book.read_snapshot().hana_compute[-1].state, ledger.HanaExposureClosed)
        starts = set(self.provider.starts)
        _ = self.tick()
        self.assertEqual(len(self.provider.recoveries), 1)
        self.assertEqual(self.provider.starts, starts)
        self.assertIsInstance(self.book.read_snapshot().hana_compute[-1].state, ledger.HanaExposureOpen)
        self.assertEqual(self.record()['state'], hana_box.Lifecycle.STOPPING.value)
        _ = self.tick()
        self.assertEqual(self.tick(), hana_box.Lifecycle.IDLE_STOPPED)
        self.assertTrue(self.controller.release('execution-one'))
        self.assertIsInstance(self.controller.request('execution-two'), hana_box.ClaimCommitted)

    def test_expired_publication_recovers_without_extending_permission(self) -> None:
        self.lose_mutation_response(hana_box.Action.PUBLISH)
        publications = list(self.provider.published)
        self.assertEqual(len(publications), 1)
        _ = self.tick()
        self.assertEqual(len(self.provider.recoveries), 1)
        self.assertEqual(self.provider.published, publications)
        self.assertIsNone(self.record()['delivered'])
        self.assertIsInstance(self.controller.permission('execution-one', 'fixture-boot', self.generation()), hana_box.ClaimRefused)
        self.assertEqual(self.record()['state'], hana_box.Lifecycle.STOPPING.value)
        _ = self.tick()
        self.assertEqual(self.tick(), hana_box.Lifecycle.IDLE_STOPPED)
        self.assertTrue(self.controller.release('execution-one'))
        self.assertIsInstance(self.controller.request('execution-two'), hana_box.ClaimCommitted)

    def test_stale_recovery_cannot_settle_a_different_mutation(self) -> None:
        self.lose_mutation_response(hana_box.Action.START)
        operation = self.record()['operation']
        observation = hana_box.BoxObservation(IDENTITY, self.clock.now, 'stopped')
        reply = hana_box.MutationSettled('old-operation', observation)
        with mock.patch.object(self.provider, 'recover', return_value=reply), self.assertRaises(ValueError):
            _ = self.tick()
        self.assertEqual(self.record()['operation'], operation)
        self.assertIsInstance(self.book.read_snapshot().hana_compute[-1].state, ledger.HanaExposureOpen)

    def test_stale_boot_and_generation_never_authorize_commands(self) -> None:
        self.ready()
        assert isinstance(self.provider.boot, hana_box.VerifiedBoot)
        generation = self.generation()
        self.assertIsInstance(self.controller.permission('execution-one', 'old-boot', generation), hana_box.ClaimRefused)
        self.assertIsInstance(self.controller.permission('execution-one', self.provider.boot.boot_id, generation - 1), hana_box.ClaimRefused)
        self.assertIsInstance(self.controller.permission('execution-two', self.provider.boot.boot_id, generation), hana_box.ClaimRefused)

    def test_stale_boot_response_never_publishes_a_permit(self) -> None:
        _ = self.controller.request('execution-one')
        _ = self.tick()
        _ = self.tick()
        self.provider.boot = hana_box.VerifiedBoot('old-boot', 'old-operation')
        _ = self.tick()
        self.assertFalse(any(row.action == hana_box.Action.PUBLISH for row in self.provider.calls))
        self.assertIsInstance(self.controller.permission('execution-one', 'old-boot', self.generation()), hana_box.ClaimRefused)

    def test_stale_acknowledgment_keeps_the_publication_unresolved(self) -> None:
        _ = self.controller.request('execution-one')
        for _step in range(3):
            _ = self.tick()
        self.provider.bad_ack = True
        with self.assertRaises(ValueError):
            _ = self.tick()
        self.assertEqual(ledger.mapping(self.record()['operation'])['action'], hana_box.Action.PUBLISH.value)
        self.assertEqual(self.record()['state'], hana_box.Lifecycle.STARTING.value)

    def test_committed_incoming_claim_prevents_idle_stop(self) -> None:
        self.ready()
        self.assertTrue(self.controller.release('execution-one'))
        self.clock.advance(hana_box.read_settings(conf.read(self.workload_path)).idle_stop_seconds)
        self.assertIsInstance(self.controller.request('execution-two'), hana_box.ClaimCommitted)
        _ = self.tick()
        self.assertFalse(any(row.action == hana_box.Action.STOP for row in self.provider.calls))
        _ = self.tick()
        self.assertEqual(ledger.rows(self.record(), 'claims')[0]['execution'], 'execution-two')

    def test_idle_stop_intent_refuses_a_concurrent_new_claim(self) -> None:
        self.ready()
        self.assertTrue(self.controller.release('execution-one'))
        self.clock.advance(hana_box.read_settings(conf.read(self.workload_path)).idle_stop_seconds)
        self.assertEqual(self.tick(), hana_box.Lifecycle.STOPPING)
        received: list[hana_box.ClaimCommitted | hana_box.ClaimRefused] = []
        self.provider.during_call = lambda: received.append(self.controller.request('execution-two'))
        _ = self.tick()
        self.assertIsInstance(received[0], hana_box.ClaimRefused)
        self.provider.during_call = lambda: None
        self.assertEqual(self.tick(), hana_box.Lifecycle.IDLE_STOPPED)
        self.assertIsInstance(self.controller.request('execution-two'), hana_box.ClaimCommitted)

    def test_interruption_and_unknown_stop_never_authorize_manual_restart(self) -> None:
        self.ready()
        self.provider.state, self.provider.cause = 'stopped', hana_box.StopCause.INTERRUPTION
        self.assertEqual(self.tick(), hana_box.Lifecycle.INTERRUPTED)
        self.assertIsInstance(self.controller.request('execution-two'), hana_box.ClaimRefused)
        starts = len(self.provider.starts)
        _ = self.tick()
        self.assertEqual(len(self.provider.starts), starts)
        self.provider.cause = hana_box.StopCause.UNKNOWN
        self.assertEqual(self.tick(), hana_box.Lifecycle.STOPPED_UNKNOWN)
        self.assertIsInstance(self.controller.request('execution-two'), hana_box.ClaimRefused)

    def test_interruption_during_idle_stop_forbids_a_manual_restart(self) -> None:
        self.ready()
        self.assertTrue(self.controller.release('execution-one'))
        self.clock.advance(hana_box.read_settings(conf.read(self.workload_path)).idle_stop_seconds)
        self.assertEqual(self.tick(), hana_box.Lifecycle.STOPPING)
        self.assertEqual(self.record()['stop_cause'], hana_box.StopCause.IDLE.value)
        self.provider.cause = hana_box.StopCause.INTERRUPTION
        _ = self.tick()
        self.assertEqual(self.tick(), hana_box.Lifecycle.INTERRUPTED)
        self.assertIsInstance(self.controller.request('execution-two'), hana_box.ClaimRefused)
        starts = set(self.provider.starts)
        for _step in range(2):
            self.assertEqual(self.tick(), hana_box.Lifecycle.INTERRUPTED)
        self.assertEqual(self.provider.starts, starts)
        self.assertEqual(self.record()['claims'], [])

    def test_automatic_restart_is_counted_without_caller_while_disabled(self) -> None:
        text_setting(self.workload_path, 'enabled', 'off')
        self.provider.state = 'running'
        self.assertEqual(self.tick(), hana_box.Lifecycle.STOPPING)
        exposure = self.book.read_snapshot().hana_compute[-1]
        self.assertIsInstance(exposure.state, ledger.HanaExposureOpen)
        self.assertEqual(self.record()['claims'], [])
        self.assertFalse(any(row.action == hana_box.Action.PUBLISH for row in self.provider.calls))
        _ = self.tick()
        _ = self.tick()
        self.assertIsInstance(self.book.read_snapshot().hana_compute[-1].state, ledger.HanaExposureClosed)

    def test_renewal_preserves_execution_and_uses_expected_generation(self) -> None:
        self.ready()
        old = self.generation()
        claim = ledger.rows(self.record(), 'claims')[0]
        expiry = ledger.instant(ledger.mapping(self.record()['delivered']), 'expires')
        settings = hana_box.read_settings(conf.read(self.workload_path))
        self.clock.now = expiry - (2 * settings.controller_tick_timeout_s + 3 * settings.tick_interval_s)
        _ = self.tick()
        intent = ledger.mapping(self.record()['operation'])
        self.assertEqual(intent['action'], hana_box.Action.PUBLISH.value)
        self.assertEqual(intent['expected_generation'], old)
        self.assertGreater(self.generation(), old)
        self.assertEqual(ledger.rows(self.record(), 'claims')[0], claim)
        _ = self.tick()
        self.assertEqual(ledger.rows(self.record(), 'claims')[0], claim)

    def test_refresh_failure_does_not_stop_lifecycle_recovery(self) -> None:
        _ = self.controller.request('execution-one')
        events: list[str] = []
        def lifecycle() -> object:
            events.append('lifecycle')
            return self.controller.tick()
        def refresh() -> object:
            events.append('refresh')
            raise RuntimeError('The bill service is unavailable')
        with mock.patch.object(sys, 'stderr', StringIO()):
            for _step in range(4):
                self.clock.advance(1)
                self.assertEqual(aws_target.controller_tick(self.settings, self.state, self.workload_path,
                    lifecycle=lifecycle, refresh=refresh), 1)
        self.assertEqual(events, ['lifecycle', 'refresh'] * 4)
        self.assertEqual(self.record()['state'], hana_box.Lifecycle.READY.value)

    def test_supervisor_bounds_ticks_and_continues_after_timeout(self) -> None:
        calls: list[float] = []
        def run(arguments: list[str], *, timeout: float, check: bool, text: bool) -> subprocess.CompletedProcess[str]:
            self.assertIn('tick', arguments)
            self.assertTrue(text)
            self.assertFalse(check)
            calls.append(timeout)
            raise subprocess.TimeoutExpired(arguments, timeout)
        pauses: list[float] = []
        settings = hana_box.read_settings(conf.read(self.workload_path))
        with mock.patch.object(sys, 'stderr', StringIO()):
            self.assertEqual(aws_target.serve(self.settings, self.state, self.workload_path, run=run,
                pause=pauses.append, keep_running=lambda: len(calls) < 2), 0)
        self.assertEqual(calls, [settings.controller_tick_timeout_s] * 2)
        self.assertEqual(pauses, [settings.tick_interval_s] * 2)

    def test_serve_cli_dispatches_to_bounded_supervisor(self) -> None:
        with mock.patch.object(sys, 'argv', ['aws_target.py', 'serve', '--settings', str(self.settings),
                '--directory', str(self.state), '--hana-settings', str(self.workload_path)]), \
                mock.patch.object(aws_target, 'serve', return_value=0) as serve:
            self.assertEqual(aws_target.main(), 0)
            serve.assert_called_once_with(self.settings, self.state, self.workload_path)

    def test_caps_changed_before_start_confirm_stopped_without_starting(self) -> None:
        _ = self.controller.request('execution-one')
        _ = self.tick()
        workload = hana_spend.read_settings(conf.read(self.workload_path))
        self.costs({'daily_limit_usd': str(workload.storage_usd_per_month / 60)})
        _ = self.tick()
        self.assertEqual(len(self.provider.starts), 0)
        self.assertEqual(self.record()['reason'], 'daily_limit')
        self.assertIsInstance(self.book.read_snapshot().hana_compute[0].state, ledger.HanaExposureClosed)
        self.assertEqual(len(self.provider.recoveries), 1)
        self.assertEqual(len(ledger.rows(self.record(), 'claims')), 1)

    def test_caps_changed_before_publication_preserve_the_guest_deadline(self) -> None:
        _ = self.controller.request('execution-one')
        for _step in range(3):
            _ = self.tick()
        self.assertEqual(ledger.mapping(self.record()['operation'])['action'], hana_box.Action.PUBLISH.value)
        workload = hana_spend.read_settings(conf.read(self.workload_path))
        self.costs({'daily_limit_usd': str(workload.storage_usd_per_month / 60)})
        _ = self.tick()
        self.assertFalse(any(row.action == hana_box.Action.PUBLISH for row in self.provider.calls))
        self.assertIsNone(self.record()['delivered'])
        self.assertEqual(self.record()['reason'], 'daily_limit')

    def test_automatic_restart_cannot_reuse_an_interrupted_execution(self) -> None:
        self.ready()
        old_boot = self.provider.boot
        self.provider.state, self.provider.cause = 'stopped', hana_box.StopCause.INTERRUPTION
        _ = self.tick()
        assert isinstance(old_boot, hana_box.VerifiedBoot)
        self.provider.state = 'running'
        self.provider.boot = hana_box.VerifiedBoot('automatic-boot', old_boot.start_token)
        before = sum(row.action == hana_box.Action.PUBLISH for row in self.provider.calls)
        self.assertEqual(self.tick(), hana_box.Lifecycle.STOPPING)
        self.assertEqual(sum(row.action == hana_box.Action.PUBLISH for row in self.provider.calls), before)
        self.assertGreater(len(self.book.read_snapshot().hana_compute), 1)
        self.assertIsInstance(self.controller.permission('execution-one', 'automatic-boot', self.generation()), hana_box.ClaimRefused)

    def test_unprovisioned_tick_makes_no_provider_call(self) -> None:
        directory = self.root / 'empty-state'
        controller = hana_box.HanaController(directory, self.settings, self.workload_path, self.provider, clock=self.clock)
        self.assertEqual(controller.tick(), hana_box.Lifecycle.UNPROVISIONED)
        self.assertEqual(self.provider.calls, [])

    def test_obsolete_provider_response_cannot_replace_a_new_operation(self) -> None:
        self.ready()
        def change_operation() -> None:
            document = self.record()
            operation = ledger.mapping(document['operation'])
            operation['token'] = 'new-operation-token'
            _ = (self.state / 'hana/lifecycle.json').write_text(json.dumps(document))
        self.provider.during_call = change_operation
        _ = self.tick()
        self.assertEqual(ledger.mapping(self.record()['operation'])['token'], 'new-operation-token')
