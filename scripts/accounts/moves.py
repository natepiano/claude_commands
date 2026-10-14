"""Account transitions: a request is distinct from a confirmed session move."""
from __future__ import annotations

import fcntl
import json
from datetime import datetime
from pathlib import Path
from typing import Literal, NotRequired, TypedDict, cast


class MoveCommon(TypedDict):
    at: str
    tool: Literal["claude", "codex"]
    subject: str
    move_id: str


RequestedFields = TypedDict("RequestedFields", {"from": str, "to": str, "why": str,
                                              "window": Literal["weekly", "five_hour", "unknown"], "reset": NotRequired[str]})


class RequestedMove(MoveCommon, RequestedFields):
    kind: Literal["requested"]


class ConfirmedMove(MoveCommon):
    kind: Literal["confirmed"]
    home: NotRequired[str]
    config_dir: NotRequired[str]
    seat: NotRequired[str]
    run: NotRequired[str]


class FailedMove(MoveCommon):
    kind: Literal["failed"]
    reason: str


class WaitedMove(MoveCommon):
    kind: Literal["waited"]
    window: Literal["weekly", "five_hour", "unknown"]
    reset: str


class WaitEndedMove(MoveCommon):
    kind: Literal["wait ended"]


class NoRoomMove(MoveCommon):
    kind: Literal["no room"]
    earliest_reset: str


class RoomAgainMove(MoveCommon):
    kind: Literal["room again"]
    label: str


MoveEntry = RequestedMove | ConfirmedMove | FailedMove | WaitedMove | WaitEndedMove | NoRoomMove | RoomAgainMove


def store_file() -> Path:
    return Path.home() / ".local" / "state" / "accounts" / "moves.jsonl"


def _valid(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    fields = cast(dict[str, object], value)
    if not all(isinstance(fields.get(key), str) and fields[key] for key in ("at", "subject", "move_id")):
        return False
    if fields.get("tool") not in ("claude", "codex"):
        return False
    try:
        if datetime.fromisoformat(cast(str, fields["at"])).tzinfo is None:
            return False
        for field in ("reset", "earliest_reset"):
            if field in fields and isinstance(fields[field], str) and fields[field]:
                if datetime.fromisoformat(cast(str, fields[field])).tzinfo is None:
                    return False
    except ValueError:
        return False
    if "window" in fields and fields["window"] not in ("weekly", "five_hour", "unknown"):
        return False
    kind = fields.get("kind")
    if kind in ("requested", "waited") and "reset" in fields:
        if not isinstance(fields["reset"], str) or not fields["reset"]:
            return False
    required = {"requested": ("from", "to", "why", "window"),
                "failed": ("reason",), "waited": ("window", "reset"),
                "wait ended": (), "no room": ("earliest_reset",), "room again": ("label",)}
    if kind == "confirmed":
        return (isinstance(fields.get("home"), str) or isinstance(fields.get("config_dir"), str)) and (
            "seat" not in fields or isinstance(fields["seat"], str))
    return isinstance(kind, str) and kind in required and all(isinstance(fields.get(key), str) for key in required[kind])


def append_move(entry: MoveEntry) -> None:
    """Append a complete transition while holding the shared writer lock."""
    if not _valid(entry):
        raise ValueError("invalid account move")
    path = store_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as output:
        fcntl.flock(output.fileno(), fcntl.LOCK_EX)
        if entry["kind"] in ("confirmed", "wait ended", "room again"):
            _ = output.seek(0)
            for line in output:
                try:
                    previous = cast(object, json.loads(line))
                except ValueError:
                    continue
                if _valid(previous):
                    held = cast(dict[str, object], previous)
                    if (held.get("kind") == entry["kind"] and held.get("move_id") == entry["move_id"]
                            and held.get("tool") == entry["tool"] and held.get("subject") == entry["subject"]
                            and (entry["kind"] != "confirmed" or
                                 (held.get("seat") == entry.get("seat") and held.get("run") == entry.get("run")))):
                        return
        _ = output.write(json.dumps(entry, sort_keys=True) + "\n")
        output.flush()


def read_moves() -> list[MoveEntry]:
    """Read valid transitions in order, skipping malformed or incomplete lines."""
    try:
        with store_file().open(encoding="utf-8") as source:
            fcntl.flock(source.fileno(), fcntl.LOCK_SH)
            lines = source.readlines()
    except FileNotFoundError:
        return []
    entries: list[MoveEntry] = []
    for line in lines:
        try:
            value = cast(object, json.loads(line))
        except ValueError:
            continue
        if _valid(value):
            entries.append(cast(MoveEntry, value))
    return entries
