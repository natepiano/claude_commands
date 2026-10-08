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
from typing import ClassVar, cast, override
from unittest import mock

from dailies_render import (NoLastReportedEta, StateClear, StateRefused,
                            check_render_state, load_state)

SCRIPT = Path(__file__).with_name("dailies_render.py")
AT = "2026-10-04T11:00"
ZONE = "America/Los_Angeles"
FENCE = "```"
FOR = "the frame-time lane's breakdown of what each added tool costs"
FOOTER_HEAD = ["", "---", "11:00 PDT update:", ""]
AGENT_LINES = ["* none active"]


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


def idle_unit(name: str, label: str, until: str, waits_for: str) -> dict[str, object]:
    return {
        **unit(False),
        "unit": name,
        "name": name,
        "label": label,
        "idle": {"waits_for": waits_for, "until": until},
    }


def idle_units() -> list[dict[str, object]]:
    return [
        idle_unit("screenshot", "screen", "2026-10-14T07:50", "a week of capture timings"),
        idle_unit("cache-evict", "cache", "2026-10-04T16:33", "a day of sweep readings"),
        idle_unit("mul_add", "mul_add", "2026-10-10T22:44", "three nightly lint runs"),
    ]


def holder(release: str = "unknown", name: str = "frame-time", since: str = "2026-10-04T10:56:00-07:00", purpose: str = FOR) -> dict[str, str]:
    return {"holder": name, "since": since, "for": purpose, "release_eta": release}


def run(arguments: list[str], scratch: str, holders: list[dict[str, str]] | None = None) -> Run:
    # HOME puts the renderer's AGENTS_DIR and READINGS_LOG under this test directory.
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
        env={**os.environ, "HOME": scratch, "BUILD_HOLD_DIR": str(hold_dir),
             "MAC_TEST_STATE_DIR": str(Path(scratch) / "mac-test")},
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
        self.assertEqual(after_timeline(lines), ["", *FOOTER_HEAD, f"* build hold: frame-time since 10:56 PDT, for {FOR} - release eta: 11:15 PDT (15 minutes)", *AGENT_LINES, "* next dailies: 11:30 PDT - nothing needed"])

    def test_one_minute_is_singular(self) -> None:
        tail = self.tail(render(report(True), holders=[holder("2026-10-04T11:01:00-07:00")]))
        self.assertIn(f"* build hold: frame-time since 10:56 PDT, for {FOR} - release eta: 11:01 PDT (1 minute)", tail)

    def test_at_the_time_counts_zero(self) -> None:
        tail = self.tail(render(report(True), holders=[holder("2026-10-04T11:00:00-07:00")]))
        self.assertIn(f"* build hold: frame-time since 10:56 PDT, for {FOR} - release eta: 11:00 PDT (0 minutes)", tail)

    def test_one_minute_overdue_is_singular(self) -> None:
        tail = self.tail(render(report(True), holders=[holder("2026-10-04T10:59:00-07:00")]))
        self.assertIn(f"* build hold: frame-time since 10:56 PDT, for {FOR} - release eta: 10:59 PDT (overdue 1 minute)", tail)

    def test_release_countdown_crosses_clock_change(self) -> None:
        fields = {**report(True), "zone": "America/New_York"}
        record = holder("2026-11-01T02:30:00-05:00", since="2026-11-01T00:15:00-04:00")
        tail = self.tail(render(fields, at="2026-11-01T00:30", holders=[record]))
        self.assertTrue(any("release eta: 02:30 EST (180 minutes)" in line for line in tail))

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
        holds = [line for line in lines if line.startswith("* build hold:")]
        self.assertEqual(len(holds), 2)
        self.assertIn("release eta: unknown", holds[0])
        self.assertIn("release eta: 10:54 PDT (overdue 6 minutes)", holds[1])

    def test_report_and_footer_match(self) -> None:
        records = [holder("2026-10-04T11:01:00-07:00")]
        report_tail = after_timeline(self.lines(render(report(True), holders=records)))
        footer_lines = self.lines(footer("--next-run", "11:30", "--nothing-needed", holders=records))
        self.assertEqual(report_tail, ["", *footer_lines])

    def test_no_hold_and_no_schedule(self) -> None:
        self.assertEqual(after_timeline(self.lines(render(report(False, next_run=None)))), ["", *FOOTER_HEAD, *AGENT_LINES, "* no dailies scheduled - nothing needed"])
        self.assertEqual(self.lines(footer()), [*FOOTER_HEAD, *AGENT_LINES, "* no dailies scheduled"])

    def test_a_needed_subject_drops_nothing_needed(self) -> None:
        tail = self.tail(render(report(False, needed="the showrunner settles the shared font")))
        self.assertEqual(tail, ["", *FOOTER_HEAD, *AGENT_LINES, "* next dailies: 11:30 PDT"])

    def test_outstanding_items_drop_nothing_needed_without_entering_footer(self) -> None:
        items = [{"since": "2026-10-03T09:05", "text": "send the Bevy PR"}, {"since": "2026-10-04T10:40", "text": "pick the demo scene"}]
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "outstanding.json"
            _ = path.write_text(json.dumps(items))
            lines = self.lines(footer("--next-run", "11:30", "--nothing-needed", "--outstanding", str(path)))
        expected = [*FOOTER_HEAD, *AGENT_LINES, "* next dailies: 11:30 PDT"]
        self.assertEqual(lines, expected)

    def test_deferred_item_suppresses_nothing_needed_only_after_its_time(self) -> None:
        items = [{"since": "2026-10-04T08:36", "text": "send the Bevy PR", "after": "2026-10-04T19:00"}, {"since": "2026-10-04T10:40", "text": "pick the demo scene", "after": "2026-10-04T10:59"}]
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "outstanding.json"
            _ = path.write_text(json.dumps(items))
            before = self.lines(footer("--next-run", "11:30", "--nothing-needed", "--outstanding", str(path), at="2026-10-04T10:30"))
            after = self.lines(footer("--next-run", "11:30", "--nothing-needed", "--outstanding", str(path)))
        self.assertEqual(before[-1], "* next dailies: 11:30 PDT - nothing needed")
        self.assertEqual(after, [*FOOTER_HEAD, *AGENT_LINES, "* next dailies: 11:30 PDT"])

    def test_held_unit_has_no_section_line(self) -> None:
        lines = self.lines(render(report(True), holders=[holder("2026-10-04T11:15:00-07:00")]))
        section = lines[: lines.index(FENCE)]
        self.assertEqual([line for line in section if "build hold" in line], [])

    def test_next_dailies_on_a_later_day_shows_its_weekday(self) -> None:
        self.assertEqual(self.lines(footer("--next-run", "09:30+1")), [*FOOTER_HEAD, *AGENT_LINES, "* next dailies: Mon 09:30 PDT"])

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


class IdleGroupTests(unittest.TestCase):
    def lines(self, result: Run) -> list[str]:
        self.assertEqual(result.code, 0, result.error)
        return result.lines

    def refused(self, result: Run, message: str) -> None:
        self.assertEqual(result.code, 2)
        self.assertIn(message, result.error)

    def fields(self, length: str = "simple") -> dict[str, object]:
        working = {**unit(False), "unit": "working-unit", "name": "working", "label": "working"}
        return {
            "length": length,
            "zone": ZONE,
            "units": [*idle_units(), working],
            "topics": [{
                "title": "Other topic",
                "update": "the queued check is running",
                "eta": "12:55 PDT",
                "needs_user": False,
            }],
        }

    def test_simple_groups_idle_units_by_return_time_before_other_topics(self) -> None:
        lines = self.lines(render(self.fields()))
        expected = [
            "### Waiting and idle",
            "- cache-evict until 16:33: a day of sweep readings",
            "- mul_add until Sat 22:44: three nightly lint runs",
            "- screenshot until Wed 2026-10-14 07:50: a week of capture timings",
            "",
        ]
        group_start = lines.index("### Waiting and idle")
        self.assertEqual(lines[group_start : group_start + len(expected)], expected)
        self.assertLess(lines.index("### working: panel widgets that work by keyboard and draw cleanly"), group_start)
        self.assertLess(group_start, lines.index("### Other topic"))
        for label in ("cache", "mul_add", "screen"):
            self.assertTrue(any(row.startswith(label) for row in timeline(lines)), label)

    def test_page_and_elaborate_keep_every_full_section(self) -> None:
        for length in ("page", "elaborate"):
            with self.subTest(length=length):
                lines = self.lines(render(self.fields(length)))
                headings = [line for line in lines[: lines.index(FENCE)] if line.startswith("### ")]
                self.assertEqual(
                    headings,
                    [
                        "### screenshot: panel widgets that work by keyboard and draw cleanly",
                        "### cache-evict: panel widgets that work by keyboard and draw cleanly",
                        "### mul_add: panel widgets that work by keyboard and draw cleanly",
                        "### working: panel widgets that work by keyboard and draw cleanly",
                        "### Other topic",
                    ],
                )
                self.assertNotIn("### Waiting and idle", lines)

    def test_action_fields_keep_idle_units_in_full_sections(self) -> None:
        needed = {**idle_units()[0], "needed": "the showrunner: start the capture"}
        held = {**idle_units()[1], "held": "the review found a missing case"}
        needs_user = {**idle_units()[2], "needs_user": True}
        fields: dict[str, object] = {
            "length": "simple",
            "zone": ZONE,
            "units": [needed, held, needs_user],
        }
        lines = self.lines(render(fields))
        self.assertNotIn("### Waiting and idle", lines)
        for name in ("screenshot", "cache-evict", "mul_add"):
            self.assertTrue(any(line.startswith(f"### {name}:") for line in lines), name)

    def test_all_idle_units_print_only_the_group(self) -> None:
        fields: dict[str, object] = {"length": "simple", "zone": ZONE, "units": idle_units()}
        lines = self.lines(render(fields))
        headings = [line for line in lines[: lines.index(FENCE)] if line.startswith("### ")]
        self.assertEqual(headings, ["### Waiting and idle"])

    def test_idle_input_refusals(self) -> None:
        base = idle_units()[0]
        cases: list[tuple[dict[str, object], str]] = [
            (
                {**base, "idle": {"waits_for": "capture timings", "until": "2026-10-14T07:50", "extra": True}},
                "units[0].idle: unknown field(s) extra",
            ),
            (
                {**base, "idle": {"waits_for": "capture timings", "until": "next week"}},
                "units[0].idle.until: 'next week' must be when the unit comes back",
            ),
            (
                {**base, "idle": {"waits_for": "x" * 81, "until": "2026-10-14T07:50"}},
                "units[0].idle.waits_for: 81 characters; at most 80, one short line",
            ),
            (
                {**base, "idle": {"waits_for": "the writer's report", "until": "2026-10-14T07:50"}},
                "units[0].idle.waits_for: 'writer' is the production's own plumbing",
            ),
        ]
        for changed, message in cases:
            with self.subTest(message=message):
                fields: dict[str, object] = {"length": "simple", "zone": ZONE, "units": [changed]}
                self.refused(render(fields), message)

    def test_renderer_refuses_idle_return_at_or_before_now(self) -> None:
        for until in ("2026-10-04T10:59", AT):
            with self.subTest(until=until):
                waiting = idle_unit("cache-evict", "cache", until, "a day of sweep readings")
                fields: dict[str, object] = {"length": "simple", "zone": ZONE, "units": [waiting]}
                self.refused(
                    render(fields),
                    "units[0].idle.until: that time has passed; remove idle now the unit is back at work, or give the new time",
                )

    def test_state_check_names_idle_return_at_now(self) -> None:
        waiting = idle_unit("cache-evict", "cache", AT, "a day of sweep readings")
        fields: dict[str, object] = {"length": "simple", "zone": ZONE, "units": [waiting]}
        with tempfile.TemporaryDirectory() as scratch:
            state = check_render_state(fields, Path(scratch) / "missing-state.json", AT)
        self.assertIsInstance(state, StateRefused)
        if isinstance(state, StateRefused):
            self.assertEqual(state.field, "units[0].idle.until")
            self.assertIn("that time has passed", state.why)

    def test_idle_units_remain_in_state_and_eta_log(self) -> None:
        fields: dict[str, object] = {"length": "simple", "zone": ZONE, "units": idle_units()}
        with tempfile.TemporaryDirectory() as scratch:
            input_path = Path(scratch) / "input.json"
            state_path = Path(scratch) / "state.json"
            log_path = Path(scratch) / "production.log"
            _ = input_path.write_text(json.dumps(fields), encoding="utf-8")
            result = run(
                [str(input_path), "--at", AT, "--state", str(state_path), "--log", str(log_path)],
                scratch,
            )
            saved = cast(dict[str, object], json.loads(state_path.read_text(encoding="utf-8")))
            logged = log_path.read_text(encoding="utf-8")
        self.assertEqual(result.code, 0, result.error)
        self.assertEqual(set(saved), {"screenshot", "cache-evict", "mul_add"})
        for name in saved:
            self.assertIn(f"{name} Phase 2 of 3", logged)


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


class UpcomingWorkTests(unittest.TestCase):
    def render_then(self, length: str, then: object, phase: str = "Phase 2 of 3: small text reads clearly") -> Run:
        current = {**unit(False), "phase": phase, "then": then}
        return render({"length": length, "zone": ZONE, "units": [current]})

    def test_three_items_show_only_the_next_item_in_simple(self) -> None:
        items = ["Phase 3: labels stay legible", "Phase 4: panels match", "Phase 5: saved scenes reopen"]
        result = self.render_then("simple", items)
        self.assertEqual(result.code, 0, result.error)
        self.assertEqual([line for line in result.lines if "then:" in line or line.startswith("  - ")], [f"- then: {items[0]}"])
        self.assertNotIn(items[1], "\n".join(result.lines))
        self.assertNotIn(items[2], "\n".join(result.lines))

    def test_three_items_are_sub_bullets_in_page_and_elaborate(self) -> None:
        items = ["Phase 3: labels stay legible", "Phase 4: panels match", "Phase 5: saved scenes reopen"]
        for length in ("page", "elaborate"):
            with self.subTest(length=length):
                result = self.render_then(length, items)
                self.assertEqual(result.code, 0, result.error)
                first = result.lines.index("- then:")
                self.assertEqual(result.lines[first:first + 4], ["- then:", *(f"  - {item}" for item in items)])
                self.assertEqual(sum(line.startswith("- then:") for line in result.lines), 1)

    def test_one_item_list_stays_inline_at_every_length(self) -> None:
        item = "Phase 3: labels stay legible"
        for length in ("simple", "page", "elaborate"):
            with self.subTest(length=length):
                result = self.render_then(length, [item])
                self.assertEqual(result.code, 0, result.error)
                self.assertIn(f"- then: {item}", result.lines)
                self.assertNotIn("- then:", result.lines)
                self.assertNotIn(f"  - {item}", result.lines)

    def test_plain_string_is_refused_with_list_form_at_every_length(self) -> None:
        item = "Phase 3: labels stay legible"
        message = 'units[0].then: must be a list of one-line items, one per upcoming phase: ["Phase 3: …", "Phase 4: …"]'
        for length in ("simple", "page", "elaborate"):
            with self.subTest(length=length):
                result = self.render_then(length, item)
                self.assertEqual(result.code, 2)
                self.assertIn(message, result.error)

    def test_chained_item_is_refused_and_named_at_every_length(self) -> None:
        items = (
            "Phase 3: labels stay legible, then match panels",
            "Phase 3: labels stay legible; then match panels",
            "Phase 3: labels stay legible THEN Phase 4: panels match",
        )
        for length in ("simple", "page", "elaborate"):
            for item in items:
                with self.subTest(length=length, item=item):
                    result = self.render_then(length, [item])
                    self.assertEqual(result.code, 2)
                    self.assertIn("units[0].then[0]: one item names more than one phase; split it into list items", result.error)
                    self.assertIn(item, result.error)

    def test_item_with_two_phase_heads_is_refused_without_a_then(self) -> None:
        items = (
            "Phase 3: labels stay legible; Phase 4: panels align",
            "Phase 3: labels stay legible and Phase 4: panels match",
        )
        for length in ("simple", "page", "elaborate"):
            for item in items:
                with self.subTest(length=length, item=item):
                    result = self.render_then(length, [item])
                    self.assertEqual(result.code, 2)
                    self.assertIn("units[0].then[0]: one item names more than one phase; split it into list items", result.error)
                    self.assertIn(item, result.error)

    def test_item_that_mentions_another_phase_without_a_head_is_allowed(self) -> None:
        item = "Phase 4: panels match once Phase 3 merges"
        result = self.render_then("simple", [item])
        self.assertEqual(result.code, 0, result.error)
        self.assertIn(f"- then: {item}", result.lines)

    def test_chained_later_item_is_refused_with_its_index(self) -> None:
        item = "Phase 4: panels match, then reopen scenes"
        result = self.render_then("page", ["Phase 3: labels stay legible", item])
        self.assertEqual(result.code, 2)
        self.assertIn("units[0].then[1]: one item names more than one phase; split it into list items", result.error)
        self.assertIn(item, result.error)

    def test_shared_purpose_phase_range_stays_one_item(self) -> None:
        item = "76–79: make keyboard labels clear"
        result = self.render_then("simple", [item], "Phase 64 of 80: front output jacks start a cable")
        self.assertEqual(result.code, 0, result.error)
        self.assertIn(f"- then: {item}", result.lines)

    def test_then_without_another_phase_is_allowed_in_an_item(self) -> None:
        item = "Phase 3: show then in the label"
        result = self.render_then("simple", [item])
        self.assertEqual(result.code, 0, result.error)
        self.assertIn(f"- then: {item}", result.lines)

    def test_empty_list_and_empty_item_are_refused_as_then(self) -> None:
        for then in ([], ["Phase 3: labels stay legible", ""], ["  "]):
            with self.subTest(then=then):
                result = self.render_then("simple", then)
                self.assertEqual(result.code, 2)
                self.assertIn("units[0].then", result.error)

    def test_non_string_item_is_refused_as_then(self) -> None:
        result = self.render_then("simple", ["Phase 3: labels stay legible", 4])
        self.assertEqual(result.code, 2)
        self.assertIn("units[0].then", result.error)

    def test_each_item_checks_phase_order(self) -> None:
        result = self.render_then("page", ["Phase 3: labels stay legible", "Phase 1: panels match"])
        self.assertEqual(result.code, 2)
        self.assertIn("units[0].then", result.error)
        self.assertIn("Phase 1", result.error)

    def test_numeric_phase_labels_in_later_items_check_order(self) -> None:
        phase = "Phase 64 of 80: front output jacks start a cable"
        for label, earlier in (("63: jack panels match", "Phase 63"), ("63–65: jack panels match", "Phase 63"), ("63: update README.md", "Phase 63")):
            with self.subTest(label=label):
                result = self.render_then("page", ["65: connect a cable", label], phase)
                self.assertEqual(result.code, 2)
                self.assertIn("units[0].then", result.error)
                self.assertIn(earlier, result.error)

    def test_each_item_checks_user_facing_words(self) -> None:
        result = self.render_then("page", ["Phase 3: labels stay legible", "Release file holds"])
        self.assertEqual(result.code, 2)
        self.assertIn("units[0].then", result.error)
        self.assertIn("file holds", result.error)

    def test_last_phase_still_requires_then(self) -> None:
        current = {**unit(False), "phase": "Phase 3 of 3: panels match"}
        result = render({"length": "simple", "zone": ZONE, "units": [current]})
        self.assertEqual(result.code, 2)
        self.assertIn("units[0].then: required on a last phase", result.error)

    def test_follow_up_return_can_be_in_any_item(self) -> None:
        phase = "follow-up 1 of 2: smooth the panel edges"
        result = self.render_then("page", ["Finish panel edges", "return to the plan at Phase 3"], phase)
        self.assertEqual(result.code, 0, result.error)

    def test_follow_up_without_return_is_refused(self) -> None:
        phase = "follow-up 1 of 2: smooth the panel edges"
        result = self.render_then("page", ["Finish panel edges", "Check saved scenes"], phase)
        self.assertEqual(result.code, 2)
        self.assertIn("units[0].then: a follow-up names the plan phase", result.error)


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
            saved = {"phase": "Phase 2 of 3: small text reads clearly", "eta": "2026-10-04T12:10:00",
                     "eta_text": "12:10", "held": None, "first": "2026-10-04T11:30:00"}
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
        previous = {"phase": old_phase, "eta": "2026-10-05T16:30:00", "eta_text": "16:30",
                    "held": held, "first": "2026-10-05T14:30:00"}
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
            "phase": self.NEW_PHASE, "eta": "2026-10-05T13:42:00", "eta_text": "13:42",
            "held": None, "first": "2026-10-05T13:42:00",
        }})
        self.assertEqual(after_timeline(result.lines), ["", "", "---", "13:30 PDT update:", "", *AGENT_LINES, "* no dailies scheduled - nothing needed"])

    def test_new_title_uses_explicit_first_eta(self) -> None:
        result, saved = self.render_new_phase(first="2026-10-05T13:00")
        self.assertEqual(result.code, 0, result.error)
        self.assertIn("- eta: 13:42 PDT, 60% done", result.lines)
        self.assertIn("- first eta: 13:00 PDT (now +0:42)", result.lines)
        self.assertEqual(saved, {"widget-enhancements": {
            "phase": self.NEW_PHASE, "eta": "2026-10-05T13:42:00", "eta_text": "13:42",
            "held": None, "first": "2026-10-05T13:00:00",
        }})
        self.assertEqual(after_timeline(result.lines), ["", "", "---", "13:30 PDT update:", "", *AGENT_LINES, "* no dailies scheduled - nothing needed"])

    def test_saved_phase_without_title_counts_as_new(self) -> None:
        result, saved = self.render_new_phase(old_phase="Phase 16 of 18")
        self.assertEqual(result.code, 0, result.error)
        self.assertFalse(any("(changed:" in line or "(unchanged" in line for line in result.lines))
        self.assertEqual(saved["widget-enhancements"], {
            "phase": self.NEW_PHASE, "eta": "2026-10-05T13:42:00", "eta_text": "13:42",
            "held": None, "first": "2026-10-05T13:42:00",
        })

    def test_new_title_repeats_held_examples_in_simple_report(self) -> None:
        reason = "the design check found defects"
        result, saved = self.render_new_phase(held=reason)
        self.assertEqual(result.code, 0, result.error)
        self.assertIn(f"- checkpoint: not merged, because {reason}, such as a main bar clipped in small windows", result.lines)
        self.assertEqual(saved, {"widget-enhancements": {
            "phase": self.NEW_PHASE, "eta": "2026-10-05T13:42:00", "eta_text": "13:42",
            "held": reason, "first": "2026-10-05T13:42:00",
        }})
        self.assertEqual(after_timeline(result.lines), ["", "", "---", "13:30 PDT update:", "", *AGENT_LINES, "* no dailies scheduled - nothing needed"])


class EtaResolutionTests(unittest.TestCase):
    def run_with_state(
        self, fields: dict[str, object], previous: dict[str, object], at: str,
    ) -> tuple[Run, dict[str, object], str]:
        with tempfile.TemporaryDirectory() as scratch:
            input_path = Path(scratch) / "dailies_input.json"
            state_path = Path(scratch) / "dailies_state.json"
            log_path = Path(scratch) / "production.log"
            _ = input_path.write_text(json.dumps(fields), encoding="utf-8")
            _ = state_path.write_text(json.dumps({"widget-enhancements": previous}), encoding="utf-8")
            result = run(
                [str(input_path), "--at", at, "--state", str(state_path), "--log", str(log_path)],
                scratch,
            )
            saved = cast(dict[str, object], json.loads(state_path.read_text(encoding="utf-8")))
            logged = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
        return result, saved, logged

    def test_unchanged_eta_uses_saved_moment_in_report_chart_state_and_log(self) -> None:
        fields = report(held=False)
        current = {**unit(False), "eta": {"time": "19:35", "percent": 60}}
        fields["units"] = [current]
        previous = {
            "phase": current["phase"],
            "eta": "2026-10-04T19:35:00",
            "eta_text": "19:35",
            "held": None,
            "first": "2026-10-04T19:35:00",
        }
        result, saved, logged = self.run_with_state(fields, previous, "2026-10-04T23:50")
        self.assertEqual(result.code, 0, result.error)
        self.assertIn("- eta: 19:35 PDT, 60% done (unchanged, overdue)", result.lines)
        self.assertIn("19:35", next(line for line in timeline(result.lines) if line.startswith("widget")))
        self.assertEqual(cast(dict[str, object], saved["widget-enhancements"])["eta"],
                         "2026-10-04T19:35:00")
        self.assertEqual(cast(dict[str, object], saved["widget-enhancements"])["eta_text"], "19:35")
        self.assertIn("widget-enhancements Phase 2 of 3 19:35 PDT, 60% done", logged)
        self.assertNotIn("tomorrow", logged)

    def test_state_without_eta_text_resolves_as_a_first_report(self) -> None:
        fields = report(held=False)
        current = {**unit(False), "eta": {"time": "19:35", "percent": 60}}
        fields["units"] = [current]
        previous = {
            "phase": current["phase"],
            "eta": "2026-10-04T19:35:00",
            "held": None,
            "first": "2026-10-04T19:35:00",
        }
        result, saved, logged = self.run_with_state(fields, previous, "2026-10-04T23:50")
        self.assertEqual(result.code, 0, result.error)
        self.assertIn("- eta: 19:35 PDT tomorrow, 60% done", result.lines)
        self.assertEqual(cast(dict[str, object], saved["widget-enhancements"])["eta"],
                         "2026-10-05T19:35:00")
        self.assertEqual(cast(dict[str, object], saved["widget-enhancements"])["eta_text"], "19:35")
        self.assertIn("19:35 PDT tomorrow", logged)
        with tempfile.TemporaryDirectory() as scratch:
            state_path = Path(scratch) / "old-state.json"
            _ = state_path.write_text(json.dumps({"widget-enhancements": previous}), encoding="utf-8")
            old = load_state(state_path)["widget-enhancements"]
        self.assertIsInstance(old.eta, NoLastReportedEta)

    def test_changed_eta_text_uses_two_hour_rule(self) -> None:
        fields = report(held=False)
        current = {**unit(False), "eta": {
            "time": "19:36",
            "percent": 60,
            "why": "the panel review found another repair",
        }}
        fields["units"] = [current]
        previous = {
            "phase": current["phase"],
            "eta": "2026-10-04T19:35:00",
            "eta_text": "19:35",
            "held": None,
            "first": "2026-10-04T19:35:00",
        }
        result, saved, logged = self.run_with_state(fields, previous, "2026-10-04T23:50")
        self.assertEqual(result.code, 0, result.error)
        self.assertIn("19:36 PDT tomorrow", "\n".join(result.lines))
        self.assertIn("changed: +24:01 because the panel review found another repair",
                      "\n".join(result.lines))
        self.assertEqual(cast(dict[str, object], saved["widget-enhancements"])["eta"],
                         "2026-10-05T19:36:00")
        self.assertIn("19:36 PDT tomorrow", logged)

    def test_explicit_day_suffix_is_different_eta_text(self) -> None:
        fields = report(held=False)
        current = {**unit(False), "eta": {
            "time": "19:35+0",
            "percent": 60,
            "why": "the phase now names today's occurrence",
        }}
        fields["units"] = [current]
        previous = {
            "phase": current["phase"],
            "eta": "2026-10-04T19:35:00",
            "eta_text": "19:35",
            "held": None,
            "first": "2026-10-04T19:35:00",
        }
        result, saved, _ = self.run_with_state(fields, previous, "2026-10-05T23:50")
        self.assertEqual(result.code, 0, result.error)
        self.assertIn("changed: +24:00 because the phase now names today's occurrence",
                      "\n".join(result.lines))
        state = cast(dict[str, object], saved["widget-enhancements"])
        self.assertEqual(state["eta"], "2026-10-05T19:35:00")
        self.assertEqual(state["eta_text"], "19:35+0")


class StatePreflightTests(unittest.TestCase):
    @override
    def setUp(self) -> None:
        # The preflight reads holder files; a hold live on this machine must not reach these tests.
        holders = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(mock.patch.dict(
            os.environ,
            {"BUILD_HOLD_DIR": holders, "MAC_TEST_STATE_DIR": str(Path(holders) / "mac-test")},
        ))

    def test_moved_eta_without_reason_returns_named_refusal(self) -> None:
        fields = report(held=False)
        with tempfile.TemporaryDirectory() as scratch:
            state_path = Path(scratch) / "state.json"
            previous = {"phase": "Phase 2 of 3: small text reads clearly",
                        "eta": "2026-10-04T11:20:00", "eta_text": "11:20",
                        "held": None, "first": "2026-10-04T11:20:00"}
            _ = state_path.write_text(json.dumps({"widget-enhancements": previous}), encoding="utf-8")
            result = check_render_state(fields, state_path, AT)
        self.assertIsInstance(result, StateRefused)
        if isinstance(result, StateRefused):
            self.assertEqual(result.field, "units[0].eta.why")
            self.assertIn("ETA moved", result.why)

    def test_unit_without_eta_returns_named_state_even_with_previous_eta(self) -> None:
        fields = report(held=False)
        current = unit(False)
        current["eta"] = {"none": "no ETA stated yet"}
        fields["units"] = [current]
        with tempfile.TemporaryDirectory() as scratch:
            state_path = Path(scratch) / "state.json"
            previous = {"phase": "Phase 2 of 3: small text reads clearly",
                        "eta": "2026-10-04T11:20:00", "eta_text": "11:20",
                        "held": None, "first": "2026-10-04T11:20:00"}
            _ = state_path.write_text(json.dumps({"widget-enhancements": previous}), encoding="utf-8")
            result = check_render_state(fields, state_path, AT)
        self.assertIsInstance(result, StateClear)

    def test_report_without_next_run_returns_named_state(self) -> None:
        fields = report(held=False, next_run=None)
        with tempfile.TemporaryDirectory() as scratch:
            result = check_render_state(fields, Path(scratch) / "missing-state.json", AT)
        self.assertIsInstance(result, StateClear)


class GanttTests(unittest.TestCase):
    """The default length: the chart, with only what needs the user and what changed above it."""

    def gantt(self, units: list[dict[str, object]], state: dict[str, dict[str, object]] | None = None) -> Run:
        fields: dict[str, object] = {"length": "gantt", "zone": ZONE, "units": units, "next_run": "11:30"}
        with tempfile.TemporaryDirectory() as scratch:
            input_path = Path(scratch) / "dailies_input.json"
            state_path = Path(scratch) / "dailies_state.json"
            _ = input_path.write_text(json.dumps(fields), encoding="utf-8")
            _ = state_path.write_text(json.dumps(state or {}), encoding="utf-8")
            return run([str(input_path), "--at", AT, "--state", str(state_path)], scratch)

    def above_chart(self, result: Run) -> list[str]:
        self.assertEqual(result.code, 0, result.error)
        self.assertEqual(result.lines[0], "**Dailies (Gantt)**, 11:00 PDT")
        return [line for line in result.lines[1:result.lines.index(FENCE)] if line]

    def last_report(self, **changed: object) -> dict[str, object]:
        """What the last report saved for the one unit, with `changed` fields put in."""
        return {"phase": unit(False)["phase"], "eta": "2026-10-04T12:40:00", "eta_text": "12:40", "held": None,
                "first": "2026-10-04T12:40:00", **changed}

    def test_a_gantt_report_is_the_chart_and_the_last_lines_of_a_simple_one(self) -> None:
        result = self.gantt([unit(False)])
        self.assertEqual(self.above_chart(result), [])
        simple = render(report(held=False))
        self.assertEqual(simple.code, 0, simple.error)
        self.assertEqual(timeline(result.lines), timeline(simple.lines))
        self.assertEqual(after_timeline(result.lines), after_timeline(simple.lines))

    def test_a_subject_that_needs_the_user_keeps_its_section_above_the_chart(self) -> None:
        asking = {**unit(False, needed="choose the wording of the label"), "needs_user": True}
        above = self.above_chart(self.gantt([asking]))
        self.assertEqual(above[0], f"### widget-enhancements: {asking['project']}")
        self.assertIn("- needed: choose the wording of the label", above)

    def test_a_unit_with_nothing_changed_since_its_last_report_has_no_line(self) -> None:
        self.assertEqual(self.above_chart(self.gantt([unit(False)], {"widget-enhancements": self.last_report()})), [])

    def test_each_change_since_the_last_report_is_one_line_above_the_chart(self) -> None:
        moved = {**unit(False), "eta": {"time": "12:40", "percent": 60, "why": "the fix needed a second pass"}}
        name = "widget-enhancements"
        last = self.last_report(eta="2026-10-04T12:13:00", eta_text="12:13", held="the panel check needs repair")
        self.assertEqual(self.above_chart(self.gantt([moved], {"widget-enhancements": last})), [
            f"- {name}: checkpoint no longer held",
            f"- {name}: eta 12:40 PDT, 60% done (changed: +0:27 because the fix needed a second pass)",
        ])
        held = {**unit(False), "held": "the panel check needs repair"}
        earlier_phase = self.last_report(phase="Phase 1 of 3: labels are named")
        self.assertEqual(self.above_chart(self.gantt([held], {"widget-enhancements": earlier_phase})), [
            f"- {name}: now on {held['phase']}",
            f"- {name}: checkpoint not merged, because the panel check needs repair",
        ])

    def test_an_eta_that_moved_less_than_a_quarter_hour_has_no_line(self) -> None:
        moved = {**unit(False), "eta": {"time": "12:40", "percent": 60, "why": "one more test run"}}
        last = self.last_report(eta="2026-10-04T12:30:00", eta_text="12:30")
        self.assertEqual(self.above_chart(self.gantt([moved], {"widget-enhancements": last})), [])

    def test_a_unit_the_last_report_had_and_this_one_lacks_is_named(self) -> None:
        state = {"widget-enhancements": self.last_report(), "retired-unit": self.last_report()}
        self.assertEqual(self.above_chart(self.gantt([unit(False)], state)), ["- retired-unit: no longer in the report"])


if __name__ == "__main__":
    _ = unittest.main()
