#!/usr/bin/env python3
"""Recompute the nightly review's rust target weights from recent commit activity.

The weights live on one line of commands/nightly_review.md ("by weight: hana 12, ... (of 20)"),
which is also where the targets come from. Hana keeps a fixed share: the user set it as a
priority, not as a measure of activity. The rest of the nights go to the other targets in
proportion to the square root of their unique non-merge commits over the window, at least one
each; the root keeps a repo of many small commits from taking every night.

`weights.py [--days N]` prints current and proposed weights; `--apply` writes the proposal.
"""

from __future__ import annotations

import argparse
import math
import re
import subprocess
from pathlib import Path

CLAUDE = Path.home() / ".claude"
COMMAND = CLAUDE / "commands" / "nightly_review.md"
LINE = re.compile(r"by weight: (?P<weights>[^()]+) \(of (?P<total>\d+)\)")
FIXED = {"hana": 12}
# Targets that are not a ~/rust repo: the repo and the paths that make them up, old names included.
SOURCES: dict[str, tuple[Path, list[str]]] = {
    "showrunner": (CLAUDE, ["commands/showrunner", "commands/producer", "commands/unit", "commands/plan/delegate*",
                            "scripts/delegate", "scripts/production", "docs/production_format.md"]),
}


def current() -> tuple[dict[str, int], int]:
    match = LINE.search(COMMAND.read_text(encoding="utf-8"))
    if match is None:
        raise SystemExit(f"no 'by weight: ... (of N)' line in {COMMAND}")
    pairs = (item.rsplit(" ", 1) for item in match["weights"].split(", "))
    return {name: int(weight) for name, weight in pairs}, int(match["total"])


def commits(target: str, days: int) -> int:
    repo, paths = SOURCES.get(target, (Path.home() / "rust" / target, []))
    log = subprocess.run(["git", "-C", str(repo), "log", "--all", "--no-merges", f"--since={days}.days", "--format=%H",
                          "--", *paths], capture_output=True, text=True, check=True)
    return len(set(log.stdout.split()))


def allocate(activity: dict[str, int], slots: int) -> dict[str, int]:
    """Split slots in proportion to sqrt(activity), at least 1 each, largest remainder first."""
    if slots < len(activity):
        raise ValueError(f"{slots} nights cannot give {len(activity)} targets one each")
    share: dict[str, int] = {}
    left = dict(activity)
    while True:
        total = sum(math.sqrt(n) for n in left.values())
        room = slots - sum(share.values())
        quota = {t: room * math.sqrt(n) / total if total else room / len(left) for t, n in left.items()}
        small = [t for t, q in quota.items() if q < 1]
        if not small:
            break
        for target in small:
            share[target] = 1
            del left[target]
        if not left:
            return share
    for target, q in quota.items():
        share[target] = math.floor(q)
    spare = slots - sum(share.values())
    for target in sorted(quota, key=lambda t: (quota[t] - math.floor(quota[t]), activity[t]), reverse=True)[:spare]:
        share[target] += 1
    return share


def main() -> None:
    parser = argparse.ArgumentParser(description="Recompute the nightly review's rust target weights.")
    _ = parser.add_argument("--days", type=int, default=90)
    _ = parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    days: int = args.days  # pyright: ignore[reportAny]
    apply: bool = args.apply  # pyright: ignore[reportAny]

    old, total = current()
    activity = {t: commits(t, days) for t in old}
    proposed = dict(FIXED) | allocate({t: n for t, n in activity.items() if t not in FIXED}, total - sum(FIXED.values()))
    order = sorted(proposed, key=lambda t: (-proposed[t], -activity[t]))

    print(f"{'target':15} {f'commits/{days}d':>12} {'current':>8} {'proposed':>9}")
    for target in order:
        note = "  (fixed priority)" if target in FIXED else ""
        print(f"{target:15} {activity[target]:12} {old[target]:8} {proposed[target]:9}{note}")
    if proposed == old:
        print("unchanged")
        return
    if apply:
        text = COMMAND.read_text(encoding="utf-8")
        line = "by weight: " + ", ".join(f"{t} {proposed[t]}" for t in order) + f" (of {total})"
        _ = COMMAND.write_text(LINE.sub(line, text, count=1), encoding="utf-8")
        print(f"applied to {COMMAND}")


if __name__ == "__main__":
    main()
