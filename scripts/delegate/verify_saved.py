#!/usr/bin/env python3
"""Report the time verify.sh pass records saved, for any span and workspace.

Reads ~/.local/state/verify/events.jsonl, one line per delegate `test`/`lint`
call, which verify.sh appends: ran, failed, interrupted, or reused with
saved_s, the recorded run's wall time minus cargo's build time. The Mac's
ledger is read over ssh. A workspace is the repo every worktree shares, named
by its folder (hana), so both machines and every worktree add into it.

Usage: verify_saved.py [--from DATE] [--to DATE] [--days N] [--workspace NAME]
                       [--by workspace|worktree|day|week] [--no-mac]
"""

import argparse
import json
import subprocess
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TypedDict, cast

LEDGER = Path.home() / ".local/state/verify/events.jsonl"
MAC_LEDGER = ".local/state/verify/events.jsonl"


class Event(TypedDict):
    at: str
    machine: str
    workspace: str
    worktree: str
    branch: str
    commit: str
    command: str
    outcome: str
    session: str
    wait_s: int
    wall_s: int
    build_s: int
    saved_s: int


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


def parse(text: str) -> list[Event]:
    return [cast(Event, json.loads(line)) for line in text.splitlines() if line.strip()]


def mac_events() -> tuple[list[Event], str]:
    try:
        result = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", "mac", f"cat {MAC_LEDGER} 2>/dev/null || true"],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except subprocess.TimeoutExpired:
        return [], "Mac unreachable (timed out); its calls are missing."
    if result.returncode != 0:
        return [], "Mac unreachable (asleep with the lid closed?); its calls are missing."
    return parse(result.stdout), ""


def group_key(event: Event, by: str) -> str:
    workspace = Path(event["workspace"]).name
    local_day = datetime.fromisoformat(event["at"]).astimezone().date()
    if by == "worktree":
        return f"{workspace} · {Path(event['worktree']).name} · {event['branch']}"
    if by == "day":
        return local_day.isoformat()
    if by == "week":
        return f"week of {(local_day - timedelta(days=local_day.weekday())).isoformat()}"
    return workspace


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--from", dest="start", type=date.fromisoformat)
    _ = parser.add_argument("--to", dest="end", type=date.fromisoformat)
    _ = parser.add_argument("--days", type=int, default=7)
    _ = parser.add_argument("--workspace")
    _ = parser.add_argument("--by", choices=("workspace", "worktree", "day", "week"), default="workspace")
    _ = parser.add_argument("--no-mac", action="store_true")
    _ = parser.add_argument("--ledger", type=Path, default=LEDGER)
    args = parser.parse_args()
    ledger = cast(Path, args.ledger)
    end = cast(date | None, args.end) or date.today()
    start = cast(date | None, args.start) or end - timedelta(days=cast(int, args.days) - 1)
    workspace_filter = cast(str | None, args.workspace)
    by = cast(str, args.by)

    events = parse(ledger.read_text()) if ledger.exists() else []
    note = ""
    if not cast(bool, args.no_mac):
        mac, note = mac_events()
        events += mac

    tallies: dict[str, Tally] = {}
    for event in events:
        local_day = datetime.fromisoformat(event["at"]).astimezone().date()
        if not start <= local_day <= end:
            continue
        if workspace_filter and Path(event["workspace"]).name != workspace_filter:
            continue
        tally = tallies.setdefault(group_key(event, by), Tally())
        if event["outcome"] == "reused":
            tally.reused += 1
            tally.saved += event["saved_s"]
        elif event["outcome"] == "ran":
            tally.ran += 1
            tally.run_time += event["wall_s"]
        else:
            tally.failed += 1
            tally.run_time += event["wall_s"]

    scope = workspace_filter or "all workspaces"
    print(f"verify.sh time saved, {scope}, {start} to {end}, by {by}\n")
    if note:
        print(f"{note}\n")
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
        calls = tally.ran + tally.reused + tally.failed
        print(
            f"| {name} | {tally.reused} | {duration(tally.saved)} | {tally.ran} | {tally.failed}"
            + f" | {duration(tally.run_time)} | {tally.reused * 100 // max(calls, 1)}% |"
        )
        total.ran += tally.ran
        total.reused += tally.reused
        total.failed += tally.failed
        total.saved += tally.saved
        total.run_time += tally.run_time
    calls = total.ran + total.reused + total.failed
    print(
        f"| **Total** | **{total.reused}** | **{duration(total.saved)}** | {total.ran} | {total.failed}"
        + f" | {duration(total.run_time)} | {total.reused * 100 // max(calls, 1)}% |"
    )


if __name__ == "__main__":
    main()
