#!/usr/bin/env python3
"""Deterministically score and rank the Hanadocs issue backlog.

The command-line interface is deliberately fixed to the Hanadocs vault.  The
small ``Scope`` seam exists only so the behavior can be tested without touching
live notes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from collections.abc import Sequence
from typing import TypedDict
from zoneinfo import ZoneInfo

import writer_lock  # pyright: ignore[reportImplicitRelativeImport]


VAULT_PATH = Path("/Users/natemccoy/rust/hanadocs")
ISSUES_PATH = VAULT_PATH / "issues"
GOALS_PATH = VAULT_PATH / "prioritization goals.md"
RANKINGS_FILENAME = "backlog-rankings.jsonl"

STAR = "⭐"
STAR_VALUES = tuple(STAR * count for count in range(1, 6))
RUBRIC_DOMAINS: dict[str, tuple[str, ...]] = {
    field: STAR_VALUES
    for field in (
        "backlog_alignment",
        "backlog_impact",
        "backlog_urgency",
        "backlog_effort",
    )
}
RUBRIC_FIELDS = tuple(RUBRIC_DOMAINS)
GENERATED_FIELDS = ("backlog_score", "backlog_rank")
OPERATIONAL_TIMEZONE = ZoneInfo("America/New_York")
OBSIDIAN_CACHE_REFRESH_NS = 1_000_000

FIELD_LINE_RE = re.compile(
    r"^(?P<key>[A-Za-z_][A-Za-z0-9_-]*):(?P<value>.*?)(?P<ending>\r?\n)?$"
)
GOAL_LINE_RE = re.compile(r"^(?P<ordinal>[1-9]\d*)\.\s+`(?P<value>[^`]+)`\s*$")
GOAL_VALUE_RE = re.compile(r"^(?P<prefix>[1-9]\d*) - (?P<name>\S(?:.*\S)?)$")
WIKILINK_RE = re.compile(r"!?\[\[(?P<body>[^\]\n]+)\]\]")
POSITIVE_INTEGER_RE = re.compile(r"^[1-9]\d*$")


class PlanningError(RuntimeError):
    """The source corpus cannot be interpreted safely."""


class ApplyError(RuntimeError):
    """A prepared plan could not be applied safely."""


class ConcurrentChangeError(ApplyError):
    """A source changed after planning and the ranking pass must be retried."""


@dataclass(frozen=True)
class Scope:
    vault: Path
    issues: Path
    goals: Path


PRODUCTION_SCOPE = Scope(VAULT_PATH, ISSUES_PATH, GOALS_PATH)


@dataclass(frozen=True)
class SourceFile:
    path: Path
    content: bytes
    mode: int
    signature: tuple[int, int, int, int, int]
    digest: str


@dataclass(frozen=True)
class FieldOccurrence:
    index: int
    raw_value: str


@dataclass
class Frontmatter:
    lines: list[str]
    closing_index: int
    fields: dict[str, list[FieldOccurrence]]


@dataclass(frozen=True)
class Goal:
    value: str
    ordinal: int
    bonus: int


@dataclass
class Issue:
    source: SourceFile
    frontmatter: Frontmatter
    status: str
    score: int | None = None
    existing_rank: int | None = None
    assigned_rank: int | None = None
    problems: list[str] = field(default_factory=list)

    @property
    def is_open(self) -> bool:
        return self.status == "open"

    @property
    def is_valid_open(self) -> bool:
        return self.is_open and not self.problems and self.score is not None


@dataclass(frozen=True)
class PlannedChange:
    source: SourceFile
    updated: bytes
    description: str


@dataclass
class RankingPlan:
    scope: Scope
    goals_source: SourceFile
    goals: tuple[Goal, ...]
    issues: tuple[Issue, ...]
    changes: tuple[PlannedChange, ...]

    @property
    def valid_open(self) -> tuple[Issue, ...]:
        return tuple(issue for issue in self.issues if issue.is_valid_open)

    @property
    def needs_prioritization(self) -> tuple[Issue, ...]:
        return tuple(issue for issue in self.issues if issue.is_open and issue.problems)

    @property
    def closed(self) -> tuple[Issue, ...]:
        return tuple(issue for issue in self.issues if not issue.is_open)


def _signature(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _operational_date(timestamp_ns: int) -> date:
    seconds, _nanoseconds = divmod(timestamp_ns, 1_000_000_000)
    return datetime.fromtimestamp(seconds, tz=OPERATIONAL_TIMEZONE).date()


def _refreshed_mtime_ns(timestamp_ns: int) -> int:
    """Request a one-millisecond mtime change within its New York date."""

    original_date = _operational_date(timestamp_ns)
    for delta in (OBSIDIAN_CACHE_REFRESH_NS, -OBSIDIAN_CACHE_REFRESH_NS):
        candidate = timestamp_ns + delta
        if _operational_date(candidate) == original_date:
            return candidate
    raise OSError("cannot refresh Obsidian cache without changing modified date")


def _read_source(path: Path) -> SourceFile:
    try:
        before = path.lstat()
    except OSError as error:
        raise PlanningError(f"cannot inspect {path}: {error}") from error
    if stat.S_ISLNK(before.st_mode):
        raise PlanningError(f"refusing to follow symlink: {path}")
    if not stat.S_ISREG(before.st_mode):
        raise PlanningError(f"expected a regular file: {path}")
    try:
        content = path.read_bytes()
        after = path.lstat()
    except OSError as error:
        raise PlanningError(f"cannot read {path}: {error}") from error
    if _signature(before) != _signature(after):
        raise PlanningError(f"file changed while it was being read: {path}")
    return SourceFile(
        path=path,
        content=content,
        mode=stat.S_IMODE(after.st_mode),
        signature=_signature(after),
        digest=hashlib.sha256(content).hexdigest(),
    )


def _decode_utf8(source: SourceFile) -> str:
    try:
        return source.content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise PlanningError(f"{source.path} is not valid UTF-8: {error}") from error


def parse_frontmatter(source: SourceFile) -> Frontmatter:
    text = _decode_utf8(source)
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        raise PlanningError(f"{source.path} does not begin with YAML frontmatter")

    closing_index = -1
    for index, line in enumerate(lines[1:], start=1):
        if line.rstrip("\r\n") == "---":
            closing_index = index
            break
    if closing_index < 0:
        raise PlanningError(f"{source.path} has no closing frontmatter delimiter")

    fields: dict[str, list[FieldOccurrence]] = {}
    for index in range(1, closing_index):
        match = FIELD_LINE_RE.fullmatch(lines[index])
        if match is None:
            continue
        fields.setdefault(match.group("key"), []).append(
            FieldOccurrence(index=index, raw_value=match.group("value").strip())
        )
    return Frontmatter(lines=lines, closing_index=closing_index, fields=fields)


def _one_field(frontmatter: Frontmatter, key: str) -> FieldOccurrence | None:
    occurrences = frontmatter.fields.get(key, [])
    if len(occurrences) > 1:
        raise ValueError(f"duplicate {key} properties")
    return occurrences[0] if occurrences else None


def _parse_scalar(raw_value: str) -> str:
    if not raw_value:
        raise ValueError("empty scalar")
    if raw_value.startswith('"'):
        try:
            decoded = json.loads(raw_value)
        except json.JSONDecodeError as error:
            raise ValueError("malformed quoted scalar") from error
        if not isinstance(decoded, str):
            raise ValueError("expected a string scalar")
        return decoded
    if raw_value.startswith("'"):
        if len(raw_value) < 2 or not raw_value.endswith("'"):
            raise ValueError("malformed quoted scalar")
        return raw_value[1:-1].replace("''", "'")
    if any(character in raw_value for character in "#[]{}"):
        raise ValueError("expected a plain scalar")
    return raw_value


def _parse_required_value(
    frontmatter: Frontmatter,
    key: str,
) -> str:
    occurrence = _one_field(frontmatter, key)
    if occurrence is None:
        raise ValueError(f"missing {key}")
    return _parse_scalar(occurrence.raw_value)


def _parse_required_stars(
    frontmatter: Frontmatter,
    key: str,
) -> tuple[str, int]:
    value = _parse_required_value(frontmatter, key)
    allowed_values = RUBRIC_DOMAINS[key]
    if value not in allowed_values:
        raise ValueError(f"invalid {key}: {value!r}")
    return value, len(value)


def normalize_obsidian_links(value: str) -> str:
    """Replace Obsidian wikilinks with the text Obsidian displays."""

    def displayed_text(match: re.Match[str]) -> str:
        body = match.group("body")
        target, separator, alias = body.partition("|")
        if separator:
            return alias.strip()
        target = re.split(r"[#^]", target, maxsplit=1)[0].strip()
        return target.rsplit("/", maxsplit=1)[-1]

    return WIKILINK_RE.sub(displayed_text, value)


def _parse_existing_rank(frontmatter: Frontmatter) -> int | None:
    try:
        occurrence = _one_field(frontmatter, "backlog_rank")
    except ValueError:
        return None
    if occurrence is None or POSITIVE_INTEGER_RE.fullmatch(occurrence.raw_value) is None:
        return None
    return int(occurrence.raw_value)


def parse_goals(source: SourceFile) -> tuple[Goal, ...]:
    text = _decode_utf8(source)
    in_current_goals = False
    values: list[str] = []
    saw_heading = False

    for line in text.splitlines():
        if line.strip() == "## Current goals":
            if saw_heading:
                raise PlanningError(f"{source.path} repeats the Current goals heading")
            saw_heading = True
            in_current_goals = True
            continue
        if in_current_goals and line.startswith("##"):
            break
        if not in_current_goals:
            continue
        match = GOAL_LINE_RE.fullmatch(line.strip())
        if match is None:
            if re.match(r"^[1-9]\d*\.\s+", line.strip()):
                raise PlanningError(
                    f"malformed ordered goal in {source.path}: {line.strip()!r}"
                )
            continue
        ordinal = int(match.group("ordinal"))
        expected = len(values) + 1
        if ordinal != expected:
            raise PlanningError(
                f"goal list in {source.path} must use dense ordinals starting at 1"
            )
        value = normalize_obsidian_links(match.group("value"))
        value_match = GOAL_VALUE_RE.fullmatch(value)
        if value_match is None or int(value_match.group("prefix")) != ordinal:
            raise PlanningError(
                f"goal {ordinal} in {source.path} must begin with {ordinal} - "
            )
        values.append(value)

    if not saw_heading:
        raise PlanningError(f"{source.path} has no '## Current goals' heading")
    if not values:
        raise PlanningError(f"{source.path} defines no current goals")
    if len(set(values)) != len(values):
        raise PlanningError(f"{source.path} contains duplicate current goals")

    return tuple(
        Goal(
            value=value,
            ordinal=index,
            bonus=2 * (len(values) - index),
        )
        for index, value in enumerate(values, start=1)
    )


def calculate_score(goal: Goal, values: dict[str, int]) -> int:
    alignment = values["backlog_alignment"]
    return (
        (4 * (alignment - 1))
        + (3 * (values["backlog_impact"] - 1))
        + (2 * (values["backlog_urgency"] - 1))
        - (values["backlog_effort"] - 1)
        + goal.bonus
    )


def _parse_issue(source: SourceFile, goals: tuple[Goal, ...]) -> Issue:
    frontmatter = parse_frontmatter(source)
    try:
        status_occurrence = _one_field(frontmatter, "status")
        if status_occurrence is None:
            raise ValueError("missing status")
        status = _parse_scalar(status_occurrence.raw_value)
    except ValueError as error:
        raise PlanningError(f"{source.path}: {error}") from error
    if status not in {"open", "closed"}:
        raise PlanningError(f"{source.path}: unsupported status {status!r}")

    issue = Issue(
        source=source,
        frontmatter=frontmatter,
        status=status,
        existing_rank=_parse_existing_rank(frontmatter),
    )
    if status == "closed":
        return issue

    goal_by_value = {goal.value: goal for goal in goals}
    try:
        raw_goal_value = _parse_required_value(frontmatter, "backlog_goal")
        goal_value = normalize_obsidian_links(raw_goal_value)
        if goal_value not in goal_by_value:
            raise ValueError(f"invalid backlog_goal: {raw_goal_value!r}")
    except ValueError as error:
        issue.problems.append(str(error))
        goal = None
    else:
        goal = goal_by_value[goal_value]

    values: dict[str, int] = {}
    for key in RUBRIC_FIELDS:
        try:
            _value, star_count = _parse_required_stars(frontmatter, key)
        except ValueError as error:
            issue.problems.append(str(error))
        else:
            values[key] = star_count

    if not issue.problems and goal is not None:
        issue.score = calculate_score(goal, values)
    return issue


def _newline_for(frontmatter: Frontmatter) -> str:
    for line in frontmatter.lines[: frontmatter.closing_index + 1]:
        if line.endswith("\r\n"):
            return "\r\n"
        if line.endswith("\n"):
            return "\n"
    return "\n"


def _generated_description(frontmatter: Frontmatter, desired: dict[str, int]) -> str:
    parts: list[str] = []
    for key in GENERATED_FIELDS:
        occurrences = frontmatter.fields.get(key, [])
        old = occurrences[0].raw_value if len(occurrences) == 1 else None
        if key in desired:
            new = str(desired[key])
            if old != new or len(occurrences) != 1:
                parts.append(f"{key} {old or '(missing)'} -> {new}")
        elif occurrences:
            parts.append(f"remove {key}")
    return "; ".join(parts)


def _rewrite_generated(
    source: SourceFile,
    frontmatter: Frontmatter,
    desired: dict[str, int],
) -> bytes:
    top_level_indices = sorted(
        occurrence.index
        for occurrences in frontmatter.fields.values()
        for occurrence in occurrences
    )
    target_indices: set[int] = set()
    for key in GENERATED_FIELDS:
        for occurrence in frontmatter.fields.get(key, []):
            target_indices.add(occurrence.index)
            next_index = next(
                (
                    candidate
                    for candidate in top_level_indices
                    if candidate > occurrence.index
                ),
                frontmatter.closing_index,
            )
            target_indices.update(
                index
                for index in range(occurrence.index + 1, next_index)
                if frontmatter.lines[index].strip()
                and not frontmatter.lines[index].lstrip().startswith("#")
            )
    if target_indices:
        original_anchor = min(target_indices)
    else:
        rubric_indices = [
            occurrence.index
            for key in ("backlog_goal", *RUBRIC_FIELDS)
            for occurrence in frontmatter.fields.get(key, [])
        ]
        if rubric_indices:
            original_anchor = max(rubric_indices) + 1
        else:
            status_indices = [
                occurrence.index
                for occurrence in frontmatter.fields.get("status", [])
            ]
            original_anchor = (
                max(status_indices) + 1 if status_indices else frontmatter.closing_index
            )

    anchor = sum(
        1
        for index in range(original_anchor)
        if index not in target_indices
    )
    rewritten = [
        line
        for index, line in enumerate(frontmatter.lines)
        if index not in target_indices
    ]
    if desired:
        newline = _newline_for(frontmatter)
        generated_lines = [
            f"backlog_score: {desired['backlog_score']}{newline}",
            f"backlog_rank: {desired['backlog_rank']}{newline}",
        ]
        rewritten[anchor:anchor] = generated_lines
    return "".join(rewritten).encode("utf-8")


def _discover_issue_paths(scope: Scope) -> tuple[Path, ...]:
    try:
        metadata = scope.issues.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise PlanningError(f"refusing to follow symlink: {scope.issues}")
        if not stat.S_ISDIR(metadata.st_mode):
            raise PlanningError(f"expected an issue directory: {scope.issues}")
        paths = tuple(sorted(scope.issues.glob("*.md"), key=lambda path: str(path)))
    except OSError as error:
        raise PlanningError(f"cannot enumerate {scope.issues}: {error}") from error
    return paths


def build_plan(scope: Scope = PRODUCTION_SCOPE) -> RankingPlan:
    goals_source = _read_source(scope.goals)
    goals = parse_goals(goals_source)
    issue_sources = tuple(_read_source(path) for path in _discover_issue_paths(scope))
    issues = tuple(_parse_issue(source, goals) for source in issue_sources)

    rank_counts = Counter(
        issue.existing_rank
        for issue in issues
        if issue.is_valid_open and issue.existing_rank is not None
    )

    def sort_key(issue: Issue) -> tuple[int, int, int, str]:
        score = issue.score
        if score is None:
            raise PlanningError(f"valid issue has no score: {issue.source.path}")
        stable_rank = issue.existing_rank
        if stable_rank is not None and rank_counts[stable_rank] == 1:
            return (-score, 0, stable_rank, str(issue.source.path))
        return (-score, 1, 0, str(issue.source.path))

    ranked = sorted((issue for issue in issues if issue.is_valid_open), key=sort_key)
    for rank, issue in enumerate(ranked, start=1):
        issue.assigned_rank = rank

    assigned = [issue.assigned_rank for issue in ranked]
    expected = list(range(1, len(ranked) + 1))
    if assigned != expected or len(set(assigned)) != len(assigned):
        raise PlanningError("planned ranks are not unique and contiguous")

    changes: list[PlannedChange] = []
    for issue in issues:
        desired: dict[str, int] = {}
        if issue.is_valid_open:
            if issue.assigned_rank is None or issue.score is None:
                raise PlanningError(f"internal ranking error for {issue.source.path}")
            desired = {
                "backlog_score": issue.score,
                "backlog_rank": issue.assigned_rank,
            }
        updated = _rewrite_generated(issue.source, issue.frontmatter, desired)
        if updated == issue.source.content:
            continue
        description = _generated_description(issue.frontmatter, desired)
        changes.append(
            PlannedChange(
                source=issue.source,
                updated=updated,
                description=description,
            )
        )

    return RankingPlan(
        scope=scope,
        goals_source=goals_source,
        goals=goals,
        issues=issues,
        changes=tuple(changes),
    )


def _assert_source_unchanged(source: SourceFile) -> None:
    try:
        current = _read_source(source.path)
    except PlanningError as error:
        raise ConcurrentChangeError(
            f"cannot verify unchanged file {source.path}: {error}"
        ) from error
    if current.signature != source.signature or current.digest != source.digest:
        raise ConcurrentChangeError(f"file changed after discovery: {source.path}")


def _assert_issue_membership_unchanged(plan: RankingPlan) -> None:
    try:
        current = _discover_issue_paths(plan.scope)
    except PlanningError as error:
        raise ConcurrentChangeError(f"cannot verify issue membership: {error}") from error
    discovered = tuple(issue.source.path for issue in plan.issues)
    if current != discovered:
        raise ConcurrentChangeError(
            "issue files were added, removed, or renamed after discovery"
        )


def _atomic_write(path: Path, content: bytes, mode: int) -> None:
    """Overwrite ``path`` in place, preserving its inode and creation date.

    Editing the existing file—rather than replacing it with a renamed temp
    file—keeps the same inode, so the macOS creation date and every other
    identity-bound attribute survive without a SetFile call. Only mtime is
    nudged one millisecond within its New York date so Obsidian invalidates its
    metadata cache while obsidian_knife still sees the same date_modified value.

    The write is deliberately not crash-atomic: a reader that catches the brief
    mid-write window may observe a partial file. The next deterministic apply
    rewrites the canonical content, so the exposure self-heals.
    """
    before = path.lstat()

    file_descriptor = os.open(path, os.O_WRONLY | os.O_NOFOLLOW)
    with os.fdopen(file_descriptor, "wb") as handle:
        os.fchmod(handle.fileno(), mode)
        _ = handle.write(content)
        handle.flush()
        _ = handle.truncate()

    os.utime(
        path,
        ns=(before.st_atime_ns, _refreshed_mtime_ns(before.st_mtime_ns)),
        follow_symlinks=False,
    )
    refreshed = os.lstat(path)
    if refreshed.st_mtime_ns == before.st_mtime_ns:
        raise OSError(f"cannot refresh Obsidian metadata cache for {path}")
    if _operational_date(refreshed.st_mtime_ns) != _operational_date(before.st_mtime_ns):
        raise OSError(f"cannot preserve modified calendar date for {path}")


def _rollback(written: Sequence[PlannedChange]) -> list[str]:
    failures: list[str] = []
    for change in reversed(written):
        try:
            current = change.source.path.read_bytes()
            if current != change.updated:
                failures.append(
                    f"did not overwrite concurrently changed file {change.source.path}"
                )
                continue
            _atomic_write(change.source.path, change.source.content, change.source.mode)
        except OSError as error:
            failures.append(f"could not restore {change.source.path}: {error}")
    return failures


class RankingEntry(TypedDict):
    name: str
    rank: int
    score: int


def _rankings_content(plan: RankingPlan) -> bytes:
    """Serialize the canonical ordering as name-sorted JSON Lines."""
    entries: list[RankingEntry] = []
    for issue in plan.valid_open:
        rank = issue.assigned_rank
        score = issue.score
        if rank is None or score is None:
            raise PlanningError(
                f"ranked issue is missing derived values: {issue.source.path}"
            )
        entries.append(
            RankingEntry(name=issue.source.path.stem, rank=rank, score=score)
        )
    entries.sort(key=lambda entry: entry["name"])
    return "".join(
        f"{json.dumps(entry, ensure_ascii=False)}\n" for entry in entries
    ).encode("utf-8")


def _write_rankings(plan: RankingPlan) -> None:
    """Overwrite the derived rankings export beside the vault's Base views.

    The export mirrors the canonical ``backlog_rank`` frontmatter and is written
    under the same writer lock as the ranks it reflects. A failure here never
    unwinds the applied frontmatter: the frontmatter is the source of truth and
    the next apply rewrites this file.
    """
    content = _rankings_content(plan)
    path = plan.scope.vault / RANKINGS_FILENAME
    try:
        if path.exists() and path.read_bytes() == content:
            return
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.prioritize-", dir=path.parent
        )
        temporary_path: str | None = temporary_name
        try:
            with os.fdopen(file_descriptor, "wb") as temporary:
                _ = temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, path)
            temporary_path = None
        finally:
            if temporary_path is not None:
                try:
                    os.unlink(temporary_path)
                except FileNotFoundError:
                    pass
    except OSError as error:
        print(
            f"warning: could not write rankings export {path}: {error}",
            file=sys.stderr,
        )


def apply_plan(plan: RankingPlan) -> None:
    _assert_issue_membership_unchanged(plan)
    _assert_source_unchanged(plan.goals_source)
    for issue in plan.issues:
        _assert_source_unchanged(issue.source)
    for change in plan.changes:
        if not os.access(change.source.path.parent, os.W_OK):
            raise ApplyError(f"directory is not writable: {change.source.path.parent}")

    written: list[PlannedChange] = []
    try:
        for change in plan.changes:
            _assert_issue_membership_unchanged(plan)
            _assert_source_unchanged(plan.goals_source)
            _assert_source_unchanged(change.source)
            _atomic_write(change.source.path, change.updated, change.source.mode)
            written.append(change)

        _assert_issue_membership_unchanged(plan)
        _assert_source_unchanged(plan.goals_source)
        expected_updates = {
            change.source.path: change.updated for change in plan.changes
        }
        for issue in plan.issues:
            current = _read_source(issue.source.path)
            expected = expected_updates.get(issue.source.path, issue.source.content)
            if current.content != expected or current.mode != issue.source.mode:
                raise ConcurrentChangeError(
                    f"post-write validation failed: {issue.source.path}"
                )
    except ConcurrentChangeError as error:
        rollback_failures = _rollback(written)
        suffix = ""
        if rollback_failures:
            suffix = "; rollback warnings: " + "; ".join(rollback_failures)
        raise ConcurrentChangeError(f"apply interrupted: {error}{suffix}") from error
    except (ApplyError, PlanningError, OSError) as error:
        rollback_failures = _rollback(written)
        suffix = ""
        if rollback_failures:
            suffix = "; rollback warnings: " + "; ".join(rollback_failures)
        raise ApplyError(f"apply failed: {error}{suffix}") from error

    _write_rankings(plan)


def _relative_path(plan: RankingPlan, path: Path) -> str:
    try:
        return str(path.relative_to(plan.scope.vault))
    except ValueError:
        return str(path)


def print_plan(plan: RankingPlan, *, mode: str) -> None:
    valid_count = len(plan.valid_open)
    needs_count = len(plan.needs_prioritization)
    print(f"Mode: {mode}")
    print(
        "Issues: "
        f"{len(plan.issues)} discovered; "
        f"{valid_count} valid open; "
        f"{needs_count} need prioritization; "
        f"{len(plan.closed)} closed"
    )
    if valid_count:
        print(f"Canonical ranks: 1..{valid_count}")
    else:
        print("Canonical ranks: none")

    if plan.needs_prioritization:
        print("Needs prioritization:")
        for issue in plan.needs_prioritization:
            problems = "; ".join(issue.problems)
            print(f"  {_relative_path(plan, issue.source.path)}: {problems}")

    if plan.changes:
        print(f"Mechanical changes: {len(plan.changes)} file(s)")
        for change in plan.changes:
            print(
                f"  {_relative_path(plan, change.source.path)}: "
                f"{change.description}"
            )
    else:
        print("No mechanical changes required.")


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Score and densely rank open Hanadocs issues."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--apply",
        action="store_true",
        help="atomically apply the proposed score and rank changes",
    )
    mode.add_argument(
        "--check",
        action="store_true",
        help="return 1 when the mechanical state is not canonical",
    )
    parser.add_argument(
        "--require-complete",
        action="store_true",
        help="return 1 when any open issue needs prioritization",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _argument_parser()
    arguments = parser.parse_args(argv)
    if arguments.apply and arguments.require_complete:
        parser.error("--require-complete cannot be combined with --apply")
    try:
        if arguments.apply:
            with writer_lock.acquire_writer_lock():
                plan = build_plan(PRODUCTION_SCOPE)
                apply_plan(plan)
            print_plan(plan, mode="apply")
            if plan.changes:
                print(f"Applied {len(plan.changes)} file change(s).")
            return 0
        plan = build_plan(PRODUCTION_SCOPE)
        if arguments.check:
            mode = "check + require-complete" if arguments.require_complete else "check"
            print_plan(plan, mode=mode)
            return 1 if plan.changes or (
                arguments.require_complete and plan.needs_prioritization
            ) else 0
        mode = (
            "dry-run + require-complete (no files written)"
            if arguments.require_complete
            else "dry-run (no files written)"
        )
        print_plan(plan, mode=mode)
        if plan.changes:
            print("Run with --apply to write these changes.")
        return 1 if arguments.require_complete and plan.needs_prioritization else 0
    except ConcurrentChangeError as error:
        print(f"concurrent change: {error}", file=sys.stderr)
        return 3
    except (PlanningError, ApplyError, writer_lock.WriterLockError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
