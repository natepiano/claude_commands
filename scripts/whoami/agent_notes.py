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
from note_file import FIELD, Note
from agent_accounts import ClaudeAccountSelection, ClaudeDirectorySource, EASTERN, Report, live_reports
from quota_alert import AgentNote, alert, blocked, current_session, refresh
from run_out import READINGS_LOG

RESET_FORMAT = "%Y-%m-%dT%H:%M:%S"


class StoredReading(TypedDict):
    account: str
    at: str
    remaining: float
    credits: NotRequired[float]


@dataclass(frozen=True)
class ObservedAccountActivity:
    """The observed login alone is active, as in the single-account updater."""


@dataclass(frozen=True)
class RoleAccountActivity:
    """The Claude logins serving launch roles are active together."""

    logins: frozenset[str]


AccountActivity = ObservedAccountActivity | RoleAccountActivity
OBSERVED_ACCOUNT_ACTIVITY = ObservedAccountActivity()


def local_reset(value: datetime) -> str:
    """Local wall time rounded to the minute: the APIs jitter by fractions of a second."""
    local = value.astimezone()
    rounded = (local + timedelta(seconds=30)).replace(second=0, microsecond=0)
    return rounded.strftime(RESET_FORMAT)


def set_fields(note: Note, updates: dict[str, str], stamp: dict[str, str]) -> bool:
    """Rewrite the named top-level fields in place; insert a missing one in key order.

    `stamp` is written too, but only when `updates` changed something.
    """
    lines = note.lines()
    changed = _rewrite(lines, updates)
    if not changed:
        return False
    _ = _rewrite(lines, stamp)
    text = note.text[:note.start] + "\n".join(lines) + note.text[note.end:]
    tmp = note.path.with_name(f".{note.path.name}.tmp")
    _ = tmp.write_text(text, encoding="utf-8")
    # The snapshot service runs with UMask 0077; keep the note's own mode instead.
    os.chmod(tmp, note.path.stat().st_mode & 0o777)
    os.replace(tmp, note.path)
    note.text = text
    note.end = note.start + len("\n".join(lines))
    return True


def _rewrite(lines: list[str], updates: dict[str, str]) -> bool:
    changed = False
    for key, value in updates.items():
        rendered = f"{key}: {value}"
        index = next((i for i, line in enumerate(lines)
                      if (match := FIELD.match(line)) and match.group(1) == key), None)
        if index is None:
            # Top-level keys are kept sorted; land before the first that sorts after.
            index = next((i for i, line in enumerate(lines)
                          if (match := FIELD.match(line)) and match.group(1) > key), len(lines))
            lines.insert(index, rendered)
            changed = True
        elif lines[index] != rendered:
            lines[index] = rendered
            changed = True
    return changed


def still_ahead(value: str | None) -> bool:
    if not value:
        return False
    try:
        return datetime.strptime(value, RESET_FORMAT) > datetime.now()
    except ValueError:
        return False


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
    activity: AccountActivity = OBSERVED_ACCOUNT_ACTIVITY,
) -> list[str]:
    """Write the matching login's quota independently of its launch-role activity."""
    messages: list[str] = []
    stamp = {"date_modified": f'"[[{checked_at.astimezone().strftime("%Y-%m-%d")}]]"'}
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
        if isinstance(activity, RoleAccountActivity):
            updates["state"] = "active" if login in activity.logins else "inactive"
        elif account.email:
            updates["state"] = "active" if matching else "inactive"
        matched = matched or matching
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
        if set_fields(note, updates, stamp):
            messages.append(f"{note.path.name}: " + ", ".join(f"{k}={v}" for k, v in updates.items()))
            if matching and remaining is not None:
                reading: StoredReading = {
                    "account": note.path.stem,
                    "at": checked_at.astimezone(timezone.utc).isoformat(timespec="seconds"),
                    "remaining": remaining,
                }
                if account.tool.lower() == "codex" and (balance := note.get("credit_balance")) is not None:
                    try:
                        credits = float(balance)
                    except ValueError:
                        pass
                    else:
                        if math.isfinite(credits) and credits >= 0:
                            reading["credits"] = credits
                readings.append(reading)
    if readings:
        try:
            append_readings(readings, checked_at)
        except (OSError, UnicodeError) as error:
            messages.append(f"readings log: {error}")
    if account.email and not matched:
        messages.append(f"{account.tool}: no note has login {account.email}")
    if account.problem or account.quota_problem:
        messages.append(f"{account.tool}: {account.problem or account.quota_problem}")
    return messages


def update() -> list[str]:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.accounts import accounts

    return update_for_selection(accounts.claude_account_selection())


def update_for_selection(selection: ClaudeAccountSelection) -> list[str]:
    """Use one run's role identities for every note and live report."""
    notes = note_file.read_notes()
    if not notes:
        return [f"no agent notes in {note_file.AGENTS_DIR}"]
    reports = asyncio.run(live_reports(selection))
    checked_at = datetime.now(timezone.utc)
    messages: list[str] = []
    activity: AccountActivity = ObservedAccountActivity()
    if selection.multiple_accounts:
        activity = RoleAccountActivity(frozenset(
            login.casefold() for note in notes
            if note.tool == "claude" and (login := note.get("login"))
            and selection.roles_for_login(login)
        ))
    for account in reports:
        messages += apply(
            notes, account, checked_at,
            activity if account.tool.lower() == "claude" else ObservedAccountActivity(),
        )
    try:
        # The existing 5-hour watcher has one episode per tool. Keep its default
        # Claude input; named-account weekly episodes belong to quota_alert.
        watch_reports = [report for report in reports if (
            report.tool.lower() != "claude"
            or not selection.multiple_accounts
            or (report.source.directory == Path.home() / ".claude"
                if isinstance(report.source, ClaudeDirectorySource)
                else selection.is_default_login(report.email or ""))
        )]
        messages += five_hour.watch(watch_reports)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        # The 5-hour watch must never cost the notes or the weekly alerts.
        messages.append(f"5-hour watch did not run: {error!r}")
    return messages


def mark_tier(plan: codex_pacer.Plan) -> None:
    """Show on the active Codex note the tier codex launches carry right now."""
    called = codex_pacer.launch_tier(codex_pacer.registry_tier(), plan["tier"])
    stamp = {"date_modified": f'"[[{datetime.now().strftime("%Y-%m-%d")}]]"'}
    for note in note_file.read_notes():
        if note.tool == "codex":
            active = note.get("state") == "active"
            _ = set_fields(note, {"tier": ("fast" if called == "fast" else "regular") if active else "null"}, stamp)


def main() -> None:
    if sys.argv[1:] == ["blocked"]:
        for line in blocked(list(note_file.read_notes())):
            print(line)
        return
    if sys.argv[1:] not in ([], ["refresh"]):
        sys.exit("usage: agent_notes.py [refresh|blocked]")
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.accounts import accounts

    selection = accounts.claude_account_selection()
    for line in update_for_selection(selection):
        print(line)
    notes: list[AgentNote] = list(note_file.read_notes())
    for line in (refresh(notes, current_session(), selection=selection) if sys.argv[1:]
                 else alert(notes, selection=selection)):
        print(line)
    try:
        mark_tier(codex_pacer.tick(notes))
    except (OSError, ValueError, KeyError, TypeError) as error:
        # The pacer must never cost the notes or the alerts; launches fall back to
        # default once its decision is ten minutes old.
        print(f"codex pacer did not run: {error!r}")


if __name__ == "__main__":
    main()
