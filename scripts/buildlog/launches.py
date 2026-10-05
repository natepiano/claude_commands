"""Collect example builds and app starts from Claude Code launch results.

Each launch has its own atomic JSON line file. Its session and timestamp name
make replay after an interrupted state update safe without scanning old logs.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast

import record
import store

STATE_NAME = "launches_state.json"
LOCK_NAME = "launches.lock"
MARK_BYTES = 256


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


def launch_record(line: dict[str, object], transcript: Path) -> dict[str, object] | None:
    result = result_from_line(line)
    if result is None:
        return None
    call_info = result.get("call_info")
    metadata = result.get("metadata")
    parameters = result.get("parameters")
    if not isinstance(call_info, dict):
        return None
    call_info = cast(dict[str, object], call_info)
    if call_info.get("mcp_tool") != "brp_launch":
        return None
    if not isinstance(metadata, dict) or not isinstance(parameters, dict):
        return None
    metadata = cast(dict[str, object], metadata)
    parameters = cast(dict[str, object], parameters)
    timestamp = metadata.get("launch_timestamp")
    duration_ms = metadata.get("launch_duration_ms")
    target = metadata.get("target_name") or parameters.get("target_name")
    directory = metadata.get("working_directory")
    worktree = parameters.get("path")
    if not isinstance(timestamp, str) or not isinstance(duration_ms, int | float) or duration_ms < 0:
        return None
    if not isinstance(target, str) or not isinstance(directory, str) or not isinstance(worktree, str):
        return None
    try:
        ended = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if ended.tzinfo is None:
            return None
    except (ValueError, OverflowError):
        return None
    session = line.get("sessionId") or line.get("session_id") or transcript.stem
    if not isinstance(session, str):
        return None
    duration = duration_ms / 1000
    target_kind = "--bin" if metadata.get("launched_as") == "app" else "--example"
    argv = ["cargo", "build", "--workspace", target_kind, target, "--message-format=json"]
    if metadata.get("profile") == "release" or parameters.get("profile") == "release":
        argv.append("--release")
    facts = record.git_facts(worktree)
    return {
        "kind": "step",
        "v": record.RECORD_VERSION,
        "id": f"{session}:{timestamp}",
        "host": store.host_name(),
        "started_at": (ended - timedelta(milliseconds=duration_ms)).isoformat(),
        "ended_at": timestamp,
        "duration_s": round(duration, 3),
        "step": "build",
        "argv": argv,
        "cwd": directory,
        **facts,
        "worktree": worktree,
        "repo_path": facts["repo_path"] or worktree,
        "caller": "brp-launch",
        "session": session,
        "slice": "none",
        "tty": False,
        "status": 0 if result.get("status") == "success" else 1,
    }


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


def collect_file(path: Path, known: dict[str, object]) -> tuple[dict[str, object], int]:
    with path.open("rb") as handle:
        size = os.fstat(handle.fileno()).st_size
        head = handle.read(min(size, MARK_BYTES)).hex()
        offset = known.get("offset", 0)
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
        while True:
            raw = handle.readline()
            if not raw or not raw.endswith(b"\n"):
                break
            offset = handle.tell()
            if b"launch_duration_ms" not in raw or b"brp_launch" not in raw:
                continue
            try:
                line = cast(object, json.loads(raw))
            except ValueError:
                continue
            if isinstance(line, dict):
                launch = launch_record(cast(dict[str, object], line), path)
                if launch is not None and save_launch(launch):
                    added += 1
        start = max(0, offset - MARK_BYTES)
        _ = handle.seek(start)
        tail = handle.read(offset - start).hex()
    return {"offset": offset, "head": head, "tail": tail}, added


def collect() -> int:
    """Read only new transcript lines and return the launches added."""
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
        for path in sorted(transcripts_root().glob("*/*.jsonl")):
            key = str(path)
            prior = state.get(key)
            known = cast(dict[str, object], prior) if isinstance(prior, dict) else {}
            try:
                state[key], count = collect_file(path, known)
            except OSError:
                continue
            added += count
        temporary = state_path.with_name(state_path.name + f".{os.getpid()}.tmp")
        _ = temporary.write_text(json.dumps(state, separators=(",", ":")))
        _ = temporary.replace(state_path)
        return added
