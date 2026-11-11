"""Shared display cells for the unit and style tables."""
from __future__ import annotations

import json
from pathlib import Path
from typing import NamedTuple, cast

from scripts.lib import conf

NONE = "—"


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

