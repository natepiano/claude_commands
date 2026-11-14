#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Prepare local evidence and deploy resources only through an explicit user command."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
from configparser import ConfigParser
from io import StringIO
from collections.abc import Callable, Sequence, Generator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import threading
import time
from typing import cast, final
from uuid import uuid4


from scripts.aws_target import admin_secrets, box, hana_box, hana_spend, ledger, remote, teardown
from scripts.aws_target.aws_target import AwsCli, AwsExecutor, AwsUnavailable, control_lock, execute_command, fetch_spot_prices, spot_quota, largest_gate_vcpus as largest_gate_vcpus
from scripts.aws_target.hana_provider import AwsHanaProvider
from scripts.aws_target.config import BudgetNotificationAddress, DeploymentSettings, money, read_budget_notification_address, deployment_record, gate_deployment, gate_regions, read_deployment_settings, read_settings
from scripts.aws_target.terminal_table import aligned_table
from scripts.lib import conf, user_zone
from scripts.mac_test import mac_test
from scripts.build_targets import target_config
from scripts.lib.atomic_file import write_bytes_atomic, write_text_atomic

TEMPLATE = Path(__file__).resolve().parents[2] / 'docs/plans/builds-aws/cloudformation.json'
HANA_TEMPLATE = TEMPLATE.with_name('hana-cloudformation.json')
CLOSED_QUOTA_REQUEST_STATUSES = frozenset(('APPROVED', 'DENIED', 'CASE_CLOSED', 'NOT_APPROVED', 'INVALID_REQUEST'))


UP_STAGE_LABELS = {
    'price': 'reading the Spot price',
    'bootstrap': 'preparing the boot script',
    'stacks': 'updating the stack',
    'scaler-key': 'preparing the scaler credentials',
    'tailscale-policy': 'checking the Tailscale policy',
    'box-join': 'joining the box to Tailscale',
    'box-configuration': 'configuring the box',
    'checks': 'checking the box',
    'image': 'preparing the box image',
    'idle-stop': 'configuring the idle timer',
    'activation': 'activating the builder',
    'routing-proof': 'checking the build route',
}


@contextmanager
def step(command: str, text: str, settings: Path, *, hint: str = '') -> Generator[None]:
    """Show progress until the operation ends."""
    interval = conf.read(settings).number('deploy_progress_interval_s')
    stream = sys.stdout
    started = time.monotonic()
    stopped = threading.Event()
    start = f'{text} ({hint})' if hint else text
    print(f'{command}: {start}', file=stream, flush=True)

    def heartbeat() -> None:
        while not stopped.wait(interval):
            minutes = int((time.monotonic() - started) / 60)
            print(f'{command}: still {text}, {minutes} min', file=stream, flush=True)

    worker = threading.Thread(target=heartbeat, name='deployment-progress', daemon=True)
    worker.start()
    try:
        yield
    finally:
        stopped.set()
        worker.join()
        elapsed = time.monotonic() - started
        print(f'{command}: {text} done in {elapsed:.0f} s', file=stream, flush=True)


class DeploymentRefused(ValueError):
    """Current evidence does not permit a resource request."""


def quota_targets(home: str) -> tuple[tuple[tuple[str, ...], str, int, str], ...]:
    """Share the configured regions and quota codes between requests and reports."""
    values = conf.read_or_repository('cloud_gate')
    return (
        (gate_regions(home, values), 'L-34B43A08', values.whole('spot_quota_request_vcpus'), 'Spot'),
        (values.words('spot_candidate_regions'), 'L-3819A6DF', values.whole('gpu_spot_quota_request_vcpus'), 'G and VT Spot'),
    )


def quota_timestamp(value: object) -> float:
    """Convert AWS timestamps to seconds for request ordering."""
    if value is None:
        return float('-inf')
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        moment = datetime.fromtimestamp(value, UTC)
    elif isinstance(value, str):
        moment = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
    else:
        raise ValueError('The quota request date is invalid')
    return moment.timestamp()


def quota_date(value: object) -> str:
    """Format AWS timestamps in the user's zone."""
    if value is None:
        return '-'
    return datetime.fromtimestamp(quota_timestamp(value), UTC).astimezone(user_zone.user_zone()).strftime('%Y-%m-%d %H:%M:%S %Z')


def request_spot_quota(api: AwsCli, code: str, desired: int, label: str) -> bool:
    """Request one regional quota unless its value is sufficient or a request is open."""
    region = api.region
    try:
        quota = spot_quota(api, code)
        if quota >= desired:
            print(f'{region}: {label} quota {quota.normalize():f} vCPUs; no request is needed for {desired} vCPUs')
            return True
        history = api.call('list-requested-service-quota-change-history-by-quota',
            '--service-code', 'ec2', '--quota-code', code, service='service-quotas')
        opened = [row for row in ledger.rows(history, 'RequestedQuotas') if ledger.text(row, 'Status') not in CLOSED_QUOTA_REQUEST_STATUSES]
        if opened:
            for row in opened:
                status = ledger.text(row, 'Status')
                value = conf.decimal(str(row['DesiredValue']))
                print(f'{region}: open {label} quota request {value.normalize():f} vCPUs; status {status}')
            return True
        reply = api.call('request-service-quota-increase', '--service-code', 'ec2',
            '--quota-code', code, '--desired-value', str(desired), service='service-quotas')
        status = ledger.text(ledger.mapping(reply['RequestedQuota']), 'Status')
        print(f'{region}: {label} quota request {desired} vCPUs; status {status}')
        return status not in {'DENIED', 'NOT_APPROVED', 'INVALID_REQUEST'}
    except (admin_secrets.SecretsRefused, DeploymentRefused):
        raise
    except (KeyError, OSError, ValueError, AwsUnavailable, subprocess.SubprocessError) as error:
        match = re.search(r'An error occurred \(([A-Za-z][A-Za-z0-9]*)\) when calling ', str(error))
        reason = match[1] if match else type(error).__name__
        print(f'{region}: {label} quota request failed: {reason}')
        return False


@dataclass(frozen=True)
class PreparedDeployment:
    at: int
    fingerprint: str
    parameters: dict[str, str]


def deployment_parameters(path: Path, settings: DeploymentSettings,
                          notification: BudgetNotificationAddress, deployment_id: str) -> dict[str, str]:
    spending = read_settings(path)
    return {
        'InstanceType': settings.instance_type, 'AmiId': settings.ami_id,
        'AvailabilityZone': settings.availability_zone, 'VpcId': settings.vpc_id, 'SubnetId': settings.subnet_id,
        'NotificationEmail': notification.value, 'RootDeviceName': settings.root_device_name,
        'RootVolumeGiB': str(settings.root_volume_gib), 'DataVolumeGiB': str(settings.data_volume_gib),
        'DataVolumeIops': str(settings.data_volume_iops), 'DataVolumeThroughput': str(settings.data_volume_throughput),
        'SpotMaxPrice': str(settings.spot_max_usd_per_hour), 'MonthlyLimitUsd': str(spending.monthly_limit_usd),
        'NoticeLowPercent': str(settings.notice_low), 'NoticeHighPercent': str(settings.notice_high),
        'StopPercent': str(settings.stop_percent), 'CostTagKey': spending.cost_explorer_tag_key,
        'CostTagValue': spending.cost_explorer_tag_value, 'Bootstrap': '',
        'ExistingDataVolumeId': settings.existing_data_volume_id,
        'ScalerUserName': settings.scaler_user_name,
        'DeploymentId': deployment_id,
    }


def fingerprint(settings: DeploymentSettings, parameters: dict[str, str], template: bytes) -> str:
    content = json.dumps({'region': settings.region, 'profile': settings.profile,
                          'stack': settings.stack_name, 'owner': settings.ami_owner_id,
                          'release': settings.nixos_release, 'parameters': parameters}, sort_keys=True)
    return sha256(template + content.encode()).hexdigest()


@final
class Deployment:
    def __init__(self, settings_path: Path, directory: Path, output: Path, *,
                 execute: AwsExecutor = execute_command, clock: Callable[[], float] = time.time,
                 pause: Callable[[float], None] = time.sleep,
                 notify: Callable[[ledger.LimitEvent], bool] = ledger.send_limit_event) -> None:
        self.settings_path = settings_path
        self.directory = directory
        self.output = output
        self.execute = execute
        self.clock = clock
        self.pause = pause
        self.notify = notify
        self.admin: admin_secrets.AdminSecretSession = admin_secrets.AdminSecretSession(settings_path)

    def _migrate_legacy(self, settings: DeploymentSettings) -> None:
        saved = deployment_record(self.directory)
        outputs = ledger.mapping(saved.get('outputs', {}))
        if saved.get('stack_id') or not outputs.get('InstanceId') or saved.get('stack') != settings.stack_name:
            return
        api = self._api(settings)
        account = ledger.text(api.call('get-caller-identity', service='sts'), 'Account')
        if saved.get('region') != settings.region or saved.get('account_id', account) != account:
            raise DeploymentRefused('legacy deployment account or region differs; restore the matching configuration')
        stacks = ledger.rows(api.call('describe-stacks', '--stack-name', settings.stack_name, service='cloudformation'), 'Stacks')
        if len(stacks) != 1 or stacks[0].get('StackStatus') not in ('CREATE_COMPLETE', 'UPDATE_COMPLETE'):
            raise DeploymentRefused('legacy migration requires one complete stack')
        stack_id = ledger.text(stacks[0], 'StackId')
        if not stack_id.startswith(f'arn:aws:cloudformation:{settings.region}:{account}:stack/{settings.stack_name}/'):
            raise DeploymentRefused('legacy stack account or region differs; restore the matching configuration')
        def physical(logical: str) -> str:
            resource = ledger.mapping(api.call('describe-stack-resource', '--stack-name', stack_id,
                '--logical-resource-id', logical, service='cloudformation').get('StackResourceDetail'))
            return ledger.text(resource, 'PhysicalResourceId')
        if physical('Instance') != outputs['InstanceId']:
            raise DeploymentRefused('legacy InstanceId differs; restore the matching deployment record')
        live = self._stack(settings)
        volume_id, budget_name = live.get('DataVolumeId', ''), live.get('BudgetName', '')
        if not volume_id or not budget_name:
            raise DeploymentRefused('legacy migration requires the live volume ID and Budget name')
        if outputs.get('DataVolumeId', volume_id) != volume_id or outputs.get('BudgetName', budget_name) != budget_name:
            raise DeploymentRefused('legacy resources differ; restore the matching deployment record')
        volumes = ledger.rows(api.call('describe-volumes', '--volume-ids', volume_id), 'Volumes')
        if len(volumes) != 1 or volumes[0].get('VolumeId') != volume_id:
            raise DeploymentRefused('legacy volume identity differs')
        tags = {ledger.text(row, 'Key'): str(row.get('Value', '')) for row in ledger.rows(volumes[0], 'Tags')}
        if tags.get('aws:cloudformation:stack-id') != stack_id:
            raise DeploymentRefused('legacy volume lacks the verified StackId tag')
        # The immutable StackId keeps a migration retry stable after a lost tagging response.
        deployment_id = str(saved.get('deployment_id') or sha256(stack_id.encode()).hexdigest()[:32])
        if tags.get('DeploymentId') and tags['DeploymentId'] != deployment_id:
            raise DeploymentRefused('legacy volume belongs to another deployment')
        budget = ledger.mapping(api.call('describe-budget', '--account-id', account,
            '--budget-name', budget_name, service='budgets').get('Budget'))
        if budget.get('BudgetName') != budget_name:
            raise DeploymentRefused('legacy Budget identity differs')
        name = physical('ScalerUser')
        user = ledger.mapping(api.call('get-user', '--user-name', name, service='iam').get('User'))
        scaler = {'name': name, 'arn': ledger.text(user, 'Arn'), 'user_id': ledger.text(user, 'UserId')}
        if not scaler['arn'].startswith(f'arn:aws:iam::{account}:user/'):
            raise DeploymentRefused('legacy scaler account differs')
        _ = api.call('create-tags', '--resources', volume_id, '--tags', 'Key=DeploymentId,Value=' + deployment_id)
        saved.update(account_id=account, region=settings.region, stack_id=stack_id, deployment_id=deployment_id,
                     scaler_user=scaler, data_volume_id=volume_id, budget_name=budget_name, preserve_budget_name=True, outputs=live)
        write_text_atomic(self.directory / 'deployment.json', json.dumps(saved, sort_keys=True) + '\n')

    def _deployment_id(self) -> str:
        settings = read_deployment_settings(self.settings_path, self.directory)
        self._migrate_legacy(settings)
        saved = deployment_record(self.directory)
        if saved.get('stack') and saved['stack'] != settings.stack_name:
            if not saved.get('replacement_deployment_id'):
                saved['replacement_deployment_id'] = uuid4().hex
                write_text_atomic(self.directory / 'deployment.json', json.dumps(saved, sort_keys=True) + '\n')
            return ledger.text(saved, 'replacement_deployment_id')
        if not saved.get('deployment_id'):
            saved.update(deployment_id=uuid4().hex)
            _ = saved.setdefault('stack', settings.stack_name)
            _ = saved.setdefault('region', settings.region)
            _ = saved.setdefault('outputs', {})
            write_text_atomic(self.directory / 'deployment.json', json.dumps(saved, sort_keys=True) + '\n')
        return ledger.text(saved, 'deployment_id')

    def _parameters(self, settings: DeploymentSettings, notification: BudgetNotificationAddress) -> dict[str, str]:
        deployment_id = self._deployment_id()
        saved = deployment_record(self.directory)
        parameters = deployment_parameters(self.settings_path, settings, notification, deployment_id)
        image = box.saved_image(self._api(settings), self.directory)
        if image.get('State') == 'available':
            parameters['AmiId'] = ledger.text(image, 'ImageId')
        parameters['BudgetName'] = str(saved.get('budget_name', '')) if saved.get('preserve_budget_name') else ''
        return parameters

    def _reserve_stack(self, settings: DeploymentSettings, parameters: dict[str, str]) -> None:
        api = self._api(settings)
        self._migrate_legacy(settings)
        saved = deployment_record(self.directory)
        account = ledger.text(api.call('get-caller-identity', service='sts'), 'Account')
        if saved.get('account_id', account) != account or saved.get('region') != settings.region:
            raise DeploymentRefused('the deployment account or region differs')
        stacks: tuple[dict[str, object], ...]
        try:
            stacks = ledger.rows(api.call('describe-stacks', '--stack-name', settings.stack_name, service='cloudformation'), 'Stacks')
        except AwsUnavailable as error:
            if 'ValidationError' not in str(error) or 'does not exist' not in str(error):
                raise
            stacks = ()
        if stacks and stacks[0].get('StackStatus') != 'REVIEW_IN_PROGRESS':
            if saved.get('stack_id') != stacks[0].get('StackId'):
                raise DeploymentRefused('the stack lacks the saved immutable identity')
            return
        if stacks and saved.get('stack_id') and saved.get('stack_id') == stacks[0].get('StackId'):
            return
        if saved.get('stack') != settings.stack_name:
            outputs = ledger.mapping(saved.get('outputs', {}))
            outputs['ExistingDataInput'] = parameters['ExistingDataVolumeId']
            saved['outputs'] = outputs
            for key in ('scaler_user', 'stack_id', 'budget_name', 'replacement_deployment_id', 'preserve_budget_name'):
                _ = saved.pop(key, None)
        saved.update(account_id=account, region=settings.region, stack=settings.stack_name,
                     deployment_id=parameters['DeploymentId'])
        write_text_atomic(self.directory / 'deployment.json', json.dumps(saved, sort_keys=True) + '\n')
        change_name = 'identity-' + parameters['DeploymentId']
        try:
            reply = api.call('describe-change-set', '--stack-name', settings.stack_name,
                             '--change-set-name', change_name, service='cloudformation')
        except AwsUnavailable as error:
            if 'does not exist' not in str(error) and 'ChangeSetNotFound' not in str(error):
                raise
            reply = {}
        if reply:
            values = {ledger.text(row, 'ParameterKey'): row.get('ParameterValue') for row in ledger.rows(reply, 'Parameters')}
            stack_id = ledger.text(reply, 'StackId')
            if (values.get('DeploymentId') != parameters['DeploymentId'] or not stack_id.startswith(
                    f'arn:aws:cloudformation:{settings.region}:{account}:stack/{settings.stack_name}/')):
                raise DeploymentRefused('the reserved change set differs from the saved deployment identity')
        reserved = bool(reply)
        started = self.clock()
        try:
            if not reply:
                # A CREATE change set reserves the StackId before any resource can exist.
                reply = api.call('create-change-set', '--stack-name', settings.stack_name,
                                 '--change-set-name', change_name, '--change-set-type', 'CREATE',
                                 '--template-body', 'file://' + str(self.output / 'template.json'),
                                 '--capabilities', 'CAPABILITY_NAMED_IAM', '--parameters', json.dumps([
                                     {'ParameterKey': key, 'ParameterValue': value} for key, value in
                                     {**parameters, 'EnableCompute': 'false'}.items()]), service='cloudformation')
                reserved = True
            saved['stack_id'] = ledger.text(reply, 'StackId')
            write_text_atomic(self.directory / 'deployment.json', json.dumps(saved, sort_keys=True) + '\n')
            _ = api.call('wait', 'change-set-create-complete', '--stack-name', ledger.text(saved, 'stack_id'),
                         '--change-set-name', change_name, service='cloudformation',
                         timeout_s=conf.read(self.settings_path).whole('stack_wait_timeout_s'))
        except (AwsUnavailable, subprocess.SubprocessError) as error:
            stack_id = str(saved.get('stack_id') or settings.stack_name)
            try:
                reply = api.call('describe-change-set', '--stack-name', stack_id,
                                 '--change-set-name', change_name, service='cloudformation')
            except (AwsUnavailable, subprocess.SubprocessError):
                pass
            failure = self._stack_failure(api, settings, stack_id, started, error, reply, parameters['NotificationEmail'])
            leftover = self._discard_change_set(api, stack_id, change_name) if reserved else ''
            raise AwsUnavailable(str(failure) + leftover) from None

    def _save_scaler_identity(self, settings: DeploymentSettings) -> None:
        saved = deployment_record(self.directory)
        api = self._api(settings)
        resource = ledger.mapping(api.call('describe-stack-resource', '--stack-name', ledger.text(saved, 'stack_id'),
                                           '--logical-resource-id', 'ScalerUser', service='cloudformation').get('StackResourceDetail'))
        name = ledger.text(resource, 'PhysicalResourceId')
        user = ledger.mapping(api.call('get-user', '--user-name', name, service='iam').get('User'))
        identity = {'name': name, 'arn': ledger.text(user, 'Arn'), 'user_id': ledger.text(user, 'UserId')}
        if saved.get('scaler_user', identity) != identity:
            raise DeploymentRefused('the scaler IAM identity differs from the deployment record')
        saved['scaler_user'] = identity
        write_text_atomic(self.directory / 'deployment.json', json.dumps(saved, sort_keys=True) + '\n')

    def _guard(self) -> ledger.SpendingAllowed:
        decision = ledger.spending_guard(self.settings_path, self.directory, 'deployment',
                                         clock=self.clock, notify=self.notify)
        if not isinstance(decision, ledger.SpendingAllowed):
            raise DeploymentRefused(decision.reason)
        if not ledger.CostLedger(self.directory).decision_is_current(decision):
            raise DeploymentRefused('the deployment guard has no durable confirmation')
        return decision

    def _api(self, settings: DeploymentSettings) -> AwsCli:
        return AwsCli(settings.profile, settings.region, settings.timeout_s, self.admin.executor(self.execute))

    def _print_spot_quota(self, api: AwsCli, largest: int) -> None:
        """Show the regional gate quota without stopping deployment on a failed read."""
        try:
            quota = spot_quota(api)
        except (admin_secrets.SecretsRefused, DeploymentRefused):
            raise
        except (KeyError, OSError, ValueError, AwsUnavailable, subprocess.SubprocessError) as error:
            print(f'{api.region}: Spot vCPU quota unknown: {self.admin.redact(str(error))}')
            return
        print(f'{api.region}: Spot quota {quota.normalize():f} vCPUs')
        if largest == 0:
            print(f'{api.region}: gate vCPU count is unknown')
        elif quota < largest:
            print(f'{api.region}: cannot run one gate ({largest} vCPUs); ask AWS for a Spot vCPU quota increase in this region.')


    def _observe(self, settings: DeploymentSettings, instance_id: str, *,
                 confirms_transition: bool = False) -> ledger.InstanceObservation:
        evidence = self._api(settings).observe(instance_id, int(self.clock()))
        observation = ledger.InstanceObservation(evidence.observed_at, evidence.state, 'confirmed deployment instance state')
        ledger.CostLedger(self.directory).observe(observation, confirms_transition=confirms_transition)
        return observation


    def _reconcile_compute_request(self, settings: DeploymentSettings, request: ledger.ComputeRequest) -> dict[str, str]:
        requested = replace(settings, stack_name=request.stack, region=request.region)
        stack = self._stack(requested)
        book = ledger.CostLedger(self.directory)
        if not stack or stack.get('ComputeEnabled') == 'false' and not stack.get('InstanceId'):
            now = int(self.clock())
            book.observe(ledger.InstanceObservation(now, 'terminated', 'confirmed absence of requested compute resources'),
                         confirms_transition=True)
            book.resolve_compute_request(request, ledger.ComputeResourcesAbsent(now))
            return {}
        if stack.get('ComputeEnabled') != 'true' or not stack.get('InstanceId') or not stack.get('DataVolumeId'):
            raise DeploymentRefused('resource identities are missing; reconcile the stack before retry')
        write_text_atomic(self.directory / 'deployment.json', json.dumps({
            **deployment_record(self.directory), 'stack': request.stack, 'region': request.region, 'at': int(self.clock()), 'outputs': stack,
        }, sort_keys=True) + '\n')
        observation = self._observe(requested, stack['InstanceId'], confirms_transition=True)
        previous_instances = {r.resolution.instance_id for r in book.read_snapshot().compute_requests
                              if isinstance(r.resolution, ledger.ComputeInstanceConfirmed)}
        if observation.state == 'terminated' and stack['InstanceId'] in previous_instances:
            raise DeploymentRefused('the previous instance does not confirm the requested compute resources')
        book.resolve_compute_request(request, ledger.ComputeInstanceConfirmed(observation.at, stack['InstanceId']))
        return stack

    def _load(self, notification: BudgetNotificationAddress
              ) -> tuple[DeploymentSettings, dict[str, str]]:
        settings = read_deployment_settings(self.settings_path, self.directory)
        parameters = self._parameters(settings, notification)
        self._before_request(settings, parameters)
        return settings, parameters

    def _before_request(self, settings: DeploymentSettings, parameters: dict[str, str]) -> None:
        _ = self._guard()
        self._confirm_preparation(settings, parameters)

    def prepare_hana(self) -> PreparedDeployment:
        """Save local preparation without accessing AWS or changing placement."""
        admin_secrets.refuse_plain_admin(self.settings_path)
        with control_lock(self.directory):
            settings, parameters, template = self._hana_inputs()
            decision = self._guard()
            prepared = self._hana_output()._write_preparation(settings, parameters, template, decision)
            path = self.directory / 'hana' / 'setup.json'
            if not path.exists():
                write_text_atomic(path, json.dumps({'version': 1, 'deployment': parameters['DeploymentId'],
                    'stack': settings.stack_name, 'region': settings.region}, sort_keys=True) + '\n')
            return prepared

    def hana_setup(self, *, approve_resources: bool = False) -> bool:
        """Deploy one retained Spot box and confirm its stopped state before registration."""
        workload = conf.read_or_repository('hana_aws')
        limits = hana_spend.read_settings(workload)
        print(f'Hana plan: separate stack; {limits.instance_type}; persistent Spot; interruption stop')
        print(f'Hana plan: root {limits.root_gib} GiB; data {limits.data_gib} GiB; compute ceiling ${limits.spot_max_usd_per_hour}/hour')
        print('Hana resources: instance, retained data disk, launch template, security group, private control bucket, scaler policy')
        print('Hana placement remains disabled')
        if not approve_resources:
            return False
        self._authorize(True)
        child = self._hana_output()
        with control_lock(self.directory):
            settings, parameters, template = self._hana_inputs()
            now = int(self.clock())
            child.confirm_preparation_evidence(settings, parameters, template)
            _ = self._guard()
            api = self._api(settings)
            with step('hana-setup', 'checking the image and subnet', self.settings_path):
                self._validate_inputs(settings, parameters)
            path = self.directory / 'hana' / 'setup.json'
            saved = remote.read_record(path)
            book = ledger.CostLedger(self.directory)
            spending = hana_spend.HanaSpending(book, self.settings_path, limits)
            if 'generation' not in saved:
                # Explicit setup approval reserves resources without enabling command admission.
                reservation = spending.reserve_setup(now, now + limits.permit_max_seconds)
                if isinstance(reservation, hana_spend.PermitRefused):
                    raise DeploymentRefused(reservation.reason)
                saved['generation'] = reservation.exposure.generation
                saved['template_sha256'] = sha256(template).hexdigest()
                saved['parameters_sha256'] = sha256(json.dumps(parameters, sort_keys=True).encode()).hexdigest()
                write_text_atomic(path, json.dumps(saved, sort_keys=True) + '\n')
            elif (saved.get('template_sha256') != sha256(template).hexdigest()
                    or saved.get('parameters_sha256') != sha256(json.dumps(parameters, sort_keys=True).encode()).hexdigest()):
                raise DeploymentRefused('The approved Hana template changed; review the saved deployment')
            # Resume existing resources through reads before another stack mutation.
            outputs = child._stack(settings) if 'generation' in saved and saved.get('requested') else {}
            if not outputs:
                active = [row for row in book.read_snapshot().hana_compute
                    if row.generation == saved['generation'] and isinstance(row.state, ledger.HanaExposureOpen)]
                if not active or int(self.clock()) >= active[0].expires:
                    raise DeploymentRefused('The Hana setup reservation expired; reconcile the separate stack before retry')
                saved['requested'] = True
                write_text_atomic(path, json.dumps(saved, sort_keys=True) + '\n')
                # CloudFormation deploy uses its existing stack waiter and private admin executor.
                with step('hana-setup', 'updating the separate Hana stack', self.settings_path):
                    _ = api.call('deploy', '--stack-name', settings.stack_name,
                        '--template-file', str(child.output / 'template.json'), '--capabilities', 'CAPABILITY_NAMED_IAM',
                        '--no-fail-on-empty-changeset', '--parameter-overrides',
                        *[f'{key}={value}' for key, value in parameters.items()], service='cloudformation',
                        timeout_s=conf.read(self.settings_path).whole('stack_wait_timeout_s'))
                outputs = child._stack(settings)
            instance, bucket = outputs['Instance'], outputs['ControlBucket']
            if saved.get('instance', instance) != instance or saved.get('data_volume', outputs['DataVolume']) != outputs['DataVolume']:
                raise DeploymentRefused('Hana resource identities changed; review replacement before setup')
            observed = api.observe(instance, int(self.clock()))
            reply = api.call('describe-instances', '--instance-ids', instance)
            instances = [row for group in ledger.rows(reply, 'Reservations') for row in ledger.rows(group, 'Instances')]
            if len(instances) != 1 or instances[0].get('InstanceId') != instance:
                raise DeploymentRefused('Hana instance confirmation differs')
            request = ledger.text(instances[0], 'SpotInstanceRequestId')
            requests = ledger.rows(api.call('describe-spot-instance-requests', '--spot-instance-request-ids', request), 'SpotInstanceRequests')
            if (len(requests) != 1 or requests[0].get('InstanceId') != instance
                    or requests[0].get('Type') != 'persistent'
                    or requests[0].get('InstanceInterruptionBehavior') != 'stop'):
                raise DeploymentRefused('Hana requires a persistent Spot request with stop behavior')
            saved.update(instance=instance, spot_request=request, bucket=bucket, data_volume=outputs['DataVolume'])
            write_text_atomic(path, json.dumps(saved, sort_keys=True) + '\n')
            with step('hana-setup', 'confirming the idle stop', self.settings_path):
                if observed.state not in ('stopped', 'stopping'):
                    _ = api.call('stop-instances', '--instance-ids', instance)
                _ = api.call('wait', 'instance-stopped', '--instance-ids', instance)
                confirmed = api.observe(instance, int(self.clock()))
                if confirmed.state != 'stopped':
                    raise DeploymentRefused('Hana stop confirmation is incomplete; repeat approved setup')
                spending.reconcile(int(self.clock()), 'stopped', generation=ledger.instant(saved, 'generation'))
        provider = AwsHanaProvider.from_settings(self.settings_path, self.directory, conf.path('hana_aws'))
        hana_box.HanaController(self.directory, self.settings_path, conf.path('hana_aws'), provider,
            clock=self.clock).register(hana_box.DeploymentIdentity(parameters['DeploymentId'], instance, request, settings.region))
        print('Hana setup complete: the box is stopped; placement remains disabled')
        return True

    def _hana_inputs(self) -> tuple[DeploymentSettings, dict[str, str], bytes]:
        """Share the existing deployment validation and use only Hana sizes and prices."""
        workload = conf.read_or_repository('hana_aws')
        limits = hana_spend.read_settings(workload)
        names = {key: workload.text(key) for key in ('stack_name', 'availability_zone', 'ami_id', 'ami_owner_id', 'vpc_id', 'subnet_id')}
        if not all(names.values()):
            raise DeploymentRefused('Configure the reviewed Hana image, owner, VPC, and subnet before prepare')
        settings = replace(read_deployment_settings(self.settings_path, self.directory), **names,
            region=limits.region, instance_type=limits.instance_type, root_volume_gib=limits.root_gib,
            data_volume_gib=limits.data_gib, spot_max_usd_per_hour=limits.spot_max_usd_per_hour,
            existing_data_volume_id='', timeout_s=workload.whole('aws_timeout_s'))
        if settings.stack_name == conf.read(self.settings_path).text('stack_name'):
            raise DeploymentRefused('The Hana stack must differ from the gate stack')
        if not settings.availability_zone.startswith(settings.region):
            raise DeploymentRefused('The Hana zone must belong to its region')
        identity_path = self.directory / 'hana' / 'setup.json'
        saved = remote.read_record(identity_path) if identity_path.exists() else {}
        identity = ledger.text(saved, 'deployment') if saved else uuid4().hex
        if saved and saved.get('stack') != settings.stack_name:
            raise DeploymentRefused('The saved Hana stack differs; review replacement before setup')
        parameters = {
            'DeploymentId': identity, 'Image': settings.ami_id, 'InstanceType': limits.instance_type,
            'AvailabilityZone': settings.availability_zone, 'VpcId': settings.vpc_id, 'SubnetId': settings.subnet_id,
            'RootDevice': settings.root_device_name, 'RootGiB': str(limits.root_gib), 'DataGiB': str(limits.data_gib),
            'SpotMaxPrice': str(limits.spot_max_usd_per_hour), 'ScalerUser': settings.scaler_user_name,
        }
        return settings, parameters, HANA_TEMPLATE.read_bytes()

    def _hana_output(self) -> Deployment:
        """Use separate preparation files with the existing deployment helper."""
        child = Deployment(self.settings_path, self.directory, self.output / 'hana',
            execute=self.execute, clock=self.clock, pause=self.pause, notify=self.notify)
        child.admin = self.admin
        return child

    def _write_preparation(self, settings: DeploymentSettings, parameters: dict[str, str], template: bytes,
                           decision: ledger.SpendingAllowed, resources: tuple[ledger.PricedResource, ...] = ()) -> PreparedDeployment:
        prepared = PreparedDeployment(int(self.clock()), fingerprint(settings, parameters, template), parameters)
        self.output.mkdir(parents=True, exist_ok=True)
        write_text_atomic(self.output / 'template.json', template.decode())
        write_text_atomic(self.output / 'parameters.json', json.dumps([
            {'ParameterKey': key, 'ParameterValue': value} for key, value in parameters.items()
        ], sort_keys=True) + '\n')
        write_text_atomic(self.output / 'preflight.json', json.dumps({
            'at': prepared.at, 'fingerprint': prepared.fingerprint,
            'decision': ledger.decision_record(decision),
            'inventory': [{'name': resource.name, 'usd_per_month': str(resource.usd_per_month)} for resource in resources],
        }, sort_keys=True) + '\n')
        return prepared

    def prepare(self) -> PreparedDeployment:
        admin_secrets.refuse_plain_admin(self.settings_path)
        with control_lock(self.directory):
            settings = read_deployment_settings(self.settings_path, self.directory)
            now = int(self.clock())
            notification = read_budget_notification_address(self.settings_path)
            parameters = self._parameters(settings, notification)
            book = ledger.CostLedger(self.directory)
            state = book.read_snapshot() if book.path.exists() else ledger.LedgerEvidence()
            resources = state.inventories[-1].resources if state.inventories else ()
            if not state.inventories:
                book.record_inventory(ledger.ResourceInventory(now, resources, 'Budget foundation', builder_removed=True))
            decision = self._guard()
            template = TEMPLATE.read_bytes()
            return self._write_preparation(settings, parameters, template, decision, resources)

    def _stack(self, settings: DeploymentSettings) -> dict[str, str]:
        try:
            row = self._api(settings).call('describe-stacks', '--stack-name', settings.stack_name, service='cloudformation')
        except AwsUnavailable as error:
            if 'does not exist' in str(error) and 'ValidationError' in str(error):
                return {}
            raise
        stacks = ledger.rows(row, 'Stacks')
        if len(stacks) != 1:
            raise DeploymentRefused('expected one stack')
        stack = stacks[0]
        saved = deployment_record(self.directory)
        if (saved.get('stack_id') and saved.get('stack') == settings.stack_name
                and stack.get('StackId') != saved['stack_id']):
            raise DeploymentRefused('the live StackId differs from the deployment record')
        status = ledger.text(stack, 'StackStatus')
        if status == 'REVIEW_IN_PROGRESS':
            return {}
        if status not in ('CREATE_COMPLETE', 'UPDATE_COMPLETE', 'UPDATE_ROLLBACK_COMPLETE'):
            raise DeploymentRefused(f'reconcile the stack before deployment: {status}')
        outputs: dict[str, str] = {}
        for item in ledger.rows(stack, 'Outputs'):
            value = item.get('OutputValue')
            if not isinstance(value, str):
                raise DeploymentRefused('stack output values must contain text')
            outputs[ledger.text(item, 'OutputKey')] = value
        return outputs

    def _validate_inputs(self, settings: DeploymentSettings, parameters: dict[str, str], instance: str = '') -> None:
        reply = self._api(settings).call('describe-images', '--image-ids', settings.ami_id,
                           '--owners', settings.ami_owner_id)
        images = ledger.rows(reply, 'Images')
        if len(images) != 1:
            raise DeploymentRefused('the selected official NixOS image is unavailable')
        image = images[0]
        if (image.get('ImageId') != settings.ami_id or image.get('OwnerId') != settings.ami_owner_id
                or image.get('Architecture') != 'x86_64' or image.get('State') != 'available'
                or 'nixos' not in str(image.get('Name', '')).lower()
                or settings.nixos_release not in str(image.get('Name', ''))
                or image.get('RootDeviceName') != settings.root_device_name):
            raise DeploymentRefused('the image identity, release, architecture or root device does not match')
        subnet = self._api(settings).call('describe-subnets', '--subnet-ids', settings.subnet_id)
        rows = ledger.rows(subnet, 'Subnets')
        if len(rows) != 1 or rows[0].get('VpcId') != settings.vpc_id or rows[0].get('AvailabilityZone') != settings.availability_zone:
            raise DeploymentRefused('the subnet does not match the VPC and availability zone')
        if settings.existing_data_volume_id:
            reply = self._api(settings).call('describe-volumes', '--volume-ids', settings.existing_data_volume_id)
            volumes = ledger.rows(reply, 'Volumes')
            if len(volumes) != 1:
                raise DeploymentRefused('the retained volume is unavailable')
            volume = volumes[0]
            tags = {str(tag.get('Key')): str(tag.get('Value')) for tag in ledger.rows(volume, 'Tags')}
            attached = (volume.get('State') == 'in-use' and bool(instance)
                        and len(ledger.rows(volume, 'Attachments')) == 1
                        and ledger.rows(volume, 'Attachments')[0].get('InstanceId') == instance)
            if (volume.get('AvailabilityZone') != settings.availability_zone or volume.get('Encrypted') is not True
                    or (volume.get('State') != 'available' and not attached) or volume.get('Size') != settings.data_volume_gib
                    or volume.get('VolumeType') != 'gp3' or volume.get('Iops') != settings.data_volume_iops
                    or volume.get('Throughput') != settings.data_volume_throughput
                    or tags.get(parameters['CostTagKey']) != parameters['CostTagValue']):
                raise DeploymentRefused('the retained volume must match its instance, encryption, tags and selected zone')

    def _adopt_volume(self, settings: DeploymentSettings, parameters: dict[str, str]) -> None:
        volume_id = settings.existing_data_volume_id
        if not volume_id:
            return
        api = self._api(settings)
        volumes = ledger.rows(api.call('describe-volumes', '--volume-ids', volume_id), 'Volumes')
        if len(volumes) != 1 or volumes[0].get('VolumeId') != volume_id:
            raise DeploymentRefused('the reused volume identity differs')
        tags = {ledger.text(row, 'Key'): str(row.get('Value', '')) for row in ledger.rows(volumes[0], 'Tags')}
        owner = tags.get('DeploymentId', '')
        if owner and owner != parameters['DeploymentId']:
            receipt = self.directory / 'down-complete.json'
            previous = remote.read_record(receipt) if receipt.exists() else {}
            if previous.get('deployment_id') != owner or previous.get('data_volume_id') != volume_id:
                raise DeploymentRefused('the reused volume owner has no matching down-complete receipt')
        if owner != parameters['DeploymentId']:
            _ = api.call('create-tags', '--resources', volume_id,
                         '--tags', 'Key=DeploymentId,Value=' + parameters['DeploymentId'])
        saved = deployment_record(self.directory)
        saved['data_volume_id'] = volume_id
        write_text_atomic(self.directory / 'deployment.json', json.dumps(saved, sort_keys=True) + '\n')

    @staticmethod
    def _discard_change_set(api: AwsCli, stack_id: str, name: str) -> str:
        try:
            _ = api.call('delete-change-set', '--stack-name', stack_id, '--change-set-name', name, service='cloudformation')
        except (ValueError, OSError, AwsUnavailable, subprocess.SubprocessError):
            return '\nunexecuted change set remains: ' + name
        return ''

    def _stack_failure(self, api: AwsCli, settings: DeploymentSettings, stack_id: str, started: float,
                       error: Exception, change: dict[str, object], notification: str) -> AwsUnavailable:
        lines = [str(error)]
        if change.get('StatusReason'):
            lines.append(str(change['StatusReason']))
        try:
            events = api.call('describe-stack-events', '--stack-name', stack_id, service='cloudformation')
            for event in ledger.rows(events, 'StackEvents'):
                stamp = datetime.fromisoformat(ledger.text(event, 'Timestamp').replace('Z', '+00:00')).timestamp()
                status = ledger.text(event, 'ResourceStatus')
                if stamp >= started and status.endswith('_FAILED'):
                    lines.append(f"{ledger.text(event, 'LogicalResourceId')} ({ledger.text(event, 'ResourceType')}): "
                                 + f"{status}: {event.get('ResourceStatusReason', '')}")
        except (ValueError, AwsUnavailable, subprocess.SubprocessError):
            lines.append('stack events are unavailable')
        try:
            stacks = ledger.rows(api.call('describe-stacks', '--stack-name', stack_id, service='cloudformation'), 'Stacks')
            for stack in stacks:
                status = ledger.text(stack, 'StackStatus')
                lines.append('stack status: ' + status)
                if status == 'UPDATE_ROLLBACK_COMPLETE':
                    lines.append('the stack rolled back; the box and the volume did not change')
                uncertain = (isinstance(error, subprocess.TimeoutExpired)
                             or any(word in str(error).lower() for word in ('timed out', 'timeout', 'max attempts exceeded')))
                if status.endswith('_COMPLETE') and not uncertain:
                    try:
                        for request in ledger.CostLedger(self.directory).pending_compute_requests():
                            _ = self._reconcile_compute_request(settings, request)
                    except (ValueError, OSError, AwsUnavailable, subprocess.SubprocessError):
                        lines.append('compute request reconciliation is unavailable')
        except (ValueError, AwsUnavailable, subprocess.SubprocessError):
            lines.append('stack status is unavailable')
        return AwsUnavailable('\n'.join(lines).replace(notification, '<notification address>'))

    def _update_stack(self, settings: DeploymentSettings, parameters: dict[str, str], *,
                      remove_builder: bool = False) -> None:
        api = self._api(settings)
        stack_id = ledger.text(deployment_record(self.directory), 'stack_id')
        live = self._stack(settings)
        notification = parameters.get('NotificationEmail') or '<notification address>'
        values: list[dict[str, object]] = [
            {'ParameterKey': key, 'UsePreviousValue': True} if remove_builder and key != 'EnableCompute' or key in ('Bootstrap', 'AmiId') and live.get('InstanceId')
            else {'ParameterKey': key, 'ParameterValue': value}
            for key, value in {**parameters, 'EnableCompute': 'false'}.items()
        ]
        if not remove_builder and (self.directory / 'gate-deployment.json').exists():
            template = ledger.mapping(cast(object, json.loads(TEMPLATE.read_text())))
            values.extend({'ParameterKey': key, 'UsePreviousValue': True}
                          for key in ledger.mapping(template['Parameters']) if key.startswith('Gate') or key == 'EnableGates')
        name = 'update-' + uuid4().hex
        started = self.clock()
        change: dict[str, object] = {}
        created = False
        executed = False
        try:
            _ = api.call('create-change-set', '--stack-name', stack_id, '--change-set-name', name,
                         '--change-set-type', 'UPDATE', '--template-body', 'file://' + str(self.output / 'template.json'),
                         '--capabilities', 'CAPABILITY_NAMED_IAM', '--parameters', json.dumps(values), service='cloudformation')
            created = True
            wait_error: Exception | None = None
            try:
                _ = api.call('wait', 'change-set-create-complete', '--stack-name', stack_id,
                             '--change-set-name', name, service='cloudformation',
                             timeout_s=conf.read(self.settings_path).whole('stack_wait_timeout_s'))
            except (AwsUnavailable, subprocess.SubprocessError) as error:
                wait_error = error
            change = api.call('describe-change-set', '--stack-name', stack_id, '--change-set-name', name,
                              service='cloudformation')
            reason = str(change.get('StatusReason', ''))
            if change.get('Status') == 'FAILED' and any(text in reason.lower() for text in (
                    "didn't contain changes", 'no updates are to be performed', 'no changes')):
                _ = api.call('delete-change-set', '--stack-name', stack_id, '--change-set-name', name, service='cloudformation')
                return
            if wait_error is not None:
                raise wait_error
            if change.get('Status') != 'CREATE_COMPLETE':
                raise AwsUnavailable('the update change set is not complete')
            protected = {
                'AWS::EC2::Instance', 'AWS::EC2::Volume', 'AWS::EC2::VolumeAttachment',
                'AWS::EC2::NetworkInterface', 'AWS::EC2::NetworkInterfaceAttachment',
                'AWS::EC2::EIP', 'AWS::EC2::EIPAssociation', 'AWS::Budgets::Budget',
            }
            replacements: list[str] = []
            for item in ledger.rows(change, 'Changes'):
                resource = ledger.mapping(item.get('ResourceChange'))
                if remove_builder and resource.get('Action') == 'Remove' and resource.get('LogicalResourceId') in (
                        'Instance', 'DataVolume', 'DataAttachment', 'LaunchTemplate', 'InstanceRole', 'InstanceProfile',
                        'SecurityGroup', 'ScalerPolicy', 'StopRole', 'BudgetStop', 'StopPolicy'):
                    continue
                if resource.get('ResourceType') in protected and (resource.get('Action') == 'Remove'
                        or resource.get('Replacement') in ('True', 'Conditional')):
                    replacements.append(f"{ledger.text(resource, 'LogicalResourceId')} ({ledger.text(resource, 'ResourceType')})")
            if replacements:
                raise DeploymentRefused('up refuses replacement: ' + ', '.join(replacements)
                                        + '; run deploy.py down --approve-destroy, then deploy.py up --approve-resources')
            if not remove_builder:
                self._before_request(settings, parameters)
            _ = api.call('execute-change-set', '--stack-name', stack_id, '--change-set-name', name, service='cloudformation')
            executed = True
            _ = api.call('wait', 'stack-update-complete', '--stack-name', stack_id, service='cloudformation',
                         timeout_s=conf.read(self.settings_path).whole('stack_wait_timeout_s'))
        except (ValueError, AwsUnavailable, subprocess.SubprocessError) as error:
            if not change:
                try:
                    change = api.call('describe-change-set', '--stack-name', stack_id,
                                      '--change-set-name', name, service='cloudformation')
                    created = True
                except (AwsUnavailable, subprocess.SubprocessError):
                    pass
            leftover = self._discard_change_set(api, stack_id, name) if created and not executed else ''
            if isinstance(error, ValueError):
                raise DeploymentRefused((str(error) + leftover).replace(notification, '<notification address>')) from None
            failure = self._stack_failure(api, settings, stack_id, started, error, change, notification)
            raise AwsUnavailable(str(failure) + leftover) from None


    def _deploy(self, settings: DeploymentSettings, parameters: dict[str, str], ) -> None:
        self._before_request(settings, parameters)
        self._adopt_volume(settings, parameters)
        self._reserve_stack(settings, parameters)
        live = self._stack(settings)
        try:
            self._confirm_preparation(settings, parameters, renew=True)
            self._before_request(settings, parameters)
        except (ValueError, AwsUnavailable, subprocess.SubprocessError) as error:
            if not live:
                stack_id = ledger.text(deployment_record(self.directory), 'stack_id')
                leftover = self._discard_change_set(self._api(settings), stack_id, 'identity-' + parameters['DeploymentId'])
                if leftover:
                    reason = (str(error) + leftover).replace(parameters['NotificationEmail'], '<notification address>')
                    if isinstance(error, ValueError):
                        raise DeploymentRefused(reason) from None
                    raise AwsUnavailable(reason) from None
            raise
        if live:
            self._update_stack(settings, parameters)
        else:
            api = self._api(settings)
            started = self.clock()
            try:
                _ = api.call('deploy',
                    '--stack-name', ledger.text(deployment_record(self.directory), 'stack_id'),
                    '--template-file', str(self.output / 'template.json'),
                    '--capabilities', 'CAPABILITY_NAMED_IAM', '--no-fail-on-empty-changeset',
                    '--parameter-overrides', *[f'{key}={value}' for key, value in parameters.items()],
                    'EnableCompute=false', service='cloudformation')
            except (AwsUnavailable, subprocess.SubprocessError) as error:
                raise self._stack_failure(api, settings, ledger.text(deployment_record(self.directory), 'stack_id'),
                                          started, error, {}, parameters['NotificationEmail']) from None
        self._save_scaler_identity(settings)
        outputs = self._stack(settings)
        saved = deployment_record(self.directory)
        saved['outputs'] = {**ledger.mapping(saved.get('outputs', {})), **outputs}
        if outputs.get('DataVolumeId'):
            saved['data_volume_id'] = outputs['DataVolumeId']
        if outputs.get('BudgetName'):
            saved['budget_name'] = outputs['BudgetName']
        write_text_atomic(self.directory / 'deployment.json', json.dumps(saved, sort_keys=True) + '\n')

    def _confirm_preparation(self, settings: DeploymentSettings, parameters: dict[str, str], *, renew: bool = False) -> None:
        current = read_deployment_settings(self.settings_path, self.directory)
        notification = BudgetNotificationAddress(parameters['NotificationEmail'])
        current_parameters = self._parameters(current, notification)
        self.confirm_preparation_evidence(settings, parameters, TEMPLATE.read_bytes(), renew=renew,
            current_fingerprint=fingerprint(current, current_parameters, TEMPLATE.read_bytes()))

    def confirm_preparation_evidence(self, settings: DeploymentSettings, parameters: dict[str, str], template: bytes,
                                    *, renew: bool = False, current_fingerprint: str = '') -> None:
        """Verify saved preparation before either workload can create resources."""
        preflight = remote.read_record(self.output / 'preflight.json')
        at = ledger.instant(preflight, 'at')
        expected = current_fingerprint or fingerprint(settings, parameters, template)
        if ((not renew and not 0 <= int(self.clock()) - at <= settings.preflight_max_age_s)
                or preflight.get('fingerprint') != expected
                or fingerprint(settings, parameters, template) != expected
                or (self.output / 'template.json').read_bytes() != template):
            raise DeploymentRefused('deployment preparation expired or its inputs changed; run prepare again')
        if renew:
            preflight['at'] = int(self.clock())
            write_text_atomic(self.output / 'preflight.json', json.dumps(preflight, sort_keys=True) + '\n')

    def _authorize(self, approve_resources: bool) -> None:
        if not approve_resources:
            raise DeploymentRefused('the user must pass --approve-resources at their terminal')
        self._check_credentials()

    def _check_credentials(self) -> None:
        admin_secrets.refuse_plain_admin(self.settings_path)
        try:
            mode = admin_secrets.credentials_file().stat().st_mode
            if not stat.S_ISREG(mode) or stat.S_IMODE(mode) != 0o600:
                raise OSError
        except OSError:
            raise DeploymentRefused('credential_file must name a private mode 0600 file') from None


    def apply(self, *, approve_resources: bool = False) -> dict[str, str]:
        self._authorize(approve_resources)
        with control_lock(self.directory):
            settings, parameters = self._load(read_budget_notification_address(self.settings_path))
            stack = self._stack(settings)
            previous_path = self.directory / 'deployment.json'
            if previous_path.exists() and ledger.mapping(remote.read_record(previous_path).get('outputs', {})):
                previous = remote.read_record(previous_path)
                saved = ledger.mapping(previous.get('outputs'))
                if previous.get('region') != settings.region:
                    raise DeploymentRefused('preserve the region of the saved deployment')
                same_stack = previous.get('stack') == settings.stack_name
                if not same_stack or not stack:
                    if settings.existing_data_volume_id != saved.get('DataVolumeId'):
                        raise DeploymentRefused('replacement requires existing_data_volume_id equal to the saved DataVolumeId')
                    if not same_stack:
                        old_settings = replace(settings, stack_name=ledger.text(previous, 'stack'))
                        if self._stack(old_settings):
                            raise DeploymentRefused('stop and delete the old stack before replacement')
                    elif not stack:
                        raise DeploymentRefused('use a new stack_name and its saved DataVolumeId to preserve the retained Budget')
                elif stack.get('InstanceId') != saved.get('InstanceId') or stack.get('DataVolumeId') != saved.get('DataVolumeId'):
                    raise DeploymentRefused('the stack differs from the saved resource identities')
            if stack and stack.get('ExistingDataInput', '') != settings.existing_data_volume_id:
                raise DeploymentRefused('stop and delete the old stack, then select its saved DataVolumeId and a new stack_name')
            self._validate_inputs(settings, parameters, stack.get('InstanceId', ''))
            if not stack:
                self._deploy(settings, parameters)
                stack = self._stack(settings)
                if not stack.get('BudgetName') or stack.get('ComputeEnabled') != 'false':
                    raise DeploymentRefused('the Budget foundation has no confirmed evidence')
                self._confirm_preparation(settings, parameters, renew=True)
            self._deploy(settings, parameters)
            pending = ledger.CostLedger(self.directory).pending_compute_requests()
            outputs = self._stack(settings)
            for request in pending:
                outputs = self._reconcile_compute_request(settings, request)
            saved = deployment_record(self.directory)
            saved['fingerprint'] = fingerprint(settings, parameters, TEMPLATE.read_bytes())
            write_text_atomic(self.directory / 'deployment.json', json.dumps(saved, sort_keys=True) + '\n')
            self.scaler_key()
            return outputs


    def _scaler_api(self) -> AwsCli:
        settings = read_deployment_settings(self.settings_path, self.directory)
        profile = conf.read(self.settings_path).text('credentials_profile')
        return AwsCli(profile, settings.region, settings.timeout_s, self.execute)

    def _scaler_present(self) -> bool:
        try:
            reply = self._scaler_api().call('get-caller-identity', service='sts')
            user = read_deployment_settings(self.settings_path, self.directory).scaler_user_name
            return ledger.text(reply, 'Arn').endswith('user/' + user)
        except (OSError, ValueError, AwsUnavailable, subprocess.SubprocessError):
            return False


    def scaler_key(self) -> None:
        settings = read_deployment_settings(self.settings_path, self.directory)
        api = self._api(settings)
        credential_file = admin_secrets.credentials_file()
        previous = credential_file.read_bytes()
        key_id = ''
        checkpoint = self.directory / 'scaler-key.json'
        credentials = ConfigParser(interpolation=None)
        _ = credentials.read_string(previous.decode())
        profile = conf.read(self.settings_path).text('credentials_profile')
        if checkpoint.exists():
            saved_key = ledger.text(remote.read_record(checkpoint), 'access_key_id')
            if credentials.get(profile, 'aws_access_key_id', fallback='') == saved_key and self._scaler_present():
                self._remove_old_scaler_keys(api, settings.scaler_user_name, saved_key)
                checkpoint.unlink()
                return
            _ = api.call('delete-access-key', '--user-name', settings.scaler_user_name,
                         '--access-key-id', saved_key, service='iam')
            checkpoint.unlink()
        try:
            reply = api.call('create-access-key', '--user-name', settings.scaler_user_name, service='iam')
            key = ledger.mapping(reply.get('AccessKey'))
            key_id = ledger.text(key, 'AccessKeyId')
            secret = ledger.text(key, 'SecretAccessKey')
            write_text_atomic(checkpoint, json.dumps({'access_key_id': key_id}) + '\n', new_file_mode=0o600)
            if not credentials.has_section(profile):
                credentials.add_section(profile)
            credentials.set(profile, 'aws_access_key_id', key_id)
            credentials.set(profile, 'aws_secret_access_key', secret)
            content = StringIO()
            credentials.write(content)
            write_bytes_atomic(credential_file, content.getvalue().encode(), mode=0o600)
            attempts = conf.read(self.settings_path).whole('scaler_key_check_attempts')
            for attempt in range(attempts):
                if self._scaler_present():
                    break
                if attempt + 1 < attempts:
                    time.sleep(conf.read(self.settings_path).number('scaler_key_check_delay_s'))
            else:
                raise DeploymentRefused('the new scaler key did not confirm its IAM user')
        except (OSError, ValueError, AwsUnavailable, subprocess.SubprocessError):
            try:
                if key_id:
                    _ = api.call('delete-access-key', '--user-name', settings.scaler_user_name,
                                 '--access-key-id', key_id, service='iam')
            except (OSError, ValueError, AwsUnavailable, subprocess.SubprocessError):
                raise DeploymentRefused('scaler key setup failed; remove the unconfirmed key through IAM') from None
            finally:
                write_bytes_atomic(credential_file, previous, mode=0o600)
            checkpoint.unlink(missing_ok=True)
            raise DeploymentRefused('scaler key setup failed; the new key was removed') from None
        self._remove_old_scaler_keys(api, settings.scaler_user_name, key_id)
        checkpoint.unlink()

    def _remove_old_scaler_keys(self, api: AwsCli, user: str, key_id: str) -> None:
        try:
            reply = api.call('list-access-keys', '--user-name', user, service='iam')
            for old in ledger.rows(reply, 'AccessKeyMetadata'):
                old_id = ledger.text(old, 'AccessKeyId')
                if old_id != key_id:
                    _ = api.call('delete-access-key', '--user-name', user,
                                 '--access-key-id', old_id, service='iam')
        except (OSError, ValueError, AwsUnavailable, subprocess.SubprocessError):
            raise DeploymentRefused('the scaler key works; IAM could not remove every old key') from None


    def remove_images(self, *, approve_destroy: bool = False) -> bool:
        """Boot gates from the official image and delete the saved gate and former builder images."""
        if not approve_destroy:
            print(teardown.IMAGES_DRY_RUN)
            return True
        self._check_credentials()
        settings = read_deployment_settings(self.settings_path, self.directory)
        api = self._api(settings)
        with control_lock(self.directory / 'gate-up'):
            deployment = gate_deployment(self.directory / 'gate-deployment.json', settings.region)
            home = ledger.rows(deployment, 'regions')[0]
            stack_id = ledger.text(deployment_record(self.directory), 'stack_id')
            body = api.call('get-template', '--stack-name', stack_id, service='cloudformation')['TemplateBody']
            template_body = ledger.mapping(cast(object, json.loads(body))) if isinstance(body, str) else ledger.mapping(body)
            image_policy = ledger.mapping(ledger.mapping(ledger.mapping(
                ledger.mapping(template_body['Resources'])['GateScalerPolicy'])['Properties'])['PolicyDocument'])
            statements = [row for row in ledger.rows(image_policy, 'Statement') if row.get('Sid') != 'GateImageTransition']
            template = self.output / 'gate-image-template.json'
            self.output.mkdir(parents=True, exist_ok=True)
            if home.get('image') != settings.ami_id:
                # Keep the old image launchable until gate-deployment.json names the official image.
                image_policy['Statement'] = [*statements, {
                    'Sid': 'GateImageTransition', 'Effect': 'Allow', 'Action': ['ec2:RunInstances'],
                    'Resource': {'Fn::Sub': 'arn:${AWS::Partition}:ec2:${AWS::Region}::image/'
                                 + ledger.text(home, 'image')}}]
                write_text_atomic(template, json.dumps(template_body))
                stack = ledger.rows(api.call('describe-stacks', '--stack-name', stack_id,
                                            service='cloudformation'), 'Stacks')[0]
                changes: list[dict[str, object]] = [
                    {'ParameterKey': ledger.text(row, 'ParameterKey'), 'UsePreviousValue': True}
                    for row in ledger.rows(stack, 'Parameters') if row['ParameterKey'] != 'GateImage']
                changes.append({'ParameterKey': 'GateImage', 'ParameterValue': settings.ami_id})
                with step('remove-images', 'switching gates to the official image', self.settings_path,
                          hint='can take several minutes'):
                    self._gate_change(api, stack_id, template, changes)
                home.update(image=settings.ami_id, version=self._stack(settings)['GateLaunchVersion'])
                write_text_atomic(self.directory / 'gate-deployment.json', json.dumps(deployment, sort_keys=True) + '\n')
            if image_policy.get('Statement') != statements:
                image_policy['Statement'] = statements
                write_text_atomic(template, json.dumps(template_body))
                stack = ledger.rows(api.call('describe-stacks', '--stack-name', stack_id,
                                            service='cloudformation'), 'Stacks')[0]
                changes = [{'ParameterKey': ledger.text(row, 'ParameterKey'), 'UsePreviousValue': True}
                           for row in ledger.rows(stack, 'Parameters')]
                with step('remove-images', 'removing the old image permission', self.settings_path,
                          hint='can take several minutes'):
                    self._gate_change(api, stack_id, template, changes)
            with step('remove-images', 'deleting the gate image and its snapshot', self.settings_path):
                removed = teardown.remove_gate_images(api, self.directory, self.clock)
            with step('remove-images', 'deleting the former builder image and its snapshot', self.settings_path):
                removed = teardown.remove_builder_image(api, self.directory) or removed
        print('remove-images: done; gates boot from the official image and no saved image remains' if removed
              else 'remove-images: nothing to remove; gates boot from the official image')
        return True

    def _gate_change(self, api: AwsCli, stack_id: str, template: Path, changes: list[dict[str, object]]) -> None:
        name = 'gate-' + uuid4().hex
        executed = False
        try:
            print('cloud-gate: preparing the stack change (about 1 minute)', flush=True)
            _ = api.call('create-change-set', '--stack-name', stack_id, '--change-set-name', name,
                         '--change-set-type', 'UPDATE', '--template-body', 'file://' + str(template),
                         '--capabilities', 'CAPABILITY_NAMED_IAM', '--parameters', json.dumps(changes),
                         service='cloudformation')
            try:
                _ = api.call('wait', 'change-set-create-complete', '--stack-name', stack_id,
                             '--change-set-name', name, service='cloudformation',
                             timeout_s=conf.read(self.settings_path).whole('stack_wait_timeout_s'))
            except AwsUnavailable:
                pass
            change = api.call('describe-change-set', '--stack-name', stack_id,
                              '--change-set-name', name, service='cloudformation')
            reason = str(change.get('StatusReason', '')).lower()
            empty = change.get('Status') == 'FAILED' and any(text in reason for text in (
                "didn't contain changes", 'no updates are to be performed', 'no changes'))
            if not empty:
                if change.get('Status') != 'CREATE_COMPLETE':
                    raise DeploymentRefused(f"The gate change set is {change.get('Status')}: {change.get('StatusReason', 'no reason given')}")
                for item in ledger.rows(change, 'Changes'):
                    resource = ledger.mapping(item['ResourceChange'])
                    if not ledger.text(resource, 'LogicalResourceId').startswith('Gate'):
                        raise DeploymentRefused('The gate change set changes a Rust resource')
                    print(f"cloud-gate: {resource.get('Action', 'Change')} {ledger.text(resource, 'LogicalResourceId')}", flush=True)
                _ = api.call('execute-change-set', '--stack-name', stack_id, '--change-set-name', name,
                             service='cloudformation')
                executed = True
                print('cloud-gate: applying the change (several minutes)', flush=True)
                try:
                    _ = api.call('wait', 'stack-update-complete', '--stack-name', stack_id, service='cloudformation',
                                 timeout_s=conf.read(self.settings_path).whole('stack_wait_timeout_s'))
                except AwsUnavailable:
                    events = ledger.rows(api.call('describe-stack-events', '--stack-name', stack_id,
                                                  '--max-items', '60', service='cloudformation'), 'StackEvents')
                    for event in reversed(events):
                        if str(event.get('ResourceStatus', '')).endswith('_FAILED'):
                            failure = event.get('ResourceStatusReason', 'no reason given')
                            print(f"cloud-gate: {event.get('LogicalResourceId')} {event.get('ResourceStatus')}: {failure}", flush=True)
                    raise
        finally:
            if not executed:
                _ = self._discard_change_set(api, stack_id, name)

    def regional_image(self, api: AwsCli, settings: DeploymentSettings, region: str) -> str:
        """Find the matching release or retain a copied image for recovery."""
        target = api.in_region(region)
        source = ledger.rows(api.call('describe-images', '--image-ids', settings.ami_id), 'Images')[0]
        images = ledger.rows(target.call('describe-images', '--owners', ledger.text(source, 'OwnerId'),
            '--filters', 'Name=name,Values=' + ledger.text(source, 'Name'), 'Name=state,Values=available'), 'Images')
        if images:
            return ledger.text(images[0], 'ImageId')
        path = self.directory / 'regional-images.json'
        saved = remote.read_record(path) if path.exists() else {}
        identity = region + ':' + settings.ami_id
        if identity not in saved:
            copied = target.call('copy-image', '--source-region', settings.region, '--source-image-id', settings.ami_id,
                '--name', ledger.text(source, 'Name'), '--client-token', sha256(identity.encode()).hexdigest())
            saved[identity] = {'region': region, 'image': ledger.text(copied, 'ImageId')}
            write_text_atomic(path, json.dumps(saved, sort_keys=True) + '\n')
        image = ledger.text(ledger.mapping(saved[identity]), 'image')
        _ = target.call('wait', 'image-available', '--image-ids', image,
                       timeout_s=conf.read(self.settings_path).whole('stack_wait_timeout_s'))
        return image

    def regional_gate_stack(self, api: AwsCli, settings: DeploymentSettings, home: dict[str, str],
                            parameters: dict[str, str], region: str) -> dict[str, object]:
        """Deploy only gate resources in the target region."""
        target = api.in_region(region)
        vpcs = ledger.rows(target.call('describe-vpcs', '--filters', 'Name=is-default,Values=true'), 'Vpcs')
        if not vpcs:
            raise DeploymentRefused(region + ': no default VPC is available')
        vpc = ledger.text(vpcs[0], 'VpcId')
        try:
            subnets = gate_subnets(target, vpc)
        except DeploymentRefused as error:
            raise DeploymentRefused(region + ': ' + str(error)) from error
        image = self.regional_image(api, settings, region)
        account = ledger.text(api.call('get-caller-identity', service='sts'), 'Account')
        stack_id = ledger.text(deployment_record(self.directory), 'stack_id')
        partition = stack_id.split(':')[1]
        deployment = ledger.text(deployment_record(self.directory), 'deployment_id')
        prefix = f'arn:{partition}:ec2:{region}:{account}:'
        values = {**parameters, 'GateImage': image, 'VpcId': vpc,
            'DeploymentId': deployment, 'CostTagKey': read_settings(self.settings_path).cost_explorer_tag_key,
            'CostTagValue': read_settings(self.settings_path).cost_explorer_tag_value,
            'RootDeviceName': settings.root_device_name, 'ScalerUserName': settings.scaler_user_name,
            'HomeGateProfileArn': home['GateProfileArn'], 'HomeGateRoleArn': home['GateRoleArn'],
            'GateSubnets': ','.join(prefix + 'subnet/' + ledger.text(row, 'id') for row in subnets)}
        template = self.output / ('region-gates-' + region + '.json')
        write_text_atomic(template, json.dumps(region_gate_template(), sort_keys=True) + '\n')
        definitions = ledger.mapping(region_gate_template()['Parameters'])
        name = settings.stack_name + '-gates-' + region
        stack_path = self.directory / 'regional-stacks.json'
        stacks = remote.read_record(stack_path) if stack_path.exists() else {}
        stacks[region] = name
        write_text_atomic(stack_path, json.dumps(stacks) + '\n')
        _ = target.call('deploy', '--stack-name', name, '--template-file', str(template),
            '--capabilities', 'CAPABILITY_NAMED_IAM', '--no-fail-on-empty-changeset', '--parameter-overrides',
            *[key + '=' + value for key, value in values.items() if key in definitions], service='cloudformation',
            timeout_s=conf.read(self.settings_path).whole('stack_wait_timeout_s'))
        stack = ledger.rows(target.call('describe-stacks', '--stack-name', name, service='cloudformation'), 'Stacks')[0]
        outputs = {ledger.text(row, 'OutputKey'): ledger.text(row, 'OutputValue') for row in ledger.rows(stack, 'Outputs')}
        return {'region': region, 'image': image, 'subnets': subnets, 'template': outputs['GateLaunchTemplate'],
                'version': outputs['GateLaunchVersion'], 'security_group': outputs['GateSecurityGroup'], 'stack': name}

    def prices(self) -> None:
        """Print regional Spot prices without resource identities."""
        settings = read_deployment_settings(self.settings_path, self.directory)
        api = self._api(settings)
        from scripts.aws_target import cloud_gate
        values = cloud_gate.settings()
        configured_types = values.words('instance_types')
        largest = largest_gate_vcpus(configured_types)
        sizes = tuple(dict.fromkeys([*configured_types,
                                    'r7a.8xlarge', 'm8a.8xlarge', 'c8a.8xlarge', *values.words('gpu_instance_types')]))
        regions = tuple(dict.fromkeys([*gate_regions(settings.region), *values.words('spot_candidate_regions')]))
        _ = self.admin.get()
        print('region type zone price')
        for region in regions:
            target = api.in_region(region)
            self._print_spot_quota(target, largest)
            try:
                quota = spot_quota(target, 'L-3819A6DF')
                print(f'{region}: G and VT Spot quota {quota.normalize():f} vCPUs')
            except (admin_secrets.SecretsRefused, DeploymentRefused):
                raise
            except (KeyError, OSError, ValueError, AwsUnavailable, subprocess.SubprocessError) as error:
                code = re.search(r'An error occurred \(([A-Za-z][A-Za-z0-9]*)\) when calling ', str(error))
                reason = code[1] if code else type(error).__name__
                print(f'{region}: G and VT Spot quota unknown: {reason}')
            zones = [ledger.text(row, 'ZoneName') for row in ledger.rows(target.call('describe-availability-zones',
                '--filters', 'Name=state,Values=available'), 'AvailabilityZones')]
            prices = fetch_spot_prices(target, sizes, int(self.clock()))
            latest = {(row.instance_type, row.zone): row.usd_per_hour for row in prices}
            for size in sizes:
                for zone in sorted(zones):
                    print(region, size, zone, str(latest.get((size, zone), 'unavailable')))

    def spot_table(self) -> None:
        """Print the local snapshot without AWS credentials or network calls."""
        from scripts.aws_target import spot_table
        print(spot_table.render(self.directory, int(self.clock()), settings_path=self.settings_path))

    def quota_request(self) -> bool:
        """Request both regional Spot quotas without duplicating open requests."""
        settings = read_deployment_settings(self.settings_path, self.directory)
        api = self._api(settings)
        success = True
        for regions, code, desired, label in quota_targets(settings.region):
            for region in regions:
                if not request_spot_quota(api.in_region(region), code, desired, label):
                    success = False
        return success

    def quota_status(self) -> bool:
        """Report applied quotas and request history without changing AWS or local state."""
        from scripts.aws_target import gpu_size_trial
        settings = read_deployment_settings(self.settings_path, self.directory)
        api = self._api(settings)
        needed = gpu_size_trial.required_vcpus(gpu_size_trial.settings())
        regional: dict[str, list[tuple[str, str]]] = {}
        for regions, code, _desired, label in quota_targets(settings.region):
            for region in dict.fromkeys(regions):
                regional.setdefault(region, []).append((code, 'Standard Spot' if label == 'Spot' else label))
        _ = self.admin.get()
        success = True
        ready: list[str] = []
        opened: list[str] = []
        unknown: list[str] = []
        unknown_quota: list[str] = []
        for region, quotas in regional.items():
            print(region, flush=True)
            target = api.in_region(region)
            for code, name in quotas:
                applied: Decimal | None = None
                try:
                    applied = spot_quota(target, code)
                    print(f'  {name}: {applied.normalize():f} vCPUs applied', flush=True)
                    if code == 'L-3819A6DF' and applied >= needed:
                        ready.append(region)
                    history = target.call('list-requested-service-quota-change-history-by-quota',
                        '--service-code', 'ec2', '--quota-code', code, service='service-quotas')
                    requests = sorted(ledger.rows(history, 'RequestedQuotas'),
                        key=lambda row: quota_timestamp(row.get('LastUpdated') or row.get('Created')), reverse=True)
                    rows: list[list[str]] = []
                    for request in requests:
                        asked = f'{conf.decimal(str(request["DesiredValue"])).normalize():f}'
                        status = ledger.text(request, 'Status')
                        case = str(request.get('CaseId') or '-')
                        date = quota_date(request.get('LastUpdated') or request.get('Created'))
                        rows.append([asked, status, case, date])
                        if status not in CLOSED_QUOTA_REQUEST_STATUSES:
                            opened.append(f'{region} {name} {asked} vCPUs ({status})')
                    if rows:
                        for line in aligned_table(('Asked vCPUs', 'Status', 'Case ID', 'Updated'), rows,
                                                  right=frozenset((0,))):
                            print('    ' + line)
                    else:
                        print('    No requests.')
                except (admin_secrets.SecretsRefused, DeploymentRefused):
                    raise
                except (KeyError, OSError, ValueError, AwsUnavailable, subprocess.SubprocessError) as error:
                    match = re.search(r'An error occurred \(([A-Za-z][A-Za-z0-9]*)\) when calling ', str(error))
                    reason = match[1] if match else type(error).__name__
                    if applied is None:
                        print(f'  {name}: applied quota unknown')
                        if code == 'L-3819A6DF':
                            unknown_quota.append(region)
                    print('    Quota status failed: ' + reason)
                    unknown.append(region + ' ' + name)
                    success = False
            print()
        fits = ', '.join(ready)
        if unknown_quota:
            fits += (', ' if fits else '') + 'unknown for ' + ', '.join(unknown_quota)
        elif not fits:
            fits = 'no region has enough applied quota'
        requests_text = 'unknown' if unknown else ', '.join(opened) or 'none'
        incomplete = '; request history unavailable: ' + ', '.join(unknown) if unknown else ''
        print(f'Summary: GPU trial needs {needed} vCPUs; quota fits: {fits}; '
              + 'open requests: ' + requests_text + incomplete, flush=True)
        return success

    def gpu_size_trial(self, folder: Path, *, approve_resources: bool = False) -> bool:
        """Check terminal approval and start the scaler service."""
        from scripts.aws_target import cloud_gate, gpu_size_trial
        self._authorize(approve_resources)
        limits = gpu_size_trial.settings()
        timeout = (limits.lifetime_minutes * 60 * (len(limits.spot_max_usd_per_hour) + 2)
                   + cloud_gate.settings().number('termination_timeout_s') * len(limits.spot_max_usd_per_hour))
        def execute(arguments: Sequence[str]) -> int:
            return self.execute(arguments, capture_output=False, text=True, timeout=timeout, check=False).returncode
        return gpu_size_trial.start_service(folder, self.directory.resolve(),
                                            self.settings_path.resolve(), execute) == 0

    def gate_up(self, *, approve_resources: bool = False) -> bool:
        """Add the gate resources and preserve every Rust parameter."""
        from scripts.aws_target import cloud_gate, gate_trial, gpu_size_trial
        self._authorize(approve_resources)
        with control_lock(self.directory / 'gate-up'):
            settings = read_deployment_settings(self.settings_path, self.directory)
            regions = gate_regions(settings.region)
            api = self._api(settings)
            saved = deployment_record(self.directory)
            stack_id = ledger.text(saved, 'stack_id')
            _ = self._stack(settings)
            stack = ledger.rows(api.call('describe-stacks', '--stack-name', stack_id,
                                       service='cloudformation'), 'Stacks')[0]
            subnets = gate_subnets(api, settings.vpc_id)
            revision = gate_trial.host_nixpkgs_revision()
            account = ledger.text(api.call('get-caller-identity', service='sts'), 'Account')
            partition = stack_id.split(':')[1]
            prefix = f'arn:{partition}:ec2:{settings.region}:{account}:'
            values = cloud_gate.settings()
            parameters = {
                'EnableGates': 'true', 'GateImage': settings.ami_id,
                'GateSubnets': ','.join(prefix + 'subnet/' + ledger.text(row, 'id') for row in subnets),
                'GateInstanceTypes': ','.join(values.words('instance_types')),
                'GateGpuInstanceTypes': ','.join(gpu_size_trial.settings().spot_max_usd_per_hour),
                'GateRootGiB': str(values.whole('root_gib')),
                'GateSpotMaxPrice': str(values.number('spot_max_usd_per_hour')),
                'GateArtifactDays': str(values.whole('artifact_days')), 'GateLogDays': str(values.whole('log_days')),
                'GateExpiryMinutes': str(values.whole('expiry_schedule_minutes')),
                'GateExpiryTimeout': str(values.whole('expiry_timeout_s')),
                'GateExpiryMemory': str(values.whole('expiry_memory_mib')),
            }
            book = ledger.CostLedger(self.directory)
            inventory = book.read_snapshot().inventories
            resources = tuple(resource for resource in inventory[-1].resources if resource.name != 'gate-shared') if inventory else ()
            book.record_inventory(ledger.ResourceInventory(int(self.clock()), resources + (
                ledger.PricedResource('gate-shared', money(str(values.number('shared_usd_per_month')))),), 'gate deployment',
                builder_removed=bool(inventory and inventory[-1].builder_removed)))
            changes: list[dict[str, object]] = [
                {'ParameterKey': ledger.text(row, 'ParameterKey'), 'UsePreviousValue': True}
                for row in ledger.rows(stack, 'Parameters') if ledger.text(row, 'ParameterKey') not in parameters]
            changes.extend({'ParameterKey': key, 'ParameterValue': value} for key, value in parameters.items())
            self.output.mkdir(parents=True, exist_ok=True)
            template = self.output / 'gate-template.json'
            write_bytes_atomic(template, TEMPLATE.read_bytes())
            with step('up', 'updating the stack', self.settings_path, hint='can take several minutes'):
                self._gate_change(api, stack_id, template, changes)
            outputs = self._stack(settings)
            manifest: dict[str, object] = {
                'deployment_id': ledger.text(saved, 'deployment_id'), 'revision': revision,
                'instance_types': values.words('instance_types'),
                'bucket': outputs['GateBucket'], 'artifact_prefix': 'artifacts/' + outputs['GateRoleId'] + ':',
                'regions': [{'region': settings.region, 'image': settings.ami_id, 'subnets': subnets,
                    'template': outputs['GateLaunchTemplate'], 'version': outputs['GateLaunchVersion'],
                    'security_group': outputs['GateSecurityGroup']}],
            }
            cloud_gate.save(self.directory / 'gate-deployment.json', manifest)
            regional = list(ledger.rows(manifest, 'regions'))
            for region in regions[1:]:
                regional.append(self.regional_gate_stack(api, settings, outputs, parameters, region))
                manifest['regions'] = regional
                cloud_gate.save(self.directory / 'gate-deployment.json', manifest)
            print('cloud-gate: deployed')
        return True

    def remove_builder(self, *, approve_destroy: bool = False) -> bool:
        if not approve_destroy:
            print(teardown.BUILDER_DRY_RUN)
            return True
        self._check_credentials()
        settings = read_deployment_settings(self.settings_path, self.directory)
        api = self._api(settings)
        removal = teardown.TargetRemoval(self.settings_path, self.directory, self.output, self.admin,
            api, self.execute, tailnet=box.tailnet_request, clock=self.clock, builder_only=True)
        with control_lock(self.directory / 'up'):
            with step('remove-builder', 'checking the deployment', self.settings_path):
                _ = removal.verify()
            removal.remember()
            with step('remove-builder', 'disabling builder routing', self.settings_path):
                _ = removal.routing()
            paths = box.builder_state_paths()
            with mac_test.locked(paths.lock), control_lock(self.directory):
                if box.running_claims(paths):
                    raise DeploymentRefused('AWS claims remain live; retry after their completion')
                with step('remove-builder', 'removing the builder from Tailscale', self.settings_path):
                    _ = removal.devices()
                stack_id = ledger.text(deployment_record(self.directory), 'stack_id')
                stack = ledger.rows(api.call('describe-stacks', '--stack-name', stack_id,
                                            service='cloudformation'), 'Stacks')[0]
                body = api.call('get-template', '--stack-name', stack_id, service='cloudformation')['TemplateBody']
                template = ledger.mapping(cast(object, json.loads(body)) if isinstance(body, str) else body)
                resources = ledger.mapping(template['Resources'])
                builder_roles = ('InstanceRole', 'InstanceProfile', 'SecurityGroup')
                needs_update = (self._stack(settings).get('ComputeEnabled') != 'false'
                                or any(ledger.mapping(resources[name]).get('Condition') != 'Compute' for name in builder_roles))
                for name in builder_roles:
                    ledger.mapping(resources[name])['Condition'] = 'Compute'
                self.output.mkdir(parents=True, exist_ok=True)
                write_text_atomic(self.output / 'template.json', json.dumps(template) + '\n')
                parameters = {ledger.text(row, 'ParameterKey'): str(row.get('ParameterValue', ''))
                              for row in ledger.rows(stack, 'Parameters')}
                if needs_update:
                    with step('remove-builder', 'updating the stack', self.settings_path, hint='can take several minutes'):
                        self._update_stack(settings, parameters, remove_builder=True)
                with step('remove-builder', 'deleting builder storage', self.settings_path):
                    _ = removal.data_volume()
                now = int(self.clock())
                book = ledger.CostLedger(self.directory)
                book.observe(ledger.InstanceObservation(now, 'terminated', 'confirmed builder removal'), confirms_transition=True)
                for request in book.pending_compute_requests():
                    book.resolve_compute_request(request, ledger.ComputeResourcesAbsent(now))
                inventories = book.read_snapshot().inventories
                if not inventories or not inventories[-1].builder_removed:
                    builder_costs = {key for key, _cost in read_settings(self.settings_path).fixed_components}
                    kept = tuple(resource for resource in inventories[-1].resources
                                 if resource.name not in builder_costs) if inventories else ()
                    book.record_inventory(ledger.ResourceInventory(now, kept, 'confirmed builder removal', builder_removed=True))
                saved = deployment_record(self.directory)
                saved.update(outputs=self._stack(settings), data_volume_id='')
                write_text_atomic(self.directory / 'deployment.json', json.dumps(saved, sort_keys=True) + '\n')
                with step('remove-builder', 'deleting local builder records', self.settings_path):
                    for name in ('up.json', 'box-tools.json', 'box-shell.json'):
                        (self.directory / name).unlink(missing_ok=True)
        print('remove-builder: done; gate resources and gate-deployment.json kept')
        return True

    def down(self, *, approve_destroy: bool = False) -> bool:
        if not approve_destroy:
            print(teardown.DRY_RUN)
            return True
        self._check_credentials()
        settings = read_deployment_settings(self.settings_path, self.directory)
        with step('down', 'removing local and cloud resources', self.settings_path):
            return teardown.TargetRemoval(self.settings_path, self.directory, self.output, self.admin,
                self._api(settings), self.execute, tailnet=box.tailnet_request, clock=self.clock).run()


def region_gate_template() -> dict[str, object]:
    """Reuse the home gate bodies without regional copies of global resources."""
    source = ledger.mapping(cast(object, json.loads(TEMPLATE.read_text())))
    names = ('GateSecurityGroup', 'GateLaunchTemplate', 'GateScalerPolicy', 'GateGpuTrialScalerPolicy', 'GateExpiryRole',
             'GateExpiryFunction', 'GateExpiryLog', 'GateExpirySchedule', 'GateExpiryPermission')
    resources = ledger.mapping(source['Resources'])
    def rewrite(value: object) -> object:
        if isinstance(value, str):
            return value.replace('cloud-claude-gate-${DeploymentId}', 'cloud-gate-${DeploymentId}-${AWS::Region}')
        if isinstance(value, list):
            return [rewrite(item) for item in cast(list[object], value)]
        if isinstance(value, dict):
            row = ledger.mapping(cast(object, value))
            if row.get('Fn::GetAtt') == ['GateProfile', 'Arn']:
                return {'Ref': 'HomeGateProfileArn'}
            if row.get('Fn::GetAtt') == ['GateRole', 'Arn']:
                return {'Ref': 'HomeGateRoleArn'}
            if row.get('Ref') == 'ScalerUser':
                return {'Ref': 'ScalerUserName'}
            return {key: rewrite(item) for key, item in row.items() if not (key == 'Condition' and item == 'Gates')}
        return value
    regional = {name: rewrite(resources[name]) for name in names}
    actions = {'ec2:RunInstances', 'ec2:CreateTags', 'ec2:TerminateInstances', 'iam:PassRole'}
    for name in ('GateScalerPolicy', 'GateGpuTrialScalerPolicy'):
        policy = ledger.mapping(ledger.mapping(ledger.mapping(regional[name])['Properties'])['PolicyDocument'])
        policy['Statement'] = [row for row in ledger.rows(policy, 'Statement') if set(cast(list[str], row['Action'])) & actions]
    parameters = ledger.mapping(source['Parameters'])
    needed = ('VpcId', 'DeploymentId', 'CostTagKey', 'CostTagValue', 'RootDeviceName', 'GateImage', 'GateSubnets',
              'GateInstanceTypes', 'GateGpuInstanceTypes', 'GateRootGiB', 'GateSpotMaxPrice', 'GateExpiryMinutes', 'GateExpiryTimeout',
              'GateExpiryMemory', 'GateLogDays')
    definitions = {key: parameters[key] for key in needed}
    definitions.update({key: {'Type': 'String'} for key in ('HomeGateProfileArn', 'HomeGateRoleArn', 'ScalerUserName')})
    outputs = {key: rewrite(value) for key, value in ledger.mapping(source['Outputs']).items()
               if key in ('GateLaunchTemplate', 'GateLaunchVersion', 'GateSecurityGroup')}
    return {'AWSTemplateFormatVersion': '2010-09-09', 'Parameters': definitions,
            'Conditions': {'GateEveryMinute': ledger.mapping(source['Conditions'])['GateEveryMinute']},
            'Resources': regional, 'Outputs': outputs}


def gate_subnets(api: AwsCli, vpc: str) -> list[dict[str, object]]:
    """Choose one public subnet in each available zone."""
    tables = ledger.rows(api.call('describe-route-tables', '--filters', 'Name=vpc-id,Values=' + vpc), 'RouteTables')
    main: dict[str, object] = {'Routes': []}
    assigned: dict[str, dict[str, object]] = {}
    for table in tables:
        for association in ledger.rows(table, 'Associations'):
            if association.get('Main') is True:
                main = table
            if association.get('SubnetId'):
                assigned[ledger.text(association, 'SubnetId')] = table
    zones: dict[str, dict[str, object]] = {}
    reply = api.call('describe-subnets', '--filters', 'Name=vpc-id,Values=' + vpc)
    for subnet in sorted(ledger.rows(reply, 'Subnets'), key=lambda row: ledger.text(row, 'SubnetId')):
        table = assigned.get(ledger.text(subnet, 'SubnetId'), main)
        public = any(route.get('DestinationCidrBlock') == '0.0.0.0/0' and route.get('State') == 'active'
                     and str(route.get('GatewayId', '')).startswith('igw-') for route in ledger.rows(table, 'Routes'))
        if public and subnet.get('State') == 'available':
            zone = ledger.text(subnet, 'AvailabilityZone')
            _ = zones.setdefault(zone, {'zone': zone, 'id': ledger.text(subnet, 'SubnetId')})
    if not zones:
        raise DeploymentRefused('The gate VPC has no available public subnet')
    return [zones[zone] for zone in sorted(zones)]


class DeploymentArguments(argparse.Namespace):
    def __init__(self) -> None:
        super().__init__()
        self.command: str = ''
        self.workload: str = 'rust'
        self.settings: Path = conf.path('aws_target')
        self.directory: Path = target_config.ssh_target_state_directory('aws')
        self.output: Path = Path.home() / '.local/state/aws-build/deployment'
        self.approve_resources: bool = False
        self.approve_destroy: bool = False
        self.trial_action: str | None = None
        self.trial_folder: Path = Path()


def main() -> int:
    os.environ['AWS_SHARED_CREDENTIALS_FILE'] = str(admin_secrets.credentials_file())
    if sys.argv[1:2] == ['gate-trial']:
        from scripts.aws_target import gate_trial
        settings_path = conf.path('aws_target')
        with step('gate-trial', 'running the trial', settings_path):
            return gate_trial.main(sys.argv[2:], settings_path, target_config.ssh_target_state_directory('aws'))
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument('command', choices=('gpu-size-trial', 'secrets', 'up', 'down', 'remove-builder', 'remove-images', 'prices', 'spot-table', 'quota-request', 'quota-status', 'prepare', 'hana-setup', 'apply'))
    _ = parser.add_argument('trial_action', nargs='?', choices=('run',))
    _ = parser.add_argument('--trial-folder', type=Path)
    _ = parser.add_argument('--workload', choices=('merge-gates', 'hana'), default='merge-gates')
    _ = parser.add_argument('--settings', type=Path)
    _ = parser.add_argument('--directory', type=Path)
    _ = parser.add_argument('--output', type=Path)
    _ = parser.add_argument('--approve-resources', action='store_true')
    _ = parser.add_argument('--approve-destroy', action='store_true')
    args = parser.parse_args(namespace=DeploymentArguments())
    deploy = Deployment(args.settings, args.directory, args.output)
    try:
        if args.command == 'gpu-size-trial':
            if args.trial_action != 'run' or args.trial_folder == Path():
                raise DeploymentRefused('Use gpu-size-trial run --trial-folder DIR --approve-resources')
            return 0 if deploy.gpu_size_trial(args.trial_folder, approve_resources=args.approve_resources) else 1
        if args.command == 'secrets':
            with step('secrets', 'saving encrypted credentials', args.settings):
                admin_secrets.configure(args.settings)
        elif args.command == 'up':
            return 0 if deploy.gate_up(approve_resources=args.approve_resources) else 1
        elif args.command == 'down':
            return 0 if deploy.down(approve_destroy=args.approve_destroy) else 1
        elif args.command == 'remove-builder':
            return 0 if deploy.remove_builder(approve_destroy=args.approve_destroy) else 1
        elif args.command == 'remove-images':
            return 0 if deploy.remove_images(approve_destroy=args.approve_destroy) else 1
        elif args.command == 'spot-table':
            deploy.spot_table()
        elif args.command == 'prices':
            deploy.prices()
        elif args.command == 'quota-request':
            return 0 if deploy.quota_request() else 1
        elif args.command == 'quota-status':
            return 0 if deploy.quota_status() else 1
        elif args.command == 'hana-setup':
            return 0 if deploy.hana_setup(approve_resources=args.approve_resources) else 1
        elif args.command == 'prepare':
            prepared = deploy.prepare_hana() if args.workload == 'hana' else deploy.prepare()
            print(json.dumps({'at': prepared.at, 'fingerprint': prepared.fingerprint, 'output': str(args.output)}))
        else:
            print(json.dumps(deploy.apply(approve_resources=args.approve_resources), sort_keys=True))
        return 0
    except (OSError, ValueError, AwsUnavailable, subprocess.SubprocessError) as error:
        print('aws_deploy: ' + deploy.admin.redact(str(error)), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
