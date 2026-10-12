#!/usr/bin/env python3
"""One holder file per session; decide when builds are quiet and may resume."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import time
import uuid
from collections import Counter
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, time as ClockTime, timedelta, timezone
from pathlib import Path
from typing import Literal, NotRequired, Required, TypedDict, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "message"))
from sessions import addressed, live_sessions  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
import override_guard


BUILD_COMMANDS = frozenset({"cargo", "rustc", "cargo-nextest"})
INSTANT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})")
SAFE = re.compile(r"[^A-Za-z0-9._-]")
RELEASE_SETTLE_S = 60
NO_ADMISSION_ACK_S = 900
NO_REGISTRATION_S = 60
RELEASE_POLL_S = 15
# A lookup that cannot read the session records is tried again before the release gives up on the
# session: a record caught half-written reads cleanly a moment later.
LOOKUP_TRIES = 3
LOOKUP_RETRY_S = 1.0

GateOutcome = Literal["Granted", "TimedOut", "MeminfoUnavailable"]
DeliveryOutcome = Literal["NotAttempted", "Delivered", "QueuedUnconfirmed", "RecipientGone", "DeliveryFailed"]


class GenerationDelivery(TypedDict):
    generation: int
    outcome: DeliveryOutcome


@dataclass(frozen=True)
class OwningSession:
    session_id: str


@dataclass(frozen=True)
class NoOwningSession:
    pass


class PendingCall(TypedDict):
    call_id: str
    pid: int
    proc_start: str
    started: bool
    admitted_generation: int


class ReleaseEntry(TypedDict, total=False):
    session_id: Required[str]
    name: Required[str]
    state: Required[str]
    attempted_at: str
    released_at: str
    wait_started_at: str
    wait_ended_at: str
    outcome: str
    answered_at: str
    calls: Required[list[PendingCall]]
    active_call_id: Required[str]
    active_released_at: Required[str]
    session_pending: Required[bool]
    session_admitted_generation: Required[int]
    delivery: Required[GenerationDelivery]


class HoldCycle(TypedDict):
    id: str
    opened_at: str
    holders: dict[str, dict[str, str]]
    recipients: dict[str, str]
    entries: list[ReleaseEntry]
    release_started_at: str
    holder_generation: NotRequired[int]
    holder_signature: NotRequired[str]


class ReleaseRecordReadError(ValueError):
    """The stored hold cycle cannot be read safely."""


@dataclass(frozen=True)
class NoCycle:
    pass


@dataclass(frozen=True)
class AwaitingTurn:
    cycle_id: str
    generation: int


@dataclass(frozen=True)
class AdmittedTurn:
    cycle_id: str
    generation: int


@dataclass(frozen=True)
class AdmissionReadError:
    reason: str


Admission = NoCycle | AwaitingTurn | AdmittedTurn | AdmissionReadError


@dataclass(frozen=True)
class DamagedRecordSetAside:
    path: Path


def release_record_error_line(error: ReleaseRecordReadError) -> str:
    return f"release record could not be read: {error}; /build_hold release sets it aside and ends the hold"


@dataclass(frozen=True)
class AwaitingRelease:
    pass


@dataclass(frozen=True)
class RecipientGone:
    pass


@dataclass(frozen=True)
class NoPendingTurn:
    pass


@dataclass(frozen=True)
class RecipientLive:
    socket: str


@dataclass(frozen=True)
class RecipientLookupFailed:
    reason: str


@dataclass(frozen=True)
class ReleaseDelivered:
    pass


@dataclass(frozen=True)
class ReleaseQueued:
    pass


@dataclass(frozen=True)
class ReleaseSendFailed:
    reason: str


@dataclass(frozen=True)
class DeliveryFailed:
    attempted_at: datetime


@dataclass(frozen=True)
class DeliveryQueued:
    attempted_at: datetime


@dataclass(frozen=True)
class ReleasedAwaitingAdmission:
    released_at: datetime


@dataclass(frozen=True)
class WaitingForMemory:
    released_at: datetime
    wait_started_at: datetime


@dataclass(frozen=True)
class MemoryGateReturned:
    wait_ended_at: datetime
    outcome: GateOutcome


@dataclass(frozen=True)
class NothingToBuild:
    answered_at: datetime


@dataclass(frozen=True)
class NoAdmissionAck:
    released_at: datetime


@dataclass(frozen=True)
class NoRegistration:
    pass


ReleaseState = (AwaitingRelease | RecipientGone | NoPendingTurn | DeliveryFailed | DeliveryQueued |
                ReleasedAwaitingAdmission | WaitingForMemory | MemoryGateReturned |
                NothingToBuild | NoAdmissionAck | NoRegistration)


def read_release_state(entry: Mapping[str, object]) -> ReleaseState:
    state = entry.get("state")
    session_id = entry.get("session_id")
    if not isinstance(state, str) or not isinstance(session_id, str) or not isinstance(entry.get("name"), str):
        raise ValueError("invalid release entry identity or state")
    fields = set(entry) - {"session_id", "name", "state", "calls", "active_call_id", "active_released_at", "session_pending", "session_admitted_generation", "delivery"}
    expected: dict[str, set[str]] = {
        "AwaitingRelease": set(),
        "RecipientGone": set(),
        "NoPendingTurn": set(),
        "DeliveryFailed": {"attempted_at"},
        "DeliveryQueued": {"attempted_at"},
        "ReleasedAwaitingAdmission": {"released_at"},
        "WaitingForMemory": {"released_at", "wait_started_at"},
        "MemoryGateReturned": {"wait_ended_at", "outcome"},
        "NothingToBuild": {"answered_at"},
        "NoAdmissionAck": {"released_at"},
        "NoRegistration": set(),
    }
    if state not in expected or fields != expected[state]:
        raise ValueError(f"invalid release state for {session_id}: {state}")

    def instant(field: str) -> datetime:
        value = entry[field]
        if not isinstance(value, str):
            raise ValueError(f"invalid {field} for {session_id}")
        try:
            return aware_instant(value)
        except ValueError:
            raise ValueError(f"invalid {field} for {session_id}") from None

    match state:
        case "AwaitingRelease":
            return AwaitingRelease()
        case "RecipientGone":
            return RecipientGone()
        case "NoPendingTurn":
            return NoPendingTurn()
        case "DeliveryFailed":
            return DeliveryFailed(instant("attempted_at"))
        case "DeliveryQueued":
            return DeliveryQueued(instant("attempted_at"))
        case "ReleasedAwaitingAdmission":
            return ReleasedAwaitingAdmission(instant("released_at"))
        case "WaitingForMemory":
            return WaitingForMemory(instant("released_at"), instant("wait_started_at"))
        case "MemoryGateReturned":
            outcome = entry["outcome"]
            if outcome not in {"Granted", "TimedOut", "MeminfoUnavailable"}:
                raise ValueError(f"invalid gate outcome for {session_id}")
            return MemoryGateReturned(instant("wait_ended_at"), cast(GateOutcome, outcome))
        case "NothingToBuild":
            return NothingToBuild(instant("answered_at"))
        case "NoAdmissionAck":
            return NoAdmissionAck(instant("released_at"))
        case "NoRegistration":
            return NoRegistration()
        case _:
            raise ValueError(f"invalid release state for {session_id}: {state}")


def store_release_state(entry: ReleaseEntry, state: ReleaseState) -> None:
    for field in ("attempted_at", "released_at", "wait_started_at", "wait_ended_at", "outcome", "answered_at"):
        _ = entry.pop(field, None)
    entry["state"] = type(state).__name__
    if isinstance(state, (MemoryGateReturned, NothingToBuild)) and not entry.get("active_call_id", ""):
        entry["session_pending"] = False
    if isinstance(state, (DeliveryFailed, DeliveryQueued)):
        entry["attempted_at"] = state.attempted_at.isoformat()
    elif isinstance(state, (ReleasedAwaitingAdmission, NoAdmissionAck)):
        entry["released_at"] = state.released_at.isoformat()
    elif isinstance(state, WaitingForMemory):
        entry["released_at"] = state.released_at.isoformat()
        entry["wait_started_at"] = state.wait_started_at.isoformat()
    elif isinstance(state, MemoryGateReturned):
        entry["wait_ended_at"] = state.wait_ended_at.isoformat()
        entry["outcome"] = state.outcome
    elif isinstance(state, NothingToBuild):
        entry["answered_at"] = state.answered_at.isoformat()


@dataclass(frozen=True)
class KnownReleaseEta:
    at: datetime


@dataclass(frozen=True)
class UnknownReleaseEta:
    pass


ReleaseEta = KnownReleaseEta | UnknownReleaseEta


@dataclass(frozen=True)
class KnownCores:
    count: int


@dataclass(frozen=True)
class UnknownCores:
    pass


Cores = KnownCores | UnknownCores


@dataclass(frozen=True)
class NoReleaseEta:
    pass


@dataclass(frozen=True)
class ClockReleaseEta:
    clock: ClockTime
    zone: ZoneInfo


ReleaseRequest = NoReleaseEta | ClockReleaseEta


@dataclass(frozen=True)
class Holder:
    # The holder's name now when it is a live Claude session, else the name the hold was taken under.
    name: str
    since: datetime
    purpose: str
    release: ReleaseEta
    # The hold file's name: the holder's Claude session id, or its name when it is no Claude session.
    key: str


@dataclass(frozen=True)
class NoHolders:
    pass


@dataclass(frozen=True)
class ActiveHolders:
    holders: tuple[Holder, ...]

    def __post_init__(self) -> None:
        if not self.holders:
            raise ValueError("active holders cannot be empty")
        if tuple(sorted(self.holders, key=lambda holder: holder.since)) != self.holders:
            raise ValueError("active holders must be ordered by since")


HoldState = NoHolders | ActiveHolders


@dataclass(frozen=True)
class Process:
    pid: int
    user: str
    command: str
    cpu_percent: float


@dataclass(frozen=True)
class Quiet:
    pass


@dataclass(frozen=True)
class Busy:
    reasons: tuple[str, ...]


def owning_session() -> OwningSession | NoOwningSession:
    """Find the session that owns this run before using the shell's session id."""
    run = os.environ.get("PLAN_DELEGATE_BOARD_DIR", "") or os.environ.get("PLAN_DELEGATE_SESSION_DIR", "")
    if run:
        active = Path(os.environ.get("PLAN_DELEGATE_ACTIVE_DIR", "/tmp/claude/delegate/active"))
        try:
            markers = tuple(active.iterdir())
        except OSError:
            markers = ()
        matches: list[tuple[int, str]] = []
        for marker in markers:
            try:
                if marker.is_file() and marker.read_text().strip() == run:
                    matches.append((marker.stat().st_mtime_ns, marker.name))
            except (OSError, UnicodeError):
                continue
        if matches:
            return OwningSession(max(matches)[1])
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    return OwningSession(session_id) if session_id else NoOwningSession()


def waiter_key(call_id: str) -> str:
    owner = owning_session()
    return owner.session_id if isinstance(owner, OwningSession) else f"verify:{call_id}"


def process_start_time(pid: int) -> str:
    if pid <= 0:
        return ""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace").rsplit(")", 1)[1].split()
        return fields[19] if fields[0] != "Z" else ""
    except (OSError, IndexError):
        return ""


def call_is_live(call: PendingCall) -> bool:
    return bool(call["proc_start"]) and process_start_time(call["pid"]) == call["proc_start"]


def group_defaults(entry: ReleaseEntry) -> None:
    _ = entry.setdefault("calls", [])
    _ = entry.setdefault("active_call_id", "")
    _ = entry.setdefault("active_released_at", "")
    _ = entry.setdefault("session_pending", entry["state"] in {"AwaitingRelease", "DeliveryQueued", "ReleasedAwaitingAdmission", "WaitingForMemory"})
    _ = entry.setdefault("session_admitted_generation", 0)
    _ = entry.setdefault("delivery", {"generation": 0, "outcome": "NotAttempted"})


def holders_signature() -> str:
    holders = read_holders(holder_directory())
    return "" if isinstance(holders, NoHolders) else json.dumps(sorted(
        (holder.key, holder.since.isoformat()) for holder in holders.holders
    ))



def _decode_holder_signature(signature: str) -> tuple[tuple[str, str], ...]:
    if not signature:
        return ()
    value = cast(object, json.loads(signature))
    if not isinstance(value, list):
        raise ValueError("invalid holder signature")
    pairs: list[tuple[str, str]] = []
    for item in cast(list[object], value):
        if not isinstance(item, list):
            raise ValueError("invalid holder signature")
        pair = cast(list[object], item)
        if len(pair) != 2 or not isinstance(pair[0], str) or not isinstance(pair[1], str):
            raise ValueError("invalid holder signature")
        pairs.append((pair[0], pair[1]))
    return tuple(pairs)

def update_holder_generation(cycle: HoldCycle) -> None:
    signature = holders_signature()
    previous = cycle.get("holder_signature", "")
    _ = cycle.setdefault("holder_generation", 1)
    cycle["holder_signature"] = signature
    if not previous or previous == signature:
        return
    cycle["holder_generation"] = cycle.get("holder_generation", 1) + 1
    before = set(_decode_holder_signature(previous))
    after = set(_decode_holder_signature(signature))
    if after - before:
        cycle["release_started_at"] = ""
    for entry in cycle["entries"]:
        group_defaults(entry)
        entry["active_call_id"] = ""
        if entry["calls"] or entry["session_pending"]:
            store_release_state(entry, AwaitingRelease())


def prune_calls(entry: ReleaseEntry) -> None:
    group_defaults(entry)
    entry["calls"] = [call for call in entry["calls"] if call_is_live(call)]
    if entry["active_call_id"] and not any(call["call_id"] == entry["active_call_id"] for call in entry["calls"]):
        entry["active_call_id"] = ""
        store_release_state(entry, AwaitingRelease() if entry["calls"] or entry["session_pending"] else NoPendingTurn())


def read_admission(key: str, call_id: str = "") -> Admission:
    try:
        cycle = read_cycle()
        if cycle is None:
            return NoCycle()
        generation = cycle.get("holder_generation", 1)
        signature = cycle.get("holder_signature", "")
        if signature and signature != holders_signature():
            return AwaitingTurn(cycle["id"], generation + 1)
        waiting = AwaitingTurn(cycle["id"], generation)
        for entry in cycle["entries"]:
            if entry["session_id"] != key:
                continue
            state = read_release_state(entry)
            if call_id:
                call = next((call for call in entry["calls"] if call["call_id"] == call_id), None)
                if call is None or not call_is_live(call):
                    return waiting
                if call["admitted_generation"] == generation:
                    return AdmittedTurn(cycle["id"], generation)
                if entry["active_call_id"] != call_id:
                    return waiting
                allowed = (DeliveryQueued, ReleasedAwaitingAdmission, WaitingForMemory, RecipientGone, DeliveryFailed)
            else:
                if entry["session_admitted_generation"] == generation:
                    return AdmittedTurn(cycle["id"], generation)
                if entry["active_call_id"]:
                    return waiting
                allowed = (DeliveryQueued, ReleasedAwaitingAdmission, WaitingForMemory)
            return AdmittedTurn(cycle["id"], generation) if isinstance(state, allowed) else waiting
        return waiting
    except ReleaseRecordReadError as error:
        return AdmissionReadError(f"release record could not be read: {error}")
    except (OSError, UnicodeError) as error:
        return AdmissionReadError(str(error))


def holder_directory() -> Path:
    return Path(os.environ.get("BUILD_HOLD_DIR", str(Path.home() / ".local/state/build-hold")))


def holder_path(directory: Path, name: str) -> Path:
    safe = SAFE.sub("-", name)
    if not safe or safe in {".", ".."}:
        raise ValueError("holder name must contain a file-name character")
    return directory / safe


def holder_of(given: str | None) -> tuple[str, str]:
    """The key a holder's hold is kept under, and the holder's name now.

    The key is the Claude session id of the session `given` means, or of the caller when no name is
    given, so a rename between hold and release loses nothing. A holder that is no live Claude
    session keeps `given` as its key.
    """
    records = live_sessions()
    if given is None:
        own = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
        session = next((record for record in records if own and record["sessionId"] == own), None)
        if session is None:
            raise ValueError("give --holder: this is not a live Claude session")
        return session["sessionId"], session["name"]
    session = addressed(given, records)
    return (given, given) if session is None else (session["sessionId"], session["name"])


def held_key(directory: Path, given: str | None) -> str:
    """The key of the hold `given`, or the caller, has. A hold taken under a name before holds were
    kept by session id is found by the name given, the session's name now, and each name it once had."""
    key, name = holder_of(given)
    none: list[str] = []
    former = next((record["formerNames"] for record in live_sessions() if record["sessionId"] == key), none)
    candidates: list[str] = [key, given or name, name, *former]
    found = next((one for one in candidates if holder_path(directory, one).is_file()), None)
    if found is None:
        raise ValueError(f"{given or name} held nothing")
    return found


def aware_instant(value: str) -> datetime:
    instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if instant.utcoffset() is None:
        raise ValueError("instant needs an offset")
    return instant


def read_holder(path: Path) -> Holder:
    """Even an unreadable or malformed regular file remains an active hold."""
    fallback = datetime.fromtimestamp(path.stat().st_mtime).astimezone()
    try:
        content = path.read_text().strip()
    except FileNotFoundError:
        raise
    except (OSError, UnicodeError):
        return Holder(path.name, fallback, "unreadable holder file", UnknownReleaseEta(), path.name)
    try:
        raw = cast(object, json.loads(content))
        if isinstance(raw, dict):
            fields = cast(dict[str, object], raw)
            name = fields.get("holder")
            since = fields.get("since")
            purpose = fields.get("for")
            release = fields.get("release_eta")
            if isinstance(name, str) and isinstance(since, str) and isinstance(purpose, str) and isinstance(release, str):
                eta: ReleaseEta = UnknownReleaseEta() if release == "unknown" else KnownReleaseEta(aware_instant(release))
                now = next((record["name"] for record in live_sessions() if record["sessionId"] == path.name), name)
                return Holder(now, aware_instant(since), purpose, eta, path.name)
    except (ValueError, TypeError):
        pass
    match = INSTANT.search(content)
    if match is None:
        return Holder(path.name, fallback, content, UnknownReleaseEta(), path.name)
    try:
        since = aware_instant(match.group())
    except ValueError:
        since = fallback
    return Holder(path.name, since, content[match.end():].lstrip(" ,;:-").strip(), UnknownReleaseEta(), path.name)


def read_holders(directory: Path) -> HoldState:
    try:
        paths = [path for path in directory.iterdir() if path.is_file()]
    except FileNotFoundError:
        return NoHolders()
    found: list[Holder] = []
    for path in paths:
        try:
            found.append(read_holder(path))
        except FileNotFoundError:
            continue
    holders = tuple(sorted(found, key=lambda holder: (holder.since, holder.name)))
    return ActiveHolders(holders) if holders else NoHolders()


def has_holder_file(directory: Path) -> bool:
    try:
        return any(path.is_file() for path in directory.iterdir())
    except FileNotFoundError:
        return False


def release_eta_text(release: ReleaseEta) -> str:
    if isinstance(release, UnknownReleaseEta):
        return "unknown"
    return release.at.astimezone().strftime("%H:%M %Z")


def holder_text(holder: Holder) -> str:
    return f"{holder.name} (for {holder.purpose}, release eta {release_eta_text(holder.release)})"


def release_request(release_time: str | None, zone_name: str | None) -> ReleaseRequest:
    if release_time is None:
        if zone_name is not None:
            raise ValueError("--zone goes with --release-eta")
        return NoReleaseEta()
    if zone_name is None:
        raise ValueError("--zone goes with --release-eta")
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", release_time):
        raise ValueError("release ETA must be HH:MM")
    try:
        zone = ZoneInfo(zone_name)
    except ZoneInfoNotFoundError:
        raise ValueError(f"unknown IANA zone: {zone_name}") from None
    hour, minute = map(int, release_time.split(":"))
    return ClockReleaseEta(ClockTime(hour, minute), zone)


def write_hold(directory: Path, name: str, purpose: str, request: ReleaseRequest, now: datetime,
               label: str | None = None) -> str:
    """Keep a hold under the key `name`. `label` is the holder's name now, when the key is a session id."""
    if not purpose.strip() or "\n" in purpose:
        raise ValueError("hold purpose must be one line of text")
    if isinstance(request, NoReleaseEta):
        release_eta = "unknown"
    else:
        release = datetime.combine(now.astimezone(request.zone).date(), request.clock, request.zone)
        if release <= now:
            raise ValueError("release ETA has already passed today")
        release_eta = release.isoformat()
    directory.mkdir(parents=True, exist_ok=True)
    record = {"holder": label or name, "since": now.isoformat(), "for": purpose.strip(), "release_eta": release_eta}
    _ = holder_path(directory, name).write_text(json.dumps(record, ensure_ascii=False) + "\n")
    return f"/build_hold from {label or name}: stop any cargo or verify.sh you are running and start none until I release. No release after 2 h: ask me. For: {purpose.strip()}"


def release_hold(directory: Path, name: str) -> str:
    path = holder_path(directory, name)
    try:
        path.unlink()
    except FileNotFoundError:
        raise ValueError(f"{name} held nothing") from None
    state = read_holders(directory)
    if isinstance(state, NoHolders):
        return "released, builds may resume."
    return "released; still held by " + "; ".join(holder_text(holder) for holder in state.holders)


def release_directory() -> Path:
    return Path(os.environ.get("BUILD_HOLD_RELEASE_DIR", str(Path.home() / ".local/state/build-hold-release")))


@contextmanager
def release_lock() -> Generator[None]:
    directory = release_directory()
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "release.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def cycle_path() -> Path | None:
    current = release_directory() / "current"
    try:
        cycle_id = current.read_text().strip()
    except FileNotFoundError:
        return None
    if not re.fullmatch(r"[0-9a-f]{32}", cycle_id):
        raise ValueError("invalid current hold cycle")
    return release_directory() / cycle_id / "cycle.json"


def read_cycle() -> HoldCycle | None:
    try:
        path = cycle_path()
        if path is None:
            return None
        record = cast(object, json.loads(path.read_text()))
        if not isinstance(record, dict):
            raise ValueError("invalid hold cycle: expected object")
        cycle = cast(dict[str, object], record)
        for field in ("id", "opened_at", "release_started_at"):
            if not isinstance(cycle.get(field), str):
                raise ValueError(f"invalid {field}: expected string")
        holders = cycle.get("holders")
        if not isinstance(holders, dict):
            raise ValueError("invalid holders: expected object")
        for name, instants in cast(dict[object, object], holders).items():
            if not isinstance(name, str) or not isinstance(instants, dict):
                raise ValueError("invalid holder: expected name and object")
            fields = cast(dict[str, object], instants)
            if not isinstance(fields.get("since"), str) or not isinstance(fields.get("released_at"), str):
                raise ValueError(f"invalid holder {name}: expected since and released_at strings")
        recipients = cycle.get("recipients")
        if not isinstance(recipients, dict):
            raise ValueError("invalid recipients: expected object")
        if any(not isinstance(name, str) or not isinstance(session_id, str)
               for session_id, name in cast(dict[object, object], recipients).items()):
            raise ValueError("invalid recipients: expected string names and session IDs")
        entries = cycle.get("entries")
        if not isinstance(entries, list):
            raise ValueError("invalid entries: expected list")
        for index, entry in enumerate(cast(list[object], entries)):
            if not isinstance(entry, dict):
                raise ValueError(f"invalid entry {index}: expected object")
            _ = read_release_state(cast(dict[str, object], entry))
            fields = cast(dict[str, object], entry)
            if "delivery" in fields:
                delivery = fields["delivery"]
                if not isinstance(delivery, dict):
                    raise ValueError(f"invalid delivery for entry {index}")
                record = cast(dict[str, object], delivery)
                generation = record.get("generation")
                outcome = record.get("outcome")
                if (set(record) != {"generation", "outcome"}
                    or not isinstance(generation, int) or isinstance(generation, bool) or generation < 0
                    or outcome not in {"NotAttempted", "Delivered", "QueuedUnconfirmed", "RecipientGone", "DeliveryFailed"}
                    or (outcome == "NotAttempted") != (generation == 0)):
                    raise ValueError(f"invalid delivery for entry {index}")
            calls = fields.get("calls", [])
            if not isinstance(calls, list):
                raise ValueError(f"invalid calls for entry {index}")
            identifiers: set[str] = set()
            for pending in cast(list[object], calls):
                if not isinstance(pending, dict):
                    raise ValueError("invalid pending call")
                call = cast(dict[str, object], pending)
                if (not isinstance(call.get("call_id"), str) or not call["call_id"]
                    or not isinstance(call.get("pid"), int) or cast(int, call["pid"]) <= 0
                    or not isinstance(call.get("proc_start"), str) or not call["proc_start"]
                    or not isinstance(call.get("started"), bool)
                    or not isinstance(call.get("admitted_generation"), int)):
                    raise ValueError("invalid pending call identity or admission")
                identifier = cast(str, call["call_id"])
                if identifier in identifiers:
                    raise ValueError("duplicate pending call")
                identifiers.add(identifier)
            if (not isinstance(fields.get("active_call_id", ""), str)
                or not isinstance(fields.get("session_pending", True), bool)
                or not isinstance(fields.get("session_admitted_generation", 0), int)):
                raise ValueError("invalid grouped entry turn")
            turn_time = fields.get("active_released_at", "")
            if not isinstance(turn_time, str):
                raise ValueError("invalid active turn timestamp")
            if turn_time:
                _ = aware_instant(turn_time)
            if fields.get("active_call_id", "") and fields["active_call_id"] not in identifiers:
                raise ValueError("active call is not listed")
            if "session_admitted_generation" not in fields and "calls" not in fields and fields.get("state") == "MemoryGateReturned":
                fields["session_admitted_generation"] = cycle.get("holder_generation", 1)
            group_defaults(cast(ReleaseEntry, cast(object, fields)))
        if not isinstance(cycle.get("holder_generation", 1), int) or cast(int, cycle.get("holder_generation", 1)) < 1:
            raise ValueError("invalid holder generation")
        signature = cycle.get("holder_signature", "")
        if not isinstance(signature, str):
            raise ValueError("invalid holder signature")
        _ = _decode_holder_signature(signature)
        _ = cycle.setdefault("holder_generation", 1)
        _ = cycle.setdefault("holder_signature", "")
        return cast(HoldCycle, cast(object, cycle))
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise ReleaseRecordReadError(str(error)) from error


def read_cycle_for_change() -> HoldCycle | NoCycle | DamagedRecordSetAside:
    """Read a cycle while release.lock is held, setting damage aside if found."""
    try:
        cycle = read_cycle()
        return NoCycle() if cycle is None else cycle
    except ReleaseRecordReadError as error:
        current = release_directory() / "current"
        try:
            path = cycle_path()
        except (OSError, ValueError):
            path = None
        damaged = path if path is not None and (path.is_symlink() or path.exists()) else current
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        destination = damaged.with_name(f"{damaged.name}.damaged-{timestamp}")
        suffix = 2
        while destination.exists():
            destination = damaged.with_name(f"{damaged.name}.damaged-{timestamp}-{suffix}")
            suffix += 1
        _ = damaged.rename(destination)
        current.unlink(missing_ok=True)
        print(f"release record could not be read ({error}); set aside as {destination}; this hold now releases every session at once", flush=True)
        return DamagedRecordSetAside(destination)


def save_cycle(cycle: HoldCycle) -> None:
    path = release_directory() / cycle["id"] / "cycle.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    _ = temporary.write_text(json.dumps(cycle, ensure_ascii=False) + "\n")
    os.replace(temporary, path)


def open_cycle(now: datetime) -> HoldCycle:
    cycle: HoldCycle = {"id": uuid.uuid4().hex, "opened_at": now.isoformat(), "holders": {}, "recipients": {}, "entries": [], "release_started_at": "", "holder_generation": 1, "holder_signature": ""}
    save_cycle(cycle)
    _ = (release_directory() / "current").write_text(cycle["id"] + "\n")
    return cycle


def start_hold(directory: Path, name: str, purpose: str, request: ReleaseRequest, now: datetime,
               label: str | None = None) -> str:
    with release_lock():
        if isinstance(read_holders(directory), NoHolders):
            cycle = open_cycle(now)
        else:
            cycle = read_cycle_for_change()
        if not isinstance(cycle, (NoCycle, DamagedRecordSetAside)) and not cycle.get("holder_signature", ""):
            cycle["holder_signature"] = holders_signature()
        notice = write_hold(directory, name, purpose, request, now, label)
        if not isinstance(cycle, (NoCycle, DamagedRecordSetAside)):
            cycle["holders"][name] = {"since": now.isoformat(), "released_at": ""}
            update_holder_generation(cycle)
            save_cycle(cycle)
        return notice + " Run python3 ~/.claude/scripts/build_hold/build_hold.py wait now in this session."


def record_recipients(recipients: Sequence[tuple[str, str]]) -> None:
    if not has_holder_file(holder_directory()):
        return
    with release_lock():
        if isinstance(read_holders(holder_directory()), NoHolders):
            return
        cycle = read_cycle_for_change()
        if isinstance(cycle, (NoCycle, DamagedRecordSetAside)):
            return
        for name, session_id in recipients:
            if session_id:
                cycle["recipients"][session_id] = name
                for entry in cycle["entries"]:
                    if entry["session_id"] == session_id:
                        entry["name"] = name
        save_cycle(cycle)


def register_wait(session_id: str, name: str = "", *, call_id: str = "", pid: int = 0,
                  proc_start: str = "", started: bool = False) -> str:
    if not session_id:
        return "NoSessionId: hold remains active; this session cannot register"
    if not has_holder_file(holder_directory()):
        return "no build hold"
    with release_lock():
        if isinstance(read_holders(holder_directory()), NoHolders):
            return "no build hold"
        cycle = read_cycle() if call_id else read_cycle_for_change()
        if cycle is None or isinstance(cycle, (NoCycle, DamagedRecordSetAside)):
            return "no hold cycle: wait for the release broadcast"
        update_holder_generation(cycle)
        generation = cycle.get("holder_generation", 1)
        existing = next((entry for entry in cycle["entries"] if entry["session_id"] == session_id), None)
        if existing is None:
            existing = new_entry(session_id, name or cycle["recipients"].get(session_id, session_id))
            existing["session_pending"] = not bool(call_id)
            cycle["entries"].append(existing)
        group_defaults(existing)
        prune_calls(existing)
        if call_id:
            if not any(call["call_id"] == call_id for call in existing["calls"]):
                process = pid or os.getpid()
                identity = proc_start or process_start_time(process)
                if not identity:
                    raise ReleaseRecordReadError("waiting call process cannot be identified")
                pending_call: PendingCall = {"call_id": call_id, "pid": process, "proc_start": identity,
                                             "started": started, "admitted_generation": 0}
                existing["calls"].append(pending_call)
                if (not session_id.startswith("verify:") and not existing["active_call_id"]
                    and existing["delivery"]["generation"] == generation
                    and isinstance(read_release_state(existing), (ReleasedAwaitingAdmission, DeliveryQueued))):
                    existing["active_call_id"] = call_id
                    existing["session_admitted_generation"] = generation
                    existing["session_pending"] = False
                    pending_call["admitted_generation"] = generation
        elif existing["session_admitted_generation"] != generation:
            existing["session_pending"] = True
        state = read_release_state(existing)
        terminal = (NothingToBuild, MemoryGateReturned, NoAdmissionAck, NoRegistration, RecipientGone, DeliveryFailed, NoPendingTurn)
        pending = any(call["admitted_generation"] != generation for call in existing["calls"])
        if isinstance(state, terminal) and not existing["active_call_id"] and (pending or existing["session_pending"]):
            cycle["entries"].remove(existing)
            existing["active_call_id"] = ""
            store_release_state(existing, AwaitingRelease())
            cycle["entries"].append(existing)
        save_cycle(cycle)
        return f"registered {session_id}; wait for direct release before building"


def new_entry(session_id: str, name: str) -> ReleaseEntry:
    entry: ReleaseEntry = {"session_id": session_id, "name": name, "state": "AwaitingRelease",
                           "calls": [], "active_call_id": "", "active_released_at": "", "session_pending": True,
                           "session_admitted_generation": 0, "delivery": {"generation": 0, "outcome": "NotAttempted"}}
    group_defaults(entry)
    return entry


def remove_call(key: str, call_id: str, *, cycle_id: str = "") -> None:
    if not (release_directory() / "current").exists():
        return
    with release_lock():
        cycle = read_cycle()
        if cycle is None or (cycle_id and cycle["id"] != cycle_id):
            return
        changed = False
        for entry in cycle["entries"]:
            if entry["session_id"] != key:
                continue
            count = len(entry["calls"])
            entry["calls"] = [call for call in entry["calls"] if call["call_id"] != call_id]
            changed = changed or len(entry["calls"]) != count
            if entry["active_call_id"] == call_id:
                changed = True
                entry["active_call_id"] = ""
                store_release_state(entry, AwaitingRelease() if entry["calls"] or entry["session_pending"] else NoPendingTurn())
        if changed:
            save_cycle(cycle)


def mark_gate(session_id: str, state: str, outcome: str = "", *, call_id: str = "", cycle_id: str = "", generation: int = 0) -> str:
    if not session_id:
        return "NoSessionId"
    if not has_holder_file(holder_directory()):
        return "no build hold"
    with release_lock():
        if isinstance(read_holders(holder_directory()), NoHolders):
            return "no build hold"
        cycle = read_cycle_for_change()
        if isinstance(cycle, (NoCycle, DamagedRecordSetAside)):
            return "no hold cycle"
        if cycle_id and cycle["id"] != cycle_id:
            return "mark ignored"
        if generation and generation != cycle.get("holder_generation", 1):
            return "mark ignored"
        if cycle.get("holder_signature", "") and cycle.get("holder_signature", "") != holders_signature():
            return "mark ignored"
        for entry in cycle["entries"]:
            if entry["session_id"] != session_id:
                continue
            if ((state == "WaitingForMemory"
                 or state == "MemoryGateReturned" and outcome in {"Granted", "TimedOut", "MeminfoUnavailable"})
                and confirm_delivery(entry, cycle.get("holder_generation", 1))):
                save_cycle(cycle)
            if entry["active_call_id"] != call_id:
                continue
            previous = read_release_state(entry)
            if not isinstance(previous, (ReleasedAwaitingAdmission, DeliveryQueued, WaitingForMemory, RecipientGone, DeliveryFailed)):
                continue
            instant = release_clock()
            if call_id:
                call = next((call for call in entry["calls"] if call["call_id"] == call_id), None)
                if call is None or not call_is_live(call):
                    return "mark ignored"
                call["started"] = True
                call["admitted_generation"] = cycle.get("holder_generation", 1)
            if state == "WaitingForMemory" and not isinstance(previous, WaitingForMemory):
                released_at = previous.released_at if isinstance(previous, ReleasedAwaitingAdmission) else previous.attempted_at if isinstance(previous, (DeliveryQueued, DeliveryFailed)) else instant
                store_release_state(entry, WaitingForMemory(released_at, instant))
            elif state == "MemoryGateReturned" and outcome in {"Granted", "TimedOut", "MeminfoUnavailable"}:
                store_release_state(entry, MemoryGateReturned(instant, cast(GateOutcome, outcome)))
                entry["active_call_id"] = ""
                if not call_id:
                    entry["session_pending"] = False
                    entry["session_admitted_generation"] = cycle.get("holder_generation", 1)
            else:
                return "mark ignored"
            save_cycle(cycle)
            return f"{session_id}: {entry['state']}"
        return "mark ignored"


def answer_nothing_to_build(session_id: str) -> str:
    """A released session's answer when it has no build or BRP launch to start, so the next is released."""
    if not session_id:
        return "NoSessionId"
    if not has_holder_file(holder_directory()):
        return "no build hold"
    with release_lock():
        if isinstance(read_holders(holder_directory()), NoHolders):
            return "no build hold"
        cycle = read_cycle_for_change()
        if isinstance(cycle, (NoCycle, DamagedRecordSetAside)):
            return "no hold cycle"
        entry = next((entry for entry in cycle["entries"] if entry["session_id"] == session_id), None)
        if entry is None:
            return "nothing-to-build ignored: this session is not registered"
        if ((not cycle.get("holder_signature", "") or cycle.get("holder_signature", "") == holders_signature())
            and confirm_delivery(entry, cycle.get("holder_generation", 1))):
            save_cycle(cycle)
        if entry["active_call_id"] or not isinstance(read_release_state(entry), (ReleasedAwaitingAdmission, DeliveryQueued)):
            return f"nothing-to-build ignored: {entry_text(entry)}"
        entry["session_pending"] = False
        store_release_state(entry, NothingToBuild(datetime.now().astimezone()))
        save_cycle(cycle)
        return f"{entry_text(entry)}; the release moves on to the next session"


def confirm_delivery(entry: ReleaseEntry, generation: int) -> bool:
    delivery = entry["delivery"]
    if delivery["generation"] == generation and delivery["outcome"] == "QueuedUnconfirmed":
        delivery["outcome"] = "Delivered"
        return True
    return False


def socket_for(session_id: str) -> RecipientLive | RecipientGone | RecipientLookupFailed:
    script = Path(__file__).resolve().parent.parent / "message" / "sessions.py"
    reason = ""
    for attempt in range(LOOKUP_TRIES):
        if attempt:
            time.sleep(LOOKUP_RETRY_S)
        try:
            result = subprocess.run([sys.executable, str(script), "socket", f"session:{session_id}"], capture_output=True, text=True)
        except OSError as error:
            reason = str(error)
            continue
        # Exit 1 is no such live session. Any other failure is the lookup itself failing.
        if result.returncode in (0, 1):
            socket = result.stdout.strip()
            return RecipientLive(socket) if result.returncode == 0 and socket else RecipientGone()
        reason = result.stderr.strip() or f"sessions.py exited {result.returncode}"
    return RecipientLookupFailed(reason)


def send_release(entry: ReleaseEntry, socket: str) -> ReleaseDelivered | ReleaseQueued | ReleaseSendFailed:
    script = Path(__file__).resolve().parent.parent / "message" / "send.py"
    notice = ("/build_hold: your hold has been released. Start your next build or BRP launch now; its memory gate records admission. "
              "With nothing to build, run python3 ~/.claude/scripts/build_hold/build_hold.py nothing-to-build now, so the next session is released. "
              f"Session {entry['session_id']}.")
    try:
        result = subprocess.run([sys.executable, str(script), "--to", f"uds:{socket}", "--text", notice], capture_output=True, text=True)
    except OSError as error:
        return ReleaseSendFailed(str(error))
    if result.returncode == 0:
        return ReleaseDelivered()
    if result.returncode == 1:
        return ReleaseQueued()
    return ReleaseSendFailed(result.stderr.strip() or result.stdout.strip() or f"send.py exited {result.returncode}")


def entry_text(entry: ReleaseEntry) -> str:
    state = read_release_state(entry)
    label = type(state).__name__
    if isinstance(state, MemoryGateReturned):
        label += f"({state.outcome})"
    return f"{entry['name']} [{entry['session_id']}]: {label}"


def cycle_status_lines(cycle: HoldCycle, now: datetime, zone: ZoneInfo | None = None) -> list[str]:
    lines = [entry_text(entry) for entry in cycle["entries"]]
    comparison_time = now if now.tzinfo is not None else now.replace(tzinfo=zone or datetime.now().astimezone().tzinfo)
    for index, entry in enumerate(cycle["entries"]):
        state = read_release_state(entry)
        if not isinstance(state, MemoryGateReturned):
            continue
        ready = state.wait_ended_at + timedelta(seconds=RELEASE_SETTLE_S)
        if comparison_time >= ready:
            continue
        following = next((candidate for candidate in cycle["entries"][index + 1:] if isinstance(read_release_state(candidate), AwaitingRelease)), None)
        if following is not None:
            lines.append(f"next {following['name']} [{following['session_id']}] at {ready.astimezone(zone):%H:%M:%S %Z}")
        break
    return lines


def release_clock() -> datetime:
    return datetime.now().astimezone()


def release_queue_idle(entries: Sequence[ReleaseEntry], generation: int, now: datetime) -> bool:
    for entry in entries:
        state = read_release_state(entry)
        if (entry["active_call_id"]
            or isinstance(state, (AwaitingRelease, DeliveryQueued, ReleasedAwaitingAdmission, WaitingForMemory))
            or any(call["admitted_generation"] != generation for call in entry["calls"])
            or (isinstance(state, MemoryGateReturned) and now < state.wait_ended_at + timedelta(seconds=RELEASE_SETTLE_S))):
            return False
    return True


def queue_told_sessions(cycle: HoldCycle, now: datetime) -> None:
    entries = cycle["entries"]
    generation = cycle.get("holder_generation", 1)
    if not release_queue_idle(entries, generation, now):
        return
    registered = {entry["session_id"] for entry in entries}
    missing = [new_entry(session_id, name) for session_id, name in cycle["recipients"].items()
               if session_id not in registered and not session_id.startswith("verify:")]
    changed = bool(missing)
    for entry in entries:
        if (entry["session_id"] in cycle["recipients"] and not entry["session_id"].startswith("verify:")
            and entry["delivery"]["generation"] != generation):
            entry["session_pending"] = True
            store_release_state(entry, AwaitingRelease())
            changed = True
    if changed:
        entries.extend(missing)
        save_cycle(cycle)


def not_reached_detail(cycle: HoldCycle) -> str:
    outcomes = {"RecipientGone": "session gone", "DeliveryFailed": "send failed",
                "QueuedUnconfirmed": "queued, no answer"}
    missing: list[str] = []
    for entry in cycle["entries"]:
        if entry["session_id"] not in cycle["recipients"] or entry["session_id"].startswith("verify:"):
            continue
        delivery = entry["delivery"]
        if delivery["generation"] == cycle.get("holder_generation", 1) and delivery["outcome"] in outcomes:
            missing.append(f"{entry['name']} ({outcomes[delivery['outcome']]})")
    return " Not reached, tell them yourself: " + ", ".join(missing) if missing else ""


def advance_release(cycle: HoldCycle, now: datetime, *, clock: Callable[[], datetime] = release_clock) -> tuple[bool, str]:
    entries = cycle["entries"]
    for entry in entries:
        prune_calls(entry)
    generation = cycle.get("holder_generation", 1)
    for entry in tuple(entries):
        state = read_release_state(entry)
        pending = any(call["admitted_generation"] != generation for call in entry["calls"])
        if isinstance(state, MemoryGateReturned) and now >= state.wait_ended_at + timedelta(seconds=RELEASE_SETTLE_S) and pending:
            entries.remove(entry)
            store_release_state(entry, AwaitingRelease())
            entries.append(entry)
    resumed = [entry for entry in entries if isinstance(read_release_state(entry), AwaitingRelease)
               and any(call["started"] and call["admitted_generation"] != generation for call in entry["calls"])]
    turn_in_progress = any(entry["active_call_id"]
                           or isinstance(read_release_state(entry), (DeliveryQueued, ReleasedAwaitingAdmission, WaitingForMemory))
                           or (isinstance(state := read_release_state(entry), MemoryGateReturned)
                               and now < state.wait_ended_at + timedelta(seconds=RELEASE_SETTLE_S))
                           for entry in entries)
    if resumed and not turn_in_progress:
        entries[:] = resumed + [entry for entry in entries if entry not in resumed]
    save_cycle(cycle)
    release_start = aware_instant(cycle["release_started_at"])
    queue_told_sessions(cycle, now)
    active = (AwaitingRelease, DeliveryQueued, ReleasedAwaitingAdmission, WaitingForMemory)
    for index, entry in enumerate(entries):
        state = read_release_state(entry)
        if isinstance(state, (RecipientGone, DeliveryFailed)) and entry["active_call_id"]:
            started_at = aware_instant(entry["active_released_at"]) if entry["active_released_at"] else state.attempted_at if isinstance(state, DeliveryFailed) else release_start
            if (now - started_at).total_seconds() < NO_ADMISSION_ACK_S:
                return False, entry_text(entry)
            entry["active_call_id"] = ""
            entry["session_pending"] = False
            store_release_state(entry, NoAdmissionAck(started_at))
            save_cycle(cycle)
            queue_told_sessions(cycle, now)
            continue
        if isinstance(state, WaitingForMemory):
            if (now - state.wait_started_at).total_seconds() < int(os.environ.get("BUILDLOG_MEM_WAIT_LIMIT_S", "900")) + RELEASE_SETTLE_S:
                return False, entry_text(entry)
            entry["active_call_id"] = ""
            entry["session_pending"] = False
            store_release_state(entry, NoAdmissionAck(state.released_at))
            save_cycle(cycle)
            queue_told_sessions(cycle, now)
        elif isinstance(state, (ReleasedAwaitingAdmission, DeliveryQueued)):
            started_at = state.released_at if isinstance(state, ReleasedAwaitingAdmission) else state.attempted_at
            turn_limit_s = NO_ADMISSION_ACK_S
            if (not entry["active_call_id"]
                and any(isinstance(read_release_state(other), AwaitingRelease) for other in entries if other is not entry)):
                turn_limit_s = RELEASE_SETTLE_S
            if (now - started_at).total_seconds() < turn_limit_s:
                if not entry["active_call_id"] and isinstance(socket_for(entry["session_id"]), RecipientGone):
                    entry["session_pending"] = False
                    store_release_state(entry, RecipientGone())
                    save_cycle(cycle)
                    queue_told_sessions(cycle, now)
                    continue
                return False, entry_text(entry)
            entry["active_call_id"] = ""
            entry["session_pending"] = False
            store_release_state(entry, NoAdmissionAck(started_at))
            save_cycle(cycle)
            queue_told_sessions(cycle, now)
        elif isinstance(state, MemoryGateReturned):
            ready = state.wait_ended_at + timedelta(seconds=RELEASE_SETTLE_S)
            if now < ready:
                next_entry = next((following for following in entries[index + 1:] if isinstance(read_release_state(following), AwaitingRelease)), None)
                target = entry_text(next_entry) if next_entry is not None else "final check"
                return False, f"{entry_text(entry)}; next {target} at {ready.astimezone():%H:%M:%S %Z}"
            pending = any(call["admitted_generation"] != generation for call in entry["calls"])
            if pending or entry["session_pending"]:
                entries.remove(entry)
                store_release_state(entry, AwaitingRelease())
                entries.append(entry)
                save_cycle(cycle)
                return False, entry_text(entry)
        elif isinstance(state, AwaitingRelease):
            pending_calls = [call for call in entry["calls"] if call["admitted_generation"] != generation]
            pending_calls.sort(key=lambda call: not call["started"])
            if pending_calls:
                entry["active_call_id"] = pending_calls[0]["call_id"]
                entry["session_admitted_generation"] = generation
                entry["session_pending"] = False
            elif not entry["session_pending"]:
                if entry["session_id"] in cycle["recipients"] and entry["delivery"]["generation"] != generation:
                    entry["session_pending"] = True
                else:
                    store_release_state(entry, NoPendingTurn())
                    save_cycle(cycle)
                    queue_told_sessions(cycle, now)
                    continue
            entry["active_released_at"] = clock().isoformat()
            if entry["session_id"].startswith("verify:"):
                store_release_state(entry, ReleasedAwaitingAdmission(clock()))
                for call in pending_calls:
                    if call["call_id"] == entry["active_call_id"]:
                        call["admitted_generation"] = generation
                save_cycle(cycle)
                return False, entry_text(entry)
            if entry["delivery"]["generation"] == generation:
                store_release_state(entry, ReleasedAwaitingAdmission(clock()))
            else:
                recipient = socket_for(entry["session_id"])
                if isinstance(recipient, RecipientGone):
                    entry["delivery"] = {"generation": generation, "outcome": "RecipientGone"}
                    store_release_state(entry, RecipientGone())
                elif isinstance(recipient, RecipientLookupFailed):
                    print(f"build_hold: {entry_text(entry)}: {recipient.reason}", file=sys.stderr)
                    entry["delivery"] = {"generation": generation, "outcome": "DeliveryFailed"}
                    store_release_state(entry, DeliveryFailed(clock()))
                else:
                    attempted_at = clock()
                    outcome = send_release(entry, recipient.socket)
                    if isinstance(outcome, ReleaseDelivered):
                        entry["delivery"] = {"generation": generation, "outcome": "Delivered"}
                        store_release_state(entry, ReleasedAwaitingAdmission(clock()))
                    elif isinstance(outcome, ReleaseQueued):
                        entry["delivery"] = {"generation": generation, "outcome": "QueuedUnconfirmed"}
                        store_release_state(entry, DeliveryQueued(attempted_at))
                    else:
                        print(f"build_hold: {entry_text(entry)}: {outcome.reason}", file=sys.stderr)
                        entry["delivery"] = {"generation": generation, "outcome": "DeliveryFailed"}
                        store_release_state(entry, DeliveryFailed(attempted_at))
                if isinstance(read_release_state(entry), (RecipientGone, DeliveryFailed)):
                    entry["session_pending"] = False
            if entry["active_call_id"]:
                for call in pending_calls:
                    if call["call_id"] == entry["active_call_id"]:
                        call["admitted_generation"] = generation
            save_cycle(cycle)
            if entry["active_call_id"] or isinstance(read_release_state(entry), active):
                return False, entry_text(entry)
            queue_told_sessions(cycle, now)
    for entry in entries:
        state = read_release_state(entry)
        pending = any(call["admitted_generation"] != generation for call in entry["calls"])
        if pending and not isinstance(state, active):
            store_release_state(entry, AwaitingRelease())
            entry["active_call_id"] = ""
            save_cycle(cycle)
            return False, entry_text(entry)
    if not cycle["recipients"] and (now - release_start).total_seconds() < NO_REGISTRATION_S:
        return False, "waiting for registrations"
    if any(isinstance(read_release_state(entry), active) for entry in entries):
        return False, "waiting for release progress"
    return True, "; ".join(entry_text(entry) for entry in entries) or "NoRegistration timeout; no recipients registered"


def release_cycle(directory: Path, name: str) -> str:
    while True:
        with release_lock():
            cycle = read_cycle_for_change()
            if isinstance(cycle, (NoCycle, DamagedRecordSetAside)):
                result = release_hold(directory, name)
                if result == "released, builds may resume.":
                    return result + " No hold cycle: broadcast this release to every session."
                return result
            path = holder_path(directory, name)
            if not path.is_file():
                raise ValueError(f"{name} held nothing")
            now = datetime.now().astimezone()
            update_holder_generation(cycle)
            holders = read_holders(directory)
            if cycle["holders"].get(name, {}).get("released_at", "") == "":
                cycle["holders"].setdefault(name, {"since": now.isoformat(), "released_at": ""})["released_at"] = now.isoformat()
                save_cycle(cycle)
            if isinstance(holders, ActiveHolders) and len(holders.holders) > 1:
                path.unlink()
                return "released; still held by " + "; ".join(holder_text(holder) for holder in holders.holders if holder.key != name)
            if not cycle["release_started_at"]:
                cycle["release_started_at"] = now.isoformat()
                save_cycle(cycle)
            complete, detail = advance_release(cycle, now)
            if complete:
                path.unlink()
                return f"released, builds may resume. {detail}" + not_reached_detail(cycle)
            print(detail, flush=True)
        time.sleep(RELEASE_POLL_S)


def quiet_verdict(load_1m: float, cores: Cores, processes: Sequence[Process], user: str) -> Quiet | Busy:
    own = Counter(process.command for process in processes if process.user == user and process.command in BUILD_COMMANDS)
    reasons: list[str] = []
    if own:
        reasons.append("your builds still running: " + ", ".join(f"{count} {command}" for command, count in sorted(own.items(), key=lambda item: (-item[1], item[0]))))
    if isinstance(cores, UnknownCores):
        reasons.append("core count unavailable, so the load limit cannot be judged")
    elif load_1m >= cores.count / 4:
        heaviest = sorted(processes, key=lambda process: process.cpu_percent, reverse=True)[:5]
        running = ", ".join(f"{process.user} {process.command} {process.cpu_percent:g}%" for process in heaviest)
        reasons.append(f"1-minute load {load_1m:g} is at or above {cores.count / 4:g}; heaviest: {running or 'none visible'}")
    return Busy(tuple(reasons)) if reasons else Quiet()


def read_processes() -> list[Process]:
    output = subprocess.check_output(["ps", "-eo", "pid=,user:32=,pcpu=,comm="], text=True)
    processes: list[Process] = []
    for line in output.splitlines():
        parts = line.split(maxsplit=3)
        if len(parts) == 4:
            pid, user, cpu, command = parts
            try:
                processes.append(Process(int(pid), user, Path(command).name, float(cpu)))
            except ValueError:
                continue
    return processes


def read_cores() -> Cores:
    count = os.cpu_count()
    return UnknownCores() if count is None else KnownCores(count)


def wait_for_quiet(
    max_wait: float = 600,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    load: Callable[[], float] = lambda: os.getloadavg()[0],
    cores: Callable[[], Cores] = read_cores,
    processes: Callable[[], Sequence[Process]] = read_processes,
    user: str | None = None,
) -> Quiet | Busy:
    if max_wait < 0:
        raise ValueError("max wait must be non-negative")
    current_user = user or os.environ.get("USER", "")
    deadline = clock() + max_wait
    while True:
        result = quiet_verdict(load(), cores(), processes(), current_user)
        if isinstance(result, Quiet) or clock() >= deadline:
            return result
        sleep(min(10, max(0, deadline - clock())))


def main(arguments: list[str]) -> int:
    override_guard.refuse_overrides(Path(__file__), "build_hold/build_hold.py")
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    hold = commands.add_parser("hold")
    _ = hold.add_argument("--holder")
    _ = hold.add_argument("--for", dest="purpose", required=True)
    _ = hold.add_argument("--release-eta")
    _ = hold.add_argument("--zone")
    quiet = commands.add_parser("quiet")
    _ = quiet.add_argument("--max-wait", type=float, default=600)
    release = commands.add_parser("release")
    _ = release.add_argument("--holder")
    _ = release.add_argument("--resume", action="store_true")
    _ = commands.add_parser("wait")
    _ = commands.add_parser("nothing-to-build")
    mark = commands.add_parser("mark")
    _ = mark.add_argument("--state", required=True, choices=["WaitingForMemory", "MemoryGateReturned"])
    _ = mark.add_argument("--outcome", choices=["Granted", "TimedOut", "MeminfoUnavailable"], default="")
    _ = mark.add_argument("--session-id", default=argparse.SUPPRESS)
    _ = mark.add_argument("--call-id", default="")
    _ = mark.add_argument("--cycle-id", default="")
    _ = mark.add_argument("--generation", type=int, default=0)
    recipient = commands.add_parser("record-recipient")
    _ = recipient.add_argument("--session-id", required=True)
    _ = recipient.add_argument("--name", required=True)
    _ = commands.add_parser("status")
    options = parser.parse_args(arguments)
    try:
        action = cast(str, options.action)
        if action == "hold":
            request = release_request(cast(str | None, options.release_eta), cast(str | None, options.zone))
            key, label = holder_of(cast(str | None, options.holder))
            print(start_hold(holder_directory(), key, cast(str, options.purpose), request, datetime.now().astimezone(), label))
        elif action == "quiet":
            verdict = wait_for_quiet(cast(float, options.max_wait))
            if isinstance(verdict, Busy):
                print("; ".join(verdict.reasons))
                return 1
            print("builds are quiet; load is below the limit")
        elif action == "release":
            given = cast(str | None, options.holder)
            resume_requested = cast(bool, options.resume)
            name: str | None = None
            if given is None and resume_requested:
                with release_lock():
                    cycle = read_cycle_for_change()
                    if not isinstance(cycle, (NoCycle, DamagedRecordSetAside)):
                        name = next((holder for holder, instants in cycle["holders"].items() if instants["released_at"] and holder_path(holder_directory(), holder).is_file()), None)
                    elif isinstance(cycle, DamagedRecordSetAside):
                        state = read_holders(holder_directory())
                        if isinstance(state, ActiveHolders) and len(state.holders) == 1:
                            name = state.holders[0].key
            if name is None and given is None and resume_requested:
                raise ValueError("release needs --holder, or --resume for an active release")
            if name is None:
                name = held_key(holder_directory(), given)
            print(release_cycle(holder_directory(), name))
        elif action == "wait":
            print(register_wait(os.environ.get("CLAUDE_CODE_SESSION_ID", "")))
        elif action == "nothing-to-build":
            print(answer_nothing_to_build(os.environ.get("CLAUDE_CODE_SESSION_ID", "")))
        elif action == "mark":
            call_id = cast(str, options.call_id)
            owner = OwningSession(cast(str, options.session_id)) if hasattr(options, "session_id") else owning_session()
            session_id = waiter_key(call_id) if call_id and not hasattr(options, "session_id") else owner.session_id if isinstance(owner, OwningSession) else ""
            print(mark_gate(session_id, cast(str, options.state), cast(str, options.outcome),
                            call_id=call_id, cycle_id=cast(str, options.cycle_id), generation=cast(int, options.generation)))
        elif action == "record-recipient":
            record_recipients([(cast(str, options.name), cast(str, options.session_id))])
        else:
            state = read_holders(holder_directory())
            if isinstance(state, NoHolders):
                print("no build hold")
            else:
                for holder in state.holders:
                    print(f"{holder.name} since {holder.since.astimezone():%H:%M %Z}, for {holder.purpose} - release eta: {release_eta_text(holder.release)}")
                try:
                    cycle = read_cycle()
                except ReleaseRecordReadError as error:
                    print(release_record_error_line(error))
                else:
                    if cycle is not None:
                        for line in cycle_status_lines(cycle, datetime.now().astimezone()):
                            print(line)
    except (ValueError, OSError) as error:
        print(f"build_hold: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
