#!/usr/bin/env python3
"""Resolve staff membership and observe native worker identities."""

from __future__ import annotations

from contextlib import redirect_stderr
import io
import json
import os
import shlex
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
import sys
from pathlib import Path
from typing import Literal, cast, override

# These source modules still use sibling imports; keep their existing module identity.

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scripts.production import showrunners  # noqa: E402
from scripts.production.production_doc import Production  # noqa: E402
import unit_names  # noqa: E402
from scripts.production import staff, unit_config, session_staff  # noqa: E402
from scripts.production.worker_admission import UnpublishedWorker, parent as worker_parent, showrunner_member  # noqa: E402
from scripts.direct import workers  # noqa: E402
from scripts.astra import astra_roles, design_roles  # noqa: E402
from scripts.lib import unit_runs  # noqa: E402
from scripts.message import sessions  # noqa: E402
from scripts.shutdown import remote  # noqa: E402


# Matchers live in unit_lookup, so lower readers use the same join without an import cycle.
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


def ensure_staff_showrunner(child: Production, caller: str = "", *, runtime_id: str = "") -> staff.StaffRow:
    record = staff.initial_snapshot()
    sources = _member_sources()
    found = sources.member(ShowrunnerMember(child.slug), record)
    location = staff.LiveDoc(remote.local_machine(), child.doc.resolve())
    if isinstance(found, MemberFound):
        if runtime_id and found.row.current_session != staff.native_id(runtime_id):
            raise staff.StaffRefused(f"{child.slug}: native session conflicts with retained staff identity")
        if found.row.doc == location:
            return found.row
        return staff.ensure("showrunner", name=found.row.name, parent=found.row.parent, doc=location)
    parent = sources.member(ShowrunnerMember(caller), record)
    parent_doc = showrunners.timer_docs().get(caller)
    if parent_doc is not None:
        existing = parent.row if isinstance(parent, MemberFound) else None
        parent_location = staff.LiveDoc(remote.local_machine(), parent_doc.resolve())
        if existing is None or existing.doc != parent_location:
            source = sources.productions.get(caller)
            if existing is None and source is None:
                raise staff.StaffRefused(f"{caller}: native showrunner identity is not proved by its timer")
            parent = MemberFound(staff.ensure(
                "showrunner", name=existing.name if existing else showrunners.herdr_space(caller, str(parent_doc)),
                parent=existing.parent if existing else None, doc=parent_location,
                runtime_id=existing.current_session if existing else source.timer.target.removeprefix("session:") if source else ""))
    if isinstance(parent, NoSuchMember):
        if caller:
            raise staff.StaffRefused(f"{child.slug}: no staff parent or timer document for {caller}")
        return staff.ensure("showrunner", name=showrunners.herdr_space(child.slug, str(child.doc)), doc=location, runtime_id=runtime_id)
    # Admission records the live doc; attach_parent records the edge and its
    # routing notices together after the showrunner appears.
    return staff.ensure("showrunner", name=showrunners.herdr_space(child.slug, str(child.doc)), doc=location, runtime_id=runtime_id)


def showrunner_members(record: staff.StaffSnapshot) -> dict[str, staff.StaffRow]:
    """Resolve local production slugs to rows with the same launch-first matching rule."""
    return _member_sources().showrunner_members(record)


def unit_parent(unit: str, session_id: str = "") -> UnitProduction:
    """Resolve the current staff edge; old launch marks supply identity only."""
    reference = session_staff.StoredReference(session_id) if session_id else session_staff.BY_NAME
    return unit_lookup.unit_parent(unit, reference, record=staff.snapshot(), sources=_member_sources())


LiveRole = Literal["showrunner", "unit", "worker", "agent", "impl", "test", "review", "fix", "astra-designer", "astra-reviewer"]


@dataclass(frozen=True)
class WorkerOwner:
    key: str
    match: staff.StaffRow


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


def is_non_staff_note(message: str) -> bool:
    """Identify a diagnostic that excludes an agent from staff."""
    return message.endswith("; not staff")


def codex_thread_host(arguments: list[str]) -> bool:
    """An app-server hosts threads; its inherited staff mark is not a session."""
    index = 2 if arguments and Path(arguments[0]).name in ("node", "nodejs") else 1
    while index < len(arguments):
        option = arguments[index]
        if option in ("-c", "--config", "--enable", "--disable", "--remote",
                      "--remote-auth-token-env", "--local-provider", "-m", "--model",
                      "-p", "--profile", "-s", "--sandbox", "-a", "--ask-for-approval",
                      "-C", "--cd", "--add-dir", "-i", "--image"):
            index += 2
        elif option.startswith("-") and option != "--":
            index += 1
        else:
            return option == "app-server"
    return False



def pane_holds_staff_member(record: staff.StaffSnapshot, sources: _MemberSources, pane: str) -> bool:
    """Join the pane's retained identity or unit labels to the supplied staff snapshot."""
    for launch in sources.launches:
        if launch.get("pane_id") != pane or launch.get("ended"):
            continue
        if record.by_member_reference(launch["session_id"]) is not None:
            return True
        environment = launch["environment"]
        production = environment.get("SHOWRUNNER_UNIT", "")
        unit = environment.get("SHOWRUNNER_UNIT_ID", "")
        if production and unit:
            found = sources.member(UnitMember(production, unit), record)
            if isinstance(found, MemberFound):
                return True
    return False


def codex_non_staff_reason(record: staff.StaffSnapshot, sources: _MemberSources,
                           environment: Mapping[str, str]) -> str:
    """Exclude a process without a thread only when its pane has no staff owner."""
    if environment.get("CODEX_THREAD_ID", ""):
        return ""
    pane = environment.get("HERDR_PANE_ID", "")
    if not pane:
        return "not in a herdr pane; not staff"
    if not pane_holds_staff_member(record, sources, pane):
        return "pane holds no staff member; not staff"
    return ""


@dataclass(frozen=True)
class PendingPublicationEvidence:
    """Read-only backend publications and every damaged inbox, in pathname order."""

    workers: tuple[UnpublishedWorker, ...]
    diagnostics: tuple[str, ...]


def pending_publication_evidence(run: Path) -> PendingPublicationEvidence:
    """Read all inboxes without invoking the writer's empty-file recovery."""
    from scripts.agents import worker_lifecycle

    pending: list[UnpublishedWorker] = []
    diagnostics: list[str] = []
    for path in sorted(run.glob("*.pending.json")):
        try:
            text = path.read_text(encoding="utf-8")
            value: object = cast(object, json.loads(text)) if text else dict[str, object]()
            fields = worker_lifecycle.validate_worker_state(path, value)
            raw = fields.get("unpublished_thread")
            if raw is None:
                continue
            if not isinstance(raw, dict):
                raise staff.StaffUnreadable(f"{path}: invalid unfinished backend evidence")
            backend = cast(dict[str, object], raw)
            native, parent = backend.get("id"), backend.get("parent")
            if not isinstance(native, str) or not isinstance(parent, str):
                raise staff.StaffUnreadable(f"{path}: unfinished backend needs native identity and parent")
            pending.append(UnpublishedWorker(path, staff.native_id(native), staff.native_id(parent)))
        except (OSError, UnicodeError, ValueError) as error:
            diagnostics.append(str(error))
    return PendingPublicationEvidence(tuple(pending), tuple(diagnostics))


class _WorkerSources:
    """Read worker evidence for one command."""

    def __init__(self, record: staff.StaffSnapshot, sources: _MemberSources,
                 *,
                 dependencies: staff.RecordInputs | None = None) -> None:
        self.record: staff.StaffSnapshot = record
        self.sources: _MemberSources = sources
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
        self.claude_registry_incomplete: bool = bool(registry.unreadable)
        self.codex_scan_incomplete: bool = False
        for path in registry.unreadable:
            if path.exists():
                _problem(path, "session registry is unreadable")
        self.running: dict[str, sessions.SessionRecord] = {
            session["sessionId"]: session for session in workers.running_director_sessions(registry)
        }
        self.native_running: dict[str, dict[str, str]] = {
            identity: {"claude": identity} for identity in self.running}
        from scripts.message import process_table
        import herdr
        try:
            for process in process_table.snapshot().values():
                try:
                    arguments = shlex.split(process.arguments)
                except ValueError as error:
                    if not herdr.is_codex_process(process.arguments.split()):
                        continue
                    self.codex_scan_incomplete = True
                    _problem(f"Codex process scan: pid {process.pid}", error)
                    continue
                if not herdr.is_codex_process(arguments) or codex_thread_host(arguments):
                    continue
                environment = herdr.process_environment(process.pid)
                if native := environment.get("CODEX_THREAD_ID", ""):
                    self.native_running[native] = {"codex": native}
                elif reason := codex_non_staff_reason(record, sources, environment):
                    _problem(f"Codex process {process.pid}", reason)
                else:
                    self.codex_scan_incomplete = True
                    _problem(f"Codex process {process.pid}", "native thread identity is unavailable")
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            self.codex_scan_incomplete = True
            _problem("Codex processes", error)
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
        raise ValueError(f"no staff parent for {selector}")

    def _owners(self) -> None:
        for slug, source in self.sources.productions.items():
            try:
                owner = self.owner(ShowrunnerMember(slug))
            except (ValueError, staff.StaffUnreadable) as error:
                _problem(source.timer.doc, error)
                continue
            session_id = (source.timer.target.removeprefix("session:")
                          if isinstance(self.sources, BoundMigrationSources) else owner.match.current_session)
            self.sessions[session_id] = owner
            if session_id in self.native_running:
                self.identities[owner.match.id] = self.native_running[session_id]
        for launch in sorted(self.sources.launches, key=lambda launch: launch["written_at"]):
            if launch.get("ended") or launch["session_id"] not in self.native_running:
                continue
            row = self.record.by_current_session(launch["session_id"])
            if row is not None:
                self.identities[row.id] = self.native_running[launch["session_id"]]
                if row.kind != "worker":
                    self.sessions[launch["session_id"]] = WorkerOwner(f"staff:{row.id}", row)

        for row in self.record.rows:
            if row.kind != "worker" and row.current_session in self.native_running:
                self.identities[row.id] = self.native_running[row.current_session]
                self.sessions[row.current_session] = WorkerOwner(f"staff:{row.id}", row)

    def recorded_owner(self, *, staff_id: str = "", production: str = "", unit: str = "") -> WorkerOwner:
        selected = worker_parent(self.record, self.sources, staff_id=staff_id,
                                 production=production, unit=unit)
        assert isinstance(selected, staff.StaffRow)
        return WorkerOwner(f"staff:{selected.id}", selected)

    def _record_run_owner(self, run: Path) -> WorkerOwner:
        director = unit_runs.director(run, dependencies=self.dependencies)
        if director in self.sessions:
            return self.sessions[director]
        row = self.record.by_member_reference(director)
        if row is not None and row.kind in ("unit", "showrunner"):
            return WorkerOwner(f"staff:{row.id}", row)
        raise ValueError(f"director {director!r} has no staff parent")

    def _run_owner(self, run: Path) -> WorkerOwner:
        owner = self._record_run_owner(run)
        director = self.directors.get(run)
        if isinstance(director, workers.LiveUnitDirector):
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

    def ledger_owner(self, worker: workers.UnitWorker, fields: Mapping[str, object],
                     owner: WorkerOwner | None) -> tuple[str, WorkerOwner]:
        current = self.record.by_member_reference(_text(fields.get("staff_id")) or worker.name)
        if current is None:
            backend = worker.backend
            session = backend.thread_id if isinstance(backend, workers.CodexThread) else backend.session_id
            current = self.record.by_current_session(session)
        if current is not None and current.kind == "worker" and current.parent is not None:
            row = self.record.by_id(current.parent)
            if row is not None:
                return current.name, WorkerOwner(f"staff:{row.id}", row)
        if owner is not None:
            return worker.name, owner
        if isinstance(worker.ownership, workers.HeldBy):
            return worker.name, self._run_owner(Path(worker.ownership.run))
        raise ValueError("worker ledger has no current staff parent")

    def paused_director_live(self, parent: WorkerOwner | None) -> bool:
        """Use the recorded native identity to check the director process."""
        if parent is not None and parent.match.current_session in self.native_running:
            return True
        if self.claude_registry_incomplete:
            raise ValueError("director registry is incomplete")
        if self.codex_scan_incomplete:
            raise ValueError("Codex director process scan is incomplete")
        return False

    def paused_current(self, worker: workers.UnitWorker, parent: WorkerOwner | None) -> bool:
        """Paused ownership has no run; its director or historical worker processes may still live."""
        failures: list[Exception] = []
        try:
            if self.paused_director_live(parent):
                return True
        except (OSError, ValueError, staff.StaffUnreadable) as error:
            failures.append(error)
        backend = worker.backend
        runs = {Path(event.run) for event in worker.events}
        if isinstance(backend, workers.ClaudeSession):
            if backend.session_id in self.running:
                self.live_worker_runs.update(runs)
                return True
            if self.claude_registry_incomplete:
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
                backend = worker.backend
                native = backend.thread_id if isinstance(backend, workers.CodexThread) else backend.session_id
                self.ledger_names.add((native, name))
                if not isinstance(worker.ownership, (workers.Retired, workers.Adopted)):
                    relevant += 1
                if isinstance(worker.ownership, workers.HeldBy):
                    run = Path(worker.ownership.run)
                    if self.skip_serverless_delegate(run):
                        continue
                    roster = self.checked(run / workers.ROSTER) if (run / workers.ROSTER).exists() else {}
                    entry = _object(roster.get(name, {}))
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
                        worker, {**fields, "staff_id": _text(fields.get("staff_id")) or key}, owner)
                except (ValueError, staff.StaffRefused, staff.StaffUnreadable, OSError):
                    if isinstance(worker.ownership, (workers.Retired, workers.Adopted)):
                        continue
                    if isinstance(worker.ownership, workers.Paused) and not self.paused_current(worker, None):
                        inactive += 1
                        continue
                    raise
                if isinstance(worker.ownership, workers.Paused) and not self.paused_current(worker, parent):
                    inactive += 1
                    continue
                if isinstance(worker.ownership, (workers.Retired, workers.Adopted)):
                    continue
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
        retained = unit_runs.director(run, dependencies=self.dependencies)
        row = self.record.by_id(staff.StaffId(retained)) or self.record.by_current_session(retained)
        if row is not None and row.kind == "unit" and row.current_session in self.native_running:
            return True
        if run not in self.directors:
            self.directors[run] = workers.resolve_live_unit_director(
                run, self.record, self.sources, tuple(self.running.values()), self.dependencies)
        live = isinstance(self.directors[run], workers.LiveUnitDirector)
        if not live and self.claude_registry_incomplete:
            raise ValueError(f"{run}: director registry is incomplete")
        if not live and self.codex_scan_incomplete:
            raise ValueError(f"{run}: Codex director process scan is incomplete")
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
            if self.claude_registry_incomplete:
                raise ValueError(f"{session}: Claude registry is incomplete")
            return False
        if _alive(fields.get("launcher_pid")):
            return True
        thread = _text(fields.get("thread_id"))
        if not thread:
            raise ValueError("Codex worker thread is unavailable")
        return thread in self.native_running or self.codex_thread_live(run, thread)

    def codex_thread_live(self, run: Path, thread: str) -> bool:
        # This process check can retain a stale worker while its server lives.
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

    def skip_serverless_delegate(self, run: Path) -> bool:
        """Exclude a serverless delegate before any held-run roster is read."""
        if run.parent.absolute() == unit_runs.FORMER_RUN_ROOT.absolute():
            server = run / "mesh_server.json"
            self.dependencies(server)
            try:
                _ = server.stat()
            except FileNotFoundError:
                self.run_activity[run] = False
                self.skipped_runs.add(run.absolute())
                return True
        return False

    def run_live(self, run: Path) -> bool:
        """Prove activity before ownership; uncertain evidence is never an inactive skip."""
        if run in self.run_activity:
            return self.run_activity[run]
        if self.skip_serverless_delegate(run):
            return False
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
                try:
                    entry = _object(value)
                except ValueError:
                    # The roster reader reports this entry and continues with its siblings.
                    continue
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
                if not matches and self.claude_registry_incomplete:
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
                native = (_text(entry.get("thread_id")) or _text(entry.get("worker_id"))
                          or _text(entry.get("session_id")))
                if (native, name) in self.ledger_names:
                    continue
                identity = self.backend(parent, name, entry)
                if isinstance(identity, BackendUnavailable):
                    _problem(f"{path} worker {name}", "worker backend is unavailable")
                role = _text(entry.get("role"))
                if not role and entry.get("role") in (None, "") and isinstance(identity, dict):
                    current = next((row for row in self.record.find(kind="worker", parent=parent.match.id)
                                    if row.name == name and len(identity) == 1
                                    and row.current_session in identity.values()), None)
                    if current is not None:
                        # Membership proves the worker kind, but not its opening role.
                        self.add(WorkerObservation(name, parent, "worker", identity))
                        continue
                self.add(WorkerObservation(name, parent, _role(role), identity))
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


@dataclass(frozen=True)
class WorkerEvidence:
    """Raw ownership observations, including workers not yet represented in staff."""

    workers: tuple[WorkerObservation, ...]
    identities: dict[staff.StaffId, dict[str, str]]
    diagnostics: tuple[str, ...]


class UnavailableWorkerPolicy(Enum):
    REFUSE = "refuse"
    LIST = "list"


def observe_workers(record: staff.StaffSnapshot, sources: _MemberSources, *,
                    unavailable: UnavailableWorkerPolicy = UnavailableWorkerPolicy.REFUSE) -> WorkerEvidence:
    """Expose the shared scanner without dropping missing rows or acquiring a lock."""
    errors = io.StringIO()
    with redirect_stderr(errors):
        scan = _WorkerSources(record, sources)
        observed = scan.collect()
    if scan.unreadable and unavailable is UnavailableWorkerPolicy.REFUSE:
        raise staff.StaffUnreadable("worker sources: " + ", ".join(map(str, sorted(scan.unreadable))))
    if scan.claude_registry_incomplete:
        if unavailable is UnavailableWorkerPolicy.REFUSE:
            raise staff.StaffUnreadable("Claude session registry is incomplete")
        _ = errors.write("Claude session registry is incomplete\n")
    if scan.codex_scan_incomplete and unavailable is UnavailableWorkerPolicy.REFUSE:
        raise staff.StaffUnreadable(errors.getvalue().strip())
    diagnostics = tuple(errors.getvalue().splitlines())
    return WorkerEvidence(observed, scan.identities, diagnostics)


def _live_member(row: staff.StaffRow, role: LiveRole, identity: dict[str, str]) -> dict[str, object]:
    """Keep the schema-3 member fields independent of stored location fields."""
    return {"id": row.id, "kind": row.kind, "name": row.name, "parent": row.parent,
            "current_session": row.current_session, "role": role, "identity": identity}


def live() -> dict[str, object]:
    """Read one owner snapshot and project identities without changing any source."""
    record = staff.snapshot()
    dependencies = staff.RecordInputs(set(record.inputs))
    sources = _member_sources(dependencies=dependencies)
    scan = _WorkerSources(record, sources, dependencies=dependencies)
    observed = scan.collect()
    members: dict[staff.StaffId, dict[str, object]] = {}
    unregistered = sorted(identity for identity in scan.native_running if record.by_current_session(identity) is None)
    for identity in unregistered:
        _problem(identity, "current native session has no staff row; identity publication is incomplete")
    incomplete = bool(unregistered) or scan.claude_registry_incomplete or scan.codex_scan_incomplete
    for run in sorted(scan.runs):
        try:
            publications = pending_publication_evidence(run)
            for diagnostic in publications.diagnostics:
                incomplete = True
                _problem(str(run), diagnostic)
            for pending in publications.workers:
                if record.by_current_session(pending.identity) is None:
                    unregistered.append(pending.identity)
                    incomplete = True
                    _problem(pending.identity, "backend publication is unfinished")
        except (OSError, ValueError, staff.StaffRefused) as error:
            incomplete = True
            _problem(str(run), error)
    active_ids = {row.id for kind in ("unit", "showrunner", "agent") for row in staff.active(record, kind)}
    resolver = session_staff.RowResolver(record, dependencies=dependencies)
    for row in record.rows:
        identity = scan.identities.get(row.id) if row.id in active_ids else None
        if row.kind != "worker" and identity is not None and row.current_session in identity.values():
            problems: list[str] = []
            document = resolver.production_doc(row)
            if row.kind != "agent" and not isinstance(document, session_staff.ProductionDoc):
                problems.append(document.reason)
            if row.kind == "unit":
                branch = resolver.unit_branch(row)
                if not isinstance(branch, session_staff.Branch):
                    problems.append(branch.reason)
                try:
                    config = resolver.unit_config(row)
                except unit_config.ConfigRefused as error:
                    problems.append(str(error))
                else:
                    if not isinstance(config, unit_config.UnitConfig):
                        problems.append(config.reason if not isinstance(config, session_staff.NoUnitConfig)
                                        else "no unit configuration")
            if problems:
                incomplete = True
                _problem(row.name, "; ".join(dict.fromkeys(problems)))
            if row.kind == "showrunner" and isinstance(document, session_staff.DocumentUnavailable):
                continue
            member = _live_member(row, row.kind, identity)
            parent = record.by_id(row.parent) if row.parent is not None else None
            if row.kind == "showrunner" and parent is not None and isinstance(parent.doc, staff.WrappedDoc):
                member["parent"] = None
            members[row.id] = member
    for entry in observed:
        owner = entry.owner.match
        if owner.id not in active_ids or isinstance(entry.identity, BackendUnavailable):
            continue
        rows = [row for row in record.find(kind="worker", parent=owner.id) if row.name == entry.name]
        if len(rows) != 1:
            incomplete = True
            _problem(entry.name, "no recorded worker membership; native identity publication is incomplete")
            continue
        row = rows[0]
        if row.current_session not in entry.identity.values():
            incomplete = True
            _problem(entry.name, "current runtime identity has no staff row; identity hook is incomplete")
            continue
        member = _live_member(row, entry.role, entry.identity)
        members[row.id] = member
    # Every emitted edge ends inside this document, even if source evidence is incomplete.
    while missing := [key for key, member in members.items()
                      if member["parent"] is not None and member["parent"] not in members]:
        for key in missing:
            incomplete = True
            _problem(key, "parent has no session identity; omitted from live staff")
            del members[key]
    return {"schema": 3, "inputs": sorted(scan.inputs), "members": [members[key] for key in sorted(members)],
            "complete": not incomplete, "unregistered": sorted(set(unregistered))}


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


def source_allowed(path: Path) -> bool:
    """Scratch checkouts never provide production ownership evidence."""
    return not path.expanduser().resolve().is_relative_to(showrunners.SCRATCH_ROOT.expanduser().resolve())


def _fill_machine() -> str:
    # Use the same machine names as shutdown records, without a second host rule.
    return remote.local_machine()


class BoundMigrationSources(_MemberSources):
    """Join legacy owners through their stored documents before their native ids exist."""

    @override
    def member(self, selector: MemberSelector, record: staff.StaffSnapshot) -> MemberFound | NoSuchMember:
        if isinstance(selector, UnitMember):
            parent = self.member(ShowrunnerMember(selector.slug), record)
            if isinstance(parent, NoSuchMember):
                return NoSuchMember(selector)
            matches = [row for row in record.find(kind="unit", parent=parent.row.id)
                       if row.name == unit_names.name(selector.unit)]
            if len(matches) > 1:
                raise staff.StaffUnreadable(f"{selector}: conflicting retained unit rows")
            return MemberFound(matches[0]) if matches else NoSuchMember(selector)
        if not isinstance(selector, ShowrunnerMember):
            return super().member(selector, record)
        source = self.productions.get(selector.slug)
        if source is None:
            return NoSuchMember(selector)
        matches = [row for row in record.find(kind="showrunner")
                   if isinstance(row.doc, (staff.LiveDoc, staff.WrappedDoc))
                   and row.doc.machine == _fill_machine()
                   and row.doc.path.resolve() == source.timer.doc.resolve()]
        if len(matches) > 1:
            raise staff.StaffUnreadable(f"{source.timer.doc}: no unique stored showrunner owner")
        return MemberFound(matches[0]) if matches else NoSuchMember(selector)


def migration_peer_identities(record: staff.StaffSnapshot) -> tuple[dict[staff.StaffId, set[str]], tuple[str, ...]]:
    """Read the peer roster once and join runtime evidence to stored member locations."""
    from scripts.message import roster

    local = remote.local_machine()
    remote_rows = [row for row in record.rows
                   if ((isinstance(row.doc, (staff.LiveDoc, staff.WrappedDoc)) and row.doc.machine != local)
                       or (isinstance(row.worktree, (staff.PresentWorktree, staff.RemovedWorktree))
                           and row.worktree.machine != local))]
    if not remote_rows:
        return {}, ()
    peer = roster.peer_snapshot(os.environ)
    if isinstance(peer, roster.PeerNotReached):
        return {}, (f"{peer.host}: native identity evidence uncertain: {peer.reason}",)
    problems = list(peer.problems)
    identities: dict[staff.StaffId, set[str]] = {}
    entries: dict[staff.StaffId, list[roster.RosterEntry]] = {}
    for row in remote_rows:
        candidates: list[roster.RosterEntry] = []
        if row.kind == "showrunner" and isinstance(row.doc, (staff.LiveDoc, staff.WrappedDoc)):
            location = row.doc
            if not source_allowed(location.path):
                problems.append(f"{row.id} {row.name}: scratch production source refused: {location.path}")
                continue
            try:
                result = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                                         location.machine, "cat -- " + shlex.quote(str(location.path))],
                                        capture_output=True, text=True, check=True, timeout=20,
                                        stdin=subprocess.DEVNULL)
                slug = FillDocument(location.path, result.stdout, f"{location.machine}:{location.path}").slug
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                problems.append(f"{row.id} {row.name}: stored peer document unavailable: {error}")
                continue
            candidates = [entry for entry in peer.entries
                          if entry.machine == location.machine and entry.role == "showrunner"
                          and isinstance(entry.showrunner, roster.RunsProduction)
                          and entry.showrunner.production == slug]
        elif row.kind == "unit" and isinstance(row.worktree, (staff.PresentWorktree, staff.RemovedWorktree)):
            location = row.worktree
            candidates = [entry for entry in peer.entries
                          if entry.machine == location.machine and entry.role == "unit director"
                          and ((isinstance(entry.session, roster.ClaudeSession)
                                and entry.session.session_id == row.current_session)
                               or (isinstance(entry.thread, roster.CodexThread)
                                   and entry.thread.thread_id == row.current_session))]
        if candidates:
            entries[row.id] = candidates
    # A worker is proved only by the address of its proved parent, never a global name.
    for row in record.find(kind="worker"):
        parents = entries.get(row.parent, []) if row.parent is not None else []
        addresses = {"session:" + parent.session.session_id for parent in parents
                     if isinstance(parent.session, roster.ClaudeSession)}
        addresses.update("codex:" + parent.thread.thread_id for parent in parents
                         if isinstance(parent.thread, roster.CodexThread))
        if addresses:
            entries[row.id] = [entry for entry in peer.entries if entry.name == row.name
                               and ((isinstance(entry.director, roster.UnderDirector)
                                     and entry.director.address in addresses)
                                    or (isinstance(entry.showrunner, roster.UnderShowrunner)
                                        and entry.showrunner.address in addresses))]
    for identity, candidates in entries.items():
        observed = set[str]()
        for entry in candidates:
            native = (entry.session.session_id if isinstance(entry.session, roster.ClaudeSession)
                      else entry.thread.thread_id if isinstance(entry.thread, roster.CodexThread) else "")
            try:
                observed.add(staff.native_id(native))
            except staff.StaffRefused as error:
                problems.append(f"{identity}: invalid peer native identity: {error}")
        if observed:
            identities[identity] = observed
    return identities, tuple(problems)


def migration_sources(record: staff.StaffSnapshot) -> tuple[_MemberSources, tuple[str, ...]]:
    """Read only stored production documents and non-scratch launch evidence."""
    problems: list[str] = []
    import herdr
    from scripts.shutdown import pane_record
    launches = pane_record.read_strict(herdr.shared_session())
    permitted: list[pane_record.PaneLaunch] = []
    for launch in launches.values():
        owner = record.by_member_reference(launch["session_id"])
        if owner is None:
            owner = record.by_id(staff.StaffId(launch["environment"].get("STAFF_ID", "")))
        if owner is not None and owner.kind == "worker":
            owner = record.by_id(owner.parent) if owner.parent else None
        if owner is not None:
            location = owner.worktree
            if not isinstance(location, staff.WorktreeUnfilled) and source_allowed(location.path):
                permitted.append(launch)
            elif isinstance(owner.doc, (staff.LiveDoc, staff.WrappedDoc)) and source_allowed(owner.doc.path):
                permitted.append(launch)
    productions: dict[str, showrunners.ShowrunnerSource] = {}
    for row in record.find(kind="showrunner"):
        location = row.doc
        if not isinstance(location, (staff.LiveDoc, staff.WrappedDoc)):
            problems.append(f"{row.id} {row.name}: missing stored production document")
            continue
        if location.machine != _fill_machine():
            problems.append(f"{row.id} {row.name}: remote production evidence requires peer read")
            continue
        if not source_allowed(location.path):
            problems.append(f"{row.id} {row.name}: scratch production source refused: {location.path}")
            continue
        try:
            document = location.path.read_text(encoding="utf-8")
            slug = FillDocument(location.path, document, str(location.path)).slug
            timer = showrunners.NOTIFIER_STATE_DIR / f"showrunner-{slug}"
            if timer.exists():
                source = showrunners.read_bound_source(timer, location.path)
            else:
                source = showrunners.ShowrunnerSource(showrunners.Timer(slug, "", location.path, ""), document)
            productions[slug] = source
        except (OSError, ValueError) as error:
            problems.append(f"{row.id} {row.name}: {error}")
    return BoundMigrationSources(productions, tuple(permitted)), tuple(problems)


def require_worker_stopped(record: staff.StaffSnapshot, identity: staff.StaffId) -> None:
    """Refuse removal while worker or peer evidence still names the native id."""
    evidence = observe_workers(record, session_staff.member_sources())
    member = record.by_id(identity)
    current_session = member.current_session if member is not None else identity
    if identity in evidence.identities or any(
        not isinstance(worker.identity, BackendUnavailable)
        and current_session in worker.identity.values() for worker in evidence.workers
    ):
        raise staff.StaffRefused(f"{identity}: native worker is current; stop it before removing its row")
    # Remote process evidence belongs to the roster's one peer reader.
    row = record.by_id(identity)
    while row is not None:
        location = row.worktree if isinstance(row.worktree, (staff.PresentWorktree, staff.RemovedWorktree)) else row.doc
        if isinstance(location, (staff.PresentWorktree, staff.RemovedWorktree, staff.LiveDoc, staff.WrappedDoc)):
            if location.machine != remote.local_machine():
                from scripts.message import roster
                peer = roster.peer_snapshot(os.environ)
                if isinstance(peer, roster.PeerNotReached):
                    raise staff.StaffRefused(f"{identity}: remote native session state is uncertain: {peer.reason}")
                if peer.problems:
                    raise staff.StaffRefused(f"{identity}: remote native session state is uncertain: {peer.problems[0]}")
                for entry in peer.entries:
                    native = (entry.session.session_id if isinstance(entry.session, roster.ClaudeSession)
                              else entry.thread.thread_id if isinstance(entry.thread, roster.CodexThread) else "")
                    if native == current_session:
                        raise staff.StaffRefused(f"{identity}: native session is live on {entry.machine}; stop it before removing its row")
            break
        row = record.by_id(row.parent) if row.parent else None
