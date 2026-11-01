#!/usr/bin/env python3
"""Match production members and import missing staff once; remove this importer on 2026-11-13."""

from __future__ import annotations

import argparse
from contextlib import nullcontext
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
# The public batch reader drops failures; share its decoder within this repair's
# two-file boundary instead of copying its timer parser.
from scripts.production.showrunners import (  # noqa: E402
    _fields as _timer_fields,  # pyright: ignore[reportPrivateUsage]
    _source as _source,  # pyright: ignore[reportPrivateUsage]
)
import unit_names  # noqa: E402
from scripts.production import staff, unit_config  # noqa: E402
from scripts.production.worker_admission import parent as worker_parent, showrunner_member  # noqa: E402
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
from scripts.production import unit_lookup  # noqa: E402


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
        self.unreadable: set[Path] = set()
        self.skipped_runs: set[Path] = set()
        self.skipped_ledgers: set[Path] = set()
        self.run_activity: dict[Path, bool] = {}
        self.live_worker_runs: set[Path] = set()
        self.activity_unavailable: dict[Path, ValueError] = {}
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
        self.registry_incomplete: bool = bool(registry.unreadable)
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
                self.unreadable.add(path)
                self.objects[path] = {}
        return self.objects[path]

    def owner(self, selector: ShowrunnerMember | UnitMember) -> WorkerOwner:
        found = (showrunner_member(self.record, self.sources, selector.slug, dependencies=self.dependencies)
                 if isinstance(selector, ShowrunnerMember) else self.sources.member(selector, self.record))
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
            except (ValueError, staff.StaffUnreadable) as error:
                _problem(source.timer.doc, error)
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
            if (row is None or row.kind not in ("unit", "showrunner", "worker")) and environment.get("SHOWRUNNER_UNIT_ID"):
                try:
                    owner = self.recorded_owner(staff_id=staff_id, unit=environment["SHOWRUNNER_UNIT_ID"],
                                                production=environment.get("SHOWRUNNER_UNIT", ""))
                except (ValueError, staff.StaffUnreadable) as error:
                    _problem(f"launch {launch['session_id']}", error)
                    continue
                self.sessions[launch["session_id"]] = owner
                if isinstance(owner.match, staff.StaffRow):
                    self.identities[owner.match.id] = {"claude": launch["session_id"]}
                continue
            if row is not None:
                self.identities[row.id] = {"claude": launch["session_id"]}
                if row.kind != "worker":
                    self.sessions[launch["session_id"]] = WorkerOwner(f"staff:{row.id}", row)

    def recorded_owner(self, *, staff_id: str = "", production: str = "", unit: str = "") -> WorkerOwner:
        selected = worker_parent(self.record, self.sources, staff_id=staff_id,
                                 production=production, unit=unit, pending=tuple(self.members.values()))
        if isinstance(selected, staff.StaffRow):
            return WorkerOwner(f"staff:{selected.id}", selected)
        return WorkerOwner(selected.key, staff.NewSeedMember())

    def _record_run_owner(self, run: Path) -> WorkerOwner:
        director = unit_runs.director(run, dependencies=self.dependencies)
        if director in self.sessions:
            return self.sessions[director]
        state = self.read(run / workers.STATE)
        slug, unit = _text(state.get("production")), _text(state.get("unit"))
        if slug or unit:
            return self.recorded_owner(production=slug, unit=unit)
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

    def ledger_owner(self, path: Path, worker: workers.UnitWorker, fields: Mapping[str, object],
                     owner: WorkerOwner | None) -> tuple[str, WorkerOwner]:
        current = self.record.by_id(staff.StaffId(_text(fields.get("staff_id")) or worker.name))
        if current is not None and current.kind == "worker" and current.parent is not None:
            row = self.record.by_id(current.parent)
            if row is not None:
                return current.name, WorkerOwner(f"staff:{row.id}", row)
        if owner is not None:
            return worker.name, owner
        try:
            return worker.name, self.owner(UnitMember(path.parent.name, path.stem))
        except ValueError:
            # An adhoc stem can name a worktree rather than a unit.
            observed = ObservedUnit(path.stem)
            found = (self.sources.member(observed, self.record)
                     if unit_names.NAME.fullmatch(path.stem) else NoSuchMember(observed))
            if isinstance(found, MemberFound):
                return worker.name, WorkerOwner(f"staff:{found.row.id}", found.row)
            if isinstance(worker.ownership, workers.HeldBy):
                return worker.name, self._run_owner(Path(worker.ownership.run))
            raise ValueError("worker ledger has no current staff parent")

    def paused_director_live(self, path: Path, parent: WorkerOwner | None) -> bool:
        """A cached live identity proves life; negative evidence needs a successful mark lookup."""
        row = parent.match if parent is not None else staff.NewSeedMember()
        if isinstance(row, staff.StaffRow):
            identity = self.identities.get(row.id, {})
            if identity.get("claude") in self.running:
                return True
            if row.kind == "unit":
                slug = unit_lookup.row_production(row, self.record, self.sources).slug
                unit = unit_names.unit_id(row.name)
            elif row.kind == "showrunner":
                if self.registry_incomplete:
                    raise ValueError("director registry is incomplete")
                if any(owner.match == row for owner in self.sessions.values()):
                    return False
                raise ValueError("showrunner director identity is unavailable")
            else:
                raise ValueError("worker ledger has no director parent")
        else:
            slug, unit = path.parent.name, path.stem
            if not unit_names.NAME.fullmatch(unit):
                raise ValueError("worker ledger director identity is unavailable")
        marked = unit_lookup.marked_unit(slug, unit, record=self.record, sources=self.sources,
                                         running=tuple(self.running.values()))
        if marked is not None and isinstance(marked.claude, unit_lookup.LiveClaude):
            return True
        if marked is not None and isinstance(marked.claude, unit_lookup.ClaudeUnknown):
            raise ValueError(marked.claude.reason)
        if self.registry_incomplete:
            raise ValueError("director registry is incomplete")
        return False

    def paused_current(self, path: Path, worker: workers.UnitWorker, parent: WorkerOwner | None) -> bool:
        """Paused ownership has no run; its director or historical worker processes may still live."""
        failures: list[Exception] = []
        try:
            if self.paused_director_live(path, parent):
                return True
        except (OSError, ValueError, staff.StaffUnreadable) as error:
            failures.append(error)
        backend = worker.backend
        runs = {Path(event.run) for event in worker.events}
        if isinstance(backend, workers.ClaudeSession):
            if backend.session_id in self.running:
                self.live_worker_runs.update(runs)
                return True
            if self.registry_incomplete:
                failures.append(ValueError("Claude registry is incomplete"))
        else:
            if not runs:
                failures.append(ValueError("paused Codex worker has no recorded event run"))
            for run in sorted(runs):
                try:
                    roster = self.checked(run / workers.ROSTER) if (run / workers.ROSTER).exists() else {}
                    entry = _object(roster.get(worker.name, {}))
                    if self.worker_live(run, {**entry, "family": worker.family, "thread_id": backend.thread_id}):
                        self.live_worker_runs.add(run)
                        return True
                except (OSError, ValueError) as error:
                    failures.append(error)
        if failures:
            failure = ValueError(str(failures[0]))
            for run in runs:
                self.activity_unavailable[run] = failure
            raise failure
        return False

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
        inactive = 0
        relevant = 0
        unresolved = False
        for key, value in entries.items():
            try:
                fields = _object(value)
                name = _text(fields.get("name")) or key
                # Reuse the ledger's decoder; its public locked reader also writes and recovers.
                worker = workers._decode_worker(name, fields)  # pyright: ignore[reportPrivateUsage]
                if not isinstance(worker.ownership, (workers.Retired, workers.Adopted)):
                    relevant += 1
                if isinstance(worker.ownership, workers.HeldBy):
                    run = Path(worker.ownership.run)
                    roster = self.checked(run / workers.ROSTER) if (run / workers.ROSTER).exists() else {}
                    entry = _object(roster.get(name, {}))
                    backend = worker.backend
                    activity: dict[str, object] = {**entry, "family": worker.family}
                    if isinstance(backend, workers.CodexThread):
                        activity["thread_id"] = backend.thread_id
                    else:
                        activity["session_id"] = backend.session_id
                        activity["worker_id"] = backend.session_id
                    if not self.current_worker(run, activity):
                        inactive += 1
                        self.runs.add(run)
                        continue
                    self.live_worker_runs.add(run)
                try:
                    name, parent = self.ledger_owner(
                        path, worker, {**fields, "staff_id": _text(fields.get("staff_id")) or key}, owner)
                except (ValueError, staff.StaffRefused, staff.StaffUnreadable, OSError):
                    if isinstance(worker.ownership, (workers.Retired, workers.Adopted)):
                        continue
                    if isinstance(worker.ownership, workers.Paused) and not self.paused_current(path, worker, None):
                        inactive += 1
                        continue
                    raise
                if isinstance(worker.ownership, workers.Paused) and not self.paused_current(path, worker, parent):
                    inactive += 1
                    continue
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
            except (ValueError, staff.StaffRefused, workers.WorkerUnavailable, OSError, staff.StaffUnreadable) as error:
                unresolved = True
                _problem(f"{path} worker {key}", error)

        if relevant and inactive == relevant and not unresolved and path.absolute() not in self.unreadable:
            self.skipped_ledgers.add(path.absolute())

    def checked(self, path: Path) -> dict[str, object]:
        data = self.read(path)
        if path.absolute() in self.unreadable:
            raise ValueError(f"{path}: current activity is unavailable")
        return data

    def director_live(self, run: Path) -> bool:
        if run not in self.directors:
            self.directors[run] = workers.resolve_live_unit_director(
                run, self.record, self.sources, tuple(self.running.values()), self.dependencies)
        live = isinstance(self.directors[run], workers.LiveUnitDirector)
        if not live and self.registry_incomplete:
            raise ValueError(f"{run}: director registry is incomplete")
        return live

    def worker_live(self, run: Path, fields: Mapping[str, object]) -> bool:
        """Use the Claude registry or a Codex process/loaded thread, never a saved status."""
        family = _text(fields.get("family")) or "codex"
        if family == "claude":
            session = _text(fields.get("worker_id")) or _text(fields.get("session_id"))
            if not session:
                raise ValueError("Claude worker session is unavailable")
            if session in self.running:
                return True
            if self.registry_incomplete:
                raise ValueError(f"{session}: Claude registry is incomplete")
            return False
        if _alive(fields.get("launcher_pid")):
            return True
        thread = _text(fields.get("thread_id"))
        if not thread:
            raise ValueError("Codex worker thread is unavailable")
        return self.codex_thread_live(run, thread)

    def codex_thread_live(self, run: Path, thread: str) -> bool:
        # This coarse display/seed check can keep a stale worker while its server lives.
        del thread
        current = run / "mesh_server.json"
        if current.exists() and _alive(self.checked(current).get("pid")):
            return True
        retired = run / "mesh_retired.json"
        if retired.exists():
            servers = self.checked(retired).get("servers")
            if not isinstance(servers, list):
                raise ValueError(f"{retired}: invalid retired server list")
            return any(_alive(_object(entry).get("pid")) for entry in cast(list[object], servers))
        return False

    def current_worker(self, run: Path, fields: Mapping[str, object]) -> bool:
        """Positive worker or director evidence wins over uncertainty in the other."""
        try:
            failure: ValueError | None = None
            try:
                if self.director_live(run):
                    return True
            except ValueError as error:
                failure = error
            if self.worker_live(run, fields):
                return True
            if failure is not None:
                raise failure
            return False
        except ValueError as error:
            self.activity_unavailable[run] = error
            raise

    def run_live(self, run: Path) -> bool:
        """Prove activity before ownership; uncertain evidence is never an inactive skip."""
        if run in self.run_activity:
            return self.run_activity[run]
        live = run in self.live_worker_runs
        failures = ([self.activity_unavailable[run]] if run in self.activity_unavailable else [])
        try:
            live = self.director_live(run) or live
        except ValueError as error:
            failures.append(error)

        def observe(fields: Mapping[str, object]) -> None:
            nonlocal live
            if not live:
                try:
                    live = self.worker_live(run, fields)
                except ValueError as error:
                    failures.append(error)
        roster = run / workers.ROSTER
        if roster.exists():
            for value in self.checked(roster).values():
                entry = _object(value)
                if entry.get("status") in ("starting", "running", "paused", "waiting_capacity"):
                    observe(entry)
        for path in sorted(run.glob("impl_worker_*.json")):
            fields = self.checked(path)
            if fields.get("family") == "claude":
                observe(fields)
        ledger = run / unit_runs.WORKERS_LOG
        if ledger.exists():
            self.dependencies(ledger)
            for line in ledger.read_text(encoding="utf-8").splitlines():
                short, separator, _name = line.partition("\t")
                if not separator or not short:
                    raise ValueError(f"{ledger}: invalid Claude workers ledger entry")
                matches = [session for session in self.running if session.startswith(short)]
                if len(matches) > 1:
                    raise ValueError(f"{ledger}: ambiguous Claude session prefix {short}")
                if not matches and self.registry_incomplete:
                    failures.append(ValueError(f"{ledger}: Claude registry is incomplete"))
                live = bool(matches) or live
        if not live and failures:
            raise failures[0]
        self.run_activity[run] = live
        if not live:
            self.skipped_runs.add(run.absolute())
        return live

    def roster(self, run: Path, owner: WorkerOwner | None = None) -> None:
        path = run / workers.ROSTER
        if not self.run_live(run):
            return
        for name, value in self.read(path).items():
            try:
                entry = _object(value)
                if entry.get("status") not in ("starting", "running", "paused", "waiting_capacity"):
                    continue
                if not self.current_worker(run, entry):
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
            except (ValueError, OSError, staff.StaffRefused, staff.StaffUnreadable) as error:
                _problem(f"{path} worker {name}", error)

    def claude_workers(self, run: Path) -> None:
        ledger = run / unit_runs.WORKERS_LOG
        launches = sorted(run.glob("impl_worker_*.json"))
        if not ledger.exists() and not launches:
            return
        try:
            if not self.run_live(run):
                return
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
        except (ValueError, OSError, KeyError, staff.StaffRefused, staff.StaffUnreadable) as error:
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
                run = Path(lifetime.session_dir)
                if not self.run_live(run):
                    return
                entry = _object(self.checked(run / workers.ROSTER).get(lifetime.worker))
                if entry.get("status") not in ("starting", "running", "paused", "waiting_capacity"):
                    return
                if not self.current_worker(run, entry):
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
            except (OSError, UnicodeError, ValueError, staff.StaffUnreadable) as error:
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


def _fill_worktree(cells: dict[str, str], machine: str) -> staff.PresentWorktree:
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
    return staff.PresentWorktree(machine, worktree)



def _fill_doc_location(document: FillDocument, machine: str) -> staff.LiveDoc:
    lines = document.text.splitlines()
    checkout = Path(production_units.production_field(lines, "Showrunner checkout")).expanduser()
    if not checkout.is_absolute():
        raise ValueError("Showrunner checkout must be absolute")
    root = Path(_fill_git(document.path.parent, "rev-parse", "--show-toplevel"))
    checkout_root = Path(_fill_git(checkout, "rev-parse", "--show-toplevel"))
    if checkout_root.resolve() != checkout.resolve():
        raise ValueError("Showrunner checkout is not a git worktree root")
    path = checkout / document.path.relative_to(root)
    branch = production_units.production_field(lines, "Merge branch")
    actual = _fill_git(checkout, "symbolic-ref", "--quiet", "--short", "HEAD")
    if actual != branch:
        raise ValueError(f"showrunner checkout is on branch {actual}, expected {branch}")
    if not path.is_file():
        raise ValueError(f"live production doc is missing: {path}")
    return staff.LiveDoc(machine, path)


@dataclass(frozen=True)
class _LiveUnitFill:
    row: staff.StaffRow
    worktree: staff.PresentWorktree
    config: unit_config.UnitConfig | unit_config.ConfigMissing
    source: str


@dataclass(frozen=True)
class _RetiredUnitDeferred:
    """Its old Units note belongs to the separate retired fill."""


class _LiveFillSources(_FillSources):
    """One live checkout per staff parent; other document copies are not evidence."""

    def __init__(self, record: staff.StaffSnapshot, sources: _MemberSources,
                 dependencies: staff.RecordInputs) -> None:
        super().__init__(record, sources, dependencies)
        registry = sessions.read_registry(dependencies=dependencies)
        self.running: dict[str, sessions.RegistryObservation] = {
            item.record["sessionId"]: item for item in registry.entries
            if sessions.running_session(item.record)
        }
        self.row_sessions: dict[staff.StaffId, list[sessions.RegistryObservation]] = {}
        # Use the same id-first join as every other membership reader.
        for launch in sources.launches:
            if launch.get("ended") or launch["session_id"] not in self.running:
                continue
            environment = launch["environment"]
            identity = environment.get("STAFF_ID", "")
            owner = record.by_id(staff.StaffId(identity)) if identity else None
            if owner is not None and owner.kind == "showrunner":
                self.row_sessions.setdefault(owner.id, []).append(self.running[launch["session_id"]])
                continue
            try:
                found = sources.member(ObservedUnit(environment.get("SHOWRUNNER_UNIT_ID", ""), identity), record)
            except (ValueError, staff.StaffUnreadable):
                continue
            if isinstance(found, MemberFound):
                self.row_sessions.setdefault(found.row.id, []).append(self.running[launch["session_id"]])
        self.timers: dict[staff.StaffId, list[showrunners.ShowrunnerSource]] = {}
        self.timer_errors: dict[staff.StaffId, str] = {}
        # The shared membership reader omits unreadable docs. Reuse its timer
        # decoder for omitted instances so failed evidence still identifies a row.
        for instance in sorted(showrunners.NOTIFIER_STATE_DIR.glob("showrunner-*")):
            slug = instance.name.removeprefix("showrunner-")
            if not instance.is_dir() or slug in sources.productions:
                continue
            # Keep the timer target available to the canonical launch-id join,
            # even when its document cannot be read. Empty document text supplies
            # no location evidence; only document_for may admit a live document.
            conf = instance / "conf"
            try:
                target = _timer_fields(conf, dependencies=dependencies).get("TARGET", "")
            except (OSError, UnicodeError):
                target = ""
            timer = showrunners.Timer(slug, target, conf, "")
            identity_sources = _MemberSources(
                {slug: showrunners.ShowrunnerSource(timer, "")}, sources.launches,
            )
            found = identity_sources.member(ShowrunnerMember(slug), record)
            if not isinstance(found, MemberFound):
                continue
            attempted: list[Path] = []

            def record_input(path: Path) -> None:
                dependencies(path)
                attempted.append(path)

            try:
                recovered = _source(instance, dependencies=record_input)
            except (OSError, UnicodeError, KeyError, ValueError) as error:
                self.timer_errors[found.row.id] = f"{attempted[-1]}: {error}"
            else:
                self.timers.setdefault(found.row.id, []).append(recovered)
        for slug, source in sources.productions.items():
            found = sources.member(ShowrunnerMember(slug), record)
            if isinstance(found, MemberFound):
                self.timers.setdefault(found.row.id, []).append(source)
                session = self.running.get(source.timer.target.removeprefix("session:"))
                if session is not None:
                    self.row_sessions.setdefault(found.row.id, []).append(session)

    def document_for(self, row: staff.StaffRow) -> FillDocument:
        candidates = self.timers.get(row.id, [])
        if row.id in self.timer_errors:
            raise ValueError(self.timer_errors[row.id])
        if not candidates:
            raise ValueError("no timer names its live production document")
        documents: dict[Path, FillDocument] = {}
        for source in candidates:
            pointer = FillDocument(source.timer.doc, source.document, str(source.timer.doc))
            location = _fill_doc_location(pointer, _fill_machine())
            self.dependencies(location.path)
            text = location.path.read_text(encoding="utf-8")
            document = FillDocument(location.path, text, str(location.path))
            # Validate the actual checkout's fields, rather than an older copy's Units table.
            if _fill_doc_location(document, _fill_machine()).path != location.path:
                raise ValueError(f"{location.path}: live document points to another checkout")
            documents[location.path.resolve()] = document
        if len(documents) != 1:
            raise ValueError("timers disagree on the live document: " + ", ".join(map(str, documents)))
        document = next(iter(documents.values()))
        common = _fill_git(document.path.parent, "rev-parse", "--path-format=absolute", "--git-common-dir")
        self.manifest.add(f"repository {common}")
        from scripts.production.tidy_wrapped import WRAPPED

        if WRAPPED in document.text or re.search(r"(?im)^- \*\*Status:\*\*\s*wrapped\b", document.text):
            raise ValueError(f"{document.path}: production is wrapped")
        return document

    def session_worktree(self, row: staff.StaffRow, machine: str) -> staff.PresentWorktree:
        folders = {item.details.get("cwd", "") for item in self.row_sessions.get(row.id, [])}
        folders.discard("")
        if not folders:
            raise ValueError("no verified live session supplies its working folder")
        if len(folders) != 1:
            raise ValueError("live sessions disagree on the working folder: " + ", ".join(sorted(folders)))
        folder = Path(next(iter(folders))).expanduser()
        if not folder.is_absolute():
            raise ValueError("live session working folder is not absolute")
        # A session may work in a subfolder of its unit's worktree.
        root = Path(_fill_git(folder, "rev-parse", "--show-toplevel"))
        return staff.PresentWorktree(machine, root)

    def session_unit(self, row: staff.StaffRow, machine: str, source: str) -> _LiveUnitFill:
        location = self.session_worktree(row, machine)
        self.dependencies(location.path / ".unit.toml")
        return _LiveUnitFill(row, location, unit_config.read(location.path, cleanup=False), source)

    def unit_for(self, row: staff.StaffRow, document: FillDocument, machine: str) -> _LiveUnitFill | _RetiredUnitDeferred:
        joined = _MemberSources({document.slug: showrunners.ShowrunnerSource(
            showrunners.Timer(document.slug, self.sources.productions[document.slug].timer.target
                              if document.slug in self.sources.productions else "session:unrecorded",
                              document.path, ""), document.text)}, self.sources.launches)
        lines = document.text.splitlines()
        try:
            cells_list = production_units.unit_table(lines)
            _, raw_rows = production_units.unit_rows(lines)
        except production_units.Refusal as table_error:
            try:
                return self.session_unit(row, machine, str(document.path))
            except (OSError, UnicodeError, ValueError) as session_error:
                raise ValueError(f"{document.path}: {table_error}; {session_error}") from session_error
        matches: list[tuple[dict[str, str], int]] = []
        start = lines.index("## Units")
        for cells, raw in zip(cells_list, raw_rows, strict=True):
            line = lines.index(raw, start) + 1
            start = line
            try:
                selected = joined.member(UnitMember(document.slug, production_units.cell_value(cells.get("Unit", ""))),
                                         self.record)
            except (ValueError, staff.StaffUnreadable):
                continue  # A stale table name with no staff identity is not a fill target.
            if isinstance(selected, MemberFound) and selected.row.id == row.id:
                matches.append((cells, line))
        if len(matches) > 1:
            raise ValueError(f"{document.path}: several Units rows identify {row.name}")
        if not matches:
            return self.session_unit(row, machine, str(document.path))
        cells, line = matches[0]
        if production_units.plan_cell_is_retired(cells.get("Plan", "")):
            return _RetiredUnitDeferred()
        try:
            location = _fill_worktree(cells, machine)
        except (OSError, ValueError) as error:
            try:
                location = self.session_worktree(row, machine)
            except (OSError, ValueError) as session_error:
                raise ValueError(f"{document.path}:{line}: {error}; {session_error}") from session_error
        try:
            config = _fill_config(cells)
        except ValueError as error:
            config = unit_config.ConfigMissing(str(error))
        return _LiveUnitFill(row, location, config, f"{document.path}:{line}")


def _fill_live(record: staff.StaffSnapshot, sources: _MemberSources,
               dependencies: staff.RecordInputs, *, dry_run: bool) -> int:
    scan = _LiveFillSources(record, sources, dependencies)
    machine = _fill_machine()
    documents: dict[staff.StaffId, FillDocument] = {}
    doc_errors: dict[staff.StaffId, str] = {}
    changed = False
    recovery_errors: list[str] = []
    for row in record.find(kind="showrunner"):
        if isinstance(row.doc, staff.WrappedDoc):
            continue
        if row.id not in scan.timers and row.id not in scan.timer_errors and row.id not in scan.row_sessions:
            continue
        try:
            document = scan.document_for(row)
            documents[row.id] = document
            location = staff.LiveDoc(machine, document.path)
            if isinstance(row.doc, staff.DocUnfilled):
                if not dry_run:
                    _ = staff.set_doc(row.id, location)
                changed = True
        except (OSError, UnicodeError, ValueError, production_units.Refusal, staff.StaffUnreadable, staff.StaffRefused) as error:
            doc_errors[row.id] = str(error)
            if isinstance(row.doc, staff.DocUnfilled):
                scan.problem(row.id, row.name, error)
    for row in record.find(kind="unit"):
        if isinstance(row.worktree, staff.RemovedWorktree):
            continue
        # A filled row needs no table re-interpretation. Finish an interrupted
        # row-then-file write only while that configuration is still missing.
        unfilled = isinstance(row.worktree, staff.WorktreeUnfilled)
        if isinstance(row.worktree, staff.PresentWorktree) and row.worktree.machine != machine:
            continue
        if row.parent not in documents and row.parent not in doc_errors and row.id not in scan.row_sessions:
            continue
        try:
            if isinstance(row.worktree, staff.PresentWorktree):
                dependencies(row.worktree.path / ".unit.toml")
                if not isinstance(unit_config.read(row.worktree.path, cleanup=False), unit_config.ConfigMissing):
                    continue
            document = documents.get(row.parent) if row.parent is not None else None
            if document is None:
                try:
                    candidate = scan.session_unit(row, machine, row.id)
                except (OSError, UnicodeError, ValueError) as session_error:
                    reason = doc_errors.get(row.parent, "") if row.parent is not None else ""
                    raise ValueError(f"{reason}; {session_error}" if reason else str(session_error)) from session_error
            else:
                candidate = scan.unit_for(row, document, machine)
            if isinstance(candidate, _RetiredUnitDeferred):
                continue
            if not unfilled and row.worktree != candidate.worktree:
                raise ValueError("configuration recovery evidence conflicts with the recorded worktree")
            dependencies(candidate.worktree.path / ".unit.toml")
            git_exclude.require_untracked_ignored(candidate.worktree.path, ".unit.toml",
                                                 planned_pattern="/.unit.toml")
            if isinstance(candidate.config, unit_config.ConfigMissing):
                if unfilled:
                    if not dry_run:
                        _ = staff.set_worktree(row.id, candidate.worktree)
                    changed = True
                    print(f"configuration {row.id} {row.name}: {candidate.config.reason}")
                else:
                    raise ValueError(candidate.config.reason)
                continue
            current = unit_config.read(candidate.worktree.path, cleanup=False)
            if not isinstance(current, unit_config.ConfigMissing) and current != candidate.config:
                raise ValueError("existing .unit.toml conflicts with the owning document")
            if not dry_run:
                git_exclude.ensure_excluded(candidate.worktree.path, "/.unit.toml")
                if unfilled:
                    _ = staff.set_worktree(row.id, candidate.worktree)
                unit_config.write(candidate.worktree.path, candidate.config)
            changed |= unfilled or isinstance(current, unit_config.ConfigMissing)
        except (OSError, UnicodeError, ValueError, production_units.Refusal, staff.StaffUnreadable, staff.StaffRefused) as error:
            if unfilled:
                scan.problem(row.id, row.name, error)
            else:
                recovery_errors.append(f"recovery {row.id} {row.name}: {str(error).replace(chr(10), ' ')}")
    if changed or dry_run or scan.problems or recovery_errors:
        for source in sorted(dependencies.paths | scan.manifest):
            print(f"source {source}")
    for problem in (*scan.problems, *recovery_errors):
        print(problem, file=sys.stderr)
    if not changed and not scan.problems and not recovery_errors:
        print("unchanged")
    return 1 if scan.problems or recovery_errors else 0


def fill(*, dry_run: bool = False) -> int:
    """Fill outward from staff identities; preview never takes a write lock."""
    with nullcontext() if dry_run else staff.operation():
        record = staff.snapshot()
        dependencies = staff.RecordInputs(set(record.inputs))
        sources = _member_sources(dependencies=dependencies)
        return _fill_live(record, sources, dependencies, dry_run=dry_run)


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
