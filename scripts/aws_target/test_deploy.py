"""Exercise guarded deployment with a private AWS executable and no network."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from decimal import Decimal
from functools import lru_cache
from io import StringIO
import json
import os
from pathlib import Path
import re
import runpy
import shlex
import shutil
import subprocess
import sys
import threading
import time
from types import CodeType, SimpleNamespace
from typing import ClassVar, cast, override
from unittest import mock
from zoneinfo import ZoneInfo


from scripts.aws_target import admin_secrets, aws_target, box, config as aws_config, deploy, gate_box, gate_trial, gpu_size_trial, ledger, remote
from scripts.aws_target.ledger_test_support import CostFixture, account_reply, text_setting
from scripts.aws_target.measurement_test_support import DAY, narrow
from scripts.aws_target.test_box import SYSTEM, FakeTailnet, write_aws_claim
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
    if '--filters' in args:
        reply({'Reservations': []}); sys.exit(0)
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
elif operation == 'get-service-quota':
    reply({'Quota': {'Value': 128}})
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
    """Keep the gate stack, the saved gate image and its snapshot in local test state."""
    def __init__(self, failure: str = '') -> None:
        self.failure: str = failure
        self.commands: list[list[str]] = []
        self.version: int = 1
        self.gate_changes: list[dict[str, object]] = []
        self.image_parameter: str = 'ami-old'
        self.manifest: Path = Path()
        self.live_template: dict[str, object] = {'Resources': {
            'GateExpiryFunction': {'live': 'keep'}, 'GateScalerPolicy': {'Properties': {
                'PolicyDocument': {'Statement': [{'Effect': 'Allow', 'Action': ['ec2:RunInstances'],
                    'Resource': {'Fn::Sub': 'arn:${AWS::Partition}:ec2:${AWS::Region}::image/${GateImage}'}}]}}}}}
        self.images: dict[str, dict[str, object]] = {
            'ami-old': {'ImageId': 'ami-old', 'State': 'available', 'Tags': self.tags(),
                'BlockDeviceMappings': [{'DeviceName': '/dev/xvda', 'Ebs': {'SnapshotId': 'snap-old', 'VolumeSize': 32}}]},
            'ami-builder': {'ImageId': 'ami-builder', 'Tags': [{'Key': box.IMAGE_OWNER_TAG, 'Value': 'true'}]}}
        self.snapshots: dict[str, dict[str, object]] = {
            'snap-old': {'SnapshotId': 'snap-old', 'Tags': self.tags()},
            'snap-builder': {'SnapshotId': 'snap-builder', 'Tags': [{'Key': box.IMAGE_OWNER_TAG, 'Value': 'true'}]}}

    @staticmethod
    def tags() -> list[dict[str, str]]:
        return [{'Key': gate_box.GATE_IMAGE_OWNER_TAG, 'Value': 'true'}]

    def __call__(self, arguments: Sequence[str], **_options: object) -> subprocess.CompletedProcess[str]:
        args = list(arguments)
        self.commands.append(args)
        operation = args[args.index('--region') + 3]
        reply: dict[str, object] = {}
        error = ''
        if operation == 'get-cost-and-usage':
            reply = account_reply(arguments)
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
        elif operation in ('describe-images', 'describe-snapshots'):
            owner = ledger.text(ledger.rows({'rows': cast(object, json.loads(args[args.index('--filters') + 1]))},
                                            'rows')[0], 'Name').removeprefix('tag:')
            tags = [{'Key': owner, 'Value': 'true'}]
            if operation == 'describe-images':
                reply = {'Images': [row for row in self.images.values() if row.get('Tags') == tags]}
            else:
                reply = {'Snapshots': [row for row in self.snapshots.values() if row['Tags'] == tags]}
        elif operation == 'deregister-image':
            del self.images[args[args.index('--image-id') + 1]]
        elif operation == 'delete-snapshot':
            del self.snapshots[args[args.index('--snapshot-id') + 1]]
        else:
            raise AssertionError(operation)
        if self.manifest.exists():
            record = remote.read_record(self.manifest)
            selected = ledger.text(ledger.rows(record, 'regions')[0] if 'regions' in record else record, 'image')
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
        values = {
            'admin_secret_file': str(self.root / 'admin.gpg'), 'deployment_profile': 'build-admin', 'stack_name': 'fixture-build',
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
        gates = self.root / 'cloud_gate.conf'
        _ = gates.write_bytes((deploy.TEMPLATE.parents[3] / 'config/cloud_gate.conf').read_bytes())
        text_setting(gates, 'regions', 'us-east-1')
        # Quota cases assume these targets; pin them in place so a /tune of the shipped values never breaks a test.
        _ = gates.write_text(re.sub(r'^(gpu_spot_quota_request_vcpus)=.*$', r'\1=4',
                                    re.sub(r'^(spot_quota_request_vcpus)=.*$', r'\1=128', gates.read_text(), flags=re.M), flags=re.M))
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


    def deployment(self) -> deploy.Deployment:
        return deploy.Deployment(self.settings, self.state, self.bundle, clock=lambda: self.now,
                                 notify=lambda _event: True)

    def previous_output(self, output: str, command: str) -> str:
        pending: list[str] = []
        previous: list[str] = []
        for line in output.splitlines():
            if not line.startswith(command + ': ') or line.startswith((command + ': refused:', command + ': image kept:')):
                previous.append(line)
                continue
            message = line.removeprefix(command + ': ')
            done = re.fullmatch(r'(.+) done in (\d+) s', message)
            heartbeat = re.fullmatch(r'still (.+), (\d+) min', message)
            if done:
                self.assertTrue(pending, line)
                self.assertEqual(pending.pop(), done[1])
            elif heartbeat:
                self.assertIn(heartbeat[1], pending)
            else:
                pending.append(message.removesuffix(' (can take several minutes)'))
        self.assertEqual(pending, [])
        return ''.join(line + '\n' for line in previous)

    def test_deployment_step_prints_start_and_elapsed_time(self) -> None:
        output = StringIO()
        with redirect_stdout(output), mock.patch.object(time, 'monotonic', side_effect=[10.0, 12.0]):
            with deploy.step('remove-builder', 'updating the stack', self.settings, hint='can take several minutes'):
                pass
        self.assertEqual(output.getvalue().splitlines(), [
            'remove-builder: updating the stack (can take several minutes)',
            'remove-builder: updating the stack done in 2 s'])

    def test_deployment_step_prints_heartbeat_until_completion(self) -> None:
        self.replace('deploy_progress_interval_s', '0.001')
        heartbeat = threading.Event()

        class ProgressOutput(StringIO):
            @override
            def write(self, text: str) -> int:
                count = super().write(text)
                if 'remove-builder: still updating the stack,' in text:
                    heartbeat.set()
                return count

        output = ProgressOutput()
        with redirect_stdout(output):
            with deploy.step('remove-builder', 'updating the stack', self.settings, hint='can take several minutes'):
                self.assertTrue(heartbeat.wait(TEST_HANG_GUARD_SECS))
        self.assertRegex(output.getvalue(), r'remove-builder: still updating the stack, \d+ min\n')
        self.assertEqual(output.getvalue().splitlines()[0],
                         'remove-builder: updating the stack (can take several minutes)')
        self.assertRegex(output.getvalue().splitlines()[-1], r'^remove-builder: updating the stack done in \d+ s$')
        self.assertEqual(self.previous_output(output.getvalue(), 'remove-builder'), '')
        self.assertFalse(any(worker.name == 'deployment-progress' for worker in threading.enumerate()))

    def test_stack_wait_uses_its_own_timeout_through_the_admin_executor(self) -> None:
        self.replace('stack_wait_timeout_s', '3600')
        calls: list[tuple[tuple[str, ...], float]] = []

        def execute(arguments: Sequence[str], *, capture_output: bool, text: bool,
                    timeout: float, check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
            calls.append((tuple(arguments), timeout))
            return aws_target.execute_command(arguments, capture_output=capture_output, text=text,
                                              timeout=timeout, check=check, env=env)

        subject = deploy.Deployment(self.settings, self.state, self.bundle, execute=execute,
                                    clock=lambda: self.now, notify=lambda _event: True)
        _ = subject.prepare()
        _ = subject.apply(approve_resources=True)
        waits = [(arguments, timeout) for arguments, timeout in calls if 'cloudformation' in arguments and 'wait' in arguments]
        self.assertTrue(waits)
        self.assertEqual({timeout for _, timeout in waits}, {3600})
        ordinary = [(arguments, timeout) for arguments, timeout in calls if 'wait' not in arguments]
        self.assertTrue(ordinary)
        self.assertEqual({timeout for _, timeout in ordinary}, {30})

    def test_secrets_command_prints_progress_without_exposing_credentials(self) -> None:
        arguments = ['deploy.py', 'secrets', '--settings', str(self.settings), '--directory', str(self.state),
                     '--output', str(self.bundle)]
        with (mock.patch.object(sys, 'argv', arguments), mock.patch.object(admin_secrets, 'configure') as configure,
              redirect_stdout(StringIO()) as output):
            self.assertEqual(deploy.main(), 0)
        configure.assert_called_once_with(self.settings)
        self.assertEqual(output.getvalue().splitlines()[0], 'secrets: saving encrypted credentials')
        self.assertEqual(self.previous_output(output.getvalue(), 'secrets'), '')

    def test_gate_trial_command_prints_progress_around_the_trial(self) -> None:
        arguments = ['deploy.py', 'gate-trial', '--base', 'base', '--changed', 'changed', '--modules', 'fixture/test_ok.py']
        with (mock.patch.object(sys, 'argv', arguments), mock.patch.object(gate_trial, 'main', return_value=0) as trial,
              redirect_stdout(StringIO()) as output):
            self.assertEqual(deploy.main(), 0)
        trial.assert_called_once_with(arguments[2:], conf.path('aws_target'), self.state)
        self.assertEqual(output.getvalue().splitlines()[0], 'gate-trial: running the trial')
        self.assertEqual(self.previous_output(output.getvalue(), 'gate-trial'), '')

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
            (ledger.PricedResource('gate-shared', aws_config.money('1')),
             ledger.PricedResource('gate-image', aws_config.money('1.60'))), 'fixture', builder_removed=True))
        _ = (subject.directory / 'deployment.json').write_text(json.dumps({
            'stack_id': 'fixture-stack', 'stack': 'fixture-build'}))
        _ = (subject.directory / 'gate-deployment.json').write_text(json.dumps({
            'deployment_id': 'fixture', 'bucket': 'fixture-bucket', 'template': 'fixture-template',
            'version': '1', 'security_group': 'fixture-group', 'revision': 'f' * 40, 'image': 'ami-old',
            'subnets': [{'id': 'fixture-subnet', 'zone': 'fixture-zone'}]}))
        (subject.directory / 'gate-image').mkdir()
        _ = (subject.directory / 'gate-image/image.json').write_text('{"image_id":"ami-old"}')
        _ = (subject.directory / 'image.json').write_text('{"image_id":"ami-builder"}')
        fake = GateImageAws(failure)
        fake.manifest = subject.directory / 'gate-deployment.json'
        _ = self.enterContext(mock.patch.object(subject, '_api', return_value=aws_target.AwsCli(
            'fixture-admin', 'us-east-1', 1, execute=fake)))
        return subject, fake

    def test_remove_gate_image_dry_run_needs_no_credentials_and_writes_nothing(self) -> None:
        self.credentials.unlink()
        before = {path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()}
        with mock.patch.object(admin_secrets.AdminSecretSession, 'get') as credentials, redirect_stdout(StringIO()) as output:
            self.assertTrue(self.deployment().remove_images())
        credentials.assert_not_called()
        self.assertIn('remove-images --approve-destroy', output.getvalue())
        self.assertEqual({path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()}, before)

    def test_remove_gate_image_switches_to_the_official_image_before_deleting_the_gate_image(self) -> None:
        subject, fake = self.gate_image_fixture()
        with redirect_stdout(StringIO()) as output:
            self.assertTrue(subject.remove_images(approve_destroy=True))
        self.assertIn('remove-images: done; gates boot from the official image', output.getvalue())
        manifest = remote.read_record(fake.manifest)
        home = ledger.rows(manifest, 'regions')[0]
        self.assertEqual((home['image'], home['version'], manifest['revision']), ('ami-fixture', '2', 'f' * 40))
        self.assertEqual(len(fake.gate_changes), 2)
        for index, change in enumerate(fake.gate_changes):
            template = ledger.mapping(change['Template'])
            self.assertEqual(ledger.mapping(template['Resources'])['GateExpiryFunction'], {'live': 'keep'})
            image_policy = ledger.mapping(ledger.mapping(ledger.mapping(
                ledger.mapping(template['Resources'])['GateScalerPolicy'])['Properties'])['PolicyDocument'])
            statements = ledger.rows(image_policy, 'Statement')
            rows = ledger.rows(change, 'Parameters')
            self.assertEqual(next(row for row in rows if row['ParameterKey'] == 'EnableCompute'),
                             {'ParameterKey': 'EnableCompute', 'UsePreviousValue': True})
            image_parameter = next(row for row in rows if row['ParameterKey'] == 'GateImage')
            if index == 0:
                self.assertEqual(statements[-1], {'Sid': 'GateImageTransition', 'Effect': 'Allow',
                    'Action': ['ec2:RunInstances'], 'Resource': {
                        'Fn::Sub': 'arn:${AWS::Partition}:ec2:${AWS::Region}::image/ami-old'}})
                self.assertEqual(image_parameter, {'ParameterKey': 'GateImage', 'ParameterValue': 'ami-fixture'})
            else:
                self.assertEqual(len(statements), 1)
                self.assertEqual(template, fake.live_template)
                self.assertEqual(image_parameter, {'ParameterKey': 'GateImage', 'UsePreviousValue': True})
        self.assertEqual(fake.launchable_images(), {'ami-fixture'})
        operations = [args[args.index('--region') + 3] for args in fake.commands]
        last_change = len(operations) - operations[::-1].index('execute-change-set') - 1
        self.assertLess(last_change, operations.index('deregister-image'))
        self.assertLess(operations.index('deregister-image'), operations.index('delete-snapshot'))
        self.assertEqual((fake.images, fake.snapshots), ({}, {}))
        self.assertFalse((subject.directory / 'gate-image').exists())
        self.assertFalse((subject.directory / 'image.json').exists())
        self.assertIn('no saved image remains', output.getvalue())
        inventories = ledger.CostLedger(subject.directory).read_snapshot().inventories
        self.assertTrue(inventories[-1].builder_removed)
        self.assertEqual(inventories[-1].resources, (ledger.PricedResource('gate-shared', aws_config.money('1')),))
        # A second run finds nothing to change or delete.
        with redirect_stdout(StringIO()) as output:
            self.assertTrue(subject.remove_images(approve_destroy=True))
        self.assertIn('remove-images: nothing to remove', output.getvalue())
        self.assertEqual(len(fake.gate_changes), 2)

    def test_remove_images_preserves_regional_stacks_and_copied_images(self) -> None:
        subject, fake = self.gate_image_fixture()
        manifest = aws_config.gate_deployment(fake.manifest, 'us-east-1')
        regional: dict[str, object] = {'region': 'us-west-2', 'stack': 'fixture-west', 'image': 'ami-west'}
        manifest['regions'] = [*ledger.rows(manifest, 'regions'), regional]
        write_text_atomic(fake.manifest, json.dumps(manifest))
        records = {'regional-stacks.json': {'us-west-2': 'fixture-west'},
                   'regional-images.json': {'copy': {'region': 'us-west-2', 'image': 'ami-west'}}}
        for name, record in records.items():
            _ = (subject.directory / name).write_text(json.dumps(record))
        before = {name: (subject.directory / name).read_bytes() for name in records}
        with redirect_stdout(StringIO()):
            self.assertTrue(subject.remove_images(approve_destroy=True))
        self.assertEqual(ledger.rows(remote.read_record(fake.manifest), 'regions')[1], regional)
        self.assertEqual({name: (subject.directory / name).read_bytes() for name in records}, before)
        self.assertTrue(all(command[command.index('--region') + 1] == 'us-east-1' for command in fake.commands))

    def test_remove_gate_image_keeps_the_selected_image_launchable_when_a_step_fails(self) -> None:
        for failure in ('activate', 'waiter', 'manifest', 'narrow waiter'):
            with self.subTest(failure=failure):
                subject, fake = self.gate_image_fixture(failure)
                atomic_write = write_text_atomic
                def write(path: Path, text: str) -> None:
                    if path == fake.manifest and fake.failure == 'manifest':
                        raise OSError('fixture manifest write failure')
                    atomic_write(path, text)
                with mock.patch.object(deploy, 'write_text_atomic', side_effect=write), redirect_stdout(StringIO()):
                    with self.assertRaises((aws_target.AwsUnavailable, OSError)):
                        _ = subject.remove_images(approve_destroy=True)
                    selected = ledger.text(ledger.rows(remote.read_record(fake.manifest), 'regions')[0], 'image')
                    self.assertEqual(selected, 'ami-fixture' if failure == 'narrow waiter' else 'ami-old')
                    self.assertIn(selected, fake.launchable_images())
                    self.assertIn('snap-old', fake.snapshots)
                    self.assertTrue((subject.directory / 'gate-image').exists())
                    fake.failure = ''
                    self.assertTrue(subject.remove_images(approve_destroy=True))
                self.assertEqual(fake.launchable_images(), {'ami-fixture'})
                self.assertNotIn('snap-old', fake.snapshots)
                self.assertFalse((subject.directory / 'gate-image').exists())

    def gate_fixture(self, change: str = 'GateBucket') -> tuple[deploy.Deployment, list[list[str]]]:
        subject = self.update_fixture()
        self.set_clock(self.now + 1)
        # The fixture keeps two sizes, so the shipped type list can change without these cases.
        shipped = (deploy.TEMPLATE.parents[3] / 'config/cloud_gate.conf').read_text()
        _ = (self.root / 'cloud_gate.conf').write_text(
            re.sub(r'(?m)^instance_types=.*$', 'instance_types=m7a.8xlarge m7a.4xlarge', shipped))
        text_setting(self.settings, 'region', 'us-east-2')
        text_setting(self.settings, 'availability_zone', 'us-east-2a')
        text_setting(self.root / 'cloud_gate.conf', 'regions', 'us-east-2')
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

    def test_gate_up_records_regional_stack_after_home_stack(self) -> None:
        subject, commands = self.gate_fixture()
        text_setting(self.root / 'cloud_gate.conf', 'regions', 'us-east-2 us-west-2')
        west: dict[str, object] = {'region': 'us-west-2', 'image': 'ami-west', 'subnets': [],
            'template': 'lt-west', 'version': '1', 'security_group': 'sg-west', 'stack': 'fixture-west'}
        def regional(_api: aws_target.AwsCli, _settings: aws_config.DeploymentSettings, _home: dict[str, str],
                     _parameters: dict[str, str], region: str) -> dict[str, object]:
            self.assertTrue(any('execute-change-set' in command for command in commands))
            self.assertEqual(region, 'us-west-2')
            return west
        with mock.patch.object(subject, 'regional_gate_stack', side_effect=regional):
            self.assertTrue(subject.gate_up(approve_resources=True))
        saved = remote.read_record(self.state / 'gate-deployment.json')
        self.assertEqual([row['region'] for row in ledger.rows(saved, 'regions')], ['us-east-2', 'us-west-2'])
        self.assertEqual(ledger.rows(saved, 'regions')[1], west)

    def test_prices_lists_all_region_type_zone_combinations_without_ids(self) -> None:
        subject = self.deployment()
        text_setting(self.settings, 'region', 'us-east-2')
        text_setting(self.settings, 'availability_zone', 'us-east-2a')
        _ = (self.root / 'cloud_gate.conf').write_bytes((deploy.TEMPLATE.parents[3] / 'config/cloud_gate.conf').read_bytes())
        text_setting(self.root / 'cloud_gate.conf', 'regions', 'us-east-2')
        text_setting(self.root / 'cloud_gate.conf', 'spot_candidate_regions', 'us-west-2 us-east-1 us-east-2 us-west-2')
        gpu_types = ('g6.xlarge', 'g4dn.xlarge', 'g4ad.xlarge', 'g5.xlarge')
        price_regions: list[str] = []
        def execute(arguments: Sequence[str], **_options: object) -> subprocess.CompletedProcess[str]:
            args = list(arguments)
            region = args[args.index('--region') + 1]
            if 'get-service-quota' in args:
                code = args[args.index('--quota-code') + 1]
                self.assertIn(code, ('L-34B43A08', 'L-3819A6DF'))
                reply: dict[str, object] = {'Quota': {'Value': 48 if code == 'L-3819A6DF' else 128}}
            elif 'describe-availability-zones' in args:
                reply = {'AvailabilityZones': [{'ZoneName': region + 'a'}]}
            else:
                self.assertIn('describe-spot-price-history', args)
                price_regions.append(region)
                sizes = args[args.index('--instance-types') + 1:args.index('--product-descriptions')]
                self.assertTrue(set(gpu_types).issubset(sizes))
                self.assertEqual(len(sizes), len(set(sizes)))
                reply = {'SpotPriceHistory': [
                    {'InstanceType': size, 'AvailabilityZone': region + 'a', 'SpotPrice': '.25', 'Timestamp': '2026-01-15T00:00:00Z'}
                    for size in ('m7a.8xlarge', *gpu_types)]}
            return subprocess.CompletedProcess(args, 0, json.dumps(reply), '')
        with (mock.patch.object(subject, '_api', return_value=aws_target.AwsCli('fixture', 'us-east-2', 1, execute=execute)),
              redirect_stdout(StringIO()) as output):
            subject.prices()
        lines = output.getvalue().splitlines()
        self.assertEqual(price_regions, ['us-east-2', 'us-west-2', 'us-east-1'])
        self.assertIn('us-east-2 m7a.8xlarge us-east-2a 0.25', lines)
        for region in ('us-east-2', 'us-west-2', 'us-east-1'):
            self.assertIn(f'{region}: Spot quota 128 vCPUs', lines)
            self.assertEqual(lines[lines.index(f'{region}: Spot quota 128 vCPUs') + 1],
                             f'{region}: G and VT Spot quota 48 vCPUs')
            for size in ('r7a.8xlarge', 'm8a.8xlarge', 'c8a.8xlarge'):
                self.assertIn(f'{region} {size} {region}a unavailable', lines)
            for size in gpu_types:
                self.assertIn(f'{region} {size} {region}a 0.25', lines)
        self.assertTrue(all('ami-' not in line and 'arn:' not in line for line in lines))

    def test_prices_and_up_report_regional_quotas_through_the_admin_executor(self) -> None:
        cases: tuple[tuple[str, dict[str, int | str], int], ...] = (
            ('m7a.4xlarge m7a.8xlarge', {'us-east-1': 32}, 32),
            ('m7a.4xlarge m7a.8xlarge', {'us-east-1': 16}, 32),
            ('m7a.4xlarge m7a.8xlarge', {'us-east-1': 'AccessDenied: fixture-admin-secret'}, 32),
            ('m7a.4xlarge m7a.8xlarge', {'us-east-1': 64, 'us-west-2': 32}, 32),
            ('m7a.4xlarge m7a.8xlarge', {'us-east-1': 'AccessDenied: fixture-admin-secret', 'us-west-2': 64}, 32),
            ('m7a.4xlarge m7a.8xlarge', {'us-east-1': 'timeout', 'us-west-2': 32}, 32),
            ('m7a.xlarge', {'us-east-1': 2, 'us-west-2': 4}, 4),
            ('m7a.large', {'us-east-1': 1, 'us-west-2': 2}, 2),
            ('m7a', {'us-east-1': 1, 'us-west-2': 32}, 0),
            ('m7a.metal', {'us-east-1': 1, 'us-west-2': 32}, 0),
            ('m7a.unknown', {'us-east-1': 1, 'us-west-2': 32}, 0),
            ('m7a m7a.metal m7a.large m7a.8xlarge m7a.xlarge', {'us-east-1': 16, 'us-west-2': 32}, 32))
        for command in ('prices',):
            for instance_types, quotas, largest in cases:
                with self.subTest(command=command, quotas=quotas, instance_types=instance_types):
                    gates = self.root / 'cloud_gate.conf'
                    text_setting(gates, 'regions', ' '.join(quotas))
                    text_setting(gates, 'spot_candidate_regions', ' '.join(quotas))
                    text_setting(gates, 'instance_types', instance_types)
                    subject = self.deployment()
                    subject.admin.credentials = subject.admin.get()
                    calls: list[tuple[str, str]] = []
                    execute = subject.execute

                    def quota_execute(arguments: Sequence[str], *, capture_output: bool, text: bool,
                                      timeout: float, check: bool,
                                      env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
                        region = arguments[arguments.index('--region') + 1] if '--region' in arguments else ''
                        if 'get-service-quota' in arguments:
                            code = arguments[arguments.index('--quota-code') + 1]
                            self.assertNotIn('--profile', arguments)
                            self.assertIsNotNone(env)
                            self.assertEqual(cast(dict[str, str], env)['AWS_ACCESS_KEY_ID'], 'fixture-admin-id')
                            self.assertEqual(cast(dict[str, str], env)['AWS_SECRET_ACCESS_KEY'], 'fixture-admin-secret')
                            self.assertEqual(list(arguments[arguments.index('service-quotas'):]),
                                ['service-quotas', 'get-service-quota', '--service-code', 'ec2',
                                 '--quota-code', code, '--output', 'json'])
                            calls.append((region, code))
                            if code == 'L-3819A6DF':
                                return subprocess.CompletedProcess(arguments, 0, json.dumps({'Quota': {'Value': 48}}), '')
                            self.assertEqual(code, 'L-34B43A08')
                            quota = quotas[region]
                            if quota == 'timeout':
                                raise subprocess.TimeoutExpired('fixture-admin-secret', timeout)
                            return subprocess.CompletedProcess(arguments, 1 if isinstance(quota, str) else 0,
                                '' if isinstance(quota, str) else json.dumps({'Quota': {'Value': quota}}),
                                quota if isinstance(quota, str) else '')
                        if 'describe-availability-zones' in arguments:
                            return subprocess.CompletedProcess(arguments, 0,
                                json.dumps({'AvailabilityZones': [{'ZoneName': region + 'a'}]}), '')
                        if 'describe-spot-price-history' in arguments and '--availability-zone' not in arguments:
                            return subprocess.CompletedProcess(arguments, 0, json.dumps({'SpotPriceHistory': []}), '')
                        self.assertNotIn('describe-instance-types', arguments)
                        return execute(arguments, capture_output=capture_output, text=text,
                                       timeout=timeout, check=check, env=env)

                    subject.execute = quota_execute
                    with mock.patch.object(deploy, 'largest_gate_vcpus', wraps=deploy.largest_gate_vcpus) as count_vcpus:
                        with redirect_stdout(StringIO()) as captured:
                            subject.prices()
                        output = captured.getvalue()
                        self.assertIn('us-east-1 r7a.8xlarge us-east-1a unavailable', output)
                        count_vcpus.assert_called_once_with(tuple(instance_types.split()))
                    codes = ('L-34B43A08', 'L-3819A6DF') if command == 'prices' else ('L-34B43A08',)
                    self.assertEqual(calls, [(region, code) for region in quotas for code in codes])
                    self.assertEqual([line for line in output.splitlines() if ': G and VT' in line],
                                     [f'{region}: G and VT Spot quota 48 vCPUs' for region in quotas]
                                     if command == 'prices' else [])
                    lines = [line for line in output.splitlines()
                             if ': Spot' in line or ': cannot run' in line or ': gate vCPU' in line]
                    expected: list[str] = []
                    for region, quota in quotas.items():
                        if isinstance(quota, str):
                            line = next(line for line in lines if line.startswith(region + ': Spot vCPU quota unknown:'))
                            if quota == 'timeout':
                                self.assertIn('timed out', line)
                            else:
                                self.assertIn('AccessDenied: [redacted]', line)
                            expected.append(line)
                        else:
                            expected.append(f'{region}: Spot quota {quota} vCPUs')
                            if largest == 0:
                                expected.append(f'{region}: gate vCPU count is unknown')
                            elif quota < largest:
                                expected.append(f'{region}: cannot run one gate ({largest} vCPUs); ask AWS for a Spot vCPU quota increase in this region.')
                    self.assertEqual(lines, expected)
                    self.assertNotIn('fixture-admin-secret', output)

    def test_prices_prints_whole_quotas_and_the_header_after_the_passphrase_prompt(self) -> None:
        text_setting(self.root / 'cloud_gate.conf', 'spot_candidate_regions', 'us-east-1')
        subject = self.deployment()
        credentials = subject.admin.get()
        prompted = False

        def unlock() -> admin_secrets.AdminCredentials:
            nonlocal prompted
            if not prompted:
                print('Admin passphrase: ')
                prompted = True
            return credentials

        def execute(arguments: Sequence[str], **_options: object) -> subprocess.CompletedProcess[str]:
            if 'get-service-quota' in arguments:
                reply: dict[str, object] = {'Quota': {'Value': 300.0}}
            elif 'describe-availability-zones' in arguments:
                reply = {'AvailabilityZones': []}
            else:
                reply = {'SpotPriceHistory': []}
            return subprocess.CompletedProcess(arguments, 0, json.dumps(reply), '')

        subject.execute = execute
        with mock.patch.object(subject.admin, 'get', side_effect=unlock), redirect_stdout(StringIO()) as captured:
            subject.prices()
        lines = captured.getvalue().splitlines()
        self.assertEqual(lines, ['Admin passphrase: ', 'region type zone price', 'us-east-1: Spot quota 300 vCPUs',
                                'us-east-1: G and VT Spot quota 300 vCPUs'])

    def test_prices_continues_after_a_failed_g_quota_read_without_error_details(self) -> None:
        text_setting(self.root / 'cloud_gate.conf', 'regions', 'us-east-1 us-west-2')
        for failure, reason in (('denied', 'AccessDeniedException'), ('timeout', 'TimeoutExpired'), ('malformed', 'KeyError')):
            with self.subTest(failure=failure):
                subject = self.deployment()

                def execute(arguments: Sequence[str], **_options: object) -> subprocess.CompletedProcess[str]:
                    args = list(arguments)
                    region = args[args.index('--region') + 1]
                    reply: dict[str, object]
                    if 'get-service-quota' in args:
                        code = args[args.index('--quota-code') + 1]
                        if code == 'L-3819A6DF' and region == 'us-east-1':
                            if failure == 'denied':
                                return subprocess.CompletedProcess(args, 1, '',
                                    'An error occurred (AccessDeniedException) when calling GetServiceQuota: '
                                    + 'fixture-admin-secret arn:aws:iam::123456789012:user/private')
                            if failure == 'timeout':
                                raise subprocess.TimeoutExpired('fixture-admin-secret', 1)
                            reply = {}
                        else:
                            reply = {'Quota': {'Value': 48 if code == 'L-3819A6DF' else 128}}
                    elif 'describe-availability-zones' in args:
                        reply = {'AvailabilityZones': [{'ZoneName': region + 'a'}]}
                    else:
                        self.assertIn('describe-spot-price-history', args)
                        reply = {'SpotPriceHistory': []}
                    return subprocess.CompletedProcess(args, 0, json.dumps(reply), '')

                subject.execute = execute
                with redirect_stdout(StringIO()) as output:
                    subject.prices()
                lines = output.getvalue().splitlines()
                self.assertIn(f'us-east-1: G and VT Spot quota unknown: {reason}', lines)
                self.assertIn('us-west-2: G and VT Spot quota 48 vCPUs', lines)
                self.assertIn('us-west-2 r7a.8xlarge us-west-2a unavailable', lines)
                self.assertNotIn('fixture-admin-secret', output.getvalue())
                self.assertNotIn('arn:', output.getvalue())
                self.assertNotIn('123456789012', output.getvalue())

    def test_quota_status_prints_aligned_regional_blocks_without_writes(self) -> None:
        gates = self.root / 'cloud_gate.conf'
        home = conf.read(self.settings).text('region')
        other, candidate = 'us-west-2', 'eu-north-1'
        text_setting(gates, 'regions', home + ' ' + other)
        text_setting(gates, 'spot_candidate_regions', other + ' ' + candidate)
        text_setting(gates, 'credential_file', str(self.credentials))
        values = conf.read(gates)
        desired = {'L-34B43A08': values.whole('spot_quota_request_vcpus'),
                   'L-3819A6DF': values.whole('gpu_spot_quota_request_vcpus')}
        zone = 'Pacific/Honolulu'
        updated = datetime.fromtimestamp(self.now, UTC)
        created = datetime.fromtimestamp(self.now - 86400, UTC)
        date = updated.astimezone(ZoneInfo(zone)).strftime('%Y-%m-%d %H:%M:%S %Z')
        created_date = created.astimezone(ZoneInfo(zone)).strftime('%Y-%m-%d %H:%M:%S %Z')
        history = 'list-requested-service-quota-change-history-by-quota'
        calls: list[tuple[str, str, str]] = []
        subject = self.deployment()

        def execute(arguments: Sequence[str], *, capture_output: bool, text: bool,
                    timeout: float, check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
            self.assertTrue(capture_output and text)
            self.assertFalse(check)
            self.assertGreater(timeout, 0)
            self.assertNotIn('--profile', arguments)
            assert env is not None
            self.assertEqual(env['AWS_ACCESS_KEY_ID'], 'fixture-admin-id')
            self.assertEqual(env['AWS_SECRET_ACCESS_KEY'], 'fixture-admin-secret')
            self.assertEqual(env['AWS_SHARED_CREDENTIALS_FILE'], str(self.credentials))
            region = arguments[arguments.index('--region') + 1]
            operation = arguments[arguments.index('service-quotas') + 1]
            code = arguments[arguments.index('--quota-code') + 1]
            self.assertIn(operation, ('get-service-quota', history))
            self.assertNotIn('--no-paginate', arguments)
            self.assertNotIn('--max-items', arguments)
            calls.append((region, code, operation))
            reply: dict[str, object]
            if operation == 'get-service-quota':
                reply = {'Quota': {'Value': desired[code], 'QuotaName': 'private-name', 'QuotaArn': 'arn:private-quota'}}
            else:
                rows: list[dict[str, object]] = []
                if (region, code) == (home, 'L-34B43A08'):
                    rows = [{'DesiredValue': desired[code], 'Status': 'APPROVED', 'Created': created.timestamp(),
                             'LastUpdated': created.timestamp()},
                            {'DesiredValue': desired[code] + 1, 'Status': 'CASE_OPENED', 'CaseId': 'case-fixture',
                             'Created': created.isoformat(), 'LastUpdated': updated.isoformat(),
                             'Id': 'private-request', 'Requester': 'private-account', 'QuotaArn': 'arn:private-quota'}]
                elif (region, code) == (other, 'L-3819A6DF'):
                    rows = [{'DesiredValue': desired[code], 'Status': 'PENDING',
                             'Created': created.isoformat().replace('+00:00', 'Z')}]
                reply = {'RequestedQuotas': rows}
            return subprocess.CompletedProcess(arguments, 0, json.dumps(reply), '')

        subject.execute = execute
        before = {path.relative_to(self.root): path.read_bytes() if path.is_file() else None
                  for path in self.root.rglob('*')}
        with (mock.patch.dict(os.environ, {'USER_ZONE': zone}),
              mock.patch.object(deploy, 'Deployment', return_value=subject),
              mock.patch.object(sys, 'argv', ['deploy.py', 'quota-status', '--settings', str(self.settings)]),
              mock.patch.object(deploy, 'write_text_atomic', side_effect=AssertionError('The report must not write')),
              mock.patch.object(deploy, 'write_bytes_atomic', side_effect=AssertionError('The report must not write')),
              mock.patch.object(subject, '_guard', side_effect=AssertionError('The report must not change the ledger')),
              redirect_stdout(StringIO()) as output, redirect_stderr(StringIO()) as errors):
            self.assertEqual(deploy.main(), 0, errors.getvalue())
        after = {path.relative_to(self.root): path.read_bytes() if path.is_file() else None
                 for path in self.root.rglob('*')}
        self.assertEqual(before, after)
        self.assertEqual(calls, [(region, code, operation) for region, code in (
            (home, 'L-34B43A08'), (other, 'L-34B43A08'), (other, 'L-3819A6DF'), (candidate, 'L-3819A6DF'))
            for operation in ('get-service-quota', history)])
        lines = output.getvalue().splitlines()
        self.assertEqual([line for line in lines if line and not line.startswith((' ', 'Summary:'))],
                         [home, other, candidate])
        self.assertEqual([line.strip() for line in lines if line.startswith('  ') and not line.startswith('    ')], [
            f'Standard Spot: {desired["L-34B43A08"]} vCPUs applied',
            f'Standard Spot: {desired["L-34B43A08"]} vCPUs applied',
            f'G and VT Spot: {desired["L-3819A6DF"]} vCPUs applied',
            f'G and VT Spot: {desired["L-3819A6DF"]} vCPUs applied'])
        headers = [line for line in lines if 'Asked vCPUs' in line]
        requests = [line for line in lines if line.startswith('    ') and any(
            status in line for status in ('CASE_OPENED', 'APPROVED', 'PENDING'))]
        self.assertEqual([re.split(r' {2,}', line.strip()) for line in requests], [
            [str(desired['L-34B43A08'] + 1), 'CASE_OPENED', 'case-fixture', date],
            [str(desired['L-34B43A08']), 'APPROVED', '-', created_date],
            [str(desired['L-3819A6DF']), 'PENDING', '-', created_date]])
        for line, status in zip(requests[:2], ('CASE_OPENED', 'APPROVED')):
            self.assertEqual(line.index(status), headers[0].index('Status'))
            self.assertEqual(line.index(date if status == 'CASE_OPENED' else created_date), headers[0].index('Updated'))
        self.assertEqual(lines.count('    No requests.'), 2)
        needed = gpu_size_trial.required_vcpus(gpu_size_trial.settings())
        fits = f'{other}, {candidate}' if desired['L-3819A6DF'] >= needed else 'no region has enough applied quota'
        self.assertEqual(lines[-1], f'Summary: GPU trial needs {needed} vCPUs; quota fits: {fits}; '
            + f'open requests: {home} Standard Spot {desired["L-34B43A08"] + 1} vCPUs (CASE_OPENED), '
            + f'{other} G and VT Spot {desired["L-3819A6DF"]} vCPUs (PENDING)')
        for private in ('private-name', 'private-request', 'private-account', 'arn:', 'fixture-admin-id', 'fixture-admin-secret'):
            self.assertNotIn(private, output.getvalue() + errors.getvalue())

    def test_quota_status_continues_after_regional_failures_and_exits_one(self) -> None:
        gates = self.root / 'cloud_gate.conf'
        home = conf.read(self.settings).text('region')
        other = 'us-west-2'
        text_setting(gates, 'regions', home + ' ' + other)
        text_setting(gates, 'spot_candidate_regions', home + ' ' + other)
        history = 'list-requested-service-quota-change-history-by-quota'
        for failure in ('get-service-quota', history, 'timeout', 'uncoded'):
            with self.subTest(failure=failure):
                subject = self.deployment()
                calls: list[tuple[str, str, str]] = []

                def execute(arguments: Sequence[str], *, capture_output: bool, text: bool,
                            timeout: float, check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
                    _ = capture_output, text, check, env
                    region = arguments[arguments.index('--region') + 1]
                    operation = arguments[arguments.index('service-quotas') + 1]
                    code = arguments[arguments.index('--quota-code') + 1]
                    self.assertIn(operation, ('get-service-quota', history))
                    calls.append((region, code, operation))
                    if region == home and code == 'L-34B43A08':
                        if failure == 'timeout':
                            raise subprocess.TimeoutExpired('private-timeout', timeout)
                        if failure == 'uncoded' or operation == failure:
                            reason = 'private-error' if failure == 'uncoded' else (
                                'An error occurred (AccessDeniedException) when calling quota operation: private-error arn:private')
                            return subprocess.CompletedProcess(arguments, 1, '', reason)
                    reply: dict[str, object] = {'Quota': {'Value': conf.read(gates).whole('spot_quota_request_vcpus')}}
                    if operation == history:
                        reply = {'RequestedQuotas': []}
                    return subprocess.CompletedProcess(arguments, 0, json.dumps(reply), '')

                subject.execute = execute
                with (mock.patch.object(deploy, 'Deployment', return_value=subject),
                      mock.patch.object(sys, 'argv', ['deploy.py', 'quota-status', '--settings', str(self.settings)]),
                      redirect_stdout(StringIO()) as output, redirect_stderr(StringIO()) as errors):
                    self.assertEqual(deploy.main(), 1)
                expected = 'TimeoutExpired' if failure == 'timeout' else 'AwsUnavailable' if failure == 'uncoded' else 'AccessDeniedException'
                self.assertIn(f'    Quota status failed: {expected}', output.getvalue())
                self.assertIn((other, 'L-34B43A08', history), calls)
                self.assertIn((other, 'L-3819A6DF', history), calls)
                self.assertIn((home, 'L-3819A6DF', history), calls)
                summary = output.getvalue().splitlines()[-1]
                self.assertTrue(summary.startswith('Summary: GPU trial needs '))
                self.assertIn('request history unavailable: ' + home + ' Standard Spot', summary)
                self.assertIn('open requests: unknown', summary)
                for private in ('private-error', 'private-timeout', 'arn:'):
                    self.assertNotIn(private, output.getvalue() + errors.getvalue())

    def test_quota_status_summary_uses_the_trial_types_and_ignores_closed_requests(self) -> None:
        gates = self.root / 'cloud_gate.conf'
        home = conf.read(self.settings).text('region')
        text_setting(gates, 'regions', home)
        text_setting(gates, 'spot_candidate_regions', home)
        prices = gpu_size_trial.settings().spot_max_usd_per_hour
        sizes = sorted(prices, key=lambda name: aws_target.largest_gate_vcpus((name,)))
        applied = aws_target.largest_gate_vcpus((sizes[0],))
        asked = conf.read(gates).whole('gpu_spot_quota_request_vcpus')
        for size in (sizes[0], sizes[-1]):
            with self.subTest(size=size):
                text_setting(gates, 'gpu_size_trial_spot_max_usd_per_hour', json.dumps({size: str(prices[size])}))
                needed = aws_target.largest_gate_vcpus(tuple(gpu_size_trial.settings().spot_max_usd_per_hour))
                subject = self.deployment()
                def execute(arguments: Sequence[str], **_options: object) -> subprocess.CompletedProcess[str]:
                    operation = arguments[arguments.index('service-quotas') + 1]
                    if operation == 'get-service-quota':
                        reply: dict[str, object] = {'Quota': {'Value': applied}}
                    else:
                        self.assertEqual(operation, 'list-requested-service-quota-change-history-by-quota')
                        reply = {'RequestedQuotas': [{'DesiredValue': asked, 'Status': status}
                            for status in ('APPROVED', 'DENIED', 'CASE_CLOSED', 'NOT_APPROVED', 'INVALID_REQUEST')]}
                    return subprocess.CompletedProcess(arguments, 0, json.dumps(reply), '')
                subject.execute = execute
                with redirect_stdout(StringIO()) as output:
                    self.assertTrue(subject.quota_status())
                fits = home if applied >= needed else 'no region has enough applied quota'
                self.assertEqual(output.getvalue().splitlines()[-1],
                    f'Summary: GPU trial needs {needed} vCPUs; quota fits: {fits}; open requests: none')

    def test_quota_status_summary_separates_unread_quotas_and_unread_history(self) -> None:
        gates = self.root / 'cloud_gate.conf'
        home = conf.read(self.settings).text('region')
        other = 'us-west-2'
        text_setting(gates, 'regions', home + ' ' + other)
        text_setting(gates, 'spot_candidate_regions', home + ' ' + other)
        needed = gpu_size_trial.required_vcpus(gpu_size_trial.settings())
        history = 'list-requested-service-quota-change-history-by-quota'
        for failure in ('get-service-quota', history):
            with self.subTest(failure=failure):
                subject = self.deployment()
                def execute(arguments: Sequence[str], **_options: object) -> subprocess.CompletedProcess[str]:
                    region = arguments[arguments.index('--region') + 1]
                    operation = arguments[arguments.index('service-quotas') + 1]
                    code = arguments[arguments.index('--quota-code') + 1]
                    self.assertIn(operation, ('get-service-quota', history))
                    if region == home and code == 'L-3819A6DF' and operation == failure:
                        return subprocess.CompletedProcess(arguments, 1, '',
                            'An error occurred (AccessDeniedException) when calling quota operation: fixture refusal')
                    reply: dict[str, object] = {'Quota': {'Value': needed}}
                    if operation == history:
                        reply = {'RequestedQuotas': [{'DesiredValue': needed, 'Status': 'PENDING'}]}
                    return subprocess.CompletedProcess(arguments, 0, json.dumps(reply), '')
                subject.execute = execute
                with redirect_stdout(StringIO()) as output:
                    self.assertFalse(subject.quota_status())
                fits = f'{other}, unknown for {home}' if failure == 'get-service-quota' else f'{home}, {other}'
                self.assertEqual(output.getvalue().splitlines()[-1],
                    f'Summary: GPU trial needs {needed} vCPUs; quota fits: {fits}; open requests: unknown; '
                    + f'request history unavailable: {home} G and VT Spot')
                self.assertIn('PENDING', output.getvalue())

    def test_quota_status_credential_refusal_stops_before_aws(self) -> None:
        subject = self.deployment()
        message = 'Run deploy.py secrets in a terminal'
        with (mock.patch.object(subject.admin, 'get', side_effect=admin_secrets.SecretsRefused(message)),
              mock.patch.object(subject, 'execute', side_effect=AssertionError('The refusal must stop AWS calls')),
              mock.patch.object(deploy, 'Deployment', return_value=subject),
              mock.patch.object(sys, 'argv', ['deploy.py', 'quota-status', '--settings', str(self.settings)]),
              redirect_stdout(StringIO()) as output, redirect_stderr(StringIO()) as errors):
            self.assertEqual(deploy.main(), 1)
        self.assertEqual(output.getvalue(), '')
        self.assertEqual(errors.getvalue(), 'aws_deploy: ' + message + '\n')

    def test_quota_request_without_an_environment_override_reaches_aws_with_the_configured_file(self) -> None:
        gates = self.root / 'cloud_gate.conf'
        text_setting(gates, 'credential_file', str(self.credentials))
        expected = str(Path(conf.read(gates).text('credential_file')).expanduser())
        subject = self.deployment()
        calls: list[str] = []

        def execute(arguments: Sequence[str], *, capture_output: bool, text: bool,
                    timeout: float, check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
            self.assertTrue(capture_output and text)
            self.assertFalse(check)
            self.assertGreater(timeout, 0)
            self.assertIsNotNone(env)
            self.assertEqual(cast(dict[str, str], env)['AWS_SHARED_CREDENTIALS_FILE'], expected)
            self.assertIn('get-service-quota', arguments)
            calls.append(arguments[arguments.index('--region') + 1])
            return subprocess.CompletedProcess(arguments, 0, json.dumps({'Quota': {'Value': 300}}), '')

        subject.execute = execute
        with (mock.patch.dict(os.environ), mock.patch.object(deploy, 'Deployment', return_value=subject),
              mock.patch.object(sys, 'argv', ['deploy.py', 'quota-request', '--settings', str(self.settings)]),
              redirect_stdout(StringIO()), redirect_stderr(StringIO()) as errors):
            _ = os.environ.pop('AWS_SHARED_CREDENTIALS_FILE', None)
            self.assertEqual(deploy.main(), 0, errors.getvalue())
            self.assertEqual(os.environ['AWS_SHARED_CREDENTIALS_FILE'], expected)
        self.assertTrue(calls)

    def test_quota_request_prints_one_credential_refusal_and_stops_before_aws(self) -> None:
        message = 'Run deploy.py secrets in a terminal to restore the encrypted credentials'
        for error in (admin_secrets.SecretsRefused(message), deploy.DeploymentRefused(message)):
            with self.subTest(error=type(error).__name__):
                subject = self.deployment()
                with (mock.patch.object(subject.admin, 'get', side_effect=error) as get,
                      mock.patch.object(subject, 'execute', side_effect=AssertionError('The refusal must stop AWS calls')) as execute,
                      mock.patch.object(deploy, 'Deployment', return_value=subject),
                      mock.patch.object(sys, 'argv', ['deploy.py', 'quota-request', '--settings', str(self.settings)]),
                      redirect_stdout(StringIO()) as output, redirect_stderr(StringIO()) as errors):
                    self.assertEqual(deploy.main(), 1)
                get.assert_called_once_with()
                execute.assert_not_called()
                self.assertEqual(output.getvalue(), '')
                self.assertEqual(errors.getvalue(), 'aws_deploy: ' + message + '\n')

    def test_quota_request_uses_admin_credentials_and_continues_after_regional_failures(self) -> None:
        get_quota = 'get-service-quota'
        history = 'list-requested-service-quota-change-history-by-quota'
        request = 'request-service-quota-increase'
        cases = (
            (300.0, '', '', '', True),
            (128, '', '', '', True),
            (5, 'PENDING', '', '', True),
            (5, 'CASE_OPENED', '', '', True),
            (5, 'IN_PROGRESS', '', '', True),
            (5, '', 'PENDING', '', True),
            (5, 'APPROVED', 'CASE_OPENED', '', True),
            (5, 'DENIED', 'PENDING', '', True),
            (5, 'CASE_CLOSED', 'PENDING', '', True),
            (5, 'NOT_APPROVED', 'PENDING', '', True),
            (5, 'INVALID_REQUEST', 'PENDING', '', True),
            (5, '', '', get_quota, False),
            (5, '', '', history, False),
            (5, '', '', request, False),
            (5, '', '', 'timeout', False),
            (5, '', '', 'uncoded', False),
            (5, '', 'NOT_APPROVED', '', False))
        path = self.root / 'cloud_gate.conf'
        _ = path.write_text(re.sub(r'(?m)^regions=.*$', 'regions=us-east-1 us-west-2',
                                  re.sub(r'(?m)^spot_candidate_regions=.*$',
                                         'spot_candidate_regions=us-east-2 us-west-2', path.read_text())))
        request_id = 'req-ab12cd34ef56'
        case_id = '9876543210'
        account_id = '123456789012'
        for quota, history_status, request_status, failure, success in cases:
            with self.subTest(quota=quota, history=history_status, request=request_status, failure=failure):
                subject = self.deployment()
                subject.admin.credentials = subject.admin.get()
                calls: list[tuple[str, str]] = []
                gpu_regions: list[str] = []

                def execute(arguments: Sequence[str], *, capture_output: bool, text: bool,
                            timeout: float, check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
                    self.assertTrue(capture_output)
                    self.assertTrue(text)
                    self.assertFalse(check)
                    region = arguments[arguments.index('--region') + 1]
                    operation = arguments[arguments.index('service-quotas') + 1]
                    self.assertNotIn('--profile', arguments)
                    self.assertIsNotNone(env)
                    self.assertEqual(cast(dict[str, str], env)['AWS_ACCESS_KEY_ID'], 'fixture-admin-id')
                    self.assertEqual(cast(dict[str, str], env)['AWS_SECRET_ACCESS_KEY'], 'fixture-admin-secret')
                    if arguments[arguments.index('--quota-code') + 1] == 'L-3819A6DF':
                        self.assertEqual(operation, get_quota)
                        gpu_regions.append(region)
                        return subprocess.CompletedProcess(arguments, 0, json.dumps({'Quota': {'Value': 4}}), '')
                    expected = ['service-quotas', operation, '--service-code', 'ec2', '--quota-code', 'L-34B43A08']
                    if operation == request:
                        expected.extend(['--desired-value', '128'])
                    self.assertEqual(list(arguments[arguments.index('service-quotas'):]), [*expected, '--output', 'json'])
                    calls.append((region, operation))
                    if region == 'us-west-2':
                        self.assertEqual(operation, get_quota)
                        reply: dict[str, object] = {'Quota': {'Value': 300.0}}
                    elif operation == failure:
                        code = 'TooManyRequestsException' if operation == request else 'AccessDeniedException'
                        return subprocess.CompletedProcess(arguments, 1, '',
                            f'An error occurred ({code}) when calling the quota operation: '
                            + f'fixture-admin-secret arn:aws:iam::{account_id}:user/private RequestId: {request_id} CaseId: {case_id}')
                    elif failure == 'timeout' and operation == request:
                        raise subprocess.TimeoutExpired('fixture-admin-secret', timeout)
                    elif failure == 'uncoded' and operation == request:
                        return subprocess.CompletedProcess(arguments, 1, '', f'private failure {request_id} {case_id}')
                    elif operation == get_quota:
                        reply = {'Quota': {'Value': quota}}
                    elif operation == history:
                        reply = {'RequestedQuotas': [{'Status': history_status, 'DesiredValue': 256.0,
                            'Id': request_id, 'CaseId': case_id, 'Requester': account_id}] if history_status else []}
                    else:
                        self.assertEqual(operation, request)
                        reply = {'RequestedQuota': {'Status': request_status, 'DesiredValue': 128.0,
                            'Id': request_id, 'CaseId': case_id, 'Requester': account_id}}
                    return subprocess.CompletedProcess(arguments, 0, json.dumps(reply), '')

                subject.execute = execute
                with (mock.patch.object(deploy, 'Deployment', return_value=subject),
                      mock.patch.object(sys, 'argv', ['deploy.py', 'quota-request', '--settings', str(self.settings)]),
                      redirect_stdout(StringIO()) as captured):
                    self.assertEqual(deploy.main(), 0 if success else 1)
                lines = [line for line in captured.getvalue().splitlines() if 'G and VT' not in line]
                self.assertEqual(gpu_regions, ['us-east-2', 'us-west-2'])
                self.assertEqual([line for line in captured.getvalue().splitlines() if 'G and VT' in line],
                    [f'{region}: G and VT Spot quota 4 vCPUs; no request is needed for 4 vCPUs' for region in gpu_regions])
                self.assertEqual(lines[-1], 'us-west-2: Spot quota 300 vCPUs; no request is needed for 128 vCPUs')
                expected_operations = [get_quota]
                if quota < 128 and failure != get_quota:
                    expected_operations.append(history)
                    if failure != history and history_status not in {'PENDING', 'CASE_OPENED', 'IN_PROGRESS'}:
                        expected_operations.append(request)
                self.assertEqual(calls, [('us-east-1', operation) for operation in expected_operations] + [('us-west-2', get_quota)])
                if failure:
                    reason = {'timeout': 'TimeoutExpired', 'uncoded': 'AwsUnavailable',
                              request: 'TooManyRequestsException'}.get(failure, 'AccessDeniedException')
                    self.assertEqual(lines[0], f'us-east-1: Spot quota request failed: {reason}')
                elif quota >= 128:
                    self.assertEqual(lines[0], f'us-east-1: Spot quota {int(quota)} vCPUs; no request is needed for 128 vCPUs')
                elif history_status in {'PENDING', 'CASE_OPENED', 'IN_PROGRESS'}:
                    self.assertEqual(lines[0], f'us-east-1: open Spot quota request 256 vCPUs; status {history_status}')
                else:
                    self.assertEqual(lines[0], f'us-east-1: Spot quota request 128 vCPUs; status {request_status}')
                for private in ('fixture-admin-id', 'fixture-admin-secret', request_id, case_id, account_id):
                    self.assertNotIn(private, captured.getvalue())

    def test_gpu_quota_request_cases_and_failures_in_each_candidate_region(self) -> None:
        get_quota = 'get-service-quota'
        history = 'list-requested-service-quota-change-history-by-quota'
        request = 'request-service-quota-increase'
        candidates = ('us-west-2', 'us-east-2')
        text_setting(self.root / 'cloud_gate.conf', 'spot_candidate_regions', ' '.join(candidates))
        cases = (
            (8, '', '', '', True),
            (4, '', '', '', True),
            (0, 'PENDING', '', '', True),
            (0, 'CASE_OPENED', '', '', True),
            (0, 'IN_PROGRESS', '', '', True),
            (0, '', 'PENDING', '', True),
            (0, 'APPROVED', 'CASE_OPENED', '', True),
            (0, 'DENIED', 'PENDING', '', True),
            (0, 'CASE_CLOSED', 'PENDING', '', True),
            (0, 'NOT_APPROVED', 'PENDING', '', True),
            (0, 'INVALID_REQUEST', 'PENDING', '', True),
            (0, '', 'DENIED', '', False),
            (0, '', 'NOT_APPROVED', '', False),
            (0, '', 'INVALID_REQUEST', '', False),
            (0, '', '', get_quota, False),
            (0, '', '', history, False),
            (0, '', '', request, False),
            (0, '', '', 'timeout', False),
            (0, '', '', 'uncoded', False),
            (0, '', '', 'malformed', False),
        )
        for selected_region in candidates:
            for quota, history_status, request_status, failure, success in cases:
                with self.subTest(region=selected_region, quota=quota, history=history_status,
                                  request=request_status, failure=failure):
                    subject = self.deployment()
                    calls: list[tuple[str, str, str]] = []

                    def execute(arguments: Sequence[str], *, capture_output: bool, text: bool,
                                timeout: float, check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
                        self.assertTrue(capture_output)
                        self.assertTrue(text)
                        self.assertFalse(check)
                        self.assertNotIn('--profile', arguments)
                        self.assertIsNotNone(env)
                        self.assertEqual(cast(dict[str, str], env)['AWS_ACCESS_KEY_ID'], 'fixture-admin-id')
                        region = arguments[arguments.index('--region') + 1]
                        code = arguments[arguments.index('--quota-code') + 1]
                        operation = arguments[arguments.index('service-quotas') + 1]
                        calls.append((region, code, operation))
                        expected = ['service-quotas', operation, '--service-code', 'ec2', '--quota-code', code]
                        if operation == request:
                            expected.extend(['--desired-value', '4'])
                        self.assertEqual(list(arguments[arguments.index('service-quotas'):]), [*expected, '--output', 'json'])
                        if code == 'L-34B43A08':
                            self.assertEqual(region, 'us-east-1')
                            self.assertEqual(operation, get_quota)
                            reply: dict[str, object] = {'Quota': {'Value': 128}}
                        elif region != selected_region:
                            self.assertEqual(code, 'L-3819A6DF')
                            self.assertEqual(operation, get_quota)
                            reply = {'Quota': {'Value': 4}}
                        elif operation == failure:
                            return subprocess.CompletedProcess(arguments, 1, '',
                                'An error occurred (AccessDeniedException) when calling the quota operation: '
                                + 'private-request private-case private-account fixture-admin-secret')
                        elif operation == request and failure == 'timeout':
                            raise subprocess.TimeoutExpired('private-request', timeout)
                        elif operation == request and failure == 'uncoded':
                            return subprocess.CompletedProcess(arguments, 1, '', 'private-request private-case private-account')
                        elif operation == get_quota:
                            reply = {'Quota': {'Value': quota}}
                        elif operation == history:
                            reply = {'RequestedQuotas': [{'Status': history_status, 'DesiredValue': 8,
                                'Id': 'private-request', 'CaseId': 'private-case', 'Requester': 'private-account'}]
                                if history_status else []}
                        else:
                            self.assertEqual(operation, request)
                            reply = {} if failure == 'malformed' else {'RequestedQuota': {'Status': request_status,
                                'Id': 'private-request', 'CaseId': 'private-case', 'Requester': 'private-account'}}
                        return subprocess.CompletedProcess(arguments, 0, json.dumps(reply), '')

                    subject.execute = execute
                    with (mock.patch.object(deploy, 'Deployment', return_value=subject),
                          mock.patch.object(sys, 'argv', ['deploy.py', 'quota-request', '--settings', str(self.settings)]),
                          redirect_stdout(StringIO()) as captured):
                        self.assertEqual(deploy.main(), 0 if success else 1)
                    expected_calls = [('us-east-1', 'L-34B43A08', get_quota)]
                    for region in candidates:
                        expected_calls.append((region, 'L-3819A6DF', get_quota))
                        if region == selected_region and quota < 4 and failure != get_quota:
                            expected_calls.append((region, 'L-3819A6DF', history))
                            if failure != history and history_status not in {'PENDING', 'CASE_OPENED', 'IN_PROGRESS'}:
                                expected_calls.append((region, 'L-3819A6DF', request))
                    self.assertEqual(calls, expected_calls)
                    lines = captured.getvalue().splitlines()
                    self.assertEqual(lines[0], 'us-east-1: Spot quota 128 vCPUs; no request is needed for 128 vCPUs')
                    if failure:
                        reason = {'timeout': 'TimeoutExpired', 'uncoded': 'AwsUnavailable', 'malformed': 'KeyError'}.get(
                            failure, 'AccessDeniedException')
                        expected_line = f'{selected_region}: G and VT Spot quota request failed: {reason}'
                    elif quota >= 4:
                        expected_line = f'{selected_region}: G and VT Spot quota {quota} vCPUs; no request is needed for 4 vCPUs'
                    elif history_status in {'PENDING', 'CASE_OPENED', 'IN_PROGRESS'}:
                        expected_line = f'{selected_region}: open G and VT Spot quota request 8 vCPUs; status {history_status}'
                    else:
                        expected_line = f'{selected_region}: G and VT Spot quota request 4 vCPUs; status {request_status}'
                    self.assertEqual(lines, [lines[0], *[
                        expected_line if region == selected_region else
                        f'{region}: G and VT Spot quota 4 vCPUs; no request is needed for 4 vCPUs' for region in candidates]])
                    for private in ('private-request', 'private-case', 'private-account', 'fixture-admin-id', 'fixture-admin-secret'):
                        self.assertNotIn(private, captured.getvalue())

    def test_regional_gate_stack_reuses_home_resources_and_copies_missing_image(self) -> None:
        subject, commands = self.gate_fixture()
        home_api = subject._api(aws_config.read_deployment_settings(self.settings, self.state))  # pyright: ignore[reportPrivateUsage]
        original = home_api.execute
        copied = False
        def execute(arguments: Sequence[str], **_options: object) -> subprocess.CompletedProcess[str]:
            args = list(arguments)
            region = args[args.index('--region') + 1]
            operation = args[args.index('--region') + 3]
            reply: dict[str, object] = {}
            nonlocal copied
            if operation == 'describe-vpcs':
                reply = {'Vpcs': [{'VpcId': 'vpc-fixture-west'}]}
            elif operation == 'describe-images':
                reply = {'Images': [{'ImageId': 'ami-fixture-home', 'Name': 'fixture-nixos', 'OwnerId': 'fixture-owner'}]
                         if region == 'us-east-2' else []}
            elif operation == 'copy-image':
                copied = True
                reply = {'ImageId': 'ami-fixture-west'}
            elif operation == 'describe-stacks' and region == 'us-west-2':
                reply = {'Stacks': [{'Outputs': [{'OutputKey': key, 'OutputValue': value} for key, value in
                    {'GateLaunchTemplate': 'lt-west', 'GateLaunchVersion': '2', 'GateSecurityGroup': 'sg-west'}.items()]}]}
            elif operation not in ('deploy', 'wait'):
                return original(arguments, capture_output=True, text=True, timeout=1, check=False)
            commands.append(args)
            return subprocess.CompletedProcess(args, 0, json.dumps(reply), '')
        api = aws_target.AwsCli('fixture', 'us-east-2', 1, execute=execute)
        settings = aws_config.read_deployment_settings(self.settings, self.state)
        home = {'GateProfileArn': 'fixture-profile', 'GateRoleArn': 'fixture-role'}
        result = subject.regional_gate_stack(api, settings, home, {}, 'us-west-2')
        self.assertTrue(copied)
        self.assertEqual(result['image'], 'ami-fixture-west')
        self.assertEqual(result['region'], 'us-west-2')
        self.assertEqual(subject.regional_gate_stack(api, settings, home, {}, 'us-west-2'), result)
        self.assertEqual(sum('copy-image' in command for command in commands), 1)
        self.assertTrue(any('deploy' in command and settings.stack_name + '-gates-us-west-2' in command for command in commands))
        query = next(command for command in commands if 'describe-images' in command and '--owners' in command)
        self.assertIn('fixture-owner', query)
        self.assertIn('Name=name,Values=fixture-nixos', query)
        resources = ledger.mapping(deploy.region_gate_template()['Resources'])
        self.assertEqual(len(resources), 9)
        self.assertIn('GateGpuTrialScalerPolicy', resources)
        self.assertNotIn('GateBucket', resources)
        self.assertNotIn('GateProfile', resources)
        for name, key in (('GateExpiryRole', 'RoleName'), ('GateExpiryFunction', 'FunctionName')):
            properties = ledger.mapping(ledger.mapping(resources[name])['Properties'])
            value = ledger.text(ledger.mapping(properties[key]), 'Fn::Sub')
            self.assertLessEqual(len(value.replace('${DeploymentId}', 'f' * 32).replace('${AWS::Region}', 'us-west-2')), 64)

    def test_regional_image_uses_matching_name_without_copy(self) -> None:
        subject = self.deployment()
        calls: list[list[str]] = []
        def execute(arguments: Sequence[str], **_options: object) -> subprocess.CompletedProcess[str]:
            args = list(arguments)
            calls.append(args)
            home = args[args.index('--region') + 1] == 'us-east-2'
            image = {'ImageId': 'ami-home' if home else 'ami-west', 'Name': 'fixture-nixos', 'OwnerId': 'fixture-owner'}
            return subprocess.CompletedProcess(args, 0, json.dumps({'Images': [image]}), '')
        api = aws_target.AwsCli('fixture', 'us-east-2', 1, execute=execute)
        self.assertEqual(subject.regional_image(api, aws_config.read_deployment_settings(self.settings), 'us-west-2'), 'ami-west')
        self.assertFalse(any('copy-image' in command for command in calls))

    def test_regional_gate_stack_refuses_missing_default_vpc(self) -> None:
        subject = self.deployment()
        def execute(arguments: Sequence[str], **_options: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(arguments, 0, '{"Vpcs": []}', '')
        with self.assertRaisesRegex(deploy.DeploymentRefused, 'us-west-2: no default VPC'):
            _ = subject.regional_gate_stack(aws_target.AwsCli('fixture', 'us-east-2', 1, execute=execute),
                aws_config.read_deployment_settings(self.settings), {}, {}, 'us-west-2')

    def test_regional_gate_stack_refuses_no_public_subnet(self) -> None:
        subject = self.deployment()
        def execute(arguments: Sequence[str], **_options: object) -> subprocess.CompletedProcess[str]:
            args = list(arguments)
            reply: dict[str, object] = {'Vpcs': [{'VpcId': 'vpc-fixture'}]}
            if 'describe-route-tables' in args:
                reply = {'RouteTables': []}
            elif 'describe-subnets' in args:
                reply = {'Subnets': [{'SubnetId': 'subnet-fixture', 'AvailabilityZone': 'us-west-2a', 'State': 'available'}]}
            return subprocess.CompletedProcess(args, 0, json.dumps(reply), '')
        with self.assertRaisesRegex(deploy.DeploymentRefused, 'no available public subnet'):
            _ = subject.regional_gate_stack(aws_target.AwsCli('fixture', 'us-east-2', 1, execute=execute),
                aws_config.read_deployment_settings(self.settings), {}, {}, 'us-west-2')

    def test_regional_image_cleanup_recovers_after_deregistration_record_failure(self) -> None:
        from scripts.aws_target import teardown
        path = self.state / 'regional-images.json'
        _ = path.write_text(json.dumps({'copy': {'region': 'us-west-2', 'image': 'ami-west'}}))
        deleted = False
        snapshots: list[str] = []
        def execute(arguments: Sequence[str], **_options: object) -> subprocess.CompletedProcess[str]:
            nonlocal deleted
            args = list(arguments)
            reply: dict[str, object] = {}
            error = ''
            if 'describe-images' in args:
                reply = {'Images': [{'BlockDeviceMappings': [{'Ebs': {'SnapshotId': 'snap-west'}}]}]}
            elif 'deregister-image' in args:
                if deleted:
                    error = 'InvalidAMIID.NotFound'
                deleted = True
            elif 'delete-snapshot' in args:
                snapshots.append(args[args.index('--snapshot-id') + 1])
            return subprocess.CompletedProcess(args, int(bool(error)), json.dumps(reply), error)
        api = aws_target.AwsCli('fixture', 'us-east-2', 1, execute=execute)
        write = write_text_atomic
        def fail_record(destination: Path, text: str) -> None:
            if 'deregistered' in text:
                raise OSError('fixture record failure')
            write(destination, text)
        with mock.patch.object(teardown, 'write_text_atomic', side_effect=fail_record):
            with self.assertRaisesRegex(OSError, 'fixture record failure'):
                _ = teardown.remove_regional_gates(api, self.state)
        self.assertTrue(deleted)
        self.assertTrue(teardown.remove_regional_gates(api, self.state))
        self.assertEqual(snapshots, ['snap-west'])
        self.assertFalse(teardown.remove_regional_gates(api, self.state))

    def test_down_removes_regional_stack_and_copied_image_before_snapshot(self) -> None:
        from scripts.aws_target import teardown
        text_setting(self.root / 'cloud_gate.conf', 'regions', 'us-east-2 us-west-2')
        cloud_gate_record = {'deployment_id': 'fixture', 'regions': [{'region': 'us-east-2'},
            {'region': 'us-west-2', 'stack': 'fixture-gates-us-west-2'}]}
        _ = (self.state / 'gate-deployment.json').write_text(json.dumps(cloud_gate_record))
        _ = (self.state / 'regional-images.json').write_text(json.dumps({'copy': {'region': 'us-west-2', 'image': 'ami-west'}}))
        calls: list[list[str]] = []
        def execute(arguments: Sequence[str], **_options: object) -> subprocess.CompletedProcess[str]:
            args = list(arguments)
            calls.append(args)
            reply: dict[str, object] = {'Reservations': []}
            if 'describe-images' in args:
                reply = {'Images': [{'BlockDeviceMappings': [{'Ebs': {'SnapshotId': 'snap-west'}}]}]}
            return subprocess.CompletedProcess(args, 0, json.dumps(reply), '')
        api = aws_target.AwsCli('fixture', 'us-east-2', 1, execute=execute)
        self.assertTrue(teardown.remove_regional_gates(api, self.state))
        operations = [command[command.index('--region') + 3] for command in calls]
        self.assertLess(operations.index('delete-stack'), operations.index('deregister-image'))
        self.assertLess(operations.index('deregister-image'), operations.index('delete-snapshot'))
        sweeps = [command for command in calls if 'describe-instances' in command]
        self.assertEqual([command[command.index('--region') + 1] for command in sweeps], ['us-east-2', 'us-west-2'])
        self.assertTrue(all(command[command.index('--region') + 1] == 'us-west-2' for command in calls if command not in sweeps))
        self.assertFalse(teardown.remove_regional_gates(api, self.state))


    def test_gate_stack_updates_print_named_start_and_done_lines(self) -> None:
        image_titles = ('switching gates to the official image', 'removing the old image permission')
        for command, titles in (('up', ('updating the stack',)), ('remove-images', image_titles)):
            with self.subTest(command=command):
                if command == 'up':
                    subject, _ = self.gate_fixture()
                    with redirect_stdout(StringIO()) as output:
                        self.assertTrue(subject.gate_up(approve_resources=True))
                else:
                    subject, _ = self.gate_image_fixture()
                    with redirect_stdout(StringIO()) as output:
                        self.assertTrue(subject.remove_images(approve_destroy=True))
                lines = output.getvalue().splitlines()
                for title in titles:
                    progress = [line for line in lines if line.startswith(f'{command}: {title}')]
                    self.assertEqual(len(progress), 2)
                    self.assertEqual(progress[0], f'{command}: {title} (can take several minutes)')
                    self.assertRegex(progress[1], '^' + re.escape(f'{command}: {title}') + r' done in \d+ s$')

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
        self.assertEqual(by_name['GateGpuInstanceTypes']['ParameterValue'],
                         ','.join(gpu_size_trial.settings().spot_max_usd_per_hour))
        self.assertEqual(by_name['GateRootGiB']['ParameterValue'], '40')
        self.assertEqual(by_name['GateSpotMaxPrice']['ParameterValue'], '0.8')
        saved = remote.read_record(self.state / 'gate-deployment.json')
        self.assertEqual(saved['artifact_prefix'], 'artifacts/fixture-role:')
        self.assertEqual(saved['revision'], 'fixture-revision')
        self.assertEqual([row['zone'] for row in ledger.rows(ledger.rows(saved, 'regions')[0], 'subnets')], ['a', 'b'])
        after = ledger.CostLedger(self.state).read_snapshot().inventories[-1].resources
        self.assertEqual(after, before + (ledger.PricedResource('gate-shared', Decimal(1)),))
        self.assertTrue(any('execute-change-set' in args for args in commands))

    def test_gpu_trial_command_requires_approval_and_checks_credentials(self) -> None:
        subject = deploy.Deployment(self.settings, self.state, self.root / 'output')
        folder = self.root / 'trial-alias'
        folder.symlink_to(self.root, target_is_directory=True)
        with mock.patch.object(gpu_size_trial, 'start_service', return_value=0) as service:
            with self.assertRaisesRegex(deploy.DeploymentRefused, '--approve-resources'):
                _ = subject.gpu_size_trial(self.root / 'trial')
            service.assert_not_called()
            with mock.patch.object(subject, '_check_credentials') as credentials:
                self.assertTrue(subject.gpu_size_trial(folder, approve_resources=True))
            credentials.assert_called_once_with()
        service.assert_called_once()
        assert service.call_args is not None
        self.assertEqual(service.call_args.args[0], folder)

    def test_gpu_trial_cli_routes_the_approved_run(self) -> None:
        folder = self.root / 'trial'
        argv = ['deploy.py', 'gpu-size-trial', 'run', '--trial-folder', str(folder),
                '--settings', str(self.settings), '--directory', str(self.state), '--approve-resources']
        with (mock.patch.object(sys, 'argv', argv),
              mock.patch.object(deploy.Deployment, 'gpu_size_trial', return_value=True) as run):
            self.assertEqual(deploy.main(), 0)
        run.assert_called_once_with(folder, approve_resources=True)

    def test_gpu_trial_permission_preserves_normal_gate_scope(self) -> None:
        source = ledger.mapping(cast(object, json.loads(deploy.TEMPLATE.read_text())))
        resources = ledger.mapping(source['Resources'])
        properties = [ledger.mapping(ledger.mapping(resources[name])['Properties'])
                      for name in ('GateScalerPolicy', 'GateGpuTrialScalerPolicy')]
        statements = tuple(row for policy in properties for row in ledger.rows(ledger.mapping(policy['PolicyDocument']), 'Statement'))
        scopes = [ledger.mapping(ledger.mapping(row['Condition'])['StringEquals'])
                  for row in statements if row.get('Condition')
                  and 'ec2:InstanceType' in ledger.mapping(ledger.mapping(row['Condition']).get('StringEquals', {}))]
        self.assertEqual([row['ec2:InstanceType'] for row in scopes],
                         [{'Ref': 'GateInstanceTypes'}, {'Ref': 'GateGpuInstanceTypes'}])
        for row in scopes:
            self.assertEqual(row['aws:RequestTag/Workload'], 'cloud-claude-gate')
            self.assertEqual(row['ec2:InstanceMarketType'], 'spot')
        regional = deploy.region_gate_template()
        self.assertIn('GateGpuInstanceTypes', ledger.mapping(regional['Parameters']))
        self.assertIn('GateGpuTrialScalerPolicy', ledger.mapping(regional['Resources']))
        gpu_statements = ledger.rows(ledger.mapping(properties[1]['PolicyDocument']), 'Statement')
        gpu_instance = gpu_statements[0]
        self.assertEqual(ledger.mapping(ledger.mapping(gpu_instance['Condition'])['Null'])['aws:RequestTag/GpuSizeTrial'], 'false')
        self.assertIn('ec2:InstanceProfile', ledger.mapping(ledger.mapping(gpu_instance['Condition'])['ArnEquals']))

    def test_gate_deployment_rejects_a_change_to_a_rust_resource(self) -> None:
        subject, commands = self.gate_fixture('Instance')
        with self.assertRaisesRegex(deploy.DeploymentRefused, 'Rust resource'):
            _ = subject.gate_up(approve_resources=True)
        self.assertFalse(any('execute-change-set' in args for args in commands))
        self.assertTrue(any('delete-change-set' in args for args in commands))
        self.assertFalse((self.state / 'gate-deployment.json').exists())


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
                            subject._update_stack(settings, prepared.parameters)  # pyright: ignore[reportPrivateUsage]
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
                def fail_delete(operation: str, *arguments: str, service: str = 'ec2',
                                timeout_s: float | None = None) -> dict[str, object]:
                    if operation == 'delete-change-set':
                        raise error
                    return call(operation, *arguments, service=service, timeout_s=timeout_s)
                with (mock.patch.object(subject, '_api', return_value=api),
                      mock.patch.object(api, 'call', side_effect=fail_delete),
                      mock.patch.object(subject, '_before_request', side_effect=deploy.DeploymentRefused('original spending refusal'))):
                    with self.assertRaisesRegex(deploy.DeploymentRefused, 'original spending refusal') as failure:
                        subject._update_stack(settings, prepared.parameters)  # pyright: ignore[reportPrivateUsage]
                args = cast(list[str], self.commands('create-change-set')[-1]['args'])
                name = args[args.index('--change-set-name') + 1]
                self.assertIn('unexecuted change set remains: ' + name, str(failure.exception))
                self.assertNotIn(str(error), str(failure.exception))
                self.assertTrue((self.root / 'update-change-set.json').exists())
                (self.root / 'update-change-set.json').unlink()


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
                        return subprocess.CompletedProcess(arguments, 1, '',
                            'reservation refused ' + notification + ' AWS_SESSION_TOKEN=fixture-token')
                    result = execute(arguments, capture_output=capture_output, text=text, timeout=timeout, check=check, env=env)
                    if 'describe-change-set' in arguments and result.returncode == 0:
                        reply = ledger.mapping(cast(object, json.loads(result.stdout)))
                        reply['StatusReason'] = 'reservation reason ' + notification
                        return subprocess.CompletedProcess(arguments, 0, json.dumps(reply), '')
                    if operation == 'wait' and 'wait' in arguments:
                        failed = True
                        return subprocess.CompletedProcess(arguments, 1, '',
                            'reservation refused ' + notification + ' AWS_SESSION_TOKEN=fixture-token')
                    return result
                _ = (self.root / 'describe-stack-events.json').write_text(json.dumps({'StackEvents': [{
                    'Timestamp': datetime.fromtimestamp(self.now, UTC).isoformat(), 'LogicalResourceId': 'Foundation',
                    'ResourceType': 'AWS::CloudFormation::Stack', 'ResourceStatus': 'CREATE_FAILED',
                    'ResourceStatusReason': 'event reason ' + notification}]}))
                subject.execute = fail
                try:
                    with self.assertRaises(aws_target.AwsUnavailable) as failure:
                        _ = subject.apply(approve_resources=True)
                    reason = subject.admin.redact(str(failure.exception))
                    self.assertIn('reservation refused <notification address>', reason)
                    self.assertIn('AWS_SESSION_TOKEN=[redacted]', reason)
                    self.assertNotIn('fixture-token', reason)
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


    def test_foundation_apply_never_requests_builder_compute(self) -> None:
        subject = self.update_fixture()
        outputs = subject.apply(approve_resources=True)
        self.assertEqual(outputs['ComputeEnabled'], 'false')
        self.assertNotIn('InstanceId', outputs)
        self.assertTrue(outputs['BudgetName'])
        self.assertFalse(self.commands('start-instances'))
        self.assertFalse(self.commands('run-instances'))
        self.assertFalse(ledger.CostLedger(self.state).pending_compute_requests())
        self.assertTrue(ledger.CostLedger(self.state).read_snapshot().inventories[-1].builder_removed)

    def test_empty_update_is_success_without_execution_or_compute_request(self) -> None:
        subject = self.update_fixture()
        _ = (self.root / 'update-plan.json').write_text(json.dumps({
            'Status': 'FAILED', 'StatusReason': "The submitted information didn't contain changes."}))
        (self.root / 'fail-wait-change-set-create-complete').touch()
        before = len(self.commands('execute-change-set'))
        outputs = subject.apply(approve_resources=True)
        self.assertEqual(outputs['ComputeEnabled'], 'false')
        self.assertEqual(len(self.commands('execute-change-set')), before)
        self.assertFalse(ledger.CostLedger(self.state).pending_compute_requests())
        self.assertTrue(self.commands('delete-change-set'))


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


    def test_parameters_choose_only_an_available_saved_image(self) -> None:
        subject = self.deployment()
        self.assertEqual(subject.prepare().parameters['AmiId'], 'ami-fixture')
        _ = (self.state / 'image.json').write_text(json.dumps({'image_id': 'ami-cache', 'system': SYSTEM}))
        self.assertEqual(subject.prepare().parameters['AmiId'], 'ami-cache')
        with mock.patch.object(box, 'saved_image', return_value={}):
            self.assertEqual(subject.prepare().parameters['AmiId'], 'ami-fixture')
        with mock.patch.object(aws_target.AwsCli, 'call', side_effect=aws_target.AwsUnavailable('InvalidAMIID.NotFound')):
            self.assertEqual(box.saved_image(subject._api(aws_config.read_deployment_settings(self.settings, self.state)), self.state), {})  # pyright: ignore[reportPrivateUsage]


    def test_update_preserves_ami_only_with_a_live_instance(self) -> None:
        subject = self.update_fixture()
        prepared = subject.prepare()
        for live in ({'InstanceId': 'i-fixture'}, {}):
            with mock.patch.object(subject, '_stack', return_value=live):
                subject._update_stack(aws_config.read_deployment_settings(self.settings, self.state), prepared.parameters)  # pyright: ignore[reportPrivateUsage]
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


    def removal_fixture(self, *, save_node: bool = True) -> dict[str, object]:
        _ = self.update_fixture()
        state = remote.read_record(self.root / 'fake-stack.json')
        outputs = ledger.mapping(state['outputs'])
        outputs.update(InstanceId='i-fixture', DataVolumeId='vol-created', ComputeEnabled='true')
        state['instance_state'] = 'stopped'
        write_text_atomic(self.root / 'fake-stack.json', json.dumps(state))
        saved = deployment_record = remote.read_record(self.state / 'deployment.json')
        saved.update(outputs={**outputs, 'DataVolumeId': ''}, data_volume_id='vol-created')
        write_text_atomic(self.state / 'deployment.json', json.dumps(saved))
        _ = (self.root / 'volume-owner').write_text(str(deployment_record['deployment_id']))
        write_text_atomic(self.state / 'up.json', json.dumps({'box-join': {}} if save_node else {}))
        (self.state / 'up').mkdir(exist_ok=True)
        (self.state / 'up/controller.lock').touch()
        ledger.CostLedger(self.state).observe(ledger.InstanceObservation(self.now, 'terminated', 'historical builder fixture'), confirms_transition=True)
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
        _ = cast(object, self.enterContext(mock.patch.object(target_config, 'ssh_target_state_directory', return_value=self.state)))
        return devices


    def down_output(self) -> tuple[bool, str]:
        output = StringIO()
        with redirect_stdout(output):
            success = self.deployment().down(approve_destroy=True)
        return success, output.getvalue()


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

    def test_down_removes_rebuilt_foundation_without_a_data_volume(self) -> None:
        _ = self.removal_fixture()
        success, output = self.down_output()
        self.assertTrue(success, output)
        subject = self.deployment()
        _ = subject.prepare()
        outputs = subject.apply(approve_resources=True)
        self.assertEqual(outputs['ComputeEnabled'], 'false')
        self.assertNotIn('InstanceId', outputs)
        self.assertNotIn('DataVolumeId', outputs)
        saved = remote.read_record(self.state / 'deployment.json')
        self.assertFalse(saved.get('data_volume_id'))
        before = len(self.commands())
        success, output = self.down_output()
        self.assertTrue(success, output)
        operations = [cast(list[str], row['args'])[5] for row in self.commands()[before:]]
        self.assertIn('delete-stack', operations)
        self.assertIn('delete-budget', operations)
        self.assertNotIn('describe-volumes', operations)
        self.assertNotIn('delete-volume', operations)
        self.assertNotIn('update-stack', operations)
        self.assertFalse((self.root / 'fake-stack.json').exists())
        self.assertFalse((self.state / 'deployment.json').exists())
        self.assertTrue((self.state / 'down-complete.json').exists())

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
        _ = (self.state / 'gate-deployment.json').write_text(json.dumps({
            'bucket': 'fixture-bucket', 'template': 'fixture-template',
            'deployment_id': aws_config.deployment_record(self.state)['deployment_id']}))
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
        labels = ['checking the deployment', 'disabling builder routing', 'removing the builder from Tailscale',
                  'updating the stack', 'deleting builder storage',
                  'deleting local builder records']
        plain = ['remove-builder: ' + label for label in labels]
        starts = [line + ' (can take several minutes)' if label == 'updating the stack' else line
                  for label, line in zip(labels, plain)]
        self.assertEqual([line for line in output.getvalue().splitlines() if line in starts], starts)
        progress = [line for line in output.getvalue().splitlines()
                    if line in starts or any(line.startswith(start + ' done in ') for start in plain)]
        self.assertEqual(len(progress), len(starts) * 2)
        self.assertEqual([line for line in output.getvalue().splitlines()
                          if line.startswith('remove-builder: ') and not line.startswith('remove-builder: done;')], progress)
        self.assertEqual(self.previous_output('\n'.join(progress), 'remove-builder'), '')
        print('\n'.join(progress))
        private = ('i-fixture', 'vol-', '123456789012', 'arn:', 'device-', 'fixture-bucket',
                   '100.64.', 'fixture-admin-secret', 'fixture-oauth-secret')
        for value in private:
            self.assertNotIn(value, '\n'.join(line for line in output.getvalue().splitlines()
                                               if line.startswith('remove-builder: ')))
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
        self.assertEqual([target.name for target in target_config.read_targets()], ['mac', 'natedev'])
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
        self.assertTrue(all(line.endswith(': present') for line in self.previous_output(output, 'down').splitlines()), output)
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


    def test_review_in_progress_stack_has_no_resources_to_reconcile(self) -> None:
        _ = (self.root / 'review-stack').write_text('empty change set')
        _ = self.update_fixture()
        state = remote.read_record(self.root / 'fake-stack.json')
        outputs = ledger.mapping(state['outputs'])
        outputs.update(InstanceId='i-fixture', DataVolumeId='vol-created', ComputeEnabled='true')
        state['instance_state'] = 'stopped'
        write_text_atomic(self.root / 'fake-stack.json', json.dumps(state))
        saved = deployment_record = remote.read_record(self.state / 'deployment.json')
        saved.update(outputs={**outputs, 'DataVolumeId': ''}, data_volume_id='vol-created')
        write_text_atomic(self.state / 'deployment.json', json.dumps(saved))
        _ = (self.root / 'volume-owner').write_text(str(deployment_record['deployment_id']))
        write_text_atomic(self.state / 'up.json', json.dumps({'box-join': {}}))
        ledger.CostLedger(self.state).observe(ledger.InstanceObservation(self.now, 'terminated', 'historical builder fixture'), confirms_transition=True)
        self.assertEqual(len(self.commands('deploy')), 2)


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


    def test_prepare_preserves_existing_cost_and_claim_evidence(self) -> None:
        subject = self.deployment()
        _ = subject.prepare()
        book = ledger.CostLedger(self.state)
        book.observe(ledger.InstanceObservation(self.now, 'stopped', 'fixture'))
        self.set_clock(self.now + 1)
        _ = subject.prepare()
        self.assertEqual(book.read_snapshot().observations, [ledger.InstanceObservation(self.now - 1, 'stopped', 'fixture')])


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


    def refuse_command(self, arguments: Sequence[str], *, capture_output: bool, text: bool,
                       timeout: float, check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        self.assertIsNone(env)
        _ = (capture_output, text, timeout, check)
        raise AssertionError(f'disabled tick called AWS: {arguments}')


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
