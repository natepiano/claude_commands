"""Staff-aware file-only session labels shared by queue readers and the roster."""
from __future__ import annotations

import contextlib
import io
import json
import sys
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, cast

SCRIPTS = Path(__file__).resolve().parents[1]
for directory in (SCRIPTS / "message", SCRIPTS / "production", SCRIPTS / "herdr", SCRIPTS / "lib", SCRIPTS.parent):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))
from scripts.production import staff, showrunners
import production_units
import session_staff
import unit_runs
from sessions import SessionRecord, running_sessions
if TYPE_CHECKING:
    import herdr


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
        problem(problems, "showrunners", error)
        registered = []
    for line in errors.getvalue().splitlines():
        if line.strip():
            problem(problems, "showrunners", line.strip())
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


def production_rows(doc: Path, slug: str, record: staff.StaffSnapshot) -> list[dict[str, str]]:
    lines = doc.read_text(encoding="utf-8").splitlines()
    return production_units.live_unit_table(lines, slug, record=record)


def every_marked_unit(record: staff.StaffSnapshot | None = None) -> session_staff.MarkedProductions | OSError:
    """Every production's observed units, joined to one staff snapshot."""
    import unit_lookup as process_units

    try:
        return process_units.every_marked_unit(record=record)
    except OSError as error:
        return error


@dataclass(frozen=True)
class DiscoverProductionMarks:
    """The full roster discovers marks across all running panes."""


DISCOVER_PRODUCTION_MARKS = DiscoverProductionMarks()


def production_places(
    records: list[SessionRecord], details: Mapping[str, ClaudeDetails],
    registered: list[showrunners.Showrunner],
    runner_ids: Mapping[str, showrunners.Showrunner], ledger_workers: set[str],
    problems: list[str],
    *, record: staff.StaffSnapshot,
    marks: session_staff.MarkedProductions | DiscoverProductionMarks = DISCOVER_PRODUCTION_MARKS,
) -> dict[str, ProductionPlace]:
    addresses = {runner["slug"]: f"session:{session_id}" for session_id, runner in runner_ids.items()}
    places: dict[str, ProductionPlace] = {}
    # One read of every production's marks serves every showrunner below.
    every = (marks if isinstance(marks, session_staff.MarkedProductions)
             else every_marked_unit(record) if registered else session_staff.MarkedProductions({}, {}))
    # One process scan places every record; a scan per record took most of the roster's time.
    record_places: dict[int, herdr.HerdrPlace] | None = None
    for runner in registered:
        doc = Path(runner["doc"])
        runner_address = addresses.get(runner["slug"], "")
        showrunner: UnderShowrunner | ProductionWithoutRunningShowrunner = (
            UnderShowrunner(runner["slug"], runner["session"], runner_address)
            if runner["session"] and runner_address
            else ProductionWithoutRunningShowrunner(runner["slug"])
        )
        try:
            if isinstance(every, OSError):
                raise every
            if fault := every.production_fault(runner["slug"]):
                problem(problems, f"unit hosts left out under {runner['slug']}", OSError(fault))
            units = every.units.get(runner["slug"], {})
        except (OSError, UnicodeError, ValueError, production_units.Refusal) as error:
            problem(problems, f"unit hosts for {runner['slug']} were not read", error)
        else:
            for unit_name, marked in units.items():
                place = ProductionPlace(showrunner, DirectsUnit(unit_name))
                if isinstance(marked.claude, session_staff.LiveClaude):
                    _ = places.setdefault(marked.claude.session_id, place)
                    continue
                # A socketless director is still identified by its marked pane.
                if record_places is None:
                    import herdr

                    record_places = herdr.herdr_places()
                for session_record in records:
                    same_place = record_places.get(session_record["pid"]) == marked.place
                    if session_record["sessionId"] not in ledger_workers and same_place:
                        _ = places.setdefault(session_record["sessionId"], place)
                        break
            continue
        # Worktree ownership is a fallback only when neither host could be read.
        try:
            rows = production_rows(doc, runner["slug"], record)
        except (OSError, UnicodeError, ValueError, production_units.Refusal) as fallback_error:
            problem(problems, f"units in {doc}", fallback_error)
            continue
        for row in rows:
            unit_name = production_units.cell_value(row.get("Unit", ""))
            worktree = production_units.cell_value(row.get("Worktree", ""))
            for session_record in records:
                if (session_record["sessionId"] not in ledger_workers and worktree
                        and details[session_record["sessionId"]].cwd == worktree):
                    _ = places.setdefault(session_record["sessionId"], ProductionPlace(showrunner, DirectsUnit(unit_name)))
    return places


def unit_labels_for(session_ids: Iterable[str], environment: Mapping[str, str]) -> dict[str, UnitLabels]:
    """Resolve Claude and Codex holders from files, without process scans, mesh calls or account reads."""
    wanted = set(session_ids)
    if not wanted:
        return {}
    try:
        record = staff.snapshot()
        records = newest_sessions()
        problems: list[str] = []
        registered = registered_showrunners(environment, problems,
                                            known_sessions={record["sessionId"]: record for record in records})
        runners = showrunner_ids(records, registered)
        marks, directors = session_staff.session_staff_and_directors(records, environment, record=record)
        places = production_places(records, {}, registered, runners, set(directors), problems, record=record, marks=marks)
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
