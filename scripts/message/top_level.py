#!/usr/bin/env python3
"""Print each other top-level Claude session's name and messaging address.

Top level is every live session except this session and the unit directors a showrunner
launched. A unit director runs in a tmux session carrying UNIT_MARK (set by
/showrunner:produce at launch); its showrunner passes messages on to it.

Sessions are the live entries of ~/.claude/sessions/<pid>.json: the pid exists
and its start time still matches procStart, so a reused pid is not counted.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict, cast

SESSIONS_DIR = Path.home() / ".claude" / "sessions"
UNIT_MARK = "SHOWRUNNER_UNIT"
USAGE = "usage: top_level.py"


class Session(TypedDict, total=False):
    pid: int
    procStart: str
    name: str
    tmux: str
    sessionId: str
    messagingSocketPath: str


@dataclass(frozen=True, slots=True)
class AddressableSession:
    name: str
    session_id: str
    address: str


@dataclass(frozen=True, slots=True)
class UnaddressableSession:
    name: str
    session_id: str


ForwardedSession = AddressableSession | UnaddressableSession


def proc_start(pid: int) -> str | None:
    """Field 22 of /proc/<pid>/stat, the start time in clock ticks; None when the pid is gone."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    # The command name (field 2) may hold spaces; fields after it start past the last ")".
    return stat.rpartition(")")[2].split()[19]


def is_unit(tmux_target: str) -> bool:
    session = tmux_target.partition(":")[0]
    result = subprocess.run(["tmux", "show-environment", "-t", session, UNIT_MARK], capture_output=True, text=True)
    return result.returncode == 0 and result.stdout.startswith(f"{UNIT_MARK}=")


def forwarded_sessions(sessions_dir: Path = SESSIONS_DIR, unit: Callable[[str], bool] = is_unit) -> list[ForwardedSession]:
    sessions: list[ForwardedSession] = []
    own_id = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    for path in sessions_dir.glob("*.json"):
        try:
            session = cast(Session, json.loads(path.read_text()))
        except (OSError, json.JSONDecodeError):
            continue
        name, pid = session.get("name", ""), session.get("pid")
        session_id = session.get("sessionId", "")
        if not name or not session_id or session_id == own_id or pid is None or proc_start(pid) != session.get("procStart"):
            continue
        tmux = session.get("tmux")
        if tmux and unit(tmux):
            continue
        socket_path = session.get("messagingSocketPath")
        if isinstance(socket_path, str) and socket_path:
            sessions.append(AddressableSession(name, session_id, f"uds:{socket_path}"))
        else:
            sessions.append(UnaddressableSession(name, session_id))
    return sorted(
        sessions,
        key=lambda session: (
            session.name,
            session.address if isinstance(session, AddressableSession) else "",
            session.session_id,
        ),
    )


def main(argv: list[str]) -> int:
    if argv:
        print(USAGE, file=sys.stderr)
        return 2
    if not os.environ.get("CLAUDE_CODE_SESSION_ID"):
        print("top_level: CLAUDE_CODE_SESSION_ID is unset, so this session cannot be left out", file=sys.stderr)
        return 2
    sessions_dir = Path(os.environ.get("NOTIFIER_SESSIONS_DIR", str(Path.home() / ".claude" / "sessions")))
    sessions = forwarded_sessions(sessions_dir)
    recipients = [session for session in sessions if isinstance(session, AddressableSession)]
    for session in sessions:
        if isinstance(session, UnaddressableSession):
            print(f"not reachable: {session.name} [{session.session_id}] has no messaging socket", file=sys.stderr)
    hold_script = Path(__file__).resolve().parent.parent / "build_hold" / "build_hold.py"
    for session in recipients:
        result = subprocess.run([sys.executable, str(hold_script), "record-recipient", "--session-id", session.session_id, "--name", session.name], capture_output=True)
        if result.returncode != 0:
            print(f"could not record recipient {session.name} [{session.session_id}]", file=sys.stderr)
            return 1
    for session in recipients:
        print(f"{session.name}\t{session.address}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
