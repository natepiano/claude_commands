#!/usr/bin/env python3
"""Bump idle unit directors and tell their running showrunners once per stretch."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import NamedTuple, TypedDict, cast
from zoneinfo import ZoneInfo

import showrunners
import add_unit

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


class _SessionSocket(NamedTuple):
    path: str


class _NoLiveSession(Enum):
    RESULT = "no live session"


class _SessionLookupUnavailable(Enum):
    RESULT = "session lookup unavailable"


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


def claude_pid(pane_pid: int, rows: list[Process]) -> int | None:
    pane = next((row for row in rows if row.pid == pane_pid), None)
    candidates = ([pane] if pane is not None else []) + descendants(pane_pid, rows)
    return next((row.pid for row in candidates
                 if row.command == "claude" or row.command.startswith("claude ")), None)


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


def unit_socket(pid: int) -> tuple[str, str] | None:
    try:
        session_id = command_output([sys.executable, str(SESSIONS), "id", str(pid)])
        record = cast(dict[str, object], json.loads((SESSIONS_DIR / f"{pid}.json").read_text(encoding="utf-8")))
        socket = record.get("messagingSocketPath")
        if session_id and isinstance(socket, str) and socket:
            return session_id, socket
    except (OSError, ValueError, TypeError):
        pass
    return None


def stretch_path(slug: str, unit: str) -> Path:
    name = hashlib.sha256(f"{slug}\0{unit}".encode()).hexdigest()
    return STATE_DIR / f"{name}.json"


def retired_units(runner: showrunners.RunningShowrunner) -> set[str]:
    """Read retired unit names from the production doc named by the check command."""
    located = showrunners.checked_doc(showrunners.NOTIFIER_STATE_DIR / f"showrunner-{runner.slug}")
    if isinstance(located, showrunners.NoCheckedDoc):
        return set()
    try:
        lines = located.path.read_text(encoding="utf-8").splitlines()
        return add_unit.retired_units(lines) | add_unit.retired_sessions(lines)
    except (OSError, UnicodeError, add_unit.Refusal):
        return set()


def rename_state(old: str, new: str, runner_before: str, runner_after: str,
                 units: list[str]) -> None:
    """Move saved stretches while the registry rename is locked."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    for unit in units:
        previous_unit = old if unit == new else unit
        source = stretch_path(runner_before, previous_unit)
        destination = stretch_path(runner_after, unit)
        if source != destination and source.exists():
            os.replace(source, destination)


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


def socket_for_target(target: str) -> _SessionSocket | _NoLiveSession | _SessionLookupUnavailable:
    try:
        result = subprocess.run(
            [sys.executable, str(SESSIONS), "socket", target],
            capture_output=True, text=True, check=False,
        )
    except OSError:
        return _SessionLookupUnavailable.RESULT
    socket = result.stdout.strip()
    if result.returncode == 0:
        return _SessionSocket(socket) if socket else _NoLiveSession.RESULT
    if result.returncode == 1:
        return _NoLiveSession.RESULT
    return _SessionLookupUnavailable.RESULT


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
    running = showrunners.running_showrunners()
    sockets = {configured["session"]: socket_for_target(configured["session"])
               for configured in settings["showrunners"]}
    configured_names = set(sockets)
    lookup_unavailable = any(isinstance(result, _SessionLookupUnavailable)
                             for result in sockets.values())
    configured_sockets = {result.path for result in sockets.values()
                          if isinstance(result, _SessionSocket)}
    missing = ([] if lookup_unavailable else
               [runner for runner in running
                if runner.session not in configured_names and runner.socket not in configured_sockets])
    runners_by_socket = {runner.socket: runner for runner in running}
    if not lookup_unavailable:
        missing_slugs = {runner.slug for runner in missing}
        for path in STATE_DIR.glob("missing-*.json"):
            if path.stem.removeprefix("missing-") not in missing_slugs:
                path.unlink()
    faults_lookup = socket_for_target(settings["faults_to"])
    faults_socket = faults_lookup.path if isinstance(faults_lookup, _SessionSocket) else ""
    for runner in missing:
        path = STATE_DIR / f"missing-{runner.slug}.json"
        stretch = read_stretch(path, runner.socket, now)
        save_stretch(path, stretch)
        if not stretch["tell_sent"] and faults_socket:
            if isinstance(runner.prompt, showrunners.PromptUnits):
                args = shlex.join(["add", runner.session, "--zone", runner.prompt.zone,
                                   *(arg for unit in runner.prompt.unit_sessions for arg in ("--unit", unit))])
                prompt_note = ""
            else:
                args = f"add {shlex.quote(runner.session)} --zone <zone> --unit <tmux session>"
                prompt_note = f" The prompt gave no zone or units because {runner.prompt.reason}."
            add = "$HOME/.claude/scripts/lib/py $HOME/.claude/scripts/production/showrunners.py " + args
            message = (f"stall-watch: showrunner {runner.slug} (session {runner.session}) is running but "
                       "missing from config/showrunners.json, so its units are not watched. "
                       f"It should run: {add}.{prompt_note}")
            pending.append(delivery(path, "missing", faults_socket,
                                    f"stall-watch:missing:{runner.slug}:{int(stretch['since'])}", message))
    for configured in settings["showrunners"]:
        showrunner_lookup = sockets[configured["session"]]
        if not isinstance(showrunner_lookup, _SessionSocket):
            continue
        showrunner_socket = showrunner_lookup.path
        runner = runners_by_socket.get(showrunner_socket)
        retired: set[str] = retired_units(runner) if runner is not None else set()
        try:
            zone = ZoneInfo(configured["zone"])
        except (KeyError, ValueError):
            print(f"stall-watch: invalid zone for {configured['session']}: {configured['zone']}", file=sys.stderr)
            continue
        for unit in configured["units"]:
            if (isinstance(unit, (showrunners.RunFinishedUnitDirector,
                                  showrunners.StandingByUnitDirector))
                    or unit.session in retired):
                stretch_path(configured["session"], unit.session).unlink(missing_ok=True)
                continue
            name = unit.session
            if subprocess.run([TMUX, "has-session", "-t", f"={name}"], capture_output=True, check=False).returncode != 0:
                continue
            try:
                pane_pid = int(command_output([TMUX, "display-message", "-p", "-t", f"={name}:", "#{pane_pid}"]))
                pane = command_output([TMUX, "capture-pane", "-p", "-J", "-S", "-400", "-t", f"={name}:"])
            except (OSError, ValueError) as error:
                print(f"stall-watch: {name}: {error}", file=sys.stderr)
                continue
            pid = claude_pid(pane_pid, rows)
            if pid is None:
                continue
            identity = unit_socket(pid)
            if identity is None:
                continue
            session_id, socket = identity
            path = stretch_path(configured["session"], name)
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
    if len(sys.argv) >= 6 and sys.argv[1] == "rename-state":
        rename_state(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5], sys.argv[6:])
        raise SystemExit(0)
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError) as error:
        print(f"stall-watch: {error}", file=sys.stderr)
        raise SystemExit(1) from error
