#!/usr/bin/env python3
"""Print every proposal still open for the user as one Markdown table.

Usage: proposals_table.py [--production <production doc>]

Without `--production`, the doc is the one held by the update timer of the calling session
(`CLAUDE_CODE_SESSION_ID`). Three sources, in this order:

  1. issues: each `*.md` directly in the issues folder the production doc's `Proposals` line
     names, while its frontmatter's status is open and its tags hold `proposal`;
  2. the plan backlog: ledger lines ending `proposed` or `deferred`;
  3. add-ons: the entries each unit's runs hold until the showrunner says build, save for later
     or drop.

Each row says what the change is and what the user can do about it, or for an add-on who decides
it. It reads only, and writes nothing.
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
from production_units import Refusal
from issues import Issue
from proposal_issue import AwaitingApproval, InProgress, ProposalProgress, proposal_progress
from proposals_folder import open_proposals

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "delegate"))
import phase_table
from scripts.delegate import plan_history

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import unit_runs
from user_zone import user_zone

BACKLOG_DIR = Path(os.environ.get("PLAN_BACKLOG_DIR") or Path.home() / ".local/state/plan-backlog")
# A run keeps its pending add-ons in a directory named by its run id, under either run-folder name.
SESSION_ROOTS = unit_runs.run_roots()
PENDING_NAME = "next_items_pending.md"
# The Production Context line that names the issues folder a production files its proposals in.
# A state says what the user can do, in their terms.
REVIEW_STATE = "waiting for your review"
IN_PROGRESS_STATE = "in progress"
# An add-on is the showrunner's to settle, not the user's (user, 2026-10-12).
ADD_ON_STATE = "the showrunner decides: build, save for later, or drop"
BACKLOG_STATES = {"proposed": REVIEW_STATE, "deferred": "deferred until you ask for it"}
UNTITLED_ADD_ON = "an add-on with no description"
NONE = "—"
# A cell longer than its width is cut, so one long sentence cannot widen the whole table.
TITLE_WIDTH = 110
STATE_WIDTH = 140

BACKLOG_LINE = re.compile(r"^- (?P<date>\d{4}-\d{2}-\d{2}) (?P<title>.+) — (?P<state>proposed|deferred)\s*$")
# The file a backlog line names, as `(file.md)` or `([[file]])`, closes its title.
BACKLOG_FILE = re.compile(r"\s*\((?:\[\[(?P<link>[^\]]+)\]\]|(?P<file>[^()]+\.md))\)$")
BY_LINE = re.compile(r"(?:Proposed|Written) (?P<date>\d{4}-\d{2}-\d{2}) by (?P<who>[^.,(]+)")
# A pending add-on is one top-level list item with its fields on the lines below it. Units
# write its title in three ways: on a `Title:` line, as the item's bold lead, or after the
# action word. The action word alone is never a title.
ADD_ON_ENTRY = re.compile(r"^- (?P<text>.+)$")
ADD_ON_FIELD = re.compile(r"^\s+(?:- )?(?P<name>[A-Z][A-Za-z ]*):\s*(?P<value>.+)$")
ADD_ON_BOLD = re.compile(r"^\*\*(?P<lead>.+?)\*\*(?P<rest>.*)$")
ADD_ON_ACTION = re.compile(r"^(?:add|change|remove)\s*[·:]\s*", re.IGNORECASE)
ACTION_WORDS = frozenset({"add", "change", "remove"})
# Where an entry with no title finds its words, in this order.
DESCRIBING_FIELDS = ("proposed", "need", "why needed", "why")
SENTENCE_END = re.compile(r"(?<=[.!?])\s")


class Row(NamedTuple):
    proposer: str
    proposal: str
    date: str
    state: str


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
        rows.append(Row(proposer or "plan backlog", title, found["date"], BACKLOG_STATES[found["state"]]))
    return rows


def issue_state(issue: Issue, progress: ProposalProgress) -> str:
    """In progress by its unit once approved or started, else the user's to review."""
    match progress:
        case AwaitingApproval():
            return REVIEW_STATE
        case InProgress():
            unit = issue.value("unit")
            return f"{IN_PROGRESS_STATE} by {unit}" if unit else IN_PROGRESS_STATE


class IssueRow(NamedTuple):
    """A proposal issue's row and the progress it was built from."""
    progress: ProposalProgress
    row: Row

    def order(self) -> tuple[bool, str, str]:
        """The waiting rows first, then the ones in progress, each oldest first."""
        in_progress = isinstance(self.progress, InProgress)
        return in_progress, self.row.date, self.row.proposal


def issue_row(issue: Issue) -> IssueRow:
    """The issue's row, its title the P number and the file's name, with its progress read once."""
    progress = proposal_progress(issue)
    return IssueRow(progress, Row(issue.value("proposed_by") or NONE,
                                  f"{issue.value('proposal_id')} {issue.path.stem}".lstrip(),
                                  issue.value("date_created") or NONE, issue_state(issue, progress)))


def issue_rows(found: list[Issue]) -> list[Row]:
    """One row per open proposal issue: the waiting ones, then the ones in progress, each oldest first."""
    return [found_row.row for found_row in sorted(map(issue_row, found), key=IssueRow.order)]


def file_date(path: Path) -> str:
    try:
        return f"{datetime.fromtimestamp(path.stat().st_mtime, user_zone()):%Y-%m-%d}"
    except OSError:
        return NONE


def head_title(text: str) -> str:
    """The title an entry's first line holds, or nothing when that line is only its action."""
    bold = ADD_ON_BOLD.match(text)
    if bold is not None:
        lead = bold["lead"].strip()
        text = bold["rest"].lstrip(" —–-:·") if lead.lower() in ACTION_WORDS else lead
    if text.startswith("Action:"):
        return ""
    title = ADD_ON_ACTION.sub("", text.strip()).split(" · ")[0].strip()
    return "" if title.lower() in ACTION_WORDS else title


def add_on_titles(lines: list[str]) -> list[str]:
    """What each entry of a pending add-on list is, in the list's order: its title, else the
    first sentence of the field that describes it."""
    starts = [index for index, line in enumerate(lines) if ADD_ON_ENTRY.match(line)]
    titles: list[str] = []
    for start, end in zip(starts, [*starts[1:], len(lines)]):
        fields = {found["name"].strip().lower(): found["value"].strip()
                  for line in lines[start + 1:end] if (found := ADD_ON_FIELD.match(line))}
        described = next((SENTENCE_END.split(fields[name], maxsplit=1)[0].rstrip(".")
                          for name in DESCRIBING_FIELDS if name in fields), "")
        titles.append(fields.get("title") or head_title(lines[start][2:].strip()) or described or UNTITLED_ADD_ON)
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
        for run in plan_history.plan_runs(path):
            rows.extend(row for root in SESSION_ROOTS for row in add_on_rows(unit, root / run.stem / PENDING_NAME))
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


def escaped(cell: str, width: int) -> str:
    text = " ".join(cell.split()).replace("|", "\\|")
    return text if len(text) <= width else text[: width - 1].rstrip() + "…"


def table(rows: list[Row]) -> str:
    lines = ["| Proposed by | Proposal | Date | State |", "| --- | --- | --- | --- |"]
    for row in rows:
        cells = (escaped(row.proposer, TITLE_WIDTH), escaped(row.proposal, TITLE_WIDTH), row.date,
                 escaped(row.state, STATE_WIDTH))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def open_rows(doc: Path, records: dict[str, object]) -> list[Row]:
    plans = unit_plan_paths(records)
    rows = issue_rows(open_proposals(doc))
    rows += backlog_rows(BACKLOG_DIR)
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
