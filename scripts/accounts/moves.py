"""Account transitions: a request is distinct from a confirmed session move."""
from __future__ import annotations

import fcntl
import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, NotRequired, TypedDict, cast, override


@dataclass(frozen=True)
class HomeAccountUnresolved:
    """The journal lacks evidence of the numbered login this home held then."""

    home: Path

    @override
    def __str__(self) -> str:
        return "home account unresolved"


HistoricalAccountLabel = str | HomeAccountUnresolved


class MoveCommon(TypedDict):
    at: str
    tool: Literal["claude", "codex"]
    subject: str
    move_id: str


RequestedFields = TypedDict("RequestedFields", {"from": HistoricalAccountLabel, "to": HistoricalAccountLabel, "why": str,
                                              "window": Literal["weekly", "five_hour", "unknown"], "reset": NotRequired[str]})


class RequestedMove(MoveCommon, RequestedFields):
    kind: Literal["requested"]
    cause: NotRequired[Literal["credits off"]]


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
    label: HistoricalAccountLabel


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
    if "cause" in fields and (kind != "requested" or fields["cause"] != "credits off"):
        return False
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


def _canonical_entry(entry: MoveEntry) -> MoveEntry:
    from scripts.whoami.note_file import canonical_label

    fields: dict[str, object] = dict(entry)
    for field in ("from", "to", "label"):
        value = fields.get(field)
        if isinstance(value, str):
            fields[field] = canonical_label(value)
    return cast(MoveEntry, cast(object, fields))


def append_move(entry: MoveEntry) -> None:
    """Append a complete transition while holding the shared writer lock."""
    if not _valid(entry):
        raise ValueError("invalid account move")
    entry = _canonical_entry(entry)
    if any(entry.get(field) == "default" for field in ("from", "to", "label")):
        raise ValueError("name the account by number")
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
    # Keep this module a standard-library leaf until a journal is actually read.
    from scripts.whoami.note_file import NumberedAccountLabel, parse_account_label

    def numbered(value: object, tool: str) -> str | HomeAccountUnresolved:
        parsed = parse_account_label(value) if isinstance(value, str) else None
        if isinstance(parsed, NumberedAccountLabel) and parsed.tool == tool:
            return parsed.label
        return HomeAccountUnresolved(Path.home() / f".{tool}")

    evidence = _role_evidence(store_file().with_name("roles.jsonl"))
    shutdown = _shutdown_evidence()
    # Never read a present-day login to interpret a past home.
    homes = {"claude": Path.home() / ".claude",
             "codex": Path.home() / ".codex"}

    def historical(entry: MoveEntry, field: str) -> HistoricalAccountLabel:
        tool = entry["tool"]
        home = homes[tool]
        for record in shutdown:
            if record.get("request_id") != entry["move_id"]:
                continue
            if field == "to":
                label = numbered(record.get("target"), tool)
                if isinstance(label, str):
                    return label
            session = record.get("session")
            if isinstance(session, dict):
                values = cast(dict[str, object], session)
                directory = values.get("config_dir", str(Path.home() / ".claude"))
                if directory == str(home) and field != "to":
                    label = numbered(record.get("source_label", record.get("label")), tool)
                    if isinstance(label, str):
                        return label
        at = datetime.fromisoformat(entry["at"])
        matching = sorted((item for item in evidence if item.tool == tool and item.at <= at), key=lambda item: item.at)
        return numbered(matching[-1].label, tool) if matching else HomeAccountUnresolved(home)

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
            entry = _canonical_entry(cast(MoveEntry, value))
            fields: dict[str, object] = dict(entry)
            for field in ("from", "to", "label"):
                if fields.get(field) == "default":
                    fields[field] = historical(entry, field)
            entries.append(cast(MoveEntry, cast(object, fields)))
    return entries


@dataclass(frozen=True)
class HomeAccountEvidence:
    at: datetime
    tool: Literal["claude", "codex"]
    label: HistoricalAccountLabel


def _role_evidence(path: Path) -> list[HomeAccountEvidence]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return []
    found: list[HomeAccountEvidence] = []
    for line in lines:
        try:
            raw = cast(object, json.loads(line))
            if not isinstance(raw, dict):
                continue
            row = cast(dict[str, object], raw)
            role, at = row.get("role"), row.get("at")
            if role not in ("unit", "seats") or not isinstance(at, str):
                continue
            tool = "claude" if role == "unit" else "codex"
            account_label = row.get("account_label")
            label = (account_label if row.get("label") == "default" and isinstance(account_label, str)
                     else HomeAccountUnresolved(Path.home() / f".{tool}"))
            stamp = datetime.fromisoformat(at)
            if stamp.tzinfo is not None:
                found.append(HomeAccountEvidence(stamp, tool, label))
        except ValueError:
            continue
    return found


def _shutdown_evidence() -> list[dict[str, object]]:
    root = Path(os.environ.get("SHUTDOWN_STATE_DIR", str(Path.home() / ".local/state/shutdown")))
    found: list[dict[str, object]] = []
    for path in root.glob("**/*.json"):
        try:
            raw = cast(object, json.loads(path.read_text(encoding="utf-8")))
            if isinstance(raw, dict):
                found.append(cast(dict[str, object], raw))
        except (OSError, UnicodeError, ValueError):
            continue
    return found
