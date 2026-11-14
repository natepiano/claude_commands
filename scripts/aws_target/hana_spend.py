"""Reserve Hana spending before a controller delivers a guest permit."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from scripts.aws_target import hana_guest, ledger
from scripts.lib import conf


@dataclass(frozen=True)
class PermitSettings:
    enabled: bool
    region: str
    instance_type: str
    spot_max_usd_per_hour: Decimal
    root_gib: int
    data_gib: int
    daily_budget_usd: Decimal
    gp3_usd_per_gib_month: Decimal
    permit_max_seconds: int
    guard_grace_seconds: int

    @property
    def storage_usd_per_month(self) -> Decimal:
        return (self.root_gib + self.data_gib) * self.gp3_usd_per_gib_month


def read_settings(configuration: conf.ConfigRead) -> PermitSettings:
    """Read workload limits without reading AWS credentials or deployment state."""
    safety = hana_guest.read_settings(configuration)
    values = PermitSettings(safety.enabled, configuration.text('region'), configuration.text('instance_type'),
        conf.decimal(configuration.text('spot_max_usd_per_hour')), configuration.whole('root_gib'), configuration.whole('data_gib'),
        conf.decimal(configuration.text('daily_budget_usd')), conf.decimal(configuration.text('gp3_usd_per_gib_month')),
        safety.permit_max_seconds, safety.guard_grace_seconds)
    if (not values.region or not values.instance_type or min(values.root_gib, values.data_gib) <= 0
            or any(not value.is_finite() or value <= 0 for value in
                   (values.spot_max_usd_per_hour, values.daily_budget_usd, values.gp3_usd_per_gib_month))):
        raise ValueError('Hana spending settings must have positive limits and complete identity')
    return values


@dataclass(frozen=True)
class PermitIssued:
    permit: hana_guest.WorkPermit
    exposure: ledger.HanaComputeExposure


@dataclass(frozen=True)
class PermitRefused:
    reason: str


@dataclass(frozen=True)
class StartReserved:
    exposure: ledger.HanaComputeExposure


@dataclass(frozen=True)
class StorageReserved:
    exposure: ledger.HanaStorageExposure


class HanaSpending:
    """Serialize permits and lifecycle evidence with the account ledger."""

    def __init__(self, book: ledger.CostLedger, account_settings: Path, settings: PermitSettings):
        self.book: ledger.CostLedger = book
        self.account_settings: Path = account_settings
        self.settings: PermitSettings = settings

    def _check(self, state: ledger.LedgerEvidence, now: int) -> PermitRefused | ledger.SpendingAllowed:
        decision = self.book.spending_decision(state, self.account_settings, now)
        if not isinstance(decision, ledger.SpendingAllowed):
            return PermitRefused(decision.reason)
        policy = decision.evidence.policy
        if isinstance(policy, ledger.SpendingPolicyUnavailable):
            return PermitRefused(policy.reason)
        daily, _monthly = ledger.spending_periods(ledger.hana_intervals(
            state, now, policy.box_minimum_seconds, reserve=True), ZoneInfo('UTC'))
        today = datetime.fromtimestamp(now, ZoneInfo('UTC')).date().isoformat()
        if any(usd > self.settings.daily_budget_usd for day, usd in daily.items() if day >= today):
            return PermitRefused('hana_daily_budget_usd')
        return decision

    def reserve_storage(self, now: int) -> StorageReserved | PermitRefused:
        """Reserve retained storage before provisioning the box."""
        if not self.settings.enabled:
            return PermitRefused('Hana is disabled')
        with self.book.transaction() as state:
            for row in state.hana_storage:
                if isinstance(row.state, ledger.HanaExposureOpen):
                    decision = self._check(state, now)
                    return decision if isinstance(decision, PermitRefused) else StorageReserved(row)
            proposed = ledger.HanaStorageExposure(now, self.settings.storage_usd_per_month)
            candidate = replace(state, hana_storage=[*state.hana_storage, proposed])
            decision = self._check(candidate, now)
            if isinstance(decision, PermitRefused):
                return decision
            state.hana_storage = candidate.hana_storage
            return StorageReserved(proposed)

    def reserve_start(self, now: int, expires: int) -> StartReserved | PermitRefused:
        """Reserve a demand start before its boot identity exists."""
        return self._reserve_compute(now, expires, expected_generation=0)

    def check_start(self, now: int, generation: int) -> StartReserved | PermitRefused:
        """Recheck an existing reservation before a start or permit mutation."""
        if not self.settings.enabled:
            return PermitRefused('Hana is disabled')
        with self.book.transaction() as state:
            for row in state.hana_compute:
                if row.generation == generation and isinstance(row.state, ledger.HanaExposureOpen) and now < row.expires:
                    decision = self._check(state, now)
                    return decision if isinstance(decision, PermitRefused) else StartReserved(row)
            return PermitRefused('The start reservation is expired or its generation is old')

    def bind_start(self, now: int, generation: int, boot_id: str) -> PermitIssued | PermitRefused:
        """Authorize an existing reservation after the controller verifies its boot identity."""
        if not boot_id.strip():
            return PermitRefused('The guest boot identity is missing')
        result = self.check_start(now, generation)
        if isinstance(result, PermitRefused):
            return result
        row = result.exposure
        return PermitIssued(hana_guest.WorkPermit(float(row.expires), str(generation), boot_id), row)

    def issue(self, now: int, expires: int, boot_id: str, *, expected_generation: int = 0) -> PermitIssued | PermitRefused:
        """Reserve a start or renewal before returning its increasing generation."""
        if not boot_id.strip():
            return PermitRefused('The guest boot identity is missing')
        result = self._reserve_compute(now, expires, expected_generation=expected_generation)
        if isinstance(result, PermitRefused):
            return result
        return PermitIssued(hana_guest.WorkPermit(float(expires), str(result.exposure.generation), boot_id), result.exposure)

    def _reserve_compute(self, now: int, expires: int, *, expected_generation: int) -> StartReserved | PermitRefused:
        if not self.settings.enabled:
            return PermitRefused('Hana is disabled')
        if type(now) is not int or type(expires) is not int or now < 0 or not 0 < expires - now <= self.settings.permit_max_seconds:
            return PermitRefused('The permit deadline is outside its renewal window')
        with self.book.transaction() as state:
            fence = state.hana_lifecycle_fence
            if isinstance(fence, ledger.HanaLifecycleFence) and now < fence.at:
                return PermitRefused('The permit time precedes the current lifecycle fence')
            active = [row for row in state.hana_compute if isinstance(row.state, ledger.HanaExposureOpen)]
            if expected_generation:
                if not active or active[0].generation != expected_generation:
                    return PermitRefused('The permit generation is old')
                if now >= active[0].expires or expires <= active[0].expires:
                    return PermitRefused('The permit expired or its deadline did not increase')
            elif active:
                return PermitRefused('Confirm the previous stop before another start')
            generation = max((row.generation for row in state.hana_compute), default=0) + 1
            proposed = ledger.HanaComputeExposure(active[0].at if active else now, expires, generation,
                max(active[0].usd_per_hour, self.settings.spot_max_usd_per_hour) if active else self.settings.spot_max_usd_per_hour)
            compute = [row for row in state.hana_compute if isinstance(row.state, ledger.HanaExposureClosed)]
            storage = list(state.hana_storage)
            if not any(isinstance(row.state, ledger.HanaExposureOpen) for row in storage):
                storage.append(ledger.HanaStorageExposure(now, self.settings.storage_usd_per_month))
            candidate = replace(state, hana_compute=[*compute, proposed], hana_storage=storage)
            decision = self._check(candidate, now)
            if isinstance(decision, PermitRefused):
                return decision
            state.hana_compute, state.hana_storage = candidate.hana_compute, candidate.hana_storage
            state.hana_lifecycle_fence = ledger.HanaLifecycleFence(now, generation)
            return StartReserved(proposed)

    def reconcile(self, now: int, observed: ledger.InstanceState, *, generation: int) -> None:
        """Apply an observation only to its generation and after the saved lifecycle time."""
        if type(now) is not int or now < 0:
            raise ValueError('The observation time must use nonnegative integer seconds')
        if type(generation) is not int or generation < 0:
            raise ValueError('The observed generation must use a nonnegative integer')
        with self.book.transaction() as state:
            current_generation = max((row.generation for row in state.hana_compute), default=0)
            if generation != current_generation:
                return
            fence = state.hana_lifecycle_fence
            if isinstance(fence, ledger.HanaLifecycleFence) and now <= fence.at:
                return
            latest = max((row.at for row in (*state.hana_compute, *state.hana_storage)), default=0)
            latest = max([latest, *(row.state.at for row in (*state.hana_compute, *state.hana_storage)
                                    if isinstance(row.state, ledger.HanaExposureClosed))])
            if now < latest:
                return
            if observed in ('stopped', 'terminated'):
                if isinstance(fence, ledger.HanaLifecycleUnfenced) and state.hana_compute:
                    state.hana_lifecycle_fence = ledger.HanaLifecycleFence(now, current_generation)
                    return
                state.hana_compute = [replace(row, state=ledger.HanaExposureClosed(now))
                    if isinstance(row.state, ledger.HanaExposureOpen) else row for row in state.hana_compute]
            elif observed in ('pending', 'running', 'stopping', 'shutting-down'):
                if not any(isinstance(row.state, ledger.HanaExposureOpen) for row in state.hana_compute):
                    current_generation += 1
                    state.hana_compute.append(ledger.HanaComputeExposure(now, now + self.settings.guard_grace_seconds,
                        current_generation, self.settings.spot_max_usd_per_hour))
                if not any(isinstance(row.state, ledger.HanaExposureOpen) for row in state.hana_storage):
                    state.hana_storage.append(ledger.HanaStorageExposure(now, self.settings.storage_usd_per_month))
            state.hana_lifecycle_fence = ledger.HanaLifecycleFence(now, current_generation)

    def confirm_storage_deleted(self, now: int) -> None:
        """Release storage only after the controller confirms volume deletion."""
        with self.book.transaction() as state:
            if any(isinstance(row.state, ledger.HanaExposureOpen) for row in state.hana_compute):
                raise ValueError('Confirm the stop before storage deletion')
            if any(now < row.at for row in state.hana_storage):
                raise ValueError('Storage deletion precedes its creation')
            state.hana_storage = [replace(row, state=ledger.HanaExposureClosed(now))
                if isinstance(row.state, ledger.HanaExposureOpen) else row for row in state.hana_storage]
