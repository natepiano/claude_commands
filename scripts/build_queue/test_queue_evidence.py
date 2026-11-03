"""Recorded evidence explains the queue without another admission decision."""

from __future__ import annotations

from scripts.build_queue.admission_test_support import AdmissionFixture
from scripts.build_queue import admission_client as client

from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from dataclasses import replace
import io
import json
import os
import re
from pathlib import Path
import tempfile
import time
import unittest
from typing import TypedDict, Unpack, cast, override
from unittest import mock

from scripts.build_queue import queue_evidence, head_facts, build_queue, queue_card, queue_view
from scripts.build_queue.queue_lines import Line, plain
from scripts.build_queue.queue_fixture import write_process, fixture_queue_read, write_recovery_fixture
from scripts.build_queue.queue_fixture import bump_line, queue_time_line, token_gap_line, write_bumps, write_token_gaps
from scripts.lint import memory_admit as gate
from scripts.build_queue.queue_fixture import write_memory_config
from scripts.lint.memory_test_support import commit_config_fixture
from scripts.lib.user_zone import user_zone
from scripts.message import roster, unit_labels

GIB = gate.GIB
NOW = datetime(2026, 10, 30, 1, 51, tzinfo=UTC).timestamp()


def gib(value: float) -> int:
    return int(value * GIB)


class FinishedTokenGapTimes(TypedDict):
    token_left_at: float
    token_back_at: float


class QueueEvidenceTests(unittest.TestCase):
    root: Path = Path()
    directory: Path = Path()
    proc: Path = Path()
    head_path: Path = Path()
    widget_path: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        config = self.root / "lint.conf"
        write_memory_config(config)
        self.directory = self.root / "buildlog/admission"
        self.directory.mkdir(parents=True)
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
            "CONF_DIR": str(config.parent),
        }))
        _ = self.enterContext(mock.patch.object(gate, "queue_read", side_effect=lambda:
            fixture_queue_read(self.directory, gate.process_table(self.proc), NOW)))
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
                         filters=["test(one)", "test(two)"],
                         priority_since=NOW - 120)
        for number in range(16):
            _ = self.waiter(600 + number, f"younger-{number}", number + 1, 20 - number)
        entries = [self.entry(name) for name in ("widget", "demo", "organon", "frame-cost", "inspectors-fix")]
        labels = roster.unit_labels(entries)
        _ = self.enterContext(mock.patch.object(unit_labels, "unit_labels_for", return_value=labels))

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

    def output(self, selector: str = "1") -> str:
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors), mock.patch("sys.argv", ["build_queue.py", "--columns", "400", selector]):
            result = build_queue.main()
        self.assertEqual(result, 0, errors.getvalue())
        self.assertEqual(errors.getvalue(), "")
        return output.getvalue()

    def line(self, output: str, prefix: str) -> str:
        return next(line.strip().removeprefix("✓ ").removeprefix("✗ ") for line in output.splitlines()
                    if line.strip().removeprefix("✓ ").removeprefix("✗ ").startswith(prefix))

    def reset_running(self, *, available: float, need: float) -> None:
        for pattern in ("*.reservation", "*.max", "*.wave"):
            for path in self.directory.glob(pattern):
                path.unlink()
        _ = (self.root / "builds.slice/sccache.service/memory.stat").write_text("anon 0\nfile 0\n")
        _ = (self.root / "meminfo").write_text(
            f"MemAvailable: {gib(available) // 1024} kB\nMemTotal: {60 * GIB // 1024} kB\n")
        self.change_head(need=gib(need))

    def test_pressure_formatters_keep_the_recorded_hold_condition(self) -> None:
        pressure_path = self.root / "pressure"
        for full, some, swap, detail in (
            (0, 30, 90, "swap"),
            (8, 0, 0, "full avg10"),
            (8, 0, 90, "full avg10"),
        ):
            with self.subTest(full=full, some=some, swap=swap):
                _ = pressure_path.write_text(
                    f"some avg10={some}.00 avg60=0.00 avg300=0.00 total=0\n"
                    + f"full avg10={full}.00 avg60=0.00 avg300=0.00 total=0\n")
                host = gate.HostMemory(20 * GIB, 60 * GIB, 100 * GIB, (100 - swap) * GIB)
                policy = gate.DEFAULT_ADMISSION_POLICY
                pressure = gate.read_pressure(host, policy=policy)
                assert isinstance(pressure, gate.MemoryPressure)
                changed = replace(policy, pressure=replace(policy.pressure,
                    full_avg10_limit=100, swap_used_limit=100, some_avg10_limit=100))
                memory_text = gate.pressure_text(pressure, changed)
                evidence = queue_evidence.pressure_text(pressure, user_zone(), changed)
                if detail == "swap":
                    self.assertIn("swap", memory_text)
                    self.assertIn("starts when swap", evidence)
                    self.assertNotIn("starts below full", evidence)
                else:
                    self.assertIn("full avg10", memory_text)
                    self.assertIn("starts below full", evidence)

    def test_budget_text_preserves_pass_block_and_run_alone_arithmetic(self) -> None:
        for check, expected in (
            (gate.BudgetCheck(4 * GIB, 6 * GIB, 12 * GIB, 6 * GIB, True),
             "Build budget: charged 4.0 + need 6.0 = 10.0 GiB <= 12.0 GiB: pass; starts when charged falls to 6.0 GiB"),
            (gate.BudgetCheck(8 * GIB, 6 * GIB, 12 * GIB, 6 * GIB, False),
             "Build budget: charged 8.0 + need 6.0 = 14.0 GiB > 12.0 GiB: blocked; starts when charged falls to 6.0 GiB"),
            (gate.BudgetCheck(0, 14 * GIB, 12 * GIB, 0, True),
             "Build budget: charged 0.0 + need 14.0 = 14.0 GiB > 12.0 GiB: pass; run-alone exception; starts when charged falls to 0.0 GiB"),
        ):
            with self.subTest(check=check):
                self.assertEqual(queue_evidence.budget_text(check), expected)

    def test_free_text_preserves_pass_and_blocked_arithmetic(self) -> None:
        for available, passes, relation, result in ((12, True, ">=", "pass"), (10, False, "<", "blocked")):
            with self.subTest(available=available):
                check = gate.FreeCheck(available * GIB, 6 * GIB, 3 * GIB, 2 * GIB, 11 * GIB, passes)
                self.assertEqual(queue_evidence.free_text(check),
                    f"Free memory: free {available:.1f} GiB {relation} need 6.0 + earlyoom 3.0 + promise not yet used 2.0 = 11.0 GiB: {result}")

    def test_capped_need_uses_the_reply_budget_without_configuration_reads(self) -> None:
        source = gate.RecordedNeed("shape", 10, 14, "full", "", (), gate.WidthNotRecorded(),
            gate.ShapeP90(10, gate.FailedPeakFloor(16 * GIB, "2026-10-30T01:40:00Z"),
                          gate.ReleaseHeadroom(), gate.CappedAtBudget(30 * GIB)))
        zone = user_zone()
        with mock.patch.object(gate, "build_budget_bytes", side_effect=AssertionError("live budget")), \
             mock.patch.object(gate, "memory_config", side_effect=AssertionError("live policy")), \
             mock.patch.object(gate, "read_memory_config", side_effect=AssertionError("live policy")), \
             mock.patch("conf.read", side_effect=AssertionError("live configuration")):
            text = queue_evidence.need_text(source, budget=19 * GIB, zone=zone)
        for value in ("19.0 GiB", "30.0 GiB", "16.0 GiB", "1.314", "14 days"):
            self.assertIn(value, text)
        self.assertNotIn("21.0 GiB", text)

    def test_full_package_estimates_keep_package_and_width_for_every_basis(self) -> None:
        for basis in (gate.BasisNotRecorded(), gate.ShapeP90(7), gate.ShapeLargest(2),
                      gate.ShapeFallback(), gate.ProgramMemoryCap("merge-gate")):
            with self.subTest(basis=basis):
                self.change_head(test_scope="full", package="hana_widget", filters=[],
                    test_threads=12, need_basis=gate.need_basis_record(basis))
                text = self.output()
                self.assertIn("Package: hana_widget; filters: none; width: 12; scope: full", text)
        self.change_head(package="", test_threads=None)
        self.assertIn("width: not recorded", self.output())

    def test_unknown_admission_time_keeps_the_running_charge_and_basis(self) -> None:
        reading = gate.queue_read()
        running = replace(reading.explanation.running[0], admitted_at=gate.AdmissionTimeNotRecorded(),
                          basis=gate.LifetimePromise(6 * GIB), promise=6 * GIB, charged=6 * GIB)
        text = queue_evidence.running_text(running, reading.explanation, {}, user_zone())
        self.assertIn("admission time unknown", text)
        self.assertIn("promise 6.0 GiB, charge 6.0 GiB", text)
        self.assertIn("holds until the step ends", text)
        self.assertNotIn("admitted 00:00", text)

    def test_optional_load_failure_preserves_evidence_and_release_advice(self) -> None:
        load = self.proc / "loadavg"
        for unreadable in (False, True):
            with self.subTest(unreadable=unreadable):
                load.unlink(missing_ok=True)
                if unreadable:
                    load.mkdir()
                try:
                    text = self.output()
                finally:
                    if unreadable:
                        load.rmdir()
                self.assertIn("host load: unavailable", text.lower())
                self.assertIn("measured process-memory p90 of 7 runs", text)
                self.assertIn("Frees when:", text)

    def test_renderers_use_model_inputs_without_load_reads_or_release_search(self) -> None:
        render = queue_card.render_selection

        def render_from_inputs(model: queue_view.QueueModel, selector: str, columns: int) -> list[Line]:
            with mock.patch.object(Path, "read_text", side_effect=AssertionError("renderer reads a file")), \
                 mock.patch.object(gate, "smallest_head_release", side_effect=AssertionError("renderer searches admission")):
                return render(model, selector, columns)

        with mock.patch.object(queue_card, "render_selection", side_effect=render_from_inputs):
            text = self.output()
        self.assertIn("Host load: 64.25", text)
        self.assertIn("Frees when:", text)

    def test_page_uses_one_read_history_and_labels_without_recalculating_admission(self) -> None:
        reading = gate.queue_read()
        release = gate.smallest_head_release(reading.explanation)
        with mock.patch.object(gate, "smallest_head_release", return_value=release) as release_read, \
             mock.patch.object(Path, "read_text", autospec=True, side_effect=Path.read_text) as load_read, \
             mock.patch.object(gate, "queue_read", return_value=reading) as read, \
             mock.patch.object(head_facts, "read_head_facts", wraps=head_facts.read_head_facts) as facts, \
             mock.patch.object(unit_labels, "unit_labels_for", return_value={}) as labels, \
             mock.patch.object(gate, "decide", side_effect=AssertionError("render recalculated admission")), \
             mock.patch.object(gate, "memory_config", side_effect=AssertionError("render read policy")):
            text = self.output()
        read.assert_called_once_with()
        release_read.assert_called_once_with(reading.explanation)
        self.assertEqual(sum(cast(tuple[object, ...], call.args)[0] == self.proc / "loadavg"
                             for call in load_read.call_args_list), 1)
        facts.assert_called_once_with(reading)
        labels.assert_called_once()
        self.assertEqual(text.count("Frees when:"), 1)
        self.assertEqual(text.count("measured process-memory p90 of 7 runs"), 1)
        self.assertEqual(text.count("Cargo token waits:"), 1)
        self.assertEqual(text.count("Host load: 64.25"), 1)
        self.assertEqual(text.count("#1 of 17"), 1)

    def test_test_phase_charge_explains_the_mark_need_history_width_and_margin(self) -> None:
        need = gate.TestPhaseNeed(2 * GIB, 7, 8, 12, "wider_unscaled", 1.5)
        charge = gate.TestPhaseCharge(gib(3), gib(0.4), need, NOW - 60)
        self.assertEqual(queue_evidence.charge_text(charge, user_zone(), policy=gate.DEFAULT_ADMISSION_POLICY, step="nextest"),
                         "tests since 21:50: peak so far 0.4 GiB, need 2.0 GiB "
                         + "(wider_unscaled, p90 of 7 runs at width 12), × 1.5")

    def test_expired_head_explains_refresh_even_when_all_resource_checks_pass(self) -> None:
        self.reset_running(available=60, need=1)
        self.change_head(estimated_at=NOW - gate.DEFAULT_ADMISSION_POLICY.timing.head_seconds)
        text = self.output()
        self.assertIn("the estimate expired at 21:41; the client refreshes it through the admission service", text)
        explanation = gate.explain_queue()
        assert isinstance(explanation.head, gate.HeadExplanation)
        self.assertTrue(explanation.head.budget.passes)
        self.assertTrue(explanation.head.free.passes)

    def test_expired_head_release_advice_requires_refresh_instead_of_queue_order_or_a_start_time(self) -> None:
        self.reset_running(available=40, need=1)
        _ = self.running(800, "holder", "clippy", 1, 1, 3600)
        expired_at = NOW - gate.DEFAULT_ADMISSION_POLICY.timing.head_seconds
        self.change_head(estimated_at=expired_at)
        for recovering in (False, True):
            with self.subTest(recovering=recovering):
                if recovering:
                    write_recovery_fixture(self.directory, gate.RecoveryStarts(NOW + 120, NOW + 37))
                explanation = gate.explain_queue()
                assert isinstance(explanation.head, gate.HeadExplanation)
                self.assertTrue(explanation.head.budget.passes)
                self.assertTrue(explanation.head.free.passes)
                for outcome in (gate.head_after_steps_end(explanation, ()),
                                gate.head_after_steps_end(explanation, (0,)),
                                gate.smallest_head_release(explanation)):
                    self.assertEqual(outcome, gate.EstimateExpired(expired_at))
                final = self.line(self.output(), "Frees when:")
                frees_when = final.partition("Frees when: ")[2]
                self.assertIn("the estimate expired at 21:41; the client refreshes it through the admission service", frees_when)
                self.assertNotIn("queue order", frees_when)
                self.assertNotIn("recovery spacing starts it at", frees_when)

    def test_release_advice_uses_admissions_run_alone_exception(self) -> None:
        self.reset_running(available=40, need=22)
        _ = self.running(800, "only-holder", "clippy", 1, 1, 3600)
        output = self.output()
        final = self.line(output, "Frees when:")
        self.assertIn("stale-only-holder clippy end", final)
        self.assertNotIn("insufficient", final)
        self.assertNotIn("still leaves", final)
        result = gate.head_after_steps_end(gate.explain_queue(), (0,))
        self.assertIsInstance(result, gate.HeadAdmitsAfterEnd)

    def test_order_rule_shows_the_queue_priority_from_its_snapshot(self) -> None:
        from dataclasses import replace
        reading = gate.queue_read()
        priority: gate.QueuePriority = {"unit": "resources", "why": "work", "set_at": NOW,
            "set_by": "showrunner"}
        reading = replace(reading, explanation=replace(reading.explanation, priority=priority))
        facts = head_facts.read_head_facts(reading)
        model = queue_view.QueueModel(reading, {}, facts, head_facts.PaceUnavailable(), user_zone(), queue_view.HostLoadUnavailable(),
            gate.smallest_head_release(reading.explanation))
        self.assertIn("queue priority: resources (the user's bump)",
                      plain(queue_view.render_view(model, 400)))

    def test_override_free_memory_terms_add_up_to_its_printed_threshold(self) -> None:
        self.reset_running(available=23, need=12)
        _ = self.running(800, "growing", "clippy", 20, 1, 30)
        head_path, head = gate.queue_read().ordered[0]
        override: gate.HeadAdmissionOverride = {
            "waiter": head_path.name, "pid": head["pid"], "start_time": head["start_time"],
            "unit": "fixture", "step": head.get("step", "nextest"), "why": "arithmetic regression",
            "set_by": "showrunner-id", "need": head["need"], "free": 23 * GIB, "reserve": 3 * GIB,
        }
        _ = gate.head_override_path(self.directory).write_text(json.dumps(override))
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
        final = self.line(self.output(), "Frees when:")
        self.assertIn("stale-useful-release clippy end", final)
        self.assertNotIn("large-peak", final)
        self.assertIn("budget charge drops 9.0 GiB", final)

    def test_release_ties_choose_least_charge_then_running_table_order(self) -> None:
        self.reset_running(available=40, need=14)
        for pid, name, charge in ((800, "larger", 8), (801, "first-small", 6), (802, "second-small", 6)):
            _ = self.running(pid, name, "clippy", charge, charge, 3600)
        final = self.line(self.output(), "Frees when:")
        self.assertIn("stale-larger clippy + stale-first-small clippy end", final)
        self.assertNotIn("second-small", final)

    def test_equal_size_releases_choose_the_least_charge_even_later_in_the_table(self) -> None:
        self.reset_running(available=40, need=6)
        for pid, name, charge in ((800, "largest", 8), (801, "medium", 7),
                                  (802, "small-a", 6), (803, "small-b", 6)):
            _ = self.running(pid, name, "clippy", charge, charge, 3600)
        final = self.line(self.output(), "Frees when:")
        self.assertIn("stale-small-a clippy + stale-small-b clippy end", final)
        self.assertNotIn("largest", final)
        self.assertNotIn("medium", final)
        self.assertIn("budget charge drops 12.0 GiB", final)

    def test_recovery_spacing_names_its_start_in_the_display_zone_without_endings(self) -> None:
        self.reset_running(available=40, need=4)
        _ = self.running(800, "holder", "clippy", 6, 6, 3600)
        write_recovery_fixture(self.directory, gate.RecoveryStarts(NOW + 120, NOW + 37))
        for zone, expected in (("America/New_York", "21:51:37 EDT"), ("UTC", "01:51:37 UTC")):
            with self.subTest(zone=zone), mock.patch.dict(os.environ, {"USER_ZONE": zone}):
                final = self.line(self.output(), "Frees when:")
                self.assertIn("no running step needs to end; recovery spacing starts it at " + expected, final)
                self.assertNotIn("still leaves pressure", final)

    def test_recovery_spacing_names_the_required_endings_before_its_start(self) -> None:
        self.reset_running(available=40, need=4)
        _ = self.running(800, "holder", "clippy", 20, 20, 3600)
        write_recovery_fixture(self.directory, gate.RecoveryStarts(NOW + 120, NOW + 37))
        final = self.line(self.output(), "Frees when:")
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
                write_recovery_fixture(self.directory, gate.RecoveryStarts(NOW + 120, NOW + 37))
                final = self.line(self.output(), "Frees when:")
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
        final = self.line(self.output(), "Frees when:")
        self.assertIn("still leaves pressure holding the head", final)

    def test_twenty_running_steps_explain_and_render_in_under_one_second(self) -> None:
        self.reset_running(available=15, need=18)
        # Charge and free-memory constraints favor different halves of the running list.
        for index in range(20):
            charge, used = (10.0, 0.1) if index < 10 else (1.0, 1.0)
            _ = self.running(800 + index, f"holder-{index}", "clippy", charge, used, 3600, peak=charge)
        started = time.perf_counter()
        reading = gate.queue_read()
        explanation = reading.explanation
        model = queue_view.QueueModel(reading, {}, head_facts.read_head_facts(reading),
            head_facts.PaceUnavailable(), user_zone(), queue_view.HostLoadUnavailable(),
            gate.smallest_head_release(reading.explanation))
        lines = plain(queue_card.render_selection(model, "1", 400)).splitlines()
        elapsed = time.perf_counter() - started
        self.assertEqual(len(explanation.running), 20)
        final = " ".join("\n".join(lines).partition("Frees when:")[2].split())
        self.assertNotIn("still leaves", final)
        self.assertIn("end (budget charge drops", final)
        self.assertLess(elapsed, 1.0, f"20-step explanation and render took {elapsed:.6f} s")

    def test_tonight_arithmetic_matches_the_actual_admission_decision(self) -> None:
        reading = gate.queue_read()
        inputs = reading.explanation.decided_from
        assert isinstance(inputs, gate.QueueInputs)
        view, ordered = reading.view, inputs.ordered
        reason = view.waiting[0].reason
        self.assertIsInstance(reason, gate.HeadAdmissionStatus)
        if not isinstance(reason, gate.HeadAdmissionStatus):
            self.fail(f"expected head admission status, got {reason}")
        running = [row.measured for row in view.running]
        head = gate.head_priority([row for _, row in ordered], view.available, 60 * GIB, running, NOW, policy=gate.DEFAULT_ADMISSION_POLICY)
        decision = gate.admission_walk(view.available, 60 * GIB, [row for _, row in ordered], running, head,
                                      gate.NoPressureHold(), recovery=gate.NoRecoverySpacing(), now=NOW, policy=gate.DEFAULT_ADMISSION_POLICY)[0].decision
        self.assertEqual(reason.decision, decision)
        self.assertEqual(decision.state, "hold")
        self.assertAlmostEqual(decision.charged / GIB, 18.1, places=5)
        self.assertAlmostEqual(decision.promised / GIB, 5.5, places=5)
        self.assertAlmostEqual(decision.threshold / GIB, 20.3, places=5)

        output = self.output()
        for section in ("Running", "#1 of 17", "Build budget:", "Free memory:", "Frees when:"):
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
        self.assertIn("Total current use: 12.4 GiB", output)
        self.assertIn("charged 18.1", output)
        self.assertRegex(output, r"younger steps may use 9\.2 GiB of the budget until 21:59.*8 min 0 s left")
        self.assertRegex(output, r"Followers: 16 waiting;\s*2 fit now")
        self.assertRegex(output, r"needs from 1\.0 GiB to 16\.0 GiB.*oldest waiting 20 min")
        final = self.line(output, "Frees when:").lower()
        self.assertIn("budget charge drops", final)
        self.assertIn("free memory rises", final)
        self.assertIn("frees when:", final)

    def test_running_records_keep_every_identity_time_and_memory_value(self) -> None:
        output = self.output()
        running_section = output[output.index("Running:"):output.index("Total current use:")]
        rows = [line for line in running_section.splitlines() if "; promise " in line]
        self.assertEqual(len(rows), 5)
        expected = (
            ("widget", "nextest", "20:51 (60 min ago)", "10.8", "1.0", "0.8", "compiling"),
            ("demo", "nextest", "21:31 (20 min ago)", "1.0", "3.8", "3.8", "compiling"),
            ("organon", "clippy", "21:50 (0 min ago)", "6.1", "6.1", "1.3", "ramp promise"),
            ("frame-cost", "clippy", "21:50 (1 min ago)", "6.8", "6.8", "6.1", "ramp promise"),
        )
        for name, step, admitted, promise, charge, used, basis in expected:
            row, = [line for line in rows if f"{name}-session," in line]
            self.assertIn(f"hana / {name} / {name}-session, {step}: admitted {admitted}", row)
            self.assertIn(f"promise {promise} GiB, charge {charge} GiB, using now {used} GiB", row)
            self.assertIn(basis, row)
        compiler, = [line for line in rows if "sccache.service" in line]
        self.assertIn("admission time unknown", compiler)
        self.assertIn("promise 0.0 GiB, charge 0.4 GiB, using now 0.4 GiB", compiler)
        self.assertNotIn("stale-widget", output)
        self.assertNotIn("p34-merge", running_section)

    def test_peak_decay_shows_the_promise_charge_gap_and_decay_time(self) -> None:
        _ = self.widget_path.with_suffix(".max").write_text(str(gib(10.8)))
        _ = self.widget_path.with_suffix(".wave").write_text(f"{gib(10.8)} {int(NOW - 1800)}")
        output = self.output()
        widget = next(line for line in output.splitlines() if "widget-session" in line and "promise" in line)
        self.assertIn("10.8 GiB", widget)
        self.assertIn("0.8 GiB", widget)
        self.assertRegex(widget.lower(), r"peak decayed.*21:46")

    def test_old_sampler_decay_explains_the_twenty_five_minute_rule(self) -> None:
        _ = self.widget_path.with_suffix(".max").write_text(str(gib(10.8)))
        self.widget_path.with_suffix(".wave").unlink()
        peak_at = NOW - 1800
        os.utime(self.widget_path.with_suffix(".max"), (peak_at, peak_at))
        widget = next(line for line in self.output().splitlines() if "widget-session" in line and "promise" in line)
        self.assertIn("old-sampler rule", widget)
        self.assertIn("25 min", widget)
        self.assertIn("10.8 GiB", widget)
        self.assertIn("0.8 GiB", widget)

    def test_need_source_records_explain_sample_window_filter_and_width(self) -> None:
        output = self.output()
        for word in ("measured", "p90", "7 runs", "14 days", "filtered", "hana_inspectors", "filters: test(one), test(two)", "width: 8"):
            self.assertIn(word, output)
        details = self.line(output, "Package:")
        self.assertEqual(len(details.partition("filters: ")[2].partition(";")[0].split(", ")), 2)
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
                text = queue_evidence.need_text(source, budget=21 * GIB, zone=user_zone())
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
                text = queue_evidence.need_text(gate.RecordedNeed(
                    "shape", 10, 14, "full", "", (), gate.WidthNotRecorded(), tier), budget=21 * GIB, zone=user_zone())
                words = (("cap", "21.0 GiB", "24.0 GiB", "no headroom factor")
                         if isinstance(tier, gate.ShapeFallback) else
                         ("floor", "15.8 GiB", "05:09", "1.314", "cap", "21.0 GiB", "24.0 GiB"))
                for word in words:
                    self.assertIn(word, text)

    def test_fallback_need_text_names_its_base_and_cap_without_a_floor_or_factor(self) -> None:
        basis = gate.ShapeFallback(cap=gate.CappedAtBudget(gib(24)))
        source = gate.RecordedNeed("shape", 0, 14, "full", "", (), gate.WidthNotRecorded(), basis)
        self.assertEqual(queue_evidence.need_text(source, budget=21 * GIB, zone=user_zone()),
                         "same-shape fallback: no runs; heavier of fallback and same-profile builds, last 14 days"
                         + "; no headroom factor; capped at 21.0 GiB (uncapped 24.0 GiB)")

    def test_cap_need_text_uses_the_program_in_the_recorded_basis(self) -> None:
        for program, words in (("merge-gate", "the merge gate's memory cap"),
                               ("rust-analyzer", "rust-analyzer's memory cap")):
            with self.subTest(program=program):
                basis = gate.read_need_basis(gate.need_basis_record(gate.ProgramMemoryCap(program)))
                source = gate.RecordedNeed("cap", 0, 14, "full", "", (), gate.WidthNotRecorded(), basis)
                self.assertEqual(queue_evidence.need_text(source, budget=21 * GIB, zone=user_zone()), words)

    def test_gate_cap_need_and_charge_text_stay_the_same_after_head_seconds(self) -> None:
        self.reset_running(available=40, need=20)
        path = self.running(700, "gate", "merge-gate", 6, 1,
                            int(gate.DEFAULT_ADMISSION_POLICY.timing.ramp_seconds + 1), peak=5)
        row = cast(dict[str, object], json.loads(path.read_text()))
        row["queue_waiter"] = {"need_source": "cap"}
        _ = path.write_text(json.dumps(row))
        self.change_head(step="merge-gate", need_source="cap", need_runs=0,
                         need_basis=gate.need_basis_record(gate.ProgramMemoryCap("merge-gate")))
        lines: list[tuple[str, str]] = []
        for elapsed in (0, gate.DEFAULT_ADMISSION_POLICY.timing.head_seconds + 1):
            with mock.patch.object(time, "time", return_value=NOW + elapsed):
                reading = gate.queue_read()
            assert isinstance(reading, gate.QueueRead)
            head = reading.explanation.head
            assert isinstance(head, gate.HeadExplanation)
            running, = reading.explanation.running
            lines.append((queue_evidence.need_text(head.need_source, budget=reading.explanation.limits.budget, zone=user_zone()), queue_evidence.charge_text(
                running.basis, user_zone(), reading.explanation.policy, running.step.step)))
            self.assertEqual(running.charged, 6 * GIB)
        self.assertEqual(lines[0], lines[1])
        self.assertEqual(lines[0], ("the merge gate's memory cap", "merge gate promise: holds until the step ends"))

    def test_lifetime_charge_text_names_the_merge_gate_and_release_build(self) -> None:
        for step, name in (("merge-gate", "merge gate"), ("build", "release build")):
            with self.subTest(step=step):
                text = queue_evidence.charge_text(gate.LifetimePromise(6 * GIB), user_zone(),
                                             gate.DEFAULT_ADMISSION_POLICY, step)
                self.assertEqual(text, f"{name} promise: holds until the step ends")

    def test_aged_cap_waiter_has_no_refresh_text_but_history_waiter_does(self) -> None:
        expired_at = NOW - gate.DEFAULT_ADMISSION_POLICY.timing.head_seconds - 1
        for sizing in (gate.FixedCapSizing(6 * GIB),
                       gate.HistoryPeakSizing(6 * GIB, gate.MeasuredProgramPeak(6 * GIB, 10), expired_at)):
            with self.subTest(sizing=sizing):
                profile = gate.ProgramProfile("merge-gate", "installed/.claude", "/tmp/gate", sizing)
                self.change_head(step=profile.step, need=profile.peak.bytes,
                                 need_source=profile.peak.source, need_runs=profile.peak.count,
                                 need_basis=gate.need_basis_record(profile.peak.basis),
                                 estimated_at=expired_at)
                head = gate.explain_queue().head
                assert isinstance(head, gate.HeadExplanation)
                self.assertEqual(head.waiter.need, 6 * GIB)
                text = self.output()
                need_line = self.line(text, "Need source:")
                self.assertIn("6.0 GiB", text)
                if isinstance(sizing, gate.FixedCapSizing):
                    self.assertNotIsInstance(head.waiter.reason, gate.EstimateExpired)
                    self.assertIn("the merge gate's memory cap", need_line)
                    self.assertNotIn("refresh", text)
                    self.assertNotIn("estimate expired", text)
                else:
                    self.assertEqual(head.waiter.reason, gate.EstimateExpired(expired_at))
                    self.assertIn("p90 of 10", need_line)
                    self.assertIn("the estimate expired at", text)
                    self.assertIn("the client refreshes it through the admission service", text)

    def test_ramp_text_uses_the_explanations_policy_after_config_changes(self) -> None:
        config = self.root / "lint.conf"
        write_memory_config(config, ramp_seconds="420")
        with mock.patch.dict(os.environ, {"CONF_DIR": str(config.parent)}):
            policy = gate.read_memory_config().policy
            write_memory_config(config, ramp_seconds="60")
            reading = gate.queue_read()
        assert isinstance(reading, gate.QueueRead)
        explanation = replace(reading.explanation, policy=policy)
        with mock.patch.object(gate, "memory_config", side_effect=AssertionError("render read config")):
            model = queue_view.QueueModel(replace(reading, explanation=explanation), {}, gate.NoWaiter(),
                head_facts.PaceUnavailable(), user_zone(), queue_view.HostLoadUnavailable(),
            gate.smallest_head_release(reading.explanation))
            output = plain(queue_card.render_selection(model, "1", 400))
        self.assertIn("ramp promise: first 7 min after admission", output)
        self.assertNotIn("ramp promise: first 1 min", output)

    def test_running_peak_rows_label_cargo_compiles_and_other_programs(self) -> None:
        _ = self.running(700, "gate", "merge-gate", 8, 1, 1800, peak=8)
        entries = [self.entry(name) for name in ("widget", "demo", "organon", "frame-cost", "inspectors-fix", "gate")]
        with mock.patch.object(unit_labels, "unit_labels_for", return_value=roster.unit_labels(entries)):
            output = self.output()
        cargo = next(line for line in output.splitlines() if "widget-session" in line and "promise" in line)
        program = next(line for line in output.splitlines() if "gate-session" in line and "promise" in line)
        self.assertIn("compiling, peak so far", cargo)
        self.assertIn("running, peak so far", program)
        self.assertNotIn("compiling", program)

    def test_old_sampler_peak_text_uses_the_kinds_hold_and_activity(self) -> None:
        config = self.root / "lint.conf"
        write_memory_config(config, peak_hold_seconds="1500", **{"peak_hold_seconds.merge-gate": "180"})
        with mock.patch.dict(os.environ, {"CONF_DIR": str(config.parent)}):
            policy = gate.read_memory_config().policy
        peak = gate.PeakSoFar(8 * GIB, NOW - 60, NOW - 60, True)
        for kind, activity, minutes in (("check", "compiling", 25),
                                        ("merge-gate", "running", 3),
                                        ("rust-analyzer", "running", 25)):
            with self.subTest(kind=kind):
                text = queue_evidence.charge_text(peak, user_zone(), policy, kind)
                self.assertTrue(text.startswith(f"{activity}, peak so far 8.0 GiB"), text)
                self.assertIn(f"peak drops {minutes} min after its last new peak", text)
                if kind == "merge-gate":
                    self.assertNotIn("25 min", text)

    def test_selected_page_warns_about_a_bad_config_key_once_per_process(self) -> None:
        config = self.root / "lint.conf"
        write_memory_config(config)
        commit_config_fixture(config)
        write_memory_config(config, head_seconds="abc")
        with AdmissionFixture() as admission:
            with mock.patch.dict(os.environ, {"CONF_DIR": str(config.parent)}):
                _ = admission.server.run_pass()
            with admission.serving(), \
                 mock.patch.object(gate, "queue_read", side_effect=client.view), \
                 mock.patch.object(gate, "_warned_config_fallbacks", set[gate.ConfigFallback]()), \
                 mock.patch("sys.argv", ["build_queue.py", "--columns", "400", "1"]), \
                 redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(build_queue.main(), 0)
                self.assertEqual(build_queue.main(), 0)
            self.assertEqual(errors.getvalue().count(
                "head_seconds='abc'"), 1)

    def test_lifetime_charge_text_says_the_promise_holds_until_the_step_ends(self) -> None:
        text = queue_evidence.charge_text(gate.LifetimePromise(16 * GIB), user_zone(), policy=gate.DEFAULT_ADMISSION_POLICY, step="nextest")
        self.assertIn("promise", text)
        self.assertIn("until the step ends", text)
        self.assertNotIn("first 2 min", text)
        self.assertEqual(queue_evidence.charge_text(gate.RampPromise(16 * GIB), user_zone(), policy=gate.DEFAULT_ADMISSION_POLICY, step="nextest"),
                         "ramp promise: first 2 min after admission")

    def test_failed_floor_clause_uses_the_users_local_clock(self) -> None:
        basis = gate.ShapeLargest(1, gate.FailedPeakFloor(gib(15.76), "2026-10-29T02:09:52Z"))
        source = gate.RecordedNeed("shape", 1, 14, "full", "", (), gate.WidthNotRecorded(), basis)
        with mock.patch.dict(os.environ, {"USER_ZONE": "America/New_York"}):
            text = queue_evidence.need_text(source, budget=21 * GIB, zone=user_zone())
        self.assertEqual(text.split("; ")[1], "failed-build floor 15.8 GiB from 22:09")

    def test_dev_shape_names_its_measured_promise_factor(self) -> None:
        source = gate.RecordedNeed("shape", 2, 14, "full", "", (), gate.WidthNotRecorded(),
                                   gate.ShapeLargest(2, factor=gate.DevHeadroom()))
        text = queue_evidence.need_text(source, budget=21 * GIB, zone=user_zone())
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
        self.assertTrue(self.output("id-widget").rstrip().endswith("Nothing waits."))

    def check_waiting_head(self, at: float, **token_times: Unpack[FinishedTokenGapTimes]) -> gate.AdmissionDecision:
        self.reset_running(available=0, need=11.8)
        _ = self.running(800, "holder", "clippy", 1, 1, 3600)
        self.head_path = self.head_path.rename(self.directory / "500-500.waiting")
        waiter = gate.read_waiter(self.head_path)
        waiter["token_left_at"] = token_times["token_left_at"]
        _ = self.head_path.write_text(json.dumps(waiter))
        gate.append_queue_time(self.directory, {"kind": "token_gap", "episode": "returned-head",
            "pid": 500, "start_time": "500", "step": "nextest", "session_id": "id-inspectors-fix",
            "token_left_at": token_times["token_left_at"], "token_back_at": token_times["token_back_at"]}, at)
        reason = gate.queue_read().view.waiting[0].reason
        assert isinstance(reason, gate.HeadAdmissionStatus)
        return reason.decision

    def test_admission_written_token_gap_matches_the_reader_and_both_commands(self) -> None:
        result = self.check_waiting_head(NOW, token_left_at=NOW - 200, token_back_at=NOW - 46)
        self.assertEqual(result.state, "hold")
        facts = head_facts.read_head_facts(gate.queue_read())
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
        prefixes = ("#1 of 17", "Bumps since it joined at",
                    "Order rule:", "Cargo token waits:")
        positions = [output.index(prefix) for prefix in prefixes]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn("Head for:", output)
        joined = datetime.fromtimestamp(NOW - 122 * 60, user_zone()).strftime("%H:%M:%S")
        self.assertEqual(self.line(output, "Bumps since it joined"),
                         f"Bumps since it joined at {joined}: 1")
        self.assertEqual(self.line(output, "Cargo token waits:"), "Cargo token waits: unknown")

    def test_legacy_head_clock_does_not_replace_the_total_queue_wait(self) -> None:
        self.change_head(head_state="head", head_since=NOW - 125)
        output = self.output()
        self.assertIn("waiting 122 min", output)
        self.assertNotIn("Head for:", output)
        self.assertNotIn("became first", output)
        self.assertNotIn("head_since", output)

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

    def test_bump_records_keep_eight_groups_order_actors_times_and_remainder(self) -> None:
        lines: list[bytes] = []
        counts = (12, 10, 8, 7, 6, 5, 4, 3, 2, 1)
        for index, count in enumerate(counts):
            for number in range(count):
                lines.append(bump_line(NOW - 300 + len(lines), session=f"bumped-{index}",
                                       step=f"step-{index}", folder=f"/tmp/folder-{index}",
                                       actor=f"actor-{number % 2}"))
        write_bumps(NOW, lines)
        output = self.output()
        section = output[output.index("Bumps since it joined"):output.index("Order rule:")]
        rows = [line.strip() for line in section.splitlines() if "last bump" in line]
        self.assertEqual(len(rows), 8)
        elapsed = 0
        for index, count in enumerate(counts[:8]):
            elapsed += count
            last_at = datetime.fromtimestamp(NOW - 300 + elapsed - 1, user_zone()).strftime("%H:%M:%S")
            self.assertEqual(rows[index], f"bumped-{index} step-{index}, /tmp/folder-{index}: "
                + f"{count} bumps; by actor-0, actor-1; last bump {last_at}")
        self.assertNotIn("step-8", section)
        self.assertNotIn("step-9", section)
        self.assertIn("and 2 more steps with 3 bumps", section)
        self.assertNotIn("this step", section.lower())

    def test_single_label_read_includes_bump_actor_and_bumped_step_sessions(self) -> None:
        write_bumps(NOW, [bump_line(NOW - 3, actor="actor-one", session="bumped-one"),
                          bump_line(NOW - 2, actor="actor-two", session="bumped-two")])
        with mock.patch.object(unit_labels, "unit_labels_for", return_value={}) as labels, \
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
        self.assertIn("#1 of 17", output)

    def test_order_rule_names_no_room_when_head_uses_all_the_budget(self) -> None:
        for need in (21, 22):
            with self.subTest(need=need):
                self.change_head(need=need * GIB)
                line = self.line(self.output(), "Order rule:")
                self.assertIn("younger steps wait until 21:59", line)
                self.assertNotIn("may use", line)

    def test_backfill_and_position_bump_report_the_admission_deadlines(self) -> None:
        self.change_head(priority_since=NOW - 630)
        priority: gate.QueuePriority = {"unit": "inspectors-fix", "why": "user work", "set_at": NOW - 630,
            "set_by": "showrunner"}
        _ = (self.directory / "queue_priority.json").write_text(json.dumps(priority))
        output = self.output()
        self.assertRegex(output, r"younger steps may start until 21:52.*1 min 30 s left")
        self.assertIn("queue priority: inspectors-fix (the user's bump)", output)

    def test_pressure_hold_and_recovery_spacing_are_explicit(self) -> None:
        _ = (self.root / "pressure").write_text("full avg10=11.00 avg60=0.00 avg300=0.00 total=0\n")
        pressure = self.output()
        self.assertRegex(pressure.lower(), r"pressure.*11(?:\.0)?")
        self.assertIn("pressure", self.line(pressure, "Frees when:").lower())
        _ = (self.root / "pressure").write_text("full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n")
        _ = (self.directory / "pressure-recovery").write_text(json.dumps({
            "state": "recovering", "ends_at": NOW + 120, "next_start_at": NOW + 30,
        }))
        recovery = self.output().lower()
        self.assertIn("recovery", recovery)
        self.assertIn("21:51", recovery)


    def test_read_failure_is_one_fixed_line_with_exit_two(self) -> None:
        for error in (OSError("cannot read ledger"), ValueError("invalid ledger"), TimeoutError("lock busy")):
            with self.subTest(error=type(error).__name__):
                output, errors = io.StringIO(), io.StringIO()
                with mock.patch.object(gate, "queue_read", side_effect=error), \
                     redirect_stdout(output), redirect_stderr(errors), mock.patch("sys.argv", ["build_queue.py", "--columns", "400", "1"]):
                    result = build_queue.main()
                self.assertEqual(result, 2)
                self.assertEqual(output.getvalue() + errors.getvalue(),
                                 f"build queue: {error}\n")


if __name__ == "__main__":
    _ = unittest.main()
