#!/usr/bin/env python3
"""Keep one designer and reviewer per design, with explicit placement and phase access.

Only the pinned authority changes placements. Local lifecycle actions keep thread identity,
assignment evidence and pending messages when a server or acknowledgement is lost.
"""
from __future__ import annotations

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
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol, cast
from uuid import UUID, uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.astra import astra_work
from scripts.agents import worker_lifecycle
from scripts.lib.atomic_file import write_text_atomic
from scripts.production import staff, session_staff, worker_admission

ROOT = Path(__file__).resolve().parents[2]
Role = Literal['designer', 'reviewer']
BUSY = ('starting', 'running', 'waiting_capacity')


class MeshLifecycleClient(Protocol):
    def call(self, method: str, params: dict[str, object]) -> dict[str, object]: ...
    def close(self) -> None: ...


class UnconfirmedThreadRelease(Protocol):
    reason: str


class RecordedTurnEvidence(Protocol):
    """The transport exposes worker operations."""

    @property
    def model(self) -> str: ...
    @property
    def effort(self) -> str: ...


class MeshLifecycleTransport(Protocol):
    Client: Callable[[int, str], MeshLifecycleClient]
    ThreadIdle: type[object]
    WorkerThreadStateUnknown: type[UnconfirmedThreadRelease]

    def ensure_server(self, session_dir: str, home: Path | None = None) -> tuple[int, bool]: ...
    def _read_live_turn(self, client: MeshLifecycleClient, thread: str) -> object: ...
    def _release_held_server(self, session_dir: str) -> None: ...
    def held_thread_end_confirmed(self, session_dir: str, thread_id: str) -> bool: ...
    def _pending_file(self, session_dir: str, name: str) -> AbstractContextManager[worker_lifecycle.WorkerStateFile]: ...
    def _pending_messages(self, handle: worker_lifecycle.WorkerStateFile) -> list[str]: ...
    def _write_pending(self, handle: worker_lifecycle.WorkerStateFile, messages: list[str]) -> None: ...
    def _move_queue_to_pending(self, client: MeshLifecycleClient, thread: str, park: Callable[[str], None]) -> None: ...
    def _release_worker_thread(self, client: MeshLifecycleClient, thread: str, session_dir: str) -> object: ...
    def recorded_turn_settings(self, record: Mapping[str, object]) -> RecordedTurnEvidence: ...


class AstraCensusProjection(Protocol):
    def hold(self, role: Literal['designer', 'reviewer'], staff_id: str, *,
             design_id: str, role_id: str) -> object: ...
    def release(self, staff_id: str) -> None: ...
    def live_roles(self) -> Sequence[Mapping[str, object]]: ...


def _mesh_transport() -> MeshLifecycleTransport:
    # The launcher also reads this store. Import only its transport at the runtime boundary.
    return cast(MeshLifecycleTransport, cast(object, importlib.import_module('scripts.agents.codex_mesh')))


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
class PhaseBoundDesigner:
    binding_generation: int
    launch_sandbox: str


@dataclass(frozen=True)
class ReadOnlyReviewer:
    pass


ThreadAccessPolicy = PhaseBoundDesigner | ReadOnlyReviewer


@dataclass(frozen=True)
class RoleNotOpened:
    claim: str = ''


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
        return RoleNotOpened(claim)
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


def resolve_model(role: Role) -> tuple[str, str]:
    result = subprocess.run(['bash', '-c',
                             'source "$1"; agents_resolve "design_roles.$2" || exit; ' +
                             '[[ "$AGENT_FAMILY" == codex ]] || exit 1; ' +
                             'printf "%s\\n%s\\n" "$AGENT_MODEL" "$AGENT_EFFORT"',
                             'design-role-model', str(ROOT / 'scripts/agents/models_config.sh'), role],
                            capture_output=True, text=True, check=False, timeout=15,
                            env={**os.environ, 'MODELS_CONFIG_TIER_ONLY': '1'})
    values = result.stdout.strip().splitlines()
    if result.returncode or len(values) != 2 or not all(values):
        raise RoleRefused('invalid design-role model configuration: ' + result.stderr.strip())
    return values[0], values[1]


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
    with prompt.open('x', encoding='utf-8') as output:
        _ = output.write(astra_work.header_line(header) + '\n' + instructions + '\n' + text + '\n' + message_text)
    _atomic(prompt.with_suffix('.messages.json'), [message.id for message in pending])
    return prompt, reply


def access_policy(design: HeldDesign, role: Role) -> ThreadAccessPolicy:
    return PhaseBoundDesigner(design.placement.generation, 'danger-full-access') if role == 'designer' else ReadOnlyReviewer()


def _mesh_context(design: HeldDesign) -> None:
    _atomic(mesh_directory(design.placement.design_id) / 'design_context.json', _mesh_context_fields(design))


def _mesh_context_fields(design: HeldDesign) -> dict[str, object]:
    return {'design_id': design.placement.design_id, 'generation': design.placement.generation,
            'binding': _design_fields(design)['binding'], 'cwd': design.repository}


def launch_thread(design: HeldDesign, role: Role, prompt: Path, model: str, effort: str, *,
                  admitted: worker_admission.WorkerStartAllowed) -> str:
    """Start the reserved thread and keep its creation claim if the result is uncertain."""
    mesh = mesh_directory(design.placement.design_id)
    mesh.mkdir(parents=True, exist_ok=True)
    name = admitted.name
    if name != _instance(design, role).name:
        raise RoleRefused("staff admission names another worker")
    result = subprocess.run(['bash', str(ROOT / 'scripts/direct/assign.sh'),
                             '--role', 'implement' if role == 'designer' else 'review', '--operation', 'prime',
                             '--session-dir', str(mesh), '--new-worker', name, '--cwd', design.repository,
                             '--prompt', str(prompt), '--model', model, '--effort', effort,
                             '--held-design', design.placement.design_id, '--held-role', role],
                            capture_output=True, text=True, check=False)
    if result.returncode:
        raise RoleRefused('RoleNeedsRecovery: thread creation not confirmed: ' + result.stderr.strip())
    roster = _read(mesh / 'mesh_roster.json')
    entry = _object(roster.get(name))
    return session_staff.worker_session(entry)


def dispatch_turn(design: HeldDesign, role: Role, prompt: Path, reply: Path, model: str, effort: str) -> None:
    """Submit one turn outside store locks and save its reply at the header's path."""
    mesh = mesh_directory(design.placement.design_id)
    result = subprocess.run(['bash', str(ROOT / 'scripts/direct/assign.sh'),
                             '--role', 'implement' if role == 'designer' else 'review', '--operation', 'follow',
                             '--session-dir', str(mesh), '--to', _instance(design, role).name,
                             '--cwd', design.repository, '--prompt', str(prompt),
                             '--summary', str(reply), '--log', str(reply.with_suffix('.log')),
                             '--model', model, '--effort', effort,
                             '--held-design', design.placement.design_id, '--held-role', role], check=False)
    if result.returncode:
        raise RoleRefused('RoleNeedsRecovery: held turn did not complete; pending input is retained')


def _project_hold(design: HeldDesign, role: Role, record: staff.StaffSnapshot) -> None:
    astra_roles = _census_projection()
    instance = _instance(design, role)
    if isinstance(instance.lifecycle, (RoleNotOpened, RoleEnded)):
        return
    _ = worker_parent(design, role, record)
    _ = astra_roles.hold(role, instance.lifecycle.staff_id,
                         design_id=design.placement.design_id, role_id=role)


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
        if not isinstance(instance.lifecycle, RoleNotOpened) or instance.lifecycle.claim:
            if not isinstance(instance.lifecycle, RoleNotOpened):
                _project_hold(design, role, staff.snapshot())
            return design
        _validate_binding(design.binding, design.repository, role)
    model, effort = resolve_model(role)
    prompt, _ = _prompt(design, role, '')
    admitted = worker_admission.admit(
        instance.name, mesh_directory(design_id), role="astra-" + role)
    with _lock(directory):
        current = read_design(design_id)
        instance = _instance(current, role)
        if not isinstance(instance.lifecycle, RoleNotOpened) or instance.lifecycle.claim:
            return current
        if current.placement != design.placement or current.binding != design.binding:
            raise RoleRefused('binding changed before thread creation')
        claim = str(uuid4())
        design = _set_lifecycle(current, role, RoleNotOpened(claim))
        _save(design)
        _mesh_context(design)
    # A failed or uncertain launch keeps its reserved claim; it cannot authorize replacement.
    thread = launch_thread(design, role, prompt, model, effort, admitted=admitted)
    with _lock(directory):
        current = read_design(design_id)
        if _instance(current, role).lifecycle != RoleNotOpened(claim):
            raise RoleRefused('RoleNeedsRecovery: thread creation claim changed')
        design = _set_lifecycle(current, role, RoleOpen(admitted.slot.row.id if isinstance(admitted.slot, worker_admission.ExistingWorkerSlot) else staff.native_id(thread)))
        _save(design)
    _project_hold(design, role, staff.snapshot())
    return design


def recover(instance_id: str) -> HeldDesign:
    """Prove the recorded thread idle before clearing an uncertain local claim."""
    codex_mesh = _mesh_transport()
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
        with worker_lifecycle.membership_operation(mesh_directory(design_id)), worker_lifecycle.lock_worker(mesh_directory(design_id), instance.name):
            entry = _object(_roster(design).get(instance.name))
            thread = session_staff.worker_session(entry)
            if not isinstance(instance.lifecycle, RoleNotOpened) and instance.lifecycle.thread_id != thread:
                raise RoleRefused('RoleNeedsRecovery: recorded thread differs; replacement refused')
            launcher = entry.get('launcher_pid')
            if isinstance(launcher, int) and launcher > 0:
                try:
                    os.kill(launcher, 0)
                except ProcessLookupError:
                    pass
                else:
                    raise RoleRefused('role launcher is still active')
            mesh = mesh_directory(design_id)
            port, _ = codex_mesh.ensure_server(str(mesh))
            client = codex_mesh.Client(port, 'held-role-recovery')
            try:
                reply = client.call('thread/resume', {'threadId': thread})
                if reply.get('error'):
                    raise RoleRefused('RoleNeedsRecovery: recorded thread cannot be resumed; replacement refused')
                if _object(_object(reply.get('result')).get('thread')).get('id') != thread:
                    raise RoleRefused('RoleNeedsRecovery: resumed thread identity differs; replacement refused')
                state = codex_mesh._read_live_turn(client, thread)  # pyright: ignore[reportPrivateUsage]
                if not isinstance(state, codex_mesh.ThreadIdle):
                    raise RoleRefused('RoleNeedsRecovery: recorded thread is not proven idle')
            finally:
                client.close()
            current = read_design(design_id)
            if current != design:
                raise RoleRefused('role changed during recovery')
            with _edit_roster(design_id) as roster:
                current_entry = _object(roster.get(instance.name))
                if current_entry != entry:
                    raise RoleRefused('roster changed during recovery')
                current_entry.update({'status': 'done', 'launcher_pid': 0})
                roster[instance.name] = current_entry
                updated = _set_lifecycle(replace(current, roles=tuple(replace(value, assignment_claim='') if value.role == role else value
                                                                    for value in current.roles)), role, RoleOpen(staff.StaffId(str(entry.get("staff_id", "")))))
                _save(updated)
        refresh_census(design_id)
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
    model, effort = resolve_model(role)
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
        with worker_lifecycle.lock_worker(mesh_directory(design_id), instance.name), _edit_roster(design_id) as roster:
            entry = _object(roster.get(instance.name))
            if session_staff.worker_session(entry) != instance.lifecycle.thread_id or entry.get('status') in BUSY:
                raise RoleRefused('RoleNeedsRecovery: roster thread or idle state changed')
            entry.update({'authorized_prompt': str(prompt), 'authorized_generation': generation,
                          'access_policy': _tag(access_policy(current, role)),
                          'held_design_id': design_id, 'held_role': role})
            roster[instance.name] = entry
            current = replace(current, roles=tuple(replace(value, assignment_claim=str(prompt)) if value.role == role else value
                                                   for value in current.roles))
            _save(current)
            _mesh_context(current)

    try:
        dispatch_turn(design, role, prompt, reply, model, effort)
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


def release_servers(design: HeldDesign) -> None:
    """Release only proven-idle threads; queue parking precedes server cleanup."""
    codex_mesh = _mesh_transport()
    codex_mesh._release_held_server(str(mesh_directory(design.placement.design_id)))  # pyright: ignore[reportPrivateUsage]


def rebind(design_id: str, *, expected_generation: int, reason: str,
           binding: Binding | None = None,
           repository: str = '', document: str = '') -> HeldDesign:
    if not reason.strip():
        raise RoleRefused('rebind needs a reason')
    directory = design_directory(design_id)
    design = _load_design(design_id, repair_empty_repository=binding is not None)
    _check_host(design)
    transition = directory / 'transition.json'
    with _lock(directory):
        fields = _read(directory / 'design.json')
        design = _decode_design(design_id, fields, repair_empty_repository=binding is not None)
        damaged_binding = _object(fields.get('binding')).get('repository') == ''
        target = replace(design, binding=binding if binding is not None else design.binding,
                         repository=repository or design.repository, document=document or design.document)
        # A target must pass the same rules as a saved record before lifecycle effects.
        _ = _decode_design(design_id, _design_fields(target))
        updated = replace(target, placement=replace(target.placement, generation=expected_generation + 1))
        pending = _read(transition) if transition.exists() else {}
        if pending and (pending.get('reason') != reason or pending.get('generation') != expected_generation):
            raise RoleRefused('another lifecycle transition is incomplete')
        publication_retry = (not damaged_binding and binding is not None and target == design and expected_generation >= 1
                             and expected_generation in
                             (design.placement.generation, design.placement.generation - 1))
        if publication_retry:
            if pending and (design.placement.generation != expected_generation + 1
                            or pending.get('target', _design_fields(design)) != _design_fields(design)):
                raise RoleRefused('another lifecycle transition is incomplete')
            context = mesh_directory(design_id) / 'design_context.json'
            identities = {role['staff_id'] for role in _census_projection().live_roles()}
            if (not pending and context.exists() and _read(context) == _mesh_context_fields(design)
                    and all(isinstance(role.lifecycle, (RoleNotOpened, RoleEnded))
                            or role.lifecycle.staff_id in identities for role in design.roles)):
                return design
        else:
            if design.placement.generation != expected_generation:
                raise RoleRefused('stale binding generation')
            fields = _design_fields(updated)
            if pending and pending.get('target', fields) != fields:
                raise RoleRefused('another lifecycle transition is incomplete')
            _idle(design)
            _atomic(transition, {'generation': expected_generation, 'reason': reason, 'target': fields})
    if publication_retry:
        return _finish_rebind(design)
    release_servers(design)
    _ = authority_request('advance', {'repository_id': design.repository_id, 'document': design.document,
                                      'host': local_host(), 'expected_generation': expected_generation})
    with _lock(directory):
        current = _load_design(design_id, repair_empty_repository=binding is not None)
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
    record = staff.snapshot()
    for instance in design.roles:
        _project_hold(design, instance.role, record)
    with _lock(directory):
        if read_design(design.placement.design_id) != design:
            raise RoleRefused('binding changed during publication')
        (directory / 'transition.json').unlink(missing_ok=True)
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


def _retire_role(design: HeldDesign, lifecycle: HeldWorker) -> None:
    with worker_lifecycle.membership_operation(mesh_directory(design.placement.design_id)):
        _census_projection().release(lifecycle.staff_id)
        if staff.by_id(lifecycle.staff_id) is not None:
            _ = staff.retire(lifecycle.staff_id)


def end(instance_id: str) -> HeldDesign:
    design_id, role_text = instance_id.split('/')
    if role_text not in ('designer', 'reviewer'):
        raise RoleRefused('unknown role: ' + role_text)
    role = role_text
    design = read_design(design_id)
    _check_host(design)
    instance = _instance(design, role)
    if isinstance(instance.lifecycle, RoleEnded):
        _retire_role(design, instance.lifecycle)
        if all(isinstance(item.lifecycle, (RoleNotOpened, RoleEnded)) for item in design.roles):
            release_servers(design)
        return design
    directory = design_directory(design_id)
    with _lock(directory):
        current = read_design(design_id)
        if current != design:
            raise RoleRefused('role changed before end')
        _idle(current)
        _atomic(directory / 'transition.json', {'generation': current.placement.generation, 'reason': 'explicit end'})
    mesh = mesh_directory(design_id)
    result = subprocess.run([sys.executable, str(ROOT / 'scripts/agents/codex_mesh.py'), 'end',
                             '--session-dir', str(mesh), '--to', instance.name], capture_output=True, text=True, check=False)
    thread = '' if isinstance(instance.lifecycle, RoleNotOpened) else instance.lifecycle.thread_id
    if thread:
        try:
            confirmed = not result.returncode and _mesh_transport().held_thread_end_confirmed(str(mesh), thread)
        except (ConnectionError, OSError, SystemExit):
            confirmed = False
        if not confirmed:
            with _lock(directory):
                _save(_set_lifecycle(read_design(design_id), role,
                                     RoleNeedsRecovery(instance.lifecycle.staff_id if not isinstance(instance.lifecycle, RoleNotOpened) else staff.StaffId(""), 'end cleanup is not confirmed')))
            raise RoleRefused('RoleNeedsRecovery: end cleanup failed; roster end marker is retained')
    if all(item.role == role or isinstance(item.lifecycle, (RoleNotOpened, RoleEnded)) for item in design.roles):
        try:
            release_servers(design)
            _atomic(directory / 'end-server-release.json', {'released_at_utc': datetime.now(UTC).isoformat()})
        except (ConnectionError, OSError, SystemExit, RoleRefused) as error:
            with _lock(directory):
                _save(_set_lifecycle(read_design(design_id), role,
                                     RoleNeedsRecovery(instance.lifecycle.staff_id if not isinstance(instance.lifecycle, RoleNotOpened) else staff.StaffId(""), 'design server release is not confirmed')))
            raise RoleRefused('RoleNeedsRecovery: design server release failed') from error
    with _lock(design_directory(design_id)):
        current = read_design(design_id)
        updated = _set_lifecycle(current, role, RoleEnded(instance.lifecycle.staff_id if not isinstance(instance.lifecycle, RoleNotOpened) else staff.StaffId("")))
        _save(updated)
        (directory / 'transition.json').unlink(missing_ok=True)
    if not isinstance(instance.lifecycle, RoleNotOpened):
        _retire_role(design, instance.lifecycle)
    return updated


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


def held_document(document: Path) -> HeldDesign | None:
    target = str(document.resolve())
    found = [design for design in _read_designs(lambda fields: fields.get('document') == target)
             if design.document == target]
    if len(found) > 1:
        raise RoleRefused('document has more than one held identity; recovery required')
    return found[0] if found else None


def unbind_run(run_directory: str) -> None:
    def relevant(fields: dict[str, object]) -> bool:
        binding = fields.get('binding')
        if not isinstance(binding, dict):
            return False
        directory = cast(dict[str, object], binding).get('run_directory')
        return isinstance(directory, str) and Path(directory) == Path(run_directory)

    for design in _read_designs(relevant, refuse_matching_damage=True):
        if isinstance(design.binding, DesignBoundToPhase) and Path(design.binding.run_directory) == Path(run_directory):
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
    moves_checkout = bool(old_checkout) and old_checkout != new_checkout
    if not moves_checkout:
        return
    for design in _read_designs(lambda fields: fields.get('repository') == old_checkout, refuse_matching_damage=True):
        path = design_directory(design.placement.design_id) / 'design.json'
        if design.repository != old_checkout:
            continue
        _idle(design)
        if moves_checkout:
            _ = authority_request('checkout', {'repository_id': design.repository_id, 'host': local_host(),
                                              'old_checkout': old_checkout, 'new_checkout': new_checkout})
        mesh = mesh_directory(path.parent.name)
        with _lock(path.parent), worker_lifecycle.membership_operation(mesh), worker_lifecycle.lock_workers(
                *(worker_lifecycle.WorkerIdentity(mesh, name) for name in _roster(design))):
            current = read_design(path.parent.name)
            if current != design:
                raise RoleRefused(f'binding changed before {reason}')
            _idle(current)
            _atomic(path.parent / 'transition.json', {'generation': design.placement.generation, 'reason': reason})
        release_servers(design)
        def moved(value: str) -> str:
            if not moves_checkout:
                return value
            try:
                return str(Path(new_checkout) / Path(value).relative_to(old_checkout))
            except ValueError:
                return value
        _ = authority_request('advance', {'repository_id': design.repository_id, 'document': moved(design.document),
                                          'host': local_host(), 'expected_generation': design.placement.generation})
        binding = design.binding
        if isinstance(binding, DesignBoundToPhase):
            binding = replace(binding, repository=moved(binding.repository),
                              run_directory=moved(binding.run_directory),
                              output_paths=tuple(moved(value) for value in binding.output_paths))
        with _lock(path.parent), worker_lifecycle.membership_operation(mesh), worker_lifecycle.lock_workers(
                *(worker_lifecycle.WorkerIdentity(mesh, name) for name in _roster(design))):
            current = read_design(path.parent.name)
            if current.placement.generation != design.placement.generation:
                raise RoleRefused(f'binding changed during {reason}')
            _idle(current)
            updated = replace(current, repository=new_checkout if moves_checkout else current.repository,
                              document=moved(current.document),
                              binding=binding, placement=replace(current.placement, generation=current.placement.generation + 1))
            _save(updated)
            (path.parent / 'transition.json').unlink(missing_ok=True)
            _mesh_context(updated)
            with _edit_roster(path.parent.name) as roster:
                for value in roster.values():
                    entry = _object(value)
                    _ = entry.pop('authorized_prompt', None)
                    _ = entry.pop('authorized_holder', None)
                    _ = entry.pop('authorized_generation', None)


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
