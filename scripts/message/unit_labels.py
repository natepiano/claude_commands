"""Staff-aware file-only session labels shared by queue readers and the roster."""
from __future__ import annotations

import json
import sys
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict, cast

SCRIPTS = Path(__file__).resolve().parents[1]
for directory in (SCRIPTS / "message", SCRIPTS / "production", SCRIPTS / "herdr", SCRIPTS / "lib", SCRIPTS.parent):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))
from scripts.production import staff, showrunners
from scripts.production import session_staff
import unit_runs
from sessions import SessionRecord, running_sessions
from user_zone import user_zone_name


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


@dataclass(frozen=True)
class UnitLabels:
    session: str
    showrunner: str = "—"
    unit: str = "—"

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
    unit = short_unit(" ".join(unit_of(director).split()))
    return UnitLabels(" ".join(name.split()), production or "—", unit or "—")


def production_of(showrunner: ShowrunnerRelationship) -> str:
    if isinstance(showrunner, (RunsProduction, UnderShowrunner, ProductionWithoutRunningShowrunner)):
        return showrunner.production
    return ""


def unit_of(director: DirectorRelationship) -> str:
    return director.unit if isinstance(director, (DirectsUnit, UnderDirector, UnitWithoutRunningDirector)) else ""


def short_unit(unit: str) -> str:
    """The shown unit name: the unit id without its "-unit" suffix."""
    return unit.removesuffix("-unit")


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


class MeshWorker(TypedDict, total=False):
    port: int
    thread_id: str
    status: str
    launcher_pid: int
    role: str
    lens: str
    cwd: str
    held_design_id: str
    transferring_role: dict[str, object]
    role_redirect: dict[str, object]


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


def newest_sessions() -> list[SessionRecord]:
    newest: dict[str, SessionRecord] = {}
    for record in running_sessions():
        _ = newest.setdefault(record["sessionId"], record)
    return list(newest.values())


def registered_showrunners(
    problems: list[str],
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
            problem(problems, row.name, doc.reason)
            continue
        try:
            timer = showrunners.timer_for(row, record)
        except (OSError, ValueError) as error:
            problem(problems, row.name, error)
            continue
        live = sessions_by_id.get(row.current_session)
        registered.append(showrunners.Showrunner(
            slug=timer.slug if isinstance(timer, showrunners.Timer) else row.name,
            session=live["name"] if live is not None else "",
            socket=live["messagingSocketPath"] if live is not None else "",
            doc=str(doc.path),
            zone=timer.zone if isinstance(timer, showrunners.Timer) else user_zone_name()))
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


def every_marked_unit(record: staff.StaffSnapshot | None = None, *,
                      running: list[SessionRecord] | None = None) -> session_staff.MarkedProductions | OSError:
    """Every production's observed units, joined to one staff snapshot."""
    import unit_lookup as process_units

    try:
        return process_units.every_marked_unit(record=record, running=running)
    except OSError as error:
        return error


def production_places(
    records: list[SessionRecord],
    ledger_workers: set[str],
    problems: list[str],
    *, record: staff.StaffSnapshot, resolver: session_staff.RowResolver | None = None,
) -> dict[str, ProductionPlace]:
    """Join native session identities to their current staff parents."""
    places: dict[str, ProductionPlace] = {}
    resolver = resolver if resolver is not None else session_staff.RowResolver(record)
    by_id = {item["sessionId"]: item for item in records}
    for row in staff.active(record, "unit"):
        identity = row.current_session
        if identity in ledger_workers or row.parent is None:
            continue
        parent = record.by_id(row.parent)
        if parent is None or parent.kind != "showrunner":
            problem(problems, row.name, "the staff parent is unavailable")
            continue
        if isinstance(parent.doc, staff.WrappedDoc):
            continue
        doc = resolver.production_doc(row)
        if not isinstance(doc, session_staff.ProductionDoc):
            problem(problems, row.name, doc.reason)
        try:
            timer = showrunners.timer_for(parent, record)
        except (OSError, ValueError) as error:
            problem(problems, parent.name, error)
            continue
        slug = timer.slug if isinstance(timer, showrunners.Timer) else parent.name
        owner = by_id.get(parent.current_session)
        relationship: UnderShowrunner | ProductionWithoutRunningShowrunner = (
            UnderShowrunner(slug, owner["name"], f"session:{parent.current_session}")
            if owner is not None else ProductionWithoutRunningShowrunner(slug))
        places[identity] = ProductionPlace(relationship, DirectsUnit(row.name))
    return places


def unit_labels_for(session_ids: Iterable[str], environment: Mapping[str, str]) -> dict[str, UnitLabels]:
    """Resolve Claude and Codex holders from files, without process scans, mesh calls or account reads."""
    wanted = set(session_ids)
    if not wanted:
        return {}
    try:
        record = staff.snapshot()
        resolver = session_staff.RowResolver(record)
        records = newest_sessions()
        problems: list[str] = []
        registered = registered_showrunners(problems,
                                            known_sessions={record["sessionId"]: record for record in records},
                                            membership=record, resolver=resolver)
        runners = showrunner_ids(records, registered, membership=record)
        directors: dict[str, str] = {
            row.current_session: parent.current_session for row in record.find(kind="worker")
            if row.parent is not None and (parent := record.by_id(row.parent)) is not None}
        places = production_places(records, set(directors), problems,
                                   record=record, resolver=resolver)
        names: dict[str, str] = {item["sessionId"]: item["name"] for item in records}
        labels: dict[str, UnitLabels] = {}
        for session_id in wanted & names.keys():
            showrunner: ShowrunnerRelationship = NoShowrunner()
            director: DirectorRelationship = NoDirector()
            if session_id in runners:
                showrunner = RunsProduction(runners[session_id]["slug"])
            elif session_id in places:
                showrunner = places[session_id].showrunner
                director = places[session_id].director
            elif session_id in directors:
                owner = directors[session_id]
                place = places.get(owner)
                unit = ""
                if place is not None:
                    showrunner = place.showrunner
                    unit = place.director.unit
                director = (UnderDirector(unit, names[owner], f"session:{owner}")
                            if owner in names else UnitWithoutRunningDirector(unit))
            labels[session_id] = make_labels(names[session_id], showrunner, director)
        labels.update(codex_unit_labels(wanted - labels.keys(), environment, records, places, problems))
        return labels
    except Exception:
        return {}


def read_run_markers(
    active: Path, live_session_ids: set[str], by_path: dict[Path, RunDirector], problems: list[str]
) -> None:
    """Add the run each live director's marker names."""
    if not active.is_dir():
        return
    try:
        markers = list(active.iterdir())
    except OSError as error:
        problem(problems, f"unit run markers in {active}", error)
        return
    for marker in markers:
        if marker.name not in live_session_ids:
            continue
        try:
            lines = marker.read_text(encoding="utf-8").splitlines()
            if lines and lines[0].strip():
                by_path[Path(lines[0].strip()).resolve()] = DirectorSession(marker.name)
        except (OSError, UnicodeError) as error:
            problem(problems, f"unit run marker {marker}", error)


def live_runs(
    roots: tuple[Path, ...], live_session_ids: set[str], problems: list[str]
) -> list[LiveRun]:
    by_path: dict[Path, RunDirector] = {}
    heartbeats: list[Path] = []
    for root in roots:
        read_run_markers(root / "active", live_session_ids, by_path, problems)
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


def stopped_run_place(run: Path) -> RunPlace:
    """Keep a worker's recorded unit without claiming that its director is live."""
    try:
        state = read_object(run / unit_runs.STATE_FILE)
    except (OSError, UnicodeError, ValueError):
        return RunPlace(NoDirector(), NoShowrunner(), "")
    cwd = as_string(state.get("working_dir"))
    production, unit = as_string(state.get("production")), as_string(state.get("unit"))
    if production and unit:
        return RunPlace(UnitWithoutRunningDirector(unit), ProductionWithoutRunningShowrunner(production), cwd)
    # Older runs may keep their identity only in the plan. Import the history
    # reader for that fallback, not for every lookup of recorded labels.
    from scripts.direct.progress_history import ProductionUnitIdentity, resolve_production_unit

    identity = resolve_production_unit(state)
    if isinstance(identity, ProductionUnitIdentity):
        return RunPlace(UnitWithoutRunningDirector(identity.unit),
                         ProductionWithoutRunningShowrunner(identity.production), cwd)
    return RunPlace(NoDirector(), NoShowrunner(), cwd)


def mesh_records(run: LiveRun, problems: list[str]) -> dict[str, MeshWorker]:
    path = run.path / "mesh_roster.json"
    if not path.exists():
        return {}
    try:
        stored = read_object(path)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
        problem(problems, f"Codex roster {path}", error)
        return {}
    records: dict[str, MeshWorker] = {}
    for name, value in stored.items():
        if isinstance(value, dict):
            records[name] = cast("MeshWorker", cast("object", value))
    return records


def listed_mesh_worker(record: MeshWorker) -> bool:
    return not (record.get("status") == "ended" or record.get("transferring_role")
                or record.get("role_redirect") or record.get("held_design_id"))


def codex_unit_labels(
    wanted: set[str], environment: Mapping[str, str], records: list[SessionRecord],
    places: Mapping[str, ProductionPlace], problems: list[str],
) -> dict[str, UnitLabels]:
    if not wanted:
        return {}
    roots = unit_runs.run_roots(environment.get("ROSTER_RUN_ROOT"))
    matches: dict[Path, dict[str, str]] = {}
    # Check requested thread IDs before discovering live runs. Claude-only
    # lookups and unknown IDs need no live-run discovery or account data.
    for root in roots:
        try:
            paths = sorted(root.glob("*/mesh_roster.json"))
        except OSError as error:
            problem(problems, f"Codex rosters in {root}", error)
            continue
        for path in paths:
            run = LiveRun(path.parent, NoRunningDirector())
            workers = {
                thread: name for name, record in sorted(mesh_records(run, problems).items())
                if (thread := as_string(record.get("thread_id"))) and thread in wanted and listed_mesh_worker(record)
            }
            if workers:
                matches[path.parent.resolve()] = workers
    if not matches:
        return {}
    names: dict[str, str] = {item["sessionId"]: item["name"] for item in records}
    labels: dict[str, UnitLabels] = {}
    runs = {run.path: run for run in live_runs(roots, set(names), problems)}
    for path in matches:
        owner = unit_runs.director(path)
        if owner in names:
            _ = runs.setdefault(path, LiveRun(path, DirectorSession(owner)))
    for run in runs.values():
        if run.path not in matches:
            continue
        owner = unit_runs.director(run.path)
        director_id = owner if owner in names else (run.director.session_id
            if isinstance(run.director, DirectorSession) else "")
        if director_id in names:
            place = places.get(director_id)
            showrunner: ShowrunnerRelationship = place.showrunner if place is not None else NoShowrunner()
            director: DirectorRelationship = place.director if place is not None else NoDirector()
        else:
            stopped = stopped_run_place(run.path)
            showrunner, director = stopped.showrunner, stopped.director
        for thread, name in matches[run.path].items():
            labels[thread] = make_labels(name, showrunner, director)
    return labels
