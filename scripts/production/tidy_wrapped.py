#!/usr/bin/env python3
"""Delete a wrapped production's reminder state, counts, reports and state folder.

Closed question records remain in the production's question folder.

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

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.production import staff, unit_lookup
from scripts.production.store_owner import showrunner_id  # noqa: E402
from scripts.copies import copies  # noqa: E402
from scripts.lib import scratch  # noqa: E402
from scripts.production.production_doc import WRAPPED  # noqa: E402
from proposals_folder import issues_folder  # noqa: E402

if TYPE_CHECKING:
    from update_registration import QuestionStaff

STATE_DIR = Path(os.environ.get("SHOWRUNNER_STATE_DIR") or Path.home() / ".local/state/showrunner")
NOTIFIER_STATE_DIR = Path(os.environ.get("NOTIFIER_STATE_DIR") or Path.home() / ".local/state/notifier")
# The state folder holds these shared folders beside each production's own; a slug never names one.
SHARED = frozenset(("footers-off", "review-paused", "outstanding", "counts", "reports"))
SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")


class TidyRefused(Exception):
    pass


def outstanding_path(slug: str, *, identities: QuestionStaff | None = None) -> Path:
    if not SLUG.fullmatch(slug) or slug in SHARED:
        raise TidyRefused(f"{slug!r} is not a production slug")
    identity = showrunner_id(slug, record=identities.record if identities else None,
                             sources=identities.sources if identities else None)
    return STATE_DIR / "outstanding" / f"{identity}.json"


def owned_paths(slug: str, *, identities: QuestionStaff | None = None) -> tuple[Path, ...]:
    from update_registration import QuestionStaff

    if not SLUG.fullmatch(slug) or slug in SHARED:
        raise TidyRefused(f"{slug!r} is not a production slug")
    identities = identities if identities is not None else QuestionStaff.read()
    identity = showrunner_id(slug, record=identities.record, sources=identities.sources)
    return (STATE_DIR / "footers-off" / identity, STATE_DIR / "review-paused" / f"{identity}.json",
            STATE_DIR / "outstanding" / f"{identity}.answered.jsonl",
            STATE_DIR / "outstanding" / f"{identity}.history.jsonl",
            STATE_DIR / "outstanding" / f"{identity}.lock",
            STATE_DIR / "outstanding" / f"{identity}.json", STATE_DIR / "counts" / identity,
            STATE_DIR / "reports" / f"{identity}.json", STATE_DIR / identity)


def can_hold_questions(document: Path) -> bool:
    """Check for question storage before reading the staff record."""
    return issues_folder(document) is not None or any(
        path.is_dir() for path in STATE_DIR.glob("*/questions")
    )


def tidy(slug: str, document: Path | None = None, *, identities: QuestionStaff | None = None) -> list[Path]:
    """Close the production's questions and delete its state files."""
    from update_registration import QuestionFolder, QuestionStaff, close_questions, question_folder

    if not SLUG.fullmatch(slug) or slug in SHARED:
        raise TidyRefused(f"{slug!r} is not a production slug")
    identities = identities if identities is not None else QuestionStaff.read()
    reference = identities.owner(document).id if document is not None else slug
    paths = owned_paths(reference, identities=identities)
    if document is not None and can_hold_questions(document):
        try:
            folder = question_folder(document, identities)
            if isinstance(folder, QuestionFolder):
                close_questions(document, identities)
                directory = paths[-1]
                if folder.path.parent == directory and directory.is_dir():
                    paths = (*paths[:-1], *(path for path in directory.iterdir() if path != folder.path))
        except staff.StaffRefused as error:
            raise TidyRefused(str(error)) from error
    owners = {slug}
    if document is not None:
        record = identities.record
        try:
            owners.update(row.name for row in unit_lookup.doc_units(document, record))
        except staff.StaffUnreadable:
            pass  # a production with no showrunner row has no units
    try:
        for owner in sorted(owners):
            copies.remove_trees(owner)
    except (copies.CopyError, OSError, scratch.ScratchError) as error:
        raise TidyRefused(str(error)) from error
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

    identities: QuestionStaff | None = None
    failed = False
    for name in argv:
        try:
            slug = wrapped_slug(Path(name))
            if identities is None:
                identities = QuestionStaff.read()
            removed = tidy(slug, Path(name), identities=identities)
        except (TidyRefused, RegistrationFailure, staff.StaffRefused, staff.StaffUnreadable,
                OSError, UnicodeError, ValueError) as error:
            print(f"kept: {error}", file=sys.stderr)
            failed = True
            continue
        count = len(removed)
        print(f"{slug}: removed {count} state file{'' if count == 1 else 's'}" if count
              else f"{slug}: no state files")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
