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


def top_level(me: str, sessions_dir: Path = SESSIONS_DIR, unit: Callable[[str], bool] = is_unit) -> list[str]:
    names: set[str] = set()
    for path in sessions_dir.glob("*.json"):
        try:
            session = cast(Session, json.loads(path.read_text()))
        except (OSError, json.JSONDecodeError):
            continue
        name, pid = session.get("name", ""), session.get("pid")
        if not name or name == me or pid is None or proc_start(pid) != session.get("procStart"):
            continue
        tmux = session.get("tmux")
        if tmux and unit(tmux):
            continue
        names.add(name)
    return sorted(names)


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] != "--self" or not argv[1]:
        print(USAGE, file=sys.stderr)
        return 2
    for name in top_level(argv[1]):
        print(name)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
