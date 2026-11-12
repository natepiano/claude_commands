"""Count the user's waiting items through the same staff routing as the change page."""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from scripts.production import change_review
from scripts.lib import conf
from scripts.production import change_routing, staff
from scripts.production import proposals_folder
from scripts.production import proposals_table
from scripts.production.production_doc import Refusal, production_field
from scripts.production.issues import source_paths

if TYPE_CHECKING:
    from scripts.production.update_registration import QuestionStaff

EFFICIENCY_LEDGER_FIELD = "Efficiency ledger"
COUNTS_HEAD = "For you: "
EFFICIENCY_LINE = re.compile(r"^- \d{4}-\d\d-\d\d (?P<head>.*?) — (?P<status>.*)$")


class FooterCounts(NamedTuple):
    changes: int = 0
    proposals: int = 0
    questions: int = 0
    efficiency: int = 0
    unrouted: int = 0


def total(each: list[FooterCounts]) -> FooterCounts:
    return FooterCounts(*(sum(column) for column in zip(FooterCounts(), *each)))


def change_counts(doc: Path, record: staff.StaffSnapshot, *, source: str = "",
                  sources: list[Path] | None = None) -> tuple[int, int]:
    """Count the production tab and the unrouted tab in one routed pass."""
    try:
        scope = change_review.review_scope(doc, False, record)
        listing = (change_review.scope_list(doc, record=record) if sources is None
                   else change_review.scope_list(doc, record=record, sources=sources))
        if sources is None:
            routed = change_review.routed_review_entries(listing, doc, record=record)
        else:
            routed = change_review.routed_review_entries(listing, doc, record=record, sources=sources)
        return (sum(owner.tab in scope and (not source or row.get("Producer") == source)
                    for _, owner, row in routed),
                0 if source else sum(isinstance(owner, change_routing.UnroutedChange) for _, owner, _ in routed))
    except OSError:
        if sources is not None:
            raise
        return 0, 0
    except change_review.ChangeReviewError:
        return 0, 0


def changes(doc: Path) -> int:
    return change_counts(doc, staff.snapshot())[0]


def unrouted_changes(doc: Path) -> int:
    """Count the same global unrouted tab as the change page."""
    return change_counts(doc, staff.snapshot())[1]


def proposals(doc: Path, *, source: str = "", sources: list[Path] | None = None) -> int:
    if source:
        waiting = proposals_table.issue_entries(
            [issue for issue in proposals_table.scope_issues(doc, sources=sources)
             if issue.value("proposed_by") == source], sources=sources)[0]
        return len(waiting)
    return len(proposals_table.waiting_entries(doc, sources=sources)[0])


def open_efficiency(line: str, *, source: str = "") -> bool:
    """Match an open ledger line and its source before the first status delimiter."""
    match = EFFICIENCY_LINE.match(line)
    return (match is not None and match.group("status").strip().startswith(("proposed", "deferred"))
            and (not source or match.group("head").rstrip().endswith(f" [by: {source}]")))


def efficiency_ledger(doc: Path) -> Path | None:
    """Read the configured efficiency ledger, or return None when it is unconfigured."""
    try:
        return Path(production_field(doc.read_text(encoding="utf-8").splitlines(), EFFICIENCY_LEDGER_FIELD)).expanduser()
    except Refusal:
        return None


def efficiency(doc: Path, *, source: str = "", sources: list[Path] | None = None) -> int:
    """Proposed and deferred efficiency proposals in the doc's ledger."""
    try:
        path = efficiency_ledger(doc)
    except (OSError, UnicodeError):
        if sources is not None:
            raise
        return 0
    try:
        if path is None:
            return 0
        if sources is not None:
            sources.append(path)
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return 0
    except (OSError, UnicodeError):
        if sources is not None:
            raise
        return 0
    return sum(open_efficiency(line, source=source) for line in lines)


def read_counts(doc: Path, *, questions: int = 0, record: staff.StaffSnapshot | None = None,
                source: str = "", sources: list[Path] | None = None) -> FooterCounts:
    """Read counts. A source collector requires complete reads; other callers retain available counts."""
    try:
        change_count, unrouted_count = change_counts(
            doc, record if record is not None else staff.snapshot(), source=source, sources=sources)
        return FooterCounts(change_count, proposals(doc, source=source, sources=sources), questions,
                            efficiency(doc, source=source, sources=sources), unrouted_count)
    except OSError:
        if sources is not None:
            raise
        return FooterCounts(questions=questions)


def routing_sources(record: staff.StaffSnapshot | None = None, *, sources: list[Path] | None = None) -> list[Path]:
    """Watch configuration, staff, review lists, and delivery records for count changes."""
    root = change_routing.review_root() if sources is None else change_routing.review_root(sources=sources)
    record = record if record is not None else staff.snapshot()
    lists = change_review.lists_directory() if sources is None else change_review.lists_directory(sources=sources)
    return [conf.path("change_review"), conf.path("review_links"), lists,
            *source_paths(lists, change_review.PREFIX + "*.md", sources=sources),
            *(Path(path) for path in record.inputs), root,
            *source_paths(root, ".review-link-delivery-*", sources=sources)]


def sources(doc: Path, *, identities: QuestionStaff | None = None,
            read_sources: list[Path] | None = None) -> list[Path]:
    """Watch count sources. A supplied collector retains paths and requires complete reads."""
    from scripts.production.update_registration import (QuestionFolder, QuestionStaff, RegistrationFailure,
                                     question_folder, question_records_for_document)

    found = [] if read_sources is None else read_sources
    found.append(doc)
    try:
        if identities is None:
            identities = QuestionStaff.read() if read_sources is None else QuestionStaff.read(sources=read_sources)
        listing = (change_review.scope_list(doc, record=identities.record) if read_sources is None
                   else change_review.scope_list(doc, record=identities.record, sources=read_sources))
        listed = [item.path for item in change_review.scope_lists(
            listing, doc, record=identities.record, sources=read_sources)]
    except change_review.ChangeReviewError:
        listed = []
    except (staff.StaffRefused, OSError) as error:
        if read_sources is not None:
            raise ValueError(str(error)) from error
        listed = []
    found.extend(listed)
    folder = proposals_folder.issues_folder(doc)
    if folder is not None:
        found.append(folder)
    queue = proposals_folder.ledger(doc)
    if queue is not None:
        found.append(queue)
    try:
        efficiency_queue = efficiency_ledger(doc)
    except OSError:
        if read_sources is not None:
            raise
        efficiency_queue = None
    if efficiency_queue is not None:
        found.append(efficiency_queue)
    if folder is not None:
        try:
            found.extend(issue.path for issue in proposals_folder.open_proposals(doc, sources=read_sources))
        except OSError:
            if read_sources is not None:
                raise
    try:
        if identities is None:
            identities = QuestionStaff.read() if read_sources is None else QuestionStaff.read(sources=read_sources)
        questions = question_folder(doc, identities)
        if isinstance(questions, QuestionFolder):
            if questions.path not in found:
                found.append(questions.path)
            found.extend(record.path for record in question_records_for_document(doc, identities, sources=read_sources)
                         if record.status == "open")
    except (staff.StaffRefused, RegistrationFailure, OSError) as error:
        if read_sources is not None:
            raise ValueError(str(error)) from error
        print(f"questions: unavailable — {error}", file=sys.stderr)
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
