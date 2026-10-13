#!/usr/bin/env python3
"""Print the newest run id, machine and UTC start time for a production unit."""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.usage import collect
from add_unit import Refusal, cell_value, unit_table


@dataclass(frozen=True)
class RunFound:
    run_id: str
    started_at: datetime


@dataclass(frozen=True)
class NoMatchingRun:
    pass


def matching_start(run_id: str, event: object, worktree: str) -> RunFound | NoMatchingRun:
    if not isinstance(event, dict):
        return NoMatchingRun()
    fields = cast(dict[str, object], event)
    working_dir = fields.get("working_dir")
    started = fields.get("run_started_at")
    if (fields.get("event_type") != "run_started" or not isinstance(working_dir, str)
            or os.path.normpath(working_dir) != os.path.normpath(worktree)
            or not isinstance(started, (int, float)) or isinstance(started, bool)):
        return NoMatchingRun()
    try:
        at = datetime.fromtimestamp(started, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return NoMatchingRun()
    return RunFound(run_id, at)


def local_run(root: Path, worktree: str) -> RunFound | NoMatchingRun:
    newest: RunFound | NoMatchingRun = NoMatchingRun()
    for path in sorted(root.glob("runs/*.jsonl")):
        try:
            with path.open(encoding="utf-8") as stream:
                for line in stream:
                    try:
                        event = cast(object, json.loads(line))
                    except ValueError:
                        continue
                    candidate = matching_start(path.stem, event, worktree)
                    if isinstance(candidate, RunFound):
                        if isinstance(newest, NoMatchingRun) or candidate.started_at > newest.started_at:
                            newest = candidate
                        break
        except (OSError, UnicodeError):
            continue
    return newest


def remote_command(root: str, worktree: str) -> str:
    script = '''import json, os, sys
from pathlib import Path
root = Path(sys.argv[1] or os.environ.get('PLAN_DELEGATE_HISTORY_DIR') or '~/.local/state/plan-delegate').expanduser()
wanted = os.path.normpath(sys.argv[2])
for path in sorted(root.glob('runs/*.jsonl')):
 try:
  with path.open(encoding='utf-8') as stream:
   for line in stream:
    try: event = json.loads(line)
    except ValueError: continue
    if not isinstance(event, dict) or event.get('event_type') != 'run_started': continue
    cwd = event.get('working_dir')
    if isinstance(cwd, str) and os.path.normpath(cwd) == wanted:
     print(json.dumps({'run_id': path.stem, 'start': event}))
 except (OSError, UnicodeError): continue
'''
    return shlex.join(("python3", "-c", script, root, worktree))


def mac_run(root: str, worktree: str) -> RunFound | NoMatchingRun:
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
            collect.MAC_HOST, remote_command(root, worktree)]
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
            record = cast(object, json.loads(line))
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        fields = cast(dict[str, object], record)
        run_id = fields.get("run_id")
        if not isinstance(run_id, str):
            continue
        candidate = matching_start(run_id, fields.get("start"), worktree)
        if isinstance(candidate, RunFound):
            if isinstance(newest, NoMatchingRun) or candidate.started_at > newest.started_at:
                newest = candidate
    return newest


def main(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--production", type=Path, required=True)
    _ = parser.add_argument("unit")
    _ = parser.add_argument("--root", default="")
    _ = parser.add_argument("--no-mac", action="store_true")
    args = parser.parse_args(arguments)
    unit = cast(str, args.unit)
    try:
        lines = cast(Path, args.production).read_text(encoding="utf-8").splitlines()
        rows = unit_table(lines)
    except (OSError, UnicodeError, Refusal) as error:
        print(f"unit_run: {error}", file=sys.stderr)
        return 1
    row = next((row for row in rows if cell_value(row.get("Unit", "")) == unit), None)
    if row is None:
        print(f"unit_run: no Units row for {unit}", file=sys.stderr)
        return 1
    worktree = cell_value(row.get("Worktree", ""))
    root_argument = cast(str, args.root)
    root = Path(root_argument or os.environ.get("PLAN_DELEGATE_HISTORY_DIR")
                or "~/.local/state/plan-delegate").expanduser()
    found = local_run(root, worktree)
    machine: Literal["natedev", "mac"] = "natedev"
    if isinstance(found, NoMatchingRun) and not cast(bool, args.no_mac):
        found = mac_run(root_argument, worktree)
        machine = "mac"
    if isinstance(found, NoMatchingRun):
        print(f"unit_run: no run found for {unit} in {worktree}", file=sys.stderr)
        return 1
    print("\t".join((found.run_id, machine, found.started_at.isoformat())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
