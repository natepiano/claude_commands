#!/usr/bin/env python3
"""Explain a missing nix shell and ask the detached healer to restart when idle.

The healthy path reads only registry and process metadata; it never calls direnv.
Registration in settings.json belongs to the unit director after the user's go.
"""
from __future__ import annotations

import json
from pathlib import Path
import signal
import subprocess
import sys
import time
from types import FrameType
from typing import TypedDict, cast

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS.parent))
from scripts.nix_env import heal


class SessionStartPayload(TypedDict):
    session_id: str


class SessionStartContext(TypedDict):
    hookEventName: str
    additionalContext: str


class SessionStartOutput(TypedDict):
    hookSpecificOutput: SessionStartContext


class MetadataDeadline(Exception):
    """Leave slow metadata reads for the periodic sweep."""


def metadata_deadline(signum: int, frame: FrameType | None) -> None:
    del signum, frame
    raise MetadataDeadline()


def main() -> int:
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, 0)
    started = time.monotonic()
    _ = signal.signal(signal.SIGALRM, metadata_deadline)
    _ = signal.setitimer(signal.ITIMER_REAL, 0.14)
    try:
        raw = cast(object, json.load(sys.stdin))
        if not isinstance(raw, dict):
            return 0
        session_id = cast(dict[str, object], raw).get("session_id")
        if not isinstance(session_id, str) or not session_id:
            return 0
        session = heal.find_session(session_id)
        if isinstance(session, heal.Gone) or not heal.broken(session):
            return 0
        payload = SessionStartPayload(session_id=session_id)
        context = SessionStartOutput(hookSpecificOutput=SessionStartContext(hookEventName="SessionStart",
            additionalContext="The nix shell for this worktree did not load. Builds in this session will fail. "
                + "Do not start builds. This session will restart when it is idle and no build hold is active. "
                + "Your worktree changes will stay."))
        print(json.dumps(context))
        _ = subprocess.Popen([str(SCRIPTS / "lib/py"), str(SCRIPTS / "nix_env/heal.py"), "sweep", "--only",
                              payload["session_id"], "--wait-idle", "1800"], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                             close_fds=True)
    except (OSError, ValueError, TypeError, subprocess.SubprocessError, MetadataDeadline):
        # Missing or half-written metadata is retried by the timer.
        return 0
    finally:
        _ = signal.setitimer(signal.ITIMER_REAL, 0)
        _ = signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0]:
            _ = signal.setitimer(signal.ITIMER_REAL, max(0.000001, previous_timer[0] - (time.monotonic() - started)),
                             previous_timer[1])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
