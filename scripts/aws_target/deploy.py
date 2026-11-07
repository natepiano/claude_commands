#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Prepare local evidence and deploy resources only through an explicit user command."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import base64
from configparser import ConfigParser
from io import StringIO
from collections.abc import Callable
from dataclasses import dataclass, fields, replace
from datetime import UTC, datetime
import gzip
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
from typing import Literal, cast, final
from uuid import uuid4


from scripts.aws_target import admin_secrets, box, ledger, remote, teardown
from scripts.aws_target.aws_target import AwsCli, AwsExecutor, AwsUnavailable, control_lock, execute_command
from scripts.aws_target.config import (
    BudgetNotificationAddress, DeploymentSettings, deployment_price, deployment_price_path, money, read_budget_notification_address,
    deployment_record, deployment_outputs, read_control_settings, read_deployment_settings, read_settings,
)
from scripts.lib import conf
from scripts.build_targets import target_config
from scripts.lib.atomic_file import write_bytes_atomic, write_text_atomic

TEMPLATE = Path(__file__).resolve().parents[2] / 'docs/plans/builds-aws/cloudformation.json'


class DeploymentRefused(ValueError):
    """Current evidence does not permit a resource request."""


@dataclass(frozen=True)
class PreparedDeployment:
    at: int
    fingerprint: str
    parameters: dict[str, str]



@dataclass(frozen=True)
class UpStage:
    name: str
    check: Callable[[], bool]
    action: Callable[[], object]
    refresh: bool = False


@dataclass(frozen=True)
class StageResult:
    status: Literal['present', 'done', 'refused']
    started_at: int
    finished_at: int
    reason: str = ''

def deployment_parameters(path: Path, settings: DeploymentSettings, bootstrap_path: Path,
                          notification: BudgetNotificationAddress, deployment_id: str) -> dict[str, str]:
    spending = read_settings(path)
    bootstrap = bootstrap_path.read_text()
    if not bootstrap.startswith('#!'):
        raise DeploymentRefused('bootstrap.sh must contain the rendered public bootstrap script')
    if any(marker in bootstrap for marker in ('PRIVATE KEY-----', 'aws_secret_access_key', 'AWS_SECRET_ACCESS_KEY')):
        raise DeploymentRefused('bootstrap.sh must contain only public bootstrap settings')
    bootstrap_payload = base64.b64encode(gzip.compress(bootstrap.encode(), mtime=0)).decode()
    if len(bootstrap_payload) > 4096:
        raise DeploymentRefused('the compressed bootstrap exceeds the CloudFormation parameter size limit')
    return {
        'InstanceType': settings.instance_type, 'AmiId': settings.ami_id,
        'AvailabilityZone': settings.availability_zone, 'VpcId': settings.vpc_id, 'SubnetId': settings.subnet_id,
        'NotificationEmail': notification.value, 'RootDeviceName': settings.root_device_name,
        'RootVolumeGiB': str(settings.root_volume_gib), 'DataVolumeGiB': str(settings.data_volume_gib),
        'DataVolumeIops': str(settings.data_volume_iops), 'DataVolumeThroughput': str(settings.data_volume_throughput),
        'SpotMaxPrice': str(settings.spot_max_usd_per_hour), 'MonthlyLimitUsd': str(spending.monthly_limit_usd),
        'NoticeLowPercent': str(settings.notice_low), 'NoticeHighPercent': str(settings.notice_high),
        'StopPercent': str(settings.stop_percent), 'CostTagKey': spending.cost_explorer_tag_key,
        'CostTagValue': spending.cost_explorer_tag_value, 'Bootstrap': bootstrap_payload,
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
        parameters = deployment_parameters(self.settings_path, settings, self.output / 'bootstrap.sh', notification, deployment_id)
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
                         '--change-set-name', change_name, service='cloudformation')
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

    def price(self) -> dict[str, str | int]:
        """Save the current public Spot price without requesting resources."""
        self._check_credentials()
        path = deployment_price_path(self.settings_path)
        settings = read_deployment_settings(self.settings_path, self.directory)
        now = int(self.clock())
        reply = self._api(settings).call('describe-spot-price-history', '--instance-types', settings.instance_type,
                         '--availability-zone', settings.availability_zone,
                         '--product-descriptions', 'Linux/UNIX',
                         '--start-time', datetime.fromtimestamp(now, UTC).isoformat(), '--max-items', '1')
        prices = ledger.rows(reply, 'SpotPriceHistory')
        if len(prices) != 1:
            raise DeploymentRefused('describe-spot-price-history must return one current price')
        price = money(ledger.text(prices[0], 'SpotPrice'))
        if price <= 0:
            raise DeploymentRefused('describe-spot-price-history must return a positive price')
        now = int(self.clock())
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        write_bytes_atomic(path, (f'deployment_price_usd_per_hour={price}\n'
                                 + f'deployment_price_at={now}\n').encode(), mode=0o600)
        return {'deployment_price_usd_per_hour': str(price), 'deployment_price_at': now, 'file': str(path)}

    def _observe(self, settings: DeploymentSettings, instance_id: str, *,
                 confirms_transition: bool = False) -> ledger.InstanceObservation:
        evidence = self._api(settings).observe(instance_id, int(self.clock()))
        observation = ledger.InstanceObservation(evidence.observed_at, evidence.state, 'confirmed deployment instance state')
        ledger.CostLedger(self.directory).observe(observation, confirms_transition=confirms_transition)
        return observation

    def _refresh_observation(self, settings: DeploymentSettings) -> None:
        pending = ledger.CostLedger(self.directory).pending_compute_requests()
        if pending:
            for request in pending:
                _ = self._reconcile_compute_request(settings, request)
            return
        path = self.directory / 'deployment.json'
        if path.exists():
            saved = remote.read_record(path)
            if not ledger.mapping(saved.get('outputs', {})).get('InstanceId'):
                return
            if saved.get('region') != settings.region:
                raise DeploymentRefused('preserve the region of the saved deployment')
            outputs = ledger.mapping(saved.get('outputs'))
            try:
                _ = self._observe(settings, ledger.text(outputs, 'InstanceId'))
            except AwsUnavailable as error:
                if ('InvalidInstanceID.NotFound' not in str(error)
                        or settings.existing_data_volume_id != outputs.get('DataVolumeId')
                        or self._stack(replace(settings, stack_name=ledger.text(saved, 'stack')))):
                    raise
                ledger.CostLedger(self.directory).observe(ledger.InstanceObservation(
                    int(self.clock()), 'terminated', 'confirmed missing instance after stack deletion'), confirms_transition=True)

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

    def _load(self, notification: BudgetNotificationAddress, *, refresh: bool = False
              ) -> tuple[DeploymentSettings, dict[str, str]]:
        settings = read_deployment_settings(self.settings_path, self.directory)
        if refresh:
            self._refresh_observation(settings)
        parameters = self._parameters(settings, notification)
        self._before_request(settings, parameters)
        return settings, parameters

    def _before_request(self, settings: DeploymentSettings, parameters: dict[str, str]) -> None:
        _ = self._guard()
        self._confirm_preparation(settings, parameters)

    def prepare(self) -> PreparedDeployment:
        admin_secrets.refuse_plain_admin(self.settings_path)
        with control_lock(self.directory):
            settings = read_deployment_settings(self.settings_path, self.directory)
            now = int(self.clock())
            price_at, price = deployment_price(self.settings_path, now)
            notification = read_budget_notification_address(self.settings_path)
            parameters = self._parameters(settings, notification)
            spending = read_settings(self.settings_path)
            book = ledger.CostLedger(self.directory)
            state = book.read_snapshot() if book.path.exists() else ledger.LedgerEvidence()
            resources = tuple(ledger.PricedResource(key, value) for key, value in spending.fixed_components)
            if not state.inventories or state.inventories[-1].resources != resources:
                book.record_inventory(ledger.ResourceInventory(now, resources, 'validated deployment configuration'))
            book.record_price(ledger.PriceSample(price_at, price, 'public price recorded by the user'))
            self._refresh_observation(settings)
            decision = self._guard()
            template = TEMPLATE.read_bytes()
            prepared = PreparedDeployment(now, fingerprint(settings, parameters, template), parameters)
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

    def _update_stack(self, settings: DeploymentSettings, parameters: dict[str, str], *, compute: bool) -> None:
        api = self._api(settings)
        stack_id = ledger.text(deployment_record(self.directory), 'stack_id')
        live = self._stack(settings)
        values: list[dict[str, object]] = [
            {'ParameterKey': key, 'UsePreviousValue': True} if key in ('Bootstrap', 'AmiId') and live.get('InstanceId')
            else {'ParameterKey': key, 'ParameterValue': value}
            for key, value in {**parameters, 'EnableCompute': str(compute).lower()}.items()
        ]
        if (self.directory / 'gate-deployment.json').exists():
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
                             '--change-set-name', name, service='cloudformation')
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
                if resource.get('ResourceType') in protected and (resource.get('Action') == 'Remove'
                        or resource.get('Replacement') in ('True', 'Conditional')):
                    replacements.append(f"{ledger.text(resource, 'LogicalResourceId')} ({ledger.text(resource, 'ResourceType')})")
            if replacements:
                raise DeploymentRefused('up refuses replacement: ' + ', '.join(replacements)
                                        + '; run deploy.py down --approve-destroy, then deploy.py up --approve-resources')
            self._before_request(settings, parameters)
            if compute:
                self._record_compute_request(settings, parameters)
            _ = api.call('execute-change-set', '--stack-name', stack_id, '--change-set-name', name, service='cloudformation')
            executed = True
            _ = api.call('wait', 'stack-update-complete', '--stack-name', stack_id, service='cloudformation')
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
                raise DeploymentRefused((str(error) + leftover).replace(parameters['NotificationEmail'], '<notification address>')) from None
            failure = self._stack_failure(api, settings, stack_id, started, error, change, parameters['NotificationEmail'])
            raise AwsUnavailable(str(failure) + leftover) from None

    def _record_compute_request(self, settings: DeploymentSettings, parameters: dict[str, str]) -> None:
        book = ledger.CostLedger(self.directory)
        for request in book.pending_compute_requests():
            _ = self._reconcile_compute_request(settings, request)
        book.record_compute_request(ledger.ComputeRequest(int(self.clock()), settings.stack_name, settings.region,
                                                         money(parameters['SpotMaxPrice'])))

    def _deploy(self, settings: DeploymentSettings, parameters: dict[str, str], *, compute: bool) -> None:
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
            self._update_stack(settings, parameters, compute=compute)
        else:
            if compute:
                self._record_compute_request(settings, parameters)
            api = self._api(settings)
            started = self.clock()
            try:
                _ = api.call('deploy',
                    '--stack-name', ledger.text(deployment_record(self.directory), 'stack_id'),
                    '--template-file', str(self.output / 'template.json'),
                    '--capabilities', 'CAPABILITY_NAMED_IAM', '--no-fail-on-empty-changeset',
                    '--parameter-overrides', *[f'{key}={value}' for key, value in parameters.items()],
                    f'EnableCompute={str(compute).lower()}', service='cloudformation')
            except (AwsUnavailable, subprocess.SubprocessError) as error:
                raise self._stack_failure(api, settings, ledger.text(deployment_record(self.directory), 'stack_id'),
                                          started, error, {}, parameters['NotificationEmail']) from None
        self._save_scaler_identity(settings)
        outputs = self._stack(settings)
        saved = deployment_record(self.directory)
        if outputs.get('DataVolumeId'):
            saved['data_volume_id'] = outputs['DataVolumeId']
        if outputs.get('BudgetName'):
            saved['budget_name'] = outputs['BudgetName']
        write_text_atomic(self.directory / 'deployment.json', json.dumps(saved, sort_keys=True) + '\n')

    def _confirm_preparation(self, settings: DeploymentSettings, parameters: dict[str, str], *, renew: bool = False) -> None:
        current = read_deployment_settings(self.settings_path, self.directory)
        notification = BudgetNotificationAddress(parameters['NotificationEmail'])
        current_parameters = self._parameters(current, notification)
        preflight = remote.read_record(self.output / 'preflight.json')
        at = ledger.instant(preflight, 'at')
        template = TEMPLATE.read_bytes()
        expected = fingerprint(current, current_parameters, template)
        if ((not renew and not 0 <= int(self.clock()) - at <= current.preflight_max_age_s)
                or preflight.get('fingerprint') != expected
                or fingerprint(settings, parameters, template) != expected
                or (self.output / 'template.json').read_bytes() != template):
            raise DeploymentRefused('deployment preparation expired or its inputs changed; run prepare again')
        try:
            _ = deployment_price(self.settings_path, int(self.clock()))
        except ValueError:
            if not renew:
                raise
            _ = self.price()
            price_at, price = deployment_price(self.settings_path, int(self.clock()))
            ledger.CostLedger(self.directory).record_price(ledger.PriceSample(price_at, price, 'public deployment price after foundation'))
        if renew:
            preflight['at'] = int(self.clock())
            write_text_atomic(self.output / 'preflight.json', json.dumps(preflight, sort_keys=True) + '\n')

    def _authorize(self, approve_resources: bool) -> None:
        if not approve_resources:
            raise DeploymentRefused('the user must pass --approve-resources at their terminal')
        self._check_credentials()

    def _check_credentials(self) -> None:
        admin_secrets.refuse_plain_admin(self.settings_path)
        credential_file = os.environ.get('AWS_SHARED_CREDENTIALS_FILE', '')
        if not credential_file or stat.S_IMODE(Path(credential_file).expanduser().stat().st_mode) != 0o600:
            raise DeploymentRefused('AWS_SHARED_CREDENTIALS_FILE must name a private mode 0600 file')

    def setup_start(self, *, approve_resources: bool = False) -> dict[str, str]:
        """Restart the deployed instance for user setup while admission stays disabled."""
        self._authorize(approve_resources)
        with control_lock(self.directory):
            prepared = cast(object, json.loads((self.output / 'parameters.json').read_text()))
            notifications = [ledger.text(row, 'ParameterValue') for row in ledger.rows({'parameters': prepared}, 'parameters')
                             if row.get('ParameterKey') == 'NotificationEmail']
            if len(notifications) != 1:
                raise DeploymentRefused('setup requires one prepared NotificationEmail parameter; run prepare again')
            settings, parameters = self._load(BudgetNotificationAddress(notifications[0]), refresh=True)
            previous = remote.read_record(self.directory / 'deployment.json')
            stack = self._stack(settings)
            saved_outputs = ledger.mapping(previous.get('outputs'))
            instance = stack.get('InstanceId', '')
            if (not instance or previous.get('stack') != settings.stack_name
                    or previous.get('region') != settings.region
                    or saved_outputs.get('InstanceId') != instance
                    or saved_outputs.get('DataVolumeId') != stack.get('DataVolumeId')):
                raise DeploymentRefused('setup requires the confirmed deployed instance and data volume')
            state = self._observe(settings, instance).state
            if state in ('pending', 'running'):
                return {'InstanceId': instance, 'State': state}
            if state != 'stopped':
                raise DeploymentRefused(f'setup cannot start an instance in state {state}')
            self._before_request(settings, parameters)
            reply = self._api(settings).call('start-instances', '--instance-ids', instance)
            starts = ledger.rows(reply, 'StartingInstances')
            if len(starts) != 1 or starts[0].get('InstanceId') != instance:
                raise DeploymentRefused('the start response is uncertain; inspect instance state before retry')
            current = ledger.text(ledger.mapping(starts[0].get('CurrentState')), 'Name')
            if current not in ('pending', 'running'):
                raise DeploymentRefused('the start response does not confirm pending or running state')
            ledger.CostLedger(self.directory).observe(ledger.InstanceObservation(
                int(self.clock()), current, 'confirmed setup start response'), confirms_transition=True)
            return {'InstanceId': instance, 'State': current}

    def apply(self, *, approve_resources: bool = False) -> dict[str, str]:
        self._authorize(approve_resources)
        with control_lock(self.directory):
            settings, parameters = self._load(read_budget_notification_address(self.settings_path), refresh=True)
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
                self._deploy(settings, parameters, compute=False)
                stack = self._stack(settings)
                if not stack.get('BudgetName') or stack.get('ComputeEnabled') != 'false':
                    raise DeploymentRefused('the Budget foundation has no confirmed evidence')
                self._confirm_preparation(settings, parameters, renew=True)
            self._deploy(settings, parameters, compute=True)
            pending = ledger.CostLedger(self.directory).pending_compute_requests()
            outputs = self._stack(settings)
            for request in pending:
                outputs = self._reconcile_compute_request(settings, request)
            saved = deployment_record(self.directory)
            saved['fingerprint'] = fingerprint(settings, parameters, TEMPLATE.read_bytes())
            write_text_atomic(self.directory / 'deployment.json', json.dumps(saved, sort_keys=True) + '\n')
            return outputs


    def bootstrap(self) -> None:
        remote_settings = remote_configuration(self.settings_path)
        text = bootstrap_configuration(self.settings_path, remote_settings, self.directory)
        self.output.mkdir(parents=True, exist_ok=True)
        write_text_atomic(self.output / 'bootstrap.sh', text)
        write_text_atomic(self.output / 'remote.conf', remote_settings)

    def _bootstrap_present(self) -> bool:
        remote_settings = remote_configuration(self.settings_path)
        text = bootstrap_configuration(self.settings_path, remote_settings, self.directory)
        try:
            return ((self.output / 'bootstrap.sh').read_text() == text
                    and (self.output / 'remote.conf').read_text() == remote_settings)
        except FileNotFoundError:
            return False

    def _price_present(self) -> bool:
        try:
            _ = deployment_price(self.settings_path, int(self.clock()))
            return True
        except ValueError:
            return False

    def _stacks_present(self) -> bool:
        settings = read_deployment_settings(self.settings_path, self.directory)
        self._migrate_legacy(settings)
        stack = self._stack(settings)
        saved = deployment_record(self.directory)
        if saved.get('account_id'):
            account = ledger.text(self._api(settings).call('get-caller-identity', service='sts'), 'Account')
            if account != saved['account_id']:
                raise DeploymentRefused('the caller account differs from the deployment record')
        outputs = deployment_outputs(self.directory)
        if not stack or stack.get('ComputeEnabled') != 'true' or not outputs:
            return False
        if (saved.get('stack') != settings.stack_name or saved.get('region') != settings.region
                or any(not stack.get(key) or stack.get(key) != outputs.get(key)
                       for key in ('InstanceId', 'DataVolumeId', 'BudgetName'))):
            return False
        if ledger.CostLedger(self.directory).pending_compute_requests():
            return False
        parameters = self._parameters(settings, read_budget_notification_address(self.settings_path))
        return saved.get('fingerprint') == fingerprint(settings, parameters, TEMPLATE.read_bytes())

    def _apply_stacks(self) -> None:
        _ = self.prepare()
        _ = self.apply(approve_resources=True)

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

    def _scaler_stage_present(self) -> bool:
        if not self._scaler_present():
            return False
        path = self.directory / 'up.json'
        previous = remote.read_record(path) if path.exists() else {}
        result = previous.get('scaler-key', {})
        return not (self.directory / 'scaler-key.json').exists() and ledger.mapping(result).get('status') != 'refused'

    def _scaler_key(self) -> None:
        settings = read_deployment_settings(self.settings_path, self.directory)
        api = self._api(settings)
        credential_file = Path(os.environ['AWS_SHARED_CREDENTIALS_FILE']).expanduser()
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

    def _box_stages(self) -> tuple[UpStage, ...]:
        setup = box.BoxSetup(self.settings_path, self.directory, self._start_box, execute=self.execute, admin=self.admin)
        return (
            UpStage('tailscale-policy', setup.policy_present, setup.policy),
            UpStage('box-join', setup.join_present, setup.join),
            UpStage('box-configuration', setup.configuration_present, setup.configure),
            UpStage('checks', setup.checks_present, setup.checks),
            UpStage('image', setup.image_present, setup.image),
            UpStage('idle-stop', setup.idle_stop_present, setup.idle_stop),
            UpStage('activation', setup.activation_present, setup.activate),
            UpStage('routing-proof', setup.routing_proof_present, setup.routing_proof),
        )

    def up(self, *, approve_resources: bool = False) -> bool:
        """Check each deployment stage and stop at the first refusal."""
        self._authorize(approve_resources)
        stages = (
            UpStage('price', self._price_present, self.price, refresh=True),
            UpStage('bootstrap', self._bootstrap_present, self.bootstrap),
            UpStage('stacks', self._stacks_present, self._apply_stacks),
            UpStage('scaler-key', self._scaler_stage_present, self._scaler_key),
            *self._box_stages(),
        )
        with control_lock(self.directory / 'up'):
            if (self.directory / 'down.json').exists():
                raise DeploymentRefused('target removal is incomplete; run deploy.py down --approve-destroy first')
            path = self.directory / 'up.json'
            results = remote.read_record(path) if path.exists() else {}
            started = int(self.clock())
            try:
                status = self.execute(['tailscale', 'status', '--json'], capture_output=True, text=True,
                                      timeout=conf.read(self.settings_path).whole('box_setup_timeout_s'), check=False)
                if status.returncode:
                    raise box.BoxRefused(f'tailscale status failed (exit {status.returncode})')
                _ = box.policy_device_bindings(box.policy_source(), box.json_record(status.stdout))
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                result = StageResult('refused', started, int(self.clock()), self.admin.redact(str(error)))
                results['up'] = {field.name: cast(object, getattr(result, field.name)) for field in fields(result)}
                write_text_atomic(path, json.dumps(results, sort_keys=True) + '\n')
                print('up: refused: ' + result.reason)
                return False
            for stage in stages:
                started = int(self.clock())
                try:
                    present = stage.check()
                    if not present or stage.refresh:
                        _ = stage.action()
                    result = StageResult('present' if present else 'done', started, int(self.clock()))
                except (OSError, ValueError, AwsUnavailable, subprocess.SubprocessError) as error:
                    reason = 'scaler credential stage failed' if stage.name == 'scaler-key' else self.admin.redact(str(error))
                    result = StageResult('refused', started, int(self.clock()), reason)
                results = remote.read_record(path) if path.exists() else results
                _ = results.pop('up', None)
                previous = ledger.mapping(results.get(stage.name, {}))
                results[stage.name] = {**previous, **{field.name: cast(object, getattr(result, field.name)) for field in fields(result)}}
                write_text_atomic(path, json.dumps(results, sort_keys=True) + '\n')
                print(f'{stage.name}: {result.status}' + (f': {result.reason}' if result.reason else ''))
                if result.status == 'refused':
                    return False
        return True

    def gate_up(self, *, approve_resources: bool = False) -> bool:
        """Add the gate resources and preserve every Rust parameter."""
        from scripts.aws_target import cloud_gate, gate_trial
        self._authorize(approve_resources)
        with control_lock(self.directory / 'gate-up'):
            settings = read_deployment_settings(self.settings_path, self.directory)
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
                'GateRootGiB': str(values.whole('root_gib')),
                'GateSpotMaxPrice': str(values.number('spot_max_usd_per_hour')),
                'GateArtifactDays': str(values.whole('artifact_days')), 'GateLogDays': str(values.whole('log_days')),
                'GateExpiryMinutes': str(values.whole('expiry_schedule_minutes')),
                'GateExpiryTimeout': str(values.number('expiry_timeout_s')),
                'GateExpiryMemory': str(values.whole('expiry_memory_mib')),
            }
            book = ledger.CostLedger(self.directory)
            inventory = book.read_snapshot().inventories
            resources = tuple(resource for resource in inventory[-1].resources if resource.name != 'gate-shared') if inventory else ()
            book.record_inventory(ledger.ResourceInventory(int(self.clock()), resources + (
                ledger.PricedResource('gate-shared', money(str(values.number('shared_usd_per_month')))),), 'gate deployment'))
            changes: list[dict[str, object]] = [
                {'ParameterKey': ledger.text(row, 'ParameterKey'), 'UsePreviousValue': True}
                for row in ledger.rows(stack, 'Parameters') if ledger.text(row, 'ParameterKey') not in parameters]
            changes.extend({'ParameterKey': key, 'ParameterValue': value} for key, value in parameters.items())
            self.output.mkdir(parents=True, exist_ok=True)
            template = self.output / 'gate-template.json'
            write_bytes_atomic(template, TEMPLATE.read_bytes())
            name = 'gate-' + uuid4().hex
            executed = False
            try:
                _ = api.call('create-change-set', '--stack-name', stack_id, '--change-set-name', name,
                             '--change-set-type', 'UPDATE', '--template-body', 'file://' + str(template),
                             '--capabilities', 'CAPABILITY_NAMED_IAM', '--parameters', json.dumps(changes),
                             service='cloudformation')
                try:
                    _ = api.call('wait', 'change-set-create-complete', '--stack-name', stack_id,
                                 '--change-set-name', name, service='cloudformation')
                except AwsUnavailable:
                    pass
                change = api.call('describe-change-set', '--stack-name', stack_id,
                                  '--change-set-name', name, service='cloudformation')
                reason = str(change.get('StatusReason', '')).lower()
                empty = change.get('Status') == 'FAILED' and any(text in reason for text in (
                    "didn't contain changes", 'no updates are to be performed', 'no changes'))
                if not empty:
                    if change.get('Status') != 'CREATE_COMPLETE':
                        raise DeploymentRefused('The gate change set is incomplete')
                    for item in ledger.rows(change, 'Changes'):
                        resource = ledger.mapping(item['ResourceChange'])
                        if not ledger.text(resource, 'LogicalResourceId').startswith('Gate'):
                            raise DeploymentRefused('The gate change set changes a Rust resource')
                    _ = api.call('execute-change-set', '--stack-name', stack_id, '--change-set-name', name,
                                 service='cloudformation')
                    executed = True
                    _ = api.call('wait', 'stack-update-complete', '--stack-name', stack_id, service='cloudformation')
            finally:
                if not executed:
                    _ = self._discard_change_set(api, stack_id, name)
            outputs = self._stack(settings)
            manifest: dict[str, object] = {
                'deployment_id': ledger.text(saved, 'deployment_id'), 'region': settings.region,
                'image': settings.ami_id, 'revision': revision, 'subnets': subnets,
                'bucket': outputs['GateBucket'], 'template': outputs['GateLaunchTemplate'],
                'version': outputs['GateLaunchVersion'], 'security_group': outputs['GateSecurityGroup'],
                'artifact_prefix': 'artifacts/' + outputs['GateRoleId'] + ':',
            }
            cloud_gate.save(self.directory / 'gate-deployment.json', manifest)
            print('cloud-gate: deployed')
        return True

    def down(self, *, approve_destroy: bool = False) -> bool:
        if not approve_destroy:
            print(teardown.DRY_RUN)
            return True
        self._check_credentials()
        settings = read_deployment_settings(self.settings_path, self.directory)
        return teardown.TargetRemoval(self.settings_path, self.directory, self.output, self.admin,
                                      self._api(settings), self.execute, tailnet=box.tailnet_request, clock=self.clock).run()

    def _start_box(self) -> object:
        settings = read_deployment_settings(self.settings_path, self.directory)
        outputs = deployment_outputs(self.directory)
        if not outputs.get('InstanceId'):
            raise DeploymentRefused('deployment.json has no InstanceId')
        instance = outputs['InstanceId']
        state = self._api(settings).observe(instance, int(self.clock())).state
        if state in ('running', 'pending'):
            return state
        while True:
            _ = self.price()
            _ = self.prepare()
            try:
                return self.setup_start(approve_resources=True)
            except AwsUnavailable as error:
                if 'InsufficientInstanceCapacity' not in str(error):
                    raise
                delay = read_control_settings(self.settings_path).capacity_backoff_s
                print(f'AWS has no Spot capacity for the box now; next try in {delay:g} seconds.')
                self.pause(delay)


def gate_subnets(api: AwsCli, vpc: str) -> list[dict[str, object]]:
    """Choose one public subnet in each available zone."""
    tables = ledger.rows(api.call('describe-route-tables', '--filters', 'Name=vpc-id,Values=' + vpc), 'RouteTables')
    main: dict[str, object] = {}
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


def remote_configuration(path: Path) -> str:
    """Build separate public lifecycle settings without granting execution permission."""
    settings = read_control_settings(path)
    source = path.read_text().splitlines()
    limits: dict[str, str] = {}
    for index, line in enumerate(source):
        key, separator, _ = line.partition('=')
        if separator and index and source[index - 1].startswith(('# range:', '# whole:', '# one of:')):
            limits[key.strip()] = source[index - 1]
    lines = ['# one of: on, off', 'offload=on', 'ci_repo=']
    for field in fields(settings):
        if field.name == 'enabled':
            continue
        value = cast(object, getattr(settings, field.name))
        if field.name == 'instance_id':
            continue
        if field.name in limits:
            lines.append(limits[field.name])
        lines.append(f'{field.name}={value}')
    return '\n'.join(lines) + '\n'


def bootstrap_configuration(path: Path, remote_settings: str, directory: Path | None = None) -> str:
    """Render the reviewed public bootstrap without creating resources."""
    settings = read_deployment_settings(path, directory)
    def nix_string(value: str) -> str:
        return json.dumps(value).replace('${', '\\${')
    def module_text(path: Path) -> str:
        return '\n'.join(line for line in path.read_text().splitlines() if not line.lstrip().startswith('#'))
    base_path = settings.bootstrap_template_path.with_name('aws-build-base.nix')
    tailscale = module_text(base_path.parent.parent / 'modules/linux/tailscale.nix')
    base = module_text(base_path).replace('../modules/linux/tailscale.nix', '(' + tailscale + ')')
    text = settings.bootstrap_template_path.read_text()
    inputs = {
        'INPUT': base,
        'IDLE_STOP_SECONDS': f"{conf.read(path).number('idle_stop_minutes') * 60:g}",
        'DATA_VOLUME_WAIT_SECONDS': str(settings.timeout_s),
        'SCRIPTS_SOURCE_URL_JSON': nix_string(settings.scripts_source_url),
        'SCRIPTS_SOURCE_SHA256_JSON': nix_string(settings.scripts_source_sha256),
        'SCRIPTS_SOURCE_COMMIT_JSON': nix_string(settings.scripts_source_commit),
        'REMOTE_SETTINGS_JSON': nix_string(remote_settings),
        'EXISTING_DATA_VOLUME_JSON': nix_string(settings.existing_data_volume_id),
    }
    for key, value in inputs.items():
        marker = f'@{key}@'
        if marker not in text:
            raise DeploymentRefused(f'the bootstrap template has no {key} input')
        text = text.replace(marker, value)
    if re.search(r'@[A-Z_]+@', text):
        raise DeploymentRefused('the bootstrap template has unknown inputs')
    return text


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


def main() -> int:
    if sys.argv[1:2] == ['gate-trial']:
        from scripts.aws_target import gate_trial
        return gate_trial.main(sys.argv[2:], conf.path('aws_target'), target_config.ssh_target_state_directory('aws'))
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument('command', choices=('secrets', 'up', 'down', 'price', 'prepare', 'apply', 'setup-start', 'bootstrap'))
    _ = parser.add_argument('--workload', choices=('rust', 'merge-gates'), default='rust')
    _ = parser.add_argument('--settings', type=Path)
    _ = parser.add_argument('--directory', type=Path)
    _ = parser.add_argument('--output', type=Path)
    _ = parser.add_argument('--approve-resources', action='store_true')
    _ = parser.add_argument('--approve-destroy', action='store_true')
    args = parser.parse_args(namespace=DeploymentArguments())
    deploy = Deployment(args.settings, args.directory, args.output)
    try:
        if args.command == 'secrets':
            admin_secrets.configure(args.settings)
        elif args.command == 'up':
            action = deploy.gate_up if args.workload == 'merge-gates' else deploy.up
            return 0 if action(approve_resources=args.approve_resources) else 1
        elif args.command == 'down':
            return 0 if deploy.down(approve_destroy=args.approve_destroy) else 1
        elif args.command == 'price':
            print(json.dumps(deploy.price(), sort_keys=True))
        elif args.command == 'bootstrap':
            deploy.bootstrap()
            print(str(args.output / 'bootstrap.sh'))
        elif args.command == 'prepare':
            prepared = deploy.prepare()
            print(json.dumps({'at': prepared.at, 'fingerprint': prepared.fingerprint, 'output': str(args.output)}))
        elif args.command == 'setup-start':
            print(json.dumps(deploy.setup_start(approve_resources=args.approve_resources), sort_keys=True))
        else:
            print(json.dumps(deploy.apply(approve_resources=args.approve_resources), sort_keys=True))
        return 0
    except (OSError, ValueError, AwsUnavailable, subprocess.SubprocessError) as error:
        print('aws_deploy: ' + deploy.admin.redact(str(error)), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
