#!/usr/bin/env python3
"""Try the local postmaster outbox before handing delivery to the relay."""

from __future__ import annotations

import hashlib
import json
import os as os
import re
import sys
import time as time
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, TypeGuard, TypedDict, cast

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import sessions  # noqa: E402
from user_zone import user_zone  # noqa: E402

WAIT_SECONDS = 3.0
CLAIM_GRACE_SECONDS = 2.0
HEARTBEAT_MAX_AGE_SECONDS = 2.0
POLL_SECONDS = 0.05
RELAY_ONLY_SENDERS = frozenset({"shutdown"})
MAX_SAFE_INTEGER = 9007199254740991

PostmasterMiss = Literal["absent", "stale", "self", "unclaimed", "outbox-error", "failed",
                         "expired", "claimed-no-result", "relay-only-sender"]
ModOutcome = Literal["delivered", "failed", "expired", "withdrawn", "abandoned"]


class OutboxEntry(TypedDict):
    id: str
    session_id: str
    socket: str
    text: str
    sha256: str
    deadline_ms: int
    sender: str
    reply_to_session_id: str


class OutboxResult(TypedDict):
    id: str
    outcome: ModOutcome
    detail: str
    sha256: str
    claimed_ms: int
    finished_ms: int


class Heartbeat(TypedDict):
    session_id: str
    at_ms: int
    version: str
    run: str


@dataclass(frozen=True)
class ReplyToSession:
    session_id: str


@dataclass(frozen=True)
class ReplyUnroutable:
    pass


ReplyDestination = ReplyToSession | ReplyUnroutable


@dataclass(frozen=True)
class RegisteredClaudeRecipient:
    session: sessions.SessionRecord


@dataclass(frozen=True)
class RecipientNotInRegistry:
    pass


PostmasterRecipient = RegisteredClaudeRecipient | RecipientNotInRegistry


@dataclass(frozen=True)
class Delivered:
    seconds: float
    verbatim: bool


@dataclass(frozen=True)
class RelayRequired:
    reason: PostmasterMiss
    seconds: float


Attempt = Delivered | RelayRequired


@dataclass(frozen=True)
class JsonValue:
    value: object


@dataclass(frozen=True)
class MissingJson:
    pass


@dataclass(frozen=True)
class UnreadableJson:
    pass


JsonRead = JsonValue | MissingJson | UnreadableJson


@dataclass(frozen=True)
class FreshHeartbeat:
    record: Heartbeat
    age: float


@dataclass(frozen=True)
class NoFreshHeartbeat:
    reason: Literal["absent", "stale"]


HeartbeatRead = FreshHeartbeat | NoFreshHeartbeat


@dataclass(frozen=True)
class ResultPending:
    pass


def _record(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)


def _timestamp(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_SAFE_INTEGER


def _entry_id(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[a-fA-F0-9]{32}", value) is not None


def _hash(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def is_outbox_entry(value: object) -> TypeGuard[OutboxEntry]:
    return (_record(value) and _entry_id(value.get("id"))
            and isinstance(value.get("session_id"), str) and value["session_id"] != ""
            and isinstance(value.get("sender"), str) and value["sender"] != ""
            and all(isinstance(value.get(field), str) for field in ("socket", "text", "reply_to_session_id"))
            and _hash(value.get("sha256")) and _timestamp(value.get("deadline_ms")))


def is_outbox_result(value: object) -> TypeGuard[OutboxResult]:
    return (_record(value) and _entry_id(value.get("id"))
            and value.get("outcome") in ("delivered", "failed", "expired", "withdrawn", "abandoned")
            and isinstance(value.get("detail"), str) and _hash(value.get("sha256"))
            and _timestamp(value.get("claimed_ms")) and _timestamp(value.get("finished_ms")))


def is_heartbeat(value: object) -> TypeGuard[Heartbeat]:
    return (_record(value) and isinstance(value.get("session_id"), str) and value["session_id"] != ""
            and _timestamp(value.get("at_ms")) and isinstance(value.get("version"), str) and value["version"] != ""
            and isinstance(value.get("run"), str) and value["run"] != "")


def new_entry_id() -> str:
    return uuid.uuid4().hex


def reply_destination(sender: str, live: list[sessions.SessionRecord]) -> ReplyDestination:
    matches = [session for session in live if session["name"] == sender]
    return ReplyToSession(matches[0]["sessionId"]) if len(matches) == 1 else ReplyUnroutable()


def wire_entry(entry_id: str, session: sessions.SessionRecord, sender: str,
               destination: ReplyDestination, text: str, deadline_ms: int) -> OutboxEntry:
    return {"id": entry_id, "session_id": session["sessionId"], "socket": f"uds:{session['messagingSocketPath']}",
            "text": text, "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "deadline_ms": deadline_ms,
            "sender": sender, "reply_to_session_id": destination.session_id if isinstance(destination, ReplyToSession) else ""}


def _read_json(path: Path) -> JsonRead:
    try:
        return JsonValue(cast(object, json.loads(path.read_text(encoding="utf-8"))))
    except FileNotFoundError:
        return MissingJson()
    except (OSError, ValueError):
        return UnreadableJson()


def _heartbeat(root: Path, live: list[sessions.SessionRecord]) -> HeartbeatRead:
    for index in range(3):
        read = _read_json(root / "heartbeat.json")
        if isinstance(read, MissingJson):
            return NoFreshHeartbeat("absent")
        if isinstance(read, JsonValue):
            if not is_heartbeat(read.value):
                return NoFreshHeartbeat("stale")
            age = time.time() - read.value["at_ms"] / 1000
            if 0 <= age <= HEARTBEAT_MAX_AGE_SECONDS and any(
                session["sessionId"] == read.value["session_id"] for session in live
            ):
                return FreshHeartbeat(read.value, age)
            return NoFreshHeartbeat("stale")
        if index < 2:
            time.sleep(0.02)
    return NoFreshHeartbeat("stale")


def _remove(path: Path) -> bool:
    try:
        path.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def _cleanup(root: Path, entry_id: str) -> None:
    # Keep delivery markers when a published entry could still be read by the mod.
    if not _remove(root / "outbox" / f"{entry_id}.json"):
        return
    _ = _remove(root / "outbox" / f".{entry_id}.json.tmp")
    _ = _remove(root / "outbox" / f".{entry_id}.withdrawn")
    _ = _remove(root / "claimed" / f"{entry_id}.json")
    _ = _remove(root / "results" / f"{entry_id}.json")


def _files(directory: Path) -> list[Path]:
    try:
        return [path for path in directory.iterdir() if path.is_file()]
    except OSError:
        return []


def _prune(root: Path, folder: str, seconds: float) -> None:
    cutoff = time.time() - seconds
    for path in _files(root / folder):
        try:
            if path.stat().st_mtime >= cutoff:
                continue
            if folder in {"claimed", "results"} and (root / "outbox" / path.name).exists():
                continue
            _ = _remove(path)
        except OSError:
            continue


def _collect(root: Path) -> None:
    for folder in ("outbox", "claimed", "results"):
        _prune(root, folder, 10 * 60)
    _prune(root, "delivered", 30 * 60)


def _result(root: Path, entry: OutboxEntry, started: float) -> Attempt | ResultPending:
    read = _read_json(root / "results" / f"{entry['id']}.json")
    if not isinstance(read, JsonValue) or not is_outbox_result(read.value) or read.value["id"] != entry["id"]:
        return ResultPending()
    seconds = time.monotonic() - started
    match read.value["outcome"]:
        case "delivered":
            return Delivered(seconds, read.value["sha256"] == entry["sha256"])
        case "failed" | "expired" as reason:
            return RelayRequired(reason, seconds)
        case "withdrawn":
            return RelayRequired("unclaimed", seconds)
        case "abandoned":
            return RelayRequired("claimed-no-result", seconds)


def _poll(root: Path, entry: OutboxEntry, started: float, deadline: float) -> Attempt | ResultPending:
    while True:
        result = _result(root, entry, started)
        if not isinstance(result, ResultPending):
            return result
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return result
        time.sleep(min(POLL_SECONDS, remaining))


def _withdraw(root: Path, entry_id: str) -> bool:
    entry = root / "outbox" / f"{entry_id}.json"
    withdrawn = root / "outbox" / f".{entry_id}.withdrawn"
    try:
        os.replace(entry, withdrawn)
    except FileNotFoundError:
        return True
    except OSError:
        return _remove(entry)
    _ = _remove(withdrawn)
    return True


def attempt(state: Path, session: sessions.SessionRecord, sender: str, entry_id: str,
            text: str, live: list[sessions.SessionRecord]) -> Attempt:
    started = time.monotonic()
    if sender in RELAY_ONLY_SENDERS:
        return RelayRequired("relay-only-sender", time.monotonic() - started)
    root = state / "postmaster"
    heartbeat = _heartbeat(root, live)
    if isinstance(heartbeat, NoFreshHeartbeat):
        return RelayRequired(heartbeat.reason, time.monotonic() - started)
    if heartbeat.record["session_id"] == session["sessionId"]:
        return RelayRequired("self", time.monotonic() - started)
    deadline = time.monotonic() + WAIT_SECONDS
    entry = wire_entry(entry_id, session, sender, reply_destination(sender, live), text,
                       int(time.time() * 1000) + int(WAIT_SECONDS * 1000))
    temporary = root / "outbox" / f".{entry_id}.json.tmp"
    try:
        try:
            temporary.parent.mkdir(parents=True, exist_ok=True)
            _ = temporary.write_text(json.dumps(entry), encoding="utf-8")
            os.replace(temporary, root / "outbox" / f"{entry_id}.json")
        except OSError:
            return RelayRequired("outbox-error", time.monotonic() - started)
        result = _poll(root, entry, started, deadline)
        if not isinstance(result, ResultPending):
            return result
        if not _withdraw(root, entry_id):
            return RelayRequired("claimed-no-result", time.monotonic() - started)
        try:
            claimed = (root / "claimed" / f"{entry_id}.json").exists()
        except OSError:
            claimed = True
        if not claimed:
            return RelayRequired("unclaimed", time.monotonic() - started)
        result = _poll(root, entry, started, time.monotonic() + CLAIM_GRACE_SECONDS)
        return (RelayRequired("claimed-no-result", time.monotonic() - started)
                if isinstance(result, ResultPending) else result)
    finally:
        _cleanup(root, entry_id)
        _collect(root)


def _today_counts(state: Path) -> tuple[Counter[str], Counter[str]]:
    zone = user_zone()
    today = datetime.fromtimestamp(time.time(), zone).date()
    via: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    try:
        with (state / "log.jsonl").open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    value = cast(object, json.loads(line))
                    if not _record(value) or not isinstance(value.get("time"), str):
                        continue
                    stamp = datetime.fromisoformat(cast(str, value["time"]))
                    if stamp.astimezone(zone).date() != today:
                        continue
                    transport, reason = value.get("via"), value.get("postmaster")
                    if isinstance(transport, str):
                        via[transport] += 1
                    if isinstance(reason, str):
                        reasons[reason] += 1
                except (ValueError, OverflowError):
                    continue
    except OSError:
        pass
    return via, reasons


def status(state: Path) -> int:
    root = state / "postmaster"
    _collect(root)
    _prune(root, "errors", 7 * 24 * 60 * 60)
    heartbeat = _heartbeat(root, sessions.live_sessions())
    running = (f"running, heartbeat {heartbeat.age:.1f} s old, worker {heartbeat.record['run']}"
               if isinstance(heartbeat, FreshHeartbeat) else "not running")
    print(f"postmaster: {running}; errors: {len(_files(root / 'errors'))}; replies: {len(_files(root / 'replies'))}")
    via, reasons = _today_counts(state)
    print("today: via " + ", ".join(f"{name}={count}" for name, count in sorted(via.items()))
          + "; postmaster " + ", ".join(f"{name}={count}" for name, count in sorted(reasons.items())))
    return 0 if isinstance(heartbeat, FreshHeartbeat) else 1


def main(argv: list[str]) -> int:
    if argv != ["status"]:
        print("usage: postmaster.py status", file=sys.stderr)
        return 2
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "message"
    return status(state)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
