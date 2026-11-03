"""Require live, validated settings before permitting AWS spending."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from scripts.lib import conf


@dataclass(frozen=True)
class MeasurementUnavailable:
    reason: str


@dataclass(frozen=True)
class LimitSettingEvidence:
    key: str
    origin: Literal["live", "recovered", "unavailable"]
    reason: str


class SpendingConfigurationUnavailable(ValueError):
    def __init__(self, reason: str, limits: tuple[LimitSettingEvidence, ...]) -> None:
        super().__init__(reason)
        self.limits: tuple[LimitSettingEvidence, ...] = limits


@dataclass(frozen=True)
class SpendingSettings:
    daily_limit_usd: Decimal
    monthly_limit_usd: Decimal
    limit_reserve_usd: Decimal
    fallback_usd_per_hour: Decimal
    fixed_usd_per_month: Decimal
    fixed_components: tuple[tuple[str, Decimal], ...]
    accounting_zone: ZoneInfo
    price_refresh_seconds: int
    price_max_age_seconds: int
    observation_max_age_seconds: int
    cost_explorer_interval_seconds: int
    cost_explorer_read_usd: Decimal
    notification_recipient: str
    notification_timeout_s: int
    notification_retry_s: int
    cost_explorer_tag_key: str
    cost_explorer_tag_value: str
    limits: tuple[LimitSettingEvidence, ...]


def money(value: str) -> Decimal:
    try:
        amount = Decimal(value)
    except InvalidOperation as error:
        raise ValueError("expected finite nonnegative money") from error
    if not amount.is_finite() or amount < 0:
        raise ValueError("expected finite nonnegative money")
    return amount


def positive_seconds(value: str) -> int:
    seconds = int(value)
    if seconds <= 0:
        raise ValueError("expected positive whole seconds")
    return seconds


def read_settings(path: Path) -> SpendingSettings:
    """Refuse recovered spending data while preserving ordinary configuration recovery."""
    limits: list[LimitSettingEvidence] = []
    errors: list[str] = []

    def amount(key: str) -> Decimal:
        origin = conf.value_origin(path, key, money)
        state = "live" if isinstance(origin, conf.UsableLiveValue) else "recovered" if isinstance(origin, conf.RecoveredValue) else "unavailable"
        reason = "" if isinstance(origin, conf.UsableLiveValue) else origin.reason
        if key in ("daily_limit_usd", "monthly_limit_usd"):
            limits.append(LimitSettingEvidence(key, state, reason))
        if not isinstance(origin, conf.UsableLiveValue):
            errors.append(f"{key}: {reason}; live spending permission is unavailable")
            return Decimal(0)
        return origin.value

    daily = amount("daily_limit_usd")
    monthly = amount("monthly_limit_usd")
    reserve = amount("limit_reserve_usd")
    fallback = amount("fallback_usd_per_hour")
    fixed = amount("fixed_usd_per_month")
    components = tuple((key, amount(key)) for key in (
        "retained_storage_usd_per_month", "root_storage_usd_per_month", "ipv4_usd_per_month",
        "gp3_iops_usd_per_month", "gp3_throughput_usd_per_month"))
    explorer = amount("cost_explorer_read_usd")
    if errors:
        raise SpendingConfigurationUnavailable("; ".join(errors), tuple(limits))

    def live_text(key: str) -> str:
        result = conf.value_origin(path, key, str)
        if not isinstance(result, conf.UsableLiveValue):
            raise SpendingConfigurationUnavailable(result.reason, tuple(limits))
        if not result.value:
            raise SpendingConfigurationUnavailable(f"{key}: value is empty", tuple(limits))
        return result.value

    def seconds(key: str) -> int:
        result = conf.value_origin(path, key, positive_seconds)
        if not isinstance(result, conf.UsableLiveValue):
            raise SpendingConfigurationUnavailable(result.reason, tuple(limits))
        return result.value

    if live_text("limit_action") != "drain":
        raise SpendingConfigurationUnavailable("limit_action: expected drain", tuple(limits))
    try:
        zone = ZoneInfo(live_text("accounting_zone"))
    except (ValueError, KeyError) as error:
        raise SpendingConfigurationUnavailable(f"accounting_zone: {error}", tuple(limits)) from error
    return SpendingSettings(daily, monthly, reserve, fallback, max(fixed, sum((cost for _, cost in components), Decimal(0))),
                            components, zone, seconds("price_refresh_seconds"), seconds("price_max_age_seconds"),
                            seconds("observation_max_age_seconds"), seconds("cost_explorer_interval_seconds"),
                            explorer, live_text("notification_recipient"), seconds("notification_timeout_s"),
                            seconds("notification_retry_s"), live_text("cost_explorer_tag_key"),
                            live_text("cost_explorer_tag_value"), tuple(limits))


@dataclass(frozen=True)
class ControlSettings:
    enabled: bool
    instance_type: str
    region: str
    instance_id: str
    credentials_profile: str
    start_after_wait_s: float
    start_min_waiting: int
    capacity_backoff_s: float
    drain_max_minutes: float
    idle_stop_minutes: float
    tick_interval_s: float
    aws_timeout_s: float
    snapshot_max_age_s: float
    permission_ttl_s: float
    interruption_poll_s: float
    imds_timeout_s: float
    remote_state_directory: str


def offload_enabled(path: Path) -> bool:
    return conf.text(path, 'offload') == 'on'


def read_control_settings(path: Path) -> ControlSettings:
    values = conf.values(path)
    enabled = offload_enabled(path)
    def required(key: str) -> str:
        value = conf.text(path, key)
        if not value.strip():
            raise ValueError(f'{key}: expected nonempty text')
        return value
    return ControlSettings(enabled, required('instance_type'), required('region') if enabled else values.get('region', ''),
                           required('instance_id') if enabled else values.get('instance_id', ''),
                           required('credentials_profile'), conf.number(path, 'start_after_wait_s'),
                           conf.whole(path, 'start_min_waiting'), conf.number(path, 'capacity_backoff_s'),
                           conf.number(path, 'drain_max_minutes'), conf.number(path, 'idle_stop_minutes'),
                           conf.number(path, 'tick_interval_s'), conf.number(path, 'aws_timeout_s'),
                           conf.number(path, 'snapshot_max_age_s'), conf.number(path, 'permission_ttl_s'),
                           conf.number(path, 'interruption_poll_s'), conf.number(path, 'imds_timeout_s'),
                           required('remote_state_directory'))
