"""The readings log and the weighted pace the dailies footer extends to 100% used."""

import json
import math
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from run_out import HALF_LIFE, MAX_GAP, MIN_SPAN, Reading, read_readings, weighted_rate

START = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


STEP = 600


def climb(used: list[float], *, start: datetime = START) -> list[Reading]:
    """One reading every ten minutes."""
    return [Reading(start + timedelta(seconds=STEP * index), percent) for index, percent in enumerate(used)]


def step(steps: float) -> float:
    return START.timestamp() + STEP * steps


class RunOutTests(unittest.TestCase):
    def test_readings_are_per_account_in_time_order_and_bad_lines_are_skipped(self) -> None:
        path = Path(self.enterContext(tempfile.TemporaryDirectory())) / "readings.jsonl"
        records: list[dict[str, object]] = [
            {"account": "claude 1", "at": (START + timedelta(hours=1)).isoformat(), "remaining": 70},
            {"account": "claude 1", "at": START.isoformat(), "remaining": 80},
            {"account": "codex 2", "at": START.isoformat(), "remaining": 55.5},
            {"account": "codex 2", "at": "2026-10-05T13:00:00", "remaining": 10},
            {"account": "codex 2", "at": START.isoformat(), "remaining": True},
            {"account": "codex 2", "at": START.isoformat(), "remaining": math.nan},
        ]
        _ = path.write_text("{bad json}\n[]\n" + "".join(json.dumps(record) + "\n" for record in records))
        self.assertEqual(read_readings(path), {
            "claude 1": [Reading(START, 20), Reading(START + timedelta(hours=1), 30)],
            "codex 2": [Reading(START, 44.5)],
        })
        self.assertEqual(read_readings(path.with_name("missing.jsonl")), {})

    def test_a_rise_twelve_hours_old_counts_half(self) -> None:
        self.assertEqual(HALF_LIFE, timedelta(hours=12))
        # Two hours at 1 point a step, then the same two hours 12 h later at 3 a step; the drop between them is a refill.
        old = climb([float(index) for index in range(13)])
        new = climb([5.0 + 3 * index for index in range(13)], start=START + HALF_LIFE)
        self.assertAlmostEqual(weighted_rate(old + new, new[-1].at.timestamp()) or 0, (0.5 * 1 + 3) / (1.5 * STEP))

    def test_quiet_recent_stretch_lengthens_time_left(self) -> None:
        rise = [5.0 * index for index in range(9)]
        busy_then_quiet = climb(rise + [40.0] * 8)
        quiet_then_busy = climb([0.0] * 8 + rise)
        plain = 40 / (16 * STEP)
        quiet = weighted_rate(busy_then_quiet, step(16)) or 0
        busy = weighted_rate(quiet_then_busy, step(16)) or 0
        self.assertLess(quiet, plain)
        self.assertGreater(busy, plain)
        self.assertGreater(60 / quiet, 60 / busy)
        # Each further quiet stretch lowers the pace, so the run-out moves later.
        paces = [weighted_rate(busy_then_quiet[:index + 1], step(index)) or 0 for index in range(8, 17)]
        self.assertEqual(paces, sorted(paces, reverse=True))
        self.assertEqual(len(set(paces)), len(paces))

    def test_refill_starts_a_new_stretch_and_the_one_before_still_counts(self) -> None:
        # An hour at 2 points a step, a refill, then an hour at 1 a step; each old rise is 7 steps older than its new one.
        readings = climb([50, 52, 54, 56, 58, 60, 62, 0, 1, 2, 3, 4, 5, 6])
        weight = math.pow(0.5, 7 * STEP / HALF_LIFE.total_seconds())
        self.assertAlmostEqual(weighted_rate(readings, step(13)) or 0, (2 * weight + 1) / ((weight + 1) * STEP))

    def test_time_at_full_is_skipped(self) -> None:
        readings = climb([88, 90, 92, 94, 96, 98, 100, 100, 100, 100, 0, 2, 4, 6, 8, 10, 12])
        self.assertAlmostEqual(weighted_rate(readings, step(16)) or 0, 2 / STEP)

    def test_time_logged_out_is_not_quiet_time(self) -> None:
        before = climb([float(index) for index in range(8)])
        after = climb([7.0 + index for index in range(8)], start=START + timedelta(days=2))
        self.assertAlmostEqual(weighted_rate(before + after, after[-1].at.timestamp()) or 0, 1 / STEP)
        # A reading `MAX_GAP` after the last still counts as quiet time; one second later it does not.
        last = before[-1].at
        counted = weighted_rate(before + [Reading(last + MAX_GAP, 7)], (last + MAX_GAP).timestamp()) or 0
        skipped = weighted_rate(before + [Reading(last + MAX_GAP + timedelta(seconds=1), 7)], (last + MAX_GAP).timestamp() + 1) or 0
        self.assertLess(counted, 1 / STEP)
        self.assertAlmostEqual(skipped, 1 / STEP)

    def test_readings_after_end_are_ignored(self) -> None:
        self.assertAlmostEqual(weighted_rate(climb([0, 1, 2, 3, 4, 5, 6, 7, 50]), step(7)) or 0, 1 / STEP)

    def test_no_pace_until_the_readings_cover_an_hour(self) -> None:
        self.assertEqual(MIN_SPAN, timedelta(hours=1))
        self.assertIsNone(weighted_rate([], step(0)))
        self.assertIsNone(weighted_rate(climb([40]), step(0)))
        self.assertIsNone(weighted_rate(climb([90, 20]), step(1)))
        self.assertIsNone(weighted_rate(climb([100.0] * 9), step(8)))
        # Six ten-minute steps weigh a little under an hour; the seventh carries it over.
        self.assertIsNone(weighted_rate(climb([0, 3, 6, 9, 12, 15, 18]), step(6)))
        self.assertAlmostEqual(weighted_rate(climb([0, 3, 6, 9, 12, 15, 18, 21]), step(7)) or 0, 3 / STEP)
        self.assertEqual(weighted_rate(climb([40.0] * 8), step(7)), 0)


if __name__ == "__main__":
    _ = unittest.main()
