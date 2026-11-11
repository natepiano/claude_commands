#!/usr/bin/env python3
"""Print the newest run id, machine and UTC start time for a production unit."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import json
import shlex
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, cast

from scripts.lib import unit_runs
from scripts.usage import collect
from scripts.production import staff, staff_sources, unit_lookup


@dataclass(frozen=True)
class RunFound:
    run_id: str
    started_at: datetime


@dataclass(frozen=True)
class NoMatchingRun:
    pass


def matching_start(run_id: str, event: object, worktree: str, *,
                   record: staff.StaffSnapshot | None = None) -> RunFound | NoMatchingRun:
    if not isinstance(event, dict):
        return NoMatchingRun()
    fields = cast(dict[str, object], event)
    agent = fields.get("main_agent")
    identity = cast(dict[str, object], agent).get("session_id") if isinstance(agent, dict) else None
    record = staff.snapshot() if record is None else record
    row = record.by_member_reference(identity) if isinstance(identity, str) else None
    started = fields.get("run_started_at")
    if (fields.get("event_type") != "run_started" or row is None
            or isinstance(row.worktree, staff.WorktreeUnfilled) or row.worktree.path != Path(worktree).expanduser().resolve()
            or not isinstance(started, (int, float)) or isinstance(started, bool)):
        return NoMatchingRun()
    try:
        at = datetime.fromtimestamp(started, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return NoMatchingRun()
    return RunFound(run_id, at)


def local_run(root: Path, worktree: str, *,
              record: staff.StaffSnapshot | None = None) -> RunFound | NoMatchingRun:
    record = staff.snapshot() if record is None else record
    newest: RunFound | NoMatchingRun = NoMatchingRun()
    for path in sorted(root.glob("runs/*.jsonl")):
        try:
            with path.open(encoding="utf-8") as stream:
                # A run file holds one run_started event; stop reading at it.
                for line in stream:
                    if '"run_started"' not in line:
                        continue
                    try:
                        event = cast(object, json.loads(line))
                    except ValueError:
                        continue
                    fields = cast(dict[str, object], event) if isinstance(event, dict) else {}
                    if fields.get("event_type") != "run_started":
                        continue
                    candidate = matching_start(path.stem, fields, worktree, record=record)
                    if isinstance(candidate, RunFound) and (
                            isinstance(newest, NoMatchingRun) or candidate.started_at > newest.started_at):
                        newest = candidate
                    break
        except (OSError, UnicodeError):
            continue
    return newest


def remote_command(root: str) -> str:
    script = '''import json, os, sys
from pathlib import Path
''' + unit_runs.REMOTE_HISTORY_ROOT + '''
for path in sorted(root.glob('runs/*.jsonl')):
 try:
  with path.open(encoding='utf-8') as stream:
   for line in stream:
    try: event = json.loads(line)
    except ValueError: continue
    if not isinstance(event, dict) or event.get('event_type') != 'run_started': continue
    print(json.dumps({'run_id': path.stem, 'start': event}))
    break
 except (OSError, UnicodeError): continue
'''
    return shlex.join(("python3", "-c", script, root))


def mac_run(root: str, worktree: str, *,
            record: staff.StaffSnapshot | None = None) -> RunFound | NoMatchingRun:
    record = staff.snapshot() if record is None else record
    argv = ["ssh", "-o", "BatchMode=yes", collect.MAC_HOST, remote_command(root)]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        print(f"unit_run: Mac unavailable: {error}", file=sys.stderr)
        return NoMatchingRun()
    if result.returncode:
        reason = result.stderr.strip() or f"ssh exited {result.returncode}"
        print(f"unit_run: Mac unavailable: {reason}", file=sys.stderr)
        return NoMatchingRun()
    newest: RunFound | NoMatchingRun = NoMatchingRun()
    for line in result.stdout.splitlines():
        try:
            event = cast(object, json.loads(line))
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        fields = cast(dict[str, object], event)
        run_id = fields.get("run_id")
        if not isinstance(run_id, str):
            continue
        candidate = matching_start(run_id, fields.get("start"), worktree, record=record)
        if isinstance(candidate, RunFound):
            if isinstance(newest, NoMatchingRun) or candidate.started_at > newest.started_at:
                newest = candidate
    return newest


def main(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--production", required=True)
    _ = parser.add_argument("unit")
    _ = parser.add_argument("--root", default="")
    _ = parser.add_argument("--no-mac", action="store_true")
    args = parser.parse_args(arguments)
    unit = cast(str, args.unit)
    try:
        record = staff.snapshot()
        production_name = cast(str, args.production)
        worktree = str(unit_lookup.local_worktree(staff_sources.member(
            staff_sources.UnitMember(production_name, unit), record)).path)
    except (staff.StaffUnreadable, staff.StaffRefused) as error:
        print(f"unit_run: {error}", file=sys.stderr)
        return 1
    root_argument = cast(str, args.root)
    root = Path(root_argument).expanduser() if root_argument else unit_runs.history_root()
    found = local_run(root, worktree, record=record)
    machine: Literal["natedev", "mac"] = "natedev"
    if isinstance(found, NoMatchingRun) and not cast(bool, args.no_mac):
        found = mac_run(root_argument, worktree, record=record)
        machine = "mac"
    if isinstance(found, NoMatchingRun):
        print(f"unit_run: no run found for {unit} in {worktree}", file=sys.stderr)
        return 1
    print("\t".join((found.run_id, machine, found.started_at.isoformat())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
