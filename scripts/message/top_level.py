#!/usr/bin/env python3
"""Print the top-level Claude sessions on this machine, one name per line.

  top_level.py --self NAME

Top level is every live session except NAME and the unit directors a showrunner
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
from pathlib import Path
from typing import TypedDict, cast

SESSIONS_DIR = Path.home() / ".claude" / "sessions"
UNIT_MARK = "SHOWRUNNER_UNIT"
USAGE = "usage: top_level.py --self NAME"


class Session(TypedDict, total=False):
    pid: int
    procStart: str
    name: str
    tmux: str
    sessionId: str


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


def forwarded_sessions(me: str, sessions_dir: Path = SESSIONS_DIR, unit: Callable[[str], bool] = is_unit) -> list[tuple[str, str]]:
    sessions: list[tuple[str, str]] = []
    own_id = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    for path in sessions_dir.glob("*.json"):
        try:
            session = cast(Session, json.loads(path.read_text()))
        except (OSError, json.JSONDecodeError):
            continue
        name, pid = session.get("name", ""), session.get("pid")
        session_id = session.get("sessionId", "")
        if not name or not session_id or (session_id == own_id if own_id else name == me) or pid is None or proc_start(pid) != session.get("procStart"):
            continue
        tmux = session.get("tmux")
        if tmux and unit(tmux):
            continue
        sessions.append((name, session_id))
    return sorted(sessions)


def top_level(me: str, sessions_dir: Path = SESSIONS_DIR, unit: Callable[[str], bool] = is_unit) -> list[str]:
    return sorted({name for name, _ in forwarded_sessions(me, sessions_dir, unit)})


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] != "--self" or not argv[1]:
        print(USAGE, file=sys.stderr)
        return 2
    sessions_dir = Path(os.environ.get("NOTIFIER_SESSIONS_DIR", str(Path.home() / ".claude" / "sessions")))
    recipients = forwarded_sessions(argv[1], sessions_dir)
    hold_script = Path(__file__).resolve().parent.parent / "build_hold" / "build_hold.py"
    for name, session_id in recipients:
        result = subprocess.run([sys.executable, str(hold_script), "record-recipient", "--session-id", session_id, "--name", name], capture_output=True)
        if result.returncode != 0:
            print(f"could not record recipient {name} [{session_id}]", file=sys.stderr)
            return 1
    for name in sorted({name for name, _ in recipients}):
        print(name)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
