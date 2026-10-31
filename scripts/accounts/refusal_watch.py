#!/usr/bin/env python3
"""Find local Claude quota refusals and start safe, account-grouped moves."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import socket
import subprocess
import sys
import tempfile
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
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
from scripts.accounts import accounts, moves, next_moves, refusals, role_map
from scripts.whoami import account
from scripts.whoami import agent_accounts
from scripts.whoami.note_file import canonical_label
from scripts.whoami import usage_store
from quota_alert import (RefusedAt, _refusal_window, blocked_next_move_line, blocked_next_move_reason_key, notify_blocked_next_move,  # pyright: ignore[reportPrivateUsage]
                         BlockedHeadReason as BlockedHeadReason, NoTargetReading as NoTargetReading,
                         TargetReadingUnavailable as TargetReadingUnavailable,
                         TargetLoginRenewalFailed as TargetLoginRenewalFailed,
                         TargetAccountUnavailable as TargetAccountUnavailable,
                         TargetAtOrBelowThreshold as TargetAtOrBelowThreshold,
                         TargetUnresolvedRefusal as TargetUnresolvedRefusal,
                         TargetPreparationRefused as TargetPreparationRefused,
                         TargetIsCurrentAccount as TargetIsCurrentAccount)

if TYPE_CHECKING:
    from scripts.shutdown import inventory as inventory_module
    from scripts.herdr import herdr
    from scripts.message import sessions
else:
    inventory_module = import_module("inventory")
    sessions = import_module("sessions")
    herdr = import_module("herdr")


@dataclass(frozen=True)
class NoSessionRefusal:
    """The session has no current transcript or assumed quota refusal."""


@dataclass(frozen=True)
class ContinuationDelivered:
    """The live owner accepted its continuation after the quota wait."""


@dataclass(frozen=True)
class ContinuationNotDelivered:
    reason: str


class LiveSessionMessenger(Protocol):
    def send_message(self, recipient: str, summary: str, text: str, *, key: str = "") -> Mapping[str, str]: ...
    def message_was_delivered(self, outcome: Mapping[str, object]) -> bool: ...


@dataclass(frozen=True)
class TranscriptRefusal:
    entry_id: str
    refused_at: RefusedAt
    window: refusals.KnownWindow | refusals.UnknownWindow
    text: str
    reset: refusals.KnownReset | refusals.NoResetKnown = field(default_factory=refusals.NoResetKnown)


@dataclass(frozen=True)
class ProofRefusal:
    entry_id: str
    refused_at: RefusedAt
    window: refusals.KnownWindow | refusals.UnknownWindow
    reset: refusals.KnownReset | refusals.NoResetKnown = field(default_factory=refusals.NoResetKnown)


class AssumedRefusalFile(TypedDict):
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
    auto_move_notices: NotRequired[dict[str, str]]
    auto_move_holds: NotRequired[dict[str, HoldStarted]]


class HoldStartedDocument(TypedDict):
    account: str
    at: str
    move_id: str
    percent: float
    minutes: NotRequired[float]
    source: NotRequired[dict[str, object]]


class WatchStateDocument(TypedDict):
    sessions: dict[str, WatchedSession]
    notices: dict[str, str]
    auto_move_notices: NotRequired[dict[str, str]]
    auto_move_holds: NotRequired[dict[str, HoldStartedDocument]]


@dataclass(frozen=True)
class LegacyHoldDuration:
    """A pre-duration hold still uses its entry's fixed minutes."""


@dataclass(frozen=True)
class GroupSelection:
    account: str | accounts.GroupNotConfigured
    move_id: str | accounts.NoGroupTransition


def _group_selection(selected: accounts.GroupAccounts, group: accounts.ClaudeGroup) -> GroupSelection:
    transition = accounts.group_transition(group)
    return GroupSelection(accounts.group_label(selected, group),
                          transition.move_id if isinstance(transition, accounts.GroupTransition) else transition)


@dataclass(frozen=True)
class HoldStarted:
    account: str
    at: datetime
    move_id: str | accounts.NoGroupTransition
    percent: float
    duration: next_moves.ResolvedHold | LegacyHoldDuration = LegacyHoldDuration()

    def __post_init__(self) -> None:
        if self.at.utcoffset() is None:
            raise ValueError("hold start needs a zone")

    def is_current(self, selection: GroupSelection, percent: float) -> bool:
        return (self.account == selection.account and self.move_id == selection.move_id
                and self.percent == percent)


@dataclass(frozen=True)
class LeftAtMost:
    percent: float
    hold_mode: next_moves.HoldMode = next_moves.FixedHold(0)


@dataclass(frozen=True)
class AutoMoveRule:
    entry_id: str
    group: accounts.ClaudeGroup
    to: str
    when: LeftAtMost


@dataclass(frozen=True)
class UnreadableNextMoveLog:
    reason: str


def read_auto_move_rules() -> tuple[AutoMoveRule | UnreadableNextMoveLog, ...]:
    try:
        return tuple(AutoMoveRule(entry.id, entry.group, entry.to,
                                  LeftAtMost(entry.at_percent, entry.hold_mode))
                     for entry in next_moves.read_next_moves() if isinstance(entry, next_moves.ClaudeGroupMove))
    except next_moves.NextMoveError as error:
        return (UnreadableNextMoveLog(str(error)),)


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
    raw_auto_notices = held.get("auto_move_notices")
    if isinstance(raw_auto_notices, dict):
        result["auto_move_notices"] = {key: value for key, value in cast(dict[str, object], raw_auto_notices).items()
                                       if isinstance(value, str)}
    raw_holds = held.get("auto_move_holds")
    if isinstance(raw_holds, dict):
        holds: dict[str, HoldStarted] = {}
        for name, raw in cast(dict[str, object], raw_holds).items():
            if not isinstance(raw, dict):
                continue
            fields = cast(dict[str, object], raw)
            label, at = fields.get("account"), fields.get("at")
            move_id, percent = fields.get("move_id"), fields.get("percent")
            if (isinstance(label, str) and isinstance(at, str) and isinstance(move_id, str)
                    and isinstance(percent, (int, float)) and not isinstance(percent, bool)
                    and 0 <= percent <= 100 and math.isfinite(percent)):
                try:
                    holds[name] = HoldStarted(label, datetime.fromisoformat(at),
                                             move_id if move_id else accounts.NoGroupTransition(), float(percent),
                                             _decode_hold_duration(fields))
                except ValueError:
                    continue
        result["auto_move_holds"] = holds
    return result


def _decode_hold_duration(fields: dict[str, object]) -> next_moves.ResolvedHold | LegacyHoldDuration:
    if "minutes" not in fields:
        return LegacyHoldDuration()
    minutes, source = fields.get("minutes"), fields.get("source")
    if (not isinstance(minutes, (int, float)) or isinstance(minutes, bool)
            or minutes < 0 or not math.isfinite(minutes) or not isinstance(source, dict)):
        raise ValueError("invalid hold duration")
    details = cast(dict[str, object], source)
    kind = details.get("kind")
    if kind == "fixed":
        return next_moves.ResolvedHold(float(minutes), next_moves.FixedHold(float(minutes)))
    if kind == "no usable pace":
        return next_moves.ResolvedHold(float(minutes), next_moves.NoUsablePace())
    pace = details.get("points_per_hour")
    if (kind == "pace" and isinstance(pace, (int, float)) and not isinstance(pace, bool)
            and pace > 0 and math.isfinite(pace)):
        return next_moves.ResolvedHold(float(minutes), next_moves.PaceEstimate(float(pace)))
    raise ValueError("invalid hold source")


def _hold_document(hold: HoldStarted) -> HoldStartedDocument:
    document = HoldStartedDocument(account=hold.account, at=hold.at.isoformat(),
                                   move_id=hold.move_id if isinstance(hold.move_id, str) else "", percent=hold.percent)
    if isinstance(hold.duration, next_moves.ResolvedHold):
        document["minutes"] = hold.duration.minutes
        source = hold.duration.source
        document["source"] = ({"kind": "pace", "points_per_hour": source.points_per_hour}
                              if isinstance(source, next_moves.PaceEstimate) else
                              {"kind": "no usable pace"} if isinstance(source, next_moves.NoUsablePace)
                              else {"kind": "fixed"})
    return document


def _state_document(state: WatchState) -> WatchStateDocument:
    document = WatchStateDocument(sessions=state["sessions"], notices=state["notices"])
    if "auto_move_notices" in state:
        document["auto_move_notices"] = state["auto_move_notices"]
    if "auto_move_holds" in state:
        document["auto_move_holds"] = {
            name: _hold_document(hold)
            for name, hold in state["auto_move_holds"].items()
        }
    return document


def _save_state(state: WatchState) -> None:
    _atomic_json(state_root() / "refusal_watch.json", _state_document(state))


def started_next_move_holds(entries: tuple[next_moves.NextMove, ...]) -> dict[str, next_moves.StartedHold] | next_moves.WatchStateUnreadable:
    """Read current heads' resolved holds without reacquiring the editor's watch lock."""
    if not entries:
        return {}
    try:
        state = _load_state()
    except (OSError, ValueError, OverflowError):
        return next_moves.WatchStateUnreadable()
    holds = state.get("auto_move_holds", {})
    if not holds:
        return {}
    selected = accounts.group_accounts()
    heads: set[accounts.ClaudeGroup] = set()
    started: dict[str, next_moves.StartedHold] = {}
    for entry in entries:
        if not isinstance(entry, next_moves.ClaudeGroupMove) or entry.group in heads:
            continue
        heads.add(entry.group)
        if isinstance(entry.hold_mode, next_moves.FixedHold) and entry.hold_mode.minutes == 0:
            continue
        hold = holds.get(entry.id)
        if hold is None or not isinstance(hold.duration, next_moves.ResolvedHold):
            continue
        selection = _group_selection(selected, entry.group)
        if hold.is_current(selection, entry.at_percent):
            started[entry.id] = next_moves.StartedHold(hold.at, hold.duration)
    return started


@contextmanager
def _watch_lock() -> Generator[None]:
    # The synchronization file does not initialize account state during a refused preparation.
    identity = hashlib.sha256(str(state_root()).encode()).hexdigest()
    lock = Path(tempfile.gettempdir()) / ("claude-refusal-watch-" + identity + ".lock")
    with lock.open("a") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield


@dataclass(frozen=True)
class LockedNextMoveEdit:
    """An edit whose watch and log locks remain held through cleanup and commit."""

    before: tuple[next_moves.NextMove, ...]

    def commit(self, entries: tuple[next_moves.NextMove, ...]) -> None:
        heads: dict[next_moves.NextMoveGroup, str] = {}
        for entry in self.before:
            _ = heads.setdefault(entry.group, entry.id)
        new_heads: dict[next_moves.NextMoveGroup, str] = {}
        for entry in entries:
            _ = new_heads.setdefault(entry.group, entry.id)
        discarded = {entry.id for entry in self.before} - {entry.id for entry in entries}
        discarded.update(entry_id for group, entry_id in heads.items() if new_heads.get(group) != entry_id)
        prior_modes = {entry.id: entry.hold_mode for entry in self.before if isinstance(entry, next_moves.ClaudeGroupMove)}
        changed_modes = {entry.id for entry in entries if isinstance(entry, next_moves.ClaudeGroupMove)
                         and entry.id in prior_modes and prior_modes[entry.id] != entry.hold_mode}
        if discarded or changed_modes:
            state = _load_state()
            original = deepcopy(state)
            for entry_id in discarded | changed_modes:
                _ = state.get("auto_move_holds", {}).pop(entry_id, None)
            for entry_id in discarded:
                _ = state.get("auto_move_notices", {}).pop(entry_id, None)
            if state != original:
                _save_state(state)
            if discarded:
                import quota_alert
                with quota_alert.state_file() as quota_state:
                    for entry_id in discarded:
                        _ = quota_state.get("workers_move_notices", {}).pop(entry_id, None)
        next_moves.write_next_moves(entries)


@contextmanager
def editing_next_moves() -> Generator[LockedNextMoveEdit]:
    """Resolve edits under watch then log locks; cleanup must precede the log write."""
    with _watch_lock(), next_moves.next_moves_lock():
        yield LockedNextMoveEdit(next_moves.read_next_moves())


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
    except OSError:
        return NoSessionRefusal()
    return _transcript_refusal(paths)


def _transcript_refusal(paths: list[Path]) -> TranscriptRefusal | NoSessionRefusal:
    try:
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


def assumed_refusal(session_id: str) -> ProofRefusal | NoSessionRefusal:
    value = _object(assumed_file(session_id))
    if not value:
        return NoSessionRefusal()
    if (not all(isinstance(value.get(key), str) for key in ("uuid", "timestamp", "window", "reset"))
            or value["window"] not in ("weekly", "five_hour")):
        raise ValueError("invalid assumed refusal")
    held = cast(AssumedRefusalFile, cast(object, value))
    at = datetime.fromisoformat(held["timestamp"])
    reset = refusals.KnownReset(datetime.fromisoformat(held["reset"])) if held["reset"] else refusals.NoResetKnown()
    if at.tzinfo is None or isinstance(reset, refusals.KnownReset) and reset.at.tzinfo is None:
        raise ValueError("assumed refusal time needs an offset")
    return ProofRefusal(held["uuid"], RefusedAt(at), refusals.KnownWindow(held["window"]), reset)


def session_refusal(session_id: str) -> TranscriptRefusal | ProofRefusal | NoSessionRefusal:
    assumed = assumed_refusal(session_id)
    return transcript_refusal(session_id) if isinstance(assumed, NoSessionRefusal) else assumed


def session_refusals(session_ids: list[str]) -> dict[str, TranscriptRefusal | ProofRefusal | NoSessionRefusal]:
    """Find this pass's transcripts with one directory scan per project."""
    detected: dict[str, TranscriptRefusal | ProofRefusal | NoSessionRefusal] = {
        session_id: assumed_refusal(session_id) for session_id in session_ids}
    wanted = {session_id + ".jsonl": session_id for session_id, refusal in detected.items()
              if isinstance(refusal, NoSessionRefusal)}
    if not wanted:
        return detected
    paths: dict[str, list[Path]] = {session_id: [] for session_id in wanted.values()}
    # Keep the index within this pass. New transcripts and exchanges are read next pass.
    try:
        with os.scandir(Path.home() / ".claude/projects") as projects:
            for project in projects:
                try:
                    if not project.is_dir():
                        continue
                    with os.scandir(project.path) as transcripts:
                        for transcript in transcripts:
                            if transcript.name in wanted:
                                paths[wanted[transcript.name]].append(Path(transcript.path))
                except OSError:
                    # A project may permit known-file reads without directory listing.
                    for name, session_id in wanted.items():
                        candidate = Path(project.path) / name
                        try:
                            if candidate.exists():
                                paths[session_id].append(candidate)
                        except OSError:
                            continue
    except OSError:
        pass
    for session_id, candidates in paths.items():
        detected[session_id] = _transcript_refusal(candidates)
    return detected


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
        _save_state(state)
        clear_assumed(session_id)


NO_SESSION_FILTER: frozenset[str] = frozenset()
NO_MOVED_GROUPS: frozenset[accounts.ClaudeGroup] = frozenset()


@dataclass(frozen=True)
class NoSessionObservation:
    """No earlier report can prove that completion work is unchanged."""


@dataclass(frozen=True)
class ReportSessionIdentities:
    identities: frozenset[tuple[str, int, str]]


_report_sessions: ContextVar[NoSessionObservation | ReportSessionIdentities] = ContextVar(
    "refusal_report_sessions", default=NoSessionObservation())


def _session_identities() -> ReportSessionIdentities:
    return ReportSessionIdentities(frozenset((row["sessionId"], row["pid"], row.get("procStart", ""))
                                           for row in sessions.live_sessions()))


@contextmanager
def session_observation() -> Generator[None]:
    """Capture registry identities before the timer's first report scan."""
    token = _report_sessions.set(_session_identities())
    try:
        yield
    finally:
        _report_sessions.reset(token)


@herdr.process_action()
def _action_group_members() -> dict[accounts.ClaudeGroup, list[inventory_module.ListedSession]]:
    """Read members fresh before refusal, queued or committed group moves."""
    return accounts.live_group_members()


@herdr.process_action()
def live_owners(*, only: frozenset[str] = NO_SESSION_FILTER) -> list[inventory_module.Session]:
    owners: dict[str, inventory_module.Session] = {}
    home_state = account.home_account_state()
    logins = {identity.login for session in sessions.live_sessions()
              if not only or session["sessionId"] in only
              if (identity := account.account_of(session["pid"], home_state=home_state)) is not None}
    for login in sorted(logins):
        for session in inventory_module.inventory(login, only, refresh_desktops=False)["sessions"]:
            if session["kind"] != "worker":
                owners[session["session_id"]] = session
    return list(owners.values())


def _notify_no_room(text: str, now: datetime) -> bool:
    import quota_alert
    config = quota_alert.load_config()
    return quota_alert.deliver_notice([quota_alert.Job("", recipient, text, "refusal")
        for recipient in quota_alert.recipients(config)], now.isoformat())




def _blocked_head_line(rule: AutoMoveRule, reason: BlockedHeadReason) -> str:
    return blocked_next_move_line(rule.group, rule.to, rule.entry_id, reason)


def _notify_blocked_head(rule: AutoMoveRule, reason: BlockedHeadReason, now: datetime) -> bool:
    return notify_blocked_next_move(rule.group, rule.to, rule.entry_id, reason, now)


def _record_blocked_head(state: WatchState, rule: AutoMoveRule, reason: BlockedHeadReason,
                         now: datetime, dry_run: bool) -> str:
    if not dry_run:
        notices = state.setdefault("auto_move_notices", {})
        fingerprint = blocked_next_move_reason_key(reason)
        if notices.get(rule.entry_id) != fingerprint:
            _ = notices.pop(rule.entry_id, None)
            if _notify_blocked_head(rule, reason, now):
                notices[rule.entry_id] = fingerprint
    return _blocked_head_line(rule, reason)


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
    messenger = cast(LiveSessionMessenger, cast(object, import_module("restart_support")))
    try:
        delivery = messenger.send_message(f"session:{session_id}", "Quota wait ended", "Continue where you stopped.",
            key=f"quota-continue-{session_id}-{entry_id}")
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        return ContinuationNotDelivered(str(error).splitlines()[0] if str(error) else type(error).__name__)
    if messenger.message_was_delivered(delivery):
        return ContinuationDelivered()
    return ContinuationNotDelivered((delivery.get("reason", "send not delivered").splitlines() or ["queued"])[0])


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


def _run(now: datetime, dry_run: bool, proof_group: frozenset[str] = NO_SESSION_FILTER, *,
         refresh_usage: Callable[[], None] = lambda: None) -> list[str]:
    state = _load_state()
    original_state = deepcopy(state)
    configured = read_auto_move_rules() if not proof_group else ()
    # An unreadable queue cannot prove a group has nothing next.
    pending_groups = (set(accounts.CLAUDE_GROUPS) if any(isinstance(rule, UnreadableNextMoveLog) for rule in configured)
                      else {rule.group for rule in configured if isinstance(rule, AutoMoveRule)})
    entries = moves.read_moves()
    ended = {entry["move_id"] for entry in entries if entry["kind"] == "wait ended"}
    for entry in entries:
        if (entry["tool"] == "claude" and entry["kind"] == "waited" and entry["move_id"] not in ended
                and (not proof_group or entry["subject"].split(": ", 1)[0] in proof_group)
                and datetime.fromisoformat(entry["reset"]) <= now and not dry_run):
            moves.append_move(moves.WaitEndedMove(kind="wait ended", at=now.isoformat(), tool="claude",
                subject=entry["subject"], move_id=entry["move_id"]))
    groups: dict[str, list[tuple[inventory_module.Session, TranscriptRefusal | ProofRefusal, refusals.AccountRefusal]]] = {}
    lines: list[str] = []
    selected_groups = accounts.GroupAccounts() if proof_group else accounts.group_accounts()
    unconfigured = {group for group in (() if proof_group else accounts.CLAUDE_GROUPS) if isinstance(accounts.group_label(selected_groups, group), accounts.GroupNotConfigured)}
    reported_unconfigured: set[accounts.ClaudeGroup] = set()
    detected = session_refusals([session["sessionId"] for session in sessions.live_sessions()
        if not proof_group or session["sessionId"] in proof_group])
    refused_ids = frozenset(session_id for session_id, refusal in detected.items()
        if isinstance(refusal, (TranscriptRefusal, ProofRefusal))
        and not ((previous := state["sessions"].get(session_id)) and previous["entry_id"] == refusal.entry_id
                 and previous["state"] in ("requested", "cancelled", "continued", "group selected")))
    refusing_logins: set[str] = set()
    owners = live_owners(only=refused_ids) if refused_ids else []
    owners = [session for session in owners if not isinstance(detected[session["session_id"]], ProofRefusal)
              or accounts.session_group(session) == "ungrouped"]
    if owners and not dry_run:
        refresh_usage()
        now = max(now, datetime.now(timezone.utc))
    waiting_groups: set[accounts.ClaudeGroup] = set()
    blocked_groups: set[accounts.ClaudeGroup] = set()
    tick_logins = frozenset(identity.login.casefold() for session in owners
        if isinstance(detected[session["session_id"]], TranscriptRefusal)
        if isinstance(identity := accounts.account_identity(Path(session.get("config_dir", str(accounts.main_config_dir())))),
                      agent_accounts.NumberedAccount))
    tick_excluded = accounts.account_labels_for_logins(tick_logins)
    journaled_waits: dict[str, moves.WaitedMove] = {}
    for session in owners:
        session_id = session["session_id"]
        refusal = detected[session_id]
        previous = state["sessions"].get(session_id)
        if (isinstance(refusal, NoSessionRefusal) or not previous or previous["entry_id"] != refusal.entry_id
                or previous["state"] not in ("retry", "no room", "waited")):
            continue
        wait_reset = refusal.reset
        if isinstance(wait_reset, refusals.NoResetKnown) and "refusal" in previous and "reset" in previous["refusal"]:
            wait_reset = refusals.KnownReset(datetime.fromisoformat(previous["refusal"]["reset"]))
        if not isinstance(wait_reset, refusals.KnownReset):
            continue
        journaled_wait = next((entry for entry in entries if entry["tool"] == "claude"
            and entry["kind"] == "waited" and entry["subject"].split(": ", 1)[0] == session_id
            and entry["reset"] == wait_reset.at.isoformat()
            and datetime.fromisoformat(entry["at"]) >= refusal.refused_at.at), None)
        if journaled_wait is not None:
            journaled_waits[session_id] = journaled_wait
            if previous["state"] in ("retry", "no room") and not dry_run:
                # Restore the saved wait before destination preparation can block its continuation.
                _set_transition(state, session_id, refusal.entry_id, "waited", journaled_wait["move_id"])
    # Preview preparation judges repairs without changing any account content.
    for session in owners:
        group_name = accounts.session_group(session)
        if group_name == "ungrouped" or group_name in pending_groups:
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
        if not isinstance(identity, agent_accounts.NumberedAccount):
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
            prepared = (accounts.prepared_account_preview(chosen.label, resume=True) if dry_run
                        else accounts.prepared_account(chosen.label, resume=True))
            if isinstance(prepared, accounts.AccountRefused):
                blocked_groups.add(group_name)
                lines.append(f"{group_name}: move refused: {prepared.reason}")
    for session in owners:
        session_id = session["session_id"]
        refusal = detected[session_id]
        if isinstance(refusal, NoSessionRefusal):
            continue
        previous = state["sessions"].get(session_id)
        if previous and previous["entry_id"] == refusal.entry_id and previous["state"] in ("requested", "cancelled", "continued", "group selected"):
            continue
        if (accounts.session_group(session) in blocked_groups | unconfigured
                and not (previous and previous["entry_id"] == refusal.entry_id and previous["state"] == "waited")):
            continue
        identity = accounts.account_identity(Path(session.get("config_dir", str(accounts.main_config_dir()))))
        if not isinstance(identity, agent_accounts.NumberedAccount):
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
            if isinstance(refusal.reset, refusals.KnownReset):
                reset = refusal.reset
            account_refusal = refusals.AccountRefusal("claude", identity.login, window, reset, refusal.refused_at.at)
            if not dry_run:
                observation = FirstRefusalObservation(login=identity.login,
                    window=account_refusal.window.name if isinstance(account_refusal.window, refusals.KnownWindow) else "unknown",
                    at=account_refusal.at.isoformat())
                if isinstance(account_refusal.reset, refusals.KnownReset):
                    observation["reset"] = account_refusal.reset.at.isoformat()
                if not previous or previous["entry_id"] != refusal.entry_id:
                    if isinstance(refusal, TranscriptRefusal):
                        refusals.write_refusal(account_refusal)
                    _set_transition(state, session_id, refusal.entry_id, "retry", "")
                state["sessions"][session_id]["refusal"] = observation
                _save_state(state)
        window, reset = account_refusal.window, account_refusal.reset
        wait_reset = refusal.reset if isinstance(refusal.reset, refusals.KnownReset) else reset
        journaled_wait = journaled_waits.get(session_id)
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
                        clear_assumed(session_id)
                        _set_transition(state, session_id, refusal.entry_id, "continued", previous["move_id"])
                        lines.append(f"{session['name']}: continued on its current account")
                continue
        if isinstance(refusal, TranscriptRefusal):
            refusing_logins.add(identity.login.casefold())
        subject = session_id + ": " + session["name"]
        pending_head = accounts.session_group(session) in pending_groups
        if (isinstance(wait_reset, refusals.KnownReset) and (pending_head
                or isinstance(window, refusals.KnownWindow) and window.name == "five_hour"
                and timedelta(0) < wait_reset.at - now < timedelta(minutes=20))):
            lines.append(f"{session['name']}: refused; wait until {wait_reset.at.astimezone(agent_accounts.user_zone()).isoformat()}")
            if not dry_run and journaled_wait is None:
                move_id = uuid4().hex
                moves.append_move(moves.WaitedMove(kind="waited", at=now.isoformat(), tool="claude",
                    subject=subject, move_id=move_id,
                    window=window.name if isinstance(window, refusals.KnownWindow) else "unknown",
                    reset=wait_reset.at.isoformat()))
                _set_transition(state, session_id, refusal.entry_id, "waited", move_id)
            continue
        if pending_head:
            continue
        group_name = accounts.session_group(session)
        key = ("proof group" if proof_group else "proof:" + session_id if isinstance(refusal, ProofRefusal)
               else identity.login.casefold()) if group_name == "ungrouped" else "group:" + group_name
        groups.setdefault(key, []).append((session, refusal, account_refusal))
    fingerprint = _reading_values_fingerprint()
    if dry_run:
        lines.insert(0, accounts.group_status_line(selected_groups))
    all_members: dict[accounts.ClaudeGroup, list[inventory_module.ListedSession]] = (_action_group_members()
        if not proof_group and any(key.startswith("group:") for key in groups)
        else {"units": [], "showrunners": []})
    moved_groups: set[accounts.ClaudeGroup] = set()
    no_room_groups: set[accounts.ClaudeGroup] = set()
    for login, group in groups.items():
        group_name = cast(accounts.ClaudeGroup, login.removeprefix("group:")) if login.startswith("group:") else "ungrouped"
        if group_name in waiting_groups or group_name in blocked_groups:
            continue
        source_identity = accounts.account_identity(Path(group[0][0].get("config_dir", str(accounts.main_config_dir()))))
        if not isinstance(source_identity, agent_accounts.NumberedAccount):
            continue
        default_identity = accounts.account_identity(accounts.main_config_dir())
        source = source_identity.label
        excluded = {source, source_identity.label, *tick_excluded}
        for member, _, _ in group:
            member_identity = accounts.account_identity(Path(member.get("config_dir", str(accounts.main_config_dir()))))
            if isinstance(member_identity, agent_accounts.NumberedAccount):
                excluded.add(member_identity.label)
        if isinstance(default_identity, agent_accounts.NumberedAccount) and default_identity.login.casefold() in refusing_logins:
            excluded.add(default_identity.label)
        for named in accounts.checked_named_accounts():
            identity = accounts.account_identity(named.dir)
            if isinstance(identity, agent_accounts.NumberedAccount) and identity.login.casefold() in refusing_logins:
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
                notice_key = login if isinstance(group[0][1], ProofRefusal) else source_identity.login.casefold()
                if state["notices"].get(notice_key) != fingerprint:
                    _ = state["notices"].pop(notice_key, None)
                    if _notify_no_room(text, now):
                        state["notices"][notice_key] = fingerprint
            continue
        if group_name != "ungrouped":
            lines.extend(f"{session.name}: refused group {group_name}; move to {chosen.label}" for session in all_members[group_name]
                         if session.session_id not in {item[0]["session_id"] for item in group})
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
        move_origin = accounts.RefusalOrigin(window_name, first_refusal.reset)
        transition = accounts.GroupTransition(uuid4().hex, move_origin, accounts.RecordedTransitionReason(why))
        if group_name != "ungrouped":
            prepared = accounts.prepared_account(chosen.label, resume=True)
            if isinstance(prepared, accounts.AccountRefused):
                lines.append(f"{group_name}: move refused: {prepared.reason}")
                continue
            transition = accounts.set_group_account(group_name, chosen.label, why=why, now=now, origin=move_origin)
            selected_groups[group_name] = chosen.label
            moved_groups.add(group_name)
            for session, refusal, _ in group:
                _set_transition(state, session["session_id"], refusal.entry_id, "group selected", "")
            # The chosen group account survives a member's failed request or interrupted delivery.
            _save_state(state)
            ids = tuple(session.session_id for session in all_members[group_name]
                        if not accounts.session_on_account(session, chosen.label))
        else:
            ids = tuple(session["session_id"] for session, _, _ in group)
        outcomes = accounts.move_sessions(ids, chosen.label, why=why) if ids else ()
        by_id = {session["session_id"]: refusal for session, refusal, _ in group}
        for outcome in outcomes:
            lines.append(accounts.move_result_line(outcome))
            if outcome.session_id not in by_id:
                continue
            refusal = by_id[outcome.session_id]
            _set_transition(state, outcome.session_id, refusal.entry_id,
                "requested" if not isinstance(outcome, accounts.SessionMoveRefused) else "group selected" if group_name != "ungrouped" else "retry",
                transition.move_id if not isinstance(outcome, accounts.SessionMoveRefused) else "")
    if not proof_group:
        if not dry_run and state != original_state:
            _save_state(state)
        before_rules = dict(selected_groups)
        lines.extend(run_auto_moves(now, dry_run=dry_run, moved_groups=frozenset(moved_groups)))
        if not dry_run:
            selected_groups = accounts.group_accounts()
            moved_groups.update(group for group in accounts.CLAUDE_GROUPS
                                if selected_groups.get(group) != before_rules.get(group))
            state = _load_state()
            original_state = deepcopy(state)
    # A committed group choice is stable. Complete its off-target members without choosing again.
    if not proof_group and selected_groups:
        eligible: set[accounts.ClaudeGroup] = {group for group in accounts.CLAUDE_GROUPS
                    if group not in moved_groups | waiting_groups | blocked_groups | no_room_groups
                    and isinstance(accounts.group_label(selected_groups, group), str)}
        def completion_members(observed: dict[accounts.ClaudeGroup, list[inventory_module.ListedSession]],
                               group_name: accounts.ClaudeGroup, target: str) -> list[inventory_module.ListedSession]:
            return [session for session in observed[group_name]
                       if not accounts.session_on_account(session, target)
                       and not (session.session_id in state["sessions"] and state["sessions"][session.session_id]["state"] == "cancelled"
                                and isinstance(detected.get(session.session_id), TranscriptRefusal)
                                and state["sessions"][session.session_id]["entry_id"] == cast(TranscriptRefusal, detected[session.session_id]).entry_id)]

        if eligible:
            # Reports can suggest work. A changed registry also finds late arrivals.
            cached = accounts.live_group_members()
            previous = _report_sessions.get()
            changed = isinstance(previous, NoSessionObservation) or previous != _session_identities()
            needs_completion = changed or any(completion_members(cached, group, target)
                for group in eligible if isinstance(target := accounts.group_label(selected_groups, group), str))
        else:
            needs_completion = False
        if needs_completion:
            with herdr.process_action():
                all_members = accounts.live_group_members()
                for group_name in accounts.CLAUDE_GROUPS:
                    if group_name not in eligible:
                        continue
                    target = accounts.group_label(selected_groups, group_name)
                    if isinstance(target, accounts.GroupNotConfigured):
                        continue
                    members = completion_members(all_members, group_name, target)
                    if not members:
                        continue
                    directory = accounts.claude_account_dir(target)
                    identity = accounts.account_identity(directory)
                    if not isinstance(identity, agent_accounts.NumberedAccount):
                        lines.append(f"{group_name}: move refused: account unreadable")
                        continue
                    held = refusals.load().get(refusals.key("claude", identity.login))
                    if held is not None and not refusals.resolved(held, refusals.stored_reading("claude", identity.login, now), now):
                        lines.append(f"{group_name}: move refused: {target} holds an unresolved refusal")
                        continue
                    prepared = (accounts.prepared_account_preview(target, resume=True) if dry_run
                                else accounts.prepared_account(target, resume=True))
                    if isinstance(prepared, accounts.AccountRefused):
                        lines.append(f"{group_name}: move refused: {prepared.reason}")
                        continue
                    if dry_run:
                        lines.extend(accounts.move_preview_line(session, target) for session in members)
                    else:
                        transition = accounts.group_transition(group_name)
                        if isinstance(transition, accounts.NoGroupTransition):
                            transition = accounts.set_group_account(group_name, target, why=f"complete {group_name} move to {target}", now=now)
                        why = f"complete {group_name} move to {target}"
                        if isinstance(transition.origin, accounts.AutoOrigin):
                            why = transition.why.text if isinstance(transition.why, accounts.RecordedTransitionReason) else ""
                        outcomes = accounts.move_sessions(tuple(session.session_id for session in members), target, why=why)
                        lines.extend(accounts.move_result_line(outcome) for outcome in outcomes)
    if not dry_run and state != original_state:
        _save_state(state)
    return lines


# Preserve the watcher API while sharing replay evidence with quota_alert.
RecordedAutoMoves = role_map.RecordedAutoMoves
UnreadableAutoMoveHistory = role_map.UnreadableAutoMoveHistory


def recorded_auto_moves() -> RecordedAutoMoves | UnreadableAutoMoveHistory:
    return role_map.recorded_auto_moves(accounts.roles_history_file())


def _run_auto_moves_locked(now: datetime, *, dry_run: bool = False,
                   moved_groups: frozenset[accounts.ClaudeGroup] = NO_MOVED_GROUPS) -> list[str]:
    """Apply log entries after the refusal pass while holding the log lock."""
    if socket.gethostname().split(".", 1)[0] != role_map.ROLE_MAP_HOST:
        return []
    configured = read_auto_move_rules()
    rules = [rule for rule in configured if isinstance(rule, AutoMoveRule)]
    lines = [rule.reason for rule in configured if isinstance(rule, UnreadableNextMoveLog)]
    if lines:
        return lines
    state = _load_state()
    original = deepcopy(state)
    holds = state.get("auto_move_holds", {})
    notices = state.get("auto_move_notices", {})
    heads: dict[accounts.ClaudeGroup, AutoMoveRule] = {}
    for rule in rules:
        _ = heads.setdefault(rule.group, rule)
    head_ids = {rule.entry_id for rule in heads.values()}
    for entry_id in set(holds) - head_ids:
        del holds[entry_id]
    for entry_id in set(notices) - head_ids:
        del notices[entry_id]
    recorded = role_map.repair_selected_auto_moves(tuple(role_map.AutoMoveTarget(rule.entry_id, rule.group, rule.to)
        for rule in rules), now, dry_run=dry_run)
    if isinstance(recorded, UnreadableAutoMoveHistory):
        if not dry_run and state != original:
            _save_state(state)
        return lines
    consumed = recorded.entry_ids & {rule.entry_id for rule in rules}
    if consumed:
        for entry_id in consumed:
            _ = holds.pop(entry_id, None)
            _ = notices.pop(entry_id, None)
        if not dry_run:
            next_moves.write_next_moves(tuple(entry for entry in next_moves.read_next_moves()
                                              if entry.id not in consumed))
    # A successor starts on a later pass, even when replay consumes its head here.
    rules = [rule for rule in heads.values() if rule.entry_id not in consumed]
    if not rules:
        if not dry_run and state != original:
            _save_state(state)
        return lines
    selected = accounts.group_accounts()
    moved = set(moved_groups)
    held_refusals = refusals.load()
    for rule in rules:
        selection = _group_selection(selected, rule.group)
        hold = holds.get(rule.entry_id)
        if hold is not None and not hold.is_current(selection, rule.when.percent):
            del holds[rule.entry_id]
        if (isinstance(selection.account, accounts.GroupNotConfigured)
                or rule.group in moved):
            continue
        try:
            source_identity = accounts.account_identity(accounts.claude_account_dir(selection.account))
        except accounts.AccountsError:
            continue
        if not isinstance(source_identity, agent_accounts.NumberedAccount):
            continue
        source = refusals.stored_reading("claude", source_identity.login, now)
        if not isinstance(source, refusals.FreshReading):
            continue
        if source.weekly.left > rule.when.percent:
            _ = holds.pop(rule.entry_id, None)
            continue
        if isinstance(rule.when.hold_mode, next_moves.HalfPointHold) or rule.when.hold_mode.minutes > 0:
            if rule.entry_id not in holds:
                holds[rule.entry_id] = HoldStarted(selection.account, now, selection.move_id, rule.when.percent,
                                                  next_moves.resolve_hold(rule.when.hold_mode, selection.account, now))
                state["auto_move_holds"] = holds
            started = holds[rule.entry_id]
            if isinstance(started.duration, LegacyHoldDuration):
                started = HoldStarted(started.account, started.at, started.move_id, started.percent,
                                      next_moves.resolve_hold(rule.when.hold_mode, selection.account, now))
                holds[rule.entry_id] = started
                state["auto_move_holds"] = holds
            if (isinstance(started.duration, next_moves.ResolvedHold)
                    and (now - started.at).total_seconds() / 60 < started.duration.minutes):
                continue
        if selection.account == rule.to:
            lines.append(_record_blocked_head(state, rule, TargetIsCurrentAccount(), now, dry_run))
            continue
        try:
            target_identity = accounts.account_identity(accounts.claude_account_dir(rule.to))
        except accounts.AccountsError as error:
            lines.append(_record_blocked_head(state, rule, TargetAccountUnavailable(str(error)), now, dry_run))
            continue
        if not isinstance(target_identity, agent_accounts.NumberedAccount):
            lines.append(_record_blocked_head(state, rule,
                TargetAccountUnavailable("no readable login"), now, dry_run))
            continue
        target = refusals.stored_reading("claude", target_identity.login, now)
        last_read = (target.weekly.at if isinstance(target, (refusals.FreshReading, refusals.StaleReading))
                     and isinstance(target.weekly, refusals.ReadingWindow) else NoTargetReading())
        target_usage = usage_store.load().get(usage_store.key("Claude", target_identity.login), {})
        if usage_store.latest_read_outcome(target_usage) in (agent_accounts.UsageReadOutcome.STALE_TOKEN,
                                                          agent_accounts.UsageReadOutcome.NO_LOGIN_TOKEN):
            attempt = accounts.login_renewal_attempts().get(rule.to)
            reason = (TargetLoginRenewalFailed(attempt.result.detail)
                      if attempt is not None and isinstance(attempt.result, accounts.LoginRenewalFailed)
                      else TargetReadingUnavailable(last_read))
            lines.append(_record_blocked_head(state, rule, reason, now, dry_run))
            continue
        if not isinstance(target, refusals.FreshReading):
            lines.append(_record_blocked_head(state, rule, TargetReadingUnavailable(last_read), now, dry_run))
            continue
        if target.weekly.left <= rule.when.percent:
            lines.append(_record_blocked_head(state, rule,
                TargetAtOrBelowThreshold(target.weekly.left, rule.when.percent), now, dry_run))
            continue
        held = held_refusals.get(refusals.key("claude", target_identity.login))
        if held is not None and not refusals.resolved(held, target, now):
            lines.append(_record_blocked_head(state, rule, TargetUnresolvedRefusal(), now, dry_run))
            continue
        prepared = (accounts.prepared_account_preview(rule.to, resume=True) if dry_run
                    else accounts.prepared_account(rule.to, resume=True))
        if isinstance(prepared, accounts.AccountRefused):
            lines.append(_record_blocked_head(state, rule, TargetPreparationRefused(prepared.reason), now, dry_run))
            continue
        _ = notices.pop(rule.entry_id, None)
        why = f"auto: {rule.entry_id}"
        lines.append(f"{rule.group}: {why}; {'would move' if dry_run else 'move'} from {selection.account} to {rule.to}")
        moved.add(rule.group)
        if dry_run:
            continue
        members = _action_group_members()
        _ = accounts.set_group_account(rule.group, rule.to, why=why, now=now, origin=accounts.AutoOrigin())
        selected[rule.group] = rule.to
        for group_rule in rules:
            if group_rule.group == rule.group:
                _ = holds.pop(group_rule.entry_id, None)
        # Consume only after the group selection and its reason are recorded.
        next_moves.write_next_moves(tuple(entry for entry in next_moves.read_next_moves()
                                          if entry.id != rule.entry_id))
        _save_state(state)
        ids = tuple(member.session_id for member in members[rule.group]
                    if not accounts.session_on_account(member, rule.to))
        outcomes = accounts.move_sessions(ids, rule.to, why=why) if ids else ()
        lines.extend(accounts.move_result_line(outcome) for outcome in outcomes)
    if not dry_run and state != original:
        _save_state(state)
    return lines


def run_auto_moves(now: datetime, *, dry_run: bool = False,
                   moved_groups: frozenset[accounts.ClaudeGroup] = NO_MOVED_GROUPS) -> list[str]:
    """Read and consume one-shot entries under the same lock as log edits."""
    if socket.gethostname().split(".", 1)[0] != role_map.ROLE_MAP_HOST:
        return []
    if dry_run:
        return _run_auto_moves_locked(now, dry_run=True, moved_groups=moved_groups)
    with next_moves.next_moves_lock():
        return _run_auto_moves_locked(now, moved_groups=moved_groups)


def renew_expired_logins(now: datetime, dry_run: bool) -> list[str]:
    """Probe local noted accounts before any move takes the queue lock."""
    stored = usage_store.load()
    attempts = accounts.login_renewal_attempts()
    lines: list[str] = []
    for named in accounts.checked_named_accounts():
        identity = accounts.account_identity(named.dir)
        if not isinstance(identity, agent_accounts.NumberedAccount):
            continue
        usage = stored.get(usage_store.key("Claude", identity.login), {})
        if usage_store.latest_read_outcome(usage) not in (agent_accounts.UsageReadOutcome.STALE_TOKEN,
                                                       agent_accounts.UsageReadOutcome.NO_LOGIN_TOKEN):
            continue
        previous = attempts.get(named.label)
        if previous is not None and now - previous.at < timedelta(minutes=60):
            continue
        if dry_run:
            lines.append(f"would renew {named.label}")
            continue
        result = accounts.renew_login(named.label)
        attempts[named.label] = accounts.LoginRenewalAttempt(now, result)
        _atomic_json(accounts.login_renewals_file(), {
            label: {"at": attempt.at.isoformat(),
                    "outcome": "renewed" if isinstance(attempt.result, accounts.LoginRenewed) else "failed",
                    "detail": attempt.result.detail if isinstance(attempt.result, accounts.LoginRenewalFailed) else ""}
            for label, attempt in attempts.items()})
        lines.append(f"{named.label} login renewed" if isinstance(result, accounts.LoginRenewed)
                     else f"{named.label} login renewal failed: {result.detail}")
    return lines


@role_map.role_map_command
def run(now: datetime | None = None, *, dry_run: bool = False, proof_group: frozenset[str] = NO_SESSION_FILTER,
        refresh_usage: Callable[[], None] = lambda: None) -> list[str]:
    from scripts.accounts import unit_watchdog

    at = now or datetime.now(timezone.utc)
    if dry_run:
        return [*renew_expired_logins(at, True), *_run(at, True, proof_group),
                *(unit_watchdog.run(at, dry_run=True) if not proof_group else [])]
    with _watch_lock():
        renewed = renew_expired_logins(at, False)
        # A probe can save a reading after the pass started.
        after_renewal = max(at, datetime.now(timezone.utc)) if renewed else at
        return [*renewed, *_run(after_renewal, False, proof_group, refresh_usage=refresh_usage),
                *(unit_watchdog.run(after_renewal, dry_run=False) if not proof_group else [])]


class WatchCommandLine(argparse.Namespace):
    dry_run: bool = False
    assume_refused: list[str] | None = None
    reset_in: float | None = None
    proof_group: list[str] | None = None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--dry-run", action="store_true")
    _ = parser.add_argument("--assume-refused", nargs=2, metavar=("SESSION", "WINDOW"))
    _ = parser.add_argument("--reset-in", type=float)
    _ = parser.add_argument("--proof-group", nargs="+")
    args = parser.parse_args(argv, namespace=WatchCommandLine())
    if args.reset_in is not None and not args.assume_refused:
        parser.error("--reset-in requires --assume-refused")
    if args.proof_group and not args.assume_refused:
        parser.error("--proof-group requires --assume-refused")
    proof_ids: set[str] = set()
    if args.assume_refused:
        name, window = args.assume_refused
        if window not in ("weekly", "five-hour"):
            parser.error("window must be weekly or five-hour")
        owners = live_owners()
        selected: dict[str, inventory_module.Session] = {}
        for probe in [name, *(args.proof_group or [])]:
            matching = [session for session in owners if session["session_id"] == probe or session["name"] == probe]
            if len(matching) != 1:
                parser.error("name or id must identify one local live owner")
            if accounts.session_group(matching[0]) != "ungrouped":
                parser.error("--assume-refused requires an ungrouped probe, neither unit nor showrunner")
            selected[matching[0]["session_id"]] = matching[0]
        if args.proof_group:
            proof_ids.update(selected)
        now = datetime.now(timezone.utc)
        held = AssumedRefusalFile(uuid=uuid4().hex, timestamp=now.isoformat(),
            window="weekly" if window == "weekly" else "five_hour",
            reset=(now + timedelta(minutes=float(args.reset_in))).isoformat() if args.reset_in is not None else "")
        if not args.dry_run:
            for session_id in selected:
                _atomic_json(assumed_file(session_id), held)
    for line in run(dry_run=bool(args.dry_run), proof_group=frozenset(proof_ids)):
        print(line)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(f"refusal watcher: {str(error).splitlines()[0]}", file=sys.stderr)
        sys.exit(1)
