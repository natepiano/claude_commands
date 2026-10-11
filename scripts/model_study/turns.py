"""Read director transcripts without carrying their contents into measurements."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

OVER_LIMIT_SECONDS = 1800
PDT = ZoneInfo("America/Los_Angeles")
# Newest first; transcripts from before the rename hold the old name.
DIRECTOR_COMMANDS = ("/unit:direct", "/unit:delegate")

type Record = dict[str, object]


def object_field(value: object, key: str) -> Record:
    if isinstance(value, dict):
        found = cast(Record, value).get(key)
        if isinstance(found, dict):
            return cast(Record, found)
    return {}


def string_field(value: Record, key: str, default: str = "") -> str:
    found = value.get(key)
    return found if isinstance(found, str) else default


def number_field(value: Record, key: str) -> int:
    found = value.get(key)
    return int(found) if isinstance(found, (int, float)) and not isinstance(found, bool) else 0


def timestamp(value: Record) -> datetime | None:
    raw = value.get("timestamp")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass(frozen=True)
class RosterEntry:
    name: str
    session_ids: list[str]
    switched_by_pdt: str | None
    production: str
    director_from_pdt: str | None


@dataclass(frozen=True)
class Turn:
    session: str
    name: str
    request_id: str
    started: str
    ended: str
    seconds: float | None
    model: str
    effort: str
    speed: str
    stop: str
    kind: str
    input: int
    output: int
    thinking: int
    cache_read: int
    write_5m: int
    write_1h: int
    context: int
    switch_turn: bool
    after_compact: int

    def to_json(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class Compaction:
    session: str
    name: str
    at: str
    trigger: str
    pre_tokens: int
    post_tokens: int
    duration_ms: int | None
    model: str

    def to_json(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class Dropped:
    before_director: int = 0
    sidechain: int = 0
    no_trigger: int = 0
    negative: int = 0
    over_limit: int = 0
    synthetic: int = 0

    def to_json(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class SessionRead:
    turns: list[Turn]
    compactions: list[Compaction]
    dropped: Dropped
    director_from: str | None
    whole_session: bool


def director_turns(reads: list[SessionRead]) -> list[Turn]:
    return recompute_switch_turns([turn for read in reads for turn in read.turns])


def recompute_switch_turns(rows: list[Turn]) -> list[Turn]:
    turns = sorted(rows, key=lambda turn: turn.started)
    previous_model: str | None = None
    combined: list[Turn] = []
    for turn in turns:
        combined.append(replace(turn, switch_turn=turn.model != previous_model))
        previous_model = turn.model
    return combined


def load_roster(path: Path) -> list[RosterEntry]:
    rows = cast(list[Record], json.loads(path.read_text()))
    return [RosterEntry(
        name=cast(str, row["name"]),
        session_ids=cast(list[str], row["session_ids"]),
        switched_by_pdt=cast(str | None, row["switched_by_pdt"]),
        production=cast(str, row["production"]),
        director_from_pdt=cast(str | None, row["director_from_pdt"]),
    ) for row in rows]


def registered_ids(name: str, registry_dir: Path) -> list[str]:
    ids: set[str] = set()
    for path in registry_dir.glob("*.json"):
        try:
            row = cast(Record, json.loads(path.read_text()))
        except (OSError, ValueError):
            continue
        if row.get("name") == name and isinstance(row.get("sessionId"), str):
            ids.add(cast(str, row["sessionId"]))
    return sorted(ids)


def session_ids(entry: RosterEntry, registry_dir: Path) -> list[str]:
    return list(dict.fromkeys([*entry.session_ids, *registered_ids(entry.name, registry_dir)]))


def user_content(record: Record) -> object:
    return object_field(record, "message").get("content")


def director_boundary(records: list[Record], director_from: str | None, first_request: datetime) -> tuple[datetime, str | None, bool]:
    if director_from is not None:
        at = datetime.fromisoformat(director_from).replace(tzinfo=PDT)
        return at, at.isoformat(), False
    for record in records:
        content = user_content(record)
        if record.get("type") == "user" and isinstance(content, str) and any(f"<command-name>{name}</command-name>" in content for name in DIRECTOR_COMMANDS):
            at = timestamp(record)
            if at is not None:
                return at, at.isoformat(), False
    for record in records:
        content = user_content(record)
        if record.get("type") == "user" and record.get("turnOrigin") in ("human", "peer") and isinstance(content, str) and any(name in content for name in DIRECTOR_COMMANDS):
            at = timestamp(record)
            if at is not None:
                return at, at.isoformat(), False
    return first_request, None, True


def prompt_kind(record: Record | None) -> str:
    if record is None:
        return "other"
    if record.get("isCompactSummary") is True:
        return "compact"
    content = user_content(record)
    if isinstance(content, list) and any(isinstance(item, dict) and cast(Record, item).get("type") == "tool_result" for item in cast(list[object], content)):
        return "continuation"
    if isinstance(content, str):
        origin = record.get("turnOrigin")
        if origin in ("human", "peer", "task_notification"):
            return cast(str, origin)
    return "other"


def read_session(path: Path, name: str, director_from: str | None) -> SessionRead:
    records: list[Record] = []
    with path.open() as source:
        for line in source:
            try:
                value = cast(object, json.loads(line))
            except ValueError:
                continue
            if isinstance(value, dict):
                records.append(cast(Record, value))

    groups: dict[str, list[tuple[int, Record]]] = {}
    sidechain = 0
    for index, record in enumerate(records):
        if record.get("type") != "assistant":
            continue
        if record.get("isSidechain") is True:
            sidechain += 1
            continue
        request_id = string_field(record, "requestId") or string_field(record, "uuid")
        if request_id:
            groups.setdefault(request_id, []).append((index, record))
    if not groups:
        return SessionRead([], [], Dropped(sidechain=sidechain), None, True)

    first = next(iter(groups.values()))[0][1]
    first_at = timestamp(first)
    if first_at is None:
        return SessionRead([], [], Dropped(sidechain=sidechain), None, True)
    boundary, director_at, whole_session = director_boundary(records, director_from, first_at)
    session = path.stem
    events: list[tuple[int, str, str]] = []
    for request_id, blocks in groups.items():
        events.append((blocks[0][0], "request", request_id))
    for index, record in enumerate(records):
        if record.get("type") == "system" and record.get("subtype") == "compact_boundary":
            events.append((index, "compaction", ""))
    events.sort()

    turns: list[Turn] = []
    compactions: list[Compaction] = []
    before_director = no_trigger = negative = over_limit = synthetic = 0
    model_before: str | None = None
    compact_count: int | None = None
    for index, event, request_id in events:
        if event == "compaction":
            record = records[index]
            at = timestamp(record)
            if at is None or at < boundary:
                continue
            metadata = object_field(record, "compactMetadata")
            duration = metadata.get("durationMs")
            compactions.append(Compaction(
                session, name, at.isoformat(), string_field(metadata, "trigger"),
                number_field(metadata, "preTokens"), number_field(metadata, "postTokens"),
                int(duration) if isinstance(duration, (int, float)) and not isinstance(duration, bool) else None,
                model_before or "unknown",
            ))
            compact_count = 0
            continue
        blocks = groups[request_id]
        start = timestamp(blocks[0][1])
        end = timestamp(blocks[-1][1])
        if start is None or end is None:
            continue
        if start < boundary:
            before_director += 1
            continue
        input_tokens = output = thinking = cache_read = write_5m = write_1h = 0
        model = effort = speed = "unknown"
        stop = "other"
        for _, block in blocks:
            message = object_field(block, "message")
            usage = object_field(message, "usage")
            cache = object_field(usage, "cache_creation")
            details = object_field(usage, "output_tokens_details")
            model = string_field(message, "model", model)
            effort = string_field(block, "perTurnEffort", effort)
            speed = string_field(usage, "speed", speed)
            reason = string_field(message, "stop_reason")
            stop = reason if reason in ("tool_use", "end_turn") else "other"
            input_tokens = max(input_tokens, number_field(usage, "input_tokens"))
            output = max(output, number_field(usage, "output_tokens"))
            thinking = max(thinking, number_field(details, "thinking_tokens"))
            cache_read = max(cache_read, number_field(usage, "cache_read_input_tokens"))
            write_1h = max(write_1h, number_field(cache, "ephemeral_1h_input_tokens"))
            write_5m = max(write_5m, number_field(cache, "ephemeral_5m_input_tokens"))
        if model == "<synthetic>":
            synthetic += 1
            continue
        trigger: Record | None = None
        previous_user: Record | None = None
        for previous_index in range(index - 1, -1, -1):
            preceding = records[previous_index]
            if previous_user is None and preceding.get("type") == "user":
                previous_user = preceding
            at = timestamp(preceding)
            if trigger is None and preceding.get("type") != "assistant" and at is not None and at <= start:
                trigger = preceding
            if trigger is not None and previous_user is not None:
                break
        seconds: float | None = None
        if trigger is None:
            no_trigger += 1
        else:
            trigger_at = timestamp(trigger)
            if trigger_at is not None:
                gap = (end - trigger_at).total_seconds()
                if gap < 0:
                    negative += 1
                elif gap > OVER_LIMIT_SECONDS:
                    over_limit += 1
                else:
                    seconds = gap
        write_5m += max(0, max(number_field(object_field(object_field(block, "message"), "usage"), "cache_creation_input_tokens") for _, block in blocks) - write_5m - write_1h)
        if compact_count is not None:
            compact_count += 1
        after_compact = compact_count if compact_count is not None and compact_count <= 10 else 0
        turns.append(Turn(
            session, name, request_id, start.isoformat(), end.isoformat(), seconds,
            model, effort, speed, stop, prompt_kind(previous_user), input_tokens,
            output, thinking, cache_read, write_5m, write_1h,
            input_tokens + cache_read + write_5m + write_1h,
            model_before != model, after_compact,
        ))
        model_before = model
    return SessionRead(turns, compactions, Dropped(before_director, sidechain, no_trigger, negative, over_limit, synthetic), director_at, whole_session)
