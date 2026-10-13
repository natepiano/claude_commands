"""Tests for statusline.jq: the usage line, its reset times, and their zone."""
from __future__ import annotations

import json
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import final, override
from zoneinfo import ZoneInfo

PROGRAM = Path(__file__).with_name("statusline.jq")
USER_ZONE = ZoneInfo("America/New_York")


def status_lines(*inputs: object, labels: Path | None = None, account: str = "claude 1") -> list[str]:
    """Run statusline.jq once over every input and return its output lines.

    `labels` is the by-label readings file, passed as the settings command passes it.
    """
    stdin = "\n".join(json.dumps(each) for each in inputs)
    stored = [] if labels is None else ["--slurpfile", "labels", str(labels)]
    result = subprocess.run(
        ["jq", "-r", "--arg", "pwd", "/tmp", "--arg", "account", account, *stored, "-f", str(PROGRAM)],
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



@final
class StoredReadingTests(unittest.TestCase):
    """The weekly and 5-hour figures agent_notes stored for the session's label."""

    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.directory = Path()
        self.now = 0
        self.reset = 0
        self.live: dict[str, object] = {}

    @override
    def setUp(self) -> None:
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.now = int(time.time())
        self.reset = self.now + 3 * 86400
        self.live = limits({"used_percentage": 10, "resets_at": self.now + 3600},
                           {"used_percentage": 97, "resets_at": self.now + 86400})

    def labels(self, readings: dict[str, object]) -> Path:
        path = self.directory / "by-label.json"
        _ = path.write_text(json.dumps(readings), encoding="utf-8")
        return path

    def weekly(self, left: float, at: int, resets_at: int | None) -> dict[str, object]:
        return {"weekly": {"left": left, "resets_at": resets_at, "at": at}}

    def test_a_fresh_reading_for_the_label_replaces_the_response_figure(self) -> None:
        path = self.labels({"claude 1": self.weekly(2.0, self.now - 120, self.reset)})
        self.assertEqual(
            status_lines(self.live, labels=path)[1].split(" | ")[1],
            f"weekly 2% left until {user_time(self.reset, '%a %H:%M %Z')}",
        )

    def test_a_reading_older_than_ten_minutes_gives_the_response_figure(self) -> None:
        path = self.labels({"claude 1": self.weekly(2.0, self.now - 601, self.reset)})
        self.assertTrue(status_lines(self.live, labels=path)[1].endswith("| weekly 3% left until "
                        + user_time(self.now + 86400, "%a %H:%M %Z")))

    def test_a_reading_whose_window_has_reset_gives_the_response_figure(self) -> None:
        path = self.labels({"claude 1": self.weekly(2.0, self.now - 60, self.now - 1)})
        self.assertIn("weekly 3% left", status_lines(self.live, labels=path)[1])

    def test_another_label_s_reading_is_not_used(self) -> None:
        path = self.labels({"claude 2": self.weekly(50.0, self.now - 60, self.reset)})
        self.assertIn("weekly 3% left", status_lines(self.live, labels=path)[1])

    def test_a_missing_file_given_as_dev_null_gives_the_response_figure(self) -> None:
        self.assertIn("weekly 3% left", status_lines(self.live, labels=Path("/dev/null"))[1])

    def test_a_none_open_five_hour_reading_shows_all_left_with_no_reset(self) -> None:
        path = self.labels({"claude 1": {"five_hour": {"left": 100.0, "resets_at": None, "at": self.now - 60}}})
        self.assertTrue(status_lines(self.live, labels=path)[1].startswith("5-hour 100% left | weekly"))

    def test_an_address_as_the_account_never_reaches_the_line(self) -> None:
        line = status_lines(self.live, account="someone@example.invalid")[0]
        self.assertNotIn("@", line)


if __name__ == "__main__":
    _ = unittest.main()
