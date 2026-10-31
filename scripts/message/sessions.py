#!/usr/bin/env python3
"""Find a live Claude session's socket, `SendMessage` address, session ID or transcript."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Literal, NotRequired, TypedDict, cast


# Claude's entrypoint for a print-mode run starts with this; an interactive session's is "cli".
HEADLESS_ENTRYPOINT_PREFIX = "sdk-"


class SessionRecord(TypedDict):
    pid: int
    sessionId: str
    name: str
    messagingSocketPath: str
    updatedAt: int
    # Older records and callers may omit it; read_session normalizes absence to empty.
    procStart: NotRequired[str]
    # "<tmux session name at start>:@<window>.%<pane>"; empty outside tmux. Only the pane id stays true.
    tmux: str
    # Every name the session had before the one it has now.
    formerNames: list[str]
    # How Claude was started: "cli" for an interactive session, "sdk-cli" for a `claude -p` run.
    # Older records and callers may omit it; read_session normalizes absence to empty.
    entrypoint: NotRequired[str]


@dataclass(frozen=True)
class LiveSessionProcess:
    """The registry or saved process identity positively identifies a live session."""

    pid: int


@dataclass(frozen=True)
class SessionNotLive:
    """The available registry or process identity establishes no live session."""


@dataclass(frozen=True)
class SessionLivenessUnknown:
    """Unreadable evidence prevents establishing whether the session is live."""

    reason: str


class UnreadableSessionRecord(Enum):
    FOUND = "unreadable session record"


RegistryEntry = SessionRecord | UnreadableSessionRecord


class RegistryDetails(TypedDict, total=False):
    cwd: str
    kind: str
    status: str


@dataclass(frozen=True)
class RegistryObservation:
    record: SessionRecord
    details: RegistryDetails


@dataclass(frozen=True)
class RegistryRead:
    entries: tuple[RegistryObservation, ...]
    unreadable: tuple[Path, ...]


def read_registry(*, dependencies: Callable[[Path], None] | None = None) -> RegistryRead:
    """Keep unreadable directories and records visible to complete observers."""
    directory = sessions_dir()
    try:
        paths = [path for path in directory.iterdir() if path.suffix == ".json"]
    except OSError:
        return RegistryRead((), (directory,))
    entries: list[RegistryObservation] = []
    unreadable: list[Path] = []
    for path in paths:
        try:
            observation = _read_observation(path, dependencies=dependencies)
        except FileNotFoundError:
            continue  # A session can remove its registry entry after the directory read.
        if isinstance(observation, UnreadableSessionRecord):
            unreadable.append(path)
        else:
            entries.append(observation)
    return RegistryRead(tuple(entries), tuple(unreadable))


def parse_session(parsed: object) -> RegistryEntry:
    """Validate one decoded registry write without reading its file again."""
    if not isinstance(parsed, dict):
        return UnreadableSessionRecord.FOUND
    data = cast(dict[str, object], parsed)
    pid = data.get("pid")
    session_id = data.get("sessionId")
    name = data.get("name")
    socket_path = data.get("messagingSocketPath")
    updated_at = data.get("updatedAt")
    proc_start = data.get("procStart")
    tmux = data.get("tmux")
    former = data.get("formerNames")
    entrypoint = data.get("entrypoint")
    if (
        not isinstance(pid, int)
        or isinstance(pid, bool)
        or pid <= 0
        or not isinstance(session_id, str)
        or not isinstance(name, str)
        or not isinstance(socket_path, str)
        or not isinstance(updated_at, int)
        or isinstance(updated_at, bool)
    ):
        return UnreadableSessionRecord.FOUND
    return SessionRecord(
        pid=pid,
        sessionId=session_id,
        name=name,
        messagingSocketPath=socket_path,
        updatedAt=updated_at,
        procStart=proc_start if isinstance(proc_start, str) else "",
        tmux=tmux if isinstance(tmux, str) else "",
        formerNames=[name for name in cast(list[object], former) if isinstance(name, str)]
        if isinstance(former, list) else [],
        entrypoint=entrypoint if isinstance(entrypoint, str) else "",
    )


def _read_observation(path: Path, *, dependencies: Callable[[Path], None] | None = None) -> RegistryObservation | UnreadableSessionRecord:
    """Read across a concurrent registry rewrite, without hiding persistent corruption."""
    for attempt in range(3):
        try:
            if dependencies is not None:
                dependencies(path)
            raw = cast(object, json.loads(path.read_text()))
            record = parse_session(raw)
            if not isinstance(record, UnreadableSessionRecord):
                return RegistryObservation(record, cast(RegistryDetails, raw))
        except FileNotFoundError:
            raise
        except (OSError, ValueError, TypeError):
            pass
        if attempt < 2:
            time.sleep(0.05)
    return UnreadableSessionRecord.FOUND


def read_session(path: Path) -> RegistryEntry:
    try:
        observation = _read_observation(path)
    except FileNotFoundError:
        return UnreadableSessionRecord.FOUND
    return observation if isinstance(observation, UnreadableSessionRecord) else observation.record


def remote_control(record: SessionRecord) -> Literal["active", "absent"]:
    """Read only this confirmed session's Remote Control registry field."""
    try:
        value = cast(object, json.loads((sessions_dir() / f"{record['pid']}.json").read_text()))
        if isinstance(value, dict):
            bridge = cast(dict[str, object], value).get("bridgeSessionId")
            if isinstance(bridge, str) and bridge:
                return "active"
    except (OSError, ValueError, TypeError):
        pass
    return "absent"


def process_start(pid: int) -> str | None:
    """The process identity stored in session records, in this platform's native units."""
    if sys.platform == "darwin":
        # Claude writes procStart under this locale and zone; other text never matches.
        try:
            result = subprocess.run(
                ("ps", "-o", "lstart=", "-p", str(pid)),
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
                env={**os.environ, "LC_ALL": "C", "TZ": "UTC"},
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        started = result.stdout.strip()
        return started if result.returncode == 0 and started else None
    try:
        # The kernel can truncate the process name inside a UTF-8 character.
        _, separator, fields = Path(f"/proc/{pid}/stat").read_bytes().rpartition(b")")
        if not separator:
            return None
        started = fields.split()[19].decode("ascii")
        return started if started.isdigit() else None
    except (OSError, IndexError, UnicodeDecodeError):
        return None


def headless(record: SessionRecord) -> bool:
    """A `claude -p` run, such as send.py's relay, which takes its sender's name for one message.

    It is no session: no roster lists it, and no name lookup finds it.
    """
    return record.get("entrypoint", "").startswith(HEADLESS_ENTRYPOINT_PREFIX)


def running_session(record: SessionRecord) -> bool:
    """The pid runs with its recorded start, or an older record has its socket. A headless run never counts."""
    if headless(record):
        return False
    try:
        os.kill(record["pid"], 0)
    except PermissionError:
        pass
    except (ProcessLookupError, OverflowError):
        return False
    recorded_start = record.get("procStart", "")
    if recorded_start:
        return recorded_start == process_start(record["pid"])
    return messaging_socket_exists(record)


def process_liveness(pid: int, recorded_start: str) -> LiveSessionProcess | SessionNotLive | SessionLivenessUnknown:
    """Check the saved process identity without treating unreadable evidence as absence."""
    if not recorded_start:
        return SessionNotLive()
    current = process_start(pid)
    if current is not None and current != recorded_start:
        return SessionNotLive()
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return SessionNotLive()
    except PermissionError:
        if current == recorded_start:
            return LiveSessionProcess(pid)
    except (OSError, OverflowError) as error:
        return SessionLivenessUnknown(f"process identity unreadable: {error}")
    if current is None:
        return SessionLivenessUnknown(f"process identity unreadable for pid {pid}")
    return LiveSessionProcess(pid)


@dataclass(frozen=True)
class SessionProcesses:
    """Every live registry record's pid for a session, from a complete registry read."""

    pids: tuple[int, ...]


def _session_observations(session_id: str) -> Iterator[LiveSessionProcess | SessionLivenessUnknown]:
    registry = read_registry()
    if sessions_dir() in registry.unreadable:
        yield SessionLivenessUnknown("session registry unreadable: " + str(sessions_dir()))
        return
    for observation in registry.entries:
        registered = observation.record
        if registered["sessionId"] != session_id:
            continue
        if running_session(registered):
            yield LiveSessionProcess(registered["pid"])
        else:
            observed = process_liveness(registered["pid"], registered.get("procStart", ""))
            if not isinstance(observed, SessionNotLive):
                yield observed
    if registry.unreadable:
        yield SessionLivenessUnknown("unreadable session registry record: " + registry.unreadable[0].name)


def session_liveness(session_id: str) -> LiveSessionProcess | SessionNotLive | SessionLivenessUnknown:
    """Find a running session and its pid, preserving unknown registry or process identity."""
    return next(_session_observations(session_id), SessionNotLive())


def session_processes(session_id: str) -> SessionProcesses | SessionLivenessUnknown:
    """Count all registered processes; incomplete evidence cannot prove a unique process."""
    pids: list[int] = []
    for observed in _session_observations(session_id):
        if isinstance(observed, SessionLivenessUnknown):
            return observed
        pids.append(observed.pid)
    return SessionProcesses(tuple(sorted(pids)))


def messaging_socket_exists(record: SessionRecord) -> bool:
    try:
        return stat.S_ISSOCK(Path(record["messagingSocketPath"]).stat().st_mode)
    except (OSError, ValueError, TypeError):
        return False


def live_session(record: SessionRecord) -> bool:
    return running_session(record) and messaging_socket_exists(record)


def sessions_dir() -> Path:
    return Path(os.environ.get("NOTIFIER_SESSIONS_DIR", str(Path.home() / ".claude/sessions")))


def _readable_records() -> Iterator[SessionRecord]:
    yield from (item.record for item in read_registry().entries)


def running_sessions() -> list[SessionRecord]:
    """Every running record, newest first, including processes without sockets."""
    return sorted(
        (record for record in _readable_records() if running_session(record)),
        key=lambda record: record["updatedAt"], reverse=True,
    )


def live_sessions() -> list[SessionRecord]:
    """Every running record with a messaging socket, newest first."""
    return sorted(
        (record for record in _readable_records() if live_session(record)),
        key=lambda record: record["updatedAt"], reverse=True,
    )


def addressed(to: str, records: list[SessionRecord]) -> SessionRecord | None:
    """The live session that `to` means: by `uds:` socket, by `session:` id, by its name now, or by a
    name that it alone once had and no session has now."""
    if to.startswith("uds:"):
        return next((record for record in records if record["messagingSocketPath"] == to[4:]), None)
    if to.startswith("session:"):
        return next((record for record in records if record["sessionId"] == to[8:]), None)
    now = next((record for record in records if record["name"] == to), None)
    if now is not None:
        return now
    once = [record for record in records if to in record["formerNames"]]
    return once[0] if len(once) == 1 else None


def named(name: str, records: list[SessionRecord]) -> SessionRecord | None:
    """The live session a session name or unit id means, as `addressed` finds it: "handler-unit" and
    "handler" each find the session named either way."""
    bare = name.removesuffix("-unit")
    return next((found for candidate in dict.fromkeys((name, bare, f"{bare}-unit"))
                 if (found := addressed(candidate, records)) is not None), None)


def latest_transcript(session_id: str, projects: Path) -> Path | None:
    """The session's transcript under the Claude projects folder: the newest one, when a resume left several."""
    transcripts = sorted(projects.glob(f"*/{session_id}.jsonl"), key=lambda path: path.stat().st_mtime)
    return transcripts[-1] if transcripts else None


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] not in {"socket", "address", "id"} or not argv[2]:
        print(
            "usage: sessions.py socket <session:id|name> | address <session:id|name> | id <pid|name>",
            file=sys.stderr,
        )
        return 2

    command, target = argv[1:]
    directory = sessions_dir()
    try:
        paths = sorted(path for path in directory.iterdir() if path.suffix == ".json")
    except OSError as error:
        detail = str(error).splitlines()
        print(
            f"sessions: cannot list registry: {detail[0] if detail else type(error).__name__}",
            file=sys.stderr,
        )
        return 3
    reads: list[RegistryEntry] = []
    for path in paths:
        try:
            observation = _read_observation(path)
        except FileNotFoundError:
            continue
        reads.append(observation if isinstance(observation, UnreadableSessionRecord) else observation.record)
    unreadable = any(read is UnreadableSessionRecord.FOUND for read in reads)
    records = (read for read in reads if not isinstance(read, UnreadableSessionRecord))
    if command != "id" and target.startswith("session:"):
        matches = (record for record in records if record["sessionId"] == target[8:])
    elif command != "id":
        matches = (record for record in records if record["name"] == target)
    elif target.isascii() and target.isdigit():
        matches = (record for record in records if record["pid"] == int(target))
    else:
        matches = (record for record in records if record["name"] == target)
    newest = max(
        (record for record in matches if live_session(record)),
        key=lambda record: record["updatedAt"], default=None,
    )
    if newest is None:
        if unreadable:
            print("sessions: one or more registry files could not be read", file=sys.stderr)
            return 3
        return 1
    # `SendMessage` takes the address as its `to`: the session on this machine,
    # whatever other sessions the sender's account lists under the same name.
    print({
        "socket": newest["messagingSocketPath"],
        "address": f"uds:{newest['messagingSocketPath']}",
        "id": newest["sessionId"],
    }[command])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
