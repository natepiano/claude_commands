#!/usr/bin/env python3
"""Render the /showrunner:dailies report from its fixed template.

Usage: dailies_render.py [<input.json>] [--chart default|ascii] [--state <state.json>] [--log <log.md>] [--at <YYYY-MM-DDTHH:MM[±HH:MM]>]
       dailies_render.py --footer --zone <IANA zone> [--next-run <HH:MM[+N]>]
                         [--nothing-needed] [--at <YYYY-MM-DDTHH:MM[±HH:MM]>]
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
--footer prints the separated bullet footer before every showrunner reply's
         Waiting on block and at every report's end, at the current time in
         --zone. Its Agents bullets
         use the report's words. Hold lines come from BUILD_HOLD_DIR or
         ~/.local/state/build-hold and MAC_TEST_STATE_DIR or
         ~/.local/state/mac-test.
--outstanding  JSON list of what waits on the user, `[{"since":
         "YYYY-MM-DDTHH:MM", "text": "..."}]`; outstanding items suppress
         ` - nothing needed` and belong in the showrunner's Waiting on block,
         which this renderer does not print. A missing file is an empty list.

The input format is in ~/.claude/commands/showrunner/dailies.md.
"""

import argparse
import json
import math
import re
import sys
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import NamedTuple, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "build_hold"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mac_test"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "whoami"))
import run_out
from run_out import READINGS_LOG, Reading
from build_hold import ActiveHolders, HoldState, KnownReleaseEta, NoHolders, Holder, ReleaseRecordReadError, cycle_status_lines, holder_directory, read_cycle, read_holders, release_record_error_line
from mac_test import ActiveMacBlock, NoMacBlock, PendingMacBlock, ci_may_still_be_on, read_block, state_paths

LENGTHS = {"simple": 240, "page": 480, "elaborate": None}
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
AGENTS_DIR = Path.home() / "rust" / "hanadocs" / "agents"
CHART_KEY = "chart"
NOW_MARK = "▼ "
# A unit under a /build_hold carries this marker on its timeline row; no
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
class Eta:
    time: str | None
    earliest: str | None
    latest: str | None
    none: str | None
    detail: str | None
    percent: int | None
    why: str | None
    first: datetime | None
    fixes: int


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
class Outstanding:
    since: datetime
    text: str
    after: datetime | None = None


@dataclass(frozen=True)
class UnreadableMacBlock:
    """A Mac block whose state file could not be decoded or read."""

    path: Path


MacBlockFooterState = NoMacBlock | PendingMacBlock | ActiveMacBlock | UnreadableMacBlock


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
    """A range end, on the day that keeps it on its side of the ETA: earliest at or before, latest at or after."""
    moment = parse_time(text, now)
    if "+" in text:
        return moment
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
    return chart


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
    lines = [" " * (ROW_LABEL_WIDTH + start_width) + "".join(axis).rstrip()]

    last = WINDOW_HOURS - 1
    for row in rows:
        prefix = f"{row.name:<{ROW_LABEL_WIDTH}}{early.get(row.name, ''):<{start_width}}"
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
    indent = ROW_LABEL_WIDTH + start_width
    reach = max((display_width(line) - indent for line in lines[1:]), default=0)
    lines[0] = " " * indent + "".join(axis[: -(-reach // 2)]).rstrip()
    return with_plans(lines, rows)


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


def parse_eta(value: object, where: str) -> Eta:
    fields = as_map(value, where)
    check_keys(fields, {"time", "earliest", "latest", "none", "detail", "percent", "why", "first", "fixes"}, where)
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
    return Eta(time, earliest, latest, none, detail, percent, why, first, fixes)


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
    found = PLUMBING.search(line)
    if found is not None:
        raise InputError(
            f"{name}: {found.group(0)!r} is the production's own plumbing or shorthand, which the user never sees; "
            + "say what changes in the app, or leave it out"
        )


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
        check_keys(fields, {"since", "text", "after"}, where)
        since, text, after = fields.get("since"), fields.get("text"), fields.get("after")
        if not isinstance(since, str) or not STARTED.match(since):
            raise InputError(f"{where}.since: expected YYYY-MM-DDTHH:MM")
        if after is not None and (not isinstance(after, str) or not STARTED.match(after)):
            raise InputError(f"{where}.after: expected YYYY-MM-DDTHH:MM")
        if not isinstance(text, str) or not text.strip() or "\n" in text:
            raise InputError(f"{where}.text: expected one non-empty line")
        check_plumbing(text, f"{where}.text")
        items.append(Outstanding(datetime.fromisoformat(since), text, datetime.fromisoformat(after) if after else None))
    return sorted(items, key=lambda item: item.since)


def read_dailies_hold() -> HoldState:
    """Read holders and check the purpose that appears in reports and footers."""
    hold = read_holders(holder_directory())
    if isinstance(hold, ActiveHolders):
        for holder in hold.holders:
            try:
                check_plumbing(holder.purpose, f"build hold {holder.name!r} purpose")
            except InputError as error:
                raise InputError(f"{error}; have {holder.name} run /build_hold hold again with other words") from None
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
                f"{error}; have {block.holder} run /mac_test block again with other words"
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
        {"unit", "name", "label", "project", "goal", "phase", "started", "held", "held_examples", "build_hold", "update", "eta", "waiting_on_it", "needed", "needs_user", "then", "idle"},
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
    _ = path.write_text(f"{CHART_KEY}={chart}\n")


def parse_report(value: object, chart: str) -> Report:
    fields = as_map(value, "input")
    if "build_hold" in fields:
        raise InputError("input.build_hold: remove this field; holds are read from the holder files")
    check_keys(fields, {"length", "zone", "next_run", "units", "topics"}, "input")
    length = text(fields, "length", "input")
    if length not in LENGTHS:
        raise InputError(f"input.length: must be one of {', '.join(LENGTHS)}")
    zone = text(fields, "zone", "input")
    next_run = clock_text(fields, "next_run", "input")
    units = [parse_unit(item, f"units[{index}]", length) for index, item in enumerate(as_list(fields.get("units"), "input.units"))]
    if not units:
        raise InputError("input.units: every unit is reported, so the list cannot be empty")
    topics = [parse_topic(item, f"topics[{index}]", length) for index, item in enumerate(as_list(fields.get("topics"), "input.topics"))]
    hold = read_dailies_hold()
    mac_block = read_mac_block()
    marked = any(unit.build_hold for unit in units)
    if marked and isinstance(hold, NoHolders):
        raise InputError("units.build_hold: a unit is marked but no holder file exists; remove the stale unit marker")
    if not marked and isinstance(hold, ActiveHolders):
        raise InputError("units.build_hold: holder files are active but no unit is marked; mark the held unit")
    if isinstance(hold, ActiveHolders):
        holder_names = {holder.name for holder in hold.holders}
        for unit in units:
            if unit.build_hold and unit.unit in holder_names:
                raise InputError(f"units.build_hold: {unit.unit} holds the build hold itself; remove its marker")
    return Report(length, chart, zone, next_run, hold, mac_block, units, topics)


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
    """Resolve this report's ETA once, preserving an unchanged same-phase moment."""
    if unit.eta.time is None:
        return NoResolvedEtaMoment()
    if (isinstance(previous, LastUnitReport)
            and same_phase(previous.phase, unit.phase)
            and isinstance(previous.eta, LastReportedEta)
            and previous.eta.text == unit.eta.time):
        return ResolvedEtaMoment(previous.eta.moment)
    return ResolvedEtaMoment(parse_time(unit.eta.time, now))


def resolve_eta_moments(report: Report, previous: dict[str, LastUnitReport],
                        now: datetime) -> dict[str, ResolvedEta]:
    """Resolve every unit's ETA exactly once for all report consumers."""
    return {unit.unit: resolve_eta_moment(unit, previous_report(previous, unit.unit), now)
            for unit in report.units}


def first_eta(unit: Unit, previous: Previous, resolved: ResolvedEta) -> datetime | None:
    """The phase's first stated ETA: the input's `first`, else the state's for the same phase, else this report's."""
    if unit.eta.first is not None:
        return unit.eta.first
    if (isinstance(previous, LastUnitReport) and same_phase(previous.phase, unit.phase)
            and previous.first is not None):
        return previous.first
    return resolved.moment if isinstance(resolved, ResolvedEtaMoment) else None


def drift_text(unit: Unit, previous: Previous, resolved: ResolvedEta,
               now: datetime, zone_name: str) -> str | None:
    """The first ETA, how far the current one has moved from it and the fix rounds added since; `None` until it moves."""
    first = first_eta(unit, previous, resolved)
    if first is None or not isinstance(resolved, ResolvedEtaMoment):
        return None
    minutes = round((resolved.moment - first).total_seconds() / 60)
    if minutes == 0 and unit.eta.fixes == 0:
        return None
    hours, rest = divmod(abs(minutes), 60)
    rounds = f", {unit.eta.fixes} fix round{'' if unit.eta.fixes == 1 else 's'} added" if unit.eta.fixes else ""
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
            "eta_text": unit.eta.time if moment else None,
            "held": unit.held,
            "first": first.isoformat() if first else None,
        }
    _ = path.write_text(json.dumps(state, indent=2) + "\n")


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
        return None
    if minutes == 0:
        return "unchanged, overdue" if moment < now else "unchanged"
    hours, rest = divmod(abs(minutes), 60)
    note = f"changed: {'+' if minutes > 0 else '-'}{hours}:{rest:02d}"
    return f"{note} because {why}" if why else note


def check_changes(report: Report, previous: dict[str, LastUnitReport],
                  resolved: dict[str, ResolvedEta]) -> None:
    """An ETA that moved CHANGE_NEEDS_WHY_MINUTES or more since the last report says why."""
    for index, unit in enumerate(report.units):
        if unit.eta.time is None or unit.eta.why is not None:
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
    return f"{range_clock(moment, now)} {local:%Z} ({'overdue ' if minutes < 0 else ''}{count})"


def hold_line(holder: Holder, now: datetime, zone: ZoneInfo) -> str:
    """A holder's line, shared by reports and reply footers."""
    since = holder.since.astimezone(zone)
    release = release_text(holder.release, now, zone) if isinstance(holder.release, KnownReleaseEta) else "unknown"
    return f"{BUILD_HOLD_MARK}: {holder.name} since {since:%H:%M} {since:%Z}, for {holder.purpose} - release eta: {release}"


def mac_block_line(
    block: PendingMacBlock | ActiveMacBlock | UnreadableMacBlock,
    zone: ZoneInfo,
) -> str:
    """A Mac block line shared by reports and reply footers."""
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


def agent_fields(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return {}
    if not lines or lines[0] != "---":
        return {}
    if "---" not in lines[1:]:
        return {}
    fields: dict[str, str] = {}
    for line in lines[1:]:
        if line == "---":
            break
        if (match := re.fullmatch(r"([A-Za-z_][\w-]*):\s*(.*?)\s*", line)) is not None:
            fields[match.group(1)] = match.group(2).strip("\"'")
    return fields


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
    for path in sorted(AGENTS_DIR.glob("*.md")):
        fields = agent_fields(path)
        if fields.get("state") == "active":
            account_readings = readings.get(path.stem, [])
            if minute_end is not None:
                account_readings = [reading for reading in account_readings if reading.at.timestamp() < minute_end]
                checked_at = agent_time(fields.get("weekly_usage_checked_at"))
                if checked_at is not None and checked_at.timestamp() >= minute_end and account_readings:
                    latest = account_readings[-1]
                    fields["weekly_usage_checked_at"] = latest.at.isoformat()
                    fields["weekly_remaining_usage"] = str(100 - latest.used_percent)
            lines.append(agent_line(path.stem, fields, account_readings, now, zone))
    if len(lines) == 1:
        lines.append("- none active")
    lines.append("")
    return lines


def footer(
    now: datetime, zone: ZoneInfo, zone_name: str, next_run: str | None,
    hold: HoldState, mac_block: MacBlockFooterState, outstanding: list[Outstanding], *,
    nothing_needed: bool, agent_lines: list[str]
) -> list[str]:
    """The separated update footer shared by replies and dailies reports."""
    items = [hold_line(holder, now, zone) for holder in hold.holders] if isinstance(hold, ActiveHolders) else []
    if isinstance(hold, ActiveHolders):
        try:
            cycle = read_cycle()
        except ReleaseRecordReadError as error:
            items.append(f"  {release_record_error_line(error)}")
        else:
            if cycle is not None and any(holder.name in cycle["holders"] for holder in hold.holders):
                items.extend(f"  {line}" for line in cycle_status_lines(cycle, now, zone))
    if not isinstance(mac_block, NoMacBlock):
        items.append(mac_block_line(mac_block, zone))
    items.extend(line.removeprefix("- ") for line in agent_lines if line and line != "### Agents")
    # An item the user deferred stays hidden until its `after` time, in the report's zone.
    local_now = now.astimezone(zone).replace(tzinfo=None) if now.tzinfo else now
    outstanding = [item for item in outstanding if item.after is None or item.after <= local_now]
    schedule = f"next dailies: {range_clock(parse_time(next_run, now), now)} {zone_name}" if next_run else "no dailies scheduled"
    quiet = nothing_needed and not outstanding
    items.append(f"{schedule}{' - nothing needed' if quiet else ''}")
    return ["", "---", f"{now:%H:%M} {zone_name} update:", "",
            *(f"{item[:2]}* {item[2:]}" if item.startswith("  ") else f"* {item}" for item in items)]


def eta_text(unit: Unit, previous: Previous, resolved: ResolvedEta,
             now: datetime, zone_name: str, with_note: bool) -> str:
    eta = unit.eta
    if eta.time is None:
        words = eta.none or ""
    else:
        if not isinstance(resolved, ResolvedEtaMoment):
            raise InputError(f"{unit.unit}: timed ETA was not resolved")
        moment = resolved.moment
        notes: list[str] = []
        note = change_note(moment, previous, unit.phase, now, eta.why) if with_note else None
        if note:
            notes.append(note)
        if eta.earliest and eta.latest:
            notes.append(f"range {range_clock(parse_range_end(eta.earliest, now, moment, earliest=True), now)}–{range_clock(parse_range_end(eta.latest, now, moment, earliest=False), now)}")
        done = f"{eta.percent}% done" if eta.percent is not None else "percent done not stated"
        words = f"{clock(moment, now, zone_name)}, {done}" + (f" ({'; '.join(notes)})" if notes else "")
    return f"{words}; {eta.detail}" if with_note and eta.detail else words


def ordered_units(report: Report, resolved: dict[str, ResolvedEta]) -> list[Unit]:
    def key(pair: tuple[int, Unit]) -> tuple[int, float, int]:
        index, unit = pair
        if unit.needs_user:
            return (0, 0.0, index)
        if unit.eta.time is None:
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
    done = (number - 1 + (unit.eta.percent or 0) / 100) / total
    return PlanProgress(number, total, round(100 * done))


def render(report: Report, previous: dict[str, LastUnitReport], resolved: dict[str, ResolvedEta],
           now: datetime, zone_name: str, outstanding: list[Outstanding],
           *, at: str | None = None) -> list[str]:
    lines = [f"**Dailies ({report.length.capitalize()})**, {now:%H:%M} {zone_name}", ""]
    user_topics = [topic for topic in report.topics if topic.needs_user]
    other_topics = [topic for topic in report.topics if not topic.needs_user]
    units = ordered_units(report, resolved)
    grouped_waits = {unit.unit: grouped_wait(report.length, unit) for unit in report.units}

    def topic_section(topic: Topic) -> None:
        lines.extend([f"### {topic.title}", f"- update: {topic.update}", f"- eta: {topic.eta}"])
        if topic.needed:
            lines.append(f"- needed: {topic.needed}")
        lines.append("")

    for topic in user_topics:
        topic_section(topic)
    for unit in units:
        if isinstance(grouped_waits[unit.unit], IdleWait):
            continue
        lines.append(f"### {unit.name}: {unit.project}")
        if unit.goal:
            lines.append(f"- goal: {goal_text(unit.goal)}")
        lines.append(f"- phase: {unit.phase}")
        if unit.held:
            last = previous_report(previous, unit.unit)
            repeat = (report.length == "simple" and isinstance(last, LastUnitReport)
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
            if report.length == "simple" or len(items) == 1:
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
    for topic in other_topics:
        topic_section(topic)

    rows: list[Row] = []
    for unit in units:
        plan = plan_progress(unit)
        if unit.eta.time is None:
            rows.append(Row(unit.label, None, unit.build_hold, plan))
            continue
        eta = resolved[unit.unit]
        if not isinstance(eta, ResolvedEtaMoment):
            raise InputError(f"{unit.unit}: timed ETA was not resolved")
        moment = eta.moment
        earliest = parse_range_end(unit.eta.earliest, now, moment, earliest=True) if unit.eta.earliest else moment
        latest = parse_range_end(unit.eta.latest, now, moment, earliest=False) if unit.eta.latest else moment
        rows.append(Row(unit.label, Estimate(unit.started, moment, earliest, latest), unit.build_hold, plan))
    lines.extend(["```", *draw(now, rows, CHART_STYLES[report.chart]), "```", ""])
    needed = any(unit.needed for unit in report.units) or any(topic.needed for topic in report.topics)
    zone = ZoneInfo(report.zone)
    lines.extend(footer(now, zone, zone_name, report.next_run, report.build_hold, report.mac_block, outstanding,
                        nothing_needed=not needed, agent_lines=agent_section(now, zone, at=at)))
    return lines


def log_line(report: Report, resolved: dict[str, ResolvedEta], now: datetime, zone_name: str) -> str:
    parts = [f"{unit.unit} {unit.phase.split(':')[0]} "
             + eta_text(unit, NoLastUnitReport(), resolved[unit.unit], now, zone_name, with_note=False)
             for unit in report.units]
    parts.extend(f"{topic.title} {topic.eta}" for topic in report.topics)
    return f"- {now:%H:%M} {zone_name}: dailies ETAs: {'; '.join(parts)}"


def local_now(zone: str, where: str, at: str | None) -> tuple[datetime, str]:
    """The minute now, or `at`, as a local time in the IANA zone, and the zone's abbreviation."""
    try:
        info = ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError):
        raise InputError(f"{where}: {zone!r} is not an IANA zone name") from None
    try:
        supplied = datetime.fromisoformat(at) if at else datetime.now(info)
        aware = supplied.replace(tzinfo=info) if supplied.tzinfo is None else supplied.astimezone(info)
    except ValueError:
        raise InputError(f"--at: {at!r} is not YYYY-MM-DDTHH:MM[±HH:MM]") from None
    return aware.replace(second=0, microsecond=0, tzinfo=None), aware.strftime("%Z")


def footer_main(zone: str, next_run: str | None, at: str | None, outstanding_path: Path | None, *, nothing_needed: bool) -> int:
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
    print("\n".join(footer(now, ZoneInfo(zone), abbreviation, next_run, hold, mac_block, outstanding,
                           nothing_needed=nothing_needed, agent_lines=agent_section(now, ZoneInfo(zone), at=at))))
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
    if cast(bool, options.footer):
        if input_path is not None or chart is not None or state_path is not None or log_path is not None:
            parser.error("--footer takes no input file, --chart, --state or --log")
        if zone is None:
            parser.error("--footer needs --zone")
        return footer_main(zone, next_run, at, outstanding_path, nothing_needed=nothing_needed)
    if zone is not None or next_run is not None or nothing_needed:
        parser.error("--zone, --next-run and --nothing-needed go with --footer; a report takes them from its input")
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
    if state_path is not None:
        save_state(state_path, report, resolved, previous)
    if log_path is not None:
        with log_path.open("a") as log:
            _ = log.write(log_line(report, resolved, now, abbreviation) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
