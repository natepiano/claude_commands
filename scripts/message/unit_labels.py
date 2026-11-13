"""Staff-aware file-only session labels shared by queue readers and the roster."""
from __future__ import annotations

import json
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import cast

from scripts.production import staff, showrunners
from scripts.production import session_staff
from scripts.lib import unit_runs
from scripts.message.sessions import SessionRecord, running_sessions
from scripts.lib.user_zone import user_zone_name


OWNER_NOT_RECORDED = "owner not recorded"


@dataclass(frozen=True)
class UnderShowrunner:
    production: str
    name: str
    address: str


@dataclass(frozen=True)
class RunsProduction:
    production: str


@dataclass(frozen=True)
class ProductionWithoutRunningShowrunner:
    production: str


@dataclass(frozen=True)
class NoShowrunner:
    """This session has no showrunner link."""


ShowrunnerRelationship = (
    RunsProduction
    | UnderShowrunner
    | ProductionWithoutRunningShowrunner
    | NoShowrunner
)


@dataclass(frozen=True)
class UnderDirector:
    unit: str
    name: str
    address: str


@dataclass(frozen=True)
class DirectsUnit:
    unit: str


@dataclass(frozen=True)
class NoDirector:
    """This session has no unit-director link."""


@dataclass(frozen=True)
class UnitWithoutRunningDirector:
    unit: str


DirectorRelationship = DirectsUnit | UnderDirector | UnitWithoutRunningDirector | NoDirector


class OwnerRole(StrEnum):
    """The role of the owner for whom a session runs a step."""

    UNKNOWN = "unknown"
    SHOWRUNNER = "showrunner"
    UNIT = "unit"


class Runner(StrEnum):
    """Who ran the step: the showrunner, the unit director, or a worker."""

    UNKNOWN = "unknown"
    SHOWRUNNER = "showrunner"
    UNIT_DIRECTOR = "unit director"
    WORKER = "worker"


@dataclass(frozen=True)
class UnitLabels:
    session: str
    showrunner: str = "—"
    unit: str = "—"
    role: OwnerRole = OwnerRole.UNKNOWN
    runner: Runner = Runner.UNKNOWN

    @property
    def owner_text(self) -> str:
        """Show the session name when the owner has no production or unit."""
        return self.session if self.showrunner == "—" and self.unit == "—" else self.showrunner

    def folder_name(self) -> str:
        """Name a copies folder from the owning director's labels.

        Look up labels with unit_labels_for([<session id from
        build_hold.owning_session()>], environment), so a Codex worker
        resolves to its unit director.
        """
        if self.showrunner == "—":
            return self.session if self.unit == "—" else self.unit
        if self.unit == "—":
            return self.showrunner
        if self.unit == self.showrunner or self.unit.startswith(self.showrunner + "-"):
            return self.unit
        return f"{self.showrunner}-{self.unit}"


def make_labels(name: str, showrunner: ShowrunnerRelationship,
                 director: DirectorRelationship) -> UnitLabels:
    production = " ".join(production_of(showrunner).split())
    unit = " ".join(unit_of(director).split())
    session = " ".join(name.split())
    if isinstance(showrunner, RunsProduction):
        role = OwnerRole.SHOWRUNNER
    elif isinstance(director, (DirectsUnit, UnderDirector, UnitWithoutRunningDirector)):
        role = OwnerRole.UNIT
    elif isinstance(showrunner, (UnderShowrunner, ProductionWithoutRunningShowrunner)):
        role = OwnerRole.SHOWRUNNER
    else:
        role = OwnerRole.UNKNOWN
    return UnitLabels(session, production or "—", unit or "—", role, runner_of(showrunner, director))


def runner_of(showrunner: ShowrunnerRelationship, director: DirectorRelationship) -> Runner:
    if isinstance(showrunner, RunsProduction):
        return Runner.SHOWRUNNER
    if isinstance(director, DirectsUnit):
        return Runner.UNIT_DIRECTOR
    if isinstance(director, (UnderDirector, UnitWithoutRunningDirector)) or isinstance(showrunner, UnderShowrunner):
        return Runner.WORKER
    return Runner.UNKNOWN


def production_of(showrunner: ShowrunnerRelationship) -> str:
    if isinstance(showrunner, (RunsProduction, UnderShowrunner, ProductionWithoutRunningShowrunner)):
        return showrunner.production
    return ""


def unit_of(director: DirectorRelationship) -> str:
    return director.unit if isinstance(director, (DirectsUnit, UnderDirector, UnitWithoutRunningDirector)) else ""


@dataclass(frozen=True)
class ProductionPlace:
    showrunner: UnderShowrunner | ProductionWithoutRunningShowrunner
    director: DirectsUnit


@dataclass(frozen=True)
class DirectorSession:
    session_id: str


@dataclass(frozen=True)
class NoRunningDirector:
    """No live Claude session owns this run."""


RunDirector = DirectorSession | NoRunningDirector


@dataclass(frozen=True)
class LiveRun:
    path: Path
    director: RunDirector


@dataclass(frozen=True)
class ClaudeDetails:
    cwd: str
    status: str


@dataclass(frozen=True)
class RunPlace:
    director: DirectorRelationship
    showrunner: ShowrunnerRelationship
    cwd: str




def as_string(value: object) -> str:
    return value if isinstance(value, str) else ""


def read_object(path: Path) -> dict[str, object]:
    parsed = cast("object", json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(parsed, dict):
        raise ValueError("expected a JSON object")
    return cast("dict[str, object]", parsed)


def problem(problems: list[str], source: str, error: BaseException | str) -> None:
    detail = str(error).splitlines()[0] if str(error).splitlines() else type(error).__name__
    message = f"{source}: {detail}"
    if message not in problems:
        problems.append(message)


@dataclass(frozen=True)
class StaffRowAnomaly:
    """Recorded facts are incomplete for this staff row."""

    row: staff.StaffRow
    reason: str

    @property
    def diagnostic(self) -> str:
        return f"{self.row.name}: {self.reason}"


@dataclass
class StaffDiagnostics:
    """Keep incomplete rows separate from reads that can hide sessions."""

    problems: list[str] = field(default_factory=list)
    anomalies: dict[staff.StaffId, StaffRowAnomaly] = field(default_factory=dict)

    def anomaly(self, row: staff.StaffRow, reason: str) -> None:
        _ = self.anomalies.setdefault(row.id, StaffRowAnomaly(row, reason))

    def document(self, row: staff.StaffRow,
                 result: session_staff.DocumentUnavailable | session_staff.LocationUnfilled) -> None:
        if isinstance(result, session_staff.LocationUnfilled):
            self.anomaly(row, result.reason)
        else:
            problem(self.problems, row.name, result.reason)

    @property
    def warnings(self) -> list[str]:
        return [anomaly.diagnostic for anomaly in self.anomalies.values()]

    @property
    def excluded_sessions(self) -> set[str]:
        return {anomaly.row.current_session for anomaly in self.anomalies.values()}


def newest_sessions() -> list[SessionRecord]:
    newest: dict[str, SessionRecord] = {}
    for record in running_sessions():
        _ = newest.setdefault(record["sessionId"], record)
    return list(newest.values())


def registered_showrunners(
    diagnostics: StaffDiagnostics,
    *, known_sessions: Mapping[str, SessionRecord] | showrunners.DiscoverSessions = showrunners.DISCOVER_SESSIONS,
    membership: staff.StaffSnapshot | showrunners.DiscoverMembership = showrunners.DISCOVER_MEMBERSHIP,
    resolver: session_staff.RowResolver | None = None,
) -> list[showrunners.Showrunner]:
    record = (resolver.record if resolver is not None else
              staff.snapshot() if isinstance(membership, showrunners.DiscoverMembership) else membership)
    resolver = resolver if resolver is not None else session_staff.RowResolver(record)
    records = (newest_sessions() if isinstance(known_sessions, showrunners.DiscoverSessions)
               else list(known_sessions.values()))
    sessions_by_id = {item["sessionId"]: item for item in records}
    registered: list[showrunners.Showrunner] = []
    for row in staff.active(record, "showrunner"):
        doc = resolver.production_doc(row)
        if not isinstance(doc, session_staff.ProductionDoc):
            diagnostics.document(row, doc)
            continue
        try:
            timer = showrunners.timer_for(row, record)
        except (OSError, ValueError) as error:
            problem(diagnostics.problems, row.name, error)
            continue
        live = sessions_by_id.get(row.current_session)
        registered.append(showrunners.Showrunner(
            name=row.name,
            session=row.name if live is not None else "",
            socket=live["messagingSocketPath"] if live is not None else "",
            doc=str(doc.path),
            zone=timer.zone if isinstance(timer, showrunners.TimerRegistration) else user_zone_name()))
    return registered


def showrunner_ids(
    records: list[SessionRecord], registered: Iterable[showrunners.Showrunner],
    *, membership: staff.StaffSnapshot | None = None,
) -> dict[str, showrunners.Showrunner]:
    membership = membership if membership is not None else staff.snapshot()
    by_id = {item["sessionId"]: item for item in records}
    by_doc = {runner["doc"]: runner for runner in registered}
    found: dict[str, showrunners.Showrunner] = {}
    for row in staff.active(membership, "showrunner"):
        if not isinstance(row.doc, staff.LiveDoc):
            continue
        runner = by_doc.get(str(row.doc.path))
        if row.current_session in by_id and runner is not None:
            found[row.current_session] = runner
    return found


def every_labeled_session(record: staff.StaffSnapshot | None = None, *,
                      running: list[SessionRecord] | None = None) -> session_staff.LabeledProductions | OSError:
    """Every production's observed units, joined to one staff snapshot."""
    from scripts.production import unit_lookup as process_units

    try:
        return process_units.every_labeled_session(record=record, running=running)
    except OSError as error:
        return error


def production_places(
    records: list[SessionRecord],
    ledger_workers: set[str],
    diagnostics: StaffDiagnostics,
    *, record: staff.StaffSnapshot, resolver: session_staff.RowResolver | None = None,
) -> dict[str, ProductionPlace]:
    """Join native session identities to their current staff parents."""
    places: dict[str, ProductionPlace] = {}
    resolver = resolver if resolver is not None else session_staff.RowResolver(record)
    by_id = {item["sessionId"]: item for item in records}
    readable_parents: set[staff.StaffId] = set()
    unreadable_parents: set[staff.StaffId] = set()
    for row in staff.active(record, "unit"):
        identity = row.current_session
        if identity in ledger_workers or not isinstance(row.parent, staff.ReportsTo):
            continue
        parent = record.by_id(row.parent.id)
        if parent is None or parent.kind != "showrunner":
            problem(diagnostics.problems, row.name, "the staff parent is unavailable")
            continue
        if isinstance(parent.doc, staff.WrappedDoc):
            continue
        doc = resolver.production_doc(row)
        if not isinstance(doc, session_staff.ProductionDoc):
            diagnostics.document(row, doc)
            continue
        if parent.id in unreadable_parents:
            continue
        if parent.id not in readable_parents:
            try:
                _ = showrunners.timer_for(parent, record)
            except (OSError, ValueError) as error:
                problem(diagnostics.problems, parent.name, error)
                unreadable_parents.add(parent.id)
                continue
            readable_parents.add(parent.id)
        name = parent.name
        owner = by_id.get(parent.current_session)
        relationship: UnderShowrunner | ProductionWithoutRunningShowrunner = (
            UnderShowrunner(name, parent.name, f"session:{parent.current_session}")
            if owner is not None else ProductionWithoutRunningShowrunner(name))
        places[identity] = ProductionPlace(relationship, DirectsUnit(row.name))
    return places


def unit_labels_for(session_ids: Iterable[str], environment: Mapping[str, str]) -> dict[str, UnitLabels]:
    """Resolve Claude and Codex holders from files, without process scans or account reads."""
    del environment
    wanted = set(session_ids)
    if not wanted:
        return {}
    try:
        record = staff.snapshot()
        resolver = session_staff.RowResolver(record)
        records = newest_sessions()
        problems: list[str] = []
        diagnostics = StaffDiagnostics(problems)
        registered = registered_showrunners(diagnostics,
                                            known_sessions={record["sessionId"]: record for record in records},
                                            membership=record, resolver=resolver)
        runners = showrunner_ids(records, registered, membership=record)
        workers: dict[str, staff.StaffRow] = {
            row.current_session: row for row in record.find(kind="worker")
            if record.parent_of(row) is not None}
        places = production_places(records, set(workers), diagnostics,
                                   record=record, resolver=resolver)
        # Read names from staff for the native sessions in this registry.
        names: dict[str, str] = {
            item["sessionId"]: member.name if (member := record.by_current_session(item["sessionId"])) is not None else ""
            for item in records}
        labels: dict[str, UnitLabels] = {}
        for session_id in wanted & names.keys():
            showrunner: ShowrunnerRelationship = NoShowrunner()
            director: DirectorRelationship = NoDirector()
            if session_id in runners:
                showrunner = RunsProduction(runners[session_id]["name"])
            elif session_id in places:
                showrunner = places[session_id].showrunner
                director = places[session_id].director
            elif session_id in workers:
                director, showrunner = staff_relationships(
                    record, workers[session_id], names,
                    {key: place.showrunner for key, place in places.items()})
            labels[session_id] = make_labels(names[session_id], showrunner, director)
        labels.update(codex_unit_labels(wanted - labels.keys(), records, places, membership=record))
        labels.update(recorded_owner_labels(wanted - labels.keys(), record))
        return labels
    except Exception:
        return {}


def recorded_owner_labels(wanted: set[str], record: staff.StaffSnapshot) -> dict[str, UnitLabels]:
    """Keep recorded ownership when the session registry has no live entry."""
    labels: dict[str, UnitLabels] = {}
    for session_id in wanted:
        member = record.by_current_session(session_id) if session_id else None
        if member is None:
            continue
        showrunner: ShowrunnerRelationship = NoShowrunner()
        director: DirectorRelationship = NoDirector()
        if member.kind == "showrunner":
            showrunner = RunsProduction(member.name)
        elif member.kind == "unit":
            director = DirectsUnit(member.name)
            parent = record.parent_of(member)
            if parent is not None and parent.kind == "showrunner":
                showrunner = ProductionWithoutRunningShowrunner(parent.name)
        elif member.kind == "worker":
            director, showrunner = staff_relationships(record, member, {}, {})
        labels[session_id] = make_labels(member.name, showrunner, director)
    return labels


def live_runs(
    roots: tuple[Path, ...], live_session_ids: set[str], problems: list[str], *,
    record: staff.StaffSnapshot | unit_runs.RunStaffAtUse = unit_runs.RunStaffAtUse.LOCAL,
) -> list[LiveRun]:
    record = staff.local_snapshot() if isinstance(record, unit_runs.RunStaffAtUse) else record
    by_path: dict[Path, RunDirector] = {
        row.run.path.resolve(): DirectorSession(row.current_session)
        for row in record.rows if row.current_session in live_session_ids and isinstance(row.run, staff.OpenRun)
    }
    heartbeats: list[Path] = []
    for root in roots:
        try:
            heartbeats.extend(root.glob(f"*/{unit_runs.HEARTBEAT}"))
        except OSError as error:
            problem(problems, f"unit runs in {root}", error)
    now = time.time()
    for heartbeat in heartbeats:
        try:
            if now - heartbeat.stat().st_mtime < unit_runs.LIVE_HEARTBEAT_SECS:
                _ = by_path.setdefault(heartbeat.parent.resolve(), NoRunningDirector())
        except OSError as error:
            problem(problems, f"worker heartbeat {heartbeat}", error)
    return [
        LiveRun(path, director)
        for path, director in sorted(by_path.items(), key=lambda item: item[0])
    ]


def stopped_run_place(run: Path, record: staff.StaffSnapshot) -> RunPlace:
    """Keep a worker's recorded unit without claiming that its director is live."""
    try:
        state = read_object(run / unit_runs.STATE_FILE)
    except (OSError, UnicodeError, ValueError):
        return RunPlace(NoDirector(), NoShowrunner(), "")
    owner = unit_runs.run_owner(state, record)
    if isinstance(owner, unit_runs.RunOwnerUnrecorded):
        return RunPlace(NoDirector(), NoShowrunner(), "")
    runner = record.parent_of(owner)
    showrunner: ShowrunnerRelationship = (
        ProductionWithoutRunningShowrunner(runner.name) if runner is not None else NoShowrunner())
    cwd = str(owner.worktree.path) if isinstance(owner.worktree, staff.PresentWorktree) else ""
    return RunPlace(UnitWithoutRunningDirector(owner.name), showrunner, cwd)






def staff_relationships(membership: staff.StaffSnapshot, member: staff.StaffRow,
                        names: Mapping[str, str], showrunners: Mapping[str, ShowrunnerRelationship]
                        ) -> tuple[DirectorRelationship, ShowrunnerRelationship]:
    parent = membership.parent_of(member)
    if parent is None:
        return NoDirector(), NoShowrunner()
    if parent.kind == "showrunner":
        return NoDirector(), UnderShowrunner(parent.name, names.get(parent.current_session, parent.name),
                                             f"session:{parent.current_session}")
    director: DirectorRelationship = (UnderDirector(parent.name, names[parent.current_session],
                                                    f"session:{parent.current_session}")
                                      if parent.current_session in names else UnitWithoutRunningDirector(parent.name))
    showrunner = showrunners.get(parent.current_session, NoShowrunner())
    if isinstance(showrunner, NoShowrunner) and (owner := membership.parent_of(parent)) is not None:
        showrunner = UnderShowrunner(owner.name, names.get(owner.current_session, owner.name),
                                    f"session:{owner.current_session}")
    return director, showrunner


def codex_unit_labels(
    wanted: set[str], records: list[SessionRecord],
    places: Mapping[str, ProductionPlace],
    *, membership: staff.StaffSnapshot,
) -> dict[str, UnitLabels]:
    names = {item["sessionId"]: item["name"] for item in records}
    labels: dict[str, UnitLabels] = {}
    for member in membership.find(kind="worker"):
        evidence = session_staff.worker_backend(membership, member.id)
        if (member.current_session not in wanted or not isinstance(evidence, session_staff.RecordedWorker)
                or not isinstance(evidence.backend, staff.CodexBackend)):
            continue
        director, showrunner = staff_relationships(membership, member, names,
                                                   {key: place.showrunner for key, place in places.items()})
        labels[member.current_session] = make_labels(member.name, showrunner, director)
    return labels
