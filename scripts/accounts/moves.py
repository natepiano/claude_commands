"""Account transitions: a request is distinct from a confirmed session move."""
from __future__ import annotations

import fcntl
import json
import os
import re
from dataclasses import dataclass
from importlib import import_module
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NotRequired, TypedDict, Protocol, cast, override, assert_never

from scripts.accounts import role_map, worker_names

if TYPE_CHECKING:
    from scripts.whoami.account import ResolvedHistoryLabel, HistoricalAccountUnresolved


class HistoryResolver(Protocol):
    def resolve_history_label(self, label: str, at: str, *, login: str = "") -> ResolvedHistoryLabel | HistoricalAccountUnresolved: ...


@dataclass(frozen=True)
class HomeAccountUnresolved:
    """The journal lacks evidence of the numbered login this home held then."""

    home: Path

    @override
    def __str__(self) -> str:
        return "home account unresolved"


HistoricalAccountLabel = str | HomeAccountUnresolved


class MoveCommon(TypedDict):
    request_id: NotRequired[str]
    at: str
    tool: Literal["claude", "codex"]
    subject: str
    move_id: str


RequestedFields = TypedDict("RequestedFields", {"from": HistoricalAccountLabel, "to": HistoricalAccountLabel, "why": str,
                                              "window": Literal["weekly", "five_hour", "unknown"], "reset": NotRequired[str]})


class RequestedMove(MoveCommon, RequestedFields):
    kind: Literal["requested"]
    cause: NotRequired[Literal["refusal", "credits off", "by hand", "auto"]]


class ConfirmedMove(MoveCommon):
    kind: Literal["confirmed"]
    home: NotRequired[str]
    config_dir: NotRequired[str]
    worker: NotRequired[str]
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


@dataclass(frozen=True)
class UnknownFallbackOwner:
    """The routing has no recorded numbered owner."""

    @override
    def __str__(self) -> str:
        return "owner unknown"


class RoutingCommon(TypedDict):
    at: str
    tool: Literal["codex"]
    fallback_at: str
    owner: str | UnknownFallbackOwner


class RoutedToClaude(RoutingCommon):
    kind: Literal["routed to claude"]
    cause: Literal["refusal", "credits off", "auto"]
    why: str
    moved: list[str]
    claude_account: NotRequired[str]


class RoutedBack(RoutingCommon):
    kind: Literal["routed back"]
    restored: list[str]
    kept: list[str]


class RoutingNotRestored(RoutingCommon):
    kind: Literal["routing not restored"]
    outcome: Literal["partial", "failed"]
    restored: list[str]
    kept: list[str]
    failed: list[str]


RoutingEntry = RoutedToClaude | RoutedBack | RoutingNotRestored
JournalEntry = MoveEntry | RoutingEntry


def store_file() -> Path:
    return Path.home() / ".local" / "state" / "accounts" / "moves.jsonl"


def _valid(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    fields = cast(dict[str, object], value)
    kind = fields.get("kind")
    if kind in ("routed to claude", "routed back", "routing not restored"):
        if "move_id" in fields or "subject" in fields or fields.get("tool") != "codex":
            return False
        if not isinstance(fields.get("owner"), (str, UnknownFallbackOwner)):
            return False
        try:
            for key in ("at", "fallback_at"):
                stamp = fields.get(key)
                if not isinstance(stamp, str) or datetime.fromisoformat(stamp).tzinfo is None:
                    return False
        except ValueError:
            return False
        keys = ("moved",) if kind == "routed to claude" else ("restored", "kept")
        if kind == "routed to claude" and (fields.get("cause") not in ("refusal", "credits off", "auto")
                                               or not isinstance(fields.get("why"), str)):
            return False
        if "claude_account" in fields:
            target = fields["claude_account"]
            if not isinstance(target, str) or not re.fullmatch(r"claude[1-9][0-9]*", target):
                return False
        if kind == "routing not restored":
            if fields.get("outcome") not in ("partial", "failed"):
                return False
            keys += ("failed",)
        return all(isinstance(fields.get(key), list) and all(isinstance(item, str) for item in cast(list[object], fields[key])) for key in keys)
    if not all(isinstance(fields.get(key), str) and fields[key] for key in ("at", "subject", "move_id")):
        return False
    if "request_id" in fields and (not isinstance(fields["request_id"], str) or not fields["request_id"]):
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
    if "cause" in fields and (kind != "requested" or fields["cause"] not in ("refusal", "credits off", "by hand", "auto")):
        return False
    if kind in ("requested", "waited") and "reset" in fields:
        if not isinstance(fields["reset"], str) or not fields["reset"]:
            return False
    required = {"requested": ("from", "to", "why", "window"),
                "failed": ("reason",), "waited": ("window", "reset"),
                "wait ended": (), "no room": ("earliest_reset",), "room again": ("label",)}
    if kind == "confirmed":
        return (isinstance(fields.get("home"), str) or isinstance(fields.get("config_dir"), str)) and (
            "worker" not in fields or isinstance(fields["worker"], str))
    return isinstance(kind, str) and kind in required and all(isinstance(fields.get(key), str) for key in required[kind])


def _canonical_entry(entry: MoveEntry) -> MoveEntry:
    from scripts.whoami.note_file import canonical_label

    fields: dict[str, object] = dict(entry)
    for field in ("from", "to", "label"):
        value = fields.get(field)
        if isinstance(value, str):
            fields[field] = canonical_label(value)
    return cast(MoveEntry, cast(object, fields))



def append_journal_line(descriptor: int, document: str) -> None:
    role_map.append_journal_line(descriptor, document, valid_row=_valid_legacy_journal_row)


def _valid_legacy_journal_row(value: object) -> bool:
    if isinstance(value, dict):
        fields = dict(cast(dict[str, object], value))
        if fields.get("kind") == "routed to claude":
            _ = fields.setdefault("why", fields.get("cause"))
        return _valid(fields)
    return False


@dataclass(frozen=True)
class RecordedAutoEntries:
    entry_ids: frozenset[str]


@dataclass(frozen=True)
class UnreadableMoveJournal:
    """Workers entries cannot safely run without their replay evidence."""



@dataclass(frozen=True)
class ReadCurrentMoveJournal:
    """A standalone caller requests current replay evidence."""


READ_CURRENT_MOVE_JOURNAL = ReadCurrentMoveJournal()

def recorded_auto_entries() -> RecordedAutoEntries | UnreadableMoveJournal:
    """Read replay evidence under the same flock as journal appenders."""
    path = store_file()
    try:
        worker_names.recover_pending_journal(path)
        with path.open("rb") as source:
            fcntl.flock(source.fileno(), fcntl.LOCK_SH)
            lines = source.read().splitlines(keepends=True)
        rows: list[dict[str, object]] = []
        for index, line in enumerate(lines):
            try:
                raw = cast(object, json.loads(line))
                if not isinstance(raw, dict):
                    raise ValueError("expected a move journal object")
                row = cast(dict[str, object], worker_names.journal_entry(cast(dict[str, object], raw)))
                if row.get("kind") == "routed to claude":
                    _ = row.setdefault("why", row.get("cause"))
                if not _valid(row):
                    raise ValueError("invalid move journal row")
                rows.append(row)
            except (ValueError, UnicodeError):
                if index == len(lines) - 1 and not line.endswith(b"\n"):
                    print(f"account moves: skipping torn final line in {path}")
                    continue
                raise
        failed = {row.get("move_id") for row in rows if row.get("kind") == "failed"}
        ids: set[str] = set()
        for row in rows:
            why = row.get("why")
            if not isinstance(why, str) or not why.startswith("auto: "):
                continue
            if (row.get("kind") == "routed to claude"
                    or row.get("kind") == "requested" and row.get("tool") == "codex" and row.get("subject") == "workers"
                    and row.get("cause") == "auto" and row.get("move_id") not in failed):
                ids.add(why.removeprefix("auto: "))
        return RecordedAutoEntries(frozenset(ids))
    except FileNotFoundError:
        return RecordedAutoEntries(frozenset())
    except (OSError, ValueError, UnicodeError) as error:
        print(f"account moves: cannot read recorded workers moves: {error}")
        return UnreadableMoveJournal()


def append_move(entry: MoveEntry) -> None:
    """Append a complete transition while holding the shared writer lock."""
    if not _valid(entry):
        raise ValueError("invalid account move")
    entry = _canonical_entry(entry)
    if any(entry.get(field) == "default" for field in ("from", "to", "label")):
        raise ValueError("name the account by number")
    path = store_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with os.fdopen(os.open(path, os.O_RDWR | os.O_CREAT, 0o600), "r+", encoding="utf-8", errors="replace") as output:
        fcntl.flock(output.fileno(), fcntl.LOCK_EX)
        _ = worker_names.migrate_locked_journal(output, path)
        if entry["kind"] in ("confirmed", "wait ended", "room again", "failed"):
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
                                 (held.get("worker") == entry.get("worker") and held.get("run") == entry.get("run")))):
                        return
        _ = output.seek(0, os.SEEK_END)
        append_journal_line(output.fileno(), json.dumps(entry, sort_keys=True))


def read_journal() -> list[JournalEntry]:
    """Read valid transitions in order, skipping malformed or incomplete lines."""
    # Keep this module a standard-library leaf until a journal is actually read.
    from scripts.whoami.note_file import NumberedAccountLabel, parse_account_label

    def numbered(value: object, tool: str) -> str | HomeAccountUnresolved:
        parsed = parse_account_label(value) if isinstance(value, str) else None
        if isinstance(parsed, NumberedAccountLabel) and parsed.tool == tool:
            return parsed.label
        return HomeAccountUnresolved(Path.home() / f".{tool}")

    evidence = _role_evidence(store_file().with_name("roles.jsonl"))
    # Never read a present-day login to interpret a past home.
    homes = {"claude": Path.home() / ".claude",
             "codex": Path.home() / ".codex"}

    def historical(entry: MoveEntry, field: str) -> HistoricalAccountLabel:
        tool = entry["tool"]
        home = homes[tool]
        recorded_login = dict(entry).get("login", "") if field == "from" else ""
        from scripts.whoami.account import ResolvedHistoryLabel
        resolver = cast(HistoryResolver, cast(object, import_module("scripts.accounts.accounts")))
        resolved = resolver.resolve_history_label(f"~/.{tool}", entry["at"], login=recorded_login if isinstance(recorded_login, str) else "")
        if isinstance(resolved, ResolvedHistoryLabel):
            return resolved.label
        at = datetime.fromisoformat(entry["at"])
        matching = sorted((item for item in evidence if item.tool == tool and item.at <= at), key=lambda item: item.at)
        return numbered(matching[-1].label, tool) if matching else HomeAccountUnresolved(home)

    try:
        with store_file().open("r+", encoding="utf-8", errors="replace") as source:
            fcntl.flock(source.fileno(), fcntl.LOCK_EX)
            lines = worker_names.migrate_locked_journal(source, store_file())
    except FileNotFoundError:
        return []
    entries: list[JournalEntry] = []
    for index, line in enumerate(lines):
        try:
            value = cast(object, json.loads(line))
        except ValueError:
            if index == len(lines) - 1 and not line.endswith("\n"):
                print(f"account moves: skipping torn final line in {store_file()}")
            continue
        if isinstance(value, dict):
            legacy = cast(dict[str, object], value)
            if legacy.get("kind") == "routed to claude":
                _ = legacy.setdefault("why", legacy.get("cause"))
        if _valid(cast(object, value)):
            raw = cast(dict[str, object], value)
            if raw["kind"] in ("routed to claude", "routed back", "routing not restored"):
                raw["owner"] = _routing_owner(raw["owner"])
                if raw["kind"] == "routed to claude":
                    _ = raw.setdefault("why", raw["cause"])
                entries.append(cast(RoutingEntry, cast(object, raw)))
                continue
            entry = _canonical_entry(cast(MoveEntry, value))
            fields: dict[str, object] = dict(entry)
            for field in ("from", "to", "label"):
                if fields.get(field) in ("default", f"~/.{entry['tool']}", str(homes[entry["tool"]])):
                    fields[field] = historical(entry, field)
            if fields["kind"] == "requested":
                _ = fields.setdefault("cause", "refusal")
            entries.append(cast(MoveEntry, cast(object, fields)))
    return entries


def account_moves(journal: list[JournalEntry]) -> list[MoveEntry]:
    """Narrow journal entries exhaustively to account transitions."""
    entries: list[MoveEntry] = []
    for entry in journal:
        match entry["kind"]:
            case "requested" | "confirmed" | "failed" | "waited" | "wait ended" | "no room" | "room again":
                entries.append(entry)
                continue
            case "routed to claude" | "routed back" | "routing not restored":
                continue
        assert_never(entry)
    return entries


def read_moves() -> list[MoveEntry]:
    """Account transitions only; work routing never participates in a move."""
    return account_moves(read_journal())


def _routing_owner(value: object) -> str | UnknownFallbackOwner:
    from scripts.whoami.note_file import NumberedAccountLabel

    class RoutingOwnerLabels(Protocol):
        AccountsError: type[Exception]

        def numbered_account_label(self, label: str, tool: Literal["claude", "codex"]) -> NumberedAccountLabel: ...

    if isinstance(value, str) and value not in ("unknown", "default"):
        # Resolve the shared input parser lazily: accounts also consumes this journal.
        accounts = cast(RoutingOwnerLabels, cast(object, import_module("scripts.accounts.accounts")))
        try:
            return accounts.numbered_account_label(value, "codex").label
        except accounts.AccountsError:
            pass
    return UnknownFallbackOwner()


def append_routing(entry: RoutingEntry) -> None:
    """Append a routing outcome once, under the account journal's writer lock."""
    if not _valid(entry):
        raise ValueError("invalid work routing")
    if entry["owner"] == "default":
        raise ValueError("name the account by number")
    owner = _routing_owner(entry["owner"])
    saved: dict[str, object] = dict(entry)
    saved["owner"] = owner if isinstance(owner, str) else "unknown"
    path = store_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with os.fdopen(os.open(path, os.O_RDWR | os.O_CREAT, 0o600), "r+", encoding="utf-8", errors="replace") as output:
        fcntl.flock(output.fileno(), fcntl.LOCK_EX)
        _ = worker_names.migrate_locked_journal(output, path)
        _ = output.seek(0)
        for line in output:
            try:
                previous = cast(object, json.loads(line))
            except ValueError:
                continue
            if _valid(previous):
                held = cast(dict[str, object], previous)
                if held.get("kind") == entry["kind"] and held.get("fallback_at") == entry["fallback_at"]:
                    return
        _ = output.seek(0, os.SEEK_END)
        append_journal_line(output.fileno(), json.dumps(saved, sort_keys=True))


@dataclass(frozen=True)
class HomeAccountEvidence:
    at: datetime
    tool: Literal["claude", "codex"]
    label: HistoricalAccountLabel


def _role_evidence(path: Path) -> list[HomeAccountEvidence]:
    try:
        lines = worker_names.role_history_lines(path)
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
            if role not in ("unit", "workers") or not isinstance(at, str):
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
