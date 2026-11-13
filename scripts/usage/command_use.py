#!/usr/bin/env python3
"""Report command use from durable natedev events."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from typing import cast

from scripts.lib import conf
from scripts.lib.transcripts import open_transcript, transcript_paths
from scripts.usage.context_economy_cache import (
    FileProgress, ParseCheckpoint, SummaryUnavailable, TranscriptCache, TranscriptReadSettings,
    logical_path, read_settings,
)
from scripts.usage.rollouts import complete_lines, parse_timestamp


class UseActor(Enum):
    USER = "user"
    AGENT = "agent"


@dataclass(frozen=True)
class UseTimeUnknown:
    """The source gives no usable time for a command use."""


UseTime = datetime | UseTimeUnknown


@dataclass
class CommandHistory:
    """Transcript counts and observed dates for one command."""

    user_count: int = 0
    agent_count: int = 0
    first_seen: UseTime = field(default_factory=UseTimeUnknown)
    last_seen: UseTime = field(default_factory=UseTimeUnknown)

    def observe(self, actor: UseActor, at: UseTime) -> None:
        if actor is UseActor.USER:
            self.user_count += 1
        else:
            self.agent_count += 1
        if isinstance(at, datetime):
            if not isinstance(self.first_seen, datetime) or at < self.first_seen:
                self.first_seen = at
            self.update_last(at)

    def update_last(self, at: datetime) -> None:
        if not isinstance(self.last_seen, datetime) or at > self.last_seen:
            self.last_seen = at

    @property
    def used(self) -> bool:
        return bool(self.user_count or self.agent_count or isinstance(self.last_seen, datetime))


def object_fields(value: object) -> dict[str, object]:
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def use_time(value: object) -> UseTime:
    parsed = parse_timestamp(value)
    return parsed if parsed is not None else UseTimeUnknown()


def skill_time(value: object) -> UseTime:
    """Claude stores lastUsedAt as epoch milliseconds."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value / 1000, timezone.utc)
        except (ValueError, OverflowError, OSError):
            return UseTimeUnknown()
    return use_time(value)


def command_name(value: str) -> str:
    return "/" + value.strip().removeprefix("/").replace("/", ":")


def command_names(checkout: Path) -> list[str]:
    commands = checkout / "commands"
    return sorted(command_name(path.relative_to(commands).with_suffix("").as_posix())
                  for path in commands.rglob("*.md"))


def claude_homes(home: Path) -> list[Path]:
    return [home / ".claude", *sorted(path for path in home.glob(".claude*")
            if path.name.removeprefix(".claude").isdigit() and path.is_dir())]


def entry_actor(record: Mapping[str, object]) -> UseActor:
    """Explicit automation evidence overrides a user-shaped message."""
    if any(record.get(key) for key in ("isMeta", "isSidechain", "sourceToolAssistantUUID", "forwardedIntent")):
        return UseActor.AGENT
    message = object_fields(record.get("message"))
    for fields in (record, message):
        for key in ("origin", "source"):
            value = fields.get(key)
            origin = object_fields(value)
            kind = origin.get("kind", origin.get("type", "")) if isinstance(value, dict) else value
            if isinstance(kind, str) and kind and kind not in ("human", "user", "interactive", "cli"):
                return UseActor.AGENT
    for key in ("entrypoint", "sessionKind"):
        value = record.get(key)
        if isinstance(value, str) and value and value not in ("cli", "interactive"):
            return UseActor.AGENT
    return UseActor.USER


def entry_uses(record: Mapping[str, object]) -> list[tuple[str, UseActor]]:
    message = object_fields(record.get("message"))
    content = message.get("content")
    blocks = cast(list[object], content) if isinstance(content, list) else [content]
    result: list[tuple[str, UseActor]] = []
    if record.get("type") == "user":
        names: set[str] = set()
        for block in blocks:
            text = block if isinstance(block, str) else object_fields(block).get("text")
            if isinstance(text, str):
                names.update(command_name(match.group(1))
                             for match in re.finditer(r"<command-name>([^<]+)</command-name>", text))
        result.extend((name, entry_actor(record)) for name in sorted(names))
    elif record.get("type") == "assistant":
        for block in blocks:
            fields = object_fields(block)
            if fields.get("type") == "tool_use" and fields.get("name") == "Skill":
                name = object_fields(fields.get("input")).get("skill")
                if isinstance(name, str) and name.strip():
                    result.append((command_name(name), UseActor.AGENT))
    return result


def time_text(value: UseTime) -> str:
    return value.isoformat() if isinstance(value, datetime) else "unknown"


@dataclass(frozen=True)
class CommandUseEvent:
    """One transcript use keeps its identity after a trim or reparse."""

    identity: str
    command: str
    actor: UseActor
    at: UseTime
    session: str
    prompt_hash: str


@dataclass(frozen=True)
class LauncherMatchWindow:
    """Only commands submitted during this interval can match launcher evidence."""

    duration: timedelta

    @classmethod
    def read(cls) -> LauncherMatchWindow:
        return cls(timedelta(seconds=conf.read("context_economy").whole("command_use_launcher_window_seconds")))


def prompt_text(record: Mapping[str, object]) -> str:
    """Reconstruct the slash input that the prompt hook hashes."""
    content = object_fields(record.get("message")).get("content")
    blocks = cast(list[object], content) if isinstance(content, list) else [content]
    text = "\n".join(value for block in blocks
                     if isinstance(value := block if isinstance(block, str) else object_fields(block).get("text"), str))
    name = re.search(r"<command-name>([^<]+)</command-name>", text)
    args = re.search(r"<command-args>(.*?)</command-args>", text, re.DOTALL)
    return name.group(1) + (" " + args.group(1) if args and args.group(1) else "") if name else text


def use_events(path: Path, record: Mapping[str, object]) -> list[CommandUseEvent]:
    session = record.get("sessionId", logical_path(path).stem)
    session = session if isinstance(session, str) else logical_path(path).stem
    identity = record.get("uuid")
    if not isinstance(identity, str) or not identity:
        identity = str(logical_path(path)) + ":" + hashlib.sha256(
            json.dumps(dict(record), sort_keys=True).encode()).hexdigest()
    digest = hashlib.sha256(prompt_text(record).encode()).hexdigest() if record.get("type") == "user" else ""
    return [CommandUseEvent(f"{identity}:{index}:{name}", name, actor,
                            use_time(record.get("timestamp")), session, digest)
            for index, (name, actor) in enumerate(entry_uses(record))]


def parse_transcript(path: Path, checkpoint: ParseCheckpoint) -> ParseCheckpoint:
    events: list[dict[str, str]] = []
    with open_transcript(path) as stream:
        completed = stream.seek(checkpoint.offset)
        for offset, line in complete_lines(stream):
            completed = offset + len(line)
            if b"command-name" not in line and b'"Skill"' not in line:
                continue
            try:
                record = object_fields(cast(object, json.loads(line)))
            except (ValueError, UnicodeError):
                continue
            events.extend({"identity": event.identity, "command": event.command, "actor": event.actor.value,
                           "at": time_text(event.at), "session": event.session, "prompt_hash": event.prompt_hash}
                          for event in use_events(path, record))
    return ParseCheckpoint(completed, json.dumps(events))


class CommandEventStore:
    """Keep use identities independently of disposable parse checkpoints."""

    def __init__(self, cache: TranscriptCache) -> None:
        checkpoint_path = Path(cast(str, cache.database.execute("PRAGMA database_list").fetchone()[2]))
        self.database: sqlite3.Connection = sqlite3.connect(checkpoint_path.with_name("command_use_events.sqlite3"))
        self.database.row_factory = sqlite3.Row
        _ = self.database.execute("PRAGMA journal_mode=WAL")
        _ = self.database.execute("""CREATE TABLE IF NOT EXISTS command_events (
            identity TEXT PRIMARY KEY, command TEXT NOT NULL, actor TEXT NOT NULL,
            at TEXT NOT NULL, session TEXT NOT NULL, prompt_hash TEXT NOT NULL)""")
        _ = self.database.execute("""CREATE TABLE IF NOT EXISTS prompt_evidence (
            identity TEXT PRIMARY KEY, session TEXT NOT NULL, prompt_hash TEXT NOT NULL,
            at TEXT NOT NULL, source TEXT NOT NULL)""")
        if cache.database.execute("SELECT 1 FROM sqlite_master WHERE name = 'command_events'").fetchone():
            _ = self.database.execute("ATTACH DATABASE ? AS checkpoints", (str(checkpoint_path),))
            with self.database:
                for table in ("command_events", "prompt_evidence"):
                    _ = self.database.execute(f"INSERT OR IGNORE INTO {table} SELECT * FROM checkpoints.{table}")
                    _ = self.database.execute(f"DROP TABLE checkpoints.{table}")
            _ = self.database.execute("DETACH DATABASE checkpoints")

    def close(self) -> None:
        self.database.close()

    def parse(self, path: Path, checkpoint: ParseCheckpoint) -> ParseCheckpoint:
        parsed = parse_transcript(path, checkpoint)
        events = cast(list[dict[str, str]], json.loads(parsed.payload))
        with self.database:
            _ = self.database.executemany("""INSERT INTO command_events VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(identity) DO UPDATE SET actor = excluded.actor""",
                ((row["identity"], row["command"], row["actor"], row["at"], row["session"], row["prompt_hash"])
                 for row in events))
        return ParseCheckpoint(parsed.offset, '{"command_events": 2}')

    def parse_evidence(self, path: Path, checkpoint: ParseCheckpoint) -> ParseCheckpoint:
        """Retain the hook's session, input hash, source, and submission time."""
        with path.open("rb") as stream, self.database:
            completed = stream.seek(checkpoint.offset)
            for offset, line in complete_lines(stream):
                completed = offset + len(line)
                try:
                    row = object_fields(cast(object, json.loads(line)))
                except (ValueError, UnicodeError):
                    continue
                identity, session, digest, at, source = (row.get(key) for key in (
                    "event_id", "session_id", "prompt_hash", "timestamp_utc", "prompt_source"))
                if all(isinstance(value, str) and value for value in (identity, session, digest, at, source)):
                    _ = self.database.execute("INSERT OR IGNORE INTO prompt_evidence VALUES (?, ?, ?, ?, ?)",
                                              (identity, session, digest, at, source))
        return ParseCheckpoint(completed)

    def histories(self, window: LauncherMatchWindow) -> dict[str, CommandHistory]:
        events = [CommandUseEvent(cast(str, row["identity"]), cast(str, row["command"]),
                                  UseActor(cast(str, row["actor"])), use_time(cast(str, row["at"])),
                                  cast(str, row["session"]), cast(str, row["prompt_hash"]))
                  for row in cast(list[sqlite3.Row], self.database.execute("SELECT * FROM command_events").fetchall())]
        candidates: dict[tuple[str, str], list[CommandUseEvent]] = {}
        for event in events:
            candidates.setdefault((event.session, event.prompt_hash), []).append(event)
        automated: set[str] = set()
        for evidence in cast(list[sqlite3.Row], self.database.execute(
                "SELECT * FROM prompt_evidence ORDER BY at, identity").fetchall()):
            matches = candidates.get((cast(str, evidence["session"]), cast(str, evidence["prompt_hash"])), [])
            stamp = use_time(cast(str, evidence["at"]))
            if not isinstance(stamp, datetime):
                continue
            recorded_at: datetime = stamp
            dated = [event for event in matches if isinstance(event.at, datetime)
                     and abs(event.at - recorded_at) <= window.duration]
            if not dated:
                continue
            nearest = min(dated, key=lambda event: (
                abs(cast(datetime, event.at) - recorded_at),
                recorded_at - cast(datetime, event.at), event.identity))
            matches.remove(nearest)
            if nearest.actor is UseActor.USER and evidence["source"] in ("ScriptPrompt", "PeerPrompt", "HarnessNotice"):
                automated.add(nearest.identity)
        histories: dict[str, CommandHistory] = {}
        for event in events:
            actor = UseActor.AGENT if event.identity in automated else event.actor
            histories.setdefault(event.command, CommandHistory()).observe(actor, event.at)
        return histories


def read_histories(home: Path, settings: TranscriptReadSettings) -> dict[str, CommandHistory]:
    window = LauncherMatchWindow.read()
    histories: dict[str, CommandHistory] = {}
    found = [path for account in claude_homes(home) for path in transcript_paths(account / "projects")]
    with (closing(TranscriptCache(settings, database_name="command_use.sqlite3")) as cache,
          closing(CommandEventStore(cache)) as store):
        evidence = Path(os.environ.get("NOTIFY_STATE_DIR", str(home / ".local/state/notify"))) / "auto-silence-events.jsonl"
        paths = [path for path in cache.paths("claude", [home], found, float("-inf")) if path != evidence.resolve()]
        progress = FileProgress("natedev", "claude", len(paths), settings)
        for path in paths:
            # Old aggregate checkpoints have no use identities. Reparse available sources once.
            row = cast(sqlite3.Row | None, cache.database.execute(
                "SELECT payload FROM summaries WHERE path = ?", (str(path),)).fetchone())
            if row is not None and row["payload"] != '{"command_events": 2}' and (
                    path.exists() or path.with_suffix(path.suffix + ".zst").exists()):
                with cache.database:
                    _ = cache.database.execute("DELETE FROM summaries WHERE path = ?", (str(path),))
            checkpoint = cache.read(path, "claude", store.parse)
            progress.advance(skipped=isinstance(checkpoint, SummaryUnavailable))
        if evidence.exists():
            _ = cache.read(evidence, "claude", store.parse_evidence)
        histories = store.histories(window)
    for account in claude_homes(home):
        config = home / ".claude.json" if account.name == ".claude" else account / ".claude.json"
        try:
            usage = object_fields(object_fields(cast(object, json.loads(config.read_text(encoding="utf-8")))).get("skillUsage"))
        except FileNotFoundError:
            continue
        for name, raw in usage.items():
            at = skill_time(object_fields(raw).get("lastUsedAt"))
            if isinstance(at, datetime):
                histories.setdefault(command_name(name), CommandHistory()).update_last(at)
    return histories


def render_report(names: list[str], histories: Mapping[str, CommandHistory]) -> str:
    selected = {name: histories.get(name, CommandHistory()) for name in names}
    times = [at for history in selected.values() for at in (history.first_seen, history.last_seen) if isinstance(at, datetime)]
    window = f"{min(times).isoformat()} to {max(times).isoformat()}" if times else "no dated use seen"
    unused = [name for name, history in selected.items() if not history.used]
    lines = [f"Command use: natedev; data window: {window}", "", "Never used: " + (", ".join(unused) or "none"), "",
             "| command | last use | user count | agent count | first seen |",
             "| --- | --- | ---: | ---: | --- |"]
    oldest = datetime.min.replace(tzinfo=timezone.utc)
    for name, history in sorted(selected.items(), key=lambda row: (
            row[1].last_seen if isinstance(row[1].last_seen, datetime) else oldest, row[0])):
        if history.used:
            lines.append(f"| {name} | {time_text(history.last_seen)} | {history.user_count} | {history.agent_count} | {time_text(history.first_seen)} |")
    return "\n".join(lines)


def main() -> int:
    try:
        histories = read_histories(Path.home(), read_settings())
        print(render_report(command_names(Path(__file__).resolve().parents[2]), histories))
    except (OSError, ValueError) as error:
        print(f"command_use: {type(error).__name__}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
