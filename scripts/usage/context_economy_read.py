"""Read cached context events and tracked settings without changing transcripts."""
from __future__ import annotations

from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import sqlite3
import subprocess
import sys
import threading
from typing import Literal, cast

from scripts.lib import conf
from scripts.usage.context_economy_cache import (
    FileProgress, ParseCheckpoint, SummaryUnavailable, TranscriptCache,
    TranscriptReadSettings, TranscriptSettingsSource, read_settings,
)
from scripts.usage.rollouts import complete_lines as complete_lines, decode_rollout, is_compaction, parse_timestamp, timestamp_of, token_usage


_CLI_VERSION = re.compile(rb'"version"\s*:\s*"([^"\\]*)"')


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
class SizeNotReported:
    """The transcript does not report this compaction size."""


@dataclass(frozen=True)
class CompactionEvent:
    at: datetime
    trigger: str | None
    pre_tokens: int | SizeNotReported
    post_tokens: int | SizeNotReported
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


class ReportSettingsTimeline(list[SettingsSet]):
    """Keep resolved start settings for one report's timeline."""

    def __init__(self, entries: Sequence[SettingsSet]) -> None:
        super().__init__(entries)
        self.by_start: dict[datetime, SettingsSet | SettingsUnknown] = {}


@dataclass(frozen=True)
class MacSessions:
    sessions: list[Session]
    timeline: list[SettingsSet]


@dataclass(frozen=True)
class MacReadFailed:
    reason: str


MacRead = MacSessions | MacReadFailed


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
        return CompactionEvent(at, None, SizeNotReported(), SizeNotReported(), "unknown")
    values = cast(dict[str, object], metadata)
    trigger, pre, post = values.get("trigger"), values.get("preTokens"), values.get("postTokens")
    return CompactionEvent(at, trigger if isinstance(trigger, str) else None,
        pre if type(pre) is int else SizeNotReported(), post if type(post) is int else SizeNotReported(),
        "precomputed" if values.get("precomputed") is True else "normal")


def _cached_session(checkpoint: ParseCheckpoint) -> tuple[dict[str, object], Session]:
    state = _object(cast(object, json.loads(checkpoint.payload)))
    empty = Session("", "claude", "", "", [], [], [])
    session = _wire_session(_wire_object(state["session"])) if state else empty
    return state, session


def _read_transcript(path: Path, checkpoint: ParseCheckpoint) -> ParseCheckpoint:
    state, session = _cached_session(checkpoint)
    previous_version = _text(state.get("previous_version")).encode()
    number = _count(state.get("lines"))
    keys = [_wire_text(value) for value in _wire_list(state.get("start_keys", []))]
    starts = dict(zip(keys, session.starts))
    request_keys = _wire_list(state.get("request_keys", []))
    completed = checkpoint.offset
    first = session.first_record_at
    with path.open("rb", buffering=1024 * 1024) as source:
        completed = source.seek(checkpoint.offset)
        for offset, line in complete_lines(source):
            position = number
            number += 1
            completed = offset + len(line)
            version_position = line.find(b'"version"')
            version_match = _CLI_VERSION.match(line, version_position) if version_position >= 0 else None
            if version_position >= 0 and version_match is None:
                version_match = _CLI_VERSION.search(line, version_position)
            version_changed = version_match is not None and version_match[1] != previous_version
            first_timestamp = isinstance(first, SessionBeginningUnknown) and b'"timestamp"' in line
            if not (first_timestamp or version_changed
                    or b'"usage"' in line and b'"assistant"' in line
                    or b'"compact_boundary"' in line
                    or b'"SessionStart"' in line and b'"hook_success"' in line):
                continue
            try:
                record = _object(cast(object, json.loads(line)))
            except (ValueError, UnicodeError):
                continue
            at = parse_timestamp(record.get("timestamp"))
            if at is None:
                continue
            if isinstance(first, SessionBeginningUnknown):
                first = at
            version = _version(record)
            restart = bool(version and previous_version and version.encode() != previous_version)
            if version:
                previous_version = version.encode()
            kind = record.get("type")
            explicit_start = False
            if kind == "attachment":
                attachment = _object(record.get("attachment"))
                name = attachment.get("hookName")
                if (attachment.get("type") == "hook_success" and attachment.get("hookEvent") == "SessionStart"
                        and name in ("SessionStart:startup", "SessionStart:resume")):
                    source_name = "startup" if name == "SessionStart:startup" else "resume"
                    hook = str(attachment.get("toolUseID", record.get("toolUseID", (path, position))))
                    if hook not in starts:
                        starts[hook] = Start(at, source_name, version)
                        explicit_start = True
            if restart and not explicit_start:
                starts[f"version:{path}:{position}"] = Start(at, "resume", version)
            message = _object(record.get("message"))
            if (kind == "assistant" and message.get("model") != "<synthetic>"
                    and isinstance(message.get("usage"), dict)):
                request_keys.append([_text(message.get("id")), _text(record.get("requestId"))])
                session.requests.append(_request(record, at))
            elif kind == "system" and record.get("subtype") == "compact_boundary":
                session.compactions.append(_compaction(record, at))
    saved = Session("", "claude", "", "", session.requests, session.compactions, list(starts.values()), first)
    return ParseCheckpoint(completed, json.dumps({"session": _session_data(saved), "request_keys": request_keys,
        "start_keys": list(starts), "previous_version": previous_version.decode(), "lines": number}, separators=(",", ":")))


def read_claude(projects_root: Path, since: datetime, until: datetime, machine: str, *,
                settings: TranscriptReadSettings | TranscriptSettingsSource = TranscriptSettingsSource.LOCAL_CONFIG,
                ) -> list[Session]:
    """Read cached main sessions and subagents; apply the window after loading each summary."""
    since, until = _utc(since), _utc(until)
    histories: dict[tuple[str, str], _SessionRecords] = {}
    found = sorted({*projects_root.glob("*/*.jsonl"), *projects_root.glob("*/*/subagents/*.jsonl"),
                    *projects_root.glob("*/*.jsonl.zst"), *projects_root.glob("*/*/subagents/*.jsonl.zst")})
    settings = read_settings(settings)
    with closing(TranscriptCache(settings)) as cache:
        paths = cache.paths("claude", [projects_root], found, since.timestamp())
        progress = FileProgress(machine, "claude", len(paths), settings)
        for path in paths:
            checkpoint = cache.read(path, "claude", _read_transcript)
            progress.advance(skipped=isinstance(checkpoint, SummaryUnavailable))
            if isinstance(checkpoint, SummaryUnavailable):
                continue
            state, parsed = _cached_session(checkpoint)
            session_id = path.parent.parent.name if path.parent.name == "subagents" else path.stem
            agent_id = path.stem.removeprefix("agent-") if path.parent.name == "subagents" else session_id
            history = histories.setdefault((session_id, agent_id), _SessionRecords())
            if isinstance(history.first_record_at, SessionBeginningUnknown):
                history.first_record_at = parsed.first_record_at
            for key, request in zip(_wire_list(state["request_keys"]), parsed.requests):
                pair = _wire_list(key)
                if since <= request.at < until:
                    history.requests[(_wire_text(pair[0]), _wire_text(pair[1]))] = request
            history.compactions.extend(event for event in parsed.compactions if since <= event.at < until)
            history.starts.update(zip((_wire_text(key) for key in _wire_list(state["start_keys"])), parsed.starts))
    sessions: list[Session] = []
    for (session_id, agent_id), history in histories.items():
        starts = sorted((start for start in history.starts.values() if start.at < until), key=lambda start: start.at)
        earlier = [start for start in starts if start.at < since]
        in_window = [start for start in starts if start.at >= since]
        if history.requests or history.compactions or in_window:
            sessions.append(Session(machine, "claude", session_id, agent_id,
                sorted(history.requests.values(), key=lambda request: request.at),
                sorted(history.compactions, key=lambda event: event.at), [*earlier[-1:], *in_window], history.first_record_at))
    return sessions


def _read_codex_file(path: Path, checkpoint: ParseCheckpoint) -> ParseCheckpoint:
    state, session = _cached_session(checkpoint)
    identity, model = session.session_id, _text(state.get("model"))
    version = _version(state)
    first = session.first_record_at
    requests, compactions = session.requests, session.compactions
    starts = {start.at: start for start in session.starts}
    previous_total = _object(state.get("previous_total"))
    completed = checkpoint.offset
    with path.open("rb", buffering=1024 * 1024) as stream:
        completed = stream.seek(checkpoint.offset)
        for offset, line in complete_lines(stream):
            completed = offset + len(line)
            if not isinstance(first, SessionBeginningUnknown) and not any(marker in line for marker in (
                    b'"session_meta"', b'"turn_context"', b'"token_count"',
                    b'"compacted"', b'"contextCompaction"', b'\\u')):
                continue
            record = decode_rollout(line.decode("utf-8"))
            if record is None:
                continue
            payload = _object(cast(object, record.get("payload")))
            at = timestamp_of(record)
            if at is not None and isinstance(first, SessionBeginningUnknown):
                first = at
            if record.get("type") == "session_meta":
                identity = _text(payload.get("id")) or identity
                value = payload.get("cli_version")
                version = value if isinstance(value, str) else None
                opening = parse_timestamp(payload.get("timestamp")) or at
                if opening is not None and opening not in starts:
                    starts[opening] = Start(opening, "resume" if starts else "startup", version)
            elif record.get("type") == "turn_context":
                model = _text(payload.get("model"))
            usage = token_usage(record)
            repeated = False
            if usage is not None:
                total = _object(_object(payload.get("info")).get("total_token_usage"))
                repeated = bool(total) and total == previous_total
                if total:
                    previous_total = total
            if at is None:
                continue
            if usage is not None and not repeated:
                requests.append(Request(at, model, version, usage.uncached_input, usage.cached_input,
                                        0, 0, usage.output))
            if is_compaction(record):
                compactions.append(CompactionEvent(at, None, SizeNotReported(), SizeNotReported(), "unknown"))
    saved = Session("", "codex", identity, identity, requests, compactions, list(starts.values()), first)
    return ParseCheckpoint(completed, json.dumps({"session": _session_data(saved), "model": model,
        "version": version, "previous_total": previous_total}, separators=(",", ":")))


def read_codex(sessions_roots: Sequence[Path], since: datetime, until: datetime, machine: str, *,
               settings: TranscriptReadSettings | TranscriptSettingsSource = TranscriptSettingsSource.LOCAL_CONFIG,
               ) -> list[Session]:
    """Read each logical rollout once, including retained summaries without a source file."""
    since, until = _utc(since), _utc(until)
    found = sorted({path.resolve() for root in sessions_roots
                    for pattern in ("*.jsonl", "*.jsonl.zst") for path in root.rglob(pattern)})
    sessions: list[Session] = []
    settings = read_settings(settings)
    with closing(TranscriptCache(settings)) as cache:
        paths = cache.paths("codex", sessions_roots, found, since.timestamp())
        progress = FileProgress(machine, "codex", len(paths), settings)
        for path in paths:
            checkpoint = cache.read(path, "codex", _read_codex_file)
            progress.advance(skipped=isinstance(checkpoint, SummaryUnavailable))
            if isinstance(checkpoint, SummaryUnavailable):
                continue
            parsed = _cached_session(checkpoint)[1]
            requests = [request for request in parsed.requests if since <= request.at < until]
            compactions = [event for event in parsed.compactions if since <= event.at < until]
            earlier = [start for start in parsed.starts if start.at < since]
            in_window = [start for start in parsed.starts if since <= start.at < until]
            retained = [*sorted(earlier, key=lambda start: start.at)[-1:], *sorted(in_window, key=lambda start: start.at)]
            if parsed.session_id and (requests or compactions or in_window):
                sessions.append(Session(machine, "codex", parsed.session_id, parsed.agent_id,
                                        requests, compactions, retained, parsed.first_record_at))
    return sessions


def configured_checkout() -> Path:
    """Resolve the command's checkout without overriding an explicit reader argument."""
    return Path(os.environ.get("CONTEXT_ECONOMY_CHECKOUT") or Path.home() / ".claude")


def _tracked_blobs(checkout: Path, commits: Sequence[str], paths: Sequence[str]) -> dict[tuple[str, str], bytes]:
    """Resolve tracked files in two Git processes and read each unique blob once."""
    names = [(commit, path) for commit in dict.fromkeys(commits) for path in paths]
    if not names:
        return {}
    checked = subprocess.run(["git", "-C", str(checkout), "cat-file", "--batch-check"],
        input="".join(f"{commit}:{path}\n" for commit, path in names).encode(), capture_output=True, check=True)
    hashes: dict[tuple[str, str], bytes] = {}
    for name, line in zip(names, checked.stdout.splitlines()):
        fields = line.split()
        if len(fields) == 3 and fields[1] == b"blob":
            hashes[name] = fields[0]
    result = subprocess.run(["git", "-C", str(checkout), "cat-file", "--batch"],
        input=b"".join(digest + b"\n" for digest in dict.fromkeys(hashes.values())), capture_output=True, check=True)
    contents: dict[bytes, bytes] = {}
    source = io.BytesIO(result.stdout)
    for header in iter(source.readline, b""):
        fields = header.split()
        contents[fields[0]] = source.read(int(fields[2]))
        _ = source.read(1)
    return {name: contents[digest] for name, digest in hashes.items()}


def _settings_values(commit: str, blobs: dict[tuple[str, str], bytes],
                     keys: tuple[str, ...], files: tuple[str, ...]) -> dict[str, object]:
    content = blobs.get((commit, "settings.json"), b"{}")
    settings = _object(cast(object, json.loads(content)))
    values: dict[str, object] = {key: settings[key] for key in keys if key in settings}
    for path in files:
        content = blobs.get((commit, path), b"")
        values[path] = ({"bytes": len(content), "sha": hashlib.sha256(content).hexdigest()[:12]}
                        if (commit, path) in blobs else None)
    return values


def settings_timeline(checkout: Path, since: datetime, until: datetime) -> list[SettingsSet]:
    """Use HEAD reflog times for the settings in force and each tracked change."""
    settings = conf.read("context_economy")
    keys, files = settings.words("settings_keys"), settings.words("files")
    result = subprocess.run(["git", "-C", str(checkout), "log", "-g", "--format=%H%x00%gD",
                             "--date=iso-strict", "HEAD"],
                            capture_output=True, text=True, check=True)
    since, until = _utc(since), _utc(until)
    moves: list[tuple[str, datetime]] = []
    for line in reversed(result.stdout.splitlines()):
        commit, selector = line.split("\0", 1)
        at = _utc(datetime.fromisoformat(selector.partition("@{")[2].removesuffix("}")))
        if at < until:
            moves.append((commit, at))
    blobs = _tracked_blobs(checkout, [move[0] for move in moves], ("settings.json", *files))
    known: dict[str, dict[str, object]] = {}
    changed: list[SettingsSet] = []
    for commit, at in moves:
        if commit not in known:
            known[commit] = _settings_values(commit, blobs, keys, files)
        current = SettingsSet(at, commit, known[commit])
        if not changed or current.values != changed[-1].values:
            changed.append(current)
    baseline = [entry for entry in changed if entry.at <= since]
    return [*(baseline[-1:] if baseline else []), *(entry for entry in changed if entry.at > since)]


def settings_for(session_start: datetime, timeline: Sequence[SettingsSet]) -> SettingsSet | SettingsUnknown:
    """Select the latest settings; later timeline entries win equal times."""
    session_start = _utc(session_start)
    if isinstance(timeline, ReportSettingsTimeline) and session_start in timeline.by_start:
        return timeline.by_start[session_start]
    eligible = [entry for entry in timeline if entry.at <= session_start]
    resolved = max(reversed(eligible), key=lambda entry: entry.at) if eligible else SettingsUnknown()
    if isinstance(timeline, ReportSettingsTimeline):
        timeline.by_start[session_start] = resolved
    return resolved


def label(current: SettingsSet, previous: SettingsSet | SettingsUnknown) -> str:
    """Name each tracked value that changed from the preceding settings set."""
    before = previous.values if isinstance(previous, SettingsSet) else {}
    changes = [f"{key}={json.dumps(value, ensure_ascii=False, separators=(',', ':'))}"
               for key, value in current.values.items() if key not in before or before[key] != value]
    changes.extend(f"{key}=<absent>" for key in before if key not in current.values)
    return ", ".join(changes)


def role_of(store: Path, machine: str, family: str, session_id: str, agent_id: str) -> str:
    """Read the latest matching ledger role without creating or changing the store."""
    from scripts.usage import collect
    from scripts.usage.usage_report import open_read_store

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


def _session_data(session: Session) -> dict[str, object]:
    return {
        "type": "session", "machine": session.machine, "family": session.family,
        "session_id": session.session_id, "agent_id": session.agent_id,
        "first_record_at": session.first_record_at.isoformat() if isinstance(session.first_record_at, datetime) else None,
        "requests": [{"at": request.at.isoformat(), "model": request.model, "cli_version": request.cli_version,
                      "input": request.input, "cache_read": request.cache_read,
                      "cache_write_5m": request.cache_write_5m, "cache_write_1h": request.cache_write_1h,
                      "output": request.output} for request in session.requests],
        "starts": [{"at": start.at.isoformat(), "source": start.source, "cli_version": start.cli_version}
                   for start in session.starts],
        "compactions": [{"at": event.at.isoformat(), "trigger": event.trigger, "kind": event.kind,
                         "pre_tokens": event.pre_tokens if isinstance(event.pre_tokens, int) else None,
                         "post_tokens": event.post_tokens if isinstance(event.post_tokens, int) else None}
                        for event in session.compactions],
    }


def _wire_object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("Mac data must contain an object")
    return cast(dict[str, object], value)


def _wire_list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise ValueError("Mac data must contain a list")
    return cast(list[object], value)


def _wire_text(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Mac data must contain text")
    return value


def _wire_optional_text(value: object) -> str | None:
    return None if value is None else _wire_text(value)


def _wire_count(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("Mac data must contain a whole number")
    return value


def _wire_time(value: object) -> datetime:
    at = parse_timestamp(_wire_text(value))
    if at is None:
        raise ValueError("Mac data has an invalid timestamp")
    return at


def _wire_request(value: object) -> Request:
    row = _wire_object(value)
    return Request(_wire_time(row["at"]), _wire_text(row["model"]), _wire_optional_text(row["cli_version"]),
                   *(_wire_count(row[name]) for name in
                     ("input", "cache_read", "cache_write_5m", "cache_write_1h", "output")))


def _wire_compaction(value: object) -> CompactionEvent:
    row = _wire_object(value)
    kind = row["kind"]
    if kind not in ("precomputed", "normal", "unknown"):
        raise ValueError("Mac data has an invalid compaction kind")
    pre, post = row["pre_tokens"], row["post_tokens"]
    return CompactionEvent(_wire_time(row["at"]), _wire_optional_text(row["trigger"]),
        SizeNotReported() if pre is None else _wire_count(pre),
        SizeNotReported() if post is None else _wire_count(post), kind)


def _wire_start(value: object) -> Start:
    row = _wire_object(value)
    source = row["source"]
    if source not in ("startup", "resume"):
        raise ValueError("Mac data has an invalid start source")
    return Start(_wire_time(row["at"]), source,
                 _wire_optional_text(row["cli_version"]))


def _wire_session(row: dict[str, object]) -> Session:
    family = row["family"]
    if family not in ("claude", "codex"):
        raise ValueError("Mac data has an invalid session family")
    first = row["first_record_at"]
    return Session(_wire_text(row["machine"]), family,
        _wire_text(row["session_id"]), _wire_text(row["agent_id"]),
        [_wire_request(value) for value in _wire_list(row["requests"])],
        [_wire_compaction(value) for value in _wire_list(row["compactions"])],
        [_wire_start(value) for value in _wire_list(row["starts"])],
        SessionBeginningUnknown.UNRECORDED if first is None else _wire_time(first))


def _mac_reply(output: str) -> MacSessions:
    sessions: list[Session] = []
    timeline: list[SettingsSet] = []
    timeline_read = False
    for line in output.splitlines():
        row = _wire_object(cast(object, json.loads(line)))
        if row["type"] == "timeline" and not timeline_read:
            for value in _wire_list(row["entries"]):
                entry = _wire_object(value)
                timeline.append(SettingsSet(_wire_time(entry["at"]), _wire_text(entry["commit"]),
                                            _wire_object(entry["values"])))
            timeline_read = True
        elif row["type"] == "session" and timeline_read:
            sessions.append(_wire_session(row))
        else:
            raise ValueError("Mac data has an invalid record type or order")
    if not timeline_read:
        raise ValueError("Mac data has no timeline")
    return MacSessions(sessions, timeline)


def timeline_start(sessions: Sequence[Session], since: datetime) -> datetime:
    """Include the settings anchor that each session needs before the report window."""
    anchors = [since]
    for session in sessions:
        earlier = [start.at for start in session.starts if start.at < since]
        if earlier:
            anchors.append(max(earlier))
        elif isinstance(session.first_record_at, datetime):
            anchors.append(session.first_record_at)
    return min(anchors)


def emit_mac(request: dict[str, object]) -> None:
    """Read both families and the checkout timeline in the remote process."""
    since, until = _wire_time(request["since"]), _wire_time(request["until"])
    projects = _wire_text(request["projects_root"])
    checkout = _wire_text(request["checkout"])
    roots = [Path(_wire_text(value)) for value in _wire_list(request["codex_roots"])]
    settings = TranscriptReadSettings(_wire_count(request["cache_max_megabytes"]),
                                      _wire_count(request["progress_seconds"]))
    sessions = read_claude(Path(projects) if projects else Path.home() / ".claude/projects", since, until, "mac",
                           settings=settings)
    sessions.extend(read_codex(roots or sorted(Path.home().glob(".codex*/sessions")), since, until, "mac",
                              settings=settings))
    timeline = settings_timeline(Path(checkout) if checkout else configured_checkout(), timeline_start(sessions, since), until)
    print(json.dumps({"type": "timeline", "entries": [
        {"at": entry.at.isoformat(), "commit": entry.commit, "values": entry.values} for entry in timeline]}), flush=True)
    for session in sessions:
        print(json.dumps(_session_data(session)), flush=True)


def _mac_payload(since: datetime, until: datetime) -> str:
    settings = read_settings()
    root = Path(__file__).resolve().parents[2]
    modules = {name: (root / path).read_text(encoding="utf-8") for name, path in (
        ("scripts.lib.conf_reads", "scripts/lib/conf_reads.py"),
        ("scripts.lib.conf", "scripts/lib/conf.py"),
        ("scripts.usage.rollouts", "scripts/usage/rollouts.py"),
        ("scripts.usage.context_economy_cache", "scripts/usage/context_economy_cache.py"),
        ("scripts.usage.context_economy_read", "scripts/usage/context_economy_read.py"),
    )}
    return json.dumps({"modules": modules, "since": since.isoformat(), "until": until.isoformat(),
        "cache_max_megabytes": settings.cache_max_megabytes, "progress_seconds": settings.progress_seconds,
        "projects_root": os.environ.get("CONTEXT_ECONOMY_MAC_PROJECTS_ROOT", ""),
        "codex_roots": _wire_list(cast(object, json.loads(os.environ.get("CONTEXT_ECONOMY_MAC_CODEX_ROOTS", "[]")))),
        "checkout": os.environ.get("CONTEXT_ECONOMY_MAC_CHECKOUT", "")})


def _mac_command() -> list[str]:
    from scripts.usage.collect import MAC_HOST, bootstrap_program

    program = bootstrap_program("sys.modules['scripts.usage.context_economy_read'].emit_mac(request)")
    configured = os.environ.get("CONTEXT_ECONOMY_MAC_COMMAND", "")
    if configured:
        return [*(_wire_text(value) for value in _wire_list(cast(object, json.loads(configured)))), program]

    return ["ssh", "-o", "BatchMode=yes", os.environ.get("CONTEXT_ECONOMY_MAC_HOST", MAC_HOST),
            shlex.join(("python3", "-c", program))]


def _run_mac(command: list[str], payload: str) -> subprocess.CompletedProcess[str]:
    """Forward remote progress while collecting only the result document."""
    read_descriptor, write_descriptor = os.pipe()
    try:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=write_descriptor, text=True, encoding="utf-8")
    except BaseException:
        os.close(read_descriptor)
        raise
    finally:
        os.close(write_descriptor)
    errors: list[str] = []

    def relay() -> None:
        with os.fdopen(read_descriptor, encoding="utf-8", errors="replace") as stream:
            for line in stream:
                errors.append(line)
                print(line, end="", file=sys.stderr, flush=True)

    forwarding = threading.Thread(target=relay)
    forwarding.start()
    try:
        with process:
            try:
                output = process.communicate(payload,
                    timeout=conf.read("context_economy").whole("mac_timeout_seconds"))[0]
            except subprocess.TimeoutExpired:
                process.kill()
                output = process.communicate()[0]
                raise
    finally:
        forwarding.join()
    return subprocess.CompletedProcess(command, process.returncode, output, "".join(errors))


def read_mac(since: datetime, until: datetime) -> MacRead:
    """Use one remote process and return a failure without discarding local sessions."""
    try:
        result = _run_mac(_mac_command(), _mac_payload(since, until))
        if result.returncode:
            return MacReadFailed(result.stderr.strip() or f"remote reader exited {result.returncode}")
        return _mac_reply(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError, KeyError) as error:
        return MacReadFailed(str(error))
