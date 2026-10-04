#!/usr/bin/env python3
"""dailies_render.py's build hold release line, run as the skill runs it."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path


SCRIPT = Path(__file__).with_name("dailies_render.py")
# A Sunday morning in the report's zone.
AT = "2026-10-04T11:00"
FENCE = "```"


@dataclass(frozen=True)
class Run:
    code: int
    lines: list[str]
    error: str


def unit(build_hold: str | None) -> dict[str, object]:
    fields: dict[str, object] = {
        "unit": "widget-enhancements",
        "label": "widget",
        "project": "panel widgets that work by keyboard and draw cleanly",
        "phase": "Phase 2 of 3: small text reads clearly",
        "started": "2026-10-04T08:50",
        "held": None,
        "update": "building the fix for small text",
        "eta": {"time": "12:40", "percent": 60},
    }
    if build_hold is not None:
        fields["build_hold"] = build_hold
    return fields


def report(build_hold: str | None, release: str | None) -> dict[str, object]:
    fields: dict[str, object] = {
        "length": "simple",
        "zone": "America/Los_Angeles",
        "next_run": "11:30",
        "units": [unit(build_hold)],
    }
    if release is not None:
        fields["build_hold_release"] = release
    return fields


def render(fields: dict[str, object]) -> Run:
    """Run the renderer at AT with HOME in a scratch directory, so no chart conf from this machine applies."""
    with tempfile.TemporaryDirectory() as scratch:
        input_path = Path(scratch) / "dailies_input.json"
        _ = input_path.write_text(json.dumps(fields))
        result = subprocess.run(
            ["python3", str(SCRIPT), str(input_path), "--at", AT],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "HOME": scratch},
        )
    return Run(result.returncode, result.stdout.splitlines(), result.stderr)


HOLD = "since 10:56, for the frame-time lane's release timings"


class BuildHoldReleaseTests(unittest.TestCase):
    def tail(self, run: Run) -> list[str]:
        """The lines after the timeline's closing fence."""
        self.assertEqual(run.code, 0, run.error)
        closing = len(run.lines) - 1 - run.lines[::-1].index(FENCE)
        return run.lines[closing + 1 :]

    def test_time_sits_between_timeline_and_last_line(self) -> None:
        tail = self.tail(render(report(HOLD, "11:15")))
        self.assertEqual(tail, ["", "build hold - release eta: 11:15 PDT (15 minutes)", "", "next run at 11:30 - nothing needed"])

    def test_one_minute_is_singular(self) -> None:
        tail = self.tail(render(report(HOLD, "11:01")))
        self.assertEqual(tail[1], "build hold - release eta: 11:01 PDT (1 minute)")

    def test_at_the_time_counts_zero(self) -> None:
        tail = self.tail(render(report(HOLD, "11:00")))
        self.assertEqual(tail[1], "build hold - release eta: 11:00 PDT (0 minutes)")

    def test_past_time_is_overdue(self) -> None:
        tail = self.tail(render(report(HOLD, "10:54")))
        self.assertEqual(tail[1], "build hold - release eta: 10:54 PDT (overdue 6 minutes)")

    def test_one_minute_overdue_is_singular(self) -> None:
        tail = self.tail(render(report(HOLD, "10:59")))
        self.assertEqual(tail[1], "build hold - release eta: 10:59 PDT (overdue 1 minute)")

    def test_later_day_shows_its_weekday(self) -> None:
        tail = self.tail(render(report(HOLD, "09:30+1")))
        self.assertEqual(tail[1], "build hold - release eta: Mon 09:30 PDT (1350 minutes)")

    def test_none_text_prints_as_given(self) -> None:
        tail = self.tail(render(report(HOLD, "none measured - requested")))
        self.assertEqual(tail[1], "build hold - release eta: none measured - requested")

    def test_no_hold_prints_no_line(self) -> None:
        tail = self.tail(render(report(None, None)))
        self.assertEqual(tail, ["", "next run at 11:30 - nothing needed"])

    def test_hold_without_release_is_refused(self) -> None:
        run = render(report(HOLD, None))
        self.assertEqual(run.code, 2)
        self.assertIn("input.build_hold_release: required", run.error)

    def test_release_without_hold_is_refused(self) -> None:
        run = render(report(None, "11:15"))
        self.assertEqual(run.code, 2)
        self.assertIn("input.build_hold_release: only while a unit has build_hold", run.error)

    def test_release_that_is_neither_time_nor_none_text_is_refused(self) -> None:
        run = render(report(HOLD, "soon"))
        self.assertEqual(run.code, 2)
        self.assertIn("input.build_hold_release: 'soon'", run.error)


if __name__ == "__main__":
    _ = unittest.main()
