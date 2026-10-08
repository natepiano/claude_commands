#!/usr/bin/env python3
"""Find a live Claude session's socket or session ID."""

from __future__ import annotations

import json
import os
import stat
import sys
from enum import Enum
from pathlib import Path
from typing import TypedDict, cast


class SessionRecord(TypedDict):
    pid: int
    sessionId: str
    name: str
    messagingSocketPath: str
    updatedAt: int
    # "<tmux session name at start>:@<window>.%<pane>"; empty outside tmux. Only the pane id stays true.
    tmux: str
    # Every name the session had before the one it has now.
    formerNames: list[str]


class UnreadableSessionRecord(Enum):
    FOUND = "unreadable session record"


RegistryEntry = SessionRecord | UnreadableSessionRecord


def read_session(path: Path) -> RegistryEntry:
    try:
        parsed = cast(object, json.loads(path.read_text()))
        if not isinstance(parsed, dict):
            return UnreadableSessionRecord.FOUND
        data = cast(dict[str, object], parsed)
        pid = data.get("pid")
        session_id = data.get("sessionId")
        name = data.get("name")
        socket_path = data.get("messagingSocketPath")
        updated_at = data.get("updatedAt")
        tmux = data.get("tmux")
        former = data.get("formerNames")
        if (
            not isinstance(pid, int)
            or isinstance(pid, bool)
            or pid <= 0
            or not isinstance(session_id, str)
            or not isinstance(name, str)
            or not isinstance(socket_path, str)
            or not isinstance(updated_at, int)
            or isinstance(updated_at, bool)
        ):
            return UnreadableSessionRecord.FOUND
        return SessionRecord(
            pid=pid,
            sessionId=session_id,
            name=name,
            messagingSocketPath=socket_path,
            updatedAt=updated_at,
            tmux=tmux if isinstance(tmux, str) else "",
            formerNames=[name for name in cast(list[object], former) if isinstance(name, str)]
            if isinstance(former, list) else [],
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return UnreadableSessionRecord.FOUND


def live_session(record: SessionRecord) -> bool:
    try:
        os.kill(record["pid"], 0)
    except PermissionError:
        pass
    except (ProcessLookupError, OverflowError):
        return False
    try:
        return stat.S_ISSOCK(Path(record["messagingSocketPath"]).stat().st_mode)
    except (OSError, ValueError, TypeError):
        return False


def sessions_dir() -> Path:
    return Path(os.environ.get("NOTIFIER_SESSIONS_DIR", str(Path.home() / ".claude/sessions")))


def live_sessions() -> list[SessionRecord]:
    """Every live session, its newest record first. A record that cannot be read is left out."""
    try:
        paths = [path for path in sessions_dir().iterdir() if path.suffix == ".json"]
    except OSError:
        return []
    reads = (read_session(path) for path in paths)
    live = [read for read in reads if not isinstance(read, UnreadableSessionRecord) and live_session(read)]
    return sorted(live, key=lambda record: record["updatedAt"], reverse=True)


def addressed(to: str, records: list[SessionRecord]) -> SessionRecord | None:
    """The live session that `to` means: by `uds:` socket, by `session:` id, by its name now, or by a
    name that it alone once had and no session has now."""
    if to.startswith("uds:"):
        return next((record for record in records if record["messagingSocketPath"] == to[4:]), None)
    if to.startswith("session:"):
        return next((record for record in records if record["sessionId"] == to[8:]), None)
    now = next((record for record in records if record["name"] == to), None)
    if now is not None:
        return now
    once = [record for record in records if to in record["formerNames"]]
    return once[0] if len(once) == 1 else None


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] not in {"socket", "id"} or not argv[2]:
        print("usage: sessions.py socket <session:id|name> | id <pid|name>", file=sys.stderr)
        return 2

    command, target = argv[1:]
    directory = sessions_dir()
    try:
        paths = sorted(path for path in directory.iterdir() if path.suffix == ".json")
    except OSError as error:
        detail = str(error).splitlines()
        print(
            f"sessions: cannot list registry: {detail[0] if detail else type(error).__name__}",
            file=sys.stderr,
        )
        return 3
    reads = [read_session(path) for path in paths]
    unreadable = any(read is UnreadableSessionRecord.FOUND for read in reads)
    records = (read for read in reads if not isinstance(read, UnreadableSessionRecord))
    if command == "socket" and target.startswith("session:"):
        matches = (record for record in records if record["sessionId"] == target[8:])
    elif command == "socket":
        matches = (record for record in records if record["name"] == target)
    elif target.isascii() and target.isdigit():
        matches = (record for record in records if record["pid"] == int(target))
    else:
        matches = (record for record in records if record["name"] == target)
    newest = max(
        (record for record in matches if live_session(record)),
        key=lambda record: record["updatedAt"], default=None,
    )
    if newest is None:
        if unreadable:
            print("sessions: one or more registry files could not be read", file=sys.stderr)
            return 3
        return 1
    print(newest["messagingSocketPath"] if command == "socket" else newest["sessionId"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
