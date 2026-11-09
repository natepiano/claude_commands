"""Read local AWS measurements and reconcile scheduled charge replies."""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import json
from pathlib import Path
from enum import Enum

from scripts.aws_target import ledger
from scripts.aws_target.ledger import ChargeRead as ChargeRead, read_charge as read_charge, charge_record as charge_record
from scripts.aws_target.config import MeasurementUnavailable as MeasurementUnavailable, SpendingSettings, offload_enabled, read_settings
from scripts.lib.atomic_file import create_text_atomic
from scripts.lib import conf
from scripts.mac_test import mac_test


class ControllerRecovery(Enum):
    CONTROLLER_TICK = 'controller_tick'
    CONFIRM_STOP = 'confirm_stop'
    STATE_EVIDENCE_RELEASE = 'state_evidence_release'


@dataclass(frozen=True)
class ControllerSnapshot:
    state: str
    at: int
    reason: str
    recovery: ControllerRecovery
    starting_at: int


@dataclass(frozen=True)
class AwsStatusMeasurement:
    controller: ControllerSnapshot
    permission: ledger.AdmissionPermission
    costs: ledger.CostEstimate | ledger.CostEvidenceUnavailable
    events: tuple[ledger.LimitEvent, ...]
    monthly_limit_usd: Decimal | MeasurementUnavailable
    enabled: bool


@dataclass(frozen=True)
class AwsMeasurement(AwsStatusMeasurement):
    charge_reads: tuple[ChargeRead, ...]
    go_live_at: int | MeasurementUnavailable


@dataclass(frozen=True)
class MeasurementInputs:
    state: ledger.LedgerEvidence
    settings: SpendingSettings | MeasurementUnavailable


def read_inputs(settings_path: Path, directory: Path) -> MeasurementInputs:
    book = ledger.CostLedger(directory)
    try:
        state = book.read_snapshot()
    except ledger.LedgerUnavailable:
        if book.path.exists():
            raise
        state = ledger.LedgerEvidence()
    try:
        settings: SpendingSettings | MeasurementUnavailable = read_settings(settings_path)
    except (OSError, ValueError, ArithmeticError) as error:
        settings = MeasurementUnavailable(str(error))
    return MeasurementInputs(state, settings)


def record_go_live(directory: Path, now: int) -> None:
    """Keep the first permitted running observation as the comparison boundary."""
    path = directory / 'go-live.json'
    if not path.exists():
        _ = create_text_atomic(path, json.dumps({'at': now}) + '\n')


def read_status_measurement(settings_path: Path, directory: Path, now: int, *,
                            inputs: MeasurementInputs | None = None) -> AwsStatusMeasurement | MeasurementUnavailable:
    try:
        enabled = offload_enabled(conf.read(settings_path))
        source = read_inputs(settings_path, directory) if inputs is None else inputs
        state, settings = source.state, source.settings
        row = mac_test.read_json_object(directory / 'controller.json')
        if row is None:
            controller = ControllerSnapshot('unavailable' if enabled else 'off', 0, 'no controller evidence',
                                            ControllerRecovery.CONTROLLER_TICK, 0)
        else:
            controller = ControllerSnapshot(str(row.get('state', 'unavailable')), ledger.instant(row, 'at'),
                                            str(row.get('reason', '')),
                                            ControllerRecovery.STATE_EVIDENCE_RELEASE if (directory / 'release-evidence').exists()
                                            else ControllerRecovery.CONFIRM_STOP,
                                            ledger.instant(row, 'starting_at') if 'starting_at' in row else ledger.instant(row, 'at'))
        permission = ledger.decode_permission(state.decision)
        try:
            if isinstance(settings, MeasurementUnavailable):
                raise ValueError(settings.reason)
            costs: ledger.CostEstimate | ledger.CostEvidenceUnavailable = ledger.costs_from_evidence(state, settings, now)
        except (OSError, ValueError, ArithmeticError) as error:
            costs = ledger.CostEvidenceUnavailable(str(error))
            permission = ledger.PermissionUnavailable(permission.at, str(error), permission.evidence)
        cap = (MeasurementUnavailable(settings.reason) if isinstance(settings, MeasurementUnavailable)
               else settings.monthly_limit_usd)
        return AwsStatusMeasurement(controller, permission, costs, tuple(state.events), cap, enabled)
    except (OSError, ValueError, KeyError, TypeError, ArithmeticError) as error:
        return MeasurementUnavailable(str(error))


def read_measurement(settings_path: Path, directory: Path, now: int, *,
                     inputs: MeasurementInputs | None = None) -> AwsMeasurement | MeasurementUnavailable:
    status = read_status_measurement(settings_path, directory, now, inputs=inputs)
    if isinstance(status, MeasurementUnavailable):
        return status
    try:
        go_live_row = mac_test.read_json_object(directory / 'go-live.json')
        go_live = ledger.instant(go_live_row, 'at') if go_live_row is not None else MeasurementUnavailable('go-live time is not recorded')
        readings = tuple(read_charge(path) for path in sorted((directory / 'charges').glob('*.json')))
        return AwsMeasurement(status.controller, status.permission, status.costs, status.events,
                              status.monthly_limit_usd, status.enabled, readings, go_live)
    except (OSError, ValueError, KeyError, TypeError, ArithmeticError) as error:
        return MeasurementUnavailable(str(error))


def charge_period_day(_settings: SpendingSettings, now: int) -> date:
    return ledger.charge_period_day(now)


def read_charges(settings_path: Path, directory: Path, now: int,
                 fetch: Callable[[int, int], Sequence[ChargeRead]]) -> ChargeRead | MeasurementUnavailable:
    """Read charges through the ledger's schedule and recovery lock."""
    return ledger.CostLedger(directory).refresh_charges(settings_path, now, fetch)


def number(value: object) -> Decimal:
    if isinstance(value, int | float | str) and not isinstance(value, bool):
        return conf.decimal(str(value))
    raise ValueError('expected a finite number')


@dataclass(frozen=True)
class DailyUsage:
    uptime_s: int
    usd: Decimal


def daily_usage(settings_path: Path, directory: Path, day: str, now: int, *,
                inputs: MeasurementInputs | None = None) -> DailyUsage | MeasurementUnavailable:
    try:
        source = read_inputs(settings_path, directory) if inputs is None else inputs
        if isinstance(source.settings, MeasurementUnavailable):
            return source.settings
        start, end = ledger.period_bounds(date.fromisoformat(day), source.settings.accounting_zone)
        end = min(now, end)
        if end < start:
            return MeasurementUnavailable('report day is in the future')
        costs = ledger.costs_from_evidence(source.state, source.settings, end, window=date.fromisoformat(day))
        uptime = sum(row.confirmed_uptime_s for row in costs.intervals)
        usd = ledger.period_cost(costs.intervals, start, end)
        book = ledger.CostLedger(directory)
        if source.state.gate_reservations or any(ledger.is_billed_day(row) for row in book.read_bills()):
            usd = ledger.period_cost((charge for spend in book.spend_days(source.settings, now)
                                      for charge in spend.intervals), start, end)
        return DailyUsage(uptime, usd)
    except (OSError, ValueError, ArithmeticError) as error:
        return MeasurementUnavailable(str(error))
