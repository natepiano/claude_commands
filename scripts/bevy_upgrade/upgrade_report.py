"""Derive upgrade states and print current pins with the user's recorded words."""
from __future__ import annotations

from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from pins import Pin
from upgrade_record import Entry, Record, on_target

State = Literal["git ready", "release ready", "re-vendor", "port in PR", "no port yet", "not checked", "done"]
STATE_ORDER: tuple[State, ...] = (
    "git ready", "release ready", "re-vendor", "port in PR", "no port yet", "not checked", "done",
)


def state(entry: Entry, target: str) -> State:
    if on_target(entry["bevy"], target):
        return "done"
    return "re-vendor" if entry["patched_bevy"] else "not checked"


def _pin(pin: Pin) -> str:
    if pin["source"] == "git":
        revision = pin["rev"][:7]
        return f"git {pin['ref'] or revision} @ {revision}"
    if pin["source"] == "path":
        return f"path {pin['path']}, {pin['version']}"
    return pin["version"]


def _cell(text: str) -> str:
    return text.replace("|", r"\|").replace("\r\n", "<br>").replace("\n", "<br>")


def _row(name: str, entry: Entry, target: str) -> str:
    dependency = name
    if entry["patched_bevy"]:
        dependency += " (patched Bevy crate)"
    elif entry["via"]:
        dependency += f" (via {', '.join(entry['via'])})"
    pin = _pin(entry["pin"])
    if not entry["patched_bevy"]:
        pin += f" on Bevy {entry['bevy']}"
    current = state(entry, target)
    next_step = "—" if current == "done" else (
        f"re-vendor from Bevy {target}" if current == "re-vendor" else "run check")
    return f"| {_cell(dependency)} | {_cell(pin)} | — | — | {current} | {next_step} |"


def table(record: Record, zone: ZoneInfo, now: datetime) -> str:
    # This phase has no upstream timestamps to print.
    _ = zone, now
    lines = [
        f"Bevy {record['target']} upgrade for {record['slug']}: {record['checkout']}, on Bevy {' and '.join(record['bevy'])}.",
        "Upstream: not checked yet.",
        "",
        "| Dependency | Our pin | crates.io | Git | State | Next |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    dependencies = record["dependencies"]
    for name in sorted(dependencies, key=lambda name: (STATE_ORDER.index(state(dependencies[name], record["target"])), name)):
        lines.append(_row(name, dependencies[name], record["target"]))
    answers: list[str] = []
    notes: list[str] = []
    for name in sorted(dependencies):
        entry = dependencies[name]
        answer = entry["answer"]
        if answer is not None:
            answers.append(f"- {name}: {answer['answer']} {answer['at']}: \"{answer['words']}\"")
        if entry["note"]:
            notes.append(f"- {name}: {entry['note']}")
    if answers:
        lines.extend(["", "Answers on git pins:", *answers])
    if notes:
        lines.extend(["", "Notes:", *notes])
    return "\n".join(lines)
