"""Read context events and tracked settings without writing state."""
from __future__ import annotations

from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
from typing import Literal, cast

from scripts.lib import conf
from scripts.usage import collect
from scripts.usage.rollouts import parse_timestamp
from scripts.usage.usage_report import open_read_store


@dataclass(frozen=True)
class Request:
    at: datetime
    model: str
    cli_version: str | None
    input: int
    cache_read: int
    cache_write_5m: int
    cache_write_1h: int
    output: int

    @property
    def context(self) -> int:
        return self.input + self.cache_read + self.cache_write_5m + self.cache_write_1h


@dataclass(frozen=True)
class CompactionEvent:
    at: datetime
    trigger: str | None
    pre_tokens: int | None
    post_tokens: int | None
    kind: Literal["precomputed", "normal", "unknown"]


@dataclass(frozen=True)
class Start:
    at: datetime
    source: Literal["startup", "resume"]
    cli_version: str | None


class SessionBeginningUnknown(Enum):
    """No transcript timestamp identifies the session's beginning."""

    UNRECORDED = "unrecorded"


@dataclass(frozen=True)
class Session:
    machine: str
    family: Literal["claude", "codex"]
    session_id: str
    agent_id: str
    requests: list[Request]
    compactions: list[CompactionEvent]
    starts: list[Start]
    first_record_at: datetime | SessionBeginningUnknown = SessionBeginningUnknown.UNRECORDED


@dataclass(frozen=True)
class SettingsSet:
    at: datetime
    commit: str
    values: dict[str, object]


@dataclass(frozen=True)
class SettingsUnknown:
    """The timeline has no settings set at or before this start."""


@dataclass
class _SessionRecords:
    requests: dict[tuple[str, str], Request] = field(default_factory=dict)
    compactions: list[CompactionEvent] = field(default_factory=list)
    starts: dict[str, Start] = field(default_factory=dict)
    first_record_at: datetime | SessionBeginningUnknown = SessionBeginningUnknown.UNRECORDED


def _object(value: object) -> dict[str, object]:
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _version(record: dict[str, object]) -> str | None:
    value = record.get("version")
    return value if isinstance(value, str) else None


def _count(value: object) -> int:
    return value if type(value) is int else 0


def _utc(at: datetime) -> datetime:
    return at.replace(tzinfo=timezone.utc) if at.tzinfo is None else at.astimezone(timezone.utc)


def _request(record: dict[str, object], at: datetime) -> Request:
    message = _object(record.get("message"))
    usage = _object(message.get("usage"))
    creation = _object(usage.get("cache_creation"))
    one_hour = _count(creation.get("ephemeral_1h_input_tokens"))
    five_minutes = _count(creation.get("ephemeral_5m_input_tokens",
        _count(usage.get("cache_creation_input_tokens", one_hour)) - one_hour))
    return Request(at, _text(message.get("model")), _version(record),
        _count(usage.get("input_tokens")), _count(usage.get("cache_read_input_tokens")),
        five_minutes, one_hour, _count(usage.get("output_tokens")))


def _compaction(record: dict[str, object], at: datetime) -> CompactionEvent:
    metadata = record.get("compactMetadata")
    if not isinstance(metadata, dict):
        return CompactionEvent(at, None, None, None, "unknown")
    values = cast(dict[str, object], metadata)
    trigger, pre, post = values.get("trigger"), values.get("preTokens"), values.get("postTokens")
    return CompactionEvent(at, trigger if isinstance(trigger, str) else None,
        pre if type(pre) is int else None, post if type(post) is int else None,
        "precomputed" if values.get("precomputed") is True else "normal")


def _read_transcript(path: Path, since: datetime, until: datetime, history: _SessionRecords) -> None:
    with path.open("rb") as source:
        for number, line in enumerate(source):
            try:
                record = _object(cast(object, json.loads(line)))
            except (ValueError, UnicodeError):
                continue
            at = parse_timestamp(record.get("timestamp"))
            if at is None:
                continue
            if isinstance(history.first_record_at, SessionBeginningUnknown) or at < history.first_record_at:
                history.first_record_at = at
            kind = record.get("type")
            if kind == "attachment":
                attachment = _object(record.get("attachment"))
                name = attachment.get("hookName")
                if (attachment.get("type") == "hook_success" and attachment.get("hookEvent") == "SessionStart"
                        and name in ("SessionStart:startup", "SessionStart:resume")):
                    source_name = "startup" if name == "SessionStart:startup" else "resume"
                    hook = str(attachment.get("toolUseID", record.get("toolUseID", (path, number))))
                    history.starts[hook] = history.starts.get(hook, Start(at, source_name, _version(record)))
            if not since <= at < until:
                continue
            message = _object(record.get("message"))
            if kind == "assistant" and isinstance(message.get("usage"), dict):
                key = (_text(message.get("id")), _text(record.get("requestId")))
                history.requests[key] = _request(record, at)
            elif kind == "system" and record.get("subtype") == "compact_boundary":
                history.compactions.append(_compaction(record, at))


def read_claude(projects_root: Path, since: datetime, until: datetime, machine: str) -> list[Session]:
    """Read main sessions and subagents whose files can contain events in the window."""
    since, until = _utc(since), _utc(until)
    histories: dict[tuple[str, str], _SessionRecords] = {}
    paths = sorted({*projects_root.glob("*/*.jsonl"), *projects_root.glob("*/*/subagents/*.jsonl")})
    for path in paths:
        if path.stat().st_mtime < since.timestamp():
            continue
        session_id = path.parent.parent.name if path.parent.name == "subagents" else path.stem
        agent_id = path.stem.removeprefix("agent-") if path.parent.name == "subagents" else session_id
        history = histories.setdefault((session_id, agent_id), _SessionRecords())
        _read_transcript(path, since, until, history)
    sessions: list[Session] = []
    for (session_id, agent_id), history in histories.items():
        starts = sorted((start for start in history.starts.values() if since <= start.at < until),
                        key=lambda start: start.at)
        if history.requests or history.compactions or starts:
            sessions.append(Session(machine, "claude", session_id, agent_id,
                sorted(history.requests.values(), key=lambda request: request.at),
                sorted(history.compactions, key=lambda event: event.at), starts, history.first_record_at))
    return sessions


def configured_checkout() -> Path:
    """Resolve the command's checkout without overriding an explicit reader argument."""
    return Path(os.environ.get("CONTEXT_ECONOMY_CHECKOUT") or Path.home() / ".claude")


class _TrackedFileMissing(Enum):
    ABSENT = "tracked file absent at this commit"


def _git_blob(checkout: Path, commit: str, path: str) -> bytes | _TrackedFileMissing:
    result = subprocess.run(["git", "-C", str(checkout), "show", f"{commit}:{path}"],
                            capture_output=True, check=False)
    return result.stdout if result.returncode == 0 else _TrackedFileMissing.ABSENT


def _settings_at(checkout: Path, commit: str, at: datetime,
                 keys: tuple[str, ...], files: tuple[str, ...]) -> SettingsSet:
    content = _git_blob(checkout, commit, "settings.json")
    settings = _object(cast(object, json.loads(content))) if isinstance(content, bytes) else {}
    values: dict[str, object] = {key: settings.get(key) for key in keys}
    for path in files:
        content = _git_blob(checkout, commit, path)
        values[path] = ({"bytes": len(content), "sha": hashlib.sha256(content).hexdigest()[:12]}
                        if isinstance(content, bytes) else None)
    return SettingsSet(at, commit, values)


def settings_timeline(checkout: Path, since: datetime, until: datetime) -> list[SettingsSet]:
    """Include the settings in force at the window's start and each tracked change."""
    settings = conf.read("context_economy")
    keys, files = settings.words("settings_keys"), settings.words("files")
    result = subprocess.run(["git", "-C", str(checkout), "log", "--first-parent", "--format=%H%x00%cI",
                             "main", "--", "settings.json", *files],
                            capture_output=True, text=True, check=True)
    since, until = _utc(since), _utc(until)
    changed: list[SettingsSet] = []
    for line in reversed(result.stdout.splitlines()):
        commit, stamp = line.split("\0", 1)
        at = _utc(datetime.fromisoformat(stamp))
        if at >= until:
            continue
        current = _settings_at(checkout, commit, at, keys, files)
        if not changed or current.values != changed[-1].values:
            changed.append(current)
    baseline = [entry for entry in changed if entry.at <= since]
    return [*(baseline[-1:] if baseline else []), *(entry for entry in changed if entry.at > since)]


def settings_for(session_start: datetime, timeline: Sequence[SettingsSet]) -> SettingsSet | SettingsUnknown:
    """Select the latest settings; later timeline entries win equal times."""
    eligible = [entry for entry in timeline if entry.at <= _utc(session_start)]
    return max(reversed(eligible), key=lambda entry: entry.at) if eligible else SettingsUnknown()


def label(current: SettingsSet, previous: SettingsSet | SettingsUnknown) -> str:
    """Name each tracked value that changed from the preceding settings set."""
    before = previous.values if isinstance(previous, SettingsSet) else {}
    return ", ".join(f"{key}={json.dumps(value, ensure_ascii=False, separators=(',', ':'))}"
                     for key, value in current.values.items() if key not in before or before[key] != value)


def role_of(store: Path, machine: str, family: str, session_id: str, agent_id: str) -> str:
    """Read the latest matching ledger role without creating or changing the store."""
    if not store.is_file():
        return "unknown"
    if family == "claude":
        identity = (collect.ClaudeSession(machine, agent_id) if agent_id == session_id
                    else collect.ClaudeSubagent(machine, session_id, agent_id))
    else:
        identity = collect.CodexThread(machine, agent_id)
    parent_id = collect.identity_columns(identity)[3]
    with closing(open_read_store(store)) as database:
        row = cast(sqlite3.Row | None, database.execute(
            "SELECT role FROM turns WHERE machine = ? AND family = ? AND parent_id = ? AND agent_id = ? "
            + "ORDER BY at DESC, rowid DESC LIMIT 1", (machine, family, parent_id, agent_id)).fetchone())
        if row is None:
            return "unknown"
        value = cast(object, row["role"])
        return value if isinstance(value, str) and value else "unknown"
