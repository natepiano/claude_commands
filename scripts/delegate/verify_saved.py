#!/usr/bin/env python3
"""Report the time verify.sh pass records saved, per worktree and branch.

Reads ~/.local/state/verify/saved.jsonl, one line per hit, which verify.sh
appends when a call is answered from a pass record. Each line's saved_s is the
recorded run's wall time minus cargo's build time: what the repeat would have
cost. One table: the last --days days and the whole ledger, per worktree and
branch, with a total row. The ledger is per machine.

Usage: verify_saved.py [--days N] [--ledger PATH]
"""

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TypedDict, cast

LEDGER = Path.home() / ".local/state/verify/saved.jsonl"


class Hit(TypedDict):
    at: str
    worktree: str
    branch: str
    command: str
    saved_s: int


@dataclass
class Tally:
    week_hits: int = 0
    week_saved: int = 0
    hits: int = 0
    saved: int = 0
    first: str = ""


def duration(seconds: int) -> str:
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours} h {minutes:02d} m"
    if minutes:
        return f"{minutes} m {secs:02d} s"
    return f"{secs} s"


def read_hits(ledger: Path) -> list[Hit]:
    if not ledger.exists():
        return []
    hits: list[Hit] = []
    for line in ledger.read_text().splitlines():
        if line.strip():
            hits.append(cast(Hit, json.loads(line)))
    return hits


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--days", type=int, default=7)
    _ = parser.add_argument("--ledger", type=Path, default=LEDGER)
    args = parser.parse_args()
    days = cast(int, args.days)
    ledger = cast(Path, args.ledger)

    hits = read_hits(ledger)
    if not hits:
        print(f"No hits recorded yet in {ledger}.")
        return

    since = datetime.now(timezone.utc) - timedelta(days=days)
    home = str(Path.home())
    tallies: dict[tuple[str, str], Tally] = {}
    for hit in hits:
        worktree = hit["worktree"].replace(home, "~", 1)
        tally = tallies.setdefault((worktree, hit["branch"]), Tally(first=hit["at"][:10]))
        tally.hits += 1
        tally.saved += hit["saved_s"]
        if datetime.fromisoformat(hit["at"]) >= since:
            tally.week_hits += 1
            tally.week_saved += hit["saved_s"]

    rows = sorted(tallies.items(), key=lambda item: (item[1].week_saved, item[1].saved), reverse=True)
    print(f"Time verify.sh pass records saved (last {days} days and since first hit)\n")
    print(f"| Worktree | Branch | Last {days} days | Saved | All time | Saved | Since |")
    print("|---|---|---|---|---|---|---|")
    for (worktree, branch), tally in rows:
        print(
            f"| `{worktree}` | `{branch}` | {tally.week_hits} hits | {duration(tally.week_saved)}"
            + f" | {tally.hits} hits | {duration(tally.saved)} | {tally.first} |"
        )
    week_hits = sum(tally.week_hits for tally in tallies.values())
    week_saved = sum(tally.week_saved for tally in tallies.values())
    all_hits = sum(tally.hits for tally in tallies.values())
    all_saved = sum(tally.saved for tally in tallies.values())
    print(
        f"| **Total** | | **{week_hits} hits** | **{duration(week_saved)}**"
        + f" | **{all_hits} hits** | **{duration(all_saved)}** | |"
    )


if __name__ == "__main__":
    main()
