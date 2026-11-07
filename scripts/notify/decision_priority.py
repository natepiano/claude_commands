"""Select phone priority from the need and an explicit decision deadline."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING
from dataclasses import dataclass
from datetime import datetime, timezone

if TYPE_CHECKING:
    from ..lib import conf
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
    from scripts.lib import conf


@dataclass(frozen=True)
class NoDecisionDue:
    """The sender supplied no decision deadline."""


@dataclass(frozen=True)
class DecisionDue:
    """A decision deadline has an explicit UTC offset."""
    at: datetime


def read_due(text: str) -> DecisionDue | NoDecisionDue:
    if not text:
        return NoDecisionDue()
    try:
        at = datetime.fromisoformat(text)
    except ValueError as error:
        raise ValueError("--due requires ISO 8601 with an explicit offset") from error
    if at.utcoffset() is None:
        raise ValueError("--due requires ISO 8601 with an explicit offset")
    return DecisionDue(at)


def priority(need: str, due: str = "", *, now: datetime | None = None) -> str:
    """A decision rings High only before its deadline within the configured window."""
    deadline = read_due(due)
    if need == "blocked":
        return "2"
    if need != "decision" or isinstance(deadline, NoDecisionDue):
        return "0"
    remaining = (deadline.at - (now or datetime.now(timezone.utc))).total_seconds()
    if remaining <= 0:
        print(f"decision priority: overdue deadline {due}; priority 0", file=sys.stderr)
        return "0"
    return "1" if remaining <= conf.read("send").number("decision_high_priority_window_seconds") else "0"
