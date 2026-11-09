"""Exercise guarded deployment with a private AWS executable and no network."""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import redirect_stderr, redirect_stdout
import base64
from datetime import UTC, datetime
from decimal import Decimal
from functools import lru_cache, partial
import gzip
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import re
import runpy
import shlex
import shutil
import subprocess
import sys
import time
from types import CodeType, SimpleNamespace
from typing import ClassVar, cast, override
from unittest import mock
from urllib.request import Request


from scripts.aws_target import admin_secrets, aws_target, box, config as aws_config, deploy, gate_box, gate_trial, ledger, measurement, remote
from scripts.aws_target.config import read_control_settings
from scripts.aws_target.ledger_test_support import CostFixture, account_reply
from scripts.aws_target.measurement_test_support import DAY, narrow
from scripts.aws_target.test_box import FAKE as BOX_FAKE, SYSTEM, FakeMonotonicClock, FakeTailnet, write_aws_claim
from scripts.buildlog import sync
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib import conf
from scripts.mac_test import mac_test
from scripts.build_targets import target_config
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


FAKE_AWS = r'''import datetime, json, os, pathlib, sys
root = pathlib.Path(os.environ['DEPLOY_FIXTURE'])
args = sys.argv[1:]
service_index = args.index('--region') + 2
service = args[service_index]
operation = args[service_index + 1]
if service == 'ssm' and os.environ.get('BOX_FIXTURE'):
    import runpy
    sys.argv = [str(root / 'box-aws'), *args]
    runpy.run_path(sys.argv[0], run_name='__main__')
    sys.exit(0)
if (root / ('fail-down-' + operation)).exists():
    print('fixture removal failure: ' + operation, file=sys.stderr)
    sys.exit(1)
state_path = root / 'fake-stack.json'
state = json.loads(state_path.read_text()) if state_path.exists() else {}
clock = int((root / 'clock').read_text())
book = root / 'state/cost-ledger.json'
evidence = json.loads(book.read_text()) if book.exists() else {}
with (root / 'commands.jsonl').open('a') as output:
    output.write(json.dumps({'args': args if '--profile' in args else ['--profile', 'environment', *args], 'decision': evidence.get('decision', {}),
                             'compute_requests': evidence.get('compute_requests', [])}) + '\n')
def reply(value):
    print(json.dumps(value))
override = root / (operation + '.json')
if override.exists():
    reply(json.loads(override.read_text()))
    sys.exit(0)
keys_path = root / 'fake-keys.json'
keys = json.loads(keys_path.read_text()) if keys_path.exists() else ['old-key']
if service == 'sts' and operation == 'get-caller-identity':
    if '--profile' not in args:
        reply({'Account': '123456789012'})
        sys.exit(0)
    import configparser
    credentials = configparser.ConfigParser()
    credentials.read(os.environ['AWS_SHARED_CREDENTIALS_FILE'])
    profile = args[args.index('--profile') + 1]
    valid = credentials.get(profile, 'aws_access_key_id', fallback='') == 'new-key'
    if not valid or (root / 'fail-scaler').exists():
        print('The profile is not ready', file=sys.stderr)
        sys.exit(1)
    reply({'Arn': 'arn:aws:iam::123456789012:user/build-scaler'})
elif service == 'iam' and operation == 'get-user':
    if (root / 'deleted-stack').exists():
        print('NoSuchEntity', file=sys.stderr); sys.exit(1)
    user = args[args.index('--user-name') + 1]
    reply({'User': {'Arn': 'arn:aws:iam::123456789012:user/' + user, 'UserId': 'user-fixture'}})
elif service == 'cloudformation' and operation == 'describe-stack-resource':
    logical = args[args.index('--logical-resource-id') + 1]
    reply({'StackResourceDetail': {'PhysicalResourceId': state['outputs']['InstanceId'] if logical == 'Instance' else 'build-scaler'}})
elif service == 'cloudformation' and operation == 'wait':
    if (root / ('fail-wait-' + args[service_index + 2])).exists():
        print('fixture wait failed', file=sys.stderr); sys.exit(1)
    reply({})
elif service == 'cloudformation' and operation == 'get-template':
    reply({'TemplateBody': json.loads((root / 'live-template.json').read_text())})
elif service == 'cloudformation' and operation == 'describe-stack-events':
    reply({'StackEvents': []})
elif service == 'cloudformation' and operation == 'delete-change-set':
    name = args[args.index('--change-set-name') + 1]
    (root / ('update-change-set.json' if name.startswith('update-') else 'change-set.json')).unlink(missing_ok=True); reply({})
elif service == 'cloudformation' and operation == 'describe-change-set':
    change = root / ('update-change-set.json' if args[args.index('--change-set-name') + 1].startswith('update-') else 'change-set.json')
    if not change.exists():
        print('ChangeSetNotFound: does not exist', file=sys.stderr); sys.exit(1)
    reply(json.loads(change.read_text()))
elif service == 'cloudformation' and operation == 'create-change-set':
    name = args[args.index('--stack-name') + 1]
    stack_id = 'arn:aws:cloudformation:us-east-1:123456789012:stack/' + name + '/fixture-id'
    updating = args[args.index('--change-set-type') + 1] == 'UPDATE'
    if updating:
        stack_id = state['stack_id']
    (root / 'reserved-stack').write_text(stack_id)
    change = root / ('update-change-set.json' if updating else 'change-set.json')
    assert not change.exists() or updating, 'duplicate change-set name'
    if updating:
        names = root / 'update-names.json'
        used = json.loads(names.read_text()) if names.exists() else []
        change_name = args[args.index('--change-set-name') + 1]
        assert change_name not in used, 'duplicate update name'
        names.write_text(json.dumps([*used, change_name]))
    change.write_text(json.dumps({'StackId': stack_id, 'Status': 'CREATE_COMPLETE', 'Changes': [],
                                 'Parameters': json.loads(args[args.index('--parameters') + 1]),
                                 'Template': json.loads(pathlib.Path(args[args.index('--template-body') + 1].removeprefix('file://')).read_text())}))
    if updating and (root / 'update-plan.json').exists():
        value = json.loads(change.read_text())
        value.update(json.loads((root / 'update-plan.json').read_text()))
        change.write_text(json.dumps(value))
    if (root / 'lose-create-reply').exists():
        print('lost CREATE response', file=sys.stderr); sys.exit(1)
    reply({'StackId': stack_id})
elif service == 'iam' and operation == 'create-access-key':
    keys.append('new-key')
    keys_path.write_text(json.dumps(keys))
    reply({'AccessKey': {'AccessKeyId': 'new-key', 'SecretAccessKey': 'fixture-secret-only-in-credentials'}})
elif service == 'iam' and operation == 'list-access-keys':
    if (root / 'deleted-stack').exists():
        print('NoSuchEntity', file=sys.stderr); sys.exit(1)
    reply({'AccessKeyMetadata': [{'AccessKeyId': key} for key in keys]})
elif service == 'iam' and operation == 'delete-access-key':
    keys.remove(args[args.index('--access-key-id') + 1])
    keys_path.write_text(json.dumps(keys))
    reply({})
elif service == 'cloudformation' and operation == 'describe-stacks':
    if not state and ((root / 'review-stack').exists() or (root / 'change-set.json').exists()):
        reply({'Stacks': [{'StackStatus': 'REVIEW_IN_PROGRESS', 'StackId': (root / 'reserved-stack').read_text() if (root / 'reserved-stack').exists() else ''}]})
        sys.exit(0)
    if not state or args[args.index('--stack-name') + 1] not in (state.get('stack_name'), state.get('stack_id')):
        print('ValidationError: Stack does not exist', file=sys.stderr)
        sys.exit(1)
    reply({'Stacks': [{'StackId': state.get('stack_id'), 'StackStatus': state.get('status', 'UPDATE_COMPLETE'),
        'Parameters': [{'ParameterKey': k, 'ParameterValue': v} for k, v in state.get('parameters', {}).items()], 'Outputs': [
        {'OutputKey': key, 'OutputValue': value} for key, value in state['outputs'].items()]}]})
elif service == 'cloudformation' and operation == 'update-stack':
    parameters = json.loads(args[args.index('--parameters') + 1])
    assert all(p.get('UsePreviousValue') is True for p in parameters if p['ParameterKey'] != 'EnableCompute')
    assert next(p for p in parameters if p['ParameterKey'] == 'EnableCompute')['ParameterValue'] == 'false'
    state['outputs'].update(ComputeEnabled='false')
    state['outputs'].pop('InstanceId', None); state['outputs'].pop('DataVolumeId', None)
    state['instance_state'] = 'terminated'
    state_path.write_text(json.dumps(state)); reply({})
elif service == 'cloudformation' and operation == 'delete-stack':
    state_path.unlink(); (root / 'deleted-stack').touch(); reply({})
elif service == 'cloudformation' and operation in ('deploy', 'execute-change-set'):
    if operation == 'execute-change-set':
        change = json.loads((root / 'update-change-set.json').read_text())
        params = {row['ParameterKey']: state.get('parameters', {}).get(row['ParameterKey'], 'previous-bootstrap')
                  if row.get('UsePreviousValue') else row['ParameterValue'] for row in change['Parameters']}
    else:
        start = args.index('--parameter-overrides') + 1
        params = dict(word.split('=', 1) for word in args[start:] if '=' in word)
    if params['EnableCompute'] == 'true' and (root / 'fail-compute').exists():
        print('Compute deploy failed', file=sys.stderr)
        sys.exit(1)
    owner = json.loads((root / 'state/deployment.json').read_text())
    outputs = {'ComputeEnabled': params['EnableCompute'], 'BudgetName': params['BudgetName'] or owner['stack'] + '-monthly-' + params['DeploymentId'],
               'ExistingDataInput': params['ExistingDataVolumeId']}
    outputs.update({key: value for key, value in state.get('outputs', {}).items() if key.startswith('Gate')})
    if operation == 'execute-change-set':
        (root / 'live-template.json').write_text(json.dumps(change['Template']))
    if params['EnableCompute'] == 'true':
        outputs.update(InstanceId='i-fixture', DataVolumeId=params['ExistingDataVolumeId'] or 'vol-created')
    owner = json.loads((root / 'state/deployment.json').read_text())
    assert owner['account_id'] == ('210987654321' if owner['stack'] == 'aws-build' else '123456789012')
    assert owner['stack_id'] == (root / 'reserved-stack').read_text()
    assert owner['deployment_id'] == params['DeploymentId']
    if params['EnableCompute'] == 'true' and state.get('outputs', {}).get('InstanceId'):
        outputs.update({key: state['outputs'][key] for key in ('InstanceId', 'DataVolumeId')})
    state = {'outputs': outputs, 'stack_name': owner['stack'], 'stack_id': owner['stack_id'], 'parameters': params,
             'instance_state': 'terminated' if params['EnableCompute'] == 'false' else 'stopped'}
    (root / 'deleted-stack').unlink(missing_ok=True)
    (root / 'deleted-volume').unlink(missing_ok=True)
    (root / 'deleted-budget').unlink(missing_ok=True)
    state_path.write_text(json.dumps(state))
    (root / 'change-set.json').unlink(missing_ok=True)
    (root / 'update-change-set.json').unlink(missing_ok=True)
    if params['EnableCompute'] == 'true' and not params['ExistingDataVolumeId']:
        (root / 'volume-owner').write_text(params['DeploymentId'])
    if params['EnableCompute'] == 'false' and (root / 'slow-foundation').exists():
        (root / 'clock').write_text(str(clock + 301))
    reply({})
elif operation == 'create-image':
    reply({'ImageId': 'ami-cache'})
elif operation in ('deregister-image', 'delete-snapshot'):
    reply({})
elif operation == 'describe-snapshots':
    reply({'Snapshots': [{'SnapshotId': 'snap-cache'}]})
elif operation == 'describe-images' and '--owners' in args and args[args.index('--owners') + 1] == 'self':
    reply({'Images': [{'ImageId': 'ami-cache', 'State': 'available', 'BlockDeviceMappings': [{'DeviceName': '/dev/xvda', 'Ebs': {'SnapshotId': 'snap-cache'}}]}]})
elif operation == 'describe-images':
    reply({'Images': [{'ImageId': 'ami-fixture', 'OwnerId': '123456789012', 'Architecture': 'x86_64',
                       'State': 'available', 'Name': 'nixos-26.05-x86_64', 'RootDeviceName': '/dev/xvda'}]})
elif operation == 'describe-subnets':
    reply({'Subnets': [{'VpcId': 'vpc-fixture', 'AvailabilityZone': 'us-east-1a'}]})
elif operation == 'create-tags':
    (root / 'volume-owner').write_text(args[args.index('--tags') + 1].split('Value=', 1)[1]); reply({})
elif operation == 'describe-volumes':
    if (root / 'deleted-volume').exists():
        print('InvalidVolume.NotFound', file=sys.stderr); sys.exit(1)
    reply({'Volumes': [{'AvailabilityZone': 'us-east-1a', 'Encrypted': True, 'State': 'available',
                        'VolumeId': args[args.index('--volume-ids') + 1], 'Attachments': [],
                        'Size': 500, 'VolumeType': 'gp3', 'Iops': 6000, 'Throughput': 500,
                        'Tags': [{'Key': 'Project', 'Value': 'fixture-build'},
                                 {'Key': 'DeploymentId', 'Value': (root / 'volume-owner').read_text() if (root / 'volume-owner').exists() else ''},
                                 {'Key': 'aws:cloudformation:stack-id', 'Value': (root / 'reserved-stack').read_text() if (root / 'reserved-stack').exists() else ''}]}]})
elif operation == 'delete-volume':
    (root / 'deleted-volume').touch(); reply({})
elif service == 'budgets' and operation == 'describe-budget':
    if (root / 'deleted-budget').exists():
        print('NotFoundException', file=sys.stderr); sys.exit(1)
    reply({'Budget': {'BudgetName': args[args.index('--budget-name') + 1]}})
elif service == 'budgets' and operation == 'delete-budget':
    (root / 'deleted-budget').touch(); reply({})
elif operation == 'describe-instances':
    if (root / 'missing-instance').exists():
        print('InvalidInstanceID.NotFound: instance does not exist', file=sys.stderr)
        sys.exit(1)
    if os.environ.get('BOX_FIXTURE'):
        live_path = root / 'live.json'
        live = json.loads(live_path.read_text())
        live['observations'] = live.get('observations', 0) + 1
        if live['observations'] >= live.get('stop_after', 3): live['ec2'] = 'stopped'
        live_path.write_text(json.dumps(live))
        state['instance_state'] = live['ec2']
    reply({'Reservations': [{'Instances': [{'InstanceId': 'i-fixture', 'LaunchTime': 'fixture-boot',
                                            'State': {'Name': state.get('instance_state', 'stopped')}}]}]})
elif operation == 'stop-instances':
    assert args[args.index('--instance-ids') + 1] == state['outputs']['InstanceId']
    state['instance_state'] = 'stopped'
    state_path.write_text(json.dumps(state))
    if os.environ.get('BOX_FIXTURE'):
        live_path = root / 'live.json'
        live = json.loads(live_path.read_text())
        live.update(ec2='stopped', forced_stop=True)
        live_path.write_text(json.dumps(live))
    reply({'StoppingInstances': [{'InstanceId': 'i-fixture', 'CurrentState': {'Name': 'stopped'}}]})
elif operation == 'start-instances':
    assert args[args.index('--instance-ids') + 1] == state['outputs']['InstanceId']
    capacity = root / 'capacity-refusals'
    if capacity.exists() and int(capacity.read_text()) > 0:
        capacity.write_text(str(int(capacity.read_text()) - 1))
        print('InsufficientInstanceCapacity: no Spot capacity', file=sys.stderr)
        sys.exit(1)
    state['instance_state'] = 'pending'
    state_path.write_text(json.dumps(state))
    if os.environ.get('BOX_FIXTURE'):
        live_path = root / 'live.json'
        live = json.loads(live_path.read_text())
        live.update(ec2='running', observations=0)
        live_path.write_text(json.dumps(live))
    reply({'StartingInstances': [{'InstanceId': 'i-fixture', 'CurrentState': {'Name': 'pending'}}]})
elif operation == 'describe-spot-price-history':
    reply({'SpotPriceHistory': [{'Timestamp': datetime.datetime.fromtimestamp(clock, datetime.UTC).isoformat(),
                                 'SpotPrice': '0.1'}]})
elif operation == 'get-cost-and-usage':
    period = args[args.index('--time-period') + 1]
    assert all(len(part.split('=')[1]) == 10 for part in period.split(','))
    bounds = dict(part.split('=', 1) for part in period.split(','))
    first, end = (datetime.datetime.fromisoformat(bounds[key]) for key in ('Start', 'End'))
    days = []
    while first < end:
        following = first + datetime.timedelta(days=1)
        days.append({'TimePeriod': {'Start': first.date().isoformat(), 'End': following.date().isoformat()},
                     'Estimated': False, 'Total': {}, 'Groups': [{'Keys': ['USE2-SpotUsage:m7a.4xlarge'],
                         'Metrics': {'UnblendedCost': {'Amount': '0.1', 'Unit': 'USD'},
                                     'UsageQuantity': {'Amount': '1', 'Unit': 'Hrs'}}}]})
        first = following
    reply({'ResultsByTime': days})
else:
    raise AssertionError((service, operation))
'''


@lru_cache(maxsize=None)
def compile_fake(source: str) -> CodeType:
    return compile(source, '<deployment fake>', 'exec')


def run_fake(path: str, *, run_name: str = '__main__') -> dict[str, object]:
    namespace: dict[str, object] = {'__name__': run_name, '__file__': path}
    exec(compile_fake(Path(path).read_text()), namespace)
    return namespace


class FixtureExecutor:
    """Run Python fixture commands without starting another interpreter."""

    def __init__(self, root: Path) -> None:
        self.root: Path = root

    def run(self, arguments: Sequence[str], *, capture_output: bool, text: bool,
            timeout: float, check: bool, env: dict[str, str] | None = None,
            input: str | None = None) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ) if env is None else env
        found = shutil.which(arguments[0], path=environment.get('PATH'))
        path = Path(found) if found else Path(arguments[0])
        if (path.parent != self.root / 'bin' or not path.read_text().startswith(f'#!{sys.executable}\n')
                or input is not None):
            return subprocess.run(arguments, capture_output=capture_output, text=text, timeout=timeout,
                                  check=check, env=env, input=input)
        assert capture_output and text
        stdout, stderr = StringIO(), StringIO()
        status = 0
        with (mock.patch.dict(os.environ, environment, clear=True),
              mock.patch.object(sys, 'argv', [str(path), *arguments[1:]]),
              mock.patch.object(runpy, 'run_path', side_effect=run_fake),
              redirect_stdout(stdout), redirect_stderr(stderr)):
            try:
                _ = run_fake(str(path))
            except SystemExit as exit:
                if exit.code is None:
                    status = 0
                elif isinstance(exit.code, int):
                    status = exit.code
                else:
                    print(exit.code, file=sys.stderr)
                    status = 1
        result = subprocess.CompletedProcess(arguments, status, stdout.getvalue(), stderr.getvalue())
        if check:
            result.check_returncode()
        return result


class GateImageAws:
    """Keep the image lifecycle and temporary instance in local test state."""
    def __init__(self, failure: str = '') -> None:
        self.failure: str = failure
        self.commands: list[list[str]] = []
        self.launches: list[dict[str, object]] = []
        self.terminated: bool = False
        self.image_count: int = 0
        self.image_reads: int = 0
        self.version: int = 1
        self.gate_changes: list[dict[str, object]] = []
        self.image_parameter: str = 'ami-fixture'
        self.manifest: Path = Path()
        self.live_template: dict[str, object] = {'Resources': {
            'GateExpiryFunction': {'live': 'keep'}, 'GateScalerPolicy': {'Properties': {
                'PolicyDocument': {'Statement': [{'Effect': 'Allow', 'Action': ['ec2:RunInstances'],
                    'Resource': {'Fn::Sub': 'arn:${AWS::Partition}:ec2:${AWS::Region}::image/${GateImage}'}}]}}}}}
        self.images: dict[str, dict[str, object]] = {
            'ami-old': self.image('ami-old', 'snap-old'),
            'ami-builder': {'ImageId': 'ami-builder', 'Tags': [{'Key': box.IMAGE_OWNER_TAG, 'Value': 'true'}]}}
        self.snapshots: dict[str, dict[str, object]] = {
            'snap-old': {'SnapshotId': 'snap-old', 'Tags': self.tags()},
            'snap-builder': {'SnapshotId': 'snap-builder', 'Tags': [{'Key': box.IMAGE_OWNER_TAG, 'Value': 'true'}]}}

    @staticmethod
    def tags() -> list[dict[str, str]]:
        return [{'Key': gate_box.GATE_IMAGE_OWNER_TAG, 'Value': 'true'}]

    @classmethod
    def image(cls, name: str, snapshot: str) -> dict[str, object]:
        return {'ImageId': name, 'State': 'available', 'Tags': cls.tags(),
                'BlockDeviceMappings': [{'DeviceName': '/dev/xvda', 'Ebs': {'SnapshotId': snapshot, 'VolumeSize': 32}}]}

    def __call__(self, arguments: Sequence[str], **_options: object) -> subprocess.CompletedProcess[str]:
        args = list(arguments)
        self.commands.append(args)
        operation = args[args.index('--region') + 3]
        reply: dict[str, object] = {}
        error = ''
        if operation == 'get-cost-and-usage':
            reply = account_reply(arguments)
        elif operation == 'export-credentials':
            reply = {'AccessKeyId': 'fixture-access', 'SecretAccessKey': 'fixture-secret'}
        elif operation == 'describe-stacks':
            reply = {'Stacks': [{'StackStatus': 'UPDATE_COMPLETE', 'StackId': 'fixture-stack',
                'Parameters': [{'ParameterKey': 'GateImage', 'ParameterValue': self.image_parameter},
                               {'ParameterKey': 'EnableCompute', 'ParameterValue': 'false'}],
                'Outputs': [{'OutputKey': 'GateLaunchVersion', 'OutputValue': str(self.version)}]}]}
        elif operation == 'get-template':
            reply = {'TemplateBody': self.live_template}
        elif operation == 'create-change-set':
            self.gate_changes.append({'Parameters': json.loads(args[args.index('--parameters') + 1]),
                'Template': json.loads(Path(args[args.index('--template-body') + 1].removeprefix('file://')).read_text())})
        elif operation == 'describe-change-set':
            reply = {'Status': 'CREATE_COMPLETE', 'Changes': [
                {'ResourceChange': {'LogicalResourceId': 'GateLaunchTemplate'}}]}
        elif operation == 'execute-change-set':
            if self.failure == 'activate':
                error = 'fixture activation failure'
            else:
                change = self.gate_changes[-1]
                self.live_template = ledger.mapping(change['Template'])
                for row in ledger.rows(change, 'Parameters'):
                    if row['ParameterKey'] == 'GateImage' and 'ParameterValue' in row:
                        image = ledger.text(row, 'ParameterValue')
                        if image != self.image_parameter:
                            self.version += 1
                            self.image_parameter = image
        elif operation == 'wait':
            if 'stack-update-complete' in args and (self.failure == 'waiter'
                    or self.failure == 'narrow waiter' and len(self.gate_changes) == 2):
                error = 'fixture stack waiter failure'
        elif operation == 'describe-stack-events':
            reply = {'StackEvents': []}
        elif operation == 'delete-change-set':
            pass
        elif operation == 'run-instances':
            self.launches.append(ledger.mapping(cast(object, json.loads(args[args.index('--cli-input-json') + 1]))))
            self.terminated = False
            if self.failure == 'lost launch':
                raise subprocess.TimeoutExpired('fixture launch', 1)
            reply = {'Instances': [{'InstanceId': 'i-image'}]}
        elif operation == 'get-object':
            if self.failure == 'interrupt':
                raise KeyboardInterrupt
            if self.failure == 'timeout':
                error = 'NoSuchKey'
            else:
                _ = Path(args[args.index('--key') + 2]).write_text(json.dumps({'status': 7 if self.failure == 'prepare' else 0}))
        elif operation == 'describe-instances':
            reply = {'Reservations': [{'Instances': [{'InstanceId': 'i-image',
                'LaunchTime': '2026-11-09T00:00:00Z', 'State': {'Name': 'terminated' if self.terminated else 'running'}}]}]}
        elif operation == 'terminate-instances':
            self.terminated = True
        elif operation == 'create-image':
            if self.failure == 'create':
                error = 'fixture image failure'
            else:
                self.image_count += 1
                self.image_reads = 0
                name, snapshot = f'ami-gate-{self.image_count}', f'snap-gate-{self.image_count}'
                self.images[name] = self.image(name, snapshot)
                self.images[name]['State'] = 'pending'
                self.snapshots[snapshot] = {'SnapshotId': snapshot, 'Tags': self.tags()}
                reply = {'ImageId': name}
        elif operation == 'describe-images':
            if '--image-ids' in args:
                name = args[args.index('--image-ids') + 1]
                if name not in self.images:
                    error = 'InvalidAMIID.NotFound'
                else:
                    self.image_reads += 1
                    if self.failure == 'image interrupt':
                        self.failure = ''
                        raise KeyboardInterrupt
                    if self.failure == 'image':
                        self.images[name]['State'] = 'failed'
                    elif self.image_reads > 1 and self.failure != 'image timeout':
                        self.images[name]['State'] = 'available'
                    reply = {'Images': [self.images[name]]}
            else:
                reply = {'Images': [row for row in self.images.values() if row.get('Tags') == self.tags()]}
        elif operation == 'describe-snapshots':
            reply = {'Snapshots': [row for row in self.snapshots.values() if row['Tags'] == self.tags()]}
        elif operation == 'deregister-image':
            del self.images[args[args.index('--image-id') + 1]]
        elif operation == 'delete-snapshot':
            del self.snapshots[args[args.index('--snapshot-id') + 1]]
        else:
            raise AssertionError(operation)
        if self.manifest.exists():
            selected = ledger.text(remote.read_record(self.manifest), 'image')
            if selected not in self.launchable_images():
                raise AssertionError(f'The selected image {selected} has no launch permission')
        return subprocess.CompletedProcess(args, int(bool(error)), json.dumps(reply), error)

    def launchable_images(self) -> set[str]:
        policy = ledger.mapping(ledger.mapping(ledger.mapping(
            ledger.mapping(self.live_template['Resources'])['GateScalerPolicy'])['Properties'])['PolicyDocument'])
        transition = [row for row in ledger.rows(policy, 'Statement') if row.get('Sid') == 'GateImageTransition']
        return {self.image_parameter, *[ledger.text(ledger.mapping(row['Resource']), 'Fn::Sub').split('/')[-1]
                                      for row in transition]}


class DeploymentTests(CostFixture):
    now: int = 0
    bundle: Path = Path()
    credentials: Path = Path()
    notification_file: Path = Path()
    price_file: Path = Path()
    patch_sections: ClassVar[dict[str, str]] = {}

    @override
    def setUp(self) -> None:
        super().setUp()
        _ = cast(object, self.enterContext(mock.patch.object(box, 'urlopen', side_effect=AssertionError('tests require a fake tailnet transport'))))
        _ = self.settings.write_text((deploy.TEMPLATE.parents[3] / 'config/aws_target.conf').read_text())
        self.now = int(datetime.fromisoformat(DAY + "T23:59:00+00:00").timestamp())
        self.bundle = self.root / 'bundle'
        self.bundle.mkdir()
        bootstrap = self.bundle / 'bootstrap.sh'
        _ = bootstrap.write_text('#!/usr/bin/env bash\ntrue\n')
        hosts = self.root / 'nixos/hosts'
        hosts.mkdir(parents=True)
        modules = hosts.parent / 'modules/linux'
        modules.mkdir(parents=True)
        _ = (modules / 'tailscale.nix').write_text(
            '# The module enables Tailscale SSH.\n{ services.tailscale = {\n' +
            '  enable = true;\n  extraSetFlags = [ "--ssh" ];\n  openFirewall = true;\n}; }\n')
        bootstrap_template = hosts / 'bootstrap.sh.in'
        _ = bootstrap_template.write_text('\n'.join((
            '#!/usr/bin/env bash', '# services.amazon-ssm-agent.enable = true',
            'systemd-run --unit=aws-build-first-boot-stop --on-active=@IDLE_STOP_SECONDS@s systemctl poweroff',
            '# udevadm wait --timeout=@DATA_VOLUME_WAIT_SECONDS@',
            '# @SCRIPTS_SOURCE_URL_JSON@ @SCRIPTS_SOURCE_SHA256_JSON@ @SCRIPTS_SOURCE_COMMIT_JSON@',
            '# @REMOTE_SETTINGS_JSON@ @EXISTING_DATA_VOLUME_JSON@', '# @INPUT@', '',
        )))
        _ = (hosts / 'aws-build-base.nix').write_text(
            '# The base shares the transport module.\n{ ... }: { imports = [ ../modules/linux/tailscale.nix ]; }')
        self.notification_file = self.root / 'notification-email'
        _ = self.notification_file.write_text('build@example.invalid\n')
        self.text_setting('notification_email_file', str(self.notification_file))
        self.price_file = self.root / 'deployment-price'
        self.text_setting('deployment_price_file', str(self.price_file))
        self.write_price()
        values = {
            'admin_secret_file': str(self.root / 'admin.gpg'), 'offload': 'off', 'deployment_profile': 'build-admin', 'stack_name': 'fixture-build',
            'ami_id': 'ami-fixture', 'ami_owner_id': '123456789012', 'nixos_release': '26.05',
            'availability_zone': 'us-east-1a', 'vpc_id': 'vpc-fixture', 'subnet_id': 'subnet-fixture',
            'root_device_name': '/dev/xvda', 'scaler_user_name': 'build-scaler',
            'bootstrap_template_path': str(bootstrap_template),
            'scripts_source_url': 'https://example.invalid/public/commit.tar.gz',
            'scripts_source_sha256': 'sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=',
            'scripts_source_commit': 'a' * 40,
        }
        values.update({
            'root_volume_gib': '32', 'data_volume_gib': '500', 'data_volume_iops': '6000',
            'data_volume_throughput': '500', 'spot_max_usd_per_hour': '0.5',
            'budget_notice_low_percent': '50', 'budget_notice_high_percent': '80', 'budget_stop_percent': '100',
            'deployment_timeout_s': '30', 'preflight_max_age_s': '300', 'fixed_usd_per_month': '0.001',
            'retained_storage_usd_per_month': '0', 'root_storage_usd_per_month': '0',
            'ipv4_usd_per_month': '0', 'gp3_iops_usd_per_month': '0', 'gp3_throughput_usd_per_month': '0',
            'region': 'us-east-1', 'cost_explorer_lookback_days': '1',
            'cost_explorer_tag_key': 'Project', 'cost_explorer_tag_value': 'fixture-build', 'accounting_zone': 'UTC',
        })
        for key, value in values.items():
            self.replace(key, value)
        _ = cast(object, self.enterContext(mock.patch.object(admin_secrets.AdminSecretSession, 'get', return_value=
            admin_secrets.AdminCredentials('fixture-admin-id', 'fixture-admin-secret', 'fixture-client', 'fixture-oauth-secret'))))
        self.credentials = self.root / 'credentials'
        _ = self.credentials.write_text('')
        self.credentials.chmod(0o600)
        bin_path = self.root / 'bin'
        bin_path.mkdir()
        self.fake_commands(f'#!{sys.executable}\n' + FAKE_AWS, 'aws')
        status = {'Self': {'HostName': 'natedev', 'DNSName': 'natedev.tail21e383.ts.net.', 'TailscaleIPs': ['100.64.0.1']},
                  'Peer': {'mac': {'HostName': "Nate's MacBook Pro 2023", 'DNSName': 'nates-macbook-pro-2023.tail21e383.ts.net.', 'TailscaleIPs': ['100.64.0.2']}}}
        self.fake_commands(f'#!{sys.executable}\nprint({json.dumps(status)!r})\n', 'tailscale')
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            'PATH': f'{bin_path}{os.pathsep}{os.environ["PATH"]}', 'DEPLOY_FIXTURE': str(self.root),
            'AWS_SHARED_CREDENTIALS_FILE': str(self.credentials),
            'BOX_REAL_GIT': shutil.which('git') or 'git',
            'SCRATCH_ROOT': str(self.root / 'scratch'), 'SCRATCH_HOLDS': str(self.root / 'holds.json'),
        })))
        _ = cast(object, self.enterContext(mock.patch.object(target_config, 'ssh_target_state_directory', return_value=self.state)))
        self.set_clock(self.now)
        if self._testMethodName not in self.process_tests:
            process = SimpleNamespace(**{**cast(Mapping[str, object], vars(subprocess)), 'run': FixtureExecutor(self.root).run})
            _ = self.enterContext(mock.patch.object(aws_target, 'subprocess', process))

    # These tests retain child processes for environment, timeout, exit, and redaction checks.
    process_tests: ClassVar[frozenset[str]] = frozenset({
        'test_price_command_writes_private_state_for_the_selected_zone_and_admin_environment',
        'test_compute_timeout_keeps_exact_instance_costs_until_preparation_recovers',
        'test_terminal_update_failure_reconciles_compute_but_uncertain_failure_keeps_it_pending',
        'test_failure_redacts_the_prepared_address_after_the_private_file_changes',
        'test_update_failure_reports_current_events_and_rollback_without_the_address',
        'test_create_reservation_failures_report_events_reason_status_and_redact',
        'test_unconfirmed_scaler_key_is_deleted_and_never_printed',
        'test_admin_child_receives_only_the_environment_credentials',
        'test_admin_child_timeout_and_error_redact_secrets',
    })

    def test_admin_child_receives_only_the_environment_credentials(self) -> None:
        source = '''import json, os, sys
print(json.dumps({'key': os.environ.get('AWS_ACCESS_KEY_ID') == 'fixture-admin-id',
                  'secret': os.environ.get('AWS_SECRET_ACCESS_KEY') == 'fixture-admin-secret',
                  'config': os.environ.get('AWS_CONFIG_FILE'), 'profile': '--profile' in sys.argv,
                  'inherited_profile': 'AWS_PROFILE' in os.environ}))
'''
        self.fake_commands(f'#!{sys.executable}\n' + source, 'aws')
        execute = admin_secrets.AdminSecretSession(self.settings).executor(aws_target.execute_command)
        with mock.patch.dict(os.environ, {'AWS_PROFILE': 'unrelated'}):
            result = execute(['aws', '--profile', 'build-admin'], capture_output=True, text=True,
                             timeout=TEST_HANG_GUARD_SECS, check=True)
        self.assertEqual(json.loads(result.stdout), {'key': True, 'secret': True, 'config': '/dev/null',
                                                    'profile': False, 'inherited_profile': False})
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, '')

    def test_admin_child_timeout_and_error_redact_secrets(self) -> None:
        session = admin_secrets.AdminSecretSession(self.settings)
        session.credentials = session.get()
        execute = session.executor(aws_target.execute_command)
        self.fake_commands(f'#!{sys.executable}\nimport time\ntime.sleep(5)\n', 'aws')
        with self.assertRaises(subprocess.TimeoutExpired) as timeout:
            _ = execute(['aws'], capture_output=True, text=True, timeout=0.02, check=False)
        self.assertEqual(cast(object, timeout.exception.cmd), 'administrator AWS command')
        self.assertIsNone(cast(object, timeout.exception.output))
        source = '''import os, sys
print(os.environ['AWS_ACCESS_KEY_ID'])
print('child refused ' + os.environ['AWS_SECRET_ACCESS_KEY'], file=sys.stderr)
sys.exit(7)
'''
        self.fake_commands(f'#!{sys.executable}\n' + source, 'aws')
        result = execute(['aws'], capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS, check=False)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (7, '', 'child refused [redacted]\n'))
        with self.assertRaises(subprocess.CalledProcessError) as failure:
            _ = execute(['aws'], capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS, check=True)
        self.assertEqual(failure.exception.returncode, 7)
        self.assertEqual(cast(object, failure.exception.stderr), 'child refused [redacted]\n')
        self.assertIsNone(cast(object, failure.exception.output))

    def test_fixture_executor_preserves_command_records_output_and_environment(self) -> None:
        runner = FixtureExecutor(self.root)
        environment = dict(os.environ, FIXTURE_CHILD_ONLY='present')
        arguments = ['aws', '--region', 'us-east-1', 'sts', 'get-caller-identity']
        real = subprocess.run(arguments, capture_output=True, text=True, env=environment,
                              check=False, timeout=TEST_HANG_GUARD_SECS)
        record = (self.root / 'commands.jsonl').read_bytes()
        (self.root / 'commands.jsonl').unlink()
        argv = sys.argv
        before = dict(os.environ)
        result = runner.run(arguments, capture_output=True, text=True, env=environment,
                            check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (real.returncode, real.stdout, real.stderr))
        self.assertEqual((self.root / 'commands.jsonl').read_bytes(), record)
        self.assertEqual(dict(os.environ), before)
        self.assertIs(sys.argv, argv)
        self.fake_commands(f'#!{sys.executable}\nimport sys\nprint("failure", file=sys.stderr)\nsys.exit(3)\n', 'aws')
        with self.assertRaises(subprocess.CalledProcessError) as failure:
            _ = runner.run(['aws'], capture_output=True, text=True, env=environment,
                           check=True, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(failure.exception.returncode, 3)
        self.assertEqual(cast(object, failure.exception.stderr), 'failure\n')
        self.assertEqual(dict(os.environ), before)
        self.assertIs(sys.argv, argv)

    @override
    def replace(self, key: str, value: str) -> None:
        if key in ('instance_id', 'existing_data_volume_id'):
            saved: dict[str, object] = aws_config.deployment_record(self.state)
            if not saved:
                saved = {'stack': 'previous-build', 'region': 'us-east-1',
                         'outputs': {'InstanceId': 'i-fixture'}}
            outputs = ledger.mapping(saved.get('outputs'))
            if key == 'instance_id':
                outputs['InstanceId'] = value
            else:
                outputs['ExistingDataInput'] = value
                outputs['DataVolumeId'] = value
            saved['outputs'] = outputs
            _ = (self.state / 'deployment.json').write_text(json.dumps(saved))
            return
        super().replace(key, value)

    def set_clock(self, at: int) -> None:
        self.now = at
        _ = (self.root / 'clock').write_text(str(at))

    def write_price(self, price: str = '0.1') -> None:
        _ = self.price_file.write_text(f'deployment_price_usd_per_hour={price}\n'
                                       + f'deployment_price_at={self.now}\n')

    def deployment(self) -> deploy.Deployment:
        return deploy.Deployment(self.settings, self.state, self.bundle, clock=lambda: self.now,
                                 notify=lambda _event: True)

    def commands(self, operation: str = '') -> list[dict[str, object]]:
        path = self.root / 'commands.jsonl'
        rows = [cast(dict[str, object], json.loads(line)) for line in path.read_text().splitlines()] if path.exists() else []
        if operation == 'deploy':
            # Compare parameters for both foundation deploys and inspected update executions.
            updates: dict[str, list[str]] = {}
            result: list[dict[str, object]] = []
            for row in rows:
                args = cast(list[str], row['args'])
                if 'create-change-set' in args and 'UPDATE' in args:
                    values = ledger.rows({'values': cast(object, json.loads(args[args.index('--parameters') + 1]))}, 'values')
                    updates[args[args.index('--change-set-name') + 1]] = [
                        f"{ledger.text(value, 'ParameterKey')}={value['ParameterValue']}"
                        for value in values if 'ParameterValue' in value]
                if 'deploy' in args:
                    result.append(row)
                elif 'execute-change-set' in args:
                    result.append({**row, 'args': [*args, *updates[args[args.index('--change-set-name') + 1]]]})
            return result
        return [row for row in rows if not operation or operation in cast(list[str], row['args'])]

    def prepare_stopped_instance(self) -> int:
        self.set_clock(int(datetime(2026, 1, 15, 12, tzinfo=UTC).timestamp()))
        self.write_price()
        for key, value in (('fallback_usd_per_hour', '1'),
                           ('price_max_age_seconds', '86400'), ('observation_max_age_seconds', '86400'),
                           ('cost_explorer_read_usd', '0')):
            self.replace(key, value)
        ledger.CostLedger(self.state).observe(ledger.InstanceObservation(self.now, 'stopped', 'prior stopped evidence'))
        _ = self.deployment().prepare()
        return self.now

    def failing_compute(self, failure: str) -> deploy.Deployment:
        compute_returned = False

        def execute(arguments: Sequence[str], *, capture_output: bool, text: bool,
                    timeout: float, check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
            nonlocal compute_returned
            operation = arguments[arguments.index('--region') + 3]
            if compute_returned and ((failure in ('stack', 'timeout') and operation == 'describe-stacks')
                                     or (failure == 'instance' and operation == 'describe-instances')):
                return subprocess.CompletedProcess(arguments, 1, '', 'the requested observation is unavailable')
            result = aws_target.execute_command(arguments, capture_output=capture_output, text=text, timeout=timeout, check=check, env=env)
            if operation == 'execute-change-set':
                compute_returned = True
                if failure == 'timeout':
                    raise subprocess.TimeoutExpired(arguments, timeout)
            return result

        return deploy.Deployment(self.settings, self.state, self.bundle, execute=execute,
                                 clock=lambda: self.now, notify=lambda _event: True)

    def instance_intervals(self, at: int) -> list[tuple[int, int, Decimal, int]]:
        costs = ledger.costs_from_evidence(ledger.CostLedger(self.state).read_snapshot(),
                                           aws_config.read_settings(self.settings), at, current_window=False)
        return [(r.start, r.end, r.usd, r.confirmed_uptime_s) for r in costs.intervals if r.kind == 'instance']

    def assert_compute_failure_accounting(self, failure: str, recovery: str) -> None:
        start = self.prepare_stopped_instance()
        subject = self.failing_compute(failure)
        with self.assertRaises((subprocess.TimeoutExpired, aws_target.AwsUnavailable)):
            _ = subject.apply(approve_resources=True)
        book = ledger.CostLedger(self.state)
        self.assertEqual(len(book.pending_compute_requests()), 1)
        self.assertEqual(bool(ledger.mapping(remote.read_record(self.state / 'deployment.json').get('outputs', {}))), failure == 'instance')
        self.set_clock(start + 7200)
        self.assertEqual(self.instance_intervals(self.now), [(start, self.now, Decimal('2'), 0)])
        self.write_price()
        with self.assertRaises(aws_target.AwsUnavailable):
            _ = subject.prepare()
        self.assertEqual(len(book.pending_compute_requests()), 1)
        self.assertEqual(self.instance_intervals(self.now), [(start, self.now, Decimal('2'), 0)])
        _ = (self.root / 'describe-instances.json').write_text(json.dumps({
            'Reservations': [{'Instances': [{'InstanceId': 'i-fixture', 'LaunchTime': 'recovered-boot',
                                            'State': {'Name': 'running'}}]}]}))
        recovered = self.deployment()
        if recovery != 'prepare':
            with self.assertRaisesRegex(deploy.DeploymentRefused, 'expired'):
                if recovery == 'apply':
                    _ = recovered.apply(approve_resources=True)
                else:
                    _ = recovered.setup_start(approve_resources=True)
            self.assertEqual(book.pending_compute_requests(), ())
        _ = recovered.prepare()
        self.assertEqual(book.pending_compute_requests(), ())
        decision = ledger.mapping(remote.read_record(self.bundle / 'preflight.json')['decision'])
        self.assertEqual([(ledger.instant(r, 'start'), ledger.instant(r, 'end'), Decimal(ledger.text(r, 'usd')))
                          for r in ledger.rows(decision, 'intervals') if r.get('kind') == 'instance'],
                         [(start, self.now, Decimal('2'))])
        self.assertEqual(self.instance_intervals(self.now + 3600),
                         [(self.now, self.now + 3600, Decimal('0.1'), 3600),
                          (start, self.now, Decimal('2'), 0)])

    def update_fixture(self) -> deploy.Deployment:
        subject = self.deployment()
        _ = subject.prepare()
        _ = subject.apply(approve_resources=True)
        self.set_clock(self.now + 1)
        _ = subject.prepare()
        return subject

    def gate_image_fixture(self, failure: str = '') -> tuple[deploy.Deployment, GateImageAws]:
        subject = self.deployment()
        subject.directory = self.state / ('image-' + (failure or 'success'))
        subject.directory.mkdir()
        book = ledger.CostLedger(subject.directory)
        book.record_inventory(ledger.ResourceInventory(self.now - 1,
            (ledger.PricedResource('gate-shared', aws_config.money('1')),), 'fixture', builder_removed=True))
        _ = (self.root / 'cloud_gate.conf').write_bytes((deploy.TEMPLATE.parents[3] / 'config/cloud_gate.conf').read_bytes())
        _ = (subject.directory / 'deployment.json').write_text(json.dumps({
            'stack_id': 'fixture-stack', 'stack': 'fixture-build'}))
        _ = (subject.directory / 'gate-deployment.json').write_text(json.dumps({
            'deployment_id': 'fixture', 'bucket': 'fixture-bucket', 'template': 'fixture-template',
            'version': '1', 'security_group': 'fixture-group', 'revision': 'f' * 40, 'image': 'ami-fixture',
            'subnets': [{'id': 'fixture-subnet', 'zone': 'fixture-zone'}]}))
        def advance(seconds: float) -> None:
            self.set_clock(self.now + max(1, int(seconds)))
        subject.pause = advance
        fake = GateImageAws(failure)
        fake.manifest = subject.directory / 'gate-deployment.json'
        _ = self.enterContext(mock.patch.object(subject, '_api', return_value=aws_target.AwsCli(
            'fixture-admin', 'us-east-1', 1, execute=fake)))
        return subject, fake

    def test_gate_image_dry_run_needs_no_credentials_and_writes_nothing(self) -> None:
        self.credentials.unlink()
        before = {path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()}
        with mock.patch.object(admin_secrets.AdminSecretSession, 'get') as credentials, redirect_stdout(StringIO()) as output:
            self.assertTrue(self.deployment().gate_image())
        credentials.assert_not_called()
        self.assertIn('gate-image --approve-resources', output.getvalue())
        self.assertEqual({path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()}, before)

    def test_gate_image_reuses_tools_and_replaces_only_after_input_changes(self) -> None:
        subject, fake = self.gate_image_fixture()
        with mock.patch.object(gate_trial, 'host_nixpkgs_revision', return_value='f' * 40) as revision:
            with mock.patch.object(box, 'create_saved_image', wraps=box.create_saved_image) as create, redirect_stdout(StringIO()) as output:
                self.assertTrue(subject.gate_image(approve_resources=True))
            self.assertEqual(create.call_count, 1)
            self.assertIn('32 GiB; monthly snapshot estimate $1.60', output.getvalue())
            self.assertTrue(fake.terminated)
            self.assertNotIn('ami-old', fake.images)
            self.assertNotIn('snap-old', fake.snapshots)
            self.assertIn('ami-builder', fake.images)
            self.assertIn('snap-builder', fake.snapshots)
            operations = [args[args.index('--region') + 3] for args in fake.commands]
            self.assertLess(operations.index('create-image'), operations.index('terminate-instances'))
            self.assertLess(operations.index('terminate-instances'), operations.index('deregister-image'))
            self.assertEqual(fake.images['ami-gate-1']['State'], 'available')
            with mock.patch.object(gate_box, 'GUEST', 'changed gate code'), redirect_stdout(StringIO()):
                self.assertTrue(subject.gate_image(approve_resources=True))
            self.assertEqual(len(fake.launches), 1)
            path = self.root / 'cloud_gate.conf'
            _ = path.write_text(path.read_text().replace('packages=git ', 'packages=hello git '))
            with redirect_stdout(StringIO()):
                self.assertTrue(subject.gate_image(approve_resources=True))
            self.assertEqual(len(fake.launches), 2)
            self.assertNotIn('ami-gate-1', fake.images)
            self.assertNotIn('snap-gate-1', fake.snapshots)
            revision.return_value = 'e' * 40
            with redirect_stdout(StringIO()):
                self.assertTrue(subject.gate_image(approve_resources=True))
            self.assertEqual(len(fake.launches), 3)
        manifest = remote.read_record(subject.directory / 'gate-deployment.json')
        self.assertEqual(manifest['image'], 'ami-gate-3')
        self.assertEqual(manifest['revision'], 'e' * 40)
        self.assertEqual(manifest['version'], '4')
        self.assertEqual(len(fake.gate_changes), 6)
        for index, change in enumerate(fake.gate_changes):
            template = ledger.mapping(change['Template'])
            self.assertEqual(ledger.mapping(template['Resources'])['GateExpiryFunction'], {'live': 'keep'})
            image_policy = ledger.mapping(ledger.mapping(ledger.mapping(
                ledger.mapping(template['Resources'])['GateScalerPolicy'])['Properties'])['PolicyDocument'])
            statements = ledger.rows(image_policy, 'Statement')
            self.assertEqual(len(statements), 2 if index % 2 == 0 else 1)
            if index % 2 == 0:
                previous = 'ami-fixture' if index == 0 else 'ami-gate-' + str(index // 2)
                self.assertEqual(statements[-1], {'Sid': 'GateImageTransition', 'Effect': 'Allow',
                    'Action': ['ec2:RunInstances'], 'Resource': {
                        'Fn::Sub': 'arn:${AWS::Partition}:ec2:${AWS::Region}::image/' + previous}})
            else:
                self.assertEqual(template, fake.live_template)
            rows = ledger.rows(change, 'Parameters')
            self.assertEqual(next(row for row in rows if row['ParameterKey'] == 'EnableCompute'),
                             {'ParameterKey': 'EnableCompute', 'UsePreviousValue': True})
            image_parameter = next(row for row in rows if row['ParameterKey'] == 'GateImage')
            self.assertEqual(image_parameter, {'ParameterKey': 'GateImage', 'ParameterValue':
                'ami-gate-' + str(index // 2 + 1)} if index % 2 == 0 else {
                    'ParameterKey': 'GateImage', 'UsePreviousValue': True})
        self.assertEqual(fake.launches[0]['ImageId'], 'ami-fixture')
        data = ledger.text(fake.launches[0], 'UserData')
        self.assertIn('cloud-gate-image-expiry', data)
        self.assertNotIn('source.bundle', data)
        self.assertNotIn('fixture-secret', data)
        inventories = ledger.CostLedger(subject.directory).read_snapshot().inventories
        self.assertTrue(inventories[-1].builder_removed)
        self.assertEqual(inventories[-1].resources[-1], ledger.PricedResource('gate-image', aws_config.money('1.60')))

    def test_gate_image_retries_activation_without_rebuilding_or_deleting_the_old_snapshot(self) -> None:
        subject, fake = self.gate_image_fixture('activate')
        with mock.patch.object(gate_trial, 'host_nixpkgs_revision', return_value='f' * 40), redirect_stdout(StringIO()):
            with self.assertRaises(aws_target.AwsUnavailable):
                _ = subject.gate_image(approve_resources=True)
            self.assertTrue(fake.terminated)
            self.assertIn('snap-old', fake.snapshots)
            self.assertEqual(remote.read_record(subject.directory / 'gate-deployment.json')['image'], 'ami-fixture')
            fake.failure = ''
            self.assertTrue(subject.gate_image(approve_resources=True))
        self.assertEqual(len(fake.launches), 1)
        self.assertNotIn('snap-old', fake.snapshots)
        self.assertEqual(remote.read_record(subject.directory / 'gate-deployment.json')['image'], 'ami-gate-1')

    def test_gate_image_keeps_the_selected_image_launchable_when_publication_fails(self) -> None:
        for failure in ('waiter', 'manifest', 'narrow waiter'):
            with self.subTest(failure=failure):
                subject, fake = self.gate_image_fixture(failure)
                atomic_write = write_text_atomic
                def write(path: Path, text: str) -> None:
                    if path == fake.manifest and fake.failure == 'manifest':
                        raise OSError('fixture manifest write failure')
                    atomic_write(path, text)
                with (mock.patch.object(gate_trial, 'host_nixpkgs_revision', return_value='f' * 40),
                      mock.patch.object(deploy, 'write_text_atomic', side_effect=write), redirect_stdout(StringIO())):
                    with self.assertRaises((aws_target.AwsUnavailable, OSError)):
                        _ = subject.gate_image(approve_resources=True)
                    selected = ledger.text(remote.read_record(fake.manifest), 'image')
                    self.assertEqual(selected, 'ami-gate-1' if failure == 'narrow waiter' else 'ami-fixture')
                    self.assertIn(selected, fake.launchable_images())
                    self.assertEqual(fake.image_parameter, 'ami-gate-1')
                    self.assertIn('snap-old', fake.snapshots)
                    fake.failure = ''
                    self.assertTrue(subject.gate_image(approve_resources=True))
                self.assertEqual(len(fake.launches), 1)
                self.assertEqual(fake.launchable_images(), {'ami-gate-1'})
                self.assertNotIn('snap-old', fake.snapshots)

    def test_gate_image_keeps_the_selected_image_after_interrupted_record_promotion(self) -> None:
        for stale_record_returns in (False, True):
            with self.subTest(stale_record_returns=stale_record_returns):
                subject, fake = self.gate_image_fixture('stale-' + str(stale_record_returns))
                pending = subject.directory / 'gate-image/pending/image.json'
                saved = subject.directory / 'gate-image/image.json'
                original_unlink = Path.unlink
                def interrupted_unlink(path: Path, missing_ok: bool = False) -> None:
                    if path == pending:
                        raise KeyboardInterrupt
                    original_unlink(path, missing_ok=missing_ok)
                with (mock.patch.object(gate_trial, 'host_nixpkgs_revision', return_value='f' * 40) as revision,
                      redirect_stdout(StringIO())):
                    with (mock.patch.object(Path, 'unlink', autospec=True, side_effect=interrupted_unlink),
                          self.assertRaises(KeyboardInterrupt)):
                        _ = subject.gate_image(approve_resources=True)
                    self.assertEqual(remote.read_record(saved), remote.read_record(pending))
                    self.assertTrue(fake.terminated)
                    self.assertEqual(remote.read_record(fake.manifest)['image'], 'ami-fixture')
                    self.assertTrue(subject.gate_image(approve_resources=True))
                    self.assertFalse(pending.exists())
                    self.assertEqual(len(fake.launches), 1)
                    self.assertEqual(remote.read_record(fake.manifest)['image'], 'ami-gate-1')
                    if stale_record_returns:
                        write_text_atomic(pending, saved.read_text())
                    revision.return_value = 'e' * 40
                    fake.failure = 'prepare'
                    with self.assertRaises(deploy.DeploymentRefused):
                        _ = subject.gate_image(approve_resources=True)
                self.assertEqual(len(fake.launches), 2)
                self.assertTrue(fake.terminated)
                self.assertEqual(remote.read_record(fake.manifest)['image'], 'ami-gate-1')
                self.assertIn('ami-gate-1', fake.images)
                self.assertIn('snap-gate-1', fake.snapshots)
                self.assertEqual(remote.read_record(saved)['image_id'], 'ami-gate-1')
                self.assertFalse(pending.exists())
                self.assertEqual(fake.launchable_images(), {'ami-gate-1'})

    def test_gate_image_terminates_the_box_on_failure_timeout_and_interruption(self) -> None:
        self.replace('gate_image_timeout_s', '60')
        for failure in ('prepare', 'timeout', 'create', 'image', 'image timeout', 'image interrupt', 'lost launch', 'interrupt'):
            with self.subTest(failure=failure):
                subject, fake = self.gate_image_fixture(failure)
                manifest = remote.read_record(fake.manifest)
                manifest['image'] = 'ami-old'
                write_text_atomic(fake.manifest, json.dumps(manifest))
                fake.image_parameter = 'ami-old'
                with (mock.patch.object(gate_trial, 'host_nixpkgs_revision', return_value='f' * 40),
                      self.assertRaises((deploy.DeploymentRefused, aws_target.AwsUnavailable,
                                         subprocess.TimeoutExpired, KeyboardInterrupt)), redirect_stdout(StringIO())):
                    _ = subject.gate_image(approve_resources=True)
                self.assertEqual(len(fake.launches), 1)
                self.assertTrue(fake.terminated)
                self.assertFalse((subject.directory / 'gate-image/image.json').exists())
                self.assertIn('ami-old', fake.images)
                self.assertIn('snap-old', fake.snapshots)
                self.assertNotIn('ami-gate-1', fake.images)
                self.assertNotIn('snap-gate-1', fake.snapshots)
                self.assertFalse((subject.directory / 'gate-image/pending/image.json').exists())
                self.assertIn('ami-builder', fake.images)
                self.assertIn('snap-builder', fake.snapshots)
                reservations = ledger.CostLedger(subject.directory).read_snapshot().gate_reservations
                self.assertIsInstance(reservations[-1].state, ledger.GateTerminated)

    def gate_fixture(self, change: str = 'GateBucket') -> tuple[deploy.Deployment, list[list[str]]]:
        subject = self.update_fixture()
        self.set_clock(self.now + 1)
        _ = (self.root / 'cloud_gate.conf').write_bytes(
            (deploy.TEMPLATE.parents[3] / 'config/cloud_gate.conf').read_bytes())
        commands: list[list[str]] = []
        outputs = {'GateBucket': 'fixture-bucket', 'GateLaunchTemplate': 'fixture-template',
                   'GateLaunchVersion': '1', 'GateSecurityGroup': 'fixture-group', 'GateRoleId': 'fixture-role'}

        def execute(arguments: Sequence[str], **_options: object) -> subprocess.CompletedProcess[str]:
            args = list(arguments)
            commands.append(args)
            operation = args[args.index('--region') + 3]
            reply: dict[str, object] = {}
            if operation == 'describe-stacks':
                reply = {'Stacks': [{'Parameters': [{'ParameterKey': key} for key in
                    ('EnableCompute', 'InstanceType', 'ExistingDataVolumeId', 'NotificationEmail')]}]}
            elif operation == 'describe-route-tables':
                reply = {'RouteTables': [{'Associations': [{'Main': True}], 'Routes': [
                    {'DestinationCidrBlock': '0.0.0.0/0', 'State': 'active', 'GatewayId': 'igw-fixture'}]}]}
            elif operation == 'describe-subnets':
                reply = {'Subnets': [{'SubnetId': 'fixture-' + zone, 'AvailabilityZone': zone, 'State': 'available'}
                                    for zone in ('a', 'b')]}
            elif operation == 'get-caller-identity':
                reply = {'Account': 'fixture-account'}
            elif operation == 'describe-change-set':
                reply = {'Status': 'CREATE_COMPLETE', 'Changes': [{'ResourceChange': {'LogicalResourceId': change}}]}
            elif operation not in ('create-change-set', 'execute-change-set', 'delete-change-set', 'wait'):
                raise AssertionError(operation)
            return subprocess.CompletedProcess(args, 0, json.dumps(reply), '')

        _ = self.enterContext(mock.patch.object(subject, '_api', return_value=aws_target.AwsCli(
            'build-admin', 'us-east-1', 1, execute=execute)))
        _ = self.enterContext(mock.patch.object(subject, '_stack', return_value=outputs))
        _ = self.enterContext(mock.patch.object(gate_trial, 'host_nixpkgs_revision', return_value='fixture-revision'))
        return subject, commands

    def test_gate_deployment_preserves_rust_parameters_and_records_scoped_resources(self) -> None:
        subject, commands = self.gate_fixture()
        before = ledger.CostLedger(self.state).read_snapshot().inventories[-1].resources
        self.assertTrue(subject.gate_up(approve_resources=True))
        arguments = next(args for args in commands if 'create-change-set' in args)
        parameters = ledger.rows({'rows': cast(object, json.loads(arguments[arguments.index('--parameters') + 1]))}, 'rows')
        by_name = {ledger.text(row, 'ParameterKey'): row for row in parameters}
        for key in ('EnableCompute', 'InstanceType', 'ExistingDataVolumeId', 'NotificationEmail'):
            self.assertEqual(by_name[key], {'ParameterKey': key, 'UsePreviousValue': True})
        self.assertEqual(by_name['GateInstanceTypes']['ParameterValue'], 'm7a.8xlarge,m7a.4xlarge')
        self.assertEqual(by_name['GateRootGiB']['ParameterValue'], '40')
        self.assertEqual(by_name['GateSpotMaxPrice']['ParameterValue'], '0.8')
        saved = remote.read_record(self.state / 'gate-deployment.json')
        self.assertEqual(saved['artifact_prefix'], 'artifacts/fixture-role:')
        self.assertEqual(saved['revision'], 'fixture-revision')
        self.assertEqual([row['zone'] for row in ledger.rows(saved, 'subnets')], ['a', 'b'])
        after = ledger.CostLedger(self.state).read_snapshot().inventories[-1].resources
        self.assertEqual(after, before + (ledger.PricedResource('gate-shared', Decimal(1)),))
        self.assertTrue(any('execute-change-set' in args for args in commands))

    def test_gate_deployment_rejects_a_change_to_a_rust_resource(self) -> None:
        subject, commands = self.gate_fixture('Instance')
        with self.assertRaisesRegex(deploy.DeploymentRefused, 'Rust resource'):
            _ = subject.gate_up(approve_resources=True)
        self.assertFalse(any('execute-change-set' in args for args in commands))
        self.assertTrue(any('delete-change-set' in args for args in commands))
        self.assertFalse((self.state / 'gate-deployment.json').exists())

    def test_live_update_without_fingerprint_preserves_first_boot_and_saves_fingerprint(self) -> None:
        subject = self.update_fixture()
        path = self.state / 'deployment.json'
        saved = remote.read_record(path)
        _ = saved.pop('fingerprint')
        _ = path.write_text(json.dumps(saved))
        template = Path(conf.read(self.settings).text('bootstrap_template_path'))
        _ = template.write_text(template.read_text() + '# changed first boot input\n')
        success, output = self.up_output(subject)
        self.assertTrue(success, output)
        self.assertIn('stacks: done', output)
        args = cast(list[str], self.commands('create-change-set')[-1]['args'])
        values = ledger.rows({'values': cast(object, json.loads(args[args.index('--parameters') + 1]))}, 'values')
        self.assertEqual(next(row for row in values if row['ParameterKey'] == 'Bootstrap'),
                         {'ParameterKey': 'Bootstrap', 'UsePreviousValue': True})
        self.assertIn('fingerprint', remote.read_record(path))
        self.assertEqual(aws_config.deployment_outputs(self.state)['InstanceId'], 'i-fixture')

    def test_update_refuses_replacement_for_each_protected_resource_type(self) -> None:
        subject = self.update_fixture()
        for logical, kind in (
                ('Instance', 'AWS::EC2::Instance'), ('DataVolume', 'AWS::EC2::Volume'),
                ('DataAttachment', 'AWS::EC2::VolumeAttachment'), ('Interface', 'AWS::EC2::NetworkInterface'),
                ('InterfaceAttachment', 'AWS::EC2::NetworkInterfaceAttachment'), ('Address', 'AWS::EC2::EIP'),
                ('Association', 'AWS::EC2::EIPAssociation'), ('Budget', 'AWS::Budgets::Budget')):
            for replacement in ('True', 'Conditional'):
                with self.subTest(kind=kind, replacement=replacement):
                    _ = (self.root / 'update-plan.json').write_text(json.dumps({'Changes': [{'ResourceChange': {
                        'LogicalResourceId': logical, 'ResourceType': kind, 'Replacement': replacement}}]}))
                    before = len(self.commands('execute-change-set'))
                    with self.assertRaisesRegex(deploy.DeploymentRefused, logical) as failure:
                        _ = subject.apply(approve_resources=True)
                    self.assertIn(kind, str(failure.exception))
                    self.assertIn('deploy.py down --approve-destroy, then deploy.py up --approve-resources', str(failure.exception))
                    self.assertEqual(len(self.commands('execute-change-set')), before)
                    self.assertFalse((self.root / 'update-change-set.json').exists())

    def test_update_refuses_removal_for_each_protected_type(self) -> None:
        subject = self.update_fixture()
        for kind in ('AWS::EC2::Instance', 'AWS::EC2::Volume', 'AWS::EC2::VolumeAttachment',
                     'AWS::EC2::NetworkInterface', 'AWS::EC2::NetworkInterfaceAttachment',
                     'AWS::EC2::EIP', 'AWS::EC2::EIPAssociation', 'AWS::Budgets::Budget'):
            with self.subTest(kind=kind):
                _ = (self.root / 'update-plan.json').write_text(json.dumps({'Changes': [{'ResourceChange': {
                    'LogicalResourceId': 'OldResource', 'ResourceType': kind, 'Action': 'Remove'}},
                    {'ResourceChange': {'LogicalResourceId': 'NewResource', 'ResourceType': kind, 'Action': 'Add'}}]}))
                before = len(self.commands('execute-change-set'))
                with self.assertRaisesRegex(deploy.DeploymentRefused, 'OldResource') as failure:
                    _ = subject.apply(approve_resources=True)
                self.assertIn(kind, str(failure.exception))
                self.assertIn('deploy.py down --approve-destroy, then deploy.py up --approve-resources', str(failure.exception))
                self.assertEqual(len(self.commands('execute-change-set')), before)
                self.assertFalse((self.root / 'update-change-set.json').exists())

    def test_unexecuted_update_cleanup_preserves_each_refusal_and_reports_delete_failure(self) -> None:
        subject = self.update_fixture()
        for cause in ('spending check refused', 'preparation check refused', 'create wait failed', 'protected removal'):
            for delete_fails in (False, True):
                with self.subTest(cause=cause, delete_fails=delete_fails):
                    if cause == 'protected removal':
                        _ = (self.root / 'update-plan.json').write_text(json.dumps({'Changes': [{'ResourceChange': {
                            'LogicalResourceId': 'Protected', 'ResourceType': 'AWS::EC2::Instance', 'Action': 'Remove'}}]}))
                    if cause == 'create wait failed':
                        (self.root / 'fail-wait-change-set-create-complete').touch()
                    if delete_fails:
                        (self.root / 'fail-down-delete-change-set').touch()
                    settings = aws_config.read_deployment_settings(self.settings, self.state)
                    prepared = subject.prepare()
                    before = len(self.commands('execute-change-set'))
                    with mock.patch.object(subject, '_before_request', side_effect=deploy.DeploymentRefused(cause)):
                        with self.assertRaises((deploy.DeploymentRefused, aws_target.AwsUnavailable)) as failure:
                            subject._update_stack(settings, prepared.parameters, compute=True)  # pyright: ignore[reportPrivateUsage]
                    expected = {'create wait failed': 'fixture wait failed', 'protected removal': 'Protected'}.get(cause, cause)
                    self.assertIn(expected, str(failure.exception))
                    args = cast(list[str], self.commands('create-change-set')[-1]['args'])
                    name = args[args.index('--change-set-name') + 1]
                    self.assertEqual('unexecuted change set remains: ' + name in str(failure.exception), delete_fails)
                    self.assertEqual((self.root / 'update-change-set.json').exists(), delete_fails)
                    self.assertEqual(len(self.commands('execute-change-set')), before)
                    for filename in ('update-change-set.json', 'update-plan.json', 'fail-down-delete-change-set',
                                     'fail-wait-change-set-create-complete'):
                        (self.root / filename).unlink(missing_ok=True)

    def test_lost_update_creation_response_discards_the_recovered_change_set(self) -> None:
        subject = self.update_fixture()
        execute = subject.execute
        before = len(self.commands('execute-change-set'))
        def lose_response(arguments: Sequence[str], *, capture_output: bool, text: bool, timeout: float,
                          check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
            result = execute(arguments, capture_output=capture_output, text=text, timeout=timeout, check=check, env=env)
            if 'create-change-set' in arguments:
                return subprocess.CompletedProcess(arguments, 1, '', 'update creation response lost')
            return result
        subject.execute = lose_response
        with self.assertRaisesRegex(aws_target.AwsUnavailable, 'update creation response lost'):
            _ = subject.apply(approve_resources=True)
        self.assertTrue(self.commands('describe-change-set'))
        self.assertTrue(self.commands('delete-change-set'))
        self.assertFalse((self.root / 'update-change-set.json').exists())
        self.assertEqual(len(self.commands('execute-change-set')), before)

    def test_cleanup_value_and_os_errors_preserve_the_original_refusal(self) -> None:
        subject = self.update_fixture()
        settings = aws_config.read_deployment_settings(self.settings, self.state)
        prepared = subject.prepare()
        api = subject._api(settings)  # pyright: ignore[reportPrivateUsage]
        call = api.call
        for error in (ValueError('cleanup value error'), OSError('cleanup OS error')):
            with self.subTest(error=type(error).__name__):
                def fail_delete(operation: str, *arguments: str, service: str = 'ec2') -> dict[str, object]:
                    if operation == 'delete-change-set':
                        raise error
                    return call(operation, *arguments, service=service)
                with (mock.patch.object(subject, '_api', return_value=api),
                      mock.patch.object(api, 'call', side_effect=fail_delete),
                      mock.patch.object(subject, '_before_request', side_effect=deploy.DeploymentRefused('original spending refusal'))):
                    with self.assertRaisesRegex(deploy.DeploymentRefused, 'original spending refusal') as failure:
                        subject._update_stack(settings, prepared.parameters, compute=True)  # pyright: ignore[reportPrivateUsage]
                args = cast(list[str], self.commands('create-change-set')[-1]['args'])
                name = args[args.index('--change-set-name') + 1]
                self.assertIn('unexecuted change set remains: ' + name, str(failure.exception))
                self.assertNotIn(str(error), str(failure.exception))
                self.assertTrue((self.root / 'update-change-set.json').exists())
                (self.root / 'update-change-set.json').unlink()

    def test_reconciliation_os_error_preserves_update_failure_diagnostics(self) -> None:
        subject = self.update_fixture()
        execute = subject.execute
        _ = (self.root / 'describe-stack-events.json').write_text(json.dumps({'StackEvents': [{
            'Timestamp': datetime.fromtimestamp(self.now, UTC).isoformat(), 'LogicalResourceId': 'DataAttachment',
            'ResourceType': 'AWS::EC2::VolumeAttachment', 'ResourceStatus': 'UPDATE_FAILED',
            'ResourceStatusReason': 'fixture attachment failure'}]}))
        def fail_update(arguments: Sequence[str], *, capture_output: bool, text: bool, timeout: float,
                        check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
            if 'execute-change-set' in arguments:
                return subprocess.CompletedProcess(arguments, 1, '', 'original update failure')
            return execute(arguments, capture_output=capture_output, text=text, timeout=timeout, check=check, env=env)
        subject.execute = fail_update
        with mock.patch.object(subject, '_reconcile_compute_request', side_effect=OSError('fixture disk full')) as reconcile:
            with self.assertRaisesRegex(aws_target.AwsUnavailable, 'original update failure') as failure:
                _ = subject.apply(approve_resources=True)
        reconcile.assert_called_once()
        reason = str(failure.exception)
        self.assertIn('DataAttachment (AWS::EC2::VolumeAttachment): UPDATE_FAILED: fixture attachment failure', reason)
        self.assertIn('stack status: UPDATE_COMPLETE', reason)
        self.assertIn('compute request reconciliation is unavailable', reason)
        self.assertNotIn('fixture disk full', reason)
        self.assertTrue(ledger.CostLedger(self.state).pending_compute_requests())

    def test_foundation_preparation_refusal_discards_the_reservation(self) -> None:
        subject = self.deployment()
        _ = subject.prepare()
        confirm = subject._confirm_preparation  # pyright: ignore[reportPrivateUsage]
        def refuse(settings: aws_config.DeploymentSettings, parameters: dict[str, str], *, renew: bool = False) -> None:
            if renew:
                raise deploy.DeploymentRefused('foundation preparation changed')
            confirm(settings, parameters, renew=renew)
        with mock.patch.object(subject, '_confirm_preparation', side_effect=refuse):
            with self.assertRaisesRegex(deploy.DeploymentRefused, 'foundation preparation changed'):
                _ = subject.apply(approve_resources=True)
        self.assertFalse((self.root / 'change-set.json').exists())
        self.assertFalse(self.commands('deploy'))

    def test_failure_redacts_the_prepared_address_after_the_private_file_changes(self) -> None:
        subject = self.update_fixture()
        notification = self.notification_file.read_text().strip()
        execute = subject.execute
        for remove in (False, True):
            with self.subTest(remove=remove):
                def fail(arguments: Sequence[str], *, capture_output: bool, text: bool, timeout: float,
                         check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
                    if 'execute-change-set' in arguments:
                        if remove:
                            self.notification_file.unlink()
                        else:
                            _ = self.notification_file.write_text('changed@example.invalid\n')
                        return subprocess.CompletedProcess(arguments, 1, '', 'update refused ' + notification)
                    return execute(arguments, capture_output=capture_output, text=text, timeout=timeout, check=check, env=env)
                subject.execute = fail
                try:
                    with self.assertRaisesRegex(aws_target.AwsUnavailable, 'update refused <notification address>') as failure:
                        _ = subject.apply(approve_resources=True)
                    self.assertNotIn(notification, str(failure.exception))
                    self.assertIn('stack status: UPDATE_COMPLETE', str(failure.exception))
                finally:
                    _ = self.notification_file.write_text(notification + '\n')
                    subject.execute = execute

    def test_create_reservation_failures_report_events_reason_status_and_redact(self) -> None:
        subject = self.deployment()
        prepared = subject.prepare()
        notification = prepared.parameters['NotificationEmail']
        execute = subject.execute
        for operation in ('create-change-set', 'wait'):
            with self.subTest(operation=operation):
                failed = False
                def fail(arguments: Sequence[str], *, capture_output: bool, text: bool, timeout: float,
                         check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
                    nonlocal failed
                    if failed and 'describe-stacks' in arguments:
                        return subprocess.CompletedProcess(arguments, 0, json.dumps({'Stacks': [{'StackStatus': 'CREATE_FAILED'}]}), '')
                    if operation == 'create-change-set' and operation in arguments:
                        failed = True
                        return subprocess.CompletedProcess(arguments, 1, '', 'reservation refused ' + notification)
                    result = execute(arguments, capture_output=capture_output, text=text, timeout=timeout, check=check, env=env)
                    if 'describe-change-set' in arguments and result.returncode == 0:
                        reply = ledger.mapping(cast(object, json.loads(result.stdout)))
                        reply['StatusReason'] = 'reservation reason ' + notification
                        return subprocess.CompletedProcess(arguments, 0, json.dumps(reply), '')
                    if operation == 'wait' and 'wait' in arguments:
                        failed = True
                        return subprocess.CompletedProcess(arguments, 1, '', 'reservation refused ' + notification)
                    return result
                _ = (self.root / 'describe-stack-events.json').write_text(json.dumps({'StackEvents': [{
                    'Timestamp': datetime.fromtimestamp(self.now, UTC).isoformat(), 'LogicalResourceId': 'Foundation',
                    'ResourceType': 'AWS::CloudFormation::Stack', 'ResourceStatus': 'CREATE_FAILED',
                    'ResourceStatusReason': 'event reason ' + notification}]}))
                subject.execute = fail
                try:
                    with self.assertRaises(aws_target.AwsUnavailable) as failure:
                        _ = subject.apply(approve_resources=True)
                    reason = str(failure.exception)
                    self.assertIn('reservation refused <notification address>', reason)
                    self.assertIn('Foundation (AWS::CloudFormation::Stack): CREATE_FAILED: event reason <notification address>', reason)
                    self.assertIn('stack status: CREATE_FAILED', reason)
                    self.assertNotIn(notification, reason)
                    if operation == 'wait':
                        self.assertIn('reservation reason <notification address>', reason)
                        self.assertFalse((self.root / 'change-set.json').exists())
                    events = cast(list[str], self.commands('describe-stack-events')[-1]['args'])
                    expected = 'fixture-build' if operation == 'create-change-set' else ledger.text(
                        aws_config.deployment_record(self.state), 'stack_id')
                    self.assertEqual(events[events.index('--stack-name') + 1], expected)
                finally:
                    subject.execute = execute

    def test_terminal_update_failure_reconciles_compute_but_uncertain_failure_keeps_it_pending(self) -> None:
        subject = self.update_fixture()
        execute = subject.execute
        for status, timeout in (('UPDATE_COMPLETE', False), ('UPDATE_ROLLBACK_COMPLETE', False),
                                ('UPDATE_IN_PROGRESS', False), ('UPDATE_ROLLBACK_FAILED', False), ('UPDATE_COMPLETE', True)):
            with self.subTest(status=status, timeout=timeout):
                path = self.root / 'fake-stack.json'
                row = remote.read_record(path)
                row.update(status='UPDATE_COMPLETE', instance_state='stopped')
                _ = path.write_text(json.dumps(row))
                _ = subject.prepare()
                def fail(arguments: Sequence[str], *, capture_output: bool, text: bool, timeout: float,
                         check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
                    if 'execute-change-set' in arguments:
                        row['status'] = status
                        _ = path.write_text(json.dumps(row))
                        if uncertain_timeout:
                            raise subprocess.TimeoutExpired(arguments, timeout)
                        return subprocess.CompletedProcess(arguments, 1, '', 'execution failed')
                    return execute(arguments, capture_output=capture_output, text=text, timeout=timeout, check=check, env=env)
                uncertain_timeout = timeout
                subject.execute = fail
                try:
                    with self.assertRaises(aws_target.AwsUnavailable):
                        _ = subject.apply(approve_resources=True)
                    requests = ledger.CostLedger(self.state).pending_compute_requests()
                    self.assertEqual(bool(requests), timeout or not status.endswith('_COMPLETE'))
                finally:
                    subject.execute = execute
                    row['status'] = 'UPDATE_COMPLETE'
                    _ = path.write_text(json.dumps(row))
                    _ = subject.prepare()

    def test_empty_update_is_success_without_execution_or_compute_request(self) -> None:
        subject = self.update_fixture()
        _ = (self.root / 'update-plan.json').write_text(json.dumps({
            'Status': 'FAILED', 'StatusReason': "The submitted information didn't contain changes."}))
        (self.root / 'fail-wait-change-set-create-complete').touch()
        before = len(self.commands('execute-change-set'))
        outputs = subject.apply(approve_resources=True)
        self.assertEqual(outputs['InstanceId'], 'i-fixture')
        self.assertEqual(len(self.commands('execute-change-set')), before)
        self.assertFalse(ledger.CostLedger(self.state).pending_compute_requests())
        self.assertTrue(self.commands('delete-change-set'))

    def test_update_failure_reports_current_events_and_rollback_without_the_address(self) -> None:
        subject = self.update_fixture()
        (self.root / 'fail-wait-stack-update-complete').touch()
        notification = self.notification_file.read_text().strip()
        _ = (self.root / 'describe-stack-events.json').write_text(json.dumps({'StackEvents': [
            {'Timestamp': datetime.fromtimestamp(self.now - 1, UTC).isoformat(), 'ResourceStatus': 'UPDATE_FAILED',
             'LogicalResourceId': 'OldFailure', 'ResourceType': 'AWS::EC2::Instance', 'ResourceStatusReason': 'old'},
            *[{'Timestamp': datetime.fromtimestamp(self.now, UTC).isoformat(), 'ResourceStatus': 'UPDATE_FAILED',
               'LogicalResourceId': name, 'ResourceType': kind, 'ResourceStatusReason': 'rejected ' + notification}
              for name, kind in (('DataAttachment', 'AWS::EC2::VolumeAttachment'), ('Instance', 'AWS::EC2::Instance'))]]}))
        execute = subject.execute
        def failure(arguments: Sequence[str], *, capture_output: bool, text: bool, timeout: float,
                    check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
            result = execute(arguments, capture_output=capture_output, text=text, timeout=timeout, check=check, env=env)
            if 'execute-change-set' in arguments:
                path = self.root / 'fake-stack.json'
                row = remote.read_record(path)
                row['status'] = 'UPDATE_ROLLBACK_COMPLETE'
                _ = path.write_text(json.dumps(row))
            return result
        subject.execute = failure
        saved = remote.read_record(self.state / 'deployment.json')
        _ = saved.pop('fingerprint')
        _ = (self.state / 'deployment.json').write_text(json.dumps(saved))
        success, output = self.up_output(subject)
        self.assertFalse(success, output)
        self.assertNotIn(notification, output)
        self.assertNotIn('OldFailure', output)
        for name, kind in (('DataAttachment', 'AWS::EC2::VolumeAttachment'), ('Instance', 'AWS::EC2::Instance')):
            self.assertIn(f'{name} ({kind}): UPDATE_FAILED: rejected <notification address>', output)
        self.assertIn('stack status: UPDATE_ROLLBACK_COMPLETE', output)
        self.assertIn('the stack rolled back; the box and the volume did not change', output)
        reason = ledger.text(ledger.mapping(remote.read_record(self.state / 'up.json')['stacks']), 'reason')
        self.assertIn('<notification address>', reason)
        self.assertNotIn(notification, reason)

    def test_change_set_creation_and_execution_failures_report_the_cause(self) -> None:
        subject = self.update_fixture()
        notification = self.notification_file.read_text().strip()
        for operation in ('create-change-set', 'execute-change-set'):
            with self.subTest(operation=operation):
                execute = subject.execute
                def fail(arguments: Sequence[str], *, capture_output: bool, text: bool, timeout: float,
                         check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
                    result = execute(arguments, capture_output=capture_output, text=text, timeout=timeout, check=check, env=env)
                    if operation in arguments:
                        return subprocess.CompletedProcess(arguments, 1, '', 'operation refused ' + notification)
                    return result
                _ = (self.root / 'update-plan.json').write_text(json.dumps({'StatusReason': 'change set refused ' + notification}))
                subject.execute = fail
                try:
                    with self.assertRaises(aws_target.AwsUnavailable) as failure:
                        _ = subject.apply(approve_resources=True)
                    reason = str(failure.exception)
                    self.assertIn('operation refused <notification address>', reason)
                    self.assertIn('change set refused <notification address>', reason)
                    self.assertIn('stack status: UPDATE_COMPLETE', reason)
                    self.assertNotIn(notification, reason)
                finally:
                    subject.execute = execute
                # Reconcile any execution request before the next case.
                _ = subject.prepare()

    def test_pending_request_reconciles_rollback_before_update_and_refuses_rollback_failed(self) -> None:
        subject = self.update_fixture()
        book = ledger.CostLedger(self.state)
        request = ledger.ComputeRequest(self.now, 'fixture-build', 'us-east-1', Decimal('0.5'))
        book.record_compute_request(request)
        path = self.root / 'fake-stack.json'
        row = remote.read_record(path)
        row['status'] = 'UPDATE_ROLLBACK_COMPLETE'
        _ = path.write_text(json.dumps(row))
        _ = subject.prepare()
        _ = subject.apply(approve_resources=True)
        resolved = next(item for item in book.read_snapshot().compute_requests if item.request_id == request.request_id)
        self.assertIsInstance(resolved.resolution, ledger.ComputeInstanceConfirmed)
        self.assertFalse(book.pending_compute_requests())
        row = remote.read_record(path)
        row['status'] = 'UPDATE_ROLLBACK_FAILED'
        _ = path.write_text(json.dumps(row))
        book.record_compute_request(ledger.ComputeRequest(self.now, 'fixture-build', 'us-east-1', Decimal('0.5')))
        before = len(self.commands('create-change-set'))
        with self.assertRaisesRegex(deploy.DeploymentRefused, 'UPDATE_ROLLBACK_FAILED'):
            _ = subject.prepare()
        self.assertEqual(len(self.commands('create-change-set')), before)

    def test_parameters_choose_only_an_available_saved_image(self) -> None:
        subject = self.deployment()
        self.assertEqual(subject.prepare().parameters['AmiId'], 'ami-fixture')
        _ = (self.state / 'image.json').write_text(json.dumps({'image_id': 'ami-cache', 'system': SYSTEM}))
        self.assertEqual(subject.prepare().parameters['AmiId'], 'ami-cache')
        with mock.patch.object(box, 'saved_image', return_value={}):
            self.assertEqual(subject.prepare().parameters['AmiId'], 'ami-fixture')
        with mock.patch.object(aws_target.AwsCli, 'call', side_effect=aws_target.AwsUnavailable('InvalidAMIID.NotFound')):
            self.assertEqual(box.saved_image(subject._api(aws_config.read_deployment_settings(self.settings, self.state)), self.state), {})  # pyright: ignore[reportPrivateUsage]

    def test_image_stage_runs_between_checks_and_idle_stop(self) -> None:
        stages = [stage.name for stage in self.deployment()._box_stages()]  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(stages[stages.index('checks'):stages.index('idle-stop') + 1], ['checks', 'image', 'idle-stop'])

    def test_update_preserves_ami_only_with_a_live_instance(self) -> None:
        subject = self.update_fixture()
        prepared = subject.prepare()
        for live in ({'InstanceId': 'i-fixture'}, {}):
            with mock.patch.object(subject, '_stack', return_value=live):
                subject._update_stack(aws_config.read_deployment_settings(self.settings, self.state), prepared.parameters, compute=False)  # pyright: ignore[reportPrivateUsage]
            args = cast(list[str], self.commands('create-change-set')[-1]['args'])
            values = ledger.rows({'values': cast(object, json.loads(args[args.index('--parameters') + 1]))}, 'values')
            ami = next(row for row in values if row['ParameterKey'] == 'AmiId')
            self.assertEqual(ami, {'ParameterKey': 'AmiId', **({'UsePreviousValue': True} if live else {'ParameterValue': 'ami-fixture'})})

    def test_down_keeps_and_reports_the_saved_image(self) -> None:
        _ = self.removal_fixture()
        path = self.state / 'image.json'
        _ = path.write_text(json.dumps({'image_id': 'ami-cache', 'system': SYSTEM, 'idle_stop_system': SYSTEM}))
        before = path.read_bytes()
        gate_directory = self.state / 'gate-image'
        gate_directory.mkdir()
        _ = (gate_directory / 'image.json').write_text('{"image_id":"ami-gate"}')
        with mock.patch.object(box, 'cleanup_images') as cleanup:
            success, output = self.down_output()
        self.assertEqual(cleanup.call_count, 1)
        self.assertEqual(cast(tuple[object, ...], cleanup.call_args.args)[1:], ({}, gate_box.GATE_IMAGE_OWNER_TAG))
        self.assertFalse(gate_directory.exists())
        self.assertTrue(success, output)
        self.assertIn('down: image kept: ami-cache; next up starts from it', output)
        self.assertEqual(path.read_bytes(), before)

    def test_new_stack_gets_current_bootstrap(self) -> None:
        subject = self.deployment()
        prepared = subject.prepare()
        _ = subject.apply(approve_resources=True)
        args = cast(list[str], self.commands('deploy')[0]['args'])
        self.assertIn('Bootstrap=' + prepared.parameters['Bootstrap'], args)
        self.assertIn('CREATE', cast(list[str], self.commands('create-change-set')[0]['args']))

    def test_recorded_rollback_reconciles_the_real_request_and_finishes_stacks(self) -> None:
        source = Path(__file__).parent / 'test_data/stack_update'
        for name in ('deployment.json', 'cost-ledger.json', 'up.json'):
            _ = shutil.copyfile(source / name, self.state / name)
        saved = remote.read_record(self.state / 'deployment.json')
        outputs = ledger.mapping(saved['outputs'])
        self.replace('stack_name', 'aws-build')
        self.replace('region', 'us-east-2')
        self.replace('availability_zone', 'us-east-2a')
        _ = (self.root / 'describe-subnets.json').write_text(json.dumps({'Subnets': [
            {'VpcId': 'vpc-fixture', 'AvailabilityZone': 'us-east-2a'}]}))
        self.replace('cost_explorer_tag_value', 'aws-build')
        book = ledger.CostLedger(self.state)
        evidence = book.read_snapshot()
        self.set_clock(max(row.at for row in evidence.observations) + 1)
        self.write_price()
        _ = (self.root / 'reserved-stack').write_text(ledger.text(saved, 'stack_id'))
        _ = (self.root / 'fake-stack.json').write_text(json.dumps({
            'outputs': outputs, 'stack_name': 'aws-build', 'stack_id': saved['stack_id'], 'status': 'UPDATE_ROLLBACK_COMPLETE'}))
        _ = (self.root / 'get-caller-identity.json').write_text(json.dumps({'Account': saved['account_id']}))
        _ = (self.root / 'get-user.json').write_text(json.dumps({'User': {
            'Arn': ledger.mapping(saved['scaler_user'])['arn'], 'UserId': ledger.mapping(saved['scaler_user'])['user_id']}}))
        _ = (self.root / 'describe-instances.json').write_text(json.dumps({'Reservations': [{'Instances': [{
            'InstanceId': outputs['InstanceId'], 'LaunchTime': 'recorded-boot', 'State': {'Name': 'stopped'}}]}]}))
        subject = self.deployment()
        subject._apply_stacks()  # pyright: ignore[reportPrivateUsage]
        request = next(item for item in book.read_snapshot().compute_requests
                       if item.request_id == 'b91c2c8572ba4e6f802bdaac9eee0227')
        self.assertIsInstance(request.resolution, ledger.ComputeInstanceConfirmed)
        if isinstance(request.resolution, ledger.ComputeInstanceConfirmed):
            self.assertEqual(request.resolution.instance_id, 'i-0216cf933a26bd1ea')
        self.assertEqual(aws_config.deployment_outputs(self.state)['DataVolumeId'], 'vol-067914014134c5036')
        self.assertFalse(book.pending_compute_requests())
        self.assertTrue(self.commands('execute-change-set'))

    def test_compute_command_starts_with_durable_unconfirmed_accounting(self) -> None:
        start = self.prepare_stopped_instance()
        _ = self.deployment().apply(approve_resources=True)
        compute = self.commands('deploy')[-1]
        requests = ledger.rows(compute, 'compute_requests')
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]['at'], start)
        self.assertEqual(requests[0]['stack'], conf.read(self.settings).text('stack_name'))
        self.assertEqual(requests[0]['region'], conf.read(self.settings).text('region'))
        self.assertEqual(ledger.mapping(requests[0]['resolution'])['outcome'], 'unconfirmed')

    def test_compute_timeout_keeps_exact_instance_costs_until_preparation_recovers(self) -> None:
        self.assert_compute_failure_accounting('timeout', 'prepare')

    def test_stack_lookup_failure_keeps_exact_instance_costs_until_apply_recovers(self) -> None:
        self.assert_compute_failure_accounting('stack', 'apply')

    def test_instance_lookup_failure_saves_identity_and_costs_until_setup_recovers(self) -> None:
        self.assert_compute_failure_accounting('instance', 'setup')

    def test_old_terminated_instance_cannot_end_a_replacement_request(self) -> None:
        start = self.prepare_stopped_instance()
        subject = self.deployment()
        outputs = subject.apply(approve_resources=True)
        (self.root / 'fake-stack.json').unlink()
        _ = (self.state / 'down-complete.json').write_text((self.state / 'deployment.json').read_text())
        self.replace('stack_name', 'replacement-build')
        self.replace('existing_data_volume_id', outputs['DataVolumeId'])
        _ = subject.prepare()
        with self.assertRaisesRegex(aws_target.AwsUnavailable, 'timed out'):
            _ = self.failing_compute('timeout').apply(approve_resources=True)
        book = ledger.CostLedger(self.state)
        book.observe(ledger.InstanceObservation(start + 60, 'terminated', 'old instance deletion'))
        self.assertEqual(self.instance_intervals(start + 3600), [(start, start + 3600, Decimal('1'), 0)])
        self.set_clock(start + 3600)
        self.write_price()
        _ = (self.root / 'describe-instances.json').write_text(json.dumps({
            'Reservations': [{'Instances': [{'InstanceId': outputs['InstanceId'], 'LaunchTime': 'old-boot',
                                            'State': {'Name': 'terminated'}}]}]}))
        with self.assertRaisesRegex(deploy.DeploymentRefused, 'previous instance'):
            _ = subject.prepare()
        self.assertEqual(len(book.pending_compute_requests()), 1)
        self.assertEqual(self.instance_intervals(self.now), [(start, self.now, Decimal('1'), 0)])
        stack_path = self.root / 'fake-stack.json'
        stack = remote.read_record(stack_path)
        ledger.mapping(stack['outputs'])['InstanceId'] = 'i-new'
        _ = stack_path.write_text(json.dumps(stack))
        _ = (self.root / 'describe-instances.json').write_text(json.dumps({
            'Reservations': [{'Instances': [{'InstanceId': 'i-new', 'LaunchTime': 'replacement-boot',
                                            'State': {'Name': 'running'}}]}]}))
        self.set_clock(start + 7200)
        self.write_price()
        _ = subject.prepare()
        self.assertEqual(ledger.mapping(remote.read_record(self.state / 'deployment.json')['outputs'])['InstanceId'], 'i-new')
        self.assertEqual(book.pending_compute_requests(), ())
        self.assertEqual(self.instance_intervals(self.now + 3600),
                         [(self.now, self.now + 3600, Decimal('0.1'), 3600), (start, self.now, Decimal('2'), 0)])

    def test_confirmed_absence_ends_only_the_unconfirmed_request_interval(self) -> None:
        start = self.prepare_stopped_instance()
        with self.assertRaisesRegex(aws_target.AwsUnavailable, 'timed out'):
            _ = self.failing_compute('timeout').apply(approve_resources=True)
        (self.root / 'fake-stack.json').unlink()
        self.set_clock(start + 3600)
        self.write_price()
        _ = self.deployment().prepare()
        self.assertFalse(ledger.CostLedger(self.state).pending_compute_requests())
        self.assertEqual(self.instance_intervals(self.now + 3600), [(start, self.now, Decimal('1'), 0)])

    def test_compute_request_preserves_conflict_checks_for_confirmed_observations(self) -> None:
        start = self.prepare_stopped_instance()
        book = ledger.CostLedger(self.state)
        book.record_compute_request(ledger.ComputeRequest(start, 'fixture-build', 'us-east-1', Decimal('0.5')))
        with self.assertRaisesRegex(ledger.LedgerUnavailable, 'conflicting instance observations'):
            book.observe(ledger.InstanceObservation(start, 'running', 'contradictory confirmed state'))
        self.assertEqual(book.read_snapshot().observations[-1].state, 'stopped')
        self.assertEqual(self.instance_intervals(start + 3600), [(start, start + 3600, Decimal('1'), 0)])

    def prepare_running_costs(self) -> tuple[int, ledger.CostLedger]:
        start = int(datetime(2026, 1, 15, tzinfo=UTC).timestamp())
        self.replace('daily_limit_usd', '20')
        self.costs({'fallback_usd_per_hour': '0.10', 'fixed_usd_per_month': '300',
                    'price_max_age_seconds': '86400', 'observation_max_age_seconds': '86400',
                    'cost_explorer_read_usd': '0'})
        book = ledger.CostLedger(self.state)
        book.record_inventory(ledger.ResourceInventory(start, (ledger.PricedResource('storage', Decimal('300')),),
                                                        'retained storage'))
        book.record_price(ledger.PriceSample(start, Decimal('0.483'), 'fresh Spot price'))
        book.observe(ledger.InstanceObservation(start, 'running', 'confirmed running instance'))
        self.set_clock(start + 23 * 3600)
        return start, book

    def test_unconfirmed_request_adds_costs_without_reversing_a_limit_refusal(self) -> None:
        start, book = self.prepare_running_costs()
        before = book.guard(self.settings, 'deployment', self.now, notify=lambda _event: True)
        self.assertIsInstance(before, ledger.SpendingRefused)
        self.assertIsInstance(before.evidence.costs, ledger.CostEstimate)
        if not isinstance(before.evidence.costs, ledger.CostEstimate):
            self.fail('the guard must price the running instance')
        book.record_compute_request(ledger.ComputeRequest(start, 'fixture-build', 'us-east-1', Decimal('0.5')))
        after = book.guard(self.settings, 'deployment', self.now, notify=lambda _event: True)
        self.assertIsInstance(after, ledger.SpendingRefused)
        self.assertIsInstance(after.evidence.costs, ledger.CostEstimate)
        if not isinstance(after.evidence.costs, ledger.CostEstimate):
            self.fail('the guard must price the running instance and request')
        request_usd = Decimal('0.5') * Decimal(23)
        self.assertGreaterEqual(after.evidence.costs.daily.usd, before.evidence.costs.daily.usd + request_usd)
        self.assertGreaterEqual(after.evidence.costs.monthly.usd, before.evidence.costs.monthly.usd + request_usd)
        self.assertEqual([r for r in after.evidence.costs.intervals if r.source != 'unconfirmed compute request'],
                         list(before.evidence.costs.intervals))

    def test_unconfirmed_request_keeps_confirmed_running_uptime(self) -> None:
        start, book = self.prepare_running_costs()
        before = self.instance_intervals(self.now)
        book.record_compute_request(ledger.ComputeRequest(start, 'fixture-build', 'us-east-1', Decimal('0.5')))
        after = self.instance_intervals(self.now)
        self.assertEqual(sum(r[3] for r in before), 23 * 3600)
        self.assertEqual(sum(r[3] for r in after), sum(r[3] for r in before))
        self.assertEqual(after, [*before, (start, self.now, Decimal('11.5'), 0)])

    def test_request_price_keeps_the_submitted_cap_after_live_cap_changes(self) -> None:
        start = self.prepare_stopped_instance()
        self.replace('fallback_usd_per_hour', '0.10')
        _ = self.deployment().prepare()
        with self.assertRaisesRegex(aws_target.AwsUnavailable, 'timed out'):
            _ = self.failing_compute('timeout').apply(approve_resources=True)
        compute = self.commands('deploy')[-1]
        arguments = cast(list[str], compute['args'])
        submitted_cap = Decimal(next(word.partition('=')[2] for word in arguments if word.startswith('SpotMaxPrice=')))
        self.assertEqual(submitted_cap, Decimal('0.5'))
        self.assertEqual(ledger.CostLedger(self.state).pending_compute_requests()[0].spot_max_usd_per_hour, submitted_cap)
        self.set_clock(start + 25 * 3600)
        midnight = int(datetime(2026, 1, 16, tzinfo=UTC).timestamp())
        for live_cap in ('0.05', '2'):
            with self.subTest(live_cap=live_cap):
                self.replace('spot_max_usd_per_hour', live_cap)
                costs = ledger.costs_from_evidence(ledger.CostLedger(self.state).read_snapshot(),
                                                   aws_config.read_settings(self.settings), self.now,
                                                   current_window=False)
                requests = [r for r in costs.intervals if r.source == 'unconfirmed compute request']
                self.assertEqual([(r.start, r.end, r.usd, r.confirmed_uptime_s) for r in requests],
                                 [(start, midnight, Decimal('6'), 0), (midnight, self.now, Decimal('6.5'), 0)])

    def up_output(self, subject: deploy.Deployment, *, include_box: bool = False) -> tuple[bool, str]:
        output = StringIO()
        with redirect_stdout(output):
            if include_box:
                success = subject.up(approve_resources=True)
            else:
                # Resource tests exclude box stages unless the test supplies box fakes.
                with mock.patch.object(deploy.Deployment, '_box_stages', return_value=()):
                    success = subject.up(approve_resources=True)
        return success, output.getvalue()

    def test_up_refuses_bad_policy_devices_before_price_or_admin_access(self) -> None:
        status = {'Self': {'HostName': 'natedev', 'DNSName': 'natedev.tail21e383.ts.net.', 'TailscaleIPs': ['100.64.0.1']}, 'Peer': {}}
        self.fake_commands(f'#!{sys.executable}\nprint({json.dumps(status)!r})\n', 'tailscale')
        with mock.patch.object(deploy.Deployment, '_price_present', side_effect=AssertionError('price must not run')) as price, \
             mock.patch.object(admin_secrets, 'read_hidden', side_effect=AssertionError('no passphrase')) as prompt, \
             mock.patch.object(box, 'tailnet_request', side_effect=AssertionError('no API')) as tailnet, \
             mock.patch.object(admin_secrets.AdminSecretSession, 'get', side_effect=AssertionError('no admin access')) as credentials:
            success, output = self.up_output(self.deployment())
        self.assertFalse(success)
        self.assertIn('nates-macbook-pro-2023: no match', output)
        self.assertIn('machine names present: natedev', output)
        price.assert_not_called()
        prompt.assert_not_called()
        tailnet.assert_not_called()
        credentials.assert_not_called()
        self.assertEqual(self.commands(), [])
        result = ledger.mapping(remote.read_record(self.state / 'up.json')['up'])
        self.assertEqual(result['status'], 'refused')
        self.assertEqual(output, 'up: refused: ' + str(result['reason']) + '\n')

    def test_up_creates_each_stage_and_the_second_run_only_checks_live_state(self) -> None:
        _ = self.credentials.write_text('[unrelated]\naws_access_key_id=other-key\naws_secret_access_key=other-value\n')
        self.price_file.unlink()
        settings = self.settings.read_bytes()
        success, output = self.up_output(self.deployment())
        self.assertTrue(success, output)
        self.assertEqual(output.splitlines(), ['price: done', 'bootstrap: done', 'stacks: done', 'scaler-key: done'])
        commands = self.commands()
        operations = [cast(list[str], row['args'])[5] for row in commands]
        self.assertLess(operations.index('describe-spot-price-history'), operations.index('deploy'))
        self.assertLess(operations.index('execute-change-set'), operations.index('create-access-key'))
        self.assertEqual(self.credentials.stat().st_mode & 0o777, 0o600)
        self.assertIn('other-key', self.credentials.read_text())
        self.assertIn('[build-scaler]', self.credentials.read_text())
        self.assertEqual(json.loads((self.root / 'fake-keys.json').read_text()), ['new-key'])
        before = len(self.commands())
        success, output = self.up_output(self.deployment())
        self.assertTrue(success, output)
        self.assertEqual(output.splitlines(), ['price: present', 'bootstrap: present', 'stacks: present', 'scaler-key: present'])
        later = self.commands()[before:]
        self.assertEqual(sum('describe-spot-price-history' in cast(list[str], row['args']) for row in later), 1)
        for row in later:
            self.assertFalse({'deploy', 'create-access-key', 'delete-access-key', 'list-access-keys'} & set(cast(list[str], row['args'])))
        self.assertEqual(self.settings.read_bytes(), settings)
        self.assertNotIn('fixture-secret-only-in-credentials', output)
        for file in self.state.rglob('*.json'):
            self.assertNotIn('fixture-secret-only-in-credentials', file.read_text())
        results = remote.read_record(self.state / 'up.json')
        self.assertEqual(set(results), {'price', 'bootstrap', 'stacks', 'scaler-key'})
        self.assertTrue(all(ledger.mapping(result)['status'] == 'present' for result in results.values()))

    def removal_fixture(self, *, save_node: bool = True) -> dict[str, object]:
        success, output = self.up_output(self.deployment())
        self.assertTrue(success, output)
        self.fake_commands(f'#!{sys.executable}\n' + r'''import json, os, pathlib, sys
root = pathlib.Path(os.environ['DEPLOY_FIXTURE'])
p = root / 'controller-enabled'
args = sys.argv[1:]
with (root / 'systemctl-commands').open('a') as log: log.write(' '.join(args) + '\n')
if 'disable' in args:
    if (root / 'fail-service').exists(): sys.exit(1)
    p.unlink(missing_ok=True)
elif 'enable' in args: p.touch()
elif 'is-enabled' in args: print('enabled' if p.exists() else 'disabled')
elif 'is-active' in args: print('active' if p.exists() else 'inactive')
else: raise AssertionError(args)
''', 'systemctl')
        (self.root / 'controller-enabled').touch()
        devices: dict[str, object] = {'devices': [{'id': 'device-box', 'hostname': 'aws-build', 'tags': ['tag:build-aws']},
                                                {'id': 'device-other', 'hostname': 'other', 'tags': []}]}
        if save_node:
            saved = remote.read_record(self.state / 'deployment.json')
            saved.update(tailscale_node_id='device-box', tailscale_instance_id='i-fixture')
            _ = (self.state / 'deployment.json').write_text(json.dumps(saved))
        policy = FakeTailnet()
        def tailnet(method: str, endpoint: str, body: bytes, headers: dict[str, str], timeout: float) -> box.PolicyReply:
            self.assertGreater(timeout, 0)
            self.assertIn('Content-Type' if endpoint == 'oauth/token' else 'Authorization', headers)
            self.assertTrue(body if method == 'POST' else not body)
            if (self.root / 'fail-tailnet').exists():
                raise box.BoxRefused('fixture device removal failure')
            if endpoint == 'oauth/token':
                return box.PolicyReply({'access_token': 'fixture-token'})
            if method == 'GET' and endpoint == 'tailnet/-/devices':
                return box.PolicyReply(devices)
            if method == 'DELETE' and endpoint.startswith('device/'):
                device_id = endpoint.removeprefix('device/')
                self.assertTrue(any(row['id'] == device_id for row in ledger.rows(devices, 'devices')))
                devices['devices'] = [row for row in ledger.rows(devices, 'devices') if row['id'] != device_id]
                return box.PolicyReply({})
            if '/acl' in endpoint or '/keys' in endpoint:
                return policy(method, endpoint, body, headers, timeout)
            raise AssertionError((method, endpoint))
        _ = cast(object, self.enterContext(mock.patch.object(box, 'tailnet_request', side_effect=tailnet)))
        _ = cast(object, self.enterContext(mock.patch.object(mac_test, 'state_paths', return_value=mac_test.StatePaths(self.state, 'aws'))))
        return devices

    def join_fixture(self, devices: dict[str, object], arguments: Sequence[str], key: str,
                     timeout: float, *, env: dict[str, str]) -> None:
        box.join_session(arguments, key, timeout, env=env)
        # Register the node after the join succeeds, using the node ID returned by the SSM fake.
        existing = ledger.rows(devices, 'devices')
        if any(row.get('nodeId', row.get('id')) == 'node-box' for row in existing):
            return
        name = 'aws-build-1' if any(row['hostname'] == 'aws-build' for row in existing) else 'aws-build'
        devices['devices'] = [*existing, {'id': 'new-device', 'nodeId': 'node-box',
                                         'hostname': name, 'tags': ['tag:build-aws']}]

    def start_box_after_tick(self, start: Callable[[], object]) -> object:
        self.set_clock(self.now + 1)
        return start()

    def down_output(self) -> tuple[bool, str]:
        output = StringIO()
        with redirect_stdout(output):
            success = self.deployment().down(approve_destroy=True)
        return success, output.getvalue()

    def test_down_missing_node_refuses_without_changes_then_up_and_down_complete(self) -> None:
        devices = self.removal_fixture(save_node=False)
        ledger.rows(devices, 'devices')[0]['nodeId'] = 'node-box'
        self.set_clock(self.now + 1)
        fake = BOX_FAKE.replace('i-box', 'i-fixture').replace('volbox', 'volcreated')
        self.fake_commands(f'#!{sys.executable}\n' + fake, 'ssh', 'nix', 'git', 'tailscale', 'rustc', 'cargo', 'cargo-nextest', 'cargo-mend', 'bash')
        _ = (self.root / 'box-aws').write_text(fake.replace("name = pathlib.Path(sys.argv[0]).name", "name = 'aws'"))
        _ = (self.root / 'live.json').write_text(json.dumps({'online': True, 'joined': False, 'active': True,
            'system': SYSTEM, 'desired': SYSTEM, 'ec2': 'running', 'stop_after': 5}))
        for key, value in (('box_check_delay_s', '0.001'), ('idle_stop_minutes', '0.001'), ('idle_stop_margin_s', '0')):
            self.replace(key, value)
        # Advance the fixture clock so a self-stop follows the earlier running observation.
        def clock() -> int:
            self.set_clock(self.now + 1)
            return self.now
        subject = deploy.Deployment(self.settings, self.state, self.bundle, clock=clock, notify=lambda _event: True)
        original_setup = box.BoxSetup
        def setup(settings: Path, directory: Path, start: Callable[[], object], *, execute: aws_target.AwsExecutor,
                  admin: admin_secrets.AdminSecretSession) -> box.BoxSetup:
            monotonic = FakeMonotonicClock()
            return original_setup(settings, directory, partial(self.start_box_after_tick, start), execute=execute, admin=admin, tailnet=box.tailnet_request,
                                  session=partial(self.join_fixture, devices), monotonic=monotonic, pause=monotonic.advance)
        with mock.patch.dict(os.environ, {'BOX_FIXTURE': str(self.root), 'BUILDLOG_DIR': str(self.root / 'buildlog')}), \
             mock.patch.object(box, 'BoxSetup', side_effect=setup), \
             mock.patch.object(target_config, 'read_targets', return_value=(target_config.SshTarget('aws', 'linux', self.settings),)):
            with mock.patch.object(original_setup, 'join', side_effect=box.BoxRefused('fixture join failure')):
                success, output = self.up_output(subject, include_box=True)
            self.assertFalse(success, output)
            self.assertIn('box-join: refused: fixture join failure', output)
            self.assertNotIn('tailscale_node_id', remote.read_record(self.state / 'deployment.json'))
            before = {path: path.read_bytes() for path in self.state.rglob('*') if path.is_file()}
            commands = len(self.commands())
            with mock.patch.object(box.Tailnet, 'delete_device') as delete:
                success, output = self.down_output()
            self.assertFalse(success, output)
            self.assertIn('identity: refused: missing saved Tailscale node id; run deploy.py up', output)
            delete.assert_not_called()
            self.assertEqual({path: path.read_bytes() for path in self.state.rglob('*') if path.is_file()}, before)
            self.assertFalse((self.state / 'down.json').exists())
            self.assertFalse((self.root / 'systemctl-commands').exists())
            self.assertTrue((self.root / 'controller-enabled').exists())
            self.assertFalse(any(cast(list[str], row['args'])[5].startswith(('delete-', 'update-'))
                                 for row in self.commands()[commands:]))
            success, output = self.up_output(subject, include_box=True)
            self.assertTrue(success, output)
            self.assertIn('routing-proof: done', output)
            self.assertEqual(self.commands('stop-instances'), [])
            self.assertEqual(remote.read_record(self.state / 'deployment.json')['tailscale_node_id'], 'node-box')
            self.assertEqual([row['id'] for row in ledger.rows(devices, 'devices')], ['device-box', 'device-other'])
            success, output = self.down_output()
            self.assertTrue(success, output)
        self.assertEqual([row['id'] for row in ledger.rows(devices, 'devices')], ['device-other'])

    def test_down_never_joined_box_reports_no_device_without_deleting_one(self) -> None:
        devices = self.removal_fixture(save_node=False)
        devices['devices'] = [row for row in ledger.rows(devices, 'devices') if row['id'] == 'device-other']
        stages = remote.read_record(self.state / 'up.json')
        self.assertNotIn('box-join', stages)
        self.assertNotIn('tailscale_node_id', remote.read_record(self.state / 'deployment.json'))
        with mock.patch.object(box.Tailnet, 'delete_device') as delete:
            success, output = self.down_output()
        self.assertTrue(success, output)
        self.assertIn('tailscale-device: no device', output)
        delete.assert_not_called()
        self.assertEqual([row['id'] for row in ledger.rows(devices, 'devices')], ['device-other'])
        self.assertFalse((self.state / 'deployment.json').exists())

    def test_down_second_node_identity_check_refuses_without_a_journal(self) -> None:
        devices = self.removal_fixture(save_node=False)
        with mock.patch.object(box.Tailnet, 'call', side_effect=[box.PolicyReply({'devices': []}),
                                                               box.PolicyReply(devices)]) as check, \
             mock.patch.object(box.Tailnet, 'delete_device') as delete:
            success, output = self.down_output()
        self.assertFalse(success, output)
        self.assertIn('tailscale-device: refused: missing saved Tailscale node id; run deploy.py up', output)
        self.assertEqual(check.call_args_list, [mock.call('GET', 'devices'), mock.call('GET', 'devices')])
        delete.assert_not_called()
        self.assertFalse((self.state / 'down.json').exists())
        self.assertTrue((self.state / 'deployment.json').exists())
        self.assertEqual([row['id'] for row in ledger.rows(devices, 'devices')], ['device-box', 'device-other'])

    def test_down_checks_each_required_saved_identity_before_changes(self) -> None:
        _ = self.removal_fixture()
        path = self.state / 'deployment.json'
        original = path.read_text()
        fields = ('data_volume_id', 'budget_name', 'deployment_id', 'tailscale_instance_id',
                  'scaler_user', 'name', 'arn', 'user_id')
        for field in fields:
            with self.subTest(field=field):
                saved = ledger.mapping(cast(object, json.loads(original)))
                parent = ledger.mapping(saved['scaler_user']) if field in ('name', 'arn', 'user_id') else saved
                del parent[field]
                _ = path.write_text(json.dumps(saved))
                before = {file: file.read_bytes() for file in self.state.rglob('*') if file.is_file()}
                success, output = self.down_output()
                self.assertFalse(success, output)
                self.assertIn('identity: refused:', output)
                self.assertIn('run deploy.py up once to record it', output)
                self.assertEqual({file: file.read_bytes() for file in self.state.rglob('*') if file.is_file()}, before)
                self.assertFalse((self.state / 'down.json').exists())
                self.assertFalse((self.root / 'systemctl-commands').exists())
        _ = path.write_text(original)

    def test_down_dry_run_changes_nothing_and_needs_no_credentials(self) -> None:
        for action in (self.deployment().down, self.deployment().remove_builder):
            before = {path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()}
            with mock.patch.dict(os.environ, {'AWS_SHARED_CREDENTIALS_FILE': ''}), redirect_stdout(StringIO()) as output:
                self.assertTrue(action())
            self.assertIn('/home build cache is lost', output.getvalue())
            self.assertIn('admin.gpg, and the box image', output.getvalue())
            self.assertIn('--approve-destroy', output.getvalue())
            self.assertEqual({path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()}, before)

    def builder_removal_fixture(self) -> dict[str, object]:
        devices = self.removal_fixture()
        stack_path = self.root / 'fake-stack.json'
        stack = remote.read_record(stack_path)
        ledger.mapping(stack['outputs']).update(GateBucket='fixture-bucket', GateLaunchTemplate='fixture-template')
        ledger.mapping(stack['parameters']).update(EnableGates='true', GateImage='ami-gate', GateRootGiB='40')
        _ = stack_path.write_text(json.dumps(stack))
        template = remote.read_record(deploy.TEMPLATE)
        for name in ('InstanceRole', 'InstanceProfile', 'SecurityGroup'):
            _ = ledger.mapping(ledger.mapping(template['Resources'])[name]).pop('Condition')
        _ = (self.root / 'live-template.json').write_text(json.dumps(template))
        _ = (self.state / 'gate-deployment.json').write_text('{"bucket":"fixture-bucket","template":"fixture-template"}\n')
        self.set_clock(self.now + 1)
        book = ledger.CostLedger(self.state)
        book.record_inventory(ledger.ResourceInventory(self.now, book.read_snapshot().inventories[-1].resources + (
            ledger.PricedResource('gate-shared', Decimal(1)),), 'fixture gate deployment'))
        book.record_compute_request(ledger.ComputeRequest(self.now, 'fixture-build', 'us-east-1', Decimal('0.5')))
        self.set_clock(self.now + 1)
        self.replace('fixed_usd_per_month', '100')
        return devices

    def test_remove_builder_preserves_gates_and_removes_storage_before_resolving_costs(self) -> None:
        devices = self.builder_removal_fixture()
        settings, credentials = self.settings.read_bytes(), self.credentials.read_bytes()
        gate_manifest = (self.state / 'gate-deployment.json').read_bytes()
        template = remote.read_record(self.root / 'live-template.json')
        before_commands = len(self.commands())
        with redirect_stdout(StringIO()) as output:
            self.assertTrue(self.deployment().remove_builder(approve_destroy=True))
        self.assertIn('gate-deployment.json kept', output.getvalue())
        self.assertEqual(self.settings.read_bytes(), settings)
        self.assertEqual(self.credentials.read_bytes(), credentials)
        self.assertEqual((self.state / 'gate-deployment.json').read_bytes(), gate_manifest)
        self.assertEqual([row['id'] for row in ledger.rows(devices, 'devices')], ['device-other'])
        operations = [cast(list[str], row['args'])[5] for row in self.commands()[before_commands:]]
        self.assertLess(operations.index('execute-change-set'), operations.index('delete-volume'))
        self.assertFalse({'delete-stack', 'delete-budget', 'delete-access-key'} & set(operations))
        command = cast(list[str], self.commands('create-change-set')[-1]['args'])
        parameters = ledger.rows({'rows': cast(object, json.loads(command[command.index('--parameters') + 1]))}, 'rows')
        for row in parameters:
            if row['ParameterKey'] == 'EnableCompute':
                self.assertEqual(row['ParameterValue'], 'false')
            else:
                self.assertIs(row['UsePreviousValue'], True)
        after_template = remote.read_record(self.root / 'live-template.json')
        expected = ledger.mapping(template['Resources'])
        for name in ('InstanceRole', 'InstanceProfile', 'SecurityGroup'):
            ledger.mapping(expected[name])['Condition'] = 'Compute'
        self.assertEqual(after_template, template)
        self.assertEqual(after_template, remote.read_record(deploy.TEMPLATE))
        saved = aws_config.deployment_outputs(self.state)
        self.assertEqual(saved['GateBucket'], 'fixture-bucket')
        self.assertNotIn('InstanceId', saved)
        self.assertNotIn('DataVolumeId', saved)
        self.assertFalse((self.state / 'up.json').exists())
        book = ledger.CostLedger(self.state)
        state = book.read_snapshot()
        self.assertEqual(state.observations[-1].state, 'terminated')
        self.assertEqual(book.pending_compute_requests(), ())
        self.assertTrue(state.inventories[-1].builder_removed)
        self.assertEqual(state.inventories[-1].resources, (ledger.PricedResource('gate-shared', Decimal(1)),))
        costs = book.estimate(aws_config.read_settings(self.settings), self.now + 3600)
        later = [row for row in costs.intervals if row.start >= self.now]
        self.assertFalse(any(row.kind == 'instance' for row in later))
        self.assertEqual(sum((row.usd for row in later if row.kind == 'fixed'), Decimal(0)), Decimal(1) / Decimal(744))
        self.assertEqual(ledger.text(remote.read_record(self.state / 'builder-removal.json'), 'stack'), 'fixture-build')

    def test_remove_builder_preserves_a_live_gate_definition_that_differs_from_repository(self) -> None:
        _ = self.builder_removal_fixture()
        path = self.root / 'live-template.json'
        template = remote.read_record(path)
        resources = ledger.mapping(template['Resources'])
        gate = ledger.mapping(resources['GateExpiryFunction'])
        ledger.mapping(gate['Properties'])['Timeout'] = 17
        _ = path.write_text(json.dumps(template))
        with redirect_stdout(StringIO()):
            self.assertTrue(self.deployment().remove_builder(approve_destroy=True))
        command = cast(list[str], self.commands('create-change-set')[-1]['args'])
        submitted = remote.read_record(Path(command[command.index('--template-body') + 1].removeprefix('file://')))
        self.assertEqual(ledger.mapping(submitted['Resources'])['GateExpiryFunction'], gate)
        for name in ('InstanceRole', 'InstanceProfile', 'SecurityGroup'):
            ledger.mapping(resources[name])['Condition'] = 'Compute'
        self.assertEqual(submitted, template)

    def test_remove_builder_resumes_after_partial_removal_and_repeats_without_deletion(self) -> None:
        _ = self.builder_removal_fixture()
        saved = remote.read_record(self.state / 'deployment.json')
        stale = {**saved, 'data_volume_id': 'vol-old',
                 'outputs': {**ledger.mapping(saved['outputs']), 'InstanceId': 'i-old', 'DataVolumeId': 'vol-old'}}
        _ = (self.state / 'builder-removal.json').write_text(json.dumps(stale))
        manifest = (self.state / 'gate-deployment.json').read_bytes()
        executed_before = len(self.commands('execute-change-set'))
        for operation in ('execute-change-set', 'delete-volume'):
            with self.subTest(operation=operation):
                failure = self.root / ('fail-down-' + operation)
                failure.touch()
                with self.assertRaises(aws_target.AwsUnavailable), redirect_stdout(StringIO()):
                    _ = self.deployment().remove_builder(approve_destroy=True)
                failure.unlink()
                self.assertFalse(ledger.CostLedger(self.state).read_snapshot().inventories[-1].builder_removed)
        with (mock.patch.object(ledger.CostLedger, 'record_inventory', side_effect=OSError('fixture ledger failure')),
              self.assertRaisesRegex(OSError, 'fixture ledger failure'), redirect_stdout(StringIO())):
            _ = self.deployment().remove_builder(approve_destroy=True)
        with redirect_stdout(StringIO()):
            self.assertTrue(self.deployment().remove_builder(approve_destroy=True))
        self.assertEqual(len(self.commands('execute-change-set')), executed_before + 1)
        self.assertEqual(len(self.commands('delete-volume')), 1)
        receipt = remote.read_record(self.state / 'builder-removal.json')
        self.assertEqual(receipt['data_volume_id'], saved['data_volume_id'])
        self.assertEqual(ledger.mapping(receipt['outputs'])['InstanceId'], ledger.mapping(saved['outputs'])['InstanceId'])
        before = len(self.commands())
        with redirect_stdout(StringIO()):
            self.assertTrue(self.deployment().remove_builder(approve_destroy=True))
        operations = [cast(list[str], row['args'])[5] for row in self.commands()[before:]]
        self.assertFalse(any(operation.startswith(('delete-', 'execute-', 'create-', 'update-')) for operation in operations))
        self.assertEqual((self.state / 'gate-deployment.json').read_bytes(), manifest)
        self.assertFalse((self.root / 'deleted-stack').exists())
        self.assertFalse((self.root / 'deleted-budget').exists())
        success, output = self.down_output()
        self.assertTrue(success, output)
        self.assertFalse((self.root / 'fake-stack.json').exists())

    def test_down_removes_resources_and_preserves_spend_and_other_profiles(self) -> None:
        devices = self.removal_fixture()
        original = self.settings.read_bytes()
        before = ledger.CostLedger(self.state).read_snapshot()
        with self.credentials.open('a') as handle:
            _ = handle.write('\n[unrelated]\naws_access_key_id=other-id\naws_secret_access_key=other-value\n')
        (self.root / 'admin.gpg').touch()
        charges = self.state / 'charges/day.json'
        charges.parent.mkdir(exist_ok=True)
        _ = charges.write_text('retained charge evidence')
        with mock.patch.object(box, 'close_routing', wraps=box.close_routing) as close:
            success, output = self.down_output()
        close.assert_called_once_with(self.settings)
        self.assertTrue(success, output)
        self.assertIn('data volume; the /home build cache is lost', output)
        self.assertNotIn('down: image kept:', output)
        self.assertIn('retained Budget', output)
        self.assertEqual([row['hostname'] for row in ledger.rows(devices, 'devices')], ['other'])
        self.assertEqual(admin_secrets.read_profiles(self.credentials).sections(), ['unrelated'])
        self.assertEqual(self.credentials.stat().st_mode & 0o777, 0o600)
        self.assertEqual(charges.read_text(), 'retained charge evidence')
        self.assertEqual(self.settings.read_bytes(), original)
        self.assertFalse(remote.read_record(self.state / 'routing.json')['enabled'])
        after = ledger.CostLedger(self.state).read_snapshot()
        self.assertEqual(after.reconciliations, before.reconciliations)
        self.assertEqual(after.events, before.events)
        self.assertEqual(after.prices, before.prices)
        self.assertEqual(after.observations[-1].state, 'terminated')
        self.assertFalse((self.state / 'deployment.json').exists())
        self.assertFalse((self.state / 'up.json').exists())
        self.assertTrue((self.root / 'admin.gpg').exists())
        operations = [cast(list[str], row['args'])[5] for row in self.commands()]
        self.assertLess(operations.index('update-stack'), operations.index('delete-stack'))
        self.assertLess(operations.index('delete-stack'), operations.index('delete-volume'))
        before_commands = len(self.commands())
        before_service = (self.root / 'systemctl-commands').read_text()
        success, output = self.down_output()
        self.assertTrue(success, output)
        self.assertTrue(all(line.endswith(': present') for line in output.splitlines()), output)
        later = [cast(list[str], row['args'])[5] for row in self.commands()[before_commands:]]
        self.assertFalse(any(operation.startswith(('delete-', 'update-')) for operation in later))
        self.assertNotIn('disable', (self.root / 'systemctl-commands').read_text()[len(before_service):])
        self.assertNotIn('fixture-admin-secret', output)
        self.assertNotIn('fixture-secret-only-in-credentials', output)

    def test_down_resumes_after_each_removal_failure(self) -> None:
        devices = self.removal_fixture()
        self.assertIn('devices', devices)
        failures = (('fail-service', 'routing-off'), ('fail-tailnet', 'tailscale-device'),
                    ('fail-down-delete-access-key', 'scaler-keys'), ('fail-down-update-stack', 'compute'),
                    ('fail-down-delete-stack', 'foundation'), ('fail-down-delete-volume', 'retained-resources'),
                    ('fail-down-delete-budget', 'retained-resources'))
        for filename, stage in failures:
            with self.subTest(stage=stage, command=filename):
                (self.root / filename).touch()
                success, output = self.down_output()
                self.assertFalse(success, output)
                self.assertIn(stage + ': refused:', output)
                self.assertTrue((self.state / 'deployment.json').exists())
                self.assertFalse(remote.read_record(self.state / 'routing.json')['enabled'])
                (self.root / filename).unlink()
        success, output = self.down_output()
        self.assertTrue(success, output)
        self.assertEqual(len(self.commands('update-stack')), 1)
        self.assertEqual(len(self.commands('delete-stack')), 1)
        self.assertEqual(len(self.commands('delete-volume')), 1)

    def test_down_refuses_a_live_claim_after_disabling_routing(self) -> None:
        devices = self.removal_fixture()
        self.assertIn('devices', devices)
        before = len(self.commands())
        claim = write_aws_claim(self.state, kept=False)
        success, output = self.down_output()
        self.assertFalse(success)
        self.assertIn('AWS claims remain live', output)
        self.assertFalse(remote.read_record(self.state / 'routing.json')['enabled'])
        self.assertFalse((self.root / 'controller-enabled').exists())
        self.assertEqual([cast(list[str], row['args'])[5] for row in self.commands()[before:]],
                         ['get-caller-identity', 'describe-stacks'])
        self.assertTrue(claim.exists())
        claim.unlink()
        success, output = self.down_output()
        self.assertTrue(success, output)

    def test_down_preserves_a_kept_claim_and_completes(self) -> None:
        _ = self.removal_fixture()
        path = write_aws_claim(self.state, kept=True)
        original = path.read_bytes()
        with mock.patch.object(box, 'running_claims', wraps=box.running_claims) as claims:
            success, output = self.down_output()
        self.assertTrue(success, output)
        self.assertEqual(claims.call_count, 2)
        self.assertEqual(path.read_bytes(), original)

    def test_down_ignores_a_reservation_and_preserves_its_file(self) -> None:
        _ = self.removal_fixture()
        path = write_aws_claim(self.state, kept=False, remote=False)
        original = path.read_bytes()
        with mock.patch.object(box, 'running_claims', wraps=box.running_claims) as claims:
            success, output = self.down_output()
        self.assertTrue(success, output)
        self.assertEqual(claims.call_count, 2)
        self.assertEqual(path.read_bytes(), original)

    def test_down_refuses_a_running_claim_before_device_removal(self) -> None:
        devices = self.removal_fixture()
        read_claims = box.running_claims
        calls = 0
        def claims(paths: mac_test.StatePaths) -> tuple[mac_test.HeldClaim, ...]:
            nonlocal calls
            calls += 1
            if calls == 2:
                _ = write_aws_claim(self.state, kept=False)
            return read_claims(paths)
        with mock.patch.object(box, 'running_claims', side_effect=claims):
            success, output = self.down_output()
        self.assertFalse(success, output)
        self.assertIn('tailscale-device: refused: AWS claims remain live', output)
        self.assertEqual(calls, 2)
        self.assertEqual(len(ledger.rows(devices, 'devices')), 2)
        self.assertTrue((self.state / 'claims/job.json').exists())

    def test_up_after_down_recreates_foundation_compute_and_scaler_key(self) -> None:
        devices = self.removal_fixture()
        success, output = self.down_output()
        self.assertTrue(success, output)
        before = len(self.commands())
        fake = BOX_FAKE.replace('i-box', 'i-fixture').replace('volbox', 'volcreated')
        self.fake_commands(f'#!{sys.executable}\n' + fake, 'ssh', 'nix', 'git', 'tailscale', 'rustc', 'cargo', 'cargo-nextest', 'cargo-mend', 'bash')
        _ = (self.root / 'box-aws').write_text(fake.replace("name = pathlib.Path(sys.argv[0]).name", "name = 'aws'"))
        _ = (self.root / 'live.json').write_text(json.dumps({'online': True, 'joined': False, 'active': True,
            'system': '/nix/store/old', 'desired': SYSTEM, 'ec2': 'stopped'}))
        for key, value in (('box_check_delay_s', '0.001'), ('idle_stop_minutes', '0.001'), ('idle_stop_margin_s', '0')):
            self.replace(key, value)
        original_setup = box.BoxSetup
        def setup(settings: Path, directory: Path, start: Callable[[], object], *, execute: aws_target.AwsExecutor,
                  admin: admin_secrets.AdminSecretSession) -> box.BoxSetup:
            monotonic = FakeMonotonicClock()
            return original_setup(settings, directory, partial(self.start_box_after_tick, start), execute=execute, admin=admin, tailnet=box.tailnet_request,
                                  session=partial(self.join_fixture, devices), monotonic=monotonic, pause=monotonic.advance)
        with mock.patch.dict(os.environ, {'BOX_FIXTURE': str(self.root), 'BUILDLOG_DIR': str(self.root / 'buildlog')}), \
             mock.patch.object(box, 'BoxSetup', side_effect=setup), \
             mock.patch.object(target_config, 'read_targets', return_value=(target_config.SshTarget('aws', 'linux', self.settings),)):
            success, output = self.up_output(self.deployment(), include_box=True)
        self.assertTrue(success, output)
        results = remote.read_record(self.state / 'up.json')
        self.assertEqual(list(results), sorted(('price', 'bootstrap', 'stacks', 'scaler-key', 'tailscale-policy', 'box-join',
                                              'box-configuration', 'checks', 'image', 'idle-stop', 'activation', 'routing-proof')))
        self.assertTrue(all(ledger.mapping(row)['status'] == 'done' for name, row in results.items() if name != 'price'), output)
        later = [cast(list[str], row['args']) for row in self.commands()[before:]]
        self.assertEqual(sum('EnableCompute=false' in args for args in later), 1)
        self.assertEqual(sum('execute-change-set' in args for args in later), 1)
        self.assertEqual(sum('create-access-key' in args for args in later), 1)
        self.assertEqual([row['hostname'] for row in ledger.rows(devices, 'devices')], ['other', 'aws-build'])
        self.assertEqual(remote.read_record(self.state / 'deployment.json')['tailscale_node_id'], 'node-box')
        success, output = self.down_output()
        self.assertTrue(success, output)
        self.assertEqual([row['id'] for row in ledger.rows(devices, 'devices')], ['device-other'])

    def test_down_resumes_local_cleanup_without_removing_spending_history(self) -> None:
        devices = self.removal_fixture()
        self.assertEqual(len(ledger.rows(devices, 'devices')), 2)
        proof = self.state / 'box-tools.json'
        _ = proof.write_text('{}')
        unlink = Path.unlink
        def remove(path: Path, missing_ok: bool = False) -> None:
            if path == proof:
                raise OSError('fixture proof removal failure')
            unlink(path, missing_ok=missing_ok)
        with mock.patch.object(Path, 'unlink', new=remove):
            success, output = self.down_output()
        self.assertFalse(success, output)
        self.assertIn('local-state: refused: fixture proof removal failure', output)
        before = len(self.commands())
        with self.assertRaisesRegex(deploy.DeploymentRefused, 'removal is incomplete'):
            _ = self.deployment().up(approve_resources=True)
        success, output = self.down_output()
        self.assertTrue(success, output)
        self.assertFalse(any(cast(list[str], row['args'])[5].startswith(('delete-', 'update-')) for row in self.commands()[before:]))
        self.assertEqual(ledger.CostLedger(self.state).read_snapshot().observations[-1].state, 'terminated')

    def test_down_verifies_immutable_account_region_and_stack_before_any_change(self) -> None:
        devices = self.removal_fixture()
        self.assertIn('devices', devices)
        path = self.state / 'deployment.json'
        original = path.read_bytes()
        saved = remote.read_record(path)
        for field, value in (('account_id', '999999999999'), ('region', 'us-west-2'),
                             ('stack_id', 'arn:aws:cloudformation:us-east-1:123456789012:stack/fixture-build/other')):
            with self.subTest(field=field):
                _ = path.write_text(json.dumps({**saved, field: value}))
                files = {p: p.read_bytes() for p in self.state.rglob('*') if p.is_file()}
                before = len(self.commands())
                success, output = self.down_output()
                self.assertFalse(success, output)
                self.assertIn('identity: refused:', output)
                self.assertEqual({p: p.read_bytes() for p in self.state.rglob('*') if p.is_file()}, files)
                self.assertTrue((self.root / 'controller-enabled').exists())
                self.assertFalse(any(cast(list[str], r['args'])[5].startswith(('delete-', 'update-')) for r in self.commands()[before:]))
        path.unlink()
        success, output = self.down_output()
        self.assertFalse(success, output)
        self.assertIn('saved deployment identity', output)
        _ = path.write_bytes(original)
        success, output = self.down_output()
        self.assertTrue(success, output)
        mutations = [cast(list[str], r['args']) for r in self.commands() if cast(list[str], r['args'])[5] in ('update-stack', 'delete-stack')]
        self.assertTrue(mutations)
        self.assertTrue(all(args[args.index('--stack-name') + 1] == saved['stack_id'] for args in mutations))

    def test_down_deletes_only_the_saved_node_and_refuses_missing_node_identity(self) -> None:
        devices = self.removal_fixture()
        path = self.state / 'deployment.json'
        saved = remote.read_record(path)
        del saved['tailscale_node_id']
        _ = path.write_text(json.dumps(saved))
        success, output = self.down_output()
        self.assertFalse(success, output)
        self.assertIn('run deploy.py up once to record it', output)
        self.assertEqual([row['id'] for row in ledger.rows(devices, 'devices')], ['device-box', 'device-other'])
        devices['devices'] = [*ledger.rows(devices, 'devices'), {'id': 'other-box', 'hostname': 'aws-build', 'tags': ['tag:build-aws']}]
        ledger.rows(devices, 'devices')[0]['nodeId'] = 'node-confirmed'
        saved.update(tailscale_node_id='node-confirmed', tailscale_instance_id='i-fixture')
        _ = path.write_text(json.dumps(saved))
        success, output = self.down_output()
        self.assertTrue(success, output)
        self.assertEqual([row['id'] for row in ledger.rows(devices, 'devices')], ['device-other', 'other-box'])

    def test_down_refuses_a_replaced_scaler_user_before_deleting_keys(self) -> None:
        devices = self.removal_fixture()
        self.assertIn('devices', devices)
        self.replace('scaler_user_name', 'another-user')
        override = self.root / 'get-user.json'
        _ = override.write_text(json.dumps({'User': {'Arn': 'arn:aws:iam::123456789012:user/build-scaler', 'UserId': 'recreated-user'}}))
        before = len(self.commands('delete-access-key'))
        success, output = self.down_output()
        self.assertFalse(success, output)
        self.assertIn('scaler ARN or UserId differs', output)
        self.assertEqual(len(self.commands('delete-access-key')), before)
        override.unlink()
        success, output = self.down_output()
        self.assertTrue(success, output)
        args = cast(list[str], self.commands('delete-access-key')[-1]['args'])
        self.assertEqual(args[args.index('--user-name') + 1], 'build-scaler')

    def test_down_refuses_stale_journals_and_foreign_retained_volume_tags(self) -> None:
        devices = self.removal_fixture()
        self.assertIn('devices', devices)
        saved = remote.read_record(self.state / 'deployment.json')
        journal = self.state / 'down.json'
        _ = journal.write_text(json.dumps({**saved, 'deployment_id': '0' * 32}))
        success, output = self.down_output()
        self.assertFalse(success, output)
        self.assertIn('removal journal differs', output)
        self.assertTrue((self.root / 'controller-enabled').exists())
        journal.unlink()
        override = self.root / 'describe-volumes.json'
        _ = override.write_text(json.dumps({'Volumes': [{'VolumeId': 'vol-created', 'State': 'available', 'Attachments': [],
                                                         'Tags': [{'Key': 'DeploymentId', 'Value': 'another-deployment'},
                                                                  {'Key': 'aws:cloudformation:stack-id', 'Value': saved['stack_id']}]}]}))
        success, output = self.down_output()
        self.assertFalse(success, output)
        self.assertIn('deployment ownership tag', output)
        self.assertEqual(self.commands('delete-volume'), [])
        override.unlink()
        success, output = self.down_output()
        self.assertTrue(success, output)
        args = cast(list[str], self.commands('delete-budget')[-1]['args'])
        self.assertEqual(args[args.index('--budget-name') + 1], 'fixture-build-monthly-' + str(saved['deployment_id']))

    def test_legacy_up_preserves_the_volume_and_budget_then_down_uses_saved_identities(self) -> None:
        devices = self.removal_fixture()
        stack_path = self.root / 'fake-stack.json'
        stack = remote.read_record(stack_path)
        outputs = ledger.mapping(stack['outputs'])
        outputs['BudgetName'] = 'fixture-build-monthly'
        _ = stack_path.write_text(json.dumps(stack))
        path = self.state / 'deployment.json'
        _ = path.write_text(json.dumps({'stack': 'fixture-build', 'region': 'us-east-1', 'outputs': outputs}))
        (self.root / 'volume-owner').unlink()
        self.set_clock(self.now + 1)
        before = len(self.commands('deploy'))
        fake = BOX_FAKE.replace('i-box', 'i-fixture').replace('volbox', 'volcreated')
        self.fake_commands(f'#!{sys.executable}\n' + fake, 'ssh', 'nix', 'git', 'tailscale', 'rustc', 'cargo', 'cargo-nextest', 'cargo-mend', 'bash')
        _ = (self.root / 'box-aws').write_text(fake.replace("name = pathlib.Path(sys.argv[0]).name", "name = 'aws'"))
        _ = (self.root / 'live.json').write_text(json.dumps({'online': True, 'joined': True, 'active': True,
            'system': SYSTEM, 'desired': SYSTEM, 'ec2': 'running', 'stop_after': 5}))
        for key, value in (('box_check_delay_s', '0.001'), ('idle_stop_minutes', '0.001'), ('idle_stop_margin_s', '0')):
            self.replace(key, value)
        original_setup = box.BoxSetup
        def setup(settings: Path, directory: Path, start: Callable[[], object], *, execute: aws_target.AwsExecutor,
                  admin: admin_secrets.AdminSecretSession) -> box.BoxSetup:
            monotonic = FakeMonotonicClock()
            return original_setup(settings, directory, partial(self.start_box_after_tick, start), execute=execute, admin=admin, tailnet=box.tailnet_request,
                                  monotonic=monotonic, pause=monotonic.advance)
        with mock.patch.dict(os.environ, {'BOX_FIXTURE': str(self.root), 'BUILDLOG_DIR': str(self.root / 'buildlog')}), \
             mock.patch.object(box, 'BoxSetup', side_effect=setup), \
             mock.patch.object(target_config, 'read_targets', return_value=(target_config.SshTarget('aws', 'linux', self.settings),)):
            success, output = self.up_output(self.deployment(), include_box=True)
        self.assertTrue(success, output)
        saved = remote.read_record(path)
        self.assertEqual(saved['data_volume_id'], 'vol-created')
        self.assertEqual(saved['budget_name'], 'fixture-build-monthly')
        self.assertEqual((self.root / 'volume-owner').read_text(), saved['deployment_id'])
        updates = self.commands('deploy')[before:]
        self.assertEqual(len(updates), 1)
        arguments = cast(list[str], updates[0]['args'])
        self.assertIn('BudgetName=fixture-build-monthly', arguments)
        self.assertIn('ExistingDataVolumeId=', arguments)
        self.assertNotIn('ExistingDataVolumeId=vol-created', arguments)
        self.assertIn('EnableCompute=true', arguments)
        self.assertIn('routing-proof: done', output)
        node = ledger.text(remote.read_record(path), 'tailscale_node_id')
        ledger.rows(devices, 'devices')[0]['nodeId'] = node
        success, output = self.down_output()
        self.assertTrue(success, output)
        arguments = cast(list[str], self.commands('delete-budget')[-1]['args'])
        self.assertEqual(arguments[arguments.index('--budget-name') + 1], 'fixture-build-monthly')
        self.assertEqual([device['id'] for device in ledger.rows(devices, 'devices')], ['device-other'])

    def test_legacy_migration_refuses_a_different_live_instance_without_changes(self) -> None:
        devices = self.removal_fixture()
        self.assertIn('devices', devices)
        path = self.state / 'deployment.json'
        saved = remote.read_record(path)
        del saved['stack_id']
        ledger.mapping(saved['outputs'])['InstanceId'] = 'i-foreign'
        _ = path.write_text(json.dumps(saved))
        before = path.read_bytes()
        requests = len(self.commands())
        with self.assertRaisesRegex(deploy.DeploymentRefused, 'legacy InstanceId differs'):
            _ = self.deployment().prepare()
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(any(cast(list[str], row['args'])[5] in ('create-tags', 'deploy') for row in self.commands()[requests:]))

    def test_lost_create_reply_recovers_the_reserved_change_set_without_a_duplicate(self) -> None:
        failure = self.root / 'lose-create-reply'
        failure.touch()
        success, output = self.up_output(self.deployment())
        self.assertFalse(success, output)
        self.assertIn('stacks: refused:', output)
        self.assertNotIn('stack_id', remote.read_record(self.state / 'deployment.json'))
        failure.unlink()
        success, output = self.up_output(self.deployment())
        self.assertTrue(success, output)
        self.assertEqual(len([row for row in self.commands('create-change-set') if 'CREATE' in cast(list[str], row['args'])]), 1)
        saved = remote.read_record(self.state / 'deployment.json')
        self.assertEqual(saved['stack_id'], (self.root / 'reserved-stack').read_text())

    def test_reserved_change_set_recovery_refuses_foreign_parameters_or_account(self) -> None:
        failure = self.root / 'lose-create-reply'
        failure.touch()
        success, output = self.up_output(self.deployment())
        self.assertFalse(success, output)
        failure.unlink()
        path = self.root / 'change-set.json'
        original = remote.read_record(path)
        for changes in ({'Parameters': [{'ParameterKey': 'DeploymentId', 'ParameterValue': 'f' * 32}]},
                        {'StackId': str(original['StackId']).replace('123456789012', '999999999999')}):
            with self.subTest(changes=changes):
                _ = path.write_text(json.dumps({**original, **changes}))
                success, output = self.up_output(self.deployment())
                self.assertFalse(success, output)
                self.assertIn('reserved change set differs', output)
                self.assertEqual(len([row for row in self.commands('create-change-set') if 'CREATE' in cast(list[str], row['args'])]), 1)
                self.assertEqual(self.commands('deploy'), [])

    def test_reused_volume_requires_a_completed_owner_and_transfers_the_tag(self) -> None:
        self.replace('existing_data_volume_id', 'vol-retained')
        _ = (self.root / 'volume-owner').write_text('a' * 32)
        success, output = self.up_output(self.deployment())
        self.assertFalse(success, output)
        self.assertIn('no matching down-complete receipt', output)
        self.assertEqual(self.commands('create-tags'), [])
        self.assertEqual(self.commands('create-change-set'), [])
        _ = (self.state / 'down-complete.json').write_text(json.dumps({
            'deployment_id': 'a' * 32, 'data_volume_id': 'vol-retained'}))
        success, output = self.up_output(self.deployment())
        self.assertTrue(success, output)
        saved = remote.read_record(self.state / 'deployment.json')
        self.assertEqual((self.root / 'volume-owner').read_text(), saved['deployment_id'])
        self.assertEqual(saved['data_volume_id'], 'vol-retained')
        self.assertEqual(len(self.commands('create-tags')), 1)
        self.assertIn('ExistingDataVolumeId=vol-retained', cast(list[str], self.commands('deploy')[-1]['args']))

    def test_up_retries_failed_compute_without_a_second_foundation(self) -> None:
        failure = self.root / 'fail-compute'
        _ = failure.write_text('fail')
        success, output = self.up_output(self.deployment())
        self.assertFalse(success)
        self.assertIn('stacks: refused:', output)
        self.assertNotIn('scaler-key:', output)
        failure.unlink()
        success, output = self.up_output(self.deployment())
        self.assertTrue(success, output)
        foundations = [row for row in self.commands('deploy') if 'EnableCompute=false' in cast(list[str], row['args'])]
        self.assertEqual(len(foundations), 1)
        self.assertEqual(len(self.commands('create-access-key')), 1)
        self.assertFalse(ledger.CostLedger(self.state).pending_compute_requests())

    def test_review_in_progress_stack_has_no_resources_to_reconcile(self) -> None:
        _ = (self.root / 'review-stack').write_text('empty change set')
        success, output = self.up_output(self.deployment())
        self.assertTrue(success, output)
        self.assertEqual(len(self.commands('deploy')), 2)

    def test_foundation_wait_does_not_expire_unchanged_compute_preparation(self) -> None:
        _ = (self.root / 'slow-foundation').write_text('slow')
        subject = deploy.Deployment(self.settings, self.state, self.bundle,
            clock=lambda: float((self.root / 'clock').read_text()), notify=lambda _event: True)
        success, output = self.up_output(subject)
        self.assertTrue(success, output)
        self.assertEqual(len(self.commands('deploy')), 2)
        self.assertEqual(int((self.root / 'clock').read_text()), self.now + 301)

    def test_unconfirmed_scaler_key_is_deleted_and_never_printed(self) -> None:
        self.replace('scaler_key_check_attempts', '2')
        self.replace('scaler_key_check_delay_s', '0.001')
        before = self.credentials.read_bytes()
        _ = (self.root / 'fail-scaler').write_text('fail')
        success, output = self.up_output(self.deployment())
        self.assertFalse(success)
        self.assertIn('scaler-key: refused:', output)
        self.assertNotIn('fixture-secret-only-in-credentials', output)
        self.assertEqual(self.credentials.read_bytes(), before)
        self.assertEqual(json.loads((self.root / 'fake-keys.json').read_text()), ['old-key'])
        self.assertEqual(sum(cast(list[str], row['args'])[1] == 'build-scaler' for row in self.commands('get-caller-identity')), 3)
        self.assertEqual(len(self.commands('delete-access-key')), 1)

    def test_up_refuses_without_approval_before_any_price_or_resource_read(self) -> None:
        with self.assertRaisesRegex(deploy.DeploymentRefused, 'approve-resources'):
            _ = self.deployment().up()
        self.assertEqual(self.commands(), [])

    def test_price_command_writes_private_state_for_the_selected_zone_and_admin_environment(self) -> None:
        path = self.root / '.config/aws-build/deployment-price'
        self.replace('deployment_price_file', '~/.config/aws-build/deployment-price')
        settings = self.settings.read_bytes()
        for existing in (False, True):
            with self.subTest(existing=existing):
                if existing:
                    path.chmod(0o644)
                output = StringIO()
                before = int(time.time())
                with (mock.patch.object(sys, 'argv', ['deploy.py', 'price', '--settings', str(self.settings)]),
                      redirect_stdout(output)):
                    self.assertEqual(deploy.main(), 0)
                after = int(time.time())
                result = ledger.mapping(cast(object, json.loads(output.getvalue())))
                at = ledger.instant(result, 'deployment_price_at')
                self.assertGreaterEqual(at, before)
                self.assertLessEqual(at, after)
                self.assertEqual(result['deployment_price_usd_per_hour'], '0.1')
                self.assertEqual(result['file'], str(path))
                self.assertEqual(aws_config.deployment_price(self.settings, at), (at, Decimal('0.1')))
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
                self.assertEqual(self.settings.read_bytes(), settings)
        calls = self.commands()
        self.assertEqual(len(calls), 2)
        for call in calls:
            args = cast(list[str], call['args'])
            self.assertEqual(args[args.index('--profile') + 1], 'environment')
            self.assertEqual(args[args.index('--region') + 1], 'us-east-1')
            self.assertIn('describe-spot-price-history', args)
            self.assertEqual(args[args.index('--instance-types') + 1], 'm7a.4xlarge')
            self.assertEqual(args[args.index('--availability-zone') + 1], 'us-east-1a')
            self.assertEqual(args[args.index('--product-descriptions') + 1], 'Linux/UNIX')
            self.assertIn('--start-time', args)

    def test_price_command_requires_private_credentials_before_any_aws_read(self) -> None:
        self.credentials.chmod(0o644)
        with self.assertRaisesRegex(deploy.DeploymentRefused, 'AWS_SHARED_CREDENTIALS_FILE'):
            _ = self.deployment().price()
        self.assertEqual(self.commands(), [])

    def test_price_command_preserves_saved_price_after_an_invalid_reply(self) -> None:
        saved = self.price_file.read_bytes()
        for prices in ([], [{'SpotPrice': '0'}], [{'SpotPrice': '-1'}], [{'SpotPrice': 'NaN'}]):
            with self.subTest(prices=prices):
                _ = (self.root / 'describe-spot-price-history.json').write_text(json.dumps({'SpotPriceHistory': prices}))
                with self.assertRaises(ValueError):
                    _ = self.deployment().price()
                self.assertEqual(self.price_file.read_bytes(), saved)

    def test_prepare_refuses_a_missing_price_file_with_the_refresh_command(self) -> None:
        self.price_file.unlink()
        with self.assertRaisesRegex(ValueError, 'deploy.py price') as failure:
            _ = self.deployment().prepare()
        self.assertIn('deployment_price_file', str(failure.exception))
        self.assertIn(str(self.price_file), str(failure.exception))
        self.assertEqual(self.commands(), [])

    def test_prepare_refuses_price_paths_inside_the_public_repository(self) -> None:
        tracked_path = Path(__file__).resolve()
        self.price_file.unlink()
        self.price_file.symlink_to(tracked_path)
        for path in (tracked_path, self.price_file):
            with self.subTest(path=path):
                self.replace('deployment_price_file', str(path))
                with self.assertRaisesRegex(ValueError, 'outside the public repository') as failure:
                    _ = self.deployment().prepare()
                self.assertIn('deployment_price_file', str(failure.exception))
                self.assertIn(str(tracked_path), str(failure.exception))
        self.assertEqual(self.commands(), [])

    def test_prepare_refuses_a_stale_price_file(self) -> None:
        self.set_clock(self.now + aws_config.read_settings(self.settings).price_max_age_seconds + 1)
        with self.assertRaisesRegex(ValueError, 'fresh positive public price'):
            _ = self.deployment().prepare()
        self.assertEqual(self.commands(), [])

    def test_bootstrap_renders_without_a_price_file(self) -> None:
        self.price_file.unlink()
        rendered = self.reviewed_bundle()
        self.assertIn('aws-build-first-boot-stop', rendered)
        self.assertIn('/etc/aws-build/remote.conf', rendered)
        self.assertEqual(self.commands(), [])

    def test_apply_and_setup_start_require_the_saved_price_after_preparation(self) -> None:
        subject = self.deployment()
        _ = subject.prepare()
        _ = subject.apply(approve_resources=True)
        self.price_file.unlink()
        for action in (subject.apply, subject.setup_start):
            with self.subTest(action=action.__name__):
                with self.assertRaisesRegex(ValueError, 'deploy.py price'):
                    _ = action(approve_resources=True)
        self.assertEqual(len(self.commands('deploy')), 2)
        self.assertEqual(self.commands('start-instances'), [])

    def test_bootstrap_renders_without_a_notification_address_file(self) -> None:
        self.notification_file.unlink()
        rendered = self.reviewed_bundle()
        self.assertIn('aws-build-first-boot-stop', rendered)
        self.assertIn('/etc/aws-build/remote.conf', rendered)

    def test_prepare_refuses_missing_or_unreadable_notification_files(self) -> None:
        self.notification_file.unlink()
        for unreadable in (False, True):
            with self.subTest(unreadable=unreadable):
                if unreadable:
                    self.notification_file.mkdir()
                with self.assertRaises((deploy.DeploymentRefused, ValueError)) as failure:
                    _ = self.deployment().prepare()
                self.assertIn('notification_email_file', str(failure.exception))
                self.assertIn(str(self.notification_file), str(failure.exception))

    def test_prepare_refuses_invalid_notification_content_without_printing_it(self) -> None:
        for content in ('', ' \n', 'build example@example.invalid', 'build.example.invalid',
                        'build@example.invalid\nother@example.invalid\n'):
            with self.subTest(content=content):
                _ = self.notification_file.write_text(content)
                with self.assertRaises((deploy.DeploymentRefused, ValueError)) as failure:
                    _ = self.deployment().prepare()
                message = str(failure.exception)
                self.assertIn('notification_email_file', message)
                self.assertIn(str(self.notification_file), message)
                if content.strip():
                    self.assertNotIn(content.strip(), message)

    def test_prepare_refuses_notification_paths_inside_the_public_repository(self) -> None:
        tracked_path = Path(__file__).resolve()
        self.notification_file.unlink()
        self.notification_file.symlink_to(tracked_path)
        for path in (tracked_path, self.notification_file):
            with self.subTest(path=path):
                self.replace('notification_email_file', str(path))
                with self.assertRaises((deploy.DeploymentRefused, ValueError)) as failure:
                    _ = self.deployment().prepare()
                self.assertIn('notification_email_file', str(failure.exception))
                self.assertIn(str(path.resolve()), str(failure.exception))

    def test_prepare_uses_the_private_notification_address_in_saved_parameters(self) -> None:
        self.replace('notification_email_file', '~/notification-email')
        _ = self.notification_file.write_text('  build@example.invalid\n')
        prepared = self.deployment().prepare()
        self.assertEqual(prepared.parameters['NotificationEmail'], 'build@example.invalid')
        saved = cast(list[dict[str, str]], json.loads((self.bundle / 'parameters.json').read_text()))
        self.assertEqual([row['ParameterValue'] for row in saved if row['ParameterKey'] == 'NotificationEmail'],
                         ['build@example.invalid'])
        _ = self.notification_file.write_text('other@example.invalid\n')
        updated = self.deployment().prepare()
        self.assertEqual(updated.parameters['NotificationEmail'], 'other@example.invalid')
        self.assertNotEqual(updated.fingerprint, prepared.fingerprint)

    def test_apply_refuses_when_the_private_notification_file_disappears_after_prepare(self) -> None:
        subject = self.deployment()
        _ = subject.prepare()
        self.notification_file.unlink()
        with self.assertRaises((deploy.DeploymentRefused, ValueError)) as failure:
            _ = subject.apply(approve_resources=True)
        self.assertIn('notification_email_file', str(failure.exception))
        self.assertIn(str(self.notification_file), str(failure.exception))
        self.assertEqual(self.commands('deploy'), [])

    def test_prepare_is_local_and_first_boot_needs_no_observation(self) -> None:
        prepared = self.deployment().prepare()
        self.assertEqual(self.commands(), [])
        self.assertEqual(prepared.parameters['MonthlyLimitUsd'], '400')
        self.assertNotIn('EnableCompute', prepared.parameters)
        book = ledger.CostLedger(self.state).read_snapshot()
        self.assertEqual(book.observations, [])
        self.assertEqual(book.decision['status'], 'allowed')
        self.assertTrue(book.inventories)
        self.assertNotIn('credentials', (self.bundle / 'parameters.json').read_text())
        self.assertEqual(gzip.decompress(base64.b64decode(prepared.parameters['Bootstrap'])).decode(),
                         (self.bundle / 'bootstrap.sh').read_text())
        self.assertEqual(conf.read(self.settings).text('offload'), 'off')

    def test_apply_guards_foundation_before_compute_and_preserves_identity_on_rerun(self) -> None:
        subject = self.deployment()
        _ = subject.prepare()
        before = ledger.CostLedger(self.state).read_snapshot().inventories
        paths = ('claims/job.json', 'affinity.json', 'mirror/warm', 'peers/aws.json')
        for name in paths:
            path = self.state / name
            path.parent.mkdir(parents=True, exist_ok=True)
            _ = path.write_text('keep')
        outputs = subject.apply(approve_resources=True)
        self.assertEqual(outputs['InstanceId'], 'i-fixture')
        calls = self.commands('deploy')
        self.assertEqual(len(calls), 2)
        self.assertIn('EnableCompute=false', cast(list[str], calls[0]['args']))
        self.assertIn('EnableCompute=true', cast(list[str], calls[1]['args']))
        for row in calls:
            decision = cast(dict[str, object], row['decision'])
            self.assertEqual(decision['status'], 'allowed')
            self.assertEqual(decision['purpose'], 'deployment')
        _ = subject.prepare()
        self.assertEqual(subject.apply(approve_resources=True), outputs)
        calls = self.commands('deploy')
        self.assertEqual(len(calls), 3)
        self.assertIn('EnableCompute=true', cast(list[str], calls[-1]['args']))
        self.assertEqual(ledger.CostLedger(self.state).read_snapshot().inventories, before)
        for name in paths:
            self.assertEqual((self.state / name).read_text(), 'keep')

    def test_a_live_limit_change_after_preparation_refuses_every_resource(self) -> None:
        subject = self.deployment()
        _ = subject.prepare()
        self.replace('daily_limit_usd', '1')
        with self.assertRaises(deploy.DeploymentRefused):
            _ = subject.apply(approve_resources=True)
        self.assertEqual(self.commands(), [])

    def test_a_limit_change_during_foundation_refuses_compute(self) -> None:
        subject = self.deployment()
        _ = subject.prepare()
        execute = subject.execute

        def change_limit(arguments: Sequence[str], *, capture_output: bool, text: bool,
                         timeout: float, check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
            result = execute(arguments, capture_output=capture_output, text=text, timeout=timeout, check=check, env=env)
            if 'EnableCompute=false' in arguments:
                self.replace('monthly_limit_usd', '1')
            return result

        subject.execute = change_limit
        with self.assertRaises(deploy.DeploymentRefused):
            _ = subject.apply(approve_resources=True)
        calls = self.commands('deploy')
        self.assertEqual(len(calls), 1)
        self.assertIn('EnableCompute=false', cast(list[str], calls[0]['args']))

    def test_apply_requires_explicit_user_gate_and_private_credentials(self) -> None:
        subject = self.deployment()
        _ = subject.prepare()
        with self.assertRaises(deploy.DeploymentRefused):
            _ = subject.apply()
        self.credentials.chmod(0o644)
        with self.assertRaises(deploy.DeploymentRefused):
            _ = subject.apply(approve_resources=True)
        self.assertEqual(self.commands(), [])

    def test_setup_restart_uses_the_guard_without_enabling_offload_or_duplicate_starts(self) -> None:
        subject = self.deployment()
        _ = subject.prepare()
        _ = subject.apply(approve_resources=True)
        _ = box.close_routing(self.settings)
        self.set_clock(self.now + 1)
        self.assertEqual(subject.setup_start(approve_resources=True), {'InstanceId': 'i-fixture', 'State': 'pending'})
        self.assertEqual(subject.setup_start(approve_resources=True), {'InstanceId': 'i-fixture', 'State': 'pending'})
        starts = self.commands('start-instances')
        self.assertEqual(len(starts), 1)
        self.assertEqual(cast(dict[str, object], starts[0]['decision'])['purpose'], 'deployment')
        self.assertEqual(conf.read(self.settings).text('offload'), 'off')
        self.assertFalse((self.state / 'admission.json').exists())
        self.assertFalse(remote.read_record(self.state / 'routing.json')['enabled'])
        self.assertEqual(ledger.CostLedger(self.state).read_snapshot().observations[-1].state, 'pending')

    def test_setup_restart_uses_prepared_parameters_without_the_private_address_file(self) -> None:
        subject = self.deployment()
        _ = subject.prepare()
        _ = subject.apply(approve_resources=True)
        self.notification_file.unlink()
        self.assertEqual(subject.setup_start(approve_resources=True), {'InstanceId': 'i-fixture', 'State': 'pending'})
        self.assertEqual(len(self.commands('start-instances')), 1)

    def test_box_start_waits_for_spot_capacity_until_accepted(self) -> None:
        waits: list[float] = []

        def pause(seconds: float) -> None:
            waits.append(seconds)
            self.set_clock(self.now + int(seconds))

        subject = deploy.Deployment(self.settings, self.state, self.bundle, clock=lambda: self.now,
                                    pause=pause, notify=lambda _event: True)
        _ = subject.prepare()
        _ = subject.apply(approve_resources=True)
        started = self.now
        _ = (self.root / 'capacity-refusals').write_text('2')
        accepted = {'InstanceId': 'i-fixture', 'State': 'pending'}
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(subject._start_box(), accepted)  # pyright: ignore[reportPrivateUsage]
        delay = read_control_settings(self.settings).capacity_backoff_s
        self.assertEqual(len(self.commands('start-instances')), 3)
        self.assertEqual((self.root / 'capacity-refusals').read_text(), '0')
        self.assertEqual(waits, [delay] * 2)
        self.assertEqual(self.now, started + int(delay) * 2)
        self.assertEqual(remote.read_record(self.bundle / 'preflight.json')['at'], self.now)
        self.assertEqual(output.getvalue().splitlines(), [
            f'AWS has no Spot capacity for the box now; next try in {delay:g} seconds.'] * 2)

    def test_box_start_raises_other_aws_errors_without_waiting(self) -> None:
        pause = mock.Mock()
        subject = deploy.Deployment(self.settings, self.state, self.bundle, clock=lambda: self.now,
                                    pause=pause, notify=lambda _event: True)
        _ = subject.prepare()
        _ = subject.apply(approve_resources=True)
        error = aws_target.AwsUnavailable('UnauthorizedOperation: start refused')
        output = StringIO()
        with (mock.patch.object(subject, 'setup_start', side_effect=error) as start,
              redirect_stdout(output), self.assertRaises(aws_target.AwsUnavailable) as failure):
            _ = subject._start_box()  # pyright: ignore[reportPrivateUsage]
        self.assertIs(failure.exception, error)
        start.assert_called_once_with(approve_resources=True)
        pause.assert_not_called()
        self.assertEqual(output.getvalue(), '')

    def test_setup_restart_refuses_a_changed_limit_before_any_resource_request(self) -> None:
        subject = self.deployment()
        _ = subject.prepare()
        _ = subject.apply(approve_resources=True)
        count = len(self.commands())
        self.replace('daily_limit_usd', '1')
        with self.assertRaises(deploy.DeploymentRefused):
            _ = subject.setup_start(approve_resources=True)
        self.assertTrue(all('describe-instances' in cast(list[str], row['args']) for row in self.commands()[count:]))
        self.assertEqual(self.commands('start-instances'), [])

    def test_prepare_preserves_existing_cost_and_claim_evidence(self) -> None:
        subject = self.deployment()
        _ = subject.prepare()
        book = ledger.CostLedger(self.state)
        book.observe(ledger.InstanceObservation(self.now, 'stopped', 'fixture'))
        self.set_clock(self.now + 1)
        _ = subject.prepare()
        self.assertEqual(book.read_snapshot().observations, [ledger.InstanceObservation(self.now - 1, 'stopped', 'fixture')])

    def test_retained_volume_reuse_keeps_the_volume_tag_and_creates_no_new_identity(self) -> None:
        self.replace('existing_data_volume_id', 'vol-retained')
        subject = self.deployment()
        _ = subject.prepare()
        result = subject.apply(approve_resources=True)
        self.assertEqual(result['DataVolumeId'], 'vol-retained')
        calls = self.commands()
        self.assertTrue(any('describe-volumes' in cast(list[str], row['args']) for row in calls))
        self.assertEqual(subject.apply(approve_resources=True)['DataVolumeId'], 'vol-retained')

    def test_template_enforces_budget_tags_retention_network_and_single_instance_policy(self) -> None:
        template = remote.read_record(deploy.TEMPLATE)
        resources = cast(dict[str, dict[str, object]], template['Resources'])
        for name in ('InstanceRole', 'InstanceProfile', 'SecurityGroup'):
            with self.subTest(compute_resource=name):
                self.assertEqual(resources[name].get('Condition'), 'Compute')
        cost_tag = {'Key': {'Ref': 'CostTagKey'}, 'Value': {'Ref': 'CostTagValue'}}
        for name in ('DataVolume', 'MonthlyBudget'):
            resource = resources[name]
            self.assertEqual(resource['DeletionPolicy'], 'Retain')
            self.assertEqual(resource['UpdateReplacePolicy'], 'Retain')
        self.assertNotIn('DeletionPolicy', resources['Instance'])
        self.assertNotIn('UpdateReplacePolicy', resources['Instance'])
        self.assertNotIn('Tags', cast(dict[str, object], resources['Instance']['Properties']))
        for name in ('Instance', 'DataVolume', 'LaunchTemplate'):
            self.assertEqual(resources[name]['DependsOn'], ['BudgetEnforcement'])
        properties = cast(dict[str, object], resources['DataVolume']['Properties'])
        self.assertIn(cost_tag, cast(list[object], properties['Tags']))
        launch = cast(dict[str, object], resources['LaunchTemplate']['Properties'])
        data = cast(dict[str, object], launch['LaunchTemplateData'])
        self.assertEqual(data['InstanceMarketOptions'], {
            'MarketType': 'spot', 'SpotOptions': {'SpotInstanceType': 'persistent',
            'InstanceInterruptionBehavior': 'stop', 'MaxPrice': {'Ref': 'SpotMaxPrice'}}})
        self.assertEqual(data['InstanceInitiatedShutdownBehavior'], 'stop')
        mappings = cast(list[dict[str, object]], data['BlockDeviceMappings'])
        self.assertIs(cast(dict[str, object], mappings[0]['Ebs'])['DeleteOnTermination'], True)
        self.assertEqual(cast(dict[str, object], data['MetadataOptions'])['HttpTokens'], 'required')
        tag_specs = cast(list[dict[str, object]], data['TagSpecifications'])
        self.assertEqual({row['ResourceType'] for row in tag_specs}, {'instance', 'volume', 'spot-instances-request'})
        for spec in tag_specs:
            self.assertIn(cost_tag, cast(list[object], spec['Tags']))
        budget = cast(dict[str, object], resources['MonthlyBudget']['Properties'])
        budget_data = cast(dict[str, object], budget['Budget'])
        self.assertEqual(budget_data['BudgetLimit'], {'Amount': {'Ref': 'MonthlyLimitUsd'}, 'Unit': 'USD'})
        self.assertIn('AWS_BUILD_DATA_VOLUME_ID', str(data['UserData']))
        self.assertIn("'Ref': 'DataVolume'", str(data['UserData']))
        ingress = cast(dict[str, object], resources['SecurityGroup']['Properties'])['SecurityGroupIngress']
        self.assertEqual(ingress, [{'IpProtocol': 'udp', 'FromPort': 41641, 'ToPort': 41641, 'CidrIp': '0.0.0.0/0'}])
        policy = cast(dict[str, object], resources['ScalerPolicy']['Properties'])
        statements = cast(list[dict[str, object]], cast(dict[str, object], policy['PolicyDocument'])['Statement'])
        self.assertEqual(statements[0]['Action'], ['ec2:StartInstances', 'ec2:StopInstances'])
        self.assertIn('${Instance}', str(statements[0]['Resource']))
        self.assertIn('ec2:DescribeSpotInstanceRequests', cast(list[str], statements[1]['Action']))
        self.assertNotIn('AccessKey', json.dumps(template))
        self.assertNotIn('mkfs', json.dumps(template))

    def test_repository_configuration_enables_approved_aws_control_and_exchange(self) -> None:
        root = Path(__file__).resolve().parents[2]
        self.assertEqual(conf.read(root / 'config/aws_target.conf').text('offload'), 'on')
        self.assertEqual(conf.read(root / 'config/build_targets.conf').text('targets').split(','),
                         ['mac', 'natedev', 'aws'])

    def refuse_command(self, arguments: Sequence[str], *, capture_output: bool, text: bool,
                       timeout: float, check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        self.assertIsNone(env)
        _ = (capture_output, text, timeout, check)
        raise AssertionError(f'disabled tick called AWS: {arguments}')

    def test_scheduled_exchange_includes_aws_while_control_stays_off(self) -> None:
        registry = self.root / 'build_targets.conf'
        _ = registry.write_text('\n'.join((
            'targets=aws,natedev', 'kind.aws=ssh', 'platform.aws=linux', 'settings.aws=aws_target.conf',
            'kind.natedev=local', 'platform.natedev=linux', 'sync.hosts.aws=aws-build',
            'sync.connect_timeout_s=5', 'sync.reach_timeout_s=5', 'sync.rsync_timeout_s=5', '',
        )))
        commands: list[list[str]] = []
        logs = self.root / 'buildlog'

        def exchange(arguments: list[str], _timeout: float) -> subprocess.CompletedProcess[str]:
            self.assertIn(arguments[0], ('ssh', 'rsync'))
            commands.append(arguments)
            return subprocess.CompletedProcess(arguments, 0, 'rc=0\n' if arguments[0] == 'ssh' else '', '')

        with (mock.patch.dict(os.environ, {'BUILDLOG_PEER': ''}),
              mock.patch('scripts.buildlog.sync.store.root', return_value=logs),
              mock.patch('scripts.buildlog.sync.store.host_name', return_value='natedev'),
              mock.patch('scripts.buildlog.sync.health.write_snapshot'),
              mock.patch.object(sync, 'run', exchange)):
            self.assertEqual(sync.sync(), 0)
            status = sync.read_target_status('aws')
            self.assertIsInstance(status, sync.SyncStatus)
            if isinstance(status, sync.SyncStatus):
                self.assertTrue(status.ok)
        self.assertTrue(commands)
        self.assertEqual(conf.read(self.settings).text('offload'), 'off')
        self.assertEqual(self.commands(), [])

    def test_service_command_and_repeated_ticks_share_charge_recovery_across_utc_midnight(self) -> None:
        subject = self.deployment()
        _ = subject.prepare()
        _ = subject.apply(approve_resources=True)
        self.replace('offload', 'on')
        registry = self.root / 'build_targets.conf'
        _ = registry.write_text('targets=aws,natedev\nkind.aws=ssh\nplatform.aws=linux\nsettings.aws=aws_target.conf\nkind.natedev=local\nplatform.natedev=linux\n')
        controller = aws_target.Controller(self.settings, self.state, self.root / 'queue', clock=lambda: self.now,
                                           notify=lambda _event: True, publish=lambda _envelope: True)
        result = controller.tick()
        self.assertEqual(result.state, 'stopped', result.reason)
        result = controller.tick()
        self.assertEqual(result.state, 'stopped', result.reason)
        self.set_clock(self.now + 86400 + 120)
        save = cast(Callable[[ledger.CostLedger, ledger.LedgerEvidence], None], getattr(ledger.CostLedger, '_save'))

        def fail_reconciliation(book: ledger.CostLedger, state: ledger.LedgerEvidence) -> None:
            if any(row.at == self.now for row in state.reconciliations):
                raise OSError('the reconciliation write failed')
            save(book, state)

        with mock.patch.object(ledger.CostLedger, '_save', fail_reconciliation):
            result = controller.tick()
        self.assertEqual(result.state, 'stopped', result.reason)
        book = ledger.CostLedger(self.state)
        self.assertEqual(len(book.read_snapshot().reconciliations), 1)
        result = controller.tick()
        self.assertEqual(result.state, 'stopped', result.reason)
        queries = self.commands('get-cost-and-usage')
        self.assertEqual(len(queries), 2)
        self.assertEqual(len(book.read_snapshot().reconciliations), 2)
        self.assertNotIn('charge_error', (self.state / 'controller.json').read_text())
        settings = read_control_settings(self.settings)
        api = aws_target.AwsCli(settings.credentials_profile, settings.region, settings.aws_timeout_s)
        self.assertIsInstance(controller.refresh_charges(api, self.now), ledger.ChargeRead)
        self.assertEqual(len(self.commands('get-cost-and-usage')), 2)

    def test_bootstrap_remote_idle_stop_requires_no_published_permission(self) -> None:
        configuration = deploy.bootstrap_configuration(self.settings, deploy.remote_configuration(self.settings))
        self.assertIn('services.amazon-ssm-agent.enable = true', configuration)
        self.assertIn('aws-build-first-boot-stop', configuration)
        self.assertNotIn('@IDLE_STOP_SECONDS@', configuration)
        self.assertNotIn('mkfs', configuration)
        remote_settings = self.root / 'remote.conf'
        _ = remote_settings.write_text(deploy.remote_configuration(self.settings))
        self.assertEqual(conf.read(self.settings).text('offload'), 'off')
        self.assertTrue(read_control_settings(remote_settings).enabled)
        stops: list[int] = []
        directory = self.root / 'remote'
        agent = remote.RemoteAgent(remote_settings, directory, clock=lambda: self.now, imds=lambda: False,
                                   process_groups=lambda: (), shutdown=lambda: stops.append(self.now), boot=lambda: 'new-boot')
        self.assertEqual(agent.tick(), 'running')
        self.assertIsInstance(remote.execution_permission(directory, 'aws', now=self.now, boot_id='new-boot'), ledger.PermissionUnavailable)
        self.set_clock(self.now + int(read_control_settings(remote_settings).idle_stop_minutes * 60))
        self.assertEqual(agent.tick(), 'stopping')
        self.assertEqual(stops, [self.now])
        self.assertIsInstance(remote.execution_permission(directory, 'aws', now=self.now, boot_id='new-boot'), ledger.PermissionUnavailable)

    def test_reviewed_bootstrap_template_renders_and_fits_the_deployment_parameter(self) -> None:
        patch = (deploy.TEMPLATE.parent / 'nixos.patch').read_text()
        for statement in (r'^\+install -d -m ([0-7]{3,4}) /var/lib/aws-build$',
                          r'^\+\s+\$\{pkgs.coreutils\}/bin/install -d -m ([0-7]{3,4}) /var/lib/aws-build$'):
            modes = list(re.finditer(statement, patch, re.MULTILINE))
            self.assertEqual(len(modes), 1, statement)
            self.assertEqual(int(modes[0].group(1), 8) & 0o005, 0o005)
        file_modes = list(re.finditer(r'^\+\s+\$\{pkgs.coreutils\}/bin/chmod ([0-7]{3,4}) /var/lib/aws-build/remote.conf$',
                                     patch, re.MULTILINE))
        self.assertEqual(len(file_modes), 1)
        self.assertEqual(int(file_modes[0].group(1), 8) & 0o004, 0o004)
        self.install_reviewed_template()
        for volume in ('', 'vol-0123456789abcdef0'):
            with self.subTest(volume=volume):
                self.replace('existing_data_volume_id', volume)
                rendered = deploy.bootstrap_configuration(self.settings, deploy.remote_configuration(self.settings))
                self.assertTrue(rendered.startswith('#!'))
                self.assertNotRegex(rendered, r'@[A-Z_]+@')
                self.assertIn('nvme-Amazon_Elastic_Block_Store_', rendered)
                self.assertIn('/etc/aws-build/remote.conf', rendered)
                self.assertNotIn('../modules/linux/tailscale.nix', rendered)
                self.assertIn('extraSetFlags = [ "--ssh" ];', rendered)
                self.assertNotIn('# The module enables Tailscale SSH.', rendered)
                bootstrap = self.bundle / 'bootstrap.sh'
                _ = bootstrap.write_text(rendered)
                parameters = self.deployment().prepare().parameters
                self.assertLessEqual(len(parameters['Bootstrap']), 4096)
                self.assertEqual(gzip.decompress(base64.b64decode(parameters['Bootstrap'])).decode(), rendered)

    def test_controller_service_uses_the_tick_command_and_reads_each_interval(self) -> None:
        self.replace('offload', 'on')
        calls: list[list[str]] = []
        pauses: list[float] = []

        def execute(arguments: Sequence[str], *, capture_output: bool, text: bool,
                    timeout: float, check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
            self.assertFalse(capture_output)
            self.assertTrue(text)
            self.assertFalse(check)
            self.assertEqual(timeout, conf.read(self.settings).number('controller_tick_timeout_s'))
            self.assertIsNone(env)
            self.assertEqual(os.environ['AWS_SHARED_CREDENTIALS_FILE'], str(self.credentials))
            calls.append(list(arguments))
            self.replace('tick_interval_s', str(7 * len(calls)))
            return subprocess.CompletedProcess(arguments, 0, '', '')

        def pause(seconds: float) -> None:
            pauses.append(seconds)
            if len(pauses) == 2:
                self.replace('offload', 'off')

        aws_target.serve(self.settings, self.state, execute=execute, pause=pause)
        expected = [sys.executable, str(Path(aws_target.__file__)), 'tick',
                    '--settings', str(self.settings), '--directory', str(self.state)]
        self.assertEqual(calls, [expected, expected])
        self.assertEqual(pauses, [7, 14])
        aws_target.serve(self.settings, self.state, execute=self.refuse_command, pause=pause)


    def test_replacement_reuses_the_saved_volume_and_preserves_cost_evidence(self) -> None:
        subject = self.deployment()
        _ = subject.prepare()
        outputs = subject.apply(approve_resources=True)
        before = ledger.CostLedger(self.state).read_snapshot()
        claim = self.state / 'retained-claim.json'
        _ = claim.write_text('keep')
        (self.root / 'fake-stack.json').unlink()
        _ = (self.state / 'down-complete.json').write_text((self.state / 'deployment.json').read_text())
        self.replace('stack_name', 'replacement-build')
        self.replace('existing_data_volume_id', outputs['DataVolumeId'])
        _ = subject.prepare()
        replacement = subject.apply(approve_resources=True)
        self.assertEqual(replacement['DataVolumeId'], outputs['DataVolumeId'])
        self.assertEqual(claim.read_text(), 'keep')
        after = ledger.CostLedger(self.state).read_snapshot()
        self.assertEqual(after.inventories, before.inventories)
        self.assertEqual(after.prices, before.prices)
        self.assertTrue(all(row in after.observations for row in before.observations))
        self.assertEqual(len(self.commands('deploy')), 4)

    def test_missing_stack_refuses_new_resources_without_the_saved_volume(self) -> None:
        subject = self.deployment()
        _ = subject.prepare()
        _ = subject.apply(approve_resources=True)
        saved = (self.state / 'deployment.json').read_bytes()
        (self.root / 'fake-stack.json').unlink()
        _ = subject.prepare()
        with self.assertRaisesRegex(deploy.DeploymentRefused, 'saved DataVolumeId'):
            _ = subject.apply(approve_resources=True)
        self.assertEqual(len(self.commands('deploy')), 2)
        self.assertEqual((self.state / 'deployment.json').read_bytes(), saved)

    def test_volume_selection_change_requires_deleting_the_old_stack(self) -> None:
        subject = self.deployment()
        _ = subject.prepare()
        outputs = subject.apply(approve_resources=True)
        self.replace('existing_data_volume_id', outputs['DataVolumeId'])
        _ = subject.prepare()
        with self.assertRaisesRegex(deploy.DeploymentRefused, 'delete the old stack'):
            _ = subject.apply(approve_resources=True)
        self.replace('stack_name', 'replacement-build')
        _ = subject.prepare()
        with self.assertRaisesRegex(deploy.DeploymentRefused, 'delete the old stack'):
            _ = subject.apply(approve_resources=True)
        self.assertEqual(len(self.commands('deploy')), 2)

    def test_setup_after_idle_stop_refreshes_stale_state_before_the_guard(self) -> None:
        subject = self.deployment()
        _ = subject.prepare()
        _ = subject.apply(approve_resources=True)
        self.set_clock(self.now + 1)
        _ = subject.setup_start(approve_resources=True)
        stack_path = self.root / 'fake-stack.json'
        stack = remote.read_record(stack_path)
        stack['instance_state'] = 'stopped'
        _ = stack_path.write_text(json.dumps(stack))
        self.set_clock(self.now + 901)
        _ = subject.prepare()
        self.assertEqual(ledger.CostLedger(self.state).read_snapshot().observations[-1].state, 'stopped')
        self.assertEqual(subject.setup_start(approve_resources=True)['State'], 'pending')
        self.assertEqual(len(self.commands('start-instances')), 2)

    def test_compute_update_revalidates_the_selected_image(self) -> None:
        subject = self.deployment()
        _ = subject.prepare()
        _ = subject.apply(approve_resources=True)
        self.replace('ami_id', 'ami-unverified')
        _ = subject.prepare()
        with self.assertRaisesRegex(deploy.DeploymentRefused, 'image identity'):
            _ = subject.apply(approve_resources=True)
        self.assertEqual(len(self.commands('describe-images')), 2)
        self.assertEqual(len(self.commands('deploy')), 2)

    def test_retained_volume_update_accepts_only_its_current_attachment(self) -> None:
        self.replace('existing_data_volume_id', 'vol-retained')
        subject = self.deployment()
        _ = subject.prepare()
        _ = subject.apply(approve_resources=True)
        volume = {'VolumeId': 'vol-retained', 'AvailabilityZone': 'us-east-1a', 'Encrypted': True, 'State': 'in-use',
                  'Size': 500, 'VolumeType': 'gp3', 'Iops': 6000, 'Throughput': 500,
                  'Tags': [{'Key': 'Project', 'Value': 'fixture-build'},
                                 {'Key': 'DeploymentId', 'Value': ledger.text(remote.read_record(self.state / 'deployment.json'), 'deployment_id')}],
                  'Attachments': [{'InstanceId': 'i-fixture'}]}
        override_path = self.root / 'describe-volumes.json'
        _ = override_path.write_text(json.dumps({'Volumes': [volume]}))
        _ = subject.prepare()
        self.assertEqual(subject.apply(approve_resources=True)['DataVolumeId'], 'vol-retained')
        volume['Attachments'] = [{'InstanceId': 'i-other'}]
        _ = override_path.write_text(json.dumps({'Volumes': [volume]}))
        with self.assertRaisesRegex(deploy.DeploymentRefused, 'retained volume'):
            _ = subject.apply(approve_resources=True)
        self.assertEqual(len(self.commands('deploy')), 3)

    def test_compute_deployment_records_runtime_before_controller_activation(self) -> None:
        subject = self.deployment()
        _ = subject.prepare()
        _ = (self.root / 'describe-instances.json').write_text(json.dumps({
            'Reservations': [{'Instances': [{'InstanceId': 'i-fixture', 'LaunchTime': 'boot',
                                            'State': {'Name': 'running'}}]}]}))
        _ = subject.apply(approve_resources=True)
        book = ledger.CostLedger(self.state)
        self.assertEqual(book.read_snapshot().observations[-1].state, 'running')
        self.set_clock(self.now + 600)
        _ = subject.prepare()
        decision = ledger.mapping(remote.read_record(self.bundle / 'preflight.json')['decision'])
        self.assertGreater(float(ledger.text(decision, 'daily_usd')), 0.01)
        self.assertEqual(conf.read(self.settings).text('offload'), 'off')

    def test_resource_requests_write_no_unused_intent_record(self) -> None:
        subject = self.deployment()
        _ = subject.prepare()
        _ = subject.apply(approve_resources=True)
        self.set_clock(self.now + 1)
        _ = subject.setup_start(approve_resources=True)
        self.assertFalse((self.state / 'deployment-intent.json').exists())

    def test_remote_settings_omit_an_absent_instance_identity(self) -> None:
        self.replace('instance_id', '')
        rendered = deploy.remote_configuration(self.settings)
        self.assertNotIn('instance_id=', rendered)
        self.assertNotIn('unpublished', rendered)
        self.assertEqual(rendered.count('offload='), 1)
        self.assertEqual(rendered.count('ci_repo='), 1)

    def test_bootstrap_settings_bind_imds_identity_without_admission(self) -> None:
        self.replace('instance_id', '')
        source = self.root / 'bootstrap.conf'
        _ = source.write_text(deploy.remote_configuration(self.settings))
        destination = self.root / 'lifecycle/remote.conf'
        requests: list[str] = []

        def reply(request: Request, *, timeout: float) -> BytesIO:
            self.assertEqual(timeout, conf.read(source).number('imds_timeout_s'))
            requests.append(request.full_url)
            if request.get_method() == 'PUT':
                return BytesIO(b'fixture-token')
            self.assertEqual(request.get_header('X-aws-ec2-metadata-token'), 'fixture-token')
            return BytesIO(b'{"instanceId":"i-abcdef","region":"us-east-2"}')

        with mock.patch.object(remote, 'urlopen', reply):
            remote.write_bootstrap_settings(source, destination)
            first = destination.read_bytes()
            remote.write_bootstrap_settings(source, destination)
        self.assertEqual(destination.read_bytes(), first)
        settings = read_control_settings(destination, remote_identity=True)
        self.assertEqual(settings.instance_id, 'i-abcdef')
        self.assertEqual(settings.region, 'us-east-2')
        self.assertTrue(settings.enabled)
        self.assertEqual(destination.stat().st_mode & 0o777, 0o644)
        self.assertEqual(len(requests), 4)
        self.assertFalse((destination.parent / 'admission.json').exists())

    def test_remote_settings_preserve_the_source_limit_lines(self) -> None:
        source = self.settings.read_text().replace('# range: 0.001..\nidle_stop_minutes=',
                                                   '# range: 10..20\nidle_stop_minutes=')
        _ = self.settings.write_text(source)
        self.assertIn('# range: 10..20\nidle_stop_minutes=15', deploy.remote_configuration(self.settings))

    def fake_commands(self, source: str, *names: str) -> None:
        for name in names:
            path = self.root / 'bin' / name
            _ = path.write_text(source)
            path.chmod(0o700)

    def run_bash(self, path: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(['bash', str(path)], capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)

    def patch_source(self, path: str) -> str:
        if not self.patch_sections:
            patch = (deploy.TEMPLATE.parent / 'nixos.patch').read_text()
            for section in patch.split('diff --git ')[1:]:
                name = section.splitlines()[0].split(' b/', 1)[1]
                self.patch_sections[name] = '\n'.join(line[1:] for line in section.splitlines()
                                                     if line.startswith('+') and not line.startswith('+++')) + '\n'
        return self.patch_sections[path]

    def install_reviewed_template(self) -> None:
        hosts = self.root / 'nixos/hosts'
        template = hosts / 'reviewed-bootstrap.sh.in'
        _ = template.write_text(self.patch_source('hosts/aws-bootstrap.sh.in'))
        _ = (hosts / 'aws-build-base.nix').write_text(self.patch_source('hosts/aws-build-base.nix'))
        self.replace('bootstrap_template_path', str(template))

    def reviewed_bundle(self) -> str:
        self.install_reviewed_template()
        with mock.patch.object(sys, 'argv', ['deploy.py', 'bootstrap', '--settings', str(self.settings),
                                           '--output', str(self.bundle)]):
            self.assertEqual(deploy.main(), 0)
        return (self.bundle / 'bootstrap.sh').read_text()

    def test_bootstrap_retry_keeps_the_existing_watchdog_deadline(self) -> None:
        rendered = self.reviewed_bundle()
        watchdog = rendered.split('# Bound the first boot', 1)[1].split('\ncat > ', 1)[0]
        watchdog = watchdog[watchdog.index('\n') + 1:]
        script = self.root / 'watchdog.sh'
        _ = script.write_text('set -euo pipefail\n' + watchdog)
        fake = r'''import json, os, pathlib, sys
root = pathlib.Path(os.environ['DEPLOY_FIXTURE'])
state = root / 'watchdog-deadline.json'
if pathlib.Path(sys.argv[0]).name == 'systemctl':
    assert sys.argv[1:] == ['show', '--property=LoadState', '--value', 'aws-build-first-boot-stop.timer']
    print('loaded' if state.exists() else 'not-found')
else:
    with (root / 'watchdog-calls.jsonl').open('a') as output:
        output.write(json.dumps(sys.argv[1:]) + '\n')
    seconds = next(arg.split('=')[1].removesuffix('s') for arg in sys.argv[1:] if arg.startswith('--on-active='))
    state.write_text(json.dumps(int((root / 'clock').read_text()) + int(seconds)))
'''
        self.fake_commands(f'#!{sys.executable}\n' + fake, 'systemctl', 'systemd-run')
        first = self.run_bash(script)
        self.assertEqual(first.returncode, 0, first.stderr)
        deadline = (self.root / 'watchdog-deadline.json').read_bytes()
        self.assertEqual(json.loads(deadline), self.now + int(read_control_settings(self.settings).idle_stop_minutes * 60))
        self.set_clock(self.now + 60)
        second = self.run_bash(script)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual((self.root / 'watchdog-deadline.json').read_bytes(), deadline)
        self.assertEqual(len((self.root / 'watchdog-calls.jsonl').read_text().splitlines()), 1)

    def test_controller_service_restarts_after_a_failed_interval_read(self) -> None:
        module = self.patch_source('modules/linux/aws-build.nix')
        unit = module.split('systemd.user.services.aws-target = {', 1)[1]
        service = unit.split('serviceConfig = {', 1)[1].split('};', 1)[0]
        self.assertRegex(service, r'Restart\s*=\s*"on-failure"\s*;')
        delay = re.search(r'RestartSec\s*=\s*"([0-9]+(?:\.[0-9]+)?)s"\s*;', service)
        if delay is None:
            self.fail('the controller needs a restart delay')
        self.assertGreater(Decimal(delay.group(1)), 0)

    def install_volume_fakes(self) -> None:
        state = self.root / 'volume-state'
        state.mkdir()
        _ = (state / 'data-volume-id').write_text('vol-0123456789abcdef0\n')
        (self.root / 'device').touch()
        fake = r'''#!/bin/sh
set -eu
root=$DEPLOY_FIXTURE
case ${0##*/} in
  udevadm) test "$1" = wait ;;
  readlink)
    test "$1" = -f
    test "$2" = /dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_vol0123456789abcdef0
    printf '%s/device\n' "$root" ;;
  lsblk)
    case "$3" in
      SERIAL) printf '%s\n' "$(cat "$root/serial")" ;;
      TYPE) printf 'disk\n' ;;
      *) test "$3" = MOUNTPOINTS ;;
    esac ;;
  blkid)
    test "$1" = -s
    test "$3" = -o
    test "$4" = value
    test "$5" = "$root/device"
    if test -f "$root/TYPE" || test -f "$root/LABEL"; then
      if test -f "$root/$2"; then cat "$root/$2"; else printf '\n'; fi
    else
      printf '\n'
      exit 2
    fi ;;
  wipefs)
    if test -f "$root/TYPE"; then cat "$root/TYPE"; else printf '\n'; fi ;;
  mkfs.ext4)
    test "$#" -eq 3
    test "$1" = -L
    test "$2" = aws-home
    test "$3" = "$root/device"
    printf 'format\n' >> "$root/formats"
    printf 'ext4\n' > "$root/TYPE"
    printf 'aws-home\n' > "$root/LABEL" ;;
  *) exit 1 ;;
esac
'''
        self.fake_commands(fake, 'udevadm', 'readlink', 'lsblk', 'blkid', 'wipefs', 'mkfs.ext4')

    def volume_script(self, *, bootstrap: bool, existing: str = '') -> Path:
        base = self.patch_source('hosts/aws-build-base.nix')
        host = self.patch_source('hosts/aws-build.nix')
        self.assertRegex(host, r'imports\s*=\s*\[\s*\(import\s+\./aws-build-base\.nix\b')
        self.assertIn('existingVolume = if bootstrap then installation.existingVolume else "";', base)
        if bootstrap:
            self.replace('existing_data_volume_id', existing)
            source = self.reviewed_bundle()
            installation = source.split('    installation = {', 1)[1].split('    };', 1)[0]
            volume = re.search(r'existingVolume = ("[^"\n]*");', installation)
            delay = re.search(r'volumeWaitSeconds = ([0-9]+);', installation)
            if volume is None or delay is None:
                self.fail('the rendered installation needs volume settings')
            existing = cast(str, json.loads(volume.group(1)))
            wait = '--timeout=' + delay.group(1)
        else:
            source, existing, wait = base, '', ''
        volume_script = source.split('systemd.services.aws-build-volume = {', 1)[1]
        script = volume_script.split("script = ''\n", 1)[1].split("\n    '';", 1)[0]
        bindings = {
            '${pkgs.systemd}/bin/udevadm': 'udevadm',
            '${lib.optionalString bootstrap "--timeout=${toString installation.volumeWaitSeconds}"}': wait,
            '${if bootstrap then "false" else "true"}': 'false' if bootstrap else 'true',
            '${lib.escapeShellArg existingVolume}': shlex.quote(existing),
            '/var/lib/aws-build': str(self.root / 'volume-state'),
            'test -b "$device"': 'test -f "$device"',
        }
        self.assertEqual(script.count('test -b "$device"'), 1)
        for name, value in bindings.items():
            script = script.replace(name, value)
        self.assertNotIn('${', script)
        path = self.root / ('bootstrap-volume.sh' if bootstrap else 'host-volume.sh')
        _ = path.write_text(script)
        return path

    def test_both_volume_modes_refuse_wrong_serials_and_preserve_existing_filesystems(self) -> None:
        self.install_volume_fakes()
        for bootstrap in (True, False):
            script = self.volume_script(bootstrap=bootstrap, existing='vol-0123456789abcdef0')
            for serial, expected_status in (('vol-wrong', 1), ('vol0123456789abcdef0', 0)):
                with self.subTest(bootstrap=bootstrap, serial=serial):
                    _ = (self.root / 'serial').write_text(serial)
                    original = {'TYPE': b'ext4\n', 'LABEL': b'aws-home\n', 'evidence': b'keep'}
                    for key, value in original.items():
                        _ = (self.root / key).write_bytes(value)
                    result = self.run_bash(script)
                    self.assertEqual(result.returncode, expected_status, result.stderr)
                    self.assertEqual({key: (self.root / key).read_bytes() for key in original}, original)
                    self.assertFalse((self.root / 'formats').exists())

    def test_bootstrap_formats_matching_blank_media_once_and_full_host_refuses_it(self) -> None:
        self.install_volume_fakes()
        _ = (self.root / 'serial').write_text('vol0123456789abcdef0')
        full = self.volume_script(bootstrap=False)
        refused = self.run_bash(full)
        self.assertNotEqual(refused.returncode, 0)
        self.assertFalse((self.root / 'formats').exists())
        bootstrap = self.volume_script(bootstrap=True)
        for attempt in range(2):
            with self.subTest(attempt=attempt):
                result = self.run_bash(bootstrap)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((self.root / 'formats').read_text(), 'format\n')
        kept = self.run_bash(full)
        self.assertEqual(kept.returncode, 0, kept.stderr)
        self.assertEqual((self.root / 'formats').read_text(), 'format\n')

    def test_earlier_day_request_costs_do_not_reduce_the_next_days_reconciled_guard_totals(self) -> None:
        self.text_setting('accounting_zone', 'America/New_York')
        self.costs({'fixed_usd_per_month': '0', 'cost_explorer_read_usd': '0',
                    'observation_max_age_seconds': '259200', 'limit_reserve_usd': '0', 'daily_limit_usd': '18'})
        start = int(datetime(2026, 2, 1, tzinfo=UTC).timestamp())
        end = start + 86400
        self.set_clock(end + 3600)
        book = ledger.CostLedger(self.state)
        book.record_inventory(ledger.ResourceInventory(start, (ledger.PricedResource('volume', Decimal(0)),), 'fixture'))
        book.observe(ledger.InstanceObservation(start, 'stopped', 'confirmed stopped'))
        book.reconcile(ledger.CostReconciliation(self.now, start, end, Decimal(24), 'UTC bill'))
        before = book.guard(self.settings, 'deployment', self.now, lambda _event: True)
        before_costs = narrow(self, before.evidence.costs, ledger.CostEstimate)
        self.assertEqual(before_costs.daily.usd, Decimal(19))
        self.assertEqual(before_costs.monthly.usd, Decimal(19))
        self.assertIsInstance(before, ledger.SpendingRefused)
        request = ledger.ComputeRequest(start, 'replacement', 'us-east-1', Decimal(2))
        book.record_compute_request(request)
        book.resolve_compute_request(request, ledger.ComputeResourcesAbsent(start + 5 * 3600))
        after = book.guard(self.settings, 'deployment', self.now, lambda _event: True)
        after_costs = narrow(self, after.evidence.costs, ledger.CostEstimate)
        self.assertGreaterEqual(after_costs.daily.usd, before_costs.daily.usd)
        self.assertGreaterEqual(after_costs.monthly.usd, before_costs.monthly.usd)
        self.assertIsInstance(after, ledger.SpendingRefused)
        self.assertEqual(sum(row.confirmed_uptime_s for row in after_costs.intervals), 0)

    def test_lower_price_keeps_each_established_day_and_the_guard_refusal(self) -> None:
        self.text_setting('accounting_zone', 'America/New_York')
        self.costs({'fixed_usd_per_month': '0', 'cost_explorer_read_usd': '0',
                    'observation_max_age_seconds': '259200', 'price_max_age_seconds': '259200',
                    'fallback_usd_per_hour': '1', 'limit_reserve_usd': '0', 'daily_limit_usd': '20'})
        start = int(datetime(2026, 2, 1, tzinfo=UTC).timestamp())
        end = start + 86400
        self.set_clock(end)
        book = ledger.CostLedger(self.state)
        book.record_inventory(ledger.ResourceInventory(start, (ledger.PricedResource('volume', Decimal(0)),), 'fixture'))
        book.observe(ledger.InstanceObservation(start, 'running', 'confirmed running'))
        book.observe(ledger.InstanceObservation(end, 'stopped', 'confirmed stopped'))
        book.record_price(ledger.PriceSample(start, Decimal('0.1'), 'earlier day price'))
        book.record_price(ledger.PriceSample(start + 5 * 3600, Decimal(1), 'later day price'))
        book.reconcile(ledger.CostReconciliation(self.now, start, end, Decimal(24), 'Cost Explorer'))
        before = book.guard(self.settings, 'deployment', self.now, lambda _event: True)
        self.assertIsInstance(before, ledger.SpendingRefused)
        before_costs = narrow(self, before.evidence.costs, ledger.CostEstimate)
        book.record_price(ledger.PriceSample(start + 6 * 3600, Decimal('0.1'), 'lower later price'))
        reopened = ledger.CostLedger(self.state)
        after = reopened.guard(self.settings, 'deployment', self.now, lambda _event: True)
        after_costs = narrow(self, after.evidence.costs, ledger.CostEstimate)
        self.assertGreaterEqual(after_costs.daily.usd, before_costs.daily.usd)
        self.assertGreaterEqual(after_costs.monthly.usd, before_costs.monthly.usd)
        self.assertIsInstance(after, ledger.SpendingRefused)
        saved = reopened.path.read_bytes()
        snapshot = reopened.read_snapshot()
        readonly = ledger.costs_from_evidence(snapshot, aws_config.read_settings(self.settings), self.now)
        self.assertEqual(reopened.path.read_bytes(), saved)
        self.assertEqual(snapshot, reopened.read_snapshot())
        self.assertEqual(readonly.daily, after_costs.daily)

        self.replace('offload', 'on')
        charges = self.state / 'charges'
        charges.mkdir()
        reading = ledger.ChargeRead('2026-02-01', self.now + 1, start, end, Decimal(24), Decimal(0), Decimal(0))
        _ = (charges / '2026-02-01.json').write_text(json.dumps(ledger.charge_record(reading)))
        recovered = reopened.refresh_charges(self.settings, self.now + 1,
                                             lambda _start, _end: self.fail('recover the saved reply first'))
        self.assertEqual(recovered, reading)
        resolved = reopened.guard(self.settings, 'deployment', self.now + 1, lambda _event: True)
        resolved_costs = narrow(self, resolved.evidence.costs, ledger.CostEstimate)
        self.assertGreaterEqual(resolved_costs.daily.usd, before_costs.daily.usd)
        self.assertGreaterEqual(resolved_costs.monthly.usd, before_costs.monthly.usd)
        self.assertIsInstance(resolved, ledger.SpendingRefused)

    def test_matching_bill_adds_no_reconciliation_charge(self) -> None:
        self.costs({'fixed_usd_per_month': '0', 'cost_explorer_read_usd': '0',
                    'observation_max_age_seconds': '259200', 'price_max_age_seconds': '259200'})
        start = self.now - 86400
        book = ledger.CostLedger(self.state)
        book.record_inventory(ledger.ResourceInventory(start, (ledger.PricedResource('volume', Decimal(0)),), 'fixture'))
        book.observe(ledger.InstanceObservation(start, 'running', 'confirmed running'))
        book.record_price(ledger.PriceSample(start, Decimal(1), 'confirmed price'))
        book.reconcile(ledger.CostReconciliation(self.now, start, self.now, Decimal(24), 'UTC bill'))
        costs = book.estimate(aws_config.read_settings(self.settings), self.now)
        self.assertEqual(ledger.period_cost(costs.intervals, start, self.now), Decimal(24))
        self.assertEqual(sum(r.usd for r in costs.intervals if r.kind == 'reconciliation'), Decimal(0))

    def test_readonly_measurements_leave_reconciled_day_totals_unestablished(self) -> None:
        self.costs({'fixed_usd_per_month': '0', 'cost_explorer_read_usd': '0'})
        start = self.now - 3600
        book = ledger.CostLedger(self.state)
        book.record_inventory(ledger.ResourceInventory(start, (ledger.PricedResource('volume', Decimal(0)),), 'fixture'))
        book.observe(ledger.InstanceObservation(start, 'stopped', 'confirmed stopped'))
        book.observe(ledger.InstanceObservation(self.now, 'stopped', 'fresh confirmed stopped'))
        book.reconcile(ledger.CostReconciliation(self.now, start, self.now, Decimal(2), 'UTC bill'))
        saved = book.path.read_bytes()
        snapshot = book.read_snapshot()
        costs = ledger.costs_from_evidence(snapshot, aws_config.read_settings(self.settings), self.now)
        self.assertEqual(costs.daily.usd, Decimal(2))
        self.assertEqual(snapshot.reconciliations[0].established_days, ())
        self.assertEqual(book.path.read_bytes(), saved)
        _ = book.estimate(aws_config.read_settings(self.settings), self.now)
        reopened = ledger.CostLedger(self.state).read_snapshot()
        self.assertEqual(reopened.reconciliations[0].established_days,
                         (ledger.EstablishedDayCost(DAY, Decimal(2)),))

    def test_estimate_preserves_distinct_replies_for_the_same_period(self) -> None:
        self.costs({'fixed_usd_per_month': '0', 'cost_explorer_read_usd': '0'})
        start = self.now - 60
        book = ledger.CostLedger(self.state)
        book.record_inventory(ledger.ResourceInventory(start, (ledger.PricedResource('volume', Decimal(0)),), 'fixture'))
        book.observe(ledger.InstanceObservation(start, 'stopped', 'confirmed stopped'))
        readings = (ledger.CostReconciliation(self.now, start, self.now, Decimal(2), 'UTC bill'),
                    ledger.CostReconciliation(self.now, start, self.now, Decimal(1), 'UTC bill'))
        for reading in readings:
            book.reconcile(reading)
        costs = book.estimate(aws_config.read_settings(self.settings), self.now)
        self.assertEqual(costs.daily.usd, Decimal(2))
        self.assertEqual(tuple(ledger.CostLedger(self.state).read_snapshot().reconciliations), readings)

    def test_money_and_measurement_use_the_configuration_finite_number_rule(self) -> None:
        for value in ('1e400', 'nan', 'inf'):
            for parser in (aws_config.money, measurement.number):
                with self.subTest(value=value, parser=parser.__name__):
                    with self.assertRaises(ValueError):
                        _ = parser(value)

    def test_fixed_deployment_values_use_the_source_limits(self) -> None:
        for key, value, normal in (('data_volume_gib', '501', '500'), ('data_volume_iops', '6001', '6000'),
                                   ('data_volume_throughput', '501', '500'), ('budget_stop_percent', '99', '100'),
                                   ('instance_type', 'm7a.8xlarge', 'm7a.4xlarge')):
            with self.subTest(key=key):
                self.replace(key, value)
                with self.assertRaises(ValueError):
                    _ = aws_config.read_deployment_settings(self.settings)
                self.replace(key, normal)

    def test_durable_decision_check_rejects_a_superseded_decision(self) -> None:
        subject = self.deployment()
        _ = subject.prepare()
        book = ledger.CostLedger(self.state)
        allowed = ledger.spending_guard(self.settings, self.state, 'deployment', clock=lambda: self.now)
        self.assertTrue(book.decision_is_current(allowed))
        self.replace('daily_limit_usd', '1')
        _ = ledger.spending_guard(self.settings, self.state, 'deployment', clock=lambda: self.now, notify=lambda _event: True)
        self.assertFalse(book.decision_is_current(allowed))
        self.assertIsInstance(book.permission_for_decision(allowed), ledger.PermissionUnavailable)

    def test_service_retains_its_interval_after_a_bad_live_read(self) -> None:
        self.replace('offload', 'on')
        calls: list[int] = []
        delays: list[float] = []
        number = conf.ConfigRead.number
        def action() -> None:
            calls.append(len(calls))
            if len(calls) == 3:
                self.replace('offload', 'off')
        def interval(settings: conf.ConfigRead, key: str) -> float:
            if len(calls) == 2:
                raise ValueError('bad live interval')
            return number(settings, key)
        with mock.patch.object(conf.ConfigRead, 'number', interval):
            deploy_service_loop = aws_config.service_loop
            deploy_service_loop(self.settings, 'tick_interval_s', action, pause=delays.append)
        self.assertEqual(len(calls), 3)
        self.assertEqual(delays, [30, 30])

    def test_service_refuses_work_when_the_first_interval_read_fails(self) -> None:
        self.replace('offload', 'on')
        calls: list[str] = []
        delays: list[float] = []
        with mock.patch.object(conf.ConfigRead, 'number', side_effect=ValueError('bad initial interval')):
            with self.assertRaisesRegex(ValueError, 'bad initial interval'):
                aws_config.service_loop(self.settings, 'tick_interval_s', lambda: calls.append('tick'), pause=delays.append)
        self.assertEqual(calls, [])
        self.assertEqual(delays, [])

    def test_remote_serve_uses_the_configured_state_directory(self) -> None:
        self.replace('offload', 'on')
        directory = Path.home() / read_control_settings(self.settings).remote_state_directory
        agent = remote.RemoteAgent(self.settings, directory)
        with (mock.patch.object(sys, 'argv', ['remote.py', 'serve', '--settings', str(self.settings)]),
              mock.patch.object(remote, 'RemoteAgent', return_value=agent) as factory,
              mock.patch.object(remote, 'service_loop') as loop):
            self.assertEqual(remote.main(), 0)
            factory.assert_called_once_with(self.settings, directory)
            loop.assert_called_once_with(self.settings, 'interruption_poll_s', agent.tick)

    def test_prepare_reads_the_template_once_and_uses_its_guard_decision(self) -> None:
        original = Path.read_bytes
        reads: list[Path] = []
        def read(path: Path) -> bytes:
            if path == deploy.TEMPLATE:
                reads.append(path)
            return original(path)
        with mock.patch.object(Path, 'read_bytes', read):
            _ = self.deployment().prepare()
        self.assertEqual(reads, [deploy.TEMPLATE])
        self.assertEqual(ledger.mapping(remote.read_record(self.bundle / 'preflight.json')['decision'])['purpose'], 'deployment')

    def test_deployment_settings_validate_the_public_source_inputs(self) -> None:
        for key, value in (('scripts_source_url', 'https://user:secret@example.invalid/a'),
                           ('scripts_source_commit', 'main'), ('scripts_source_sha256', 'unverified')):
            with self.subTest(key=key):
                settings = aws_config.read_deployment_settings(self.settings)
                self.replace(key, value)
                with self.assertRaises(ValueError):
                    _ = aws_config.read_deployment_settings(self.settings)
                self.replace(key, str(cast(object, getattr(settings, key))))

    def test_shared_aws_cli_rejects_a_malformed_stack_reply(self) -> None:
        subject = self.deployment()
        _ = subject.prepare()
        _ = (self.root / 'describe-stacks.json').write_text('[]')
        with self.assertRaisesRegex(aws_target.AwsUnavailable, 'invalid reply'):
            _ = subject.apply(approve_resources=True)
        self.assertEqual(self.commands('deploy'), [])


    def test_replacement_preparation_accepts_a_confirmed_deleted_instance(self) -> None:
        subject = self.deployment()
        _ = subject.prepare()
        outputs = subject.apply(approve_resources=True)
        (self.root / 'fake-stack.json').unlink()
        _ = (self.root / 'missing-instance').write_text('deleted')
        _ = (self.state / 'down-complete.json').write_text((self.state / 'deployment.json').read_text())
        self.replace('stack_name', 'replacement-build')
        self.replace('existing_data_volume_id', outputs['DataVolumeId'])
        self.set_clock(self.now + 600)
        _ = subject.prepare()
        self.assertEqual(ledger.CostLedger(self.state).read_snapshot().observations[-1].state, 'terminated')
        self.assertEqual(len(self.commands('deploy')), 2)
