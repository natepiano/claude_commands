#!/usr/bin/env python3
"""SessionEnd hook: mark this herdr pane's launch entry ended, so a restore leaves it closed.

Only an end by a signal (reason `other`, as at a reboot or a crash) keeps the entry for a restore.
See `scripts/shutdown/pane_record.py`.
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
    reason: str


def main() -> None:
    pane_id = os.environ.get("HERDR_PANE_ID", "")
    if os.environ.get("HERDR_ENV") != "1" or not pane_id:
        return
    payload = cast(HookInput, json.loads(sys.stdin.read()))
    session_id = payload.get("session_id", "")
    if not session_id or payload.get("reason", "other") == "other":
        return
    pane_record.end(os.environ.get("HERDR_SESSION") or "default", pane_id, session_id)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # A hook must never break the session it serves.
        pass
    sys.exit(0)
