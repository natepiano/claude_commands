#!/usr/bin/env python3
"""dailies_render.py's footer, report, and timeline through this worktree's CLI."""

from __future__ import annotations

import json
import sys
import signal
import dailies_render
from cli_fixture import invoke_cli
import os
import subprocess
import tempfile
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import ClassVar, TypedDict, cast, final, override
from unittest import mock
from zoneinfo import ZoneInfo

from dailies_render import (NoLastReportedEta, StateClear, StateRefused,
                            check_render_state, last_report, load_state)

SCRIPT = Path(__file__).with_name("dailies_render.py")
AT = "2026-10-04T11:00"
ZONE = "America/Los_Angeles"
FENCE = "```"
FOR = "the frame-time lane's breakdown of what each added tool costs"
FOOTER_HEAD = ["", "---", "11:00 PDT update:", ""]
AGENT_LINES = ["* none active"]


def setUpModule() -> None:
    # The state check reads the build hold and mac_test state; no test sees the machine's own.
    scratch = tempfile.TemporaryDirectory()
    previous = {name: os.environ.get(name) for name in ("BUILD_HOLD_DIR", "MAC_TEST_STATE_DIR")}
    os.environ["BUILD_HOLD_DIR"] = str(Path(scratch.name) / "build-hold")
    os.environ["MAC_TEST_STATE_DIR"] = str(Path(scratch.name) / "mac-test")

    def restore() -> None:
        for name, value in previous.items():
            if value is None:
                _ = os.environ.pop(name, None)
            else:
                os.environ[name] = value
        scratch.cleanup()

    unittest.addModuleCleanup(restore)


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


def run(arguments: list[str], scratch: str, holders: list[dict[str, str]] | None = None, *, subprocess_cli: bool = False) -> Run:
    # HOME puts the renderer's AGENTS_DIR and READINGS_LOG under this test directory.
    (Path(scratch) / "rust/hanadocs/agents").mkdir(parents=True, exist_ok=True)
    (Path(scratch) / ".local/state/agent-notes").mkdir(parents=True, exist_ok=True)
    hold_dir = Path(scratch) / "holds"
    hold_dir.mkdir(exist_ok=True)
    for index, record in enumerate(holders or []):
        _ = (hold_dir / f"holder-{index}").write_text(json.dumps(record) + "\n")
    environment = {**os.environ, "HOME": scratch, "BUILD_HOLD_DIR": str(hold_dir),
                   "MAC_TEST_STATE_DIR": str(Path(scratch) / "mac-test")}
    if subprocess_cli:
        result = subprocess.run([sys.executable, str(SCRIPT), *arguments], env=environment,
                                capture_output=True, text=True, check=False, timeout=300)
    else:
        result = invoke_cli(dailies_render, lambda: dailies_render.main(arguments), arguments,
                            environment, Path(scratch), production_state=True)

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
    def test_outstanding_reader_preserves_full_questions_and_distinguishes_review_results(self) -> None:
        question = "Session: demo\nAction: choose a route\n" + "Which route? " * 120
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "outstanding.json"
            _ = path.write_text(json.dumps([
                {"since": AT, "text": "reminder"},
                {"since": AT, "text": "facts needed", "question": question},
                {"since": AT, "text": "route", "question": question, "reviewed": "Use the short route?"},
                {"since": AT, "text": "review failed", "question": question,
                 "review_unavailable": "reviewer unavailable", "after": "2026-10-04T12:00"},
            ]))
            items = dailies_render.read_outstanding(path)
            self.assertIsInstance(items[0], dailies_render.OutstandingReminder)
            awaiting = items[1]
            self.assertIsInstance(awaiting, dailies_render.OutstandingQuestion)
            assert isinstance(awaiting, dailies_render.OutstandingQuestion)
            self.assertEqual(awaiting.question, question)
            reviewed = items[2]
            self.assertIsInstance(reviewed, dailies_render.ReviewedOutstandingQuestion)
            assert isinstance(reviewed, dailies_render.ReviewedOutstandingQuestion)
            self.assertEqual(reviewed.reviewed, "Use the short route?")
            unavailable = items[3]
            self.assertIsInstance(unavailable, dailies_render.ReviewUnavailableOutstandingQuestion)
            assert isinstance(unavailable, dailies_render.ReviewUnavailableOutstandingQuestion)
            self.assertEqual(unavailable.question, question)
            self.assertEqual(unavailable.reason, "reviewer unavailable")
            self.assertEqual(dailies_render.due(items, datetime.fromisoformat(AT)), items[:3])
            self.assertEqual(dailies_render.due(items, datetime(2026, 10, 4, 12)), items)
            output = self.lines(footer("--nothing-needed", "--outstanding", str(path)))
            self.assertFalse(output[-1].endswith(" - nothing needed"))
            self.assertFalse(any("Which route?" in line for line in output))

    def test_outstanding_reader_refuses_conflicting_or_incomplete_review_evidence(self) -> None:
        cases: list[dict[str, object]] = [
            {"reviewed": "Choose now?"},
            {"question": "Choose now?", "reviewed": "Choose now?", "review_unavailable": "offline"},
            {"question": ""},
            {"question": "Choose now?", "reviewed": 42},
            {"question": "Choose now?", "review_unavailable": 42},
            {"question": None},
            {"question": "Choose now?", "reviewed": None},
            {"question": "Choose now?", "review_unavailable": None},
        ]
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "outstanding.json"
            for fields in cases:
                with self.subTest(fields=fields):
                    _ = path.write_text(json.dumps([{"since": AT, "text": "route", **fields}]))
                    with self.assertRaises(dailies_render.InputError):
                        _ = dailies_render.read_outstanding(path)

    def lines(self, result: Run) -> list[str]:
        self.assertEqual(result.code, 0, result.error)
        return result.lines

    def refused(self, result: Run, message: str) -> None:
        self.assertEqual(result.code, 2)
        self.assertIn(message, result.error)

    def tail(self, result: Run) -> list[str]:
        return after_timeline(self.lines(result))

    def test_render_calls_keep_home_configuration_separate_and_restore_the_caller(self) -> None:
        caller_environment = os.environ.copy()
        caller_directory = Path.cwd()
        caller_arguments = sys.argv.copy()
        caller_path = sys.path.copy()
        caller_chart = dailies_render.CHART_CONF
        caller_signals = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            for home, chart in ((first, "ascii"), (second, "default")):
                result = run(["--chart", chart], home)
                self.assertEqual(result.code, 0, result.error)
                self.assertIn(str(Path(home) / ".local/state/showrunner/dailies.conf"), result.error)
            self.assertIn("ascii", (Path(first) / ".local/state/showrunner/dailies.conf").read_text())
            self.assertIn("default", (Path(second) / ".local/state/showrunner/dailies.conf").read_text())
        self.assertEqual(os.environ, caller_environment)
        self.assertEqual(Path.cwd(), caller_directory)
        self.assertEqual(sys.argv, caller_arguments)
        self.assertEqual(sys.path, caller_path)
        self.assertEqual(dailies_render.CHART_CONF, caller_chart)
        self.assertEqual({sig: signal.getsignal(sig) for sig in caller_signals}, caller_signals)

    def test_cold_imported_production_paths_return_to_the_callers_home_after_each_case(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            caller = Path(scratch) / "caller"
            caller.mkdir()
            environment = {**os.environ, "HOME": str(caller)}
            for name in ("SHOWRUNNERS_CONFIG", "NOTIFIER_STATE_DIR", "NOTIFIER_SESSIONS_DIR",
                         "SHOWRUNNER_STATE_DIR", "SHOWRUNNERS_SESSIONS", "SHOWRUNNERS_SEND"):
                _ = environment.pop(name, None)
            program = '''import os, sys
from pathlib import Path
from types import ModuleType
sys.path.insert(0, str(Path.cwd()))
from cli_fixture import invoke_cli
assert "showrunners" not in sys.modules
assert "dailies_render" not in sys.modules
caller = Path.home()
caller_path = sys.path.copy()
command = ModuleType("fixture_command")
command.__file__ = "fixture_command.py"
def paths(home):
    import dailies_render, showrunners, note_file
    assert dailies_render.CHART_CONF == home / ".local/state/showrunner/dailies.conf"
    assert note_file.notes_dir() == home / "rust/hanadocs/agents"
    assert dailies_render.READINGS_LOG == home / ".local/state/agent-notes/readings.jsonl"
    assert showrunners.NOTIFIER_STATE_DIR == home / ".local/state/notifier"
    assert showrunners.SESSIONS_DIR == home / ".claude/sessions"
    assert showrunners.SHOWRUNNER_STATE_DIR == home / ".local/state/showrunner"
for name in ("first", "second"):
    home = caller.parent / name
    home.mkdir()
    def main():
        paths(home)
        return 0
    result = invoke_cli(command, main, [], {**os.environ, "HOME": str(home)}, home,
                        production_state=True)
    assert result.returncode == 0, result.stderr
    assert Path.home() == caller
    assert sys.path == caller_path
    paths(caller)
'''
            result = subprocess.run([sys.executable, "-c", program], env=environment,
                                    cwd=SCRIPT.parent, capture_output=True, text=True, timeout=300)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_counts_print_on_one_line_above_the_chart_in_every_length(self) -> None:
        counts = [{"label": "nightly queue", "count": 8}, {"label": "proposals", "not_found": True}]
        for length in ("gantt", "simple", "page", "elaborate"):
            with self.subTest(length=length):
                lines = self.lines(render({**report(False), "length": length, "counts": counts}))
                at = lines.index("counts: nightly queue 8 · proposals not found")
                self.assertEqual(lines[at + 1 : at + 3], ["", FENCE])

    def test_no_counts_prints_no_counts_line(self) -> None:
        self.assertFalse([line for line in self.lines(render(report(False))) if line.startswith("counts:")])

    def test_a_count_that_is_not_a_whole_number_is_refused(self) -> None:
        for count in (-1, 2.5, True, None, "8"):
            with self.subTest(count=count):
                self.refused(render({**report(False), "counts": [{"label": "queue", "count": count}]}),
                             "counts[0].count: a whole number of 0 or more")

    def test_hold_line_and_unit_marker(self) -> None:
        lines = self.lines(render(report(True), holders=[holder("2026-10-04T11:15:00-07:00")]))
        self.assertIn("12:40 build hold", next(row for row in timeline(lines) if row.startswith("widget")))
        self.assertEqual(after_timeline(lines), ["", *FOOTER_HEAD, f"* build hold: frame-time holds builds since 10:56 PDT for {FOR}; expected to release 11:15 PDT (in 15 minutes)", *AGENT_LINES, "* next dailies: 11:30 PDT - nothing needed"])

    def test_one_minute_is_singular(self) -> None:
        tail = self.tail(render(report(True), holders=[holder("2026-10-04T11:01:00-07:00")]))
        self.assertIn(f"* build hold: frame-time holds builds since 10:56 PDT for {FOR}; expected to release 11:01 PDT (in 1 minute)", tail)

    def test_at_the_time_counts_zero(self) -> None:
        tail = self.tail(render(report(True), holders=[holder("2026-10-04T11:00:00-07:00")]))
        self.assertIn(f"* build hold: frame-time holds builds since 10:56 PDT for {FOR}; expected to release 11:00 PDT (now)", tail)

    def test_one_minute_overdue_is_singular(self) -> None:
        tail = self.tail(render(report(True), holders=[holder("2026-10-04T10:59:00-07:00")]))
        self.assertIn(f"* build hold: frame-time holds builds since 10:56 PDT for {FOR}; expected to release 10:59 PDT (1 minute overdue)", tail)

    def test_release_countdown_crosses_clock_change(self) -> None:
        fields = {**report(True), "zone": "America/New_York"}
        record = holder("2026-11-01T02:30:00-05:00", since="2026-11-01T00:15:00-04:00")
        tail = self.tail(render(fields, at="2026-11-01T00:30", holders=[record]))
        self.assertTrue(any("expected to release 02:30 EST (in 180 minutes)" in line for line in tail))

    def test_plumbing_in_holder_purpose_is_refused_in_report_and_footer(self) -> None:
        with tempfile.TemporaryDirectory() as scratch, mock.patch.dict(os.environ, {
            "HOME": scratch, "BUILDLOG_DIR": str(Path(scratch) / "buildlog"),
            "BUILD_HOLD_DIR": str(Path(scratch) / "holders"),
            "BUILD_HOLD_RELEASE_DIR": str(Path(scratch) / "release"),
            "MAC_TEST_STATE_DIR": str(Path(scratch) / "mac-state"),
            "NOTIFIER_SESSIONS_DIR": str(Path(scratch) / "sessions"),
            "UNIT_ACTIVE_DIR": str(Path(scratch) / "active"),
            "TZ": ZONE, "USER_ZONE": ZONE,
        }):
            record = holder(purpose="the writer's timings")
            for result in (render(report(True), holders=[record]), footer(holders=[record])):
                with self.subTest(result=result):
                    self.refused(result, "frame-time")
                    self.assertIn("'writer'", result.error)
                    self.assertIn("have frame-time run /build:hold hold again with other words", result.error)

    def test_multiple_holders_share_footer(self) -> None:
        records = [holder(), holder("2026-10-04T10:54:00-07:00", "other", "2026-10-04T10:57:00-07:00")]
        lines = self.lines(footer("--next-run", "11:30", holders=records))
        holds = [line for line in lines if line.startswith("* build hold:")]
        self.assertEqual(len(holds), 2)
        self.assertIn("no release time given", holds[0])
        self.assertIn("expected to release 10:54 PDT (6 minutes overdue)", holds[1])

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

    def test_root_footer_counts_lower_lists_and_names_one_it_cannot_read(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            kid, grandkid = Path(scratch) / "kid.json", Path(scratch) / "grandkid.json"
            _ = kid.write_text(json.dumps([{"since": "2026-10-03T09:05", "text": "send the Bevy PR"}]))
            counted = self.lines(footer("--next-run", "11:30", "--nothing-needed", "--below", str(kid),
                                        "--below", str(grandkid)))
            _ = kid.write_text("[]")
            _ = grandkid.write_text("{bad")
            unread = self.lines(footer("--next-run", "11:30", "--nothing-needed", "--below", str(kid),
                                       "--below", str(grandkid)))
            _ = grandkid.write_text("[]")
            quiet = self.lines(footer("--next-run", "11:30", "--nothing-needed", "--below", str(kid),
                                      "--below", str(grandkid)))
        self.assertEqual(counted, [*FOOTER_HEAD, *AGENT_LINES, "* next dailies: 11:30 PDT"])
        self.assertEqual(unread, [*FOOTER_HEAD, *AGENT_LINES, "* grandkid: needs-user list unreadable",
                                  "* next dailies: 11:30 PDT"])
        self.assertEqual(quiet, [*FOOTER_HEAD, *AGENT_LINES, "* next dailies: 11:30 PDT - nothing needed"])

    def test_the_counts_line_follows_the_header_in_footer_and_report(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            ledger = Path(folder) / "ledger.md"
            _ = ledger.write_text("- 2026-10-16 One (one.md) — proposed\n- 2026-10-16 Two (two.md) — accepted 2026-10-16\n")
            doc = Path(folder) / "production.md"
            _ = doc.write_text(f"# Production\n\n- **Improvement queue:** {ledger}\n")
            head = [*FOOTER_HEAD[:-1], "For you: 1 proposal", ""]
            self.assertEqual(self.lines(footer("--doc", str(doc))), [*head, *AGENT_LINES, "* no dailies scheduled"])
            tail = after_timeline(self.lines(render({**report(False), "doc": str(doc)})))
            self.assertEqual(tail, ["", *head, *AGENT_LINES, "* next dailies: 11:30 PDT - nothing needed"])
            # A doc with nothing counted adds no line.
            _ = doc.write_text("# Production\n")
            self.assertEqual(self.lines(footer("--doc", str(doc))), [*FOOTER_HEAD, *AGENT_LINES, "* no dailies scheduled"])

    def test_lower_showrunner_footer_names_its_root(self) -> None:
        self.assertEqual(self.lines(footer("--next-run", "11:30", "--reports-to", "build-followups")),
                         [*FOOTER_HEAD, *AGENT_LINES, "* reports to build-followups", "* next dailies: 11:30 PDT"])

    def test_held_unit_has_no_section_line(self) -> None:
        lines = self.lines(render(report(True), holders=[holder("2026-10-04T11:15:00-07:00")]))
        section = lines[: lines.index(FENCE)]
        self.assertEqual([line for line in section if "build hold" in line], [])

    def test_next_dailies_on_a_later_day_shows_its_weekday(self) -> None:
        self.assertEqual(self.lines(footer("--next-run", "09:30+1")), [*FOOTER_HEAD, *AGENT_LINES, "* next dailies: Mon 09:30 PDT"])

    def test_stale_marker_and_unmarked_active_hold_are_refused(self) -> None:
        self.refused(render(report(True)), "remove the stale unit marker")
        self.refused(render(report(False), holders=[holder()]), "mark the held unit")

    def test_a_report_whose_only_units_hold_the_builds_needs_no_marker(self) -> None:
        # A holder's own unit is never marked, so with no other unit there is none to mark.
        by_name = render({**report(False), "units": [{**unit(False), "unit": "frame-time"}]}, holders=[holder()])
        # A holder is named by its session; the builder says which unit that session directs.
        by_session = render({**report(False), "units": [{**unit(False), "holds_builds": True}]}, holders=[holder()])
        for result in (by_name, by_session):
            self.assertEqual(result.code, 0, result.error)
            self.assertEqual([line for line in timeline(result.lines) if "build hold" in line], [])
            self.assertEqual(len([line for line in result.lines if line.startswith("* build hold: frame-time ")]), 1)
        both = [{**unit(False), "holds_builds": True}, {**unit(False), "unit": "other", "label": "other"}]
        self.refused(render({**report(False), "units": both}, holders=[holder()]), "mark the held unit")
        marked = render({**report(False), "units": [{**unit(True), "holds_builds": True}]}, holders=[holder()])
        self.refused(marked, "holds the build hold itself")

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
                    result = run([str(input_path), "--at", AT, "--chart", chart], scratch, subprocess_cli=True)
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

    def test_an_eta_stated_again_a_day_later_with_the_same_clock_is_read_on_its_new_day(self) -> None:
        fields = report(held=False)
        current = {**unit(False), "eta": {"time": "16:00", "percent": 60, "stated": "2026-10-05T09:30",
                                          "why": "the fix needed a second day"}}
        fields["units"] = [current]
        previous = {
            "phase": current["phase"],
            "eta": "2026-10-04T16:00:00",
            "eta_text": "16:00",
            "held": None,
            "first": "2026-10-04T16:00:00",
        }
        result, saved, _ = self.run_with_state(fields, previous, "2026-10-05T10:00")
        self.assertEqual(result.code, 0, result.error)
        self.assertEqual(cast(dict[str, object], saved["widget-enhancements"])["eta"], "2026-10-05T16:00:00")
        self.assertIn("- eta: 16:00 PDT, 60% done (changed: +24:00 because the fix needed a second day)",
                      result.lines)

    def test_a_passed_etas_range_stays_on_the_etas_day(self) -> None:
        fields = report(held=False)
        fields["units"] = [{**unit(False), "eta": {"time": "10:00", "earliest": "09:40", "latest": "10:30",
                                                   "percent": 60, "stated": "2026-10-04T09:00"}}]
        result = render(fields, "2026-10-04T14:00")
        self.assertEqual(result.code, 0, result.error)
        self.assertIn("- eta: 10:00 PDT, 60% done (overdue; range 09:40–10:30)", result.lines)

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


def saved_row(name: str, started: str = "", eta: str = "") -> dict[str, object]:
    """One row of a lower production's saved report; with no times, a row whose ETA is not known."""
    if not eta:
        return {"name": name}
    return {"name": name, "started": started, "eta": eta, "earliest": eta, "latest": eta}


def saved(written: str, *rows: dict[str, object], units: int = 2, held: int = 0, interval: int | None = 30,
          zone: str = ZONE) -> dict[str, object]:
    return {"written": written, "zone": zone, "interval": interval, "units": units, "held": held, "rows": list(rows)}


def child(name: str, saved_report: dict[str, object] | None = None, **fields: object) -> dict[str, object]:
    lower: dict[str, object] = {"name": name, "open": 0, **fields}
    if saved_report is not None:
        lower["report"] = saved_report
    return lower


class LowerProductionTests(unittest.TestCase):
    """A showrunner's report shows each production directly below it as one row."""

    ROWS: ClassVar[tuple[dict[str, object], ...]] = (
        saved_row("fps", "2026-10-04T09:10", "2026-10-04T13:20"),
        saved_row("docs", "2026-10-04T10:05", "2026-10-04T14:40"),
        saved_row("idle"),
    )
    lines: list[str] = []
    # The chart opens at 06:00 for the unit's 08:50 start, so a 09:10 start sits three empty cells in.
    LEAD: ClassVar[str] = "  " * 3

    def rows(self, *children: dict[str, object], reports_to: str = "") -> list[str]:
        fields: dict[str, object] = {**report(False), "children": list(children)}
        if reports_to:
            fields["reports_to"] = reports_to
        result = render(fields)
        self.assertEqual(result.code, 0, result.error)
        self.lines = result.lines
        return timeline(result.lines)[2:]

    def refused(self, message: str, *children: dict[str, object]) -> None:
        result = render({**report(False), "children": list(children)})
        self.assertEqual(result.code, 2)
        self.assertIn(message, result.error)

    def test_no_units_and_nothing_below_renders_one_line_and_no_chart(self) -> None:
        # Seen 2026-10-12: every unit's run had finished, so the builder left them all out.
        result = render({**report(False), "units": []})
        self.assertEqual(result.code, 0, result.error)
        self.assertEqual(result.lines[:4], ["**Dailies (Simple)**, 11:00 PDT", "",
                                            "No unit is working: every run has finished.", ""])
        self.assertNotIn("```", result.lines)
        self.assertIn("* next dailies: 11:30 PDT - nothing needed", result.lines)

    def test_no_units_still_charts_the_productions_below(self) -> None:
        below = child("tool-based-ui", saved("2026-10-04T10:40", *self.ROWS, units=3, held=1))
        result = render({**report(False), "units": [], "children": [below]})
        self.assertEqual(result.code, 0, result.error)
        self.assertNotIn("No unit is working: every run has finished.", result.lines)
        rows = timeline(result.lines)[1:]
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].startswith("tool-based-ui "), rows[0])
        self.assertTrue(rows[0].endswith("  3 units, 1 held"), rows[0])

    def test_a_production_with_nothing_below_it_renders_as_before(self) -> None:
        plain = render(report(False))
        self.assertEqual(render({**report(False), "children": []}).lines, plain.lines)
        self.assertEqual(timeline(plain.lines)[1][:9], "widget   ")

    def test_a_current_child_is_one_row_from_its_earliest_start_to_its_latest_eta(self) -> None:
        rows = self.rows(child("tool-based-ui", saved("2026-10-04T10:40", *self.ROWS, units=3, held=1)))
        self.assertEqual(rows, [f"tool-based-ui {self.LEAD}⬜⬜⬜⬜⬜🟦 14:40  3 units, 1 held"])
        # The label column widens for the name, and the unit's row keeps its plan column.
        self.assertTrue(timeline(self.lines)[1].startswith("widget          "), timeline(self.lines)[1])
        self.assertIn("Phase 2 of 3", timeline(self.lines)[1])

    def test_a_stale_child_says_when_its_report_is_from(self) -> None:
        # Two intervals old is still current; a minute more is not.
        self.assertEqual(self.rows(child("kid", saved("2026-10-04T10:00", *self.ROWS))),
                         [f"kid      {self.LEAD}⬜⬜⬜⬜⬜🟦 14:40  2 units, 0 held"])
        self.assertEqual(self.rows(child("kid", saved("2026-10-04T09:59", *self.ROWS))),
                         [f"kid      {self.LEAD}⬜⬜⬜⬜⬜🟦 14:40  2 units, 0 held, as of 09:59"])
        self.assertTrue(self.rows(child("kid", saved("2026-10-03T21:15", *self.ROWS, units=1)))[0]
                        .endswith("  1 unit, 0 held, as of Sat 21:15"))

    def test_a_child_that_reports_on_demand_always_says_when(self) -> None:
        self.assertEqual(self.rows(child("kid", saved("2026-10-04T10:58", *self.ROWS, interval=None))),
                         [f"kid      {self.LEAD}⬜⬜⬜⬜⬜🟦 14:40  2 units, 0 held, as of 10:58"])

    def test_a_child_with_no_saved_report_reads_no_report_yet(self) -> None:
        self.assertEqual(self.rows(child("kid")), ["kid      ?  no report yet"])

    def test_a_child_with_no_eta_keeps_its_counts(self) -> None:
        self.assertEqual(self.rows(child("kid", saved("2026-10-04T10:40", saved_row("idle"), units=1))),
                         ["kid      ?  1 unit, 0 held"])

    def test_times_come_over_from_the_child_s_zone(self) -> None:
        eastern = saved("2026-10-04T13:40", saved_row("fps", "2026-10-04T12:10", "2026-10-04T17:40"),
                        zone="America/New_York")
        self.assertEqual(self.rows(child("kid", eastern)),
                         [f"kid      {self.LEAD}⬜⬜⬜⬜⬜🟦 14:40  2 units, 0 held"])

    def test_expand_prints_the_child_s_rows_under_its_own(self) -> None:
        rows = self.rows(child("kid", saved("2026-10-04T10:40", *self.ROWS), expand=True),
                         child("other", saved("2026-10-04T10:40", *self.ROWS)))
        self.assertEqual(rows, [f"kid      {self.LEAD}⬜⬜⬜⬜⬜🟦 14:40  2 units, 0 held",
                                f"kid/fps  {self.LEAD}⬜⬜⬜⬜🟦 13:20",
                                f"kid/docs {self.LEAD}  ⬜⬜⬜⬜🟦 14:40",
                                "kid/idle ?",
                                f"other    {self.LEAD}⬜⬜⬜⬜⬜🟦 14:40  2 units, 0 held"])

    def test_what_waits_on_the_user_below_marks_the_row_and_the_footer(self) -> None:
        quiet = self.rows(child("kid", saved("2026-10-04T10:40", *self.ROWS)))
        self.assertEqual(after_timeline(self.lines)[-1], "* next dailies: 11:30 PDT - nothing needed")
        self.assertNotIn("open question", quiet[0])
        marked = self.rows(child("kid", saved("2026-10-04T10:40", *self.ROWS), open=1), child("new", open=2),
                           reports_to="top")
        self.assertTrue(marked[0].endswith("  2 units, 0 held, 1 open question"), marked[0])
        self.assertEqual(marked[1], "new      ?  no report yet, 2 open questions")
        self.assertEqual(after_timeline(self.lines)[-2:], ["* reports to top", "* next dailies: 11:30 PDT"])

    def test_a_list_below_that_cannot_be_read_is_named_in_the_footer(self) -> None:
        _ = self.rows(child("other", unreadable=["other"]),
                      child("kid", saved("2026-10-04T10:40", *self.ROWS), unreadable=["leaf", "kid"]))
        self.assertEqual(after_timeline(self.lines)[-4:],
                         ["* kid: needs-user list unreadable", "* leaf: needs-user list unreadable",
                          "* other: needs-user list unreadable", "* next dailies: 11:30 PDT"])

    def test_a_saved_report_carries_the_counts_and_one_row_of_each_production_below(self) -> None:
        # Three levels: this production saves the leaf's counts inside its own, so its parent reads one file.
        middle = {**report(False), "children": [child("leaf", saved("2026-10-04T10:40", *self.ROWS, units=3, held=1),
                                                      expand=True)]}
        with tempfile.TemporaryDirectory() as scratch:
            summary = last_report(middle, Path(scratch) / "none.json", AT, dailies_render.ReportsEvery(30))
        self.assertEqual({key: summary[key] for key in ("written", "zone", "interval", "units", "held")},
                         {"written": AT, "zone": ZONE, "interval": 30, "units": 4, "held": 1})
        self.assertEqual(summary["rows"], [
            {"name": "widget", "started": "2026-10-04T08:50", "eta": "2026-10-04T12:40",
             "earliest": "2026-10-04T12:40", "latest": "2026-10-04T12:40"},
            {"name": "leaf", "started": "2026-10-04T09:10", "eta": "2026-10-04T14:40",
             "earliest": "2026-10-04T14:40", "latest": "2026-10-04T14:40"}])
        # The top reads that file as it is and draws the middle with the leaf inside its numbers.
        rows = self.rows(child("middle", summary, expand=True))
        self.assertEqual(rows, ["middle            ⬜⬜⬜⬜⬜⬜🟦 14:40  4 units, 1 held",
                                "middle/widget     ⬜⬜⬜⬜🟦 12:40",
                                f"middle/leaf   {self.LEAD}⬜⬜⬜⬜⬜🟦 14:40"])

    def test_a_malformed_child_is_refused(self) -> None:
        self.refused("children: widget is also a unit's label", child("widget"))
        self.refused("children[0].open: a whole number of 0 or more", {"name": "kid"})
        self.refused("children[0].report.written: required", child("kid", {"zone": ZONE}))
        self.refused("children[0].report.zone: 'Mars/Base' is not an IANA zone name",
                     child("kid", saved("2026-10-04T10:40", zone="Mars/Base")))
        self.refused("children[0].report.rows[0].eta: required",
                     child("kid", saved("2026-10-04T10:40", {"name": "fps", "started": "2026-10-04T09:10"})))


class StyleSummaryFields(TypedDict):
    rules_done: int
    rules_total: int
    unclassified_rules: int
    cells_current_pct: float
    stale: int
    proposals: int
    waiting_to_merge_back: int
    oldest_waiting: str | None
    synced_at: str
    synced_tip: str


def style_summary(**changed: object) -> dict[str, object]:
    fields: StyleSummaryFields = {
        "rules_done": 21, "rules_total": 51, "unclassified_rules": 0,
        "cells_current_pct": 62.0, "stale": 1402, "proposals": 3,
        "waiting_to_merge_back": 0, "oldest_waiting": None,
        "synced_at": "2026-10-04T17:05:00+00:00", "synced_tip": "a" * 40,
    }
    return {**fields, **changed}


@final
class StyleChildReportTests(unittest.TestCase):
    def saved_style(self, **changed: object) -> dict[str, object]:
        return {**saved("2026-10-04T10:40", saved_row("names-unit")),
                "style": style_summary(**changed)}

    def parsed(self, fields: dict[str, object]) -> dailies_render.ChildReport:
        return dailies_render.parse_child_report(fields, "report", ZoneInfo(ZONE))

    def rendered(self, fields: dict[str, object], *, expand: bool = False) -> list[str]:
        result = render({**report(False), "children": [child("hana-style", fields, expand=expand)]})
        self.assertEqual(result.code, 0, result.error)
        return result.lines

    def test_legacy_report_has_no_style_line_and_keeps_its_child_row(self) -> None:
        fields = saved("2026-10-04T10:40", saved_row("names-unit"))
        self.assertIsInstance(self.parsed(fields).style, dailies_render.NoStyleSummary)
        lines = self.rendered(fields)
        self.assertEqual([line for line in lines if line.startswith("hana-style")],
                         ["hana-style ?  2 units, 0 held"])
        self.assertFalse(any("rules done" in line or "style summary" in line for line in lines))

    def test_unavailable_summary_is_printed_directly_under_the_child_row(self) -> None:
        fields = {**saved("2026-10-04T10:40", saved_row("names-unit")),
                  "style": {"unavailable": "ledger database is locked"}}
        parsed = self.parsed(fields).style
        self.assertIsInstance(parsed, dailies_render.StyleSummaryUnavailable)
        assert isinstance(parsed, dailies_render.StyleSummaryUnavailable)
        self.assertEqual(parsed.reason, "ledger database is locked")
        lines = self.rendered(fields)
        row = next(index for index, line in enumerate(lines) if line.startswith("hana-style "))
        self.assertEqual(lines[row + 1], "hana-style  style summary unavailable: ledger database is locked")

    def test_summary_without_pending_handoffs_has_no_waiting_flag(self) -> None:
        fields = self.saved_style()
        parsed = self.parsed(fields).style
        self.assertIsInstance(parsed, dailies_render.StyleSummary)
        assert isinstance(parsed, dailies_render.StyleSummary)
        self.assertIsInstance(parsed.waiting, dailies_render.NoPendingHandoffs)
        self.assertEqual(parsed.synced_tip, "a" * 40)
        self.assertEqual(parsed.synced_at, datetime(2026, 10, 4, 10, 5))
        lines = self.rendered(fields)
        row = next(index for index, line in enumerate(lines) if line.startswith("hana-style "))
        self.assertEqual(lines[row + 1],
                         "hana-style  rules done 21/51 · cells 62% · stale 1,402 · proposals 3 · synced 10:05")
        self.assertFalse(any("hand-off waiting" in line for line in lines))

    def test_pending_handoff_is_flagged_only_after_four_hours(self) -> None:
        for created, flag in (("2026-10-04T14:00:00+00:00", ""),
                              ("2026-10-04T12:00:00+00:00", " · hand-off waiting 6h")):
            with self.subTest(created=created):
                fields = self.saved_style(waiting_to_merge_back=1, oldest_waiting=created)
                parsed = self.parsed(fields).style
                assert isinstance(parsed, dailies_render.StyleSummary)
                self.assertIsInstance(parsed.waiting, dailies_render.PendingHandoffs)
                assert isinstance(parsed.waiting, dailies_render.PendingHandoffs)
                self.assertEqual(parsed.waiting.count, 1)
                lines = self.rendered(fields)
                summary = next(line for line in lines if "rules done" in line)
                self.assertEqual(summary,
                                 "hana-style  rules done 21/51 · cells 62% · stale 1,402 · proposals 3 · synced 10:05" + flag)

    def test_unclassified_rules_add_the_pull_instruction(self) -> None:
        lines = self.rendered(self.saved_style(unclassified_rules=7))
        self.assertIn("hana-style  rules done 21/51 · cells 62% · stale 1,402 · proposals 3 · synced 10:05"
                      + " · unclassified 7 — pull nate_style", lines)

    def test_pending_handoff_age_is_elapsed_time_across_both_daylight_saving_changes(self) -> None:
        for stamp in ("2026-03-08T08:30:00+00:00", "2026-11-01T07:30:00+00:00"):
            with self.subTest(created=stamp):
                created = datetime.fromisoformat(stamp)
                at = (created + timedelta(hours=5, minutes=1)).astimezone(ZoneInfo(ZONE))
                fields = self.saved_style(waiting_to_merge_back=1, oldest_waiting=stamp)
                result = render({**report(False), "children": [child("hana-style", fields)]},
                                at=at.strftime("%Y-%m-%dT%H:%M"))
                self.assertEqual(result.code, 0, result.error)
                summary = next(line for line in result.lines if "rules done" in line)
                self.assertTrue(summary.endswith(" · hand-off waiting 5h"), summary)

    def test_sync_from_an_earlier_day_names_its_weekday(self) -> None:
        lines = self.rendered(self.saved_style(synced_at="2026-10-01T17:05:00+00:00"))
        summary = next(line for line in lines if "rules done" in line)
        self.assertTrue(summary.endswith(" · synced Thu 10:05"), summary)

    def test_cells_percent_is_floored_and_reaches_one_hundred_only_when_complete(self) -> None:
        for percentage, expected in ((99.6, 99), (100.0, 100), (0.5, 0)):
            with self.subTest(percentage=percentage):
                lines = self.rendered(self.saved_style(cells_current_pct=percentage, stale=1))
                summary = next(line for line in lines if "rules done" in line)
                self.assertIn(f" · cells {expected}% · stale 1 · ", summary)

    def test_unknown_key_inside_the_style_summary_is_refused(self) -> None:
        with self.assertRaisesRegex(dailies_render.InputError, r"report\.style.*unknown.*invented"):
            _ = self.parsed(self.saved_style(invented=1))

    def test_style_child_expansion_keeps_all_four_unit_rows(self) -> None:
        names = ("names-unit", "types-unit", "modules-unit", "structure-unit")
        fields = {**saved("2026-10-04T10:40", *(saved_row(name) for name in names), units=4),
                  "style": style_summary()}
        lines = self.rendered(fields, expand=True)
        self.assertEqual([line.split()[0] for line in lines if line.startswith("hana-style/")],
                         [f"hana-style/{name}" for name in names])
        self.assertEqual(sum("rules done" in line for line in lines), 1)

    def test_saved_cadence_variants_preserve_the_legacy_json_bytes(self) -> None:
        for interval, cadence in ((None, dailies_render.ReportsOnDemand()),
                                  (30, dailies_render.ReportsEvery(30))):
            with self.subTest(interval=interval), tempfile.TemporaryDirectory() as scratch:
                legacy = saved(AT, units=0, interval=interval)
                parsed = self.parsed(legacy)
                self.assertEqual(parsed.cadence, cadence)
                current = last_report({"length": "simple", "zone": ZONE, "units": []},
                                      Path(scratch) / "none.json", AT, cadence)
                self.assertEqual(json.dumps(current, indent=2).encode(), json.dumps(legacy, indent=2).encode())


if __name__ == "__main__":
    _ = unittest.main()
