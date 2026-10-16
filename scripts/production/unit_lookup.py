#!/usr/bin/env python3
"""Find production units by marks on tmux sessions and herdr panes.

Both hosts carry SHOWRUNNER_UNIT and SHOWRUNNER_UNIT_ID. Current names,
places and messaging sockets are read from the hosts and live session records.
Run state comes from /unit:direct records. `every_marked_unit` reads both hosts
once for every production, rejecting duplicate marks rather than choosing one.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
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
USAGE = ("usage: unit_lookup.py list <production slug> | pane <production slug> <unit>"
         + " | mark <production slug> <unit> <tmux session>")


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


@dataclass(frozen=True)
class TmuxPlace:
    pane: str


Place = TmuxPlace | HerdrPlace


@dataclass(frozen=True)
class MarkedUnit:
    unit: str
    # The tmux session label or herdr session name.
    label: str
    claude: Claude
    place: Place


def place_text(place: Place) -> str:
    if isinstance(place, HerdrPlace):
        return f"herdr:{place.session}:{place.pane}"
    return place.pane


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


def tmux_binary() -> str:
    """The tmux to run. Read at each call, so a test can point every caller at its stand-in."""
    found = os.environ.get("UNIT_LOOKUP_TMUX") or shutil.which("tmux")
    if found:
        return found
    # `^out` alone: `nixpkgs#tmux` without it also prints the man output's path.
    result = subprocess.run(["nix", "build", "--no-link", "--print-out-paths", "nixpkgs#tmux^out"],
                            text=True, capture_output=True, check=True)
    return str(Path(result.stdout.strip()) / "bin/tmux")


def _tmux(*arguments: str) -> subprocess.CompletedProcess[str]:
    try:
        binary = tmux_binary()
    except subprocess.CalledProcessError as error:
        raise OSError(f"no tmux to run: {error}") from error
    return subprocess.run([binary, *arguments], capture_output=True, text=True, check=False)


def installed_tmux_binary() -> str:
    """Resolve tmux for observation, without building or fetching a missing binary."""
    binary = os.environ.get("UNIT_LOOKUP_TMUX") or shutil.which("tmux")
    if not binary:
        raise OSError("tmux is not installed; cannot read its sessions")
    return binary


def _observe_tmux(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([installed_tmux_binary(), *arguments], capture_output=True, text=True, check=False)


def tmux_sessions() -> dict[str, tuple[str, list[str]]]:
    """Each tmux session id to its label and its pane ids. No tmux server means no sessions."""
    listed = _observe_tmux("list-panes", "-a", "-F", "#{session_id}\t#{pane_id}\t#{session_name}")
    if listed.returncode != 0:
        detail = listed.stderr.strip()
        if "no server running" in detail or "error connecting" in detail:
            return {}
        raise OSError(f"tmux could not list panes: {detail or f'exit {listed.returncode}'}")
    found: dict[str, tuple[str, list[str]]] = {}
    for line in listed.stdout.splitlines():
        session, pane, label = (line.split("\t", 2) + ["", ""])[:3]
        found.setdefault(session, (label, []))[1].append(pane)
    return found


def _marks(session: str) -> dict[str, str]:
    """The marks in one tmux session's environment. A session that ended meanwhile has none."""
    shown = _observe_tmux("show-environment", "-t", session)
    if shown.returncode != 0:
        return {}
    pairs = (line.split("=", 1) for line in shown.stdout.splitlines() if "=" in line)
    return {name: value for name, value in pairs if name in (PRODUCTION_MARK, UNIT_MARK)}


def _claudes(native_processes: list[herdr.HerdrProcess]) -> tuple[dict[str, LiveClaude], dict[HerdrPlace, LiveClaude], str]:
    """Newest live Claude per tmux or herdr pane, and any unread records."""
    native_places = {process.pid: process.place for process in native_processes}
    try:
        paths = sorted(path for path in sessions_dir().iterdir() if path.suffix == ".json")
    except OSError as error:
        return {}, {}, f"cannot list session records: {error}"
    newest: dict[Place, tuple[int, LiveClaude]] = {}
    unread = ""
    for path in paths:
        record = read_session(path)
        if isinstance(record, UnreadableSessionRecord):
            unread = "one or more session records could not be read"
            continue
        if not live_session(record):
            continue
        if record["tmux"]:
            pane = record["tmux"].rpartition(".")[2]
            if not pane:
                continue
            place: Place = TmuxPlace(pane)
        else:
            found = native_places.get(record["pid"])
            if found is None:
                # A live registry PID may be absent from the snapshot because
                # its working directory was deleted. Placement needs no cwd.
                placement = herdr.herdr_place_of(record["pid"])
                if isinstance(placement, herdr.NotInHerdr):
                    continue
                found = placement
            place = found
        if place not in newest or record["updatedAt"] > newest[place][0]:
            newest[place] = (record["updatedAt"], LiveClaude(record["name"], record["sessionId"],
                                                           record["messagingSocketPath"], record["pid"]))
    tmux = {place.pane: claude for place, (_, claude) in newest.items() if isinstance(place, TmuxPlace)}
    native = {place: claude for place, (_, claude) in newest.items() if isinstance(place, HerdrPlace)}
    return tmux, native, unread


# Starts each session's block in the one tmux call that reads every session's marks.
MARKS_HEADER = "\x1fmarks of "


def _every_marks(sessions: list[str]) -> dict[str, dict[str, str]]:
    """Each session's marks from one tmux call, not one per session: under load each call cost
    several milliseconds. A session the call did not reach, because one before it ended and tmux
    stopped there, is asked again alone."""
    chain: list[str] = []
    for session in sessions:
        chain += [*([";"] if chain else []), "display-message", "-p", "-t", session, f"{MARKS_HEADER}{session}",
                  ";", "show-environment", "-t", session]
    found: dict[str, dict[str, str]] = {}
    current = ""
    for line in (_observe_tmux(*chain).stdout if chain else "").splitlines():
        if line.startswith(MARKS_HEADER):
            current = line.removeprefix(MARKS_HEADER)
            found[current] = {}
        elif current and "=" in line:
            name, value = line.split("=", 1)
            if name in (PRODUCTION_MARK, UNIT_MARK):
                found[current][name] = value
    return {session: found[session] if session in found else _marks(session) for session in sessions}


def _marked_sessions(claudes: dict[str, LiveClaude], unread: str) -> Iterable[tuple[str, MarkedUnit]]:
    """Each tmux session carrying both marks, with its production slug."""
    listed = tmux_sessions()
    every_marks = _every_marks(list(listed))
    for session, (label, panes) in listed.items():
        marks = every_marks[session]
        slug, unit = marks.get(PRODUCTION_MARK, ""), marks.get(UNIT_MARK, "")
        if not slug or not unit:
            continue
        live = [pane for pane in panes if pane in claudes]
        claude: Claude = (claudes[live[0]] if live else ClaudeUnknown(unread) if unread else ClaudeNotRunning())
        pane = live[0] if live else panes[0]
        yield slug, MarkedUnit(unit, label, claude, TmuxPlace(pane))


def _herdr_marked(claudes: dict[HerdrPlace, LiveClaude], unread: str,
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
        claude: Claude = claudes.get(place, ClaudeUnknown(unread) if unread else ClaudeNotRunning())
        yield slug, MarkedUnit(unit, place.session, claude, place)


def _lookup_slug(slug: str) -> str:
    # A current notifier name needs no scan of Formerly entries or reread of its doc.
    if (showrunners.NOTIFIER_STATE_DIR / f"showrunner-{slug}").is_dir():
        return slug
    return canonical_slug(slug)


def every_marked_unit(*, include_tmux: bool = True, every_host: bool = False) -> MarkedProductions:
    """Read herdr and optionally tmux. Failure of all included sources is unreadable.

    With every_host, any included source failure is unreadable. Duplicate marks leave that
    production out; callers must never pick one host.
    """
    native_processes = herdr.herdr_processes()
    tmux_claudes, herdr_claudes, unread = _claudes(native_processes)
    marked_units: list[tuple[str, MarkedUnit]] = []
    failures: list[tuple[str, str]] = []
    sources = [("herdr", _herdr_marked(herdr_claudes, unread, native_processes))]
    if include_tmux:
        sources.insert(0, ("tmux", _marked_sessions(tmux_claudes, unread)))
    for host, source in sources:
        try:
            marked_units.extend(source)
        except (OSError, herdr.HerdrFailure) as error:
            failures.append((host, str(error)))
    if failures and (every_host or len(failures) == len(sources)):
        raise OSError("; ".join(
            f"{host}: {detail}" if every_host and not detail.startswith((f"{host}:", f"{host} ")) else detail
            for host, detail in failures))
    units: dict[str, dict[str, MarkedUnit]] = {}
    faults: dict[str, str] = {}
    current_slugs: dict[str, str] = {}
    for slug, marked in marked_units:
        if slug not in current_slugs:
            current_slugs[slug] = _lookup_slug(slug)
        slug = current_slugs[slug]
        production = units.setdefault(slug, {})
        if marked.unit in production:
            previous = production[marked.unit]
            if isinstance(previous.place, TmuxPlace) and isinstance(marked.place, TmuxPlace):
                hosts = f"tmux sessions {previous.label} and {marked.label}"
            else:
                first = "tmux" if isinstance(previous.place, TmuxPlace) else "herdr"
                second = "tmux" if isinstance(marked.place, TmuxPlace) else "herdr"
                hosts = f"{first} session {previous.label} and {second} session {marked.label}"
            _ = faults.setdefault(slug, f"{hosts} both carry the mark of {marked.unit}")
        production[marked.unit] = marked
    return MarkedProductions({slug: found for slug, found in units.items() if slug not in faults}, faults)


def marked_units(slug: str, *, every_host: bool = False) -> dict[str, MarkedUnit]:
    """Every marked unit of the production, in either host, by unit id.

    Raises OSError when neither host can say which sessions exist, or two sessions carry one of
    the production's unit marks. With every_host, any host failure raises OSError.
    """
    slug = _lookup_slug(slug)
    found = every_marked_unit(every_host=every_host)
    if slug in found.faults:
        raise OSError(found.faults[slug])
    return found.units.get(slug, {})


def tmux_claude(label: str) -> Claude:
    """The live Claude in the tmux session named `label`, marked or not."""
    claudes, _, unread = _claudes([])
    panes = next((panes for name, panes in tmux_sessions().values() if name == label), None)
    if panes is None:
        return ClaudeNotRunning()
    live = [claudes[pane] for pane in panes if pane in claudes]
    return live[0] if live else ClaudeUnknown(unread) if unread else ClaudeNotRunning()


def unmark(target: str) -> None:
    """Remove both marks from the tmux session `target` names."""
    for name in (PRODUCTION_MARK, UNIT_MARK):
        _ = _tmux("set-environment", "-u", "-t", target, name)


def mark(target: str, slug: str, unit: str) -> None:
    """Mark the tmux session `target` names, or holds as a pane, as this unit of this production."""
    for name, value in ((PRODUCTION_MARK, slug), (UNIT_MARK, unit)):
        done = _tmux("set-environment", "-t", target, name, value)
        if done.returncode != 0:
            raise OSError(f"tmux could not mark {target}: {done.stderr.strip() or f'exit {done.returncode}'}")


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
                    print("\t".join((unit.unit, place_text(unit.place), unit.label, running, *told)))
            case ["pane", slug, name]:
                found = marked_units(slug).get(name)
                if found is None:
                    return 1
                print(place_text(found.place))
            case ["mark", slug, name, target]:
                mark(target, slug, name)
            case _:
                print(USAGE, file=sys.stderr)
                return 2
    except OSError as error:
        print(f"unit_lookup: {error}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
