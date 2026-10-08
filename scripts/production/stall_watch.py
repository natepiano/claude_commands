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
from datetime import datetime
from pathlib import Path
from typing import NamedTuple, TypedDict, cast
from zoneinfo import ZoneInfo

import add_unit
import showrunners
import unit_lookup

STATE_DIR = Path(os.environ.get("STALL_WATCH_STATE_DIR") or Path.home() / ".local/state/stall-watch")
SESSIONS_DIR = Path(os.environ.get("NOTIFIER_SESSIONS_DIR") or Path.home() / ".claude/sessions")
PROJECTS_DIR = Path(os.environ.get("STALL_WATCH_PROJECTS_DIR") or Path.home() / ".claude/projects")
SESSIONS = Path(os.environ.get("STALL_WATCH_SESSIONS") or Path(__file__).resolve().parent.parent / "message/sessions.py")
SEND = Path(os.environ.get("STALL_WATCH_SEND") or Path(__file__).resolve().parent.parent / "message/send.py")
TMUX = os.environ.get("STALL_WATCH_TMUX") or "tmux"
PS = os.environ.get("STALL_WATCH_PS") or "ps"
WAITING_KINDS = ("done", "blocked", "gate", "decision")
HOLDING_KIND = "holding"
TURN_END = re.compile(rf"^\s*(?:— )?(?P<kind>{'|'.join((*WAITING_KINDS, HOLDING_KIND))}):.*$", re.MULTILINE)
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


def work_running(pid: int, rows: list[Process]) -> bool:
    for row in descendants(pid, rows):
        if row.command.endswith(" <defunct>"):
            continue
        word = row.command.split(" ", 1)[0]
        if Path(word).name in WORK:
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
        # The units are the doc's live rows; each is found by the mark on its tmux session.
        try:
            slug = showrunners.production_slug(configured["doc"])
            lines = Path(configured["doc"]).read_text(encoding="utf-8").splitlines()
            live = {add_unit.cell_value(cells.get("Unit", "")) for cells in add_unit.live_unit_table(lines, slug)}
            marked = unit_lookup.marked_units(slug)
        except (OSError, UnicodeError, ValueError, add_unit.Refusal) as error:
            print(f"stall-watch: {configured['slug']}: its units are not watched: {error}", file=sys.stderr)
            continue
        for name, unit in marked.items():
            if unit.state is not unit_lookup.UnitState.RUNNING or name not in live:
                stretch_path(slug, name).unlink(missing_ok=True)
                continue
            claude = unit.claude
            if not isinstance(claude, unit_lookup.LiveClaude):
                if isinstance(claude, unit_lookup.ClaudeUnknown):
                    print(f"stall-watch: {name}: {claude.reason}", file=sys.stderr)
                continue
            try:
                pane = command_output([TMUX, "capture-pane", "-p", "-J", "-S", "-400", "-t", unit.pane])
            except OSError as error:
                print(f"stall-watch: {name}: {error}", file=sys.stderr)
                continue
            pid, session_id, socket = claude.pid, claude.session_id, claude.socket
            path = stretch_path(slug, name)
            stretch = read_stretch(path, hashlib.sha256(pane.encode()).hexdigest(), now)
            turns = list(TURN_END.finditer(pane))
            last = turns[-1].group(0).strip() if turns else "none on screen"
            if turns and turns[-1].group("kind") in WAITING_KINDS:
                stretch = Stretch(pane_hash=hashlib.sha256(pane.encode()).hexdigest(), since=now,
                                  bump_sent=False, tell_sent=False, reported_status="")
                save_stretch(path, stretch)
                continue
            running_work = work_running(pid, rows)
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
