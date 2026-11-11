#!/usr/bin/env python3
"""Resolve staff membership and observe native worker identities."""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

from contextlib import redirect_stderr
import io
import json
import os
import shlex
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
import sys
from pathlib import Path
from typing import cast

from scripts.production import showrunners  # noqa: E402
from scripts.herdr import herdr_types  # noqa: E402
from scripts.production.production_doc import ProductionDocument  # noqa: E402
from scripts.production import staff, unit_config, session_staff  # noqa: E402
from scripts.herdr import pane_launch  # noqa: E402
from scripts.lib import unit_runs  # noqa: E402
from scripts.message import sessions  # noqa: E402
from scripts.agents import remote  # noqa: E402


# Matchers live in unit_lookup, so lower readers use the same join without an import cycle.
from scripts.production.unit_lookup import (  # noqa: E402
    ShowrunnerMember as ShowrunnerMember, UnitMember as UnitMember, ObservedUnit as ObservedUnit,
    MemberSelector as MemberSelector, MemberFound as MemberFound, NoSuchMember as NoSuchMember,
    UnitProduction as UnitProduction, MemberSources as _MemberSources,
    member_sources as _member_sources,
)
from scripts.production import unit_lookup  # noqa: E402


def member(selector: MemberSelector, record: staff.StaffSnapshot) -> MemberFound | NoSuchMember:
    """Resolve pane identity or current production membership through the shared join."""
    return _MemberSources(()).member(selector, record)


def ensure_staff_showrunner(child: ProductionDocument, caller: str = "", *, runtime_id: str = "") -> staff.StaffRow:
    record = staff.initial_snapshot()
    found = member(ShowrunnerMember(child.name), record)
    location = staff.LiveDoc(remote.local_machine(), child.doc.resolve())
    if isinstance(found, MemberFound):
        if runtime_id and found.row.current_session != staff.native_id(runtime_id):
            raise staff.StaffRefused(f"{child.name}: native session conflicts with retained staff identity")
        if found.row.doc == location:
            return pane_launch.staff_admitted(found.row)
        return pane_launch.staff_admitted(staff.ensure("showrunner", name=found.row.name, parent=found.row.parent, doc=location))
    if caller:
        _ = showrunners.owner_for(caller, record)
    # Admission records the document. Parent attachment records the edge and notices later.
    return pane_launch.staff_admitted(staff.ensure("showrunner", name=child.name, doc=location, runtime_id=runtime_id))


def showrunner_members(record: staff.StaffSnapshot) -> dict[str, staff.StaffRow]:
    """Return showrunner rows from the supplied staff snapshot."""
    return {row.name: row for row in record.find(kind="showrunner")}


def unit_parent(unit: str, session_id: str = "") -> UnitProduction:
    """Resolve the current staff edge through the stored member reference."""
    reference = session_staff.StoredReference(session_id) if session_id else session_staff.BY_NAME
    return unit_lookup.unit_parent(unit, reference, record=staff.snapshot())


LiveRole = str


@dataclass(frozen=True)
class WorkerOwner:
    key: str
    match: staff.StaffRow


BackendUnavailable = staff.BackendUnavailable


@dataclass(frozen=True)
class ClaudeSession:
    session_id: str


@dataclass(frozen=True)
class CodexThread:
    thread_id: str


@dataclass(frozen=True)
class WorkerObservation:
    row: staff.StaffRow
    owner: WorkerOwner
    identity: ClaudeSession | CodexThread | BackendUnavailable

    @property
    def name(self) -> str:
        return self.row.name





def _observation_session(value: ClaudeSession | CodexThread) -> str:
    return value.session_id if isinstance(value, ClaudeSession) else value.thread_id


def _problem(path: Path | str, error: object, *, pid: int = 0) -> None:
    message = f"staff sources: {path}: {error}".replace("\n", " ").replace("\r", " ")
    if not session_staff.collect_anomaly(f"pid {pid} (codex): {message}" if pid else message, pid=pid):
        print(message, file=sys.stderr)


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
    """Join a pane's retained launch to the supplied staff snapshot."""
    for launch in sources.launches:
        if launch.get("pane_id") != pane or launch.get("ended"):
            continue
        if record.by_member_reference(launch["session_id"]) is not None:
            return True
    return False


class CodexProcessThreads:
    """Read a thread from its process or terminal metadata."""

    def __init__(self) -> None:
        self.panes: dict[tuple[str, str], dict[str, object]] = {}
        self.foreground_pids: dict[tuple[str, str], int] = {}
        self.threads: dict[Path, dict[str, str]] = {}

    def thread(self, pid: int, environment: Mapping[str, str]) -> str:
        from scripts.herdr import herdr
        from scripts.message import codex_daemon

        try:
            try:
                return herdr.codex_resume_thread(pid)
            except (OSError, ValueError):
                pass
            place = herdr.herdr_place_of(pid)
            if isinstance(place, herdr.NotInHerdr):
                return environment.get("CODEX_THREAD_ID", "")
            key = (place.session, place.pane)
            client = herdr.Herdr(place.session)
            if key not in self.foreground_pids:
                self.foreground_pids[key] = client.pane_codex_pid(place.pane)
            if self.foreground_pids[key] != pid:
                return environment.get("CODEX_THREAD_ID", "")
            if key not in self.panes:
                self.panes[key] = herdr.json_object(client.result("pane", "get", place.pane).get("pane"),
                                                  "Codex pane")
            pane = self.panes[key]
            if identity := herdr.pane_session(pane):
                return identity
            if identity := environment.get("CODEX_THREAD_ID", ""):
                return identity
            title = pane.get("terminal_title_stripped")
            if not isinstance(title, str):
                return ""
            name = title.partition(" | ")[0]
            home = Path(environment.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()
            if home not in self.threads:
                observed = codex_daemon.loaded_sessions(home / "app-server-control/app-server-control.sock")
                self.threads[home] = {}
                if not isinstance(observed, codex_daemon.DaemonUnreadable):
                    for entry in observed:
                        # A repeated name cannot identify one terminal's thread.
                        if entry.name in self.threads[home]:
                            self.threads[home][entry.name] = ""
                        else:
                            self.threads[home][entry.name] = entry.thread_id
            return self.threads[home].get(name, "")
        except (OSError, ValueError, herdr_types.HerdrFailure):
            return environment.get("CODEX_THREAD_ID", "")


    def check(self, pid: int, identity: str) -> session_staff.ReportProcessIdentity:
        from scripts.herdr import herdr

        try:
            native = identity or (self.thread(pid, herdr.process_environment(pid)) if pid else "")
        except (OSError, ValueError, herdr_types.HerdrFailure):
            native = identity
        return session_staff.ReportProcessIdentity(native, session_staff.morty_thread(native))


@dataclass(frozen=True)
class UnpublishedWorker:
    """A backend publication still requires a staff row."""

    inbox: Path
    identity: staff.StaffId
    parent: staff.StaffId


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
    """Read worker membership from staff and session activity from live processes."""

    def __init__(self, record: staff.StaffSnapshot, sources: _MemberSources,
                 *,
                 dependencies: staff.RecordInputs | None = None) -> None:
        self.record: staff.StaffSnapshot = record
        self.dependencies: staff.RecordInputs = dependencies if dependencies is not None else staff.RecordInputs(set(record.inputs))
        self.inputs: set[str] = self.dependencies.paths
        self.identities: dict[staff.StaffId, dict[str, str]] = {}
        registry = sessions.read_registry(dependencies=self.dependencies)
        self.claude_registry_incomplete: bool = bool(registry.unreadable)
        self.codex_scan_incomplete: bool = False
        for path in registry.unreadable:
            if path.exists():
                _problem(path, "session registry is unreadable")
        self.running: dict[str, sessions.SessionRecord] = {
            item.record["sessionId"]: item.record for item in registry.entries if sessions.running_session(item.record)
        }
        self.native_running: dict[str, dict[str, str]] = {
            identity: {"claude": identity} for identity in self.running}
        from scripts.message import process_table
        from scripts.herdr import herdr
        try:
            processes = process_table.snapshot()
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            self.codex_scan_incomplete = True
            _problem("Codex processes", error)
            processes = {}
        threads = CodexProcessThreads()
        for process in processes.values():
            try:
                try:
                    arguments = shlex.split(process.arguments)
                except ValueError as error:
                    if not herdr.is_codex_process(process.arguments.split()):
                        continue
                    self.codex_scan_incomplete = True
                    _problem(f"Codex process scan: pid {process.pid}", error, pid=process.pid)
                    continue
                if not herdr.is_codex_process(arguments) or codex_thread_host(arguments):
                    continue
                try:
                    environment = herdr.process_environment(process.pid)
                except (OSError, herdr_types.HerdrFailure):
                    if process.pid in process_table.snapshot():
                        raise
                    continue
                native = threads.thread(process.pid, environment)
                if session_staff.report_process(process.pid, native).is_morty:
                    continue
                if native:
                    if record.by_current_session(native) is not None:
                        self.native_running[native] = {"codex": native}
                elif (pane := environment.get("HERDR_PANE_ID", "")) and pane_holds_staff_member(record, sources, pane):
                    self.codex_scan_incomplete = True
                    _problem(f"Codex process {process.pid} in pane {pane}",
                             "cannot read its thread; staff status may be incomplete", pid=process.pid)
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                self.codex_scan_incomplete = True
                _problem(f"Codex process {process.pid}", error, pid=process.pid)
        for row in record.rows:
            identity = self.native_running.get(row.current_session)
            if identity is not None:
                self.identities[row.id] = identity

    def collect(self, *, current_only: bool = False) -> tuple[WorkerObservation, ...]:
        observations: list[WorkerObservation] = []
        for row in self.record.find(kind="worker"):
            parent = self.record.parent_of(row)
            if parent is None:
                _problem(row.id, "worker parent is unavailable")
                continue
            if current_only and row.current_session not in self.native_running:
                continue
            backend = self.record.backends.get(row.id)
            identity = (ClaudeSession(row.current_session) if isinstance(backend, staff.ClaudeBackend) else
                        CodexThread(row.current_session) if isinstance(backend, staff.CodexBackend) else BackendUnavailable())
            observations.append(WorkerObservation(row, WorkerOwner(f"staff:{parent.id}", parent), identity))
        return tuple(observations)


@dataclass(frozen=True)
class WorkerEvidence:
    """Raw ownership observations, including workers not yet represented in staff."""

    workers: tuple[WorkerObservation, ...]
    identities: dict[staff.StaffId, dict[str, str]]
    diagnostics: tuple[str, ...]


def observe_workers(record: staff.StaffSnapshot, sources: _MemberSources, *,
                    current_only: bool = False) -> WorkerEvidence:
    """Expose the shared scanner without dropping missing rows or acquiring a lock."""
    errors = io.StringIO()
    with redirect_stderr(errors):
        scan = _WorkerSources(record, sources)
        observed = scan.collect(current_only=current_only)
    if scan.claude_registry_incomplete:
        _ = errors.write("Claude session registry is incomplete\n")
    diagnostics = tuple(errors.getvalue().splitlines())
    return WorkerEvidence(observed, scan.identities, diagnostics)


def production_units(slug: str) -> tuple[staff.StaffRow, ...]:
    """Read unit membership for the staff pause and resume commands."""
    record = staff.snapshot()
    return session_staff.production_members(record).get(slug, ())


def _live_member(row: staff.StaffRow, role: LiveRole, identity: Mapping[str, str]) -> dict[str, object]:
    """Keep the schema-3 member fields independent of stored location fields."""
    return {"id": row.id, "kind": row.kind, "name": row.name, "parent": row.parent.id if isinstance(row.parent, staff.ReportsTo) else None,
            "current_session": row.current_session, "role": role, "identity": dict(identity)}


def live() -> dict[str, object]:
    """Read one owner snapshot and project identities without changing any source."""
    try:
        record = staff.snapshot()
        dependencies = staff.RecordInputs(set(record.inputs))
        sources = _member_sources(dependencies=dependencies)
    except staff.StaffUnreadable as error:
        _problem("staff membership", error)
        return {"schema": 3, "complete": False, "problems": [str(error)]}
    pane_workers: dict[staff.StaffId, dict[str, object]] = {}
    pane_problems: list[str] = []
    try:
        parents = session_staff.pane_parents(record)
        if parents:
            from scripts.herdr import worker_panes

            for states in worker_panes.observe_parents(parents, record).values():
                for state in states:
                    row = state.row
                    backend = record.backends.get(row.id)
                    identity = ({"claude": row.current_session} if isinstance(backend, staff.ClaudeBackend) else
                                {"codex": row.current_session} if isinstance(backend, staff.CodexBackend) else {})
                    value = _live_member(row, row.name, identity)
                    value["pane_state"] = ("open" if isinstance(state, worker_panes.OpenWorker) else
                                           "stopped" if isinstance(state, worker_panes.StoppedWorker) else
                                           "closed" if isinstance(state, worker_panes.ClosedWorker) else
                                           "unknown" if isinstance(state, worker_panes.ProcessUnknownWorker) else "incomplete")
                    if isinstance(state, (worker_panes.ProcessUnknownWorker, worker_panes.UncertainWorker)):
                        reason = f"{row.name}: {state.reason}"
                        pane_problems.append(reason)
                        _problem(row.name, state.reason)
                    pane_workers[row.id] = value
    except (OSError, ValueError) as error:
        pane_problems.append(str(error))
        _problem("pane workers", error)
    scan = _WorkerSources(record, sources, dependencies=dependencies)
    observed = scan.collect()
    members: dict[staff.StaffId, dict[str, object]] = {}
    unregistered = sorted(identity for identity in scan.native_running if record.by_current_session(identity) is None)
    for identity in unregistered:
        _problem(identity, "current native session has no staff row; identity publication is incomplete")
    incomplete = bool(unregistered or pane_problems) or scan.claude_registry_incomplete or scan.codex_scan_incomplete
    for root in unit_runs.run_roots(os.environ.get("ROSTER_RUN_ROOT")):
        for run in sorted({path.parent for path in root.glob("*/*.pending.json")}):
            publications = pending_publication_evidence(run)
            for diagnostic in publications.diagnostics:
                incomplete = True
                _problem(run, diagnostic)
            for pending in publications.workers:
                if record.by_current_session(pending.identity) is None:
                    unregistered.append(pending.identity)
                    incomplete = True
                    _problem(pending.identity, "backend publication is unfinished")
    active_ids = {row.id for kind in ("unit", "showrunner", "producer") for row in staff.active(record, kind)}
    resolver = session_staff.RowResolver(record, dependencies=dependencies)
    for row in record.rows:
        identity = scan.identities.get(row.id) if row.id in active_ids else None
        if row.kind != "worker" and identity is not None and row.current_session in identity.values():
            problems: list[str] = []
            document = resolver.production_doc(row)
            if row.kind != "producer" and not isinstance(document, session_staff.ProductionDoc):
                problems.append(f"production doc could not be read: {document.reason}")
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
            parent = record.by_id(row.parent.id) if isinstance(row.parent, staff.ReportsTo) else None
            if row.kind == "showrunner" and parent is not None and isinstance(parent.doc, staff.WrappedDoc):
                member["parent"] = None
            members[row.id] = member
    for entry in observed:
        row = entry.row
        if entry.owner.match.id not in active_ids:
            continue
        if row.id in pane_workers:
            members[row.id] = pane_workers[row.id]
            continue
        worker_identity = ({"claude": entry.identity.session_id} if isinstance(entry.identity, ClaudeSession)
                           else {"codex": entry.identity.thread_id} if isinstance(entry.identity, CodexThread) else {})
        if isinstance(entry.identity, BackendUnavailable):
            incomplete = True
            _problem(row.name, "worker backend is unavailable")
        member = _live_member(row, row.name, worker_identity)
        members[row.id] = member
    # Every emitted edge ends inside this document, even if source evidence is incomplete.
    while missing := [key for key, member in members.items()
                      if member["parent"] is not None and member["parent"] not in members]:
        for key in missing:
            incomplete = True
            member = members[key]
            parent_id = cast(staff.StaffId, member["parent"])
            parent = record.by_id(parent_id)
            if parent is None:
                _problem(key, "parent staff row is unavailable")
                del members[key]
                continue
            _problem(parent.id, "parent has no current session evidence")
            members[parent.id] = _live_member(parent, "unit" if parent.kind == "unit" else "showrunner", {})
    return {"schema": 3, "inputs": sorted(scan.inputs), "members": [members[key] for key in sorted(members)],
            "complete": not incomplete, "unregistered": sorted(set(unregistered)),
            **({"problems": pane_problems} if pane_problems else {})}


def cli_live() -> int:
    """Keep source failures on the same staff CLI exception boundary when run as a script."""
    def run() -> int:
        print(json.dumps(live(), sort_keys=True))
        return 0

    return staff.cli_result(run)



def require_worker_stopped(record: staff.StaffSnapshot, identity: staff.StaffId) -> None:
    """Refuse removal while worker or peer evidence still names the native id."""
    evidence = observe_workers(record, session_staff.member_sources(), current_only=True)
    for diagnostic in evidence.diagnostics:
        print(f"anomaly: {diagnostic}", file=sys.stderr)
    member = record.by_id(identity)
    current_session = member.current_session if member is not None else identity
    if identity in evidence.identities or any(
        not isinstance(worker.identity, BackendUnavailable)
        and current_session == _observation_session(worker.identity) for worker in evidence.workers
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
                for diagnostic in peer.problems:
                    print(f"anomaly: {diagnostic}", file=sys.stderr)
                for entry in peer.entries:
                    native = (entry.session.session_id if isinstance(entry.session, roster.ClaudeSession)
                              else entry.thread.thread_id if isinstance(entry.thread, roster.CodexThread) else "")
                    if native == current_session:
                        raise staff.StaffRefused(f"{identity}: native session is live on {entry.machine}; stop it before removing its row")
            break
        row = record.by_id(row.parent.id) if isinstance(row.parent, staff.ReportsTo) else None


