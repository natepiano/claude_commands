"""Own phone delivery for a session outage after its shutdown record is gone."""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
import time
import uuid
from collections.abc import Generator, Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, NotRequired, TypedDict, cast


class AlertLedgerEntry(TypedDict):
    state: Literal["claimed", "delivered"]
    token: str
    pid: NotRequired[int]
    at: NotRequired[float]


# Fifteen minutes exceeds the review, send and remote transport deadlines (under five minutes).
CLAIM_LEASE_SECONDS = 15 * 60


@dataclass(frozen=True)
class AlertClaim:
    session_id: str
    token: str


@dataclass(frozen=True)
class AlertInFlight:
    """Another sender owns delivery for this outage."""


@dataclass(frozen=True)
class AlertDelivered:
    """This outage already sent its phone push."""


def _path() -> Path:
    return Path.home() / ".local/state/accounts/unit_outage_alerts.json"


@contextmanager
def _locked_entries() -> Generator[dict[str, AlertLedgerEntry]]:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            raw = cast(object, json.loads(path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            raw = {}
        if not isinstance(raw, dict):
            raise ValueError("invalid outage alert ledger")
        entries: dict[str, AlertLedgerEntry] = {}
        for session_id, item in cast(dict[str, object], raw).items():
            if not isinstance(item, dict):
                raise ValueError("invalid outage alert entry")
            fields = cast(dict[str, object], item)
            state, token = fields.get("state"), fields.get("token")
            if state not in ("claimed", "delivered") or not isinstance(token, str):
                raise ValueError("invalid outage alert entry")
            entry = AlertLedgerEntry(state=state, token=token)
            pid, at = fields.get("pid"), fields.get("at")
            if isinstance(pid, int) and isinstance(at, (int, float)):
                entry.update(pid=pid, at=float(at))
            entries[session_id] = entry
        before = entries.copy()
        yield entries
        if entries == before:
            return
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(entries, stream)
                _ = stream.write("\n")
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)


def _owner_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def claim(session_id: str) -> AlertClaim | AlertInFlight | AlertDelivered:
    with _locked_entries() as entries:
        existing = entries.get(session_id)
        if existing is not None:
            if existing["state"] == "delivered":
                return AlertDelivered()
            # Entries from before owner leases have no active owner.
            if (time.time() - existing.get("at", 0) < CLAIM_LEASE_SECONDS
                    and _owner_alive(existing.get("pid", 0))):
                return AlertInFlight()
        token = uuid.uuid4().hex
        entries[session_id] = AlertLedgerEntry(state="claimed", token=token, pid=os.getpid(), at=time.time())
        return AlertClaim(session_id, token)


def delivered(claimed: AlertClaim) -> None:
    with _locked_entries() as entries:
        existing = entries.get(claimed.session_id)
        if existing is not None and existing["state"] == "claimed" and existing["token"] == claimed.token:
            entries[claimed.session_id] = AlertLedgerEntry(state="delivered", token=claimed.token)


def release(claimed: AlertClaim) -> None:
    with _locked_entries() as entries:
        existing = entries.get(claimed.session_id)
        if existing is not None and existing["state"] == "claimed" and existing["token"] == claimed.token:
            del entries[claimed.session_id]


def clear(session_id: str) -> None:
    """A live session ends the outage, including any old in-flight delivery."""
    with _locked_entries() as entries:
        _ = entries.pop(session_id, None)


@contextmanager
def reserve(session_ids: Iterable[str]) -> Generator[dict[str, AlertClaim]]:
    """Release every unfinished claim when a sender finishes or fails."""
    held: dict[str, AlertClaim] = {}
    try:
        for session_id in dict.fromkeys(session_ids):
            owned = claim(session_id)
            if isinstance(owned, AlertClaim):
                held[session_id] = owned
        yield held
    finally:
        for owned in held.values():
            release(owned)
