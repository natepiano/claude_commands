"""Tell the user when Claude or Codex is close to its 5-hour limit.

agent_notes hands `watch` the live reports on its two-minute timer. When an
account's 5-hour window has THRESHOLD percent or less left, every session on the
quota alert list is sent one `5-hour limit:` message, and escalate.py holds the
same news for the user: it reaches them directly unless they type in a terminal
within ESCALATE_MINUTES. The hold is the record of the alert, so a low spell is
announced once; it is closed when the window is back above the threshold.
"""
from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from itertools import repeat
from pathlib import Path
from datetime import datetime, timezone
import hashlib
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.whoami.agent_accounts import KnownCodexAccount
from agent_accounts import Report, seats_home
from quota_alert import (PROTOCOL, load_config, recipients, relay, showrunners_now,
                         CodexAlertPassReadings, ReadCurrentCodexReadings, READ_CURRENT_CODEX_READINGS,
                         codex_alert_pass, selected_codex_report, codex_swap_line)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "message"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "notify"))
import escalate  # noqa: E402
from user_action import NoActionRequired  # noqa: E402

# Percent of the 5-hour window left at which the user is told.
THRESHOLD = 20
ESCALATE_MINUTES = 15


def watch(reports: list[Report], *,
          codex_readings: CodexAlertPassReadings | ReadCurrentCodexReadings = READ_CURRENT_CODEX_READINGS) -> list[str]:
    """One line per session told and per alert closed."""
    lines: list[str] = []
    now = datetime.now(timezone.utc)
    readings = (codex_alert_pass(seats_home(), reports, now)
                if isinstance(codex_readings, ReadCurrentCodexReadings) else codex_readings)
    selected = readings.selected_account
    if isinstance(selected, KnownCodexAccount):
        escalate.reconcile_codex_holds(selected.home, selected.login)
    codex_report = selected_codex_report(readings, now)
    for report in [*(report for report in reports if report.tool.lower() != "codex"), codex_report]:
        owner: escalate.HoldOwner = escalate.UnownedHold()
        name = report.tool
        key = f"five-hour-{report.tool.lower()}"
        if report.tool.lower() == "codex":
            if not isinstance(selected, KnownCodexAccount) or report.problem:
                continue
            owner = selected
            name = selected.label
            identity = hashlib.sha256(f"{selected.home}\0{selected.login.casefold()}".encode()).hexdigest()[:24]
            key += "-" + identity
        window = report.five_hour
        left = window.remaining_percent if window else None
        if window is None or left is None:
            continue
        if report.tool.lower() == "codex" and (window.resets_at is None or window.resets_at <= now):
            continue
        if left > THRESHOLD:
            if escalate.close(key):
                lines.append(f"5-hour limit: {name} is back above {THRESHOLD}%")
            continue
        config = load_config()
        zone = ZoneInfo(next((runner["zone"] for runner in showrunners_now()), "UTC"))
        resets = f"; it resets at {window.resets_at.astimezone(zone):%H:%M %Z}" if window.resets_at else ""
        news = f"{name} has {left:.0f}% of its 5-hour limit left{resets}."
        swap_line = codex_swap_line(readings, now) if report.tool.lower() == "codex" else ""
        if swap_line:
            news += "\n" + swap_line
        names = recipients(config)
        if not escalate.hold(
            key,
            f"{name} 5-hour limit",
            news,
            NoActionRequired(),
            ESCALATE_MINUTES,
            need="note",
            owner=owner,
            relays=tuple(escalate.HeldRelay(recipient=recipient, key=f"5-hour {name}")
                         for recipient in names) if isinstance(owner, KnownCodexAccount) else (),
        ):
            continue
        text = (f"5-hour limit: {news}\nTell the user in one line and change nothing else. They are sent it directly"
                + f" unless they type in a terminal within {ESCALATE_MINUTES} minutes.\nProtocol: {PROTOCOL}")
        with ThreadPoolExecutor() as pool:
            problems = pool.map(relay, names, repeat(text), repeat(f"5-hour {name}"))
            lines += [f"5-hour limit: {name} {left:.0f}% left -> {recipient}"
                      + (f": NOT sent: {problem}" if problem else "") for recipient, problem in zip(names, problems)]
    return lines
