#!/usr/bin/env python3
"""Update per-account agent notes using the same live reports as /whoami.

Match by tool and login. Preserve inactive accounts' last observed values until
that quota window expires; unknown or expired usage is YAML null. Never infer
100% remaining from a reset. weekly_usage_checked_at records the observation in
UTC so a saved reading is distinguishable from live usage. No credentials are
saved. Other frontmatter and note bodies are preserved. Run as a script, it then
hands the notes to quota_alert.py, which records routing when an account runs
low, and to codex_pacer.py, which decides the `pace` tier from them; `refresh`
(/quota_refresh) is the run after the user reports a usage reset.
`usage-only` reads and saves usage, then starts `renew-logins` when renewal is due.
`renew-logins` renews expired local Claude logins under its own schedule lock.
It sends no quota alerts and runs neither account moves nor the Codex pacer.
`blocked` records a launch that Codex refused for quota: it reads the notes
as the timer left them and hands them to quota_alert.blocked().
"""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import asyncio
import json
import hashlib
import math
import re
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NotRequired, TypedDict, cast


from scripts.whoami import codex_pacer
from scripts.whoami import five_hour
from scripts.whoami import note_file
from scripts.whoami import session_table
from scripts.whoami.session_table import SessionsSection, KeepSections, KEEP_SECTIONS
from scripts.whoami.note_file import FIELD, Note, reset_time
from scripts.accounts import next_moves, refusals, role_map
from scripts.accounts.role_map import role_map_command
from scripts.whoami.agent_accounts import KnownCodexAccount, UnknownCodexAccount, codex_account
from scripts.whoami.agent_accounts import (ClaudeAccountsInUse, CodexHomeSource, EASTERN, Report, UsageReadOutcome,
                            ScheduledUsageReads, UnscheduledUsageReads, UNSCHEDULED_USAGE_READS,
                            claude_on_disk, codex_email_on_disk, codex_home_display, codex_homes,
                            codex_report_in_use, live_reports, observe_workers_home as resolve_workers_home, NoWorkersHome)
from scripts.whoami.quota_alert import (AgentNote, alert, blocked, current_session, refresh, CodexAlertPassReadings,
                         codex_alert_pass, RefusedAt, RefusalTimeUnknown,
                         StoppedWorker, StoppedWorkerUnknown)
from scripts.whoami.run_out import READINGS_LOG
from scripts.whoami import usage_store
from scripts.maintenance.schedule import ScheduleBusy, locked_history, schedule_directory
from scripts.maintenance.settings import positive, usage_only_interval
from scripts.lib.user_zone import user_zone


# Expand at refresh time so callers can use a scratch home.
SESSIONS_SEEN = Path("~/.local/state/agent-notes/sessions_seen.json")
# Read low accounts every two minutes so exhaustion moves keep their cadence.
LIVE_USAGE_REMAINING_THRESHOLD = 10


def read_sessions_seen() -> dict[str, str]:
    """Read valid machine observations; missing or damaged state never blocks notes."""
    try:
        data = cast(object, json.loads(SESSIONS_SEEN.expanduser().read_text(encoding="utf-8")))
    except (OSError, UnicodeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    seen: dict[str, str] = {}
    for machine, value in cast(dict[str, object], data).items():
        if not isinstance(value, str):
            continue
        try:
            at = datetime.fromisoformat(value)
        except ValueError:
            continue
        if at.utcoffset() is not None:
            seen[machine] = value
    return seen


def write_sessions_seen(seen: dict[str, str]) -> None:
    """Replace the machine observations once, outside the vault."""
    path = SESSIONS_SEEN.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    output = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".sessions-seen-", delete=False)
    temporary = Path(output.name)
    try:
        with output:
            _ = output.write(json.dumps(seen, sort_keys=True) + "\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class StoredReading(TypedDict):
    account: str
    at: str
    remaining: float
    credits: NotRequired[float]


@dataclass(frozen=True)
class QuotaUpdates:
    """One report's note updates and fresh observation log entries, before writing."""
    updates: dict[str, dict[str, str]]
    messages: tuple[str, ...]
    readings: tuple[StoredReading, ...]


NO_REMOVED_FIELDS: frozenset[str] = frozenset()


def rename_legacy_notes(directory: Path) -> list[str]:
    messages: list[str] = []
    for old in sorted(directory.glob("*.md")):
        parsed = note_file.parse_account_label(old.stem)
        if not isinstance(parsed, note_file.NumberedAccountLabel) or old.stem != f"{parsed.tool} {parsed.number}":
            continue
        new = directory / note_file.note_name(parsed.label)
        try:
            os.link(old, new)
            os.unlink(old)
        except FileExistsError:
            messages.append(f"agents: {old.name} and {new.name} both exist; reading {new.name}")
        except OSError as error:
            messages.append(f"agents: could not rename {old.name}: {error}")
        else:
            messages.append(f"agents: renamed {old.name} to {new.name}")
    return messages


def local_reset(value: datetime) -> str:
    """Local wall time rounded to the minute, with its offset: the APIs jitter by fractions of a
    second, and the note travels to machines in other zones."""
    local = value.astimezone()
    rounded = (local + timedelta(seconds=30)).replace(second=0, microsecond=0)
    return rounded.isoformat(timespec="seconds")


def set_fields(note: Note, updates: dict[str, str], stamp: dict[str, str]) -> bool:
    return write_note(note, dict(updates), stamp=stamp)


def _rewrite(lines: list[str], updates: dict[str, str | tuple[str, ...]],
             remove: frozenset[str] = NO_REMOVED_FIELDS) -> bool:
    before = list(lines)
    for key in (*remove, *updates):
        index = next((i for i, line in enumerate(lines)
                      if (match := FIELD.match(line)) and match.group(1) == key), None)
        if index is not None:
            end = index + 1
            while end < len(lines) and (lines[end].startswith((" ", "\t", "- "))):
                end += 1
            if key in remove:
                del lines[index:end]
                continue
        else:
            index = next((i for i, line in enumerate(lines)
                          if (match := FIELD.match(line)) and match.group(1) > key), len(lines))
            end = index
        if key not in updates:
            continue
        value = updates[key]
        rendered = ([f"{key}:"] + [f"  - {item}" for item in value] if value
                    else [f"{key}: []"]) if isinstance(value, tuple) else [f"{key}: {value}"]
        lines[index:end] = rendered
    return lines != before


def write_note(note: Note, updates: dict[str, str | tuple[str, ...]], *,
               remove: frozenset[str] = NO_REMOVED_FIELDS,
               section: SessionsSection | KeepSections = KEEP_SECTIONS,
               stamp: dict[str, str]) -> bool:
    """Replace frontmatter and the owned section atomically, only when changed."""
    lines = note.lines()
    _ = _rewrite(lines, updates, remove)
    # Keep the closing delimiter separate so body replacement never touches frontmatter.
    suffix = note.text[note.end:]
    delimiter_end = suffix.find("\n", 1) + 1
    if delimiter_end == 0:
        delimiter_end = len(suffix)
    delimiter, body = suffix[:delimiter_end], suffix[delimiter_end:]
    if isinstance(section, SessionsSection):
        body = session_table.replace_section(body, section.section)
    else:
        body = re.sub(r"^## Live sessions[ \t]*$", "## Session history", body, flags=re.MULTILINE)
    text = note.text[:note.start] + "\n".join(lines) + delimiter + body
    if text == note.text:
        return False
    _ = _rewrite(lines, dict(stamp))
    front = "\n".join(lines)
    text = note.text[:note.start] + front + delimiter + body
    tmp = note.path.with_name(f".{note.path.name}.tmp")
    try:
        _ = tmp.write_text(text, encoding="utf-8")
        os.chmod(tmp, note.path.stat().st_mode & 0o777)
        os.replace(tmp, note.path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
    note.text = text
    note.end = note.start + len(front)
    return True


def still_ahead(value: str | None) -> bool:
    reset = reset_time(value)
    return isinstance(reset, note_file.KnownReset) and reset.value > datetime.now(timezone.utc)


def append_readings(readings: list[StoredReading], checked_at: datetime) -> None:
    """Keep the recent readings and replace the log in one write."""
    READINGS_LOG.parent.mkdir(parents=True, exist_ok=True)
    cutoff = checked_at.astimezone(timezone.utc) - timedelta(days=8)
    kept: list[StoredReading] = []
    try:
        with READINGS_LOG.open(encoding="utf-8") as existing:
            for line in existing:
                try:
                    entry = cast(object, json.loads(line))
                    if not isinstance(entry, dict):
                        continue
                    values = cast(dict[str, object], cast(object, entry))
                    account = values.get("account")
                    at_text = values.get("at")
                    remaining = values.get("remaining")
                    if (
                        not isinstance(account, str)
                        or not isinstance(at_text, str)
                        or isinstance(remaining, bool)
                        or not isinstance(remaining, (int, float))
                    ):
                        continue
                    at = datetime.fromisoformat(at_text)
                    if at.tzinfo is not None and at.astimezone(timezone.utc) >= cutoff:
                        record: StoredReading = {
                            "account": account,
                            "at": at_text,
                            "remaining": float(remaining),
                        }
                        credits = values.get("credits")
                        if not isinstance(credits, bool) and isinstance(credits, (int, float)):
                            record["credits"] = float(credits)
                        kept.append(record)
                except (ValueError, TypeError, KeyError):
                    continue
    except FileNotFoundError:
        pass
    kept.extend(readings)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=READINGS_LOG.parent, prefix=".readings-", delete=False) as output:
            temporary = output.name
            for entry in kept:
                _ = output.write(json.dumps(entry, separators=(",", ":")) + "\n")
        os.replace(temporary, READINGS_LOG)
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)


def apply(
    notes: list[Note], account: Report, checked_at: datetime,
    *, multiple_codex_homes: bool = False,
) -> QuotaUpdates:
    """Compute quota updates without writing any note."""
    if account.usage_read_outcome in (UsageReadOutcome.HELD, UsageReadOutcome.CREDENTIALS_UNAVAILABLE):
        return QuotaUpdates({}, (), ())
    messages: list[str] = []
    by_label: dict[str, dict[str, str]] = {}
    reset = account.weekly_reset
    fresh = reset if reset is not None and reset > checked_at else None
    remaining = account.weekly_remaining if fresh is not None else None
    matched = False
    readings: list[StoredReading] = []
    for note in notes:
        if note.tool != account.tool.lower():
            continue
        updates: dict[str, str] = {}
        login = (note.get("login") or "").casefold()
        matching = bool(account.email) and login == account.email.casefold()
        matched = matched or matching
        if note.tool == "claude":
            # Nothing reads a Claude limit-reset count live, so a recorded one only goes stale.
            for key in ("limit_reset_count", "limit_reset"):
                if note.get(key) not in (None, "null"):
                    updates[key] = "null"
        if matching:
            if account.tool.lower() == "codex" and account.limit_reset_count is not None:
                updates["limit_reset_count"] = str(account.limit_reset_count)
                # Preserve the earliest known expiration, including its Eastern offset.
                expirations = account.limit_reset_expirations
                updates["limit_reset"] = expirations[0].astimezone(EASTERN).isoformat(timespec="seconds") if expirations else "null"
            if account.credit_balance is not None:
                updates["credit_balance"] = account.credit_balance
            updates["weekly_remaining_usage"] = "null" if remaining is None else f"{remaining:g}"
            if fresh is not None:
                updates["resets"] = local_reset(fresh)
            if remaining is not None:
                updates["weekly_usage_checked_at"] = checked_at.isoformat(timespec="seconds")
        elif note.get("weekly_remaining_usage") is None or not still_ahead(note.get("resets")):
            updates["weekly_remaining_usage"] = "null"
        if note.get("weekly_usage_checked_at") is None and "weekly_usage_checked_at" not in updates:
            updates["weekly_usage_checked_at"] = "null"
        by_label[note.label] = updates
        preview = note.lines()
        if _rewrite(preview, dict(updates)):
            messages.append(f"{note.path.name}: " + ", ".join(f"{k}={v}" for k, v in updates.items()))
            if matching and remaining is not None:
                reading: StoredReading = {
                    "account": note.label,
                    "at": checked_at.astimezone(timezone.utc).isoformat(timespec="seconds"),
                    "remaining": remaining,
                }
                if account.tool.lower() == "codex" and (balance := updates.get("credit_balance", note.get("credit_balance"))) is not None:
                    try:
                        credits = float(balance)
                    except ValueError:
                        pass
                    else:
                        if math.isfinite(credits) and credits >= 0:
                            reading["credits"] = credits
                readings.append(reading)
    if account.email and not matched:
        messages.append(f"{account.tool}: no note has login {account.email}")
    if account.problem or account.quota_problem:
        tool = account.tool
        if (tool.lower() == "codex" and multiple_codex_homes
                and (account.problem or account.email is None)
                and isinstance(account.source, CodexHomeSource)):
            tool += f" ({codex_home_display(account.source.home)})"
        messages.append(f"{tool}: {account.problem or account.quota_problem}")
    return QuotaUpdates(by_label, tuple(messages), tuple(readings))


@role_map_command
def update(*, include_warnings: bool = True) -> list[str]:
    from scripts.accounts import accounts

    workers_home = resolve_workers_home()
    return list(update_alert_pass(accounts.claude_account_selection(), workers_home, include_warnings=include_warnings).messages)


def update_for_selection(selection: ClaudeAccountsInUse, workers_home: Path | NoWorkersHome) -> list[str]:
    """Update notes and watch using one live pass."""
    return list(update_alert_pass(selection, workers_home).messages)


@dataclass(frozen=True)
class UpdatedAgentNotes:
    messages: tuple[str, ...]
    codex_readings: CodexAlertPassReadings
    usage_reused: bool = False


@dataclass(frozen=True)
class RefreshedUsage:
    """Reports and store diagnostics from a usage-only read-and-save attempt."""
    reports: list[Report]
    observed_at: datetime
    messages: tuple[str, ...]


def refresh_usage(selection: ClaudeAccountsInUse, workers_home: Path | NoWorkersHome,
                  logins: dict[str, str], *,
                  schedule: ScheduledUsageReads | UnscheduledUsageReads = UNSCHEDULED_USAGE_READS) -> RefreshedUsage:
    """Read and save usage for both the notes timer and the Mac refresher."""
    reading = (live_reports(selection, workers_home, schedule=schedule)
               if isinstance(schedule, ScheduledUsageReads) else live_reports(selection, workers_home))
    reports = asyncio.run(reading)
    observed_at = datetime.now(timezone.utc)
    messages: list[str] = []
    if reports or isinstance(schedule, UnscheduledUsageReads):
        try:
            usage_store.save(reports, observed_at, logins)
        except (OSError, ValueError) as error:
            messages.append(f"usage store: {error}")
    unavailable = sum(report.usage_read_outcome == UsageReadOutcome.CREDENTIALS_UNAVAILABLE
                      for report in reports)
    if unavailable:
        messages.append(f"usage refresh: {unavailable} account(s) have login credentials unavailable in this session")
    return RefreshedUsage(reports, observed_at, tuple(messages))


def timer_usage_signature(selection: ClaudeAccountsInUse, workers_home: Path | NoWorkersHome) -> str:
    """Invalidate reuse when a login, directory, or account selection changes."""
    identities = sorted((str(home), codex_email_on_disk(home)) for home in codex_homes(workers_home))
    # Session order and Python's randomized set order must not change a persistent key.
    signature = (selection.home_directory_account, workers_home, identities,
                 sorted((str(account.directory), account.identity.login, account.label)
                        for account in selection.named_accounts),
                 sorted(selection.live_logins),
                 sorted((str(directory), claude_on_disk(directory).email) for directory in selection.live_directories))
    return hashlib.sha256(repr(signature).encode()).hexdigest()


def urgent_usage_read(notes: list[Note], selection: ClaudeAccountsInUse, workers_home: Path | NoWorkersHome) -> bool:
    """Moves and accounts near exhaustion keep the two-minute live cadence."""
    try:
        pending_moves = role_map.is_role_map_host() and bool(next_moves.read_next_moves())
    except next_moves.NextMoveError:
        return True
    if pending_moves:
        return True
    workers = workers_home if isinstance(workers_home, NoWorkersHome) else codex_account(workers_home)
    for note in notes:
        in_use = (selection.is_in_use(note.get("login") or "") if note.tool == "claude" else
                  isinstance(workers, KnownCodexAccount) and note.label == workers.label)
        if not in_use:
            continue
        for field in ("weekly_remaining_usage", "five_hour_remaining_usage"):
            try:
                if float(note.get(field) or "nan") <= LIVE_USAGE_REMAINING_THRESHOLD:
                    return True
            except ValueError:
                pass
    # A fallback can restore workers as soon as its reset passes.
    from scripts.whoami import quota_alert
    try:
        saved = cast(object, json.loads(quota_alert.STATE.read_text(encoding="utf-8")))
        return isinstance(saved, dict) and bool(cast(dict[str, object], saved).get("switch"))
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return True


def claude_note_logins(notes: Iterable[AgentNote]) -> dict[str, str]:
    return {note.label: login for note in notes
            if note.tool == "claude" and (login := note.get("login"))}


@role_map_command
def update_alert_pass(selection: ClaudeAccountsInUse, workers_home: Path | NoWorkersHome, *,
                      force_usage: bool = False, include_warnings: bool = True) -> UpdatedAgentNotes:
    """Build the alert readings before writing any note."""
    from scripts.accounts import accounts

    accounts.sweep_claude_temp_files(datetime.now(timezone.utc))
    messages = rename_legacy_notes(note_file.notes_dir())
    if isinstance(workers_home, NoWorkersHome):
        messages.append(workers_home.reason)
    notes = note_file.read_notes()
    multiple_codex_homes = len(codex_homes(workers_home)) > 1
    checked_at = datetime.now(timezone.utc)
    signature = timer_usage_signature(selection, workers_home)
    recent = (usage_store.TimerUsageDue() if force_usage or urgent_usage_read(notes, selection, workers_home)
              else usage_store.recent_timer_usage(signature, checked_at))
    if isinstance(recent, usage_store.RecentTimerUsage):
        for report in recent.reports:
            in_use = (codex_report_in_use(report, workers_home) if report.tool == "Codex"
                      else selection.is_in_use(report.email or ""))
            if in_use and any(quota is not None and quota.remaining_percent is not None
                              and quota.remaining_percent <= LIVE_USAGE_REMAINING_THRESHOLD
                              for quota in (*report.quotas, report.five_hour)):
                recent = usage_store.TimerUsageDue()
                break
    fresh = isinstance(recent, usage_store.TimerUsageDue)
    if isinstance(recent, usage_store.RecentTimerUsage):
        reports = recent.reports
        observed_at = recent.observed_at
    else:
        refreshed = refresh_usage(selection, workers_home, claude_note_logins(notes))
        reports, observed_at = refreshed.reports, refreshed.observed_at
        checked_at = observed_at
        messages.extend(refreshed.messages)
    codex_readings = codex_alert_pass(workers_home, reports, observed_at)
    if not notes:
        messages.append(f"no agent notes in {note_file.notes_dir()}")
    try:
        if fresh:
            usage_store.save_timer_usage(signature, reports, observed_at)
    except (OSError, ValueError) as error:
        # The saved readings must never cost the notes or the alerts.
        messages.append(f"usage store: {error}")
    merged: dict[str, dict[str, str | tuple[str, ...]]] = {note.label: {} for note in notes}
    notes_by_label = {note.label: note for note in notes}
    readings: list[StoredReading] = []
    for account in reports if fresh else []:
        result = apply(notes, account, checked_at, multiple_codex_homes=multiple_codex_homes)
        messages.extend(result.messages)
        readings.extend(result.readings)
        for label, updates in result.updates.items():
            note = notes_by_label[label]
            if account.email and (note.get("login") or "").casefold() == account.email.casefold():
                merged[label].update(updates)
            else:
                for key, value in updates.items():
                    _ = merged[label].setdefault(key, value)
    try:
        groups = accounts.group_note_labels()
    except accounts.AccountsError as error:
        messages.append(f"roles: {error}")
        groups = None
    group_roles: tuple[tuple[accounts.ClaudeGroup, str], ...] = (("showrunners", "showrunner"), ("units", "unit"))
    sessions = session_table.live_sessions(os.environ)
    machines_seen: dict[str, str] = {}
    if isinstance(sessions, session_table.SessionsUnread):
        messages.append(f"sessions: anomaly: {sessions.reason}")
    else:
        machines_seen = read_sessions_seen()
        machines_seen.update({machine: checked_at.astimezone(user_zone()).isoformat(timespec="seconds")
                              for machine in sessions.machines_read})
        try:
            write_sessions_seen(machines_seen)
        except OSError as error:
            messages.append(f"sessions seen: {error}")
    stamp = {"date_modified": f'"[[{checked_at.astimezone().strftime("%Y-%m-%d")}]]"'}
    for note in notes:
        updates = merged[note.label]
        if note.tool == "claude" and groups is not None:
            updates["role"] = tuple(role for group, role in group_roles
                                    if groups.get(group) == note.label)
        elif note.tool == "codex" and isinstance(codex_readings.selected_account, KnownCodexAccount):
            updates["role"] = ("worker",) if note.label == codex_readings.selected_account.label else ()
        rows = sessions.rows_by_label if isinstance(sessions, session_table.ReadSessions) else {}
        updates["sessions"] = str(len(rows.get(note.label, ())))
        _ = write_note(note, updates, remove=frozenset({"state"}), stamp=stamp)
    if readings:
        try:
            append_readings(readings, checked_at)
        except (OSError, UnicodeError) as error:
            messages.append(f"readings log: {error}")
    if include_warnings:
        messages += watch_usage(reports, selection, workers_home, codex_readings)
    return UpdatedAgentNotes(tuple(messages), codex_readings, usage_reused=not fresh)


def watch_usage(reports: list[Report], selection: ClaudeAccountsInUse, workers_home: Path | NoWorkersHome,
                codex_readings: CodexAlertPassReadings) -> list[str]:
    """Warn only for accounts in use, sharing the pass's existing usage readings."""
    try:
        watch_reports = [report for report in reports
                         if (report.tool.lower() != "codex" or codex_report_in_use(report, workers_home))
                         and (report.tool.lower() != "claude" or selection.is_in_use(report.email or ""))]
        return five_hour.watch(watch_reports, codex_readings=codex_readings)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        return [f"5-hour watch did not run: {error!r}"]


def mark_tier(plan: codex_pacer.Plan) -> None:
    """Show the launch tier on the worker account and clear it on other Codex notes."""
    called = codex_pacer.launch_tier(codex_pacer.registry_tier(), plan["tier"])
    stamp = {"date_modified": f'"[[{datetime.now().strftime("%Y-%m-%d")}]]"'}
    for note in note_file.read_notes():
        if note.tool == "codex":
            tier = ("fast" if called == "fast" else "regular") if "worker" in note.get_list("role") else "null"
            _ = set_fields(note, {"tier": tier}, stamp)


class RefusalCommandLine(argparse.Namespace):
    identity: list[str] = []
    window: str = "unknown"
    refused_at: str = ""
    stopped: list[str] | None = None


class UsageOnlyCommandLine(argparse.Namespace):
    poll_seconds: float = 0


def positive_poll_seconds(value: str) -> float:
    return positive({"poll_seconds": float(value)}, "poll_seconds")


def usage_only(*, poll_seconds: float = 0) -> None:
    """Read each due account, then start renewal without waiting for a probe."""
    from scripts.accounts import accounts, refusal_watch

    interval = usage_only_interval() - poll_seconds / 2
    with locked_history(schedule_directory("usage-only")) as history:
        if isinstance(history, ScheduleBusy):
            print(f"already claimed by {history.owner_id}")
            return
        with role_map.stored_role_map():
            selection = accounts.claude_account_selection(usage_only=True)
            workers_home = resolve_workers_home()
            logins = claude_note_logins(note_file.read_notes())
            with usage_store.read_cadence(interval):
                refreshed = refresh_usage(selection, workers_home, logins,
                    schedule=ScheduledUsageReads(history, interval))
    for line in refreshed.messages:
        print(line)
    try:
        if refusal_watch.renew_expired_logins(refreshed.observed_at, True):
            _ = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "renew-logins"],
                                 start_new_session=True, stdin=subprocess.DEVNULL)
    except Exception as error:
        reason = str(error).splitlines()[0] if str(error) else type(error).__name__
        print(f"login renewal: {reason}", file=sys.stderr)


def renew_logins() -> None:
    """Renew expired logins while holding only the renewal schedule lock."""
    from scripts.accounts import refusal_watch

    try:
        with locked_history(schedule_directory("login-renewal")) as renewal_history:
            if isinstance(renewal_history, ScheduleBusy):
                print(f"already claimed by {renewal_history.owner_id}")
                return
            for line in refusal_watch.renew_expired_logins(datetime.now(timezone.utc), False):
                print(line)
    except Exception as error:
        reason = str(error).splitlines()[0] if str(error) else type(error).__name__
        print(f"login renewal: {reason}", file=sys.stderr)


@role_map_command
def main() -> None:
    if sys.argv[1:2] == ["renew-logins"]:
        parser = argparse.ArgumentParser(prog="agent_notes.py renew-logins")
        _ = parser.parse_args(sys.argv[2:])
        renew_logins()
        return
    if sys.argv[1:2] == ["blocked"]:
        parser = argparse.ArgumentParser(prog="agent_notes.py blocked")
        _ = parser.add_argument("identity", nargs="+", metavar="HOME LOGIN LABEL | HOME REASON")
        _ = parser.add_argument("--window", choices=("weekly", "five_hour", "unknown"), default="unknown")
        _ = parser.add_argument("--refused-at", default="", metavar="AWARE_ISO_TIME")
        _ = parser.add_argument("--stopped", nargs=2, metavar=("SESSION_ID", "WORKER"))
        parsed = parser.parse_args(sys.argv[2:], namespace=RefusalCommandLine())
        arguments = parsed.identity
        if len(arguments) == 3:
            label = note_file.parse_account_label(arguments[2])
            if not isinstance(label, note_file.NumberedAccountLabel) or label.tool != "codex":
                parser.error("LABEL must name a numbered Codex account, such as codex1")
            identity = KnownCodexAccount(Path(arguments[0]), arguments[1], label.label)
        elif len(arguments) == 2:
            identity = UnknownCodexAccount(Path(arguments[0]), arguments[1])
        else:
            parser.error("expected HOME LOGIN LABEL or HOME REASON")
        refused_at: RefusedAt | RefusalTimeUnknown = RefusalTimeUnknown()
        if parsed.refused_at:
            try:
                parsed_time = datetime.fromisoformat(parsed.refused_at)
            except ValueError:
                parser.error("--refused-at needs an aware ISO time")
            if parsed_time.tzinfo is None:
                parser.error("--refused-at needs an aware ISO time")
            refused_at = RefusedAt(parsed_time)
        window = (refusals.KnownWindow("weekly") if parsed.window == "weekly" else
                  refusals.KnownWindow("five_hour") if parsed.window == "five_hour" else refusals.UnknownWindow())
        stopped = (StoppedWorker(*parsed.stopped) if parsed.stopped is not None
                   else StoppedWorkerUnknown())
        for line in blocked(list(note_file.read_notes()), identity, window=window, refused_at=refused_at, stopped=stopped):
            print(line)
        return
    if sys.argv[1:2] == ["usage-only"]:
        parser = argparse.ArgumentParser(prog="agent_notes.py usage-only")
        _ = parser.add_argument("--poll-seconds", type=positive_poll_seconds, default=0)
        parsed = parser.parse_args(sys.argv[2:], namespace=UsageOnlyCommandLine())
        try:
            usage_only(poll_seconds=parsed.poll_seconds)
        except (OSError, ValueError) as error:
            sys.exit(str(error))
        return
    if sys.argv[1:] not in ([], ["refresh"]):
        sys.exit("usage: agent_notes.py [refresh|usage-only|renew-logins|blocked]")
    from scripts.accounts import accounts

    from scripts.herdr import herdr

    from scripts.accounts import refusal_watch

    with refusal_watch.session_observation(), herdr.process_snapshot():
        selection = accounts.claude_account_selection()
        workers_home = resolve_workers_home()
        updated = update_alert_pass(selection, workers_home, force_usage=bool(sys.argv[1:]))
        for line in updated.messages:
            print(line)
        notes: list[AgentNote] = list(note_file.read_notes())
        for line in (refresh(notes, current_session(), selection=selection, codex_readings=updated.codex_readings) if sys.argv[1:]
                     else alert(notes, selection=selection, codex_readings=updated.codex_readings)):
            print(line)
        try:
            mark_tier(codex_pacer.tick(notes))
        except (OSError, ValueError, KeyError, TypeError) as error:
            # The pacer must never cost the notes or the alerts; launches fall back to
            # default once its decision is ten minutes old.
            print(f"codex pacer did not run: {error!r}")
        if not sys.argv[1:]:
            try:
                from scripts.accounts import refusal_watch
                def refresh_refusal_usage() -> None:
                    if updated.usage_reused:
                        # Refusal placement needs current usage, without another roster probe or note pass.
                        try:
                            reports = asyncio.run(live_reports(selection, workers_home))
                            usage_store.save(reports, datetime.now(timezone.utc), claude_note_logins(notes))
                        except Exception as error:
                            # Keep placement and the watchdog running with stored readings.
                            reason = str(error).splitlines()[0] if str(error) else type(error).__name__
                            print(f"refusal usage refresh: {reason}", file=sys.stderr)

                for line in refusal_watch.run(refresh_usage=refresh_refusal_usage):
                    print(line)
            except Exception as error:
                reason = str(error).splitlines()[0] if str(error) else type(error).__name__
                print(f"refusal watcher: {reason}", file=sys.stderr)
            try:
                from scripts.accounts import login_sittings

                for line in login_sittings.run(datetime.now(timezone.utc)):
                    print(line)
            except Exception as error:
                reason = str(error).splitlines()[0] if str(error) else type(error).__name__
                print(f"login sittings: {reason}", file=sys.stderr)


if __name__ == "__main__":
    main()
