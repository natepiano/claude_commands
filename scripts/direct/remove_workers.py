#!/usr/bin/env python3
"""Remove workers that no later step will message.

A claude worker launches as a named background session (agents/agent_bg.sh) and
is left alive when its turn ends, so a peer can still ask it a question. That
leaves someone to remove it, and for a long time nothing did: workers from runs a
week old were still listed by `claude agents`. A worker is named for its project,
so a stale `hana_catalyst-test` also shares its address with the next run's
tester, and a message to that name stops working until the sender picks a row.

The call is `claude rm`, never `claude stop`: stop answers "stopped" on a worker
whose process already exited and leaves it listed under its name. `rm` works on
running and exited sessions alike, leaves the working directory alone, and
keeps the transcript, so a worker removed in error comes back with
`claude --resume <session id>`.

Every launch appends `<id>\\t<name>` to its run's `workers` ledger. This removes:

  * every worker in each `--session-dir`'s ledger -- the caller's own run, at the
    end of a phase, or every run its unit director holds, at the end of the run
    (end_session.sh passes one `--session-dir` per run), when nothing will message
    those workers again;
  * every worker of a run that is not live, from any ledger under the run
    roots -- the sweep that catches a run whose unit director died before its
    own cleanup ran.

Claude sessions held by a unit worker ledger remain available until that unit
is retired, regardless of the run's phase or active marker.

A run is live while a run-active marker names its session directory and the
unit director that wrote the marker is still listed, or while its heartbeat
log moved in the last `unit_runs.LIVE_HEARTBEAT_SECS`. The heartbeat test covers
a run with no marker, which prepare_session.sh skips outside a Claude session: a
worker still working beats every minute.

Only background sessions are ever removed, and never the session running this.
Server reclamation belongs to codex_mesh's idle and retired-server cleanup;
run-end worker cleanup never launches it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, cast

if TYPE_CHECKING:
    from ..lib import unit_runs
    from .worker_state import worker_ledger
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
    import unit_runs
    from worker_state import worker_ledger

# Run folders sit under unit/, and under delegate/ for a run started before the rename.
DEFAULT_ROOTS = unit_runs.run_roots()
LEDGER = "workers"
CLAUDE_TIMEOUT_SECS = 30

# Workers launched before the ledger were named `<session dir basename>-<slot>`,
# and the basename was a uuid, so the name alone says which run owns them.
# Remove once no worker launched before the ledger can still be alive.
LEGACY_NAME = re.compile(
    r"^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})-(impl|test|review)$"
)


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


def live_runs(roots: tuple[Path, ...], listed_sessions: set[str]) -> set[Path]:
    live: set[Path] = set()
    now = time.time()
    for root in roots:
        active = root / "active"
        if active.is_dir():
            for marker in active.iterdir():
                if marker.name not in listed_sessions:
                    continue
                lines = marker.read_text(encoding="utf-8").splitlines()
                if lines and lines[0].strip():
                    live.add(Path(lines[0].strip()).resolve())
        for heartbeat in root.glob(f"*/{unit_runs.HEARTBEAT}"):
            if now - heartbeat.stat().st_mtime < unit_runs.LIVE_HEARTBEAT_SECS:
                live.add(heartbeat.parent.resolve())
    return live


def ledger_ids(run: Path) -> list[str]:
    ledger = worker_ledger(run)
    if not ledger.is_file():
        return []
    ids: list[str] = []
    for line in ledger.read_text(encoding="utf-8").splitlines():
        worker_id = line.split("\t", 1)[0].strip()
        if worker_id:
            ids.append(worker_id)
    return ids


def removal_targets(
    roots: tuple[Path, ...], own_runs: set[Path], rows: list[AgentRow], live: set[Path]
) -> dict[str, str]:
    """Worker id to the reason it is being removed."""
    targets: dict[str, str] = {}
    for own_run in sorted(own_runs):
        for worker_id in ledger_ids(own_run):
            _ = targets.setdefault(worker_id, "this run's phase is over")
    for directory in (found for root in roots for found in root.glob("*")):
        if not directory.is_dir() or not worker_ledger(directory).is_file():
            continue
        run = directory.resolve()
        if run in own_runs or run in live:
            continue
        for worker_id in ledger_ids(run):
            _ = targets.setdefault(worker_id, f"run {run.name} is not live")
    for row in rows:
        match = LEGACY_NAME.match(row.get("name", ""))
        if match is None:
            continue
        runs = {(root / match.group(1)).resolve() for root in roots}
        if runs & own_runs:
            _ = targets.setdefault(row.get("id", ""), "this run's phase is over")
        elif not runs & live:
            _ = targets.setdefault(row.get("id", ""), f"run {match.group(1)} is not live")
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.direct import workers
    try:
        held = workers.held_claude_sessions()
        # A run-end or broad sweep must also keep workers predating the unit
        # ledger while their director is still available to dispatch them.
        for run in {*(directory for root in roots for directory in root.glob("*")), *own_runs}:
            if run.is_dir() and workers.run_has_live_director(run):
                held.update(ledger_ids(run))
    except (workers.WorkerUnavailable, OSError, ValueError) as error:
        print(f"remove_workers: keeping workers until unit ledger can be read: {error}", file=sys.stderr)
        return {}
    for row in rows:
        if row.get("sessionId", "") in held or row.get("id", "") in held:
            _ = targets.pop(row.get("id", ""), None)
    for identity in held:
        _ = targets.pop(identity, None)
    return targets


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0] if __doc__ else None)
    _ = parser.add_argument(
        "--session-dir", action="append", default=[], help="a run of the caller's; all its workers go (repeatable)"
    )
    _ = parser.add_argument("--run-root", help="one run root in place of both run-folder names")
    args = parser.parse_args()
    session_dirs = cast("list[str]", args.session_dir)
    given = cast("str | None", args.run_root)
    roots = (Path(given),) if given else DEFAULT_ROOTS
    own_runs = {Path(session_dir).resolve() for session_dir in session_dirs if session_dir}

    claude = claude_bin()
    rows = list_sessions(claude)
    if rows is None:
        print("remove_workers: could not list sessions with `claude agents --json`; nothing removed.", file=sys.stderr)
        return 1

    listed_sessions = {row.get("sessionId", "") for row in rows}
    targets = removal_targets(roots, own_runs, rows, live_runs(roots, listed_sessions))

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
        try:
            result = subprocess.run(
                [claude, "rm", worker_id],
                capture_output=True,
                text=True,
                timeout=CLAUDE_TIMEOUT_SECS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            failed += 1
            print(f"remove_workers: could not remove {name} ({worker_id}): {error}", file=sys.stderr)
            continue
        if result.returncode == 0:
            removed += 1
            print(f"removed worker {name} ({worker_id}): {targets[worker_id]}")
        else:
            failed += 1
            detail = (result.stderr or result.stdout).strip()
            print(f"remove_workers: could not remove {name} ({worker_id}): {detail}", file=sys.stderr)

    if removed == 0 and failed == 0:
        print("remove_workers: no workers to remove.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
