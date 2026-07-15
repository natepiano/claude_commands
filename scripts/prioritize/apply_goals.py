#!/usr/bin/env python3
"""Apply one approved Hanadocs strategic-goals note and rerank the backlog."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import renumber  # pyright: ignore[reportImplicitRelativeImport]
import review_hash  # pyright: ignore[reportImplicitRelativeImport]
import writer_lock  # pyright: ignore[reportImplicitRelativeImport]


VAULT_PATH = Path("/Users/natemccoy/rust/hanadocs")
ISSUES_PATH = VAULT_PATH / "issues"
GOALS_PATH = VAULT_PATH / "prioritization goals.md"
PRODUCTION_SCOPE = renumber.Scope(VAULT_PATH, ISSUES_PATH, GOALS_PATH)
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
MANIFEST_FIELDS = {"expected_goals_hash", "evidence_hashes", "updated_content"}


class GoalsApplyError(RuntimeError):
    """An approved goals update cannot be validated or applied safely."""


@dataclass(frozen=True)
class Runtime:
    scope: renumber.Scope
    writer_lock_path: Path


PRODUCTION_RUNTIME = Runtime(
    scope=PRODUCTION_SCOPE,
    writer_lock_path=writer_lock.LOCK_PATH,
)


@dataclass(frozen=True)
class GoalsPlan:
    manifest_source: renumber.SourceFile
    current_source: renumber.SourceFile
    expected_goals_hash: str
    evidence_hashes: Mapping[Path, str]
    updated_content: bytes
    goals: tuple[renumber.Goal, ...]

    @property
    def changes_content(self) -> bool:
        return self.updated_content != self.current_source.content


def _prepared_source(path: Path, content: bytes, mode: int) -> renumber.SourceFile:
    return renumber.SourceFile(
        path=path,
        content=content,
        mode=mode,
        signature=(0, 0, 0, 0, 0),
        digest=hashlib.sha256(content).hexdigest(),
    )


def _read_manifest(
    path: Path,
) -> tuple[renumber.SourceFile, str, dict[Path, str], bytes]:
    try:
        source = renumber._read_source(path)
        text = source.content.decode("utf-8")
    except (renumber.PlanningError, UnicodeDecodeError) as error:
        raise GoalsApplyError(f"cannot read goals approval manifest {path}: {error}") from error

    try:
        record = json.loads(text)
    except json.JSONDecodeError as error:
        raise GoalsApplyError(f"invalid goals approval JSON: {error.msg}") from error
    if not isinstance(record, dict):
        raise GoalsApplyError("goals approval manifest must be one JSON object")
    if set(record) != MANIFEST_FIELDS:
        raise GoalsApplyError(
            "goals approval manifest must contain exactly "
            "expected_goals_hash, evidence_hashes, and updated_content"
        )

    expected_hash = record["expected_goals_hash"]
    evidence_hashes = record["evidence_hashes"]
    updated_content = record["updated_content"]
    if not isinstance(expected_hash, str) or HASH_RE.fullmatch(expected_hash) is None:
        raise GoalsApplyError(
            "expected_goals_hash must be 64 lowercase hexadecimal digits"
        )
    if not isinstance(evidence_hashes, dict):
        raise GoalsApplyError("evidence_hashes must be an object")
    parsed_evidence: dict[Path, str] = {}
    for evidence_path, evidence_hash in evidence_hashes.items():
        if not isinstance(evidence_path, str):
            raise GoalsApplyError("evidence paths must be strings")
        if not isinstance(evidence_hash, str) or HASH_RE.fullmatch(evidence_hash) is None:
            raise GoalsApplyError(f"invalid evidence hash for {evidence_path}")
        parsed_evidence[Path(evidence_path)] = evidence_hash
    if not isinstance(updated_content, str):
        raise GoalsApplyError("updated_content must be a complete UTF-8 note string")
    try:
        updated_bytes = updated_content.encode("utf-8")
    except UnicodeEncodeError as error:
        raise GoalsApplyError("updated_content is not valid UTF-8 text") from error
    return source, expected_hash, parsed_evidence, updated_bytes


def _evidence_source(path: Path, scope: renumber.Scope) -> renumber.SourceFile:
    if not path.is_absolute() or ".." in path.parts or path.suffix != ".md":
        raise GoalsApplyError(f"invalid linked evidence path: {path}")
    if path == scope.goals:
        raise GoalsApplyError(
            "the goals note is guarded by expected_goals_hash, not evidence_hashes"
        )
    try:
        path.relative_to(scope.vault)
        resolved_vault = scope.vault.resolve(strict=True)
        resolved_path = path.resolve(strict=True)
    except (OSError, ValueError) as error:
        raise GoalsApplyError(f"linked evidence is outside the fixed vault: {path}") from error
    if not resolved_path.is_relative_to(resolved_vault):
        raise GoalsApplyError(f"linked evidence leaves the fixed vault: {path}")
    relative = path.relative_to(scope.vault)
    cursor = scope.vault
    for part in relative.parts:
        cursor /= part
        try:
            if stat.S_ISLNK(cursor.lstat().st_mode):
                raise GoalsApplyError(f"linked evidence uses a symlink: {path}")
        except OSError as error:
            raise GoalsApplyError(f"cannot inspect linked evidence {path}: {error}") from error
    try:
        return renumber._read_source(path)
    except renumber.PlanningError as error:
        raise GoalsApplyError(f"cannot read linked evidence {path}: {error}") from error


def _evidence_hash(path: Path, scope: renumber.Scope) -> str:
    source = _evidence_source(path, scope)
    if path.parent == scope.issues:
        return review_hash.review_hash_for_source(source)
    return source.digest


def _validate_evidence_hashes(
    evidence_hashes: Mapping[Path, str],
    scope: renumber.Scope,
) -> None:
    for path, expected in evidence_hashes.items():
        if _evidence_hash(path, scope) != expected:
            raise GoalsApplyError(f"linked goal evidence changed after review: {path}")


def prepare_plan(
    manifest_path: Path,
    scope: renumber.Scope = PRODUCTION_SCOPE,
) -> GoalsPlan:
    manifest_source, expected_hash, evidence_hashes, updated_content = _read_manifest(
        manifest_path
    )
    try:
        current_source = renumber._read_source(scope.goals)
    except renumber.PlanningError as error:
        raise GoalsApplyError(f"cannot read current goals note: {error}") from error
    if current_source.digest != expected_hash:
        raise GoalsApplyError(
            "strategic goals changed after approval "
            f"({expected_hash} != {current_source.digest})"
        )
    _validate_evidence_hashes(evidence_hashes, scope)

    prepared = _prepared_source(scope.goals, updated_content, current_source.mode)
    try:
        goals = renumber.parse_goals(prepared)
    except renumber.PlanningError as error:
        raise GoalsApplyError(f"approved goals note is invalid: {error}") from error
    return GoalsPlan(
        manifest_source=manifest_source,
        current_source=current_source,
        expected_goals_hash=expected_hash,
        evidence_hashes=evidence_hashes,
        updated_content=updated_content,
        goals=goals,
    )


def _capture_issue_baseline(
    scope: renumber.Scope,
) -> tuple[tuple[Path, ...], dict[Path, renumber.SourceFile]]:
    try:
        paths = renumber._discover_issue_paths(scope)
        before = {path: renumber._read_source(path) for path in paths}
        if renumber._discover_issue_paths(scope) != paths:
            raise GoalsApplyError(
                "issue files were added, removed, or renamed during discovery"
            )
    except renumber.PlanningError as error:
        raise GoalsApplyError(f"cannot capture issue baseline: {error}") from error
    return paths, before


def _verify_ranking_baseline(
    ranking_plan: renumber.RankingPlan,
    expected_paths: tuple[Path, ...],
    before: Mapping[Path, renumber.SourceFile],
    updated_goals: bytes,
) -> None:
    if tuple(issue.source.path for issue in ranking_plan.issues) != expected_paths:
        raise GoalsApplyError("issue membership changed before reranking")
    if ranking_plan.goals_source.content != updated_goals:
        raise GoalsApplyError("goals note changed before reranking")
    for issue in ranking_plan.issues:
        baseline = before[issue.source.path]
        if (
            issue.source.signature != baseline.signature
            or issue.source.digest != baseline.digest
        ):
            raise GoalsApplyError(
                f"issue changed before reranking: {issue.source.path}"
            )


def _validate_final_state(
    plan: GoalsPlan,
    scope: renumber.Scope,
    expected_paths: tuple[Path, ...],
) -> None:
    current_goals = renumber._read_source(scope.goals)
    if (
        current_goals.content != plan.updated_content
        or current_goals.mode != plan.current_source.mode
    ):
        raise GoalsApplyError("goals note changed during apply")
    if renumber.parse_goals(current_goals) != plan.goals:
        raise GoalsApplyError("final goals structure differs from the approved goals")

    validation = renumber.build_plan(scope)
    if tuple(issue.source.path for issue in validation.issues) != expected_paths:
        raise GoalsApplyError("issue membership changed during final validation")
    if validation.goals_source.content != plan.updated_content:
        raise GoalsApplyError("goals note changed during final validation")
    if validation.changes:
        raise GoalsApplyError(
            "post-apply validation found remaining score or rank changes"
        )

    renumber._assert_source_unchanged(validation.goals_source)
    renumber._assert_issue_membership_unchanged(validation)
    for issue in validation.issues:
        renumber._assert_source_unchanged(issue.source)


def _rollback_issues(
    before: Mapping[Path, renumber.SourceFile],
    owned_contents: Mapping[Path, set[bytes]],
) -> list[str]:
    failures: list[str] = []
    for path in reversed(tuple(before)):
        owned = owned_contents.get(path)
        if not owned:
            continue
        source = before[path]
        try:
            current = renumber._read_source(path)
            if current.content == source.content:
                continue
            if current.content not in owned or current.mode != source.mode:
                failures.append(f"did not overwrite concurrently changed file {path}")
                continue
            renumber._atomic_write(path, source.content, source.mode)
        except (renumber.PlanningError, OSError) as error:
            failures.append(f"could not restore {path}: {error}")
    return failures


def _rollback_goals(plan: GoalsPlan, goal_was_written: bool) -> list[str]:
    if not goal_was_written:
        return []
    try:
        current = renumber._read_source(plan.current_source.path)
        if current.content == plan.current_source.content:
            return []
        if (
            current.content != plan.updated_content
            or current.mode != plan.current_source.mode
        ):
            return [
                "did not overwrite concurrently changed goals note "
                f"{plan.current_source.path}"
            ]
        renumber._atomic_write(
            plan.current_source.path,
            plan.current_source.content,
            plan.current_source.mode,
        )
    except (renumber.PlanningError, OSError) as error:
        return [f"could not restore {plan.current_source.path}: {error}"]
    return []


def apply_manifest(
    manifest_path: Path,
    runtime: Runtime = PRODUCTION_RUNTIME,
) -> GoalsPlan:
    try:
        with writer_lock.acquire_writer_lock(runtime.writer_lock_path):
            plan = prepare_plan(manifest_path, runtime.scope)
            expected_paths, before = _capture_issue_baseline(runtime.scope)
            owned_issue_contents: dict[Path, set[bytes]] = {}
            goal_was_written = False
            try:
                renumber._assert_source_unchanged(plan.current_source)
                _validate_evidence_hashes(plan.evidence_hashes, runtime.scope)
                if plan.changes_content:
                    renumber._atomic_write(
                        plan.current_source.path,
                        plan.updated_content,
                        plan.current_source.mode,
                    )
                    goal_was_written = True

                ranking_plan = renumber.build_plan(runtime.scope)
                _verify_ranking_baseline(
                    ranking_plan,
                    expected_paths,
                    before,
                    plan.updated_content,
                )
                for change in ranking_plan.changes:
                    owned_issue_contents.setdefault(change.source.path, set()).add(
                        change.updated
                    )
                renumber.apply_plan(ranking_plan)
                _validate_final_state(plan, runtime.scope, expected_paths)
                _validate_evidence_hashes(plan.evidence_hashes, runtime.scope)
                return plan
            except (
                GoalsApplyError,
                renumber.PlanningError,
                renumber.ApplyError,
                OSError,
            ) as error:
                rollback_failures = _rollback_issues(before, owned_issue_contents)
                rollback_failures.extend(_rollback_goals(plan, goal_was_written))
                suffix = ""
                if rollback_failures:
                    suffix = "; rollback warnings: " + "; ".join(rollback_failures)
                raise GoalsApplyError(
                    f"approved goals apply failed: {error}{suffix}"
                ) from error
    except writer_lock.WriterLockError as error:
        raise GoalsApplyError(str(error)) from error


def print_plan(plan: GoalsPlan, *, mode: str) -> None:
    print(f"Mode: {mode}")
    print(f"Goals note: {plan.current_source.path}")
    print(f"Approved goals: {len(plan.goals)}")
    print(f"Goals content changing: {'yes' if plan.changes_content else 'no'}")


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="approved goals JSON manifest")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="apply the complete goals note and rerank under the shared writer lock",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _argument_parser().parse_args(argv)
    try:
        if not arguments.apply:
            plan = prepare_plan(arguments.manifest, PRODUCTION_SCOPE)
            print_plan(plan, mode="dry-run (no files written)")
            print("Run with --apply to replace the goals note and rerank.")
            return 0
        plan = apply_manifest(arguments.manifest, PRODUCTION_RUNTIME)
        print_plan(plan, mode="apply")
        print("Approved goals applied; score and rank are canonical.")
        return 0
    except GoalsApplyError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
