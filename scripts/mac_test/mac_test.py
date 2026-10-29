#!/usr/bin/env python3
"""Coordinate build-step use of the Mac: one claim per repository, and the Mac block."""

from __future__ import annotations

import argparse
from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import threading
from typing import Literal, NotRequired, TypeGuard, TypedDict, cast


sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lint"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "build_hold"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import override_guard
import build_hold
import offload
import sweep
from user_zone import user_zone


MESSAGE_TIMEOUT_S = 30.0


class StateReadError(ValueError):
    """A Mac coordination record cannot be read or decoded."""


class ClaimStateUnreadable(StateReadError):
    """A claim file, or the claims folder, that cannot be read or decoded."""

    def __init__(self, path: Path, error: Exception) -> None:
        super().__init__(str(error))
        self.path: Path = path


class ClaimOwnerRecord(TypedDict):
    """The process identity and job details retained by either kind of claim."""

    pid: int
    proc_start: str
    what: str
    worktree: str
    since: str
    holder_session: NotRequired[str]
    idle_since: NotRequired[str]
    copy: NotRequired[bool]


class ProcessBoundClaim(ClaimOwnerRecord):
    """Mac capacity held only while the claiming process lives."""


class RemoteJobClaim(ClaimOwnerRecord):
    """Mac capacity held until the named host's job is known to have ended."""

    remote_host: str


RunRecord = ProcessBoundClaim | RemoteJobClaim


@dataclass(frozen=True)
class RepositoryScope:
    """A claim that excludes only other claims for the same repository."""

    name: str


@dataclass(frozen=True)
class WholeMacScope:
    """A claim that names no repository, so it excludes every other claim."""


ClaimScope = RepositoryScope | WholeMacScope
WHOLE_MAC = WholeMacScope()


@dataclass(frozen=True)
class HeldClaim:
    """One claim record, the repository it covers and the file that holds it."""

    scope: ClaimScope
    path: Path
    record: RunRecord


@dataclass(frozen=True)
class ClaimFinished:
    """A claim attempt that ended with its exit status."""

    status: int


@dataclass(frozen=True)
class ClaimBusy:
    """A claim attempt refused by a live claim whose scope overlaps."""

    holder: RunRecord


@dataclass(frozen=True)
class ClaimHeldByCopy:
    """A claim attempt whose overlapping live claims are all copies, which give way."""

    holders: tuple[RunRecord, ...]


ClaimAttempt = ClaimFinished | ClaimBusy | ClaimHeldByCopy


class NoRemoteJob:
    """A claim requested for the calling process alone."""


NO_REMOTE_JOB = NoRemoteJob()


CiSwitch = Literal[
    "off_by_this_block",
    "off_before",
    "not_configured",
    "still_on",
    "off_unconfirmed",
]


class NoRun:
    """No process owns the Mac."""


class NoMacBlock:
    """No session excludes work from the Mac."""


@dataclass(frozen=True)
class RecordedSession:
    """The session recorded when a block was requested."""

    name: str
    holder: str


@dataclass(frozen=True)
class HolderQueue:
    """The holder's named message queue."""

    name: str


MessageDelivery = RecordedSession | HolderQueue


@dataclass(frozen=True)
class NamedShowrunner:
    """A showrunner that also receives expiry messages."""

    name: str


class NoShowrunner:
    """A block with no showrunner recipient."""


Showrunner = NamedShowrunner | NoShowrunner


@dataclass(frozen=True)
class WaitingOnLocalRun:
    """A block waiting for a local Mac run."""

    what: str


class WaitingOnCiJob:
    """A block waiting for CI's Mac job."""


class WaitingOnUnknownCi:
    """A block waiting until CI can be checked."""


WaitingOn = WaitingOnLocalRun | WaitingOnCiJob | WaitingOnUnknownCi


class FreeMessageNotNeeded:
    """An active block that did not wait for Mac work."""


@dataclass(frozen=True)
class FreeMessageOwed:
    """An active block whose availability message still needs delivery."""

    what: str
    ended: datetime


class FreeMessageSent:
    """An active block whose availability message was delivered."""


FreeMessage = FreeMessageNotNeeded | FreeMessageOwed | FreeMessageSent


@dataclass(frozen=True)
class PendingMacBlock:
    """A block that is waiting for existing Mac work to finish."""

    holder: str
    reason: str
    since: datetime
    expires: datetime
    delivery: MessageDelivery
    showrunner: Showrunner
    ci: CiSwitch
    waiting_on: WaitingOn


@dataclass(frozen=True)
class ActiveMacBlock:
    """A block that currently has exclusive control of the Mac."""

    holder: str
    reason: str
    since: datetime
    expires: datetime
    delivery: MessageDelivery
    showrunner: Showrunner
    ci: CiSwitch
    free_message: FreeMessage


MacBlockState = NoMacBlock | PendingMacBlock | ActiveMacBlock
RunState = RunRecord | NoRun

NO_RUN = NoRun()
NO_MAC_BLOCK = NoMacBlock()
NO_SHOWRUNNER = NoShowrunner()
WAITING_ON_CI_JOB = WaitingOnCiJob()
WAITING_ON_UNKNOWN_CI = WaitingOnUnknownCi()
FREE_MESSAGE_NOT_NEEDED = FreeMessageNotNeeded()
FREE_MESSAGE_SENT = FreeMessageSent()


@dataclass(frozen=True)
class GhAnswer:
    """Text returned by a successful gh call."""

    stdout: str


@dataclass(frozen=True)
class GhFailure:
    """The useful final line from a failed gh call."""

    line: str


GhResult = GhAnswer | GhFailure


@dataclass(frozen=True)
class CiSwitchReadFailed:
    """The current CI switch value could not be read."""

    line: str


@dataclass(frozen=True)
class CiSwitchWriteUnconfirmed:
    """The request to turn off the CI switch had no confirmed result."""

    line: str


CiSwitchOffResult = (
    Literal["off_before", "off_by_this_block"]
    | CiSwitchReadFailed
    | CiSwitchWriteUnconfirmed
)


class CiIdle:
    """No queued or running CI Mac job exists."""


class CiBusy:
    """A queued or running CI Mac job exists."""


@dataclass(frozen=True)
class CiUnknown:
    """CI could not be checked."""

    line: str


CiActivity = CiIdle | CiBusy | CiUnknown
CI_IDLE = CiIdle()
CI_BUSY = CiBusy()


class NoProblem:
    """No diagnostic line was produced."""


@dataclass(frozen=True)
class ProblemLine:
    """A diagnostic line produced by an external call."""

    line: str


Problem = NoProblem | ProblemLine
NO_PROBLEM = NoProblem()


class NoExpiredBlockLiftFailure:
    """No expired block remains in place because its lift failed."""


@dataclass(frozen=True)
class ExpiredBlockLiftFailed:
    """An expired block remains because its CI switch could not be restored."""

    gh_error_line: str


ExpiredBlockLiftFailure = NoExpiredBlockLiftFailure | ExpiredBlockLiftFailed
NO_EXPIRED_BLOCK_LIFT_FAILURE = NoExpiredBlockLiftFailure()


@dataclass(frozen=True)
class MacTestConfig:
    """Mac claim, block and CI settings."""

    ci_repo: str
    ci_variable: str
    ci_workflow: str
    ci_job: str
    gh_timeout_s: float
    block_hours: float
    block_max_hours: float
    block_warn_minutes: float
    host: str = "mac"
    connect_timeout_s: float = 6.0
    claim_idle_minutes: float = 15.0
    claim_poll_seconds: float = 60.0


@dataclass(frozen=True)
class MacActive:
    """The user's Mac processes seen by the activity check."""

    count: int


@dataclass(frozen=True)
class MacIdle:
    """The activity check found no Mac process."""


@dataclass(frozen=True)
class ActivityCheckFailed:
    """Mac activity could not be checked or its answer could not be read."""

    reason: str


MacActivity = MacActive | MacIdle | ActivityCheckFailed


@dataclass(frozen=True)
class SettleOutcome:
    """State and diagnostics after one complete settlement pass."""

    claims: tuple[HeldClaim, ...]
    block: MacBlockState
    expired_block_lift_failure: ExpiredBlockLiftFailure
    ci_problem: Problem
    waiting_problem: Problem


@dataclass(frozen=True)
class ValidBlockDuration:
    """A finite block duration accepted by the configured limit."""

    hours: float


class InvalidBlockDuration:
    """A block duration outside the configured limit."""


BlockDuration = ValidBlockDuration | InvalidBlockDuration
INVALID_BLOCK_DURATION = InvalidBlockDuration()
FreeNowAnswer = Literal["free", "off", "backoff", "blocked", "busy", "state"]


class CliArguments(argparse.Namespace):
    """Arguments accepted across the mac-test subcommands."""

    def __init__(self) -> None:
        super().__init__()
        self.command: str = ""
        self.pid: int = 0
        self.what: str = ""
        self.worktree: str = ""
        self.wait: float = 0.0
        self.remote: str | NoRemoteJob = NO_REMOTE_JOB
        self.holder: str = ""
        self.reason: str = ""
        self.hours: str = ""
        self.showrunner: str = ""
        self.settings: str = ""
        self.repo_root: str = ""
        self.copy: bool = False


class StatePaths:
    """Files used by mac-test coordination."""

    def __init__(self, directory: Path) -> None:
        self.directory: Path = directory
        self.lock: Path = directory / "state.lock"
        self.control_lock: Path = directory / "control.lock"
        self.watch_lock: Path = directory / "watch.lock"
        self.claim_watch_lock: Path = directory / "claim_watch.lock"
        self.run: Path = directory / "run.json"
        self.claims: Path = directory / "claims"
        self.block: Path = directory / "block.json"


def state_paths() -> StatePaths:
    configured = os.environ.get("MAC_TEST_STATE_DIR", "~/.local/state/mac-test")
    return StatePaths(Path(configured).expanduser())


def config_path() -> Path:
    configured = os.environ.get("MAC_TEST_CONFIG", "~/.claude/config/mac_test.conf")
    return Path(configured).expanduser()


def configured_number(values: dict[str, str], key: str, default: float) -> float:
    try:
        value = float(values.get(key, str(default)))
    except ValueError:
        return default
    return value if math.isfinite(value) and value > 0 else default


def read_config() -> MacTestConfig:
    values = sweep.config_values(str(config_path()))
    return MacTestConfig(
        ci_repo=values.get("ci_repo", ""),
        ci_variable=values.get("ci_variable", "MACOS_CI"),
        ci_workflow=values.get("ci_workflow", "ci.yml"),
        ci_job=values.get("ci_job", "macOS: Compile and Test"),
        gh_timeout_s=configured_number(values, "gh_timeout_s", 30.0),
        block_hours=configured_number(values, "block_hours", 4.0),
        block_max_hours=configured_number(values, "block_max_hours", 48.0),
        block_warn_minutes=configured_number(values, "block_warn_minutes", 15.0),
        host=values.get("host", "mac"),
        connect_timeout_s=configured_number(values, "connect_timeout_s", 6.0),
        claim_idle_minutes=configured_number(values, "claim_idle_minutes", 15.0),
        claim_poll_seconds=configured_number(values, "claim_poll_seconds", 60.0),
    )


@contextmanager
def locked(path: Path) -> Generator[None, None, None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with record_after_unlock(), path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@contextmanager
def lock_if_available(path: Path) -> Generator[bool, None, None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def read_json_object(path: Path) -> dict[str, object] | None:
    try:
        decoded = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise StateReadError(f"{path}: {error}") from error
    if not isinstance(decoded, dict):
        raise StateReadError(f"{path} does not contain a JSON object")
    return cast(dict[str, object], decoded)


def read_run(path: Path) -> RunState:
    values = read_json_object(path)
    if values is None:
        return NO_RUN
    pid = values.get("pid")
    proc_start = values.get("proc_start")
    what = values.get("what")
    worktree = values.get("worktree")
    since = values.get("since")
    if (
        type(pid) is not int
        or not isinstance(proc_start, str)
        or not isinstance(what, str)
        or not isinstance(worktree, str)
        or not isinstance(since, str)
    ):
        raise StateReadError(f"{path} has an invalid run record")
    _ = aware_timestamp(since, path, "since")
    record: ProcessBoundClaim = {
        "pid": pid,
        "proc_start": proc_start,
        "what": what,
        "worktree": worktree,
        "since": since,
    }
    if "holder_session" in values:
        holder_session = required_string(values, "holder_session", path)
        if not holder_session.strip():
            raise StateReadError(f"{path} has an invalid holder_session")
        record["holder_session"] = holder_session
    if "idle_since" in values:
        idle_since = values["idle_since"]
        _ = aware_timestamp(idle_since, path, "idle_since")
        record["idle_since"] = cast(str, idle_since)
    if "copy" in values:
        if values["copy"] is not True:
            raise StateReadError(f"{path} has an invalid copy")
        record["copy"] = True
    if "remote_host" not in values:
        return record
    host = values["remote_host"]
    if not isinstance(host, str) or not host.strip():
        raise StateReadError(f"{path} has an invalid remote_host")
    remote: RemoteJobClaim = {**record, "remote_host": host}
    return remote


def aware_timestamp(value: object, path: Path, field: str) -> datetime:
    if not isinstance(value, str):
        raise StateReadError(f"{path} has an invalid {field}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise StateReadError(f"{path} has an invalid {field}") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise StateReadError(f"{path} has an invalid {field}")
    return parsed


def required_string(values: dict[str, object], key: str, path: Path) -> str:
    value = values.get(key)
    if not isinstance(value, str):
        raise StateReadError(f"{path} has an invalid block record")
    return value


def decoded_delivery(session: object, holder: str, path: Path) -> MessageDelivery:
    if session is None:
        return HolderQueue(holder)
    if not isinstance(session, str):
        raise StateReadError(f"{path} has an invalid block record")
    return RecordedSession(session, holder)


def decoded_showrunner(showrunner: object, path: Path) -> Showrunner:
    if showrunner is None:
        return NO_SHOWRUNNER
    if not isinstance(showrunner, str):
        raise StateReadError(f"{path} has an invalid block record")
    return NamedShowrunner(showrunner)


def decoded_ci(value: object, path: Path) -> CiSwitch:
    if value not in (
        "off_by_this_block",
        "off_before",
        "not_configured",
        "still_on",
        "off_unconfirmed",
    ):
        raise StateReadError(f"{path} has an invalid block record")
    return value


def decode_waiting(value: object, path: Path) -> WaitingOn:
    if not isinstance(value, dict):
        raise StateReadError(f"{path} has an invalid block record")
    item = cast(dict[str, object], value)
    kind = item.get("kind")
    if kind == "local_run" and set(item) == {"kind", "what"}:
        what = item.get("what")
        if isinstance(what, str):
            return WaitingOnLocalRun(what)
    if kind == "ci_job" and set(item) == {"kind"}:
        return WAITING_ON_CI_JOB
    if kind == "ci_unknown" and set(item) == {"kind"}:
        return WAITING_ON_UNKNOWN_CI
    raise StateReadError(f"{path} has an invalid block record")


def decode_free_message(value: object, path: Path) -> FreeMessage:
    if not isinstance(value, dict):
        raise StateReadError(f"{path} has an invalid block record")
    item = cast(dict[str, object], value)
    kind = item.get("kind")
    if kind == "not_needed" and set(item) == {"kind"}:
        return FREE_MESSAGE_NOT_NEEDED
    if kind == "sent" and set(item) == {"kind"}:
        return FREE_MESSAGE_SENT
    if kind == "owed" and set(item) == {"kind", "what", "ended"}:
        what = item.get("what")
        if isinstance(what, str):
            return FreeMessageOwed(what, aware_timestamp(item.get("ended"), path, "ended"))
    raise StateReadError(f"{path} has an invalid block record")


def decode_legacy_block(values: dict[str, object], path: Path) -> MacBlockState:
    required = {"holder", "session", "for", "since", "state"}
    if set(values) != required:
        raise StateReadError(f"{path} has an invalid block record")
    holder = required_string(values, "holder", path)
    reason = required_string(values, "for", path)
    since = aware_timestamp(values.get("since"), path, "since")
    delivery = decoded_delivery(values.get("session"), holder, path)
    state = values.get("state")
    if state == "pending":
        return PendingMacBlock(
            holder, reason, since, since + timedelta(hours=48), delivery,
            NO_SHOWRUNNER, "still_on", WaitingOnLocalRun("a test"),
        )
    if state == "active":
        return ActiveMacBlock(
            holder, reason, since, since + timedelta(hours=48), delivery,
            NO_SHOWRUNNER, "still_on", FREE_MESSAGE_NOT_NEEDED,
        )
    raise StateReadError(f"{path} has an invalid block record")


def read_block(path: Path) -> MacBlockState:
    values = read_json_object(path)
    if values is None:
        return NO_MAC_BLOCK
    if "version" not in values:
        return decode_legacy_block(values, path)
    state = values.get("state")
    common_keys = {
        "version", "holder", "session", "showrunner", "for", "since",
        "expires", "state", "ci",
    }
    expected_keys = common_keys | ({"waiting_on"} if state == "pending" else {"free_message"})
    if values.get("version") != 2 or set(values) != expected_keys:
        raise StateReadError(f"{path} has an invalid block record")
    holder = required_string(values, "holder", path)
    reason = required_string(values, "for", path)
    since = aware_timestamp(values.get("since"), path, "since")
    expires = aware_timestamp(values.get("expires"), path, "expires")
    delivery = decoded_delivery(values.get("session"), holder, path)
    showrunner = decoded_showrunner(values.get("showrunner"), path)
    ci = decoded_ci(values.get("ci"), path)
    if state == "pending":
        return PendingMacBlock(
            holder, reason, since, expires, delivery, showrunner, ci,
            decode_waiting(values.get("waiting_on"), path),
        )
    if state == "active":
        return ActiveMacBlock(
            holder, reason, since, expires, delivery, showrunner, ci,
            decode_free_message(values.get("free_message"), path),
        )
    raise StateReadError(f"{path} has an invalid block record")


def read_claim_block(paths: StatePaths) -> MacBlockState:
    """Read the block that refuses new claims until its record is removed."""
    return read_block(paths.block)


def timestamp(value: datetime) -> str:
    return value.isoformat()


def block_record(block: PendingMacBlock | ActiveMacBlock) -> dict[str, object]:
    record: dict[str, object] = {
        "version": 2,
        "holder": block.holder,
        "for": block.reason,
        "since": timestamp(block.since),
        "expires": timestamp(block.expires),
        "session": block.delivery.name if isinstance(block.delivery, RecordedSession) else None,
        "showrunner": block.showrunner.name if isinstance(block.showrunner, NamedShowrunner) else None,
        "state": "pending" if isinstance(block, PendingMacBlock) else "active",
        "ci": block.ci,
    }
    if isinstance(block, PendingMacBlock):
        waiting = block.waiting_on
        if isinstance(waiting, WaitingOnLocalRun):
            record["waiting_on"] = {"kind": "local_run", "what": waiting.what}
        elif isinstance(waiting, WaitingOnCiJob):
            record["waiting_on"] = {"kind": "ci_job"}
        else:
            record["waiting_on"] = {"kind": "ci_unknown"}
    else:
        message = block.free_message
        if isinstance(message, FreeMessageOwed):
            record["free_message"] = {
                "kind": "owed", "what": message.what, "ended": timestamp(message.ended),
            }
        elif isinstance(message, FreeMessageSent):
            record["free_message"] = {"kind": "sent"}
        else:
            record["free_message"] = {"kind": "not_needed"}
    return record


def write_json(path: Path, record: RunRecord | dict[str, object]) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", delete=False,
        ) as output:
            temporary_path = Path(output.name)
            json.dump(record, output, separators=(",", ":"))
            _ = output.write("\n")
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def write_block(path: Path, block: PendingMacBlock | ActiveMacBlock) -> None:
    write_json(path, block_record(block))


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class MacBlockEvent:
    kind: Literal["began", "ended"]
    block: PendingMacBlock | ActiveMacBlock
    at: datetime


pending_hold_events: ContextVar[tuple[MacBlockEvent, ...]] = ContextVar("pending_hold_events", default=())
coordination_depth: ContextVar[int] = ContextVar("coordination_depth", default=0)


@contextmanager
def record_after_unlock() -> Generator[None, None, None]:
    depth = coordination_depth.get()
    token = coordination_depth.set(depth + 1)
    try:
        yield
    finally:
        coordination_depth.reset(token)
        if depth == 0:
            events = pending_hold_events.get()
            _ = pending_hold_events.set(())
            for event in events:
                start_hold_recorder(event)


def start_hold_recorder(event: MacBlockEvent) -> None:
    command = [sys.executable, str(Path(__file__).resolve().parent.parent / "buildlog" / "record.py"),
               "hold-event", event.kind, "mac", timestamp(event.block.since), event.block.holder, event.block.reason,
               timestamp(event.at.astimezone(timezone.utc)), timestamp(event.block.expires)]
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    except OSError:
        return
    threading.Thread(target=process.wait, name="hold-event-recorder", daemon=True).start()


def record_hold_event(event: Literal["began", "ended"], block: PendingMacBlock | ActiveMacBlock, at: datetime) -> None:
    record = MacBlockEvent(event, block, at)
    if coordination_depth.get():
        _ = pending_hold_events.set((*pending_hold_events.get(), record))
    else:
        start_hold_recorder(record)


def local_time(instant: str, include_zone: bool = True) -> str:
    format_string = "%H:%M %Z" if include_zone else "%H:%M"
    return datetime.fromisoformat(instant).astimezone(user_zone()).strftime(format_string)


def block_time(instant: datetime) -> str:
    return instant.astimezone(user_zone()).strftime("%a %H:%M %Z")


def message_time(instant: datetime) -> str:
    return instant.astimezone(user_zone()).strftime("%H:%M %Z")


def process_start_time(pid: int) -> str | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    closing_parenthesis = stat.rfind(")")
    fields_after_name = stat[closing_parenthesis + 1 :].split()
    if closing_parenthesis < 0 or len(fields_after_name) <= 19:
        return None
    return fields_after_name[19]


def run_is_live(run: RunRecord) -> bool:
    return "remote_host" in run or process_start_time(run["pid"]) == run["proc_start"]


def run_is_kept(run: RunRecord) -> TypeGuard[RemoteJobClaim]:
    return "remote_host" in run and process_start_time(run["pid"]) != run["proc_start"]


def copy_gives_way(run: RunRecord) -> bool:
    """A live test copy whose process still runs, so a new step can stop it.

    A kept copy claim holds leftovers like any other kept claim and never gives way.
    """
    return run.get("copy") is True and process_start_time(run["pid"]) == run["proc_start"]


def claim_scope(worktree: str) -> ClaimScope:
    """The repository a claim covers: the one its worktree is in, else the whole Mac."""
    repository = offload.repository_identity(Path(worktree))
    if isinstance(repository, offload.KnownRepository):
        return RepositoryScope(repository.name)
    return WHOLE_MAC


def claim_path(paths: StatePaths, scope: ClaimScope) -> Path:
    if isinstance(scope, RepositoryScope):
        return paths.claims / f"{scope.name}.json"
    return paths.run


def scopes_overlap(first: ClaimScope, second: ClaimScope) -> bool:
    return isinstance(first, WholeMacScope) or isinstance(second, WholeMacScope) or first == second


def claim_files(paths: StatePaths) -> tuple[Path, ...]:
    try:
        repository_files = sorted(paths.claims.glob("*.json"))
    except OSError as error:
        raise ClaimStateUnreadable(paths.claims, error) from error
    return (paths.run, *repository_files)


def read_claims(paths: StatePaths) -> tuple[HeldClaim, ...]:
    """Every claim record, oldest first.

    `claims/<repo>.json` holds one repository's claim. `run.json` holds a claim
    that names no repository, or a record left by the machine-wide claim from
    before; its scope comes from its worktree, so an old record keeps its
    repository until its owner ends, and covers every repository when its
    worktree is in none.
    """
    held: list[HeldClaim] = []
    for path in claim_files(paths):
        try:
            record = read_run(path)
        except StateReadError as error:
            raise ClaimStateUnreadable(path, error) from error
        if isinstance(record, NoRun):
            continue
        scope = claim_scope(record["worktree"]) if path == paths.run else RepositoryScope(path.stem)
        held.append(HeldClaim(scope, path, record))
    return tuple(sorted(held, key=lambda claim: datetime.fromisoformat(claim.record["since"])))


def live_claims(paths: StatePaths) -> tuple[HeldClaim, ...]:
    """Read every claim and remove each one whose owner has ended."""
    live: list[HeldClaim] = []
    for claim in read_claims(paths):
        if run_is_live(claim.record):
            live.append(claim)
        else:
            claim.path.unlink(missing_ok=True)
    return tuple(live)


def message_script(environment_name: str, file_name: str) -> Path:
    configured = os.environ.get(environment_name)
    if configured is not None:
        return Path(configured).expanduser()
    return Path(__file__).resolve().parent.parent / "message" / file_name


def message_timeout_s() -> float:
    try:
        timeout = float(
            os.environ.get("MAC_TEST_MESSAGE_TIMEOUT_S", str(MESSAGE_TIMEOUT_S))
        )
    except ValueError:
        return MESSAGE_TIMEOUT_S
    return (
        timeout
        if math.isfinite(timeout) and timeout > 0
        else MESSAGE_TIMEOUT_S
    )


def delivery_target(delivery: MessageDelivery) -> str:
    if isinstance(delivery, HolderQueue):
        return delivery.name
    sessions_script = message_script("MAC_TEST_SESSIONS", "sessions.py")
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(sessions_script),
                "socket",
                f"session:{delivery.name}",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=message_timeout_s(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return delivery.holder
    socket = result.stdout.strip()
    if result.returncode == 0 and socket:
        return f"uds:{socket}"
    return delivery.holder


def send_message(
    target: str,
    summary: str,
    key: str,
    repeat_minutes: float,
    text: str,
) -> bool:
    send_script = message_script("MAC_TEST_SEND", "send.py")
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(send_script),
                "--to",
                target,
                "--from",
                "mac-test",
                "--summary",
                summary,
                "--key",
                key,
                "--repeat-minutes",
                f"{repeat_minutes:g}",
                "--text",
                text,
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=message_timeout_s(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode in (0, 1)


def run_gh(config: MacTestConfig, *arguments: str) -> GhResult:
    try:
        result = subprocess.run(
            ["gh", *arguments],
            capture_output=True,
            text=True,
            check=False,
            timeout=config.gh_timeout_s,
        )
    except subprocess.TimeoutExpired:
        return GhFailure(f"gh timed out after {config.gh_timeout_s:g} s")
    except OSError as error:
        return GhFailure(str(error))
    if result.returncode == 0:
        return GhAnswer(result.stdout)
    printed = [
        line
        for line in (*result.stdout.splitlines(), *result.stderr.splitlines())
        if line.strip()
    ]
    return GhFailure(printed[-1] if printed else f"gh exited {result.returncode}")


def json_items(answer: GhAnswer) -> list[object] | GhFailure:
    try:
        decoded = cast(object, json.loads(answer.stdout))
    except json.JSONDecodeError:
        return GhFailure("gh returned invalid JSON")
    if not isinstance(decoded, list):
        return GhFailure("gh returned invalid JSON")
    return cast(list[object], decoded)


def jobs_for_run(config: MacTestConfig, database_id: int) -> list[object] | GhFailure:
    answer = run_gh(
        config,
        "run",
        "view",
        str(database_id),
        "--repo",
        config.ci_repo,
        "--json",
        "jobs",
    )
    if isinstance(answer, GhFailure):
        return answer
    try:
        decoded = cast(object, json.loads(answer.stdout))
    except json.JSONDecodeError:
        return GhFailure("gh returned invalid JSON")
    if not isinstance(decoded, dict):
        return GhFailure("gh returned invalid JSON")
    jobs = cast(dict[str, object], decoded).get("jobs")
    if not isinstance(jobs, list):
        return GhFailure("gh returned invalid JSON")
    return cast(list[object], jobs)


def run_ids(answer: GhAnswer) -> list[int] | GhFailure:
    items = json_items(answer)
    if isinstance(items, GhFailure):
        return items
    ids: list[int] = []
    for item in items:
        if not isinstance(item, dict):
            return GhFailure("gh returned invalid JSON")
        database_id = cast(dict[str, object], item).get("databaseId")
        if type(database_id) is not int:
            return GhFailure("gh returned invalid JSON")
        ids.append(database_id)
    return ids


def ci_activity(config: MacTestConfig) -> CiActivity:
    ids: list[int] = []
    for status in ("queued", "in_progress"):
        answer = run_gh(
            config,
            "run",
            "list",
            "--repo",
            config.ci_repo,
            "--workflow",
            config.ci_workflow,
            "--status",
            status,
            "--json",
            "databaseId",
        )
        if isinstance(answer, GhFailure):
            return CiUnknown(answer.line)
        status_ids = run_ids(answer)
        if isinstance(status_ids, GhFailure):
            return CiUnknown(status_ids.line)
        ids.extend(status_ids)
    for database_id in dict.fromkeys(ids):
        jobs = jobs_for_run(config, database_id)
        if isinstance(jobs, GhFailure):
            return CiUnknown(jobs.line)
        mac_job_completed = False
        for raw_job in jobs:
            if not isinstance(raw_job, dict):
                return CiUnknown("gh returned invalid JSON")
            job = cast(dict[str, object], raw_job)
            if job.get("name") != config.ci_job:
                continue
            if job.get("status") != "completed":
                return CI_BUSY
            mac_job_completed = True
        if not mac_job_completed:
            return CI_BUSY
    return CI_IDLE


def switch_ci_off(config: MacTestConfig) -> CiSwitchOffResult:
    current = run_gh(
        config,
        "variable",
        "get",
        config.ci_variable,
        "--repo",
        config.ci_repo,
    )
    if isinstance(current, GhFailure):
        return CiSwitchReadFailed(current.line)
    if current.stdout.strip() != "true":
        return "off_before"
    changed = run_gh(
        config,
        "variable",
        "set",
        config.ci_variable,
        "--body",
        "false",
        "--repo",
        config.ci_repo,
    )
    if isinstance(changed, GhFailure):
        return CiSwitchWriteUnconfirmed(changed.line)
    return "off_by_this_block"


def switch_ci_back(config: MacTestConfig) -> GhResult:
    return run_gh(
        config,
        "variable",
        "set",
        config.ci_variable,
        "--body",
        "true",
        "--repo",
        config.ci_repo,
    )


def ci_needs_restore(ci: CiSwitch) -> bool:
    # An unanswered write may have worked. Restoring can also undo a later manual switch-off.
    return ci in ("off_by_this_block", "off_unconfirmed")


def ci_may_still_be_on(ci: CiSwitch) -> bool:
    return ci in ("still_on", "off_unconfirmed")


def replace_ci(
    block: PendingMacBlock | ActiveMacBlock,
    ci: CiSwitch,
) -> PendingMacBlock | ActiveMacBlock:
    if isinstance(block, PendingMacBlock):
        return PendingMacBlock(
            block.holder,
            block.reason,
            block.since,
            block.expires,
            block.delivery,
            block.showrunner,
            ci,
            block.waiting_on,
        )
    return ActiveMacBlock(
        block.holder,
        block.reason,
        block.since,
        block.expires,
        block.delivery,
        block.showrunner,
        ci,
        block.free_message,
    )


def send_mac_free(block: ActiveMacBlock, owed: FreeMessageOwed) -> bool:
    text = (
        f"Message from mac-test: the Mac is free. {owed.what} ended at "
        + f"{message_time(owed.ended)}; your block ({block.reason}) is active, and "
        + "nothing is built or tested there until you run unblock."
    )
    return send_message(
        delivery_target(block.delivery),
        "Mac is free",
        f"mac-free-{timestamp(block.since)}",
        1440,
        text,
    )


def skipped_ci_runs(config: MacTestConfig, since: datetime) -> str:
    if not config.ci_repo:
        return ""
    answer = run_gh(
        config,
        "run",
        "list",
        "--repo",
        config.ci_repo,
        "--workflow",
        config.ci_workflow,
        "--created",
        f">={timestamp(since.replace(microsecond=0))}",
        "--limit",
        "200",
        "--json",
        "databaseId,headBranch,headSha",
    )
    if isinstance(answer, GhFailure):
        return f"Could not list the CI runs that skipped the macOS job: {answer.line}"
    items = json_items(answer)
    if isinstance(items, GhFailure):
        return f"Could not list the CI runs that skipped the macOS job: {items.line}"
    branch_runs: dict[str, tuple[int, str]] = {}
    for item in items:
        if not isinstance(item, dict):
            return "Could not list the CI runs that skipped the macOS job: gh returned invalid JSON"
        run = cast(dict[str, object], item)
        database_id = run.get("databaseId")
        branch = run.get("headBranch")
        sha = run.get("headSha")
        if (
            type(database_id) is not int
            or not isinstance(branch, str)
            or not isinstance(sha, str)
        ):
            return "Could not list the CI runs that skipped the macOS job: gh returned invalid JSON"
        jobs = jobs_for_run(config, database_id)
        if isinstance(jobs, GhFailure):
            return f"Could not list the CI runs that skipped the macOS job: {jobs.line}"
        skipped = False
        for raw_job in jobs:
            if not isinstance(raw_job, dict):
                return "Could not list the CI runs that skipped the macOS job: gh returned invalid JSON"
            job = cast(dict[str, object], raw_job)
            if (
                job.get("name") == config.ci_job
                and job.get("conclusion") == "skipped"
            ):
                skipped = True
                break
        if skipped:
            prior = branch_runs.get(branch)
            branch_runs[branch] = (
                (1, sha) if prior is None else (prior[0] + 1, prior[1])
            )
    if branch_runs:
        lines = ["CI runs that skipped the macOS job under this block:"]
        lines.extend(
            f"  {branch}: {count} runs, newest {sha[:9]}"
            for branch, (count, sha) in sorted(branch_runs.items())
        )
        lines.append(
            "Run CI again on each branch's newest commit to make up its macOS check: "
            + f"gh workflow run {config.ci_workflow} --repo {config.ci_repo} --ref <branch>"
        )
    else:
        lines = ["No CI run skipped the macOS job under this block."]
    if len(items) == 200:
        lines.append(
            "Only the newest 200 runs were looked at; older ones under this block are not listed."
        )
    return "\n".join(lines)


def expiry_text(block: PendingMacBlock | ActiveMacBlock, skipped: str) -> str:
    text = (
        f"Message from mac-test: the Mac block by {block.holder} ({block.reason}) "
        + f"reached its time limit at {block_time(block.expires)} and lifted by itself."
    )
    if ci_needs_restore(block.ci):
        text += " CI may use the Mac again."
    if skipped:
        text += f"\n{skipped}"
    return text


def send_to_block_recipients(
    block: PendingMacBlock | ActiveMacBlock,
    summary: str,
    key: str,
    repeat_minutes: float,
    text: str,
) -> None:
    _ = send_message(
        delivery_target(block.delivery), summary, key, repeat_minutes, text
    )
    if isinstance(block.showrunner, NamedShowrunner):
        _ = send_message(
            block.showrunner.name, summary, key, repeat_minutes, text
        )


def expire_block(
    paths: StatePaths,
    config: MacTestConfig,
    claims: tuple[HeldClaim, ...],
    block: PendingMacBlock | ActiveMacBlock,
) -> SettleOutcome:
    if not config.ci_repo:
        block = replace_ci(block, "not_configured")
    elif ci_needs_restore(block.ci):
        restored = switch_ci_back(config)
        if isinstance(restored, GhFailure):
            text = (
                f"Message from mac-test: the Mac block by {block.holder} is past "
                + "its time limit but still in place: CI's Mac switch could not be "
                + f"turned back on ({restored.line}). Run github-warmup, then unblock."
            )
            send_to_block_recipients(
                block,
                "Mac block could not lift",
                f"mac-block-stuck-{timestamp(block.since)}",
                60,
                text,
            )
            return SettleOutcome(
                claims,
                block,
                ExpiredBlockLiftFailed(restored.line),
                NO_PROBLEM,
                NO_PROBLEM,
            )
    with locked(paths.lock):
        at = utc_now()
        try:
            paths.block.unlink()
        except FileNotFoundError:
            pass
        else:
            record_hold_event("ended", block, at)
    skipped = (
        skipped_ci_runs(config, block.since)
        if block.ci != "not_configured"
        else ""
    )
    send_to_block_recipients(
        block,
        "Mac block lifted",
        f"mac-block-expired-{timestamp(block.since)}",
        1440,
        expiry_text(block, skipped),
    )
    return SettleOutcome(
        claims,
        NO_MAC_BLOCK,
        NO_EXPIRED_BLOCK_LIFT_FAILURE,
        NO_PROBLEM,
        NO_PROBLEM,
    )


def warning_text(block: PendingMacBlock | ActiveMacBlock) -> str:
    return (
        f"Message from mac-test: your Mac block ({block.reason}) lifts by itself at "
        + f"{block_time(block.expires)}. Run block again to keep it."
    )


def watcher_is_running(paths: StatePaths) -> bool:
    with lock_if_available(paths.watch_lock) as acquired:
        return not acquired


def start_watcher(paths: StatePaths) -> None:
    if watcher_is_running(paths):
        return
    environment = [
        f"{name}={value}"
        for name, value in sorted(os.environ.items())
        if name.startswith("MAC_TEST_")
    ]
    command = [
        "systemd-run",
        "--user",
        "--collect",
        "--quiet",
        "--no-block",
        "--unit",
        f"mac-test-watch-{os.getpid()}",
        "--",
        "/usr/bin/env",
        f"HOME={os.environ.get('HOME', '')}",
        f"PATH={os.environ.get('PATH', '')}",
        *environment,
        sys.executable,
        str(Path(__file__).resolve()),
        "watch",
    ]
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        failed = result.returncode != 0
    except OSError:
        failed = True
    if failed:
        print(
            "warning: the Mac block watcher could not be started",
            file=sys.stderr,
        )


def start_claim_watcher(paths: StatePaths) -> None:
    with lock_if_available(paths.claim_watch_lock) as acquired:
        if not acquired:
            return
    environment = [
        f"{name}={value}"
        for name, value in sorted(os.environ.items())
        if name.startswith("MAC_TEST_")
    ]
    command = [
        "systemd-run", "--user", "--collect", "--quiet", "--no-block",
        "--unit", f"mac-test-claim-watch-{os.getpid()}", "--", "/usr/bin/env",
        f"HOME={os.environ.get('HOME', '')}",
        f"PATH={os.environ.get('PATH', '')}",
        *environment, sys.executable, str(Path(__file__).resolve()), "watch-claim",
    ]
    try:
        result = subprocess.run(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
        failed = result.returncode != 0
    except OSError:
        failed = True
    if failed:
        print("warning: the Mac claim watcher could not be started", file=sys.stderr)


def mac_activity_script() -> str:
    """Count the user's mirror work and build executables without sending signals."""
    return offload.mirror_selector_script(offload.ALL_MIRRORS) + r'''
mirror_pids=$(select_leftovers) || exit 1
process_records=$(ps -axww -o pid=,uid=,comm=) || exit 1
activity_user=$(id -u) || exit 1
executable_pids=$(printf '%s\n' "$process_records" | awk -v user="$activity_user" '
    $2 == user {
        executable = $0
        sub(/^[[:space:]]*[0-9]+[[:space:]]+[0-9]+[[:space:]]+/, "", executable)
        name = executable
        sub(/^.*\//, "", name)
        if (name == "cargo" || name == "rustc" || name == "cargo-nextest" || executable ~ /\/target\//)
            print $1
    }') || exit 1
active_count=$(printf '%s\n%s\n' "$mirror_pids" "$executable_pids" | awk '/^[0-9]+$/ && !seen[$0]++ { count++ } END { print count+0 }') || exit 1
printf 'active=%s\nrc=0\n' "$active_count"
'''


def check_mac_activity(config: MacTestConfig, host: str) -> MacActivity:
    try:
        result = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o",
             f"ConnectTimeout={config.connect_timeout_s:g}", host, mac_activity_script()],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, check=False, timeout=10,
        )
    except subprocess.TimeoutExpired:
        return ActivityCheckFailed("SSH activity check timed out after 10 s")
    except OSError as error:
        return ActivityCheckFailed(f"SSH activity check could not start: {error}")
    except UnicodeError:
        return ActivityCheckFailed("the activity check answer could not be read")
    if result.returncode != 0:
        reason = " ".join(result.stderr.split()) or f"SSH exited {result.returncode}"
        return ActivityCheckFailed(reason)
    lines = result.stdout.splitlines()
    if len(lines) != 2 or lines[1] != "rc=0" or not lines[0].startswith("active="):
        return ActivityCheckFailed("the activity check answer could not be read")
    count_text = lines[0].removeprefix("active=")
    if not count_text.isascii() or not count_text.isdecimal():
        return ActivityCheckFailed("the activity check answer could not be read")
    try:
        count = int(count_text)
    except ValueError:
        return ActivityCheckFailed("the activity check answer could not be read")
    return MacActive(count) if count else MacIdle()


def claim_idle_clause(run: RunRecord) -> str:
    if "idle_since" not in run:
        return ""
    return f", idle since {local_time(run['idle_since'], include_zone=False)}"


def claim_host(run: RunRecord, config: MacTestConfig) -> str:
    return run["remote_host"] if "remote_host" in run else config.host


def claim_watch_pass(paths: StatePaths, config: MacTestConfig) -> bool:
    """Check every live claim once; return whether another pass may still be needed.

    One activity check serves every claim on a host. It counts all of the
    user's Mac work, since a build process cannot be traced to a repository,
    so a claim is idle only while the Mac runs nothing at all.
    """
    with locked(paths.lock):
        claims = tuple(claim for claim in read_claims(paths) if run_is_live(claim.record))
    if not claims:
        return False
    activity: dict[str, MacActivity] = {}
    for claim in claims:
        host = claim_host(claim.record, config)
        if host not in activity:
            activity[host] = check_mac_activity(config, host)
    now = utc_now()
    watched = False
    for claim in claims:
        if watch_claim(paths, config, claim, activity[claim_host(claim.record, config)], now):
            watched = True
    return watched


def watch_claim(
    paths: StatePaths, config: MacTestConfig, claim: HeldClaim, activity: MacActivity, now: datetime,
) -> bool:
    """Apply one activity answer to one claim; return whether it is still watched."""
    run = claim.record
    with locked(paths.lock):
        current = read_run(claim.path)
        if isinstance(current, NoRun):
            return False
        if (current["pid"], current["proc_start"], current["since"]) != (
            run["pid"], run["proc_start"], run["since"],
        ):
            return True
        if not run_is_live(current):
            return False
        if isinstance(activity, ActivityCheckFailed):
            print(f"Mac claim activity check failed: {activity.reason}", flush=True)
            return True
        if isinstance(activity, MacActive):
            if "idle_since" in current:
                del current["idle_since"]
                write_json(claim.path, current)
            return True
        if "idle_since" not in current:
            current["idle_since"] = timestamp(now)
            write_json(claim.path, current)
            return True
        idle_since = aware_timestamp(current["idle_since"], claim.path, "idle_since")
        if now - idle_since < timedelta(minutes=config.claim_idle_minutes):
            return True
        block_present = not isinstance(read_block(paths.block), NoMacBlock)
        claim.path.unlink(missing_ok=True)
    if block_present:
        start_watcher(paths)
    released_at = local_time(timestamp(now), include_zone=False)
    quoted_what = "'" + current["what"].replace("'", "'\"'\"'") + "'"
    quoted_worktree = "'" + current["worktree"].replace("'", "'\"'\"'") + "'"
    remote_argument = ""
    if "remote_host" in current:
        quoted_host = "'" + current["remote_host"].replace("'", "'\"'\"'") + "'"
        remote_argument = f" --remote {quoted_host}"
    text = (
        f'Your Mac claim "{current["what"]}" '
        + f"(since {local_time(current['since'], include_zone=False)}) was released at {released_at}: "
        + f"nothing ran on the Mac for {config.claim_idle_minutes:g} minutes, so tests waiting for it can run. "
        + "Claim it again before your next Mac command: "
        + "python3 ~/.claude/scripts/mac_test/mac_test.py claim --pid $$ "
        + f"--what {quoted_what} --worktree {quoted_worktree}{remote_argument} (see /build:mac)."
    )
    print(text, flush=True)
    if "holder_session" in current:
        _ = send_message(
            f"session:{current['holder_session']}", "Mac claim released",
            f"mac-claim-idle-{current['pid']}-{current['since']}", 0, text,
        )
    return False


def command_watch_claim(paths: StatePaths, config: MacTestConfig) -> int:
    with ExitStack() as watcher_lock:
        acquired = watcher_lock.enter_context(lock_if_available(paths.claim_watch_lock))
        if not acquired:
            return 0
        while True:
            try:
                if not claim_watch_pass(paths, config):
                    with locked(paths.lock):
                        if not any(run_is_live(claim.record) for claim in read_claims(paths)):
                            # A new claimant must see the watcher lock released.
                            watcher_lock.close()
                            return 0
            except (OSError, StateReadError) as error:
                print(f"Mac claim state unreadable: {error}", flush=True)
            time.sleep(config.claim_poll_seconds)


def pending_with_wait(
    block: PendingMacBlock,
    waiting_on: WaitingOn,
) -> PendingMacBlock:
    return PendingMacBlock(
        block.holder,
        block.reason,
        block.since,
        block.expires,
        block.delivery,
        block.showrunner,
        block.ci,
        waiting_on,
    )


def activate_block(
    block: PendingMacBlock,
    now: datetime,
    message_needed: bool,
) -> ActiveMacBlock:
    ended_work = (
        block.waiting_on.what
        if isinstance(block.waiting_on, WaitingOnLocalRun)
        else "CI's hold on the Mac"
    )
    free_message: FreeMessage = (
        FreeMessageOwed(ended_work, now)
        if message_needed
        else FREE_MESSAGE_NOT_NEEDED
    )
    return ActiveMacBlock(
        block.holder,
        block.reason,
        block.since,
        block.expires,
        block.delivery,
        block.showrunner,
        block.ci,
        free_message,
    )


def mark_free_message_sent(block: ActiveMacBlock) -> ActiveMacBlock:
    return ActiveMacBlock(
        block.holder,
        block.reason,
        block.since,
        block.expires,
        block.delivery,
        block.showrunner,
        block.ci,
        FREE_MESSAGE_SENT,
    )


def full_settle(
    paths: StatePaths,
    config: MacTestConfig,
    *,
    activation_message_needed: bool = True,
    watcher_holds_lock: bool = False,
) -> SettleOutcome:
    with locked(paths.lock):
        claims = live_claims(paths)
        block = read_block(paths.block)
    if isinstance(block, NoMacBlock):
        return SettleOutcome(
            claims,
            block,
            NO_EXPIRED_BLOCK_LIFT_FAILURE,
            NO_PROBLEM,
            NO_PROBLEM,
        )
    now = utc_now()
    if now >= block.expires:
        outcome = expire_block(paths, config, claims, block)
        if (
            not watcher_holds_lock
            and not isinstance(outcome.block, NoMacBlock)
        ):
            start_watcher(paths)
        return outcome

    ci_problem: Problem = NO_PROBLEM
    waiting_problem: Problem = NO_PROBLEM
    if ci_may_still_be_on(block.ci):
        if not config.ci_repo:
            block = replace_ci(block, "not_configured")
            with locked(paths.lock):
                write_block(paths.block, block)
        else:
            prior_ci = block.ci
            switched = switch_ci_off(config)
            if isinstance(switched, CiSwitchReadFailed):
                ci_problem = ProblemLine(switched.line)
            elif isinstance(switched, CiSwitchWriteUnconfirmed):
                ci_problem = ProblemLine(switched.line)
                block = replace_ci(block, "off_unconfirmed")
                with locked(paths.lock):
                    write_block(paths.block, block)
            else:
                settled_ci: CiSwitch = (
                    "off_by_this_block"
                    if prior_ci == "off_unconfirmed" and switched == "off_before"
                    else switched
                )
                block = replace_ci(block, settled_ci)
                with locked(paths.lock):
                    write_block(paths.block, block)

    if isinstance(block, PendingMacBlock):
        with locked(paths.lock):
            claims = live_claims(paths)
        if claims:
            block = pending_with_wait(block, WaitingOnLocalRun(claims[0].record["what"]))
            with locked(paths.lock):
                write_block(paths.block, block)
        elif config.ci_repo:
            activity = ci_activity(config)
            if isinstance(activity, CiBusy):
                block = pending_with_wait(block, WAITING_ON_CI_JOB)
                with locked(paths.lock):
                    write_block(paths.block, block)
            elif isinstance(activity, CiUnknown):
                waiting_problem = ProblemLine(activity.line)
                block = pending_with_wait(block, WAITING_ON_UNKNOWN_CI)
                with locked(paths.lock):
                    write_block(paths.block, block)
            else:
                block = activate_block(block, now, activation_message_needed)
                with locked(paths.lock):
                    write_block(paths.block, block)
        else:
            block = activate_block(block, now, activation_message_needed)
            with locked(paths.lock):
                write_block(paths.block, block)

    if (
        isinstance(block, ActiveMacBlock)
        and isinstance(block.free_message, FreeMessageOwed)
        and send_mac_free(block, block.free_message)
    ):
        block = mark_free_message_sent(block)
        with locked(paths.lock):
            write_block(paths.block, block)

    if block.expires - now <= timedelta(minutes=config.block_warn_minutes):
        _ = send_message(
            delivery_target(block.delivery),
            "Mac block nearing its time limit",
            f"mac-block-warn-{timestamp(block.expires)}",
            1440,
            warning_text(block),
        )
    if not watcher_holds_lock:
        start_watcher(paths)
    return SettleOutcome(
        claims,
        block,
        NO_EXPIRED_BLOCK_LIFT_FAILURE,
        ci_problem,
        waiting_problem,
    )


def block_duration(raw: str, config: MacTestConfig) -> BlockDuration:
    if raw == "":
        value = config.block_hours
    else:
        try:
            value = float(raw)
        except ValueError:
            return INVALID_BLOCK_DURATION
    if (
        not math.isfinite(value)
        or value <= 0
        or value > config.block_max_hours
    ):
        return INVALID_BLOCK_DURATION
    return ValidBlockDuration(value)


def requested_delivery(holder: str) -> MessageDelivery:
    session = os.environ.get("CLAUDE_CODE_SESSION_ID")
    return (
        RecordedSession(session, holder)
        if session is not None
        else HolderQueue(holder)
    )


def requested_showrunner(name: str) -> Showrunner:
    return NamedShowrunner(name) if name else NO_SHOWRUNNER


def take_over_kept_claim(args: CliArguments, claim: HeldClaim, kept: RemoteJobClaim) -> ClaimAttempt:
    proc_start = process_start_time(args.pid)
    if proc_start is None:
        print(f"cannot claim: process {args.pid} is not running", file=sys.stderr)
        return ClaimFinished(1)
    kept["pid"] = args.pid
    kept["proc_start"] = proc_start
    if args.copy:
        kept["copy"] = True
    else:
        _ = kept.pop("copy", None)
    write_json(claim.path, kept)
    print(f"leftovers: {kept['remote_host']}")
    return ClaimFinished(13)


def claim_once(args: CliArguments, paths: StatePaths, scope: ClaimScope) -> ClaimAttempt:
    """One claim attempt under state.lock: take over, refuse, or write the claim."""
    try:
        claims = live_claims(paths)
    except ClaimStateUnreadable as error:
        print(f"state unreadable: {error.path}")
        return ClaimFinished(12)
    overlapping = tuple(claim for claim in claims if scopes_overlap(claim.scope, scope))
    if isinstance(args.remote, str):
        for claim in overlapping:
            record = claim.record
            if claim.scope == scope and run_is_kept(record):
                return take_over_kept_claim(args, claim, record)
    try:
        block = read_claim_block(paths)
    except (OSError, StateReadError):
        print(f"state unreadable: {paths.block}")
        return ClaimFinished(12)
    if not isinstance(block, NoMacBlock):
        print(f"blocked by {block.holder}: {block.reason}")
        return ClaimFinished(10)
    if overlapping:
        # A test copy gives way to any new step; a copy never replaces a copy.
        if not args.copy and all(copy_gives_way(claim.record) for claim in overlapping):
            return ClaimHeldByCopy(tuple(claim.record for claim in overlapping))
        return ClaimBusy(overlapping[0].record)
    proc_start = process_start_time(args.pid)
    if proc_start is None:
        print(f"cannot claim: process {args.pid} is not running", file=sys.stderr)
        return ClaimFinished(1)
    claimed: RunRecord = {
        "pid": args.pid,
        "proc_start": proc_start,
        "what": args.what,
        "worktree": args.worktree,
        "since": timestamp(utc_now()),
    }
    if args.copy:
        claimed["copy"] = True
    if isinstance(args.remote, str):
        remote_claim: RemoteJobClaim = {**claimed, "remote_host": args.remote}
        claimed = remote_claim
    session = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if session:
        claimed["holder_session"] = session
    else:
        owner = build_hold.owning_session()
        if isinstance(owner, build_hold.OwningSession):
            claimed["holder_session"] = owner.session_id
    path = claim_path(paths, scope)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, claimed)
    print("claimed")
    return ClaimFinished(0)


def copy_yield_seconds() -> float:
    try:
        seconds = float(os.environ.get("MAC_TEST_COPY_YIELD_S", "60"))
    except ValueError:
        return 60.0
    return seconds if math.isfinite(seconds) and seconds > 0 else 60.0


def stop_copy(holder: RunRecord) -> None:
    """SIGTERM a copy claim's runner, never a process that reused its pid."""
    if process_start_time(holder["pid"]) != holder["proc_start"]:
        return
    try:
        os.kill(holder["pid"], signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass


def command_claim(args: CliArguments, paths: StatePaths) -> int:
    scope = claim_scope(args.worktree)
    deadline = time.monotonic() + max(args.wait, 0.0)
    yield_deadline = 0.0
    stopped: set[tuple[int, str]] = set()
    while True:
        with locked(paths.lock):
            attempt = claim_once(args, paths, scope)
        if isinstance(attempt, ClaimFinished):
            if attempt.status in (0, 13):
                start_claim_watcher(paths)
            return attempt.status
        if isinstance(attempt, ClaimHeldByCopy):
            # Signal outside state.lock, then wait for each copy's runner to
            # clean up and release; the lock is never held while waiting.
            if not stopped:
                yield_deadline = time.monotonic() + copy_yield_seconds()
            for holder in attempt.holders:
                if (holder["pid"], holder["proc_start"]) not in stopped:
                    stopped.add((holder["pid"], holder["proc_start"]))
                    stop_copy(holder)
            if time.monotonic() < yield_deadline:
                time.sleep(0.1)
                continue
            busy_run = attempt.holders[0]
            print(f"busy: {busy_run['what']} since {local_time(busy_run['since'], include_zone=False)}")
            return 11
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            busy_run = attempt.holder
            if run_is_kept(busy_run):
                print(f"busy: leftovers of {busy_run['what']} on {busy_run['remote_host']} since "
                      + f"{local_time(busy_run['since'], include_zone=False)}")
                return 11
            print(
                f"busy: {busy_run['what']} since "
                + f"{local_time(busy_run['since'], include_zone=False)}"
            )
            return 11
        time.sleep(min(2.0, remaining))


def command_release(args: CliArguments, paths: StatePaths) -> int:
    with locked(paths.lock):
        # Each file is read alone, so another repository's damaged record
        # cannot keep this owner's claim in place.
        for path in claim_files(paths):
            try:
                record = read_run(path)
            except StateReadError:
                continue
            if isinstance(record, dict) and record["pid"] == args.pid:
                path.unlink(missing_ok=True)
        block_present = not isinstance(read_block(paths.block), NoMacBlock)
    if block_present:
        start_watcher(paths)
    return 0


def free_now(
    paths: StatePaths, settings: Path | offload.DefaultMacSettings, repo_root: Path, copy: bool = False,
) -> FreeNowAnswer:
    """Whether a claim for repo_root's repository made now could start a Mac run.

    Read from local records alone, and cheap enough for LINT_QUEUE_LEAVE_HOOK,
    which runs it on every held memory poll: no SSH, no gh and no lock. CI is
    not read, since a claim does not consult it; the claim itself still decides
    under its lock. Another repository's claim leaves the answer free. A copy
    claim gives way to a new test, so it holds the scope only for another copy
    (`copy`).
    """
    if not offload.read_config(settings).enabled:
        return "off"
    if isinstance(offload.read_backoff(time.time()), offload.ActiveBackoff):
        return "backoff"
    scope = claim_scope(str(repo_root))
    try:
        if not isinstance(read_claim_block(paths), NoMacBlock):
            return "blocked"
        claims = read_claims(paths)
    except (OSError, StateReadError):
        return "state"
    held = any(
        scopes_overlap(claim.scope, scope)
        and run_is_live(claim.record)
        and (copy or not copy_gives_way(claim.record))
        for claim in claims
    )
    return "busy" if held else "free"


def command_free_now(args: CliArguments, paths: StatePaths) -> int:
    settings = Path(args.settings) if args.settings else offload.DEFAULT_MAC_SETTINGS
    answer = free_now(paths, settings, Path(args.repo_root), args.copy)
    print(answer)
    return 0 if answer == "free" else 1


def block_state_line(
    block: PendingMacBlock | ActiveMacBlock,
    waiting_problem: Problem,
) -> str:
    if isinstance(block, ActiveMacBlock):
        return (
            f"Mac blocked for {block.holder}: nothing is running there, "
            + "it is free now."
        )
    waiting = block.waiting_on
    if isinstance(waiting, WaitingOnLocalRun):
        return (
            f"Block pending for {block.holder}: {waiting.what} is running on the Mac. "
            + "Nothing new starts there, and you get a message when it ends."
        )
    if isinstance(waiting, WaitingOnCiJob):
        return (
            f"Block pending for {block.holder}: a CI run may still use the Mac. "
            + "Nothing new starts there, and you get a message when CI no longer "
            + "needs the Mac."
        )
    line = (
        waiting_problem.line
        if isinstance(waiting_problem, ProblemLine)
        else "unknown error"
    )
    return (
        f"Block pending for {block.holder}: CI could not be checked ({line}). "
        + "Nothing new starts there, and you get a message once CI's Mac job is "
        + "known to be idle."
    )


def command_block(
    args: CliArguments,
    paths: StatePaths,
    config: MacTestConfig,
) -> int:
    duration = block_duration(args.hours, config)
    if isinstance(duration, InvalidBlockDuration):
        print(
            "a block lasts more than 0 and at most "
            + f"{config.block_max_hours:g} hours"
        )
        return 2
    with locked(paths.control_lock):
        with locked(paths.lock):
            claims = live_claims(paths)
            existing = read_block(paths.block)
            if (
                not isinstance(existing, NoMacBlock)
                and existing.holder != args.holder
            ):
                print(f"already blocked by {existing.holder}")
                return 1
            now = utc_now()
            expires = now + timedelta(hours=duration.hours)
            if isinstance(existing, NoMacBlock):
                if claims:
                    block: PendingMacBlock | ActiveMacBlock = PendingMacBlock(
                        args.holder,
                        args.reason,
                        now,
                        expires,
                        requested_delivery(args.holder),
                        requested_showrunner(args.showrunner),
                        "still_on",
                        WaitingOnLocalRun(claims[0].record["what"]),
                    )
                elif config.ci_repo:
                    block = PendingMacBlock(
                        args.holder,
                        args.reason,
                        now,
                        expires,
                        requested_delivery(args.holder),
                        requested_showrunner(args.showrunner),
                        "still_on",
                        WAITING_ON_UNKNOWN_CI,
                    )
                else:
                    block = ActiveMacBlock(
                        args.holder,
                        args.reason,
                        now,
                        expires,
                        requested_delivery(args.holder),
                        requested_showrunner(args.showrunner),
                        "still_on",
                        FREE_MESSAGE_NOT_NEEDED,
                    )
                new_block = True
            elif isinstance(existing, PendingMacBlock):
                block = PendingMacBlock(
                    existing.holder,
                    args.reason,
                    existing.since,
                    expires,
                    requested_delivery(args.holder),
                    requested_showrunner(args.showrunner),
                    existing.ci,
                    existing.waiting_on,
                )
                new_block = False
            else:
                block = ActiveMacBlock(
                    existing.holder,
                    args.reason,
                    existing.since,
                    expires,
                    requested_delivery(args.holder),
                    requested_showrunner(args.showrunner),
                    existing.ci,
                    existing.free_message,
                )
                new_block = False
            write_block(paths.block, block)
            if new_block:
                record_hold_event("began", block, now)
        outcome = full_settle(
            paths,
            config,
            activation_message_needed=not new_block,
        )
        if isinstance(outcome.block, NoMacBlock):
            return 0
        print(block_state_line(outcome.block, outcome.waiting_problem))
        if ci_may_still_be_on(outcome.block.ci):
            lift_failure = outcome.expired_block_lift_failure
            line = (
                lift_failure.gh_error_line
                if isinstance(lift_failure, ExpiredBlockLiftFailed)
                else (
                    outcome.ci_problem.line
                    if isinstance(outcome.ci_problem, ProblemLine)
                    else "unknown error"
                )
            )
            print(
                f"CI can still use the Mac: {line}. Run github-warmup, then block again."
            )
        print(
            f"It lifts by itself at {block_time(outcome.block.expires)} "
            + "unless you run block again."
        )
        return 2 if ci_may_still_be_on(outcome.block.ci) else 0


def command_unblock(
    args: CliArguments,
    paths: StatePaths,
    config: MacTestConfig,
) -> int:
    with locked(paths.control_lock):
        with locked(paths.lock):
            block = read_block(paths.block)
        if isinstance(block, NoMacBlock):
            print("no block")
            return 0
        if block.holder != args.holder:
            print(f"blocked by {block.holder}")
            return 1
        if not config.ci_repo:
            block = replace_ci(block, "not_configured")
        elif ci_needs_restore(block.ci):
            restored = switch_ci_back(config)
            if isinstance(restored, GhFailure):
                print(
                    "Mac still blocked: CI's Mac switch could not be turned back "
                    + f"on ({restored.line}). Run github-warmup, then unblock again."
                )
                return 2
        with locked(paths.lock):
            at = utc_now()
            try:
                paths.block.unlink()
            except FileNotFoundError:
                pass
            else:
                record_hold_event("ended", block, at)
        if ci_needs_restore(block.ci):
            print("Mac unblocked; CI may use the Mac again.")
        elif block.ci == "off_before":
            print("Mac unblocked; CI's Mac switch was already off and stays off.")
        else:
            print("Mac unblocked.")
        if block.ci != "not_configured":
            print(skipped_ci_runs(config, block.since))
        return 0


def pending_wait_text(waiting: WaitingOn) -> str:
    if isinstance(waiting, WaitingOnLocalRun):
        return "a test is running"
    if isinstance(waiting, WaitingOnCiJob):
        return "a CI run may still use the Mac"
    return "CI could not be checked"


def print_switch_with_block(
    block: PendingMacBlock | ActiveMacBlock,
    problem: Problem,
) -> None:
    if block.ci == "off_by_this_block":
        print("CI's Mac switch: off (this block)")
    elif block.ci == "off_before":
        print("CI's Mac switch: off (set elsewhere)")
    elif block.ci == "not_configured":
        print("CI's Mac switch: not configured")
    else:
        line = problem.line if isinstance(problem, ProblemLine) else "unknown error"
        print(f"CI's Mac switch: still on ({line})")


def print_switch_without_block(config: MacTestConfig) -> None:
    if not config.ci_repo:
        print("CI's Mac switch: not configured")
        return
    answer = run_gh(
        config,
        "variable",
        "get",
        config.ci_variable,
        "--repo",
        config.ci_repo,
    )
    if isinstance(answer, GhFailure):
        print(f"CI's Mac switch: unknown ({answer.line})")
    elif answer.stdout.strip() == "true":
        print("CI's Mac switch: on")
    else:
        print("CI's Mac switch: off (set elsewhere)")


def command_status(paths: StatePaths, config: MacTestConfig) -> int:
    with locked(paths.control_lock):
        outcome = full_settle(paths, config)
        lift_failure = outcome.expired_block_lift_failure
        switch_problem = (
            ProblemLine(lift_failure.gh_error_line)
            if isinstance(lift_failure, ExpiredBlockLiftFailed)
            else outcome.ci_problem
        )
        if not outcome.claims:
            print("free")
        for claim in outcome.claims:
            run = claim.record
            if run_is_kept(run):
                print(f"running: leftovers of {run['what']} on {run['remote_host']} "
                      + f"since {local_time(run['since'])}, kept under pid {run['pid']}"
                      + claim_idle_clause(run))
            else:
                copy_words = "copy of " if run.get("copy") is True else ""
                print(
                    f"running: {copy_words}{run['what']} ({run['worktree']}) "
                    + f"since {local_time(run['since'])}"
                    + claim_idle_clause(run)
                )
        block = outcome.block
        if isinstance(block, NoMacBlock):
            print("no block")
            print_switch_without_block(config)
        elif isinstance(block, PendingMacBlock):
            print(
                f"block pending for {block.holder} "
                + f"({pending_wait_text(block.waiting_on)}), "
                + f"lifts {block_time(block.expires)}: {block.reason}"
            )
            print_switch_with_block(block, switch_problem)
        else:
            print(
                f"blocked by {block.holder} since {block_time(block.since)}, "
                + f"lifts {block_time(block.expires)}: {block.reason}"
            )
            if isinstance(block.free_message, FreeMessageOwed):
                print(
                    f'The "Mac is free" message has not reached {block.holder} yet.'
                )
            print_switch_with_block(block, switch_problem)
        if isinstance(lift_failure, ExpiredBlockLiftFailed):
            print(
                "The block is past its time limit but could not lift: CI's Mac "
                + "switch could not be turned back on "
                + f"({lift_failure.gh_error_line}). Run github-warmup, then unblock."
            )
    return 0


def watch_interval() -> float:
    try:
        interval = float(os.environ.get("MAC_TEST_WATCH_INTERVAL_S", "60"))
    except ValueError:
        return 60.0
    return (
        interval
        if math.isfinite(interval) and interval > 0
        else 60.0
    )


def command_watch(paths: StatePaths, config: MacTestConfig) -> int:
    with lock_if_available(paths.watch_lock) as acquired:
        if not acquired:
            return 0
        while True:
            with locked(paths.control_lock):
                outcome = full_settle(
                    paths, config, watcher_holds_lock=True
                )
            if isinstance(outcome.block, NoMacBlock):
                return 0
            time.sleep(watch_interval())


def remote_host_argument(value: str) -> str:
    if not value.strip():
        raise argparse.ArgumentTypeError("remote host must not be empty")
    return value


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)

    claim = commands.add_parser("claim")
    _ = claim.add_argument("--pid", type=int, required=True)
    _ = claim.add_argument("--what", required=True)
    _ = claim.add_argument("--worktree", default=os.getcwd())
    _ = claim.add_argument("--wait", type=float, default=0.0)
    _ = claim.add_argument("--remote", type=remote_host_argument, default=NO_REMOTE_JOB)
    _ = claim.add_argument("--copy", action="store_true")

    release = commands.add_parser("release")
    _ = release.add_argument("--pid", type=int, required=True)

    block = commands.add_parser("block")
    _ = block.add_argument("--holder", required=True)
    _ = block.add_argument("--for", dest="reason", required=True)
    _ = block.add_argument("--hours", default="")
    _ = block.add_argument("--showrunner", default="")

    unblock = commands.add_parser("unblock")
    _ = unblock.add_argument("--holder", required=True)

    free = commands.add_parser("free-now")
    _ = free.add_argument("--settings", default="")
    _ = free.add_argument("--repo-root", default=os.getcwd())
    _ = free.add_argument("--copy", action="store_true")

    _ = commands.add_parser("status")
    _ = commands.add_parser("watch")
    _ = commands.add_parser("watch-claim")
    return root


def main() -> int:
    override_guard.refuse_overrides(Path(__file__), "mac_test/mac_test.py")
    args = parser().parse_args(namespace=CliArguments())
    paths = state_paths()
    if args.command == "claim":
        return command_claim(args, paths)
    if args.command == "release":
        return command_release(args, paths)
    if args.command == "free-now":
        return command_free_now(args, paths)
    config = read_config()
    if args.command == "block":
        return command_block(args, paths, config)
    if args.command == "unblock":
        return command_unblock(args, paths, config)
    if args.command == "watch":
        return command_watch(paths, config)
    if args.command == "watch-claim":
        return command_watch_claim(paths, config)
    return command_status(paths, config)


if __name__ == "__main__":
    raise SystemExit(main())
