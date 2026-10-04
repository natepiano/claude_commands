#!/usr/bin/env python3
"""dailies_render.py's footer, in a report and alone, run as the skills run it."""

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
ZONE = "America/Los_Angeles"
FENCE = "```"


@dataclass(frozen=True)
class Run:
    code: int
    lines: list[str]
    error: str


def unit(build_hold: str | None, needed: str | None = None) -> dict[str, object]:
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
    if needed is not None:
        fields["needed"] = needed
    return fields


def report(build_hold: str | None, release: str | None, next_run: str | None = "11:30", needed: str | None = None) -> dict[str, object]:
    fields: dict[str, object] = {
        "length": "simple",
        "zone": ZONE,
        "units": [unit(build_hold, needed)],
    }
    if next_run is not None:
        fields["next_run"] = next_run
    if release is not None:
        fields["build_hold_release"] = release
    return fields


def run(arguments: list[str], scratch: str) -> Run:
    """Run the script with HOME in `scratch`, so no chart conf from this machine applies."""
    result = subprocess.run(
        ["python3", str(SCRIPT), *arguments],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "HOME": scratch},
    )
    return Run(result.returncode, result.stdout.splitlines(), result.stderr)


def render(fields: dict[str, object]) -> Run:
    """Render the report at AT."""
    with tempfile.TemporaryDirectory() as scratch:
        input_path = Path(scratch) / "dailies_input.json"
        _ = input_path.write_text(json.dumps(fields))
        return run([str(input_path), "--at", AT], scratch)


def footer(*arguments: str, at: str = AT) -> Run:
    """The footer alone, as a showrunner reply prints it."""
    with tempfile.TemporaryDirectory() as scratch:
        return run(["--footer", "--zone", ZONE, "--at", at, *arguments], scratch)


def after_timeline(lines: list[str]) -> list[str]:
    """The lines after the timeline's closing fence."""
    closing = len(lines) - 1 - lines[::-1].index(FENCE)
    return lines[closing + 1 :]


HOLD = "since 10:56, for the frame-time lane's release timings"


class ReportFooterTests(unittest.TestCase):
    def tail(self, run: Run) -> list[str]:
        self.assertEqual(run.code, 0, run.error)
        return after_timeline(run.lines)

    def test_release_line_opens_the_footer(self) -> None:
        tail = self.tail(render(report(HOLD, "11:15")))
        self.assertEqual(tail, ["", "build hold - release eta: 11:15 PDT (15 minutes)", "", "11:00 PDT · next dailies 11:30 PDT - nothing needed"])

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
        self.assertEqual(tail, ["", "11:00 PDT · next dailies 11:30 PDT - nothing needed"])

    def test_a_needed_subject_drops_nothing_needed(self) -> None:
        tail = self.tail(render(report(None, None, needed="the showrunner settles the shared font")))
        self.assertEqual(tail, ["", "11:00 PDT · next dailies 11:30 PDT"])

    def test_no_schedule_says_so(self) -> None:
        tail = self.tail(render(report(None, None, next_run=None)))
        self.assertEqual(tail, ["", "11:00 PDT · no dailies scheduled - nothing needed"])

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


class FooterAloneTests(unittest.TestCase):
    def lines(self, run: Run) -> list[str]:
        self.assertEqual(run.code, 0, run.error)
        return run.lines

    def refused(self, run: Run, message: str) -> None:
        self.assertEqual(run.code, 2)
        self.assertEqual(run.lines, [])
        self.assertIn(message, run.error)

    def test_hold_line_then_the_time_and_next_dailies(self) -> None:
        lines = self.lines(footer("--next-run", "11:53", "--build-hold-release", "11:40", at="2026-10-04T11:37"))
        self.assertEqual(lines, ["build hold - release eta: 11:40 PDT (3 minutes)", "", "11:37 PDT · next dailies 11:53 PDT"])

    def test_no_hold_is_the_time_and_next_dailies_alone(self) -> None:
        self.assertEqual(self.lines(footer("--next-run", "11:30", "--nothing-needed")), ["11:00 PDT · next dailies 11:30 PDT - nothing needed"])

    def test_no_schedule_says_so(self) -> None:
        self.assertEqual(self.lines(footer()), ["11:00 PDT · no dailies scheduled"])

    def test_next_dailies_on_a_later_day_shows_its_weekday(self) -> None:
        self.assertEqual(self.lines(footer("--next-run", "09:30+1")), ["11:00 PDT · next dailies Mon 09:30 PDT"])

    def test_none_text_release_has_no_count(self) -> None:
        lines = self.lines(footer("--next-run", "11:30", "--build-hold-release", "no ETA stated yet"))
        self.assertEqual(lines[0], "build hold - release eta: no ETA stated yet")

    def test_matches_the_report_footer(self) -> None:
        tail = after_timeline(self.lines(render(report(HOLD, "10:54"))))
        alone = self.lines(footer("--next-run", "11:30", "--build-hold-release", "10:54", "--nothing-needed"))
        self.assertEqual(tail, ["", *alone])

    def test_zone_is_required(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            self.refused(run(["--footer", "--at", AT], scratch), "--footer needs --zone")

    def test_unknown_zone_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            self.refused(run(["--footer", "--zone", "Pacific/Nowhere", "--at", AT], scratch), "--zone: 'Pacific/Nowhere' is not an IANA zone name")

    def test_release_that_is_neither_time_nor_none_text_is_refused(self) -> None:
        self.refused(footer("--build-hold-release", "soon"), "--build-hold-release: 'soon'")

    def test_next_run_that_is_not_a_time_is_refused(self) -> None:
        self.refused(footer("--next-run", "soon"), "--next-run: 'soon' is not HH:MM or HH:MM+N")

    def test_footer_takes_no_report_input(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            input_path = Path(scratch) / "dailies_input.json"
            _ = input_path.write_text(json.dumps(report(None, None)))
            self.refused(run(["--footer", "--zone", ZONE, str(input_path)], scratch), "--footer takes no input file")

    def test_footer_options_need_footer(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            self.refused(run(["--zone", ZONE], scratch), "go with --footer")


if __name__ == "__main__":
    _ = unittest.main()
