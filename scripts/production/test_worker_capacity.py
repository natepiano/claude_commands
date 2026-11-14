"""Tests for the Codex worker stop: which workers stop, what ends, and who gets a report."""
from __future__ import annotations

import unittest
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import override
from unittest import mock

from scripts.agents import detached_runs, remote, restart
from scripts.direct import progress_history
from scripts.herdr import herdr, herdr_types, worker_panes
from scripts.production import broadcast, session_staff, showrunners, staff, worker_capacity
from scripts.production.staff_test_support import StaffTestCase, fixture_member, fixture_runtime_id, fixture_worker

FAULTS_TO = "fault-reader"
HEADING = "Codex workers have no capacity. This is the stop report for your Codex workers."
FAULT_HEADING = "Codex worker stop is incomplete. A Codex worker that still runs can spend credits."


class WorkerCapacityTests(StaffTestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        unset = staff.native_id(fixture_runtime_id("unset"))
        row = staff.StaffRow(unset, "worker", "unset", staff.StaffRoot(), current_session=unset)
        self.parent: staff.StaffRow = row
        self.codex: staff.StaffRow = row
        self.claude: staff.StaffRow = row
        self.run_dir: Path = Path()
        # The sweep reads `first` once. Each later observation of one worker comes from `later`, in order.
        self.first: list[worker_panes.WorkerPane] = []
        self.later: list[worker_panes.WorkerPane] = []
        self.sent: list[tuple[str, str, str]] = []
        self.lock: worker_panes.Kept | object = object()
        self.close: mock.MagicMock = mock.MagicMock()
        self.stop_row: mock.MagicMock = mock.MagicMock()
        self.active: mock.MagicMock = mock.MagicMock()
        self.cancel: mock.MagicMock = mock.MagicMock()
        self.live_runs: mock.MagicMock = mock.MagicMock()

    @override
    def setUp(self) -> None:
        super().setUp()
        runner = fixture_member("showrunner", name="capacity-runner")
        self.parent = fixture_member(
            "unit", name="capacity-unit", parent=runner.id, worktree=staff.PresentWorktree(remote.local_machine(), self.root))
        self.codex = fixture_worker("implementer", self.parent.id, backend=staff.CodexBackend(self.root / "codex"))
        self.claude = fixture_worker("review2", self.parent.id)
        self.run_dir = self.root / "run"
        self.sent = []
        self.lock = object()

        def record_send(session: str, sender: str, text: str, *options: str) -> str:
            del options
            self.sent.append((session, sender, text))
            return ""

        @contextmanager
        def lock(parent: staff.StaffId, name: staff.WorkerName, *, blocking: bool = False,
                 deadline: float = 0) -> Generator[object]:
            del blocking
            self.assertEqual((parent, name.value), (self.parent.id, "implementer"))
            self.assertGreater(deadline, 0)
            yield self.lock

        def observe_parents(parents: Sequence[staff.StaffRow], record: staff.StaffSnapshot) -> dict[staff.StaffId, list[worker_panes.WorkerPane]]:
            del parents, record
            return {self.parent.id: list(self.first)}

        def observe(parent: staff.StaffId, record: staff.StaffSnapshot) -> list[worker_panes.WorkerPane]:
            del parent, record
            return [self.later.pop(0)]

        _ = self.enterContext(mock.patch.object(session_staff, "pane_parents", return_value=[self.parent]))
        _ = self.enterContext(mock.patch.object(worker_panes, "observe_parents", side_effect=observe_parents))
        _ = self.enterContext(mock.patch.object(worker_panes, "observe", side_effect=observe))
        _ = self.enterContext(mock.patch.object(worker_panes, "parent_run_index", return_value={}))
        _ = self.enterContext(mock.patch.object(worker_panes, "worker_run", return_value=self.run_dir))
        _ = self.enterContext(mock.patch.object(worker_panes, "worker_lock", side_effect=lock))
        self.close = self.enterContext(mock.patch.object(worker_panes, "close", return_value=worker_panes.Closed()))
        self.stop_row = self.enterContext(mock.patch.object(restart, "stop_row", return_value=True))
        self.active = self.enterContext(mock.patch.object(
            progress_history, "active_dispatch", return_value=progress_history.NoActiveDispatch()))
        self.cancel = self.enterContext(mock.patch.object(progress_history, "cancel_worker_passes", return_value=1))
        self.live_runs = self.enterContext(mock.patch.object(detached_runs, "live_runs_for_staff", return_value=[]))
        _ = self.enterContext(mock.patch.object(broadcast, "send", side_effect=record_send))
        _ = self.enterContext(mock.patch.object(herdr, "shared_session", return_value="fixture-session"))
        _ = self.enterContext(mock.patch.object(herdr, "Herdr"))
        _ = self.enterContext(mock.patch.object(showrunners, "load_settings_from", return_value=showrunners.ShowrunnerSettings(
            threshold_percent=1, repeat_minutes=30, stall_minutes=5, faults_to=FAULTS_TO, always=[])))

    def stop(self, first: Sequence[worker_panes.WorkerPane], *later: worker_panes.WorkerPane) -> int:
        self.first = list(first)
        self.later = list(later)
        return worker_capacity.stop()

    def open_worker(self, row: staff.StaffRow | None = None, status: herdr.AgentStatus = herdr.AgentStatus.WORKING) -> worker_panes.OpenWorker:
        return worker_panes.OpenWorker(row or self.codex, "p1", "t1", status)

    def test_an_open_codex_worker_stops_and_its_pass_ends_and_its_director_gets_one_report(self) -> None:
        stopped = worker_panes.StoppedWorker(self.codex, "p1", "t1")
        for status in (herdr.AgentStatus.WORKING, herdr.AgentStatus.IDLE):
            with self.subTest(status=status):
                self.sent.clear()
                self.stop_row.reset_mock()
                self.close.reset_mock()
                opened = self.open_worker(status=status)
                self.assertEqual(self.stop([opened, self.open_worker(self.claude)], opened, stopped), 0)
                self.assertEqual([call.args[0] for call in self.stop_row.call_args_list], [self.codex])
                self.cancel.assert_called_with(self.run_dir, self.codex.id)
                self.close.assert_called_once_with(self.parent.id, staff.WorkerName("implementer"), lock=self.lock)
                self.assertEqual(self.sent, [(str(self.parent.id), worker_capacity.SENDER, "\n".join([
                    HEADING, "Stopped: implementer.",
                    "Passes ended as canceled: implementer. Continue that work yourself from the worktree."]))])

    def test_a_stop_that_passes_its_deadline_gets_a_second_attempt(self) -> None:
        opened = self.open_worker()
        self.stop_row.return_value = False
        self.assertEqual(self.stop([opened], opened, opened, worker_panes.ClosedWorker(self.codex)), 0)
        self.assertEqual(self.stop_row.call_count, 2)
        self.close.assert_not_called()
        self.assertIn("Stopped: implementer.", self.sent[0][2])

    def test_a_claude_worker_and_an_idle_closed_codex_worker_get_no_stop_and_no_report(self) -> None:
        self.assertEqual(self.stop([self.open_worker(self.claude), worker_panes.ClosedWorker(self.codex)]), 0)
        self.active.assert_called_once_with(self.codex.id, self.run_dir)
        self.stop_row.assert_not_called()
        self.cancel.assert_not_called()
        self.assertEqual(self.sent, [])

    def test_a_closed_codex_worker_with_an_open_pass_gets_its_pass_canceled(self) -> None:
        closed = worker_panes.ClosedWorker(self.codex)
        self.active.return_value = progress_history.ActiveDispatch("instance", ("launcher",))
        self.assertEqual(self.stop([closed], closed), 0)
        self.stop_row.assert_not_called()
        self.cancel.assert_called_once_with(self.run_dir, self.codex.id)
        self.assertEqual(self.sent, [(str(self.parent.id), worker_capacity.SENDER, "\n".join([
            HEADING, "Passes ended as canceled: implementer. Continue that work yourself from the worktree."]))])

    def test_a_worker_not_proved_stopped_keeps_its_pass_and_faults_go_to_the_fault_reader(self) -> None:
        opened = self.open_worker()
        unknown = worker_panes.ProcessUnknownWorker(self.codex, "p1", "t1", "process unreadable")
        cases: tuple[tuple[tuple[worker_panes.WorkerPane, ...], worker_panes.Kept | object, str], ...] = (
            ((opened, opened, opened), object(), "the stop did not prove that the worker stopped"),
            ((opened, unknown), object(), "process unreadable"),
            ((), worker_panes.Kept("dispatch lock is held"), "dispatch lock is held"),
        )
        for later, lock, reason in cases:
            with self.subTest(reason=reason):
                self.sent.clear()
                self.lock = lock
                self.assertEqual(self.stop([opened], *later), 1)
                self.cancel.assert_not_called()
                self.close.assert_not_called()
                self.assertEqual(self.sent, [
                    (str(self.parent.id), worker_capacity.SENDER, "\n".join([
                        HEADING, f"Not proved stopped: implementer: {reason}. Send it no work."])),
                    (FAULTS_TO, worker_capacity.SENDER, "\n".join([
                        FAULT_HEADING, f"capacity-unit: implementer: not proved stopped: {reason}"]))])

    def test_a_failed_stop_of_one_worker_does_not_end_the_sweep(self) -> None:
        other = fixture_worker("reader", self.parent.id, backend=staff.CodexBackend(self.root / "codex"))
        opened, stopped = self.open_worker(), worker_panes.StoppedWorker(other, "p2", "t2")
        seen: list[str] = []

        @contextmanager
        def lock(parent: staff.StaffId, name: staff.WorkerName, *, blocking: bool = False,
                 deadline: float = 0) -> Generator[object]:
            del parent, blocking, deadline
            seen.append(name.value)
            if name.value == "implementer":
                raise herdr_types.HerdrFailure("pane read failed", "unreadable")
            yield self.lock

        with mock.patch.object(worker_panes, "worker_lock", side_effect=lock):
            self.assertEqual(self.stop([opened, self.open_worker(other)], self.open_worker(other), stopped), 1)
        self.assertEqual(seen, ["implementer", "reader"])
        self.assertEqual([call.args[0] for call in self.stop_row.call_args_list], [other])
        self.assertIn("Stopped: reader.", self.sent[0][2])
        self.assertIn("Not proved stopped: implementer: pane read failed. Send it no work.", self.sent[0][2])
        self.assertEqual(self.sent[1][0], FAULTS_TO)

    def test_an_unreadable_pass_state_is_a_fault_and_a_live_detached_run_is_named(self) -> None:
        opened = self.open_worker()
        self.cancel.side_effect = SystemExit("unit state is unreadable")
        self.live_runs.return_value = [{"run_id": "run-7", "log": "/logs/run-7.log"}]
        self.assertEqual(self.stop([opened], opened, worker_panes.ClosedWorker(self.codex)), 1)
        self.live_runs.assert_called_once_with(self.run_dir / detached_runs.DETACHED_RUNS_DIR, frozenset({self.codex.id}))
        self.assertEqual(self.sent, [
            (str(self.parent.id), worker_capacity.SENDER, "\n".join([
                HEADING, "Stopped: implementer.", "Pass stays open: implementer: unit state is unreadable.",
                f"Detached run of implementer continues: run-7: result {self.run_dir / detached_runs.DETACHED_RUNS_DIR / 'run-7.json'}; "
                + "log /logs/run-7.log."])),
            (FAULTS_TO, worker_capacity.SENDER, "\n".join([
                FAULT_HEADING, "capacity-unit: implementer: pass not canceled: unit state is unreadable"]))])

    def test_a_report_that_is_not_delivered_fails_the_command(self) -> None:
        opened = self.open_worker()
        with mock.patch.object(broadcast, "send", return_value="no such session"):
            self.assertEqual(self.stop([opened], opened, worker_panes.ClosedWorker(self.codex)), 1)

    def test_main_accepts_only_stop(self) -> None:
        self.assertEqual(worker_capacity.main([]), 2)
        self.assertEqual(worker_capacity.main(["start"]), 2)
        self.assertEqual(worker_capacity.main(["stop"]), 0)


if __name__ == "__main__":
    _ = unittest.main()
