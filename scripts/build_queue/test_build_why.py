"""Admission-backed explanations of the 21:51 build queue."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
import io
import fcntl
import json
import os
import re
from pathlib import Path
import tempfile
import time
import unittest
from typing import TypedDict, Unpack, cast, override
from unittest import mock

from scripts.build_queue import build_why, head_facts
from scripts.build_queue.queue_fixture import write_process
from scripts.build_queue.test_head_facts import bump_line, queue_time_line, token_gap_line, write_bumps, write_token_gaps
from scripts.lint import memory_admit as gate
from scripts.lib.user_zone import user_zone
from scripts.message import roster, unit_labels

GIB = gate.GIB
NOW = datetime(2026, 10, 30, 1, 51, tzinfo=UTC).timestamp()


def gib(value: float) -> int:
    return int(value * GIB)


class FinishedTokenGapTimes(TypedDict, total=False):
    token_left_at: float
    token_back_at: float


class BuildWhyTests(unittest.TestCase):
    root: Path = Path()
    directory: Path = Path()
    proc: Path = Path()
    head_path: Path = Path()
    widget_path: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.directory = self.root / "buildlog/admission"
        self.directory.mkdir(parents=True)
        _ = (self.directory / "lock").touch()
        self.proc = self.root / "proc"
        self.proc.mkdir()
        _ = (self.proc / "loadavg").write_text("64.25 60.00 55.00 32/1000 999\n")
        _ = (self.root / "meminfo").write_text(
            f"MemAvailable: {gib(16.3) // 1024} kB\nMemTotal: {60 * GIB // 1024} kB\n")
        _ = (self.root / "pressure").write_text(
            "some avg10=0.00 avg60=0.00 avg300=0.00 total=0\n" +
            "full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n")
        self.enterContext(mock.patch.dict(os.environ, {
            "BUILDLOG_DIR": str(self.root / "buildlog"),
            "BUILDLOG_PROC": str(self.proc),
            "BUILDLOG_MEMINFO": str(self.root / "meminfo"),
            "BUILDLOG_MEMORY_PRESSURE": str(self.root / "pressure"),
            "BUILDLOG_BUILDS_CGROUP": str(self.root / "builds.slice"),
            "BUILDLOG_BUILD_BUDGET_GIB": "21",
            "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"),
            "USER_ZONE": "America/New_York",
        }))
        _ = self.enterContext(mock.patch.object(time, "time", return_value=NOW))
        _ = self.enterContext(mock.patch.object(os, "cpu_count", return_value=32))
        self.widget_path = self.running(100, "widget", "nextest", 10.8, 0.8, 3600, peak=1.0)
        _ = self.running(200, "demo", "nextest", 1.0, 3.8, 1200, peak=3.8)
        _ = self.running(300, "organon", "clippy", 6.1, 1.3, 30)
        _ = self.running(400, "frame-cost", "clippy", 6.8, 6.1, 60)
        compiler = self.root / "builds.slice/sccache.service"
        compiler.mkdir(parents=True)
        _ = (compiler / "memory.stat").write_text(f"anon {gib(0.4)}\nfile 0\n")
        self.head_path = self.waiter(500, "inspectors-fix", 11.8, 122)
        self.change_head(need_source="measured", need_runs=7, test_threads=8,
                         test_scope="filtered", package="hana_inspectors",
                         filters=["test(one)", "test(two)"], holds_younger=True,
                         priority_since=NOW - 120)
        for number in range(16):
            _ = self.waiter(600 + number, f"younger-{number}", number + 1, 20 - number)
        entries = [self.entry(name) for name in ("widget", "demo", "organon", "frame-cost", "inspectors-fix")]
        labels = roster.unit_labels(entries)
        _ = self.enterContext(mock.patch.object(unit_labels, "unit_labels_for", return_value=labels))
        _ = self.enterContext(mock.patch.object(roster, "roster", return_value=roster.Roster(entries, [])))
        _ = self.enterContext(mock.patch.object(roster, "unit_labels_for", return_value=labels))

    def entry(self, name: str) -> roster.RosterEntry:
        return roster.RosterEntry(
            machine="test-machine", kind="codex", role="freestanding", name=f"{name}-session",
            status="running", reach=roster.CannotReceiveMessages("fixture"), cwd="/tmp/tree",
            showrunner=roster.UnderShowrunner("hana", "hana", "session:hana"),
            director=roster.UnderDirector(f"{name}-unit", name, f"session:{name}"),
            assignment=roster.NotAWorker(), thread=roster.CodexThread(f"id-{name}"),
            session=roster.NoClaudeSession(),
        )

    def running(self, pid: int, name: str, step: str, need: float, used: float,
                age: int, *, peak: float = 0) -> Path:
        write_process(self.proc, pid, gib(used))
        folder = "/tmp/p34-merge" if name == "widget" else f"/tmp/{name}"
        row: dict[str, object] = {
            "pid": pid, "start_time": str(pid), "need": gib(need), "repo": "hana", "step": step,
            "worktree": folder, "sidecar": "", "admitted_at": gate.store.utc_iso(NOW - age),
            "test_threads": 8 if step == "nextest" else None, "call_id": None,
            "session_id": f"id-{name}", "session_name": f"stale-{name}",
        }
        path = self.directory / f"{pid}.reservation"
        _ = path.write_text(json.dumps(row))
        if peak:
            _ = path.with_suffix(".max").write_text(str(gib(peak)))
            _ = path.with_suffix(".wave").write_text(f"{gib(peak)} {int(NOW - 60)}")
        return path

    def waiter(self, pid: int, name: str, need: float, minutes: int) -> Path:
        write_process(self.proc, pid)
        row: dict[str, object] = {
            "pid": pid, "start_time": str(pid), "need": gib(need), "since": NOW - minutes * 60,
            "session_id": f"id-{name}", "session_name": name, "step": "nextest", "folder": f"/tmp/{name}",
        }
        path = self.directory / f"{pid}.waiting"
        _ = path.write_text(json.dumps(row))
        return path

    def change_head(self, **changes: object) -> None:
        row = cast(dict[str, object], json.loads(self.head_path.read_text()))
        row.update(changes)
        _ = self.head_path.write_text(json.dumps(row))

    def output(self) -> str:
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors), mock.patch("sys.argv", ["build_why.py"]):
            result = build_why.main()
        self.assertEqual(result, 0, errors.getvalue())
        self.assertEqual(errors.getvalue(), "")
        return output.getvalue()

    def line(self, output: str, prefix: str) -> str:
        return next(line.strip() for line in output.splitlines() if line.strip().startswith(prefix))

    def reset_running(self, *, available: float, need: float) -> None:
        for pattern in ("*.reservation", "*.max", "*.wave"):
            for path in self.directory.glob(pattern):
                path.unlink()
        _ = (self.root / "builds.slice/sccache.service/memory.stat").write_text("anon 0\nfile 0\n")
        _ = (self.root / "meminfo").write_text(
            f"MemAvailable: {gib(available) // 1024} kB\nMemTotal: {60 * GIB // 1024} kB\n")
        self.change_head(need=gib(need))

    def test_release_advice_uses_admissions_run_alone_exception(self) -> None:
        self.reset_running(available=40, need=22)
        _ = self.running(800, "only-holder", "clippy", 1, 1, 3600)
        output = self.output()
        final = self.line(output, "Waiting because:")
        self.assertIn("stale-only-holder clippy end", final)
        self.assertNotIn("insufficient", final)
        self.assertNotIn("still leaves", final)
        result = gate.head_after_steps_end(gate.explain_queue(), (0,))
        self.assertIsInstance(result, gate.HeadAdmitsAfterEnd)

    def test_override_free_memory_terms_add_up_to_its_printed_threshold(self) -> None:
        self.reset_running(available=23, need=12)
        _ = self.running(800, "growing", "clippy", 20, 1, 30)
        starts = {pid: identity.start for pid, identity in gate.process_table(self.proc).items()}
        with mock.patch.object(gate, "showrunner_is_registered", return_value=True), \
                mock.patch.object(gate, "process_start_time", side_effect=starts.get), \
                mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "showrunner-id", "CODEX_THREAD_ID": ""}):
            _ = gate.admit_head("arithmetic regression")
        output = self.output()
        free = self.line(output, "Free memory:")
        match = re.search(r"need ([0-9.]+) \+ earlyoom ([0-9.]+) \+ promise not yet used ([0-9.]+) = ([0-9.]+) GiB: pass", free)
        self.assertIsNotNone(match, free)
        if match is not None:
            need, reserve, growth, threshold = (float(value) for value in match.groups())
            self.assertEqual((need, reserve, threshold), (12.0, 3.0, 15.0))
            self.assertEqual(need + reserve + growth, threshold, free)

    def test_release_advice_names_only_the_smaller_step_that_frees_enough_memory(self) -> None:
        self.reset_running(available=3, need=8)
        _ = self.running(800, "large-peak", "clippy", 10, 0.1, 3600, peak=10)
        _ = self.running(801, "useful-release", "clippy", 9, 9, 3600)
        final = self.line(self.output(), "Waiting because:")
        self.assertIn("stale-useful-release clippy end", final)
        self.assertNotIn("large-peak", final)
        self.assertIn("budget charge drops 9.0 GiB", final)

    def test_release_ties_choose_least_charge_then_running_table_order(self) -> None:
        self.reset_running(available=40, need=14)
        for pid, name, charge in ((800, "larger", 8), (801, "first-small", 6), (802, "second-small", 6)):
            _ = self.running(pid, name, "clippy", charge, charge, 3600)
        final = self.line(self.output(), "Waiting because:")
        self.assertIn("stale-larger clippy + stale-first-small clippy end", final)
        self.assertNotIn("second-small", final)

    def test_equal_size_releases_choose_the_least_charge_even_later_in_the_table(self) -> None:
        self.reset_running(available=40, need=6)
        for pid, name, charge in ((800, "largest", 8), (801, "medium", 7),
                                  (802, "small-a", 6), (803, "small-b", 6)):
            _ = self.running(pid, name, "clippy", charge, charge, 3600)
        final = self.line(self.output(), "Waiting because:")
        self.assertIn("stale-small-a clippy + stale-small-b clippy end", final)
        self.assertNotIn("largest", final)
        self.assertNotIn("medium", final)
        self.assertIn("budget charge drops 12.0 GiB", final)

    def test_recovery_spacing_names_its_start_in_the_display_zone_without_endings(self) -> None:
        self.reset_running(available=40, need=4)
        _ = self.running(800, "holder", "clippy", 6, 6, 3600)
        gate.write_recovery(self.directory, gate.RecoveryStarts(NOW + 120, NOW + 37))
        for zone, expected in (("America/New_York", "21:51:37 EDT"), ("UTC", "01:51:37 UTC")):
            with self.subTest(zone=zone), mock.patch.dict(os.environ, {"USER_ZONE": zone}):
                final = self.line(self.output(), "Waiting because:")
                self.assertIn("no running step needs to end; recovery spacing starts it at " + expected, final)
                self.assertNotIn("still leaves pressure", final)

    def test_recovery_spacing_names_the_required_endings_before_its_start(self) -> None:
        self.reset_running(available=40, need=4)
        _ = self.running(800, "holder", "clippy", 20, 20, 3600)
        gate.write_recovery(self.directory, gate.RecoveryStarts(NOW + 120, NOW + 37))
        final = self.line(self.output(), "Waiting because:")
        self.assertIn("stale-holder clippy end (budget charge drops 20.0 GiB; "
                      + "free memory rises 20.0 GiB and promise not yet used drops 0.0 GiB); "
                      + "then recovery spacing starts it at 21:51:37 EDT", final)
        self.assertNotIn("no running step needs to end", final)
        self.assertNotIn("still leaves pressure", final)

    def test_recovery_release_finishes_with_the_start_time(self) -> None:
        cases = (
            (6, "no running step needs to end; recovery spacing starts it at 21:51:37 EDT."),
            (20, "stale-holder clippy end (budget charge drops 20.0 GiB; "
             + "free memory rises 20.0 GiB and promise not yet used drops 0.0 GiB); "
             + "then recovery spacing starts it at 21:51:37 EDT."),
        )
        for index, (charge, expected) in enumerate(cases):
            with self.subTest(charge=charge):
                self.reset_running(available=40, need=4)
                _ = self.running(800 + index, "holder", "clippy", charge, charge, 3600)
                gate.write_recovery(self.directory, gate.RecoveryStarts(NOW + 120, NOW + 37))
                final = self.line(self.output(), "Waiting because:")
                self.assertEqual(final.partition("Frees when: ")[2], expected)

    def test_release_keeps_the_snapshot_limits_and_reports_a_pressure_hold(self) -> None:
        explanation = gate.explain_queue()
        with mock.patch.object(gate, "build_budget_bytes", side_effect=AssertionError("use snapshot budget")), \
             mock.patch.object(gate, "read_meminfo", side_effect=AssertionError("no second reading")):
            release = gate.smallest_head_release(explanation)
        self.assertIsInstance(release, gate.HeadAdmitsAfterEnd)
        _ = (self.root / "pressure").write_text(
            "some avg10=0.00 avg60=0.00 avg300=0.00 total=0\n" +
            "full avg10=25.00 avg60=0.00 avg300=0.00 total=0\n")
        final = self.line(self.output(), "Waiting because:")
        self.assertIn("still leaves pressure holding the head", final)

    def test_twenty_running_steps_explain_and_render_in_under_one_second(self) -> None:
        self.reset_running(available=15, need=18)
        # Charge and free-memory constraints favor different halves of the running list.
        for index in range(20):
            charge, used = (10.0, 0.1) if index < 10 else (1.0, 1.0)
            _ = self.running(800 + index, f"holder-{index}", "clippy", charge, used, 3600, peak=charge)
        started = time.perf_counter()
        reading = gate.locked_queue_read()
        explanation = reading.explanation
        lines = build_why.render_explanation(explanation, user_zone(), {}, head_facts.read_head_facts(reading))
        elapsed = time.perf_counter() - started
        self.assertEqual(len(explanation.running), 20)
        final = self.line("\n".join(lines), "Waiting because:")
        self.assertNotIn("still leaves", final)
        self.assertIn("end (budget charge drops", final)
        self.assertLess(elapsed, 1.0, f"20-step explanation and render took {elapsed:.6f} s")

    def test_tonight_arithmetic_matches_the_actual_admission_decision(self) -> None:
        view, ordered = gate.read_queue(self.directory, gate.process_table(self.proc), NOW)
        reason = view.waiting[0].reason
        self.assertIsInstance(reason, gate.HeadAdmissionStatus)
        if not isinstance(reason, gate.HeadAdmissionStatus):
            self.fail(f"expected head admission status, got {reason}")
        running = [row.measured for row in view.running]
        head = gate.head_priority([row for _, row in ordered], view.available, 60 * GIB, running, NOW)
        decision = gate.queue_decision(view.available, 60 * GIB, ordered[0][1], [row for _, row in ordered], running, head,
                                      gate.NoPressureHold(), recovery=gate.NoRecoverySpacing(), now=NOW)
        self.assertEqual(reason.decision, decision)
        self.assertEqual(decision.state, "hold")
        self.assertAlmostEqual(decision.charged / GIB, 18.1, places=5)
        self.assertAlmostEqual(decision.promised / GIB, 5.5, places=5)
        self.assertAlmostEqual(decision.threshold / GIB, 20.3, places=5)

        output = self.output()
        for section in ("Running", "First in line", "Build budget:", "Free memory:", "Waiting because:"):
            self.assertIn(section, output)
        budget = self.line(output, "Build budget:")
        for term in ("18.1", "11.8", "29.9", "21.0", "9.2", "blocked"):
            self.assertIn(term, budget)
        free = self.line(output, "Free memory:")
        for term in ("16.3", "11.8", "earlyoom", "3.0", "promise not yet used", "5.5", "20.3", "blocked"):
            self.assertIn(term, free)
        self.assertIn("122 min", output)
        self.assertIn("21:51", output)
        self.assertIn("60.0 GiB", output)
        self.assertIn("64.25", output)
        self.assertIn("32", output)
        self.assertRegex(output, r"Total:.*charged\s+18\.1 GiB.*using\s+12\.4 GiB")
        self.assertRegex(output, r"younger steps may use 9\.2 GiB of the budget until 21:59.*8 min 0 s left")
        self.assertRegex(output, r"16 waiting behind it;\s*2 would fit now")
        self.assertRegex(output, r"needs from 1\.0 to 16\.0 GiB.*oldest wait 20 min")
        final = self.line(output, "Waiting because:").lower()
        self.assertIn("budget", final)
        self.assertIn("free", final)
        self.assertIn("frees when:", final)

    def test_running_columns_align_every_text_and_numeric_cell(self) -> None:
        output = self.output()
        running_section = output[output.index("Running"):output.index("Total:")]
        rows = [line for line in running_section.splitlines() if "-session" in line or "sccache.service" in line]
        self.assertEqual(len(rows), 5)
        starts: list[list[int]] = []
        ends: list[list[int]] = []
        decimals: list[list[int]] = []
        for line in rows:
            cells = line.split("|")[1:-1]
            self.assertEqual(len(cells), 9, line)
            row_starts: list[int] = []
            row_ends: list[int] = []
            row_decimals: list[int] = []
            offset = line.index("|") + 1
            for column, cell in enumerate(cells):
                start = offset + len(cell) - len(cell.lstrip())
                row_starts.append(start)
                row_ends.append(offset + len(cell.rstrip()))
                if column == 4:
                    self.assertRegex(cell.strip(), r"^\d+ min$|^—$")
                elif column in (5, 6, 7):
                    self.assertRegex(cell.strip(), r"^\d+\.\d GiB$")
                    row_decimals.append(start + cell.strip().index("."))
                offset += len(cell) + 1
            starts.append(row_starts)
            ends.append(row_ends)
            decimals.append(row_decimals)
        for column in (4, 5, 6, 7):
            self.assertEqual(len({row[column] for row in ends}), 1, f"numeric column {column}")
        for column in (0, 1, 2, 3, 8):
            self.assertEqual(len({row[column] for row in starts}), 1, f"text column {column}")
        for column in range(3):
            self.assertEqual(len({row[column] for row in decimals}), 1, f"decimal column {column}")
        widget = next(line for line in rows if "widget-session" in line)
        for value in ("10.8 GiB", "1.0 GiB", "0.8 GiB"):
            self.assertIn(value, widget)
        self.assertNotIn("stale-widget", output)
        self.assertNotIn("p34-merge", widget)

    def test_wide_and_combining_labels_align_columns_in_terminal_cells(self) -> None:
        headers = ("Showrunner", "Unit", "Session", "Step", "Age", "Promise", "Charged", "Using now", "Charge basis")
        rows = [(label, label, label, label, "122 min", "10.8 GiB", "1.0 GiB", "0.8 GiB", label)
                for label in ("a", "界", "e\u0301")]
        table = build_why.aligned_table(headers, rows, frozenset({4, 5, 6, 7}))

        def terminal_cells(text: str) -> int:
            # These fixtures contain one known wide character and one known combining mark.
            return len(text) + text.count("界") - text.count("\u0301")

        starts: list[list[int]] = []
        ends: list[list[int]] = []
        for line in table[1:]:
            row_starts: list[int] = []
            row_ends: list[int] = []
            offset = 1
            for cell in line.split("|")[1:-1]:
                row_starts.append(terminal_cells(line[:offset]) + len(cell) - len(cell.lstrip()))
                row_ends.append(terminal_cells(line[:offset] + cell.rstrip()))
                offset += len(cell) + 1
            starts.append(row_starts)
            ends.append(row_ends)
        for column in (4, 5, 6, 7):
            self.assertEqual(len({row[column] for row in ends}), 1, f"numeric column {column}")
        for column in (0, 1, 2, 3, 8):
            self.assertEqual(len({row[column] for row in starts}), 1, f"text column {column}")

    def test_peak_decay_shows_the_promise_charge_gap_and_decay_time(self) -> None:
        _ = self.widget_path.with_suffix(".max").write_text(str(gib(10.8)))
        _ = self.widget_path.with_suffix(".wave").write_text(f"{gib(10.8)} {int(NOW - 1800)}")
        output = self.output()
        widget = next(line for line in output.splitlines() if "widget-session" in line)
        self.assertIn("10.8 GiB", widget)
        self.assertIn("0.8 GiB", widget)
        self.assertRegex(widget.lower(), r"peak decayed.*21:46")

    def test_old_sampler_decay_explains_the_twenty_five_minute_rule(self) -> None:
        _ = self.widget_path.with_suffix(".max").write_text(str(gib(10.8)))
        self.widget_path.with_suffix(".wave").unlink()
        peak_at = NOW - 1800
        os.utime(self.widget_path.with_suffix(".max"), (peak_at, peak_at))
        widget = next(line for line in self.output().splitlines() if "widget-session" in line)
        self.assertIn("old-sampler rule", widget)
        self.assertIn("25 min", widget)
        self.assertIn("10.8 GiB", widget)
        self.assertIn("0.8 GiB", widget)

    def test_need_source_records_explain_sample_window_filter_and_width(self) -> None:
        output = self.output()
        for word in ("measured", "p90", "7 runs", "14 days", "filtered", "hana_inspectors", "2 filters", "width 8"):
            self.assertIn(word, output)
        for source, runs, words in (
            ("buildlog", 12, ("build-log", "0.65", "12 runs")),
            ("pooled", 31, ("pooled", "all repos", "31 runs")),
            ("fallback", 0, ("fallback", "fewer than 5 runs")),
        ):
            with self.subTest(source=source):
                self.change_head(need_source=source, need_runs=runs)
                changed = self.output()
                for word in words:
                    self.assertIn(word, changed)

    def test_shape_need_text_names_each_tier_and_its_run_count(self) -> None:
        for basis, words in (
            (gate.ShapeP90(12), ("same-shape", "p90", "12 runs")),
            (gate.ShapeLargest(3), ("same-shape", "largest", "3 runs")),
            (gate.ShapeFallback(), ("same-profile", "fallback")),
        ):
            with self.subTest(basis=basis):
                source = gate.RecordedNeed("shape", 12 if isinstance(basis, gate.ShapeP90) else 3,
                                           14, "full", "", (), gate.WidthNotRecorded(), basis)
                text = build_why.need_text(source)
                for word in (*words, "14 days"):
                    self.assertIn(word, text)
                if not isinstance(basis, gate.ShapeP90):
                    self.assertNotIn("p90", text)

    def test_shape_need_text_names_floor_release_factor_and_budget_cap(self) -> None:
        ended_at = "2026-10-29T09:09:52Z"
        floor = gate.FailedPeakFloor(gib(15.76), ended_at)
        for tier in (
            gate.ShapeP90(10, floor, gate.ReleaseHeadroom(), gate.CappedAtBudget(gib(24))),
            gate.ShapeLargest(2, floor, gate.ReleaseHeadroom(), gate.CappedAtBudget(gib(24))),
            gate.ShapeFallback(gate.CappedAtBudget(gib(24))),
        ):
            with self.subTest(tier=tier):
                text = build_why.need_text(gate.RecordedNeed(
                    "shape", 10, 14, "full", "", (), gate.WidthNotRecorded(), tier))
                words = (("cap", "21.0 GiB", "24.0 GiB", "no headroom factor")
                         if isinstance(tier, gate.ShapeFallback) else
                         ("floor", "15.8 GiB", "05:09", "1.314", "cap", "21.0 GiB", "24.0 GiB"))
                for word in words:
                    self.assertIn(word, text)

    def test_fallback_need_text_names_its_base_and_cap_without_a_floor_or_factor(self) -> None:
        basis = gate.ShapeFallback(cap=gate.CappedAtBudget(gib(24)))
        source = gate.RecordedNeed("shape", 0, 14, "full", "", (), gate.WidthNotRecorded(), basis)
        self.assertEqual(build_why.need_text(source),
                         "same-shape fallback: no runs; heavier of fallback and same-profile builds, last 14 days"
                         + "; no headroom factor; capped at 21.0 GiB (uncapped 24.0 GiB)")

    def test_lifetime_charge_text_says_the_promise_holds_until_the_step_ends(self) -> None:
        text = build_why.charge_text(gate.LifetimePromise(16 * GIB), user_zone())
        self.assertIn("promise", text)
        self.assertIn("until the step ends", text)
        self.assertNotIn("first 2 min", text)
        self.assertEqual(build_why.charge_text(gate.RampPromise(16 * GIB), user_zone()),
                         "ramp promise: first 2 min after admission")

    def test_failed_floor_clause_uses_the_users_local_clock(self) -> None:
        basis = gate.ShapeLargest(1, gate.FailedPeakFloor(gib(15.76), "2026-10-29T02:09:52Z"))
        source = gate.RecordedNeed("shape", 1, 14, "full", "", (), gate.WidthNotRecorded(), basis)
        with mock.patch.dict(os.environ, {"USER_ZONE": "America/New_York"}):
            text = build_why.need_text(source)
        self.assertEqual(text.split("; ")[1], "failed-build floor 15.8 GiB from 22:09")

    def test_dev_shape_names_its_measured_promise_factor(self) -> None:
        source = gate.RecordedNeed("shape", 2, 14, "full", "", (), gate.WidthNotRecorded(),
                                   gate.ShapeLargest(2, factor=gate.DevHeadroom()))
        text = build_why.need_text(source)
        self.assertEqual(text.split("; ")[2], "dev promise factor × 4.203")
        self.assertNotIn("release headroom", text)

    def test_old_waiter_without_need_source_is_reported_and_not_rewritten(self) -> None:
        row = cast(dict[str, object], json.loads(self.head_path.read_text()))
        for key in ("need_source", "need_runs", "test_threads"):
            _ = row.pop(key, None)
        _ = self.head_path.write_text(json.dumps(row))
        before = self.head_path.read_bytes()
        self.assertIn("source not recorded", self.output())
        self.assertEqual(self.head_path.read_bytes(), before)

    def test_explanation_never_reads_sqlite_or_changes_admission_files(self) -> None:
        before = {path.name: path.read_bytes() for path in self.directory.iterdir()}
        output = self.output()
        with mock.patch("sqlite3.connect", side_effect=AssertionError("explain must not open SQLite")), \
             mock.patch("subprocess.run", side_effect=AssertionError("explain must not run external commands")):
            self.assertEqual(self.output(), output)
        self.assertEqual({path.name: path.read_bytes() for path in self.directory.iterdir()}, before)

    def test_no_waiter_says_nothing_waits(self) -> None:
        for path in self.directory.glob("*.waiting"):
            path.unlink()
        self.assertTrue(self.output().rstrip().endswith("Nothing waits."))

    def check_waiting_head(self, at: float, **token_times: Unpack[FinishedTokenGapTimes]) -> gate.CheckResult:
        self.reset_running(available=0, need=11.8)
        _ = self.running(800, "holder", "clippy", 1, 1, 3600)
        self.head_path = self.head_path.rename(self.directory / "500-500.waiting")
        with mock.patch.object(gate, "process_start_time", side_effect=str), \
             mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "id-inspectors-fix", "CLAUDE_SESSION_NAME": "inspectors-fix"}):
            return gate.check(self.root / "meminfo", 500, "", ["cargo", "nextest"], NOW - 122 * 60,
                              now=datetime.fromtimestamp(at, UTC),
                              profile=gate.StepProfile("hana", "nextest", "inspectors-fix",
                                                       gate.ExpectedPeak(gib(11.8), "measured", 7)),
                              **token_times)

    def test_admission_written_head_time_reaches_build_why_and_queue(self) -> None:
        started_head = NOW - 125
        result = self.check_waiting_head(started_head)
        self.assertEqual(result.decision.state, "hold")
        self.assertEqual(json.loads(self.head_path.read_text())["head_since"], started_head)
        self.assertIn("Head for: 2 min (since 21:48:55)", self.output())
        reading = gate.locked_queue_read()
        facts = head_facts.read_head_facts(reading)
        assert isinstance(facts, head_facts.HeadFacts)
        text = head_facts.head_time_text(facts, user_zone(), short=True)
        self.assertIn("head for 2 min", text)
        self.assertNotIn("head for 122 min", text)

    def test_admission_written_token_gap_matches_the_reader_and_both_commands(self) -> None:
        result = self.check_waiting_head(NOW, token_left_at=NOW - 200, token_back_at=NOW - 46)
        self.assertEqual(result.decision.state, "hold")
        facts = head_facts.read_head_facts(gate.locked_queue_read())
        self.assertIsInstance(facts, head_facts.HeadFacts)
        if not isinstance(facts, head_facts.HeadFacts):
            self.fail("the held step must have head facts")
        waits = facts.token_waits
        self.assertIsInstance(waits, head_facts.RecordedTokenWaits)
        if not isinstance(waits, head_facts.RecordedTokenWaits):
            self.fail("admission's token gap must match its waiting step")
        self.assertEqual([(wait.session_id, wait.pid, wait.start_time, wait.left_at, wait.returned.at)
                          for wait in waits.head_waits], [("id-inspectors-fix", 500, "500", NOW - 200, NOW - 46)])
        self.assertIn("Cargo token: inspectors-fix-session nextest: waited 154 s (21:47-21:50)", self.output())
        self.assertIn("cargo token waited 154 s", head_facts.cargo_token_text(facts))

    def test_four_head_facts_follow_first_in_line_in_the_specified_order(self) -> None:
        write_bumps(NOW, [bump_line(NOW - 1)])
        output = self.output()
        prefixes = ("First in line:", "Head for:", "Bumps since it joined at",
                    "Order rule:", "Cargo token waits:")
        positions = [output.index(prefix) for prefix in prefixes]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(self.line(output, "Head for:"), "Head for: unknown")
        joined = datetime.fromtimestamp(NOW - 122 * 60, user_zone()).strftime("%H:%M:%S")
        self.assertEqual(self.line(output, "Bumps since it joined"),
                         f"Bumps since it joined at {joined}: 1")
        self.assertEqual(self.line(output, "Cargo token waits:"), "Cargo token waits: unknown")

    def test_token_gaps_only_show_current_waiting_runs(self) -> None:
        since = NOW - 122 * 60
        write_token_gaps([
            token_gap_line(since - 100, since + 10, session="id-inspectors-fix", pid=500,
                           start_time="500"),
            token_gap_line(NOW - 600, NOW - 500, session="id-widget", pid=100, start_time="100"),
            token_gap_line(NOW - 400, NOW - 300, session="", step="clippy", pid=500, start_time="500"),
            token_gap_line(NOW - 200, NOW - 46, session="id-inspectors-fix", pid=500, start_time="500"),
        ])
        lines = [line.strip() for line in self.output().splitlines() if line.strip().startswith("Cargo token:")]
        self.assertEqual(lines, ["Cargo token: inspectors-fix-session nextest: waited 154 s (21:47-21:50)"])

    def test_token_record_without_matching_waits_prints_none(self) -> None:
        for lines in ([queue_time_line(NOW - 20)],
                      [token_gap_line(NOW - 200, NOW - 46, session="unrelated", pid=999)]):
            with self.subTest(lines=lines):
                write_token_gaps(lines)
                output = self.output()
                self.assertEqual(self.line(output, "Cargo token waits:"), "Cargo token waits: none")
                self.assertFalse(any(line.strip().startswith("Cargo token:") for line in output.splitlines()))

    def test_bump_table_caps_at_eight_groups_and_right_aligns_bumps(self) -> None:
        lines: list[bytes] = []
        for index, count in enumerate((12, 10, 8, 7, 6, 5, 4, 3, 2, 1)):
            for number in range(count):
                lines.append(bump_line(NOW - 300 + len(lines), session=f"bumped-{index}",
                                       step=f"step-{index}", folder=f"/tmp/folder-{index}",
                                       actor=f"actor-{number % 2}"))
        write_bumps(NOW, lines)
        output = self.output()
        section = output[output.index("Bumps since it joined"):output.index("Order rule:")]
        rows = [line for line in section.splitlines() if line.startswith("|")]
        self.assertEqual([cell.strip() for cell in rows[0].split("|")[1:-1]],
                         ["Step", "Session", "Folder", "Bumps", "By", "Last bump"])
        self.assertEqual(len(rows), 9)
        self.assertEqual([line.split("|")[4].strip() for line in rows[1:]],
                         ["12", "10", "8", "7", "6", "5", "4", "3"])
        self.assertEqual(len({len(line.rsplit("|", 3)[0].rstrip()) for line in rows[1:]}), 1)
        for index in range(8):
            self.assertIn(f"step-{index}", section)
        self.assertNotIn("step-8", section)
        self.assertNotIn("step-9", section)
        self.assertIn("and 2 more steps with 3 bumps", section)
        self.assertNotIn("this step", section.lower())

    def test_single_label_read_includes_bump_actor_and_bumped_step_sessions(self) -> None:
        write_bumps(NOW, [bump_line(NOW - 3, actor="actor-one", session="bumped-one"),
                          bump_line(NOW - 2, actor="actor-two", session="bumped-two")])
        with mock.patch.object(roster, "unit_labels_for", return_value={}) as labels, \
             mock.patch.object(roster, "roster", side_effect=AssertionError("no full roster")), \
             mock.patch.object(gate, "queue_read", wraps=gate.queue_read) as reading, \
             mock.patch.object(head_facts, "read_head_facts", wraps=head_facts.read_head_facts) as facts:
            output = self.output()
        labels.assert_called_once()
        requested = set(cast(list[str], labels.call_args_list[0].args[0]))
        self.assertTrue({"actor-one", "actor-two", "bumped-one", "bumped-two", "id-inspectors-fix"}
                        <= requested)
        self.assertIn("actor-one", output)
        self.assertIn("bumped-two", output)
        reading.assert_called_once_with()
        facts.assert_called_once()

    def test_bump_read_failure_prints_unknown_and_preserves_success_exit(self) -> None:
        gate.store.host_file(gate.store.host_name(), NOW).mkdir(parents=True)
        output = self.output()
        self.assertRegex(self.line(output, "Bumps since it joined:"),
                         r"^Bumps since it joined: unknown \(.+\)$")
        self.assertNotIn("the admission state could not be read", output)
        self.assertIn("First in line:", output)

    def test_order_rule_names_no_room_when_head_uses_all_the_budget(self) -> None:
        for need in (21, 22):
            with self.subTest(need=need):
                self.change_head(need=need * GIB)
                line = self.line(self.output(), "Order rule:")
                self.assertIn("younger steps wait until 21:59", line)
                self.assertNotIn("may use", line)

    def test_backfill_and_position_bump_report_the_admission_deadlines(self) -> None:
        self.change_head(priority_since=NOW - 630, bump_order=-1)
        output = self.output()
        self.assertRegex(output, r"younger steps may start until 21:52.*1 min 30 s left")
        self.assertIn("bumped by position at 21:40", output)

    def test_pressure_hold_and_recovery_spacing_are_explicit(self) -> None:
        _ = (self.root / "pressure").write_text("full avg10=11.00 avg60=0.00 avg300=0.00 total=0\n")
        pressure = self.output()
        self.assertRegex(pressure.lower(), r"pressure.*11(?:\.0)?")
        self.assertIn("pressure", self.line(pressure, "Waiting because:").lower())
        _ = (self.root / "pressure").write_text("full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n")
        _ = (self.directory / "pressure-recovery").write_text(json.dumps({
            "state": "recovering", "ends_at": NOW + 120, "next_start_at": NOW + 30,
        }))
        recovery = self.output().lower()
        self.assertIn("recovery", recovery)
        self.assertIn("21:51", recovery)

    def test_missing_or_corrupt_snapshot_with_held_lock_answers_busy_in_under_a_second(self) -> None:
        for content in (None, "{"):
            with self.subTest(content=content):
                path = self.directory / "queue.json"
                if content is None:
                    path.unlink(missing_ok=True)
                else:
                    _ = path.write_text(content)
                output, errors = io.StringIO(), io.StringIO()
                with (self.directory / "lock").open("a+") as lock, \
                     redirect_stdout(output), redirect_stderr(errors), mock.patch("sys.argv", ["build_why.py"]):
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    started = time.perf_counter()
                    status = build_why.main()
                    elapsed = time.perf_counter() - started
                self.assertLess(elapsed, 1.0)
                self.assertEqual((status, output.getvalue(), errors.getvalue()),
                                 (0, "queue busy; try again\n", ""))

    def test_read_failure_is_one_fixed_line_with_exit_two(self) -> None:
        for error in (OSError("cannot read ledger"), ValueError("invalid ledger"), TimeoutError("lock busy")):
            with self.subTest(error=type(error).__name__):
                output, errors = io.StringIO(), io.StringIO()
                with mock.patch.object(gate, "queue_read", side_effect=error), \
                     redirect_stdout(output), redirect_stderr(errors), mock.patch("sys.argv", ["build_why.py"]):
                    result = build_why.main()
                self.assertEqual(result, 2)
                self.assertEqual(output.getvalue() + errors.getvalue(),
                                 "build-why: the admission state could not be read.\n")


if __name__ == "__main__":
    _ = unittest.main()
