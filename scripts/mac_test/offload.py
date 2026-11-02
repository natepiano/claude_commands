#!/usr/bin/env python3
"""Run one read-only Cargo judgment on an idle Mac mirror when it is available."""

from __future__ import annotations

import argparse
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
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
from typing import TYPE_CHECKING, Literal, NewType, TextIO, TypedDict, cast, final
from urllib.parse import quote


sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lint"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "buildlog"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "build_hold"))
import cargo_manifest
import override_guard
import sweep
import treekey
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "build_targets"))
if TYPE_CHECKING:
    from ..build_targets import target_config
else:
    import target_config


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


LegacyMirrorName = NewType("LegacyMirrorName", str)
RepositoryPolicyName = NewType("RepositoryPolicyName", str)


@dataclass(frozen=True)
class KnownRepository:
    """A repository with its readable name and normalized origin identity."""

    name: str
    key: str = ""

    @property
    def legacy_mirror_name(self) -> LegacyMirrorName:
        return LegacyMirrorName(self.name)

    @property
    def policy_name(self) -> RepositoryPolicyName:
        if self.key and not self.key.startswith("git-root:"):
            return RepositoryPolicyName(self.key.rsplit("/", 1)[-1])
        return RepositoryPolicyName(self.name)

    @property
    def path_key(self) -> str:
        return quote(self.key or self.name, safe="").replace(".", "%2E")


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
    target: str = ""
    slot: int = 1

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
class UnresolvedLeftoverCount:
    """Process evidence cannot establish the number of remaining processes."""


@dataclass(frozen=True)
class LeftoversRemain:
    count: int | UnresolvedLeftoverCount


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

    slot: int = 1


@dataclass(frozen=True)
class TakenOverMacClaim:
    """Capacity inherited from a lost job on the host in the claim's reply."""

    host: str
    slot: int = 1


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


@dataclass(frozen=True)
class UnresolvedSlotStartup:
    """The Linux slot refused startup because earlier ownership remains unresolved."""


RemoteRunStatus = ReportedMacStatus | MissingMacStatus | UnresolvedSlotStartup
MISSING_MAC_STATUS = MissingMacStatus()


class UnavailableProbe:
    """A probe that did not return a complete availability snapshot."""


ProbeResult = ProbeSnapshot | UnavailableProbe | UnresolvedSlotStartup
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
        self.target: str = ""
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
                if marker == "linux_startup=unresolved":
                    status = UnresolvedSlotStartup()
                    held_status_line = ""
                    cleanup = CleanupUnconfirmed()
                    continue
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
        if isinstance(status, UnresolvedSlotStartup):
            return status
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


def report_cleanup(cleanup: CleanupOutcome, target: str = "") -> None:
    label = target or "Mac"
    if isinstance(cleanup, CleanupFailed):
        print(f"{label} build output not removed: {cleanup.rc}", flush=True)
    elif isinstance(cleanup, CleanupSkipped):
        print(f"{label} build output kept: the run used another target dir", flush=True)
    elif isinstance(cleanup, CleanupUnconfirmed):
        print(f"{label} build output cleanup unconfirmed", flush=True)


def config_path() -> Path:
    configured = os.environ.get("MAC_TEST_CONFIG", "~/.claude/config/mac_test.conf")
    return Path(configured).expanduser()


def configured_ssh_target(target: str) -> target_config.SshTarget:
    """Resolve an SSH target before access to its coordination state."""
    for configured in target_config.read_targets():
        if configured.name == target and isinstance(configured, target_config.SshTarget):
            if target in (".", "..") or Path(target).name != target:
                raise target_config.ConfigError(f"invalid target name: {target}")
            return configured
    raise target_config.ConfigError(f"unknown SSH target: {target}")


def state_directory(target: str = "mac") -> Path:
    target = target or "mac"
    if target != "mac":
        _ = configured_ssh_target(target)
        return Path.home() / ".local/state/build-targets" / target
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
    common = sweep.git_common_dir(str(repo_root))
    if name is None or common is None:
        return UNKNOWN_REPOSITORY
    source = Path(sweep.source_git_dir(common))
    if not (source / "config").is_file():
        return KnownRepository(name)
    # Load admission after module initialization to preserve its recorder boundary.
    if TYPE_CHECKING:
        from ..lint import memory_admit
    else:
        import memory_admit
    identity = memory_admit.git_identity(["git"], str(source.parent))[0]
    if isinstance(identity, memory_admit.RepositoryIdentity):
        return KnownRepository(name, identity.key)
    return KnownRepository(name)


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
    target: str = "",
) -> int:
    """Write the wire form and return the exit status for one outcome."""
    label = target or "Mac"
    if isinstance(result, PassedOnMac):
        mac = "passed"
        reason = ""
        status = 0
    elif isinstance(result, NoTestMatchedOnMac):
        mac = "declined"
        reason = "no_tests"
        status = 75
        print(f"mac_test: no test matched on the {label}; running on natedev instead")
    elif isinstance(result, FailedOnMac):
        mac = "failed"
        reason = ""
        status = result.status
        advice = (
            "only a test that must run on Linux may rerun with --local to run it on natedev."
            if verb == "nextest" else "fix the finding in the local worktree; the mirror sends no edits back."
        )
        if target:
            print(f"mac_test: this ran on {label}. Treat the failure as real. Fix it in the local worktree.")
        else:
            print("mac_test: this ran on the Mac (macOS). Treat the failure as real and fix it; " + advice)
    elif isinstance(result, LostMacRun):
        mac = "lost"
        reason = ""
        status = 75
        print(f"mac_test: the link to the {label} dropped; running on natedev instead")
    else:
        mac = "declined"
        reason = result.reason
        status = 75
        if not decline_already_shown(result.reason, last_decline):
            print(f"mac_test: not running on the {label} ({reason})")
    if isinstance(destination, ResultFile):
        holder_line = f"holder={holder_value(result.holder)}\n" if isinstance(result, BusyMacDecline) else ""
        _ = destination.path.write_text(
            f"mac={mac}\nreason={reason}\nseconds={result.seconds:.6f}\n" + holder_line,
            encoding="utf-8",
        )
    return status


def backoff_path(target: str = "mac") -> Path:
    return state_directory(target) / "unreachable_until"


def read_backoff(now: float, target: str = "mac") -> BackoffState:
    path = backoff_path(target)
    try:
        until = float(path.read_text(encoding="utf-8").strip())
    except FileNotFoundError:
        return NO_BACKOFF
    except (OSError, ValueError):
        return NO_BACKOFF
    if not math.isfinite(until):
        return NO_BACKOFF
    return ACTIVE_BACKOFF if until > now else NO_BACKOFF


def write_backoff(until: float, target: str = "mac") -> None:
    path = backoff_path(target)
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


def remote_mirror(repository: KnownRepository, target: str = "mac", slot: int = 1) -> str:
    if target_config.uses_legacy_mac_layout(configured_ssh_target(target or "mac").platform):
        return f".local/state/mac-test/mirror/{repository.legacy_mirror_name}"
    if slot < 1:
        raise ValueError("slot must be positive")
    return f".local/state/build-target/mirror/{repository.path_key}/{slot}"


def probe_script(repository: KnownRepository, target: str = "mac", slot: int = 1) -> str:
    if not target_config.uses_legacy_mac_layout(configured_ssh_target(target or "mac").platform):
        if TYPE_CHECKING:
            from . import linux_runner
        else:
            import linux_runner
        return linux_runner.probe_script(remote_mirror(repository, target, slot))
    mirror = shlex.quote(remote_mirror(repository, target, slot))
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
    selection = ["--target", request.target] if request.target else []
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
        *selection,
    ]


def release_claim(controller: ProcessController, target: str = "") -> None:
    _ = controller.capture(
        [
            sys.executable,
            str(Path(__file__).with_name("mac_test.py")),
            "release",
            "--pid",
            str(os.getpid()),
            *(["--target", target] if target else []),
        ]
    )


def read_claim_answer(result: CapturedCommandResult) -> MacClaimAnswer:
    if not isinstance(result, CommandOutput):
        return MacClaimNotConfirmed("state", "", "")
    slots = [line.removeprefix("slot: ") for line in result.stdout.splitlines() if line.startswith("slot: ")]
    if slots and (len(slots) != 1 or not slots[0].isascii() or not slots[0].isdecimal() or int(slots[0]) < 1):
        return MacClaimNotConfirmed("state", result.stdout, result.stderr)
    slot = int(slots[0]) if slots else 1
    if result.status == 0:
        return FreshMacClaim(slot)
    if result.status == 13:
        hosts = [line.removeprefix("leftovers: ").strip() for line in result.stdout.splitlines()
                 if line.startswith("leftovers: ")]
        if len(hosts) == 1 and hosts[0]:
            return TakenOverMacClaim(hosts[0], slot)
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


def trusted_linux_source(root: Path) -> bool:
    """Keep the local shell approval boundary before remote evaluation."""
    if TYPE_CHECKING:
        from ..lib import nix_env
    else:
        from scripts.lib import nix_env
    envrc = nix_env.nearest_envrc(root)
    if envrc is not None:
        if nix_env.trusted_envrc(envrc):
            return True
        return isinstance(nix_env.ensure(root), nix_env.Loaded)
    return nix_env.trusted_root(root) or nix_env.registered_worktree(root)


class PublicToolParity(TypedDict):
    rustc: str
    nextest: str
    mend: str
    scripts_commit: str


@dataclass(frozen=True)
class UnavailableToolParity:
    """The sender could not read every public tool version."""


def expected_linux_parity(controller: ProcessController, config: OffloadConfig) -> PublicToolParity | UnavailableToolParity:
    """Compare public tools with configured pins or the sender's versions."""
    expected: dict[str, str] = {}
    commands = (("rustc", ["rustc", "-V"]),
                ("nextest", ["cargo", "nextest", "--version"]),
                ("mend", ["cargo", "mend", "--version"]),
                ("scripts_commit", ["git", "-C", str(Path(__file__).resolve().parents[2]), "rev-parse", "HEAD"]))
    for key, arguments in commands:
        value = config.values.get(f"parity.{key}", "")
        if not value:
            result = controller.capture(arguments, CommandTimeLimit(config.probe_timeout_s))
            if not isinstance(result, CommandOutput) or result.status != 0 or not result.stdout.strip():
                return UnavailableToolParity()
            value = result.stdout.strip()
        expected[key] = value
    return {"rustc": expected["rustc"], "nextest": expected["nextest"],
            "mend": expected["mend"], "scripts_commit": expected["scripts_commit"]}


def copy_tracked_scripts(controller: ProcessController, config: OffloadConfig) -> bool:
    """Send tracked scripts and named public configuration without Git metadata."""
    root = Path(__file__).resolve().parents[2]
    listing = controller.capture(["git", "-C", str(root), "ls-files", "-z", "scripts",
                                  "config/lint.conf", "config/build_targets.conf",
                                  "config/aws_target.conf", "config/mac_test.conf",
                                  "config/user_zone.conf"])
    if not isinstance(listing, CommandOutput) or listing.status != 0:
        return False
    paths = [path for path in listing.stdout.split("\0") if path]
    # The sender must include the new tracked runner before target activation.
    if "scripts/mac_test/linux_runner.py" not in paths:
        return False
    for name in paths:
        path = root / name
        if not path.is_file() or path.is_symlink():
            return False
    clean = controller.capture(["git", "-C", str(root), "diff", "HEAD", "--quiet", "--", *paths])
    if not isinstance(clean, CommandOutput) or clean.status != 0:
        return False
    expected = expected_linux_parity(controller, config)
    if isinstance(expected, UnavailableToolParity):
        return False
    commit = controller.capture(["git", "-C", str(root), "rev-parse", "HEAD"],
                                CommandTimeLimit(config.probe_timeout_s))
    if not isinstance(commit, CommandOutput) or commit.status != 0 or commit.stdout.strip() != expected["scripts_commit"]:
        return False
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", prefix="target-scripts-") as manifest:
        _ = manifest.write("\n".join(paths) + "\n")
        manifest.flush()
        result = controller.capture(["rsync", "-rlpc", f"--files-from={manifest.name}",
                                     "-e", shlex.join(["ssh", *ssh_options(config)]),
                                     f"{root}/", f"{config.host}:.claude/"])
    if not isinstance(result, CommandOutput) or result.status != 0:
        return False
    clean = controller.capture(["git", "-C", str(root), "diff", "HEAD", "--quiet", "--", *paths])
    current_commit = controller.capture(["git", "-C", str(root), "rev-parse", "HEAD"],
                                       CommandTimeLimit(config.probe_timeout_s))
    if (not isinstance(clean, CommandOutput) or clean.status != 0
            or not isinstance(current_commit, CommandOutput) or current_commit.status != 0
            or current_commit.stdout != commit.stdout):
        return False
    stamp = controller.capture([*ssh_prefix(config),
        "printf '%s\\n' " + shlex.quote(commit.stdout.strip())
        + ' > "$HOME/.claude/.build-target-script-commit"'])
    return isinstance(stamp, CommandOutput) and stamp.status == 0


def probe_mac(
    controller: ProcessController,
    config: OffloadConfig,
    repository: KnownRepository,
    target: str = "mac",
    slot: int = 1,
) -> ProbeResult:
    linux = not target_config.uses_legacy_mac_layout(configured_ssh_target(target or "mac").platform)
    if linux:
        if TYPE_CHECKING:
            from . import linux_runner
        else:
            import linux_runner
        script = linux_runner.probe_command_script(remote_mirror(repository, target, slot), config.values,
                                                   repository.policy_name)
        if isinstance(probe_reachability(controller, config), UnreachableHost):
            print(f"mac_test: could not reach {target}: SSH reachability check failed",
                  file=sys.stderr, flush=True)
            return UNAVAILABLE_PROBE
        timeout_s = linux_runner.positive_setting(config.values, "shell_timeout_s") + config.probe_timeout_s
    else:
        script = probe_script(repository, target, slot)
        timeout_s = config.probe_timeout_s
    result = controller.capture(
        [*ssh_prefix(config), script],
        CommandTimeLimit(timeout_s),
    )
    if (linux and isinstance(result, CommandOutput)
            and result.stdout.strip() == "linux_startup=unresolved"):
        return UnresolvedSlotStartup()
    if isinstance(result, CommandTimedOut):
        reason = f"SSH probe timed out after {timeout_s:g} s"
    elif isinstance(result, CommandCouldNotStart):
        reason = f"SSH probe could not start (time limit {timeout_s:g} s)"
    elif result.status != 0:
        reason = " ".join(result.stderr.split()) or f"SSH probe exited with status {result.status}"
    else:
        probe = parse_probe(result.stdout)
        if isinstance(probe, ProbeSnapshot):
            if not linux:
                return probe
            expected = expected_linux_parity(controller, config)
            values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
            if not isinstance(expected, UnavailableToolParity) and all(values.get(key) == value for key, value in expected.items()):
                return probe
            print(f"mac_test: {target} is not ready: public tools or tracked scripts differ", file=sys.stderr)
            return UNAVAILABLE_PROBE
        reason = "the probe answer could not be read"
    print(f"mac_test: could not reach the {'Mac' if target in ('', 'mac') else target}: {reason}", file=sys.stderr, flush=True)
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
                        f"{config.host}:{shlex.quote(remote_mirror(repository, request.target, request.slot))}/",
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
    words: tuple[str, ...], skip_expression: str, *, insert_filter: bool = False
) -> tuple[str, ...]:
    if not skip_expression:
        return words
    if TYPE_CHECKING:
        from . import linux_runner
    else:
        import linux_runner
    return linux_runner.nextest_words_with_skip(words, skip_expression, insert_filter=insert_filter)


def remote_test_command(
    config: OffloadConfig,
    request: OffloadRequest,
    repository: KnownRepository,
    sent_tree_key: TreeKey = UNAVAILABLE_TREE_KEY,
) -> str:
    if not target_config.uses_legacy_mac_layout(configured_ssh_target(request.target or "mac").platform):
        if TYPE_CHECKING:
            from . import linux_runner
        else:
            import linux_runner
        words = request.nextest_words
        return linux_runner.command_script(
            remote_mirror(repository, request.target, request.slot), config.values,
            repository.policy_name, request.verb, words, request.call_id,
            sent_tree_key.value if isinstance(sent_tree_key, KnownTreeKey) else "",
        )
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
        target = shlex.quote(f"{remote_mirror(repository, request.target, request.slot)}/target")
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
        f"cd {shlex.quote(remote_mirror(repository, request.target, request.slot))} && "
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
        character if character.isalnum() or character in "_-/"
        else "." if character in "]^\\"
        else f"[{character}]"
        for character in text
    )


def mirror_selector_script(scope: MirrorScope, target: str = "mac", slot: int = 1) -> str:
    """Define select_leftovers: the user's processes running from the scoped mirrors.

    A repository's scope stops at its own mirror, so its leftover check never
    reaches another repository's run. The `[.]` keeps each pattern from
    matching this script's own text.
    """
    if not target_config.uses_legacy_mac_layout(configured_ssh_target(target or "mac").platform):
        if not isinstance(scope, KnownRepository):
            raise ValueError("Linux cleanup requires a repository and slot")
        if TYPE_CHECKING:
            from . import linux_runner
        else:
            import linux_runner
        return linux_runner.mirror_selector_script(remote_mirror(scope, target, slot))
    if isinstance(scope, KnownRepository):
        mirror = extended_regex_literal(remote_mirror(scope, target, slot))
        command_pattern = f"{mirror}([/'\"[:space:]]|$)"
        cwd_pattern = f"^n.*/{mirror}(/|$)"
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


def leftover_check_script(repository: KnownRepository, target: str = "mac", slot: int = 1) -> str:
    return mirror_selector_script(repository, target, slot) + r'''
leftover_pids=$(select_leftovers) || exit "$?"
printf '%s\n' "$leftover_pids" | while IFS= read -r leftover_pid; do
    [ -n "$leftover_pid" ] && kill "$leftover_pid" 2>/dev/null
done
sleep 1 || exit 1
leftover_pids=$(select_leftovers) || exit "$?"
printf '%s\n' "$leftover_pids" | while IFS= read -r leftover_pid; do
    [ -n "$leftover_pid" ] && kill -9 "$leftover_pid" 2>/dev/null
done
leftover_pids=$(select_leftovers) || exit "$?"
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


def check_leftovers(config: OffloadConfig, host: str, repository: KnownRepository,
                   target: str = "mac", slot: int = 1) -> LeftoverCheckResult:
    """End and count one mirror's work without exposing the check to a second signal."""
    try:
        result = subprocess.run(
            ["ssh", *ssh_options(config), host, leftover_check_script(repository, target, slot)],
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
    if result.returncode != 0:
        if not target_config.uses_legacy_mac_layout(configured_ssh_target(target or "mac").platform):
            if TYPE_CHECKING:
                from . import linux_runner
            else:
                import linux_runner
            if result.returncode == linux_runner.SLOT_PROCESSES_UNRESOLVED:
                return LeftoversRemain(UnresolvedLeftoverCount())
        return LeftoverCheckFailed(" ".join(result.stderr.split()) or f"SSH cleanup exited with status {result.returncode}")
    answer = parse_leftover_check(result.stdout)
    if isinstance(answer, LeftoverCheckFailed):
        reason = " ".join(result.stderr.split())
        if reason:
            return LeftoverCheckFailed(reason)
    return answer


def report_leftover_check(result: LeftoverCheckResult, request: OffloadRequest) -> None:
    label = request.target or "Mac"
    if isinstance(result, LeftoversRemain):
        if not decline_already_shown("busy", request.last_decline):
            remaining = (f"{result.count} leftover processes of a lost run are still" if isinstance(result.count, int)
                         else "leftover process ownership remains unresolved")
            print(f"mac_test: {remaining} on the {label}; "
                  + "it stays busy until they end", file=sys.stderr, flush=True)
    elif isinstance(result, LeftoverCheckFailed):
        if not decline_already_shown("unreachable", request.last_decline):
            print("mac_test: could not confirm that a lost run's leftovers ended: "
                  + f"{result.reason}; the {label} stays busy", file=sys.stderr, flush=True)


def check_taken_over_claim(
    config: OffloadConfig, claim: TakenOverMacClaim | TakeoverHostUnreadable, repository: KnownRepository,
    target: str = "mac",
) -> LeftoverCheckResult:
    if isinstance(claim, TakenOverMacClaim):
        return check_leftovers(config, claim.host, repository, target, claim.slot)
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
            release_claim(controller, request.target)
        elif isinstance(claim, (TakenOverMacClaim, TakeoverHostUnreadable)):
            checked = check_taken_over_claim(config, claim, repository, request.target)
            report_leftover_check(checked, request)
            if isinstance(checked, LeftoversEnded):
                release_claim(controller, request.target)
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
        linux = not target_config.uses_legacy_mac_layout(configured_ssh_target(request.target or "mac").platform)
        if linux:
            expected = expected_linux_parity(controller, config)
            if isinstance(expected, UnavailableToolParity):
                return declined("state", started)
            config = replace(config, values={**config.values, **{f"parity.{key}": value for key, value in expected.items()}})
            if not copy_tracked_scripts(controller, config):
                return declined("copy", started)
            copy_started = time.monotonic()
            copied = copy_tree_with_key(controller, config, request, repository)
            if isinstance(copied, TreeCopyFailed):
                return declined("copy", started)
            copy_seconds = elapsed_seconds(copy_started)
            release_allowed = False
            probe = probe_mac(controller, config, repository, request.target, request.slot)
            if isinstance(probe, UnresolvedSlotStartup):
                checked = check_leftovers(config, config.host, repository, request.target, request.slot)
                release_allowed = isinstance(checked, LeftoversEnded)
                report_leftover_check(checked, request)
                return BusyMacDecline(LostRunLeftovers(), elapsed_seconds(started))
            if isinstance(probe, UnavailableProbe):
                checked = check_leftovers(config, config.host, repository, request.target, request.slot)
                release_allowed = isinstance(checked, LeftoversEnded)
                report_leftover_check(checked, request)
                write_backoff(time.time() + config.unreachable_backoff_s, request.target)
                return declined("unreachable", started)
            if probe.free_gib < config.free_floor_gib:
                checked = check_leftovers(config, config.host, repository, request.target, request.slot)
                release_allowed = isinstance(checked, LeftoversEnded)
                report_leftover_check(checked, request)
                return declined("disk", started)
        else:
            probe = probe_mac(controller, config, repository, request.target, request.slot)
            if isinstance(probe, (UnavailableProbe, UnresolvedSlotStartup)):
                write_backoff(time.time() + config.unreachable_backoff_s, request.target)
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
        if linux:
            running = f"running on {request.target} (Linux, host {probe.hostname})"
        print(f"mac_test: {running}; tree copied in {copy_seconds:.1f} s", flush=True)
        release_allowed = False
        remote_status = controller.stream_mac_run(
            [
                *ssh_prefix(config),
                remote_test_command(config, request, repository, copied.tree_key),
            ]
        )
        seconds = elapsed_seconds(started)
        if linux and isinstance(remote_status, UnresolvedSlotStartup):
            checked = check_leftovers(config, config.host, repository, request.target, request.slot)
            release_allowed = isinstance(checked, LeftoversEnded)
            report_leftover_check(checked, request)
            return BusyMacDecline(LostRunLeftovers(), elapsed_seconds(started))
        if isinstance(remote_status, (MissingMacStatus, UnresolvedSlotStartup)):
            checked = check_leftovers(config, config.host, repository, request.target, request.slot)
            release_allowed = isinstance(checked, LeftoversEnded)
            report_leftover_check(checked, request)
            return LostMacRun(elapsed_seconds(started))
        report_cleanup(remote_status.cleanup, request.target)
        if isinstance(remote_status.cleanup, CleanupUnconfirmed):
            checked = check_leftovers(config, config.host, repository, request.target, request.slot)
            release_allowed = isinstance(checked, LeftoversEnded)
            report_leftover_check(checked, request)
        else:
            release_allowed = True
        if linux and remote_status.status == 75:
            return declined("state", started)
        if remote_status.status == 0:
            return PassedOnMac(seconds)
        if remote_status.status == 4 and request.verb == "nextest":
            return NoTestMatchedOnMac(seconds)
        return FailedOnMac(remote_status.status, seconds)
    except OffloadInterrupted:
        if not release_allowed:
            checked = check_leftovers(config, config.host, repository, request.target, request.slot)
            release_allowed = isinstance(checked, LeftoversEnded)
            report_leftover_check(checked, request)
        raise
    finally:
        if release_allowed:
            release_claim(controller, request.target)


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
                         verb: MacVerb, target: str = "") -> KnownRepository | IneligibleMacStep:
    """Share repository, package/workspace and backoff rules with eligible --kind."""
    if not config.enabled:
        return IneligibleMacStep("off")
    repository = repository_identity(repo_root)
    if isinstance(repository, UnknownRepository):
        return IneligibleMacStep("repo")
    if target and not target_config.uses_legacy_mac_layout(configured_ssh_target(target).platform):
        kind = "test" if verb == "nextest" else verb
        if kind not in ("test", "check", "doc") or kind not in comma_values(config.values.get("kinds", "")):
            return IneligibleMacStep("repo")
        if isinstance(read_backoff(time.time(), target), ActiveBackoff):
            return IneligibleMacStep("backoff")
        return repository
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
    if isinstance(read_backoff(time.time(), target), ActiveBackoff):
        return IneligibleMacStep("backoff")
    return repository


def run_offload(
    controller: ProcessController,
    config: OffloadConfig,
    request: OffloadRequest,
    started: float,
) -> MacOffloadResult:
    eligibility = mac_step_eligibility(config, request.repo_root, request.package, request.verb, request.target)
    if isinstance(eligibility, IneligibleMacStep):
        return declined(eligibility.reason, started)
    repository = eligibility
    linux = not target_config.uses_legacy_mac_layout(configured_ssh_target(request.target or "mac").platform)
    if linux and not trusted_linux_source(request.repo_root):
        return declined("repo", started)
    if not linux and isinstance(request.source, StagedDocSnapshot):
        try:
            if workspace_has_linux_only(request.source.root, comma_values(config.values[f"linux_only.{repository.name}"])):
                return declined("linux_only", started)
        except (OSError, UnicodeError, ValueError):
            return declined("repo", started)

    claim = claim_for_run(controller, config, request, repository)
    if isinstance(claim, (TakenOverMacClaim, TakeoverHostUnreadable)):
        checked = check_taken_over_claim(config, claim, repository, request.target)
        report_leftover_check(checked, request)
        if isinstance(checked, LeftoverCheckFailed):
            write_backoff(time.time() + config.unreachable_backoff_s, request.target)
            return declined("unreachable", started)
        if isinstance(checked, LeftoversRemain):
            return BusyMacDecline(LostRunLeftovers(), elapsed_seconds(started))
        release_claim(controller, request.target)
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
    return claimed_run(controller, config, replace(request, slot=claim.slot), repository, started)


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
    _ = run.add_argument("--target", default="")
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
        target=arguments.target,
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
    settings = arguments.settings
    if not settings and arguments.target:
        settings = str(configured_ssh_target(arguments.target).settings)
    config = read_config(Path(settings)) if settings else read_config()
    controller = ProcessController()
    started = time.monotonic()
    try:
        with interrupt_handlers(controller):
            result = run_offload(controller, config, request, started)
        return write_result(result, request.result, request.last_decline, request.verb, request.target)
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
