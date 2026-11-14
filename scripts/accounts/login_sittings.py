"""Send scheduled Claude login reminders from stored dates on both machines."""
from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal, cast

from scripts.accounts import accounts
from scripts.agents import remote
from scripts.lib import conf
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.user_zone import user_zone
from scripts.maintenance.schedule import ScheduleBusy, locked_history
from scripts.message import send_budget, send_result
from scripts.notify import push_texts, review
from scripts.whoami import agent_accounts, note_file
from scripts.whoami.quota_alert import PushFailed, PushSent


TEXT_ID = "accounts.login_sitting"
PushKind = Literal["sitting", "safety"]


@dataclass(frozen=True, order=True)
class LoginEnd:
    machine: str
    account: str
    at: datetime


@dataclass(frozen=True)
class NoRecordedTime:
    """This event has not occurred."""


@dataclass(frozen=True)
class PeerReadFailed:
    reason: str


@dataclass(frozen=True)
class SittingPolicy:
    machine: str
    first: datetime
    interval: timedelta
    window: timedelta
    margin: timedelta
    peer_interval: timedelta
    peer_timeout: int

    @classmethod
    def read(cls, settings: conf.ConfigRead) -> SittingPolicy:
        return cls(settings.text("login_sitting_machine"),
                   datetime.fromisoformat(settings.text("login_sitting_first")),
                   timedelta(days=settings.whole("login_sitting_interval_days")),
                   timedelta(days=settings.whole("login_sitting_window_days")),
                   timedelta(days=settings.whole("login_safety_margin_days")),
                   timedelta(minutes=settings.whole("login_peer_read_minutes")),
                   settings.whole("login_peer_read_timeout_seconds"))


@dataclass
class ReminderState:
    sitting: datetime | NoRecordedTime = field(default_factory=NoRecordedTime)
    named: set[LoginEnd] = field(default_factory=set)
    peer: tuple[LoginEnd, ...] = ()
    peer_read: datetime | NoRecordedTime = field(default_factory=NoRecordedTime)
    peer_attempt: datetime | NoRecordedTime = field(default_factory=NoRecordedTime)


def recorded_time(value: object) -> datetime | NoRecordedTime:
    parsed = agent_accounts.login_end_from_iso(value)
    return parsed.at if isinstance(parsed, agent_accounts.LoginEndKnown) else NoRecordedTime()


def login_dates(rows: object, machine: str) -> tuple[LoginEnd, ...]:
    """Read only numbered Claude labels and dates from the public account rows."""
    if not isinstance(rows, list):
        raise ValueError("account rows must be a list")
    result: list[LoginEnd] = []
    for row in cast(list[object], rows):
        if not isinstance(row, dict):
            continue
        fields = cast(dict[str, object], row)
        label = fields.get("label")
        if not isinstance(label, str):
            continue
        parsed = note_file.parse_account_label(label)
        at = recorded_time(fields.get("login_ends_at"))
        if isinstance(parsed, note_file.NumberedAccountLabel) and parsed.tool == "claude" and isinstance(at, datetime):
            result.append(LoginEnd(machine, parsed.label, at))
    return tuple(result)


def local_dates(machine: str) -> tuple[LoginEnd, ...]:
    """Use the same stored rows as the accounts status command."""
    reading = accounts.read_status(source=accounts.StoredUsageOnly())
    rows = [{"label": row.label, "login_ends_at": agent_accounts.login_end_iso(row.login_ends_at)}
            for row in reading.rows if row.tool == "claude"]
    return login_dates(rows, machine)


def peer_dates(machine: str, timeout: int) -> tuple[LoginEnd, ...] | PeerReadFailed:
    command = ['"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/accounts/accounts.py" status --stored --json']
    try:
        done = subprocess.run(["ssh", "-o", "BatchMode=yes", machine, *command],
                              capture_output=True, text=True, timeout=timeout, check=False)
        if done.returncode:
            return PeerReadFailed(f"{machine} account read exited {done.returncode}")
        document = cast(object, json.loads(done.stdout))
        if not isinstance(document, dict):
            raise ValueError("account status must be an object")
        return login_dates(cast(dict[str, object], document).get("accounts"), machine)
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        return PeerReadFailed(str(error).splitlines()[0])


def load_state(path: Path) -> ReminderState:
    try:
        document = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return ReminderState()
    if not isinstance(document, dict):
        raise ValueError(f"{path}: expected reminder state")
    fields = cast(dict[str, object], document)

    def ends(key: str) -> tuple[LoginEnd, ...]:
        rows = fields.get(key, [])
        result: list[LoginEnd] = []
        if isinstance(rows, list):
            for row in cast(list[object], rows):
                if isinstance(row, dict):
                    item = cast(dict[str, object], row)
                    machine = item.get("machine")
                    if isinstance(machine, str):
                        result.extend(login_dates([item], machine))
        return tuple(result)

    return ReminderState(recorded_time(fields.get("sitting")), set(ends("named")), ends("peer"),
                         recorded_time(fields.get("peer_read")), recorded_time(fields.get("peer_attempt")))


def save_state(path: Path, state: ReminderState, current_dates: tuple[LoginEnd, ...]) -> None:
    current_ends = {(end.machine, end.account): end.at for end in current_dates}
    state.named = {end for end in state.named
                   if current_ends.get((end.machine, end.account), end.at) == end.at}
    def stamp(at: datetime | NoRecordedTime) -> str:
        return at.isoformat() if isinstance(at, datetime) else ""

    def rows(ends: tuple[LoginEnd, ...] | set[LoginEnd]) -> list[dict[str, str]]:
        return [{"machine": end.machine, "label": end.account, "login_ends_at": end.at.isoformat()}
                for end in sorted(ends)]

    write_text_atomic(path, json.dumps({"sitting": stamp(state.sitting), "named": rows(state.named),
        "peer": rows(state.peer), "peer_read": stamp(state.peer_read), "peer_attempt": stamp(state.peer_attempt)}) + "\n",
        new_file_mode=0o600)


def reminder_push(ends: tuple[LoginEnd, ...]) -> push_texts.RenderedPush:
    zone = user_zone()
    labels = sorted({end.account for end in ends})
    lines = [label + ": " + ", ".join(
        f"{'Mac' if end.machine == 'mac' else end.machine} {end.at.astimezone(zone):%a %m-%d}"
        for end in sorted(ends) if end.account == label) for label in labels]
    return push_texts.render(TEXT_ID, accounts="\n".join(lines))


def send_push(push: push_texts.RenderedPush, kind: PushKind) -> PushSent | PushFailed:
    limit = review.user_send_seconds()
    try:
        done = subprocess.run(push_texts.user_send_argv(push, key=f"login-{kind}", timeout=limit),
                              capture_output=True, text=True, timeout=send_budget.user_seconds(limit), check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        return PushFailed(str(error).splitlines()[0])
    result = send_result.send_outcome(done.returncode, done.stdout)
    if isinstance(result, send_result.ConfirmedSendOutcome) and result.header.outcome in ("sent", "skipped", "kept", "replaced"):
        return PushSent()
    return PushFailed(f"send.py did not accept the login reminder (exit {done.returncode})")


def run(now: datetime, *, configuration: conf.ConfigRead | conf.UseConfiguredValue = conf.UseConfiguredValue.CURRENT,
        machine: Callable[[], str] = remote.local_machine,
        peer_machine: Callable[[], str] = remote.other_machine,
        read_local: Callable[[str], tuple[LoginEnd, ...]] = local_dates,
        read_peer: Callable[[str, int], tuple[LoginEnd, ...] | PeerReadFailed] = peer_dates,
        send: Callable[[push_texts.RenderedPush, PushKind], PushSent | PushFailed] = send_push,
        state_path: Path | conf.UseConfiguredValue = conf.UseConfiguredValue.CURRENT) -> list[str]:
    settings = conf.read("accounts") if isinstance(configuration, conf.UseConfiguredValue) else configuration
    local = machine()
    if local != settings.text("login_sitting_machine"):
        return []
    policy = SittingPolicy.read(settings)
    path = (accounts.accounts_home() / ".local/state/accounts/login_sittings.json"
            if isinstance(state_path, conf.UseConfiguredValue) else state_path)
    with locked_history(path.with_suffix("")) as history:
        if isinstance(history, ScheduleBusy):
            return []
        return _run_claimed(now, policy, path, local, peer_machine, read_local, read_peer, send)


def _run_claimed(now: datetime, policy: SittingPolicy, path: Path, local: str,
                peer_machine: Callable[[], str], read_local: Callable[[str], tuple[LoginEnd, ...]],
                read_peer: Callable[[str, int], tuple[LoginEnd, ...] | PeerReadFailed],
                send: Callable[[push_texts.RenderedPush, PushKind], PushSent | PushFailed]) -> list[str]:
    """Read, send, and save while the caller holds the reminder lock."""
    state = load_state(path)
    lines: list[str] = []
    if isinstance(state.peer_attempt, NoRecordedTime) or now - state.peer_attempt >= policy.peer_interval:
        peer = read_peer(peer_machine(), policy.peer_timeout)
        state.peer_attempt = now
        if isinstance(peer, PeerReadFailed):
            lines.append(f"login sittings: peer dates unavailable: {peer.reason}")
        else:
            state.peer = peer
            state.peer_read = now
        save_state(path, state, state.peer)
    dates = (*read_local(local), *state.peer)
    save_state(path, state, dates)

    def deliver(ends: tuple[LoginEnd, ...], kind: PushKind) -> bool:
        outcome = send(reminder_push(ends), kind)
        if isinstance(outcome, PushFailed):
            lines.append(f"login sittings: {kind} push failed: {outcome.why}")
            return False
        state.named.update(ends)
        lines.append(f"login sittings: {kind} reminder accepted")
        return True

    if now >= policy.first:
        sitting = policy.first + ((now - policy.first) // policy.interval) * policy.interval
        if isinstance(state.sitting, NoRecordedTime) or state.sitting < sitting:
            labels = {end.account for end in dates if end.at <= sitting + policy.window}
            if not labels and dates:
                labels.add(min(dates, key=lambda end: (end.at, end.account)).account)
            selected = tuple(end for end in dates if end.account in labels)
            if not selected or deliver(selected, "sitting"):
                state.sitting = sitting
                save_state(path, state, dates)
            else:
                return lines
    safety = tuple(end for end in dates if end.at <= now + policy.margin and end not in state.named)
    if safety and deliver(safety, "safety"):
        save_state(path, state, dates)
    return lines
