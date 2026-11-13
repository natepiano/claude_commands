"""Join and configure the box only during a user deployment."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from http.client import HTTPResponse
from typing import Protocol, cast, final
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from scripts.aws_target import admin_secrets, ledger
from scripts.aws_target.aws_target import AwsCli, AwsUnavailable
from scripts.build_targets import target_config
from scripts.lib import conf
from scripts.lib.atomic_file import write_text_atomic
from scripts.mac_test import mac_test


IMAGE_OWNER_TAG = 'aws-build-image'


def builder_state_paths() -> mac_test.StatePaths:
    """Resolve the former builder's claims and locks without target registration."""
    return mac_test.StatePaths(target_config.ssh_target_state_directory('aws'), 'aws', platform='linux')


def running_claims(paths: mac_test.StatePaths) -> tuple[mac_test.HeldClaim, ...]:
    """Read remote job claims whose owner process still runs without deleting claims."""
    return tuple(claim for claim in mac_test.read_claims(paths)
                 if 'remote_host' in claim.record and not mac_test.run_is_kept(claim.record))


def close_routing(settings: Path) -> bool:
    """Disable AWS routing while excluding new claims."""
    paths = builder_state_paths()
    path = paths.directory / 'routing.json'
    desired: dict[str, object] = {'enabled': False, 'settings': str(settings.resolve())}
    with mac_test.locked(paths.lock):
        changed = not path.exists() or json_record(path.read_text()) != desired
        if changed:
            write_text_atomic(path, json.dumps(desired) + '\n')
    return changed


class BoxRefused(ValueError):
    """A live box check does not permit the next stage."""


def saved_image(api: AwsCli, directory: Path) -> dict[str, object]:
    """Read the saved image from EC2 without accepting a deleted image."""
    path = directory / 'image.json'
    if not path.exists():
        return {}
    record = json_record(path.read_text())
    try:
        reply = api.call('describe-images', '--image-ids', ledger.text(record, 'image_id'), '--owners', 'self')
    except AwsUnavailable as error:
        if 'NotFound' in str(error):
            return {}
        raise
    return next(iter(ledger.rows(reply, 'Images')), dict[str, object]())


def create_saved_image(api: AwsCli, directory: Path, instance: str, *, name: str,
                       tags: list[dict[str, str]], identity: dict[str, object],
                       exclude_devices: tuple[str, ...] = ()) -> dict[str, object]:
    """Save an image and its identity through the common image boundary."""
    arguments = ['--instance-id', instance, '--no-reboot', '--name', name]
    if exclude_devices:
        arguments.extend(('--block-device-mappings', json.dumps([
            {'DeviceName': device, 'NoDevice': ''} for device in exclude_devices])))
    reply = api.call('create-image', *arguments, '--tag-specifications', json.dumps([
        {'ResourceType': kind, 'Tags': tags} for kind in ('image', 'snapshot')]))
    record = {**identity, 'image_id': ledger.text(reply, 'ImageId')}
    write_text_atomic(directory / 'image.json', json.dumps(record, sort_keys=True) + '\n')
    return record


def image_leftovers(api: AwsCli, current: dict[str, object], owner_tag: str) -> tuple[list[str], list[str]]:
    filters = json.dumps([{'Name': 'tag:' + owner_tag, 'Values': ['true']}])
    images = api.call('describe-images', '--owners', 'self', '--filters', filters)
    snapshots = api.call('describe-snapshots', '--owner-ids', 'self', '--filters', filters)
    # An empty current image keeps no snapshot: every owned image and snapshot goes.
    mappings: tuple[dict[str, object], ...] = ledger.rows(current, 'BlockDeviceMappings') if current else ()
    kept = {ledger.mapping(row.get('Ebs')).get('SnapshotId') for row in mappings}
    return ([ledger.text(image, 'ImageId') for image in ledger.rows(images, 'Images')
             if image.get('ImageId') != current.get('ImageId')],
            [ledger.text(snapshot, 'SnapshotId') for snapshot in ledger.rows(snapshots, 'Snapshots')
             if snapshot.get('SnapshotId') not in kept])


def cleanup_images(api: AwsCli, current: dict[str, object], owner_tag: str) -> bool:
    """Delete older owned images before their snapshots; report whether anything was deleted."""
    images, snapshots = image_leftovers(api, current, owner_tag)
    for image in images:
        _ = api.call('deregister-image', '--image-id', image)
    for snapshot in snapshots:
        _ = api.call('delete-snapshot', '--snapshot-id', snapshot)
    return bool(images or snapshots)


@dataclass(frozen=True)
class PolicyReply:
    value: dict[str, object]
    etag: str = ''


class TailnetTransport(Protocol):
    def __call__(self, method: str, endpoint: str, body: bytes, headers: dict[str, str], timeout: float) -> PolicyReply: ...


def tailnet_request(method: str, endpoint: str, body: bytes, headers: dict[str, str], timeout: float) -> PolicyReply:
    """Report HTTP validation errors without request credentials or user fields."""
    try:
        request = Request('https://api.tailscale.com/api/v2/' + endpoint, data=body or None,
                          headers=headers, method=method)
        with cast(HTTPResponse, urlopen(request, timeout=timeout)) as response:
            content = response.read()
            return PolicyReply(json_record(content) if content else {}, response.headers.get('ETag', ''))
    except HTTPError as error:
        content = error.read().decode(errors='replace')
        detail = ' '.join(content.split())[:500] if content.strip() else str(error.reason)
        try:
            value = cast(object, json.loads(content))
        except ValueError:
            value = None
        if isinstance(value, dict):
            record = cast(dict[str, object], value)
            message = record.get('message')
            details = [message] if isinstance(message, str) and message else []
            data = record.get('data')
            for entry in cast(list[object], data) if isinstance(data, list) else []:
                errors = cast(dict[str, object], entry).get('errors') if isinstance(entry, dict) else None
                if isinstance(errors, list):
                    details.extend(item for item in cast(list[object], errors) if isinstance(item, str))
            detail = '; '.join(' '.join(item.split()) for item in details) or str(error.reason)
        raise BoxRefused(f'Tailscale {method} {endpoint}: HTTP {error.code}: {detail}') from None
    except (OSError, ValueError) as error:
        reason = error.reason if isinstance(error, URLError) else error
        raise BoxRefused(f'Tailscale {method} {endpoint}: request failed: {reason}') from None


@dataclass(frozen=True)
class TailnetAuthorization:
    token: str = field(repr=False)


@final
class Tailnet:
    def __init__(self, settings: Path, admin: admin_secrets.AdminSecretSession, transport: TailnetTransport = tailnet_request) -> None:
        self.settings = settings
        self.transport = transport
        self.admin = admin

    def _authorization(self) -> TailnetAuthorization:
        credentials = self.admin.get()
        body = urlencode({'client_id': credentials.tailscale_client_id,
                          'client_secret': credentials.tailscale_client_secret,
                          'grant_type': 'client_credentials'}).encode()
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

    def delete_device(self, device_id: str) -> None:
        authorization = self._authorization()
        _ = self.transport('DELETE', 'device/' + device_id, b'',
                           {'Authorization': 'Bearer ' + authorization.token},
                           conf.read(self.settings).number('aws_timeout_s'))


# SSM stores this public command. The key enters stdin only after terminal echo stops.
JOIN_COMMAND = '''sudo bash -c 'set +x; unset HISTFILE; stty -echo || exit 1; printf "AWS_JOIN_READY\\n"; IFS= read -r key; printf "%s\\n" "$key" | tailscale up --ssh --advertise-tags=tag:build-aws --auth-key=file:/dev/stdin >/dev/null 2>&1; result=$?; unset key; test "$result" = 0 && printf "AWS_JOIN_DONE\\n"; exit "$result"' '''.strip()


def json_record(content: str | bytes) -> dict[str, object]:
    return ledger.mapping(cast(object, json.loads(content)))


def public_strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise BoxRefused('expected a list of public strings')
    values = cast(list[object], value)
    if not all(isinstance(item, str) for item in values):
        raise BoxRefused('expected a list of public strings')
    return tuple(cast(list[str], values))
