#!/usr/bin/env python3
"""Run one nextest command on an idle Mac mirror when it is available."""

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
import sweep


# Version the mirrored repository's development shell exports on the Mac.
MACOSX_DEPLOYMENT_TARGET = "14.0"


MacDeclineReason = Literal[
    "off",
    "repo",
    "linux_only",
    "backoff",
    "blocked",
    "busy",
    "state",
    "unreachable",
    "mac_busy",
    "battery",
    "disk",
    "copy",
]
MacPower = Literal["ac", "battery"]


@dataclass(frozen=True)
class KnownRepository:
    """A repository with a stable shared-git-directory name."""

    name: str


class UnknownRepository:
    """A directory that has no readable git repository identity."""


RepositoryIdentity = KnownRepository | UnknownRepository
UNKNOWN_REPOSITORY = UnknownRepository()


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

    reason: MacDeclineReason
    seconds: float


MacOffloadResult = (
    PassedOnMac | FailedOnMac | NoTestMatchedOnMac | LostMacRun | DeclinedMacRun
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


@dataclass(frozen=True)
class OffloadRequest:
    """One nextest invocation and the metadata sent with it."""

    repo_root: Path
    package: str
    call_id: str
    result: ResultDestination
    nextest_words: tuple[str, ...]
    last_decline: LastMacDecline = NO_PRIOR_MAC_DECLINE


@dataclass(frozen=True)
class ProbeSnapshot:
    """Mac availability values returned by one remote probe."""

    power: MacPower
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
class ReportedMacStatus:
    """The test status printed by the remote command."""

    status: int


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
        self.package: str = ""
        self.call_id: str = ""
        self.filter_run: bool = False
        self.result: str | None = None
        self.settings: str = ""
        self.last_decline: str = ""
        self.nextest_words: list[str] = []


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
        held_status_line = ""
        try:
            for line in output:
                if held_status_line:
                    print(held_status_line, end="", flush=True)
                    held_status_line = ""
                if re.fullmatch(r"mac_exit=([0-9]+)", line.rstrip("\r\n")):
                    held_status_line = line
                else:
                    print(line, end="", flush=True)
            _ = process.wait()
        finally:
            self.active = IDLE_PROCESS
            output.close()
        status_match = re.fullmatch(
            r"mac_exit=([0-9]+)", held_status_line.rstrip("\r\n")
        )
        if status_match is None:
            return MISSING_MAC_STATUS
        mac_status = int(status_match.group(1))
        if mac_status > 255:
            return MISSING_MAC_STATUS
        return ReportedMacStatus(mac_status)


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


def declined(reason: MacDeclineReason, started: float) -> DeclinedMacRun:
    return DeclinedMacRun(reason, elapsed_seconds(started))


def decline_already_shown(reason: MacDeclineReason, last_decline: LastMacDecline) -> bool:
    return isinstance(last_decline, PriorMacDecline) and reason == last_decline.reason


def write_result(
    result: MacOffloadResult,
    destination: ResultDestination,
    last_decline: LastMacDecline = NO_PRIOR_MAC_DECLINE,
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
        print(
            "mac_test: this ran on the Mac (macOS). Treat the failure as real "
            + "and fix it; only a test that must run on Linux may rerun with "
            + "--local to run it on natedev."
        )
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
        _ = destination.path.write_text(
            f"mac={mac}\nreason={reason}\nseconds={result.seconds:.6f}\n",
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
            "case \"$(pmset -g batt | head -1)\" in "
            + "*\"AC Power\"*) power=ac ;; *) power=battery ;; esac",
            "free_gib=$(df -g \"$HOME\" | awk 'NR == 2 {print $4}')",
            "printf 'power=%s\\nfree_gib=%s\\nhost=%s\\nrc=0\\n' "
            + '"$power" "$free_gib" "$(hostname -s)"',
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
        power = values["power"]
        hostname = values["host"]
    except (KeyError, ValueError):
        return UNAVAILABLE_PROBE
    if (
        not math.isfinite(free_gib)
        or power not in ("ac", "battery")
        or not hostname
    ):
        return UNAVAILABLE_PROBE
    return ProbeSnapshot(power, free_gib, hostname)


def claim_arguments(request: OffloadRequest, config: OffloadConfig) -> list[str]:
    return [
        sys.executable,
        str(Path(__file__).with_name("mac_test.py")),
        "claim",
        "--pid",
        str(os.getpid()),
        "--what",
        f"nextest {request.package}",
        "--worktree",
        str(request.repo_root),
        "--remote",
        config.host,
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
    ignored = ignored_paths(controller, request.repo_root)
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
                        f"{request.repo_root}/",
                        f"{config.host}:{remote_mirror(repository)}/",
                    ]
                )
                return isinstance(result, CommandOutput) and result.status == 0
            finally:
                exclude_path.unlink(missing_ok=True)
    except OSError:
        return False


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
) -> str:
    budget = config.values.get(
        f"mac_budget_gib.{repository.name}", config.default_target_budget_gib
    )
    words = nextest_words_with_skip(
        request.nextest_words,
        config.values.get(f"mac_skip.{repository.name}", ""),
    )
    invocation = shlex.join(["nextest", *words])
    return (
        f"cd {shlex.quote(remote_mirror(repository))} && "
        'PATH="$HOME/.cargo/bin:$PATH" '
        "BUILDLOG_CALLER=verify-mac "
        f"BUILDLOG_CALL_ID={shlex.quote(request.call_id)} "
        "BUILDLOG_SYNC=1 "
        f"LINT_SWEEP_BUDGET_GIB={shlex.quote(budget)} "
        f"MACOSX_DEPLOYMENT_TARGET={MACOSX_DEPLOYMENT_TARGET} "
        f"~/.claude/scripts/lint/lint {invocation}; echo mac_exit=$?"
    )


def leftover_check_script() -> str:
    return r'''select_leftovers() {
    if command_pids=$(pgrep -f '[.]local/state/mac-test/mirror/'); then
        :
    else
        selector_result=$?
        [ "$selector_result" -eq 1 ] || return 1
    fi
    cwd_records=$(lsof -a -u "$(id -u)" -d cwd -Fpn) || return 1
    cwd_pids=$(printf '%s\n' "$cwd_records" | awk '/^p/ { pid = substr($0, 2) } /^n.*\/[.]local\/state\/mac-test\/mirror(\/|$)/ { print pid }') || return 1
    printf '%s\n%s\n' "$command_pids" "$cwd_pids" | awk '/^[0-9]+$/ && !seen[$0]++ { print }'
}
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


def check_leftovers(config: OffloadConfig, host: str) -> LeftoverCheckResult:
    """End and count mirror work without exposing the check to a second signal."""
    try:
        result = subprocess.run(
            ["ssh", *ssh_options(config), host, leftover_check_script()],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        return LeftoverCheckFailed("SSH leftover check timed out after 10 s")
    except OSError:
        return LeftoverCheckFailed("SSH leftover check could not start (time limit 10 s)")
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
    config: OffloadConfig, claim: TakenOverMacClaim | TakeoverHostUnreadable
) -> LeftoverCheckResult:
    if isinstance(claim, TakenOverMacClaim):
        return check_leftovers(config, claim.host)
    return LeftoverCheckFailed("the kept claim's host could not be read")


def claim_for_run(
    controller: ProcessController, config: OffloadConfig, request: OffloadRequest
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
            checked = check_taken_over_claim(config, claim)
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
        if probe.power == "battery":
            return declined("battery", started)
        if probe.free_gib < config.free_floor_gib:
            return declined("disk", started)

        copy_started = time.monotonic()
        if not copy_tree(controller, config, request, repository):
            return declined("copy", started)
        copy_seconds = elapsed_seconds(copy_started)
        print(
            "mac_test: running on the Mac "
            + f"(macOS, host {probe.hostname}); tree copied in {copy_seconds:.1f} s",
            flush=True,
        )
        release_allowed = False
        remote_status = controller.stream_mac_run(
            [
                *ssh_prefix(config),
                remote_test_command(config, request, repository),
            ]
        )
        seconds = elapsed_seconds(started)
        if isinstance(remote_status, MissingMacStatus):
            checked = check_leftovers(config, config.host)
            release_allowed = isinstance(checked, LeftoversEnded)
            report_leftover_check(checked, request)
            return LostMacRun(elapsed_seconds(started))
        release_allowed = True
        if remote_status.status == 0:
            return PassedOnMac(seconds)
        if remote_status.status == 4:
            return NoTestMatchedOnMac(seconds)
        return FailedOnMac(remote_status.status, seconds)
    except OffloadInterrupted:
        if not release_allowed:
            checked = check_leftovers(config, config.host)
            release_allowed = isinstance(checked, LeftoversEnded)
            report_leftover_check(checked, request)
        raise
    finally:
        if release_allowed:
            release_claim(controller)


def run_offload(
    controller: ProcessController,
    config: OffloadConfig,
    request: OffloadRequest,
    started: float,
) -> MacOffloadResult:
    if not config.enabled:
        return declined("off", started)
    repository = repository_identity(request.repo_root)
    if isinstance(repository, UnknownRepository):
        return declined("repo", started)
    linux_only_key = f"linux_only.{repository.name}"
    if linux_only_key not in config.values:
        return declined("repo", started)
    if request.package in comma_values(config.values[linux_only_key]):
        return declined("linux_only", started)

    backoff = read_backoff(time.time())
    if isinstance(backoff, ActiveBackoff):
        return declined("backoff", started)

    claim = claim_for_run(controller, config, request)
    if isinstance(claim, (TakenOverMacClaim, TakeoverHostUnreadable)):
        checked = check_taken_over_claim(config, claim)
        report_leftover_check(checked, request)
        if isinstance(checked, LeftoverCheckFailed):
            write_backoff(time.time() + config.unreachable_backoff_s)
            return declined("unreachable", started)
        if isinstance(checked, LeftoversRemain):
            return declined("busy", started)
        release_claim(controller)
        claim = claim_for_run(controller, config, request)
        if isinstance(claim, (TakenOverMacClaim, TakeoverHostUnreadable)):
            return declined("busy", started)
    if isinstance(claim, MacClaimNotConfirmed):
        if not decline_already_shown(claim.reason, request.last_decline):
            if claim.stdout:
                print(claim.stdout, end="" if claim.stdout.endswith("\n") else "\n", file=sys.stderr, flush=True)
            if claim.stderr:
                print(claim.stderr, end="" if claim.stderr.endswith("\n") else "\n", file=sys.stderr, flush=True)
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
    _ = run.add_argument("--package", required=True)
    _ = run.add_argument("--call-id", required=True)
    _ = run.add_argument("--settings", default="")
    _ = run.add_argument("--last-decline", choices=("busy", "blocked"), default="")
    _ = run.add_argument("--filter-run", action="store_true")
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
        package=arguments.package,
        call_id=arguments.call_id,
        result=result_destination(arguments.result),
        nextest_words=tuple(words),
        last_decline=last_decline,
    )


def restore_ignored_interrupt_default(arguments: CliArguments) -> None:
    if (
        arguments.command == "run"
        and signal.getsignal(signal.SIGINT) == signal.SIG_IGN
    ):
        _ = signal.signal(signal.SIGINT, signal.SIG_DFL)


def main() -> int:
    arguments = parser().parse_args(namespace=CliArguments())
    restore_ignored_interrupt_default(arguments)
    request = request_from_arguments(arguments)
    config = read_config(Path(arguments.settings)) if arguments.settings else read_config()
    controller = ProcessController()
    started = time.monotonic()
    try:
        with interrupt_handlers(controller):
            result = run_offload(controller, config, request, started)
        return write_result(result, request.result, request.last_decline)
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
