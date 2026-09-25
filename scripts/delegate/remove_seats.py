#!/usr/bin/env python3
"""Remove delegate seats that no later step will message.

A claude seat launches as a named background session (agents/agent_bg.sh) and
is left alive when its turn ends, so a peer can still ask it a question. That
leaves someone to remove it, and for a long time nothing did: seats from runs a
week old were still listed by `claude agents`. A seat is named for its project,
so a stale `hana_catalyst-test` also shares its address with the next run's
tester, and a message to that name stops working until the sender picks a row.

The call is `claude rm`, never `claude stop`: stop answers "stopped" on a seat
whose process already exited and leaves it listed under its name. `rm` works on
running and exited sessions alike, leaves the working directory alone, and
keeps the transcript, so a seat removed in error comes back with
`claude --resume <session id>`.

Every launch appends `<id>\\t<name>` to its run's `seats` ledger. This removes:

  * every seat in `--session-dir`'s ledger -- the caller's own run, at the end
    of a phase or of the run, when nothing will message those seats again;
  * every seat of a run that is not live, from any ledger under the delegate
    root -- the sweep that catches a run whose orchestrator died before its
    own cleanup ran.

A run is live while a run-active marker names its session directory and the
orchestrator session that wrote the marker is still listed, or while its
heartbeat log moved in the last `LIVE_HEARTBEAT_SECS`. The heartbeat test
covers a run with no marker, which prepare_session.sh skips outside a Claude
session: a seat still working beats every minute.

Only background sessions are ever removed, and never the session running this.
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
from typing import TypedDict, cast

DEFAULT_ROOT = Path("/tmp/claude/delegate")
LEDGER = "seats"
HEARTBEAT = "heartbeat.log"
LIVE_HEARTBEAT_SECS = 600
CLAUDE_TIMEOUT_SECS = 30

# Seats launched before the ledger were named `<session dir basename>-<slot>`,
# and the basename was a uuid, so the name alone says which run owns them.
# Remove once no seat launched before the ledger can still be alive.
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


def live_runs(root: Path, listed_sessions: set[str]) -> set[Path]:
    live: set[Path] = set()
    active = root / "active"
    if active.is_dir():
        for marker in active.iterdir():
            if marker.name not in listed_sessions:
                continue
            lines = marker.read_text(encoding="utf-8").splitlines()
            if lines and lines[0].strip():
                live.add(Path(lines[0].strip()).resolve())
    now = time.time()
    for heartbeat in root.glob(f"*/{HEARTBEAT}"):
        if now - heartbeat.stat().st_mtime < LIVE_HEARTBEAT_SECS:
            live.add(heartbeat.parent.resolve())
    return live


def ledger_ids(run: Path) -> list[str]:
    ledger = run / LEDGER
    if not ledger.is_file():
        return []
    ids: list[str] = []
    for line in ledger.read_text(encoding="utf-8").splitlines():
        seat_id = line.split("\t", 1)[0].strip()
        if seat_id:
            ids.append(seat_id)
    return ids


def removal_targets(
    root: Path, own_run: Path | None, rows: list[AgentRow], live: set[Path]
) -> dict[str, str]:
    """Seat id to the reason it is being removed."""
    targets: dict[str, str] = {}
    if own_run is not None:
        for seat_id in ledger_ids(own_run):
            targets[seat_id] = "this run's phase is over"
    for ledger in root.glob(f"*/{LEDGER}"):
        run = ledger.parent.resolve()
        if run == own_run or run in live:
            continue
        for seat_id in ledger_ids(run):
            _ = targets.setdefault(seat_id, f"run {run.name} is not live")
    for row in rows:
        match = LEGACY_NAME.match(row.get("name", ""))
        if match is None:
            continue
        run = (root / match.group(1)).resolve()
        if run == own_run:
            _ = targets.setdefault(row.get("id", ""), "this run's phase is over")
        elif run not in live:
            _ = targets.setdefault(row.get("id", ""), f"run {run.name} is not live")
    return targets


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0] if __doc__ else None)
    _ = parser.add_argument("--session-dir", help="the caller's own run; all its seats go")
    _ = parser.add_argument("--delegate-root", default=str(DEFAULT_ROOT))
    args = parser.parse_args()
    session_dir = cast("str | None", args.session_dir)
    root = Path(cast("str", args.delegate_root))
    own_run = Path(session_dir).resolve() if session_dir else None

    claude = claude_bin()
    rows = list_sessions(claude)
    if rows is None:
        print("remove_seats: could not list sessions with `claude agents --json`; nothing removed.", file=sys.stderr)
        return 1

    listed_sessions = {row.get("sessionId", "") for row in rows}
    targets = removal_targets(root, own_run, rows, live_runs(root, listed_sessions))

    caller = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    removed = 0
    failed = 0
    for row in rows:
        seat_id = row.get("id", "")
        if row.get("kind") != "background" or seat_id not in targets:
            continue
        if caller and row.get("sessionId") == caller:
            continue
        name = row.get("name", "?")
        try:
            result = subprocess.run(
                [claude, "rm", seat_id],
                capture_output=True,
                text=True,
                timeout=CLAUDE_TIMEOUT_SECS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            failed += 1
            print(f"remove_seats: could not remove {name} ({seat_id}): {error}", file=sys.stderr)
            continue
        if result.returncode == 0:
            removed += 1
            print(f"removed seat {name} ({seat_id}): {targets[seat_id]}")
        else:
            failed += 1
            detail = (result.stderr or result.stdout).strip()
            print(f"remove_seats: could not remove {name} ({seat_id}): {detail}", file=sys.stderr)

    if removed == 0 and failed == 0:
        print("remove_seats: no seats to remove.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
