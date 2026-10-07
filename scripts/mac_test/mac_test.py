#!/usr/bin/env python3
"""Coordinate exclusive test use of the Mac."""

from __future__ import annotations

import argparse
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from typing import Literal, TypedDict, cast


sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lint"))
import sweep


MESSAGE_TIMEOUT_S = 30.0


class RunRecord(TypedDict):
    """A process that currently owns Mac test capacity."""

    pid: int
    proc_start: str
    what: str
    worktree: str
    since: str


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


@dataclass(frozen=True)
class MacTestConfig:
    """Mac block and CI settings."""

    ci_repo: str
    ci_variable: str
    ci_workflow: str
    ci_job: str
    ci_gate_job: str
    gh_timeout_s: float
    block_hours: float
    block_max_hours: float
    block_warn_minutes: float


@dataclass(frozen=True)
class SettleOutcome:
    """State and diagnostics after one complete settlement pass."""

    run: RunState
    block: MacBlockState
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


class CliArguments(argparse.Namespace):
    """Arguments accepted across the mac-test subcommands."""

    def __init__(self) -> None:
        super().__init__()
        self.command: str = ""
        self.pid: int = 0
        self.what: str = ""
        self.worktree: str = ""
        self.wait: float = 0.0
        self.holder: str = ""
        self.reason: str = ""
        self.hours: str = ""
        self.showrunner: str = ""


class StatePaths:
    """Files used by mac-test coordination."""

    def __init__(self, directory: Path) -> None:
        self.directory: Path = directory
        self.lock: Path = directory / "state.lock"
        self.control_lock: Path = directory / "control.lock"
        self.watch_lock: Path = directory / "watch.lock"
        self.run: Path = directory / "run.json"
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
        ci_gate_job=values.get("ci_gate_job", ""),
        gh_timeout_s=configured_number(values, "gh_timeout_s", 30.0),
        block_hours=configured_number(values, "block_hours", 4.0),
        block_max_hours=configured_number(values, "block_max_hours", 48.0),
        block_warn_minutes=configured_number(values, "block_warn_minutes", 15.0),
    )


@contextmanager
def locked(path: Path) -> Generator[None, None, None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as lock_file:
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
    if not isinstance(decoded, dict):
        raise ValueError(f"{path} does not contain a JSON object")
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
        raise ValueError(f"{path} has an invalid run record")
    return {
        "pid": pid,
        "proc_start": proc_start,
        "what": what,
        "worktree": worktree,
        "since": since,
    }


def aware_timestamp(value: object, path: Path, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{path} has an invalid {field}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{path} has an invalid {field}") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{path} has an invalid {field}")
    return parsed


def required_string(values: dict[str, object], key: str, path: Path) -> str:
    value = values.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{path} has an invalid block record")
    return value


def decoded_delivery(session: object, holder: str, path: Path) -> MessageDelivery:
    if session is None:
        return HolderQueue(holder)
    if not isinstance(session, str):
        raise ValueError(f"{path} has an invalid block record")
    return RecordedSession(session, holder)


def decoded_showrunner(showrunner: object, path: Path) -> Showrunner:
    if showrunner is None:
        return NO_SHOWRUNNER
    if not isinstance(showrunner, str):
        raise ValueError(f"{path} has an invalid block record")
    return NamedShowrunner(showrunner)


def decoded_ci(value: object, path: Path) -> CiSwitch:
    if value not in (
        "off_by_this_block",
        "off_before",
        "not_configured",
        "still_on",
        "off_unconfirmed",
    ):
        raise ValueError(f"{path} has an invalid block record")
    return value


def decode_waiting(value: object, path: Path) -> WaitingOn:
    if not isinstance(value, dict):
        raise ValueError(f"{path} has an invalid block record")
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
    raise ValueError(f"{path} has an invalid block record")


def decode_free_message(value: object, path: Path) -> FreeMessage:
    if not isinstance(value, dict):
        raise ValueError(f"{path} has an invalid block record")
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
    raise ValueError(f"{path} has an invalid block record")


def decode_legacy_block(values: dict[str, object], path: Path) -> MacBlockState:
    required = {"holder", "session", "for", "since", "state"}
    if set(values) != required:
        raise ValueError(f"{path} has an invalid block record")
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
    raise ValueError(f"{path} has an invalid block record")


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
        raise ValueError(f"{path} has an invalid block record")
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
    raise ValueError(f"{path} has an invalid block record")


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
    return datetime.now(timezone.utc).replace(microsecond=0)


def local_time(instant: str, include_zone: bool = True) -> str:
    format_string = "%H:%M %Z" if include_zone else "%H:%M"
    return datetime.fromisoformat(instant).astimezone().strftime(format_string)


def block_time(instant: datetime) -> str:
    return instant.astimezone().strftime("%a %H:%M %Z")


def message_time(instant: datetime) -> str:
    return instant.astimezone().strftime("%H:%M %Z")


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
    return process_start_time(run["pid"]) == run["proc_start"]


def live_run(paths: StatePaths) -> RunState:
    run = read_run(paths.run)
    if isinstance(run, dict) and not run_is_live(run):
        paths.run.unlink(missing_ok=True)
        return NO_RUN
    return run


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
        for raw_job in jobs:
            if not isinstance(raw_job, dict):
                return CiUnknown("gh returned invalid JSON")
            job = cast(dict[str, object], raw_job)
            name = job.get("name")
            occupies_mac = name == config.ci_job or (
                bool(config.ci_gate_job) and name == config.ci_gate_job
            )
            if (
                occupies_mac
                and job.get("status") != "completed"
            ):
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
        f">={timestamp(since)}",
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
    run: RunState,
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
                run, block, ProblemLine(restored.line), NO_PROBLEM
            )
    with locked(paths.lock):
        paths.block.unlink(missing_ok=True)
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
    return SettleOutcome(run, NO_MAC_BLOCK, NO_PROBLEM, NO_PROBLEM)


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
        else "CI's Mac job"
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
        run = live_run(paths)
        block = read_block(paths.block)
    if isinstance(block, NoMacBlock):
        return SettleOutcome(run, block, NO_PROBLEM, NO_PROBLEM)
    now = utc_now()
    if now >= block.expires:
        outcome = expire_block(paths, config, run, block)
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
            run = live_run(paths)
        if isinstance(run, dict):
            block = pending_with_wait(block, WaitingOnLocalRun(run["what"]))
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
    return SettleOutcome(run, block, ci_problem, waiting_problem)


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


def command_claim(args: CliArguments, paths: StatePaths) -> int:
    deadline = time.monotonic() + max(args.wait, 0.0)
    while True:
        with locked(paths.lock):
            run = live_run(paths)
            block = read_block(paths.block)
            if not isinstance(block, NoMacBlock):
                print(f"blocked by {block.holder}: {block.reason}")
                return 10
            if isinstance(run, NoRun):
                proc_start = process_start_time(args.pid)
                if proc_start is None:
                    print(
                        f"cannot claim: process {args.pid} is not running",
                        file=sys.stderr,
                    )
                    return 1
                claimed: RunRecord = {
                    "pid": args.pid,
                    "proc_start": proc_start,
                    "what": args.what,
                    "worktree": args.worktree,
                    "since": timestamp(utc_now()),
                }
                write_json(paths.run, claimed)
                print("claimed")
                return 0
            busy_run = run

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            print(
                f"busy: {busy_run['what']} since "
                + f"{local_time(busy_run['since'], include_zone=False)}"
            )
            return 11
        time.sleep(min(2.0, remaining))


def command_release(args: CliArguments, paths: StatePaths) -> int:
    with locked(paths.lock):
        run = live_run(paths)
        if isinstance(run, dict) and run["pid"] == args.pid:
            paths.run.unlink(missing_ok=True)
        block_present = not isinstance(read_block(paths.block), NoMacBlock)
    if block_present:
        start_watcher(paths)
    return 0


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
            f"Block pending for {block.holder}: CI's Mac job is running. "
            + "Nothing new starts there, and you get a message when it ends."
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
            run = live_run(paths)
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
                if isinstance(run, dict):
                    block: PendingMacBlock | ActiveMacBlock = PendingMacBlock(
                        args.holder,
                        args.reason,
                        now,
                        expires,
                        requested_delivery(args.holder),
                        requested_showrunner(args.showrunner),
                        "still_on",
                        WaitingOnLocalRun(run["what"]),
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
        outcome = full_settle(
            paths,
            config,
            activation_message_needed=not new_block,
        )
        if isinstance(outcome.block, NoMacBlock):
            return 0
        print(block_state_line(outcome.block, outcome.waiting_problem))
        if ci_may_still_be_on(outcome.block.ci):
            line = (
                outcome.ci_problem.line
                if isinstance(outcome.ci_problem, ProblemLine)
                else "unknown error"
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
            paths.block.unlink(missing_ok=True)
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
        return "CI's Mac job is running"
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
        if isinstance(outcome.run, NoRun):
            print("free")
        else:
            print(
                f"running: {outcome.run['what']} ({outcome.run['worktree']}) "
                + f"since {local_time(outcome.run['since'])}"
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
            print_switch_with_block(block, outcome.ci_problem)
        else:
            print(
                f"blocked by {block.holder} since {block_time(block.since)}, "
                + f"lifts {block_time(block.expires)}: {block.reason}"
            )
            if isinstance(block.free_message, FreeMessageOwed):
                print(
                    f'The "Mac is free" message has not reached {block.holder} yet.'
                )
            print_switch_with_block(block, outcome.ci_problem)
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


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)

    claim = commands.add_parser("claim")
    _ = claim.add_argument("--pid", type=int, required=True)
    _ = claim.add_argument("--what", required=True)
    _ = claim.add_argument("--worktree", default=os.getcwd())
    _ = claim.add_argument("--wait", type=float, default=0.0)

    release = commands.add_parser("release")
    _ = release.add_argument("--pid", type=int, required=True)

    block = commands.add_parser("block")
    _ = block.add_argument("--holder", required=True)
    _ = block.add_argument("--for", dest="reason", required=True)
    _ = block.add_argument("--hours", default="")
    _ = block.add_argument("--showrunner", default="")

    unblock = commands.add_parser("unblock")
    _ = unblock.add_argument("--holder", required=True)

    _ = commands.add_parser("status")
    _ = commands.add_parser("watch")
    return root


def main() -> int:
    args = parser().parse_args(namespace=CliArguments())
    paths = state_paths()
    if args.command == "claim":
        return command_claim(args, paths)
    if args.command == "release":
        return command_release(args, paths)
    config = read_config()
    if args.command == "block":
        return command_block(args, paths, config)
    if args.command == "unblock":
        return command_unblock(args, paths, config)
    if args.command == "watch":
        return command_watch(paths, config)
    return command_status(paths, config)


if __name__ == "__main__":
    raise SystemExit(main())
