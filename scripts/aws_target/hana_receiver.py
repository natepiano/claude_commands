"""Receive signed control objects as root without guest AWS credentials."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from http.client import HTTPResponse
import inspect
import json
from pathlib import Path
import time
from typing import cast
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from scripts.aws_target import hana_guest as guest
from scripts.lib import conf

# Control messages have a fixed protocol size limit.
CONTROL_MAX_BYTES = 65536


@dataclass(frozen=True)
class ReceiverSettings:
    aws_timeout_s: int
    control_poll_seconds: int


def read_settings(configuration: conf.ConfigRead) -> ReceiverSettings:
    settings = ReceiverSettings(configuration.whole('aws_timeout_s'), configuration.whole('control_poll_seconds'))
    if min(settings.aws_timeout_s, settings.control_poll_seconds) <= 0:
        raise ValueError('Hana receiver intervals must be positive')
    return settings


def read_document(content: bytes) -> dict[str, object]:
    if len(content) > CONTROL_MAX_BYTES:
        raise ValueError('The Hana control document exceeds its protocol limit')
    value = cast(object, json.loads(content))
    if not isinstance(value, dict):
        raise ValueError('The Hana control document must be an object')
    return cast(dict[str, object], value)


def signed_url(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError('The Hana control URL is missing')
    parts = urlsplit(value)
    if (parts.scheme != 'https' or not parts.hostname or not parts.hostname.endswith('.amazonaws.com')
            or '.s3.' not in parts.hostname or parts.username or parts.password or parts.fragment):
        raise ValueError('The Hana control URL is invalid')
    return value


def receive(settings: guest.SafetySettings, descriptor: dict[str, object], control: dict[str, object],
            *, paths: guest.GuestPaths = guest.GUEST_PATHS, clock: Callable[[], float] = time.time) -> dict[str, object]:
    """Verify a control message before acknowledging its installed permit."""
    if (descriptor.get('version') != 1 or not isinstance(descriptor.get('expires'), int)
            or clock() >= cast(int, descriptor['expires']) or not descriptor.get('start_token') or not descriptor.get('deployment')):
        raise ValueError('The Hana boot descriptor is invalid or expired')
    boot = guest.read_identity(paths.boot_id)
    if not boot:
        raise ValueError('The Hana boot identity is missing')
    reply: dict[str, object] = {'deployment': descriptor['deployment'], 'start_token': descriptor['start_token'],
        'boot_id': boot, 'at': int(clock())}
    if 'permit' not in control:
        return reply
    if control.get('deployment') != descriptor['deployment'] or control.get('start_token') != descriptor['start_token']:
        raise ValueError('The Hana control message belongs to another start')
    raw = control['permit']
    if not isinstance(raw, dict):
        raise ValueError('The Hana permit is invalid')
    permit_fields = cast(dict[str, object], raw)
    expires, generation = permit_fields.get('expires_at'), permit_fields.get('generation')
    if (type(expires) not in (int, float) or not isinstance(generation, str) or not generation.isdecimal()
            or int(generation) <= 0 or permit_fields.get('boot_id') != boot
            or not isinstance(control.get('token'), str) or not isinstance(control.get('execution'), str)):
        raise ValueError('The Hana permit identity is invalid')
    permit = guest.WorkPermit(float(cast(float, expires)), generation, boot)
    with guest.guest_lock(paths):
        if paths.fence.exists():
            raise ValueError('Hana shutdown is fenced')
        previous = guest.read_identity(paths.generation)
        current = guest.read_permit(paths.permit)
        if not (isinstance(current, guest.WorkPermit) and current == permit):
            expected = control.get('expected_generation')
            if type(expected) is not int or expected != int(previous or '0'):
                raise ValueError('The Hana permit generation changed')
            if not guest.fresh_permit(settings, permit, generation, boot, clock):
                raise ValueError('The Hana permit is expired or exceeds its window')
            if previous and int(generation) <= int(previous):
                raise ValueError('The Hana permit generation did not increase')
            # A failed partial write refuses permission until a controller recovery.
            guest.atomic_record(paths.permit, asdict(permit))
            _ = paths.generation.write_text(generation)
        elif not guest.fresh_permit(settings, permit, generation, boot, clock):
            raise ValueError('The Hana permit expired')
    reply.update(token=control['token'], generation=int(generation), execution=control['execution'])
    return reply


def transfer(request: Request, timeout: int) -> bytes:
    with cast(HTTPResponse, urlopen(request, timeout=timeout)) as response:
        return response.read(CONTROL_MAX_BYTES + 1)


def run_receiver(settings: guest.SafetySettings, transport: ReceiverSettings,
                 *, paths: guest.GuestPaths = guest.GUEST_PATHS,
                 fetch: Callable[[Request, int], bytes] = transfer, pause: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.time, keep_running: Callable[[], bool] = lambda: True) -> None:
    """Read root-only IMDS user data once per boot and publish scoped acknowledgements."""
    metadata = 'http://169.254.169.254/latest/'
    token = fetch(Request(metadata + 'api/token', method='PUT',
        headers={'X-aws-ec2-metadata-token-ttl-seconds': '21600'}), transport.aws_timeout_s).decode()
    descriptor = read_document(fetch(Request(metadata + 'user-data',
        headers={'X-aws-ec2-metadata-token': token}), transport.aws_timeout_s))
    if descriptor.get('setup_only') is True:
        return
    get_url, put_url = signed_url(descriptor.get('control_url')), signed_url(descriptor.get('report_url'))
    while keep_running():
        control = read_document(fetch(Request(get_url), transport.aws_timeout_s))
        reply = receive(settings, descriptor, control, paths=paths, clock=clock)
        _ = fetch(Request(put_url, data=json.dumps(reply).encode(), method='PUT'), transport.aws_timeout_s)
        pause(transport.control_poll_seconds)


def receiver_program() -> str:
    """Render the reviewed receiver beside the existing standalone safety program."""
    prefix = ('from __future__ import annotations\nfrom collections.abc import Callable\n'
        'from dataclasses import asdict, dataclass\nfrom pathlib import Path\nfrom typing import cast\n'
        'from urllib.parse import urlsplit\nfrom urllib.request import Request, urlopen\n'
        'import json, time\nimport safety as guest\nCONTROL_MAX_BYTES = 65536\n')
    definitions = (ReceiverSettings, read_document, signed_url, receive, transfer, run_receiver)
    program = prefix + '\n'.join(inspect.getsource(item) for item in definitions)
    return program + '''
if __name__ == '__main__':
    try:
        values = json.loads((guest.CONTROL / 'receiver.json').read_text())
        safety = guest.decode_settings(json.loads((guest.CONTROL / 'safety.json').read_text()))
        run_receiver(safety, ReceiverSettings(**values))
    except Exception:
        # The independent guard stops the box after a receiver failure.
        raise SystemExit('Hana receiver refused the control message') from None
'''


def render_files(settings: ReceiverSettings) -> tuple[guest.GuestFile, ...]:
    unit = f'''[Unit]
Description=Hana root control receiver
After=network-online.target
Wants=network-online.target
Before=hana-tools.service

[Service]
Type=simple
User=root
UMask=0077
ExecStart=/run/current-system/sw/bin/python3 {guest.CONTROL}/receiver.py
Restart=on-failure
RestartSec={settings.control_poll_seconds}

[Install]
WantedBy=multi-user.target
'''
    return (guest.GuestFile(guest.CONTROL / 'receiver.py', receiver_program(), 0o600),
        guest.GuestFile(guest.CONTROL / 'receiver.json', json.dumps(asdict(settings)) + '\n', 0o600),
        guest.GuestFile(Path('/etc/systemd/system/hana-receiver.service'), unit, 0o644))
