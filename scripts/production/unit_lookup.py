#!/usr/bin/env python3
"""Find recorded production units and join their herdr panes by staff identity.

Current names, places and messaging sockets come from herdr and live session
records. Run state comes from /unit:direct records.
"""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import json
import os
import subprocess
import sys
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, cast

from scripts.message import sessions  # noqa: E402
from scripts.herdr import herdr_types  # noqa: E402
from scripts.herdr.place import HerdrPlace  # noqa: E402
from scripts.message.sessions import SessionRecord, live_session, sessions_dir as sessions_dir  # noqa: E402
from scripts.production import staff, session_staff, unit_config  # noqa: E402
from scripts.production.session_labels import StaffLabels
from scripts.agents import remote
from scripts.lib import unit_runs  # noqa: E402

if TYPE_CHECKING:
    from scripts.herdr import herdr

USAGE = "usage: unit_lookup.py list <production name> | pane <production name> <unit>"


from scripts.production.session_staff import (
    ShowrunnerMember as ShowrunnerMember,
    UnitMember as UnitMember,
    ObservedUnit as ObservedUnit,
    MemberSelector as MemberSelector,
    MemberFound as MemberFound,
    NoSuchMember as NoSuchMember,
    unit_match as _unit_match,
    MemberSources as MemberSources,
    member_sources as member_sources,
    LiveClaude as LiveClaude,
    ClaudeNotRunning as ClaudeNotRunning,
    ClaudeUnknown as ClaudeUnknown,
    Claude as Claude,
    Place as Place,
    LabeledSession as LabeledSession,
    ObservedProcess as ObservedProcess,
    ProcessUnobserved as ProcessUnobserved,
    LiveSession as LiveSession,
    NoSession as NoSession,
    place_text as place_text,
    holder_text as holder_text,
    LabeledProductions as LabeledProductions,
    production_members as production_members,
    group_labeled_sessions as group_labeled_sessions,
    retained_session_labels,
    session_staff_and_directors as session_staff_and_directors,
)


def doc_owner(doc: Path, record: staff.StaffSnapshot) -> staff.StaffRow:
    """The showrunner row whose staff `doc` is this local production document."""
    owner = record.by_doc(remote.local_machine(), doc.resolve())
    if owner is None or owner.kind != "showrunner":
        raise staff.StaffUnreadable(f"{doc}: no showrunner staff row")
    return owner


def doc_units(doc: Path, record: staff.StaffSnapshot) -> tuple[staff.StaffRow, ...]:
    """The production's unit rows in staff.json row order; a retired row has a RemovedWorktree."""
    return tuple(record.find(kind="unit", parent=doc_owner(doc, record).id))


def local_worktree(selected: MemberFound | NoSuchMember) -> staff.PresentWorktree | staff.RemovedWorktree:
    """Require a recorded local location without changing its lifecycle state."""
    if isinstance(selected, NoSuchMember):
        raise staff.StaffUnreadable(f"{selected.selector}: no matching staff member")
    location = selected.row.worktree
    if isinstance(location, staff.WorktreeUnfilled):
        raise staff.StaffUnreadable(f"{selected.row.name}: staff worktree is unfilled")
    if location.machine != remote.local_machine():
        raise staff.StaffUnreadable(f"{selected.row.name}: staff worktree is on {location.machine}: {location.path}")
    return location


@dataclass(frozen=True)
class UnitProblem:
    unit_id: staff.StaffId
    name: str
    reason: str


@dataclass(frozen=True)
class UnitMetadata:
    cells: dict[staff.StaffId, dict[str, str]]
    problems: tuple[UnitProblem, ...]


def unit_metadata(record: staff.StaffSnapshot, *,
                  name: str | None = None) -> UnitMetadata:
    """Return unit facts and problems without writing diagnostics."""
    productions = production_members(record)
    rows = (tuple(row for children in productions.values() for row in children) if name is None
            else productions.get(name, ()))
    resolver = session_staff.RowResolver(record, rows=rows)
    metadata: dict[staff.StaffId, dict[str, str]] = {}
    issues: list[UnitProblem] = []
    for row in rows:
        cells = {"Unit": row.name}
        problems: list[str] = []
        tree = resolver.unit_worktree(row)
        if isinstance(tree, staff.WorktreeUnfilled):
            cells["Worktree"] = "unavailable: the worktree is not recorded"
            problems.append("the worktree is not recorded")
        else:
            cells["Worktree"] = f"`{tree.path}`"
        branch = resolver.unit_branch(row)
        cells["Branch"] = branch.name if isinstance(branch, session_staff.Branch) else "unavailable: " + branch.reason
        if not isinstance(branch, session_staff.Branch):
            problems.append(branch.reason)
        try:
            config = resolver.unit_config(row)
        except unit_config.ConfigRefused as error:
            cells["Plan"] = "unavailable: " + str(error)
            problems.append(str(error))
        else:
            if isinstance(config, unit_config.UnitConfig):
                if isinstance(config.plan, unit_config.PlanFile):
                    cells["Plan"] = ("unavailable: the worktree is not recorded"
                                     if isinstance(tree, staff.WorktreeUnfilled)
                                     else "active" if config.plan.path == Path("active")
                                     else str(tree.path / config.plan.path))
                else:
                    cells["Plan"] = "standby" if isinstance(config.plan, unit_config.StandbyPlan) else "permanent"
                if isinstance(config.port, unit_config.AppPort):
                    cells["Port"] = str(config.port.number)
            else:
                reason = config.reason if not isinstance(config, session_staff.NoUnitConfig) else "no unit configuration"
                cells["Plan"] = "unavailable: " + reason
                problems.append(reason)
        doc = resolver.production_doc(row)
        if not isinstance(doc, session_staff.ProductionDoc):
            problems.append(doc.reason)
        if problems:
            causes = dict.fromkeys(" ".join(reason.splitlines()) for reason in problems)
            issues.append(UnitProblem(row.id, row.name, "; ".join(causes)))
        metadata[row.id] = cells
    return UnitMetadata(metadata, tuple(issues))


@dataclass(frozen=True)
class UnitProduction:
    """The current parent and its source document, resolved from the staff edge."""

    name: str
    doc: str


def unit_row(unit: str, record: staff.StaffSnapshot, *,
             reference: session_staff.StoredReference | session_staff.LiveSession | session_staff.ByName = session_staff.BY_NAME) -> staff.StaffRow:
    """Resolve a selected identity without a name fallback."""
    found = _unit_match(ObservedUnit(unit), record, reference)
    if isinstance(found, NoSuchMember):
        raise staff.StaffUnreadable(f"{unit}: no unique staff entry")
    return found.row


def row_production(row: staff.StaffRow, record: staff.StaffSnapshot) -> UnitProduction:
    """Resolve a unit's current parent from the caller's owner snapshot."""
    parent = record.parent_of(row)
    if parent is not None and parent.kind == "showrunner" and isinstance(parent.doc, (staff.LiveDoc, staff.WrappedDoc)):
        return UnitProduction(parent.name, str(parent.doc.path))
    raise staff.StaffUnreadable(f"{row.name}: no showrunner parent document")


@dataclass(frozen=True)
class NoSessionShowrunner:
    """The session has no staff row with a showrunner at or above it."""


def session_showrunner(session_id: str, *, record: staff.StaffSnapshot,
                       sources: MemberSources) -> MemberFound | NoSessionShowrunner:
    """Follow the newest launch's staff id to its first showrunner."""
    row = record.by_current_session(session_id)
    launches = [launch for launch in sources.launches
                if row is not None and record.by_member_reference(launch["session_id"]) == row]
    if not launches:
        return NoSessionShowrunner()
    while row is not None:
        if row.kind == "showrunner":
            return MemberFound(row)
        row = record.by_id(row.parent.id) if isinstance(row.parent, staff.ReportsTo) else None
    return NoSessionShowrunner()


def unit_parent(unit: str,
                reference: session_staff.StoredReference | session_staff.LiveSession | session_staff.ByName = session_staff.BY_NAME, *,
                record: staff.StaffSnapshot) -> UnitProduction:
    """Resolve identity and its current parent without another owner read."""
    row = unit_row(unit, record, reference=reference)
    return row_production(row, record)


class UnitState(Enum):
    RUNNING = "running"
    RUN_FINISHED = "run-finished"
    STANDING_BY = "standing-by"


def runs_dir() -> Path:
    """Where /unit:direct keeps one record per run. Read at each call, as the recorder reads it."""
    return unit_runs.history_root() / "runs"


class RunRecord(NamedTuple):
    path: Path
    state: UnitState


class HeldRun(NamedTuple):
    """A directed run with its history, worktree, state, and held folder."""

    path: Path
    worktree: Path
    state: UnitState
    folder: Path


@dataclass(frozen=True)
class NoUnfinishedRun:
    """The director holds no run with an unfinished history."""


@dataclass(frozen=True)
class HeldRunUnknown:
    """A held history cannot establish the unit's run state."""

    path: Path
    reason: str


class HeldRunLookup:
    """Read only held histories and retain each result for one watch pass."""

    def __init__(self, *, record: staff.StaffSnapshot | None = None) -> None:
        self._record: staff.StaffSnapshot | None = record
        self._histories: dict[Path, HeldRun | HeldRunUnknown] = {}

    def unfinished_held_run(self, session_id: str) -> HeldRun | HeldRunUnknown | NoUnfinishedRun:
        if not session_id:
            return NoUnfinishedRun()
        if self._record is None:
            self._record = staff.snapshot()
        row = self._record.by_current_session(session_id)
        folders = [row.run.path] if row is not None and isinstance(row.run, staff.OpenRun) else []
        for folder in folders:
            if unit_runs.director(folder, record=self._record) != session_id:
                continue
            path = runs_dir() / (Path(folder).name + ".jsonl")
            if path not in self._histories:
                try:
                    with path.open("rb") as handle:
                        first = _event(handle.readline().decode("utf-8"))
                    if first is None or first.get("event_type") != "run_started":
                        raise ValueError("history does not start with run_started")
                    row = self._record.by_member_reference(session_id)
                    if row is None or isinstance(row.worktree, staff.WorktreeUnfilled):
                        raise ValueError(f"{session_id}: the staff worktree is unavailable")
                    state = UnitState.RUNNING if unit_runs.run_is_open(Path(folder)) else UnitState.RUN_FINISHED
                    self._histories[path] = HeldRun(path, row.worktree.path, state, Path(folder))
                except (OSError, UnicodeError, ValueError) as error:
                    self._histories[path] = HeldRunUnknown(path, str(error))
            run = self._histories[path]
            if isinstance(run, HeldRunUnknown) or run.state is UnitState.RUNNING:
                return run
        return NoUnfinishedRun()


class RunStates(NamedTuple):
    records: dict[str, RunRecord]

    def state(self, worktree: Path) -> UnitState:
        record = self.records.get(str(worktree.expanduser().resolve()))
        return record.state if record is not None else UnitState.STANDING_BY


class _StartedRun(NamedTuple):
    at: float
    path: Path


def _event(line: str) -> dict[str, object] | None:
    try:
        event = cast(object, json.loads(line))
    except ValueError:
        return None
    return cast(dict[str, object], event) if isinstance(event, dict) else None


def read_run_states(*, record: staff.StaffSnapshot | None = None) -> RunStates:
    """Join each run's director to its staff folder and read the newest run state."""
    directory = runs_dir()
    record = staff.snapshot() if record is None else record
    newest: dict[str, _StartedRun] = {}
    for path in directory.glob("*.jsonl"):
        with path.open("rb") as handle:
            first = _event(handle.readline().decode("utf-8"))
        if first is None or first.get("event_type") != "run_started":
            continue
        row = unit_runs.run_owner(first, record)
        if isinstance(row, unit_runs.RunOwnerUnrecorded) or isinstance(row.worktree, staff.WorktreeUnfilled):
            continue
        worktree = str(row.worktree.path)
        started = first.get("run_started_at")
        at = float(started) if isinstance(started, (int, float)) and not isinstance(started, bool) else 0.0
        if worktree not in newest or at > newest[worktree].at:
            newest[worktree] = _StartedRun(at, path)
    records: dict[str, RunRecord] = {}
    for worktree, run in newest.items():
        records[worktree] = RunRecord(run.path, UnitState.RUN_FINISHED)
    for row in record.rows:
        if isinstance(row.run, staff.OpenRun) and not isinstance(row.worktree, staff.WorktreeUnfilled):
            if unit_runs.run_is_open(row.run.path):
                path = directory / f"{row.run.path.name}.jsonl"
                records[str(row.worktree.path)] = RunRecord(path, UnitState.RUNNING)
    return RunStates(records)


def run_state(worktree: Path) -> UnitState:
    """A unit's run state, read from the newest record of a /unit:direct run in its worktree.

    No record means the unit has started no run, so it is standing by. Raises OSError when the
    records cannot be read, which says nothing about the unit.
    """
    return read_run_states().state(worktree)


class RecordsRead(NamedTuple):
    """Every session record was readable."""


class RecordsPartlyUnread(NamedTuple):
    reason: str


def _place_of(pid: int, places: dict[int, HerdrPlace]) -> HerdrPlace | None:
    from scripts.herdr import herdr

    place = places.get(pid)
    if place is not None:
        return place
    # A live registry PID may be absent from the snapshot because its
    # working directory was deleted. Placement needs no cwd.
    placement = herdr.herdr_place_of(pid)
    return None if isinstance(placement, herdr.NotInHerdr) else placement


class UnreadAt(NamedTuple):
    """Records that could not be read: the panes they belong to, and whether any names no process."""

    places: dict[HerdrPlace, str]
    unplaced: RecordsRead | RecordsPartlyUnread


def _claudes(processes: list[herdr.HerdrProcess], *,
             running: Sequence[SessionRecord] | None = None) -> tuple[dict[int, LiveClaude], UnreadAt]:
    """Newest live Claude record per pid, and the panes whose record could not be read.

    Claude names each record after its pid. A record that cannot be read for a pid that no longer runs,
    or whose file was removed after the listing, is a session that has exited: during an account move
    many exit at once. It makes only its own pane unknown, never every pane.
    """
    places = {process.pid: process.place for process in processes}
    entries: Iterable[SessionRecord]
    newest: dict[int, tuple[int, LiveClaude]] = {}
    unread: dict[HerdrPlace, str] = {}
    unplaced: RecordsRead | RecordsPartlyUnread = RecordsRead()
    if running is None:
        registry = sessions.read_registry()
        if sessions.sessions_dir() in registry.unreadable:
            return {}, UnreadAt({}, RecordsPartlyUnread("cannot list session records"))
        entries = (item.record for item in registry.entries)
        for path in registry.unreadable:
            pid = int(path.stem) if path.stem.isdigit() else None
            place = None if pid is None else _place_of(pid, places)
            if pid is None:
                unplaced = RecordsPartlyUnread("one or more session records could not be read")
            elif place is not None:
                unread[place] = f"the session record of pid {pid} could not be read"
    else:
        entries = running
    for record in entries:
        if not live_session(record):
            # A running registered Claude with no messaging socket cannot be
            # called absent. Preserve the existing fail-closed unknown state.
            if sessions.running_session(record):
                place = _place_of(record["pid"], places)
                if place is not None:
                    unread[place] = f"the live session of pid {record['pid']} has no messaging socket"
            continue
        place = _place_of(record["pid"], places)
        if place is None:
            continue
        if record["pid"] not in newest or record["updatedAt"] > newest[record["pid"]][0]:
            newest[record["pid"]] = (record["updatedAt"], LiveClaude(record["name"], record["sessionId"],
                                                           record["messagingSocketPath"], record["pid"]))
    return {pid: claude for pid, (_, claude) in newest.items()}, UnreadAt(unread, unplaced)


def _herdr_labeled(claudes: dict[int, LiveClaude], unread: UnreadAt,
                  processes: list[herdr.HerdrProcess]) -> Iterable[LabeledSession]:
    """Join live Claude by PID, retaining stopped panes and unknown evidence."""
    from scripts.herdr import herdr

    running = {session["name"] for session in herdr.live_sessions()}
    holders = {process.pid: (process.place, claudes[process.pid].session_id if process.pid in claudes else
                            process.marks.get("CODEX_THREAD_ID", "") or process.marks.get("CLAUDE_CODE_SESSION_ID", ""))
               for process in processes}
    process_counts = Counter(process.place for process in processes)
    inherited: set[int] = set()
    agents = [process for process in processes if holders[process.pid][1]
              and (process.pid in claudes or process.command in ("claude", "codex"))]
    if agents:
        from scripts.message import process_table

        darwin = os.environ.get("HERDR_PLATFORM", sys.platform) == "darwin"
        try:
            parents = process_table.snapshot() if darwin else {}
        except (OSError, ValueError, IndexError, subprocess.SubprocessError):
            parents = {}
        for process in agents:
            try:
                current = process.pid
                seen: set[int] = {current}
                while current > 1:
                    if darwin:
                        parent = parents.get(current)
                        if parent is None:
                            break
                        current = parent.parent
                    else:
                        current = herdr.linux_process_parent(current)
                    if current in seen:
                        break
                    seen.add(current)
                    if holders.get(current) == holders[process.pid]:
                        # A registered Claude keeps its pane; its headless claude child is the copy.
                        inherited.add(process.pid if current in claudes else current)
            except (OSError, ValueError, IndexError, subprocess.SubprocessError):
                # Keep this process evidence when its ancestry is unavailable.
                pass
    for process in processes:
        if process.pid in inherited:
            continue
        if process.place.session not in running:
            continue
        native = holders[process.pid][1]
        problem = unread.places.get(process.place, unread.unplaced.reason
                                    if isinstance(unread.unplaced, RecordsPartlyUnread) else "")
        if problem and process.pid not in claudes:
            _ = session_staff.collect_anomaly(f"pid {process.pid} in {place_text(process.place)}: {problem}", pid=process.pid)
        if not native:
            if process.place in unread.places or process.command in ("claude", "codex"):
                yield LabeledSession("", ClaudeUnknown("native runtime identity is unavailable"), process.place,
                                     ObservedProcess(process.pid, process.command))
            continue
        claude: Claude
        if process.pid in claudes:
            claude = claudes[process.pid]
        elif problem:
            claude = ClaudeUnknown(problem)
        elif (process.command in ("sh", "bash", "zsh", "dash", "fish", "ksh") and process_counts[process.place] == 1
              and process.marks.get("CLAUDE_CODE_SESSION_ID") == native
              and not process.marks.get("CODEX_THREAD_ID")):
            claude = ClaudeNotRunning()
        else:
            claude = ClaudeUnknown("native process has no registered Claude session")
        yield LabeledSession("", claude, process.place,
                                 ObservedProcess(process.pid, process.command), LiveSession(native))


def observed_session_labels(*, record: staff.StaffSnapshot | None = None, running: Sequence[SessionRecord] | None = None) -> tuple[LabeledSession, ...]:
    """Read native process identities for stop and cleanup checks."""
    from scripts.herdr import herdr

    try:
        record = record if record is not None else staff.snapshot()
        processes = herdr.herdr_processes(membership=StaffLabels(record))
        claudes, unread = _claudes(processes, running=running)
        return tuple(_herdr_labeled(claudes, unread, processes))
    except herdr_types.HerdrFailure as error:
        raise OSError(str(error)) from error


def every_labeled_session(*, record: staff.StaffSnapshot | None = None,
                      sources: MemberSources | None = None,
                      running: Sequence[SessionRecord] | None = None) -> LabeledProductions:
    """Read all herdr units once. An unreadable host raises OSError."""
    record = record if record is not None else staff.snapshot()
    return group_labeled_sessions(observed_session_labels(record=record, running=running), record=record, sources=sources)


def labeled_sessions(production_name: str, *, record: staff.StaffSnapshot | None = None,
                 sources: MemberSources | None = None) -> dict[str, LabeledSession]:
    """Return the selected process state for each unit."""
    found = every_labeled_session(record=record, sources=sources)
    return found.status_units(production_name)


def labeled_session(production_name: str, name: str, *, record: staff.StaffSnapshot | None = None,
                sources: MemberSources | None = None,
                running: Sequence[SessionRecord] | None = None) -> LabeledSession | None:
    """Return one unit of the production. Unreadable herdr raises OSError."""
    record = record if record is not None else staff.snapshot()
    sources = sources if sources is not None else member_sources()
    selected = sources.member(UnitMember(production_name, name), record)
    label = selected.row.name if isinstance(selected, MemberFound) else name
    marked = observed_session_labels(record=record, running=running)
    if isinstance(selected, MemberFound):
        # A retained row still needs pane liveness checks when cleanup is retried.
        found = retained_session_labels(marked, selected.row, production_name, record=record, sources=sources)
    else:
        found = group_labeled_sessions(marked, record=record, sources=sources)
    return found.units.get(production_name, {}).get(label)


def main(arguments: list[str]) -> int:
    """Exit 0 found or done, 1 no such unit session, 2 usage, 3 cannot tell or could not do it."""
    try:
        match arguments:
            case ["list", production_name]:
                for unit in labeled_sessions(production_name).values():
                    claude = unit.claude
                    told = ((claude.name, claude.session_id, claude.socket) if isinstance(claude, LiveClaude)
                            else ("", "", ""))
                    running = ("live" if isinstance(claude, LiveClaude)
                               else "unknown" if isinstance(claude, ClaudeUnknown) else "stopped")
                    print("\t".join((unit.unit, place_text(unit.place), unit.place.session, running, *told)))
            case ["pane", production_name, name]:
                found = labeled_session(production_name, name)
                if found is None:
                    return 1
                print(place_text(found.place))
            case _:
                print(USAGE, file=sys.stderr)
                return 2
    except OSError as error:
        print(f"unit_lookup: {error}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
