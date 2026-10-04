#!/usr/bin/env python3
"""Find a live Claude session's socket or session ID."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from typing import TypedDict, cast


class SessionRecord(TypedDict):
    pid: int
    sessionId: str
    name: str
    messagingSocketPath: str
    updatedAt: int


def read_session(path: Path) -> SessionRecord | None:
    try:
        parsed = cast(object, json.loads(path.read_text()))
        if not isinstance(parsed, dict):
            return None
        data = cast(dict[str, object], parsed)
        pid = data.get("pid")
        session_id = data.get("sessionId")
        name = data.get("name")
        socket_path = data.get("messagingSocketPath")
        updated_at = data.get("updatedAt")
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
            return None
        return SessionRecord(
            pid=pid,
            sessionId=session_id,
            name=name,
            messagingSocketPath=socket_path,
            updatedAt=updated_at,
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


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


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] not in {"socket", "id"} or not argv[2]:
        print("usage: sessions.py socket <session:id|name> | id <pid|name>", file=sys.stderr)
        return 2

    command, target = argv[1:]
    directory = Path(
        os.environ.get("NOTIFIER_SESSIONS_DIR", str(Path.home() / ".claude/sessions"))
    )
    try:
        records = (
            record for path in directory.glob("*.json")
            if (record := read_session(path)) is not None
        )
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
    except OSError:
        return 1
    if newest is None:
        return 1
    print(newest["messagingSocketPath"] if command == "socket" else newest["sessionId"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
