"""File-only production, unit and session labels for queue readers."""
from __future__ import annotations

import contextlib
import io
import json
import sys
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict, cast, override

# Match the existing messaging modules' identities and registry configuration.
SCRIPTS = Path(__file__).resolve().parents[1]
for directory in (SCRIPTS / "message", SCRIPTS / "production", SCRIPTS / "lib"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

import unit_runs
import showrunners
from sessions import SessionRecord, running_sessions
import unit_marks


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
class MarkedDirectorDiscovery:
    ownership: ProductionPlace
    mark: unit_marks.MarkedUnit


@dataclass(frozen=True)
class WorktreeDirectorDiscovery:
    ownership: ProductionPlace
    worktree: str


@dataclass(frozen=True)
class ProductionPlacements:
    sessions: dict[str, ProductionPlace]
    discovery: list[MarkedDirectorDiscovery | WorktreeDirectorDiscovery]


@dataclass(frozen=True)
class DirectorSession:
    session_id: str


@dataclass(frozen=True)
class NoRunningDirector:
    """No live Claude session owns this run."""


RunDirector = DirectorSession | NoRunningDirector


@dataclass(frozen=True)
class LiveUnitRun:
    path: Path
    director: RunDirector


@dataclass(frozen=True)
class RunOwnership:
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


def string_value(value: object) -> str:
    return value if isinstance(value, str) else ""


def read_object(path: Path) -> dict[str, object]:
    parsed = cast("object", json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(parsed, dict):
        raise ValueError("expected a JSON object")
    return cast("dict[str, object]", parsed)


def record_problem(problems: list[str], source: str, error: BaseException | str) -> None:
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
    environment: Mapping[str, str], problems: list[str],
    *, known_sessions: Mapping[str, SessionRecord] | showrunners.DiscoverSessions = showrunners.DISCOVER_SESSIONS,
) -> list[showrunners.Showrunner]:
    state = Path(
        environment.get(
            "NOTIFIER_STATE_DIR", str(Path.home() / ".local/state/notifier")
        )
    )
    sessions_directory = Path(
        environment.get("NOTIFIER_SESSIONS_DIR", str(Path.home() / ".claude/sessions"))
    )
    showrunners.SESSIONS_DIR = sessions_directory
    errors = io.StringIO()
    try:
        with contextlib.redirect_stderr(errors):
            registered = showrunners.registered_showrunners(state, known_sessions=known_sessions)
    except (OSError, ValueError) as error:
        record_problem(problems, "showrunners", error)
        registered = []
    for line in errors.getvalue().splitlines():
        if line.strip():
            record_problem(problems, "showrunners", line.strip())
    return registered


def showrunner_ids(
    records: list[SessionRecord], registered: Iterable[showrunners.Showrunner]
) -> dict[str, showrunners.Showrunner]:
    found: dict[str, showrunners.Showrunner] = {}
    for runner in registered:
        if runner["socket"]:
            match = next((record for record in records
                          if record["messagingSocketPath"] == runner["socket"]), None)
        else:
            match = next((record for record in records
                          if runner["session"] and record["name"] == runner["session"]), None)
        if match is not None:
            found[match["sessionId"]] = runner
    return found


def unit_labels_for(session_ids: Iterable[str], environment: Mapping[str, str]) -> dict[str, UnitLabels]:
    """Resolve Claude and Codex holders from files, without process scans, mesh calls or account reads."""
    wanted = set(session_ids)
    if not wanted:
        return {}
    try:
        records = newest_sessions()
        problems: list[str] = []
        registered = registered_showrunners(environment, problems,
                                            known_sessions={record["sessionId"]: record for record in records})
        runners = showrunner_ids(records, registered)
        marks, directors = unit_marks.session_unit_marks(records, environment)
        places = production_places_from_marks(registered, runners, marks, problems).sessions
        names = {record["sessionId"]: record["name"] for record in records}
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
                director = UnderDirector(unit, names[owner], f"session:{owner}")
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
        record_problem(problems, f"unit run markers in {active}", error)
        return
    for marker in markers:
        if marker.name not in live_session_ids:
            continue
        try:
            lines = marker.read_text(encoding="utf-8").splitlines()
            if lines and lines[0].strip():
                by_path[Path(lines[0].strip()).resolve()] = DirectorSession(marker.name)
        except (OSError, UnicodeError) as error:
            record_problem(problems, f"unit run marker {marker}", error)


def live_runs(
    roots: tuple[Path, ...], live_session_ids: set[str], problems: list[str]
) -> list[LiveUnitRun]:
    by_path: dict[Path, RunDirector] = {}
    heartbeats: list[Path] = []
    for root in roots:
        read_run_markers(root / "active", live_session_ids, by_path, problems)
        try:
            heartbeats.extend(root.glob(f"*/{unit_runs.HEARTBEAT}"))
        except OSError as error:
            record_problem(problems, f"unit runs in {root}", error)
    now = time.time()
    for heartbeat in heartbeats:
        try:
            if now - heartbeat.stat().st_mtime < unit_runs.LIVE_HEARTBEAT_SECS:
                _ = by_path.setdefault(heartbeat.parent.resolve(), NoRunningDirector())
        except OSError as error:
            record_problem(problems, f"worker heartbeat {heartbeat}", error)
    return [
        LiveUnitRun(path, director)
        for path, director in sorted(by_path.items(), key=lambda item: item[0])
    ]


def stopped_run_place(run: Path) -> RunOwnership:
    """Keep a worker's recorded unit without claiming that its director is live."""
    try:
        state = read_object(run / unit_runs.STATE_FILE)
    except (OSError, UnicodeError, ValueError):
        return RunOwnership(NoDirector(), NoShowrunner(), "")
    cwd = string_value(state.get("working_dir"))
    production, unit = string_value(state.get("production")), string_value(state.get("unit"))
    if production and unit:
        return RunOwnership(UnitWithoutRunningDirector(unit), ProductionWithoutRunningShowrunner(production), cwd)
    # Older runs may keep their identity only in the plan. Import the history
    # reader for that fallback, not for every lookup of recorded labels.
    from scripts.direct.progress_history import ProductionUnitIdentity, resolve_production_unit

    identity = resolve_production_unit(state)
    if isinstance(identity, ProductionUnitIdentity):
        return RunOwnership(UnitWithoutRunningDirector(identity.unit),
                         ProductionWithoutRunningShowrunner(identity.production), cwd)
    return RunOwnership(NoDirector(), NoShowrunner(), cwd)


def mesh_records(run: LiveUnitRun, problems: list[str]) -> dict[str, MeshWorker]:
    path = run.path / "mesh_roster.json"
    if not path.exists():
        return {}
    try:
        stored = read_object(path)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
        record_problem(problems, f"Codex roster {path}", error)
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
            record_problem(problems, f"Codex rosters in {root}", error)
            continue
        for path in paths:
            run = LiveUnitRun(path.parent, NoRunningDirector())
            workers = {
                thread: name for name, record in sorted(mesh_records(run, problems).items())
                if (thread := string_value(record.get("thread_id"))) and thread in wanted and listed_mesh_worker(record)
            }
            if workers:
                matches[path.parent.resolve()] = workers
    if not matches:
        return {}
    names = {record["sessionId"]: record["name"] for record in records}
    labels: dict[str, UnitLabels] = {}
    for run in live_runs(roots, set(names), problems):
        if run.path not in matches:
            continue
        if isinstance(run.director, DirectorSession) and run.director.session_id in names:
            place = places.get(run.director.session_id)
            showrunner: ShowrunnerRelationship = place.showrunner if place is not None else NoShowrunner()
            director: DirectorRelationship = place.director if place is not None else NoDirector()
        else:
            stopped = stopped_run_place(run.path)
            showrunner, director = stopped.showrunner, stopped.director
        for thread, name in matches[run.path].items():
            labels[thread] = make_labels(name, showrunner, director)
    return labels


def production_rows(
    doc: Path, slug: str, every: unit_marks.MarkedProductions | OSError,
) -> list[dict[str, str]]:
    import production_units

    class KnownUnitMarks(production_units.SessionMarks):
        """Use the roster's known marks, retaining rows when those marks are unreadable."""

        def __init__(self, slug: str, every: unit_marks.MarkedProductions | OSError) -> None:
            super().__init__(slug)
            self._known: set[str] | OSError = (
                every if isinstance(every, OSError)
                else set(every.units.get(slug, {})) | set(every.faults.get(slug, {}))
            )

        @override
        def has(self, unit: str) -> bool:
            return isinstance(self._known, OSError) or unit in self._known

    lines = doc.read_text(encoding="utf-8").splitlines()
    marks = KnownUnitMarks(slug, every)
    return [row for row in production_units.unit_table(lines) if not production_units.unit_is_retired(row, marks)]


def production_owner(runner: showrunners.Showrunner, address: str) -> UnderShowrunner | ProductionWithoutRunningShowrunner:
    return (UnderShowrunner(runner["slug"], runner["session"], address)
            if runner["session"] and address else ProductionWithoutRunningShowrunner(runner["slug"]))


def production_places_from_marks(
    registered: list[showrunners.Showrunner],
    runners: Mapping[str, showrunners.Showrunner],
    marks: unit_marks.MarkedProductions | OSError,
    problems: list[str],
) -> ProductionPlacements:
    """Resolve recorded owners once; leave process and worktree discovery to the full roster."""
    import production_units

    addresses = {runner["slug"]: f"session:{sid}" for sid, runner in runners.items()}
    places: dict[str, ProductionPlace] = {}
    discovery: list[MarkedDirectorDiscovery | WorktreeDirectorDiscovery] = []
    for runner in registered:
        address = addresses.get(runner["slug"], "")
        owner = production_owner(runner, address)
        if isinstance(marks, OSError):
            record_problem(problems, f"unit hosts for {runner['slug']} were not read", marks)
        elif fault := marks.production_fault(runner["slug"]):
            record_problem(problems, f"unit hosts left out under {runner['slug']}", OSError(fault))
        try:
            rows = production_rows(Path(runner["doc"]), runner["slug"], marks)
        except (OSError, UnicodeError, ValueError, production_units.Refusal) as error:
            record_problem(problems, f"unit hosts for {runner['slug']} were not read", error)
            record_problem(problems, f"units in {runner['doc']}", error)
            continue
        for row in rows:
            unit = production_units.cell_value(row.get("Unit", ""))
            ownership = ProductionPlace(owner, DirectsUnit(unit))
            if isinstance(marks, OSError):
                discovery.append(WorktreeDirectorDiscovery(ownership, production_units.cell_value(row.get("Worktree", ""))))
                continue
            marked = marks.units.get(runner["slug"], {}).get(unit)
            if marked is None:
                continue
            if isinstance(marked.claude, unit_marks.LiveClaude):
                _ = places.setdefault(marked.claude.session_id, ownership)
            else:
                discovery.append(MarkedDirectorDiscovery(ownership, marked))
    return ProductionPlacements(places, discovery)
