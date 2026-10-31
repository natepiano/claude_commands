#!/usr/bin/env python3
"""Delete a wrapped production's own state files: its footer switch, review pause, open questions, footer counts,
footer report and state folder.

Usage: tidy_wrapped.py <production doc>...

The wrap calls `tidy` once the update timer is gone. Run by hand, this is the sweep for productions
wrapped before the wrap tidied: a doc that does not say wrapped, or whose production still has an
update timer, keeps every file.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

STATE_DIR = Path(os.environ.get("SHOWRUNNER_STATE_DIR") or Path.home() / ".local/state/showrunner")
NOTIFIER_STATE_DIR = Path(os.environ.get("NOTIFIER_STATE_DIR") or Path.home() / ".local/state/notifier")
# The state folder holds these shared folders beside each production's own; a slug never names one.
SHARED = frozenset(("footers-off", "review-paused", "outstanding", "counts", "reports"))
SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
WRAPPED = "> **Status: PRODUCTION — wrapped.**"


class TidyRefused(Exception):
    pass


def outstanding_path(slug: str) -> Path:
    if not SLUG.fullmatch(slug) or slug in SHARED:
        raise TidyRefused(f"{slug!r} is not a production slug")
    return STATE_DIR / "outstanding" / f"{slug}.json"


def owned_paths(slug: str) -> tuple[Path, ...]:
    outstanding = outstanding_path(slug)
    return (STATE_DIR / "footers-off" / slug, STATE_DIR / "review-paused" / f"{slug}.json",
            STATE_DIR / "outstanding" / f"{slug}.answered.jsonl",
            outstanding, STATE_DIR / "counts" / slug,
            STATE_DIR / "reports" / f"{slug}.json", STATE_DIR / slug)


def tidy(slug: str) -> list[Path]:
    """Delete the production's own state files and return those that existed."""
    removed: list[Path] = []
    for path in owned_paths(slug):
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        elif path.exists() or path.is_symlink():
            path.unlink()
        else:
            continue
        removed.append(path)
    return removed


def wrapped_slug(doc: Path) -> str:
    """The slug of a wrapped production with no update timer; anything else refuses."""
    slug = doc.name.removesuffix("-production.md")
    if slug == doc.name:
        raise TidyRefused(f"{doc}: production doc name must end in -production.md")
    lines = doc.read_text(encoding="utf-8").splitlines()
    status = next((line for line in lines if line.startswith("> **Status:")), "")
    if not status.startswith(WRAPPED):
        raise TidyRefused(f"{doc}: not wrapped")
    if (NOTIFIER_STATE_DIR / f"showrunner-{slug}").exists():
        raise TidyRefused(f"{doc}: {slug} still has an update timer")
    return slug


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: tidy_wrapped.py <production doc>...", file=sys.stderr)
        return 2
    failed = False
    for name in argv:
        try:
            slug = wrapped_slug(Path(name))
            removed = tidy(slug)
        except (TidyRefused, OSError, UnicodeError) as error:
            print(f"kept: {error}", file=sys.stderr)
            failed = True
            continue
        count = len(removed)
        print(f"{slug}: removed {count} state file{'' if count == 1 else 's'}" if count
              else f"{slug}: no state files")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
