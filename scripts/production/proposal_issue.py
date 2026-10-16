#!/usr/bin/env python3
"""Set a hanadocs issue's proposal fields: approve, start, close or stars.

Edits one issue file's frontmatter in place and reads nothing else. Every verb also sets
date_modified to today in the user's zone, written "[[YYYY-MM-DD]]" as the vault writes dates.

Usage:
  proposal_issue.py approve <issue> --showrunner S --unit U --plan P
  proposal_issue.py start <issue>
  proposal_issue.py close <issue> --as-built PATH
  proposal_issue.py stars <issue> --goal G --alignment A --impact I --urgency U --effort E
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
from user_zone import user_zone  # noqa: E402

FENCE = "---"
# Values the vault writes bare; every other value is written double-quoted.
BARE = frozenset({"stage", "status"})
KEY = re.compile(r"^([A-Za-z_][\w-]*):")
GOAL = re.compile(r"^\d+ - \S.*$")
STAR = "⭐"


class Refusal(ValueError):
    """The issue file cannot be edited."""


def _quoted(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _value_line(key: str, value: str) -> str:
    return f"{key}: {value if key in BARE else _quoted(value)}\n"


def _closing_fence(lines: list[str], path: Path) -> int:
    if not lines or lines[0].rstrip("\r\n") != FENCE:
        raise Refusal(f"{path} has no frontmatter")
    for index in range(1, len(lines)):
        if lines[index].rstrip("\r\n") == FENCE:
            return index
    raise Refusal(f"{path} has no closing --- for its frontmatter")


def _key_span(lines: list[str], end: int, key: str) -> tuple[int, int] | None:
    """The key's line and its continuation lines, as a slice, or None when the key is absent."""
    for start in range(1, end):
        match = KEY.match(lines[start])
        if match is None or match.group(1) != key:
            continue
        stop = start + 1
        while stop < end and KEY.match(lines[stop]) is None:
            stop += 1
        return start, stop
    return None


def set_fields(text: str, fields: dict[str, str], path: Path) -> tuple[str, list[str]]:
    """The text with each field set, and the keys whose lines changed."""
    lines = text.splitlines(keepends=True)
    changed: list[str] = []
    for key, value in fields.items():
        end = _closing_fence(lines, path)
        line = _value_line(key, value)
        span = _key_span(lines, end, key)
        if span is None:
            lines.insert(end, line)
            changed.append(key)
            continue
        start, stop = span
        newline = lines[start][len(lines[start].rstrip("\r\n")):] or "\n"
        replacement = line[:-1] + newline
        if lines[start:stop] != [replacement]:
            lines[start:stop] = [replacement]
            changed.append(key)
    return "".join(lines), changed


def _read(path: Path) -> str:
    if not path.is_file():
        raise Refusal(f"{path} is not a file")
    with path.open(encoding="utf-8", newline="") as source:
        return source.read()


def _write(path: Path, text: str) -> None:
    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as temporary:
            temporary_name = temporary.name
            _ = temporary.write(text)
        os.replace(temporary_name, path)
        temporary_name = ""
    finally:
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)


def _stars(name: str, value: str) -> str:
    """One to five stars, from a count or from the stars themselves."""
    count = int(value) if value.isdigit() else len(value) if value and value == STAR * len(value) else 0
    if not 1 <= count <= 5:
        raise Refusal(f"--{name} takes 1 to 5 (or one to five {STAR}), not {value!r}")
    return STAR * count


def _vault_date(today: date) -> str:
    return f"[[{today.isoformat()}]]"


def edit(path: Path, fields: dict[str, str], today: date) -> list[str]:
    """Set the fields and date_modified in the issue at path. Returns the keys whose lines changed."""
    text, changed = set_fields(_read(path), {**fields, "date_modified": _vault_date(today)}, path)
    if changed:
        _write(path, text)
    return changed


def fields_for(args: argparse.Namespace, today: date) -> dict[str, str]:
    verb = cast(str, args.verb)
    if verb == "approve":
        return {
            "showrunner": cast(str, args.showrunner),
            "unit": cast(str, args.unit),
            "plan": cast(str, args.plan),
        }
    if verb == "start":
        return {"stage": "active"}
    if verb == "close":
        return {"as_built": cast(str, args.as_built), "status": "closed", "date_closed": _vault_date(today)}
    goal = cast(str, args.goal)
    if GOAL.match(goal) is None:
        raise Refusal(f"--goal takes a goal like '1 - Ship Hana', not {goal!r}")
    return {
        "backlog_goal": goal,
        **{f"backlog_{name}": _stars(name, cast(str, getattr(args, name))) for name in (
            "alignment", "impact", "urgency", "effort",
        )},
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    verbs = parser.add_subparsers(dest="verb", required=True)
    approve = verbs.add_parser("approve", help="record the user's approval; stage stays backlog")
    _ = approve.add_argument("--showrunner", required=True, help="the showrunner's session name")
    _ = approve.add_argument("--unit", required=True, help="the unit, as in the production's Units table")
    _ = approve.add_argument("--plan", required=True, help="'<plan path> Phase N–M'")
    _ = verbs.add_parser("start", help="stage active")
    close = verbs.add_parser("close", help="set as_built and close")
    _ = close.add_argument("--as-built", required=True, help="the as-built doc, e.g. docs/hana/as-built/<file>.md")
    stars = verbs.add_parser("stars", help="set the five ranking values")
    _ = stars.add_argument("--goal", required=True, help="e.g. '1 - Ship Hana'")
    for name in ("alignment", "impact", "urgency", "effort"):
        _ = stars.add_argument(f"--{name}", required=True, help="1 to 5")
    for verb in (approve, verbs.choices["start"], close, stars):
        _ = verb.add_argument("issue", type=Path)
    args = parser.parse_args(argv)
    # A plan's Issues line writes `~/rust/hanadocs/issues/<name>.md`, quoted since names hold spaces.
    path = cast(Path, args.issue).expanduser()
    today = datetime.now(user_zone()).date()
    try:
        changed = edit(path, fields_for(args, today), today)
    except (Refusal, OSError, UnicodeError) as error:
        print(f"proposal_issue: {error}", file=sys.stderr)
        return 2
    verb = cast(str, args.verb)
    print(f"{verb} {path.name}: set {', '.join(changed)}" if changed else f"{verb} {path.name}: unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
