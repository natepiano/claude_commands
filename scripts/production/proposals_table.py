#!/usr/bin/env python3
"""Print every proposal still open for the user as one Markdown table.

Usage: proposals_table.py [--production <production doc>] [--next]

Without `--production`, the doc is the one held by the update timer of the calling session
(`CLAUDE_CODE_SESSION_ID`). Three sources, in this order:

  1. issues: each `*.md` directly in the issues folder the production doc's `Proposals` line
     names, while its frontmatter's status is open and its tags hold `proposal`;
  2. the plan backlog: ledger lines ending `proposed` or `deferred`;
  3. add-ons: the entries each unit's runs hold until the showrunner says build, save for later
     or drop.

Each row says what the change is and what the user can do about it, or for an add-on who decides
it. The rows waiting for the user's review come first, in urgency.py's order: an issue's level is
its optional `review_urgency` (`review_due` with soon, `review_marked` with user), its value the
score of its proposed stars, its prerequisite the first of its `depends_on`. The table reads only.
`--next` prints the one proposal /next shows and pins it until it leaves the waiting rows.
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from typing import NamedTuple, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.production import showrunners
from production_units import Refusal
from issues import Issue
from proposal_issue import AwaitingApproval, InProgress, ProposalProgress, proposal_progress
from proposals_folder import ledger, open_proposals, owners, scope_docs, slug_of

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "direct"))
import phase_table
from scripts.direct import plan_history

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import unit_runs
import urgency
from user_zone import user_zone

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "prioritize"))
from scripts.prioritize import renumber

# A backlog line's key: this, its ledger's path, then its title.
BACKLOG_KEY = "backlog: "
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
# The stars a proposal's writer proposes, on its `Proposed ranking:` line until the user confirms them.
RANKING_LINE = re.compile(r"^Proposed ranking:(?P<values>.*)$", re.MULTILINE)
RANKING_GOAL = re.compile(r"\bgoal (?P<ordinal>\d+) - ")
STAR_FIELDS = ("alignment", "impact", "urgency", "effort")
UNSCORED = -math.inf


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
    """The rows of the plan backlog folder's ledger.md."""
    return ledger_rows(backlog / "ledger.md")


def ledger_rows(ledger: Path) -> list[Row]:
    """The rows of one improvement ledger; a file a line names is beside the ledger."""
    backlog = ledger.parent
    rows: list[Row] = []
    for line in read_lines(ledger):
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
    """A proposal issue's row, the progress it was built from, and what orders it while it waits."""
    progress: ProposalProgress
    entry: urgency.Entry[Row]

    def order(self) -> tuple[str, str]:
        """Oldest first."""
        return self.entry.item.date, self.entry.item.proposal


def stars(text: str) -> int:
    return text.count(renumber.STAR)


def proposal_score(issue: Issue) -> float:
    """The score the vault's ranking gives the issue's stars, from its frontmatter once confirmed, else
    from its `Proposed ranking:` line; UNSCORED when either lacks a value.

    The goal's bonus counts down from the first goal, so it differs from the vault's by one amount
    for every proposal and keeps their order.
    """
    goal_text = issue.value("backlog_goal")
    values = {name: stars(issue.value(f"backlog_{name}")) for name in STAR_FIELDS}
    if not goal_text or not all(values.values()):
        line = RANKING_LINE.search(read_body(issue.path))
        parts = {part.strip().split(" ", 1)[0]: part.strip() for part in (line["values"] if line else "").split("·")}
        goal_text = parts.get("goal", "").removeprefix("goal ")
        values = {name: stars(parts.get(name, "")) for name in STAR_FIELDS}
    ordinal = re.match(r"(\d+) - ", goal_text)
    if ordinal is None or not all(1 <= count <= len(renumber.URGENCY_BONUSES) for count in values.values()):
        return UNSCORED
    goal = renumber.Goal(value=goal_text, ordinal=int(ordinal[1]), bonus=-2 * int(ordinal[1]))
    return renumber.calculate_score(goal, {f"backlog_{name}": count for name, count in values.items()})


def read_body(path: Path) -> str:
    text = "\n".join(read_lines(path))
    parts = text.split("\n---", 2) if text.startswith("---") else ["", "", text]
    return parts[-1]


def day(text: str) -> datetime | None:
    try:
        moment = date.fromisoformat(text)
    except ValueError:
        return None
    return datetime(moment.year, moment.month, moment.day)


def issue_urgency(issue: Issue) -> urgency.Urgency:
    """The issue's `review_urgency`, `review_due` and `review_marked`; a value it cannot read is normal."""
    try:
        return urgency.from_fields({key: issue.value(f"review_{key}") for key in ("urgency", "due", "marked")})
    except urgency.UrgencyError as error:
        print(f"proposals_table: {issue.path.name}: {error}; read as normal", file=sys.stderr)
        return urgency.Urgency()


def issue_row(issue: Issue) -> IssueRow:
    """The issue's row, its title the P number and the file's name, with its progress read once."""
    progress = proposal_progress(issue)
    level = issue_urgency(issue) if isinstance(progress, AwaitingApproval) else urgency.Urgency()
    shown = urgency.cell(level)
    row = Row(issue.value("proposed_by") or NONE, f"{issue.value('proposal_id')} {issue.path.stem}".lstrip(),
              issue.value("date_created") or NONE,
              issue_state(issue, progress) + (f" · {shown}" if shown else ""))
    waits = isinstance(progress, AwaitingApproval)
    entry = urgency.Entry(str(issue.path), row, level, day(row.date),
                          (proposal_score(issue) if waits else UNSCORED,),
                          next(iter(issue.fields.get("depends_on", [])), ""))
    return IssueRow(progress, entry)


def issue_entries(found: list[Issue]) -> tuple[list[urgency.Entry[Row]], list[Row]]:
    """The waiting issues, to be ordered, and the rows of the ones in progress, oldest first."""
    rows = sorted(map(issue_row, found), key=IssueRow.order)
    folder = {issue.path.stem: str(issue.path) for issue in found}
    # `depends_on` names an issue by its file name; an entry's prerequisite is its key, the path.
    waiting = [replace(row.entry, after=folder.get(row.entry.after, ""))
               for row in rows if isinstance(row.progress, AwaitingApproval)]
    return waiting, [row.entry.item for row in rows if isinstance(row.progress, InProgress)]


def issue_rows(found: list[Issue]) -> list[Row]:
    """One row per open proposal issue: the waiting ones in urgency.py's order, then the ones in progress."""
    waiting, in_progress = issue_entries(found)
    return [entry.item for entry in urgency.order(waiting)] + in_progress


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


def queue(doc: Path) -> str:
    """The name urgency.py pins the proposal /next showed under."""
    return f"proposal-{slug_of(doc)}"


def scope_issues(doc: Path, below: bool = False) -> list[Issue]:
    """The open proposal issues of the production, and with `below` of each one below it, from each one's folder."""
    found: dict[Path, Issue] = {}
    names = owners(doc, below)
    for each in scope_docs(doc, below):
        for issue in open_proposals(each, names):
            _ = found.setdefault(issue.path, issue)
    return list(found.values())


def improvement_ledgers(doc: Path, below: bool = False) -> list[Path]:
    """The Improvement queue ledger of the production, and with `below` of each one below it, once each."""
    return list(dict.fromkeys(found for each in scope_docs(doc, below) if (found := ledger(each)) is not None))


def waiting_entries(doc: Path, below: bool = False) -> tuple[list[urgency.Entry[Row]], list[Row]]:
    """The proposals waiting for the user's review, to be ordered, then the other issue and backlog rows.

    Only the production's own, and with `below` those of each production below it: issues their
    proposer field gives to one of them, and the lines of the Improvement queue ledgers their docs name.
    """
    waiting, in_progress = issue_entries(scope_issues(doc, below))
    rest = list(in_progress)
    for found in improvement_ledgers(doc, below):
        for row in ledger_rows(found):
            if row.state == REVIEW_STATE:
                waiting.append(urgency.Entry(f"{BACKLOG_KEY}{found}: {row.proposal}", row, since=day(row.date),
                                             value=(UNSCORED,)))
            else:
                rest.append(row)
    return waiting, rest


def open_rows(doc: Path, records: dict[str, object]) -> list[Row]:
    """The waiting proposals, the one /next shows first; then the rest; then the add-ons."""
    waiting, rows = waiting_entries(doc)
    rows = [entry.item for entry in urgency.queue_order(waiting, queue(doc))] + rows
    for unit, plan in unit_plan_paths(records).items():
        rows.extend(unit_add_on_rows(unit, plan))
    return rows


def proposal_text(shown: urgency.Entry[Row]) -> str:
    """A waiting proposal as /next prints it: its title, then the file to read."""
    where = Path(shown.key.removeprefix(BACKLOG_KEY).split(": ", 1)[0])
    return f"{shown.item.proposal}\n{where}"


def next_proposal(doc: Path) -> str:
    """The proposal /next shows, and the file to read: the one shown last while it waits, else the first."""
    waiting, _ = waiting_entries(doc)
    shown = urgency.top(waiting, queue(doc))
    return proposal_text(shown) if shown is not None else "no proposal is waiting for your review"


def main(arguments: list[str]) -> int:
    one = "--next" in arguments
    match [argument for argument in arguments if argument != "--next"]:
        case ["--production", doc_text]:
            doc: Path | None = Path(doc_text)
        case []:
            doc = showrunners.production_of(os.environ.get("CLAUDE_CODE_SESSION_ID", ""))
            if doc is None:
                print("proposals_table: this session is no production's showrunner; pass --production <doc>",
                      file=sys.stderr)
                return 1
        case _:
            print("usage: proposals_table.py [--production <production doc>] [--next]", file=sys.stderr)
            return 2
    if one:
        print(next_proposal(doc.expanduser().resolve()))
        return 0
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
