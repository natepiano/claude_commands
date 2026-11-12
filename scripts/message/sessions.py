#!/usr/bin/env python3
"""Find a live Claude session's socket, `SendMessage` address, session ID or transcript."""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import sys
from collections.abc import Callable
from dataclasses import dataclass
from importlib import import_module
from typing import cast

from scripts.production import staff

from scripts.message.session_records import (
    process_start as process_start,
    SessionRecord as SessionRecord,
    UnreadableSessionRecord as UnreadableSessionRecord,
    RegistryEntry as RegistryEntry,
    RegistryDetails as RegistryDetails,
    RegistryObservation as RegistryObservation,
    RegistryRead as RegistryRead,
    SessionRetryPolicy as SessionRetryPolicy,
    retry_policy as retry_policy,
    RegistryRecordGone as RegistryRecordGone,
    read_registry as read_registry,
    parse_session as parse_session,
    read_session as read_session,
    headless_process as headless_process,
    headless as headless,
    running_session as running_session,
    messaging_socket_exists as messaging_socket_exists,
    sessions_dir as sessions_dir,
    HEADLESS_ENTRYPOINT_PREFIX as HEADLESS_ENTRYPOINT_PREFIX,
    LiveSessionProcess as LiveSessionProcess,
    SessionNotLive as SessionNotLive,
    SessionLivenessUnknown as SessionLivenessUnknown,
    remote_control as remote_control,
    process_liveness as process_liveness,
    SessionProcesses as SessionProcesses,
    session_liveness as session_liveness,
    session_processes as session_processes,
    live_session as live_session,
    running_records as running_records,
    running_sessions as running_sessions,
    live_sessions as live_sessions,
    config_dirs as config_dirs,
    transcript_locations as transcript_locations,
    latest_transcript as latest_transcript,

)


@dataclass(frozen=True)
class StaffNameMissing:
    name: str


def staff_recipient(name: str, membership: staff.StaffSnapshot) -> staff.StaffRow | StaffNameMissing:
    """Use the shared staff lookup without reading registry names."""
    resolve = cast(Callable[[str, staff.StaffSnapshot], staff.StaffRow | StaffNameMissing],
                   import_module("scripts.message.send").staff_named)
    return resolve(name, membership)


def addressed(to: str, records: list[SessionRecord], *,
              membership: staff.StaffSnapshot | None = None) -> SessionRecord | None:
    """Find a native address or the current session of a named staff member."""
    if to.startswith("uds:"):
        return next((record for record in records if record["messagingSocketPath"] == to[4:]), None)
    if to.startswith("session:"):
        return next((record for record in records if record["sessionId"] == to[8:]), None)
    member = staff_recipient(to, staff.snapshot() if membership is None else membership)
    # The CLI and send.py share StaffRow when Python loads sessions.py twice.
    if isinstance(member, staff.StaffRow):
        return next((record for record in records if record["sessionId"] == member.current_session), None)
    return None


def named(name: str, records: list[SessionRecord], *,
          membership: staff.StaffSnapshot | None = None) -> SessionRecord | None:
    """Find the live session with this staff name."""
    return addressed(name, records, membership=membership)


def member_target(target: str) -> str:
    """Resolve a retained staff reference to its current conversation at delivery."""
    if not target.startswith("session:"):
        return target
    resolve = cast(Callable[[str], str], import_module("scripts.production.staff").current_session_target)
    return resolve(target)


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] not in {"socket", "member-socket", "address", "id"} or not argv[2]:
        print(
            "usage: sessions.py socket <session:id|name> | address <session:id|name> | id <pid|name>",
            file=sys.stderr,
        )
        return 2

    command, target = argv[1:]
    if command == "member-socket":
        try:
            target = member_target(target)
        except (OSError, ValueError) as error:
            print(f"sessions: staff target unavailable: {error}", file=sys.stderr)
            return 3
        command = "socket"
    registry = read_registry()
    unreadable = bool(registry.unreadable)
    records = sorted((item.record for item in registry.entries if live_session(item.record)),
                     key=lambda record: record["updatedAt"], reverse=True)
    try:
        newest = (next((record for record in records if record["pid"] == int(target)), None)
                  if command == "id" and target.isascii() and target.isdigit()
                  else addressed(target, records))
    except (OSError, ValueError) as error:
        print(f"sessions: {error}", file=sys.stderr)
        return 3
    if newest is None:
        if unreadable:
            print("sessions: one or more registry files could not be read", file=sys.stderr)
            return 3
        print(f"sessions: no live session for {target!r}", file=sys.stderr)
        return 1
    # `SendMessage` takes the address as its `to`: the session on this machine,
    # whatever other sessions the sender's account lists under the same name.
    print({
        "socket": newest["messagingSocketPath"],
        "address": f"uds:{newest['messagingSocketPath']}",
        "id": newest["sessionId"],
    }[command])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
