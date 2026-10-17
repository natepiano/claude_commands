"""The footer's counts line: how much waits on the user, read at render time and never stored.

Changes are the rows still to review on the list the doc's Change review line names, proposals the
open proposal issues in its Proposals folder, and projects the improvement-queue lines still to
decide or hand out, for a doc with an Improvement queue line. Questions are counted only where no
Waiting on block carries them, so each count is shown once.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import NamedTuple

import change_review
import issues
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
    folder = proposals_folder.issues_folder(doc)
    return 0 if folder is None or not folder.is_dir() else len(issues.open_issues(folder, proposals_folder.PROPOSAL_TAG))


def open_project(line: str) -> bool:
    """A ledger line still to decide (`proposed`), or accepted and not yet handed out or settled."""
    match = LEDGER_STATUS.match(line)
    if match is None:
        return False
    status = match.group("status").strip()
    return status.startswith("proposed") or (status.startswith("accepted") and SETTLED.search(status) is None)


def projects(doc: Path) -> int:
    try:
        ledger = Path(production_field(doc.read_text(encoding="utf-8").splitlines(), IMPROVEMENT_QUEUE_FIELD))
    except Refusal:
        return 0
    try:
        lines = ledger.expanduser().read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0
    return sum(open_project(line) for line in lines)


def read_counts(doc: Path, *, questions: int = 0) -> FooterCounts:
    """Each count for one production doc; a source that cannot be read counts nothing."""
    try:
        return FooterCounts(changes(doc), proposals(doc), projects(doc), questions)
    except OSError:
        return FooterCounts(questions=questions)


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
