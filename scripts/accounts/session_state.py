"""Classify a staff member for the accounts board and the units table."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from scripts.production import staff

SessionState = Literal["busy", "idle", "paused", "exited"]
StaffState = Literal["active", "paused", "exited"]


@dataclass(frozen=True)
class LiveActivity:
    status: str


@dataclass(frozen=True)
class NoLiveSession:
    """No live session was observed for this member."""


@dataclass(frozen=True)
class UnrecordedStaff:
    """The observed session has no staff row."""


def staff_state(member: staff.StaffRow | UnrecordedStaff) -> StaffState:
    """Read the member's recorded hold for the style table and session classifier."""
    if isinstance(member, staff.StaffRow):
        if isinstance(member.exited, staff.Exited):
            return "exited"
        if isinstance(member.paused, staff.Paused):
            return "paused"
    return "active"


def classify(member: staff.StaffRow | UnrecordedStaff,
             activity: LiveActivity | NoLiveSession) -> SessionState:
    """Staff exit and pause records take precedence over observed activity."""
    hold = staff_state(member)
    if hold != "active":
        return hold
    if isinstance(activity, NoLiveSession):
        return "exited"
    return "busy" if activity.status == "busy" else "idle"
