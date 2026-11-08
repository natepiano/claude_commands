#!/usr/bin/env python3
"""Keep one designer and reviewer per design, with explicit placement and phase access.

Only the pinned authority changes placements. Local lifecycle actions keep thread identity,
assignment evidence and pending messages when a server or acknowledgement is lost.
"""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import configparser
import fcntl
import hashlib
import importlib
import json
import os
import shlex
import socket
import subprocess
import sys
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol, cast
from uuid import UUID, uuid4

from scripts.astra import astra_work
from scripts.agents import worker_lifecycle
from scripts.lib.atomic_file import write_text_atomic
from scripts.production import staff, session_staff, worker_admission

ROOT = Path(__file__).resolve().parents[2]
Role = Literal['designer', 'reviewer']
BUSY = ('starting', 'running', 'waiting_capacity')
DESIGNER_SANDBOX = 'danger-full-access'


class AstraCensusProjection(Protocol):
    def HeldDesignRole(self, design_id: str, role_id: str) -> object: ...
    def hold(self, role: Literal['designer', 'reviewer'], lifetime: object, staff_id: str) -> object: ...
    def release(self, staff_id: str) -> None: ...
    def live_roles(self, membership: staff.StaffSnapshot) -> Sequence[Mapping[str, object]]: ...


def _census_projection() -> AstraCensusProjection:
    return cast(AstraCensusProjection, cast(object, importlib.import_module('scripts.astra.astra_roles')))


@dataclass(frozen=True)
class DesignUnbound:
    pass


@dataclass(frozen=True)
class DesignBoundToPhase:
    run_directory: str
    phase: str
    repository: str
    output_paths: tuple[str, ...]


Binding = DesignUnbound | DesignBoundToPhase
UNBOUND = DesignUnbound()


@dataclass(frozen=True)
class KeepBinding:
    """A rebind keeps the saved binding."""


KEEP_BINDING = KeepBinding()


@dataclass(frozen=True)
class PhaseBoundDesigner:
    binding_generation: int
    launch_sandbox: str


@dataclass(frozen=True)
class ReadOnlyReviewer:
    pass


ThreadAccessPolicy = PhaseBoundDesigner | ReadOnlyReviewer


@dataclass(frozen=True)
class LegacyCreationState:
    """An old claim cannot prove that thread creation did not start."""


LEGACY_CREATION_STATE = LegacyCreationState()


@dataclass(frozen=True, init=False)
class RoleNotOpened:
    claim: str
    creation: Literal['not_started', 'outcome_unknown']

    def __init__(self, claim: str = '',
                 creation: Literal['not_started', 'outcome_unknown'] | LegacyCreationState = LEGACY_CREATION_STATE) -> None:
        object.__setattr__(self, 'claim', claim)
        object.__setattr__(self, 'creation', ('outcome_unknown' if claim else 'not_started')
                           if isinstance(creation, LegacyCreationState) else creation)


@dataclass(frozen=True)
class HeldWorker:
    staff_id: staff.StaffId

    @property
    def thread_id(self) -> str:
        member = staff.by_id(self.staff_id)
        return member.current_session if member is not None else ''


@dataclass(frozen=True)
class RoleOpen(HeldWorker):
    pass


@dataclass(frozen=True)
class RolePausedForMove(HeldWorker):
    pass


@dataclass(frozen=True)
class RoleNeedsRecovery(HeldWorker):
    reason: str


@dataclass(frozen=True)
class RoleEnded(HeldWorker):
    pass


Lifecycle = RoleNotOpened | RoleOpen | RolePausedForMove | RoleNeedsRecovery | RoleEnded


@dataclass(frozen=True)
class DesignPlacement:
    design_id: str
    host: str
    generation: int


@dataclass(frozen=True)
class RoleInstance:
    role: Role
    name: str
    lifecycle: Lifecycle
    assignment_claim: str = ""


@dataclass(frozen=True)
class HeldDesign:
    placement: DesignPlacement
    repository_id: str
    repository: str
    document: str
    area: str
    created_at_utc: str
    binding: Binding
    roles: tuple[RoleInstance, ...]


@dataclass(frozen=True)
class PendingMessage:
    id: str
    source: str
    text: str


class RoleRefused(ValueError):
    pass


class DamagedDesignRecord(RoleRefused):
    path: Path
    field: str

    def __init__(self, path: Path, field: str) -> None:
        self.path = path
        self.field = field
        super().__init__('missing or invalid ' + field.rsplit('.', 1)[-1])


def state_root() -> Path:
    return Path(os.environ.get('DESIGN_ROLES_STATE') or '~/.local/state/astra/design-roles').expanduser()


def ownership_root() -> Path:
    return Path(os.environ.get('DESIGN_OWNERSHIP_STATE') or '~/.local/state/astra/design-ownership').expanduser()


def local_host() -> str:
    return os.environ.get('DESIGN_ROLES_HOST') or socket.gethostname().split('.', 1)[0]


def design_directory(design_id: str) -> Path:
    if str(UUID(design_id)) != design_id:
        raise RoleRefused('design id must be a UUID')
    return state_root() / design_id


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise RoleRefused('expected a JSON object')
    return cast(dict[str, object], value)


def _read(path: Path) -> dict[str, object]:
    return worker_lifecycle.read_worker_state(path)


def _text(fields: dict[str, object], key: str) -> str:
    value = fields.get(key)
    if not isinstance(value, str) or not value:
        raise RoleRefused('missing or invalid ' + key)
    return value


def _integer(fields: dict[str, object], key: str) -> int:
    value = fields.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise RoleRefused('missing or invalid ' + key)
    return value


def _atomic(path: Path, value: object) -> None:
    _ = worker_lifecycle.read_worker_value(path)
    _ = worker_lifecycle.validate_worker_state_value(path, value)
    write_text_atomic(path, json.dumps(value, indent=2, sort_keys=True) + '\n')


@contextmanager
def _lock(directory: Path) -> Generator[None]:
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'lock').open('a', encoding='utf-8') as handle:
        worker_lifecycle.acquire_flock(handle, mode=fcntl.LOCK_EX)
        try:
            yield
        finally:
            worker_lifecycle.acquire_flock(handle, mode=fcntl.LOCK_UN)


def _tag(value: Binding | Lifecycle | ThreadAccessPolicy) -> dict[str, object]:
    return {'kind': type(value).__name__, **cast(dict[str, object], asdict(value))}


def _binding(fields: dict[str, object]) -> Binding:
    if fields.get('kind') == 'DesignUnbound':
        return DesignUnbound()
    if fields.get('kind') != 'DesignBoundToPhase':
        raise RoleRefused('unknown binding')
    paths = fields.get('output_paths')
    if not isinstance(paths, list) or not paths or any(not isinstance(path, str) for path in cast(list[object], paths)):
        raise RoleRefused('binding needs exact output paths')
    return DesignBoundToPhase(_text(fields, 'run_directory'), _text(fields, 'phase'),
                              _text(fields, 'repository'), tuple(cast(list[str], paths)))


def _lifecycle(fields: dict[str, object]) -> Lifecycle:
    kind = fields.get('kind')
    if kind == 'RoleNotOpened':
        claim = fields.get('claim', '')
        if not isinstance(claim, str):
            raise RoleRefused('invalid launch claim')
        creation = fields.get('creation', 'outcome_unknown' if claim else 'not_started')
        if creation not in ('not_started', 'outcome_unknown'):
            raise RoleRefused('invalid thread creation state')
        return RoleNotOpened(claim, creation)
    if kind == 'RoleEnded' and fields.get('staff_id') == '':
        return RoleEnded(staff.StaffId(''))
    reference = staff.StaffId(_text(fields, 'staff_id'))
    if kind == 'RoleOpen':
        return RoleOpen(reference)
    if kind == 'RolePausedForMove':
        return RolePausedForMove(reference)
    if kind == 'RoleNeedsRecovery':
        return RoleNeedsRecovery(reference, _text(fields, 'reason'))
    if kind == 'RoleEnded':
        return RoleEnded(reference)
    raise RoleRefused('unknown lifecycle')


def read_design(design_id: str) -> HeldDesign:
    return _load_design(design_id, repair_empty_repository=False)


def _load_design(design_id: str, *, repair_empty_repository: bool) -> HeldDesign:
    path = design_directory(design_id) / 'design.json'
    try:
        return _decode_design(design_id, _read(path), repair_empty_repository=repair_empty_repository)
    except RoleRefused as error:
        raise worker_lifecycle.WorkerStateUnreadable(path, str(error)) from error


def _decode_design(design_id: str, fields: dict[str, object], *,
                        repair_empty_repository: bool = False) -> HeldDesign:
    path = design_directory(design_id) / 'design.json'
    if fields.get('schema') != 1:
        raise RoleRefused('unknown design schema')
    placement = _object(fields.get('placement'))
    if placement.get('design_id') != design_id:
        raise RoleRefused('design identity differs from directory')
    entries = fields.get('roles')
    if not isinstance(entries, list):
        raise RoleRefused('missing roles')
    instances: list[RoleInstance] = []
    for value in cast(list[object], entries):
        entry = _object(value)
        role = entry.get('role')
        if role not in ('designer', 'reviewer') or any(instance.role == role for instance in instances):
            raise RoleRefused('invalid or duplicate role')
        claim = entry.get('assignment_claim', '')
        if not isinstance(claim, str):
            raise RoleRefused('invalid assignment claim')
        instances.append(RoleInstance(role, _text(entry, 'name'), _lifecycle(_object(entry.get('lifecycle'))), claim))
    if {instance.role for instance in instances} != {'designer', 'reviewer'}:
        raise RoleRefused('design must record both role slots')
    for key in ('repository', 'document'):
        if not Path(_text(fields, key)).is_absolute():
            raise RoleRefused(key + ' must be absolute')
    binding_fields = _object(fields.get('binding'))
    empty_repository = (binding_fields.get('kind') == 'DesignBoundToPhase'
                        and binding_fields.get('repository') == '')
    if empty_repository:
        binding_fields = {**binding_fields, 'repository': _text(fields, 'repository')}
    design = HeldDesign(DesignPlacement(design_id, _text(placement, 'host'), _integer(placement, 'generation')),
                      _text(fields, 'repository_id'), _text(fields, 'repository'), _text(fields, 'document'),
                      _text(fields, 'area'), _text(fields, 'created_at_utc'),
                      _binding(binding_fields), tuple(instances))
    if empty_repository and not repair_empty_repository:
        raise DamagedDesignRecord(path, 'binding.repository')
    return design


def _design_fields(design: HeldDesign) -> dict[str, object]:
    if isinstance(design.binding, DesignBoundToPhase) and not design.binding.repository:
        raise RoleRefused('missing or invalid binding.repository')
    fields = asdict(design)
    fields['schema'] = 1
    binding_fields = _tag(design.binding)
    if isinstance(design.binding, DesignBoundToPhase):
        binding_fields['output_paths'] = list(design.binding.output_paths)
    fields['binding'] = binding_fields
    roles: list[dict[str, object]] = []
    for role in design.roles:
        lifecycle = _tag(role.lifecycle)
        roles.append({'role': role.role, 'name': role.name, 'lifecycle': lifecycle, 'assignment_claim': role.assignment_claim})
    fields['roles'] = roles
    return fields


def _save(design: HeldDesign) -> None:
    fields = _design_fields(design)
    _ = _decode_design(design.placement.design_id, fields)
    _atomic(design_directory(design.placement.design_id) / 'design.json', fields)


def _instance(design: HeldDesign, role: Role) -> RoleInstance:
    return next(instance for instance in design.roles if instance.role == role)


def _set_lifecycle(design: HeldDesign, role: Role, lifecycle: Lifecycle) -> HeldDesign:
    return replace(design, roles=tuple(replace(instance, lifecycle=lifecycle) if instance.role == role else instance
                                      for instance in design.roles))


def authority_request(operation: str, payload: dict[str, object]) -> dict[str, object]:
    """Send an ownership request after releasing all design and mesh locks."""
    config = configparser.ConfigParser()
    path = Path(os.environ.get('DESIGN_ROLES_CONFIG') or ROOT / 'config/design_roles.conf')
    if not config.read(path):
        raise RoleRefused('ownership authority is not configured')
    authority = config.get('ownership', 'host')
    if authority == local_host():
        return _authority_local(operation, payload)
    command = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', authority,
               '~/.claude/scripts/lib/py ~/.claude/scripts/astra/design_roles.py authority ' + shlex.quote(operation)]
    result = subprocess.run(command, input=json.dumps(payload), capture_output=True, text=True, timeout=30, check=False)
    if result.returncode:
        raise RoleRefused('ownership authority unavailable: ' + (result.stderr or result.stdout).strip())
    return _object(cast(object, json.loads(result.stdout)))


def register_repository(repository_id: str, checkouts: dict[str, str]) -> None:
    if not repository_id or not checkouts:
        raise RoleRefused('repository id and checkout mappings are required')
    _ = authority_request('register', {'repository_id': repository_id, 'checkouts': checkouts})


def _authority_local(operation: str, payload: dict[str, object]) -> dict[str, object]:
    root = ownership_root()
    with _lock(root):
        path = root / 'repositories.json'
        repositories = _read(path) if path.exists() else {}
        repository_id = _text(payload, 'repository_id')
        if operation == 'register':
            checkouts = _object(payload.get('checkouts'))
            for host, checkout in checkouts.items():
                if not host or not isinstance(checkout, str) or not Path(checkout).is_absolute():
                    raise RoleRefused('checkout mappings must name absolute paths')
            previous = repositories.get(repository_id)
            if previous is not None and _object(previous).get('checkouts') != checkouts:
                raise RoleRefused('repository already registered; update its checkout mapping explicitly')
            repositories[repository_id] = payload
            _atomic(path, repositories)
            return payload
        repository = repositories.get(repository_id)
        if repository is None:
            host = payload.get('host') or '<host>'
            raise RoleRefused(f'repository {repository_id} is not registered; run design_roles.py register '
                              + f'--repository {repository_id} --host {host} '
                              + '--checkout <absolute path>')
        repository = _object(repository)
        checkouts = _object(repository.get('checkouts'))
        host = _text(payload, 'host')
        checkout = checkouts.get(host)
        if not isinstance(checkout, str):
            raise RoleRefused('repository has no explicit checkout mapping for ' + host)
        if operation == 'checkout':
            old = _text(payload, 'old_checkout')
            if checkout not in (old, payload.get('new_checkout')):
                raise RoleRefused('checkout mapping changed')
            checkouts[host] = _text(payload, 'new_checkout')
            repository['checkouts'] = checkouts
            repositories[repository_id] = repository
            _atomic(path, repositories)
            for placed_path in (root / 'placements').glob('*.json'):
                placed = _read(placed_path)
                if placed.get('repository_id') == repository_id and placed.get('host') == host:
                    placed['checkout'] = checkouts[host]
                    _atomic(placed_path, placed)
            return repository
        document = _text(payload, 'document')
        try:
            relative = (Path(checkout) / document).resolve().relative_to(Path(checkout).resolve())
        except ValueError as error:
            raise RoleRefused('document is outside registered checkout') from error
        if relative == Path('.'):
            raise RoleRefused('document must be inside registered repository')
        key = hashlib.sha256((repository_id + '\0' + relative.as_posix()).encode()).hexdigest()
        placement_path = root / 'placements' / f'{key}.json'
        if placement_path.exists():
            placement = _read(placement_path)
            if placement.get('host') != host:
                raise RoleRefused('design is placed on another host')
            if operation == 'advance':
                expected = _integer(payload, 'expected_generation')
                if placement.get('generation') not in (expected, expected + 1):
                    raise RoleRefused('authority binding generation changed')
                for alias_path in (root / 'placements').glob('*.json'):
                    alias = _read(alias_path)
                    if alias.get('design_id') == placement.get('design_id'):
                        alias['generation'] = expected + 1
                        _atomic(alias_path, alias)
                placement['generation'] = expected + 1
            if operation == 'alias':
                try:
                    alias = (Path(checkout) / _text(payload, 'alias')).resolve().relative_to(Path(checkout).resolve()).as_posix()
                except ValueError as error:
                    raise RoleRefused('document alias is outside registered checkout') from error
                alias_key = hashlib.sha256((repository_id + '\0' + alias).encode()).hexdigest()
                alias_path = root / 'placements' / f'{alias_key}.json'
                if alias_path.exists() and _read(alias_path).get('design_id') != placement['design_id']:
                    raise RoleRefused('document alias already belongs to another design')
                _atomic(alias_path, placement)
            return placement
        if operation != 'reserve':
            raise RoleRefused('design placement not recorded')
        placement: dict[str, object] = {'design_id': str(uuid4()), 'host': host, 'generation': 1,
                     'request_id': _text(payload, 'request_id'), 'repository_id': repository_id,
                     'document': relative.as_posix(), 'checkout': checkout}
        _atomic(placement_path, placement)
        return placement


def _check_host(design: HeldDesign) -> None:
    if design.placement.host != local_host():
        raise RoleRefused('design host differs from local host')


def worker_parent(design: HeldDesign, role: Role, record: staff.StaffSnapshot) -> staff.StaffRow:
    """Read the parent of an opened helper from the staff record."""
    instance = _instance(design, role)
    if isinstance(instance.lifecycle, (RoleNotOpened, RoleEnded)):
        raise RoleRefused('role has no active worker')
    row = record.by_id(instance.lifecycle.staff_id)
    parent = record.parent_of(row) if row is not None else None
    if row is None or row.kind != 'worker' or parent is None:
        raise RoleRefused('role worker parent is unavailable')
    return parent


def _validate_binding(binding: Binding, repository: str, role: Role) -> None:
    if role == 'designer' and isinstance(binding, DesignUnbound):
        raise RoleRefused('a designer needs an authorized phase binding')
    if isinstance(binding, DesignBoundToPhase):
        _ = astra_work.phase_work(binding.run_directory, binding.phase)
        if Path(binding.repository).resolve() != Path(repository).resolve() or not binding.output_paths:
            raise RoleRefused('binding must name its repository and exact output paths')
        for path in binding.output_paths:
            try:
                _ = (Path(repository) / path).resolve().relative_to(Path(repository).resolve())
            except ValueError as error:
                raise RoleRefused('output path escapes repository') from error
        from scripts.direct import phase_reservation
        phases = phase_reservation.open_phases(Path(binding.run_directory))
        name = phase_reservation.phase_directory_name(binding.phase)
        phase = next((phase for phase in phases if phase.phase == name), None)
        if phase is None:
            raise RoleRefused('phase reservation ledger is unreadable or phase is not open')
        _ = _read(phase.directory / 'delegated_phase_reservation_state.json')
        files = (phase.directory / phase_reservation.FILES_FILE).read_text(encoding='utf-8').splitlines()
        allowed = {(Path(repository) / path.strip()).resolve() for path in files if path.strip()}
        if any((Path(repository) / path).resolve() not in allowed for path in binding.output_paths):
            raise RoleRefused('binding paths are outside the phase')


def mesh_directory(design_id: str) -> Path:
    return design_directory(design_id) / 'mesh'


def durable_meshes() -> tuple[Path, ...]:
    return tuple(sorted(path.parent for path in state_root().glob('*/mesh/mesh_roster.json')))


@contextmanager
def _edit_roster(design_id: str) -> Generator[dict[str, object]]:
    path = mesh_directory(design_id) / 'mesh_roster.json'
    with worker_lifecycle.locked_state(path) as state:
        roster = _read(state.path)
        yield roster
        session_staff.publish_worker_roster(state, roster)


def _roster(design: HeldDesign) -> dict[str, object]:
    path = mesh_directory(design.placement.design_id) / 'mesh_roster.json'
    return _read(path) if path.exists() else {}


def _idle(design: HeldDesign) -> None:
    if any(instance.assignment_claim or (isinstance(instance.lifecycle, RoleNotOpened) and instance.lifecycle.claim)
           for instance in design.roles):
        raise RoleRefused("role is busy; an assignment claim is active")
    for value in _roster(design).values():
        entry = _object(value)
        if entry.get('status') in BUSY:
            raise RoleRefused('role is busy; finish its turn first')


def _pane_recovery_idle(lifecycle: HeldWorker) -> None:
    result = _worker_command(['list', '--json'])
    listing = cast(list[object], json.loads(result.stdout))
    fields = next((_object(value) for value in listing if _object(value).get('id') == lifecycle.staff_id),
                  dict[str, object]())
    if fields.get('state') not in ('OpenWorker', 'StoppedWorker', 'ClosedWorker'):
        raise RoleRefused('role process evidence is unavailable; worker is retained')
    if fields.get('state') == 'OpenWorker' and fields.get('activity') not in ('idle', 'done'):
        raise RoleRefused('role is busy; finish its turn first')


def _prompt(design: HeldDesign, role: Role, text: str) -> tuple[Path, Path]:
    directory = design_directory(design.placement.design_id)
    token = f'{datetime.now(UTC):%Y%m%dT%H%M%S%fZ}-{uuid4()}'
    reply = directory / 'replies' / f'{role}-{token}.md'
    prompt = directory / 'prompts' / f'{role}-{token}.md'
    if role == 'designer':
        instructions = (ROOT / 'config/design_roles/designer.md').read_text(encoding='utf-8')
        instructions += '\nUse apply_patch for all repository edits. Edit only the bound output paths.\n'
    else:
        review_prompt = cast(Callable[[Path, str], str],
                             importlib.import_module('scripts.astra.astra_review').review_prompt)
        instructions = review_prompt(Path(design.document), '')
    work = (astra_work.phase_work(design.binding.run_directory, design.binding.phase)
            if isinstance(design.binding, DesignBoundToPhase) else astra_work.StandaloneWork())
    header = astra_work.AstraWork('astra-' + role, _instance(design, role).name, design.document,
                                 datetime.now(UTC).isoformat(), str(reply), work)
    pending = pending_messages(design.placement.design_id, role)
    message_text = '\n'.join(f'Pending message {message.id} from {message.source}: {message.text}' for message in pending)
    prompt.parent.mkdir(parents=True, exist_ok=True)
    reply.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(prompt, astra_work.header_line(header) + '\n' + instructions + '\n' + text + '\n' + message_text)
    _atomic(prompt.with_suffix('.messages.json'), [message.id for message in pending])
    return prompt, reply


def access_policy(design: HeldDesign, role: Role) -> ThreadAccessPolicy:
    return PhaseBoundDesigner(design.placement.generation, DESIGNER_SANDBOX) if role == 'designer' else ReadOnlyReviewer()


def _mesh_context(design: HeldDesign) -> None:
    _atomic(mesh_directory(design.placement.design_id) / 'design_context.json', _mesh_context_fields(design))


def _mesh_context_fields(design: HeldDesign) -> dict[str, object]:
    return {'design_id': design.placement.design_id, 'generation': design.placement.generation,
            'binding': _design_fields(design)['binding'], 'cwd': design.repository}


def _worker_command(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    """Use the shared command without another worker selector."""
    return subprocess.run([sys.executable, str(ROOT / 'scripts/production/worker.py'), *arguments],
                          capture_output=True, text=True, check=False)


def dispatch_turn(design: HeldDesign, role: Role, prompt: Path, reply: Path) -> None:
    """Dispatch through assign.sh and save the completed reply."""
    if isinstance(design.binding, DesignBoundToPhase):
        run, phase = Path(design.binding.run_directory), design.binding.phase
    else:
        run, phase = design_directory(design.placement.design_id), 'astra-review'
        state = worker_lifecycle.read_worker_state(run / 'progress_history_state.json')
        if not state:
            _recorder_command(['start-run', '--session-dir', str(run), '--working-dir', design.repository])
        if not state.get('phase'):
            _recorder_command(['start-phase', '--session-dir', str(run), '--phase-id', phase,
                               '--phase-title', 'Held Astra review'])
    name = _instance(design, role).name
    parent = worker_parent(design, role, staff.snapshot())
    result = _assign_command(['--role', 'review' if role == 'reviewer' else 'implement',
        '--target', 'durable-design-role', '--policy', 'astra-' + role,
        '--session-dir', str(run), '--parent', str(parent.id), '--to', name,
        '--phase', phase, '--prompt', str(prompt), '--summary', str(reply)])
    if result.returncode:
        raise RoleRefused(result.stderr.strip() or f'held dispatch exited {result.returncode}; pending input is retained')


def _assign_command(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(['bash', str(ROOT / 'scripts/direct/assign.sh'), *arguments],
                          capture_output=True, text=True, check=False)


def _recorder_command(arguments: list[str], *, env: Mapping[str, str] = os.environ) -> None:
    result = subprocess.run([sys.executable, str(ROOT / 'scripts/direct/progress_history.py'), *arguments],
                            env=env, capture_output=True, text=True, check=False)
    if result.returncode:
        raise RoleRefused(result.stderr.strip() or 'held dispatch recording did not complete')


def _project_hold(design: HeldDesign, role: Role, record: staff.StaffSnapshot) -> None:
    astra_roles = _census_projection()
    instance = _instance(design, role)
    if isinstance(instance.lifecycle, (RoleNotOpened, RoleEnded)):
        return
    _ = worker_parent(design, role, record)
    _ = astra_roles.hold(role, astra_roles.HeldDesignRole(design.placement.design_id, role),
                         str(instance.lifecycle.staff_id))


def refresh_census(design_id: str) -> None:
    design = read_design(design_id)
    record = staff.snapshot()
    for instance in design.roles:
        _project_hold(design, instance.role, record)


def _allow_new_roles() -> None:
    config = configparser.ConfigParser()
    _ = config.read(os.environ.get('DESIGN_ROLES_CONFIG') or ROOT / 'config/design_roles.conf')
    if not config.getboolean('roles', 'new_open', fallback=True):
        raise RoleRefused('new role opens and adoptions are disabled; keep existing identities and evidence')


def open_role(repository: str, document: str, role: Role, area: str, *,
              host: str = '', binding: Binding = UNBOUND, request_id: str = '') -> HeldDesign:
    """Reserve identity before creation; retries never make a replacement thread."""
    _allow_new_roles()
    host = host or local_host()
    if host != local_host():
        raise RoleRefused('pair host differs from local host')
    placement = authority_request('reserve', {'repository_id': repository, 'document': document,
                                             'host': host, 'request_id': request_id or str(uuid4())})
    design_id = _text(placement, 'design_id')
    binding = _resolved_binding(binding, _text(placement, 'checkout'))
    directory = design_directory(design_id)
    with _lock(directory):
        if (directory / 'design.json').exists():
            design = read_design(design_id)
        else:
            checkout = _text(placement, 'checkout')
            document_path = str((Path(checkout) / _text(placement, 'document')).resolve())
            design = HeldDesign(DesignPlacement(design_id, host, _integer(placement, 'generation')),
                                repository, checkout, document_path, area, datetime.now(UTC).isoformat(),
                                binding, tuple(RoleInstance(kind, f'astra-{design_id}-{kind}',
                                                          RoleNotOpened()) for kind in ('designer', 'reviewer')))
            _save(design)
        _check_host(design)
        instance = _instance(design, role)
        if isinstance(instance.lifecycle, RoleEnded):
            raise RoleRefused('role ended; this design identity cannot be reopened.')
        if not isinstance(instance.lifecycle, RoleNotOpened) or instance.lifecycle.creation == 'outcome_unknown':
            if not isinstance(instance.lifecycle, RoleNotOpened):
                _project_hold(design, role, staff.snapshot())
            else:
                print(f'creation outcome unknown; run recover for {design_id}/{role}; replacement refused', file=sys.stderr)
            return design
        _validate_binding(design.binding, design.repository, role)
    name = instance.name
    with _lock(directory):
        current = read_design(design_id)
        if current != design:
            raise RoleRefused('RoleNeedsRecovery: role changed before pane creation; run recover')
        design = replace(current, roles=tuple(replace(value, name=name, lifecycle=RoleNotOpened('pane-start', 'not_started'))
            if value.role == role else value for value in current.roles))
        _save(design)
    design = _begin_creation(design, role, 'pane-start')
    run = Path(design.binding.run_directory) if isinstance(design.binding, DesignBoundToPhase) else directory
    parent = staff.snapshot().by_current_session(staff.runtime_id())
    if parent is None or parent.kind not in ('unit', 'showrunner'):
        raise RoleRefused('staff: caller: not opened: caller is not a unit or showrunner')
    result = _assign_command(['--role', 'review' if role == 'reviewer' else 'implement',
        '--target', 'durable-design-role', '--policy', 'astra-' + role, '--new-worker', name,
        '--session-dir', str(run), '--parent', str(parent.id)])
    if result.returncode:
        raise RoleRefused(result.stderr.strip() or 'held worker startup did not complete; creation claim is retained')
    fields = _object(cast(object, json.loads(result.stdout)))
    row_id = staff.StaffId(_text(fields, 'id'))
    with _lock(directory):
        current = read_design(design_id)
        if _instance(current, role).lifecycle != RoleNotOpened('pane-start', 'outcome_unknown'):
            raise RoleRefused('RoleNeedsRecovery: thread creation claim changed')
        design = _set_lifecycle(current, role, RoleOpen(row_id))
        _save(design)
    _project_hold(design, role, staff.snapshot())
    return design


def _begin_creation(design: HeldDesign, role: Role, claim: str) -> HeldDesign:
    """Record an uncertain outcome before the worker command creates a pane."""
    design_id = design.placement.design_id
    with _lock(design_directory(design_id)):
        current = read_design(design_id)
        if current != design or _instance(current, role).lifecycle != RoleNotOpened(claim, 'not_started'):
            raise RoleRefused('RoleNeedsRecovery: thread creation claim changed')
        updated = _set_lifecycle(current, role, RoleNotOpened(claim, 'outcome_unknown'))
        _save(updated)
        return updated


def recover(instance_id: str) -> HeldDesign:
    """Prove the recorded thread idle before clearing an uncertain local claim."""
    design_id, role_text = instance_id.split('/')
    if role_text not in ('designer', 'reviewer'):
        raise RoleRefused('unknown role')
    role = role_text
    with _lock(design_directory(design_id)):
        design = read_design(design_id)
        _check_host(design)
        instance = _instance(design, role)
        if isinstance(instance.lifecycle, RoleEnded):
            raise RoleRefused('role ended; this design identity cannot be reopened.')
        lifecycle = instance.lifecycle
        if isinstance(lifecycle, RoleNotOpened):
            if lifecycle.creation == 'not_started':
                raise RoleRefused('startup has not started; repeat the original open command')
            membership = staff.snapshot()
            parent = worker_admission.parent(membership, staff_id=staff.runtime_id(),
                                              run=Path(design.binding.run_directory) if isinstance(design.binding, DesignBoundToPhase) else design_directory(design_id))
            member = worker_admission.find_worker(membership, parent.id, instance.name)
            if member is None:
                raise RoleRefused('RoleNeedsRecovery: creation outcome unknown; no recorded worker; replacement refused; run end to clear the claim')
            lifecycle = RoleOpen(member.id)
        observed = replace(design, roles=tuple(replace(value, assignment_claim='') if value.role == role else value
                                               for value in design.roles))
        _pane_recovery_idle(lifecycle)
        updated = _set_lifecycle(observed, role, RoleOpen(lifecycle.staff_id))
        _save(updated)
        _project_hold(updated, role, staff.snapshot())
        return updated


def resume(instance_id: str, prompt_file: str | Path) -> Path:
    design_id, role_text = instance_id.split('/')
    if role_text not in ('designer', 'reviewer'):
        raise RoleRefused('unknown role')
    role = role_text
    directory = design_directory(design_id)
    observed = _instance(read_design(design_id), role)
    if isinstance(observed.lifecycle, RoleNeedsRecovery) or observed.assignment_claim:
        _ = recover(instance_id)
    with _lock(directory):
        design = read_design(design_id)
        _check_host(design)
        instance = _instance(design, role)
        if not isinstance(instance.lifecycle, (RoleOpen, RolePausedForMove)):
            raise RoleRefused(type(instance.lifecycle).__name__ + ': role is unavailable')
        if (directory / 'transition.json').exists():
            raise RoleRefused('RoleNeedsRecovery: binding transition is incomplete')
        _idle(design)
        _validate_binding(design.binding, design.repository, role)
        generation = design.placement.generation
    prompt, reply = _prompt(design, role, Path(prompt_file).read_text(encoding='utf-8'))
    with _lock(directory):
        current = read_design(design_id)
        if current.placement.generation != generation:
            raise RoleRefused('binding changed before dispatch')
        _idle(current)
        if _instance(current, role).lifecycle != instance.lifecycle:
            raise RoleRefused('role changed before dispatch')
        if (directory / 'transition.json').exists():
            raise RoleRefused('binding transition is incomplete')
        current = replace(current, roles=tuple(replace(value, assignment_claim=str(prompt)) if value.role == role else value
                                               for value in current.roles))
        _save(current)

    try:
        dispatch_turn(design, role, prompt, reply)
    except BaseException as error:
        with _lock(directory):
            current = read_design(design_id)
            _save(_set_lifecycle(replace(current, roles=tuple(replace(value, assignment_claim='') if value.role == role else value
                                                          for value in current.roles)), role,
                                     RoleNeedsRecovery(instance.lifecycle.staff_id, str(error))))
        raise
    with _lock(directory):
        current = read_design(design_id)
        _save(_set_lifecycle(replace(current, roles=tuple(replace(value, assignment_claim='') if value.role == role else value
                                                      for value in current.roles)), role, RoleOpen(instance.lifecycle.staff_id)))
        identifiers = cast(list[str], worker_lifecycle.read_worker_value(prompt.with_suffix('.messages.json')))
        for identifier in identifiers:
            (directory / 'pending' / role / f'{identifier}.json').unlink(missing_ok=True)
    _project_hold(design, role, staff.snapshot())
    return reply


def rebind(design_id: str, *, expected_generation: int, reason: str,
           binding: Binding | KeepBinding = KEEP_BINDING,
           repository: str = '', document: str = '') -> HeldDesign:
    if not reason.strip():
        raise RoleRefused('rebind needs a reason')
    directory = design_directory(design_id)
    design = _load_design(design_id, repair_empty_repository=not isinstance(binding, KeepBinding))
    _check_host(design)
    transition = directory / 'transition.json'
    with _lock(directory):
        fields = _read(directory / 'design.json')
        design = _decode_design(design_id, fields, repair_empty_repository=not isinstance(binding, KeepBinding))
        damaged_binding = _object(fields.get('binding')).get('repository') == ''
        target = replace(design, binding=design.binding if isinstance(binding, KeepBinding) else binding,
                         repository=repository or design.repository, document=document or design.document)
        # A target must pass the same rules as a saved record before lifecycle effects.
        _ = _decode_design(design_id, _design_fields(target))
        updated = replace(target, placement=replace(target.placement, generation=expected_generation + 1))
        pending = _read(transition) if transition.exists() else {}
        if pending and (pending.get('reason') != reason or pending.get('generation') != expected_generation):
            raise RoleRefused('another lifecycle transition is incomplete')
        committed_retry = bool(pending and design.placement.generation == expected_generation + 1
                               and pending.get('target') == _design_fields(design)
                               and target == design)
        publication_retry = committed_retry or (not damaged_binding and not isinstance(binding, KeepBinding) and target == design and expected_generation >= 1
                             and expected_generation in
                             (design.placement.generation, design.placement.generation - 1))
        if publication_retry:
            if pending and (design.placement.generation != expected_generation + 1
                            or pending.get('target', _design_fields(design)) != _design_fields(design)):
                raise RoleRefused('another lifecycle transition is incomplete')
            context = mesh_directory(design_id) / 'design_context.json'
            if (not pending and context.exists() and _read(context) == _mesh_context_fields(design)
                    and {role.lifecycle.staff_id for role in design.roles
                         if not isinstance(role.lifecycle, (RoleNotOpened, RoleEnded))}.issubset(
                             entry['staff_id'] for entry in _census_projection().live_roles(staff.snapshot()))):
                return design
        else:
            if design.placement.generation != expected_generation:
                raise RoleRefused('stale binding generation')
            fields = _design_fields(updated)
            if pending and pending.get('target', fields) != fields:
                raise RoleRefused('another lifecycle transition is incomplete')
            _idle(design)
            _atomic(transition, {'generation': expected_generation, 'reason': reason, 'target': fields,
                                 'source_binding': _tag(design.binding)})
    if publication_retry:
        return _finish_rebind(design)
    _ = authority_request('advance', {'repository_id': design.repository_id, 'document': design.document,
                                      'host': local_host(), 'expected_generation': expected_generation})
    with _lock(directory):
        current = _load_design(design_id, repair_empty_repository=not isinstance(binding, KeepBinding))
        if current != design:
            raise RoleRefused('binding changed while releasing server')
        _save(updated)
    return _finish_rebind(updated)


def _finish_rebind(design: HeldDesign) -> HeldDesign:
    """Keep the transition until the saved generation is fully published."""
    directory = design_directory(design.placement.design_id)
    with _lock(directory):
        if read_design(design.placement.design_id) != design:
            raise RoleRefused('binding changed during publication')
        _mesh_context(design)
        transition = directory / 'transition.json'
        pending = _read(transition) if transition.exists() else {}
        if pending.get('reset_routes'):
            with _edit_roster(design.placement.design_id) as roster:
                for value in roster.values():
                    entry = _object(value)
                    for key in ('authorized_prompt', 'authorized_holder', 'authorized_generation'):
                        _ = entry.pop(key, None)
    record = staff.snapshot()
    for instance in design.roles:
        _project_hold(design, instance.role, record)
    with _lock(directory):
        if read_design(design.placement.design_id) != design:
            raise RoleRefused('binding changed during publication')
        if (_read(transition) if transition.exists() else {}) != pending:
            raise RoleRefused('lifecycle transition changed during publication')
        transition.unlink(missing_ok=True)
    return design


def queue_message(design_id: str, role: Role, source: str, text: str, *, message_id: str = '') -> PendingMessage:
    identifier = message_id or str(uuid4())
    _ = UUID(identifier)
    message = PendingMessage(identifier, source, text)
    directory = design_directory(design_id)
    with _lock(directory):
        design = read_design(design_id)
        if isinstance(_instance(design, role).lifecycle, RoleEnded):
            raise RoleRefused('role ended')
        path = directory / 'pending' / role / f'{identifier}.json'
        if path.exists() and _read(path) != asdict(message):
            raise RoleRefused('message id already names other text')
        _atomic(path, asdict(message))
    return message


def pending_messages(design_id: str, role: Role) -> tuple[PendingMessage, ...]:
    return tuple(PendingMessage(_text(value, 'id'), _text(value, 'source'), _text(value, 'text'))
                 for path in sorted((design_directory(design_id) / 'pending' / role).glob('*.json'))
                 for value in [_read(path)])


def end(instance_id: str) -> HeldDesign:
    design_id, role_text = instance_id.split('/')
    if role_text not in ('designer', 'reviewer'):
        raise RoleRefused('unknown role: ' + role_text)
    role = role_text
    design = read_design(design_id)
    _check_host(design)
    instance = _instance(design, role)
    if isinstance(instance.lifecycle, RoleEnded):
        _finish_end(design, role)
        return design
    directory = design_directory(design_id)
    unknown_creation = (isinstance(instance.lifecycle, RoleNotOpened)
                        and instance.lifecycle.creation == 'outcome_unknown')
    with _lock(directory):
        current = read_design(design_id)
        if current != design:
            raise RoleRefused('role changed before end')
        # Only this role's uncertain creation claim permits explicit end.
        _idle(replace(current, roles=tuple(
            replace(value, lifecycle=RoleNotOpened()) if unknown_creation and value.role == role else value
            for value in current.roles)))
        _atomic(directory / 'transition.json', {'generation': current.placement.generation, 'reason': 'explicit end', 'role': role})
    run = Path(design.binding.run_directory) if isinstance(design.binding, DesignBoundToPhase) else directory
    if unknown_creation:
        membership = staff.snapshot()
        parent = worker_admission.parent(membership, staff_id=staff.runtime_id(), run=run)
        member = worker_admission.find_worker(membership, parent.id, instance.name)
        if member is not None:
            instance = replace(instance, lifecycle=RoleOpen(member.id))
            design = _set_lifecycle(design, role, instance.lifecycle)
    arguments = ['end', instance.name, '--session-dir', str(run)]
    if not isinstance(instance.lifecycle, RoleNotOpened):
        arguments.extend(('--parent', str(worker_parent(design, role, staff.snapshot()).id)))
    result = _worker_command(arguments)
    if result.returncode:
        raise RoleRefused(result.stderr.strip() or f'held worker end exited {result.returncode}; retry end')
    with _lock(directory):
        updated = _set_lifecycle(read_design(design_id), role, RoleEnded(
            instance.lifecycle.staff_id if not isinstance(instance.lifecycle, RoleNotOpened) else staff.StaffId('')))
        _save(updated)
    _finish_end(updated, role)
    return updated


def _finish_end(design: HeldDesign, role: Role) -> None:
    """Release the census before clearing this role's end fence."""
    lifecycle = _instance(design, role).lifecycle
    if isinstance(lifecycle, HeldWorker) and lifecycle.staff_id:
        _census_projection().release(lifecycle.staff_id)
    directory = design_directory(design.placement.design_id)
    with _lock(directory):
        transition = directory / 'transition.json'
        pending = _read(transition) if transition.exists() else {}
        if pending.get('reason') == 'explicit end' and pending.get('role') == role:
            transition.unlink(missing_ok=True)


def list_roles() -> dict[str, object]:
    entries: list[dict[str, object]] = []
    problems: list[dict[str, str]] = []
    for path in sorted(state_root().glob('*/design.json')):
        try:
            design = read_design(path.parent.name)
            for role in design.roles:
                lifecycle = role.lifecycle
                transition = path.parent / 'transition.json'
                if transition.exists() and not isinstance(lifecycle, (RoleNotOpened, RoleEnded)):
                    lifecycle = RoleNeedsRecovery(lifecycle.staff_id, 'incomplete ' + str(_read(transition).get('reason')))
                if not isinstance(lifecycle, (RoleNotOpened, RoleEnded)):
                    entry = _object(_roster(design).get(role.name))
                    if (mesh_directory(design.placement.design_id) / f'{role.name}.account-move.json').exists():
                        lifecycle = RolePausedForMove(lifecycle.staff_id)
                    elif entry.get('status') == 'failed':
                        lifecycle = RoleNeedsRecovery(lifecycle.staff_id, 'mesh turn failed')
                entries.append({'design_id': design.placement.design_id, 'instance_id': design.placement.design_id + '/' + role.role,
                                'role': role.role, 'name': role.name, 'lifecycle': _tag(lifecycle),
                                'binding': _tag(design.binding), 'generation': design.placement.generation,
                                'document': design.document, 'host': design.placement.host})
        except (OSError, ValueError, KeyError, TypeError) as error:
            problems.append({'path': str(path), 'reason': str(error)})
    return {'roles': entries, 'problems': problems}


def render_roles(report: dict[str, object]) -> str:
    lines: list[str] = []
    for entry in cast(list[dict[str, object]], report['roles']):
        lifecycle = _object(entry['lifecycle'])
        thread = ' | staff ' + _text(lifecycle, 'staff_id') if lifecycle.get('staff_id') else ''
        lines.append(f"{entry['instance_id']} | {entry['role']} | {_text(lifecycle, 'kind')} | "
                     + f"{thread} | {entry['document']}")
    return '\n'.join(lines) if lines else 'no held roles'


def _resolved_binding(binding: Binding, repository: str) -> Binding:
    if isinstance(binding, DesignBoundToPhase) and not binding.repository:
        return replace(binding, repository=repository)
    return binding


def _has_live_roles(fields: dict[str, object]) -> bool:
    roles = fields.get('roles')
    if not isinstance(roles, list):
        return False
    return any(isinstance(value, dict)
               and isinstance(lifecycle := cast(dict[str, object], value).get('lifecycle'), dict)
               and cast(dict[str, object], lifecycle).get('kind') in ('RoleOpen', 'RolePausedForMove', 'RoleNeedsRecovery')
               for value in cast(list[object], roles))


@dataclass
class _RecordWarnings:
    paths: set[Path]


_record_warnings: ContextVar[_RecordWarnings] = ContextVar('design_role_record_warnings')


@contextmanager
def record_operation() -> Generator[None]:
    """Report each bad record once across this operation's nested sweeps."""
    try:
        _ = _record_warnings.get()
    except LookupError:
        token = _record_warnings.set(_RecordWarnings(set()))
        try:
            yield
        finally:
            _record_warnings.reset(token)
    else:
        yield


def _read_designs(relevant: Callable[[dict[str, object]], bool], *,
                  refuse_matching_damage: bool = False) -> Generator[HeldDesign]:
    """Skip broken records, but preserve a caller's matching live role for recovery."""
    with record_operation():
        warned = _record_warnings.get().paths
        for path in sorted(state_root().glob('*/design.json')):
            fields: dict[str, object] = {}
            try:
                fields = _read(path)
                design = _decode_design(path.parent.name, fields)
            except (OSError, ValueError, KeyError, TypeError) as error:
                reason = ' '.join(str(error).splitlines())
                live_roles = _has_live_roles(fields)
                if relevant(fields) and (refuse_matching_damage or live_roles):
                    recovery = '; live held role needs recovery' if live_roles else ''
                    raise RoleRefused(f'{path}: {reason}{recovery}') from error
                if path not in warned:
                    print(f'warning: {path}: {reason}', file=sys.stderr)
                    warned.add(path)
                continue
            yield design


@dataclass(frozen=True)
class NotHeldDocument:
    pass


def held_document(document: Path) -> HeldDesign | NotHeldDocument:
    target = str(document.resolve())
    found = [design for design in _read_designs(lambda fields: fields.get('document') == target)
             if design.document == target]
    if len(found) > 1:
        raise RoleRefused('document has more than one held identity; recovery required')
    return found[0] if found else NotHeldDocument()


def unbind_run(run_directory: str) -> None:
    def matches_run(binding: object) -> bool:
        if not isinstance(binding, dict):
            return False
        directory = cast(dict[str, object], binding).get('run_directory')
        return isinstance(directory, str) and Path(directory) == Path(run_directory)

    def relevant(fields: dict[str, object]) -> bool:
        return matches_run(fields.get('binding'))

    for design in _read_designs(relevant, refuse_matching_damage=True):
        transition = design_directory(design.placement.design_id) / 'transition.json'
        pending = _read(transition) if transition.exists() else {}
        source_binding = pending.get('source_binding')
        if (matches_run(source_binding)
                and pending.get('target') == _design_fields(design)):
            _ = _finish_rebind(design)
        if relevant(_design_fields(design)):
            _ = rebind(design.placement.design_id, expected_generation=design.placement.generation,
                       reason='run ended after reservation release', binding=DesignUnbound())


def check_patch(design_id: str, generation: int, cwd: Path, paths: list[str]) -> DesignBoundToPhase:
    """Reject the whole patch before attribution, including symlink and rename escapes."""
    design = read_design(design_id)
    if (design_directory(design_id) / 'transition.json').exists():
        raise RoleRefused('binding transition is incomplete')
    if design.placement.generation != generation:
        raise RoleRefused('stale binding generation')
    _check_host(design)
    if not isinstance(design.binding, DesignBoundToPhase):
        raise RoleRefused('designer is unbound')
    binding = design.binding
    _validate_binding(binding, design.repository, 'designer')
    root = Path(binding.repository).resolve()
    allowed = {(root / name).resolve() for name in binding.output_paths}
    for path in paths:
        candidate = (cwd / path).resolve()
        if candidate not in allowed:
            raise RoleRefused('unlisted or escaping output path: ' + path)
        try:
            _ = candidate.relative_to(root)
        except ValueError as error:
            raise RoleRefused('output path escapes repository: ' + path) from error
    from scripts.direct import phase_reservation
    ledger = phase_reservation.phase_directory(Path(binding.run_directory), binding.phase) / phase_reservation.STATE_FILE
    _ = _read(ledger)
    return binding


def move_checkout(old_checkout: str, new_checkout: str, *,
                reason: str) -> None:
    """Move live document paths and preserve saved prompts and replies."""
    if not old_checkout or old_checkout == new_checkout:
        return

    def moved(value: str) -> str:
        try:
            return str(Path(new_checkout) / Path(value).relative_to(old_checkout))
        except ValueError:
            return value

    for design in _read_designs(lambda fields: fields.get('repository') == old_checkout, refuse_matching_damage=True):
        if design.repository not in (old_checkout, new_checkout):
            continue
        directory = design_directory(design.placement.design_id)
        transition = directory / 'transition.json'
        pending = _read(transition) if transition.exists() else {}
        request = {'old_checkout': old_checkout, 'new_checkout': new_checkout, 'reason': reason}
        if design.repository == new_checkout:
            if all(pending.get(key) == value for key, value in request.items()) and pending.get('target') == _design_fields(design):
                _ = _finish_rebind(design)
            continue
        binding = design.binding
        if isinstance(binding, DesignBoundToPhase):
            binding = replace(binding, repository=moved(binding.repository), run_directory=moved(binding.run_directory),
                              output_paths=tuple(moved(value) for value in binding.output_paths))
        updated = replace(design, repository=new_checkout, document=moved(design.document), binding=binding,
                          placement=replace(design.placement, generation=design.placement.generation + 1))
        fields = _design_fields(updated)
        mesh = mesh_directory(design.placement.design_id)
        with _lock(directory), worker_lifecycle.membership_operation(mesh), worker_lifecycle.lock_workers(
                *(worker_lifecycle.WorkerIdentity(mesh, name) for name in _roster(design))):
            if read_design(design.placement.design_id) != design:
                raise RoleRefused(f'binding changed before {reason}')
            _idle(design)
            if pending and (pending.get('target') != fields or any(pending.get(key) != value for key, value in request.items())):
                raise RoleRefused('another lifecycle transition is incomplete')
            _atomic(transition, {**request, 'generation': design.placement.generation, 'target': fields, 'reset_routes': True})
        _ = authority_request('checkout', {'repository_id': design.repository_id, 'host': local_host(),
                                          'old_checkout': old_checkout, 'new_checkout': new_checkout})
        _ = authority_request('advance', {'repository_id': design.repository_id, 'document': updated.document,
                                         'host': local_host(), 'expected_generation': design.placement.generation})
        with _lock(directory):
            if read_design(design.placement.design_id) != design:
                raise RoleRefused(f'binding changed during {reason}')
            _save(updated)
        _ = _finish_rebind(updated)


def checkout_failures(old_checkout: str) -> list[str]:
    """List live document paths that still use the old checkout."""
    failures: list[str] = []
    try:
        for design in _read_designs(lambda fields: fields.get('repository') == old_checkout):
            path = design_directory(design.placement.design_id) / 'design.json'
            try:
                if old_checkout:
                    values = [design.repository, design.document]
                    if isinstance(design.binding, DesignBoundToPhase):
                        values.extend((design.binding.repository, design.binding.run_directory,
                                       *design.binding.output_paths))
                    if any(value == old_checkout or value.startswith(old_checkout + '/') for value in values):
                        failures.append(str(path) + ': live binding still names old checkout')
                context_path = mesh_directory(design.placement.design_id) / 'design_context.json'
                if context_path.exists():
                    context = _read(context_path)
                    if context.get('generation') != design.placement.generation:
                        failures.append(str(context_path) + ': stale launch context')
            except (OSError, ValueError) as error:
                failures.append(str(path) + ': ' + str(error))
    except RoleRefused as error:
        failures.append(str(error))
    return failures


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    p = commands.add_parser('open')
    for flag in ('repository', 'document', 'role', 'area'):
        _ = p.add_argument('--' + flag, required=True)
    _ = p.add_argument('--host', default='')
    _ = p.add_argument('--run-directory', default='')
    _ = p.add_argument('--phase', default='')
    _ = p.add_argument('--output-path', action='append', default=[])
    _ = p.add_argument('--checkout', default='')
    p = commands.add_parser('resume')
    _ = p.add_argument('instance_id')
    _ = p.add_argument('--prompt-file', required=True)
    p = commands.add_parser('end')
    _ = p.add_argument('instance_id')
    p = commands.add_parser('recover')
    _ = p.add_argument('instance_id')
    p = commands.add_parser('rebind')
    _ = p.add_argument('design_id')
    _ = p.add_argument('--expected-generation', type=int, required=True)
    _ = p.add_argument('--reason', required=True)
    _ = p.add_argument('--unbind', action='store_true')
    for flag in ('run-directory', 'phase', 'checkout', 'document'):
        _ = p.add_argument('--' + flag, default='')
    _ = p.add_argument('--output-path', action='append', default=[])
    p = commands.add_parser('unbind-run')
    _ = p.add_argument('run_directory')
    p = commands.add_parser('list')
    _ = p.add_argument('--json', action='store_true')
    p = commands.add_parser('authority')
    _ = p.add_argument('operation')
    p = commands.add_parser('register')
    _ = p.add_argument('--repository', required=True)
    _ = p.add_argument('--host', required=True)
    _ = p.add_argument('--checkout', required=True)
    args = cast(dict[str, object], vars(parser.parse_args(argv)))
    def argument(key: str) -> str:
        return cast(str, args.get(key, ''))
    try:
        command = argument('command')
        if command == 'open':
            role = argument('role')
            if role not in ('designer', 'reviewer'):
                raise RoleRefused('unknown role')
            binding: Binding = UNBOUND
            if argument('run_directory') or argument('phase'):
                if not argument('run_directory') or not argument('phase'):
                    raise RoleRefused('run-directory and phase are required together')
                binding = DesignBoundToPhase(str(Path(argument('run_directory')).resolve()), argument('phase'),
                                             argument('checkout'), tuple(cast(list[str], args['output_path'])))
            design = open_role(argument('repository'), argument('document'), role, argument('area'),
                               host=argument('host'), binding=binding)
            print(design.placement.design_id + '/' + role)
        elif command == 'resume':
            print(resume(argument('instance_id'), argument('prompt_file')))
        elif command == 'rebind':
            repair = bool(args.get('unbind') or (argument('run_directory') and argument('phase')
                                                and argument('checkout')))
            current = _load_design(argument('design_id'), repair_empty_repository=repair)
            next_binding = current.binding
            if args.get('unbind'):
                next_binding = DesignUnbound()
            elif argument('run_directory') or argument('phase'):
                if not argument('run_directory') or not argument('phase'):
                    raise RoleRefused('run-directory and phase are required together')
                next_binding = DesignBoundToPhase(str(Path(argument('run_directory')).resolve()), argument('phase'),
                                                 argument('checkout') or current.repository,
                                                 tuple(cast(list[str], args['output_path'])) or
                                                 (current.binding.output_paths if isinstance(current.binding, DesignBoundToPhase) else ()))
            updated = rebind(argument('design_id'), expected_generation=cast(int, args['expected_generation']),
                       reason=argument('reason'), binding=next_binding,
                       repository=argument('checkout'), document=argument('document'))
            print(f'{updated.placement.design_id} rebound at generation {updated.placement.generation}')
        elif command == 'end':
            _ = end(argument('instance_id'))
        elif command == 'recover':
            _ = recover(argument('instance_id'))
        elif command == 'unbind-run':
            unbind_run(argument('run_directory'))
        elif command == 'register':
            register_repository(argument('repository'), {argument('host'): argument('checkout')})
        elif command == 'authority':
            config = configparser.ConfigParser()
            _ = config.read(os.environ.get('DESIGN_ROLES_CONFIG') or ROOT / 'config/design_roles.conf')
            if config.get('ownership', 'host') != local_host():
                raise RoleRefused('this host is not the ownership authority')
            print(json.dumps(_authority_local(argument('operation'), _object(cast(object, json.load(sys.stdin))))))
        else:
            report = list_roles()
            for problem in cast(list[dict[str, str]], report['problems']) if not args.get('json') else ():
                print(f"problem: {problem['path']}: {problem['reason']}", file=sys.stderr)
            print(json.dumps(report) if args.get('json') else render_roles(report))
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, configparser.Error) as error:
        damaged = (error.__cause__ if isinstance(error, worker_lifecycle.WorkerStateUnreadable)
                   else error)
        detail = (f'{damaged.path}: missing or invalid {damaged.field}'
                  if isinstance(damaged, DamagedDesignRecord) else str(error))
        print('design_roles: ' + detail.replace('\n', ' '), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
