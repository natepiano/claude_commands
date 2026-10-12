"""The last good 5-hour and weekly reading of each Claude login, kept between reads.

The usage service refuses many reads (HTTP 429). Every read that succeeds is saved here, so the
accounts table has each account's newest numbers and the time they were read.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import TypedDict, cast

from agent_accounts import Quota, Report


class StoredWindow(TypedDict):
    # Percent of the window left.
    left: float
    # When the window resets, ISO 8601 with an offset. Empty when no window is open.
    resets: str
    # When this was read, ISO 8601 with an offset.
    at: str


class StoredUsage(TypedDict, total=False):
    five_hour: StoredWindow
    weekly: StoredWindow


def store_file() -> Path:
    return Path.home() / ".local" / "state" / "agent-notes" / "usage.json"


def _window(value: object) -> StoredWindow | None:
    if not isinstance(value, dict):
        return None
    held = cast(dict[str, object], value)
    left, resets, at = held.get("left"), held.get("resets"), held.get("at")
    if isinstance(left, bool) or not isinstance(left, (int, float)) or not isinstance(resets, str) or not isinstance(at, str):
        return None
    return StoredWindow(left=float(left), resets=resets, at=at)


def load() -> dict[str, StoredUsage]:
    """Every login's stored windows, keyed by the login in lower case. Empty when the file cannot be read."""
    try:
        decoded = cast(object, json.loads(store_file().read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {}
    if not isinstance(decoded, dict):
        return {}
    stored: dict[str, StoredUsage] = {}
    for login, value in cast(dict[str, object], decoded).items():
        if not isinstance(value, dict):
            continue
        held = cast(dict[str, object], value)
        usage = StoredUsage()
        if (five_hour := _window(held.get("five_hour"))) is not None:
            usage["five_hour"] = five_hour
        if (weekly := _window(held.get("weekly"))) is not None:
            usage["weekly"] = weekly
        stored[login] = usage
    return stored


def _read(quota: Quota, checked_at: datetime) -> StoredWindow | None:
    left = quota.remaining_percent
    if left is None:
        return None
    return StoredWindow(
        left=left,
        resets=quota.resets_at.isoformat() if quota.resets_at is not None else "",
        at=checked_at.isoformat(timespec="seconds"),
    )


def merge(stored: dict[str, StoredUsage], reports: list[Report], checked_at: datetime) -> bool:
    """Put each window the Claude reports hold into `stored`. A refused read changes nothing.

    Returns whether anything changed.
    """
    changed = False
    for report in reports:
        weekly_quota = next((quota for quota in report.quotas if quota.label == "Weekly"), None)
        weekly = _read(weekly_quota, checked_at) if weekly_quota is not None else None
        if report.tool != "Claude" or not report.email or weekly is None:
            continue
        usage = stored.setdefault(report.email.casefold(), StoredUsage())
        usage["weekly"] = weekly
        # A read that succeeded and holds no 5-hour window found none open: all of it is left.
        usage["five_hour"] = (
            (_read(report.five_hour, checked_at) if report.five_hour is not None else None)
            or StoredWindow(left=100.0, resets="", at=checked_at.isoformat(timespec="seconds"))
        )
        changed = True
    return changed


def write(stored: dict[str, StoredUsage]) -> None:
    """Replace the store in one step, readable by this user only."""
    path = store_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            _ = output.write(json.dumps(stored, indent=1, sort_keys=True) + "\n")
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def save(reports: list[Report], checked_at: datetime) -> None:
    """Keep every window `reports` hold beside the ones already stored."""
    stored = load()
    if merge(stored, reports, checked_at):
        write(stored)
