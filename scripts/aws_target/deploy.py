#!/usr/bin/env python3
"""Prepare local evidence and deploy resources only through an explicit user command."""
from __future__ import annotations

import argparse
import base64
from collections.abc import Callable
from dataclasses import dataclass, fields, replace
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
from typing import cast, final

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.aws_target import ledger, remote
from scripts.aws_target.aws_target import AwsCli, AwsExecutor, AwsUnavailable, control_lock, execute_command
from scripts.aws_target.config import (
    DeploymentSettings, deployment_price, money, read_control_settings, read_deployment_settings, read_settings,
)
from scripts.lib import conf
from scripts.mac_test import offload
from scripts.lib.atomic_file import write_text_atomic

TEMPLATE = Path(__file__).resolve().parents[2] / 'docs/plans/builds-aws/cloudformation.json'


class DeploymentRefused(ValueError):
    """Current evidence does not permit a resource request."""


@dataclass(frozen=True)
class PreparedDeployment:
    at: int
    fingerprint: str
    parameters: dict[str, str]


def deployment_parameters(path: Path, settings: DeploymentSettings, bootstrap_path: Path) -> dict[str, str]:
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
        'NotificationEmail': settings.notification_email, 'RootDeviceName': settings.root_device_name,
        'RootVolumeGiB': str(settings.root_volume_gib), 'DataVolumeGiB': str(settings.data_volume_gib),
        'DataVolumeIops': str(settings.data_volume_iops), 'DataVolumeThroughput': str(settings.data_volume_throughput),
        'SpotMaxPrice': str(settings.spot_max_usd_per_hour), 'MonthlyLimitUsd': str(spending.monthly_limit_usd),
        'NoticeLowPercent': str(settings.notice_low), 'NoticeHighPercent': str(settings.notice_high),
        'StopPercent': str(settings.stop_percent), 'CostTagKey': spending.cost_explorer_tag_key,
        'CostTagValue': spending.cost_explorer_tag_value, 'Bootstrap': bootstrap_payload,
        'ExistingDataVolumeId': settings.existing_data_volume_id,
        'ScalerUserName': settings.scaler_user_name,
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
                 notify: Callable[[ledger.LimitEvent], bool] = ledger.send_limit_event) -> None:
        self.settings_path = settings_path
        self.directory = directory
        self.output = output
        self.execute = execute
        self.clock = clock
        self.notify = notify

    def _guard(self) -> ledger.SpendingAllowed:
        decision = ledger.spending_guard(self.settings_path, self.directory, 'deployment',
                                         clock=self.clock, notify=self.notify)
        if not isinstance(decision, ledger.SpendingAllowed):
            raise DeploymentRefused(decision.reason)
        if not ledger.CostLedger(self.directory).decision_is_current(decision):
            raise DeploymentRefused('the deployment guard has no durable confirmation')
        return decision

    def _api(self, settings: DeploymentSettings) -> AwsCli:
        return AwsCli(settings.profile, settings.region, settings.timeout_s, self.execute)

    def _observe(self, settings: DeploymentSettings, instance_id: str) -> ledger.InstanceObservation:
        evidence = self._api(settings).observe(instance_id, int(self.clock()))
        observation = ledger.InstanceObservation(evidence.observed_at, evidence.state, 'confirmed deployment instance state')
        ledger.CostLedger(self.directory).observe(observation)
        return observation

    def _refresh_observation(self, settings: DeploymentSettings) -> None:
        pending = ledger.CostLedger(self.directory).pending_compute_requests()
        if pending:
            _ = self._reconcile_compute_request(settings, pending[0])
            return
        path = self.directory / 'deployment.json'
        if path.exists():
            saved = remote.read_record(path)
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
            book.observe(ledger.InstanceObservation(now, 'terminated', 'confirmed absence of requested compute resources'))
            book.resolve_compute_request(request, ledger.ComputeResourcesAbsent(now))
            return {}
        if stack.get('ComputeEnabled') != 'true' or not stack.get('InstanceId') or not stack.get('DataVolumeId'):
            raise DeploymentRefused('resource identities are missing; reconcile the stack before retry')
        write_text_atomic(self.directory / 'deployment.json', json.dumps({
            'stack': request.stack, 'region': request.region, 'at': int(self.clock()), 'outputs': stack,
        }, sort_keys=True) + '\n')
        observation = self._observe(requested, stack['InstanceId'])
        previous_instances = {r.resolution.instance_id for r in book.read_snapshot().compute_requests
                              if isinstance(r.resolution, ledger.ComputeInstanceConfirmed)}
        if observation.state == 'terminated' and stack['InstanceId'] in previous_instances:
            raise DeploymentRefused('the previous instance does not confirm the requested compute resources')
        book.resolve_compute_request(request, ledger.ComputeInstanceConfirmed(observation.at, stack['InstanceId']))
        return stack

    def _load(self, *, refresh: bool = False) -> tuple[DeploymentSettings, dict[str, str]]:
        settings = read_deployment_settings(self.settings_path)
        if refresh:
            self._refresh_observation(settings)
        parameters = deployment_parameters(self.settings_path, settings, self.output / 'bootstrap.sh')
        self._before_request(settings, parameters)
        return settings, parameters

    def _before_request(self, settings: DeploymentSettings, parameters: dict[str, str]) -> None:
        _ = self._guard()
        self._confirm_preparation(settings, parameters)

    def prepare(self) -> PreparedDeployment:
        with control_lock(self.directory):
            settings = read_deployment_settings(self.settings_path)
            now = int(self.clock())
            price_at, price = deployment_price(self.settings_path, now)
            parameters = deployment_parameters(self.settings_path, settings, self.output / 'bootstrap.sh')
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
        status = ledger.text(stack, 'StackStatus')
        if status not in ('CREATE_COMPLETE', 'UPDATE_COMPLETE'):
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

    def _deploy(self, settings: DeploymentSettings, parameters: dict[str, str], *, compute: bool) -> None:
        self._before_request(settings, parameters)
        if compute:
            ledger.CostLedger(self.directory).record_compute_request(
                ledger.ComputeRequest(int(self.clock()), settings.stack_name, settings.region,
                                      money(parameters['SpotMaxPrice'])))
        _ = self._api(settings).call('deploy',
            '--stack-name', settings.stack_name, '--template-file', str(self.output / 'template.json'),
            '--capabilities', 'CAPABILITY_NAMED_IAM', '--no-fail-on-empty-changeset',
            '--parameter-overrides', *[f'{key}={value}' for key, value in parameters.items()],
            f'EnableCompute={str(compute).lower()}', service='cloudformation')

    def _confirm_preparation(self, settings: DeploymentSettings, parameters: dict[str, str]) -> None:
        current = read_deployment_settings(self.settings_path)
        current_parameters = deployment_parameters(self.settings_path, current, self.output / 'bootstrap.sh')
        preflight = remote.read_record(self.output / 'preflight.json')
        at = ledger.instant(preflight, 'at')
        template = TEMPLATE.read_bytes()
        expected = fingerprint(current, current_parameters, template)
        if (not 0 <= int(self.clock()) - at <= current.preflight_max_age_s
                or preflight.get('fingerprint') != expected
                or fingerprint(settings, parameters, template) != expected
                or (self.output / 'template.json').read_bytes() != template):
            raise DeploymentRefused('deployment preparation expired or its inputs changed; run prepare again')
        _ = deployment_price(self.settings_path, int(self.clock()))

    def _authorize(self, approve_resources: bool) -> None:
        if not approve_resources:
            raise DeploymentRefused('the user must pass --approve-resources at their terminal')
        credential_file = os.environ.get('AWS_SHARED_CREDENTIALS_FILE', '')
        if not credential_file or stat.S_IMODE(Path(credential_file).expanduser().stat().st_mode) != 0o600:
            raise DeploymentRefused('AWS_SHARED_CREDENTIALS_FILE must name a private mode 0600 file')

    def setup_start(self, *, approve_resources: bool = False) -> dict[str, str]:
        """Restart the deployed instance for user setup while admission stays disabled."""
        self._authorize(approve_resources)
        with control_lock(self.directory):
            settings, parameters = self._load(refresh=True)
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
            settings, parameters = self._load(refresh=True)
            stack = self._stack(settings)
            previous_path = self.directory / 'deployment.json'
            if previous_path.exists():
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
                        raise DeploymentRefused('use a new stack_name for replacement to preserve the retained Budget')
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
            self._deploy(settings, parameters, compute=True)
            pending = ledger.CostLedger(self.directory).pending_compute_requests()
            if len(pending) != 1:
                raise DeploymentRefused('the compute request has no durable accounting evidence')
            return self._reconcile_compute_request(settings, pending[0])


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
        if field.name == 'instance_id' and not value:
            continue
        if field.name in limits:
            lines.append(limits[field.name])
        lines.append(f'{field.name}={value}')
    return '\n'.join(lines) + '\n'


def bootstrap_configuration(path: Path, remote_settings: str) -> str:
    """Render the reviewed public bootstrap without creating resources."""
    settings = read_deployment_settings(path)
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
        'IDLE_STOP_SECONDS': f"{conf.number(path, 'idle_stop_minutes') * 60:g}",
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
        self.settings: Path = conf.path('aws_target')
        self.directory: Path = offload.ssh_target_state_directory('aws')
        self.output: Path = Path.home() / '.local/state/aws-build/deployment'
        self.approve_resources: bool = False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument('command', choices=('prepare', 'apply', 'setup-start', 'bootstrap'))
    _ = parser.add_argument('--settings', type=Path)
    _ = parser.add_argument('--directory', type=Path)
    _ = parser.add_argument('--output', type=Path)
    _ = parser.add_argument('--approve-resources', action='store_true')
    args = parser.parse_args(namespace=DeploymentArguments())
    deploy = Deployment(args.settings, args.directory, args.output)
    try:
        if args.command == 'bootstrap':
            remote_settings = remote_configuration(args.settings)
            text = bootstrap_configuration(args.settings, remote_settings)
            args.output.mkdir(parents=True, exist_ok=True)
            write_text_atomic(args.output / 'bootstrap.sh', text)
            write_text_atomic(args.output / 'remote.conf', remote_settings)
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
        print(f'aws_deploy: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
