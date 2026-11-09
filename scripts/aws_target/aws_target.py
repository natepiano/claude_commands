#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Control one AWS target through confirmed lifecycle and spending evidence."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
import fcntl
import json
from pathlib import Path
import shlex
import subprocess
import sys
import time
from typing import Literal, Protocol, cast, final, get_args

from scripts.aws_target import ledger, measurement, remote
from scripts.aws_target.config import ControlSettings, money, offload_enabled, read_control_settings, read_settings, service_loop
from scripts.build_queue import queue_snapshot
from scripts.build_targets.target_config import SshTarget, is_aws_target
from scripts.lib import conf
from scripts.lib.atomic_file import write_text_atomic
from scripts.build_queue import admission_records
from scripts.mac_test import mac_test, offload
from scripts.buildlog import store
from scripts.direct import step_record


class AwsExecutor(Protocol):
    def __call__(self, arguments: Sequence[str], *, capture_output: bool, text: bool,
                 timeout: float, check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]: ...


def execute_command(arguments: Sequence[str], *, capture_output: bool, text: bool,
                    timeout: float, check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(arguments, capture_output=capture_output, text=text, timeout=timeout, check=check, env=env)


class AwsUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class InstanceEvidence:
    instance_id: str
    generation: str
    state: ledger.InstanceState
    observed_at: int


@dataclass(frozen=True)
class ControlResult:
    state: str
    reason: str
    at: int
    generation: str = ''


@dataclass(frozen=True)
class UnpublishedPermissionSince:
    generation: str
    since: int

    @classmethod
    def read(cls, state: dict[str, object], generation: str, now: int) -> UnpublishedPermissionSince:
        raw = state.get('unpublished_permission')
        if isinstance(raw, dict):
            row = cast(dict[str, object], raw)
            since = row.get('since')
            if row.get('generation') == generation and type(since) is int and 0 <= since <= now:
                return cls(generation, since)
        return cls(generation, now)


@final
class AwsCli:
    def __init__(self, profile: str, region: str, timeout_s: float,
                 execute: AwsExecutor = execute_command) -> None:
        self.profile = profile
        self.region = region
        self.timeout_s = timeout_s
        self.execute = execute

    def call(self, operation: str, *arguments: str, service: str = 'ec2') -> dict[str, object]:
        result = self.execute(['aws', '--profile', self.profile, '--region', self.region,
                               service, operation, *arguments, '--output', 'json'], capture_output=True, text=True,
                              timeout=self.timeout_s, check=False)
        if result.returncode:
            raise AwsUnavailable(result.stderr.strip() or f'{operation}: failed')
        if not result.stdout.strip() or service == 'cloudformation' and operation == 'deploy':
            return {}
        raw = cast(object, json.loads(result.stdout))
        if not isinstance(raw, dict):
            raise AwsUnavailable(f'{operation}: invalid reply')
        return cast(dict[str, object], raw)

    def observe(self, instance_id: str, now: int) -> InstanceEvidence:
        row = self.call('describe-instances', '--instance-ids', instance_id)
        try:
            reservations = cast(list[dict[str, object]], row['Reservations'])
            instances = [instance for reservation in reservations
                         for instance in cast(list[dict[str, object]], reservation['Instances'])]
            if len(instances) != 1 or instances[0].get('InstanceId') != instance_id:
                raise AwsUnavailable('instance identity does not match')
            instance = instances[0]
            state = cast(dict[str, object], instance['State']).get('Name')
            generation = instance.get('LaunchTime')
            if state not in get_args(ledger.InstanceState) or state == 'unknown' or not isinstance(generation, str) or not generation:
                raise AwsUnavailable('instance lifecycle evidence is incomplete')
            return InstanceEvidence(instance_id, f'{instance_id}:{generation}', cast(ledger.InstanceState, state), now)
        except (KeyError, TypeError, AttributeError, IndexError) as error:
            raise AwsUnavailable('instance lifecycle reply is malformed') from error


@contextmanager
def control_lock(directory: Path) -> Generator[None, None, None]:
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'controller.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def instance_generation(directory: Path) -> str:
    try:
        row = mac_test.read_json_object(directory / 'controller.json')
        generation = row.get('generation') if row else ''
        return generation if isinstance(generation, str) else ''
    except (OSError, ValueError):
        return ''


@final
class Controller:
    def __init__(self, settings_path: Path, directory: Path, queue_directory: Path, *,
                 execute: AwsExecutor = execute_command, clock: Callable[[], float] = time.time,
                 notify: Callable[[ledger.LimitEvent], bool] = ledger.send_limit_event,
                 publish: Callable[[remote.AdmissionEnvelope], bool] | None = None,
                 target: str = 'aws') -> None:
        self.settings_path = settings_path
        self.directory = directory
        self.queue_directory = queue_directory
        self.execute = execute
        self.clock = clock
        self.notify = notify
        self.publish = publish
        self.target = target
        self.path = directory / 'controller.json'

    def _publish(self, envelope: remote.AdmissionEnvelope, settings: ControlSettings, configuration: conf.ConfigRead) -> bool:
        connection = offload.connection_settings(configuration)
        command = shlex.join(['python3', '.claude/scripts/aws_target/remote.py', 'publish',
                              '--directory', settings.remote_state_directory])
        result = subprocess.run(['ssh', *offload.ssh_options(connection), connection.host, command], input=json.dumps(remote.envelope_record(envelope)),
                                capture_output=True, text=True, timeout=settings.aws_timeout_s, check=False)
        return result.returncode == 0

    def _read(self) -> dict[str, object]:
        return mac_test.read_json_object(self.path) or {}

    def _save(self, state: dict[str, object], result: ControlResult) -> ControlResult:
        _ = state.pop('charge_error', None)
        if result.state == 'starting':
            if state.get('state') != 'starting' or 'starting_at' not in state:
                state['starting_at'] = result.at
        else:
            _ = state.pop('starting_at', None)
        state.update(asdict(result))
        write_text_atomic(self.path, json.dumps(state, sort_keys=True))
        return result

    def _queued(self, settings: ControlSettings, now: int, configuration: conf.ConfigRead) -> int:
        snapshot = queue_snapshot.read_queue_snapshot(self.queue_directory)
        if (not isinstance(snapshot, queue_snapshot.QueueSnapshot)
                or not 0 <= now - snapshot.written_at <= settings.snapshot_max_age_s
                or isinstance(snapshot.inputs.host, admission_records.ReadingUnavailable)):
            return 0
        values = configuration.values()
        return sum(1 for _, row in snapshot.inputs.ordered
                   if row.get('step') not in ('build', 'final')
                   and offload.linux_kind_permitted(values, str(row.get('step', ''))) and row.get('repo_identity')
                   and (repository_name := row.get('repository_name')) and 'token_left_at' not in row
                   and now - row['since'] >= settings.start_after_wait_s
                   and not offload.linux_version_declined_recently(
                       self.target, repository_name, settings.version_decline_hold_s, now))

    def _pinned_work(self, settings: ControlSettings, now: int, configuration: conf.ConfigRead) -> bool:
        values = configuration.values()
        return any(isinstance(record.selection, step_record.Pinned)
                   and record.selection.target == self.target
                   and record.state in (step_record.StepState.WAITING_HOLD, step_record.StepState.WAITING_SSH)
                   and self.target in record.eligible_targets
                   and offload.linux_kind_permitted(values, record.kind)
                   and isinstance(repository := offload.repository_identity(record.repo), offload.KnownRepository)
                   and not offload.linux_version_declined_recently(
                       self.target, repository.name, settings.version_decline_hold_s, now)
                   for record in step_record.live_records())

    def _prices(self, api: AwsCli, settings: ControlSettings, now: int) -> None:
        def fetch(_now: int) -> Sequence[ledger.PriceSample]:
            raw = api.call('describe-spot-price-history', '--instance-types', settings.instance_type,
                           '--product-descriptions', 'Linux/UNIX', '--max-items', '1')
            rows = cast(list[dict[str, object]], raw['SpotPriceHistory'])
            return tuple(ledger.PriceSample(int(datetime.fromisoformat(str(row['Timestamp']).replace('Z', '+00:00')).timestamp()),
                                            money(str(row['SpotPrice'])), 'DescribeSpotPriceHistory') for row in rows)
        _ = ledger.CostLedger(self.directory).refresh_prices(self.settings_path, now, fetch)

    def _release(self, evidence: InstanceEvidence) -> None:
        paths = mac_test.state_paths(self.target)
        captured = mac_test.capture_release_evidence(paths, evidence.generation, evidence.state, evidence.observed_at)
        if captured.claims:
            _ = mac_test.release_stopped_claims(paths, captured)

    def refresh_charges(self, api: AwsCli, now: int) -> measurement.ChargeRead | measurement.MeasurementUnavailable:

        def fetch(start: int, end: int) -> Decimal:
            settings = read_settings(self.settings_path)
            start_day = datetime.fromtimestamp(start, UTC).date().isoformat()
            end_day = datetime.fromtimestamp(end, UTC).date().isoformat()
            reply = api.call('get-cost-and-usage', '--time-period', f'Start={start_day},End={end_day}',
                             '--granularity', 'DAILY', '--metrics', 'UnblendedCost', '--filter',
                             json.dumps({'Tags': {'Key': settings.cost_explorer_tag_key, 'Values': [settings.cost_explorer_tag_value]}}), service='ce')
            rows = reply.get('ResultsByTime')
            items = cast(list[object], rows) if isinstance(rows, list) else []
            if len(items) != 1 or not isinstance(items[0], dict):
                raise ValueError('Cost Explorer daily reply is unavailable')
            row = cast(dict[str, object], items[0])
            total = cast(dict[str, object], row['Total'])
            cost = cast(dict[str, object], total['UnblendedCost'])
            if cost.get('Unit') != 'USD':
                raise ValueError('Cost Explorer requires USD')
            return money(str(cost['Amount']))

        return measurement.read_charges(self.settings_path, self.directory, now, fetch)

    def _active(self) -> bool:
        paths = mac_test.state_paths(self.target)
        with mac_test.locked(paths.lock):
            return bool(mac_test.read_claims(paths))

    def _stop(self, api: AwsCli, state: dict[str, object], evidence: InstanceEvidence, reason: str) -> ControlResult:
        now = int(self.clock())
        state['stop_generation'] = evidence.generation
        _ = self._save(state, ControlResult('stopping', reason, now, evidence.generation))
        _ = api.call('stop-instances', '--instance-ids', evidence.instance_id)
        confirmed = api.observe(evidence.instance_id, int(self.clock()))
        if confirmed.state in mac_test.RELEASE_STATES:
            self._release(confirmed)
        if confirmed.state in ('stopped', 'terminated'):
            self._clear_drain(state)
        return self._save(state, ControlResult(confirmed.state, reason, now, confirmed.generation))

    @staticmethod
    def _clear_drain(state: dict[str, object]) -> None:
        for key in ('drain_generation', 'drain_deadline', 'stop_generation'):
            _ = state.pop(key, None)

    def tick(self) -> ControlResult:
        return self._action('tick')

    def start(self) -> ControlResult:
        return self._action('start')

    def stop(self) -> ControlResult:
        return self._action('stop')

    def _action(self, action: Literal['tick', 'start', 'stop']) -> ControlResult:
        now = int(self.clock())
        configuration = conf.read(self.settings_path)
        if not offload_enabled(configuration):
            return ControlResult('off', 'disabled', now)
        state: dict[str, object] = {}
        with control_lock(self.directory):
            try:
                settings = read_control_settings(configuration, self.directory)
                if not settings.instance_id:
                    raise ValueError("deployment.json: no saved instance identity")
                configured = SshTarget(self.target, 'linux', self.settings_path)
                if not is_aws_target(configured):
                    raise ValueError('target is not an AWS target')
                state = self._read()
                api = AwsCli(settings.credentials_profile, settings.region, settings.aws_timeout_s, self.execute)
                evidence = api.observe(settings.instance_id, now)
                self._release(evidence)
                previous_generation = state.get('permitted_generation')
                state['instance_evidence'] = asdict(evidence)
                unpublished = UnpublishedPermissionSince.read(state, evidence.generation, now)
                if evidence.state == 'running':
                    state['unpublished_permission'] = asdict(unpublished)
                else:
                    _ = state.pop('unpublished_permission', None)
                if evidence.state in ('stopped', 'terminated'):
                    self._clear_drain(state)
                if state.get('stop_generation') == evidence.generation and evidence.state in ('running', 'pending'):
                    return self._stop(api, state, evidence, 'recorded_stop_intent')
                if action == 'stop':
                    if evidence.state in mac_test.RELEASE_STATES:
                        return self._save(state, ControlResult(evidence.state, 'manual_stop', now, evidence.generation))
                    return self._stop(api, state, evidence, 'manual_stop')
                ledger.CostLedger(self.directory).observe(ledger.InstanceObservation(now, evidence.state, 'DescribeInstances'))
                try:
                    self._prices(api, settings, now)
                except (AwsUnavailable, OSError, ValueError, KeyError, TypeError) as error:
                    state['price_error'] = str(error)
                try:
                    period_day = measurement.charge_period_day(read_settings(self.settings_path), now).isoformat()
                    if state.get('charge_period_day') != period_day:
                        reading = self.refresh_charges(api, now)
                        if isinstance(reading, measurement.ChargeRead):
                            state['charge_period_day'] = reading.day
                except (AwsUnavailable, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
                    print(f'aws_target: charge read unavailable: {error}', file=sys.stderr)
                decision = ledger.spending_guard(self.settings_path, self.directory, 'admission',
                                                 clock=self.clock, notify=self.notify)
                permission = ledger.CostLedger(self.directory).permission_for_decision(decision)
                if isinstance(decision, ledger.SpendingAllowed) and not isinstance(permission, ledger.PermissionAllowed):
                    decision = ledger.SpendingUnavailable(decision.at, permission.reason, decision.evidence)
                if isinstance(decision, ledger.SpendingAllowed):
                    state['permitted_generation'] = evidence.generation
                    if evidence.state == 'running':
                        measurement.record_go_live(self.directory, now)
                if state.get('drain_generation') == evidence.generation:
                    permission = ledger.PermissionUnavailable(decision.at, 'controlled_drain')
                envelope = remote.AdmissionEnvelope(1, self.target, 'admission', evidence.generation,
                                                    decision.at, decision.at + int(settings.permission_ttl_s), permission)
                published = False
                if evidence.state == 'running':
                    try:
                        published = self.publish(envelope) if self.publish is not None else self._publish(envelope, settings, configuration)
                    except (OSError, ValueError, subprocess.SubprocessError) as error:
                        state['publication_error'] = {'at': int(self.clock()), 'reason': str(error)}
                state['permission_published'] = published
                now = int(self.clock())
                if published:
                    _ = state.pop('unpublished_permission', None)
                elif evidence.state == 'running':
                    if (action == 'tick' and now - unpublished.since >= settings.idle_stop_minutes * 60
                            and not self._active()):
                        return self._stop(api, state, evidence, 'unpublished_idle')
                drain_reason = (decision.reason if not isinstance(decision, ledger.SpendingAllowed)
                                else 'controlled_drain' if state.get('drain_generation') == evidence.generation else '')
                if drain_reason:
                    if evidence.state != 'running':
                        return self._save(state, ControlResult('refused', drain_reason, now, evidence.generation))
                    if previous_generation != evidence.generation:
                        return self._stop(api, state, evidence, 'unexpected_generation:' + drain_reason)
                    deadline = (state.get('drain_deadline') if state.get('drain_generation') == evidence.generation
                                else now + int(settings.drain_max_minutes * 60))
                    if type(deadline) is not int:
                        raise ValueError('invalid drain deadline')
                    state.update({'drain_deadline': deadline, 'drain_generation': evidence.generation})
                    if now >= deadline or not self._active():
                        return self._stop(api, state, evidence, drain_reason)
                    return self._save(state, ControlResult('draining', drain_reason, now, evidence.generation))
                if evidence.state != 'stopped':
                    return self._save(state, ControlResult('starting' if evidence.state == 'pending' else evidence.state,
                                                           '' if published else 'permission_unpublished', now, evidence.generation))
                if (action == 'tick' and not self._pinned_work(settings, now, configuration)
                        and self._queued(settings, now, configuration) < settings.start_min_waiting):
                    return self._save(state, ControlResult('stopped', 'no_eligible_work', now, evidence.generation))
                backoff = state.get('capacity_retry_after', 0)
                if type(backoff) is not int or backoff > now:
                    return self._save(state, ControlResult('refused', 'capacity_backoff', now, evidence.generation))
                _ = self._save(state, ControlResult('starting', 'start_intent', now, evidence.generation))
                try:
                    _ = api.call('start-instances', '--instance-ids', evidence.instance_id)
                except AwsUnavailable as error:
                    if 'InsufficientInstanceCapacity' in str(error):
                        state['capacity_retry_after'] = now + int(settings.capacity_backoff_s)
                    raise
                return self._save(state, ControlResult('starting', 'start_requested', now, evidence.generation))
            except (OSError, ValueError, AwsUnavailable, KeyError, TypeError, subprocess.SubprocessError) as error:
                state['api_failure'] = {'at': now, 'reason': str(error)}
                return self._save(state, ControlResult('refused', str(error), now, str(state.get('generation', ''))))


def serve(settings_path: Path, directory: Path, *, execute: AwsExecutor = execute_command,
          pause: Callable[[float], object] = time.sleep) -> None:
    def tick() -> object:
        return execute([sys.executable, str(Path(__file__)), 'tick',
                        '--settings', str(settings_path), '--directory', str(directory)],
                       capture_output=False, text=True, check=False,
                       timeout=conf.read(settings_path).number('controller_tick_timeout_s'))
    service_loop(settings_path, 'tick_interval_s', tick, pause=pause)


class ControlArguments(argparse.Namespace):
    def __init__(self) -> None:
        super().__init__()
        self.command: str = ''
        self.settings: Path = conf.path('aws_target')
        self.directory: Path = Path()
        self.queue_directory: Path = store.root() / 'admission'


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument('command', choices=('tick', 'start', 'stop', 'serve'))
    _ = parser.add_argument('--settings', type=Path)
    _ = parser.add_argument('--directory', type=Path)
    _ = parser.add_argument('--queue-directory', type=Path)
    args = parser.parse_args(namespace=ControlArguments())
    directory = args.directory
    if directory == Path() and offload_enabled(conf.read(args.settings)):
        directory = offload.state_directory('aws')
    if args.command == 'serve':
        serve(args.settings, directory)
        return 0
    controller = Controller(args.settings, directory, args.queue_directory)
    result = {'tick': controller.tick, 'start': controller.start, 'stop': controller.stop}[args.command]()
    print(json.dumps(asdict(result)))
    return 1 if result.state == 'refused' else 0


if __name__ == '__main__':
    raise SystemExit(main())
