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
from typing import Literal, Required, TypedDict, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "message"))
from sessions import addressed, live_sessions  # noqa: E402


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


class HoldCycle(TypedDict):
    id: str
    opened_at: str
    holders: dict[str, dict[str, str]]
    recipients: dict[str, str]
    entries: list[ReleaseEntry]
    release_started_at: str


class ReleaseRecordReadError(ValueError):
    """The stored hold cycle cannot be read safely."""


@dataclass(frozen=True)
class NoCycle:
    pass


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


ReleaseState = (AwaitingRelease | RecipientGone | DeliveryFailed | DeliveryQueued |
                ReleasedAwaitingAdmission | WaitingForMemory | MemoryGateReturned |
                NothingToBuild | NoAdmissionAck | NoRegistration)


def read_release_state(entry: Mapping[str, object]) -> ReleaseState:
    state = entry.get("state")
    session_id = entry.get("session_id")
    if not isinstance(state, str) or not isinstance(session_id, str) or not isinstance(entry.get("name"), str):
        raise ValueError("invalid release entry identity or state")
    fields = set(entry) - {"session_id", "name", "state"}
    expected: dict[str, set[str]] = {
        "AwaitingRelease": set(),
        "RecipientGone": set(),
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
    cycle: HoldCycle = {"id": uuid.uuid4().hex, "opened_at": now.isoformat(), "holders": {}, "recipients": {}, "entries": [], "release_started_at": ""}
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
        notice = write_hold(directory, name, purpose, request, now, label)
        if not isinstance(cycle, (NoCycle, DamagedRecordSetAside)):
            cycle["holders"][name] = {"since": now.isoformat(), "released_at": ""}
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


def register_wait(session_id: str, name: str = "") -> str:
    if not session_id:
        return "NoSessionId: hold remains active; this session cannot register"
    if not has_holder_file(holder_directory()):
        return "no build hold"
    with release_lock():
        if isinstance(read_holders(holder_directory()), NoHolders):
            return "no build hold"
        cycle = read_cycle_for_change()
        if isinstance(cycle, (NoCycle, DamagedRecordSetAside)):
            return "no hold cycle: wait for the release broadcast"
        existing = next((entry for entry in cycle["entries"] if entry["session_id"] == session_id), None)
        if existing is None:
            cycle["entries"].append(new_entry(session_id, name or cycle["recipients"].get(session_id, session_id)))
            save_cycle(cycle)
        elif isinstance(read_release_state(existing), NoRegistration):
            cycle["entries"].remove(existing)
            store_release_state(existing, AwaitingRelease())
            cycle["entries"].append(existing)
            save_cycle(cycle)
        return f"registered {session_id}; wait for direct release before building"


def new_entry(session_id: str, name: str) -> ReleaseEntry:
    return {"session_id": session_id, "name": name, "state": "AwaitingRelease"}


def mark_gate(session_id: str, state: str, outcome: str = "") -> str:
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
        for entry in cycle["entries"]:
            if entry["session_id"] != session_id:
                continue
            previous = read_release_state(entry)
            if not isinstance(previous, (ReleasedAwaitingAdmission, DeliveryQueued, WaitingForMemory)):
                continue
            instant = datetime.now().astimezone()
            if state == "WaitingForMemory" and not isinstance(previous, WaitingForMemory):
                released_at = previous.released_at if isinstance(previous, ReleasedAwaitingAdmission) else previous.attempted_at
                store_release_state(entry, WaitingForMemory(released_at, instant))
            elif state == "MemoryGateReturned" and outcome in {"Granted", "TimedOut", "MeminfoUnavailable"}:
                store_release_state(entry, MemoryGateReturned(instant, cast(GateOutcome, outcome)))
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
        if not isinstance(read_release_state(entry), (ReleasedAwaitingAdmission, DeliveryQueued)):
            return f"nothing-to-build ignored: {entry_text(entry)}"
        store_release_state(entry, NothingToBuild(datetime.now().astimezone()))
        save_cycle(cycle)
        return f"{entry_text(entry)}; the release moves on to the next session"


class SessionLookupUnavailable(Exception):
    """`sessions.py` could not read the session records, which says nothing about the session."""


def socket_for(session_id: str) -> str | None:
    """The session's socket, or None when it is not running."""
    script = Path(__file__).resolve().parent.parent / "message" / "sessions.py"
    reason = ""
    for attempt in range(LOOKUP_TRIES):
        if attempt:
            time.sleep(LOOKUP_RETRY_S)
        result = subprocess.run([sys.executable, str(script), "socket", f"session:{session_id}"], capture_output=True, text=True)
        # Exit 1 is no such live session. Any other failure is the lookup itself failing.
        if result.returncode in (0, 1):
            return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None
        reason = result.stderr.strip() or f"sessions.py exited {result.returncode}"
    raise SessionLookupUnavailable(reason)


def send_release(entry: ReleaseEntry, socket: str) -> int:
    script = Path(__file__).resolve().parent.parent / "message" / "send.py"
    notice = ("/build_hold: your hold has been released. Start your next build or BRP launch now; its memory gate records admission. "
              "With nothing to build, run python3 ~/.claude/scripts/build_hold/build_hold.py nothing-to-build now, so the next session is released. "
              f"Session {entry['session_id']}.")
    result = subprocess.run([sys.executable, str(script), "--to", f"uds:{socket}", "--text", notice], capture_output=True, text=True)
    return result.returncode


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


def advance_release(cycle: HoldCycle, now: datetime, *, clock: Callable[[], datetime] = release_clock) -> tuple[bool, str]:
    entries = cycle["entries"]
    release_start = aware_instant(cycle["release_started_at"])
    registered = {entry["session_id"] for entry in entries}
    missing = [(session_id, name) for session_id, name in cycle["recipients"].items() if session_id not in registered]
    if missing and (now - release_start).total_seconds() >= NO_REGISTRATION_S:
        for session_id, name in missing:
            entry = new_entry(session_id, name)
            store_release_state(entry, NoRegistration())
            entries.append(entry)
        missing = []
        save_cycle(cycle)
    active = (AwaitingRelease, DeliveryQueued, ReleasedAwaitingAdmission, WaitingForMemory)
    for index, entry in enumerate(entries):
        state = read_release_state(entry)
        if isinstance(state, WaitingForMemory):
            if (now - state.wait_started_at).total_seconds() < int(os.environ.get("BUILDLOG_MEM_WAIT_LIMIT_S", "900")) + RELEASE_SETTLE_S:
                return False, entry_text(entry)
            store_release_state(entry, NoAdmissionAck(state.released_at))
            save_cycle(cycle)
        elif isinstance(state, (ReleasedAwaitingAdmission, DeliveryQueued)):
            started_at = state.released_at if isinstance(state, ReleasedAwaitingAdmission) else state.attempted_at
            if (now - started_at).total_seconds() < NO_ADMISSION_ACK_S:
                return False, entry_text(entry)
            store_release_state(entry, NoAdmissionAck(started_at))
            save_cycle(cycle)
        elif isinstance(state, MemoryGateReturned):
            ready = state.wait_ended_at + timedelta(seconds=RELEASE_SETTLE_S)
            if now < ready:
                next_entry = next((following for following in entries[index + 1:] if isinstance(read_release_state(following), AwaitingRelease)), None)
                target = entry_text(next_entry) if next_entry is not None else "final check"
                return False, f"{entry_text(entry)}; next {target} at {ready.astimezone():%H:%M:%S %Z}"
        elif isinstance(state, AwaitingRelease):
            try:
                socket = socket_for(entry["session_id"])
            except SessionLookupUnavailable as error:
                # Not knowing is not the session being gone: this is a delivery that failed.
                print(f"build_hold: {entry_text(entry)}: {error}", file=sys.stderr)
                store_release_state(entry, DeliveryFailed(clock()))
            else:
                if socket is None:
                    store_release_state(entry, RecipientGone())
                else:
                    attempted_at = clock()
                    outcome = send_release(entry, socket)
                    if outcome == 0:
                        store_release_state(entry, ReleasedAwaitingAdmission(clock()))
                    elif outcome == 1:
                        store_release_state(entry, DeliveryQueued(attempted_at))
                    else:
                        store_release_state(entry, DeliveryFailed(attempted_at))
            save_cycle(cycle)
            if isinstance(read_release_state(entry), active):
                return False, entry_text(entry)
    if missing or (not cycle["recipients"] and (now - release_start).total_seconds() < NO_REGISTRATION_S):
        return False, "waiting for registrations" + (": " + ", ".join(name for _, name in missing) if missing else "")
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
                return f"released, builds may resume. {detail}"
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
    _ = mark.add_argument("--session-id", default="")
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
            print(mark_gate(cast(str, options.session_id) or os.environ.get("CLAUDE_CODE_SESSION_ID", ""), cast(str, options.state), cast(str, options.outcome)))
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
