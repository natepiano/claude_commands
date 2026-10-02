#!/usr/bin/env python3
"""Report the time verify.sh pass records saved, for any span and workspace.

Reads the build log's calls table (`buildlog query`), one row per delegate
`test`/`lint` call that could use a pass record: ran, failed, interrupted,
reused (a recorded pass) or replayed (a recorded lint failure), with saved_s,
the recorded run's wall time minus cargo's build time. Both machines' calls
are there: natedev's hourly `buildlog sync` pulls the Mac's records, so the
Mac's latest calls are as fresh as the last sync, which the report names. A
workspace is the repo every worktree shares, named by its folder (hana), so
both machines and every worktree add into it.

Usage: verify_saved.py [--from DATE] [--to DATE] [--days N] [--workspace NAME]
                       [--by workspace|worktree|day|week] [--no-mac]

--no-mac keeps only this machine's calls.
"""

import argparse
import json
import os
import socket
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TypedDict, cast

BUILDLOG = Path.home() / ".claude/scripts/buildlog/buildlog"
QUERY = (
    "SELECT started_at, host, repo, worktree_name, branch, outcome, wall_s, saved_s"
    + " FROM calls WHERE tool='verify.sh' AND cached=1"
)


class Call(TypedDict):
    started_at: str
    host: str
    repo: str | None
    worktree_name: str | None
    branch: str | None
    outcome: str
    wall_s: int
    saved_s: int


class SyncStatus(TypedDict):
    at: str
    peer: str
    ok: bool
    last_ok: str | None


@dataclass
class Tally:
    ran: int = 0
    reused: int = 0
    failed: int = 0
    saved: int = 0
    run_time: int = 0


def duration(seconds: int) -> str:
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours} h {minutes:02d} m"
    if minutes:
        return f"{minutes} m {secs:02d} s"
    return f"{secs} s"


def build_log_root() -> Path:
    """Where buildlog keeps its records; store.root() in ~/.claude/scripts/buildlog."""
    override = os.environ.get("BUILDLOG_DIR")
    return Path(override) if override else Path.home() / ".local/state/buildlog"


def this_host() -> str:
    """The short host name the build log files this machine's records under."""
    return socket.gethostname().split(".")[0]


def calls() -> list[Call]:
    result = subprocess.run([str(BUILDLOG), "query", "--json", QUERY], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        sys.exit(f"verify_saved: buildlog query failed: {result.stderr.strip()}")
    return cast(list[Call], json.loads(result.stdout))


def local_time(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp).astimezone()


def sync_note() -> str:
    try:
        status = cast(SyncStatus, json.loads((build_log_root() / "sync.json").read_text()))
    except (OSError, ValueError):
        return "Mac records: never synced on this machine (natedev's hourly buildlog sync pulls them)."
    peer = "Mac" if status["peer"] == "mac" else status["peer"]
    if status["last_ok"] is None:
        return f"{peer} records: never synced (last attempt {local_time(status['at']):%Y-%m-%d %H:%M})."
    return f"{peer} records as of {local_time(status['last_ok']):%Y-%m-%d %H:%M} (last sync)."


def group_key(call: Call, by: str) -> str:
    workspace = call["repo"] or "-"
    local_day = local_time(call["started_at"]).date()
    if by == "worktree":
        return f"{workspace} · {call['worktree_name'] or '-'} · {call['branch'] or '-'}"
    if by == "day":
        return local_day.isoformat()
    if by == "week":
        return f"week of {(local_day - timedelta(days=local_day.weekday())).isoformat()}"
    return workspace


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    _ = parser.add_argument("--from", dest="start", type=date.fromisoformat)
    _ = parser.add_argument("--to", dest="end", type=date.fromisoformat)
    _ = parser.add_argument("--days", type=int, default=7)
    _ = parser.add_argument("--workspace")
    _ = parser.add_argument("--by", choices=("workspace", "worktree", "day", "week"), default="workspace")
    _ = parser.add_argument("--no-mac", action="store_true")
    args = parser.parse_args()
    end = cast(date | None, args.end) or date.today()
    start = cast(date | None, args.start) or end - timedelta(days=cast(int, args.days) - 1)
    workspace_filter = cast(str | None, args.workspace)
    by = cast(str, args.by)
    only_here = cast(bool, args.no_mac)
    host = this_host()

    tallies: dict[str, Tally] = {}
    for call in calls():
        if only_here and call["host"] != host:
            continue
        if not start <= local_time(call["started_at"]).date() <= end:
            continue
        if workspace_filter and call["repo"] != workspace_filter:
            continue
        tally = tallies.setdefault(group_key(call, by), Tally())
        if call["outcome"] in ("reused", "replayed"):
            tally.reused += 1
            tally.saved += call["saved_s"]
        elif call["outcome"] == "ran":
            tally.ran += 1
            tally.run_time += call["wall_s"]
        else:
            tally.failed += 1
            tally.run_time += call["wall_s"]

    scope = workspace_filter or "all workspaces"
    where = f", {host} only" if only_here else ""
    print(f"verify.sh time saved, {scope}{where}, {start} to {end}, by {by}\n")
    if not only_here:
        print(f"{sync_note()}\n")
    if not tallies:
        print("No calls recorded in that span.")
        return
    print(f"| {by.capitalize()} | Reused | Saved | Ran | Failed | Time running | Reused share |")
    print("|---|---|---|---|---|---|---|")
    rows = sorted(tallies.items()) if by in ("day", "week") else sorted(
        tallies.items(), key=lambda item: item[1].saved, reverse=True
    )
    total = Tally()
    for name, tally in rows:
        count = tally.ran + tally.reused + tally.failed
        print(
            f"| {name} | {tally.reused} | {duration(tally.saved)} | {tally.ran} | {tally.failed}"
            + f" | {duration(tally.run_time)} | {tally.reused * 100 // max(count, 1)}% |"
        )
        total.ran += tally.ran
        total.reused += tally.reused
        total.failed += tally.failed
        total.saved += tally.saved
        total.run_time += tally.run_time
    count = total.ran + total.reused + total.failed
    print(
        f"| **Total** | **{total.reused}** | **{duration(total.saved)}** | {total.ran} | {total.failed}"
        + f" | {duration(total.run_time)} | {total.reused * 100 // max(count, 1)}% |"
    )


if __name__ == "__main__":
    main()
