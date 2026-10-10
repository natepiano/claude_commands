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
    """A run kept on natedev before a Mac test command began."""

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
class OffloadConfig:
    """Settings that govern one Mac offload decision."""

    enabled: bool
    host: str
    connect_timeout_s: float
    probe_timeout_s: float
    unreachable_backoff_s: float
    free_floor_gib: float
    max_load: float
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


@dataclass(frozen=True)
class ProbeSnapshot:
    """Mac availability values returned by one remote probe."""

    processes: int
    load: float
    power: MacPower
    free_gib: float
    hostname: str


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


ActiveProcess = IdleProcess | RunningProcess
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
        self.nextest_words: list[str] = []


class ProcessController:
    """Starts one child at a time and exposes it to signal cleanup."""

    def __init__(self) -> None:
        self.active: ActiveProcess = IDLE_PROCESS

    def stop(self) -> None:
        active = self.active
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
            status = process.wait()
        finally:
            self.active = IDLE_PROCESS
            output.close()
        status_match = re.fullmatch(
            r"mac_exit=([0-9]+)", held_status_line.rstrip("\r\n")
        )
        if status_match is None or status == 255:
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


def read_config() -> OffloadConfig:
    values = sweep.config_values(str(config_path()))
    return OffloadConfig(
        enabled=values.get("offload") == "on",
        host=values.get("host", "mac"),
        connect_timeout_s=positive_number(values, "connect_timeout_s", 6.0),
        probe_timeout_s=positive_number(values, "probe_timeout_s", 15.0),
        unreachable_backoff_s=positive_number(
            values, "unreachable_backoff_s", 300.0
        ),
        free_floor_gib=positive_number(values, "free_floor_gib", 60.0),
        max_load=positive_number(values, "max_load", 6.0),
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


def write_result(result: MacOffloadResult, destination: ResultDestination) -> int:
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
            "mac_test: this ran on the Mac (macOS). A failure that looks "
            + "unrelated to your change may be a macOS difference; rerun with "
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
        print(f"mac_test: staying on natedev ({reason})")
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
            "procs=$( (pgrep -x cargo; pgrep -x rustc; "
            + "pgrep -x cargo-nextest) 2>/dev/null | wc -l | tr -d ' ')",
            "load=$(sysctl -n vm.loadavg | awk '{print $2}')",
            "case \"$(pmset -g batt | head -1)\" in "
            + "*\"AC Power\"*) power=ac ;; *) power=battery ;; esac",
            "free_gib=$(df -g \"$HOME\" | awk 'NR == 2 {print $4}')",
            "printf 'procs=%s\\nload=%s\\npower=%s\\nfree_gib=%s\\nhost=%s\\nrc=0\\n' "
            + '"$procs" "$load" "$power" "$free_gib" "$(hostname -s)"',
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
        processes = int(values["procs"])
        load = float(values["load"])
        free_gib = float(values["free_gib"])
        power = values["power"]
        hostname = values["host"]
    except (KeyError, ValueError):
        return UNAVAILABLE_PROBE
    if (
        processes < 0
        or not math.isfinite(load)
        or not math.isfinite(free_gib)
        or power not in ("ac", "battery")
        or not hostname
    ):
        return UNAVAILABLE_PROBE
    return ProbeSnapshot(processes, load, power, free_gib, hostname)


def claim_arguments(request: OffloadRequest) -> list[str]:
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


def probe_mac(
    controller: ProcessController,
    config: OffloadConfig,
    repository: KnownRepository,
) -> ProbeResult:
    result = controller.capture(
        [*ssh_prefix(config), probe_script(repository)],
        CommandTimeLimit(config.probe_timeout_s),
    )
    if not isinstance(result, CommandOutput) or result.status == 255:
        return UNAVAILABLE_PROBE
    return parse_probe(result.stdout)


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
        f"~/.claude/scripts/lint/lint {invocation}; echo mac_exit=$?"
    )


def end_remote_work(config: OffloadConfig, repository: KnownRepository) -> None:
    try:
        _ = subprocess.run(
            [
                *ssh_prefix(config),
                f"pkill -f {shlex.quote(remote_mirror(repository))}",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return


def claimed_run(
    controller: ProcessController,
    config: OffloadConfig,
    request: OffloadRequest,
    repository: KnownRepository,
    started: float,
) -> MacOffloadResult:
    try:
        probe = probe_mac(controller, config, repository)
        if isinstance(probe, UnavailableProbe):
            write_backoff(time.time() + config.unreachable_backoff_s)
            return declined("unreachable", started)
        if probe.processes > 0 or probe.load > config.max_load:
            return declined("mac_busy", started)
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
        remote_status = controller.stream_mac_run(
            [
                *ssh_prefix(config),
                remote_test_command(config, request, repository),
            ]
        )
        seconds = elapsed_seconds(started)
        if isinstance(remote_status, MissingMacStatus):
            end_remote_work(config, repository)
            return LostMacRun(elapsed_seconds(started))
        if remote_status.status == 0:
            return PassedOnMac(seconds)
        if remote_status.status == 4:
            return NoTestMatchedOnMac(seconds)
        return FailedOnMac(remote_status.status, seconds)
    except OffloadInterrupted:
        end_remote_work(config, repository)
        raise


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

    release_may_be_needed = True
    try:
        claim = controller.capture(claim_arguments(request))
        if not isinstance(claim, CommandOutput):
            release_may_be_needed = False
            return declined("state", started)
        if claim.status != 0:
            release_may_be_needed = False
            if claim.status == 10:
                return declined("blocked", started)
            if claim.status == 11:
                return declined("busy", started)
            return declined("state", started)
        return claimed_run(controller, config, request, repository, started)
    finally:
        if release_may_be_needed:
            release_claim(controller)


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
    _ = run.add_argument("--filter-run", action="store_true")
    _ = run.add_argument("--result")
    _ = run.add_argument("nextest_words", nargs=argparse.REMAINDER)
    return root


def request_from_arguments(arguments: CliArguments) -> OffloadRequest:
    words = arguments.nextest_words
    if words[:1] == ["--"]:
        words = words[1:]
    return OffloadRequest(
        repo_root=Path(arguments.repo_root).resolve(),
        package=arguments.package,
        call_id=arguments.call_id,
        result=result_destination(arguments.result),
        nextest_words=tuple(words),
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
    config = read_config()
    controller = ProcessController()
    started = time.monotonic()
    try:
        with interrupt_handlers(controller):
            result = run_offload(controller, config, request, started)
        return write_result(result, request.result)
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
