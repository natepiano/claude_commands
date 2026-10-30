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
from collections.abc import Callable, Generator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol, TextIO, cast
from uuid import UUID, uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.astra import astra_work

ROOT = Path(__file__).resolve().parents[2]
Role = Literal['designer', 'reviewer']
BUSY = ('starting', 'running', 'waiting_capacity')


class MeshLifecycleClient(Protocol):
    def call(self, method: str, params: dict[str, object]) -> dict[str, object]: ...
    def close(self) -> None: ...


class UnconfirmedThreadRelease(Protocol):
    reason: str


class MeshLifecycleTransport(Protocol):
    Client: Callable[[int, str], MeshLifecycleClient]
    ThreadIdle: type[object]
    WorkerThreadStateUnknown: type[UnconfirmedThreadRelease]

    def ensure_server(self, session_dir: str, home: Path | None = None) -> tuple[int, bool]: ...
    def _read_live_turn(self, client: MeshLifecycleClient, thread: str) -> object: ...
    def _release_held_server(self, session_dir: str) -> None: ...
    def held_thread_end_confirmed(self, session_dir: str, thread_id: str) -> bool: ...
    def _pending_file(self, session_dir: str, name: str) -> AbstractContextManager[TextIO]: ...
    def _pending_for_thread(self, handle: TextIO, thread: str) -> list[str]: ...
    def _write_pending(self, handle: TextIO, thread: str, messages: list[str]) -> None: ...
    def _move_queue_to_pending(self, client: MeshLifecycleClient, thread: str, park: Callable[[str], None]) -> None: ...
    def _release_worker_thread(self, client: MeshLifecycleClient, thread: str, session_dir: str) -> object: ...


class AstraCensusProjection(Protocol):
    def hold(self, role: Literal['designer', 'reviewer'], thread: str, home: str, *,
             manager: str, design_id: str, role_id: str) -> object: ...
    def release(self, thread: str) -> None: ...


def _mesh_transport() -> MeshLifecycleTransport:
    # The launcher also reads this store. Import only its transport at the runtime boundary.
    return cast(MeshLifecycleTransport, cast(object, importlib.import_module('scripts.agents.codex_mesh')))


def _census_projection() -> AstraCensusProjection:
    return cast(AstraCensusProjection, cast(object, importlib.import_module('scripts.astra.astra_roles')))


@dataclass(frozen=True)
class UnitHolder:
    production_slug: str
    unit_name: str


@dataclass(frozen=True)
class ShowrunnerHolder:
    production_slug: str


Holder = UnitHolder | ShowrunnerHolder


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
class RoleOpen:
    thread_id: str


@dataclass(frozen=True)
class RolePausedForMove:
    thread_id: str


@dataclass(frozen=True)
class RoleNeedsRecovery:
    thread_id: str
    reason: str


@dataclass(frozen=True)
class RoleEnded:
    thread_id: str


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
    holder: Holder
    binding: Binding
    roles: tuple[RoleInstance, ...]


@dataclass(frozen=True)
class PendingMessage:
    id: str
    source: str
    text: str


@dataclass(frozen=True)
class HolderReach:
    session_id: str


@dataclass(frozen=True)
class HolderUnavailable:
    reason: str


class RoleRefused(ValueError):
    pass


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
    return _object(cast(object, json.loads(path.read_text(encoding='utf-8'))))


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
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.{uuid4()}.tmp')
    try:
        _ = temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n', encoding='utf-8')
        _ = temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _lock(directory: Path) -> Generator[None]:
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'lock').open('a', encoding='utf-8') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _tag(value: Holder | Binding | Lifecycle | ThreadAccessPolicy | HolderReach | HolderUnavailable) -> dict[str, object]:
    return {'kind': type(value).__name__, **cast(dict[str, object], asdict(value))}


def _holder(fields: dict[str, object]) -> Holder:
    production = _text(fields, 'production_slug')
    if fields.get('kind') == 'UnitHolder':
        return UnitHolder(production, _text(fields, 'unit_name'))
    if fields.get('kind') == 'ShowrunnerHolder':
        return ShowrunnerHolder(production)
    raise RoleRefused('unknown holder')


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
    thread = fields.get('thread_id') if kind == 'RoleEnded' else _text(fields, 'thread_id')
    if not isinstance(thread, str):
        raise RoleRefused('invalid thread id')
    if kind == 'RoleOpen':
        return RoleOpen(thread)
    if kind == 'RolePausedForMove':
        return RolePausedForMove(thread)
    if kind == 'RoleNeedsRecovery':
        return RoleNeedsRecovery(thread, _text(fields, 'reason'))
    if kind == 'RoleEnded':
        return RoleEnded(thread)
    raise RoleRefused('unknown lifecycle')


def read_design(design_id: str) -> HeldDesign:
    fields = _read(design_directory(design_id) / 'design.json')
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
    return HeldDesign(DesignPlacement(design_id, _text(placement, 'host'), _integer(placement, 'generation')),
                      _text(fields, 'repository_id'), _text(fields, 'repository'), _text(fields, 'document'),
                      _text(fields, 'area'), _text(fields, 'created_at_utc'),
                      _holder(_object(fields.get('holder'))), _binding(_object(fields.get('binding'))), tuple(instances))


def _save(design: HeldDesign) -> None:
    fields = asdict(design)
    fields['schema'] = 1
    fields['holder'] = _tag(design.holder)
    fields['binding'] = _tag(design.binding)
    fields['roles'] = [{'role': role.role, 'name': role.name, 'lifecycle': _tag(role.lifecycle), 'assignment_claim': role.assignment_claim} for role in design.roles]
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


def register_repository(repository_id: str, checkouts: dict[str, str], *, inventory_complete: bool = False) -> None:
    if not repository_id or not checkouts:
        raise RoleRefused('repository id and checkout mappings are required')
    _ = authority_request('register', {'repository_id': repository_id, 'checkouts': checkouts,
                                      'inventory_complete': inventory_complete})


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
            if previous is not None and previous != payload:
                recorded = _object(previous)
                if recorded.get('checkouts') != checkouts or recorded.get('inventory_complete') is True:
                    raise RoleRefused('repository already registered; update its checkout mapping explicitly')
            repositories[repository_id] = payload
            _atomic(path, repositories)
            return payload
        repository = _object(repositories.get(repository_id))
        if repository.get('inventory_complete') is not True:
            raise RoleRefused('legacy inventory incomplete; recovery required')
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
            if operation == 'legacy':
                placement['adoption_required'] = _object(payload.get('source'))
                _atomic(placement_path, placement)
            if operation == 'adopt-reserve':
                adoption = _text(payload, 'adoption_id')
                if placement.get('adoption_id', adoption) != adoption:
                    raise RoleRefused('AdoptionReserved: another adoption owns the placement')
                placement['adoption_id'] = adoption
                if placement.get('adoption_state') == 'completed':
                    return placement
                placement['adoption_state'] = 'AdoptionReserved'
                _atomic(placement_path, placement)
            if operation == 'adopt-commit':
                if placement.get('adoption_id') != payload.get('adoption_id'):
                    raise RoleRefused('adoption claim changed')
                placement['adoption_state'] = 'completed'
                _ = placement.pop('adoption_required', None)
                _atomic(placement_path, placement)
            return placement
        if operation != 'reserve':
            raise RoleRefused('design placement not recorded')
        placement: dict[str, object] = {'design_id': str(uuid4()), 'host': host, 'generation': 1,
                     'request_id': _text(payload, 'request_id'), 'repository_id': repository_id,
                     'document': relative.as_posix(), 'checkout': checkout}
        _atomic(placement_path, placement)
        return placement


def resolve_holder(holder: Holder) -> HolderReach | HolderUnavailable:
    """Resolve current local holder identity; never use a cached session after restart."""
    sys.path.insert(0, str(ROOT / "scripts/production"))
    from scripts.production import unit_lookup
    if isinstance(holder, UnitHolder):
        try:
            marked = unit_lookup.every_marked_unit()
        except OSError as error:
            return HolderUnavailable('unit marks unreadable: ' + str(error))
        unit = marked.units.get(holder.production_slug, {}).get(holder.unit_name)
        if unit is None:
            return HolderUnavailable('unit holder is not recorded')
        if not isinstance(unit.claude, unit_lookup.LiveClaude):
            return HolderUnavailable('unit holder has no live Claude session')
        session = unit.claude.session_id
    else:
        lookup = cast(Callable[[], dict[str, str]],
                      importlib.import_module('scripts.production.production_tree').showrunner_sessions)
        session = lookup().get(holder.production_slug, '')
        from scripts.message import sessions
        if session not in {record['sessionId'] for record in sessions.running_sessions()}:
            return HolderUnavailable('showrunner holder has no live local session')
    if not session:
        return HolderUnavailable('holder session is unavailable')
    return HolderReach(session)


def _authorize(design: HeldDesign) -> HolderReach:
    if design.placement.host != local_host():
        raise RoleRefused('pair host differs from holder host')
    reach = resolve_holder(design.holder)
    if isinstance(reach, HolderUnavailable):
        raise RoleRefused(reach.reason)
    if os.environ.get('CLAUDE_CODE_SESSION_ID') != reach.session_id:
        raise RoleRefused('only the current holder may dispatch or change this design')
    return reach


def resolve_model(role: Role) -> tuple[str, str]:
    result = subprocess.run(['bash', '-c',
                             'source "$1"; agents_resolve "design_roles.$2" || exit; ' +
                             '[[ "$AGENT_FAMILY" == codex ]] || exit 1; ' +
                             'printf "%s\\n%s\\n" "$AGENT_MODEL" "$AGENT_EFFORT"',
                             'design-role-model', str(ROOT / 'scripts/agents/agents_config.sh'), role],
                            capture_output=True, text=True, check=False, timeout=15,
                            env={**os.environ, 'AGENTS_CONFIG_TIER_ONLY': '1'})
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
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+', encoding='utf-8') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        _ = handle.seek(0)
        text = handle.read()
        roster = _object(cast(object, json.loads(text))) if text else {}
        yield roster
        _ = handle.seek(0)
        _ = handle.truncate()
        json.dump(roster, handle, indent=2, sort_keys=True)
        handle.flush()


def _roster(design: HeldDesign) -> dict[str, object]:
    path = mesh_directory(design.placement.design_id) / 'mesh_roster.json'
    return _read(path) if path.exists() else {}


def _idle(design: HeldDesign) -> None:
    if any(instance.assignment_claim or (isinstance(instance.lifecycle, RoleNotOpened) and instance.lifecycle.claim)
           for instance in design.roles):
        raise RoleRefused("role is busy; an assignment claim is active")
    for value in _roster(design).values():
        entry = _object(value)
        if entry.get('transferring_role') or entry.get('role_redirect'):
            raise RoleRefused('role transfer is fenced; use the destination holder')
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


def _mesh_context(design: HeldDesign, reach: HolderReach) -> None:
    _atomic(mesh_directory(design.placement.design_id) / 'design_context.json',
            {'design_id': design.placement.design_id, 'generation': design.placement.generation,
             'holder_session': reach.session_id, 'binding': _tag(design.binding), 'cwd': design.repository})


def launch_thread(design: HeldDesign, role: Role, prompt: Path, model: str, effort: str) -> str:
    """Start the reserved thread and keep its creation claim if the result is uncertain."""
    mesh = mesh_directory(design.placement.design_id)
    mesh.mkdir(parents=True, exist_ok=True)
    name = _instance(design, role).name
    policy = access_policy(design, role)
    result = subprocess.run([sys.executable, str(ROOT / 'scripts/agents/codex_mesh.py'), 'prime',
                             '--session-dir', str(mesh), '--name', name, '--cwd', design.repository,
                             '--instructions-file', str(prompt), '--model', model, '--effort', effort,
                             '--sandbox', 'read-only' if isinstance(policy, ReadOnlyReviewer) else policy.launch_sandbox,
                             '--held-design', design.placement.design_id, '--held-role', role],
                            capture_output=True, text=True, check=False)
    if result.returncode:
        raise RoleRefused('RoleNeedsRecovery: thread creation not confirmed: ' + result.stderr.strip())
    roster = _read(mesh / 'mesh_roster.json')
    entry = _object(roster.get(name))
    return _text(entry, 'thread_id')


def dispatch_turn(design: HeldDesign, role: Role, prompt: Path, reply: Path, model: str, effort: str) -> None:
    """Submit one turn outside store locks and save its reply at the header's path."""
    mesh = mesh_directory(design.placement.design_id)
    result = subprocess.run([sys.executable, str(ROOT / 'scripts/agents/codex_mesh.py'), 'follow',
                             '--session-dir', str(mesh), '--to', _instance(design, role).name,
                             '--role', 'impl' if role == 'designer' else 'review', '--message-file', str(prompt),
                             '--summary-file', str(reply), '--log-file', str(reply.with_suffix('.log')),
                             '--model', model, '--effort', effort, '--cwd', design.repository], check=False)
    if result.returncode:
        raise RoleRefused('RoleNeedsRecovery: held turn did not complete; pending input is retained')


def _project_hold(design: HeldDesign, role: Role, reach: HolderReach) -> None:
    astra_roles = _census_projection()
    instance = _instance(design, role)
    if isinstance(instance.lifecycle, (RoleNotOpened, RoleEnded)):
        return
    mesh = mesh_directory(design.placement.design_id)
    record = mesh / 'mesh_server.json'
    home = _text(_read(record if record.exists() else mesh / 'serving_home.json'), 'codex_home')
    _atomic(mesh / 'serving_home.json', {'codex_home': home})
    _ = astra_roles.hold(role, instance.lifecycle.thread_id, home, manager=reach.session_id,
                         design_id=design.placement.design_id, role_id=role)


def refresh_census(design_id: str) -> None:
    design = read_design(design_id)
    reach = resolve_holder(design.holder)
    if isinstance(reach, HolderReach):
        for instance in design.roles:
            _project_hold(design, instance.role, reach)


def _allow_new_roles() -> None:
    config = configparser.ConfigParser()
    _ = config.read(os.environ.get('DESIGN_ROLES_CONFIG') or ROOT / 'config/design_roles.conf')
    if not config.getboolean('roles', 'new_open', fallback=True):
        raise RoleRefused('new role opens and adoptions are disabled; keep existing identities and evidence')


def open_role(repository: str, document: str, role: Role, holder: Holder, area: str, *,
              host: str = '', binding: Binding = UNBOUND, request_id: str = '') -> HeldDesign:
    """Reserve identity before creation; retries never make a replacement thread."""
    _allow_new_roles()
    host = host or local_host()
    if host != local_host():
        raise RoleRefused('pair host differs from holder host')
    placement = authority_request('reserve', {'repository_id': repository, 'document': document,
                                             'host': host, 'request_id': request_id or str(uuid4())})
    design_id = _text(placement, 'design_id')
    if placement.get('adoption_state') == 'AdoptionReserved':
        raise RoleRefused('AdoptionReserved: complete the reserved transfer before opening')
    if placement.get('adoption_required'):
        raise RoleRefused('AdoptionRequired: ' + json.dumps(placement['adoption_required']))
    directory = design_directory(design_id)
    with _lock(directory):
        if (directory / 'design.json').exists():
            design = read_design(design_id)
            if design.holder != holder:
                raise RoleRefused('design has another holder; rebind explicitly while idle')
        else:
            checkout = _text(placement, 'checkout')
            document_path = str((Path(checkout) / _text(placement, 'document')).resolve())
            design = HeldDesign(DesignPlacement(design_id, host, _integer(placement, 'generation')),
                                repository, checkout, document_path, area, datetime.now(UTC).isoformat(), holder,
                                binding, tuple(RoleInstance(kind, f'{holder.production_slug}-{area}-astra-{kind}',
                                                          RoleNotOpened()) for kind in ('designer', 'reviewer')))
            _save(design)
        reach = _authorize(design)
        instance = _instance(design, role)
        if isinstance(instance.lifecycle, RoleEnded):
            raise RoleRefused('role ended; this design identity cannot be reopened.')
        if not isinstance(instance.lifecycle, RoleNotOpened) or instance.lifecycle.claim:
            if not isinstance(instance.lifecycle, RoleNotOpened):
                _project_hold(design, role, reach)
            return design
        _validate_binding(design.binding, design.repository, role)
    model, effort = resolve_model(role)
    prompt, _ = _prompt(design, role, '')
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
        _mesh_context(design, reach)
    # A failed or uncertain launch keeps its reserved claim; it cannot authorize replacement.
    thread = launch_thread(design, role, prompt, model, effort)
    with _lock(directory):
        current = read_design(design_id)
        if _instance(current, role).lifecycle != RoleNotOpened(claim):
            raise RoleRefused('RoleNeedsRecovery: thread creation claim changed')
        design = _set_lifecycle(current, role, RoleOpen(thread))
        _save(design)
    _project_hold(design, role, reach)
    return design


def recover(instance_id: str) -> HeldDesign:
    """Prove the recorded thread idle before clearing an uncertain local claim."""
    codex_mesh = _mesh_transport()
    design_id, role_text = instance_id.split('/')
    if role_text not in ('designer', 'reviewer'):
        raise RoleRefused('unknown role')
    role = role_text
    design = read_design(design_id)
    _ = _authorize(design)
    instance = _instance(design, role)
    if isinstance(instance.lifecycle, RoleEnded):
        raise RoleRefused('role ended; this design identity cannot be reopened.')
    entry = _object(_roster(design).get(instance.name))
    if entry.get('transferring_role') or entry.get('role_redirect'):
        raise RoleRefused('role transfer is fenced; use the destination holder')
    thread = _text(entry, 'thread_id')
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
    with _lock(design_directory(design_id)):
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
                                                                for value in current.roles)), role, RoleOpen(thread))
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
        reach = _authorize(design)
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
        with _edit_roster(design_id) as roster:
            entry = _object(roster.get(instance.name))
            if entry.get('thread_id') != instance.lifecycle.thread_id or entry.get('status') in BUSY:
                raise RoleRefused('RoleNeedsRecovery: roster thread or idle state changed')
            entry.update({'authorized_prompt': str(prompt), 'authorized_generation': generation,
                          'authorized_holder': reach.session_id, 'access_policy': _tag(access_policy(current, role)),
                          'held_design_id': design_id, 'held_role': role})
            roster[instance.name] = entry
            current = replace(current, roles=tuple(replace(value, assignment_claim=str(prompt)) if value.role == role else value
                                                   for value in current.roles))
            _save(current)
            _mesh_context(current, reach)

    try:
        dispatch_turn(design, role, prompt, reply, model, effort)
    except BaseException as error:
        with _lock(directory):
            current = read_design(design_id)
            _save(_set_lifecycle(replace(current, roles=tuple(replace(value, assignment_claim='') if value.role == role else value
                                                          for value in current.roles)), role,
                                     RoleNeedsRecovery(instance.lifecycle.thread_id, str(error))))
        raise
    with _lock(directory):
        current = read_design(design_id)
        _save(_set_lifecycle(replace(current, roles=tuple(replace(value, assignment_claim='') if value.role == role else value
                                                      for value in current.roles)), role, RoleOpen(instance.lifecycle.thread_id)))
        identifiers = cast(list[str], json.loads(prompt.with_suffix('.messages.json').read_text(encoding='utf-8')))
        for identifier in identifiers:
            (directory / 'pending' / role / f'{identifier}.json').unlink(missing_ok=True)
    _project_hold(design, role, reach)
    return reply


def release_servers(design: HeldDesign) -> None:
    """Release only proven-idle threads; queue parking precedes server cleanup."""
    codex_mesh = _mesh_transport()
    codex_mesh._release_held_server(str(mesh_directory(design.placement.design_id)))  # pyright: ignore[reportPrivateUsage]


def rebind(design_id: str, *, expected_generation: int, reason: str,
           holder: Holder | None = None, binding: Binding | None = None,
           repository: str = '', document: str = '') -> HeldDesign:
    if not reason.strip():
        raise RoleRefused('rebind needs a reason')
    directory = design_directory(design_id)
    design = read_design(design_id)
    _ = _authorize(design)
    if holder is not None:
        reach = resolve_holder(holder)
        if isinstance(reach, HolderUnavailable):
            raise RoleRefused(reach.reason)
        _ = authority_request('lookup', {'repository_id': design.repository_id, 'document': design.document,
                                        'host': local_host()})
    with _lock(directory):
        design = read_design(design_id)
        if design.placement.generation != expected_generation:
            raise RoleRefused('stale binding generation')
        transition = directory / 'transition.json'
        if transition.exists() and _read(transition).get('reason') != reason:
            raise RoleRefused('another lifecycle transition is incomplete')
        _idle(design)
        _atomic(directory / 'transition.json', {'generation': expected_generation, 'reason': reason})
    release_servers(design)
    _ = authority_request('advance', {'repository_id': design.repository_id, 'document': design.document,
                                      'host': local_host(), 'expected_generation': expected_generation})
    with _lock(directory):
        current = read_design(design_id)
        if current.placement.generation != expected_generation:
            raise RoleRefused('binding changed while releasing server')
        updated = replace(current, holder=holder or current.holder, binding=binding if binding is not None else current.binding,
                          repository=repository or current.repository, document=document or current.document,
                          placement=replace(current.placement, generation=expected_generation + 1))
        _save(updated)
        (directory / 'transition.json').unlink(missing_ok=True)
        reach = resolve_holder(updated.holder)
        if isinstance(reach, HolderReach):
            _mesh_context(updated, reach)
    if isinstance(reach, HolderReach):
        for instance in updated.roles:
            _project_hold(updated, instance.role, reach)
    return updated


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
    astra_roles = _census_projection()
    design_id, role_text = instance_id.split('/')
    if role_text not in ('designer', 'reviewer'):
        raise RoleRefused('unknown role: ' + role_text)
    role = role_text
    design = read_design(design_id)
    _ = _authorize(design)
    instance = _instance(design, role)
    if isinstance(instance.lifecycle, RoleEnded):
        if all(isinstance(item.lifecycle, (RoleNotOpened, RoleEnded)) for item in design.roles):
            release_servers(design)
        return design
    directory = design_directory(design_id)
    with _lock(directory):
        current = read_design(design_id)
        if current != design:
            raise RoleRefused('role changed before end')
        transition = directory / 'transition.json'
        if transition.exists() and _read(transition).get('reason') == 'adoption':
            raise RoleRefused('AdoptionNeedsRecovery: complete the fenced transfer before end')
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
                                     RoleNeedsRecovery(thread, 'end cleanup is not confirmed')))
            raise RoleRefused('RoleNeedsRecovery: end cleanup failed; roster end marker is retained')
    if all(item.role == role or isinstance(item.lifecycle, (RoleNotOpened, RoleEnded)) for item in design.roles):
        try:
            release_servers(design)
            _atomic(directory / 'end-server-release.json', {'released_at_utc': datetime.now(UTC).isoformat()})
        except (ConnectionError, OSError, SystemExit, RoleRefused) as error:
            with _lock(directory):
                _save(_set_lifecycle(read_design(design_id), role,
                                     RoleNeedsRecovery(thread, 'design server release is not confirmed')))
            raise RoleRefused('RoleNeedsRecovery: design server release failed') from error
    with _lock(design_directory(design_id)):
        current = read_design(design_id)
        updated = _set_lifecycle(current, role, RoleEnded(thread))
        _save(updated)
        (directory / 'transition.json').unlink(missing_ok=True)
    if thread:
        astra_roles.release(thread)
    return updated


def confirm_adoption(design: HeldDesign, role: Role, thread_id: str) -> None:
    """Confirm the released identity at its destination, without starting a turn."""
    codex_mesh = _mesh_transport()
    mesh = mesh_directory(design.placement.design_id)
    home = Path(_text(_read(mesh / 'serving_home.json'), 'codex_home'))
    port, _ = codex_mesh.ensure_server(str(mesh), home)
    client = codex_mesh.Client(port, 'held-adoption-confirm')
    try:
        entry = _object(_roster(design).get(_instance(design, role).name))
        reply = client.call('thread/resume', {'threadId': thread_id,
                                            'developerInstructions': Path(_text(entry, 'instructions_file')).read_text(encoding='utf-8'),
                                            'sandbox': 'read-only' if role == 'reviewer' else 'danger-full-access',
                                            'approvalPolicy': 'never'})
        if reply.get('error'):
            raise RoleRefused('AdoptionNeedsRecovery: destination could not resume the same thread')
        result = _object(reply.get('result'))
        if _object(result.get('thread')).get('id') != thread_id:
            raise RoleRefused('AdoptionNeedsRecovery: destination thread identity differs')
        state = codex_mesh._read_live_turn(client, thread_id)  # pyright: ignore[reportPrivateUsage]
        if not isinstance(state, codex_mesh.ThreadIdle):
            raise RoleRefused('AdoptionNeedsRecovery: destination is not proven idle')
    finally:
        client.close()


def _check_adoption_source(entry: dict[str, object], thread_id: str) -> None:
    if (entry.get('thread_id') != thread_id or entry.get('status') in BUSY
            or (entry.get('status') == 'paused' and entry.get('paused_from') in BUSY)):
        raise RoleRefused('AdoptionRequired: source is busy or its thread differs')


def _adoption_model(source: Path, name: str, thread_id: str, entry: dict[str, object]) -> tuple[str, str]:
    """Read missing settings only from records linked to this source thread."""
    settings: dict[str, object] = {key: entry.get(key) for key in ('model', 'effort')}
    for path in sorted(source.glob('impl_worker_*.json')):
        if all(value is not None for value in settings.values()):
            break
        launch = _read(path)
        if launch.get('worker_id') != thread_id:
            continue
        for key in settings:
            if settings[key] is None:
                settings[key] = launch.get(key)
        slot = path.stem.removeprefix('impl_worker_')
        assignments: dict[str, set[str]] = {'model': set(), 'effort': set()}
        for assignment in sorted(source.glob(f'phases/*/impl_agent_{slot}')):
            log = assignment.with_name(assignment.name + '.log')
            if not log.exists():
                continue
            with log.open(encoding='utf-8') as handle:
                if not any(line.rstrip().endswith(f'] mesh: {name} thread {thread_id}') for line in handle):
                    continue
            values = dict(line.split('=', 1) for line in assignment.read_text(encoding='utf-8').splitlines() if '=' in line)
            for key, field in (('model', 'agent'), ('effort', 'effort')):
                if values.get(field):
                    assignments[key].add(values[field])
        for key, values in assignments.items():
            if settings[key] is None and values:
                if len(values) != 1:
                    raise RoleRefused('AdoptionRequired: conflicting source launch evidence for ' + key)
                settings[key] = next(iter(values))
    if any(value is None for value in settings.values()):
        try:
            server = _read(source / 'mesh_server.json')
            client = _mesh_transport().Client(_integer(server, 'port'), 'design-role-adopt-evidence')
        except (OSError, ValueError, ConnectionError, SystemExit) as error:
            raise RoleRefused('AdoptionRequired: source model or effort evidence is unavailable') from error
        try:
            reply = client.call('thread/read', {'threadId': thread_id, 'includeTurns': False})
            result = _object(reply.get('result'))
            thread = _object(result.get('thread'))
            if not reply.get('error') and thread.get('id') == thread_id:
                for key, fields in (('model', ('model',)), ('effort', ('reasoningEffort', 'effort', 'reasoning_effort'))):
                    if settings[key] is None:
                        settings[key] = next((record[field] for record in (thread, result)
                                              for field in fields if record.get(field)), None)
        except (ConnectionError, OSError, SystemExit) as error:
            raise RoleRefused('AdoptionRequired: source model or effort evidence is unavailable') from error
        finally:
            client.close()
    for key, value in settings.items():
        if not isinstance(value, str) or not value:
            raise RoleRefused('AdoptionRequired: no recorded source ' + key + ' for thread ' + thread_id)
    return cast(str, settings['model']), cast(str, settings['effort'])


def adopt(repository: str, document: str, role: Role, holder: Holder, area: str, *,
          source_mesh: str, name: str, thread_id: str, adoption_id: str,
          binding: Binding = UNBOUND, host: str = '') -> HeldDesign:
    """Fence and release the source, then install the exact thread at its destination."""
    _allow_new_roles()
    codex_mesh = _mesh_transport()
    host = host or local_host()
    if host != local_host():
        raise RoleRefused('adoption cannot transfer hosts')
    placement = authority_request('reserve', {'repository_id': repository, 'document': document, 'host': host,
                                             'request_id': adoption_id})
    design_id = _text(placement, 'design_id')
    completed = design_directory(design_id) / 'adoption.json'
    if completed.exists():
        previous = _read(completed)
        if (previous.get('adoption_id') != adoption_id or previous.get('thread_id') != thread_id
                or previous.get('name') != name or Path(_text(previous, 'source_mesh')).resolve() != Path(source_mesh).resolve()):
            raise RoleRefused('adoption identity differs from completed transfer')
        design = read_design(design_id)
        if design.holder != holder or _instance(design, role).lifecycle != RoleOpen(thread_id):
            raise RoleRefused('adoption holder or role differs from completed transfer')
        reach = _authorize(design)
        if placement.get('adoption_state') != 'completed':
            _ = authority_request('adopt-commit', {'repository_id': repository, 'document': document, 'host': host,
                                                  'adoption_id': adoption_id})
        (design_directory(design_id) / 'transition.json').unlink(missing_ok=True)
        _project_hold(design, role, reach)
        return design
    if not isinstance(placement.get('adoption_required'), dict):
        raise RoleRefused('AdoptionRequired: authority has no recorded source; complete its inventory first')
    recorded_source = _object(placement['adoption_required'])
    if (recorded_source.get('thread_id') != thread_id or recorded_source.get('name') != name
            or Path(_text(recorded_source, 'mesh')).resolve() != Path(source_mesh).resolve()
            or _holder(_object(recorded_source.get('holder'))) != holder):
        raise RoleRefused('AdoptionRequired: source thread, mesh, name or holder differs from the authority record')
    checkout = _text(placement, 'checkout')
    candidate = HeldDesign(DesignPlacement(design_id, host, _integer(placement, 'generation')), repository,
                           checkout, str((Path(checkout) / _text(placement, 'document')).resolve()), area,
                           datetime.now(UTC).isoformat(), holder, binding,
                           tuple(RoleInstance(kind, name if kind == role else f'{holder.production_slug}-{area}-astra-{kind}',
                                              RoleOpen(thread_id) if kind == role else RoleNotOpened())
                                 for kind in ('designer', 'reviewer')))
    reach = _authorize(candidate)
    _validate_binding(binding, candidate.repository, role)
    model, effort = resolve_model(role)
    source = Path(source_mesh)
    source_path = source / 'mesh_roster.json'
    entry = _object(_read(source_path).get(name))
    _check_adoption_source(entry, thread_id)
    source_model, source_effort = _adoption_model(source, name, thread_id, entry)
    if source_model != model or source_effort != effort:
        raise RoleRefused('AdoptionRequired: source model or effort differs from the configured design-role model and effort')
    transfer = {'adoption_id': adoption_id, 'destination': str(mesh_directory(design_id))}
    if entry.get('transferring_role') is not None and entry.get('transferring_role') != transfer:
        raise RoleRefused('source is fenced by another adoption')
    prompt, _ = _prompt(candidate, role, 'Continue the adopted design in the same thread when the holder assigns work.')
    _ = authority_request('adopt-reserve', {'repository_id': repository, 'document': document, 'host': host,
                                            'adoption_id': adoption_id})
    with _lock(design_directory(design_id)), source_path.open('r+', encoding='utf-8') as source_lock:
        fcntl.flock(source_lock, fcntl.LOCK_EX)
        roster = _object(cast(object, json.load(source_lock)))
        entry = _object(roster.get(name))
        _check_adoption_source(entry, thread_id)
        if entry.get('model', source_model) not in (None, model) or entry.get('effort', source_effort) not in (None, effort):
            raise RoleRefused('adoption requires the configured model and effort, recorded at the source')
        fence = entry.get('transferring_role')
        destination = str(mesh_directory(design_id))
        transfer = {'adoption_id': adoption_id, 'destination': destination}
        if fence is not None and fence != transfer:
            raise RoleRefused('source is fenced by another adoption')
        _atomic(design_directory(design_id) / 'transition.json',
                {'generation': _integer(placement, 'generation'), 'reason': 'adoption'})
        _atomic(design_directory(design_id) / 'transfer.json',
                {**transfer, 'design_id': design_id, 'thread_id': thread_id, 'name': name, 'source_mesh': str(source)})
        entry['transferring_role'] = transfer
        roster[name] = entry
        _ = source_lock.seek(0)
        _ = source_lock.truncate()
        json.dump(roster, source_lock)
        source_lock.flush()
    server = _read(source / 'mesh_server.json')
    port = _integer(server, 'port')
    client = codex_mesh.Client(port, 'design-role-adopt')
    try:
        with codex_mesh._pending_file(str(source), name) as pending:  # pyright: ignore[reportPrivateUsage]
            state = codex_mesh._read_live_turn(client, thread_id)  # pyright: ignore[reportPrivateUsage]
            if not isinstance(state, codex_mesh.ThreadIdle):
                raise RoleRefused('AdoptionNeedsRecovery: source is not proven idle')
            def park(message: str) -> None:
                messages = codex_mesh._pending_for_thread(pending, thread_id)  # pyright: ignore[reportPrivateUsage]
                codex_mesh._write_pending(pending, thread_id, [*messages, message])  # pyright: ignore[reportPrivateUsage]
            codex_mesh._move_queue_to_pending(client, thread_id, park)  # pyright: ignore[reportPrivateUsage]
            released = codex_mesh._release_worker_thread(client, thread_id, str(source))  # pyright: ignore[reportPrivateUsage]
            if isinstance(released, codex_mesh.WorkerThreadStateUnknown):
                raise RoleRefused('AdoptionNeedsRecovery: ' + released.reason)
            _ = pending.seek(0)
            pending_text = pending.read()
    finally:
        client.close()
    directory = design_directory(design_id)
    with _lock(directory):
        if (directory / 'design.json').exists():
            design = read_design(design_id)
        else:
            checkout = _text(placement, 'checkout')
            design = HeldDesign(DesignPlacement(design_id, host, _integer(placement, 'generation')), repository,
                                checkout, str((Path(checkout) / _text(placement, 'document')).resolve()), area,
                                datetime.now(UTC).isoformat(), holder, binding,
                                tuple(RoleInstance(kind, name if kind == role else f'{holder.production_slug}-{area}-astra-{kind}',
                                                   RoleOpen(thread_id) if kind == role else RoleNotOpened())
                                      for kind in ('designer', 'reviewer')))
        reach = _authorize(design)
        _validate_binding(binding, design.repository, role)
        mesh = mesh_directory(design_id)
        mesh.mkdir(parents=True, exist_ok=True)
        destination_roster = _read(mesh / 'mesh_roster.json') if (mesh / 'mesh_roster.json').exists() else {}
        adopted = dict(entry)
        _ = adopted.pop('transferring_role', None)
        _ = adopted.pop('paused_from', None)
        _ = adopted.pop('paused_turn', None)
        adopted.update({'held_design_id': design_id, 'held_role': role, 'access_policy': _tag(access_policy(design, role)),
                        'instructions_file': str(prompt), 'status': 'done', 'cwd': design.repository,
                        'model': source_model, 'effort': source_effort})
        destination_roster[name] = adopted
        _atomic(mesh / 'mesh_roster.json', destination_roster)
        _atomic(mesh / 'serving_home.json', {'codex_home': _text(server, 'codex_home')})
        if pending_text:
            _ = (mesh / f'{name}.pending.json').write_text(pending_text, encoding='utf-8')
        _save(design)
        _mesh_context(design, reach)
    confirm_adoption(design, role, thread_id)
    with source_path.open('r+', encoding='utf-8') as source_lock:
        fcntl.flock(source_lock, fcntl.LOCK_EX)
        roster = _object(cast(object, json.load(source_lock)))
        current = _object(roster.get(name))
        current['role_redirect'] = {'design_id': design_id, 'destination': str(mesh), 'adoption_id': adoption_id}
        roster[name] = current
        _ = source_lock.seek(0)
        _ = source_lock.truncate()
        json.dump(roster, source_lock)
        source_lock.flush()
    _atomic(directory / 'adoption.json', {'adoption_id': adoption_id, 'source_mesh': str(source), 'name': name,
                                         'thread_id': thread_id, 'completed': True})
    _ = authority_request('adopt-commit', {'repository_id': repository, 'document': document, 'host': host,
                                          'adoption_id': adoption_id})
    (directory / 'transition.json').unlink(missing_ok=True)
    # The serving home is known even before a destination server is needed.
    astra_roles = _census_projection()
    _ = astra_roles.hold(role, thread_id, _text(server, 'codex_home'), manager=reach.session_id,
                         design_id=design_id, role_id=role)
    return design


def transfer_route(thread_id: str, destination_design: str = '') -> str:
    """Local fences survive source deletion. Only the recorded destination may use the thread."""
    if not thread_id:
        return ''
    for path in state_root().glob('*/transfer.json'):
        fields = _read(path)
        if fields.get('thread_id') == thread_id and fields.get('design_id') != destination_design:
            return _text(fields, 'destination')
    return ''


def list_roles() -> dict[str, object]:
    entries: list[dict[str, object]] = []
    problems: list[dict[str, str]] = []
    for path in sorted(state_root().glob('*/design.json')):
        try:
            design = read_design(path.parent.name)
            reach = resolve_holder(design.holder)
            for role in design.roles:
                lifecycle = role.lifecycle
                transition = path.parent / 'transition.json'
                if transition.exists() and not isinstance(lifecycle, (RoleNotOpened, RoleEnded)):
                    lifecycle = RoleNeedsRecovery(lifecycle.thread_id, 'incomplete ' + str(_read(transition).get('reason')))
                if not isinstance(lifecycle, (RoleNotOpened, RoleEnded)):
                    entry = _object(_roster(design).get(role.name))
                    if (mesh_directory(design.placement.design_id) / f'{role.name}.account-move.json').exists():
                        lifecycle = RolePausedForMove(lifecycle.thread_id)
                    elif entry.get('status') == 'failed':
                        lifecycle = RoleNeedsRecovery(lifecycle.thread_id, 'mesh turn failed')
                entries.append({'design_id': design.placement.design_id, 'instance_id': design.placement.design_id + '/' + role.role,
                                'role': role.role, 'name': role.name, 'holder': _tag(design.holder),
                                'holder_reach': _tag(reach), 'lifecycle': _tag(lifecycle),
                                'binding': _tag(design.binding), 'generation': design.placement.generation,
                                'document': design.document, 'host': design.placement.host})
        except (OSError, ValueError, KeyError, TypeError) as error:
            problems.append({'path': str(path), 'reason': str(error)})
    return {'roles': entries, 'problems': problems}


def render_roles(report: dict[str, object]) -> str:
    lines: list[str] = []
    for entry in cast(list[dict[str, object]], report['roles']):
        holder = _holder(_object(entry['holder']))
        holder_name = (f'{holder.unit_name} ({holder.production_slug})' if isinstance(holder, UnitHolder)
                       else holder.production_slug + ' showrunner')
        reach = _object(entry['holder_reach'])
        route = ('session ' + _text(reach, 'session_id') if reach.get('kind') == 'HolderReach'
                 else 'unavailable: ' + _text(reach, 'reason'))
        lifecycle = _object(entry['lifecycle'])
        thread = ' | thread ' + _text(lifecycle, 'thread_id') if lifecycle.get('thread_id') else ''
        lines.append(f"{entry['instance_id']} | {entry['role']} | {_text(lifecycle, 'kind')} | "
                     + f"{holder_name} | {route}{thread} | {entry['document']}")
    if not lines:
        lines.append('no held roles')
    for problem in cast(list[dict[str, str]], report['problems']):
        lines.append(f"problem: {problem['path']}: {problem['reason']}")
    return '\n'.join(lines)


def held_document(document: Path) -> HeldDesign | None:
    found = [read_design(path.parent.name) for path in state_root().glob('*/design.json')
             if _read(path).get('document') == str(document.resolve())]
    if len(found) > 1:
        raise RoleRefused('document has more than one held identity; recovery required')
    return found[0] if found else None


def unbind_run(run_directory: str) -> None:
    for path in state_root().glob('*/design.json'):
        design = read_design(path.parent.name)
        if isinstance(design.binding, DesignBoundToPhase) and Path(design.binding.run_directory) == Path(run_directory):
            _ = rebind(design.placement.design_id, expected_generation=design.placement.generation,
                       reason='run ended after reservation release', binding=DesignUnbound())


def end_holder(production: str, unit: str) -> None:
    for path in state_root().glob('*/design.json'):
        design = read_design(path.parent.name)
        if design.holder == UnitHolder(production, unit):
            reach = resolve_holder(ShowrunnerHolder(production))
            if not isinstance(reach, HolderReach) or reach.session_id != os.environ.get('CLAUDE_CODE_SESSION_ID'):
                raise RoleRefused('only the production showrunner may retire held roles')
            for instance in design.roles:
                if not isinstance(instance.lifecycle, (RoleNotOpened, RoleEnded)):
                    _end_retired_instance(design, instance)


def _end_retired_instance(design: HeldDesign, instance: RoleInstance) -> None:
    astra_roles = _census_projection()
    _idle(design)
    result = subprocess.run([sys.executable, str(ROOT / 'scripts/agents/codex_mesh.py'), 'end',
                             '--session-dir', str(mesh_directory(design.placement.design_id)), '--to', instance.name],
                            capture_output=True, text=True, check=False)
    if result.returncode:
        raise RoleRefused('RoleNeedsRecovery: retired role cleanup failed; retry after its turn finishes')
    if isinstance(instance.lifecycle, RoleNotOpened):
        return
    with _lock(design_directory(design.placement.design_id)):
        current = read_design(design.placement.design_id)
        _save(_set_lifecycle(current, instance.role, RoleEnded(instance.lifecycle.thread_id)))
    astra_roles.release(instance.lifecycle.thread_id)


def check_patch(design_id: str, generation: int, cwd: Path, paths: list[str]) -> DesignBoundToPhase:
    """Reject the whole patch before attribution, including symlink and rename escapes."""
    design = read_design(design_id)
    if (design_directory(design_id) / 'transition.json').exists():
        raise RoleRefused('binding transition is incomplete')
    if design.placement.generation != generation:
        raise RoleRefused('stale binding generation')
    _ = _authorize(design)
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


def rename_holder(production: str, old_unit: str, new_unit: str, old_checkout: str, new_checkout: str) -> None:
    """Rebind live routes only. Saved prompt/reply paths and aliases retain their history."""
    move_holder(UnitHolder(production, old_unit), UnitHolder(production, new_unit), old_checkout, new_checkout,
                reason='unit rename')


def move_holder(old_holder: Holder, new_holder: Holder, old_checkout: str = '', new_checkout: str = '', *,
                reason: str) -> None:
    """Give each design `old_holder` holds to `new_holder`, and move its live paths from `old_checkout` to
    `new_checkout` when both are given. A unit transfer or a production rename changes the holder only;
    a unit rename changes both. Role names, saved prompt/reply paths and aliases retain their history."""
    moves_checkout = bool(old_checkout) and old_checkout != new_checkout
    for path in sorted(state_root().glob('*/design.json')):
        design = read_design(path.parent.name)
        if design.holder not in (old_holder, new_holder):
            continue
        _idle(design)
        if design.holder == new_holder and (not moves_checkout or design.repository == new_checkout):
            continue
        if moves_checkout:
            _ = authority_request('checkout', {'repository_id': design.repository_id, 'host': local_host(),
                                              'old_checkout': old_checkout, 'new_checkout': new_checkout})
        with _lock(path.parent):
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
        with _lock(path.parent):
            current = read_design(path.parent.name)
            if current.placement.generation != design.placement.generation:
                raise RoleRefused(f'binding changed during {reason}')
            updated = replace(current, holder=new_holder, repository=new_checkout if moves_checkout else current.repository,
                              document=moved(current.document),
                              binding=binding, placement=replace(current.placement, generation=current.placement.generation + 1))
            _save(updated)
            (path.parent / 'transition.json').unlink(missing_ok=True)
            reach = resolve_holder(new_holder)
            _mesh_context(updated, reach if isinstance(reach, HolderReach) else HolderReach('HolderRestartPending'))
            with _edit_roster(path.parent.name) as roster:
                for value in roster.values():
                    entry = _object(value)
                    if moves_checkout:
                        entry['cwd'] = new_checkout
                    _ = entry.pop('authorized_prompt', None)
                    _ = entry.pop('authorized_holder', None)
                    _ = entry.pop('authorized_generation', None)


def rename_failures(production: str, old_unit: str, old_checkout: str) -> list[str]:
    return holder_failures(UnitHolder(production, old_unit), old_checkout)


def holder_failures(old_holder: Holder, old_checkout: str = '') -> list[str]:
    """Each place a design still names `old_holder`, or `old_checkout` when one is given."""
    failures: list[str] = []
    for path in state_root().glob('*/design.json'):
        try:
            design = read_design(path.parent.name)
            if design.holder.production_slug != old_holder.production_slug:
                continue
            if design.holder == old_holder:
                failures.append(str(path) + ': holder still names ' + _holder_name(old_holder))
            if old_checkout:
                values = [design.repository, design.document]
                if isinstance(design.binding, DesignBoundToPhase):
                    values.extend((design.binding.repository, design.binding.run_directory,
                                   *design.binding.output_paths))
                if any(value == old_checkout or value.startswith(old_checkout + '/') for value in values):
                    failures.append(str(path) + ': live binding still names old checkout')
                for value in _roster(design).values():
                    entry = _object(value)
                    if entry.get('cwd') == old_checkout:
                        failures.append(str(path) + ': launch cwd still names old checkout')
            context_path = mesh_directory(design.placement.design_id) / 'design_context.json'
            if context_path.exists():
                context = _read(context_path)
                if (old_checkout and context.get('cwd') == old_checkout) or (
                        context.get('generation') != design.placement.generation):
                    failures.append(str(context_path) + ': stale launch context')
        except (OSError, ValueError) as error:
            failures.append(str(path) + ': ' + str(error))
    return failures


def _holder_name(holder: Holder) -> str:
    return holder.unit_name if isinstance(holder, UnitHolder) else holder.production_slug + ' showrunner'


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for command in ('open', 'adopt'):
        p = commands.add_parser(command)
        for flag in ('repository', 'document', 'role', 'production', 'area'):
            _ = p.add_argument('--' + flag, required=True)
        _ = p.add_argument('--unit', default='')
        _ = p.add_argument('--host', default='')
        _ = p.add_argument('--run-directory', default='')
        _ = p.add_argument('--phase', default='')
        _ = p.add_argument('--output-path', action='append', default=[])
        _ = p.add_argument('--checkout', default='')
        if command == 'adopt':
            for flag in ('source-mesh', 'name', 'thread-id', 'adoption-id'):
                _ = p.add_argument('--' + flag, required=True)
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
    for flag in ('run-directory', 'phase', 'checkout', 'document', 'production', 'unit'):
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
    _ = p.add_argument('--inventory-complete', action='store_true')
    args = cast(dict[str, object], vars(parser.parse_args(argv)))
    def argument(key: str) -> str:
        return cast(str, args.get(key, ''))
    try:
        command = argument('command')
        if command in ('open', 'adopt'):
            role = argument('role')
            if role not in ('designer', 'reviewer'):
                raise RoleRefused('unknown role')
            holder = UnitHolder(argument('production'), argument('unit')) if argument('unit') else ShowrunnerHolder(argument('production'))
            binding: Binding = UNBOUND
            if argument('run_directory') or argument('phase'):
                if not argument('run_directory') or not argument('phase'):
                    raise RoleRefused('run-directory and phase are required together')
                binding = DesignBoundToPhase(str(Path(argument('run_directory')).resolve()), argument('phase'),
                                             argument('checkout'), tuple(cast(list[str], args['output_path'])))
            if command == 'open':
                design = open_role(argument('repository'), argument('document'), role, holder, argument('area'),
                                   host=argument('host'), binding=binding)
            else:
                design = adopt(argument('repository'), argument('document'), role, holder, argument('area'),
                               source_mesh=argument('source_mesh'), name=argument('name'), thread_id=argument('thread_id'),
                               adoption_id=argument('adoption_id'), binding=binding, host=argument('host'))
            print(design.placement.design_id + '/' + role)
        elif command == 'resume':
            print(resume(argument('instance_id'), argument('prompt_file')))
        elif command == 'rebind':
            current = read_design(argument('design_id'))
            next_binding = current.binding
            if args.get('unbind'):
                next_binding = DesignUnbound()
            elif argument('run_directory') or argument('phase'):
                if not argument('run_directory') or not argument('phase'):
                    raise RoleRefused('run-directory and phase are required together')
                next_binding = DesignBoundToPhase(str(Path(argument('run_directory')).resolve()), argument('phase'),
                                                 argument('checkout') or current.repository,
                                                 tuple(cast(list[str], args['output_path'])))
            next_holder = current.holder
            if argument('production'):
                next_holder = UnitHolder(argument('production'), argument('unit')) if argument('unit') else ShowrunnerHolder(argument('production'))
            _ = rebind(argument('design_id'), expected_generation=cast(int, args['expected_generation']),
                       reason=argument('reason'), binding=next_binding, holder=next_holder,
                       repository=argument('checkout'), document=argument('document'))
        elif command == 'end':
            _ = end(argument('instance_id'))
        elif command == 'recover':
            _ = recover(argument('instance_id'))
        elif command == 'unbind-run':
            unbind_run(argument('run_directory'))
        elif command == 'register':
            register_repository(argument('repository'), {argument('host'): argument('checkout')},
                                inventory_complete=cast(bool, args['inventory_complete']))
        elif command == 'authority':
            config = configparser.ConfigParser()
            _ = config.read(os.environ.get('DESIGN_ROLES_CONFIG') or ROOT / 'config/design_roles.conf')
            if config.get('ownership', 'host') != local_host():
                raise RoleRefused('this host is not the ownership authority')
            print(json.dumps(_authority_local(argument('operation'), _object(cast(object, json.load(sys.stdin))))))
        else:
            report = list_roles()
            print(json.dumps(report) if args.get('json') else render_roles(report))
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, configparser.Error) as error:
        print('design_roles: ' + str(error).replace('\n', ' '), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
