"""Recorded evidence explains the queue without another admission decision."""

from __future__ import annotations

from scripts.build_queue import admission_records as records

from scripts.lint.memory_test_support import admission_policy_fixture

from scripts.build_queue.admission_test_support import AdmissionFixture
from scripts.build_queue.scope_activity import IdleService, RunningServiceJob
from scripts.build_queue import admission_client as client

from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from typing import TypedDict, Unpack, cast, override
from unittest import mock

from scripts.build_queue import queue_evidence, head_facts, build_queue, queue_card, queue_view
from scripts.build_queue.queue_lines import Line, plain
from scripts.build_queue.queue_fixture import write_process, write_scope, fixture_queue_read, ci_step
from scripts.build_queue.queue_fixture import bump_line, queue_time_line, token_gap_line, write_bumps, write_token_gaps
from scripts.lint import memory_admit as gate

from scripts.buildlog import store

from scripts.build_queue.queue_fixture import write_memory_config
from scripts.lint.memory_test_support import commit_config_fixture
from scripts.lib.build_steps import read_step_names
from scripts.lib.user_zone import user_zone
from scripts.message import roster, unit_labels

GIB = gate.GIB
NOW = datetime(2026, 10, 30, 1, 51, tzinfo=UTC).timestamp()


def gib(value: float) -> int:
    return int(value * GIB)


class FinishedTokenGapTimes(TypedDict):
    token_left_at: float
    token_back_at: float


class RoomTextTests(unittest.TestCase):
    def test_ci_amount_shows_mib_only_under_a_tenth_of_a_gib(self) -> None:
        for value, text in ((47 * GIB // 1024, "47 MiB"), (GIB // 10 - 1, "102 MiB"),
                            (GIB // 10, "0.1 GiB"), (22 * GIB, "22.0 GiB")):
            with self.subTest(value=value):
                self.assertEqual(queue_evidence.ci_amount(value), text)

    def test_cap_text_shows_idle_ci_use_in_mib(self) -> None:
        room = gate.memory_room(gate.HostMemory(20 * GIB, 64 * GIB),
            (gate.RunMemory("run", 8 * GIB, gate.AnonymousRunUse(3 * GIB)),),
            admission_policy_fixture(), ci_use=63 * GIB // 1024)
        assert isinstance(room, gate.MemoryRoom)
        self.assertIn("anonymous use 3.0 GiB + CI use 63 MiB + free 20.0 GiB", queue_evidence.room_text(room))

    def test_idle_ci_slice_row_shows_mib_and_sccache_keeps_gib(self) -> None:
        small = 47 * GIB // 1024
        ci = gate.UnownedSliceMemory(small, owner="hana-ci.slice", job_state=IdleService(), pid=gate.PidNotRecorded())
        compiler = gate.CompilerServiceMemory(small, swap_bytes=0, pid=gate.PidNotRecorded())
        self.assertIn("hana-ci.slice: idle; 47 MiB RAM", queue_evidence.service_text(ci, 0.0))
        self.assertIn("sccache.service: idle; 0.0 GiB RAM", queue_evidence.service_text(compiler, 0.0))

    def test_cap_text_names_ci_use_and_its_printed_terms_sum_to_the_cap(self) -> None:
        for ci_use, text in (
            (0, "Cap 20.0 GiB; anonymous use 3.0 GiB + free 20.0 GiB − safety 3.0 GiB; kept aside 5.0 GiB; room 12.0 GiB"),
            (4 * GIB, "Cap 24.0 GiB; anonymous use 3.0 GiB + CI use 4.0 GiB + free 20.0 GiB − safety 3.0 GiB; kept aside 5.0 GiB; room 12.0 GiB"),
        ):
            with self.subTest(ci_use=ci_use):
                room = gate.memory_room(gate.HostMemory(20 * GIB, 64 * GIB),
                    (gate.RunMemory("run", 8 * GIB, gate.AnonymousRunUse(3 * GIB)),),
                    admission_policy_fixture(), ci_use=ci_use)
                assert isinstance(room, gate.MemoryRoom)
                self.assertEqual(queue_evidence.room_text(room), text)



class QueueEvidenceTests(unittest.TestCase):
    root: Path = Path()
    directory: Path = Path()
    proc: Path = Path()
    head_path: Path = Path()
    widget_path: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        config = self.root / "build_admission.conf"
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
        _ = (compiler / "memory.stat").write_text(f"anon {gib(0.4)}\nfile 0\ninactive_file 0\n")
        _ = (compiler / "memory.current").write_text(str(gib(0.4)))
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
        _ = self.enterContext(mock.patch.object(gate, "priority_labels", return_value=labels))

    def entry(self, name: str) -> roster.RosterEntry:
        return roster.RosterEntry(
            machine="test-machine", kind="codex", role="freestanding", name=f"{name}-session",
            status="running", reach=roster.CannotReceiveMessages("fixture"), cwd="/tmp/tree",
            showrunner=roster.UnderShowrunner("hana", "hana", "session:hana"),
            director=roster.UnderDirector(name, name, f"session:{name}"),
            assignment=roster.NotAWorker(), thread=roster.CodexThread(f"id-{name}"),
            session=roster.NoClaudeSession(),
        )

    def running(self, pid: int, name: str, step: str, need: float, used: float,
                age: int, *, peak: float = 0) -> Path:
        write_process(self.proc, pid, gib(used))
        folder = "/tmp/p34-merge" if name == "widget" else f"/tmp/{name}"
        scope = self.root / f"{pid}.scope"
        write_scope(scope, members=(pid,), current=gib(used), anonymous=gib(used))
        sidecar = self.root / f"{pid}.scope-name"
        _ = sidecar.write_text(str(scope))
        row: dict[str, object] = {
            "pid": pid, "start_time": str(pid), "need": gib(need), "repo": "hana", "step": step,
            "worktree": folder, "sidecar": str(sidecar), "admitted_at": store.utc_iso(NOW - age),
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
        compiler = self.root / "builds.slice/sccache.service"
        _ = (compiler / "memory.stat").write_text("anon 0\nfile 0\ninactive_file 0\n")
        _ = (compiler / "memory.current").write_text("0")
        _ = (self.root / "meminfo").write_text(
            f"MemAvailable: {gib(available) // 1024} kB\nMemTotal: {60 * GIB // 1024} kB\n")
        self.change_head(need=gib(need))

    def test_unavailable_run_use_names_the_run_without_showing_an_anonymous_sum(self) -> None:
        self.reset_running(available=20, need=8)
        for pid, name in ((100, "merge-holder"), (101, "other-holder")):
            path = self.running(pid, name, "merge-gate", 6, 4, 180)
            row = cast(dict[str, object], json.loads(path.read_text()))
            row["sidecar"] = ""
            row["reservation_id"] = name
            _ = path.write_text(json.dumps(row))
            _ = (self.proc / str(pid) / "status").write_text("VmRSS: 4194304 kB\nVmSwap: 1048576 kB\n")
        status = self.proc / "100/status"
        read_text = Path.read_text
        for denied in (False, True):
            with self.subTest(denied=denied):
                _ = status.write_text("VmRSS: 4194304 kB\nVmSwap: 1048576 kB\n")

                def read_private(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
                    if denied and path == status:
                        raise PermissionError(13, "Permission denied", str(path))
                    return read_text(path, encoding=encoding, errors=errors)

                with mock.patch.object(Path, "read_text", read_private):
                    captured = fixture_queue_read(self.directory, gate.process_table(self.proc), NOW)
                room = captured.explanation.room
                assert isinstance(room, gate.MemoryRoom)
                self.assertIsInstance(room.cap, gate.CapUnavailable)
                text = queue_evidence.room_text(room)
                self.assertIn("unavailable", text)
                self.assertIn("merge-holder", text)
                self.assertEqual(text.count("merge-holder"), 1)
                self.assertEqual(text.count("other-holder"), 1)
                self.assertEqual(text.count("anonymous use unavailable"), 1)
                self.assertNotIn("anonymous use 0.0", text)
                self.assertNotIn("anonymous use 4.0", text)
                self.assertIn("room", text)

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
                policy = admission_policy_fixture()
                pressure = gate.read_pressure(host, policy=policy)
                assert isinstance(pressure, gate.MemoryPressure)
                changed = replace(policy, pressure=replace(policy.pressure,
                    full_avg10_limit=100, swap_used_limit=100, some_avg10_limit=100))
                memory_text = gate.pressure_text(pressure)
                evidence = queue_evidence.pressure_text(pressure, changed)
                if detail == "swap":
                    self.assertIn("swap", memory_text)
                    self.assertIn("starts when swap", evidence)
                    self.assertNotIn("starts below full", evidence)
                else:
                    self.assertIn("full avg10", memory_text)
                    self.assertIn("starts below full", evidence)


    def test_capped_need_uses_the_saved_budget_without_configuration_reads(self) -> None:
        rule = gate.ReleaseHeadroom(gate.read_memory_config(report=False).release_headroom)
        source = gate.RecordedNeed("shape", 10, 14, "full", "", (), gate.WidthNotRecorded(),
            gate.ShapeRankedPeak(10, 10, gate.FailedPeakFloor(16 * GIB, "2026-10-30T01:40:00Z"),
                          rule, gate.CappedAtBudget(30 * GIB, 19 * GIB)))
        zone = user_zone()
        with mock.patch.object(gate, "read_memory_config", side_effect=AssertionError("live policy")), \
             mock.patch("scripts.lib.conf.read", side_effect=AssertionError("live configuration")):
            text = queue_evidence.need_text(source, zone=zone)
        for value in ("19.0 GiB", "30.0 GiB", "16.0 GiB", str(rule.factor), "14 days"):
            self.assertIn(value, text)
        self.assertNotIn("21.0 GiB", text)

    def test_full_package_estimates_keep_package_and_width_for_every_basis(self) -> None:
        for basis in (gate.BasisNotRecorded(), gate.ShapeRankedPeak(7, 10), gate.ShapeLargest(2),
                      gate.ShapeFallback()):
            with self.subTest(basis=basis):
                self.change_head(test_scope="full", package="hana_widget", filters=[],
                    test_threads=12, need_basis=gate.need_basis_record(basis))
                text = self.output()
                self.assertIn("Package: hana_widget; filters: none; width: 12; scope: full", text)
        self.change_head(package="", test_threads=None)
        self.assertIn("width: not recorded", self.output())

    def test_unknown_admission_time_keeps_the_running_charge_and_basis(self) -> None:
        reading = gate.queue_read()
        running = replace(reading.explanation.running[0], admitted_at=gate.AdmissionTimeNotRecorded())
        explanation = replace(reading.explanation, running=(running, *reading.explanation.running[1:]))
        text = queue_evidence.running_text(running, explanation, {}, user_zone(), names=read_step_names())
        self.assertIn("admission time unknown", text)
        self.assertIn("need 10.8 GiB; anonymous use 0.8 GiB; held slot 0.0 GiB; kept aside 10.0 GiB", text)
        self.assertNotIn("admitted 00:00", text)

    def test_running_and_release_sentences_use_step_prose(self) -> None:
        reading = gate.queue_read()
        names = read_step_names()
        for step, prose in (("targeted-test", "targeted test"), ("merge-gate", "merge gate")):
            with self.subTest(step=step):
                running = replace(reading.explanation.running[0],
                                  step=replace(reading.explanation.running[0].step, step=step))
                explanation = replace(reading.explanation, running=(running, *reading.explanation.running[1:]))
                text = queue_evidence.running_text(running, explanation, {}, user_zone(), names=names)
                self.assertIn(f" {prose}: admitted ", text)
                self.assertNotIn(step, text)
                release = gate.HeadStartsAt(NOW + 30, (0,), 0, 0, 0)
                text = queue_evidence.release_text(release, explanation, {}, user_zone(), names=names)
                self.assertIn(f" {prose} end ", text)
                self.assertNotIn(step, text)

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
                self.assertIn("measured process-memory estimate from 7 runs", text)
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
             mock.patch.object(gate, "admission_pass", side_effect=AssertionError("render recalculated admission")), \
             mock.patch.object(gate, "memory_config", side_effect=AssertionError("render read policy")):
            text = self.output()
        read.assert_called_once_with()
        release_read.assert_called_once_with(reading.explanation)
        self.assertEqual(sum(cast(tuple[object, ...], call.args)[0] == self.proc / "loadavg"
                             for call in load_read.call_args_list), 1)
        facts.assert_called_once_with(reading)
        labels.assert_called_once()
        self.assertEqual(text.count("Frees when:"), 1)
        self.assertEqual(text.count("measured process-memory estimate from 7 runs"), 1)
        self.assertEqual(text.count("Cargo token waits:"), 1)
        self.assertEqual(text.count("Host load: 64.25"), 1)
        self.assertEqual(text.count("#1 of 17"), 1)


    def test_expired_head_explains_refresh_even_when_all_resource_checks_pass(self) -> None:
        self.reset_running(available=60, need=1)
        self.change_head(estimated_at=NOW - admission_policy_fixture().timing.estimate_refresh_seconds)
        text = self.output()
        self.assertIn("the estimate expired at 21:41; the client refreshes it through the admission service", text)
        explanation = gate.explain_queue()
        assert isinstance(explanation.head, gate.HeadExplanation)
        self.assertTrue(explanation.head.room.passes)
        self.assertTrue(explanation.head.free.passes)

    def test_expired_head_release_advice_requires_refresh_instead_of_queue_order_or_a_start_time(self) -> None:
        self.reset_running(available=40, need=1)
        _ = self.running(800, "holder", "clippy", 1, 1, 3600)
        expired_at = NOW - admission_policy_fixture().timing.estimate_refresh_seconds
        self.change_head(estimated_at=expired_at)
        explanation = gate.explain_queue()
        assert isinstance(explanation.head, gate.HeadExplanation)
        self.assertTrue(explanation.head.room.passes)
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
        self.reset_running(available=20, need=22)
        _ = self.running(800, "only-holder", "clippy", 1, 1, 3600)
        output = self.output()
        final = self.line(output, "Frees when:")
        self.assertIn("stale-only-holder hana clippy end", final)
        self.assertNotIn("insufficient", final)
        self.assertNotIn("still leaves", final)
        result = gate.head_after_steps_end(gate.explain_queue(), (0,))
        self.assertIsInstance(result, gate.HeadAdmitsAfterEnd)

    def test_order_rule_shows_the_queue_priority_from_its_snapshot(self) -> None:
        from dataclasses import replace
        reading = gate.queue_read()
        priority: gate.QueuePriority = {"unit": "resources", "why": "work", "set_at": NOW,
            "set_by": "showrunner", "transition": gate.SettledPriority()}
        reading = replace(reading, explanation=replace(reading.explanation, priority=priority))
        facts = head_facts.read_head_facts(reading)
        model = queue_view.QueueModel(reading, {}, facts, head_facts.PaceUnavailable(), user_zone(), queue_view.HostLoadUnavailable(),
            gate.smallest_head_release(reading.explanation))
        self.assertIn("queue priority: resources (work)", plain(queue_view.render_view(model, 400)))

    def test_override_room_uses_the_recorded_head_need_and_safety(self) -> None:
        self.reset_running(available=23, need=12)
        _ = self.running(800, "growing", "clippy", 20, 1, 30)
        head_path, head = gate.queue_read().ordered[0]
        override: gate.HeadAdmissionOverride = {
            "waiter": head_path.name, "pid": head["pid"], "start_time": head["start_time"],
            "unit": "fixture", "step": head.get("step", "nextest"), "why": "arithmetic regression",
            "set_by": "showrunner-id", "need": head["need"], "free": 23 * GIB, "reserve": 3 * GIB,
        }
        _ = gate.head_override_path(self.directory).write_text(json.dumps(override))
        reading = gate.queue_read()
        assert isinstance(reading.explanation.head, gate.HeadExplanation)
        self.assertEqual(reading.explanation.head.room, gate.RoomCheck(12 * GIB, 20 * GIB, True, basis=gate.OverrideRoom()))
        self.assertEqual(self.line(self.output(), "Room:"), "Room: need 12.0 GiB <= room 20.0 GiB: pass; head override")
        self.assertNotIn("starts alone", self.output())

    def test_card_shows_the_keep_aside_captured_by_the_admission_pass(self) -> None:
        self.reset_running(available=10, need=12)
        _ = self.running(800, "captured", "clippy", 8, 3, 3600)
        reading = gate.queue_read()
        room = reading.explanation.room
        assert isinstance(room, gate.MemoryRoom)
        hold, = room.runs
        self.assertEqual(hold.bytes, 5 * GIB)
        self.assertEqual(hold.memory.use, gate.AnonymousRunUse(3 * GIB))
        self.assertEqual(hold.memory.need, 8 * GIB)
        running, = (row for row in reading.explanation.running if gate.running_has_reservation(row.step))
        self.assertEqual(running.charged, 8 * GIB)
        model = queue_view.QueueModel(reading, {}, head_facts.read_head_facts(reading),
            head_facts.PaceUnavailable(), user_zone(), queue_view.HostLoadUnavailable(),
            gate.smallest_head_release(reading.explanation))
        with mock.patch.object(gate, "read_meminfo", side_effect=AssertionError("use captured host memory")), \
             mock.patch.object(gate, "reservation_memory", side_effect=AssertionError("use captured run memory")):
            card = plain(queue_card.render_card(model, [1], 400))
        self.assertIn("need 8.0 GiB; anonymous use 3.0 GiB; held slot 0.0 GiB; kept aside 5.0 GiB", card)
        self.assertNotIn("kept aside 8.0 GiB", card)
        head = reading.explanation.head
        assert isinstance(head, gate.HeadExplanation)
        self.assertIn(queue_evidence.room_check_text(head.room), card)

    def test_release_advice_names_only_the_smaller_step_that_frees_enough_memory(self) -> None:
        self.reset_running(available=13, need=8)
        _ = self.running(800, "large-peak", "clippy", 10, 0.1, 3600, peak=10)
        _ = self.running(801, "useful-release", "clippy", 9, 9, 3600)
        final = self.line(self.output(), "Frees when:")
        self.assertIn("stale-useful-release hana clippy end", final)
        self.assertNotIn("large-peak", final)
        self.assertIn("keep-aside drops 0.0 GiB; free memory rises 9.0 GiB", final)

    def test_release_advice_retains_the_running_ci_keep_aside(self) -> None:
        self.reset_running(available=15, need=11)
        _ = self.running(800, "ending", "clippy", 8, 3, 3600)
        _ = self.running(801, "remaining", "clippy", 1, 1, 3600)
        inputs = gate.queue_read().explanation.decided_from
        ending = next(index for index, step in enumerate(inputs.running) if step.session_id == "id-ending")
        config = gate.read_memory_config()
        for job, expected in ((IdleService(), gate.HeadAdmitsAfterEnd),
                              (RunningServiceJob(NOW - 120), gate.HeadStillHeld)):
            with self.subTest(job=job):
                ci = ci_step(4 * GIB, job)
                captured = replace(inputs, running=(*inputs.running, ci))
                explanation = gate.queue_read_from(captured, config.policy).explanation
                outcome = gate.head_after_steps_end(explanation, (ending,))
                self.assertIsInstance(outcome, expected)
                assert isinstance(outcome, (gate.HeadAdmitsAfterEnd, gate.HeadStillHeld))
                expected_room = 8 * GIB if isinstance(job, RunningServiceJob) else 15 * GIB
                self.assertEqual(outcome.decision.room.room, expected_room)

    def test_release_ties_choose_least_charge_then_running_table_order(self) -> None:
        self.reset_running(available=4, need=14)
        for pid, name, charge in ((800, "larger", 8), (801, "first-small", 6), (802, "second-small", 6)):
            _ = self.running(pid, name, "clippy", charge, charge, 3600)
        final = self.line(self.output(), "Frees when:")
        self.assertIn("stale-larger hana clippy + stale-first-small hana clippy end", final)
        self.assertNotIn("second-small", final)

    def test_equal_size_releases_choose_the_least_charge_even_later_in_the_table(self) -> None:
        self.reset_running(available=0, need=6)
        for pid, name, charge in ((800, "largest", 8), (801, "medium", 7),
                                  (802, "small-a", 6), (803, "small-b", 6)):
            _ = self.running(pid, name, "clippy", charge, charge, 3600)
        final = self.line(self.output(), "Frees when:")
        self.assertIn("stale-small-a hana clippy + stale-small-b hana clippy end", final)
        self.assertNotIn("largest", final)
        self.assertNotIn("medium", final)
        self.assertIn("keep-aside drops 0.0 GiB; free memory rises 12.0 GiB", final)


    def test_release_keeps_the_snapshot_limits_and_reports_a_pressure_hold(self) -> None:
        explanation = gate.explain_queue()
        with mock.patch.object(gate, "read_memory_config", side_effect=AssertionError("use snapshot policy")), \
             mock.patch.object(gate, "read_meminfo", side_effect=AssertionError("no second reading")):
            release = gate.smallest_head_release(explanation)
        self.assertIsInstance(release, gate.HeadAdmitsAfterEnd)
        _ = (self.root / "pressure").write_text(
            "some avg10=0.00 avg60=0.00 avg300=0.00 total=0\n" +
            "full avg10=25.00 avg60=0.00 avg300=0.00 total=0\n")
        final = self.line(self.output(), "Frees when:")
        self.assertIn("still leaves pressure holding the head", final)

    def test_twenty_running_steps_bound_release_scenarios_and_reuse_the_result(self) -> None:
        self.reset_running(available=15, need=18)
        # Charge and free-memory constraints favor different halves of the running list.
        for index in range(20):
            charge, used = (10.0, 0.1) if index < 10 else (1.0, 1.0)
            _ = self.running(800 + index, f"holder-{index}", "clippy", charge, used, 3600, peak=charge)
        reading = gate.queue_read()
        explanation = reading.explanation
        with mock.patch.object(gate, "head_after_steps_end", wraps=gate.head_after_steps_end) as scenarios:
            release = gate.smallest_head_release(explanation)
        self.assertLessEqual(scenarios.call_count, 2 + len(explanation.running) ** 2)
        model = queue_view.QueueModel(reading, {}, head_facts.read_head_facts(reading),
            head_facts.PaceUnavailable(), user_zone(), queue_view.HostLoadUnavailable(), release)
        with mock.patch.object(gate, "smallest_head_release",
                               side_effect=AssertionError("the card must reuse the release result")):
            lines = plain(queue_card.render_selection(model, "1", 400)).splitlines()
        self.assertEqual(len(explanation.running), 21)
        compiler, = (row for row in explanation.running
                     if isinstance(row.step.measured, gate.CompilerServiceMemory))
        self.assertEqual(compiler.charged, 0)
        self.assertEqual(sum(gate.running_has_reservation(row.step) for row in explanation.running), 20)
        final = " ".join("\n".join(lines).partition("Frees when:")[2].split())
        self.assertNotIn("still leaves", final)
        self.assertIn("end (keep-aside drops", final)

    def test_recorded_room_matches_the_actual_admission_decision(self) -> None:
        reading = gate.queue_read()
        inputs = reading.explanation.decided_from
        assert isinstance(inputs, gate.QueueInputs)
        head = reading.explanation.head
        assert isinstance(head, gate.HeadExplanation)
        result = gate.admission_pass(inputs, NOW, reading.explanation.policy)
        self.assertEqual(head.decision, result.rows[0].decision)
        self.assertEqual(head.decision.state, "hold")
        self.assertEqual(head.room.need, head.waiter.need)
        self.assertEqual(head.room.room,
                         reading.view.available - head.decision.reserve - head.decision.promised)
        output = self.output()
        self.assertIn(queue_evidence.room_check_text(head.room), output)
        self.assertNotIn("Build budget:", output)
        for section in ("Running", "#1 of 17", "Room:", "Frees when:"):
            self.assertIn(section, output)

    def test_running_records_keep_every_identity_time_and_memory_value(self) -> None:
        output = self.output()
        running_section = output[output.index("Running:"):output.index("Total current use:")]
        rows = [line for line in running_section.splitlines() if "; need " in line or "sccache.service:" in line]
        self.assertEqual(len(rows), 5)
        expected = (
            ("widget", "nextest", "20:51 (60 min ago)", "10.8", "0.8", "10.0"),
            ("demo", "nextest", "21:31 (20 min ago)", "1.0", "3.8", "0.0"),
            ("organon", "clippy", "21:50 (0 min ago)", "6.1", "1.3", "4.8"),
            ("frame-cost", "clippy", "21:50 (1 min ago)", "6.8", "6.1", "0.7"),
        )
        for name, step, admitted, need, used, kept in expected:
            row, = [line for line in rows if f"{name}-session," in line]
            self.assertIn(f"hana / {name} / {name}-session, hana {step}: admitted {admitted}", row)
            self.assertIn(f"need {need} GiB; anonymous use {used} GiB; held slot 0.0 GiB; kept aside {kept} GiB", row)
        compiler, = [line for line in rows if "sccache.service" in line]
        self.assertIn("sccache.service: idle; 0.4 GiB RAM; 0.0 GiB swapped", compiler)
        self.assertNotIn("admission time unknown", compiler)
        self.assertNotIn("stale-widget", output)
        self.assertNotIn("p34-merge", running_section)


    def test_need_source_records_explain_sample_window_filter_and_width(self) -> None:
        output = self.output()
        for word in ("measured", "estimate", "7 runs", "14 days", "filtered", "hana_inspectors", "filters: test(one), test(two)", "width: 8"):
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
            (gate.ShapeRankedPeak(12, 10), ("same-shape", "highest 10%", "12 runs")),
            (gate.ShapeRankedPeak(20, 50), ("same-shape", "highest 50%", "20 runs")),
            (gate.ShapeLargest(3), ("same-shape", "largest", "3 runs")),
            (gate.ShapeFallback(), ("same-profile", "fallback")),
        ):
            with self.subTest(basis=basis):
                source = gate.RecordedNeed("shape", basis.runs if isinstance(basis, gate.ShapeRankedPeak) else 3,
                                           14, "full", "", (), gate.WidthNotRecorded(), basis)
                text = queue_evidence.need_text(source, zone=user_zone())
                for word in (*words, "14 days"):
                    self.assertIn(word, text)
                self.assertNotIn("p90", text)

    def test_shape_need_text_names_floor_release_factor_and_budget_cap(self) -> None:
        ended_at = "2026-10-29T09:09:52Z"
        floor = gate.FailedPeakFloor(gib(15.76), ended_at)
        rule = gate.ReleaseHeadroom(gate.read_memory_config(report=False).release_headroom)
        for tier in (
            gate.ShapeRankedPeak(10, 10, floor, rule, gate.CappedAtBudget(gib(24), 21 * GIB)),
            gate.ShapeLargest(2, floor, rule, gate.CappedAtBudget(gib(24), 21 * GIB)),
            gate.ShapeFallback(gate.CappedAtBudget(gib(24), 21 * GIB)),
        ):
            with self.subTest(tier=tier):
                text = queue_evidence.need_text(gate.RecordedNeed(
                    "shape", 10, 14, "full", "", (), gate.WidthNotRecorded(), tier), zone=user_zone())
                words = (("limited to", "21.0 GiB", "24.0 GiB", "no headroom factor")
                         if isinstance(tier, gate.ShapeFallback) else
                         ("floor", "15.8 GiB", "05:09", str(rule.factor), "limited to", "21.0 GiB", "24.0 GiB"))
                for word in words:
                    self.assertIn(word, text)

    def test_fallback_need_text_names_its_base_and_cap_without_a_floor_or_factor(self) -> None:
        basis = gate.ShapeFallback(cap=gate.CappedAtBudget(gib(24), 21 * GIB))
        source = gate.RecordedNeed("shape", 0, 14, "full", "", (), gate.WidthNotRecorded(), basis)
        self.assertEqual(queue_evidence.need_text(source, zone=user_zone()),
                         "same-shape fallback: no runs; heavier of fallback and same-profile builds, last 14 days"
                         + "; no headroom factor; limited to 21.0 GiB at that time (uncut 24.0 GiB)")

    def test_need_text_names_saved_caps_and_dev_size_headroom(self) -> None:
        for basis, words in (
            (gate.ShapeFallback(gate.CappedAtBudget(22 * GIB, 21 * GIB)),
             ("limited to 21.0 GiB at that time (uncut 22.0 GiB)",)),
            (gate.ShapeFallback(gate.CappedAtBudget(22 * GIB, gate.CapNotRecorded())),
             ("limited to an unrecorded amount at that time (uncut 22.0 GiB)",)),
            (gate.ShapeLargest(2, factor=gate.DevSizeHeadroom(1.25, 2 * GIB)),
             ("dev", "1.25", "margin", "2.0 GiB", "uncut estimate")),
        ):
            with self.subTest(basis=basis):
                source = gate.RecordedNeed("shape", 2, 14, "full", "", (), gate.WidthNotRecorded(), basis)
                text = queue_evidence.need_text(source, zone=user_zone())
                for word in words:
                    self.assertIn(word, text)
                self.assertNotIn("4.203", text)
                if isinstance(basis, gate.ShapeFallback):
                    self.assertEqual(text.split("; ")[-1], words[0])



    def test_aged_history_waiter_shows_refresh_text(self) -> None:
        expired_at = NOW - admission_policy_fixture().timing.estimate_refresh_seconds - 1
        profile = gate.ProgramProfile("merge-gate", "installed/.claude", "/tmp/gate",
            gate.HistoryPeakSizing(6 * GIB, gate.MeasuredProgramPeak(6 * GIB, 10), expired_at))
        self.change_head(step=profile.step, need=profile.peak.bytes,
                         need_source=profile.peak.source, need_runs=profile.peak.count,
                         need_basis=gate.need_basis_record(profile.peak.basis), estimated_at=expired_at)
        head = gate.explain_queue().head
        assert isinstance(head, gate.HeadExplanation)
        self.assertEqual(head.waiter.need, 6 * GIB)
        text = self.output()
        need_line = self.line(text, "Need source:")
        self.assertIn("6.0 GiB", text)
        self.assertEqual(head.cause, gate.EstimateExpired(expired_at))
        self.assertIn("measured process-memory estimate from 10 runs", need_line)
        self.assertIn("the estimate expired at", text)
        self.assertIn("the client refreshes it through the admission service", text)

    def test_selected_page_warns_about_a_bad_config_key_once_per_process(self) -> None:
        config = self.root / "build_admission.conf"
        write_memory_config(config)
        commit_config_fixture(config)
        write_memory_config(config, estimate_refresh_seconds="abc")
        with AdmissionFixture() as admission:
            with mock.patch.dict(os.environ, {"CONF_DIR": str(config.parent)}):
                _ = admission.server.run_pass()
            with admission.serving(), \
                 mock.patch.object(gate, "queue_read", side_effect=client.view), \
                 mock.patch.object(records, "_warned_configuration_diagnostics", set[gate.ConfigurationDiagnostic]()), \
                 mock.patch("sys.argv", ["build_queue.py", "--columns", "400", "1"]), \
                 redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(build_queue.main(), 0)
                self.assertEqual(build_queue.main(), 0)
            self.assertEqual(errors.getvalue().count(
                "estimate_refresh_seconds='abc'"), 1)


    def test_failed_floor_clause_uses_the_users_local_clock(self) -> None:
        basis = gate.ShapeLargest(1, gate.FailedPeakFloor(gib(15.76), "2026-10-29T02:09:52Z"))
        source = gate.RecordedNeed("shape", 1, 14, "full", "", (), gate.WidthNotRecorded(), basis)
        with mock.patch.dict(os.environ, {"USER_ZONE": "America/New_York"}):
            text = queue_evidence.need_text(source, zone=user_zone())
        self.assertEqual(text.split("; ")[1], "failed-build floor 15.8 GiB from 22:09")

    def test_dev_shape_names_its_measured_promise_factor(self) -> None:
        source = gate.RecordedNeed("shape", 2, 14, "full", "", (), gate.WidthNotRecorded(),
                                   gate.ShapeLargest(2, factor=gate.LegacyDevMultiplierHeadroom()))
        text = queue_evidence.need_text(source, zone=user_zone())
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

    def test_all_token_waiters_remain_waiting_in_the_snapshot_and_explanation(self) -> None:
        from scripts.build_queue import queue_snapshot
        from scripts.build_queue.queue_fixture import fixture_queue_inputs
        paths = sorted(self.directory.glob("*.waiting"))
        for path in paths:
            row = cast(dict[str, object], json.loads(path.read_text()))
            row["token_left_at"] = NOW - 10
            _ = path.write_text(json.dumps(row))
        inputs = fixture_queue_inputs(self.directory, gate.process_table(self.proc), NOW,
                                      admission_policy_fixture())
        queue_snapshot.write_queue_snapshot(self.directory, inputs, admission_policy_fixture())
        snapshot = queue_snapshot.read_queue_snapshot(self.directory)
        assert isinstance(snapshot, queue_snapshot.QueueSnapshot)
        reading = gate.queue_read_from(snapshot.inputs, snapshot.policy)
        self.assertEqual(len(reading.view.waiting), len(paths))
        self.assertTrue(all(isinstance(step.reason, gate.TokenWaitingWaiter) for step in reading.view.waiting))
        self.assertIsInstance(reading.explanation.head, gate.TokenWaitersOnly)
        with mock.patch.object(gate, "queue_read", return_value=reading):
            text = self.output()
        self.assertNotIn("Nothing waits.", text)
        self.assertIn("All waiting steps are waiting for the cargo token.", text)
        self.assertIn("Frees when: all waiting steps are waiting for the cargo token.", text)

    def check_waiting_head(self, at: float, **token_times: Unpack[FinishedTokenGapTimes]) -> gate.AdmissionDecision:
        self.reset_running(available=0, need=11.8)
        _ = self.running(800, "holder", "clippy", 1, 1, 3600)
        self.head_path = self.head_path.rename(self.directory / "500-500.waiting")
        waiter = gate.read_waiter(self.head_path)
        waiter["token_left_at"] = token_times["token_left_at"]
        waiter["token_back_at"] = token_times["token_back_at"]
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
        store.host_file(store.host_name(), NOW).mkdir(parents=True)
        output = self.output()
        self.assertRegex(self.line(output, "Bumps since it joined:"),
                         r"^Bumps since it joined: unknown \(.+\)$")
        self.assertNotIn("the admission state could not be read", output)
        self.assertIn("#1 of 17", output)


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
