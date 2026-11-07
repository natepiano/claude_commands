#!/usr/bin/env python3
"""Validate admission and stop idle or interrupted AWS work."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from http.client import HTTPResponse
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
from typing import cast, final
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.aws_target import ledger
from scripts.aws_target.config import read_control_settings, service_loop
from scripts.lib import conf
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.process_group import send_process_group
from scripts.mac_test import linux_runner, offload



@dataclass(frozen=True)
class AdmissionEnvelope:
    version: int
    target: str
    purpose: str
    generation: str
    decision_at: int
    expires_at: int
    permission: ledger.AdmissionPermission


def envelope_record(envelope: AdmissionEnvelope) -> dict[str, object]:
    decision = ledger.permission_record(envelope.permission)
    return {'version': envelope.version, 'target': envelope.target, 'purpose': envelope.purpose,
            'generation': envelope.generation, 'decision_at': envelope.decision_at,
            'expires_at': envelope.expires_at, 'decision': decision}


def parse_admission(raw: object, *, target: str, generation: str, now: int) -> AdmissionEnvelope | ledger.PermissionUnavailable:
    try:
        if isinstance(raw, str):
            raw = cast(object, json.loads(raw))
        if not isinstance(raw, dict):
            raise ValueError('expected an admission envelope')
        row = cast(dict[str, object], raw)
        if set(row) != {'version', 'target', 'purpose', 'generation', 'decision_at', 'expires_at', 'decision'}:
            raise ValueError('invalid admission fields')
        if type(row['version']) is not int or row['version'] != 1:
            raise ValueError('invalid admission version')
        if row['target'] != target or not target or row['generation'] != generation or not generation:
            raise ValueError('admission identity does not match')
        if row['purpose'] != 'admission':
            raise ValueError('invalid admission purpose')
        at, expiry = row['decision_at'], row['expires_at']
        if type(at) is not int or type(expiry) is not int or not 0 <= at <= now < expiry or expiry <= at:
            raise ValueError('admission evidence is stale or future')
        permission = ledger.decode_permission(row['decision'])
        if permission.at != at:
            raise ValueError('decision time does not match')
        return AdmissionEnvelope(1, target, 'admission', generation, at, expiry, permission)
    except (ValueError, KeyError, TypeError) as error:
        return ledger.PermissionUnavailable(now, str(error))


def publish_admission(directory: Path, raw: str, *, now: int, boot_id: str) -> bool:
    row = cast(object, json.loads(raw))
    if not isinstance(row, dict):
        return False
    fields = cast(dict[str, object], row)
    target, generation = fields.get('target'), fields.get('generation')
    if not isinstance(target, str) or not isinstance(generation, str):
        return False
    envelope = parse_admission(fields, target=target, generation=generation, now=now)
    if isinstance(envelope, ledger.PermissionUnavailable):
        return False
    directory.mkdir(parents=True, exist_ok=True)
    # A new boot has no valid permission until this channel binds it.
    write_text_atomic(directory / 'admission.json', json.dumps(envelope_record(envelope)))
    write_text_atomic(directory / 'generation.json', json.dumps({'generation': generation, 'boot_id': boot_id}))
    return True


def read_record(path: Path) -> dict[str, object]:
    value = cast(object, json.loads(path.read_text()))
    if not isinstance(value, dict):
        raise ValueError(f'{path.name}: expected an evidence record')
    return cast(dict[str, object], value)


def current_generation(directory: Path, boot_id: str) -> str:
    row = read_record(directory / 'generation.json')
    if row.get('boot_id') != boot_id or not isinstance(row.get('generation'), str) or not row['generation']:
        raise ValueError('instance generation is not bound to this boot')
    return cast(str, row['generation'])


def execution_permission(directory: Path, target: str, *, now: int, boot_id: str) -> ledger.AdmissionPermission:
    try:
        generation = current_generation(directory, boot_id)
        if interruption_generation(directory, target=target, generation=generation):
            return ledger.PermissionUnavailable(now, 'instance interruption is active')
        envelope = parse_admission((directory / 'admission.json').read_text(), target=target,
                                   generation=generation, now=now)
        return envelope if isinstance(envelope, ledger.PermissionUnavailable) else envelope.permission
    except (OSError, ValueError, TypeError, KeyError) as error:
        return ledger.PermissionUnavailable(now, str(error))


def write_interruption(directory: Path, *, target: str, generation: str, at: int) -> None:
    if not target or not generation:
        raise ValueError('interruption identity is missing')
    directory.mkdir(parents=True, exist_ok=True)
    write_text_atomic(directory / 'draining.json', json.dumps({'generation': generation, 'at': at}))
    write_text_atomic(directory / 'interruption.json', json.dumps(
        {'version': 1, 'target': target, 'generation': generation, 'at': at}))


def interruption_generation(directory: Path, *, target: str, generation: str) -> str:
    try:
        row = read_record(directory / 'interruption.json')
        if (row.get('version') == 1 and row.get('target') == target and row.get('generation') == generation
                and type(row.get('at')) is int and cast(int, row['at']) >= 0):
            return generation
    except (OSError, ValueError, TypeError):
        pass
    return ''


@final
class ImdsClient:
    def __init__(self, timeout_s: float) -> None:
        self.timeout_s = timeout_s

    def read(self, path: str) -> bytes:
        token_request = Request('http://169.254.169.254/latest/api/token', method='PUT',
                                headers={'X-aws-ec2-metadata-token-ttl-seconds': '60'})
        with cast(HTTPResponse, urlopen(token_request, timeout=self.timeout_s)) as response:
            token = response.read().decode()
        request = Request('http://169.254.169.254/latest/' + path,
                          headers={'X-aws-ec2-metadata-token': token})
        with cast(HTTPResponse, urlopen(request, timeout=self.timeout_s)) as response:
            return response.read()

    def __call__(self) -> bool:
        try:
            row = ledger.mapping(cast(object, json.loads(self.read('meta-data/spot/instance-action'))))
            return row.get('action') in ('stop', 'terminate', 'hibernate')
        except HTTPError as error:
            if error.code == 404:
                return False
            raise


def write_bootstrap_settings(source: Path, destination: Path) -> None:
    """Bind public lifecycle settings to this instance without granting admission."""
    identity = ledger.mapping(cast(object, json.loads(
        ImdsClient(conf.read(source).number('imds_timeout_s')).read('dynamic/instance-identity/document'))))
    instance_id, region = ledger.text(identity, 'instanceId'), ledger.text(identity, 'region')
    if not re.fullmatch(r'i-[0-9a-f]+', instance_id) or not re.fullmatch(r'[a-z0-9-]+', region):
        raise ValueError('IMDS returned an invalid instance identity')
    text = source.read_text()
    for key, value in (('instance_id', instance_id), ('region', region)):
        text, count = re.subn(rf'^{key}=.*$', f'{key}={value}', text, flags=re.MULTILINE)
        if not count:
            text = text.rstrip('\n') + f'\n{key}={value}\n'
        elif count != 1:
            raise ValueError(f'{key}: expected one bootstrap setting')
    destination.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(destination, text)
    destination.chmod(0o644)


def active_process_groups() -> Sequence[int]:
    groups: set[int] = set()
    tokens: set[bytes] = set()
    leaders: dict[int, int] = {}
    root = Path.home() / offload.linux_mirror_root()
    current_boot = linux_runner.boot_identity()
    for marker in root.glob('*/*/.linux-run.json'):
        lease = linux_runner.read_slot_run(marker)
        if isinstance(lease, linux_runner.UnmarkedSlot):
            continue
        if linux_runner.slot_boot(lease, current_boot, require_boot_identity=True) is linux_runner.SlotBoot.PREVIOUS:
            continue
        tokens.add(f"BUILD_TARGET_RUN_TOKEN={lease['token']}".encode())
        leaders[lease['pid']] = lease['start_time']
    if not tokens:
        return ()
    for process in linux_runner.PROC_ROOT.glob('[0-9]*'):
        try:
            if process.stat().st_uid != os.getuid():
                continue
            pid = int(process.name)
            fields = linux_runner.process_fields(pid, linux_runner.PROC_ROOT)
            if fields[0] in ('Z', 'X'):
                continue
            if leaders.get(pid) == int(fields[19]):
                groups.add(int(fields[2]))
                continue
            environment = set((process / 'environ').read_bytes().split(b'\0'))
            if tokens.intersection(environment):
                groups.add(int(fields[2]))
        except (FileNotFoundError, ProcessLookupError):
            continue
        except (OSError, ValueError, IndexError) as error:
            print(f'aws_remote: process evidence unavailable: {error}', file=sys.stderr)
    return tuple(sorted(groups))


def stop_host() -> None:
    _ = subprocess.run(['shutdown', '-h', 'now'], check=True)


@final
class RemoteAgent:
    def __init__(self, settings_path: Path, directory: Path, *, target: str = 'aws',
                 clock: Callable[[], float] = time.time, imds: Callable[[], bool] | None = None,
                 process_groups: Callable[[], Sequence[int]] = active_process_groups,
                 signal_group: Callable[[int, int], object] = send_process_group,
                 shutdown: Callable[[], object] = stop_host,
                 boot: Callable[[], str] = linux_runner.boot_identity) -> None:
        self.settings_path = settings_path
        self.directory = directory
        self.target = target
        self.clock = clock
        self.imds = imds
        self.process_groups = process_groups
        self.signal_group = signal_group
        self.shutdown = shutdown
        self.boot = boot

    def tick(self) -> str:
        settings = read_control_settings(self.settings_path, remote_identity=True)
        if not settings.enabled:
            return 'off'
        now = int(self.clock())
        boot_id = self.boot()
        try:
            generation = current_generation(self.directory, boot_id)
        except (OSError, ValueError):
            # An unpublished boot must still reach its idle stop.
            generation = f'unpublished:{boot_id}'
        state_path = self.directory / 'idle.json'
        try:
            state = read_record(state_path)
        except FileNotFoundError:
            state = {}
        polled_at = state.get('polled_at', 0)
        if type(polled_at) is not int:
            raise ValueError('invalid interruption poll evidence')
        interrupted = bool(interruption_generation(self.directory, target=self.target, generation=generation))
        if now - polled_at >= settings.interruption_poll_s:
            try:
                interrupted = (self.imds if self.imds is not None else ImdsClient(settings.imds_timeout_s))() or interrupted
            except (OSError, ValueError) as error:
                state['imds_error'] = {'at': now, 'reason': str(error)}
                print(f'aws_remote: interruption check failed: {error}', file=sys.stderr)
            state['polled_at'] = now
        if interrupted:
            write_interruption(self.directory, target=self.target, generation=generation, at=now)
        try:
            groups = tuple(self.process_groups())
        except (OSError, ValueError, linux_runner.SlotProcessesUnresolved) as error:
            groups = ()
            state['process_error'] = {'at': now, 'reason': str(error)}
            print(f'aws_remote: process evidence unavailable: {error}', file=sys.stderr)
        if interrupted:
            for group in groups:
                try:
                    _ = self.signal_group(group, signal.SIGTERM)
                except (OSError, ValueError) as error:
                    state['signal_error'] = {'at': now, 'group': group, 'reason': str(error)}
                    print(f'aws_remote: group signal failed: {error}', file=sys.stderr)
        idle_since = state.get('idle_since', now)
        if type(idle_since) is not int:
            raise ValueError('invalid idle evidence')
        if groups or state.get('generation') != generation:
            idle_since = now
        state.update({'idle_since': idle_since, 'generation': generation})
        self.directory.mkdir(parents=True, exist_ok=True)
        write_text_atomic(state_path, json.dumps(state))
        if not groups and now - idle_since >= settings.idle_stop_minutes * 60:
            _ = self.shutdown()
            return 'stopping'
        return 'interrupted' if interrupted else 'running'


class RemoteArguments(argparse.Namespace):
    def __init__(self) -> None:
        super().__init__()
        self.command: str = ''
        self.directory: Path = Path()
        self.settings: Path = Path()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument('command', choices=('publish', 'tick', 'serve'))
    _ = parser.add_argument('--directory', type=Path)
    _ = parser.add_argument('--settings', type=Path)
    args = parser.parse_args(namespace=RemoteArguments())
    if args.command == 'publish':
        if args.directory == Path():
            parser.error('--directory is required for publish')
        return 0 if publish_admission(args.directory.expanduser(), sys.stdin.read(),
                                      now=int(time.time()), boot_id=linux_runner.boot_identity()) else 1
    if args.settings == Path():
        parser.error('--settings is required for tick or serve')
    if args.command == 'tick' and args.directory == Path():
        parser.error('--directory is required for tick')
    directory = args.directory.expanduser() if args.directory != Path() else Path.home() / read_control_settings(args.settings, remote_identity=True).remote_state_directory
    agent = RemoteAgent(args.settings, directory)
    if args.command == 'serve':
        service_loop(args.settings, 'interruption_poll_s', agent.tick)
        return 0
    print(agent.tick())
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
