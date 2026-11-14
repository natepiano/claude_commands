#!/usr/bin/env python3
"""Read configured build targets and their recorded holds and runs."""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass


from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from typing import TYPE_CHECKING

from scripts.build_targets.target_config import (
    AnyEligibleTarget as AnyEligibleTarget,
    LocalRequired as LocalRequired,
    Routing as Routing,
    ANY_ELIGIBLE_TARGET as ANY_ELIGIBLE_TARGET,
    EligibleTarget as EligibleTarget,
    ExcludedTarget as ExcludedTarget,
    ConfigError as ConfigError,
    LocalTarget as LocalTarget,
    Platform as Platform,
    SshTarget as SshTarget,
    Target as Target,
    read_targets as read_targets,
)


if TYPE_CHECKING:
    from scripts.mac_test import eligibility, mac_test


@dataclass(frozen=True)
class ExpiresAt:
    when: datetime


@dataclass(frozen=True)
class ReleaseEstimated:
    when: datetime


@dataclass(frozen=True)
class UntilReleased:
    pass


@dataclass(frozen=True)
class ExpiryNotRecorded:
    pass


@dataclass(frozen=True)
class AwaitingBlockRemoval:
    """A block remains enforced after its requested expiry."""
    requested_expiry: datetime | ExpiryNotRecorded = ExpiryNotRecorded()


HoldEnd = ExpiresAt | ReleaseEstimated | UntilReleased | AwaitingBlockRemoval


@dataclass(frozen=True)
class Free:
    pass


@dataclass(frozen=True)
class IdleSince:
    """The claim watcher has seen the Mac idle since this time."""

    when: datetime


@dataclass(frozen=True)
class NotSeenIdle:
    """The claim watcher has not seen the Mac idle during this claim."""


ClaimIdle = IdleSince | NotSeenIdle
NOT_SEEN_IDLE = NotSeenIdle()


@dataclass(frozen=True)
class HolderSession:
    """The Claude session recorded by the claim or hold."""

    session_id: str


@dataclass(frozen=True)
class NoHolderSession:
    """The claim or hold did not record a Claude session."""


NO_HOLDER_SESSION = NoHolderSession()


@dataclass(frozen=True)
class RunningJob:
    """One live Mac claim: a job for one repository, or for the whole Mac."""

    what: str
    since: datetime
    idle: ClaimIdle
    holder_session: HolderSession | NoHolderSession = NO_HOLDER_SESSION


@dataclass(frozen=True)
class Running:
    """Every live Mac claim, oldest first."""

    jobs: tuple[RunningJob, ...]


@dataclass(frozen=True)
class StartNotRecorded:
    pass


@dataclass(frozen=True)
class Held:
    holder: str
    reason: str
    since: datetime | StartNotRecorded
    end: HoldEnd
    holder_session: HolderSession | NoHolderSession = NO_HOLDER_SESSION


@dataclass(frozen=True)
class Unreachable:
    reason: str


@dataclass(frozen=True)
class StateUnreadable:
    reason: str


TargetState = Free | Running | Held | Unreachable | StateUnreadable


@dataclass(frozen=True)
class TargetSnapshot:
    target: Target
    states: tuple[TargetState, ...]
    claims: tuple[mac_test.HeldClaim, ...]
    backoff: eligibility.BackoffState


def target_eligibility(
    repo_root: Path, package: str, routing: Routing
) -> tuple[EligibleTarget | ExcludedTarget, ...]:
    """Keep configured order while excluding Mac targets the test cannot use."""
    configured = read_targets()
    if all(target.platform == "linux" for target in configured):
        return tuple(EligibleTarget(target) for target in configured)
    return mac_policy_eligibility(repo_root, package, routing, configured)


def mac_policy_eligibility(
    repo_root: Path, package: str, routing: Routing, configured: tuple[Target, ...]
) -> tuple[EligibleTarget | ExcludedTarget, ...]:
    """Read repository policy when a configured target uses macOS."""
    from scripts.mac_test import eligibility

    results: list[EligibleTarget | ExcludedTarget] = []
    repository = eligibility.repository_identity(repo_root)
    for target in configured:
        if target.platform == "linux":
            results.append(EligibleTarget(target))
        elif isinstance(routing, LocalRequired):
            results.append(ExcludedTarget(target, routing.reason))
        elif isinstance(repository, eligibility.UnknownRepository):
            results.append(ExcludedTarget(target, "repo"))
        else:
            config = eligibility.read_config(target.settings) if isinstance(target, SshTarget) else eligibility.read_config()
            policy = eligibility.linux_only_policy(config, repository, repo_root, package)
            results.append(ExcludedTarget(target, policy.reason) if isinstance(policy, eligibility.IneligibleMacStep)
                           else EligibleTarget(target))
    return tuple(results)


def utc_now() -> datetime:

    return datetime.now(timezone.utc)


def running_job(claim: mac_test.HeldClaim) -> RunningJob:
    from scripts.mac_test import mac_test

    record = claim.record
    since = mac_test.aware_timestamp(record["since"], claim.path, "since")
    session_id = record.get("holder_session", "")
    holder = HolderSession(session_id) if session_id else NO_HOLDER_SESSION
    if "idle_since" not in record:
        return RunningJob(record["what"], since, NOT_SEEN_IDLE, holder)
    idle = mac_test.aware_timestamp(record["idle_since"], claim.path, "idle_since")
    return RunningJob(record["what"], since, IdleSince(idle), holder)


def local_hold_state() -> TargetState:
    return local_hold_states()[0]


def local_hold_states() -> tuple[TargetState, ...]:
    """Read the local hold without importing remote claim readers."""
    from scripts.build_hold import build_hold

    try:
        holds = build_hold.natedev_holds(build_hold.holder_directory())
        if not holds.in_force:
            return (Free(),)
        return tuple(Held(holder.name, holder.purpose,
            holder.since if holder.start_recorded else StartNotRecorded(),
            ReleaseEstimated(holder.release.at) if isinstance(holder.release, build_hold.KnownReleaseEta) else UntilReleased(),
            HolderSession(build_hold.holder_session(holder.key))) for holder in holds.in_force)
    except build_hold.ReleaseRecordReadError as error:
        return (StateUnreadable(build_hold.release_record_error_line(error)),)
    except (OSError, UnicodeError, ConfigError) as error:
        return (StateUnreadable(str(error)),)


def ssh_target_state(target: SshTarget) -> TargetState:
    return ssh_target_snapshot(target).states[0]


def ssh_target_snapshot(target: SshTarget) -> TargetSnapshot:
    """Read remote claim, block and backoff records once, without probing the target."""
    from scripts.build_hold import build_hold
    from scripts.mac_test import eligibility, mac_test

    claims: tuple[mac_test.HeldClaim, ...] = ()
    backoff: eligibility.BackoffState = eligibility.NO_BACKOFF

    def snapshot(state: TargetState) -> TargetSnapshot:
        return TargetSnapshot(target, (state,), claims, backoff)

    try:
        paths = mac_test.state_paths(target.name)
        claims = tuple(mac_test.read_claims(paths))
        block = mac_test.read_claim_block(paths)
        backoff = eligibility.read_backoff(utc_now().timestamp(), target.name, strict=True)
        if not isinstance(block, mac_test.NoMacBlock):
            block_end = ExpiresAt(block.expires) if block.expires > utc_now() else AwaitingBlockRemoval(block.expires)
            session = (HolderSession(block.delivery.name)
                       if isinstance(block.delivery, mac_test.RecordedSession) else NO_HOLDER_SESSION)
            return snapshot(Held(mac_test.holder_name(block.holder), block.reason, block.since, block_end, session))
        if isinstance(eligibility.read_failed_probe(target.name), eligibility.FailedProbe):
            return snapshot(Unreachable("failed probe"))
        jobs = tuple(running_job(claim) for claim in claims if mac_test.run_is_live(claim.record))
        if jobs:
            return snapshot(Running(jobs))
        if isinstance(backoff, eligibility.UnreadableBackoff):
            return snapshot(StateUnreadable("retry backoff: " + backoff.reason))
        if isinstance(backoff, eligibility.ActiveBackoff):
            return snapshot(Unreachable("retry backoff"))
        return snapshot(Free())
    except build_hold.ReleaseRecordReadError as error:
        return snapshot(StateUnreadable(build_hold.release_record_error_line(error)))
    except (OSError, UnicodeError, mac_test.StateReadError, ConfigError) as error:
        return snapshot(StateUnreadable(str(error)))


def target_snapshots() -> tuple[TargetSnapshot, ...]:
    """Read coordination evidence once for one page reading."""
    from scripts.mac_test import eligibility

    return tuple(TargetSnapshot(target, local_hold_states(), (), eligibility.NO_BACKOFF)
                 if isinstance(target, LocalTarget) else ssh_target_snapshot(target)
                 for target in read_targets())


def start_text(since: datetime | StartNotRecorded, zone: tzinfo) -> str:
    """Name a hold's recorded start, or say that none was recorded."""
    return "start not recorded" if isinstance(since, StartNotRecorded) else "since " + clock(since, zone)


def end_text(end: HoldEnd, zone: tzinfo) -> str:
    """Name a fixed expiry, an estimated release, an open hold or an expired block."""
    if isinstance(end, ExpiresAt):
        return "until " + clock(end.when, zone)
    if isinstance(end, ReleaseEstimated):
        return "expected release " + clock(end.when, zone)
    if isinstance(end, AwaitingBlockRemoval):
        expiry = end.requested_expiry
        return "awaiting unblock" + (f"; requested expiry was {clock(expiry, zone)}" if isinstance(expiry, datetime) else "")
    return "until released"


def clock(instant: datetime, zone: tzinfo) -> str:
    return instant.astimezone(zone).strftime("%H:%M %Z")


def target_state(target: Target) -> TargetState:
    """Read state without probing a host or changing coordination files."""
    if isinstance(target, LocalTarget):
        return local_hold_state()
    return ssh_target_state(target)
