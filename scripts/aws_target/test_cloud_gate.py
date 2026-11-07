"""Exercise durable cloud gates with private files and a fake AWS transport."""
from __future__ import annotations

from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from decimal import Decimal
from io import StringIO
import json
from pathlib import Path
import subprocess
from typing import cast, override
from unittest import mock

from scripts.aws_target import cloud_gate, gate_box, ledger
from scripts.aws_target.aws_target import AwsCli
from scripts.aws_target.config import read_settings
from scripts.aws_target.ledger_test_support import CostFixture, instant
from scripts.buildlog import store
from scripts.build_targets import target_config
from scripts.production import affected_modules
from scripts.production import merge_gate as gate


class FakeGateAws:
    """Implement only the operations that a gate scaler can request."""
    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.launches: list[dict[str, object]] = []
        self.refusals: list[str] = []
        self.result: dict[str, object] = {}
        self.has_result: bool = True
        self.has_log: bool = True
        self.state: str = 'running'
        self.terminated: bool = False
        self.confirm_termination: bool = True
        self.upload_error: str = ''

    def __call__(self, arguments: Sequence[str], **_options: object) -> subprocess.CompletedProcess[str]:
        args = list(arguments)
        self.commands.append(args)
        index = args.index('--region') + 2
        service, operation = args[index:index + 2]
        reply: dict[str, object] = {}
        error = ''
        if operation == 'get-cost-and-usage':
            reply = {'ResultsByTime': [{'Total': {'UnblendedCost': {'Amount': '0', 'Unit': 'USD'}}}]}
        elif operation == 'put-object':
            error = self.upload_error
        elif operation == 'run-instances':
            self.launches.append(ledger.mapping(cast(object, json.loads(args[args.index('--cli-input-json') + 1]))))
            error = self.refusals.pop(0) if self.refusals else ''
            reply = {'Instances': [{'InstanceId': 'i-fixture'}]}
        elif operation == 'get-object':
            key_at = args.index('--key') + 1
            key, destination = args[key_at], Path(args[key_at + 1])
            if key.endswith('/gate.log') and self.has_log:
                _ = destination.write_text('fixture cloud output\n')
            elif key.endswith('/result.json') and self.has_result:
                cloud_gate.save(destination, self.result)
            else:
                error = 'NoSuchKey'
        elif operation == 'terminate-instances':
            self.terminated = True
        elif operation == 'describe-instances':
            state = 'terminated' if self.terminated and self.confirm_termination else self.state
            reply = {'Reservations': [{'Instances': [{'InstanceId': 'i-fixture', 'State': {'Name': state},
                                                      'LaunchTime': '2026-01-15T00:00:00+00:00'}]}]}
        else:
            raise AssertionError((service, operation))
        return subprocess.CompletedProcess(args, int(bool(error)), json.dumps(reply), error)


class CloudGateTests(CostFixture):
    now: float = 0
    book: ledger.CostLedger = ledger.CostLedger(Path())
    fake: FakeGateAws = FakeGateAws()
    folder: Path = Path()
    request: dict[str, object] = {}
    deployment: dict[str, object] = {}

    @override
    def setUp(self) -> None:
        super().setUp()
        repository = Path(__file__).resolve().parents[2]
        _ = (self.root / 'merge_gate_instances.conf').write_bytes(
            (repository / 'config/merge_gate_instances.conf').read_bytes())
        self.now = float(instant('2026-01-15T01:00:00+00:00'))
        self.book = ledger.CostLedger(self.state)
        self.book.record_inventory(ledger.ResourceInventory(int(self.now) - 1,
            (ledger.PricedResource('fixture-fixed', Decimal(0)),), 'fixture'))
        self.book.observe(ledger.InstanceObservation(int(self.now) - 1, 'stopped', 'fixture'))
        self.fake = FakeGateAws()
        self.folder = self.root / 'attempt'
        self.folder.mkdir()
        _ = (self.folder / 'source.bundle').write_bytes(b'private bundle')
        self.request = {'attempt': 'attempt', 'commit': 'commit', 'tree': 'tree', 'mode': 'merge-gate',
            'origin': '/private/origin', 'modules': ['scripts/fixture/test_ok.py'],
            'expires': int(self.now) + 1200, 'module_time_limit_s': 1800, 'source_key': 'source/private.bundle'}
        cloud_gate.save(self.folder / 'request.json', self.request)
        self.deployment = {'deployment_id': 'fixture', 'bucket': 'fixture-bucket', 'template': 'fixture-template',
            'version': '1', 'security_group': 'fixture-group', 'revision': 'fixture-revision',
            'artifact_prefix': 'artifacts/fixture-role:',
            'subnets': [{'id': 'fixture-a', 'zone': 'a'}, {'id': 'fixture-b', 'zone': 'b'}]}
        self.fake.result = self.result()

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def result(self, status: int = 0) -> dict[str, object]:
        run = gate.GateRun((gate.GateJobResult('fixture', 'test_ok.py', status, 1, 'OK'),), 1, 2)
        record = gate_box.completion_record(run, 'attempt', 'commit', 'tree', 'fixture-guest', self.now,
                                            self.now + 1, 'merge-gate')
        return {'attempt': 'attempt', 'commit': 'commit', 'tree': 'tree', 'host': 'fixture-guest',
                'verdict': 'pass' if run.passed else 'fail', 'gate': gate_box.encode_run(run), 'record': record}

    def execute(self) -> dict[str, object]:
        worker = cloud_gate.GateWorker(AwsCli('build-scaler', 'fixture-region', 1, execute=self.fake),
            self.book, self.settings, clock=lambda: self.now, pause=self.advance)
        return worker.execute(self.folder, self.request, self.deployment)

    def test_complete_result_keeps_log_and_confirms_termination(self) -> None:
        outcome = self.execute()
        self.assertEqual(outcome['status'], 'complete')
        self.assertEqual((self.folder / 'gate.log').read_text(), 'fixture cloud output\n')
        self.assertIsInstance(self.book.read_snapshot().gate_reservations[0].state, ledger.GateTerminated)
        self.assertTrue(self.fake.terminated)

    def test_first_gate_reconciles_the_period_before_deployment(self) -> None:
        self.assertEqual(self.execute()['status'], 'complete')
        charge_files = list((self.state / 'charges').glob('*.json'))
        self.assertEqual(len(charge_files), 1)
        reading = ledger.read_charge(charge_files[0])
        self.assertLess(reading.period_end, self.book.read_snapshot().inventories[0].at)
        self.assertEqual(reading.estimated_usd, Decimal(0))
        self.assertEqual(reading.actual_usd, Decimal(0))

    def test_remote_failure_remains_red_without_fallback(self) -> None:
        self.fake.result = self.result(1)
        outcome = self.execute()
        self.assertEqual(outcome['status'], 'complete')
        result = ledger.mapping(outcome['result'])
        self.assertFalse(gate_box.decode_run(result['gate']).passed)

    def test_capacity_tries_every_zone_before_the_smaller_size(self) -> None:
        self.fake.refusals = ['InsufficientInstanceCapacity'] * 3
        self.assertEqual(self.execute()['status'], 'complete')
        choices = [(row['InstanceType'], ledger.rows(row, 'NetworkInterfaces')[0]['SubnetId'])
                   for row in self.fake.launches]
        self.assertEqual(choices, [('m7a.8xlarge', 'fixture-a'), ('m7a.8xlarge', 'fixture-b'),
                                   ('m7a.4xlarge', 'fixture-a'), ('m7a.4xlarge', 'fixture-b')])

    def test_all_capacity_refusals_allow_local_fallback(self) -> None:
        self.fake.refusals = ['InsufficientInstanceCapacity'] * 4
        self.assertEqual(self.execute()['status'], 'fallback')
        self.assertIsInstance(self.book.read_snapshot().gate_reservations[0].state, ledger.GateAbsent)

    def test_aws_outage_before_launch_allows_fallback(self) -> None:
        self.fake.upload_error = 'EndpointConnectionError'
        self.assertEqual(self.execute()['status'], 'fallback')
        self.assertEqual(self.fake.launches, [])

    def test_missing_image_allows_fallback(self) -> None:
        self.fake.refusals = ['InvalidAMIID.NotFound']
        self.assertEqual(self.execute()['status'], 'fallback')

    def test_access_denial_does_not_allow_local_fallback(self) -> None:
        self.fake.upload_error = 'AccessDenied'
        self.assertEqual(self.execute()['status'], 'red')
        self.assertIsInstance(self.book.read_snapshot().gate_reservations[0].state, ledger.GateAbsent)

    def test_lost_launch_reply_reuses_exact_arguments_and_token(self) -> None:
        self.fake.refusals = ['EndpointConnectionError']
        self.assertEqual(self.execute()['status'], 'complete')
        self.assertEqual(len(self.fake.launches), 2)
        self.assertEqual(self.fake.launches[0], self.fake.launches[1])

    def test_uncertain_launch_retains_reservation_and_recovers_after_restart(self) -> None:
        self.fake.refusals = ['EndpointConnectionError'] * 2
        self.assertEqual(self.execute()['status'], 'pending')
        self.assertIsInstance(self.book.read_snapshot().gate_reservations[0].state, ledger.GateLaunchPending)
        self.assertEqual(self.execute()['status'], 'complete')
        self.assertEqual(self.fake.launches[0], self.fake.launches[-1])
        self.assertEqual(len(self.book.read_snapshot().gate_reservations), 1)

    def test_invalid_result_identity_verdict_or_modules_is_red(self) -> None:
        for field, value in (('attempt', 'other'), ('tree', 'other'), ('commit', 'other'),
                             ('host', ''), ('verdict', 'fail')):
            with self.subTest(field=field):
                row = {**self.result(), field: value}
                with self.assertRaises(ValueError):
                    cloud_gate.validate_result(row, self.request)
        run = gate.GateRun((gate.GateJobResult('fixture', 'test_other.py', 0, 1, ''),), 1, 1)
        with self.assertRaises(ValueError):
            cloud_gate.validate_result({**self.result(), 'gate': gate_box.encode_run(run)}, self.request)

    def test_missing_final_log_is_red(self) -> None:
        self.fake.has_log = False
        self.assertEqual(self.execute()['status'], 'red')
        self.assertTrue(self.fake.terminated)

    def test_dead_instance_without_result_is_red(self) -> None:
        self.fake.has_result = False
        self.fake.state = 'terminated'
        self.assertEqual(self.execute()['status'], 'red')
        self.assertTrue(self.fake.terminated)

    def test_expired_instance_without_result_is_red(self) -> None:
        self.fake.has_result = False
        self.request['expires'] = int(self.now) + 1
        self.assertEqual(self.execute()['status'], 'red')
        self.assertTrue(self.fake.terminated)

    def test_unconfirmed_termination_keeps_charging(self) -> None:
        self.fake.confirm_termination = False
        self.assertEqual(self.execute()['status'], 'red')
        self.assertIsInstance(self.book.read_snapshot().gate_reservations[0].state, ledger.GateRunning)
        before = self.book.estimate(read_settings(self.settings), int(self.now)).monthly.usd
        self.advance(60)
        self.assertGreater(self.book.estimate(read_settings(self.settings), int(self.now)).monthly.usd, before)

    def test_combined_rust_cost_refuses_gate_before_launch(self) -> None:
        self.book.observe(ledger.InstanceObservation(int(self.now), 'running', 'fixture'))
        self.advance(18 * 3600)
        self.request['expires'] = int(self.now) + 1200
        self.assertEqual(self.execute(), {'status': 'fallback', 'reason': 'daily_limit'})
        self.assertEqual(self.fake.launches, [])

    def test_parallel_admissions_share_one_cost_reservation_lock(self) -> None:
        self.costs({'daily_limit_usd': '4', 'limit_reserve_usd': '3'})
        def reserve(index: int) -> ledger.GateReserved | ledger.GateCapReached:
            return self.book.reserve_gate(self.settings, ledger.GateReservation(
                str(index), int(self.now), int(self.now) + 3600, Decimal('.6'), Decimal(0)))
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(reserve, range(2)))
        self.assertEqual(sum(isinstance(row, ledger.GateReserved) for row in results), 1)
        self.assertEqual(len(self.book.read_snapshot().gate_reservations), 1)

    def test_future_reservation_checks_the_next_day_and_month(self) -> None:
        self.costs({'daily_limit_usd': '4', 'monthly_limit_usd': '4', 'limit_reserve_usd': '3'})
        boundary = instant('2026-01-31T23:59:59+00:00')
        result = self.book.reserve_gate(self.settings, ledger.GateReservation(
            'boundary', boundary, boundary + 3600, Decimal('1.1'), Decimal(0)))
        self.assertIsInstance(result, ledger.GateCapReached)
        self.assertEqual(self.book.read_snapshot().gate_reservations, [])

    def test_gate_reservation_also_restricts_rust_admission(self) -> None:
        self.costs({'daily_limit_usd': '4', 'limit_reserve_usd': '3'})
        _ = self.book.reserve_gate(self.settings, ledger.GateReservation(
            'busy', int(self.now), int(self.now) + 3600, Decimal('.99'), Decimal(0)))
        self.book.observe(ledger.InstanceObservation(int(self.now), 'running', 'fixture'))
        decision = self.book.guard(self.settings, 'admission', int(self.now) + 60, notify=lambda _event: True)
        self.assertIsInstance(decision, ledger.SpendingRefused)

    def test_import_uses_real_remote_host_and_appends_once(self) -> None:
        destination = self.root / 'buildlog/fixture-guest.jsonl'
        destination.parent.mkdir()
        _ = destination.write_text('{"interrupted":')
        with mock.patch.object(store, 'host_file', return_value=destination) as host_file:
            cloud_gate.import_record(self.folder, self.result())
            cloud_gate.import_record(self.folder, self.result())
        self.assertEqual(host_file.call_args.args[0], 'fixture-guest')
        records = [ledger.mapping(cast(object, json.loads(line))) for line in destination.read_text().splitlines()]
        self.assertEqual([row['id'] for row in records], ['gate-attempt'])
        self.assertEqual(records[0]['host'], 'fixture-guest')

    def test_remote_red_never_calls_the_local_runner(self) -> None:
        remote = gate.GateRun((gate.GateJobResult('fixture', 'test_ok.py', 1, 1, ''),), 1, 1)
        with (mock.patch.object(cloud_gate, 'run', return_value=cloud_gate.CloudCompleted(remote)),
              mock.patch.object(gate, 'run_suites') as local):
            self.assertEqual(gate.run_gate_suites(self.root), remote)
        local.assert_not_called()

    def test_allowed_fallback_calls_the_original_local_runner(self) -> None:
        with (mock.patch.object(cloud_gate, 'run', return_value=cloud_gate.LocalFallback('AWS is down')),
              mock.patch.object(gate, 'run_suites') as local, redirect_stdout(StringIO())):
            _ = gate.run_gate_suites(self.root, full=True, mode=gate.SuiteMode.TARGETED_TEST)
        self.assertTrue(local.call_args.kwargs['full'])
        self.assertEqual(local.call_args.kwargs['mode'], gate.SuiteMode.TARGETED_TEST)


class CloudRouteTests(CostFixture):
    repo: Path = Path()
    modules: list[tuple[str, Path]] = []

    @override
    def setUp(self) -> None:
        super().setUp()
        repository = Path(__file__).resolve().parents[2]
        for name in ('merge_gate', 'merge_gate_instances'):
            _ = (self.root / (name + '.conf')).write_bytes((repository / 'config' / (name + '.conf')).read_bytes())
        self.repo = self.root / 'repo'
        folder = self.repo / 'scripts/fixture'
        folder.mkdir(parents=True)
        for name in ('test_one.py', 'test_two.py'):
            _ = (folder / name).write_text('import unittest\n')
        for args in (('init', '-q', '-b', 'fixture'), ('config', 'user.name', 'Fixture'),
                     ('config', 'user.email', 'fixture@example.invalid'), ('add', '.'), ('commit', '-qm', 'Fixture'),
                     ('remote', 'add', 'origin', '/private/origin'), ('update-ref', 'refs/remotes/origin/main', 'HEAD')):
            _ = gate.checked_git(self.repo, *args)
        self.modules = [('fixture', path) for path in sorted(folder.glob('test_*.py'))]
        _ = self.enterContext(mock.patch.object(cloud_gate, 'discover_test_modules', return_value=self.modules))
        _ = self.enterContext(mock.patch.object(target_config, 'ssh_target_state_directory', return_value=self.state))

    def test_bundle_keeps_exact_tree_main_and_current_branch(self) -> None:
        folder = self.root / 'attempt'
        request = cloud_gate.prepare(self.repo, folder, gate.SuiteMode.MERGE_GATE, False, 1800)
        self.assertEqual(request['tree'], gate.checked_git(self.repo, 'rev-parse', 'HEAD^{tree}'))
        self.assertEqual(request['commit'], gate.checked_git(self.repo, 'rev-parse', 'HEAD'))
        refs = gate.checked_git(self.repo, 'bundle', 'list-heads', str(folder / 'source.bundle'))
        self.assertIn('refs/remotes/origin/main', refs)
        self.assertIn('refs/heads/fixture', refs)
        self.assertEqual(request['modules'], ['scripts/fixture/test_one.py', 'scripts/fixture/test_two.py'])

    def test_targeted_selection_runs_locally_and_sends_only_selected_paths(self) -> None:
        selection = affected_modules.AffectedSelection(affected_modules.CertifiedBase('base', '2026-01-15T00:00:00+00:00'),
                                            ('scripts/fixture/test_one.py',), (self.modules[0][1],))
        with (mock.patch.object(gate.PassedTreeLedger, 'certified_base', return_value=selection.base),
              mock.patch.object(affected_modules, 'select_affected', return_value=selection) as select,
              redirect_stdout(StringIO())):
            request = cloud_gate.prepare(self.repo, self.root / 'attempt', gate.SuiteMode.TARGETED_TEST, False, 1800)
        self.assertEqual(select.call_args.args[0], self.repo)
        self.assertEqual(request['modules'], ['scripts/fixture/test_one.py'])

    def test_session_runs_only_scaler_service_and_imports_explicit_record(self) -> None:
        cloud_gate.save(self.state / 'gate-deployment.json', {'image': 'fixture-image', 'template': 'fixture-template'})
        commands: list[list[str]] = []

        def service(command: Sequence[str]) -> int:
            commands.append(list(command))
            folder = Path(command[command.index('--worker') + 1])
            request = cloud_gate.read(folder / 'request.json')
            run = gate.GateRun(tuple(gate.GateJobResult('fixture', path.name, 0, 1, '') for _, path in self.modules), 1, 1)
            row: dict[str, object] = {key: request[key] for key in ('attempt', 'commit', 'tree')}
            row.update(host='remote-host', verdict='pass', gate=gate_box.encode_run(run), record=gate_box.completion_record(
                run, ledger.text(request, 'attempt'), ledger.text(request, 'commit'), ledger.text(request, 'tree'),
                'remote-host', 0, 1, 'merge-gate'))
            cloud_gate.save(folder / 'outcome.json', {'status': 'complete', 'result': row})
            _ = (folder / 'gate.log').write_text('cloud log\n')
            return 0

        destination = self.root / 'record.jsonl'
        with (mock.patch.object(cloud_gate, 'AwsCli', side_effect=AssertionError('The session must not call AWS')),
              mock.patch.object(store, 'host_file', return_value=destination), redirect_stdout(StringIO()) as output):
            outcome = cloud_gate.run(self.repo, mode=gate.SuiteMode.MERGE_GATE, full=False,
                                     module_time_limit_s=1800, service=service)
        self.assertIsInstance(outcome, cloud_gate.CloudCompleted)
        self.assertIn('cloud log', output.getvalue())
        self.assertEqual(commands[0][0], 'systemd-run')
        self.assertIn('--setenv=AWS_SHARED_CREDENTIALS_FILE=' + str(self.root / '.config/aws-build/credentials'), commands[0])
        self.assertIn('--property=KillMode=control-group', commands[0])
        self.assertEqual(ledger.mapping(cast(object, json.loads(destination.read_text())))['host'], 'remote-host')

    def test_absent_stack_needs_no_service_or_aws_call(self) -> None:
        service = mock.Mock(side_effect=AssertionError('An absent stack needs no service'))
        outcome = cloud_gate.run(self.repo, mode=gate.SuiteMode.MERGE_GATE, full=False,
                                 module_time_limit_s=1800, service=service)
        self.assertIsInstance(outcome, cloud_gate.LocalFallback)
        service.assert_not_called()
