#!/usr/bin/env python3
"""Print each other top-level Claude session's name and messaging socket.

The roster selects Claude sessions that are neither workers nor unit directors
under a showrunner. Session records provide the socket that SendMessage needs.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import roster
import sessions

USAGE = "usage: top_level.py"


@dataclass(frozen=True, slots=True)
class AddressableSession:
    name: str
    session_id: str
    address: str


@dataclass(frozen=True, slots=True)
class UnaddressableSession:
    name: str
    session_id: str


ForwardedSession = AddressableSession | UnaddressableSession


def forwarded_sessions() -> list[ForwardedSession]:
    identity = roster.sender_identity(os.environ)
    result = roster.roster(os.environ)
    for problem in result.problems:
        print(f"top_level: roster: {problem}", file=sys.stderr)
    # Select the newest running record before checking its socket, as the
    # roster does, so an older reachable process never replaces it.
    records: dict[str, sessions.SessionRecord] = {}
    for record in sessions.running_sessions():
        _ = records.setdefault(record["sessionId"], record)
    forwarded: dict[str, ForwardedSession] = {}
    for entry in result.entries:
        if not roster.is_top_level(entry) or roster.is_sender(entry, identity):
            continue
        if not isinstance(entry.session, roster.ClaudeSession):
            continue
        session_id = entry.session.session_id
        record = records.get(session_id)
        socket_path = (
            record["messagingSocketPath"]
            if record is not None and sessions.messaging_socket_exists(record) else ""
        )
        forwarded[session_id] = (
            AddressableSession(entry.name, session_id, f"uds:{socket_path}")
            if socket_path else UnaddressableSession(entry.name, session_id)
        )
    return sorted(forwarded.values(), key=lambda session: (
        session.name,
        session.address if isinstance(session, AddressableSession) else "",
        session.session_id,
    ))


def main(argv: list[str]) -> int:
    if argv:
        print(USAGE, file=sys.stderr)
        return 2
    forwarded = forwarded_sessions()
    recipients = [session for session in forwarded if isinstance(session, AddressableSession)]
    for session in forwarded:
        if isinstance(session, UnaddressableSession):
            print(f"not reachable: {session.name} [{session.session_id}] has no messaging socket", file=sys.stderr)
    hold_script = Path(__file__).resolve().parent.parent / "build_hold" / "build_hold.py"
    for session in recipients:
        result = subprocess.run([sys.executable, str(hold_script), "record-recipient", "--session-id", session.session_id, "--name", session.name], capture_output=True)
        if result.returncode != 0:
            print(f"could not record recipient {session.name} [{session.session_id}]", file=sys.stderr)
            return 1
    for session in recipients:
        print(f"{session.name}\t{session.address}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
