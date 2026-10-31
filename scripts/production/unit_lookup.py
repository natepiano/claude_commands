#!/usr/bin/env python3
"""Find recorded production units and join their herdr panes by staff identity.

Current names, places and messaging sockets come from herdr and live session
records. Run state comes from /unit:direct records. Duplicate marks are refused.
"""
from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import NamedTuple, cast

MESSAGE = Path(__file__).resolve().parent.parent / "message"
sys.path.insert(0, str(MESSAGE))
sys.path.insert(0, str(MESSAGE.parent / "herdr"))
sys.path.insert(0, str(MESSAGE.parents[1]))
import herdr  # noqa: E402
import showrunners  # noqa: E402
import sessions  # noqa: E402
from herdr import HerdrPlace  # noqa: E402
from sessions import SessionRecord, UnreadableSessionRecord, live_session, read_session  # noqa: E402
from showrunners import canonical_slug  # noqa: E402
import unit_names  # noqa: E402
from scripts.production import staff  # noqa: E402
from scripts.shutdown import pane_record  # noqa: E402
from scripts.lib import unit_runs  # noqa: E402

PRODUCTION_MARK = "SHOWRUNNER_UNIT"
UNIT_MARK = "SHOWRUNNER_UNIT_ID"
USAGE = "usage: unit_lookup.py list <production slug> | pane <production slug> <unit>"


@dataclass(frozen=True)
class ShowrunnerMember:
    slug: str


@dataclass(frozen=True)
class UnitMember:
    slug: str
    unit: str


@dataclass(frozen=True)
class ObservedUnit:
    """A pane identity, independent of its launch-time production mark."""

    unit: str
    staff_id: str = ""


MemberSelector = ShowrunnerMember | UnitMember | ObservedUnit


@dataclass(frozen=True)
class MemberFound:
    row: staff.StaffRow


@dataclass(frozen=True)
class NoSuchMember:
    selector: MemberSelector


def _unit_match(selector: UnitMember | ObservedUnit, record: staff.StaffSnapshot,
                staff_id: str = "", *, parent: staff.StaffRow | None = None) -> MemberFound | NoSuchMember:
    """One id-first join; production requests also require their current parent."""
    if staff_id:
        row = record.by_id(staff.StaffId(staff_id))
        matches = [row] if row is not None and row.kind == "unit" else []
    else:
        matches = [row for row in record.find(kind="unit") if row.name == unit_names.name(selector.unit)]
    if isinstance(selector, UnitMember):
        matches = [row for row in matches if parent is not None and row.parent == parent.id]
    if len(matches) > 1:
        raise staff.StaffUnreadable(f"{selector.unit}: no unique staff entry")
    return MemberFound(matches[0]) if matches else NoSuchMember(selector)


@dataclass(frozen=True)
class MemberSources:
    productions: dict[str, showrunners.ShowrunnerSource]
    launches: tuple[pane_record.PaneLaunch, ...]

    def member(self, selector: MemberSelector, record: staff.StaffSnapshot) -> MemberFound | NoSuchMember:
        if isinstance(selector, ObservedUnit):
            return _unit_match(selector, record, selector.staff_id)
        source = self.productions.get(selector.slug)
        if isinstance(selector, UnitMember):
            parent = self.member(ShowrunnerMember(selector.slug), record)
            if isinstance(parent, NoSuchMember):
                return NoSuchMember(selector)
            # A transferred launch keeps its old production mark. Only its row's
            # current parent admits it to this request; timestamps cannot choose
            # between identities from different productions.
            launches = [launch for launch in self.launches
                        if launch["environment"].get(UNIT_MARK) == selector.unit
                        and launch["environment"].get("STAFF_ID")]
            local = [launch for launch in launches
                     if launch["environment"].get(PRODUCTION_MARK) == selector.slug]
            if local:
                latest = max(local, key=lambda launch: launch["written_at"])
                found = _unit_match(selector, record, latest["environment"]["STAFF_ID"], parent=parent.row)
                if isinstance(found, MemberFound):
                    return found
                # A missing durable id is not permission to reuse its old name.
                if (identity := record.by_id(staff.StaffId(latest["environment"]["STAFF_ID"]))) is None or identity.kind != "unit":
                    return NoSuchMember(selector)
            identities: dict[staff.StaffId, MemberFound] = {}
            for launch in launches:
                found = _unit_match(selector, record, launch["environment"]["STAFF_ID"], parent=parent.row)
                if isinstance(found, MemberFound):
                    identities[found.row.id] = found
            if len(identities) > 1:
                raise staff.StaffUnreadable(f"{selector.slug}/{selector.unit}: conflicting staff identities")
            if identities:
                return next(iter(identities.values()))
            return _unit_match(selector, record, parent=parent.row)
        if source is None:
            row = record.by_path(selector.slug)
            return MemberFound(row) if row is not None and row.kind == "showrunner" else NoSuchMember(selector)
        launches = [launch for launch in self.launches
                    if f"session:{launch['session_id']}" == source.timer.target]
        staff_id = max(launches, key=lambda launch: launch["written_at"])["environment"].get("STAFF_ID", "") if launches else ""
        if staff_id:
            row = record.by_id(staff.StaffId(staff_id))
            return MemberFound(row) if row is not None and row.kind == "showrunner" else NoSuchMember(selector)
        row = record.by_path(showrunners.space_from_text(selector.slug, source.document))
        return MemberFound(row) if row is not None and row.kind == "showrunner" else NoSuchMember(selector)

    def showrunner_members(self, record: staff.StaffSnapshot) -> dict[str, staff.StaffRow]:
        found: dict[str, staff.StaffRow] = {}
        for slug in self.productions:
            selected = self.member(ShowrunnerMember(slug), record)
            if isinstance(selected, MemberFound):
                found[slug] = selected.row
        return found


def member_sources() -> MemberSources:
    session = herdr.shared_session()
    try:
        launches = pane_record.read_strict(session)
    except OSError as error:
        raise staff.StaffUnreadable(f"{pane_record.record_path(session)}: {error}") from error
    except ValueError as error:
        raise staff.StaffUnreadable(str(error)) from error
    sources = showrunners.read_sources(unreadable=showrunners.UnreadableTimerPolicy.SKIP)
    return MemberSources({source.timer.slug: source for source in sources}, tuple(launches.values()))


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


class LiveClaude(NamedTuple):
    name: str
    session_id: str
    socket: str
    pid: int


class ClaudeNotRunning(NamedTuple):
    pass


class ClaudeUnknown(NamedTuple):
    """The session records could not all be read, which says nothing about whether Claude runs."""

    reason: str


Claude = LiveClaude | ClaudeNotRunning | ClaudeUnknown


Place = HerdrPlace


@dataclass(frozen=True)
class MarkedUnit:
    unit: str
    claude: Claude
    place: Place
    pid: int = 0
    command: str = ""
    staff_id: str = ""


def place_text(place: Place) -> str:
    return f"herdr:{place.session}:{place.pane}"


def holder_text(unit: MarkedUnit) -> str:
    """Identify the process whose marks caused a refusal, including uncertain holders."""
    pid = unit.pid or (unit.claude.pid if isinstance(unit.claude, LiveClaude) else 0)
    command = unit.command or ("claude" if isinstance(unit.claude, LiveClaude) else "unknown")
    return f"pid {pid} ({command}) in {place_text(unit.place)}"


@dataclass(frozen=True)
class MarkedProductions:
    # Each production's marked units, by production slug and then unit id.
    units: dict[str, dict[str, MarkedUnit]]
    # Why a unit was left out, by production slug and then unit id: two sessions carry its mark.
    faults: dict[str, dict[str, str]]
    members: dict[str, tuple[str, ...]] = field(default_factory=dict, compare=False)

    def production_fault(self, slug: str) -> str:
        """Every left-out unit of the production, in one line; empty when none was left out."""
        return "; ".join(detail for _, detail in sorted(self.faults.get(slug, {}).items()))


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


def _lookup_slug(slug: str) -> str:
    # A current notifier name needs no scan of Formerly entries or reread of its doc.
    if (showrunners.NOTIFIER_STATE_DIR / f"showrunner-{slug}").is_dir():
        return slug
    return canonical_slug(slug)


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


def production_members(record: staff.StaffSnapshot, sources: MemberSources) -> dict[str, tuple[staff.StaffRow, ...]]:
    """All recorded unit children, including those with no pane or local worktree."""
    runners = sources.showrunner_members(record)
    # A timer is not required for an offline production whose short name is its slug.
    mapped = {row.id for row in runners.values()}
    runners.update({row.name: row for row in record.find(kind="showrunner")
                    if row.id not in mapped and row.name not in runners})
    return {slug: tuple(record.find(kind="unit", parent=row.id)) for slug, row in runners.items()}


def group_marked_units(marked: Iterable[tuple[str, MarkedUnit]], *,
                       record: staff.StaffSnapshot | None = None) -> MarkedProductions:
    """Join panes to the recorded parent; refuse ambiguous legacy names and duplicate panes."""
    record = record if record is not None else staff.snapshot()
    sources = member_sources()
    membership = production_members(record, sources)
    parents = {row.id: slug for slug, rows in membership.items() for row in rows}
    found: dict[str, dict[staff.StaffId, list[MarkedUnit]]] = {}
    faults: dict[str, dict[str, str]] = {}
    for old_slug, unit in marked:
        try:
            selected = sources.member(ObservedUnit(unit.unit, unit.staff_id), record)
            if isinstance(selected, NoSuchMember):
                continue
            row = selected.row
            slug = parents.get(row.id)
            if slug is None:
                raise staff.StaffUnreadable(f"{row.name}: no showrunner staff parent")
        except (staff.StaffUnreadable, unit_names.BadName) as error:
            faults.setdefault(old_slug, {})[unit.unit] = str(error)
            continue
        found.setdefault(slug, {}).setdefault(row.id, []).append(unit)
    units: dict[str, dict[str, MarkedUnit]] = {}
    names: dict[staff.StaffId, str] = {}
    for slug, production in found.items():
        units[slug] = {}
        for identity, places in production.items():
            row = record.by_id(identity)
            assert row is not None
            # Inherited marks preserve a stopped pane for recovery, but cannot
            # compete with a live Claude or unresolved session for ownership.
            holders = [place for place in places if isinstance(place.claude, (LiveClaude, ClaudeUnknown))]
            selected = holders[0] if holders else places[0]
            name = (selected.unit if selected.unit and unit_names.name(selected.unit) == row.name
                    else unit_names.unit_id(row.name))
            names[identity] = name
            if len(holders) > 1:
                sessions = " and ".join(sorted(holder_text(place) for place in holders))
                faults.setdefault(slug, {})[name] = f"{name}: its mark is on herdr sessions {sessions}"
            else:
                units[slug][name] = selected if selected.unit == name else replace(selected, unit=name)
    return MarkedProductions(units, faults, {slug: tuple(names.get(row.id, unit_names.unit_id(row.name)) for row in rows)
                                           for slug, rows in membership.items()})


def session_unit_marks(
    records: list[SessionRecord], environment: Mapping[str, str], *, record: staff.StaffSnapshot | None = None,
) -> tuple[MarkedProductions, dict[str, str]]:
    """Read unit marks only for known session PIDs, without enumerating processes."""
    marked: list[tuple[str, MarkedUnit]] = []
    directors: dict[str, str] = {}
    known = {session["sessionId"] for session in records}
    proc = Path(environment.get("HERDR_PROC", "/proc"))
    for session in records:
        try:
            # Herdr's Linux environment reader is private; keep this PID-only IO here.
            raw = (proc / str(session["pid"]) / "environ").read_bytes()
            values = dict(item.split("=", 1) for item in raw.decode(errors="replace").split("\0")
                          if "=" in item)
        except OSError:
            continue
        owner = values.get("CLAUDE_CODE_SESSION_ID", "")
        if owner in known and owner != session["sessionId"]:
            directors[session["sessionId"]] = owner
            continue
        slug = values.get(PRODUCTION_MARK, "")
        unit = values.get(UNIT_MARK, "")
        staff_id = values.get("STAFF_ID", "")
        if unit or staff_id:
            unit_mark = MarkedUnit(
                unit, LiveClaude(session["name"], session["sessionId"],
                                 session["messagingSocketPath"], session["pid"]),
                herdr.HerdrPlace(values.get("HERDR_SESSION", ""), values.get("HERDR_PANE_ID", "")),
                staff_id=staff_id,
            )
            marked.append((slug, unit_mark))
    return group_marked_units(marked, record=record), directors


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
