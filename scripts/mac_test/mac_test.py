#!/usr/bin/env python3
"""Coordinate build-step use of the Mac: one claim per repository, and the Mac block."""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
from collections.abc import Generator
from contextlib import ExitStack, contextmanager, nullcontext
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
import uuid
from typing import TYPE_CHECKING, Literal, TypeGuard, cast
from urllib.parse import quote


from scripts.build_hold.owning_session import NoOwningSession, OwningSession
from scripts.build_hold import override_guard
from scripts.mac_test import claim_state
from scripts.mac_test.claim_state import (
    CiSwitch as CiSwitch,
    NoMacBlock as NoMacBlock,
    RecordedSession as RecordedSession,
    HolderQueue as HolderQueue,
    MessageDelivery as MessageDelivery,
    NamedShowrunner as NamedShowrunner,
    NoShowrunner as NoShowrunner,
    Showrunner as Showrunner,
    WaitingOnLocalRun as WaitingOnLocalRun,
    WaitingOnCiJob as WaitingOnCiJob,
    WaitingOnUnknownCi as WaitingOnUnknownCi,
    WaitingOn as WaitingOn,
    FreeMessageNotNeeded as FreeMessageNotNeeded,
    FreeMessageOwed as FreeMessageOwed,
    FreeMessageSent as FreeMessageSent,
    FreeMessage as FreeMessage,
    PendingMacBlock as PendingMacBlock,
    ActiveMacBlock as ActiveMacBlock,
    MacBlockState as MacBlockState,
    MacBlockEvent as MacBlockEvent,
    StateReadError as StateReadError,
    ClaimStateUnreadable as ClaimStateUnreadable,
    ClaimOwnerRecord as ClaimOwnerRecord,
    ProcessBoundClaim as ProcessBoundClaim,
    RemoteJobClaim as RemoteJobClaim,
    RunRecord as RunRecord,
    RepositoryScope as RepositoryScope,
    WholeMacScope as WholeMacScope,
    ClaimScope as ClaimScope,
    WHOLE_MAC as WHOLE_MAC,
    HeldClaim as HeldClaim,
    NoRun as NoRun,
    RunState as RunState,
    NO_RUN as NO_RUN,
    StatePaths as StatePaths,
    state_paths as state_paths,
    locked as locked,
    read_json_object as read_json_object,
    read_run as read_run,
    aware_timestamp as aware_timestamp,
    required_string as required_string,
    target_claim_scope as target_claim_scope,
    claim_files as claim_files,
    read_claims as read_claims,
    record_after_unlock as record_after_unlock,
    pending_hold_events as pending_hold_events,
    coordination_depth as coordination_depth,
)
from scripts.lib import conf
from scripts.lib.user_zone import user_zone

from scripts.message import session_send
from scripts.lib import process_start as process_identity
from scripts.build_targets import target_config
from scripts.build_targets.target_config import ConfigError
from scripts.buildlog.mac_idle_alert import MacIdleConfigError, idle_config as idle_config

if TYPE_CHECKING:
    from scripts.build_queue.placement import SubmittingSession
    from scripts.build_queue import placement
    from scripts.mac_test import offload

MESSAGE_TIMEOUT_S = 30.0


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
    ci_jobless_queued_minutes: float
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
    work: int


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
FreeNowAnswer = Literal["free", "off", "backoff", "blocked", "busy", "state", "bad"]


@dataclass(frozen=True)
class UnassignedClaim:
    """A manual claim uses the provider's existing scope selection."""


UNASSIGNED_CLAIM = UnassignedClaim()



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
        self.target: str = ""
        self.leased_slots: list[int] = []
        self.assignment_file: str = ""
        self.assignment: str = ""
        self.from_pid: int = 0
        self.package: str = ""
        self.verb: offload.MacVerb = "nextest"
        self.claim_id: str = ""
        self.episode: str = ""
        self.attempt: str = ""
        self.identity: bool = False
        self.session: SubmittingSession | claim_state.CallingSession = claim_state.CALLING_SESSION


def config_path(target: str = "") -> Path:
    from scripts.mac_test import offload

    return offload.configured_ssh_target(target).settings if target else conf.path("mac_test")


def read_config(target: str = "") -> MacTestConfig:
    from scripts.mac_test import offload

    settings = config_path(target)
    config = conf.read(settings)
    values = config.values()
    platform = offload.configured_ssh_target(target).platform if target else "macos"
    if platform == "macos":
        _ = idle_config(config)
    if "ci_repo" not in values:
        ci_repo = config.text("ci_repo")
    else:
        ci_repo = values["ci_repo"]
    if platform == "linux" and ci_repo:
        raise StateReadError(f"{settings}: ci_repo must be empty for a Linux target")
    ci_fields = {key: config.text(key) if ci_repo else values.get(key, "")
                 for key in ("ci_variable", "ci_workflow", "ci_job")}

    return MacTestConfig(
        ci_repo=ci_repo,
        ci_variable=ci_fields["ci_variable"],
        ci_workflow=ci_fields["ci_workflow"],
        ci_job=ci_fields["ci_job"],
        gh_timeout_s=config.number("gh_timeout_s"),
        ci_jobless_queued_minutes=config.number("ci_jobless_queued_minutes") if ci_repo else 0.0,
        block_hours=config.number("block_hours"),
        block_max_hours=config.number("block_max_hours"),
        block_warn_minutes=config.number("block_warn_minutes"),
        host=config.text("host"),
        connect_timeout_s=config.number("connect_timeout_s"),
        claim_idle_minutes=config.number("claim_idle_minutes"),
        claim_poll_seconds=config.number("claim_poll_seconds"),
    )


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


def holder_name(key: str) -> str:
    """Show the current staff name when the fixed holder key resolves."""
    from scripts.production import staff

    try:
        member = staff.initial_snapshot().by_id(staff.StaffId(key))
    except staff.StaffUnreadable:
        return key
    return member.name if member is not None else key


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


def blocking_claim_block(paths: StatePaths, session: SubmittingSession) -> MacBlockState:
    """Return the block that prevents this session from starting new work."""
    block = read_claim_block(paths)
    if isinstance(block, NoMacBlock) or block_belongs_to(block, session):
        return NO_MAC_BLOCK
    return block


def blocked_claim(block: PendingMacBlock | ActiveMacBlock) -> ClaimFinished:
    print(f"blocked by {holder_name(block.holder)}: {block.reason}")
    return ClaimFinished(10)


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


def start_hold_recorder(event: MacBlockEvent) -> None:
    command = [str(Path(__file__).resolve().parents[1] / "lib/py"), str(Path(__file__).resolve().parent.parent / "buildlog" / "record.py"),
               "hold-event", event.kind, event.target, timestamp(event.block.since), event.block.holder, event.block.reason,
               timestamp(event.at.astimezone(timezone.utc)), timestamp(event.block.expires)]
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    except OSError:
        return
    threading.Thread(target=process.wait, name="hold-event-recorder", daemon=True).start()


def record_hold_event(
    event: Literal["began", "ended"], block: PendingMacBlock | ActiveMacBlock,
    at: datetime, target: str = "mac",
) -> None:
    record = MacBlockEvent(event, block, at, target)
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


def run_is_live(run: RunRecord) -> bool:
    return "remote_host" in run or process_identity.process_start(run["pid"]) == process_identity.Started(run["proc_start"])


def run_is_kept(run: RunRecord) -> TypeGuard[RemoteJobClaim]:
    return "remote_host" in run and process_identity.process_start(run["pid"]) != process_identity.Started(run["proc_start"])


def copy_gives_way(run: RunRecord) -> bool:
    """A live test copy whose process still runs, so a new step can stop it.

    A kept copy claim holds leftovers like any other kept claim and never gives way.
    """
    return run.get("copy") is True and process_identity.process_start(run["pid"]) == process_identity.Started(run["proc_start"])


def claim_scope(worktree: str) -> ClaimScope:
    """The repository a claim covers: the one its worktree is in, else the whole Mac."""
    from scripts.mac_test import offload

    repository = offload.repository_identity(Path(worktree))
    if isinstance(repository, offload.KnownRepository):
        return RepositoryScope(repository.name, repository.key)
    return WHOLE_MAC


def claim_path(paths: StatePaths, scope: ClaimScope) -> Path:
    from scripts.mac_test import offload

    if isinstance(scope, RepositoryScope):
        key = offload.KnownRepository(scope.name, scope.key).path_key if scope.key else quote(scope.name, safe="")
        suffix = f".slot-{scope.slot}" if scope.slot else ""
        return paths.claims / f"{key}{suffix}.json"
    return paths.run


def scopes_overlap(first: ClaimScope, second: ClaimScope) -> bool:
    if isinstance(first, WholeMacScope) or isinstance(second, WholeMacScope):
        return True
    same_repository = first.key == second.key if first.key and second.key else first.name == second.name
    return same_repository and (not first.slot or not second.slot or first.slot == second.slot)


def slot_capacity(paths: StatePaths, scope: ClaimScope) -> int:
    if target_config.uses_legacy_mac_layout(paths.platform) or isinstance(scope, WholeMacScope):
        return 1
    values = conf.read(config_path(paths.target)).values()
    missing = (target_config.MissingSlotCapacity.REQUIRE_CONFIGURED if scope.key else
               target_config.MissingSlotCapacity.SINGLE_SLOT)
    return target_config.slot_capacity(values, scope.name, missing=missing)


def affinity_path(paths: StatePaths, scope: RepositoryScope) -> Path:
    from scripts.mac_test import offload

    return paths.affinity / f"{offload.KnownRepository(scope.name, scope.key).path_key}.json"


def preferred_slots(paths: StatePaths, scope: RepositoryScope, worktree: str, capacity: int) -> tuple[int, ...]:
    affinity = read_json_object(affinity_path(paths, scope)) or {}
    if any(type(slot) is not int or slot < 1 for slot in affinity.values()):
        raise StateReadError("invalid repository slot affinity")
    preferred = affinity.get(str(Path(worktree).resolve()))
    slots = tuple(range(1, capacity + 1))
    return (cast(int, preferred), *(slot for slot in slots if slot != preferred)) if preferred in slots else slots


def remember_slot(paths: StatePaths, scope: RepositoryScope, record: RunRecord) -> None:
    path = affinity_path(paths, scope)
    affinity = {worktree: slot for worktree, slot in (read_json_object(path) or {}).items()
                if Path(worktree).exists()}
    affinity[str(Path(record["worktree"]).resolve())] = scope.slot
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, affinity)


def live_claims(paths: StatePaths) -> tuple[HeldClaim, ...]:
    """Read every claim and remove each one whose owner has ended."""
    from scripts.mac_test import offload

    live: list[HeldClaim] = []
    for claim in read_claims(paths):
        if run_is_live(claim.record):
            live.append(claim)
        elif offload.pending_start_path(paths.target, claim_state.ClaimIdentity(
                claim.record["claim_id"], claim.record["episode"], claim.record["attempt"])).exists():
            release = CliArguments()
            release.pid, release.target = claim.record["pid"], paths.target
            release.claim_id = claim.record["claim_id"]
            release.episode, release.attempt = claim.record["episode"], claim.record["attempt"]
            if command_release(release, paths, provider_lock_held=True) != 0:
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
    from scripts.production import staff

    key = delivery.name if isinstance(delivery, HolderQueue) else delivery.holder
    member = staff.initial_snapshot().by_id(staff.StaffId(key))
    if member is not None:
        return member.id
    if isinstance(delivery, HolderQueue):
        return delivery.name
    sessions_script = message_script("MAC_TEST_SESSIONS", "sessions.py")
    try:
        result = subprocess.run(
            [
                str(Path(__file__).resolve().parents[1] / "lib/py"),
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
    send_script = Path(os.environ.get("MAC_TEST_SEND", str(session_send.SEND_SCRIPT))).expanduser()
    try:
        result = subprocess.run(
            session_send.send_argv("--to", target, "--from", "mac-test", "--summary", summary,
                                   "--key", key, "--repeat-minutes", f"{repeat_minutes:g}",
                                   "--text", text, send_script=send_script),
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


@dataclass(frozen=True)
class CiRun:
    """A CI run that can still need the Mac."""

    status: Literal["queued", "in_progress"]
    created_at: datetime


def ci_activity(config: MacTestConfig) -> CiActivity:
    runs: dict[int, CiRun] = {}
    now = utc_now()
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
            "databaseId,createdAt",
        )
        if isinstance(answer, GhFailure):
            return CiUnknown(answer.line)
        items = json_items(answer)
        if isinstance(items, GhFailure):
            return CiUnknown(items.line)
        for item in items:
            if not isinstance(item, dict):
                return CiUnknown("gh returned invalid JSON")
            run = cast(dict[str, object], item)
            database_id = run.get("databaseId")
            created = run.get("createdAt")
            if type(database_id) is not int or not isinstance(created, str):
                return CiUnknown("gh returned invalid JSON")
            try:
                created_at = datetime.fromisoformat(created)
            except ValueError:
                return CiUnknown("gh returned invalid JSON")
            if created_at.tzinfo is None:
                return CiUnknown("gh returned invalid JSON")
            runs[database_id] = CiRun(status, created_at)
    for database_id, run in runs.items():
        jobs = jobs_for_run(config, database_id)
        if isinstance(jobs, GhFailure):
            return CiUnknown(jobs.line)
        if (run.status == "queued" and not jobs
                and now - run.created_at > timedelta(minutes=config.ci_jobless_queued_minutes)):
            continue
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


def send_target_free(block: ActiveMacBlock, owed: FreeMessageOwed, target: str) -> bool:
    label = "the Mac" if target == "mac" else target
    text = (
        f"Message from mac-test: {label} is free. {owed.what} ended at "
        + f"{message_time(owed.ended)}; your block ({block.reason}) is active, and "
        + "nothing is built or tested there until you run unblock."
    )
    return send_message(
        delivery_target(block.delivery),
        f"{'Mac' if target == 'mac' else target} is free",
        f"{target}-free-{timestamp(block.since)}",
        1440,
        text,
    )


@dataclass(frozen=True)
class CiRunForReport:
    """A candidate run's check suite and branch details for the skipped-job report."""

    check_suite_id: str
    branch: str
    sha: str


def skipped_mac_suites(config: MacTestConfig, suite_ids: list[str]) -> set[str] | GhFailure:
    if not suite_ids:
        return set()
    unique_ids = list(dict.fromkeys(suite_ids))
    # Each nodes field reads at most 100 IDs; both fields share one request.
    batches = [unique_ids[start:start + 100] for start in range(0, len(unique_ids), 100)]
    fields = [
        f"batch{index}: nodes(ids: {json.dumps(batch)}) {{ ... on CheckSuite {{ "
        + "id checkRuns(first: 1, filterBy: {checkName: $job, conclusions: [SKIPPED]}) "
        + "{ totalCount } } }"
        for index, batch in enumerate(batches)
    ]
    answer = run_gh(config, "api", "graphql", "-f", "query=query($job: String!) { "
                    + " ".join(fields) + " }", "-f", f"job={config.ci_job}")
    if isinstance(answer, GhFailure):
        return answer
    try:
        decoded = cast(object, json.loads(answer.stdout))
    except json.JSONDecodeError:
        return GhFailure("gh returned invalid JSON")
    if not isinstance(decoded, dict):
        return GhFailure("gh returned invalid JSON")
    response = cast(dict[str, object], decoded)
    data = response.get("data")
    if response.get("errors") or not isinstance(data, dict):
        return GhFailure("gh returned invalid JSON")
    results = cast(dict[str, object], data)
    skipped: set[str] = set()
    for index, batch in enumerate(batches):
        nodes = results.get(f"batch{index}")
        if not isinstance(nodes, list) or len(cast(list[object], nodes)) != len(batch):
            return GhFailure("gh returned invalid JSON")
        seen: set[str] = set()
        for raw_node in cast(list[object], nodes):
            if not isinstance(raw_node, dict):
                return GhFailure("gh returned invalid JSON")
            node = cast(dict[str, object], raw_node)
            suite_id = node.get("id")
            checks = node.get("checkRuns")
            if not isinstance(suite_id, str) or suite_id not in batch or suite_id in seen or not isinstance(checks, dict):
                return GhFailure("gh returned invalid JSON")
            count = cast(dict[str, object], checks).get("totalCount")
            if type(count) is not int or count < 0:
                return GhFailure("gh returned invalid JSON")
            seen.add(suite_id)
            if count:
                skipped.add(suite_id)
    return skipped


def skipped_ci_runs(config: MacTestConfig, since: datetime) -> str:
    if not config.ci_repo:
        return ""
    answer = run_gh(
        config,
        "api",
        f"repos/{config.ci_repo}/actions/workflows/{quote(config.ci_workflow, safe='')}/runs",
        "--method", "GET",
        "-f", f"created=>={timestamp(since.replace(microsecond=0))}",
        "-F", "per_page=100",
        "--paginate", "--slurp",
    )
    if isinstance(answer, GhFailure):
        return f"Could not list the CI runs that skipped the macOS job: {answer.line}"
    pages = json_items(answer)
    if isinstance(pages, GhFailure):
        return f"Could not list the CI runs that skipped the macOS job: {pages.line}"
    items: list[object] = []
    for page in pages:
        if not isinstance(page, dict):
            return "Could not list the CI runs that skipped the macOS job: gh returned invalid JSON"
        page_runs = cast(dict[str, object], page).get("workflow_runs")
        if not isinstance(page_runs, list):
            return "Could not list the CI runs that skipped the macOS job: gh returned invalid JSON"
        items.extend(cast(list[object], page_runs))
        if len(items) >= 200:
            items = items[:200]
            break
    runs: list[CiRunForReport] = []
    for item in items:
        if not isinstance(item, dict):
            return "Could not list the CI runs that skipped the macOS job: gh returned invalid JSON"
        run = cast(dict[str, object], item)
        suite_id = run.get("check_suite_node_id")
        branch = run.get("head_branch")
        sha = run.get("head_sha")
        if (
            type(run.get("id")) is not int
            or not isinstance(suite_id, str)
            or not suite_id
            or not isinstance(branch, str)
            or not isinstance(sha, str)
        ):
            return "Could not list the CI runs that skipped the macOS job: gh returned invalid JSON"
        runs.append(CiRunForReport(suite_id, branch, sha))
    skipped = skipped_mac_suites(config, [run.check_suite_id for run in runs])
    if isinstance(skipped, GhFailure):
        return f"Could not list the CI runs that skipped the macOS job: {skipped.line}"
    branch_runs: dict[str, tuple[int, str]] = {}
    for candidate in runs:
        if candidate.check_suite_id in skipped:
            prior = branch_runs.get(candidate.branch)
            branch_runs[candidate.branch] = (
                (1, candidate.sha) if prior is None else (prior[0] + 1, prior[1])
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


def expiry_text(block: PendingMacBlock | ActiveMacBlock, skipped: str, target: str) -> str:
    label = "the Mac" if target == "mac" else target
    text = (
        f"Message from mac-test: {label} block by {holder_name(block.holder)} ({block.reason}) "
        + f"reached its time limit at {block_time(block.expires)} and lifted by itself."
    )
    if ci_needs_restore(block.ci):
        text += f" CI may use {label} again."
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
    label = "Mac" if paths.target == "mac" else paths.target
    if not config.ci_repo:
        block = replace_ci(block, "not_configured")
    elif ci_needs_restore(block.ci):
        restored = switch_ci_back(config)
        if isinstance(restored, GhFailure):
            text = (
                f"Message from mac-test: {label} block by {holder_name(block.holder)} is past "
                + f"its time limit but still in place: CI's {label} switch could not be "
                + f"turned back on ({restored.line}). Run github-warmup, then unblock."
            )
            send_to_block_recipients(
                block,
                f"{label} block could not lift",
                f"{paths.target}-block-stuck-{timestamp(block.since)}",
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
            record_hold_event("ended", block, at, paths.target)
    skipped = (
        skipped_ci_runs(config, block.since)
        if block.ci != "not_configured"
        else ""
    )
    send_to_block_recipients(
        block,
        f"{label} block lifted",
        f"{paths.target}-block-expired-{timestamp(block.since)}",
        1440,
        expiry_text(block, skipped, paths.target),
    )
    return SettleOutcome(
        claims,
        NO_MAC_BLOCK,
        NO_EXPIRED_BLOCK_LIFT_FAILURE,
        NO_PROBLEM,
        NO_PROBLEM,
    )


def warning_text(block: PendingMacBlock | ActiveMacBlock, target: str) -> str:
    label = "Mac" if target == "mac" else target
    return (
        f"Message from mac-test: your {label} block ({block.reason}) lifts by itself at "
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
        if name.startswith("MAC_TEST_") or name == "BUILD_TARGETS_CONFIG"
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
        str(Path(__file__).resolve().parents[1] / "lib/py"),
        str(Path(__file__).resolve()),
        "watch",
        *(["--target", paths.target] if paths.registry_selected or paths.target != "mac" else []),
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
            f"warning: the {'Mac' if paths.target == 'mac' else paths.target} block watcher could not be started",
            file=sys.stderr,
        )


def start_claim_watcher(paths: StatePaths) -> None:
    with lock_if_available(paths.claim_watch_lock) as acquired:
        if not acquired:
            return
    environment = [
        f"{name}={value}"
        for name, value in sorted(os.environ.items())
        if name.startswith("MAC_TEST_") or name == "BUILD_TARGETS_CONFIG"
    ]
    command = [
        "systemd-run", "--user", "--collect", "--quiet", "--no-block",
        "--unit", f"mac-test-claim-watch-{os.getpid()}", "--", "/usr/bin/env",
        f"HOME={os.environ.get('HOME', '')}",
        f"PATH={os.environ.get('PATH', '')}",
        *environment, str(Path(__file__).resolve().parents[1] / "lib/py"), str(Path(__file__).resolve()), "watch-claim",
        *(["--target", paths.target] if paths.registry_selected or paths.target != "mac" else []),
    ]
    try:
        result = subprocess.run(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
        failed = result.returncode != 0
    except OSError:
        failed = True
    if failed:
        print(f"warning: the {'Mac' if paths.target == 'mac' else paths.target} claim watcher could not be started",
              file=sys.stderr)


def mac_activity_script() -> str:
    """Count the user's mirror work and build executables without sending signals."""
    from scripts.mac_test import offload

    return offload.mirror_selector_script(offload.ALL_MIRRORS) + r'''
mirror_pids=$(select_leftovers) || exit 1
process_records=$(ps -axww -o pid=,uid=,comm=) || exit 1
activity_user=$(id -u) || exit 1
executable_records=$(printf '%s\n' "$process_records" | awk -v user="$activity_user" '
    $2 == user {
        executable = $0
        sub(/^[[:space:]]*[0-9]+[[:space:]]+[0-9]+[[:space:]]+/, "", executable)
        name = executable
        sub(/^.*\//, "", name)
        compiler = name == "cargo" || name == "rustc" || name == "cargo-nextest"
        if (compiler || executable ~ /\/target\//)
            print $1, (!compiler && executable ~ /\/target\// && executable !~ /\/deps\//) ? "app" : "work"
    }') || exit 1
executable_pids=$(printf '%s\n' "$executable_records" | awk '{ print $1 }') || exit 1
app_pids=$(printf '%s\n' "$executable_records" | awk '$2 == "app" { print $1 }') || exit 1
active_pids=$(printf '%s\n%s\n' "$mirror_pids" "$executable_pids" | awk '/^[0-9]+$/ && !seen[$0]++ { print $0 }') || exit 1
work_count=$(printf '%s\n' "$app_pids" '--active--' "$active_pids" | awk '
    $0 == "--active--" { active=1; next }
    /^[0-9]+$/ { if (!active) app[$0]=1; else if (!app[$0]) count++ }
    END { print count+0 }') || exit 1
active_count=$(printf '%s\n' "$active_pids" | awk '/^[0-9]+$/ { count++ } END { print count+0 }') || exit 1
printf 'active=%s\nwork=%s\nrc=0\n' "$active_count" "$work_count"
'''


def check_mac_activity(config: MacTestConfig, host: str) -> MacActivity:
    from scripts.buildlog import sync as buildlog_sync

    arguments = ["ssh", "-o", "BatchMode=yes", "-o",
                 f"ConnectTimeout={config.connect_timeout_s:g}", host, mac_activity_script()]
    try:
        result = subprocess.run(
            arguments,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, check=False, timeout=10,
        )
    except subprocess.TimeoutExpired:
        return ActivityCheckFailed("SSH activity check timed out after 10 s")
    except OSError as error:
        return ActivityCheckFailed(f"SSH activity check could not start: {error}")
    except UnicodeError:
        return ActivityCheckFailed("the activity check answer could not be read")
    ledger = buildlog_sync.active_transfers.get()
    if isinstance(ledger, buildlog_sync.TransferLedger):
        ledger.observe("ssh_activity", arguments, result.stdout, result.stderr)
    if result.returncode != 0:
        reason = " ".join(result.stderr.split()) or f"SSH exited {result.returncode}"
        return ActivityCheckFailed(reason)
    lines = result.stdout.splitlines()
    if (len(lines) != 3 or lines[2] != "rc=0" or not lines[0].startswith("active=")
            or not lines[1].startswith("work=")):
        return ActivityCheckFailed("the activity check answer could not be read")
    counts = [lines[0].removeprefix("active="), lines[1].removeprefix("work=")]
    if any(not text.isascii() or not text.isdecimal() for text in counts):
        return ActivityCheckFailed("the activity check answer could not be read")
    try:
        count, work = (int(text) for text in counts)
    except ValueError:
        return ActivityCheckFailed("the activity check answer could not be read")
    if work > count:
        return ActivityCheckFailed("the activity check work count exceeds its active count")
    return MacActive(count, work) if count else MacIdle()


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
    from scripts.buildlog import sync as buildlog_sync

    with locked(paths.lock):
        claims = tuple(claim for claim in read_claims(paths) if run_is_live(claim.record))
    if not claims:
        return False
    if paths.platform == "linux":
        # The Linux controller must confirm remote cleanup before claim removal.
        return True
    activity: dict[str, MacActivity] = {}
    traffic: dict[str, buildlog_sync.TransferLedger] = {}
    for claim in claims:
        host = claim_host(claim.record, config)
        if host not in activity:
            traffic[host] = buildlog_sync.TransferLedger()
            with buildlog_sync.measure_transfers(traffic[host]):
                activity[host] = check_mac_activity(config, host)
    now = utc_now()
    watched = False
    for claim in claims:
        host = claim_host(claim.record, config)
        with buildlog_sync.measure_transfers(traffic[host]):
            if watch_claim(paths, config, claim, activity[host], now):
                watched = True
    return watched


def watch_claim(
    paths: StatePaths, config: MacTestConfig, claim: HeldClaim, activity: MacActivity, now: datetime,
) -> bool:
    """Apply one activity answer to one claim; return whether it is still watched."""
    from scripts.buildlog import sync as buildlog_sync

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
        changed = False
        ledger = buildlog_sync.active_transfers.get()
        if isinstance(ledger, buildlog_sync.TransferLedger) and ledger.transfers:
            # Charge a shared host query to only one live claim.
            current["transfers"] = [*current.get("transfers", []), *ledger.transfers]
            ledger.transfers.clear()
            changed = True
        if isinstance(activity, ActivityCheckFailed):
            if changed:
                write_json(claim.path, current)
            print(f"Mac claim activity check failed: {activity.reason}", flush=True)
            return True
        if isinstance(activity, MacActive) and activity.work > 0:
            if "work_idle_since" in current:
                del current["work_idle_since"]
                changed = True
        elif "work_idle_since" not in current:
            current["work_idle_since"] = timestamp(now)
            changed = True
        if isinstance(activity, MacActive):
            if "idle_since" in current:
                del current["idle_since"]
                changed = True
            if changed:
                write_json(claim.path, current)
            return True
        if "idle_since" not in current:
            current["idle_since"] = timestamp(now)
            write_json(claim.path, current)
            return True
        if changed:
            write_json(claim.path, current)
        idle_since = aware_timestamp(current["idle_since"], claim.path, "idle_since")
        if now - idle_since < timedelta(minutes=config.claim_idle_minutes):
            return True
    release = CliArguments()
    release.pid, release.target = current["pid"], paths.target
    release.claim_id = current["claim_id"]
    release.episode, release.attempt = current["episode"], current["attempt"]
    if command_release(release, paths) != 0:
        return True
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
        + "~/.claude/scripts/lib/py ~/.claude/scripts/mac_test/mac_test.py claim --pid $$ "
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
        waiting_claims = tuple(claim for claim in claims if not claim_belongs_to_block(claim.record, block))
        # The holder's running work does not need a Mac-free message.
        message_needed = activation_message_needed and not claims
        if waiting_claims:
            block = pending_with_wait(block, WaitingOnLocalRun(waiting_claims[0].record["what"]))
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
                block = activate_block(block, now, message_needed)
                with locked(paths.lock):
                    write_block(paths.block, block)
        else:
            block = activate_block(block, now, message_needed)
            with locked(paths.lock):
                write_block(paths.block, block)

    if (
        isinstance(block, ActiveMacBlock)
        and isinstance(block.free_message, FreeMessageOwed)
        and send_target_free(block, block.free_message, paths.target)
    ):
        block = mark_free_message_sent(block)
        with locked(paths.lock):
            write_block(paths.block, block)

    if block.expires - now <= timedelta(minutes=config.block_warn_minutes):
        _ = send_message(
            delivery_target(block.delivery),
            f"{'Mac' if paths.target == 'mac' else paths.target} block nearing its time limit",
            f"{paths.target}-block-warn-{timestamp(block.expires)}",
            1440,
            warning_text(block, paths.target),
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
    from scripts.build_hold import build_hold

    owner = build_hold.owning_session()
    return (
        RecordedSession(owner.session_id, holder)
        if isinstance(owner, OwningSession)
        else HolderQueue(holder)
    )


def block_belongs_to(
    block: PendingMacBlock | ActiveMacBlock,
    owner: OwningSession | NoOwningSession,
) -> bool:
    return (
        isinstance(owner, OwningSession)
        and isinstance(block.delivery, RecordedSession)
        and block.delivery.name == owner.session_id
    )


def claim_belongs_to_block(
    claim: RunRecord,
    block: PendingMacBlock | ActiveMacBlock,
) -> bool:
    return (
        isinstance(block.delivery, RecordedSession)
        and claim.get("holder_session") == block.delivery.name
    )


def requested_showrunner(name: str) -> Showrunner:
    return NamedShowrunner(name) if name else NO_SHOWRUNNER


def print_claim_identity(args: CliArguments, record: RunRecord) -> None:
    if args.identity:
        identity = claim_state.claim_identity(record)
        print("claim_identity=" + json.dumps({"claim_id": identity.claim_id,
              "episode": identity.episode, "attempt": identity.attempt}))


def take_over_kept_claim(args: CliArguments, claim: HeldClaim, kept: RemoteJobClaim,
                         *, session: SubmittingSession) -> ClaimAttempt:
    proc_start = process_identity.process_start(args.pid)
    if not isinstance(proc_start, process_identity.Started):
        print(f"cannot claim: process {args.pid} is not running", file=sys.stderr)
        return ClaimFinished(1)
    kept["pid"] = args.pid
    kept["proc_start"] = proc_start.start
    kept["submitting_session"] = claim_state.session_id(session)
    kept["cleanup_pending"] = True
    if args.copy:
        kept["copy"] = True
    else:
        _ = kept.pop("copy", None)
    write_json(claim.path, kept)
    print_claim_identity(args, kept)
    print(f"leftovers: {kept['remote_host']}")
    if "slot" in kept:
        print(f"slot: {kept['slot']}")
    return ClaimFinished(13)


def claim_once(args: CliArguments, paths: StatePaths, scope: ClaimScope,
               *, session: SubmittingSession, reserve_wait: bool = False,
               assignment: placement.PlacementAssignment | UnassignedClaim = UNASSIGNED_CLAIM) -> ClaimAttempt:
    """One claim attempt under state.lock: take over, refuse, or write the claim."""
    try:
        claims = live_claims(paths)
    except ClaimStateUnreadable as error:
        print(f"state unreadable: {error.path}")
        return ClaimFinished(12)
    overlapping = tuple(claim for claim in claims if scopes_overlap(claim.scope, scope))
    identity = claim_state.ClaimIdentity(args.claim_id, args.episode, args.attempt)
    try:
        block = blocking_claim_block(paths, session)
    except (OSError, StateReadError):
        print(f"state unreadable: {paths.block}")
        return ClaimFinished(12)
    for claim in overlapping:
        record = claim.record
        if ("remote_host" in record and record.get("cleanup_pending")
                and record["pid"] == args.pid and claim_state.claim_identity(record) == identity
                and not run_is_kept(record)
                and (not args.leased_slots or record.get("slot", 1) in args.leased_slots)):
            print_claim_identity(args, record)
            print(f"leftovers: {record['remote_host']}")
            if "slot" in record:
                print(f"slot: {record['slot']}")
            return ClaimFinished(13)
    if (isinstance(assignment, UnassignedClaim)
            and not target_config.uses_legacy_mac_layout(paths.platform) and isinstance(scope, RepositoryScope)):
        try:
            capacity = slot_capacity(paths, scope)
            slots = preferred_slots(paths, scope, args.worktree, capacity)
            if args.leased_slots:
                slots = tuple(slot for slot in slots if slot in args.leased_slots)
        except (OSError, StateReadError, ConfigError) as error:
            print(f"state unreadable: {error}")
            return ClaimFinished(12)
        for claim in overlapping:
            if (claim.record["pid"] == args.pid and claim.record["worktree"] == args.worktree
                    and claim_state.claim_identity(claim.record) == identity
                    and not run_is_kept(claim.record) and "slot" in claim.record
                    and (not args.leased_slots or claim.record["slot"] in args.leased_slots)
                    and isinstance(block, NoMacBlock)):
                print_claim_identity(args, claim.record)
                print(f"claimed\nslot: {claim.record['slot']}")
                return ClaimFinished(0)
        for slot in slots:
            selected = RepositoryScope(scope.name, scope.key, slot)
            if not any(scopes_overlap(claim.scope, selected) for claim in claims):
                scope = selected
                overlapping = ()
                break
        else:
            if args.leased_slots and slots:
                selected_slot = next((claim.record["slot"] for claim in overlapping
                                      if run_is_kept(claim.record) and "slot" in claim.record
                                      and claim.record["slot"] in args.leased_slots), slots[0])
                scope = RepositoryScope(scope.name, scope.key, selected_slot)
                overlapping = tuple(claim for claim in claims if scopes_overlap(claim.scope, scope))
    if (not target_config.uses_legacy_mac_layout(paths.platform) and isinstance(scope, RepositoryScope)
            and args.leased_slots and scope.slot not in args.leased_slots):
        return ClaimFinished(11)
    if isinstance(args.remote, str) or reserve_wait:
        for claim in overlapping:
            record = claim.record
            if scopes_overlap(claim.scope, scope) and run_is_kept(record):
                return take_over_kept_claim(args, claim, record, session=session)
    if not isinstance(block, NoMacBlock):
        return blocked_claim(block)
    owner = session
    if overlapping:
        # A test copy gives way to any new step; a copy never replaces a copy.
        if not args.copy and all(copy_gives_way(claim.record) for claim in overlapping):
            return ClaimHeldByCopy(tuple(claim.record for claim in overlapping))
        return ClaimBusy(overlapping[0].record)
    proc_start = process_identity.process_start(args.pid)
    if not isinstance(proc_start, process_identity.Started):
        print(f"cannot claim: process {args.pid} is not running", file=sys.stderr)
        return ClaimFinished(1)
    claimed: RunRecord = {
        "claim_id": args.claim_id or uuid.uuid4().hex,
        "episode": args.episode or uuid.uuid4().hex,
        "attempt": args.attempt or uuid.uuid4().hex,
        "submitting_session": claim_state.session_id(session),
        "pid": args.pid,
        "proc_start": proc_start.start,
        "what": args.what,
        "worktree": args.worktree,
        "since": timestamp(utc_now()),
    }
    if args.copy:
        claimed["copy"] = True
    if isinstance(args.remote, str):
        remote_claim: RemoteJobClaim = {**claimed, "remote_host": args.remote}
        claimed = remote_claim
    if isinstance(owner, OwningSession):
        claimed["holder_session"] = owner.session_id
    if isinstance(scope, RepositoryScope):
        claimed["repo_name"] = scope.name
        claimed["repo_key"] = scope.key
        if scope.slot:
            claimed["slot"] = scope.slot
    path = claim_path(paths, scope)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, claimed)
    print_claim_identity(args, claimed)
    if isinstance(scope, RepositoryScope) and scope.slot:
        remember_slot(paths, scope, claimed)
    print("claimed")
    if isinstance(scope, RepositoryScope) and scope.slot:
        print(f"slot: {scope.slot}")
    return ClaimFinished(0)


def copy_yield_seconds() -> float:
    try:
        seconds = float(os.environ.get("MAC_TEST_COPY_YIELD_S", "60"))
    except ValueError:
        return 60.0
    return seconds if math.isfinite(seconds) and seconds > 0 else 60.0


def stop_copy(holder: RunRecord) -> None:
    """SIGTERM a copy claim's runner, never a process that reused its pid."""
    if process_identity.process_start(holder["pid"]) != process_identity.Started(holder["proc_start"]):
        return
    try:
        os.kill(holder["pid"], signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass


def adopt_wait_claim(args: CliArguments, paths: StatePaths, scope: ClaimScope,
                     *, session: SubmittingSession) -> ClaimAttempt:
    """Transfer a live reservation under the target lock without releasing capacity."""
    start = process_identity.process_start(args.pid)
    if not isinstance(start, process_identity.Started):
        return ClaimFinished(12)
    claims = read_claims(paths)
    owners = [claim for claim in claims if scopes_overlap(claim.scope, scope)
              and claim.record["pid"] == args.from_pid
              and claim_state.claim_identity(claim.record) == claim_state.ClaimIdentity(
                  args.claim_id, args.episode, args.attempt)
              and claim.record["submitting_session"] == claim_state.session_id(session)
              and ("remote_host" not in claim.record or claim.record.get("cleanup_pending"))
              and process_identity.process_start(args.from_pid) == process_identity.Started(claim.record["proc_start"])
              and (claim.record["worktree"] == args.worktree or claim.record.get("cleanup_pending"))]
    if len(owners) != 1 or not isinstance(args.remote, str):
        return ClaimFinished(12)
    claim = owners[0]
    if (not target_config.uses_legacy_mac_layout(paths.platform) and isinstance(claim.scope, RepositoryScope)
            and args.leased_slots and claim.scope.slot not in args.leased_slots):
        return ClaimFinished(11)
    # A reservation grants capacity. A later block still prevents new work.
    # Retained remote work keeps its cleanup path.
    if "remote_host" not in claim.record:
        try:
            block = blocking_claim_block(paths, session)
        except (OSError, StateReadError):
            print(f"state unreadable: {paths.block}")
            return ClaimFinished(12)
        if not isinstance(block, NoMacBlock):
            return blocked_claim(block)
    record: RemoteJobClaim
    if "remote_host" in claim.record:
        record = {**claim.record}
    else:
        record = {**claim.record, "remote_host": args.remote}
    record["pid"] = args.pid
    record["proc_start"] = start.start
    write_json(claim.path, record)
    print_claim_identity(args, record)
    if isinstance(claim.scope, RepositoryScope) and claim.scope.slot:
        remember_slot(paths, claim.scope, record)
        print(f"slot: {claim.scope.slot}")
    if record.get("cleanup_pending"):
        print(f"leftovers: {record['remote_host']}")
        return ClaimFinished(13)
    print("claimed")
    return ClaimFinished(0)


def command_claim_wait(args: CliArguments, paths: StatePaths, *, kind: str = "",
                       session: SubmittingSession | claim_state.CallingSession = claim_state.CALLING_SESSION,
                       assignment: placement.PlacementAssignment | UnassignedClaim = UNASSIGNED_CLAIM) -> int:
    """Reserve a slot for a live local wait without starting remote work."""
    from scripts.mac_test import offload

    settings = Path(args.settings) if args.settings else config_path(args.target)
    config = offload.read_config(settings)
    if isinstance(offload.mac_step_eligibility(config, Path(args.repo_root), args.package,
                                             args.verb, args.target, kind=kind), offload.IneligibleMacStep):
        return 1
    args.worktree = args.repo_root
    args.what = f"{args.verb} {args.package or 'workspace'}"
    return command_claim(args, paths, watch_claim=False, session=session, assignment=assignment)


def command_claim(args: CliArguments, paths: StatePaths, *, watch_claim: bool = True,
                  session: SubmittingSession | claim_state.CallingSession = claim_state.CALLING_SESSION,
                  assignment: placement.PlacementAssignment | UnassignedClaim = UNASSIGNED_CLAIM) -> int:
    from scripts.build_queue import admission_protocol as wire, placement

    if (args.assignment_file or args.assignment) and isinstance(assignment, UnassignedClaim):
        saved = wire.parse_reply(args.assignment or Path(args.assignment_file).read_text())
        if not isinstance(saved, wire.Assignment):
            raise ValueError("claim requires a remote assignment")
        assignment = saved.assignment
    session = claim_state.submitting_session(session)
    if isinstance(assignment, placement.PlacementAssignment):
        destination = assignment.destination
        if destination.target != (args.target or "mac") or destination.provider not in ("mac", "ssh"):
            return 12
        scope = destination.scope
        if destination.provider == "ssh" and (not isinstance(scope, RepositoryScope) or scope.slot != destination.slot):
            return 12
        if not args.from_pid:
            args.episode, args.attempt = assignment.episode, assignment.attempt
            if not args.claim_id:
                args.claim_id = uuid.uuid5(uuid.NAMESPACE_URL, placement.attempt_key(assignment.episode, assignment.attempt)).hex
        session = assignment.session
    else:
        scope = target_claim_scope(paths, args.worktree)
    deadline = time.monotonic() + max(args.wait, 0.0)
    yield_deadline = 0.0
    stopped: set[tuple[int, str]] = set()
    while True:
        with locked(paths.lock):
            attempt = (adopt_wait_claim(args, paths, scope, session=session) if args.from_pid
                       else claim_once(args, paths, scope, session=session, reserve_wait=not watch_claim, assignment=assignment))
        if isinstance(attempt, ClaimFinished):
            if watch_claim and attempt.status in (0, 13):
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


def command_release(args: CliArguments, paths: StatePaths, *, provider_lock_held: bool = False) -> int:
    from scripts.mac_test import offload

    with nullcontext() if provider_lock_held else locked(paths.lock):
        # Each file is read alone, so another repository's damaged record
        # cannot keep this owner's claim in place.
        owned: list[tuple[Path, RunRecord]] = []
        for path in claim_files(paths):
            try:
                record = read_run(path)
            except StateReadError:
                continue
            if (isinstance(record, dict) and record["pid"] == args.pid
                    and claim_state.claim_identity(record) == claim_state.ClaimIdentity(
                        args.claim_id, args.episode, args.attempt)):
                owned.append((path, record))
    for path, record in owned:
        identity = claim_state.ClaimIdentity(args.claim_id, args.episode, args.attempt)
        pending = offload.pending_start_path(paths.target, identity)
        if "remote_host" in record and pending.exists():
            settings = Path(args.settings) if args.settings else config_path(paths.target)
            connection = offload.read_connection_settings(settings)
            command = (offload.start_fence_command(identity, paths.target, settle=True, scope_key=path.stem)
                       + "\nprintf 'leftovers=0\\nrc=0\\n'\n")
            observed = offload.read_leftovers(connection, record["remote_host"], command, paths.target)
            if (not isinstance(observed.result, offload.LeftoversEnded)
                    or "dispatch_start_settled=" + offload.pending_start_key(identity) not in observed.output.splitlines()):
                print("cannot release: the remote start fence remains unconfirmed", file=sys.stderr)
                return 12
        with nullcontext() if provider_lock_held else locked(paths.lock):
            if read_run(path) != record:
                print("cannot release: the exact claim changed during cleanup", file=sys.stderr)
                return 12
            transfers = record.get("transfers", [])
            if transfers:
                print("copy_bytes=" + json.dumps(transfers))
            pending.unlink(missing_ok=True)
            path.unlink(missing_ok=True)
    with nullcontext() if provider_lock_held else locked(paths.lock):
        block_present = not isinstance(read_block(paths.block), NoMacBlock)
    if block_present:
        start_watcher(paths)
    return 0


def free_now(
    paths: StatePaths, settings: Path | offload.DefaultMacSettings, repo_root: Path, copy: bool = False,
    *, session: SubmittingSession | claim_state.CallingSession = claim_state.CALLING_SESSION,
) -> FreeNowAnswer:
    """Whether a claim for repo_root's repository made now could start a Mac run.

    Read from local records alone, and cheap enough for LINT_QUEUE_LEAVE_HOOK,
    which runs it on every held memory poll: no SSH, no gh and no lock. CI is
    not read, since a claim does not consult it; the claim itself still decides
    under its lock. Another repository's claim leaves the answer free. A copy
    claim gives way to a new test, so it holds the scope only for another copy
    (`copy`).
    """
    from scripts.mac_test import offload
    from scripts.buildlog import repository

    if not offload.read_config(settings).enabled:
        return "off"
    backoff = (offload.read_backoff(time.time()) if paths.target == "mac"
               else offload.read_backoff(time.time(), paths.target))
    if isinstance(backoff, offload.ActiveBackoff):
        return "backoff"
    session = claim_state.submitting_session(session)
    scope = target_claim_scope(paths, str(repo_root))
    try:
        block = blocking_claim_block(paths, session)
        if not isinstance(block, NoMacBlock):
            return "blocked"
        claims = read_claims(paths)
    except (OSError, StateReadError):
        return "state"
    repository_name = repository.repo_name(str(repo_root))
    if repository_name is not None and isinstance(
            offload.read_mirror_busy(time.time(), repository_name, paths.target), offload.ActiveBackoff):
        return "busy"
    scopes: tuple[ClaimScope, ...] = (scope,)
    if not target_config.uses_legacy_mac_layout(paths.platform) and isinstance(scope, RepositoryScope):
        try:
            scopes = tuple(RepositoryScope(scope.name, scope.key, slot)
                           for slot in range(1, slot_capacity(paths, scope) + 1))
        except (OSError, ConfigError):
            return "state"
    held = all(any(
        scopes_overlap(claim.scope, candidate) and run_is_live(claim.record)
        and (copy or not copy_gives_way(claim.record)) for claim in claims
    ) for candidate in scopes)
    if held:
        return "busy"
    if isinstance(offload.read_failed_probe(paths.target), offload.FailedProbe):
        return "bad"
    if repository_name is not None and offload.linux_version_decline_path(paths.target, repository_name).exists():
        return "bad"
    return "free"


def command_free_now(args: CliArguments, paths: StatePaths) -> int:
    from scripts.mac_test import offload

    settings = Path(args.settings) if args.settings else offload.DEFAULT_MAC_SETTINGS
    if not args.settings and args.target:
        settings = config_path(args.target)
    answer = free_now(paths, settings, Path(args.repo_root), args.copy, session=args.session)
    print(answer)
    return 0 if answer == "free" else 1


def block_state_line(
    block: PendingMacBlock | ActiveMacBlock,
    waiting_problem: Problem,
    target: str = "mac",
) -> str:
    host = "the Mac" if target == "mac" else target
    if isinstance(block, ActiveMacBlock):
        return (
            f"{'Mac' if target == 'mac' else target} blocked for {holder_name(block.holder)}: nothing is running there, "
            + "it is free now."
        )
    waiting = block.waiting_on
    if isinstance(waiting, WaitingOnLocalRun):
        return (
            f"Block pending for {holder_name(block.holder)}: {waiting.what} is running on {host}. "
            + "Nothing new starts there, and you get a message when it ends."
        )
    if isinstance(waiting, WaitingOnCiJob):
        return (
            f"Block pending for {holder_name(block.holder)}: a CI run may still use the Mac. "
            + "Nothing new starts there, and you get a message when CI no longer "
            + "needs the Mac."
        )
    line = (
        waiting_problem.line
        if isinstance(waiting_problem, ProblemLine)
        else "unknown error"
    )
    return (
        f"Block pending for {holder_name(block.holder)}: CI could not be checked ({line}). "
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
                print(f"already blocked by {holder_name(existing.holder)}")
                return 1
            now = utc_now()
            expires = now + timedelta(hours=duration.hours)
            if isinstance(existing, NoMacBlock):
                pending = PendingMacBlock(
                    args.holder,
                    args.reason,
                    now,
                    expires,
                    requested_delivery(args.holder),
                    requested_showrunner(args.showrunner),
                    "still_on",
                    WAITING_ON_UNKNOWN_CI,
                )
                waiting_claims = tuple(claim for claim in claims if not claim_belongs_to_block(claim.record, pending))
                block: PendingMacBlock | ActiveMacBlock = pending
                if waiting_claims:
                    block = pending_with_wait(pending, WaitingOnLocalRun(waiting_claims[0].record["what"]))
                elif not config.ci_repo:
                    block = activate_block(pending, now, False)
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
                record_hold_event("began", block, now, paths.target)
        outcome = full_settle(
            paths,
            config,
            activation_message_needed=not new_block,
        )
        if isinstance(outcome.block, NoMacBlock):
            return 0
        print(block_state_line(outcome.block, outcome.waiting_problem, paths.target))
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
            print(f"blocked by {holder_name(block.holder)}")
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
                record_hold_event("ended", block, at, paths.target)
        if ci_needs_restore(block.ci):
            print("Mac unblocked; CI may use the Mac again.")
        elif block.ci == "off_before":
            print("Mac unblocked; CI's Mac switch was already off and stays off.")
        else:
            print(f"{'Mac' if paths.target == 'mac' else paths.target} unblocked.")
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
    from scripts.mac_test import offload

    with locked(paths.control_lock):
        outcome = full_settle(paths, config)
        lift_failure = outcome.expired_block_lift_failure
        switch_problem = (
            ProblemLine(lift_failure.gh_error_line)
            if isinstance(lift_failure, ExpiredBlockLiftFailed)
            else outcome.ci_problem
        )
        if not outcome.claims:
            print("unreachable: failed probe" if isinstance(offload.read_failed_probe(paths.target), offload.FailedProbe) else "free")
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
                f"block pending for {holder_name(block.holder)} "
                + f"({pending_wait_text(block.waiting_on)}), "
                + f"lifts {block_time(block.expires)}: {block.reason}"
            )
            print_switch_with_block(block, switch_problem)
        else:
            print(
                f"blocked by {holder_name(block.holder)} since {block_time(block.since)}, "
                + f"lifts {block_time(block.expires)}: {block.reason}"
            )
            if isinstance(block.free_message, FreeMessageOwed):
                print(
                    f'The "{"Mac" if paths.target == "mac" else paths.target} is free" message '
                    + f'has not reached {holder_name(block.holder)} yet.'
                )
            print_switch_with_block(block, switch_problem)
        if isinstance(lift_failure, ExpiredBlockLiftFailed):
            print(
                "The block is past its time limit but could not lift: CI's Mac "
                + "switch could not be turned back on "
                + f"({lift_failure.gh_error_line}). Run github-warmup, then unblock."
            )
    return 0


def command_target_status(paths: StatePaths) -> int:
    """Show recorded target state without locks, probes or cleanup."""
    from scripts.mac_test import offload

    try:
        block = read_claim_block(paths)
        claims = tuple(claim for claim in read_claims(paths) if run_is_live(claim.record))
    except (OSError, StateReadError) as error:
        print(f"state unreadable: {error}")
        return 12
    if not isinstance(block, NoMacBlock):
        print(f"blocked by {holder_name(block.holder)} since {block_time(block.since)}: {block.reason}")
    for claim in claims:
        print(f"running: {claim.record['what']} ({claim.record['worktree']})")
    if isinstance(block, NoMacBlock) and not claims:
        if isinstance(offload.read_failed_probe(paths.target), offload.FailedProbe):
            print("unreachable: failed probe")
        elif isinstance(offload.read_backoff(time.time(), paths.target), offload.ActiveBackoff):
            print("unreachable: retry backoff")
        else:
            print("free")
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
            time.sleep(watch_interval() if paths.target == "mac" else config.claim_poll_seconds)


def remote_host_argument(value: str) -> str:
    if not value.strip():
        raise argparse.ArgumentTypeError("remote host must not be empty")
    return value


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    _ = root.add_argument("--target", default="")
    commands = root.add_subparsers(dest="command", required=True)

    claim = commands.add_parser("claim")
    _ = claim.add_argument("--pid", type=int, required=True)
    _ = claim.add_argument("--what", required=True)
    _ = claim.add_argument("--worktree", default=os.getcwd())
    _ = claim.add_argument("--wait", type=float, default=0.0)
    _ = claim.add_argument("--remote", type=remote_host_argument, default=NO_REMOTE_JOB)
    _ = claim.add_argument("--copy", action="store_true")

    _ = claim.add_argument("--leased-slot", dest="leased_slots", type=int, action="append", default=[])
    _ = claim.add_argument("--from-pid", type=int, default=0)
    wait_claim = commands.add_parser("claim-wait")
    _ = wait_claim.add_argument("--pid", type=int, required=True)
    _ = wait_claim.add_argument("--settings", default="")
    _ = wait_claim.add_argument("--repo-root", required=True)
    _ = wait_claim.add_argument("--package", default="")
    _ = wait_claim.add_argument("--verb", choices=("nextest", "check", "doc", "clippy", "mend-check"), default="nextest")

    release = commands.add_parser("release")
    _ = release.add_argument("--pid", type=int, required=True)
    _ = release.add_argument("--settings", default="")
    for command in (claim, wait_claim, release):
        for field in ("claim-id", "episode", "attempt"):
            _ = command.add_argument("--" + field, required=command is release, default="")
    for command in (claim, wait_claim):
        _ = command.add_argument("--identity", action="store_true")
        _ = command.add_argument("--assignment-file", default="")
        _ = command.add_argument("--assignment", default="")

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

    def parsed_session(value: str) -> SubmittingSession:
        return OwningSession(value) if value else NoOwningSession()

    for command in (claim, wait_claim, free):
        _ = command.add_argument("--session", type=parsed_session, default=claim_state.CALLING_SESSION)
    for command in commands.choices.values():
        _ = command.add_argument("--target", default=argparse.SUPPRESS)
    return root


def main() -> int:
    override_guard.refuse_overrides(Path(__file__), "mac_test/mac_test.py")
    args = parser().parse_args(namespace=CliArguments())
    try:
        paths = state_paths(args.target)
        return dispatch_command(args, paths)
    except (OSError, UnicodeError, StateReadError) as error:
        print(f"state unreadable: {error}", file=sys.stderr)
        return 12
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2


def dispatch_command(args: CliArguments, paths: StatePaths) -> int:
    if args.command == "claim":
        return command_claim(args, paths, session=args.session)
    if args.command == "claim-wait":
        return command_claim_wait(args, paths, session=args.session)
    if args.command == "release":
        return command_release(args, paths)
    if args.command == "free-now":
        return command_free_now(args, paths)
    if args.command == "status" and args.target:
        return command_target_status(paths)
    try:
        config = read_config(args.target)
    except MacIdleConfigError as error:
        print(f"mac_test: {error}", file=sys.stderr)
        return 2
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
