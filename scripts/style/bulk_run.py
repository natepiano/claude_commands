#!/usr/bin/env python3
"""Apply one approved names or values rule; test its clean workspace tree once on natedev.

Each original decision keeps a normal proposal ticket. All tickets share one checkpoint.
This command never sends messages or changes approvals.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import socket
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from contextlib import ExitStack
from dataclasses import asdict, dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Protocol, cast, override

import bulk_constants as constants
import bulk_rename as bulk
import coverage_report
import rules
import style_ledger as ledger_api
import style_proposals
import style_footprint as rust
from approved_renames import BindingRename, NoBindingScope, refactor_rename, rename_declaration
from issues import Issue, frontmatter
from proposal_issue import ApprovedDecision, UnansweredDecision, decision_answer


@dataclass(frozen=True)
class CommandResult:
    code: int
    stdout: str = ""
    stderr: str = ""


class Runner(Protocol):
    def __call__(self, repo: Path, arguments: Sequence[str]) -> CommandResult: ...


def run_command(repo: Path, arguments: Sequence[str]) -> CommandResult:
    result = subprocess.run(arguments, cwd=repo, capture_output=True, text=True, check=False)
    return CommandResult(result.returncode, result.stdout, result.stderr)


def git(repo: Path, *arguments: str) -> str:
    result = run_command(repo, ("git", *arguments))
    if result.code:
        raise bulk.BulkRenameError(result.stderr.strip() or "git failed: " + " ".join(arguments))
    return result.stdout.strip()


def require_base(repo: Path, tree: str) -> str:
    if git(repo, "status", "--porcelain", "--untracked-files=no"):
        raise bulk.BulkRenameError("workspace run requires a clean tracked tree and index")
    if not re.fullmatch(r"[0-9a-f]{40}", tree) or git(repo, "rev-parse", "HEAD^{tree}") != tree:
        raise bulk.BulkRenameError("base-tree mismatch")
    _ = git(repo, "symbolic-ref", "HEAD")
    return git(repo, "rev-parse", "HEAD")


@dataclass(frozen=True)
class Selection:
    findings: tuple[ledger_api.HeldFinding, ...]
    skipped: tuple[bulk.Refusal, ...]


def select(ledger: ledger_api.Ledger, definition: rules.Rule) -> Selection:
    """Only filed approvals are ready. An always-approved rule also accepts unanswered decisions.

    Declines are never overridden. Unfiled findings still need the normal filing step.
    Other proposal tickets' files are excluded, including other rules and families.
    File reservations additionally protect active readers and non-proposal builders.
    """
    tickets = tuple(ticket for ticket in ledger.proposal_tickets() if ticket["state"] not in ledger_api.FINAL_TICKET_STATES)
    owners = {ticket["id"] for ticket in tickets}
    busy = {path for ticket in tickets for path in (*ticket["files"], *(item["path"] for item in ticket["widened"]))}
    approvals: dict[Path, bool] = {}
    ready: list[ledger_api.HeldFinding] = []
    skipped: list[bulk.Refusal] = []
    for finding in ledger.held_findings(ledger_api.AllHeldFindings()):
        if finding.rule.removesuffix("#uncovered") != definition.rule_id:
            continue
        reason = ""
        if not isinstance(finding.state, ledger_api.Filed):
            reason = ledger_api.held_state_name(finding.state)
        elif isinstance(finding.build_ticket, ledger_api.BuildTicket) and finding.build_ticket.ticket in owners:
            reason = "held by another open ticket"
        elif finding.path in busy:
            reason = "file held by another open ticket"
        elif not rules.judged_text_unchanged(finding.rule_blob, definition):
            reason = "rule changed since judged"
        else:
            proposal = finding.state.proposal
            if proposal not in approvals:
                answer = decision_answer(Issue(proposal, frontmatter(proposal))) if proposal.is_file() else UnansweredDecision()
                approvals[proposal] = (isinstance(answer, ApprovedDecision)
                                       or (isinstance(definition.approval, rules.AlwaysApproved)
                                           and isinstance(answer, UnansweredDecision) and proposal.is_file()))
            if not approvals[proposal]:
                reason = "decision not approved"
        if reason:
            skipped.append(bulk.Refusal(finding.finding_id, reason))
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
    owned: dict[str, str] = {}
    planned: list[tuple[str, str, str, list[ledger_api.HeldFinding]]] = []
    for proposal, findings in groups.items():
        unit = "bulk-" + hashlib.sha256((findings[0].rule + proposal).encode()).hexdigest()[:16] + f"-{family}-unit"
        ticket = ledger.next_ticket_id(unit)
        planned.append((ticket, unit, proposal, findings))
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

    def judged_file(self, repo: Path, commit: str, path: str) -> CommandResult:
        key = (commit, path)
        if key not in self.judged:
            # Decode only after reading: declaration line endings must remain byte-identical.
            result = subprocess.run(["git", "show", f"{commit}:{path}"], cwd=repo, capture_output=True, check=False)
            self.judged[key] = CommandResult(result.returncode, result.stdout.decode("utf-8"))
        return self.judged[key]


def current_finding(ledger: ledger_api.Ledger, finding: ledger_api.HeldFinding, head: str,
                   sources: RenameSources) -> ledger_api.HeldFinding | bulk.Refusal:
    """Shared site lineage plus exact physical declaration bytes, before any edits.

    Whitespace and line terminators count. A follow is provenance, never a new judgment.
    If original coordinates cannot be recovered unambiguously, a worker must judge again.
    """
    evidence = ledger.finding_site(finding, head)
    judged = ledger.judged_commit(finding)
    if not isinstance(evidence, ledger_api.UnchangedSite) or not isinstance(judged, ledger_api.JudgedCommit):
        return bulk.Refusal(finding.finding_id, "changed since judged")
    path = ledger.repo / evidence.path
    if path.resolve() != path or not path.is_file() or path.suffix != ".rs":
        return bulk.Refusal(finding.finding_id, "finding path is not a regular Rust file")
    if evidence.path not in sources.current:
        sources.current[evidence.path] = path.read_bytes().decode("utf-8")
    source = sources.current[evidence.path]
    old = sources.judged_file(ledger.repo, judged.commit, finding.path)
    if old.code:
        return bulk.Refusal(finding.finding_id, "changed since judged")
    old_lines = old.stdout.encode().splitlines(keepends=True)
    new_lines = source.encode().splitlines(keepends=True)
    old_line = finding.line
    if isinstance(finding.follow, ledger_api.FollowedDeclaration):
        matches = [i + 1 for i, line in enumerate(old_lines) if line.decode("utf-8").rstrip("\r\n") == finding.text]
        old_line = matches[0] if len(matches) == 1 else 0
    if (not 0 < old_line <= len(old_lines) or not 0 < evidence.line <= len(new_lines)
            or old_lines[old_line - 1] != new_lines[evidence.line - 1]):
        return bulk.Refusal(finding.finding_id, "changed since judged")
    return replace(finding, path=evidence.path, line=evidence.line, text=source.splitlines()[evidence.line - 1])


def current_rename(ledger: ledger_api.Ledger, finding: ledger_api.HeldFinding, head: str,
                   sources: RenameSources) -> bulk.RenameFinding | bulk.Refusal:
    current = current_finding(ledger, finding, head, sources)
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
        return bulk.Refusal(finding.finding_id, "changed since judged")
    return bulk.RenameFinding(finding.finding_id, current.path, pair)


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
class PlannedEdits:
    files: tuple[bulk.FileRenames | ConstantFileEdit, ...]
    refusals: tuple[bulk.Refusal, ...]
    constant_proofs: tuple[ConstantTargetProof, ...] = ()


class WorkspaceIntent(bulk.BatchIntent):
    """The shared batch journal, extended with exact bytes for newly created files."""

    def record_plan(self, plan: PlannedEdits) -> None:
        # BatchIntent's existing-file journal needs only path and edited bytes.
        self.record_edits(tuple(bulk.FileRenames(file.path, file.original, file.edited, (), ())
                                for file in plan.files if not isinstance(file, ConstantFileEdit) or not file.created))
        record = bulk.object_fields(cast(object, json.loads(self.path.read_text())))
        record["created_files"] = {file.path: file.edited.encode().hex() for file in plan.files
                                   if isinstance(file, ConstantFileEdit) and file.created}
        bulk.atomic_json(self.path, record)

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
        self.files: dict[str, bulk.FileRenames | ConstantFileEdit] = {file.path: file for file in files}
        self.dependencies: dict[str, frozenset[str]] = {path: file.dependencies | {path} for path, file in self.files.items()}
        self.initial_untracked: set[str] = self.untracked()
        self.constant_proofs: tuple[ConstantTargetProof, ...] = ()

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

    def guard(self, kept: set[str]) -> None:
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


def plan_renames(repo: Path, ledger: ledger_api.Ledger, base: str, selected: Selection,
                 server: bulk.RenameServer, available: Callable[[Path, str], None]) -> PlannedEdits:
    sources = RenameSources()
    refused: dict[str, str] = {}
    groups: dict[str, list[bulk.RenameFinding]] = defaultdict(list)
    reservations: dict[str, str] = {}
    for finding in selected.findings:
        try:
            rename = current_rename(ledger, finding, base, sources)
            if isinstance(rename, bulk.Refusal):
                refused[finding.finding_id] = rename.reason
            else:
                if rename.path not in reservations:
                    try:
                        available(repo, rename.path)
                    except (bulk.BulkRenameError, OSError) as error:
                        reservations[rename.path] = str(error)
                    else:
                        reservations[rename.path] = ""
                if reservations[rename.path]:
                    refused[finding.finding_id] = reservations[rename.path]
                else:
                    groups[rename.path].append(rename)
        except (bulk.BulkRenameError, OSError, UnicodeError) as error:
            refused[finding.finding_id] = str(error)
    docs = bulk.repository_docs(repo)
    files: list[bulk.FileRenames] = []
    for path, findings in groups.items():
        file = bulk.rename_file(server, repo / path, path, sources.current[path], tuple(findings), docs)
        files.append(file)
        refused.update((item.finding_id, item.reason) for item in file.refusals)
        for document in file.related:
            sources.current[document.path] = document.edited
    return PlannedEdits(tuple(file for file in bulk.combine_file_renames(files) if file.edited != file.original),
                        tuple(bulk.Refusal(identity, reason) for identity, reason in refused.items()))


def plan_constants(repo: Path, ledger: ledger_api.Ledger, base: str, selected: Selection) -> PlannedEdits:
    """Plan each Cargo target against committed bytes; merge only identical shared-file edits."""
    sources = RenameSources()
    refused: dict[str, str] = {}
    findings: list[ledger_api.HeldFinding] = []
    for finding in selected.findings:
        current = current_finding(ledger, finding, base, sources)
        if isinstance(current, bulk.Refusal):
            refused[finding.finding_id] = current.reason
        else:
            findings.append(current)
    # Export the base once to reuse Cargo's existing target discovery, including configured roots.
    # Source snapshots never consult the checkout's Rust bytes or untracked files.
    entries = git(repo, "ls-tree", "-rz", base).split("\0")
    blobs: dict[str, str] = {}
    for entry in filter(None, entries):
        header, path = entry.split("\t", 1)
        mode, kind, blob = header.split()
        if kind == "blob" and mode in {"100644", "100755"}:
            blobs[path] = blob
    contents = {path: data for path, data in ledger_api.blob_contents(repo, blobs)}
    members = rules.workspace_members({path: data.decode() for path, data in contents.items()
                                      if Path(path).name == "Cargo.toml"})
    directories = tuple(member.directory for member in members)
    proofs: list[ConstantTargetProof] = []
    seen: set[str] = set()
    with tempfile.TemporaryDirectory(prefix="bulk-constant-sources-") as folder:
        export = Path(folder)
        for path, data in contents.items():
            target = export / path
            target.parent.mkdir(parents=True, exist_ok=True)
            _ = target.write_bytes(data)
        for member in members:
            owned = tuple(finding for finding in findings if style_proposals.crate_for(directories, finding.path) == member.directory)
            if not owned:
                continue
            try:
                targets = bulk.cargo_target_roots(export / member.directory)
            except (bulk.BulkRenameError, ValueError, OSError) as error:
                refused.update((finding.finding_id, str(error)) for finding in owned)
                continue
            for target in targets:
                entry = target.path.relative_to(export).as_posix()
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



def diagnostic_files(repo: Path, result: CommandResult) -> set[str]:
    paths: set[str] = set()
    def spans(message: dict[str, object]) -> None:
        for entry in bulk.object_list(message.get("spans", [])):
            span = bulk.object_fields(entry)
            name = str(span.get("file_name", ""))
            if name and not name.startswith("<"):
                resolved = (repo / name).resolve()
                if resolved.is_relative_to(repo):
                    paths.add(resolved.relative_to(repo).as_posix())
            expansion = span.get("expansion")
            if isinstance(expansion, dict):
                spans({"spans": [bulk.object_fields(cast(object, expansion)).get("span", {})]})
        for child in bulk.object_list(message.get("children", [])):
            spans(bulk.object_fields(child))
    for line in (result.stdout + "\n" + result.stderr).splitlines():
        try:
            message = bulk.object_fields(cast(object, json.loads(line)))
        except (ValueError, bulk.BulkRenameError):
            continue
        if message.get("reason") == "compiler-message":
            message = bulk.object_fields(message.get("message"))
        if message.get("level") == "error":
            spans(message)
    return paths


def validate(workspace: WorkspaceEdits, runner: Runner, times: dict[str, float],
             loops: list[dict[str, object]]) -> tuple[set[str], dict[str, str]]:
    kept = set(workspace.files)
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
            errors: set[str] = set()
            for result in failures:
                errors.update(diagnostic_files(workspace.repo, result))
            remove = set(errors)
            while True:
                expanded = set(remove)
                for path in remove:
                    expanded.update(workspace.dependencies.get(path, frozenset()))
                if expanded == remove:
                    break
                remove = expanded
            remove &= kept
            loop.update(errors=sorted(errors), reverted=sorted(remove))
            if not remove:
                raise bulk.BulkRenameError("validation errors remain but no new edited file can be reverted")
            workspace.restore(sorted(remove))
            kept -= remove
            reverted.update((item.finding.finding_id, "workspace diagnostics") for path in remove for item in workspace.files[path].kept)
        finally:
            seconds = time.monotonic() - started
            times["check_clippy"] += seconds
            loop["seconds"] = seconds
            loops.append(loop)


def tested_tree_proof(checkout: Path, tree: str, runner: Runner, times: dict[str, float]) -> CommandResult:
    """Test and lint the exact clean tree, then require its machine-wide final pass record.

    Keep all evidence checks here. A zero command exit alone never authorizes publication.
    The query fails closed until builds-unit's final-pass integration is available.
    """
    require_test_tree(checkout, tree)
    verify = str(Path.home() / ".claude/scripts/direct/verify.sh")
    for stage, arguments in (("verify_final", ("final",)), ("verify_lint", ("lint",)),
                             ("final_pass", ("final-pass", tree))):
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


def test_tree(repo: Path, commit: str, tree: str, runner: Runner, times: dict[str, float]) -> CommandResult:
    with tempfile.TemporaryDirectory(prefix="bulk-rule-test-") as folder:
        checkout = Path(folder) / "repo"
        _ = git(repo, "clone", "--shared", "--no-checkout", str(repo), str(checkout))
        _ = git(checkout, "checkout", "--detach", commit)
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


def run_rule(repo: Path, rule: str, base_tree: str, ledger: ledger_api.Ledger,
             server: bulk.RenameServer | RenameService = RenameService.ON_DEMAND,
             runner: Runner = run_command, available: Callable[[Path, str], None] = bulk.check_available) -> Path:
    repo = repo.resolve()
    if repo != ledger.repo.resolve():
        raise bulk.BulkRenameError("ledger belongs to a different checkout")
    intent = WorkspaceIntent(repo, f"bulk_run --rule {rule} --base-tree {base_tree} --repo {repo}")
    interrupted_tickets = intent.recover()
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
        selected = select(ledger, definition)
        owned = claim(ledger, selected, definition.family, git(repo, "symbolic-ref", "HEAD"), intent)
        # Claiming changes build_ticket; the shared site proof must receive that current row.
        selected = Selection(ledger.named_held(owned), selected.skipped)
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
    try:
        started = time.monotonic()
        with ExitStack() as stack:
            if definition.family == "names":
                rename_server = stack.enter_context(bulk.RustAnalyzer(repo)) if isinstance(server, RenameService) else server
                plan = plan_renames(repo, ledger, base, selected, rename_server, available)
            else:
                plan = plan_constants(repo, ledger, base, selected)
        refused.update((item.finding_id, item.reason) for item in plan.refusals)
        times["rename"] = time.monotonic() - started
        candidates = plan.files
        workspace.files = {file.path: file for file in candidates}
        workspace.dependencies = {file.path: file.dependencies | {file.path} for file in candidates}
        workspace.constant_proofs = plan.constant_proofs
        _ = require_base(repo, base_tree)
        for file in candidates:
            available(repo, file.path)
        for file in candidates:
            target = repo / file.path
            if target.is_symlink() or target.resolve() != target:
                raise bulk.BulkRenameError("edit path is a symlink or resolves outside its workspace path: " + file.path)
            if isinstance(file, ConstantFileEdit) and file.created and target.exists():
                raise bulk.BulkRenameError("constant creation would overwrite an untracked file: " + file.path)
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
            formatted = runner(repo, ("cargo", "+nightly", "fmt", *(arg for name in packages for arg in ("--package", name))))
            times["fmt"] = time.monotonic() - started
            if formatted.code:
                raise bulk.BulkRenameError("workspace format failed: " + formatted.stderr.strip())
            workspace.guard(kept)
            kept, reverted = validate(workspace, runner, times, loops)
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
    except (Exception, KeyboardInterrupt) as error:
        if git(repo, "rev-parse", "HEAD") != base:
            _ = git(repo, "reset", "--hard", base)
        workspace.rollback(intent)
        status = "batch aborted: " + str(error)
        for identity in owned:
            if identity not in refused:
                reverted[identity] = status
        for ticket in tickets:
            _ = ledger.bulk_release(ticket, {identity: reason for identity, reason in {**refused, **reverted}.items() if owned[identity] == ticket})
            ledger.ticket_drop(ticket)
        kept_findings = ()
        commit = ""
        if isinstance(error, KeyboardInterrupt):
            raise
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


class Arguments(argparse.Namespace):
    repo: Path = Path()
    rule: str = ""
    base_tree: str = ""


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--repo", type=Path, default=Path.cwd())
    _ = parser.add_argument("--rule", required=True)
    _ = parser.add_argument("--base-tree", required=True)
    args = parser.parse_args(argv, namespace=Arguments())
    try:
        if socket.gethostname().split(".")[0] != "natedev":
            raise bulk.BulkRenameError("workspace bulk tests must run on natedev")
        with ledger_api.open_ledger(args.repo) as ledger:
            report = run_rule(args.repo, args.rule, args.base_tree, ledger)
            status = bulk.object_fields(cast(object, json.loads(report.read_text()))).get("status", "")
            return 1 if (str(status).startswith(("tests failed:", "batch aborted:"))
                         or "publication pending:" in str(status)) else 0
    except (bulk.BulkRenameError, ledger_api.LedgerError, OSError, subprocess.SubprocessError) as error:
        print(f"bulk workspace: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
