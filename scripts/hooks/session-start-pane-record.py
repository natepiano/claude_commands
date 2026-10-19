#!/usr/bin/env python3
"""SessionStart hook: record the Claude launch of the herdr pane this session runs in.

The entry is what a restore needs to resume the pane on its own account: see
`scripts/shutdown/pane_record.py`. A session outside herdr gets nothing.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import TypedDict, cast

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shutdown"))
import pane_record  # noqa: E402


class HookInput(TypedDict, total=False):
    session_id: str
    cwd: str
    source: str


def main() -> None:
    environment = dict(os.environ)
    pane_id = environment.get("HERDR_PANE_ID", "")
    if environment.get("HERDR_ENV") != "1" or not pane_id:
        return
    payload = cast(HookInput, json.loads(sys.stdin.read()))
    session_id = payload.get("session_id", "")
    if not session_id:
        return
    session = environment.get("HERDR_SESSION") or "default"
    arguments = pane_record.claude_arguments(os.getpid())
    pane_record.write(session, pane_record.launch(pane_id, session_id, payload.get("cwd") or os.getcwd(),
                                                  environment, arguments))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # A hook must never break the session it serves.
        pass
    sys.exit(0)
