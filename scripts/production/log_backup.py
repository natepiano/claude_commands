#!/usr/bin/env python3
"""Keep dated copies of a production's LOG, so a LOG emptied by mistake loses at most one update interval.

Usage: log_backup.py <production doc>

`production_check.sh` runs this for a running production, and the notifier runs that check once per
scheduled update tick, so every production is covered with no setup of its own. Copies go to
`<state>/<slug>/log-backups/log-<YYYYmmddTHHMM>.md`, stamped in the production's zone as LOG lines
are, and the newest KEEP stay.

An empty or missing LOG is not copied, so an emptied LOG never pushes out a good copy. A LOG unchanged
since the newest copy is not copied again, so the copies kept span KEEP changes.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

from production_units import read_production
from production_units import Refusal

STATE_DIR = Path(os.environ.get("SHOWRUNNER_STATE_DIR") or Path.home() / ".local/state/showrunner")
BACKUPS = "log-backups"
KEEP = 48


def copies(directory: Path) -> list[Path]:
    """The copies kept in `directory`, oldest first: the stamp sorts as text."""
    return sorted(directory.glob("log-*.md"))


def backup(log: Path, directory: Path, now: datetime) -> Path | None:
    """Copy `log` under `now`'s stamp and drop all but the newest KEEP; None when nothing new was copied."""
    try:
        content = log.read_bytes()
    except FileNotFoundError:
        return None
    if not content.strip():
        return None
    kept = copies(directory)
    if kept and kept[-1].read_bytes() == content:
        return None
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"log-{now:%Y%m%dT%H%M}.md"
    # Written beside the target and renamed, so a copy is never seen half-written.
    scratch = target.with_name(f".{target.name}.{os.getpid()}")
    _ = scratch.write_bytes(content)
    _ = scratch.replace(target)
    for old in copies(directory)[:-KEEP]:
        old.unlink()
    return target


def main(arguments: list[str]) -> int:
    if len(arguments) != 1:
        print("usage: log_backup.py <production doc>", file=sys.stderr)
        return 2
    try:
        production = read_production(Path(arguments[0]))
        target = backup(production.log, STATE_DIR / production.slug / BACKUPS, datetime.now(production.zone))
    except (Refusal, OSError) as error:
        print(f"log_backup: {error}", file=sys.stderr)
        return 1
    print(f"copied to {target}" if target else "nothing new to copy")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
