"""Test the shared guest and the disposable resource definitions."""
from __future__ import annotations

from collections.abc import Callable
import base64
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
from types import SimpleNamespace
from typing import Self, cast
import unittest
from unittest import mock

from scripts.aws_target import gate_box, gate_trial, ledger
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.production import merge_gate as gate


ROOT = Path(__file__).resolve().parents[2]


class GateBoxTests(unittest.TestCase):
    def test_trial_and_route_share_one_guest(self) -> None:
        self.assertIs(gate_trial.GUEST, gate_box.GUEST)
        _ = compile(gate_box.GUEST, '<gate guest>', 'exec')
        self.assertNotIn('basedpyright', gate_box.GUEST)
        self.assertIn('merge_gate.SelectedModules', gate_box.GUEST)
        self.assertIn("'HEAD^{tree}'", gate_box.GUEST)
        self.assertIn("'+refs/remotes/origin/*:refs/remotes/origin/*'", gate_box.GUEST)

    def test_production_boot_has_absolute_expiry_and_no_type_package(self) -> None:
        links = {'production': 'true', 'expires': '1800000000', 'step': 'size',
                 'guest': 's3://fixture/requests/attempt.guest.py', 'request': 's3://fixture/requests/attempt.json',
                 'result': 's3://fixture/artifacts/role:__INSTANCE_ID__/attempt/result.json',
                 'log': 's3://fixture/artifacts/role:__INSTANCE_ID__/attempt/gate.log'}
        script = gate_box.user_data(links, 'fixture-revision', 20, 5, 1800)
        self.assertIn('--on-calendar=@1800000000', script)
        self.assertIn('shutdown -h now', script)
        self.assertIn('while sleep 5;', script)
        self.assertIn('aws s3 cp', script)
        self.assertIn('instance-id', script)
        self.assertNotIn('#basedpyright', script)
        self.assertIn('#chromium', script)
        self.assertLessEqual(len(script.encode()), 16384)
        result = subprocess.run(['sh', '-n'], input=script, text=True, capture_output=True,
                                timeout=TEST_HANG_GUARD_SECS, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('base64 -d', script)
        self.assertNotIn(gate_box.GUEST, script)
        self.assertIn('aws s3 cp s3://fixture/requests/attempt.guest.py /var/lib/gate-trial/guest.py', script)
        self.assertIn('aws s3 cp s3://fixture/requests/attempt.json /var/lib/gate-trial/request.json', script)
        session = shlex.split(script[script.index('nix --extra-experimental-features'):])[-1]
        self.assertLess(session.index('aws s3 cp s3://fixture/requests'), session.index('exec python3'))

    def test_full_gate_downloads_program_and_request_without_inlining_modules(self) -> None:
        modules = [f'scripts/fixture/test_module_{index:04d}.py' for index in range(740)]
        links = {'production': 'true', 'expires': '1800000000', 'step': 'size',
                 'attempt': 'a' * 32, 'commit': 'b' * 40, 'tree': 'c' * 40, 'mode': 'merge-gate',
                 'origin': 'https://example.invalid/team/project.git', 'source': 's3://fixture/source/' + 'd' * 64 + '.bundle',
                 'guest': 's3://fixture/requests/attempt.guest.py', 'request': 's3://fixture/requests/attempt.json',
                 'log': 's3://fixture/artifacts/role:__INSTANCE_ID__/attempt/gate.log',
                 'result': 's3://fixture/artifacts/role:__INSTANCE_ID__/attempt/result.json'}
        script = gate_box.user_data(links, 'f' * 40, 20, 5, 1800)
        self.assertLess(len(base64.b64encode(script.encode())), 25600)
        self.assertNotIn('test_module_', script)
        self.assertNotIn('"modules"', script)
        self.assertNotIn(base64.b64encode(gate_box.GUEST.encode()).decode(), script)
        session = shlex.split(script[script.index('nix --extra-experimental-features'):])[-1]
        downloads = session[session.index('aws s3 cp s3://fixture/requests'):session.index('ln -sf')]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _ = (root / 'source.py').write_text(gate_box.GUEST)
            _ = (root / 'manifest.json').write_text(json.dumps({'modules': modules}))
            aws = root / 'aws'
            _ = aws.write_text('#!/bin/sh\ncase "$3" in *.guest.py) cp "$FIXTURE/source.py" "$4" ;;'
                               + ' *.json) cp "$FIXTURE/manifest.json" "$4" ;; *) exit 1 ;; esac\n')
            aws.chmod(0o700)
            result = subprocess.run(['sh', '-ec', downloads.replace('/var/lib/gate-trial', str(root))],
                env={**os.environ, 'PATH': str(root) + os.pathsep + os.environ['PATH'], 'FIXTURE': str(root)},
                text=True, capture_output=True, timeout=TEST_HANG_GUARD_SECS, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root / 'guest.py').read_text(), gate_box.GUEST)
            self.assertEqual(json.loads((root / 'request.json').read_text()), {'modules': modules})

    def test_result_round_trip_preserves_reruns_and_test_failures(self) -> None:
        first = gate.GateJobResult('fixture', 'test_one.py', 1, 2, 'failed', cpu_seconds=1, peak_mib=3)
        second = gate.GateJobResult('fixture', 'test_one.py', 0, 1, 'passed')
        run = gate.GateRun((second,), 4, 3, executions=(first, second))
        self.assertEqual(gate_box.decode_run(gate_box.encode_run(run)), run)
        self.assertEqual([job.output for job in gate_box.execution_records(gate_box.decode_run(gate_box.encode_run(run)))],
                         ['failed', 'passed'])
        record = gate_box.completion_record(run, 'attempt', 'commit', 'tree', 'remote-host', 0, 4, 'merge-gate')
        self.assertEqual((record['kind'], record['host'], record['status']), ('gate_run', 'remote-host', 0))
        self.assertEqual(len(ledger.rows(record, 'modules')), 2)

    def test_result_rejects_missing_or_invalid_status_and_metrics(self) -> None:
        run = gate.GateRun((gate.GateJobResult('fixture', 'test_one.py', 0, 1, ''),), 1, 1)
        for field, value in (('returncode', False), ('seconds', -1), ('seconds', float('nan')),
                             ('kind', 'UNKNOWN'), ('restart', 'other')):
            with self.subTest(field=field, value=value):
                row = gate_box.encode_run(run)
                ledger.rows(row, 'jobs')[0][field] = value
                with self.assertRaises((ValueError, KeyError)):
                    _ = gate_box.decode_run(row)
        with self.assertRaises(KeyError):
            _ = gate_box.decode_run({})


class GateResourcesTests(unittest.TestCase):
    def template(self) -> dict[str, object]:
        return ledger.mapping(cast(object, json.loads((ROOT / 'docs/plans/builds-aws/cloudformation.json').read_text())))

    def resources(self) -> dict[str, object]:
        return ledger.mapping(self.template()['Resources'])

    def test_rust_resources_remain_identical(self) -> None:
        original = subprocess.run(['git', '-C', str(ROOT), 'show', 'HEAD:docs/plans/builds-aws/cloudformation.json'],
                                  check=True, capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
        before = ledger.mapping(cast(object, json.loads(original.stdout)))
        current = self.template()
        for section in ('Resources', 'Parameters', 'Outputs'):
            for name, row in ledger.mapping(before[section]).items():
                if name.startswith("Gate") or name.startswith("EnableGates"):
                    continue
                self.assertEqual(ledger.mapping(current[section])[name], row, (section, name))

    def test_cloud_resources_use_the_machine_name(self) -> None:
        names = {'GateBucket': 'BucketName', 'GateRole': 'RoleName', 'GateProfile': 'InstanceProfileName',
                 'GateSecurityGroup': 'GroupName', 'GateLaunchTemplate': 'LaunchTemplateName',
                 'GateExpiryRole': 'RoleName', 'GateExpiryFunction': 'FunctionName', 'GateExpirySchedule': 'Name',
                 'GateExpiryLog': 'LogGroupName'}
        for name, field in names.items():
            with self.subTest(resource=name):
                properties = ledger.mapping(ledger.mapping(self.resources()[name])['Properties'])
                suffix = '-expiry' if name in ('GateExpiryRole', 'GateExpiryFunction', 'GateExpirySchedule', 'GateExpiryLog') else ''
                prefix = '/aws/lambda/' if name == 'GateExpiryLog' else ''
                self.assertEqual(properties[field], {'Fn::Sub': prefix + 'cloud-claude-gate-${DeploymentId}' + suffix})
                if name in ('GateProfile', 'GateExpirySchedule'):
                    continue
                tags = (ledger.rows(properties, 'Tags') if name != 'GateLaunchTemplate' else
                        ledger.rows(ledger.rows(properties, 'TagSpecifications')[0], 'Tags'))
                self.assertIn({'Key': 'Workload', 'Value': 'cloud-claude-gate'}, tags)
                self.assertIn({'Key': 'Name', 'Value': 'cloud-claude-gate'}, tags)

    def test_launch_template_terminates_spot_and_deletes_its_root(self) -> None:
        resource = ledger.mapping(self.resources()['GateLaunchTemplate'])
        data = ledger.mapping(ledger.mapping(resource['Properties'])['LaunchTemplateData'])
        market = ledger.mapping(data['InstanceMarketOptions'])
        self.assertEqual(market['MarketType'], 'spot')
        options = ledger.mapping(market['SpotOptions'])
        self.assertEqual(options['SpotInstanceType'], 'one-time')
        self.assertEqual(options['InstanceInterruptionBehavior'], 'terminate')
        self.assertEqual(data['InstanceInitiatedShutdownBehavior'], 'terminate')
        self.assertIs(ledger.mapping(ledger.rows(data, 'BlockDeviceMappings')[0]['Ebs'])['DeleteOnTermination'], True)
        bucket = ledger.mapping(ledger.mapping(self.resources()['GateBucket'])['Properties'])
        rules = ledger.rows(ledger.mapping(bucket['LifecycleConfiguration']), 'Rules')
        self.assertEqual(rules[0]['ExpirationInDays'], {'Ref': 'GateArtifactDays'})

    def test_scaler_grants_have_workload_and_resource_scopes(self) -> None:
        properties = ledger.mapping(ledger.mapping(self.resources()['GateScalerPolicy'])['Properties'])
        statements = ledger.rows(ledger.mapping(properties['PolicyDocument']), 'Statement')
        by_action = {str(row['Action']): row for row in statements}
        self.assertEqual(properties['Users'], [{'Ref': 'ScalerUser'}])
        terminate = by_action[str(['ec2:TerminateInstances'])]
        self.assertEqual(ledger.mapping(ledger.mapping(terminate['Condition'])['StringEquals']), {
            'ec2:ResourceTag/GateDeployment': {'Ref': 'DeploymentId'}, 'ec2:ResourceTag/Workload': 'cloud-claude-gate'})
        role = by_action[str(['iam:PassRole'])]
        self.assertEqual(role['Resource'], {'Fn::GetAtt': ['GateRole', 'Arn']})
        self.assertEqual(role['Condition'], {'StringEquals': {'iam:PassedToService': 'ec2.amazonaws.com'}})
        for row in statements:
            if row['Resource'] == '*':
                self.assertIn(row['Action'], (['ec2:DescribeInstances'], ['ce:GetCostAndUsage']))
        launches = [row for row in statements if row['Action'] == ['ec2:RunInstances']]
        self.assertEqual(len(launches), 5)
        instance = next(row for row in launches if 'instance/*' in json.dumps(row['Resource']))
        condition = ledger.mapping(instance['Condition'])
        self.assertEqual(ledger.mapping(condition['StringEquals'])['ec2:InstanceType'], {'Ref': 'GateInstanceTypes'})
        self.assertEqual(ledger.mapping(condition['StringEquals'])['ec2:InstanceMarketType'], 'spot')
        self.assertIn('ec2:InstanceProfile', ledger.mapping(condition['ArnEquals']))

    def test_guest_can_write_only_its_credential_prefix(self) -> None:
        role = ledger.mapping(ledger.mapping(self.resources()['GateRole'])['Properties'])
        policy = ledger.mapping(ledger.rows(role, 'Policies')[0]['PolicyDocument'])
        writes = [row for row in ledger.rows(policy, 'Statement') if row['Action'] == ['s3:PutObject']]
        self.assertEqual(writes, [{'Effect': 'Allow', 'Action': ['s3:PutObject'],
            'Resource': {'Fn::Sub': '${GateBucket.Arn}/artifacts/${!aws:userid}/*'}}])
        self.assertEqual(ledger.mapping(self.template()['Outputs'])['GateRoleId'],
                         {'Condition': 'Gates', 'Value': {'Fn::GetAtt': ['GateRole', 'RoleId']}})

    def test_expiry_function_finds_all_pages_and_terminates_only_expired_instances(self) -> None:
        properties = ledger.mapping(ledger.mapping(self.resources()['GateExpiryFunction'])['Properties'])
        code = ledger.text(ledger.mapping(properties['Code']), 'ZipFile')
        expired = {'InstanceId': 'expired', 'Tags': [{'Key': 'ExpiresAt', 'Value': '99'}]}
        live = {'InstanceId': 'live', 'Tags': [{'Key': 'ExpiresAt', 'Value': '101'}]}
        invalid = {'InstanceId': 'invalid', 'Tags': [{'Key': 'ExpiresAt', 'Value': 'invalid'}]}
        class ExpiryClient:
            operation: str = ''
            filters: list[dict[str, object]] = []
            terminated: list[list[str]] = []

            def get_paginator(self, operation: str) -> Self:
                self.operation = operation
                return self

            def paginate(self, *, Filters: list[dict[str, object]]) -> list[dict[str, object]]:
                self.filters = Filters
                return [{'Reservations': [{'Instances': [live]}]},
                        {'Reservations': [{'Instances': [expired, invalid]}]}]

            def terminate_instances(self, *, InstanceIds: list[str]) -> None:
                self.terminated = [*self.terminated, InstanceIds]

        client = ExpiryClient()

        def aws_client(service: str) -> ExpiryClient:
            self.assertEqual(service, 'ec2')
            return client
        namespace: dict[str, object] = {}
        with (mock.patch.dict('sys.modules', {'boto3': SimpleNamespace(client=aws_client)}),
              mock.patch.dict(os.environ, {'DEPLOYMENT': 'fixture'}), mock.patch('time.time', return_value=100)):
            exec(compile(code, '<expiry>', 'exec'), namespace)
            cast(Callable[[object, object], None], namespace['handler'])({}, None)
        self.assertEqual(client.terminated, [['expired']])
        self.assertEqual(client.operation, 'describe_instances')
        filters = client.filters
        self.assertIn({'Name': 'tag:GateDeployment', 'Values': ['fixture']}, filters)
        self.assertIn({'Name': 'tag:Workload', 'Values': ['cloud-claude-gate']}, filters)
        schedule = ledger.mapping(ledger.mapping(self.resources()['GateExpirySchedule'])['Properties'])
        self.assertIn('rate(1 minute)', json.dumps(schedule))
