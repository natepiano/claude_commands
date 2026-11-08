#!/usr/bin/env python3
"""End the staff workers selected by the caller's run records."""
from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys
from typing import cast

from scripts.agents import worker_lifecycle
from scripts.production import staff, worker_admission
from scripts.lib import unit_runs


def run_workers(run: Path, record: staff.StaffSnapshot) -> set[staff.StaffId]:
    """Select the run's staff workers from its dispatch records."""
    launches = [worker_lifecycle.read_json_object(path) for path in run.glob("impl_worker_*.json")]
    members = {staff.StaffId(str(entry.get("staff_id", "")))
               for entry in launches}
    state = worker_lifecycle.read_json_object(run / unit_runs.STATE_FILE)
    passes = state.get("pass", {})
    fields = cast(dict[str, object], passes) if isinstance(passes, dict) else {}
    entries = [fields] if "instance_id" in fields else [cast(dict[str, object], item)
        for item in fields.values() if isinstance(item, dict)]
    names = {entry.get("worker_name") or entry.get("team_slot") for entry in entries}
    if names:
        parent = worker_admission.parent(record, run=run)
        members.update(row.id for row in record.find(kind="worker", parent=parent.id) if row.name in names)
    return members


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    run_argument = parser.add_argument("--session-dir", action="append", default=[])
    args = parser.parse_args()
    session_dirs = cast(list[str], getattr(args, run_argument.dest))
    own_runs = {Path(path).resolve() for path in session_dirs if path}
    pane_runs = own_runs
    pane_code = 0
    if pane_runs:
        record = staff.local_snapshot()
        parents = {worker_admission.parent(record, run=run).id for run in pane_runs}
        caller = record.by_current_session(staff.runtime_id())
        if caller is None or caller.kind not in ("unit", "showrunner") or parents != {caller.id}:
            print("remove_workers: the selected run does not belong to the native caller; nothing removed.", file=sys.stderr)
            return 2
        selected = {identity for run in pane_runs for identity in run_workers(run, record)}
        for member in record.find(kind="worker"):
            if member.parent != staff.ReportsTo(caller.id) or member.id not in selected:
                continue
            result = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / "production/worker.py"),
                                     "end", member.name], check=False)
            if result.returncode:
                pane_code = result.returncode
        return pane_code

    print("remove_workers: no workers to remove.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
