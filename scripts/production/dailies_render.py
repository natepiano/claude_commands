#!/usr/bin/env python3
"""Render the /showrunner:dailies report from its fixed template.

Usage: dailies_render.py <input.json> [--state <state.json>] [--log <log.md>] [--at <YYYY-MM-DDTHH:MM>]

The input gives each subject's fields; this script owns the layout, so no line
of the template can be dropped or renamed. It refuses, with exit 2, an input
that breaks a template rule: a unit without its phase or `held`, a follow-up
or last phase without `then`, an unknown field, an update too long for the
length, an update or held reason that names another phase without saying
why, a held reason carrying its own examples, a held count the update does
not report against (`<k> of <N>`), or an ETA time without its percent.

--state  JSON file holding each unit's last reported phase, ETA and held
         reason. The script reads it to write `(unchanged)` / `(changed:
         ±h:mm)` and to print a held reason's examples only the first time,
         then saves this report's values to it.
--log    appends the `dailies ETAs:` line to this file.
--at     renders as if the clock read this local time (for checks).

The input format is in ~/.claude/commands/showrunner/dailies.md.
"""

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

LENGTHS = {"simple": 240, "page": 480, "elaborate": None}
PHASE = re.compile(r"^(?:Phase (\d+) of (\d+)|follow-up (\d+) of (\d+)): \S")
TIME = re.compile(r"^\d{1,2}:\d{2}(?:\+\d+)?$")
NONE = ("none measured - requested", "none measured", "no ETA stated yet")
LABEL_LIMIT = 8
RETURN = re.compile(r"\bthe plan at Phase \d+|\bplan done\b")
PHASE_MENTION = re.compile(r"\bPhases? (\d+(?:\s*(?:,|and|-|–|to)\s*\d+)*)|\bP(\d+)\b")
COUNT = re.compile(r"\b(\d+) [a-z]")
EXAMPLES = re.compile(r"\bsuch as\b|\be\.g\.|\bfor example\b")
STARTED = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")
WHITE = "⬜"
GREEN = "🟩"
RED = "🟥"
BLANK = "  "
NOW_MARK = "▼ "
CELL_WIDTH = 2
ROW_LABEL_WIDTH = 9
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


@dataclass(frozen=True)
class Unit:
    unit: str
    label: str
    phase: str
    started: datetime
    held: str | None
    held_examples: str | None
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
    zone: str
    next_run: str | None
    units: list[Unit]
    topics: list[Topic]


@dataclass(frozen=True)
class Previous:
    phase: str
    eta: datetime | None
    held: str | None


@dataclass(frozen=True)
class Estimate:
    started: datetime
    eta: datetime
    earliest: datetime
    latest: datetime


@dataclass(frozen=True)
class Row:
    name: str
    estimate: Estimate | None


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


def window_start(now: datetime) -> datetime:
    mark = now.replace(hour=now.hour - now.hour % LABEL_EVERY_HOURS, minute=0, second=0, microsecond=0)
    return mark - timedelta(hours=HOURS_BEFORE)


def draw(now: datetime, rows: list[Row]) -> list[str]:
    """24 hourly cells: white from the phase's start (or the left edge) to the ETA, green at the earliest time, red at the latest; `→` past the right edge."""
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
    lines = [" " * ROW_LABEL_WIDTH + "".join(axis).rstrip()]

    last = WINDOW_HOURS - 1
    for row in rows:
        if row.estimate is None:
            lines.append(f"{row.name:<{ROW_LABEL_WIDTH}}?")
            continue
        estimate = row.estimate
        cells = [BLANK] * WINDOW_HOURS
        first = max(0, column(estimate.started))
        for index in range(first, min(column(estimate.eta), last) + 1):
            cells[index] = WHITE
        if column(estimate.earliest) <= last:
            cells[max(0, column(estimate.earliest))] = GREEN
        cells[max(0, min(column(estimate.latest), last))] = RED
        arrow = "→" if column(estimate.latest) > last else ""
        span = f"{estimate.eta:%H:%M}"
        if (estimate.earliest, estimate.latest) != (estimate.eta, estimate.eta):
            span += f" ({estimate.earliest:%H:%M}–{estimate.latest:%H:%M})"
        lines.append(f"{row.name:<{ROW_LABEL_WIDTH}}{''.join(cells).rstrip()}{arrow} {span}")
    return lines


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
    check_keys(fields, {"time", "earliest", "latest", "none", "detail", "percent"}, where)
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
    return Eta(time, earliest, latest, none, optional_text(fields, "detail", where), percent)


def check_update(update: str, length: str, where: str, key: str = "update") -> None:
    limit = LENGTHS[length]
    if limit is not None and len(update) > limit:
        raise InputError(f"{where}.{key}: {len(update)} characters; a {length} {key} is one short line, at most {limit}")


def other_phases(line: str, number: int | None) -> list[int]:
    """Phase numbers `line` names other than the heading's (`None` on a follow-up)."""
    named: list[int] = []
    for match in PHASE_MENTION.finditer(line):
        mention: str = match.group(1) or match.group(2)
        named.extend(int(digits.group(0)) for digits in re.finditer(r"\d+", mention))
    return [found for found in named if found != number]


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
        {"unit", "label", "phase", "started", "held", "held_examples", "update", "eta", "waiting_on_it", "needed", "needs_user", "then"},
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
        label=label,
        phase=phase,
        started=datetime.fromisoformat(started_text),
        held=held,
        held_examples=held_examples,
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


def parse_report(value: object) -> Report:
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
    return Report(length, zone, next_run, units, topics)


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
        previous[unit] = Previous(phase, datetime.fromisoformat(eta) if eta else None, held)
    return previous


def save_state(path: Path, report: Report, now: datetime) -> None:
    state: dict[str, dict[str, str | None]] = {}
    for unit in report.units:
        moment = parse_time(unit.eta.time, now) if unit.eta.time else None
        state[unit.unit] = {"phase": unit.phase, "eta": moment.isoformat() if moment else None, "held": unit.held}
    _ = path.write_text(json.dumps(state, indent=2) + "\n")


def clock(moment: datetime, now: datetime, zone_name: str) -> str:
    days = (moment.date() - now.date()).days
    base = f"{moment:%H:%M} {zone_name}"
    if days == 0:
        return base
    if days == 1:
        return f"{base} tomorrow"
    return f"{moment:%a} {base}"


def change_note(moment: datetime, previous: Previous | None, phase: str, now: datetime) -> str | None:
    if previous is None or previous.phase != phase or previous.eta is None:
        return None
    minutes = round((moment - previous.eta).total_seconds() / 60)
    if minutes == 0:
        return "unchanged, overdue" if moment < now else "unchanged"
    hours, rest = divmod(abs(minutes), 60)
    return f"changed: {'+' if minutes > 0 else '-'}{hours}:{rest:02d}"


def eta_text(unit: Unit, previous: Previous | None, now: datetime, zone_name: str, with_note: bool) -> str:
    eta = unit.eta
    if eta.time is None:
        words = eta.none or ""
    else:
        moment = parse_time(eta.time, now)
        notes: list[str] = []
        note = change_note(moment, previous, unit.phase, now) if with_note else None
        if note:
            notes.append(note)
        if eta.earliest and eta.latest:
            notes.append(f"range {parse_time(eta.earliest, now):%H:%M}–{parse_time(eta.latest, now):%H:%M}")
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


def render(report: Report, previous: dict[str, Previous], now: datetime, zone_name: str, utc_now: datetime) -> list[str]:
    lines = [f"**Dailies ({report.length.capitalize()})**, {now:%H:%M} {zone_name} / {utc_now:%H:%M} UTC", ""]
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
        lines.append(f"### {unit.unit}, {unit.phase}")
        if unit.held:
            last = previous.get(unit.unit)
            repeat = last is not None and last.phase == unit.phase and last.held == unit.held
            examples = f", {unit.held_examples}" if unit.held_examples and not repeat else ""
            lines.append(f"- held: not merged, because {unit.held}{examples}")
        lines.append(f"- update: {unit.update}")
        lines.append(f"- eta: {eta_text(unit, previous.get(unit.unit), now, zone_name, with_note=True)}")
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
        if unit.eta.time is None:
            rows.append(Row(unit.label, None))
            continue
        moment = parse_time(unit.eta.time, now)
        earliest = parse_time(unit.eta.earliest, now) if unit.eta.earliest else moment
        latest = parse_time(unit.eta.latest, now) if unit.eta.latest else moment
        rows.append(Row(unit.label, Estimate(unit.started, moment, earliest, latest)))
    lines.extend(["```", *draw(now, rows), "```", ""])

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
    _ = parser.add_argument("input", type=Path)
    _ = parser.add_argument("--state", type=Path)
    _ = parser.add_argument("--log", type=Path)
    _ = parser.add_argument("--at")
    options = parser.parse_args(arguments)
    input_path = cast(Path, options.input)
    state_path = cast(Path | None, options.state)
    log_path = cast(Path | None, options.log)
    at = cast(str | None, options.at)
    try:
        report = parse_report(cast(object, json.loads(input_path.read_text())))
        previous = load_state(state_path)
    except (InputError, json.JSONDecodeError, OSError) as error:
        print(f"dailies_render: {error}", file=sys.stderr)
        return 2
    zone = ZoneInfo(report.zone)
    aware = datetime.fromisoformat(at).replace(tzinfo=zone) if at else datetime.now(zone)
    zone_name = aware.strftime("%Z")
    now = aware.replace(second=0, microsecond=0, tzinfo=None)
    utc_now = aware.astimezone(ZoneInfo("UTC"))
    print("\n".join(render(report, previous, now, zone_name, utc_now)))
    if state_path is not None:
        save_state(state_path, report, now)
    if log_path is not None:
        with log_path.open("a") as log:
            _ = log.write(log_line(report, now, zone_name) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
