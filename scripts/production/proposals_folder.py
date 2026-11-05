"""Find proposal stores and join their proposers to the production's staff members."""
from __future__ import annotations

from pathlib import Path


import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.production import showrunners, staff, unit_lookup
from scripts.production import showrunner_tree
from scripts.production.issues import Issue, open_issues
# Callers such as rename_unit read the tag here.
from scripts.production.issues import PROPOSAL_TAG as PROPOSAL_TAG
from scripts.production.production_doc import Refusal, production_field

PROPOSALS_FIELD = "Proposals"
IMPROVEMENT_QUEUE_FIELD = "Improvement queue"
PRODUCTION_SUFFIX = "-production.md"


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


def slug_of(doc: Path) -> str:
    """The production's slug: its doc's name without `-production.md`, or without `.md` for another doc."""
    return doc.name.removesuffix(PRODUCTION_SUFFIX) if doc.name.endswith(PRODUCTION_SUFFIX) else doc.stem


def owner_docs(doc: Path) -> list[Path]:
    """This production's doc, then the doc of each production below it, the nearest level first."""
    if not doc.name.endswith(PRODUCTION_SUFFIX):
        return [doc]
    slug = slug_of(doc)
    docs = showrunners.timer_docs()
    below = (docs[child] for child in showrunner_tree.descendants_of(slug) if child in docs)
    return [doc, *below]


def scope_docs(doc: Path, below: bool = False) -> list[Path]:
    """The docs whose items the production's showrunner shows: its own, and with `below` each one below it."""
    return owner_docs(doc) if below else [doc]


def owners(doc: Path, below: bool = False) -> frozenset[str]:
    """Read the fixed ids of each production and its units."""
    record = staff.snapshot()
    identities: set[str] = set()
    for production in scope_docs(doc, below):
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
