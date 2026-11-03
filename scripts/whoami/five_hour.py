"""Log Codex five-hour warnings and relay Claude warnings once per low spell."""
from __future__ import annotations

import sys
import os
import subprocess
import fcntl
import json
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime, timezone
import hashlib
from typing import Literal, TypedDict, cast
from uuid import uuid4
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.whoami.agent_accounts import KnownCodexAccount
from scripts.whoami.agent_accounts import Report, observe_workers_home
from scripts.whoami import account
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


@dataclass(frozen=True)
class WarningDelivery:
    spell: str
    delivered: frozenset[str]


@dataclass(frozen=True)
class NoWarningDelivery:
    """No warning is owed for this low spell."""


@dataclass(frozen=True)
class EndedWarningSpell:
    """Recovery ended this spell; any remaining sent marker is obsolete."""


@dataclass(frozen=True)
class NoticeNotClaimed:
    """Another pass owns the notice, or all recipients have received it."""


class WarningDeliveryDocument(TypedDict):
    spell: str
    delivered: list[str]


class EndedWarningSpellDocument(TypedDict):
    state: Literal["ended"]


def delivery_path(key: str) -> Path:
    return STATE / f"{key}.delivery"


def read_delivery(key: str) -> WarningDelivery | NoWarningDelivery | EndedWarningSpell:
    try:
        value = cast(object, json.loads(delivery_path(key).read_text(encoding="utf-8")))
    except FileNotFoundError:
        return NoWarningDelivery()
    if isinstance(value, dict):
        fields = cast(dict[str, object], value)
        if fields.get("state") == "ended":
            return EndedWarningSpell()
        spell, delivered = fields.get("spell"), fields.get("delivered")
        if isinstance(spell, str) and spell and isinstance(delivered, list):
            recipients_sent = cast(list[object], delivered)
            if all(isinstance(recipient, str) for recipient in recipients_sent):
                return WarningDelivery(spell, frozenset(cast(list[str], recipients_sent)))
    raise ValueError("invalid five-hour warning delivery")


def save_delivery(key: str, delivery: WarningDelivery | EndedWarningSpell) -> None:
    temporary = STATE / f".{key}-{uuid4().hex}.tmp"
    try:
        document = (WarningDeliveryDocument(spell=delivery.spell, delivered=sorted(delivery.delivered))
                    if isinstance(delivery, WarningDelivery) else EndedWarningSpellDocument(state="ended"))
        _ = temporary.write_text(json.dumps(document), encoding="utf-8")
        os.replace(temporary, delivery_path(key))
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def notice_state_lock(key: str) -> Generator[None]:
    """Order recovery, recipient receipts and the sent marker together."""
    with (STATE / f"{key}.state-lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


@contextmanager
def notice_claim(key: str) -> Generator[WarningDelivery | NoticeNotClaimed]:
    """Hold the send claim until this pass ends; a stopped owner releases it."""
    with (STATE / f"{key}.claim").open("a") as claim:
        try:
            fcntl.flock(claim.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield NoticeNotClaimed()
            return
        try:
            delivery: WarningDelivery | NoticeNotClaimed = NoticeNotClaimed()
            with notice_state_lock(key):
                previous = read_delivery(key)
                if isinstance(previous, EndedWarningSpell):
                    notice_path(key).unlink(missing_ok=True)
                if not notice_path(key).exists():
                    delivery = previous if isinstance(previous, WarningDelivery) else WarningDelivery(uuid4().hex, frozenset())
                    save_delivery(key, delivery)
            yield delivery
        finally:
            fcntl.flock(claim.fileno(), fcntl.LOCK_UN)


def record_recipient(key: str, claim: WarningDelivery, recipient: str) -> None:
    with notice_state_lock(key):
        current = read_delivery(key)
        if isinstance(current, WarningDelivery) and current.spell == claim.spell:
            save_delivery(key, WarningDelivery(current.spell, current.delivered | {recipient}))


def finish_notice(key: str, claim: WarningDelivery, names: list[str], name: str) -> None:
    with notice_state_lock(key):
        current = read_delivery(key)
        if (isinstance(current, WarningDelivery) and current.spell == claim.spell
                and current.delivered.issuperset(names)):
            _ = notice_path(key).write_text(name, encoding="utf-8")
            delivery_path(key).unlink(missing_ok=True)


def recover_notice(key: str) -> bool:
    if not STATE.exists():
        return False
    with notice_state_lock(key):
        marker, pending = notice_path(key), delivery_path(key)
        recovered = marker.exists() or pending.exists()
        if recovered:
            save_delivery(key, EndedWarningSpell())
        marker.unlink(missing_ok=True)
        pending.unlink(missing_ok=True)
        return recovered


def cancel_outgoing_notices(key: str, names: list[str]) -> None:
    """Retire queued session warnings for an account no longer selected."""
    for path in STATE.glob("five-hour-codex-*.notice"):
        if path == notice_path(key):
            continue
        name = path.read_text(encoding="utf-8")
        retired = True
        for recipient in names:
            result = subprocess.run([sys.executable, str(escalate.SEND), "retire", "--to", recipient,
                                "--from", "quota_alert", "--key", f"5-hour {name}"],
                               capture_output=True, text=True, check=False)
            if result.returncode != 0:
                retired = False
        if retired:
            path.unlink(missing_ok=True)


def watch(reports: list[Report], *,
          now: datetime | None = None,
          codex_readings: CodexAlertPassReadings | ReadCurrentCodexReadings = READ_CURRENT_CODEX_READINGS) -> list[str]:
    """One line per Codex warning, Claude session told and alert closed."""
    lines: list[str] = []
    now = now or datetime.now(timezone.utc)
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
        elif report.tool.lower() == "claude":
            if not report.email:
                continue
            identity = hashlib.sha256(report.email.casefold().encode()).hexdigest()[:24]
            key += "-" + identity
            label = account.label_for("claude", report.email)
            name = "Claude" if label == report.email else label
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
            recovered = recover_notice(key) if report.tool.lower() != "codex" else marker.exists()
            if recovered or old_hold_closed:
                if report.tool.lower() == "codex":
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
        if report.tool.lower() == "codex":
            try:
                with marker.open("x", encoding="utf-8") as output:
                    _ = output.write(name)
            except FileExistsError:
                continue
            if old_hold_closed:
                continue
            lines.append(f"5-hour limit: {news}")
            continue
        with notice_claim(key) as claimed:
            if isinstance(claimed, NoticeNotClaimed):
                continue
            delivery: WarningDelivery = claimed
            names = recipients(config)
            if not old_hold_closed:
                owed = [recipient for recipient in names if recipient not in delivery.delivered]
                text = (f"5-hour limit: {news}\nTell the user in one line and change nothing else."
                        + f"\nProtocol: {PROTOCOL}")

                def send_notice(recipient: str) -> str | None:
                    problem = relay(recipient, text, f"5-hour {name}")
                    if not problem:
                        record_recipient(key, delivery, recipient)
                    return problem

                with ThreadPoolExecutor() as pool:
                    problems = list(pool.map(send_notice, owed))
                lines += [f"5-hour limit: {name} {left:.0f}% left -> {recipient}"
                          + (f": NOT sent: {problem}" if problem else "") for recipient, problem in zip(owed, problems)]
            else:
                for recipient in names:
                    record_recipient(key, delivery, recipient)
            finish_notice(key, delivery, names, name)
    return lines
