"""The last good 5-hour and weekly reading of each Claude and Codex login, kept between reads.

The usage service refuses many reads (HTTP 429). Every read that succeeds is saved here, so the
accounts table has each account's newest numbers and the time they were read.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Literal, TypedDict, cast

from agent_accounts import ClaudeDirectorySource, NoWeeklyReadSkip, Quota, Report, StaleTokenSkip
from note_file import canonical_label


class StoredWindow(TypedDict):
    # Percent of the window left.
    left: float
    # When the window resets, ISO 8601 with an offset. Empty when no window is open.
    resets: str
    # When this was read, ISO 8601 with an offset.
    at: str


class StoredCredits(TypedDict):
    # As `Report.credit_balance` holds it: whole credits, "unlimited", or "null" when none is reported.
    balance: str
    # When this was read, ISO 8601 with an offset.
    at: str


class StoredUsage(TypedDict, total=False):
    # Absent beside a weekly window when the account has no 5-hour window.
    five_hour: StoredWindow
    weekly: StoredWindow
    # Codex only: the credits that carry work once an allowance is used up.
    credits: StoredCredits
    # The latest attempt skipped this login; successful reading timestamps stay unchanged.
    weekly_read_skip: Literal["stale_token"]
    # Identifies a later failed attempt even when authentication returns no login.
    weekly_read_skip_directory: str


def weekly_read_skip(usage: StoredUsage) -> StaleTokenSkip | NoWeeklyReadSkip:
    """Decode the latest attempt's skip reason without inspecting display text."""
    return StaleTokenSkip() if usage.get("weekly_read_skip") == "stale_token" else NoWeeklyReadSkip()


def key(tool: str, login: str) -> str:
    """Where a login's readings are kept: a Claude login as it is, a Codex login behind `codex:`.

    One address can hold an account with each tool.
    """
    folded = login.casefold()
    return f"codex:{folded}" if tool == "Codex" else folded


class LabelWindow(TypedDict):
    # Percent of the window left.
    left: float
    # When the window resets, epoch seconds; None when no window is open.
    resets_at: int | None
    # When this was read, epoch seconds.
    at: int


class LabelUsage(TypedDict, total=False):
    five_hour: LabelWindow
    weekly: LabelWindow


def store_file() -> Path:
    return Path.home() / ".local" / "state" / "agent-notes" / "usage.json"


def label_file() -> Path:
    """Each Claude account's readings under its note label, for the status line.

    The status line knows its session's label (CLAUDE_CONFIG_DIR/account-label)
    and not its login, and runs too often to look one up. It holds no login.
    """
    return store_file().with_name("by-label.json")


def _window(value: object) -> StoredWindow | None:
    if not isinstance(value, dict):
        return None
    held = cast(dict[str, object], value)
    left, resets, at = held.get("left"), held.get("resets"), held.get("at")
    if isinstance(left, bool) or not isinstance(left, (int, float)) or not isinstance(resets, str) or not isinstance(at, str):
        return None
    return StoredWindow(left=float(left), resets=resets, at=at)


def _credits(value: object) -> StoredCredits | None:
    if not isinstance(value, dict):
        return None
    held = cast(dict[str, object], value)
    balance, at = held.get("balance"), held.get("at")
    if not isinstance(balance, str) or not isinstance(at, str):
        return None
    return StoredCredits(balance=balance, at=at)


def load() -> dict[str, StoredUsage]:
    """Every login's stored readings, keyed by `key`. Empty when the file cannot be read."""
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
        if (credits := _credits(held.get("credits"))) is not None:
            usage["credits"] = credits
        if held.get("weekly_read_skip") == "stale_token":
            usage["weekly_read_skip"] = "stale_token"
            directory = held.get("weekly_read_skip_directory")
            if isinstance(directory, str):
                usage["weekly_read_skip_directory"] = directory
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
    """Save readings and the latest typed skip reason, preserving last good numbers on failure.

    Returns whether anything changed.
    """
    changed = False
    for report in reports:
        if report.tool == "Claude" and isinstance(report.source, ClaudeDirectorySource):
            directory = str(report.source.directory)
            for login, held in stored.items():
                if (held.get("weekly_read_skip_directory") == directory
                        and (not report.email or login != key(report.tool, report.email))):
                    _ = held.pop("weekly_read_skip", None)
                    _ = held.pop("weekly_read_skip_directory", None)
                    changed = True
        if report.tool not in ("Claude", "Codex") or not report.email:
            continue
        login = key(report.tool, report.email)
        if isinstance(report.weekly_read_skip, StaleTokenSkip):
            usage = stored.setdefault(login, StoredUsage())
            if "weekly_read_skip" not in usage:
                usage["weekly_read_skip"] = "stale_token"
                changed = True
            if isinstance(report.source, ClaudeDirectorySource):
                directory = str(report.source.directory)
                if usage.get("weekly_read_skip_directory") != directory:
                    usage["weekly_read_skip_directory"] = directory
                    changed = True
        elif login in stored and "weekly_read_skip" in stored[login]:
            del stored[login]["weekly_read_skip"]
            _ = stored[login].pop("weekly_read_skip_directory", None)
            changed = True
        weekly_quota = next((quota for quota in report.quotas if quota.label == "Weekly"), None)
        weekly = _read(weekly_quota, checked_at) if weekly_quota is not None else None
        if weekly is None:
            continue
        usage = stored.setdefault(login, StoredUsage())
        usage["weekly"] = weekly
        if report.credit_balance is not None:
            usage["credits"] = StoredCredits(
                balance=report.credit_balance, at=checked_at.isoformat(timespec="seconds"),
            )
        five_hour = _read(report.five_hour, checked_at) if report.five_hour is not None else None
        if five_hour is not None:
            usage["five_hour"] = five_hour
        elif report.tool == "Claude":
            # A Claude read that holds no 5-hour window found none open: all of it is left.
            usage["five_hour"] = StoredWindow(left=100.0, resets="", at=checked_at.isoformat(timespec="seconds"))
        else:
            # Codex lists every window an account has, so this account has no 5-hour window.
            _ = usage.pop("five_hour", None)
        changed = True
    return changed


def write(stored: dict[str, StoredUsage]) -> None:
    """Replace the store in one step, readable by this user only."""
    _replace(store_file(), json.dumps(stored, indent=1, sort_keys=True) + "\n")


def _label_window(window: StoredWindow) -> LabelWindow | None:
    try:
        at = int(datetime.fromisoformat(window["at"]).timestamp())
        resets_at = int(datetime.fromisoformat(window["resets"]).timestamp()) if window["resets"] else None
    except ValueError:
        return None
    return LabelWindow(left=window["left"], resets_at=resets_at, at=at)


def by_label(stored: dict[str, StoredUsage], logins: dict[str, str]) -> dict[str, LabelUsage]:
    """The stored Claude readings of each label in `logins` (label to login).

    A label that is itself a login is left out, so no address reaches the file.
    """
    labelled: dict[str, LabelUsage] = {}
    for label, login in sorted(logins.items()):
        usage = stored.get(key("Claude", login))
        if usage is None or "@" in label:
            continue
        entry = LabelUsage()
        if "five_hour" in usage and (five_hour := _label_window(usage["five_hour"])) is not None:
            entry["five_hour"] = five_hour
        if "weekly" in usage and (weekly := _label_window(usage["weekly"])) is not None:
            entry["weekly"] = weekly
        if entry:
            labelled[canonical_label(label)] = entry
    return labelled


def _replace(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            _ = output.write(text)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def save(reports: list[Report], checked_at: datetime, logins: dict[str, str] | None = None) -> None:
    """Keep every reading `reports` hold beside the ones already stored.

    With `logins` (Claude note label to login), also rewrite the by-label file
    from the whole store, so a label keeps its last good reading when this read
    was refused; the status line judges its age.
    """
    stored = load()
    if merge(stored, reports, checked_at):
        write(stored)
    if logins is not None:
        _replace(label_file(), json.dumps(by_label(stored, logins), indent=1, sort_keys=True) + "\n")
