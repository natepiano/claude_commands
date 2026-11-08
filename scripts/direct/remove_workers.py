#!/usr/bin/env python3
"""Remove Claude workers selected from staff.

Explicit runs select their staff parent. The sweep selects workers without a live parent.
The command keeps transcripts and never removes the caller.
"""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import TypedDict, cast

from scripts.agents import worker_lifecycle
from scripts.production import session_staff, staff, worker_admission
from scripts.lib import unit_runs

CLAUDE_TIMEOUT_SECS = 30
OWN_RUN_FINISHED = "its run finished this phase"
DIRECTOR_NOT_LIVE = "unit director is not live"



class AgentRow(TypedDict, total=False):
    id: str
    sessionId: str
    kind: str
    name: str


def claude_bin() -> str:
    """The claude binary by path: a caller that sources a profile gets the
    interactive alias, which turns `claude rm <id>` into a new session."""
    configured = os.environ.get("CLAUDE_BIN", "")
    if configured:
        return configured
    local = Path.home() / ".local" / "bin" / "claude"
    if os.access(local, os.X_OK):
        return str(local)
    return shutil.which("claude") or "claude"


def list_sessions(claude: str) -> list[AgentRow] | None:
    try:
        listed = subprocess.run(
            [claude, "agents", "--json"],
            capture_output=True,
            text=True,
            timeout=CLAUDE_TIMEOUT_SECS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if listed.returncode != 0:
        return None
    try:
        parsed = cast("object", json.loads(listed.stdout))
    except ValueError:
        return None
    if not isinstance(parsed, list):
        return None
    rows = cast("list[object]", parsed)
    return [cast("AgentRow", cast("object", row)) for row in rows if isinstance(row, dict)]



def run_workers(run: Path, record: staff.StaffSnapshot) -> set[staff.StaffId]:
    """Select the run's workers from its roster and dispatch records."""
    roster = worker_admission.resolve_roster(run, session_staff.read_worker_roster(run / "mesh_roster.json"))
    launches = [worker_lifecycle.read_json_object(path) for path in run.glob("impl_worker_*.json")]
    members = {staff.StaffId(str(cast(dict[str, object], entry).get("staff_id", "")))
               for entry in [*roster.values(), *launches] if isinstance(entry, dict)}
    state = worker_lifecycle.read_json_object(run / unit_runs.STATE_FILE)
    passes = state.get("pass", {})
    fields = cast(dict[str, object], passes) if isinstance(passes, dict) else {}
    entries = [fields] if "instance_id" in fields else [cast(dict[str, object], item)
        for item in fields.values() if isinstance(item, dict)]
    names = {entry.get("worker_name") or entry.get("team_slot") for entry in entries}
    if names:
        parent = worker_admission.parent(record, run=run, roster=roster)
        members.update(row.id for row in record.find(kind="worker", parent=parent.id) if row.name in names)
    return members


@contextmanager
def _run_owner_read(run: Path) -> Generator[None]:
    try:
        yield
    except (ValueError, staff.StaffUnreadable) as error:
        print(f"anomaly: {run}: {error}", file=sys.stderr)


def removal_targets(own_runs: set[Path], rows: list[AgentRow], *,
                    roots: tuple[Path, ...] | None = None) -> dict[str, str]:
    record = staff.local_snapshot()
    owners: set[staff.StaffId] = set()
    for run in own_runs:
        with _run_owner_read(run):
            owners.add(worker_admission.parent(record, run=run).id)
    protected: set[staff.StaffId] = set()
    for run in unit_runs.live_run_folders(time.time(), roots):
        with _run_owner_read(run):
            protected.add(worker_admission.parent(record, run=run).id)
    selected: set[staff.StaffId] = set()
    for run in own_runs:
        with _run_owner_read(run):
            selected.update(run_workers(run, record))
    pane_workers: set[staff.StaffId] = set()
    for root in unit_runs.run_roots() if roots is None else roots:
        for pin in root.glob("*/worker_transport"):
            if unit_runs.worker_transport(pin.parent) == "herdr":
                with _run_owner_read(pin.parent):
                    pane_workers.update(run_workers(pin.parent, record))
    listed = {row.get("sessionId", "") for row in rows}
    targets: dict[str, str] = {}
    reason = OWN_RUN_FINISHED if own_runs else DIRECTOR_NOT_LIVE
    for member in record.find(kind="worker"):
        if not isinstance(record.backends.get(member.id), staff.ClaudeBackend):
            continue
        parent = record.parent_of(member)
        if parent is None or (parent.id not in owners and (parent.current_session in listed or parent.id in protected)):
            continue
        if (parent.id in owners and member.id in selected) or not own_runs:
            if member.id in pane_workers:
                print(f"anomaly: {member.name}: pane worker retained; use worker.py end from its parent", file=sys.stderr)
                continue
            agent = next((row for row in rows if row.get("sessionId") == member.current_session), None)
            targets[agent.get("id", member.current_session) if agent is not None else member.current_session] = reason
    return targets


def retire_removed_worker(session_id: str) -> None:
    """Retire a removed session while its run membership locks remain held."""
    record = staff.local_snapshot()
    for member in record.find(kind="worker"):
        if member.current_session == session_id:
            _ = staff.retire(member.id)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0] if __doc__ else None)
    _ = parser.add_argument(
        "--session-dir", action="append", default=[], help="a run of the caller's; all its workers go (repeatable)"
    )
    _ = parser.add_argument("--run-root", action="append", default=[])
    args = parser.parse_args()
    session_dirs = cast("list[str]", args.session_dir)
    own_runs = {Path(session_dir).resolve() for session_dir in session_dirs if session_dir}

    pane_runs = {run for run in own_runs if unit_runs.worker_transport(run) == "herdr"}
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
        own_runs.difference_update(pane_runs)
        if not own_runs:
            return pane_code

    claude = claude_bin()
    rows = list_sessions(claude)
    if rows is None:
        print("remove_workers: could not list sessions with `claude agents --json`; nothing removed.", file=sys.stderr)
        return 1

    roots = tuple(Path(root) for root in cast(list[str], args.run_root))
    targets = removal_targets(own_runs, rows, roots=roots or None)

    caller = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    removed = 0
    failed = 0
    for row in rows:
        worker_id = row.get("id", "")
        if row.get("kind") != "background" or worker_id not in targets:
            continue
        if caller and row.get("sessionId") == caller:
            continue
        name = row.get("name", "?")
        session_id = row.get("sessionId", "")
        runs = tuple(own_runs)
        try:
            with worker_lifecycle.membership_operation(*runs):
                result = subprocess.run(
                    [claude, "rm", worker_id],
                    capture_output=True,
                    text=True,
                    timeout=CLAUDE_TIMEOUT_SECS,
                    check=False,
                )
                if result.returncode == 0:
                    retire_removed_worker(session_id)
                    removed += 1
                    print(f"removed worker {name} ({worker_id}): {targets[worker_id]}")
                else:
                    failed += 1
                    detail = (result.stderr or result.stdout).strip()
                    print(f"remove_workers: could not remove {name} ({worker_id}): {detail}", file=sys.stderr)
        except worker_lifecycle.OperationDeadlineExpired:
            raise
        except (OSError, subprocess.TimeoutExpired, staff.StaffRefused, staff.StaffUnreadable) as error:
            failed += 1
            print(f"remove_workers: could not remove {name} ({worker_id}): {error}", file=sys.stderr)

    # A prior removal can finish before its staff publication.
    listed = {identity for row in rows for identity in (row.get("id", ""), row.get("sessionId", ""))}
    for session_id in set(targets).difference(listed):
        runs = tuple(own_runs)
        try:
            with worker_lifecycle.membership_operation(*runs):
                retire_removed_worker(session_id)
        except worker_lifecycle.OperationDeadlineExpired:
            raise
        except (OSError, staff.StaffRefused, staff.StaffUnreadable) as error:
            failed += 1
            print(f"remove_workers: {session_id}: {error}", file=sys.stderr)

    if removed == 0 and failed == 0:
        print("remove_workers: no workers to remove.")
    return 1 if failed else pane_code


if __name__ == "__main__":
    sys.exit(main())
