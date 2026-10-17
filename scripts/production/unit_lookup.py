#!/usr/bin/env python3
"""Find production units by marks on herdr panes.

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

MESSAGE = Path(__file__).resolve().parent.parent / "message"
sys.path.insert(0, str(MESSAGE))
sys.path.insert(0, str(MESSAGE.parent / "herdr"))
import herdr  # noqa: E402
import showrunners  # noqa: E402
from herdr import HerdrPlace  # noqa: E402
from sessions import UnreadableSessionRecord, live_session, read_session  # noqa: E402
from showrunners import canonical_slug  # noqa: E402

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
    # Why a production's units were left out, by production slug: two sessions carry one unit's mark.
    faults: dict[str, str]


def runs_dir() -> Path:
    """Where /unit:direct keeps one record per run. Read at each call, as the recorder reads it."""
    root = os.environ.get("PLAN_DELEGATE_HISTORY_DIR")
    return (Path(root).expanduser() if root else Path.home() / ".local/state/plan-delegate") / "runs"


def _events(path: Path) -> list[dict[str, object]]:
    """A run record's events. A line caught half-written is left out; the next read has it."""
    events: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event = cast(object, json.loads(line))
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(cast(dict[str, object], event))
    return events


def run_state(worktree: Path) -> UnitState:
    """A unit's run state, read from the newest record of a /unit:direct run in its worktree.

    No record means the unit has started no run, so it is standing by. Raises OSError when the
    records cannot be read, which says nothing about the unit.
    """
    directory = runs_dir()
    if not directory.exists():
        return UnitState.STANDING_BY
    wanted = str(worktree.expanduser().resolve())
    newest: tuple[float, list[dict[str, object]]] | None = None
    for path in directory.iterdir():
        if path.suffix != ".jsonl":
            continue
        events = _events(path)
        first = events[0] if events else {}
        if first.get("event_type") != "run_started" or first.get("working_dir") != wanted:
            continue
        started = first.get("run_started_at")
        at = float(started) if isinstance(started, (int, float)) and not isinstance(started, bool) else 0.0
        if newest is None or at > newest[0]:
            newest = (at, events)
    if newest is None:
        return UnitState.STANDING_BY
    return UnitState.RUN_FINISHED if newest[1][-1].get("event_type") == "run_finished" else UnitState.RUNNING


def sessions_dir() -> Path:
    return Path(os.environ.get("NOTIFIER_SESSIONS_DIR") or Path.home() / ".claude/sessions")


class RecordsRead(NamedTuple):
    """Every session record was readable."""


class RecordsPartlyUnread(NamedTuple):
    reason: str


def _claudes(processes: list[herdr.HerdrProcess]) -> tuple[dict[HerdrPlace, LiveClaude], RecordsRead | RecordsPartlyUnread]:
    """Newest live Claude per herdr pane, with the completeness of the records."""
    places = {process.pid: process.place for process in processes}
    try:
        paths = sorted(path for path in sessions_dir().iterdir() if path.suffix == ".json")
    except OSError as error:
        return {}, RecordsPartlyUnread(f"cannot list session records: {error}")
    newest: dict[HerdrPlace, tuple[int, LiveClaude]] = {}
    evidence: RecordsRead | RecordsPartlyUnread = RecordsRead()
    for path in paths:
        record = read_session(path)
        if isinstance(record, UnreadableSessionRecord):
            evidence = RecordsPartlyUnread("one or more session records could not be read")
            continue
        if not live_session(record):
            continue
        place = places.get(record["pid"])
        if place is None:
            # A live registry PID may be absent from the snapshot because its
            # working directory was deleted. Placement needs no cwd.
            placement = herdr.herdr_place_of(record["pid"])
            if isinstance(placement, herdr.NotInHerdr):
                continue
            place = placement
        if place not in newest or record["updatedAt"] > newest[place][0]:
            newest[place] = (record["updatedAt"], LiveClaude(record["name"], record["sessionId"],
                                                           record["messagingSocketPath"], record["pid"]))
    return {place: claude for place, (_, claude) in newest.items()}, evidence


def _herdr_marked(claudes: dict[HerdrPlace, LiveClaude], evidence: RecordsRead | RecordsPartlyUnread,
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
        claude: Claude = claudes.get(place, ClaudeUnknown(evidence.reason) if isinstance(evidence, RecordsPartlyUnread) else ClaudeNotRunning())
        yield slug, MarkedUnit(unit, claude, place)


def _lookup_slug(slug: str) -> str:
    # A current notifier name needs no scan of Formerly entries or reread of its doc.
    if (showrunners.NOTIFIER_STATE_DIR / f"showrunner-{slug}").is_dir():
        return slug
    return canonical_slug(slug)


def every_marked_unit() -> MarkedProductions:
    """Read all herdr units once. An unreadable host raises OSError."""
    try:
        processes = herdr.herdr_processes()
        claudes, evidence = _claudes(processes)
        marked = list(_herdr_marked(claudes, evidence, processes))
    except herdr.HerdrFailure as error:
        raise OSError(str(error)) from error
    units: dict[str, dict[str, MarkedUnit]] = {}
    faults: dict[str, str] = {}
    current_slugs: dict[str, str] = {}
    for slug, unit in marked:
        if slug not in current_slugs:
            current_slugs[slug] = _lookup_slug(slug)
        slug = current_slugs[slug]
        production = units.setdefault(slug, {})
        if unit.unit in production:
            previous = production[unit.unit]
            hosts = f"herdr sessions {previous.place.session} and {unit.place.session}"
            _ = faults.setdefault(slug, f"{hosts} both carry the mark of {unit.unit}")
        production[unit.unit] = unit
    return MarkedProductions({slug: found for slug, found in units.items() if slug not in faults}, faults)


def marked_units(slug: str) -> dict[str, MarkedUnit]:
    """The production's units. Unreadable herdr or duplicate marks raise OSError."""
    slug = _lookup_slug(slug)
    found = every_marked_unit()
    if slug in found.faults:
        raise OSError(found.faults[slug])
    return found.units.get(slug, {})


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
                found = marked_units(slug).get(name)
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
