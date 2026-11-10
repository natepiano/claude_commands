"""Remove the target while retaining its spending evidence and encrypted credentials."""
from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from io import StringIO
import json
import shutil
from pathlib import Path
import subprocess
import time
from typing import cast, final

from scripts.aws_target import admin_secrets, box, ledger, remote
from scripts.aws_target.aws_target import AwsCli, AwsExecutor, AwsUnavailable, control_lock
from scripts.aws_target.config import deployment_record
from scripts.lib import conf
from scripts.lib.atomic_file import write_bytes_atomic, write_text_atomic
from scripts.mac_test import mac_test


DRY_RUN = '''down would disable AWS routing and aws-target.service.
down refuses while an AWS job still runs. Kept claims do not delay down.
down would delete the aws-build Tailscale device and keep tag:build-aws policy rules.
down would delete all scaler access keys and remove only the scaler credential profile.
down would remove compute resources, then the foundation stack and retained Budget.
down would delete the data volume and the gate image with its snapshot. The /home build cache is lost.
down would remove deployment.json, up.json, setup proofs, and the prepared bundle.
down keeps the spend ledger, charge history, disabled routing.json, admin.gpg, and the box image.
Run deploy.py down --approve-destroy to remove the target.'''

BUILDER_DRY_RUN = '''remove-builder would disable builder routing and aws-target.service.
remove-builder refuses while an AWS job still runs.
remove-builder would delete the builder Tailscale device and builder compute resources.
remove-builder would delete the retained data volume. The /home build cache is lost.
remove-builder keeps the stack, Budget, scaler credentials, gate resources, and gate-deployment.json.
remove-builder keeps admin.gpg, and the box image.
remove-builder resolves builder accounting and keeps the spending history.
Run deploy.py remove-builder --approve-destroy to remove only the builder.'''


@dataclass(frozen=True)
class RemovalIdentity:
    stack: str
    region: str
    account_id: str = ''
    stack_id: str = ''
    deployment_id: str = ''
    instance: str = ''
    volume: str = ''
    budget: str = ''


@dataclass(frozen=True)
class SavedNode:
    node_id: str


@dataclass(frozen=True)
class NeverJoined:
    pass


@final
class TargetRemoval:
    def __init__(self, settings: Path, directory: Path, output: Path, admin: admin_secrets.AdminSecretSession,
                 api: AwsCli, execute: AwsExecutor, *, tailnet: box.TailnetTransport = box.tailnet_request,
                 pause: Callable[[float], object] = time.sleep, clock: Callable[[], float] = time.time,
                 builder_only: bool = False) -> None:
        self.settings = settings
        self.builder_only = builder_only
        self.directory = directory
        self.output = output
        self.admin = admin
        self.api = api
        self.execute = execute
        self.tailnet = box.Tailnet(settings, admin, tailnet)
        self.pause = pause
        self.clock = clock
        self.identity = RemovalIdentity(conf.read(settings).text('stack_name'), conf.read(settings).text('region'))
        self.saved: dict[str, object] = {}

    def _wait(self, check: Callable[[], bool], reason: str) -> None:
        deadline = time.monotonic() + conf.read(self.settings).whole('box_setup_timeout_s')
        while not check():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError(reason)
            _ = self.pause(min(conf.read(self.settings).number('box_check_delay_s'), remaining))

    def _local(self, arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return self.execute(arguments, capture_output=True, text=True,
                            timeout=conf.read(self.settings).whole('deployment_timeout_s'), check=False)

    def routing(self) -> bool:
        changed = box.close_routing(self.settings)
        paths = mac_test.state_paths('aws')
        enabled = self._local(['systemctl', '--user', 'is-enabled', 'aws-target.service'])
        active = self._local(['systemctl', '--user', 'is-active', 'aws-target.service'])
        if enabled.stdout.strip() not in ('disabled', 'not-found') or active.stdout.strip() not in ('inactive', 'unknown'):
            result = self._local(['systemctl', '--user', 'disable', '--now', 'aws-target.service'])
            if result.returncode:
                raise ValueError('could not disable aws-target.service; routing remains disabled')
            changed = True
        with mac_test.locked(paths.lock):
            if box.running_claims(paths):
                raise ValueError('AWS claims remain live; routing is disabled; retry after their completion')
        return changed

    def _device_identity(self) -> SavedNode | NeverJoined:
        node = str(self.saved.get('tailscale_node_id', ''))
        if not node:
            path = self.directory / 'up.json'
            stages = remote.read_record(path) if path.exists() else {}
            joined = ledger.mapping(stages.get('box-join', {})).get('status') in ('done', 'present')
            devices = ledger.rows(self.tailnet.call('GET', 'devices').value, 'devices')
            matches = [device for device in devices if device.get('hostname') == 'aws-build'
                       and 'tag:build-aws' in box.public_strings(device.get('tags', []))]
            if not joined and not matches:
                return NeverJoined()
            raise ValueError('missing saved Tailscale node id; run deploy.py up once to record it')
        if self.saved.get('tailscale_instance_id') != self.identity.instance:
            raise ValueError('the saved Tailscale instance differs; run deploy.py up once to record it')
        return SavedNode(node)

    def devices(self) -> bool | NeverJoined:
        identity = self._device_identity()
        if isinstance(identity, NeverJoined):
            self.remember()
            return identity
        devices = ledger.rows(self.tailnet.call('GET', 'devices').value, 'devices')
        matches = [device for device in devices if device.get('nodeId', device.get('id')) == identity.node_id]
        if len(matches) > 1:
            raise ValueError('multiple devices have the saved Tailscale node id')
        if not matches:
            self.remember()
            return False
        device = matches[0]
        if 'tag:build-aws' not in box.public_strings(device.get('tags', [])):
            raise ValueError('the saved Tailscale device lacks tag:build-aws')
        self.remember()
        self.tailnet.delete_device(ledger.text(device, 'id'))
        return True

    def _scaler_user(self) -> str:
        identity = ledger.mapping(self.saved.get('scaler_user', {}))
        if not identity and self._stack().get('StackStatus') == 'REVIEW_IN_PROGRESS':
            return ''
        if not identity:
            raise ValueError('missing saved scaler identity; run deploy.py up once to record it')
        name = ledger.text(identity, 'name')
        if self._stack():
            resource = ledger.mapping(self.api.call('describe-stack-resource', '--stack-name', self.identity.stack_id,
                                                   '--logical-resource-id', 'ScalerUser', service='cloudformation').get('StackResourceDetail'))
            if resource.get('PhysicalResourceId') != name:
                raise ValueError('the stack scaler resource differs from its saved identity')
        try:
            user = ledger.mapping(self.api.call('get-user', '--user-name', name, service='iam').get('User'))
        except AwsUnavailable as error:
            if 'NoSuchEntity' in str(error) and not self._stack():
                return ''
            raise
        if user.get('Arn') != identity.get('arn') or user.get('UserId') != identity.get('user_id'):
            raise ValueError('the scaler ARN or UserId differs from its saved identity')
        return name

    def scaler(self) -> bool:
        user = self._scaler_user()
        reply: dict[str, object]
        changed = False
        try:
            reply = self.api.call('list-access-keys', '--user-name', user, service='iam') if user else {'AccessKeyMetadata': []}
        except AwsUnavailable as error:
            if 'NoSuchEntity' not in str(error):
                raise
            reply = {'AccessKeyMetadata': []}
        for key in ledger.rows(reply, 'AccessKeyMetadata'):
            _ = self.api.call('delete-access-key', '--user-name', user,
                              '--access-key-id', ledger.text(key, 'AccessKeyId'), service='iam')
            changed = True
        path = admin_secrets.credentials_file()
        profiles = admin_secrets.read_profiles(path)
        if profiles.remove_section(conf.read(self.settings).text('credentials_profile')):
            content = StringIO()
            profiles.write(content)
            write_bytes_atomic(path, content.getvalue().encode(), mode=0o600)
            changed = True
        return changed

    def _stack(self) -> dict[str, object]:
        try:
            reply = self.api.call('describe-stacks', '--stack-name', self.identity.stack_id, service='cloudformation')
        except AwsUnavailable as error:
            if 'ValidationError' in str(error) and 'does not exist' in str(error):
                return {}
            raise
        stacks = ledger.rows(reply, 'Stacks')
        if len(stacks) != 1:
            raise ValueError('down requires one stack')
        if stacks[0].get('StackId') != self.identity.stack_id:
            raise ValueError('the live StackId differs from its saved identity')
        return {} if stacks[0].get('StackStatus') == 'DELETE_COMPLETE' else stacks[0]

    def _outputs(self, stack: dict[str, object]) -> dict[str, str]:
        return {ledger.text(row, 'OutputKey'): ledger.text(row, 'OutputValue') for row in ledger.rows(stack, 'Outputs')
                if row.get('OutputKey') in ('InstanceId', 'DataVolumeId', 'BudgetName', 'ComputeEnabled')}

    def verify(self) -> bool:
        self.saved = deployment_record(self.directory)
        receipt = self.directory / 'down-complete.json'
        builder_receipt = self.directory / 'builder-removal.json'
        if builder_receipt.exists() and (self.builder_only
                or ledger.mapping(self.saved.get('outputs', {})).get('ComputeEnabled') == 'false'):
            previous = remote.read_record(builder_receipt)
            outputs = ledger.mapping(self.saved.get('outputs', {})).copy()
            previous_outputs = ledger.mapping(previous.get('outputs', {}))
            self.saved['data_volume_id'] = (self.saved.get('data_volume_id') or outputs.get('DataVolumeId')
                                           or previous.get('data_volume_id') or previous_outputs.get('DataVolumeId', ''))
            if not outputs.get('DataVolumeId'):
                outputs['DataVolumeId'] = self.saved['data_volume_id']
            if not outputs.get('InstanceId'):
                outputs['InstanceId'] = previous_outputs.get('InstanceId', '')
            self.saved['outputs'] = outputs
        complete = not self.saved and receipt.exists()
        if complete:
            self.saved = remote.read_record(receipt)
        if not self.saved:
            raise ValueError('down requires a saved deployment identity; run deploy.py up once to record it')
        saved = self.saved
        for key in ('account_id', 'region', 'stack', 'stack_id', 'deployment_id'):
            value = saved.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f'missing saved {key}; run deploy.py up once to record it')
        account = ledger.text(self.api.call('get-caller-identity', service='sts'), 'Account')
        if (saved.get('account_id') != account or saved.get('region') != self.identity.region
                or saved.get('stack') != self.identity.stack):
            raise ValueError('the caller account, region, or stack differs from the deployment record')
        stack_id = ledger.text(saved, 'stack_id')
        deployment_id = ledger.text(saved, 'deployment_id')
        if not stack_id.startswith(f'arn:aws:cloudformation:{self.identity.region}:{account}:stack/'):
            raise ValueError('the saved StackId differs from the account or region')
        outputs = ledger.mapping(saved.get('outputs', {}))
        self.identity = RemovalIdentity(self.identity.stack, self.identity.region, account, stack_id, deployment_id,
                                        str(outputs.get('InstanceId', '')), str(saved.get('data_volume_id', '')),
                                        str(saved.get('budget_name', '')))
        path = self.directory / 'down.json'
        if path.exists() and not self.builder_only:
            journal = remote.read_record(path)
            for key in ('account_id', 'region', 'stack_id', 'deployment_id'):
                if journal.get(key) != getattr(self.identity, key):
                    raise ValueError('the removal journal differs from the current deployment identity')
        stack = self._stack()
        if not stack:
            try:
                named = ledger.rows(self.api.call('describe-stacks', '--stack-name', self.identity.stack, service='cloudformation'), 'Stacks')
            except AwsUnavailable as error:
                if 'ValidationError' not in str(error) or 'does not exist' not in str(error):
                    raise
                named = ()
            if named and named[0].get('StackId') != self.identity.stack_id:
                raise ValueError('the named stack has another StackId')
        if complete:
            if stack:
                raise ValueError('a completed removal record cannot authorize another stack deletion')
            return True
        # Refusals must leave up available to record missing deletion identities.
        for key in ('data_volume_id', 'budget_name'):
            value = saved.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f'missing saved {key}; run deploy.py up once to record it')
        scaler = ledger.mapping(saved.get('scaler_user', {}))
        for key in ('name', 'arn', 'user_id'):
            value = scaler.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f'missing saved scaler {key}; run deploy.py up once to record it')
        _ = self._device_identity()
        if stack:
            outputs = self._outputs(stack)
            for key, value in (('InstanceId', self.identity.instance), ('DataVolumeId', self.identity.volume),
                               ('BudgetName', self.identity.budget)):
                if value and outputs.get(key, value) != value:
                    raise ValueError(f'the live {key} differs from the deployment record')
        return False

    def remember(self) -> None:
        path = self.directory / ('builder-removal.json' if self.builder_only else 'down.json')
        if self.builder_only or not path.exists():
            write_text_atomic(path, json.dumps(self.saved if self.builder_only else asdict(self.identity)) + '\n')

    def compute(self) -> bool:
        stack = self._stack()
        if not stack or stack.get('StackStatus') in ('REVIEW_IN_PROGRESS', 'DELETE_IN_PROGRESS'):
            return False
        if ledger.text(stack, 'StackStatus').endswith('_IN_PROGRESS'):
            self._wait(lambda: not ledger.text(self._stack(), 'StackStatus').endswith('_IN_PROGRESS'),
                       'the stack update did not finish')
            stack = self._stack()
        if self._outputs(stack).get('ComputeEnabled') != 'true':
            return False
        if stack.get('StackStatus') not in ('CREATE_COMPLETE', 'UPDATE_COMPLETE', 'UPDATE_ROLLBACK_COMPLETE'):
            raise ValueError('down cannot remove compute from stack state ' + ledger.text(stack, 'StackStatus'))
        parameters = [{'ParameterKey': ledger.text(row, 'ParameterKey'), 'UsePreviousValue': True}
                      for row in ledger.rows(stack, 'Parameters') if row.get('ParameterKey') != 'EnableCompute']
        parameters.append({'ParameterKey': 'EnableCompute', 'ParameterValue': 'false'})
        _ = self.api.call('update-stack', '--stack-name', self.identity.stack_id, '--use-previous-template',
                          '--capabilities', 'CAPABILITY_NAMED_IAM', '--parameters', json.dumps(parameters), service='cloudformation')
        def removed() -> bool:
            current = self._stack()
            status = str(current.get('StackStatus', ''))
            if status in ('UPDATE_FAILED', 'UPDATE_ROLLBACK_COMPLETE', 'UPDATE_ROLLBACK_FAILED'):
                raise ValueError('compute removal failed: ' + status)
            return not current or status == 'UPDATE_COMPLETE' and self._outputs(current).get('ComputeEnabled') == 'false'
        self._wait(removed, 'compute removal did not finish')
        print('removed: instance, root volume, launch template, security group, instance profile, compute roles, and volume attachment')
        return True

    def foundation(self) -> bool:
        stack = self._stack()
        if not stack:
            return False
        if stack.get('StackStatus') != 'DELETE_IN_PROGRESS':
            _ = self.api.call('delete-stack', '--stack-name', self.identity.stack_id, service='cloudformation')
        self._wait(lambda: not self._stack(), 'foundation deletion did not finish')
        print('removed: foundation stack, scaler user, policies, and Budget actions')
        return True

    def data_volume(self) -> bool:
        changed = False
        if self.identity.volume:
            reply: dict[str, object]
            try:
                reply = self.api.call('describe-volumes', '--volume-ids', self.identity.volume)
            except AwsUnavailable as error:
                if 'InvalidVolume.NotFound' not in str(error):
                    raise
                reply = {'Volumes': []}
            volumes = ledger.rows(reply, 'Volumes')
            if volumes:
                if len(volumes) != 1 or volumes[0].get('VolumeId') != self.identity.volume:
                    raise ValueError('the retained volume identity differs')
                tags = {ledger.text(tag, 'Key'): str(tag.get('Value', '')) for tag in ledger.rows(volumes[0], 'Tags')}
                if tags.get('DeploymentId') != self.identity.deployment_id:
                    raise ValueError('the retained volume lacks the saved deployment ownership tag')
                if volumes[0].get('State') != 'deleting':
                    if volumes[0].get('State') != 'available' or ledger.rows(volumes[0], 'Attachments'):
                        raise ValueError('the retained data volume is still attached')
                    _ = self.api.call('delete-volume', '--volume-id', self.identity.volume)
                def volume_absent() -> bool:
                    try:
                        return not ledger.rows(self.api.call('describe-volumes', '--volume-ids', self.identity.volume), 'Volumes')
                    except AwsUnavailable as error:
                        if 'InvalidVolume.NotFound' in str(error):
                            return True
                        raise
                self._wait(volume_absent, 'the data volume deletion did not finish')
                print('removed: data volume; the /home build cache is lost')
                changed = True
        return changed

    def retained(self) -> bool:
        if not self.identity.volume or not self.identity.budget:
            raise ValueError('missing saved volume or Budget identity; run deploy.py up once to record it')
        changed = self.data_volume()
        if self.identity.budget:
            account = self.identity.account_id
            try:
                budget = ledger.mapping(self.api.call('describe-budget', '--account-id', account,
                    '--budget-name', self.identity.budget, service='budgets').get('Budget'))
                if budget.get('BudgetName') != self.identity.budget:
                    raise ValueError('the live Budget name differs from the saved identity')
            except AwsUnavailable as error:
                if 'NotFoundException' not in str(error):
                    raise
            else:
                _ = self.api.call('delete-budget', '--account-id', account, '--budget-name', self.identity.budget, service='budgets')
                print('removed: retained Budget')
                changed = True
        return changed

    def local_state(self) -> bool:
        changed = False
        # Explicit names preserve charges, claims, and every ledger record.
        for path in (self.directory / 'up.json', self.directory / 'box-tools.json',
                     self.directory / 'box-shell.json', self.directory / 'scaler-key.json',
                     *(self.output / name for name in ('preflight.json', 'parameters.json', 'template.json', 'bootstrap.sh', 'remote.conf'))):
            if path.exists():
                path.unlink()
                changed = True
        book = ledger.CostLedger(self.directory)
        if book.path.exists():
            snapshot = book.read_snapshot()
            if snapshot.observations and snapshot.observations[-1].state != 'terminated' or book.pending_compute_requests():
                now = int(self.clock())
                book.observe(ledger.InstanceObservation(now, 'terminated', 'confirmed target removal'), confirms_transition=True)
                for request in book.pending_compute_requests():
                    book.resolve_compute_request(request, ledger.ComputeResourcesAbsent(now))
        write_text_atomic(self.directory / 'down-complete.json', json.dumps(self.saved, sort_keys=True) + '\n')
        (self.directory / 'down.json').unlink(missing_ok=True)
        (self.directory / 'deployment.json').unlink(missing_ok=True)
        return changed

    def gate_image(self) -> bool:
        directory = self.directory / 'gate-image'
        if not directory.exists():
            return False
        from scripts.aws_target.gate_box import GATE_IMAGE_OWNER_TAG
        box.cleanup_images(self.api, {}, GATE_IMAGE_OWNER_TAG)
        book = ledger.CostLedger(self.directory)
        inventories = book.read_snapshot().inventories
        if inventories and any(item.name == 'gate-image' for item in inventories[-1].resources):
            previous = inventories[-1]
            book.record_inventory(ledger.ResourceInventory(int(self.clock()),
                tuple(item for item in previous.resources if item.name != 'gate-image'), 'gate image removed',
                builder_removed=previous.builder_removed))
        shutil.rmtree(directory)
        return True

    def _kept_image(self) -> None:
        path = self.directory / 'image.json'
        if path.exists():
            print('down: image kept: ' + ledger.text(ledger.mapping(cast(object, json.loads(path.read_text()))), 'image_id')
                  + '; next up starts from it')

    def run(self) -> bool:
        stages = (('routing-off', self.routing), ('tailscale-device', self.devices), ('scaler-keys', self.scaler),
                  ('compute', self.compute), ('foundation', self.foundation), ('retained-resources', self.retained),
                  ('gate-image', self.gate_image), ('local-state', self.local_state))
        with control_lock(self.directory / 'up'), ExitStack() as locks:
            try:
                if self.verify():
                    for name, action in stages:
                        print(f'{name}: present')
                    self._kept_image()
                    return True
            except (OSError, ValueError, AwsUnavailable, subprocess.SubprocessError) as error:
                print('identity: refused: ' + self.admin.redact(str(error)))
                return False
            for name, action in stages:
                try:
                    if name == 'tailscale-device':
                        # Keep claims and controller actions excluded throughout resource removal.
                        paths = mac_test.state_paths('aws')
                        _ = locks.enter_context(mac_test.locked(paths.lock))
                        _ = locks.enter_context(control_lock(self.directory))
                        if box.running_claims(paths):
                            raise ValueError('AWS claims remain live; retry after their completion')
                    changed = action()
                    status = 'no device' if isinstance(changed, NeverJoined) else 'done' if changed else 'present'
                    print(f'{name}: ' + status)
                except (OSError, ValueError, AwsUnavailable, subprocess.SubprocessError) as error:
                    print(f'{name}: refused: ' + self.admin.redact(str(error)))
                    return False
        self._kept_image()
        return True
