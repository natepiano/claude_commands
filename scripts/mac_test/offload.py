#!/usr/bin/env python3
"""Run one read-only Cargo judgment on an idle Mac mirror when it is available."""

from __future__ import annotations

import argparse
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
import math
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import traceback
from types import FrameType
from typing import Literal, TextIO, cast, final


sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lint"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "buildlog"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "build_hold"))
import cargo_manifest
import override_guard
import sweep
import treekey


# Version the mirrored repository's development shell exports on the Mac.
MACOSX_DEPLOYMENT_TARGET = "14.0"


MacVerb = Literal["nextest", "check", "clippy", "mend-check", "doc"]


NonBusyMacDeclineReason = Literal[
    "off",
    "repo",
    "linux_only",
    "backoff",
    "blocked",
    "state",
    "unreachable",
    "mac_busy",
    "disk",
    "copy",
]
MacDeclineReason = NonBusyMacDeclineReason | Literal["busy"]


@dataclass(frozen=True)
class KnownRepository:
    """A repository with a stable shared-git-directory name."""

    name: str


class UnknownRepository:
    """A directory that has no readable git repository identity."""


RepositoryIdentity = KnownRepository | UnknownRepository
UNKNOWN_REPOSITORY = UnknownRepository()


class AllMirrors:
    """Every repository's mirror on the Mac."""


MirrorScope = KnownRepository | AllMirrors
ALL_MIRRORS = AllMirrors()


@dataclass(frozen=True)
class KnownTreeKey:
    value: str


@dataclass(frozen=True)
class UnavailableTreeKey:
    pass


TreeKey = KnownTreeKey | UnavailableTreeKey
UNAVAILABLE_TREE_KEY = UnavailableTreeKey()


@dataclass(frozen=True)
class CopiedTree:
    tree_key: TreeKey


@dataclass(frozen=True)
class TreeCopyFailed:
    pass


def read_tree_key(directory: Path) -> TreeKey:
    if key := treekey.tree_key(str(directory)):
        return KnownTreeKey(key)
    return UNAVAILABLE_TREE_KEY


@dataclass(frozen=True)
class PassedOnMac:
    """A completed Mac run with a successful test status."""

    seconds: float


@dataclass(frozen=True)
class FailedOnMac:
    """A completed Mac run with its nonzero test status."""

    status: int
    seconds: float


@dataclass(frozen=True)
class NoTestMatchedOnMac:
    """A completed Mac run whose filter selected no test."""

    seconds: float


@dataclass(frozen=True)
class LostMacRun:
    """A Mac run whose final test status was not received."""

    seconds: float


@dataclass(frozen=True)
class DeclinedMacRun:
    """An attempt declined before a Mac test command began."""

    reason: NonBusyMacDeclineReason
    seconds: float


@dataclass(frozen=True)
class KnownMacJob:
    what: str


@dataclass(frozen=True)
class LostRunLeftovers:
    pass


@dataclass(frozen=True)
class UnreadableMacHolder:
    pass


MacHolder = KnownMacJob | LostRunLeftovers | UnreadableMacHolder


@dataclass(frozen=True)
class BusyMacDecline:
    holder: MacHolder
    seconds: float

    @property
    def reason(self) -> Literal["busy"]:
        return "busy"


def claim_holder(stdout: str) -> MacHolder:
    match = re.fullmatch(r"busy: (.+) since (?:[01][0-9]|2[0-3]):[0-5][0-9]\n?", stdout)
    return KnownMacJob(match.group(1)) if match is not None else UnreadableMacHolder()


def holder_value(holder: MacHolder) -> str:
    if isinstance(holder, KnownMacJob):
        return holder.what
    return "leftovers" if isinstance(holder, LostRunLeftovers) else "unreadable"


MacOffloadResult = (
    PassedOnMac | FailedOnMac | NoTestMatchedOnMac | LostMacRun | DeclinedMacRun | BusyMacDecline
)


@dataclass(frozen=True)
class ResultFile:
    """The wire-result destination requested by verify.sh."""

    path: Path


class NoResultFile:
    """A caller that did not request a wire-result file."""


ResultDestination = ResultFile | NoResultFile
NO_RESULT_FILE = NoResultFile()


@dataclass(frozen=True)
class NoPriorMacDecline:
    """This call has not shown a busy or blocked decline for this target."""


@dataclass(frozen=True)
class PriorMacDecline:
    """The decline already shown by this target's previous attempt."""

    reason: Literal["busy", "blocked"]


LastMacDecline = NoPriorMacDecline | PriorMacDecline
NO_PRIOR_MAC_DECLINE = NoPriorMacDecline()


@dataclass(frozen=True)
class DefaultMacSettings:
    """Select settings through MAC_TEST_CONFIG or its existing default."""


DEFAULT_MAC_SETTINGS = DefaultMacSettings()


@dataclass(frozen=True)
class OffloadConfig:
    """Settings that govern one Mac offload decision."""

    enabled: bool
    host: str
    connect_timeout_s: float
    probe_timeout_s: float
    unreachable_backoff_s: float
    free_floor_gib: float
    default_target_budget_gib: str
    values: dict[str, str]

    @property
    def leftover_timeout_s(self) -> float:
        """Bound the cleanup SSH call independently of the reachability probe."""
        return positive_number(self.values, "leftover_timeout_s", 10.0)


@dataclass(frozen=True)
class RepositoryWorkingTree:
    pass


@dataclass(frozen=True)
class StagedDocSnapshot:
    root: Path


MacSource = RepositoryWorkingTree | StagedDocSnapshot


@dataclass(frozen=True)
class OffloadRequest:
    """One Mac verb, its arguments and the metadata sent with it.

    nextest_words keeps the existing caller API; it holds arguments for any verb.
    """

    repo_root: Path
    package: str
    call_id: str
    result: ResultDestination
    nextest_words: tuple[str, ...]
    last_decline: LastMacDecline = NO_PRIOR_MAC_DECLINE
    copy: bool = False
    verb: MacVerb = "nextest"
    source: MacSource = RepositoryWorkingTree()

    @property
    def source_root(self) -> Path:
        return self.source.root if isinstance(self.source, StagedDocSnapshot) else self.repo_root


@dataclass(frozen=True)
class ProbeSnapshot:
    """Mac availability values returned by one remote probe."""

    free_gib: float
    hostname: str


@dataclass(frozen=True)
class LeftoversEnded:
    """Both selectors completed and found no remaining remote work."""


@dataclass(frozen=True)
class LeftoversRemain:
    count: int


@dataclass(frozen=True)
class LeftoverCheckFailed:
    reason: str


LeftoverCheckResult = LeftoversEnded | LeftoversRemain | LeftoverCheckFailed


@dataclass(frozen=True)
class CommandOutput:
    """Exit status and text from a command that completed."""

    status: int
    stdout: str
    stderr: str


class CommandTimedOut:
    """A command that exceeded its assigned time."""


class CommandCouldNotStart:
    """A command whose process could not be created."""


CapturedCommandResult = CommandOutput | CommandTimedOut | CommandCouldNotStart
COMMAND_TIMED_OUT = CommandTimedOut()
COMMAND_COULD_NOT_START = CommandCouldNotStart()


@dataclass(frozen=True)
class FreshMacClaim:
    """New capacity with no remote job started under it."""


@dataclass(frozen=True)
class TakenOverMacClaim:
    """Capacity inherited from a lost job on the host in the claim's reply."""

    host: str


@dataclass(frozen=True)
class TakeoverHostUnreadable:
    """A takeover reply that cannot name the host for its leftover check."""


@dataclass(frozen=True)
class MacClaimNotConfirmed:
    """A refusal or failed call that supplies no confirmed claim to release."""

    reason: MacDeclineReason
    stdout: str
    stderr: str


MacClaimAnswer = FreshMacClaim | TakenOverMacClaim | TakeoverHostUnreadable | MacClaimNotConfirmed


class IgnoredPathListingFailed:
    """Git did not provide the repository's ignored paths."""


IgnoredPathListingResult = tuple[str, ...] | IgnoredPathListingFailed
IGNORED_PATH_LISTING_FAILED = IgnoredPathListingFailed()


@dataclass(frozen=True)
class CommandTimeLimit:
    """Maximum seconds allowed for one child command."""

    seconds: float


class NoCommandTimeLimit:
    """A child command allowed to run until it completes."""


CommandDeadline = CommandTimeLimit | NoCommandTimeLimit
NO_COMMAND_TIME_LIMIT = NoCommandTimeLimit()


class IdleProcess:
    """No child process is active."""


@dataclass(frozen=True)
class RunningProcess:
    """The child process currently owned by the runner."""

    process: subprocess.Popen[str]


class NoClaimInterrupt:
    """No signal arrived while the local claim was completing."""


NO_CLAIM_INTERRUPT = NoClaimInterrupt()


@dataclass
class LocalClaimAttempt:
    """A local claim whose answer is retained before a signal takes effect."""

    answer: MacClaimAnswer = MacClaimNotConfirmed("state", "", "")
    interruption: NoClaimInterrupt | OffloadInterrupted = NO_CLAIM_INTERRUPT


ActiveProcess = IdleProcess | RunningProcess | LocalClaimAttempt
IDLE_PROCESS = IdleProcess()


@dataclass(frozen=True)
class CleanupDone:
    """The remote mirror's build output was removed."""


@dataclass(frozen=True)
class CleanupFailed:
    """The remote delete returned a nonzero status."""

    rc: int


@dataclass(frozen=True)
class CleanupSkipped:
    """The run selected another target directory."""


@dataclass(frozen=True)
class CleanupKept:
    """The mirror's build output was kept after the free-space check."""


@dataclass(frozen=True)
class CleanupUnconfirmed:
    """The remote command did not report a usable cleanup status."""


CleanupOutcome = CleanupDone | CleanupFailed | CleanupSkipped | CleanupKept | CleanupUnconfirmed


@dataclass(frozen=True)
class ReportedMacStatus:
    """The test status printed by the remote command."""

    status: int
    cleanup: CleanupOutcome = CleanupUnconfirmed()


class MissingMacStatus:
    """The remote command ended without a usable test status."""


RemoteRunStatus = ReportedMacStatus | MissingMacStatus
MISSING_MAC_STATUS = MissingMacStatus()


class UnavailableProbe:
    """A probe that did not return a complete availability snapshot."""


ProbeResult = ProbeSnapshot | UnavailableProbe
UNAVAILABLE_PROBE = UnavailableProbe()


@dataclass(frozen=True)
class ReachableHost:
    """The host completed a command over SSH."""


@dataclass(frozen=True)
class UnreachableHost:
    """The host could not complete the reachability probe."""

    reason: str


HostReachability = ReachableHost | UnreachableHost


class NoBackoff:
    """No active unreachable interval exists."""


class ActiveBackoff:
    """The last failed probe still suppresses remote work."""


BackoffState = NoBackoff | ActiveBackoff
NO_BACKOFF = NoBackoff()
ACTIVE_BACKOFF = ActiveBackoff()


@final
class OffloadInterrupted(Exception):
    """A signal that requires child cleanup and claim release."""

    def __init__(self, signal_number: int) -> None:
        super().__init__(signal_number)
        self.signal_number = signal_number


class CliArguments(argparse.Namespace):
    """Arguments accepted by the offload command."""

    def __init__(self) -> None:
        super().__init__()
        self.command: str = ""
        self.repo_root: str = ""
        self.source_root: str = ""
        self.package: str = ""
        self.call_id: str = ""
        self.filter_run: bool = False
        self.result: str | None = None
        self.settings: str = ""
        self.last_decline: str = ""
        self.copy: bool = False
        self.nextest_words: list[str] = []
        self.verb: MacVerb = "nextest"


class ProcessController:
    """Starts one child at a time and exposes it to signal cleanup."""

    def __init__(self) -> None:
        self.active: ActiveProcess = IDLE_PROCESS

    def stop(self) -> None:
        active = self.active
        if isinstance(active, LocalClaimAttempt):
            return
        self.active = IDLE_PROCESS
        if not isinstance(active, RunningProcess):
            return
        process = active.process
        if process.poll() is not None:
            return
        process.terminate()
        try:
            _ = process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            _ = process.wait()

    def defer_claim_interrupt(self, signal_number: int) -> bool:
        if not isinstance(self.active, LocalClaimAttempt):
            return False
        self.active.interruption = OffloadInterrupted(signal_number)
        return True

    def complete_claim(self, arguments: Sequence[str], attempt: LocalClaimAttempt) -> None:
        """Read the bounded local claim's reply even when the runner is signalled."""
        self.active = attempt
        try:
            try:
                process = subprocess.Popen(
                    arguments,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    start_new_session=True,
                )
            except OSError:
                attempt.answer = read_claim_answer(COMMAND_COULD_NOT_START)
            else:
                stdout, stderr = process.communicate()
                attempt.answer = read_claim_answer(CommandOutput(process.returncode, stdout, stderr))
        finally:
            self.active = IDLE_PROCESS

    def capture(
        self,
        arguments: Sequence[str],
        deadline: CommandDeadline = NO_COMMAND_TIME_LIMIT,
    ) -> CapturedCommandResult:
        try:
            process = subprocess.Popen(
                arguments,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except OSError:
            return COMMAND_COULD_NOT_START
        self.active = RunningProcess(process)
        try:
            try:
                timeout = (
                    deadline.seconds
                    if isinstance(deadline, CommandTimeLimit)
                    else None
                )
                stdout, stderr = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                _, _ = process.communicate()
                return COMMAND_TIMED_OUT
            return CommandOutput(process.returncode, stdout, stderr)
        finally:
            self.active = IDLE_PROCESS

    def stream_mac_run(self, arguments: Sequence[str]) -> RemoteRunStatus:
        try:
            process = subprocess.Popen(
                arguments,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except OSError:
            return MISSING_MAC_STATUS
        raw_output = process.stdout
        if raw_output is None:
            process.kill()
            _ = process.wait()
            return MISSING_MAC_STATUS
        output = cast(TextIO, raw_output)
        self.active = RunningProcess(process)
        status: RemoteRunStatus = MISSING_MAC_STATUS
        cleanup: CleanupOutcome = CleanupUnconfirmed()
        held_status_line = ""
        try:
            for line in output:
                marker = line.rstrip("\r\n")
                status_match = re.fullmatch(r"mac_exit=([0-9]+)", marker)
                if status_match is not None:
                    if held_status_line:
                        print(held_status_line, end="", flush=True)
                    status_digits = status_match.group(1).lstrip("0") or "0"
                    mac_status = int(status_digits) if len(status_digits) <= 3 else 256
                    status = ReportedMacStatus(mac_status) if mac_status <= 255 else MISSING_MAC_STATUS
                    if isinstance(status, ReportedMacStatus):
                        held_status_line = line
                    else:
                        held_status_line = ""
                        print(line, end="", flush=True)
                    cleanup = CleanupUnconfirmed()
                    continue
                if marker.startswith("mac_cleanup="):
                    cleanup = parse_cleanup(marker.removeprefix("mac_cleanup="))
                    continue
                if held_status_line:
                    print(held_status_line, end="", flush=True)
                    held_status_line = ""
                status = MISSING_MAC_STATUS
                cleanup = CleanupUnconfirmed()
                print(line, end="", flush=True)
            _ = process.wait()
        finally:
            self.active = IDLE_PROCESS
            output.close()
        if isinstance(status, ReportedMacStatus):
            return ReportedMacStatus(status.status, cleanup)
        return MISSING_MAC_STATUS


def parse_cleanup(value: str) -> CleanupOutcome:
    if value == "skipped":
        return CleanupSkipped()
    if value == "kept":
        return CleanupKept()
    if not re.fullmatch(r"[0-9]+", value):
        return CleanupUnconfirmed()
    digits = value.lstrip("0") or "0"
    if len(digits) > 3:
        return CleanupUnconfirmed()
    rc = int(digits)
    if rc > 255:
        return CleanupUnconfirmed()
    return CleanupDone() if rc == 0 else CleanupFailed(rc)


def report_cleanup(cleanup: CleanupOutcome) -> None:
    if isinstance(cleanup, CleanupFailed):
        print(f"Mac build output not removed: {cleanup.rc}", flush=True)
    elif isinstance(cleanup, CleanupSkipped):
        print("Mac build output kept: the run used another target dir", flush=True)
    elif isinstance(cleanup, CleanupUnconfirmed):
        print("Mac build output cleanup unconfirmed", flush=True)


def config_path() -> Path:
    configured = os.environ.get("MAC_TEST_CONFIG", "~/.claude/config/mac_test.conf")
    return Path(configured).expanduser()


def state_directory() -> Path:
    configured = os.environ.get("MAC_TEST_STATE_DIR", "~/.local/state/mac-test")
    return Path(configured).expanduser()


def positive_number(values: dict[str, str], key: str, default: float) -> float:
    try:
        value = float(values.get(key, str(default)))
    except ValueError:
        return default
    return value if math.isfinite(value) and value > 0 else default


def read_config(path: Path | DefaultMacSettings = DEFAULT_MAC_SETTINGS) -> OffloadConfig:
    settings = config_path() if isinstance(path, DefaultMacSettings) else path.expanduser()
    values = sweep.config_values(str(settings))
    return OffloadConfig(
        enabled=values.get("offload") == "on",
        host=values.get("host", "mac"),
        connect_timeout_s=positive_number(values, "connect_timeout_s", 6.0),
        probe_timeout_s=positive_number(values, "probe_timeout_s", 15.0),
        unreachable_backoff_s=positive_number(
            values, "unreachable_backoff_s", 300.0
        ),
        free_floor_gib=positive_number(values, "free_floor_gib", 60.0),
        default_target_budget_gib=values.get("mac_budget_gib", "24"),
        values=values,
    )


def repository_identity(repo_root: Path) -> RepositoryIdentity:
    name = sweep.repo_name(str(repo_root))
    return KnownRepository(name) if name is not None else UNKNOWN_REPOSITORY


def comma_values(value: str) -> set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


def result_destination(path: str | None) -> ResultDestination:
    return ResultFile(Path(path)) if path is not None else NO_RESULT_FILE


def elapsed_seconds(started: float) -> float:
    return max(0.0, time.monotonic() - started)


def declined(reason: NonBusyMacDeclineReason, started: float) -> DeclinedMacRun:
    return DeclinedMacRun(reason, elapsed_seconds(started))


def decline_already_shown(reason: MacDeclineReason, last_decline: LastMacDecline) -> bool:
    return isinstance(last_decline, PriorMacDecline) and reason == last_decline.reason


def write_result(
    result: MacOffloadResult,
    destination: ResultDestination,
    last_decline: LastMacDecline = NO_PRIOR_MAC_DECLINE,
    verb: MacVerb = "nextest",
) -> int:
    """Write the wire form and return the exit status for one outcome."""
    if isinstance(result, PassedOnMac):
        mac = "passed"
        reason = ""
        status = 0
    elif isinstance(result, NoTestMatchedOnMac):
        mac = "declined"
        reason = "no_tests"
        status = 75
        print("mac_test: no test matched on the Mac; running on natedev instead")
    elif isinstance(result, FailedOnMac):
        mac = "failed"
        reason = ""
        status = result.status
        advice = (
            "only a test that must run on Linux may rerun with --local to run it on natedev."
            if verb == "nextest" else "fix the finding in the local worktree; the mirror sends no edits back."
        )
        print("mac_test: this ran on the Mac (macOS). Treat the failure as real and fix it; " + advice)
    elif isinstance(result, LostMacRun):
        mac = "lost"
        reason = ""
        status = 75
        print("mac_test: the link to the Mac dropped; running on natedev instead")
    else:
        mac = "declined"
        reason = result.reason
        status = 75
        if not decline_already_shown(result.reason, last_decline):
            print(f"mac_test: not running on the Mac ({reason})")
    if isinstance(destination, ResultFile):
        holder_line = f"holder={holder_value(result.holder)}\n" if isinstance(result, BusyMacDecline) else ""
        _ = destination.path.write_text(
            f"mac={mac}\nreason={reason}\nseconds={result.seconds:.6f}\n" + holder_line,
            encoding="utf-8",
        )
    return status


def backoff_path() -> Path:
    return state_directory() / "unreachable_until"


def read_backoff(now: float) -> BackoffState:
    path = backoff_path()
    try:
        until = float(path.read_text(encoding="utf-8").strip())
    except FileNotFoundError:
        return NO_BACKOFF
    except (OSError, ValueError):
        return NO_BACKOFF
    if not math.isfinite(until):
        return NO_BACKOFF
    return ACTIVE_BACKOFF if until > now else NO_BACKOFF


def write_backoff(until: float) -> None:
    path = backoff_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as output:
            temporary_path = Path(output.name)
            try:
                _ = output.write(f"{until:.6f}\n")
                output.flush()
                os.replace(temporary_path, path)
            finally:
                temporary_path.unlink(missing_ok=True)
    except OSError:
        return


def ssh_options(config: OffloadConfig) -> list[str]:
    return [
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={config.connect_timeout_s:g}",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=3",
    ]


def ssh_prefix(config: OffloadConfig) -> list[str]:
    return ["ssh", *ssh_options(config), config.host]


def remote_mirror(repository: KnownRepository) -> str:
    return f".local/state/mac-test/mirror/{repository.name}"


def probe_script(repository: KnownRepository) -> str:
    mirror = shlex.quote(remote_mirror(repository))
    return "\n".join(
        (
            f'mkdir -p "$HOME"/{mirror} || exit 1',
            "free_gib=$(df -g \"$HOME\" | awk 'NR == 2 {print $4}')",
            "printf 'free_gib=%s\\nhost=%s\\nrc=0\\n' "
            + '"$free_gib" "$(hostname -s)"',
        )
    )


def parse_probe(output: str) -> ProbeResult:
    values: dict[str, str] = {}
    for line in output.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value
    if values.get("rc") != "0":
        return UNAVAILABLE_PROBE
    try:
        free_gib = float(values["free_gib"])
        hostname = values["host"]
    except (KeyError, ValueError):
        return UNAVAILABLE_PROBE
    if not math.isfinite(free_gib) or not hostname:
        return UNAVAILABLE_PROBE
    return ProbeSnapshot(free_gib, hostname)


def claim_arguments(request: OffloadRequest, config: OffloadConfig) -> list[str]:
    copy_words = ["--copy"] if request.copy else []
    return [
        sys.executable,
        str(Path(__file__).with_name("mac_test.py")),
        "claim",
        "--pid",
        str(os.getpid()),
        "--what",
        f"{request.verb} {request.package or 'workspace'}",
        "--worktree",
        str(request.repo_root),
        "--remote",
        config.host,
        *copy_words,
    ]


def release_claim(controller: ProcessController) -> None:
    _ = controller.capture(
        [
            sys.executable,
            str(Path(__file__).with_name("mac_test.py")),
            "release",
            "--pid",
            str(os.getpid()),
        ]
    )


def read_claim_answer(result: CapturedCommandResult) -> MacClaimAnswer:
    if not isinstance(result, CommandOutput):
        return MacClaimNotConfirmed("state", "", "")
    if result.status == 0:
        return FreshMacClaim()
    if result.status == 13:
        hosts = [line.removeprefix("leftovers: ").strip() for line in result.stdout.splitlines()
                 if line.startswith("leftovers: ")]
        if len(hosts) == 1 and hosts[0]:
            return TakenOverMacClaim(hosts[0])
        return TakeoverHostUnreadable()
    reason: MacDeclineReason = (
        "blocked" if result.status == 10 else "busy" if result.status == 11 else "state"
    )
    return MacClaimNotConfirmed(reason, result.stdout, result.stderr)


def probe_reachability(
    controller: ProcessController,
    config: OffloadConfig,
) -> HostReachability:
    """Check SSH reachability without creating or inspecting a repository."""
    result = controller.capture(
        [*ssh_prefix(config), "true"],
        CommandTimeLimit(config.probe_timeout_s),
    )
    if isinstance(result, CommandOutput) and result.status == 0:
        return ReachableHost()
    return UnreachableHost(f"SSH probe failed for {config.host}")


def probe_mac(
    controller: ProcessController,
    config: OffloadConfig,
    repository: KnownRepository,
) -> ProbeResult:
    result = controller.capture(
        [*ssh_prefix(config), probe_script(repository)],
        CommandTimeLimit(config.probe_timeout_s),
    )
    if isinstance(result, CommandTimedOut):
        reason = f"SSH probe timed out after {config.probe_timeout_s:g} s"
    elif isinstance(result, CommandCouldNotStart):
        reason = f"SSH probe could not start (time limit {config.probe_timeout_s:g} s)"
    elif result.status != 0:
        reason = " ".join(result.stderr.split()) or f"SSH probe exited with status {result.status}"
    else:
        probe = parse_probe(result.stdout)
        if isinstance(probe, ProbeSnapshot):
            return probe
        reason = "the probe answer could not be read"
    print(f"mac_test: could not reach the Mac: {reason}", file=sys.stderr, flush=True)
    return UNAVAILABLE_PROBE


def ignored_paths(
    controller: ProcessController, repo_root: Path
) -> IgnoredPathListingResult:
    result = controller.capture(
        [
            "git",
            "-C",
            str(repo_root),
            "ls-files",
            "--others",
            "--ignored",
            "--exclude-standard",
            "--directory",
            "-z",
        ]
    )
    if not isinstance(result, CommandOutput) or result.status != 0:
        return IGNORED_PATH_LISTING_FAILED
    return tuple(path for path in result.stdout.split("\0") if path)


def escape_rsync_filter_path(path: str) -> str:
    escaped = ""
    for character in path:
        if character in "\\*?[":
            escaped += "\\"
        escaped += character
    return escaped


def copy_tree(
    controller: ProcessController,
    config: OffloadConfig,
    request: OffloadRequest,
    repository: KnownRepository,
) -> bool:
    ignored = () if isinstance(request.source, StagedDocSnapshot) else ignored_paths(controller, request.repo_root)
    if isinstance(ignored, IgnoredPathListingFailed):
        return False
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", prefix="mac-test-exclude-", delete=False
        ) as exclude_file:
            exclude_path = Path(exclude_file.name)
            try:
                _ = exclude_file.write("/.git\n/target/\n")
                for path in ignored:
                    _ = exclude_file.write(f"/{escape_rsync_filter_path(path)}\n")
                exclude_file.flush()
                result = controller.capture(
                    [
                        "rsync",
                        "-rlpc",
                        "--delete",
                        f"--exclude-from={exclude_path}",
                        "-e",
                        shlex.join(["ssh", *ssh_options(config)]),
                        f"{request.source_root}/",
                        f"{config.host}:{remote_mirror(repository)}/",
                    ]
                )
                return isinstance(result, CommandOutput) and result.status == 0
            finally:
                exclude_path.unlink(missing_ok=True)
    except OSError:
        return False


def copy_tree_with_key(
    controller: ProcessController,
    config: OffloadConfig,
    request: OffloadRequest,
    repository: KnownRepository,
) -> CopiedTree | TreeCopyFailed:
    before = read_tree_key(request.source_root)
    if not copy_tree(controller, config, request, repository):
        return TreeCopyFailed()
    after = read_tree_key(request.source_root)
    if isinstance(before, KnownTreeKey) and isinstance(after, KnownTreeKey) and before == after:
        return CopiedTree(before)
    return CopiedTree(UNAVAILABLE_TREE_KEY)


def nextest_words_with_skip(
    words: tuple[str, ...], skip_expression: str
) -> tuple[str, ...]:
    if not skip_expression:
        return words
    updated = list(words)
    try:
        expression_index = updated.index("-E") + 1
    except ValueError:
        return words
    if expression_index >= len(updated):
        return words
    updated[expression_index] = (
        f"({updated[expression_index]}) & not ({skip_expression})"
    )
    return tuple(updated)


def remote_test_command(
    config: OffloadConfig,
    request: OffloadRequest,
    repository: KnownRepository,
    sent_tree_key: TreeKey = UNAVAILABLE_TREE_KEY,
) -> str:
    budget = config.values.get(
        f"mac_budget_gib.{repository.name}", config.default_target_budget_gib
    )
    words = request.nextest_words
    if request.verb == "nextest":
        words = nextest_words_with_skip(words, config.values.get(f"mac_skip.{repository.name}", ""))
    invocation = shlex.join([request.verb, *words])
    sent_tree = (
        f"BUILDLOG_SENT_TREE_KEY={shlex.quote(sent_tree_key.value)} "
        if isinstance(sent_tree_key, KnownTreeKey) else ""
    )
    if any(word == "--target-dir" or word.startswith("--target-dir=") for word in words):
        cleanup_command = "echo mac_cleanup=skipped"
    else:
        target = shlex.quote(f"{remote_mirror(repository)}/target")
        floor = shlex.quote(f"{config.free_floor_gib:g}")
        cleanup_command = (
            'if free_gib=$(df -g "$HOME"); then '
            + "free_gib=$(printf '%s\\n' \"$free_gib\" | awk 'NR == 2 {print $4}'); "
            + 'else free_gib=; fi; '
            + f'if awk -v free="$free_gib" -v floor={floor} '
            + "'BEGIN { exit !(free ~ /^[0-9]+([.][0-9]+)?$/ && (free + 0) < (floor + 0)) }'; then "
            + f'if rm -rf -- "$HOME"/{target}; then echo mac_cleanup=0; '
            + "else echo mac_cleanup=$?; fi; else echo mac_cleanup=kept; fi"
        )
    forced_config = "LINT_CONFIG_FORCE=1 " if request.verb == "doc" else ""
    return (
        f"cd {shlex.quote(remote_mirror(repository))} && "
        'PATH="$HOME/.cargo/bin:$PATH" '
        "BUILDLOG_CALLER=verify-mac "
        f"BUILDLOG_CALL_ID={shlex.quote(request.call_id)} "
        f"{sent_tree}"
        "BUILDLOG_SYNC=1 "
        "LINT_SKIP_SWEEP=1 "
        f"{forced_config}"
        f"LINT_SWEEP_BUDGET_GIB={shlex.quote(budget)} "
        f"MACOSX_DEPLOYMENT_TARGET={MACOSX_DEPLOYMENT_TARGET} "
        f"~/.claude/scripts/lint/lint {invocation}; echo mac_exit=$?; {cleanup_command}"
    )


def extended_regex_literal(text: str) -> str:
    """Match text literally in pgrep's and awk's extended regular expressions.

    A character that is special inside a bracket (`]`, `^`, `\\`) matches any
    character instead, which can only widen the selection by that position.
    """
    return "".join(
        character if character.isalnum() or character in "_-"
        else "." if character in "]^\\"
        else f"[{character}]"
        for character in text
    )


def mirror_selector_script(scope: MirrorScope) -> str:
    """Define select_leftovers: the user's processes running from the scoped mirrors.

    A repository's scope stops at its own mirror, so its leftover check never
    reaches another repository's run. The `[.]` keeps each pattern from
    matching this script's own text.
    """
    if isinstance(scope, KnownRepository):
        name = extended_regex_literal(scope.name)
        command_pattern = f"[.]local/state/mac-test/mirror/{name}([/'\"[:space:]]|$)"
        cwd_pattern = f"^n.*/[.]local/state/mac-test/mirror/{name}(/|$)"
    else:
        command_pattern = "[.]local/state/mac-test/mirror/"
        cwd_pattern = "^n.*/[.]local/state/mac-test/mirror(/|$)"
    return (
        f"mirror_command_pattern={shlex.quote(command_pattern)}\n"
        + f"mirror_cwd_pattern={shlex.quote(cwd_pattern)}\n"
        + r'''select_leftovers() {
    if command_pids=$(pgrep -u "$(id -u)" -f "$mirror_command_pattern"); then
        :
    else
        selector_result=$?
        [ "$selector_result" -eq 1 ] || return 1
    fi
    cwd_records=$(lsof -a -u "$(id -u)" -d cwd -Fpn) || return 1
    cwd_pids=$(printf '%s\n' "$cwd_records" | awk -v pattern="$mirror_cwd_pattern" '/^p/ { pid = substr($0, 2) } $0 ~ pattern { print pid }') || return 1
    printf '%s\n%s\n' "$command_pids" "$cwd_pids" | awk '/^[0-9]+$/ && !seen[$0]++ { print }'
}
'''
    )


def leftover_check_script(repository: KnownRepository) -> str:
    return mirror_selector_script(repository) + r'''
leftover_pids=$(select_leftovers) || exit 1
printf '%s\n' "$leftover_pids" | while IFS= read -r leftover_pid; do
    [ -n "$leftover_pid" ] && kill "$leftover_pid" 2>/dev/null
done
sleep 1 || exit 1
leftover_pids=$(select_leftovers) || exit 1
printf '%s\n' "$leftover_pids" | while IFS= read -r leftover_pid; do
    [ -n "$leftover_pid" ] && kill -9 "$leftover_pid" 2>/dev/null
done
leftover_pids=$(select_leftovers) || exit 1
leftover_count=$(printf '%s\n' "$leftover_pids" | awk 'NF { count++ } END { print count+0 }') || exit 1
printf 'leftovers=%s\nrc=0\n' "$leftover_count"
'''


def parse_leftover_check(output: str) -> LeftoverCheckResult:
    values: dict[str, str] = {}
    for line in output.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value
    count_text = values.get("leftovers", "")
    if values.get("rc") != "0" or not count_text.isascii() or not count_text.isdecimal():
        return LeftoverCheckFailed("the leftover check answer could not be read")
    count = int(count_text)
    return LeftoversEnded() if count == 0 else LeftoversRemain(count)


def check_leftovers(config: OffloadConfig, host: str, repository: KnownRepository) -> LeftoverCheckResult:
    """End and count one mirror's work without exposing the check to a second signal."""
    try:
        result = subprocess.run(
            ["ssh", *ssh_options(config), host, leftover_check_script(repository)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=config.leftover_timeout_s,
        )
    except subprocess.TimeoutExpired:
        return LeftoverCheckFailed(f"SSH leftover check timed out after {config.leftover_timeout_s:g} s")
    except OSError:
        return LeftoverCheckFailed(f"SSH leftover check could not start (time limit {config.leftover_timeout_s:g} s)")
    answer = parse_leftover_check(result.stdout)
    if isinstance(answer, LeftoverCheckFailed):
        reason = " ".join(result.stderr.split())
        if reason:
            return LeftoverCheckFailed(reason)
    return answer


def report_leftover_check(result: LeftoverCheckResult, request: OffloadRequest) -> None:
    if isinstance(result, LeftoversRemain):
        if not decline_already_shown("busy", request.last_decline):
            print(f"mac_test: {result.count} leftover processes of a lost run are still on the Mac; "
                  + "it stays busy until they end", file=sys.stderr, flush=True)
    elif isinstance(result, LeftoverCheckFailed):
        if not decline_already_shown("unreachable", request.last_decline):
            print("mac_test: could not confirm that a lost run's leftovers ended: "
                  + f"{result.reason}; the Mac stays busy", file=sys.stderr, flush=True)


def check_taken_over_claim(
    config: OffloadConfig, claim: TakenOverMacClaim | TakeoverHostUnreadable, repository: KnownRepository
) -> LeftoverCheckResult:
    if isinstance(claim, TakenOverMacClaim):
        return check_leftovers(config, claim.host, repository)
    return LeftoverCheckFailed("the kept claim's host could not be read")


def claim_for_run(
    controller: ProcessController, config: OffloadConfig, request: OffloadRequest, repository: KnownRepository
) -> MacClaimAnswer:
    attempt = LocalClaimAttempt()
    try:
        controller.complete_claim(claim_arguments(request, config), attempt)
        if isinstance(attempt.interruption, OffloadInterrupted):
            raise attempt.interruption
        return attempt.answer
    except OffloadInterrupted:
        claim = attempt.answer
        if isinstance(claim, FreshMacClaim):
            release_claim(controller)
        elif isinstance(claim, (TakenOverMacClaim, TakeoverHostUnreadable)):
            checked = check_taken_over_claim(config, claim, repository)
            report_leftover_check(checked, request)
            if isinstance(checked, LeftoversEnded):
                release_claim(controller)
        raise


def claimed_run(
    controller: ProcessController,
    config: OffloadConfig,
    request: OffloadRequest,
    repository: KnownRepository,
    started: float,
) -> MacOffloadResult:
    release_allowed = True
    try:
        probe = probe_mac(controller, config, repository)
        if isinstance(probe, UnavailableProbe):
            write_backoff(time.time() + config.unreachable_backoff_s)
            return declined("unreachable", started)
        if probe.free_gib < config.free_floor_gib:
            return declined("disk", started)

        copy_started = time.monotonic()
        copied = copy_tree_with_key(controller, config, request, repository)
        if isinstance(copied, TreeCopyFailed):
            return declined("copy", started)
        copy_seconds = elapsed_seconds(copy_started)
        running = (
            f"running a copy on the Mac while natedev compiles (host {probe.hostname})"
            if request.copy
            else f"running on the Mac (macOS, host {probe.hostname})"
        )
        print(f"mac_test: {running}; tree copied in {copy_seconds:.1f} s", flush=True)
        release_allowed = False
        remote_status = controller.stream_mac_run(
            [
                *ssh_prefix(config),
                remote_test_command(config, request, repository, copied.tree_key),
            ]
        )
        seconds = elapsed_seconds(started)
        if isinstance(remote_status, MissingMacStatus):
            checked = check_leftovers(config, config.host, repository)
            release_allowed = isinstance(checked, LeftoversEnded)
            report_leftover_check(checked, request)
            return LostMacRun(elapsed_seconds(started))
        report_cleanup(remote_status.cleanup)
        if isinstance(remote_status.cleanup, CleanupUnconfirmed):
            checked = check_leftovers(config, config.host, repository)
            release_allowed = isinstance(checked, LeftoversEnded)
            report_leftover_check(checked, request)
        else:
            release_allowed = True
        if remote_status.status == 0:
            return PassedOnMac(seconds)
        if remote_status.status == 4 and request.verb == "nextest":
            return NoTestMatchedOnMac(seconds)
        return FailedOnMac(remote_status.status, seconds)
    except OffloadInterrupted:
        if not release_allowed:
            checked = check_leftovers(config, config.host, repository)
            release_allowed = isinstance(checked, LeftoversEnded)
            report_leftover_check(checked, request)
        raise
    finally:
        if release_allowed:
            release_claim(controller)


@dataclass(frozen=True)
class IneligibleMacStep:
    reason: NonBusyMacDeclineReason


def workspace_has_linux_only(repo_root: Path, excluded: set[str]) -> bool:
    # Cargo adds in-tree path dependencies to workspace membership.
    root = repo_root.resolve()
    manifest = cargo_manifest.toml_table(root / "Cargo.toml")
    workspace = cargo_manifest.nested_table(manifest, "workspace")
    shared = cargo_manifest.nested_table(workspace, "dependencies")
    members = set(cargo_manifest.member_directories(root, manifest))
    excluded_paths = cargo_manifest.expanded_directories(root, cargo_manifest.string_sequence(workspace, "exclude"))
    if not members:
        raise ValueError("workspace members cannot be read")
    pending = list(members)
    while pending:
        member = pending.pop()
        values = cargo_manifest.toml_table(member / "Cargo.toml")
        package = cargo_manifest.nested_table(values, "package")
        name = package.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("workspace package name cannot be read")
        if name in excluded:
            return True
        tables = [values]
        for target in cargo_manifest.nested_table(values, "target").values():
            if not isinstance(target, dict):
                raise ValueError("target dependencies cannot be read")
            tables.append(cast(dict[str, object], target))
        for table in tables:
            for section in ("dependencies", "dev-dependencies", "build-dependencies"):
                for dependency_name, dependency in cargo_manifest.nested_table(table, section).items():
                    if not isinstance(dependency, dict):
                        if not isinstance(dependency, str):
                            raise ValueError("dependency cannot be read")
                        continue
                    definition = cast(dict[str, object], dependency)
                    base = member
                    if definition.get("workspace") is True:
                        inherited = shared.get(dependency_name)
                        if not isinstance(inherited, (str, dict)):
                            raise ValueError("workspace dependency cannot be read")
                        if isinstance(inherited, str):
                            continue
                        definition = cast(dict[str, object], inherited)
                        base = root
                    if "path" not in definition:
                        continue
                    path = definition["path"]
                    if not isinstance(path, str) or not path:
                        raise ValueError("dependency path cannot be read")
                    directory = (base / path).resolve()
                    if not cargo_manifest.within(root, directory) or directory in excluded_paths or directory in members:
                        continue
                    members.add(directory)
                    pending.append(directory)
    return False


def mac_step_eligibility(config: OffloadConfig, repo_root: Path, package: str,
                         verb: MacVerb) -> KnownRepository | IneligibleMacStep:
    """Share repository, package/workspace and backoff rules with eligible --kind."""
    if not config.enabled:
        return IneligibleMacStep("off")
    repository = repository_identity(repo_root)
    if isinstance(repository, UnknownRepository):
        return IneligibleMacStep("repo")
    key = f"linux_only.{repository.name}"
    if key not in config.values:
        return IneligibleMacStep("repo")
    excluded = comma_values(config.values[key])
    if verb == "nextest":
        if package in excluded:
            return IneligibleMacStep("linux_only")
    else:
        try:
            if workspace_has_linux_only(repo_root, excluded):
                return IneligibleMacStep("linux_only")
        except (OSError, UnicodeError, ValueError):
            return IneligibleMacStep("repo")
    if isinstance(read_backoff(time.time()), ActiveBackoff):
        return IneligibleMacStep("backoff")
    return repository


def run_offload(
    controller: ProcessController,
    config: OffloadConfig,
    request: OffloadRequest,
    started: float,
) -> MacOffloadResult:
    eligibility = mac_step_eligibility(config, request.repo_root, request.package, request.verb)
    if isinstance(eligibility, IneligibleMacStep):
        return declined(eligibility.reason, started)
    repository = eligibility
    if isinstance(request.source, StagedDocSnapshot):
        try:
            if workspace_has_linux_only(request.source.root, comma_values(config.values[f"linux_only.{repository.name}"])):
                return declined("linux_only", started)
        except (OSError, UnicodeError, ValueError):
            return declined("repo", started)

    claim = claim_for_run(controller, config, request, repository)
    if isinstance(claim, (TakenOverMacClaim, TakeoverHostUnreadable)):
        checked = check_taken_over_claim(config, claim, repository)
        report_leftover_check(checked, request)
        if isinstance(checked, LeftoverCheckFailed):
            write_backoff(time.time() + config.unreachable_backoff_s)
            return declined("unreachable", started)
        if isinstance(checked, LeftoversRemain):
            return BusyMacDecline(LostRunLeftovers(), elapsed_seconds(started))
        release_claim(controller)
        claim = claim_for_run(controller, config, request, repository)
        if isinstance(claim, (TakenOverMacClaim, TakeoverHostUnreadable)):
            return BusyMacDecline(LostRunLeftovers(), elapsed_seconds(started))
    if isinstance(claim, MacClaimNotConfirmed):
        if not decline_already_shown(claim.reason, request.last_decline):
            if claim.stdout:
                print(claim.stdout, end="" if claim.stdout.endswith("\n") else "\n", file=sys.stderr, flush=True)
            if claim.stderr:
                print(claim.stderr, end="" if claim.stderr.endswith("\n") else "\n", file=sys.stderr, flush=True)
        if claim.reason == "busy":
            return BusyMacDecline(claim_holder(claim.stdout), elapsed_seconds(started))
        return declined(claim.reason, started)
    return claimed_run(controller, config, request, repository, started)


@contextmanager
def interrupt_handlers(controller: ProcessController) -> Generator[None, None, None]:
    previous_interrupt = signal.getsignal(signal.SIGINT)
    previous_terminate = signal.getsignal(signal.SIGTERM)
    previous_hangup = signal.getsignal(signal.SIGHUP)

    def stop_for_signal(signal_number: int, frame: FrameType | None) -> None:
        _ = frame
        _ = signal.signal(signal.SIGINT, signal.SIG_IGN)
        _ = signal.signal(signal.SIGTERM, signal.SIG_IGN)
        _ = signal.signal(signal.SIGHUP, signal.SIG_IGN)
        if controller.defer_claim_interrupt(signal_number):
            return
        controller.stop()
        raise OffloadInterrupted(signal_number)

    _ = signal.signal(signal.SIGINT, stop_for_signal)
    _ = signal.signal(signal.SIGTERM, stop_for_signal)
    _ = signal.signal(signal.SIGHUP, stop_for_signal)
    try:
        yield
    finally:
        _ = signal.signal(signal.SIGINT, previous_interrupt)
        _ = signal.signal(signal.SIGTERM, previous_terminate)
        _ = signal.signal(signal.SIGHUP, previous_hangup)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    _ = run.add_argument("--repo-root", required=True)
    _ = run.add_argument("--source-root", default="")
    _ = run.add_argument("--package", default="")
    _ = run.add_argument("--verb", choices=("nextest", "check", "clippy", "mend-check", "doc"), default="nextest")
    _ = run.add_argument("--call-id", required=True)
    _ = run.add_argument("--settings", default="")
    _ = run.add_argument("--last-decline", choices=("busy", "blocked"), default="")
    _ = run.add_argument("--filter-run", action="store_true")
    _ = run.add_argument("--copy", action="store_true")
    _ = run.add_argument("--result")
    _ = run.add_argument("nextest_words", nargs=argparse.REMAINDER)
    return root


def request_from_arguments(arguments: CliArguments) -> OffloadRequest:
    words = arguments.nextest_words
    if words[:1] == ["--"]:
        words = words[1:]
    last_decline: LastMacDecline = NO_PRIOR_MAC_DECLINE
    if arguments.last_decline == "busy":
        last_decline = PriorMacDecline("busy")
    elif arguments.last_decline == "blocked":
        last_decline = PriorMacDecline("blocked")
    return OffloadRequest(
        repo_root=Path(arguments.repo_root).resolve(),
        source=StagedDocSnapshot(Path(arguments.source_root).resolve()) if arguments.source_root else RepositoryWorkingTree(),
        package=arguments.package,
        call_id=arguments.call_id,
        result=result_destination(arguments.result),
        nextest_words=tuple(words),
        last_decline=last_decline,
        copy=arguments.copy,
        verb=arguments.verb,
    )


def restore_ignored_interrupt_default(arguments: CliArguments) -> None:
    if (
        arguments.command == "run"
        and signal.getsignal(signal.SIGINT) == signal.SIG_IGN
    ):
        _ = signal.signal(signal.SIGINT, signal.SIG_DFL)


def main() -> int:
    override_guard.refuse_overrides(Path(__file__), "mac_test/offload.py")
    cli = parser()
    arguments = cli.parse_args(namespace=CliArguments())
    if arguments.verb != "nextest" and (arguments.copy or arguments.filter_run):
        cli.error("--copy and --filter-run apply only to nextest")
    if arguments.source_root and arguments.verb != "doc":
        cli.error("--source-root applies only to a staged doc snapshot")
    restore_ignored_interrupt_default(arguments)
    request = request_from_arguments(arguments)
    config = read_config(Path(arguments.settings)) if arguments.settings else read_config()
    controller = ProcessController()
    started = time.monotonic()
    try:
        with interrupt_handlers(controller):
            result = run_offload(controller, config, request, started)
        return write_result(result, request.result, request.last_decline, request.verb)
    except OffloadInterrupted as interrupted:
        return 128 + interrupted.signal_number
    except Exception:
        traceback.print_exc()
        if isinstance(request.result, ResultFile):
            try:
                request.result.path.unlink(missing_ok=True)
            except OSError:
                pass
        return 75


if __name__ == "__main__":
    raise SystemExit(main())
