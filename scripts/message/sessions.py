#!/usr/bin/env python3
"""Find a live Claude session's socket, `SendMessage` address, session ID or transcript."""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import json
import os
import stat
import sys
import time
import tomllib
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from enum import Enum
from importlib import import_module
from pathlib import Path
from typing import Literal, NotRequired, TypedDict, cast

from scripts.lib.session_paths import sessions_directory
from scripts.lib.transcripts import transcript_paths

from scripts.message.process_identity import process_start as process_start
from scripts.message.relay_identity import RelayIdentity, live_relay_identity, relay_senders_directory


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
    # The conversation's folder, for the exact staff-worktree join. Older records may omit it.
    cwd: NotRequired[str]
    # Older records omit status. The reader converts absence to an empty string.
    status: NotRequired[str]


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


@dataclass(frozen=True)
class SessionRetryPolicy:
    registry_reads: int
    registry_delay_seconds: float


def retry_policy() -> SessionRetryPolicy:
    """Read tuning at use; a missing or invalid setting never invents a retry budget."""
    path = Path(__file__).resolve().parents[2] / "config/session_retries.toml"
    with path.open("rb") as source:
        values = cast(dict[str, object], tomllib.load(source))
    count = values["registry_reads"]
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        raise ValueError(f"{path}: registry_reads must be a positive integer")
    delay = values["registry_delay_seconds"]
    if not isinstance(delay, (int, float)) or isinstance(delay, bool) or not 0 <= delay < float("inf"):
        raise ValueError(f"{path}: registry_delay_seconds must be finite and nonnegative")
    return SessionRetryPolicy(count, float(delay))


@dataclass(frozen=True)
class RegistryRecordGone:
    """The file vanished, or its filename's process is proven gone."""


def read_registry(*, dependencies: Callable[[Path], None] | None = None) -> RegistryRead:
    """Keep unreadable directories and records visible to complete observers."""
    directory = sessions_dir()
    try:
        paths = [path for path in directory.iterdir() if path.suffix == ".json"]
    except OSError:
        return RegistryRead((), (directory,))
    entries: list[RegistryObservation] = []
    unreadable: list[Path] = []
    policy = retry_policy()
    for path in paths:
        observation = _read_observation(path, policy, dependencies=dependencies)
        if isinstance(observation, RegistryRecordGone):
            continue
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
    cwd = data.get("cwd")
    status = data.get("status")
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
        cwd=cwd if isinstance(cwd, str) else "",
        status=status if isinstance(status, str) else "",
    )


def _read_observation(path: Path, policy: SessionRetryPolicy, *, dependencies: Callable[[Path], None] | None = None) -> RegistryObservation | UnreadableSessionRecord | RegistryRecordGone:
    """Reread a concurrent write; only a live unreadable record remains uncertain."""
    for attempt in range(policy.registry_reads):
        try:
            if dependencies is not None:
                dependencies(path)
            raw = cast(object, json.loads(path.read_text()))
            record = parse_session(raw)
            if not isinstance(record, UnreadableSessionRecord):
                return RegistryObservation(record, cast(RegistryDetails, raw))
        except FileNotFoundError:
            return RegistryRecordGone()
        except (OSError, ValueError, TypeError):
            pass
        if attempt + 1 < policy.registry_reads:
            time.sleep(policy.registry_delay_seconds)
    if path.stem.isdigit():
        try:
            os.kill(int(path.stem), 0)
        except (ProcessLookupError, OverflowError):
            return RegistryRecordGone()
        except OSError:
            pass  # A denied process probe does not prove absence.
    return UnreadableSessionRecord.FOUND


def read_session(path: Path) -> RegistryEntry:
    observation = _read_observation(path, retry_policy())
    return observation.record if isinstance(observation, RegistryObservation) else UnreadableSessionRecord.FOUND


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


def headless_process(pid: int, entrypoint: str = "") -> bool:
    """Exclude SDK and relay processes even before a session record exists."""
    return (entrypoint.startswith(HEADLESS_ENTRYPOINT_PREFIX)
            or isinstance(live_relay_identity(pid, relay_senders_directory()), RelayIdentity))


def headless(record: SessionRecord) -> bool:
    """A `claude -p` run, or a process identified by the relay launcher's PID record.

    It is no session: no roster lists it, and no name lookup finds it.
    """
    return headless_process(record["pid"], record.get("entrypoint", ""))


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
    return sessions_directory()


def _readable_records() -> Iterator[SessionRecord]:
    yield from (item.record for item in read_registry().entries)


def _running(records: Iterable[SessionRecord]) -> list[SessionRecord]:
    return sorted((record for record in records if running_session(record)),
                  key=lambda record: record["updatedAt"], reverse=True)


def running_records(registry: RegistryRead) -> list[SessionRecord]:
    """Every running record of one registry read, newest first, including processes without sockets."""
    return _running(item.record for item in registry.entries)


def running_sessions() -> list[SessionRecord]:
    """Every running record, newest first, including processes without sockets."""
    return _running(_readable_records())


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
    """Find the live session with this staff name."""
    return addressed(name, records)


def config_dirs() -> list[Path]:
    directories = {path for path in Path.home().glob(".claude*") if path.is_dir()}
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    if override:
        directories.add(Path(override).expanduser())
    return sorted(directories)


def transcript_locations(session_id: str) -> list[Path]:
    """Return distinct transcript paths across the Claude accounts."""
    return sorted({path.resolve() for directory in config_dirs()
                   for path in transcript_paths(directory / "projects", f"*/{session_id}.jsonl")})


def latest_transcript(session_id: str, projects: Path) -> Path | None:
    """The session's transcript under the Claude projects folder: the newest one, when a resume left several."""
    transcripts = sorted(transcript_paths(projects, f"*/{session_id}.jsonl"), key=lambda path: path.stat().st_mtime)
    return transcripts[-1] if transcripts else None


def member_target(target: str) -> str:
    """Resolve a retained staff reference to its current conversation at delivery."""
    if not target.startswith("session:"):
        return target
    resolve = cast(Callable[[str], str], import_module("scripts.production.staff").current_session_target)
    return resolve(target)


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] not in {"socket", "member-socket", "address", "id"} or not argv[2]:
        print(
            "usage: sessions.py socket <session:id|name> | address <session:id|name> | id <pid|name>",
            file=sys.stderr,
        )
        return 2

    command, target = argv[1:]
    if command == "member-socket":
        try:
            target = member_target(target)
        except (OSError, ValueError) as error:
            print(f"sessions: staff target unavailable: {error}", file=sys.stderr)
            return 3
        command = "socket"
    registry = read_registry()
    unreadable = bool(registry.unreadable)
    records = (observation.record for observation in registry.entries)
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
