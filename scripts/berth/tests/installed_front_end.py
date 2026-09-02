#!/usr/bin/env python3
"""Installed front-end fixtures shared by the wrapper and timing suites."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import (
    ClassVar,
    cast,
    override,
)

SCRIPTS_ROOT = Path("/Users/natemccoy/.claude/scripts")
BERTH_ROOT = SCRIPTS_ROOT / "berth"
INSTALLED_DIRECTORY = Path("/Users/natemccoy/.cargo/bin")
INSTALLED_BINARY = INSTALLED_DIRECTORY / "cargo-berth"
PRE_EDIT_HOOK = BERTH_ROOT / "install/hooks/berth_pre_edit.sh"
POST_BASH_HOOK = BERTH_ROOT / "install/hooks/berth_post_bash.sh"
SESSION_START_HOOK = BERTH_ROOT / "install/hooks/berth_session_start.sh"
INSTALL_SCRIPT = BERTH_ROOT / "install/install.sh"
INSTALLED_TIMING_ARTIFACTS = (
    INSTALLED_BINARY,
    PRE_EDIT_HOOK,
    POST_BASH_HOOK,
    SESSION_START_HOOK,
)
UNREAD_MESSAGE = "MESSAGE_MUST_REMAIN_UNREAD"
POST_TOOL_USE_BOUND_SECONDS = 0.20
POST_TOOL_USE_SAMPLE_COUNT = 5
FIXED_COST_ATTRIBUTION_TOLERANCE_FRACTION = 0.10
CURRENT_PROJECTION_SCHEMA_VERSION = 3
FIRST_RUN = "01900a1b-2c3d-7e4f-8a5b-6c7d8e9f0a1b"
SECOND_RUN = "01900a1b-2c3d-7e4f-8a5b-6c7d8e9f0a1c"
SHORT_TIMING_JOURNAL_RECORD_COUNT = 12
WORKING_REPOSITORY_JOURNAL_RECORD_COUNT = 214
SHALLOW_TIMING_HISTORY_COMMIT_COUNT = 4
DEEP_TIMING_HISTORY_COMMIT_COUNT = 64
DIVERGENT_TIMING_BASE_COMMIT_COUNT = 32
DIVERGENT_TIMING_COMMITS_PER_SUBJECT = 8
TIMING_RETAINED_RESERVATION_COUNT = 2
HISTORY_GIT_PROCESS_CEILING = 6
# The hook runs drift and the live board read inside one `hook post-tool-use`
# process, so this bounds both together -- it was 3 when a canned drift result
# could be substituted for the first half, and that substitution stopped being
# possible when the two engine calls became one.
LIVE_BOARD_GIT_PROCESS_CEILING = 9
FNV_OFFSET_BASIS = 14_695_981_039_346_656_037
FNV_PRIME = 1_099_511_628_211
UINT64_MASK = (1 << 64) - 1
DARWIN_MAP_FAILED = ctypes.c_void_p(-1).value
DARWIN_MINCORE_RESIDENT = 0x01
DARWIN_MS_INVALIDATE = 0x0002
COLD_PAGE_INVALIDATION_ATTEMPTS = 50
TIMING_SUMMARY_PATH = Path(tempfile.gettempdir()) / "cargo-berth-ready-timing-summary.json"

sys.path.insert(0, str(SCRIPTS_ROOT))


def installed_timing_artifact_digests() -> dict[str, str]:
    """Fingerprint the globally installed engine and the three hook wrappers.

    These four files are the whole installed front end. A measurement is only
    ever a statement about the pair that produced it, so the digests travel with
    the published bound rather than being recomputed from whatever is installed
    when someone later reads the number.
    """

    return {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in INSTALLED_TIMING_ARTIFACTS
    }


def run_explicit_engine_install(arguments: list[str]) -> int:
    """Run the separately selected installer mode and never start a test class."""

    if len(arguments) != 1:
        print(
            f"usage: {Path(sys.argv[0]).name} --install-engine /path/to/cargo-liner",
            file=sys.stderr,
        )
        return 2
    repository_root = Path(arguments[0]).resolve()
    installation = subprocess.run(
        [str(INSTALL_SCRIPT), str(repository_root)],
        cwd=repository_root,
        env=os.environ.copy(),
        text=True,
        check=False,
    )
    return installation.returncode


@dataclass(frozen=True)
class HookInvocation:
    """One completed hook plus every cargo-berth argv the hook issued."""

    process: subprocess.CompletedProcess[str]
    cargo_berth_calls: list[list[str]]
    git_calls: list[list[str]]
    jq_call_count: int
    elapsed_seconds: float
    jq_trace: tuple[str, ...] = ()

    @property
    def hook_level_executable_count(self) -> int:
        """Count Bash plus the shim's cargo-berth and jq child executions."""

        return 1 + len(self.cargo_berth_calls) + self.jq_call_count


# Every PostToolUse route now costs the same two processes: the wrapper's bash,
# and the one `exec cargo-berth hook post-tool-use` it hands off to. No jq at
# all. The wrapper does not branch on the outcome, so nothing the hook itself does
# can move this number -- what used to differ per outcome was a front end that
# read the board in a second engine call and rendered incidents through jq, and
# that front end is gone. A per-outcome expectation is what let this measurement
# keep asserting a process count the wrapper had stopped producing. The only
# cells that record more are the ones whose fixture injects a competing process
# on purpose; see expected_hook_level_executables below.
HOOK_LEVEL_EXECUTABLES = 2


def expected_hook_level_executables(
    git_race_transition: "GitRaceTransition",
) -> int:
    """Return the executables one hook invocation records for this fixture."""

    # Two fixtures reproduce a race by having the instrumented git wrapper fire a
    # real competing cargo-berth process -- a foreign `claim` against the same
    # scope, or a concurrent `release` of the marker reservation -- while the
    # engine is mid-`git status`. That process is logged to the same call log this
    # count reads, so those cells record one executable beyond the two the hook
    # front end itself spawns. It is the fixture's injection being counted, not
    # the wrapper branching on an outcome.
    if git_race_transition is GitRaceTransition.UNCHANGED:
        return HOOK_LEVEL_EXECUTABLES
    return HOOK_LEVEL_EXECUTABLES + 1


# `hook post-tool-use` writes its outcome into the JSON body on stdout and always
# exits 0: the PostToolUse contract reads a non-zero exit as a failed hook, not as
# a reported outcome, so the route has exactly one exit code and it cannot vary.
# The per-route numbers that used to sit at these call sites -- 20 for a clear
# tree, 21 for a widen or a replay stop -- belonged to the retired
# `drift --post-tool-use-payload` route, which encoded the outcome in the exit
# code because it had no JSON body to put it in.
POST_TOOL_USE_HOOK_EXIT_CODE = 0


class ProcessCacheTemperature(Enum):
    """Executable-page coverage carried by the published timing result."""

    COLD_EXECUTABLE_PAGES = "cold_executable_pages"
    WARMED_EXECUTABLE_PAGES = "warmed_executable_pages"


class TimingJournalAge(Enum):
    """Journal history carried by one ready-engine outcome sample."""

    SHORT = "short"
    WORKING_REPOSITORY = "working_repository"

    @property
    def record_count(self) -> int:
        """Return the exact journal length represented by this age."""

        match self:
            case TimingJournalAge.SHORT:
                return SHORT_TIMING_JOURNAL_RECORD_COUNT
            case TimingJournalAge.WORKING_REPOSITORY:
                return WORKING_REPOSITORY_JOURNAL_RECORD_COUNT


class TimingHistoryProfile(Enum):
    """Commit topology measured by a repository-history timing row."""

    SHALLOW = "shallow"
    DEEP = "deep"
    DIVERGENT = "divergent"


class HistorySubjectCardinality(Enum):
    """Number of active reservation histories in one history row."""

    ONE = 1
    TWENTY = 20


class LostEvidenceAlertCardinality(Enum):
    """Number of rewritten release alerts rendered by one recovery row."""

    ONE = 1
    TWENTY = 20


class IncursionIncidentCardinality(Enum):
    """Number of retained incidents serialized by one live board read."""

    ONE = 1
    FIFTY = 50


class RetainedIncursionState(Enum):
    """Board section that owns the incidents during the measured read."""

    OUTSTANDING = "outstanding"
    RESOLVED = "resolved"


@dataclass(frozen=True)
class TimedChildExecutablePages:
    """Executable files whose page residency defines one timed sample."""

    paths: tuple[Path, ...]

    def invalidate_and_verify_cold(self) -> None:
        """Invalidate every file and require zero resident pages afterward."""

        for path in sorted({path.resolve(strict=True) for path in self.paths}):
            self.invalidate_and_verify_file(path)

    @staticmethod
    def invalidate_and_verify_file(path: Path) -> None:
        """Apply Darwin `MS_INVALIDATE` and gate on `mincore` residency."""

        file_descriptor = os.open(path, os.O_RDONLY)
        try:
            file_size = os.fstat(file_descriptor).st_size
            if file_size == 0:
                raise AssertionError(f"timed child executable is empty: {path}")
            mapped_address = cast(
                int,
                DARWIN_LIBC.mmap(
                    None,
                    file_size,
                    DARWIN_PROT_READ,
                    DARWIN_MAP_SHARED,
                    file_descriptor,
                    0,
                ),
            )
            if mapped_address == DARWIN_MAP_FAILED:
                raise_last_darwin_error("mmap", path)
            try:
                page_count = (file_size + DARWIN_PAGE_SIZE - 1) // DARWIN_PAGE_SIZE
                resident_pages = page_count
                for _ in range(COLD_PAGE_INVALIDATION_ATTEMPTS):
                    if DARWIN_LIBC.msync(
                        mapped_address, file_size, DARWIN_MS_INVALIDATE
                    ) != 0:
                        raise_last_darwin_error("msync(MS_INVALIDATE)", path)
                    residency = (ctypes.c_ubyte * page_count)()
                    if DARWIN_LIBC.mincore(
                        mapped_address, file_size, residency
                    ) != 0:
                        raise_last_darwin_error("mincore", path)
                    resident_pages = sum(
                        cast(int, residency[page_index]) & DARWIN_MINCORE_RESIDENT
                        for page_index in range(page_count)
                    )
                    if resident_pages == 0:
                        break
                    time.sleep(0.01)
                else:
                    raise AssertionError(
                        f"cold timing gate found {resident_pages} of {page_count} resident pages for {path} after {COLD_PAGE_INVALIDATION_ATTEMPTS} invalidations"
                    )
            finally:
                if DARWIN_LIBC.munmap(mapped_address, file_size) != 0:
                    raise_last_darwin_error("munmap", path)
        finally:
            os.close(file_descriptor)


DARWIN_LIBC = ctypes.CDLL(None, use_errno=True)
DARWIN_LIBC.mmap.argtypes = (
    ctypes.c_void_p,
    ctypes.c_size_t,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_longlong,
)
DARWIN_LIBC.mmap.restype = ctypes.c_void_p
DARWIN_LIBC.msync.argtypes = (ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int)
DARWIN_LIBC.msync.restype = ctypes.c_int
DARWIN_LIBC.mincore.argtypes = (
    ctypes.c_void_p,
    ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_ubyte),
)
DARWIN_LIBC.mincore.restype = ctypes.c_int
DARWIN_LIBC.munmap.argtypes = (ctypes.c_void_p, ctypes.c_size_t)
DARWIN_LIBC.munmap.restype = ctypes.c_int
DARWIN_MAP_SHARED = 0x0001
DARWIN_PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")
DARWIN_PROT_READ = 0x01


def raise_last_darwin_error(operation: str, path: Path) -> None:
    """Raise the errno recorded by one Darwin virtual-memory operation."""

    error_number = ctypes.get_errno()
    raise OSError(error_number, f"{operation}: {os.strerror(error_number)}", path)


def following_fixture_uuid_v7(seed: str, offset: int) -> str:
    """Return a nearby UUIDv7 while preserving the seed's version and variant."""

    seed_value = int(uuid.UUID(seed).hex, 16)
    return str(uuid.UUID(int=seed_value + offset))


class EngineInstallationState(Enum):
    """Whether a wrapper finds an engine on PATH to hand the event to."""

    READY = "Ready"
    NEEDS_REPAIR = "NeedsRepair"


class PostToolUseOutcome(Enum):
    """Every engine or live-board result required by the PostToolUse matrix."""

    TYPED_CLEAR = "typed_clear"
    ORDINARY_WIDEN = "ordinary_widen"
    FIRST_TOUCH_ACQUISITION = "first_touch_acquisition"
    FOREIGN_ONLY_INCURSION = "foreign_only_incursion"
    RECORDED_INCURSION = "recorded_incursion"
    UNREADABLE_JOURNAL = "unreadable_journal"
    DUPLICATE_INCURSION_INCIDENT = "duplicate_incursion_incident"
    POST_WRITE_INCURSION_ACQUIRED = "post_write_incursion_acquired"
    POST_WRITE_INCURSION_NOT_ACQUIRED = "post_write_incursion_not_acquired"
    COLLISION = "collision"
    ATTRIBUTION = "attribution"
    LOST_EVIDENCE_RESOLVED_TRUNK = "lost_evidence_resolved_trunk"
    LOST_EVIDENCE_UNRESOLVED_TRUNK = "lost_evidence_unresolved_trunk"
    TRUNK_EQUIVALENCE_POSITIVE = "trunk_equivalence_positive"
    TRUNK_EQUIVALENCE_NEGATIVE = "trunk_equivalence_negative"
    SUCCESSOR_EQUIVALENCE_POSITIVE = "successor_equivalence_positive"
    SUCCESSOR_EQUIVALENCE_NEGATIVE = "successor_equivalence_negative"
    STALE_SESSION_MAPPING = "stale_session_mapping"
    STALE_MARKER_RUN = "stale_marker_run"
    SESSION_WORKTREE_MISMATCH = "session_worktree_mismatch"
    TYPED_REPLAY_FAILURE = "typed_replay_failure"


REQUIRED_READY_OUTCOMES = frozenset(PostToolUseOutcome)


class ScopedEquivalenceVerdict(Enum):
    """Content relation constructed for a scoped-patch timing row."""

    EQUIVALENT = "equivalent"
    DIFFERENT = "different"


class PostWriteProtection(Enum):
    """Whether the post-write row includes one unreserved path to acquire."""

    ACQUIRED = "acquired"
    NOT_ACQUIRED = "not_acquired"


class TrunkResolution(Enum):
    """Whether the configured trunk resolves during lost-evidence recovery."""

    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"


class GitRaceTransition(Enum):
    """Real ledger mutation performed during one instrumented git read."""

    UNCHANGED = "unchanged"
    CLAIM_COLLIDING_SCOPE = "claim_colliding_scope"
    RELEASE_MARKER_RESERVATION = "release_marker_reservation"


class DurableProofCacheState(Enum):
    """Whether a scoped-equivalence result exists before the measured call."""

    NOT_APPLICABLE = "not_applicable"
    NOT_EVALUATED = "not_evaluated"
    STORED = "stored"


class PostToolUseGitProcessContract(Enum):
    """Git process-count contract checked for one ready-state timing cell."""

    SCOPED_EQUIVALENCE_COUNT = "scoped_equivalence_count"
    OUTCOME_PROCESS_CEILING = "outcome_process_ceiling"


class PostToolUseObservationContract(Enum):
    """One repeatable process observation required by a timing cell."""

    HOOK_LEVEL_EXECUTABLE_COUNT = "hook_level_executable_count"
    PROVENANCE_LOG_ARGV_COUNT = "provenance_log_argv_count"
    PROVENANCE_REV_LIST_ARGV_COUNT = "provenance_rev_list_argv_count"
    HOOK_LEVEL_EXECUTABLE_SAMPLE_CONSISTENCY = (
        "hook_level_executable_sample_consistency"
    )
    GIT_ARGV_SAMPLE_CONSISTENCY = "git_argv_sample_consistency"
    PROVENANCE_LOG_SAMPLE_CONSISTENCY = "provenance_log_sample_consistency"
    PROVENANCE_REV_LIST_SAMPLE_CONSISTENCY = (
        "provenance_rev_list_sample_consistency"
    )
    EQUIVALENCE_SAMPLE_CONSISTENCY = "equivalence_sample_consistency"


@dataclass(frozen=True)
class PostToolUseTimingCell:
    """One typed engine outcome and the process facts its shim route must retain."""

    outcome: PostToolUseOutcome
    durable_proof_cache_states: tuple[DurableProofCacheState, ...]
    expected_provenance_log_argv: int
    expected_provenance_rev_list_argv: int
    expected_equivalence_argv: int
    required_rendered_text: str
    maximum_git_argv: int


@dataclass(frozen=True)
class PostToolUseGitContractMismatch:
    """One ready-state cell whose observed Git argv changed its contract."""

    outcome: PostToolUseOutcome
    journal_age: TimingJournalAge
    process_cache_temperature: ProcessCacheTemperature
    durable_proof_cache_state: DurableProofCacheState
    git_process_contract: PostToolUseGitProcessContract
    expected_count: int
    observed_count: int
    observed_argv: tuple[tuple[str, ...], ...]

    def as_report(self) -> dict[str, object]:
        """Return the complete mismatch evidence for the matrix summary."""

        return {
            "outcome": self.outcome.value,
            "journal_age": self.journal_age.value,
            "process_cache_temperature": self.process_cache_temperature.value,
            "durable_proof_cache_state": self.durable_proof_cache_state.value,
            "git_process_contract": self.git_process_contract.value,
            "expected_count": self.expected_count,
            "observed_count": self.observed_count,
            "observed_argv": self.observed_argv,
        }


@dataclass(frozen=True)
class PostToolUseSampleProcessEvidence:
    """Raw child-process argv explaining one timing sample's observation."""

    sample_index: int
    cargo_berth_argv: tuple[tuple[str, ...], ...]
    git_argv: tuple[tuple[str, ...], ...]
    jq_trace: tuple[str, ...]

    def as_report(self) -> dict[str, object]:
        """Return the raw process evidence for one timing sample."""

        return {
            "sample_index": self.sample_index,
            "cargo_berth_argv": self.cargo_berth_argv,
            "git_argv": self.git_argv,
            "jq_trace": self.jq_trace,
        }


@dataclass(frozen=True)
class PostToolUseObservationContractMismatch:
    """One timing-cell observation deferred until after summary publication."""

    outcome: PostToolUseOutcome
    journal_age: TimingJournalAge
    process_cache_temperature: ProcessCacheTemperature
    durable_proof_cache_state: DurableProofCacheState
    observation_contract: PostToolUseObservationContract
    expected_value: int
    observed_value: int
    observed_sample_values: tuple[int, ...]
    sample_process_evidence: tuple[PostToolUseSampleProcessEvidence, ...]

    def as_report(self) -> dict[str, object]:
        """Return enough evidence to correct a stale timing-cell expectation."""

        return {
            "outcome": self.outcome.value,
            "journal_age": self.journal_age.value,
            "process_cache_temperature": self.process_cache_temperature.value,
            "durable_proof_cache_state": self.durable_proof_cache_state.value,
            "observation_contract": self.observation_contract.value,
            "expected_value": self.expected_value,
            "observed_value": self.observed_value,
            "observed_sample_values": self.observed_sample_values,
            "raw_argv": [
                evidence.as_report() for evidence in self.sample_process_evidence
            ],
        }


@dataclass(frozen=True)
class PostToolUseEngineExecutionMeasurement:
    """One timed standalone engine execution of the real PostToolUse request."""

    elapsed_seconds: float
    rendered_response: dict[str, object]


@dataclass(frozen=True)
class PostToolUseAttributionMismatch:
    """One temperature whose named components missed the measured intercept."""

    process_cache_temperature: ProcessCacheTemperature
    measurement_run: int
    measured_zero_git_intercept_seconds: float
    component_sum_seconds: float
    attribution_error_fraction: float
    components: dict[str, float]
    cumulative_routes: dict[str, float]

    def as_report(self) -> dict[str, object]:
        """Return the complete mismatch evidence for the matrix summary."""

        return {
            "process_cache_temperature": self.process_cache_temperature.value,
            "measurement_run": self.measurement_run,
            "measured_zero_git_intercept_seconds": self.measured_zero_git_intercept_seconds,
            "component_sum_seconds": self.component_sum_seconds,
            "attribution_error_fraction": self.attribution_error_fraction,
            "tolerance_fraction": FIXED_COST_ATTRIBUTION_TOLERANCE_FRACTION,
            "components": self.components,
            "cumulative_routes": self.cumulative_routes,
        }


@dataclass(frozen=True)
class PublishedHistoryMeasurement:
    """One measured history topology and its exact repository sizes."""

    profile: TimingHistoryProfile
    base_commit_count: int
    commits_per_subject: int
    subject_cardinalities: tuple[HistorySubjectCardinality, ...]

    def head_commit_count(self) -> int:
        """Return the first subject's ancestry length."""

        return self.base_commit_count + self.commits_per_subject

    def all_refs_commit_count(
        self, subject_cardinality: HistorySubjectCardinality
    ) -> int:
        """Return the unique commit count reachable from all subject branches."""

        return (
            self.base_commit_count
            + self.commits_per_subject * subject_cardinality.value
        )


@dataclass(frozen=True)
class PublishedRecoveryInput:
    """One recovery-action input whose complete rendered commands were measured."""

    outcome: PostToolUseOutcome
    action_kinds: tuple[str, ...]
    action_argv: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class PublishedPostToolUseBound:
    """The complete, deliberately narrow contract proved by the timing matrix."""

    covered_path: Path
    installation_state: EngineInstallationState
    measured_process_cache_temperatures: tuple[ProcessCacheTemperature, ...]
    maximum_seconds: float
    samples_per_outcome: int
    outcome_matrix_journal_ages: tuple[TimingJournalAge, ...]
    outcome_matrix_history_commit_count: int
    outcome_matrix_retained_reservation_count: int
    history_measurements: tuple[PublishedHistoryMeasurement, ...]
    measured_recovery_inputs: tuple[PublishedRecoveryInput, ...]
    excluded_global_hooks: str
    engine_invocation: str

    @property
    def largest_recovery_action_set(self) -> tuple[str, ...]:
        """Return the measured input with the most recovery actions."""

        largest_input = max(
            self.measured_recovery_inputs,
            key=lambda recovery_input: len(recovery_input.action_kinds),
        )
        return largest_input.action_kinds

    @property
    def longest_recovery_action_argv(self) -> tuple[str, ...]:
        """Return the longest individual recovery argv in the measured inputs."""

        return max(
            (
                argv
                for recovery_input in self.measured_recovery_inputs
                for argv in recovery_input.action_argv
            ),
            key=len,
        )


@dataclass(frozen=True)
class PostToolUseTimingBudgetOverrun:
    """One measured sample at or above the published wall-clock limit."""

    outcome: str
    process_cache_temperature: ProcessCacheTemperature
    durable_proof_cache_state: DurableProofCacheState
    sample_index: int
    elapsed_seconds: float


@dataclass(frozen=True)
class TimingRepositoryContract:
    """Exact repository dimensions one timing fixture must satisfy."""

    journal_record_count: int
    retained_reservation_count: int
    head_commit_count: int


@dataclass
class ScratchPostToolUseRepository:
    """Own one real repository and every state-transition input for a timed hook."""

    temporary_directory: tempfile.TemporaryDirectory[str]
    repository_root: Path
    hook_root: Path
    equivalence_object_ids: tuple[str, ...]
    git_race_transition: GitRaceTransition
    transition_environment: dict[str, str]
    journal_contract_applies: bool = True

    def cleanup(self) -> None:
        """Remove the independently constructed scratch repository."""

        self.temporary_directory.cleanup()


PUBLISHED_POST_TOOL_USE_BOUND = PublishedPostToolUseBound(
    covered_path=POST_BASH_HOOK,
    installation_state=EngineInstallationState.READY,
    measured_process_cache_temperatures=(
        ProcessCacheTemperature.COLD_EXECUTABLE_PAGES,
        ProcessCacheTemperature.WARMED_EXECUTABLE_PAGES,
    ),
    maximum_seconds=POST_TOOL_USE_BOUND_SECONDS,
    samples_per_outcome=POST_TOOL_USE_SAMPLE_COUNT,
    outcome_matrix_journal_ages=(
        TimingJournalAge.SHORT,
        TimingJournalAge.WORKING_REPOSITORY,
    ),
    outcome_matrix_history_commit_count=SHALLOW_TIMING_HISTORY_COMMIT_COUNT,
    outcome_matrix_retained_reservation_count=TIMING_RETAINED_RESERVATION_COUNT,
    history_measurements=(
        PublishedHistoryMeasurement(
            profile=TimingHistoryProfile.SHALLOW,
            base_commit_count=SHALLOW_TIMING_HISTORY_COMMIT_COUNT,
            commits_per_subject=0,
            subject_cardinalities=(
                HistorySubjectCardinality.ONE,
                HistorySubjectCardinality.TWENTY,
            ),
        ),
        PublishedHistoryMeasurement(
            profile=TimingHistoryProfile.DEEP,
            base_commit_count=DEEP_TIMING_HISTORY_COMMIT_COUNT,
            commits_per_subject=0,
            subject_cardinalities=(
                HistorySubjectCardinality.ONE,
                HistorySubjectCardinality.TWENTY,
            ),
        ),
        PublishedHistoryMeasurement(
            profile=TimingHistoryProfile.DIVERGENT,
            base_commit_count=DIVERGENT_TIMING_BASE_COMMIT_COUNT,
            commits_per_subject=DIVERGENT_TIMING_COMMITS_PER_SUBJECT,
            subject_cardinalities=(
                HistorySubjectCardinality.ONE,
                HistorySubjectCardinality.TWENTY,
            ),
        ),
    ),
    measured_recovery_inputs=(
        PublishedRecoveryInput(
            outcome=PostToolUseOutcome.STALE_SESSION_MAPPING,
            action_kinds=("clear_session_mapping",),
            action_argv=(("cargo-berth", "identity", "clear-session", "--json"),),
        ),
        PublishedRecoveryInput(
            outcome=PostToolUseOutcome.SESSION_WORKTREE_MISMATCH,
            action_kinds=(
                "rerun_from_holding_worktree",
                "claim_separately_here",
            ),
            action_argv=(
                ("cargo-berth", "drift", "--json"),
                ("cargo-berth", "identity", "clear-session", "--json"),
            ),
        ),
    ),
    excluded_global_hooks="matcherless random-ack and context-usage PostToolUse hooks",
    engine_invocation="wrapper checks PATH once, then execs cargo-berth hook post-tool-use",
)


def envelope(
    verb: str,
    status: str,
    exit_code: int,
    payload: dict[str, object],
    reservations: list[str] | None = None,
) -> dict[str, object]:
    """Build one complete engine envelope around fixture-owned typed facts."""

    return {
        "verb": verb,
        "status": status,
        "exit_code": exit_code,
        "reservations": reservations or [],
        "blocked_by": [],
        "message": UNREAD_MESSAGE,
        "payload": payload,
        "presentation": {"kind": "not_provided"},
    }


class InstalledFrontEndFixture(unittest.TestCase):
    """Build repositories and drive the installed wrappers and engine.

    This carries no test of its own. It exists so the wrapper suite and the
    timing suite share one definition of the installed front end instead of
    one borrowing the other's test class.
    """

    base_environment: ClassVar[dict[str, str]] = {}
    installed_artifact_digests: ClassVar[dict[str, str]] = {}

    @override
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        scratch_root = Path("/tmp/claude")
        scratch_root.mkdir(parents=True, exist_ok=True)
        self.temporary_directory: tempfile.TemporaryDirectory[str] = (
            tempfile.TemporaryDirectory(
                prefix="cargo-berth-hook-rendering.", dir=scratch_root
            )
        )
        self.fixture_root: Path = Path(self.temporary_directory.name)
        self.repository_root: Path = self.fixture_root / "repository"
        self.edit_path: Path = self.repository_root / "source file.rs"
        self.holding_root: Path = self.fixture_root / "holding worktree"
        self.environment: dict[str, str] = {}

    @classmethod
    @override
    def setUpClass(cls) -> None:
        if os.environ.get("CARGO_BERTH_TIMING_REPOSITORY_ROOT") is not None:
            raise RuntimeError(
                f"automatic timing-test installation is disabled; run `{Path(__file__).name} --install-engine /path/to/cargo-liner` as a separate command, then invoke the tests without CARGO_BERTH_TIMING_REPOSITORY_ROOT"
            )
        unavailable_artifacts = [
            path
            for path in INSTALLED_TIMING_ARTIFACTS
            if not path.is_file() or not os.access(path, os.R_OK)
        ]
        if unavailable_artifacts:
            raise RuntimeError(
                f"the timing tests never install global artifacts; run `{Path(__file__).name} --install-engine /path/to/cargo-liner` separately before testing (unavailable: {unavailable_artifacts!r})"
            )
        if not INSTALLED_BINARY.is_file() or not os.access(INSTALLED_BINARY, os.X_OK):
            raise RuntimeError(
                "the explicitly installed cargo-berth binary is not executable"
            )
        cls.base_environment = os.environ.copy()
        cls.base_environment["PATH"] = os.pathsep.join(
            [str(INSTALLED_DIRECTORY), cls.base_environment.get("PATH", "")]
        )
        selected_binary = shutil.which(
            "cargo-berth", path=cls.base_environment["PATH"]
        )
        if selected_binary != str(INSTALLED_BINARY):
            raise RuntimeError(
                f"fixture PATH did not select the explicitly installed cargo-berth binary: selected {selected_binary!r}, expected {str(INSTALLED_BINARY)!r}"
            )
        cls.installed_artifact_digests = installed_timing_artifact_digests()

    @override
    def setUp(self) -> None:
        self.repository_root.mkdir()
        (self.repository_root / ".git").mkdir()
        self.holding_root.mkdir()
        self.environment = self.base_environment.copy()

    @override
    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def run_installed_engine_hook(
        self,
        fixture: ScratchPostToolUseRepository,
        process_cache_temperature: ProcessCacheTemperature = ProcessCacheTemperature.WARMED_EXECUTABLE_PAGES,
        hook: Path = POST_BASH_HOOK,
        session_id: str = "fixture-session",
    ) -> HookInvocation:
        """Run the canonical hook with forwarding engine, git, and jq traces."""

        fixture_bin = Path(
            tempfile.mkdtemp(
                prefix="cargo-berth-hook-trace.",
                dir=fixture.temporary_directory.name,
            )
        )
        cargo_call_log = fixture_bin / "cargo-calls.txt"
        git_call_log = fixture_bin / "git-calls.txt"
        jq_call_log = fixture_bin / "jq-calls.txt"
        bash_environment = fixture_bin / "bash_env.sh"
        cargo_wrapper = fixture_bin / "cargo-berth"
        git_wrapper = fixture_bin / "git"
        requires_git_race_wrapper = (
            fixture.git_race_transition is not GitRaceTransition.UNCHANGED
        )
        real_git = shutil.which("git", path=self.base_environment["PATH"])
        real_jq = shutil.which("jq", path=self.base_environment["PATH"])
        real_bash = shutil.which("bash", path=self.base_environment["PATH"])
        if real_bash is None or real_git is None or real_jq is None:
            raise RuntimeError("the timing fixture requires bash, git, and jq")

        _ = bash_environment.write_text(
            "\n".join(
                (
                    "if [ -z \"${CARGO_BERTH_TEST_ROOT_SHELL_PID:-}\" ]; then",
                    "    export CARGO_BERTH_TEST_ROOT_SHELL_PID=$$",
                    "fi",
                    "function jq {",
                    "    if [ \"$$\" = \"$CARGO_BERTH_TEST_ROOT_SHELL_PID\" ]; then",
                    f"        printf '%s\\t%s\\t%s\\n' \"$$\" \"${{BASH_SUBSHELL:-unknown}}\" \"${{FUNCNAME[1]:-top-level}}\" >> {shlex.quote(str(jq_call_log))}",
                    "    fi",
                    f"    {shlex.quote(real_jq)} \"$@\"",
                    "}",
                    "",
                )
            ),
            encoding="utf-8",
        )
        _ = cargo_wrapper.write_text(
            self.forwarding_cargo_berth_wrapper(cargo_call_log),
            encoding="utf-8",
        )
        cargo_wrapper.chmod(0o755)
        if requires_git_race_wrapper:
            _ = git_wrapper.write_text(
                self.forwarding_git_wrapper(),
                encoding="utf-8",
            )
            git_wrapper.chmod(0o755)

        hook_environment = self.base_environment.copy()
        hook_environment.update(fixture.transition_environment)
        hook_environment.update(
            {
                "BASH_ENV": str(bash_environment),
                "CARGO_BERTH_TEST_BINARY": str(INSTALLED_BINARY),
                "CARGO_BERTH_TEST_CARGO_CALL_LOG": str(cargo_call_log),
                "CARGO_BERTH_TEST_GIT_RACE_TRANSITION": fixture.git_race_transition.value,
                "CARGO_BERTH_TEST_GIT_TRACE": str(git_call_log),
                "CARGO_BERTH_TEST_REAL_GIT": real_git,
                "CARGO_BERTH_TEST_REAL_PATH": self.base_environment["PATH"],
                "PATH": os.pathsep.join(
                    [str(fixture_bin), self.base_environment["PATH"]]
                ),
            }
        )
        hook_environment["GIT_TRACE2_EVENT"] = str(git_call_log)
        if process_cache_temperature is ProcessCacheTemperature.COLD_EXECUTABLE_PAGES:
            timed_executables = [
                hook,
                Path("/usr/bin/env"),
                Path(real_bash),
                Path("/bin/sh"),
                cargo_wrapper,
                INSTALLED_BINARY,
                Path(real_git),
                Path(real_jq),
            ]
            if requires_git_race_wrapper:
                timed_executables.append(git_wrapper)
            TimedChildExecutablePages(tuple(timed_executables)).invalidate_and_verify_cold()
        started_at = time.perf_counter()
        completed = subprocess.run(
            [str(hook)],
            cwd=fixture.hook_root,
            env=hook_environment,
            input=json.dumps(
                self.post_bash_payload_for(fixture.hook_root, session_id)
            ),
            text=True,
            capture_output=True,
            check=False,
        )
        elapsed_seconds = time.perf_counter() - started_at
        return HookInvocation(
            process=completed,
            cargo_berth_calls=self.read_delimited_calls(cargo_call_log),
            git_calls=self.read_git_trace2_event_calls(git_call_log),
            jq_call_count=self.line_count(jq_call_log),
            elapsed_seconds=elapsed_seconds,
            jq_trace=tuple(
                jq_call_log.read_text(encoding="utf-8").splitlines()
                if jq_call_log.exists()
                else []
            ),
        )

    def run_installed_engine_probe(
        self,
        repository_root: Path,
        arguments: list[str],
        expected_exit_code: int,
        process_cache_temperature: ProcessCacheTemperature,
    ) -> float:
        """Time one installed-engine route without changing its scheduling inputs."""

        environment = self.base_environment.copy()
        for variable in (
            "BASH_ENV",
            "CARGO_BERTH_BYPASS",
            "CARGO_BERTH_RUN",
            "CARGO_BERTH_TEST_GIT_TRACE",
            "CARGO_BERTH_TEST_REAL_GIT",
        ):
            _ = environment.pop(variable, None)
        environment["CARGO_BERTH_POST_COMMIT"] = "1"
        environment["CARGO_BERTH_SESSION_ID"] = "fixture-session"
        if (
            process_cache_temperature
            is ProcessCacheTemperature.COLD_EXECUTABLE_PAGES
        ):
            TimedChildExecutablePages((INSTALLED_BINARY,)).invalidate_and_verify_cold()
        started_at = time.perf_counter()
        completed = subprocess.run(
            [str(INSTALLED_BINARY), *arguments],
            cwd=repository_root,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        elapsed_seconds = time.perf_counter() - started_at
        self.assertEqual(
            completed.returncode,
            expected_exit_code,
            f"fixed-cost engine probe {' '.join(arguments)} failed: stdout={completed.stdout!r}, stderr={completed.stderr!r}",
        )
        return elapsed_seconds

    def run_installed_engine_post_tool_use_probe(
        self,
        repository_root: Path,
        process_cache_temperature: ProcessCacheTemperature,
    ) -> PostToolUseEngineExecutionMeasurement:
        """Time the exact engine request issued across the PostToolUse boundary."""

        elapsed_seconds, completed = (
            self.execute_installed_engine_post_tool_use_probe(
                repository_root, process_cache_temperature
            )
        )
        self.assertEqual(
            completed.returncode,
            POST_TOOL_USE_HOOK_EXIT_CODE,
            f"fixed-cost PostToolUse engine probe failed: stdout={completed.stdout!r}, stderr={completed.stderr!r}",
        )
        decoded = cast(object, json.loads(completed.stdout))
        self.assertIsInstance(decoded, dict)
        return PostToolUseEngineExecutionMeasurement(
            elapsed_seconds=elapsed_seconds,
            rendered_response=cast(dict[str, object], decoded),
        )

    def run_installed_engine_post_tool_use_elapsed_probe(
        self,
        repository_root: Path,
        process_cache_temperature: ProcessCacheTemperature,
    ) -> float:
        """Time a PostToolUse engine route whose pass-through output may be empty."""

        elapsed_seconds, completed = (
            self.execute_installed_engine_post_tool_use_probe(
                repository_root, process_cache_temperature
            )
        )
        self.assertEqual(
            completed.returncode,
            POST_TOOL_USE_HOOK_EXIT_CODE,
            f"fixed-cost PostToolUse engine probe failed: stdout={completed.stdout!r}, stderr={completed.stderr!r}",
        )
        return elapsed_seconds

    def execute_installed_engine_post_tool_use_probe(
        self,
        repository_root: Path,
        process_cache_temperature: ProcessCacheTemperature,
    ) -> tuple[float, subprocess.CompletedProcess[str]]:
        """Execute the exact engine request issued across the PostToolUse boundary."""

        environment = self.base_environment.copy()
        for variable in (
            "BASH_ENV",
            "CARGO_BERTH_TEST_GIT_TRACE",
            "CARGO_BERTH_TEST_REAL_GIT",
            "CARGO_BERTH_TEST_ROOT_SHELL_PID",
        ):
            _ = environment.pop(variable, None)
        if (
            process_cache_temperature
            is ProcessCacheTemperature.COLD_EXECUTABLE_PAGES
        ):
            TimedChildExecutablePages((INSTALLED_BINARY,)).invalidate_and_verify_cold()
        started_at = time.perf_counter()
        completed = subprocess.run(
            [str(INSTALLED_BINARY), "hook", "post-tool-use"],
            cwd=repository_root,
            env=environment,
            input=json.dumps(self.post_bash_payload_for(repository_root)),
            text=True,
            capture_output=True,
            check=False,
        )
        elapsed_seconds = time.perf_counter() - started_at
        return elapsed_seconds, completed

    def forwarding_cargo_berth_wrapper(self, call_log: Path) -> str:
        """Return a forwarding wrapper that performs live-board race transitions."""

        return f"""#!/bin/sh
set -eu
printf '%s' \"${{1-}}\" >> {shlex.quote(str(call_log))}
command_name=${{1-}}
shift || true
printf '\\t%s' \"$@\" >> {shlex.quote(str(call_log))}
printf '\\n' >> {shlex.quote(str(call_log))}
unset BASH_ENV CARGO_BERTH_TEST_ROOT_SHELL_PID
exec \"$CARGO_BERTH_TEST_BINARY\" \"$command_name\" \"$@\"
"""

    def forwarding_git_wrapper(self) -> str:
        """Return a one-use git wrapper that reproduces two lock-time races."""

        return f"""#!/bin/sh
set -eu
first_argument=${{1-}}
shift || true
second_argument=${{1-}}
if [ \"$first_argument\" = --no-optional-locks ] && [ \"$second_argument\" = status ] \
    && [ \"$CARGO_BERTH_TEST_GIT_RACE_TRANSITION\" != unchanged ] \
    && [ ! -e \"$CARGO_BERTH_TEST_GIT_RACE_TRIGGER\" ]; then
    trap '/bin/ln -sf \"$CARGO_BERTH_TEST_REAL_GIT\" \"$0\"' EXIT
    case $CARGO_BERTH_TEST_GIT_RACE_TRANSITION in
        claim_colliding_scope)
            : > \"$CARGO_BERTH_TEST_GIT_RACE_TRIGGER\"
            printf 'claim\\t%s\\t--run\\t%s\\t--why\\t%s\\t--json\\n' \
                \"$CARGO_BERTH_TEST_COLLISION_SCOPE\" \
                \"$CARGO_BERTH_TEST_FOREIGN_RUN\" \
                'deterministic drift collision' \
                >> \"$CARGO_BERTH_TEST_CARGO_CALL_LOG\"
            (
                cd \"$CARGO_BERTH_TEST_FOREIGN_ROOT\"
                unset GIT_TRACE2 GIT_TRACE2_BRIEF GIT_TRACE2_EVENT
                PATH=\"$CARGO_BERTH_TEST_REAL_PATH\" \
                    \"$CARGO_BERTH_TEST_BINARY\" claim \
                    \"$CARGO_BERTH_TEST_COLLISION_SCOPE\" \
                    --run \"$CARGO_BERTH_TEST_FOREIGN_RUN\" \
                    --why 'deterministic drift collision' --json >/dev/null
            ) &
            transition_pid=$!
            ;;
        release_marker_reservation)
            : > \"$CARGO_BERTH_TEST_GIT_RACE_TRIGGER\"
            printf 'release\\t%s\\t--json\\n' \
                \"$CARGO_BERTH_TEST_MARKER_RESERVATION\" \
                >> \"$CARGO_BERTH_TEST_CARGO_CALL_LOG\"
            (
                cd \"$CARGO_BERTH_TEST_TRANSITION_ROOT\"
                unset GIT_TRACE2 GIT_TRACE2_BRIEF GIT_TRACE2_EVENT
                PATH=\"$CARGO_BERTH_TEST_REAL_PATH\" \
                    CARGO_BERTH_RUN=\"$CARGO_BERTH_TEST_SUBJECT_RUN\" \
                    \"$CARGO_BERTH_TEST_BINARY\" release \
                    \"$CARGO_BERTH_TEST_MARKER_RESERVATION\" --json >/dev/null
            ) &
            transition_pid=$!
            ;;
        unchanged) ;;
    esac
    git_status=0
    "$CARGO_BERTH_TEST_REAL_GIT" "$first_argument" "$@" || git_status=$?
    transition_status=0
    wait "$transition_pid" || transition_status=$?
    [ "$transition_status" -eq 0 ] || exit "$transition_status"
    exit "$git_status"
fi
exec \"$CARGO_BERTH_TEST_REAL_GIT\" \"$first_argument\" \"$@\"
"""

    @staticmethod
    def read_delimited_calls(path: Path) -> list[list[str]]:
        """Read tab-delimited argv records emitted by a forwarding wrapper."""

        if not path.exists():
            return []
        return [line.split("\t") for line in path.read_text().splitlines()]

    @staticmethod
    def read_git_trace2_event_calls(path: Path) -> list[list[str]]:
        """Read exact process-start argv arrays emitted by Git's event stream."""

        if not path.exists():
            return []
        calls: list[list[str]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            record = cast(dict[str, object], json.loads(line))
            if record.get("event") != "start":
                continue
            raw_argv = record.get("argv")
            if not isinstance(raw_argv, list):
                continue
            raw_arguments = cast(list[object], raw_argv)
            if not all(isinstance(argument, str) for argument in raw_arguments):
                continue
            argv = cast(list[str], raw_arguments)
            if not argv or Path(argv[0]).name != "git":
                continue
            calls.append(argv[1:])
        return calls

    @staticmethod
    def line_count(path: Path) -> int:
        """Count newline-delimited trace records."""

        if not path.exists():
            return 0
        return len(path.read_text(encoding="utf-8").splitlines())

    @staticmethod
    def journal_fingerprint(journal_bytes: bytes) -> int:
        """Calculate the projection's FNV-1a digest over complete journal bytes."""

        fingerprint = FNV_OFFSET_BASIS
        for byte in journal_bytes:
            fingerprint = ((fingerprint ^ byte) * FNV_PRIME) & UINT64_MASK
        return fingerprint

    def pre_edit_payload(self) -> dict[str, object]:
        return {
            "tool_name": "Edit",
            "session_id": "fixture-session",
            "cwd": str(self.repository_root),
            "tool_input": {"file_path": str(self.edit_path)},
        }

    def post_bash_payload(self) -> dict[str, object]:
        return {
            "tool_name": "Bash",
            "session_id": "fixture-session",
            "cwd": str(self.repository_root),
            "tool_input": {"command": "true"},
        }

    def session_start_payload(self) -> dict[str, object]:
        return {"session_id": "fixture-session", "cwd": str(self.repository_root)}

    def post_bash_payload_for(
        self,
        repository_root: Path,
        session_id: str = "fixture-session",
    ) -> dict[str, object]:
        """Build a PostToolUse payload naming one scratch worktree."""

        return {
            "tool_name": "Bash",
            "session_id": session_id,
            "cwd": str(repository_root),
            "tool_input": {"command": "true"},
        }

    def git_command(self, repository_root: Path, arguments: list[str]) -> str:
        """Run git in a scratch repository and return trimmed standard output."""

        completed = subprocess.run(
            ["git", *arguments],
            cwd=repository_root,
            env=self.base_environment,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            f"git {' '.join(arguments)} failed: {completed.stderr}",
        )
        return completed.stdout.strip()

    def engine_command(
        self,
        repository_root: Path,
        arguments: list[str],
        expected_exit_codes: tuple[int, ...] = (0,),
        session_id: str = "",
        coordination_run_id: str = "",
    ) -> dict[str, object]:
        """Run the installed engine against one scratch repository."""

        environment = self.base_environment.copy()
        for variable in (
            "CARGO_BERTH_BYPASS",
            "CARGO_BERTH_POST_COMMIT",
            "CARGO_BERTH_RUN",
            "CARGO_BERTH_SESSION_ID",
            "CARGO_BERTH_TEST_GIT_TRACE",
            "CARGO_BERTH_TEST_REAL_GIT",
        ):
            _ = environment.pop(variable, None)
        if session_id:
            environment["CARGO_BERTH_SESSION_ID"] = session_id
        if coordination_run_id:
            environment["CARGO_BERTH_RUN"] = coordination_run_id
        completed = subprocess.run(
            [str(INSTALLED_BINARY), *arguments],
            cwd=repository_root,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertIn(
            completed.returncode,
            expected_exit_codes,
            f"cargo-berth {' '.join(arguments)} failed: {completed.stdout}\n{completed.stderr}",
        )
        decoded = cast(object, json.loads(completed.stdout))
        self.assertIsInstance(decoded, dict)
        return cast(dict[str, object], decoded)

    def new_scratch_post_tool_use_repository(
        self, initial_commit_count: int
    ) -> ScratchPostToolUseRepository:
        """Construct an initialized repository below `/tmp/claude`."""

        Path("/tmp/claude").mkdir(parents=True, exist_ok=True)
        temporary_directory = tempfile.TemporaryDirectory(
            prefix="cargo-berth-post-tool-use.", dir="/tmp/claude"
        )
        fixture_root = Path(temporary_directory.name)
        repository_root = fixture_root / "repository"
        repository_root.mkdir()
        _ = (repository_root / "src").mkdir()
        _ = (repository_root / "src/lib.rs").write_text(
            "pub fn base() {}\n", encoding="utf-8"
        )
        _ = self.git_command(
            repository_root, ["init", "--quiet", "--initial-branch", "main"]
        )
        _ = self.git_command(
            repository_root, ["config", "user.email", "test@example.invalid"]
        )
        _ = self.git_command(
            repository_root, ["config", "user.name", "Berth Timing Test"]
        )
        _ = self.git_command(repository_root, ["add", "src/lib.rs"])
        _ = self.git_command(
            repository_root, ["commit", "--quiet", "-m", "initial"]
        )
        _ = self.engine_command(repository_root, ["init", "--json"])
        _ = self.git_command(
            repository_root, ["add", ".claude/config/berth.toml"]
        )
        _ = self.git_command(
            repository_root,
            [
                "-c",
                "core.hooksPath=/dev/null",
                "commit",
                "--quiet",
                "-m",
                "configure berth",
            ],
        )
        for commit_index in range(initial_commit_count - 2):
            _ = self.git_command(
                repository_root,
                [
                    "-c",
                    "core.hooksPath=/dev/null",
                    "commit",
                    "--quiet",
                    "--allow-empty",
                    "-m",
                    f"fixture history {commit_index}",
                ],
            )
        return ScratchPostToolUseRepository(
            temporary_directory=temporary_directory,
            repository_root=repository_root,
            hook_root=repository_root,
            equivalence_object_ids=(),
            git_race_transition=GitRaceTransition.UNCHANGED,
            transition_environment={},
        )

    def add_scratch_worktree(
        self, fixture: ScratchPostToolUseRepository, branch_name: str
    ) -> Path:
        """Add one linked worktree owned by the scratch fixture."""

        worktree_root = Path(fixture.temporary_directory.name) / branch_name
        _ = self.git_command(
            fixture.repository_root,
            [
                "worktree",
                "add",
                "--quiet",
                "-b",
                branch_name,
                str(worktree_root),
            ],
        )
        return worktree_root

    def commit_scratch_work(
        self, repository_root: Path, message: str
    ) -> str:
        """Commit all scratch-worktree changes without invoking installed hooks."""

        _ = self.git_command(repository_root, ["add", "--all"])
        _ = self.git_command(
            repository_root,
            [
                "-c",
                "core.hooksPath=/dev/null",
                "commit",
                "--quiet",
                "-m",
                message,
            ],
        )
        return self.git_command(repository_root, ["rev-parse", "HEAD"])

    def commit_empty_scratch_work(
        self, repository_root: Path, message: str
    ) -> str:
        """Add one history commit without introducing a drift path."""

        _ = self.git_command(
            repository_root,
            [
                "-c",
                "core.hooksPath=/dev/null",
                "commit",
                "--quiet",
                "--allow-empty",
                "-m",
                message,
            ],
        )
        return self.git_command(repository_root, ["rev-parse", "HEAD"])

    def claim_scratch_scope(
        self,
        repository_root: Path,
        scope: str,
        coordination_run_id: str,
        session_id: str = "",
    ) -> str:
        """Create one real retained reservation and return its identifier."""

        claimed = self.engine_command(
            repository_root,
            [
                "claim",
                scope,
                "--run",
                coordination_run_id,
                "--why",
                "PostToolUse timing fixture",
                "--json",
            ],
            session_id=session_id,
        )
        payload = cast(dict[str, object], claimed["payload"])
        data = cast(dict[str, object], payload["data"])
        return cast(str, data["reservation_id"])

    def abandon_scratch_reservation(
        self,
        repository_root: Path,
        reservation_id: str,
        coordination_run_id: str,
    ) -> None:
        """Retain one reservation as released without requiring git evidence."""

        _ = self.engine_command(
            repository_root,
            [
                "resolve",
                reservation_id,
                "--abandon",
                "--why",
                "timing fixture work is intentionally absent",
                "--json",
            ],
            coordination_run_id=coordination_run_id,
        )

    def journal_records(
        self, fixture: ScratchPostToolUseRepository
    ) -> list[dict[str, object]]:
        """Decode every real event in the scratch ledger."""

        journal_path = fixture.repository_root / ".git/cargo-berth/journal.ndjson"
        return [
            cast(dict[str, object], json.loads(line))
            for line in journal_path.read_text(encoding="utf-8").splitlines()
        ]

    def pad_journal_with_renewals(
        self,
        fixture: ScratchPostToolUseRepository,
        reservation_id: str,
        coordination_run_id: str,
        target_record_count: int,
    ) -> None:
        """Append real renewal facts until the named journal size is reached."""

        records = self.journal_records(fixture)
        self.assertLessEqual(len(records), target_record_count)
        if len(records) == target_record_count:
            return
        _ = self.engine_command(
            fixture.hook_root,
            ["renew", reservation_id, "--json"],
            coordination_run_id=coordination_run_id,
        )
        records = self.journal_records(fixture)
        renewal_template = records[-1]
        self.assertEqual(renewal_template["op"], "renew")
        additional_renewals = target_record_count - len(records)
        if additional_renewals > 0:
            journal_path = (
                fixture.repository_root / ".git/cargo-berth/journal.ndjson"
            )
            journal_bytes = journal_path.read_bytes()
            projection_path = (
                fixture.repository_root / ".git/cargo-berth/reservations.json"
            )
            projection = cast(
                dict[str, object],
                json.loads(projection_path.read_text(encoding="utf-8")),
            )
            generation = cast(int, renewal_template["projection_generation"])
            serialized_renewals: list[bytes] = []
            for offset in range(1, additional_renewals + 1):
                renewal = renewal_template.copy()
                renewal["event_id"] = following_fixture_uuid_v7(
                    cast(str, renewal_template["event_id"]), offset
                )
                renewal["projection_generation"] = generation + offset
                serialized_renewals.append(
                    json.dumps(renewal, separators=(",", ":")).encode()
                )
            journal_bytes += b"\n".join(serialized_renewals) + b"\n"
            _ = journal_path.write_bytes(journal_bytes)
            projection["generation"] = generation + additional_renewals
            projection["journal_end_offset"] = len(journal_bytes)
            projection["journal_fingerprint"] = self.journal_fingerprint(
                journal_bytes
            )
            _ = projection_path.write_text(
                json.dumps(projection, indent=2) + "\n", encoding="utf-8"
            )
        self.assertEqual(len(self.journal_records(fixture)), target_record_count)

    def remove_drift_fingerprint(self, worktree_root: Path) -> None:
        """Force the measured cheap call to rebuild its comparison from repository facts."""

        administrative_directory = self.git_command(
            worktree_root, ["rev-parse", "--git-path", "cargo-berth"]
        )
        ledger_directory = Path(administrative_directory)
        if not ledger_directory.is_absolute():
            ledger_directory = worktree_root / ledger_directory
        for fingerprint in ledger_directory.glob("drift-fingerprint-*.json"):
            fingerprint.unlink()

    def prepare_basic_timing_fixture(
        self, outcome: PostToolUseOutcome, journal_age: TimingJournalAge
    ) -> ScratchPostToolUseRepository:
        """Build clear, widen, and first-touch repository states."""

        fixture = self.new_scratch_post_tool_use_repository(
            SHALLOW_TIMING_HISTORY_COMMIT_COUNT
        )
        primary = self.claim_scratch_scope(
            fixture.repository_root,
            "file:owned.txt",
            FIRST_RUN,
            "fixture-session",
        )
        secondary = self.claim_scratch_scope(
            fixture.repository_root, "file:secondary.txt", SECOND_RUN
        )
        if outcome is PostToolUseOutcome.FIRST_TOUCH_ACQUISITION:
            self.pad_journal_with_renewals(
                fixture, primary, FIRST_RUN, journal_age.record_count - 2
            )
            self.abandon_scratch_reservation(
                fixture.repository_root, primary, FIRST_RUN
            )
            self.abandon_scratch_reservation(
                fixture.repository_root, secondary, SECOND_RUN
            )
            _ = (fixture.repository_root / "first-touch.rs").write_text(
                "new work\n", encoding="utf-8"
            )
        else:
            self.pad_journal_with_renewals(
                fixture, primary, FIRST_RUN, journal_age.record_count
            )
            if outcome is PostToolUseOutcome.ORDINARY_WIDEN:
                _ = (fixture.repository_root / "outside.txt").write_text(
                    "outside reservation\n", encoding="utf-8"
                )
        return fixture

    def prepare_incursion_timing_fixture(
        self, outcome: PostToolUseOutcome, journal_age: TimingJournalAge
    ) -> ScratchPostToolUseRepository:
        """Build an outstanding or transition-before-board incursion state."""

        fixture = self.new_scratch_post_tool_use_repository(
            SHALLOW_TIMING_HISTORY_COMMIT_COUNT
        )
        subject = self.claim_scratch_scope(
            fixture.repository_root,
            "file:owned.txt",
            FIRST_RUN,
            "fixture-session",
        )
        foreign_root = self.add_scratch_worktree(fixture, "incursion-holder")
        _ = self.claim_scratch_scope(
            foreign_root, "file:held.txt", SECOND_RUN
        )
        _ = (fixture.repository_root / "held.txt").write_text(
            "entered foreign reservation\n", encoding="utf-8"
        )
        if outcome is not PostToolUseOutcome.FOREIGN_ONLY_INCURSION:
            appended_by_transition = (
                1 if outcome is PostToolUseOutcome.RECORDED_INCURSION else 0
            )
            self.pad_journal_with_renewals(
                fixture,
                subject,
                FIRST_RUN,
                journal_age.record_count - 1 - appended_by_transition,
            )
            observed = self.engine_command(
                fixture.repository_root,
                ["drift", "--full", "--reservation", subject, "--json"],
                expected_exit_codes=(1,),
                session_id="fixture-session",
            )
            self.assertEqual(observed["status"], "incursion")
            records = self.journal_records(fixture)
            incursion_record = next(
                record for record in records if record["op"] == "incursion"
            )
            incident_id = cast(str, incursion_record["incident_id"])
            journal_path = (
                fixture.repository_root / ".git/cargo-berth/journal.ndjson"
            )
            match outcome:
                case PostToolUseOutcome.RECORDED_INCURSION:
                    _ = self.engine_command(
                        fixture.repository_root,
                        [
                            "resolve",
                            subject,
                            "--incursion",
                            incident_id,
                            "--json",
                        ],
                        coordination_run_id=FIRST_RUN,
                    )
                case PostToolUseOutcome.UNREADABLE_JOURNAL:
                    _ = journal_path.write_text("{}\n", encoding="utf-8")
                    fixture.journal_contract_applies = False
                case PostToolUseOutcome.DUPLICATE_INCURSION_INCIDENT:
                    with journal_path.open("a", encoding="utf-8") as journal:
                        _ = journal.write(
                            json.dumps(incursion_record, separators=(",", ":"))
                            + "\n"
                        )
                    fixture.journal_contract_applies = False
                case _:
                    raise AssertionError(
                        f"{outcome.value} has no incursion board state to build"
                    )
            self.remove_drift_fingerprint(fixture.repository_root)
        else:
            self.pad_journal_with_renewals(
                fixture, subject, FIRST_RUN, journal_age.record_count
            )
        return fixture

    def prepare_post_write_incursion_timing_fixture(
        self,
        post_write_protection: PostWriteProtection,
        journal_age: TimingJournalAge,
    ) -> ScratchPostToolUseRepository:
        """Build a markerless post-write incursion with one foreign holder."""

        fixture = self.new_scratch_post_tool_use_repository(
            SHALLOW_TIMING_HISTORY_COMMIT_COUNT
        )
        retired = self.claim_scratch_scope(
            fixture.repository_root,
            "file:retired.txt",
            FIRST_RUN,
            "fixture-session",
        )
        self.abandon_scratch_reservation(
            fixture.repository_root, retired, FIRST_RUN
        )
        foreign_root = self.add_scratch_worktree(fixture, "post-write-holder")
        holder = self.claim_scratch_scope(
            foreign_root, "file:held.txt", SECOND_RUN
        )
        self.pad_journal_with_renewals(
            fixture, holder, SECOND_RUN, journal_age.record_count
        )
        _ = (fixture.repository_root / "held.txt").write_text(
            "entered foreign reservation\n", encoding="utf-8"
        )
        if post_write_protection is PostWriteProtection.ACQUIRED:
            _ = (fixture.repository_root / "free.txt").write_text(
                "unreserved work\n", encoding="utf-8"
            )
        return fixture

    def prepare_attribution_timing_fixture(
        self, journal_age: TimingJournalAge
    ) -> ScratchPostToolUseRepository:
        """Build one committed incursion with a usable provenance anchor."""

        fixture = self.new_scratch_post_tool_use_repository(3)
        _ = self.claim_scratch_scope(
            fixture.repository_root, "file:held.txt", FIRST_RUN
        )
        subject_root = self.add_scratch_worktree(fixture, "attribution-subject")
        subject = self.claim_scratch_scope(
            subject_root,
            "file:subject-owned.txt",
            SECOND_RUN,
            "fixture-session",
        )
        fixture.hook_root = subject_root
        self.pad_journal_with_renewals(
            fixture, subject, SECOND_RUN, journal_age.record_count
        )
        _ = (subject_root / "held.txt").write_text(
            "committed incursion\n", encoding="utf-8"
        )
        _ = self.commit_scratch_work(subject_root, "enter the holder scope")
        self.remove_drift_fingerprint(subject_root)
        return fixture

    def prepare_collision_timing_fixture(
        self, journal_age: TimingJournalAge
    ) -> ScratchPostToolUseRepository:
        """Build a lock-time collision driven by a real foreign claim."""

        fixture = self.new_scratch_post_tool_use_repository(
            SHALLOW_TIMING_HISTORY_COMMIT_COUNT
        )
        subject = self.claim_scratch_scope(
            fixture.repository_root,
            "file:owned.txt",
            FIRST_RUN,
            "fixture-session",
        )
        retired = self.claim_scratch_scope(
            fixture.repository_root, "file:retired.txt", SECOND_RUN
        )
        self.abandon_scratch_reservation(
            fixture.repository_root, retired, SECOND_RUN
        )
        self.pad_journal_with_renewals(
            fixture, subject, FIRST_RUN, journal_age.record_count
        )
        foreign_root = self.add_scratch_worktree(fixture, "collision-holder")
        collision_path = "shared/collision.txt"
        (fixture.repository_root / "shared").mkdir()
        _ = (fixture.repository_root / collision_path).write_text(
            "observed before foreign claim\n", encoding="utf-8"
        )
        fixture.git_race_transition = GitRaceTransition.CLAIM_COLLIDING_SCOPE
        fixture.transition_environment = {
            "CARGO_BERTH_TEST_COLLISION_SCOPE": f"file:{collision_path}",
            "CARGO_BERTH_TEST_FOREIGN_ROOT": str(foreign_root),
            "CARGO_BERTH_TEST_FOREIGN_RUN": SECOND_RUN,
            "CARGO_BERTH_TEST_GIT_RACE_TRIGGER": str(
                Path(fixture.temporary_directory.name) / "collision-trigger"
            ),
        }
        return fixture

    def prepare_lost_evidence_timing_fixture(
        self,
        trunk_resolution: TrunkResolution,
        journal_age: TimingJournalAge,
        alert_cardinality: LostEvidenceAlertCardinality = LostEvidenceAlertCardinality.ONE,
    ) -> ScratchPostToolUseRepository:
        """Build released reservations whose protected commit was rewritten."""

        fixture = self.new_scratch_post_tool_use_repository(3)
        observer = self.claim_scratch_scope(
            fixture.repository_root,
            "file:observer.txt",
            FIRST_RUN,
            "fixture-session",
        )
        released_reservations: list[str] = []
        released_paths: list[Path] = []
        for alert_index in range(alert_cardinality.value):
            released_path = fixture.repository_root / f"released-{alert_index}.txt"
            released_paths.append(released_path)
            released_reservations.append(
                self.claim_scratch_scope(
                    fixture.repository_root,
                    f"file:{released_path.name}",
                    SECOND_RUN,
                )
            )
            _ = released_path.write_text(
                f"released work {alert_index}\n", encoding="utf-8"
            )
        _ = self.commit_scratch_work(fixture.repository_root, "released work")
        for released_reservation in released_reservations:
            for _ in range(3):
                _ = self.engine_command(
                    fixture.repository_root,
                    ["release", released_reservation, "--json"],
                )
        self.pad_journal_with_renewals(
            fixture, observer, FIRST_RUN, journal_age.record_count
        )
        for released_path in released_paths:
            released_path.unlink()
        _ = self.git_command(fixture.repository_root, ["add", "--all"])
        _ = self.git_command(
            fixture.repository_root,
            [
                "-c",
                "core.hooksPath=/dev/null",
                "commit",
                "--quiet",
                "--amend",
                "--allow-empty",
                "-m",
                "rewritten target",
            ],
        )
        if trunk_resolution is TrunkResolution.UNRESOLVED:
            head = self.git_command(fixture.repository_root, ["rev-parse", "HEAD"])
            _ = self.git_command(
                fixture.repository_root, ["checkout", "--quiet", "--detach", head]
            )
            _ = self.git_command(
                fixture.repository_root, ["update-ref", "-d", "refs/heads/main"]
            )
        return fixture

    def prepare_trunk_equivalence_timing_fixture(
        self,
        verdict: ScopedEquivalenceVerdict,
        durable_state: DurableProofCacheState,
        journal_age: TimingJournalAge,
    ) -> ScratchPostToolUseRepository:
        """Build one released trunk comparison with a cold or stored verdict."""

        fixture = self.new_scratch_post_tool_use_repository(3)
        subject = self.claim_scratch_scope(
            fixture.repository_root, "file:src/lib.rs", FIRST_RUN
        )
        observer_root = self.add_scratch_worktree(fixture, "trunk-observer")
        _ = self.commit_empty_scratch_work(observer_root, "observer history")
        observer = self.claim_scratch_scope(
            observer_root,
            "file:observer.txt",
            SECOND_RUN,
            "fixture-session",
        )
        fixture.hook_root = observer_root
        phase_start = self.git_command(
            fixture.repository_root, ["rev-parse", "HEAD"]
        )
        _ = (fixture.repository_root / "src/lib.rs").write_text(
            "pub fn protected() {}\n", encoding="utf-8"
        )
        protected_tip = self.commit_scratch_work(
            fixture.repository_root, "protected identity"
        )
        for _ in range(3):
            _ = self.engine_command(
                fixture.repository_root, ["release", subject, "--json"]
            )
        record_count_before_rewrite = (
            journal_age.record_count
            if durable_state is DurableProofCacheState.NOT_EVALUATED
            else journal_age.record_count - 2
        )
        self.pad_journal_with_renewals(
            fixture,
            observer,
            SECOND_RUN,
            record_count_before_rewrite,
        )
        if verdict is ScopedEquivalenceVerdict.DIFFERENT:
            _ = (fixture.repository_root / "src/lib.rs").write_text(
                "pub fn replacement() {}\n", encoding="utf-8"
            )
            _ = self.git_command(fixture.repository_root, ["add", "src/lib.rs"])
        _ = self.git_command(
            fixture.repository_root,
            [
                "-c",
                "core.hooksPath=/dev/null",
                "commit",
                "--quiet",
                "--amend",
                "-m",
                "rewritten target",
            ],
        )
        target = self.git_command(fixture.repository_root, ["rev-parse", "HEAD"])
        if durable_state is DurableProofCacheState.STORED:
            _ = self.engine_command(fixture.repository_root, ["board", "--json"])
        fixture.equivalence_object_ids = (phase_start, protected_tip, target)
        return fixture

    def prepare_successor_equivalence_timing_fixture(
        self,
        verdict: ScopedEquivalenceVerdict,
        durable_state: DurableProofCacheState,
        journal_age: TimingJournalAge,
    ) -> ScratchPostToolUseRepository:
        """Build one ordered successor comparison with a cold or stored verdict."""

        fixture = self.new_scratch_post_tool_use_repository(3)
        phase_start = self.git_command(
            fixture.repository_root, ["rev-parse", "HEAD"]
        )
        predecessor_root = self.add_scratch_worktree(fixture, "predecessor")
        successor_root = self.add_scratch_worktree(fixture, "successor")
        predecessor = self.claim_scratch_scope(
            predecessor_root, "file:src/lib.rs", FIRST_RUN
        )
        claim_arguments = [
            "claim",
            "file:src/lib.rs",
            "--run",
            SECOND_RUN,
            "--defer",
            predecessor,
            "--overlap-why",
            "the order is not known yet",
            "--why",
            "successor timing work",
            "--json",
        ]
        proposed = self.engine_command(
            successor_root,
            claim_arguments,
            expected_exit_codes=(3,),
            session_id="fixture-session",
        )
        proposal_payload = cast(dict[str, object], proposed["payload"])
        proposal_data = cast(dict[str, object], proposal_payload["data"])
        proposal_token = cast(str, proposal_data["proposal_token"])
        applying_arguments = claim_arguments[:-1] + [
            "--proposal",
            proposal_token,
            "--json",
        ]
        applied = self.engine_command(
            successor_root,
            applying_arguments,
            session_id="fixture-session",
        )
        applied_payload = cast(dict[str, object], applied["payload"])
        applied_data = cast(dict[str, object], applied_payload["data"])
        successor = cast(str, applied_data["reservation_id"])
        sequenced = self.engine_command(
            fixture.repository_root,
            [
                "sequence",
                predecessor,
                successor,
                "--why",
                "predecessor content must reach the successor",
                "--json",
            ],
        )
        self.assertEqual(sequenced["status"], "sequenced")
        _ = (predecessor_root / "src/lib.rs").write_text(
            "pub fn rewritten_predecessor() {}\n", encoding="utf-8"
        )
        protected_tip = self.commit_scratch_work(
            predecessor_root, "protected predecessor"
        )
        _ = self.engine_command(
            predecessor_root, ["release", predecessor, "--json"]
        )
        fixture.hook_root = successor_root
        rewritten_content = "pub fn rewritten_predecessor() {}\n"
        _ = (fixture.repository_root / "src/lib.rs").write_text(
            rewritten_content, encoding="utf-8"
        )
        _ = self.commit_scratch_work(
            fixture.repository_root, "rewritten trunk integration"
        )
        _ = self.engine_command(fixture.repository_root, ["board", "--json"])
        _ = self.engine_command(fixture.repository_root, ["board", "--json"])
        if durable_state is DurableProofCacheState.NOT_EVALUATED:
            self.pad_journal_with_renewals(
                fixture,
                successor,
                SECOND_RUN,
                journal_age.record_count,
            )
        if verdict is ScopedEquivalenceVerdict.EQUIVALENT:
            _ = (successor_root / "src/lib.rs").write_text(
                rewritten_content, encoding="utf-8"
            )
            successor_head = self.commit_scratch_work(
                successor_root, "successor work"
            )
        else:
            _ = (successor_root / "src/lib.rs").write_text(
                "pub fn different_successor() {}\n", encoding="utf-8"
            )
            successor_head = self.commit_scratch_work(
                successor_root, "successor work"
            )
        if durable_state is DurableProofCacheState.STORED:
            _ = self.engine_command(fixture.repository_root, ["board", "--json"])
            self.pad_journal_with_renewals(
                fixture,
                successor,
                SECOND_RUN,
                journal_age.record_count,
            )
        fixture.equivalence_object_ids = (
            phase_start,
            protected_tip,
            successor_head,
        )
        return fixture

    def prepare_identity_timing_fixture(
        self, outcome: PostToolUseOutcome, journal_age: TimingJournalAge
    ) -> ScratchPostToolUseRepository:
        """Build each real coordination-identity rejection state."""

        fixture = self.new_scratch_post_tool_use_repository(
            SHALLOW_TIMING_HISTORY_COMMIT_COUNT
        )
        if outcome is PostToolUseOutcome.STALE_SESSION_MAPPING:
            subject = self.claim_scratch_scope(
                fixture.repository_root,
                "file:session.txt",
                FIRST_RUN,
                "fixture-session",
            )
            secondary = self.claim_scratch_scope(
                fixture.repository_root, "file:secondary.txt", SECOND_RUN
            )
            mapping_path = (
                fixture.repository_root
                / ".git/cargo-berth/session-identities.json"
            )
            stale_mapping = mapping_path.read_bytes()
            self.pad_journal_with_renewals(
                fixture, secondary, SECOND_RUN, journal_age.record_count - 1
            )
            _ = self.engine_command(
                fixture.repository_root, ["release", subject, "--json"]
            )
            _ = mapping_path.write_bytes(stale_mapping)
        elif outcome is PostToolUseOutcome.STALE_MARKER_RUN:
            retired = self.claim_scratch_scope(
                fixture.repository_root, "file:retired.txt", SECOND_RUN
            )
            self.abandon_scratch_reservation(
                fixture.repository_root, retired, SECOND_RUN
            )
            subject = self.claim_scratch_scope(
                fixture.repository_root, "file:marker.txt", FIRST_RUN
            )
            self.pad_journal_with_renewals(
                fixture, subject, FIRST_RUN, journal_age.record_count
            )
            _ = (fixture.repository_root / "marker-change.txt").write_text(
                "changed during marker observation\n", encoding="utf-8"
            )
            fixture.git_race_transition = (
                GitRaceTransition.RELEASE_MARKER_RESERVATION
            )
            fixture.transition_environment = {
                "CARGO_BERTH_TEST_GIT_RACE_TRIGGER": str(
                    Path(fixture.temporary_directory.name) / "marker-trigger"
                ),
                "CARGO_BERTH_TEST_MARKER_RESERVATION": subject,
                "CARGO_BERTH_TEST_SUBJECT_RUN": FIRST_RUN,
                "CARGO_BERTH_TEST_TRANSITION_ROOT": str(
                    fixture.repository_root
                ),
            }
        else:
            subject = self.claim_scratch_scope(
                fixture.repository_root,
                "file:live-session",
                FIRST_RUN,
                "fixture-session",
            )
            retired = self.claim_scratch_scope(
                fixture.repository_root, "file:retired.txt", SECOND_RUN
            )
            self.abandon_scratch_reservation(
                fixture.repository_root, retired, SECOND_RUN
            )
            self.pad_journal_with_renewals(
                fixture, subject, FIRST_RUN, journal_age.record_count
            )
            fixture.hook_root = self.add_scratch_worktree(
                fixture, "identity-mismatch"
            )
        return fixture

    def prepare_replay_failure_timing_fixture(
        self,
        journal_age: TimingJournalAge,
    ) -> ScratchPostToolUseRepository:
        """Reorder real release and widen facts into a typed replay failure."""

        fixture = self.new_scratch_post_tool_use_repository(
            SHALLOW_TIMING_HISTORY_COMMIT_COUNT
        )
        subject = self.claim_scratch_scope(
            fixture.repository_root,
            "file:owned.txt",
            FIRST_RUN,
            "fixture-session",
        )
        _ = self.claim_scratch_scope(
            fixture.repository_root, "file:secondary.txt", SECOND_RUN
        )
        self.pad_journal_with_renewals(
            fixture, subject, FIRST_RUN, journal_age.record_count - 2
        )
        _ = (fixture.repository_root / "outside.txt").write_text(
            "widen before release\n", encoding="utf-8"
        )
        widened = self.engine_command(
            fixture.repository_root,
            ["drift", "--full", "--reservation", subject, "--json"],
            session_id="fixture-session",
        )
        self.assertEqual(widened["status"], "widened")
        self.abandon_scratch_reservation(
            fixture.repository_root, subject, FIRST_RUN
        )
        journal_path = fixture.repository_root / ".git/cargo-berth/journal.ndjson"
        lines = journal_path.read_text(encoding="utf-8").splitlines()
        widen_record = cast(dict[str, object], json.loads(lines[-2]))
        release_record = cast(dict[str, object], json.loads(lines[-1]))
        self.assertEqual(widen_record["op"], "widen")
        self.assertEqual(release_record["op"], "release")
        lines[-2:] = [lines[-1], lines[-2]]
        journal_bytes = ("\n".join(lines) + "\n").encode()
        _ = journal_path.write_bytes(journal_bytes)
        projection_path = (
            fixture.repository_root / ".git/cargo-berth/reservations.json"
        )
        projection = cast(
            dict[str, object],
            json.loads(projection_path.read_text(encoding="utf-8")),
        )
        projection["generation"] = widen_record["projection_generation"]
        projection["journal_end_offset"] = len(journal_bytes)
        projection["journal_fingerprint"] = self.journal_fingerprint(
            journal_bytes
        )
        _ = projection_path.write_text(
            json.dumps(projection, indent=2) + "\n", encoding="utf-8"
        )
        return fixture

    def prepare_post_tool_use_timing_fixture(
        self,
        outcome: PostToolUseOutcome,
        durable_state: DurableProofCacheState,
        journal_age: TimingJournalAge,
    ) -> ScratchPostToolUseRepository:
        """Construct the real repository state that produces one timing row."""

        match outcome:
            case PostToolUseOutcome.TYPED_CLEAR | PostToolUseOutcome.ORDINARY_WIDEN | PostToolUseOutcome.FIRST_TOUCH_ACQUISITION:
                return self.prepare_basic_timing_fixture(outcome, journal_age)
            case PostToolUseOutcome.FOREIGN_ONLY_INCURSION | PostToolUseOutcome.RECORDED_INCURSION | PostToolUseOutcome.UNREADABLE_JOURNAL | PostToolUseOutcome.DUPLICATE_INCURSION_INCIDENT:
                return self.prepare_incursion_timing_fixture(outcome, journal_age)
            case PostToolUseOutcome.POST_WRITE_INCURSION_ACQUIRED:
                return self.prepare_post_write_incursion_timing_fixture(
                    PostWriteProtection.ACQUIRED, journal_age
                )
            case PostToolUseOutcome.POST_WRITE_INCURSION_NOT_ACQUIRED:
                return self.prepare_post_write_incursion_timing_fixture(
                    PostWriteProtection.NOT_ACQUIRED, journal_age
                )
            case PostToolUseOutcome.COLLISION:
                return self.prepare_collision_timing_fixture(journal_age)
            case PostToolUseOutcome.ATTRIBUTION:
                return self.prepare_attribution_timing_fixture(journal_age)
            case PostToolUseOutcome.LOST_EVIDENCE_RESOLVED_TRUNK:
                return self.prepare_lost_evidence_timing_fixture(
                    TrunkResolution.RESOLVED, journal_age
                )
            case PostToolUseOutcome.LOST_EVIDENCE_UNRESOLVED_TRUNK:
                return self.prepare_lost_evidence_timing_fixture(
                    TrunkResolution.UNRESOLVED, journal_age
                )
            case PostToolUseOutcome.TRUNK_EQUIVALENCE_POSITIVE:
                return self.prepare_trunk_equivalence_timing_fixture(
                    ScopedEquivalenceVerdict.EQUIVALENT, durable_state, journal_age
                )
            case PostToolUseOutcome.TRUNK_EQUIVALENCE_NEGATIVE:
                return self.prepare_trunk_equivalence_timing_fixture(
                    ScopedEquivalenceVerdict.DIFFERENT, durable_state, journal_age
                )
            case PostToolUseOutcome.SUCCESSOR_EQUIVALENCE_POSITIVE:
                return self.prepare_successor_equivalence_timing_fixture(
                    ScopedEquivalenceVerdict.EQUIVALENT, durable_state, journal_age
                )
            case PostToolUseOutcome.SUCCESSOR_EQUIVALENCE_NEGATIVE:
                return self.prepare_successor_equivalence_timing_fixture(
                    ScopedEquivalenceVerdict.DIFFERENT, durable_state, journal_age
                )
            case PostToolUseOutcome.STALE_SESSION_MAPPING | PostToolUseOutcome.STALE_MARKER_RUN | PostToolUseOutcome.SESSION_WORKTREE_MISMATCH:
                return self.prepare_identity_timing_fixture(outcome, journal_age)
            case PostToolUseOutcome.TYPED_REPLAY_FAILURE:
                return self.prepare_replay_failure_timing_fixture(journal_age)

    def prepare_history_timing_fixture(
        self,
        history_measurement: PublishedHistoryMeasurement,
        subject_cardinality: HistorySubjectCardinality,
    ) -> ScratchPostToolUseRepository:
        """Build active subjects over one exact commit topology."""

        fixture = self.new_scratch_post_tool_use_repository(
            history_measurement.base_commit_count
        )
        subject_roots = [fixture.repository_root]
        for subject_index in range(1, subject_cardinality.value):
            subject_roots.append(
                self.add_scratch_worktree(
                    fixture, f"history-subject-{subject_index}"
                )
            )
        first_reservation = ""
        first_run = ""
        for subject_index, subject_root in enumerate(subject_roots):
            for commit_index in range(history_measurement.commits_per_subject):
                _ = self.commit_empty_scratch_work(
                    subject_root,
                    f"{history_measurement.profile.value} subject {subject_index} commit {commit_index}",
                )
            coordination_run_id = following_fixture_uuid_v7(
                FIRST_RUN, subject_index + 100
            )
            reservation_id = self.claim_scratch_scope(
                subject_root,
                f"file:history-subject-{subject_index}.txt",
                coordination_run_id,
                "fixture-session" if subject_index == 0 else "",
            )
            if subject_index == 0:
                first_reservation = reservation_id
                first_run = coordination_run_id
        self.pad_journal_with_renewals(
            fixture,
            first_reservation,
            first_run,
            WORKING_REPOSITORY_JOURNAL_RECORD_COUNT,
        )
        return fixture

    def prepare_incident_cardinality_timing_fixture(
        self,
        incident_cardinality: IncursionIncidentCardinality,
        retained_state: RetainedIncursionState,
    ) -> ScratchPostToolUseRepository:
        """Build a fixed-reservation board with an exact retained-incident count."""

        fixture = self.new_scratch_post_tool_use_repository(
            SHALLOW_TIMING_HISTORY_COMMIT_COUNT
        )
        subject = self.claim_scratch_scope(
            fixture.repository_root,
            "file:owned.txt",
            FIRST_RUN,
            "fixture-session",
        )
        foreign_root = self.add_scratch_worktree(fixture, "incident-holder")
        _ = self.claim_scratch_scope(foreign_root, "tree:shared", SECOND_RUN)
        shared_root = fixture.repository_root / "shared"
        shared_root.mkdir()
        for incident_index in range(incident_cardinality.value):
            _ = (shared_root / f"entered-{incident_index}.txt").write_text(
                f"incursion {incident_index}\n", encoding="utf-8"
            )
            observed_incursion = self.engine_command(
                fixture.repository_root,
                ["drift", "--full", "--reservation", subject, "--json"],
                expected_exit_codes=(1,),
                session_id="fixture-session",
            )
            self.assertEqual(observed_incursion["status"], "incursion")
        incursion_records = [
            record
            for record in self.journal_records(fixture)
            if record["op"] == "incursion"
        ]
        self.assertEqual(len(incursion_records), incident_cardinality.value)
        if retained_state is RetainedIncursionState.RESOLVED:
            _ = self.engine_command(
                fixture.repository_root,
                ["resolve", subject, "--every-incursion", "--json"],
                coordination_run_id=FIRST_RUN,
            )
            resolution_count = sum(
                record["op"] == "resolve_incursion"
                for record in self.journal_records(fixture)
            )
            self.assertEqual(resolution_count, incident_cardinality.value)
        self.pad_journal_with_renewals(
            fixture,
            subject,
            FIRST_RUN,
            WORKING_REPOSITORY_JOURNAL_RECORD_COUNT,
        )
        return fixture

    def post_tool_use_timing_cells(self) -> list[PostToolUseTimingCell]:
        no_proof = (DurableProofCacheState.NOT_APPLICABLE,)
        proof_states = (
            DurableProofCacheState.NOT_EVALUATED,
            DurableProofCacheState.STORED,
        )
        return [
            PostToolUseTimingCell(PostToolUseOutcome.TYPED_REPLAY_FAILURE, no_proof, 0, 0, 0, "widen_requires_unreleased", 0),
            PostToolUseTimingCell(PostToolUseOutcome.TYPED_CLEAR, no_proof, 0, 0, 0, "", 6),
            PostToolUseTimingCell(PostToolUseOutcome.ORDINARY_WIDEN, no_proof, 0, 0, 0, "AUTO-WIDEN", 6),
            PostToolUseTimingCell(PostToolUseOutcome.FIRST_TOUCH_ACQUISITION, no_proof, 0, 0, 0, "FIRST-TOUCH CLAIM", 3),
            PostToolUseTimingCell(PostToolUseOutcome.FOREIGN_ONLY_INCURSION, no_proof, 0, 0, 0, "INCURSION", 9),
            PostToolUseTimingCell(PostToolUseOutcome.RECORDED_INCURSION, no_proof, 0, 0, 0, "", 9),
            PostToolUseTimingCell(PostToolUseOutcome.UNREADABLE_JOURNAL, no_proof, 0, 0, 0, "could not read the reservation ledger", 0),
            PostToolUseTimingCell(PostToolUseOutcome.DUPLICATE_INCURSION_INCIDENT, no_proof, 0, 0, 0, "REPLAY HARD STOP: duplicate_incursion_incident", 0),
            PostToolUseTimingCell(PostToolUseOutcome.POST_WRITE_INCURSION_ACQUIRED, no_proof, 0, 0, 0, "First-touch reservation", 3),
            PostToolUseTimingCell(PostToolUseOutcome.POST_WRITE_INCURSION_NOT_ACQUIRED, no_proof, 0, 0, 0, "nothing was reserved", 3),
            PostToolUseTimingCell(PostToolUseOutcome.COLLISION, no_proof, 0, 0, 0, "COLLISION", 6),
            PostToolUseTimingCell(PostToolUseOutcome.ATTRIBUTION, no_proof, 1, 1, 0, "Committed by", 12),
            PostToolUseTimingCell(PostToolUseOutcome.LOST_EVIDENCE_RESOLVED_TRUNK, no_proof, 0, 0, 0, "--integrated-as", 13),
            PostToolUseTimingCell(PostToolUseOutcome.LOST_EVIDENCE_UNRESOLVED_TRUNK, no_proof, 0, 0, 0, "Resolve trunk first", 9),
            PostToolUseTimingCell(PostToolUseOutcome.TRUNK_EQUIVALENCE_POSITIVE, proof_states, 0, 0, 7, "", 13),
            PostToolUseTimingCell(PostToolUseOutcome.TRUNK_EQUIVALENCE_NEGATIVE, proof_states, 0, 0, 6, "INTEGRATION EVIDENCE LOST", 12),
            PostToolUseTimingCell(PostToolUseOutcome.SUCCESSOR_EQUIVALENCE_POSITIVE, proof_states, 0, 0, 7, "", 14),
            PostToolUseTimingCell(PostToolUseOutcome.SUCCESSOR_EQUIVALENCE_NEGATIVE, proof_states, 0, 0, 6, "", 13),
            PostToolUseTimingCell(PostToolUseOutcome.STALE_SESSION_MAPPING, no_proof, 0, 0, 0, "stale_session_mapping", 4),
            PostToolUseTimingCell(PostToolUseOutcome.STALE_MARKER_RUN, no_proof, 0, 0, 0, "stale_marker_run", 6),
            PostToolUseTimingCell(PostToolUseOutcome.SESSION_WORKTREE_MISMATCH, no_proof, 0, 0, 0, "session_worktree_mismatch", 2),
        ]

    def assert_installed_engine_preflight(self) -> None:
        """Prove the selected binary writes `identity_inputs.status = "recorded"`."""

        fixture = self.new_scratch_post_tool_use_repository(2)
        try:
            _ = self.claim_scratch_scope(
                fixture.repository_root,
                "file:preflight.txt",
                FIRST_RUN,
                "fixture-session",
            )
            claim_record = self.journal_records(fixture)[0]
            identity_inputs = cast(
                dict[str, object], claim_record["identity_inputs"]
            )
            self.assertEqual(identity_inputs["status"], "recorded")
        finally:
            fixture.cleanup()

    def assert_installed_timing_artifacts_unchanged(self, activity: str) -> None:
        """Fail if an installer replaced any timed artifact during this run."""

        self.assertEqual(
            installed_timing_artifact_digests(),
            self.installed_artifact_digests,
            f"the global cargo-berth installation changed {activity}; discard every timing sample",
        )

    def assert_timing_repository_contract(
        self,
        fixture: ScratchPostToolUseRepository,
        contract: TimingRepositoryContract,
    ) -> None:
        """Assert every repository dimension named by the published bound."""

        projection_path = (
            fixture.repository_root / ".git/cargo-berth/reservations.json"
        )
        projection = cast(
            dict[str, object], json.loads(projection_path.read_text(encoding="utf-8"))
        )
        self.assertEqual(
            projection["schema_version"], CURRENT_PROJECTION_SCHEMA_VERSION
        )
        records = self.journal_records(fixture)
        journal_path = fixture.repository_root / ".git/cargo-berth/journal.ndjson"
        journal_bytes = journal_path.read_bytes()
        self.assertEqual(len(records), contract.journal_record_count)
        self.assertEqual(
            projection["generation"], records[-1]["projection_generation"]
        )
        self.assertEqual(projection["journal_end_offset"], len(journal_bytes))
        self.assertEqual(
            projection["journal_fingerprint"],
            self.journal_fingerprint(journal_bytes),
        )
        retained_reservations = {
            cast(str, record["reservation_id"])
            for record in records
            if record["op"] == "claim"
        }
        self.assertEqual(
            len(retained_reservations),
            contract.retained_reservation_count,
        )
        history_commit_count = int(
            self.git_command(fixture.hook_root, ["rev-list", "--count", "HEAD"])
        )
        self.assertEqual(history_commit_count, contract.head_commit_count)

    def assert_history_repository_contract(
        self,
        fixture: ScratchPostToolUseRepository,
        history_measurement: PublishedHistoryMeasurement,
        subject_cardinality: HistorySubjectCardinality,
    ) -> None:
        """Assert the journal, subject, and commit sizes of one history row."""

        self.assert_timing_repository_contract(
            fixture,
            TimingRepositoryContract(
                journal_record_count=WORKING_REPOSITORY_JOURNAL_RECORD_COUNT,
                retained_reservation_count=subject_cardinality.value,
                head_commit_count=history_measurement.head_commit_count(),
            ),
        )
        all_refs_commit_count = int(
            self.git_command(fixture.hook_root, ["rev-list", "--all", "--count"])
        )
        self.assertEqual(
            all_refs_commit_count,
            history_measurement.all_refs_commit_count(subject_cardinality),
        )

    @staticmethod
    def normalized_git_call(call: list[str]) -> list[str]:
        """Remove the engine's fixed read-only git prefix from one trace row."""

        if call and call[0] == "--no-optional-locks":
            return call[1:]
        return call

    def canonical_git_command_sequence(
        self, invocation: HookInvocation
    ) -> tuple[str, ...]:
        """Canonicalize concurrent command names from the raw git argv trace."""

        commands: list[str] = []
        for call in invocation.git_calls:
            normalized_call = self.normalized_git_call(call)
            if normalized_call:
                commands.append(normalized_call[0])
        commands.sort()
        return tuple(commands)

    def observed_provenance_log_argv(self, invocation: HookInvocation) -> int:
        """Count the batched incursion path-log invocation."""

        return sum(
            self.normalized_git_call(call)[:1] == ["log"]
            and any(
                "cargo-berth-incursion-commit" in argument
                for argument in self.normalized_git_call(call)
            )
            for call in invocation.git_calls
        )

    def observed_provenance_rev_list_argv(
        self, invocation: HookInvocation
    ) -> int:
        """Count the batched incursion range-membership invocation."""

        if self.observed_provenance_log_argv(invocation) == 0:
            return 0
        return sum(
            self.normalized_git_call(call)
            == ["rev-list", "--ignore-missing", "--parents", "--stdin"]
            for call in invocation.git_calls
        )

    def observed_equivalence_git_argv(
        self,
        invocation: HookInvocation,
        equivalence_object_ids: tuple[str, ...],
    ) -> int:
        """Count one scoped-patch comparison without relying on worker order."""

        if not equivalence_object_ids:
            return 0
        equivalence_commands = {
            "cat-file",
            "diff",
            "log",
            "merge-base",
            "merge-tree",
            "read-tree",
            "rev-list",
            "update-index",
            "write-tree",
        }
        normalized_calls = [
            self.normalized_git_call(raw_call)
            for raw_call in invocation.git_calls
        ]
        scoped_merge = any(
            bool(call)
            and call[0] == "merge-tree"
            and all(
                any(object_id in argument for argument in call)
                for object_id in equivalence_object_ids
            )
            for call in normalized_calls
        )
        if not scoped_merge:
            return 0
        scoped_batch_check = any(
            call
            == ["cat-file", "--batch-check=%(objectname) %(objecttype)"]
            for call in normalized_calls
        )
        object_bound_calls = sum(
            bool(call)
            and call[0] in equivalence_commands
            and call
            != ["cat-file", "--batch-check=%(objectname) %(objecttype)"]
            and (
                call[0] in {"update-index", "write-tree"}
                or (
                    len(call) == 3
                    and call[:2] == ["rev-list", "--parents"]
                    and call[2].startswith("refs/heads/")
                )
                or any(
                    object_id in argument
                    for object_id in equivalence_object_ids
                    for argument in call
                )
            )
            for call in normalized_calls
        )
        return int(scoped_batch_check) + object_bound_calls

    @staticmethod
    def rendered_post_tool_use_detail(rendered: str) -> str:
        """Read the hook's additional context from one JSON response."""

        if not rendered:
            return ""
        decoded = cast(dict[str, object], json.loads(rendered))
        hook_output = cast(dict[str, object], decoded["hookSpecificOutput"])
        return cast(str, hook_output["additionalContext"])

    @staticmethod
    def rendered_recovery_argv(rendered: str) -> list[list[str]]:
        """Parse every backtick-delimited recovery command from hook output."""

        detail = InstalledFrontEndFixture.rendered_post_tool_use_detail(rendered)
        command_texts = detail.split("`")[1::2]
        commands: list[list[str]] = []
        for command_text in command_texts:
            shell_words = shlex.split(command_text)
            if "&&" not in shell_words:
                continue
            separator_index = shell_words.index("&&")
            commands.append(shell_words[separator_index + 1 :])
        return commands

    @staticmethod
    def median_timing_sample(samples: list[float]) -> float:
        """Return the middle of the fixed five-sample timing set."""

        return sorted(samples)[len(samples) // 2]

    @staticmethod
    def record_post_tool_use_sample_consistency_mismatch(
        cell: PostToolUseTimingCell,
        durable_state: DurableProofCacheState,
        journal_age: TimingJournalAge,
        process_cache_temperature: ProcessCacheTemperature,
        observation_contract: PostToolUseObservationContract,
        observed_sample_values: list[int],
        sample_process_evidence: list[PostToolUseSampleProcessEvidence],
        observation_contract_mismatches: list[
            PostToolUseObservationContractMismatch
        ],
    ) -> None:
        """Collect one inconsistent sample dimension without aborting the matrix."""

        distinct_value_count = len(set(observed_sample_values))
        if distinct_value_count == 1:
            return
        observation_contract_mismatches.append(
            PostToolUseObservationContractMismatch(
                outcome=cell.outcome,
                journal_age=journal_age,
                process_cache_temperature=process_cache_temperature,
                durable_proof_cache_state=durable_state,
                observation_contract=observation_contract,
                expected_value=1,
                observed_value=distinct_value_count,
                observed_sample_values=tuple(observed_sample_values),
                sample_process_evidence=tuple(sample_process_evidence),
            )
        )

    def measure_fixed_cost_attribution_temperature(
        self,
        measurement_run: int,
        process_cache_temperature: ProcessCacheTemperature,
        interpreter_only_hook: Path,
        engine_path_lookup_hook: Path,
        measured_zero_git_intercept_seconds: float,
        intercept_observation_count: int,
        fitted_git_process_seconds: float,
        attribution_mismatches: list[PostToolUseAttributionMismatch],
    ) -> dict[str, object]:
        """Measure named costs independently from the production-route intercept."""

        cumulative_samples: dict[str, list[float]] = {
            "shim_interpreter_startup": [],
            "shim_engine_path_lookup": [],
            "shim_real_engine_replay_route": [],
            "engine_startup": [],
            "engine_unconfigured_repository": [],
            "engine_replayed_ledger": [],
            "engine_post_tool_use_replay_route": [],
            "engine_post_tool_use_clear_route": [],
            "engine_post_tool_use_widen_route": [],
        }

        def collect_sample() -> None:
            cumulative_samples["engine_startup"].append(
                self.run_installed_engine_probe(
                    self.repository_root,
                    ["--version"],
                    0,
                    process_cache_temperature,
                )
            )

            unconfigured_fixture = self.new_scratch_post_tool_use_repository(2)
            try:
                configuration_path = (
                    unconfigured_fixture.repository_root
                    / ".claude/config/berth.toml"
                )
                configuration_path.unlink()
                cumulative_samples["engine_unconfigured_repository"].append(
                    self.run_installed_engine_probe(
                        unconfigured_fixture.repository_root,
                        ["drift", "--json"],
                        4,
                        process_cache_temperature,
                    )
                )
            finally:
                unconfigured_fixture.cleanup()

            replay_fixture = self.prepare_replay_failure_timing_fixture(
                TimingJournalAge.WORKING_REPOSITORY
            )
            try:
                interpreter = self.run_installed_engine_hook(
                    replay_fixture,
                    process_cache_temperature,
                    hook=interpreter_only_hook,
                )
                self.assertEqual(interpreter.process.returncode, 0)
                self.assertEqual(interpreter.cargo_berth_calls, [])
                self.assertEqual(interpreter.git_calls, [])
                self.assertEqual(interpreter.jq_call_count, 0)
                cumulative_samples["shim_interpreter_startup"].append(
                    interpreter.elapsed_seconds
                )
                engine_path_lookup = self.run_installed_engine_hook(
                    replay_fixture,
                    process_cache_temperature,
                    hook=engine_path_lookup_hook,
                )
                self.assertEqual(engine_path_lookup.process.returncode, 0)
                self.assertEqual(engine_path_lookup.cargo_berth_calls, [])
                self.assertEqual(engine_path_lookup.git_calls, [])
                self.assertEqual(engine_path_lookup.jq_call_count, 0)
                cumulative_samples["shim_engine_path_lookup"].append(
                    engine_path_lookup.elapsed_seconds
                )
                cumulative_samples["engine_replayed_ledger"].append(
                    self.run_installed_engine_probe(
                        replay_fixture.repository_root,
                        ["drift", "--json"],
                        4,
                        process_cache_temperature,
                    )
                )
                post_tool_use_engine_replay = (
                    self.run_installed_engine_post_tool_use_probe(
                        replay_fixture.repository_root,
                        process_cache_temperature,
                    )
                )
                cumulative_samples["engine_post_tool_use_replay_route"].append(
                    post_tool_use_engine_replay.elapsed_seconds
                )
                clear_fixture = self.prepare_basic_timing_fixture(
                    PostToolUseOutcome.TYPED_CLEAR,
                    TimingJournalAge.WORKING_REPOSITORY,
                )
                try:
                    cumulative_samples["engine_post_tool_use_clear_route"].append(
                        self.run_installed_engine_post_tool_use_elapsed_probe(
                            clear_fixture.repository_root,
                            process_cache_temperature,
                        )
                    )
                finally:
                    clear_fixture.cleanup()

                widen_fixture = self.prepare_basic_timing_fixture(
                    PostToolUseOutcome.ORDINARY_WIDEN,
                    TimingJournalAge.WORKING_REPOSITORY,
                )
                try:
                    cumulative_samples["engine_post_tool_use_widen_route"].append(
                        self.run_installed_engine_post_tool_use_elapsed_probe(
                            widen_fixture.repository_root,
                            process_cache_temperature,
                        )
                    )
                finally:
                    widen_fixture.cleanup()

                real_engine_replay_route = self.run_installed_engine_hook(
                    replay_fixture,
                    process_cache_temperature,
                )
                self.assertEqual(real_engine_replay_route.process.returncode, 0)
                self.assertEqual(real_engine_replay_route.git_calls, [])
                self.assertEqual(
                    real_engine_replay_route.cargo_berth_calls,
                    [["hook", "post-tool-use"]],
                )
                self.assertIn(
                    "REPLAY HARD STOP",
                    real_engine_replay_route.process.stdout
                    + real_engine_replay_route.process.stderr,
                )
                cumulative_samples["shim_real_engine_replay_route"].append(
                    real_engine_replay_route.elapsed_seconds
                )
            finally:
                replay_fixture.cleanup()

        if (
            process_cache_temperature
            is ProcessCacheTemperature.WARMED_EXECUTABLE_PAGES
        ):
            collect_sample()
            for samples in cumulative_samples.values():
                samples.clear()
        for _ in range(POST_TOOL_USE_SAMPLE_COUNT):
            collect_sample()

        cumulative = {
            name: self.median_timing_sample(samples)
            for name, samples in cumulative_samples.items()
        }

        engine_startup_seconds = cumulative["engine_startup"]
        repository_discovery_seconds = (
            cumulative["engine_unconfigured_repository"] - engine_startup_seconds
        )
        ledger_replay_seconds = (
            cumulative["engine_replayed_ledger"]
            - cumulative["engine_unconfigured_repository"]
        )
        engine_path_lookup_seconds = (
            cumulative["shim_engine_path_lookup"]
            - cumulative["shim_interpreter_startup"]
        )
        shim_interpreter_startup_seconds = cumulative["shim_interpreter_startup"]
        shim_engine_exec_and_payload_transport_seconds = (
            cumulative["shim_real_engine_replay_route"]
            - cumulative["shim_engine_path_lookup"]
            - cumulative["engine_post_tool_use_replay_route"]
        )
        engine_post_replay_observation_mutation_and_response_seconds = (
            cumulative["engine_post_tool_use_widen_route"]
            - cumulative["engine_post_tool_use_clear_route"]
        )
        components = {
            "shim_interpreter_startup": shim_interpreter_startup_seconds,
            "engine_path_lookup": engine_path_lookup_seconds,
            "engine_process_startup_and_dynamic_linking": engine_startup_seconds,
            "repository_discovery_before_first_git": repository_discovery_seconds,
            "ledger_open_and_journal_replay": ledger_replay_seconds,
            "shim_engine_exec_and_payload_transport": shim_engine_exec_and_payload_transport_seconds,
            "engine_post_replay_observation_mutation_and_response": engine_post_replay_observation_mutation_and_response_seconds,
        }
        component_route_coefficients = {
            "shim_interpreter_startup": {
                "shim_interpreter_startup": 1,
            },
            "engine_path_lookup": {
                "shim_engine_path_lookup": 1,
                "shim_interpreter_startup": -1,
            },
            "engine_process_startup_and_dynamic_linking": {
                "engine_startup": 1,
            },
            "repository_discovery_before_first_git": {
                "engine_unconfigured_repository": 1,
                "engine_startup": -1,
            },
            "ledger_open_and_journal_replay": {
                "engine_replayed_ledger": 1,
                "engine_unconfigured_repository": -1,
            },
            "shim_engine_exec_and_payload_transport": {
                "shim_real_engine_replay_route": 1,
                "shim_engine_path_lookup": -1,
                "engine_post_tool_use_replay_route": -1,
            },
            "engine_post_replay_observation_mutation_and_response": {
                "engine_post_tool_use_widen_route": 1,
                "engine_post_tool_use_clear_route": -1,
            },
        }
        component_sum_route_coefficients: dict[str, int] = {}
        for route_coefficients in component_route_coefficients.values():
            for route, coefficient in route_coefficients.items():
                component_sum_route_coefficients[route] = (
                    component_sum_route_coefficients.get(route, 0) + coefficient
                )
        component_sum_route_coefficients = {
            route: coefficient
            for route, coefficient in component_sum_route_coefficients.items()
            if coefficient != 0
        }
        self.assertEqual(
            component_sum_route_coefficients,
            {
                "engine_replayed_ledger": 1,
                "shim_real_engine_replay_route": 1,
                "engine_post_tool_use_replay_route": -1,
                "engine_post_tool_use_widen_route": 1,
                "engine_post_tool_use_clear_route": -1,
            },
            "fixed-cost components collapsed to an intercept input instead of seven independently measured routes",
        )
        attributed_seconds = sum(components.values())
        attribution_error_fraction = abs(
            attributed_seconds - measured_zero_git_intercept_seconds
        ) / measured_zero_git_intercept_seconds
        if (
            attribution_error_fraction
            > FIXED_COST_ATTRIBUTION_TOLERANCE_FRACTION
        ):
            attribution_mismatches.append(
                PostToolUseAttributionMismatch(
                    process_cache_temperature=process_cache_temperature,
                    measurement_run=measurement_run,
                    measured_zero_git_intercept_seconds=measured_zero_git_intercept_seconds,
                    component_sum_seconds=attributed_seconds,
                    attribution_error_fraction=attribution_error_fraction,
                    components=components,
                    cumulative_routes=cumulative,
                )
            )
        return {
            "measurement_run": measurement_run,
            "process_cache_temperature": process_cache_temperature.value,
            "sample_statistic": "median_of_five_independently_restored_samples",
            "components": components,
            "component_sum_seconds": attributed_seconds,
            "measured_zero_git_intercept_seconds": measured_zero_git_intercept_seconds,
            "intercept_observation_count": intercept_observation_count,
            "fitted_git_process_seconds": fitted_git_process_seconds,
            "attribution_error_fraction": attribution_error_fraction,
            "allocation_method": "independent cumulative probes checked against a linear zero-Git intercept fitted from the median production outcome-matrix routes",
            "ledger_probe_boundary": "configured ledger open and replay are isolated by subtracting the unconfigured repository route; the replay-error exit is not used as the intercept",
            "shim_engine_boundary_probe": "the canonical PostToolUse wrapper invoking the real engine for a working-journal replay failure, minus a stand-in script that starts Bash and resolves cargo-berth on PATH without executing it, minus the standalone engine executing the identical hook post-tool-use request",
            "engine_post_replay_mutation_probe": "the standalone ordinary-widen PostToolUse route minus the standalone typed-clear PostToolUse route over separately restored two-reservation, 214-record fixtures; both routes execute the same six Git command families, so their signed delta isolates changed-path observation, widening, mutation-lock, durable-append, response-construction, and route-completion work after replay",
            "component_sum_symbolic_reduction": component_sum_route_coefficients,
            "negative_median_delta_resolution": "none; signed independently measured differences remain visible",
            "cumulative_routes": cumulative,
        }

    @staticmethod
    def production_zero_git_intercept(
        ready_results: list[dict[str, object]],
        process_cache_temperature: ProcessCacheTemperature,
    ) -> tuple[float, int, float]:
        """Fit the zero-Git intercept across real ready-state hook routes."""

        observations = [
            (
                float(cast(int, result["git_argv"])),
                cast(float, result["median_wall_seconds"]),
            )
            for result in ready_results
            if result["process_cache_temperature"]
            == process_cache_temperature.value
            and result.get("outcome") != PostToolUseOutcome.TYPED_REPLAY_FAILURE.value
        ]
        mean_git = sum(git_count for git_count, _ in observations) / len(observations)
        mean_wall = sum(wall for _, wall in observations) / len(observations)
        git_variance = sum(
            (git_count - mean_git) ** 2 for git_count, _ in observations
        )
        if git_variance == 0:
            raise AssertionError("production routes do not vary their Git process counts")
        fitted_git_process_seconds = sum(
            (git_count - mean_git) * (wall - mean_wall)
            for git_count, wall in observations
        ) / git_variance
        return (
            mean_wall - fitted_git_process_seconds * mean_git,
            len(observations),
            fitted_git_process_seconds,
        )

    def measure_fixed_cost_attribution(
        self,
        ready_results: list[dict[str, object]],
        attribution_mismatches: list[PostToolUseAttributionMismatch],
    ) -> dict[str, object]:
        """Measure every named zero-Git component at both cache temperatures."""

        interpreter_only_hook = self.fixture_root / "post-bash-interpreter-only.sh"
        _ = interpreter_only_hook.write_text(
            "#!/usr/bin/env bash\nset -u\nexit 0\n",
            encoding="utf-8",
        )
        interpreter_only_hook.chmod(0o755)
        engine_path_lookup_hook = (
            self.fixture_root / "post-bash-engine-path-lookup.sh"
        )
        _ = engine_path_lookup_hook.write_text(
            "#!/usr/bin/env bash\nset -u\ncommand -v cargo-berth >/dev/null 2>&1\n",
            encoding="utf-8",
        )
        engine_path_lookup_hook.chmod(0o755)
        intercepts = {
            process_cache_temperature: self.production_zero_git_intercept(
                ready_results, process_cache_temperature
            )
            for process_cache_temperature in PUBLISHED_POST_TOOL_USE_BOUND.measured_process_cache_temperatures
        }
        self.assert_installed_timing_artifacts_unchanged(
            "between test-class setup and the fixed-cost ladder"
        )
        temperature_results_by_run: list[list[dict[str, object]]] = []
        measurement_runs: list[dict[str, object]] = []
        for measurement_run in (1, 2):
            self.assert_installed_timing_artifacts_unchanged(
                f"before fixed-cost measurement run {measurement_run}"
            )
            temperatures = [
                self.measure_fixed_cost_attribution_temperature(
                    measurement_run,
                    process_cache_temperature,
                    interpreter_only_hook,
                    engine_path_lookup_hook,
                    *intercepts[process_cache_temperature],
                    attribution_mismatches,
                )
                for process_cache_temperature in PUBLISHED_POST_TOOL_USE_BOUND.measured_process_cache_temperatures
            ]
            self.assert_installed_timing_artifacts_unchanged(
                f"during fixed-cost measurement run {measurement_run}"
            )
            temperature_results_by_run.append(temperatures)
            measurement_runs.append(
                {
                    "measurement_run": measurement_run,
                    "number_origin": "freshly measured cumulative routes and seven signed components",
                    "temperatures": temperatures,
                }
            )

        component_comparisons: dict[str, dict[str, dict[str, object]]] = {}
        unresolved_components: list[dict[str, str]] = []
        for temperature_index, process_cache_temperature in enumerate(
            PUBLISHED_POST_TOOL_USE_BOUND.measured_process_cache_temperatures
        ):
            first_temperature = temperature_results_by_run[0][temperature_index]
            second_temperature = temperature_results_by_run[1][temperature_index]
            first_components = cast(
                dict[str, float], first_temperature["components"]
            )
            second_components = cast(
                dict[str, float], second_temperature["components"]
            )
            self.assertEqual(first_components.keys(), second_components.keys())
            temperature_comparisons: dict[str, dict[str, object]] = {}
            for component_name in first_components:
                first_seconds = first_components[component_name]
                second_seconds = second_components[component_name]
                spread_seconds = abs(second_seconds - first_seconds)
                exceeds_first_value = spread_seconds > abs(first_seconds)
                exceeds_second_value = spread_seconds > abs(second_seconds)
                resolved = not (exceeds_first_value or exceeds_second_value)
                if not resolved:
                    unresolved_components.append(
                        {
                            "process_cache_temperature": process_cache_temperature.value,
                            "component": component_name,
                        }
                    )
                temperature_comparisons[component_name] = {
                    "run_1_seconds": first_seconds,
                    "run_2_seconds": second_seconds,
                    "spread_seconds": spread_seconds,
                    "spread_exceeds_run_1_absolute_value": exceeds_first_value,
                    "spread_exceeds_run_2_absolute_value": exceeds_second_value,
                    "measurement_status": (
                        "resolved_at_five_samples"
                        if resolved
                        else "not_resolved_at_five_samples"
                    ),
                }
            component_comparisons[process_cache_temperature.value] = (
                temperature_comparisons
            )

        all_attributions_within_tolerance = all(
            cast(float, temperature["attribution_error_fraction"])
            <= FIXED_COST_ATTRIBUTION_TOLERANCE_FRACTION
            for temperatures in temperature_results_by_run
            for temperature in temperatures
        )
        if unresolved_components:
            attribution_validation = (
                "not_validated_five_samples_do_not_resolve_every_component"
            )
        elif all_attributions_within_tolerance:
            attribution_validation = "validated"
        else:
            attribution_validation = "not_validated_component_sum_misses_gate"
        return {
            "measurement_kind": "fixed_cost_attribution",
            "zero_git_route": "linear intercept across production outcome-matrix routes",
            "installation_state": PUBLISHED_POST_TOOL_USE_BOUND.installation_state.value,
            "installed_artifact_sha256": self.installed_artifact_digests,
            "fresh_measurement_runs": measurement_runs,
            "replayed_prior_matrix_intercepts": {
                process_cache_temperature.value: {
                    "number_origin": "replayed from the preceding outcome matrix measured before both ladder runs",
                    "measured_zero_git_intercept_seconds": intercept[0],
                    "intercept_observation_count": intercept[1],
                    "fitted_git_process_seconds": intercept[2],
                }
                for process_cache_temperature, intercept in intercepts.items()
            },
            "component_comparisons": component_comparisons,
            "unresolved_components": unresolved_components,
            "attribution_validation": attribution_validation,
            "all_run_attributions_within_tolerance": all_attributions_within_tolerance,
        }

    def measure_ready_post_tool_use_cell(
        self,
        cell: PostToolUseTimingCell,
        durable_state: DurableProofCacheState,
        journal_age: TimingJournalAge,
        process_cache_temperature: ProcessCacheTemperature,
        budget_overruns: list[PostToolUseTimingBudgetOverrun],
        observed_recovery_argv: dict[PostToolUseOutcome, list[list[str]]],
        git_contract_mismatches: list[PostToolUseGitContractMismatch],
        observation_contract_mismatches: list[
            PostToolUseObservationContractMismatch
        ],
    ) -> dict[str, object]:
        """Measure five independently restored samples for one ready-state cell."""

        expected_equivalence_argv = (
            cell.expected_equivalence_argv
            if durable_state is DurableProofCacheState.NOT_EVALUATED
            else 0
        )
        if (
            process_cache_temperature
            is ProcessCacheTemperature.WARMED_EXECUTABLE_PAGES
        ):
            warmup_fixture = self.prepare_post_tool_use_timing_fixture(
                cell.outcome, durable_state, journal_age
            )
            try:
                if warmup_fixture.journal_contract_applies:
                    self.assert_timing_repository_contract(
                        warmup_fixture,
                        TimingRepositoryContract(
                            journal_record_count=journal_age.record_count,
                            retained_reservation_count=PUBLISHED_POST_TOOL_USE_BOUND.outcome_matrix_retained_reservation_count,
                            head_commit_count=PUBLISHED_POST_TOOL_USE_BOUND.outcome_matrix_history_commit_count,
                        ),
                    )
                warmup = self.run_installed_engine_hook(warmup_fixture)
                self.assertEqual(
                    warmup.process.returncode,
                    0,
                    f"{cell.outcome.value} warmup hook failed: stdout={warmup.process.stdout!r}, stderr={warmup.process.stderr!r}, cargo={warmup.cargo_berth_calls!r}, git={warmup.git_calls!r}",
                )
            finally:
                warmup_fixture.cleanup()

        elapsed_samples: list[float] = []
        observed_hook_executables: list[int] = []
        observed_git_argv: list[int] = []
        observed_provenance_log: list[int] = []
        observed_provenance_rev_list: list[int] = []
        observed_equivalence: list[int] = []
        observed_git_calls: list[list[list[str]]] = []
        sample_process_evidence: list[PostToolUseSampleProcessEvidence] = []
        for sample_index in range(POST_TOOL_USE_SAMPLE_COUNT):
            fixture = self.prepare_post_tool_use_timing_fixture(
                cell.outcome, durable_state, journal_age
            )
            try:
                if fixture.journal_contract_applies:
                    self.assert_timing_repository_contract(
                        fixture,
                        TimingRepositoryContract(
                            journal_record_count=journal_age.record_count,
                            retained_reservation_count=PUBLISHED_POST_TOOL_USE_BOUND.outcome_matrix_retained_reservation_count,
                            head_commit_count=PUBLISHED_POST_TOOL_USE_BOUND.outcome_matrix_history_commit_count,
                        ),
                    )
                invocation = self.run_installed_engine_hook(
                    fixture, process_cache_temperature
                )
                self.assertEqual(invocation.process.returncode, 0)
                rendered = invocation.process.stdout + invocation.process.stderr
                if cell.required_rendered_text:
                    self.assertIn(
                        cell.required_rendered_text,
                        rendered,
                        f"{cell.outcome.value} {process_cache_temperature.value} sample {sample_index} did not render its required text: cargo={invocation.cargo_berth_calls!r}, git={invocation.git_calls!r}",
                    )
                else:
                    self.assertEqual(
                        rendered,
                        "",
                        f"{cell.outcome.value} {process_cache_temperature.value} sample {sample_index} rendered unexpected feedback",
                    )
                if cell.outcome is PostToolUseOutcome.TYPED_REPLAY_FAILURE:
                    self.assertIn("REPLAY HARD STOP", rendered)
                    self.assertIn("reinitialize-after-review", rendered)
                    self.assertNotIn(UNREAD_MESSAGE, rendered)
                provenance_log_argv = self.observed_provenance_log_argv(invocation)
                provenance_rev_list_argv = self.observed_provenance_rev_list_argv(
                    invocation
                )
                equivalence_git_argv = self.observed_equivalence_git_argv(
                    invocation, fixture.equivalence_object_ids
                )
                process_evidence = PostToolUseSampleProcessEvidence(
                    sample_index=sample_index,
                    cargo_berth_argv=tuple(
                        tuple(argv) for argv in invocation.cargo_berth_calls
                    ),
                    git_argv=tuple(tuple(argv) for argv in invocation.git_calls),
                    jq_trace=invocation.jq_trace,
                )
                sample_process_evidence.append(process_evidence)
                expected_observations = (
                    (
                        PostToolUseObservationContract.HOOK_LEVEL_EXECUTABLE_COUNT,
                        expected_hook_level_executables(
                            fixture.git_race_transition
                        ),
                        invocation.hook_level_executable_count,
                    ),
                    (
                        PostToolUseObservationContract.PROVENANCE_LOG_ARGV_COUNT,
                        cell.expected_provenance_log_argv,
                        provenance_log_argv,
                    ),
                    (
                        PostToolUseObservationContract.PROVENANCE_REV_LIST_ARGV_COUNT,
                        cell.expected_provenance_rev_list_argv,
                        provenance_rev_list_argv,
                    ),
                )
                for observation_contract, expected_value, observed_value in (
                    expected_observations
                ):
                    if observed_value == expected_value:
                        continue
                    observation_contract_mismatches.append(
                        PostToolUseObservationContractMismatch(
                            outcome=cell.outcome,
                            journal_age=journal_age,
                            process_cache_temperature=process_cache_temperature,
                            durable_proof_cache_state=durable_state,
                            observation_contract=observation_contract,
                            expected_value=expected_value,
                            observed_value=observed_value,
                            observed_sample_values=(observed_value,),
                            sample_process_evidence=(process_evidence,),
                        )
                    )
                if (
                    invocation.elapsed_seconds
                    >= PUBLISHED_POST_TOOL_USE_BOUND.maximum_seconds
                ):
                    budget_overruns.append(
                        PostToolUseTimingBudgetOverrun(
                            outcome=f"{cell.outcome.value}[journal_age={journal_age.value}]",
                            process_cache_temperature=process_cache_temperature,
                            durable_proof_cache_state=durable_state,
                            sample_index=sample_index,
                            elapsed_seconds=invocation.elapsed_seconds,
                        )
                    )
                elapsed_samples.append(invocation.elapsed_seconds)
                observed_hook_executables.append(
                    invocation.hook_level_executable_count
                )
                observed_git_argv.append(len(invocation.git_calls))
                observed_provenance_log.append(provenance_log_argv)
                observed_provenance_rev_list.append(provenance_rev_list_argv)
                observed_equivalence.append(equivalence_git_argv)
                observed_git_calls.append(invocation.git_calls)
                if cell.outcome in {
                    PostToolUseOutcome.STALE_SESSION_MAPPING,
                    PostToolUseOutcome.STALE_MARKER_RUN,
                    PostToolUseOutcome.SESSION_WORKTREE_MISMATCH,
                }:
                    observed_recovery_argv[cell.outcome] = (
                        self.rendered_recovery_argv(rendered)
                    )
            finally:
                fixture.cleanup()
        consistency_observations = (
            (
                PostToolUseObservationContract.HOOK_LEVEL_EXECUTABLE_SAMPLE_CONSISTENCY,
                observed_hook_executables,
            ),
            (
                PostToolUseObservationContract.GIT_ARGV_SAMPLE_CONSISTENCY,
                observed_git_argv,
            ),
            (
                PostToolUseObservationContract.PROVENANCE_LOG_SAMPLE_CONSISTENCY,
                observed_provenance_log,
            ),
            (
                PostToolUseObservationContract.PROVENANCE_REV_LIST_SAMPLE_CONSISTENCY,
                observed_provenance_rev_list,
            ),
            (
                PostToolUseObservationContract.EQUIVALENCE_SAMPLE_CONSISTENCY,
                observed_equivalence,
            ),
        )
        for observation_contract, observed_sample_values in consistency_observations:
            self.record_post_tool_use_sample_consistency_mismatch(
                cell,
                durable_state,
                journal_age,
                process_cache_temperature,
                observation_contract,
                observed_sample_values,
                sample_process_evidence,
                observation_contract_mismatches,
            )
        observed_argv = tuple(tuple(argv) for argv in observed_git_calls[0])
        if observed_equivalence[0] != expected_equivalence_argv:
            git_contract_mismatches.append(
                PostToolUseGitContractMismatch(
                    outcome=cell.outcome,
                    journal_age=journal_age,
                    process_cache_temperature=process_cache_temperature,
                    durable_proof_cache_state=durable_state,
                    git_process_contract=PostToolUseGitProcessContract.SCOPED_EQUIVALENCE_COUNT,
                    expected_count=expected_equivalence_argv,
                    observed_count=observed_equivalence[0],
                    observed_argv=observed_argv,
                )
            )
        if observed_git_argv[0] > cell.maximum_git_argv:
            git_contract_mismatches.append(
                PostToolUseGitContractMismatch(
                    outcome=cell.outcome,
                    journal_age=journal_age,
                    process_cache_temperature=process_cache_temperature,
                    durable_proof_cache_state=durable_state,
                    git_process_contract=PostToolUseGitProcessContract.OUTCOME_PROCESS_CEILING,
                    expected_count=cell.maximum_git_argv,
                    observed_count=observed_git_argv[0],
                    observed_argv=observed_argv,
                )
            )
        return {
            "measurement_kind": "outcome_matrix",
            "cell": f"{cell.outcome.value}[journal_age={journal_age.value},proof_cache={durable_state.value}]",
            "outcome": cell.outcome.value,
            "journal_age": journal_age.value,
            "journal_records": journal_age.record_count,
            "installation_state": PUBLISHED_POST_TOOL_USE_BOUND.installation_state.value,
            "process_cache_temperature": process_cache_temperature.value,
            "durable_proof_cache_state": durable_state.value,
            "maximum_wall_seconds": max(elapsed_samples),
            "median_wall_seconds": self.median_timing_sample(elapsed_samples),
            "hook_level_executables": observed_hook_executables[0],
            "git_argv": observed_git_argv[0],
            "maximum_git_argv": cell.maximum_git_argv,
            "provenance_log_git_argv": observed_provenance_log[0],
            "provenance_rev_list_git_argv": observed_provenance_rev_list[0],
            "equivalence_git_argv": observed_equivalence[0],
        }

    def measure_history_post_tool_use_cell(
        self,
        history_measurement: PublishedHistoryMeasurement,
        subject_cardinality: HistorySubjectCardinality,
        process_cache_temperature: ProcessCacheTemperature,
        budget_overruns: list[PostToolUseTimingBudgetOverrun],
    ) -> dict[str, object]:
        """Measure one commit topology and active-subject cardinality."""

        if (
            process_cache_temperature
            is ProcessCacheTemperature.WARMED_EXECUTABLE_PAGES
        ):
            warmup_fixture = self.prepare_history_timing_fixture(
                history_measurement, subject_cardinality
            )
            try:
                self.assert_history_repository_contract(
                    warmup_fixture, history_measurement, subject_cardinality
                )
                warmup = self.run_installed_engine_hook(warmup_fixture)
                self.assertEqual(warmup.process.returncode, 0)
            finally:
                warmup_fixture.cleanup()
        elapsed_samples: list[float] = []
        observed_git_argv: list[int] = []
        for sample_index in range(POST_TOOL_USE_SAMPLE_COUNT):
            fixture = self.prepare_history_timing_fixture(
                history_measurement, subject_cardinality
            )
            try:
                self.assert_history_repository_contract(
                    fixture, history_measurement, subject_cardinality
                )
                invocation = self.run_installed_engine_hook(
                    fixture, process_cache_temperature
                )
                self.assertEqual(invocation.process.returncode, 0)
                self.assertEqual(
                    invocation.hook_level_executable_count,
                    expected_hook_level_executables(fixture.git_race_transition),
                )
                self.assertEqual(
                    invocation.process.stdout + invocation.process.stderr, ""
                )
                self.assertLessEqual(
                    len(invocation.git_calls), HISTORY_GIT_PROCESS_CEILING
                )
                if (
                    invocation.elapsed_seconds
                    >= PUBLISHED_POST_TOOL_USE_BOUND.maximum_seconds
                ):
                    budget_overruns.append(
                        PostToolUseTimingBudgetOverrun(
                            outcome=f"history[profile={history_measurement.profile.value},subjects={subject_cardinality.value}]",
                            process_cache_temperature=process_cache_temperature,
                            durable_proof_cache_state=DurableProofCacheState.NOT_APPLICABLE,
                            sample_index=sample_index,
                            elapsed_seconds=invocation.elapsed_seconds,
                        )
                    )
                elapsed_samples.append(invocation.elapsed_seconds)
                observed_git_argv.append(len(invocation.git_calls))
            finally:
                fixture.cleanup()
        self.assertEqual(len(set(observed_git_argv)), 1)
        return {
            "measurement_kind": "repository_history",
            "cell": f"history[profile={history_measurement.profile.value},subjects={subject_cardinality.value}]",
            "history_profile": history_measurement.profile.value,
            "subject_cardinality": subject_cardinality.value,
            "journal_age": TimingJournalAge.WORKING_REPOSITORY.value,
            "journal_records": WORKING_REPOSITORY_JOURNAL_RECORD_COUNT,
            "head_commit_count": history_measurement.head_commit_count(),
            "all_refs_commit_count": history_measurement.all_refs_commit_count(
                subject_cardinality
            ),
            "installation_state": PUBLISHED_POST_TOOL_USE_BOUND.installation_state.value,
            "process_cache_temperature": process_cache_temperature.value,
            "durable_proof_cache_state": DurableProofCacheState.NOT_APPLICABLE.value,
            "maximum_wall_seconds": max(elapsed_samples),
            "median_wall_seconds": self.median_timing_sample(elapsed_samples),
            "hook_level_executables": 2,
            "git_argv": observed_git_argv[0],
        }

    def measure_lost_evidence_cardinality_cell(
        self,
        trunk_resolution: TrunkResolution,
        alert_cardinality: LostEvidenceAlertCardinality,
        process_cache_temperature: ProcessCacheTemperature,
        budget_overruns: list[PostToolUseTimingBudgetOverrun],
    ) -> dict[str, object]:
        """Measure one recovery variant at an exact lost-evidence alert count."""

        outcome = {
            TrunkResolution.RESOLVED: PostToolUseOutcome.LOST_EVIDENCE_RESOLVED_TRUNK,
            TrunkResolution.UNRESOLVED: PostToolUseOutcome.LOST_EVIDENCE_UNRESOLVED_TRUNK,
        }[trunk_resolution]
        required_text = {
            TrunkResolution.RESOLVED: "--integrated-as",
            TrunkResolution.UNRESOLVED: "Resolve trunk first",
        }[trunk_resolution]
        if (
            process_cache_temperature
            is ProcessCacheTemperature.WARMED_EXECUTABLE_PAGES
        ):
            warmup_fixture = self.prepare_lost_evidence_timing_fixture(
                trunk_resolution,
                TimingJournalAge.WORKING_REPOSITORY,
                alert_cardinality,
            )
            try:
                self.assert_timing_repository_contract(
                    warmup_fixture,
                    TimingRepositoryContract(
                        journal_record_count=WORKING_REPOSITORY_JOURNAL_RECORD_COUNT,
                        retained_reservation_count=alert_cardinality.value + 1,
                        head_commit_count=SHALLOW_TIMING_HISTORY_COMMIT_COUNT,
                    ),
                )
                warmup = self.run_installed_engine_hook(warmup_fixture)
                self.assertEqual(warmup.process.returncode, 0)
            finally:
                warmup_fixture.cleanup()
        elapsed_samples: list[float] = []
        observed_git_argv: list[int] = []
        observed_git_sequences: list[tuple[str, ...]] = []
        for sample_index in range(POST_TOOL_USE_SAMPLE_COUNT):
            fixture = self.prepare_lost_evidence_timing_fixture(
                trunk_resolution,
                TimingJournalAge.WORKING_REPOSITORY,
                alert_cardinality,
            )
            try:
                self.assert_timing_repository_contract(
                    fixture,
                    TimingRepositoryContract(
                        journal_record_count=WORKING_REPOSITORY_JOURNAL_RECORD_COUNT,
                        retained_reservation_count=alert_cardinality.value + 1,
                        head_commit_count=SHALLOW_TIMING_HISTORY_COMMIT_COUNT,
                    ),
                )
                invocation = self.run_installed_engine_hook(
                    fixture, process_cache_temperature
                )
                self.assertEqual(invocation.process.returncode, 0)
                self.assertEqual(
                    invocation.hook_level_executable_count,
                    expected_hook_level_executables(fixture.git_race_transition),
                )
                rendered = invocation.process.stdout + invocation.process.stderr
                self.assertEqual(
                    rendered.count(required_text), alert_cardinality.value
                )
                outcome_ceiling = next(
                    cell.maximum_git_argv
                    for cell in self.post_tool_use_timing_cells()
                    if cell.outcome is outcome
                )
                self.assertLessEqual(len(invocation.git_calls), outcome_ceiling)
                if (
                    invocation.elapsed_seconds
                    >= PUBLISHED_POST_TOOL_USE_BOUND.maximum_seconds
                ):
                    budget_overruns.append(
                        PostToolUseTimingBudgetOverrun(
                            outcome=f"{outcome.value}[alerts={alert_cardinality.value}]",
                            process_cache_temperature=process_cache_temperature,
                            durable_proof_cache_state=DurableProofCacheState.NOT_APPLICABLE,
                            sample_index=sample_index,
                            elapsed_seconds=invocation.elapsed_seconds,
                        )
                    )
                elapsed_samples.append(invocation.elapsed_seconds)
                observed_git_argv.append(len(invocation.git_calls))
                observed_git_sequences.append(
                    self.canonical_git_command_sequence(invocation)
                )
            finally:
                fixture.cleanup()
        self.assertEqual(len(set(observed_git_argv)), 1)
        self.assertEqual(len(set(observed_git_sequences)), 1)
        return {
            "measurement_kind": "lost_evidence_cardinality",
            "cell": f"{outcome.value}[alerts={alert_cardinality.value}]",
            "outcome": outcome.value,
            "alert_cardinality": alert_cardinality.value,
            "journal_age": TimingJournalAge.WORKING_REPOSITORY.value,
            "journal_records": WORKING_REPOSITORY_JOURNAL_RECORD_COUNT,
            "installation_state": PUBLISHED_POST_TOOL_USE_BOUND.installation_state.value,
            "process_cache_temperature": process_cache_temperature.value,
            "durable_proof_cache_state": DurableProofCacheState.NOT_APPLICABLE.value,
            "maximum_wall_seconds": max(elapsed_samples),
            "median_wall_seconds": self.median_timing_sample(elapsed_samples),
            "hook_level_executables": 2,
            "git_argv": observed_git_argv[0],
            "canonical_git_command_sequence": observed_git_sequences[0],
        }

    def measure_incident_cardinality_cell(
        self,
        retained_state: RetainedIncursionState,
        incident_cardinality: IncursionIncidentCardinality,
        process_cache_temperature: ProcessCacheTemperature,
        budget_overruns: list[PostToolUseTimingBudgetOverrun],
    ) -> dict[str, object]:
        """Measure a live board read while reservations stay fixed at two."""

        if (
            process_cache_temperature
            is ProcessCacheTemperature.WARMED_EXECUTABLE_PAGES
        ):
            warmup_fixture = self.prepare_incident_cardinality_timing_fixture(
                incident_cardinality, retained_state
            )
            try:
                self.assert_timing_repository_contract(
                    warmup_fixture,
                    TimingRepositoryContract(
                        journal_record_count=WORKING_REPOSITORY_JOURNAL_RECORD_COUNT,
                        retained_reservation_count=TIMING_RETAINED_RESERVATION_COUNT,
                        head_commit_count=SHALLOW_TIMING_HISTORY_COMMIT_COUNT,
                    ),
                )
                warmup = self.run_installed_engine_hook(warmup_fixture)
                self.assertEqual(warmup.process.returncode, 0)
            finally:
                warmup_fixture.cleanup()
        elapsed_samples: list[float] = []
        observed_git_argv: list[int] = []
        observed_git_sequences: list[tuple[str, ...]] = []
        for sample_index in range(POST_TOOL_USE_SAMPLE_COUNT):
            fixture = self.prepare_incident_cardinality_timing_fixture(
                incident_cardinality, retained_state
            )
            try:
                self.assert_timing_repository_contract(
                    fixture,
                    TimingRepositoryContract(
                        journal_record_count=WORKING_REPOSITORY_JOURNAL_RECORD_COUNT,
                        retained_reservation_count=TIMING_RETAINED_RESERVATION_COUNT,
                        head_commit_count=SHALLOW_TIMING_HISTORY_COMMIT_COUNT,
                    ),
                )
                board = self.engine_command(
                    fixture.repository_root, ["board", "--json"]
                )
                payload = cast(dict[str, object], board["payload"])
                data = cast(dict[str, object], payload["data"])
                section_name = {
                    RetainedIncursionState.OUTSTANDING: "outstanding_incursions",
                    RetainedIncursionState.RESOLVED: "recorded_incursion_answers",
                }[retained_state]
                section = cast(dict[str, object], data[section_name])
                entries = cast(list[object], section["entries"])
                self.assertEqual(len(entries), incident_cardinality.value)
                invocation = self.run_installed_engine_hook(
                    fixture, process_cache_temperature
                )
                self.assertEqual(invocation.process.returncode, 0)
                self.assertEqual(
                    invocation.hook_level_executable_count,
                    expected_hook_level_executables(fixture.git_race_transition),
                )
                rendered = invocation.process.stdout + invocation.process.stderr
                if retained_state is RetainedIncursionState.OUTSTANDING:
                    self.assertIn("STOP", rendered)
                else:
                    self.assertNotIn("STOP", rendered)
                self.assertLessEqual(
                    len(invocation.git_calls), LIVE_BOARD_GIT_PROCESS_CEILING
                )
                if (
                    invocation.elapsed_seconds
                    >= PUBLISHED_POST_TOOL_USE_BOUND.maximum_seconds
                ):
                    budget_overruns.append(
                        PostToolUseTimingBudgetOverrun(
                            outcome=f"board_incidents[state={retained_state.value},incidents={incident_cardinality.value}]",
                            process_cache_temperature=process_cache_temperature,
                            durable_proof_cache_state=DurableProofCacheState.NOT_APPLICABLE,
                            sample_index=sample_index,
                            elapsed_seconds=invocation.elapsed_seconds,
                        )
                    )
                elapsed_samples.append(invocation.elapsed_seconds)
                observed_git_argv.append(len(invocation.git_calls))
                observed_git_sequences.append(
                    self.canonical_git_command_sequence(invocation)
                )
            finally:
                fixture.cleanup()
        self.assertEqual(len(set(observed_git_argv)), 1)
        self.assertEqual(len(set(observed_git_sequences)), 1)
        return {
            "measurement_kind": "board_incident_cardinality",
            "cell": f"board_incidents[state={retained_state.value},incidents={incident_cardinality.value}]",
            "retained_incursion_state": retained_state.value,
            "incident_cardinality": incident_cardinality.value,
            "retained_reservations": TIMING_RETAINED_RESERVATION_COUNT,
            "journal_age": TimingJournalAge.WORKING_REPOSITORY.value,
            "journal_records": WORKING_REPOSITORY_JOURNAL_RECORD_COUNT,
            "installation_state": PUBLISHED_POST_TOOL_USE_BOUND.installation_state.value,
            "process_cache_temperature": process_cache_temperature.value,
            "durable_proof_cache_state": DurableProofCacheState.NOT_APPLICABLE.value,
            "maximum_wall_seconds": max(elapsed_samples),
            "median_wall_seconds": self.median_timing_sample(elapsed_samples),
            "hook_level_executables": 5,
            "git_argv": observed_git_argv[0],
            "canonical_git_command_sequence": observed_git_sequences[0],
        }

    def measure_binary_absent_post_tool_use_cell(
        self,
        engineless_path: str,
        process_cache_temperature: ProcessCacheTemperature,
        budget_overruns: list[PostToolUseTimingBudgetOverrun],
    ) -> dict[str, object]:
        """Measure five samples of the route the wrapper walks with no engine."""

        real_bash = shutil.which("bash", path=self.base_environment["PATH"])
        if real_bash is None:
            raise RuntimeError("the timing fixture requires bash")
        environment = {**self.base_environment, "PATH": engineless_path}
        serialized_payload = json.dumps(self.post_bash_payload())

        def run_once() -> tuple[subprocess.CompletedProcess[str], float]:
            if (
                process_cache_temperature
                is ProcessCacheTemperature.COLD_EXECUTABLE_PAGES
            ):
                TimedChildExecutablePages(
                    (POST_BASH_HOOK, Path("/usr/bin/env"), Path(real_bash))
                ).invalidate_and_verify_cold()
            started_at = time.perf_counter()
            completed = subprocess.run(
                [str(POST_BASH_HOOK)],
                cwd=self.repository_root,
                env=environment,
                input=serialized_payload,
                text=True,
                capture_output=True,
                check=False,
            )
            return completed, time.perf_counter() - started_at

        if (
            process_cache_temperature
            is ProcessCacheTemperature.WARMED_EXECUTABLE_PAGES
        ):
            warmup, _ = run_once()
            self.assertEqual(warmup.returncode, 0)
        elapsed_samples: list[float] = []
        for sample_index in range(POST_TOOL_USE_SAMPLE_COUNT):
            completed, elapsed_seconds = run_once()
            self.assertEqual(completed.returncode, 0)
            self.assertEqual(completed.stderr, "")
            self.assertIn("installation needs repair", completed.stdout)
            self.assertIn("not on PATH", completed.stdout)
            if elapsed_seconds >= PUBLISHED_POST_TOOL_USE_BOUND.maximum_seconds:
                budget_overruns.append(
                    PostToolUseTimingBudgetOverrun(
                        outcome="engine_binary_absent",
                        process_cache_temperature=process_cache_temperature,
                        durable_proof_cache_state=DurableProofCacheState.NOT_APPLICABLE,
                        sample_index=sample_index,
                        elapsed_seconds=elapsed_seconds,
                    )
                )
            elapsed_samples.append(elapsed_seconds)
        return {
            "measurement_kind": "installation_state",
            "cell": "engine_binary_absent",
            "outcome": "engine_binary_absent",
            "installation_state": EngineInstallationState.NEEDS_REPAIR.value,
            "process_cache_temperature": process_cache_temperature.value,
            "durable_proof_cache_state": DurableProofCacheState.NOT_APPLICABLE.value,
            "maximum_wall_seconds": max(elapsed_samples),
            "median_wall_seconds": self.median_timing_sample(elapsed_samples),
            "hook_level_executables": 1,
            "cargo_berth_invocations": 0,
            "git_argv": 0,
        }

    def run_complete_post_tool_use_outcome_matrix_measurement(
        self,
    ) -> None:
        self.assertEqual(PUBLISHED_POST_TOOL_USE_BOUND.covered_path, POST_BASH_HOOK)
        self.assertEqual(
            PUBLISHED_POST_TOOL_USE_BOUND.installation_state,
            EngineInstallationState.READY,
        )
        self.assertEqual(
            PUBLISHED_POST_TOOL_USE_BOUND.measured_process_cache_temperatures,
            (
                ProcessCacheTemperature.COLD_EXECUTABLE_PAGES,
                ProcessCacheTemperature.WARMED_EXECUTABLE_PAGES,
            ),
        )
        self.assertEqual(
            PUBLISHED_POST_TOOL_USE_BOUND.outcome_matrix_journal_ages,
            (TimingJournalAge.SHORT, TimingJournalAge.WORKING_REPOSITORY),
        )
        self.assertEqual(
            tuple(
                history_measurement.profile
                for history_measurement in PUBLISHED_POST_TOOL_USE_BOUND.history_measurements
            ),
            (
                TimingHistoryProfile.SHALLOW,
                TimingHistoryProfile.DEEP,
                TimingHistoryProfile.DIVERGENT,
            ),
        )
        for installed_artifact in INSTALLED_TIMING_ARTIFACTS:
            self.assertTrue(installed_artifact.is_file())
            self.assertTrue(os.access(installed_artifact, os.X_OK))
        self.assert_installed_engine_preflight()

        cells = self.post_tool_use_timing_cells()
        covered_outcomes = [cell.outcome for cell in cells]
        self.assertEqual(len(covered_outcomes), len(set(covered_outcomes)))
        self.assertEqual(set(covered_outcomes), REQUIRED_READY_OUTCOMES)

        report: list[dict[str, object]] = []
        budget_overruns: list[PostToolUseTimingBudgetOverrun] = []
        observed_recovery_argv: dict[PostToolUseOutcome, list[list[str]]] = {}
        git_contract_mismatches: list[PostToolUseGitContractMismatch] = []
        observation_contract_mismatches: list[
            PostToolUseObservationContractMismatch
        ] = []
        attribution_mismatches: list[PostToolUseAttributionMismatch] = []
        for cell in cells:
            for durable_state in cell.durable_proof_cache_states:
                for journal_age in (
                    PUBLISHED_POST_TOOL_USE_BOUND.outcome_matrix_journal_ages
                ):
                    for process_cache_temperature in (
                        PUBLISHED_POST_TOOL_USE_BOUND.measured_process_cache_temperatures
                    ):
                        report.append(
                            self.measure_ready_post_tool_use_cell(
                                cell,
                                durable_state,
                                journal_age,
                                process_cache_temperature,
                                budget_overruns,
                                observed_recovery_argv,
                                git_contract_mismatches,
                                observation_contract_mismatches,
                            )
                        )

        lost_evidence_results: dict[
            tuple[
                TrunkResolution,
                ProcessCacheTemperature,
                LostEvidenceAlertCardinality,
            ],
            dict[str, object],
        ] = {}
        for trunk_resolution in TrunkResolution:
            for process_cache_temperature in (
                PUBLISHED_POST_TOOL_USE_BOUND.measured_process_cache_temperatures
            ):
                for alert_cardinality in LostEvidenceAlertCardinality:
                    result = self.measure_lost_evidence_cardinality_cell(
                        trunk_resolution,
                        alert_cardinality,
                        process_cache_temperature,
                        budget_overruns,
                    )
                    lost_evidence_results[
                        (
                            trunk_resolution,
                            process_cache_temperature,
                            alert_cardinality,
                        )
                    ] = result
                    report.append(result)
                one_alert = lost_evidence_results[
                    (
                        trunk_resolution,
                        process_cache_temperature,
                        LostEvidenceAlertCardinality.ONE,
                    )
                ]
                twenty_alerts = lost_evidence_results[
                    (
                        trunk_resolution,
                        process_cache_temperature,
                        LostEvidenceAlertCardinality.TWENTY,
                    )
                ]
                self.assertEqual(one_alert["git_argv"], twenty_alerts["git_argv"])
                self.assertEqual(
                    one_alert["canonical_git_command_sequence"],
                    twenty_alerts["canonical_git_command_sequence"],
                )

        incident_results: dict[
            tuple[
                RetainedIncursionState,
                ProcessCacheTemperature,
                IncursionIncidentCardinality,
            ],
            dict[str, object],
        ] = {}
        for retained_state in RetainedIncursionState:
            for process_cache_temperature in (
                PUBLISHED_POST_TOOL_USE_BOUND.measured_process_cache_temperatures
            ):
                for incident_cardinality in IncursionIncidentCardinality:
                    result = self.measure_incident_cardinality_cell(
                        retained_state,
                        incident_cardinality,
                        process_cache_temperature,
                        budget_overruns,
                    )
                    incident_results[
                        (
                            retained_state,
                            process_cache_temperature,
                            incident_cardinality,
                        )
                    ] = result
                    report.append(result)
                one_incident = incident_results[
                    (
                        retained_state,
                        process_cache_temperature,
                        IncursionIncidentCardinality.ONE,
                    )
                ]
                fifty_incidents = incident_results[
                    (
                        retained_state,
                        process_cache_temperature,
                        IncursionIncidentCardinality.FIFTY,
                    )
                ]
                self.assertEqual(
                    one_incident["git_argv"], fifty_incidents["git_argv"]
                )

        for history_measurement in PUBLISHED_POST_TOOL_USE_BOUND.history_measurements:
            for subject_cardinality in history_measurement.subject_cardinalities:
                for process_cache_temperature in (
                    PUBLISHED_POST_TOOL_USE_BOUND.measured_process_cache_temperatures
                ):
                    report.append(
                        self.measure_history_post_tool_use_cell(
                            history_measurement,
                            subject_cardinality,
                            process_cache_temperature,
                            budget_overruns,
                        )
                    )

        for recovery_input in PUBLISHED_POST_TOOL_USE_BOUND.measured_recovery_inputs:
            self.assertEqual(
                observed_recovery_argv[recovery_input.outcome],
                [list(argv) for argv in recovery_input.action_argv],
            )
        observed_recovery_inputs = tuple(
            observed_recovery_argv[recovery_input.outcome]
            for recovery_input in PUBLISHED_POST_TOOL_USE_BOUND.measured_recovery_inputs
        )
        largest_observed_action_set = max(observed_recovery_inputs, key=len)
        self.assertEqual(
            tuple(
                recovery_input.action_kinds
                for recovery_input in PUBLISHED_POST_TOOL_USE_BOUND.measured_recovery_inputs
                if len(recovery_input.action_argv) == len(largest_observed_action_set)
            ),
            (PUBLISHED_POST_TOOL_USE_BOUND.largest_recovery_action_set,),
        )
        longest_observed_action = max(
            (
                tuple(argv)
                for action_set in observed_recovery_inputs
                for argv in action_set
            ),
            key=len,
        )
        self.assertEqual(
            longest_observed_action,
            PUBLISHED_POST_TOOL_USE_BOUND.longest_recovery_action_argv,
        )

        engineless_path = os.pathsep.join(
            directory
            for directory in self.base_environment["PATH"].split(os.pathsep)
            if not (Path(directory) / "cargo-berth").exists()
        )
        self.assertIsNone(shutil.which("cargo-berth", path=engineless_path))
        for process_cache_temperature in (
            PUBLISHED_POST_TOOL_USE_BOUND.measured_process_cache_temperatures
        ):
            report.append(
                self.measure_binary_absent_post_tool_use_cell(
                    engineless_path, process_cache_temperature, budget_overruns
                )
            )
        ready_results = [
            result
            for result in report
            if result["installation_state"]
            == EngineInstallationState.READY.value
        ]
        fixed_cost_attribution = self.measure_fixed_cost_attribution(
            ready_results, attribution_mismatches
        )
        ready_budget_overruns = [
            overrun
            for overrun in budget_overruns
            if overrun.outcome != "engine_binary_absent"
        ]
        red_cell_maxima = [
            {
                "cell": result["cell"],
                "process_cache_temperature": result[
                    "process_cache_temperature"
                ],
                "maximum_wall_seconds": result["maximum_wall_seconds"],
                "git_argv": result["git_argv"],
            }
            for result in ready_results
            if cast(float, result["maximum_wall_seconds"])
            >= PUBLISHED_POST_TOOL_USE_BOUND.maximum_seconds
        ]
        maximum_git_argv = max(
            cast(int, result["git_argv"]) for result in ready_results
        )
        maximum_wall_cells = {
            process_cache_temperature.value: [
                {
                    "cell": result["cell"],
                    "measurement_kind": result["measurement_kind"],
                    "maximum_wall_seconds": result["maximum_wall_seconds"],
                    "git_argv": result["git_argv"],
                }
                for result in ready_results
                if result["process_cache_temperature"]
                == process_cache_temperature.value
                and result["maximum_wall_seconds"]
                == max(
                    cast(float, candidate["maximum_wall_seconds"])
                    for candidate in ready_results
                    if candidate["process_cache_temperature"]
                    == process_cache_temperature.value
                )
            ]
            for process_cache_temperature in PUBLISHED_POST_TOOL_USE_BOUND.measured_process_cache_temperatures
        }
        observed_git_argv_by_outcome = {
            outcome: max(
                cast(int, result["git_argv"])
                for result in ready_results
                if result.get("outcome") == outcome
            )
            for outcome in sorted(
                {
                    cast(str, result["outcome"])
                    for result in ready_results
                    if "outcome" in result
                }
            )
        }
        matrix_summary = {
            "ready_engine_samples": len(ready_results)
            * POST_TOOL_USE_SAMPLE_COUNT,
            "samples_at_or_above_bound": {
                process_cache_temperature.value: sum(
                    overrun.process_cache_temperature
                    is process_cache_temperature
                    for overrun in ready_budget_overruns
                )
                for process_cache_temperature in PUBLISHED_POST_TOOL_USE_BOUND.measured_process_cache_temperatures
            },
            "maximum_wall_seconds": {
                process_cache_temperature.value: max(
                    cast(float, result["maximum_wall_seconds"])
                    for result in ready_results
                    if result["process_cache_temperature"]
                    == process_cache_temperature.value
                )
                for process_cache_temperature in PUBLISHED_POST_TOOL_USE_BOUND.measured_process_cache_temperatures
            },
            "maximum_wall_cells": maximum_wall_cells,
            "maximum_git_argv": maximum_git_argv,
            "maximum_git_argv_cells": [
                result["cell"]
                for result in ready_results
                if result["git_argv"] == maximum_git_argv
            ],
            "observed_git_argv_by_outcome": observed_git_argv_by_outcome,
            "ready_cell_maxima": [
                {
                    "cell": result["cell"],
                    "measurement_kind": result["measurement_kind"],
                    "process_cache_temperature": result[
                        "process_cache_temperature"
                    ],
                    "maximum_wall_seconds": result["maximum_wall_seconds"],
                    "git_argv": result["git_argv"],
                }
                for result in ready_results
            ],
            "needs_repair_rows": [
                result
                for result in report
                if result["installation_state"]
                == EngineInstallationState.NEEDS_REPAIR.value
            ],
            "red_cell_maxima": red_cell_maxima,
            "fixed_cost_attribution": fixed_cost_attribution,
            "git_contract_mismatches": [
                mismatch.as_report() for mismatch in git_contract_mismatches
            ],
            "observation_contract_mismatches": [
                mismatch.as_report()
                for mismatch in observation_contract_mismatches
            ],
            "attribution_mismatches": [
                mismatch.as_report() for mismatch in attribution_mismatches
            ],
        }
        _ = TIMING_SUMMARY_PATH.write_text(
            json.dumps(matrix_summary, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(
            json.dumps(
                {
                    "published_post_tool_use_bound": {
                        "path": str(PUBLISHED_POST_TOOL_USE_BOUND.covered_path),
                        "installation_state": PUBLISHED_POST_TOOL_USE_BOUND.installation_state.value,
                        "measured_process_cache_temperatures": [
                            process_cache_temperature.value
                            for process_cache_temperature in PUBLISHED_POST_TOOL_USE_BOUND.measured_process_cache_temperatures
                        ],
                        "maximum_seconds": PUBLISHED_POST_TOOL_USE_BOUND.maximum_seconds,
                        "outcome_matrix_journal_ages": [
                            {
                                "age": journal_age.value,
                                "record_count": journal_age.record_count,
                            }
                            for journal_age in PUBLISHED_POST_TOOL_USE_BOUND.outcome_matrix_journal_ages
                        ],
                        "outcome_matrix_history_commit_count": PUBLISHED_POST_TOOL_USE_BOUND.outcome_matrix_history_commit_count,
                        "outcome_matrix_retained_reservations": PUBLISHED_POST_TOOL_USE_BOUND.outcome_matrix_retained_reservation_count,
                        "history_measurements": [
                            {
                                "profile": history_measurement.profile.value,
                                "base_commit_count": history_measurement.base_commit_count,
                                "commits_per_subject": history_measurement.commits_per_subject,
                                "repository_sizes": [
                                    {
                                        "subject_cardinality": subject_cardinality.value,
                                        "head_commit_count": history_measurement.head_commit_count(),
                                        "all_refs_commit_count": history_measurement.all_refs_commit_count(
                                            subject_cardinality
                                        ),
                                    }
                                    for subject_cardinality in history_measurement.subject_cardinalities
                                ],
                            }
                            for history_measurement in PUBLISHED_POST_TOOL_USE_BOUND.history_measurements
                        ],
                        "measured_recovery_inputs": [
                            {
                                "outcome": recovery_input.outcome.value,
                                "action_kinds": recovery_input.action_kinds,
                                "action_argv": recovery_input.action_argv,
                            }
                            for recovery_input in PUBLISHED_POST_TOOL_USE_BOUND.measured_recovery_inputs
                        ],
                        "largest_recovery_action_set": PUBLISHED_POST_TOOL_USE_BOUND.largest_recovery_action_set,
                        "longest_recovery_action_argv": PUBLISHED_POST_TOOL_USE_BOUND.longest_recovery_action_argv,
                        "excluded_global_hooks": PUBLISHED_POST_TOOL_USE_BOUND.excluded_global_hooks,
                        "engine_invocation": PUBLISHED_POST_TOOL_USE_BOUND.engine_invocation,
                    },
                    "matrix_summary": matrix_summary,
                    "fixed_cost_attribution": fixed_cost_attribution,
                    "samples": report,
                },
                sort_keys=True,
            )
        )
        self.assertEqual(
            git_contract_mismatches,
            [],
            f"PostToolUse outcome Git process contracts changed; see {TIMING_SUMMARY_PATH} for every mismatch",
        )
        self.assertEqual(
            observation_contract_mismatches,
            [],
            f"PostToolUse outcome observations changed; see {TIMING_SUMMARY_PATH} for every mismatch and raw argv",
        )
        self.assertNotEqual(
            fixed_cost_attribution["attribution_validation"],
            "not_validated_component_sum_misses_gate",
            f"resolved independent fixed-cost probes did not reproduce the production-route zero-Git intercept; see {TIMING_SUMMARY_PATH} for every component",
        )
        self.assertEqual(
            budget_overruns,
            [],
            f"PostToolUse samples exceeded the published shim bound: {matrix_summary}",
        )

















