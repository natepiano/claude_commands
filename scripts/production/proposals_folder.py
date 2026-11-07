"""Find proposal stores and join their proposers to the production's staff members."""
from __future__ import annotations

from pathlib import Path


from scripts.production import staff, unit_lookup
from scripts.production import showrunner_tree
from scripts.production.issues import Issue, open_issues
# Callers such as rename read the tag here.
from scripts.production.issues import PROPOSAL_TAG as PROPOSAL_TAG
from scripts.production.production_doc import Refusal, production_field

PROPOSALS_FIELD = "Proposals"
IMPROVEMENT_QUEUE_FIELD = "Improvement queue"


def ledger(doc: Path) -> Path | None:
    """Return the configured ledger, or None when the document has no ledger field."""
    try:
        lines = doc.read_text(encoding="utf-8").splitlines()
    except UnicodeError as error:
        raise OSError(f"{doc}: {error}") from error
    try:
        return Path(production_field(lines, IMPROVEMENT_QUEUE_FIELD)).expanduser()
    except Refusal:
        return None


def issues_folder(doc: Path) -> Path | None:
    """Return the configured folder, or None when the document has no proposals field."""
    try:
        lines = doc.read_text(encoding="utf-8").splitlines()
    except UnicodeError as error:
        raise OSError(f"{doc}: {error}") from error
    try:
        folder = Path(production_field(lines, PROPOSALS_FIELD)).expanduser()
    except Refusal:
        return None
    # A relative folder starts at the checkout that contains docs/plans.
    return folder if folder.is_absolute() else doc.parent.parent.parent / folder


def owner_docs(doc: Path, *, record: staff.StaffSnapshot | None = None) -> list[Path]:
    """Return this owner's document and the documents of its descendants."""
    record = staff.snapshot() if record is None else record
    owner = unit_lookup.doc_owner(doc, record)
    docs = {row.name: row.doc.path for row in record.find(kind="showrunner")
            if isinstance(row.doc, (staff.LiveDoc, staff.WrappedDoc))}
    return [doc, *(docs[child] for child in showrunner_tree.descendants_of(owner.name, record=record) if child in docs)]


def scope_docs(doc: Path, below: bool = False, *,
               record: staff.StaffSnapshot | None = None) -> list[Path]:
    """Return the production document and the selected descendant documents."""
    return owner_docs(doc, record=record) if below else [doc]


def owners(doc: Path, below: bool = False) -> frozenset[str]:
    """Read the fixed ids of each production and its units."""
    record = staff.snapshot()
    identities: set[str] = set()
    for production in scope_docs(doc, below, record=record):
        owner = unit_lookup.doc_owner(production, record)
        identities.add(owner.id)
        identities.update(row.id for row in record.find(kind="unit", parent=owner.id))
    return frozenset(identities)


def proposer(issue: Issue) -> str:
    """Read the proposer's fixed staff id."""
    return issue.value("proposed_by")


def proposal_issues(doc: Path) -> list[Issue]:
    """The open proposal issues in the production's folder, of any proposer."""
    folder = issues_folder(doc)
    if folder is None or not folder.is_dir():
        return []
    return [issue for issue in open_issues(folder, PROPOSAL_TAG) if issue.value("record_type") != "question"]


def open_proposals(doc: Path) -> list[Issue]:
    """The open proposal issues in the production's folder that are its own. Staff is read only when issues exist."""
    found = proposal_issues(doc)
    names = owners(doc) if found else frozenset[str]()
    return [issue for issue in found if proposer(issue) in names]
