#!/usr/bin/env python3
"""Update per-account agent notes using the same live reports as /whoami.

Match by tool and login. Preserve inactive accounts' last observed values until
that quota window expires; unknown or expired usage is YAML null. Never infer
100% remaining from a reset. weekly_usage_checked_at records the observation in
UTC so a saved reading is distinguishable from live usage. No credentials are
saved. Other frontmatter and note bodies are preserved. Run as a script, it then
hands the notes to quota_alert.py, which messages sessions when an account runs
low, and to codex_pacer.py, which decides the `pace` tier from them; `refresh`
(/quota_refresh) is the run after the user reports a usage reset. `blocked` is
codex_mesh.py reporting that Codex refused a launch for quota: it reads the notes
as the timer left them and hands them to quota_alert.blocked().
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NotRequired, TypedDict, cast

import codex_pacer
import five_hour
import note_file
import session_table
from session_table import SessionsSection, KeepSections, KEEP_SECTIONS
from note_file import FIELD, Note, reset_time
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.accounts import refusals
from scripts.whoami.agent_accounts import KnownCodexAccount, UnknownCodexAccount
from agent_accounts import (ClaudeAccountsInUse, ClaudeDirectorySource, CodexHomeSource, EASTERN, Report,
                            codex_home_display, codex_homes,
                            codex_report_in_use, live_reports, seats_home as resolve_seats_home)
from quota_alert import (AgentNote, alert, blocked, current_session, refresh, CodexAlertPassReadings,
                         codex_alert_pass, RefusedAt, RefusalTimeUnknown)
from run_out import READINGS_LOG
import usage_store



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


def update() -> list[str]:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.accounts import accounts

    seats_home = resolve_seats_home()
    return update_for_selection(accounts.claude_account_selection(), seats_home)


def update_for_selection(selection: ClaudeAccountsInUse, seats_home: Path) -> list[str]:
    """Update notes and watch using one live pass."""
    return list(update_alert_pass(selection, seats_home).messages)


@dataclass(frozen=True)
class UpdatedAgentNotes:
    messages: tuple[str, ...]
    codex_readings: CodexAlertPassReadings


def update_alert_pass(selection: ClaudeAccountsInUse, seats_home: Path) -> UpdatedAgentNotes:
    """Build the alert readings before writing any note."""
    messages = rename_legacy_notes(note_file.notes_dir())
    notes = note_file.read_notes()
    multiple_codex_homes = len(codex_homes(seats_home)) > 1
    reports = asyncio.run(live_reports(selection, seats_home))
    checked_at = datetime.now(timezone.utc)
    codex_readings = codex_alert_pass(seats_home, reports, checked_at)
    if not notes:
        messages.append(f"no agent notes in {note_file.notes_dir()}")
    try:
        # Usage history and process labels use the stable account label.
        logins = {note.label: login for note in notes
                  if note.tool == "claude" and (login := note.get("login"))}
        usage_store.save(reports, checked_at, logins)
    except (OSError, ValueError) as error:
        # The saved readings must never cost the notes or the alerts.
        messages.append(f"usage store: {error}")
    merged: dict[str, dict[str, str | tuple[str, ...]]] = {note.label: {} for note in notes}
    notes_by_label = {note.label: note for note in notes}
    readings: list[StoredReading] = []
    for account in reports:
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
    from scripts.accounts import accounts
    try:
        groups = accounts.group_note_labels()
    except accounts.AccountsError as error:
        messages.append(f"roles: {error}")
        groups = None
    group_roles: tuple[tuple[accounts.ClaudeGroup, str], ...] = (("showrunners", "showrunner"), ("units", "unit"))
    sessions = session_table.live_sessions(os.environ)
    if isinstance(sessions, session_table.SessionsUnread):
        messages.append(f"sessions: {sessions.reason}")
    stamp = {"date_modified": f'"[[{checked_at.astimezone().strftime("%Y-%m-%d")}]]"'}
    for note in notes:
        updates = merged[note.label]
        if note.tool == "claude" and groups is not None:
            updates["role"] = tuple(role for group, role in group_roles
                                    if groups.get(group) == note.label)
        elif note.tool == "codex" and isinstance(codex_readings.selected_account, KnownCodexAccount):
            updates["role"] = ("worker",) if note.label == codex_readings.selected_account.label else ()
        section: SessionsSection | KeepSections = KEEP_SECTIONS
        if isinstance(sessions, session_table.ReadSessions):
            kept = tuple(session_table.last_seen(row) for row in session_table.section_rows(note.text[note.end:])
                         if row.machine in sessions.rows_kept)
            live = tuple(session_table.observed(row, checked_at)
                         for row in sessions.rows_by_label.get(note.label, ()))
            rows = live + kept
            updates["sessions"] = str(len(live))
            section = SessionsSection(session_table.render_section(rows, sessions.unattributed))
        _ = write_note(note, updates, remove=frozenset({"state"}), section=section, stamp=stamp)
    if readings:
        try:
            append_readings(readings, checked_at)
        except (OSError, UnicodeError) as error:
            messages.append(f"readings log: {error}")
    try:
        # Keep the default Claude input; Codex ownership comes from the frozen pass.
        watch_reports = [report for report in reports
                         if (report.tool.lower() != "codex" or codex_report_in_use(report, seats_home))
                         and (report.tool.lower() != "claude" or not selection.multiple_accounts
                              or (report.source.directory == Path.home() / ".claude"
                                  if isinstance(report.source, ClaudeDirectorySource)
                                  else selection.is_default_login(report.email or "")))]
        messages += five_hour.watch(watch_reports, codex_readings=codex_readings)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        # The 5-hour watch must never cost the notes or the weekly alerts.
        messages.append(f"5-hour watch did not run: {error!r}")
    return UpdatedAgentNotes(tuple(messages), codex_readings)


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


def main() -> None:
    if sys.argv[1:2] == ["blocked"]:
        parser = argparse.ArgumentParser(prog="agent_notes.py blocked")
        _ = parser.add_argument("identity", nargs="+", metavar="HOME LOGIN LABEL | HOME REASON")
        _ = parser.add_argument("--window", choices=("weekly", "five_hour", "unknown"), default="unknown")
        _ = parser.add_argument("--refused-at", default="", metavar="AWARE_ISO_TIME")
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
        for line in blocked(list(note_file.read_notes()), identity, window=window, refused_at=refused_at):
            print(line)
        return
    if sys.argv[1:] not in ([], ["refresh"]):
        sys.exit("usage: agent_notes.py [refresh|blocked]")
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.accounts import accounts

    selection = accounts.claude_account_selection()
    seats_home = resolve_seats_home()
    updated = update_alert_pass(selection, seats_home)
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
            for line in refusal_watch.run():
                print(line)
        except Exception as error:
            reason = str(error).splitlines()[0] if str(error) else type(error).__name__
            print(f"refusal watcher: {reason}", file=sys.stderr)


if __name__ == "__main__":
    main()
