"""Collect example builds and app starts from Claude Code launch results.

Each launch has its own atomic JSON line file. Its session and timestamp name
make replay after an interrupted state update safe without scanning old logs.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import cast

import record
import store

STATE_NAME = "launches_state.json"
LOCK_NAME = "launches.lock"
MARK_BYTES = 256


class LaunchOutcome(StrEnum):
    RECORDED = "recorded"
    NOT_A_LAUNCH = "not_a_launch"
    NO_RESULT = "no_result"
    NO_LOCATION = "no_location"


@dataclass(frozen=True)
class RecordedLaunch:
    step: dict[str, object]

    @property
    def outcome(self) -> LaunchOutcome:
        return LaunchOutcome.RECORDED


@dataclass(frozen=True)
class LaunchCounts:
    recorded: int
    not_a_launch: int
    no_result: int
    no_location: int
    unresolved: int
    added: int

    def summary(self) -> str:
        found = self.recorded + self.no_location
        return (
            f"{found} launches found: {self.recorded} recorded "
            f"({self.added} new, {self.unresolved} with an unknown worktree), "
            f"{self.no_location} without a location; skipped {self.no_result} results "
            f"without launch facts and {self.not_a_launch} other tool results"
        )


def transcripts_root() -> Path:
    override = os.environ.get("BUILDLOG_TRANSCRIPTS")
    return Path(override) if override else Path.home() / ".claude/projects"


def result_from_line(line: dict[str, object]) -> dict[str, object] | None:
    meta = line.get("mcpMeta")
    if isinstance(meta, dict):
        structured = cast(dict[str, object], meta).get("structuredContent")
        if isinstance(structured, dict):
            return cast(dict[str, object], structured)
    message = line.get("message")
    content: object = None
    if isinstance(message, dict):
        content = cast(dict[str, object], message).get("content")
        if isinstance(content, list):
            for item in cast(list[object], content):
                if not isinstance(item, dict):
                    continue
                entry = cast(dict[str, object], item)
                if entry.get("type") != "tool_result":
                    continue
                body = entry.get("content")
                if isinstance(body, str):
                    try:
                        parsed = cast(object, json.loads(body))
                    except ValueError:
                        continue
                    if isinstance(parsed, dict):
                        return cast(dict[str, object], parsed)
    for notification in (cast(object, content), line.get("content")):
        if not isinstance(notification, str) or "<task-notification>" not in notification:
            continue
        _, start, remainder = notification.partition("<result>")
        if not start:
            continue
        body, end, _ = remainder.partition("</result>")
        if not end:
            continue
        try:
            parsed = cast(object, json.loads(body))
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return cast(dict[str, object], parsed)
    return None


def launch_record(
    line: dict[str, object], transcript: Path, *, backfill: bool = False
) -> RecordedLaunch | LaunchOutcome:
    result = result_from_line(line)
    if result is None:
        return LaunchOutcome.NO_RESULT
    call_info = result.get("call_info")
    metadata = result.get("metadata")
    parameters = result.get("parameters")
    if not isinstance(call_info, dict):
        return LaunchOutcome.NOT_A_LAUNCH
    call_info = cast(dict[str, object], call_info)
    if call_info.get("mcp_tool") != "brp_launch":
        return LaunchOutcome.NOT_A_LAUNCH
    if not isinstance(metadata, dict) or not isinstance(parameters, dict):
        return LaunchOutcome.NO_RESULT
    metadata = cast(dict[str, object], metadata)
    parameters = cast(dict[str, object], parameters)
    timestamp = metadata.get("launch_timestamp")
    duration_ms = metadata.get("launch_duration_ms")
    target = metadata.get("target_name") or parameters.get("target_name")
    directory = metadata.get("working_directory")
    path = parameters.get("path")
    if not isinstance(timestamp, str) or not isinstance(duration_ms, int | float) or duration_ms < 0:
        return LaunchOutcome.NO_RESULT
    if not isinstance(target, str) or not target:
        return LaunchOutcome.NO_RESULT
    try:
        ended = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if ended.tzinfo is None:
            return LaunchOutcome.NO_RESULT
    except (ValueError, OverflowError):
        return LaunchOutcome.NO_RESULT
    session = line.get("sessionId") or line.get("session_id") or transcript.stem
    if not isinstance(session, str):
        return LaunchOutcome.NO_RESULT
    location = path if isinstance(path, str) and path else directory
    if not isinstance(location, str) or not location:
        return LaunchOutcome.NO_LOCATION
    cwd = directory if isinstance(directory, str) and directory else location
    duration = duration_ms / 1000
    target_kind = "--bin" if metadata.get("launched_as") == "app" else "--example"
    argv = ["cargo", "build", "--workspace", target_kind, target, "--message-format=json"]
    if metadata.get("profile") == "release" or parameters.get("profile") == "release":
        argv.append("--release")
    facts = record.git_facts(location)
    if backfill:
        facts["branch"] = None
        facts["sha"] = None
    return RecordedLaunch({
        "kind": "step",
        "v": record.RECORD_VERSION,
        "id": f"{session}:{timestamp}",
        "host": store.host_name(),
        "started_at": (ended - timedelta(milliseconds=duration_ms)).isoformat(),
        "ended_at": timestamp,
        "duration_s": round(duration, 3),
        "step": "build",
        "argv": argv,
        "cwd": cwd,
        **facts,
        "caller": "brp-launch",
        "session": session,
        "slice": "none",
        "tty": False,
        "status": 0 if result.get("status") == "success" else 1,
    })


def save_launch(record_data: dict[str, object]) -> bool:
    identity = str(record_data["id"])
    digest = hashlib.sha256(identity.encode()).hexdigest()
    path = store.root() / str(record_data["host"]) / f"launch-{digest}.jsonl"
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        _ = temporary.write_text(json.dumps(record_data, separators=(",", ":")) + "\n")
        _ = os.link(temporary, path)
    except FileExistsError:
        return False
    finally:
        temporary.unlink(missing_ok=True)
    return True


def collect_file(
    path: Path, known: dict[str, object], recorded_ids: set[str], no_location_ids: set[str]
) -> tuple[dict[str, object], Counter[LaunchOutcome], int, int]:
    with path.open("rb") as handle:
        size = os.fstat(handle.fileno()).st_size
        head = handle.read(min(size, MARK_BYTES)).hex()
        backfill = bool(known) and known.get("reread_complete") is not True
        saved_offset = known.get("offset", 0) if backfill else 0
        if not isinstance(saved_offset, int):
            saved_offset = 0
        offset = 0 if backfill else known.get("offset", 0)
        previous_head = known.get("head")
        previous_tail = known.get("tail")
        tail = ""
        if isinstance(offset, int) and 0 <= offset <= size:
            start = max(0, offset - MARK_BYTES)
            _ = handle.seek(start)
            tail = handle.read(offset - start).hex()
        if (
            not isinstance(offset, int)
            or offset > size
            or not isinstance(previous_head, str)
            or not head.startswith(previous_head)
            or previous_tail != tail
        ):
            offset = 0
        _ = handle.seek(offset)
        added = 0
        unresolved = 0
        counts: Counter[LaunchOutcome] = Counter()
        while True:
            line_start = handle.tell()
            raw = handle.readline()
            if not raw or not raw.endswith(b"\n"):
                break
            offset = handle.tell()
            if b"brp_launch" not in raw:
                continue
            if not any(marker in raw for marker in (b'"structuredContent"', b'"tool_result"', b'<task-notification>')):
                continue
            try:
                line = cast(object, json.loads(raw))
            except ValueError:
                continue
            if isinstance(line, dict):
                entry = cast(dict[str, object], line)
                result = result_from_line(entry)
                if result is None:
                    continue
                launch = launch_record(entry, path, backfill=backfill and line_start < saved_offset)
                if isinstance(launch, RecordedLaunch):
                    identity = cast(str, launch.step["id"])
                    if identity in recorded_ids:
                        continue
                    recorded_ids.add(identity)
                    counts[LaunchOutcome.RECORDED] += 1
                    if save_launch(launch.step):
                        added += 1
                        if launch.step["worktree"] is None:
                            unresolved += 1
                elif launch is LaunchOutcome.NO_LOCATION:
                    metadata = result.get("metadata")
                    if isinstance(metadata, dict):
                        timestamp = cast(dict[str, object], metadata).get("launch_timestamp")
                        session = entry.get("sessionId") or entry.get("session_id") or path.stem
                        if isinstance(timestamp, str) and isinstance(session, str):
                            identity = f"{session}:{timestamp}"
                            if identity not in no_location_ids:
                                no_location_ids.add(identity)
                                counts[launch] += 1
                else:
                    counts[launch] += 1
        start = max(0, offset - MARK_BYTES)
        _ = handle.seek(start)
        tail = handle.read(offset - start).hex()
    return {"offset": offset, "head": head, "tail": tail, "reread_complete": True}, counts, unresolved, added


def collect() -> LaunchCounts:
    """Read new lines, revisit legacy state once, and report launch outcomes."""
    root = store.root()
    root.mkdir(parents=True, exist_ok=True)
    with (root / LOCK_NAME).open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state_path = root / STATE_NAME
        try:
            loaded = cast(object, json.loads(state_path.read_text()))
        except (OSError, ValueError):
            loaded = {}
        state = cast(dict[str, object], loaded) if isinstance(loaded, dict) else {}
        added = 0
        unresolved = 0
        counts: Counter[LaunchOutcome] = Counter()
        recorded_ids: set[str] = set()
        no_location_ids: set[str] = set()
        for path in sorted(transcripts_root().glob("*/*.jsonl")):
            key = str(path)
            prior = state.get(key)
            known = cast(dict[str, object], prior) if isinstance(prior, dict) else {}
            try:
                state[key], file_counts, file_unresolved, count = collect_file(
                    path, known, recorded_ids, no_location_ids
                )
            except OSError:
                continue
            counts.update(file_counts)
            unresolved += file_unresolved
            added += count
        temporary = state_path.with_name(state_path.name + f".{os.getpid()}.tmp")
        _ = temporary.write_text(json.dumps(state, separators=(",", ":")))
        _ = temporary.replace(state_path)
        return LaunchCounts(
            recorded=counts[LaunchOutcome.RECORDED],
            not_a_launch=counts[LaunchOutcome.NOT_A_LAUNCH],
            no_result=counts[LaunchOutcome.NO_RESULT],
            no_location=counts[LaunchOutcome.NO_LOCATION],
            unresolved=unresolved,
            added=added,
        )
