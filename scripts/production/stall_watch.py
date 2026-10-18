#!/usr/bin/env python3
"""Bump idle unit directors and tell their running showrunners once per stretch."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from datetime import time as clock
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, TypedDict, cast
from zoneinfo import ZoneInfo

import add_unit
import production_units
import showrunners
import unit_lookup
from session_pane import HerdrFailure, read_place

import herdr  # noqa: E402  session_pane puts scripts/herdr on the path

if TYPE_CHECKING:
    from collections.abc import Iterator

STATE_DIR = Path(os.environ.get("STALL_WATCH_STATE_DIR") or Path.home() / ".local/state/stall-watch")
SESSIONS_DIR = Path(os.environ.get("NOTIFIER_SESSIONS_DIR") or Path.home() / ".claude/sessions")
PROJECTS_DIR = Path(os.environ.get("STALL_WATCH_PROJECTS_DIR") or Path.home() / ".claude/projects")
SESSIONS = Path(os.environ.get("STALL_WATCH_SESSIONS") or Path(__file__).resolve().parent.parent / "message/sessions.py")
SEND = Path(os.environ.get("STALL_WATCH_SEND") or Path(__file__).resolve().parent.parent / "message/send.py")
PS = os.environ.get("STALL_WATCH_PS") or "ps"
WAITING_KINDS = ("done", "blocked", "gate", "decision")
HOLDING_KIND = "holding"
TURN_END = re.compile(rf"^\s*(?:— )?(?P<kind>{'|'.join((*WAITING_KINDS, HOLDING_KIND))}):.*$", re.MULTILINE)
# A time a holding line names: a date, with or without its year, is optional, and UTC or GMT marks a time
# not in the production's zone.
HOLD_TIME = re.compile(r"\b(?:(?:(?P<year>\d{4})-)?(?P<month>\d{2})-(?P<day>\d{2})[ T])?"
                       + r"(?P<hour>\d{1,2}):(?P<minute>\d{2})\b(?:\s+(?P<utc>UTC|GMT)\b)?")
WORK = {"zsh", "bash", "sh", "implement.sh", "review.sh", "verify.sh"}


class Process(NamedTuple):
    pid: int
    parent: int
    command: str


class Stretch(TypedDict):
    pane_hash: str
    since: float
    bump_sent: bool
    tell_sent: bool
    reported_status: str


class Delivery(NamedTuple):
    state_path: Path
    kind: str
    command: list[str]


class ContentBlock(TypedDict, total=False):
    type: str
    text: str


class TranscriptMessage(TypedDict, total=False):
    content: list[ContentBlock] | str


class TranscriptEntry(TypedDict, total=False):
    type: str
    message: TranscriptMessage


def command_output(command: list[str]) -> str:
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise OSError(f"{' '.join(command)} exited {result.returncode}: {result.stderr.strip()}")
    return result.stdout.strip()


def processes() -> list[Process]:
    rows: list[Process] = []
    for line in command_output([PS, "-eo", "pid=,ppid=,args="]).splitlines():
        match = re.match(r"\s*(\d+)\s+(\d+)\s+(.*)", line)
        if match:
            rows.append(Process(int(match.group(1)), int(match.group(2)), match.group(3)))
    return rows


def descendants(root: int, rows: list[Process]) -> list[Process]:
    children: dict[int, list[Process]] = {}
    for row in rows:
        children.setdefault(row.parent, []).append(row)
    found: list[Process] = []
    queue = [root]
    for parent in queue:
        for child in children.get(parent, []):
            found.append(child)
            queue.append(child.pid)
    return found


def unit_marks(pid: int) -> tuple[str, str] | None:
    """The production and unit a process's environment marks it with, inherited from its unit's session."""
    if sys.platform == "darwin":
        # The Mac has no /proc; herdr reads the environment from ps.
        try:
            values = herdr.darwin_process_environment(pid)
        except (HerdrFailure, subprocess.SubprocessError, OSError):
            return None
    else:
        try:
            environment = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
        except OSError:
            return None
        values = dict(item.decode(errors="replace").split("=", 1) for item in environment if b"=" in item)
    slug, unit = values.get(unit_lookup.PRODUCTION_MARK, ""), values.get(unit_lookup.UNIT_MARK, "")
    return (slug, unit) if slug and unit else None


def work_running(pid: int, rows: list[Process], marks: tuple[str, str] | None = None) -> bool:
    """A shell or launcher under the unit's Claude, or any process the unit started that left its tree.

    A process carrying the unit's marks counts wherever it sits, except the Claude, the pane around
    it, and the long-lived helpers the Claude starts itself (its MCP servers).
    """
    for row in descendants(pid, rows):
        if row.command.endswith(" <defunct>"):
            continue
        word = row.command.split(" ", 1)[0]
        if Path(word).name in WORK:
            return True
    if marks is None:
        return False
    parents = {row.pid: row.parent for row in rows}
    ancestors: set[int] = set()
    current = pid
    while current in parents and current not in ancestors:
        ancestors.add(current)
        current = parents[current]
    helpers = {row.pid for row in rows if row.parent == pid}
    helpers.update(child.pid for helper in tuple(helpers) for child in descendants(helper, rows))
    current_slugs: dict[str, str] = {}
    for row in rows:
        if row.pid in ancestors or row.pid in helpers or row.command.endswith(" <defunct>"):
            continue
        inherited = unit_marks(row.pid)
        if inherited is None or inherited[1] != marks[1]:
            continue
        slug = inherited[0]
        if slug not in current_slugs:
            current_slugs[slug] = showrunners.canonical_slug(slug)
        if (current_slugs[slug], inherited[1]) == marks:
            return True
    return False


def latest_transcript_activity(session_id: str) -> float:
    latest = 0.0
    for transcript in PROJECTS_DIR.glob(f"*/{session_id}.jsonl"):
        latest = max(latest, transcript.stat().st_mtime)
    for directory in PROJECTS_DIR.glob(f"*/{session_id}/subagents"):
        for path in directory.rglob("*"):
            if path.is_file():
                latest = max(latest, path.stat().st_mtime)
    return latest


def lines_from_end(path: Path, block: int = 1 << 20) -> Iterator[bytes]:
    """The file's lines, last first, read a block at a time from its end."""
    with path.open("rb") as file:
        end = file.seek(0, os.SEEK_END)
        rest = b""
        while end > 0:
            start = max(0, end - block)
            _ = file.seek(start)
            lines = (file.read(end - start) + rest).split(b"\n")
            rest = lines.pop(0) if start > 0 else b""
            yield from reversed(lines)
            end = start


def transcript_turn_end(session_id: str) -> re.Match[str] | None:
    """The turn-end line closing the session's last assistant text, read from its transcript.

    Compaction clears the pane, so a unit that ended its turn and then compacted while idle shows no
    turn-end line on screen; its transcript still ends with that text. A tool call after the text
    means the turn went on, so there is no turn-end line.
    """
    try:
        transcripts = sorted(PROJECTS_DIR.glob(f"*/{session_id}.jsonl"), key=lambda path: path.stat().st_mtime)
        if not transcripts:
            return None
        for line in lines_from_end(transcripts[-1]):
            if b'"assistant"' not in line:
                continue
            try:
                entry = cast(TranscriptEntry, json.loads(line))
            except ValueError:
                continue
            content = entry.get("message", {}).get("content") if entry.get("type") == "assistant" else None
            blocks = content if isinstance(content, list) else []
            if any(block.get("type") == "tool_use" for block in blocks):
                return None
            text = "\n".join(block.get("text", "") for block in blocks if block.get("type") == "text")
            if text:
                turns = list(TURN_END.finditer(text))
                return turns[-1] if turns else None
    except OSError:
        return None
    return None


def hold_days(match: re.Match[str], today: date) -> list[date]:
    """The days a hold's time can fall on: the one it names, or each near today when it names no year or no date."""
    if not match["day"]:
        return [today + timedelta(days=offset) for offset in (-1, 0, 1)]
    years = [int(match["year"])] if match["year"] else [today.year + offset for offset in (-1, 0, 1)]
    days: list[date] = []
    for year in years:
        try:
            days.append(date(year, int(match["month"]), int(match["day"])))
        except ValueError:
            continue
    return days


def stated_hold_end(line: str, now: float, zone: ZoneInfo) -> float | None:
    """The latest time a holding line names, so a hold that states when it ends is not bumped before then.

    A time without a date, or a date without a year, is the occurrence nearest now: "until 01:00" read at
    23:00 is tomorrow, "since 23:50" read at 00:10 is yesterday, and "10-22 11:47" is the nearest 22 October.
    """
    instants: list[float] = []
    for match in HOLD_TIME.finditer(line):
        hour, minute = int(match["hour"]), int(match["minute"])
        if hour > 23 or minute > 59:
            continue
        place = ZoneInfo("UTC") if match["utc"] else zone
        candidates = [datetime.combine(day, clock(hour, minute), place).timestamp()
                      for day in hold_days(match, datetime.fromtimestamp(now, place).date())]
        if candidates:
            instants.append(min(candidates, key=lambda instant: abs(instant - now)))
    return max(instants, default=None)


def stretch_path(slug: str, unit: str) -> Path:
    name = hashlib.sha256(f"{slug}\0{unit}".encode()).hexdigest()
    return STATE_DIR / f"{name}.json"


def read_stretch(path: Path, pane_hash: str, now: float) -> Stretch:
    try:
        old = cast(Stretch, json.loads(path.read_text(encoding="utf-8")))
        _ = old.setdefault("reported_status", "")
        if (old["pane_hash"] == pane_hash or old["bump_sent"] or old["tell_sent"]
                or old.get("reported_status")):
            return old
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return Stretch(pane_hash=pane_hash, since=now, bump_sent=False, tell_sent=False,
                   reported_status="")


def save_stretch(path: Path, stretch: Stretch) -> None:
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    _ = temporary.write_text(json.dumps(stretch) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def delivery(path: Path, kind: str, socket: str, key: str, text: str) -> Delivery:
    return Delivery(path, kind, [sys.executable, str(SEND), "--to", f"uds:{socket}", "--from", "stall-watch",
                                 "--key", key, "--text", text])


def send_all(pending: list[Delivery]) -> None:
    """Start every relay, then give the entire batch one 90-second deadline."""
    started: list[tuple[Delivery, subprocess.Popen[str]]] = []
    for item in pending:
        try:
            started.append((item, subprocess.Popen(item.command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                                   text=True)))
        except OSError as error:
            print(f"stall-watch: {item.kind}: {error}", file=sys.stderr)
    deadline = time.monotonic() + 90
    for item, process in started:
        try:
            out, err = process.communicate(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            process.kill()
            out, err = process.communicate()
            print(f"stall-watch: {item.kind}: send timeout", file=sys.stderr)
            continue
        if process.returncode != 0:
            print(f"stall-watch: {item.kind}: {err.strip() or out.strip() or process.returncode}", file=sys.stderr)
            continue
        stretch = cast(Stretch, json.loads(item.state_path.read_text(encoding="utf-8")))
        if item.kind == "bump":
            stretch["bump_sent"] = True
        else:
            stretch["tell_sent"] = True
        save_stretch(item.state_path, stretch)


def tick(now: float) -> None:
    settings = showrunners.load_settings()
    rows = processes()
    pending: list[Delivery] = []
    for configured in showrunners.registered_showrunners():
        # A showrunner that is not running has no one to tell: its units are not watched.
        showrunner_socket = configured["socket"]
        if not showrunner_socket:
            continue
        try:
            zone = ZoneInfo(configured["zone"])
        except (KeyError, ValueError):
            print(f"stall-watch: invalid zone for {configured['slug']}: {configured['zone']}", file=sys.stderr)
            continue
        # The units are the doc's live rows; each is found by the mark on its herdr pane.
        try:
            slug = showrunners.production_slug(configured["doc"])
            lines = Path(configured["doc"]).read_text(encoding="utf-8").splitlines()
            live = {production_units.cell_value(cells.get("Unit", "")): production_units.cell_value(cells.get("Worktree", ""))
                    for cells in production_units.live_unit_table(lines, slug)}
            marked = unit_lookup.marked_units(slug)
        except (OSError, UnicodeError, ValueError, production_units.Refusal) as error:
            print(f"stall-watch: {configured['slug']}: its units are not watched: {error}", file=sys.stderr)
            continue
        for name, unit in marked.items():
            # The review unit waits for a review merge by design, so idle is its normal state.
            if add_unit.is_review_unit(name, lines):
                stretch_path(slug, name).unlink(missing_ok=True)
                continue
            # A unit is watched while the newest /unit:direct run in its worktree is unfinished.
            try:
                running = name in live and unit_lookup.run_state(Path(live[name])) is unit_lookup.UnitState.RUNNING
            except (OSError, UnicodeError) as error:
                print(f"stall-watch: {name}: its run state is unknown: {error}", file=sys.stderr)
                continue
            if not running:
                stretch_path(slug, name).unlink(missing_ok=True)
                continue
            claude = unit.claude
            if not isinstance(claude, unit_lookup.LiveClaude):
                if isinstance(claude, unit_lookup.ClaudeUnknown):
                    print(f"stall-watch: {name}: {claude.reason}", file=sys.stderr)
                continue
            try:
                pane = read_place(unit.place, 400, joined=True).strip()
            except (OSError, HerdrFailure) as error:
                print(f"stall-watch: {name}: {error}", file=sys.stderr)
                continue
            pid, session_id, socket = claude.pid, claude.session_id, claude.socket
            path = stretch_path(slug, name)
            stretch = read_stretch(path, hashlib.sha256(pane.encode()).hexdigest(), now)
            turns = list(TURN_END.finditer(pane))
            turn = turns[-1] if turns else transcript_turn_end(session_id)
            last = turn.group(0).strip() if turn else "none on screen"
            # A hold that states when it ends waits like a gate until then; the idle clock starts after it.
            held_until = (stated_hold_end(turn.group(0), now, zone)
                          if turn and turn.group("kind") == HOLDING_KIND else None)
            if turn and (turn.group("kind") in WAITING_KINDS or (held_until is not None and held_until > now)):
                stretch = Stretch(pane_hash=hashlib.sha256(pane.encode()).hexdigest(), since=now,
                                  bump_sent=False, tell_sent=False, reported_status="")
                save_stretch(path, stretch)
                continue
            running_work = work_running(pid, rows, (slug, unit.unit))
            if running_work or (stretch["reported_status"] and last != stretch["reported_status"]):
                stretch = Stretch(pane_hash=hashlib.sha256(pane.encode()).hexdigest(), since=now,
                                  bump_sent=False, tell_sent=False, reported_status="")
            elif not stretch["reported_status"]:
                stretch["since"] = max(stretch["since"], min(latest_transcript_activity(session_id), now))
            save_stretch(path, stretch)
            since = stretch["since"]
            if now - since < settings["stall_minutes"] * 60 or running_work:
                continue
            since_text = datetime.fromtimestamp(since, zone).strftime("%H:%M %Z")
            key = f"stall-watch:{name}:{int(since)}"
            if not stretch["reported_status"]:
                stretch["reported_status"] = last
                save_stretch(path, stretch)
            if not stretch["bump_sent"]:
                text = (f"stall-watch: you have been idle since {since_text} with nothing running. "
                        "Continue your run; if you are waiting on someone, say on whom in one line.")
                pending.append(delivery(path, "bump", socket, f"{key}:bump", text))
            if not stretch["tell_sent"] and showrunner_socket:
                text = f"{name} idle since {since_text}, nothing running; bumped. Last status: {last}"
                pending.append(delivery(path, "tell", showrunner_socket, f"{key}:tell", text))
    send_all(pending)


def main() -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with (STATE_DIR / "lock").open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        now = float(os.environ.get("STALL_WATCH_NOW_EPOCH") or time.time())
        tick(now)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError) as error:
        print(f"stall-watch: {error}", file=sys.stderr)
        raise SystemExit(1) from error
