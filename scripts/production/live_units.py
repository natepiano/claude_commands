#!/usr/bin/env python3
"""List a showrunner's configured unit sessions, excluding retired rows."""
from __future__ import annotations

import sys

import showrunners
from add_unit import Refusal, production_field, retired_sessions


def live_units(session: str) -> list[str]:
    settings = showrunners.load_settings()
    runner = next((item for item in settings["showrunners"] if item["session"] == session), None)
    if runner is None:
        raise ValueError(f"showrunner absent from config: {session}")

    retired: set[str] = set()
    for instance in showrunners.NOTIFIER_STATE_DIR.glob("showrunner-*"):
        if not instance.is_dir():
            continue
        located = showrunners.checked_doc(instance)
        if isinstance(located, showrunners.NoCheckedDoc):
            continue
        try:
            lines = located.path.read_text(encoding="utf-8").splitlines()
            if production_field(lines, "Showrunner session") == session:
                retired.update(retired_sessions(lines))
        except (OSError, UnicodeError, Refusal):
            continue
    return [unit.name for unit in runner["units"] if unit.name not in retired]


def main(arguments: list[str]) -> int:
    if len(arguments) != 1:
        print("usage: live_units.py <showrunner session>", file=sys.stderr)
        return 2
    try:
        units = live_units(arguments[0])
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1
    print("\n".join(units))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
