#!/usr/bin/env python3
"""Report whether natedev's hold refuses the calling session's build."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "build_targets"))
import build_targets
import build_hold
from targets import Held, StateUnreadable, target_state


class HoldStateReadError(ValueError):
    """The hold decision could not be read; callers may run their command."""


def hold_applies() -> str:
    """Return the refusal while a hold is in force; allow its release.

    Raise HoldStateReadError when the hold state cannot be read.
    """
    try:
        state = target_state(build_targets.local_target())
        if isinstance(state, StateUnreadable):
            raise HoldStateReadError(state.reason)
        if not isinstance(state, Held):
            return ""
        # The ownership helper treats a damaged cycle as not owned. Read it here
        # as well so a read failure lets the caller run its command.
        holds = build_hold.natedev_holds(build_hold.holder_directory())
        if not holds.in_force or build_targets.caller_holds_only_own():
            return ""
        names = "; ".join(build_hold.holder_text(holder) for holder in holds.in_force)
        return (f"natedev builds are held by {names}. Keep working, writes included. "
                "Run tests with verify.sh test: they go to the Mac. "
                "Run check, lint and builds after the hold ends.")
    except (OSError, UnicodeError, ValueError) as error:
        raise HoldStateReadError(str(error)) from error


def main() -> int:
    try:
        reason = hold_applies()
    except HoldStateReadError as error:
        print(f"natedev hold state cannot be read: {error}", file=sys.stderr)
        return 2
    if not reason:
        return 1
    print(reason)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
