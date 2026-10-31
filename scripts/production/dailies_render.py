#!/usr/bin/env python3
"""Render the /showrunner:dailies report from its fixed template.

Usage: dailies_render.py [<input.json>] [--chart default|ascii] [--state <state.json>] [--log <log.md>] [--at <YYYY-MM-DDTHH:MM[±HH:MM]>]
       dailies_render.py --footer --zone <IANA zone> [--next-run <HH:MM[+N]>]
                         [--nothing-needed] [--at <YYYY-MM-DDTHH:MM[±HH:MM]>]
                         [--below <outstanding.json>]... [--reports-to <root slug>] [--doc <production doc>]
Both forms take [--outstanding <outstanding.json>].

The input gives each subject's fields; this script owns the layout, so no line
of the template can be dropped or renamed. It refuses, with exit 2, an input
that breaks a template rule: a unit without its phase or `held`, a follow-up
or last phase without `then`, an unknown field, an update too long for the
length, an update or held reason that names another phase without saying
why, a held reason carrying its own examples, a held count the update does
not report against (`<k> of <N>`), an ETA time without its percent, an
ETA that moved CHANGE_NEEDS_WHY_MINUTES or more since the last report
without `why`, a `then` naming a phase at or before the heading's, a goal
without its measured numbers, a unit hold marker that disagrees with the
holder files, or a line using the
production's own plumbing words (PLUMBING).

--state  JSON file holding each unit's last reported phase, ETA, held
         reason and the phase's first ETA. The script reads it to write `(unchanged)` / `(changed:
         ±h:mm)` and, in a simple report, to print a held reason's examples
         only the first time,
         then saves this report's values to it.
--log    appends the `dailies ETAs:` line to this file.
--at     renders as if the clock read this local time (for checks); an offset
         identifies an occurrence in a repeated hour.
--chart  sets the chart mode in CHART_CONF, which every showrunner's dailies
         read; with no input file it only sets the mode.
--footer prints the separated bullet footer that ends every report, alone,
         at the current time in --zone; the checks use it, and no reply
         carries it (user, 2026-10-18). Its Agents bullets
         use the report's words. Hold lines come from BUILD_HOLD_DIR or
         ~/.local/state/build-hold and MAC_TEST_STATE_DIR or
         ~/.local/state/mac-test.
--outstanding  JSON list of what waits on the user, `[{"since":
         "YYYY-MM-DDTHH:MM", "text": "..."}]`; outstanding items suppress
         ` - nothing needed`; the status line shows only their count. A
         missing file is an empty list.
--below  With --footer, once per production below a root showrunner: that
         production's list. Its due items count with --outstanding. A list
         that cannot be read adds the bullet `<slug>: needs-user list
         unreadable`, where the slug is the file's name, and never fails the
         footer.
--reports-to  With --footer, for a showrunner under a root: adds the bullet
         `reports to <root slug>`.
--doc    With --footer, the production doc: the line after the header counts
         its changes, efficiency and proposals (footer_counts.py). A
         report reads the doc from its input's `doc`.

The input format is in ~/.claude/commands/showrunner/dailies.md.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import NamedTuple, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "build_hold"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mac_test"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "whoami"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from atomic_file import write_text_atomic
from scripts.whoami import run_out
from scripts.whoami import note_file
from scripts.whoami.run_out import READINGS_LOG, Reading
from build_hold import ActiveHolders, HoldCycle, HoldState, KnownReleaseEta, NoHolders, Holder, ReleaseEntry, ReleaseRecordReadError, holder_directory, read_cycle, read_holders, release_record_error_line
from mac_test import ActiveMacBlock, NoMacBlock, PendingMacBlock, ci_may_still_be_on, read_block, state_paths
import urgency
from urgency import Urgency, UrgencyError

# The longest update line of each length. `gantt` is the default: the chart, with only what needs the user and
# what changed above it. It takes the input `simple` takes.
LENGTHS = {"gantt": 240, "simple": 240, "page": 480, "elaborate": None}
SHORT_LENGTHS = ("gantt", "simple")
# A gantt report names an ETA that moved by at least this many minutes since the last report.
MAJOR_ETA_MINUTES = 15
PHASE = re.compile(r"^(?:Phase (\d+) of (\d+)|follow-up (\d+) of (\d+)): \S")
TIME = re.compile(r"^\d{1,2}:\d{2}(?:\+\d+)?$")
NONE = ("none measured - requested", "none measured", "no ETA stated yet")
# An ETA that moved this much since the last report carries its reason (user,
# 2026-10-01: why a unit's timing changed is an important detail).
CHANGE_NEEDS_WHY_MINUTES = 15
LABEL_LIMIT = 8
IDLE_LIMIT = 80
RETURN = re.compile(r"\bthe plan at Phase \d+|\bplan done\b")
PHASE_MENTION = re.compile(r"\bPhases? (\d+(?:\s*(?:,|and|-|–|to)\s*\d+)*)|\bP(\d+)\b")
THEN_PHASE_LABEL = re.compile(r"^(\d+(?:\s*[-–]\s*\d+)?)\s*:")
COUNT = re.compile(r"\b(\d+) [a-z]")
EXAMPLES = re.compile(r"\bsuch as\b|\be\.g\.|\bfor example\b")
STARTED = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")
# What a footer and the open-items list say of a lower production's list that cannot be read.
UNREADABLE_LIST = "needs-user list unreadable"
STAMP = "%Y-%m-%dT%H:%M"
NO_REPORT = "no report yet"
NO_ROWS = "No unit is working: every run has finished."
# Words for the production's own coordination, which you never see in the app
# (user, 2026-10-02: "i have no idea what a stale file holds from the merged
# work are released even means"; "retaking every view" and "file clashes get
# settled at its checkpoint" were the same).
PLUMBING = re.compile(r"\b(?:berth|reservations?|incursions?|holders?|file holds?|file clash\w*|writers?|testers?|retak\w*|re-?shoot\w*)\b", re.IGNORECASE)
# A `then` that names another plan's document may name that plan's phases.
OTHER_PLAN = re.compile(r"\S+\.md\b")
WHITE = "⬜"
GREEN = "🟩"
RED = "🟥"
BLUE = "🟦"
BLANK = "  "
# The `ascii` chart: only characters a code font has, so the desktop app keeps
# every row aligned, which it cannot with the coloured squares (user,
# 2026-10-03: the coloured chart by default, this one when remote). The app's
# code font, Anthropic Mono, has `─`, `━` and `●` on one centre line but no
# `┼` or `┤`; those came from another font, 2-3 px lower (2026-10-04). So a
# heavy line marks the earliest-to-latest range instead of crossbars.
LINE = "──"
BAND = "━━"
DOT = "● "
DOT_RANGED = "●━"
# The chart mode every showrunner's dailies use, kept in one file so no
# session has to remember it (user, 2026-10-03). `--chart` sets it.
CHART_CONF = Path.home() / ".local/state/showrunner/dailies.conf"
CHART_KEY = "chart"
NOW_MARK = "▼ "
# A unit under a /build:hold carries this marker on its timeline row; no
# marker means not held (user, 2026-10-03: "Without that marker I will assume
# it is not held"). The hold's start, purpose and release are stated once, in
# the line after the timeline (user, 2026-10-04: "you do not need to repeat
# this in every lane - just at the suffix").
BUILD_HOLD_MARK = "build hold"
CELL_WIDTH = 2
ROW_LABEL_WIDTH = 9
# Right of each row, right-aligned: `Phase N of M - P%` for the whole plan,
# then ten blocks, one per 10% rounded, and a line at 100% (user, 2026-10-03).
PLAN_BLOCKS = 10
PLAN_FILL = "█"
PLAN_END = "│"
PLAN_FULL = "100%"
PLAN_GAP = 3
# A phase that started before the left edge has its start beside its name.
START_FORMAT = "%b-%d %H:%M"
# The timeline always spans 24 one-hour cells, labelled every three hours. It
# rolls to fit the rows: it opens at the three-hour mark at or before the
# earliest phase start, and later only as far as keeps every latest time in
# view, never past now's mark. While the chart is wider than CHART_WIDTH it
# opens later still, three hours at a time, so the plan bars fit. User, 2026-10-04.
WINDOW_HOURS = 24
# Columns the desktop app's code block shows: a 95-column row stayed whole, a
# 107-column row wrapped its plan bar (2026-10-04).
CHART_WIDTH = 95
LABEL_EVERY_HOURS = 3

JsonMap = dict[str, object]


class InputError(Exception):
    pass


@dataclass(frozen=True)
class EtaRange:
    """The earliest and latest the stated time may turn out to be, as clock texts."""

    earliest: str
    latest: str


@dataclass(frozen=True)
class TimedEta:
    """A stated time. `spread`, `percent`, `why`, `detail` and `first` are each `None` when the input states none."""

    time: str
    spread: EtaRange | None
    percent: int | None
    why: str | None
    detail: str | None
    # The phase's first ETA, and the fix rounds added since it.
    first: datetime | None
    fixes: int
    # When the unit stated this time, as the builder first saw it. `None` in an input written by hand.
    stated: datetime | None


@dataclass(frozen=True)
class NoEta:
    """No stated time, and the reason the report prints in its place."""

    reason: str
    detail: str | None


Eta = TimedEta | NoEta


@dataclass(frozen=True)
class Goal:
    target: str
    unit: str
    start: float
    now: float
    aim: float


@dataclass(frozen=True)
class UpcomingWork:
    items: tuple[str, ...]


@dataclass(frozen=True)
class NoUpcomingWork:
    pass


@dataclass(frozen=True)
class IdleWait:
    waits_for: str
    until: datetime


@dataclass(frozen=True)
class NotIdle:
    pass


@dataclass(frozen=True)
class Unit:
    unit: str
    name: str
    label: str
    project: str
    goal: Goal | None
    phase: str
    started: datetime
    held: str | None
    held_examples: str | None
    build_hold: bool
    # The unit's own session holds the builds: the builder writes it, since a holder is named by its session.
    holds_builds: bool
    update: str
    eta: Eta
    waiting_on_it: str | None
    needed: str | None
    needs_user: bool
    upcoming_work: UpcomingWork | NoUpcomingWork
    idle: IdleWait | NotIdle


@dataclass(frozen=True)
class Topic:
    title: str
    update: str
    eta: str
    needed: str | None
    needs_user: bool


@dataclass(frozen=True)
class DueNow:
    pass


@dataclass(frozen=True)
class DeferredUntil:
    at: datetime


OutstandingTiming = DueNow | DeferredUntil


@dataclass(frozen=True)
class Ranking:
    """Where an item goes in the user's list (urgency.py); `follows` is the text of the item it comes after."""
    urgency: Urgency = Urgency()
    follows: str = ""


@dataclass(frozen=True)
class OutstandingReminder:
    since: datetime
    text: str
    timing: OutstandingTiming = DueNow()
    rank: Ranking = Ranking()


@dataclass(frozen=True)
class OutstandingQuestion:
    since: datetime
    text: str
    question: str
    timing: OutstandingTiming = DueNow()
    rank: Ranking = Ranking()
    # The unit (or showrunner) that filed it with `update_registration.py ask`; the answer goes back to it.
    asked_by: str = ""


@dataclass(frozen=True)
class ReviewedOutstandingQuestion:
    since: datetime
    text: str
    question: str
    reviewed: str
    timing: OutstandingTiming = DueNow()
    rank: Ranking = Ranking()
    # The unit (or showrunner) that filed it with `update_registration.py ask`; the answer goes back to it.
    asked_by: str = ""


@dataclass(frozen=True)
class ReviewUnavailableOutstandingQuestion:
    since: datetime
    text: str
    question: str
    reason: str
    timing: OutstandingTiming = DueNow()
    rank: Ranking = Ranking()
    # The unit (or showrunner) that filed it with `update_registration.py ask`; the answer goes back to it.
    asked_by: str = ""


Outstanding = (OutstandingReminder | OutstandingQuestion | ReviewedOutstandingQuestion
               | ReviewUnavailableOutstandingQuestion)


@dataclass(frozen=True)
class UnreadableMacBlock:
    """A Mac block whose state file could not be decoded or read."""

    path: Path


MacBlockFooterState = NoMacBlock | PendingMacBlock | ActiveMacBlock | UnreadableMacBlock


@dataclass(frozen=True)
class ProductionCount:
    """One count config/dailies.conf names for this production; `None` when its source is missing."""

    label: str
    number: int | None


@dataclass(frozen=True)
class LastReportedEta:
    text: str
    moment: datetime


@dataclass(frozen=True)
class NoLastReportedEta:
    pass


@dataclass(frozen=True)
class LastUnitReport:
    phase: str
    eta: LastReportedEta | NoLastReportedEta
    held: str | None
    first: datetime | None


@dataclass(frozen=True)
class NoLastUnitReport:
    pass


Previous = LastUnitReport | NoLastUnitReport


@dataclass(frozen=True)
class ResolvedEtaMoment:
    moment: datetime


@dataclass(frozen=True)
class NoResolvedEtaMoment:
    pass


ResolvedEta = ResolvedEtaMoment | NoResolvedEtaMoment


@dataclass(frozen=True)
class Estimate:
    started: datetime
    eta: datetime
    earliest: datetime
    latest: datetime


@dataclass(frozen=True)
class PlanProgress:
    number: int
    total: int
    percent: int


@dataclass(frozen=True)
class Row:
    name: str
    estimate: Estimate | None
    build_hold: bool
    plan: PlanProgress | None
    # Written last on the row: a lower production's counts and marks.
    note: str = ""
    # Lines printed under the row, outside the chart's width fitting.
    lines_below: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReportsEvery:
    minutes: int


@dataclass(frozen=True)
class ReportsOnDemand:
    pass


@dataclass(frozen=True)
class PendingHandoffs:
    count: int
    # An aware instant keeps the elapsed age exact across a daylight-saving change.
    oldest_pending_since: datetime


@dataclass(frozen=True)
class NoPendingHandoffs:
    pass


@dataclass(frozen=True)
class StyleSummary:
    rules_done: int
    rules_total: int
    unclassified_rules: int
    cells_current_pct: float
    stale: int
    proposals: int
    waiting: PendingHandoffs | NoPendingHandoffs
    synced_at: datetime
    synced_tip: str


@dataclass(frozen=True)
class NoStyleSummary:
    pass


@dataclass(frozen=True)
class StyleSummaryUnavailable:
    reason: str


@dataclass(frozen=True)
class ChildReport:
    """A production below this one, as its own last report saved it. Times are in this report's zone."""

    written: datetime
    cadence: ReportsEvery | ReportsOnDemand
    units: int
    held: int
    rows: list[Row]
    style: StyleSummary | NoStyleSummary | StyleSummaryUnavailable


@dataclass(frozen=True)
class NoChildReport:
    """A production below this one that has saved no report yet."""


@dataclass(frozen=True)
class Child:
    """One production directly below this one: a single chart row, and its own rows too when `expand` is set."""

    name: str
    report: ChildReport | NoChildReport
    # Due items for the user in its list and the lists below it, and each list that could not be read.
    open: int
    unreadable: list[str]
    expand: bool


@dataclass(frozen=True)
class Report:
    length: str
    chart: str
    zone: str
    next_run: str | None
    build_hold: HoldState
    mac_block: MacBlockFooterState
    units: list[Unit]
    topics: list[Topic]
    counts: list[ProductionCount]
    children: list[Child]
    # The root this production's showrunner is under; empty for a root.
    reports_to: str
    # The production doc the footer's counts line reads; None for an input that names none.
    doc: Path | None = None


@dataclass(frozen=True)
class ChartStyle:
    """The timeline's cells, each two columns wide, and whether `(earliest–latest)` follows the ETA."""

    run: str
    earliest: str
    before_eta: str
    eta: str
    eta_ranged: str
    range_fill: str
    latest: str
    show_range: bool


@dataclass(frozen=True)
class _AgentWeekUsage:
    reset_at: datetime
    checked_at: datetime
    used_percent: float


CHART_STYLES = {
    "default": ChartStyle(WHITE, GREEN, WHITE, BLUE, BLUE, BLANK, RED, show_range=True),
    "ascii": ChartStyle(LINE, BAND, BAND, DOT, DOT_RANGED, BAND, BAND, show_range=False),
}


def parse_time(text: str, now: datetime) -> datetime:
    """`HH:MM`, with `+N` for N days later; a time more than two hours before now is tomorrow."""
    clock_part, _, days = text.partition("+")
    hour_text, minute_text = clock_part.split(":")
    moment = now.replace(hour=int(hour_text), minute=int(minute_text), second=0, microsecond=0)
    if days:
        return moment + timedelta(days=int(days))
    if moment < now - timedelta(hours=2):
        return moment + timedelta(days=1)
    return moment


def parse_range_end(text: str, now: datetime, eta: datetime, *, earliest: bool) -> datetime:
    """A range end, on the ETA's day or the one next to it that keeps it on its side: earliest at or before, latest at or after. `+N` counts days from the report."""
    if "+" in text:
        return parse_time(text, now)
    hour_text, minute_text = text.split(":")
    moment = eta.replace(hour=int(hour_text), minute=int(minute_text), second=0, microsecond=0)
    if earliest and moment > eta:
        return moment - timedelta(days=1)
    if not earliest and moment < eta:
        return moment + timedelta(days=1)
    return moment


def mark_at_or_before(moment: datetime) -> datetime:
    return moment.replace(hour=moment.hour - moment.hour % LABEL_EVERY_HOURS, minute=0, second=0, microsecond=0)


def window_start(now: datetime, rows: list[Row]) -> datetime:
    """The three-hour mark at or before the earliest start, moved later only as far as keeps every latest time and now in view, and never past now's mark."""
    estimates = [row.estimate for row in rows if row.estimate is not None]
    now_mark = mark_at_or_before(now)
    if not estimates:
        return now_mark
    first_start = mark_at_or_before(min(estimate.started for estimate in estimates))
    end = max(now, *(estimate.latest for estimate in estimates)).replace(minute=0, second=0, microsecond=0)
    # The latest mark that leaves `end` in the last cell or earlier.
    earliest_open = end - timedelta(hours=WINDOW_HOURS - 1)
    fits_end = mark_at_or_before(earliest_open)
    if fits_end < earliest_open:
        fits_end += timedelta(hours=LABEL_EVERY_HOURS)
    return min(max(first_start, fits_end), now_mark)


def chart_width(lines: list[str]) -> int:
    return max(display_width(line) for line in lines)


def draw(now: datetime, rows: list[Row], style: ChartStyle) -> list[str]:
    """The chart from `window_start`, or, while it is wider than CHART_WIDTH, the narrowest opening up to now's mark."""
    opening = window_start(now, rows)
    now_mark = mark_at_or_before(now)
    chart = draw_from(opening, now, rows, style)
    while chart_width(chart) > CHART_WIDTH and opening < now_mark:
        opening += timedelta(hours=LABEL_EVERY_HOURS)
        later = draw_from(opening, now, rows, style)
        if chart_width(later) < chart_width(chart):
            chart = later
    result = [chart[0]]
    for line, row in zip(chart[1:], rows, strict=True):
        result.append(line)
        result.extend(row.lines_below)
    return result


def draw_from(start: datetime, now: datetime, rows: list[Row], style: ChartStyle) -> list[str]:
    """24 hourly cells from `start` in `style`: the run from the phase's start (or the left edge) to the ETA, marks at the earliest time, the ETA and the latest, and `before_eta` and `range_fill` cells between them; `→` past the right edge; a start before the left edge is written before the cells."""

    def column(moment: datetime) -> int:
        return int((moment - start).total_seconds() // 3600)

    axis: list[str] = []
    for index in range(WINDOW_HOURS):
        hour = start + timedelta(hours=index)
        if index == column(now):
            axis.append(NOW_MARK)
        elif hour.hour % LABEL_EVERY_HOURS == 0:
            axis.append(f"{hour:%H}")
        else:
            axis.append(BLANK)
    early = {row.name: f"{row.estimate.started:{START_FORMAT}} " for row in rows
             if row.estimate is not None and column(row.estimate.started) < 0}
    start_width = max((len(text) for text in early.values()), default=0)
    # A unit's label fits ROW_LABEL_WIDTH; a lower production's name may not.
    label_width = max(ROW_LABEL_WIDTH, *(len(row.name) + 1 for row in rows))
    lines = [" " * (label_width + start_width) + "".join(axis).rstrip()]

    last = WINDOW_HOURS - 1
    for row in rows:
        prefix = f"{row.name:<{label_width}}{early.get(row.name, ''):<{start_width}}"
        hold = f" {BUILD_HOLD_MARK}" if row.build_hold else ""
        if row.estimate is None:
            lines.append(f"{prefix}?{hold}")
            continue
        estimate = row.estimate
        cells = [BLANK] * WINDOW_HOURS
        first = max(0, column(estimate.started))
        eta_cell = max(0, min(column(estimate.eta), last))
        latest_cell = max(0, min(column(estimate.latest), last))
        for index in range(first, eta_cell + 1):
            cells[index] = style.run
        ranged = (estimate.earliest, estimate.latest) != (estimate.eta, estimate.eta)
        if ranged and column(estimate.earliest) <= last:
            earliest_cell = max(0, column(estimate.earliest))
            cells[earliest_cell] = style.earliest
            for index in range(earliest_cell + 1, eta_cell):
                cells[index] = style.before_eta
        # The ETA takes the earliest time's cell when the two share an hour,
        # and keeps its own cell when the latest time shares it.
        cells[eta_cell] = style.eta_ranged if ranged and latest_cell > eta_cell else style.eta
        if ranged:
            for index in range(eta_cell + 1, latest_cell):
                cells[index] = style.range_fill
            if latest_cell > eta_cell:
                cells[latest_cell] = style.latest
        arrow = "→" if column(estimate.latest) > last else ""
        span = f"{estimate.eta:%H:%M}"
        if ranged and style.show_range:
            span += f" ({estimate.earliest:%H:%M}–{estimate.latest:%H:%M})"
        lines.append(f"{prefix}{''.join(cells).rstrip()}{arrow} {span}{hold}")
    # The axis ends with the widest row, so its labels never widen the chart.
    indent = label_width + start_width
    reach = max((display_width(line) - indent for line in lines[1:]), default=0)
    lines[0] = " " * indent + "".join(axis[: -(-reach // 2)]).rstrip()
    drawn = with_plans(lines, rows)
    # A note goes on last, so it moves neither the axis nor the plan column.
    return [drawn[0], *(f"{line}  {row.note}" if row.note else line for line, row in zip(drawn[1:], rows, strict=True))]


def display_width(line: str) -> int:
    """Terminal columns: wide characters, the coloured cells among them, take two."""
    return sum(2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in line)


def with_plans(lines: list[str], rows: list[Row]) -> list[str]:
    """Each row's whole-plan progress in one right-aligned column past the longest row, with `100%` over its end line."""
    progress = [row.plan for row in rows if row.plan is not None]
    if not progress:
        return lines
    # Each number pads to its column's widest, so `Phase`, `of`, `-` and `%` line up.
    number_width = max(len(str(plan.number)) for plan in progress)
    total_width = max(len(str(plan.total)) for plan in progress)
    percent_width = max(len(str(plan.percent)) for plan in progress)
    plans = {
        row.name: f"Phase {row.plan.number:>{number_width}} of {row.plan.total:>{total_width}} - {row.plan.percent:>{percent_width}}%"
        for row in rows
        if row.plan is not None
    }
    left = max(display_width(line) for line in lines) + PLAN_GAP
    text_width = max(len(text) for text in plans.values())
    # `100%` ends over the end line, so the axis is no wider than a row.
    header = f"{lines[0]}{' ' * (left - display_width(lines[0]) + text_width + 2 + PLAN_BLOCKS - len(PLAN_FULL))}{PLAN_FULL}"
    drawn = [header]
    for line, row in zip(lines[1:], rows, strict=True):
        if row.plan is None:
            drawn.append(line)
            continue
        blocks = round(row.plan.percent / 10)
        bar = PLAN_FILL * blocks + " " * (PLAN_BLOCKS - blocks)
        drawn.append(f"{line}{' ' * (left - display_width(line))}{plans[row.name]:>{text_width}} {bar}{PLAN_END}")
    return drawn


def as_map(value: object, where: str) -> JsonMap:
    if not isinstance(value, dict):
        raise InputError(f"{where}: expected an object")
    fields: JsonMap = {}
    for key, item in cast(dict[object, object], value).items():
        if not isinstance(key, str):
            raise InputError(f"{where}: every key must be text")
        fields[key] = item
    return fields


def as_list(value: object, where: str) -> list[object]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise InputError(f"{where}: expected a list")
    return cast(list[object], value)


def check_keys(fields: JsonMap, allowed: set[str], where: str) -> None:
    unknown = sorted(set(fields) - allowed)
    if unknown:
        raise InputError(f"{where}: unknown field(s) {', '.join(unknown)}; allowed: {', '.join(sorted(allowed))}")


def optional_text(fields: JsonMap, key: str, where: str) -> str | None:
    value = fields.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise InputError(f"{where}.{key}: must be non-empty text when given")
    if "\n" in value:
        raise InputError(f"{where}.{key}: must be one line")
    return value.strip()


def text(fields: JsonMap, key: str, where: str) -> str:
    value = optional_text(fields, key, where)
    if value is None:
        raise InputError(f"{where}.{key}: required")
    return value


def parse_upcoming_work(fields: JsonMap, where: str) -> UpcomingWork | NoUpcomingWork:
    value = fields.get("then")
    if value is None:
        return NoUpcomingWork()
    if isinstance(value, str):
        raise InputError(f'{where}.then: must be a list of one-line items, one per upcoming phase: ["Phase 3: …", "Phase 4: …"]')
    if not isinstance(value, list) or not value:
        raise InputError(f"{where}.then: expected a non-empty list of one-line text")
    items = tuple(text({"then": item}, "then", where) for item in cast(list[object], value))
    for index, item in enumerate(items):
        if re.search(r", then |; then| then Phase|\bPhase \d+:.*\bPhase \d+:", item, re.IGNORECASE):
            raise InputError(f"{where}.then[{index}]: one item names more than one phase; split it into list items: {item}")
    return UpcomingWork(items)


def flag(fields: JsonMap, key: str, where: str) -> bool:
    value = fields.get(key, False)
    if not isinstance(value, bool):
        raise InputError(f"{where}.{key}: must be true or false")
    return value


def clock_text(fields: JsonMap, key: str, where: str) -> str | None:
    value = optional_text(fields, key, where)
    if value is not None and not TIME.match(value):
        raise InputError(f"{where}.{key}: {value!r} is not HH:MM or HH:MM+N")
    return value


def whole(fields: JsonMap, key: str, where: str) -> int:
    value = fields.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise InputError(f"{where}.{key}: a whole number of 0 or more")
    return value


def zone_named(zone: str, where: str) -> ZoneInfo:
    try:
        return ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError):
        raise InputError(f"{where}: {zone!r} is not an IANA zone name") from None


def moment_in(fields: JsonMap, key: str, where: str, source: ZoneInfo, zone: ZoneInfo) -> datetime:
    """A `YYYY-MM-DDTHH:MM` local time in `source`, as the same moment's local time in `zone`."""
    value = text(fields, key, where)
    if not STARTED.match(value):
        raise InputError(f"{where}.{key}: {value!r} is not YYYY-MM-DDTHH:MM")
    return datetime.fromisoformat(value).replace(tzinfo=source).astimezone(zone).replace(tzinfo=None)


def parse_child_row(value: object, where: str, source: ZoneInfo, zone: ZoneInfo) -> Row:
    fields = as_map(value, where)
    times = ("started", "eta", "earliest", "latest")
    check_keys(fields, {"name", *times}, where)
    name = text(fields, "name", where)
    if all(fields.get(key) is None for key in times):
        return Row(name, None, False, None)
    started, eta, earliest, latest = (moment_in(fields, key, where, source, zone) for key in times)
    return Row(name, Estimate(started, eta, earliest, latest), False, None)


def style_moment(fields: JsonMap, key: str, where: str, zone: ZoneInfo) -> datetime:
    value = text(fields, key, where)
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        raise InputError(f"{where}.{key}: expected an ISO timestamp with a UTC offset") from None
    if moment.tzinfo is None:
        raise InputError(f"{where}.{key}: expected an ISO timestamp with a UTC offset")
    return moment.astimezone(zone)


def parse_style_summary(value: object, where: str, zone: ZoneInfo) -> StyleSummary | StyleSummaryUnavailable:
    fields = as_map(value, where)
    if "unavailable" in fields:
        check_keys(fields, {"unavailable"}, where)
        return StyleSummaryUnavailable(text(fields, "unavailable", where))
    check_keys(fields, {"rules_done", "rules_total", "unclassified_rules", "cells_current_pct", "stale",
                        "proposals", "waiting_to_merge_back", "oldest_waiting", "synced_at", "synced_tip"}, where)
    percent = fields.get("cells_current_pct")
    if isinstance(percent, bool) or not isinstance(percent, (int, float)) or not 0 <= percent <= 100:
        raise InputError(f"{where}.cells_current_pct: a number from 0 to 100")
    count = whole(fields, "waiting_to_merge_back", where)
    if "oldest_waiting" not in fields:
        raise InputError(f"{where}.oldest_waiting: required")
    waiting: PendingHandoffs | NoPendingHandoffs
    if fields["oldest_waiting"] is None:
        if count:
            raise InputError(f"{where}.oldest_waiting: required when hand-offs are waiting")
        waiting = NoPendingHandoffs()
    else:
        if not count:
            raise InputError(f"{where}.waiting_to_merge_back: must be above 0 when oldest_waiting is given")
        waiting = PendingHandoffs(count, style_moment(fields, "oldest_waiting", where, ZoneInfo("UTC")))
    return StyleSummary(whole(fields, "rules_done", where), whole(fields, "rules_total", where),
                        whole(fields, "unclassified_rules", where), float(percent), whole(fields, "stale", where),
                        whole(fields, "proposals", where), waiting,
                        style_moment(fields, "synced_at", where, zone).replace(tzinfo=None),
                        text(fields, "synced_tip", where))


def parse_child_report(value: object, where: str, zone: ZoneInfo) -> ChildReport:
    """A lower production's saved report, its times moved from its own zone into `zone`."""
    fields = as_map(value, where)
    check_keys(fields, {"written", "zone", "interval", "units", "held", "rows", "style"}, where)
    source = zone_named(text(fields, "zone", where), f"{where}.zone")
    interval = fields.get("interval")
    if interval is not None and (not isinstance(interval, int) or isinstance(interval, bool) or interval <= 0):
        raise InputError(f"{where}.interval: minutes as a whole number above 0, or null when it reports on demand")
    cadence = ReportsOnDemand() if interval is None else ReportsEvery(interval)
    style = parse_style_summary(fields["style"], f"{where}.style", zone) if "style" in fields else NoStyleSummary()
    rows = [parse_child_row(item, f"{where}.rows[{index}]", source, zone)
            for index, item in enumerate(as_list(fields.get("rows"), f"{where}.rows"))]
    return ChildReport(moment_in(fields, "written", where, source, zone), cadence,
                       whole(fields, "units", where), whole(fields, "held", where), rows, style)


def parse_child(value: object, where: str, zone: ZoneInfo) -> Child:
    fields = as_map(value, where)
    check_keys(fields, {"name", "report", "open", "unreadable", "expand"}, where)
    saved = fields.get("report")
    report = NoChildReport() if saved is None else parse_child_report(saved, f"{where}.report", zone)
    unreadable = [text({"unreadable": item}, "unreadable", where)
                  for item in as_list(fields.get("unreadable"), f"{where}.unreadable")]
    return Child(text(fields, "name", where), report, whole(fields, "open", where), unreadable,
                 flag(fields, "expand", where))


def parse_eta(value: object, where: str) -> Eta:
    fields = as_map(value, where)
    check_keys(fields, {"time", "earliest", "latest", "none", "detail", "percent", "why", "first", "fixes", "stated"}, where)
    time = clock_text(fields, "time", where)
    earliest = clock_text(fields, "earliest", where)
    latest = clock_text(fields, "latest", where)
    none = optional_text(fields, "none", where)
    if (time is None) == (none is None):
        raise InputError(f"{where}: give exactly one of time or none")
    if none is not None and none not in NONE:
        raise InputError(f"{where}.none: must be one of {', '.join(repr(choice) for choice in NONE)}")
    if (earliest is None) != (latest is None):
        raise InputError(f"{where}: give earliest and latest together, or neither")
    if time is None and earliest is not None:
        raise InputError(f"{where}: a range needs a time")
    percent = fields.get("percent")
    if time is not None and "percent" not in fields:
        raise InputError(f"{where}.percent: required with a time; the unit director's phase percent done (0-100), or null when it stated none")
    if time is None and percent is not None:
        raise InputError(f"{where}.percent: only with a time")
    if percent is not None and (not isinstance(percent, int) or isinstance(percent, bool) or not 0 <= percent <= 100):
        raise InputError(f"{where}.percent: a whole number from 0 to 100, or null")
    why = optional_text(fields, "why", where)
    if why is not None and time is None:
        raise InputError(f"{where}.why: only with a time")
    detail = optional_text(fields, "detail", where)
    if detail is not None and detail.lower().startswith("from"):
        raise InputError(f"{where}.detail: never say where the ETA came from; say what the time covers, or leave it out")
    first_text = optional_text(fields, "first", where)
    try:
        first = datetime.fromisoformat(first_text) if first_text is not None else None
    except ValueError:
        raise InputError(f"{where}.first: the phase's first ETA as YYYY-MM-DDTHH:MM") from None
    fixes = fields.get("fixes", 0)
    if not isinstance(fixes, int) or isinstance(fixes, bool) or fixes < 0:
        raise InputError(f"{where}.fixes: the fix rounds added since the first ETA, a whole number from 0")
    if time is None and (first is not None or fixes):
        raise InputError(f"{where}: first and fixes only with a time")
    stated_text = optional_text(fields, "stated", where)
    try:
        stated = datetime.fromisoformat(stated_text) if stated_text is not None else None
    except ValueError:
        raise InputError(f"{where}.stated: when the ETA was stated, as YYYY-MM-DDTHH:MM") from None
    if time is None and stated is not None:
        raise InputError(f"{where}.stated: only with a time")
    if none is not None:
        return NoEta(none, detail)
    if time is None:
        raise InputError(f"{where}: give exactly one of time or none")
    spread = EtaRange(earliest, latest) if earliest is not None and latest is not None else None
    return TimedEta(time, spread, percent, why, detail, first, fixes, stated)


def measure(fields: JsonMap, key: str, where: str) -> float:
    value = fields.get(key)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise InputError(f"{where}.{key}: a measured number")
    return float(value)


def parse_goal(value: object, where: str) -> Goal | None:
    if value is None:
        return None
    fields = as_map(value, where)
    check_keys(fields, {"target", "unit", "start", "now", "aim"}, where)
    target = text(fields, "target", where)
    check_words(target, "target", where)
    start, aim = measure(fields, "start", where), measure(fields, "aim", where)
    if start == aim:
        raise InputError(f"{where}: start and aim are the same; the goal has nothing to achieve")
    return Goal(target, text(fields, "unit", where), start, measure(fields, "now", where), aim)


def goal_text(goal: Goal) -> str:
    achieved = round((goal.start - goal.now) / (goal.start - goal.aim) * 100)
    return f"{goal.target} - {achieved}% of {goal.aim:g} {goal.unit} target achieved"


def check_update(update: str, length: str, where: str, key: str = "update") -> None:
    limit = LENGTHS[length]
    if limit is not None and len(update) > limit:
        raise InputError(f"{where}.{key}: {len(update)} characters; a {length} {key} is one short line, at most {limit}")


def phases_named(line: str) -> list[int]:
    """Every phase number `line` names."""
    named: list[int] = []
    for match in PHASE_MENTION.finditer(line):
        mention: str = match.group(1) or match.group(2)
        named.extend(int(digits.group(0)) for digits in re.finditer(r"\d+", mention))
    return named


def other_phases(line: str, number: int | None) -> list[int]:
    """Phase numbers `line` names other than the heading's (`None` on a follow-up)."""
    return [found for found in phases_named(line) if found != number]


def check_then_order(then: str, number: int, where: str) -> None:
    """Plan phases run in number order, so what comes next never has a lower number."""
    label = THEN_PHASE_LABEL.match(then)
    named: list[int] = [] if OTHER_PLAN.search(then) else phases_named(then)
    if label is not None:
        named.extend(int(digits.group(0)) for digits in re.finditer(r"\d+", label.group(1)))
    earlier = [found for found in named if found <= number]
    if earlier:
        raise InputError(
            f"{where}.then: names Phase {', '.join(str(found) for found in earlier)} after this Phase {number}, which reads as impossible. "
            + "Renumber the plan so its numbers follow the run order (packaging: the showrunner's call), then report the new numbers."
        )


def check_words(line: str, key: str, where: str) -> None:
    """A line says what changes in the app, in words you know, without the production's plumbing."""
    check_plumbing(line, f"{where}.{key}")


def check_plumbing(line: str, name: str) -> None:
    problem = plumbing(line)
    if problem is not None:
        raise InputError(f"{name}: {problem}")


def plumbing(line: str) -> str | None:
    """Why the line fails the plumbing check, or None when it passes."""
    found = PLUMBING.search(line)
    if found is None:
        return None
    return (f"{found.group(0)!r} is the production's own plumbing or shorthand, which the user never sees; "
            + "say what changes in the app, or leave it out")


def read_ranking(fields: JsonMap, where: str) -> Ranking:
    """An item's `urgency`, `due`, `marked` and `follows`; none of them is a normal item."""
    follows = fields.get("follows", "")
    if not isinstance(follows, str) or "\n" in follows:
        raise InputError(f"{where}.follows: expected one line of text")
    try:
        return Ranking(urgency.from_fields(fields), follows.strip())
    except UrgencyError as error:
        raise InputError(f"{where}: {error}") from None


def read_outstanding(path: Path | None) -> list[Outstanding]:
    """What waits on the user, oldest first; no file means nothing waits."""
    if path is None or not path.exists():
        return []
    entries = cast(object, json.loads(path.read_text()))
    if not isinstance(entries, list):
        raise InputError(f"{path}: expected a JSON list")
    items: list[Outstanding] = []
    for index, entry in enumerate(cast(list[object], entries)):
        where = f"{path}[{index}]"
        if not isinstance(entry, dict):
            raise InputError(f"{where}: expected an object")
        fields = cast(dict[str, object], entry)
        check_keys(fields, {"since", "text", "after", "question", "reviewed", "review_unavailable", "urgency", "due",
                            "marked", "follows", "asked_by"}, where)
        rank = read_ranking(fields, where)
        since, text, after = fields.get("since"), fields.get("text"), fields.get("after")
        if not isinstance(since, str) or not STARTED.match(since):
            raise InputError(f"{where}.since: expected YYYY-MM-DDTHH:MM")
        if after is not None and (not isinstance(after, str) or not STARTED.match(after)):
            raise InputError(f"{where}.after: expected YYYY-MM-DDTHH:MM")
        if not isinstance(text, str) or not text.strip() or "\n" in text:
            raise InputError(f"{where}.text: expected one non-empty line")
        # The plumbing check runs when `update_registration.py questions` writes an item, not here:
        # a stored item that fails it must not take the whole list down.
        timing = DeferredUntil(datetime.fromisoformat(after)) if isinstance(after, str) else DueNow()
        since_at = datetime.fromisoformat(since)
        question = fields.get("question")
        reviewed, unavailable = fields.get("reviewed"), fields.get("review_unavailable")
        asked_by = fields.get("asked_by", "")
        if not isinstance(asked_by, str) or "\n" in asked_by or ("asked_by" in fields and not asked_by.strip()):
            raise InputError(f"{where}.asked_by: expected one non-empty line")
        if "question" not in fields:
            if "reviewed" in fields or "review_unavailable" in fields or "asked_by" in fields:
                raise InputError(f"{where}: question required for a review result or an asker")
            items.append(OutstandingReminder(since_at, text, timing, rank))
            continue
        if not isinstance(question, str) or not question.strip():
            raise InputError(f"{where}.question: expected non-empty full question")
        if "reviewed" in fields:
            if not isinstance(reviewed, str) or not reviewed.strip() or "review_unavailable" in fields:
                raise InputError(f"{where}.reviewed: expected completed review without an unavailable result")
            items.append(ReviewedOutstandingQuestion(since_at, text, question, reviewed, timing, rank, asked_by))
        elif "review_unavailable" in fields:
            if not isinstance(unavailable, str):
                raise InputError(f"{where}.review_unavailable: expected reason text")
            items.append(ReviewUnavailableOutstandingQuestion(since_at, text, question, unavailable, timing, rank, asked_by))
        else:
            items.append(OutstandingQuestion(since_at, text, question, timing, rank, asked_by))
    return sorted(items, key=lambda item: item.since)


def due(items: list[Outstanding], now: datetime) -> list[Outstanding]:
    """The items the user has not deferred past `now`, a local time in the report's zone."""
    return [item for item in items if isinstance(item.timing, DueNow) or item.timing.at <= now]


def read_dailies_hold() -> HoldState:
    """Read holders and check the purpose that appears in reports and footers."""
    hold = read_holders(holder_directory())
    if isinstance(hold, ActiveHolders):
        for holder in hold.holders:
            try:
                check_plumbing(holder.purpose, f"build hold {holder.name!r} purpose")
            except InputError as error:
                raise InputError(f"{error}; have {holder.name} run /build:hold hold again with other words") from None
    return hold


def read_mac_block() -> MacBlockFooterState:
    """Read the Mac block without locks or external calls."""
    path = state_paths().block
    try:
        block = read_block(path)
    except (ValueError, OSError):
        return UnreadableMacBlock(path)
    if isinstance(block, (PendingMacBlock, ActiveMacBlock)):
        try:
            check_plumbing(block.reason, f"Mac block {block.holder!r} reason")
        except InputError as error:
            raise InputError(
                f"{error}; have {block.holder} run /build:mac block again with other words"
            ) from None
    return block


def check_one_phase(line: str, number: int | None, key: str, where: str) -> None:
    """A line may name another phase only when it says why that phase is here."""
    others = other_phases(line, number)
    if others and "because" not in line:
        raise InputError(
            f"{where}.{key}: names Phase {', '.join(str(found) for found in others)} outside the heading's phase; "
            + "name only the heading's phase, or say why the other is here ('because ...'). Later work goes in `then`."
        )


def check_counts(held: str, update: str, where: str) -> None:
    """A count in the held reason is what the update reports progress against."""
    for count in COUNT.finditer(held):
        total: str = count.group(1)
        if not re.search(rf"\b\d+ of {total}\b", update):
            raise InputError(
                f"{where}.update: the held reason counts {total}; say how many of them are done, as '<k> of {total} ...'"
            )


def parse_idle(fields: JsonMap, where: str) -> IdleWait | NotIdle:
    value = fields.get("idle")
    if value is None:
        return NotIdle()
    idle = as_map(value, f"{where}.idle")
    check_keys(idle, {"waits_for", "until"}, f"{where}.idle")
    until_value = idle.get("until")
    if not isinstance(until_value, str) or not STARTED.match(until_value):
        raise InputError(
            f"{where}.idle.until: {until_value!r} must be when the unit comes back, as YYYY-MM-DDTHH:MM in the zone"
        )
    try:
        until = datetime.fromisoformat(until_value)
    except ValueError:
        raise InputError(
            f"{where}.idle.until: {until_value!r} must be when the unit comes back, as YYYY-MM-DDTHH:MM in the zone"
        ) from None
    waits_for = text(idle, "waits_for", f"{where}.idle")
    if len(waits_for) > IDLE_LIMIT:
        raise InputError(
            f"{where}.idle.waits_for: {len(waits_for)} characters; at most {IDLE_LIMIT}, one short line"
        )
    check_words(waits_for, "idle.waits_for", where)
    return IdleWait(waits_for, until)


def parse_unit(value: object, where: str, length: str) -> Unit:
    fields = as_map(value, where)
    check_keys(
        fields,
        {"unit", "name", "label", "project", "goal", "phase", "started", "held", "held_examples", "build_hold", "holds_builds", "update", "eta", "waiting_on_it", "needed", "needs_user", "then", "idle"},
        where,
    )
    if "held" not in fields:
        raise InputError(f"{where}.held: required; the reason the phase's checkpoint is not merged, or null when no checkpoint waits")
    unit = text(fields, "unit", where)
    label = optional_text(fields, "label", where) or unit.removesuffix("-unit")
    if len(label) > LABEL_LIMIT:
        raise InputError(f"{where}.label: {label!r} is longer than {LABEL_LIMIT}; give a short label")
    phase = text(fields, "phase", where)
    started_text = text(fields, "started", where)
    if not STARTED.match(started_text):
        raise InputError(f"{where}.started: {started_text!r} must be the phase's start as YYYY-MM-DDTHH:MM in the zone")
    match = PHASE.match(phase)
    if match is None:
        raise InputError(
            f"{where}.phase: {phase!r} must read 'Phase <N> of <M>: <what it changes>' or 'follow-up <K> of <Q>: <what it changes>'"
        )
    upcoming_work = parse_upcoming_work(fields, where)
    plan_number, plan_total, follow_number, follow_total = match.groups()
    number = int(plan_number or follow_number)
    total = int(plan_total or follow_total)
    if number > total:
        raise InputError(f"{where}.phase: {number} of {total}")
    if follow_number is not None and (
        isinstance(upcoming_work, NoUpcomingWork) or not any(RETURN.search(item) for item in upcoming_work.items)
    ):
        raise InputError(f"{where}.then: a follow-up names the plan phase it returns to ('the plan at Phase <N>'), or says 'plan done' after reading the plan")
    if isinstance(upcoming_work, NoUpcomingWork) and number == total:
        raise InputError(f"{where}.then: required on a last phase; name the queued work, or 'nothing queued'")
    heading_number = int(plan_number) if plan_number else None
    if isinstance(upcoming_work, UpcomingWork):
        for item in upcoming_work.items:
            if heading_number is not None:
                check_then_order(item, heading_number, where)
            check_words(item, "then", where)
    for key in ("phase", "held", "held_examples", "update", "waiting_on_it", "needed"):
        line = optional_text(fields, key, where)
        if line is not None:
            check_words(line, key, where)
    held = optional_text(fields, "held", where)
    held_examples = optional_text(fields, "held_examples", where)
    if held is None and held_examples is not None:
        raise InputError(f"{where}.held_examples: only with a held reason")
    update = text(fields, "update", where)
    check_update(update, length, where)
    check_one_phase(update, heading_number, "update", where)
    if held is not None:
        check_update(held, length, where, "held")
        check_one_phase(held, heading_number, "held", where)
        if EXAMPLES.search(held):
            raise InputError(f"{where}.held: give the reason alone; examples go in held_examples, shown only the first time")
        check_counts(held, update, where)
    if held_examples is not None:
        check_update(held_examples, length, where, "held_examples")
    build_hold = fields.get("build_hold", False)
    if not isinstance(build_hold, bool):
        raise InputError(f"{where}.build_hold: true while the unit is held, else left out; hold details come from the holder files")
    holds_builds = fields.get("holds_builds", False)
    if not isinstance(holds_builds, bool):
        raise InputError(f"{where}.holds_builds: true while the unit's own session holds the builds, else left out")
    return Unit(
        unit=unit,
        name=optional_text(fields, "name", where) or unit,
        label=label,
        project=text(fields, "project", where),
        goal=parse_goal(fields.get("goal"), f"{where}.goal"),
        phase=phase,
        started=datetime.fromisoformat(started_text),
        held=held,
        held_examples=held_examples,
        build_hold=build_hold,
        holds_builds=holds_builds,
        update=update,
        eta=parse_eta(fields.get("eta"), f"{where}.eta"),
        waiting_on_it=optional_text(fields, "waiting_on_it", where),
        needed=optional_text(fields, "needed", where),
        needs_user=flag(fields, "needs_user", where),
        upcoming_work=upcoming_work,
        idle=parse_idle(fields, where),
    )


def parse_topic(value: object, where: str, length: str) -> Topic:
    fields = as_map(value, where)
    check_keys(fields, {"title", "update", "eta", "needed", "needs_user"}, where)
    update = text(fields, "update", where)
    check_update(update, length, where)
    return Topic(
        title=text(fields, "title", where),
        update=update,
        eta=text(fields, "eta", where),
        needed=optional_text(fields, "needed", where),
        needs_user=flag(fields, "needs_user", where),
    )


def read_chart(path: Path) -> str:
    """The `chart=` line of the conf; `default` when the file or the line is missing."""
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return "default"
    for line in lines:
        key, _, value = line.partition("=")
        if key.strip() == CHART_KEY:
            chart = value.strip()
            if chart not in CHART_STYLES:
                raise InputError(f"{path}: chart={chart!r} must be one of {', '.join(CHART_STYLES)}; set it with --chart")
            return chart
    return "default"


def write_chart(path: Path, chart: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path, f"{CHART_KEY}={chart}\n")


def parse_report(value: object, chart: str) -> Report:
    fields = as_map(value, "input")
    if "build_hold" in fields:
        raise InputError("input.build_hold: remove this field; holds are read from the holder files")
    check_keys(fields, {"length", "zone", "next_run", "units", "topics", "counts", "children", "reports_to", "doc"}, "input")
    length = text(fields, "length", "input")
    if length not in LENGTHS:
        raise InputError(f"input.length: must be one of {', '.join(LENGTHS)}")
    zone = text(fields, "zone", "input")
    next_run = clock_text(fields, "next_run", "input")
    units = [parse_unit(item, f"units[{index}]", length) for index, item in enumerate(as_list(fields.get("units"), "input.units"))]
    topics = [parse_topic(item, f"topics[{index}]", length) for index, item in enumerate(as_list(fields.get("topics"), "input.topics"))]
    hold = read_dailies_hold()
    mac_block = read_mac_block()
    marked = any(unit.build_hold for unit in units)
    if marked and isinstance(hold, NoHolders):
        raise InputError("units.build_hold: a unit is marked but no holder file exists; remove the stale unit marker")
    if isinstance(hold, ActiveHolders):
        holder_names = {holder.name for holder in hold.holders}
        holding = {unit.unit for unit in units if unit.holds_builds or unit.unit in holder_names}
        # A holder's own unit is never marked, so a marker is owed only where some unit holds nothing.
        if not marked and any(unit.unit not in holding for unit in units):
            raise InputError("units.build_hold: holder files are active but no unit is marked; mark the held unit")
        for unit in units:
            if unit.build_hold and unit.unit in holding:
                raise InputError(f"units.build_hold: {unit.unit} holds the build hold itself; remove its marker")
    counts = [parse_count(item, f"counts[{index}]") for index, item in enumerate(as_list(fields.get("counts"), "input.counts"))]
    supplied_children = as_list(fields.get("children"), "input.children")
    children = ([parse_child(item, f"children[{index}]", zone_named(zone, "input.zone"))
                 for index, item in enumerate(supplied_children)] if supplied_children else [])
    labels = {unit.label for unit in units}
    for child in children:
        if child.name in labels:
            raise InputError(f"children: {child.name} is also a unit's label; give that unit another label")
    doc = optional_text(fields, "doc", "input")
    return Report(length, chart, zone, next_run, hold, mac_block, units, topics, counts, children,
                  optional_text(fields, "reports_to", "input") or "", Path(doc) if doc else None)


def parse_count(value: object, where: str) -> ProductionCount:
    fields = as_map(value, where)
    check_keys(fields, {"label", "count", "not_found"}, where)
    label = text(fields, "label", where)
    number = fields.get("count")
    if fields.get("not_found") is True and number is None:
        return ProductionCount(label, None)
    if not isinstance(number, int) or isinstance(number, bool) or number < 0:
        raise InputError(f"{where}.count: a whole number of 0 or more, or `not_found: true`")
    return ProductionCount(label, number)


def counts_line(counts: list[ProductionCount]) -> str:
    return "counts: " + " · ".join(
        f"{count.label} {'not found' if count.number is None else count.number}" for count in counts)


def load_state(path: Path | None) -> dict[str, LastUnitReport]:
    if path is None or not path.exists():
        return {}
    fields = as_map(cast(object, json.loads(path.read_text())), str(path))
    previous: dict[str, LastUnitReport] = {}
    for unit, entry in fields.items():
        entry_fields = as_map(entry, f"{path}:{unit}")
        phase = text(entry_fields, "phase", f"{path}:{unit}")
        eta_moment = optional_text(entry_fields, "eta", f"{path}:{unit}")
        eta_text = optional_text(entry_fields, "eta_text", f"{path}:{unit}")
        held = optional_text(entry_fields, "held", f"{path}:{unit}")
        first = optional_text(entry_fields, "first", f"{path}:{unit}")
        if eta_text is not None and eta_moment is None:
            raise InputError(f"{path}:{unit}.eta: required with eta_text")
        eta = (LastReportedEta(eta_text, datetime.fromisoformat(eta_moment))
               if eta_text is not None and eta_moment is not None else NoLastReportedEta())
        previous[unit] = LastUnitReport(
            phase, eta, held, datetime.fromisoformat(first) if first else None)
    return previous


def previous_report(previous: dict[str, LastUnitReport], unit: str) -> Previous:
    """The unit's last report, named explicitly when no saved report exists."""
    report = previous.get(unit)
    return report if report is not None else NoLastUnitReport()


def same_phase(previous: str, current: str) -> bool:
    """One phase across reports has the same title, even after a renumber; a saved phase with no title matches none."""
    return previous.partition(": ")[2] == current.partition(": ")[2]


def resolve_eta_moment(unit: Unit, previous: Previous, now: datetime) -> ResolvedEta:
    """Resolve this report's ETA once: from the moment it was stated, else preserving an unchanged same-phase moment."""
    eta = unit.eta
    if isinstance(eta, NoEta):
        return NoResolvedEtaMoment()
    # A time that names no day is read on the day it was stated. That comes before the last report's
    # moment: the same clock stated again a day later is a new time. `+N` counts days from the report,
    # as the builder writes it.
    if eta.stated is not None and "+" not in eta.time:
        stated = eta.stated if eta.stated.tzinfo is not None else eta.stated.replace(tzinfo=now.tzinfo)
        return ResolvedEtaMoment(parse_time(eta.time, stated))
    if (isinstance(previous, LastUnitReport)
            and same_phase(previous.phase, unit.phase)
            and isinstance(previous.eta, LastReportedEta)
            and previous.eta.text == eta.time):
        return ResolvedEtaMoment(previous.eta.moment)
    return ResolvedEtaMoment(parse_time(eta.time, now))


def resolve_eta_moments(report: Report, previous: dict[str, LastUnitReport],
                        now: datetime) -> dict[str, ResolvedEta]:
    """Resolve every unit's ETA exactly once for all report consumers."""
    return {unit.unit: resolve_eta_moment(unit, previous_report(previous, unit.unit), now)
            for unit in report.units}


def first_eta(unit: Unit, previous: Previous, resolved: ResolvedEta) -> datetime | None:
    """The phase's first stated ETA: the input's `first`, else the state's for the same phase, else this report's."""
    if isinstance(unit.eta, TimedEta) and unit.eta.first is not None:
        return unit.eta.first
    if (isinstance(previous, LastUnitReport) and same_phase(previous.phase, unit.phase)
            and previous.first is not None):
        return previous.first
    return resolved.moment if isinstance(resolved, ResolvedEtaMoment) else None


def drift_text(unit: Unit, previous: Previous, resolved: ResolvedEta,
               now: datetime, zone_name: str) -> str | None:
    """The first ETA, how far the current one has moved from it and the fix rounds added since; `None` until it moves."""
    first = first_eta(unit, previous, resolved)
    eta = unit.eta
    if first is None or not isinstance(resolved, ResolvedEtaMoment) or isinstance(eta, NoEta):
        return None
    minutes = round((resolved.moment - first).total_seconds() / 60)
    if minutes == 0 and eta.fixes == 0:
        return None
    hours, rest = divmod(abs(minutes), 60)
    rounds = f", {eta.fixes} fix round{'' if eta.fixes == 1 else 's'} added" if eta.fixes else ""
    return f"{clock(first, now, zone_name)} (now {'-' if minutes < 0 else '+'}{hours}:{rest:02d}{rounds})"


def save_state(path: Path, report: Report, resolved: dict[str, ResolvedEta],
               previous: dict[str, LastUnitReport]) -> None:
    state: dict[str, dict[str, str | None]] = {}
    for unit in report.units:
        eta = resolved[unit.unit]
        moment = eta.moment if isinstance(eta, ResolvedEtaMoment) else None
        first = first_eta(unit, previous_report(previous, unit.unit), eta)
        state[unit.unit] = {
            "phase": unit.phase,
            "eta": moment.isoformat() if moment else None,
            "eta_text": unit.eta.time if isinstance(unit.eta, TimedEta) and moment else None,
            "held": unit.held,
            "first": first.isoformat() if first else None,
        }
    write_text_atomic(path, json.dumps(state, indent=2) + "\n")


def clock(moment: datetime, now: datetime, zone_name: str) -> str:
    days = (moment.date() - now.date()).days
    base = f"{moment:%H:%M} {zone_name}"
    if days == 0:
        return base
    if days == 1:
        return f"{base} tomorrow"
    return f"{moment:%a} {base}"


def change_minutes(moment: datetime, previous: Previous, phase: str) -> int | None:
    """Minutes the ETA moved since the last report of the same phase; `None` on a first ETA or a new phase."""
    if (not isinstance(previous, LastUnitReport) or not same_phase(previous.phase, phase)
            or not isinstance(previous.eta, LastReportedEta)):
        return None
    return round((moment - previous.eta.moment).total_seconds() / 60)


def change_note(moment: datetime, previous: Previous, phase: str,
                now: datetime, why: str | None) -> str | None:
    minutes = change_minutes(moment, previous, phase)
    if minutes is None:
        # No earlier report to compare with: a time that has passed is still said to have passed.
        return "overdue" if moment < now else None
    if minutes == 0:
        return "unchanged, overdue" if moment < now else "unchanged"
    hours, rest = divmod(abs(minutes), 60)
    note = f"changed: {'+' if minutes > 0 else '-'}{hours}:{rest:02d}"
    return f"{note} because {why}" if why else note


def check_changes(report: Report, previous: dict[str, LastUnitReport],
                  resolved: dict[str, ResolvedEta]) -> None:
    """An ETA that moved CHANGE_NEEDS_WHY_MINUTES or more since the last report says why."""
    for index, unit in enumerate(report.units):
        if isinstance(unit.eta, NoEta) or unit.eta.why is not None:
            continue
        eta = resolved[unit.unit]
        if not isinstance(eta, ResolvedEtaMoment):
            continue
        minutes = change_minutes(eta.moment, previous_report(previous, unit.unit), unit.phase)
        if minutes is not None and abs(minutes) >= CHANGE_NEEDS_WHY_MINUTES:
            raise InputError(
                f"units[{index}].eta.why: the ETA moved {minutes:+d} minutes since the last report; "
                + "say why in a few words (rendered after 'because'), from the unit director's own reports"
            )


def check_idle(report: Report, now: datetime) -> None:
    for index, unit in enumerate(report.units):
        if isinstance(unit.idle, IdleWait) and unit.idle.until <= now:
            raise InputError(
                f"units[{index}].idle.until: that time has passed; remove idle now the unit is back at work, or give the new time"
            )


class StateClear(NamedTuple):
    pass


class StateRefused(NamedTuple):
    field: str
    why: str


def check_render_state(value: object, state_path: Path, at: str | None = None) -> StateClear | StateRefused:
    """Check a candidate against renderer state before its builder changes the clock."""
    try:
        report = parse_report(value, "default")
        previous = load_state(state_path)
        now, _ = local_now(report.zone, "input.zone", at)
        resolved = resolve_eta_moments(report, previous, now)
        check_changes(report, previous, resolved)
        check_idle(report, now)
    except (InputError, OSError, ValueError, json.JSONDecodeError) as error:
        detail = str(error)
        field, separator, why = detail.partition(": ")
        return StateRefused(field if separator else "input", why if separator else detail)
    return StateClear()


def range_clock(moment: datetime, now: datetime) -> str:
    """A range end: the bare time today, the weekday before it on any other day."""
    return f"{moment:%H:%M}" if moment.date() == now.date() else f"{moment:%a %H:%M}"


def idle_clock(moment: datetime, now: datetime) -> str:
    days = (moment.date() - now.date()).days
    if days == 0:
        return f"{moment:%H:%M}"
    if days < 7:
        return f"{moment:%a %H:%M}"
    return f"{moment:%a %Y-%m-%d %H:%M}"


def release_text(release: KnownReleaseEta, now: datetime, zone: ZoneInfo) -> str:
    """A release time in the report zone, with weekday and minutes until it."""
    local = release.at.astimezone(zone)
    moment = local.replace(tzinfo=None)
    minutes = round((release.at.timestamp() - now.replace(tzinfo=zone).timestamp()) / 60)
    count = f"{abs(minutes)} minute{'' if abs(minutes) == 1 else 's'}"
    when = f"{count} overdue" if minutes < 0 else f"in {count}" if minutes else "now"
    return f"{range_clock(moment, now)} {local:%Z} ({when})"


def release_progress(cycle: HoldCycle, zone: ZoneInfo) -> str:
    """When the hold ended; the memory gate now orders the builds."""
    started = datetime.fromisoformat(cycle["release_started_at"]).astimezone(zone)
    return f"released {started:%H:%M} {started:%Z}; the memory gate orders builds"


def hold_line(holder: Holder, now: datetime, zone: ZoneInfo, cycle: HoldCycle | None = None) -> str:
    """A holder's line, in a report's footer: who holds builds, why, since when and until when."""
    since = holder.since.astimezone(zone)
    head = f"{BUILD_HOLD_MARK}: {holder.name} holds builds since {since:%H:%M} {since:%Z} for {holder.purpose}"
    if cycle is not None and cycle["release_started_at"] and holder.key in cycle["holders"]:
        return f"{head}; {release_progress(cycle, zone)}"
    if isinstance(holder.release, KnownReleaseEta):
        return f"{head}; expected to release {release_text(holder.release, now, zone)}"
    return f"{head}; no release time given"


def released_session(entry: ReleaseEntry) -> str:
    """A released session by its name, never by its id."""
    name = entry["name"]
    return name if name and name != entry["session_id"] else "an unnamed session"


def hold_action_lines(cycle: HoldCycle) -> list[str]:
    """One line for each session a release could not bring back, so that someone looks at it."""
    lines: list[str] = []
    for entry in cycle["entries"]:
        if entry["delivery"] in {"DeliveryFailed", "RecipientGone", "QueuedUnconfirmed"}:
            lines.append(f"{released_session(entry)} could not be told builds are released; tell it")
    return lines


def mac_block_line(
    block: PendingMacBlock | ActiveMacBlock | UnreadableMacBlock,
    zone: ZoneInfo,
) -> str:
    """A Mac block line in a report's footer."""
    if isinstance(block, UnreadableMacBlock):
        return f"Mac block: its state file cannot be read ({block.path})"
    since = block.since.astimezone(zone)
    expires = block.expires.astimezone(zone)
    label = "Mac block pending" if isinstance(block, PendingMacBlock) else "Mac block"
    line = (
        f"{label}: {block.holder} since {since:%H:%M %Z}, for {block.reason} "
        + f"- lifts {expires:%a %H:%M %Z}"
    )
    if ci_may_still_be_on(block.ci):
        line += " - CI can still use the Mac"
    return line


def machine_local(value: datetime) -> datetime:
    """Interpret an offset-free wall time in the machine's time zone."""
    return value.astimezone()


def agent_time(value: str | None) -> datetime | None:
    if not value or value == "null":
        return None
    try:
        return machine_local(datetime.fromisoformat(value))
    except ValueError:
        return None


def agent_number(value: str | None) -> float | None:
    if value is None or value == "null":
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def agent_refill(reset: datetime, now: datetime, zone: ZoneInfo) -> str:
    local = reset.astimezone(zone)
    return f"{local:%H:%M} today" if local.date() == now.date() else f"{local:%a %H:%M}"


def agent_resets(fields: dict[str, str], zone: ZoneInfo) -> str:
    raw = fields.get("limit_reset_count")
    try:
        count = int(raw) if raw is not None else None
    except ValueError:
        count = None
    if count is None:
        return "resets unknown"
    if count <= 0:
        return "no resets available"
    expiration = agent_time(fields.get("limit_reset"))
    until = f" until {expiration.astimezone(zone):%b %-d}" if expiration else ""
    return f"{count} reset{'' if count == 1 else 's'} available{until}"


def agent_line(name: str, fields: dict[str, str], readings: list[Reading], now: datetime, zone: ZoneInfo) -> str:
    """The weighted pace (run_out.py) is extended to 100% used; until an hour of readings gives one, the week's use since its refill stands in."""
    resets = agent_resets(fields, zone)
    reset = agent_time(fields.get("resets"))
    refill = agent_refill(reset, now, zone) if reset and reset.timestamp() > now.replace(tzinfo=zone).timestamp() else None
    remaining = agent_number(fields.get("weekly_remaining_usage"))
    if remaining is None or refill is None or reset is None:
        return f"- {name}: week's usage unknown; {'refills ' + refill if refill else 'refill time unknown'}; {resets}"
    week = _AgentWeekUsage(reset, agent_time(fields.get("weekly_usage_checked_at")) or now.replace(tzinfo=zone), 100 - remaining)
    if week.used_percent >= 100:
        return f"- {name}: {week.used_percent:g}%; ran out, back at its {refill} refill; {resets}"
    rate = run_out.weighted_rate(readings, week.checked_at.timestamp())
    if rate is None:
        last_refill = machine_local((week.reset_at - timedelta(days=7)).replace(tzinfo=None))
        first_allowed = max(last_refill.timestamp(), now.replace(tzinfo=zone).timestamp() - run_out.WINDOW.total_seconds())
        elapsed = week.checked_at.timestamp() - first_allowed
        rate = week.used_percent / elapsed if elapsed > 0 else 0
    if rate <= 0:
        pace = f"does not run out at this pace, so it hits its {refill} refill first"
    else:
        empty_seconds = week.checked_at.timestamp() + (100 - week.used_percent) / rate
        rounded_seconds = math.floor((empty_seconds + 30) / 60) * 60
        empty_at = datetime.fromtimestamp(rounded_seconds, zone)
        time_text = f"{empty_at:%H:%M %Z} today" if empty_at.date() == now.date() else f"{empty_at:%a %H:%M %Z}"
        if empty_at.timestamp() >= week.reset_at.timestamp():
            pace = f"runs out about {time_text}, so it hits its {refill} refill first"
        else:
            pace = f"runs out about {time_text}, before its {refill} refill"
    return f"- {name}: {week.used_percent:g}%; {pace}; {resets}"


def agent_section(now: datetime, zone: ZoneInfo, *, at: str | None = None) -> list[str]:
    readings = run_out.read_readings(READINGS_LOG)
    lines = ["### Agents"]
    stamped = datetime.fromisoformat(at) if at is not None else None
    if stamped is not None and stamped.tzinfo is None:
        stamped = stamped.replace(tzinfo=zone)
    minute_end = stamped.timestamp() + 60 if stamped is not None else None
    for note in note_file.read_notes():
        fields = {key: value for key in ("limit_reset_count", "limit_reset", "resets",
                                        "weekly_usage_checked_at", "weekly_remaining_usage")
                  if (value := note.get(key)) is not None}
        if note_file.in_use(note):
            account_readings = readings.get(note.label, [])
            if minute_end is not None:
                account_readings = [reading for reading in account_readings if reading.at.timestamp() < minute_end]
                checked_at = agent_time(fields.get("weekly_usage_checked_at"))
                if checked_at is not None and checked_at.timestamp() >= minute_end and account_readings:
                    latest = account_readings[-1]
                    fields["weekly_usage_checked_at"] = latest.at.isoformat()
                    fields["weekly_remaining_usage"] = str(100 - latest.used_percent)
            lines.append(agent_line(note.label, fields, account_readings, now, zone))
    if len(lines) == 1:
        lines.append("- none active")
    lines.append("")
    return lines


def footer(
    now: datetime, zone: ZoneInfo, zone_name: str, next_run: str | None,
    hold: HoldState, mac_block: MacBlockFooterState, outstanding: list[Outstanding], *,
    nothing_needed: bool, agent_lines: list[str], unreadable: Sequence[str] = (), root: str = "",
    counts: str | None = None
) -> list[str]:
    """The separated update footer that ends a dailies report.

    `outstanding` already holds the lower productions' items for a root. `unreadable` names each
    lower production whose list could not be read, and `root` the root a lower showrunner is under.
    `counts` is the counts line that follows the header, when anything is counted.
    """
    items: list[str] = []
    if isinstance(hold, ActiveHolders):
        try:
            cycle = read_cycle()
        except ReleaseRecordReadError as error:
            items = [hold_line(holder, now, zone) for holder in hold.holders]
            items.append(f"  {release_record_error_line(error)}")
        else:
            items = [hold_line(holder, now, zone, cycle) for holder in hold.holders]
            if cycle is not None and any(holder.key in cycle["holders"] for holder in hold.holders):
                items.extend(f"  {line}" for line in hold_action_lines(cycle))
    if not isinstance(mac_block, NoMacBlock):
        items.append(mac_block_line(mac_block, zone))
    items.extend(line.removeprefix("- ") for line in agent_lines if line and line != "### Agents")
    # By name, so the lists keep one order however the tree was walked.
    items.extend(f"{slug}: {UNREADABLE_LIST}" for slug in sorted(unreadable))
    if root:
        items.append(f"reports to {root}")
    # An item the user deferred stays hidden until its `after` time, in the report's zone.
    local_now = now.astimezone(zone).replace(tzinfo=None) if now.tzinfo else now
    outstanding = due(outstanding, local_now)
    schedule = f"next dailies: {range_clock(parse_time(next_run, now), now)} {zone_name}" if next_run else "no dailies scheduled"
    # A list that cannot be read may hold an item, so the footer never calls that quiet.
    quiet = nothing_needed and not outstanding and not unreadable
    items.append(f"{schedule}{' - nothing needed' if quiet else ''}")
    return ["", "---", f"{now:%H:%M} {zone_name} update:", *([counts] if counts else []), "",
            *(f"{item[:2]}* {item[2:]}" if item.startswith("  ") else f"* {item}" for item in items)]


def doc_counts(doc: Path | None) -> str | None:
    """The counts line for a production doc; the status line carries the question count."""
    import footer_counts

    return None if doc is None else footer_counts.counts_line(footer_counts.read_counts(doc))


def eta_text(unit: Unit, previous: Previous, resolved: ResolvedEta,
             now: datetime, zone_name: str, with_note: bool) -> str:
    eta = unit.eta
    if isinstance(eta, NoEta):
        words = eta.reason
    else:
        if not isinstance(resolved, ResolvedEtaMoment):
            raise InputError(f"{unit.unit}: timed ETA was not resolved")
        moment = resolved.moment
        notes: list[str] = []
        note = change_note(moment, previous, unit.phase, now, eta.why) if with_note else None
        if note:
            notes.append(note)
        if eta.spread is not None:
            earliest = range_clock(parse_range_end(eta.spread.earliest, now, moment, earliest=True), now)
            latest = range_clock(parse_range_end(eta.spread.latest, now, moment, earliest=False), now)
            notes.append(f"range {earliest}–{latest}")
        done = f"{eta.percent}% done" if eta.percent is not None else "percent done not stated"
        words = f"{clock(moment, now, zone_name)}, {done}" + (f" ({'; '.join(notes)})" if notes else "")
    return f"{words}; {eta.detail}" if with_note and eta.detail else words


def major_changes(unit: Unit, last: Previous, resolved: ResolvedEta, now: datetime, zone_name: str) -> list[str]:
    """What a gantt report says about a unit above the chart: one line for each thing that changed since its
    last report. A unit with no last report, or with nothing changed, has none."""
    if not isinstance(last, LastUnitReport):
        return []
    changes: list[str] = []
    if not same_phase(last.phase, unit.phase):
        changes.append(f"- {unit.name}: now on {unit.phase}")
    if unit.held != last.held:
        changes.append(f"- {unit.name}: checkpoint "
                       + (f"not merged, because {unit.held}" if unit.held else "no longer held"))
    if isinstance(resolved, ResolvedEtaMoment):
        minutes = change_minutes(resolved.moment, last, unit.phase)
        if minutes is not None and abs(minutes) >= MAJOR_ETA_MINUTES:
            changes.append(f"- {unit.name}: eta {eta_text(unit, last, resolved, now, zone_name, with_note=True)}")
    return changes


def ordered_units(report: Report, resolved: dict[str, ResolvedEta]) -> list[Unit]:
    def key(pair: tuple[int, Unit]) -> tuple[int, float, int]:
        index, unit = pair
        if unit.needs_user:
            return (0, 0.0, index)
        if isinstance(unit.eta, NoEta):
            return (2, 0.0, index)
        eta = resolved[unit.unit]
        if not isinstance(eta, ResolvedEtaMoment):
            raise InputError(f"{unit.unit}: timed ETA was not resolved")
        return (1, eta.moment.timestamp(), index)

    return [unit for _, unit in sorted(enumerate(report.units), key=key)]


def grouped_wait(length: str, unit: Unit) -> IdleWait | NotIdle:
    if (
        length == "simple"
        and isinstance(unit.idle, IdleWait)
        and not unit.needs_user
        and unit.needed is None
        and unit.held is None
    ):
        return unit.idle
    return NotIdle()


def plan_progress(unit: Unit) -> PlanProgress | None:
    """The whole plan's percent done: earlier phases whole, this one at its stated percent (none stated counts as 0); a follow-up has none."""
    match = PHASE.match(unit.phase)
    if match is None or match.group(1) is None:
        return None
    number, total = int(match.group(1)), int(match.group(2))
    percent = unit.eta.percent if isinstance(unit.eta, TimedEta) else None
    done = (number - 1 + (percent or 0) / 100) / total
    return PlanProgress(number, total, round(100 * done))


def unit_rows(units: list[Unit], resolved: dict[str, ResolvedEta], now: datetime) -> list[Row]:
    rows: list[Row] = []
    for unit in units:
        plan = plan_progress(unit)
        stated = unit.eta
        if isinstance(stated, NoEta):
            rows.append(Row(unit.label, None, unit.build_hold, plan))
            continue
        eta = resolved[unit.unit]
        if not isinstance(eta, ResolvedEtaMoment):
            raise InputError(f"{unit.unit}: timed ETA was not resolved")
        moment = eta.moment
        spread = stated.spread
        earliest = parse_range_end(spread.earliest, now, moment, earliest=True) if spread else moment
        latest = parse_range_end(spread.latest, now, moment, earliest=False) if spread else moment
        rows.append(Row(unit.label, Estimate(unit.started, moment, earliest, latest), unit.build_hold, plan))
    return rows


def child_row(child: Child, now: datetime, zone: ZoneInfo) -> Row:
    """A lower production as one row: a bar from its earliest start to its latest ETA, then its counts and marks."""
    saved = child.report
    estimate = None
    notes: list[str] = []
    lines_below: tuple[str, ...] = ()
    if isinstance(saved, NoChildReport):
        notes.append(NO_REPORT)
    else:
        estimates = [row.estimate for row in saved.rows if row.estimate is not None]
        if estimates:
            last = max(each.eta for each in estimates)
            estimate = Estimate(min(each.started for each in estimates), last, last, last)
        notes.append(f"{saved.units} unit{'' if saved.units == 1 else 's'}, {saved.held} held")
        # Nothing refreshes a production that reports on demand, so its row always says when it is from.
        if isinstance(saved.cadence, ReportsOnDemand) or now - saved.written > timedelta(minutes=2 * saved.cadence.minutes):
            notes.append(f"as of {range_clock(saved.written, now)}")
        style = saved.style
        if isinstance(style, StyleSummaryUnavailable):
            lines_below = (f"{child.name}  style summary unavailable: {style.reason}",)
        elif isinstance(style, StyleSummary):
            summary = (f"{child.name}  rules done {style.rules_done}/{style.rules_total}"
                       f" · cells {math.floor(style.cells_current_pct)}% · stale {style.stale:,}"
                       f" · proposals {style.proposals} · synced {range_clock(style.synced_at, now)}")
            if style.unclassified_rules:
                summary += f" · unclassified {style.unclassified_rules} — pull nate_style"
            if isinstance(style.waiting, PendingHandoffs):
                age = now.replace(tzinfo=zone) - style.waiting.oldest_pending_since
                hours = age.total_seconds() / 3600
                if hours > 4:
                    summary += f" · hand-off waiting {int(hours)}h"
            lines_below = (summary,)
    if child.open:
        notes.append(f"{child.open} open question{'' if child.open == 1 else 's'}")
    return Row(child.name, estimate, False, None, ", ".join(notes), lines_below)


def render(report: Report, previous: dict[str, LastUnitReport], resolved: dict[str, ResolvedEta],
           now: datetime, zone_name: str, outstanding: list[Outstanding],
           *, at: str | None = None) -> list[str]:
    lines = [f"**Dailies ({report.length.capitalize()})**, {now:%H:%M} {zone_name}", ""]
    user_topics = [topic for topic in report.topics if topic.needs_user]
    other_topics = [topic for topic in report.topics if not topic.needs_user]
    units = ordered_units(report, resolved)
    grouped_waits = {unit.unit: grouped_wait(report.length, unit) for unit in report.units}
    gantt = report.length == "gantt"

    def topic_section(topic: Topic) -> None:
        lines.extend([f"### {topic.title}", f"- update: {topic.update}", f"- eta: {topic.eta}"])
        if topic.needed:
            lines.append(f"- needed: {topic.needed}")
        lines.append("")

    for topic in user_topics:
        topic_section(topic)
    for unit in units:
        if isinstance(grouped_waits[unit.unit], IdleWait) or (gantt and not unit.needs_user):
            continue
        lines.append(f"### {unit.name}: {unit.project}")
        if unit.goal:
            lines.append(f"- goal: {goal_text(unit.goal)}")
        lines.append(f"- phase: {unit.phase}")
        if unit.held:
            last = previous_report(previous, unit.unit)
            repeat = (report.length in SHORT_LENGTHS and isinstance(last, LastUnitReport)
                      and same_phase(last.phase, unit.phase) and last.held == unit.held)
            examples = f", {unit.held_examples}" if unit.held_examples and not repeat else ""
            lines.append(f"- checkpoint: not merged, because {unit.held}{examples}")
        lines.append(f"- update: {unit.update}")
        last = previous_report(previous, unit.unit)
        lines.append(f"- eta: {eta_text(unit, last, resolved[unit.unit], now, zone_name, with_note=True)}")
        drift = drift_text(unit, last, resolved[unit.unit], now, zone_name)
        if drift:
            lines.append(f"- first eta: {drift}")
        if unit.waiting_on_it:
            lines.append(f"- waiting on it: {unit.waiting_on_it}")
        if unit.needed:
            lines.append(f"- needed: {unit.needed}")
        if isinstance(unit.upcoming_work, UpcomingWork):
            items = unit.upcoming_work.items
            if report.length in SHORT_LENGTHS or len(items) == 1:
                lines.append(f"- then: {items[0]}")
            else:
                lines.append("- then:")
                lines.extend(f"  - {item}" for item in items)
        lines.append("")
    idle_units: list[tuple[int, Unit, IdleWait]] = []
    for index, unit in enumerate(report.units):
        wait = grouped_waits[unit.unit]
        if isinstance(wait, IdleWait):
            idle_units.append((index, unit, wait))
    idle_units.sort(key=lambda item: (item[2].until, item[0]))
    if idle_units:
        lines.append("### Waiting and idle")
        for _, unit, wait in idle_units:
            lines.append(f"- {unit.name} until {idle_clock(wait.until, now)}: {wait.waits_for}")
        lines.append("")
    if gantt:
        changes = [line for unit in units if not unit.needs_user
                   for line in major_changes(unit, previous_report(previous, unit.unit), resolved[unit.unit],
                                             now, zone_name)]
        reported = {unit.unit for unit in report.units}
        changes.extend(f"- {unit}: no longer in the report" for unit in previous if unit not in reported)
        if changes:
            lines.extend([*changes, ""])
    else:
        for topic in other_topics:
            topic_section(topic)

    zone = ZoneInfo(report.zone)
    rows = unit_rows(units, resolved, now)
    for child in report.children:
        rows.append(child_row(child, now, zone))
        if child.expand and isinstance(child.report, ChildReport):
            rows.extend(Row(f"{child.name}/{row.name}", row.estimate, False, None) for row in child.report.rows)
    if report.counts:
        lines.extend([counts_line(report.counts), ""])
    if rows:
        lines.extend(["```", *draw(now, rows, CHART_STYLES[report.chart]), "```", ""])
    else:
        # Every unit's run has finished and was left out, and no production is below: there is nothing to chart.
        lines.extend([NO_ROWS, ""])
    # A due item in a lower production's list needs the user as much as one named here.
    needed = (any(unit.needed for unit in report.units) or any(topic.needed for topic in report.topics)
              or any(child.open for child in report.children))
    lines.extend(footer(now, zone, zone_name, report.next_run, report.build_hold, report.mac_block, outstanding,
                        nothing_needed=not needed, agent_lines=agent_section(now, zone, at=at),
                        unreadable=[slug for child in report.children for slug in child.unreadable],
                        root=report.reports_to, counts=doc_counts(report.doc)))
    return lines


def last_report(value: object, state_path: Path, at: str | None, cadence: ReportsEvery | ReportsOnDemand) -> JsonMap:
    """What a production saves for the showrunner above it, from an input `check_render_state` passed.

    Its chart rows, the units first and then one row per production below it, and its counts with
    theirs added: a parent reads this one file and never the files further down.
    """
    report = parse_report(value, "default")
    now, _ = local_now(report.zone, "input.zone", at)
    zone = ZoneInfo(report.zone)
    resolved = resolve_eta_moments(report, load_state(state_path), now)
    rows = [*unit_rows(ordered_units(report, resolved), resolved, now),
            *(child_row(child, now, zone) for child in report.children)]
    below = [child.report for child in report.children if isinstance(child.report, ChildReport)]
    saved_rows: list[JsonMap] = []
    for row in rows:
        fields: JsonMap = {"name": row.name}
        if row.estimate is not None:
            estimate = row.estimate
            fields.update(started=f"{estimate.started:{STAMP}}", eta=f"{estimate.eta:{STAMP}}",
                          earliest=f"{estimate.earliest:{STAMP}}", latest=f"{estimate.latest:{STAMP}}")
        saved_rows.append(fields)
    interval = cadence.minutes if isinstance(cadence, ReportsEvery) else None
    return {"written": f"{now:{STAMP}}", "zone": report.zone, "interval": interval,
            "units": len(report.units) + sum(saved.units for saved in below),
            "held": sum(1 for unit in report.units if unit.held) + sum(saved.held for saved in below),
            "rows": saved_rows}


def log_line(report: Report, resolved: dict[str, ResolvedEta], now: datetime, zone_name: str) -> str:
    parts = [f"{unit.unit} {unit.phase.split(':')[0]} "
             + eta_text(unit, NoLastUnitReport(), resolved[unit.unit], now, zone_name, with_note=False)
             for unit in report.units]
    parts.extend(f"{topic.title} {topic.eta}" for topic in report.topics)
    return f"- {now:%H:%M} {zone_name}: dailies ETAs: {'; '.join(parts)}"


def local_now(zone: str, where: str, at: str | None) -> tuple[datetime, str]:
    """The minute now, or `at`, as a local time in the IANA zone, and the zone's abbreviation."""
    info = zone_named(zone, where)
    try:
        supplied = datetime.fromisoformat(at) if at else datetime.now(info)
        aware = supplied.replace(tzinfo=info) if supplied.tzinfo is None else supplied.astimezone(info)
    except ValueError:
        raise InputError(f"--at: {at!r} is not YYYY-MM-DDTHH:MM[±HH:MM]") from None
    return aware.replace(second=0, microsecond=0, tzinfo=None), aware.strftime("%Z")


def footer_main(zone: str, next_run: str | None, at: str | None, outstanding_path: Path | None, *,
                nothing_needed: bool, below: Sequence[Path] = (), root: str = "", doc: Path | None = None) -> int:
    unreadable: list[str] = []
    try:
        if next_run is not None and not TIME.match(next_run):
            raise InputError(f"--next-run: {next_run!r} is not HH:MM or HH:MM+N")
        now, abbreviation = local_now(zone, "--zone", at)
        hold = read_dailies_hold()
        mac_block = read_mac_block()
        outstanding = read_outstanding(outstanding_path)
    except (InputError, json.JSONDecodeError, OSError) as error:
        print(f"dailies_render: {error}", file=sys.stderr)
        return 2
    # A lower production's bad list is named in the footer; it never takes the root's footer down.
    for path in below:
        try:
            outstanding.extend(read_outstanding(path))
        except (InputError, ValueError, OSError):
            unreadable.append(path.stem)
    print("\n".join(footer(now, ZoneInfo(zone), abbreviation, next_run, hold, mac_block, outstanding,
                           nothing_needed=nothing_needed, agent_lines=agent_section(now, ZoneInfo(zone), at=at),
                           unreadable=unreadable, root=root, counts=doc_counts(doc))))
    return 0


def main(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Render the /showrunner:dailies report from its fixed template.")
    _ = parser.add_argument("input", type=Path, nargs="?")
    _ = parser.add_argument("--chart", choices=sorted(CHART_STYLES), help=f"set the chart mode in {CHART_CONF} for every showrunner")
    _ = parser.add_argument("--state", type=Path)
    _ = parser.add_argument("--log", type=Path)
    _ = parser.add_argument("--at")
    _ = parser.add_argument("--footer", action="store_true", help="print the separated bullet footer, including the report's Agents lines")
    _ = parser.add_argument("--zone", help="with --footer: the production's zone, as an IANA name")
    _ = parser.add_argument("--next-run", help="with --footer: the next scheduled report, HH:MM or HH:MM+N; leave it out when none is scheduled")
    _ = parser.add_argument("--nothing-needed", action="store_true", help="with --footer: no subject needs a follow-up nobody has started")
    _ = parser.add_argument("--outstanding", type=Path, help="JSON list of what waits on the user; suppresses nothing needed when due")
    _ = parser.add_argument("--below", type=Path, action="append", default=[],
                            help="with --footer, once per lower production: its list, counted with --outstanding; one that cannot be read is named")
    _ = parser.add_argument("--reports-to", default="", help="with --footer: the root production a lower showrunner is under")
    _ = parser.add_argument("--doc", type=Path, help="with --footer: the production doc whose changes, efficiency and proposals are counted")
    options = parser.parse_args(arguments)
    input_path = cast(Path | None, options.input)
    chart = cast(str | None, options.chart)
    state_path = cast(Path | None, options.state)
    log_path = cast(Path | None, options.log)
    at = cast(str | None, options.at)
    zone = cast(str | None, options.zone)
    next_run = cast(str | None, options.next_run)
    nothing_needed = cast(bool, options.nothing_needed)
    outstanding_path = cast(Path | None, options.outstanding)
    below = cast(list[Path], options.below)
    reports_to = cast(str, options.reports_to)
    doc = cast(Path | None, options.doc)
    if cast(bool, options.footer):
        if input_path is not None or chart is not None or state_path is not None or log_path is not None:
            parser.error("--footer takes no input file, --chart, --state or --log")
        if zone is None:
            parser.error("--footer needs --zone")
        return footer_main(zone, next_run, at, outstanding_path, nothing_needed=nothing_needed,
                           below=below, root=reports_to, doc=doc)
    if zone is not None or next_run is not None or nothing_needed or below or reports_to or doc is not None:
        parser.error("--zone, --next-run, --nothing-needed, --below, --reports-to and --doc go with --footer; "
                     + "a report takes them from its input")
    if chart is not None:
        write_chart(CHART_CONF, chart)
        print(f"dailies chart: {chart} ({CHART_CONF})", file=sys.stderr)
    if input_path is None:
        if chart is None:
            parser.error("give an input file, --chart (or both), or --footer")
        return 0
    try:
        report = parse_report(cast(object, json.loads(input_path.read_text())), read_chart(CHART_CONF))
        previous = load_state(state_path)
        now, abbreviation = local_now(report.zone, "input.zone", at)
        resolved = resolve_eta_moments(report, previous, now)
        check_changes(report, previous, resolved)
        check_idle(report, now)
        outstanding = read_outstanding(outstanding_path)
    except (InputError, json.JSONDecodeError, OSError) as error:
        print(f"dailies_render: {error}", file=sys.stderr)
        return 2
    print("\n".join(render(report, previous, resolved, now, abbreviation, outstanding, at=at)))
    try:
        if state_path is not None:
            save_state(state_path, report, resolved, previous)
        if log_path is not None:
            with log_path.open("a") as log:
                _ = log.write(log_line(report, resolved, now, abbreviation) + "\n")
    except OSError as error:
        print(f"dailies_render: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
