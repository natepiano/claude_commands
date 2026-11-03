"""Require live, validated settings before permitting AWS spending."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal, TypeVar
from collections.abc import Callable
import subprocess
import sys
import time
from urllib.parse import urlsplit
import re
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


T = TypeVar('T')


def _live(path: Path, key: str, convert: Callable[[str], T]) -> T:
    result = conf.value_origin(path, key, convert)
    if not isinstance(result, conf.UsableLiveValue):
        raise ValueError(f'{key}: {result.reason}; live input is unavailable')
    return result.value


def service_loop(settings_path: Path, interval: str, action: Callable[[], object], *,
                 pause: Callable[[float], object] = time.sleep) -> None:
    """Repeat enabled work and retain the last usable interval after a read failure."""
    if not offload_enabled(settings_path):
        return
    delay = conf.number(settings_path, interval)
    while offload_enabled(settings_path):
        try:
            try:
                _ = action()
            finally:
                delay = conf.number(settings_path, interval)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            print(f'aws_service: {error}', file=sys.stderr)
        if not offload_enabled(settings_path):
            return
        _ = pause(delay)


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
        try:
            return _live(path, key, str)
        except ValueError as error:
            raise SpendingConfigurationUnavailable(str(error), tuple(limits)) from error

    def seconds(key: str) -> int:
        try:
            return _live(path, key, positive_seconds)
        except ValueError as error:
            raise SpendingConfigurationUnavailable(str(error), tuple(limits)) from error

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


@dataclass(frozen=True)
class DeploymentSettings:
    region: str
    profile: str
    stack_name: str
    instance_type: str
    ami_id: str
    ami_owner_id: str
    nixos_release: str
    availability_zone: str
    vpc_id: str
    subnet_id: str
    notification_email: str
    existing_data_volume_id: str
    root_device_name: str
    root_volume_gib: int
    data_volume_gib: int
    data_volume_iops: int
    data_volume_throughput: int
    spot_max_usd_per_hour: Decimal
    notice_low: int
    notice_high: int
    stop_percent: int
    timeout_s: int
    preflight_max_age_s: int
    scaler_user_name: str
    scripts_source_url: str
    scripts_source_commit: str
    scripts_source_sha256: str
    bootstrap_template_path: Path


def read_deployment_settings(path: Path) -> DeploymentSettings:
    """Require current deployment inputs before preparing any resource request."""
    def text(key: str) -> str:
        return _live(path, key, str)

    def whole(key: str) -> int:
        return _live(path, key, positive_seconds)

    def positive_money(key: str) -> Decimal:
        value = _live(path, key, money)
        if value <= 0:
            raise ValueError(f'{key}: live positive price is required')
        return value

    region = text('region')
    zone = text('availability_zone')
    instance_type = text('instance_type')
    if region not in ('us-east-1', 'us-east-2') or not zone.startswith(region):
        raise ValueError('select a supported region and its availability zone')
    email = text('notification_email')
    if '@' not in email or any(character.isspace() for character in email):
        raise ValueError('notification_email: expected one email address')
    owner = text('ami_owner_id')
    if len(owner) != 12 or not owner.isdecimal():
        raise ValueError('ami_owner_id: expected a twelve-digit account ID')
    low, high, stop = (whole(key) for key in (
        'budget_notice_low_percent', 'budget_notice_high_percent', 'budget_stop_percent'))
    if not 0 < low < high < stop:
        raise ValueError('Budget thresholds must increase')
    source_url = text('scripts_source_url')
    url = urlsplit(source_url)
    if url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError('scripts_source_url must contain a public HTTPS URL without credentials')
    commit, source_hash = text('scripts_source_commit'), text('scripts_source_sha256')
    if not re.fullmatch(r'[0-9a-f]{40}', commit) or not re.fullmatch(r'sha256-[A-Za-z0-9+/]{43}=', source_hash):
        raise ValueError('the public scripts need an exact commit and a Nix SHA-256 hash')
    template = Path(text('bootstrap_template_path')).expanduser()
    if not template.is_absolute():
        template = path.parent / template
    return DeploymentSettings(region, text('deployment_profile'), text('stack_name'), instance_type,
                              text('ami_id'), owner, text('nixos_release'), zone, text('vpc_id'),
                              text('subnet_id'), email, conf.values(path)['existing_data_volume_id'],
                              text('root_device_name'), whole('root_volume_gib'), whole('data_volume_gib'),
                              whole('data_volume_iops'), whole('data_volume_throughput'),
                              positive_money('spot_max_usd_per_hour'), low, high, stop,
                              whole('deployment_timeout_s'), whole('preflight_max_age_s'), text('scaler_user_name'),
                              source_url, commit, source_hash, template)


def deployment_price(path: Path, now: int) -> tuple[int, Decimal]:
    settings = read_settings(path)
    at = _live(path, 'deployment_price_at', positive_seconds)
    price = _live(path, 'deployment_price_usd_per_hour', money)
    if price <= 0 or not 0 <= now - at <= settings.price_max_age_seconds:
        raise ValueError('deployment requires a fresh public price recorded in live configuration')
    return at, price
