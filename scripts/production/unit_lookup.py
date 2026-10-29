#!/usr/bin/env python3
"""Find production units by marks on herdr panes.

Current names, places and messaging sockets come from herdr and live session
records. Run state comes from /unit:direct records. Duplicate marks are refused.
"""
from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import NamedTuple, cast

MESSAGE = Path(__file__).resolve().parent.parent / "message"
sys.path.insert(0, str(MESSAGE))
sys.path.insert(0, str(MESSAGE.parent / "herdr"))
sys.path.insert(0, str(MESSAGE.parents[1]))
import herdr  # noqa: E402
import showrunners  # noqa: E402
from herdr import HerdrPlace  # noqa: E402
from sessions import SessionRecord, UnreadableSessionRecord, live_session, read_session  # noqa: E402
from showrunners import canonical_slug  # noqa: E402
from scripts.lib import unit_runs  # noqa: E402

PRODUCTION_MARK = "SHOWRUNNER_UNIT"
UNIT_MARK = "SHOWRUNNER_UNIT_ID"
USAGE = "usage: unit_lookup.py list <production slug> | pane <production slug> <unit>"


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


def place_text(place: Place) -> str:
    return f"herdr:{place.session}:{place.pane}"


class MarkedProductions(NamedTuple):
    # Each production's marked units, by production slug and then unit id.
    units: dict[str, dict[str, MarkedUnit]]
    # Why a unit was left out, by production slug and then unit id: two sessions carry its mark.
    faults: dict[str, dict[str, str]]

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


def _claudes(processes: list[herdr.HerdrProcess]) -> tuple[dict[HerdrPlace, LiveClaude], UnreadAt]:
    """Newest live Claude per herdr pane, and the panes whose record could not be read.

    Claude names each record after its pid. A record that cannot be read for a pid that no longer runs,
    or whose file was removed after the listing, is a session that has exited: during an account move
    many exit at once. It makes only its own pane unknown, never every pane.
    """
    places = {process.pid: process.place for process in processes}
    try:
        paths = sorted(path for path in sessions_dir().iterdir() if path.suffix == ".json")
    except OSError as error:
        return {}, UnreadAt({}, RecordsPartlyUnread(f"cannot list session records: {error}"))
    newest: dict[HerdrPlace, tuple[int, LiveClaude]] = {}
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
            continue
        place = _place_of(record["pid"], places)
        if place is None:
            continue
        if place not in newest or record["updatedAt"] > newest[place][0]:
            newest[place] = (record["updatedAt"], LiveClaude(record["name"], record["sessionId"],
                                                           record["messagingSocketPath"], record["pid"]))
    return {place: claude for place, (_, claude) in newest.items()}, UnreadAt(unread, unplaced)


def _herdr_marked(claudes: dict[HerdrPlace, LiveClaude], unread: UnreadAt,
                  processes: list[herdr.HerdrProcess]) -> Iterable[tuple[str, MarkedUnit]]:
    """Each running herdr pane carrying both marks, once per place."""
    running = {session["name"] for session in herdr.live_sessions()}
    seen: set[HerdrPlace] = set()
    for process in processes:
        place = process.place
        if place.session not in running or place in seen:
            continue
        slug, unit = process.marks.get(PRODUCTION_MARK, ""), process.marks.get(UNIT_MARK, "")
        if not slug or not unit:
            continue
        seen.add(place)
        claude: Claude
        if place in claudes:
            claude = claudes[place]
        elif place in unread.places:
            claude = ClaudeUnknown(unread.places[place])
        elif isinstance(unread.unplaced, RecordsPartlyUnread):
            claude = ClaudeUnknown(unread.unplaced.reason)
        else:
            claude = ClaudeNotRunning()
        yield slug, MarkedUnit(unit, claude, place)


def _lookup_slug(slug: str) -> str:
    # A current notifier name needs no scan of Formerly entries or reread of its doc.
    if (showrunners.NOTIFIER_STATE_DIR / f"showrunner-{slug}").is_dir():
        return slug
    return canonical_slug(slug)


def _claude_places(places: list[MarkedUnit]) -> list[MarkedUnit]:
    """The unit's places that may hold its Claude, when one does; else every place.

    The marks are environment variables, so every child process inherits them. A detached run, such as
    a worker's build that mesh_runs supervises, stays in the herdr service's cgroup with the unit's marks
    and a pane id that can be out of date. Such a process is not a second session of the unit.
    """
    with_claude = [place for place in places if not isinstance(place.claude, ClaudeNotRunning)]
    return with_claude or places


def every_marked_unit() -> MarkedProductions:
    """Read all herdr units once. An unreadable host raises OSError."""
    try:
        processes = herdr.herdr_processes()
        claudes, unread = _claudes(processes)
        marked = list(_herdr_marked(claudes, unread, processes))
    except herdr.HerdrFailure as error:
        raise OSError(str(error)) from error
    return group_marked_units(marked)


def group_marked_units(marked: Iterable[tuple[str, MarkedUnit]]) -> MarkedProductions:
    """Resolve current production slugs and refuse units marked in more than one place."""
    found: dict[str, dict[str, list[MarkedUnit]]] = {}
    current_slugs: dict[str, str] = {}
    for slug, unit in marked:
        if slug not in current_slugs:
            current_slugs[slug] = _lookup_slug(slug)
        found.setdefault(current_slugs[slug], {}).setdefault(unit.unit, []).append(unit)
    units: dict[str, dict[str, MarkedUnit]] = {}
    faults: dict[str, dict[str, str]] = {}
    for slug, production in found.items():
        production = {name: _claude_places(places) for name, places in production.items()}
        units[slug] = {name: places[0] for name, places in production.items() if len(places) == 1}
        for name, places in production.items():
            if len(places) > 1:
                sessions = " and ".join(sorted(place.place.session for place in places))
                faults.setdefault(slug, {})[name] = f"{name}: its mark is on herdr sessions {sessions}"
    return MarkedProductions(units, faults)


def session_unit_marks(
    records: list[SessionRecord], environment: Mapping[str, str],
) -> tuple[MarkedProductions, dict[str, str]]:
    """Read unit marks only for known session PIDs, without enumerating processes."""
    marked: list[tuple[str, MarkedUnit]] = []
    directors: dict[str, str] = {}
    known = {record["sessionId"] for record in records}
    proc = Path(environment.get("HERDR_PROC", "/proc"))
    for record in records:
        try:
            # Herdr's Linux environment reader is private; keep this PID-only IO here.
            raw = (proc / str(record["pid"]) / "environ").read_bytes()
            values = dict(item.split("=", 1) for item in raw.decode(errors="replace").split("\0")
                          if "=" in item)
        except OSError:
            continue
        owner = values.get("CLAUDE_CODE_SESSION_ID", "")
        if owner in known and owner != record["sessionId"]:
            directors[record["sessionId"]] = owner
            continue
        slug = values.get(PRODUCTION_MARK, "")
        unit = values.get(UNIT_MARK, "")
        if slug and unit:
            unit_mark = MarkedUnit(
                unit, LiveClaude(record["name"], record["sessionId"],
                                 record["messagingSocketPath"], record["pid"]),
                herdr.HerdrPlace(values.get("HERDR_SESSION", ""), values.get("HERDR_PANE_ID", "")),
            )
            marked.append((slug, unit_mark))
    return group_marked_units(marked), directors


def marked_units(slug: str) -> dict[str, MarkedUnit]:
    """Every unit of the production. Unreadable herdr, or any unit's mark on two sessions, raises OSError."""
    slug = _lookup_slug(slug)
    found = every_marked_unit()
    if fault := found.production_fault(slug):
        raise OSError(fault)
    return found.units.get(slug, {})


def marked_unit(slug: str, name: str) -> MarkedUnit | None:
    """One unit of the production. Unreadable herdr, or this unit's mark on two sessions, raises OSError."""
    slug = _lookup_slug(slug)
    found = every_marked_unit()
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
