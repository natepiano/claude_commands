#!/usr/bin/env python3
"""Build the semantic input snapshot used by the Hanadocs rank watcher."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

import renumber  # pyright: ignore[reportImplicitRelativeImport]


ISSUES_DIR = Path("/Users/natemccoy/rust/hanadocs/issues")
GOALS_FILE = Path("/Users/natemccoy/rust/hanadocs/prioritization goals.md")

INPUT_FIELDS = (
    "status",
    "strategic_goal",
    "alignment",
    "impact",
    "urgency",
    "leverage",
    "confidence",
    "effort",
)

TOP_LEVEL_PROPERTY = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):(?:[ \t]*(.*))?$")
GOAL_LINE = re.compile(r"^(?P<ordinal>[1-9][0-9]*)\.\s+`(?P<value>[^`]+)`\s*$")
NUMBERED_GOAL_START = re.compile(r"^[1-9][0-9]*\.\s+")
GOAL_VALUE = re.compile(r"^(?P<prefix>[1-9][0-9]*) - (?P<name>\S(?:.*\S)?)$")


def invalid_scalar(raw: str, reason: str) -> Dict[str, str]:
    return {"invalid": reason, "raw": raw}


def read_stable_text(path: Path) -> str:
    try:
        before = os.lstat(path)
    except OSError as error:
        raise ValueError(f"cannot inspect {path}: {error}") from error
    if stat.S_ISLNK(before.st_mode):
        raise ValueError(f"refusing to follow symlink: {path}")
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(f"expected a regular file: {path}")

    try:
        content = path.read_bytes()
        after = os.lstat(path)
    except OSError as error:
        raise ValueError(f"cannot read {path}: {error}") from error

    signature_before = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    signature_after = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if signature_before != signature_after:
        raise ValueError(f"file changed while it was being read: {path}")
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"{path} is not valid UTF-8: {error}") from error


def parse_status_scalar(raw: Optional[str]) -> Any:
    if raw is None:
        return None

    value = raw.strip()
    if not value:
        return invalid_scalar(raw, "empty scalar")

    if value.startswith('"'):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return invalid_scalar(raw, "malformed double-quoted scalar")
        if not isinstance(parsed, str):
            return invalid_scalar(raw, "non-string scalar")
        return parsed

    if value.startswith("'"):
        if len(value) < 2 or not value.endswith("'"):
            return invalid_scalar(raw, "malformed single-quoted scalar")
        return value[1:-1].replace("''", "'")

    if any(character in value for character in "#[]{}"):
        return invalid_scalar(raw, "non-plain scalar")
    return value


def parse_domain_scalar(raw: Optional[str]) -> Any:
    return parse_status_scalar(raw)


def frontmatter_values(path: Path) -> tuple[Dict[str, Any], Any]:
    text = read_stable_text(path)
    lines = text.splitlines()
    values: Dict[str, Any] = {field: None for field in INPUT_FIELDS}

    if not lines or lines[0] != "---":
        return values, invalid_scalar(lines[0] if lines else "", "missing opening delimiter")

    occurrences: Dict[str, List[Any]] = {field: [] for field in INPUT_FIELDS}
    found_closing_delimiter = False
    for line in lines[1:]:
        if line == "---":
            found_closing_delimiter = True
            break
        if line[:1].isspace():
            continue
        match = TOP_LEVEL_PROPERTY.match(line)
        if match is None:
            continue
        key, raw = match.groups()
        if key in occurrences:
            if key == "status":
                parsed = parse_status_scalar(raw)
            else:
                parsed = parse_domain_scalar(raw)
            if key == "strategic_goal" and isinstance(parsed, str):
                parsed = renumber.normalize_obsidian_links(parsed)
            occurrences[key].append(parsed)

    if not found_closing_delimiter:
        return values, invalid_scalar("", "missing closing delimiter")

    for field, found in occurrences.items():
        if len(found) == 1:
            values[field] = found[0]
        elif len(found) > 1:
            values[field] = {
                "invalid": "duplicate property",
                "values": found,
            }

    return values, "valid"


def current_goals() -> Any:
    lines = read_stable_text(GOALS_FILE).splitlines()
    in_current_goals = False
    saw_heading = False
    goals: List[str] = []
    errors: List[str] = []

    for line in lines:
        if line.strip() == "## Current goals":
            if saw_heading:
                errors.append("repeated Current goals heading")
                continue
            saw_heading = True
            in_current_goals = True
            continue
        if in_current_goals and line.startswith("##"):
            break
        if not in_current_goals:
            continue

        stripped = line.strip()
        match = GOAL_LINE.fullmatch(stripped)
        if match is None:
            if NUMBERED_GOAL_START.match(stripped):
                errors.append(f"malformed ordered goal: {stripped!r}")
            continue

        ordinal = int(match.group("ordinal"))
        expected_ordinal = len(goals) + 1
        value = renumber.normalize_obsidian_links(match.group("value"))
        value_match = GOAL_VALUE.fullmatch(value)
        if ordinal != expected_ordinal:
            errors.append(
                f"goal ordinal {ordinal} is not the expected {expected_ordinal}"
            )
        elif value_match is None or int(value_match.group("prefix")) != ordinal:
            errors.append(f"goal {ordinal} must begin with {ordinal} - ")
        goals.append(value)

    if not saw_heading:
        errors.append("missing Current goals heading")
    if not goals:
        errors.append("no current goals")
    if len(set(goals)) != len(goals):
        errors.append("duplicate current goal values")
    if errors:
        return {"invalid": errors, "values": goals}
    return goals


def build_snapshot() -> Dict[str, Any]:
    if ISSUES_DIR.is_symlink():
        raise ValueError(f"refusing symlinked issues directory: {ISSUES_DIR}")
    if not ISSUES_DIR.is_dir():
        raise ValueError(f"issues directory does not exist: {ISSUES_DIR}")
    if not GOALS_FILE.is_file():
        raise ValueError(f"goals file does not exist: {GOALS_FILE}")

    issues: List[Dict[str, Any]] = []
    for path in sorted(ISSUES_DIR.glob("*.md"), key=lambda item: item.name):
        values, frontmatter_state = frontmatter_values(path)
        record: Dict[str, Any] = {
            "path": f"issues/{path.name}",
            "frontmatter": frontmatter_state,
        }
        record.update(values)
        issues.append(record)

    return {
        "schema": 1,
        "goals": current_goals(),
        "issues": issues,
    }


def completeness_errors(snapshot: Mapping[str, Any]) -> Iterable[str]:
    goals = snapshot["goals"]
    if not isinstance(goals, list):
        yield f"{GOALS_FILE}: malformed goals section {goals!r}"
        goal_values = set()
    else:
        goal_values = set(goals)
    for issue in snapshot["issues"]:
        if issue["status"] != "open":
            continue

        path = issue["path"]
        if issue["frontmatter"] != "valid":
            yield f"{path}: malformed frontmatter {issue['frontmatter']!r}"
            continue
        strategic_goal = issue["strategic_goal"]
        if not isinstance(strategic_goal, str) or strategic_goal not in goal_values:
            yield f"{path}: invalid or missing strategic_goal {strategic_goal!r}"

        for field, domain in renumber.RUBRIC_DOMAINS.items():
            value = issue[field]
            if value not in domain:
                yield f"{path}: invalid or missing {field} {value!r}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="write canonical JSON to this path",
    )
    parser.add_argument(
        "--require-complete",
        action="store_true",
        help="fail unless every open issue has valid rubric inputs",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        snapshot = build_snapshot()
    except (OSError, UnicodeError, ValueError) as error:
        print(f"snapshot error: {error}", file=sys.stderr)
        return 2

    if args.require_complete:
        errors = list(completeness_errors(snapshot))
        if errors:
            print(
                f"bootstrap metadata is incomplete ({len(errors)} problem(s)):",
                file=sys.stderr,
            )
            for error in errors:
                print(f"- {error}", file=sys.stderr)
            return 1

    try:
        args.output.write_text(
            json.dumps(snapshot, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError as error:
        print(f"snapshot write error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
