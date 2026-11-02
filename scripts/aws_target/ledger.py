"""Persist cost evidence and spending decisions without making AWS calls."""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Callable, Generator, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, time as day_time, timedelta
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
from scripts.aws_target.config import (
    LimitSettingEvidence, SpendingConfigurationUnavailable, SpendingSettings, money, read_settings,
)

InstanceState = Literal["pending", "running", "stopping", "stopped", "terminated", "unknown"]
SpendingPurpose = Literal["deployment", "start", "admission"]
ChargeKind = Literal["instance", "fixed", "explorer", "reconciliation"]


@dataclass(frozen=True)
class InstanceObservation:
    at: int
    state: InstanceState
    source: str


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
class CostReconciliation:
    at: int
    period_start: int
    period_end: int
    usd: Decimal
    source: str


@dataclass(frozen=True)
class ChargedInterval:
    start: int
    end: int
    usd: Decimal
    source: str
    kind: ChargeKind


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


class ObservationRejected(ValueError):
    """The observation does not confirm a lifecycle state."""


class LedgerUnavailable(ValueError):
    """Evidence cannot establish a safe spending decision."""


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise LedgerUnavailable("expected an evidence record")
    return cast(dict[str, object], value)


def _text(row: dict[str, object], key: str) -> str:
    value = row[key]
    if not isinstance(value, str) or not value:
        raise LedgerUnavailable(f"{key}: expected nonempty text")
    return value


def _instant(row: dict[str, object], key: str) -> int:
    value = row[key]
    if type(value) is not int or value < 0:
        raise LedgerUnavailable(f"{key}: expected nonnegative integer seconds")
    return value


def _rows(row: dict[str, object], key: str) -> tuple[dict[str, object], ...]:
    value = row[key]
    if not isinstance(value, list):
        raise LedgerUnavailable(f"{key}: expected evidence records")
    return tuple(_mapping(item) for item in cast(list[object], value))


def _decode(row: dict[str, object]) -> LedgerEvidence:
    if row.get("version") != 1:
        raise LedgerUnavailable("unsupported ledger version")
    observations: list[InstanceObservation] = []
    for record in _rows(row, "observations"):
        state = _text(record, "state")
        if state not in ("pending", "running", "stopping", "stopped", "terminated", "unknown"):
            raise LedgerUnavailable("unknown instance state")
        observations.append(InstanceObservation(_instant(record, "at"), state, _text(record, "source")))
    evidence = LedgerEvidence(
        observations,
        [PriceSample(_instant(r, "at"), money(_text(r, "usd_per_hour")), _text(r, "source")) for r in _rows(row, "prices")],
        [ResourceInventory(_instant(r, "at"), tuple(PricedResource(_text(p, "name"), money(_text(p, "usd_per_month")))
                                                 for p in _rows(r, "resources")), _text(r, "source"))
         for r in _rows(row, "inventories")],
        [CostReconciliation(_instant(r, "at"), _instant(r, "period_start"), _instant(r, "period_end"),
                            money(_text(r, "usd")), _text(r, "source")) for r in _rows(row, "reconciliations")],
        [LimitEvent(_text(r, "event_id"), _instant(r, "at"), _text(r, "period"), money(_text(r, "estimate_usd")),
                    money(_text(r, "limit_usd")), _text(r, "recipient"), _instant(r, "timeout_s"),
                    DeliveryState(r.get("delivery", "pending")),
                    _instant(r, "retry_after") if "retry_after" in r else 0,
                    DeliveryOwner(_text(r, "owner")) if r.get("owner") else DeliveryUnowned())
         for r in _rows(row, "events")],
        _instant(row, "price_refresh_at"), _mapping(row["decision"]))
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
                             "usd": str(r.usd), "source": r.source} for r in state.reconciliations],
        "events": [{"event_id": r.event_id, "at": r.at, "period": r.period, "estimate_usd": str(r.estimate_usd),
                    "limit_usd": str(r.limit_usd), "recipient": r.recipient, "timeout_s": r.timeout_s,
                    "delivery": r.delivery.value, "retry_after": r.retry_after,
                    "owner": r.owner.token if isinstance(r.owner, DeliveryOwner) else ""} for r in state.events],
        "price_refresh_at": state.price_refresh_at, "decision": state.decision,
    }


def _days(start: int, end: int, zone: ZoneInfo) -> Iterator[tuple[int, int]]:
    while start < end:
        date = datetime.fromtimestamp(start, zone).date() + timedelta(days=1)
        boundary = int(datetime.combine(date, day_time(), zone).timestamp())
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
             current_window: bool = False) -> CostEstimate:
    if type(now) is not int or now < 0:
        raise LedgerUnavailable("decision time must use nonnegative integer seconds")
    if not state.inventories:
        raise LedgerUnavailable("a priced resource inventory is required before spending")
    inventories = sorted(state.inventories, key=lambda item: item.at)
    observations = sorted((r for r in state.observations if r.state != "unknown"), key=lambda item: item.at)
    prices = sorted(state.prices, key=lambda item: item.at)
    if any(r.at > now for r in (*inventories, *observations, *prices, *state.reconciliations)):
        raise LedgerUnavailable("cost evidence is from the future")
    if observations and now - observations[-1].at > settings.observation_max_age_seconds:
        raise LedgerUnavailable("instance state evidence is stale")
    local = datetime.fromtimestamp(now, settings.accounting_zone)
    month_start = int(local.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp())
    window_start = month_start if current_window else 0
    # Include the complete periods needed by overlapping reconciliations.
    for reading in sorted(state.reconciliations, key=lambda r: r.period_end, reverse=True):
        if reading.period_end > window_start:
            window_start = min(window_start, reading.period_start)
    relevant_readings = [r for r in state.reconciliations if r.period_end > window_start]
    intervals: list[ChargedInterval] = []
    inventory_index = max(0, bisect_right(inventories, window_start, key=lambda r: r.at) - 1)
    for index in range(inventory_index, len(inventories)):
        inventory = inventories[index]
        end = inventories[index + 1].at if index + 1 < len(inventories) else now
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
        end = observations[index + 1].at if index + 1 < len(observations) else now
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
                intervals.append(ChargedInterval(left, right, price * Decimal(right - left) / Decimal(3600), source, "instance"))
    if observations and inventories[0].at < observations[0].at:
        fallback(inventories[0].at, observations[0].at)
    interval = settings.cost_explorer_interval_seconds
    first_read = inventories[0].at + max(0, (window_start - inventories[0].at + interval - 1) // interval) * interval
    for at in range(first_read, now, interval):
        intervals.append(ChargedInterval(at, at, settings.cost_explorer_read_usd, "scheduled Cost Explorer read", "explorer"))
    for reading in relevant_readings:
        if reading.period_end <= reading.period_start:
            raise LedgerUnavailable("invalid Cost Explorer period")
        estimated = sum((_overlap(r, reading.period_start, reading.period_end) for r in intervals), Decimal(0))
        extra = max(Decimal(0), reading.usd - estimated)
        for start, end in _days(reading.period_start, reading.period_end, settings.accounting_zone):
            intervals.append(ChargedInterval(start, end, extra * Decimal(end - start)
                                             / Decimal(reading.period_end - reading.period_start), reading.source, "reconciliation"))
    day, month = local.date().isoformat(), local.strftime("%Y-%m")
    daily = sum((r.usd for r in intervals if datetime.fromtimestamp(r.start, settings.accounting_zone).date().isoformat() == day), Decimal(0))
    monthly_total = sum((r.usd for r in intervals if datetime.fromtimestamp(r.start, settings.accounting_zone).strftime("%Y-%m") == month), Decimal(0))
    return CostEstimate(now, DailyTotal(day, daily), MonthlyTotal(month, monthly_total), tuple(intervals),
                        inventories[-1].at, observations[-1].at if observations else 0)


def _reserved_fixed_costs(state: LedgerEvidence, settings: SpendingSettings, costs: CostEstimate) -> tuple[Decimal, Decimal]:
    """Reserve each period's known fixed costs before the first resource allocation."""
    local = datetime.fromtimestamp(costs.at, settings.accounting_zone)
    inventory = max(state.inventories, key=lambda item: item.at)
    monthly = max(settings.fixed_usd_per_month, sum((p.usd_per_month for p in inventory.resources), Decimal(0)))
    day_end = int(datetime.combine(local.date() + timedelta(days=1), day_time(), settings.accounting_zone).timestamp())
    day_start = int(datetime.combine(local.date(), day_time(), settings.accounting_zone).timestamp())
    denominator = Decimal(_month_seconds(costs.at, settings.accounting_zone))
    daily_accrued = sum((r.usd for r in costs.intervals if r.kind == "fixed"
                         and datetime.fromtimestamp(r.start, settings.accounting_zone).date() == local.date()), Decimal(0))
    monthly_accrued = sum((r.usd for r in costs.intervals if r.kind == "fixed"
                           and datetime.fromtimestamp(r.start, settings.accounting_zone).strftime("%Y-%m") == costs.monthly.month), Decimal(0))
    return (max(Decimal(0), monthly * Decimal(day_end - day_start) / denominator - daily_accrued),
            max(Decimal(0), monthly - monthly_accrued))


def send_limit_event(event: LimitEvent) -> bool:
    """Use the existing message interface after persisting the event identity."""
    script = Path(__file__).resolve().parents[1] / "message" / "send.py"
    # Keep the key active across every date that the message interface can represent.
    repeat_minutes = (datetime.max - datetime.min) // timedelta(minutes=1) + 1
    result = subprocess.run([sys.executable, str(script), "--to", event.recipient, "--from", "aws-spending-guard",
                             "--key", f"aws-limit:{event.event_id}", "--repeat-minutes", str(repeat_minutes), "--text",
                             f"AWS spending guard: {event.period} limit refuses new work; drain the target. "
                             + f"Estimate ${event.estimate_usd}; limit ${event.limit_usd}."],
                            capture_output=True, text=True, timeout=event.timeout_s, check=False)
    return result.returncode == 0 and result.stdout.startswith(("SENT:", "SKIPPED:"))


def _decision_record(decision: SpendingDecision) -> dict[str, object]:
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

    @contextmanager
    def _locked(self) -> Generator[LedgerEvidence]:
        self.directory.mkdir(parents=True, exist_ok=True)
        with (self.directory / "cost-ledger.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                state = _decode(_mapping(cast(object, json.loads(self.path.read_text())))) if self.path.exists() else LedgerEvidence()
                yield state
            except (KeyError, TypeError, ArithmeticError, UnicodeError) as error:
                raise LedgerUnavailable(f"ledger evidence is invalid: {error}") from error
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _save(self, state: LedgerEvidence) -> None:
        document = _encode(state)
        _ = _decode(document)
        write_text_atomic(self.path, json.dumps(document, sort_keys=True) + "\n")

    def observe(self, observation: InstanceObservation) -> None:
        if observation.state == "unknown":
            raise ObservationRejected("unknown does not confirm an instance lifecycle state")
        with self._locked() as state:
            if any(r.at == observation.at and r.state not in (observation.state, "unknown") for r in state.observations):
                raise LedgerUnavailable("conflicting instance observations")
            if observation not in state.observations:
                state.observations.append(observation)
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
            if reading not in state.reconciliations:
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

    def estimate(self, settings: SpendingSettings, now: int) -> CostEstimate:
        with self._locked() as state:
            return _charges(state, settings, now)

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
                state.decision = _decision_record(decision)
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
