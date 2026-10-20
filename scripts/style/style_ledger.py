#!/usr/bin/env python3
"""Store style judgments and derive coverage at a repository's synced tip."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import tomllib
import uuid
from collections import Counter
from collections.abc import Callable, Generator, Iterable, Iterator, Mapping
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path, PurePosixPath
from types import TracebackType
from typing import Literal, TypedDict, cast

import rules as rule_catalog
import style_footprint
import style_rules
from approved_renames import AppliedCheck, AppliedInCode, Folder, MissingFile, NoAppliedCheck, RenameNotApplied, applied_check
from candidate_generators import enumerate_candidates
from declarations import Declaration, DeclaredFile, DeclaredItem, ImplOrTraitItem, moved_site_line, read_declarations
from rules import Family, InputScope, MemberReach, Rule, Tier

CellState = Literal["current", "stale", "open", "awaiting merge"]
Verdict = Literal["clean", "fixed", "exception", "held", "waived"]
LintTool = Literal["clippy", "mend", "rustfmt"]
# How long every ledger connection waits for another writer's lock before it fails.
BUSY_TIMEOUT_SECONDS = 30


class DeclaredInputScope(StrEnum):
    RULE = "rule"
    FILE = "file"


@dataclass(frozen=True)
class UndeclaredInputScope:
    """The judge gave no input scope; a cell that allows file scope refuses a scoped verdict."""


UNDECLARED_INPUT_SCOPE = UndeclaredInputScope()
SCOPED_VERDICTS: tuple[Verdict, ...] = ("clean", "exception", "held")


class CellInputScope(StrEnum):
    FILE = "file"
    CRATE = "crate"
    MANIFEST = "manifest"
    MODULE = "module"


def _comparison_scope(declared: DeclaredInputScope, scope: CellInputScope) -> CellInputScope:
    return CellInputScope.FILE if declared == DeclaredInputScope.FILE else scope


def _cell_scope(rule: Rule, path: str, tip: RepositoryTip) -> CellInputScope:
    if (rule.tier == "D" and path.endswith(".rs") and path != tip.crate(path)
            and rule.input_scope != "manifest"):
        return CellInputScope.MODULE
    if rule.tier == "D" and rule.input_scope == "file" and path == tip.crate(path):
        return CellInputScope.CRATE
    return CellInputScope(rule.input_scope)


def _judgment_rule(component: Rule, path: str) -> Rule:
    """A listed rule's cell at a file that is not Rust takes the file's own inputs."""
    return replace(component, input_scope="file") if component.tier == "B" and not path.endswith(".rs") else component


class LedgerError(ValueError):
    """A ledger operation cannot represent the requested check."""


class CheckCouldNotRun(LedgerError):
    """A git command failed or the ledger schema is newer than this script; the request is not at fault."""


class ChangedBeforeLock(LedgerError):
    """Rows a writer read before its write lock changed before it took the lock."""


# How many times a writer reads, then takes the write lock, before rows that keep changing refuse.
LOCK_ATTEMPTS = 3


def retry_on_change[T](attempt: Callable[[], T]) -> T:
    """Run attempt again while the rows it read change before its write lock; the last change refuses."""
    for _ in range(LOCK_ATTEMPTS - 1):
        try:
            return attempt()
        except ChangedBeforeLock:
            continue
    return attempt()


@dataclass(frozen=True)
class SyncWait:
    unit: str
    since: str
    token: str


@dataclass(frozen=True)
class Cell:
    rule: str
    path: str
    crate: str
    tier: Tier
    family: Family | None
    state: CellState


@dataclass(frozen=True)
class ListedSite:
    rule: str
    path: str
    crate: str
    line: int
    text: str


@dataclass(frozen=True)
class RuleCounts:
    rule_id: str
    tier: Tier
    current: int
    stale: int
    open: int
    awaiting_merge: int
    held: int
    input_scope: InputScope

    @property
    def total(self) -> int:
        return self.current + self.stale + self.open + self.awaiting_merge

    @property
    def complete(self) -> bool:
        return self.current == self.total


@dataclass(frozen=True)
class WholeRepository:
    """Coverage across every crate in the repository."""


@dataclass(frozen=True)
class CrateCoverage:
    crate: str


CoverageScope = WholeRepository | CrateCoverage
_WHOLE_REPOSITORY = WholeRepository()


def _finding_cell(definition: Rule, path: str, tip: RepositoryTip) -> str:
    """The judgment cell of a finding's site: its crate for a manifest structure rule, else its file."""
    return tip.crate(path) if definition.tier == "D" and definition.input_scope == "manifest" else path


def _crate_filter(scope: CoverageScope) -> str | None:
    return scope.crate if isinstance(scope, CrateCoverage) else None


FindingKind = Literal["rename", "type", "restructure"]
HeldStateName = Literal["unfiled", "filed", "waived", "built", "withdrawn"]
TicketKind = Literal["read", "listed", "structure", "proposal"]
TicketState = Literal["queued", "assigned", "checkpointed", "merged", "dropped"]
# A ticket in one of these states never changes again.
FINAL_TICKET_STATES: frozenset[TicketState] = frozenset({"merged", "dropped"})


@dataclass(frozen=True)
class Unfiled:
    """The finding has no proposal yet."""


@dataclass(frozen=True)
class Filed:
    proposal: Path


@dataclass(frozen=True)
class Waived:
    """The user has waived the finding."""


@dataclass(frozen=True)
class Built:
    """The approved finding has merged into the style branch. proposal is the decision that approved it."""

    proposal: Path


@dataclass(frozen=True)
class ExceptionVerdict:
    """A judgment under the rule's newer text found the finding's site an exception."""

    judgment_id: int


@dataclass(frozen=True)
class DuplicateOf:
    """The finding is a bare rename that a qualified rename finding of the same rule and file already holds.

    The bare finding's before and after are the last `::` segments of the qualified finding's before and after.
    """

    finding_id: str


@dataclass(frozen=True)
class DeclarationGone:
    """No file at the synced commit declares the finding's subject any more."""

    commit: str


WithdrawnBy = ExceptionVerdict | DuplicateOf | DeclarationGone


def withdrawn_line(by: WithdrawnBy) -> str:
    match by:
        case ExceptionVerdict():
            return f"judgment {by.judgment_id} exception"
        case DuplicateOf():
            return f"duplicate of {by.finding_id}"
        case DeclarationGone():
            return f"declaration gone at {by.commit[:7]}"


@dataclass(frozen=True)
class NoDecision:
    """The finding was withdrawn while unfiled, before `file` put it in a decision."""


@dataclass(frozen=True)
class Withdrawn:
    """The finding left its decision, for the reason in by."""

    proposal: Path | NoDecision
    by: WithdrawnBy


HeldState = Unfiled | Filed | Waived | Built | Withdrawn


def held_state_name(state: HeldState) -> HeldStateName:
    match state:
        case Unfiled():
            return "unfiled"
        case Filed():
            return "filed"
        case Waived():
            return "waived"
        case Built():
            return "built"
        case Withdrawn():
            return "withdrawn"


@dataclass(frozen=True)
class AllHeldFindings:
    """Findings in every state."""


@dataclass(frozen=True)
class HeldFindingsInState:
    state: HeldStateName


@dataclass(frozen=True)
class UnfiledFindingsFromOpenTickets:
    """Findings still subject to revision by an assigned or checkpointed ticket."""


@dataclass(frozen=True)
class ApprovedProposal:
    issue: Path
    unit: str
    finding_ids: tuple[str, ...]


@dataclass(frozen=True)
class SelectedByCoverage:
    """The ticket requires the mechanical change filter against its saved base."""

    base: str


@dataclass(frozen=True)
class HeldJudgment:
    rule: str
    path: str
    commit: str
    ticket: str
    worker: str
    rule_blob: str


@dataclass(frozen=True)
class CellWaived:
    judgment: HeldJudgment


@dataclass(frozen=True)
class CellStillHeld:
    judgment: HeldJudgment


@dataclass(frozen=True)
class InputChangedSinceJudged:
    """The held judgment no longer describes the current input."""


@dataclass(frozen=True)
class NoHeldJudgment:
    """There is no held judgment for this finding's cell."""


@dataclass(frozen=True)
class UnknownRule:
    """The finding's rule is not in the catalog, so no judgment cell can be named for it."""


@dataclass(frozen=True)
class NewestHeldJudgment:
    """A cell's newest held judgment as a decline reads it, with the id of the cell's newest judgment of any verdict."""

    judgment_id: int
    judgment: HeldJudgment
    inputs: Judgment
    cell_newest_id: int


# What declining one finding did to the held judgment of its cell.
DeclineOutcome = CellWaived | CellStillHeld | InputChangedSinceJudged | NoHeldJudgment


@dataclass(frozen=True)
class NoBuildTicket:
    """No ticket holds this finding for a build."""


@dataclass(frozen=True)
class BuildTicket:
    ticket: str


@dataclass(frozen=True)
class NotReadByPerson:
    """No person read the code for the built finding; the applied check decides whether its change is in."""


@dataclass(frozen=True)
class ReadByPerson:
    """A person read the code for the built finding in place of the applied check, and reopen keeps that verdict.

    The state shows the verdict: filed again when the change is not in the code, built when it is. A new build
    clears it, because the person read the old build.
    """

    by: str


PersonReading = NotReadByPerson | ReadByPerson


def reading_line(state: HeldState, reading: ReadByPerson) -> str:
    match state:
        case Filed():
            verdict = "filed again by"
        case Built():
            verdict = "applied, read by"
        case _:
            verdict = "read by"
    return f"{verdict} {reading.by}: a person read the code"


@dataclass(frozen=True)
class NoWaiveUndo:
    """No person undid a waive of the finding: it was never waived, or its waive stands."""


@dataclass(frozen=True)
class WaiveUndoneBy:
    """A person undid the finding's waive, so it went back to unfiled.

    The record stays after every later change of the finding, a new waive included, so a later `file` never erases it.
    """

    by: str


WaiveUndo = NoWaiveUndo | WaiveUndoneBy


@dataclass(frozen=True)
class NotFollowed:
    """The finding is at the path and line it was judged at."""


@dataclass(frozen=True)
class FollowedDeclaration:
    """The finding's declaration moved to another file, and the finding followed it at commit. The record is the last move."""

    from_path: str
    from_line: int
    commit: str


FollowState = NotFollowed | FollowedDeclaration


def followed_line(follow: FollowedDeclaration) -> str:
    return f"followed from {follow.from_path}:{follow.from_line} at {follow.commit[:7]}"


@dataclass(frozen=True)
class RuleCandidate:
    ticket: str
    unit: str
    path: str
    line: int
    idea: str
    created_at: str


@dataclass(frozen=True)
class HeldFinding:
    finding_id: str
    rule: str
    path: str
    line: int
    text: str
    kind: FindingKind
    before: str
    after: str
    rule_blob: str
    ticket: str
    state: HeldState
    build_ticket: NoBuildTicket | BuildTicket
    reading: PersonReading
    waive_undo: WaiveUndo
    follow: FollowState


def rule_changed_since_judged(finding: HeldFinding, definitions: Mapping[str, Rule]) -> bool:
    """Whether the catalog has no rule for the finding, or the rule's text is not the text the finding was judged under."""
    definition = definitions.get(finding.rule.removesuffix("#uncovered"))
    return definition is None or definition.blob != finding.rule_blob


@dataclass(frozen=True)
class JudgedCommit:
    """The commit a finding was judged at: its judging ticket's checkpoint, or the ticket's start before one."""

    commit: str


@dataclass(frozen=True)
class NoJudgedCommit:
    """The finding's judging ticket is not in the ledger, or names no commit in the repo."""


@dataclass(frozen=True)
class BareDuplicate:
    """A bare rename finding that leaves its decision, the qualified finding it duplicates, and that decision."""

    finding_id: str
    qualified: str
    decision: Path


@dataclass(frozen=True)
class SupersedingException:
    """The judgment with the verdict exception under the rule's current text that is newest at a finding's cell, also newer than its own."""

    judgment_id: int
    rule_blob: str


@dataclass(frozen=True)
class WithdrawnFinding:
    finding: HeldFinding
    verdict: SupersedingException


@dataclass(frozen=True)
class KeptForBuild:
    """A finding the newer verdict would withdraw, kept because a ticket that is not final builds it."""

    finding: HeldFinding
    verdict: SupersedingException
    ticket: str


@dataclass(frozen=True)
class Withdrawal:
    """The filed findings that an exception under their rule's newer text withdraws, and those an open build keeps."""

    withdrawn: tuple[WithdrawnFinding, ...]
    kept: tuple[KeptForBuild, ...]


@dataclass(frozen=True)
class DeclarationNotRead:
    """The step does not read the finding: not a `.rs` path, no judged commit, a read commit outside the synced
    tip, no named item at its line, or an unfiled finding of a ticket that is still open."""

    finding: HeldFinding


@dataclass(frozen=True)
class DeclarationInPlace:
    """The finding's file did not change since its read commit, or still declares its subject at the synced tip."""

    finding: HeldFinding


@dataclass(frozen=True)
class DeclarationMoved:
    """One other file declares the finding's subject at the synced tip, or a built rename of an approved decision renamed
    the subject or its impl or trait and one file gained the new declaration; the finding follows it to path and line."""

    finding: HeldFinding
    declaration: Declaration
    path: str
    line: int


@dataclass(frozen=True)
class DeclarationRemoved:
    """No file declares the finding's subject at the synced tip. The finding is unfiled, or its file is gone there, so it is withdrawn."""

    finding: HeldFinding
    declaration: Declaration


@dataclass(frozen=True)
class DeclaredInSeveralFiles:
    """Two or more other files declare the finding's subject; a person picks, and the finding stays as it is."""

    finding: HeldFinding
    declaration: Declaration
    paths: tuple[str, ...]


@dataclass(frozen=True)
class DeclaredInAnotherCrate:
    """The subject of a filed finding moved to a file in another crate. Its decision is per crate, so it stays."""

    finding: HeldFinding
    declaration: Declaration
    path: str


@dataclass(frozen=True)
class DeclarationRenamed:
    """A filed rename finding whose own change is in the code. Its declaration is gone, and its after name is declared
    with the same keyword and owner, or the owner's name after the built renames, in its file or in the one file that
    gained it. The finding is built there."""

    finding: HeldFinding
    declaration: Declaration
    renamed: Declaration
    path: str
    line: int


@dataclass(frozen=True)
class RenameAwaitingApproval:
    """A filed rename whose change is in the code, as DeclarationRenamed reads it, in a decision the user has not
    approved. Built means approved and in the code, so the finding stays filed, with no line, until the answer."""

    finding: HeldFinding


@dataclass(frozen=True)
class DeclarationGoneFromFile:
    """No file declares a filed finding's subject at the synced tip, and its file is still there. The finding stays
    filed until a person waives it or declares the item again."""

    finding: HeldFinding
    declaration: Declaration


@dataclass(frozen=True)
class NoRenamedSite:
    """The finding is not a filed rename of its own declaration, no file gained its after name with the same keyword
    and owner, or the applied check finds the rename not applied there."""


@dataclass(frozen=True)
class UnqualifiedName:
    """A rename's before is one name, or a type finding's item head: it can name any item of that name."""


@dataclass(frozen=True)
class ModuleQualified:
    """A rename's before is a path whose last qualifier is a module (it does not start upper case): it names a module
    item of a file of that module."""

    module: str


@dataclass(frozen=True)
class SelfQualified:
    """A rename's before is a `Self::` path: its owner must be read at the built finding's line."""


@dataclass(frozen=True)
class TypeQualified:
    """A rename's before is a path whose last qualifier is a type or trait: it names an item of that impl or trait, or a
    variant of that enum."""

    type_name: str


RenameScope = UnqualifiedName | ModuleQualified | SelfQualified | TypeQualified


@dataclass(frozen=True)
class ItemRename:
    """The old and new name of one item, as a built rename or type finding gives them, and the items its old name can
    name."""

    old: str
    new: str
    scope: RenameScope


@dataclass(frozen=True)
class NoItemRename:
    """The finding does not rename one item: its before and after are not one name pair, its qualifier is not one name,
    or a type finding changes more than one name."""


@dataclass(frozen=True)
class BuiltRename:
    """The item rename of one built finding, with the finding's path and decision."""

    rename: ItemRename
    path: str
    proposal: Path


@dataclass(frozen=True)
class RenamedByBuilds:
    """The finding's declaration with the new names that built renames of approved decisions gave its item, the impl
    or trait that holds it, or both."""

    declaration: Declaration


@dataclass(frozen=True)
class NoBuiltRename:
    """No built rename of an approved decision gives the finding's item or its impl or trait a new name."""


BuiltRenaming = RenamedByBuilds | NoBuiltRename


def _module_name(path: str) -> str:
    """The file's module: crate for src/lib.rs and src/main.rs, its directory for mod.rs or other lib.rs and main.rs,
    else its stem."""
    file = PurePosixPath(path)
    if file.parent.name == "src" and file.stem in ("lib", "main"):
        return "crate"
    return file.parent.name if file.stem in ("mod", "lib", "main") else file.stem


# A type finding's item head: its keyword, its name and the rest of the item.
_ITEM_HEAD = re.compile(rf"(fn|struct|enum|union|trait|type|const|static|mod)\s+({style_footprint.IDENTIFIER.pattern})(.*)", re.DOTALL)


def _item_rename(kind: str, before: str, after: str) -> ItemRename | NoItemRename:
    """The item rename a finding names.

    A rename gives the last path segment of before and of after; a note after `;` in after is not part of the name.
    The segment before the old name sets the scope: none, `Self`, a type or trait (upper case) or a module. A type
    finding gives the item name when only that name changes.
    A field form such as `Owner.field` gives none.
    """
    if kind == "rename":
        *qualifier, old = before.strip().split("::")
        new = after.split(";", 1)[0].strip().split("::")[-1]
        if old == new or not style_footprint.IDENTIFIER.fullmatch(old) or not style_footprint.IDENTIFIER.fullmatch(new):
            return NoItemRename()
        if not qualifier:
            return ItemRename(old, new, UnqualifiedName())
        if not style_footprint.IDENTIFIER.fullmatch(qualifier[-1]):
            return NoItemRename()
        if qualifier[-1] == "Self":
            return ItemRename(old, new, SelfQualified())
        if qualifier[-1].removeprefix("r#")[0].isupper():
            return ItemRename(old, new, TypeQualified(qualifier[-1]))
        return ItemRename(old, new, ModuleQualified(qualifier[-1].removeprefix("r#")))
    if kind == "type":
        old_head = _ITEM_HEAD.fullmatch(before.strip())
        new_head = _ITEM_HEAD.fullmatch(after.strip())
        if (old_head and new_head and old_head[1] == new_head[1] and old_head[3] == new_head[3]
                and old_head[2] != new_head[2]):
            return ItemRename(old_head[2], new_head[2], UnqualifiedName())
    return NoItemRename()


@dataclass(frozen=True)
class KeptForOpenBuild:
    """A finding whose declaration is gone from its file, kept because a ticket that is not final builds it.

    A declaration that moved to several files or to another crate is left instead.
    """

    finding: HeldFinding
    ticket: str


DeclarationOutcome = (DeclarationNotRead | DeclarationInPlace | DeclarationMoved | DeclarationRemoved
                      | DeclaredInSeveralFiles | DeclaredInAnotherCrate | DeclarationRenamed | RenameAwaitingApproval
                      | DeclarationGoneFromFile | KeptForOpenBuild)
# The outcomes that leave a finding as it is for a person to waive or fix in the code.
DeclarationLeft = DeclaredInSeveralFiles | DeclaredInAnotherCrate | DeclarationGoneFromFile


@dataclass(frozen=True)
class DeclarationReading:
    """Where each finding's declaration is at the synced tip, one outcome per finding read, in the order given."""

    tip: str
    outcomes: tuple[DeclarationOutcome, ...]


@dataclass(frozen=True)
class FindingNotBuilt:
    """A merged ticket's finding that stays filed for a later ticket."""

    finding_id: str
    cause: RenameNotApplied | NoAppliedCheck


@dataclass(frozen=True)
class BuiltAtMerge:
    """A filed finding that a plain merge built, and the person who read the code when the applied check could not."""

    finding_id: str
    merge: str
    reading: PersonReading


def built_at_merge_line(item: BuiltAtMerge) -> str:
    reader = f", read by {item.reading.by}" if isinstance(item.reading, ReadByPerson) else ""
    return f"built {item.finding_id} at {item.merge[:7]}{reader}"


@dataclass(frozen=True)
class TicketJudgment:
    """The latest judgment a ticket recorded for one cell."""

    judgment_id: int
    rule: str
    path: str
    commit: str
    declared_scope: DeclaredInputScope


@dataclass(frozen=True)
class CarriedVerdict:
    """A ticket verdict to restamp at the checkpoint, with the input it has there."""

    judgment_id: int
    scope: CellInputScope
    fingerprint: str


@dataclass(frozen=True)
class CheckpointRestamps:
    """The ticket's latest judgments as read before the write lock, and the verdicts they carry to the checkpoint."""

    latest: tuple[TicketJudgment, ...]
    carried: tuple[CarriedVerdict, ...]


@dataclass(frozen=True)
class NoRestamps:
    """The move carries no verdict: it reaches no checkpoint, or the ticket's start is not under it."""


@dataclass(frozen=True)
class StyleMergeChanges:
    """A fast-forward of the unit branch to the pushed style tip changes the inputs of these judged cells."""

    style_branch: str
    cells: tuple[Cell, ...]


@dataclass(frozen=True)
class NoStyleMergeChange:
    """No style branch is stored, the unit tip is not in the pushed style tip, or that tip has each judged input."""


StyleMerge = StyleMergeChanges | NoStyleMergeChange


@dataclass(frozen=True)
class StoredStyleBranch:
    """The style merge branch the last sync stored."""

    name: str


@dataclass(frozen=True)
class NoStoredStyleBranch:
    """No sync stored a style merge branch."""


StyleBranch = StoredStyleBranch | NoStoredStyleBranch


def not_built_line(repo: Path, item: FindingNotBuilt) -> str:
    if isinstance(item.cause, RenameNotApplied):
        return f"not built {item.finding_id}: rename not applied in {item.cause.path}"
    return (f"not built {item.finding_id}: {item.cause.kind} has no applied check; mark it built with "
            + f"style_ledger.py held built --repo {repo} --id {item.finding_id} after checking")


@dataclass(frozen=True)
class NoPendingHandoffs:
    """Every hand-off has reached the branch point."""


@dataclass(frozen=True)
class PendingHandoffs:
    count: int
    oldest_pending_since: datetime


class SummaryJSON(TypedDict):
    rules_done: int
    rules_total: int
    unclassified_rules: int
    cells_current_pct: float
    stale: int
    proposals: int
    waiting_to_merge_back: int
    oldest_waiting: str | None
    synced_at: str
    synced_tip: str


@dataclass(frozen=True)
class StyleLedgerSummary:
    rules_done: int
    rules_total: int
    unclassified_rules: int
    cells_current_pct: float
    stale: int
    proposals: int
    waiting: PendingHandoffs | NoPendingHandoffs
    synced_at: datetime
    synced_tip: str

    def to_json(self) -> SummaryJSON:
        return {
            "rules_done": self.rules_done, "rules_total": self.rules_total,
            "unclassified_rules": self.unclassified_rules, "cells_current_pct": self.cells_current_pct,
            "stale": self.stale, "proposals": self.proposals,
            "waiting_to_merge_back": self.waiting.count if isinstance(self.waiting, PendingHandoffs) else 0,
            "oldest_waiting": self.waiting.oldest_pending_since.isoformat() if isinstance(self.waiting, PendingHandoffs) else None,
            "synced_at": self.synced_at.isoformat(), "synced_tip": self.synced_tip,
        }


class TicketRecord(TypedDict):
    id: str
    unit: str
    kind: TicketKind
    family: str
    rule: str
    crate: str
    files: list[str]
    cells: list[list[str]]
    sites: list[dict[str, object]]
    rules: list[str]
    rule_blobs: dict[str, str]
    state: TicketState
    branch: str
    base: str
    proposal: str
    finding_ids: list[str]
    checkpoint: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class UnjudgedTicketCell:
    rule: str
    path: str


@dataclass(frozen=True)
class ProposalCell:
    """A rule and file cell of a proposal ticket that record takes."""

    rule: str
    path: str


@dataclass(frozen=True)
class PinnedRuleText:
    rule_id: str
    blob: str
    text: bytes


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Judgment:
    rule_blob: str
    input_fingerprint: str
    commit: str
    verdict: Verdict
    input_scope: CellInputScope
    declared_scope: DeclaredInputScope


@dataclass(frozen=True)
class LintCoverage:
    has_runs: bool
    passed: bool
    diagnostics: dict[str, int]
    rule_blobs: dict[str, str]


@dataclass(frozen=True)
class RepositoryTip:
    commit: str
    tree: str
    blobs: dict[str, str]
    # Deepest first, so the first that holds a path is its innermost crate.
    crate_dirs: tuple[str, ...]
    rust_files: tuple[str, ...]

    def crate(self, path: str) -> str:
        """The innermost crate directory that holds path, as a decision's crate is."""
        return next((directory for directory in self.crate_dirs if _under(path, directory)), ".")


def _git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments], capture_output=True, text=True, check=False,
    )
    if result.returncode:
        raise CheckCouldNotRun(result.stderr.strip() or f"git {' '.join(arguments)} failed")
    return result.stdout.rstrip("\n")


def pushed_branch_point_tip(repo: Path, branch_point: str) -> str:
    branch = branch_point.removeprefix("refs/heads/")
    if "origin" in _git(repo, "remote").splitlines():
        ref = f"refs/remotes/origin/{branch}"
        try:
            return _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}")
        except LedgerError as error:
            raise LedgerError(f"origin exists but pushed branch point {ref} cannot be resolved; fetch origin first") from error
    return _git(repo, "rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}")


def _under(path: str, directory: str) -> bool:
    return directory == "." or path == directory or path.startswith(directory.rstrip("/") + "/")


def _table(value: object) -> dict[str, object]:
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in cast(list[object], value) if isinstance(item, str))


def _ignored_paths(data: dict[str, object]) -> tuple[str, ...]:
    paths: list[str] = []
    for key, value in data.items():
        if key in ("ignore", "ignored", "exclude", "excludes", "ignored_paths", "ignore_paths"):
            paths.extend(_strings(value))
        if isinstance(value, dict):
            paths.extend(_ignored_paths(cast(dict[str, object], value)))
    return tuple(paths)


def _is_ignored(path: str, patterns: tuple[str, ...]) -> bool:
    if "vendor" in PurePosixPath(path).parts[:-1]:
        return True
    return any(
        _under(path, pattern.rstrip("/")) or fnmatch.fnmatchcase(path, pattern)
        or (pattern.startswith("**/") and fnmatch.fnmatchcase(path, pattern[3:]))
        for pattern in patterns
    )


def _tip(repo: Path, commit: str) -> RepositoryTip:
    commit = _git(repo, "rev-parse", "--verify", f"{commit}^{{commit}}")
    tree = _git(repo, "rev-parse", f"{commit}^{{tree}}")
    entries = _git(repo, "ls-tree", "-rz", "--full-tree", commit)
    blobs: dict[str, str] = {}
    for entry in entries.split("\0"):
        if not entry:
            continue
        metadata, path = entry.split("\t", 1)
        _, kind, blob = metadata.split()
        if kind == "blob":
            blobs[path] = blob
    manifests = {path: _git(repo, "show", f"{commit}:{path}") for path in blobs if PurePosixPath(path).name == "Cargo.toml"}
    directories = tuple(member.directory for member in rule_catalog.workspace_members(manifests))
    ignored: list[str] = []
    for config in (".claude/config/berth.toml", "rustfmt.toml", ".rustfmt.toml"):
        if config in blobs:
            ignored.extend(_ignored_paths(_table(tomllib.loads(_git(repo, "show", f"{commit}:{config}")))))
    files = tuple(sorted(path for path in blobs if path.endswith(".rs") and not _is_ignored(path, tuple(ignored))))
    return RepositoryTip(commit, tree, blobs, directories, files)


def _export_blobs(repo: Path, tip: RepositoryTip, export: Path) -> None:
    with tempfile.TemporaryFile() as contents:
        result = subprocess.run(
            ["git", "-C", str(repo), "cat-file", "--batch"],
            input="".join(f"{blob}\n" for blob in tip.blobs.values()).encode(),
            stdout=contents, stderr=subprocess.PIPE, check=False,
        )
        if result.returncode:
            raise LedgerError(result.stderr.decode(errors="replace").strip() or "could not export synced blobs")
        _ = contents.seek(0)
        for path, blob in tip.blobs.items():
            header = contents.readline().split()
            if len(header) != 3 or header[:2] != [blob.encode(), b"blob"]:
                raise LedgerError(f"{path}: invalid Git blob response")
            size = int(header[2])
            content = contents.read(size)
            if len(content) != size or contents.read(1) != b"\n":
                raise LedgerError(f"{path}: incomplete Git blob response")
            destination = export / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            _ = destination.write_bytes(content)


def _fingerprint(repo: Path, tip: RepositoryTip, scope: CellInputScope, path: str) -> str:
    if scope == CellInputScope.MODULE:
        if path not in tip.blobs:
            raise LedgerError(f"{path}: no file at {tip.commit}")
        directory = PurePosixPath(path).parent
        parent_module = directory.as_posix() + ".rs"
        inputs = sorted((name, blob) for name, blob in tip.blobs.items()
                        if name.endswith(".rs") and (PurePosixPath(name).parent == directory or name == parent_module))
        return hashlib.sha256(json.dumps(inputs, separators=(",", ":")).encode()).hexdigest()
    if scope == "manifest":
        paths = {"Cargo.toml", *(str(PurePosixPath(directory) / "Cargo.toml") for directory in tip.crate_dirs)}
        manifests = sorted((name, blob) for name, blob in tip.blobs.items() if name in paths)
        return hashlib.sha256(json.dumps(manifests, separators=(",", ":")).encode()).hexdigest()
    if scope == "crate":
        directory = tip.crate(path)
        if directory == ".":
            inputs = sorted((name, blob) for name, blob in tip.blobs.items() if tip.crate(name) == ".")
            return hashlib.sha256(json.dumps(inputs, separators=(",", ":")).encode()).hexdigest()
        return _git(repo, "rev-parse", f"{tip.commit}:{directory}")
    blob = tip.blobs.get(path)
    if blob is None:
        raise LedgerError(f"{path}: no file at {tip.commit}")
    return blob


def _config_hash(repo: Path, tip: RepositoryTip, tool: LintTool) -> str:
    names = {
        "clippy": ("clippy.toml", ".clippy.toml"),
        "rustfmt": ("rustfmt.toml", ".rustfmt.toml"),
        "mend": ("mend.toml", ".mend.toml"),
    }[tool]
    inputs: list[tuple[str, str]] = []
    for path, blob in sorted(tip.blobs.items()):
        if PurePosixPath(path).name in names or (tool == "mend" and path == ".cargo/mend.toml"):
            inputs.append((path, blob))
        if PurePosixPath(path).name == "Cargo.toml" and tool in ("clippy", "mend"):
            data = _table(tomllib.loads(_git(repo, "show", f"{tip.commit}:{path}")))
            lints = _table(data.get("workspace")).get("lints")
            mend = _table(_table(data.get("package")).get("metadata")).get("mend")
            inputs.append((path, json.dumps({"lints": lints, "mend": mend if tool == "mend" else None}, sort_keys=True)))
    if tool == "mend":
        config_root = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
        global_config = config_root / "cargo-mend" / "config.toml"
        if global_config.is_file():
            inputs.append(("global:cargo-mend/config.toml", hashlib.sha256(global_config.read_bytes()).hexdigest()))
    return hashlib.sha256(json.dumps(inputs, separators=(",", ":")).encode()).hexdigest()


MIGRATIONS = {
    1: """
        CREATE TABLE judgments (
            id INTEGER PRIMARY KEY,
            rule TEXT NOT NULL, rule_blob TEXT NOT NULL, input_scope TEXT NOT NULL,
            input_fingerprint TEXT NOT NULL, path TEXT NOT NULL, "commit" TEXT NOT NULL,
            verdict TEXT NOT NULL, verdict_detail TEXT NOT NULL,
            ticket TEXT NOT NULL, worker TEXT NOT NULL, recorded_at TEXT NOT NULL,
            branch TEXT NOT NULL,
            UNIQUE(rule, path, "commit", rule_blob, input_scope, input_fingerprint, ticket, worker)
        );
        CREATE INDEX judgments_cell ON judgments(rule, path);
        CREATE TABLE lint_runs (
            id INTEGER PRIMARY KEY, tip TEXT NOT NULL, tree TEXT NOT NULL,
            tool TEXT NOT NULL, scope TEXT NOT NULL, config_hash TEXT NOT NULL,
            passed INTEGER NOT NULL, diagnostics_json TEXT NOT NULL,
            rule_blobs_json TEXT NOT NULL,
            UNIQUE(tree, tool, scope, config_hash)
        );
        CREATE TABLE sync (id INTEGER PRIMARY KEY CHECK(id = 1), tip TEXT NOT NULL);
    """,
    2: """
        ALTER TABLE judgments DROP COLUMN branch;
        ALTER TABLE sync ADD COLUMN style_branch TEXT NOT NULL DEFAULT '';
    """,
    3: """
        CREATE TABLE listed_rules (
            rule TEXT NOT NULL, rule_blob TEXT NOT NULL, tip TEXT NOT NULL,
            PRIMARY KEY(rule, rule_blob, tip)
        );
        CREATE TABLE listed_sites (
            rule TEXT NOT NULL, rule_blob TEXT NOT NULL, tip TEXT NOT NULL,
            path TEXT NOT NULL, line INTEGER NOT NULL, text TEXT NOT NULL,
            PRIMARY KEY(rule, rule_blob, tip, path, line, text),
            FOREIGN KEY(rule, rule_blob, tip) REFERENCES listed_rules(rule, rule_blob, tip) ON DELETE CASCADE
        );
    """,
    4: """
        ALTER TABLE sync ADD COLUMN synced_at TEXT NOT NULL DEFAULT '';
        UPDATE sync SET synced_at=strftime('%Y-%m-%dT%H:%M:%f+00:00','now');
        CREATE TABLE tickets (
            id TEXT PRIMARY KEY, unit TEXT NOT NULL, kind TEXT NOT NULL, family TEXT NOT NULL,
            rule TEXT NOT NULL, crate TEXT NOT NULL, files TEXT NOT NULL, sites TEXT NOT NULL,
            rules TEXT NOT NULL, rule_blobs TEXT NOT NULL, state TEXT NOT NULL,
            branch TEXT NOT NULL, checkpoint TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX tickets_one_open ON tickets(unit) WHERE state IN ('assigned','checkpointed');
        CREATE TABLE handoffs (
            checkpoint TEXT PRIMARY KEY, state TEXT NOT NULL, merge_commit TEXT NOT NULL,
            sent_to TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE processed_tips (tip TEXT PRIMARY KEY, processed_at TEXT NOT NULL);
        CREATE TABLE held_findings (
            finding_id TEXT PRIMARY KEY, rule TEXT NOT NULL, path TEXT NOT NULL, line INTEGER NOT NULL,
            text TEXT NOT NULL, kind TEXT NOT NULL, before TEXT NOT NULL, after TEXT NOT NULL,
            rule_blob TEXT NOT NULL, ticket TEXT NOT NULL, state TEXT NOT NULL, proposal TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE reconciliation (id INTEGER PRIMARY KEY CHECK(id=1), branch_point TEXT NOT NULL);
    """,
    5: """
        CREATE TABLE rule_texts (blob TEXT PRIMARY KEY, text BLOB NOT NULL);
        ALTER TABLE tickets ADD COLUMN base TEXT NOT NULL DEFAULT '';
    """,
    6: """
        ALTER TABLE tickets ADD COLUMN proposal TEXT NOT NULL DEFAULT '';
    """,
    7: """
        CREATE TABLE sync_waits (unit TEXT PRIMARY KEY, since TEXT NOT NULL, token TEXT NOT NULL);
    """,
    8: """
        CREATE TABLE ticket_yields (
            ticket TEXT NOT NULL, path TEXT NOT NULL, recorded_at TEXT NOT NULL,
            PRIMARY KEY(ticket, path)
        );
    """,
    9: "ALTER TABLE tickets ADD COLUMN cells TEXT NOT NULL DEFAULT '[]';",
    10: "ALTER TABLE judgments ADD COLUMN declared_scope TEXT NOT NULL DEFAULT 'rule';",
    11: """
        ALTER TABLE held_findings ADD COLUMN build_ticket TEXT NOT NULL DEFAULT '';
        ALTER TABLE tickets ADD COLUMN finding_ids TEXT NOT NULL DEFAULT '[]';
        CREATE TABLE rule_candidates (
            ticket TEXT NOT NULL, unit TEXT NOT NULL, path TEXT NOT NULL, line INTEGER NOT NULL,
            idea TEXT NOT NULL, created_at TEXT NOT NULL,
            PRIMARY KEY(ticket, path, line, idea)
        );
    """,
    # The old issue a migration moved the finding from, so a rerun credits it to that issue.
    12: "ALTER TABLE held_findings ADD COLUMN moved_from TEXT NOT NULL DEFAULT '';",
    # The judgment whose exception under a newer rule text withdrew the finding; 0 while it is not withdrawn.
    13: "ALTER TABLE held_findings ADD COLUMN withdrawn_by INTEGER NOT NULL DEFAULT 0;",
    # The person who read the code for the built finding in place of the applied check; '' while none did.
    14: "ALTER TABLE held_findings ADD COLUMN person_read_by TEXT NOT NULL DEFAULT '';",
    # The qualified finding that a withdrawn bare rename duplicates; '' when no duplicate withdrew it.
    15: "ALTER TABLE held_findings ADD COLUMN duplicate_of TEXT NOT NULL DEFAULT '';",
    # The person who undid the finding's last waive; '' while none did.
    16: "ALTER TABLE held_findings ADD COLUMN unwaived_by TEXT NOT NULL DEFAULT '';",
    # Where the finding's declaration was before its last follow, and the synced commit of that follow; '' while it
    # never followed. The synced commit at which no file declared it any more; '' while one does.
    17: """
        ALTER TABLE held_findings ADD COLUMN followed_from TEXT NOT NULL DEFAULT '';
        ALTER TABLE held_findings ADD COLUMN followed_from_line INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE held_findings ADD COLUMN followed_at TEXT NOT NULL DEFAULT '';
        ALTER TABLE held_findings ADD COLUMN declaration_gone_at TEXT NOT NULL DEFAULT '';
    """,
}


class Ledger:
    def __init__(self, repo: Path, database: Path) -> None:
        self.repo: Path = repo
        self.database: Path = database
        self._catalog: tuple[Rule, ...] = rule_catalog.load_rules(repo)
        self._tips: dict[str, RepositoryTip] = {}
        self._fingerprints: dict[tuple[str, CellInputScope, str], str] = {}
        self._ancestors: dict[tuple[str, str], bool] = {}
        self._branch_tips: dict[str, str] = {}
        # The declaration step's git reads, by commit: what each stored judged commit resolves to, the paths changed
        # from a read commit to a tip, the `.rs` files at a tip that hold a name as a word, and a file's content and
        # items at a commit. Rows are read fresh, so a recheck under the write lock sees another writer's changes.
        self._resolved_commits: dict[str, JudgedCommit | NoJudgedCommit] = {}
        self._changed_paths: dict[tuple[str, str], frozenset[str]] = {}
        self._named_files: dict[tuple[str, str], tuple[str, ...]] = {}
        self._commit_files: dict[tuple[str, str], bytes | Folder | MissingFile] = {}
        self._declared_files: dict[tuple[str, str], DeclaredFile] = {}
        database.parent.mkdir(parents=True, exist_ok=True)
        self._connection: sqlite3.Connection = sqlite3.connect(database, timeout=BUSY_TIMEOUT_SECONDS)
        try:
            _ = self._connection.execute("PRAGMA foreign_keys=ON")
            _ = self._connection.execute("BEGIN IMMEDIATE")
            _ = self._connection.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
            versions = cast(list[tuple[int]], self._connection.execute("SELECT version FROM schema_version").fetchall())
            version = versions[0][0] if versions else 0
            if version > max(MIGRATIONS):
                raise CheckCouldNotRun(f"unsupported ledger schema version {version}")
            for number, migration in sorted(MIGRATIONS.items()):
                if number > version:
                    for statement in migration.split(";"):
                        if statement.strip():
                            _ = self._connection.execute(statement)
                    _ = self._connection.execute("DELETE FROM schema_version")
                    _ = self._connection.execute("INSERT INTO schema_version VALUES (?)", (number,))
            self._connection.commit()
        except BaseException:
            # A locked or newer ledger fails here; close so the caller holds no connection.
            self.close()
            raise

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> Ledger:
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None) -> None:
        self.close()

    def synced_commit(self) -> str:
        """The synced tip as it is stored, with no catalog or coverage work."""
        rows = cast(list[tuple[str]], self._connection.execute("SELECT tip FROM sync WHERE id=1").fetchall())
        if not rows:
            raise LedgerError("no synced tip; run set-tip first")
        return rows[0][0]

    def _synced_tip(self) -> RepositoryTip:
        return self._tip(self.synced_commit())

    def _tip(self, commit: str) -> RepositoryTip:
        commit = _git(self.repo, "rev-parse", "--verify", f"{commit}^{{commit}}")
        if commit not in self._tips:
            self._tips[commit] = _tip(self.repo, commit)
        return self._tips[commit]

    def _fingerprint(self, tip: RepositoryTip, scope: CellInputScope, path: str) -> str:
        key = (tip.commit, scope, tip.crate(path) if scope == "crate" else "." if scope == "manifest" else path)
        if key not in self._fingerprints:
            self._fingerprints[key] = _fingerprint(self.repo, tip, scope, path)
        return self._fingerprints[key]

    def _judgment_inputs_match(self, row: Judgment, tip: RepositoryTip, scope: CellInputScope, path: str) -> bool:
        scope = _comparison_scope(row.declared_scope, scope)
        try:
            previous = (row.input_fingerprint if row.input_scope == scope
                        else self._fingerprint(self._tip(row.commit), scope, path))
            return previous == self._fingerprint(tip, scope, path)
        except LedgerError:
            return False

    def _is_ancestor(self, commit: str, tip: str) -> bool:
        key = (commit, tip)
        if key not in self._ancestors:
            result = subprocess.run(
                ["git", "-C", str(self.repo), "merge-base", "--is-ancestor", commit, tip],
                capture_output=True, text=True, check=False,
            )
            if result.returncode not in (0, 1):
                raise LedgerError(result.stderr.strip())
            self._ancestors[key] = result.returncode == 0
        return self._ancestors[key]

    def set_tip(self, tip: str, style_branch: str | None = None) -> None:
        commit = _git(self.repo, "rev-parse", "--verify", f"{tip}^{{commit}}")
        if style_branch is not None:
            style_branch = "refs/heads/" + style_branch.removeprefix("refs/heads/")
            _ = _git(self.repo, "check-ref-format", style_branch)
        # The sites exist before the tip is visible, so the write lock holds only the sync row.
        self._catalog = rule_catalog.load_rules(self.repo)
        self._refresh_sites(self._tip(commit))
        with self._connection:
            _ = self._connection.execute("BEGIN IMMEDIATE")
            _ = self._connection.execute(
                "INSERT INTO sync(id,tip,style_branch,synced_at) VALUES (1,?,?,?) ON CONFLICT(id) DO UPDATE SET tip=excluded.tip,style_branch=COALESCE(?,sync.style_branch),synced_at=excluded.synced_at",
                (commit, style_branch or "", _now(), style_branch),
            )

    def ensure_synced_tip(self, tip: str, style_branch: str) -> None:
        """Initialize a production at its style branch tip without rewriting an exact match."""
        style_branch = "refs/heads/" + style_branch.removeprefix("refs/heads/")
        sync = cast(list[tuple[str, str]], self._connection.execute("SELECT tip,style_branch FROM sync WHERE id=1").fetchall())
        if not sync or sync[0] != (tip, style_branch):
            self.set_tip(tip, style_branch)

    def synced_at(self) -> datetime:
        rows = cast(list[tuple[str]], self._connection.execute("SELECT synced_at FROM sync WHERE id=1").fetchall())
        if not rows:
            raise LedgerError("no synced tip; run set-tip first")
        return datetime.fromisoformat(rows[0][0])

    def _ticket(self, ticket_id: str) -> TicketRecord:
        rows = cast(list[tuple[str, str, TicketKind, str, str, str, str, str, str, str, TicketState, str, str, str, str, str, str, str, str]],
                    self._connection.execute("SELECT id,unit,kind,family,rule,crate,files,sites,rules,rule_blobs,state,branch,checkpoint,created_at,updated_at,base,proposal,cells,finding_ids FROM tickets WHERE id=?", (ticket_id,)).fetchall())
        if not rows:
            raise LedgerError(f"unknown ticket {ticket_id}")
        row = rows[0]
        return {
            "id": row[0], "unit": row[1], "kind": row[2], "family": row[3], "rule": row[4], "crate": row[5],
            "files": cast(list[str], json.loads(row[6])), "sites": cast(list[dict[str, object]], json.loads(row[7])),
            "rules": cast(list[str], json.loads(row[8])), "rule_blobs": cast(dict[str, str], json.loads(row[9])),
            "state": row[10], "branch": row[11], "checkpoint": row[12], "created_at": row[13], "updated_at": row[14],
            "base": row[15], "proposal": row[16], "cells": cast(list[list[str]], json.loads(row[17])),
            "finding_ids": cast(list[str], json.loads(row[18])),
        }

    def _check_pin(self, ticket: str, rule: str, rule_blob: str) -> str:
        rows = cast(list[tuple[str]], self._connection.execute("SELECT rule_blobs FROM tickets WHERE id=?", (ticket,)).fetchall())
        if rows:
            pins = cast(dict[str, str], json.loads(rows[0][0]))
            if rule not in pins:
                raise LedgerError(f"{ticket}: rule {rule} is outside the ticket")
            if rule_blob and rule_blob != pins[rule]:
                raise LedgerError(f"{ticket}: rule blob for {rule} differs from ticket pin {pins[rule]}")
            return pins[rule]
        if not rule_blob:
            raise LedgerError(f"{rule}: --rule-blob is required without a ledger ticket")
        return rule_blob

    def ticket_add(
        self, id: str, unit: str, kind: TicketKind, family: str, crate: str,
        files: list[str], rules: list[str], rule: str = "", sites: list[dict[str, object]] | None = None,
        proposal: str = "", cells: tuple[tuple[str, str], ...] = (),
        finding_ids: tuple[str, ...] = (),
    ) -> None:
        if not re.fullmatch(re.escape(unit) + r"-\d+", id):
            raise LedgerError(f"{id}: ticket id must be {unit}-<n>")
        if kind == "listed" and (not rule or rule not in rules or rule.endswith("#uncovered")):
            raise LedgerError(f"{id}: listed ticket requires its canonical rule in rules")
        if kind != "listed" and rule:
            raise LedgerError(f"{id}: rule is for listed tickets only")
        if kind == "read" and sites:
            raise LedgerError(f"{id}: sites are for listed or structure tickets only")
        if not rules:
            raise LedgerError(f"{id}: ticket must cover at least one rule")
        if cells and kind != "structure":
            raise LedgerError(f"{id}: explicit cells are for structure tickets only")
        cells = tuple((rule_id, self._path(path)) for rule_id, path in cells)
        if any(cell[0] not in rules for cell in cells):
            raise LedgerError(f"{id}: cell rule is outside ticket rules")
        finding_ids = tuple(sorted(set(finding_ids)))
        files = [self._path(path) for path in files]
        crate = self._path(crate)

        def added() -> bool:
            """Whether the ticket exists with these fields; refuses other fields, or findings a proposal cannot build."""
            exists = cast(list[tuple[str]], self._connection.execute("SELECT id FROM tickets WHERE id=?", (id,)).fetchall())
            if exists:
                previous = self._ticket(id)
                if (previous["unit"], previous["kind"], previous["family"], previous["crate"], previous["files"], previous["rules"], previous["rule"], previous["sites"], previous["proposal"], previous["cells"], previous["finding_ids"]) != (unit, kind, family, crate, files, rules, rule, sites or [], proposal, [list(cell) for cell in cells], list(finding_ids)):
                    raise LedgerError(f"{id}: ticket already exists in state {previous['state']} with different fields")
                return True
            if kind == "proposal":
                findings = tuple(self._held(finding_id) for finding_id in finding_ids)
                if not proposal or not findings:
                    raise LedgerError(f"{id}: proposal requires filed findings")
                for finding in findings:
                    if not isinstance(finding.state, Filed) or str(finding.state.proposal) != proposal:
                        raise LedgerError(f"{id}: proposal requires filed findings; {finding.finding_id} is not filed to {proposal}")
                    if isinstance(finding.build_ticket, BuildTicket):
                        owner = self._ticket(finding.build_ticket.ticket)
                        if owner["state"] not in FINAL_TICKET_STATES:
                            raise LedgerError(f"{finding.finding_id}: build ticket {owner['id']} already holds finding")
                if set(files) != {finding.path for finding in findings} or set(rules) != {finding.rule for finding in findings}:
                    raise LedgerError(f"{id}: proposal files and rules must match its filed findings")
            elif proposal or finding_ids:
                raise LedgerError(f"{id}: proposal findings are for proposal tickets only")
            return False

        def attempt() -> None:
            if added():
                return
            # Pin the rule texts before the write lock; under the lock the ticket checks run again.
            self._catalog = rule_catalog.load_rules(self.repo)
            definitions = {rule_id: self._component_rule(rule_id) for rule_id in rules}
            texts: list[PinnedRuleText] = []
            for rule_id, definition in definitions.items():
                text = definition.path.read_bytes()
                result = subprocess.run(
                    ["git", "-C", str(self.repo), "hash-object", "--stdin"],
                    input=text, capture_output=True, check=False,
                )
                if result.returncode:
                    raise LedgerError(result.stderr.decode(errors="replace").strip() or "git hash-object failed")
                hashed = result.stdout.decode().strip()
                if hashed != definition.blob:
                    raise ChangedBeforeLock(f"{rule_id}: rule changed while pinning; rerun")
                texts.append(PinnedRuleText(rule_id, hashed, text))
            with self._connection:
                _ = self._connection.execute("BEGIN IMMEDIATE")
                if added():
                    return
                _ = self._connection.executemany(
                    "INSERT INTO rule_texts VALUES (?,?) ON CONFLICT(blob) DO NOTHING", [(pin.blob, pin.text) for pin in texts],
                )
                pins = {pin.rule_id: pin.blob for pin in texts}
                now = _now()
                _ = self._connection.execute(
                    "INSERT INTO tickets(id,unit,kind,family,rule,crate,files,sites,rules,rule_blobs,state,branch,checkpoint,created_at,updated_at,proposal,cells,finding_ids) "
                    + "VALUES (?,?,?,?,?,?,?,?,?,?,'queued','','',?,?,?,?,?)",
                    (id, unit, kind, family, rule, crate, json.dumps(files), json.dumps(sites or []),
                     json.dumps(rules), json.dumps(pins, sort_keys=True), now, now, proposal, json.dumps(cells), json.dumps(finding_ids)),
                )
                for finding_id in finding_ids:
                    _ = self._connection.execute(
                        "UPDATE held_findings SET build_ticket=?,updated_at=? WHERE finding_id=?", (id, now, finding_id),
                    )

        retry_on_change(attempt)

    def pinned_rule_texts(self, ticket_id: str) -> tuple[PinnedRuleText, ...]:
        ticket = self._ticket(ticket_id)
        texts: list[PinnedRuleText] = []
        for rule_id in ticket["rules"]:
            blob = ticket["rule_blobs"][rule_id]
            rows = cast(list[tuple[bytes]], self._connection.execute(
                "SELECT text FROM rule_texts WHERE blob=?", (blob,),
            ).fetchall())
            if not rows:
                raise LedgerError(f"{ticket_id}: no pinned text for {blob}")
            texts.append(PinnedRuleText(rule_id, blob, rows[0][0]))
        return tuple(texts)

    def queued_tickets(self, unit: str) -> tuple[TicketRecord, ...]:
        rows = cast(list[tuple[str]], self._connection.execute(
            "SELECT id FROM tickets WHERE unit=? AND state='queued' ORDER BY created_at,id", (unit,),
        ).fetchall())
        return tuple(self._ticket(row[0]) for row in rows)

    def next_ticket_id(self, unit: str) -> str:
        rows = cast(list[tuple[str]], self._connection.execute(
            "SELECT id FROM tickets WHERE unit=?", (unit,),
        ).fetchall())
        highest = max((int(row[0].removeprefix(unit + "-")) for row in rows), default=0)
        return f"{unit}-{highest + 1}"

    def unjudged_ticket_cells(self, id: str) -> tuple[UnjudgedTicketCell, ...]:
        ticket = self._ticket(id)
        if ticket["kind"] == "proposal":
            return ()
        paths = [ticket["crate"]] if ticket["kind"] == "structure" else ticket["files"]
        rules = [ticket["rule"]] if ticket["kind"] == "listed" else ticket["rules"]
        judged = set(cast(list[tuple[str, str]], self._connection.execute(
            "SELECT rule,path FROM judgments WHERE ticket=?", (id,),
        ).fetchall()))
        yielded = {row[0] for row in cast(list[tuple[str]], self._connection.execute(
            "SELECT path FROM ticket_yields WHERE ticket=?", (id,),
        ).fetchall())}
        owned = ({(rule, path) for rule, path in ticket["cells"] if path not in yielded}
                 if ticket["cells"] else {(rule, path) for rule in rules for path in paths if path not in yielded})
        return tuple(UnjudgedTicketCell(rule, path) for rule, path in sorted(owned - judged))

    def ticket_yield(self, id: str, path: str) -> None:
        path = self._path(path)
        with self._connection:
            _ = self._connection.execute("BEGIN IMMEDIATE")
            ticket = self._ticket(id)
            if ticket["state"] != "assigned":
                raise LedgerError(f"{id}: cannot yield from {ticket['state']}")
            if ticket["kind"] in ("structure", "proposal"):
                raise LedgerError(f"{id}: cannot yield a {ticket['kind']} ticket")
            if path not in ticket["files"]:
                raise LedgerError(f"{id}: {path} is not in ticket files")
            if self._connection.execute("SELECT 1 FROM ticket_yields WHERE ticket=? AND path=?", (id, path)).fetchone():
                return
            _ = self._connection.execute("DELETE FROM judgments WHERE ticket=? AND path=?", (id, path))
            _ = self._connection.execute(
                "DELETE FROM held_findings WHERE ticket=? AND path=? AND state='unfiled'", (id, path),
            )
            _ = self._connection.execute("INSERT INTO ticket_yields VALUES (?,?,?)", (id, path, _now()))

    def applied_check(self, finding: HeldFinding, commit: str) -> AppliedCheck:
        """Whether the finding's change is in the code at commit."""
        return applied_check(finding.kind, finding.before, finding.after, finding.path,
                             lambda path: self._file_at(commit, path))

    def _file_at(self, commit: str, path: str) -> bytes | Folder | MissingFile:
        """What is at path in commit: a file's bytes, a folder or nothing. A git error raises, so it never reads as nothing."""
        entry = _git(self.repo, "ls-tree", commit, "--", path)
        if not entry:
            return MissingFile()
        # An entry reads `<mode> <type> <object>\t<path>`.
        if entry.split()[1] == "tree":
            return Folder()
        shown = subprocess.run(["git", "-C", str(self.repo), "show", f"{commit}:{path}"], capture_output=True, check=False)
        if shown.returncode:
            raise CheckCouldNotRun(shown.stderr.decode(errors="replace").strip() or f"git show {commit}:{path} failed")
        return shown.stdout

    def _ticket_transition(self, id: str, target: TicketState, commit: str = "", branch: str = "") -> tuple[FindingNotBuilt, ...]:
        """Move the ticket to target. A proposal merge returns each of its findings that stays filed.

        The git work runs before the write lock; if the rows it read change before the lock, the move starts again.
        """

        def arrived(ticket: TicketRecord) -> bool:
            """Whether the ticket is already at target; refuses a move its state does not allow."""
            state = ticket["state"]
            if state == target:
                if target == "assigned" and ticket["branch"] != branch:
                    raise LedgerError(f"{id}: assigned to branch {ticket['branch']}")
                if target == "checkpointed" and ticket["checkpoint"] != commit:
                    raise LedgerError(f"{id}: checkpointed at {ticket['checkpoint']}")
                return True
            predecessor = {"assigned": "queued", "checkpointed": "assigned", "merged": "checkpointed"}
            if (target == "dropped" and state in ("merged", "dropped")) or (target != "dropped" and predecessor.get(target) != state):
                raise LedgerError(f"{id}: cannot become {target} from {state}")
            if target == "checkpointed":
                missing = self.unjudged_ticket_cells(id)
                if missing:
                    raise LedgerError(f"{id}: unjudged cells: " + ", ".join(f"{cell.rule} {cell.path}" for cell in missing))
            if target == "assigned":
                active = self.ticket_open(ticket["unit"])
                if active:
                    raise LedgerError(f"{id}: unit {ticket['unit']} already has {active[0]['id']} in state {active[0]['state']}")
            return False

        def attempt() -> tuple[FindingNotBuilt, ...]:
            ticket = self._ticket(id)
            if arrived(ticket):
                return ()
            checkpoint = commit if target == "checkpointed" else ticket["checkpoint"]
            restamps = self.checkpoint_restamps(ticket, checkpoint) if target in ("checkpointed", "merged") else NoRestamps()
            building = target == "merged" and ticket["kind"] == "proposal"
            findings = self.filed_build_findings(id) if building else ()
            checks = tuple((finding.finding_id, self.applied_check(finding, checkpoint)) for finding in findings)
            not_built: list[FindingNotBuilt] = []
            with self._connection:
                _ = self._connection.execute("BEGIN IMMEDIATE")
                current = self._ticket(id)
                if arrived(current):
                    return ()
                if (current != ticket or (building and self.filed_build_findings(id) != findings)
                        or (isinstance(restamps, CheckpointRestamps) and self.latest_ticket_judgments(id) != restamps.latest)):
                    raise ChangedBeforeLock(f"{id}: the ticket changed while it became {target}; rerun")
                if isinstance(restamps, CheckpointRestamps):
                    for verdict in restamps.carried:
                        _ = self._connection.execute(
                            'INSERT INTO judgments(rule,rule_blob,input_scope,input_fingerprint,path,"commit",verdict,verdict_detail,ticket,worker,recorded_at,declared_scope) '
                            + 'SELECT rule,rule_blob,?,?,path,?,verdict,verdict_detail,ticket,worker,?,declared_scope FROM judgments WHERE id=? '
                            + 'ON CONFLICT(rule,path,"commit",rule_blob,input_scope,input_fingerprint,ticket,worker) DO NOTHING',
                            (verdict.scope, verdict.fingerprint, checkpoint, datetime.now(timezone.utc).isoformat(), verdict.judgment_id),
                        )
                if building:
                    now = _now()
                    for finding_id, check in checks:
                        if isinstance(check, AppliedInCode):
                            # A person's reading was of the old build, so a new build clears it.
                            _ = self._connection.execute(
                                "UPDATE held_findings SET state='built',person_read_by='',updated_at=? WHERE finding_id=?", (now, finding_id),
                            )
                            continue
                        if isinstance(check, RenameNotApplied):
                            # The next refill takes the finding into a new ticket.
                            _ = self._connection.execute(
                                "UPDATE held_findings SET build_ticket='',updated_at=? WHERE finding_id=?", (now, finding_id),
                            )
                        # A finding with no check keeps the merged ticket, so refill skips it until a person marks it built.
                        not_built.append(FindingNotBuilt(finding_id, check))
                if target == "dropped" and ticket["kind"] == "proposal":
                    _ = self._connection.execute(
                        "UPDATE held_findings SET build_ticket='',updated_at=? WHERE build_ticket=? AND state='filed'",
                        (_now(), id),
                    )
                _ = self._connection.execute(
                    "UPDATE tickets SET state=?,branch=?,checkpoint=?,updated_at=? WHERE id=?",
                    (target, branch if target == "assigned" else ticket["branch"],
                     commit if target == "checkpointed" else ticket["checkpoint"], _now(), id),
                )
            return tuple(not_built)

        return retry_on_change(attempt)

    def ticket_assign(self, id: str, branch: str) -> None:
        branch = "refs/heads/" + branch.removeprefix("refs/heads/")
        _ = _git(self.repo, "check-ref-format", branch)
        _ = self._ticket_transition(id, "assigned", branch=branch)

    def ticket_base(self, id: str) -> str:
        """Remember the assigned branch's start before publishing its first phase."""
        ticket = self._ticket(id)
        if ticket["base"]:
            return ticket["base"]
        if ticket["state"] != "assigned":
            raise LedgerError(f"{id}: cannot record start commit in state {ticket['state']}")
        # Resolve the branch before the write lock; an assigned ticket keeps its branch.
        base = _git(self.repo, "rev-parse", "--verify", f"{ticket['branch']}^{{commit}}")
        with self._connection:
            _ = self._connection.execute("BEGIN IMMEDIATE")
            ticket = self._ticket(id)
            if ticket["base"]:
                return ticket["base"]
            if ticket["state"] != "assigned":
                raise LedgerError(f"{id}: cannot record start commit in state {ticket['state']}")
            _ = self._connection.execute("UPDATE tickets SET base=?,updated_at=? WHERE id=?", (base, _now(), id))
            return base

    def ticket_checkpoint(self, id: str, commit: str) -> None:
        _ = self._ticket_transition(id, "checkpointed", _git(self.repo, "rev-parse", "--verify", f"{commit}^{{commit}}"))

    def latest_ticket_judgments(self, ticket_id: str) -> tuple[TicketJudgment, ...]:
        rows = cast(list[tuple[int, str, str, str, str]], self._connection.execute(
            'SELECT j.id,j.rule,j.path,j."commit",j.declared_scope FROM judgments j WHERE j.ticket=? '
            + 'AND j.id=(SELECT latest.id FROM judgments latest WHERE latest.ticket=j.ticket '
            + 'AND latest.rule=j.rule AND latest.path=j.path ORDER BY latest.recorded_at DESC,latest.id DESC LIMIT 1)',
            (ticket_id,),
        ).fetchall())
        return tuple(TicketJudgment(judgment_id, rule, path, commit, DeclaredInputScope(declared))
                     for judgment_id, rule, path, commit, declared in rows)

    def filed_build_findings(self, ticket_id: str) -> tuple[HeldFinding, ...]:
        """The filed findings this ticket holds for its build."""
        rows = cast(list[tuple[str]], self._connection.execute(
            "SELECT finding_id FROM held_findings WHERE build_ticket=? AND state='filed' ORDER BY finding_id", (ticket_id,),
        ).fetchall())
        return tuple(self._held(finding_id) for (finding_id,) in rows)

    def checkpoint_restamps(self, ticket: TicketRecord, checkpoint: str) -> CheckpointRestamps | NoRestamps:
        """The verdicts of this ticket that carry across edits made only by this ticket."""
        if not ticket["base"] or not self._is_ancestor(ticket["base"], checkpoint):
            return NoRestamps()
        latest = self.latest_ticket_judgments(ticket["id"])
        self._catalog = rule_catalog.load_rules(self.repo)
        tip = self._tip(checkpoint)
        # Commits inherited at the start or merged from another branch are not ours.
        own_commits = set(_git(self.repo, "rev-list", "--first-parent", checkpoint,
                               "^" + ticket["base"]).splitlines())
        carried: list[CarriedVerdict] = []
        for judgment in latest:
            try:
                definition = self._component_rule(judgment.rule)
                if definition.family != ticket["family"] or judgment.commit == checkpoint:
                    continue
                if not self._is_ancestor(judgment.commit, checkpoint):
                    continue
                changed_commits = set(_git(self.repo, "rev-list", "--full-history", checkpoint,
                                          "^" + judgment.commit, "--", judgment.path).splitlines())
                if not changed_commits <= own_commits:
                    continue
                scope = _comparison_scope(judgment.declared_scope, _cell_scope(definition, judgment.path, tip))
                fingerprint = self._fingerprint(tip, scope, judgment.path)
            except LedgerError:
                continue
            carried.append(CarriedVerdict(judgment.judgment_id, scope, fingerprint))
        return CheckpointRestamps(latest, tuple(carried))

    def ticket_merge(self, id: str) -> tuple[FindingNotBuilt, ...]:
        """Mark the ticket merged. A proposal finding becomes built only when its change is in the checkpoint.

        Each run, also on a merged ticket, returns the findings that wait for a person to mark them built.
        """
        ticket = self._ticket(id)
        if ticket["state"] == "merged":
            # Merge clears the build ticket of a rename that is not applied, so a finding that keeps it has no check.
            return tuple(FindingNotBuilt(finding.finding_id, NoAppliedCheck(finding.kind)) for finding in self.filed_build_findings(id))
        if ticket["state"] != "checkpointed":
            raise LedgerError(f"{id}: cannot become merged from {ticket['state']}")
        rows = cast(list[tuple[str]], self._connection.execute("SELECT style_branch FROM sync WHERE id=1").fetchall())
        branch = rows[0][0] if rows else ""
        branch_tip = _git(self.repo, "rev-parse", "--verify", f"{branch}^{{commit}}") if branch else ""
        if not branch_tip or not self._is_ancestor(ticket["checkpoint"], branch_tip):
            raise LedgerError(f"{id}: checkpoint {ticket['checkpoint'][:7]} is not on {branch or 'the style branch'}")
        return self._ticket_transition(id, "merged")

    def wait_for_sync(self, unit: str) -> None:
        with self._connection:
            _ = self._connection.execute(
                "INSERT INTO sync_waits VALUES (?,?,?) ON CONFLICT(unit) DO NOTHING", (unit, _now(), uuid.uuid4().hex),
            )

    def clear_sync_wait(self, unit: str) -> None:
        with self._connection:
            _ = self._connection.execute("DELETE FROM sync_waits WHERE unit=?", (unit,))

    def acknowledge_sync_wait(self, wait: SyncWait) -> None:
        with self._connection:
            _ = self._connection.execute(
                "DELETE FROM sync_waits WHERE unit=? AND token=?", (wait.unit, wait.token),
            )

    def sync_waits(self) -> tuple[SyncWait, ...]:
        rows = cast(list[tuple[str, str, str]], self._connection.execute(
            "SELECT unit,since,token FROM sync_waits ORDER BY since,unit",
        ).fetchall())
        return tuple(SyncWait(unit, since, token) for unit, since, token in rows)

    def ticket_origin(self, id: str) -> ApprovedProposal | SelectedByCoverage:
        ticket = self._ticket(id)
        if ticket["kind"] != "proposal":
            return SelectedByCoverage(ticket["base"])
        return ApprovedProposal(Path(ticket["proposal"]), ticket["unit"], tuple(ticket["finding_ids"]))

    def proposal_tickets(self) -> tuple[TicketRecord, ...]:
        rows = cast(list[tuple[str]], self._connection.execute(
            "SELECT id FROM tickets WHERE kind='proposal' ORDER BY created_at,id",
        ).fetchall())
        return tuple(self._ticket(row[0]) for row in rows)

    def unsynced_merges(self, proposal: Path) -> tuple[TicketRecord, ...]:
        """The merged tickets of the proposal whose checkpoint the synced commit does not hold."""
        commit = self.synced_commit()
        return tuple(ticket for ticket in self.proposal_tickets()
                     if ticket["state"] == "merged" and Path(ticket["proposal"]) == proposal
                     and not self._is_ancestor(ticket["checkpoint"], commit))

    def ticket_drop(self, id: str) -> None:
        _ = self._ticket_transition(id, "dropped")

    def ticket_open(self, unit: str) -> tuple[TicketRecord, ...]:
        rows = cast(list[tuple[str]], self._connection.execute(
            "SELECT id FROM tickets WHERE unit=? AND state IN ('assigned','checkpointed') ORDER BY created_at,id", (unit,),
        ).fetchall())
        return tuple(self._ticket(row[0]) for row in rows)

    def handoff_add(self, checkpoint: str) -> None:
        checkpoint = _git(self.repo, "rev-parse", "--verify", f"{checkpoint}^{{commit}}")
        now = _now()
        with self._connection:
            _ = self._connection.execute("INSERT INTO handoffs VALUES (?,'pending','','',?,?) ON CONFLICT(checkpoint) DO NOTHING", (checkpoint, now, now))

    def handoff_send(self, checkpoint: str, to: str) -> None:
        checkpoint = _git(self.repo, "rev-parse", "--verify", f"{checkpoint}^{{commit}}")
        with self._connection:
            _ = self._connection.execute("BEGIN IMMEDIATE")
            rows = cast(list[tuple[str, str]], self._connection.execute("SELECT state,sent_to FROM handoffs WHERE checkpoint=?", (checkpoint,)).fetchall())
            if not rows:
                raise LedgerError(f"unknown hand-off {checkpoint}")
            state, sent_to = rows[0]
            if state == "sent" and sent_to == to:
                return
            if state != "pending":
                raise LedgerError(f"{checkpoint}: cannot send hand-off in state {state}")
            if not to:
                raise LedgerError("hand-off send requires --to")
            _ = self._connection.execute("UPDATE handoffs SET state='sent',sent_to=?,updated_at=? WHERE checkpoint=?", (to, _now(), checkpoint))

    def handoff_reroute(self, checkpoint: str, to: str) -> None:
        checkpoint = _git(self.repo, "rev-parse", "--verify", f"{checkpoint}^{{commit}}")
        if not to:
            raise LedgerError("hand-off reroute requires --to")
        with self._connection:
            _ = self._connection.execute("BEGIN IMMEDIATE")
            rows = cast(list[tuple[str, str]], self._connection.execute(
                "SELECT state,sent_to FROM handoffs WHERE checkpoint=?", (checkpoint,),
            ).fetchall())
            if not rows:
                raise LedgerError(f"unknown hand-off {checkpoint}")
            state, sent_to = rows[0]
            if state != "sent":
                hint = "; use handoff send" if state == "pending" else ""
                raise LedgerError(f"{checkpoint}: cannot reroute hand-off in state {state}{hint}")
            if sent_to != to:
                _ = self._connection.execute(
                    "UPDATE handoffs SET sent_to=?,updated_at=? WHERE checkpoint=?", (to, _now(), checkpoint),
                )

    def _first_merge(self, checkpoint: str, branch_point: str) -> str:
        branch_tip = pushed_branch_point_tip(self.repo, branch_point)
        for commit in _git(self.repo, "rev-list", "--first-parent", "--reverse", "--merges", branch_tip).splitlines():
            if self._is_ancestor(checkpoint, commit):
                return commit
        # A fast-forward has no merge commit; its containing branch-point tip is the evidence.
        return branch_tip

    def handoff_merged(self, checkpoint: str, merge_commit: str = "") -> None:
        checkpoint = _git(self.repo, "rev-parse", "--verify", f"{checkpoint}^{{commit}}")
        existing = cast(list[tuple[str]], self._connection.execute("SELECT state FROM handoffs WHERE checkpoint=?", (checkpoint,)).fetchall())
        if not existing:
            raise LedgerError(f"unknown hand-off {checkpoint}")
        if existing[0][0] == "pending":
            raise LedgerError(f"{checkpoint}: cannot become merged from pending")
        branches = cast(list[tuple[str]], self._connection.execute("SELECT branch_point FROM reconciliation WHERE id=1").fetchall())
        if branches:
            branch_point = branches[0][0]
        else:
            raise LedgerError("hand-off merge requires a branch point saved by reconcile")
        branch_tip = pushed_branch_point_tip(self.repo, branch_point)
        if not self._is_ancestor(checkpoint, branch_tip):
            raise LedgerError(f"{checkpoint}: hand-off is not merged into {branch_point}")
        if merge_commit:
            actual_merge = _git(self.repo, "rev-parse", "--verify", f"{merge_commit}^{{commit}}")
            if not self._is_ancestor(checkpoint, actual_merge) or not self._is_ancestor(actual_merge, branch_tip):
                raise LedgerError(f"{checkpoint}: {merge_commit} is not merge evidence on {branch_point}")
        else:
            actual_merge = self._first_merge(checkpoint, branch_point)
        with self._connection:
            _ = self._connection.execute("BEGIN IMMEDIATE")
            rows = cast(list[tuple[str]], self._connection.execute("SELECT state FROM handoffs WHERE checkpoint=?", (checkpoint,)).fetchall())
            if not rows:
                raise LedgerError(f"unknown hand-off {checkpoint}")
            state = rows[0][0]
            if state == "merged":
                return
            if state != "sent":
                raise LedgerError(f"{checkpoint}: cannot become merged from {state}")
            _ = self._connection.execute("UPDATE handoffs SET state='merged',merge_commit=?,updated_at=? WHERE checkpoint=?", (actual_merge, _now(), checkpoint))

    def pending_handoffs(self) -> tuple[dict[str, str], ...]:
        rows = cast(list[tuple[str, str, str, str, str, str]], self._connection.execute(
            "SELECT checkpoint,state,merge_commit,sent_to,created_at,updated_at FROM handoffs WHERE state!='merged' ORDER BY created_at,checkpoint",
        ).fetchall())
        return tuple(dict(zip(("checkpoint", "state", "merge_commit", "sent_to", "created_at", "updated_at"), row)) for row in rows)

    def tip_seen(self, tip: str) -> bool:
        tip = _git(self.repo, "rev-parse", "--verify", f"{tip}^{{commit}}")
        return self._connection.execute("SELECT tip FROM processed_tips WHERE tip=?", (tip,)).fetchone() is not None

    def tip_done(self, tip: str) -> None:
        tip = _git(self.repo, "rev-parse", "--verify", f"{tip}^{{commit}}")
        with self._connection:
            _ = self._connection.execute("INSERT INTO processed_tips VALUES (?,?) ON CONFLICT(tip) DO NOTHING", (tip, _now()))

    def hold(
        self, rule: str, path: str, line: int, text: str, kind: FindingKind, before: str,
        after: str, rule_blob: str, ticket: str,
    ) -> HeldFinding:
        self._catalog = rule_catalog.load_rules(self.repo)
        definition = self._component_rule(rule)
        rule_blob = self._check_pin(ticket, rule, rule_blob)
        path = self._path(path)
        if isinstance(definition.reach, MemberReach) and not _under(path, definition.reach.crate_dir.as_posix()):
            raise LedgerError(f"{rule}: {path} is outside rule reach")
        if kind not in ("rename", "type", "restructure") or line < 1:
            raise LedgerError("held finding requires a valid kind and positive line")
        finding_id = hashlib.sha256(json.dumps((rule, path, line, text, rule_blob), separators=(",", ":")).encode()).hexdigest()[:16]
        if not self._connection.execute("SELECT 1 FROM held_findings WHERE finding_id=?", (finding_id,)).fetchone():
            # A followed finding keeps the id of its first site. A ticket that reads its new file holds the same change
            # again, so that change returns the followed finding, the one with the nearest line.
            followed = cast(list[tuple[str, int]], self._connection.execute(
                "SELECT finding_id,line FROM held_findings WHERE rule=? AND path=? AND text=? AND kind=? AND before=? AND after=? "
                + "AND rule_blob=? AND followed_at!='' AND state IN ('unfiled','filed')", (rule, path, text, kind, before, after, rule_blob),
            ).fetchall())
            if followed:
                return self._held(min(followed, key=lambda row: (abs(row[1] - line), row[0]))[0])
        now = _now()
        with self._connection:
            _ = self._connection.execute(
                "INSERT INTO held_findings(finding_id,rule,path,line,text,kind,before,after,rule_blob,ticket,state,proposal,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,'unfiled','',?,?) ON CONFLICT(finding_id) DO NOTHING",
                (finding_id, rule, path, line, text, kind, before, after, rule_blob, ticket, now, now),
            )
        return self._held(finding_id)

    def _held(self, finding_id: str) -> HeldFinding:
        rows = cast(list[tuple[str, str, str, int, str, FindingKind, str, str, str, str, HeldStateName, str, str, int, str, str, str, str, int, str, str]], self._connection.execute(
            "SELECT finding_id,rule,path,line,text,kind,before,after,rule_blob,ticket,state,proposal,build_ticket,withdrawn_by,person_read_by,duplicate_of,unwaived_by,"
            + "followed_from,followed_from_line,followed_at,declaration_gone_at FROM held_findings WHERE finding_id=?", (finding_id,),
        ).fetchall())
        if not rows:
            raise LedgerError(f"unknown held finding {finding_id}")
        row = rows[0]
        state: HeldState
        match row[10]:
            case "filed":
                state = Filed(Path(row[11]))
            case "waived":
                state = Waived()
            case "built":
                state = Built(Path(row[11]))
            case "withdrawn":
                state = Withdrawn(Path(row[11]) if row[11] else NoDecision(),
                                  DuplicateOf(row[15]) if row[15] else DeclarationGone(row[20]) if row[20] else ExceptionVerdict(row[13]))
            case "unfiled":
                state = Unfiled()
        return HeldFinding(row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7], row[8], row[9], state, BuildTicket(row[12]) if row[12] else NoBuildTicket(),
                           ReadByPerson(row[14]) if row[14] else NotReadByPerson(), WaiveUndoneBy(row[16]) if row[16] else NoWaiveUndo(),
                           FollowedDeclaration(row[17], row[18], row[19]) if row[19] else NotFollowed())

    def judged_commit(self, finding: HeldFinding) -> JudgedCommit | NoJudgedCommit:
        rows = cast(list[tuple[str, str]], self._connection.execute(
            "SELECT checkpoint,base FROM tickets WHERE id=?", (finding.ticket,),
        ).fetchall())
        for commit in (commit for commit in (rows[0] if rows else ()) if commit):
            if commit not in self._resolved_commits:
                resolved = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}"],
                                          capture_output=True, text=True, check=False)
                self._resolved_commits[commit] = NoJudgedCommit() if resolved.returncode else JudgedCommit(resolved.stdout.strip())
            judged = self._resolved_commits[commit]
            if isinstance(judged, JudgedCommit):
                return judged
        return NoJudgedCommit()

    def held_findings(self, selection: AllHeldFindings | HeldFindingsInState | UnfiledFindingsFromOpenTickets) -> tuple[HeldFinding, ...]:
        if isinstance(selection, UnfiledFindingsFromOpenTickets):
            query = self._connection.execute(
                "SELECT h.finding_id FROM held_findings h JOIN tickets t ON t.id=h.ticket "
                + "WHERE h.state='unfiled' AND t.state IN ('assigned','checkpointed') "
                + "ORDER BY h.created_at,h.finding_id",
            )
        elif isinstance(selection, HeldFindingsInState):
            query = self._connection.execute(
                "SELECT finding_id FROM held_findings WHERE state=? ORDER BY created_at,finding_id", (selection.state,),
            )
        else:
            query = self._connection.execute("SELECT finding_id FROM held_findings ORDER BY created_at,finding_id")
        rows = cast(list[tuple[str]], query.fetchall())
        return tuple(self._held(row[0]) for row in rows)

    def proposal_findings(self, proposal: Path) -> tuple[HeldFinding, ...]:
        rows = cast(list[tuple[str]], self._connection.execute(
            "SELECT finding_id FROM held_findings WHERE proposal=? ORDER BY created_at,finding_id", (str(proposal),),
        ).fetchall())
        return tuple(self._held(row[0]) for row in rows)

    def proposal_paths(self) -> tuple[Path, ...]:
        """Every issue that holds a finding, in any state."""
        rows = cast(list[tuple[str]], self._connection.execute(
            "SELECT DISTINCT proposal FROM held_findings WHERE proposal<>'' ORDER BY proposal",
        ).fetchall())
        return tuple(Path(row[0]) for row in rows)

    def moved_findings(self, source: Path) -> dict[str, Path]:
        """The decision of each finding that a migration moved from source."""
        rows = cast(list[tuple[str, str]], self._connection.execute(
            "SELECT finding_id,proposal FROM held_findings WHERE moved_from=? ORDER BY finding_id", (str(source),),
        ).fetchall())
        return {finding_id: Path(proposal) for finding_id, proposal in rows}

    def move_issue_findings(self, source: Path, decisions: Mapping[str, Path]) -> None:
        """Move each finding from source to its decision in one transaction, and record source.

        A finding that an earlier run moved from source stays as it is.
        """
        with self._connection:
            _ = self._connection.execute("BEGIN IMMEDIATE")
            now = _now()
            moved = self.moved_findings(source)
            for finding_id, decision in sorted(decisions.items()):
                if finding_id in moved:
                    if moved[finding_id] != decision:
                        raise LedgerError(f"{finding_id}: moved from {source} to {moved[finding_id]}, not {decision}")
                    continue
                finding = self._held(finding_id)
                if not isinstance(finding.state, Filed) or finding.state.proposal != source:
                    raise LedgerError(f"{finding_id}: finding is not filed to {source}")
                if isinstance(finding.build_ticket, BuildTicket):
                    ticket = self._ticket(finding.build_ticket.ticket)
                    if ticket["state"] not in FINAL_TICKET_STATES:
                        raise LedgerError(f"{finding_id}: live build ticket {ticket['id']}")
                _ = self._connection.execute(
                    "UPDATE held_findings SET proposal=?,moved_from=?,updated_at=? WHERE finding_id=?",
                    (str(decision), str(source), now, finding_id),
                )

    def withdraw_duplicates(self, duplicates: Iterable[BareDuplicate]) -> None:
        """Withdraw each bare duplicate into its decision in one transaction, and record the finding it duplicates.

        A row filed to another issue moves to the decision and records that issue, so a rerun credits it there.
        A row already withdrawn as the same duplicate stays as it is.
        """
        with self._connection:
            _ = self._connection.execute("BEGIN IMMEDIATE")
            now = _now()
            for duplicate in sorted(duplicates, key=lambda item: item.finding_id):
                finding = self._held(duplicate.finding_id)
                if finding.state == Withdrawn(duplicate.decision, DuplicateOf(duplicate.qualified)):
                    continue
                if not isinstance(finding.state, (Filed, Built)):
                    raise LedgerError(f"{duplicate.finding_id}: cannot withdraw a {held_state_name(finding.state)} finding as a duplicate")
                if isinstance(finding.build_ticket, BuildTicket):
                    ticket = self._ticket(finding.build_ticket.ticket)
                    if ticket["state"] not in FINAL_TICKET_STATES:
                        raise LedgerError(f"{duplicate.finding_id}: build ticket {ticket['id']} still holds the finding")
                source = finding.state.proposal
                _ = self._connection.execute(
                    "UPDATE held_findings SET state='withdrawn',duplicate_of=?,withdrawn_by=0,proposal=?,"
                    + "moved_from=CASE WHEN ?=proposal THEN moved_from ELSE ? END,updated_at=? WHERE finding_id=?",
                    (duplicate.qualified, str(duplicate.decision), str(duplicate.decision), str(source), now, duplicate.finding_id),
                )

    def record_candidate(self, ticket: str, path: str, line: int, idea: str) -> None:
        path = self._path(path)
        if line < 1 or not idea.strip() or len(idea.splitlines()) != 1:
            raise LedgerError("candidate requires a positive line and one line of text")
        with self._connection:
            _ = self._connection.execute("BEGIN IMMEDIATE")
            owner = self._ticket(ticket)
            _ = self._connection.execute(
                "INSERT INTO rule_candidates VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                (ticket, owner["unit"], path, line, idea, _now()),
            )

    def drop_candidates(self, idea: str) -> int:
        """Remove every candidate with exactly this idea text. Returns how many it removed."""
        with self._connection:
            removed = self._connection.execute("DELETE FROM rule_candidates WHERE idea=?", (idea,)).rowcount
        if not removed:
            raise LedgerError(f"no rule candidate has the idea {idea!r}")
        return removed

    def rule_candidates(self) -> tuple[RuleCandidate, ...]:
        rows = cast(list[tuple[str, str, str, int, str, str]], self._connection.execute(
            "SELECT ticket,unit,path,line,idea,created_at FROM rule_candidates ORDER BY created_at,ticket,path,line,idea",
        ).fetchall())
        return tuple(RuleCandidate(*row) for row in rows)

    def decline_findings(self, finding_ids: Iterable[str], words: str) -> dict[str, DeclineOutcome]:
        """Waive the findings, and with the words each judgment cell that no other finding holds.

        The catalog, the synced tip and each cell's decision come before the write lock; if the rows they
        rest on change before the lock, the decline starts again.
        """
        ids = tuple(dict.fromkeys(finding_ids))

        def attempt() -> dict[str, DeclineOutcome]:
            findings = tuple(self._held(finding_id) for finding_id in ids)
            # A newer verdict withdrew a withdrawn finding, so a decline does not make it waived.
            resolved = next((finding for finding in findings if isinstance(finding.state, (Built, Withdrawn))), None)
            if resolved is not None:
                raise LedgerError(f"{resolved.finding_id}: finding already {held_state_name(resolved.state)}")
            if not findings:
                return {}
            commit = self.synced_commit()
            tip = self._tip(commit)
            self._catalog = rule_catalog.load_rules(self.repo)
            definitions: dict[str, Rule | UnknownRule] = {}
            for rule in sorted({finding.rule for finding in findings}):
                try:
                    definitions[rule] = self._component_rule(rule)
                except LedgerError:
                    definitions[rule] = UnknownRule()

            def still_held() -> set[tuple[str, str]]:
                """The cells that a finding outside this decline still holds."""
                remaining = cast(list[tuple[str, str, str]], self._connection.execute(
                    "SELECT finding_id,rule,path FROM held_findings WHERE state NOT IN ('waived','built','withdrawn')",
                ).fetchall())
                return {(rule, _finding_cell(definition, path, tip)) for finding_id, rule, path in remaining
                        if finding_id not in ids and rule in definitions and isinstance(definition := definitions[rule], Rule)}

            cells: dict[tuple[str, str], tuple[Rule, list[HeldFinding]]] = {}
            outcomes: dict[str, DeclineOutcome] = {}
            for finding in findings:
                definition = definitions[finding.rule]
                if isinstance(definition, UnknownRule):
                    outcomes[finding.finding_id] = InputChangedSinceJudged()
                else:
                    cells.setdefault((finding.rule, _finding_cell(definition, finding.path, tip)), (definition, []))[1].append(finding)
            held_by_others = still_held() & cells.keys()
            reads = {cell: self.newest_held_judgment(*cell) for cell in cells}
            decisions = {cell: self.decline_outcome(reads[cell], cell[1], definition, tip, members, cell in held_by_others)
                         for cell, (definition, members) in cells.items()}
            with self._connection:
                _ = self._connection.execute("BEGIN IMMEDIATE")
                if (tuple(self._held(finding_id) for finding_id in ids) != findings or self.synced_commit() != commit
                        or still_held() & cells.keys() != held_by_others
                        or any(self.newest_held_judgment(*cell) != read for cell, read in reads.items())):
                    raise ChangedBeforeLock("the findings or their judgments changed while they were declined; rerun")
                now = _now()
                _ = self._connection.executemany("UPDATE held_findings SET state='waived',updated_at=? WHERE finding_id=?",
                                                 [(now, finding.finding_id) for finding in findings])
                for cell, read in reads.items():
                    if isinstance(read, NewestHeldJudgment) and isinstance(decisions[cell], CellWaived):
                        _ = self._connection.execute(
                            "UPDATE judgments SET verdict='waived',verdict_detail=?,recorded_at=? WHERE id=?",
                            (words, _now(), read.judgment_id),
                        )
            for cell, (_, members) in cells.items():
                for finding in members:
                    outcomes[finding.finding_id] = decisions[cell]
            return {finding.finding_id: outcomes[finding.finding_id] for finding in findings}

        return retry_on_change(attempt)

    def newest_held_judgment(self, rule: str, path: str) -> NewestHeldJudgment | NoHeldJudgment:
        rows = cast(list[tuple[int, str, str, str, str, str, str, str, str, str]], self._connection.execute(
            'SELECT id,rule,path,"commit",ticket,worker,rule_blob,input_scope,input_fingerprint,declared_scope FROM judgments '
            + "WHERE rule=? AND path=? AND verdict='held' ORDER BY recorded_at DESC,id DESC LIMIT 1",
            (rule, path),
        ).fetchall())
        if not rows:
            return NoHeldJudgment()
        row = rows[0]
        newest = cast(tuple[int], self._connection.execute(
            "SELECT id FROM judgments WHERE rule=? AND path=? ORDER BY recorded_at DESC,id DESC LIMIT 1",
            (rule, path),
        ).fetchone())[0]
        inputs = Judgment(row[6], row[8], row[3], "held", CellInputScope(row[7]), DeclaredInputScope(row[9]))
        return NewestHeldJudgment(row[0], HeldJudgment(*row[1:7]), inputs, newest)

    def decline_outcome(self, held: NewestHeldJudgment | NoHeldJudgment, path: str, definition: Rule, tip: RepositoryTip,
                        members: list[HeldFinding], held_by_others: bool) -> DeclineOutcome:
        """What declining the members does to the cell's held judgment; it writes nothing."""
        if isinstance(held, NoHeldJudgment):
            return held
        try:
            unchanged = (self._judgment_inputs_match(held.inputs, tip, _cell_scope(definition, path, tip), path)
                         and self._is_ancestor(held.inputs.commit, tip.commit))
        except LedgerError:
            unchanged = False
        if (held.cell_newest_id != held.judgment_id or held.inputs.rule_blob != definition.blob
                or any(finding.rule_blob != definition.blob for finding in members) or not unchanged):
            return InputChangedSinceJudged()
        if held_by_others:
            return CellStillHeld(held.judgment)
        return CellWaived(held.judgment)

    def file_held(self, finding_id: str, proposal: Path) -> None:
        with self._connection:
            _ = self._connection.execute("BEGIN IMMEDIATE")
            finding = self._held(finding_id)
            if isinstance(finding.state, Filed) and finding.state.proposal == proposal:
                return
            if not isinstance(finding.state, Unfiled):
                raise LedgerError(f"{finding_id}: cannot file finding in state {held_state_name(finding.state)}")
            _ = self._connection.execute("UPDATE held_findings SET state='filed',proposal=?,updated_at=? WHERE finding_id=?", (str(proposal), _now(), finding_id))

    def _changed_rule_findings(self, definitions: Mapping[str, Rule]) -> tuple[tuple[HeldFinding, Rule], ...]:
        """Each filed finding whose rule's current text is not the text it was judged under, with that rule.

        Filing skips an unfiled finding by the same lookup.
        """
        changed: list[tuple[HeldFinding, Rule]] = []
        for finding in self.held_findings(HeldFindingsInState("filed")):
            definition = definitions.get(finding.rule.removesuffix("#uncovered"))
            if definition is not None and definition.blob != finding.rule_blob:
                changed.append((finding, definition))
        return tuple(changed)

    def _superseded(self, definitions: Mapping[str, Rule], tip: RepositoryTip) -> Withdrawal:
        withdrawn: list[WithdrawnFinding] = []
        kept: list[KeptForBuild] = []
        for finding, definition in self._changed_rule_findings(definitions):
            # The verdict must also be newer than the finding's own judgment, so an old exception under a text
            # that the rule returned to never withdraws a finding judged after it.
            rows = cast(list[tuple[int, Verdict, str]], self._connection.execute(
                "SELECT id,verdict,rule_blob FROM judgments WHERE rule=? AND path=? AND rule_blob IN (?,?) "
                + "ORDER BY recorded_at DESC,id DESC LIMIT 1",
                (finding.rule, _finding_cell(definition, finding.path, tip), definition.blob, finding.rule_blob),
            ).fetchall())
            if not rows or rows[0][1:] != ("exception", definition.blob):
                continue
            verdict = SupersedingException(rows[0][0], definition.blob)
            if isinstance(finding.build_ticket, BuildTicket):
                ticket = self._ticket(finding.build_ticket.ticket)
                if ticket["state"] not in FINAL_TICKET_STATES:
                    kept.append(KeptForBuild(finding, verdict, ticket["id"]))
                    continue
            withdrawn.append(WithdrawnFinding(finding, verdict))
        return Withdrawal(tuple(withdrawn), tuple(kept))

    def superseded_findings(self, definitions: Mapping[str, Rule]) -> Withdrawal:
        """What withdraw_superseded withdraws and keeps; it writes nothing.

        definitions maps each rule id to its current catalog rule. The synced tip loads only when a filed
        finding's rule text changed.
        """
        if not self._changed_rule_findings(definitions):
            return Withdrawal((), ())
        return self._superseded(definitions, self._synced_tip())

    def withdraw_superseded(self, definitions: Mapping[str, Rule]) -> Withdrawal:
        """Withdraw each filed finding whose cell's newest judgment is an exception under its rule's current text.

        A finding that a ticket which is not final builds stays filed and is returned as kept. The plan comes
        before the write lock; if the rows it read change before the lock, the withdrawal starts again.
        """

        def attempt() -> Withdrawal:
            if not self._changed_rule_findings(definitions):
                return Withdrawal((), ())
            tip = self._synced_tip()
            plan = self._superseded(definitions, tip)
            if not plan.withdrawn:
                return plan
            with self._connection:
                _ = self._connection.execute("BEGIN IMMEDIATE")
                if self.synced_commit() != tip.commit or self._superseded(definitions, tip) != plan:
                    raise ChangedBeforeLock("the findings or their judgments changed while they were withdrawn; rerun")
                now = _now()
                _ = self._connection.executemany(
                    "UPDATE held_findings SET state='withdrawn',withdrawn_by=?,updated_at=? WHERE finding_id=?",
                    [(item.verdict.judgment_id, now, item.finding.finding_id) for item in plan.withdrawn],
                )
            return plan

        return retry_on_change(attempt)

    def _changed_since(self, commit: str, tip: str) -> frozenset[str]:
        key = (commit, tip)
        if key not in self._changed_paths:
            changed = _git(self.repo, "diff", "--no-renames", "--name-only", "-z", commit, tip)
            self._changed_paths[key] = frozenset(path for path in changed.split("\0") if path)
        return self._changed_paths[key]

    def _files_naming(self, tip: str, name: str) -> tuple[str, ...]:
        """The `.rs` files at tip that hold name as a whole word."""
        key = (tip, name)
        if key not in self._named_files:
            result = subprocess.run(["git", "-C", str(self.repo), "grep", "-l", "-z", "-w", "-F", "-e", name, tip, "--", "*.rs"],
                                    capture_output=True, text=True, check=False)
            # git grep exits 1 when no file matches.
            if result.returncode not in (0, 1):
                raise CheckCouldNotRun(result.stderr.strip() or f"git grep {name} {tip} failed")
            # Each match reads `<tip>:<path>`.
            self._named_files[key] = tuple(entry.removeprefix(f"{tip}:") for entry in result.stdout.split("\0") if entry)
        return self._named_files[key]

    def _commit_file(self, commit: str, path: str) -> bytes | Folder | MissingFile:
        """What _file_at reads at a resolved commit, read once."""
        key = (commit, path)
        if key not in self._commit_files:
            self._commit_files[key] = self._file_at(commit, path)
        return self._commit_files[key]

    def _declared_file(self, commit: str, path: str) -> DeclaredFile:
        """The items of path at commit. A path that is not a file there reads as no text, so it declares no item."""
        key = (commit, path)
        if key not in self._declared_files:
            content = self._commit_file(commit, path)
            self._declared_files[key] = read_declarations(content.decode(errors="replace") if isinstance(content, bytes) else "")
        return self._declared_files[key]

    def _declaration_outcome(self, finding: HeldFinding, tip: RepositoryTip, rust_files: frozenset[str],
                             approved: Callable[[Path], bool], renames: tuple[BuiltRename, ...]) -> DeclarationOutcome:
        if not isinstance(finding.state, (Unfiled, Filed)) or PurePosixPath(finding.path).suffix != ".rs":
            return DeclarationNotRead(finding)
        if isinstance(finding.state, Unfiled):
            judging = cast(list[tuple[TicketState]], self._connection.execute(
                "SELECT state FROM tickets WHERE id=?", (finding.ticket,)).fetchall())
            if judging and judging[0][0] in ("assigned", "checkpointed"):
                return DeclarationNotRead(finding)
        # A followed finding's path and line are at the commit of its last follow.
        if isinstance(finding.follow, FollowedDeclaration):
            read = finding.follow.commit
        else:
            judged = self.judged_commit(finding)
            if isinstance(judged, NoJudgedCommit):
                return DeclarationNotRead(finding)
            read = judged.commit
        if not self._is_ancestor(read, tip.commit):
            return DeclarationNotRead(finding)
        changed = self._changed_since(read, tip.commit)
        if finding.path not in changed:
            return DeclarationInPlace(finding)
        old_file = self._declared_file(read, finding.path)
        old = old_file.declaration_at(finding.line)
        if not isinstance(old, DeclaredItem):
            return DeclarationNotRead(finding)
        declaration = old.declaration
        if any(item.declaration == declaration for item in self._declared_file(tip.commit, finding.path).declared_items):
            return DeclarationInPlace(finding)
        # A file the declaration moved to gained it since the read commit; one that already declared it holds a
        # different item of the same identity.
        found: dict[str, DeclaredItem] = {}
        for path in self._files_naming(tip.commit, declaration.name):
            if path == finding.path or path not in rust_files or path not in changed:
                continue
            if any(item.declaration == declaration for item in self._declared_file(read, path).declared_items):
                continue
            item = next((item for item in self._declared_file(tip.commit, path).declared_items if item.declaration == declaration), None)
            if item is not None:
                found[path] = item
        if len(found) > 1:
            return DeclaredInSeveralFiles(finding, declaration, tuple(sorted(found)))
        for path in found:
            if isinstance(finding.state, Filed) and tip.crate(path) != tip.crate(finding.path):
                return DeclaredInAnotherCrate(finding, declaration, path)
        # The open build holds the finding whatever the rename check would find, so the check does not run.
        if isinstance(finding.build_ticket, BuildTicket):
            ticket = self._ticket(finding.build_ticket.ticket)
            if ticket["state"] not in FINAL_TICKET_STATES:
                return KeptForOpenBuild(finding, ticket["id"])
        if found:
            path, item = next(iter(found.items()))
            return DeclarationMoved(finding, declaration, path,
                                    moved_site_line(old_file, finding.line, old, self._declared_file(tip.commit, path), item))
        after_builds = self._built_renaming(finding, read, declaration, tip, approved, renames)
        renamed = self._renamed_site(finding, read, old_file, old, after_builds, tip, rust_files, changed, approved)
        if not isinstance(renamed, NoRenamedSite):
            return renamed
        if isinstance(after_builds, RenamedByBuilds):
            followed = self._followed_rename(finding, read, old_file, old, after_builds, tip, rust_files, changed)
            if not isinstance(followed, NoRenamedSite):
                return followed
        if isinstance(finding.state, Filed) and finding.path in tip.blobs:
            return DeclarationGoneFromFile(finding, declaration)
        return DeclarationRemoved(finding, declaration)

    def _renamed_site(self, finding: HeldFinding, read: str, old_file: DeclaredFile, old: DeclaredItem,
                      after_builds: BuiltRenaming, tip: RepositoryTip, rust_files: frozenset[str], changed: frozenset[str],
                      approved: Callable[[Path], bool],
                      ) -> DeclarationRenamed | RenameAwaitingApproval | DeclaredInSeveralFiles | DeclaredInAnotherCrate | NoRenamedSite:
        """Where a filed rename's after name is declared at the tip with the keyword and owner of its gone declaration,
        else with the owner that the built renames gave it.

        A file counts only when it gained that declaration since the read commit; this holds for the finding's own
        file too, which is checked first. Several files or a file in another crate leave the finding, as a move does.
        The applied check must not find the rename not applied at the site, so reopen does not file it again at once.
        A rename in a decision that the user has not approved waits for the answer.
        """
        declaration = old.declaration
        state = finding.state
        old_name = finding.before.strip().split("::")[-1]
        new_name = finding.after.strip().split("::")[-1]
        # A variant or field line reads as its enum or struct, so its before name is not the declaration's name.
        if (not isinstance(state, Filed) or finding.kind != "rename" or old_name != declaration.name
                or new_name == old_name or not style_footprint.IDENTIFIER.fullmatch(new_name)):
            return NoRenamedSite()
        renamed = Declaration(declaration.keyword, new_name, declaration.owner)
        found = self._gaining_files(finding, read, renamed, tip, rust_files, changed)
        # The owner's own name comes first: a built rename of another type of that name, or one that a later commit put
        # back, gives the owner a new name that the code does not use.
        if (not found and isinstance(after_builds, RenamedByBuilds)
                and after_builds.declaration.owner != declaration.owner):
            renamed = Declaration(declaration.keyword, new_name, after_builds.declaration.owner)
            found = self._gaining_files(finding, read, renamed, tip, rust_files, changed)
        if len(found) > 1:
            return DeclaredInSeveralFiles(finding, renamed, tuple(sorted(found)))
        if not found:
            return NoRenamedSite()
        path, item = next(iter(found.items()))
        if tip.crate(path) != tip.crate(finding.path):
            return DeclaredInAnotherCrate(finding, renamed, path)
        if isinstance(applied_check(finding.kind, finding.before, finding.after, path,
                                    lambda read_path: self._commit_file(tip.commit, read_path)), RenameNotApplied):
            return NoRenamedSite()
        if not approved(state.proposal):
            return RenameAwaitingApproval(finding)
        return DeclarationRenamed(finding, declaration, renamed, path,
                                  moved_site_line(old_file, finding.line, old, self._declared_file(tip.commit, path), item))

    def _built_renaming(self, finding: HeldFinding, read: str, declaration: Declaration, tip: RepositoryTip,
                        approved: Callable[[Path], bool], renames: tuple[BuiltRename, ...]) -> BuiltRenaming:
        """The new names that the built renames of approved decisions in the finding's crate gave its item and the impl
        or trait that holds it.

        Each name follows its renames one after another, and stops at a name with two new names or at a name it had
        before. A name keeps its old name when the finding's own rename names it. A rename names the item only when its
        scope holds the item: a module path names a module item in a file of that module, and a type path names an
        item of that type's impl or trait under any name the renames gave the type. A built `Self` path has the type
        of its own impl or trait.
        """
        own = _item_rename(finding.kind, finding.before, finding.after)
        crate = tip.crate(finding.path)
        module = _module_name(finding.path)
        # The built finding can sit at a call site in any file of the crate; the gaining-file search still guards it.
        in_crate = tuple(built_rename for built_rename in renames if tip.crate(built_rename.path) == crate)

        def renamed(name: str, holds_item: Callable[[RenameScope], bool]) -> tuple[str, ...]:
            """name, then each new name the renames gave it in turn, up to a name with two new names or a name the chain
            already holds; name alone when the finding's own rename names it."""
            names = (name,)
            if isinstance(own, ItemRename) and own.old == name:
                return names
            while new := {built_rename.rename.new for built_rename in in_crate if built_rename.rename.old == names[-1]
                          and holds_item(built_rename.rename.scope) and approved(built_rename.proposal)}:
                if len(new) > 1 or not new.isdisjoint(names):
                    return names
                names = (*names, *new)
            return names

        def module_item(scope: RenameScope) -> bool:
            return isinstance(scope, UnqualifiedName) or (isinstance(scope, ModuleQualified) and scope.module == module)

        owner = declaration.owner
        if not isinstance(owner, ImplOrTraitItem):
            names = renamed(declaration.name, module_item)
            if len(names) == 1:
                return NoBuiltRename()
            return RenamedByBuilds(Declaration(declaration.keyword, names[-1], owner))
        # The impl's type or the trait is a module item. A trait impl's owner is its type, so a trait path does not
        # reach the items of a trait impl.
        def owner_item(scope: RenameScope) -> bool:
            if module_item(scope):
                return True
            if not isinstance(scope, ModuleQualified):
                return False
            return any(tip.crate(path) == crate and _module_name(path) == scope.module
                       and any(item.declaration.name == owner.name
                               and item.declaration.keyword in ("struct", "enum", "union", "trait", "type")
                               and not isinstance(item.declaration.owner, ImplOrTraitItem)
                               for item in self._declared_file(read, path).declared_items)
                       for path in self._files_naming(read, owner.name))

        owners = renamed(owner.name, owner_item)

        def impl_or_trait_item(scope: RenameScope) -> bool:
            return (isinstance(scope, UnqualifiedName)
                    or (isinstance(scope, TypeQualified) and scope.type_name in owners))

        names = renamed(declaration.name, impl_or_trait_item)
        if len(names) == 1 and len(owners) == 1:
            return NoBuiltRename()
        return RenamedByBuilds(Declaration(declaration.keyword, names[-1], ImplOrTraitItem(owners[-1])))

    def _followed_rename(self, finding: HeldFinding, read: str, old_file: DeclaredFile, old: DeclaredItem,
                         after_builds: RenamedByBuilds, tip: RepositoryTip, rust_files: frozenset[str], changed: frozenset[str],
                         ) -> DeclarationMoved | DeclaredInSeveralFiles | DeclaredInAnotherCrate | NoRenamedSite:
        """Where the finding's item is declared at the tip under the new names that the built renames gave it.

        Several gaining files or a filed finding's file in another crate leave the finding, as a move does.
        """
        renamed = after_builds.declaration
        found = self._gaining_files(finding, read, renamed, tip, rust_files, changed)
        if len(found) > 1:
            return DeclaredInSeveralFiles(finding, renamed, tuple(sorted(found)))
        if not found:
            return NoRenamedSite()
        path, item = next(iter(found.items()))
        if isinstance(finding.state, Filed) and tip.crate(path) != tip.crate(finding.path):
            return DeclaredInAnotherCrate(finding, renamed, path)
        return DeclarationMoved(finding, renamed, path,
                                moved_site_line(old_file, finding.line, old, self._declared_file(tip.commit, path), item))

    def _gaining_files(self, finding: HeldFinding, read: str, renamed: Declaration, tip: RepositoryTip,
                       rust_files: frozenset[str], changed: frozenset[str]) -> dict[str, DeclaredItem]:
        """The files that gained renamed since the read commit, with its item there.

        The finding's own file is checked first; when it gained renamed, it is the only file.
        """

        def gained(path: str) -> tuple[DeclaredItem, ...]:
            """The items of path that declare renamed at the tip; none when path declared it at the read commit."""
            if any(item.declaration == renamed for item in self._declared_file(read, path).declared_items):
                return ()
            return tuple(item for item in self._declared_file(tip.commit, path).declared_items if item.declaration == renamed)

        if own := gained(finding.path):
            return {finding.path: own[0]}
        found: dict[str, DeclaredItem] = {}
        for path in self._files_naming(tip.commit, renamed.name):
            if path != finding.path and path in rust_files and path in changed and (items := gained(path)):
                found[path] = items[0]
        return found

    def _declaration_reading(self, finding_ids: tuple[str, ...], tip: RepositoryTip,
                             approved: Callable[[Path], bool]) -> DeclarationReading:
        rust_files = frozenset(tip.rust_files)
        built_renames: list[BuiltRename] = []
        for finding in self.held_findings(HeldFindingsInState("built")):
            rename = _item_rename(finding.kind, finding.before, finding.after)
            if isinstance(finding.state, Built) and isinstance(rename, ItemRename):
                if isinstance(rename.scope, SelfQualified):
                    read = JudgedCommit(finding.follow.commit) if isinstance(finding.follow, FollowedDeclaration) else self.judged_commit(finding)
                    if isinstance(read, NoJudgedCommit):
                        continue
                    item = self._declared_file(read.commit, finding.path).declaration_at(finding.line)
                    if not isinstance(item, DeclaredItem) or not isinstance(item.declaration.owner, ImplOrTraitItem):
                        continue
                    rename = ItemRename(rename.old, rename.new, TypeQualified(item.declaration.owner.name))
                built_renames.append(BuiltRename(rename, finding.path, finding.state.proposal))
        renames = tuple(built_renames)
        return DeclarationReading(tip.commit, tuple(self._declaration_outcome(self._held(finding_id), tip, rust_files, approved, renames)
                                                    for finding_id in finding_ids))

    def declaration_reading(self, finding_ids: Iterable[str], approved: Callable[[Path], bool]) -> DeclarationReading:
        """What follow_declarations writes for the findings at the synced tip; it writes nothing.

        approved tells whether the user approved the decision at a path.
        """
        return self._declaration_reading(tuple(finding_ids), self._synced_tip(), approved)

    def follow_declarations(self, finding_ids: Iterable[str], at: str, approved: Callable[[Path], bool]) -> DeclarationReading:
        """Move each finding whose declaration moved to one other file, or whose item a built rename renamed, there,
        build each filed rename whose new name is declared and whose decision is approved, and withdraw each finding
        whose declaration is gone when it is unfiled or its file is gone.

        at is the synced tip of the caller's reading; when the synced tip is no longer at, the step refuses. A filed
        finding whose item is gone from its file stays filed. approved tells whether the user approved the decision
        at a path, and it is read again under the write lock. The plan comes before the write lock; if a row it read
        changes before the lock, the step starts again. A git failure stops before any write.
        """
        ids = tuple(finding_ids)

        def moved_tip() -> LedgerError:
            return LedgerError(f"the synced tip moved from {at} since the declarations were read; rerun")

        def attempt() -> DeclarationReading:
            if self.synced_commit() != at:
                raise moved_tip()
            tip = self._tip(at)
            plan = self._declaration_reading(ids, tip, approved)
            moves = [item for item in plan.outcomes if isinstance(item, DeclarationMoved)]
            renames = [item for item in plan.outcomes if isinstance(item, DeclarationRenamed)]
            removals = [item for item in plan.outcomes if isinstance(item, DeclarationRemoved)]
            if not moves and not renames and not removals:
                return plan
            with self._connection:
                _ = self._connection.execute("BEGIN IMMEDIATE")
                if self.synced_commit() != at:
                    raise moved_tip()
                if self._declaration_reading(ids, tip, approved) != plan:
                    raise ChangedBeforeLock("the findings or their decisions changed while their declarations were read; rerun")
                now = _now()
                _ = self._connection.executemany(
                    "UPDATE held_findings SET path=?,line=?,followed_from=?,followed_from_line=?,followed_at=?,updated_at=? WHERE finding_id=?",
                    [(item.path, item.line, item.finding.path, item.finding.line, tip.commit, now, item.finding.finding_id)
                     for item in moves],
                )
                # A person's reading was of an old build, so the new build clears it. A rename follows to its new
                # declaration, in its own file too, so reopen checks that site and its line reads at the tip.
                _ = self._connection.executemany(
                    "UPDATE held_findings SET state='built',person_read_by='',path=?,line=?,followed_from=?,followed_from_line=?,"
                    + "followed_at=?,updated_at=? WHERE finding_id=?",
                    [(item.path, item.line, item.finding.path, item.finding.line, tip.commit, now, item.finding.finding_id)
                     for item in renames],
                )
                _ = self._connection.executemany(
                    "UPDATE held_findings SET state='withdrawn',declaration_gone_at=?,updated_at=? WHERE finding_id=?",
                    [(tip.commit, now, item.finding.finding_id) for item in removals],
                )
            return plan

        return retry_on_change(attempt)

    def mark_built(self, finding_id: str) -> None:
        """Mark a filed finding built after a person checked that its change is in the code.

        The finding needs a merged build ticket, and a rename must be applied at that ticket's checkpoint.
        The git check runs before the write lock; if the finding or its ticket change before the lock, it starts again.
        """

        def merged_build_ticket(finding: HeldFinding) -> TicketRecord:
            """The finding's merged build ticket; refuses a finding that is not filed or has none."""
            if not isinstance(finding.state, Filed):
                raise LedgerError(f"{finding_id}: cannot mark a {held_state_name(finding.state)} finding built")
            if not isinstance(finding.build_ticket, BuildTicket):
                raise LedgerError(f"{finding_id}: no merged build ticket")
            ticket = self._ticket(finding.build_ticket.ticket)
            if ticket["state"] not in FINAL_TICKET_STATES:
                raise LedgerError(f"{finding_id}: build ticket {ticket['id']} still holds the finding")
            if ticket["state"] != "merged":
                raise LedgerError(f"{finding_id}: no merged build ticket")
            return ticket

        def attempt() -> None:
            finding = self._held(finding_id)
            if isinstance(finding.state, Built):
                return
            ticket = merged_build_ticket(finding)
            check = self.applied_check(finding, ticket["checkpoint"])
            if isinstance(check, RenameNotApplied):
                raise LedgerError(f"{finding_id}: rename not applied in {check.path} at {ticket['id']} checkpoint")
            with self._connection:
                _ = self._connection.execute("BEGIN IMMEDIATE")
                current = self._held(finding_id)
                if isinstance(current.state, Built):
                    return
                if current != finding or merged_build_ticket(current) != ticket:
                    raise ChangedBeforeLock(f"{finding_id}: the finding changed while it was marked built; rerun")
                # A person's reading was of the old build, so a new build clears it.
                _ = self._connection.execute("UPDATE held_findings SET state='built',person_read_by='',updated_at=? WHERE finding_id=?",
                                             (_now(), finding_id))

        return retry_on_change(attempt)

    def mark_built_at_merge(self, names: tuple[str, ...], merge: str, reading: PersonReading) -> tuple[BuiltAtMerge, ...]:
        """Mark filed findings built after a plain merge put their change in the code, with no build ticket.

        Each name is a finding id or a unique prefix of one. The merge must be on the synced branch and must not be
        older than the commit each finding was judged at, and the applied check runs at the merge. A rename that is not applied is refused. A finding that the check cannot prove needs
        a person's reading, and the row keeps it; a proven rename clears it. Every refusal comes before the write.
        The git work runs before the write lock; if a finding changes before the lock, it starts again.
        """
        try:
            commit = _git(self.repo, "rev-parse", "--verify", f"{merge}^{{commit}}")
        except CheckCouldNotRun as error:
            raise LedgerError(f"{merge}: no such commit") from error
        tip = self.synced_commit()
        if not self._is_ancestor(commit, tip):
            raise LedgerError(f"{merge} is not on the synced branch at {tip[:7]}")

        def named(name: str, identities: list[str]) -> HeldFinding:
            """The one filed finding that name names; refuses a name that is not one."""
            finding = self._named_finding(name, identities)
            if not isinstance(finding.state, Filed):
                raise LedgerError(f"{finding.finding_id}: cannot mark a {held_state_name(finding.state)} finding built")
            self._refuse_open_build_ticket(finding)
            return finding

        def attempt() -> tuple[BuiltAtMerge, ...]:
            identities = self._held_identities()
            findings = {finding.finding_id: finding for finding in (named(name, identities) for name in names)}
            built: list[BuiltAtMerge] = []
            for finding_id, finding in sorted(findings.items()):
                # A merge older than the finding's judgment cannot hold its change.
                judged = self.judged_commit(finding)
                if isinstance(judged, JudgedCommit) and judged.commit != commit and self._is_ancestor(commit, judged.commit):
                    raise LedgerError(f"{finding_id}: {merge} is older than {judged.commit[:7]}, where the finding was judged")
                check = self.applied_check(finding, commit)
                match check:
                    case AppliedInCode():
                        built.append(BuiltAtMerge(finding_id, commit, NotReadByPerson()))
                    case RenameNotApplied():
                        raise LedgerError(f"{finding_id}: rename not applied in {check.path} at {merge}")
                    case NoAppliedCheck():
                        if isinstance(reading, NotReadByPerson):
                            raise LedgerError(f"{finding_id}: {check.kind} has no applied check at {merge}; "
                                              + "pass --by after reading the code")
                        built.append(BuiltAtMerge(finding_id, commit, reading))
            with self._connection:
                _ = self._connection.execute("BEGIN IMMEDIATE")
                if any(self._held(finding_id) != finding for finding_id, finding in findings.items()):
                    raise ChangedBeforeLock(f"{', '.join(sorted(findings))}: a finding changed while it was marked built; rerun")
                now = _now()
                for item in built:
                    by = item.reading.by if isinstance(item.reading, ReadByPerson) else ""
                    _ = self._connection.execute(
                        "UPDATE held_findings SET state='built',person_read_by=?,updated_at=? WHERE finding_id=?",
                        (by, now, item.finding_id),
                    )
            return tuple(built)

        return retry_on_change(attempt)

    def _held_identities(self) -> list[str]:
        return [row[0] for row in cast(list[tuple[str]], self._connection.execute("SELECT finding_id FROM held_findings").fetchall())]

    def _named_finding(self, name: str, identities: list[str]) -> HeldFinding:
        """The one held finding that name, a finding id or a unique prefix of one, names; refuses a name that is not one."""
        matches = [identity for identity in identities if identity.startswith(name)]
        if not matches:
            raise LedgerError(f"no finding {name}")
        if len(matches) > 1:
            raise LedgerError(f"{name} matches {len(matches)} findings")
        return self._held(matches[0])

    def _refuse_open_build_ticket(self, finding: HeldFinding) -> None:
        if isinstance(finding.build_ticket, BuildTicket):
            ticket = self._ticket(finding.build_ticket.ticket)
            if ticket["state"] not in FINAL_TICKET_STATES:
                raise LedgerError(f"{finding.finding_id}: build ticket {ticket['id']} still holds the finding")

    def named_held(self, names: Iterable[str]) -> tuple[HeldFinding, ...]:
        """The held findings that the names name, each once and in finding-id order; refuses a name that is not one."""
        identities = self._held_identities()
        findings = {finding.finding_id: finding for finding in (self._named_finding(name, identities) for name in names)}
        return tuple(findings[finding_id] for finding_id in sorted(findings))

    def unwaive_held(self, names: tuple[str, ...], person: WaiveUndoneBy) -> tuple[str, ...]:
        """Set waived findings back to unfiled, with no decision and no build ticket, and record who did it.

        Each name is a finding id or a unique prefix of one. A finding that an open ticket judged or builds, or whose
        rule text changed since it was judged, is refused; style_proposals.unwaive also refuses a finding that its
        declined decision waives again. Every refusal comes before the write. A cell whose newest judgment a decline
        set to waived is held again.
        """
        with self._connection:
            _ = self._connection.execute("BEGIN IMMEDIATE")
            findings = self.named_held(names)
            self._catalog = rule_catalog.load_rules(self.repo)
            definitions = {rule.rule_id: rule for rule in self._catalog}
            for finding in findings:
                if not isinstance(finding.state, Waived):
                    raise LedgerError(f"{finding.finding_id}: cannot unwaive a {held_state_name(finding.state)} finding")
                # Filing skips such a finding, so it would stay unfiled.
                if rule_changed_since_judged(finding, definitions):
                    raise LedgerError(f"{finding.finding_id}: rule changed since judged")
                self._refuse_open_build_ticket(finding)
                # An unfiled finding of an open ticket waits for that ticket, and a yield of its file deletes it.
                judging = cast(list[tuple[str]], self._connection.execute(
                    "SELECT state FROM tickets WHERE id=? AND state IN ('assigned','checkpointed')", (finding.ticket,),
                ).fetchall())
                if judging:
                    raise LedgerError(f"{finding.finding_id}: {judging[0][0]} ticket {finding.ticket} still holds the finding")
            tip = self._synced_tip()
            cells = {(finding.rule, _finding_cell(self._component_rule(finding.rule), finding.path, tip)) for finding in findings}
            now = _now()
            _ = self._connection.executemany(
                "UPDATE held_findings SET state='unfiled',proposal='',build_ticket='',moved_from='',unwaived_by=?,updated_at=? WHERE finding_id=?",
                [(person.by, now, finding.finding_id) for finding in findings],
            )
            for rule, path in sorted(cells):
                newest = cast(list[tuple[int, Verdict]], self._connection.execute(
                    "SELECT id,verdict FROM judgments WHERE rule=? AND path=? ORDER BY recorded_at DESC,id DESC LIMIT 1", (rule, path),
                ).fetchall())
                if newest and newest[0][1] == "waived":
                    _ = self._connection.execute("UPDATE judgments SET verdict='held',verdict_detail=?,recorded_at=? WHERE id=?",
                                                 (f"unwaived by {person.by}", now, newest[0][0]))
        return tuple(finding.finding_id for finding in findings)

    def return_to_filed(self, refilings: Mapping[str, PersonReading]) -> None:
        """Set built findings whose change is not in the code back to filed, with no build ticket.

        refilings maps each finding to who found the change not in the code: the applied check, or a person.
        A person's reading stays on the row after later changes.
        """
        with self._connection:
            _ = self._connection.execute("BEGIN IMMEDIATE")
            now = _now()
            for finding_id in sorted(refilings):
                if not isinstance(self._held(finding_id).state, Built):
                    raise LedgerError(f"{finding_id}: finding is not built")
                _ = self._connection.execute(
                    "UPDATE held_findings SET state='filed',build_ticket='',updated_at=? WHERE finding_id=?", (now, finding_id),
                )
                reading = refilings[finding_id]
                if isinstance(reading, ReadByPerson):
                    _ = self._connection.execute(
                        "UPDATE held_findings SET person_read_by=? WHERE finding_id=?", (reading.by, finding_id),
                    )

    def record_person_applied(self, readings: Mapping[str, ReadByPerson]) -> None:
        """Record each person who read the code and found the change of a built finding in it; the findings stay built."""
        with self._connection:
            _ = self._connection.execute("BEGIN IMMEDIATE")
            now = _now()
            for finding_id in sorted(readings):
                if not isinstance(self._held(finding_id).state, Built):
                    raise LedgerError(f"{finding_id}: finding is not built")
                _ = self._connection.execute(
                    "UPDATE held_findings SET person_read_by=?,updated_at=? WHERE finding_id=?",
                    (readings[finding_id].by, now, finding_id),
                )

    def waive_held(self, finding_id: str) -> None:
        with self._connection:
            _ = self._connection.execute("BEGIN IMMEDIATE")
            finding = self._held(finding_id)
            # A withdrawn finding keeps the verdict that withdrew it.
            if isinstance(finding.state, (Waived, Withdrawn)):
                return
            _ = self._connection.execute("UPDATE held_findings SET state='waived',updated_at=? WHERE finding_id=?",
                                         (_now(), finding_id))

    def reconcile(self, branch_point: str, style_branch: str) -> tuple[str, ...]:
        branch_tip = pushed_branch_point_tip(self.repo, branch_point)
        style_branch = "refs/heads/" + style_branch.removeprefix("refs/heads/")
        present = subprocess.run(
            ["git", "-C", str(self.repo), "show-ref", "--verify", "--quiet", style_branch],
            capture_output=True, text=True, check=False,
        )
        if present.returncode == 1:
            style_tip = branch_tip
        elif present.returncode:
            raise LedgerError(present.stderr.strip())
        else:
            style_tip = _git(self.repo, "rev-parse", "--verify", f"{style_branch}^{{commit}}")
        changes: list[str] = []
        sync = cast(list[tuple[str, str]], self._connection.execute("SELECT tip,style_branch FROM sync WHERE id=1").fetchall())
        if not sync or sync[0][1] != style_branch:
            self.set_tip(style_tip, style_branch)
            changes.append(f"style branch {style_branch}")
        with self._connection:
            _ = self._connection.execute("INSERT INTO reconciliation VALUES (1,?) ON CONFLICT(id) DO UPDATE SET branch_point=excluded.branch_point", (branch_point,))
        for handoff in self.pending_handoffs():
            checkpoint = handoff["checkpoint"]
            if self._is_ancestor(checkpoint, branch_tip):
                with self._connection:
                    _ = self._connection.execute("UPDATE handoffs SET state='merged',merge_commit=?,updated_at=? WHERE checkpoint=?",
                                                 (self._first_merge(checkpoint, branch_point), _now(), checkpoint))
                changes.append(f"hand-off {checkpoint}: {handoff['state']} -> merged")
        tickets = cast(list[tuple[str, str, str, str]], self._connection.execute("SELECT id,branch,state,checkpoint FROM tickets WHERE state IN ('assigned','checkpointed') ORDER BY id").fetchall())
        for id, branch, state, checkpoint in tickets:
            if state == "checkpointed" and self._is_ancestor(checkpoint, style_tip):
                not_built = self.ticket_merge(id)
                changes.append(f"ticket {id}: checkpointed -> merged")
                changes.extend(not_built_line(self.repo, item) for item in not_built)
            elif state == "assigned":
                result = subprocess.run(["git", "-C", str(self.repo), "show-ref", "--verify", "--quiet", branch], check=False, capture_output=True)
                if result.returncode == 1:
                    self.ticket_drop(id)
                    changes.append(f"ticket {id}: assigned -> dropped")
                elif result.returncode:
                    raise LedgerError(result.stderr.decode(errors="replace").strip())
        return tuple(changes)

    def summary(self) -> StyleLedgerSummary:
        return self.summary_of(self.rule_counts(WholeRepository()))

    def summary_of(self, repository_counts: tuple[RuleCounts, ...]) -> StyleLedgerSummary:
        progress = tuple(rule for rule in repository_counts if rule.tier in ("A", "B", "C", "D"))
        total = sum(rule.total for rule in repository_counts)
        waiting_rows = self.pending_handoffs()
        waiting = PendingHandoffs(len(waiting_rows), datetime.fromisoformat(waiting_rows[0]["created_at"])) if waiting_rows else NoPendingHandoffs()
        proposals = cast(tuple[int], self._connection.execute("SELECT COUNT(DISTINCT proposal) FROM held_findings WHERE state='filed'").fetchone())[0]
        return StyleLedgerSummary(
            sum(rule.complete for rule in progress), len(progress), sum(rule.tier == "unclassified" for rule in self._catalog),
            100.0 * sum(rule.current for rule in repository_counts) / total if total else 0.0,
            sum(rule.stale for rule in repository_counts), proposals, waiting, self.synced_at(), self._synced_tip().commit,
        )

    def _refresh_sites(self, tip: RepositoryTip) -> None:
        definitions = [
            rule for rule in self._catalog
            if (rule.tier in ("B", "D") and rule.candidates is not None)
            or (rule.tier == "B" and rule.pre_filter is not None)
        ]
        markers = cast(list[tuple[str, str]], self._connection.execute(
            "SELECT rule,rule_blob FROM listed_rules WHERE tip=?", (tip.commit,),
        ).fetchall())
        if set(markers) == {(rule.rule_id, rule.blob) for rule in definitions}:
            return

        # Refresh together: pre-filter rules also see non-Rust files named by generators.
        sites: set[ListedSite] = set()
        with tempfile.TemporaryDirectory(prefix="style-listed-") as scratch:
            root = Path(scratch)
            export = root / "export"
            export.mkdir()
            _export_blobs(self.repo, tip, export)
            ignored: list[str] = []
            for config in (".claude/config/berth.toml", "rustfmt.toml", ".rustfmt.toml"):
                if config in tip.blobs:
                    ignored.extend(_ignored_paths(_table(tomllib.loads(_git(self.repo, "show", f"{tip.commit}:{config}")))))
            for definition in definitions:
                if definition.candidates is None:
                    continue
                try:
                    enumeration = enumerate_candidates(definition.candidates, export)
                except (SystemExit, re.error) as error:
                    raise LedgerError(f"{definition.rule_id}: {error}") from error
                for candidate in enumeration.candidates:
                    path = self._path(candidate.file)
                    if path not in tip.blobs or _is_ignored(path, tuple(ignored)):
                        continue
                    if isinstance(definition.reach, MemberReach) and not _under(path, definition.reach.crate_dir.as_posix()):
                        continue
                    sites.add(ListedSite(definition.rule_id, path, tip.crate(path), candidate.line, candidate.text))
            universe = set(tip.rust_files) | {site.path for site in sites}
            for definition in definitions:
                if definition.candidates is not None or definition.pre_filter is None:
                    continue
                try:
                    pattern = re.compile(definition.pre_filter)
                except re.error as error:
                    raise LedgerError(f"{definition.rule_id}: invalid pre_filter: {error}") from error
                for path in sorted(universe):
                    if isinstance(definition.reach, MemberReach) and not _under(path, definition.reach.crate_dir.as_posix()):
                        continue
                    for line, text in enumerate((export / path).read_text().splitlines(), 1):
                        if pattern.search(text):
                            sites.add(ListedSite(definition.rule_id, path, tip.crate(path), line, text))
        blobs = {rule.rule_id: rule.blob for rule in definitions}
        with self._connection:
            _ = self._connection.execute("DELETE FROM listed_rules WHERE tip=?", (tip.commit,))
            _ = self._connection.executemany(
                "INSERT INTO listed_rules(rule,rule_blob,tip) VALUES (?,?,?)",
                [(rule.rule_id, rule.blob, tip.commit) for rule in definitions],
            )
            _ = self._connection.executemany(
                "INSERT INTO listed_sites(rule,rule_blob,tip,path,line,text) VALUES (?,?,?,?,?,?)",
                [(site.rule, blobs[site.rule], tip.commit, site.path, site.line, site.text)
                 for site in sorted(sites, key=lambda site: (site.rule, site.path, site.line, site.text))],
            )

    def _listed_sites(self, rule: str, tip: RepositoryTip) -> tuple[ListedSite, ...]:
        rows = cast(list[tuple[str, int, str]], self._connection.execute(
            "SELECT path,line,text FROM listed_sites WHERE rule=? AND tip=? ORDER BY path,line,text",
            (rule, tip.commit),
        ).fetchall())
        return tuple(ListedSite(rule, path, tip.crate(path), line, text) for path, line, text in rows)

    def _listed_site_paths(self, rule: str) -> frozenset[str]:
        """The files that hold a listed site of the rule at the synced tip, the only files where record takes a tier B cell."""
        synced = self._synced_tip()
        self._refresh_sites(synced)
        return frozenset(site.path for site in self._listed_sites(rule, synced))

    def proposal_cells(self, ticket_id: str) -> tuple[ProposalCell, ...]:
        """Each rule and file cell of the proposal ticket that record takes at the synced tip.

        It uses record's own path checks: for example, a tier B rule has a cell only at a file that holds one of its
        listed sites, and a rule not in the catalog has none.
        """
        ticket = self._ticket(ticket_id)
        if ticket["kind"] != "proposal":
            raise LedgerError(f"{ticket_id}: cells are for proposal tickets only")
        self._catalog = rule_catalog.load_rules(self.repo)
        tip = self._synced_tip()
        cells: list[ProposalCell] = []
        for rule in ticket["rules"]:
            try:
                definition = self._component_rule(rule)
            except LedgerError:
                continue
            for path in ticket["files"]:
                try:
                    self._check_cell_path(definition, path, tip)
                except CheckCouldNotRun:
                    # A git failure is not a refusal of the cell, so the phase is not written without its cells.
                    raise
                except LedgerError:
                    continue
                cells.append(ProposalCell(rule, path))
        return tuple(sorted(cells, key=lambda cell: (cell.rule, cell.path)))

    def rust_files(self, crate: str) -> tuple[str, ...]:
        tip = self._synced_tip()
        return tuple(path for path in tip.rust_files if tip.crate(path) == crate)

    def listed_sites(self, rule: str, crate: str | None = None) -> tuple[ListedSite, ...]:
        self._catalog = rule_catalog.load_rules(self.repo)
        if not any(definition.rule_id == rule for definition in self._catalog):
            raise LedgerError(f"unknown rule {rule}")
        tip = self._synced_tip()
        self._refresh_sites(tip)
        return tuple(site for site in self._listed_sites(rule, tip) if crate is None or site.crate == crate)

    def unclassified_rules(self) -> tuple[str, ...]:
        self._catalog = rule_catalog.load_rules(self.repo)
        return tuple(rule.rule_id for rule in self._catalog if rule.tier == "unclassified")

    def _rule(self, rule_id: str) -> Rule:
        rule = next((rule for rule in self._catalog if rule.rule_id == rule_id), None)
        if rule is None:
            raise LedgerError(f"unknown rule {rule_id}")
        if rule.tier == "unclassified":
            raise LedgerError(f"unclassified rule {rule_id}")
        return rule

    def _component_rule(self, rule_id: str) -> Rule:
        definition = self._rule(rule_id.removesuffix("#uncovered"))
        if rule_id.endswith("#uncovered"):
            if definition.tier != "B" or definition.candidates is None or definition.candidates.surface != "partial":
                raise LedgerError(f"unknown rule {rule_id}")
            return replace(definition, rule_id=rule_id, tier="C")
        return definition

    def _path(self, path: str) -> str:
        candidate = PurePosixPath(path)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise LedgerError(f"path must be relative to the repository: {path}")
        return str(candidate)

    def _check_cell_path(self, definition: Rule, path: str, tip: RepositoryTip) -> None:
        """Refuse a cell that record does not take at the tip.

        It refuses a path outside the rule's reach, a tier with no judgment cells, and a path that the tier does not judge.
        """
        rule = definition.rule_id
        if isinstance(definition.reach, MemberReach) and not _under(path, definition.reach.crate_dir.as_posix()):
            raise LedgerError(f"{rule}: {path} is outside rule reach")
        if definition.tier in ("A", "E"):
            raise LedgerError(f"{rule}: tier {definition.tier} has no judgment cells in this phase")
        if definition.tier == "B":
            if path not in self._listed_site_paths(rule):
                raise LedgerError(f"{rule}: {path} holds no listed site at the synced tip")
            if path not in tip.blobs:
                raise LedgerError(f"{rule}: {path} is not a file at {tip.commit}")
        elif definition.tier == "D":
            paths = set(tip.rust_files)
            crates = {tip.crate(file) for file in tip.rust_files}
            if definition.candidates is not None:
                synced = self._synced_tip()
                self._refresh_sites(synced)
                sites = self._listed_sites(rule, synced)
                crates.update(site.crate for site in sites)
                paths.update(site.path for site in sites)
            if path not in crates and path not in paths:
                raise LedgerError(f"{rule}: {path} must name a crate, included Rust file or listed site")
        elif path not in tip.rust_files:
            raise LedgerError(f"{rule}: {path} is not an included Rust file")

    def record(
        self, rule: str, path: str, commit: str, rule_blob: str, verdict: Verdict,
        detail: str = "", ticket: str = "", worker: str = "", *,
        input_scope: DeclaredInputScope | UndeclaredInputScope = UNDECLARED_INPUT_SCOPE,
    ) -> None:
        if input_scope == DeclaredInputScope.FILE and verdict not in SCOPED_VERDICTS:
            raise LedgerError(f"{verdict}: file input scope requires clean, exception or held")
        self._catalog = rule_catalog.load_rules(self.repo)
        definition = self._component_rule(rule)
        rule_blob = self._check_pin(ticket, rule, rule_blob)
        path = self._path(path)
        if ticket and self._connection.execute("SELECT 1 FROM tickets WHERE id=?", (ticket,)).fetchone():
            owned = self._ticket(ticket)["cells"]
            if owned and [rule, path] not in owned:
                raise LedgerError(f"{ticket}: {rule} {path} is outside ticket cells")
        tip = self._tip(commit)
        self._check_cell_path(definition, path, tip)
        scope = _cell_scope(definition, path, tip)
        if definition.tier == "B" and not path.endswith(".rs"):
            scope = CellInputScope.FILE
        file_scope_allowed = scope in (CellInputScope.CRATE, CellInputScope.MODULE) and path in tip.rust_files
        if isinstance(input_scope, UndeclaredInputScope):
            if file_scope_allowed and verdict in SCOPED_VERDICTS:
                raise LedgerError(
                    f"{rule}: {path}: say --input-scope file (the verdict rests on this file alone; "
                    + "it stays current when other files change) or --input-scope rule "
                    + "(the verdict rests on the rule's crate or module scope)")
            input_scope = DeclaredInputScope.RULE
        elif input_scope == DeclaredInputScope.FILE:
            if scope == CellInputScope.FILE:
                input_scope = DeclaredInputScope.RULE
            elif not file_scope_allowed:
                raise LedgerError(f"{rule}: {path}: file input scope requires an included Rust file, not a manifest or crate cell")
            else:
                scope = _comparison_scope(input_scope, scope)
        fingerprint = self._fingerprint(tip, scope, path)
        with self._connection:
            _ = self._connection.execute("BEGIN IMMEDIATE")
            if ticket and self._connection.execute(
                "SELECT 1 FROM ticket_yields WHERE ticket=? AND path=?", (ticket, path),
            ).fetchone():
                raise LedgerError(f"{ticket}: {path} was yielded")
            _ = self._connection.execute(
                'INSERT INTO judgments(rule,rule_blob,input_scope,input_fingerprint,path,"commit",verdict,verdict_detail,ticket,worker,recorded_at,declared_scope) '
                + 'VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(rule,path,"commit",rule_blob,input_scope,input_fingerprint,ticket,worker) '
                + 'DO UPDATE SET verdict=excluded.verdict,verdict_detail=excluded.verdict_detail,recorded_at=excluded.recorded_at,declared_scope=excluded.declared_scope',
                (rule, rule_blob, scope, fingerprint, path, tip.commit, verdict, detail, ticket, worker,
                 datetime.now(timezone.utc).isoformat(), input_scope),
            )

    def latest_held_judgments(self) -> dict[tuple[str, str], str]:
        """Return judgment commits for cells whose latest verdict is held."""
        rows = cast(list[tuple[str, str, str]], self._connection.execute(
            'SELECT j.rule,j.path,j."commit" FROM judgments j WHERE j.verdict=\'held\' '
            + 'AND j.id=(SELECT latest.id FROM judgments latest WHERE latest.rule=j.rule AND latest.path=j.path '
            + 'ORDER BY latest.recorded_at DESC,latest.id DESC LIMIT 1)',
        ).fetchall())
        return {(rule, path): commit for rule, path, commit in rows}

    def lint_record(self, tip: str, tool: LintTool, passed: bool, diagnostics: dict[str, int]) -> None:
        if any(count < 0 for count in diagnostics.values()):
            raise LedgerError("diagnostic counts must be nonnegative")
        snapshot = self._tip(tip)
        self._catalog = rule_catalog.load_rules(self.repo)
        rule_blobs = {
            rule.rule_id: rule.blob for rule in self._catalog
            if rule.tier == "A" and style_rules.parse_frontmatter(rule.path)["mechanism"][0] == tool
        }
        with self._connection:
            _ = self._connection.execute(
                "INSERT INTO lint_runs(tip,tree,tool,scope,config_hash,passed,diagnostics_json,rule_blobs_json) VALUES (?,?,?,'workspace',?,?,?,?) "
                + "ON CONFLICT(tree,tool,scope,config_hash) DO UPDATE SET tip=excluded.tip,passed=excluded.passed,diagnostics_json=excluded.diagnostics_json,rule_blobs_json=excluded.rule_blobs_json",
                (snapshot.commit, snapshot.tree, tool, _config_hash(self.repo, snapshot, tool), int(passed),
                 json.dumps(diagnostics, sort_keys=True), json.dumps(rule_blobs, sort_keys=True)),
            )

    def _judgments(self, rule: str, path: str) -> tuple[Judgment, ...]:
        rows = cast(list[tuple[str, str, str, Verdict, str, str]], self._connection.execute(
            'SELECT rule_blob,input_fingerprint,"commit",verdict,input_scope,declared_scope FROM judgments WHERE rule=? AND path=? ORDER BY recorded_at DESC,id DESC', (rule, path),
        ).fetchall())
        return tuple(Judgment(blob, fingerprint, commit, verdict, CellInputScope(scope), DeclaredInputScope(declared))
                     for blob, fingerprint, commit, verdict, scope, declared in rows)

    def _judgment_state(self, rule: Rule, path: str, tip: RepositoryTip) -> tuple[CellState, str]:
        rows = self._judgments(rule.rule_id, path)
        scope = _cell_scope(rule, path, tip)
        pending = ""
        style_branch = self._style_branch()
        for row in rows:
            if row.declared_scope != rows[0].declared_scope or row.rule_blob != rule.blob:
                continue
            try:
                ancestor = self._is_ancestor(row.commit, tip.commit)
            except LedgerError:
                continue
            if ancestor and self._judgment_inputs_match(row, tip, scope, path):
                return "current", row.verdict
            if not ancestor and isinstance(style_branch, StoredStyleBranch):
                try:
                    branch_commit = self._branch_commit(style_branch.name)
                    checked_tip = self._tip(branch_commit)
                    if self._is_ancestor(row.commit, branch_commit) and self._judgment_inputs_match(row, checked_tip, scope, path):
                        if not pending:
                            pending = row.verdict
                except LedgerError:
                    continue
        if pending:
            return "awaiting merge", pending
        return ("stale" if rows else "open"), ""

    def _style_branch(self) -> StyleBranch:
        branches = cast(list[tuple[str]], self._connection.execute("SELECT style_branch FROM sync WHERE id=1").fetchall())
        return StoredStyleBranch(branches[0][0]) if branches and branches[0][0] else NoStoredStyleBranch()

    def _branch_commit(self, branch: str) -> str:
        if branch not in self._branch_tips:
            self._branch_tips[branch] = _git(self.repo, "rev-parse", "--verify", f"{branch}^{{commit}}")
        return self._branch_tips[branch]

    def _repeated_judgments(self, cells: Iterable[Cell], base: str) -> Iterator[tuple[Cell, Judgment, CellInputScope]]:
        """Yield each stale cell that a ticket based at base cannot change, with its newest judgment and input scope.

        The newest judgment (current rule blob, newest declared scope) is in base and in the style branch, and its
        inputs at base are the inputs that judgment saw: a new judgment at base repeats it. The scope is taken at the
        synced tip, as the stale verdict takes it.
        """
        tip = self._tip(base)
        try:
            synced = self._synced_tip()
            match self._style_branch():
                case StoredStyleBranch(name):
                    merged = self._branch_commit(name)
                case NoStoredStyleBranch():
                    merged = synced.commit
        except LedgerError:
            return
        for cell in cells:
            if cell.state != "stale":
                continue
            rule = _judgment_rule(self._component_rule(cell.rule), cell.path)
            scope = _cell_scope(rule, cell.path, synced)
            rows = self._judgments(cell.rule, cell.path)
            for row in rows:
                if row.declared_scope != rows[0].declared_scope or row.rule_blob != rule.blob:
                    continue
                try:
                    repeats = (self._is_ancestor(row.commit, tip.commit) and self._is_ancestor(row.commit, merged)
                               and self._judgment_inputs_match(row, tip, scope, cell.path))
                except LedgerError:
                    repeats = False
                if repeats:
                    yield cell, row, scope
                break

    def judged_at_base(self, cells: Iterable[Cell], base: str) -> tuple[Cell, ...]:
        """Return the stale cells that a ticket based at base cannot change. Only a merge into base changes their inputs."""
        return tuple(cell for cell, _, _ in self._repeated_judgments(cells, base))

    def style_merge_changes(self, cells: Iterable[Cell], base: str) -> StyleMerge:
        """Return the cells judged at base whose inputs a fast-forward of base to the pushed style tip changes."""
        style_branch = self._style_branch()
        if isinstance(style_branch, NoStoredStyleBranch):
            return NoStyleMergeChange()
        # A missing pushed style branch is an error with its own line, not a quiet wait for sync.
        pushed = pushed_branch_point_tip(self.repo, style_branch.name)
        if not self._is_ancestor(base, pushed):
            return NoStyleMergeChange()
        tip = self._tip(pushed)
        changed = tuple(cell for cell, row, scope in self._repeated_judgments(cells, base)
                        if not self._judgment_inputs_match(row, tip, scope, cell.path))
        return StyleMergeChanges(style_branch.name, changed) if changed else NoStyleMergeChange()

    def _lint_coverage(self, tool: LintTool, tip: RepositoryTip) -> LintCoverage:
        rows = cast(list[tuple[str, str, int, str, str]], self._connection.execute(
            "SELECT tree,config_hash,passed,diagnostics_json,rule_blobs_json FROM lint_runs WHERE tool=? AND scope='workspace' ORDER BY id DESC", (tool,),
        ).fetchall())
        config_hash = _config_hash(self.repo, tip, tool)
        for tree, config, passed, serialized, rule_blobs_json in rows:
            if tree == tip.tree and config == config_hash:
                diagnostics = cast(dict[str, int], json.loads(serialized))
                rule_blobs = cast(dict[str, str], json.loads(rule_blobs_json))
                return LintCoverage(True, bool(passed), diagnostics, rule_blobs)
        return LintCoverage(bool(rows), False, {}, {})

    def _coverage(self, rule_id: str | None = None, crate: str | None = None) -> Iterator[tuple[Cell, str]]:
        self._catalog = rule_catalog.load_rules(self.repo)
        self._branch_tips.clear()
        tip = self._synced_tip()
        self._refresh_sites(tip)
        lint_tools: dict[LintTool, LintCoverage] = {}
        if rule_id is not None:
            if rule_id.endswith("#uncovered"):
                _ = self._component_rule(rule_id)
            elif not any(rule.rule_id == rule_id for rule in self._catalog):
                raise LedgerError(f"unknown rule {rule_id}")
        for definition in self._catalog:
            if rule_id is not None and definition.rule_id != rule_id.removesuffix("#uncovered"):
                continue
            if definition.tier in ("E", "unclassified"):
                continue
            files = [path for path in tip.rust_files if not isinstance(definition.reach, MemberReach)
                     or _under(path, definition.reach.crate_dir.as_posix())]
            sites = self._listed_sites(definition.rule_id, tip) if definition.tier in ("B", "D") else ()
            if definition.tier == "B":
                paths = sorted({site.path for site in sites})
            elif definition.tier == "D":
                paths = (sorted({tip.crate(path) for path in files} | {site.crate for site in sites})
                         if definition.input_scope == "manifest" else sorted(set(files) | {site.path for site in sites}))
            else:
                paths = files
            components = [(definition, paths)]
            if definition.tier == "B" and definition.candidates is not None and definition.candidates.surface == "partial":
                components.append((replace(definition, rule_id=definition.rule_id + "#uncovered", tier="C"), files))
            lint_coverage = LintCoverage(False, False, {}, {})
            if definition.tier == "A":
                tool = cast(LintTool, style_rules.parse_frontmatter(definition.path)["mechanism"][0])
                if tool not in lint_tools:
                    lint_tools[tool] = self._lint_coverage(tool, tip)
                lint_coverage = lint_tools[tool]
            for component, component_paths in components:
                if rule_id is not None and rule_id.endswith("#uncovered") and component.rule_id != rule_id:
                    continue
                for path in component_paths:
                    cell_crate = path if component.tier == "D" and component.input_scope == "manifest" else tip.crate(path)
                    if crate is not None and cell_crate != crate:
                        continue
                    if component.tier == "A":
                        state: CellState = "open"
                        if lint_coverage.has_runs:
                            state = "current" if (lint_coverage.passed and lint_coverage.diagnostics.get(path, 0) == 0
                                                  and lint_coverage.rule_blobs.get(component.rule_id) == component.blob) else "stale"
                        verdict = ""
                    else:
                        state, verdict = self._judgment_state(_judgment_rule(component, path), path, tip)
                    yield Cell(component.rule_id, path, cell_crate, cast(Tier, component.tier), component.family, state), verdict

    def cells(self, rule: str | None = None, crate: str | None = None) -> Iterator[Cell]:
        for cell, _ in self._coverage(rule, crate):
            yield cell

    def rule_counts(self, scope: CoverageScope) -> tuple[RuleCounts, ...]:
        return self._count_rows(self._coverage(None, _crate_filter(scope)))

    def _count_rows(self, coverage: Iterable[tuple[Cell, str]]) -> tuple[RuleCounts, ...]:
        states: dict[str, Counter[CellState]] = {}
        held: Counter[str] = Counter()
        for cell, verdict in coverage:
            rule_id = cell.rule.removesuffix("#uncovered")
            if rule_id not in states:
                states[rule_id] = Counter()
            states[rule_id][cell.state] += 1
            if cell.state == "current" and verdict == "held":
                held[rule_id] += 1
        rows: list[RuleCounts] = []
        for definition in self._catalog:
            if definition.tier == "unclassified":
                continue
            counts = states.get(definition.rule_id, Counter())
            rows.append(RuleCounts(definition.rule_id, definition.tier,
                                   counts["current"], counts["stale"], counts["open"], counts["awaiting merge"],
                                   held[definition.rule_id], definition.input_scope))
        return tuple(rows)

    def status(self, rule: str | None = None, scope: CoverageScope = _WHOLE_REPOSITORY, as_json: bool = False) -> None:
        if as_json:
            coverage = tuple(self._coverage(rule, _crate_filter(scope)))
            unclassified = self.unclassified_rules()
            print(json.dumps({"cells": [asdict(cell) for cell, _ in coverage], "unclassified_rules": unclassified}, sort_keys=True))
        else:
            rows = self._count_rows(self._coverage(rule, _crate_filter(scope)))
            repository_rows = self.rule_counts(scope) if rule or isinstance(scope, CrateCoverage) else rows
            complete = {row.rule_id: row.complete for row in repository_rows}
            unclassified = self.unclassified_rules()
            if unclassified:
                print(f"unclassified: {len(unclassified)} rules — pull nate_style")
            print("rule\tcurrent\tstale\topen\tawaiting merge\theld\tdone")
            for row in rows:
                if rule and row.rule_id != rule.removesuffix("#uncovered"):
                    continue
                counts = [str(count) for count in (row.current, row.stale, row.open, row.awaiting_merge, row.held)]
                print("\t".join([row.rule_id, *counts, str(complete[row.rule_id]).lower()]))



@dataclass(frozen=True)
class LedgerLocation:
    repo: Path
    database: Path


def ledger_location(repo: Path) -> LedgerLocation:
    repo = Path(_git(repo.expanduser().resolve(), "rev-parse", "--show-toplevel"))
    common = Path(_git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    main_repo = common.parent
    state = Path(os.environ.get("STYLE_LEDGER_STATE_DIR", str(Path.home() / ".local/state/style"))).expanduser()
    return LedgerLocation(repo, state / main_repo.name / "ledger.sqlite")


def open_ledger(repo: Path) -> Ledger:
    location = ledger_location(repo)
    return Ledger(location.repo, location.database)


@contextmanager
def preview_ledger(repo: Path) -> Generator[Ledger]:
    """Read and migrate a temporary copy without changing the stored ledger."""
    location = ledger_location(repo)
    with tempfile.TemporaryDirectory(prefix="style-ledger-preview-") as directory:
        database = Path(directory) / "ledger.sqlite"
        if location.database.exists():
            with (closing(sqlite3.connect(location.database.resolve().as_uri() + "?mode=ro", uri=True,
                                          timeout=BUSY_TIMEOUT_SECONDS)) as source,
                  closing(sqlite3.connect(database, timeout=BUSY_TIMEOUT_SECONDS)) as destination):
                source.backup(destination)
        with Ledger(location.repo, database) as ledger:
            yield ledger


class LedgerArguments(argparse.Namespace):
    command: str = ""
    repo: str = ""
    rule: str = ""
    path: str = ""
    commit: str = ""
    rule_blob: str = ""
    verdict: Verdict = "clean"
    input_scope: DeclaredInputScope | UndeclaredInputScope = UNDECLARED_INPUT_SCOPE
    detail: str = ""
    ticket: str = ""
    worker: str = ""
    tip: str = ""
    style_branch: str | None = None
    tool: LintTool = "clippy"
    passed: str = "0"
    diagnostics: str = ""
    crate: str = ""
    json: bool = False
    action: str = ""
    id: str = ""
    unit: str = ""
    branch: str = ""
    kind: str = ""
    family: str = ""
    files: str = "[]"
    sites: str = "[]"
    cells: str = "[]"
    rules: str = "[]"
    checkpoint: str = ""
    to: str = ""
    merge_commit: str = ""
    branch_point: str = ""
    line: int = 0
    text: str = ""
    before: str = ""
    after: str = ""
    proposal: str = ""
    state: HeldStateName | None = None
    finding_ids: str = "[]"
    site: str = ""
    idea: str = ""
    # `held built` takes more than one --id only with --commit.
    ids: list[str] = []
    merge: str | None = None
    by: str | None = None


def _json_strings(serialized: str) -> list[str]:
    value = cast(object, json.loads(serialized))
    if not isinstance(value, list) or any(not isinstance(item, str) for item in cast(list[object], value)):
        raise LedgerError("expected a JSON list of strings")
    return cast(list[str], value)


def _json_cells(serialized: str) -> tuple[tuple[str, str], ...]:
    value = cast(object, json.loads(serialized))
    if not isinstance(value, list):
        raise LedgerError("expected a JSON list of [rule, path] pairs")
    cells: list[tuple[str, str]] = []
    for item in cast(list[object], value):
        if not isinstance(item, list):
            raise LedgerError("expected a JSON list of [rule, path] pairs")
        pair = cast(list[object], item)
        if len(pair) != 2 or not isinstance(pair[0], str) or not isinstance(pair[1], str):
            raise LedgerError("expected a JSON list of [rule, path] pairs")
        cells.append((pair[0], pair[1]))
    return tuple(cells)


def _json_sites(serialized: str) -> list[dict[str, object]]:
    value = cast(object, json.loads(serialized))
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in cast(list[object], value)):
        raise LedgerError("expected a JSON list of site objects")
    return cast(list[dict[str, object]], value)


def _held_json(finding: HeldFinding) -> dict[str, object]:
    result: dict[str, object] = asdict(finding)
    result["build_ticket"] = finding.build_ticket.ticket if isinstance(finding.build_ticket, BuildTicket) else ""
    result["state"] = held_state_name(finding.state)
    if isinstance(finding.state, (Filed, Built, Withdrawn)):
        result["proposal"] = str(finding.state.proposal) if isinstance(finding.state.proposal, Path) else ""
    if isinstance(finding.state, Withdrawn):
        # One of the two is set, as in the ledger row: the exception judgment, or the qualified finding duplicated.
        by = finding.state.by
        result["withdrawn_by"] = by.judgment_id if isinstance(by, ExceptionVerdict) else 0
        result["duplicate_of"] = by.finding_id if isinstance(by, DuplicateOf) else ""
    result["reading"] = reading_line(finding.state, finding.reading) if isinstance(finding.reading, ReadByPerson) else ""
    result["unwaived_by"] = finding.waive_undo.by if isinstance(finding.waive_undo, WaiveUndoneBy) else ""
    follow = finding.follow
    result["followed_from"] = f"{follow.from_path}:{follow.from_line}" if isinstance(follow, FollowedDeclaration) else ""
    result["followed_at"] = follow.commit if isinstance(follow, FollowedDeclaration) else ""
    result["declaration_gone_at"] = (finding.state.by.commit if isinstance(finding.state, Withdrawn)
                                     and isinstance(finding.state.by, DeclarationGone) else "")
    return result


def _wake_sync_waiters(repo: Path, ticket_id: str) -> None:
    scripts = Path(__file__).resolve().parents[1]
    try:
        result = subprocess.run([
            str(scripts / "lib/py"), str(scripts / "style/style_sync.py"),
            "wake-waiters", "--repo", str(repo), "--ticket", ticket_id,
        ], capture_output=True, text=True, check=False)
        if result.stdout:
            print(result.stdout, end="")
        if result.returncode:
            print("style_ledger: wake: " + " ".join(
                (result.stderr.strip() or f"style_sync.py exited {result.returncode}").splitlines(),
            ))
    except OSError as error:
        print("style_ledger: wake: " + " ".join(str(error).splitlines()))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    record = commands.add_parser("record")
    for field in ("rule", "path", "commit", "ticket", "worker"):
        _ = record.add_argument(f"--{field}", required=True)
    _ = record.add_argument("--rule-blob", default="")
    _ = record.add_argument("--verdict", choices=("clean", "fixed", "exception", "held", "waived"), required=True)
    _ = record.add_argument("--detail", default="")
    _ = record.add_argument("--input-scope", choices=tuple(DeclaredInputScope), type=DeclaredInputScope, default=UNDECLARED_INPUT_SCOPE)
    candidate = commands.add_parser("candidate")
    for field in ("ticket", "site", "idea"):
        _ = candidate.add_argument(f"--{field}", required=True)
    lint = commands.add_parser("lint-record")
    _ = lint.add_argument("--tip", required=True)
    _ = lint.add_argument("--tool", choices=("clippy", "mend", "rustfmt"), required=True)
    _ = lint.add_argument("--passed", choices=("0", "1"), required=True)
    _ = lint.add_argument("--diagnostics", required=True)
    sync = commands.add_parser("set-tip")
    _ = sync.add_argument("--tip", required=True)
    _ = sync.add_argument("--style-branch", default=None)
    status = commands.add_parser("status")
    _ = status.add_argument("--rule", default="")
    _ = status.add_argument("--crate", default="")
    _ = status.add_argument("--json", action="store_true")
    summary = commands.add_parser("summary")
    _ = summary.add_argument("--json", action="store_true")
    reconcile = commands.add_parser("reconcile")
    _ = reconcile.add_argument("--branch-point", required=True)
    _ = reconcile.add_argument("--style-branch", required=True)
    for command in (record, candidate, lint, sync, status, summary, reconcile):
        _ = command.add_argument("--repo", required=True)
    for name, actions in (
        ("ticket", ("add", "assign", "checkpoint", "merge", "drop", "open", "yield")),
        ("handoff", ("add", "send", "reroute", "merged", "pending")),
        ("tip", ("seen", "done")),
        ("held", ("add", "file", "waive", "built", "list")),
    ):
        group = commands.add_parser(name)
        _ = group.add_argument("--repo", default=argparse.SUPPRESS)
        children = group.add_subparsers(dest="action", required=True)
        for action in actions:
            command = children.add_parser(action)
            _ = command.add_argument("--repo", default=argparse.SUPPRESS)
            if name == "ticket":
                if action == "open":
                    _ = command.add_argument("--unit", required=True)
                else:
                    _ = command.add_argument("--id", required=True)
                if action == "add":
                    for field in ("unit", "family", "crate", "files", "rules"):
                        _ = command.add_argument(f"--{field}", required=True)
                    _ = command.add_argument("--kind", choices=("read", "listed", "structure", "proposal"), required=True)
                    _ = command.add_argument("--rule", default="")
                    _ = command.add_argument("--sites", default="[]")
                    _ = command.add_argument("--cells", default="[]")
                    _ = command.add_argument("--proposal", default="")
                    _ = command.add_argument("--finding-ids", default="[]")
                elif action == "assign":
                    _ = command.add_argument("--branch", required=True)
                elif action == "checkpoint":
                    _ = command.add_argument("--commit", required=True)
                elif action == "yield":
                    _ = command.add_argument("--path", required=True)
            elif name == "handoff":
                if action != "pending":
                    _ = command.add_argument("--checkpoint", required=True)
                if action in ("send", "reroute"):
                    _ = command.add_argument("--to", required=True)
                elif action == "merged":
                    _ = command.add_argument("--merge-commit", default="")
            elif name == "tip":
                _ = command.add_argument("--tip", required=True)
            elif action == "add":
                for field in ("rule", "path", "text", "before", "after", "rule-blob", "ticket"):
                    _ = command.add_argument(f"--{field}", required=True)
                _ = command.add_argument("--line", type=int, required=True)
                _ = command.add_argument("--kind", choices=("rename", "type", "restructure"), required=True)
            elif action == "list":
                _ = command.add_argument("--state", choices=("unfiled", "filed", "waived", "built", "withdrawn"), default=None)
                _ = command.add_argument("--json", action="store_true")
            elif action == "built":
                _ = command.add_argument("--id", dest="ids", action="append", required=True)
                _ = command.add_argument("--commit", dest="merge", default=None)
                _ = command.add_argument("--by", default=None)
            else:
                _ = command.add_argument("--id", required=True)
                if action == "file":
                    _ = command.add_argument("--proposal", required=True)
    args = parser.parse_args(namespace=LedgerArguments())
    if not args.repo:
        parser.error("--repo is required")
    if args.command == "held" and args.action == "built":
        # A blank name is a prefix of every finding.
        if any(not name.strip() for name in args.ids):
            parser.error("held built --id requires a finding id")
        if args.merge is None and len(args.ids) > 1:
            parser.error("held built takes more than one --id only with --commit")
        if args.merge is None and args.by is not None:
            parser.error("held built --by requires --commit")
        if args.by is not None and not args.by.strip():
            parser.error("held built --by requires a name")
    try:
        with open_ledger(Path(args.repo)) as ledger:
            if args.command == "set-tip":
                ledger.set_tip(args.tip, args.style_branch)
            elif args.command == "record":
                ledger.record(args.rule, args.path, args.commit, args.rule_blob, args.verdict, args.detail, args.ticket, args.worker,
                              input_scope=args.input_scope)
            elif args.command == "candidate":
                path, separator, line = args.site.rpartition(":")
                if not separator or not path or not line.isdecimal():
                    raise LedgerError("candidate --site must be <path>:<line>")
                ledger.record_candidate(args.ticket, path, int(line), args.idea)
            elif args.command == "lint-record":
                value = cast(object, json.loads(Path(args.diagnostics).read_text()))
                if not isinstance(value, dict) or any(not isinstance(key, str) or type(count) is not int or count < 0
                                                      for key, count in cast(dict[object, object], value).items()):
                    raise LedgerError("diagnostics must be an object of file paths to nonnegative counts")
                ledger.lint_record(args.tip, args.tool, args.passed == "1", cast(dict[str, int], value))
            elif args.command == "status":
                scope = CrateCoverage(args.crate) if args.crate else WholeRepository()
                ledger.status(args.rule or None, scope, args.json)
            elif args.command == "summary":
                result = ledger.summary().to_json()
                if args.json:
                    print(json.dumps(result, sort_keys=True))
                else:
                    for key, value in result.items():
                        print(f"{key}: {value}")
            elif args.command == "reconcile":
                for change in ledger.reconcile(args.branch_point, args.style_branch or ""):
                    print(change)
            elif args.command == "ticket":
                if args.action == "add":
                    ledger.ticket_add(args.id, args.unit, cast(TicketKind, args.kind), args.family, args.crate,
                                      _json_strings(args.files), _json_strings(args.rules), args.rule, _json_sites(args.sites), args.proposal, _json_cells(args.cells), tuple(_json_strings(args.finding_ids)))
                elif args.action == "assign":
                    ledger.ticket_assign(args.id, args.branch)
                elif args.action == "checkpoint":
                    ledger.ticket_checkpoint(args.id, args.commit)
                elif args.action == "merge":
                    for item in ledger.ticket_merge(args.id):
                        print(not_built_line(ledger.repo, item))
                    if ledger.sync_waits():
                        _wake_sync_waiters(ledger.repo, args.id)
                elif args.action == "drop":
                    ledger.ticket_drop(args.id)
                elif args.action == "yield":
                    ledger.ticket_yield(args.id, args.path)
                else:
                    for ticket in ledger.ticket_open(args.unit):
                        print(json.dumps(ticket, sort_keys=True))
            elif args.command == "handoff":
                if args.action == "add":
                    ledger.handoff_add(args.checkpoint)
                elif args.action == "send":
                    ledger.handoff_send(args.checkpoint, args.to)
                elif args.action == "reroute":
                    ledger.handoff_reroute(args.checkpoint, args.to)
                elif args.action == "merged":
                    ledger.handoff_merged(args.checkpoint, args.merge_commit)
                else:
                    pending = ledger.pending_handoffs()
                    if not pending:
                        print("no pending hand-offs")
                    for handoff in pending:
                        print(json.dumps(handoff, sort_keys=True))
            elif args.command == "tip":
                if args.action == "seen":
                    print("seen" if ledger.tip_seen(args.tip) else "new")
                else:
                    ledger.tip_done(args.tip)
            elif args.command == "held":
                if args.action == "add":
                    finding = ledger.hold(args.rule, args.path, args.line, args.text, cast(FindingKind, args.kind),
                                          args.before, args.after, args.rule_blob, args.ticket)
                    print(finding.finding_id)
                    # A re-add changes no state, so a built row whose rename is not in the code waits for reopen.
                    # The add already succeeded, so a hint that cannot be checked is left out. Reopen keeps a person's
                    # reading of a built row, so the hint is left out for it too.
                    if isinstance(finding.state, Built) and isinstance(finding.reading, NotReadByPerson):
                        try:
                            tip = ledger.synced_commit()
                            check = ledger.applied_check(finding, tip)
                        except LedgerError:
                            pass
                        else:
                            if isinstance(check, RenameNotApplied):
                                print(f"built, but not in the code at {tip[:7]}: run style_proposals.py reopen --all "
                                      + f"(issue {finding.state.proposal})", file=sys.stderr)
                elif args.action == "file":
                    ledger.file_held(args.id, Path(args.proposal))
                elif args.action == "waive":
                    ledger.waive_held(args.id)
                elif args.action == "built":
                    if args.merge is None:
                        ledger.mark_built(args.ids[0])
                    else:
                        reading: PersonReading = ReadByPerson(args.by.strip()) if args.by is not None else NotReadByPerson()
                        for item in ledger.mark_built_at_merge(tuple(args.ids), args.merge, reading):
                            print(built_at_merge_line(item))
                else:
                    findings = ledger.held_findings(HeldFindingsInState(args.state) if args.state else AllHeldFindings())
                    if args.json:
                        print(json.dumps([_held_json(finding) for finding in findings], sort_keys=True))
                    else:
                        for finding in findings:
                            details = ([followed_line(finding.follow)] if isinstance(finding.follow, FollowedDeclaration) else []) + (
                                [withdrawn_line(finding.state.by)] if isinstance(finding.state, Withdrawn) else []) + (
                                [reading_line(finding.state, finding.reading)] if isinstance(finding.reading, ReadByPerson) else []) + (
                                [f"unwaived by {finding.waive_undo.by}"] if isinstance(finding.waive_undo, WaiveUndoneBy) else [])
                            print("\t".join([finding.finding_id, finding.rule, f"{finding.path}:{finding.line}",
                                             held_state_name(finding.state), *details]))
    except (LedgerError, rule_catalog.RuleValidationError, OSError, sqlite3.Error, ValueError) as error:
        parser.exit(2, f"style_ledger: {error}\n")


if __name__ == "__main__":
    main()
