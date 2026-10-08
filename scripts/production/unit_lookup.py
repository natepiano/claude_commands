#!/usr/bin/env python3
"""Find a production's units by the marks on their tmux sessions.

A unit's tmux session carries two marks in its environment: the production slug and the unit id.
Nothing else records which session is which unit. A unit's current name, pane and socket are read
here, from tmux and the live session records, each time they are needed, so a rename or a relaunch
leaves nothing to bring in step. Its run state is stored nowhere either: it is read from the
records /unit:direct keeps of its runs.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from enum import Enum
from pathlib import Path
from typing import NamedTuple, cast

MESSAGE = Path(__file__).resolve().parent.parent / "message"
sys.path.insert(0, str(MESSAGE))
from sessions import UnreadableSessionRecord, live_session, read_session  # noqa: E402

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


class MarkedUnit(NamedTuple):
    unit: str
    # The first pane of the marked tmux session, as tmux addresses it ("%41").
    pane: str
    # The tmux session's label now. It is a display name: nothing finds a pane by it.
    label: str
    claude: Claude


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


def tmux_sessions() -> dict[str, tuple[str, list[str]]]:
    """Each tmux session id to its label and its pane ids. No tmux server means no sessions."""
    listed = _tmux("list-panes", "-a", "-F", "#{session_id}\t#{pane_id}\t#{session_name}")
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
    shown = _tmux("show-environment", "-t", session)
    if shown.returncode != 0:
        return {}
    pairs = (line.split("=", 1) for line in shown.stdout.splitlines() if "=" in line)
    return {name: value for name, value in pairs if name in (PRODUCTION_MARK, UNIT_MARK)}


def _claudes() -> tuple[dict[str, LiveClaude], str]:
    """The newest live Claude in each pane, and why the records were not all read (empty if they were)."""
    try:
        paths = sorted(path for path in sessions_dir().iterdir() if path.suffix == ".json")
    except OSError as error:
        return {}, f"cannot list session records: {error}"
    newest: dict[str, tuple[int, LiveClaude]] = {}
    unread = ""
    for path in paths:
        record = read_session(path)
        if isinstance(record, UnreadableSessionRecord):
            unread = "one or more session records could not be read"
            continue
        pane = record["tmux"].rpartition(".")[2]
        if not pane or not live_session(record):
            continue
        if pane not in newest or record["updatedAt"] > newest[pane][0]:
            newest[pane] = (record["updatedAt"], LiveClaude(record["name"], record["sessionId"],
                                                            record["messagingSocketPath"], record["pid"]))
    return {pane: claude for pane, (_, claude) in newest.items()}, unread


def marked_units(slug: str) -> dict[str, MarkedUnit]:
    """Every unit of the production that has a tmux session, by unit id.

    Raises OSError when tmux cannot say which sessions exist. Two sessions carrying one unit's mark
    is a fault of whoever marked them, and is raised too: no caller may pick one.
    """
    claudes, unread = _claudes()
    units: dict[str, MarkedUnit] = {}
    for session, (label, panes) in tmux_sessions().items():
        marks = _marks(session)
        unit = marks.get(UNIT_MARK, "")
        if marks.get(PRODUCTION_MARK) != slug or not unit:
            continue
        if unit in units:
            raise OSError(f"tmux sessions {units[unit].label} and {label} both carry the mark of {unit}")
        live = [claudes[pane] for pane in panes if pane in claudes]
        claude: Claude = (live[0] if live else ClaudeUnknown(unread) if unread else ClaudeNotRunning())
        units[unit] = MarkedUnit(unit, panes[0], label, claude)
    return units


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
                    print("\t".join((unit.unit, unit.pane, unit.label, running, *told)))
            case ["pane", slug, name]:
                found = marked_units(slug).get(name)
                if found is None:
                    return 1
                print(found.pane)
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
