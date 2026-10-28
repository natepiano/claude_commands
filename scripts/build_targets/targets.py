#!/usr/bin/env python3
"""Read configured build targets and their recorded holds and runs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sys


SCRIPTS = Path(__file__).resolve().parent.parent
for directory in ("build_hold", "mac_test", "lint", "lib"):
    sys.path.insert(0, str(SCRIPTS / directory))
import build_hold
import mac_test
import offload
from target_config import (
    ConfigError as ConfigError,
    LocalTarget as LocalTarget,
    Platform as Platform,
    SshTarget as SshTarget,
    Target as Target,
    read_targets as read_targets,
)


@dataclass(frozen=True)
class AnyEligibleTarget:
    """The test may run on any target that supports its repository and package."""


@dataclass(frozen=True)
class LocalRequired:
    reason: str


Routing = AnyEligibleTarget | LocalRequired
ANY_ELIGIBLE_TARGET = AnyEligibleTarget()


@dataclass(frozen=True)
class EligibleTarget:
    target: Target


@dataclass(frozen=True)
class ExcludedTarget:
    target: Target
    reason: str


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
class RunningJob:
    """One live Mac claim: a job for one repository, or for the whole Mac."""

    what: str
    since: datetime
    idle: ClaimIdle


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
    results: list[EligibleTarget | ExcludedTarget] = []
    repository = offload.repository_identity(repo_root)
    for target in read_targets():
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
    record = claim.record
    since = mac_test.aware_timestamp(record["since"], claim.path, "since")
    if "idle_since" not in record:
        return RunningJob(record["what"], since, NOT_SEEN_IDLE)
    idle = mac_test.aware_timestamp(record["idle_since"], claim.path, "idle_since")
    return RunningJob(record["what"], since, IdleSince(idle))


def target_state(target: Target) -> TargetState:
    """Read state without probing a host or changing coordination files."""
    try:
        if isinstance(target, LocalTarget):
            holds = build_hold.natedev_holds(build_hold.holder_directory())
            if not holds.in_force:
                return Free()
            holder = holds.in_force[0]
            end = (
                ReleaseEstimated(holder.release.at)
                if isinstance(holder.release, build_hold.KnownReleaseEta)
                else UntilReleased()
            )
            return Held(holder.name, holder.purpose, holder.since, end)
        paths = mac_test.state_paths()
        claims = mac_test.read_claims(paths)
        block = mac_test.read_claim_block(paths)
        if not isinstance(block, mac_test.NoMacBlock):
            block_end = (
                ExpiresAt(block.expires)
                if block.expires > utc_now()
                else AwaitingBlockRemoval()
            )
            return Held(block.holder, block.reason, block.since, block_end)
        jobs = tuple(running_job(claim) for claim in claims if mac_test.run_is_live(claim.record))
        return Running(jobs) if jobs else Free()
    except build_hold.ReleaseRecordReadError as error:
        return StateUnreadable(build_hold.release_record_error_line(error))
    except (OSError, UnicodeError, mac_test.StateReadError) as error:
        return StateUnreadable(str(error))
