#!/usr/bin/env python3
"""List running herdr sessions and attach to one by name."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import herdr

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS / "message"))
from sessions import SessionRecord, UnreadableSessionRecord, live_session, read_session, sessions_dir  # noqa: E402

sys.path.insert(0, str(SCRIPTS / "production"))
from showrunners import registered_showrunners  # noqa: E402

USAGE = "usage: herdr_sessions.py list | attach <name>"


def claude_names() -> dict[str, str]:
    """The newest live Claude name in each herdr session."""
    newest: dict[str, SessionRecord] = {}
    directory = sessions_dir()
    if not directory.exists():
        return {}
    for path in sorted(directory.iterdir()):
        if path.suffix != ".json":
            continue
        record = read_session(path)
        if isinstance(record, UnreadableSessionRecord) or not live_session(record):
            continue
        place = herdr.herdr_place_of(record["pid"])
        if not isinstance(place, herdr.HerdrPlace):
            continue
        if place.session not in newest or record["updatedAt"] > newest[place.session]["updatedAt"]:
            newest[place.session] = record
    return {session: record["name"] for session, record in newest.items()}


def unit_roles() -> dict[str, str]:
    """Unit roles come from the marks inherited by processes in herdr panes."""
    roles: dict[str, str] = {}
    for process in herdr.herdr_processes():
        slug = process.marks.get("SHOWRUNNER_UNIT", "")
        unit = process.marks.get("SHOWRUNNER_UNIT_ID", "")
        if slug and unit:
            _ = roles.setdefault(process.place.session, f"unit {slug}/{unit}")
    return roles


def main(argv: list[str]) -> int:
    if argv[1:] != ["list"] and not (len(argv) == 3 and argv[1] == "attach" and argv[2]):
        print(USAGE, file=sys.stderr)
        return 2
    try:
        running = herdr.live_sessions()
        if argv[1] == "attach":
            name = argv[2]
            if not any(session["name"] == name for session in running):
                names = ", ".join(session["name"] for session in running) or "none"
                print(f"herdr_sessions: {name} is not running; running sessions: {names}", file=sys.stderr)
                return 1
            binary = herdr.herdr_binary()
            os.execvp(binary, [binary, "session", "attach", name])
        if not running:
            return 0
        names = claude_names()
        roles = unit_roles()
        showrunner_names = {runner["session"] for runner in registered_showrunners() if runner["session"]}
        for session in running:
            name = session["name"]
            claude = names.get(name, "")
            role = roles.get(name, "showrunner" if claude and claude in showrunner_names else "standalone")
            print(f"{name}\t{role}\t{claude}")
        return 0
    except (herdr.HerdrFailure, OSError) as error:
        print(f"herdr_sessions: {error}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
