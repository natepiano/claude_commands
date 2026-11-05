#!/usr/bin/env python3
"""Convert known proposal references to staff ids and preserve historical names."""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from issues import Issue, frontmatter
from scripts.production import session_staff, staff
from proposal_issue import REMOVED_FIELDS, issue_lock, read_issue, write_issue, remove_fields, set_fields


class UnresolvedReference(ValueError):
    """Several staff rows match a stored reference."""


def reference(record: staff.StaffSnapshot, value: str, kinds: tuple[staff.Kind, ...], parent: str = "", *,
              sources: session_staff.MemberSources) -> str:
    row = record.by_id(staff.StaffId(value))
    if row is not None and row.kind in kinds:
        return row.id
    original = value
    named = re.fullmatch(r"(.+?)\s*\(([^()]+)\)", value)
    if named:
        parent = reference(record, named[2].strip(), ("showrunner",), sources=sources)
        value = named[1].strip()
    rows = [row for row in record.rows if row.kind in kinds and row.name == value
            and (not parent or row.reports_to(staff.StaffId(parent)))]
    if not rows and "showrunner" in kinds:
        slug = sources.canonical_slug(value)
        found = sources.member(session_staff.ShowrunnerMember(slug), record)
        if isinstance(found, session_staff.MemberFound) and (not parent or found.row.reports_to(staff.StaffId(parent))):
            rows = [found.row]
    if len(rows) > 1:
        raise UnresolvedReference(f"{value}: no unique staff row")
    return rows[0].id if rows else original


def migrate(folder: Path, *, limit: int = 0) -> tuple[int, list[str]]:
    """Convert complete files atomically. Stop after the requested number of changed files."""
    changed = 0
    problems: list[str] = []
    sources = session_staff.member_sources(record=staff.snapshot())
    for path in sorted(folder.rglob("*.md")):
        with issue_lock(path):
            issue = Issue(path, frontmatter(path))
            if "proposal" not in issue.fields.get("tags", []):
                continue
            record = staff.snapshot()
            fields: dict[str, str] = {}
            references: tuple[tuple[str, tuple[staff.Kind, ...]], ...] = (
                ("approved_in", ("showrunner",)), ("proposed_by", ("showrunner", "unit")), ("unit", ("unit",)))
            try:
                for key, kinds in references:
                    value = issue.value(key)
                    if value:
                        parent = fields.get("approved_in", "") if key == "unit" else ""
                        fields[key] = reference(record, value, kinds, parent, sources=sources)
            except UnresolvedReference as error:
                problems.append(f"{path}: {error}")
                continue
            original = read_issue(path)
            text, _ = set_fields(original, fields, path)
            text, _ = remove_fields(text, REMOVED_FIELDS, path)
            if text != original:
                write_issue(path, text)
                changed += 1
                if limit and changed == limit:
                    break
    return changed, problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("folder", type=Path)
    _ = parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    changed, problems = migrate(cast(Path, args.folder), limit=cast(int, args.limit))
    print(f"{changed} changed")
    for problem in problems:
        print(problem, file=sys.stderr)
    return 2 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
