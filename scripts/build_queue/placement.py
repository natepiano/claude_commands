"""Placement decisions keep submitting owners separate from remote executions."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from scripts.build_hold.owning_session import NoOwningSession, OwningSession
from scripts.mac_test.claim_state import ClaimScope

SubmittingSession = OwningSession | NoOwningSession


@dataclass(frozen=True)
class SubmittingOwner:
    host: str
    pid: int
    start_time: str


@dataclass(frozen=True)
class ClaimHolderIdentity:
    host: str
    pid: int
    start_time: str


@dataclass(frozen=True)
class ClaimHolderUnconfirmed:
    reason: str


@dataclass(frozen=True)
class TokenReadinessUnchecked:
    pass


@dataclass(frozen=True)
class TokenUnavailable:
    pass


@dataclass(frozen=True)
class TokenRequired:
    readiness: TokenReadinessUnchecked | TokenUnavailable = TokenUnavailable()


@dataclass(frozen=True)
class TokenHeld:
    pass


@dataclass(frozen=True)
class TokenNotRequired:
    pass


LocalExecutionPrerequisite = TokenRequired | TokenHeld | TokenNotRequired


@dataclass(frozen=True)
class NormalStep:
    pass


@dataclass(frozen=True)
class SpeculativeCopy:
    parent_episode: str
    call_started: float


PlacementPurpose = NormalStep | SpeculativeCopy


@dataclass(frozen=True)
class AttemptRunning:
    pass


@dataclass(frozen=True)
class AttemptCompleted:
    status: int


@dataclass(frozen=True)
class PrelaunchRefused:
    reason: str


@dataclass(frozen=True)
class AttemptDeclined:
    reason: str


@dataclass(frozen=True)
class AttemptCancelled:
    reason: str


@dataclass(frozen=True)
class AttemptOutcomeUnavailable:
    reason: str


AttemptOutcome = AttemptRunning | AttemptCompleted | PrelaunchRefused | AttemptDeclined | AttemptCancelled | AttemptOutcomeUnavailable
TerminalAttemptOutcome = AttemptCompleted | PrelaunchRefused | AttemptDeclined | AttemptCancelled


@dataclass(frozen=True)
class RetryAt:
    deadline: float


@dataclass(frozen=True)
class DoNotRetry:
    reason: str


RetryDecision = RetryAt | DoNotRetry


@dataclass(frozen=True)
class DispatchPathSettled:
    pass


@dataclass(frozen=True)
class DispatchPathPending:
    reason: str


DispatchPathState = DispatchPathSettled | DispatchPathPending


@dataclass(frozen=True)
class NoTerminalReport:
    pass


@dataclass(frozen=True)
class TerminalAttemptReport:
    outcome: TerminalAttemptOutcome
    path: DispatchPathState
    received_at: float


@dataclass(frozen=True)
class ConfiguredOrder:
    pass


@dataclass(frozen=True)
class PinnedHost:
    target: str


@dataclass(frozen=True)
class MacOnlyHost:
    pass


@dataclass(frozen=True)
class ScalerGateOrder:
    pass


HostSelection = ConfiguredOrder | PinnedHost | MacOnlyHost | ScalerGateOrder


@dataclass(frozen=True)
class AllowedHost:
    pass


@dataclass(frozen=True)
class FixedExclusion:
    reason: str


@dataclass(frozen=True)
class TemporaryExclusion:
    reason: str


@dataclass(frozen=True)
class EvidenceNeeded:
    reason: str


HostEligibility = AllowedHost | FixedExclusion | TemporaryExclusion | EvidenceNeeded


@dataclass(frozen=True)
class PlacementDestination:
    provider: Literal["local", "mac", "ssh", "scaler"]
    target: str
    host: str
    scope: ClaimScope
    slot: int = 0


@dataclass(frozen=True)
class ConfiguredProviderSettings:
    pass


@dataclass(frozen=True)
class ExplicitProviderSettings:
    path: Path


ProviderSettings = ConfiguredProviderSettings | ExplicitProviderSettings


@dataclass(frozen=True)
class PlacementCandidate:
    destination: PlacementDestination
    eligibility: AllowedHost | FixedExclusion = AllowedHost()
    settings: ProviderSettings = ConfiguredProviderSettings()


@dataclass(frozen=True)
class SourceKeyKnown:
    key: str


@dataclass(frozen=True)
class SourceKeyUnavailable:
    reason: str


SourceIdentity = SourceKeyKnown | SourceKeyUnavailable


@dataclass(frozen=True)
class PlacementConstraints:
    source_key: SourceIdentity
    command: tuple[str, ...]
    repository: str
    package: str
    session: SubmittingSession
    candidates: tuple[PlacementCandidate, ...]
    selection: HostSelection = ConfiguredOrder()


@dataclass(frozen=True)
class PlacementAssignment:
    episode: str
    attempt: str
    destination: PlacementDestination
    source_key: SourceIdentity
    owner: SubmittingOwner
    session: SubmittingSession
    since: float


@dataclass(frozen=True)
class CurrentAssignment:
    pass


@dataclass(frozen=True)
class ClosedAssignment:
    reason: str


AssignmentState = CurrentAssignment | ClosedAssignment


@dataclass(frozen=True)
class PermissionAvailable:
    pass


@dataclass(frozen=True)
class PermissionConsumed:
    consumed_at: float


LaunchPermission = PermissionAvailable | PermissionConsumed


@dataclass(frozen=True)
class ProviderClaimReference:
    provider: Literal["local", "mac", "ssh", "scaler"]
    target: str
    scope: ClaimScope
    claim_id: str
    episode: str
    attempt: str


@dataclass(frozen=True)
class NoProviderClaim:
    pass


@dataclass(frozen=True)
class AdoptedProviderClaim:
    reference: ProviderClaimReference
    holder: ClaimHolderIdentity | ClaimHolderUnconfirmed
    session: SubmittingSession
    host: str
    generation: str


@dataclass(frozen=True)
class RetainedCleanupClaim:
    reference: ProviderClaimReference
    reason: str


@dataclass(frozen=True)
class ReservedHost:
    assignment: PlacementAssignment
    claim: AdoptedProviderClaim


@dataclass(frozen=True)
class ProcessExecution:
    host: str
    generation: str
    pid: int
    start_time: str


@dataclass(frozen=True)
class CloudExecution:
    target: str
    generation: str
    instance: str
    attempt: str


ExecutionIdentity = ProcessExecution | CloudExecution


@dataclass(frozen=True)
class ExecutionNotStarted:
    pass


@dataclass(frozen=True)
class ObservedExecution:
    identity: ExecutionIdentity


@dataclass(frozen=True)
class ExecutionEvidenceUnavailable:
    reason: str
    observed_at: float


ExecutionEvidence = ExecutionNotStarted | ObservedExecution | ExecutionEvidenceUnavailable


@dataclass(frozen=True)
class NoLocalHoldOwner:
    """Remote host evidence does not depend on a local caller lineage."""


@dataclass(frozen=True)
class HostEvidenceKey:
    destination: PlacementDestination
    session: SubmittingSession
    command: tuple[str, ...]
    configuration_generation: str
    repository: str = ""
    package: str = ""
    step: str = ""
    settings: ProviderSettings = ConfiguredProviderSettings()
    local_hold_owner: SubmittingOwner | NoLocalHoldOwner = NoLocalHoldOwner()


@dataclass(frozen=True)
class ResourceHolderSession:
    session_id: str


@dataclass(frozen=True)
class ResourceHolderUnknown:
    pass


@dataclass(frozen=True)
class ResourceHolderUnit:
    unit: str


ResourceHolder = ResourceHolderSession | ResourceHolderUnit | ResourceHolderUnknown


@dataclass(frozen=True)
class BusyProviderDiagnostic:
    target: str
    what: str
    since: str


@dataclass(frozen=True)
class ProviderRetryNotice:
    target: str
    reason: str
    delay_seconds: float


@dataclass(frozen=True)
class NoProviderDiagnostic:
    pass


NO_PROVIDER_DIAGNOSTIC = NoProviderDiagnostic()


@dataclass(frozen=True)
class FreshHostEvidence:
    key: HostEvidenceKey
    observed_at: float
    generation: str
    eligibility: HostEligibility
    claims: tuple[AdoptedProviderClaim, ...] = ()
    cleanup_claims: tuple[RetainedCleanupClaim, ...] = ()
    executions: tuple[tuple[ProviderClaimReference, ExecutionEvidence], ...] = ()
    holder: ResourceHolder = ResourceHolderUnknown()
    provider_diagnostic: BusyProviderDiagnostic | NoProviderDiagnostic = NO_PROVIDER_DIAGNOSTIC


@dataclass(frozen=True)
class StaleHostEvidence:
    key: HostEvidenceKey
    observed_at: float
    reason: str


@dataclass(frozen=True)
class HostEvidenceUnavailable:
    key: HostEvidenceKey
    observed_at: float
    reason: str


HostEvidence = FreshHostEvidence | StaleHostEvidence | HostEvidenceUnavailable


@dataclass(frozen=True)
class CandidateEligibility:
    destination: PlacementDestination
    eligibility: HostEligibility


@dataclass(frozen=True)
class UnknownAssignment:
    episode: str
    attempt: str


@dataclass(frozen=True)
class NoLaunchPermission:
    reason: str


def attempt_key(episode: str, attempt: str) -> str:
    """Length framing keeps different episode and attempt pairs distinct."""
    return f"{len(episode)}:{episode}{attempt}"


def retry_decision(destination: PlacementDestination, reason: str, now: float) -> RetryDecision:
    """Keep the existing remote decline delays separate from evidence age."""
    if reason in ("no_tests", "off", "repo", "linux_only", "local_flag"):
        return DoNotRetry(reason)
    delay = 180 if destination.provider == "mac" and reason not in ("busy", "blocked", "disk") else 0
    return RetryAt(now + delay)


def copy_order(purpose: SpeculativeCopy, episode: str) -> tuple[float, str]:
    return purpose.call_started, episode
