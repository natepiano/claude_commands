"""Shared cells for the production tables and board snapshot."""
from __future__ import annotations

import json
from pathlib import Path
from typing import NamedTuple, cast

import live_units
import unit_lookup

NONE = "—"


class OpenStep(NamedTuple):
    label: str
    text: str


class NoOpenStep(NamedTuple):
    pass


def session_cell(unit: live_units.LiveUnit) -> str:
    """The unit's session name now, or why there is none."""
    if unit.session is None:
        return "gone"
    claude = unit.session.claude
    if isinstance(claude, unit_lookup.LiveClaude):
        return claude.name
    if isinstance(claude, unit_lookup.ClaudeUnknown):
        return "unknown"
    return "not running"


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

