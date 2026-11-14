"""Read open waits in a phase instance for the Units report."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import NamedTuple, cast


class NamedWaitHolder(NamedTuple):
    """The holder named in a wait record."""

    label: str


class UnnamedWaitHolder(NamedTuple):
    """The wait record names no holder."""


class PhaseWait(NamedTuple):
    """An open worker or director wait in one phase instance."""

    reason: str
    started: float
    holder: NamedWaitHolder | UnnamedWaitHolder


def _wait_holder(value: object) -> NamedWaitHolder | UnnamedWaitHolder:
    if isinstance(value, dict):
        fields = cast(dict[str, object], value)
        holder = fields.get("holder")
        if fields.get("state") == "NamedHolder" and isinstance(holder, dict):
            holder = cast(dict[str, object], holder)
            label = holder.get("unit") or holder.get("session_id")
            if isinstance(label, str) and label:
                return NamedWaitHolder(label)
    return UnnamedWaitHolder()


def phase_waits(events: Sequence[Mapping[str, object]], instance_id: str) -> list[PhaseWait]:
    """Read waits that remain open in one phase instance."""
    waits: dict[str, Mapping[str, object]] = {}
    for event in events:
        kind = event.get("event_type")
        wait_id = str(event.get("wait_id", ""))
        if kind == "run_finished" or kind == "phase_finished" and event.get("phase_instance_id") == instance_id:
            waits.clear()
        elif kind == "wait_started" and event.get("phase_instance_id") == instance_id:
            waits[wait_id] = event
        elif kind in ("wait_finished", "wait_unclosed"):
            _ = waits.pop(wait_id, None)
        elif kind == "wait_holder_seen" and wait_id in waits:
            waits[wait_id] = {**waits[wait_id], "holder": event.get("holder")}
        elif kind == "pass_finished":
            waits = {key: wait for key, wait in waits.items()
                     if wait.get("pass_instance_id") != event.get("pass_instance_id")}
    result: list[PhaseWait] = []
    for wait in waits.values():
        started = wait.get("wait_started_epoch", wait.get("timestamp_epoch"))
        if isinstance(started, int | float):
            result.append(PhaseWait(str(wait.get("wait_reason") or wait.get("wait_kind") or "wait"),
                                    float(started), _wait_holder(wait.get("holder"))))
    return result
