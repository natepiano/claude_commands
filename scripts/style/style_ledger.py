#!/usr/bin/env python3
"""Store style judgments and derive coverage at a repository's synced tip."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import sqlite3
import subprocess
import tomllib
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from types import TracebackType
from typing import Literal, cast

import rules as rule_catalog
import style_rules
from rules import Family, InputScope, MemberReach, Rule, Tier

CellState = Literal["current", "stale", "open", "awaiting merge"]
Verdict = Literal["clean", "fixed", "exception", "held", "waived"]
LintTool = Literal["clippy", "mend", "rustfmt"]


class LedgerError(ValueError):
    """A ledger operation cannot represent the requested check."""


@dataclass(frozen=True)
class Cell:
    rule: str
    path: str
    crate: str
    tier: Tier
    family: Family | None
    state: CellState


@dataclass(frozen=True)
class RuleProgress:
    rule_id: str
    tier: Tier
    family: Family | None
    cells_current: int
    cells_total: int
    done: bool


@dataclass(frozen=True)
class Judgment:
    rule_blob: str
    input_fingerprint: str
    commit: str
    verdict: Verdict


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


def _fingerprint(repo: Path, tip: RepositoryTip, scope: InputScope, path: str) -> str:
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
}


class Ledger:
    def __init__(self, repo: Path, database: Path) -> None:
        self.repo: Path = repo
        self.database: Path = database
        self._catalog: tuple[Rule, ...] = rule_catalog.load_rules(repo)
        self._tips: dict[str, RepositoryTip] = {}
        self._fingerprints: dict[tuple[str, InputScope, str], str] = {}
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

    def _synced_tip(self) -> RepositoryTip:
        rows = cast(list[tuple[str]], self._connection.execute("SELECT tip FROM sync WHERE id=1").fetchall())
        if not rows:
            raise LedgerError("no synced tip; run set-tip first")
        return self._tip(rows[0][0])

    def _tip(self, commit: str) -> RepositoryTip:
        commit = _git(self.repo, "rev-parse", "--verify", f"{commit}^{{commit}}")
        if commit not in self._tips:
            self._tips[commit] = _tip(self.repo, commit)
        return self._tips[commit]

    def _fingerprint(self, tip: RepositoryTip, scope: InputScope, path: str) -> str:
        key = (tip.commit, scope, tip.crate(path) if scope == "crate" else path if scope == "file" else ".")
        if key not in self._fingerprints:
            self._fingerprints[key] = _fingerprint(self.repo, tip, scope, path)
        return self._fingerprints[key]

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
                "INSERT INTO sync(id,tip,style_branch) VALUES (1,?,?) ON CONFLICT(id) DO UPDATE SET tip=excluded.tip,style_branch=COALESCE(?,sync.style_branch)",
                (commit, style_branch or "", style_branch),
            )

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

    def _path(self, path: str) -> str:
        candidate = PurePosixPath(path)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise LedgerError(f"path must be relative to the repository: {path}")
        return str(candidate)

    def record(
        self, rule: str, path: str, commit: str, rule_blob: str, verdict: Verdict,
        detail: str = "", ticket: str = "", worker: str = "",
    ) -> None:
        self._catalog = rule_catalog.load_rules(self.repo)
        definition = self._rule(rule)
        path = self._path(path)
        if isinstance(definition.reach, MemberReach) and not _under(path, definition.reach.crate_dir.as_posix()):
            raise LedgerError(f"{rule}: {path} is outside rule reach")
        if definition.tier in ("A", "B", "E"):
            raise LedgerError(f"{rule}: tier {definition.tier} has no judgment cells in this phase")
        tip = self._tip(commit)
        if definition.tier == "D":
            crates = {tip.crate(file) for file in tip.rust_files}
            if path not in crates:
                raise LedgerError(f"{rule}: {path} must name a crate with Rust files")
        elif path not in tip.rust_files:
            raise LedgerError(f"{rule}: {path} is not an included Rust file")
        fingerprint = self._fingerprint(tip, definition.input_scope, path)
        with self._connection:
            _ = self._connection.execute(
                'INSERT INTO judgments(rule,rule_blob,input_scope,input_fingerprint,path,"commit",verdict,verdict_detail,ticket,worker,recorded_at) '
                + 'VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(rule,path,"commit",rule_blob,input_scope,input_fingerprint,ticket,worker) '
                + 'DO UPDATE SET verdict=excluded.verdict,verdict_detail=excluded.verdict_detail,recorded_at=excluded.recorded_at',
                (rule, rule_blob, definition.input_scope, fingerprint, path, tip.commit, verdict, detail, ticket, worker,
                 datetime.now(timezone.utc).isoformat()),
            )

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
        rows = cast(list[tuple[str, str, str, Verdict]], self._connection.execute(
            'SELECT rule_blob,input_fingerprint,"commit",verdict FROM judgments WHERE rule=? AND path=? ORDER BY recorded_at DESC,id DESC', (rule, path),
        ).fetchall())
        return tuple(Judgment(*row) for row in rows)

    def _judgment_state(self, rule: Rule, path: str, tip: RepositoryTip) -> tuple[CellState, str]:
        rows = self._judgments(rule.rule_id, path)
        fingerprint = self._fingerprint(tip, rule.input_scope, path)
        pending = ""
        branches = cast(list[tuple[str]], self._connection.execute("SELECT style_branch FROM sync WHERE id=1").fetchall())
        style_branch = branches[0][0] if branches else ""
        for row in rows:
            if row.rule_blob != rule.blob:
                continue
            ancestor = self._is_ancestor(row.commit, tip.commit)
            if ancestor and row.input_fingerprint == fingerprint:
                return "current", row.verdict
            if not ancestor and style_branch:
                try:
                    if style_branch not in self._branch_tips:
                        self._branch_tips[style_branch] = _git(self.repo, "rev-parse", "--verify", f"{style_branch}^{{commit}}")
                    branch_commit = self._branch_tips[style_branch]
                    checked_tip = self._tip(branch_commit)
                    if self._is_ancestor(row.commit, branch_commit) and self._fingerprint(checked_tip, rule.input_scope, path) == row.input_fingerprint:
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
        lint_tools: dict[LintTool, LintCoverage] = {}
        if rule_id is not None and not any(rule.rule_id == rule_id for rule in self._catalog):
            raise LedgerError(f"unknown rule {rule_id}")
        for definition in self._catalog:
            if rule_id is not None and definition.rule_id != rule_id:
                continue
            if definition.tier in ("B", "E", "unclassified"):
                continue
            files = [path for path in tip.rust_files if not isinstance(definition.reach, MemberReach)
                     or _under(path, definition.reach.crate_dir.as_posix())]
            paths = sorted({tip.crate(path) for path in files}) if definition.tier == "D" else files
            lint_coverage = LintCoverage(False, False, {}, {})
            if definition.tier == "A":
                tool = cast(LintTool, style_rules.parse_frontmatter(definition.path)["mechanism"][0])
                if tool not in lint_tools:
                    lint_tools[tool] = self._lint_coverage(tool, tip)
                lint_coverage = lint_tools[tool]
            for path in paths:
                cell_crate = path if definition.tier == "D" else tip.crate(path)
                if crate is not None and cell_crate != crate:
                    continue
                if definition.tier == "A":
                    state: CellState = "open"
                    if lint_coverage.has_runs:
                        state = "current" if (lint_coverage.passed and lint_coverage.diagnostics.get(path, 0) == 0
                                              and lint_coverage.rule_blobs.get(definition.rule_id) == definition.blob) else "stale"
                    verdict = ""
                else:
                    state, verdict = self._judgment_state(definition, path, tip)
                yield Cell(definition.rule_id, path, cell_crate, definition.tier, definition.family, state), verdict

    def cells(self, rule: str | None = None, crate: str | None = None) -> Iterator[Cell]:
        for cell, _ in self._coverage(rule, crate):
            yield cell

    def rules(self) -> Iterator[RuleProgress]:
        cells = tuple(self.cells())
        for definition in self._catalog:
            if definition.tier == "unclassified":
                continue
            own = [cell for cell in cells if cell.rule == definition.rule_id]
            current = sum(cell.state == "current" for cell in own)
            yield RuleProgress(definition.rule_id, definition.tier, definition.family, current, len(own),
                               definition.tier != "B" and current == len(own))


    def status(self, rule: str | None = None, crate: str | None = None, as_json: bool = False) -> None:
        coverage = tuple(self._coverage(rule, crate))
        unclassified = self.unclassified_rules()
        if as_json:
            print(json.dumps({"cells": [asdict(cell) for cell, _ in coverage], "unclassified_rules": unclassified}, sort_keys=True))
        else:
            if unclassified:
                print(f"unclassified: {len(unclassified)} rules — pull nate_style")
            print("rule\tcurrent\tstale\topen\tawaiting merge\theld\tdone")
            for progress in self.rules():
                if rule and progress.rule_id != rule:
                    continue
                own = [(cell, verdict) for cell, verdict in coverage if cell.rule == progress.rule_id]
                counts = [str(sum(cell.state == state for cell, _ in own)) for state in ("current", "stale", "open", "awaiting merge")]
                held = sum(cell.state == "current" and verdict == "held" for cell, verdict in own)
                print("\t".join([progress.rule_id, *counts, str(held), str(progress.done).lower()]))



def open_ledger(repo: Path) -> Ledger:
    repo = Path(_git(repo.expanduser().resolve(), "rev-parse", "--show-toplevel"))
    common = Path(_git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    main_repo = common.parent
    state = Path(os.environ.get("STYLE_LEDGER_STATE_DIR", str(Path.home() / ".local/state/style"))).expanduser()
    return Ledger(repo, state / main_repo.name / "ledger.sqlite")


class LedgerArguments(argparse.Namespace):
    command: str = ""
    repo: str = ""
    rule: str = ""
    path: str = ""
    commit: str = ""
    rule_blob: str = ""
    verdict: Verdict = "clean"
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    record = commands.add_parser("record")
    for field in ("rule", "path", "commit", "rule-blob", "ticket", "worker"):
        _ = record.add_argument(f"--{field}", required=True)
    _ = record.add_argument("--verdict", choices=("clean", "fixed", "exception", "held", "waived"), required=True)
    _ = record.add_argument("--detail", default="")
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
    for command in (record, lint, sync, status):
        _ = command.add_argument("--repo", required=True)
    args = parser.parse_args(namespace=LedgerArguments())
    try:
        with open_ledger(Path(args.repo)) as ledger:
            if args.command == "set-tip":
                ledger.set_tip(args.tip, args.style_branch)
            elif args.command == "record":
                ledger.record(args.rule, args.path, args.commit, args.rule_blob, args.verdict, args.detail, args.ticket, args.worker)
            elif args.command == "lint-record":
                value = cast(object, json.loads(Path(args.diagnostics).read_text()))
                if not isinstance(value, dict) or any(not isinstance(key, str) or type(count) is not int or count < 0
                                                      for key, count in cast(dict[object, object], value).items()):
                    raise LedgerError("diagnostics must be an object of file paths to nonnegative counts")
                ledger.lint_record(args.tip, args.tool, args.passed == "1", cast(dict[str, int], value))
            else:
                ledger.status(args.rule or None, args.crate or None, args.json)
    except (LedgerError, rule_catalog.RuleValidationError, OSError, sqlite3.Error, ValueError) as error:
        parser.exit(2, f"style_ledger: {error}\n")


if __name__ == "__main__":
    main()
