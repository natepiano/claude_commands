"""The counts line: how much waits on the user, read at render time and never stored.

The status line and the dailies report's own footer show it; no reply carries it (user,
2026-10-18). Changes are the rows still to review on the list the doc's Change review line names,
proposals the open proposal issues in its Proposals folder that are its own (see
proposals_folder), and projects the improvement-queue
lines still to decide or hand out, for a doc with an Improvement queue line.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import NamedTuple

import change_review
import proposals_folder
from production_units import Refusal, production_field

IMPROVEMENT_QUEUE_FIELD = "Improvement queue"
COUNTS_HEAD = "For you: "
# A ledger line: `- <date> <title> (<file>) — <status>`.
LEDGER_STATUS = re.compile(r"^- \d{4}-\d\d-\d\d .* — (?P<status>.*)$")
SETTLED = re.compile(r"\b(assigned|applied|built|declined)\b", re.IGNORECASE)


class FooterCounts(NamedTuple):
    changes: int = 0
    proposals: int = 0
    projects: int = 0
    questions: int = 0


def total(each: list[FooterCounts]) -> FooterCounts:
    return FooterCounts(*(sum(column) for column in zip(FooterCounts(), *each)))


def changes(doc: Path) -> int:
    try:
        path = change_review.list_path(doc)
        return 0 if path is None or not path.exists() else len(change_review.pending(path))
    except (change_review.ChangeReviewError, OSError):
        return 0


def proposals(doc: Path) -> int:
    return len(proposals_folder.open_proposals(doc))


def open_project(line: str) -> bool:
    """A ledger line still to decide (`proposed`), or accepted and not yet handed out or settled."""
    match = LEDGER_STATUS.match(line)
    if match is None:
        return False
    status = match.group("status").strip()
    return status.startswith("proposed") or (status.startswith("accepted") and SETTLED.search(status) is None)


def ledger(doc: Path) -> Path | None:
    """The ledger the doc's Improvement queue line names, or None when it names none."""
    try:
        return Path(production_field(doc.read_text(encoding="utf-8").splitlines(), IMPROVEMENT_QUEUE_FIELD)).expanduser()
    except Refusal:
        return None


def projects(doc: Path) -> int:
    path = ledger(doc)
    if path is None:
        return 0
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0
    return sum(open_project(line) for line in lines)


def read_counts(doc: Path, *, questions: int = 0) -> FooterCounts:
    """Each count for one production doc; a source that cannot be read counts nothing."""
    try:
        return FooterCounts(changes(doc), proposals(doc), projects(doc), questions)
    except OSError:
        return FooterCounts(questions=questions)


def sources(doc: Path) -> list[Path]:
    """The files the counts read, so a copy of them can tell when it is out of date.

    A proposal closes in place, so each open one is named beside its folder, which a new one
    changes.
    """
    try:
        listed = change_review.list_path(doc)
    except (change_review.ChangeReviewError, OSError):
        listed = None
    folder = proposals_folder.issues_folder(doc)
    try:
        queue = ledger(doc)
    except OSError:
        queue = None
    found = [doc, *(path for path in (listed, folder, queue) if path is not None)]
    # A unit added below changes which proposals are this production's.
    found.extend(proposals_folder.owner_docs(doc)[1:])
    try:
        found.extend(issue.path for issue in proposals_folder.open_proposals(doc))
    except OSError:
        pass
    return found


def plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def counts_line(counts: FooterCounts) -> str | None:
    """`For you: 2 changes to review, 1 proposal, 3 projects, 1 question`, zero counts left out; None when all are zero."""
    parts = [text for count, text in (
        (counts.changes, f"{plural(counts.changes, 'change')} to review"),
        (counts.proposals, plural(counts.proposals, "proposal")),
        (counts.projects, plural(counts.projects, "project")),
        (counts.questions, plural(counts.questions, "question"))) if count]
    return COUNTS_HEAD + ", ".join(parts) if parts else None
