#!/usr/bin/env python3
"""Store style judgments and derive coverage at a repository's synced tip."""

from __future__ import annotations

import argparse
import fcntl
import fnmatch
import hashlib
import json
import os
import posixpath
import re
import sqlite3
import subprocess
import sys
import tempfile
import tomllib
import uuid
from collections import Counter
from collections.abc import Callable, Generator, Iterable, Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
from functools import wraps
from pathlib import Path, PurePosixPath
from types import TracebackType
from typing import Concatenate, Literal, TypedDict, cast

from git_process import (CheckCouldNotRun as CheckCouldNotRun, GitReadFailed as GitReadFailed,
                         LedgerError as LedgerError, GitExitAnswers, SUCCESS_ONLY, run_git)

import rules as rule_catalog
import style_footprint
import declarations
import finding_sites
from finding_sites import (NeedsJudgment as NeedsJudgment,
                           SiteEvidence as SiteEvidence, UnchangedSite as UnchangedSite)
import style_rules
from approved_renames import (AppliedCheck, AppliedInCode, ApprovedRename, BindingProof, BindingRename, FileRename, Folder, MissingFile,
                              NoAppliedCheck, RenameNotApplied, applied_check, approved_rename, binding_rename, binding_rename_check, file_rename)
from candidate_generators import enumerate_candidates
from declarations import (BlockScopedImpl, Declaration, DeclaredFile, DeclaredItem, FileLevelImpl, ImplementedTrait, ImplOrTraitItem,
                          ImplPosition, UnprovedImplPosition,
                          NoImplementedTrait, TraitMembership, UnreadableImplementedTrait, moved_site_line, read_declarations)
from rules import Family, InputScope, MemberReach, Rule, Tier

CellState = Literal["current", "stale", "open", "awaiting merge"]
# `removed` closes a ticket cell whose file the ticket's own commits deleted; it is not a verdict on code.
Verdict = Literal["clean", "fixed", "exception", "held", "waived", "removed"]
LintTool = Literal["clippy", "mend", "rustfmt"]
# Why a sync finished a tip: it ran the lint, or the tip holds the style branch's own tree, so no lint ran.
ProcessedTipReason = Literal["linted", "own hand-off"]
# How long every ledger connection waits for another writer's lock before it fails.
BUSY_TIMEOUT_SECONDS = 30
FINDING_REPLACEMENT_GUIDANCE = (
    "The After text replaces the whole Before span. Supply the full replacement, never the old wording with a new line added. "
    + "Rewrite a comment to name the constructs and keep every reason."
)


UNCOVERED_SUFFIX = "#uncovered"


def base_rule(rule: str) -> str:
    """Remove exactly one trailing uncovered-component suffix."""
    return rule.removesuffix(UNCOVERED_SUFFIX)


def base_rule_sql(reference: str) -> str:
    """The same key for a trusted SQL column reference or named bind parameter."""
    size = len(UNCOVERED_SUFFIX)
    return (f"CASE WHEN substr({reference},-{size})='{UNCOVERED_SUFFIX}' "
            + f"THEN substr({reference},1,length({reference})-{size}) ELSE {reference} END")


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


def protected_ticket_files(files: frozenset[str], crates: tuple[str, ...]) -> frozenset[str]:
    """A ticket's edit paths and the build inputs those paths depend on."""
    protected = {"Cargo.toml", "Cargo.lock", *files}
    for path in files:
        if path in ("Cargo.toml", "Cargo.lock"):
            continue
        crate = next((crate for crate in crates if crate == "." or path == crate or path.startswith(crate + "/")), "")
        if not crate:
            raise LedgerError(f"unknown crate for {path}")
        if path == crate:
            raise LedgerError(f"directory footprint cannot be proved for {path}")
        protected.update(str(PurePosixPath(crate) / name) for name in ("Cargo.toml", "build.rs"))
    return frozenset(protected)


@dataclass(frozen=True)
class NoReadFiles:
    """The judgment uses its declared file or rule scope."""


NO_READ_FILES = NoReadFiles()


@dataclass(frozen=True)
class ReadFiles:
    """The judgment rests on its cell file and these named files."""

    paths: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.paths:
            raise LedgerError("--reads requires at least one file")




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
class UnitRenamed:
    tickets: int
    waits: int


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
class JudgedAgain:
    """A judgment under the rule's newer text, or another ticket's of a changed file under the same text, judged the
    finding's cell again and did not hold it; that judgment now decides the cell."""

    judgment_id: int


@dataclass(frozen=True)
class NotJudgedAgain:
    """No newer judgment at the finding's cell settles it."""


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


@dataclass(frozen=True)
class RuleBodyChanged:
    """The finding's rule now has this canonical body; ordinary refill must judge it again."""

    rule_blob: str


WithdrawnBy = ExceptionVerdict | JudgedAgain | DuplicateOf | DeclarationGone | RuleBodyChanged


def withdrawn_line(by: WithdrawnBy) -> str:
    match by:
        case ExceptionVerdict():
            return f"judgment {by.judgment_id} exception"
        case JudgedAgain():
            return f"judged again in judgment {by.judgment_id}"
        case DuplicateOf():
            return f"duplicate of {by.finding_id}"
        case DeclarationGone():
            return f"declaration gone at {by.commit[:7]}"
        case RuleBodyChanged():
            return f"rule body changed to {by.rule_blob[:8]}"


@dataclass(frozen=True)
class NoDecision:
    """The finding was withdrawn while unfiled, before `file` put it in a decision."""


@dataclass(frozen=True)
class Withdrawn:
    """The finding left its decision, for the reason in by."""

    proposal: Path | NoDecision
    by: WithdrawnBy


HeldState = Unfiled | Filed | Waived | Built | Withdrawn


class WaiveProvenance(StrEnum):
    UNRECORDED = "unrecorded"
    UNIT = "unit"
    PERSON = "person"
    LEDGER = "ledger"


@dataclass(frozen=True)
class WaiveByUnit:
    ticket: str


@dataclass(frozen=True)
class WaiveByPerson:
    by: str


@dataclass(frozen=True)
class WaiveByLedger:
    """The ledger waived a finding whose file or site is gone."""


WaiveActor = WaiveByUnit | WaiveByPerson | WaiveByLedger
DeclineActor = WaiveByPerson | WaiveByLedger


def _waive_actor_record(actor: WaiveActor) -> tuple[WaiveProvenance, str]:
    match actor:
        case WaiveByUnit(ticket):
            if not ticket.strip():
                raise LedgerError("held waive --ticket requires a ticket")
            return WaiveProvenance.UNIT, ticket
        case WaiveByPerson(by):
            if not by.strip():
                raise LedgerError("held waive --person requires a name")
            return WaiveProvenance.PERSON, by
        case WaiveByLedger():
            return WaiveProvenance.LEDGER, "ledger"


@dataclass(frozen=True)
class NoRecordedWaiveActor:
    """An older waive has no caller name on the finding."""


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
    widened: tuple[str, ...] = ()
    base: str = ""
    other_issues: tuple[Path, ...] = ()

    @property
    def issues(self) -> tuple[Path, ...]:
        return (self.issue, *self.other_issues)


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


# The id, state and checkpoint of a ticket that built or holds a finding.
LinkedTicket = tuple[str, TicketState, str]
# The checkpoint and base of the ticket that judged a finding; empty when there is no such ticket.
JudgingTicket = tuple[tuple[str, str], ...]


def _refuse_open_ticket(finding_id: str, ticket: str, state: TicketState) -> None:
    if state not in FINAL_TICKET_STATES:
        raise LedgerError(f"{finding_id}: build ticket {ticket} still holds the finding")


@dataclass(frozen=True)
class BuiltAtMergeRows:
    """Every ledger row that decides whether a merge builds a finding, as read before the write lock."""

    finding: HeldFinding
    linked: tuple[LinkedTicket, ...]
    judging: JudgingTicket

    def refuse_open_build_ticket(self) -> None:
        """Refuse the finding while its build ticket, as read, is not merged or dropped."""
        if isinstance(self.finding.build_ticket, BuildTicket):
            ticket = self.finding.build_ticket.ticket
            states: dict[str, TicketState] = {id: state for id, state, _ in self.linked}
            if ticket not in states:
                raise LedgerError(f"unknown ticket {ticket}")
            _refuse_open_ticket(self.finding.finding_id, ticket, states[ticket])


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
NO_WAIVE_UNDO = NoWaiveUndo()


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
    waive_provenance: WaiveProvenance
    waive_actor: WaiveActor | NoRecordedWaiveActor


@dataclass(frozen=True)
class BulkRefusal:
    """A finding released from a bulk ticket for ordinary implementation."""

    finding_id: str
    ticket: str
    reason: str
    at: str


def rule_changed_since_judged(finding: HeldFinding, definitions: Mapping[str, Rule]) -> bool:
    """Whether the catalog has no rule for the finding, or the rule's text is not the text the finding was judged under."""
    definition = definitions.get(base_rule(finding.rule))
    return definition is None or not rule_catalog.judged_text_unchanged(finding.rule_blob, definition)


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
class SupersedingJudgment:
    """The newest judgment at a finding's cell under the rule's current text, also newer than its own: an exception
    for a filed finding, any verdict for an unfiled one. Under the text the finding was judged under, it is another
    ticket's judgment of a changed file, which did not hold the finding."""

    judgment_id: int
    verdict: Verdict
    rule_blob: str


@dataclass(frozen=True)
class WithdrawnFinding:
    finding: HeldFinding
    verdict: SupersedingJudgment | RuleBodyChanged


@dataclass(frozen=True)
class KeptForBuild:
    """A finding the newer verdict would withdraw, kept because a ticket that is not final builds it."""

    finding: HeldFinding
    verdict: SupersedingJudgment
    ticket: str


@dataclass(frozen=True)
class Withdrawal:
    """Findings withdrawn by changed rule bodies or newer judgments, and those an open build keeps."""

    withdrawn: tuple[WithdrawnFinding, ...]
    kept: tuple[KeptForBuild, ...]


class OutsideFailure(StrEnum):
    OLD_NAME_PRESENT = "old name still declared in crate"
    NO_PASSING_FILES = "zero passing files"
    SEVERAL_FILES = "several passing files"
    AMBIGUOUS_ITEMS = "ambiguous destination items"
    BODY_FAILED = "body check failed"
    BODY_UNREADABLE = "body unreadable or empty"
    WRONG_KEYWORD = "keyword differs"
    WRONG_OWNER = "owner differs"
    TRAIT_UNCERTAIN = "trait identity uncertain"
    NOT_GAINED = "declaration not gained"
    UNMAPPED = "mapped site not proved"
    CHILD_UNPROVED = "child binding or scope not proved"


@dataclass(frozen=True)
class MappedSiteProof:
    site: declarations.MappedDeclaration


@dataclass(frozen=True)
class GainedFileProof:
    path: str
    declaration: Declaration


@dataclass(frozen=True)
class OutsideCandidate:
    path: str
    declaration: Declaration
    line: int
    unchanged: int
    old_nonblank_count: int
    checks: tuple[OutsideFailure, ...]


@dataclass(frozen=True)
class NoOutsideCandidates:
    """No changed file declared an examined target."""


@dataclass(frozen=True)
class OutsideCandidates:
    items: tuple[OutsideCandidate, ...]

    def __post_init__(self) -> None:
        if not self.items:
            raise ValueError("outside candidates must not be empty")


@dataclass(frozen=True)
class OutsideRenameNotProven:
    mapped: declarations.MappedSite
    candidates: NoOutsideCandidates | OutsideCandidates
    reasons: tuple[OutsideFailure, ...]


@dataclass(frozen=True)
class OutsideRenameProven:
    declaration: Declaration
    path: str
    line: int
    proof: MappedSiteProof | GainedFileProof


@dataclass(frozen=True)
class NoOutsideEvidence:
    """No outside-rename search read this outcome."""


@dataclass(frozen=True)
class OutsideRenameUnresolved:
    """An outside rename was examined without proof; the finding keeps its state and decision."""

    finding: HeldFinding
    declaration: Declaration
    evidence: OutsideRenameNotProven


def outside_evidence_line(evidence: OutsideRenameNotProven) -> str:
    """Name every examined target and each failed check in a stable order."""
    candidates = "none"
    if isinstance(evidence.candidates, OutsideCandidates):
        candidates = ", ".join(
            f"{item.path}:{item.line} {declarations.declaration_line(item.declaration)} "
            + f"{item.unchanged}/{item.old_nonblank_count} body lines "
            + ("passed" if not item.checks else "/".join(item.checks)) for item in evidence.candidates.items)
    return (f"outside rename not proved: {', '.join(evidence.reasons)}; candidates: {candidates}; "
            + mapped_evidence_line(evidence.mapped))


def mapped_evidence_line(mapped: declarations.MappedSite) -> str:
    match mapped:
        case declarations.MappedDeclaration(path, line, declaration):
            return f"mapped site {path}:{line} declares {declarations.declaration_line(declaration)}"
        case declarations.MappedNoDeclaration(path, line):
            return f"mapped site {path}:{line} declares no declaration"
        case declarations.UnmappedSite(reason):
            return f"mapped site unresolved: {reason}"


def outside_evidence_suffix(evidence: OutsideRenameNotProven) -> str:
    return ("; " + mapped_evidence_line(evidence.mapped)
            + f"; outside rename not proved: {', '.join(evidence.reasons)}")



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
    the subject or its impl or trait and one file gained the new declaration. A proved outside rename also follows to path and line.

    For a finding on a statement line of a function, the subject is the function, and line is the one statement line of
    the function there that holds the finding's text."""

    finding: HeldFinding
    declaration: Declaration
    path: str
    line: int


@dataclass(frozen=True)
class DeclarationRemoved:
    """No file declares the finding's subject at the synced tip. The finding is unfiled, or its file is gone there, so it is withdrawn."""

    finding: HeldFinding
    declaration: Declaration
    evidence: NoOutsideEvidence | OutsideRenameNotProven = NoOutsideEvidence()


@dataclass(frozen=True)
class DeclaredInSeveralFiles:
    """Two or more other files declare the finding's subject; a person picks, and the finding stays as it is."""

    finding: HeldFinding
    declaration: Declaration
    paths: tuple[str, ...]
    evidence: NoOutsideEvidence | OutsideRenameNotProven = NoOutsideEvidence()


@dataclass(frozen=True)
class StatementNotFollowed:
    """The function that holds the finding's statement line moved to path, but the finding's text is not on exactly one
    statement line of the function there. The finding stays as it is."""

    finding: HeldFinding
    declaration: Declaration
    path: str
    reason: declarations.StatementRefusal


@dataclass(frozen=True)
class TraitIdentityUncertain:
    """The trait paths cannot be proved equal or different, so the finding stays as it is."""

    finding: HeldFinding
    declaration: Declaration


class TraitIdentity(StrEnum):
    SAME = "same"
    DIFFERENT = "different"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True)
class ResolvedTraitPath:
    """A path with supported imports expanded and no skipped binding of its names."""

    path: tuple[str, ...]


@dataclass(frozen=True)
class UnresolvedTraitPath:
    """An unsupported import or relative path prevents a trait identity proof."""


@dataclass(frozen=True)
class ResolvedTypeArguments:
    """The implementing type's arguments with each supported type path rooted in its revision."""

    words: tuple[str, ...]


@dataclass(frozen=True)
class UnresolvedTypeArguments:
    """An argument cannot be proved to name the same type across revisions."""


@dataclass(frozen=True)
class ResolvedTraitModule:
    """One declared trait's full rooted module path, proved by the crate's file layout."""

    module: str
    declarations: int


@dataclass(frozen=True)
class UnresolvedTraitModule:
    """The path does not prove which declared trait it names."""


@dataclass(frozen=True)
class RootedFileModule:
    """The complete module chain proved by a file's src layout."""

    path: tuple[str, ...]


@dataclass(frozen=True)
class UnreadableFileModule:
    """The file has no proved module chain in its crate's src layout."""


@dataclass(frozen=True)
class DeclaredInAnotherCrate:
    """The subject of a filed finding moved to a file in another crate. Its decision is per crate, so it stays."""

    finding: HeldFinding
    declaration: Declaration
    path: str
    evidence: NoOutsideEvidence | OutsideRenameNotProven = NoOutsideEvidence()


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
    evidence: NoOutsideEvidence | OutsideRenameNotProven = NoOutsideEvidence()


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
    """A built rename with its read site, its build site, and its decision.

    The before name resolves at before_commit; the after name resolves at commit.
    """

    rename: ItemRename
    path: str
    commit: str
    before_path: str
    before_commit: str
    proposal: Path
    before_position: ImplPosition
    position: ImplPosition


@dataclass(frozen=True)
class BuiltRenameEvidence:
    """Built findings and their rename sites at one ledger version and repository tip."""

    findings: tuple[HeldFinding, ...]
    renames: tuple[BuiltRename, ...]


@dataclass(frozen=True)
class UnrestrictedTraitMembership:
    """The declaration is not an impl or trait member, so trait membership does not restrict it."""


@dataclass(frozen=True)
class RequiredTraitMembership:
    """A destination must retain the source member's trait relationship, with any proved trait rename."""

    implemented_trait: TraitMembership
    commit: str
    path: str


TraitRequirement = UnrestrictedTraitMembership | RequiredTraitMembership


def _trait_requirement(item: DeclaredItem, commit: str, path: str) -> TraitRequirement:
    return (RequiredTraitMembership(item.implemented_trait, commit, path) if isinstance(item.declaration.owner, ImplOrTraitItem)
            else UnrestrictedTraitMembership())


@dataclass(frozen=True)
class RenamedByBuilds:
    """The finding's declaration with the new names that built renames of approved decisions gave its item, the impl
    or trait that holds it, or both."""

    declaration: Declaration
    trait_requirement: TraitRequirement


@dataclass(frozen=True)
class NoBuiltRename:
    """No built rename of an approved decision gives the finding's item or its impl or trait a new name."""


@dataclass(frozen=True)
class RefusedBuiltRename:
    """The built-rename reader examined this crate's subject but could not authorize its follow."""


BuiltRenaming = RenamedByBuilds | NoBuiltRename | RefusedBuiltRename | TraitIdentityUncertain


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


@dataclass(frozen=True)
class FindingSite:
    """A finding's source evidence at the tip; it says nothing about cell coverage."""

    finding: HeldFinding
    evidence: SiteEvidence


DeclarationOutcome = (DeclarationNotRead | DeclarationInPlace | DeclarationMoved | DeclarationRemoved
                      | DeclaredInSeveralFiles | DeclaredInAnotherCrate | DeclarationRenamed | RenameAwaitingApproval
                      | DeclarationGoneFromFile | KeptForOpenBuild | TraitIdentityUncertain | OutsideRenameUnresolved
                      | StatementNotFollowed | FindingSite)
# The outcomes that leave a finding as it is for a person to waive or fix in the code.
DeclarationLeft = (DeclaredInSeveralFiles | DeclaredInAnotherCrate | DeclarationGoneFromFile | TraitIdentityUncertain
                   | OutsideRenameUnresolved | StatementNotFollowed)


@dataclass(frozen=True)
class DeclarationReading:
    """Where each finding's declaration is at the synced tip, one outcome per finding read, in the order given."""

    tip: str
    outcomes: tuple[DeclarationOutcome, ...]


@dataclass(frozen=True)
class HeldReservationRelease:
    """What a ticket merge did with the reservation the ticket's checkpoint kept: the line to print, and whether it stays held."""

    line: str
    still_held: bool


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
    verdict: Verdict
    declared_scope: DeclaredInputScope
    reads: ReadFiles | NoReadFiles = NO_READ_FILES


@dataclass(frozen=True)
class CarriedVerdict:
    """A ticket verdict to restamp at the checkpoint, with the input it has there."""

    judgment_id: int
    scope: CellInputScope
    fingerprint: str


@dataclass(frozen=True)
class CarriedTypesFinding:
    """An open types site followed through a proven binding rename without a new judgment."""

    finding: HeldFinding
    line: int
    text: str


@dataclass(frozen=True)
class BindingCarryForward:
    """The types verdicts and open sites that can cross one ticket's binding renames."""

    verdicts: tuple[CarriedVerdict, ...] = ()
    findings: tuple[CarriedTypesFinding, ...] = ()


@dataclass(frozen=True)
class CheckpointRestamps:
    """The ticket's latest judgments and the removed rows on their cells as read before the write lock, and the verdicts they carry."""

    latest: tuple[TicketJudgment, ...]
    removals: frozenset[tuple[int, str, str, str, str]]
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


class WidenedPath(TypedDict):
    """A path that a person added to an approved proposal ticket outside its findings, with the reason."""

    path: str
    reason: str
    widened_by: str
    widened_at: str


def widened_line(widened: WidenedPath) -> str:
    return f"widened: {widened['path']} — {widened['reason']} ({widened['widened_by']}, {widened['widened_at']})"


class YieldedPath(TypedDict):
    """A file that a ticket gave up to product work, and when."""

    path: str
    yielded_at: str


def yielded_line(yielded: YieldedPath) -> str:
    return f"yielded: {yielded['path']} ({yielded['yielded_at']})"


class RemovedCell(TypedDict):
    """A ticket cell whose file the ticket's own commits deleted, the commit that deleted it, and its state line."""

    rule: str
    path: str
    commit: str
    state: str


def removed_cell(ticket_id: str, rule: str, path: str, commit: str) -> RemovedCell:
    return RemovedCell(rule=rule, path=path, commit=commit, state=f"removed by ticket {ticket_id} at {commit[:7]}")


def removed_line(removed: RemovedCell) -> str:
    return f"{removed['rule']} {removed['path']}: {removed['state']}"


@dataclass(frozen=True)
class BatchOwner:
    """The durable batch intent that exclusively owns a ticket claim."""

    batch: str

    def __post_init__(self) -> None:
        if not self.batch:
            raise ValueError("a batch owner requires a batch id")


@dataclass(frozen=True)
class UnbatchedTicket:
    """An ordinary or legacy ticket has no batch recovery owner."""


UNBATCHED_TICKET = UnbatchedTicket()


@dataclass(frozen=True)
class UnrestrictedTicketOwner:
    """Ordinary ticket operations do not require a batch recovery owner."""


_UNRESTRICTED_TICKET_OWNER = UnrestrictedTicketOwner()


class TicketBatchMismatch(LedgerError):
    def __init__(self, ticket: str, owner: BatchOwner | UnbatchedTicket) -> None:
        description = f"batch {owner.batch}" if isinstance(owner, BatchOwner) else "no batch owner"
        super().__init__(f"{ticket}: kept unchanged; ledger records {description}")


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
    widened: tuple[WidenedPath, ...]
    yielded: tuple[YieldedPath, ...]
    removed: tuple[RemovedCell, ...]


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
class UnreadableCode:
    """The file is not UTF-8 or does not tokenize, so the paths it writes are not known."""


@dataclass(frozen=True)
class FileYield:
    """A yield that frees no finding: the ticket is not a proposal ticket, so it gives up only its file."""


@dataclass(frozen=True)
class FindingYield:
    """The findings that a proposal ticket's yield frees, and the commit whose code showed that no kept finding
    needs them."""

    freed: tuple[str, ...]
    read_commit: str


def _code_words(content: bytes | Folder | MissingFile) -> tuple[str, ...] | UnreadableCode:
    """The code words of a Rust file, each without its `r#`, with no comment. A missing file has none."""
    if not isinstance(content, bytes):
        return ()
    try:
        text = content.decode()
    except UnicodeDecodeError:
        return UnreadableCode()
    code = style_footprint.tokens(text)
    if any(token.kind == "unknown" for token in code):
        return UnreadableCode()
    return tuple(token.value.removeprefix("r#") for token in code)


def _glob_sources(prefix: tuple[str, ...], home: style_footprint.CrateModule,
                  inline: tuple[str, ...]) -> tuple[tuple[str, ...], ...] | style_footprint.UnresolvedPath:
    """The modules of the crate, as paths under `crate`, that a glob use with prefix can import from.

    In a module file of the crate, an anchored path resolves from the file's module and the inline modules around the
    glob; any other path can start at the crate's library name or at a child of that module. A file outside the crate
    root folder is the root of another crate, so only a path from the library name reaches the crate.
    """
    anchored = bool(prefix) and prefix[0] in style_footprint.PATH_ANCHORS
    if isinstance(home, style_footprint.OutsideCrate):
        return () if anchored else (prefix[1:],)
    scope = (*home, *inline)
    if not anchored:
        return prefix[1:], (*scope, *prefix)
    target = style_footprint.resolved_module(prefix, scope)
    return target if isinstance(target, style_footprint.UnresolvedPath) else (target,)


# A text that can hold a `path` attribute: the word, then `=` or a comment before it. Only such a file is tokenized.
PATH_ATTRIBUTE_TEXT = re.compile(r"\bpath\s*(?:=|/[/*])")
PLAIN_STRING = re.compile(r'"([^"\\]*)"')


@dataclass(frozen=True)
class MountedFile:
    """A `path` attribute: the file that declares it, and each repository path its literal can name. A reading that
    leaves the repository starts with `..`."""

    declaring: str
    readings: tuple[str, ...]


@dataclass(frozen=True)
class MountedFolder:
    """A `path` attribute on an inline module: the file that declares it, and each repository folder its literal can
    name. The module's code is in the declaring file; the folder holds the files that its `mod` items mount."""

    declaring: str
    readings: tuple[str, ...]


@dataclass(frozen=True)
class UnreadPathAttribute:
    """A `path` attribute whose file the scan cannot name, so it can mount any file."""

    declaring: str
    reason: str


def _path_attributes(path: str, content: bytes) -> tuple[MountedFile | MountedFolder | UnreadPathAttribute, ...]:
    """The `path` attributes of a Rust file, also those inside `cfg_attr`, read from its code words.

    Rust reads the literal from the module's folder (`_module_folders`). On an inline module the literal names a
    folder, not a file. Each reading is kept, also the declaring file's folder, so a caller can require that all of
    them stay in one crate. A literal that is not a plain string, an absolute path, an attribute in a macro (an inner
    attribute reads as one) and a file that does not read as code are unread.
    """
    try:
        text = content.decode()
    except UnicodeDecodeError:
        return (UnreadPathAttribute(path, "is not UTF-8"),) if PATH_ATTRIBUTE_TEXT.search(content.decode(errors="replace")) else ()
    if not PATH_ATTRIBUTE_TEXT.search(text):
        return ()
    words = _code_words(content)
    if isinstance(words, UnreadableCode):
        return (UnreadPathAttribute(path, "does not tokenize"),)
    ends = style_footprint.delimiter_ends(words)
    macros = style_footprint.macro_spans(words, ends)
    blocks = style_footprint.import_context(words).module_blocks
    owners = style_footprint.attribute_owners(words)
    file = PurePosixPath(path)
    read: list[tuple[int, tuple[int, ...], str | UnreadPathAttribute]] = []
    for begin, end in style_footprint.attribute_spans(words):
        for index in (index for index in range(begin, end) if words[index] == "path"):
            literal = PLAIN_STRING.fullmatch(words[index + 2]) if words[index + 1:index + 2] == ("=",) else None
            chain = tuple(sorted(block for block in blocks if block < index < ends.get(block, len(words))))
            if any(start < index < stop for start, stop in macros):
                read.append((begin, chain, UnreadPathAttribute(path, "holds a path attribute in a macro")))
            elif literal is None or literal.group(1).startswith("/"):
                read.append((begin, chain, UnreadPathAttribute(path, "holds a path attribute that is not a relative string")))
            else:
                read.append((begin, chain, literal.group(1)))
    # The `{` of each inline module whose outer path attribute names its folder.
    inline_owner = {begin: owners[begin] + 2 for begin, _, _ in read
                    if owners.get(begin) is not None and words[owners[begin]] == "mod" and owners[begin] + 2 in blocks}
    folders: dict[int, tuple[str, ...]] = {}
    for begin, _, relative in read:
        if begin in inline_owner and isinstance(relative, str):
            folders[inline_owner[begin]] = (*folders.get(inline_owner[begin], ()), relative)
    result: list[MountedFile | MountedFolder | UnreadPathAttribute] = []
    for begin, chain, relative in read:
        if isinstance(relative, UnreadPathAttribute):
            result.append(relative)
            continue
        readings = tuple(sorted({posixpath.normpath(str(folder / relative))
                                 for folder in {file.parent, *_module_folders(file, chain, words, folders)}}))
        result.append(MountedFolder(path, readings) if begin in inline_owner else MountedFile(path, readings))
    return tuple(result)


def _module_folders(file: PurePosixPath, chain: tuple[int, ...], words: tuple[str, ...],
                    folders: dict[int, tuple[str, ...]]) -> set[PurePosixPath]:
    """The folders that Rust reads a `path` literal from, inside the inline modules that open at chain.

    It starts at the file's folder. An inline module moves to its name, under the file's stem first in a file that is
    not `mod.rs`, `lib.rs` or `main.rs`, and also to the folder that each of its `path` attributes names: a `cfg_attr`
    attribute can be inactive, so a reading is only added, never replaced. A file that another `path` attribute
    mounts reads as `mod.rs`, so both forms are kept.
    """
    stems = ("",) if file.name in {"mod.rs", "lib.rs", "main.rs"} else ("", file.stem)
    states = {(file.parent, stem) for stem in stems}
    for block in chain:
        states = ({(folder.joinpath(*filter(None, (stem, words[block - 1]))), "") for folder, stem in states}
                  | {(folder / literal, "") for folder, _ in states for literal in folders.get(block, ())})
    return {folder for folder, _ in states}


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
    reads: ReadFiles | NoReadFiles = NO_READ_FILES
    ticket: str = ""


@dataclass(frozen=True)
class RuleGenerationChange:
    """One reconciled identity transition and fresh current-text evidence recorded before it."""

    rule: str
    previous_blob: str
    generation: int
    current_blob: str
    fresh_judgments: tuple[int, ...]


@dataclass(frozen=True)
class UnseenTip:
    """No sync finished the tip."""


@dataclass(frozen=True)
class LintCoverage:
    has_runs: bool
    passed: bool
    diagnostics: dict[str, int]
    rule_blobs: dict[str, str]


@dataclass(frozen=True)
class TreeEntry:
    """One `git ls-tree -r` entry: a blob, or a submodule's `commit`."""

    mode: str
    kind: str
    object: str


# A symlink is a blob too, but its content is a path, not Rust.
REGULAR_FILE_MODES = ("100644", "100755")


@dataclass(frozen=True)
class RepositoryTip:
    commit: str
    tree: str
    blobs: dict[str, str]
    # Deepest first, so the first that holds a path is its innermost crate.
    crate_dirs: tuple[str, ...]
    rust_files: tuple[str, ...]
    entries: dict[str, TreeEntry]

    def crate(self, path: str) -> str:
        """The innermost crate directory that holds path, as a decision's crate is."""
        return next((directory for directory in self.crate_dirs if _under(path, directory)), ".")


def _git_bytes(repo: Path, *arguments: str, input: bytes = b"",
               answers: GitExitAnswers = SUCCESS_ONLY) -> subprocess.CompletedProcess[bytes]:
    return run_git(("git", "-C", str(repo), *arguments), input=input, answers=answers)


def _git(repo: Path, *arguments: str) -> str:
    return _git_bytes(repo, *arguments).stdout.decode().rstrip("\n")


def _ticket_operation[**P, T](operation: str) -> Callable[[Callable[Concatenate[Ledger, str, P], T]], Callable[Concatenate[Ledger, str, P], T]]:
    """The public operation owns the refusal; nested operations retain its context."""
    def decorate(method: Callable[Concatenate[Ledger, str, P], T]) -> Callable[Concatenate[Ledger, str, P], T]:
        @wraps(method)
        def run(ledger: Ledger, id: str, *args: P.args, **kwargs: P.kwargs) -> T:
            with ledger.git_operation(f"ticket {id} {operation}"):
                return method(ledger, id, *args, **kwargs)
        return run
    return decorate


def pushed_branch_point_tip(repo: Path, branch_point: str) -> str:
    branch = branch_point.removeprefix("refs/heads/")
    if "origin" in _git(repo, "remote").splitlines():
        ref = f"refs/remotes/origin/{branch}"
        try:
            return _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}")
        except GitReadFailed as error:
            # Preserve the actionable diagnosis and the same typed read failure, including its Git details.
            error.args = (f"origin exists but pushed branch point {ref} cannot be resolved; fetch origin first",)
            raise
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
    listing = _git(repo, "ls-tree", "-rz", "--full-tree", commit)
    entries: dict[str, TreeEntry] = {}
    for entry in listing.split("\0"):
        if not entry:
            continue
        metadata, path = entry.split("\t", 1)
        mode, kind, name = metadata.split()
        entries[path] = TreeEntry(mode, kind, name)
    blobs = {path: entry.object for path, entry in entries.items() if entry.kind == "blob"}
    configs = (".claude/config/berth.toml", "rustfmt.toml", ".rustfmt.toml")
    inputs = {path: blob for path, blob in blobs.items() if PurePosixPath(path).name == "Cargo.toml" or path in configs}
    contents = {path: content.decode(errors="replace") for path, content in blob_contents(repo, inputs)}
    manifests = {path: text for path, text in contents.items() if PurePosixPath(path).name == "Cargo.toml"}
    directories = tuple(member.directory for member in rule_catalog.workspace_members(manifests))
    ignored: list[str] = []
    for config in configs:
        if config in blobs:
            ignored.extend(_ignored_paths(_table(tomllib.loads(contents[config]))))
    files = tuple(sorted(path for path in blobs if path.endswith(".rs") and not _is_ignored(path, tuple(ignored))))
    return RepositoryTip(commit, tree, blobs, directories, files, entries)


def blob_contents(repo: Path, blobs: Mapping[str, str]) -> Iterator[tuple[str, bytes]]:
    """Each path of blobs with the bytes of its blob, read by one `git cat-file --batch`."""
    with tempfile.TemporaryFile() as contents:
        command = ("git", "-C", str(repo), "cat-file", "--batch")
        _ = run_git(command, input="".join(f"{blob}\n" for blob in blobs.values()).encode(), output=contents)
        _ = contents.seek(0)
        for path, blob in blobs.items():
            header = contents.readline().split()
            if len(header) != 3 or header[:2] != [blob.encode(), b"blob"]:
                raise GitReadFailed(command, 0, f"{path}: invalid Git blob response")
            size = int(header[2])
            content = contents.read(size)
            if len(content) != size or contents.read(1) != b"\n":
                raise GitReadFailed(command, 0, f"{path}: incomplete Git blob response")
            yield path, content


def _export_blobs(repo: Path, tip: RepositoryTip, export: Path) -> None:
    for path, content in blob_contents(repo, tip.blobs):
        destination = export / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        _ = destination.write_bytes(content)


def _inputs(tip: RepositoryTip, scope: CellInputScope, path: str) -> dict[str, TreeEntry]:
    """The tree entries a cell's fingerprint covers, by path; a crate scope covers its whole directory tree."""
    if scope in (CellInputScope.MODULE, CellInputScope.FILE) and path not in tip.blobs:
        raise LedgerError(f"{path}: no file at {tip.commit}")
    if scope == CellInputScope.MODULE:
        directory = PurePosixPath(path).parent
        parent_module = directory.as_posix() + ".rs"
        return {name: entry for name, entry in tip.entries.items()
                if name.endswith(".rs") and (PurePosixPath(name).parent == directory or name == parent_module)}
    if scope == CellInputScope.MANIFEST:
        paths = {"Cargo.toml", *(str(PurePosixPath(directory) / "Cargo.toml") for directory in tip.crate_dirs)}
        return {name: entry for name, entry in tip.entries.items() if name in paths}
    if scope == CellInputScope.CRATE:
        directory = tip.crate(path)
        if directory == ".":
            return {name: entry for name, entry in tip.entries.items() if tip.crate(name) == "."}
        return {name: entry for name, entry in tip.entries.items() if _under(name, directory)}
    return {path: tip.entries[path]}


def _input_key(tip: RepositoryTip, scope: CellInputScope, path: str) -> tuple[str, CellInputScope, str]:
    """Cells with the same inputs share a key: a crate scope by its crate, the manifest scope once per commit."""
    return tip.commit, scope, tip.crate(path) if scope == "crate" else "." if scope == "manifest" else path


def _inputs_hash(inputs: dict[str, TreeEntry]) -> str:
    """SHA-256 over the sorted (path, blob) pairs; a submodule entry is not part of it."""
    pairs = sorted((name, entry.object) for name, entry in inputs.items() if entry.kind == "blob")
    return hashlib.sha256(json.dumps(pairs, separators=(",", ":")).encode()).hexdigest()


def _finding_id(fields: tuple[str | int, ...]) -> str:
    """The first 16 hex digits of SHA-256 over the fields of a held finding."""
    return hashlib.sha256(json.dumps(fields, separators=(",", ":")).encode()).hexdigest()[:16]


def _fingerprint(repo: Path, tip: RepositoryTip, scope: CellInputScope, path: str) -> str:
    if scope == CellInputScope.CRATE and tip.crate(path) != ".":
        return _git(repo, "rev-parse", f"{tip.commit}:{tip.crate(path)}")
    inputs = _inputs(tip, scope, path)
    return inputs[path].object if scope == CellInputScope.FILE else _inputs_hash(inputs)


@dataclass(frozen=True)
class RustCode:
    """What a modules judgment reads in a Rust file: its code tokens, and each inner doc with its code position."""

    tokens: tuple[style_footprint.Token, ...]
    # The count of code tokens before each doc, so a `//!` that moves into another `mod` block differs.
    inner_docs: tuple[tuple[int, str], ...]


@dataclass(frozen=True)
class NotTokenized:
    """The file is not UTF-8, or the tokenizer cannot read all of it."""


NOT_TOKENIZED = NotTokenized()


def _rust_code(content: bytes) -> RustCode | NotTokenized:
    try:
        text = content.decode()
    except UnicodeDecodeError:
        return NOT_TOKENIZED
    code = style_footprint.tokens(text)
    try:
        spans = style_footprint.token_spans(text)
    except style_footprint.UnreadableTokenSpans:
        return NOT_TOKENIZED
    if len(spans) != len(code):
        return NOT_TOKENIZED
    # Only whitespace and comments lie between two code tokens, so each gap holds the comments at one code position.
    gaps = (text[start:end] for start, end in zip((0, *(span.end for span in spans)), (*(span.start for span in spans), len(text))))
    # Module docs count toward a facade (when-to-split-a-module.md), so `//!` and `/*!` text is read as code.
    return RustCode(code, tuple((position, doc) for position, gap in enumerate(gaps)
                                for doc in style_footprint.documented_text(gap) if doc.startswith(("//!", "/*!"))))


def _only_comments_changed(before: dict[str, TreeEntry], after: dict[str, TreeEntry],
                           code: Callable[[str], RustCode | NotTokenized]) -> bool:
    """Whether two input sets hold the same paths and each changed entry is a Rust file with the same code and inner docs.

    code reads a blob. A changed mode, a changed submodule, a changed file that is not Rust or does not tokenize, and
    a fingerprint change with no changed entry do not match.
    """
    changed = tuple(name for name, entry in before.items() if after.get(name) != entry)
    if before.keys() != after.keys() or not changed:
        return False
    return all(name.endswith(".rs") and before[name].mode == after[name].mode and before[name].mode in REGULAR_FILE_MODES
               and isinstance(old := code(before[name].object), RustCode) and old == code(after[name].object)
               for name in changed)


def _read_files(serialized: str) -> ReadFiles | NoReadFiles:
    if serialized == "null":
        return NO_READ_FILES
    return ReadFiles(tuple(cast(list[str], json.loads(serialized))))


def _read_inputs(tip: RepositoryTip, path: str, reads: ReadFiles) -> dict[str, TreeEntry]:
    inputs: dict[str, TreeEntry] = {}
    for name in sorted({path, *reads.paths}):
        if name not in tip.blobs:
            raise LedgerError(f"{name}: no file at {tip.commit}")
        inputs[name] = tip.entries[name]
    return inputs


def _read_fingerprint(tip: RepositoryTip, path: str, reads: ReadFiles) -> str:
    return _inputs_hash(_read_inputs(tip, path, reads))


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


def legacy_built_at_sql(finding: str = "held_findings") -> str:
    """The build time of a built finding before schema 25, using its SQL table name or alias."""
    return (f"COALESCE((SELECT updated_at FROM tickets WHERE id={finding}.build_ticket "
            + f"AND state='merged'), {finding}.updated_at)")


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
    18: "ALTER TABLE judgments ADD COLUMN read_files TEXT NOT NULL DEFAULT 'null';",
    19: "ALTER TABLE held_findings ADD COLUMN waive_provenance TEXT NOT NULL DEFAULT 'unrecorded';",
    20: "ALTER TABLE held_findings ADD COLUMN waived_by TEXT NOT NULL DEFAULT '';",
    # A path that `style_plan.py widen` adds to an approved proposal ticket outside its findings, with the reason.
    21: """
        CREATE TABLE ticket_widened_paths (
            ticket TEXT NOT NULL, path TEXT NOT NULL, reason TEXT NOT NULL,
            widened_by TEXT NOT NULL, widened_at TEXT NOT NULL,
            PRIMARY KEY(ticket, path)
        );
    """,
    # The unit run folder and phase whose reservation record the ticket's checkpoint kept for its merge to release.
    22: """
        ALTER TABLE tickets ADD COLUMN reservation_session_dir TEXT NOT NULL DEFAULT '';
        ALTER TABLE tickets ADD COLUMN reservation_phase TEXT NOT NULL DEFAULT '';
    """,
    # Distinguish linted tips from the style branch's own hand-offs (style_sync.py own-handoff).
    23: """
        ALTER TABLE processed_tips ADD COLUMN reason TEXT NOT NULL DEFAULT 'linted';
    """,
    # Findings a bulk rename batch released to ordinary tickets, with the refusal reason (bulk_rename.py).
    24: """
        CREATE TABLE IF NOT EXISTS bulk_refusals (
            finding_id TEXT NOT NULL, ticket TEXT NOT NULL, reason TEXT NOT NULL, at TEXT NOT NULL,
            PRIMARY KEY(finding_id, ticket)
        );
    """,
    25: f"""
        CREATE TABLE refill_results (
            unit TEXT PRIMARY KEY, at TEXT NOT NULL, outcome TEXT NOT NULL,
            split_at TEXT NOT NULL DEFAULT '', ready INTEGER NOT NULL DEFAULT 0,
            changed INTEGER NOT NULL DEFAULT 0, structure INTEGER NOT NULL DEFAULT 0,
            other INTEGER NOT NULL DEFAULT 0
        );
        ALTER TABLE held_findings ADD COLUMN built_at TEXT NOT NULL DEFAULT '';
        UPDATE held_findings SET built_at={legacy_built_at_sql()} WHERE state='built';
    """,
    # Proposal tickets keep their original decisions; a workspace batch shares only its binding proof.
    26: """
        CREATE TABLE IF NOT EXISTS bulk_workspace_tickets (
            ticket TEXT PRIMARY KEY, batch TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS bulk_workspace_batch ON bulk_workspace_tickets(batch);
    """,
    # Permit one checkpointed predecessor and one successor; keep dependency rows across final states.
    27: """
        DROP INDEX IF EXISTS tickets_one_open;
        CREATE UNIQUE INDEX IF NOT EXISTS tickets_one_assigned ON tickets(unit) WHERE state='assigned';
        CREATE TABLE IF NOT EXISTS ticket_predecessors (
            ticket TEXT PRIMARY KEY, predecessor TEXT NOT NULL
        );
    """,
    # Claim ownership is immutable and independent of checkpoint proof sharing.
    28: """
        CREATE TABLE IF NOT EXISTS ticket_batch_owners (
            ticket TEXT PRIMARY KEY REFERENCES tickets(id), batch TEXT NOT NULL CHECK(batch <> '')
        );
    """,
    # Rule changes withdraw without inventing a judgment row or rewriting historical pins.
    29: """
        CREATE TABLE IF NOT EXISTS rule_body_withdrawals (
            finding_id TEXT PRIMARY KEY REFERENCES held_findings(finding_id), rule_blob TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS invalidated_held_judgments (
            judgment_id INTEGER PRIMARY KEY REFERENCES judgments(id), recorded_at TEXT NOT NULL
        );
        CREATE VIEW IF NOT EXISTS active_judgments AS SELECT j.* FROM judgments j WHERE NOT EXISTS (
            SELECT 1 FROM invalidated_held_judgments invalid
            WHERE invalid.judgment_id=j.id AND invalid.recorded_at=j.recorded_at
        );
    """,
    # Generations survive restoration and verdict updates. Removed rows remain deletion barriers.
    30: f"""
        ALTER TABLE judgments ADD COLUMN generation INTEGER NOT NULL DEFAULT 0;
        CREATE TABLE rule_generations (
            rule TEXT PRIMARY KEY, rule_blob TEXT NOT NULL, generation INTEGER NOT NULL
        );
        INSERT INTO rule_generations
            WITH normalized AS (
                SELECT {base_rule_sql('rule')} AS base_rule,rule_blob,recorded_at,id FROM judgments
            ), ranked AS (
                SELECT base_rule,rule_blob,
                    row_number() OVER (PARTITION BY base_rule ORDER BY recorded_at DESC,id DESC) AS rank
                FROM normalized
            ) SELECT base_rule,rule_blob,0 FROM ranked WHERE rank=1;
        UPDATE judgments SET generation=-1 WHERE verdict!='removed' AND {base_rule_sql('rule')} IN (
            SELECT {base_rule_sql('j.rule')} FROM judgments j JOIN invalidated_held_judgments invalid
            ON invalid.judgment_id=j.id AND invalid.recorded_at=j.recorded_at
        );
        DROP VIEW active_judgments;
        CREATE VIEW active_judgments AS SELECT j.* FROM judgments j
            LEFT JOIN rule_generations r ON r.rule={base_rule_sql('j.rule')}
            WHERE j.verdict='removed' OR j.generation=coalesce(r.generation,0);
    """,
    31: """
        CREATE TABLE IF NOT EXISTS ticket_proposals (
            ticket TEXT NOT NULL REFERENCES tickets(id), proposal TEXT NOT NULL,
            PRIMARY KEY(ticket, proposal)
        );
        INSERT OR IGNORE INTO ticket_proposals SELECT id,proposal FROM tickets WHERE kind='proposal';
    """,
}


class GitReadCache:
    """The trees and blob bytes read by one split scan."""

    def __init__(self, repo: Path) -> None:
        self.repo: Path = repo
        self.tips: dict[str, RepositoryTip] = {}
        self.contents: dict[str, bytes] = {}
        self.warmed: set[str] = set()

    def tip(self, commit: str) -> RepositoryTip:
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            commit = _git(self.repo, "rev-parse", "--verify", f"{commit}^{{commit}}")
        if commit not in self.tips:
            self.tips[commit] = _tip(self.repo, commit)
        return self.tips[commit]

    def read(self, commit: str, paths: Iterable[str]) -> None:
        tip = self.tip(commit)
        self.read_blobs(blob for path in paths if (blob := tip.blobs.get(path, "")))

    def read_blobs(self, blobs: Iterable[str]) -> None:
        """Read each immutable blob once, shared by file and site-proof reads."""
        unread = {blob: blob for blob in blobs if blob not in self.contents}
        if unread:
            self.contents.update(blob_contents(self.repo, unread))

    def warm(self, commit: str) -> None:
        """Read source and manifest bytes in one batch for the whole tree."""
        commit = self.tip(commit).commit
        if commit not in self.warmed:
            self.read(commit, (path for path in self.tip(commit).blobs if path.endswith((".rs", ".md", ".toml"))))
            self.warmed.add(commit)

    def blob(self, commit: str, path: str) -> str:
        try:
            return self.tip(commit).blobs.get(path, "")
        except GitReadFailed:
            raise
        except LedgerError:
            return ""

    def crate_directories(self, commit: str) -> tuple[str, ...]:
        return self.tip(commit).crate_dirs

    def file(self, commit: str, path: str) -> bytes | Folder | MissingFile:
        tip = self.tip(commit)
        if path not in tip.blobs:
            return Folder() if any(name.startswith(path.rstrip("/") + "/") for name in tip.entries) else MissingFile()
        self.warm(tip.commit)
        self.read(tip.commit, (path,))
        return self.contents[tip.blobs[path]]

    def text(self, commit: str, path: str) -> str:
        content = self.file(commit, path)
        return content.decode(errors="replace") if isinstance(content, bytes) else ""


@dataclass(frozen=True)
class NoGitReadCache:
    """Ordinary ledger operations use their existing read paths."""


_NO_GIT_READ_CACHE = NoGitReadCache()


class Ledger:
    def __init__(self, repo: Path, database: Path, *, git_cache: GitReadCache | NoGitReadCache = _NO_GIT_READ_CACHE) -> None:
        self.repo: Path = repo
        self._git_operation_depth: int = 0
        self.database: Path = database
        self.git_cache: GitReadCache | NoGitReadCache = git_cache
        self._catalog: tuple[Rule, ...] = self._load_rules()
        self._tips: dict[str, RepositoryTip] = {}
        self._fingerprints: dict[tuple[str, CellInputScope, str], str] = {}
        self._inputs: dict[tuple[str, CellInputScope, str], dict[str, TreeEntry]] = {}
        self._rust_codes: dict[str, RustCode | NotTokenized] = {}
        self._ancestors: dict[tuple[str, str], bool] = {}
        self._branch_tips: dict[str, str] = {}
        # The declaration step's git reads, by commit: what each stored judged commit resolves to, the paths changed
        # from a read commit to a tip, the files at a tip that hold a name as a word (refill reads them too), and a
        # file's content and items at a commit. Rows are read fresh, so a recheck under the write lock sees another writer's changes.
        self._resolved_commits: dict[str, JudgedCommit | NoJudgedCommit] = {}
        self._changed_paths: dict[tuple[str, str], frozenset[str]] = {}
        self._named_files: dict[tuple[str, str, tuple[str, ...]], tuple[str, ...]] = {}
        self._commit_files: dict[tuple[str, str], bytes | Folder | MissingFile] = {}
        self._binding_proofs: dict[tuple[str, str, str], BindingProof] = {}
        self._declared_files: dict[tuple[str, str], DeclaredFile] = {}
        self._site_sources: dict[str, finding_sites.SourceSites] = {}
        self._site_maps: dict[tuple[str, str, str], dict[int, int]] = {}
        self._site_evidence: dict[tuple[HeldFinding, str], SiteEvidence] = {}
        self._follow_evidence: dict[tuple[HeldFinding, str], SiteEvidence] = {}
        self._built_rename_evidence: dict[str, BuiltRenameEvidence] = {}
        self._site_rows_version: tuple[int, int] = (-1, -1)
        self._site_rows: dict[tuple[str, str], tuple[Judgment, ...]] = {}
        self._inactive_site_holders: set[tuple[str, str]] = set()
        self._site_holding_rows: dict[tuple[str, str], list[tuple[str, Judgment]]] = {}
        self._site_rule_families: dict[str, str] = {}
        self._file_modules: dict[tuple[str, str], dict[str, RootedFileModule]] = {}
        self._outside_diffs: dict[tuple[str, str, str, str], str] = {}
        self._extern_names: dict[tuple[str, str], frozenset[str] | UnresolvedTypeArguments] = {}
        self._crate_names: dict[tuple[str, str, str], bool] = {}
        self._outside_bodies: dict[tuple[str, str, str, DeclaredItem, str, DeclaredItem], tuple[int, int, tuple[OutsideFailure, ...]]] = {}
        self._item_bodies: dict[tuple[str, str, DeclaredItem], declarations.ReadableBody | declarations.UnreadableBody] = {}
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

    @contextmanager
    def git_operation(self, context: str) -> Generator[None]:
        """Translate once, after transaction rollback; retain nested operation names."""
        self._git_operation_depth += 1
        try:
            yield
        except GitReadFailed as error:
            if self._git_operation_depth > 1:
                error.operations = (context, *error.operations)
                raise
            nested = " (" + ", ".join(part.rsplit(" ", 1)[-1] for part in error.operations) + ")" if error.operations else ""
            raise LedgerError(f"{context}{nested}: {error}") from error
        finally:
            self._git_operation_depth -= 1

    def _load_rules(self) -> tuple[Rule, ...]:
        return rule_catalog.load_rules(self.repo)

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
        # Only full object names are immutable; resolve branch names before either cache.
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            commit = _git(self.repo, "rev-parse", "--verify", f"{commit}^{{commit}}")
        if isinstance(self.git_cache, GitReadCache):
            return self.git_cache.tip(commit)
        if commit not in self._tips:
            self._tips[commit] = _tip(self.repo, commit)
        return self._tips[commit]

    def _fingerprint(self, tip: RepositoryTip, scope: CellInputScope, path: str) -> str:
        key = _input_key(tip, scope, path)
        if key not in self._fingerprints:
            self._fingerprints[key] = _fingerprint(self.repo, tip, scope, path)
        return self._fingerprints[key]

    def _scope_inputs(self, tip: RepositoryTip, scope: CellInputScope, path: str) -> dict[str, TreeEntry]:
        """What _inputs selects, selected once."""
        key = _input_key(tip, scope, path)
        if key not in self._inputs:
            self._inputs[key] = _inputs(tip, scope, path)
        return self._inputs[key]

    def _rust_code(self, blob: str) -> RustCode | NotTokenized:
        """What _rust_code reads in a blob, read once."""
        if blob not in self._rust_codes:
            if isinstance(self.git_cache, GitReadCache):
                self.git_cache.read_blobs((blob,))
                self._rust_codes[blob] = _rust_code(self.git_cache.contents[blob])
                return self._rust_codes[blob]
            result = _git_bytes(self.repo, "cat-file", "blob", blob)
            self._rust_codes[blob] = _rust_code(result.stdout)
        return self._rust_codes[blob]

    def _judged_commit_missing(self, commit: str) -> bool:
        """A missing historical judgment cannot judge current inputs; a failed lookup still refuses."""
        result = _git_bytes(self.repo, "rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}", answers=GitExitAnswers(frozenset({0, 1})))
        return result.returncode == 1

    def _judgment_inputs_match(self, row: Judgment, tip: RepositoryTip, scope: CellInputScope, path: str, family: Family | None) -> bool:
        """Whether the judged inputs are unchanged at tip.

        Names rules judge comment text, so their inputs must be byte equal. A modules judgment also matches when only
        comments other than inner docs changed; that check reads the inputs at the judged commit, so it runs only
        after the fingerprints differ.
        """
        try:
            if isinstance(row.reads, ReadFiles):
                if row.input_fingerprint == _read_fingerprint(tip, path, row.reads):
                    return True
                if family != "modules":
                    return False
                return _only_comments_changed(_read_inputs(self._tip(row.commit), path, row.reads),
                                              _read_inputs(tip, path, row.reads), self._rust_code)
            scope = _comparison_scope(row.declared_scope, scope)
            previous = (row.input_fingerprint if row.input_scope == scope
                        else self._fingerprint(self._tip(row.commit), scope, path))
            if previous == self._fingerprint(tip, scope, path):
                return True
            if family != "modules":
                return False
            return _only_comments_changed(self._scope_inputs(self._tip(row.commit), scope, path),
                                          self._scope_inputs(tip, scope, path), self._rust_code)
        except GitReadFailed:
            if self._judged_commit_missing(row.commit):
                return False
            raise
        except LedgerError:
            return False

    def _is_ancestor(self, commit: str, tip: str) -> bool:
        key = (commit, tip)
        if key not in self._ancestors:
            result = _git_bytes(self.repo, "merge-base", "--is-ancestor", commit, tip, answers=GitExitAnswers(frozenset({0, 1})))
            self._ancestors[key] = result.returncode == 0
        return self._ancestors[key]

    def set_tip(self, tip: str, style_branch: str | None = None) -> None:
        commit = _git(self.repo, "rev-parse", "--verify", f"{tip}^{{commit}}")
        if style_branch is not None:
            style_branch = "refs/heads/" + style_branch.removeprefix("refs/heads/")
            _ = _git(self.repo, "check-ref-format", style_branch)
        # The sites exist before the tip is visible, so the write lock holds only the sync row.
        self._catalog = self._load_rules()
        self._refresh_sites(self._tip(commit))
        with self._connection:
            _ = self._connection.execute("BEGIN IMMEDIATE")
            _ = self._connection.execute(
                "INSERT INTO sync(id,tip,style_branch,synced_at) VALUES (1,?,?,?) ON CONFLICT(id) DO UPDATE SET tip=excluded.tip,style_branch=COALESCE(?,sync.style_branch),synced_at=excluded.synced_at",
                (commit, style_branch or "", _now(), style_branch),
            )
            self._check_ticket_pipelines()

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
        widened = cast(list[tuple[str, str, str, str]], self._connection.execute(
            "SELECT path,reason,widened_by,widened_at FROM ticket_widened_paths WHERE ticket=? ORDER BY path",
            (ticket_id,),
        ).fetchall())
        yielded = cast(list[tuple[str, str]], self._connection.execute(
            "SELECT path,recorded_at FROM ticket_yields WHERE ticket=? ORDER BY path", (ticket_id,),
        ).fetchall())
        removed = sorted((judgment.rule, judgment.path, judgment.commit) for judgment in self.latest_ticket_judgments(ticket_id)
                         if judgment.verdict == "removed")
        return {
            "id": row[0], "unit": row[1], "kind": row[2], "family": row[3], "rule": row[4], "crate": row[5],
            "files": cast(list[str], json.loads(row[6])), "sites": cast(list[dict[str, object]], json.loads(row[7])),
            "rules": cast(list[str], json.loads(row[8])), "rule_blobs": cast(dict[str, str], json.loads(row[9])),
            "state": row[10], "branch": row[11], "checkpoint": row[12], "created_at": row[13], "updated_at": row[14],
            "base": row[15], "proposal": row[16], "cells": cast(list[list[str]], json.loads(row[17])),
            "finding_ids": cast(list[str], json.loads(row[18])),
            "widened": tuple(WidenedPath(path=path, reason=reason, widened_by=by, widened_at=at)
                             for path, reason, by, at in widened),
            "yielded": tuple(YieldedPath(path=path, yielded_at=at) for path, at in yielded),
            "removed": tuple(removed_cell(ticket_id, rule, path, commit) for rule, path, commit in removed),
        }

    def _check_pin(self, ticket: str, rule: str, rule_blob: str) -> str:
        rows = cast(list[tuple[str, str]], self._connection.execute(
            "SELECT rule_blobs,state FROM tickets WHERE id=?", (ticket,)).fetchall())
        if rows:
            pins = cast(dict[str, str], json.loads(rows[0][0]))
            if rows[0][1] in FINAL_TICKET_STATES:
                raise LedgerError(f"{ticket}: cannot judge from {rows[0][1]}")
            if rule not in pins:
                raise LedgerError(f"{ticket}: rule {rule} is outside the ticket")
            definition = self._component_rule(rule)
            if rule_blob and rule_blob != pins[rule]:
                if not rule_catalog.judged_text_unchanged(rule_blob, definition, compared_blob=pins[rule]):
                    raise LedgerError(f"{ticket}: rule blob for {rule} differs from ticket pin {pins[rule]}")
            rule_blob = rule_blob or pins[rule]
            if not rule_catalog.judged_text_unchanged(pins[rule], definition):
                raise LedgerError(f"{ticket}: rule body changed for {rule}; ordinary refill must judge it again")
        if not rule_blob:
            raise LedgerError(f"{rule}: --rule-blob is required without a ledger ticket")
        if not rule_catalog.judged_text_unchanged(rule_blob, self._component_rule(rule)):
            raise LedgerError(f"{ticket}: rule body changed for {rule}; ordinary refill must judge it again")
        return rule_blob

    @_ticket_operation("add")
    def ticket_add(
        self, id: str, unit: str, kind: TicketKind, family: str, crate: str,
        files: list[str], rules: list[str], rule: str = "", sites: list[dict[str, object]] | None = None,
        proposal: str = "", cells: tuple[tuple[str, str], ...] = (),
        finding_ids: tuple[str, ...] = (),
        *, batch_owner: BatchOwner | UnbatchedTicket = UNBATCHED_TICKET,
        proposals: tuple[Path, ...] = (),
    ) -> None:
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
        decisions = tuple(sorted(set((Path(proposal), *proposals)))) if proposal else ()

        def added() -> bool:
            """Whether the ticket exists with these fields; refuses other fields, or findings a proposal cannot build."""
            exists = cast(list[tuple[str]], self._connection.execute("SELECT id FROM tickets WHERE id=?", (id,)).fetchall())
            if exists:
                previous = self._ticket(id)
                if self.ticket_batch_owner(id) != batch_owner:
                    raise LedgerError(f"{id}: ticket already exists with a different batch owner")
                if self.ticket_proposals(id) != decisions:
                    raise LedgerError(f"{id}: ticket already exists with different proposal decisions")
                if (previous["unit"], previous["kind"], previous["family"], previous["crate"], previous["files"], previous["rules"], previous["rule"], previous["sites"], previous["proposal"], previous["cells"], previous["finding_ids"]) != (unit, kind, family, crate, files, rules, rule, sites or [], proposal, [list(cell) for cell in cells], list(finding_ids)):
                    raise LedgerError(f"{id}: ticket already exists in state {previous['state']} with different fields")
                return True
            if not re.fullmatch(re.escape(unit) + r"-\d+", id):
                raise LedgerError(f"{id}: ticket id must be {unit}-<n>")
            if kind == "proposal":
                findings = tuple(self._held(finding_id) for finding_id in finding_ids)
                if not proposal or not findings:
                    raise LedgerError(f"{id}: proposal requires filed findings")
                for finding in findings:
                    if not isinstance(finding.state, Filed) or finding.state.proposal not in decisions:
                        raise LedgerError(f"{id}: proposal requires filed findings; {finding.finding_id} is not filed to {proposal}")
                    if isinstance(finding.build_ticket, BuildTicket):
                        owner = self._ticket(finding.build_ticket.ticket)
                        if owner["state"] not in FINAL_TICKET_STATES:
                            raise LedgerError(f"{finding.finding_id}: build ticket {owner['id']} already holds finding")
                if {finding.state.proposal for finding in findings if isinstance(finding.state, Filed)} != set(decisions):
                    raise LedgerError(f"{id}: each proposal decision must hold a ticket finding")
                if set(files) != {finding.path for finding in findings} or set(rules) != {finding.rule for finding in findings}:
                    raise LedgerError(f"{id}: proposal files and rules must match its filed findings")
                if len(decisions) > 1:
                    snapshot = self._tip(self.synced_commit())
                    if any(snapshot.crate(finding.path) != crate or self._component_rule(finding.rule).family != family
                           for finding in findings):
                        raise LedgerError(f"{id}: combined decisions must share one family and crate")
            elif proposal or finding_ids or proposals:
                raise LedgerError(f"{id}: proposal findings are for proposal tickets only")
            return False

        def attempt() -> None:
            if added():
                return
            # Pin the rule texts before the write lock; under the lock the ticket checks run again.
            self._catalog = self._load_rules()
            definitions = {rule_id: self._component_rule(rule_id) for rule_id in rules}
            texts: list[PinnedRuleText] = []
            for rule_id, definition in definitions.items():
                text = definition.path.read_bytes()
                result = _git_bytes(self.repo, "hash-object", "--stdin", input=text)
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
                _ = self._connection.executemany("INSERT INTO ticket_proposals VALUES (?,?)",
                                                 [(id, str(decision)) for decision in decisions])
                if isinstance(batch_owner, BatchOwner):
                    _ = self._connection.execute(
                        "INSERT INTO ticket_batch_owners VALUES (?,?)", (id, batch_owner.batch),
                    )
                for finding_id in finding_ids:
                    _ = self._connection.execute(
                        "UPDATE held_findings SET build_ticket=?,updated_at=? WHERE finding_id=?", (id, now, finding_id),
                    )

        retry_on_change(attempt)

    def ticket_batch_owner(self, id: str) -> BatchOwner | UnbatchedTicket:
        rows = cast(list[tuple[str]], self._connection.execute(
            "SELECT batch FROM ticket_batch_owners WHERE ticket=?", (id,),
        ).fetchall())
        return BatchOwner(rows[0][0]) if rows else UNBATCHED_TICKET

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
            "SELECT id FROM tickets WHERE substr(id,1,?)=?", (len(unit) + 1, unit + "-"),
        ).fetchall())
        highest = max((int(row[0][len(unit) + 1:]) for row in rows
                       if re.fullmatch(re.escape(unit) + r"-\d+", row[0])), default=0)
        return f"{unit}-{highest + 1}"

    def _ticket_cells(self, ticket: TicketRecord) -> frozenset[tuple[str, str]]:
        """The rule and path cells the ticket owns, less the paths it yielded."""
        yielded = {yielded["path"] for yielded in ticket["yielded"]}
        if ticket["cells"]:
            return frozenset((rule, path) for rule, path in ticket["cells"] if path not in yielded)
        paths = [ticket["crate"]] if ticket["kind"] == "structure" else ticket["files"]
        rules = [ticket["rule"]] if ticket["kind"] == "listed" else ticket["rules"]
        return frozenset((rule, path) for rule in rules for path in paths if path not in yielded)

    def unjudged_ticket_cells(self, id: str, tip: str, exists: Callable[[str], bool]) -> tuple[UnjudgedTicketCell, ...]:
        """The cells the ticket owns with no verdict of the ticket at tip. A proposal ticket owns none.

        A cell with a removed row of any ticket is judged by a row of this ticket while exists says nothing is at its path.
        A file there again is a new cell: the removals of every ticket in tip set the boundary, as coverage reads them, and
        only a row of this ticket after them judges it (_rows_after_removals).
        """
        ticket = self._ticket(id)
        if ticket["kind"] == "proposal":
            return ()
        unjudged: list[UnjudgedTicketCell] = []
        for rule, path in sorted(self._ticket_cells(ticket)):
            rows = self._judgments(rule, path)
            if any(row.verdict == "removed" for row in rows) and exists(path):
                commit = _git(self.repo, "rev-parse", "--verify", f"{tip}^{{commit}}")
                judged = any(row.ticket == id and row.verdict != "removed" for row in self._rows_after_removals(rows, commit))
            else:
                judged = any(row.ticket == id for row in rows)
            if not judged:
                unjudged.append(UnjudgedTicketCell(rule, path))
        return tuple(unjudged)

    @_ticket_operation("yield")
    def ticket_yield(self, id: str, path: str) -> tuple[str, ...]:
        """Give up the assigned ticket's work on path, and return the proposal findings that the yield frees.

        The ticket's verdicts and unfiled held findings on path go. A proposal ticket also yields its findings at path:
        each stays filed under its decision with no build ticket, so a later proposal ticket takes it, and this ticket
        no longer lists it. It refuses a path with no finding of the ticket, a path that holds every finding the ticket
        has left, and a yield that a kept finding can need (_yield_couplings). A structure ticket refuses. A recorded
        yield changes nothing. The git reads run before the write lock; if the ticket, its findings or the commit they
        read change before the lock, the yield starts again.
        """
        path = self._path(path)

        def recorded(ticket: TicketRecord) -> bool:
            """Whether the ticket already yielded path; refuses a ticket that cannot yield."""
            if ticket["state"] != "assigned":
                raise LedgerError(f"{id}: cannot yield from {ticket['state']}")
            if ticket["kind"] == "structure":
                raise LedgerError(f"{id}: cannot yield a structure ticket")
            return any(yielded["path"] == path for yielded in ticket["yielded"])

        def attempt() -> tuple[str, ...]:
            ticket = self._ticket(id)
            if recorded(ticket):
                return ()
            findings = tuple(self._held(finding_id) for finding_id in ticket["finding_ids"])
            read = self._yielded_findings(ticket, findings, path)
            freed = read.freed if isinstance(read, FindingYield) else ()
            with self._connection:
                _ = self._connection.execute("BEGIN IMMEDIATE")
                current = self._ticket(id)
                if recorded(current):
                    return ()
                if current != ticket or tuple(self._held(finding_id) for finding_id in current["finding_ids"]) != findings:
                    raise ChangedBeforeLock(f"{id}: the ticket changed while it yielded {path}; rerun")
                if isinstance(read, FindingYield) and (current["base"] or self.synced_commit()) != read.read_commit:
                    raise ChangedBeforeLock(f"{id}: the synced tip moved while it yielded {path}; rerun")
                now = _now()
                _ = self._connection.execute("DELETE FROM judgments WHERE ticket=? AND path=?", (id, path))
                _ = self._connection.execute(
                    "DELETE FROM held_findings WHERE ticket=? AND path=? AND state='unfiled'", (id, path),
                )
                if freed:
                    _ = self._connection.executemany(
                        "UPDATE held_findings SET build_ticket='',updated_at=? WHERE finding_id=?",
                        [(now, finding_id) for finding_id in freed],
                    )
                    _ = self._connection.execute(
                        "UPDATE tickets SET finding_ids=?,updated_at=? WHERE id=?",
                        (json.dumps([finding_id for finding_id in ticket["finding_ids"] if finding_id not in freed]), now, id),
                    )
                _ = self._connection.execute("INSERT INTO ticket_yields VALUES (?,?,?)", (id, path, now))
            return freed

        return retry_on_change(attempt)

    def bulk_ticket(self, id: str) -> TicketRecord:
        """The assigned proposal ticket a bulk batch may edit."""
        ticket = self._ticket(id)
        if ticket["kind"] != "proposal" or ticket["state"] != "assigned":
            raise LedgerError(f"{id}: bulk rename requires an assigned proposal ticket")
        return ticket

    def bulk_findings(self, id: str) -> tuple[HeldFinding, ...]:
        """The bulk ticket's findings, each still filed and held by this ticket."""
        ticket = self.bulk_ticket(id)
        findings = tuple(self._held(identity) for identity in ticket["finding_ids"])
        if any(not isinstance(finding.state, Filed) or finding.build_ticket != BuildTicket(id) for finding in findings):
            raise LedgerError(f"{id}: a bulk finding is no longer filed under this build ticket")
        return findings

    def bulk_refusals(self, ticket: str) -> tuple[BulkRefusal, ...]:
        rows = cast(list[tuple[str, str, str, str]], self._connection.execute(
            "SELECT finding_id,ticket,reason,at FROM bulk_refusals WHERE ticket=? ORDER BY finding_id", (ticket,),
        ).fetchall())
        return tuple(BulkRefusal(*row) for row in rows)

    def bulk_share_checkpoint(self, ids: Sequence[str]) -> None:
        """Persist one workspace proof group after every decision ticket reaches the same checkpoint.

        This shares one family's workspace evidence, never approval or finding ownership. A partial checkpoint is recoverable:
        repeat the ordinary checkpoints, then this operation. No proof group exists until all have succeeded.
        """
        members = tuple(sorted(set(ids)))
        if not members:
            return
        with self._connection:
            _ = self._connection.execute("BEGIN IMMEDIATE")
            tickets = tuple(self._ticket(id) for id in members)
            first = tickets[0]
            signature = (first["base"], first["branch"], first["checkpoint"], first["rules"])
            if (not all(signature[:3]) or len(first["rules"]) != 1 or first["family"] not in {"names", "values"}
                    or any(ticket["kind"] != "proposal" or ticket["family"] != first["family"]
                           or ticket["crate"] != "." or ticket["state"] not in ("checkpointed", "merged")
                           or (ticket["base"], ticket["branch"], ticket["checkpoint"], ticket["rules"]) != signature
                           for ticket in tickets)):
                raise LedgerError("workspace proof requires one rule, base, branch and checkpoint on same-family names or values proposal tickets")
            existing = cast(list[tuple[str, str]], self._connection.execute(
                "SELECT ticket,batch FROM bulk_workspace_tickets WHERE batch IN "
                + "(SELECT batch FROM bulk_workspace_tickets WHERE ticket IN ("
                + ",".join("?" for _ in members) + "))", members,
            ).fetchall())
            if existing and (set(id for id, _ in existing) != set(members)
                             or any(batch != members[0] for _, batch in existing)):
                raise LedgerError("workspace ticket already belongs to another proof group")
            _ = self._connection.executemany(
                "INSERT OR IGNORE INTO bulk_workspace_tickets VALUES (?,?)", [(id, members[0]) for id in members],
            )
        self._binding_proofs.clear()

    def _binding_renames(self, ticket: TicketRecord, path: str, commit: str) -> tuple[BindingRename, ...]:
        """The ticket's own renames plus recorded companions at this exact checkpoint."""
        membership = "t.id=?"
        parameters: tuple[str, ...] = (path, ticket["id"])
        if ticket["checkpoint"] == commit:
            membership += (" OR (t.id IN (SELECT ticket FROM bulk_workspace_tickets WHERE batch="
                           + "(SELECT batch FROM bulk_workspace_tickets WHERE ticket=?)) "
                           + "AND t.state IN ('checkpointed','merged') AND t.base=? AND t.branch=? "
                           + "AND t.checkpoint=? AND t.rules=?)")
            parameters += (ticket["id"], ticket["base"], ticket["branch"], commit, json.dumps(ticket["rules"]))
        # Filter by path in SQL: a workspace proof must not load every finding once per edited file.
        rows = cast(list[tuple[str, str, str, int, str]], self._connection.execute(
            "SELECT h.kind,h.before,h.after,h.line,h.text FROM tickets t JOIN json_each(t.finding_ids) f "
            + "JOIN held_findings h ON h.finding_id=f.value WHERE h.path=? AND (" + membership + ") "
            + "ORDER BY t.id,h.finding_id", parameters,
        ).fetchall())
        return tuple(pair for kind, before, after, line, text in rows
                     if isinstance(pair := binding_rename(kind, before, after, line, text), BindingRename))

    def bulk_release(self, id: str, reasons: Mapping[str, str]) -> tuple[BulkRefusal, ...]:
        """Record refusals and release only those findings, atomically; repeated refusals change nothing.

        Each finding stays filed under its decision. Removing ticket membership prevents merge-time checks from
        counting it as part of this batch, and clearing build_ticket makes it available to an ordinary ticket.
        """
        if any(not reason.strip() for reason in reasons.values()):
            raise LedgerError("bulk refusal requires a reason")
        with self._connection:
            _ = self._connection.execute("BEGIN IMMEDIATE")
            ticket = self.bulk_ticket(id)
            recorded = self.bulk_refusals(id)
            known = {row.finding_id for row in recorded}
            pending = {identity: reason for identity, reason in reasons.items() if identity not in known}
            if not pending:
                return recorded
            for identity in pending:
                finding = self._held(identity)
                if (identity not in ticket["finding_ids"] or finding.build_ticket != BuildTicket(id)
                        or not isinstance(finding.state, Filed)):
                    raise LedgerError(f"{identity}: finding is not filed under bulk ticket {id}")
            now = _now()
            for identity, reason in pending.items():
                _ = self._connection.execute("INSERT INTO bulk_refusals VALUES (?,?,?,?)", (identity, id, reason, now))
                _ = self._connection.execute(
                    "UPDATE held_findings SET build_ticket='',updated_at=? WHERE finding_id=?", (now, identity),
                )
            _ = self._connection.execute("UPDATE tickets SET finding_ids=?,updated_at=? WHERE id=?",
                                         (json.dumps([identity for identity in ticket["finding_ids"] if identity not in pending]), now, id))
        self._binding_proofs = {key: proof for key, proof in self._binding_proofs.items() if key[0] != id}
        return self.bulk_refusals(id)

    def _yielded_findings(self, ticket: TicketRecord, findings: tuple[HeldFinding, ...],
                          path: str) -> FileYield | FindingYield:
        """What a yield of path frees; refuses a path the ticket cannot yield.

        A proposal ticket frees its findings at path, read at its base, or at the synced tip while it has none. Any
        other ticket yields a file of its own and frees no finding.
        """
        if ticket["kind"] != "proposal":
            if path not in ticket["files"]:
                raise LedgerError(f"{ticket['id']}: {path} is not in ticket files")
            return FileYield()
        freed = tuple(finding for finding in findings if finding.path == path)
        kept = tuple(finding for finding in findings if finding.path != path)
        if not freed:
            raise LedgerError(f"{ticket['id']}: no finding of the ticket is at {path}")
        if not kept:
            raise LedgerError(f"{ticket['id']}: {path} holds every finding the ticket has left; "
                              + "drop the ticket with style_ledger.py ticket drop")
        commit = ticket["base"] or self.synced_commit()
        couplings = self._yield_couplings(commit, freed, kept)
        if couplings:
            raise LedgerError(f"{ticket['id']}: a kept finding can need a finding at {path}, so the ticket cannot "
                              + "yield it; drop the ticket instead: " + " | ".join(couplings))
        return FindingYield(tuple(finding.finding_id for finding in freed), commit)

    def _yield_couplings(self, commit: str, freed: tuple[HeldFinding, ...], kept: tuple[HeldFinding, ...]) -> tuple[str, ...]:
        """Why a kept finding can need a freed one to build, read at commit; none when it cannot.

        A kept finding in a crate with no freed finding cannot. In a crate that holds both, each finding of the crate
        must be a plain file rename (`file_rename`), and: no freed file names a kept module, by its old or new name;
        no freed rename and kept rename share a new name, and neither takes the other's old name; and no file of the
        crate reaches a renamed module by a path that holds none of its names (_hidden_module_paths).
        """
        tip = self._tip(commit)
        reasons = self._crossing_mounts(tip, frozenset(tip.crate(finding.path) for finding in (*freed, *kept)))
        for crate in sorted({tip.crate(finding.path) for finding in freed}):
            shared = tuple(finding for finding in kept if tip.crate(finding.path) == crate)
            if shared:
                yielded = tuple(finding for finding in freed if tip.crate(finding.path) == crate)
                reasons += self._crate_couplings(commit, crate, yielded, shared)
        return tuple(dict.fromkeys(reasons))

    def _crossing_mounts(self, tip: RepositoryTip, crates: frozenset[str]) -> list[str]:
        """Why a `path` attribute in any Rust file of the repository can join a crate of crates to another crate.

        A reading outside the declaring crate's folder mounts a file of one crate into another, so a finding's crate
        is not all the code that compiles its file. It refuses when the declaring crate or the mounted file's crate
        is in crates; a folder that an inline module's attribute names is read the same way. An unread attribute, and one
        that mounts a file not named `.rs`, refuse wherever they are. One `git cat-file --batch` reads every file.
        """
        reasons: list[str] = []
        blobs = {path: blob for path, blob in tip.blobs.items() if path.endswith(".rs")}
        for path, content in blob_contents(self.repo, blobs):
            for attribute in _path_attributes(path, content):
                if isinstance(attribute, UnreadPathAttribute):
                    reasons.append(f"{path} {attribute.reason}")
                    continue
                home = tip.crate(path)
                # Only `.rs` files are read here, so a mounted file of another name could mount further files unseen.
                if isinstance(attribute, MountedFile):
                    reasons += [f"{path} mounts {reading}, which is not a .rs file" for reading in attribute.readings
                                if not reading.endswith(".rs")]
                kind = "file" if isinstance(attribute, MountedFile) else "folder"
                for reading in attribute.readings:
                    outside = reading.startswith("..") or tip.crate(reading) != home
                    if outside and (home in crates or (not reading.startswith("..") and tip.crate(reading) in crates)):
                        reasons.append(f"{path} mounts {reading}, a {kind} outside {home}")
        return reasons

    def _crate_couplings(self, commit: str, crate: str, freed: tuple[HeldFinding, ...],
                         kept: tuple[HeldFinding, ...]) -> list[str]:
        moves = {finding.finding_id: file_rename(finding.kind, finding.path, finding.after) for finding in (*freed, *kept)}
        renames = {finding_id: moved for finding_id, moved in moves.items() if isinstance(moved, FileRename)}
        if len(renames) < len(moves):
            return [f"{finding_id} is not a file rename, and {crate} holds a freed and a kept finding"
                    for finding_id in moves if finding_id not in renames]
        reasons: list[str] = []
        for finding in freed:
            yielded = renames[finding.finding_id]
            words = _code_words(self._commit_file(commit, finding.path))
            for other in kept:
                moved = renames[other.finding_id]
                names = {moved.old_stem, moved.new_stem}
                if isinstance(words, UnreadableCode):
                    reasons.append(f"{finding.path} does not tokenize")
                else:
                    reasons += [f"{finding.path} names {name}, a module that {other.finding_id} renames"
                                for name in sorted(names & set(words))]
                shared = ({yielded.new_stem} & names) | ({moved.new_stem} & {yielded.old_stem})
                reasons += [f"{finding.finding_id} and {other.finding_id} both rename to or from {name}" for name in sorted(shared)]
        return reasons + self._hidden_module_paths(commit, crate, tuple(renames.values()))

    def _hidden_module_paths(self, commit: str, crate: str, renames: tuple[FileRename, ...]) -> list[str]:
        """Why a file of the crate can reach a renamed module by a path that holds none of its names.

        A public use of a module name re-exports it, a glob use of a renamed module's parent imports its name, and an
        `include`, `macro_rules` or `macro` word can build a path that no scan reads. Modules come from the folder
        layout under `src` (style_footprint.crate_module), and each file of the crate must tokenize.
        """
        root = PurePosixPath(crate) / "src"
        reasons: list[str] = []
        parents: set[tuple[str, ...]] = set()
        for moved in renames:
            module = style_footprint.crate_module(moved.old, root)
            if isinstance(module, style_footprint.OutsideCrate):
                reasons.append(f"{moved.old} is not a module file of {crate}")
            else:
                parents.add(module[:-1])
        names = {name for moved in renames for name in (moved.old_stem, moved.new_stem)}
        tip = self._tip(commit)
        for path in (path for path in tip.rust_files if tip.crate(path) == crate):
            words = _code_words(self._commit_file(commit, path))
            if isinstance(words, UnreadableCode):
                reasons.append(f"{path} does not tokenize")
                continue
            reasons += [f"{path} holds {word}" for word in sorted(style_footprint.UNREAD_CODE_WORDS & set(words))]
            uses = style_footprint.use_items(words)
            reasons += [f"{path} re-exports {name}" for use in uses if "pub" in words[use.start:use.end]
                        for name in sorted(names & set(words[use.start:use.end]))]
            trees = style_footprint.use_trees(words, style_footprint.code_item_starts(words))
            if any(words[index] == "*" and index not in trees.prefixes for use in uses for index in range(use.start, use.end)):
                reasons.append(f"{path} holds a glob use that the scan cannot read")
            globs = [index for index in trees.prefixes if words[index] == "*"]
            if not globs:
                continue
            home = style_footprint.crate_module(path, root)
            scopes = style_footprint.import_context(words).scopes
            for index in globs:
                sources = _glob_sources(trees.prefixes[index], home, tuple(name.split("@")[0] for name in scopes[index].module))
                if isinstance(sources, style_footprint.UnresolvedPath):
                    reasons.append(f"{path} holds a glob use that the scan cannot resolve")
                else:
                    reasons += [f"{path} glob-imports {'::'.join(('crate', *source))}" for source in sources if source in parents]
        return reasons

    @_ticket_operation("widen")
    def ticket_widen(self, id: str, paths: tuple[str, ...], reason: str, by: str,
                     approved: Callable[[], frozenset[str]]) -> tuple[WidenedPath, ...]:
        """Add paths outside the findings to an open proposal ticket, and return the ticket's widened paths.

        approved reads the decision and returns the paths that its findings and approved renames already allow; it
        raises when the decision is not approved. A path widened again with the same reason changes nothing.
        """
        reason, by = reason.strip(), by.strip()
        if not reason:
            raise LedgerError(f"{id}: widen requires a reason")
        if not by:
            raise LedgerError(f"{id}: widen requires --by")
        normalized = tuple(dict.fromkeys(self._path(path) for path in paths))
        with self._connection:
            _ = self._connection.execute("BEGIN IMMEDIATE")
            ticket = self._ticket(id)
            if ticket["kind"] != "proposal":
                raise LedgerError(f"{id}: only a proposal ticket widens; this is a {ticket['kind']} ticket")
            if ticket["state"] in FINAL_TICKET_STATES:
                raise LedgerError(f"{id}: cannot widen a {ticket['state']} ticket")
            previous = {widened["path"]: widened["reason"] for widened in ticket["widened"]}
            for path in normalized:
                if path in previous and previous[path] != reason:
                    raise LedgerError(f"{id}: {path} is already widened for another reason: {previous[path]}")
            if all(path in previous for path in normalized):
                return ticket["widened"]
            allowed = approved()
            for path in normalized:
                if path in allowed:
                    raise LedgerError(f"{id}: {path} is already allowed by the approved findings")
                if path in previous and previous[path] != reason:
                    raise LedgerError(f"{id}: {path} is already widened for another reason: {previous[path]}")
            now = _now()
            _ = self._connection.executemany(
                "INSERT INTO ticket_widened_paths VALUES (?,?,?,?,?)",
                [(id, path, reason, by, now) for path in normalized if path not in previous],
            )
            self._check_ticket_pipelines(ticket["unit"])
            return self._ticket(id)["widened"]

    def _check_ticket_pipelines(self, unit: str = "") -> None:
        """Reprove disjointness under the caller's write lock, including its pending writes.

        Both assignment and footprint changes use this boundary. Ordinary tickets read no sources here.
        """
        pairs = cast(list[tuple[str, str]], self._connection.execute(
            "SELECT p.predecessor,p.ticket FROM ticket_predecessors p JOIN tickets t ON t.id=p.ticket "
            + "WHERE t.state IN ('assigned','checkpointed') AND (?='' OR t.unit=?)", (unit, unit),
        ).fetchall())
        for predecessor, successor in pairs:
            self._check_ticket_pair(self._ticket(predecessor), self._ticket(successor))

    @_ticket_operation("resume check")
    def check_ticket_pipeline(self, id: str) -> None:
        """Revalidate a resumed ticket before publishing work; footprint writes cannot race this reading."""
        with self._connection:
            _ = self._connection.execute("BEGIN IMMEDIATE")
            ticket = self._ticket(id)
            if ticket["state"] in FINAL_TICKET_STATES:
                raise LedgerError(f"{id}: cannot resume or gate from {ticket['state']}")
            self._check_ticket_pipelines(ticket["unit"])

    def _pipeline_footprint(self, ticket: TicketRecord, commits: frozenset[str]) -> frozenset[str]:
        """Conservative edit paths at every relevant immutable tree, including current finding rows.

        Selection can parse exact module references. This mutation guard includes every mention of a module name
        and every possible path-attribute owner instead: a false overlap waits, never silently drops a reference.
        """
        files = {*ticket["files"], *(widened["path"] for widened in ticket["widened"])}
        if ticket["kind"] == "proposal":
            if not ticket["finding_ids"]:
                raise LedgerError(f"{ticket['id']}: findings cannot be read")
            for finding in self.named_held(ticket["finding_ids"]):
                files.add(finding.path)
                moved = file_rename(finding.kind, finding.path, finding.after)
                pair = approved_rename(finding.kind, finding.before, finding.after)
                if isinstance(moved, FileRename):
                    files.add(moved.new)
                    name = moved.old_stem
                elif finding.kind == "restructure" and finding.path.endswith(".rs"):
                    path = PurePosixPath(finding.path)
                    name = path.parent.name if path.name == "mod.rs" else path.stem
                elif isinstance(pair, ApprovedRename):
                    name = pair.old
                else:
                    continue
                for commit in commits:
                    files.update(self.files_naming(commit, name, ("*.rs", "*.md")))
                    if isinstance(moved, FileRename) or finding.kind == "restructure":
                        files.update(self.files_naming(commit, "path"))
        if not files:
            raise LedgerError(f"{ticket['id']}: empty pipeline footprint")
        return frozenset(files)

    def _check_ticket_pair(self, predecessor: TicketRecord, successor: TicketRecord) -> None:
        """Reprove disjointness from fresh rows and resolved tips; any uncertainty refuses the transaction."""
        try:
            if predecessor["state"] not in ("checkpointed", "merged") or not predecessor["checkpoint"]:
                raise LedgerError("predecessor has no waiting or merged checkpoint")
            style_branch, style_tip = self._style_branch_tip()
            live = {successor["branch"]: _git(self.repo, "rev-parse", "--verify", f"{successor['branch']}^{{commit}}")}
            if style_branch:
                live[style_branch] = style_tip
            commits = {self.synced_commit(), *live.values()}
            for ticket in (predecessor, successor):
                if not ticket["base"] or (ticket["state"] == "checkpointed" and not ticket["checkpoint"]):
                    raise LedgerError(f"{ticket['id']}: footprint has no base or checkpoint")
                commits.add(ticket["base"])
                if ticket["checkpoint"]:
                    commits.add(ticket["checkpoint"])
            trees = frozenset(commits)
            crates = tuple(sorted({crate for commit in trees for crate in self._tip(commit).crate_dirs}, key=len, reverse=True))
            footprints: list[frozenset[str]] = []
            for ticket in (predecessor, successor):
                end = ticket["checkpoint"] or live[successor["branch"]]
                # Neither inherited predecessor work nor imported style work belongs to this ticket.
                revisions = (end, f"^{ticket['base']}", *((f"^{style_tip}",) if style_tip else ()))
                paths = _git_bytes(self.repo, "log", "--no-merges", "--name-only", "--no-renames", "-z",
                                   "--format=", *revisions, "--").stdout
                if paths and not paths.endswith(b"\0"):
                    raise LedgerError("unreadable changed paths")
                actual = frozenset(os.fsdecode(path) for path in paths.split(b"\0") if path)
                footprints.append(self._pipeline_footprint(ticket, trees) | actual)
            waiting, selected = footprints
            overlap = ((waiting & protected_ticket_files(selected, crates))
                       | (selected & protected_ticket_files(waiting, crates)))
            if overlap:
                raise LedgerError("overlap: " + ", ".join(sorted(overlap)))
            if any(_git(self.repo, "rev-parse", "--verify", f"{branch}^{{commit}}") != commit
                   for branch, commit in live.items()):
                raise LedgerError("branch moved during footprint check")
        except GitReadFailed:
            raise
        except (OSError, ValueError, subprocess.CalledProcessError) as error:
            raise LedgerError(f"{successor['id']}: waiting for merge: {predecessor['id']}; footprint check: {error}") from error

    def applied_check(self, finding: HeldFinding, commit: str) -> AppliedCheck:
        """Whether the finding's change is in the code at commit."""
        rename = binding_rename(finding.kind, finding.before, finding.after, finding.line, finding.text)
        if isinstance(rename, BindingRename) and isinstance(finding.build_ticket, BuildTicket):
            key = (finding.build_ticket.ticket, finding.path, commit)
            if key in self._binding_proofs:
                return self._binding_proofs[key].checks.get(rename, NoAppliedCheck("rename"))
            ticket = self._ticket(finding.build_ticket.ticket)
            if ticket["base"]:
                before, after = self._file_at(ticket["base"], finding.path), self._file_at(commit, finding.path)
                if not isinstance(before, bytes) or not isinstance(after, bytes):
                    self._binding_proofs[key] = BindingProof({}, {}, False, {})
                    return NoAppliedCheck("rename")
                # Read ticket membership rather than state: the same proof serves findings already marked built.
                renames = self._binding_renames(ticket, finding.path, commit)
                self._binding_proofs[key] = binding_rename_check(
                    finding.path, before.decode(errors="replace"), after.decode(errors="replace"), renames)
                return self._binding_proofs[key].checks.get(rename, NoAppliedCheck("rename"))
        return applied_check(finding.kind, finding.before, finding.after, finding.path,
                             lambda path: self._file_at(commit, path))

    def _file_at(self, commit: str, path: str) -> bytes | Folder | MissingFile:
        """What is at path in commit: a file's bytes, a folder or nothing. A git error raises, so it never reads as nothing."""
        if isinstance(self.git_cache, GitReadCache):
            return self.git_cache.file(commit, path)
        entry = _git(self.repo, "ls-tree", commit, "--", path)
        if not entry:
            return MissingFile()
        # An entry reads `<mode> <type> <object>\t<path>`.
        if entry.split()[1] == "tree":
            return Folder()
        shown = _git_bytes(self.repo, "show", f"{commit}:{path}")
        return shown.stdout

    def _ticket_arrived(self, ticket: TicketRecord, target: TicketState, commit: str = "", branch: str = "",
                        reservation: tuple[str, str] = ("", "")) -> bool:
        """Check state, repeats and predecessor order from ledger rows, before any Git proof."""
        state = ticket["state"]
        if state == target:
            if target == "assigned" and ticket["branch"] != branch:
                raise LedgerError(f"{ticket['id']}: assigned to branch {ticket['branch']}")
            if target == "checkpointed" and reservation[0]:
                held = self._ticket_reservation(ticket["id"])
                if held[0] and held != reservation:
                    raise LedgerError(f"{ticket['id']}: checkpointed holding the reservation of phase {held[1]} in {held[0]}")
                # A repeat stores the reservation an earlier checkpoint did not name.
                return ticket["checkpoint"] == commit and held == reservation
            if target == "checkpointed":
                return ticket["checkpoint"] == commit
            return True
        predecessor = {"assigned": "queued", "checkpointed": "assigned", "merged": "checkpointed"}
        if (target == "dropped" and state in ("merged", "dropped")) or (target != "dropped" and predecessor.get(target) != state):
            raise LedgerError(f"{ticket['id']}: cannot become {target} from {state}")
        if target == "assigned":
            active = self.ticket_open(ticket["unit"])
            if active and (len(active) != 1 or active[0]["state"] != "checkpointed"):
                raise LedgerError(f"{ticket['id']}: unit {ticket['unit']} already has {active[0]['id']} in state {active[0]['state']}")
        if target == "merged":
            rows = cast(list[tuple[str]], self._connection.execute(
                "SELECT p.predecessor FROM ticket_predecessors p JOIN tickets t ON t.id=p.predecessor "
                + "WHERE p.ticket=? AND t.state<>'merged'", (ticket["id"],),
            ).fetchall())
            if rows:
                raise LedgerError(f"{ticket['id']}: waiting for predecessor {rows[0][0]} to merge")
        return False

    def _ticket_transition(self, id: str, target: TicketState, commit: str = "", branch: str = "",
                           reservation: tuple[str, str] = ("", ""), *,
                           required_owner: BatchOwner | UnrestrictedTicketOwner = _UNRESTRICTED_TICKET_OWNER,
                           ) -> tuple[FindingNotBuilt, ...]:
        """Move the ticket to target. A proposal merge returns each of its findings that stays filed.

        A checkpoint stores the reservation's run folder and phase in the same write as its state.

        The git work runs before the write lock; if the rows it read change before the lock, the move starts again.
        """
        assignment_tip = ""

        def arrived(ticket: TicketRecord) -> bool:
            """Whether the ticket is already at target; refuses a move its state does not allow."""
            nonlocal assignment_tip
            if isinstance(required_owner, BatchOwner):
                owner = self.ticket_batch_owner(id)
                if owner != required_owner:
                    raise TicketBatchMismatch(id, owner)
            if self._ticket_arrived(ticket, target, commit, branch, reservation):
                return True
            if target == "checkpointed":
                if ticket["state"] == "checkpointed" and ticket["checkpoint"] != commit:
                    if not self._is_ancestor(ticket["checkpoint"], commit):
                        raise LedgerError(f"{id}: checkpointed at {ticket['checkpoint']}")
                    branch_tip = _git(self.repo, "rev-parse", "--verify", f"{ticket['branch']}^{{commit}}")
                    if not self._is_ancestor(commit, branch_tip):
                        raise LedgerError(f"{id}: checkpoint {commit[:7]} is not on {ticket['branch']}")
                missing = self.unjudged_ticket_cells(id, commit, lambda path: not isinstance(self._file_at(commit, path), MissingFile))
                if missing:
                    raise LedgerError(f"{id}: unjudged cells: " + ", ".join(f"{cell.rule} {cell.path}" for cell in missing))
            if target == "assigned":
                try:
                    _ = _git(self.repo, "check-ref-format", branch)
                except GitReadFailed:
                    raise
                except LedgerError as error:
                    raise LedgerError(f"{id}: cannot assign: invalid branch {branch}") from error
                active = self.ticket_open(ticket["unit"])
                if active:
                    try:
                        assignment_tip = _git(self.repo, "rev-parse", "--verify", f"{branch}^{{commit}}")
                    except GitReadFailed:
                        raise
                    except LedgerError as error:
                        raise LedgerError(f"{id}: cannot resolve branch {branch}; waiting for merge: "
                                          + f"{active[0]['id']} checkpointed at {active[0]['checkpoint']}; assignment refused") from error
                    if assignment_tip != active[0]["checkpoint"] or not assignment_tip:
                        raise LedgerError(f"{id}: branch must start at waiting checkpoint {active[0]['checkpoint']}")
            return False

        def attempt() -> tuple[FindingNotBuilt, ...]:
            ticket = self._ticket(id)
            if arrived(ticket):
                return ()
            if target == "merged":
                style_branch, branch_tip = self._style_branch_tip()
                if not branch_tip or not self._is_ancestor(ticket["checkpoint"], branch_tip):
                    raise LedgerError(f"{id}: checkpoint {ticket['checkpoint'][:7]} is not on {style_branch or 'the style branch'}")
            checkpoint = commit if target == "checkpointed" else ticket["checkpoint"]
            restamps = self.checkpoint_restamps(ticket, checkpoint) if target in ("checkpointed", "merged") else NoRestamps()
            building = target == "merged" and ticket["kind"] == "proposal"
            findings = self.filed_build_findings(id) if building else ()
            checks = tuple((finding.finding_id, self.applied_check(finding, checkpoint)) for finding in findings)
            carry = self.binding_carry_forward(ticket, checkpoint) if building else BindingCarryForward()
            not_built: list[FindingNotBuilt] = []
            with self._connection:
                _ = self._connection.execute("BEGIN IMMEDIATE")
                current = self._ticket(id)
                if arrived(current):
                    return ()
                # A removal recorded since the read moves the boundary that decides which verdicts carry.
                if (current != ticket or (building and self.filed_build_findings(id) != findings)
                        or (building and self.binding_carry_forward(ticket, checkpoint) != carry)
                        or (isinstance(restamps, CheckpointRestamps)
                            and (self.latest_ticket_judgments(id) != restamps.latest
                                 or self._removals(restamps.latest) != restamps.removals))):
                    raise ChangedBeforeLock(f"{id}: the ticket changed while it became {target}; rerun")
                for verdict in (*(restamps.carried if isinstance(restamps, CheckpointRestamps) else ()), *carry.verdicts):
                    _ = self._connection.execute(
                        'INSERT INTO judgments(rule,rule_blob,input_scope,input_fingerprint,path,"commit",verdict,verdict_detail,ticket,worker,recorded_at,declared_scope,read_files,generation) '
                        + 'SELECT rule,rule_blob,?,?,path,?,verdict,verdict_detail,ticket,worker,?,declared_scope,read_files,generation FROM active_judgments WHERE id=? '
                        + 'ON CONFLICT(rule,path,"commit",rule_blob,input_scope,input_fingerprint,ticket,worker) '
                        + 'DO UPDATE SET verdict=excluded.verdict,verdict_detail=excluded.verdict_detail,'
                        + 'recorded_at=excluded.recorded_at,declared_scope=excluded.declared_scope,read_files=excluded.read_files,generation=excluded.generation '
                        + 'WHERE judgments.generation!=excluded.generation',
                        (verdict.scope, verdict.fingerprint, checkpoint, datetime.now(timezone.utc).isoformat(), verdict.judgment_id),
                    )
                for moved in carry.findings:
                    _ = self._connection.execute(
                        "UPDATE held_findings SET path=?,line=?,text=?,followed_from=?,followed_from_line=?,followed_at=?,updated_at=? "
                        + "WHERE finding_id=?",
                        (moved.finding.path, moved.line, moved.text, moved.finding.path, moved.finding.line,
                         checkpoint, _now(), moved.finding.finding_id),
                    )
                if building:
                    now = _now()
                    for finding_id, check in checks:
                        if isinstance(check, AppliedInCode):
                            # A person's reading was of the old build, so a new build clears it.
                            _ = self._connection.execute(
                                "UPDATE held_findings SET state='built',person_read_by='',built_at=CASE WHEN built_at='' THEN ? ELSE built_at END,updated_at=? WHERE finding_id=?", (now, now, finding_id),
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
                if target == "assigned":
                    active = self.ticket_open(ticket["unit"])
                    if active:
                        _ = self._connection.execute("INSERT INTO ticket_predecessors VALUES (?,?)", (id, active[0]["id"]))
                        _ = self._connection.execute("UPDATE tickets SET base=? WHERE id=?", (assignment_tip, id))
                # Check before merge removes the successor from the open set, after any carried finding moves.
                if target == "merged":
                    self._check_ticket_pipelines(ticket["unit"])
                _ = self._connection.execute(
                    "UPDATE tickets SET state=?,branch=?,checkpoint=?,updated_at=? WHERE id=?",
                    (target, branch if target == "assigned" else ticket["branch"],
                     commit if target == "checkpointed" else ticket["checkpoint"], _now(), id),
                )
                self._check_ticket_pipelines(ticket["unit"])
                if target == "checkpointed" and reservation[0]:
                    _ = self._connection.execute("UPDATE tickets SET reservation_session_dir=?,reservation_phase=? WHERE id=?",
                                                 (*reservation, id))
            return tuple(not_built)

        return retry_on_change(attempt)

    @_ticket_operation("assign")
    def ticket_assign(self, id: str, branch: str) -> None:
        branch = "refs/heads/" + branch.removeprefix("refs/heads/")
        _ = self._ticket_transition(id, "assigned", branch=branch)

    @_ticket_operation("base")
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
            self._check_ticket_pipelines(ticket["unit"])
            return base

    @_ticket_operation("checkpoint")
    def ticket_checkpoint(self, id: str, commit: str, session_dir: str = "", phase: str = "") -> None:
        """Mark the ticket checkpointed; a unit run folder and phase name the reservation record its merge releases."""
        ticket = self._ticket(id)
        reservation = (session_dir, phase)
        # A reference may need resolving, but the stored hash and reservation prove an exact repeat without Git.
        previous = ticket["checkpoint"] if ticket["state"] == "checkpointed" else commit
        if self._ticket_arrived(ticket, "checkpointed", previous, reservation=reservation) and commit == previous:
            return
        if not ticket["checkpoint"] or commit != ticket["checkpoint"]:
            commit = _git(self.repo, "rev-parse", "--verify", f"{commit}^{{commit}}")
        _ = self._ticket_transition(id, "checkpointed", commit, reservation=reservation)

    def binding_carry_forward(self, ticket: TicketRecord, checkpoint: str) -> BindingCarryForward:
        """Carry types inputs only when the ticket's whole path change is a proven binding rename."""
        if not ticket["base"]:
            return BindingCarryForward()
        renames: dict[str, list[BindingRename]] = {}
        for finding in self.named_held(ticket["finding_ids"]):
            pair = binding_rename(finding.kind, finding.before, finding.after, finding.line, finding.text)
            if isinstance(pair, BindingRename):
                renames.setdefault(finding.path, []).append(pair)
        if not renames:
            return BindingCarryForward()
        base, tip = self._tip(ticket["base"]), self._tip(checkpoint)
        definitions = {rule.rule_id: rule for rule in rule_catalog.load_rules(self.repo) if rule.family == "types"}
        if not definitions:
            return BindingCarryForward()
        open_types = tuple(f for f in self.held_findings(AllHeldFindings())
                           if isinstance(f.state, Unfiled | Filed) and base_rule(f.rule) in definitions)
        verdicts: list[CarriedVerdict] = []
        moves: list[CarriedTypesFinding] = []
        for path in ticket["files"]:
            if path not in base.blobs or path not in tip.blobs or base.blobs[path] == tip.blobs[path]:
                continue
            pairs = self._binding_renames(ticket, path, checkpoint) if path in renames else ()
            if not pairs:
                continue
            key = (ticket["id"], path, checkpoint)
            if key not in self._binding_proofs:
                before, after = self._file_at(base.commit, path), self._file_at(checkpoint, path)
                if not isinstance(before, bytes) or not isinstance(after, bytes):
                    continue
                self._binding_proofs[key] = binding_rename_check(path, before.decode(errors="replace"),
                                                                after.decode(errors="replace"), pairs)
            proof = self._binding_proofs[key]
            if not proof.explained or not any(isinstance(check, AppliedInCode) for check in proof.checks.values()):
                continue
            rows = cast(list[tuple[int, str, str, str, str, Verdict, str, str, str, str]], self._connection.execute(
                'SELECT j.id,j.rule,j.rule_blob,j.input_fingerprint,j."commit",j.verdict,j.input_scope,j.declared_scope,j.read_files,j.ticket FROM active_judgments j WHERE j.path=? AND j.id=(SELECT newest.id FROM active_judgments newest '
                + 'WHERE newest.rule=j.rule AND newest.path=j.path ORDER BY newest.recorded_at DESC,newest.id DESC LIMIT 1)',
                (path,),
            ).fetchall())
            blocked: set[str] = set()
            for judgment_id, rule, blob, fingerprint, commit, verdict, scope, declared, reads, holder in rows:
                definition = definitions.get(base_rule(rule))
                if definition is None:
                    continue
                judgment = Judgment(blob, fingerprint, commit, verdict, CellInputScope(scope),
                                    DeclaredInputScope(declared), _read_files(reads), holder)
                judgments = (judgment, *self._judgments(rule, path))
                try:
                    if (not self._is_ancestor(judgment.commit, base.commit)
                            or judgment not in self._rows_after_removals(judgments, base.commit)):
                        blocked.add(rule)
                        continue
                    judged = self._tip(judgment.commit)
                    scope = _comparison_scope(judgment.declared_scope, _cell_scope(definition, path, tip))
                    old_inputs = (_read_inputs(judged, path, judgment.reads) if isinstance(judgment.reads, ReadFiles)
                                  else self._scope_inputs(judged, scope, path))
                    new_inputs = (_read_inputs(tip, path, judgment.reads) if isinstance(judgment.reads, ReadFiles)
                                  else self._scope_inputs(tip, scope, path))
                    other_old = {name: entry for name, entry in old_inputs.items() if name != path}
                    other_new = {name: entry for name, entry in new_inputs.items() if name != path}
                    if (judgment.verdict == "removed" or not rule_catalog.judged_text_unchanged(judgment.rule_blob, definition)
                            or judged.blobs.get(path) != base.blobs[path] or other_old != other_new
                            or not self._judgment_inputs_match(judgment, judged, scope, path, "types")):
                        blocked.add(rule)
                        continue
                    fingerprint = (_read_fingerprint(tip, path, judgment.reads) if isinstance(judgment.reads, ReadFiles)
                                   else self._fingerprint(tip, scope, path))
                except GitReadFailed:
                    raise
                except LedgerError:
                    blocked.add(rule)
                    continue
                verdicts.append(CarriedVerdict(judgment_id, scope, fingerprint))
            after = self._file_at(checkpoint, path)
            if not isinstance(after, bytes):
                continue
            lines = after.decode(errors="replace").splitlines()
            for finding in open_types:
                if finding.path != path or finding.rule in blocked or rule_changed_since_judged(finding, definitions):
                    continue
                judged = self.judged_commit(finding)
                read = finding.follow.commit if isinstance(finding.follow, FollowedDeclaration) else (
                    judged.commit if isinstance(judged, JudgedCommit) else "")
                if not read or self._tip(read).blobs.get(path) != base.blobs[path]:
                    continue
                line = proof.line_map.get(finding.line, 0)
                if 1 <= line <= len(lines):
                    moves.append(CarriedTypesFinding(finding, line, lines[line - 1]))
        return BindingCarryForward(tuple(verdicts), tuple(moves))

    def latest_ticket_judgments(self, ticket_id: str) -> tuple[TicketJudgment, ...]:
        rows = cast(list[tuple[int, str, str, str, Verdict, str, str]], self._connection.execute(
            'SELECT j.id,j.rule,j.path,j."commit",j.verdict,j.declared_scope,j.read_files FROM active_judgments j WHERE j.ticket=? '
            + 'AND j.id=(SELECT latest.id FROM active_judgments latest WHERE latest.ticket=j.ticket '
            + 'AND latest.rule=j.rule AND latest.path=j.path ORDER BY latest.recorded_at DESC,latest.id DESC LIMIT 1)',
            (ticket_id,),
        ).fetchall())
        return tuple(TicketJudgment(judgment_id, rule, path, commit, verdict, DeclaredInputScope(declared), _read_files(reads))
                     for judgment_id, rule, path, commit, verdict, declared, reads in rows)

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
        removals = self._removals(latest)
        self._catalog = self._load_rules()
        tip = self._tip(checkpoint)
        # Commits inherited at the start or merged from another branch are not ours.
        own_commits = set(_git(self.repo, "rev-list", "--first-parent", checkpoint,
                               "^" + ticket["base"]).splitlines())
        carried: list[CarriedVerdict] = []
        for judgment in latest:
            # A removed cell keeps the commit that deleted its file; it has no inputs to carry.
            if judgment.verdict == "removed":
                continue
            try:
                definition = self._component_rule(judgment.rule)
                if definition.family != ticket["family"] or judgment.commit == checkpoint:
                    continue
                if not self._is_ancestor(judgment.commit, checkpoint):
                    continue
                # A removal in the checkpoint by any ticket ends the cell, so a verdict before it does not carry.
                after = self._rows_after_removals(self._judgments(judgment.rule, judgment.path), checkpoint)
                if judgment.commit not in {row.commit for row in after if row.verdict != "removed"}:
                    continue
                paths = (judgment.path, *judgment.reads.paths) if isinstance(judgment.reads, ReadFiles) else (judgment.path,)
                changed_commits = set(_git(self.repo, "rev-list", "--full-history", checkpoint,
                                          "^" + judgment.commit, "--", *paths).splitlines())
                if not changed_commits <= own_commits:
                    continue
                scope = _comparison_scope(judgment.declared_scope, _cell_scope(definition, judgment.path, tip))
                fingerprint = (_read_fingerprint(tip, judgment.path, judgment.reads) if isinstance(judgment.reads, ReadFiles)
                               else self._fingerprint(tip, scope, judgment.path))
            except GitReadFailed:
                raise
            except LedgerError:
                continue
            carried.append(CarriedVerdict(judgment.judgment_id, scope, fingerprint))
        return CheckpointRestamps(latest, removals, tuple(carried))

    def _removals(self, judgments: tuple[TicketJudgment, ...]) -> frozenset[tuple[int, str, str, str, str]]:
        """The removed rows of any ticket on the cells of these judgments, as (id, ticket, rule, path, commit).

        A yield deletes rows and SQLite can give a deleted id to the next row, so the id alone does not name a removal.
        """
        cells = {(judgment.rule, judgment.path) for judgment in judgments}
        rows = cast(list[tuple[int, str, str, str, str]], self._connection.execute(
            'SELECT id,ticket,rule,path,"commit" FROM active_judgments WHERE verdict=\'removed\'').fetchall())
        return frozenset(row for row in rows if (row[2], row[3]) in cells)

    @_ticket_operation("merge")
    def ticket_merge(self, id: str) -> tuple[FindingNotBuilt, ...]:
        """Mark the ticket merged. A proposal finding becomes built only when its change is in the checkpoint.

        Each run, also on a merged ticket, returns the findings that wait for a person to mark them built.
        """
        ticket = self._ticket(id)
        if self._ticket_arrived(ticket, "merged"):
            # Merge clears the build ticket of a rename that is not applied, so a finding that keeps it has no check.
            return tuple(FindingNotBuilt(finding.finding_id, NoAppliedCheck(finding.kind)) for finding in self.filed_build_findings(id))
        return self._ticket_transition(id, "merged")

    def _style_branch_tip(self) -> tuple[str, str]:
        """The style branch the ledger syncs with and the full id of its tip; both empty when it names none."""
        rows = cast(list[tuple[str]], self._connection.execute("SELECT style_branch FROM sync WHERE id=1").fetchall())
        branch = rows[0][0] if rows else ""
        return branch, _git(self.repo, "rev-parse", "--verify", f"{branch}^{{commit}}") if branch else ""

    def _ticket_reservation(self, id: str) -> tuple[str, str]:
        """The run folder and phase of the reservation record the ticket's checkpoint kept."""
        return cast(tuple[str, str], self._connection.execute(
            "SELECT reservation_session_dir,reservation_phase FROM tickets WHERE id=?", (id,)).fetchone())

    @_ticket_operation("release")
    def release_held_reservation(self, id: str) -> HeldReservationRelease | None:
        """Release the berth reservation a final ticket's checkpoint kept; None when it keeps no record to release.

        The style branch tip is the merged commit: a release berth made at a later commit of the unit's branch counts
        when that tip contains it. A confirmed release, or a record that holds no reservation, clears the run folder
        and phase, so a later merge makes no berth call; a release that fails keeps them for the next merge or reconcile.
        """
        rows = cast(list[tuple[str, str, str]], self._connection.execute(
            "SELECT state,reservation_session_dir,reservation_phase FROM tickets WHERE id=?", (id,)).fetchall())
        if not rows:
            raise LedgerError(f"unknown ticket {id}")
        state, session_dir, phase = rows[0]
        if not session_dir:
            return None
        if state not in FINAL_TICKET_STATES:
            raise LedgerError(f"{id}: its reservation is released at ticket merge, not in state {state}")
        scripts = Path(__file__).resolve().parents[1]
        merged = self._style_branch_tip()[1]
        result = subprocess.run([str(scripts / "lib/py"), str(scripts / "direct/phase_reservation.py"), "release-held",
                                 "--session-dir", session_dir, "--phase", phase, "--ticket", id,
                                 "--merged", merged],
                                capture_output=True, text=True, check=False)
        answer = " ".join((result.stdout if result.returncode == 0 else result.stderr).split())
        if result.returncode == 0:
            with self._connection:
                _ = self._connection.execute("UPDATE tickets SET reservation_session_dir='',reservation_phase='' WHERE id=?", (id,))
        return HeldReservationRelease(f"ticket {id}: {answer or f'phase_reservation.py exited {result.returncode}'}",
                                      result.returncode != 0)

    def rename_unit(self, old: str, new: str) -> UnitRenamed:
        family = rule_catalog.style_family(old)
        if isinstance(family, rule_catalog.NotStyleUnit) or family != rule_catalog.style_family(new):
            raise LedgerError(f"{old} -> {new}: units must name the same style family")
        if old == new:
            return UnitRenamed(0, 0)
        with self._connection:
            _ = self._connection.execute("BEGIN IMMEDIATE")
            source = cast(tuple[int, int], self._connection.execute(
                "SELECT (SELECT count(*) FROM tickets WHERE unit=? AND state IN ('queued','assigned','checkpointed')), "
                + "(SELECT count(*) FROM sync_waits WHERE unit=?)", (old, old),
            ).fetchone())
            if not any(source):
                return UnitRenamed(0, 0)
            target = cast(tuple[int, int], self._connection.execute(
                "SELECT EXISTS(SELECT 1 FROM tickets WHERE unit=? AND state IN ('queued','assigned','checkpointed')), "
                + "EXISTS(SELECT 1 FROM sync_waits WHERE unit=?)", (new, new),
            ).fetchone())
            if any(target):
                raise LedgerError(f"{new}: target unit already owns a current ticket or sync wait")
            _ = self._connection.execute(
                "UPDATE tickets SET unit=? WHERE unit=? AND state IN ('queued','assigned','checkpointed')", (new, old),
            )
            _ = self._connection.execute("UPDATE sync_waits SET unit=? WHERE unit=?", (new, old))
            return UnitRenamed(*source)

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
        return ApprovedProposal(Path(ticket["proposal"]), ticket["unit"], tuple(ticket["finding_ids"]),
                                tuple(widened["path"] for widened in ticket["widened"]), ticket["base"],
                                tuple(issue for issue in self.ticket_proposals(id) if str(issue) != ticket["proposal"]))

    def ticket_crate(self, id: str) -> str:
        """The crate whose work the ticket builds or judges."""
        return self._ticket(id)["crate"]

    def ticket_proposals(self, id: str) -> tuple[Path, ...]:
        """Every decision a ticket builds, including the primary decision of a legacy ticket."""
        rows = cast(list[tuple[str]], self._connection.execute(
            "SELECT proposal FROM ticket_proposals WHERE ticket=? ORDER BY proposal", (id,),
        ).fetchall())
        if rows:
            return tuple(Path(row[0]) for row in rows)
        proposal = self._ticket(id)["proposal"]
        return (Path(proposal),) if proposal else ()

    def proposal_tickets(self) -> tuple[TicketRecord, ...]:
        rows = cast(list[tuple[str]], self._connection.execute(
            "SELECT id FROM tickets WHERE kind='proposal' ORDER BY created_at,id",
        ).fetchall())
        return tuple(self._ticket(row[0]) for row in rows)

    def unsynced_merges(self, proposal: Path) -> tuple[TicketRecord, ...]:
        """The merged tickets of the proposal whose checkpoint the synced commit does not hold."""
        commit = self.synced_commit()
        return tuple(ticket for ticket in self.proposal_tickets()
                     if ticket["state"] == "merged" and proposal in self.ticket_proposals(ticket["id"])
                     and not self._is_ancestor(ticket["checkpoint"], commit))

    @_ticket_operation("drop")
    def ticket_drop(self, id: str) -> None:
        _ = self._ticket_transition(id, "dropped")

    @_ticket_operation("recover")
    def ticket_drop_batch(self, id: str, owner: BatchOwner) -> None:
        """Release only this intent's claim, rechecking ownership under the transition write lock."""
        exists = cast(list[tuple[str]], self._connection.execute("SELECT id FROM tickets WHERE id=?", (id,)).fetchall())
        if not exists:
            return
        actual = self.ticket_batch_owner(id)
        if actual != owner:
            raise TicketBatchMismatch(id, actual)
        if self._ticket(id)["state"] in FINAL_TICKET_STATES:
            return
        _ = self._ticket_transition(id, "dropped", required_owner=owner)

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
            # A pending row merges too: ancestry on the pushed branch point is the evidence, not the notice.
            if state not in ("pending", "sent"):
                raise LedgerError(f"{checkpoint}: cannot become merged from {state}")
            _ = self._connection.execute("UPDATE handoffs SET state='merged',merge_commit=?,updated_at=? WHERE checkpoint=?", (actual_merge, _now(), checkpoint))

    def pending_handoffs(self) -> tuple[dict[str, str], ...]:
        rows = cast(list[tuple[str, str, str, str, str, str]], self._connection.execute(
            "SELECT checkpoint,state,merge_commit,sent_to,created_at,updated_at FROM handoffs WHERE state!='merged' ORDER BY created_at,checkpoint",
        ).fetchall())
        return tuple(dict(zip(("checkpoint", "state", "merge_commit", "sent_to", "created_at", "updated_at"), row)) for row in rows)

    def tip_seen(self, tip: str) -> ProcessedTipReason | UnseenTip:
        tip = _git(self.repo, "rev-parse", "--verify", f"{tip}^{{commit}}")
        rows = cast(list[tuple[ProcessedTipReason]], self._connection.execute(
            "SELECT reason FROM processed_tips WHERE tip=?", (tip,)).fetchall())
        return rows[0][0] if rows else UnseenTip()

    def tip_done(self, tip: str, reason: ProcessedTipReason = "linted") -> None:
        tip = _git(self.repo, "rev-parse", "--verify", f"{tip}^{{commit}}")
        with self._connection:
            _ = self._connection.execute("INSERT INTO processed_tips(tip,processed_at,reason) VALUES (?,?,?) ON CONFLICT(tip) DO NOTHING",
                                         (tip, _now(), reason))

    def hold(
        self, rule: str, path: str, line: int, text: str, kind: FindingKind, before: str,
        after: str, rule_blob: str, ticket: str,
    ) -> HeldFinding:
        self._catalog = self._load_rules()
        definition = self._component_rule(rule)
        rule_blob = self._check_pin(ticket, rule, rule_blob)
        path = self._path(path)
        if isinstance(definition.reach, MemberReach) and not _under(path, definition.reach.crate_dir.as_posix()):
            raise LedgerError(f"{rule}: {path} is outside rule reach")
        if kind not in ("rename", "type", "restructure") or line < 1:
            raise LedgerError("held finding requires a valid kind and positive line")
        before_lines, after_lines = before.splitlines(), after.splitlines()
        if before_lines and len(after_lines) > len(before_lines) and any(
            after_lines[start:start + len(before_lines)] == before_lines
            for start in range(len(after_lines) - len(before_lines) + 1)
        ):
            raise LedgerError(f"{rule}: after text must replace the whole before span, not keep it with added lines")
        change = (kind, before, after)
        site = (rule, path, line, text, rule_blob)
        # The first id names the site. When a different change holds it, the change gets the second id, which also names
        # the change, so a call never returns a row that holds a different change.
        finding_ids = (_finding_id(site), _finding_id((*site, *change)))
        with self._connection:
            _ = self._connection.execute("BEGIN IMMEDIATE")
            _ = self._check_pin(ticket, rule, rule_blob)
            take_over = ("UPDATE held_findings SET ticket=?,updated_at=? WHERE finding_id=? AND state IN ('unfiled','filed') "
                         + "AND ticket!=?")
            while True:
                rows = cast(list[tuple[str, str, str, str, int]], self._connection.execute(
                    "SELECT h.finding_id,h.kind,h.before,h.after,EXISTS "
                    + "(SELECT 1 FROM rule_body_withdrawals w WHERE w.finding_id=h.finding_id AND h.state='withdrawn') "
                    + "FROM held_findings h WHERE h.finding_id IN (?,?)", finding_ids,
                ).fetchall())
                changes = {row[0]: row[1:4] for row in rows}
                same = [finding_id for finding_id in finding_ids if changes.get(finding_id) == change]
                if not same:
                    break
                if any(row[0] == same[0] and row[4] for row in rows):
                    # Preserve withdrawn history when a body returns; repeats follow the same replacement chain.
                    finding_ids = (_finding_id((*site, *change, same[0])),
                                   _finding_id((*site, *change, same[0], "replacement")))
                    continue
                _ = self._connection.execute(take_over, (ticket, _now(), same[0], ticket))
                return self._held(same[0])
            # A followed finding keeps the id of its first site. A ticket that reads its new file holds the same change
            # again, so that change returns the followed finding, the one with the nearest line.
            followed = cast(list[tuple[str, int]], self._connection.execute(
                "SELECT finding_id,line FROM held_findings WHERE rule=? AND path=? AND text=? AND kind=? AND before=? AND after=? "
                + "AND rule_blob=? AND followed_at!='' AND state IN ('unfiled','filed')", (rule, path, text, kind, before, after, rule_blob),
            ).fetchall())
            if followed:
                nearest = min(followed, key=lambda row: (abs(row[1] - line), row[0]))[0]
                _ = self._connection.execute(take_over, (ticket, _now(), nearest, ticket))
                return self._held(nearest)
            free = [finding_id for finding_id in finding_ids if finding_id not in changes]
            if not free:
                raise LedgerError(f"{rule}: other changes hold both finding ids of {path}:{line}")
            now = _now()
            _ = self._connection.execute(
                "INSERT INTO held_findings(finding_id,rule,path,line,text,kind,before,after,rule_blob,ticket,state,"
                + "proposal,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,'unfiled','',?,?)",
                (free[0], rule, path, line, text, kind, before, after, rule_blob, ticket, now, now),
            )
            # Every return reads its row before the commit, so a write after the commit cannot change what it returns.
            return self._held(free[0])

    def _held(self, finding_id: str) -> HeldFinding:
        rows = cast(list[tuple[str, str, str, int, str, FindingKind, str, str, str, str, HeldStateName, str, str, int, str, str, str, str, int, str, str, str, str]], self._connection.execute(
            "SELECT finding_id,rule,path,line,text,kind,before,after,rule_blob,ticket,state,proposal,build_ticket,withdrawn_by,person_read_by,duplicate_of,unwaived_by,"
            + "followed_from,followed_from_line,followed_at,declaration_gone_at,waive_provenance,waived_by FROM held_findings WHERE finding_id=?", (finding_id,),
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
                changed = cast(list[tuple[str]], self._connection.execute(
                    "SELECT rule_blob FROM rule_body_withdrawals WHERE finding_id=?", (finding_id,),
                ).fetchall())
                state = Withdrawn(Path(row[11]) if row[11] else NoDecision(),
                                  RuleBodyChanged(changed[0][0]) if changed else
                                  DuplicateOf(row[15]) if row[15] else DeclarationGone(row[20]) if row[20]
                                  else ExceptionVerdict(row[13]) if row[11] else JudgedAgain(row[13]))
            case "unfiled":
                state = Unfiled()
        provenance = WaiveProvenance(row[21])
        actor: WaiveActor | NoRecordedWaiveActor = NoRecordedWaiveActor()
        if row[22]:
            if provenance == WaiveProvenance.UNIT:
                actor = WaiveByUnit(row[22])
            elif provenance == WaiveProvenance.PERSON:
                actor = WaiveByPerson(row[22])
            elif provenance == WaiveProvenance.LEDGER:
                actor = WaiveByLedger()
        return HeldFinding(row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7], row[8], row[9], state, BuildTicket(row[12]) if row[12] else NoBuildTicket(),
                           ReadByPerson(row[14]) if row[14] else NotReadByPerson(), WaiveUndoneBy(row[16]) if row[16] else NoWaiveUndo(),
                           FollowedDeclaration(row[17], row[18], row[19]) if row[19] else NotFollowed(), provenance, actor)

    def judged_commit(self, finding: HeldFinding) -> JudgedCommit | NoJudgedCommit:
        return self._judged_from(self._judging_ticket(finding.ticket))

    def judged_again_without(self, finding: HeldFinding, definition: Rule) -> SupersedingJudgment | NotJudgedAgain:
        """The judgment that read the finding's changed file again and did not hold it, as withdrawal reads it."""
        return self._judged_again_without(finding, definition, self._synced_tip())

    def _judging_ticket(self, ticket_id: str) -> JudgingTicket:
        return tuple(cast(list[tuple[str, str]], self._connection.execute(
            "SELECT checkpoint,base FROM tickets WHERE id=?", (ticket_id,),
        ).fetchall()))

    def _judged_from(self, judging: JudgingTicket) -> JudgedCommit | NoJudgedCommit:
        """The judging ticket's checkpoint, else its start, from its row as read; reads git and not the ledger."""
        for commit in (commit for commit in (judging[0] if judging else ()) if commit):
            if commit not in self._resolved_commits:
                resolved = _git_bytes(self.repo, "rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}", answers=GitExitAnswers(frozenset({0, 1})))
                self._resolved_commits[commit] = NoJudgedCommit() if resolved.returncode else JudgedCommit(resolved.stdout.decode().strip())
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

    def _cell_findings(self, rule: str, path: str, commit: str) -> tuple[HeldFinding, ...]:
        """Every finding whose site maps to this rule's cell at the commit, in any state."""
        definition = self._component_rule(rule)
        tip = self._tip(commit)
        rows = cast(list[tuple[str, str]], self._connection.execute(
            "SELECT finding_id,path FROM held_findings WHERE rule=? ORDER BY created_at,finding_id", (rule,),
        ).fetchall())
        return tuple(self._held(finding_id) for finding_id, site in rows
                     if _finding_cell(definition, site, tip) == path)

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

    def open_build_finding_ids(self) -> frozenset[str]:
        """Filed findings that a ticket which is not final holds."""
        rows = cast(list[tuple[str]], self._connection.execute(
            "SELECT h.finding_id FROM held_findings h JOIN tickets t ON t.id=h.build_ticket "
            + "WHERE h.state='filed' AND t.state NOT IN ('merged','dropped')",
        ).fetchall())
        return frozenset(row[0] for row in rows)

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
                    "UPDATE held_findings SET state='withdrawn',built_at='',duplicate_of=?,withdrawn_by=0,proposal=?,"
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

    def decline_findings(self, finding_ids: Iterable[str], words: str, actor: DeclineActor) -> dict[str, DeclineOutcome]:
        """Waive the findings, and with the words each judgment cell that no other finding holds.

        The catalog, the synced tip and each cell's decision come before the write lock; if the rows they
        rest on change before the lock, the decline starts again.
        """
        ids = tuple(dict.fromkeys(finding_ids))
        match cast(object, actor):
            case (WaiveByPerson() | WaiveByLedger()) as decline_actor:
                provenance, by = _waive_actor_record(decline_actor)
            case _:
                raise LedgerError("decline requires a person or the ledger")

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
            self._catalog = self._load_rules()
            definitions: dict[str, Rule | UnknownRule] = {}
            for rule in sorted({finding.rule for finding in findings}):
                try:
                    definitions[rule] = self._component_rule(rule)
                except GitReadFailed:
                    raise
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
                _ = self._connection.executemany("UPDATE held_findings SET state='waived',built_at='',waive_provenance=?,waived_by=?,updated_at=? WHERE finding_id=?",
                                                 [(provenance, by, now, finding.finding_id) for finding in findings])
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
        rows = cast(list[tuple[int, str, str, str, str, str, str, str, str, str, str]], self._connection.execute(
            'SELECT id,rule,path,"commit",ticket,worker,rule_blob,input_scope,input_fingerprint,declared_scope,read_files FROM active_judgments '
            + "WHERE rule=? AND path=? AND verdict='held' ORDER BY recorded_at DESC,id DESC LIMIT 1",
            (rule, path),
        ).fetchall())
        if not rows:
            return NoHeldJudgment()
        row = rows[0]
        newest = cast(tuple[int], self._connection.execute(
            "SELECT id FROM active_judgments WHERE rule=? AND path=? ORDER BY recorded_at DESC,id DESC LIMIT 1",
            (rule, path),
        ).fetchone())[0]
        inputs = Judgment(row[6], row[8], row[3], "held", CellInputScope(row[7]), DeclaredInputScope(row[9]), _read_files(row[10]))
        return NewestHeldJudgment(row[0], HeldJudgment(*row[1:7]), inputs, newest)

    def decline_outcome(self, held: NewestHeldJudgment | NoHeldJudgment, path: str, definition: Rule, tip: RepositoryTip,
                        members: list[HeldFinding], held_by_others: bool) -> DeclineOutcome:
        """What declining the members does to the cell's held judgment; it writes nothing."""
        if isinstance(held, NoHeldJudgment):
            return held
        try:
            unchanged = (self._judgment_inputs_match(held.inputs, tip, _cell_scope(definition, path, tip), path, definition.family)
                         and self._is_ancestor(held.inputs.commit, tip.commit))
        except GitReadFailed:
            raise
        except LedgerError:
            unchanged = False
        if (held.cell_newest_id != held.judgment_id or not rule_catalog.judged_text_unchanged(held.inputs.rule_blob, definition)
                or any(not rule_catalog.judged_text_unchanged(finding.rule_blob, definition) for finding in members) or not unchanged):
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

    def _withdrawal_candidates(self, definitions: Mapping[str, Rule]) -> tuple[tuple[HeldFinding, Rule], ...]:
        """Compare distinct open rule pins before hydrating changed bodies or candidates for newer judgments."""
        pins = cast(list[tuple[str, str, int]], self._connection.execute(
            "SELECT h.rule,h.rule_blob,MAX(h.state='unfiled' AND NOT EXISTS "
            + "(SELECT 1 FROM tickets t WHERE t.id=h.ticket AND t.state IN ('assigned','checkpointed')) "
            + "AND EXISTS (SELECT 1 FROM active_judgments j WHERE j.rule=h.rule AND j.ticket!=h.ticket "
            + "AND j.verdict!='removed' AND j.recorded_at>CASE WHEN h.followed_at!='' THEN h.updated_at ELSE h.created_at END)) "
            + "FROM held_findings h WHERE h.state IN ('unfiled','filed') GROUP BY h.rule,h.rule_blob"
        ).fetchall())
        candidates: list[tuple[HeldFinding, Rule]] = []
        for rule, blob, newer in pins:
            definition = definitions.get(base_rule(rule))
            if definition is None:
                continue
            changed = not rule_catalog.judged_text_unchanged(blob, definition)
            if not changed and not newer:
                continue
            ids = cast(list[tuple[str]], self._connection.execute(
                "SELECT h.finding_id FROM held_findings h WHERE h.rule=? AND h.rule_blob=? "
                + "AND h.state IN ('unfiled','filed') AND (? OR (h.state='unfiled' AND NOT EXISTS "
                + "(SELECT 1 FROM tickets t WHERE t.id=h.ticket AND t.state IN ('assigned','checkpointed')))) "
                + "ORDER BY h.created_at,h.finding_id", (rule, blob, changed),
            ).fetchall())
            candidates.extend((self._held(id), definition) for (id,) in ids)
        return tuple(candidates)

    def _judged_again_without(self, finding: HeldFinding, definition: Rule, tip: RepositoryTip) -> SupersedingJudgment | NotJudgedAgain:
        """The newest judgment at the cell under the finding's rule text, when another ticket recorded it after the
        finding was held (or followed), reading the cell's inputs at tip, which the holding ticket never judged there;
        for a followed finding whose holding ticket never judged its new cell, any such judgment of the inputs at tip.
        A ticket that holds the finding again takes it over, so that judgment read the changed file and did not hold
        it."""
        cell = _finding_cell(definition, finding.path, tip)
        created, followed, updated = cast(tuple[str, str, str], self._connection.execute(
            "SELECT created_at,followed_at,updated_at FROM held_findings WHERE finding_id=?", (finding.finding_id,)).fetchone())
        candidates = cast(list[tuple[int, Verdict, str, str, str, str, str, str, str]], self._connection.execute(
            'SELECT id,verdict,ticket,input_fingerprint,"commit",input_scope,declared_scope,read_files,rule_blob FROM active_judgments '
            + "WHERE rule=? AND path=? AND verdict!='removed' AND recorded_at>? ORDER BY recorded_at DESC,id DESC",
            (finding.rule, cell, updated if followed else created),
        ).fetchall())
        rows = [row for row in candidates if rule_catalog.judged_text_unchanged(
            row[8], definition, compared_blob=finding.rule_blob)][:1]
        holder = {fingerprint for fingerprint, blob in cast(list[tuple[str, str]], self._connection.execute(
            "SELECT input_fingerprint,rule_blob FROM active_judgments WHERE rule=? AND path=? AND ticket=?",
            (finding.rule, cell, finding.ticket),
        ).fetchall()) if rule_catalog.judged_text_unchanged(blob, definition, compared_blob=finding.rule_blob)}
        if not rows or rows[0][2] == finding.ticket:
            return NotJudgedAgain()
        judgment_id, verdict, _, fingerprint, commit, scope, declared, reads, blob = rows[0]
        # A followed finding's holding ticket judged its old file, so a judgment of its new file after the follow read
        # that file again.
        if not ((fingerprint not in holder) if holder else bool(followed)):
            return NotJudgedAgain()
        # A judgment of an older file at the cell did not read the file the finding is at now.
        judgment = Judgment(blob, fingerprint, commit, verdict, CellInputScope(scope), DeclaredInputScope(declared), _read_files(reads))
        try:
            current = (self._is_ancestor(commit, tip.commit)
                       and self._judgment_inputs_match(judgment, tip, _cell_scope(definition, cell, tip), cell, definition.family))
        except GitReadFailed:
            raise
        except LedgerError:
            current = False
        return SupersedingJudgment(judgment_id, verdict, blob) if current else NotJudgedAgain()

    def _superseded(self, definitions: Mapping[str, Rule]) -> Withdrawal:
        withdrawn: list[WithdrawnFinding] = []
        kept: list[KeptForBuild] = []
        for finding, definition in self._withdrawal_candidates(definitions):
            if not rule_catalog.judged_text_unchanged(finding.rule_blob, definition):
                withdrawn.append(WithdrawnFinding(finding, RuleBodyChanged(definition.blob)))
                continue
            if not self._connection.execute("SELECT 1 FROM sync WHERE id=1").fetchone():
                continue
            verdict = self._judged_again_without(finding, definition, self._synced_tip())
            if isinstance(verdict, NotJudgedAgain):
                continue
            if isinstance(finding.build_ticket, BuildTicket):
                ticket = self._ticket(finding.build_ticket.ticket)
                if ticket["state"] not in FINAL_TICKET_STATES:
                    kept.append(KeptForBuild(finding, verdict, ticket["id"]))
                    continue
            withdrawn.append(WithdrawnFinding(finding, verdict))
        return Withdrawal(tuple(withdrawn), tuple(kept))

    def superseded_findings(self, definitions: Mapping[str, Rule]) -> Withdrawal:
        """What withdraw_superseded withdraws and keeps; it writes nothing.

        definitions maps each rule id to its current catalog rule. Only same-body findings need a synced tip;
        rule-body withdrawals require no source reading.
        """
        if not self._withdrawal_candidates(definitions):
            return Withdrawal((), ())
        return self._superseded(definitions)

    def _obsolete_tickets(self, definitions: Mapping[str, Rule], withdrawal: Withdrawal) -> tuple[TicketRecord, ...]:
        """Cancel stale pins and dependent successors, including a build holding a withdrawn finding."""
        rows = cast(list[tuple[str]], self._connection.execute(
            "SELECT id FROM tickets WHERE state NOT IN ('merged','dropped') ORDER BY id").fetchall())
        tickets = tuple(self._ticket(row[0]) for row in rows)
        obsolete = {ticket["id"] for ticket in tickets if any(
            (definition := definitions.get(base_rule(rule))) is not None
            and not rule_catalog.judged_text_unchanged(blob, definition)
            for rule, blob in ticket["rule_blobs"].items())}
        obsolete.update(item.finding.build_ticket.ticket for item in withdrawal.withdrawn
                        if isinstance(item.verdict, RuleBodyChanged) and isinstance(item.finding.build_ticket, BuildTicket))
        dependencies = cast(list[tuple[str, str]], self._connection.execute(
            "SELECT ticket,predecessor FROM ticket_predecessors").fetchall())
        while successors := {ticket for ticket, predecessor in dependencies if predecessor in obsolete} - obsolete:
            obsolete.update(successors)
        return tuple(ticket for ticket in tickets if ticket["id"] in obsolete)

    def _rule_generation_changes(self, definitions: Mapping[str, Rule]) -> tuple[RuleGenerationChange, ...]:
        """Observed identity transitions, including restoration and catalog removal, without writing."""
        rows = cast(list[tuple[str, str, int]], self._connection.execute(
            "SELECT rule,rule_blob,generation FROM rule_generations ORDER BY rule").fetchall())
        changes: list[RuleGenerationChange] = []
        for rule, blob, generation in rows:
            definition = definitions.get(rule)
            if (definition is None and not blob) or (definition is not None and blob
                    and rule_catalog.judged_text_unchanged(blob, definition)):
                continue
            # A worker can finish a current-text read before the planner reconciles it. Preserve that
            # fresh evidence, never evidence from a previous generation of a restored body.
            judgments = cast(list[tuple[int, str]], self._connection.execute(
                f"SELECT id,rule_blob FROM active_judgments WHERE {base_rule_sql('rule')}=? "
                + "AND verdict!='removed' ORDER BY id", (rule,)).fetchall())
            fresh = tuple(id for id, pin in judgments
                          if definition is not None and rule_catalog.judged_text_unchanged(pin, definition))
            changes.append(RuleGenerationChange(rule, blob, generation, definition.blob if definition is not None else "", fresh))
        return tuple(changes)

    def withdraw_superseded(self, definitions: Mapping[str, Rule]) -> Withdrawal:
        """Withdraw all open old-body findings and cancel obsolete tickets in one transaction.

        Same-body findings retain the existing newer-judgment policy, including open-build protection.
        Source proofs run before the write lock; the transaction rechecks the plan before changing rows.
        Workers judge outside this transaction. Their hold/record writes check pins and ticket state under
        their own write locks, so cancellation cannot race an old-body finding back into the ledger.
        """
        def attempt() -> Withdrawal:
            plan = self._superseded(definitions)
            cancelled = self._obsolete_tickets(definitions, plan)
            generations = self._rule_generation_changes(definitions)
            if not plan.withdrawn and not cancelled and not generations:
                return plan
            with self._connection:
                _ = self._connection.execute("BEGIN IMMEDIATE")
                if (self._superseded(definitions) != plan
                        or self._obsolete_tickets(definitions, plan) != cancelled
                        or self._rule_generation_changes(definitions) != generations):
                    raise ChangedBeforeLock("the findings or tickets changed while they were withdrawn; rerun")
                now = _now()
                # Drop builds and their dependent tickets before withdrawing their findings, atomically.
                _ = self._connection.executemany("UPDATE tickets SET state='dropped',updated_at=? WHERE id=?",
                                                 [(now, ticket["id"]) for ticket in cancelled])
                _ = self._connection.executemany(
                    "UPDATE held_findings SET build_ticket='',updated_at=? WHERE build_ticket=? AND state='filed'",
                    [(now, ticket["id"]) for ticket in cancelled])
                _ = self._connection.executemany(
                    "UPDATE rule_generations SET generation=?,rule_blob=? WHERE rule=?",
                    [(change.generation + 1, change.current_blob, change.rule) for change in generations])
                _ = self._connection.executemany(
                    "UPDATE judgments SET generation=? WHERE id=?",
                    [(change.generation + 1, id) for change in generations for id in change.fresh_judgments])
                for item in plan.withdrawn:
                    judgment = item.verdict.judgment_id if isinstance(item.verdict, SupersedingJudgment) else 0
                    _ = self._connection.execute(
                        "UPDATE held_findings SET state='withdrawn',withdrawn_by=?,build_ticket='',updated_at=? WHERE finding_id=?",
                        (judgment, now, item.finding.finding_id))
                    if isinstance(item.verdict, RuleBodyChanged):
                        _ = self._connection.execute(
                            "INSERT INTO rule_body_withdrawals VALUES (?,?) ON CONFLICT(finding_id) DO UPDATE SET rule_blob=excluded.rule_blob",
                            (item.finding.finding_id, item.verdict.rule_blob))
            return plan

        return retry_on_change(attempt)

    def _changed_since(self, commit: str, tip: str) -> frozenset[str]:
        key = (commit, tip)
        if key not in self._changed_paths:
            changed = _git(self.repo, "diff", "--no-renames", "--name-only", "-z", commit, tip)
            self._changed_paths[key] = frozenset(path for path in changed.split("\0") if path)
        return self._changed_paths[key]

    def files_naming(self, tip: str, name: str, pathspecs: tuple[str, ...] = ("*.rs",)) -> tuple[str, ...]:
        """The files at tip that match pathspecs and hold name as a whole word, read once for each ledger."""
        key = (tip, name, pathspecs)
        if key not in self._named_files:
            result = _git_bytes(self.repo, "grep", "-l", "-z", "-w", "-F", "-e", name, tip, "--", *pathspecs, answers=GitExitAnswers(frozenset({0, 1})))
            self._named_files[key] = tuple(os.fsdecode(entry).removeprefix(f"{tip}:")
                                           for entry in result.stdout.split(b"\0") if entry)
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

    def _crate_file_modules(self, commit: str, crate: str) -> dict[str, RootedFileModule]:
        """Prove file modules through plain declarations, with one scan per crate and commit."""
        key = (commit, crate)
        if key in self._file_modules:
            return self._file_modules[key]
        modules: dict[str, RootedFileModule] = {}
        self._file_modules[key] = modules
        tip = self._tip(commit)
        manifest = self._commit_file(commit, str(PurePosixPath(crate) / "Cargo.toml"))
        if not isinstance(manifest, bytes):
            return modules
        data = _table(tomllib.loads(manifest.decode(errors="replace")))
        bins = data.get("bin", [])
        targets = [_table(data.get("lib"))]
        if isinstance(bins, list):
            targets.extend(_table(item) for item in cast(list[object], bins))
        if any("path" in target for target in targets):
            return modules
        files = {path for path in tip.blobs if path.endswith(".rs") and tip.crate(path) == crate}
        links: dict[str, tuple[str, ...]] = {}
        for path in sorted(files):
            content = self._commit_file(commit, path)
            if not isinstance(content, bytes):
                return modules
            words = tuple(token.value for token in style_footprint.tokens(content.decode(errors="replace")))
            attributes = style_footprint.decorated_items(words)
            if any(words[index] == "mod" and "path" in attribute for index, attribute in attributes.items()):
                return modules
            depth = 0
            names: list[str] = []
            for index, word in enumerate(words):
                if word == "mod" and depth == 0 and index not in attributes and index + 2 < len(words):
                    if style_footprint.IDENTIFIER.fullmatch(words[index + 1]) and words[index + 2] == ";":
                        names.append(words[index + 1])
                depth += (word in ("{", "(", "[")) - (word in ("}", ")", "]"))
            links[path] = tuple(names)
        src = PurePosixPath(crate) / "src"
        pending: list[tuple[str, tuple[str, ...]]] = [(str(src / name), ("crate",)) for name in ("lib.rs", "main.rs") if str(src / name) in files]
        while pending:
            path, chain = pending.pop()
            if path in modules:
                if modules[path].path != chain:
                    modules.clear()
                    return modules
                continue
            modules[path] = RootedFileModule(chain)
            file = PurePosixPath(path)
            is_root = file.parent == src and file.name in ("lib.rs", "main.rs")
            directory = file.parent if is_root or file.name == "mod.rs" else file.with_suffix("")
            for name in links[path]:
                candidates = [str(directory / (name + ".rs")), str(directory / name / "mod.rs")]
                children = [child for child in candidates if child in files and not PurePosixPath(child).is_relative_to(src / "bin")]
                if len(children) == 1:
                    pending.append((children[0], (*chain, name)))
        return modules

    def _file_module(self, commit: str, path: str) -> RootedFileModule | UnreadableFileModule:
        """Read the same proved module chain for imports and trait declarations."""
        crate = self._tip(commit).crate(path)
        return self._crate_file_modules(commit, crate).get(path, UnreadableFileModule())

    def _relative_trait_path(self, commit: str, path: str, trait: tuple[str, ...],
                             position: ImplPosition) -> ResolvedTraitPath | UnresolvedTraitPath:
        """Resolve self and super only from a file-level header's proved module chain."""
        if trait[0] not in ("self", "super"):
            return ResolvedTraitPath(trait)
        if isinstance(position, BlockScopedImpl):
            return UnresolvedTraitPath()
        module = self._file_module(commit, path)
        if isinstance(module, UnreadableFileModule):
            return UnresolvedTraitPath()
        parts = module.path
        if trait[0] == "self":
            return ResolvedTraitPath((*parts, *trait[1:]))
        index = 0
        while index < len(trait) and trait[index] == "super":
            if len(parts) == 1:
                return UnresolvedTraitPath()
            parts = parts[:-1]
            index += 1
        return ResolvedTraitPath((*parts, *trait[index:]))

    def _trait_site_position(self, commit: str, path: str, line: int, spelling: str) -> ImplPosition:
        """Read the qualifier's scope, or the declared item's header scope when the qualifier is absent."""
        content = self._commit_file(commit, path)
        if not isinstance(content, bytes):
            return UnprovedImplPosition()
        tokenized = style_footprint.tokenized_text(content.decode(errors="replace"))
        words = tuple(token.value for token in tokenized.tokens)
        wanted = tuple(token.value for token in style_footprint.tokens(spelling.split(";", 1)[0]))
        if not wanted:
            return UnprovedImplPosition()
        blocks: list[int] = []
        scopes: dict[tuple[int, ...], int] = {}
        for index, (word, number) in enumerate(zip(words, tokenized.lines, strict=True)):
            if number == line and words[index:index + len(wanted)] == wanted:
                scopes[tuple(blocks)] = index
            if word == "{":
                blocks.append(index)
            elif word == "}" and blocks:
                _ = blocks.pop()
        if not scopes:
            declared = self._declared_file(commit, path)
            item = declared.declaration_at(line)
            if not isinstance(item, DeclaredItem):
                return UnprovedImplPosition()
            # Several item boundaries on one line do not prove which item owns the site.
            boundaries = [other for other in declared.declared_items
                          if line in (other.first_line, other.keyword_line, other.last_line)]
            if len(boundaries) > 1 or any(other.import_position != item.import_position for other in boundaries):
                return UnprovedImplPosition()
            return item.import_position
        if len(scopes) != 1:
            return UnprovedImplPosition()
        scope, index = next(iter(scopes.items()))
        return BlockScopedImpl(index) if scope else FileLevelImpl()

    def _trait_import_path(self, commit: str, path: str, trait: tuple[str, ...],
                           position: ImplPosition) -> ResolvedTraitPath | UnresolvedTraitPath:
        """Read only imports visible at the header; enclosing block imports still block their names."""
        if isinstance(position, UnprovedImplPosition):
            return UnresolvedTraitPath()
        content = self._commit_file(commit, path)
        words = tuple(token.value for token in style_footprint.tokens(content.decode(errors="replace"))) if isinstance(content, bytes) else ()
        blocks: list[int] = []
        enclosing: dict[int, tuple[int, ...]] = {}
        for index, word in enumerate(words):
            if word == "use" or isinstance(position, BlockScopedImpl) and index == position.header_word:
                enclosing[index] = tuple(blocks)
            if word == "{":
                blocks.append(index)
            elif word == "}" and blocks:
                _ = blocks.pop()
        header_blocks = enclosing.get(position.header_word, ()) if isinstance(position, BlockScopedImpl) else ()
        imports: dict[str, list[tuple[str, ...]]] = {}
        blocked: set[str] = set()
        glob = False
        for item in style_footprint.use_items(words):
            use = words.index("use", item.start, item.end)
            use_blocks = enclosing.get(use, ())
            if use_blocks and (len(use_blocks) > len(header_blocks) or header_blocks[:len(use_blocks)] != use_blocks):
                continue
            body = words[use + 1:item.end - 1]
            bindings: list[tuple[str, tuple[str, ...]]] = []
            if "as" in body:
                alias = body.index("as")
                paths = style_footprint.use_paths(body[:alias])
                if len(paths) == 1 and len(body) == alias + 2 and style_footprint.IDENTIFIER.fullmatch(body[alias + 1]):
                    bindings.append((body[alias + 1], paths[0]))
            else:
                bindings.extend((imported[-1], imported) for imported in item.paths)
            supported = bool(bindings) and not item.attributed and not use_blocks and words[item.start] == "use"
            if "*" in body:
                glob = True
            if not supported:
                # An unread visible import can bind either end of the trait path.
                blocked.update(name for name, _ in bindings)
                if not bindings:
                    blocked.update(word for word in body if style_footprint.IDENTIFIER.fullmatch(word))
                continue
            for name, imported in bindings:
                imports.setdefault(name, []).append(imported)
        seen: set[tuple[str, str]] = set()
        # A binding must not expand twice, even when each expansion grows the path.
        while len(seen) < 32:
            relative = self._relative_trait_path(commit, path, trait, position)
            if isinstance(relative, UnresolvedTraitPath):
                return relative
            trait = relative.path
            if glob or trait[0] in blocked or trait[-1] in blocked:
                return UnresolvedTraitPath()
            candidates = imports.get(trait[0], [])
            if not candidates:
                return ResolvedTraitPath(trait)
            binding = (path, trait[0])
            if len(candidates) != 1 or binding in seen:
                return UnresolvedTraitPath()
            seen.add(binding)
            trait = (*candidates[0], *trait[1:])
        return UnresolvedTraitPath()

    def _trait_module(self, commit: str, path: str, trait: tuple[str, ...]) -> ResolvedTraitModule | UnresolvedTraitModule:
        """Resolve only declared module traits; a missing or ambiguous declaration proves nothing."""
        tip = self._tip(commit)
        crate = tip.crate(path)
        homes = [candidate for candidate in self.files_naming(commit, trait[-1])
                 if candidate in tip.rust_files and tip.crate(candidate) == crate
                 for item in self._declared_file(commit, candidate).declared_items
                 if item.declaration.keyword == "trait" and item.declaration.name == trait[-1]
                 and not isinstance(item.declaration.owner, ImplOrTraitItem)]
        modules: dict[str, str] = {}
        for candidate in (*homes, path):
            module = self._file_module(commit, candidate)
            if isinstance(module, RootedFileModule):
                modules[candidate] = "::".join(module.path)
        if len(trait) == 1:
            local = [home for home in homes if home in modules and modules[home] == modules.get(path)]
            choices = local if local else homes
        else:
            if trait[0] in ("self", "super"):
                return UnresolvedTraitModule()
            module = "::".join(trait[:-1] if trait[0] == "crate" else ("crate", *trait[:-1]))
            choices = [home for home in homes if modules.get(home) == module]
        return (ResolvedTraitModule(modules[choices[0]], len(homes))
                if len(choices) == 1 and choices[0] in modules else UnresolvedTraitModule())

    def _trait_identity(self, left: TraitMembership, left_commit: str, left_path: str,
                        right: TraitMembership, right_commit: str, right_path: str) -> TraitIdentity:
        """Prove trait equality or difference; missing name evidence keeps the finding."""
        if isinstance(left, UnreadableImplementedTrait) or isinstance(right, UnreadableImplementedTrait):
            return TraitIdentity.UNCERTAIN
        if isinstance(left, NoImplementedTrait) or isinstance(right, NoImplementedTrait):
            return TraitIdentity.SAME if left == right else TraitIdentity.DIFFERENT
        left_import = self._trait_import_path(left_commit, left_path, left.path, left.position)
        right_import = self._trait_import_path(right_commit, right_path, right.path, right.position)
        if isinstance(left_import, UnresolvedTraitPath) or isinstance(right_import, UnresolvedTraitPath):
            return TraitIdentity.UNCERTAIN
        left_words, right_words = left_import.path, right_import.path
        if left.arguments != right.arguments or left_words[-1] != right_words[-1]:
            return TraitIdentity.DIFFERENT
        if left_words == right_words:
            return TraitIdentity.SAME
        left_home = self._trait_module(left_commit, left_path, left_words)
        right_home = self._trait_module(right_commit, right_path, right_words)
        if isinstance(left_home, ResolvedTraitModule) and isinstance(right_home, ResolvedTraitModule):
            if left_home.module != right_home.module:
                return TraitIdentity.DIFFERENT
            if left_home.declarations == right_home.declarations == 1:
                return TraitIdentity.SAME
        return TraitIdentity.UNCERTAIN

    def _trait_rename_identity(self, membership: ImplementedTrait, commit: str, path: str,
                               built: BuiltRename, qualifier: tuple[str, ...]) -> TraitIdentity:
        """Prove what the qualifier names at the built finding's own site and commit."""
        resolved = self._trait_import_path(built.before_commit, built.before_path, qualifier, built.before_position)
        if isinstance(resolved, UnresolvedTraitPath):
            return TraitIdentity.UNCERTAIN
        candidate = resolved.path
        if commit == built.before_commit and path == built.before_path:
            return self._trait_identity(membership, commit, path,
                                        ImplementedTrait(candidate, membership.arguments, built.before_position), commit, path)
        if len(candidate) == 1:
            home = self._trait_module(built.before_commit, built.before_path, candidate)
            if isinstance(home, UnresolvedTraitModule):
                return TraitIdentity.UNCERTAIN
            candidate = (*home.module.split("::"), candidate[-1])
        return self._trait_identity(membership, commit, path,
                                    ImplementedTrait(candidate, membership.arguments, built.before_position), built.before_commit, built.before_path)

    def _matches_declaration(self, item: DeclaredItem, declaration: Declaration, requirement: TraitRequirement,
                             commit: str, path: str) -> TraitIdentity:
        if item.declaration != declaration:
            return TraitIdentity.DIFFERENT
        if isinstance(requirement, UnrestrictedTraitMembership):
            return TraitIdentity.SAME
        return self._trait_identity(requirement.implemented_trait, requirement.commit, requirement.path,
                                    item.implemented_trait, commit, path)

    def _matching_items(self, finding: HeldFinding, declaration: Declaration, requirement: TraitRequirement,
                        commit: str, path: str) -> tuple[DeclaredItem, ...] | TraitIdentityUncertain:
        matches: list[DeclaredItem] = []
        for item in self._declared_file(commit, path).declared_items:
            identity = self._matches_declaration(item, declaration, requirement, commit, path)
            if identity == TraitIdentity.UNCERTAIN:
                return TraitIdentityUncertain(finding, declaration)
            if identity == TraitIdentity.SAME:
                matches.append(item)
        return tuple(matches)

    def _outside_mapping(self, read: str, tip: str, path: str, line: int) -> declarations.MappedSite:
        key = (read, tip, path, path)
        new = self._declared_file(tip, path)
        if key not in self._outside_diffs:
            self._outside_diffs[key] = _git(self.repo, "diff", "--no-ext-diff", "--no-textconv", "--no-color", "--no-renames",
                                            "--diff-algorithm=myers", "--no-indent-heuristic", "--unified=0", read, tip, "--", path)
        return declarations.mapped_site(self._outside_diffs[key], path, line, new)

    def _crate_declares(self, tip: RepositoryTip, crate: str, name: str) -> bool:
        key = (tip.commit, crate, name)
        if key not in self._crate_names:
            self._crate_names[key] = any(item.declaration.name == name
                                         for path in tip.rust_files if tip.crate(path) == crate
                                         for item in self._declared_file(tip.commit, path).declared_items)
        return self._crate_names[key]

    def _outside_body_check(self, read: str, old_path: str, old: DeclaredItem, commit: str, path: str,
                            item: DeclaredItem) -> tuple[int, int, tuple[OutsideFailure, ...]]:
        key = (read, old_path, commit, old, path, item)
        if key not in self._outside_bodies:
            old_body = self._item_body(read, old_path, old)
            body = self._item_body(commit, path, item)
            count = len(old_body.lines) if isinstance(old_body, declarations.ReadableBody) else 0
            unchanged = 0
            failures: tuple[OutsideFailure, ...] = ()
            if not count or not isinstance(body, declarations.ReadableBody) or not body.lines:
                failures = (OutsideFailure.BODY_UNREADABLE,)
            elif isinstance(old_body, declarations.ReadableBody):
                unchanged = declarations.preserved_body(old_body, body, old.declaration.name, item.declaration.name)
                if 2 * unchanged < count:
                    failures = (OutsideFailure.BODY_FAILED,)
            self._outside_bodies[key] = (unchanged, count, failures)
        return self._outside_bodies[key]

    def _item_body(self, commit: str, path: str, item: DeclaredItem) -> declarations.ReadableBody | declarations.UnreadableBody:
        """An immutable item's body is shared by every candidate comparison."""
        key = (commit, path, item)
        if key not in self._item_bodies:
            self._item_bodies[key] = declarations.item_body(self._declared_file(commit, path), item)
        return self._item_bodies[key]

    def _outside_extern_names(self, commit: str, path: str) -> frozenset[str] | UnresolvedTypeArguments:
        """Read extern names only to refuse argument proof, never to resolve an alias."""
        key = (commit, path)
        if key in self._extern_names:
            return self._extern_names[key]
        tip = self._tip(commit)
        crate = PurePosixPath(tip.crate(path))
        files = {path, *(str(crate / "src" / root) for root in ("lib.rs", "main.rs"))}
        names: set[str] = set()
        for file in sorted(files & tip.blobs.keys()):
            content = self._commit_file(commit, file)
            if not isinstance(content, bytes):
                self._extern_names[key] = UnresolvedTypeArguments()
                return self._extern_names[key]
            tokens = tuple(token.value for token in style_footprint.tokens(content.decode(errors="replace")))
            attribute_ends = {end for _, end in style_footprint.attribute_spans(tokens)}
            depth = 0
            for cursor, token in enumerate(tokens):
                if tokens[cursor:cursor + 2] == ("extern", "crate"):
                    end = next((stop for stop in range(cursor + 2, len(tokens)) if tokens[stop] == ";"), len(tokens))
                    body = tokens[cursor + 2:end]
                    match body:
                        case (crate_name,):
                            name = crate_name
                        case (crate_name, "as", name):
                            pass
                        case _:
                            self._extern_names[key] = UnresolvedTypeArguments()
                            return self._extern_names[key]
                    start = cursor - 1 if cursor and tokens[cursor - 1] == "pub" else cursor
                    if (depth or start in attribute_ends or start > 0 and tokens[start - 1] not in (";", "}")
                            or end == len(tokens)
                            or not all(style_footprint.IDENTIFIER.fullmatch(part) for part in (crate_name, name))):
                        self._extern_names[key] = UnresolvedTypeArguments()
                        return self._extern_names[key]
                    names.update((crate_name, name))
                depth += (token in ("{", "(", "[")) - (token in ("}", ")", "]"))
        self._extern_names[key] = frozenset(names)
        return self._extern_names[key]

    def _outside_type_arguments(self, commit: str, path: str, arguments: tuple[str, ...],
                                position: ImplPosition, contexts: tuple[tuple[str, str], ...],
                                ) -> ResolvedTypeArguments | UnresolvedTypeArguments:
        """Resolve argument paths with the header's imports; unsupported arguments grant no owner proof."""
        words: list[str] = []
        index = 0
        primitives = {"bool", "char", "str", "u8", "u16", "u32", "u64", "u128", "usize",
                      "i8", "i16", "i32", "i64", "i128", "isize", "f32", "f64"}
        while index < len(arguments):
            word = arguments[index]
            if word in ("<", ">", ","):
                words.append(word)
                index += 1
                continue
            if not style_footprint.IDENTIFIER.fullmatch(word):
                return UnresolvedTypeArguments()
            parts = [word]
            index += 1
            while index + 1 < len(arguments) and arguments[index] == "::":
                if not style_footprint.IDENTIFIER.fullmatch(arguments[index + 1]):
                    return UnresolvedTypeArguments()
                parts.append(arguments[index + 1])
                index += 2
            resolved = self._trait_import_path(commit, path, tuple(parts), position)
            if isinstance(resolved, UnresolvedTraitPath):
                return UnresolvedTypeArguments()
            rooted = resolved.path
            if rooted == (word,) and word in primitives:
                words.append(word)
                continue
            if rooted[0] != "crate":
                for revision, file in contexts:
                    extern_names = self._outside_extern_names(revision, file)
                    if isinstance(extern_names, UnresolvedTypeArguments) or word in extern_names or rooted[0] in extern_names:
                        return UnresolvedTypeArguments()
                module = self._file_module(commit, path)
                if not isinstance(position, FileLevelImpl) or isinstance(module, UnreadableFileModule):
                    return UnresolvedTypeArguments()
                declared = self._declared_file(commit, path)
                local = [item for item in declared.declared_items
                         if item.declaration.name == rooted[0]
                         and item.declaration.keyword in ("mod", "struct", "enum", "union", "type")
                         and isinstance(item.import_position, FileLevelImpl)]
                if len(local) > 1:
                    return UnresolvedTypeArguments()
                for item in local:
                    header = tuple(token.value for token in style_footprint.tokens("\n".join(
                        declared.source_lines[item.first_line - 1:item.keyword_line])))
                    if any(header[span[0] + 2:span[0] + 3] in (("cfg",), ("cfg_attr",))
                           for span in style_footprint.attribute_spans(header)):
                        return UnresolvedTypeArguments()
                # Only a proved local root receives the file module; other paths stay external.
                if local:
                    rooted = (*module.path, *rooted)
                elif len(rooted) == 1:
                    return UnresolvedTypeArguments()
            for part in rooted:
                words.extend((part, "::"))
            _ = words.pop()
        return ResolvedTypeArguments(tuple(words))

    def _outside_type_identity(self, left: DeclaredItem, read: str, source: str,
                               right: DeclaredItem, commit: str, destination: str) -> TraitIdentity:
        """Prove the complete implementing type, without reducing paths to their last name."""
        before, after = left.implemented_type, right.implemented_type
        if isinstance(before, declarations.UnreadableImplementedType) or isinstance(after, declarations.UnreadableImplementedType):
            return TraitIdentity.UNCERTAIN
        if isinstance(before, declarations.NoImplementedType) or isinstance(after, declarations.NoImplementedType):
            return TraitIdentity.SAME if before == after else TraitIdentity.DIFFERENT
        first = self._trait_import_path(read, source, before.path, before.position)
        second = self._trait_import_path(commit, destination, after.path, after.position)
        if isinstance(first, UnresolvedTraitPath) or isinstance(second, UnresolvedTraitPath):
            return TraitIdentity.UNCERTAIN
        contexts = ((read, source), (read, destination), (commit, source), (commit, destination))
        before_arguments = self._outside_type_arguments(read, source, before.arguments, before.position, contexts)
        after_arguments = self._outside_type_arguments(commit, destination, after.arguments, after.position, contexts)
        if isinstance(before_arguments, UnresolvedTypeArguments) or isinstance(after_arguments, UnresolvedTypeArguments):
            return TraitIdentity.UNCERTAIN
        if before_arguments != after_arguments:
            return TraitIdentity.DIFFERENT
        if first.path[0] == second.path[0] == "crate":
            return TraitIdentity.SAME if first.path == second.path else TraitIdentity.DIFFERENT
        first_module, second_module = self._file_module(read, source), self._file_module(commit, destination)
        if isinstance(first_module, UnreadableFileModule) or isinstance(second_module, UnreadableFileModule):
            return TraitIdentity.UNCERTAIN
        if not isinstance(before.position, FileLevelImpl) or not isinstance(after.position, FileLevelImpl):
            return TraitIdentity.UNCERTAIN
        first_path = first.path if first.path[0] == "crate" else (*first_module.path, *first.path)
        second_path = second.path if second.path[0] == "crate" else (*second_module.path, *second.path)
        return TraitIdentity.SAME if first_path == second_path else TraitIdentity.DIFFERENT

    def _outside_search(self, finding: HeldFinding, read: str, old_file: DeclaredFile, old: DeclaredItem,
                        tip: RepositoryTip, changed: frozenset[str], *, line: int, target: Declaration,
                        ) -> OutsideRenameProven | OutsideRenameNotProven:
        """Prove one item rename without a ledger pair; every refused search retains its evidence."""
        mapped = self._outside_mapping(read, tip.commit, finding.path, line)
        crate = tip.crate(finding.path)
        if self._crate_declares(tip, crate, old.declaration.name):
            return OutsideRenameNotProven(mapped, NoOutsideCandidates(), (OutsideFailure.OLD_NAME_PRESENT,))
        examined: list[OutsideCandidate] = []
        passing: list[tuple[str, DeclaredItem]] = []
        requirement = _trait_requirement(old, read, finding.path)
        for path in sorted(set(tip.rust_files) & changed):
            if tip.crate(path) != crate:
                continue
            destination = self._declared_file(tip.commit, path)
            for item in destination.declared_items:
                if item.declaration.name == old.declaration.name:
                    continue
                checks: list[OutsideFailure] = []
                if item.declaration.keyword != target.keyword:
                    checks.append(OutsideFailure.WRONG_KEYWORD)
                if item.declaration.owner != target.owner:
                    checks.append(OutsideFailure.WRONG_OWNER)
                if not checks and target.owner == old.declaration.owner:
                    owner_identity = self._outside_type_identity(old, read, finding.path, item, tip.commit, path)
                    if owner_identity != TraitIdentity.SAME:
                        checks.append(OutsideFailure.WRONG_OWNER)
                if not checks:
                    identity = self._matches_declaration(item, replace(target, name=item.declaration.name),
                                                         requirement, tip.commit, path)
                    if identity == TraitIdentity.UNCERTAIN:
                        checks.append(OutsideFailure.TRAIT_UNCERTAIN)
                    elif identity == TraitIdentity.DIFFERENT:
                        checks.append(OutsideFailure.WRONG_OWNER)
                prior = self._declared_file(read, path)
                if any(other.declaration == item.declaration for other in prior.declared_items):
                    checks.append(OutsideFailure.NOT_GAINED)
                unchanged, count, body_checks = self._outside_body_check(read, finding.path, old, tip.commit, path, item)
                checks.extend(body_checks)
                examined.append(OutsideCandidate(path, item.declaration, item.keyword_line, unchanged, count, tuple(checks)))
                if not checks:
                    passing.append((path, item))
        examined.sort(key=lambda item: (item.path, item.declaration.keyword, item.declaration.name, item.line))
        candidates: NoOutsideCandidates | OutsideCandidates = (OutsideCandidates(tuple(examined)) if examined
                                                               else NoOutsideCandidates())
        paths = {entry[0] for entry in passing}
        if len(paths) > 1:
            return OutsideRenameNotProven(mapped, candidates, (OutsideFailure.SEVERAL_FILES,))
        if len(passing) > 1:
            return OutsideRenameNotProven(mapped, candidates, (OutsideFailure.AMBIGUOUS_ITEMS,))
        if not passing:
            failures = tuple(dict.fromkeys(check for candidate in examined for check in candidate.checks))
            return OutsideRenameNotProven(mapped, candidates, (OutsideFailure.NO_PASSING_FILES, *failures))
        path, item = passing[0]
        proof: MappedSiteProof | GainedFileProof
        if path == finding.path:
            if (not isinstance(mapped, declarations.MappedDeclaration) or mapped.declaration != item.declaration
                    or not item.first_line <= mapped.line <= item.last_line):
                return OutsideRenameNotProven(mapped, candidates, (OutsideFailure.UNMAPPED,))
            proof = MappedSiteProof(mapped)
        else:
            proof = GainedFileProof(path, item.declaration)
        destination = self._declared_file(tip.commit, path)
        key = (read, tip.commit, finding.path, path)
        if key not in self._outside_diffs:
            self._outside_diffs[key] = declarations.source_diff("\n".join(old_file.source_lines) + "\n",
                                                               "\n".join(destination.source_lines) + "\n")
        located = declarations.mapped_site(self._outside_diffs[key], path, line, destination)
        site_line = moved_site_line(old_file, line, old, destination, item)
        if (isinstance(located, declarations.MappedDeclaration) and located.declaration == item.declaration
                and item.first_line <= located.line <= item.last_line):
            site_line = located.line
        return OutsideRenameProven(item.declaration, path, site_line, proof)

    def _outside_refusal_evidence(self, finding: HeldFinding, read: str, old_file: DeclaredFile, old: DeclaredItem,
                                  tip: RepositoryTip, changed: frozenset[str]) -> OutsideRenameNotProven:
        evidence = self._outside_rename(finding, read, old_file, old, tip, changed)
        if isinstance(evidence, OutsideRenameNotProven):
            return evidence
        return OutsideRenameNotProven(self._outside_mapping(read, tip.commit, finding.path, finding.line),
                                     NoOutsideCandidates(), (OutsideFailure.UNMAPPED,))

    def _outside_trait_rename(self, finding: HeldFinding, read: str, old_file: DeclaredFile, old: DeclaredItem,
                               tip: RepositoryTip, changed: frozenset[str],
                               ) -> OutsideRenameProven | OutsideRenameNotProven:
        """Prove the implemented trait's rename before an unchanged member inherits it."""
        membership = old.implemented_trait
        mapped = self._outside_mapping(read, tip.commit, finding.path, finding.line)
        failed = OutsideRenameNotProven(mapped, NoOutsideCandidates(), (OutsideFailure.CHILD_UNPROVED,))
        if not isinstance(membership, ImplementedTrait):
            return failed
        crate = tip.crate(finding.path)
        owners = [(path, item) for path in self.files_naming(read, membership.path[-1])
                  if self._tip(read).crate(path) == crate
                  for item in self._declared_file(read, path).declared_items
                  if item.declaration.keyword == "trait" and item.declaration.name == membership.path[-1]
                  and self._trait_identity(membership, read, finding.path,
                                           ImplementedTrait((item.declaration.name,), membership.arguments, item.import_position),
                                           read, path) == TraitIdentity.SAME]
        if len(owners) != 1:
            return failed
        owner_path, owner = owners[0]
        owner_file = self._declared_file(read, owner_path)
        proof = self._outside_search(replace(finding, path=owner_path), read, owner_file, owner, tip, changed,
                                     line=owner.keyword_line, target=owner.declaration)
        if isinstance(proof, OutsideRenameNotProven):
            return proof
        destination = self._declared_file(tip.commit, proof.path)
        renamed_owner = [item for item in destination.declared_items if item.declaration == proof.declaration]
        if len(renamed_owner) != 1:
            return failed
        trait = ImplementedTrait((proof.declaration.name,), membership.arguments, renamed_owner[0].import_position)
        children = [(path, item) for path in sorted(set(tip.rust_files) & changed) if tip.crate(path) == crate
                    for item in self._declared_file(tip.commit, path).declared_items
                    if item.declaration == old.declaration
                    and self._outside_type_identity(old, read, finding.path, item, tip.commit, path) == TraitIdentity.SAME
                    and self._trait_identity(trait, tip.commit, proof.path, item.implemented_trait,
                                             tip.commit, path) == TraitIdentity.SAME]
        if len(children) != 1:
            return failed
        path, child = children[0]
        return OutsideRenameProven(child.declaration, path,
                                   moved_site_line(old_file, finding.line, old, self._declared_file(tip.commit, path), child),
                                   proof.proof)

    def _outside_owner_rename(self, finding: HeldFinding, read: str, old_file: DeclaredFile, old: DeclaredItem,
                        tip: RepositoryTip, changed: frozenset[str],
                        ) -> OutsideRenameProven | OutsideRenameNotProven:
        """A child follows only inside its proved renamed owner, with its own binding retained."""
        declaration = old.declaration
        if (isinstance(old.implemented_trait, ImplementedTrait)
                and not self._crate_declares(tip, tip.crate(finding.path), old.implemented_trait.path[-1])):
            return self._outside_trait_rename(finding, read, old_file, old, tip, changed)
        owner: DeclaredItem | declarations.NoDeclarationAtLine = declarations.NoDeclarationAtLine()
        if isinstance(declaration.owner, ImplOrTraitItem):
            owners = [item for item in old_file.declared_items
                      if item.declaration.name == declaration.owner.name
                      and item.declaration.keyword in ("struct", "enum", "union", "trait")]
            if len(owners) == 1 and not self._crate_declares(tip, tip.crate(finding.path), declaration.owner.name):
                owner = owners[0]
        if not isinstance(owner, DeclaredItem):
            parent = declarations.lexical_parent(old_file, old)
            if isinstance(parent, DeclaredItem) and parent.declaration.keyword == "fn":
                owner = parent
        if isinstance(owner, DeclaredItem):
            proof = self._outside_search(finding, read, old_file, owner, tip, changed, line=owner.keyword_line, target=owner.declaration)
            if isinstance(proof, OutsideRenameNotProven):
                return proof
            destination = self._declared_file(tip.commit, proof.path)
            expected = replace(declaration, owner=ImplOrTraitItem(proof.declaration.name))
            required_type = old.implemented_type
            owner_items = [item for item in destination.declared_items if item.declaration == proof.declaration]
            if isinstance(required_type, declarations.ImplementedType) and len(owner_items) == 1:
                required_type = declarations.ImplementedType((proof.declaration.name,), required_type.arguments,
                                                            owner_items[0].import_position)
            required_member = replace(old, implemented_type=required_type)
            children = [item for item in destination.declared_items if item.declaration == expected
                        and self._outside_type_identity(required_member, tip.commit, proof.path,
                                                        item, tip.commit, proof.path) == TraitIdentity.SAME]
            if len(children) == 1:
                child = children[0]
                identity = self._trait_identity(old.implemented_trait, read, finding.path,
                                                child.implemented_trait, tip.commit, proof.path)
                if identity == TraitIdentity.SAME:
                    return OutsideRenameProven(child.declaration, proof.path,
                                               moved_site_line(old_file, finding.line, old, destination, child), proof.proof)
            if not children:
                child_proof = self._outside_search(finding, read, old_file, old, tip, changed,
                                                   line=finding.line, target=expected)
                if isinstance(child_proof, OutsideRenameProven) and child_proof.path == proof.path:
                    candidates = [item for item in destination.declared_items if item.declaration == child_proof.declaration]
                    if (len(candidates) == 1 and self._outside_type_identity(
                            required_member, tip.commit, proof.path, candidates[0], tip.commit, proof.path) == TraitIdentity.SAME):
                        return child_proof
                if isinstance(child_proof, OutsideRenameNotProven):
                    return child_proof
            return OutsideRenameNotProven(self._outside_mapping(read, tip.commit, finding.path, finding.line),
                                         NoOutsideCandidates(), (OutsideFailure.CHILD_UNPROVED,))
        proof = self._outside_search(finding, read, old_file, old, tip, changed, line=finding.line, target=declaration)
        if isinstance(proof, OutsideRenameNotProven):
            return proof
        return proof

    def _outside_rename(self, finding: HeldFinding, read: str, old_file: DeclaredFile, old: DeclaredItem,
                        tip: RepositoryTip, changed: frozenset[str],
                        ) -> OutsideRenameProven | OutsideRenameNotProven:
        """An owner proof carries only its own header or a proved retained child site."""
        proof = self._outside_owner_rename(finding, read, old_file, old, tip, changed)
        if isinstance(proof, OutsideRenameNotProven):
            return proof
        binding = declarations.binding_at(old_file, old, finding.line)
        if isinstance(binding, declarations.UnreadableLocalBinding):
            return OutsideRenameNotProven(self._outside_mapping(read, tip.commit, finding.path, finding.line),
                                         NoOutsideCandidates(), (OutsideFailure.CHILD_UNPROVED,))
        if isinstance(binding, declarations.LocalBinding):
            destination = self._declared_file(tip.commit, proof.path)
            owners = [item for item in destination.declared_items if item.declaration == proof.declaration]
            bindings = [child for item in owners for child in declarations.local_bindings(destination, item)
                        if child.name == binding.name]
            if (len(bindings) != 1 or len(owners) != 1 or bindings[0].closure != binding.closure
                    or len(bindings[0].scope) != len(binding.scope)
                    or old_file.binding_scope(old, binding) != destination.binding_scope(owners[0], bindings[0])):
                return OutsideRenameNotProven(self._outside_mapping(read, tip.commit, finding.path, finding.line),
                                             NoOutsideCandidates(), (OutsideFailure.CHILD_UNPROVED,))
            return replace(proof, line=bindings[0].line)
        site_kind = old_file.site_kind(old, finding.line)
        if site_kind == declarations.DeclarationSite.HEADER:
            return proof
        destination = self._declared_file(tip.commit, proof.path)
        owners = [item for item in destination.declared_items if item.declaration == proof.declaration]
        text = old_file.source_lines[finding.line - 1].strip()
        if len(owners) == 1 and text:
            item = owners[0]
            source_sites = [number for number in range(old.keyword_line + 1, old.last_line)
                            if old_file.source_lines[number - 1].strip() == text]
            target_sites = [number for number in range(item.keyword_line + 1, item.last_line)
                            if destination.source_lines[number - 1].strip() == text]
            # The owner's proof cannot replace a changed child or a statement that is no longer unique.
            if source_sites == [finding.line] and len(target_sites) == 1:
                target_line = target_sites[0]
                if (site_kind == declarations.DeclarationSite.STATEMENT
                        and destination.site_kind(item, target_line) == declarations.DeclarationSite.STATEMENT):
                    return replace(proof, line=target_line)
                # A simple named field or parameter keeps its name, complete text and position.
                child = re.fullmatch(r"(?:pub(?:\([^)]*\))?\s+)?([A-Za-z_][A-Za-z_0-9]*)\s*:[^;]+,?", text)
                if (site_kind == declarations.DeclarationSite.UNSUPPORTED_CHILD and child
                        and destination.site_kind(item, target_line) == declarations.DeclarationSite.UNSUPPORTED_CHILD
                        and target_line - item.keyword_line == finding.line - old.keyword_line):
                    name = child[1]
                    named = re.compile(rf"(?:pub(?:\([^)]*\))?\s+)?{re.escape(name)}\s*:")
                    source_names = [number for number in range(old.keyword_line + 1, old.last_line)
                                    if named.match(old_file.source_lines[number - 1].strip())]
                    target_names = [number for number in range(item.keyword_line + 1, item.last_line)
                                    if named.match(destination.source_lines[number - 1].strip())]
                    if source_names == [finding.line] and target_names == [target_line]:
                        return replace(proof, line=target_line)
        return OutsideRenameNotProven(self._outside_mapping(read, tip.commit, finding.path, finding.line),
                                     NoOutsideCandidates(), (OutsideFailure.CHILD_UNPROVED,))

    def _refresh_site_rows(self) -> None:
        version = (cast(tuple[int], self._connection.execute("PRAGMA data_version").fetchone())[0],
                   self._connection.total_changes)
        if version == self._site_rows_version:
            return
        self._site_rows_version = version
        self._site_evidence.clear()
        self._follow_evidence.clear()
        self._built_rename_evidence.clear()
        pinned = cast(list[tuple[str, bytes]], self._connection.execute("SELECT blob,text FROM rule_texts").fetchall())
        self._site_rule_families.clear()
        for blob, text in pinned:
            try:
                source = text.decode()
            except UnicodeDecodeError:
                self._site_rule_families[blob] = "unreadable"
                continue
            if match := re.search(r"(?m)^family:[ \t]*(names|values|types|modules)[ \t]*$", source):
                self._site_rule_families[blob] = match[1]
        grouped: dict[tuple[str, str], list[Judgment]] = {}
        self._site_holding_rows.clear()
        self._inactive_site_holders = set(cast(list[tuple[str, str]], self._connection.execute(
            "SELECT DISTINCT j.rule,j.ticket FROM judgments j WHERE j.verdict!='removed' "
            + "AND NOT EXISTS (SELECT 1 FROM active_judgments active WHERE active.id=j.id)").fetchall()))
        rows = cast(list[tuple[str, str, str, str, str, Verdict, str, str, str, str]], self._connection.execute(
            'SELECT rule,path,rule_blob,input_fingerprint,"commit",verdict,input_scope,declared_scope,read_files,ticket '
            + 'FROM active_judgments ORDER BY recorded_at DESC,id DESC').fetchall())
        for rule, path, blob, fingerprint, commit, verdict, scope, declared, reads, ticket in rows:
            judgment = Judgment(blob, fingerprint, commit, verdict, CellInputScope(scope), DeclaredInputScope(declared), _read_files(reads), ticket)
            grouped.setdefault((rule, path), []).append(judgment)
            self._site_holding_rows.setdefault((rule, ticket), []).append((path, judgment))
        self._site_rows = {key: tuple(rows) for key, rows in grouped.items()}

    def _prepare_sites(self, findings: tuple[HeldFinding, ...], tip: RepositoryTip) -> None:
        """Read each needed immutable blob once, using Git's existing batch reader."""
        wanted: dict[str, str] = {}
        for finding in findings:
            judged = self.judged_commit(finding)
            if isinstance(judged, NoJudgedCommit):
                continue
            read = finding.follow.commit if isinstance(finding.follow, FollowedDeclaration) else judged.commit
            before = self._tip(read)
            for tree in (before, tip):
                blob = tree.blobs.get(finding.path, "")
                if blob and blob not in self._site_sources:
                    wanted[blob] = blob
        if isinstance(self.git_cache, GitReadCache):
            self.git_cache.read_blobs(wanted)
            cached = self.git_cache.contents
            contents = ((blob, cached[blob]) for blob in wanted)
        else:
            contents = blob_contents(self.repo, wanted)
        for blob, content in contents:
            try:
                self._site_sources[blob] = finding_sites.SourceSites(content.decode())
            except UnicodeDecodeError:
                # Non-UTF-8 is not source evidence.
                continue

    def _site_source(self, tip: RepositoryTip, path: str) -> finding_sites.SourceSites:
        blob = tip.blobs.get(path, "")
        if not blob:
            return finding_sites.SourceSites("")
        if blob not in self._site_sources:
            if isinstance(self.git_cache, GitReadCache):
                self.git_cache.read_blobs((blob,))
                content = self.git_cache.contents[blob]
            else:
                content = next(blob_contents(self.repo, {path: blob}))[1]
            self._site_sources[blob] = finding_sites.SourceSites(content.decode())
        return self._site_sources[blob]

    def _original_site_inputs(self, finding: HeldFinding, read: RepositoryTip, tip: RepositoryTip,
                              *, line_local: bool = False) -> bool:
        """Retain the original judgment's scope even when today's rule has narrowed it."""
        candidates = [(path, row) for path, row in self._site_holding_rows.get((finding.rule, finding.ticket), ())
                      if _under(finding.path, path)]
        contextual = finding.rule in {
            "global:rust/no-duplicate-implementations.md", "global:rust/one-mechanism-per-behavior.md",
            "global:rust/crate-apis-expose-behavior.md", "global:rust/behavior-lives-in-its-most-cohesive-crate.md",
        }
        if not candidates:
            return (finding.rule, finding.ticket) not in self._inactive_site_holders and line_local and not contextual
        longest = max(len(path) for path, _ in candidates)
        path, row = next((path, row) for path, row in candidates if len(path) == longest)
        if (not rule_catalog.judged_text_unchanged(row.rule_blob, self._component_rule(finding.rule),
                                                           compared_blob=finding.rule_blob) or not self._is_ancestor(row.commit, tip.commit)
                or row not in self._rows_after_removals(self._site_rows[(finding.rule, path)], tip.commit)):
            return False
        scope = _comparison_scope(row.declared_scope, row.input_scope)
        whole_inputs = contextual or row.input_scope in (CellInputScope.CRATE, CellInputScope.MANIFEST)
        if line_local and not whole_inputs:
            return True
        original = self._tip(row.commit)
        if not self._judgment_inputs_match(row, original, scope, path, None):
            return False
        old = (_read_inputs(original, path, row.reads) if isinstance(row.reads, ReadFiles)
               else self._scope_inputs(original, scope, path))
        new = (_read_inputs(tip, path, row.reads) if isinstance(row.reads, ReadFiles)
               else self._scope_inputs(tip, scope, path))
        if whole_inputs:
            return old == new and self._is_ancestor(row.commit, read.commit)
        # A prior follow is site provenance, not authority to reset broader input evidence.
        return ({name: entry for name, entry in old.items() if name != finding.path}
                == {name: entry for name, entry in new.items() if name != finding.path}
                and self._is_ancestor(row.commit, read.commit))

    def finding_site(self, finding: HeldFinding, at: str) -> SiteEvidence:
        """The shared follow result consumed by filing and both refill rank gates."""
        return self.finding_sites((finding,), at)[finding.finding_id]

    def finding_sites(self, findings: Iterable[HeldFinding], at: str) -> dict[str, SiteEvidence]:
        """Read missing site evidence together; changed rows never borrow another row's proof."""
        requested = tuple(findings)
        if not requested:
            return {}
        self._refresh_site_rows()
        tip = self._tip(at)
        missing = tuple(dict.fromkeys(finding.finding_id for finding in requested
                                      if (finding, tip.commit) not in self._follow_evidence))
        if missing:
            _ = self._declaration_reading(missing, tip, lambda _: False)
        return {finding.finding_id: self._follow_evidence.get((finding, tip.commit),
                                                             NeedsJudgment("finding changed while reading"))
                for finding in requested}

    def _site_after_refusal(self, outcome: DeclarationNotRead | DeclarationInPlace, tip: RepositoryTip) -> DeclarationOutcome:
        evidence = self._site_proof(outcome.finding, tip.commit)
        if not isinstance(evidence, NeedsJudgment):
            return FindingSite(outcome.finding, evidence)
        return outcome

    def _site_proof(self, finding: HeldFinding, at: str) -> SiteEvidence:
        """Additional strict evidence, used only after the existing declaration proofs refuse."""
        self._refresh_site_rows()
        tip = self._tip(at)
        key = (finding, tip.commit)
        if key not in self._site_evidence:
            try:
                self._site_evidence[key] = self._finding_site(finding, tip)
            except GitReadFailed:
                raise
            except (LedgerError, UnicodeDecodeError, finding_sites.UnreadableSourceItems, style_footprint.UnreadableTokenSpans):
                self._site_evidence[key] = NeedsJudgment("source or input proof unreadable")
        return self._site_evidence[key]

    def _finding_site(self, finding: HeldFinding, tip: RepositoryTip) -> SiteEvidence:
        if not isinstance(finding.state, (Unfiled, Filed)) or not finding.path.endswith(".rs"):
            return NeedsJudgment("not an open Rust finding")
        if ((finding.rule, finding.ticket) in self._inactive_site_holders
                and not self._site_holding_rows.get((finding.rule, finding.ticket))):
            return NeedsJudgment("judgment belongs to an inactive rule generation")
        judged = self.judged_commit(finding)
        if isinstance(judged, NoJudgedCommit):
            return NeedsJudgment("no judged commit")
        read = finding.follow.commit if isinstance(finding.follow, FollowedDeclaration) else judged.commit
        rows = self._site_rows.get((finding.rule, finding.path), ())
        # Share the removal rule with coverage. Include the site's commit as a candidate even if
        # this legacy finding has no matching cell row.
        site_row = Judgment(finding.rule_blob, "", read, "held", CellInputScope.FILE, DeclaredInputScope.FILE)
        eligible = self._rows_after_removals((site_row, *rows), tip.commit)
        original_row = replace(site_row, commit=judged.commit)
        if (not self._is_ancestor(judged.commit, read) or not self._is_ancestor(read, tip.commit)
                or site_row not in eligible
                or original_row not in self._rows_after_removals((original_row, *rows), tip.commit)):
            return NeedsJudgment("judgment crossed ancestry or removal boundary")
        before = self._tip(read)
        definition = next((rule for rule in self._catalog
                           if rule.rule_id == base_rule(finding.rule)), None)
        family = self._site_rule_families.get(finding.rule_blob, "")
        if not family and definition is not None and rule_catalog.judged_text_unchanged(finding.rule_blob, definition):
            family = definition.family or ""
        for tree in (before, tip):
            entry = tree.entries.get(finding.path)
            if entry is not None and entry.mode not in REGULAR_FILE_MODES:
                return NeedsJudgment("not a regular source file")
        if family not in ("names", "values", "types", "modules"):
            return NeedsJudgment("pinned rule family unreadable")
        if not self._original_site_inputs(finding, before, tip, line_local=family in ("names", "values")):
            return NeedsJudgment("original context inputs changed or missing")
        old = self._site_source(before, finding.path)
        new = self._site_source(tip, finding.path)
        # Reject an unproved owner before accepting an unchanged file.
        _ = old.owner_chain(finding.line), new.items
        if before.blobs.get(finding.path) == tip.blobs.get(finding.path) and finding.path in tip.blobs:
            # The file gate itself is satisfied; context rules also retain their original external inputs.
            return UnchangedSite(finding.path, finding.line)
        if not (1 <= finding.line <= len(old.lines)):
            return NeedsJudgment("old source coordinate unreadable")
        line = old.lines[finding.line - 1]
        if not line.strip() or finding.text.strip() != line.strip():
            return NeedsJudgment("finding text is not its old source line")
        if finding.kind == "type" and not old.locates_subject(finding.line, finding.before):
            return NeedsJudgment("proposed type subject is not located in its original item")
        key = (read, tip.commit, finding.path)
        if key not in self._site_maps:
            # A proven binding rename already supplies this alignment; never run a second checker.
            proof = self._binding_proofs.get(key)
            if proof is not None and proof.explained and any(isinstance(check, AppliedInCode) for check in proof.checks.values()):
                self._site_maps[key] = dict(proof.line_map)
            else:
                patch = _git(self.repo, "diff", "--no-ext-diff", "--no-textconv", "--no-color", "--no-renames",
                             "--diff-algorithm=myers", "--no-indent-heuristic", "--unified=0", read, tip.commit, "--", finding.path)
                self._site_maps[key] = finding_sites.unchanged_lines(patch, old, new)
        mapped = self._site_maps[key].get(finding.line, 0)
        if (mapped and mapped <= len(new.lines) and line == new.lines[mapped - 1]
                and old.same_owner(finding.line, new, mapped)):
            if family in ("names", "values"):
                return UnchangedSite(finding.path, mapped)
            if old.same_item(finding.line, new, mapped):
                return UnchangedSite(finding.path, mapped)
            return NeedsJudgment("enclosing item, owner, imports or original inputs changed")
        return NeedsJudgment("unchanged source site not proved")

    def _declaration_outcome(self, finding: HeldFinding, tip: RepositoryTip, rust_files: frozenset[str],
                             approved: Callable[[Path], bool], renames: tuple[BuiltRename, ...],
                             built_findings: tuple[HeldFinding, ...]) -> DeclarationOutcome:
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
            return self._site_after_refusal(DeclarationNotRead(finding), tip)
        declaration = old.declaration
        requirement = _trait_requirement(old, read, finding.path)
        if isinstance(old.implemented_trait, UnreadableImplementedTrait):
            return TraitIdentityUncertain(finding, declaration)
        in_place = self._matching_items(finding, declaration, requirement, tip.commit, finding.path)
        if isinstance(in_place, TraitIdentityUncertain):
            return in_place
        if in_place:
            return self._site_after_refusal(DeclarationInPlace(finding), tip)
        # A destination must gain the same member, including its proved trait identity.
        found: dict[str, tuple[DeclaredItem, ...]] = {}
        for path in self.files_naming(tip.commit, declaration.name):
            if path == finding.path or path not in rust_files or path not in changed:
                continue
            prior = self._matching_items(finding, declaration, requirement, read, path)
            items = self._matching_items(finding, declaration, requirement, tip.commit, path)
            if isinstance(prior, TraitIdentityUncertain):
                return prior
            if isinstance(items, TraitIdentityUncertain):
                return items
            if items and not prior:
                found[path] = items
        if len(found) > 1 or any(len(items) != 1 for items in found.values()):
            return DeclaredInSeveralFiles(finding, declaration, tuple(sorted(found)),
                                          self._outside_refusal_evidence(finding, read, old_file, old, tip, changed))
        for path in found:
            if isinstance(finding.state, Filed) and tip.crate(path) != tip.crate(finding.path):
                return DeclaredInAnotherCrate(finding, declaration, path,
                                              self._outside_refusal_evidence(finding, read, old_file, old, tip, changed))
        # The open build holds the finding whatever the rename check would find, so the check does not run.
        if isinstance(finding.build_ticket, BuildTicket):
            ticket = self._ticket(finding.build_ticket.ticket)
            if ticket["state"] not in FINAL_TICKET_STATES:
                return KeptForOpenBuild(finding, ticket["id"])
        if found:
            path, items = next(iter(found.items()))
            item, = items
            return self._moved_site(finding, old_file, old, declaration, tip, path, item)
        after_builds = self._built_renaming(finding, read, old, tip, approved, renames)
        if isinstance(after_builds, TraitIdentityUncertain):
            return after_builds
        renamed = self._renamed_site(finding, read, old_file, old, after_builds, tip, rust_files, changed, approved)
        if not isinstance(renamed, NoRenamedSite):
            return renamed
        if isinstance(after_builds, RenamedByBuilds):
            followed = self._followed_rename(finding, read, old_file, old, after_builds, tip, rust_files, changed)
            if not isinstance(followed, NoRenamedSite):
                return followed
        if isinstance(finding.state, Filed) and finding.path in tip.blobs and finding.path not in rust_files:
            return DeclarationGoneFromFile(finding, declaration)
        # A refused subject question keeps the established transition, without a ledger-wide name veto.
        invalid_type = any(built.path == finding.path and built.kind == "type"
                           and not isinstance(_item_rename(built.kind, built.before, built.after), ItemRename)
                           and (head := _ITEM_HEAD.fullmatch(built.before.strip()))
                           and (head[1], head[2]) == (declaration.keyword, declaration.name) for built in built_findings)
        if isinstance(after_builds, (RenamedByBuilds, RefusedBuiltRename)) or invalid_type:
            if isinstance(finding.state, Filed) and finding.path in tip.blobs:
                return DeclarationGoneFromFile(finding, declaration)
            return DeclarationRemoved(finding, declaration)
        outside = self._outside_rename(finding, read, old_file, old, tip, changed)
        # Search first. A scoped built pair that also names a gained candidate keeps the prior filed refusal.
        # This is no authority to follow or withdraw an unfiled row, and an old spelling alone is insufficient.
        if isinstance(finding.state, Filed) and finding.path in tip.blobs:
            targets = ((outside.declaration,) if isinstance(outside, OutsideRenameProven) else
                       tuple(item.declaration for item in outside.candidates.items
                             if OutsideFailure.NOT_GAINED not in item.checks)
                       if isinstance(outside.candidates, OutsideCandidates) else ())
            if any(tip.crate(built.path) == tip.crate(finding.path)
                   and isinstance(built.rename.scope, (ModuleQualified, TypeQualified))
                   and built.rename.old == declaration.name and approved(built.proposal)
                   and any(target.keyword == declaration.keyword and target.owner == declaration.owner
                           and target.name == built.rename.new for target in targets) for built in renames):
                return DeclarationGoneFromFile(finding, declaration)
        if isinstance(outside, OutsideRenameProven):
            # An exact competing built pair keeps a filed decision at its original site. A name alone cannot
            # block the search, and an outside-crate decision cannot withdraw an unfiled finding.
            if isinstance(finding.state, Filed) and finding.path in tip.blobs:
                for built in renames:
                    if (tip.crate(built.path) == tip.crate(finding.path) or not approved(built.proposal)
                            or (built.rename.old, built.rename.new) != (declaration.name, outside.declaration.name)):
                        continue
                    scope = built.rename.scope
                    owner = declaration.owner
                    if (isinstance(scope, UnqualifiedName)
                            or isinstance(scope, ModuleQualified) and scope.module == _module_name(finding.path)
                            or isinstance(scope, TypeQualified) and isinstance(owner, ImplOrTraitItem)
                            and (scope.type_name == owner.name or isinstance(old.implemented_trait, ImplementedTrait)
                                 and scope.type_name == old.implemented_trait.path[-1])):
                        return DeclarationGoneFromFile(finding, declaration)
            return DeclarationMoved(finding, outside.declaration, outside.path, outside.line)
        if OutsideFailure.OLD_NAME_PRESENT not in outside.reasons:
            return OutsideRenameUnresolved(finding, declaration, outside)
        if isinstance(finding.state, Filed) and finding.path in tip.blobs:
            return DeclarationGoneFromFile(finding, declaration, outside)
        return DeclarationRemoved(finding, declaration, outside)

    def _renamed_site(self, finding: HeldFinding, read: str, old_file: DeclaredFile, old: DeclaredItem,
                      after_builds: BuiltRenaming, tip: RepositoryTip, rust_files: frozenset[str], changed: frozenset[str],
                      approved: Callable[[Path], bool],
                      ) -> DeclarationRenamed | RenameAwaitingApproval | DeclaredInSeveralFiles | DeclaredInAnotherCrate | DeclarationGoneFromFile | NoRenamedSite | TraitIdentityUncertain:
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
        requirement = _trait_requirement(old, read, finding.path)
        found = self._gaining_files(finding, read, renamed, requirement, tip, rust_files, changed)
        if isinstance(found, TraitIdentityUncertain):
            return found
        # The owner's own name comes first: a built rename of another type of that name, or one that a later commit put
        # back, gives the owner a new name that the code does not use.
        if (not found and isinstance(after_builds, RenamedByBuilds)
                and (after_builds.declaration.owner != declaration.owner
                     or after_builds.trait_requirement != requirement)):
            renamed = Declaration(declaration.keyword, new_name, after_builds.declaration.owner)
            found = self._gaining_files(finding, read, renamed, after_builds.trait_requirement, tip, rust_files, changed)
            if isinstance(found, TraitIdentityUncertain):
                return found
        if len(found) > 1 or any(len(items) != 1 for items in found.values()):
            return DeclaredInSeveralFiles(finding, renamed, tuple(sorted(found)))
        if not found:
            return NoRenamedSite()
        path, items = next(iter(found.items()))
        item, = items
        if tip.crate(path) != tip.crate(finding.path):
            return DeclaredInAnotherCrate(finding, renamed, path)
        if isinstance(applied_check(finding.kind, finding.before, finding.after, path,
                                    lambda read_path: self._commit_file(tip.commit, read_path)), RenameNotApplied):
            return DeclarationGoneFromFile(finding, declaration)
        if not approved(state.proposal):
            return RenameAwaitingApproval(finding)
        return DeclarationRenamed(finding, declaration, renamed, path,
                                  moved_site_line(old_file, finding.line, old, self._declared_file(tip.commit, path), item))

    def _built_renaming(self, finding: HeldFinding, read: str, original: DeclaredItem, tip: RepositoryTip,
                        approved: Callable[[Path], bool], renames: tuple[BuiltRename, ...]) -> BuiltRenaming:
        """The new names that the built renames of approved decisions in the finding's crate gave its item and the impl
        or trait that holds it.

        Each name follows its renames one after another, and stops at a name with two new names or at a name it had
        before. A name keeps its old name when the finding's own rename names it. A rename names the item only when its
        scope holds the item: a module path names a module item in a file of that module, and a type path names an
        item of that type's impl or trait under any name the renames gave the type. A built `Self` path has the type
        of its own impl or trait.
        """
        declaration = original.declaration
        requirement = _trait_requirement(original, read, finding.path)
        own = _item_rename(finding.kind, finding.before, finding.after)
        crate = tip.crate(finding.path)
        module = _module_name(finding.path)
        # The built finding can sit at a call site in any file of the crate; the gaining-file search still guards it.
        in_crate = tuple(built_rename for built_rename in renames if tip.crate(built_rename.path) == crate)

        uncertain_traits: list[BuiltRename] = []
        examined: set[BuiltRename] = set()

        def renamed(name: str, holds_item: Callable[[BuiltRename], bool]) -> tuple[str, ...]:
            """name, then each new name the renames gave it in turn, up to a name with two new names or a name the chain
            already holds; name alone when the finding's own rename names it."""
            names = (name,)
            questions = tuple(built for built in in_crate if built.rename.old == name)
            questions = tuple(built for built in questions if holds_item(built))
            examined.update(questions)
            if isinstance(own, ItemRename) and own.old == name:
                return names
            while questions:
                new = {built.rename.new for built in questions if approved(built.proposal)}
                if not new or len(new) > 1 or not new.isdisjoint(names):
                    return names
                names = (*names, *new)
                questions = tuple(built for built in in_crate if built.rename.old == names[-1])
                questions = tuple(built for built in questions if holds_item(built))
                examined.update(questions)
            return names

        def module_item(built: BuiltRename) -> bool:
            scope = built.rename.scope
            return isinstance(scope, UnqualifiedName) or (isinstance(scope, ModuleQualified) and scope.module == module)

        owner = declaration.owner
        if not isinstance(owner, ImplOrTraitItem):
            names = renamed(declaration.name, module_item)
            if len(names) == 1:
                return RefusedBuiltRename() if examined else NoBuiltRename()
            return RenamedByBuilds(Declaration(declaration.keyword, names[-1], owner), requirement)
        # The impl's type and its implemented trait have separate name histories.
        def owner_item(built: BuiltRename) -> bool:
            scope = built.rename.scope
            if module_item(built):
                return True
            if not isinstance(scope, ModuleQualified):
                return False
            return any(tip.crate(path) == crate and _module_name(path) == scope.module
                       and any(item.declaration.name == owner.name
                               and item.declaration.keyword in ("struct", "enum", "union", "trait", "type")
                               and not isinstance(item.declaration.owner, ImplOrTraitItem)
                               for item in self._declared_file(read, path).declared_items)
                       for path in self.files_naming(read, owner.name))

        owners = renamed(owner.name, owner_item)
        traits: tuple[str, ...] = ()
        membership = original.implemented_trait
        if isinstance(membership, ImplementedTrait):
            trait_name = membership.path[-1]

            references = {trait_name: RequiredTraitMembership(membership, read, finding.path)}

            def trait_matches(built: BuiltRename, path: tuple[str, ...]) -> bool:
                reference = references.get(path[-1], references[trait_name])
                trait = reference.implemented_trait
                if not isinstance(trait, ImplementedTrait):
                    return False
                identity = self._trait_rename_identity(trait, reference.commit, reference.path, built, path)
                if identity == TraitIdentity.UNCERTAIN and path == (trait.path[-1],) and path[-1] != trait_name:
                    resolved = self._trait_import_path(built.before_commit, built.before_path, path, built.before_position)
                    # A proved rename grants its new name before a later build declares it. Imports still decide
                    # that name first, and another declared trait prevents this chain from proving identity.
                    declared = any(
                        item.declaration.keyword == "trait" and item.declaration.name == path[-1]
                        and not isinstance(item.declaration.owner, ImplOrTraitItem)
                        for candidate in self.files_naming(built.before_commit, path[-1])
                        if candidate in tip.rust_files and tip.crate(candidate) == crate
                        for item in self._declared_file(built.before_commit, candidate).declared_items)
                    if isinstance(resolved, ResolvedTraitPath) and resolved.path == path and not declared:
                        identity = TraitIdentity.SAME
                if identity == TraitIdentity.UNCERTAIN:
                    uncertain_traits.append(built)
                return identity == TraitIdentity.SAME

            def trait_item(built: BuiltRename) -> bool:
                scope = built.rename.scope
                if isinstance(scope, UnqualifiedName):
                    matches = trait_matches(built, (built.rename.old,))
                else:
                    matches = (isinstance(scope, ModuleQualified)
                               and trait_matches(built, (scope.module, built.rename.old)))
                if matches:
                    reference = references[built.rename.old]
                    trait = reference.implemented_trait
                    if isinstance(trait, ImplementedTrait):
                        # The before name belongs to the read commit; the new name belongs to the build commit.
                        references[built.rename.new] = RequiredTraitMembership(
                            ImplementedTrait((*trait.path[:-1], built.rename.new), trait.arguments, built.position), built.commit, built.path)
                return matches

            traits = renamed(trait_name, trait_item)
            requirement = references[traits[-1]]

        def impl_or_trait_item(built: BuiltRename) -> bool:
            scope = built.rename.scope
            if isinstance(scope, UnqualifiedName):
                return True
            if not isinstance(scope, TypeQualified):
                return False
            if scope.type_name in traits and isinstance(membership, ImplementedTrait):
                return trait_matches(built, (scope.type_name,))
            return scope.type_name in owners

        names = renamed(declaration.name, impl_or_trait_item)
        if uncertain_traits:
            # Uncertain authority matters only when a destination could have gained the renamed item.
            for built in uncertain_traits:
                scope = built.rename.scope
                candidate = (Declaration(declaration.keyword, built.rename.new, ImplOrTraitItem(owners[-1]))
                             if isinstance(scope, TypeQualified) else
                             Declaration(declaration.keyword, names[-1], ImplOrTraitItem(owners[-1])))
                if self._gaining_files(finding, read, candidate, UnrestrictedTraitMembership(), tip,
                                       frozenset(tip.rust_files), self._changed_since(read, tip.commit)):
                    return TraitIdentityUncertain(finding, declaration)
            # No renamed destination exists. Keep the established plain-deletion result for this trait question.
            return RefusedBuiltRename()
        if len(names) == 1 and len(owners) == 1 and len(traits) <= 1:
            return RefusedBuiltRename() if examined else NoBuiltRename()
        return RenamedByBuilds(Declaration(declaration.keyword, names[-1], ImplOrTraitItem(owners[-1])), requirement)

    def _followed_rename(self, finding: HeldFinding, read: str, old_file: DeclaredFile, old: DeclaredItem,
                         after_builds: RenamedByBuilds, tip: RepositoryTip, rust_files: frozenset[str], changed: frozenset[str],
                         ) -> (DeclarationMoved | StatementNotFollowed | DeclaredInSeveralFiles | DeclaredInAnotherCrate | NoRenamedSite
                               | TraitIdentityUncertain):
        """Where the finding's item is declared at the tip under the new names that the built renames gave it.

        Several gaining files or a filed finding's file in another crate leave the finding, as a move does.
        """
        renamed = after_builds.declaration
        found = self._gaining_files(finding, read, renamed, after_builds.trait_requirement, tip, rust_files, changed)
        if isinstance(found, TraitIdentityUncertain):
            return found
        if len(found) > 1 or any(len(items) != 1 for items in found.values()):
            return DeclaredInSeveralFiles(finding, renamed, tuple(sorted(found)))
        if not found:
            return NoRenamedSite()
        path, items = next(iter(found.items()))
        item, = items
        if isinstance(finding.state, Filed) and tip.crate(path) != tip.crate(finding.path):
            return DeclaredInAnotherCrate(finding, renamed, path)
        return self._moved_site(finding, old_file, old, renamed, tip, path, item)

    def _moved_site(self, finding: HeldFinding, old_file: DeclaredFile, old: DeclaredItem, declaration: Declaration,
                    tip: RepositoryTip, path: str, item: DeclaredItem) -> DeclarationMoved | StatementNotFollowed:
        """The finding's site in item, the one declaration of path at the tip that its old item moved to.

        A statement line of a function follows only to the one statement line of the function there that holds the
        finding's text; another site follows as moved_site_line reads it.
        """
        destination = self._declared_file(tip.commit, path)
        if old_file.site_kind(old, finding.line) != declarations.DeclarationSite.STATEMENT:
            return DeclarationMoved(finding, declaration, path, moved_site_line(old_file, finding.line, old, destination, item))
        site = declarations.statement_site(destination, item, finding.text)
        if isinstance(site, declarations.StatementRefusal):
            return StatementNotFollowed(finding, declaration, path, site)
        return DeclarationMoved(finding, declaration, path, site)

    def _gaining_files(self, finding: HeldFinding, read: str, renamed: Declaration, requirement: TraitRequirement,
                       tip: RepositoryTip, rust_files: frozenset[str], changed: frozenset[str],
                       ) -> dict[str, tuple[DeclaredItem, ...]] | TraitIdentityUncertain:
        """Find gained members; an uncertain source or destination keeps the finding."""
        found: dict[str, tuple[DeclaredItem, ...]] = {}
        for path in (finding.path, *self.files_naming(tip.commit, renamed.name)):
            if path != finding.path and (path not in rust_files or path not in changed):
                continue
            prior = self._matching_items(finding, renamed, requirement, read, path)
            current = self._matching_items(finding, renamed, requirement, tip.commit, path)
            if isinstance(prior, TraitIdentityUncertain):
                return prior
            if isinstance(current, TraitIdentityUncertain):
                return current
            if current and not prior:
                if path == finding.path:
                    return {path: current}
                found[path] = current
        return found

    def _built_renames(self, tip: RepositoryTip) -> BuiltRenameEvidence:
        self._refresh_site_rows()
        if tip.commit in self._built_rename_evidence:
            return self._built_rename_evidence[tip.commit]
        built_renames: list[BuiltRename] = []
        built_findings = self.held_findings(HeldFindingsInState("built"))
        for finding in built_findings:
            rename = _item_rename(finding.kind, finding.before, finding.after)
            if isinstance(finding.state, Built) and isinstance(rename, ItemRename):
                judged = self.judged_commit(finding)
                if isinstance(judged, NoJudgedCommit):
                    continue
                read = JudgedCommit(finding.follow.commit) if isinstance(finding.follow, FollowedDeclaration) else judged
                before_path = finding.follow.from_path if isinstance(finding.follow, FollowedDeclaration) else finding.path
                if isinstance(rename.scope, SelfQualified):
                    item = self._declared_file(read.commit, finding.path).declaration_at(finding.line)
                    if not isinstance(item, DeclaredItem) or not isinstance(item.declaration.owner, ImplOrTraitItem):
                        continue
                    rename = ItemRename(rename.old, rename.new, TypeQualified(item.declaration.owner.name))
                before_line = finding.follow.from_line if isinstance(finding.follow, FollowedDeclaration) else finding.line
                built_renames.append(BuiltRename(
                    rename, finding.path, read.commit, before_path, judged.commit, finding.state.proposal,
                    self._trait_site_position(judged.commit, before_path, before_line, finding.before),
                    self._trait_site_position(read.commit, finding.path, finding.line, finding.after)))
        evidence = BuiltRenameEvidence(built_findings, tuple(built_renames))
        self._built_rename_evidence[tip.commit] = evidence
        return evidence

    def _declaration_reading(self, finding_ids: tuple[str, ...], tip: RepositoryTip,
                             approved: Callable[[Path], bool]) -> DeclarationReading:
        self._prepare_sites(tuple(self._held(finding_id) for finding_id in finding_ids), tip)
        rust_files = frozenset(tip.rust_files)
        built = self._built_renames(tip)
        self._refresh_site_rows()
        outcomes = tuple(self._declaration_outcome(self._held(finding_id), tip, rust_files, approved, built.renames,
                                                   built.findings) for finding_id in finding_ids)
        for outcome in outcomes:
            finding = outcome.finding
            if isinstance(outcome, FindingSite):
                evidence = outcome.evidence
            elif isinstance(outcome, DeclarationMoved):
                evidence = UnchangedSite(outcome.path, outcome.line)
            elif isinstance(outcome, DeclarationRemoved):
                evidence = NeedsJudgment("no source site after proved declaration removal")
            elif isinstance(outcome, DeclarationNotRead) and isinstance(self.judged_commit(finding), NoJudgedCommit):
                # Existing untracked findings are eligible for filing; this is not a new carry.
                evidence = UnchangedSite(finding.path, finding.line)
            elif isinstance(outcome, (DeclarationInPlace, DeclarationNotRead)):
                evidence = self._site_proof(finding, tip.commit)
            else:
                evidence = NeedsJudgment("existing declaration proof refused this transition")
            self._follow_evidence[(finding, tip.commit)] = evidence
        return DeclarationReading(tip.commit, outcomes)

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
            sites = [item for item in plan.outcomes if isinstance(item, FindingSite)]
            follows = [(item.finding, item.evidence) for item in sites if isinstance(item.evidence, UnchangedSite)]
            renames = [item for item in plan.outcomes if isinstance(item, DeclarationRenamed)]
            removals = [item for item in plan.outcomes if isinstance(item, DeclarationRemoved)]
            if not moves and not renames and not removals and not follows:
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
                     for item in moves] + [(site.path, site.line, finding.path, finding.line, tip.commit, now, finding.finding_id)
                                         for finding, site in follows],
                )
                # A person's reading was of an old build, so the new build clears it. A rename follows to its new
                # declaration, in its own file too, so reopen checks that site and its line reads at the tip.
                _ = self._connection.executemany(
                    "UPDATE held_findings SET state='built',person_read_by='',path=?,line=?,followed_from=?,followed_from_line=?,"
                    + "followed_at=?,built_at=CASE WHEN built_at='' THEN ? ELSE built_at END,updated_at=? WHERE finding_id=?",
                    [(item.path, item.line, item.finding.path, item.finding.line, tip.commit, now, now, item.finding.finding_id)
                     for item in renames],
                )
                _ = self._connection.executemany(
                    "UPDATE held_findings SET state='withdrawn',declaration_gone_at=?,updated_at=? WHERE finding_id=?",
                    [(tip.commit, now, item.finding.finding_id) for item in removals],
                )
                self._check_ticket_pipelines()
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
                now = _now()
                _ = self._connection.execute("UPDATE held_findings SET state='built',person_read_by='',built_at=CASE WHEN built_at='' THEN ? ELSE built_at END,updated_at=? WHERE finding_id=?",
                                             (now, now, finding_id))

        return retry_on_change(attempt)

    def mark_built_at_merge(self, names: tuple[str, ...], merge: str, reading: PersonReading) -> tuple[BuiltAtMerge, ...]:
        """Mark filed findings built after a plain merge put their change in the code, with no build ticket.

        Each name is a finding id or a unique prefix of one. The merge must be on the synced branch and must not be
        older than the commit each finding was judged at. It must contain the checkpoint of each ticket that built or
        holds a finding, except a dropped ticket. The applied check runs at the merge. A rename that is not applied is
        refused. A finding that the check cannot prove needs a person's reading, and the row keeps it; a proven rename
        clears it. Every refusal comes before the write.
        The git work runs before the write lock. If the synced tip or a row that decides a finding changes before the
        lock, it starts again.
        """
        try:
            commit = _git(self.repo, "rev-parse", "--verify", f"{merge}^{{commit}}")
        except CheckCouldNotRun as error:
            raise LedgerError(f"{merge}: no such commit") from error

        def attempt() -> tuple[BuiltAtMerge, ...]:
            tip = self.synced_commit()
            if not self._is_ancestor(commit, tip):
                raise LedgerError(f"{merge} is not on the synced branch at {tip[:7]}")
            identities = self._held_identities()
            finding_ids = sorted({self._named_finding(name, identities).finding_id for name in names})
            read = {finding_id: self._built_at_merge_rows(finding_id) for finding_id in finding_ids}
            for rows in read.values():
                if not isinstance(rows.finding.state, Filed):
                    raise LedgerError(f"{rows.finding.finding_id}: cannot mark a "
                                      + f"{held_state_name(rows.finding.state)} finding built")
                rows.refuse_open_build_ticket()
            built: list[BuiltAtMerge] = []
            for finding_id, rows in read.items():
                finding = rows.finding
                # A merge older than the finding's judgment cannot hold its change.
                judged = self._judged_from(rows.judging)
                if isinstance(judged, JudgedCommit) and judged.commit != commit and self._is_ancestor(commit, judged.commit):
                    raise LedgerError(f"{finding_id}: {merge} is older than {judged.commit[:7]}, where the finding was judged")
                self._refuse_merge_without_checkpoints(finding_id, rows.linked, commit, merge)
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
                if self.synced_commit() != tip or any(self._built_at_merge_rows(finding_id) != rows
                                                      for finding_id, rows in read.items()):
                    raise ChangedBeforeLock(f"{', '.join(finding_ids)}: a finding or its tickets changed "
                                            + "while it was marked built; rerun")
                now = _now()
                for item in built:
                    by = item.reading.by if isinstance(item.reading, ReadByPerson) else ""
                    _ = self._connection.execute(
                        "UPDATE held_findings SET state='built',person_read_by=?,built_at=CASE WHEN built_at='' THEN ? ELSE built_at END,updated_at=? WHERE finding_id=?",
                        (by, now, now, item.finding_id),
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
            _refuse_open_ticket(finding.finding_id, ticket["id"], ticket["state"])

    def _linked_tickets(self, finding: HeldFinding) -> tuple[LinkedTicket, ...]:
        """The tickets that built or hold the finding: its build ticket and each ticket whose finding ids list it."""
        build_ticket = finding.build_ticket.ticket if isinstance(finding.build_ticket, BuildTicket) else ""
        return tuple(cast(list[LinkedTicket], self._connection.execute(
            "SELECT id,state,checkpoint FROM tickets "
            + "WHERE id=? OR EXISTS(SELECT 1 FROM json_each(finding_ids) WHERE value=?) ORDER BY id",
            (build_ticket, finding.finding_id),
        ).fetchall()))

    def _built_at_merge_rows(self, finding_id: str) -> BuiltAtMergeRows:
        """The finding's held row, its linked tickets and its judging ticket, as they are now."""
        finding = self._held(finding_id)
        return BuiltAtMergeRows(finding, self._linked_tickets(finding), self._judging_ticket(finding.ticket))

    def _refuse_merge_without_checkpoints(self, finding_id: str, linked: tuple[LinkedTicket, ...], commit: str,
                                          merge: str) -> None:
        """Refuse a merge that does not contain the checkpoint of a linked ticket of the finding.

        A dropped ticket's checkpoint is not merged work, so it does not count.
        """
        for ticket, state, checkpoint in linked:
            if state != "dropped" and checkpoint and not self._is_ancestor(checkpoint, commit):
                raise LedgerError(f"{finding_id}: {merge} does not contain {ticket}'s checkpoint {checkpoint[:7]}")

    def named_held(self, names: Iterable[str]) -> tuple[HeldFinding, ...]:
        """The held findings that the names name, each once and in finding-id order; refuses a name that is not one."""
        identities = self._held_identities()
        findings = {finding.finding_id: finding for finding in (self._named_finding(name, identities) for name in names)}
        return tuple(findings[finding_id] for finding_id in sorted(findings))

    def _restore_held_findings(self, findings: tuple[HeldFinding, ...], detail: str) -> None:
        """Restore findings and newest waived cell judgments under the caller's write lock."""
        tip = self._synced_tip()
        cells = {(finding.rule, _finding_cell(self._component_rule(finding.rule), finding.path, tip)) for finding in findings}
        now = _now()
        _ = self._connection.executemany(
            "UPDATE held_findings SET state='unfiled',proposal='',build_ticket='',moved_from='',built_at='',updated_at=? WHERE finding_id=?",
            [(now, finding.finding_id) for finding in findings],
        )
        for rule, path in sorted(cells):
            newest = cast(list[tuple[int, Verdict]], self._connection.execute(
                "SELECT id,verdict FROM active_judgments WHERE rule=? AND path=? ORDER BY recorded_at DESC,id DESC LIMIT 1", (rule, path),
            ).fetchall())
            if newest and newest[0][1] == "waived":
                _ = self._connection.execute("UPDATE judgments SET verdict='held',verdict_detail=?,recorded_at=? WHERE id=?",
                                             (detail, now, newest[0][0]))

    def replace_held(self, finding_id: str, after: str, person: WaiveUndo = NO_WAIVE_UNDO) -> HeldFinding:
        """Correct a waived finding and hold it again, with its id and judging ticket."""
        with self._connection:
            _ = self._connection.execute("BEGIN IMMEDIATE")
            finding = self._held(finding_id)
            if not isinstance(finding.state, Waived):
                raise LedgerError(f"{finding_id}: cannot replace a {held_state_name(finding.state)} finding")
            if finding.waive_provenance == WaiveProvenance.LEDGER:
                raise LedgerError(f"{finding_id}: cannot replace a ledger waive")
            if finding.waive_provenance == WaiveProvenance.PERSON:
                raise LedgerError(f"{finding_id}: cannot replace a person's waived finding; use the person-attributed unwaive path")
            if isinstance(person, WaiveUndoneBy) and not person.by.strip():
                raise LedgerError(f"{finding_id}: held replace --person requires a name")
            if finding.waive_provenance == WaiveProvenance.UNRECORDED and isinstance(person, NoWaiveUndo):
                raise LedgerError(f"{finding_id}: unrecorded waive requires held replace --person <name>")
            detail = f"replaced waived finding {finding_id}"
            if isinstance(person, WaiveUndoneBy):
                detail += f"; unwaived by {person.by}"
            self._restore_held_findings((finding,), detail)
            _ = self._connection.execute("UPDATE held_findings SET after=?,person_read_by='' WHERE finding_id=?", (after, finding_id))
            if isinstance(person, WaiveUndoneBy):
                _ = self._connection.execute("UPDATE held_findings SET unwaived_by=? WHERE finding_id=?", (person.by, finding_id))
            self._check_ticket_pipelines()
        return self._held(finding_id)

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
            self._catalog = self._load_rules()
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
            self._restore_held_findings(findings, f"unwaived by {person.by}")
            _ = self._connection.executemany(
                "UPDATE held_findings SET unwaived_by=? WHERE finding_id=?",
                [(person.by, finding.finding_id) for finding in findings],
            )
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
                    "UPDATE held_findings SET state='filed',build_ticket='',built_at='',updated_at=? WHERE finding_id=?", (now, finding_id),
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

    def waive_held(self, finding_id: str, actor: WaiveActor) -> None:
        with self._connection:
            _ = self._connection.execute("BEGIN IMMEDIATE")
            finding = self._held(finding_id)
            provenance, by = _waive_actor_record(actor)
            if isinstance(actor, WaiveByUnit) and actor.ticket != finding.ticket:
                raise LedgerError(f"{finding_id}: ticket {actor.ticket} did not judge this finding; expected {finding.ticket}")
            # A unit cannot relabel an existing waive. A person's explicit waive protects it from replacement.
            if isinstance(finding.state, Withdrawn) or isinstance(finding.state, Waived) and isinstance(actor, WaiveByUnit):
                return
            _ = self._connection.execute("UPDATE held_findings SET state='waived',built_at='',waive_provenance=?,waived_by=?,updated_at=? WHERE finding_id=?",
                                         (provenance, by, _now(), finding_id))

    def reconcile(self, branch_point: str, style_branch: str) -> tuple[str, ...]:
        style_branch = "refs/heads/" + style_branch.removeprefix("refs/heads/")
        with self.git_operation(f"reconcile {style_branch}"):
            branch_tip = pushed_branch_point_tip(self.repo, branch_point)
            present = _git_bytes(self.repo, "show-ref", "--verify", "--quiet", style_branch, answers=GitExitAnswers(frozenset({0, 1})))
            style_tip = branch_tip if present.returncode == 1 else _git(self.repo, "rev-parse", "--verify", f"{style_branch}^{{commit}}")
        changes: list[str] = []
        sync = cast(list[tuple[str, str]], self._connection.execute("SELECT tip,style_branch FROM sync WHERE id=1").fetchall())
        if not sync or sync[0][1] != style_branch:
            with self.git_operation(f"reconcile {style_branch}"):
                self.set_tip(style_tip, style_branch)
            changes.append(f"style branch {style_branch}")
        with self._connection:
            _ = self._connection.execute("INSERT INTO reconciliation VALUES (1,?) ON CONFLICT(id) DO UPDATE SET branch_point=excluded.branch_point", (branch_point,))
        with self.git_operation(f"reconcile {style_branch}"):
            for handoff in self.pending_handoffs():
                checkpoint = handoff["checkpoint"]
                if self._is_ancestor(checkpoint, branch_tip):
                    with self._connection:
                        _ = self._connection.execute("UPDATE handoffs SET state='merged',merge_commit=?,updated_at=? WHERE checkpoint=?",
                                                     (self._first_merge(checkpoint, branch_point), _now(), checkpoint))
                    changes.append(f"hand-off {checkpoint}: {handoff['state']} -> merged")
        tickets = cast(list[tuple[str, str, str, str]], self._connection.execute(
            "SELECT id,branch,state,checkpoint FROM tickets WHERE state IN ('assigned','checkpointed') "
            + "ORDER BY EXISTS(SELECT 1 FROM ticket_predecessors p JOIN tickets prev ON prev.id=p.predecessor "
            + "WHERE p.ticket=tickets.id AND prev.state='checkpointed'),unit,length(id),id",
        ).fetchall())
        for id, branch, state, checkpoint in tickets:
            with self.git_operation(f"ticket {id} reconcile"):
                contained = state == "checkpointed" and self._is_ancestor(checkpoint, style_tip)
                if contained:
                    not_built = self.ticket_merge(id)
                    changes.append(f"ticket {id}: checkpointed -> merged")
                    changes.extend(not_built_line(self.repo, item) for item in not_built)
                elif state == "assigned":
                    result = _git_bytes(self.repo, "show-ref", "--verify", "--quiet", branch, answers=GitExitAnswers(frozenset({0, 1})))
                    if result.returncode == 1:
                        self.ticket_drop(id)
                        changes.append(f"ticket {id}: assigned -> dropped")
        # Final tickets keep their reservation records until release is confirmed, also across cancellation or a crash.
        held = cast(list[tuple[str]], self._connection.execute(
            "SELECT id FROM tickets WHERE state IN ('merged','dropped') AND reservation_session_dir!='' ORDER BY id").fetchall())
        for (id,) in held:
            with self.git_operation(f"ticket {id} reconcile"):
                release = self.release_held_reservation(id)
            if release is not None:
                changes.append(release.line)
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
        self._catalog = self._load_rules()
        tip = self._synced_tip()
        yielded = {yielded["path"] for yielded in ticket["yielded"]}
        cells: list[ProposalCell] = []
        for rule in ticket["rules"]:
            try:
                definition = self._component_rule(rule)
            except GitReadFailed:
                raise
            except LedgerError:
                continue
            for path in (path for path in ticket["files"] if path not in yielded):
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
        self._catalog = self._load_rules()
        if not any(definition.rule_id == rule for definition in self._catalog):
            raise LedgerError(f"unknown rule {rule}")
        tip = self._synced_tip()
        self._refresh_sites(tip)
        return tuple(site for site in self._listed_sites(rule, tip) if crate is None or site.crate == crate)

    def unclassified_rules(self) -> tuple[str, ...]:
        self._catalog = self._load_rules()
        return tuple(rule.rule_id for rule in self._catalog if rule.tier == "unclassified")

    def _rule(self, rule_id: str) -> Rule:
        rule = next((rule for rule in self._catalog if rule.rule_id == rule_id), None)
        if rule is None:
            raise LedgerError(f"unknown rule {rule_id}")
        if rule.tier == "unclassified":
            raise LedgerError(f"unclassified rule {rule_id}")
        return rule

    def _component_rule(self, rule_id: str) -> Rule:
        definition = self._rule(base_rule(rule_id))
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

    @contextmanager
    def _record_write(self, ticket: str, rule: str, path: str, rule_blob: str) -> Generator[int]:
        """Publish any verdict only while its ticket, pin and generation remain valid."""
        with self._connection:
            _ = self._connection.execute("BEGIN IMMEDIATE")
            _ = self._check_pin(ticket, rule, rule_blob)
            if self._connection.execute(
                "SELECT 1 FROM ticket_yields WHERE ticket=? AND path=?", (ticket, path),
            ).fetchone():
                raise LedgerError(f"{ticket}: {path} was yielded")
            _ = self._connection.execute(
                f"INSERT INTO rule_generations VALUES ({base_rule_sql(':rule')},:blob,0) ON CONFLICT(rule) DO NOTHING",
                {"rule": rule, "blob": rule_blob})
            generation = cast(tuple[int], self._connection.execute(
                f"SELECT generation FROM rule_generations WHERE rule={base_rule_sql(':rule')}", {"rule": rule}).fetchone())[0]
            yield generation

    def record(
        self, rule: str, path: str, commit: str, rule_blob: str, verdict: Verdict,
        detail: str = "", ticket: str = "", worker: str = "", *,
        input_scope: DeclaredInputScope | UndeclaredInputScope = UNDECLARED_INPUT_SCOPE,
        reads: Iterable[str] | NoReadFiles = NO_READ_FILES,
    ) -> None:
        if verdict == "removed":
            _ = self.record_removed(rule, path, commit, rule_blob, detail, ticket, worker, input_scope=input_scope, reads=reads)
            return
        if input_scope == DeclaredInputScope.FILE and verdict not in SCOPED_VERDICTS:
            raise LedgerError(f"{verdict}: file input scope requires clean, exception or held")
        self._catalog = self._load_rules()
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
        read_files: ReadFiles | NoReadFiles = NO_READ_FILES
        if not isinstance(reads, NoReadFiles):
            if input_scope != DeclaredInputScope.RULE:
                raise LedgerError("--reads requires --input-scope rule")
            if verdict not in SCOPED_VERDICTS:
                raise LedgerError(f"{verdict}: --reads requires clean, exception or held")
            if not file_scope_allowed:
                raise LedgerError(f"{rule}: {path}: --reads requires a crate or module cell of an included Rust file")
            read_files = ReadFiles(tuple(sorted({self._path(name) for name in reads})))
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
        fingerprint = (_read_fingerprint(tip, path, read_files) if isinstance(read_files, ReadFiles)
                       else self._fingerprint(tip, scope, path))
        with self._record_write(ticket, rule, path, rule_blob) as generation:
            if verdict == "held":
                findings = self._cell_findings(rule, path, tip.commit)
                if not any(isinstance(finding.state, (Unfiled, Filed)) for finding in findings):
                    inactive = "".join(f"\n{finding.finding_id}: {held_state_name(finding.state)}" for finding in findings)
                    raise LedgerError(
                        f"{rule}: {path}: cannot record held without a live held finding.{inactive}\n"
                        + "A waived finding is the user's decision and does not make a cell held; "
                        + "hold a new finding with held add first, or record waived when only waived findings remain.")
            _ = self._connection.execute(
                'INSERT INTO judgments(rule,rule_blob,input_scope,input_fingerprint,path,"commit",verdict,verdict_detail,ticket,worker,recorded_at,declared_scope,read_files,generation) '
                + 'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(rule,path,"commit",rule_blob,input_scope,input_fingerprint,ticket,worker) '
                + 'DO UPDATE SET verdict=excluded.verdict,verdict_detail=excluded.verdict_detail,recorded_at=excluded.recorded_at,declared_scope=excluded.declared_scope,read_files=excluded.read_files,generation=excluded.generation',
                (rule, rule_blob, scope, fingerprint, path, tip.commit, verdict, detail, ticket, worker,
                 datetime.now(timezone.utc).isoformat(), input_scope,
                 json.dumps(read_files.paths if isinstance(read_files, ReadFiles) else None), generation),
            )

    def record_removed(
        self, rule: str, path: str, commit: str, rule_blob: str, detail: str, ticket_id: str, worker: str, *,
        input_scope: DeclaredInputScope | UndeclaredInputScope = UNDECLARED_INPUT_SCOPE,
        reads: Iterable[str] | NoReadFiles = NO_READ_FILES,
    ) -> RemovedCell:
        """Close a ticket cell whose file the ticket's own commits deleted; the row keeps the commit that deleted it.

        Own commits are the first-parent commits from the ticket's start to commit, as `checkpoint_restamps` reads them.
        The deleting commit is the newest first-parent commit whose diff against its first parent deletes the path, and it
        must be an own commit that is not a merge: a merge that only brings in other work deletes nothing in that diff,
        and a delete that a merge brings in is not the ticket's. Commits of other work that changed the file before the
        delete do not matter. It refuses a ticket the ledger does not hold, a cell outside the ticket, a ticket with no
        start, a commit not under the start, a path that is not a file at the start (a crate folder included), a path
        still in the tree at commit, and a path whose newest delete is a merge or is not there. The git reads run before
        the write lock; under it, a yield of the path refuses as in record.
        """
        if input_scope == DeclaredInputScope.FILE:
            raise LedgerError("removed: file input scope requires clean, exception or held")
        if not isinstance(reads, NoReadFiles):
            raise LedgerError("removed: --reads requires clean, exception or held")
        self._catalog = self._load_rules()
        definition = self._component_rule(rule)
        rule_blob = self._check_pin(ticket_id, rule, rule_blob)
        path = self._path(path)
        ticket = self._ticket(ticket_id)
        if (rule, path) not in self._ticket_cells(ticket):
            raise LedgerError(f"{ticket_id}: {rule} {path} is outside ticket cells")
        base = ticket["base"]
        if not base:
            raise LedgerError(f"{ticket_id}: no start commit, so its own commits are not known")
        commit = _git(self.repo, "rev-parse", "--verify", f"{commit}^{{commit}}")
        if not self._is_ancestor(base, commit):
            raise LedgerError(f"{ticket_id}: {commit[:7]} is not under the ticket's start {base[:7]}")
        if not isinstance(self._file_at(base, path), bytes):
            raise LedgerError(f"{path}: not a file at the ticket's start {base[:7]}")
        if not isinstance(self._file_at(commit, path), MissingFile):
            raise LedgerError(f"{path}: still in the tree at {commit[:7]}")
        # The newest first-parent delete, then its parents: more than one parent is a merge.
        removing = _git(self.repo, "log", "--first-parent", "--diff-merges=first-parent", "--diff-filter=D", "--no-renames",
                        "--no-patch", "--format=%H %P", "-n", "1", commit, "^" + base, "--", path).split()
        if not removing:
            raise LedgerError(f"{path}: no commit of {ticket_id}'s own deletes it")
        deleting = removing[0]
        if len(removing) > 2:
            raise LedgerError(f"{path}: merge {deleting[:7]} deletes it, not a commit of {ticket_id}'s own")
        scope = _cell_scope(definition, path, self._tip(deleting))
        with self._record_write(ticket_id, rule, path, rule_blob) as generation:
            _ = self._connection.execute(
                'INSERT INTO judgments(rule,rule_blob,input_scope,input_fingerprint,path,"commit",verdict,verdict_detail,ticket,worker,recorded_at,declared_scope,read_files,generation) '
                + 'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(rule,path,"commit",rule_blob,input_scope,input_fingerprint,ticket,worker) '
                + "DO UPDATE SET verdict=excluded.verdict,verdict_detail=excluded.verdict_detail,recorded_at=excluded.recorded_at,generation=excluded.generation",
                # A removed row has no inputs, so its fingerprint is empty and no tip matches it.
                (rule, rule_blob, scope, "", path, deleting, "removed", detail, ticket_id, worker,
                 datetime.now(timezone.utc).isoformat(), DeclaredInputScope.RULE, json.dumps(None), generation),
            )
        return removed_cell(ticket_id, rule, path, deleting)

    def latest_held_judgments(self) -> dict[tuple[str, str], str]:
        """Return judgment commits for cells whose latest verdict is held."""
        rows = cast(list[tuple[str, str, str]], self._connection.execute(
            'SELECT j.rule,j.path,j."commit" FROM active_judgments j WHERE j.verdict=\'held\' '
            + 'AND j.id=(SELECT latest.id FROM active_judgments latest WHERE latest.rule=j.rule AND latest.path=j.path '
            + 'ORDER BY latest.recorded_at DESC,latest.id DESC LIMIT 1)',
        ).fetchall())
        return {(rule, path): commit for rule, path, commit in rows}

    def lint_record(self, tip: str, tool: LintTool, passed: bool, diagnostics: dict[str, int]) -> None:
        if any(count < 0 for count in diagnostics.values()):
            raise LedgerError("diagnostic counts must be nonnegative")
        snapshot = self._tip(tip)
        self._catalog = self._load_rules()
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
        rows = cast(list[tuple[str, str, str, Verdict, str, str, str, str]], self._connection.execute(
            'SELECT rule_blob,input_fingerprint,"commit",verdict,input_scope,declared_scope,read_files,ticket FROM active_judgments '
            + "WHERE rule=? AND path=? ORDER BY recorded_at DESC,id DESC", (rule, path),
        ).fetchall())
        return tuple(Judgment(blob, fingerprint, commit, verdict, CellInputScope(scope), DeclaredInputScope(declared), _read_files(reads), ticket)
                     for blob, fingerprint, commit, verdict, scope, declared, reads, ticket in rows)

    def _rows_after_removals(self, rows: tuple[Judgment, ...], tip: str) -> tuple[Judgment, ...]:
        """The rows, newest first, that can judge the file at the path in tip.

        A removed row in tip ends the cell: a file at the path after it is a new cell. So the removed rows in tip go, and
        so does each row whose commit is not under every one of them, whenever it was recorded. A removed row outside tip
        stays.
        """
        removals: set[str] = set()
        for row in rows:
            if row.verdict == "removed":
                try:
                    if self._is_ancestor(row.commit, tip):
                        removals.add(row.commit)
                except GitReadFailed:
                    raise
                except LedgerError:
                    pass

        def after_removals(row: Judgment) -> bool:
            if row.verdict == "removed" and row.commit in removals:
                return False
            try:
                return all(self._is_ancestor(removal, row.commit) for removal in removals)
            except GitReadFailed:
                raise
            except LedgerError:
                return False

        return tuple(row for row in rows if after_removals(row))

    def _judgment_state(self, rule: Rule, path: str, tip: RepositoryTip) -> tuple[CellState, str]:
        """The cell's state at tip, with the verdict that gives it.

        A removed row in tip ends the cell: a file at the path now is a new cell, so no row at a commit before the removal
        judges it (_rows_after_removals). A removed row only in the style branch, whose file is gone there, gives awaiting
        merge.
        """
        rows = self._rows_after_removals(self._judgments(rule.rule_id, path), tip.commit)
        scope = _cell_scope(rule, path, tip)
        pending = ""
        # Inactive generations remain history, but never supply coverage.
        judged = not rows and self._connection.execute(
            "SELECT 1 FROM judgments j WHERE j.rule=? AND j.path=? AND j.verdict!='removed' "
            + "AND NOT EXISTS (SELECT 1 FROM active_judgments active WHERE active.id=j.id) LIMIT 1", (rule.rule_id, path),
        ).fetchone() is not None
        declared = next((row.declared_scope for row in rows if row.verdict != "removed"), DeclaredInputScope.RULE)
        style_branch = self._style_branch()
        for row in rows:
            if row.verdict == "removed":
                try:
                    if not pending and isinstance(style_branch, StoredStyleBranch):
                        branch_commit = self._branch_commit(style_branch.name)
                        if self._is_ancestor(row.commit, branch_commit) and path not in self._tip(branch_commit).entries:
                            pending = row.verdict
                except GitReadFailed:
                    if self._judged_commit_missing(row.commit):
                        continue
                    raise
                except LedgerError:
                    pass
                continue
            judged = True
            if row.declared_scope != declared or not rule_catalog.judged_text_unchanged(row.rule_blob, rule):
                continue
            try:
                ancestor = self._is_ancestor(row.commit, tip.commit)
            except GitReadFailed:
                if self._judged_commit_missing(row.commit):
                    continue
                raise
            except LedgerError:
                continue
            if ancestor and self._judgment_inputs_match(row, tip, scope, path, rule.family):
                return "current", row.verdict
            if not ancestor and isinstance(style_branch, StoredStyleBranch):
                try:
                    branch_commit = self._branch_commit(style_branch.name)
                    checked_tip = self._tip(branch_commit)
                    if self._is_ancestor(row.commit, branch_commit) and self._judgment_inputs_match(row, checked_tip, scope, path, rule.family):
                        if not pending:
                            pending = row.verdict
                except GitReadFailed:
                    if self._judged_commit_missing(row.commit):
                        continue
                    raise
                except LedgerError:
                    continue
        if pending:
            return "awaiting merge", pending
        return ("stale" if judged else "open"), ""

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
        except GitReadFailed:
            raise
        except LedgerError:
            return
        for cell in cells:
            if cell.state != "stale":
                continue
            rule = _judgment_rule(self._component_rule(cell.rule), cell.path)
            scope = _cell_scope(rule, cell.path, synced)
            # Rows before a removal in base judged a file that is gone.
            rows = self._rows_after_removals(self._judgments(cell.rule, cell.path), tip.commit)
            for row in rows:
                if row.verdict == "removed":
                    break
                if row.declared_scope != rows[0].declared_scope or not rule_catalog.judged_text_unchanged(row.rule_blob, rule):
                    continue
                try:
                    repeats = (self._is_ancestor(row.commit, tip.commit) and self._is_ancestor(row.commit, merged)
                               and self._judgment_inputs_match(row, tip, scope, cell.path, rule.family))
                except GitReadFailed:
                    raise
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
                        if not self._judgment_inputs_match(row, tip, scope, cell.path, cell.family))
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
        self._catalog = self._load_rules()
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
            if rule_id is not None and definition.rule_id != base_rule(rule_id):
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
                                                  and rule_catalog.judged_text_unchanged(lint_coverage.rule_blobs.get(component.rule_id, ""), component)) else "stale"
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
            rule_id = base_rule(cell.rule)
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
                if rule and row.rule_id != base_rule(rule):
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


def record_refill(repo: Path, unit: str, outcome: str) -> None:
    """Save a refill result without loading rules or changing the schema."""
    database = ledger_location(repo).database
    if not database.is_file():
        return
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=rw", uri=True,
                                timeout=BUSY_TIMEOUT_SECONDS)) as connection, connection:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='refill_results'").fetchone():
            return
        now = _now()
        _ = connection.execute(
            "INSERT INTO refill_results(unit,at,outcome) VALUES (?,?,?) "
            + "ON CONFLICT(unit) DO UPDATE SET at=excluded.at,outcome=excluded.outcome",
            (unit, now, outcome),
        )


@dataclass(frozen=True)
class RecordedSplit:
    ready: int
    changed: int
    structure: int
    other: int
    measured_at: str


@dataclass(frozen=True)
class NoSplitRecorded:
    """The unit has no completed split scan."""


def require_split_table(repo: Path) -> None:
    """Refuse an old stored schema before a split scans its backup."""
    database = ledger_location(repo).database
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True,
                                timeout=BUSY_TIMEOUT_SECONDS)) as connection:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='refill_results'").fetchone():
            raise LedgerError("ledger has no refill_results (schema below 25)")


def record_split(repo: Path, unit: str, split: RecordedSplit, *,
                 busy_timeout_seconds: float = BUSY_TIMEOUT_SECONDS) -> None:
    """Save a complete split without changing the last refill result."""
    database = ledger_location(repo).database
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=rw", uri=True,
                                timeout=busy_timeout_seconds)) as connection, connection:
        _ = connection.execute(
            "INSERT INTO refill_results(unit,at,outcome,split_at,ready,changed,structure,other) "
            + "VALUES (?,'','',?,?,?,?,?) ON CONFLICT(unit) DO UPDATE SET "
            + "split_at=excluded.split_at,ready=excluded.ready,changed=excluded.changed,"
            + "structure=excluded.structure,other=excluded.other",
            (unit, split.measured_at, split.ready, split.changed, split.structure, split.other),
        )


@contextmanager
def refill_lock(repo: Path) -> Generator[None]:
    """Serialize refill selection for units that share a ledger."""
    folder = ledger_location(repo).database.parent
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "refill.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


@contextmanager
def preview_ledger(repo: Path, *, git_cache: GitReadCache | NoGitReadCache = _NO_GIT_READ_CACHE) -> Generator[Ledger]:
    """Read and migrate a temporary copy without changing the stored ledger."""
    location = ledger_location(repo)
    with tempfile.TemporaryDirectory(prefix="style-ledger-preview-") as directory:
        database = Path(directory) / "ledger.sqlite"
        if location.database.exists():
            with (closing(sqlite3.connect(location.database.resolve().as_uri() + "?mode=ro", uri=True,
                                          timeout=BUSY_TIMEOUT_SECONDS)) as source,
                  closing(sqlite3.connect(database, timeout=BUSY_TIMEOUT_SECONDS)) as destination):
                source.backup(destination)
        with Ledger(location.repo, database, git_cache=git_cache) as ledger:
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
    reads: list[str] | NoReadFiles = NO_READ_FILES
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
    from_unit: str = ""
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
    session_dir: str = ""
    phase: str = ""
    by: str | None = None
    person: str | None = None


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
        # The judgment row is set for either verdict withdrawal.
        by = finding.state.by
        result["withdrawn_by"] = by.judgment_id if isinstance(by, (ExceptionVerdict, JudgedAgain)) else 0
        result["duplicate_of"] = by.finding_id if isinstance(by, DuplicateOf) else ""
        result["rule_changed_to"] = by.rule_blob if isinstance(by, RuleBodyChanged) else ""
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
    _ = record.add_argument("--verdict", choices=("clean", "fixed", "exception", "held", "waived", "removed"), required=True)
    _ = record.add_argument("--detail", default="")
    _ = record.add_argument("--input-scope", choices=tuple(DeclaredInputScope), type=DeclaredInputScope, default=UNDECLARED_INPUT_SCOPE)
    _ = record.add_argument("--reads", nargs="+", default=NO_READ_FILES)
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
        ("unit", ("rename",)),
        ("ticket", ("add", "assign", "checkpoint", "merge", "drop", "open", "yield")),
        ("handoff", ("add", "send", "reroute", "merged", "pending")),
        ("tip", ("seen", "done")),
        ("held", ("add", "replace", "file", "waive", "built", "list")),
    ):
        group = commands.add_parser(name)
        _ = group.add_argument("--repo", default=argparse.SUPPRESS)
        children = group.add_subparsers(dest="action", required=True)
        for action in actions:
            command = children.add_parser(action)
            _ = command.add_argument("--repo", default=argparse.SUPPRESS)
            if name == "unit":
                _ = command.add_argument("--from", dest="from_unit", required=True)
                _ = command.add_argument("--to", required=True)
            elif name == "ticket":
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
                    _ = command.add_argument("--session-dir", default="")
                    _ = command.add_argument("--phase", default="")
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
                for field in ("rule", "path", "text", "rule-blob", "ticket"):
                    _ = command.add_argument(f"--{field}", required=True)
                _ = command.add_argument("--before", required=True, help="The whole span to replace. " + FINDING_REPLACEMENT_GUIDANCE)
                _ = command.add_argument("--after", required=True, help=FINDING_REPLACEMENT_GUIDANCE)
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
                elif action == "replace":
                    _ = command.add_argument("--after", required=True)
                    _ = command.add_argument("--person", default=None)
                elif action == "waive":
                    actor = command.add_mutually_exclusive_group(required=True)
                    _ = actor.add_argument("--ticket", default="")
                    _ = actor.add_argument("--person", default=None)
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
    if args.command == "ticket" and args.action == "checkpoint" and bool(args.session_dir) != bool(args.phase):
        parser.error("ticket checkpoint takes --session-dir and --phase together")
    try:
        with open_ledger(Path(args.repo)) as ledger:
            if args.command == "unit":
                renamed = ledger.rename_unit(args.from_unit, args.to)
                print(f"moved {renamed.tickets} tickets and {renamed.waits} sync waits")
            elif args.command == "set-tip":
                ledger.set_tip(args.tip, args.style_branch)
            elif args.command == "record" and args.verdict == "removed":
                print(removed_line(ledger.record_removed(args.rule, args.path, args.commit, args.rule_blob, args.detail, args.ticket,
                                                         args.worker, input_scope=args.input_scope, reads=args.reads)))
            elif args.command == "record":
                ledger.record(args.rule, args.path, args.commit, args.rule_blob, args.verdict, args.detail, args.ticket, args.worker,
                              input_scope=args.input_scope, reads=args.reads)
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
                    ledger.ticket_checkpoint(args.id, args.commit, args.session_dir, args.phase)
                elif args.action == "merge":
                    for item in ledger.ticket_merge(args.id):
                        print(not_built_line(ledger.repo, item))
                    release = ledger.release_held_reservation(args.id)
                    if ledger.sync_waits():
                        _wake_sync_waiters(ledger.repo, args.id)
                    if release is not None and release.still_held:
                        raise LedgerError(f"{release.line}; rerun ticket merge")
                    if release is not None:
                        print(release.line)
                elif args.action == "drop":
                    ledger.ticket_drop(args.id)
                elif args.action == "yield":
                    for finding_id in ledger.ticket_yield(args.id, args.path):
                        print(f"yielded {finding_id}: it stays filed for a later ticket")
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
                    seen = ledger.tip_seen(args.tip)
                    print("new" if isinstance(seen, UnseenTip) else "seen" if seen == "linted"
                          else f"seen ({seen}: synced with no lint run)")
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
                        except GitReadFailed:
                            raise
                        except LedgerError:
                            pass
                        else:
                            if isinstance(check, RenameNotApplied):
                                print(f"built, but not in the code at {tip[:7]}: run style_proposals.py reopen --all "
                                      + f"(issue {finding.state.proposal})", file=sys.stderr)
                elif args.action == "replace":
                    person: WaiveUndo = WaiveUndoneBy(args.person.strip()) if args.person is not None else NoWaiveUndo()
                    print(ledger.replace_held(args.id, args.after, person).finding_id)
                elif args.action == "file":
                    ledger.file_held(args.id, Path(args.proposal))
                elif args.action == "waive":
                    waive_actor: WaiveActor = WaiveByPerson(args.person.strip()) if args.person is not None else WaiveByUnit(args.ticket.strip())
                    ledger.waive_held(args.id, waive_actor)
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
