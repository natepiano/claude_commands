"""Queue output uses one shared reading and explains each waiting step."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import fcntl
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
import unittest.mock
from unittest import mock
from typing import cast, override
from zoneinfo import ZoneInfo

from scripts.build_queue import build_queue
from scripts.lint import memory_admit

GIB = memory_admit.GIB
NOW = 1793214000.0


class QueueTests(unittest.TestCase):
    root: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            "BUILDLOG_DIR": str(self.root / "buildlog"),
            "BUILDLOG_PROC": str(self.root / "proc"),
            "BUILDLOG_MEMINFO": str(self.root / "meminfo"),
            "BUILDLOG_MEMORY_PRESSURE": str(self.root / "pressure"),
            "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"),
            "USER_ZONE": "America/New_York",
        })))
        (self.root / "proc").mkdir()
        _ = (self.root / "meminfo").write_text(
            f"MemAvailable: {int(23.1 * GIB) // 1024} kB\nMemTotal: {60 * GIB // 1024} kB\n")

    def test_waiting_row_names_head_and_age_with_local_time(self) -> None:
        with mock.patch.dict(os.environ, {"BUILDLOG_BUILD_BUDGET_GIB": "21"}):
            decision = memory_admit.decide(int(23.1 * GIB), 60 * GIB, int(10.9 * GIB),
                                           [memory_admit.MeasuredMemory(15 * GIB)])
        head = memory_admit.WaitingStep(
            10, "head-id", "clone88ci", "check", "/tmp/clone88", NOW - 2400,
            int(10.9 * GIB), memory_admit.head_wait_reason(decision, int(10.9 * GIB), int(23.1 * GIB),
                {"pid": 10, "start_time": "10", "need": int(10.9 * GIB), "since": NOW - 2400}, NOW),
        )
        follower = memory_admit.WaitingStep(
            11, "follower-id", "", "doc", "/tmp/tree", NOW - 300,
            int(11.6 * GIB), memory_admit.BehindHead("clone88ci", NOW - 2400),
        )
        view = memory_admit.QueueView(
            (head, follower), (), int(23.1 * GIB), 0, 3 * GIB, NOW,
        )
        lines = build_queue.render_queue(view, ZoneInfo("America/New_York"))
        output = "\n".join(lines)
        self.assertIn("| 1 | clone88ci | check | clone88 | 14:20 (40 min) | 10.9 GiB | "
                      + "head: needs 10.9 GiB; builds use 15.0 of the 21.0 GiB budget "
                      + "(starts when build use falls to 10.1 GiB); younger steps wait |", output)
        self.assertIn("| 2 | tree | doc | tree | 14:55 (5 min) | 11.6 GiB | "
                      + "needs 11.6 GiB; 23.1 free; behind clone88ci (waiting 40 min) |", output)
        self.assertNotIn("head-id", output)
        self.assertNotIn("follower-id", output)
        self.assertLess(lines.index("**Running**"), lines.index("**Waiting**"))

    def test_memory_reason_names_free_memory_and_earlyoom_reserve(self) -> None:
        pending = int(23.5 * GIB)
        waiting = memory_admit.WaitingStep(
            10, "waiting-id", "unit", "check", "/tmp/tree", NOW - 2400,
            int(11.6 * GIB), memory_admit.NeedsMemory(int(11.6 * GIB), int(23.1 * GIB)),
        )
        running = (
            memory_admit.RunningStep("one", "", "test", "/one", 12 * GIB, memory_admit.PromisedMemory(12 * GIB)),
            memory_admit.RunningStep("two", "", "test", "/two", int(11.5 * GIB), memory_admit.PromisedMemory(int(11.5 * GIB))),
            memory_admit.RunningStep("three", "", "test", "/three", 8 * GIB, memory_admit.MeasuredMemory(GIB)),
        )
        view = memory_admit.QueueView((waiting,), running, int(23.1 * GIB), pending, 3 * GIB, NOW)
        line = next(line for line in build_queue.render_queue(view, ZoneInfo("UTC")) if "earlyoom" in line)
        self.assertTrue(line.endswith(
            "needs 11.6 GiB; 23.1 free; 3.0 kept for earlyoom |"))

    def test_budget_reason_names_charged_builds_and_the_limit(self) -> None:
        waiting = memory_admit.WaitingStep(
            10, "waiting-id", "unit", "check", "/tmp/tree", NOW - 300,
            int(10.8 * GIB), memory_admit.NeedsBuildBudget(int(10.8 * GIB), int(17.6 * GIB), 20 * GIB),
        )
        view = memory_admit.QueueView((waiting,), (), 30 * GIB, 0, 3 * GIB, NOW)
        line = next(line for line in build_queue.render_queue(view, ZoneInfo("UTC")) if "budget" in line)
        self.assertTrue(line.endswith("needs 10.8 GiB; builds 17.6 of 20.0 GiB budget |"))

    def test_head_reason_names_measured_budget_limit_and_start_level(self) -> None:
        need, available = int(15.1 * GIB), int(19.5 * GIB)
        memory: list[memory_admit.StepMemory] = [memory_admit.MeasuredMemory(int(1.32 * GIB)) for _ in range(10)]
        with unittest.mock.patch.dict(os.environ, {"BUILDLOG_BUILD_BUDGET_GIB": "20"}):
            decision = memory_admit.decide(available, 60 * GIB, need, memory)
        waiting = memory_admit.WaitingStep(
            10, "waiting-id", "unit", "nextest", "/tmp/tree", NOW - 2400,
            need, memory_admit.head_wait_reason(decision, need, available,
                {"pid": 10, "start_time": "10", "need": need, "since": NOW - 2400}, NOW),
        )
        view = memory_admit.QueueView((waiting,), (), available, 0, 3 * GIB, NOW)
        line = next(line for line in build_queue.render_queue(view, ZoneInfo("UTC")) if "head:" in line)
        self.assertTrue(line.endswith("head: needs 15.1 GiB; builds use 13.2 of the 20.0 GiB budget "
                                      + "(starts when build use falls to 4.9 GiB); younger steps wait |"))

    def test_head_reason_names_only_free_memory_when_budget_passes(self) -> None:
        need, available = int(15.1 * GIB), 18 * GIB
        decision = memory_admit.decide(available, 60 * GIB, need, [memory_admit.MeasuredMemory(GIB)])
        waiting = memory_admit.WaitingStep(
            10, "waiting-id", "unit", "nextest", "/tmp/tree", NOW - 2400,
            need, memory_admit.head_wait_reason(decision, need, available,
                {"pid": 10, "start_time": "10", "need": need, "since": NOW - 2400}, NOW),
        )
        view = memory_admit.QueueView((waiting,), (), available, 0, 3 * GIB, NOW)
        line = next(line for line in build_queue.render_queue(view, ZoneInfo("UTC")) if "head:" in line)
        self.assertTrue(line.endswith("head: needs 18.1 GiB free; 18.0 free; younger steps wait |"))

    def test_head_backfill_label_uses_the_since_zone_and_becomes_strict_at_ten_minutes(self) -> None:
        need, available = int(15.1 * GIB), 18 * GIB
        decision = memory_admit.decide(available, 60 * GIB, need, [memory_admit.MeasuredMemory(GIB)])
        for waited, bump, suffix in ((540, 0, "younger steps may start until 15:01"),
                                      (600, 0, "younger steps wait"),
                                      (60, -1, "younger steps wait")):
            with self.subTest(waited=waited, bump=bump):
                row: memory_admit.Waiter = {"pid": 10, "start_time": "10", "need": need,
                                           "since": NOW - waited, "bump_order": bump}
                waiting = memory_admit.WaitingStep(
                    10, "waiting-id", "unit", "nextest", "/tmp/tree", row["since"], need,
                    memory_admit.head_wait_reason(decision, need, available, row, NOW))
                view = memory_admit.QueueView((waiting,), (), available, 0, 3 * GIB, NOW)
                line = next(line for line in build_queue.render_queue(view, ZoneInfo("America/New_York")) if "head:" in line)
                self.assertTrue(line.endswith("head: needs 18.1 GiB free; 18.0 free; " + suffix + " |"))

    def test_live_queue_pressure_reason_reaches_the_cli(self) -> None:
        process = self.root / "proc/100"
        process.mkdir()
        fields = ["S", "1", *(["0"] * 17), "100"]
        _ = (process / "stat").write_text(f"100 (verify) {' '.join(fields)}\n")
        directory = self.root / "buildlog/admission"
        directory.mkdir(parents=True)
        _ = (directory / "100.waiting").write_text(json.dumps({
            "pid": 100, "start_time": "100", "need": 8 * GIB, "since": NOW,
            "session_name": "unit", "step": "check", "folder": "/tree",
        }))
        _ = (self.root / "pressure").write_text("full avg10=23.4 avg60=1.0 total=1\n")
        output = io.StringIO()
        with mock.patch("sys.argv", ["build_queue.py"]), redirect_stdout(output):
            self.assertEqual(build_queue.main(), 0)
        self.assertIn("| 1 | unit | check | tree |", output.getvalue())
        self.assertIn("memory pressure: full avg10 23.4", output.getvalue())
        self.assertNotIn("swap", output.getvalue())
        self.assertNotIn("ready", output.getvalue())

    def test_unavailable_reading_does_not_claim_zero_free_memory(self) -> None:
        step = memory_admit.WaitingStep(
            10, "session", "unit", "check", "/tmp/tree", NOW - 300,
            12 * GIB, memory_admit.ReadingUnavailable(),
        )
        view = memory_admit.QueueView((step,), (), 0, 0, 0, NOW)
        line = next(line for line in build_queue.render_queue(view, ZoneInfo("UTC")) if "unavailable" in line)
        self.assertTrue(line.endswith("needs 12.0 GiB; memory reading unavailable |"))
        self.assertNotIn("0.0 free", line)

    def test_running_rows_show_signed_delta_and_totals(self) -> None:
        steps = (
            memory_admit.RunningStep("session-id", "unit", "lint", "/tmp/tree", 8 * GIB,
                                    memory_admit.MeasuredMemory(int(1.2 * GIB))),
            memory_admit.RunningStep("other-id", "link", "build", "/tmp/link", 4 * GIB,
                                    memory_admit.MeasuredMemory(5 * GIB)),
        )
        view = memory_admit.QueueView((), steps, 20 * GIB, 8 * GIB, GIB, NOW)
        output = "\n".join(build_queue.render_queue(view, ZoneInfo("UTC")))
        self.assertIn("| 1 | unit | lint | tree | 8.0 GiB | 1.2 GiB | 6.8 GiB | measured |", output)
        self.assertIn("| 2 | link | build | link | 4.0 GiB | 5.0 GiB | -1.0 GiB | measured |", output)
        self.assertIn("Totals: promised 12.0 GiB; measured 6.2 GiB; free 20.0 GiB; reserve 1.0 GiB", output)

    def test_pending_and_ended_steps_do_not_report_zero_measured_use(self) -> None:
        pending = memory_admit.RunningStep("first", "", "check", "/one", 8 * GIB, memory_admit.PromisedMemory(8 * GIB))
        ended = memory_admit.RunningStep("second", "", "test", "/two", 12 * GIB, memory_admit.GoneMemory())
        view = memory_admit.QueueView((), (pending, ended), 20 * GIB, 8 * GIB, GIB, NOW)
        output = "\n".join(build_queue.render_queue(view, ZoneInfo("UTC")))
        self.assertIn("| 1 | one | check | one | 8.0 GiB | — | — | starting |", output)
        self.assertNotIn("second", output)
        self.assertNotIn("two", output)
        self.assertIn("promised 8.0 GiB; measured 0.0 GiB", output)

    def test_empty_queue_reads_once_without_subprocess_or_wait(self) -> None:
        view = memory_admit.QueueView((), (), 20 * GIB, 0, GIB, NOW)
        output = io.StringIO()
        with mock.patch.object(memory_admit, "queue_view", return_value=view) as read, \
             mock.patch("subprocess.run", side_effect=AssertionError("no subprocess")), \
             mock.patch("time.sleep", side_effect=AssertionError("no wait")), \
             mock.patch("sys.argv", ["build_queue.py"]), redirect_stdout(output):
            self.assertEqual(build_queue.main(), 0)
        read.assert_called_once_with()
        self.assertEqual(output.getvalue(), (
            "**Running**\n\n"
            + "| # | Session | Step | Folder | Promised | Using | Delta | State |\n"
            + "| --- | --- | --- | --- | --- | --- | --- | --- |\n"
            + "| empty |  |  |  |  |  |  |  |\n\n"
            + "**Waiting**\n\n"
            + "| # | Session | Step | Folder | Waiting since | Needs | Reason |\n"
            + "| --- | --- | --- | --- | --- | --- | --- |\n"
            + "| empty |  |  |  |  |  |  |\n\n"
            + "Totals: promised 0.0 GiB; measured 0.0 GiB; free 20.0 GiB; reserve 1.0 GiB\n"))

    def test_one_row_per_table_uses_names_and_escapes_cell_text(self) -> None:
        waiting = memory_admit.WaitingStep(10, "hidden-id", "", "doc", "/tmp/hana/",
                                          NOW - 60, GIB, memory_admit.ReadyForAdmission())
        running = memory_admit.RunningStep("other-hidden-id", "lfo|trunk\nunit", "check", "/tmp/lfo",
                                          2 * GIB, memory_admit.MeasuredMemory(GIB))
        view = memory_admit.QueueView((waiting,), (running,), 20 * GIB, GIB, GIB, NOW)
        lines = build_queue.render_queue(view, ZoneInfo("UTC"))
        output = "\n".join(lines)
        self.assertIn("| 1 | lfo\\|trunk unit | check | lfo | 2.0 GiB | 1.0 GiB | 1.0 GiB | measured |", output)
        self.assertIn("| 1 | hana | doc | hana | 18:59 (1 min) | 1.0 GiB | "
                      + "needs 1.0 GiB; 20.0 free; ready for the next admission check |", output)
        self.assertEqual(sum(line.startswith("| 1 |") for line in lines), 2)
        self.assertNotIn("empty", output)
        self.assertNotIn("hidden-id", output)

    def test_cli_reports_admission_lock_timeout_with_status_two(self) -> None:
        directory = self.root / "buildlog/admission"
        directory.mkdir(parents=True)
        errors = io.StringIO()
        output = io.StringIO()
        with (directory / "lock").open("a+") as lock, \
             mock.patch.object(memory_admit, "QUEUE_LOCK_TIMEOUT_SECONDS", 0.03), \
             mock.patch("sys.argv", ["build_queue.py"]), \
             redirect_stderr(errors), redirect_stdout(output):
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.assertEqual(build_queue.main(), 2)
        self.assertEqual(errors.getvalue(), "build queue: admission lock busy for 0.03 s; try again\n")
        self.assertEqual(output.getvalue(), "")

    def test_ten_thousand_waiting_and_running_rows_render_in_under_one_second(self) -> None:
        waiting = tuple(memory_admit.WaitingStep(
            pid, f"session-{pid}", "", "check", f"/tmp/tree-{pid}", NOW - pid,
            12 * GIB, memory_admit.NeedsMemory(12 * GIB, 20 * GIB),
        ) for pid in range(10000))
        running = tuple(memory_admit.RunningStep(
            f"session-{pid}", "", "test", f"/tmp/tree-{pid}", 8 * GIB, memory_admit.MeasuredMemory(GIB),
        ) for pid in range(10000))
        view = memory_admit.QueueView(waiting, running, 20 * GIB, 10 * GIB, GIB, NOW)
        started = time.perf_counter()
        lines = build_queue.render_queue(view, ZoneInfo("America/New_York"))
        elapsed = time.perf_counter() - started
        self.assertEqual(sum(line.startswith("| ") for line in lines), 20004)
        self.assertEqual(sum(line.startswith("| 10000 | tree-9999 |") for line in lines), 2)
        self.assertLess(elapsed, 1.0, f"render took {elapsed:.6f} s")
        print(f"queue render: 20000 rows in {elapsed:.6f} s")

    def test_bump_passes_selector_and_caller_and_reports_ambiguous_match(self) -> None:
        errors = io.StringIO()
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "caller-id", "CODEX_THREAD_ID": ""}), \
             mock.patch.object(memory_admit, "bump_waiter", side_effect=ValueError("two folders match; use position")) as bump, \
             mock.patch("sys.argv", ["build_queue.py", "bump", "tree"]), redirect_stderr(errors):
            self.assertEqual(build_queue.main(), 2)
        bump.assert_called_once_with("tree", "caller-id")
        self.assertIn("two folders match; use position", errors.getvalue())

    def test_bump_cli_moves_the_step_and_names_the_actor_in_output_and_log(self) -> None:
        for pid in (100, 200):
            process = self.root / "proc" / str(pid)
            process.mkdir()
            fields = ["S", "1", *(["0"] * 17), str(pid)]
            _ = (process / "stat").write_text(f"{pid} (verify) {' '.join(fields)}\n")
        directory = self.root / "buildlog" / "admission"
        directory.mkdir(parents=True)
        actors = (
            ("claude-parent", "codex-worker", "codex-worker"),
            ("claude-session", "", "claude-session"),
            ("", "", "user:test-user"),
        )
        for index, (claude, codex, actor) in enumerate(actors, 1):
            with self.subTest(actor=actor):
                for pid, name, since in ((100, "head", NOW - 2400), (200, "warmup", NOW - 300)):
                    row: memory_admit.Waiter = {
                        "pid": pid, "start_time": str(pid), "need": 12 * GIB, "since": since,
                        "session_id": f"session-{pid}", "session_name": name,
                        "step": "check", "folder": f"/tmp/{name}",
                    }
                    _ = (directory / f"{pid}.waiting").write_text(json.dumps(row))
                output = io.StringIO()
                with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": claude,
                                                   "CODEX_THREAD_ID": codex, "USER": "test-user"}), \
                     mock.patch("time.time", return_value=NOW), \
                     mock.patch("sys.argv", ["build_queue.py", "bump", "2"]), redirect_stdout(output):
                    self.assertEqual(build_queue.main(), 0)
                    self.assertEqual(memory_admit.queue_view().waiting[0].pid, 200)
                self.assertEqual(output.getvalue(),
                                 f"{actor} moved warmup: check in /tmp/warmup to the head; memory limits still apply\n")
                rows = [cast(dict[str, object], json.loads(line))
                        for path in (self.root / "buildlog").glob("*/*.jsonl")
                        for line in path.read_text().splitlines()]
                self.assertEqual(len(rows), index)
                self.assertEqual({key: rows[-1][key] for key in ("kind", "actor", "session", "step", "folder", "old_position")}, {
                    "kind": "queue_bump", "actor": actor, "session": "session-200",
                    "step": "check", "folder": "/tmp/warmup", "old_position": 2,
                })


if __name__ == "__main__":
    _ = unittest.main()
