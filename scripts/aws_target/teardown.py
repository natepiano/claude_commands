"""Remove the target while retaining its spending evidence and encrypted credentials."""
from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from io import StringIO
import json
from pathlib import Path
import subprocess
import time
from typing import final

from scripts.aws_target import admin_secrets, box, ledger, remote
from scripts.aws_target.aws_target import AwsCli, AwsExecutor, AwsUnavailable, control_lock
from scripts.aws_target.config import deployment_record
from scripts.build_targets import target_config
from scripts.lib import conf
from scripts.lib.atomic_file import write_bytes_atomic, write_text_atomic
from scripts.mac_test import mac_test


DRY_RUN = '''down would disable AWS routing and aws-target.service.
down refuses while an AWS claim remains live.
down would delete the aws-build Tailscale device and keep tag:build-aws policy rules.
down would delete all scaler access keys and remove only the scaler credential profile.
down would remove compute resources, then the foundation stack and retained Budget.
down would delete the data volume. The /home build cache is lost.
down would remove deployment.json, up.json, setup proofs, and the prepared bundle.
down keeps the spend ledger, charge history, disabled routing.json, and admin.gpg.
Run deploy.py down --approve-destroy to remove the target.'''


@dataclass(frozen=True)
class RemovalIdentity:
    stack: str
    region: str
    instance: str = ''
    volume: str = ''
    budget: str = ''


@final
class TargetRemoval:
    def __init__(self, settings: Path, directory: Path, output: Path, admin: admin_secrets.AdminSecretSession,
                 api: AwsCli, execute: AwsExecutor, *, tailnet: box.TailnetTransport = box.tailnet_request,
                 pause: Callable[[float], object] = time.sleep, clock: Callable[[], float] = time.time) -> None:
        self.settings = settings
        self.directory = directory
        self.output = output
        self.admin = admin
        self.api = api
        self.execute = execute
        self.tailnet = box.Tailnet(settings, admin, tailnet)
        self.pause = pause
        self.clock = clock
        self.identity = RemovalIdentity(conf.read(settings).text('stack_name'), conf.read(settings).text('region'))

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
        path = target_config.ssh_target_state_directory('aws') / 'routing.json'
        desired: dict[str, object] = {'enabled': False, 'settings': str(self.settings.resolve())}
        paths = mac_test.state_paths('aws')
        with mac_test.locked(paths.lock):
            changed = not path.exists() or remote.read_record(path) != desired
            if changed:
                write_text_atomic(path, json.dumps(desired) + '\n')
        enabled = self._local(['systemctl', '--user', 'is-enabled', 'aws-target.service'])
        active = self._local(['systemctl', '--user', 'is-active', 'aws-target.service'])
        if enabled.stdout.strip() not in ('disabled', 'not-found') or active.stdout.strip() not in ('inactive', 'unknown'):
            result = self._local(['systemctl', '--user', 'disable', '--now', 'aws-target.service'])
            if result.returncode:
                raise ValueError('could not disable aws-target.service; routing remains disabled')
            changed = True
        with mac_test.locked(paths.lock):
            if mac_test.live_claims(paths):
                raise ValueError('AWS claims remain live; routing is disabled; retry after their completion')
        return changed

    def devices(self) -> bool:
        changed = False
        for device in ledger.rows(self.tailnet.call('GET', 'devices').value, 'devices'):
            hostname = device.get('hostname')
            if hostname != 'aws-build':
                continue
            if 'tag:build-aws' not in box.public_strings(device.get('tags', [])):
                raise ValueError('the aws-build device lacks tag:build-aws; review its identity')
            self.tailnet.delete_device(ledger.text(device, 'id'))
            changed = True
        return changed

    def scaler(self) -> bool:
        user = conf.read(self.settings).text('scaler_user_name')
        reply: dict[str, object]
        changed = False
        try:
            reply = self.api.call('list-access-keys', '--user-name', user, service='iam')
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
            reply = self.api.call('describe-stacks', '--stack-name', self.identity.stack, service='cloudformation')
        except AwsUnavailable as error:
            if 'ValidationError' in str(error) and 'does not exist' in str(error):
                return {}
            raise
        stacks = ledger.rows(reply, 'Stacks')
        if len(stacks) != 1:
            raise ValueError('down requires one stack')
        return {} if stacks[0].get('StackStatus') == 'DELETE_COMPLETE' else stacks[0]

    def _outputs(self, stack: dict[str, object]) -> dict[str, str]:
        return {ledger.text(row, 'OutputKey'): ledger.text(row, 'OutputValue') for row in ledger.rows(stack, 'Outputs')
                if row.get('OutputKey') in ('InstanceId', 'DataVolumeId', 'BudgetName', 'ComputeEnabled')}

    def remember(self) -> None:
        path = self.directory / 'down.json'
        saved = remote.read_record(path) if path.exists() else deployment_record(self.directory)
        if saved and (saved.get('stack') != self.identity.stack or saved.get('region') != self.identity.region):
            raise ValueError('down requires the saved stack name and region')
        stack = self._stack()
        outputs = self._outputs(stack) if stack else {}
        previous = ledger.mapping(saved.get('outputs', {})) if not path.exists() else saved
        def identity(key: str, old: str) -> str:
            value = str(previous.get(old if path.exists() else key, ''))
            current = outputs.get(key, '')
            if value and current and value != current:
                raise ValueError(f'down: the saved {key} differs from the stack')
            return current or value
        self.identity = RemovalIdentity(self.identity.stack, self.identity.region,
                                        identity('InstanceId', 'instance'), identity('DataVolumeId', 'volume'),
                                        identity('BudgetName', 'budget'))
        # Save only public identities before an update removes the compute outputs.
        if stack or saved:
            content = json.dumps(asdict(self.identity)) + '\n'
            if not path.exists() or path.read_text() != content:
                write_text_atomic(path, content)

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
        _ = self.api.call('update-stack', '--stack-name', self.identity.stack, '--use-previous-template',
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
            _ = self.api.call('delete-stack', '--stack-name', self.identity.stack, service='cloudformation')
        self._wait(lambda: not self._stack(), 'foundation deletion did not finish')
        print('removed: foundation stack, scaler user, policies, and Budget actions')
        return True

    def retained(self) -> bool:
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
        if self.identity.budget:
            account = ledger.text(self.api.call('get-caller-identity', service='sts'), 'Account')
            try:
                _ = self.api.call('describe-budget', '--account-id', account, '--budget-name', self.identity.budget, service='budgets')
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
        for path in (self.directory / 'deployment.json', self.directory / 'up.json', self.directory / 'box-tools.json',
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
        (self.directory / 'down.json').unlink(missing_ok=True)
        return changed

    def run(self) -> bool:
        stages = (('routing-off', self.routing), ('tailscale-device', self.devices), ('scaler-keys', self.scaler),
                  ('compute', self.compute), ('foundation', self.foundation), ('retained-resources', self.retained),
                  ('local-state', self.local_state))
        with control_lock(self.directory / 'up'), ExitStack() as locks:
            for name, action in stages:
                try:
                    if name == 'tailscale-device':
                        # Keep claims and controller actions excluded throughout resource removal.
                        paths = mac_test.state_paths('aws')
                        _ = locks.enter_context(mac_test.locked(paths.lock))
                        _ = locks.enter_context(control_lock(self.directory))
                        if mac_test.live_claims(paths):
                            raise ValueError('AWS claims remain live; retry after their completion')
                        self.remember()
                    changed = action()
                    print(f'{name}: ' + ('done' if changed else 'present'))
                except (OSError, ValueError, AwsUnavailable, subprocess.SubprocessError) as error:
                    print(f'{name}: refused: ' + self.admin.redact(str(error)))
                    return False
        return True
