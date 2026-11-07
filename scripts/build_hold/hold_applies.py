#!/usr/bin/env python3
"""Report whether natedev's hold refuses the calling session's build."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "build_targets"))
import build_targets
import build_hold
from targets import Held, StateUnreadable, target_state


BUILDING_CARGO_VERBS = frozenset({
    "build", "b", "check", "c", "clippy", "test", "t", "nextest", "run", "r", "bench", "doc", "d",
    "rustc", "rustdoc", "fix", "install", "miri", "mend",
})
CARGO_OPTIONS_WITH_VALUES = frozenset({
    "-C", "--config", "-Z", "--color", "--manifest-path", "--lockfile-path", "--target-dir", "--target",
    "-j", "--jobs", "-p", "--package", "--exclude", "-F", "--features", "--profile", "--message-format",
    "--artifact-dir", "--out-dir",
})


def cargo_verb(arguments: tuple[str, ...]) -> str:
    """Match the cargo wrapper's first-verb and option-value rules."""
    skip_value = False
    for argument in arguments:
        if skip_value:
            skip_value = False
        elif argument in CARGO_OPTIONS_WITH_VALUES:
            skip_value = True
        elif not argument.startswith(("+", "-")):
            return argument
    return ""


class HoldStateReadError(ValueError):
    """The hold decision could not be read; admission must stop."""


def hold_applies() -> str:
    """Return the refusal while another session's holder file exists.

    Raise HoldStateReadError when the hold state cannot be read.
    """
    try:
        state = target_state(build_targets.local_target())
        if isinstance(state, StateUnreadable):
            raise HoldStateReadError(state.reason)
        holds = build_hold.natedev_holds(build_hold.holder_directory())
        if not holds.in_force or build_targets.caller_holds_only_own():
            return ""
        if not isinstance(state, Held):
            raise HoldStateReadError("holder file exists; target state disagrees")
        names = "; ".join(build_hold.holder_text(holder) for holder in holds.in_force)
        return (f"natedev builds are held by {names}. Keep working, writes included. "
                "Use verify.sh: eligible steps go to the Mac; other steps wait. "
                "Raw cargo and merge gates must wait for the hold to end.")
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
