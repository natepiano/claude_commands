#!/usr/bin/env python3
"""Read configured build targets and their recorded holds and runs."""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass


import json
import time
from calendar import monthrange
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, cast

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
    is_aws_target,
)


if TYPE_CHECKING:
    from scripts.aws_target import ledger as cost_ledger, measurement
    from scripts.buildlog import record
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


@dataclass(frozen=True)
class RepositorySlots:
    """Occupied capacity belongs to one repository, including retained remote ownership."""

    repository: str
    capacity: int
    active: int
    retained: int


    @property
    def occupied(self) -> int:
        return self.active + self.retained


@dataclass(frozen=True)
class RemovalComplete:
    """No incomplete removal record remains."""


@dataclass(frozen=True)
class RemovalPending:
    resources: tuple[str, ...]
    storage_usd_per_day: Decimal


REMOVAL_COMPLETE = RemovalComplete()


@dataclass(frozen=True)
class AwsStatusRow:
    target: str
    state: str
    evidence_at: int
    reason: str
    spending: str
    decision_at: int
    monthly_usd: Decimal | measurement.MeasurementUnavailable
    monthly_limit_usd: Decimal | measurement.MeasurementUnavailable
    slots: tuple[RepositorySlots, ...]
    results: tuple[record.AttemptResult, ...]
    notices: tuple[cost_ledger.LimitEvent, ...]
    starting_at: int
    recovery: measurement.ControllerRecovery
    removal: RemovalComplete | RemovalPending = REMOVAL_COMPLETE


def read_aws_status(target: SshTarget) -> AwsStatusRow | StateUnreadable:
    """Read local controller, ledger, claim, and result evidence without probes or writes."""
    from scripts.aws_target import ledger as cost_ledger, measurement
    from scripts.buildlog import record
    from scripts.mac_test import mac_test

    try:
        paths = mac_test.state_paths(target.name)
        snapshot = measurement.read_status_measurement(target.settings, paths.directory, int(time.time()))
        if isinstance(snapshot, measurement.MeasurementUnavailable):
            return StateUnreadable(snapshot.reason)
        claims = tuple(claim for claim in mac_test.read_claims(paths) if mac_test.run_is_live(claim.record))
        repositories: dict[mac_test.ClaimScope, list[mac_test.HeldClaim]] = {}
        for claim in claims:
            scope = replace(claim.scope, slot=0) if isinstance(claim.scope, mac_test.RepositoryScope) else claim.scope
            repositories.setdefault(scope, []).append(claim)
        slots: list[RepositorySlots] = []
        for scope, held in repositories.items():
            active = sum(1 for claim in held if not mac_test.run_is_kept(claim.record)
                         and not claim.record.get('cleanup_pending'))
            capacity = mac_test.slot_capacity(paths, scope)
            name = scope.name if isinstance(scope, mac_test.RepositoryScope) else 'unknown'
            slots.append(RepositorySlots(name, capacity, active, len(held) - active))
        slots.sort(key=lambda row: row.repository)
        permission = snapshot.permission
        spending = cost_ledger.permission_status(permission) if snapshot.enabled else 'disabled'
        costs = snapshot.costs
        monthly = costs.monthly.usd if isinstance(costs, cost_ledger.CostEstimate) else measurement.MeasurementUnavailable(costs.reason)
        reason = snapshot.controller.reason
        if not isinstance(permission, cost_ledger.PermissionAllowed):
            reason = '; '.join(filter(None, (reason, permission.reason)))
        removal: RemovalComplete | RemovalPending = REMOVAL_COMPLETE
        removal_path = paths.directory / 'down.json'
        if removal_path.exists():
            value = cast(object, json.loads(removal_path.read_text()))
            if not isinstance(value, dict):
                raise ValueError('down.json: expected a removal record')
            saved = cast(dict[str, object], value)
            resources = tuple(str(saved[key]) for key in ('volume', 'budget') if saved.get(key))
            inventory = cost_ledger.CostLedger(paths.directory).read_snapshot().inventories
            names = ('retained_storage_usd_per_month', 'gp3_iops_usd_per_month', 'gp3_throughput_usd_per_month')
            monthly_storage = sum((r.usd_per_month for r in inventory[-1].resources if r.name in names), Decimal(0)) if inventory else Decimal(0)
            now = datetime.fromtimestamp(time.time(), timezone.utc)
            removal = RemovalPending(resources, monthly_storage / monthrange(now.year, now.month)[1])
        return AwsStatusRow(target.name, snapshot.controller.state, snapshot.controller.at, reason, spending,
                            permission.at, monthly, snapshot.monthly_limit_usd, tuple(slots),
                            record.read_attempt_results(target.name), snapshot.events, snapshot.controller.starting_at, snapshot.controller.recovery, removal)
    except (OSError, ValueError, mac_test.StateReadError, ConfigError) as error:
        return StateUnreadable(str(error))


TargetState = Free | Running | Held | Unreachable | StateUnreadable | AwsStatusRow


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
        return Held(holder.name, holder.purpose, holder.since, end, HolderSession(holder.key))
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
            return Held(block.holder, block.reason, block.since, block_end, session)
        if isinstance(offload.read_failed_probe(target.name), offload.FailedProbe):
            return Unreachable("failed probe")
        if is_aws_target(target):
            return read_aws_status(target)
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
