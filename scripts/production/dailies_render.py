#!/usr/bin/env python3
"""Render the /showrunner:dailies report from its fixed template.

Usage: dailies_render.py [<input.json>] [--chart default|ascii] [--state <state.json>] [--log <log.md>] [--at <YYYY-MM-DDTHH:MM>]

The input gives each subject's fields; this script owns the layout, so no line
of the template can be dropped or renamed. It refuses, with exit 2, an input
that breaks a template rule: a unit without its phase or `held`, a follow-up
or last phase without `then`, an unknown field, an update too long for the
length, an update or held reason that names another phase without saying
why, a held reason carrying its own examples, a held count the update does
not report against (`<k> of <N>`), an ETA time without its percent, an
ETA that moved CHANGE_NEEDS_WHY_MINUTES or more since the last report
without `why`, a `then` naming a phase at or before the heading's, or a line
using the production's own plumbing words (PLUMBING).

--state  JSON file holding each unit's last reported phase, ETA, held
         reason and the phase's first ETA. The script reads it to write `(unchanged)` / `(changed:
         ±h:mm)` and, in a simple report, to print a held reason's examples
         only the first time,
         then saves this report's values to it.
--log    appends the `dailies ETAs:` line to this file.
--at     renders as if the clock read this local time (for checks).
--chart  sets the chart mode in CHART_CONF, which every showrunner's dailies
         read; with no input file it only sets the mode.

The input format is in ~/.claude/commands/showrunner/dailies.md.
"""

import argparse
import json
import re
import sys
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

LENGTHS = {"simple": 240, "page": 480, "elaborate": None}
PHASE = re.compile(r"^(?:Phase (\d+) of (\d+)|follow-up (\d+) of (\d+)): \S")
TIME = re.compile(r"^\d{1,2}:\d{2}(?:\+\d+)?$")
NONE = ("none measured - requested", "none measured", "no ETA stated yet")
# An ETA that moved this much since the last report carries its reason (user,
# 2026-10-01: why a unit's timing changed is an important detail).
CHANGE_NEEDS_WHY_MINUTES = 15
LABEL_LIMIT = 8
RETURN = re.compile(r"\bthe plan at Phase \d+|\bplan done\b")
PHASE_MENTION = re.compile(r"\bPhases? (\d+(?:\s*(?:,|and|-|–|to)\s*\d+)*)|\bP(\d+)\b")
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
# 2026-10-03: the coloured chart by default, this one when remote).
LINE = "──"
CROSS = "┼─"
DOT = "● "
DOT_RANGED = "●·"
DOTS = "··"
CAP = "┤ "
# The chart mode every showrunner's dailies use, kept in one file so no
# session has to remember it (user, 2026-10-03). `--chart` sets it.
CHART_CONF = Path.home() / ".local/state/showrunner/dailies.conf"
CHART_KEY = "chart"
NOW_MARK = "▼ "
# A unit under a /build_hold carries this marker; no marker means not held
# (user, 2026-10-03: "Without that marker I will assume it is not held").
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
# opens six hours before the three-hour mark at or before now, so a phase that
# started this morning shows its whole run, and now always sits a quarter to
# a third of the way in.
WINDOW_HOURS = 24
LABEL_EVERY_HOURS = 3
HOURS_BEFORE = 6

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
class Unit:
    unit: str
    name: str
    label: str
    project: str
    phase: str
    started: datetime
    held: str | None
    held_examples: str | None
    build_hold: str | None
    update: str
    eta: Eta
    waiting_on_it: str | None
    needed: str | None
    needs_user: bool
    then: str | None


@dataclass(frozen=True)
class Topic:
    title: str
    update: str
    eta: str
    needed: str | None
    needs_user: bool


@dataclass(frozen=True)
class Report:
    length: str
    chart: str
    zone: str
    next_run: str | None
    units: list[Unit]
    topics: list[Topic]


@dataclass(frozen=True)
class Previous:
    phase: str
    eta: datetime | None
    held: str | None
    first: datetime | None


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
    eta: str
    eta_ranged: str
    range_fill: str
    latest: str
    show_range: bool


CHART_STYLES = {
    "default": ChartStyle(WHITE, GREEN, BLUE, BLUE, BLANK, RED, show_range=True),
    "ascii": ChartStyle(LINE, CROSS, DOT, DOT_RANGED, DOTS, CAP, show_range=False),
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


def window_start(now: datetime) -> datetime:
    mark = now.replace(hour=now.hour - now.hour % LABEL_EVERY_HOURS, minute=0, second=0, microsecond=0)
    return mark - timedelta(hours=HOURS_BEFORE)


def draw(now: datetime, rows: list[Row], style: ChartStyle) -> list[str]:
    """24 hourly cells in `style`: the run from the phase's start (or the left edge) to the ETA, marks at the earliest time, the ETA and the latest, and the range filled between the ETA and the latest; `→` past the right edge; a start before the left edge is written before the cells."""
    start = window_start(now)

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
            cells[max(0, column(estimate.earliest))] = style.earliest
        # The ETA takes the earliest time's cell when the two share an hour;
        # the latest keeps its own.
        cells[eta_cell] = style.eta_ranged if ranged and latest_cell > eta_cell else style.eta
        if ranged:
            for index in range(eta_cell + 1, latest_cell):
                cells[index] = style.range_fill
            cells[latest_cell] = style.latest
        arrow = "→" if column(estimate.latest) > last else ""
        span = f"{estimate.eta:%H:%M}"
        if ranged and style.show_range:
            span += f" ({estimate.earliest:%H:%M}–{estimate.latest:%H:%M})"
        lines.append(f"{prefix}{''.join(cells).rstrip()}{arrow} {span}{hold}")
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
    header = f"{lines[0]}{' ' * (left - display_width(lines[0]) + text_width + 1 + PLAN_BLOCKS)}{PLAN_FULL}"
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
    if OTHER_PLAN.search(then):
        return
    earlier = [found for found in phases_named(then) if found <= number]
    if earlier:
        raise InputError(
            f"{where}.then: names Phase {', '.join(str(found) for found in earlier)} after this Phase {number}, which reads as impossible. "
            + "Renumber the plan so its numbers follow the run order (packaging: the showrunner's call), then report the new numbers."
        )


def check_words(line: str, key: str, where: str) -> None:
    """A line says what changes in the app, in words you know, without the production's plumbing."""
    found = PLUMBING.search(line)
    if found is not None:
        raise InputError(
            f"{where}.{key}: {found.group(0)!r} is the production's own plumbing or shorthand, which the user never sees; "
            + "say what changes in the app, or leave it out"
        )


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


def parse_unit(value: object, where: str, length: str) -> Unit:
    fields = as_map(value, where)
    check_keys(
        fields,
        {"unit", "name", "label", "project", "phase", "started", "held", "held_examples", "build_hold", "update", "eta", "waiting_on_it", "needed", "needs_user", "then"},
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
    then = optional_text(fields, "then", where)
    plan_number, plan_total, follow_number, follow_total = match.groups()
    number = int(plan_number or follow_number)
    total = int(plan_total or follow_total)
    if number > total:
        raise InputError(f"{where}.phase: {number} of {total}")
    if follow_number is not None and (then is None or not RETURN.search(then)):
        raise InputError(f"{where}.then: a follow-up names the plan phase it returns to ('the plan at Phase <N>'), or says 'plan done' after reading the plan")
    if then is None and number == total:
        raise InputError(f"{where}.then: required on a last phase; name the queued work, or 'nothing queued'")
    heading_number = int(plan_number) if plan_number else None
    if then is not None and heading_number is not None:
        check_then_order(then, heading_number, where)
    for key in ("phase", "held", "held_examples", "build_hold", "update", "waiting_on_it", "needed", "then"):
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
    return Unit(
        unit=unit,
        name=optional_text(fields, "name", where) or unit,
        label=label,
        project=text(fields, "project", where),
        phase=phase,
        started=datetime.fromisoformat(started_text),
        held=held,
        held_examples=held_examples,
        build_hold=optional_text(fields, "build_hold", where),
        update=update,
        eta=parse_eta(fields.get("eta"), f"{where}.eta"),
        waiting_on_it=optional_text(fields, "waiting_on_it", where),
        needed=optional_text(fields, "needed", where),
        needs_user=flag(fields, "needs_user", where),
        then=then,
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
    return Report(length, chart, zone, next_run, units, topics)


def load_state(path: Path | None) -> dict[str, Previous]:
    if path is None or not path.exists():
        return {}
    fields = as_map(cast(object, json.loads(path.read_text())), str(path))
    previous: dict[str, Previous] = {}
    for unit, entry in fields.items():
        entry_fields = as_map(entry, f"{path}:{unit}")
        phase = text(entry_fields, "phase", f"{path}:{unit}")
        eta = optional_text(entry_fields, "eta", f"{path}:{unit}")
        held = optional_text(entry_fields, "held", f"{path}:{unit}")
        first = optional_text(entry_fields, "first", f"{path}:{unit}")
        previous[unit] = Previous(phase, datetime.fromisoformat(eta) if eta else None, held, datetime.fromisoformat(first) if first else None)
    return previous


def first_eta(unit: Unit, previous: Previous | None, now: datetime) -> datetime | None:
    """The phase's first stated ETA: the input's `first`, else the state's for the same phase, else this report's."""
    if unit.eta.first is not None:
        return unit.eta.first
    if previous is not None and previous.phase == unit.phase and previous.first is not None:
        return previous.first
    return parse_time(unit.eta.time, now) if unit.eta.time else None


def drift_text(unit: Unit, previous: Previous | None, now: datetime, zone_name: str) -> str | None:
    """The first ETA, how far the current one has moved from it and the fix rounds added since; `None` until it moves."""
    first = first_eta(unit, previous, now)
    if first is None or unit.eta.time is None:
        return None
    minutes = round((parse_time(unit.eta.time, now) - first).total_seconds() / 60)
    if minutes == 0 and unit.eta.fixes == 0:
        return None
    hours, rest = divmod(abs(minutes), 60)
    rounds = f", {unit.eta.fixes} fix round{'' if unit.eta.fixes == 1 else 's'} added" if unit.eta.fixes else ""
    return f"{clock(first, now, zone_name)} (now {'-' if minutes < 0 else '+'}{hours}:{rest:02d}{rounds})"


def save_state(path: Path, report: Report, now: datetime, previous: dict[str, Previous]) -> None:
    state: dict[str, dict[str, str | None]] = {}
    for unit in report.units:
        moment = parse_time(unit.eta.time, now) if unit.eta.time else None
        first = first_eta(unit, previous.get(unit.unit), now)
        state[unit.unit] = {"phase": unit.phase, "eta": moment.isoformat() if moment else None, "held": unit.held, "first": first.isoformat() if first else None}
    _ = path.write_text(json.dumps(state, indent=2) + "\n")


def clock(moment: datetime, now: datetime, zone_name: str) -> str:
    days = (moment.date() - now.date()).days
    base = f"{moment:%H:%M} {zone_name}"
    if days == 0:
        return base
    if days == 1:
        return f"{base} tomorrow"
    return f"{moment:%a} {base}"


def change_minutes(moment: datetime, previous: Previous | None, phase: str) -> int | None:
    """Minutes the ETA moved since the last report of the same phase; `None` on a first ETA or a new phase."""
    if previous is None or previous.phase != phase or previous.eta is None:
        return None
    return round((moment - previous.eta).total_seconds() / 60)


def change_note(moment: datetime, previous: Previous | None, phase: str, now: datetime, why: str | None) -> str | None:
    minutes = change_minutes(moment, previous, phase)
    if minutes is None:
        return None
    if minutes == 0:
        return "unchanged, overdue" if moment < now else "unchanged"
    hours, rest = divmod(abs(minutes), 60)
    note = f"changed: {'+' if minutes > 0 else '-'}{hours}:{rest:02d}"
    return f"{note} because {why}" if why else note


def check_changes(report: Report, previous: dict[str, Previous], now: datetime) -> None:
    """An ETA that moved CHANGE_NEEDS_WHY_MINUTES or more since the last report says why."""
    for index, unit in enumerate(report.units):
        if unit.eta.time is None or unit.eta.why is not None:
            continue
        minutes = change_minutes(parse_time(unit.eta.time, now), previous.get(unit.unit), unit.phase)
        if minutes is not None and abs(minutes) >= CHANGE_NEEDS_WHY_MINUTES:
            raise InputError(
                f"units[{index}].eta.why: the ETA moved {minutes:+d} minutes since the last report; "
                + "say why in a few words (rendered after 'because'), from the unit director's own reports"
            )


def range_clock(moment: datetime, now: datetime) -> str:
    """A range end: the bare time today, the weekday before it on any other day."""
    return f"{moment:%H:%M}" if moment.date() == now.date() else f"{moment:%a %H:%M}"


def eta_text(unit: Unit, previous: Previous | None, now: datetime, zone_name: str, with_note: bool) -> str:
    eta = unit.eta
    if eta.time is None:
        words = eta.none or ""
    else:
        moment = parse_time(eta.time, now)
        notes: list[str] = []
        note = change_note(moment, previous, unit.phase, now, eta.why) if with_note else None
        if note:
            notes.append(note)
        if eta.earliest and eta.latest:
            notes.append(f"range {range_clock(parse_range_end(eta.earliest, now, moment, earliest=True), now)}–{range_clock(parse_range_end(eta.latest, now, moment, earliest=False), now)}")
        done = f"{eta.percent}% done" if eta.percent is not None else "percent done not stated"
        words = f"{clock(moment, now, zone_name)}, {done}" + (f" ({'; '.join(notes)})" if notes else "")
    return f"{words}; {eta.detail}" if with_note and eta.detail else words


def ordered_units(report: Report, now: datetime) -> list[Unit]:
    def key(pair: tuple[int, Unit]) -> tuple[int, float, int]:
        index, unit = pair
        if unit.needs_user:
            return (0, 0.0, index)
        if unit.eta.time is None:
            return (2, 0.0, index)
        return (1, parse_time(unit.eta.time, now).timestamp(), index)

    return [unit for _, unit in sorted(enumerate(report.units), key=key)]


def plan_progress(unit: Unit) -> PlanProgress | None:
    """The whole plan's percent done: earlier phases whole, this one at its stated percent (none stated counts as 0); a follow-up has none."""
    match = PHASE.match(unit.phase)
    if match is None or match.group(1) is None:
        return None
    number, total = int(match.group(1)), int(match.group(2))
    done = (number - 1 + (unit.eta.percent or 0) / 100) / total
    return PlanProgress(number, total, round(100 * done))


def render(report: Report, previous: dict[str, Previous], now: datetime, zone_name: str) -> list[str]:
    lines = [f"**Dailies ({report.length.capitalize()})**, {now:%H:%M} {zone_name}", ""]
    user_topics = [topic for topic in report.topics if topic.needs_user]
    other_topics = [topic for topic in report.topics if not topic.needs_user]
    units = ordered_units(report, now)

    def topic_section(topic: Topic) -> None:
        lines.extend([f"### {topic.title}", f"- update: {topic.update}", f"- eta: {topic.eta}"])
        if topic.needed:
            lines.append(f"- needed: {topic.needed}")
        lines.append("")

    for topic in user_topics:
        topic_section(topic)
    for unit in units:
        lines.append(f"### {unit.name}: {unit.project}")
        lines.append(f"- phase: {unit.phase}")
        if unit.build_hold:
            lines.append(f"- {BUILD_HOLD_MARK}: {unit.build_hold}")
        if unit.held:
            last = previous.get(unit.unit)
            repeat = report.length == "simple" and last is not None and last.phase == unit.phase and last.held == unit.held
            examples = f", {unit.held_examples}" if unit.held_examples and not repeat else ""
            lines.append(f"- checkpoint: not merged, because {unit.held}{examples}")
        lines.append(f"- update: {unit.update}")
        lines.append(f"- eta: {eta_text(unit, previous.get(unit.unit), now, zone_name, with_note=True)}")
        drift = drift_text(unit, previous.get(unit.unit), now, zone_name)
        if drift:
            lines.append(f"- first eta: {drift}")
        if unit.waiting_on_it:
            lines.append(f"- waiting on it: {unit.waiting_on_it}")
        if unit.needed:
            lines.append(f"- needed: {unit.needed}")
        if unit.then:
            lines.append(f"- then: {unit.then}")
        lines.append("")
    for topic in other_topics:
        topic_section(topic)

    rows: list[Row] = []
    for unit in units:
        on_hold = unit.build_hold is not None
        plan = plan_progress(unit)
        if unit.eta.time is None:
            rows.append(Row(unit.label, None, on_hold, plan))
            continue
        moment = parse_time(unit.eta.time, now)
        earliest = parse_range_end(unit.eta.earliest, now, moment, earliest=True) if unit.eta.earliest else moment
        latest = parse_range_end(unit.eta.latest, now, moment, earliest=False) if unit.eta.latest else moment
        rows.append(Row(unit.label, Estimate(unit.started, moment, earliest, latest), on_hold, plan))
    lines.extend(["```", *draw(now, rows, CHART_STYLES[report.chart]), "```", ""])

    needed = any(unit.needed for unit in report.units) or any(topic.needed for topic in report.topics)
    tail = "" if needed else " - nothing needed"
    lines.append(f"next run at {report.next_run}{tail}" if report.next_run else f"no run scheduled{tail}")
    return lines


def log_line(report: Report, now: datetime, zone_name: str) -> str:
    parts = [f"{unit.unit} {unit.phase.split(':')[0]} {eta_text(unit, None, now, zone_name, with_note=False)}" for unit in report.units]
    parts.extend(f"{topic.title} {topic.eta}" for topic in report.topics)
    return f"- {now:%H:%M} {zone_name}: dailies ETAs: {'; '.join(parts)}"


def main(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Render the /showrunner:dailies report from its fixed template.")
    _ = parser.add_argument("input", type=Path, nargs="?")
    _ = parser.add_argument("--chart", choices=sorted(CHART_STYLES), help=f"set the chart mode in {CHART_CONF} for every showrunner")
    _ = parser.add_argument("--state", type=Path)
    _ = parser.add_argument("--log", type=Path)
    _ = parser.add_argument("--at")
    options = parser.parse_args(arguments)
    input_path = cast(Path | None, options.input)
    chart = cast(str | None, options.chart)
    if chart is not None:
        write_chart(CHART_CONF, chart)
        print(f"dailies chart: {chart} ({CHART_CONF})", file=sys.stderr)
    if input_path is None:
        if chart is None:
            parser.error("give an input file, --chart, or both")
        return 0
    state_path = cast(Path | None, options.state)
    log_path = cast(Path | None, options.log)
    at = cast(str | None, options.at)
    try:
        report = parse_report(cast(object, json.loads(input_path.read_text())), read_chart(CHART_CONF))
        previous = load_state(state_path)
    except (InputError, json.JSONDecodeError, OSError) as error:
        print(f"dailies_render: {error}", file=sys.stderr)
        return 2
    zone = ZoneInfo(report.zone)
    aware = datetime.fromisoformat(at).replace(tzinfo=zone) if at else datetime.now(zone)
    zone_name = aware.strftime("%Z")
    now = aware.replace(second=0, microsecond=0, tzinfo=None)
    try:
        check_changes(report, previous, now)
    except InputError as error:
        print(f"dailies_render: {error}", file=sys.stderr)
        return 2
    print("\n".join(render(report, previous, now, zone_name)))
    if state_path is not None:
        save_state(state_path, report, now, previous)
    if log_path is not None:
        with log_path.open("a") as log:
            _ = log.write(log_line(report, now, zone_name) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
