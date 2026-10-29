"""The last good 5-hour and weekly reading of each Claude and Codex login, kept between reads.

The usage service refuses many reads (HTTP 429). Every read that succeeds is saved here, so the
accounts table has each account's newest numbers and the time they were read.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import sys
import tempfile
from dataclasses import dataclass
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal, TypedDict, cast

# Package callers and sibling scripts share the same store and claim types.
if __name__ == "usage_store":
    _ = sys.modules.setdefault("scripts.whoami.usage_store", sys.modules[__name__])
elif __name__ == "scripts.whoami.usage_store":
    _ = sys.modules.setdefault("usage_store", sys.modules[__name__])

from scripts.whoami.agent_accounts import AccountSourceUnspecified, ClaudeDirectorySource, CodexHomeSource, KnownUsageLogin, NoWeeklyReadSkip, Quota, Report, StaleTokenSkip, UsageReadOutcome, UsageReadClaim, UsageReadHeld
from scripts.whoami.note_file import canonical_label


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
    read_outcome: UsageReadOutcome
    attempted_at: str
    attempt_outcome: UsageReadOutcome



class UsageReadings(dict[str, StoredUsage]):
    """Last good readings and the login last read from each tool directory."""

    def __init__(self) -> None:
        super().__init__()
        self.directory_logins: dict[str, str] = {}


def directory_key(tool: str, directory: str) -> str:
    """Keep tool identities separate without folding case-sensitive paths."""
    return f"{tool}:{directory}"


def weekly_read_skip(usage: StoredUsage) -> StaleTokenSkip | NoWeeklyReadSkip:
    """Decode the latest attempt's skip reason without inspecting display text."""
    return StaleTokenSkip() if usage.get("weekly_read_skip") == "stale_token" else NoWeeklyReadSkip()


def latest_read_outcome(usage: StoredUsage) -> UsageReadOutcome:
    """Old stores have no failed attempt unless they hold the legacy stale-token flag."""
    return usage.get("read_outcome", UsageReadOutcome.STALE_TOKEN
                     if isinstance(weekly_read_skip(usage), StaleTokenSkip) else UsageReadOutcome.OK)


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


@dataclass(frozen=True)
class RecentTimerUsage:
    """The timer's last reports with their original observation time."""
    reports: list[Report]
    observed_at: datetime


@dataclass(frozen=True)
class TimerUsageDue:
    """The timer must read usage before making a decision."""


class TimerReport(TypedDict):
    tool: str
    login: str
    directory: str
    problem: str
    quota_problem: str
    usage: StoredUsage


def timer_file() -> Path:
    return store_file().with_name("timer-usage.json")


def save_timer_usage(signature: str, reports: list[Report], at: datetime) -> None:
    """Keep usage only. This file must never contain credentials."""
    encoded: list[TimerReport] = []
    for report in reports:
        usage = StoredUsage()
        for quota in report.quotas:
            if quota.label == "Weekly" and (window := _read(quota, at)) is not None:
                usage["weekly"] = window
        if report.five_hour is not None and (window := _read(report.five_hour, at)) is not None:
            usage["five_hour"] = window
        if report.credit_balance is not None:
            usage["credits"] = StoredCredits(balance=report.credit_balance, at=at.isoformat())
        usage["read_outcome"] = report.usage_read_outcome
        directory = (str(report.source.directory) if isinstance(report.source, ClaudeDirectorySource)
                     else str(report.source.home) if isinstance(report.source, CodexHomeSource) else "")
        encoded.append(TimerReport(tool=report.tool, login=report.email or "", directory=directory,
                                   problem=report.problem or "", quota_problem=report.quota_problem or "", usage=usage))
    _replace(timer_file(), json.dumps({"signature": signature, "at": at.isoformat(), "reports": encoded}) + "\n")


def recent_timer_usage(signature: str, now: datetime) -> RecentTimerUsage | TimerUsageDue:
    try:
        decoded = cast(object, json.loads(timer_file().read_text(encoding="utf-8")))
        if not isinstance(decoded, dict):
            return TimerUsageDue()
        saved = cast(dict[str, object], decoded)
        if saved.get("signature") != signature or not isinstance(saved.get("at"), str):
            return TimerUsageDue()
        at = datetime.fromisoformat(cast(str, saved["at"]))
        if at.tzinfo is None or not timedelta(0) <= now - at < timedelta(minutes=10):
            return TimerUsageDue()
        rows = saved.get("reports")
        if not isinstance(rows, list):
            return TimerUsageDue()
        reports: list[Report] = []
        for raw in cast(list[object], rows):
            if not isinstance(raw, dict):
                return TimerUsageDue()
            row = cast(dict[str, object], raw)
            if any(not isinstance(row.get(name), str) for name in
                   ("tool", "login", "directory", "problem", "quota_problem")) or not isinstance(row.get("usage"), dict):
                return TimerUsageDue()
            entry = cast(TimerReport, cast(object, raw))
            source = (CodexHomeSource(Path(entry["directory"])) if entry["tool"] == "Codex" else
                      ClaudeDirectorySource(Path(entry["directory"]))) if entry["directory"] else AccountSourceUnspecified()
            report = Report(entry["tool"], email=entry["login"] or None, source=source,
                            problem=entry["problem"] or None, quota_problem=entry["quota_problem"] or None,
                            usage_read_outcome=UsageReadOutcome(entry["usage"].get("read_outcome", UsageReadOutcome.ERROR)))
            for name in ("weekly", "five_hour"):
                window = _window(entry["usage"].get(name))
                if window is None:
                    if name in entry["usage"]:
                        return TimerUsageDue()
                    continue
                reset = datetime.fromisoformat(window["resets"]) if window["resets"] else None
                if (not math.isfinite(window["left"])
                        or reset is not None and (reset.tzinfo is None or reset <= now)):
                    return TimerUsageDue()
                quota = Quota("Weekly" if name == "weekly" else "5-hour", 100 - window["left"], reset)
                if name == "weekly":
                    report.quotas.append(quota)
                else:
                    report.five_hour = quota
            credits = _credits(entry["usage"].get("credits"))
            report.credit_balance = credits["balance"] if credits is not None else None
            reports.append(report)
        return RecentTimerUsage(reports, at)
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        return TimerUsageDue()


@contextmanager
def read_lock() -> Generator[None]:
    """Guard claims and store writes across readers, including separate threads."""
    path = store_file().with_name("usage.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def claim_read(login: str, now: datetime) -> UsageReadClaim | UsageReadHeld:
    with read_lock():
        stored = load()
        usage = stored.setdefault(key("Claude", login), StoredUsage())
        attempted = usage.get("attempted_at", "")
        try:
            at = datetime.fromisoformat(attempted)
        except ValueError:
            at = None
        if at is not None and at.tzinfo is not None:
            age = (now - at).total_seconds()
            if age < 90 or (age < 600 and usage.get("attempt_outcome") == UsageReadOutcome.RATE_LIMITED_429):
                return UsageReadHeld()
        usage["attempted_at"] = now.isoformat()
        # The claim replaces the previous attempt. A prior 429 cannot extend it.
        _ = usage.pop("attempt_outcome", None)
        write(stored)
    return UsageReadClaim(now)


def finish_read(login: str, claim: UsageReadClaim, report: Report) -> None:
    """Keep the result even when the caller does not save reports."""
    with read_lock():
        stored = load()
        usage = stored.get(key("Claude", login))
        if usage is None or usage.get("attempted_at") != claim.at.isoformat():
            return  # A newer reader owns the login now.
        usage["attempt_outcome"] = report.usage_read_outcome
        _ = merge(stored, [report], claim.at)
        write(stored)


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


def load() -> UsageReadings:
    """Every login's stored readings, keyed by `key`. Empty when the file cannot be read."""
    try:
        decoded = cast(object, json.loads(store_file().read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return UsageReadings()
    if not isinstance(decoded, dict):
        return UsageReadings()
    stored = UsageReadings()
    links = cast(dict[str, object], decoded).get("_directory_logins")
    if isinstance(links, dict):
        stored.directory_logins.update({directory: login for directory, login in
            cast(dict[str, object], links).items() if isinstance(login, str) and login})
    for login, value in cast(dict[str, object], decoded).items():
        if login == "_directory_logins" or not isinstance(value, dict):
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
        if "read_outcome" in held:
            try:
                usage["read_outcome"] = UsageReadOutcome(held["read_outcome"])
            except (ValueError, TypeError):
                usage["read_outcome"] = UsageReadOutcome.ERROR
        attempted = held.get("attempted_at")
        if isinstance(attempted, str):
            usage["attempted_at"] = attempted
        if "attempt_outcome" in held:
            try:
                usage["attempt_outcome"] = UsageReadOutcome(held["attempt_outcome"])
            except (ValueError, TypeError):
                usage["attempt_outcome"] = UsageReadOutcome.ERROR
        # Accept links from the first Phase 75 format, but write only the directory map.
        directory = held.get("read_source_directory") or held.get("weekly_read_skip_directory")
        if isinstance(directory, str):
            tool = "Codex" if login.startswith("codex:") else "Claude"
            _ = stored.directory_logins.setdefault(directory_key(tool, directory), login)
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


def merge(stored: UsageReadings, reports: list[Report], checked_at: datetime) -> bool:
    """Save the latest attempt outcome, preserving last good numbers and times on failure.

    Returns whether anything changed.
    """
    changed = False
    for report in reports:
        if report.usage_read_outcome == UsageReadOutcome.HELD or report.tool not in ("Claude", "Codex"):
            continue
        if report.tool == "Claude" and isinstance(report.usage_read_claim, UsageReadClaim):
            current = stored.get(key("Claude", report.email or ""), StoredUsage())
            try:
                attempted = datetime.fromisoformat(current.get("attempted_at", ""))
            except ValueError:
                attempted = report.usage_read_claim.at
            if attempted.tzinfo is not None and report.usage_read_claim.at < attempted:
                continue
        read_at = (report.usage_read_claim.at if isinstance(report.usage_read_claim, UsageReadClaim)
                   else checked_at)
        directory = (str(report.source.directory) if isinstance(report.source, ClaudeDirectorySource)
                     else str(report.source.home) if isinstance(report.source, CodexHomeSource) else "")
        source_key = directory_key(report.tool, directory)
        login = key(report.tool, report.email) if report.email else ""
        if directory:
            if not login:
                login = stored.directory_logins.get(source_key, "")
                if not login and isinstance(report.last_known_login, KnownUsageLogin):
                    login = key(report.tool, report.last_known_login.login)
            if login and stored.directory_logins.get(source_key) != login:
                stored.directory_logins[source_key] = login
                changed = True
        if not login:
            continue
        usage = stored.setdefault(login, StoredUsage())
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
        weekly = _read(weekly_quota, read_at) if weekly_quota is not None else None
        outcome = (UsageReadOutcome.OK if weekly is not None else UsageReadOutcome.STALE_TOKEN
                   if isinstance(report.weekly_read_skip, StaleTokenSkip) else report.usage_read_outcome)
        if usage.get("read_outcome") != outcome:
            usage["read_outcome"] = outcome
            changed = True
        if weekly is None:
            continue
        usage["weekly"] = weekly
        if report.credit_balance is not None:
            usage["credits"] = StoredCredits(
                balance=report.credit_balance, at=read_at.isoformat(timespec="seconds"),
            )
        five_hour = _read(report.five_hour, read_at) if report.five_hour is not None else None
        if five_hour is not None:
            usage["five_hour"] = five_hour
        elif report.tool == "Claude":
            # A Claude read that holds no 5-hour window found none open: all of it is left.
            usage["five_hour"] = StoredWindow(left=100.0, resets="", at=read_at.isoformat(timespec="seconds"))
        else:
            # Codex lists every window an account has, so this account has no 5-hour window.
            _ = usage.pop("five_hour", None)
        changed = True
    return changed


def write(stored: dict[str, StoredUsage]) -> None:
    """Replace the store. The caller must hold read_lock across load, merge and write."""
    encoded: dict[str, object] = dict(stored)
    if isinstance(stored, UsageReadings) and stored.directory_logins:
        encoded["_directory_logins"] = stored.directory_logins
    _replace(store_file(), json.dumps(encoded, indent=1, sort_keys=True) + "\n")


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
    with read_lock():
        stored = load()
        if merge(stored, reports, checked_at):
            write(stored)
        if logins is not None:
            _replace(label_file(), json.dumps(by_label(stored, logins), indent=1, sort_keys=True) + "\n")
