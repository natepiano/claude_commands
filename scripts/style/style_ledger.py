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
import tempfile
import tomllib
import uuid
from collections import Counter
from collections.abc import Generator, Iterable, Iterator, Mapping
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path, PurePosixPath
from types import TracebackType
from typing import Literal, TypedDict, cast

import rules as rule_catalog
import style_rules
from candidate_generators import enumerate_candidates
from rules import Family, InputScope, MemberReach, Rule, Tier

CellState = Literal["current", "stale", "open", "awaiting merge"]
Verdict = Literal["clean", "fixed", "exception", "held", "waived"]
LintTool = Literal["clippy", "mend", "rustfmt"]


class DeclaredInputScope(StrEnum):
    RULE = "rule"
    FILE = "file"


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


class LedgerError(ValueError):
    """A ledger operation cannot represent the requested check."""


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


def _crate_filter(scope: CoverageScope) -> str | None:
    return scope.crate if isinstance(scope, CrateCoverage) else None


FindingKind = Literal["rename", "type", "restructure"]
HeldStateName = Literal["unfiled", "filed", "waived", "built"]
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
    """The approved finding has merged into the style branch."""


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


# What declining one finding did to the held judgment of its cell.
DeclineOutcome = CellWaived | CellStillHeld | InputChangedSinceJudged | NoHeldJudgment


@dataclass(frozen=True)
class NoBuildTicket:
    """No ticket holds this finding for a build."""


@dataclass(frozen=True)
class BuildTicket:
    ticket: str


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
    state: Unfiled | Filed | Waived | Built
    build_ticket: NoBuildTicket | BuildTicket


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
    crate_dirs: tuple[str, ...]
    rust_files: tuple[str, ...]

    def crate(self, path: str) -> str:
        return next((directory for directory in self.crate_dirs if _under(path, directory)), ".")


def _git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments], capture_output=True, text=True, check=False,
    )
    if result.returncode:
        raise LedgerError(result.stderr.strip() or f"git {' '.join(arguments)} failed")
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
        database.parent.mkdir(parents=True, exist_ok=True)
        self._connection: sqlite3.Connection = sqlite3.connect(database, timeout=30)
        _ = self._connection.execute("PRAGMA foreign_keys=ON")
        _ = self._connection.execute("BEGIN IMMEDIATE")
        _ = self._connection.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
        versions = cast(list[tuple[int]], self._connection.execute("SELECT version FROM schema_version").fetchall())
        version = versions[0][0] if versions else 0
        if version > max(MIGRATIONS):
            self.close()
            raise LedgerError(f"unsupported ledger schema version {version}")
        for number, migration in sorted(MIGRATIONS.items()):
            if number > version:
                for statement in migration.split(";"):
                    if statement.strip():
                        _ = self._connection.execute(statement)
                _ = self._connection.execute("DELETE FROM schema_version")
                _ = self._connection.execute("INSERT INTO schema_version VALUES (?)", (number,))
        self._connection.commit()

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
        with self._connection:
            _ = self._connection.execute(
                "INSERT INTO sync(id,tip,style_branch,synced_at) VALUES (1,?,?,?) ON CONFLICT(id) DO UPDATE SET tip=excluded.tip,style_branch=COALESCE(?,sync.style_branch),synced_at=excluded.synced_at",
                (commit, style_branch or "", _now(), style_branch),
            )
            self._catalog = rule_catalog.load_rules(self.repo)
            self._refresh_sites(self._tip(commit))

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
        with self._connection:
            _ = self._connection.execute("BEGIN IMMEDIATE")
            exists = cast(list[tuple[str]], self._connection.execute("SELECT id FROM tickets WHERE id=?", (id,)).fetchall())
            if exists:
                previous = self._ticket(id)
                if (previous["unit"], previous["kind"], previous["family"], previous["crate"], previous["files"], previous["rules"], previous["rule"], previous["sites"], previous["proposal"], previous["cells"], previous["finding_ids"]) != (unit, kind, family, crate, files, rules, rule, sites or [], proposal, [list(cell) for cell in cells], list(finding_ids)):
                    raise LedgerError(f"{id}: ticket already exists in state {previous['state']} with different fields")
                return
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
            self._catalog = rule_catalog.load_rules(self.repo)
            definitions = {rule_id: self._component_rule(rule_id) for rule_id in rules}
            pins = {rule_id: definition.blob for rule_id, definition in definitions.items()}
            for rule_id, definition in definitions.items():
                text = definition.path.read_bytes()
                result = subprocess.run(
                    ["git", "-C", str(self.repo), "hash-object", "--stdin"],
                    input=text, capture_output=True, check=False,
                )
                if result.returncode:
                    raise LedgerError(result.stderr.decode(errors="replace").strip() or "git hash-object failed")
                hashed = result.stdout.decode().strip()
                if hashed != pins[rule_id]:
                    raise LedgerError(f"{rule_id}: rule changed while pinning; rerun")
                _ = self._connection.execute(
                    "INSERT INTO rule_texts VALUES (?,?) ON CONFLICT(blob) DO NOTHING", (hashed, text),
                )
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

    def _ticket_transition(self, id: str, target: TicketState, commit: str = "", branch: str = "") -> None:
        with self._connection:
            _ = self._connection.execute("BEGIN IMMEDIATE")
            ticket = self._ticket(id)
            state = ticket["state"]
            if state == target:
                if target == "assigned" and ticket["branch"] != branch:
                    raise LedgerError(f"{id}: assigned to branch {ticket['branch']}")
                if target == "checkpointed" and ticket["checkpoint"] != commit:
                    raise LedgerError(f"{id}: checkpointed at {ticket['checkpoint']}")
                return
            predecessor = {"assigned": "queued", "checkpointed": "assigned", "merged": "checkpointed"}
            if (target == "dropped" and state in ("merged", "dropped")) or (target != "dropped" and predecessor.get(target) != state):
                raise LedgerError(f"{id}: cannot become {target} from {state}")
            if target == "checkpointed":
                missing = self.unjudged_ticket_cells(id)
                if missing:
                    raise LedgerError(f"{id}: unjudged cells: " + ", ".join(f"{cell.rule} {cell.path}" for cell in missing))
                self._restamp_checkpoint_judgments(ticket, commit)
            if target == "merged":
                self._restamp_checkpoint_judgments(ticket, ticket["checkpoint"])
            if target == "assigned":
                active = self.ticket_open(ticket["unit"])
                if active:
                    raise LedgerError(f"{id}: unit {ticket['unit']} already has {active[0]['id']} in state {active[0]['state']}")
            if target == "merged" and ticket["kind"] == "proposal":
                _ = self._connection.execute(
                    "UPDATE held_findings SET state='built',updated_at=? WHERE build_ticket=? AND state='filed'",
                    (_now(), id),
                )
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

    def ticket_assign(self, id: str, branch: str) -> None:
        branch = "refs/heads/" + branch.removeprefix("refs/heads/")
        _ = _git(self.repo, "check-ref-format", branch)
        self._ticket_transition(id, "assigned", branch=branch)

    def ticket_base(self, id: str) -> str:
        """Remember the assigned branch's start before publishing its first phase."""
        with self._connection:
            _ = self._connection.execute("BEGIN IMMEDIATE")
            ticket = self._ticket(id)
            if ticket["base"]:
                return ticket["base"]
            if ticket["state"] != "assigned":
                raise LedgerError(f"{id}: cannot record start commit in state {ticket['state']}")
            base = _git(self.repo, "rev-parse", "--verify", f"{ticket['branch']}^{{commit}}")
            _ = self._connection.execute("UPDATE tickets SET base=?,updated_at=? WHERE id=?", (base, _now(), id))
            return base

    def ticket_checkpoint(self, id: str, commit: str) -> None:
        self._ticket_transition(id, "checkpointed", _git(self.repo, "rev-parse", "--verify", f"{commit}^{{commit}}"))

    def _restamp_checkpoint_judgments(self, ticket: TicketRecord, checkpoint: str) -> None:
        """Carry this ticket's verdicts across edits made only by this ticket."""
        if not ticket["base"] or not self._is_ancestor(ticket["base"], checkpoint):
            return
        self._catalog = rule_catalog.load_rules(self.repo)
        tip = self._tip(checkpoint)
        # Commits inherited at the start or merged from another branch are not ours.
        own_commits = set(_git(self.repo, "rev-list", "--first-parent", checkpoint,
                               "^" + ticket["base"]).splitlines())
        rows = cast(list[tuple[int, str, str, str, str]], self._connection.execute(
            'SELECT j.id,j.rule,j.path,j."commit",j.declared_scope FROM judgments j WHERE j.ticket=? '
            + 'AND j.id=(SELECT latest.id FROM judgments latest WHERE latest.ticket=j.ticket '
            + 'AND latest.rule=j.rule AND latest.path=j.path ORDER BY latest.recorded_at DESC,latest.id DESC LIMIT 1)',
            (ticket["id"],),
        ).fetchall())
        for judgment_id, rule, path, judged_commit, declared in rows:
            try:
                definition = self._component_rule(rule)
                if definition.family != ticket["family"] or judged_commit == checkpoint:
                    continue
                if not self._is_ancestor(judged_commit, checkpoint):
                    continue
                changed_commits = set(_git(self.repo, "rev-list", "--full-history", checkpoint,
                                          "^" + judged_commit, "--", path).splitlines())
                if not changed_commits <= own_commits:
                    continue
                scope = _comparison_scope(DeclaredInputScope(declared), _cell_scope(definition, path, tip))
                fingerprint = self._fingerprint(tip, scope, path)
            except LedgerError:
                continue
            _ = self._connection.execute(
                'INSERT INTO judgments(rule,rule_blob,input_scope,input_fingerprint,path,"commit",verdict,verdict_detail,ticket,worker,recorded_at,declared_scope) '
                + 'SELECT rule,rule_blob,?,?,path,?,verdict,verdict_detail,ticket,worker,?,declared_scope FROM judgments WHERE id=? '
                + 'ON CONFLICT(rule,path,"commit",rule_blob,input_scope,input_fingerprint,ticket,worker) DO NOTHING',
                (scope, fingerprint, checkpoint, datetime.now(timezone.utc).isoformat(), judgment_id),
            )

    def ticket_merge(self, id: str) -> None:
        ticket = self._ticket(id)
        if ticket["state"] == "merged":
            return
        if ticket["state"] != "checkpointed":
            raise LedgerError(f"{id}: cannot become merged from {ticket['state']}")
        rows = cast(list[tuple[str]], self._connection.execute("SELECT style_branch FROM sync WHERE id=1").fetchall())
        branch = rows[0][0] if rows else ""
        branch_tip = _git(self.repo, "rev-parse", "--verify", f"{branch}^{{commit}}") if branch else ""
        if not branch_tip or not self._is_ancestor(ticket["checkpoint"], branch_tip):
            raise LedgerError(f"{id}: checkpoint {ticket['checkpoint'][:7]} is not on {branch or 'the style branch'}")
        self._ticket_transition(id, "merged")

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

    def ticket_drop(self, id: str) -> None:
        self._ticket_transition(id, "dropped")

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
        now = _now()
        with self._connection:
            _ = self._connection.execute(
                "INSERT INTO held_findings(finding_id,rule,path,line,text,kind,before,after,rule_blob,ticket,state,proposal,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,'unfiled','',?,?) ON CONFLICT(finding_id) DO NOTHING",
                (finding_id, rule, path, line, text, kind, before, after, rule_blob, ticket, now, now),
            )
        return self._held(finding_id)

    def _held(self, finding_id: str) -> HeldFinding:
        rows = cast(list[tuple[str, str, str, int, str, FindingKind, str, str, str, str, HeldStateName, str, str]], self._connection.execute(
            "SELECT finding_id,rule,path,line,text,kind,before,after,rule_blob,ticket,state,proposal,build_ticket FROM held_findings WHERE finding_id=?", (finding_id,),
        ).fetchall())
        if not rows:
            raise LedgerError(f"unknown held finding {finding_id}")
        row = rows[0]
        state = Filed(Path(row[11])) if row[10] == "filed" else Waived() if row[10] == "waived" else Built() if row[10] == "built" else Unfiled()
        return HeldFinding(row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7], row[8], row[9], state, BuildTicket(row[12]) if row[12] else NoBuildTicket())

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

        One transaction loads the rule catalog once and reads the held findings once.
        """
        with self._connection:
            _ = self._connection.execute("BEGIN IMMEDIATE")
            findings = tuple(self._held(finding_id) for finding_id in dict.fromkeys(finding_ids))
            built = next((finding for finding in findings if isinstance(finding.state, Built)), None)
            if built is not None:
                raise LedgerError(f"{built.finding_id}: finding already built")
            if not findings:
                return {}
            tip = self._synced_tip()
            self._catalog = rule_catalog.load_rules(self.repo)
            now = _now()
            _ = self._connection.executemany("UPDATE held_findings SET state='waived',updated_at=? WHERE finding_id=?",
                                             [(now, finding.finding_id) for finding in findings])
            definitions: dict[str, Rule | None] = {}
            for rule in sorted({finding.rule for finding in findings}):
                try:
                    definitions[rule] = self._component_rule(rule)
                except LedgerError:
                    definitions[rule] = None

            def cell_path(definition: Rule, path: str) -> str:
                return tip.crate(path) if definition.tier == "D" and definition.input_scope == "manifest" else path

            remaining = cast(list[tuple[str, str]], self._connection.execute(
                "SELECT rule,path FROM held_findings WHERE state NOT IN ('waived','built')",
            ).fetchall())
            still_held = {(rule, cell_path(definition, path)) for rule, path in remaining
                          if (definition := definitions.get(rule)) is not None}
            cells: dict[tuple[str, str], tuple[Rule, list[HeldFinding]]] = {}
            outcomes: dict[str, DeclineOutcome] = {}
            for finding in findings:
                definition = definitions[finding.rule]
                if definition is None:
                    outcomes[finding.finding_id] = InputChangedSinceJudged()
                else:
                    cells.setdefault((finding.rule, cell_path(definition, finding.path)), (definition, []))[1].append(finding)
            for (rule, path), (definition, members) in cells.items():
                outcome = self._decline_cell(rule, path, definition, tip, members, (rule, path) in still_held, words)
                for finding in members:
                    outcomes[finding.finding_id] = outcome
            return {finding.finding_id: outcomes[finding.finding_id] for finding in findings}

    def _decline_cell(self, rule: str, path: str, definition: Rule, tip: RepositoryTip,
                      members: list[HeldFinding], held_by_others: bool, words: str) -> DeclineOutcome:
        rows = cast(list[tuple[int, str, str, str, str, str, str, str, str, str]], self._connection.execute(
            'SELECT id,rule,path,"commit",ticket,worker,rule_blob,input_scope,input_fingerprint,declared_scope FROM judgments '
            + "WHERE rule=? AND path=? AND verdict='held' ORDER BY recorded_at DESC,id DESC",
            (rule, path),
        ).fetchall())
        if not rows:
            return NoHeldJudgment()
        row = rows[0]
        judgment = HeldJudgment(*row[1:7])
        newest = cast(tuple[int], self._connection.execute(
            "SELECT id FROM judgments WHERE rule=? AND path=? ORDER BY recorded_at DESC,id DESC LIMIT 1",
            (rule, path),
        ).fetchone())[0]
        held_row = Judgment(row[6], row[8], row[3], "held", CellInputScope(row[7]), DeclaredInputScope(row[9]))
        try:
            unchanged = (self._judgment_inputs_match(held_row, tip, _cell_scope(definition, path, tip), path)
                         and self._is_ancestor(row[3], tip.commit))
        except LedgerError:
            unchanged = False
        if (newest != row[0] or row[6] != definition.blob
                or any(finding.rule_blob != definition.blob for finding in members) or not unchanged):
            return InputChangedSinceJudged()
        if held_by_others:
            return CellStillHeld(judgment)
        _ = self._connection.execute(
            "UPDATE judgments SET verdict='waived',verdict_detail=?,recorded_at=? WHERE id=?", (words, _now(), row[0]),
        )
        return CellWaived(judgment)

    def file_held(self, finding_id: str, proposal: Path) -> None:
        with self._connection:
            _ = self._connection.execute("BEGIN IMMEDIATE")
            finding = self._held(finding_id)
            if isinstance(finding.state, Filed) and finding.state.proposal == proposal:
                return
            if not isinstance(finding.state, Unfiled):
                state = "filed" if isinstance(finding.state, Filed) else "built" if isinstance(finding.state, Built) else "waived"
                raise LedgerError(f"{finding_id}: cannot file finding in state {state}")
            _ = self._connection.execute("UPDATE held_findings SET state='filed',proposal=?,updated_at=? WHERE finding_id=?", (str(proposal), _now(), finding_id))

    def waive_held(self, finding_id: str) -> None:
        with self._connection:
            _ = self._connection.execute("BEGIN IMMEDIATE")
            finding = self._held(finding_id)
            if isinstance(finding.state, Waived):
                return
            _ = self._connection.execute("UPDATE held_findings SET state='waived',updated_at=? WHERE finding_id=?", (_now(), finding_id))

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
                self.ticket_merge(id)
                changes.append(f"ticket {id}: checkpointed -> merged")
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

    def record(
        self, rule: str, path: str, commit: str, rule_blob: str, verdict: Verdict,
        detail: str = "", ticket: str = "", worker: str = "", *,
        input_scope: DeclaredInputScope = DeclaredInputScope.RULE,
    ) -> None:
        if input_scope == DeclaredInputScope.FILE and verdict not in ("clean", "exception"):
            raise LedgerError(f"{verdict}: file input scope requires clean or exception")
        self._catalog = rule_catalog.load_rules(self.repo)
        definition = self._component_rule(rule)
        rule_blob = self._check_pin(ticket, rule, rule_blob)
        path = self._path(path)
        if ticket and self._connection.execute("SELECT 1 FROM tickets WHERE id=?", (ticket,)).fetchone():
            owned = self._ticket(ticket)["cells"]
            if owned and [rule, path] not in owned:
                raise LedgerError(f"{ticket}: {rule} {path} is outside ticket cells")
        if isinstance(definition.reach, MemberReach) and not _under(path, definition.reach.crate_dir.as_posix()):
            raise LedgerError(f"{rule}: {path} is outside rule reach")
        if definition.tier in ("A", "E"):
            raise LedgerError(f"{rule}: tier {definition.tier} has no judgment cells in this phase")
        tip = self._tip(commit)
        scope = _cell_scope(definition, path, tip)
        if definition.tier == "B":
            synced = self._synced_tip()
            self._refresh_sites(synced)
            if path not in {site.path for site in self._listed_sites(rule, synced)}:
                raise LedgerError(f"{rule}: {path} holds no listed site at the synced tip")
            if not path.endswith(".rs"):
                scope = CellInputScope.FILE
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
        if input_scope == DeclaredInputScope.FILE:
            if scope == CellInputScope.FILE:
                input_scope = DeclaredInputScope.RULE
            elif scope == CellInputScope.MANIFEST or path not in tip.rust_files:
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
        branches = cast(list[tuple[str]], self._connection.execute("SELECT style_branch FROM sync WHERE id=1").fetchall())
        style_branch = branches[0][0] if branches else ""
        for row in rows:
            if row.declared_scope != rows[0].declared_scope or row.rule_blob != rule.blob:
                continue
            try:
                ancestor = self._is_ancestor(row.commit, tip.commit)
            except LedgerError:
                continue
            if ancestor and self._judgment_inputs_match(row, tip, scope, path):
                return "current", row.verdict
            if not ancestor and style_branch:
                try:
                    if style_branch not in self._branch_tips:
                        self._branch_tips[style_branch] = _git(self.repo, "rev-parse", "--verify", f"{style_branch}^{{commit}}")
                    branch_commit = self._branch_tips[style_branch]
                    checked_tip = self._tip(branch_commit)
                    if self._is_ancestor(row.commit, branch_commit) and self._judgment_inputs_match(row, checked_tip, scope, path):
                        if not pending:
                            pending = row.verdict
                except LedgerError:
                    continue
        if pending:
            return "awaiting merge", pending
        return ("stale" if rows else "open"), ""

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
                        judgment_rule = replace(component, input_scope="file") if component.tier == "B" and not path.endswith(".rs") else component
                        state, verdict = self._judgment_state(judgment_rule, path, tip)
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
            with (closing(sqlite3.connect(location.database.resolve().as_uri() + "?mode=ro", uri=True)) as source,
                  closing(sqlite3.connect(database)) as destination):
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
    input_scope: DeclaredInputScope = DeclaredInputScope.RULE
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
    result["state"] = "filed" if isinstance(finding.state, Filed) else "waived" if isinstance(finding.state, Waived) else "built" if isinstance(finding.state, Built) else "unfiled"
    if isinstance(finding.state, Filed):
        result["proposal"] = str(finding.state.proposal)
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
    _ = record.add_argument("--input-scope", choices=(DeclaredInputScope.FILE,), type=DeclaredInputScope, default=DeclaredInputScope.RULE)
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
        ("held", ("add", "file", "waive", "list")),
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
                _ = command.add_argument("--state", choices=("unfiled", "filed", "waived", "built"), default=None)
                _ = command.add_argument("--json", action="store_true")
            else:
                _ = command.add_argument("--id", required=True)
                if action == "file":
                    _ = command.add_argument("--proposal", required=True)
    args = parser.parse_args(namespace=LedgerArguments())
    if not args.repo:
        parser.error("--repo is required")
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
                    ledger.ticket_merge(args.id)
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
                elif args.action == "file":
                    ledger.file_held(args.id, Path(args.proposal))
                elif args.action == "waive":
                    ledger.waive_held(args.id)
                else:
                    findings = ledger.held_findings(HeldFindingsInState(args.state) if args.state else AllHeldFindings())
                    if args.json:
                        print(json.dumps([_held_json(finding) for finding in findings], sort_keys=True))
                    else:
                        for finding in findings:
                            print(f"{finding.finding_id}\t{finding.rule}\t{finding.path}:{finding.line}\t{_held_json(finding)['state']}")
    except (LedgerError, rule_catalog.RuleValidationError, OSError, sqlite3.Error, ValueError) as error:
        parser.exit(2, f"style_ledger: {error}\n")


if __name__ == "__main__":
    main()
