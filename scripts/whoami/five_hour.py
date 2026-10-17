"""Log Codex five-hour warnings and relay Claude warnings once per low spell."""
from __future__ import annotations

import sys
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from itertools import repeat
from pathlib import Path
from datetime import datetime, timezone
import hashlib
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.whoami.agent_accounts import KnownCodexAccount
from agent_accounts import Report, observe_workers_home
from quota_alert import (PROTOCOL, load_config, recipients, relay, showrunners_now,
                         CodexAlertPassReadings, ReadCurrentCodexReadings, READ_CURRENT_CODEX_READINGS,
                         codex_alert_pass, selected_codex_report, codex_swap_line)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "message"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "notify"))
import escalate  # noqa: E402
# Percent of the five-hour window left at which the alert list is told.
THRESHOLD = 20
STATE = Path(os.environ.get("FIVE_HOUR_STATE_DIR")
             or Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "five_hour")


def notice_path(key: str) -> Path:
    return STATE / f"{key}.notice"


def cancel_outgoing_notices(key: str, names: list[str]) -> None:
    """Retire queued session warnings for an account no longer selected."""
    for path in STATE.glob("five-hour-codex-*.notice"):
        if path == notice_path(key):
            continue
        name = path.read_text(encoding="utf-8")
        for recipient in names:
            _ = subprocess.run([sys.executable, str(escalate.SEND), "retire", "--to", recipient,
                                "--from", "quota_alert", "--key", f"5-hour {name}"],
                               capture_output=True, text=True, check=False)
        path.unlink(missing_ok=True)


def watch(reports: list[Report], *,
          codex_readings: CodexAlertPassReadings | ReadCurrentCodexReadings = READ_CURRENT_CODEX_READINGS) -> list[str]:
    """One line per Codex warning, Claude session told and alert closed."""
    lines: list[str] = []
    now = datetime.now(timezone.utc)
    readings = (codex_alert_pass(observe_workers_home(), reports, now)
                if isinstance(codex_readings, ReadCurrentCodexReadings) else codex_readings)
    selected = readings.selected_account
    if isinstance(selected, KnownCodexAccount):
        escalate.reconcile_codex_holds(selected.home, selected.login)
    codex_report = selected_codex_report(readings, now)
    for report in [*(report for report in reports if report.tool.lower() != "codex"), codex_report]:
        name = report.tool
        key = f"five-hour-{report.tool.lower()}"
        if report.tool.lower() == "codex":
            if not isinstance(selected, KnownCodexAccount):
                continue
            name = selected.label
            identity = hashlib.sha256(f"{selected.home}\0{selected.login.casefold()}".encode()).hexdigest()[:24]
            key += "-" + identity
            cancel_outgoing_notices(key, recipients(load_config()))
        old_hold_closed = escalate.close(key)
        marker = notice_path(key)
        if report.problem:
            continue
        window = report.five_hour
        left = window.remaining_percent if window else None
        if window is None or left is None:
            continue
        if report.tool.lower() == "codex" and (window.resets_at is None or window.resets_at <= now):
            continue
        if left > THRESHOLD:
            if marker.exists() or old_hold_closed:
                marker.unlink(missing_ok=True)
                lines.append(f"5-hour limit: {name} is back above {THRESHOLD}%")
            continue
        config = load_config()
        zone = ZoneInfo(next((runner["zone"] for runner in showrunners_now()), "UTC"))
        resets = f"; it resets at {window.resets_at.astimezone(zone):%H:%M %Z}" if window.resets_at else ""
        news = f"{name} has {left:.0f}% of its 5-hour limit left{resets}."
        swap_line = codex_swap_line(readings, now) if report.tool.lower() == "codex" else ""
        if swap_line:
            news += "\n" + swap_line
        STATE.mkdir(parents=True, exist_ok=True)
        try:
            with marker.open("x", encoding="utf-8") as output:
                _ = output.write(name)
        except FileExistsError:
            continue
        if old_hold_closed:
            continue
        if report.tool.lower() == "codex":
            lines.append(f"5-hour limit: {news}")
            continue
        names = recipients(config)
        text = (f"5-hour limit: {news}\nTell the user in one line and change nothing else."
                + f"\nProtocol: {PROTOCOL}")
        with ThreadPoolExecutor() as pool:
            problems = pool.map(relay, names, repeat(text), repeat(f"5-hour {name}"))
            lines += [f"5-hour limit: {name} {left:.0f}% left -> {recipient}"
                      + (f": NOT sent: {problem}" if problem else "") for recipient, problem in zip(names, problems)]
    return lines
