#!/usr/bin/env python3
"""Delete state for wrapped productions without active timers.

Closed questions remain in their question folders.
"""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import os
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from scripts.production import showrunners, staff, unit_lookup  # noqa: E402
from scripts.copies import copies  # noqa: E402
from scripts.production.production_doc import WRAPPED  # noqa: E402
from scripts.production.proposals_folder import issues_folder  # noqa: E402

if TYPE_CHECKING:
    from scripts.production.update_registration import QuestionStaff

STATE_DIR = Path(os.environ.get("SHOWRUNNER_STATE_DIR") or Path.home() / ".local/state/showrunner")
NOTIFIER_STATE_DIR = Path(os.environ.get("NOTIFIER_STATE_DIR") or Path.home() / ".local/state/notifier")


class TidyRefused(Exception):
    pass


def owned_paths(owner: staff.StaffRow) -> tuple[Path, ...]:
    identity = owner.id
    return (STATE_DIR / "footers-off" / identity, STATE_DIR / "review-paused" / f"{identity}.json",
            STATE_DIR / "outstanding" / f"{identity}.answered.jsonl",
            STATE_DIR / "outstanding" / f"{identity}.history.jsonl",
            STATE_DIR / "outstanding" / f"{identity}.lock",
            STATE_DIR / "outstanding" / f"{identity}.json", STATE_DIR / "counts" / identity,
            STATE_DIR / "reports" / f"{identity}.json", STATE_DIR / identity)


def can_hold_questions(document: Path) -> bool:
    """Check for question storage before reading the staff record."""
    folder = issues_folder(document)
    return folder is not None or any(
        path.is_dir() for path in STATE_DIR.glob("*/questions")
    )


def tidy(production_name: str, document: Path | None = None, *, identities: QuestionStaff | None = None) -> list[Path]:
    """Close the production's questions and delete its state files."""
    from scripts.production.update_registration import QuestionFolder, QuestionStaff, close_questions, question_folder

    identities = identities if identities is not None else QuestionStaff.read()
    row = identities.owner(document) if document is not None else showrunners.owner_for(production_name, identities.record)
    paths = owned_paths(row)
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
    owners = {str(row.id)}
    if document is not None:
        record = identities.record
        try:
            for row in unit_lookup.doc_units(document, record):
                owners.add(str(row.id))
        except staff.StaffUnreadable:
            pass  # a production with no showrunner row has no units
    try:
        for owner in sorted(owners):
            copies.remove_trees(owner)
    except copies.CopyError as error:
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


def wrapped_owner(doc: Path, record: staff.StaffSnapshot) -> staff.StaffRow:
    """Read the wrapped owner's staff name and require no update timer."""
    owner = unit_lookup.doc_owner(doc, record)
    lines = doc.read_text(encoding="utf-8").splitlines()
    status = next((line for line in lines if line.startswith("> **Status:")), "")
    if not status.startswith(WRAPPED):
        raise TidyRefused(f"{doc}: not wrapped")
    if (NOTIFIER_STATE_DIR / f"showrunner-{owner.id}").exists():
        raise TidyRefused(f"{doc}: {owner.name} still has an update timer")
    return owner


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: tidy_wrapped.py <production doc>...", file=sys.stderr)
        return 2
    from scripts.production.update_registration import QuestionStaff, RegistrationFailure

    identities: QuestionStaff | None = None
    failed = False
    for name in argv:
        try:
            if identities is None:
                identities = QuestionStaff.read()
            owner = wrapped_owner(Path(name), identities.record)
            removed = tidy(owner.name, Path(name), identities=identities)
        except (TidyRefused, RegistrationFailure, staff.StaffRefused, staff.StaffUnreadable,
                OSError, UnicodeError, ValueError) as error:
            print(f"kept: {error}", file=sys.stderr)
            failed = True
            continue
        count = len(removed)
        print(f"{owner.name}: removed {count} state file{'' if count == 1 else 's'}" if count
              else f"{owner.name}: no state files")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
