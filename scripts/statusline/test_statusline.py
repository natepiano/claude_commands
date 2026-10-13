"""Tests for statusline.jq: the usage line, its reset times, and their zone."""
from __future__ import annotations

import json
import subprocess
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PROGRAM = Path(__file__).with_name("statusline.jq")
USER_ZONE = ZoneInfo("America/New_York")


def status_lines(*inputs: object) -> list[str]:
    """Run statusline.jq once over every input and return its output lines."""
    stdin = "\n".join(json.dumps(each) for each in inputs)
    result = subprocess.run(
        ["jq", "-r", "--arg", "pwd", "/tmp", "--arg", "account", "claude 1", "-f", str(PROGRAM)],
        input=stdin, capture_output=True, text=True, check=True,
    )
    return result.stdout.splitlines()


def limits(five_hour: dict[str, float | None], seven_day: dict[str, float | None]) -> dict[str, object]:
    return {"rate_limits": {"five_hour": five_hour, "seven_day": seven_day}}


def user_time(epoch: int, fmt: str) -> str:
    return datetime.fromtimestamp(epoch, USER_ZONE).strftime(fmt)


class UsageLineTests(unittest.TestCase):
    def test_reset_times_in_each_season(self) -> None:
        edt = int(datetime(2026, 10, 13, 18, 0, tzinfo=timezone.utc).timestamp())
        est = int(datetime(2026, 12, 3, 12, 0, tzinfo=timezone.utc).timestamp())
        lines = status_lines(limits({"used_percentage": 38, "resets_at": edt}, {"used_percentage": 60, "resets_at": est}))
        self.assertEqual(lines[1], "5-hour 62% left until 14:00 EDT | weekly 40% left until Thu 07:00 EST")

    def test_missing_reset_shows_percent_alone(self) -> None:
        lines = status_lines(limits({"used_percentage": 38}, {"used_percentage": 60, "resets_at": None}))
        self.assertEqual(lines[1], "5-hour 62% left | weekly 40% left")

    def test_missing_rate_limits_gives_one_line(self) -> None:
        self.assertEqual(len(status_lines({})), 1)

    def test_daylight_saving_edges_match_zoneinfo(self) -> None:
        """Each second either side of every transition, 2026 to 2040, reads as zoneinfo does."""
        epochs: list[int] = []
        hour = datetime(2026, 1, 1, tzinfo=timezone.utc)
        while hour.year < 2041:
            following = hour + timedelta(hours=1)
            if hour.astimezone(USER_ZONE).utcoffset() != following.astimezone(USER_ZONE).utcoffset():
                edge = int(following.timestamp())
                epochs.extend([edge - 1, edge])
            hour = following
        self.assertEqual(len(epochs), 15 * 2 * 2)
        inputs = [limits({"used_percentage": 0, "resets_at": t}, {"used_percentage": 0, "resets_at": t}) for t in epochs]
        lines = status_lines(*inputs)[1::2]
        expected = [
            f"5-hour 100% left until {user_time(t, '%H:%M %Z')} | weekly 100% left until {user_time(t, '%a %H:%M %Z')}"
            for t in epochs
        ]
        self.assertEqual(lines, expected)


if __name__ == "__main__":
    _ = unittest.main()
