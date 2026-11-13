#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Run one read-only Cargo judgment on an idle Mac mirror when it is available."""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
from collections.abc import Generator, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field, replace
from importlib import import_module
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
from types import FrameType
from typing import TYPE_CHECKING, BinaryIO, Callable, Literal, NewType, Protocol, TextIO, TypedDict, cast, final
from urllib.parse import quote


from scripts.build_hold.owning_session import NoOwningSession, OwningSession
from scripts.mac_test import cargo_manifest, claim_state
if TYPE_CHECKING:
    from scripts.build_queue import placement
else:
    class _DeferredPlacement:
        """Load placement types when an offload operation uses them."""

        def __getattr__(self, name: str) -> object:
            return cast(object, getattr(import_module("scripts.build_queue.placement"), name))

    placement = _DeferredPlacement()
from scripts.lib import process_start as process_identity
from scripts.mac_test.abandoned_sessions import removal_script
from scripts.build_hold import override_guard
from scripts.lib import copy_record
from scripts.direct.step_selection import Pinned

if TYPE_CHECKING:
    from scripts.copies import copies
from scripts.direct import result_environment
from scripts.lib import conf
from scripts.lib.atomic_file import write_text_atomic
from scripts.buildlog import treekey
from scripts.mac_test.mirror_root import MacMirrorRoot
from scripts.buildlog import repository, store
from scripts.buildlog import sync as buildlog_sync
from scripts.build_targets import target_config
from scripts.lib import build_folders


# Version the mirrored repository's development shell exports on the Mac.
MACOSX_DEPLOYMENT_TARGET = "14.0"

MacVerb = Literal["nextest", "check", "clippy", "mend-check", "doc", "build"]


def step_kind(verb: str) -> str:
    """Map a command verb to its recorded step kind."""
    return {"test": "nextest", "mend-check": "mend"}.get(verb, verb)


NonBusyMacDeclineReason = Literal[
    "off",
    "repo",
    "linux_only",
    "backoff",
    "blocked",
    "state",
    "unreachable",
    "mac_busy",
    "target does not permit this command",
    "disk",
    "copy",
    "source_changed",
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
    detail: str = ""


def read_tree_key(directory: Path) -> TreeKey:
    if key := treekey.tree_key(str(directory)):
        return KnownTreeKey(key)
    return UNAVAILABLE_TREE_KEY


@dataclass(frozen=True)
class FetchFolder:
    """A plain repository-relative folder requested from this run."""

    value: str


@dataclass(frozen=True)
class NoFetch:
    """The caller requested no files from the Mac."""


NO_FETCH = NoFetch()


@dataclass(frozen=True)
class Fetched:
    path: Path
    kib: int


@dataclass(frozen=True)
class FetchMissing:
    pass


@dataclass(frozen=True)
class FetchTooLarge:
    kib: int
    limit_mib: float


@dataclass(frozen=True)
class FetchFailed:
    reason: str


@dataclass(frozen=True)
class FetchNotRun:
    pass


FetchResult = Fetched | FetchMissing | FetchTooLarge | FetchFailed | FetchNotRun


@dataclass(frozen=True)
class PassedOnMac:
    """A completed Mac run with a successful test status."""

    seconds: float
    fetch: FetchResult = FetchNotRun()
    build_target_directory: str = ""


@dataclass(frozen=True)
class FailedOnMac:
    """A completed Mac run with its nonzero test status."""

    status: int
    seconds: float
    fetch: FetchResult = FetchNotRun()


@dataclass(frozen=True)
class NoTestMatchedOnMac:
    """A completed Mac run whose filter selected no test."""

    seconds: float
    fetch: FetchResult = FetchNotRun()


@dataclass(frozen=True)
class LostMacRun:
    """A Mac run whose final test status was not received."""

    seconds: float
    detail: str = 'runner sent no final status'


@dataclass(frozen=True)
class DeclinedMacRun:
    """An attempt declined before a Mac test command began."""

    reason: NonBusyMacDeclineReason
    seconds: float
    detail: str = ""


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


@dataclass(frozen=True)
class PassedOnTarget:
    seconds: float
    build_target_directory: str = ""


@dataclass(frozen=True)
class FailedOnTarget:
    status: int
    seconds: float


@dataclass(frozen=True)
class UnclaimedMacRun:
    """The runner must acquire capacity before execution."""


@dataclass(frozen=True)
class MacClaimFromWait:
    """A live local process owns the reservation for transfer."""

    owner_pid: int
    identity: claim_state.ClaimIdentity
    submitting_session: claim_state.SubmittingSession


@dataclass(frozen=True)
class UnresolvedLeftoverCount:
    """Process evidence cannot establish the number of remaining processes."""


@dataclass
class AttemptEvidence:
    """Observed completion and ownership for one SSH attempt."""

    slot: int = 0
    completion: Literal["confirmed", "missing"] = "missing"
    cleanup: Literal["confirmed", "unconfirmed"] = "unconfirmed"
    unresolved_count: int | UnresolvedLeftoverCount = UnresolvedLeftoverCount()
    evidence_at: float = 0.0
    traffic: buildlog_sync.TransferLedger = field(default_factory=buildlog_sync.TransferLedger)
    execution: placement.ExecutionEvidence = field(default_factory=lambda: placement.ExecutionNotStarted())
    launch_authorized: bool = False
    provider: placement.ProviderClaimReference | placement.NoProviderClaim = field(default_factory=lambda: placement.NoProviderClaim())


MacOffloadResult = (
    PassedOnMac | FailedOnMac | PassedOnTarget | FailedOnTarget | NoTestMatchedOnMac | LostMacRun | DeclinedMacRun | BusyMacDecline
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
    """Select the live Mac configuration unless an explicit settings path is supplied."""


DEFAULT_MAC_SETTINGS = DefaultMacSettings()


@dataclass(frozen=True)
class SshConnectionSettings:
    host: str
    connect_timeout_s: float
    leftover_timeout_s: float


@dataclass(frozen=True)
class OffloadConfig:
    """Settings that govern one remote execution decision."""

    enabled: bool
    connection: SshConnectionSettings
    probe_timeout_s: float
    unreachable_backoff_s: float
    free_floor_gib: float
    default_target_budget_gib: str
    values: dict[str, str]
    source: Path | DefaultMacSettings = DEFAULT_MAC_SETTINGS

    @property
    def host(self) -> str:
        return self.connection.host

    @property
    def connect_timeout_s(self) -> float:
        return self.connection.connect_timeout_s

    @property
    def leftover_timeout_s(self) -> float:
        return self.connection.leftover_timeout_s

    @property
    def fetch_limit_mib(self) -> float:
        return float(self.values["fetch_limit_mib"])

    @property
    def abandoned_session_minutes(self) -> int:
        return int(self.values["abandoned_session_minutes"])

    @property
    def mirror_busy_backoff_s(self) -> float:
        return float(self.values["mirror_busy_backoff_s"])

    @property
    def fetch_retention_days(self) -> float:
        return float(self.values["fetch_retention_days"])


@dataclass(frozen=True)
class RepositoryWorkingTree:
    pass


@dataclass(frozen=True)
class StagedDocSnapshot:
    root: Path


MacSource = RepositoryWorkingTree | StagedDocSnapshot


@dataclass(frozen=True)
class DirectOffloadRequest:
    """A direct caller must submit its pinned destination before execution."""


@dataclass(frozen=True)
class AssignedOffloadRequest:
    assignment: placement.PlacementAssignment


@dataclass(frozen=True)
class SettledDispatchOperations:
    """The adapter has no unfinished claim or start child."""


@dataclass(frozen=True)
class PendingClaimOperation:
    """The adapter must collect its local claim child before cleanup."""


@dataclass(frozen=True)
class PendingStartOperation:
    """The remote start has no acknowledgement and can still arrive."""


@dataclass(frozen=True)
class AcknowledgedStartOperation:
    """The remote command acknowledged execution; its local child still runs."""


@dataclass(frozen=True)
class ClaimReleaseConfirmed:
    """The provider confirmed release of this exact claim."""


@dataclass(frozen=True)
class ClaimReleaseUnconfirmed:
    reason: str


@dataclass(frozen=True)
class StartSettled:
    leftovers: LeftoverCheckResult


@dataclass(frozen=True)
class StartUnsettled:
    reason: str
    leftovers: LeftoverCheckResult


def pending_start_key(identity: claim_state.ClaimIdentity) -> str:
    """Name the fence for one exact provider claim."""
    return hashlib.sha256(json.dumps([identity.claim_id, identity.episode, identity.attempt]).encode()).hexdigest()


def pending_start_path(target: str, identity: claim_state.ClaimIdentity) -> Path:
    return claim_state.state_paths(target).directory / "pending-starts" / pending_start_key(identity)


def start_fence_relative(identity: claim_state.ClaimIdentity, target: str, *, scope_key: str = "run") -> Path:
    state = (Path(".local/state/mac-test") if target in ("", "mac")
             else Path(".local/state/build-targets") / target)
    return state / "dispatch-fences" / scope_key / pending_start_key(identity)


def clean_start_fences_command(identity: claim_state.ClaimIdentity, target: str, *, scope_key: str = "run") -> str:
    """Remove inactive fences from older claims in this exact provider scope."""
    encoded = str(start_fence_relative(identity, target, scope_key=scope_key)).encode().hex()
    program = '''import fcntl, pathlib, sys
base = pathlib.Path.home() / bytes.fromhex(sys.argv[1]).decode()
for path in base.parent.glob("*.lock"):
    if path == base.with_suffix(".lock"):
        continue
    try:
        with path.open("r") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue
            path.unlink(missing_ok=True)
    except FileNotFoundError:
        continue
'''
    return shlex.join(["python3", "-c", program, encoded])


def prepare_start_fence_command(identity: claim_state.ClaimIdentity, target: str, *, scope_key: str = "run") -> str:
    """Create the start identity before the probe acknowledges preparation."""
    encoded = str(start_fence_relative(identity, target, scope_key=scope_key)).encode().hex()
    program = '''import pathlib, sys
base = pathlib.Path.home() / bytes.fromhex(sys.argv[1]).decode()
base.parent.mkdir(parents=True, exist_ok=True)
base.with_suffix(".lock").touch()
'''
    return shlex.join(["python3", "-c", program, encoded])


def start_fence_command(identity: claim_state.ClaimIdentity, target: str, *, settle: bool = False,
                        scope_key: str = "run") -> str:
    """Use the prepared identity without permitting recreation after release."""
    key = pending_start_key(identity)
    relative = str(start_fence_relative(identity, target, scope_key=scope_key))
    encoded = relative.encode().hex()
    if settle:
        program = '''import fcntl, pathlib, sys
base = pathlib.Path.home() / bytes.fromhex(sys.argv[1]).decode()
path = base.with_suffix(".lock")
try:
    lock = path.open("r")
except FileNotFoundError:
    print("dispatch_start_settled=" + sys.argv[2], flush=True)
    sys.exit(0)
with lock:
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("dispatch_start_pending=" + sys.argv[2], flush=True)
        sys.exit(0)
    path.unlink(missing_ok=True)
    print("dispatch_start_settled=" + sys.argv[2], flush=True)
'''
        return shlex.join(["python3", "-c", program, encoded, key])
    program = '''import fcntl, os, pathlib, sys
base = pathlib.Path.home() / bytes.fromhex(sys.argv[1]).decode()
fcntl.flock(8, fcntl.LOCK_EX)
try:
    current = base.with_suffix(".lock").stat()
except FileNotFoundError:
    current = None
opened = os.fstat(8)
matches = current is not None and (current.st_dev, current.st_ino) == (opened.st_dev, opened.st_ino)
if not matches:
    print("dispatch_start_settled=" + sys.argv[2], flush=True)
    sys.exit(75)
'''
    lock = shlex.quote(relative + ".lock")
    missing = start_fence_command(identity, target, settle=True, scope_key=scope_key)
    return (f'if [ ! -f "$HOME"/{lock} ]; then {missing}; exit 75; fi\n'
            + f'exec 8<"$HOME"/{lock} || exit 75\n'
            + shlex.join(["python3", "-c", program, encoded, key]) + " || exit 75\n")


def fenced_start_command(identity: claim_state.ClaimIdentity, target: str, command: str, *, scope_key: str = "run") -> str:
    """Remove the prepared identity after the existing remote command ends."""
    return (start_fence_command(identity, target, scope_key=scope_key)
            + clean_start_fences_command(identity, target, scope_key=scope_key) + " || exit 75\n"
            + "(\n" + command + "\n)\n"
            + "dispatch_status=$?\nexec 8<&-\n"
            + start_fence_command(identity, target, settle=True, scope_key=scope_key)
            + '\nexit "$dispatch_status"')


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
    owner: str
    holder_session: str
    last_decline: LastMacDecline = NO_PRIOR_MAC_DECLINE
    copy: bool = False
    verb: MacVerb = "nextest"
    source: MacSource = RepositoryWorkingTree()
    target: str = ""
    slot: int = 1
    admission: UnclaimedMacRun | MacClaimFromWait = UnclaimedMacRun()
    environment: tuple[tuple[str, str], ...] = ()
    fetch: FetchFolder | NoFetch = NO_FETCH
    repository_scope: copy_record.RepositoryScope = copy_record.ALL_REPOSITORIES
    kind: str = ""
    placement: DirectOffloadRequest | AssignedOffloadRequest = DirectOffloadRequest()

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

    process: subprocess.Popen[str] | subprocess.Popen[bytes]


class NoClaimInterrupt:
    """No signal arrived while the local claim was completing."""


NO_CLAIM_INTERRUPT = NoClaimInterrupt()


@dataclass
class LocalClaimAttempt:
    """A local claim whose answer is retained before a signal takes effect."""

    answer: MacClaimAnswer = MacClaimNotConfirmed("state", "", "")
    interruption: NoClaimInterrupt | OffloadInterrupted = NO_CLAIM_INTERRUPT
    process: IdleProcess | RunningProcess = field(default_factory=IdleProcess)


ActiveProcess = IdleProcess | RunningProcess | LocalClaimAttempt
IDLE_PROCESS = IdleProcess()


@dataclass(frozen=True)
class MeasuredCleanupSize:
    """The target size measured before removal, in KiB."""

    kib: int


@dataclass(frozen=True)
class CleanupSizeNotMeasured:
    """The target size could not be measured before removal."""


@dataclass(frozen=True)
class CleanupDone:
    """The remote mirror's build output was removed."""

    size: MeasuredCleanupSize | CleanupSizeNotMeasured
    free_gib: float


@dataclass(frozen=True)
class CleanupRemovedWithoutFacts:
    """The remote output was removed without size or free-space facts."""


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


CleanupOutcome = CleanupDone | CleanupRemovedWithoutFacts | CleanupFailed | CleanupSkipped | CleanupKept | CleanupUnconfirmed


@dataclass(frozen=True)
class ReportedMacStatus:
    """The test status printed by the remote command."""

    status: int
    cleanup: CleanupOutcome = CleanupUnconfirmed()
    build_target_directory: str = ""


@dataclass(frozen=True)
class ToolVersionMismatch:
    reason: str


@dataclass(frozen=True)
class MissingMacStatus:
    """The remote command ended without a usable test status."""

    detail: str = 'runner sent no final status'


@dataclass(frozen=True)
class MirrorUseBusy:
    """The mirror lease refused the command before the runner started."""


@dataclass(frozen=True)
class UnresolvedSlotStartup:
    """The Linux slot refused startup because earlier ownership remains unresolved."""


RemoteRunStatus = ReportedMacStatus | MissingMacStatus | UnresolvedSlotStartup | ToolVersionMismatch | MirrorUseBusy
MISSING_MAC_STATUS = MissingMacStatus()


@dataclass(frozen=True)
class UnavailableProbe:
    """A probe that did not return a complete availability snapshot."""

    detail: str = ""


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
        self.kind: str = ""
        self.claim_owner_pid: int = 0
        self.claim_id: str = ""
        self.episode: str = ""
        self.attempt: str = ""
        self.claim_session: str = ""
        self.assignment_file: str = ""
        self.environment: list[str] = []
        self.fetch: str | None = None


class ProcessController:
    """Starts one child at a time and exposes it to signal cleanup."""

    def __init__(self) -> None:
        self.active: ActiveProcess = IDLE_PROCESS
        self.attempt_evidence: AttemptEvidence = AttemptEvidence()
        self.copy_failure_detail: str = ""
        self.placement: DirectOffloadRequest | AssignedOffloadRequest = DirectOffloadRequest()
        self.pending: SettledDispatchOperations | PendingClaimOperation | PendingStartOperation | AcknowledgedStartOperation = SettledDispatchOperations()
        self.expected_claim: claim_state.ClaimIdentity | claim_state.NoClaimIdentity = claim_state.NO_CLAIM_IDENTITY
        self.claim_identity: claim_state.ClaimIdentity | claim_state.NoClaimIdentity = claim_state.NO_CLAIM_IDENTITY
        self.claim_settings: Path | DefaultMacSettings = DEFAULT_MAC_SETTINGS
        self.claim_target: str = ""
        self.claim_scope_key: str = "run"

    def stop(self) -> None:
        active = self.active
        if isinstance(active, LocalClaimAttempt):
            return
        if not isinstance(active, RunningProcess):
            return
        process = active.process
        if process.poll() is not None:
            self.active = IDLE_PROCESS
            if not isinstance(self.pending, PendingStartOperation):
                self.pending = SettledDispatchOperations()
            return
        process.terminate()
        try:
            _ = process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            _ = process.wait()
        self.active = IDLE_PROCESS
        if not isinstance(self.pending, PendingStartOperation):
            self.pending = SettledDispatchOperations()

    def defer_claim_interrupt(self, signal_number: int) -> bool:
        if not isinstance(self.active, LocalClaimAttempt):
            return False
        self.active.interruption = OffloadInterrupted(signal_number)
        return True

    def complete_claim(self, arguments: Sequence[str], attempt: LocalClaimAttempt) -> None:
        """Read the bounded local claim's reply even when the runner is signalled."""
        self.active = attempt
        self.pending = PendingClaimOperation()
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
                self.attempt_evidence.provider = placement.NoProviderClaim()
                attempt.answer = read_claim_answer(COMMAND_COULD_NOT_START)
            else:
                attempt.process = RunningProcess(process)
                stdout, stderr = process.communicate()
                answer = CommandOutput(process.returncode, stdout, stderr)
                expected = self.expected_claim
                attempt.answer = read_claim_answer(answer, expected=expected,
                    assigned_slot=self.placement.assignment.destination.slot
                        if isinstance(self.placement, AssignedOffloadRequest) else 0)
                if answer.status in (0, 13):
                    self.claim_identity = claim_state.reply_claim_identity(stdout)
                elif isinstance(self.placement, AssignedOffloadRequest):
                    self.inspect_claim(attempt, expected)
        finally:
            if isinstance(attempt.process, RunningProcess) and attempt.process.process.poll() is None:
                process = attempt.process.process
                process.terminate()
                try:
                    _ = process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    _ = process.wait()
            self.active = IDLE_PROCESS
            self.pending = SettledDispatchOperations()

    def inspect_claim(self, attempt: LocalClaimAttempt,
                      expected: claim_state.ClaimIdentity | claim_state.NoClaimIdentity) -> None:
        """Inspect an uncertain response without another acquisition."""
        if not isinstance(self.placement, AssignedOffloadRequest) or not isinstance(expected, claim_state.ClaimIdentity):
            return
        assigned = self.placement.assignment
        paths = claim_state.state_paths(assigned.destination.target)
        with claim_state.locked(paths.lock):
            for held in claim_state.read_claims(paths):
                record = held.record
                if (claim_state.claim_identity(record) != expected or held.scope != assigned.destination.scope
                        or record["pid"] != os.getpid()
                        or record["submitting_session"] != claim_state.session_id(assigned.session)):
                    continue
                start = process_identity.process_start(os.getpid())
                if not isinstance(start, process_identity.Started) or start.start != record["proc_start"]:
                    continue
                self.claim_identity = expected
                if "remote_host" in record and record.get("cleanup_pending"):
                    attempt.answer = TakenOverMacClaim(record["remote_host"], record.get("slot", 1))
                elif "remote_host" in record:
                    attempt.answer = FreshMacClaim(record.get("slot", 1))
                return

    def capture(
        self,
        arguments: Sequence[str],
        deadline: CommandDeadline = NO_COMMAND_TIME_LIMIT,
    ) -> CapturedCommandResult:
        if arguments[0] == "rsync":
            arguments = [*arguments[:2], "--stats", *arguments[2:]]
        try:
            process = subprocess.Popen(
                arguments,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
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
                stdout, stderr = process.communicate()
                self.observe_command(arguments, stdout, stderr)
                if arguments[0] in ("ssh", "rsync"):
                    self.attempt_evidence.traffic.transfers[-1]["complete"] = False
                return COMMAND_TIMED_OUT
            self.observe_command(arguments, stdout, stderr)
            return CommandOutput(process.returncode,
                stdout.decode().replace("\r\n", "\n").replace("\r", "\n"),
                stderr.decode().replace("\r\n", "\n").replace("\r", "\n"))
        finally:
            self.active = IDLE_PROCESS

    def observe_command(self, arguments: Sequence[str], stdout: bytes, stderr: bytes) -> None:
        if arguments[0] not in ("ssh", "rsync"):
            return
        if arguments[0] == "ssh":
            operation = "ssh_control"
        elif any(word.startswith("--files-from=") for word in arguments):
            operation = "rsync_scripts"
        elif ":" in arguments[-2]:
            operation = "rsync_fetch"
        else:
            operation = "rsync_source"
        self.attempt_evidence.traffic.observe_bytes(operation, list(arguments), stdout, stderr)

    def capture_copy(
        self, arguments: Sequence[str], deadline: CommandDeadline = NO_COMMAND_TIME_LIMIT,
    ) -> CapturedCommandResult:
        """Keep command errors for the copy refusal."""
        result = self.capture(arguments, deadline)
        if isinstance(result, CommandOutput):
            if result.status != 0 and result.stderr.strip():
                error = "; ".join(result.stderr.strip().splitlines())
                self.copy_failure_detail = f"copy: {arguments[0]}: {error}"
        else:
            error = "timed out" if isinstance(result, CommandTimedOut) else "could not start"
            self.copy_failure_detail = f"copy: {arguments[0]}: {error}"
        return result

    def stream_mac_run(self, arguments: Sequence[str]) -> RemoteRunStatus:
        self.pending = PendingStartOperation()
        try:
            process = subprocess.Popen(
                arguments,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
        except OSError:
            self.pending = SettledDispatchOperations()
            return MISSING_MAC_STATUS
        self.attempt_evidence.execution = placement.ExecutionEvidenceUnavailable(
            "the remote start has no final observation", time.time())
        raw_output = process.stdout
        if raw_output is None:
            process.kill()
            _ = process.wait()
            return MISSING_MAC_STATUS
        output = cast(BinaryIO | TextIO, raw_output)
        self.active = RunningProcess(process)
        status: RemoteRunStatus = MISSING_MAC_STATUS
        cleanup: CleanupOutcome = CleanupUnconfirmed()
        held_status_line = ""
        mismatch = ""
        mirror_refused = False
        build_target_directory = ""
        received_bytes = 0
        finished_output = False
        try:
            for raw_line in output:
                if isinstance(raw_line, bytes):
                    received_bytes += len(raw_line)
                    line = raw_line.decode()
                else:
                    received_bytes += len(raw_line.encode("utf-8"))
                    line = raw_line
                line = line.replace("\r\n", "\n").replace("\r", "\n")
                marker = line.rstrip("\r\n")
                identity = self.claim_identity
                if (isinstance(identity, claim_state.ClaimIdentity)
                        and marker == "dispatch_start_settled=" + pending_start_key(identity)):
                    self.pending = SettledDispatchOperations()
                    pending_start_path(self.claim_target, identity).unlink(missing_ok=True)
                    if not isinstance(status, ReportedMacStatus):
                        self.attempt_evidence.execution = placement.ExecutionNotStarted()
                    continue
                if (isinstance(identity, claim_state.ClaimIdentity)
                        and marker == "dispatch_start_pending=" + pending_start_key(identity)):
                    self.pending = PendingStartOperation()
                    continue
                if mirror_busy_refusal(marker):
                    mirror_refused = True
                if marker.startswith("build_target_directory="):
                    build_target_directory = marker.removeprefix("build_target_directory=")
                    continue
                if re.match(r"[A-Za-z0-9_-]+ version mismatch: ", marker):
                    mismatch = marker
                    print(line, end="", flush=True)
                    continue
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
                        self.pending = AcknowledgedStartOperation()
                        held_status_line = line
                    else:
                        held_status_line = ""
                        print(line, end="", flush=True)
                    cleanup = CleanupUnconfirmed()
                    continue
                if marker.startswith("mac_trim="):
                    print(line, end="", flush=True)
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
            finished_output = True
            exit_status = process.wait()
        finally:
            if process.poll() is None:
                self.stop()
            self.active = IDLE_PROCESS
            startup_refused = isinstance(status, UnresolvedSlotStartup)
            if isinstance(self.pending, AcknowledgedStartOperation) or mirror_refused or mismatch or startup_refused:
                self.pending = SettledDispatchOperations()
            if (mirror_refused or mismatch or startup_refused) and not isinstance(status, ReportedMacStatus):
                self.attempt_evidence.execution = placement.ExecutionNotStarted()
            output.close()
            if arguments[0] == "ssh":
                self.attempt_evidence.traffic.transfers.append({"operation": "ssh_run",
                    "sent_bytes": len(arguments[-1].encode("utf-8")),
                    "received_bytes": received_bytes, "complete": finished_output})
        if mismatch and not isinstance(status, (ReportedMacStatus)):
            return ToolVersionMismatch(mismatch)
        if isinstance(status, ReportedMacStatus):
            return ReportedMacStatus(status.status, cleanup, build_target_directory)
        if mirror_refused:
            return MirrorUseBusy()
        if isinstance(status, UnresolvedSlotStartup):
            return status
        return MissingMacStatus(f"runner exited with status {exit_status} without a final status")


def parse_cleanup(value: str) -> CleanupOutcome:
    removed = re.fullmatch(r"0 ([0-9]+|-) ([0-9]+(?:[.][0-9]+)?)", value)
    if removed:
        size = CleanupSizeNotMeasured() if removed[1] == "-" else MeasuredCleanupSize(int(removed[1]))
        return CleanupDone(size, float(removed[2]))
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
    if rc == 0:
        return CleanupRemovedWithoutFacts()
    return CleanupFailed(rc) if 0 < rc <= 255 else CleanupUnconfirmed()


def report_cleanup(cleanup: CleanupOutcome, target: str, *, path: str, free_floor_gib: float) -> None:
    from scripts.lint import sweep

    label = target or "Mac"
    if isinstance(cleanup, CleanupDone):
        size = sweep.gib(cleanup.size.kib * 1024) if isinstance(cleanup.size, MeasuredCleanupSize) else "size not measured"
        print(f"{label} build output removed: {path}, {size}; "
              + f"Mac free {cleanup.free_gib:g} GiB under the {free_floor_gib:g} GiB floor", flush=True)
    elif isinstance(cleanup, CleanupFailed):
        print(f"{label} build output not removed: {cleanup.rc}", flush=True)
    elif isinstance(cleanup, CleanupSkipped):
        print(f"{label} build output kept: the run used another target dir", flush=True)
    elif isinstance(cleanup, CleanupUnconfirmed):
        print(f"{label} build output cleanup unconfirmed", flush=True)


def config_path() -> Path:
    return conf.path("mac_test")


def configured_ssh_target(target: str) -> target_config.SshTarget:
    """Resolve an SSH target before access to its coordination state."""
    return target_config.configured_ssh_target(target)


def ssh_target_state_directory(target: str) -> Path:
    """Return the state path without requiring target registration."""
    return target_config.ssh_target_state_directory(target)


def state_directory(target: str = "mac") -> Path:
    target = target or "mac"
    if target != "mac":
        _ = configured_ssh_target(target)
    return ssh_target_state_directory(target)


def read_connection_settings(path: Path | DefaultMacSettings = DEFAULT_MAC_SETTINGS) -> SshConnectionSettings:
    settings = config_path() if isinstance(path, DefaultMacSettings) else path.expanduser()
    return connection_settings(conf.read(settings))


def connection_settings(config: conf.ConfigRead) -> SshConnectionSettings:
    return SshConnectionSettings(config.text("host"), config.number("connect_timeout_s"),
                                 config.number("leftover_timeout_s"))


def read_config(path: Path | DefaultMacSettings = DEFAULT_MAC_SETTINGS) -> OffloadConfig:
    settings = config_path() if isinstance(path, DefaultMacSettings) else path.expanduser()
    config = conf.read(settings)
    values = config.values()
    values["mirror_busy_backoff_s"] = str(conf.read("mac_test").number("mirror_busy_backoff_s"))
    values["abandoned_session_minutes"] = str(conf.read("mac_test").whole("abandoned_session_minutes"))
    return OffloadConfig(
        enabled=config.text("offload") == "on",
        connection=connection_settings(config),
        probe_timeout_s=config.number("probe_timeout_s"),
        unreachable_backoff_s=config.number("unreachable_backoff_s"),
        free_floor_gib=config.number("free_floor_gib"),
        default_target_budget_gib=config.text("target_budget_gib" if "target_budget_gib" in values else "mac_budget_gib"),
        values=values,
        source=settings,
    )


def repository_identity(repo_root: Path) -> RepositoryIdentity:
    name = repository.repo_name(str(repo_root))
    common = repository.git_common_dir(str(repo_root))
    if name is None or common is None:
        return UNKNOWN_REPOSITORY
    source = Path(repository.source_git_dir(common))
    if not (source / "config").is_file():
        return KnownRepository(name)
    key = repository.repository_key(str(source.parent))
    return KnownRepository(name) if key == store.UNKNOWN_IDENTITY else KnownRepository(name, key)


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
    evidence: AttemptEvidence | None = None,
    fetch: FetchFolder | NoFetch = NO_FETCH,
) -> int:
    """Write the wire form and return the exit status for one outcome."""
    label = target or "Mac"
    if isinstance(result, (PassedOnMac, PassedOnTarget)):
        mac = "passed"
        reason = ""
        status = 0
    elif isinstance(result, NoTestMatchedOnMac):
        mac = "declined"
        reason = "no_tests"
        status = 75
        print(f"mac_test: no test matched on the {label}; running on natedev instead")
    elif isinstance(result, (FailedOnMac, FailedOnTarget)):
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
        reason = result.detail if target else ""
        status = 75
        print(f"mac_test: the {label} {result.detail}; local fallback requires admission")
    else:
        mac = "declined"
        reason = result.detail if isinstance(result, DeclinedMacRun) and result.detail else result.reason
        status = 75
        description = reason
        if isinstance(result, DeclinedMacRun) and result.reason == "disk":
            reason = "disk"
            description = f"disk: {result.detail}" if result.detail else reason
        elif isinstance(result, DeclinedMacRun) and result.reason == "source_changed":
            reason = "source_changed"
        if not decline_already_shown(result.reason, last_decline):
            print(f"mac_test: not running on the {label} ({description})")
    fetched = result.fetch if isinstance(result, (PassedOnMac, FailedOnMac, NoTestMatchedOnMac)) else FetchNotRun()
    if isinstance(destination, ResultFile):
        holder_line = f"holder={holder_value(result.holder)}\n" if isinstance(result, BusyMacDecline) else ""
        linux = bool(target) and not target_config.uses_legacy_mac_layout(configured_ssh_target(target).platform)
        prefix = f"outcome={mac}\ntarget={target}" if linux else f"mac={mac}"
        details = ""
        if linux:
            evidence = evidence or AttemptEvidence(evidence_at=time.time())
            count = str(evidence.unresolved_count) if isinstance(evidence.unresolved_count, int) else "unavailable"
            details = (f"slot={evidence.slot}\ncompletion={evidence.completion}\ncleanup={evidence.cleanup}\n"
                       + f"unresolved_count={count}\nevidence_at={evidence.evidence_at}\n")
        if isinstance(result, (PassedOnMac, PassedOnTarget)) and result.build_target_directory:
            details += f'build_target_directory={result.build_target_directory}\n'
        _ = destination.path.write_text(
            f"{prefix}\nreason={reason}\nseconds={result.seconds:.6f}\n" + holder_line + details
            + (f"fetch={fetch_word(fetched)}\n" if isinstance(fetch, FetchFolder) else ""),
            encoding="utf-8",
        )
    if isinstance(fetch, FetchFolder):
        report_fetch(fetch, fetched)
    return status


def backoff_path(target: str = "mac") -> Path:
    return state_directory(target) / "unreachable_until"


def mirror_busy_path(repository: str, target: str = "mac") -> Path:
    """Keep one refusal time for each target and repository."""
    return state_directory(target) / f"mirror-busy-{quote(repository, safe='')}.until"


def read_mirror_busy(now: float, repository: str, target: str = "mac") -> BackoffState:
    try:
        until = float(mirror_busy_path(repository, target).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return NO_BACKOFF
    return ACTIVE_BACKOFF if math.isfinite(until) and until > now else NO_BACKOFF


def write_mirror_busy(config: OffloadConfig, repository: str, target: str) -> None:
    path = mirror_busy_path(repository, target)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path, f"{time.time() + config.mirror_busy_backoff_s:.6f}\n")


def mirror_busy_refusal(detail: str) -> bool:
    return re.search(r"\bcopies: [^\n]+: busy; held by session ", detail) is not None


@dataclass(frozen=True)
class FailedProbe:
    """The target's last completed probe failed."""


@dataclass(frozen=True)
class NoFailedProbe:
    """The target has no failed probe record."""


FailedProbeState = FailedProbe | NoFailedProbe


def read_failed_probe(target: str = "mac") -> FailedProbeState:
    return FailedProbe() if (state_directory(target) / "failed_probe").exists() else NoFailedProbe()


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


def ssh_options(config: SshConnectionSettings) -> list[str]:
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
    return ["ssh", *ssh_options(config.connection), config.host]


def linux_mirror_root() -> Path:
    return Path('.local/state/build-target/mirror')


def mac_mirror_root() -> MacMirrorRoot:
    """Read the remote mirror root without expanding the local home folder."""
    return MacMirrorRoot.read(conf.read("mac_test"))


def remote_mirror(repository: KnownRepository, target: str = "mac", slot: int = 1) -> str:
    if target_config.uses_legacy_mac_layout(configured_ssh_target(target or "mac").platform):
        return f"{mac_mirror_root().relative}/{repository.legacy_mirror_name}"
    if slot < 1:
        raise ValueError("slot must be positive")
    name = build_folders.repository_key_for(repository.policy_name, repository.key or repository.name)
    return str(build_folders.build_folder_path(build_folders.BuildFolderSpec(
        kind="mirror", parent=linux_mirror_root(), repository=name, owner=target, slot=slot, suffix_length=0)))


def mirror_target(repository: KnownRepository, target: str, slot: int) -> str:
    return f"{remote_mirror(repository, target, slot)}/target"


def probe_script(repository: KnownRepository, target: str = "mac", slot: int = 1) -> str:
    from scripts.copies import copies

    if not target_config.uses_legacy_mac_layout(configured_ssh_target(target or "mac").platform):
        from scripts.mac_test import linux_runner
        return linux_runner.probe_script(remote_mirror(repository, target, slot))
    return "\n".join(
        (
            copies.mirror_probe_setup(remote_mirror(repository, target, slot)),
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


def claim_arguments(request: OffloadRequest, config: OffloadConfig, leased_slots: tuple[int, ...] = ()) -> list[str]:
    copy_words = ["--copy"] if request.copy else []
    identity = (claim_state.ClaimIdentity(uuid.uuid4().hex, request.call_id, uuid.uuid4().hex)
                if isinstance(request.admission, UnclaimedMacRun) else request.admission.identity)
    if isinstance(request.placement, AssignedOffloadRequest):
        assignment = request.placement.assignment
        if isinstance(request.admission, UnclaimedMacRun):
            identity = claim_state.ClaimIdentity(uuid.uuid5(uuid.NAMESPACE_URL,
                placement.attempt_key(assignment.episode, assignment.attempt)).hex, assignment.episode, assignment.attempt)
        leased_slots = (assignment.destination.slot,) if assignment.destination.slot else ()
    session = (request.holder_session if isinstance(request.admission, UnclaimedMacRun)
               else claim_state.session_id(request.admission.submitting_session))
    if isinstance(request.placement, AssignedOffloadRequest):
        session = claim_state.session_id(request.placement.assignment.session)
    identity_words = ["--claim-id", identity.claim_id, "--episode", identity.episode, "--attempt", identity.attempt]
    selection = ["--target", request.target] if request.target else []
    handoff = (["--from-pid", str(request.admission.owner_pid)]
               if isinstance(request.admission, MacClaimFromWait) else [])
    return [
        str(Path(__file__).resolve().parents[1] / "lib/py"),
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
        *identity_words,
        *assigned_claim_arguments(request),
        "--identity", "--session", session,
        *selection,
        *handoff,
        *[word for slot in leased_slots for word in ("--leased-slot", str(slot))],
    ]


def release_claim(controller: ProcessController, target: str = "") -> ClaimReleaseConfirmed | ClaimReleaseUnconfirmed:
    if not isinstance(controller.pending, SettledDispatchOperations):
        return ClaimReleaseUnconfirmed("a claim or start operation remains pending")
    identity = controller.claim_identity
    if isinstance(identity, claim_state.NoClaimIdentity):
        return ClaimReleaseUnconfirmed("the exact claim identity is unavailable")
    result = controller.capture(
        [
            str(Path(__file__).resolve().parents[1] / "lib/py"),
            str(Path(__file__).with_name("mac_test.py")),
            "release",
            "--pid",
            str(os.getpid()),
            "--claim-id", identity.claim_id,
            "--episode", identity.episode,
            "--attempt", identity.attempt,
            *(["--settings", str(controller.claim_settings)] if isinstance(controller.claim_settings, Path) else []),
            *(["--target", target] if target else []),
        ]
    )
    if isinstance(result, CommandOutput):
        if result.status == 0:
            controller.claim_identity = claim_state.NO_CLAIM_IDENTITY
        for line in result.stdout.splitlines():
            if line.startswith("copy_bytes="):
                transfers = cast(list[buildlog_sync.TransferBytes], json.loads(line.removeprefix("copy_bytes=")))
                controller.attempt_evidence.traffic.transfers.extend(transfers)

    if isinstance(result, CommandOutput) and result.status == 0:
        pending_start_path(target, identity).unlink(missing_ok=True)
        return ClaimReleaseConfirmed()
    return ClaimReleaseUnconfirmed("the provider did not confirm exact release")


def assigned_claim_arguments(request: OffloadRequest) -> list[str]:
    if not isinstance(request.placement, AssignedOffloadRequest):
        return []
    from scripts.build_queue import admission_protocol as wire

    return ["--assignment", wire.encode_reply(wire.Assignment(request.placement.assignment)).strip()]


def read_claim_answer(result: CapturedCommandResult, *,
                      expected: claim_state.ClaimIdentity | claim_state.NoClaimIdentity = claim_state.NO_CLAIM_IDENTITY,
                      assigned_slot: int = 0) -> MacClaimAnswer:
    if not isinstance(result, CommandOutput):
        return MacClaimNotConfirmed("state", "", "")
    slots = [line.removeprefix("slot: ") for line in result.stdout.splitlines() if line.startswith("slot: ")]
    if slots and (len(slots) != 1 or not slots[0].isascii() or not slots[0].isdecimal() or int(slots[0]) < 1):
        return MacClaimNotConfirmed("state", result.stdout, result.stderr)
    slot = int(slots[0]) if slots else 1
    if result.status in (0, 13) and isinstance(claim_state.reply_claim_identity(result.stdout), claim_state.NoClaimIdentity):
        return MacClaimNotConfirmed("state", result.stdout, result.stderr)
    if result.status == 0:
        if ((isinstance(expected, claim_state.ClaimIdentity)
             and claim_state.reply_claim_identity(result.stdout) != expected)
                or assigned_slot and slot != assigned_slot):
            return MacClaimNotConfirmed("state", result.stdout, result.stderr)
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
    from scripts.lib import nix_env
    envrc = nix_env.nearest_envrc(root)
    if envrc is not None:
        if nix_env.trusted_envrc(envrc):
            return True
        return isinstance(nix_env.ensure(root), nix_env.Loaded)
    return nix_env.trusted_root(root) or nix_env.registered_worktree(root) or nix_env.generated_source(root)


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
    from scripts.mac_test.linux_runner import normalize_tool_version
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
        expected[key] = normalize_tool_version(value)
    return {"rustc": expected["rustc"], "nextest": expected["nextest"],
            "mend": expected["mend"], "scripts_commit": expected["scripts_commit"]}


def recorded_linux_version_mismatch(controller: ProcessController, config: OffloadConfig,
                                    target: str) -> str:
    """Compare the tree with the tools recorded by deployment without contacting the box."""
    from scripts.mac_test import linux_runner
    try:
        raw = cast(object, json.loads((target_config.ssh_target_state_directory(target) / "box-tools.json").read_text()))
        if not isinstance(raw, dict):
            return ""
        versions = cast(dict[str, object], raw).get("versions")
        if not isinstance(versions, dict):
            return ""
        entries = cast(dict[str, object], versions)
        if any(not isinstance(value, str) for value in entries.values()):
            return ""
        actual = {key: linux_runner.normalize_tool_version(cast(str, value)) for key, value in entries.items()}
        expected = expected_linux_parity(controller, config)
        if isinstance(expected, UnavailableToolParity):
            return ""
        selected = {key: linux_runner.normalize_tool_version(value) for key, value in (
            ("rustc", expected["rustc"]), ("nextest", expected["nextest"]), ("mend", expected["mend"])) if key in actual}
        return linux_runner.version_mismatch(target, actual, selected, config.values.get("version_pin_file", ""))
    except (OSError, ValueError, UnicodeError):
        return ""


def linux_version_decline_path(target: str, repository_name: str) -> Path:
    """Use one decline record for each repository name."""
    return state_directory(target) / f"version-decline-{repository_name}"


def write_version_decline(target: str, repository_name: str) -> None:
    path = linux_version_decline_path(target, repository_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_text(str(time.time()), encoding="utf-8")


def linux_version_declined_recently(target: str, repository_name: str, hold_s: float, now: float) -> bool:
    """Read whether this repository has a decline within the hold duration."""
    try:
        return 0 <= now - float(linux_version_decline_path(target, repository_name).read_text()) < hold_s
    except (OSError, ValueError, UnicodeError):
        return False


def copy_tracked_scripts(controller: ProcessController, config: OffloadConfig) -> bool:
    """Send every tracked script and config file without Git metadata."""
    from scripts.copies import copies

    controller.copy_failure_detail = ""
    root = Path(__file__).resolve().parents[2]
    listing = controller.capture_copy(["git", "-C", str(root), "ls-files", "-z", "scripts", "config"])
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
    clean = controller.capture_copy(["git", "-C", str(root), "diff", "HEAD", "--quiet", "--", *paths])
    if not isinstance(clean, CommandOutput) or clean.status != 0:
        return False
    expected = expected_linux_parity(controller, config)
    if isinstance(expected, UnavailableToolParity):
        return False
    commit = controller.capture_copy(["git", "-C", str(root), "rev-parse", "HEAD"],
                                CommandTimeLimit(config.probe_timeout_s))
    if not isinstance(commit, CommandOutput) or commit.status != 0 or commit.stdout.strip() != expected["scripts_commit"]:
        return False
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", prefix="target-scripts-") as manifest:
        _ = manifest.write("\n".join(paths) + "\n")
        manifest.flush()
        result = controller.capture_copy(["rsync", copies.RSYNC_RECEIVER_WRITE_TIME_FLAGS, "--compress", f"--files-from={manifest.name}",
                                     "-e", shlex.join(["ssh", *ssh_options(config.connection)]),
                                     f"{root}/", f"{config.host}:.claude/"])
    if not isinstance(result, CommandOutput) or result.status != 0:
        return False
    clean = controller.capture_copy(["git", "-C", str(root), "diff", "HEAD", "--quiet", "--", *paths])
    current_commit = controller.capture_copy(["git", "-C", str(root), "rev-parse", "HEAD"],
                                       CommandTimeLimit(config.probe_timeout_s))
    if (not isinstance(clean, CommandOutput) or clean.status != 0
            or not isinstance(current_commit, CommandOutput) or current_commit.status != 0
            or current_commit.stdout != commit.stdout):
        return False
    stamp = controller.capture_copy([*ssh_prefix(config),
        "printf '%s\\n' " + shlex.quote(commit.stdout.strip())
        + ' > "$HOME/.claude/.build-target-script-commit"'])
    return isinstance(stamp, CommandOutput) and stamp.status == 0


def probe_mac(
    controller: ProcessController,
    config: OffloadConfig,
    repository: KnownRepository,
    target: str = "mac",
    slot: int = 1,
    *, record_version_decline: bool = True,
) -> ProbeResult:
    mark = state_directory(target) / "failed_probe"

    def failed() -> UnavailableProbe:
        mark.parent.mkdir(parents=True, exist_ok=True)
        _ = mark.write_text("failed\n", encoding="utf-8")
        return UNAVAILABLE_PROBE

    def succeeded(probe: ProbeSnapshot) -> ProbeSnapshot:
        mark.unlink(missing_ok=True)
        return probe

    linux = not target_config.uses_legacy_mac_layout(configured_ssh_target(target or "mac").platform)
    if linux:
        from scripts.mac_test import linux_runner
        script = linux_runner.probe_command_script(remote_mirror(repository, target, slot), runner_values(config, target),
                                                   repository.policy_name)
        if isinstance(probe_reachability(controller, config), UnreachableHost):
            print(f"mac_test: could not reach {target}: SSH reachability check failed",
                  file=sys.stderr, flush=True)
            return failed()
        timeout_s = linux_runner.positive_setting(config.values, "shell_timeout_s") + config.probe_timeout_s
    else:
        script = probe_script(repository, target, slot)
        timeout_s = config.probe_timeout_s
    identity = controller.claim_identity
    if isinstance(identity, claim_state.ClaimIdentity):
        path = pending_start_path(target, identity)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(path, pending_start_key(identity))
        script = ("(\n" + script + "\n)\nprobe_status=$?\n"
                  + "if [ \"$probe_status\" -eq 0 ]; then\n"
                  + prepare_start_fence_command(identity, target, scope_key=controller.claim_scope_key) + " || exit 1\nfi\n"
                  + 'exit "$probe_status"')
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
        reason = next((line.strip() for line in reversed(result.stderr.splitlines()) if line.strip()),
                      f"SSH probe exited with status {result.status}")
    else:
        probe = parse_probe(result.stdout)
        if isinstance(probe, ProbeSnapshot):
            if not linux:
                return succeeded(probe)
            expected = expected_linux_parity(controller, config)
            values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
            if not isinstance(expected, UnavailableToolParity) and all(values.get(key) == expected[key] for key in ("nextest", "mend", "scripts_commit")):
                return succeeded(probe)
            from scripts.mac_test import linux_runner
            detail = (linux_runner.version_mismatch(target, values,
                      {"nextest": expected["nextest"], "mend": expected["mend"]},
                      config.values.get("version_pin_file", ""))
                      if not isinstance(expected, UnavailableToolParity) else "")
            if detail:
                print(detail, file=sys.stderr, flush=True)
                if record_version_decline:
                    write_version_decline(target, repository.name)
                return UnavailableProbe(detail)
            print(f"mac_test: {target} is not ready: public tools or tracked scripts differ", file=sys.stderr)
            return failed()
        reason = "the probe answer could not be read"
    print(f"mac_test: could not reach the {'Mac' if target in ('', 'mac') else target}: {reason}", file=sys.stderr, flush=True)
    return failed()


@dataclass(frozen=True)
class MacDiskReady:
    """The Mac meets the disk minimum."""

    free_gib: float


@dataclass(frozen=True)
class SweepShort:
    """The Mac remains below the disk minimum after the sweep."""

    free_gib: float


@dataclass(frozen=True)
class SweepFailed:
    """The sweep or its final disk reading failed."""

    cause: str


def ensure_mac_disk_floor(
    controller: ProcessController, config: OffloadConfig, repository: KnownRepository,
    probe: ProbeSnapshot, target: str = "mac", slot: int = 1,
) -> MacDiskReady | SweepShort | SweepFailed:
    """Run one shared floor sweep, then read the Mac disk space again."""
    if probe.free_gib >= config.free_floor_gib:
        return MacDiskReady(probe.free_gib)
    try:
        directory = state_directory()
        directory.mkdir(parents=True, exist_ok=True)
        # Keep the inode stable so every waiter uses the same lock.
        path = directory / f"floor-sweep-{quote(config.host, safe='')}.lock"
        with path.open("a+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                fcntl.flock(lock, fcntl.LOCK_EX)
            else:
                _ = lock.seek(0)
                _ = lock.truncate()
                _ = lock.write("floor sweep was interrupted")
                lock.flush()
                settings = config_path() if isinstance(config.source, DefaultMacSettings) else config.source
                timeout = conf.read(settings).number("floor_sweep_timeout_s")
                result = controller.capture(
                    [*ssh_prefix(config),
                     "~/.claude/scripts/lib/py ~/.claude/scripts/lint/sweep.py --floor-only --host mac"],
                    CommandTimeLimit(timeout),
                )
                if isinstance(result, CommandTimedOut):
                    cause = f"floor sweep timed out after {timeout:g} s"
                elif isinstance(result, CommandCouldNotStart):
                    cause = "floor sweep could not start"
                elif result.status != 0:
                    detail = " ".join(result.stderr.split())
                    cause = f"floor sweep exited with status {result.status}" + (f": {detail}" if detail else "")
                else:
                    cause = "ok"
                _ = lock.seek(0)
                _ = lock.truncate()
                _ = lock.write(cause)
                lock.flush()
            _ = lock.seek(0)
            cause = lock.read()
            after = probe_mac(controller, config, repository, target, slot)
            if cause != "ok":
                return SweepFailed(cause or "floor sweep result missing")
            if not isinstance(after, ProbeSnapshot):
                return SweepFailed("Mac disk reading failed after the floor sweep")
            if after.free_gib < config.free_floor_gib:
                return SweepShort(after.free_gib)
            return MacDiskReady(after.free_gib)
    except (OSError, ValueError) as error:
        return SweepFailed(f"floor sweep unavailable: {error}")


def ignored_paths(
    controller: ProcessController, repo_root: Path
) -> IgnoredPathListingResult:
    result = controller.capture_copy(
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
    from scripts.copies import copies

    controller.copy_failure_detail = ""
    ignored = () if isinstance(request.source, StagedDocSnapshot) else ignored_paths(controller, request.repo_root)
    if isinstance(ignored, IgnoredPathListingFailed):
        return False
    mirror = request_mirror(config, request, repository)
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
                result = controller.capture_copy(copies.remote_rsync(
                    request.source_root, mirror, exclude_path, ssh_options(config.connection)))
                return isinstance(result, CommandOutput) and result.status == 0
            finally:
                exclude_path.unlink(missing_ok=True)
    except OSError as error:
        controller.copy_failure_detail = f"copy: {error}"
        return False


def copy_tree_with_key(
    controller: ProcessController,
    config: OffloadConfig,
    request: OffloadRequest,
    repository: KnownRepository,
) -> CopiedTree | TreeCopyFailed:
    before = read_tree_key(request.source_root)
    if not copy_tree(controller, config, request, repository):
        return TreeCopyFailed(controller.copy_failure_detail)
    after = read_tree_key(request.source_root)
    if isinstance(before, KnownTreeKey) and isinstance(after, KnownTreeKey) and before == after:
        return CopiedTree(before)
    return CopiedTree(UNAVAILABLE_TREE_KEY)


def nextest_words_with_skip(
    words: tuple[str, ...], skip_expression: str, *, insert_filter: bool = False
) -> tuple[str, ...]:
    if not skip_expression:
        return words
    from scripts.mac_test import linux_runner
    return linux_runner.nextest_words_with_skip(words, skip_expression, insert_filter=insert_filter)


def runner_values(config: OffloadConfig, target: str) -> dict[str, str]:
    return {**config.values, 'selected_target': target,
            'pinned_target': os.environ.get('BUILDLOG_PINNED_TARGET', '')}


def remote_cleanup_command(config: OffloadConfig, request: OffloadRequest, repository: KnownRepository) -> str:
    """Trim the mirror budget before the run releases its lease."""
    if any(word == "--target-dir" or word.startswith("--target-dir=") for word in request.nextest_words):
        return "echo mac_cleanup=skipped"
    budget = config.values.get(f"mac_budget_gib.{repository.name}", config.default_target_budget_gib)
    target = shlex.quote(mirror_target(repository, request.target, request.slot))
    program = (
        'import sys; from scripts.lint import sweep; from scripts.lib import conf; '
        'config = conf.read("lint"); '
        'sys.exit(sweep.sweep_workspace(sys.argv[1], config, False, '
        'config.number(sweep.RECENT_KEY)))'
    )
    floor = shlex.quote(f"{config.free_floor_gib:g}")
    return (
        f'cd "$HOME"/{shlex.quote(remote_mirror(repository, request.target, request.slot))} && '
        + 'trim_started=$(date +%s); '
        + f'if LINT_SWEEP_BUDGET_GIB={shlex.quote(budget)} "$HOME/.claude/scripts/lib/py" '
        + f'-B -c {shlex.quote(program)} "$HOME"/{target} >/dev/null 2>&1; then '
        + 'trim_status=0; else trim_status=$?; fi; '
        + 'trim_elapsed=$(( $(date +%s) - trim_started )); '
        + f'if trim_size=$(du -sk "$HOME"/{target} 2>/dev/null); then '
        + 'trim_size=${trim_size%%[[:space:]]*}; '
        + f'over_budget=$(awk -v size="$trim_size" -v budget={shlex.quote(budget)} '
        + "'BEGIN { print (size > budget * 1048576) ? \"yes\" : \"no\" }'); "
        + 'else over_budget=unknown; fi; '
        + "printf 'mac_trim=status=%s elapsed_seconds=%s over_budget=%s\\n' "
        + '"$trim_status" "$trim_elapsed" "$over_budget"; '
        + 'if free_gib=$(df -g "$HOME"); then '
        + "free_gib=$(printf '%s\\n' \"$free_gib\" | awk 'NR == 2 {print $4}'); "
        + 'else free_gib=; fi; '
        + f'if awk -v free="$free_gib" -v floor={floor} '
        + "'BEGIN { exit !(free ~ /^[0-9]+([.][0-9]+)?$/ && (free + 0) < (floor + 0)) }'; then "
        + f'if size_kib=$(du -sk "$HOME"/{target} 2>/dev/null); then '
        + "size_kib=${size_kib%%[[:space:]]*}; else size_kib=-; fi; "
        + f'if rm -rf -- "$HOME"/{target}; then '
        + "printf 'mac_cleanup=0 %s %s\\n' \"$size_kib\" \"$free_gib\"; "
        + "else echo mac_cleanup=$?; fi; else echo mac_cleanup=kept; fi"
    )


def remote_test_command(
    config: OffloadConfig,
    request: OffloadRequest,
    repository: KnownRepository,
    sent_tree_key: TreeKey = UNAVAILABLE_TREE_KEY,
) -> str:
    clear_environment = (result_environment.read_policy().clear_remote_environment()
                         if request.verb == "nextest" and request.environment else "")
    if not target_config.uses_legacy_mac_layout(configured_ssh_target(request.target or "mac").platform):
        from scripts.mac_test import linux_runner
        words = request.nextest_words
        return linux_runner.command_script(
            remote_mirror(repository, request.target, request.slot),
            {**runner_values(config, request.target), "step_kind": request.kind or verb_kind(request.verb)},
            repository.policy_name, request.verb, words, request.call_id,
            sent_tree_key.value if isinstance(sent_tree_key, KnownTreeKey) else "",
            repository.key, request.environment, clear_environment,
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
    repo_identity = f"BUILDLOG_REPO_IDENTITY={shlex.quote(repository.key)} " if repository.key else ""
    target = shlex.quote(mirror_target(repository, request.target, request.slot))
    custom_target = any(word == "--target-dir" or word.startswith("--target-dir=") for word in words)
    if isinstance(request.fetch, FetchFolder):
        cleanup_command = "echo mac_cleanup=kept"
    else:
        cleanup_command = remote_cleanup_command(config, request, repository)
    abandoned_sessions = ""
    if not custom_target:
        abandoned_sessions = removal_script(f'"$HOME"/{target}', config.abandoned_session_minutes) + " && "
    forced_config = "LINT_CONFIG_FORCE=1 " if request.verb == "doc" else ""
    remove_folder = f"rm -rf -- {shlex.quote(request.fetch.value)} && " if isinstance(request.fetch, FetchFolder) else ""
    environment = "".join(f"{name}={shlex.quote(value)} " for name, value in request.environment)
    clear_environment = clear_environment + " && " if clear_environment else ""
    return (
        f"cd {shlex.quote(remote_mirror(repository, request.target, request.slot))} && "
        f"{abandoned_sessions}{remove_folder}{clear_environment}{environment}"
        'PATH="$HOME/.cargo/bin:$PATH" '
        "BUILDLOG_CALLER=verify-mac "
        f"BUILDLOG_PINNED_TARGET={shlex.quote(os.environ.get('BUILDLOG_PINNED_TARGET', ''))} "
        f"{repo_identity}"
        f"BUILDLOG_CALL_ID={shlex.quote(request.call_id)} "
        f"{sent_tree}"
        "BUILDLOG_SYNC=1 "
        "LINT_SKIP_SWEEP=1 "
        f"{forced_config}"
        f"LINT_SWEEP_BUDGET_GIB={shlex.quote(budget)} "
        f"MACOSX_DEPLOYMENT_TARGET={MACOSX_DEPLOYMENT_TARGET} "
        f"~/.claude/scripts/lint/lint {invocation}; echo mac_exit=$?; {cleanup_command}"
    )


def plain_fetch_folder(value: str) -> bool:
    folder = value.removesuffix("/")
    return (
        re.fullmatch(r"[A-Za-z0-9_./-]+", folder) is not None
        and not folder.startswith(("/", "-"))
        and all(part not in ("", ".", "..") for part in folder.split("/"))
    )


def fetch_word(result: FetchResult) -> str:
    if isinstance(result, Fetched):
        return "fetched"
    if isinstance(result, FetchMissing):
        return "missing"
    if isinstance(result, FetchTooLarge):
        return "too_large"
    if isinstance(result, FetchFailed):
        return "failed"
    return "not_run"


def report_fetch(folder: FetchFolder, result: FetchResult) -> None:
    if isinstance(result, Fetched):
        print(f"mac_test: fetched {folder.value} from the Mac to {result.path} ({result.kib} KiB)")
        return
    if isinstance(result, FetchMissing):
        reason = f"{folder.value} is missing on the Mac"
    elif isinstance(result, FetchTooLarge):
        reason = f"{folder.value} is {result.kib} KiB, over the {result.limit_mib:g} MiB limit"
    elif isinstance(result, FetchFailed):
        reason = result.reason
    else:
        reason = "the Mac run did not report a test status"
    print(f"mac_test: nothing fetched: {reason}")


def fetch_folder(
    controller: ProcessController,
    config: OffloadConfig,
    request: OffloadRequest,
    repository: KnownRepository,
) -> FetchResult:
    """Fetch under the run's claim, publishing only a complete per-call folder."""
    folder = request.fetch
    if not isinstance(folder, FetchFolder):
        return FetchNotRun()
    # The call ID becomes a local directory component, never a path supplied to SSH.
    if re.fullmatch(r"[A-Za-z0-9_.-]+", request.call_id) is None or request.call_id in (".", ".."):
        return FetchFailed("the call ID is not a directory name")
    fetched = state_directory(request.target) / "fetched"
    destination = fetched / request.call_id
    partial = fetched / f"{request.call_id}.partial"
    owns_partial = False
    try:
        fetched.mkdir(parents=True, exist_ok=True)
        oldest = time.time() - config.fetch_retention_days * 24 * 60 * 60
        for entry in fetched.iterdir():
            if entry.lstat().st_mtime < oldest:
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
        if destination.exists() or destination.is_symlink() or partial.exists() or partial.is_symlink():
            return FetchFailed("the fetched destination already exists")
        quoted = shlex.quote(folder.value)
        size_command = (
            f"cd {shlex.quote(remote_mirror(repository, request.target, request.slot))} && "
            + f"if [ -d {quoted} ]; then "
            + f"if size=$(du -sk {quoted}); then "
            + "printf 'fetch_kib=%s\\nrc=0\\n' \"${size%%[[:space:]]*}\"; else exit 1; fi; "
            + "else printf 'fetch=missing\\nrc=0\\n'; fi"
        )
        size = controller.capture([*ssh_prefix(config), size_command], CommandTimeLimit(config.probe_timeout_s))
        if not isinstance(size, CommandOutput) or size.status != 0:
            return FetchFailed("the Mac folder size could not be read")
        if size.stdout.strip() == "fetch=missing\nrc=0":
            return FetchMissing()
        measured = re.fullmatch(r"fetch_kib=([0-9]+)\nrc=0\n?", size.stdout)
        if measured is None:
            return FetchFailed("the Mac folder size was not usable")
        kib = int(measured.group(1))
        if kib > config.fetch_limit_mib * 1024:
            return FetchTooLarge(kib, config.fetch_limit_mib)
        partial.mkdir()
        owns_partial = True
        landing = partial / folder.value
        landing.mkdir(parents=True)
        copied = controller.capture([
            "rsync", "-rlp", "--compress", "--safe-links", "-e", shlex.join(["ssh", *ssh_options(config.connection)]),
            f"{config.host}:{remote_mirror(repository, request.target, request.slot)}/{folder.value}/", f"{landing}/",
        ])
        if not isinstance(copied, CommandOutput) or copied.status != 0:
            return FetchFailed("the folder copy from the Mac failed")
        if destination.exists() or destination.is_symlink():
            return FetchFailed("the fetched destination already exists")
        os.replace(partial, destination)
        return Fetched(destination / folder.value, kib)
    except OSError as error:
        return FetchFailed(str(error))
    finally:
        if owns_partial and partial.exists():
            shutil.rmtree(partial)


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

    A repository's scope stops at its own mirror. Brackets around the first character prevent the pattern from matching its assignment.
    """
    if not target_config.uses_legacy_mac_layout(configured_ssh_target(target or "mac").platform):
        if not isinstance(scope, KnownRepository):
            raise ValueError("Linux cleanup requires a repository and slot")
        from scripts.mac_test import linux_runner
        return linux_runner.mirror_selector_script(remote_mirror(scope, target, slot),
                                                   require_boot_identity=False)
    root = mac_mirror_root().relative
    mirror = f"[{root[0]}]" + root[1:].replace(".", "[.]")
    if isinstance(scope, KnownRepository):
        mirror += "/" + extended_regex_literal(scope.legacy_mirror_name)
        command_pattern = f"{mirror}([/'\"[:space:]]|$)"
        cwd_pattern = f"^n.*/{mirror}(/|$)"
    else:
        command_pattern = f"{mirror}/"
        cwd_pattern = f"^n.*/{mirror}(/|$)"
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


@dataclass(frozen=True)
class LeftoverObservation:
    result: LeftoverCheckResult
    output: str


def read_leftovers(config: SshConnectionSettings, host: str, script: str,
                   target: str) -> LeftoverObservation:
    """End and count one mirror's work without exposing the check to a second signal."""
    try:
        arguments = ["ssh", *ssh_options(config), host, script]
        result = subprocess.run(
            arguments,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=config.leftover_timeout_s,
        )
    except subprocess.TimeoutExpired:
        return LeftoverObservation(LeftoverCheckFailed(f"SSH leftover check timed out after {config.leftover_timeout_s:g} s"), "")
    except OSError:
        return LeftoverObservation(LeftoverCheckFailed(f"SSH leftover check could not start (time limit {config.leftover_timeout_s:g} s)"), "")
    ledger = buildlog_sync.active_transfers.get()
    if isinstance(ledger, buildlog_sync.TransferLedger):
        ledger.observe("ssh_cleanup", arguments, result.stdout, result.stderr)
    if result.returncode != 0:
        if not target_config.uses_legacy_mac_layout(configured_ssh_target(target or "mac").platform):
            from scripts.mac_test import linux_runner
            if result.returncode == linux_runner.SLOT_PROCESSES_UNRESOLVED:
                return LeftoverObservation(LeftoversRemain(UnresolvedLeftoverCount()), result.stdout)
        return LeftoverObservation(LeftoverCheckFailed(" ".join(result.stderr.split()) or f"SSH cleanup exited with status {result.returncode}"), result.stdout)
    answer = parse_leftover_check(result.stdout)
    if isinstance(answer, LeftoverCheckFailed):
        reason = " ".join(result.stderr.split())
        if reason:
            return LeftoverObservation(LeftoverCheckFailed(reason), result.stdout)
    return LeftoverObservation(answer, result.stdout)


def check_leftovers(config: SshConnectionSettings, host: str, repository: KnownRepository,
                   target: str = "mac", slot: int = 1) -> LeftoverCheckResult:
    """End and count one mirror's work on its recorded host."""
    return read_leftovers(config, host, leftover_check_script(repository, target, slot), target).result


def settle_pending_start(controller: ProcessController, config: OffloadConfig, host: str,
                         repository: KnownRepository, target: str = "mac", slot: int = 1
                         ) -> StartSettled | StartUnsettled:
    """Keep the claim until the remote fence prevents a delayed start."""
    identity = controller.claim_identity
    if not isinstance(identity, claim_state.ClaimIdentity):
        return StartUnsettled("the exact start identity is unavailable", LeftoverCheckFailed("the exact start identity is unavailable"))
    try:
        connection = read_connection_settings(config.source)
    except (OSError, ValueError) as error:
        return StartUnsettled(str(error), LeftoverCheckFailed(f"cleanup settings unavailable: {error}"))
    command = (start_fence_command(identity, target, settle=True, scope_key=controller.claim_scope_key) + " || exit 1\n"
               + leftover_check_script(repository, target, slot))
    observed = read_leftovers(connection, host, command, target)
    if (isinstance(observed.result, LeftoversEnded)
            and "dispatch_start_settled=" + pending_start_key(identity) in observed.output.splitlines()):
        controller.pending = SettledDispatchOperations()
        pending_start_path(target, identity).unlink(missing_ok=True)
        return StartSettled(observed.result)
    return StartUnsettled("the original remote start has not settled", observed.result)


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


def check_current_leftovers(
    config: OffloadConfig, host: str, repository: KnownRepository, target: str = "mac", slot: int = 1,
) -> LeftoverCheckResult:
    """Read the current connection settings before checking retained work."""
    try:
        connection = read_connection_settings(config.source)
    except (OSError, ValueError) as error:
        return LeftoverCheckFailed(f"cleanup settings unavailable: {error}")
    return check_leftovers(connection, host, repository, target, slot)


def check_taken_over_claim(
    controller: ProcessController, config: OffloadConfig, claim: TakenOverMacClaim | TakeoverHostUnreadable, repository: KnownRepository,
    target: str = "mac",
) -> LeftoverCheckResult:
    if isinstance(claim, TakenOverMacClaim):
        if isinstance(controller.pending, PendingStartOperation):
            return settle_pending_start(controller, config, claim.host, repository, target, claim.slot).leftovers
        return check_current_leftovers(config, claim.host, repository, target, claim.slot)
    return LeftoverCheckFailed("the kept claim's host could not be read")


def claim_for_run(
    controller: ProcessController, config: OffloadConfig, request: OffloadRequest, repository: KnownRepository,
    leased_slots: tuple[int, ...] = (),
) -> MacClaimAnswer:
    attempt = LocalClaimAttempt()
    controller.placement = request.placement
    controller.claim_settings = config.source
    controller.claim_target = request.target
    if isinstance(request.admission, MacClaimFromWait):
        controller.expected_claim = request.admission.identity
    elif isinstance(request.placement, AssignedOffloadRequest):
        assigned = request.placement.assignment
        controller.expected_claim = claim_state.ClaimIdentity(uuid.uuid5(uuid.NAMESPACE_URL,
            placement.attempt_key(assigned.episode, assigned.attempt)).hex, assigned.episode, assigned.attempt)
    if isinstance(request.placement, AssignedOffloadRequest) and isinstance(controller.expected_claim, claim_state.ClaimIdentity):
        assigned = request.placement.assignment
        identity = controller.expected_claim
        controller.attempt_evidence.provider = placement.ProviderClaimReference(
            assigned.destination.provider, assigned.destination.target, assigned.destination.scope,
            identity.claim_id, identity.episode, identity.attempt)
    try:
        controller.complete_claim(claim_arguments(request, config, leased_slots), attempt)
        if isinstance(attempt.answer, (FreshMacClaim, TakenOverMacClaim)):
            claim_path = cast(Callable[[claim_state.StatePaths, claim_state.ClaimScope], Path],
                              getattr(import_module("scripts.mac_test.mac_test"), "claim_path"))

            paths = claim_state.state_paths(request.target)
            scope = (request.placement.assignment.destination.scope
                     if isinstance(request.placement, AssignedOffloadRequest)
                     else claim_state.target_claim_scope(paths, str(request.repo_root)))
            if isinstance(scope, claim_state.RepositoryScope) and not target_config.uses_legacy_mac_layout(paths.platform):
                scope = replace(scope, slot=attempt.answer.slot)
            controller.claim_scope_key = claim_path(paths, scope).stem
        if isinstance(request.placement, AssignedOffloadRequest) and isinstance(controller.claim_identity, claim_state.ClaimIdentity):
            assigned = request.placement.assignment
            identity = controller.claim_identity
            controller.attempt_evidence.provider = placement.ProviderClaimReference(
                assigned.destination.provider, assigned.destination.target, assigned.destination.scope,
                identity.claim_id, identity.episode, identity.attempt)
            if pending_start_path(request.target, identity).exists():
                controller.pending = PendingStartOperation()
        if isinstance(attempt.interruption, OffloadInterrupted):
            raise attempt.interruption
        return attempt.answer
    except OffloadInterrupted:
        claim = attempt.answer
        if isinstance(claim, FreshMacClaim):
            _ = release_claim(controller, request.target)
        elif isinstance(claim, (TakenOverMacClaim, TakeoverHostUnreadable)):
            checked = check_taken_over_claim(controller, config, claim, repository, request.target)
            report_leftover_check(checked, request)
            if isinstance(checked, LeftoversEnded):
                _ = release_claim(controller, request.target)
        raise


def observe_cleanup(evidence: AttemptEvidence, checked: LeftoverCheckResult) -> None:
    evidence.evidence_at = time.time()
    evidence.cleanup = "confirmed" if isinstance(checked, LeftoversEnded) else "unconfirmed"
    evidence.unresolved_count = (0 if isinstance(checked, LeftoversEnded) else checked.count
                                 if isinstance(checked, LeftoversRemain) else UnresolvedLeftoverCount())


def claimed_run(
    controller: ProcessController,
    config: OffloadConfig,
    request: OffloadRequest,
    repository: KnownRepository,
    started: float,
) -> MacOffloadResult:
    from scripts.copies import copies

    release_allowed = True
    sync_after_release = False
    evidence = controller.attempt_evidence
    evidence.slot = request.slot
    try:
        linux = not target_config.uses_legacy_mac_layout(configured_ssh_target(request.target or "mac").platform)
        if linux:
            expected = expected_linux_parity(controller, config)
            if isinstance(expected, UnavailableToolParity):
                return declined("state", started)
            config = replace(config, values={**config.values, **{f"parity.{key}": value for key, value in expected.items()}})
            if not copy_tracked_scripts(controller, config):
                return DeclinedMacRun("copy", elapsed_seconds(started), controller.copy_failure_detail)
            copy_started = time.monotonic()
            copied = copy_tree_with_key(controller, config, request, repository)
            if isinstance(copied, TreeCopyFailed):
                return DeclinedMacRun("copy", elapsed_seconds(started), copied.detail)
            copy_seconds = elapsed_seconds(copy_started)
            release_allowed = False
            probe = probe_mac(controller, config, repository, request.target, request.slot)
            if isinstance(probe, UnresolvedSlotStartup):
                checked = check_current_leftovers(config, config.host, repository, request.target, request.slot)
                release_allowed = isinstance(checked, LeftoversEnded)
                observe_cleanup(evidence, checked)
                report_leftover_check(checked, request)
                return BusyMacDecline(LostRunLeftovers(), elapsed_seconds(started))
            if isinstance(probe, UnavailableProbe):
                checked = check_current_leftovers(config, config.host, repository, request.target, request.slot)
                release_allowed = isinstance(checked, LeftoversEnded)
                observe_cleanup(evidence, checked)
                report_leftover_check(checked, request)
                if isinstance(checked, LeftoversRemain):
                    return BusyMacDecline(LostRunLeftovers(), elapsed_seconds(started))
                if probe.detail:
                    return DeclinedMacRun("state", elapsed_seconds(started), probe.detail)
                write_backoff(time.time() + config.unreachable_backoff_s, request.target)
                return declined("unreachable", started)
            if probe.free_gib < config.free_floor_gib:
                checked = check_current_leftovers(config, config.host, repository, request.target, request.slot)
                release_allowed = isinstance(checked, LeftoversEnded)
                observe_cleanup(evidence, checked)
                report_leftover_check(checked, request)
                return declined("disk", started)
        else:
            probe = probe_mac(controller, config, repository, request.target, request.slot)
            if isinstance(probe, (UnavailableProbe, UnresolvedSlotStartup)):
                write_backoff(time.time() + config.unreachable_backoff_s, request.target)
                return declined("unreachable", started)
            disk = ensure_mac_disk_floor(controller, config, repository, probe, request.target, request.slot)
            if isinstance(disk, SweepFailed):
                return DeclinedMacRun("disk", elapsed_seconds(started), disk.cause)
            if isinstance(disk, SweepShort):
                return declined("disk", started)
            copy_started = time.monotonic()
            copied = copy_tree_with_key(controller, config, request, repository)
            if isinstance(copied, TreeCopyFailed):
                return DeclinedMacRun("copy", elapsed_seconds(started), copied.detail)
            copy_seconds = elapsed_seconds(copy_started)
        mirror_busy_path(repository.name, request.target).unlink(missing_ok=True)
        running = (
            f"running a copy on the Mac while natedev compiles (host {probe.hostname})"
            if request.copy
            else f"running on the Mac (macOS, host {probe.hostname})"
        )
        if linux:
            running = f"running on {request.target} (Linux, host {probe.hostname})"
        print(f"mac_test: {running}; tree copied in {copy_seconds:.1f} s", flush=True)
        copies.register_remote(request_mirror(config, request, repository))
        if isinstance(request.placement, AssignedOffloadRequest):
            from scripts.build_queue import admission_client, admission_protocol as wire

            assignment = request.placement.assignment
            source = assignment.source_key
            if isinstance(source, placement.SourceKeyKnown) and (
                    not isinstance(copied.tree_key, KnownTreeKey) or copied.tree_key.value != source.key):
                release_allowed = True
                return DeclinedMacRun("source_changed", elapsed_seconds(started), "the copied source differs from the assignment")
            identity = controller.claim_identity
            start = process_identity.process_start(os.getpid())
            if not isinstance(identity, claim_state.ClaimIdentity) or not isinstance(start, process_identity.Started):
                return declined("state", started)
            reference = placement.ProviderClaimReference(assignment.destination.provider,
                assignment.destination.target, assignment.destination.scope,
                identity.claim_id, identity.episode, identity.attempt)
            holder = placement.ClaimHolderIdentity(store.host_name(), os.getpid(), start.start)
            while True:
                try:
                    permission = admission_client.consume_permission(wire.ConsumePermission(
                        uuid.uuid4().hex, assignment, reference, holder))
                except wire.AdmissionUnavailable:
                    evidence.execution = placement.ExecutionEvidenceUnavailable("authorization response unavailable", time.time())
                    return declined("state", started)
                if isinstance(permission, wire.FirstAuthorization):
                    break
                if (not isinstance(permission.state, placement.CurrentAssignment)
                        or not isinstance(permission.permission, placement.PermissionAvailable)):
                    return declined("state", started)
                from scripts.build_queue import admission_records as records

                time.sleep(records.read_memory_config().waiting_pass_seconds)
            evidence.launch_authorized = True
        release_allowed = False
        identity = controller.claim_identity
        command = copies.remote_use_command(request_mirror(config, request, repository),
                                           remote_test_command(config, request, repository, copied.tree_key))
        if isinstance(identity, claim_state.ClaimIdentity):
            command = fenced_start_command(identity, request.target, command, scope_key=controller.claim_scope_key)
        remote_status = controller.stream_mac_run(
            [
                *ssh_prefix(config),
                command,
            ]
        )
        if isinstance(remote_status, MirrorUseBusy):
            release_allowed = True
            write_mirror_busy(config, repository.name, request.target)
            return BusyMacDecline(LostRunLeftovers(), elapsed_seconds(started))
        if not isinstance(remote_status, ReportedMacStatus):
            if isinstance(controller.pending, PendingStartOperation):
                settlement = settle_pending_start(controller, config, config.host, repository, request.target, request.slot)
                if isinstance(settlement, StartUnsettled):
                    observe_cleanup(evidence, settlement.leftovers)
                    report_leftover_check(settlement.leftovers, request)
                    return LostMacRun(elapsed_seconds(started), "remote startup has no acknowledgement")
                checked = settlement.leftovers
            elif not isinstance(controller.pending, SettledDispatchOperations):
                return LostMacRun(elapsed_seconds(started), "remote startup has no acknowledgement")
            else:
                checked = check_current_leftovers(config, config.host, repository, request.target, request.slot)
            release_allowed = isinstance(checked, LeftoversEnded)
            observe_cleanup(evidence, checked)
            report_leftover_check(checked, request)
            if isinstance(remote_status, ToolVersionMismatch):
                write_version_decline(request.target, repository.name)
                return DeclinedMacRun("state", elapsed_seconds(started), remote_status.reason)
            if linux and isinstance(remote_status, UnresolvedSlotStartup):
                return BusyMacDecline(LostRunLeftovers(), elapsed_seconds(started))
            detail = (remote_status.detail if isinstance(remote_status, MissingMacStatus)
                      else "runner sent no final status")
            return LostMacRun(elapsed_seconds(started), detail)
        if not linux:
            linux_version_decline_path(request.target, repository.name).unlink(missing_ok=True)
        fetched = fetch_folder(controller, config, request, repository) if isinstance(request.fetch, FetchFolder) else FetchNotRun()
        if not linux and isinstance(request.fetch, FetchFolder):
            cleanup = controller.stream_mac_run([
                *ssh_prefix(config),
                copies.remote_use_command(request_mirror(config, request, repository),
                    f"echo mac_exit={remote_status.status}; " + remote_cleanup_command(config, request, repository)),
            ])
            remote_status = replace(remote_status, cleanup=(cleanup.cleanup if isinstance(cleanup, ReportedMacStatus)
                                                          else CleanupUnconfirmed()))
        evidence.completion = "confirmed"
        report_cleanup(remote_status.cleanup, request.target,
                       path=f"~/{mirror_target(repository, request.target, request.slot)}",
                       free_floor_gib=config.free_floor_gib)
        if isinstance(remote_status.cleanup, CleanupUnconfirmed):
            checked = check_current_leftovers(config, config.host, repository, request.target, request.slot)
            release_allowed = isinstance(checked, LeftoversEnded)
            observe_cleanup(evidence, checked)
            report_leftover_check(checked, request)
        else:
            release_allowed = True
        sync_after_release = linux
        seconds = elapsed_seconds(started)
        if linux and remote_status.status == 75:
            return declined("state", started)
        if remote_status.status == 0:
            return PassedOnMac(seconds, fetched, remote_status.build_target_directory)
        if remote_status.status == 4 and request.verb == "nextest":
            return NoTestMatchedOnMac(seconds, fetched)
        return FailedOnMac(remote_status.status, seconds, fetched)
    except OffloadInterrupted:
        controller.stop()
        if not release_allowed and isinstance(controller.pending, SettledDispatchOperations):
            checked = check_current_leftovers(config, config.host, repository, request.target, request.slot)
            release_allowed = isinstance(checked, LeftoversEnded)
            observe_cleanup(evidence, checked)
            report_leftover_check(checked, request)
        raise
    finally:
        evidence.cleanup = "confirmed" if release_allowed else "unconfirmed"
        evidence.evidence_at = time.time()
        if release_allowed:
            evidence.unresolved_count = 0
        if release_allowed and isinstance(controller.pending, SettledDispatchOperations):
            _ = release_claim(controller, request.target)
            if sync_after_release:
                with buildlog_sync.measure_transfers(evidence.traffic):
                    buildlog_sync.sync_target(request.target)


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


def linux_kind_permitted(values: dict[str, str], kind: str) -> bool:
    return all(step in ('test', 'check', 'doc', 'lint', 'example', 'build')
               and step in comma_values(values.get('kinds', '')) for step in remote_step_kinds(kind))


def remote_step_kinds(kind: str) -> tuple[str, ...]:
    """Return the kinds that a command must run on its pinned target."""
    return ('check', 'test') if kind == 'final' else (kind,)


def verb_kind(verb: MacVerb) -> str:
    return "test" if verb == "nextest" else "lint" if verb in ("clippy", "mend-check") else verb


def mac_step_eligibility(config: OffloadConfig, repo_root: Path, package: str,
                         verb: MacVerb, target: str = "", *, kind: str = "") -> KnownRepository | IneligibleMacStep:
    """Share repository, package/workspace and backoff rules with eligible --kind."""
    if not config.enabled:
        return IneligibleMacStep("off")
    repository = repository_identity(repo_root)
    if isinstance(repository, UnknownRepository):
        return IneligibleMacStep("repo")
    if target and not target_config.uses_legacy_mac_layout(configured_ssh_target(target).platform):
        if not linux_kind_permitted(config.values, kind or verb_kind(verb)):
            return IneligibleMacStep("target does not permit this command")
        if isinstance(read_backoff(time.time(), target), ActiveBackoff):
            return IneligibleMacStep("backoff")
        return repository
    key = f"linux_only.{repository.name}"
    if key not in config.values:
        return IneligibleMacStep("repo")
    excluded = comma_values(config.values[key])
    if verb == "nextest" and package:
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
    from scripts.copies import copies

    eligibility = mac_step_eligibility(config, request.repo_root, request.package, request.verb, request.target, kind=request.kind)
    if isinstance(eligibility, IneligibleMacStep):
        return declined(eligibility.reason, started)
    repository = eligibility
    linux = not target_config.uses_legacy_mac_layout(configured_ssh_target(request.target or "mac").platform)
    if linux and not trusted_linux_source(request.repo_root):
        return declined("repo", started)
    if linux:
        detail = recorded_linux_version_mismatch(controller, config, request.target)
        decline_path = linux_version_decline_path(request.target, repository.name)
        if detail:
            write_version_decline(request.target, repository.name)
            return DeclinedMacRun("state", elapsed_seconds(started), detail)
        decline_path.unlink(missing_ok=True)
    if not linux and isinstance(request.source, StagedDocSnapshot):
        try:
            if workspace_has_linux_only(request.source.root, comma_values(config.values[f"linux_only.{repository.name}"])):
                return declined("linux_only", started)
        except (OSError, UnicodeError, ValueError):
            return declined("repo", started)

    try:
        with ExitStack() as admission_leases:
            slots = (target_config.slot_capacity(config.values, repository.name,
                     missing=target_config.MissingSlotCapacity.SINGLE_SLOT) if linux else 1)
            leases: dict[int, ExitStack] = {}
            selected_slots = ((request.placement.assignment.destination.slot or 1,)
                              if isinstance(request.placement, AssignedOffloadRequest) else range(1, slots + 1))
            for slot in selected_slots:
                lease = ExitStack()
                try:
                    lease.enter_context(copies.remote_lease(request_mirror(config, replace(request, slot=slot), repository)))
                except copies.BusyRemoteMirror:
                    continue
                leases[slot] = admission_leases.enter_context(lease)
            result = leased_offload(controller, config, request, repository, started, linux, leases)
            if isinstance(result, DeclinedMacRun) and result.reason == "copy" and mirror_busy_refusal(result.detail):
                write_mirror_busy(config, repository.name, request.target)
            return result
    except copies.CopyError as error:
        return DeclinedMacRun("copy", elapsed_seconds(started), str(error))


def request_mirror(config: OffloadConfig, request: OffloadRequest, repository: KnownRepository) -> copies.RemoteMirror:
    from scripts.copies import copies

    scope = request.repository_scope
    if isinstance(scope, copies.AllRepositories):
        scope = copies.SelectedRepository(repository.name)
    return copies.RemoteMirror(request.owner, scope, config.host,
                               remote_mirror(repository, request.target, request.slot),
                               request.holder_session)


def leased_offload(controller: ProcessController, config: OffloadConfig, request: OffloadRequest,
                   repository: KnownRepository, started: float, linux: bool,
                   leases: dict[int, ExitStack]) -> MacOffloadResult:
    if not leases:
        write_mirror_busy(config, repository.name, request.target)
        return BusyMacDecline(LostRunLeftovers(), elapsed_seconds(started))
    claim = claim_for_run(controller, config, request, repository, tuple(leases))
    # A transferred reservation can be adopted only once.
    request = replace(request, admission=UnclaimedMacRun())
    if isinstance(claim, (TakenOverMacClaim, TakeoverHostUnreadable)):
        controller.attempt_evidence.slot = claim.slot if isinstance(claim, TakenOverMacClaim) else 0
        checked = check_taken_over_claim(controller, config, claim, repository, request.target)
        observe_cleanup(controller.attempt_evidence, checked)
        report_leftover_check(checked, request)
        if isinstance(checked, LeftoverCheckFailed):
            write_backoff(time.time() + config.unreachable_backoff_s, request.target)
            return declined("unreachable", started)
        if isinstance(checked, LeftoversRemain):
            return BusyMacDecline(LostRunLeftovers(), elapsed_seconds(started))
        if isinstance(controller.pending, PendingStartOperation):
            controller.attempt_evidence.cleanup = "unconfirmed"
            return BusyMacDecline(LostRunLeftovers(), elapsed_seconds(started))
        _ = release_claim(controller, request.target)
        if isinstance(request.placement, AssignedOffloadRequest):
            return BusyMacDecline(LostRunLeftovers(), elapsed_seconds(started))
        claim = claim_for_run(controller, config, request, repository, tuple(leases))
        if isinstance(claim, (TakenOverMacClaim, TakeoverHostUnreadable)):
            return BusyMacDecline(LostRunLeftovers(), elapsed_seconds(started))
    if isinstance(claim, MacClaimNotConfirmed):
        if claim.reason in ("busy", "blocked"):
            controller.attempt_evidence.provider = placement.NoProviderClaim()
        if not decline_already_shown(claim.reason, request.last_decline):
            if claim.stdout:
                print(claim.stdout, end="" if claim.stdout.endswith("\n") else "\n", file=sys.stderr, flush=True)
            if claim.stderr:
                print(claim.stderr, end="" if claim.stderr.endswith("\n") else "\n", file=sys.stderr, flush=True)
        if claim.reason == "busy":
            return BusyMacDecline(claim_holder(claim.stdout), elapsed_seconds(started))
        return declined(claim.reason, started)
    if claim.slot not in leases:
        _ = release_claim(controller, request.target)
        return BusyMacDecline(LostRunLeftovers(), elapsed_seconds(started))
    for slot, lease in leases.items():
        if slot != claim.slot:
            lease.close()
    selected = replace(request, slot=claim.slot)
    result = claimed_run(controller, config, selected, repository, started)
    if linux and isinstance(result, PassedOnMac):
        return PassedOnTarget(result.seconds, result.build_target_directory)
    if linux and isinstance(result, FailedOnMac):
        return FailedOnTarget(result.status, result.seconds)
    return result


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
    _ = run.add_argument("--verb", choices=("nextest", "check", "clippy", "mend-check", "doc", "build"), default="nextest")
    _ = run.add_argument("--kind", default="")
    _ = run.add_argument("--call-id", required=True)
    _ = run.add_argument("--settings", default="")
    _ = run.add_argument("--target", default="")
    _ = run.add_argument("--assignment-file", default="")
    _ = run.add_argument("--last-decline", choices=("busy", "blocked"), default="")
    _ = run.add_argument("--filter-run", action="store_true")
    _ = run.add_argument("--copy", action="store_true")
    _ = run.add_argument("--claim-owner-pid", type=int, default=0)
    for field in ("claim-id", "episode", "attempt", "claim-session"):
        _ = run.add_argument("--" + field, default="")
    _ = run.add_argument("--result")
    _ = run.add_argument("--env", dest="environment", action="append", default=[])
    _ = run.add_argument("--fetch")
    _ = run.add_argument("nextest_words", nargs=argparse.REMAINDER)
    return root


def holder_session() -> str:
    """Use the unit owner session before the shell session."""
    from scripts.build_hold import build_hold

    owner = build_hold.owning_session()
    if isinstance(owner, OwningSession):
        return owner.session_id
    return os.environ.get("CODEX_THREAD_ID") or f"pid-{os.getpid()}"


def request_from_arguments(arguments: CliArguments) -> OffloadRequest:
    from scripts.copies import copies

    words = arguments.nextest_words
    if words[:1] == ["--"]:
        words = words[1:]
    last_decline: LastMacDecline = NO_PRIOR_MAC_DECLINE
    if arguments.last_decline == "busy":
        last_decline = PriorMacDecline("busy")
    elif arguments.last_decline == "blocked":
        last_decline = PriorMacDecline("blocked")
    assigned: DirectOffloadRequest | AssignedOffloadRequest = DirectOffloadRequest()
    if arguments.assignment_file:
        from scripts.build_queue import admission_client, admission_protocol as wire

        saved = admission_client.read_assignment(Path(arguments.assignment_file))
        if not isinstance(saved, wire.Assignment):
            raise ValueError("offload requires a remote assignment")
        assigned = AssignedOffloadRequest(saved.assignment)
        if saved.assignment.destination.target != "mac" or arguments.target:
            arguments.target = saved.assignment.destination.target
    return OffloadRequest(
        placement=assigned,
        repo_root=Path(arguments.repo_root).resolve(),
        source=StagedDocSnapshot(Path(arguments.source_root).resolve()) if arguments.source_root else RepositoryWorkingTree(),
        owner=copies.source_owner(Path(arguments.repo_root)),
        holder_session=holder_session(),
        package=arguments.package,
        call_id=arguments.call_id,
        result=result_destination(arguments.result),
        nextest_words=tuple(words),
        last_decline=last_decline,
        copy=arguments.copy,
        verb=arguments.verb,
        kind=arguments.kind,
        target=arguments.target,
        environment=tuple((name, os.environ[name]) for name in arguments.environment),
        fetch=FetchFolder(arguments.fetch.removesuffix("/")) if arguments.fetch is not None else NO_FETCH,
        admission=MacClaimFromWait(
            arguments.claim_owner_pid,
            claim_state.ClaimIdentity(arguments.claim_id, arguments.episode, arguments.attempt),
            OwningSession(arguments.claim_session) if arguments.claim_session else NoOwningSession(),
        ) if arguments.claim_owner_pid else UnclaimedMacRun(),
    )


def restore_ignored_interrupt_default(arguments: CliArguments) -> None:
    if (
        arguments.command == "run"
        and signal.getsignal(signal.SIGINT) == signal.SIG_IGN
    ):
        _ = signal.signal(signal.SIGINT, signal.SIG_DFL)


def persist_attempt(request: OffloadRequest, result: MacOffloadResult,
                    evidence: AttemptEvidence, started: float) -> None:
    """Append each attempt independently of the final verify call target."""
    from scripts.buildlog import record

    class MeasuredSshAttempt(record.SshAttempt):
        transfers: list[buildlog_sync.TransferBytes]
        sent_bytes: int
        received_bytes: int

    if not request.target:
        return
    repository = repository_identity(request.repo_root)
    queued = os.environ.get("BUILDLOG_MEM_WAIT_SINCE", "")
    first = os.environ.get("BUILDLOG_MEM_WAIT_FIRST_FILE", "")
    if first and Path(first).is_file():
        queued = Path(first).read_text().strip()
    outcome = ("passed" if isinstance(result, (PassedOnMac, PassedOnTarget)) else "failed"
               if isinstance(result, (FailedOnMac, FailedOnTarget)) else "lost"
               if isinstance(result, LostMacRun) else "no_tests"
               if isinstance(result, NoTestMatchedOnMac) else "declined")
    attempt: MeasuredSshAttempt = {
        "call_id": request.call_id, "target": request.target,
        "repo_identity": (repository.key if isinstance(repository, KnownRepository) and repository.key
                          else store.UNKNOWN_IDENTITY),
        "queued_at": float(queued) if queued else time.time() - elapsed_seconds(started),
        "departed_at": time.time() - elapsed_seconds(started), "slot": evidence.slot,
        "outcome": outcome, "completion": evidence.completion, "cleanup": evidence.cleanup,
        "unresolved_count": evidence.unresolved_count if isinstance(evidence.unresolved_count, int) else None,
        "evidence_at": evidence.evidence_at or time.time(),
        "transfers": evidence.traffic.transfers,
        "sent_bytes": sum(transfer["sent_bytes"] for transfer in evidence.traffic.transfers),
        "received_bytes": sum(transfer["received_bytes"] for transfer in evidence.traffic.transfers),
    }
    if isinstance(result, LostMacRun):
        attempt["reason"] = result.detail
    if isinstance(result, (DeclinedMacRun, BusyMacDecline)):
        attempt["reason"] = result.detail if isinstance(result, DeclinedMacRun) and result.detail else result.reason
    record.ssh_attempt(attempt)


class DirectPlacementConstraintBuilder(Protocol):
    """The fixed source policy used by a direct pinned offload request."""

    def __call__(self, repo_root: Path, package: str,
                 kind: Literal["check", "lint", "doc", "test", "build", "example", "final"],
                 command: tuple[str, ...], session: claim_state.SubmittingSession, source_key: placement.SourceIdentity,
                 *, selection: Pinned, provider_settings: placement.ProviderSettings) -> placement.PlacementConstraints: ...


def submit_direct_placement(request: OffloadRequest, since: float, config: OffloadConfig) -> OffloadRequest:
    """A direct caller keeps its fixed destination through queue placement."""
    from scripts.build_queue import admission_client, admission_protocol as wire, admission_records as records
    start = process_identity.process_start(os.getpid())
    if not isinstance(start, process_identity.Started):
        raise wire.AdmissionUnavailable("the submitting process is unavailable")
    measured_key = treekey.tree_key(str(request.source_root))
    source_key = (placement.SourceKeyKnown(measured_key) if measured_key is not None
                  else placement.SourceKeyUnavailable("the source tree key is unavailable"))
    session = claim_state.submitting_session(claim_state.CALLING_SESSION)
    command = ("cargo", request.verb, *(("run",) if request.verb == "nextest" else ()), *request.nextest_words)
    episode = uuid.uuid4().hex
    local = admission_client.step_request(list(command), str(request.repo_root),
        wire.Owner(os.getpid(), start.start), since, "", episode)
    builder = cast(DirectPlacementConstraintBuilder, getattr(
        import_module("scripts.build_targets.build_targets"), "fixed_placement_constraints"))
    constraints = builder(request.repo_root, request.package,
        cast(Literal["check", "lint", "doc", "test", "build", "example", "final"], request.kind or verb_kind(request.verb)),
        command, session, source_key, selection=Pinned(request.target or "mac"),
        provider_settings=placement.ExplicitProviderSettings(config.source.resolve())
            if isinstance(config.source, Path) else placement.ConfiguredProviderSettings())
    submitted = wire.PlacementRequest(episode, placement.SubmittingOwner(store.host_name(), os.getpid(), start.start),
        since, local.profile, constraints, identity=local.identity, run=local.run,
        prerequisite=placement.TokenNotRequired())
    while True:
        reply = admission_client.request_placement(submitted)
        if isinstance(reply, wire.Assignment):
            return replace(request, target=(request.target if reply.assignment.destination.target == "mac"
                                            else reply.assignment.destination.target),
                           slot=reply.assignment.destination.slot or 1,
                           placement=AssignedOffloadRequest(reply.assignment))
        if not isinstance(reply, wire.PlacementWait):
            raise wire.AdmissionUnavailable("the fixed remote assignment did not permit execution")
        time.sleep(records.read_memory_config().waiting_pass_seconds)


def report_placement_result(controller: ProcessController, request: OffloadRequest, result: MacOffloadResult) -> None:
    """Report a settled adapter path without inventing a remote process identity."""
    from scripts.build_queue import admission_client, admission_protocol as wire

    if not isinstance(request.placement, AssignedOffloadRequest):
        return
    assigned = request.placement.assignment
    evidence = controller.attempt_evidence
    provider = evidence.provider
    if (isinstance(provider, placement.ProviderClaimReference)
            and (provider.episode, provider.attempt) != (assigned.episode, assigned.attempt)):
        if isinstance(controller.pending, SettledDispatchOperations):
            reason = result.reason if isinstance(result, (DeclinedMacRun, BusyMacDecline)) else "cleanup completed"
            _ = admission_client.claim_failure(wire.ClaimFailure(uuid.uuid4().hex, assigned, reason, provider))
        return
    if isinstance(provider, placement.NoProviderClaim):
        reason = result.reason if isinstance(result, (DeclinedMacRun, BusyMacDecline)) else "claim unavailable"
        _ = admission_client.claim_failure(wire.ClaimFailure(uuid.uuid4().hex, assigned, reason))
        return
    outcome: placement.AttemptOutcome
    if isinstance(result, (PassedOnMac, PassedOnTarget)):
        outcome = placement.AttemptCompleted(0)
    elif isinstance(result, (FailedOnMac, FailedOnTarget)):
        outcome = placement.AttemptCompleted(result.status)
    elif isinstance(result, NoTestMatchedOnMac):
        outcome = placement.AttemptDeclined("no_tests")
    elif (isinstance(result, LostMacRun) and evidence.cleanup == "confirmed"
          and isinstance(controller.pending, SettledDispatchOperations)):
        outcome = (placement.PrelaunchRefused("lost")
                   if not evidence.launch_authorized and isinstance(evidence.execution, placement.ExecutionNotStarted)
                   else placement.AttemptDeclined("lost"))
    elif (isinstance(result, (DeclinedMacRun, BusyMacDecline))
          and isinstance(evidence.execution, placement.ExecutionNotStarted)):
        outcome = placement.PrelaunchRefused(result.reason)
    elif isinstance(result, (DeclinedMacRun, BusyMacDecline)) and evidence.completion == "confirmed":
        outcome = placement.AttemptDeclined(result.reason)
    else:
        outcome = placement.AttemptOutcomeUnavailable("the provider has unresolved execution or cleanup")
    path = (placement.DispatchPathSettled() if isinstance(controller.pending, SettledDispatchOperations)
            else placement.DispatchPathPending("a claim or start child remains active"))
    _ = admission_client.report_execution(wire.ExecutionReport(uuid.uuid4().hex, assigned, provider,
                                                               evidence.execution, outcome, path))


def main() -> int:
    override_guard.refuse_overrides(Path(__file__), "mac_test/offload.py")
    from scripts.build_queue import admission_protocol as wire

    cli = parser()
    arguments = cli.parse_args(namespace=CliArguments())
    if arguments.environment and arguments.verb != "nextest":
        cli.error("--env applies only to nextest runs")
    if arguments.fetch is not None and (arguments.verb != "nextest" or arguments.copy):
        cli.error("--fetch applies only to non-copy nextest runs")
    for name in arguments.environment:
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None:
            cli.error(f"--env {name}: not a variable name")
        if name not in os.environ:
            cli.error(f"--env {name}: not set here")
    if arguments.fetch is not None and not plain_fetch_folder(arguments.fetch):
        cli.error("--fetch: not a plain relative folder")
    if arguments.verb != "nextest" and (arguments.copy or arguments.filter_run):
        cli.error("--copy and --filter-run apply only to nextest")
    if arguments.source_root and arguments.verb != "doc":
        cli.error("--source-root applies only to a staged doc snapshot")
    restore_ignored_interrupt_default(arguments)
    request = request_from_arguments(arguments)
    settings = arguments.settings
    if arguments.target:
        target_settings = configured_ssh_target(arguments.target).settings.expanduser().resolve()
        if settings and Path(settings).expanduser().resolve() != target_settings:
            cli.error(f"--settings {Path(settings).expanduser().resolve()} differs from "
                      + f"--target {arguments.target} settings {target_settings}")
        settings = str(target_settings)
    config = read_config(Path(settings)) if settings else read_config()
    if isinstance(request.placement, AssignedOffloadRequest):
        request = replace(request, slot=request.placement.assignment.destination.slot or 1)
    controller = ProcessController()
    started = time.monotonic()
    result: MacOffloadResult = LostMacRun(0, "runner did not complete")
    direct = isinstance(request.placement, DirectOffloadRequest)
    since = time.time()
    try:
        if direct:
            request = submit_direct_placement(request, since, config)
        while True:
            with interrupt_handlers(controller), buildlog_sync.measure_transfers(controller.attempt_evidence.traffic):
                result = run_offload(controller, config, request, started)
            if not direct or not isinstance(result, DeclinedMacRun) or result.reason != "source_changed":
                break
            from scripts.build_queue import admission_client

            report_placement_result(controller, request, result)
            persist_attempt(request, result, controller.attempt_evidence, started)
            assert isinstance(request.placement, AssignedOffloadRequest)
            assigned = request.placement.assignment
            _ = admission_client.cancel_placement(wire.CancelPlacement(
                uuid.uuid4().hex, assigned.episode, assigned.owner, "source changed before dispatch"))
            request = replace(request, placement=DirectOffloadRequest(), admission=UnclaimedMacRun())
            controller = ProcessController()
            started = time.monotonic()
            result = LostMacRun(0, "replacement placement did not complete")
            request = submit_direct_placement(request, since, config)
        status = write_result(result, request.result, request.last_decline, request.verb, request.target,
                              controller.attempt_evidence, request.fetch)
        return status
    except OffloadInterrupted as interrupted:
        result = LostMacRun(elapsed_seconds(started), f"interrupted by signal {interrupted.signal_number}")
        return 128 + interrupted.signal_number
    except Exception:
        traceback.print_exc()
        if isinstance(request.result, ResultFile):
            try:
                request.result.path.unlink(missing_ok=True)
            except OSError:
                pass
        return 75
    finally:
        try:
            report_placement_result(controller, request, result)
        except (wire.AdmissionUnavailable, OSError, ValueError, ConnectionError) as error:
            print(f"offload: cannot report placement: {error}", file=sys.stderr)
        try:
            persist_attempt(request, result, controller.attempt_evidence, started)
        except (OSError, ValueError) as error:
            print(f"offload: cannot record the attempt: {error}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
