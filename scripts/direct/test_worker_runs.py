"""Detached run readings explain a pane worker's apparent inactivity."""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast, override
from unittest.mock import patch

from scripts.agents.codex_mesh import DETACHED_RUNS_DIR
from scripts.agents.mesh_runs import RunRecord
from scripts.direct import worker_runs


class WorkerRunsTests(unittest.TestCase):
    root: Path = Path()
    session: Path = Path()
    board: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.session = self.root / "unit-session"
        self.board = self.root / "board"
        (self.session / DETACHED_RUNS_DIR).mkdir(parents=True)

    def run_record(self, run_id: str = "tests", *, worker: str = "worker-one",
                   pid: int = 120, started: float = 1_000,
                   status: Literal["running", "finished"] = "running",
                   delivery: str = "", finished: float = 1_090, exit_code: int = 0) -> Path:
        log = self.session / DETACHED_RUNS_DIR / f"{run_id}.log"
        record: RunRecord = {
            "run_id": run_id, "status": status, "session_dir": str(self.session),
            "worker": "same-pane-name", "staff_id": worker, "label": f"Check {run_id}",
            "command": ["python3", "-m", "unittest"], "log": str(log),
            "started_at": started, "pid": pid, "director_dir": "", "done_message_file": "",
        }
        if status == "finished":
            record["exit_code"] = exit_code
            record["finished_at"] = datetime.fromtimestamp(finished, UTC).isoformat()
            record["delivery"] = delivery
        _ = (log.with_suffix(".json")).write_text(json.dumps(record), encoding="utf-8")
        _ = log.write_text("starting\n", encoding="utf-8")
        return log

    @staticmethod
    def process_stat(pid: int, *, user: int, system: int, child_user: int, child_system: int) -> str:
        return (f"{pid} (fixture (worker) with spaces) S 1 1 1 0 -1 0 10000 20000 30000 40000 "
                + f"{user} {system} {child_user} {child_system} 20 0 1 0 0\n")

    @staticmethod
    def unavailable_process_stat(pid: int) -> worker_runs.ProcessStatUnavailable:
        return worker_runs.ProcessStatUnavailable(f"fixture pid {pid}")

    def test_process_times_cover_linux_days_and_macos_fractional_seconds(self) -> None:
        table = worker_runs.parse_process_table(
            "10 1 3-04:05:06\n20 1 02:03:04\n30 1 05:06\n"
            + "40 1 1:02.50\n50 1 02:03:04.25\n60 1 00:01.25\n"
        )
        for pid, seconds in ((10, 273_906), (20, 7_384), (30, 306),
                             (40, 62.5), (50, 7_384.25), (60, 1.25)):
            with self.subTest(pid=pid):
                self.assertEqual(worker_runs.tree_cpu_seconds(table, pid, self.unavailable_process_stat), seconds)

    def test_tree_cpu_includes_every_descendant_and_excludes_other_processes(self) -> None:
        table = worker_runs.parse_process_table(
            "120 1 00:01\n121 120 00:02\n122 121 00:03\n123 120 00:04\n999 1 59:59\n"
        )
        self.assertEqual(worker_runs.tree_cpu_seconds(table, 120, self.unavailable_process_stat), 10)
        self.assertEqual(worker_runs.tree_cpu_seconds(table, 121, self.unavailable_process_stat), 5)
        self.assertEqual(worker_runs.tree_cpu_seconds(table, 777, self.unavailable_process_stat), 0)

    def test_process_reader_uses_one_portable_ps_call(self) -> None:
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess(
                ["ps"], 0, "120 1 00:02.50\n121 120 00:01.25\n", "")) as process:
            table = worker_runs.read_process_table()
        assert isinstance(table, dict)
        self.assertEqual(worker_runs.tree_cpu_seconds(
            table, 120, self.unavailable_process_stat), 3.75)
        process.assert_called_once_with(["ps", "-A", "-o", "pid=,ppid=,time="],
                                        capture_output=True, text=True, check=False)

    def test_idle_worker_gets_first_reading_then_measured_cpu_and_log_growth(self) -> None:
        log = self.run_record()
        _ = log.write_text("x" * 8_000 + "\n\x1b[32mchecking\x1b[0m\n\n", encoding="utf-8")
        table = worker_runs.parse_process_table("120 1 00:01\n121 120 00:02\n")
        readings = worker_runs.BoardRunReadings(self.board, 1_100, process_reader=lambda: table, stat_reader=self.unavailable_process_stat)
        first = worker_runs.observe_session(self.session, {"worker-one": "idle"}, {}, readings)
        readings.save()
        self.assertEqual(first["worker-one"]["state"], "waiting on run")
        self.assertEqual(first["worker-one"].get("run"), {
            "label": "Check tests", "started_at": 1_000,
            "growth": {"kind": "first_reading"}, "last_line": "checking",
        })
        initial_size = log.stat().st_size
        with log.open("a", encoding="utf-8") as stream:
            _ = stream.write("y" * 99 + "\n")
        self.assertEqual(log.stat().st_size - initial_size, 100)
        table = worker_runs.parse_process_table("120 1 00:03\n121 120 00:05\n")
        readings = worker_runs.BoardRunReadings(self.board, 1_110, process_reader=lambda: table, stat_reader=self.unavailable_process_stat)
        second = worker_runs.observe_session(self.session, {"worker-one": "idle"}, {}, readings)
        readings.save()
        self.assertEqual(second["worker-one"].get("run"), {
            "label": "Check tests", "started_at": 1_000,
            "growth": {"kind": "measured", "cpu_percent": 50, "log_bytes_per_minute": 600},
            "last_line": "y" * 99,
        })
        self.assertEqual(sorted(path.name for path in self.board.iterdir()), ["run_readings.json", "run_readings.lock"])
        self.assertEqual(sorted(path.name for path in self.session.iterdir()), [DETACHED_RUNS_DIR])

    def test_many_workers_and_sessions_share_one_process_snapshot(self) -> None:
        _ = self.run_record(worker="worker-one")
        other = self.root / "second-session"
        (other / DETACHED_RUNS_DIR).mkdir(parents=True)
        source = self.session / DETACHED_RUNS_DIR / "tests.json"
        _ = (other / DETACHED_RUNS_DIR / source.name).write_bytes(source.read_bytes())
        table = worker_runs.parse_process_table("120 1 00:01\n")
        with patch.object(worker_runs, "read_process_table", return_value=table) as process:
            readings = worker_runs.BoardRunReadings(self.board, 1_100, process_reader=process, stat_reader=self.unavailable_process_stat)
            for session in (self.session, other):
                rows = worker_runs.observe_session(session, {"worker-one": "idle"}, {}, readings)
                self.assertEqual(rows["worker-one"]["state"], "waiting on run")
            readings.save()
        self.assertEqual(process.call_count, 1)

    def test_finished_or_absent_runs_never_read_processes_or_write_readings(self) -> None:
        for finished in (False, True):
            with self.subTest(finished=finished):
                if finished:
                    _ = self.run_record(status="finished", delivery="sent to worker")
                with patch.object(worker_runs, "read_process_table", side_effect=AssertionError("unexpected ps")) as process:
                    readings = worker_runs.BoardRunReadings(self.board, 1_100, process_reader=process, stat_reader=self.unavailable_process_stat)
                    rows = worker_runs.observe_session(self.session, {"worker-one": "idle"}, {}, readings)
                    readings.save()
                self.assertEqual(rows["worker-one"]["state"], "idle")
                self.assertEqual(process.call_count, 0)
                self.assertFalse((self.board / "run_readings.json").exists())

    def test_pane_activity_has_priority_and_missing_run_pid_is_explicit(self) -> None:
        _ = self.run_record()
        for pane_state in ("working", "running", "starting", "idle", "stopped"):
            with self.subTest(pane=pane_state):
                readings = worker_runs.BoardRunReadings(self.board, 1_100, process_reader=lambda: {}, stat_reader=self.unavailable_process_stat)
                rows = worker_runs.observe_session(self.session, {"worker-one": pane_state}, {}, readings)
                self.assertEqual(rows["worker-one"]["state"],
                                 "working" if pane_state in ("working", "running", "starting") else "run gone")
                if pane_state in ("idle", "stopped"):
                    self.assertIsNotNone(rows["worker-one"].get("run"))

    def test_log_tail_excludes_earlier_output_and_removes_terminal_sequences(self) -> None:
        log = self.run_record()
        _ = log.write_text("outside the tail\n" + "x" * 8192, encoding="utf-8")
        table = worker_runs.parse_process_table("120 1 00:01\n")
        readings = worker_runs.BoardRunReadings(self.board, 1_100, process_reader=lambda: table, stat_reader=self.unavailable_process_stat)
        rows = worker_runs.observe_session(self.session, {"worker-one": "idle"}, {}, readings)
        run = rows["worker-one"].get("run")
        assert run is not None
        self.assertEqual(run["last_line"], "x" * 4096)
        _ = log.write_text("old\n\x1b]0;window title\x07\x1b[31mlatest\x1b[0m\n \n", encoding="utf-8")
        readings = worker_runs.BoardRunReadings(self.board, 1_110, process_reader=lambda: table, stat_reader=self.unavailable_process_stat)
        rows = worker_runs.observe_session(self.session, {"worker-one": "idle"}, {}, readings)
        run = rows["worker-one"].get("run")
        assert run is not None
        self.assertEqual(run["last_line"], "latest")

    def test_runs_of_workers_outside_the_pane_make_no_process_call(self) -> None:
        _ = self.run_record(worker="other-worker")
        with patch.object(worker_runs, "read_process_table", side_effect=AssertionError("unshown run")) as process:
            readings = worker_runs.BoardRunReadings(self.board, 1_100, process_reader=process, stat_reader=self.unavailable_process_stat)
            for state in ("idle", "done", "stopped", "closed", "unknown", "incomplete"):
                rows = worker_runs.observe_session(self.session, {"worker-one": state}, {}, readings)
                self.assertEqual(rows["worker-one"], {"state": state})
            readings.save()
        self.assertFalse((self.board / "run_readings.json").exists())

    def test_queued_result_clears_only_after_that_workers_next_pass(self) -> None:
        _ = self.run_record(status="finished", delivery="queued for director", exit_code=1)
        cases: tuple[tuple[dict[str, float], str], ...] = (
            ({}, "result queued"), ({"worker-one": 1_080}, "result queued"),
            ({"other-worker": 1_095}, "result queued"), ({"worker-one": 1_095}, "idle"),
        )
        for passes, expected in cases:
            with self.subTest(passes=passes):
                readings = worker_runs.BoardRunReadings(self.board, 1_100, process_reader=lambda: {}, stat_reader=self.unavailable_process_stat)
                rows = worker_runs.observe_session(self.session, {"worker-one": "idle"}, passes, readings)
                self.assertEqual(rows["worker-one"]["state"], expected)
                if expected == "result queued":
                    self.assertEqual(rows["worker-one"].get("run"), {
                        "label": "Check tests", "started_at": 1_000,
                        "growth": {"kind": "finished", "exit_code": 1}, "last_line": "starting",
                    })

    def test_newest_run_uses_staff_identity_instead_of_worker_name(self) -> None:
        _ = self.run_record("older", worker="worker-one", started=900, pid=119)
        _ = self.run_record("newer", worker="worker-one", status="finished", started=1_000,
                            delivery="sent to worker")
        _ = self.run_record("other", worker="worker-two", started=1_010, pid=121)
        table = worker_runs.parse_process_table("119 1 00:01\n121 1 00:02\n")
        readings = worker_runs.BoardRunReadings(self.board, 1_100, process_reader=lambda: table, stat_reader=self.unavailable_process_stat)
        rows = worker_runs.observe_session(self.session, {"worker-one": "idle", "worker-two": "idle"}, {}, readings)
        self.assertEqual(rows["worker-one"]["state"], "idle")
        self.assertEqual(rows["worker-two"]["state"], "waiting on run")
        self.assertEqual(rows["worker-two"].get("run"), {
            "label": "Check other", "started_at": 1_010,
            "growth": {"kind": "first_reading"}, "last_line": "starting",
        })

    def test_finished_run_readings_drop_when_another_run_remains_running(self) -> None:
        _ = self.run_record("one", worker="worker-one")
        _ = self.run_record("two", worker="worker-two", pid=121)
        table = worker_runs.parse_process_table("120 1 00:01\n121 1 00:02\n")
        readings = worker_runs.BoardRunReadings(self.board, 1_100, process_reader=lambda: table, stat_reader=self.unavailable_process_stat)
        _ = worker_runs.observe_session(self.session, {"worker-one": "idle", "worker-two": "idle"}, {}, readings)
        readings.save()
        _ = self.run_record("one", worker="worker-one", status="finished", delivery="sent")
        readings = worker_runs.BoardRunReadings(self.board, 1_110, process_reader=lambda: table, stat_reader=self.unavailable_process_stat)
        _ = worker_runs.observe_session(self.session, {"worker-one": "idle", "worker-two": "idle"}, {}, readings)
        readings.save()
        persisted = (self.board / "run_readings.json").read_text(encoding="utf-8")
        self.assertNotIn('"one"', persisted)
        self.assertIn('"two"', persisted)

    def test_separate_board_builds_keep_both_workers_measured_growth(self) -> None:
        _ = self.run_record("one", worker="worker-one")
        other = self.root / "other-session"
        (other / DETACHED_RUNS_DIR).mkdir(parents=True)
        with patch.object(self, "session", other):
            _ = self.run_record("two", worker="worker-two", pid=121)
        initial = worker_runs.parse_process_table("120 1 00:01\n121 1 00:02\n")
        first = worker_runs.BoardRunReadings(self.board, 1_100, process_reader=lambda: initial, stat_reader=self.unavailable_process_stat)
        second = worker_runs.BoardRunReadings(self.board, 1_100, process_reader=lambda: initial, stat_reader=self.unavailable_process_stat)
        _ = worker_runs.observe_session(self.session, {"worker-one": "idle"}, {}, first)
        _ = worker_runs.observe_session(other, {"worker-two": "idle"}, {}, second)
        first.save()
        second.save()

        later = worker_runs.parse_process_table("120 1 00:03\n121 1 00:05\n")
        first = worker_runs.BoardRunReadings(self.board, 1_110, process_reader=lambda: later, stat_reader=self.unavailable_process_stat)
        second = worker_runs.BoardRunReadings(self.board, 1_110, process_reader=lambda: later, stat_reader=self.unavailable_process_stat)
        first_rows = worker_runs.observe_session(self.session, {"worker-one": "idle"}, {}, first)
        second_rows = worker_runs.observe_session(other, {"worker-two": "idle"}, {}, second)
        first.save()
        second.save()

        for rows, worker, percent in ((first_rows, "worker-one", 20), (second_rows, "worker-two", 30)):
            with self.subTest(worker=worker):
                run = rows[worker].get("run")
                assert run is not None
                self.assertEqual(run["growth"], {
                    "kind": "measured", "cpu_percent": percent, "log_bytes_per_minute": 0,
                })
        saved = cast(dict[str, worker_runs.RunReading], cast(object, json.loads(
            (self.board / "run_readings.json").read_text(encoding="utf-8"))))
        self.assertEqual(set(saved), {"one", "two"})
        self.assertEqual((saved["one"]["at"], saved["two"]["at"]), (1_110, 1_110))

    def test_idle_board_keeps_another_boards_running_reading(self) -> None:
        _ = self.run_record()
        initial = worker_runs.parse_process_table("120 1 00:01\n")
        first = worker_runs.BoardRunReadings(self.board, 1_100, process_reader=lambda: initial, stat_reader=self.unavailable_process_stat)
        _ = worker_runs.observe_session(self.session, {"worker-one": "idle"}, {}, first)
        first.save()
        other = self.root / "idle-session"
        other.mkdir()
        idle = worker_runs.BoardRunReadings(self.board, 1_105, process_reader=lambda: {}, stat_reader=self.unavailable_process_stat)
        _ = worker_runs.observe_session(other, {"idle-worker": "idle"}, {}, idle)
        idle.save()

        later = worker_runs.parse_process_table("120 1 00:03\n")
        next_build = worker_runs.BoardRunReadings(self.board, 1_110, process_reader=lambda: later, stat_reader=self.unavailable_process_stat)
        rows = worker_runs.observe_session(self.session, {"worker-one": "idle"}, {}, next_build)
        run = rows["worker-one"].get("run")
        assert run is not None
        self.assertEqual(run["growth"], {
            "kind": "measured", "cpu_percent": 20, "log_bytes_per_minute": 0,
        })

    def test_finished_board_removes_only_its_own_reading(self) -> None:
        _ = self.run_record("one", worker="worker-one")
        _ = self.run_record("two", worker="worker-two", pid=121)
        table = worker_runs.parse_process_table("120 1 00:01\n121 1 00:02\n")
        first = worker_runs.BoardRunReadings(self.board, 1_100, process_reader=lambda: table, stat_reader=self.unavailable_process_stat)
        _ = worker_runs.observe_session(self.session, {"worker-one": "idle", "worker-two": "idle"}, {}, first)
        _ = self.run_record("one", worker="worker-one", status="finished", delivery="sent")
        finished = worker_runs.BoardRunReadings(self.board, 1_110, process_reader=lambda: {}, stat_reader=self.unavailable_process_stat)
        _ = worker_runs.observe_session(self.session, {"worker-one": "idle"}, {}, finished)
        first.save()
        finished.save()

        saved = cast(dict[str, worker_runs.RunReading], cast(object, json.loads(
            (self.board / "run_readings.json").read_text(encoding="utf-8"))))
        self.assertEqual(set(saved), {"two"})
        self.assertEqual(saved["two"]["at"], 1_100)

    def test_idle_board_expires_only_readings_older_than_ten_minutes(self) -> None:
        _ = self.run_record("old", worker="worker-one")
        _ = self.run_record("recent", worker="worker-two", pid=121)
        table = worker_runs.parse_process_table("120 1 00:01\n121 1 00:02\n")
        old = worker_runs.BoardRunReadings(self.board, 1_000, process_reader=lambda: table, stat_reader=self.unavailable_process_stat)
        _ = worker_runs.observe_session(self.session, {"worker-one": "idle"}, {}, old)
        old.save()
        recent = worker_runs.BoardRunReadings(self.board, 1_590, process_reader=lambda: table, stat_reader=self.unavailable_process_stat)
        _ = worker_runs.observe_session(self.session, {"worker-two": "idle"}, {}, recent)
        recent.save()
        idle = worker_runs.BoardRunReadings(self.board, 1_601, process_reader=lambda: {}, stat_reader=self.unavailable_process_stat)
        idle.save()

        saved = cast(dict[str, worker_runs.RunReading], cast(object, json.loads(
            (self.board / "run_readings.json").read_text(encoding="utf-8"))))
        self.assertEqual(set(saved), {"recent"})
        self.assertEqual(saved["recent"]["at"], 1_590)

    def test_pane_queued_delivery_shows_the_finished_result(self) -> None:
        _ = self.run_record(status="finished", delivery="queued: worker has no live holder", exit_code=1)
        readings = worker_runs.BoardRunReadings(self.board, 1_100, process_reader=lambda: {}, stat_reader=self.unavailable_process_stat)
        rows = worker_runs.observe_session(self.session, {"worker-one": "idle"}, {}, readings)
        self.assertEqual(rows["worker-one"], {
            "state": "result queued",
            "run": {"label": "Check tests", "started_at": 1_000,
                    "growth": {"kind": "finished", "exit_code": 1}, "last_line": "starting"},
        })

    def test_failed_process_snapshot_keeps_waiting_and_preserves_the_last_good_reading(self) -> None:
        _ = self.run_record()
        failures = (subprocess.CompletedProcess(["ps"], 1, "", "process table unavailable"),
                    OSError("cannot start ps"))
        for failure in failures:
            with self.subTest(failure=str(failure)):
                initial = worker_runs.parse_process_table("120 1 00:01\n")
                first = worker_runs.BoardRunReadings(self.board, 1_100, process_reader=lambda: initial, stat_reader=self.unavailable_process_stat)
                _ = worker_runs.observe_session(self.session, {"worker-one": "idle"}, {}, first)
                first.save()
                before = (self.board / "run_readings.json").read_bytes()
                with patch.object(subprocess, "run", side_effect=failure if isinstance(failure, OSError) else None,
                                  return_value=failure):
                    snapshot = worker_runs.read_process_table()
                failed = worker_runs.BoardRunReadings(self.board, 1_110, process_reader=lambda: snapshot, stat_reader=self.unavailable_process_stat)
                rows = worker_runs.observe_session(self.session, {"worker-one": "idle"}, {}, failed)
                failed.save()
                self.assertEqual(rows["worker-one"]["state"], "waiting on run")
                run = rows["worker-one"].get("run")
                assert run is not None
                self.assertEqual(run["growth"], {"kind": "first_reading"})
                self.assertEqual((self.board / "run_readings.json").read_bytes(), before)

                later = worker_runs.parse_process_table("120 1 00:03\n")
                recovered = worker_runs.BoardRunReadings(self.board, 1_120, process_reader=lambda: later, stat_reader=self.unavailable_process_stat)
                rows = worker_runs.observe_session(self.session, {"worker-one": "idle"}, {}, recovered)
                run = rows["worker-one"].get("run")
                assert run is not None
                self.assertEqual(run["growth"], {
                    "kind": "measured", "cpu_percent": 10, "log_bytes_per_minute": 0,
                })

    def test_reaped_child_cpu_grows_when_ps_times_do_not_change_between_builds(self) -> None:
        _ = self.run_record()
        table = worker_runs.parse_process_table("120 1 00:01\n121 120 00:02\n122 121 00:03\n999 1 59:59\n")
        stat_lines = {
            120: self.process_stat(120, user=100, system=200, child_user=300, child_system=400),
            121: self.process_stat(121, user=200, system=100, child_user=0, child_system=0),
            122: self.process_stat(122, user=100, system=100, child_user=0, child_system=0),
        }
        read_pids: list[int] = []

        def stat_reader(pid: int) -> worker_runs.ProcessCpuWithReapedChildren | worker_runs.ProcessStatUnavailable:
            read_pids.append(pid)
            return worker_runs.parse_process_stat(stat_lines[pid], 100)

        first = worker_runs.BoardRunReadings(self.board, 1_100, process_reader=lambda: table, stat_reader=stat_reader)
        rows = worker_runs.observe_session(self.session, {"worker-one": "idle"}, {}, first)
        first.save()
        self.assertEqual(sorted(read_pids), [120, 121, 122])
        first_run = rows["worker-one"].get("run")
        assert first_run is not None
        self.assertEqual(first_run["growth"], {"kind": "first_reading"})
        saved = cast(dict[str, worker_runs.RunReading], cast(object, json.loads(
            (self.board / "run_readings.json").read_text(encoding="utf-8"))))
        self.assertEqual(saved["tests"]["cpu_seconds"], 15)

        stat_lines[120] = self.process_stat(120, user=100, system=200, child_user=600, child_system=500)
        stat_lines[121] = self.process_stat(121, user=200, system=100, child_user=100, child_system=100)
        read_pids.clear()
        second = worker_runs.BoardRunReadings(self.board, 1_110, process_reader=lambda: table, stat_reader=stat_reader)
        rows = worker_runs.observe_session(self.session, {"worker-one": "idle"}, {}, second)
        second.save()

        self.assertEqual(rows["worker-one"]["state"], "waiting on run")
        run = rows["worker-one"].get("run")
        assert run is not None
        self.assertEqual(run["growth"], {
            "kind": "measured", "cpu_percent": 60, "log_bytes_per_minute": 0,
        })
        self.assertEqual(sorted(read_pids), [120, 121, 122])

    def test_unavailable_stat_uses_only_that_pids_ps_cpu_time(self) -> None:
        table = worker_runs.parse_process_table("120 1 00:01\n121 120 00:02\n122 121 00:03\n999 1 59:59\n")
        stat_lines = {
            120: self.process_stat(120, user=100, system=200, child_user=300, child_system=400),
            121: self.process_stat(121, user=200, system=400, child_user=600, child_system=800),
            122: self.process_stat(122, user=300, system=600, child_user=900, child_system=1200),
        }
        for unavailable, expected in (((120,), 51), ((121,), 42), ((122,), 33), ((120, 121, 122), 6)):
            with self.subTest(unavailable=unavailable):
                read_pids: list[int] = []

                def stat_reader(pid: int) -> worker_runs.ProcessCpuWithReapedChildren | worker_runs.ProcessStatUnavailable:
                    read_pids.append(pid)
                    if pid in unavailable:
                        return worker_runs.ProcessStatUnavailable("stat disappeared or cannot be read")
                    return worker_runs.parse_process_stat(stat_lines[pid], 100)

                self.assertEqual(worker_runs.tree_cpu_seconds(table, 120, stat_reader=stat_reader), expected)
                self.assertEqual(sorted(read_pids), [120, 121, 122])
