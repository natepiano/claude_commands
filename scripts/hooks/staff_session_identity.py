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
from scripts.agents import pane_record
from scripts.herdr import pane_launch


@dataclass(frozen=True)
class ClaudeIdentityHandoff:
    """One exact live Claude process proves both ends of a clear."""

    pid: int
    process_start: str
    old_id: staff.StaffId
    new_id: staff.StaffId


@dataclass(frozen=True)
class ClaudeLaunchObservation:
    """An ancestor process has this retained launch observation."""

    pid: int
    entry: pane_launch.PaneLaunch


class MissingClaudeLaunch(Enum):
    MISSING = "no ancestor has a retained launch observation"


@dataclass(frozen=True)
class StaleClaudeLaunch:
    """An ancestor pid has a launch observation from another generation."""

    entry: pane_launch.PaneLaunch


def _launch_observation(host: str) -> ClaudeLaunchObservation | MissingClaudeLaunch | StaleClaudeLaunch:
    entries = pane_record.read_strict(host).values()
    pid = os.getppid()
    stale: StaleClaudeLaunch | MissingClaudeLaunch = MissingClaudeLaunch.MISSING
    for depth in range(pane_record.ANCESTOR_LIMIT):
        del depth
        matches = [entry for entry in entries if entry.get("pid") == pid]
        if matches:
            started = sessions.process_start(pid)
            matching_generation = [entry for entry in matches if entry.get("proc_start") == started]
            if started and matching_generation:
                entry = max(matching_generation, key=lambda entry: entry["written_at"])
                return ClaudeLaunchObservation(pid, entry)
            if isinstance(stale, MissingClaudeLaunch):
                stale = StaleClaudeLaunch(max(matches, key=lambda entry: entry["written_at"]))
        if pid <= 1:
            break
        pid = pane_record.process_parent(pid)
    return stale


def prove(payload: Mapping[str, object], environment: Mapping[str, str]) -> ClaudeIdentityHandoff:
    pane = environment.get("HERDR_PANE_ID", "")
    host = environment.get("HERDR_SESSION", "")
    if environment.get("HERDR_ENV") != "1" or not pane or not host:
        raise staff.StaffRefused("clear: exact herdr pane evidence is missing")
    observed = _launch_observation(host)
    if isinstance(observed, MissingClaudeLaunch):
        raise staff.StaffRefused("clear: old launch observation is missing")
    if isinstance(observed, StaleClaudeLaunch):
        raise staff.StaffRefused("clear: Claude process start time differs from the launch observation")
    pid, entry = observed.pid, observed.entry
    recorded_start = entry.get("proc_start")
    started = sessions.process_start(pid)
    if not started or started != recorded_start:
        raise staff.StaffRefused("clear: Claude process start time differs from the launch observation")
    raw = payload.get("session_id")
    if not isinstance(raw, str):
        raise staff.StaffRefused("clear: hook session_id is missing")
    record = staff.initial_snapshot()
    reference = pane_launch.launch_reference(entry, record)
    old = (record.rows_by_id[reference.id].current_session if isinstance(reference, pane_launch.StaffLaunchReference)
           else staff.native_id(reference.session))
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
    observation = _launch_observation(environment.get("HERDR_SESSION", ""))
    previous = None
    if isinstance(observation, (ClaudeLaunchObservation, StaleClaudeLaunch)):
        reference = pane_launch.launch_reference(observation.entry, record)
        if isinstance(reference, pane_launch.StaffLaunchReference):
            previous = record.by_id(reference.id)
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
