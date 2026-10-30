#!/usr/bin/env python3
"""Apply explicit review approvals, refusing stale content or reviewed values."""
from __future__ import annotations

import argparse
from collections.abc import Mapping
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import stat
import sys
from tempfile import NamedTemporaryFile
from typing import cast

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.issue_score import questions, vault
from scripts.issue_score.score_issues import content_hash


@dataclass(frozen=True)
class ApprovedEdit:
    path: Path
    before: bytes
    after: bytes
    mode: int


class ApplyArguments(argparse.Namespace):
    approvals: Path = Path()


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object in review or approvals")
    return cast(dict[str, object], value)


def _list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise ValueError("Expected a JSON list in review or approvals")
    return cast(list[object], value)


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Expected text in review or approvals")
    return value


def _approved_value(field: str, value: str, areas: set[str]) -> str:
    value = vault.plain(value)
    if field in vault.RATING_FIELDS:
        valid = re.fullmatch("⭐{1,5}", value) is not None
    elif field == "backlog_goal":
        valid = value in questions.GOAL_LABELS.values()
    elif field == "category":
        valid = value in questions.CATEGORIES or value == "unfiled"
    elif field == "area":
        value = vault.normalize_area(value)
        valid = value in areas or value == "unfiled"
    else:
        raise ValueError(f"Field is not an approvable issue label: {field}")
    if not valid:
        raise ValueError(f"Invalid approved value for {field}: {value}")
    return value


def _field_lines(field: str, value: str, newline: str) -> list[str]:
    if field == "area":
        value = "[[" + value + "]]"
    if field == "category":
        link = f"[[issue structure#{value}|{value}]]"
        return ["category:" + newline, "  - " + json.dumps(link, ensure_ascii=False) + newline]
    return [field + ": " + json.dumps(value, ensure_ascii=False) + newline]


def _edit_frontmatter(source: bytes, changes: Mapping[str, str]) -> bytes:
    text = source.decode("utf-8")
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise ValueError("Approved issue has no frontmatter")
    closing = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"), 0)
    if not closing:
        raise ValueError("Approved issue has unterminated frontmatter")
    output = [lines[0]]
    seen: set[str] = set()
    index = 1
    while index < closing:
        match = vault.FRONTMATTER_FIELD.match(lines[index])
        if not match or vault.plain(match[1]) not in changes:
            output.append(lines[index])
            index += 1
            continue
        field = vault.plain(match[1])
        if field in seen:
            raise ValueError(f"Duplicate approved field in frontmatter: {field}")
        seen.add(field)
        output.extend(_field_lines(field, changes[field], newline))
        index += 1
        # Only this field's list values are replaced; comments and blank lines survive.
        while index < closing and not vault.FRONTMATTER_FIELD.match(lines[index]):
            if not re.match(r"^\s*-\s+", lines[index]):
                output.append(lines[index])
            index += 1
    for field in changes:
        if field not in seen:
            output.extend(_field_lines(field, changes[field], newline))
    output.extend(lines[closing:])
    return "".join(output).encode("utf-8")


def _write_edit(edit: ApprovedEdit) -> None:
    with NamedTemporaryFile(mode="wb", dir=edit.path.parent, prefix=".issue-labels-", delete=False) as handle:
        temporary = Path(handle.name)
        try:
            _ = handle.write(edit.after)
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), edit.mode)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        if edit.path.read_bytes() != edit.before:
            raise ValueError(f"{edit.path}: issue changed during approval; rescore needed")
        os.replace(temporary, edit.path)
    finally:
        temporary.unlink(missing_ok=True)


def apply_labels(approvals_path: Path, *, issues_dir: Path = vault.ISSUES_DIR,
                 area_definitions_path: Path = vault.AREA_DEFINITIONS_PATH,
                 rules_dirs: tuple[Path, ...] = vault.STYLE_RULES_DIRS) -> list[Path]:
    """Preflight all approvals before writing only the approved frontmatter fields.

    Approvals: {"review_path": "...-reassess.json", "approvals":
                [{"issue_path": "...md", "fields": {"backlog_impact": "⭐⭐⭐"}}]}.
    The Markdown companion is for reading; the JSON preserves its reviewed values.
    """
    approvals = _object(cast(object, json.loads(approvals_path.read_text(encoding="utf-8"))))
    review_path = Path(_text(approvals.get("review_path")))
    if not review_path.is_absolute():
        review_path = approvals_path.parent / review_path
    review = _object(cast(object, json.loads(review_path.read_text(encoding="utf-8"))))
    rows = {_text(row.get("issue_path")): row for item in _list(review.get("rows")) if (row := _object(item))}
    issues = vault.load_scoring_issues(issues_dir, rules_dirs=rules_dirs)
    by_path = {issue.path.resolve(): issue for issue in issues}
    areas = [_text(area) for area in _list(review.get("area_vocabulary"))]
    reviewed_definitions = {area: _text(definition)
                            for area, definition in _object(review.get("area_definitions")).items()}
    definitions = vault.load_area_definitions(area_definitions_path)
    edits: list[ApprovedEdit] = []
    seen: set[Path] = set()
    for approval_value in _list(approvals.get("approvals")):
        approval = _object(approval_value)
        reviewed_path = _text(approval.get("issue_path"))
        path = Path(reviewed_path).resolve()
        if not path.is_relative_to(issues_dir.resolve()) or path not in by_path or reviewed_path not in rows:
            raise ValueError(f"{path}: approval does not identify an open issue in this review")
        if path in seen:
            raise ValueError(f"{path}: duplicate issue approval")
        seen.add(path)
        before = path.read_bytes()
        document = vault.parse_document(path, before.decode("utf-8"))
        if vault.plain(document.value("status")).casefold() != "open":
            raise ValueError(f"{path}: approval no longer identifies an open issue")
        issue = vault.scoring_issue(document, rules_dirs=rules_dirs)
        row = rows[reviewed_path]
        if definitions != reviewed_definitions:
            raise ValueError(f"{path}: content hash changed; area definitions changed at {area_definitions_path}; rescore needed before applying labels")
        if content_hash(issue, areas, reviewed_definitions) != row.get("content_hash"):
            raise ValueError(f"{path}: content hash changed; rescore needed before applying labels")
        current = _object(row.get("current"))
        changes: dict[str, str] = {}
        for field, value in _object(approval.get("fields")).items():
            approved = _approved_value(field, _text(value), set(definitions))
            if issue.labels.get(field, "") == approved:
                continue
            if issue.labels.get(field, "") != current.get(field):
                raise ValueError(f"{path}: approved field {field} changed since review")
            changes[field] = approved
        if changes:
            edits.append(ApprovedEdit(path, before, _edit_frontmatter(before, changes), stat.S_IMODE(path.stat().st_mode)))
    for edit in edits:
        _write_edit(edit)
    return [edit.path for edit in edits]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("approvals", type=Path)
    args = parser.parse_args(argv, namespace=ApplyArguments())
    try:
        for path in apply_labels(args.approvals):
            print(path)
    except (OSError, ValueError) as error:
        print(f"issue-score: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
