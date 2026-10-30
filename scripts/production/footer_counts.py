"""The counts line: how much waits on the user, read at render time and never stored.

The status line and the dailies report's own footer show it; no reply carries it (user,
2026-10-18). Each count is the production's own: the showrunners above and below it count their
own (user, 2026-10-27). Changes are the production's open rows from its current and earlier
lists (change_review.scope_rows), proposals the issues and plan-backlog lines that /next proposal
lists for review (proposals_table.waiting_entries), and efficiency the proposed or deferred
lines in its Efficiency ledger. Accepted plan-backlog lines are work in progress and do not count.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import NamedTuple

import change_review
import proposals_folder
import proposals_table
from production_units import Refusal, production_field

EFFICIENCY_LEDGER_FIELD = "Efficiency ledger"
COUNTS_HEAD = "For you: "
EFFICIENCY_LINE = re.compile(r"^- \d{4}-\d\d-\d\d .*? — (?P<status>.*)$")


class FooterCounts(NamedTuple):
    changes: int = 0
    proposals: int = 0
    questions: int = 0
    efficiency: int = 0


def total(each: list[FooterCounts]) -> FooterCounts:
    return FooterCounts(*(sum(column) for column in zip(FooterCounts(), *each)))


def changes(doc: Path) -> int:
    try:
        return len(change_review.scope_rows(change_review.scope_list(doc), doc))
    except (change_review.ChangeReviewError, OSError):
        return 0


def proposals(doc: Path) -> int:
    return len(proposals_table.waiting_entries(doc)[0])


def open_efficiency(line: str) -> bool:
    """A dated efficiency ledger line whose first status is proposed or deferred."""
    match = EFFICIENCY_LINE.match(line)
    return match is not None and match.group("status").strip().startswith(("proposed", "deferred"))


def _field_path(doc: Path, field: str) -> Path | None:
    try:
        return Path(production_field(doc.read_text(encoding="utf-8").splitlines(), field)).expanduser()
    except Refusal:
        return None


def efficiency_ledger(doc: Path) -> Path | None:
    """The ledger the doc's Efficiency ledger line names, or None when it names none."""
    return _field_path(doc, EFFICIENCY_LEDGER_FIELD)


def efficiency(doc: Path) -> int:
    """Proposed and deferred efficiency proposals in the doc's ledger."""
    try:
        path = efficiency_ledger(doc)
        if path is None:
            return 0
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return 0
    return sum(open_efficiency(line) for line in lines)


def read_counts(doc: Path, *, questions: int = 0) -> FooterCounts:
    """Each count for one production doc; a source that cannot be read counts nothing."""
    try:
        return FooterCounts(changes(doc), proposals(doc), questions, efficiency(doc))
    except OSError:
        return FooterCounts(questions=questions)


def sources(doc: Path) -> list[Path]:
    """The files the counts read, so a copy of them can tell when it is out of date.

    A proposal closes in place, so each open one is named beside its folder, which a new one
    changes.
    """
    try:
        listed = [item.path for item in change_review.scope_lists(change_review.scope_list(doc), doc)]
    except (change_review.ChangeReviewError, OSError):
        listed = []
    folder = proposals_folder.issues_folder(doc)
    try:
        queue = proposals_folder.ledger(doc)
        efficiency_queue = efficiency_ledger(doc)
    except OSError:
        queue = None
        efficiency_queue = None
    found = [doc, *listed, *(path for path in (folder, queue, efficiency_queue) if path is not None)]
    try:
        found.extend(issue.path for issue in proposals_folder.open_proposals(doc))
    except OSError:
        pass
    return found


def plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def counts_line(counts: FooterCounts) -> str | None:
    """`For you: 1 question, 2 changes, 130 efficiency, 2 proposals`.

    Questions come first because they need the user most (user, 2026-10-20). "efficiency" has
    no plural. Zero counts are left out; None when all are zero.
    """
    parts = [text for count, text in (
        (counts.questions, plural(counts.questions, "question")),
        (counts.changes, plural(counts.changes, "change")),
        (counts.efficiency, f"{counts.efficiency} efficiency"),
        (counts.proposals, plural(counts.proposals, "proposal"))) if count]
    return COUNTS_HEAD + ", ".join(parts) if parts else None
