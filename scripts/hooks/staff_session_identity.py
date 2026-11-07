#!/usr/bin/env python3
"""Publish a proved Claude runtime handoff before ordinary launch recording."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
import json
import os
import sys
from typing import cast

from scripts.production import staff
from scripts.message import sessions
from scripts.shutdown import pane_record


@dataclass(frozen=True)
class ClaudeIdentityHandoff:
    """One exact live Claude process proves both ends of a clear."""

    pid: int
    process_start: str
    old_id: staff.StaffId
    new_id: staff.StaffId


def prove(payload: Mapping[str, object], environment: Mapping[str, str]) -> ClaudeIdentityHandoff:
    pane = environment.get("HERDR_PANE_ID", "")
    host = environment.get("HERDR_SESSION", "")
    if environment.get("HERDR_ENV") != "1" or not pane or not host:
        raise staff.StaffRefused("clear: exact herdr pane evidence is missing")
    pid = os.getppid()
    # Hook commands may run below one shell; walk only to the recorded Claude pid.
    entry = pane_record.read_strict(host).get(pane)
    if entry is None:
        raise staff.StaffRefused("clear: old launch observation is missing")
    recorded_pid = entry.get("pid")
    recorded_start = entry.get("proc_start")
    for depth in range(pane_record.ANCESTOR_LIMIT):
        del depth
        if pid == recorded_pid:
            break
        if pid <= 1:
            raise staff.StaffRefused("clear: launch observation belongs to another process")
        pid = pane_record.process_parent(pid)
    else:
        raise staff.StaffRefused("clear: Claude parent pid is not proved")
    started = sessions.process_start(pid)
    if not started or started != recorded_start:
        raise staff.StaffRefused("clear: Claude process start time differs from the launch observation")
    raw = payload.get("session_id")
    if not isinstance(raw, str):
        raise staff.StaffRefused("clear: hook session_id is missing")
    observed = staff.native_id(entry["session_id"])
    record = staff.initial_snapshot()
    owner = record.by_member_reference(observed)
    old = owner.current_session if owner is not None else observed
    current = staff.runtime_id(pid)
    if payload.get("hook_event_name") == "SessionEnd":
        if payload.get("reason") != "clear" or raw != old:
            raise staff.StaffRefused("clear: SessionEnd does not prove the old identity")
        # This observation is read only; no handoff journal or pane write.
        return ClaudeIdentityHandoff(pid, started, old, staff.native_id(current))
    if payload.get("source") != "clear" or raw != current:
        raise staff.StaffRefused("clear: SessionStart and registry identities differ")
    if sessions.process_start(pid) != started:
        raise staff.StaffRefused("clear: Claude process changed during observation")
    return ClaudeIdentityHandoff(pid, started, old, staff.native_id(raw))


class ClearIdentityResult(Enum):
    """Whether ordinary launch recording may run after identity handling."""

    NOT_CLEAR = "not clear"
    UNSTAFFED = "no membership to update"
    STAFFED = "staff owns the current conversation"


def handle(payload: Mapping[str, object], environment: Mapping[str, str]) -> ClearIdentityResult:
    if payload.get("source") != "clear" and payload.get("reason") != "clear":
        return ClearIdentityResult.NOT_CLEAR
    if environment.get("HERDR_ENV") != "1":
        return ClearIdentityResult.UNSTAFFED
    record = staff.initial_snapshot()
    raw = payload.get("session_id")
    row = record.by_current_session(raw) if isinstance(raw, str) else None
    if payload.get("hook_event_name") == "SessionEnd" and row is None:
        return ClearIdentityResult.UNSTAFFED
    if not record.rows:
        return ClearIdentityResult.UNSTAFFED
    entry = pane_record.read_strict(environment.get("HERDR_SESSION", "")).get(
        environment.get("HERDR_PANE_ID", ""))
    previous = record.by_member_reference(entry["session_id"]) if entry is not None else None
    if row is None and previous is None:
        return ClearIdentityResult.UNSTAFFED
    proof = prove(payload, environment)
    if payload.get("hook_event_name") != "SessionEnd":
        try:
            _ = staff.update_current_session(proof.old_id, proof.new_id)
        except staff.StaffNotFound:
            current = staff.initial_snapshot()
            if current.by_current_session(proof.old_id) is None and current.by_current_session(proof.new_id) is None:
                return ClearIdentityResult.UNSTAFFED
            raise
    return ClearIdentityResult.STAFFED


def main() -> int:
    try:
        raw = cast(object, json.loads(sys.stdin.read()))
        if not isinstance(raw, dict):
            raise staff.StaffRefused("hook input must be an object")
        _ = handle(cast(dict[str, object], raw), os.environ)
        return 0
    except (OSError, ValueError, staff.StaffRefused, staff.StaffNotFound) as error:
        print(f"staff identity: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
