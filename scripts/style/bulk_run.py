#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Apply one approved names or values rule; test its clean workspace tree once on natedev.

Each original decision keeps a normal proposal ticket. All tickets share one checkpoint.
This command never sends messages or changes approvals.
"""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import hashlib
import importlib
import json
import re
import socket
import subprocess
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from contextlib import ExitStack
from dataclasses import asdict, dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Protocol, cast, override

from scripts.copies import copies
from scripts.style import style_sync

from scripts.style import bulk_constants as constants
from scripts.style import bulk_rename as bulk
from scripts.style import approved_renames
from scripts.style import coverage_report
from scripts.style import rules
from scripts.style import style_ledger as ledger_api
from scripts.style import style_proposals
from scripts.style import style_footprint as rust
from scripts.style.approved_renames import (BindingRename, NoBindingScope, NoMachineRenameRule, machine_rename_rule,
                              refactor_rename, rename_declaration, type_named_parameter)
from scripts.production.issues import Issue, frontmatter
from scripts.production.proposal_issue import ApprovedDecision, UnansweredDecision, decision_answer


@dataclass(frozen=True)
class CommandResult:
    code: int
    stdout: str = ""
    stderr: str = ""


class Runner(Protocol):
    def __call__(self, repo: Path, arguments: Sequence[str]) -> CommandResult: ...


def run_command(repo: Path, arguments: Sequence[str]) -> CommandResult:
    result = (bulk.cargo_output(repo, arguments) if arguments and Path(arguments[0]).name == "cargo"
              else subprocess.run(arguments, cwd=repo, capture_output=True, text=True, check=False))
    return CommandResult(result.returncode, result.stdout, result.stderr)


def git(repo: Path, *arguments: str) -> str:
    result = run_command(repo, ("git", *arguments))
    if result.code:
        raise bulk.BulkRenameError(result.stderr.strip() or "git failed: " + " ".join(arguments))
    return result.stdout.strip()


def require_base(repo: Path, tree: str) -> str:
    if git(repo, "--no-optional-locks", "status", "--porcelain", "--untracked-files=no"):
        raise bulk.BulkRenameError("workspace run requires a clean tracked tree and index")
    if not re.fullmatch(r"[0-9a-f]{40}", tree) or git(repo, "rev-parse", "HEAD^{tree}") != tree:
        raise bulk.BulkRenameError("base-tree mismatch")
    _ = git(repo, "symbolic-ref", "HEAD")
    return git(repo, "rev-parse", "HEAD")


@dataclass(frozen=True)
class OpenTicketRefusal(bulk.Refusal):
    """Selection blocked by the named open tickets, on this finding's path."""

    path: str
    ticket_ids: tuple[str, ...]


@dataclass(frozen=True)
class FreshMachineJudgment:
    """A new current-tip rename judgment after the shared semantic proof, not continuity."""

    finding_id: str
    anchor: ledger_api.FindingAnchor
    finding_identity: str
    rule_blob: str
    inputs: tuple[tuple[str, str], ...]


class JudgmentPolicy(Enum):
    PROVE_CURRENT = "prove a new judgment while planning"
    FROZEN_ONLY = "consume the plan's judgments without replacing them"


@dataclass(frozen=True)
class Selection:
    findings: tuple[ledger_api.HeldFinding, ...]
    skipped: tuple[bulk.Refusal, ...]
    judgments: tuple[FreshMachineJudgment, ...] = ()
    judgment_policy: JudgmentPolicy = JudgmentPolicy.PROVE_CURRENT
    newly_admitted: frozenset[str] = frozenset()


def approved_finding_decision(finding: ledger_api.HeldFinding, definition: rules.Rule) -> bool:
    """The single approval boundary shared by selection and fresh judgment."""
    if not isinstance(finding.state, ledger_api.Filed):
        return False
    proposal = finding.state.proposal
    answer = decision_answer(Issue(proposal, frontmatter(proposal))) if proposal.is_file() else UnansweredDecision()
    return (isinstance(answer, ApprovedDecision)
            or isinstance(definition.approval, rules.AlwaysApproved)
            and isinstance(answer, UnansweredDecision) and proposal.is_file())


def select(ledger: ledger_api.Ledger, definition: rules.Rule) -> Selection:
    """Only filed approvals are ready. An always-approved rule also accepts unanswered decisions.

    Declines are never overridden. Unfiled findings still need the normal filing step.
    Other proposal tickets' files are excluded, including other rules and families.
    File reservations additionally protect active readers and non-proposal builders.
    """
    tickets = tuple(ticket for ticket in ledger.proposal_tickets() if ticket["state"] not in ledger_api.FINAL_TICKET_STATES)
    owners = {ticket["id"] for ticket in tickets}
    busy: dict[str, set[str]] = defaultdict(set)
    for ticket in tickets:
        for path in (*ticket["files"], *(item["path"] for item in ticket["widened"])):
            busy[path].add(ticket["id"])
    approvals: dict[Path, bool] = {}
    ready: list[ledger_api.HeldFinding] = []
    skipped: list[bulk.Refusal] = []
    for finding in ledger.held_findings(ledger_api.AllHeldFindings()):
        if finding.rule.removesuffix("#uncovered") != definition.rule_id:
            continue
        reason = ""
        ticket_ids: tuple[str, ...] = ()
        if not isinstance(finding.state, ledger_api.Filed):
            reason = ledger_api.held_state_name(finding.state)
        elif isinstance(finding.build_ticket, ledger_api.BuildTicket) and finding.build_ticket.ticket in owners:
            reason = "held by another open ticket"
            ticket_ids = (finding.build_ticket.ticket,)
        elif finding.path in busy:
            reason = "file held by another open ticket"
            ticket_ids = tuple(sorted(busy[finding.path]))
        elif not rules.judged_text_unchanged(finding.rule_blob, definition):
            reason = "rule changed since judged"
        else:
            proposal = finding.state.proposal
            if proposal not in approvals:
                approvals[proposal] = approved_finding_decision(finding, definition)
            if not approvals[proposal]:
                reason = "decision not approved"
        if reason:
            skipped.append(OpenTicketRefusal(finding.finding_id, reason, finding.path, ticket_ids)
                           if ticket_ids else bulk.Refusal(finding.finding_id, reason))
        else:
            ready.append(finding)
    return Selection(tuple(ready), tuple(skipped))


def claim(ledger: ledger_api.Ledger, selected: Selection, family: str, branch: str,
          intent: bulk.BatchIntent) -> dict[str, str]:
    """Normal tickets retain original approvals and later checkpoint the same commit.

    Called under refill_lock. ticket_add rechecks exclusive finding ownership under its SQL lock.
    """
    groups: dict[str, list[ledger_api.HeldFinding]] = defaultdict(list)
    for finding in selected.findings:
        if isinstance(finding.state, ledger_api.Filed):
            groups[str(finding.state.proposal)].append(finding)
    if not groups:
        intent.begin(())
        return {}
    holder = str(ledger.bulk_holder().id)
    owned: dict[str, str] = {}
    planned: list[tuple[str, str, str, list[ledger_api.HeldFinding]]] = []
    for proposal, findings in groups.items():
        ticket = ledger.next_ticket_id()
        planned.append((ticket, holder, proposal, findings))
        owned.update((finding.finding_id, ticket) for finding in findings)
    # ticket_add commits independently: persist every planned claim before the first one.
    intent.begin((), sorted(set(owned.values())), sorted(owned))
    created: list[str] = []
    try:
        for ticket, unit, proposal, findings in planned:
            ledger.ticket_add(ticket, unit, "proposal", family, ".", sorted({f.path for f in findings}),
                              sorted({f.rule for f in findings}), proposal=proposal,
                              finding_ids=tuple(f.finding_id for f in findings),
                              batch_owner=ledger_api.BatchOwner(intent.batch_id))
            created.append(ticket)
            ledger.ticket_assign(ticket, branch)
            _ = ledger.ticket_base(ticket)
    except BaseException:
        for ticket in created:
            ledger.ticket_drop_batch(ticket, ledger_api.BatchOwner(intent.batch_id))
        raise
    return owned


class RenameSources:
    """Original judged bytes and current sources, shared for all findings in one run."""
    def __init__(self) -> None:
        self.current: dict[str, str] = {}
        self.judged: dict[tuple[str, str], CommandResult] = {}
        self.judgments: dict[str, FreshMachineJudgment] = {}

    def judged_file(self, repo: Path, commit: str, path: str) -> CommandResult:
        key = (commit, path)
        if key not in self.judged:
            # Decode only after reading: declaration line endings must remain byte-identical.
            result = subprocess.run(["git", "show", f"{commit}:{path}"], cwd=repo, capture_output=True, check=False)
            self.judged[key] = CommandResult(result.returncode, result.stdout.decode("utf-8"))
        return self.judged[key]


def current_finding(ledger: ledger_api.Ledger, finding: ledger_api.HeldFinding, head: str,
                   sources: RenameSources, checkout: Path
                   ) -> ledger_api.HeldFinding | bulk.Refusal:
    """Shared site lineage plus exact physical declaration bytes, before any edits.

    Whitespace and line terminators count. A follow is provenance, never a new judgment.
    If original coordinates cannot be recovered unambiguously, a worker must judge again.
    The ledger owns lineage; the planner's checkout owns current physical bytes.
    """
    evidence = ledger.finding_site(finding, head)
    if finding.finding_id in sources.judgments:
        judgment = sources.judgments[finding.finding_id]
        refusal = validate_machine_judgment(ledger, finding, head, checkout, judgment)
        if refusal:
            return bulk.Refusal(finding.finding_id, refusal)
        evidence = judgment
    if not isinstance(evidence, (ledger_api.UnchangedSite, FreshMachineJudgment)):
        return bulk.Refusal(finding.finding_id, "changed since judged: " + evidence.reason)
    current_path = evidence.anchor.path if isinstance(evidence, FreshMachineJudgment) else evidence.path
    current_line = evidence.anchor.line if isinstance(evidence, FreshMachineJudgment) else evidence.line
    path = checkout / current_path
    if path.resolve() != path or not path.is_file() or path.suffix != ".rs":
        return bulk.Refusal(finding.finding_id, "finding path is not a regular Rust file")
    if current_path not in sources.current:
        sources.current[current_path] = path.read_bytes().decode("utf-8")
    source = sources.current[current_path]
    old = sources.judged_file(ledger.repo, evidence.anchor.commit, evidence.anchor.path)
    if old.code:
        return bulk.Refusal(finding.finding_id, "changed since judged: the file cannot be read at the judged commit")
    old_lines = old.stdout.encode().splitlines(keepends=True)
    new_lines = source.encode().splitlines(keepends=True)
    old_line = evidence.anchor.line
    if (not 0 < old_line <= len(old_lines) or not 0 < current_line <= len(new_lines)
            or old_lines[old_line - 1] != new_lines[current_line - 1]):
        return bulk.Refusal(finding.finding_id, "changed since judged: the judged line bytes differ from the current line")
    return replace(finding, path=current_path, line=current_line, text=source.splitlines()[current_line - 1])


def current_rename(ledger: ledger_api.Ledger, finding: ledger_api.HeldFinding, head: str,
                   sources: RenameSources, checkout: Path
                   ) -> bulk.RenameFinding | bulk.Refusal:
    current = current_finding(ledger, finding, head, sources, checkout)
    if isinstance(current, bulk.Refusal):
        return current
    source = sources.current[current.path]
    pair = refactor_rename(finding.kind, finding.before, finding.after, current.line, source.splitlines()[current.line - 1])
    if not isinstance(pair, BindingRename):
        return bulk.Refusal(finding.finding_id, "finding does not specify one identifier rename")
    scope = rename_declaration(source, pair)
    if isinstance(scope, NoBindingScope):
        return bulk.Refusal(finding.finding_id, scope.reason)
    if source.count("\n", 0, scope.offset) + 1 != current.line:
        return bulk.Refusal(finding.finding_id, "changed since judged: the declaration is not on the current line")
    authority = ledger.rename_authority(finding)
    if finding.finding_id in sources.judgments:
        judgment = sources.judgments[finding.finding_id]
        authority = approved_renames.AnchoredRenameSource(judgment.anchor.commit, current.path, current.line, source)
    return bulk.RenameFinding(finding.finding_id, current.path, pair, authority, finding.finding_id + ":" + ledger.finding_input_identity(finding))


AppliedFinding = bulk.KeptRename | constants.KeptConstant


@dataclass(frozen=True)
class ConstantFileEdit:
    path: str
    original: str
    edited: str
    kept: tuple[constants.KeptConstant, ...]
    dependencies: frozenset[str]
    created: bool


@dataclass(frozen=True)
class ConstantTargetProof:
    sources: constants.CrateSources
    result: constants.CrateConstants


@dataclass(frozen=True)
class NotCheckedFinding:
    """Admission requires a proof service that cannot promise read-only operation."""
    finding_id: str
    reason: str


@dataclass(frozen=True)
class PlannedEdits:
    files: tuple[bulk.FileRenames | ConstantFileEdit, ...]
    refusals: tuple[bulk.Refusal, ...]
    constant_proofs: tuple[ConstantTargetProof, ...] = ()
    judgments: tuple[FreshMachineJudgment, ...] = ()
    pending: tuple[bulk.PendingFinding, ...] = ()
    not_checked: tuple[NotCheckedFinding, ...] = ()


def require_ready_plan(plan: PlannedEdits) -> None:
    """Never execute or rebuild an incomplete atomic batch."""
    if plan.not_checked:
        raise bulk.BulkRenameError("admission not checked: " + plan.not_checked[0].reason)
    if plan.pending:
        first = plan.pending[0]
        raise bulk.ReservationBlocked(first.path, first.holders)


def require_edit_paths(repo: Path, files: tuple[bulk.FileRenames | ConstantFileEdit, ...]) -> None:
    """Check filesystem destinations immediately before every initial or rebuilt write."""
    for file in files:
        target = repo / file.path
        if target.is_symlink() or target.resolve() != target:
            raise bulk.BulkRenameError("edit path is a symlink or resolves outside its workspace path: " + file.path)
        if isinstance(file, ConstantFileEdit) and file.created and target.exists():
            raise bulk.BulkRenameError("constant creation would overwrite an untracked file: " + file.path)


class WorkspaceIntent(bulk.BatchIntent):
    """The shared batch journal, extended with exact bytes for newly created files."""

    def record_plan(self, plan: PlannedEdits) -> None:
        # BatchIntent's existing-file journal needs only path and edited bytes.
        record = bulk.object_fields(cast(object, json.loads(self.path.read_text())))
        existing = bulk.object_fields(record["files"])
        current = self.file_edits(tuple(bulk.FileRenames(file.path, file.original, file.edited, (), ())
                                       for file in plan.files if not isinstance(file, ConstantFileEdit) or not file.created))
        excluded = set(existing) - current.keys()
        # Keep the original bytes across every rebuilt plan.
        record["files"] = {**existing, **{name: {**bulk.object_fields(existing.get(name, edit)), "after": edit["after"]}
                                           for name, edit in current.items()}}
        record["formatter_restored_paths"] = sorted(excluded)
        record["created_files"] = {**bulk.object_fields(record.get("created_files", {})),
                                   **{file.path: file.edited.encode().hex() for file in plan.files
                                      if isinstance(file, ConstantFileEdit) and file.created}}
        bulk.atomic_json(self.path, record)

    def record_current(self) -> None:
        """Record exact guarded bytes without replacing the original recovery bytes."""
        record = bulk.object_fields(cast(object, json.loads(self.path.read_text())))
        files = bulk.object_fields(record["files"])
        for name, raw in files.items():
            edit = bulk.object_fields(raw)
            edit["after"] = (self.repo / name).read_bytes().hex()
            files[name] = edit
        record["files"] = files
        created = bulk.object_fields(record.get("created_files", {}))
        for name in created:
            path = self.repo / name
            if path.exists():
                created[name] = path.read_bytes().hex()
        record["created_files"] = created
        bulk.atomic_json(self.path, record)

    def complete_claims(self, owned: dict[str, str], selected: Selection,
                        ledger: ledger_api.Ledger) -> None:
        """Save the completed selection separately from the planned claim list."""
        record = bulk.object_fields(cast(object, json.loads(self.path.read_text())))
        record["completed_claims"] = owned
        record["finding_inputs"] = {finding.finding_id: ledger.finding_input_identity(finding)
                                    for finding in selected.findings}
        record["skipped"] = [asdict(item) for item in selected.skipped]
        bulk.atomic_json(self.path, record)

    def stop_admission(self) -> None:
        record = bulk.object_fields(cast(object, json.loads(self.path.read_text())))
        record["admission_stopped"] = True
        bulk.atomic_json(self.path, record)

    def clear_admission(self) -> None:
        """End preservation after startup or validated recovery."""
        if self.path.exists():
            record = bulk.object_fields(cast(object, json.loads(self.path.read_text())))
            record["admission_stopped"] = False
            bulk.atomic_json(self.path, record)

    def retry_eligible(self) -> bool:
        """Preserve claims only while a child admission remains unresolved."""
        if not self.path.exists():
            return False
        record = bulk.object_fields(cast(object, json.loads(self.path.read_text())))
        return record.get("admission_stopped") is True

    def restore_admission(self, stopped: BaseException) -> None:
        """Restore exact source bytes and retain the marker if recovery fails."""
        try:
            _ = self.recover()
        except Exception as error:
            print(f"style admission recovery: {error}", file=sys.stderr)
            raise stopped from error

    def resume_claims(self, ledger: ledger_api.Ledger, definition: rules.Rule) -> tuple[Selection, dict[str, str]]:
        """Reuse claims only when the journal and live ownership still agree."""
        record = bulk.object_fields(cast(object, json.loads(self.path.read_text())))
        raw = bulk.object_fields(record.get("completed_claims", {}))
        if (set(raw) != set(bulk.object_list(record["findings"]))
                or set(raw.values()) != set(bulk.object_list(record["tickets"]))
                or any(not isinstance(ticket, str) for ticket in raw.values())):
            raise self.refuse("completed claims do not match the journal")
        owned = {identity: str(ticket) for identity, ticket in raw.items()}
        tickets = {ticket["id"]: ticket for ticket in ledger.proposal_tickets()}
        for ticket in set(owned.values()):
            item = tickets.get(ticket)
            if (item is None or ledger.ticket_batch_owner(ticket) != ledger_api.BatchOwner(self.batch_id)
                    or item["state"] != "assigned" or item["base"] != record["base"]
                    or item["branch"] != record["branch"] or item["family"] != definition.family
                    or set(item["finding_ids"]) != {identity for identity in owned if owned[identity] == ticket}):
                raise self.refuse("completed ticket ownership or membership changed: " + ticket)
        findings = ledger.named_held(owned)
        inputs = bulk.object_fields(record.get("finding_inputs", {}))
        if (set(inputs) != set(owned) or {finding.finding_id for finding in findings} != set(owned)
                or any(finding.build_ticket != ledger_api.BuildTicket(owned[finding.finding_id])
                       or ledger.finding_input_identity(finding) != inputs[finding.finding_id]
                       or finding.rule.removesuffix("#uncovered") != definition.rule_id
                       or not isinstance(finding.state, ledger_api.Filed)
                       or str(finding.state.proposal) != tickets[owned[finding.finding_id]]["proposal"]
                       or not approved_finding_decision(finding, definition) for finding in findings)):
            raise self.refuse("completed finding claims changed")
        skipped: list[bulk.Refusal] = []
        for raw_skip in bulk.object_list(record.get("skipped", [])):
            skip = bulk.object_fields(raw_skip)
            skipped.append(OpenTicketRefusal(str(skip["finding_id"]), str(skip["reason"]), str(skip["path"]),
                                            tuple(str(ticket) for ticket in bulk.object_list(skip["ticket_ids"])))
                           if "ticket_ids" in skip else bulk.Refusal(str(skip["finding_id"]), str(skip["reason"])))
        return Selection(findings, tuple(skipped)), owned

    def owned_creations(self) -> dict[str, bytes]:
        """Read owned paths from the durable journal, including files hidden by Git ignores."""
        created: dict[str, bytes] = {}
        if self.path.exists():
            try:
                record = bulk.object_fields(cast(object, json.loads(self.path.read_text())))
                tracked = set(self.git("ls-tree", "-r", "--name-only", "-z", "HEAD").split("\0"))
                existing = bulk.object_fields(record["files"])
                for name, raw in bulk.object_fields(record.get("created_files", {})).items():
                    path = self.repo / name
                    if (not name or name in tracked or name in existing or Path(name).is_absolute()
                            or ".." in Path(name).parts or path.is_symlink() or path.resolve() != path
                            or not isinstance(raw, str)):
                        raise ValueError("invalid created file")
                    created[name] = bytes.fromhex(raw)
            except (KeyError, ValueError, OSError) as error:
                raise self.refuse(f"cannot read created files: {error}") from error
        return created

    @override
    def recover(self) -> tuple[str, ...]:
        created = self.owned_creations()
        for name, after in created.items():
            path = self.repo / name
            if path.exists() and (not path.is_file() or path.read_bytes() != after):
                raise self.refuse(f"{name}: bytes do not match the recorded creation")
        # Validate and restore existing edits first; a rejected journal deletes nothing.
        tickets = super().recover()
        for name in created:
            (self.repo / name).unlink(missing_ok=True)
        return tickets


class WorkspaceEdits:
    """Exact approved edits; all files participating in one extraction or rename revert together."""
    def __init__(self, repo: Path, base: str, files: tuple[bulk.FileRenames, ...]) -> None:
        self.repo: Path = repo
        self.base: str = base
        self.files: dict[str, bulk.FileRenames | ConstantFileEdit] = {}
        self.dependencies: dict[str, frozenset[str]] = {}
        self.initial_untracked: set[str] = self.untracked()
        self.constant_proofs: tuple[ConstantTargetProof, ...] = ()
        self.refused_files: set[str] = set()
        self.excluded_paths: set[str] = set()
        self.set_plan(PlannedEdits(files, ()))

    def set_plan(self, plan: PlannedEdits) -> None:
        self.excluded_paths.update(self.files.keys() - {file.path for file in plan.files})
        self.excluded_paths.difference_update(file.path for file in plan.files)
        self.files = {file.path: file for file in plan.files}
        self.dependencies = {path: file.dependencies | {path} for path, file in self.files.items()}
        self.constant_proofs = plan.constant_proofs

    def untracked(self) -> set[str]:
        return set(filter(None, git(self.repo, "ls-files", "--others", "--exclude-standard", "-z").split("\0")))

    def restore(self, paths: Sequence[str]) -> None:
        existing: list[str] = []
        for path in paths:
            file = self.files.get(path)
            if isinstance(file, ConstantFileEdit) and file.created:
                _ = git(self.repo, "reset", self.base, "--", path)
                (self.repo / path).unlink(missing_ok=True)
            else:
                existing.append(path)
        if existing:
            _ = git(self.repo, "restore", "--source", self.base, "--staged", "--worktree", "--", *existing)

    def rollback(self, intent: WorkspaceIntent) -> None:
        changed = set(filter(None, git(self.repo, "diff", self.base, "--name-only", "-z").split("\0")))
        self.restore(sorted(changed | intent.owned_creations().keys()))
        for path in self.untracked() - self.initial_untracked:
            (self.repo / path).unlink()

    def refuse_format_changes(self) -> dict[str, str]:
        """Restore a failed rename's whole edit group before validating the survivors."""
        refused: dict[str, str] = {}
        for path, file in self.files.items():
            if path in self.refused_files or not isinstance(file, bulk.FileRenames):
                continue
            target = self.repo / path
            if target.is_symlink() or not target.is_file():
                raise bulk.BulkRenameError(f"verification replaced {path}")
            if bulk.file_edits_preserved(file, target.read_bytes().decode()):
                continue
            reason = f"post-format rename edit changed: {path}"
            remove = dependent_files(self.dependencies, {path}) & self.files.keys()
            self.restore(sorted(remove))
            self.refused_files.update(remove)
            refused.update((item.finding.finding_id, reason) for member in remove for item in self.files[member].kept)
        return refused

    def guard(self, kept: set[str]) -> None:
        # Retries may format restored paths again; they remain owned until the run ends.
        self.restore(sorted(self.excluded_paths))
        changed = set(filter(None, git(self.repo, "diff", self.base, "--name-only", "-z").split("\0")))
        unrelated = (changed - kept) | (self.untracked() - self.initial_untracked - kept)
        if unrelated:
            raise bulk.BulkRenameError("verification changed an unrelated file: " + ", ".join(sorted(unrelated)))
        for path in kept:
            file = self.files[path]
            target = self.repo / path
            if target.is_symlink() or not target.is_file():
                raise bulk.BulkRenameError(f"verification replaced {path}")
            if isinstance(file, bulk.FileRenames) and not bulk.file_edits_preserved(file, target.read_bytes().decode()):
                raise bulk.BulkRenameError(f"post-format rename edit changed: {path}")
        for proof in self.constant_proofs:
            paths = {edit.path for edit in proof.result.edits}
            if not paths & kept:
                continue
            if not paths <= kept:
                raise bulk.BulkRenameError("constant extraction was only partly reverted")
            edits = tuple(constants.CrateConstantEdit(edit.path, edit.original,
                          (self.repo / edit.path).read_bytes().decode(), edit.created) for edit in proof.result.edits)
            checked = constants.crate_constant_check(proof.sources, edits,
                                                     tuple(item.finding for item in proof.result.kept))
            if isinstance(checked, constants.ConstantsNotProved):
                raise bulk.BulkRenameError("post-format constant proof failed: " + checked.reason)


def reprove_rename_plan(workspace: WorkspaceEdits, kept: set[str],
                        replan: Callable[[frozenset[str]], PlannedEdits],
                        record: Callable[[PlannedEdits], None], times: dict[str, float]) -> dict[str, str]:
    """Restore prior overlays and feed only survivors through the existing planner."""
    survivors = frozenset(item.finding.finding_id for path in kept for item in workspace.files[path].kept)
    workspace.restore(sorted(workspace.files))
    started = time.monotonic()
    plan = replan(survivors) if survivors else PlannedEdits((), ())
    require_ready_plan(plan)
    times["rename"] += time.monotonic() - started
    require_edit_paths(workspace.repo, plan.files)
    workspace.refused_files.clear()
    workspace.set_plan(plan)
    record(plan)
    for file in plan.files:
        _ = (workspace.repo / file.path).write_bytes(file.edited.encode())
    return {item.finding_id: item.reason for item in plan.refusals}


_NO_RESTORED_PATHS: frozenset[str] = frozenset()


def format_rename_plan(workspace: WorkspaceEdits, runner: Runner, arguments: tuple[str, ...],
                       replan: Callable[[frozenset[str]], PlannedEdits],
                       record: Callable[[PlannedEdits], None], times: dict[str, float],
                       record_current: Callable[[], None],
                       restored_paths: frozenset[str] = _NO_RESTORED_PATHS) -> dict[str, str]:
    """Drop only edit groups, then prove surviving findings on their rebuilt overlay.

    Rebuilding all survivors through the original planner also covers a reader
    whose destination-name references reappear in a rolled-back file. Resolution
    answers are never reused. Each retry removes at least one finding.
    """
    refused: dict[str, str] = {}
    workspace.excluded_paths.update(restored_paths)
    while workspace.files:
        started = time.monotonic()
        formatted = runner(workspace.repo, arguments)
        times["fmt"] += time.monotonic() - started
        if formatted.code:
            raise bulk.BulkRenameError("workspace format failed: " + formatted.stderr.strip())
        # A package-wide formatter can revisit an excluded file on a retry.
        workspace.restore(sorted(workspace.excluded_paths))
        dropped = workspace.refuse_format_changes()
        kept = set(workspace.files) - workspace.refused_files
        workspace.guard(kept)
        record_current()
        if not dropped:
            return refused
        refused.update(dropped)
        refused.update(reprove_rename_plan(workspace, kept, replan, record, times))
    return refused


def validate_machine_judgment(ledger: ledger_api.Ledger, finding: ledger_api.HeldFinding,
                              head: str, checkout: Path, judgment: FreshMachineJudgment) -> str:
    """Frozen inputs are checked, never refreshed; the ordinary byte gate still runs."""
    if judgment.anchor.commit != head:
        return "fresh judgment tip changed"
    if judgment.finding_identity != ledger.finding_input_identity(finding):
        return "fresh judgment finding row changed"
    site = ledger.rename_judgment_site(finding, head)
    if isinstance(site, ledger_api.NeedsJudgment):
        return "fresh judgment refused: " + site.reason
    if site.anchor != judgment.anchor or finding.rule_blob != judgment.rule_blob:
        return "fresh judgment rule or coordinate changed"
    definition = next((rule for rule in rules.load_rules(checkout) if rule.rule_id == finding.rule), None)
    if definition is None or definition.blob != finding.rule_blob:
        return "fresh judgment rule changed"
    contract = machine_rename_rule(definition.blob)
    if isinstance(contract, NoMachineRenameRule):
        return "fresh judgment refused: " + contract.reason
    if not approved_finding_decision(finding, definition):
        return "fresh judgment decision not approved"
    for path, identity in judgment.inputs:
        target = checkout / path
        if target.resolve() != target or not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != identity:
            return "fresh judgment source inputs changed: " + path
    return ""


@dataclass(frozen=True)
class MachineRenameCandidate:
    """Current rule and declaration are eligible; semantic reference proof is still required."""
    anchor: ledger_api.FindingAnchor
    source: str
    rename: BindingRename


def machine_rename_candidate(ledger: ledger_api.Ledger, finding: ledger_api.HeldFinding,
                             head: str, checkout: Path) -> MachineRenameCandidate | bulk.Refusal:
    """The read-only prerequisites of a fresh judgment, shared with admission reports."""
    site = ledger.rename_judgment_site(finding, head)
    if isinstance(site, ledger_api.NeedsJudgment):
        return bulk.Refusal(finding.finding_id, "changed since judged: " + site.reason)
    definition = next((rule for rule in rules.load_rules(checkout) if rule.rule_id == finding.rule), None)
    if definition is None or definition.blob != finding.rule_blob:
        return bulk.Refusal(finding.finding_id, "fresh judgment rule changed")
    if not approved_finding_decision(finding, definition):
        return bulk.Refusal(finding.finding_id, "fresh judgment decision not approved")
    path, line = site.anchor.path, site.anchor.line
    target = checkout / path
    if target.resolve() != target or not target.is_file():
        return bulk.Refusal(finding.finding_id, "fresh judgment source is not a regular file")
    source = target.read_bytes().decode()
    committed = RenameSources().judged_file(ledger.repo, head, path)
    if committed.code or committed.stdout != source:
        return bulk.Refusal(finding.finding_id, "fresh judgment checkout differs from tip")
    lines = source.splitlines()
    if not 0 < line <= len(lines):
        return bulk.Refusal(finding.finding_id, "fresh judgment declaration location is unreadable")
    pair = refactor_rename(finding.kind, finding.before, finding.after, line, lines[line - 1])
    if not isinstance(pair, BindingRename):
        return bulk.Refusal(finding.finding_id, "finding does not specify one identifier rename")
    declaration = type_named_parameter(source, pair)
    if isinstance(declaration, NoBindingScope):
        return bulk.Refusal(finding.finding_id, "fresh judgment: " + declaration.reason)
    contract = machine_rename_rule(definition.blob)
    if isinstance(contract, NoMachineRenameRule):
        return bulk.Refusal(finding.finding_id, "fresh judgment: " + contract.reason)
    if source.count("\n", 0, declaration.offset) + 1 != line:
        return bulk.Refusal(finding.finding_id, "fresh judgment declaration location changed")
    return MachineRenameCandidate(site.anchor, source, pair)


def fresh_machine_judgment(ledger: ledger_api.Ledger, finding: ledger_api.HeldFinding,
                           head: str, checkout: Path, server: bulk.RenameServer,
                           availability: PlanningFileAvailability) -> FreshMachineJudgment | bulk.Refusal:
    """Run the existing rename executor as a read-only proof before giving stale work authority."""
    candidate = machine_rename_candidate(ledger, finding, head, checkout)
    if isinstance(candidate, bulk.Refusal):
        return candidate
    path = candidate.anchor.path
    source, pair = candidate.source, candidate.rename
    target = checkout / path
    try:
        availability.require((path,))
        # rename_file owns syntax, references, capture, docs, visibility and module-instance
        # proof. It updates analyzer overlays only; no checkout or ledger row is written.
        file = bulk.rename_file(server, target, path, source,
                                (bulk.RenameFinding(finding.finding_id, path, pair, approved_renames.AnchoredRenameSource(candidate.anchor.commit, path, pair.line, source), finding.finding_id + ":" + ledger.finding_input_identity(finding)),),
                                bulk.repository_docs(checkout), check_files=availability.require)
        if not file.kept:
            return next(iter(file.refusals), bulk.Refusal(finding.finding_id, "fresh judgment has no complete rename proof"))
        inputs = tuple((name, hashlib.sha256((checkout / name).read_bytes()).hexdigest())
                       for name in sorted({path, *file.proof_dependencies, *file.dependencies}))
        return FreshMachineJudgment(finding.finding_id, candidate.anchor,
                                    ledger.finding_input_identity(finding), finding.rule_blob, inputs)
    finally:
        # A proof must not leave a speculative rename as input to another proof.
        for document in tuple(server.documents):
            if document.is_relative_to(checkout):
                server.open(document, document.read_bytes().decode())


def plan_renames(repo: Path, ledger: ledger_api.Ledger, base: str, selected: Selection,
                 server: bulk.RenameServer | RenameService, availability: PlanningFileAvailability) -> PlannedEdits:
    sources = RenameSources()
    sources.judgments = {item.finding_id: item for item in selected.judgments}
    refused: dict[str, bulk.Refusal] = {}
    pending: list[bulk.PendingFinding] = []
    groups: dict[str, list[bulk.RenameFinding]] = defaultdict(list)
    not_checked: list[NotCheckedFinding] = []
    for finding in selected.findings:
        try:
            rename = current_rename(ledger, finding, base, sources, repo)
            if (isinstance(rename, bulk.Refusal) and finding.finding_id not in sources.judgments
                    and (selected.judgment_policy == JudgmentPolicy.PROVE_CURRENT
                         or finding.finding_id in selected.newly_admitted)
                    and isinstance(ledger.finding_site(finding, base), ledger_api.NeedsJudgment)):
                candidate = ledger.rename_judgment_site(finding, base)
                if isinstance(candidate, ledger_api.NeedsJudgment):
                    rename = bulk.Refusal(finding.finding_id, "changed since judged: " + candidate.reason)
                else:
                    if isinstance(server, RenameService):
                        prerequisites = machine_rename_candidate(ledger, finding, base, repo)
                        if isinstance(prerequisites, bulk.Refusal):
                            refused[finding.finding_id] = prerequisites
                        else:
                            not_checked.append(NotCheckedFinding(finding.finding_id, server.value))
                        continue
                    judgment = fresh_machine_judgment(ledger, finding, base, repo, server, availability)
                    if isinstance(judgment, bulk.Refusal):
                        rename = judgment
                    else:
                        sources.judgments[finding.finding_id] = judgment
                        rename = current_rename(ledger, finding, base, sources, repo)
            if isinstance(rename, bulk.Refusal):
                refused[finding.finding_id] = rename
            else:
                groups[rename.path].append(rename)
        except bulk.ReservationBlocked as blocked:
            pending.append(bulk.PendingFinding(finding.finding_id, blocked.path, blocked.holders))
        except (bulk.BulkRenameError, OSError, UnicodeError) as error:
            refused[finding.finding_id] = bulk.Refusal(finding.finding_id, str(error))
    docs = bulk.repository_docs(repo)
    files: list[bulk.FileRenames] = []
    for path, findings in groups.items():
        if isinstance(server, RenameService):
            not_checked.extend(NotCheckedFinding(item.finding_id, server.value) for item in findings)
            continue
        try:
            file = bulk.rename_file(server, repo / path, path, sources.current[path], tuple(findings), docs,
                                    check_files=availability.require)
        except bulk.ReservationBlocked as blocked:
            pending.extend(bulk.PendingFinding(item.finding_id, blocked.path, blocked.holders) for item in findings)
            continue
        files.append(file)
        refused.update((item.finding_id, item) for item in file.refusals)
        for document in file.related:
            sources.current[document.path] = document.edited
    return PlannedEdits(tuple(file for file in bulk.combine_file_renames(files) if file.edited != file.original),
                        tuple(refused.values()),
                        judgments=tuple(sources.judgments.values()), pending=tuple(pending),
                        not_checked=tuple(not_checked))


def plan_constants(repo: Path, ledger: ledger_api.Ledger, base: str, selected: Selection) -> PlannedEdits:
    """Plan each Cargo target against committed bytes; merge only identical shared-file edits."""
    sources = RenameSources()
    refused: dict[str, str] = {}
    findings: list[ledger_api.HeldFinding] = []
    for finding in selected.findings:
        current = current_finding(ledger, finding, base, sources, repo)
        if isinstance(current, bulk.Refusal):
            refused[finding.finding_id] = current.reason
        else:
            findings.append(current)
    # Target discovery shares the frozen manifest/path reader with source ownership.
    # No export is needed; snapshots never consult untracked files or write sources.
    blobs = {path: entry.object for path, entry in ledger_api.tree_entries(repo, base).items()
             if entry.kind == "blob" and entry.mode in ledger_api.REGULAR_FILE_MODES}
    contents = {path: data for path, data in ledger_api.blob_contents(repo, blobs)}
    members = rules.workspace_members({path: data.decode() for path, data in contents.items()
                                      if Path(path).name == "Cargo.toml"})
    directories = tuple(member.directory for member in members)
    proofs: list[ConstantTargetProof] = []
    seen: set[str] = set()
    for member in members:
        owned = tuple(finding for finding in findings if style_proposals.crate_for(directories, finding.path) == member.directory)
        if not owned:
            continue
        try:
            targets = bulk.cargo_target_roots(
                repo / member.directory, frozen=bulk.FrozenCargoTargetInventory(
                    contents[(Path(member.directory) / "Cargo.toml").as_posix()].decode(),
                    frozenset(repo / path for path in blobs)))
        except (bulk.BulkRenameError, ValueError, OSError) as error:
            refused.update((finding.finding_id, str(error)) for finding in owned)
            continue
        for target in targets:
            entry = target.path.relative_to(repo).as_posix()
            snapshot = constants.CrateSources(entry, {path: data.decode() for path, data in contents.items()
                if (path.endswith(".rs") or path == entry) and Path(path).is_relative_to(Path(entry).parent)
                and style_proposals.crate_for(directories, path) == member.directory})
            try:
                modules = constants.crate_module_paths(snapshot)
            except (ValueError, rust.UnreadableTokenSpans) as error:
                refused.update((finding.finding_id, f"target {entry}: {error}") for finding in owned)
                continue
            applicable = tuple(finding for finding in owned if finding.path in modules)
            seen.update(finding.finding_id for finding in applicable)
            result = constants.apply_crate_constants(snapshot, applicable)
            refused.update((item.finding_id, item.reason) for item in result.refusals)
            if result.kept:
                proofs.append(ConstantTargetProof(snapshot, result))
    for finding in findings:
        if finding.finding_id not in seen:
            _ = refused.setdefault(finding.finding_id, "finding is not in an ordinary Cargo target module")
    # A shared module must satisfy every target. Conflicting edit groups are refused together.
    while True:
        edits: dict[str, constants.CrateConstantEdit] = {}
        conflicts: set[str] = set()
        for proof in proofs:
            for edit in proof.result.edits:
                if edit.path in edits and edits[edit.path] != edit:
                    conflicts.add(edit.path)
                edits[edit.path] = edit
        removed = [proof for proof in proofs if conflicts & {edit.path for edit in proof.result.edits}
                   or any(item.finding.finding_id in refused for item in proof.result.kept)]
        if not removed:
            break
        for proof in removed:
            for item in proof.result.kept:
                _ = refused.setdefault(item.finding.finding_id, "incompatible Cargo target constant edits")
        proofs = [proof for proof in proofs if proof not in removed]
    files: dict[str, ConstantFileEdit] = {}
    for proof in proofs:
        paths = frozenset(edit.path for edit in proof.result.edits)
        for edit in proof.result.edits:
            previous = files.get(edit.path)
            kept = tuple(item for item in proof.result.kept if item.finding.path == edit.path)
            if previous:
                kept = tuple({item.finding.finding_id: item for item in (*previous.kept, *kept)}.values())
            files[edit.path] = ConstantFileEdit(edit.path, edit.original, edit.edited, kept,
                                               paths | (previous.dependencies if previous else frozenset[str]()), edit.created)
    return PlannedEdits(tuple(files.values()), tuple(bulk.Refusal(identity, reason) for identity, reason in refused.items()), tuple(proofs))



def dependent_files(dependencies: dict[str, frozenset[str]], paths: set[str]) -> set[str]:
    """All files that must be kept or refused together, including transitive groups."""
    reached = set(paths)
    pending = list(paths)
    while pending:
        for path in dependencies.get(pending.pop(), frozenset()):
            if path not in reached:
                reached.add(path)
                pending.append(path)
    return reached


class AvailableFile(Enum):
    AVAILABLE = "available"


@dataclass(frozen=True)
class UnavailableFile:
    reason: str


class PlanningFileAvailability:
    """One reservation answer per path during planning; the final guard checks afresh."""

    def __init__(self, repo: Path, available: Callable[[Path, str], None]) -> None:
        self.repo: Path = repo
        self.available: Callable[[Path, str], None] = available
        self.checked: dict[str, AvailableFile | UnavailableFile | bulk.ReservationBlocked] = {}

    def require(self, paths: tuple[str, ...]) -> None:
        for path in paths:
            if path not in self.checked:
                try:
                    self.available(self.repo, path)
                    self.checked[path] = AvailableFile.AVAILABLE
                except bulk.ReservationBlocked as blocked:
                    self.checked[path] = blocked
                except (bulk.BulkRenameError, OSError) as error:
                    self.checked[path] = UnavailableFile(str(error))
            answer = self.checked[path]
            if isinstance(answer, bulk.ReservationBlocked):
                raise answer
            if isinstance(answer, UnavailableFile):
                raise bulk.BulkRenameError(answer.reason)


def available_constant_plan(plan: PlannedEdits, availability: PlanningFileAvailability) -> PlannedEdits:
    """Partition atomic groups; a terminal error wins over a transient reservation."""
    files = {file.path: file for file in plan.files}
    dependencies = {file.path: file.dependencies for file in plan.files}
    refused = {item.finding_id: item.reason for item in plan.refusals}
    removed: set[str] = set()
    pending = list(plan.pending)
    for path in files:
        try:
            availability.require((path,))
        except bulk.ReservationBlocked as blocked:
            group = dependent_files(dependencies, {path})
            removed.update(group)
            pending.extend(bulk.PendingFinding(item.finding.finding_id, blocked.path, blocked.holders)
                           for member in group & files.keys() for item in files[member].kept)
        except bulk.BulkRenameError as error:
            group = dependent_files(dependencies, {path})
            removed.update(group)
            for member in group & files.keys():
                for item in files[member].kept:
                    _ = refused.setdefault(item.finding.finding_id, str(error))
    return PlannedEdits(tuple(file for file in plan.files if file.path not in removed),
                        tuple(bulk.Refusal(identity, reason) for identity, reason in refused.items()),
                        tuple(proof for proof in plan.constant_proofs
                              if not any(edit.path in removed for edit in proof.result.edits)), plan.judgments,
                        tuple({item.finding_id: item for item in pending
                               if item.finding_id not in refused}.values()), plan.not_checked)


def diagnostic_failures(repo: Path, result: CommandResult) -> dict[str, str]:
    """Retain the compiler cause alongside every diagnostic path, including expansions."""
    paths: dict[str, str] = {}
    def spans(message: dict[str, object], reason: str) -> None:
        for entry in bulk.object_list(message.get("spans", [])):
            span = bulk.object_fields(entry)
            name = str(span.get("file_name", ""))
            if name and not name.startswith("<"):
                resolved = (repo / name).resolve()
                if resolved.is_relative_to(repo):
                    paths[resolved.relative_to(repo).as_posix()] = reason
            expansion = span.get("expansion")
            if isinstance(expansion, dict):
                spans({"spans": [bulk.object_fields(cast(object, expansion)).get("span", {})]}, reason)
        for child in bulk.object_list(message.get("children", [])):
            spans(bulk.object_fields(child), reason)
    for line in (result.stdout + "\n" + result.stderr).splitlines():
        try:
            message = bulk.object_fields(cast(object, json.loads(line)))
        except (ValueError, bulk.BulkRenameError):
            continue
        if message.get("reason") == "compiler-message":
            message = bulk.object_fields(message.get("message"))
        if message.get("level") == "error":
            detail = str(message.get("message", ""))
            code = message.get("code")
            label = str(bulk.object_fields(cast(object, code)).get("code", "")) if isinstance(code, dict) else ""
            reason = "workspace diagnostics" + (": " + (label + ": " if label else "") + detail if detail else "")
            spans(message, reason)
    return paths


def validate(workspace: WorkspaceEdits, runner: Runner, times: dict[str, float],
             loops: list[dict[str, object]], reprove_remaining: Callable[[set[str]], set[str]]) -> tuple[set[str], dict[str, str]]:
    kept = set(workspace.files) - workspace.refused_files
    reverted: dict[str, str] = {}
    while True:
        started = time.monotonic()
        loop: dict[str, object] = {"reverted": []}
        try:
            results = [runner(workspace.repo, ("cargo", verb, "--workspace", "--all-targets", "--message-format=json", *extra))
                       for verb, extra in (("check", ()), ("clippy", ("--", "-D", "warnings")))]
            workspace.guard(kept)
            failures = [result for result in results if result.code]
            if not failures:
                return kept, reverted
            errors: dict[str, str] = {}
            for result in failures:
                errors.update(diagnostic_failures(workspace.repo, result))
            remove = dependent_files(workspace.dependencies, set(errors)) & kept
            loop.update(errors=sorted(errors), reverted=sorted(remove))
            if not remove:
                raise bulk.BulkRenameError("validation errors remain but no new edited file can be reverted")
            workspace.restore(sorted(remove))
            kept -= remove
            for path in remove:
                causes = sorted({reason for error_path, reason in errors.items()
                                 if path in dependent_files(workspace.dependencies, {error_path})})
                reason = "; ".join(causes)
                reverted.update((item.finding.finding_id, reason) for item in workspace.files[path].kept)
            kept = reprove_remaining(kept)
        finally:
            seconds = time.monotonic() - started
            times["check_clippy"] += seconds
            loop["seconds"] = seconds
            loops.append(loop)


def tested_tree_proof(checkout: Path, tree: str, runner: Runner, times: dict[str, float]) -> CommandResult:
    """Test and lint the exact clean tree, then require its machine-wide final pass record.

    Keep all evidence checks here. A zero command exit alone never authorizes publication.
    The query fails closed until the saved final pass integration is available.
    """
    require_test_tree(checkout, tree)
    verify = str(Path.home() / ".claude/scripts/direct/verify.sh")
    for stage, arguments in (("verify_final", ("final",)), ("verify_lint", ("lint",)),
                             ("final_pass", ("final", "--saved", "--tree", tree))):
        started = time.monotonic()
        try:
            result = runner(checkout, ("bash", verify, *arguments))
        finally:
            times[stage] = time.monotonic() - started
            require_test_tree(checkout, tree)
        if result.code:
            return CommandResult(result.code, result.stdout, result.stderr or f"verify.sh {arguments[0]} failed")
    return CommandResult(0)


def first_failing_test(result: CommandResult) -> str:
    for line in (result.stdout + "\n" + result.stderr).splitlines():
        if re.search(r"(?:\bFAIL\b|\bFAILED\b|^failures:)", line):
            return line.strip()[:300]
    return "workspace test command exited " + str(result.code)


def require_test_tree(repo: Path, tree: str) -> None:
    if (git(repo, "-c", "status.showUntrackedFiles=all", "status", "--porcelain=v1",
            "--untracked-files=all", "--ignore-submodules=none") or git(repo, "rev-parse", "HEAD^{tree}") != tree
            or git(repo, "write-tree") != tree):
        raise bulk.BulkRenameError("workspace tests require an unchanged clean checkout of the tested tree")


def test_tree(repo: Path, commit: str, tree: str, runner: Runner, times: dict[str, float], *, owner: str = "") -> CommandResult:
    with copies.standing(owner or style_sync.gate_owner(repo), repo, copies.CommittedSource(commit)) as checkout:
        require_test_tree(checkout, tree)
        result = tested_tree_proof(checkout, tree, runner, times)
        require_test_tree(checkout, tree)
        return result


def summary(report: dict[str, object]) -> str:
    return (f"bulk {report['rule']}: {report['applied']} applied, {report['refused']} refused, "
            f"{report['reverted']} reverted; tree {report['tested_tree']}; commit {report['commit']}; {report['status']}; "
            f"counts={json.dumps(report['counts'], sort_keys=True)}; seconds={json.dumps(report['seconds'], sort_keys=True)}; "
            f"skipped={json.dumps(report['skip_counts'], sort_keys=True)}")


def review_sample(findings: tuple[AppliedFinding, ...]) -> tuple[AppliedFinding, ...]:
    renames = tuple(item for item in findings if isinstance(item, bulk.KeptRename))
    # Constants have one kind; the existing rename sampler stratifies its different site kinds.
    return bulk.sample_findings(renames) if renames else findings[:60]


def review_record(item: AppliedFinding) -> dict[str, object]:
    if isinstance(item, bulk.KeptRename):
        return asdict(item)
    finding = item.finding
    return {"finding": {"finding_id": finding.finding_id, "path": finding.path, "line": finding.line,
                        "text": finding.text, "after": finding.after}, "kind": item.kind,
            "constant": item.constant.declaration, "type_proof": item.type_proof.value}


class RenameService(Enum):
    ON_DEMAND = "start only for a names rule"
    READ_ONLY = "selection-only for semantic rename proof: rust-analyzer may run build scripts and write build artifacts"


def plan_rule(repo: Path, ledger: ledger_api.Ledger, base: str, selected: Selection,
              definition: rules.Rule, server: bulk.RenameServer | RenameService,
              available: Callable[[Path, str], None]) -> PlannedEdits:
    """Plan against one execution checkout while retaining canonical ledger authority."""
    if definition.family != "names":
        return available_constant_plan(plan_constants(repo, ledger, base, selected),
                                       PlanningFileAvailability(repo, available))
    if server == RenameService.READ_ONLY:
        return plan_renames(repo, ledger, base, selected, server,
                            PlanningFileAvailability(repo, available))
    with ExitStack() as stack:
        rename_server = stack.enter_context(bulk.RustAnalyzer(repo)) if isinstance(server, RenameService) else server
        # Injected analyzers have the same lifetime across retries; clear
        # their overlays after restoring the tree, before any new proof.
        if not isinstance(server, RenameService):
            for path in tuple(rename_server.documents):
                if path.is_relative_to(repo):
                    rename_server.open(path, (repo / path).read_text())
        return plan_renames(repo, ledger, base, selected, rename_server,
                            PlanningFileAvailability(repo, available))


def execute_plan(workspace: WorkspaceEdits, plan: PlannedEdits, definition: rules.Rule,
                 ledger: ledger_api.Ledger, intent: WorkspaceIntent, runner: Runner,
                 available: Callable[[Path, str], None],
                 replan: Callable[[frozenset[str]], PlannedEdits], times: dict[str, float],
                 loops: list[dict[str, object]], refused: dict[str, str]) -> set[str]:
    """Install the complete plan, journal writes, format/reprove and validate its survivors.

    Both workspace and identified crate runs use this execution owner. Replanning must
    preserve the caller's selection and source boundaries before any rebuilt overlay.
    Refusals update the caller's accumulator so rollback retains evidence even on failure.
    """
    try:
        require_ready_plan(plan)
        repo, base = workspace.repo, workspace.base
        refused.update((item.finding_id, item.reason) for item in plan.refusals)
        candidates = plan.files
        workspace.set_plan(plan)
        _ = require_base(repo, git(repo, "rev-parse", base + "^{tree}"))
        for file in candidates:
            available(repo, file.path)
        require_edit_paths(repo, candidates)
        intent.record_plan(plan)
        for file in candidates:
            _ = (repo / file.path).write_bytes(file.edited.encode())
        kept = set(workspace.files)
        if kept:
            started = time.monotonic()
            members = coverage_report.workspace(ledger, base)
            directories = tuple(member.directory for member in members)
            touched = {style_proposals.crate_for(directories, path) for path in kept}
            packages = sorted({member.package_name for member in members if member.directory in touched})
            if not packages:
                raise bulk.BulkRenameError("changed files have no workspace package")
            format_arguments = ("cargo", "+nightly", "fmt", *(arg for name in packages for arg in ("--package", name)))
            if definition.family == "names":
                refused.update(format_rename_plan(workspace, runner, format_arguments, replan,
                                                  intent.record_plan, times, intent.record_current))
                kept = set(workspace.files)
            else:
                formatted = runner(repo, format_arguments)
                times["fmt"] = time.monotonic() - started
                if formatted.code:
                    raise bulk.BulkRenameError("workspace format failed: " + formatted.stderr.strip())
                workspace.guard(kept)
                intent.record_current()
            def reprove_remaining(paths: set[str]) -> set[str]:
                if definition.family != "names":
                    return paths
                restored = frozenset(workspace.files.keys() - paths)
                refused.update(reprove_rename_plan(workspace, paths, replan, intent.record_plan, times))
                refused.update(format_rename_plan(workspace, runner, format_arguments, replan,
                                                  intent.record_plan, times, intent.record_current, restored))
                return set(workspace.files)

            if kept:
                kept, diagnostics_refused = validate(workspace, runner, times, loops, reprove_remaining)
                refused.update(diagnostics_refused)
        return kept
    except (KeyboardInterrupt, SystemExit) as stopped:
        if not intent.retry_eligible():
            raise
        intent.restore_admission(stopped)
        raise


def run_rule(repo: Path, rule: str, base_tree: str, ledger: ledger_api.Ledger,
             server: bulk.RenameServer | RenameService = RenameService.ON_DEMAND,
             runner: Runner = run_command, available: Callable[[Path, str], None] = bulk.check_available) -> Path:
    repo = repo.resolve()
    if repo != ledger.repo.resolve():
        raise bulk.BulkRenameError("ledger belongs to a different checkout")
    intent = WorkspaceIntent(repo, f"bulk_run --rule {rule} --base-tree {base_tree} --repo {repo}")
    interrupted_tickets = intent.recover()
    resume = intent.retry_eligible()
    if not resume:
        interrupted_owner = ledger_api.BatchOwner(intent.batch_id)
        with ledger_api.refill_lock(repo):
            for ticket in interrupted_tickets:
                try:
                    ledger.ticket_drop_batch(ticket, interrupted_owner)
                except ledger_api.TicketBatchMismatch as error:
                    print(f"bulk recovery: {error}")
        intent.finish()
    base = require_base(repo, base_tree)
    definition = next((item for item in rules.load_rules(repo) if item.rule_id == rule or item.name == rule), None)
    if definition is None or definition.family not in {"names", "values"}:
        raise bulk.BulkRenameError("workspace bulk run requires a names or values family rule")
    times: dict[str, float] = dict.fromkeys(("select", "rename", "fmt", "check_clippy", "commit", "tests", "verify_final", "verify_lint", "final_pass"), 0.0)
    started = time.monotonic()
    with ledger_api.refill_lock(repo):
        if resume:
            selected, owned = intent.resume_claims(ledger, definition)
            intent.clear_admission()
        else:
            selected = select(ledger, definition)
            owned = claim(ledger, selected, definition.family, git(repo, "symbolic-ref", "HEAD"), intent)
            # Claiming changes build_ticket; use the current rows for proof and retry validation.
            selected = replace(selected, findings=ledger.named_held(owned))
            intent.complete_claims(owned, selected, ledger)
    times["select"] = time.monotonic() - started
    tickets = sorted(set(owned.values()))
    refused: dict[str, str] = {}
    reverted: dict[str, str] = {}
    loops: list[dict[str, object]] = []
    workspace = WorkspaceEdits(repo, base, ())
    commit = ""
    tree = ""
    status = "no edits"
    kept_findings: tuple[AppliedFinding, ...] = ()
    def rollback_failure(reason: str) -> None:
        nonlocal status, kept_findings, commit
        status = reason
        if git(repo, "rev-parse", "HEAD") != base:
            _ = git(repo, "reset", "--hard", base)
        workspace.rollback(intent)
        for identity in owned:
            if identity not in refused:
                reverted[identity] = status
        for ticket in tickets:
            _ = ledger.bulk_release(ticket, {identity: reason for identity, reason in {**refused, **reverted}.items() if owned[identity] == ticket})
            ledger.ticket_drop(ticket)
        kept_findings = ()
        commit = ""

    try:
        with bulk.admission_attempt(intent.stop_admission, intent.clear_admission):
            started = time.monotonic()
            def release_blocked_files(plan: PlannedEdits) -> PlannedEdits:
                # Workspace runs have no resume manifest. Release blocked groups for
                # later selection, so every edit committed now has its full checkpoint.
                reservation_refusals = tuple(bulk.Refusal(item.finding_id,
                    f"{item.path}: another worktree holds the file: " + "; ".join(
                        f"{holder.holder_worktree_root} ({holder.holder_branch}, reservation {holder.reservation_id})"
                        for holder in item.holders)) for item in plan.pending)
                return replace(plan, refusals=(*plan.refusals, *reservation_refusals), pending=())

            def replan(identities: frozenset[str]) -> PlannedEdits:
                survivors = Selection(tuple(finding for finding in selected.findings
                                             if finding.finding_id in identities), ())
                return release_blocked_files(plan_rule(repo, ledger, base, survivors, definition, server, available))

            plan = release_blocked_files(plan_rule(repo, ledger, base, selected, definition, server, available))
            times["rename"] = time.monotonic() - started
            kept = execute_plan(workspace, plan, definition, ledger, intent,
                                runner, available, replan, times, loops, refused)
            for ticket in tickets:
                _ = ledger.bulk_release(ticket, {identity: reason for identity, reason in {**refused, **reverted}.items() if owned[identity] == ticket})
            kept_findings = tuple(item for path in sorted(kept) for item in workspace.files[path].kept)
            if kept_findings:
                started = time.monotonic()
                workspace.guard(kept)
                _ = git(repo, "add", "--", *sorted(kept))
                tree = git(repo, "write-tree")
                operation = "rename" if definition.family == "names" else "constant extraction"
                title = f"style({definition.name}): bulk {operation} — {len(kept_findings)} findings"
                _ = git(repo, "commit", "-m", title)
                commit = git(repo, "rev-parse", "HEAD")
                if git(repo, "rev-parse", "HEAD^{tree}") != tree:
                    raise bulk.BulkRenameError("commit changed the validated tree")
                times["commit"] = time.monotonic() - started
                started = time.monotonic()
                try:
                    result = test_tree(repo, commit, tree, runner, times)
                finally:
                    times["tests"] = time.monotonic() - started
                if result.code:
                    reason = "tests failed: " + first_failing_test(result)
                    reverted.update((item.finding.finding_id, reason) for item in kept_findings)
                    _ = git(repo, "reset", "--hard", base)
                    commit = ""
                    for ticket in tickets:
                        _ = ledger.bulk_release(ticket, {identity: why for identity, why in reverted.items() if owned[identity] == ticket})
                    kept_findings = ()
                    status = reason
                else:
                    started = time.monotonic()
                    _ = git(repo, "commit", "--amend", "-m", f"{title}\n\nTested-Tree: {tree}")
                    commit = git(repo, "rev-parse", "HEAD")
                    _ = require_base(repo, tree)
                    times["commit"] += time.monotonic() - started
                    # Once tested, keep the commit even if publishing its checkpoints fails halfway.
                    # Repeating ticket_checkpoint and bulk_share_checkpoint completes that publication.
                    active = sorted({owned[item.finding.finding_id] for item in kept_findings})
                    try:
                        for ticket in active:
                            ledger.ticket_checkpoint(ticket, commit)
                        ledger.bulk_share_checkpoint(active)
                    except Exception as error:
                        status = "tested; checkpoint publication pending: " + str(error)
                    else:
                        status = "tested; checkpointed; built at normal ticket merge"
            # Empty tickets are dropped; checkpointed tickets remain for normal merge.
            for ticket in tickets:
                if not any(item.finding.finding_id in owned and owned[item.finding.finding_id] == ticket for item in kept_findings):
                    ledger.ticket_drop(ticket)
    except bulk.StyleAdmissionStopped:
        raise
    except (bulk.BulkRenameError, OSError, UnicodeError) as error:
        rollback_failure("batch refused: " + str(error))
    except (KeyboardInterrupt, SystemExit):
        if intent.retry_eligible():
            raise
        rollback_failure("batch interrupted")
        raise
    except Exception as error:
        rollback_failure("tool defect: " + str(error))
    members = coverage_report.workspace(ledger, base)
    counts: Counter[tuple[str, str, str, str]] = Counter()
    for finding in selected.findings:
        identity = finding.finding_id
        reason = refused.get(identity, reverted.get(identity, ""))
        route = "refused" if identity in refused else "reverted" if identity in reverted else "applied"
        crate = style_proposals.crate_for(tuple(member.directory for member in members), finding.path)
        counts[(route, reason, definition.family, crate)] += 1
    report: dict[str, object] = {
        "rule": definition.rule_id, "base": base, "base_tree": base_tree, "tickets": tickets,
        "commit": commit, "tested_tree": tree, "status": status, "applied": len(kept_findings),
        "applied_ids": [item.finding.finding_id for item in kept_findings],
        "refused": len(refused), "reverted": len(reverted), "skipped": [asdict(item) for item in selected.skipped],
        "refusals": refused, "reversions": reverted,
        "skip_counts": dict(Counter(item.reason for item in selected.skipped)),
        "counts": [{"disposition": route, "reason": reason, "family": family, "crate": crate, "count": count}
                   for (route, reason, family, crate), count in sorted(counts.items())],
        "seconds": times, "validation_loops": loops, "sample": [review_record(item) for item in review_sample(kept_findings)],
    }
    report["summary"] = summary(report)
    folder = ledger.database.parent / "bulk_review"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / ("workspace-" + hashlib.sha256((definition.rule_id + base).encode()).hexdigest()[:16] + ".json")
    bulk.atomic_json(path, report)
    intent.finish()
    print(report["summary"])
    return path


class CrateCommand(Protocol):
    def __call__(self, repo: Path, packages: Sequence[str], run_id: str, rule: str, base_tree: str,
                 destination: str, ledger: ledger_api.Ledger,
                 server: bulk.RenameServer | RenameService, runner: Runner,
                 available: Callable[[Path, str], None], /) -> int: ...


def run_crates(repo: Path, packages: Sequence[str], run_id: str, rule: str, base_tree: str,
               destination: str, ledger: ledger_api.Ledger,
               server: bulk.RenameServer | RenameService = RenameService.ON_DEMAND,
               runner: Runner = run_command,
               available: Callable[[Path, str], None] = bulk.check_available) -> int:
    """Enter the lifecycle through a deferred typed command; crate_run depends on this API."""
    handler = cast(CrateCommand, getattr(importlib.import_module("scripts.style.crate_run"), "run_crates"))
    return handler(repo, packages, run_id, rule, base_tree, destination, ledger, server, runner, available)


class DryRunCommand(Protocol):
    def __call__(self, repo: Path, packages: Sequence[str], rule: str, base_tree: str,
                 ledger: ledger_api.Ledger, /) -> dict[str, object]: ...


class Arguments(argparse.Namespace):
    repo: Path = Path()
    rule: str = ""
    base_tree: str = ""
    crate: list[str] = []
    run: str = ""
    destination: str = ""
    dry_run: bool = False


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--repo", type=Path, default=Path.cwd())
    _ = parser.add_argument("--rule", default="")
    _ = parser.add_argument("--base-tree", default="")
    _ = parser.add_argument("--crate", action="append", default=[])
    _ = parser.add_argument("--run", default="")
    _ = parser.add_argument("--destination", default="")
    _ = parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv, namespace=Arguments())
    if args.crate:
        if not args.run and not args.dry_run:
            parser.error("--crate requires --run")
    elif args.run or args.destination:
        parser.error("--run and --destination require --crate")
    elif not args.rule or not args.base_tree:
        parser.error("--rule and --base-tree are required without --crate")
    if args.dry_run and not args.crate:
        parser.error("--dry-run requires --crate")
    try:
        if args.dry_run:
            location = ledger_api.ledger_location(args.repo)
            with ledger_api.Ledger(location.repo, location.database, access=ledger_api.LedgerAccess.READ_ONLY) as ledger:
                handler = cast(DryRunCommand, getattr(importlib.import_module("scripts.style.crate_run"), "dry_run_crates"))
                print(json.dumps(handler(args.repo, args.crate, args.rule, args.base_tree, ledger), indent=2))
                return 0
        if socket.gethostname().split(".")[0] != "natedev":
            raise bulk.BulkRenameError("workspace bulk tests must run on natedev")
        with ledger_api.open_ledger(args.repo) as ledger:
            if args.crate:
                return run_crates(args.repo, args.crate, args.run, args.rule, args.base_tree,
                                  args.destination, ledger)
            report = run_rule(args.repo, args.rule, args.base_tree, ledger)
            status = bulk.object_fields(cast(object, json.loads(report.read_text()))).get("status", "")
            return 1 if (str(status).startswith(("tests failed:", "batch refused:", "tool defect:"))
                         or "publication pending:" in str(status)) else 0
    except (bulk.BulkRenameError, ledger_api.LedgerError, OSError, subprocess.SubprocessError) as error:
        print(f"bulk workspace: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    # crate_run imports this module: keep one class identity when entered as a script.
    from scripts.style import bulk_run
    raise SystemExit(bulk_run.main())
