"""Join and configure the box only during a user deployment."""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
import ipaddress
import json
import os
from pathlib import Path
from http.client import HTTPResponse
import selectors
import shlex
import subprocess
import time
from typing import Protocol, cast, final
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from scripts.aws_target import ledger
from scripts.aws_target.aws_target import AwsCli, AwsExecutor, AwsUnavailable, control_lock, execute_command
from scripts.aws_target.config import deployment_outputs, read_deployment_settings
from scripts.lib import conf
from scripts.lib.atomic_file import write_text_atomic
from scripts.mac_test import linux_runner, offload


class BoxRefused(ValueError):
    """A live box check does not permit the next stage."""


@dataclass(frozen=True)
class PolicyReply:
    value: dict[str, object]
    etag: str = ''


class TailnetTransport(Protocol):
    def __call__(self, method: str, endpoint: str, body: bytes, headers: dict[str, str], timeout: float) -> PolicyReply: ...


def tailnet_request(method: str, endpoint: str, body: bytes, headers: dict[str, str], timeout: float) -> PolicyReply:
    """Keep HTTP error bodies and credentials out of deployment errors."""
    try:
        request = Request('https://api.tailscale.com/api/v2/' + endpoint, data=body or None,
                          headers=headers, method=method)
        with cast(HTTPResponse, urlopen(request, timeout=timeout)) as response:
            content = response.read()
            return PolicyReply(json_record(content) if content else {}, response.headers.get('ETag', ''))
    except (OSError, ValueError):
        raise BoxRefused(f'Tailscale {method} {endpoint}: request failed') from None


@dataclass(frozen=True)
class TailnetAuthorization:
    token: str = field(repr=False)


@final
class Tailnet:
    def __init__(self, settings: Path, transport: TailnetTransport = tailnet_request) -> None:
        self.settings = settings
        self.transport = transport

    def _authorization(self) -> TailnetAuthorization:
        path = Path(conf.read(self.settings).text('tailscale_oauth_file')).expanduser().resolve()
        try:
            if path.is_relative_to(Path(__file__).resolve().parents[2]) or path.stat().st_mode & 0o777 != 0o600:
                raise ValueError
            credentials = json_record(path.read_text())
            body = urlencode({'client_id': ledger.text(credentials, 'client_id'),
                              'client_secret': ledger.text(credentials, 'client_secret'),
                              'grant_type': 'client_credentials'}).encode()
        except (OSError, ValueError):
            raise BoxRefused(f'tailscale_oauth_file: {path}: require private JSON outside the repository, mode 0600') from None
        reply = self.transport('POST', 'oauth/token', body,
                               {'Content-Type': 'application/x-www-form-urlencoded'},
                               conf.read(self.settings).number('aws_timeout_s'))
        return TailnetAuthorization(ledger.text(reply.value, 'access_token'))

    def call(self, method: str, endpoint: str, value: object = None, *, etag: str = '') -> PolicyReply:
        authorization = self._authorization()
        headers = {'Authorization': 'Bearer ' + authorization.token, 'Accept': 'application/json',
                   'Content-Type': 'application/json'}
        if etag:
            headers['If-Match'] = etag
        return self.transport(method, 'tailnet/-/' + endpoint,
                              json.dumps(value).encode() if value is not None else b'', headers,
                              conf.read(self.settings).number('aws_timeout_s'))


@dataclass(frozen=True)
class JoinedBox:
    address: str
    node_id: str


@dataclass(frozen=True)
class UnjoinedBox:
    pass


# SSM stores this public command. The key enters stdin only after terminal echo stops.
JOIN_COMMAND = '''sudo bash -c 'set +x; unset HISTFILE; stty -echo || exit 1; printf "AWS_JOIN_READY\\n"; IFS= read -r key; printf "%s\\n" "$key" | tailscale up --ssh --advertise-tags=tag:build-aws --auth-key=file:/dev/stdin >/dev/null 2>&1; result=$?; unset key; test "$result" = 0 && printf "AWS_JOIN_DONE\\n"; exit "$result"' '''.strip()


class JoinSession(Protocol):
    def __call__(self, arguments: Sequence[str], key: str, timeout: float) -> None: ...


def join_session(arguments: Sequence[str], key: str, timeout: float) -> None:
    """Wait for echo suppression before sending the key through SSM stdin."""
    try:
        with subprocess.Popen(arguments, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as process:
            if process.stdout is None:
                raise BoxRefused('SSM join has no output channel')
            try:
                deadline = time.monotonic() + timeout
                output = b''
                with selectors.DefaultSelector() as selector:
                    _ = selector.register(process.stdout, selectors.EVENT_READ)
                    while b'AWS_JOIN_READY\r\n' not in output and b'AWS_JOIN_READY\n' not in output:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0 or not selector.select(remaining):
                            raise BoxRefused('SSM join did not disable terminal echo')
                        chunk = os.read(process.stdout.fileno(), 4096)
                        if not chunk or len(output) > 65536:
                            raise BoxRefused('SSM join did not disable terminal echo')
                        output += chunk
                output, _ = process.communicate((key + '\n').encode(), timeout=max(0.001, deadline - time.monotonic()))
                if process.returncode or b'AWS_JOIN_DONE' not in output:
                    raise BoxRefused('SSM join failed; the key was not printed')
            finally:
                if process.poll() is None:
                    process.kill()
                    _ = process.communicate()
    except (OSError, subprocess.SubprocessError):
        raise BoxRefused('SSM join failed; the key was not printed') from None


@final
class BoxSetup:
    def __init__(self, settings: Path, directory: Path, start: Callable[[], object], *,
                 execute: AwsExecutor = execute_command, tailnet: TailnetTransport = tailnet_request,
                 session: JoinSession = join_session, pause: Callable[[float], object] = time.sleep) -> None:
        self.settings = settings
        self.directory = directory
        self.start = start
        self.execute = execute
        self.tailnet = Tailnet(settings, tailnet)
        self.session = session
        self.pause = pause
        self.joined: JoinedBox | UnjoinedBox = UnjoinedBox()

    def _run(self, arguments: Sequence[str]) -> str:
        result = self.execute(arguments, capture_output=True, text=True,
                              timeout=conf.read(self.settings).whole('box_setup_timeout_s'), check=False)
        if result.returncode:
            raise BoxRefused(f'{arguments[0]} failed during box setup (exit {result.returncode})')
        return result.stdout.strip()

    def _api(self) -> AwsCli:
        settings = read_deployment_settings(self.settings, self.directory)
        return AwsCli(settings.profile, settings.region, settings.timeout_s, self.execute)

    def _instance(self) -> str:
        outputs = deployment_outputs(self.directory)
        if not outputs.get('InstanceId'):
            raise BoxRefused('deployment.json has no InstanceId')
        return outputs['InstanceId']

    def _wait(self, check: Callable[[], bool], reason: str) -> None:
        settings = conf.read(self.settings)
        attempts = settings.whole('box_check_attempts')
        for attempt in range(attempts):
            if check():
                return
            if attempt + 1 < attempts:
                _ = self.pause(conf.read(self.settings).number('box_check_delay_s'))
        raise BoxRefused(reason)

    def ssm_online(self) -> bool:
        reply = self._api().call('describe-instance-information', '--filters',
                                 json.dumps([{'Key': 'InstanceIds', 'Values': [self._instance()]}]), service='ssm')
        return any(row.get('InstanceId') == self._instance() and row.get('PingStatus') == 'Online'
                   for row in ledger.rows(reply, 'InstanceInformationList'))

    def _ssm(self, command: str) -> str:
        api = self._api()
        reply = api.call('send-command', '--instance-ids', self._instance(), '--document-name', 'AWS-RunShellScript',
                         '--parameters', json.dumps({'commands': [command]}), service='ssm')
        command_id = ledger.text(ledger.mapping(reply.get('Command')), 'CommandId')
        output = ''
        def completed() -> bool:
            nonlocal output
            try:
                result = api.call('get-command-invocation', '--command-id', command_id,
                                  '--instance-id', self._instance(), service='ssm')
            except AwsUnavailable as error:
                if 'InvocationDoesNotExist' in str(error):
                    return False
                raise
            status = ledger.text(result, 'Status')
            if status in ('Pending', 'InProgress', 'Delayed'):
                return False
            if status != 'Success':
                raise BoxRefused(f'SSM command {command_id}: {status}')
            output = ledger.text(result, 'StandardOutputContent')
            return True
        self._wait(completed, 'SSM command did not finish')
        return output.strip()

    def _status(self) -> dict[str, object]:
        return json_record(self._run(['tailscale', 'status', '--json']))

    def _policy(self) -> tuple[PolicyReply, dict[str, object], list[str]]:
        source = json_record((Path(__file__).resolve().parents[2] / 'config/aws-build-tailscale.json').read_text())
        status = self._status()
        nodes = [ledger.mapping(status.get('Self')), *[ledger.mapping(p) for p in ledger.mapping(status.get('Peer')).values()]]
        bindings: dict[str, str] = {}
        for placeholder, hostname in ledger.mapping(source.pop('devices')).items():
            matches = [node for node in nodes if node.get('HostName') == hostname]
            if len(matches) != 1:
                raise BoxRefused(f'Tailscale policy requires one device named {hostname}')
            addresses = public_strings(matches[0].get('TailscaleIPs'))
            if not addresses:
                raise BoxRefused(f'Tailscale policy has no address for {hostname}')
            bindings[placeholder] = str(addresses[0])
        for placeholder, address in bindings.items():
            source = json_record(json.dumps(source).replace(json.dumps(placeholder), json.dumps(address)))
        live = self.tailnet.call('GET', 'acl')
        desired = dict(live.value)
        changes: list[str] = []
        owners = dict(ledger.mapping(desired.get('tagOwners', {})))
        wanted_owners = ledger.mapping(source.get('tagOwners'))['tag:build-aws']
        if owners.get('tag:build-aws') != wanted_owners:
            owners['tag:build-aws'] = wanted_owners
            desired['tagOwners'] = owners
            changes.append('set tagOwners for tag:build-aws')
        for section in ('acls', 'grants', 'ssh'):
            existing = list(ledger.rows(live.value, section)) if section in live.value else []
            keep: list[dict[str, object]] = []
            for row in existing:
                src = public_strings(row.get('src'))
                dst = public_strings(row.get('dst'))
                # Additive policies cannot deny a broad outbound rule without changing unrelated access.
                if any(s in ('*', 'tag:build-aws', 'autogroup:tagged') for s in src):
                    raise BoxRefused(f'Tailscale {section}: an outbound rule includes tag:build-aws; require policy review')
                scoped = [d for d in dst if d == 'tag:build-aws' or d.startswith('tag:build-aws:')]
                if scoped and len(scoped) != len(dst):
                    raise BoxRefused(f'Tailscale {section}: a mixed destination rule requires policy review')
                if not scoped:
                    keep.append(row)
            wanted = list(ledger.rows(source, section)) if section in source else []
            merged = [*keep, *wanted]
            if merged != existing:
                desired[section] = merged
                changes.append(f'replace {section} rules for tag:build-aws: ' + json.dumps(wanted, sort_keys=True))
        return live, desired, changes

    def policy_present(self) -> bool:
        return not self._policy()[2]

    def policy(self) -> None:
        live, desired, changes = self._policy()
        if not changes:
            return
        if not live.etag:
            raise BoxRefused('Tailscale policy has no ETag; refuse a concurrent policy overwrite')
        for change in changes:
            print('tailscale-policy: ' + change)
        _ = self.tailnet.call('POST', 'acl', desired, etag=live.etag)
        if not self.policy_present():
            raise BoxRefused('Tailscale policy did not retain the requested rules')

    def join_present(self) -> bool:
        self.joined = UnjoinedBox()
        if not self.ssm_online():
            return False
        # Select only public identity fields before SSM records command output.
        command = "python3 - <<'PY'\nimport json, subprocess\ns = json.loads(subprocess.run(['tailscale', 'status', '--json'], capture_output=True, text=True).stdout or '{}')\np = json.loads(subprocess.run(['tailscale', 'debug', 'prefs'], capture_output=True, text=True).stdout or '{}')\nn = s.get('Self') or {}\nprint(json.dumps({'running': s.get('BackendState') == 'Running', 'ssh': p.get('RunSSH'), 'tags': n.get('Tags', []), 'id': n.get('ID', ''), 'addresses': n.get('TailscaleIPs', [])}))\nPY"
        state = json_record(self._ssm(command))
        if not state.get('running') or not state.get('ssh') or 'tag:build-aws' not in cast(list[str], state.get('tags', [])):
            return False
        addresses = public_strings(state.get('addresses'))
        if not addresses:
            return False
        address = ipaddress.ip_address(str(addresses[0]))
        if address not in ipaddress.ip_network('100.64.0.0/10') and address not in ipaddress.ip_network('fd7a:115c:a1e0::/48'):
            raise BoxRefused('SSM returned an address outside Tailscale')
        self.joined = JoinedBox(str(address), ledger.text(state, 'id'))
        return True

    def join(self) -> None:
        _ = self.start()
        self._wait(self.ssm_online, 'the box did not reach SSM')
        if self.join_present():
            return
        key = self.tailnet.call('POST', 'keys', {
            'capabilities': {'devices': {'create': {'reusable': False, 'ephemeral': False,
                                                   'preauthorized': True, 'tags': ['tag:build-aws']}}},
            'expirySeconds': conf.read(self.settings).whole('tailscale_key_expiry_s')}).value
        key_id = ledger.text(key, 'id')
        try:
            settings = read_deployment_settings(self.settings, self.directory)
            self.session(['aws', '--profile', settings.profile, '--region', settings.region, 'ssm', 'start-session',
                          '--target', self._instance(), '--document-name', 'AWS-StartInteractiveCommand',
                          '--parameters', json.dumps({'command': [JOIN_COMMAND]})], ledger.text(key, 'key'),
                         conf.read(self.settings).whole('box_setup_timeout_s'))
            self._wait(self.join_present, 'the box did not confirm its Tailscale identity')
        finally:
            _ = self.tailnet.call('DELETE', 'keys/' + key_id)

    def _ssh(self, command: str) -> str:
        if isinstance(self.joined, UnjoinedBox):
            raise BoxRefused('the box has no SSM-confirmed Tailscale identity')
        connection = offload.read_connection_settings(self.settings)
        return self._run(['ssh', *offload.ssh_options(connection), 'natepiano@' + self.joined.address, command])

    def _desired_system(self) -> str:
        source = Path(conf.read(self.settings).text('nixos_configuration_directory')).expanduser().resolve()
        if self._run(['git', '-C', str(source), 'branch', '--show-current']) != 'main':
            raise BoxRefused('the NixOS source must use main')
        if self._run(['git', '-C', str(source), 'status', '--porcelain']):
            raise BoxRefused('the NixOS source must have no local edits')
        result = self._run(['nix', 'eval', '--raw', '--no-write-lock-file', f'path:{source}#nixosConfigurations.aws-build.config.system.build.toplevel.outPath'])
        if not result.startswith('/nix/store/') or not re_store_path(result):
            raise BoxRefused('Nix returned an invalid system path')
        return result

    def configuration_present(self) -> bool:
        if not self.join_present():
            return False
        return self._ssh('readlink -f /run/current-system') == self._desired_system() and self._remote_active()

    def _remote_active(self) -> bool:
        try:
            return self._ssh('systemctl is-active aws-build-remote.service') == 'active'
        except BoxRefused:
            return False

    def configure(self) -> None:
        desired = self._desired_system()
        source = Path(conf.read(self.settings).text('nixos_configuration_directory')).expanduser().resolve()
        built = self._run(['nix', 'build', '--no-link', '--print-out-paths', '--no-write-lock-file',
                           f'path:{source}#nixosConfigurations.aws-build.config.system.build.toplevel'])
        if built != desired or self._desired_system() != desired:
            raise BoxRefused('the NixOS source changed during the build')
        _ = self.start()
        self._wait(self.join_present, 'box configuration requires the joined instance')
        values = conf.read(self.settings)
        timeout = values.whole('box_setup_timeout_s') + values.whole('box_check_attempts') * values.number('box_check_delay_s')
        idle_record = Path('/home/natepiano') / conf.read(self.settings).text('remote_state_directory') / 'idle.json'
        reset_idle = 'rm -f -- ' + shlex.quote(str(idle_record))
        # Hold the controller lock and use a bounded box timer during the copy and detached switch.
        with control_lock(self.directory):
            _ = self._ssh(f'sudo systemd-run --unit=aws-build-setup-stop --on-active={timeout}s --collect ' +
                          '/run/current-system/sw/bin/systemctl poweroff && ' +
                          'sudo systemctl stop aws-build-remote.service && ' +
                          'if systemctl cat aws-build-first-boot-stop.timer >/dev/null 2>&1; then ' +
                          'sudo systemctl stop aws-build-first-boot-stop.timer; fi')
            try:
                self._copy([desired])
                recovery = reset_idle + '; systemctl start aws-build-remote.service'
                script = ('trap ' + shlex.quote(recovery) + ' EXIT; ' +
                          shlex.quote(desired + '/bin/switch-to-configuration') + ' switch')
                try:
                    _ = self._ssh('sudo systemd-run --unit=aws-build-switch --collect --no-block ' +
                                  '/run/current-system/sw/bin/bash -c ' + shlex.quote(script))
                except BoxRefused as error:
                    if 'exit 255' not in str(error):
                        raise
                    # A switch can restart Tailscale before SSH returns its launch status.
                def switched() -> bool:
                    try:
                        return self._ssh('readlink -f /run/current-system') == desired and self._remote_active()
                    except (BoxRefused, OSError, subprocess.SubprocessError):
                        return False
                self._wait(switched, 'the detached switch did not confirm the system and remote service')
            finally:
                # A failed reconnect leaves the poweroff timer armed.
                try:
                    _ = self._ssh(reset_idle + ' && sudo systemctl start aws-build-remote.service && ' +
                                  'systemctl is-active --quiet aws-build-remote.service && ' +
                                  'sudo systemctl stop aws-build-setup-stop.timer')
                except (BoxRefused, OSError, subprocess.SubprocessError):
                    pass

    def _copy(self, paths: list[str]) -> None:
        if isinstance(self.joined, UnjoinedBox):
            raise BoxRefused('copy requires the joined instance')
        # Nix uses the same bounded SSH connection as the other box commands.
        options = offload.ssh_options(offload.read_connection_settings(self.settings))
        _ = self._run(['env', 'NIX_SSHOPTS=' + shlex.join(options), 'nix', 'copy', '--substitute-on-destination',
                      '--to', 'ssh://natepiano@' + self.joined.address, *paths])

    def _shell(self) -> tuple[str, str]:
        directory = Path(conf.read(self.settings).text('box_shell_directory')).expanduser().resolve()
        selector = conf.read(self.settings).text('devshell.' + directory.name) if (
            'devshell.' + directory.name in conf.read(self.settings).values()) else conf.read(self.settings).text('devshell')
        return str(directory), selector

    def _versions(self, source: str, selector: str, *, on_box: bool) -> dict[str, str]:
        versions: dict[str, str] = {}
        for tool, command in (('rustc', ['rustc', '-V']), ('nextest', ['cargo', 'nextest', '--version']),
                              ('mend', ['cargo', 'mend', '--version'])):
            arguments = ['nix', 'develop', '--no-write-lock-file', f'path:{source}#{selector}', '--command', *command]
            output = self._ssh(shlex.join(arguments)) if on_box else self._run(arguments)
            versions[tool] = linux_runner.normalize_tool_version(output)
            if not versions[tool]:
                raise BoxRefused(f'{tool}: the configured shell returned no version')
        return versions

    def _health(self) -> bool:
        if not self.join_present() or not self._remote_active():
            return False
        outputs = deployment_outputs(self.directory)
        if not outputs.get('DataVolumeId'):
            raise BoxRefused('deployment.json has no DataVolumeId')
        volume = outputs['DataVolumeId'].replace('-', '')
        command = ('test "$(findmnt -n -o TARGET --target /home)" = /home && ' +
                   'device=$(findmnt -n -o SOURCE --target /home) && ' +
                   'test "$(lsblk -dn -o SERIAL "$device" | tr -d "[:space:]-")" = ' + shlex.quote(volume) +
                   ' && test "$(findmnt -n -o FSTYPE --target /home)" = ext4 && printf healthy')
        return self._ssh(command) == 'healthy'

    def checks_present(self) -> bool:
        if not self._health():
            return False
        directory, selector = self._shell()
        path = self.directory / 'box-shell.json'
        if not path.exists():
            return False
        saved = json_record(path.read_text())
        source = ledger.text(saved, 'source')
        # The archive identity changes when shell inputs change.
        archive = json_record(self._run(['nix', 'flake', 'archive', '--json', '--no-write-lock-file', 'path:' + directory]))
        if archive.get('path') != source or saved.get('selector') != selector:
            return False
        expected = self._versions(directory, selector, on_box=False)
        return self._versions(source, selector, on_box=True) == expected

    def checks(self) -> None:
        if not self._health():
            raise BoxRefused('box checks require SSM, the expected /home volume, and aws-build-remote')
        directory, selector = self._shell()
        archive = json_record(self._run(['nix', 'flake', 'archive', '--json', '--no-write-lock-file', 'path:' + directory]))
        paths: list[str] = []
        def collect(row: dict[str, object]) -> None:
            paths.append(ledger.text(row, 'path'))
            for child in ledger.mapping(row.get('inputs', {})).values():
                collect(ledger.mapping(child))
        collect(archive)
        if not all(re_store_path(path) for path in paths):
            raise BoxRefused('the shell archive contains an invalid store path')
        self._copy(paths)
        expected = self._versions(directory, selector, on_box=False)
        actual = self._versions(paths[0], selector, on_box=True)
        mismatches = [f'{tool} box={actual[tool]} natedev={version}' for tool, version in expected.items() if actual[tool] != version]
        if mismatches:
            raise BoxRefused('aws version mismatch: ' + '; '.join(mismatches) + '; move the box pins in /etc/nixos/hosts/aws-build.nix')
        write_text_atomic(self.directory / 'box-shell.json', json.dumps({'source': paths[0], 'selector': selector}) + '\n')
        if not self._health():
            raise BoxRefused('box health changed during the tool checks')


def re_store_path(value: str) -> bool:
    import re
    return re.fullmatch(r'/nix/store/[a-z0-9]{32}-[A-Za-z0-9+._?=-]+', value) is not None


def json_record(content: str | bytes) -> dict[str, object]:
    return ledger.mapping(cast(object, json.loads(content)))


def public_strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise BoxRefused('expected a list of public strings')
    values = cast(list[object], value)
    if not all(isinstance(item, str) for item in values):
        raise BoxRefused('expected a list of public strings')
    return tuple(cast(list[str], values))
