#!/usr/bin/env python3
"""Print every proposal still open for the user as one Markdown table.

Usage: proposals_table.py [--production <production doc>]

Without `--production`, the doc is the one held by the update timer of the calling session
(`CLAUDE_CODE_SESSION_ID`). Three sources, in this order:

  1. the plan backlog: ledger lines ending `proposed` or `deferred`;
  2. proposal docs: `docs/plans/proposal-*.md` in the trunk and unit worktrees, while the doc's
     status line still says PROPOSAL;
  3. add-ons: the entries each unit's runs hold for the user's `next` or `drop`.

It reads only, and writes nothing.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import NamedTuple, cast

import showrunners
from add_unit import Refusal

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "delegate"))
import phase_table
import progress_history

BACKLOG_DIR = Path(os.environ.get("PLAN_BACKLOG_DIR") or Path.home() / ".local/state/plan-backlog")
# A run keeps its pending add-ons here, in a directory named by its run id.
SESSION_ROOT = Path("/tmp/claude/delegate")
PENDING_NAME = "next_items_pending.md"
ADD_ON_STATE = "add-on: next or drop"
NONE = "—"
# A title longer than this is cut, so one long sentence cannot widen the whole table.
TITLE_WIDTH = 90

BACKLOG_LINE = re.compile(r"^- (?P<date>\d{4}-\d{2}-\d{2}) (?P<title>.+) — (?P<state>proposed|deferred)\s*$")
# The file a backlog line names, as `(file.md)` or `([[file]])`, closes its title.
BACKLOG_FILE = re.compile(r"\s*\((?:\[\[(?P<link>[^\]]+)\]\]|(?P<file>[^()]+\.md))\)$")
BY_LINE = re.compile(r"(?:Proposed|Written) (?P<date>\d{4}-\d{2}-\d{2}) by (?P<who>[^.,(]+)")
STATUS_LINE = re.compile(r"^>\s*\*\*Status:(?P<status>[^*]*)")
# A pending add-on is one top-level list item. Units write its title in three ways: on a
# `Title:` line under `- Action: add`, as the item's bold lead, or after the action word.
ADD_ON_ENTRY = re.compile(r"^- (?P<text>.+)$")
ADD_ON_TITLE = re.compile(r"^\s+(?:- )?Title:\s*(?P<title>.+)$")
ADD_ON_BOLD = re.compile(r"^\*\*(?P<title>.+?)\*\*")
ADD_ON_ACTION = re.compile(r"^(?:add|change|remove)\s*[·:]\s*", re.IGNORECASE)


class Row(NamedTuple):
    proposer: str
    proposal: str
    date: str
    state: str


class Worktree(NamedTuple):
    """A checkout of the production: the trunk under the name `trunk`, or one unit's."""
    owner: str
    root: Path


def read_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return []


def by_line(lines: list[str]) -> tuple[str, str]:
    """The proposer and date a doc states as `Proposed|Written <date> by <who>`, or two empty strings."""
    found = BY_LINE.search(" ".join(lines))
    return (found["who"].strip(), found["date"]) if found else ("", "")


def backlog_rows(backlog: Path) -> list[Row]:
    rows: list[Row] = []
    for line in read_lines(backlog / "ledger.md"):
        found = BACKLOG_LINE.match(line)
        if found is None:
            continue
        title = found["title"]
        named = BACKLOG_FILE.search(title)
        proposer = ""
        if named is not None:
            title = title[: named.start()]
            file_name = named["file"] or f"{named['link']}.md"
            proposer, _ = by_line(read_lines(backlog / file_name)[:12])
        rows.append(Row(proposer or "plan backlog", title, found["date"], found["state"]))
    return rows


def doc_row(path: Path, owner: str) -> Row | None:
    """The row of one proposal doc, or None once its status line no longer says PROPOSAL."""
    lines = read_lines(path)
    status = next((found["status"] for line in lines[:12] if (found := STATUS_LINE.match(line))), None)
    if status is not None and "PROPOSAL" not in status:
        return None
    heading = next((line[2:].strip() for line in lines if line.startswith("# ")), path.stem)
    proposer, date = by_line(lines[:12])
    if not date:
        date = file_date(path)
    return Row(proposer or owner, heading.removeprefix("Proposal:").strip(), date, "proposed")


def doc_rows(worktrees: list[Worktree]) -> list[Row]:
    """One row per proposal doc name; a unit's copy is read before the trunk's, so the unit is its owner."""
    rows: dict[str, Row] = {}
    for worktree in sorted(worktrees, key=lambda each: each.owner == "trunk"):
        for path in sorted((worktree.root / "docs/plans").glob("proposal-*.md")):
            if path.name in rows:
                continue
            row = doc_row(path, worktree.owner)
            if row is not None:
                rows[path.name] = row
    return sorted(rows.values(), key=lambda row: row.date)


def file_date(path: Path) -> str:
    try:
        return f"{datetime.fromtimestamp(path.stat().st_mtime):%Y-%m-%d}"
    except OSError:
        return NONE


def add_on_titles(lines: list[str]) -> list[str]:
    """The title of each entry of a pending add-on list, in the list's order."""
    starts = [index for index, line in enumerate(lines) if ADD_ON_ENTRY.match(line)]
    titles: list[str] = []
    for start, end in zip(starts, [*starts[1:], len(lines)]):
        text = lines[start][2:].strip()
        bold = ADD_ON_BOLD.match(text)
        if text.startswith("Action:"):
            title = next((found["title"] for line in lines[start + 1:end] if (found := ADD_ON_TITLE.match(line))), "")
        elif bold is not None:
            title = bold["title"]
        else:
            title = text
        title = ADD_ON_ACTION.sub("", title.strip()).split(" · ")[0]
        titles.append(title.strip() or text)
    return titles


def add_on_rows(unit: str, pending: Path) -> list[Row]:
    """One row per entry of a run's pending add-on list; its date is the list's last change."""
    date = file_date(pending)
    return [Row(unit, title, date, ADD_ON_STATE) for title in add_on_titles(read_lines(pending))]


def unit_plans(plan: Path) -> list[Path]:
    """The paths a unit's runs may have recorded its plan under: where it is now, and `docs/plans`
    when the finished plan has moved to `docs/as-built`."""
    if plan.parent.name == "as-built":
        return [plan, plan.parent.parent / "plans" / plan.name]
    return [plan]


def unit_add_on_rows(unit: str, plan: Path) -> list[Row]:
    rows: list[Row] = []
    for path in unit_plans(plan):
        for run in progress_history.plan_runs(path):
            rows.extend(add_on_rows(unit, SESSION_ROOT / run.stem / PENDING_NAME))
    return rows


def unit_plan_paths(records: dict[str, object]) -> dict[str, Path]:
    plans: dict[str, Path] = {}
    for unit, record in records.items():
        plan = cast(dict[str, object], record).get("plan") if isinstance(record, dict) else None
        if isinstance(plan, str):
            plans[unit] = Path(plan)
    return plans


def worktree_of(plan: Path) -> Path:
    """A plan lives at `<worktree>/docs/<plans|as-built>/<name>.md`."""
    return plan.parent.parent.parent


def escaped(cell: str) -> str:
    text = " ".join(cell.split()).replace("|", "\\|")
    return text if len(text) <= TITLE_WIDTH else text[: TITLE_WIDTH - 1].rstrip() + "…"


def table(rows: list[Row]) -> str:
    lines = ["| Proposed by | Proposal | Date | State |", "| --- | --- | --- | --- |"]
    lines.extend("| " + " | ".join(escaped(cell) for cell in row) + " |" for row in rows)
    return "\n".join(lines)


def open_rows(doc: Path, records: dict[str, object]) -> list[Row]:
    plans = unit_plan_paths(records)
    worktrees = [Worktree("trunk", worktree_of(doc))]
    worktrees.extend(Worktree(unit, worktree_of(plan)) for unit, plan in plans.items())
    rows = backlog_rows(BACKLOG_DIR) + doc_rows(worktrees)
    for unit, plan in plans.items():
        rows.extend(unit_add_on_rows(unit, plan))
    return rows


def main(arguments: list[str]) -> int:
    match arguments:
        case ["--production", doc_text]:
            doc: Path | None = Path(doc_text)
        case []:
            doc = showrunners.production_of(os.environ.get("CLAUDE_CODE_SESSION_ID", ""))
            if doc is None:
                print("proposals_table: this session is no production's showrunner; pass --production <doc>",
                      file=sys.stderr)
                return 1
        case _:
            print("usage: proposals_table.py [--production <production doc>]", file=sys.stderr)
            return 2
    try:
        records = cast(dict[str, object], json.loads(phase_table.show_production(doc)))
    except (OSError, ValueError, Refusal) as error:
        print(f"proposals_table: {error}", file=sys.stderr)
        return 1
    rows = open_rows(doc.expanduser().resolve(), records)
    print(table(rows) if rows else "no open proposals")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
