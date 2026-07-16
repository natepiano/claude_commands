#!/usr/bin/env python3
"""Apply user-approved Hanadocs rubric values, then rerank under one lock."""

from __future__ import annotations

import argparse
import json
import re
import stat
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import renumber  # pyright: ignore[reportImplicitRelativeImport]
import review_hash  # pyright: ignore[reportImplicitRelativeImport]
import writer_lock  # pyright: ignore[reportImplicitRelativeImport]


JUDGMENT_FIELDS = ("backlog_goal", *renumber.RUBRIC_FIELDS)
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
MANIFEST_FIELDS = {
    "path",
    "review_hash",
    "goals_hash",
    "evidence_hashes",
    "proposed",
}


class ApprovalError(RuntimeError):
    """An approval manifest or guarded apply is unsafe."""


@dataclass(frozen=True)
class Runtime:
    scope: renumber.Scope
    writer_lock_path: Path


PRODUCTION_RUNTIME = Runtime(
    scope=renumber.PRODUCTION_SCOPE,
    writer_lock_path=writer_lock.LOCK_PATH,
)


@dataclass(frozen=True)
class Approval:
    path: Path
    expected_review_hash: str
    expected_goals_hash: str
    evidence_hashes: Mapping[Path, str]
    proposed: Mapping[str, str]


@dataclass(frozen=True)
class RatingChange:
    source: renumber.SourceFile
    updated: bytes
    changed_fields: tuple[str, ...]


@dataclass(frozen=True)
class ApprovalTarget:
    approval: Approval
    source: renumber.SourceFile
    expected_post_review_hash: str


@dataclass(frozen=True)
class ApprovalPlan:
    goals_source: renumber.SourceFile
    approvals: tuple[Approval, ...]
    targets: tuple[ApprovalTarget, ...]
    changes: tuple[RatingChange, ...]


def _line_ending(line: str) -> str:
    if line.endswith("\r\n"):
        return "\r\n"
    if line.endswith("\n"):
        return "\n"
    return ""


def _serialized_judgment(field: str, value: str) -> str:
    if field in renumber.RUBRIC_FIELDS:
        return value
    return json.dumps(value, ensure_ascii=False)


def _read_manifest(path: Path, scope: renumber.Scope) -> tuple[Approval, ...]:
    try:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise ApprovalError(f"approval manifest must be a regular file: {path}")
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise ApprovalError(f"cannot read approval manifest {path}: {error}") from error

    approvals: list[Approval] = []
    seen: set[Path] = set()
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise ApprovalError(
                f"{path}:{line_number}: invalid JSON: {error.msg}"
            ) from error
        if not isinstance(record, dict):
            raise ApprovalError(f"{path}:{line_number}: expected a JSON object")
        if set(record) != MANIFEST_FIELDS:
            raise ApprovalError(
                f"{path}:{line_number}: approval row must contain exactly "
                + ", ".join(sorted(MANIFEST_FIELDS))
            )

        path_value = record.get("path")
        expected_hash = record.get("review_hash")
        expected_goals_hash = record.get("goals_hash")
        evidence_hashes = record.get("evidence_hashes")
        proposed = record.get("proposed")
        if not isinstance(path_value, str):
            raise ApprovalError(f"{path}:{line_number}: path must be a string")
        issue_path = Path(path_value)
        if (
            not issue_path.is_absolute()
            or issue_path.parent != scope.issues
            or issue_path.suffix != ".md"
        ):
            raise ApprovalError(
                f"{path}:{line_number}: issue path is outside fixed scope: "
                f"{issue_path}"
            )
        if issue_path in seen:
            raise ApprovalError(f"{path}:{line_number}: duplicate issue {issue_path}")
        seen.add(issue_path)

        if not isinstance(expected_hash, str) or HASH_RE.fullmatch(expected_hash) is None:
            raise ApprovalError(
                f"{path}:{line_number}: review_hash must be 64 lowercase hex digits"
            )
        if (
            not isinstance(expected_goals_hash, str)
            or HASH_RE.fullmatch(expected_goals_hash) is None
        ):
            raise ApprovalError(
                f"{path}:{line_number}: goals_hash must be 64 lowercase hex digits"
            )
        if not isinstance(evidence_hashes, dict) or not evidence_hashes:
            raise ApprovalError(
                f"{path}:{line_number}: evidence_hashes must be a non-empty object"
            )
        parsed_evidence: dict[Path, str] = {}
        for evidence_path_value, evidence_hash in evidence_hashes.items():
            if not isinstance(evidence_path_value, str):
                raise ApprovalError(
                    f"{path}:{line_number}: evidence paths must be strings"
                )
            if not isinstance(evidence_hash, str) or HASH_RE.fullmatch(evidence_hash) is None:
                raise ApprovalError(
                    f"{path}:{line_number}: invalid evidence hash for "
                    f"{evidence_path_value}"
                )
            parsed_evidence[Path(evidence_path_value)] = evidence_hash
        if not isinstance(proposed, dict) or set(proposed) != set(JUDGMENT_FIELDS):
            raise ApprovalError(
                f"{path}:{line_number}: proposed must contain exactly "
                + ", ".join(JUDGMENT_FIELDS)
            )
        if any(not isinstance(value, str) for value in proposed.values()):
            raise ApprovalError(
                f"{path}:{line_number}: every proposed value must be a string"
            )

        parsed_proposed = dict(cast(dict[str, str], proposed))
        parsed_proposed["backlog_goal"] = renumber.normalize_obsidian_links(
            parsed_proposed["backlog_goal"]
        )
        approvals.append(
            Approval(
                path=issue_path,
                expected_review_hash=expected_hash,
                expected_goals_hash=expected_goals_hash,
                evidence_hashes=parsed_evidence,
                proposed=parsed_proposed,
            )
        )

    if not approvals:
        raise ApprovalError(f"approval manifest contains no rows: {path}")
    return tuple(approvals)


def _validate_proposed(
    approval: Approval,
    goals: tuple[renumber.Goal, ...],
) -> None:
    goal_values = {goal.value for goal in goals}
    goal = approval.proposed["backlog_goal"]
    if goal not in goal_values:
        raise ApprovalError(
            f"{approval.path}: backlog_goal is not a current goal: {goal!r}"
        )
    for field in renumber.RUBRIC_FIELDS:
        value = approval.proposed[field]
        if value not in renumber.RUBRIC_DOMAINS[field]:
            raise ApprovalError(f"{approval.path}: invalid {field}: {value!r}")


def _evidence_source(path: Path, scope: renumber.Scope) -> renumber.SourceFile:
    if not path.is_absolute() or ".." in path.parts or path.suffix != ".md":
        raise ApprovalError(f"invalid linked evidence path: {path}")
    try:
        path.relative_to(scope.vault)
    except ValueError as error:
        raise ApprovalError(f"linked evidence is outside the fixed vault: {path}") from error
    try:
        resolved_vault = scope.vault.resolve(strict=True)
        resolved_path = path.resolve(strict=True)
    except OSError as error:
        raise ApprovalError(f"cannot resolve linked evidence {path}: {error}") from error
    if not resolved_path.is_relative_to(resolved_vault):
        raise ApprovalError(f"linked evidence leaves the fixed vault: {path}")
    relative = path.relative_to(scope.vault)
    cursor = scope.vault
    for part in relative.parts:
        cursor /= part
        try:
            if stat.S_ISLNK(cursor.lstat().st_mode):
                raise ApprovalError(f"linked evidence uses a symlink: {path}")
        except OSError as error:
            raise ApprovalError(f"cannot inspect linked evidence {path}: {error}") from error
    return renumber._read_source(path)


def _evidence_hash(path: Path, scope: renumber.Scope) -> str:
    source = _evidence_source(path, scope)
    if path == scope.goals:
        return source.digest
    if path.parent == scope.issues:
        return review_hash.evidence_hash_for_source(source)
    return source.digest


def _validate_evidence_hashes(approval: Approval, scope: renumber.Scope) -> None:
    for path, expected in approval.evidence_hashes.items():
        current = _evidence_hash(path, scope)
        if current != expected:
            raise ApprovalError(
                f"{approval.path}: linked evidence changed after review: {path}"
            )


def _status(frontmatter: renumber.Frontmatter, path: Path) -> str:
    try:
        occurrence = renumber._one_field(frontmatter, "status")
        if occurrence is None:
            raise ValueError("missing status")
        value = renumber._parse_scalar(occurrence.raw_value)
    except ValueError as error:
        raise ApprovalError(f"{path}: {error}") from error
    return value


def _rewrite_ratings(
    source: renumber.SourceFile,
    frontmatter: renumber.Frontmatter,
    proposed: Mapping[str, str],
) -> tuple[bytes, tuple[str, ...]]:
    lines = list(frontmatter.lines)
    missing: list[str] = []
    changed: list[str] = []
    existing_indices: list[int] = []
    removed_indices: set[int] = set()
    top_level_indices = sorted(
        occurrence.index
        for occurrences in frontmatter.fields.values()
        for occurrence in occurrences
    )

    def continuation_indices(index: int) -> tuple[int, ...]:
        next_index = next(
            (
                candidate
                for candidate in top_level_indices
                if candidate > index
            ),
            frontmatter.closing_index,
        )
        return tuple(
            candidate
            for candidate in range(index + 1, next_index)
            if lines[candidate].strip()
            and not lines[candidate].lstrip().startswith("#")
        )

    for field in JUDGMENT_FIELDS:
        occurrences = frontmatter.fields.get(field, [])
        canonical = f"{field}: {_serialized_judgment(field, proposed[field])}"
        if not occurrences:
            missing.append(field)
            changed.append(field)
            continue
        retained = occurrences[0]
        existing_indices.append(retained.index)
        replacement = canonical + _line_ending(lines[retained.index])
        field_changed = lines[retained.index] != replacement or len(occurrences) > 1
        lines[retained.index] = replacement
        for position, occurrence in enumerate(occurrences):
            continuations = continuation_indices(occurrence.index)
            if continuations:
                removed_indices.update(continuations)
                field_changed = True
            if position > 0:
                removed_indices.add(occurrence.index)
        if field_changed:
            changed.append(field)

    if existing_indices:
        anchor = max(existing_indices) + 1
    else:
        generated_indices = [
            occurrence.index
            for field in renumber.GENERATED_FIELDS
            for occurrence in frontmatter.fields.get(field, [])
        ]
        priority_indices = [
            occurrence.index
            for occurrence in frontmatter.fields.get("priority", [])
        ]
        if generated_indices:
            anchor = min(generated_indices)
        elif priority_indices:
            anchor = max(priority_indices) + 1
        else:
            anchor = frontmatter.closing_index

    rewritten = [
        line for index, line in enumerate(lines) if index not in removed_indices
    ]
    if missing:
        adjusted_anchor = anchor - sum(
            index < anchor for index in removed_indices
        )
        newline = renumber._newline_for(frontmatter)
        rewritten[adjusted_anchor:adjusted_anchor] = [
            f"{field}: {_serialized_judgment(field, proposed[field])}{newline}"
            for field in missing
        ]

    return "".join(rewritten).encode("utf-8"), tuple(changed)


def prepare_plan(
    manifest_path: Path,
    scope: renumber.Scope = renumber.PRODUCTION_SCOPE,
) -> ApprovalPlan:
    approvals = _read_manifest(manifest_path, scope)
    goals_source = renumber._read_source(scope.goals)
    goals = renumber.parse_goals(goals_source)
    targets: list[ApprovalTarget] = []
    changes: list[RatingChange] = []

    for approval in approvals:
        if approval.expected_goals_hash != goals_source.digest:
            raise ApprovalError(
                f"{approval.path}: strategic goals changed after review "
                f"({approval.expected_goals_hash} != {goals_source.digest})"
            )
        _validate_proposed(approval, goals)
        _validate_evidence_hashes(approval, scope)
        source = renumber._read_source(approval.path)
        current_hash = review_hash.review_hash_for_source(source)
        if current_hash != approval.expected_review_hash:
            raise ApprovalError(
                f"{approval.path}: source evidence changed after review "
                f"({approval.expected_review_hash} != {current_hash})"
            )
        frontmatter = renumber.parse_frontmatter(source)
        if _status(frontmatter, approval.path) != "open":
            raise ApprovalError(
                f"{approval.path}: only currently open issues may receive a batch"
            )
        updated, changed_fields = _rewrite_ratings(
            source, frontmatter, approval.proposed
        )
        targets.append(
            ApprovalTarget(
                approval=approval,
                source=source,
                expected_post_review_hash=review_hash.review_hash_for_content(
                    source.path, updated
                ),
            )
        )
        if updated != source.content:
            changes.append(
                RatingChange(
                    source=source,
                    updated=updated,
                    changed_fields=changed_fields,
                )
            )

    return ApprovalPlan(
        goals_source=goals_source,
        approvals=approvals,
        targets=tuple(targets),
        changes=tuple(changes),
    )


def _rollback_owned_changes(
    before: Mapping[Path, renumber.SourceFile],
    owned_contents: Mapping[Path, set[bytes]],
) -> list[str]:
    failures: list[str] = []
    for path in reversed(tuple(before)):
        source = before[path]
        try:
            current = path.read_bytes()
            if current == source.content:
                continue
            if current not in owned_contents.get(path, set()):
                failures.append(f"did not overwrite concurrently changed file {path}")
                continue
            renumber._atomic_write(path, source.content, source.mode)
        except OSError as error:
            failures.append(f"could not restore {path}: {error}")
    return failures


def apply_manifest(manifest_path: Path, runtime: Runtime = PRODUCTION_RUNTIME) -> ApprovalPlan:
    try:
        with writer_lock.acquire_writer_lock(runtime.writer_lock_path):
            before: dict[Path, renumber.SourceFile] = {}
            owned_contents: dict[Path, set[bytes]] = {}
            try:
                plan = prepare_plan(manifest_path, runtime.scope)
                issue_paths = renumber._discover_issue_paths(runtime.scope)
                before = {path: renumber._read_source(path) for path in issue_paths}
                if renumber._discover_issue_paths(runtime.scope) != issue_paths:
                    raise ApprovalError(
                        "issue files changed while the apply baseline was captured"
                    )
                renumber._assert_source_unchanged(plan.goals_source)
                for target in plan.targets:
                    renumber._assert_source_unchanged(target.source)
                    _validate_evidence_hashes(target.approval, runtime.scope)

                for change in plan.changes:
                    renumber._assert_source_unchanged(plan.goals_source)
                    renumber._assert_source_unchanged(change.source)
                    renumber._atomic_write(
                        change.source.path,
                        change.updated,
                        change.source.mode,
                    )
                    owned_contents.setdefault(change.source.path, set()).add(
                        change.updated
                    )

                renumber._assert_source_unchanged(plan.goals_source)
                ranking_plan = renumber.build_plan(runtime.scope)
                ranking_paths = tuple(
                    issue.source.path for issue in ranking_plan.issues
                )
                if ranking_paths != issue_paths:
                    raise ApprovalError("issue membership changed before reranking")
                rating_updates = {
                    change.source.path: change.updated for change in plan.changes
                }
                for issue in ranking_plan.issues:
                    baseline = before[issue.source.path]
                    expected = rating_updates.get(
                        issue.source.path, baseline.content
                    )
                    if issue.source.content != expected or issue.source.mode != baseline.mode:
                        raise ApprovalError(
                            f"issue changed before reranking: {issue.source.path}"
                        )
                for change in ranking_plan.changes:
                    owned_contents.setdefault(change.source.path, set()).add(
                        change.updated
                    )
                renumber.apply_plan(ranking_plan)

                renumber._assert_source_unchanged(plan.goals_source)
                validation_plan = renumber.build_plan(runtime.scope)
                renumber._assert_source_unchanged(plan.goals_source)
                if (
                    tuple(issue.source.path for issue in validation_plan.issues)
                    != issue_paths
                ):
                    raise ApprovalError(
                        "issue membership changed during final validation"
                    )
                if validation_plan.changes:
                    raise ApprovalError(
                        "post-apply ranking validation found remaining mechanical changes"
                    )
                for target in plan.targets:
                    current = renumber._read_source(target.source.path)
                    if (
                        review_hash.review_hash_for_source(current)
                        != target.expected_post_review_hash
                    ):
                        raise ApprovalError(
                            f"approved values or source evidence changed during apply: "
                            f"{target.source.path}"
                        )
                    _validate_evidence_hashes(target.approval, runtime.scope)
                return plan
            except (
                ApprovalError,
                renumber.PlanningError,
                renumber.ApplyError,
                OSError,
            ) as error:
                rollback_failures = _rollback_owned_changes(before, owned_contents)
                suffix = ""
                if rollback_failures:
                    suffix = "; rollback warnings: " + "; ".join(rollback_failures)
                raise ApprovalError(
                    f"approved batch apply failed: {error}{suffix}"
                ) from error
    except writer_lock.WriterLockError as error:
        raise ApprovalError(str(error)) from error


def print_plan(plan: ApprovalPlan, *, mode: str) -> None:
    print(f"Mode: {mode}")
    print(
        f"Approved rows: {len(plan.approvals)}; "
        f"judgment files changing: {len(plan.changes)}"
    )
    for change in plan.changes:
        fields = ", ".join(change.changed_fields)
        print(f"  {change.source.path}: {fields}")
    print("Post-apply review hashes:")
    for target in plan.targets:
        print(
            json.dumps(
                {
                    "path": str(target.source.path),
                    "post_review_hash": target.expected_post_review_hash,
                },
                sort_keys=True,
            )
        )


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Apply an approved Hanadocs rating JSONL batch and rerank."
    )
    parser.add_argument("manifest", type=Path, help="immutable approved batch JSONL")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="apply approved ratings and deterministic score/rank changes",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _argument_parser().parse_args(argv)
    try:
        if not arguments.apply:
            plan = prepare_plan(arguments.manifest, renumber.PRODUCTION_SCOPE)
            print_plan(plan, mode="dry-run (no files written)")
            if plan.changes:
                print("Run with --apply to write this approved batch and rerank.")
            return 0

        plan = apply_manifest(arguments.manifest, PRODUCTION_RUNTIME)
        print_plan(plan, mode="apply")
        print("Approved ratings applied; score and rank are canonical.")
        return 0
    except ApprovalError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
