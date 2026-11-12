"""Shared display cells for the unit and style tables."""
from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import NamedTuple, cast

from scripts.lib import conf
from scripts.lib.terminal_cells import terminal_width

NONE = "—"


def fitted(headers: Sequence[str], rows: Sequence[Sequence[str]], *,
           cuttable: tuple[int, ...], columns: int) -> tuple[tuple[str, ...], list[tuple[str, ...]]]:
    """Remove columns with only NONE cells, then cut text to fit the terminal width.

    The cuttable indexes refer to headers before column removal.
    """
    kept = [index for index in range(len(headers)) if not rows or any(row[index] != NONE for row in rows)]
    headers = tuple(headers[index] for index in kept)
    table_rows = [tuple(row[index] for index in kept) for row in rows]
    minimums = [max(3, terminal_width(header)) for header in headers]
    positions = {original: index for index, original in enumerate(kept)}
    cut_columns = [positions[index] for index in cuttable if index in positions]
    while True:
        widths = [max([minimum, *(terminal_width(row[index]) for row in table_rows)])
                  for index, minimum in enumerate(minimums)]
        if 1 + sum(width + 3 for width in widths) <= columns - 4:
            break
        column = next((index for index in cut_columns if widths[index] > minimums[index]), None)
        if column is None:
            break
        width = widths[column] - 1
        for index, row in enumerate(table_rows):
            cell = row[column]
            if terminal_width(cell) <= width:
                continue
            used = 0
            end = 0
            for char in cell:
                size = terminal_width(char)
                if used + size > width - 1:
                    break
                used += size
                end += 1
            cell = cell[:end].rstrip() + "…"
            table_rows[index] = (*row[:column], cell, *row[column + 1:])
    return tuple(headers), table_rows


def machine_column(machines: list[str]) -> tuple[str, ...]:
    """Return machine cells only when a row uses a machine other than the configured home machine."""
    if not any(machines):
        return ()
    values = conf.read("build_targets").values()
    homes = [key.removeprefix("kind.") for key, value in values.items()
             if key.startswith("kind.") and value == "local"]
    if len(homes) != 1:
        raise conf.Unusable("build_targets: expected one home machine with kind=local")
    return tuple(machine or NONE for machine in machines) if any(machine and machine != homes[0] for machine in machines) else ()


class OpenStep(NamedTuple):
    label: str
    text: str


class NoOpenStep(NamedTuple):
    pass


def open_step(events: list[dict[str, object]]) -> OpenStep | NoOpenStep:
    """The step a run has open in its open phase: the newest open activity, else the newest open pass."""
    passes: dict[str, OpenStep] = {}
    activities: dict[str, OpenStep] = {}
    for event in events:
        kind = event.get("event_type")
        if kind in ("phase_started", "phase_finished", "run_finished"):
            passes.clear()
            activities.clear()
        elif kind == "pass_started":
            passes[str(event.get("pass_instance_id"))] = OpenStep(
                str(event.get("pass_kind", "")), str(event.get("pass_activity", "")))
        elif kind == "pass_finished":
            _ = passes.pop(str(event.get("pass_instance_id")), None)
        elif kind == "activity_started":
            activities[str(event.get("activity_instance_id"))] = OpenStep(
                str(event.get("activity_label", "")), str(event.get("activity_text", "")))
        elif kind == "activity_finished":
            _ = activities.pop(str(event.get("activity_instance_id")), None)
    for steps in (activities, passes):
        if steps:
            return list(steps.values())[-1]
    return NoOpenStep()


def run_events(path: Path) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return events
    for line in lines:
        try:
            event = cast(object, json.loads(line))
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(cast(dict[str, object], event))
    return events


def escaped(cell: str) -> str:
    return " ".join(cell.split()).replace("|", "\\|")

