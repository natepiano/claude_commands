"""Exercise durable cloud gates with private files and a fake AWS transport."""
from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from decimal import Decimal
from io import StringIO
import base64
import gzip
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import cast, override
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from scripts.aws_target import cloud_gate, gate_box, ledger
from scripts.aws_target.aws_target import USAGE_ONLY, AwsCli
from scripts.aws_target.config import read_settings
from scripts.aws_target.ledger_test_support import CostFixture, account_reply, instant, numeric_settings
from scripts.build_queue import admission_client, admission_protocol as wire, cloud_progress, progress_profile
from scripts.build_queue.admission_test_support import AdmissionFixture, caller_admission
from scripts.buildlog import store
from scripts.build_targets import target_config
from scripts.production import affected_modules
from scripts.production import merge_gate as gate
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


class FakeGateAws:
    """Implement only the operations that a gate scaler can request."""
    def __init__(self) -> None:
        self.log: str = 'fixture cloud output\n'
        self.commands: list[list[str]] = []
        self.launches: list[dict[str, object]] = []
        self.refusals: list[str] = []
        self.result: dict[str, object] = {}
        self.has_result: bool = True
        self.has_log: bool = True
        self.state: str = 'running'
        self.state_reason: dict[str, str] = {}
        self.has_instance: bool = True
        self.terminated: bool = False
        self.confirm_termination: bool = True
        self.upload_error: str = ''
        self.upload_timeout: bool = False
        self.launch_timeouts: int = 0
        self.read_timeouts: int = 0
        self.billing_pages: list[dict[str, object]] = []

    def __call__(self, arguments: Sequence[str], **_options: object) -> subprocess.CompletedProcess[str]:
        args = list(arguments)
        self.commands.append(args)
        index = args.index('--region') + 2
        service, operation = args[index:index + 2]
        reply: dict[str, object] = {}
        error = ''
        if operation == 'get-cost-and-usage':
            reply = self.billing_pages.pop(0) if self.billing_pages else account_reply(arguments)
        elif operation == 'export-credentials':
            assert service == 'configure'
            reply = {'AccessKeyId': 'fixture-access', 'SecretAccessKey': 'fixture-secret', 'SessionToken': 'fixture-token'}
        elif operation == 'put-object':
            if self.upload_timeout:
                raise subprocess.TimeoutExpired('aws s3api put-object', 30)
            error = self.upload_error
        elif operation == 'run-instances':
            self.launches.append(ledger.mapping(cast(object, json.loads(args[args.index('--cli-input-json') + 1]))))
            if self.launch_timeouts:
                self.launch_timeouts -= 1
                raise subprocess.TimeoutExpired('aws run-instances', 1)
            error = self.refusals.pop(0) if self.refusals else ''
            reply = {'Instances': [{'InstanceId': 'i-fixture'}]}
        elif operation == 'get-object':
            if self.read_timeouts:
                self.read_timeouts -= 1
                raise subprocess.TimeoutExpired('aws s3api get-object', 30)
            key_at = args.index('--key') + 1
            key, destination = args[key_at], Path(args[key_at + 1])
            if key.endswith('/gate.log') and self.has_log:
                _ = destination.write_text(self.log)
            elif key.endswith('/result.json') and self.has_result:
                cloud_gate.save(destination, self.result)
            else:
                error = 'NoSuchKey'
        elif operation == 'terminate-instances':
            self.terminated = True
        elif operation == 'describe-instances':
            state = 'terminated' if self.terminated and self.confirm_termination else self.state
            instance: dict[str, object] = {'InstanceId': 'i-fixture', 'State': {'Name': state},
                                          'LaunchTime': '2026-01-15T00:00:00+00:00'}
            if self.state_reason:
                instance['StateReason'] = self.state_reason
            reply = {'Reservations': [{'Instances': [instance]}] if self.has_instance else []}
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
        _ = (self.root / 'cloud_gate.conf').write_bytes(
            (repository / 'config/cloud_gate.conf').read_bytes())
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
            self.book, self.settings, self.folder / 'reservation.cgroup', clock=lambda: self.now, pause=self.advance)
        return worker.execute(self.folder, self.request, self.deployment)

    def test_complete_result_keeps_log_and_confirms_termination(self) -> None:
        outcome = self.execute()
        self.assertEqual(outcome['status'], 'complete')
        self.assertEqual((self.folder / 'gate.log').read_text(), 'fixture cloud output\n')
        self.assertIsInstance(self.book.read_snapshot().gate_reservations[0].state, ledger.GateTerminated)
        self.assertTrue(self.fake.terminated)

    def test_saved_image_changes_only_the_launch_image(self) -> None:
        self.assertEqual(self.execute()['status'], 'complete')
        official = self.fake.launches[-1]
        self.deployment['image'] = 'ami-saved-gate'
        (self.folder / 'launch.json').unlink()
        self.assertEqual(self.execute()['status'], 'complete')
        saved = self.fake.launches[-1]
        self.assertEqual(saved['ImageId'], 'ami-saved-gate')
        self.assertEqual({key: value for key, value in saved.items() if key != 'ImageId'}, official)

    def test_poll_read_timeout_retries_and_completes(self) -> None:
        self.fake.read_timeouts = 1
        self.assertEqual(self.execute()['status'], 'complete')
        reads = [command[command.index('--key') + 1] for command in self.fake.commands if 'get-object' in command]
        self.assertEqual(reads[:2], ['artifacts/attempt/gate.log', 'artifacts/attempt/gate.log'])
        self.assertIn('artifacts/attempt/result.json', reads)
        self.assertEqual(len(self.fake.launches), 1)
        self.assertTrue(self.fake.terminated)

    def test_worker_sigterm_during_result_poll_terminates_launched_instance(self) -> None:
        cloud_gate.save(self.folder / 'deployment.json', self.deployment)
        script = '''import json, signal, sys
from pathlib import Path
from unittest import mock
from scripts.aws_target import cloud_gate, ledger
from scripts.aws_target.aws_target import AwsCli
from scripts.aws_target.test_cloud_gate import FakeGateAws

folder, state, configuration = map(Path, sys.argv[1:4])
at = float(sys.argv[4])
fake = FakeGateAws()
fake.has_result = False
def execute(arguments, **options):
    reply = fake(arguments, **options)
    (folder / 'commands.json').write_text(json.dumps(fake.commands))
    if 'get-object' in arguments and arguments[arguments.index('--key') + 1].endswith('/result.json'):
        signal.raise_signal(signal.SIGTERM)
    return reply
api = AwsCli('build-scaler', 'fixture-region', 1, execute=execute)
worker = cloud_gate.GateWorker(api, ledger.CostLedger(state), configuration, folder / 'reservation.cgroup', clock=lambda: at)
with (mock.patch.object(cloud_gate, 'AwsCli', return_value=api),
      mock.patch.object(cloud_gate, 'GateWorker', return_value=worker),
      mock.patch.object(sys, 'argv', ['cloud-gate', '--worker', str(folder), '--directory', str(state),
                                   '--sidecar', str(folder / 'reservation.cgroup'), '--settings', str(configuration)])):
    raise SystemExit(cloud_gate.main())
'''
        reply = subprocess.run([sys.executable, '-c', script, str(self.folder), str(self.state), str(self.settings), str(self.now)],
                               capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        commands = cast(list[list[str]], json.loads((self.folder / 'commands.json').read_text()))
        terminated = [command[command.index('--instance-ids') + 1] for command in commands if 'terminate-instances' in command]
        self.assertEqual(terminated, ['i-fixture'], reply.stdout + reply.stderr)
        self.assertEqual(reply.returncode, 143, reply.stderr)
        self.assertEqual(self.book.read_snapshot().gate_reservations[0].state,
                         ledger.GateTerminated(int(self.now), 'i-fixture'))
        self.assertEqual(cloud_gate.read(self.folder / 'outcome.json'), {
            'status': 'fallback', 'reason': 'The worker stopped, so its instance was terminated'})

    def test_each_log_download_publishes_module_progress(self) -> None:
        self.fake.log = ('Runner: 1 modules, whole machine, 32 CPUs\n'
                         + 'fixture/test_ok.py exit=0 seconds=1.00\n')
        with mock.patch.object(progress_profile, 'publish_counter', wraps=progress_profile.publish_counter) as publish:
            self.assertEqual(self.execute()['status'], 'complete')
        sidecar = self.folder / 'reservation.cgroup'
        counter = progress_profile.ProgressCounter('modules', 1, 1)
        self.assertEqual(progress_profile.read_counter(sidecar), counter)
        self.assertEqual(publish.call_count, 2)
        publish.assert_has_calls([mock.call(sidecar, counter), mock.call(sidecar, counter)])

    def test_module_progress_counts_each_module_once(self) -> None:
        text = ('Runner: 2 modules, whole machine, 16 CPUs\n'
                + 'fixture/test_one.py exit=1 seconds=1.00\n'
                + 'fixture/test_two.py exit=-15 seconds=2.00 stopped\n'
                + 'fixture/test_one.py exit=0 seconds=1.00\n')
        self.assertEqual(cloud_gate.log_progress(text), progress_profile.ProgressCounter('modules', 2, 2))
        self.assertIsInstance(cloud_gate.log_progress('Preparing tools\n'), progress_profile.NoCounter)
        self.assertIsInstance(cloud_gate.log_progress('Runner: 0 modules, whole machine, 32 CPUs\n'),
                              progress_profile.NoCounter)

    def test_each_log_download_publishes_cloud_state_and_latest_failures(self) -> None:
        self.fake.log = ('Runner: 2 modules, whole machine, 16 CPUs\n'
                        + 'fixture/test_one.py exit=1 seconds=1.00\n'
                        + 'fixture/test_two.py exit=-15 seconds=2.00 stopped\n'
                        + 'fixture/test_one.py exit=0 seconds=1.00\n'
                        + 'Cloud gate: uploading\n')
        with mock.patch.object(cloud_progress, 'publish', wraps=cloud_progress.publish) as publish:
            self.assertEqual(self.execute()['status'], 'complete')
        expected = cloud_progress.CloudProgress('uploading', 16, 1, self.now)
        sidecar = self.folder / 'reservation.cgroup'
        self.assertEqual(cloud_progress.read(sidecar), expected)
        self.assertEqual(publish.call_args_list[-2:], [mock.call(sidecar, expected), mock.call(sidecar, expected)])
        states = [cast(cloud_progress.CloudProgress, call.args[1]).state for call in publish.call_args_list]
        self.assertEqual(states, ['requesting', 'requesting', 'setup', 'uploading', 'uploading'])

    def test_cloud_state_tracks_setup_running_and_capacity_fallback_size(self) -> None:
        self.assertEqual(cloud_gate.log_cloud_progress('Preparing tools\n', 32, 1),
                         cloud_progress.CloudProgress('setup', 32, 0, 1))
        self.assertEqual(cloud_gate.log_cloud_progress('Runner: 3 modules, whole machine, 16 CPUs\n', 32, 2),
                         cloud_progress.CloudProgress('running', 16, 0, 2))
        self.fake.refusals = ['InsufficientInstanceCapacity'] * 3
        with mock.patch.object(cloud_progress, 'publish', wraps=cloud_progress.publish) as publish:
            self.assertEqual(self.execute()['status'], 'complete')
        sizes = [cast(cloud_progress.CloudProgress, call.args[1]).size_vcpus for call in publish.call_args_list]
        self.assertEqual(sizes, [0, 32, 32, 16, 16, 16, 16, 16])

    def test_instance_and_volume_tags_use_the_machine_name(self) -> None:
        self.assertEqual(self.execute()['status'], 'complete')
        for resource in ledger.rows(self.fake.launches[0], 'TagSpecifications'):
            tags = {str(row['Key']): row['Value'] for row in ledger.rows(resource, 'Tags')}
            self.assertEqual(tags['Name'], 'cloud-claude-gate')
            self.assertEqual(tags['Workload'], 'cloud-claude-gate')

    def test_first_gate_reconciles_the_period_before_deployment(self) -> None:
        self.assertEqual(self.execute()['status'], 'complete')
        charge_files = list((self.state / 'charges').glob('*.json'))
        self.assertEqual(len(charge_files), 1)
        reading = ledger.read_charge(charge_files[0])
        self.assertLess(reading.period_end, self.book.read_snapshot().inventories[0].at)
        self.assertEqual(reading.estimated_usd, Decimal(0))
        self.assertEqual(reading.actual_usd, Decimal(0))
        commands = [row for row in self.fake.commands if 'get-cost-and-usage' in row]
        self.assertEqual(commands[0][commands[0].index('--filter') + 1], USAGE_ONLY)
        self.assertEqual(commands[0][commands[0].index('--group-by') + 1], 'Type=DIMENSION,Key=USAGE_TYPE')
        self.fake.billing_pages = [account_reply(commands[0], '-0.0000036')]
        worker = cloud_gate.GateWorker(AwsCli('build-scaler', 'fixture-region', 1, execute=self.fake),
                                      self.book, self.settings, self.folder / 'reservation.cgroup',
                                      clock=lambda: self.now + 86400)
        _ = worker.charges()
        self.assertLess(min(row.usd for row in self.book.read_snapshot().reconciliations), Decimal(0))
        self.fake.billing_pages = [{'ResultsByTime': []}]
        worker = cloud_gate.GateWorker(AwsCli('build-scaler', 'fixture-region', 1, execute=self.fake),
                                      self.book, self.settings, self.folder / 'reservation.cgroup',
                                      clock=lambda: self.now + 172800)
        with self.assertRaisesRegex(ValueError, 'daily reply is unavailable'):
            worker.charges()

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
        with redirect_stdout(StringIO()) as output:
            self.assertEqual(self.execute()['status'], 'fallback')
        self.assertIsInstance(self.book.read_snapshot().gate_reservations[0].state, ledger.GateAbsent)
        rows = [ledger.mapping(cast(object, json.loads(line)))
                for line in (self.state / 'cloud-gate-launches.jsonl').read_text().splitlines()]
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(row['result'] == 'refused' for row in rows))
        self.assertEqual(output.getvalue().count('Cloud gate refused:'), 4)

    def test_rejected_parameter_falls_back_and_records_a_refusal(self) -> None:
        reason = ('An error occurred (InvalidParameterValue) when calling the RunInstances operation: '
                  + 'Encoded User data is limited to 25600 bytes; account 123456789012 subnet-fixture i-private')
        self.fake.refusals = [reason]
        with redirect_stdout(StringIO()) as output:
            outcome = self.execute()
        self.assertEqual(outcome['status'], 'fallback')
        self.assertIn('InvalidParameterValue', str(outcome['reason']))
        self.assertIsInstance(self.book.read_snapshot().gate_reservations[0].state, ledger.GateAbsent)
        rows = [ledger.mapping(cast(object, json.loads(line)))
                for line in (self.state / 'cloud-gate-launches.jsonl').read_text().splitlines()]
        self.assertEqual(rows, [{'time': self.now, 'attempt': 'attempt', 'instance_type': 'm7a.8xlarge',
            'vcpus': 32, 'availability_zone': 'a', 'result': 'refused', 'error_code': 'InvalidParameterValue', 'reason': reason}])
        self.assertEqual(output.getvalue(), 'Cloud gate refused: m7a.8xlarge in a: InvalidParameterValue\n')
        for private in ('123456789012', 'subnet-fixture', 'i-private', 'RED'):
            self.assertNotIn(private, output.getvalue() + str(outcome))

    def test_launched_box_records_vcpus_and_download_links(self) -> None:
        self.assertEqual(self.execute()['status'], 'complete')
        rows = [ledger.mapping(cast(object, json.loads(line)))
                for line in (self.state / 'cloud-gate-launches.jsonl').read_text().splitlines()]
        self.assertEqual(rows, [{'time': self.now, 'attempt': 'attempt', 'instance_type': 'm7a.8xlarge',
            'vcpus': 32, 'availability_zone': 'a', 'result': 'launched', 'error_code': '', 'reason': ''}])
        data = ledger.text(self.fake.launches[0], 'UserData')
        self.assertTrue(data.startswith('#!/bin/sh\n'))
        links = ledger.mapping(cast(object, json.loads(data.split("<<'GATE_LINKS'\n", 1)[1].split('\nGATE_LINKS', 1)[0])))
        for key in ('guest', 'request', 'source', 'log', 'result', 'boot_error'):
            parsed = urlsplit(ledger.text(links, key))
            query = parse_qs(parsed.query)
            self.assertEqual(parsed.scheme, 'https')
            self.assertEqual(query['X-Amz-Expires'], ['1200'])
            self.assertEqual(query['X-Amz-Security-Token'], ['fixture-token'])
            self.assertTrue(query['X-Amz-Credential'][0].startswith('fixture-access/'))
        self.assertEqual(urlsplit(ledger.text(links, 'log')).path, '/artifacts/attempt/gate.log')
        self.assertNotIn('fixture-secret', data)
        self.assertNotIn('s3://', data)
        self.assertLess(len(base64.b64encode(data.encode())), 25600)
        self.assertNotIn('test_module_0000.py', data)
        self.assertIn('requests/attempt.guest.py', data)
        self.assertIn('requests/attempt.json', data)
        self.assertEqual((self.folder / 'guest.py').read_text(), gate_box.GUEST)

    def test_aws_outage_before_launch_allows_fallback(self) -> None:
        self.fake.upload_error = 'EndpointConnectionError'
        self.assertEqual(self.execute()['status'], 'fallback')
        self.assertEqual(self.fake.launches, [])

    def test_missing_image_allows_fallback(self) -> None:
        self.fake.refusals = ['InvalidAMIID.NotFound']
        self.assertEqual(self.execute()['status'], 'fallback')

    def test_access_denial_allows_local_fallback(self) -> None:
        self.fake.upload_error = 'AccessDenied'
        self.assertEqual(self.execute()['status'], 'fallback')
        self.assertIsInstance(self.book.read_snapshot().gate_reservations[0].state, ledger.GateAbsent)

    def test_upload_timeout_allows_local_fallback(self) -> None:
        self.fake.upload_timeout = True
        outcome = self.execute()
        self.assertEqual(outcome['status'], 'fallback')
        self.assertIn('timed out after 30 seconds', str(outcome['reason']))
        self.assertEqual(self.fake.launches, [])
        self.assertIsInstance(self.book.read_snapshot().gate_reservations[0].state, ledger.GateAbsent)

    def test_lost_launch_reply_reuses_exact_arguments_and_token(self) -> None:
        self.fake.refusals = ['EndpointConnectionError']
        self.assertEqual(self.execute()['status'], 'complete')
        self.assertEqual(len(self.fake.launches), 2)
        self.assertEqual(self.fake.launches[0], self.fake.launches[1])

    def test_uncertain_launch_releases_reservation_before_local_fallback(self) -> None:
        self.fake.launch_timeouts = 1
        self.assertEqual(self.execute()['status'], 'fallback')
        self.assertIsInstance(self.book.read_snapshot().gate_reservations[0].state, ledger.GateAbsent)
        self.assertEqual(self.execute()['status'], 'complete')
        self.assertEqual(self.fake.launches[0], self.fake.launches[-1])
        self.assertEqual(len(self.book.read_snapshot().gate_reservations), 1)

    def test_invalid_result_identity_verdict_or_modules_is_rejected(self) -> None:
        for field, value in (('attempt', 'other'), ('tree', 'other'), ('commit', 'other'),
                             ('host', ''), ('verdict', 'fail')):
            with self.subTest(field=field):
                row = {**self.result(), field: value}
                with self.assertRaises(ValueError):
                    cloud_gate.validate_result(row, self.request)
        run = gate.GateRun((gate.GateJobResult('fixture', 'test_other.py', 0, 1, ''),), 1, 1)
        with self.assertRaises(ValueError):
            cloud_gate.validate_result({**self.result(), 'gate': gate_box.encode_run(run)}, self.request)

    def test_missing_final_log_allows_local_fallback(self) -> None:
        self.fake.has_log = False
        self.assertEqual(self.execute()['status'], 'fallback')
        self.assertTrue(self.fake.terminated)

    def test_dead_instance_without_result_allows_local_fallback(self) -> None:
        self.fake.has_result = False
        self.fake.state = 'terminated'
        self.assertEqual(self.execute(), {'status': 'fallback', 'reason': 'The instance stopped without a final result'})
        self.assertTrue(self.fake.terminated)

    def test_stopped_instance_fallback_includes_aws_state_reason(self) -> None:
        self.fake.has_result = False
        self.fake.state = 'shutting-down'
        code = 'Server.SpotInstanceTermination'
        message = 'Server.SpotInstanceTermination: Spot instance termination'
        self.fake.state_reason = {'Code': code, 'Message': message}
        self.assertEqual(self.execute(), {
            'status': 'fallback', 'reason': f'The instance stopped without a final result ({code}: {message})'})
        descriptions = [command for command in self.fake.commands if 'describe-instances' in command]
        self.assertEqual(len(descriptions), 3)
        self.assertTrue(self.fake.terminated)

    def test_expired_instance_without_result_allows_local_fallback(self) -> None:
        self.fake.has_result = False
        self.request['expires'] = int(self.now) + 1
        self.assertEqual(self.execute()['status'], 'fallback')
        self.assertTrue(self.fake.terminated)

    def test_unconfirmed_termination_keeps_charging(self) -> None:
        self.fake.confirm_termination = False
        self.assertEqual(self.execute()['status'], 'fallback')
        self.assertIsInstance(self.book.read_snapshot().gate_reservations[0].state, ledger.GateRunning)
        before = self.book.estimate(read_settings(self.settings), int(self.now)).monthly.usd
        self.advance(60)
        self.assertGreater(self.book.estimate(read_settings(self.settings), int(self.now)).monthly.usd, before)

    def worker_with_pending_gate(self) -> cloud_gate.GateWorker:
        reservation = ledger.GateReservation('orphan', int(self.now), int(self.now) + 1200, Decimal('.8'), Decimal(0))
        self.assertIsInstance(self.book.reserve_gate(self.settings, reservation), ledger.GateReserved)
        return cloud_gate.GateWorker(AwsCli('build-scaler', 'fixture-region', 1, execute=self.fake),
            self.book, self.settings, self.folder / 'reservation.cgroup', clock=lambda: self.now, pause=self.advance)

    def test_expired_pending_gate_without_instance_stops_charging(self) -> None:
        worker = self.worker_with_pending_gate()
        self.fake.has_instance = False
        self.advance(8 * 3600)
        self.assertGreater(self.book.estimate(read_settings(self.settings), int(self.now)).monthly.usd, Decimal(0))
        worker.reconcile()
        self.assertEqual(self.book.read_snapshot().gate_reservations[0].state, ledger.GateAbsent(int(self.now)))
        self.assertEqual(self.book.estimate(read_settings(self.settings), int(self.now)).monthly.usd, Decimal(0))
        self.advance(3600)
        self.assertEqual(self.book.estimate(read_settings(self.settings), int(self.now)).monthly.usd, Decimal(0))
        query, = self.fake.commands
        self.assertEqual(query[query.index('--filters') + 1], 'Name=tag:GateAttempt,Values=orphan')

    def test_expired_pending_gate_with_terminated_instance_stops_charging(self) -> None:
        worker = self.worker_with_pending_gate()
        self.fake.state = 'terminated'
        self.advance(1200)
        worker.reconcile()
        self.assertEqual(self.book.read_snapshot().gate_reservations[0].state,
                         ledger.GateTerminated(int(self.now), 'i-fixture'))
        charged = self.book.estimate(read_settings(self.settings), int(self.now)).monthly.usd
        self.advance(3600)
        self.assertEqual(self.book.estimate(read_settings(self.settings), int(self.now)).monthly.usd, charged)

    def test_expired_pending_gate_with_live_instance_is_observed_next_time(self) -> None:
        worker = self.worker_with_pending_gate()
        self.advance(1200)
        worker.reconcile()
        self.assertEqual(self.book.read_snapshot().gate_reservations[0].state, ledger.GateRunning('i-fixture'))
        self.fake.state = 'terminated'
        worker.reconcile()
        self.assertEqual(self.book.read_snapshot().gate_reservations[0].state,
                         ledger.GateTerminated(int(self.now), 'i-fixture'))
        self.assertIn('--instance-ids', self.fake.commands[-1])

    def test_pending_gate_before_expiry_is_unchanged(self) -> None:
        worker = self.worker_with_pending_gate()
        self.advance(1199)
        worker.reconcile()
        self.assertIsInstance(self.book.read_snapshot().gate_reservations[0].state, ledger.GateLaunchPending)
        self.assertEqual(self.fake.commands, [])

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
        buildlog = self.root / 'buildlog'
        destination = buildlog / 'fixture-guest.jsonl'
        destination.parent.mkdir()
        traceback = 'Traceback (most recent call last):\nAssertionError: retained cloud failure\n'
        for retried in (False, True):
            with self.subTest(retried=retried):
                passed = gate.GateJobResult('fixture', 'test_ok.py', 0, 1, 'OK')
                failed = gate.GateJobResult('fixture', 'test_ok.py', 1, 2, traceback)
                run = gate.GateRun((passed,), 3, 2, executions=(failed, passed) if retried else (passed,))
                result = self.result()
                result['gate'] = gate_box.encode_run(run)
                result['record'] = gate_box.completion_record(run, 'attempt', 'commit', 'tree', 'fixture-guest',
                                                             self.now, self.now + 3, 'merge-gate')
                _ = destination.write_text('{"interrupted":')
                with (mock.patch.dict(os.environ, {'BUILDLOG_DIR': str(buildlog)}),
                      mock.patch.object(store, 'host_file', return_value=destination) as host_file):
                    cloud_gate.import_record(self.folder, result)
                    cloud_gate.import_record(self.folder, result)
                self.assertEqual(host_file.call_args.args[0], 'fixture-guest')
                records = [ledger.mapping(cast(object, json.loads(line))) for line in destination.read_text().splitlines()]
                self.assertEqual([row['id'] for row in records], ['gate-attempt'])
                self.assertEqual(records[0]['host'], 'fixture-guest')
                modules = ledger.rows(records[0], 'modules')
                if retried:
                    first, second = modules
                    self.assertNotEqual(first['log'], '')
                    kept = gzip.decompress((buildlog / ledger.text(first, 'log')).read_bytes()).decode('utf-8')
                    self.assertIn(traceback, kept)
                    self.assertEqual((first['attempt'], first['status']), (1, 1))
                    self.assertEqual((second['attempt'], second['status'], second['log']), (2, 0, ''))
                else:
                    self.assertEqual(modules[0]['log'], '')

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
    admission: AdmissionFixture = cast(AdmissionFixture, cast(object, None))

    @override
    def setUp(self) -> None:
        super().setUp()
        repository = Path(__file__).resolve().parents[2]
        for name in ('merge_gate', 'cloud_gate'):
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
        self.admission = caller_admission(self, self.root, {'BUILDLOG_DIR': str(self.root / 'buildlog')})

    def test_main_commit_needs_no_bundle(self) -> None:
        folder = self.root / 'attempt'
        request = cloud_gate.prepare(self.repo, folder, gate.SuiteMode.MERGE_GATE, False, 1800)
        self.assertEqual(request['tree'], gate.checked_git(self.repo, 'rev-parse', 'HEAD^{tree}'))
        self.assertEqual(request['commit'], gate.checked_git(self.repo, 'rev-parse', 'HEAD'))
        self.assertEqual(request['head'], request['commit'])
        self.assertEqual(request['origin_main'], request['commit'])
        self.assertEqual(request['clone_url'], 'https://github.com/natepiano/claude_commands.git')
        self.assertEqual(request['source_key'], '')
        self.assertFalse((folder / 'source.bundle').exists())
        self.assertEqual(request['modules'], ['scripts/fixture/test_one.py', 'scripts/fixture/test_two.py'])

    def test_bundle_contains_only_the_commit_ahead_of_main(self) -> None:
        base = gate.checked_git(self.repo, 'rev-parse', 'refs/remotes/origin/main')
        _ = (self.repo / 'new-file').write_text('one new commit\n')
        _ = gate.checked_git(self.repo, 'add', 'new-file')
        _ = gate.checked_git(self.repo, 'commit', '-qm', 'New commit')
        folder = self.root / 'attempt'
        request = cloud_gate.prepare(self.repo, folder, gate.SuiteMode.MERGE_GATE, False, 1800)
        bundle = folder / 'source.bundle'
        header = bundle.read_bytes().split(b'\n\n', 1)[0].decode()
        self.assertIn('-' + base + ' ', header)
        self.assertEqual(request['origin_main'], base)
        refs = gate.checked_git(self.repo, 'bundle', 'list-heads', str(bundle))
        self.assertEqual(refs.splitlines(), [str(request['commit']) + ' refs/cloud-gates/attempt'])
        pack = bundle.read_bytes().split(b'\n\n', 1)[1]
        indexed = subprocess.run(['git', '-C', str(self.repo), 'index-pack', '--stdin', '--fix-thin'],
            input=pack, check=True, capture_output=True, timeout=10)
        pack_hash = indexed.stdout.decode().split()[-1]
        objects = gate.checked_git(self.repo, 'verify-pack', '-v', str(self.repo / '.git/objects/pack' / ('pack-' + pack_hash + '.idx')))
        commits = [line.split()[0] for line in objects.splitlines() if len(line.split()) > 1 and line.split()[1] == 'commit']
        self.assertEqual(commits, [request['commit']])

    def test_snapshot_preserves_checkout_and_fetches_into_clone(self) -> None:
        _ = (self.repo / '.gitignore').write_text('ignored\n')
        _ = gate.checked_git(self.repo, 'add', '.gitignore')
        _ = gate.checked_git(self.repo, 'commit', '-qm', 'Ignore fixture output')
        _ = gate.checked_git(self.repo, 'update-ref', 'refs/remotes/origin/main', 'HEAD')
        clone = self.root / 'clone'
        _ = gate.checked_git(self.repo, 'clone', '-q', str(self.repo), str(clone))
        _ = self.modules[0][1].write_text('tracked edit\n')
        _ = self.modules[1][1].write_text('staged edit\n')
        _ = gate.checked_git(self.repo, 'add', str(self.modules[1][1]))
        _ = (self.repo / 'untracked').write_text('untracked file\n')
        _ = (self.repo / 'ignored').write_text('ignored file\n')
        status = gate.checked_git(self.repo, 'status', '--porcelain')
        index = (self.repo / '.git/index').read_bytes()
        head = gate.checked_git(self.repo, 'rev-parse', 'HEAD')
        refs = gate.checked_git(self.repo, 'for-each-ref')
        folder = self.root / 'attempt'
        request = cloud_gate.prepare(self.repo, folder, gate.SuiteMode.MERGE_GATE, False, 1800)
        commit = ledger.text(request, 'commit')
        self.assertNotEqual(commit, head)
        self.assertEqual(request['head'], head)
        self.assertEqual(request['tree'], gate.checked_git(self.repo, 'rev-parse', commit + '^{tree}'))
        self.assertEqual(gate.checked_git(self.repo, 'rev-parse', commit + '^'), head)
        for name, content in (('scripts/fixture/test_one.py', 'tracked edit'),
                              ('scripts/fixture/test_two.py', 'staged edit'), ('untracked', 'untracked file')):
            self.assertEqual(gate.checked_git(self.repo, 'show', commit + ':' + name), content)
        self.assertNotIn('ignored', gate.checked_git(self.repo, 'ls-tree', '--name-only', commit).splitlines())
        self.assertEqual(gate.checked_git(self.repo, 'status', '--porcelain'), status)
        self.assertEqual((self.repo / '.git/index').read_bytes(), index)
        self.assertEqual(gate.checked_git(self.repo, 'rev-parse', 'HEAD'), head)
        self.assertEqual(gate.checked_git(self.repo, 'for-each-ref'), refs)
        _ = gate.checked_git(clone, 'fetch', str(folder / 'source.bundle'), commit)
        _ = gate.checked_git(clone, 'checkout', '--detach', commit)
        self.assertEqual(gate.checked_git(clone, 'rev-parse', 'HEAD^{tree}'), request['tree'])
        self.assertEqual((clone / 'untracked').read_text(), 'untracked file\n')

    def test_targeted_selection_runs_locally_and_sends_only_selected_paths(self) -> None:
        selection = affected_modules.AffectedSelection(affected_modules.CertifiedBase('base', '2026-01-15T00:00:00+00:00'),
                                            ('scripts/fixture/test_one.py',), (self.modules[0][1],))
        for dirty in (False, True):
            with self.subTest(dirty=dirty):
                if dirty:
                    _ = self.modules[0][1].write_text('import os\n')
                expected_tree = gate.clean_test_tree(self.repo)
                with (mock.patch.object(gate.PassedTreeLedger, 'certified_base', autospec=True,
                                        return_value=selection.base) as certified,
                      mock.patch.object(affected_modules, 'select_affected', return_value=selection) as select,
                      redirect_stdout(StringIO())):
                    request = cloud_gate.prepare(self.repo, self.root / 'attempt', gate.SuiteMode.TARGETED_TEST, False, 1800)
                self.assertEqual(select.call_args.args[0], self.repo)
                self.assertEqual(select.call_args.args[3], affected_modules.dirty_paths(self.repo))
                self.assertEqual(cast(gate.PassedTreeLedger, certified.call_args.args[0]).tree, expected_tree)
                self.assertEqual(request['modules'], ['scripts/fixture/test_one.py'])

    def test_pending_attempt_reuses_only_the_same_working_tree(self) -> None:
        cloud_gate.save(self.state / 'gate-deployment.json', {'image': 'fixture-image', 'template': 'fixture-template'})
        folders: list[Path] = []
        def service(command: Sequence[str]) -> int:
            folder = Path(command[command.index('--worker') + 1])
            folders.append(folder)
            cloud_gate.save(folder / 'outcome.json', {'status': 'pending', 'reason': 'fixture pending'})
            return 0
        for content in ('first edit\n', 'first edit\n', 'second edit\n'):
            _ = self.modules[0][1].write_text(content)
            _ = cloud_gate.run(self.repo, mode=gate.SuiteMode.MERGE_GATE, full=False,
                               module_time_limit_s=1800, service=service)
        self.assertEqual(folders[0], folders[1])
        self.assertNotEqual(folders[1], folders[2])

    def test_session_runs_only_scaler_service_and_imports_explicit_record(self) -> None:
        for dirty, later_edit, changed_head in ((False, False, False), (True, False, False),
                                               (True, True, False), (True, False, True)):
            with self.subTest(dirty=dirty, later_edit=later_edit, changed_head=changed_head):
                if dirty:
                    _ = self.modules[0][1].write_text('snapshot edit\n')
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
                    record = ledger.mapping(row['record'])
                    record.update(head=request['head'], dirty=bool(request['dirty_paths']), dirty_paths=request['dirty_paths'])
                    if later_edit:
                        _ = self.modules[0][1].write_text('later edit\n')
                    if changed_head:
                        _ = gate.checked_git(self.repo, 'add', '.')
                        _ = gate.checked_git(self.repo, 'commit', '-qm', 'Later HEAD')
                    cloud_gate.save(folder / 'outcome.json', {'status': 'complete', 'result': row})
                    _ = (folder / 'gate.log').write_text('cloud log\n')
                    return 0

                destination = self.root / f'record-{dirty}-{later_edit}-{changed_head}.jsonl'
                with (mock.patch.object(cloud_gate, 'AwsCli', side_effect=AssertionError('The session must not call AWS')),
                      mock.patch.object(store, 'host_file', return_value=destination),
                      mock.patch.dict(os.environ, {'MERGE_GATE_PASSED_DIR': str(self.root / 'passed')}),
                      redirect_stdout(StringIO()) as output):
                    outcome = cloud_gate.run(self.repo, mode=gate.SuiteMode.MERGE_GATE, full=False,
                                             module_time_limit_s=1800, service=service)
                if changed_head:
                    self.assertEqual(outcome, cloud_gate.LocalFallback('The checkout changed during the cloud attempt'))
                    self.assertFalse(destination.exists())
                    continue
                self.assertIsInstance(outcome, cloud_gate.CloudCompleted)
                self.assertIn('cloud log', output.getvalue())
                self.assertEqual(commands[0][0], 'systemd-run')
                sidecar = Path(commands[0][commands[0].index('--sidecar') + 1])
                folder = Path(commands[0][commands[0].index('--worker') + 1])
                self.assertEqual(sidecar, folder / 'reservation.cgroup')
                self.assertIn('--setenv=AWS_SHARED_CREDENTIALS_FILE=' + str(self.root / '.config/aws-build/credentials'), commands[0])
                self.assertIn('--property=KillMode=control-group', commands[0])
                self.assertIn('--working-directory=' + str(self.root / '.claude'), commands[0])
                self.assertEqual(ledger.mapping(cast(object, json.loads(destination.read_text())))['host'], 'remote-host')

                record = cloud_gate.read(destination)
                request = cloud_gate.read(folder / 'request.json')
                self.assertEqual(record['sha'], request['commit'])
                self.assertEqual(record['head'], request['head'])
                self.assertEqual(record['dirty'], dirty)
                self.assertFalse((self.root / 'passed/passed-trees.jsonl').exists())

    def test_absent_stack_needs_no_service_or_aws_call(self) -> None:
        service = mock.Mock(side_effect=AssertionError('An absent stack needs no service'))
        outcome = cloud_gate.run(self.repo, mode=gate.SuiteMode.MERGE_GATE, full=False,
                                 module_time_limit_s=1800, service=service)
        self.assertIsInstance(outcome, cloud_gate.LocalFallback)
        service.assert_not_called()

    def test_cloud_admission_uses_route_identity_and_no_local_memory(self) -> None:
        cloud_gate.save(self.state / 'gate-deployment.json', {'image': 'fixture-image', 'template': 'fixture-template'})
        self.admission.free(0)
        requests: list[wire.AdmitRequest] = []
        admit = admission_client.admit
        def record(request: wire.AdmitRequest, on_wait: Callable[[wire.Wait], None],
                   refresh: Callable[[], wire.AdmitRequest]) -> wire.Admitted | wire.Refused | admission_client.LeftQueue:
            requests.append(request)
            return admit(request, on_wait, refresh)
        def service(command: Sequence[str]) -> int:
            record, = self.admission.caller_reservations().values()
            self.assertEqual(record.need, 0)
            sidecar = Path(command[command.index('--sidecar') + 1])
            self.assertEqual(str(sidecar), requests[-1].sidecar)
            initial = cloud_progress.read(sidecar)
            self.assertIsInstance(initial, cloud_progress.CloudProgress)
            assert isinstance(initial, cloud_progress.CloudProgress)
            self.assertEqual(initial.state, 'requesting')
            folder = Path(command[command.index('--worker') + 1])
            cloud_gate.save(folder / 'outcome.json', {'status': 'fallback', 'reason': 'capacity'})
            return 0
        with mock.patch.object(admission_client, 'admit', side_effect=record):
            for mode in (gate.SuiteMode.MERGE_GATE, gate.SuiteMode.TARGETED_TEST):
                outcome = cloud_gate.run(self.repo, mode=mode, full=True, module_time_limit_s=1800, service=service)
                self.assertIsInstance(outcome, cloud_gate.LocalFallback)
                self.assertTrue(self.admission.callers_released())
                request = requests[-1]
                self.assertEqual(request.place, wire.CloudPlace('aws'))
                self.assertEqual(request.identity.step, mode.value)
                self.assertEqual(request.owner.pid, os.getpid())
                self.assertEqual(request.run, wire.RunIdentity(request.owner.pid, request.owner.start_time))

    def test_cloud_fallback_releases_before_local_memory_admission(self) -> None:
        cloud_gate.save(self.state / 'gate-deployment.json', {'image': 'fixture-image', 'template': 'fixture-template'})
        def service(command: Sequence[str]) -> int:
            record, = self.admission.caller_reservations().values()
            self.assertEqual(record.need, 0)
            folder = Path(command[command.index('--worker') + 1])
            cloud_gate.save(folder / 'outcome.json', {'status': 'fallback', 'reason': 'capacity'})
            return 0
        run = cloud_gate.run
        def route(repo: Path, **_kwargs: object) -> cloud_gate.LocalFallback | cloud_gate.CloudCompleted:
            return run(repo, mode=gate.SuiteMode.TARGETED_TEST, full=True, module_time_limit_s=1800, service=service)
        def local(repo: Path, *_args: object, **_kwargs: object) -> gate.GateRun:
            self.assertTrue(self.admission.callers_released())
            with gate.gate_reservation(repo, self.root, gate.BuildHoldWait(), mode=gate.SuiteMode.TARGETED_TEST) as place:
                record, = self.admission.caller_reservations().values()
                self.assertGreater(record.need, 0)
                place.status = 0
            return gate.GateRun((), 0, 0)
        with (mock.patch.object(cloud_gate, 'run', side_effect=route),
              mock.patch.object(gate, 'run_suites', side_effect=local), redirect_stdout(StringIO())):
            _ = gate.run_gate_suites(self.repo, full=True, mode=gate.SuiteMode.TARGETED_TEST)
        self.assertTrue(self.admission.callers_released())

    def test_cloud_service_error_releases_its_slot(self) -> None:
        cloud_gate.save(self.state / 'gate-deployment.json', {'image': 'fixture-image', 'template': 'fixture-template'})
        for status_error in (False, True):
            with (self.subTest(status_error=status_error),
                  mock.patch.object(affected_modules, 'dirty_paths', return_value=set(),
                                    side_effect=affected_modules.GraphError('fixture status error') if status_error else None)):
                service = mock.Mock(side_effect=OSError('fixture service error'))
                outcome = cloud_gate.run(self.repo, mode=gate.SuiteMode.MERGE_GATE, full=True,
                                         module_time_limit_s=1800, service=service)
                self.assertIsInstance(outcome, cloud_gate.LocalFallback)
                self.assertTrue(self.admission.callers_released())
                if status_error:
                    self.assertEqual(outcome, cloud_gate.LocalFallback('fixture status error'))
                    service.assert_not_called()

    def test_route_uses_installed_limit_and_handles_missing_results(self) -> None:
        cloud_gate.save(self.state / 'gate-deployment.json', {'image': 'fixture-image', 'template': 'fixture-template'})
        outcome = cloud_gate.run(self.repo, mode=gate.SuiteMode.MERGE_GATE, full=True, module_time_limit_s=1800,
                                 service=lambda _command: 0)
        self.assertIsInstance(outcome, cloud_gate.LocalFallback)
        self.assertTrue(self.admission.callers_released())

        installed = self.root / '.claude/config/aws_target.conf'
        installed.parent.mkdir(parents=True)
        _ = installed.write_bytes(self.settings.read_bytes())
        numeric_settings(installed, {'daily_limit_usd': '1'})
        checkout = self.repo / 'config'
        checkout.mkdir()
        for source in self.root.glob('*.conf'):
            _ = (checkout / source.name).write_bytes(source.read_bytes())
        for missing_minimum in (True, False):
            with self.subTest(missing_minimum=missing_minimum):
                contents = self.settings.read_text()
                if missing_minimum:
                    contents = '\n'.join(line for line in contents.splitlines()
                                         if not line.startswith('box_minimum_seconds=')) + '\n'
                _ = (checkout / 'aws_target.conf').write_text(contents)
                fake = FakeGateAws()
                now = int(time.time())
                book = ledger.CostLedger(self.state)
                book.record_inventory(ledger.ResourceInventory(now, (ledger.PricedResource('fixture-fixed', Decimal(0)),), 'fixture'))
                book.observe(ledger.InstanceObservation(now, 'stopped', 'fixture'))
                def service(command: Sequence[str]) -> int:
                    folder = Path(command[command.index('--worker') + 1])
                    request = cloud_gate.read(folder / 'request.json')
                    run = gate.GateRun(tuple(gate.GateJobResult('fixture', path.name, 0, 1, '')
                                             for _, path in self.modules), 1, 1)
                    fake.result = {key: request[key] for key in ('attempt', 'commit', 'tree')}
                    fake.result.update(host='fixture-guest', verdict='pass', gate=gate_box.encode_run(run),
                        record=gate_box.completion_record(run, ledger.text(request, 'attempt'),
                            ledger.text(request, 'commit'), ledger.text(request, 'tree'), 'fixture-guest', now, now + 1, 'merge-gate'))
                    api = AwsCli('build-scaler', 'fixture-region', 1, execute=fake)
                    arguments = list(command[command.index('--worker'):])
                    with (mock.patch.object(cloud_gate, 'AwsCli', return_value=api),
                          mock.patch.object(sys, 'argv', ['cloud-gate', *arguments])):
                        return cloud_gate.main()
                deployment: dict[str, object] = {'image': 'fixture-image', 'template': 'fixture-template',
                    'deployment_id': 'fixture', 'bucket': 'fixture-bucket', 'version': '1',
                    'security_group': 'fixture-group', 'revision': 'fixture-revision',
                    'subnets': [{'id': 'fixture-a', 'zone': 'a'}]}
                cloud_gate.save(self.state / 'gate-deployment.json', deployment)
                with mock.patch.dict(os.environ, {'CONF_DIR': str(checkout), 'AWS_TARGET_CONFIG': str(installed)}):
                    outcome = cloud_gate.run(self.repo, mode=gate.SuiteMode.MERGE_GATE, full=True,
                                             module_time_limit_s=1800, service=service)
                self.assertEqual(outcome, cloud_gate.LocalFallback('daily_limit'))
                self.assertEqual(fake.launches, [])
                self.assertTrue(self.admission.callers_released())
