#!/usr/bin/env python3
"""dailies_render.py's footer, report, and timeline through this worktree's CLI."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, cast

SCRIPT = Path(__file__).with_name("dailies_render.py")
AT = "2026-10-04T11:00"
ZONE = "America/Los_Angeles"
FENCE = "```"
FOR = "the frame-time lane's breakdown of what each added tool costs"
AGENT_LINES = ["### Agents", "- none active", ""]


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


def report(held: bool, next_run: str | None = "11:30", needed: str | None = None) -> dict[str, object]:
    fields: dict[str, object] = {"length": "simple", "zone": ZONE, "units": [unit(held, needed)]}
    if next_run is not None:
        fields["next_run"] = next_run
    return fields


def holder(release: str = "unknown", name: str = "frame-time", since: str = "2026-10-04T10:56:00-07:00", purpose: str = FOR) -> dict[str, str]:
    return {"holder": name, "since": since, "for": purpose, "release_eta": release}


def run(arguments: list[str], scratch: str, holders: list[dict[str, str]] | None = None) -> Run:
    # HOME puts the renderer's AGENTS_DIR, READINGS_LOG and RUN_OUTS_LOG under this test directory.
    (Path(scratch) / "rust/hanadocs/agents").mkdir(parents=True, exist_ok=True)
    (Path(scratch) / ".local/state/agent-notes").mkdir(parents=True, exist_ok=True)
    hold_dir = Path(scratch) / "holds"
    hold_dir.mkdir(exist_ok=True)
    for index, record in enumerate(holders or []):
        _ = (hold_dir / f"holder-{index}").write_text(json.dumps(record) + "\n")
    result = subprocess.run(
        ["python3", str(SCRIPT), *arguments],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "HOME": scratch, "BUILD_HOLD_DIR": str(hold_dir)},
    )
    return Run(result.returncode, result.stdout.splitlines(), result.stderr)


def render(fields: dict[str, object], at: str = AT, holders: list[dict[str, str]] | None = None) -> Run:
    with tempfile.TemporaryDirectory() as scratch:
        input_path = Path(scratch) / "dailies_input.json"
        _ = input_path.write_text(json.dumps(fields))
        return run([str(input_path), "--at", at], scratch, holders)


def footer(*arguments: str, at: str = AT, holders: list[dict[str, str]] | None = None) -> Run:
    with tempfile.TemporaryDirectory() as scratch:
        return run(["--footer", "--zone", ZONE, "--at", at, *arguments], scratch, holders)


def after_timeline(lines: list[str]) -> list[str]:
    closing = len(lines) - 1 - lines[::-1].index(FENCE)
    return lines[closing + 1 :]


def timeline(lines: list[str]) -> list[str]:
    opening = lines.index(FENCE)
    closing = len(lines) - 1 - lines[::-1].index(FENCE)
    return lines[opening + 1 : closing]


class ReportFooterTests(unittest.TestCase):
    def lines(self, result: Run) -> list[str]:
        self.assertEqual(result.code, 0, result.error)
        return result.lines

    def refused(self, result: Run, message: str) -> None:
        self.assertEqual(result.code, 2)
        self.assertIn(message, result.error)

    def tail(self, result: Run) -> list[str]:
        return after_timeline(self.lines(result))

    def test_hold_line_and_unit_marker(self) -> None:
        lines = self.lines(render(report(True), holders=[holder("2026-10-04T11:15:00-07:00")]))
        self.assertIn("12:40 build hold", next(row for row in timeline(lines) if row.startswith("widget")))
        self.assertEqual(after_timeline(lines), ["", f"build hold: frame-time since 10:56 PDT, for {FOR} - release eta: 11:15 PDT (15 minutes)", "", *AGENT_LINES, "11:00 PDT · next dailies 11:30 PDT - nothing needed"])

    def test_one_minute_is_singular(self) -> None:
        tail = self.tail(render(report(True), holders=[holder("2026-10-04T11:01:00-07:00")]))
        self.assertEqual(tail[1], f"build hold: frame-time since 10:56 PDT, for {FOR} - release eta: 11:01 PDT (1 minute)")

    def test_at_the_time_counts_zero(self) -> None:
        tail = self.tail(render(report(True), holders=[holder("2026-10-04T11:00:00-07:00")]))
        self.assertEqual(tail[1], f"build hold: frame-time since 10:56 PDT, for {FOR} - release eta: 11:00 PDT (0 minutes)")

    def test_one_minute_overdue_is_singular(self) -> None:
        tail = self.tail(render(report(True), holders=[holder("2026-10-04T10:59:00-07:00")]))
        self.assertEqual(tail[1], f"build hold: frame-time since 10:56 PDT, for {FOR} - release eta: 10:59 PDT (overdue 1 minute)")

    def test_release_countdown_crosses_clock_change(self) -> None:
        fields = {**report(True), "zone": "America/New_York"}
        record = holder("2026-11-01T02:30:00-05:00", since="2026-11-01T00:15:00-04:00")
        tail = self.tail(render(fields, at="2026-11-01T00:30", holders=[record]))
        self.assertIn("release eta: 02:30 EST (180 minutes)", tail[1])

    def test_plumbing_in_holder_purpose_is_refused_in_report_and_footer(self) -> None:
        record = holder(purpose="the writer's timings")
        for result in (render(report(True), holders=[record]), footer(holders=[record])):
            with self.subTest(result=result):
                self.refused(result, "frame-time")
                self.assertIn("'writer'", result.error)
                self.assertIn("/build_hold hold again with other words", result.error)

    def test_multiple_holders_share_footer(self) -> None:
        records = [holder(), holder("2026-10-04T10:54:00-07:00", "other", "2026-10-04T10:57:00-07:00")]
        lines = self.lines(footer("--next-run", "11:30", holders=records))
        self.assertEqual(len([line for line in lines if line.startswith("build hold:")]), 2)
        self.assertIn("release eta: unknown", lines[0])
        self.assertIn("release eta: 10:54 PDT (overdue 6 minutes)", lines[1])

    def test_report_and_footer_match(self) -> None:
        records = [holder("2026-10-04T11:01:00-07:00")]
        report_tail = after_timeline(self.lines(render(report(True), holders=records)))
        footer_lines = self.lines(footer("--next-run", "11:30", "--nothing-needed", holders=records))
        self.assertEqual(report_tail, ["", *footer_lines[:-1], *AGENT_LINES, footer_lines[-1]])

    def test_no_hold_and_no_schedule(self) -> None:
        self.assertEqual(after_timeline(self.lines(render(report(False, next_run=None)))), ["", *AGENT_LINES, "11:00 PDT · no dailies scheduled - nothing needed"])
        self.assertEqual(self.lines(footer()), ["11:00 PDT · no dailies scheduled"])

    def test_a_needed_subject_drops_nothing_needed(self) -> None:
        tail = self.tail(render(report(False, needed="the showrunner settles the shared font")))
        self.assertEqual(tail, ["", *AGENT_LINES, "11:00 PDT · next dailies 11:30 PDT"])

    def test_outstanding_items_list_and_drop_nothing_needed(self) -> None:
        items = [{"since": "2026-10-03T09:05", "text": "send the Bevy PR"}, {"since": "2026-10-04T10:40", "text": "pick the demo scene"}]
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "outstanding.json"
            _ = path.write_text(json.dumps(items))
            lines = self.lines(footer("--next-run", "11:30", "--nothing-needed", "--outstanding", str(path)))
        expected = ["waiting on you:", "- send the Bevy PR (since Sat 09:05)", "- pick the demo scene (since 10:40)", "", "11:00 PDT · next dailies 11:30 PDT"]
        self.assertEqual(lines, expected)

    def test_deferred_item_hides_until_its_after_time(self) -> None:
        items = [{"since": "2026-10-04T08:36", "text": "send the Bevy PR", "after": "2026-10-04T19:00"}, {"since": "2026-10-04T10:40", "text": "pick the demo scene", "after": "2026-10-04T10:59"}]
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "outstanding.json"
            _ = path.write_text(json.dumps(items))
            lines = self.lines(footer("--next-run", "11:30", "--outstanding", str(path)))
        self.assertEqual(lines, ["waiting on you:", "- pick the demo scene (since 10:40)", "", "11:00 PDT · next dailies 11:30 PDT"])

    def test_held_unit_has_no_section_line(self) -> None:
        lines = self.lines(render(report(True), holders=[holder("2026-10-04T11:15:00-07:00")]))
        section = lines[: lines.index(FENCE)]
        self.assertEqual([line for line in section if "build hold" in line], [])

    def test_next_dailies_on_a_later_day_shows_its_weekday(self) -> None:
        self.assertEqual(self.lines(footer("--next-run", "09:30+1")), ["11:00 PDT · next dailies Mon 09:30 PDT"])

    def test_stale_marker_and_unmarked_active_hold_are_refused(self) -> None:
        self.refused(render(report(True)), "remove the stale unit marker")
        self.refused(render(report(False), holders=[holder()]), "mark the held unit")

    def test_top_level_hold_is_refused(self) -> None:
        self.refused(render({**report(False), "build_hold": {"since": "10:56"}}), "holds are read from the holder files")

    def test_unit_hold_text_is_refused(self) -> None:
        fields = report(True)
        fields["units"] = [{**unit(False), "build_hold": "since 10:56"}]
        self.refused(render(fields), "units[0].build_hold: true while the unit is held")

    def test_footer_zone_and_next_run_validation(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            self.refused(run(["--footer", "--at", AT], scratch), "--footer needs --zone")
            self.refused(run(["--footer", "--zone", "Pacific/Nowhere", "--at", AT], scratch), "not an IANA zone name")
        self.refused(footer("--next-run", "soon"), "--next-run: 'soon'")

    def test_footer_options_need_footer(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            self.refused(run(["--zone", ZONE], scratch), "go with --footer")

    def test_footer_takes_no_report_input(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            input_path = Path(scratch) / "input.json"
            _ = input_path.write_text(json.dumps(report(False)))
            self.refused(run(["--footer", "--zone", ZONE, str(input_path)], scratch), "--footer takes no input file")


def ranged_unit(label: str, phase: str, started: str, eta: dict[str, object]) -> dict[str, object]:
    return {**unit(False), "unit": f"{label}-unit", "label": label, "phase": phase, "started": started, "eta": eta}


class TimelineWindowTests(unittest.TestCase):
    def axis(self, units: list[dict[str, object]], at: str) -> str:
        run_result = render({"length": "simple", "zone": ZONE, "units": units}, at)
        self.assertEqual(run_result.code, 0, run_result.error)
        return timeline(run_result.lines)[0].strip()

    def test_narrow_chart_opens_at_the_earliest_start(self) -> None:
        self.assertTrue(self.axis([unit(False)], AT).startswith("06"))

    def test_wide_chart_drops_past_hours_so_plan_bars_fit(self) -> None:
        units = [
            ranged_unit("widget", "Phase 41 of 48: holes", "2026-10-04T08:50",
                        {"time": "16:41", "earliest": "15:06", "latest": "18:19", "percent": 65}),
            ranged_unit("trunk", "Phase 59 of 69: landing", "2026-10-03T22:36",
                        {"time": "17:51", "earliest": "15:36", "latest": "20:48", "percent": 85}),
            ranged_unit("frame", "Phase 6 of 7: dimming", "2026-10-04T11:58",
                        {"time": "21:12", "earliest": "16:35", "latest": "06:26+1", "percent": 25}),
        ]
        self.assertTrue(self.axis(units, "2026-10-04T14:59").startswith("12"))

    def test_same_hour_latest_keeps_eta_marker_in_both_chart_styles(self) -> None:
        fields = report(False)
        fields["units"] = [{**unit(False), "eta": {"time": "12:40", "earliest": "11:30", "latest": "12:55", "percent": 60}}]
        with tempfile.TemporaryDirectory() as scratch:
            input_path = Path(scratch) / "report.json"
            _ = input_path.write_text(json.dumps(fields))
            for chart, marker in (("default", "🟦"), ("ascii", "● ")):
                with self.subTest(chart=chart):
                    result = run([str(input_path), "--at", AT, "--chart", chart], scratch)
                    self.assertEqual(result.code, 0, result.error)
                    row = next(line for line in timeline(result.lines) if line.startswith("widget"))
                    self.assertIn(marker, row)


class RenumberedPhaseTests(unittest.TestCase):
    def test_a_renumbered_plan_keeps_the_phase_history(self) -> None:
        fields = report(held=False)
        renumbered = unit(held=False)
        renumbered["phase"] = "Phase 2 of 4: small text reads clearly"
        renumbered["eta"] = {"time": "12:40", "percent": 60, "why": "one more repair round"}
        fields["units"] = [renumbered]
        with tempfile.TemporaryDirectory() as scratch:
            input_path = Path(scratch) / "dailies_input.json"
            state_path = Path(scratch) / "dailies_state.json"
            _ = input_path.write_text(json.dumps(fields))
            saved = {"phase": "Phase 2 of 3: small text reads clearly", "eta": "2026-10-04T12:10:00", "held": None, "first": "2026-10-04T11:30:00"}
            _ = state_path.write_text(json.dumps({"widget-enhancements": saved}))
            result = run([str(input_path), "--at", AT, "--state", str(state_path)], scratch)
        self.assertEqual(result.code, 0, result.error)
        self.assertIn("(changed: +0:30 because one more repair round)", "\n".join(result.lines))
        self.assertIn("- first eta: 11:30 PDT (now +1:10)", result.lines)


class ChangedPhaseTitleTests(unittest.TestCase):
    OLD_PHASE: ClassVar[str] = "Phase 16 of 18: A measured working day"
    NEW_PHASE: ClassVar[str] = "Phase 16 of 19: The phone hears about the disk only when the user has something to do"
    AT: ClassVar[str] = "2026-10-05T13:30"

    def render_new_phase(self, *, first: str | None = None, held: str | None = None, old_phase: str = OLD_PHASE) -> tuple[Run, dict[str, object]]:
        fields = report(held=False, next_run=None)
        current = {**unit(False), "phase": self.NEW_PHASE, "started": "2026-10-05T12:00", "held": held}
        eta: dict[str, object] = {"time": "13:42", "percent": 60}
        if first is not None:
            eta["first"] = first
        current["eta"] = eta
        if held is not None:
            current["held_examples"] = "such as a main bar clipped in small windows"
        fields["units"] = [current]
        previous = {"phase": old_phase, "eta": "2026-10-05T16:30:00", "held": held, "first": "2026-10-05T14:30:00"}
        with tempfile.TemporaryDirectory() as scratch:
            input_path = Path(scratch) / "dailies_input.json"
            state_path = Path(scratch) / "dailies_state.json"
            _ = input_path.write_text(json.dumps(fields))
            _ = state_path.write_text(json.dumps({"widget-enhancements": previous}))
            result = run([str(input_path), "--at", self.AT, "--state", str(state_path)], scratch)
            saved = cast(dict[str, object], json.loads(state_path.read_text()))
        return result, saved

    def test_new_title_resets_prior_eta_and_first_eta(self) -> None:
        result, saved = self.render_new_phase()
        self.assertEqual(result.code, 0, result.error)
        self.assertIn("- eta: 13:42 PDT, 60% done", result.lines)
        self.assertFalse(any("(changed:" in line or "(unchanged" in line for line in result.lines))
        self.assertFalse(any(line.startswith("- first eta:") for line in result.lines))
        self.assertEqual(saved, {"widget-enhancements": {
            "phase": self.NEW_PHASE, "eta": "2026-10-05T13:42:00", "held": None, "first": "2026-10-05T13:42:00",
        }})
        self.assertEqual(after_timeline(result.lines), ["", *AGENT_LINES, "13:30 PDT · no dailies scheduled - nothing needed"])

    def test_new_title_uses_explicit_first_eta(self) -> None:
        result, saved = self.render_new_phase(first="2026-10-05T13:00")
        self.assertEqual(result.code, 0, result.error)
        self.assertIn("- eta: 13:42 PDT, 60% done", result.lines)
        self.assertIn("- first eta: 13:00 PDT (now +0:42)", result.lines)
        self.assertEqual(saved, {"widget-enhancements": {
            "phase": self.NEW_PHASE, "eta": "2026-10-05T13:42:00", "held": None, "first": "2026-10-05T13:00:00",
        }})
        self.assertEqual(after_timeline(result.lines), ["", *AGENT_LINES, "13:30 PDT · no dailies scheduled - nothing needed"])

    def test_saved_phase_without_title_counts_as_new(self) -> None:
        result, saved = self.render_new_phase(old_phase="Phase 16 of 18")
        self.assertEqual(result.code, 0, result.error)
        self.assertFalse(any("(changed:" in line or "(unchanged" in line for line in result.lines))
        self.assertEqual(saved["widget-enhancements"], {
            "phase": self.NEW_PHASE, "eta": "2026-10-05T13:42:00", "held": None, "first": "2026-10-05T13:42:00",
        })

    def test_new_title_repeats_held_examples_in_simple_report(self) -> None:
        reason = "the design check found defects"
        result, saved = self.render_new_phase(held=reason)
        self.assertEqual(result.code, 0, result.error)
        self.assertIn(f"- checkpoint: not merged, because {reason}, such as a main bar clipped in small windows", result.lines)
        self.assertEqual(saved, {"widget-enhancements": {
            "phase": self.NEW_PHASE, "eta": "2026-10-05T13:42:00", "held": reason, "first": "2026-10-05T13:42:00",
        }})
        self.assertEqual(after_timeline(result.lines), ["", *AGENT_LINES, "13:30 PDT · no dailies scheduled - nothing needed"])


if __name__ == "__main__":
    _ = unittest.main()
