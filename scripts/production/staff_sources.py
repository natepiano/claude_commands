#!/usr/bin/env python3
"""Match production members and import missing staff once; remove this importer on 2026-11-13."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import import_module
import sys
from pathlib import Path
from typing import Literal, cast

# These source modules still use sibling imports; keep their existing module identity.

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

sys.path.insert(0, str(Path(__file__).resolve().parent))
import production_units  # noqa: E402
from scripts.production import showrunners  # noqa: E402
import unit_names  # noqa: E402
from scripts.production import staff, unit_config  # noqa: E402
from scripts.direct import workers  # noqa: E402
from scripts.astra import astra_roles, design_roles  # noqa: E402
from scripts.lib import unit_runs, git_exclude  # noqa: E402
from scripts.message import sessions  # noqa: E402
from scripts.shutdown import remote  # noqa: E402


# Matchers live below production_units, so lower readers use the same join without an import cycle.
from unit_lookup import (  # noqa: E402
    ShowrunnerMember as ShowrunnerMember, UnitMember as UnitMember, ObservedUnit as ObservedUnit,
    MemberSelector as MemberSelector, MemberFound as MemberFound, NoSuchMember as NoSuchMember,
    UnitProduction as UnitProduction, MemberSources as _MemberSources,
    member_sources as _member_sources,
)
import unit_lookup  # noqa: E402


def member(selector: MemberSelector, record: staff.StaffSnapshot) -> MemberFound | NoSuchMember:
    """Resolve pane identity or current production membership through the shared join."""
    return _member_sources().member(selector, record)


def showrunner_members(record: staff.StaffSnapshot) -> dict[str, staff.StaffRow]:
    """Resolve local production slugs to rows with the same launch-first matching rule."""
    return _member_sources().showrunner_members(record)


def unit_parent(unit: str, session_id: str = "") -> UnitProduction:
    """Resolve the current staff edge; old launch marks supply identity only."""
    return unit_lookup.unit_parent(unit, session_id, record=staff.snapshot())


def _showrunner_key(slug: str) -> str:
    return f"showrunner:{slug}"


def _seed_member(key: str, kind: Literal["showrunner", "unit"], name: str, found: MemberFound | NoSuchMember,
                 parent: staff.SeedParent | staff.KeepSeedParent | staff.RootSeedParent) -> staff.SeedMember:
    return staff.SeedMember(key, kind, name if isinstance(found, NoSuchMember) else found.row.name,
                           staff.NewSeedMember() if isinstance(found, NoSuchMember) else found.row.id, parent)


def _collect(record: staff.StaffSnapshot, sources: _MemberSources) -> staff.SeedInput:
    members: list[staff.SeedMember] = []
    imported_unit_names = {row.name for row in record.find(kind="unit")}
    for slug, source in sources.productions.items():
        try:
            cells = production_units.unit_table(source.document.splitlines())
            found = sources.member(ShowrunnerMember(slug), record)
            production_members = [_seed_member(_showrunner_key(slug), "showrunner",
                                               showrunners.space_from_text(slug, source.document), found,
                                               staff.KeepSeedParent())]
            for unit_cells in cells:
                unit = production_units.cell_value(unit_cells.get("Unit", ""))
                name = unit_names.name(unit)
                selected = sources.member(UnitMember(slug, unit), record)
                if isinstance(selected, MemberFound):
                    production_members.append(_seed_member(f"unit:{slug}/{unit}", "unit", name, selected,
                                                           staff.KeepSeedParent()))
                    continue
                # A moved unit can remain in its old Units table. Import never recreates it there.
                if name in imported_unit_names:
                    continue
                path_text = production_units.cell_value(unit_cells.get("Worktree", ""))
                if (not path_text or not Path(path_text).expanduser().is_dir()
                        or production_units.plan_cell_is_retired(unit_cells.get("Plan", ""))):
                    continue
                production_members.append(_seed_member(f"unit:{slug}/{unit}", "unit", name, selected,
                                                       staff.SeedParent(_showrunner_key(slug))))
        except (OSError, UnicodeError, production_units.Refusal, unit_names.BadName) as error:
            print(f"seed: {slug} ({source.timer.doc}): {error}", file=sys.stderr)
            continue
        members.extend(production_members)
    return staff.SeedInput(tuple(members), ())


def _print_plan(seed_input: staff.SeedInput) -> bool:
    changed = False
    for entry in seed_input.members:
        if isinstance(entry.match, staff.NewSeedMember):
            action, identity = "add", entry.key
            changed = True
        else:
            action, identity = "keep", entry.match
        print(f"{action} {identity} {entry.kind} {entry.name}")
    return changed


def seed(*, dry_run: bool = False) -> None:
    """Import missing local members without moving or deleting any recorded member."""
    with staff.operation():
        record = staff.initial_snapshot()
        dependencies = staff.RecordInputs(set(record.inputs))
        sources = _member_sources(dependencies=dependencies)
        seed_input = _worker_seed(record, sources, _collect(record, sources), dependencies)
        changed = _print_plan(seed_input)
        if dry_run:
            if not changed:
                print("unchanged", file=sys.stderr)
        else:
            _ = staff.apply_seed(seed_input)


# The source scan is shared by seed and the live handler. It never writes source records.
LiveRole = Literal["showrunner", "unit", "impl", "test", "review", "fix", "astra-designer", "astra-reviewer"]


@dataclass(frozen=True)
class WorkerOwner:
    key: str
    match: staff.StaffRow | staff.NewSeedMember


@dataclass(frozen=True)
class BackendUnavailable:
    """Membership is known, but no authoritative backend was recorded."""


@dataclass(frozen=True)
class WorkerObservation:
    name: str
    owner: WorkerOwner
    role: LiveRole
    identity: dict[str, str] | BackendUnavailable


def _problem(path: Path | str, error: object) -> None:
    message = f"staff sources: {path}: {error}"
    print(message.replace("\n", " ").replace("\r", " "), file=sys.stderr)


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return cast(dict[str, object], value)


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _alive(pid: object) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _role(value: str) -> LiveRole:
    if value in ("designer", "reviewer"):
        return cast(LiveRole, f"astra-{value}")
    if value in ("impl", "test", "review", "fix", "astra-designer", "astra-reviewer"):
        return cast(LiveRole, value)
    raise ValueError(f"missing or invalid recorded worker role: {value!r}")


class _WorkerSources:
    """One command's read evidence, shared by the importer and live projection."""

    def __init__(self, record: staff.StaffSnapshot, sources: _MemberSources,
                 members: tuple[staff.SeedMember, ...] = (), *,
                 dependencies: staff.RecordInputs | None = None) -> None:
        self.record: staff.StaffSnapshot = record
        self.sources: _MemberSources = sources
        self.members: dict[str, staff.SeedMember] = {entry.key: entry for entry in members}
        self.objects: dict[Path, dict[str, object]] = {}
        self.observed: dict[tuple[str, str], WorkerObservation] = {}
        self.identities: dict[staff.StaffId, dict[str, str]] = {}
        self.sessions: dict[str, WorkerOwner] = {}
        self.runs: set[Path] = set()
        self.backends: dict[tuple[str, str], dict[str, str]] = {}
        self.backend_ids: dict[str, dict[str, str]] = {}
        self.processed_ledgers: set[tuple[Path, str]] = set()
        self.dependencies: staff.RecordInputs = dependencies if dependencies is not None else staff.RecordInputs(set(record.inputs))
        self.inputs: set[str] = self.dependencies.paths
        self.directors: dict[Path, workers.LiveUnitDirector | workers.NoLiveUnitDirector] = {}
        self.blocked_threads: set[str] = set()
        self.ledger_names: set[tuple[str, str]] = set()
        registry = sessions.read_registry(dependencies=self.dependencies)
        for path in registry.unreadable:
            if path.exists():
                _problem(path, "session registry is unreadable")
        self.running: dict[str, sessions.SessionRecord] = {
            session["sessionId"]: session for session in workers.running_director_sessions(registry)
        }
        self._owners()

    def read(self, path: Path) -> dict[str, object]:
        path = path.expanduser().absolute()
        if path not in self.objects:
            self.inputs.add(str(path))
            try:
                self.objects[path] = _object(cast(object, json.loads(path.read_text(encoding="utf-8"))))
            except (OSError, UnicodeError, ValueError) as error:
                _problem(path, error)
                self.objects[path] = {}
        return self.objects[path]

    def owner(self, selector: ShowrunnerMember | UnitMember) -> WorkerOwner:
        found = self.sources.member(selector, self.record)
        if isinstance(found, MemberFound):
            return WorkerOwner(f"staff:{found.row.id}", found.row)
        key = (_showrunner_key(selector.slug) if isinstance(selector, ShowrunnerMember)
               else f"unit:{selector.slug}/{selector.unit}")
        if key in self.members:
            entry = self.members[key]
            row = self.record.by_id(entry.match) if isinstance(entry.match, str) else None
            return WorkerOwner(key, row if row is not None else staff.NewSeedMember())
        raise ValueError(f"no staff parent for {selector}")

    def _owners(self) -> None:
        for slug, source in self.sources.productions.items():
            try:
                owner = self.owner(ShowrunnerMember(slug))
            except ValueError:
                continue
            session_id = source.timer.target.removeprefix("session:")
            self.sessions[session_id] = owner
            if session_id in self.running and isinstance(owner.match, staff.StaffRow):
                self.identities[owner.match.id] = {"claude": session_id}
        for launch in sorted(self.sources.launches, key=lambda launch: launch["written_at"]):
            if launch.get("ended") or launch["session_id"] not in self.running:
                continue
            environment = launch["environment"]
            staff_id = environment.get("STAFF_ID", "")
            row = self.record.by_id(staff.StaffId(staff_id)) if staff_id else None
            if row is None and not staff_id and environment.get("SHOWRUNNER_UNIT_ID"):
                found = self.sources.member(ObservedUnit(environment["SHOWRUNNER_UNIT_ID"]), self.record)
                row = found.row if isinstance(found, MemberFound) else None
            if row is not None:
                self.identities[row.id] = {"claude": launch["session_id"]}
                if row.kind != "worker":
                    self.sessions[launch["session_id"]] = WorkerOwner(f"staff:{row.id}", row)
            elif not staff_id and environment.get("SHOWRUNNER_UNIT_ID"):
                try:
                    self.sessions[launch["session_id"]] = self.owner(UnitMember(
                        environment.get("SHOWRUNNER_UNIT", ""), environment["SHOWRUNNER_UNIT_ID"]))
                except ValueError:
                    pass

    def _record_run_owner(self, run: Path) -> WorkerOwner:
        director_path = run / unit_runs.DIRECTOR_FILE
        self.inputs.add(str(director_path.absolute()))
        director = director_path.read_text(encoding="utf-8").strip()
        if director in self.sessions:
            return self.sessions[director]
        state = self.read(run / workers.STATE)
        slug, unit = _text(state.get("production")), _text(state.get("unit"))
        if slug and unit:
            return self.owner(UnitMember(slug, unit))
        raise ValueError(f"director {director!r} has no staff parent")

    def _run_owner(self, run: Path) -> WorkerOwner:
        owner = self._record_run_owner(run)
        director = self.directors.get(run)
        if isinstance(director, workers.LiveUnitDirector) and isinstance(owner.match, staff.StaffRow):
            self.identities[owner.match.id] = {"claude": director.session_id}
            self.sessions[director.session_id] = owner
        return owner

    def add(self, observed: WorkerObservation) -> None:
        _ = staff.WorkerName.parse(observed.name)
        key = (observed.owner.key, observed.name)
        previous = self.observed.get(key)
        if (previous is not None and not isinstance(previous.identity, BackendUnavailable)
                and not isinstance(observed.identity, BackendUnavailable) and previous.identity != observed.identity):
            raise ValueError(f"conflicting worker identities for {observed.name}")
        if previous is None or not isinstance(observed.identity, BackendUnavailable):
            self.observed[key] = observed

    def backend(self, owner: WorkerOwner, name: str, fields: Mapping[str, object]) -> dict[str, str] | BackendUnavailable:
        identity = self.backend_ids.get(_text(fields.get("staff_id"))) or self.backends.get((owner.key, name))
        if identity is not None:
            return identity
        # Existing producers own identities here until they have a ledger entry.
        thread = _text(fields.get("thread_id"))
        matches = [identity for identity in self.backends.values() if thread and identity.get("codex") == thread]
        if matches:
            return matches[0]
        if thread:
            return {"codex": thread}
        session = _text(fields.get("worker_id")) or _text(fields.get("session_id"))
        return {"claude": session} if session else BackendUnavailable()

    def ledger(self, path: Path, owner: WorkerOwner | None = None) -> None:
        marker = (path.absolute(), owner.key if owner is not None else "")
        if marker in self.processed_ledgers:
            return
        self.processed_ledgers.add(marker)
        data = self.read(path)
        try:
            entries = _object(data.get("workers", {}))
        except ValueError as error:
            _problem(path, error)
            return
        for key, value in entries.items():
            try:
                fields = _object(value)
                name = _text(fields.get("name")) or key
                # Reuse the ledger's decoder; its public locked reader also writes and recovers.
                worker = workers._decode_worker(name, fields)  # pyright: ignore[reportPrivateUsage]
                current = self.record.by_id(staff.StaffId(_text(fields.get("staff_id")) or key))
                parent = owner
                if current is not None and current.kind == "worker" and current.parent is not None:
                    row = self.record.by_id(current.parent)
                    if row is not None:
                        parent = WorkerOwner(f"staff:{row.id}", row)
                        name = current.name
                if parent is None:
                    try:
                        parent = self.owner(UnitMember(path.parent.name, path.stem))
                    except ValueError:
                        # An adhoc stem is a worktree name and digest; `.claude-<digest>` names no unit.
                        observed = ObservedUnit(path.stem)
                        found = (self.sources.member(observed, self.record)
                                 if unit_names.NAME.fullmatch(path.stem) else NoSuchMember(observed))
                        if isinstance(found, MemberFound):
                            parent = WorkerOwner(f"staff:{found.row.id}", found.row)
                        elif isinstance(worker.ownership, workers.HeldBy):
                            parent = self._run_owner(Path(worker.ownership.run))
                        else:
                            raise ValueError("worker ledger has no current staff parent")
                self.ledger_names.add((parent.key, name))
                if isinstance(worker.ownership, (workers.Retired, workers.Adopted)):
                    continue
                backend = worker.backend
                identity = ({"codex": backend.thread_id} if isinstance(backend, workers.CodexThread)
                            else {"claude": backend.session_id})
                self.backend_ids[_text(fields.get("staff_id")) or key] = identity
                self.backends[(parent.key, name)] = identity
                # These roles have their own holder record. An unreadable owner
                # cannot authorize importing them under the ledger's unit.
                if worker.role not in ("designer", "reviewer", "astra-designer", "astra-reviewer"):
                    self.add(WorkerObservation(name, parent, _role(worker.role), identity))
                if isinstance(worker.ownership, workers.HeldBy):
                    self.runs.add(Path(worker.ownership.run))
            except (ValueError, staff.StaffRefused, workers.WorkerUnavailable, OSError) as error:
                _problem(f"{path} worker {key}", error)

    def run_live(self, run: Path) -> bool:
        if run not in self.directors:
            self.directors[run] = workers.resolve_live_unit_director(
                run, self.record, self.sources, tuple(self.running.values()), self.dependencies)
        if isinstance(self.directors[run], workers.LiveUnitDirector):
            return True
        server = run / "mesh_server.json"
        return server.exists() and _alive(self.read(server).get("pid"))

    def roster(self, run: Path, owner: WorkerOwner | None = None) -> None:
        path = run / workers.ROSTER
        if not self.run_live(run):
            return
        for name, value in self.read(path).items():
            try:
                entry = _object(value)
                if entry.get("status") not in ("starting", "running", "paused", "waiting_capacity"):
                    continue
                parent = owner if owner is not None else self._run_owner(run)
                ledger = _text(entry.get("unit_ledger"))
                if ledger:
                    self.ledger(Path(ledger), parent)
                if (parent.key, name) in self.ledger_names:
                    continue
                identity = self.backend(parent, name, entry)
                if isinstance(identity, BackendUnavailable):
                    _problem(f"{path} worker {name}", "worker backend is unavailable")
                self.add(WorkerObservation(name, parent, _role(_text(entry.get("role"))), identity))
            except (ValueError, OSError, staff.StaffRefused) as error:
                _problem(f"{path} worker {name}", error)

    def claude_workers(self, run: Path) -> None:
        ledger = run / "workers"
        launches = sorted(run.glob("impl_worker_*.json"))
        if not ledger.exists() and not launches:
            return
        try:
            owner = self._run_owner(run)
            board = Path(__file__).resolve().parents[1] / "direct/board.sh"
            result = subprocess.run(["bash", str(board), "roles", str(run)], capture_output=True,
                                    text=True, check=True, timeout=5, stdin=subprocess.DEVNULL)
            roles = dict(line.split("\t", 1) for line in result.stdout.splitlines() if "\t" in line)
            board_log = run / "board.log"
            self.dependencies(board_log)
            registrations: dict[str, str] = {}
            try:
                lines = board_log.read_text(encoding="utf-8").splitlines()
            except FileNotFoundError:
                lines = []
            for line in lines:
                registration = re.search(r"\[([^]]+)\] register: .*?\bmesh=([A-Za-z0-9._-]+)(?:[; ]|$)", line)
                if registration:
                    registrations[registration[2]] = registration[1]
            found: dict[str, tuple[str, str]] = {}
            for path in launches:
                fields = self.read(path)
                identity, slot = _text(fields.get("worker_id")), _text(fields.get("slot"))
                if fields.get("family") == "claude" and identity in self.running:
                    found[identity] = (_text(fields.get("name")) or slot, slot)
            if ledger.exists():
                self.inputs.add(str(ledger.absolute()))
                for line in ledger.read_text(encoding="utf-8").splitlines():
                    short, separator, name = line.partition("\t")
                    if not separator or not short:
                        raise ValueError("invalid Claude workers ledger entry")
                    matches = [session for session in self.running if session.startswith(short)]
                    if len(matches) > 1:
                        raise ValueError(f"ambiguous Claude session prefix {short}")
                    if matches and matches[0] not in found:
                        found[matches[0]] = (name, registrations.get(name, ""))
            for identity, (name, slot) in found.items():
                self.add(WorkerObservation(name, owner, _role(roles.get(slot, "")), {"claude": identity}))
        except (OSError, UnicodeError, ValueError, subprocess.SubprocessError, staff.StaffRefused) as error:
            _problem(run, error)

    def design(self, design_id: str) -> None:
        path = design_roles.state_root() / design_id / "design.json"
        try:
            self.inputs.add(str(path.absolute()))
            design = design_roles.read_design(design_id)
            holder = design.holder
            owner = self.owner(UnitMember(holder.production_slug, holder.unit_name)
                               if isinstance(holder, design_roles.UnitHolder)
                               else ShowrunnerMember(holder.production_slug))
            for instance in design.roles:
                if isinstance(instance.lifecycle, (design_roles.RoleEnded, design_roles.RoleNotOpened)):
                    continue
                fields = {"thread_id": instance.lifecycle.thread_id}
                identity = self.backend(owner, instance.name, fields)
                if isinstance(identity, BackendUnavailable):
                    _problem(path, f"{instance.name}: worker backend is unavailable")
                else:
                    self.observed = {key: entry for key, entry in self.observed.items() if entry.identity != identity}
                self.add(WorkerObservation(instance.name, owner, cast(LiveRole, f"astra-{instance.role}"), identity))
        except (ValueError, OSError, KeyError, staff.StaffRefused) as error:
            _problem(path, error)

    def astra(self, path: Path) -> None:
        try:
            fields = self.read(path)
            role = astra_roles._parse_record(fields, path)  # pyright: ignore[reportPrivateUsage]
            lifetime = role["lifetime"]
            if isinstance(lifetime, astra_roles.ExecProcess) and not _alive(lifetime.pid):
                return
            owner = self.sessions.get(role["manager"])
            if isinstance(lifetime, astra_roles.HeldDesignRole):
                self.design(lifetime.design_id)
                return
            if isinstance(lifetime, astra_roles.MeshWorker):
                entry = _object(self.read(Path(lifetime.session_dir) / workers.ROSTER).get(lifetime.worker))
                if entry.get("status") not in ("starting", "running", "paused", "waiting_capacity", "done"):
                    return
                name = lifetime.worker
            else:
                name = f"astra-{role['role']}{lifetime.pid}"
            if owner is None:
                raise ValueError(f"manager {role['manager']} has no staff member")
            identity = self.backend(owner, name, fields)
            if isinstance(identity, BackendUnavailable):
                _problem(path, f"{name}: worker backend is unavailable")
            if not isinstance(identity, BackendUnavailable):
                self.observed = {key: entry for key, entry in self.observed.items() if entry.identity != identity}
            self.add(WorkerObservation(name, owner, cast(LiveRole, f"astra-{role['role']}"), identity))
        except (ValueError, OSError, KeyError, StopIteration, staff.StaffRefused) as error:
            self.blocked_threads.add(path.stem)
            _problem(path, error)

    def collect(self) -> tuple[WorkerObservation, ...]:
        ledger_root = Path(os.environ.get("UNIT_WORKERS_STATE", "~/.local/state/unit-workers")).expanduser()
        for path in sorted(ledger_root.glob("*/*.json")):
            if not path.name.endswith(".move.json"):
                self.ledger(path)
        for root in unit_runs.run_roots(os.environ.get("ROSTER_RUN_ROOT")):
            self.runs.update(path.parent for path in root.glob(f"*/{workers.ROSTER}"))
            self.runs.update(path.parent for path in root.glob("*/workers"))
            self.runs.update(path.parent for path in root.glob("*/impl_worker_*.json"))
        for run in sorted(self.runs):
            try:
                if (run / workers.ROSTER).exists():
                    self.roster(run)
                self.claude_workers(run)
            except (OSError, UnicodeError, ValueError) as error:
                _problem(run, error)
        for path in sorted(design_roles.state_root().glob("*/design.json")):
            self.design(path.parent.name)
        for path in sorted(astra_roles.state_root().glob("*.json")):
            self.astra(path)
        return tuple(entry for entry in self.observed.values()
                     if isinstance(entry.identity, BackendUnavailable)
                     or entry.identity.get("codex") not in self.blocked_threads)


def _worker_seed(record: staff.StaffSnapshot, sources: _MemberSources, base: staff.SeedInput,
                 dependencies: staff.RecordInputs) -> staff.SeedInput:
    members = list(base.members)
    keys = {entry.key for entry in members}
    parent_keys = {entry.match: entry.key for entry in members if isinstance(entry.match, str)}
    for observed in _WorkerSources(record, sources, base.members, dependencies=dependencies).collect():
        owner = observed.owner
        parent_key = parent_keys.get(owner.match.id, owner.key) if isinstance(owner.match, staff.StaffRow) else owner.key
        if parent_key not in keys:
            assert isinstance(owner.match, staff.StaffRow)
            members.append(staff.SeedMember(parent_key, owner.match.kind, owner.match.name,
                                            owner.match.id, staff.KeepSeedParent()))
            keys.add(parent_key)
        existing = (next((row for row in record.find(kind="worker", parent=owner.match.id)
                          if row.name == observed.name), None)
                    if isinstance(owner.match, staff.StaffRow) else None)
        key = f"worker:{parent_key}/{observed.name}"
        members.append(staff.SeedMember(key, "worker", observed.name,
                                        existing.id if existing is not None else staff.NewSeedMember(),
                                        staff.SeedParent(parent_key)))
    return staff.SeedInput(tuple(members), base.retire)


def live() -> dict[str, object]:
    """Read one owner snapshot and project identities without changing any source."""
    record = staff.snapshot()
    dependencies = staff.RecordInputs(set(record.inputs))
    sources = _member_sources(dependencies=dependencies)
    scan = _WorkerSources(record, sources, dependencies=dependencies)
    observed = scan.collect()
    members: dict[staff.StaffId, dict[str, object]] = {}
    for row in record.rows:
        identity = scan.identities.get(row.id)
        if row.kind != "worker" and identity is not None:
            members[row.id] = {**row.to_json(), "role": row.kind, "identity": identity}
    for entry in observed:
        owner = entry.owner.match
        if not isinstance(owner, staff.StaffRow) or isinstance(entry.identity, BackendUnavailable):
            continue
        rows = [row for row in record.find(kind="worker", parent=owner.id) if row.name == entry.name]
        if len(rows) != 1:
            _problem(entry.name, "no recorded worker membership; run staff_sources.py seed")
            continue
        row = rows[0]
        member: dict[str, object] = {**row.to_json(), "role": entry.role, "identity": entry.identity}
        members[row.id] = member
    # Every emitted edge ends inside this document, even if source evidence is incomplete.
    while missing := [key for key, member in members.items()
                      if member["parent"] is not None and member["parent"] not in members]:
        for key in missing:
            _problem(key, "parent has no session identity; omitted from live staff")
            del members[key]
    return {"schema": 3, "inputs": sorted(scan.inputs), "members": [members[key] for key in sorted(members)]}


def cli_live() -> int:
    """Keep source failures on the same staff CLI exception boundary when run as a script."""
    def run() -> int:
        print(json.dumps(live(), sort_keys=True))
        return 0

    return staff.cli_result(run)


@dataclass(frozen=True)
class FillDocument:
    path: Path
    text: str
    source: str

    @property
    def slug(self) -> str:
        return showrunners.production_slug(str(self.path))


@dataclass(frozen=True)
class FillUnit:
    row: staff.StaffRow
    worktree: staff.PresentWorktree
    config: unit_config.UnitConfig
    source: str


def _fill_git(folder: Path, *arguments: str) -> str:
    done = subprocess.run(["git", "-C", str(folder), *arguments], text=True,
                          capture_output=True, check=False)
    if done.returncode:
        raise ValueError(done.stderr.strip() or f"git {' '.join(arguments)} failed in {folder}")
    return done.stdout.strip()


class _FillSources:
    """The fill's manifest and document evidence, collected before any write."""

    def __init__(self, record: staff.StaffSnapshot, sources: _MemberSources,
                 dependencies: staff.RecordInputs) -> None:
        self.record: staff.StaffSnapshot = record
        self.sources: _MemberSources = sources
        self.dependencies: staff.RecordInputs = dependencies
        self.documents: dict[tuple[Path, str], FillDocument] = {}
        self.repositories: set[Path] = set()
        self.manifest: set[str] = set()
        self.problems: list[str] = []

    def problem(self, source: str | Path, unit: str, reason: object) -> None:
        self.problems.append(f"{source} {unit}: {str(reason).replace(chr(10), ' ')}")

    def repository(self, path: Path) -> None:
        folder = path if path.is_dir() else path.parent
        try:
            common = Path(_fill_git(folder, "rev-parse", "--path-format=absolute", "--git-common-dir"))
            # The common directory names the repository once, even for linked worktrees.
            self.repositories.add(common)
            self.manifest.add(f"repository {common}")
        except (OSError, ValueError) as error:
            self.problem(path, "repository", error)

    def document(self, path: Path) -> None:
        path = path.expanduser().absolute()
        self.dependencies(path)
        try:
            text = path.read_text(encoding="utf-8")
            self.documents[(path, text)] = FillDocument(path, text, str(path))
        except (OSError, UnicodeError) as error:
            self.problem(path, "document", error)

    def _repository_documents(self, common: Path) -> None:
        # --git-dir works for both a main checkout and its linked worktrees.
        def git(*arguments: str) -> str:
            done = subprocess.run(["git", "--git-dir", str(common), *arguments],
                                  capture_output=True, text=True, check=False)
            if done.returncode:
                raise ValueError(done.stderr.strip() or f"cannot read repository {common}")
            return done.stdout.strip()

        try:
            listing = git("worktree", "list", "--porcelain")
            worktrees = [Path(line.removeprefix("worktree ")) for line in listing.splitlines()
                         if line.startswith("worktree ")]
            for worktree in worktrees:
                self.manifest.add(f"worktree {worktree}")
                for path in sorted((worktree / "docs").rglob("*-production.md")):
                    self.document(path)
            # Take the newest committed copy of each path among all local tips.
            newest: dict[str, tuple[int, str]] = {}
            for branch in git("for-each-ref", "--format=%(refname)", "refs/heads").splitlines():
                self.manifest.add(f"branch {common}:{branch}")
                for relative in git("ls-tree", "-r", "--name-only", branch, "--", "docs").splitlines():
                    if not relative.endswith("-production.md"):
                        continue
                    stamp = int(git("log", "-1", "--format=%ct", branch, "--", relative))
                    if relative not in newest or stamp > newest[relative][0]:
                        newest[relative] = (stamp, branch)
            for relative, (_, branch) in newest.items():
                source = f"{common}:{branch}:{relative}"
                self.manifest.add(source)
                text = git("show", f"{branch}:{relative}")
                # A working copy already represents this committed text.
                if any(doc.text.strip() == text for doc in self.documents.values()):
                    continue
                path = (worktrees[0] / relative) if worktrees else common.parent / relative
                self.documents[(path, text)] = FillDocument(path, text, source)
        except (OSError, UnicodeError, ValueError) as error:
            self.problem(common, "repository", error)

    def collect(self) -> tuple[FillDocument, ...]:
        for source in self.sources.productions.values():
            path = source.timer.doc.expanduser().absolute()
            self.documents[(path, source.document)] = FillDocument(path, source.document, str(path))
            self.dependencies(path)
            self.repository(path)
            try:
                for cells in production_units.unit_table(source.document.splitlines()):
                    path_text = production_units.cell_value(cells.get("Worktree", ""))
                    if path_text and Path(path_text).expanduser().is_dir():
                        self.repository(Path(path_text).expanduser())
            except production_units.Refusal as error:
                self.problem(path, source.timer.slug, error)
        for path_text in tuple(self.dependencies.paths):
            path = Path(path_text)
            if path.name.endswith("-production.md") and not any(doc.path == path for doc in self.documents.values()):
                self.document(path)
                self.repository(path)
        for row in self.record.rows:
            location = row.worktree if row.kind == "unit" else row.doc
            if isinstance(location, (staff.PresentWorktree, staff.LiveDoc)):
                if location.machine == _fill_machine():
                    self.repository(location.path)
                    if isinstance(location, staff.LiveDoc):
                        self.document(location.path)
        state = Path(os.environ.get("SHOWRUNNER_STATE_DIR") or showrunners.SHOWRUNNER_STATE_DIR)
        slugs: set[str] = set()
        for folder in sorted(state.glob("*")):
            if not folder.is_dir():
                continue
            slugs.add(folder.name)
            self.manifest.add(f"showrunner {folder}")
            # A state folder may itself live inside a checkout or link to one.
            if (folder.resolve() / ".git").exists():
                self.repository(folder.resolve())
        config = Path(os.environ.get("SHOWRUNNERS_CONFIG") or showrunners.CONFIG)
        self.dependencies(config)
        if config.exists():
            try:
                settings = _object(cast(object, json.loads(config.read_text(encoding="utf-8"))))
                always = settings.get("always", [])
                if not isinstance(always, list) or any(not isinstance(item, str) for item in cast(list[object], always)):
                    raise ValueError("always must list production slugs")
                slugs.update(cast(list[str], always))
            except (OSError, UnicodeError, ValueError) as error:
                self.problem(config, "showrunners", error)
        for common in sorted(self.repositories):
            self._repository_documents(common)
        found_slugs = {document.slug for document in self.documents.values()}
        for slug in sorted(slugs - found_slugs):
            self.problem(state / slug, slug, "no source holds its production doc")
        return tuple(self.documents.values())


def _fill_machine() -> str:
    # Use the same machine names as shutdown records, without a second host rule.
    return remote.local_machine()


def _fill_config(cells: dict[str, str]) -> unit_config.UnitConfig:
    nightly_unit = cast(str, import_module("scripts.production.add_unit").NIGHTLY_REVIEW_UNIT)

    plan_text = production_units.cell_value(cells.get("Plan", ""))
    if not plan_text:
        raise ValueError("missing Plan cell")
    plan = (unit_config.StandbyPlan() if plan_text == "standby" else
            unit_config.PermanentPlan() if plan_text == "permanent" else unit_config.PlanFile(Path(plan_text)))
    port_text = production_units.cell_value(cells.get("Port", ""))
    port: unit_config.AppPort | unit_config.NoAppPort = unit_config.NoAppPort()
    if port_text and port_text not in ("—", "-", "none"):
        if not port_text.isdigit():
            raise ValueError("invalid Port cell")
        port = unit_config.AppPort(int(port_text))
    unit = production_units.cell_value(cells.get("Unit", ""))
    duties = ("nightly-review",) if unit_names.unit_id(unit) == nightly_unit else ()
    return unit_config.UnitConfig(plan, port, duties)


def _fill_unit(document: FillDocument, cells: dict[str, str], row: staff.StaffRow,
               machine: str, line: int) -> FillUnit:
    path_text = production_units.cell_value(cells.get("Worktree", ""))
    branch = production_units.cell_value(cells.get("Branch", ""))
    if not path_text or path_text in ("—", "-"):
        raise ValueError("missing Worktree cell")
    worktree = Path(path_text).expanduser()
    if not worktree.is_absolute():
        raise ValueError("Worktree cell must be absolute")
    if not branch:
        raise ValueError("missing Branch cell")
    actual = _fill_git(worktree, "symbolic-ref", "--quiet", "--short", "HEAD")
    root = Path(_fill_git(worktree, "rev-parse", "--show-toplevel"))
    if root.resolve() != worktree.resolve():
        raise ValueError("Worktree cell is not a git worktree root")
    if actual != branch:
        raise ValueError(f"worktree is on branch {actual}, expected {branch}")
    return FillUnit(row, staff.PresentWorktree(machine, worktree), _fill_config(cells),
                    f"{document.path}:{line}")


def _fill_doc_location(document: FillDocument, machine: str) -> staff.LiveDoc:
    path = document.path
    lines = document.text.splitlines()
    try:
        checkout_text = production_units.production_field(lines, "Showrunner checkout")
    except production_units.Refusal:
        # Older documents may name only the timer's explicit path.
        pass
    else:
        checkout = Path(checkout_text).expanduser()
        if not checkout.is_absolute():
            raise ValueError("Showrunner checkout must be absolute")
        root = Path(_fill_git(document.path.parent, "rev-parse", "--show-toplevel"))
        path = checkout / document.path.relative_to(root)
        branch = production_units.production_field(lines, "Merge branch")
        actual = _fill_git(checkout, "symbolic-ref", "--quiet", "--short", "HEAD")
        if actual != branch:
            raise ValueError(f"showrunner checkout is on branch {actual}, expected {branch}")
    if not path.is_file():
        raise ValueError(f"live production doc is missing: {path}")
    return staff.LiveDoc(machine, path)


def fill(*, dry_run: bool = False) -> int:
    """Fill complete live rows and files; retain every missing fact for the director."""
    with staff.operation():
        record = staff.snapshot()
        dependencies = staff.RecordInputs(set(record.inputs))
        sources = _member_sources(dependencies=dependencies)
        scan = _FillSources(record, sources, dependencies)
        documents = scan.collect()
        machine = _fill_machine()
        units: dict[staff.StaffId, FillUnit] = {}
        rejected: set[staff.StaffId] = set()
        seen: set[staff.StaffId] = set()
        docs: dict[staff.StaffId, staff.LiveDoc] = {}
        doc_evidence: dict[staff.StaffId, FillDocument] = {}
        rejected_docs: set[staff.StaffId] = set()
        # Discovery-only docs use the same matcher as timers, without another read.
        productions = dict(sources.productions)
        for document in documents:
            if document.slug not in productions:
                timer = showrunners.Timer(document.slug, "session:unrecorded", document.path, "")
                productions[document.slug] = showrunners.ShowrunnerSource(timer, document.text)
        joined = _MemberSources(productions, sources.launches)
        for document in documents:
            try:
                from scripts.production.tidy_wrapped import WRAPPED

                if WRAPPED in document.text or re.search(r"(?im)^- \*\*Status:\*\*\s*wrapped\b", document.text):
                    continue
                owner = joined.member(ShowrunnerMember(document.slug), record)
                if isinstance(owner, NoSuchMember):
                    scan.problem(document.source, document.slug, "no current showrunner staff row")
                    continue
                try:
                    location = _fill_doc_location(document, machine)
                    current_doc = docs.get(owner.row.id)
                    if current_doc is not None:
                        previous_doc = doc_evidence[owner.row.id]
                        if current_doc != location or previous_doc.text.strip() != document.text.strip():
                            raise ValueError(f"conflicting production documents: {previous_doc.source} "
                                             + f"({current_doc.path}) and {document.source} ({location.path})")
                    else:
                        docs[owner.row.id] = location
                        doc_evidence[owner.row.id] = document
                    if not isinstance(owner.row.doc, staff.DocUnfilled) and owner.row.doc != location:
                        raise ValueError(f"existing doc {owner.row.doc!r} conflicts with document location {location!r}")
                except (OSError, ValueError) as error:
                    rejected_docs.add(owner.row.id)
                    scan.problem(document.source, document.slug, error)
                lines = document.text.splitlines()
                cells_list = production_units.unit_table(lines)
                _, raw_rows = production_units.unit_rows(lines)
                search_from = lines.index("## Units")
                for cells, raw in zip(cells_list, raw_rows, strict=True):
                    line = lines.index(raw, search_from) + 1
                    search_from = line
                    name = production_units.cell_value(cells.get("Unit", ""))
                    selected: MemberFound | NoSuchMember = NoSuchMember(UnitMember(document.slug, name))
                    try:
                        selected = joined.member(UnitMember(document.slug, name), record)
                        if isinstance(selected, MemberFound):
                            seen.add(selected.row.id)
                        if production_units.plan_cell_is_retired(cells.get("Plan", "")):
                            continue
                        if isinstance(selected, NoSuchMember):
                            raise ValueError(f"no current unit staff row for {name}")
                        candidate = _fill_unit(document, cells, selected.row, machine, line)
                        previous = units.get(candidate.row.id)
                        if previous is not None and (previous.worktree, previous.config) != (candidate.worktree, candidate.config):
                            raise ValueError(f"conflicting unit facts with {previous.source}")
                        units[candidate.row.id] = candidate
                    except (OSError, ValueError, staff.StaffUnreadable) as error:
                        if isinstance(selected, MemberFound):
                            rejected.add(selected.row.id)
                        scan.problem(f"{document.path}:{line}", name, error)
            except (OSError, ValueError, production_units.Refusal, staff.StaffUnreadable) as error:
                scan.problem(document.source, document.slug, error)
        for row in record.find(kind="unit"):
            if row.parent is not None and row.parent in docs and row.id not in seen:
                scan.problem(docs[row.parent].path, row.name, "no Units row supplies its worktree and plan")
        for candidate in units.values():
            dependencies(candidate.worktree.path / ".unit.toml")
        for source in sorted(dependencies.paths | scan.manifest):
            print(f"source {source}")
        changed = False
        for identity, doc in sorted(docs.items()):
            if identity in rejected_docs:
                continue
            row = record.by_id(identity)
            if row is not None and isinstance(row.doc, staff.DocUnfilled):
                changed = True
                if not dry_run:
                    _ = staff.set_doc(identity, doc)
        for identity, candidate in sorted(units.items()):
            if identity in rejected:
                continue
            try:
                if not isinstance(candidate.row.worktree, staff.WorktreeUnfilled) and candidate.row.worktree != candidate.worktree:
                    raise ValueError(f"existing worktree {candidate.row.worktree!r} conflicts with document location {candidate.worktree!r}")
                current = unit_config.read(candidate.worktree.path, cleanup=False)
                if not isinstance(current, unit_config.ConfigMissing) and current != candidate.config:
                    raise ValueError("existing .unit.toml conflicts with document facts")
                # Check ignore policy before any staff mutation. Dry run never edits excludes.
                git_exclude.require_untracked_ignored(candidate.worktree.path, ".unit.toml",
                                                     planned_pattern="/.unit.toml")
                if not dry_run:
                    git_exclude.ensure_excluded(candidate.worktree.path, "/.unit.toml")
                    if candidate.row.worktree != candidate.worktree:
                        _ = staff.set_worktree(identity, candidate.worktree)
                    unit_config.write(candidate.worktree.path, candidate.config)
                changed |= candidate.row.worktree != candidate.worktree or isinstance(current, unit_config.ConfigMissing)
            except (OSError, ValueError, staff.StaffRefused, staff.StaffUnreadable) as error:
                scan.problem(candidate.source, candidate.row.name, error)
        for problem in scan.problems:
            print(problem, file=sys.stderr)
        if not changed:
            print("unchanged")
        return 1 if scan.problems else 0


class _Arguments(argparse.Namespace):
    command: str = ""
    dry_run: bool = False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("command", choices=["seed", "fill"])
    _ = parser.add_argument("--dry-run", action="store_true")
    arguments = _Arguments()
    _ = parser.parse_args(argv, namespace=arguments)

    def run() -> int:
        if arguments.command == "fill":
            return fill(dry_run=arguments.dry_run)
        seed(dry_run=arguments.dry_run)
        return 0

    return staff.cli_result(run)


if __name__ == "__main__":
    raise SystemExit(main())
