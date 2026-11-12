"""Private configuration and fixed times for cost tests."""
from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
import os
import re
from decimal import Decimal
from pathlib import Path
import subprocess
import tempfile
from typing import cast, override
import unittest
from unittest import mock

from scripts.lint.memory_test_support import config_environment, write_config_fixture
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.aws_target import ledger


REAL_RUN = subprocess.run
REAL_POPEN = subprocess.Popen


def git_only(arguments: Sequence[str], **options: object) -> subprocess.CompletedProcess[str]:
    """Permit private history reads and reject all AWS or build commands."""
    if arguments[0] != "git":
        raise AssertionError(f"Unexpected external command: {arguments}")
    return REAL_RUN(list(arguments), capture_output=True, text=True,
                    check=bool(options.get("check", False)), timeout=TEST_HANG_GUARD_SECS)


def instant(value: str) -> int:
    """Convert an explicit zone timestamp to an integer accounting boundary."""
    return int(datetime.fromisoformat(value).timestamp())


def numeric_settings(path: Path, values: dict[str, str]) -> None:
    """Give each private cost its explicit numeric limit."""
    lines: list[str] = []
    for line in path.read_text().splitlines():
        if line.partition('=')[0].strip() in values:
            if lines and re.fullmatch(r'# (whole|range): .*', lines[-1]):
                _ = lines.pop()
        else:
            lines.append(line)
    for key, value in values.items():
        form = "whole" if key.endswith(("_seconds", "_timeout_s", "_days")) else "range"
        if key in ("connect_timeout_s", "leftover_timeout_s"):
            form = "range"
        lines.extend((f"# {form}: 0..", f"{key}={value}"))
    _ = path.write_text("\n".join(lines) + "\n")


def text_setting(path: Path, key: str, value: str) -> None:
    lines = [line for line in path.read_text().splitlines()
             if line.partition("=")[0].strip() != key]
    _ = path.write_text("\n".join([*lines, f"{key}={value}"]) + "\n")


def install_cost_settings(path: Path, overrides: str = "") -> None:
    """Install complete settings with exact, private test prices."""
    _ = write_config_fixture(path, "offload=on\nregion=us-east-1\ninstance_id=i-fixture\n" + overrides)
    numeric_settings(path, {
        "daily_limit_usd": "20", "monthly_limit_usd": "400", "limit_reserve_usd": "3",
        "fallback_usd_per_hour": "1", "fixed_usd_per_month": "0",
        "retained_storage_usd_per_month": "0", "root_storage_usd_per_month": "0",
        "ipv4_usd_per_month": "0", "gp3_iops_usd_per_month": "0",
        "gp3_throughput_usd_per_month": "0", "cost_explorer_read_usd": "0",
        "price_refresh_seconds": "3600", "price_max_age_seconds": "7200",
        "observation_max_age_seconds": "86400", "cost_explorer_interval_seconds": "86400",
        "notification_timeout_s": "5", "notification_retry_s": "60", "cost_explorer_lookback_days": "1",
    })
    text_setting(path, "cost_explorer_tag_key", "Project")
    text_setting(path, "cost_explorer_tag_value", "fixture-build")
    text_setting(path, "accounting_zone", "UTC")
    text_setting(path, "notification_recipient", "fixture-showrunner")


def account_reply(arguments: Sequence[str], amount: str = '0') -> dict[str, object]:
    """Return daily usage costs for the requested UTC period."""
    period = arguments[arguments.index('--time-period') + 1]
    bounds = dict(part.split('=', 1) for part in period.split(','))
    start, end = (datetime.fromisoformat(bounds[key]) for key in ('Start', 'End'))
    days: list[dict[str, object]] = []
    while start < end:
        following = start + timedelta(days=1)
        days.append({'TimePeriod': {'Start': start.date().isoformat(), 'End': following.date().isoformat()},
                     'Estimated': False, 'Total': {}, 'Groups': [
                         {'Keys': ['USE2-SpotUsage:m7a.4xlarge'],
                          'Metrics': {'UnblendedCost': {'Amount': amount, 'Unit': 'USD'},
                                      'UsageQuantity': {'Amount': '1', 'Unit': 'Hrs'}}}]})
        start = following
    return {'ResultsByTime': days}


def charge_readings(start: int, end: int, usd: Decimal, query_usd: Decimal) -> tuple[ledger.ChargeRead, ...]:
    """Return account bills without an AWS command."""
    return tuple(ledger.ChargeRead(datetime.fromtimestamp(at, UTC).date().isoformat(), end, at, at + 86400,
                                  usd, Decimal(0), query_usd if at + 86400 == end else Decimal(0),
                                  (ledger.UsageTypeCost('USE2-SpotUsage:m7a.4xlarge', usd),), False)
                 for at in range(start, end, 86400))


class CostFixture(unittest.TestCase):
    root: Path = Path()
    settings: Path = Path()
    state: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.settings = self.root / "aws_target.conf"
        self.state = self.root / "state"
        self.state.mkdir()
        environment = config_environment(self.root)
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            **environment, "HOME": str(self.root),
            "AWS_TARGET_CONFIG": str(self.settings),
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
        })))
        install_cost_settings(self.settings)

    def configure(self, overrides: str) -> None:
        _ = write_config_fixture(self.settings, overrides)

    def costs(self, values: dict[str, str]) -> None:
        numeric_settings(self.settings, values)

    def text_setting(self, key: str, value: str) -> None:
        text_setting(self.settings, key, value)

    def replace(self, key: str, value: str) -> None:
        lines = self.settings.read_text().splitlines()
        for index, line in enumerate(lines):
            if line.partition("=")[0].strip() == key:
                lines[index] = f"{key}={value}"
                break
        else:
            raise AssertionError(f"Fixture setting is absent: {key}")
        _ = self.settings.write_text("\n".join(lines) + "\n")

    def remove(self, key: str) -> None:
        lines = [line for line in self.settings.read_text().splitlines()
                 if line.partition("=")[0].strip() != key]
        _ = self.settings.write_text("\n".join(lines) + "\n")

    def commit_settings(self) -> None:
        for arguments in (("init", "-q"), ("config", "user.name", "Cost fixture"),
                          ("config", "user.email", "cost@example.invalid"),
                          ("add", self.settings.name), ("commit", "-qm", "Record valid settings")):
            _ = subprocess.run(["git", "-C", str(self.root), *arguments], check=True,
                               capture_output=True, timeout=TEST_HANG_GUARD_SECS)
