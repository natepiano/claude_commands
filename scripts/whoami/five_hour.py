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
from zoneinfo import ZoneInfo

from agent_accounts import Report
from quota_alert import PROTOCOL, load_config, recipients, relay

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "message"))
import escalate  # noqa: E402

# Percent of the 5-hour window left at which the user is told.
THRESHOLD = 20
ESCALATE_MINUTES = 15


def watch(reports: list[Report]) -> list[str]:
    """One line per session told and per alert closed."""
    lines: list[str] = []
    for report in reports:
        window = report.five_hour
        left = window.remaining_percent if window else None
        if window is None or left is None:
            continue
        key = f"five-hour-{report.tool.lower()}"
        if left > THRESHOLD:
            if escalate.close(key):
                lines.append(f"5-hour limit: {report.tool} is back above {THRESHOLD}%")
            continue
        config = load_config()
        zone = ZoneInfo(next((runner["zone"] for runner in config["showrunners"]), "UTC"))
        resets = f"; it resets at {window.resets_at.astimezone(zone):%H:%M %Z}" if window.resets_at else ""
        news = f"{report.tool} has {left:.0f}% of its 5-hour limit left{resets}."
        if not escalate.hold(key, f"{report.tool} 5-hour limit", news, ESCALATE_MINUTES):
            continue
        text = (f"5-hour limit: {news}\nTell the user in one line and change nothing else. They are sent it directly"
                + f" unless they type in a terminal within {ESCALATE_MINUTES} minutes.\nProtocol: {PROTOCOL}")
        names = recipients(config)
        with ThreadPoolExecutor() as pool:
            problems = pool.map(relay, names, repeat(text), repeat(f"5-hour {report.tool}"))
            lines += [f"5-hour limit: {report.tool} {left:.0f}% left -> {name}"
                      + (f": NOT sent: {problem}" if problem else "") for name, problem in zip(names, problems)]
    return lines
