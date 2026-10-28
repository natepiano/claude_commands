#!/usr/bin/env python3
"""Give each Claude session its own temp root and point its Bash commands' TMPDIR at it.

Test runs, mktemp and tempfile left their folders loose in /tmp, and nothing named their maker
(user, 2026-10-27). Each session gets ROOT/<session id> at start (and again at resume, clear and
compact, should the sweep have removed it), and Claude Code reads the `export TMPDIR=...` line this
hook writes to $CLAUDE_ENV_FILE before each Bash command. Sessions run for weeks, so nothing waits
on a session end: the hourly tmp sweep (scripts/lint/tmp_sweep.py) judges each entry in the root
with its 4 h idle rule, and removes the root itself only once its session stops. The path stays
short, so a Unix socket made in TMPDIR fits the 104-byte limit on the Mac.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
from pathlib import Path
from typing import cast

ROOT = "/tmp/claude/session"
ROOT_ENV = "SESSION_TEMP_ROOT"
SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")


def temp_root(session_id: str) -> Path:
    return Path(os.environ.get(ROOT_ENV) or ROOT) / session_id


def export_line(path: Path) -> str:
    return f"export TMPDIR={shlex.quote(str(path))}\n"


def main() -> int:
    try:
        raw = cast(object, json.loads(sys.stdin.read()))
    except (ValueError, UnicodeDecodeError):
        return 0
    session_id = cast(dict[str, object], raw).get("session_id") if isinstance(raw, dict) else None
    env_file = os.environ.get("CLAUDE_ENV_FILE")
    if not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id) or not env_file:
        return 0
    path = temp_root(session_id)
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        line = export_line(path)
        env = Path(env_file)
        if line not in (env.read_text(encoding="utf-8") if env.exists() else ""):
            with env.open("a", encoding="utf-8") as handle:
                _ = handle.write(line)
    except OSError as error:
        print(f"session temp root: {error}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
