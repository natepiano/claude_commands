"""Render independent safety services for the kept Hana guest."""
from __future__ import annotations

from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from enum import StrEnum
import fcntl
import inspect
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
from typing import cast

from scripts.lib import conf


CONTROL = Path('/var/lib/hana-aws')
DATA = Path('/var/lib/hana')
PERMIT = Path('/run/hana-aws/work-permit.json')
GENERATION = Path('/run/hana-aws/generation')
BOOT_ID = Path('/proc/sys/kernel/random/boot_id')
WORK_SLICE = 'hana-work.slice'


@dataclass(frozen=True)
class SafetySettings:
    enabled: bool
    guard_grace_seconds: int
    watchdog_check_seconds: int
    permit_max_seconds: int
    step_cpu_limit: int
    step_memory_gib: int
    mount_timeout_seconds: int


def decode_settings(values: Mapping[str, object]) -> SafetySettings:
    """Validate the settings used by the installed guest services."""
    enabled = values['enabled']
    if type(enabled) is not bool:
        raise ValueError('enabled must be a boolean')
    numbers: dict[str, int] = {}
    for key in ('guard_grace_seconds', 'watchdog_check_seconds', 'permit_max_seconds', 'step_cpu_limit',
                'step_memory_gib', 'mount_timeout_seconds'):
        number = values[key]
        if type(number) is not int or number <= 0:
            raise ValueError(key + ' must be a positive integer')
        numbers[key] = number
    return SafetySettings(enabled, **numbers)


def read_settings(configuration: conf.ConfigRead) -> SafetySettings:
    """Read Hana settings without reading deployment or credential files."""
    enabled = configuration.text('enabled')
    if enabled not in ('on', 'off'):
        raise ValueError('enabled must be on or off')
    values: dict[str, object] = {'enabled': enabled == 'on'}
    for key in ('guard_grace_seconds', 'watchdog_check_seconds', 'permit_max_seconds', 'step_cpu_limit',
                'step_memory_gib', 'mount_timeout_seconds'):
        values[key] = configuration.whole(key)
    return decode_settings(values)


@dataclass(frozen=True)
class WorkPermit:
    """Permission for one boot and execution generation until a UTC epoch time."""

    expires_at: float
    generation: str
    boot_id: str


@dataclass(frozen=True)
class NoWorkPermit:
    """The guest has no readable, valid permit record."""


Permit = WorkPermit | NoWorkPermit


def read_permit(path: Path) -> Permit:
    """Treat missing, unreadable, and malformed records as absent permission."""
    try:
        raw = cast(object, json.loads(path.read_text()))
        if not isinstance(raw, dict):
            return NoWorkPermit()
        row = cast(dict[str, object], raw)
        expires, generation, boot_id = row['expires_at'], row['generation'], row['boot_id']
        if (isinstance(expires, bool) or not isinstance(expires, (int, float))
                or not math.isfinite(expires) or expires <= 0):
            return NoWorkPermit()
        if not isinstance(generation, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', generation):
            return NoWorkPermit()
        if not isinstance(boot_id, str) or not boot_id.strip():
            return NoWorkPermit()
        return WorkPermit(float(expires), generation, boot_id)
    except (OSError, UnicodeError, ValueError, KeyError, OverflowError):
        return NoWorkPermit()


def fresh_permit(settings: SafetySettings, permit: Permit, generation: str, boot_id: str,
                 clock: Callable[[], float]) -> bool:
    """Require current identity and an expiry within one configured renewal window."""
    return (isinstance(permit, WorkPermit) and bool(generation) and bool(boot_id)
            and permit.generation == generation and permit.boot_id == boot_id
            and math.isfinite(permit.expires_at)
            and 0 < permit.expires_at - clock() <= settings.permit_max_seconds)


@dataclass(frozen=True)
class GuestPaths:
    """Root-owned coordination files shared by safety, renewal, and command admission."""

    directory: Path
    boot_id: Path

    @property
    def permit(self) -> Path:
        return self.directory / 'work-permit.json'

    @property
    def generation(self) -> Path:
        return self.directory / 'generation'

    @property
    def lock(self) -> Path:
        return self.directory / 'permit.lock'

    @property
    def fence(self) -> Path:
        return self.directory / 'shutdown.json'

    @property
    def deadline(self) -> Path:
        return self.directory / 'permit-deadline.json'


GUEST_PATHS = GuestPaths(PERMIT.parent, BOOT_ID)


@contextmanager
def guest_lock(paths: GuestPaths) -> Generator[None]:
    """Hold one lock across permit reads, shutdown decisions, renewal, and command starts."""
    paths.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(paths.lock, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, 'a') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def atomic_record(path: Path, value: Mapping[str, object]) -> None:
    """Publish a complete private record through a sibling file and atomic rename."""
    descriptor, name = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(dict(value), stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class PermitDeadline:
    """One observed permit with a boot-clock deadline that survives reader restarts."""

    permit: WorkPermit
    monotonic_deadline: float


@dataclass(frozen=True)
class FirstPermitRead:
    """This boot has not recorded a permit deadline."""


@dataclass(frozen=True)
class PermitDeadlineUnreadable:
    """A damaged deadline cannot grant another renewal window."""


BoundPermit = PermitDeadline | NoWorkPermit
PreviousDeadline = PermitDeadline | FirstPermitRead | PermitDeadlineUnreadable


def read_deadline(path: Path) -> PreviousDeadline:
    try:
        raw = cast(object, json.loads(path.read_text()))
        if not isinstance(raw, dict):
            return PermitDeadlineUnreadable()
        row = cast(dict[str, object], raw)
        expires, deadline = row['expires_at'], row['monotonic_deadline']
        generation, boot_id = row['generation'], row['boot_id']
        if (isinstance(expires, bool) or not isinstance(expires, (int, float)) or not math.isfinite(expires)
                or isinstance(deadline, bool) or not isinstance(deadline, (int, float))
                or not math.isfinite(deadline) or not isinstance(generation, str) or not isinstance(boot_id, str)):
            return PermitDeadlineUnreadable()
        return PermitDeadline(WorkPermit(float(expires), generation, boot_id), float(deadline))
    except FileNotFoundError:
        return FirstPermitRead()
    except (OSError, UnicodeError, ValueError, KeyError, OverflowError):
        return PermitDeadlineUnreadable()


def observe_permit(settings: SafetySettings, permit: Permit, previous: PreviousDeadline,
                   generation: str, boot_id: str, *, clock: Callable[[], float],
                   uptime: Callable[[], float]) -> BoundPermit:
    """Bound each new permit read without extending an unchanged permit's boot-clock deadline."""
    if (isinstance(previous, PermitDeadlineUnreadable)
            or not fresh_permit(settings, permit, generation, boot_id, clock)):
        return NoWorkPermit()
    assert isinstance(permit, WorkPermit)
    if isinstance(previous, PermitDeadline) and previous.permit == permit:
        return previous if uptime() < previous.monotonic_deadline else NoWorkPermit()
    return PermitDeadline(permit, uptime() + min(settings.permit_max_seconds, permit.expires_at - clock()))


def bound_permit(settings: SafetySettings, paths: GuestPaths, *, clock: Callable[[], float],
                 uptime: Callable[[], float]) -> BoundPermit:
    """Read and record permission while the caller holds the guest lock."""
    previous = read_deadline(paths.deadline)
    permit = observe_permit(settings, read_permit(paths.permit), previous,
        read_identity(paths.generation), read_identity(paths.boot_id), clock=clock, uptime=uptime)
    if isinstance(permit, PermitDeadline) and permit != previous:
        atomic_record(paths.deadline, {**asdict(permit.permit), 'monotonic_deadline': permit.monotonic_deadline})
    return permit


def write_permit(settings: SafetySettings, permit: WorkPermit, *, paths: GuestPaths = GUEST_PATHS,
                 clock: Callable[[], float] = time.time) -> None:
    """Renew permission atomically unless the guest has fenced shutdown."""
    with guest_lock(paths):
        if paths.fence.exists():
            raise ValueError('Shutdown is fenced; permit renewal is refused')
        if not fresh_permit(settings, permit, read_identity(paths.generation), read_identity(paths.boot_id), clock):
            raise ValueError('The permit identity or renewal window is invalid')
        atomic_record(paths.permit, asdict(permit))


class GuardAction(StrEnum):
    WAIT = 'wait'
    RELEASE = 'release'
    STOP = 'stop'


def guard_action(settings: SafetySettings, permit: BoundPermit, *, boot_elapsed: float,
                 clock: Callable[[], float], uptime: Callable[[], float]) -> GuardAction:
    """Allow setup after a fresh permit, or stop after the boot grace interval."""
    if watchdog_action(settings, permit, clock=clock, uptime=uptime) == WatchdogAction.KEEP:
        return GuardAction.RELEASE
    return GuardAction.WAIT if boot_elapsed < settings.guard_grace_seconds else GuardAction.STOP


class WatchdogAction(StrEnum):
    KEEP = 'keep'
    STOP = 'stop'


def watchdog_action(settings: SafetySettings, permit: BoundPermit, *, clock: Callable[[], float],
                    uptime: Callable[[], float]) -> WatchdogAction:
    """Decide without reading controller or command state."""
    return (WatchdogAction.KEEP if isinstance(permit, PermitDeadline)
            and fresh_permit(settings, permit.permit, permit.permit.generation, permit.permit.boot_id, clock)
            and uptime() < permit.monotonic_deadline else WatchdogAction.STOP)


def stop_work(run: Callable[[list[str]], None]) -> None:
    """Kill every command cgroup before requesting poweroff, including after a kill error."""
    try:
        run(['systemctl', 'kill', '--kill-whom=all', '--signal=KILL', WORK_SLICE])
    finally:
        run(['systemctl', 'poweroff', '--no-block'])


def _command_arguments(settings: SafetySettings, generation: str,
                       command: tuple[str, ...], directory: Path) -> list[str]:
    """Prepare an unprivileged command in its own bounded slice after admission checks."""
    if not re.fullmatch(r'[A-Za-z0-9_]+', generation):
        raise ValueError('The generation cannot name a command unit')
    if not directory.is_relative_to(DATA) or '..' in directory.parts or not command:
        raise ValueError('The command needs a source directory on the data mount')
    return ['systemd-run', '--wait', '--collect', '--uid=hana-runner',
            '--unit=hana-command-' + generation, '--slice=hana-work-' + generation + '.slice',
            '--property=CPUQuota=' + str(settings.step_cpu_limit * 100) + '%',
            '--property=MemoryMax=' + str(settings.step_memory_gib * 1024**3),
            '--property=KillMode=control-group', '--working-directory=' + str(directory), '--', *command]


def admit_command[StartedCommand](settings: SafetySettings, command: tuple[str, ...], directory: Path,
                                  *, mounted: bool, start: Callable[[list[str]], StartedCommand],
                                  paths: GuestPaths = GUEST_PATHS, clock: Callable[[], float] = time.time,
                                  uptime: Callable[[], float] = time.monotonic) -> StartedCommand:
    """Start a command under the same lock that excludes shutdown and renewal races.

    The start callback spawns the process and returns immediately.
    The caller waits for completion outside the lock.
    """
    with guest_lock(paths):
        if paths.fence.exists():
            raise ValueError('Shutdown is fenced; command admission is refused')
        permit = bound_permit(settings, paths, clock=clock, uptime=uptime)
        if (not settings.enabled or not mounted
                or watchdog_action(settings, permit, clock=clock, uptime=uptime) != WatchdogAction.KEEP):
            raise ValueError('The data mount and a fresh permit are required')
        assert isinstance(permit, PermitDeadline)
        return start(_command_arguments(settings, permit.permit.generation, command, directory))


def read_identity(path: Path) -> str:
    """An unreadable identity cannot authorize a permit."""
    try:
        return path.read_text().strip()
    except (OSError, UnicodeError):
        return ''


def run_safety(mode: str, settings: SafetySettings, *, clock: Callable[[], float] = time.time,
               uptime: Callable[[], float] = time.monotonic,
               pause: Callable[[float], None] = time.sleep,
               run: Callable[[list[str]], None], paths: GuestPaths = GUEST_PATHS) -> None:
    """Keep boot grace separate from the watchdog's execution deadline."""
    if mode not in ('guard', 'watchdog'):
        raise ValueError('Expected guard or watchdog')
    while True:
        delay = 0.0
        permit: BoundPermit = NoWorkPermit()
        with guest_lock(paths):
            if paths.fence.exists():
                stopping = True
            else:
                permit = bound_permit(settings, paths, clock=clock, uptime=uptime)
                if mode == 'guard':
                    action = guard_action(settings, permit, boot_elapsed=uptime(), clock=clock, uptime=uptime)
                    if action == GuardAction.RELEASE:
                        return
                    stopping = action == GuardAction.STOP
                else:
                    stopping = watchdog_action(settings, permit, clock=clock, uptime=uptime) == WatchdogAction.STOP
            if stopping:
                atomic_record(paths.fence, {'state': 'shutdown'})
            elif mode == 'guard':
                delay = min(settings.watchdog_check_seconds, max(0, settings.guard_grace_seconds - uptime()))
            else:
                assert isinstance(permit, PermitDeadline)
                delay = min(settings.watchdog_check_seconds, max(0, permit.permit.expires_at - clock()),
                            max(0, permit.monotonic_deadline - uptime()))
        if stopping:
            stop_work(run)
            return
        pause(delay)


@dataclass(frozen=True)
class GuestFile:
    path: Path
    content: str
    mode: int


def guest_program() -> str:
    """Use the gate guest's source-rendering idiom without copying decision logic."""
    definitions = (SafetySettings, decode_settings, WorkPermit, NoWorkPermit, read_permit, fresh_permit,
                   GuestPaths, guest_lock, atomic_record, PermitDeadline, FirstPermitRead, PermitDeadlineUnreadable,
                   read_deadline, observe_permit, bound_permit, write_permit, GuardAction, guard_action,
                   WatchdogAction, watchdog_action, stop_work, _command_arguments, admit_command, read_identity, run_safety)
    header = ('from __future__ import annotations\n'
              'from collections.abc import Callable, Generator, Mapping\nfrom contextlib import contextmanager\n'
              'from dataclasses import asdict, dataclass\n'
              'from enum import StrEnum\nfrom pathlib import Path\nfrom typing import cast\n'
              'import fcntl, json, math, os, re, subprocess, sys, tempfile, time\n')
    constants = ''.join(f'{name} = Path({str(value)!r})\n' for name, value in (
        ('CONTROL', CONTROL), ('DATA', DATA), ('PERMIT', PERMIT), ('GENERATION', GENERATION), ('BOOT_ID', BOOT_ID)))
    program = header + constants + f'WORK_SLICE = {WORK_SLICE!r}\n'
    for definition in definitions:
        program += inspect.getsource(definition) + '\n'
        if definition is GuestPaths:
            program += 'GUEST_PATHS = GuestPaths(PERMIT.parent, BOOT_ID)\n'
    program += '''
Permit = WorkPermit | NoWorkPermit
BoundPermit = PermitDeadline | NoWorkPermit
PreviousDeadline = PermitDeadline | FirstPermitRead | PermitDeadlineUnreadable

def main():
    values = json.loads((CONTROL / 'safety.json').read_text())
    settings = decode_settings(values)
    def run(command):
        subprocess.run(command, check=True, timeout=settings.watchdog_check_seconds)
    run_safety(sys.argv[1], settings, run=run)

if __name__ == '__main__':
    main()
'''
    return program


def render_files(settings: SafetySettings, *, device: Path, tool_setup: Path) -> tuple[GuestFile, ...]:
    """Return guest files without installing units or touching deployment state."""
    for path in (device, tool_setup):
        if not re.fullmatch(r'/[A-Za-z0-9_./-]+', str(path)) or '..' in path.parts:
            raise ValueError('Expected an absolute device or setup path')
    python = '/run/current-system/sw/bin/python3'
    prefix = f'{python} {CONTROL}/safety.py'
    guard = f'''[Unit]
Description=Hana boot permit guard
DefaultDependencies=no
FailureAction=poweroff

[Service]
Type=oneshot
RemainAfterExit=yes
RuntimeDirectory=hana-aws
RuntimeDirectoryMode=0700
RuntimeDirectoryPreserve=yes
ExecStart={prefix} guard
TimeoutStartSec={settings.guard_grace_seconds + settings.watchdog_check_seconds}s

[Install]
WantedBy=multi-user.target
'''
    watchdog = f'''[Unit]
Description=Hana execution permit watchdog
After=hana-guard.service
FailureAction=poweroff
StartLimitAction=poweroff

[Service]
Type=simple
ExecStart={prefix} watchdog
Restart=always
RestartSec={settings.watchdog_check_seconds}s

[Install]
WantedBy=multi-user.target
'''
    mount = f'''[Unit]
Description=Hana data disk
JobTimeoutSec={settings.mount_timeout_seconds}s

[Mount]
What={device}
Where={DATA}
Type=ext4
Options=nofail
TimeoutSec={settings.mount_timeout_seconds}s

[Install]
WantedBy=multi-user.target
'''
    tools = f'''[Unit]
Description=Hana tool setup
Requires=var-lib-hana.mount
After=hana-guard.service var-lib-hana.mount
ConditionPathIsMountPoint={DATA}

[Service]
Type=oneshot
ExecStart={tool_setup}
RemainAfterExit=yes
'''
    work_slice = f'''[Unit]
Description=Hana command resource bounds

[Slice]
CPUQuota={settings.step_cpu_limit * 100}%
MemoryMax={settings.step_memory_gib * 1024**3}
'''
    units = {'hana-guard.service': guard, 'hana-watchdog.service': watchdog,
             'var-lib-hana.mount': mount, 'hana-tools.service': tools, WORK_SLICE: work_slice}
    return (GuestFile(CONTROL / 'safety.py', guest_program(), 0o600),
            GuestFile(CONTROL / 'safety.json', json.dumps(asdict(settings)) + '\n', 0o600),
            *(GuestFile(Path('/etc/systemd/system') / name, content, 0o644) for name, content in units.items()))
