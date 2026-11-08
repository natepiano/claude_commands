"""Persist cost evidence and spending decisions without making AWS calls."""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Callable, Generator, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time as day_time, timedelta
from decimal import Decimal
from enum import Enum
import fcntl
import json
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4
from typing import Literal, cast
from zoneinfo import ZoneInfo

from scripts.lib.atomic_file import write_text_atomic
from scripts.lib import conf
from scripts.message import session_send
from scripts.aws_target.config import (
    LimitSettingEvidence, MeasurementUnavailable, SpendingConfigurationUnavailable, SpendingSettings, money, offload_enabled, read_settings,
)

InstanceState = Literal["pending", "running", "stopping", "stopped", "shutting-down", "terminated", "unknown"]
SpendingPurpose = Literal["deployment", "start", "admission"]
ChargeKind = Literal["instance", "fixed", "explorer", "reconciliation"]


@dataclass(frozen=True)
class InstanceObservation:
    at: int
    state: InstanceState
    source: str


@dataclass(frozen=True)
class ComputeUnconfirmed:
    """The resource request has no confirmed result."""


@dataclass(frozen=True)
class ComputeInstanceConfirmed:
    at: int
    instance_id: str


@dataclass(frozen=True)
class ComputeResourcesAbsent:
    at: int


@dataclass(frozen=True)
class ComputeRequest:
    at: int
    stack: str
    region: str
    spot_max_usd_per_hour: Decimal
    request_id: str = field(default_factory=lambda: uuid4().hex)
    resolution: ComputeUnconfirmed | ComputeInstanceConfirmed | ComputeResourcesAbsent = ComputeUnconfirmed()


@dataclass(frozen=True)
class GateLaunchPending:
    """The launch has no confirmed instance identity."""


@dataclass(frozen=True)
class GateRunning:
    instance_id: str


@dataclass(frozen=True)
class GateAbsent:
    at: int


@dataclass(frozen=True)
class GateTerminated:
    at: int
    instance_id: str


@dataclass(frozen=True)
class GateReservation:
    attempt: str
    at: int
    expires: int
    usd_per_hour: Decimal
    allowance_usd: Decimal
    state: GateLaunchPending | GateRunning | GateAbsent | GateTerminated = GateLaunchPending()


@dataclass(frozen=True)
class GateReserved:
    reservation: GateReservation


@dataclass(frozen=True)
class GateCapReached:
    reason: str


@dataclass(frozen=True)
class PriceSample:
    at: int
    usd_per_hour: Decimal
    source: str


@dataclass(frozen=True)
class PricedResource:
    name: str
    usd_per_month: Decimal


@dataclass(frozen=True)
class ResourceInventory:
    at: int
    resources: tuple[PricedResource, ...]
    source: str


@dataclass(frozen=True)
class EstablishedDayCost:
    day: str
    usd: Decimal


@dataclass(frozen=True)
class CostReconciliation:
    at: int
    period_start: int
    period_end: int
    usd: Decimal
    source: str
    query_usd: Decimal = Decimal(0)
    established_days: tuple[EstablishedDayCost, ...] = field(default=(), compare=False)


@dataclass(frozen=True)
class ChargeRead:
    day: str
    at: int
    period_start: int
    period_end: int
    actual_usd: Decimal
    estimated_usd: Decimal
    query_usd: Decimal

    @property
    def difference_usd(self) -> Decimal:
        return self.actual_usd - self.estimated_usd


@dataclass(frozen=True)
class ChargedInterval:
    start: int
    end: int
    usd: Decimal
    source: str
    kind: ChargeKind
    confirmed_uptime_s: int = 0


@dataclass(frozen=True)
class DailyTotal:
    day: str
    usd: Decimal


@dataclass(frozen=True)
class MonthlyTotal:
    month: str
    usd: Decimal


@dataclass(frozen=True)
class CostEstimate:
    at: int
    daily: DailyTotal
    monthly: MonthlyTotal
    intervals: tuple[ChargedInterval, ...]
    inventory_at: int
    observation_at: int
    established_reconciliations: tuple[CostReconciliation, ...] = ()


@dataclass(frozen=True)
class CostEvidenceUnavailable:
    reason: str


@dataclass(frozen=True)
class SpendingPolicyUnavailable:
    reason: str


UNVALIDATED_SPENDING_POLICY = SpendingPolicyUnavailable("spending settings have not been validated")


@dataclass(frozen=True)
class GuardEvidence:
    purpose: SpendingPurpose
    limits: tuple[LimitSettingEvidence, ...]
    costs: CostEstimate | CostEvidenceUnavailable
    policy: SpendingSettings | SpendingPolicyUnavailable = UNVALIDATED_SPENDING_POLICY
    reserved_daily_fixed_usd: Decimal = Decimal(0)
    reserved_monthly_fixed_usd: Decimal = Decimal(0)


@dataclass(frozen=True)
class SpendingAllowed:
    at: int
    evidence: GuardEvidence


@dataclass(frozen=True)
class SpendingRefused:
    at: int
    reason: str
    evidence: GuardEvidence
    action: Literal["drain"] = "drain"


@dataclass(frozen=True)
class SpendingUnavailable:
    at: int
    reason: str
    evidence: GuardEvidence


SpendingDecision = SpendingAllowed | SpendingRefused | SpendingUnavailable


class DeliveryState(str, Enum):
    PENDING = "pending"
    DELIVERED = "delivered"


@dataclass(frozen=True)
class DeliveryUnowned:
    """No caller has reserved this notice for delivery."""


@dataclass(frozen=True)
class DeliveryOwner:
    """The caller that holds the delivery lock for this notice."""

    token: str


@dataclass(frozen=True)
class LimitEvent:
    event_id: str
    at: int
    period: str
    estimate_usd: Decimal
    limit_usd: Decimal
    recipient: str
    timeout_s: int
    delivery: DeliveryState = DeliveryState.PENDING
    retry_after: int = 0
    owner: DeliveryOwner | DeliveryUnowned = DeliveryUnowned()


@dataclass
class LedgerEvidence:
    observations: list[InstanceObservation] = field(default_factory=list)
    prices: list[PriceSample] = field(default_factory=list)
    inventories: list[ResourceInventory] = field(default_factory=list)
    reconciliations: list[CostReconciliation] = field(default_factory=list)
    events: list[LimitEvent] = field(default_factory=list)
    price_refresh_at: int = 0
    decision: dict[str, object] = field(default_factory=dict)
    charge_refresh_at: int = 0
    compute_requests: list[ComputeRequest] = field(default_factory=list)
    gate_reservations: list[GateReservation] = field(default_factory=list)


class ObservationRejected(ValueError):
    """The observation does not confirm a lifecycle state."""


class LedgerUnavailable(ValueError):
    """Evidence cannot establish a safe spending decision."""


def mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise LedgerUnavailable("expected an evidence record")
    return cast(dict[str, object], value)


def text(row: dict[str, object], key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value:
        raise LedgerUnavailable(f"{key}: expected nonempty text")
    return value


def instant(row: dict[str, object], key: str) -> int:
    value = row.get(key)
    if type(value) is not int or value < 0:
        raise LedgerUnavailable(f"{key}: expected nonnegative integer seconds")
    return value


def rows(row: dict[str, object], key: str) -> tuple[dict[str, object], ...]:
    value = row.get(key)
    if not isinstance(value, list):
        raise LedgerUnavailable(f"{key}: expected evidence records")
    return tuple(mapping(item) for item in cast(list[object], value))


def _decode(row: dict[str, object]) -> LedgerEvidence:
    if row.get("version") != 1:
        raise LedgerUnavailable("unsupported ledger version")
    observations: list[InstanceObservation] = []
    for record in rows(row, "observations"):
        state = text(record, "state")
        if state not in ("pending", "running", "stopping", "stopped", "shutting-down", "terminated", "unknown"):
            raise LedgerUnavailable("unknown instance state")
        observations.append(InstanceObservation(instant(record, "at"), state, text(record, "source")))
    evidence = LedgerEvidence(
        observations,
        [PriceSample(instant(r, "at"), money(text(r, "usd_per_hour")), text(r, "source")) for r in rows(row, "prices")],
        [ResourceInventory(instant(r, "at"), tuple(PricedResource(text(p, "name"), money(text(p, "usd_per_month")))
                                                 for p in rows(r, "resources")), text(r, "source"))
         for r in rows(row, "inventories")],
        [CostReconciliation(instant(r, "at"), instant(r, "period_start"), instant(r, "period_end"),
                            money(text(r, "usd")), text(r, "source"), money(str(r.get("query_usd", "0"))),
                            tuple(EstablishedDayCost(date.fromisoformat(text(d, "day")).isoformat(), money(text(d, "usd")))
                                  for d in rows(r, "established_days")) if "established_days" in r else ())
         for r in rows(row, "reconciliations")],
        [LimitEvent(text(r, "event_id"), instant(r, "at"), text(r, "period"), money(text(r, "estimate_usd")),
                    money(text(r, "limit_usd")), text(r, "recipient"), instant(r, "timeout_s"),
                    DeliveryState(r.get("delivery", "pending")),
                    instant(r, "retry_after") if "retry_after" in r else 0,
                    DeliveryOwner(text(r, "owner")) if r.get("owner") else DeliveryUnowned())
         for r in rows(row, "events")],
        instant(row, "price_refresh_at"), mapping(row["decision"]),
        instant(row, "charge_refresh_at") if "charge_refresh_at" in row else 0)
    for record in rows(row, 'compute_requests') if 'compute_requests' in row else ():
        resolution = mapping(record.get('resolution'))
        outcome = text(resolution, 'outcome')
        if outcome == 'unconfirmed':
            resolved = ComputeUnconfirmed()
        elif outcome == 'instance':
            resolved = ComputeInstanceConfirmed(instant(resolution, 'at'), text(resolution, 'instance_id'))
        elif outcome == 'absent':
            resolved = ComputeResourcesAbsent(instant(resolution, 'at'))
        else:
            raise LedgerUnavailable('unknown compute request outcome')
        request = ComputeRequest(instant(record, 'at'), text(record, 'stack'), text(record, 'region'),
                                 money(text(record, 'spot_max_usd_per_hour')), text(record, 'request_id'), resolved)
        if not isinstance(resolved, ComputeUnconfirmed) and resolved.at < request.at:
            raise LedgerUnavailable('compute reconciliation precedes its request')
        evidence.compute_requests.append(request)
    if len({r.request_id for r in evidence.compute_requests}) != len(evidence.compute_requests):
        raise LedgerUnavailable('duplicate compute request identity')
    if sum(isinstance(r.resolution, ComputeUnconfirmed) for r in evidence.compute_requests) > 1:
        raise LedgerUnavailable('multiple unconfirmed compute requests')
    for item in rows(row, 'gate_reservations') if 'gate_reservations' in row else ():
        status = mapping(item['state'])
        kind = text(status, 'kind')
        if kind == 'pending':
            gate_state = GateLaunchPending()
        elif kind == 'running':
            gate_state = GateRunning(text(status, 'instance_id'))
        elif kind == 'absent':
            gate_state = GateAbsent(instant(status, 'at'))
        elif kind == 'terminated':
            gate_state = GateTerminated(instant(status, 'at'), text(status, 'instance_id'))
        else:
            raise LedgerUnavailable('unknown gate reservation state')
        evidence.gate_reservations.append(GateReservation(
            text(item, 'attempt'), instant(item, 'at'), instant(item, 'expires'),
            money(text(item, 'usd_per_hour')), money(text(item, 'allowance_usd')), gate_state))
    if len({r.attempt for r in evidence.gate_reservations}) != len(evidence.gate_reservations):
        raise LedgerUnavailable('duplicate gate attempt')
    for inventory in evidence.inventories:
        names = [resource.name for resource in inventory.resources]
        if not names or len(set(names)) != len(names):
            raise LedgerUnavailable("expected distinct priced resources")
    states: dict[int, str] = {}
    for observation in evidence.observations:
        if observation.state == "unknown":
            continue
        if observation.at in states and states[observation.at] != observation.state:
            raise LedgerUnavailable("conflicting instance observations")
        states[observation.at] = observation.state
    prices: dict[int, Decimal] = {}
    for sample in evidence.prices:
        if sample.at in prices and prices[sample.at] != sample.usd_per_hour:
            raise LedgerUnavailable("conflicting price observations")
        prices[sample.at] = sample.usd_per_hour
    if any(reading.period_end <= reading.period_start or reading.period_end > reading.at
           for reading in evidence.reconciliations):
        raise LedgerUnavailable("invalid Cost Explorer period")
    return evidence


def _encode(state: LedgerEvidence) -> dict[str, object]:
    return {
        "version": 1,
        "observations": [{"at": r.at, "state": r.state, "source": r.source} for r in state.observations],
        "prices": [{"at": r.at, "usd_per_hour": str(r.usd_per_hour), "source": r.source} for r in state.prices],
        "inventories": [{"at": r.at, "source": r.source,
                         "resources": [{"name": p.name, "usd_per_month": str(p.usd_per_month)} for p in r.resources]}
                        for r in state.inventories],
        "reconciliations": [{"at": r.at, "period_start": r.period_start, "period_end": r.period_end,
                             "usd": str(r.usd), "source": r.source, "query_usd": str(r.query_usd),
                             "established_days": [{"day": d.day, "usd": str(d.usd)} for d in r.established_days]}
                            for r in state.reconciliations],
        "events": [{"event_id": r.event_id, "at": r.at, "period": r.period, "estimate_usd": str(r.estimate_usd),
                    "limit_usd": str(r.limit_usd), "recipient": r.recipient, "timeout_s": r.timeout_s,
                    "delivery": r.delivery.value, "retry_after": r.retry_after,
                    "owner": r.owner.token if isinstance(r.owner, DeliveryOwner) else ""} for r in state.events],
        "price_refresh_at": state.price_refresh_at, "decision": state.decision,
        "charge_refresh_at": state.charge_refresh_at,
        'gate_reservations': [
            {'attempt': r.attempt, 'at': r.at, 'expires': r.expires, 'usd_per_hour': str(r.usd_per_hour),
             'allowance_usd': str(r.allowance_usd), 'state':
             {'kind': 'pending'} if isinstance(r.state, GateLaunchPending) else
             {'kind': 'running', 'instance_id': r.state.instance_id} if isinstance(r.state, GateRunning) else
             {'kind': 'absent', 'at': r.state.at} if isinstance(r.state, GateAbsent) else
             {'kind': 'terminated', 'at': r.state.at, 'instance_id': r.state.instance_id}}
            for r in state.gate_reservations],
        "compute_requests": [
            {'at': r.at, 'stack': r.stack, 'region': r.region, 'spot_max_usd_per_hour': str(r.spot_max_usd_per_hour),
             'request_id': r.request_id,
             'resolution': {'outcome': 'unconfirmed'} if isinstance(r.resolution, ComputeUnconfirmed) else
             {'outcome': 'instance', 'at': r.resolution.at, 'instance_id': r.resolution.instance_id}
             if isinstance(r.resolution, ComputeInstanceConfirmed) else {'outcome': 'absent', 'at': r.resolution.at}}
            for r in state.compute_requests],
    }


def period_bounds(day: date, zone: ZoneInfo) -> tuple[int, int]:
    first = datetime.combine(day, day_time(), zone)
    return int(first.timestamp()), int((first + timedelta(days=1)).timestamp())


def charge_period_day(now: int) -> date:
    return datetime.fromtimestamp(now, ZoneInfo('UTC')).date() - timedelta(days=1)


def _days(start: int, end: int, zone: ZoneInfo) -> Iterator[tuple[int, int]]:
    while start < end:
        _, boundary = period_bounds(datetime.fromtimestamp(start, zone).date(), zone)
        stop = min(end, boundary)
        yield start, stop
        start = stop


def _month_seconds(at: int, zone: ZoneInfo) -> int:
    current = datetime.fromtimestamp(at, zone)
    first = current.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    following = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    return int(following.timestamp()) - int(first.timestamp())


def _overlap(charge: ChargedInterval, start: int, end: int) -> Decimal:
    if charge.start == charge.end:
        return charge.usd if start <= charge.start < end else Decimal(0)
    seconds = max(0, min(end, charge.end) - max(start, charge.start))
    return charge.usd * Decimal(seconds) / Decimal(charge.end - charge.start)


def _charges(state: LedgerEvidence, settings: SpendingSettings, now: int, *,
             current_window: bool = False, require_fresh: bool = True,
             window: date | None = None, pricing_end: int | None = None) -> CostEstimate:
    if type(now) is not int or now < 0:
        raise LedgerUnavailable("decision time must use nonnegative integer seconds")
    pricing_end = now if pricing_end is None else pricing_end
    if not state.inventories:
        raise LedgerUnavailable("a priced resource inventory is required before spending")
    inventories = sorted(state.inventories, key=lambda item: item.at)
    observations = sorted((r for r in state.observations if r.state != "unknown"), key=lambda item: item.at)
    prices = sorted(state.prices, key=lambda item: item.at)
    if any(r.at > pricing_end for r in (*inventories, *observations, *prices)) or (require_fresh and any(
            r.at > now for r in state.reconciliations)):
        raise LedgerUnavailable("cost evidence is from the future")
    if require_fresh and observations and now - observations[-1].at > settings.observation_max_age_seconds:
        raise LedgerUnavailable("instance state evidence is stale")
    local = datetime.fromtimestamp(now, settings.accounting_zone)
    month_start = int(local.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp())
    window_start = period_bounds(window, settings.accounting_zone)[0] if window is not None else month_start if current_window else 0
    requested_start = window_start
    # Include the complete periods needed by overlapping reconciliations.
    for reading in sorted(state.reconciliations, key=lambda r: r.period_end, reverse=True):
        if reading.period_end > window_start and reading.period_start < now:
            window_start = min(window_start, reading.period_start)
    relevant_readings = [r for r in state.reconciliations if r.period_end > window_start and r.period_start < now]
    intervals: list[ChargedInterval] = []
    inventory_index = max(0, bisect_right(inventories, window_start, key=lambda r: r.at) - 1)
    for index in range(inventory_index, len(inventories)):
        inventory = inventories[index]
        end = inventories[index + 1].at if index + 1 < len(inventories) else pricing_end
        monthly = max(settings.fixed_usd_per_month, sum((p.usd_per_month for p in inventory.resources), Decimal(0)))
        for start, stop in _days(max(inventory.at, window_start), end, settings.accounting_zone):
            intervals.append(ChargedInterval(start, stop, monthly * Decimal(stop - start)
                                             / Decimal(_month_seconds(start, settings.accounting_zone)), inventory.source, "fixed"))

    def fallback(start: int, end: int) -> None:
        for left, right in _days(max(start, window_start), end, settings.accounting_zone):
            intervals.append(ChargedInterval(left, right, settings.fallback_usd_per_hour * Decimal(right - left)
                                             / Decimal(3600), "unobserved instance time at configured fallback", "instance"))

    observation_index = max(0, bisect_right(observations, window_start, key=lambda r: r.at) - 1)
    for index in range(observation_index, len(observations)):
        observation = observations[index]
        end = observations[index + 1].at if index + 1 < len(observations) else pricing_end
        if end <= window_start:
            continue
        confirmed_end = min(end, observation.at + settings.observation_max_age_seconds)
        fallback(confirmed_end, end)
        if observation.state not in ("running", "pending", "stopping"):
            continue
        begin = max(observation.at, window_start)
        if begin >= confirmed_end:
            continue
        first_price = max(0, bisect_right(prices, begin, key=lambda p: p.at) - 1)
        last_price = bisect_left(prices, confirmed_end, key=lambda p: p.at)
        applicable_prices = prices[first_price:last_price]
        cuts = sorted({begin, confirmed_end, *(p.at for p in applicable_prices if begin < p.at < confirmed_end),
                       *(p.at + settings.price_max_age_seconds for p in applicable_prices
                         if begin < p.at + settings.price_max_age_seconds < confirmed_end)})
        for start, stop in zip(cuts, cuts[1:]):
            price_index = bisect_right(prices, start, key=lambda p: p.at) - 1
            if price_index >= 0 and start - prices[price_index].at < settings.price_max_age_seconds:
                price, source = prices[price_index].usd_per_hour, prices[price_index].source
            else:
                price, source = settings.fallback_usd_per_hour, "configured fallback_usd_per_hour"
            for left, right in _days(start, stop, settings.accounting_zone):
                intervals.append(ChargedInterval(left, right, price * Decimal(right - left) / Decimal(3600),
                                                 source, "instance", right - left))
    if observations and inventories[0].at < observations[0].at:
        fallback(inventories[0].at, observations[0].at)
    for request in state.compute_requests:
        if request.at > pricing_end:
            raise LedgerUnavailable('compute request evidence is from the future')
        start = max(request.at, window_start)
        end = pricing_end if isinstance(request.resolution, ComputeUnconfirmed) else min(pricing_end, request.resolution.at)
        if start >= end:
            continue
        rate = max(settings.fallback_usd_per_hour, request.spot_max_usd_per_hour)
        for left, right in _days(start, end, settings.accounting_zone):
            intervals.append(ChargedInterval(left, right, rate * Decimal(right - left)
                                             / Decimal(3600), 'unconfirmed compute request', 'instance'))
    for reservation in state.gate_reservations:
        if isinstance(reservation.state, GateAbsent):
            continue
        end = min(pricing_end, reservation.state.at) if isinstance(reservation.state, GateTerminated) else pricing_end
        for left, right in _days(max(reservation.at, window_start), end, settings.accounting_zone):
            intervals.append(ChargedInterval(left, right, reservation.usd_per_hour * Decimal(right - left)
                                             / Decimal(3600), 'gate:' + reservation.attempt, 'instance'))
        if window_start <= reservation.at < pricing_end:
            intervals.append(ChargedInterval(reservation.at, reservation.at, reservation.allowance_usd,
                                             'gate storage and transfer:' + reservation.attempt, 'fixed'))
    interval = settings.cost_explorer_interval_seconds
    origin = state.charge_refresh_at + interval if state.charge_refresh_at else inventories[0].at
    first_read = origin + max(0, (window_start - origin + interval - 1) // interval) * interval
    for at in range(first_read, pricing_end, interval):
        intervals.append(ChargedInterval(at, at, settings.cost_explorer_read_usd, "scheduled Cost Explorer read", "explorer"))
    if state.charge_refresh_at:
        for reading in relevant_readings:
            intervals.append(ChargedInterval(reading.period_start, reading.period_start, reading.query_usd,
                                             "confirmed Cost Explorer read", "explorer"))
    established_reconciliations: list[CostReconciliation] = []
    for reading in relevant_readings:
        if reading.period_end <= reading.period_start:
            raise LedgerUnavailable("invalid Cost Explorer period")
        estimated = period_cost((r for r in intervals if r.kind != "explorer"), reading.period_start, reading.period_end)
        extra = max(Decimal(0), reading.usd - estimated)
        established_days: list[EstablishedDayCost] = []
        previous = {d.day: d.usd for d in reading.established_days}
        for start, end in _days(reading.period_start, reading.period_end, settings.accounting_zone):
            day = datetime.fromtimestamp(start, settings.accounting_zone).date().isoformat()
            base = period_cost((r for r in intervals if r.kind != "explorer"), start, end)
            computed = base + extra * Decimal(end - start) / Decimal(reading.period_end - reading.period_start)
            established = max(computed, previous.get(day, Decimal(0)))
            established_days.append(EstablishedDayCost(day, established))
            intervals.append(ChargedInterval(start, end, established - base, reading.source, "reconciliation"))
        established_reconciliations.append(replace(reading, established_days=tuple(established_days)))
    if window is not None or pricing_end > now:
        intervals = [replace(r, start=max(r.start, requested_start), end=min(r.end, now),
                             usd=_overlap(r, requested_start, now),
                             confirmed_uptime_s=max(0, min(now, r.end) - max(requested_start, r.start))
                             if r.confirmed_uptime_s else 0) for r in intervals
                     if (r.end > requested_start or r.start == r.end >= requested_start) and r.start < now]
    day, month = local.date().isoformat(), local.strftime("%Y-%m")
    daily = sum((r.usd for r in intervals if datetime.fromtimestamp(r.start, settings.accounting_zone).date().isoformat() == day), Decimal(0))
    monthly_total = sum((r.usd for r in intervals if datetime.fromtimestamp(r.start, settings.accounting_zone).strftime("%Y-%m") == month), Decimal(0))
    return CostEstimate(now, DailyTotal(day, daily), MonthlyTotal(month, monthly_total), tuple(intervals),
                        inventories[-1].at, observations[-1].at if observations else 0, tuple(established_reconciliations))


def costs_from_evidence(state: LedgerEvidence, settings: SpendingSettings, now: int, *,
                        current_window: bool = True, window: date | None = None) -> CostEstimate:
    """Calculate report costs without publishing a decision or requiring fresh admission evidence."""
    reconciliations = [r for r in state.reconciliations if r.period_start < now]
    # Price complete reconciliation periods before restricting costs to the report window.
    pricing_end = max(now, max((r.period_end for r in reconciliations), default=now))
    snapshot = LedgerEvidence(
        [r for r in state.observations if r.at <= pricing_end], [r for r in state.prices if r.at <= pricing_end],
        [r for r in state.inventories if r.at <= pricing_end], reconciliations,
        charge_refresh_at=state.charge_refresh_at,
        compute_requests=[r for r in state.compute_requests if r.at <= pricing_end],
        gate_reservations=[r for r in state.gate_reservations if r.at <= pricing_end])
    return _charges(snapshot, settings, now, current_window=current_window, require_fresh=False,
                    window=window, pricing_end=pricing_end)


def period_cost(intervals: Sequence[ChargedInterval] | Iterator[ChargedInterval], start: int, end: int) -> Decimal:
    """Return the sum of charges inside the requested period."""
    return sum((_overlap(charge, start, end) for charge in intervals), Decimal(0))


def read_charge(path: Path) -> ChargeRead:
    row = mapping(cast(object, json.loads(path.read_text())))
    reading = ChargeRead(text(row, 'day'), instant(row, 'at'), instant(row, 'period_start'),
                         instant(row, 'period_end'), money(text(row, 'actual_usd')),
                         money(text(row, 'estimated_usd')), money(text(row, 'query_usd')))
    if reading.period_end <= reading.period_start or reading.period_end > reading.at:
        raise LedgerUnavailable('invalid charge reconciliation')
    return reading


def charge_record(reading: ChargeRead) -> dict[str, object]:
    return {'day': reading.day, 'at': reading.at, 'period_start': reading.period_start,
            'period_end': reading.period_end, 'actual_usd': str(reading.actual_usd),
            'estimated_usd': str(reading.estimated_usd), 'query_usd': str(reading.query_usd)}


def _reserved_fixed_costs(state: LedgerEvidence, settings: SpendingSettings, costs: CostEstimate) -> tuple[Decimal, Decimal]:
    """Reserve each period's known fixed costs before the first resource allocation."""
    local = datetime.fromtimestamp(costs.at, settings.accounting_zone)
    inventory = max(state.inventories, key=lambda item: item.at)
    monthly = max(settings.fixed_usd_per_month, sum((p.usd_per_month for p in inventory.resources), Decimal(0)))
    day_start, day_end = period_bounds(local.date(), settings.accounting_zone)
    denominator = Decimal(_month_seconds(costs.at, settings.accounting_zone))
    daily_accrued = sum((r.usd for r in costs.intervals if r.kind == "fixed"
                         and datetime.fromtimestamp(r.start, settings.accounting_zone).date() == local.date()), Decimal(0))
    monthly_accrued = sum((r.usd for r in costs.intervals if r.kind == "fixed"
                           and datetime.fromtimestamp(r.start, settings.accounting_zone).strftime("%Y-%m") == costs.monthly.month), Decimal(0))
    return (max(Decimal(0), monthly * Decimal(day_end - day_start) / denominator - daily_accrued),
            max(Decimal(0), monthly - monthly_accrued))


def gate_future_periods(state: LedgerEvidence, settings: SpendingSettings, now: int) -> tuple[dict[str, Decimal], dict[str, Decimal]]:
    """Reserve each open gate across all affected days and months."""
    daily: dict[str, Decimal] = {}
    monthly: dict[str, Decimal] = {}
    for reservation in state.gate_reservations:
        if isinstance(reservation.state, (GateAbsent, GateTerminated)):
            continue
        for left, right in _days(max(now, reservation.at), reservation.expires, settings.accounting_zone):
            cost = reservation.usd_per_hour * Decimal(right - left) / Decimal(3600)
            day = datetime.fromtimestamp(left, settings.accounting_zone).strftime('%Y-%m-%d')
            daily[day] = daily.get(day, Decimal(0)) + cost
            monthly[day[:7]] = monthly.get(day[:7], Decimal(0)) + cost
        if reservation.at >= now:
            day = datetime.fromtimestamp(reservation.at, settings.accounting_zone).strftime('%Y-%m-%d')
            daily[day] = daily.get(day, Decimal(0)) + reservation.allowance_usd
            monthly[day[:7]] = monthly.get(day[:7], Decimal(0)) + reservation.allowance_usd
    return daily, monthly


def gate_future_costs(state: LedgerEvidence, settings: SpendingSettings, now: int) -> tuple[Decimal, Decimal]:
    """Return open reservations for the current accounting periods."""
    daily, monthly = gate_future_periods(state, settings, now)
    day = datetime.fromtimestamp(now, settings.accounting_zone).strftime('%Y-%m-%d')
    return daily.get(day, Decimal(0)), monthly.get(day[:7], Decimal(0))


def send_limit_event(event: LimitEvent) -> bool:
    """Use the existing message interface after persisting the event identity."""
    # Keep the key active across every date that the message interface can represent.
    repeat_minutes = (datetime.max - datetime.min) // timedelta(minutes=1) + 1
    result = subprocess.run(session_send.send_argv("--to", event.recipient, "--from", "aws-spending-guard",
                             "--key", f"aws-limit:{event.event_id}", "--repeat-minutes", str(repeat_minutes), "--text",
                             f"AWS spending guard: {event.period} limit refuses new work; drain the target. "
                             + f"Estimate ${event.estimate_usd}; limit ${event.limit_usd}."),
                            capture_output=True, text=True, timeout=event.timeout_s, check=False)
    return result.returncode == 0 and result.stdout.startswith(("SENT:", "SKIPPED:"))


def decision_record(decision: SpendingDecision) -> dict[str, object]:
    evidence = decision.evidence
    record: dict[str, object] = {
        "at": decision.at, "purpose": evidence.purpose,
        "status": "allowed" if isinstance(decision, SpendingAllowed) else "refused" if isinstance(decision, SpendingRefused) else "unavailable",
        "reason": "" if isinstance(decision, SpendingAllowed) else decision.reason,
        "limits": [{"key": r.key, "origin": r.origin, "reason": r.reason} for r in evidence.limits],
        "reserved_daily_fixed_usd": str(evidence.reserved_daily_fixed_usd),
        "reserved_monthly_fixed_usd": str(evidence.reserved_monthly_fixed_usd),
    }
    if isinstance(evidence.costs, CostEstimate):
        costs = evidence.costs
        record.update({
            "daily_usd": str(costs.daily.usd), "monthly_usd": str(costs.monthly.usd),
            "inventory_at": costs.inventory_at, "observation_at": costs.observation_at,
            "intervals": [{"start": r.start, "end": r.end, "usd": str(r.usd), "source": r.source, "kind": r.kind}
                          for r in costs.intervals
                          if isinstance(evidence.policy, SpendingSettings)
                          and datetime.fromtimestamp(r.start, evidence.policy.accounting_zone).date().isoformat() == costs.daily.day],
        })
    else:
        record["costs_unavailable"] = evidence.costs.reason
    if isinstance(evidence.policy, SpendingSettings):
        record.update({"daily_limit_usd": str(evidence.policy.daily_limit_usd),
                       "monthly_limit_usd": str(evidence.policy.monthly_limit_usd),
                       "reserve_usd": str(evidence.policy.limit_reserve_usd)})
    return record


class CostLedger:
    def __init__(self, directory: Path) -> None:
        self.directory: Path = directory
        self.path: Path = directory / "cost-ledger.json"

    def _read(self) -> LedgerEvidence:
        """Read atomically published evidence without creating files or acquiring locks."""
        try:
            return _decode(mapping(cast(object, json.loads(self.path.read_text()))))
        except (OSError, ValueError, KeyError, TypeError, ArithmeticError) as error:
            raise LedgerUnavailable(f"ledger evidence is unavailable: {error}") from error

    def read_snapshot(self) -> LedgerEvidence:
        """Read the published evidence without creating coordination files."""
        return self._read()

    @contextmanager
    def _locked(self) -> Generator[LedgerEvidence]:
        self.directory.mkdir(parents=True, exist_ok=True)
        with (self.directory / "cost-ledger.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                state = self._read() if self.path.exists() else LedgerEvidence()
                yield state
            except (KeyError, TypeError, ArithmeticError, UnicodeError) as error:
                raise LedgerUnavailable(f"ledger evidence is invalid: {error}") from error
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _save(self, state: LedgerEvidence) -> None:
        document = _encode(state)
        _ = _decode(document)
        write_text_atomic(self.path, json.dumps(document, sort_keys=True) + "\n")

    def reserve_gate(self, settings_path: Path, request: GateReservation) -> GateReserved | GateCapReached:
        """Reserve gate costs under the shared Rust accounting lock."""
        with self._locked() as state:
            for previous in state.gate_reservations:
                if previous.attempt == request.attempt:
                    return GateReserved(previous)
            settings = read_settings(settings_path)
            costs = _charges(state, settings, request.at, current_window=True, require_fresh=False)
            state.gate_reservations.append(request)
            daily, monthly = gate_future_periods(state, settings, request.at)
            fixed_daily, fixed_monthly = _reserved_fixed_costs(state, settings, costs)
            for day, amount in daily.items():
                accrued = costs.daily.usd if day == costs.daily.day else Decimal(0)
                if accrued + amount + fixed_daily + settings.limit_reserve_usd >= settings.daily_limit_usd:
                    return GateCapReached('daily_limit')
            for month, amount in monthly.items():
                accrued = costs.monthly.usd if month == costs.monthly.month else Decimal(0)
                if accrued + amount + fixed_monthly + settings.limit_reserve_usd >= settings.monthly_limit_usd:
                    return GateCapReached('monthly_limit')
            self._save(state)
            return GateReserved(request)

    def resolve_gate(self, attempt: str, outcome: GateRunning | GateAbsent | GateTerminated) -> None:
        """Release a reservation only after AWS confirms its final state."""
        with self._locked() as state:
            state.gate_reservations = [replace(r, state=outcome) if r.attempt == attempt else r
                                       for r in state.gate_reservations]
            self._save(state)

    def observe(self, observation: InstanceObservation, *, confirms_transition: bool = False) -> None:
        if observation.state == "unknown":
            raise ObservationRejected("unknown does not confirm an instance lifecycle state")
        with self._locked() as state:
            conflicts = [r for r in state.observations if r.at == observation.at and r.state not in (observation.state, "unknown")]
            if conflicts:
                if not confirms_transition:
                    raise LedgerUnavailable("conflicting instance observations")
                state.observations = [r for r in state.observations if r not in conflicts]
            if observation not in state.observations:
                state.observations.append(observation)
            self._save(state)

    def pending_compute_requests(self) -> tuple[ComputeRequest, ...]:
        if not self.path.exists():
            return ()
        return tuple(r for r in self.read_snapshot().compute_requests if isinstance(r.resolution, ComputeUnconfirmed))

    def record_compute_request(self, request: ComputeRequest) -> None:
        with self._locked() as state:
            if any(isinstance(r.resolution, ComputeUnconfirmed) for r in state.compute_requests):
                raise LedgerUnavailable('reconcile the outstanding compute request before another deployment')
            state.compute_requests.append(request)
            self._save(state)

    def resolve_compute_request(self, request: ComputeRequest,
                                resolution: ComputeInstanceConfirmed | ComputeResourcesAbsent) -> None:
        with self._locked() as state:
            if request not in state.compute_requests or not isinstance(request.resolution, ComputeUnconfirmed):
                raise LedgerUnavailable('the compute request is no longer outstanding')
            state.compute_requests[state.compute_requests.index(request)] = replace(request, resolution=resolution)
            self._save(state)

    def record_price(self, sample: PriceSample) -> None:
        with self._locked() as state:
            if any(p.at == sample.at and p.usd_per_hour != sample.usd_per_hour for p in state.prices):
                raise LedgerUnavailable("conflicting price observations")
            if sample not in state.prices:
                state.prices.append(sample)
            self._save(state)

    def record_inventory(self, inventory: ResourceInventory) -> None:
        if not inventory.resources or len({p.name for p in inventory.resources}) != len(inventory.resources):
            raise LedgerUnavailable("expected distinct priced resources")
        with self._locked() as state:
            if any(r.at == inventory.at and r != inventory for r in state.inventories):
                raise LedgerUnavailable("conflicting resource inventories")
            if inventory not in state.inventories:
                state.inventories.append(inventory)
            self._save(state)

    def reconcile(self, reading: CostReconciliation) -> None:
        if reading.period_end <= reading.period_start:
            raise LedgerUnavailable("invalid Cost Explorer period")
        with self._locked() as state:
            if reading in state.reconciliations:
                return
            state.reconciliations.append(reading)
            self._save(state)

    def refresh_prices(self, settings_path: Path, now: int,
                       fetch: Callable[[int], Sequence[PriceSample]]) -> bool:
        """Ask the injected source for prices only when the configured interval expires."""
        settings = read_settings(settings_path)
        with self._locked() as state:
            if state.price_refresh_at and now - state.price_refresh_at < settings.price_refresh_seconds:
                return False
            for sample in fetch(now):
                if sample not in state.prices:
                    state.prices.append(sample)
            state.price_refresh_at = now
            self._save(state)
        return True

    def _reconcile_charge(self, state: LedgerEvidence, reading: ChargeRead) -> None:
        key = (reading.period_start, reading.period_end, 'Cost Explorer')
        established: dict[str, Decimal] = {}
        for saved in state.reconciliations:
            if (saved.period_start, saved.period_end, saved.source) == key:
                for day in saved.established_days:
                    established[day.day] = max(established.get(day.day, Decimal(0)), day.usd)
        reconciliation = CostReconciliation(reading.at, reading.period_start, reading.period_end,
                                             reading.actual_usd, 'Cost Explorer', reading.query_usd,
                                             tuple(EstablishedDayCost(day, usd) for day, usd in sorted(established.items())))
        if reconciliation in state.reconciliations and state.charge_refresh_at >= reading.at:
            return
        state.reconciliations = [r for r in state.reconciliations
                                 if (r.period_start, r.period_end, r.source) != key]
        state.reconciliations.append(reconciliation)
        state.charge_refresh_at = max(state.charge_refresh_at, reading.at)
        self._save(state)

    def refresh_charges(self, settings_path: Path, now: int,
                        fetch: Callable[[int, int], Decimal], *, gate_workload: bool = False) -> ChargeRead | MeasurementUnavailable:
        """Recover the newest saved reply before another scheduled query."""
        configuration = conf.read(settings_path)
        if not gate_workload and not offload_enabled(configuration):
            return MeasurementUnavailable('disabled')
        settings = read_settings(configuration)
        day = charge_period_day(now)
        start, end = period_bounds(day, ZoneInfo('UTC'))
        with self._locked() as state:
            newest = max((self.directory / 'charges').glob('*.json'), default=None)
            if newest is not None:
                previous = read_charge(newest)
                self._reconcile_charge(state, previous)
                if (previous.period_start, previous.period_end) == (start, end):
                    return previous
            if state.charge_refresh_at and now < state.charge_refresh_at + settings.cost_explorer_interval_seconds:
                return MeasurementUnavailable('charge read is not due')
            estimated = Decimal(0)
            if not gate_workload or not state.inventories or any(inventory.at <= end for inventory in state.inventories):
                costs = costs_from_evidence(state, settings, end,
                                            window=datetime.fromtimestamp(start, settings.accounting_zone).date())
                estimated = period_cost(costs.intervals, start, end)
            actual = fetch(start, end)
            if not actual.is_finite() or actual < 0:
                raise ValueError('Cost Explorer reply must contain finite nonnegative USD')
            reading = ChargeRead(day.isoformat(), now, start, end, actual, estimated, settings.cost_explorer_read_usd)
            path = self.directory / 'charges' / f'{day.isoformat()}.json'
            write_text_atomic(path, json.dumps(charge_record(reading), sort_keys=True) + '\n')
            self._reconcile_charge(state, reading)
            return reading

    def _save_estimate(self, state: LedgerEvidence, costs: CostEstimate) -> None:
        established = {r: r for r in costs.established_reconciliations}
        state.reconciliations = [established.get(r, r) for r in state.reconciliations]
        self._save(state)

    def estimate(self, settings: SpendingSettings, now: int) -> CostEstimate:
        with self._locked() as state:
            costs = _charges(state, settings, now)
            self._save_estimate(state, costs)
            return costs

    def decision_is_current(self, decision: SpendingDecision) -> bool:
        """Confirm that durable evidence contains this exact decision."""
        try:
            with self._locked() as state:
                return state.decision == decision_record(decision)
        except (OSError, ValueError):
            return False

    def permission_for_decision(self, decision: SpendingDecision) -> AdmissionPermission:
        if not self.decision_is_current(decision):
            return PermissionUnavailable(decision.at, 'current decision has no durable publication')
        return decode_permission(decision_record(decision))

    def _decide(self, state: LedgerEvidence, settings_path: Path,
                purpose: SpendingPurpose, now: int) -> SpendingDecision:
        evidence = GuardEvidence(purpose, (), CostEvidenceUnavailable("costs have not been validated"))
        try:
            settings = read_settings(settings_path)
            evidence = GuardEvidence(purpose, settings.limits, evidence.costs, settings)
            costs = _charges(state, settings, now, current_window=True)
            evidence = GuardEvidence(purpose, settings.limits, costs, settings)
            if purpose != "deployment" and not any(r.state != "unknown" for r in state.observations):
                raise LedgerUnavailable("instance state evidence is missing")
            daily_fixed, monthly_fixed = _reserved_fixed_costs(state, settings, costs)
            gate_daily, gate_monthly = gate_future_costs(state, settings, now)
            daily_fixed += gate_daily
            monthly_fixed += gate_monthly
            evidence = GuardEvidence(purpose, settings.limits, costs, settings, daily_fixed, monthly_fixed)
            reason = ""
            for period, key, total, limit in (
                ("daily", costs.daily.day, costs.daily.usd + daily_fixed, settings.daily_limit_usd),
                ("monthly", costs.monthly.month, costs.monthly.usd + monthly_fixed, settings.monthly_limit_usd),
            ):
                if total + settings.limit_reserve_usd < limit:
                    continue
                reason = reason or f"{period}_limit"
                event_id = f"{period}:{key}"
                if not any(event.event_id == event_id for event in state.events):
                    state.events.append(LimitEvent(event_id, now, period, total, limit,
                                                   settings.notification_recipient, settings.notification_timeout_s))
            return SpendingRefused(now, reason, evidence) if reason else SpendingAllowed(now, evidence)
        except SpendingConfigurationUnavailable as error:
            return SpendingUnavailable(now, str(error), GuardEvidence(purpose, error.limits, evidence.costs))
        except (OSError, ValueError, ArithmeticError) as error:
            return SpendingUnavailable(now, str(error), evidence)

    def _deliver_pending(self, settings_path: Path, clock: Callable[[], float],
                         notify: Callable[[LimitEvent], bool]) -> None:
        # The delivery lock prevents concurrent sends without holding the ledger lock.
        with (self.directory / "cost-notice.lock").open("a") as delivery_lock:
            try:
                fcntl.flock(delivery_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            try:
                retry_s = read_settings(settings_path).notification_retry_s
                with self._locked() as state:
                    pending = tuple(event for event in state.events
                                    if event.delivery is DeliveryState.PENDING)
                for candidate in pending:
                    owner = DeliveryOwner(uuid4().hex)
                    with self._locked() as state:
                        event = next(saved for saved in state.events if saved.event_id == candidate.event_id)
                        attempt_at = int(clock())
                        if event.delivery is not DeliveryState.PENDING or event.retry_after > attempt_at:
                            continue
                        event = replace(event, retry_after=attempt_at + retry_s, owner=owner)
                        state.events = [event if saved.event_id == event.event_id else saved for saved in state.events]
                        self._save(state)
                    try:
                        delivered = notify(event)
                    except Exception:
                        delivered = False
                    with self._locked() as state:
                        state.events = [replace(saved, owner=DeliveryUnowned(),
                                                delivery=DeliveryState.DELIVERED if delivered else DeliveryState.PENDING)
                                        if saved.event_id == event.event_id and saved.owner == owner else saved
                                        for saved in state.events]
                        self._save(state)
            finally:
                fcntl.flock(delivery_lock, fcntl.LOCK_UN)

    def guard(self, settings_path: Path, purpose: SpendingPurpose, now: int,
              notify: Callable[[LimitEvent], bool] = send_limit_event, *,
              clock: Callable[[], float] = time.monotonic) -> SpendingDecision:
        clock_started = clock()

        def delivery_time() -> float:
            return now + clock() - clock_started

        evidence = GuardEvidence(purpose, (), CostEvidenceUnavailable("ledger evidence is unavailable"))
        try:
            with self._locked() as state:
                decision = self._decide(state, settings_path, purpose, now)
                evidence = decision.evidence
                state.decision = decision_record(decision)
                if isinstance(evidence.costs, CostEstimate):
                    self._save_estimate(state, evidence.costs)
                else:
                    self._save(state)
        except (OSError, ValueError, ArithmeticError) as error:
            return SpendingUnavailable(now, str(error), evidence)
        try:
            self._deliver_pending(settings_path, delivery_time, notify)
        except (OSError, ValueError, ArithmeticError) as error:
            print(f"AWS limit notice remains pending: {error}", file=sys.stderr)
        return decision


def spending_guard(settings_path: Path, directory: Path, purpose: SpendingPurpose = "admission", *,
                   clock: Callable[[], float] = time.time,
                   notify: Callable[[LimitEvent], bool] = send_limit_event) -> SpendingDecision:
    return CostLedger(directory).guard(settings_path, purpose, int(clock()), notify, clock=clock)


@dataclass(frozen=True)
class PermissionAllowed:
    at: int
    evidence: dict[str, object]


@dataclass(frozen=True)
class PermissionRefused:
    at: int
    reason: str
    evidence: dict[str, object]


@dataclass(frozen=True)
class PermissionUnavailable:
    at: int
    reason: str
    evidence: dict[str, object] = field(default_factory=dict)


AdmissionPermission = PermissionAllowed | PermissionRefused | PermissionUnavailable


def permission_status(permission: AdmissionPermission) -> str:
    if isinstance(permission, PermissionAllowed):
        return 'allowed'
    if isinstance(permission, PermissionRefused):
        return 'refused'
    return 'unavailable'


def permission_record(permission: AdmissionPermission) -> dict[str, object]:
    """Encode a permission with the ledger's decision fields."""
    if permission.evidence:
        return permission.evidence
    if isinstance(permission, PermissionAllowed):
        return {}
    return {'at': permission.at, 'purpose': 'admission',
            'status': permission_status(permission),
            'reason': permission.reason}


def decode_permission(value: object) -> AdmissionPermission:
    """Decode the ledger decision without inferring permission from absent costs."""
    try:
        row = mapping(value)
        at = instant(row, 'at')
        if row.get('purpose') != 'admission':
            raise LedgerUnavailable('decision purpose is not admission')
        status = row.get('status')
        if status == 'unavailable':
            return PermissionUnavailable(at, text(row, 'reason'), row)
        if status == 'refused':
            return PermissionRefused(at, text(row, 'reason'), row)
        if status != 'allowed':
            raise LedgerUnavailable('unknown permission state')
        limits = rows(row, 'limits')
        if {r.get('key') for r in limits if r.get('origin') == 'live'} != {'daily_limit_usd', 'monthly_limit_usd'}:
            raise LedgerUnavailable('live spending limits are missing')
        _ = instant(row, 'inventory_at')
        _ = instant(row, 'observation_at')
        for key in ('daily_usd', 'monthly_usd', 'daily_limit_usd', 'monthly_limit_usd', 'reserve_usd'):
            _ = money(text(row, key))
        return PermissionAllowed(at, row)
    except (KeyError, TypeError, ValueError, ArithmeticError) as error:
        return PermissionUnavailable(0, f'invalid decision evidence: {error}')
