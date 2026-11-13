#!/usr/bin/env python3
"""Read configured build targets and their recorded holds and runs."""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass


from dataclasses import dataclass
from datetime import datetime, timezone
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
    from scripts.mac_test import mac_test


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
class AwaitingBlockRemoval:
    """A block remains enforced after its requested expiry."""


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
class Held:
    holder: str
    reason: str
    since: datetime
    end: HoldEnd
    holder_session: HolderSession | NoHolderSession = NO_HOLDER_SESSION


@dataclass(frozen=True)
class Unreachable:
    reason: str


@dataclass(frozen=True)
class StateUnreadable:
    reason: str


TargetState = Free | Running | Held | Unreachable | StateUnreadable


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
    from scripts.mac_test import offload

    results: list[EligibleTarget | ExcludedTarget] = []
    repository = offload.repository_identity(repo_root)
    for target in configured:
        if target.platform == "linux":
            results.append(EligibleTarget(target))
        elif isinstance(routing, LocalRequired):
            results.append(ExcludedTarget(target, routing.reason))
        elif isinstance(repository, offload.UnknownRepository):
            results.append(ExcludedTarget(target, "repo"))
        else:
            config = offload.read_config(target.settings) if isinstance(target, SshTarget) else offload.read_config()
            key = f"linux_only.{repository.name}"
            if key not in config.values:
                results.append(ExcludedTarget(target, "repo"))
            elif package in offload.comma_values(config.values[key]):
                results.append(ExcludedTarget(target, "linux_only"))
            else:
                results.append(EligibleTarget(target))
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
    """Read the local hold without importing remote claim readers."""
    from scripts.build_hold import build_hold

    try:
        holds = build_hold.natedev_holds(build_hold.holder_directory())
        if not holds.in_force:
            return Free()
        holder = holds.in_force[0]
        end = (
            ReleaseEstimated(holder.release.at)
            if isinstance(holder.release, build_hold.KnownReleaseEta)
            else UntilReleased()
        )
        return Held(holder.name, holder.purpose, holder.since, end,
                    HolderSession(build_hold.holder_session(holder.key)))
    except build_hold.ReleaseRecordReadError as error:
        return StateUnreadable(build_hold.release_record_error_line(error))
    except (OSError, UnicodeError, ConfigError) as error:
        return StateUnreadable(str(error))


def ssh_target_state(target: SshTarget) -> TargetState:
    """Read remote claim records without probing the target."""
    from scripts.build_hold import build_hold
    from scripts.mac_test import mac_test, offload

    try:
        paths = mac_test.state_paths(target.name)
        claims = mac_test.read_claims(paths)
        block = mac_test.read_claim_block(paths)
        if not isinstance(block, mac_test.NoMacBlock):
            block_end = (
                ExpiresAt(block.expires)
                if block.expires > utc_now()
                else AwaitingBlockRemoval()
            )
            session = (HolderSession(block.delivery.name)
                       if isinstance(block.delivery, mac_test.RecordedSession) else NO_HOLDER_SESSION)
            return Held(mac_test.holder_name(block.holder), block.reason, block.since, block_end, session)
        if isinstance(offload.read_failed_probe(target.name), offload.FailedProbe):
            return Unreachable("failed probe")
        jobs = tuple(running_job(claim) for claim in claims if mac_test.run_is_live(claim.record))
        if jobs:
            return Running(jobs)
        if isinstance(offload.read_backoff(utc_now().timestamp(), target.name), offload.ActiveBackoff):
            return Unreachable("retry backoff")
        return Free()
    except build_hold.ReleaseRecordReadError as error:
        return StateUnreadable(build_hold.release_record_error_line(error))
    except (OSError, UnicodeError, mac_test.StateReadError, ConfigError) as error:
        return StateUnreadable(str(error))


def target_state(target: Target) -> TargetState:
    """Read state without probing a host or changing coordination files."""
    if isinstance(target, LocalTarget):
        return local_hold_state()
    return ssh_target_state(target)
