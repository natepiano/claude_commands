"""Count the user's waiting items through the same staff routing as the change page."""
from __future__ import annotations

import re
import os
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

import change_review
from scripts.production import change_routing, showrunners, staff
import proposals_folder
import proposals_table
from production_units import Refusal, production_field

if TYPE_CHECKING:
    from update_registration import QuestionStaff

EFFICIENCY_LEDGER_FIELD = "Efficiency ledger"
COUNTS_HEAD = "For you: "
EFFICIENCY_LINE = re.compile(r"^- \d{4}-\d\d-\d\d .*? — (?P<status>.*)$")


class FooterCounts(NamedTuple):
    changes: int = 0
    proposals: int = 0
    questions: int = 0
    efficiency: int = 0
    unrouted: int = 0


def total(each: list[FooterCounts]) -> FooterCounts:
    return FooterCounts(*(sum(column) for column in zip(FooterCounts(), *each)))


def changes(doc: Path) -> int:
    try:
        root = Path(os.environ.get("REVIEW_LINKS_ROOT") or Path.home() / ".local/share/review-links").expanduser()
        return sum(change_routing.waits_for_user(entry.review_list.path, entry.entry.item, root)
                   for entry in change_review.scoped_review_entries(change_review.scope_list(doc), doc))
    except (change_review.ChangeReviewError, OSError):
        return 0


def unrouted_changes(doc: Path) -> int:
    """Count the same global unrouted tab as the change page."""
    try:
        current = change_review.scope_list(doc)
        root = Path(os.environ.get("REVIEW_LINKS_ROOT") or Path.home() / ".local/share/review-links").expanduser()
        return sum(isinstance(owner, change_routing.UnroutedChange)
                   and change_routing.waits_for_user(item.path, row, root)
                   for item, owner, row in change_review.routed_review_entries(current, doc))
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
        return FooterCounts(changes(doc), proposals(doc), questions, efficiency(doc), unrouted_changes(doc))
    except OSError:
        return FooterCounts(questions=questions)


def routing_sources() -> list[Path]:
    """Watch staff, all review lists, and delivery records for tab count changes."""
    root = Path(os.environ.get("REVIEW_LINKS_ROOT") or Path.home() / ".local/share/review-links").expanduser()
    record = staff.snapshot()
    routing = change_routing.StaffRouting(record, showrunners.timer_docs(), change_review.ReviewHistoryReader())
    return [*(source for repo in routing.repository_choices({}, "") for source in change_routing.history_sources(repo)),
            change_review.LISTS, *change_review.LISTS.glob(change_review.PREFIX + "*.md"),
            *(Path(path) for path in record.inputs), root, *root.glob(".review-link-delivery-*")]


def sources(doc: Path, *, identities: QuestionStaff | None = None) -> list[Path]:
    """Return the files that affect the counts and their containing folders.

    Watch each open record because closing it changes the file.
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
    if folder is not None:
        try:
            found.extend(issue.path for issue in proposals_folder.open_proposals(doc))
        except OSError:
            pass
    from update_registration import QuestionFolder, QuestionStaff, question_folder, question_records_for_document

    try:
        if identities is None:
            identities = QuestionStaff.read()
        questions = question_folder(doc, identities)
        if isinstance(questions, QuestionFolder):
            if questions.path not in found:
                found.append(questions.path)
            found.extend(record.path for record in question_records_for_document(doc, identities)
                         if record.status == "open")
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
        (counts.proposals, plural(counts.proposals, "proposal")),
        (counts.unrouted, f"{counts.unrouted} unrouted")) if count]
    return COUNTS_HEAD + ", ".join(parts) if parts else None
