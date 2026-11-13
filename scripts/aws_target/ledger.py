"""Persist cost evidence and spending decisions without making AWS calls."""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Callable, Generator, Iterator, Mapping, Sequence
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
    LimitSettingEvidence, MeasurementUnavailable, SpendingConfigurationUnavailable, SpendingSettings, money, read_settings,
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
class BoxRate:
    usd_per_hour: Decimal
    source: str


@dataclass(frozen=True)
class GateAllowance:
    usd: Decimal
    source: str


@dataclass(frozen=True)
class GateAllowanceUnpriced:
    """Bill pricing has not established the completed gate's allowance."""


@dataclass(frozen=True)
class GateOwnerUnknown:
    """No worker identity establishes whether the attempt is active."""


@dataclass(frozen=True)
class GateWorkerOwner:
    pid: int
    start: str
    folder: str


@dataclass(frozen=True)
class NoGateTrial:
    pass


@dataclass(frozen=True)
class GateTrialBudget:
    trial_id: str
    limit_usd: Decimal


@dataclass(frozen=True)
class GateReservation:
    attempt: str
    at: int
    expires: int
    usd_per_hour: Decimal
    allowance_usd: Decimal
    state: GateLaunchPending | GateRunning | GateAbsent | GateTerminated = GateLaunchPending()
    instance_type: str = ''
    rate: BoxRate = BoxRate(Decimal(0), 'not priced')
    region: str = ''
    day_rates: tuple[tuple[str, BoxRate], ...] = ()
    booked_allowance: GateAllowance | GateAllowanceUnpriced = GateAllowanceUnpriced()
    owner: GateWorkerOwner | GateOwnerUnknown = GateOwnerUnknown()
    mode: str = ''
    trial: GateTrialBudget | NoGateTrial = NoGateTrial()
    root_gib: int = 0


def gate_is_open(reservation: GateReservation) -> bool:
    """Keep pending and running reservations open until AWS confirms closure."""
    return isinstance(reservation.state, (GateLaunchPending, GateRunning))


@dataclass(frozen=True)
class GateReserved:
    reservation: GateReservation


@dataclass(frozen=True)
class GateAttemptEnded:
    reason: str = 'The attempt booking has ended; use a new attempt id'


@dataclass(frozen=True)
class GateCapReached:
    reason: str


@dataclass(frozen=True)
class PriceSample:
    at: int
    usd_per_hour: Decimal
    source: str


@dataclass(frozen=True)
class RegionalSpotPrice:
    region: str
    zone: str
    instance_type: str
    usd_per_hour: Decimal
    read_at: int


def regional_spot_prices(region: str, reply: dict[str, object], sizes: Sequence[str],
                         read_at: int) -> tuple[RegionalSpotPrice, ...]:
    """Select the latest Linux/UNIX price for each requested type and zone."""
    if reply.get('NextToken'):
        raise ValueError('The regional Spot price reply is incomplete')
    latest: dict[tuple[str, str], tuple[float, RegionalSpotPrice]] = {}
    for row in rows(reply, 'SpotPriceHistory'):
        size, zone = text(row, 'InstanceType'), text(row, 'AvailabilityZone')
        if size not in sizes:
            continue
        at = datetime.fromisoformat(text(row, 'Timestamp').replace('Z', '+00:00')).timestamp()
        price = money(text(row, 'SpotPrice'))
        if not zone or price < 0:
            raise ValueError('The regional Spot price reply has invalid rows')
        key = (zone, size)
        if key not in latest or at > latest[key][0]:
            latest[key] = (at, RegionalSpotPrice(region, zone, size, price, read_at))
    return tuple(price for _, price in latest.values())


def read_spot_prices(directory: Path) -> tuple[RegionalSpotPrice, ...]:
    """Read the complete regional price snapshot, or return no rows when absent."""
    try:
        document = mapping(cast(object, json.loads((directory / 'spot-prices.json').read_text())))
    except FileNotFoundError:
        return ()
    prices = tuple(RegionalSpotPrice(text(row, 'region'), text(row, 'zone'), text(row, 'instance_type'),
        money(text(row, 'usd_per_hour')), instant(row, 'read_at')) for row in rows(document, 'prices'))
    if any(not row.region or not row.zone or not row.instance_type or row.usd_per_hour < 0 or row.read_at < 0 for row in prices):
        raise ValueError('The regional Spot price snapshot has invalid rows')
    return prices


def write_spot_prices(directory: Path, prices: Sequence[RegionalSpotPrice]) -> None:
    """Replace the complete regional price snapshot atomically."""
    document = {'prices': [dict(region=row.region, zone=row.zone, instance_type=row.instance_type,
        usd_per_hour=str(row.usd_per_hour), read_at=row.read_at) for row in prices]}
    write_text_atomic(directory / 'spot-prices.json', json.dumps(document, sort_keys=True) + '\n')


def recent_gate_launches(directory: Path, since: float) -> tuple[dict[str, object], ...]:
    """Read backwards in 64 KiB blocks until the first row before the window."""
    selected: list[dict[str, object]] = []
    try:
        with (directory / 'cloud-gate-launches.jsonl').open('rb') as stream:
            position = stream.seek(0, 2)
            pending = b''
            while position:
                count = min(position, 65536)
                position -= count
                _ = stream.seek(position)
                lines = (stream.read(count) + pending).split(b'\n')
                pending = lines.pop(0) if position else b''
                for line in reversed(lines):
                    if not line:
                        continue
                    try:
                        row = mapping(cast(object, json.loads(line)))
                        at = float(str(row['time']))
                    except (ValueError, KeyError, TypeError, UnicodeError):
                        continue
                    if at < since:
                        return tuple(selected)
                    selected.append(row)
    except FileNotFoundError:
        pass
    return tuple(selected)


@dataclass(frozen=True)
class PricedResource:
    name: str
    usd_per_month: Decimal


@dataclass(frozen=True)
class ResourceInventory:
    at: int
    resources: tuple[PricedResource, ...]
    source: str
    builder_removed: bool = False


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
class UsageTypeCost:
    usage_type: str
    usd: Decimal
    quantity: Decimal = Decimal(0)
    unit: str = ''


@dataclass(frozen=True)
class ChargeRead:
    day: str
    at: int
    period_start: int
    period_end: int
    actual_usd: Decimal
    estimated_usd: Decimal
    query_usd: Decimal
    usage_types: tuple[UsageTypeCost, ...] = ()
    provisional: bool = True
    account_bill: bool = True

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
                                                 for p in rows(r, "resources")), text(r, "source"), r.get("builder_removed") is True)
         for r in rows(row, "inventories")],
        [CostReconciliation(instant(r, "at"), instant(r, "period_start"), instant(r, "period_end"),
                            conf.decimal(text(r, "usd")), text(r, "source"), money(str(r.get("query_usd", "0"))),
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
            money(text(item, 'usd_per_hour')), money(text(item, 'allowance_usd')), gate_state,
            str(item.get('instance_type', '')),
            BoxRate(money(str(item.get('applied_usd_per_hour', item['usd_per_hour']))),
                    str(item.get('rate_source', 'spot_max_usd_per_hour'))), str(item.get('region', '')),
            tuple((text(rate, 'day'), BoxRate(money(text(rate, 'usd_per_hour')), text(rate, 'source')))
                  for rate in rows(item, 'day_rates')) if 'day_rates' in item else (),
            GateAllowance(money(text(mapping(item['booked_allowance']), 'usd')),
                          text(mapping(item['booked_allowance']), 'source'))
            if item.get('booked_allowance') is not None else GateAllowanceUnpriced(),
            GateWorkerOwner(instant(mapping(item['owner']), 'pid'), text(mapping(item['owner']), 'start'),
                            text(mapping(item['owner']), 'folder')) if item.get('owner') else GateOwnerUnknown(),
            str(item.get('mode', '')),
            GateTrialBudget(text(mapping(item['trial']), 'trial_id'), money(text(mapping(item['trial']), 'limit_usd')))
            if item.get('trial') else NoGateTrial(), int(str(item.get('root_gib', 0)))))
    if len({r.attempt for r in evidence.gate_reservations}) != len(evidence.gate_reservations):
        raise LedgerUnavailable('duplicate gate attempt')
    for inventory in evidence.inventories:
        names = [resource.name for resource in inventory.resources]
        if not names and not inventory.builder_removed or len(set(names)) != len(names):
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
                         "resources": [{"name": p.name, "usd_per_month": str(p.usd_per_month)} for p in r.resources],
                         **({"builder_removed": True} if r.builder_removed else {})}
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
             'allowance_usd': str(r.allowance_usd), 'instance_type': r.instance_type, 'region': r.region,
             'mode': r.mode, 'root_gib': r.root_gib,
             'trial': {'trial_id': r.trial.trial_id, 'limit_usd': str(r.trial.limit_usd)}
                      if isinstance(r.trial, GateTrialBudget) else None,
             'owner': {'pid': r.owner.pid, 'start': r.owner.start, 'folder': r.owner.folder}
                      if isinstance(r.owner, GateWorkerOwner) else None,
             'booked_allowance': {'usd': str(r.booked_allowance.usd), 'source': r.booked_allowance.source}
                                 if isinstance(r.booked_allowance, GateAllowance) else None,
             'day_rates': [{'day': day, 'usd_per_hour': str(rate.usd_per_hour), 'source': rate.source}
                           for day, rate in r.day_rates],
             'applied_usd_per_hour': str(r.rate.usd_per_hour), 'rate_source': r.rate.source, 'state':
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
    if (require_fresh and observations and observations[-1].state != "terminated"
            and now - observations[-1].at > settings.observation_max_age_seconds):
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
        monthly = max(Decimal(0) if inventory.builder_removed else settings.fixed_usd_per_month,
                      sum((p.usd_per_month for p in inventory.resources), Decimal(0)))
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
        if observation.state != "terminated":
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
    for charge in gate_compute_intervals(state, pricing_end, settings.box_minimum_seconds):
        if charge.start == charge.end:
            if charge.start >= window_start:
                intervals.append(charge)
        else:
            for left, right in _days(max(charge.start, window_start), charge.end, settings.accounting_zone):
                intervals.append(replace(charge, start=left, end=right, usd=_overlap(charge, left, right)))
    for reservation in state.gate_reservations:
        if isinstance(reservation.state, GateAbsent):
            continue
        if window_start <= reservation.at < pricing_end:
            allowance = gate_allowance(reservation)
            intervals.append(ChargedInterval(reservation.at, reservation.at, allowance.usd,
                                             'gate storage and transfer:' + reservation.attempt + ' (' + allowance.source + ')', 'fixed'))
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
                         instant(row, 'period_end'), conf.decimal(text(row, 'actual_usd')),
                         money(text(row, 'estimated_usd')), money(text(row, 'query_usd')),
                         tuple(UsageTypeCost(text(item, 'usage_type'), conf.decimal(text(item, 'usd')),
                                             conf.decimal(str(item.get('quantity', '0'))), str(item.get('unit', '')))
                               for item in rows(row, 'usage_types')) if 'usage_types' in row else (),
                         row.get('provisional', True) is True, row.get('excludes_credits') is True)
    if reading.period_end <= reading.period_start or reading.period_end > reading.at:
        raise LedgerUnavailable('invalid charge reconciliation')
    return reading


def charge_record(reading: ChargeRead) -> dict[str, object]:
    return {'day': reading.day, 'at': reading.at, 'period_start': reading.period_start,
            'period_end': reading.period_end, 'actual_usd': str(reading.actual_usd),
            'estimated_usd': str(reading.estimated_usd), 'query_usd': str(reading.query_usd),
            **({'usage_types': [{'usage_type': item.usage_type, 'usd': str(item.usd),
                                'quantity': str(item.quantity), 'unit': item.unit} for item in reading.usage_types],
                'provisional': reading.provisional, 'excludes_credits': True} if reading.account_bill else {})}


SPEND_GROUPS = ('gate compute at 32 CPU', 'compute at 16 CPU', 'storage', 'public IPv4', 'data transfer', 'other')


@dataclass(frozen=True)
class SpendGroupCost:
    group: str
    usd: Decimal


@dataclass(frozen=True)
class DailySpend:
    day: str
    source: str
    groups: tuple[SpendGroupCost, ...]
    runs: int
    query_usd: Decimal = Decimal(0)
    intervals: tuple[ChargedInterval, ...] = ()
    provisional_bill: bool = False

    @property
    def usd(self) -> Decimal:
        return sum((row.usd for row in self.groups), Decimal(0))


def spend_group(usage_type: str) -> str:
    """Assign each AWS usage type to one spend group."""
    if 'SpotUsage:' in usage_type or 'BoxUsage:' in usage_type:
        size = usage_type.partition(':')[2]
        if size.endswith('.8xlarge'):
            return SPEND_GROUPS[0]
        if size.endswith('.4xlarge'):
            return SPEND_GROUPS[1]
    lowered = usage_type.lower()
    if any(name in lowered for name in ('ebs:', 'timedstorage', 'storageusage', 'volumeusage', 'snapshotusage')):
        return 'storage'
    if 'publicipv4' in lowered:
        return 'public IPv4'
    if any(name in lowered for name in ('datatransfer', 'dataxfer', 'aws-out-bytes', 'aws-in-bytes', 'regional-bytes')):
        return 'data transfer'
    return 'other'


def is_billed_day(reading: ChargeRead) -> bool:
    """A bill must round to at least one cent."""
    return reading.account_bill and reading.actual_usd.quantize(Decimal('.01')) >= Decimal('.01')


@dataclass(frozen=True)
class _GateCountUnavailable:
    """The bill day has S3 costs but no recorded gate launches."""


@dataclass(frozen=True)
class _AllowanceRatesUnavailable:
    """No usable bill rows establish the non-compute rates."""


@dataclass(frozen=True)
class _GateCountsNotRead:
    """This lookup requests compute rates without S3 allocation."""


_GATE_COUNTS_NOT_READ = _GateCountsNotRead()


@dataclass(frozen=True)
class _AllowanceRate:
    usd_per_hour: Decimal
    usd_per_gate: Decimal | _GateCountUnavailable
    source: str


@dataclass(frozen=True)
class _BillRates:
    days: tuple[str, ...]
    rates: tuple[BoxRate, ...]
    allowance_days: tuple[str, ...] = ()
    allowances: tuple[_AllowanceRate | _AllowanceRatesUnavailable, ...] = ()

    def for_day(self, day: str, fallback: Decimal) -> BoxRate:
        """Select the day's rate without reading the bill history again."""
        return (self.rates[max(0, bisect_right(self.days, day) - 1)] if self.days
                else BoxRate(fallback, 'spot_max_usd_per_hour'))

    def allowance_for_day(self, day: str) -> _AllowanceRate | _AllowanceRatesUnavailable:
        """Select non-compute rates with the same bill day rule."""
        return (self.allowances[max(0, bisect_right(self.allowance_days, day) - 1)] if self.allowance_days
                else _AllowanceRatesUnavailable())


def _bill_rates(readings: Sequence[ChargeRead], instance_type: str, now: int, region: str = '', *,
                root_gib: int = 0,
                gate_counts: Mapping[str, int] | _GateCountsNotRead = _GATE_COUNTS_NOT_READ) -> _BillRates:
    """Index completed bills once for one type and region."""
    prefixes = {'us-east-1': ('', 'USE1'), 'us-east-2': ('USE2',),
                'us-west-1': ('USW1',), 'us-west-2': ('USW2',)}
    rates: dict[str, BoxRate] = {}
    allowances: dict[str, _AllowanceRate | _AllowanceRatesUnavailable] = {}
    for reading in sorted(readings, key=lambda row: row.at):
        if not is_billed_day(reading) or reading.period_end > now:
            continue
        rows_for_type = [row for row in reading.usage_types
                         if row.usage_type.partition('SpotUsage:')[2] == instance_type and instance_type
                         and (not region or row.usage_type.partition('SpotUsage:')[0].removesuffix('-')
                              in prefixes.get(region, ()))]
        hours = sum((row.quantity for row in rows_for_type), Decimal(0))
        usd = sum((row.usd for row in rows_for_type), Decimal(0))
        if hours > 0 and usd > 0:
            rates[reading.day] = BoxRate(usd / hours, 'AWS bill ' + reading.day)
        if isinstance(gate_counts, _GateCountsNotRead):
            continue
        def matches(row: UsageTypeCost, usage: str, *, regional: bool = True) -> bool:
            return row.usage_type.endswith(usage) and (not regional or not region
                or row.usage_type.removesuffix(usage).removesuffix('-') in prefixes.get(region, ()))

        ipv4 = [row for row in reading.usage_types if matches(row, 'PublicIPv4:InUseAddress')]
        root = [row for row in reading.usage_types if matches(row, 'EBS:VolumeUsage.gp3')]
        s3 = [row for row in reading.usage_types if any(matches(row, usage, regional=False)
              for usage in ('Requests-Tier1', 'Requests-Tier2', 'TimedStorage-ByteHrs', 'DataTransfer-Out-Bytes'))]
        ipv4_hours = sum((row.quantity for row in ipv4), Decimal(0))
        root_months = sum((row.quantity for row in root), Decimal(0))
        s3_usd = sum((row.usd for row in s3), Decimal(0))
        count = gate_counts.get(reading.day, 0)
        allowances[reading.day] = _AllowanceRatesUnavailable()
        if ((ipv4 or root or s3)
                and not any(row.usd > 0 and row.quantity <= 0 for row in (*ipv4, *root))):
            month_hours = Decimal(_month_seconds(reading.period_start, ZoneInfo('UTC'))) / Decimal(3600)
            hourly = (sum((row.usd for row in ipv4), Decimal(0)) / ipv4_hours if ipv4_hours > 0 else Decimal(0))
            hourly += (sum((row.usd for row in root), Decimal(0)) * root_gib / root_months / month_hours
                       if root_months > 0 else Decimal(0))
            share = s3_usd / count if count else Decimal(0) if not s3_usd else _GateCountUnavailable()
            allowances[reading.day] = _AllowanceRate(hourly, share,
                                                    'AWS bill ' + reading.day)
    days = tuple(sorted(rates))
    allowance_days = tuple(sorted(allowances))
    return _BillRates(days, tuple(rates[day] for day in days), allowance_days,
                      tuple(allowances[day] for day in allowance_days))


def gate_allowance(reservation: GateReservation) -> GateAllowance:
    """Use the booked cost for completed gates and retain the reserve for open gates."""
    if isinstance(reservation.state, GateTerminated) and isinstance(reservation.booked_allowance, GateAllowance):
        return reservation.booked_allowance
    return GateAllowance(reservation.allowance_usd, 'attempt_allowance_usd')


def _book_allowance(reservation: GateReservation, indexes: Sequence[_BillRates]) -> GateAllowance | GateAllowanceUnpriced:
    """Price actual box hours and one share of the launch day's S3 costs."""
    if not isinstance(reservation.state, GateTerminated):
        return GateAllowanceUnpriced()
    total = Decimal(0)
    sources: list[str] = []
    spans = tuple(_days(reservation.at, reservation.state.at, ZoneInfo('UTC'))) or ((reservation.at, reservation.at),)
    for number, (start, end) in enumerate(spans):
        day = datetime.fromtimestamp(start, ZoneInfo('UTC')).date().isoformat()
        candidates = [index.allowance_for_day(day) for index in indexes]
        if not candidates or any(isinstance(rate, _AllowanceRatesUnavailable)
                or (number == 0 and isinstance(rate.usd_per_gate, _GateCountUnavailable)) for rate in candidates):
            return GateAllowance(reservation.allowance_usd, 'attempt_allowance_usd')
        costs = [GateAllowance(rate.usd_per_hour * Decimal(end - start) / Decimal(3600)
                               + (rate.usd_per_gate if number == 0 and isinstance(rate.usd_per_gate, Decimal)
                                  else Decimal(0)), rate.source)
                 for rate in candidates if isinstance(rate, _AllowanceRate)]
        selected = max(costs, key=lambda cost: cost.usd)
        total += selected.usd
        if selected.source not in sources:
            sources.append(selected.source)
    return GateAllowance(total, ', '.join(sources))


def _correct_allowance_estimates(state: LedgerEvidence, readings: Sequence[ChargeRead], now: int) -> None:
    """Recompute obsolete allowance floors where the gate day has its own bill."""
    billed_starts = sorted(row.at for row in state.gate_reservations
        if isinstance(row.state, GateTerminated) and isinstance(row.booked_allowance, GateAllowance)
        and row.booked_allowance.source.startswith('AWS bill '))
    bills = {(row.period_start, row.period_end): row.actual_usd for row in readings
             if is_billed_day(row) and row.period_end <= now}
    corrected: list[CostReconciliation] = []
    for reading in state.reconciliations:
        position = bisect_left(billed_starts, reading.period_start)
        key = (reading.period_start, reading.period_end)
        if (reading.source == 'Cost Explorer' and key in bills and position < len(billed_starts)
                and billed_starts[position] < reading.period_end
                and sum((day.usd for day in reading.established_days), Decimal(0)) > bills[key]):
            reading = replace(reading, established_days=())
        corrected.append(reading)
    state.reconciliations = corrected


def box_rate(readings: Sequence[ChargeRead], instance_type: str, fallback: Decimal, now: int, day: str, *,
             region: str = '') -> BoxRate:
    """Use the day's bill, the nearest earlier bill, or the oldest available bill."""
    return _bill_rates(readings, instance_type, now, region).for_day(day, fallback)


def gate_rate(readings: Sequence[ChargeRead], instance_type: str, fallback: Decimal, now: int, day: str,
              region: str = '') -> BoxRate:
    """Use the box region or reserve the highest configured regional rate for the day."""
    regions = (region,) if region else conf.read_or_repository('cloud_gate').words('regions')
    return max((box_rate(readings, instance_type, fallback, now, day, region=item) for item in regions),
               key=lambda rate: rate.usd_per_hour, default=BoxRate(fallback, 'spot_max_usd_per_hour'))


def box_cost(rate: BoxRate, running_seconds: int, minimum_seconds: int) -> Decimal:
    """Price one box with the configured minimum running time."""
    return rate.usd_per_hour * Decimal(max(minimum_seconds, running_seconds)) / Decimal(3600)


def day_compute_intervals(start: int, end: int, minimum_seconds: int, rate_for_day: Callable[[str], BoxRate], *,
                          source: str = '') -> tuple[ChargedInterval, ...]:
    """Split a lifetime at UTC midnight and apply its minimum charge once."""
    zone = ZoneInfo('UTC')
    spans = tuple(_days(start, end, zone)) if end > start else ((start, start),)
    duration = max(1, end - start)
    billable = Decimal(max(minimum_seconds, end - start))
    intervals: list[ChargedInterval] = []
    for left, right in spans:
        rate = rate_for_day(datetime.fromtimestamp(left, zone).date().isoformat())
        usd = rate.usd_per_hour * billable * Decimal(max(1, right - left)) / Decimal(duration) / Decimal(3600)
        intervals.append(ChargedInterval(left, right, usd, source or rate.source, 'instance'))
    return tuple(intervals)


def trial_gate_cost(reservation: GateReservation, minimum_seconds: int) -> Decimal:
    """Keep the full maximum for open boxes and book confirmed box costs."""
    if isinstance(reservation.state, GateAbsent):
        return Decimal(0)
    if isinstance(reservation.state, GateTerminated):
        evidence = LedgerEvidence(gate_reservations=[reservation])
        return (sum((row.usd for row in gate_compute_intervals(evidence, reservation.state.at, minimum_seconds)), Decimal(0))
                + gate_allowance(reservation).usd)
    return (reservation.usd_per_hour * Decimal(max(minimum_seconds, reservation.expires - reservation.at))
            / Decimal(3600) + reservation.allowance_usd)


def gate_compute_intervals(state: LedgerEvidence, now: int, minimum_seconds: int, *,
                           reserve: bool = False) -> tuple[ChargedInterval, ...]:
    intervals: list[ChargedInterval] = []
    for reservation in state.gate_reservations:
        if isinstance(reservation.state, GateAbsent) or (reservation.at > now and not reserve):
            continue
        end = min(now, reservation.state.at) if isinstance(reservation.state, GateTerminated) else (
            max(now, reservation.expires) if reserve else now)
        rate = reservation.rate if reservation.rate.source != 'not priced' else BoxRate(reservation.usd_per_hour, 'spot_max_usd_per_hour')
        rates = dict(reservation.day_rates)
        intervals.extend(day_compute_intervals(reservation.at, end, minimum_seconds, lambda day: rates.get(day, rate),
                                              source='gate:' + reservation.attempt))
    return tuple(intervals)


def older_compute_intervals(intervals: Sequence[ChargedInterval], readings: Sequence[ChargeRead],
                            settings: SpendingSettings, now: int) -> tuple[ChargedInterval, ...]:
    """Price each UTC day and apply the minimum once per builder lifetime."""
    lifetimes: list[ChargedInterval] = []
    for row in sorted((row for row in intervals if row.kind == 'instance'), key=lambda row: row.start):
        if lifetimes and lifetimes[-1].end == row.start:
            lifetimes[-1] = replace(lifetimes[-1], end=row.end)
        else:
            lifetimes.append(row)
    rates = _bill_rates(readings, settings.instance_type, now)
    return tuple(charge for row in lifetimes for charge in day_compute_intervals(
        row.start, row.end, settings.box_minimum_seconds,
        lambda day: rates.for_day(day, settings.spot_max_usd_per_hour)))


def spending_periods(intervals: Sequence[ChargedInterval], zone: ZoneInfo) -> tuple[dict[str, Decimal], dict[str, Decimal]]:
    """Allocate charges to the configured accounting days and months."""
    daily: dict[str, Decimal] = {}
    monthly: dict[str, Decimal] = {}
    for charge in intervals:
        spans = ((charge.start, charge.end),) if charge.start == charge.end else _days(charge.start, charge.end, zone)
        for start, end in spans:
            day = datetime.fromtimestamp(start, zone).date().isoformat()
            usd = charge.usd if start == end else _overlap(charge, start, end)
            daily[day] = daily.get(day, Decimal(0)) + usd
            monthly[day[:7]] = monthly.get(day[:7], Decimal(0)) + usd
    return daily, monthly


def gate_spend_days(state: LedgerEvidence, settings: SpendingSettings, readings: Sequence[ChargeRead], now: int, *,
                    reserve: bool = False) -> tuple[DailySpend, ...]:
    """Use account bills for completed days and bill rates for unbilled days."""
    zone = ZoneInfo('UTC')
    today = datetime.fromtimestamp(now, zone).date()
    local = datetime.fromtimestamp(now, settings.accounting_zone)
    month_start, _ = period_bounds(local.date().replace(day=1), settings.accounting_zone)
    first = min(today.replace(day=1), datetime.fromtimestamp(month_start, zone).date())
    last_at = max((r.expires for r in state.gate_reservations
                   if gate_is_open(r)), default=now) if reserve else now
    last = datetime.fromtimestamp(max(now, last_at - 1), zone).date()
    bills = {r.day: r for r in readings if is_billed_day(r) and r.period_end <= now}
    if bills:
        first = min(first, date.fromisoformat(min(bills)))
    latest = max(bills.values(), key=lambda row: row.day, default=None)
    base = _charges(replace(state, gate_reservations=[]), replace(settings, accounting_zone=zone), now,
                    require_fresh=False)
    older = tuple(row for row in base.intervals if row.kind == 'instance')
    if latest is not None:
        older = older_compute_intervals(older, readings, settings, now)
    gate_compute: dict[str, list[ChargedInterval]] = {}
    for charge in gate_compute_intervals(state, now, settings.box_minimum_seconds, reserve=reserve):
        gate_compute.setdefault(charge.source, []).append(charge)
    days: list[DailySpend] = []
    for offset in range((last - first).days + 1):
        day = first + timedelta(days=offset)
        key = day.isoformat()
        start, end = period_bounds(day, zone)
        stop = end if reserve else min(now, end)
        groups: dict[str, Decimal] = dict.fromkeys(SPEND_GROUPS, Decimal(0))
        intervals: list[ChargedInterval] = []

        def include(charge: ChargedInterval) -> None:
            if charge.start == charge.end:
                if start <= charge.start < end:
                    intervals.append(charge)
            elif charge.start < end and charge.end > start:
                intervals.append(replace(charge, start=max(start, charge.start), end=min(end, charge.end),
                                         usd=_overlap(charge, start, end)))

        for charge in base.intervals:
            if charge.kind == 'explorer':
                include(charge)
        query_usd = period_cost(tuple(row for row in base.intervals if row.kind == 'explorer'), start, end)
        bill = bills.get(key)
        runs = sum(start <= r.at < end and isinstance(r.state, (GateRunning, GateTerminated))
                   for r in state.gate_reservations)
        if bill is not None:
            for row in bill.usage_types:
                groups[spend_group(row.usage_type)] += row.usd
            groups['other'] += bill.actual_usd - sum(groups.values(), Decimal(0))
            include(ChargedInterval(start, end, bill.actual_usd, 'AWS bill', 'reconciliation'))
            source = 'AWS bill'
        else:
            source = 'estimated from the newest bill' if latest is not None else 'estimated from configured prices'
            if latest is not None:
                for row in latest.usage_types:
                    if 'SpotUsage:' not in row.usage_type:
                        groups[spend_group(row.usage_type)] += row.usd
                groups['other'] += latest.actual_usd - sum((row.usd for row in latest.usage_types), Decimal(0))
                include(ChargedInterval(start, end, sum(groups.values(), Decimal(0)), 'newest bill non-compute', 'fixed'))
            else:
                groups['storage'] += period_cost(tuple(row for row in base.intervals if row.kind != 'explorer'
                                                       and row.kind != 'instance'), start, end)
                for charge in base.intervals:
                    if charge.kind not in ('explorer', 'instance'):
                        include(charge)
            groups[spend_group('SpotUsage:' + settings.instance_type)] += period_cost(older, start, end)
            for charge in older:
                include(charge)
            for reservation in state.gate_reservations:
                if isinstance(reservation.state, GateAbsent) or reservation.at > stop:
                    continue
                for charge in gate_compute.get('gate:' + reservation.attempt, ()):
                    groups[spend_group('SpotUsage:' + reservation.instance_type)] += _overlap(charge, start, end)
                    include(charge)
        for reservation in state.gate_reservations:
            if isinstance(reservation.state, GateAbsent) or not start <= reservation.at < end:
                continue
            if ((bill is None and latest is None) or
                    (reserve and gate_is_open(reservation))):
                allowance = gate_allowance(reservation)
                groups['other'] += allowance.usd
                include(ChargedInterval(reservation.at, reservation.at, allowance.usd,
                                         'gate storage and transfer:' + reservation.attempt + ' (' + allowance.source + ')', 'fixed'))
        days.append(DailySpend(key, source, tuple(SpendGroupCost(group, usd) for group, usd in groups.items()),
                              runs, query_usd, tuple(intervals), provisional_bill=bill is not None and bill.provisional))
    return tuple(days)


def _reserved_fixed_costs(state: LedgerEvidence, settings: SpendingSettings, costs: CostEstimate) -> tuple[Decimal, Decimal]:
    """Reserve each period's known fixed costs before the first resource allocation."""
    local = datetime.fromtimestamp(costs.at, settings.accounting_zone)
    inventory = max(state.inventories, key=lambda item: item.at)
    monthly = max(Decimal(0) if inventory.builder_removed else settings.fixed_usd_per_month,
                      sum((p.usd_per_month for p in inventory.resources), Decimal(0)))
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
        pending = replace(state, gate_reservations=[reservation])
        reserved_days, _months = spending_periods(
            gate_compute_intervals(pending, now, settings.box_minimum_seconds, reserve=True), settings.accounting_zone)
        accrued_days, _months = spending_periods(
            gate_compute_intervals(pending, now, settings.box_minimum_seconds), settings.accounting_zone)
        for day, reserved in reserved_days.items():
            cost = max(Decimal(0), reserved - accrued_days.get(day, Decimal(0)))
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

    def read_bills(self) -> tuple[ChargeRead, ...]:
        return tuple(read_charge(path) for path in sorted((self.directory / 'charges').glob('*.json')))

    def launches(self) -> tuple[dict[str, object], ...]:
        path = self.directory / 'cloud-gate-launches.jsonl'
        return tuple(mapping(cast(object, json.loads(line))) for line in path.read_text().splitlines()) if path.exists() else ()

    def _price_gates(self, state: LedgerEvidence, readings: Sequence[ChargeRead], now: int) -> None:
        launch_rows = self.launches()
        launches = {str(row.get('attempt', '')): row for row in reversed(launch_rows) if row.get('result') == 'launched'}
        gate_counts: dict[str, int] = {}
        for row in launches.values():
            day = datetime.fromtimestamp(float(str(row['time'])), ZoneInfo('UTC')).date().isoformat()
            gate_counts[day] = gate_counts.get(day, 0) + 1
        indexes: dict[tuple[str, str, int], _BillRates] = {}
        needs_root = (any(isinstance(row.state, GateTerminated) for row in state.gate_reservations)
                      and any(row.usage_type.endswith('EBS:VolumeUsage.gp3') and row.quantity > 0
                              for reading in readings for row in reading.usage_types))
        configuration = conf.read_or_repository('cloud_gate') if needs_root or any(
            not row.region for row in state.gate_reservations) else None
        configured_regions = configuration.words('regions') if configuration is not None else ()
        root_gib = configuration.whole('root_gib') if configuration is not None and needs_root else 0
        priced: list[GateReservation] = []
        for reservation in state.gate_reservations:
            launch = launches.get(reservation.attempt)
            size = text(launch, 'instance_type') if launch is not None else reservation.instance_type
            at = int(float(str(launch['time']))) if launch is not None else reservation.at
            end = reservation.state.at if isinstance(reservation.state, GateTerminated) else max(now, reservation.expires)
            days = tuple(datetime.fromtimestamp(left, ZoneInfo('UTC')).date().isoformat()
                         for left, _right in _days(at, max(at + 1, end), ZoneInfo('UTC')))
            regions = (reservation.region,) if reservation.region else configured_regions
            for region in regions:
                key = (size, region, reservation.root_gib or root_gib)
                if key not in indexes:
                    indexes[key] = _bill_rates(readings, size, now, region, root_gib=key[2], gate_counts=gate_counts)
            rates = tuple((day, max((indexes[(size, region, reservation.root_gib or root_gib)].for_day(day, reservation.usd_per_hour) for region in regions),
                                   key=lambda rate: rate.usd_per_hour,
                                   default=BoxRate(reservation.usd_per_hour, 'spot_max_usd_per_hour'))) for day in days)
            reservation = replace(reservation, at=at, instance_type=size, rate=rates[0][1], day_rates=rates)
            priced.append(replace(reservation, booked_allowance=_book_allowance(
                reservation, tuple(indexes[(size, region, reservation.root_gib or root_gib)] for region in regions))))
        state.gate_reservations = priced
        _correct_allowance_estimates(state, readings, now)

    def priced_snapshot(self, now: int) -> LedgerEvidence:
        """Read current bill prices without publishing ledger changes."""
        state = self.read_snapshot()
        self._price_gates(state, self.read_bills(), now)
        return state

    def spend_days(self, settings: SpendingSettings, now: int) -> tuple[DailySpend, ...]:
        state = self.read_snapshot()
        readings = self.read_bills()
        self._price_gates(state, readings, now)
        days = gate_spend_days(state, settings, readings, now)
        launches = self.launches()
        logged_attempts = {str(row.get('attempt', '')) for row in launches if row.get('result') == 'launched'}
        return tuple(replace(day, runs=sum(row.get('result') == 'launched'
            and datetime.fromtimestamp(float(str(row['time'])), ZoneInfo('UTC')).date().isoformat() == day.day
            for row in launches) + sum(r.attempt not in logged_attempts and isinstance(r.state, (GateRunning, GateTerminated))
                and datetime.fromtimestamp(r.at, ZoneInfo('UTC')).date().isoformat() == day.day
                for r in state.gate_reservations)) for day in days)

    def reserve_gate(self, settings_path: Path, request: GateReservation) -> GateReserved | GateCapReached | GateAttemptEnded:
        """Reserve gate costs under the shared Rust accounting lock."""
        with self._locked() as state:
            for previous in state.gate_reservations:
                if previous.attempt == request.attempt:
                    if isinstance(previous.state, (GateAbsent, GateTerminated)):
                        return GateAttemptEnded()
                    return GateReserved(previous)
            settings = read_settings(settings_path)
            if isinstance(request.trial, GateTrialBudget):
                if request.mode != 'gpu-size-trial' or not request.trial.trial_id or request.trial.limit_usd <= 0:
                    raise LedgerUnavailable('The GPU trial budget is invalid')
                previous_trials = [row for row in state.gate_reservations
                    if isinstance(row.trial, GateTrialBudget) and row.trial.trial_id == request.trial.trial_id]
                limit = min([request.trial.limit_usd, *(row.trial.limit_usd for row in previous_trials
                                                       if isinstance(row.trial, GateTrialBudget))])
                trial_id = request.trial.trial_id
                request = replace(request, trial=GateTrialBudget(trial_id, limit))
                self._price_gates(state, self.read_bills(), request.at)
                scoped = [row for row in state.gate_reservations
                    if isinstance(row.trial, GateTrialBudget) and row.trial.trial_id == trial_id]
                total = sum((trial_gate_cost(row, settings.box_minimum_seconds) for row in scoped), Decimal(0))
                if total + trial_gate_cost(request, settings.box_minimum_seconds) > limit:
                    return GateCapReached('gpu_size_trial_limit_usd')
            costs = _charges(state, settings, request.at, current_window=True, require_fresh=False)
            state.gate_reservations.append(request)
            readings = self.read_bills()
            self._price_gates(state, readings, request.at)
            days = gate_spend_days(state, settings, readings, request.at, reserve=True)
            fixed_daily, fixed_monthly = _reserved_fixed_costs(state, settings, costs)
            if any(is_billed_day(r) and r.period_end <= request.at for r in readings):
                fixed_daily = fixed_monthly = Decimal(0)
            today = datetime.fromtimestamp(request.at, settings.accounting_zone).date().isoformat()
            daily, monthly = spending_periods(tuple(charge for day in days for charge in day.intervals), settings.accounting_zone)
            reason = ''
            for day, amount in daily.items():
                if day >= today and amount + fixed_daily + settings.limit_reserve_usd >= settings.daily_limit_usd:
                    reason = 'daily_limit'
            if not reason and any(amount + fixed_monthly + settings.limit_reserve_usd >= settings.monthly_limit_usd
                                  for month, amount in monthly.items() if month >= today[:7]):
                reason = 'monthly_limit'
            if reason:
                state.gate_reservations = [r for r in state.gate_reservations if r.attempt != request.attempt]
                self._save(state)
                return GateCapReached(reason)
            self._save(state)
            return GateReserved(state.gate_reservations[-1])

    def gate_region(self, attempt: str, region: str) -> None:
        """Retain the region before AWS can accept a launch."""
        with self._locked() as state:
            state.gate_reservations = [replace(row, region=region) if row.attempt == attempt else row
                                       for row in state.gate_reservations]
            self._save(state)

    def gate_owner(self, attempt: str, owner: GateWorkerOwner) -> None:
        """Save the worker identity before an attempt can launch a box."""
        with self._locked() as state:
            state.gate_reservations = [replace(row, owner=owner) if row.attempt == attempt else row
                                       for row in state.gate_reservations]
            self._save(state)

    def resolve_gate(self, attempt: str, outcome: GateRunning | GateAbsent | GateTerminated, *, instance_type: str = '') -> None:
        """Preserve confirmed termination against every later outcome."""
        with self._locked() as state:
            if any(row.attempt == attempt and isinstance(row.state, GateTerminated) for row in state.gate_reservations):
                return
            state.gate_reservations = [replace(row, state=outcome, instance_type=instance_type or row.instance_type)
                                       if row.attempt == attempt else row for row in state.gate_reservations]
            self._price_gates(state, self.read_bills(), int(time.time()) if isinstance(outcome, GateRunning) else outcome.at)
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
        if (not inventory.resources and not inventory.builder_removed
                or len({p.name for p in inventory.resources}) != len(inventory.resources)):
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
        query_usd = reading.query_usd
        for saved in state.reconciliations:
            if (saved.period_start, saved.period_end, saved.source) == key:
                if saved.at != reading.at:
                    query_usd += saved.query_usd
                else:
                    query_usd = saved.query_usd
                for day in saved.established_days:
                    established[day.day] = max(established.get(day.day, Decimal(0)), day.usd)
        reconciliation = CostReconciliation(reading.at, reading.period_start, reading.period_end,
                                             reading.actual_usd, 'Cost Explorer', query_usd,
                                             tuple(EstablishedDayCost(day, usd) for day, usd in sorted(established.items())))
        if reconciliation in state.reconciliations and state.charge_refresh_at >= reading.at:
            return
        state.reconciliations = [r for r in state.reconciliations
                                 if (r.period_start, r.period_end, r.source) != key]
        state.reconciliations.append(reconciliation)
        state.charge_refresh_at = max(state.charge_refresh_at, reading.at)
        self._save(state)

    def refresh_charges(self, settings_path: Path, now: int,
                        fetch: Callable[[int, int], Sequence[ChargeRead]]) -> ChargeRead | MeasurementUnavailable:
        """Refresh recent account bills through the shared schedule and recovery lock."""
        configuration = conf.read(settings_path)
        settings = read_settings(configuration)
        day = charge_period_day(now)
        start, end = period_bounds(day, ZoneInfo('UTC'))
        start -= (settings.cost_explorer_lookback_days - 1) * 86400
        with self._locked() as state:
            newest = max((self.directory / 'charges').glob('*.json'), default=None)
            if newest is not None:
                previous = read_charge(newest)
                if previous.account_bill:
                    # Recover every day from the last query before applying its schedule.
                    complete = True
                    for offset in range(settings.cost_explorer_lookback_days):
                        saved_day = date.fromisoformat(previous.day) - timedelta(days=offset)
                        path = self.directory / 'charges' / f'{saved_day.isoformat()}.json'
                        if path.exists():
                            reading = read_charge(path)
                            if reading.account_bill and reading.at == previous.at:
                                self._reconcile_charge(state, reading)
                            else:
                                complete = False
                        else:
                            complete = False
                    if complete and now < previous.at + settings.cost_explorer_interval_seconds:
                        return previous
            readings = fetch(start, end)
            saved: list[ChargeRead] = []
            for reading in readings:
                estimated = Decimal(0)
                if state.inventories and any(inventory.at <= reading.period_end for inventory in state.inventories):
                    costs = costs_from_evidence(state, settings, reading.period_end,
                                                window=datetime.fromtimestamp(reading.period_start, settings.accounting_zone).date())
                    estimated = period_cost(costs.intervals, reading.period_start, reading.period_end)
                reading = replace(reading, at=now, estimated_usd=estimated)
                path = self.directory / 'charges' / f'{reading.day}.json'
                write_text_atomic(path, json.dumps(charge_record(reading), sort_keys=True) + '\n')
                saved.append(reading)
            for reading in saved:
                self._reconcile_charge(state, reading)
            return saved[-1]

    def _save_estimate(self, state: LedgerEvidence, costs: CostEstimate) -> None:
        established = {r: r for r in costs.established_reconciliations}
        state.reconciliations = [established.get(r, r) for r in state.reconciliations]
        self._save(state)

    def estimate(self, settings: SpendingSettings, now: int) -> CostEstimate:
        with self._locked() as state:
            self._price_gates(state, self.read_bills(), now)
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
            self._price_gates(state, self.read_bills(), now)
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
