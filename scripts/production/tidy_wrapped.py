#!/usr/bin/env python3
"""Delete a wrapped production's reminder state, counts, reports and state folder.

Closed question records remain in the proposals store.

Usage: tidy_wrapped.py <production doc>...

A production with an update timer or a status other than wrapped keeps its state.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from update_registration import QuestionStaff

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


def tidy(slug: str, document: Path | None = None, *, identities: QuestionStaff | None = None) -> list[Path]:
    """Close the production's questions and delete its state files."""
    from update_registration import QuestionStaff, close_questions
    from proposals_folder import issues_folder

    paths = owned_paths(slug)
    if document is not None and issues_folder(document) is not None:
        identities = identities if identities is not None else QuestionStaff.read()
        close_questions(document, identities)
    removed: list[Path] = []
    for path in paths:
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
    from update_registration import QuestionStaff, RegistrationFailure
    from proposals_folder import issues_folder

    identities: QuestionStaff | None = None
    failed = False
    for name in argv:
        try:
            slug = wrapped_slug(Path(name))
            if issues_folder(Path(name)) is not None:
                identities = identities if identities is not None else QuestionStaff.read()
            removed = tidy(slug, Path(name), identities=identities)
        except (TidyRefused, RegistrationFailure, OSError, UnicodeError, ValueError) as error:
            print(f"kept: {error}", file=sys.stderr)
            failed = True
            continue
        count = len(removed)
        print(f"{slug}: removed {count} state file{'' if count == 1 else 's'}" if count
              else f"{slug}: no state files")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
