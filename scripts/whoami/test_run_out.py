"""Run-out detection, replay, and the lean the footer learns from them."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import override

import run_out
from run_out import Reading, episodes, latest_drop, lean, ratios, read_run_outs, record_run_outs, trailing_rate

START = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


def climb(used: list[float], *, minutes: int = 60, start: datetime = START) -> list[Reading]:
    return [Reading(start + timedelta(minutes=minutes * index), percent) for index, percent in enumerate(used)]


class RunOutTests(unittest.TestCase):
    root: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def write_readings(self, account: str, readings: list[Reading]) -> Path:
        path = self.root / "readings.jsonl"
        with path.open("a", encoding="utf-8") as output:
            for reading in readings:
                _ = output.write(json.dumps({"account": account, "at": reading.at.isoformat(), "remaining": 100 - reading.used_percent}) + "\n")
        return path

    def write_run_outs(self, *ratio_lists: list[float]) -> Path:
        path = self.root / "run_outs.jsonl"
        _ = path.write_text("".join(
            json.dumps({"account": "claude 1", "ended": (START + timedelta(days=index)).isoformat(), "ratios": values}) + "\n"
            for index, values in enumerate(ratio_lists)
        ))
        return path

    def test_trailing_rate_needs_an_hour(self) -> None:
        readings = climb([10, 20], minutes=30)
        self.assertIsNone(trailing_rate(readings, START.timestamp(), (START + timedelta(hours=1)).timestamp()))
        readings = climb([10, 20])
        self.assertAlmostEqual(trailing_rate(readings, START.timestamp(), (START + timedelta(hours=1)).timestamp()) or 0, 10 / 3600)

    def test_latest_drop_finds_refill_then_redeemed_reset(self) -> None:
        readings = climb([42, 99, 0, 19, 5, 20])
        self.assertEqual(latest_drop(readings, readings[3].at.timestamp()), readings[2].at.timestamp())
        self.assertEqual(latest_drop(readings, readings[-1].at.timestamp()), readings[4].at.timestamp())

    def test_latest_drop_ignores_readings_after_end(self) -> None:
        readings = climb([42, 99, 0, 19, 5])
        self.assertEqual(latest_drop(readings, readings[3].at.timestamp()), readings[2].at.timestamp())
        self.assertIsNone(latest_drop(readings, readings[1].at.timestamp()))

    def test_latest_drop_is_none_without_a_drop(self) -> None:
        self.assertIsNone(latest_drop([], START.timestamp()))
        self.assertIsNone(latest_drop(climb([0, 20, 20, 99]), (START + timedelta(hours=3)).timestamp()))

    def test_run_out_at_empty_and_reset_when_nearly_out_count(self) -> None:
        emptied = climb([80, 90, 100, 100])
        reset_low = climb([90, 96, 96, 0, 10], start=START + timedelta(hours=4))
        reset_high = climb([40, 50, 0], start=START + timedelta(hours=9))
        still_going = climb([60, 70], start=START + timedelta(hours=12))
        found = episodes(emptied + reset_low + reset_high + still_going)
        self.assertEqual([reached for _, reached in found], [emptied[2], reset_low[1]])
        self.assertEqual(found[0][0], emptied[:3])
        self.assertEqual(found[1][0], reset_low[:2])

    def test_steady_pace_scores_one_and_a_late_burst_scores_under_one(self) -> None:
        steady = climb([70, 80, 90, 100])
        span, reached = episodes(steady)[0]
        self.assertEqual([round(ratio, 3) for ratio in ratios(span, reached)], [1.0, 1.0])
        burst = climb([70, 75, 80, 100])
        span, reached = episodes(burst)[0]
        self.assertEqual([round(ratio, 3) for ratio in ratios(span, reached)], [0.25, 0.4])

    def test_each_run_out_is_recorded_once(self) -> None:
        readings = self.write_readings("claude 1", climb([70, 75, 80, 100]))
        run_outs = self.root / "run_outs.jsonl"
        self.assertEqual(record_run_outs(readings, run_outs),
                         ["claude 1: run-out at 2026-10-05T15:00:00+00:00 recorded; footer lean now 0.29"])
        _ = self.write_readings("claude 1", climb([100, 0], start=START + timedelta(hours=4)))
        self.assertEqual(record_run_outs(readings, run_outs), [])
        self.assertEqual(read_run_outs(run_outs), [
            {"account": "claude 1", "ended": "2026-10-05T15:00:00+00:00", "ratios": [0.25, 0.4]},
        ])

    def test_lean_is_one_until_learned_then_the_lower_quartile(self) -> None:
        self.assertEqual(lean(self.root / "missing.jsonl"), 1.0)
        self.assertEqual(lean(self.write_run_outs([0.8])), 0.8)
        self.assertAlmostEqual(lean(self.write_run_outs([0.5, 0.7], [0.8, 0.9, 1.0])), 0.7)

    def test_lean_follows_only_the_latest_run_outs(self) -> None:
        path = self.write_run_outs(*([[0.1]] * 3 + [[0.9]] * run_out.KEPT))
        self.assertAlmostEqual(lean(path), 0.9)

    def test_bad_lines_and_repeated_run_outs_are_skipped(self) -> None:
        path = self.write_run_outs([0.5])
        _ = path.write_text("{bad json}\n[]\n" + path.read_text() * 2
                            + json.dumps({"account": "codex 2", "ended": START.isoformat(), "ratios": [True, "x", -1, 0.6]}) + "\n")
        self.assertEqual(read_run_outs(path), [
            {"account": "claude 1", "ended": START.isoformat(), "ratios": [0.5]},
            {"account": "codex 2", "ended": START.isoformat(), "ratios": [0.6]},
        ])


if __name__ == "__main__":
    _ = unittest.main()
