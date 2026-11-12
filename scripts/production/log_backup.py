#!/usr/bin/env python3
"""Keep dated copies of a production's LOG.

Usage: log_backup.py <production name or staff id>

`production_check.sh` calls this for a running production. A manual call also creates a copy.
No automatic report runs this check.
Copies use `<state>/<staff id>/log-backups/log-<YYYYmmddTHHMM>.md` in the production's zone.
The newest KEEP copies remain.

An empty, missing or unchanged LOG creates no copy.
"""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import os
import sys
from datetime import datetime
from pathlib import Path

from scripts.production import staff
from scripts.lib.atomic_file import write_bytes_atomic
from scripts.production.production_doc import Refusal, read_production  # noqa: E402

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
    write_bytes_atomic(target, content)
    for old in copies(directory)[:-KEEP]:
        old.unlink()
    return target


def main(arguments: list[str]) -> int:
    if len(arguments) != 1:
        print("usage: log_backup.py <production name or staff id>", file=sys.stderr)
        return 2
    try:
        production = read_production(arguments[0])
        target = backup(production.log, STATE_DIR / production.owner.id / BACKUPS, datetime.now(production.zone))
    except (Refusal, OSError, staff.StaffRefused, staff.StaffUnreadable) as error:
        print(f"log_backup: {error}", file=sys.stderr)
        return 1
    print(f"copied to {target}" if target else "nothing new to copy")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
