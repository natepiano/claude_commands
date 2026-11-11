"""Placement decisions keep submitting owners separate from remote executions."""
from __future__ import annotations

from dataclasses import dataclass
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
class ConfiguredOrder:
    pass


@dataclass(frozen=True)
class PinnedHost:
    target: str


@dataclass(frozen=True)
class ScalerGateOrder:
    pass


HostSelection = ConfiguredOrder | PinnedHost | ScalerGateOrder


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
class PlacementCandidate:
    destination: PlacementDestination
    eligibility: AllowedHost | FixedExclusion = AllowedHost()


@dataclass(frozen=True)
class PlacementConstraints:
    source_key: str
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
    source_key: str
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
    owner: SubmittingOwner
    session: SubmittingSession
    host: str
    generation: str


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
class HostEvidenceKey:
    destination: PlacementDestination
    session: SubmittingSession
    command: tuple[str, ...]
    configuration_generation: str
    repository: str = ""
    package: str = ""
    step: str = ""


@dataclass(frozen=True)
class FreshHostEvidence:
    key: HostEvidenceKey
    observed_at: float
    generation: str
    eligibility: HostEligibility
    claims: tuple[AdoptedProviderClaim, ...] = ()


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
