#!/usr/bin/env python3
"""Find local Claude quota refusals and start safe, account-grouped moves."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NotRequired, Protocol, TypedDict, cast
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts" / "shutdown"))
sys.path.insert(0, str(ROOT / "scripts" / "whoami"))
from scripts.accounts import accounts, moves, refusals
import account
import agent_accounts
from note_file import canonical_label
import usage_store
from quota_alert import RefusedAt, _refusal_window  # pyright: ignore[reportPrivateUsage]

if TYPE_CHECKING:
    from scripts.shutdown import inventory as inventory_module, record
    from scripts.message import sessions
else:
    inventory_module = import_module("inventory")
    record = import_module("record")
    sessions = import_module("sessions")


@dataclass(frozen=True)
class NoSessionRefusal:
    """The session has no current transcript or assumed quota refusal."""


@dataclass(frozen=True)
class ContinuationDelivered:
    """The live owner accepted its continuation after the quota wait."""


@dataclass(frozen=True)
class ContinuationNotDelivered:
    reason: str


class LiveMessageSent(TypedDict):
    kind: Literal["sent"]


class LiveMessageQueued(TypedDict):
    kind: Literal["queued"]
    reason: str


class LiveSessionMessenger(Protocol):
    def send_message(self, recipient: str, summary: str, text: str, *, key: str = "") -> LiveMessageSent | LiveMessageQueued: ...


@dataclass(frozen=True)
class TranscriptRefusal:
    entry_id: str
    refused_at: RefusedAt
    window: refusals.KnownWindow | refusals.UnknownWindow
    text: str
    reset: refusals.KnownReset | refusals.NoResetKnown = field(default_factory=refusals.NoResetKnown)


class AssumedRefusal(TypedDict):
    uuid: str
    timestamp: str
    window: Literal["weekly", "five_hour"]
    reset: str


class FirstRefusalObservation(TypedDict):
    login: str
    window: Literal["weekly", "five_hour", "unknown"]
    reset: NotRequired[str]
    at: str


WatchTransition = Literal["requested", "cancelled", "continued", "waited", "no room", "retry", "group selected"]


class WatchedSession(TypedDict):
    entry_id: str
    state: WatchTransition
    move_id: str
    refusal: NotRequired[FirstRefusalObservation]


class WatchState(TypedDict):
    sessions: dict[str, WatchedSession]
    notices: dict[str, str]


def state_root() -> Path:
    return Path.home() / ".local" / "state" / "accounts"


def assumed_file(session_id: str) -> Path:
    if not session_id or Path(session_id).name != session_id:
        raise ValueError("invalid session id")
    return state_root() / "assumed_refusals" / (session_id + ".json")


def _object(path: Path) -> dict[str, object]:
    try:
        value = cast(object, json.loads(path.read_text()))
    except FileNotFoundError:
        return {}
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".watch-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            _ = output.write(json.dumps(value, sort_keys=True) + "\n")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _load_state() -> WatchState:
    held = _object(state_root() / "refusal_watch.json")
    result = WatchState(sessions={}, notices={})
    raw_sessions = held.get("sessions")
    if isinstance(raw_sessions, dict):
        for session_id, raw in cast(dict[str, object], raw_sessions).items():
            if isinstance(raw, dict):
                value = cast(dict[str, object], raw)
                if (all(isinstance(value.get(key), str) for key in ("entry_id", "move_id"))
                        and value.get("state") in ("requested", "cancelled", "continued", "waited", "no room", "retry", "group selected")):
                    result["sessions"][session_id] = cast(WatchedSession, cast(object, value))
    raw_notices = held.get("notices")
    if isinstance(raw_notices, dict):
        result["notices"] = {key: value for key, value in cast(dict[str, object], raw_notices).items() if isinstance(value, str)}
    return result


@contextmanager
def _watch_lock() -> Generator[None]:
    # The synchronization file does not initialize account state during a refused preparation.
    identity = hashlib.sha256(str(state_root()).encode()).hexdigest()
    lock = Path(tempfile.gettempdir()) / ("claude-refusal-watch-" + identity + ".lock")
    with lock.open("a") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield


class TranscriptEntry(TypedDict, total=False):
    type: str
    isApiErrorMessage: bool
    error: str
    uuid: str
    timestamp: str
    message: object
    quotaLimits: object


@dataclass(frozen=True)
class NewestExchange:
    entry: TranscriptEntry


@dataclass(frozen=True)
class NoExchange:
    """No readable user or assistant entry is present in the transcript."""


def _newest_exchange(path: Path) -> NewestExchange | NoExchange:
    """Read the newest user or assistant turn, ignoring trailing bookkeeping."""
    try:
        with path.open("rb") as source:
            position = source.seek(0, os.SEEK_END)
            pending = b""
            while position:
                size = min(position, 64 * 1024)
                position -= size
                _ = source.seek(position)
                lines = (source.read(size) + pending).split(b"\n")
                pending = lines[0] if position else b""
                for line in reversed(lines[1:] if position else lines):
                    try:
                        value = cast(object, json.loads(line))
                    except (ValueError, TypeError, UnicodeError):
                        continue
                    if isinstance(value, dict):
                        entry = cast(dict[str, object], value)
                        if entry.get("type") in ("user", "assistant"):
                            return NewestExchange(cast(TranscriptEntry, cast(object, entry)))
    except OSError:
        pass
    return NoExchange()


def transcript_refusal(session_id: str, *, config_dir: Path | None = None) -> TranscriptRefusal | NoSessionRefusal:
    root = config_dir if config_dir is not None else Path.home() / ".claude"
    try:
        paths = list((root / "projects").glob("*/" + session_id + ".jsonl"))
        if not paths:
            return NoSessionRefusal()
        transcript = max(paths, key=lambda path: path.stat().st_mtime)
    except OSError:
        return NoSessionRefusal()
    latest = _newest_exchange(transcript)
    if isinstance(latest, NoExchange):
        return NoSessionRefusal()
    entry = latest.entry
    if entry.get("type") != "assistant" or entry.get("isApiErrorMessage") is not True or entry.get("error") != "rate_limit":
        return NoSessionRefusal()
    entry_id, timestamp = entry.get("uuid"), entry.get("timestamp")
    if not isinstance(entry_id, str) or not entry_id or not isinstance(timestamp, str):
        return NoSessionRefusal()
    try:
        at = datetime.fromisoformat(timestamp)
    except ValueError:
        return NoSessionRefusal()
    if at.tzinfo is None:
        return NoSessionRefusal()
    message = entry.get("message")
    content = cast(dict[str, object], message).get("content") if isinstance(message, dict) else ""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = "\n".join(str(cast(dict[str, object], block).get("text", ""))
                         for block in cast(list[object], content) if isinstance(block, dict))
    else:
        text = ""
    folded = text.casefold()
    window: refusals.KnownWindow | refusals.UnknownWindow = (
        refusals.KnownWindow("weekly") if "weekly limit" in folded else
        refusals.KnownWindow("five_hour") if "hit your session limit" in folded else refusals.UnknownWindow())
    reset: refusals.KnownReset | refusals.NoResetKnown = refusals.NoResetKnown()
    quota = entry.get("quotaLimits")
    if isinstance(quota, dict):
        limits = cast(dict[str, object], quota)
        rate_type = limits.get("rateLimitType")
        if rate_type in ("seven_day", "weekly", "seven_day_opus", "seven_day_sonnet"):
            window = refusals.KnownWindow("weekly")
        elif rate_type == "five_hour":
            window = refusals.KnownWindow("five_hour")
        raw_reset = limits.get("resetsAt")
        try:
            if isinstance(raw_reset, (int, float)) and not isinstance(raw_reset, bool):
                reset = refusals.KnownReset(datetime.fromtimestamp(raw_reset, timezone.utc))
            elif isinstance(raw_reset, str):
                parsed = datetime.fromisoformat(raw_reset)
                if parsed.tzinfo is not None:
                    reset = refusals.KnownReset(parsed)
        except (ValueError, OverflowError, OSError):
            pass
    return TranscriptRefusal(entry_id, RefusedAt(at), window, text, reset)


def assumed_refusal(session_id: str) -> TranscriptRefusal | NoSessionRefusal:
    value = _object(assumed_file(session_id))
    if not value:
        return NoSessionRefusal()
    if (not all(isinstance(value.get(key), str) for key in ("uuid", "timestamp", "window", "reset"))
            or value["window"] not in ("weekly", "five_hour")):
        raise ValueError("invalid assumed refusal")
    held = cast(AssumedRefusal, cast(object, value))
    at = datetime.fromisoformat(held["timestamp"])
    reset = refusals.KnownReset(datetime.fromisoformat(held["reset"])) if held["reset"] else refusals.NoResetKnown()
    if at.tzinfo is None or isinstance(reset, refusals.KnownReset) and reset.at.tzinfo is None:
        raise ValueError("assumed refusal time needs an offset")
    return TranscriptRefusal(held["uuid"], RefusedAt(at), refusals.KnownWindow(held["window"]), "assumed refusal", reset)


def session_refusal(session_id: str) -> TranscriptRefusal | NoSessionRefusal:
    assumed = assumed_refusal(session_id)
    return transcript_refusal(session_id) if isinstance(assumed, NoSessionRefusal) else assumed


def clear_assumed(session_id: str) -> None:
    assumed_file(session_id).unlink(missing_ok=True)


def cancelled(session_id: str) -> None:
    """Suppress this unchanged refusal, including after its assumption is removed."""
    try:
        refusal = session_refusal(session_id)
    except (OSError, ValueError):
        # Cancellation must also remove an incomplete live-proof file.
        clear_assumed(session_id)
        refusal = transcript_refusal(session_id)
    if isinstance(refusal, NoSessionRefusal):
        clear_assumed(session_id)
        return
    with _watch_lock():
        state = _load_state()
        state["sessions"][session_id] = WatchedSession(entry_id=refusal.entry_id, state="cancelled", move_id="")
        # An assumption can cover a real transcript refusal; neither may retrigger cancellation.
        transcript = transcript_refusal(session_id)
        if isinstance(transcript, TranscriptRefusal):
            state["sessions"][session_id] = WatchedSession(entry_id=transcript.entry_id, state="cancelled", move_id="")
        _atomic_json(state_root() / "refusal_watch.json", state)
        clear_assumed(session_id)


NO_SESSION_FILTER: frozenset[str] = frozenset()


def live_owners(*, only: frozenset[str] = NO_SESSION_FILTER) -> list[inventory_module.Session]:
    owners: dict[str, inventory_module.Session] = {}
    logins = {identity.login for session in sessions.live_sessions()
              if not only or session["sessionId"] in only
              if (identity := account.account_of(session["pid"])) is not None}
    for login in sorted(logins):
        for session in inventory_module.inventory(login, only, refresh_desktops=False)["sessions"]:
            if session["kind"] != "seat":
                owners[session["session_id"]] = session
    return list(owners.values())


def _notify_no_room(text: str, now: datetime) -> None:
    import quota_alert
    config = quota_alert.load_config()
    _ = quota_alert.deliver_and_record([quota_alert.Job("", recipient, text, "refusal")
        for recipient in quota_alert.recipients(config)], now.isoformat())


def _first_refusal(held: FirstRefusalObservation) -> refusals.AccountRefusal:
    window = refusals.UnknownWindow() if held["window"] == "unknown" else refusals.KnownWindow(held["window"])
    reset = refusals.KnownReset(datetime.fromisoformat(held["reset"])) if "reset" in held else refusals.NoResetKnown()
    return refusals.AccountRefusal("claude", held["login"], window, reset, datetime.fromisoformat(held["at"]))


def _set_transition(state: WatchState, session_id: str, entry_id: str, transition: WatchTransition, move_id: str) -> None:
    previous = state["sessions"].get(session_id)
    current = WatchedSession(entry_id=entry_id, state=transition, move_id=move_id)
    if previous and previous["entry_id"] == entry_id and "refusal" in previous:
        current["refusal"] = previous["refusal"]
    state["sessions"][session_id] = current


def _continue_session(session_id: str, entry_id: str) -> ContinuationDelivered | ContinuationNotDelivered:
    # Shutdown imports this watcher for its hold and cancellation; resolve its delivery boundary at use.
    messenger = cast(LiveSessionMessenger, cast(object, import_module("settle")))
    try:
        delivery = messenger.send_message(f"session:{session_id}", "Quota wait ended", "Continue where you stopped.",
            key=f"quota-continue-{session_id}-{entry_id}")
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        return ContinuationNotDelivered(str(error).splitlines()[0] if str(error) else type(error).__name__)
    if delivery["kind"] == "sent":
        return ContinuationDelivered()
    return ContinuationNotDelivered((delivery["reason"].splitlines() or ["queued"])[0])


def _reading_values_fingerprint() -> str:
    values: dict[str, dict[str, object]] = {}
    for identity, reading in usage_store.load().items():
        values[identity] = {}
        for name, window in (("weekly", reading.get("weekly")), ("five_hour", reading.get("five_hour"))):
            if window is not None:
                values[identity][name] = {"left": window["left"], "resets": window["resets"]}
        if "credits" in reading:
            values[identity]["credits"] = reading["credits"]["balance"]
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def _run(now: datetime, dry_run: bool) -> list[str]:
    state = _load_state()
    original_state = json.dumps(state, sort_keys=True)
    open_ids = {saved["session"]["session_id"] for saved in record.all_session_records()}
    entries = moves.read_moves()
    ended = {entry["move_id"] for entry in entries if entry["kind"] == "wait ended"}
    for entry in entries:
        if (entry["tool"] == "claude" and entry["kind"] == "waited" and entry["move_id"] not in ended
                and datetime.fromisoformat(entry["reset"]) <= now and not dry_run):
            moves.append_move(moves.WaitEndedMove(kind="wait ended", at=now.isoformat(), tool="claude",
                subject=entry["subject"], move_id=entry["move_id"]))
    groups: dict[str, list[tuple[inventory_module.Session, TranscriptRefusal, refusals.AccountRefusal]]] = {}
    lines: list[str] = []
    selected_groups = accounts.group_accounts()
    unconfigured = {group for group in accounts.CLAUDE_GROUPS if isinstance(accounts.group_label(selected_groups, group), accounts.GroupNotConfigured)}
    reported_unconfigured: set[accounts.ClaudeGroup] = set()
    detected = {session["sessionId"]: session_refusal(session["sessionId"]) for session in sessions.live_sessions()
        if session["sessionId"] not in open_ids}
    refused_ids = frozenset(session_id for session_id, refusal in detected.items() if isinstance(refusal, TranscriptRefusal))
    refusing_logins: set[str] = set()
    owners = live_owners(only=refused_ids) if refused_ids else []
    waiting_groups: set[accounts.ClaudeGroup] = set()
    blocked_groups: set[accounts.ClaudeGroup] = set()
    tick_logins = frozenset(identity.login.casefold() for session in owners
        if isinstance(identity := accounts.account_identity(Path(session.get("config_dir", str(accounts.main_config_dir())))),
                      agent_accounts.KnownClaudeAccount))
    tick_excluded = accounts.account_labels_for_logins(tick_logins)
    # Preparation is a read-only gate: a broken destination changes no group or refusal state.
    for session in owners:
        group_name = accounts.session_group(session)
        if group_name == "ungrouped":
            continue
        if group_name in unconfigured:
            if group_name not in reported_unconfigured:
                lines.append(accounts.group_configuration_line(group_name))
                reported_unconfigured.add(group_name)
            continue
        refusal = detected[session["session_id"]]
        if isinstance(refusal, NoSessionRefusal):
            continue
        previous = state["sessions"].get(session["session_id"])
        if previous and previous["entry_id"] == refusal.entry_id and previous["state"] in ("requested", "cancelled", "continued", "group selected"):
            continue
        identity = accounts.account_identity(Path(session.get("config_dir", str(accounts.main_config_dir()))))
        if isinstance(identity, agent_accounts.UnreadableClaudeAccount):
            continue
        if previous and previous["entry_id"] == refusal.entry_id and "refusal" in previous:
            first = _first_refusal(previous["refusal"])
            window = first.window
            preflight_reset = first.reset
        else:
            reading = refusals.stored_reading("claude", identity.login, now)
            window = _refusal_window(refusal.window, reading, refusal.refused_at.at)
            preflight_reset: refusals.KnownReset | refusals.NoResetKnown = refusals.NoResetKnown()
            if isinstance(window, refusals.KnownWindow) and isinstance(reading, (refusals.FreshReading, refusals.StaleReading)):
                value = reading.weekly if window.name == "weekly" else reading.five_hour
                if isinstance(value, refusals.ReadingWindow):
                    preflight_reset = value.reset
        if isinstance(refusal.reset, refusals.KnownReset):
            preflight_reset = refusal.reset
        if previous and previous["entry_id"] == refusal.entry_id and previous["state"] == "waited":
            waiting_groups.add(group_name)
            continue
        if isinstance(window, refusals.KnownWindow) and window.name == "five_hour" and isinstance(preflight_reset, refusals.KnownReset) and timedelta(0) < preflight_reset.at - now < timedelta(minutes=20):
            waiting_groups.add(group_name)
            continue
        chosen = accounts.placement(group_name, now, exclude=tick_excluded)
        if not isinstance(chosen.reason, accounts.GroupNoRoom):
            assert isinstance(chosen.label, str)
            prepared = accounts.prepared_account(chosen.label, resume=True)
            if isinstance(prepared, accounts.AccountRefused):
                blocked_groups.add(group_name)
                lines.append(f"{group_name}: move refused: {prepared.reason}")
    for session in owners:
        session_id = session["session_id"]
        if session_id in open_ids or accounts.session_group(session) in blocked_groups | unconfigured:
            continue
        refusal = detected[session_id]
        if isinstance(refusal, NoSessionRefusal):
            continue
        previous = state["sessions"].get(session_id)
        if previous and previous["entry_id"] == refusal.entry_id and previous["state"] in ("requested", "cancelled", "continued", "group selected"):
            continue
        identity = accounts.account_identity(Path(session.get("config_dir", str(accounts.main_config_dir()))))
        if isinstance(identity, agent_accounts.UnreadableClaudeAccount):
            lines.append(f"{session['name']}: refused; account unreadable")
            continue
        if previous and previous["entry_id"] == refusal.entry_id and "refusal" in previous:
            account_refusal = _first_refusal(previous["refusal"])
        else:
            reading = refusals.stored_reading("claude", identity.login, now)
            window = _refusal_window(refusal.window, reading, refusal.refused_at.at)
            reset: refusals.KnownReset | refusals.NoResetKnown = refusals.NoResetKnown()
            if isinstance(window, refusals.KnownWindow) and isinstance(reading, (refusals.FreshReading, refusals.StaleReading)):
                value = reading.weekly if window.name == "weekly" else reading.five_hour
                if isinstance(value, refusals.ReadingWindow):
                    reset = value.reset
            account_refusal = refusals.AccountRefusal("claude", identity.login, window, reset, refusal.refused_at.at)
            # A watch entry without its first observation takes the stored refusal written at that time.
            if previous and previous["entry_id"] == refusal.entry_id:
                saved = refusals.load().get(refusals.key("claude", identity.login))
                if saved is not None and saved.at == refusal.refused_at.at:
                    account_refusal = saved
            if not dry_run:
                observation = FirstRefusalObservation(login=identity.login,
                    window=account_refusal.window.name if isinstance(account_refusal.window, refusals.KnownWindow) else "unknown",
                    at=account_refusal.at.isoformat())
                if isinstance(account_refusal.reset, refusals.KnownReset):
                    observation["reset"] = account_refusal.reset.at.isoformat()
                if not previous or previous["entry_id"] != refusal.entry_id:
                    refusals.write_refusal(account_refusal)
                    _set_transition(state, session_id, refusal.entry_id, "retry", "")
                state["sessions"][session_id]["refusal"] = observation
                _atomic_json(state_root() / "refusal_watch.json", state)
        window, reset = account_refusal.window, account_refusal.reset
        if previous and previous["entry_id"] == refusal.entry_id and previous["state"] == "waited":
            waited = next((entry for entry in entries if entry["kind"] == "waited" and entry["move_id"] == previous["move_id"]), None)
            if waited is not None and datetime.fromisoformat(waited["reset"]) <= now:
                if dry_run:
                    lines.append(f"{session['name']}: refused; continue on its current account")
                else:
                    delivery = _continue_session(session_id, refusal.entry_id)
                    if isinstance(delivery, ContinuationNotDelivered):
                        lines.append(f"{session['name']}: continuation not delivered: {delivery.reason}")
                    else:
                        _set_transition(state, session_id, refusal.entry_id, "continued", previous["move_id"])
                        lines.append(f"{session['name']}: continued on its current account")
                continue
        refusing_logins.add(identity.login.casefold())
        wait_reset = refusal.reset if isinstance(refusal.reset, refusals.KnownReset) else reset
        subject = session_id + ": " + session["name"]
        if (isinstance(window, refusals.KnownWindow) and window.name == "five_hour"
                and isinstance(wait_reset, refusals.KnownReset) and timedelta(0) < wait_reset.at - now < timedelta(minutes=20)):
            lines.append(f"{session['name']}: refused; wait until {wait_reset.at.astimezone(agent_accounts.user_zone()).isoformat()}")
            if not dry_run and not any(entry["tool"] == "claude" and entry["subject"].split(": ", 1)[0] == session_id
                    and entry["kind"] == "waited" and entry["reset"] == wait_reset.at.isoformat() for entry in entries):
                move_id = uuid4().hex
                moves.append_move(moves.WaitedMove(kind="waited", at=now.isoformat(), tool="claude",
                    subject=subject, move_id=move_id, window="five_hour", reset=wait_reset.at.isoformat()))
                _set_transition(state, session_id, refusal.entry_id, "waited", move_id)
            continue
        group_name = accounts.session_group(session)
        key = identity.login.casefold() if group_name == "ungrouped" else "group:" + group_name
        groups.setdefault(key, []).append((session, refusal, account_refusal))
    fingerprint = _reading_values_fingerprint()
    if dry_run:
        lines.insert(0, accounts.group_status_line(selected_groups))
    all_members: dict[accounts.ClaudeGroup, list[inventory_module.Session]] = accounts.live_group_members() if any(key.startswith("group:") for key in groups) or accounts.groups_file().exists() else {"units": [], "showrunners": []}
    moved_groups: set[accounts.ClaudeGroup] = set()
    no_room_groups: set[accounts.ClaudeGroup] = set()
    for login, group in groups.items():
        group_name = cast(accounts.ClaudeGroup, login.removeprefix("group:")) if login.startswith("group:") else "ungrouped"
        if group_name in waiting_groups or group_name in blocked_groups:
            continue
        source_identity = accounts.account_identity(Path(group[0][0].get("config_dir", str(accounts.main_config_dir()))))
        if isinstance(source_identity, agent_accounts.UnreadableClaudeAccount):
            continue
        source_dir = Path(group[0][0].get("config_dir", str(accounts.main_config_dir())))
        default_identity = accounts.account_identity(accounts.main_config_dir())
        uses_default_login = (isinstance(default_identity, agent_accounts.KnownClaudeAccount)
                              and default_identity.login.casefold() == source_identity.login.casefold())
        source = "default" if source_dir == accounts.main_config_dir() or uses_default_login else source_identity.label
        excluded = {source, source_identity.label, *tick_excluded}
        if isinstance(default_identity, agent_accounts.KnownClaudeAccount) and default_identity.login.casefold() in refusing_logins:
            excluded.add("default")
        for named in accounts.checked_named_accounts():
            identity = accounts.account_identity(named.dir)
            if isinstance(identity, agent_accounts.KnownClaudeAccount) and identity.login.casefold() in refusing_logins:
                excluded.add(canonical_label(named.label))
        if group_name == "ungrouped":
            chosen = accounts.pick("claude", now, exclude=frozenset(excluded), keep=accounts.NoKeep())
        else:
            placed = accounts.placement(group_name, now, exclude=frozenset(excluded))
            if isinstance(placed.reason, accounts.GroupNoRoom):
                chosen = placed.reason.outcome
            else:
                assert isinstance(placed.label, str) and isinstance(placed.dir, Path)
                chosen = accounts.PickedAccount(placed.label, placed.dir, 0, accounts.NoResetKnown())
        if isinstance(chosen, accounts.NoAccountWithRoom):
            if group_name != "ungrouped":
                no_room_groups.add(group_name)
            text = source + " refused; " + accounts.no_room_line("claude", chosen)
            lines.extend(f"{session['name']}: refused; {text}" for session, _, _ in group)
            if not dry_run:
                for session, refusal, _ in group:
                    prior = state["sessions"].get(session["session_id"])
                    if not prior or prior["entry_id"] != refusal.entry_id or prior["state"] != "no room":
                        move_id = uuid4().hex
                        moves.append_move(moves.NoRoomMove(kind="no room", at=now.isoformat(), tool="claude",
                            subject=session["session_id"] + ": " + session["name"], move_id=move_id,
                            earliest_reset=chosen.earliest_reset.at.isoformat() if isinstance(chosen.earliest_reset, accounts.EarliestReset) else ""))
                        _set_transition(state, session["session_id"], refusal.entry_id, "no room", move_id)
                notice_login = source_identity.login.casefold()
                if state["notices"].get(notice_login) != fingerprint:
                    _notify_no_room(text, now)
                    state["notices"][notice_login] = fingerprint
            continue
        if group_name != "ungrouped":
            lines.extend(f"{session['name']}: refused group {group_name}; move to {chosen.label}" for session in all_members[group_name]
                         if session["session_id"] not in {item[0]["session_id"] for item in group})
        for session, _, _ in group:
            lines.append(f"{session['name']}: refused; move from {source} to {chosen.label}")
            prior = state["sessions"].get(session["session_id"])
            if not dry_run and prior and prior["state"] == "no room":
                moves.append_move(moves.RoomAgainMove(kind="room again", at=now.isoformat(), tool="claude",
                    subject=session["session_id"] + ": " + session["name"], move_id=prior["move_id"], label=chosen.label))
        if dry_run:
            if group_name != "ungrouped":
                selected_groups[group_name] = chosen.label
                moved_groups.add(group_name)
            continue
        first_refusal = group[0][2]
        window_name = first_refusal.window.name if isinstance(first_refusal.window, refusals.KnownWindow) else "unknown"
        reset_text = first_refusal.reset.at.astimezone(agent_accounts.user_zone()).isoformat() if isinstance(first_refusal.reset, refusals.KnownReset) else "unknown reset"
        why = f"{source} refused: {window_name} limit until {reset_text}"
        if group_name != "ungrouped":
            prepared = accounts.prepared_account(chosen.label, resume=True)
            if isinstance(prepared, accounts.AccountRefused):
                lines.append(f"{group_name}: move refused: {prepared.reason}")
                continue
            accounts.set_group_account(group_name, chosen.label, why=why, now=now)
            selected_groups[group_name] = chosen.label
            moved_groups.add(group_name)
            for session, refusal, _ in group:
                _set_transition(state, session["session_id"], refusal.entry_id, "group selected", "")
            # The chosen group account survives a member's failed request or interrupted delivery.
            _atomic_json(state_root() / "refusal_watch.json", state)
            ids = tuple(session["session_id"] for session in all_members[group_name]
                        if not accounts.session_on_account(session, chosen.label))
        else:
            ids = tuple(session["session_id"] for session, _, _ in group)
        outcomes = accounts.move_sessions(ids, chosen.label, why=why) if ids else ()
        by_id = {session["session_id"]: refusal for session, refusal, _ in group}
        for outcome in outcomes:
            if outcome.session_id not in by_id:
                if isinstance(outcome, accounts.SessionMoveRefused):
                    lines.append(f"{outcome.session_id}: move refused: {outcome.reason}")
                continue
            refusal = by_id[outcome.session_id]
            _set_transition(state, outcome.session_id, refusal.entry_id,
                "requested" if isinstance(outcome, accounts.SessionMoveRequested) else "group selected" if group_name != "ungrouped" else "retry",
                outcome.request_id if isinstance(outcome, accounts.SessionMoveRequested) else "")
            if isinstance(outcome, accounts.SessionMoveRefused):
                lines.append(f"{outcome.session_id}: move refused: {outcome.reason}")
    # A committed group choice is stable. Complete its off-target members without choosing again.
    if accounts.groups_file().exists():
        for group_name in accounts.CLAUDE_GROUPS:
            if group_name in moved_groups or group_name in waiting_groups or group_name in blocked_groups or group_name in no_room_groups:
                continue
            target = accounts.group_label(selected_groups, group_name)
            if isinstance(target, accounts.GroupNotConfigured):
                continue
            members = [session for session in all_members[group_name]
                       if session["session_id"] not in open_ids and not accounts.session_on_account(session, target)
                       and not (session["session_id"] in state["sessions"] and state["sessions"][session["session_id"]]["state"] == "cancelled"
                                and isinstance(detected.get(session["session_id"]), TranscriptRefusal)
                                and state["sessions"][session["session_id"]]["entry_id"] == cast(TranscriptRefusal, detected[session["session_id"]]).entry_id)]
            if not members:
                continue
            directory = accounts.main_config_dir() if target == "default" else accounts.account_dir(target)
            identity = accounts.account_identity(directory)
            if isinstance(identity, agent_accounts.UnreadableClaudeAccount):
                lines.append(f"{group_name}: move refused: account unreadable")
                continue
            held = refusals.load().get(refusals.key("claude", identity.login))
            if held is not None and not refusals.resolved(held, refusals.stored_reading("claude", identity.login, now), now):
                lines.append(f"{group_name}: move refused: {target} holds an unresolved refusal")
                continue
            prepared = accounts.prepared_account(target, resume=True)
            if isinstance(prepared, accounts.AccountRefused):
                lines.append(f"{group_name}: move refused: {prepared.reason}")
                continue
            lines.extend(f"{session['name']}: moving to {target} ({group_name})" for session in members)
            if not dry_run:
                _ = accounts.move_sessions(tuple(session["session_id"] for session in members), target,
                    why=f"complete {group_name} move to {target}")
    if not dry_run and json.dumps(state, sort_keys=True) != original_state:
        _atomic_json(state_root() / "refusal_watch.json", state)
    return lines


def run(now: datetime | None = None, *, dry_run: bool = False) -> list[str]:
    at = now or datetime.now(timezone.utc)
    if dry_run:
        return _run(at, True)
    with _watch_lock():
        return _run(at, False)


class WatchCommandLine(argparse.Namespace):
    dry_run: bool = False
    assume_refused: list[str] | None = None
    reset_in: float | None = None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--dry-run", action="store_true")
    _ = parser.add_argument("--assume-refused", nargs=2, metavar=("SESSION", "WINDOW"))
    _ = parser.add_argument("--reset-in", type=float)
    args = parser.parse_args(argv, namespace=WatchCommandLine())
    if args.reset_in is not None and not args.assume_refused:
        parser.error("--reset-in requires --assume-refused")
    if args.assume_refused and not args.dry_run:
        name, window = args.assume_refused
        if window not in ("weekly", "five-hour"):
            parser.error("window must be weekly or five-hour")
        matching = [session for session in live_owners() if session["session_id"] == name or session["name"] == name]
        if len(matching) != 1:
            parser.error("name or id must identify one local live owner")
        now = datetime.now(timezone.utc)
        held = AssumedRefusal(uuid=uuid4().hex, timestamp=now.isoformat(),
            window="weekly" if window == "weekly" else "five_hour",
            reset=(now + timedelta(minutes=float(args.reset_in))).isoformat() if args.reset_in is not None else "")
        _atomic_json(assumed_file(matching[0]["session_id"]), held)
    for line in run(dry_run=bool(args.dry_run)):
        print(line)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(f"refusal watcher: {str(error).splitlines()[0]}", file=sys.stderr)
        sys.exit(1)
