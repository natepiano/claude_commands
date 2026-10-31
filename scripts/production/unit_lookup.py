#!/usr/bin/env python3
"""Find recorded production units and join their herdr panes by staff identity.

Current names, places and messaging sockets come from herdr and live session
records. Run state comes from /unit:direct records. Duplicate marks are refused.
"""
from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import NamedTuple, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

MESSAGE = Path(__file__).resolve().parent.parent / "message"
sys.path.insert(0, str(MESSAGE))
sys.path.insert(0, str(MESSAGE.parent / "herdr"))
sys.path.insert(0, str(MESSAGE.parents[1]))
import herdr  # noqa: E402
import sessions  # noqa: E402
from herdr import HerdrPlace  # noqa: E402
from sessions import UnreadableSessionRecord, live_session, read_session  # noqa: E402
from scripts.production import staff  # noqa: E402
from scripts.lib import unit_runs  # noqa: E402

USAGE = "usage: unit_lookup.py list <production slug> | pane <production slug> <unit>"


from unit_marks import (
    PRODUCTION_MARK as PRODUCTION_MARK,
    UNIT_MARK as UNIT_MARK,
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
    MarkedUnit as MarkedUnit,
    place_text as place_text,
    holder_text as holder_text,
    MarkedProductions as MarkedProductions,
    lookup_slug as _lookup_slug,
    production_members as production_members,
    group_marked_units as group_marked_units,
    session_unit_marks as session_unit_marks,
)


@dataclass(frozen=True)
class UnitProduction:
    """The current parent and its source document, resolved from the staff edge."""

    slug: str
    doc: str


def unit_row(unit: str, record: staff.StaffSnapshot, *, staff_id: str = "") -> staff.StaffRow:
    """Join a pane by durable id, or by its unique legacy name until 2026-11-13."""
    found = _unit_match(ObservedUnit(unit, staff_id), record, staff_id)
    if isinstance(found, NoSuchMember):
        raise staff.StaffUnreadable(f"{unit or staff_id}: no unique staff entry")
    return found.row


def row_production(row: staff.StaffRow, record: staff.StaffSnapshot,
                   sources: MemberSources) -> UnitProduction:
    """Resolve a unit's current parent from the caller's owner snapshot."""
    parents = [slug for slug, parent in sources.showrunner_members(record).items() if parent.id == row.parent]
    if len(parents) != 1:
        raise staff.StaffUnreadable(f"{row.name}: staff parent has no unique production document")
    slug = parents[0]
    return UnitProduction(slug, str(sources.productions[slug].timer.doc))


def unit_parent(unit: str, session_id: str = "", *, record: staff.StaffSnapshot,
                sources: MemberSources | None = None) -> UnitProduction:
    """Resolve identity and its current parent without another owner read."""
    sources = sources if sources is not None else member_sources()
    launches = [launch for launch in sources.launches if session_id and launch["session_id"] == session_id]
    environment = max(launches, key=lambda launch: launch["written_at"])["environment"] if launches else {}
    if launches:
        row = unit_row(environment.get(UNIT_MARK, unit), record, staff_id=environment.get("STAFF_ID", ""))
    else:
        selected = sources.member(ObservedUnit(unit), record)
        if isinstance(selected, NoSuchMember):
            raise staff.StaffUnreadable(f"{unit}: no unique staff entry")
        row = selected.row
    return row_production(row, record, sources)


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


class RunStates(NamedTuple):
    # Keep the recorded spelling: unit_move matches working_dir exactly.
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


def _last_event(path: Path) -> dict[str, object] | None:
    """Read backwards to the last JSON object, retaining lines across block boundaries."""
    with path.open("rb") as handle:
        position = handle.seek(0, os.SEEK_END)
        pending = b""
        block_size = 64 * 1024
        while position:
            size = min(position, block_size)
            position -= size
            _ = handle.seek(position)
            lines = (handle.read(size) + pending).split(b"\n")
            pending = lines.pop(0) if position else b""
            for line in reversed(lines):
                if not line.strip():
                    continue
                event = _event(line.decode("utf-8"))
                if event is not None:
                    return event
            block_size *= 2
    return None


def read_run_states() -> RunStates:
    """Snapshot newest runs once: each file's first line, then each newest file's last event.

    Keys keep working_dir as recorded; state() looks up a resolved worktree path. Unreadable
    records raise OSError, and invalid UTF-8 in the lines read raises UnicodeError.
    """
    directory = runs_dir()
    if not directory.exists():
        return RunStates({})
    newest: dict[str, _StartedRun] = {}
    for path in directory.iterdir():
        if path.suffix != ".jsonl":
            continue
        with path.open("rb") as handle:
            first = _event(handle.readline().decode("utf-8"))
        if first is None or first.get("event_type") != "run_started":
            continue
        worktree = first.get("working_dir")
        if not isinstance(worktree, str) or not worktree:
            continue
        started = first.get("run_started_at")
        at = float(started) if isinstance(started, (int, float)) and not isinstance(started, bool) else 0.0
        if worktree not in newest or at > newest[worktree].at:
            newest[worktree] = _StartedRun(at, path)
    records: dict[str, RunRecord] = {}
    for worktree, run in newest.items():
        last = _last_event(run.path)
        state = UnitState.RUN_FINISHED if last is not None and last.get("event_type") == "run_finished" else UnitState.RUNNING
        records[worktree] = RunRecord(run.path, state)
    return RunStates(records)


def run_state(worktree: Path) -> UnitState:
    """A unit's run state, read from the newest record of a /unit:direct run in its worktree.

    No record means the unit has started no run, so it is standing by. Raises OSError when the
    records cannot be read, which says nothing about the unit.
    """
    return read_run_states().state(worktree)


def sessions_dir() -> Path:
    return Path(os.environ.get("NOTIFIER_SESSIONS_DIR") or Path.home() / ".claude/sessions")


class RecordsRead(NamedTuple):
    """Every session record was readable."""


class RecordsPartlyUnread(NamedTuple):
    reason: str


def _place_of(pid: int, places: dict[int, HerdrPlace]) -> HerdrPlace | None:
    place = places.get(pid)
    if place is not None:
        return place
    # A live registry PID may be absent from the snapshot because its
    # working directory was deleted. Placement needs no cwd.
    placement = herdr.herdr_place_of(pid)
    return None if isinstance(placement, herdr.NotInHerdr) else placement


def _pid_runs(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (ProcessLookupError, OverflowError):
        return False
    return True


class UnreadAt(NamedTuple):
    """Records that could not be read: the panes they belong to, and whether any names no process."""

    places: dict[HerdrPlace, str]
    unplaced: RecordsRead | RecordsPartlyUnread


def _claudes(processes: list[herdr.HerdrProcess]) -> tuple[dict[int, LiveClaude], UnreadAt]:
    """Newest live Claude record per pid, and the panes whose record could not be read.

    Claude names each record after its pid. A record that cannot be read for a pid that no longer runs,
    or whose file was removed after the listing, is a session that has exited: during an account move
    many exit at once. It makes only its own pane unknown, never every pane.
    """
    places = {process.pid: process.place for process in processes}
    try:
        paths = sorted(path for path in sessions_dir().iterdir() if path.suffix == ".json")
    except OSError as error:
        return {}, UnreadAt({}, RecordsPartlyUnread(f"cannot list session records: {error}"))
    newest: dict[int, tuple[int, LiveClaude]] = {}
    unread: dict[HerdrPlace, str] = {}
    unplaced: RecordsRead | RecordsPartlyUnread = RecordsRead()
    for path in paths:
        record = read_session(path)
        if isinstance(record, UnreadableSessionRecord):
            pid = int(path.stem) if path.stem.isdigit() else None
            if pid is not None and (not path.exists() or not _pid_runs(pid)):
                continue
            # A record that Claude is writing reads complete a moment later.
            record = read_session(path)
            if isinstance(record, UnreadableSessionRecord):
                place = None if pid is None else _place_of(pid, places)
                if pid is None:
                    unplaced = RecordsPartlyUnread("one or more session records could not be read")
                elif place is not None:
                    unread[place] = f"the session record of pid {pid} could not be read"
                continue
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


def _herdr_marked(claudes: dict[int, LiveClaude], unread: UnreadAt,
                  processes: list[herdr.HerdrProcess]) -> Iterable[tuple[str, MarkedUnit]]:
    """Join live Claude by PID, retaining stopped panes and unknown evidence."""
    running = {session["name"] for session in herdr.live_sessions()}
    unknown_seen: set[tuple[HerdrPlace, str, str]] = set()
    live_marks = {(process.place, process.marks.get(PRODUCTION_MARK), process.marks.get(UNIT_MARK))
                  for process in processes if process.pid in claudes}
    for process in processes:
        place = process.place
        if place.session not in running:
            continue
        slug, unit = process.marks.get(PRODUCTION_MARK, ""), process.marks.get(UNIT_MARK, "")
        staff_id = process.marks.get("STAFF_ID", "")
        if not unit and not staff_id:
            continue
        claude: Claude
        if process.pid in claudes:
            claude = claudes[process.pid]
        elif place in unread.places:
            claude = ClaudeUnknown(unread.places[place])
        elif isinstance(unread.unplaced, RecordsPartlyUnread):
            if (place, slug, unit) in live_marks:
                continue
            claude = ClaudeUnknown(unread.unplaced.reason)
        else:
            claude = ClaudeNotRunning()
        if isinstance(claude, ClaudeUnknown):
            key = (place, slug, unit)
            if key in unknown_seen:
                continue
            unknown_seen.add(key)
        yield slug, MarkedUnit(unit, claude, place, process.pid, process.command, staff_id)


def observed_unit_marks() -> tuple[tuple[str, MarkedUnit], ...]:
    """Read pane marks for stop and cleanup checks, including former staff names."""
    try:
        processes = herdr.herdr_processes()
        claudes, unread = _claudes(processes)
        return tuple(_herdr_marked(claudes, unread, processes))
    except herdr.HerdrFailure as error:
        raise OSError(str(error)) from error


def every_marked_unit(*, record: staff.StaffSnapshot | None = None) -> MarkedProductions:
    """Read all herdr units once. An unreadable host raises OSError."""
    return group_marked_units(observed_unit_marks(), record=record)


def marked_units(slug: str, *, record: staff.StaffSnapshot | None = None) -> dict[str, MarkedUnit]:
    """Every unit of the production. Unreadable herdr, or any unit's mark on two sessions, raises OSError."""
    slug = _lookup_slug(slug)
    found = every_marked_unit(record=record)
    if fault := found.production_fault(slug):
        raise OSError(fault)
    return found.units.get(slug, {})


def marked_unit(slug: str, name: str, *, record: staff.StaffSnapshot | None = None) -> MarkedUnit | None:
    """One unit of the production. Unreadable herdr, or this unit's mark on two sessions, raises OSError."""
    slug = _lookup_slug(slug)
    found = every_marked_unit(record=record)
    if fault := found.faults.get(slug, {}).get(name):
        raise OSError(fault)
    return found.units.get(slug, {}).get(name)


def main(arguments: list[str]) -> int:
    """Exit 0 found or done, 1 no such unit session, 2 usage, 3 cannot tell or could not do it."""
    try:
        match arguments:
            case ["list", slug]:
                for unit in marked_units(slug).values():
                    claude = unit.claude
                    told = ((claude.name, claude.session_id, claude.socket) if isinstance(claude, LiveClaude)
                            else ("", "", ""))
                    running = ("live" if isinstance(claude, LiveClaude)
                               else "unknown" if isinstance(claude, ClaudeUnknown) else "stopped")
                    print("\t".join((unit.unit, place_text(unit.place), unit.place.session, running, *told)))
            case ["pane", slug, name]:
                found = marked_unit(slug, name)
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
