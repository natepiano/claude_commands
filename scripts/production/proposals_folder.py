"""Where a production's proposal issues live and which of them are its own.

A leaf, so readers such as the footer counts import no cycle. Productions can share one issues
folder, so a production's own proposals are those its proposer field names: the production or
one of its units. A production below it answers for its own (user, 2026-10-27); `below` adds
each production below it at any depth, and its units.
"""
from __future__ import annotations

import re
from pathlib import Path

import showrunners
import showrunner_tree
from issues import Issue, open_issues
# Callers such as rename_unit read the tag here.
from issues import PROPOSAL_TAG as PROPOSAL_TAG
from production_units import Refusal, production_field, unit_table

PROPOSALS_FIELD = "Proposals"
IMPROVEMENT_QUEUE_FIELD = "Improvement queue"
PRODUCTION_SUFFIX = "-production.md"
# `types-unit (cargo-liner-style)`: a unit with the production it works in.
UNIT_IN_PRODUCTION = re.compile(r".+?\s*\((?P<production>[^()]+)\)")


def ledger(doc: Path) -> Path | None:
    """The ledger the doc's Improvement queue line names, or None when it names none."""
    try:
        return Path(production_field(doc.read_text(encoding="utf-8").splitlines(),
                                     IMPROVEMENT_QUEUE_FIELD)).expanduser()
    except Refusal:
        return None


def issues_folder(doc: Path) -> Path | None:
    """The folder the production doc's Proposals line names, or None when it names none."""
    try:
        lines = doc.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        lines = []
    try:
        folder = Path(production_field(lines, PROPOSALS_FIELD)).expanduser()
    except Refusal:
        return None
    # The doc lives at `<checkout>/docs/plans/<name>.md`; a relative folder is under the checkout.
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
    """The proposer names whose proposals are this production's: each production and unit its docs name."""
    names: set[str] = set()
    for production in scope_docs(doc, below):
        names.add(slug_of(production))
        try:
            rows = unit_table(production.read_text(encoding="utf-8").splitlines())
        except (OSError, UnicodeError, Refusal):
            continue
        names.update(row["Unit"] for row in rows if row.get("Unit"))
    return frozenset(names)


def proposer(issue: Issue) -> str:
    """The name that owns a proposal: the production of `unit (production)`, else the proposer field."""
    named = issue.value("proposed_by")
    found = UNIT_IN_PRODUCTION.fullmatch(named)
    return found["production"].strip() if found else named


def open_proposals(doc: Path, names: frozenset[str] | None = None) -> list[Issue]:
    """The open proposal issues in the production's folder that are its own, or that `names` own."""
    folder = issues_folder(doc)
    if folder is None or not folder.is_dir():
        return []
    names = owners(doc) if names is None else names
    return [issue for issue in open_issues(folder, PROPOSAL_TAG) if proposer(issue) in names]
