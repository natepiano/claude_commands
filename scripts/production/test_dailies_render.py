#!/usr/bin/env python3
"""dailies_render.py's build hold and footer, in a report and alone, run as the skills run it."""

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
SINCE = "10:56"
FOR = "the frame-time lane's breakdown of what each added tool costs"
HOLD_LINE = f"build hold: since 10:56 PDT, for {FOR} - release eta: "


@dataclass(frozen=True)
class Run:
    code: int
    lines: list[str]
    error: str


def unit(held: bool, needed: str | None = None) -> dict[str, object]:
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
    if held:
        fields["build_hold"] = True
    if needed is not None:
        fields["needed"] = needed
    return fields


def hold(release: str, since: str = SINCE, purpose: str = FOR) -> dict[str, object]:
    """The report input's top-level build hold."""
    return {"since": since, "for": purpose, "release": release}


def report(held: bool, build_hold: dict[str, object] | None, next_run: str | None = "11:30", needed: str | None = None) -> dict[str, object]:
    fields: dict[str, object] = {
        "length": "simple",
        "zone": ZONE,
        "units": [unit(held, needed)],
    }
    if next_run is not None:
        fields["next_run"] = next_run
    if build_hold is not None:
        fields["build_hold"] = build_hold
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


def timeline(lines: list[str]) -> list[str]:
    """The lines inside the timeline's fences."""
    opening = lines.index(FENCE)
    closing = len(lines) - 1 - lines[::-1].index(FENCE)
    return lines[opening + 1 : closing]


def hold_flags(release: str, since: str = SINCE, purpose: str = FOR) -> list[str]:
    """The footer's three build hold flags."""
    return ["--build-hold-since", since, "--build-hold-for", purpose, "--build-hold-release", release]


class ReportFooterTests(unittest.TestCase):
    def tail(self, run: Run) -> list[str]:
        self.assertEqual(run.code, 0, run.error)
        return after_timeline(run.lines)

    def refused(self, run: Run, message: str) -> None:
        self.assertEqual(run.code, 2)
        self.assertIn(message, run.error)

    def test_hold_line_opens_the_footer(self) -> None:
        tail = self.tail(render(report(True, hold("11:15"))))
        self.assertEqual(tail, ["", f"{HOLD_LINE}11:15 PDT (15 minutes)", "", "11:00 PDT · next dailies 11:30 PDT - nothing needed"])

    def test_one_minute_is_singular(self) -> None:
        tail = self.tail(render(report(True, hold("11:01"))))
        self.assertEqual(tail[1], f"{HOLD_LINE}11:01 PDT (1 minute)")

    def test_at_the_time_counts_zero(self) -> None:
        tail = self.tail(render(report(True, hold("11:00"))))
        self.assertEqual(tail[1], f"{HOLD_LINE}11:00 PDT (0 minutes)")

    def test_past_time_is_overdue(self) -> None:
        tail = self.tail(render(report(True, hold("10:54"))))
        self.assertEqual(tail[1], f"{HOLD_LINE}10:54 PDT (overdue 6 minutes)")

    def test_one_minute_overdue_is_singular(self) -> None:
        tail = self.tail(render(report(True, hold("10:59"))))
        self.assertEqual(tail[1], f"{HOLD_LINE}10:59 PDT (overdue 1 minute)")

    def test_later_day_shows_its_weekday(self) -> None:
        tail = self.tail(render(report(True, hold("09:30+1"))))
        self.assertEqual(tail[1], f"{HOLD_LINE}Mon 09:30 PDT (1350 minutes)")

    def test_none_text_prints_as_given(self) -> None:
        tail = self.tail(render(report(True, hold("none measured - requested"))))
        self.assertEqual(tail[1], f"{HOLD_LINE}none measured - requested")

    def test_no_hold_prints_no_line(self) -> None:
        tail = self.tail(render(report(False, None)))
        self.assertEqual(tail, ["", "11:00 PDT · next dailies 11:30 PDT - nothing needed"])

    def test_a_needed_subject_drops_nothing_needed(self) -> None:
        tail = self.tail(render(report(False, None, needed="the showrunner settles the shared font")))
        self.assertEqual(tail, ["", "11:00 PDT · next dailies 11:30 PDT"])

    def test_no_schedule_says_so(self) -> None:
        tail = self.tail(render(report(False, None, next_run=None)))
        self.assertEqual(tail, ["", "11:00 PDT · no dailies scheduled - nothing needed"])

    def test_held_unit_without_hold_is_refused(self) -> None:
        self.refused(render(report(True, None)), "input.build_hold: required while a unit has build_hold")

    def test_hold_without_held_unit_is_refused(self) -> None:
        self.refused(render(report(False, hold("11:15"))), "input.build_hold: only while a unit has build_hold")

    def test_release_that_is_neither_time_nor_none_text_is_refused(self) -> None:
        self.refused(render(report(True, hold("soon"))), "input.build_hold.release: 'soon'")

    def test_since_that_is_not_a_clock_time_is_refused(self) -> None:
        for since in ("soon", "9:05", "10:56+1", "24:00"):
            with self.subTest(since=since):
                self.refused(render(report(True, hold("11:15", since=since))), f"input.build_hold.since: {since!r} is not HH:MM")

    def test_each_part_is_required(self) -> None:
        for key in ("since", "for", "release"):
            with self.subTest(key=key):
                fields = hold("11:15")
                del fields[key]
                self.refused(render(report(True, fields)), f"input.build_hold.{key}: required")

    def test_unknown_hold_field_is_refused(self) -> None:
        self.refused(render(report(True, {**hold("11:15"), "until": "11:15"})), "input.build_hold: unknown field(s) until")

    def test_plumbing_in_for_is_refused(self) -> None:
        self.refused(render(report(True, hold("11:15", purpose="the writer's timings"))), "input.build_hold.for: 'writer'")

    def test_unit_hold_text_is_refused(self) -> None:
        fields = report(True, hold("11:15"))
        fields["units"] = [{**unit(False), "build_hold": "since 10:56, for the timings"}]
        self.refused(render(fields), "units[0].build_hold: true while the unit is held, else left out")

    def test_release_field_is_gone(self) -> None:
        self.refused(render({**report(True, hold("11:15")), "build_hold_release": "11:15"}), "input: unknown field(s) build_hold_release")


class HeldUnitTests(unittest.TestCase):
    def lines(self, run: Run) -> list[str]:
        self.assertEqual(run.code, 0, run.error)
        return run.lines

    def test_held_unit_has_no_section_line(self) -> None:
        lines = self.lines(render(report(True, hold("11:15"))))
        section = lines[: lines.index(FENCE)]
        self.assertEqual([line for line in section if "build hold" in line], [])

    def test_held_unit_keeps_its_timeline_tag(self) -> None:
        rows = timeline(self.lines(render(report(True, hold("11:15")))))
        widget = [row for row in rows if row.startswith("widget")]
        self.assertEqual(len(widget), 1)
        self.assertIn("12:40 build hold", widget[0])

    def test_hold_is_stated_once(self) -> None:
        lines = self.lines(render(report(True, hold("11:15"))))
        self.assertEqual([line for line in lines if "build hold:" in line], [f"{HOLD_LINE}11:15 PDT (15 minutes)"])

    def test_unheld_unit_has_no_tag(self) -> None:
        lines = self.lines(render(report(False, None)))
        self.assertEqual([line for line in lines if "build hold" in line], [])


class FooterAloneTests(unittest.TestCase):
    def lines(self, run: Run) -> list[str]:
        self.assertEqual(run.code, 0, run.error)
        return run.lines

    def refused(self, run: Run, message: str) -> None:
        self.assertEqual(run.code, 2)
        self.assertEqual(run.lines, [])
        self.assertIn(message, run.error)

    def test_hold_line_then_the_time_and_next_dailies(self) -> None:
        lines = self.lines(footer("--next-run", "11:53", *hold_flags("11:40", since="11:34"), at="2026-10-04T11:37"))
        self.assertEqual(
            lines,
            [f"build hold: since 11:34 PDT, for {FOR} - release eta: 11:40 PDT (3 minutes)", "", "11:37 PDT · next dailies 11:53 PDT"],
        )

    def test_no_hold_is_the_time_and_next_dailies_alone(self) -> None:
        self.assertEqual(self.lines(footer("--next-run", "11:30", "--nothing-needed")), ["11:00 PDT · next dailies 11:30 PDT - nothing needed"])

    def test_no_schedule_says_so(self) -> None:
        self.assertEqual(self.lines(footer()), ["11:00 PDT · no dailies scheduled"])

    def test_next_dailies_on_a_later_day_shows_its_weekday(self) -> None:
        self.assertEqual(self.lines(footer("--next-run", "09:30+1")), ["11:00 PDT · next dailies Mon 09:30 PDT"])

    def test_none_text_release_has_no_count(self) -> None:
        lines = self.lines(footer("--next-run", "11:30", *hold_flags("no ETA stated yet")))
        self.assertEqual(lines[0], f"{HOLD_LINE}no ETA stated yet")

    def test_matches_the_report_footer(self) -> None:
        tail = after_timeline(self.lines(render(report(True, hold("10:54")))))
        alone = self.lines(footer("--next-run", "11:30", *hold_flags("10:54"), "--nothing-needed"))
        self.assertEqual(tail, ["", *alone])

    def test_partial_hold_flags_are_refused(self) -> None:
        flags = hold_flags("11:40")
        for index in range(0, len(flags), 2):
            with self.subTest(dropped=flags[index]):
                partial = flags[:index] + flags[index + 2 :]
                self.refused(footer(*partial), "--build-hold-since, --build-hold-for and --build-hold-release go together")

    def test_zone_is_required(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            self.refused(run(["--footer", "--at", AT], scratch), "--footer needs --zone")

    def test_unknown_zone_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            self.refused(run(["--footer", "--zone", "Pacific/Nowhere", "--at", AT], scratch), "--zone: 'Pacific/Nowhere' is not an IANA zone name")

    def test_release_that_is_neither_time_nor_none_text_is_refused(self) -> None:
        self.refused(footer(*hold_flags("soon")), "--build-hold-release: 'soon'")

    def test_since_that_is_not_a_clock_time_is_refused(self) -> None:
        self.refused(footer(*hold_flags("11:40", since="soon")), "--build-hold-since: 'soon' is not HH:MM")

    def test_plumbing_in_for_is_refused(self) -> None:
        self.refused(footer(*hold_flags("11:40", purpose="the writer's timings")), "--build-hold-for: 'writer'")

    def test_next_run_that_is_not_a_time_is_refused(self) -> None:
        self.refused(footer("--next-run", "soon"), "--next-run: 'soon' is not HH:MM or HH:MM+N")

    def test_footer_takes_no_report_input(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            input_path = Path(scratch) / "dailies_input.json"
            _ = input_path.write_text(json.dumps(report(False, None)))
            self.refused(run(["--footer", "--zone", ZONE, str(input_path)], scratch), "--footer takes no input file")

    def test_footer_options_need_footer(self) -> None:
        for arguments in (["--zone", ZONE], hold_flags("11:40")):
            with self.subTest(arguments=arguments), tempfile.TemporaryDirectory() as scratch:
                self.refused(run(arguments, scratch), "go with --footer")


if __name__ == "__main__":
    _ = unittest.main()
