"""Detached runs explain a worker's apparent inactivity."""
from __future__ import annotations

import json
import io
from contextlib import redirect_stderr
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, override
from unittest.mock import patch

from scripts.agents.worker_lifecycle import DETACHED_RUNS_DIR
from scripts.agents.detached_runs import RunRecord
from scripts.direct import worker_runs
from scripts.production import staff
from scripts.production.staff_test_support import StaffTestCase, fixture_member, fixture_worker
from scripts.herdr import herdr, worker_panes
from scripts.agents import remote


class WorkerRunsTests(unittest.TestCase):
    root: Path = Path()
    session: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.session = self.root / "unit-session"
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

    def test_pane_activity_has_priority_and_missing_run_pid_is_explicit(self) -> None:
        _ = self.run_record()
        for pane_state in ("working", "running", "starting", "idle", "stopped"):
            with self.subTest(pane=pane_state):
                readings = worker_runs.WorkerRuns(1_100, process_reader=set)
                rows = worker_runs.observe_session(self.session, {"worker-one": pane_state}, {}, readings)
                self.assertEqual(rows["worker-one"]["state"],
                                 "working" if pane_state in ("working", "running", "starting") else "run gone")
                if pane_state in ("idle", "stopped"):
                    self.assertIsNotNone(rows["worker-one"].get("run"))

    def test_queued_result_clears_only_after_that_workers_next_pass(self) -> None:
        _ = self.run_record(status="finished", delivery="queued for director", exit_code=1)
        cases: tuple[tuple[dict[str, float], str], ...] = (
            ({}, "result queued"), ({"worker-one": 1_080}, "result queued"),
            ({"other-worker": 1_095}, "result queued"), ({"worker-one": 1_095}, "idle"),
        )
        for passes, expected in cases:
            with self.subTest(passes=passes):
                readings = worker_runs.WorkerRuns(1_100, process_reader=set)
                rows = worker_runs.observe_session(self.session, {"worker-one": "idle"}, passes, readings)
                self.assertEqual(rows["worker-one"]["state"], expected)
                if expected == "result queued":
                    self.assertEqual(rows["worker-one"].get("run"), {
                        "label": "Check tests", "started_at": 1_000,
                    })

    def test_newest_run_uses_staff_identity_instead_of_worker_name(self) -> None:
        _ = self.run_record("older", worker="worker-one", started=900, pid=119)
        _ = self.run_record("newer", worker="worker-one", status="finished", started=1_000,
                            delivery="sent to worker")
        _ = self.run_record("other", worker="worker-two", started=1_010, pid=121)
        table = {119, 121}
        readings = worker_runs.WorkerRuns(1_100, process_reader=lambda: table)
        rows = worker_runs.observe_session(self.session, {"worker-one": "idle", "worker-two": "idle"}, {}, readings)
        self.assertEqual(rows["worker-one"]["state"], "idle")
        self.assertEqual(rows["worker-two"]["state"], "waiting on run")
        self.assertEqual(rows["worker-two"].get("run"), {
            "label": "Check other", "started_at": 1_010,
        })

    def test_pane_queued_delivery_shows_the_finished_result(self) -> None:
        _ = self.run_record(status="finished", delivery="queued: worker has no live holder", exit_code=1)
        readings = worker_runs.WorkerRuns(1_100, process_reader=set)
        rows = worker_runs.observe_session(self.session, {"worker-one": "idle"}, {}, readings)
        self.assertEqual(rows["worker-one"], {
            "state": "result queued",
            "run": {"label": "Check tests", "started_at": 1_000},
        })

    def test_running_workers_share_one_process_snapshot_and_closed_workers_stay_closed(self) -> None:
        _ = self.run_record(worker="worker-one")
        _ = self.run_record("other", worker="worker-two", pid=121)
        reads: list[str] = []

        def processes() -> set[int]:
            reads.append("snapshot")
            return {120, 121}

        runs = worker_runs.WorkerRuns(1_100, process_reader=processes)
        states = worker_runs.observe_session(self.session,
            {"worker-one": "idle", "worker-two": "idle", "absent": "idle"}, {}, runs)
        self.assertEqual(states["worker-one"]["state"], "waiting on run")
        self.assertEqual(states["worker-two"]["state"], "waiting on run")
        self.assertEqual(states["absent"], {"state": "idle"})
        closed = worker_runs.observe_session(self.session, {"worker-one": "closed"}, {}, runs)
        self.assertEqual(closed["worker-one"], {"state": "closed"})
        self.assertEqual(reads, ["snapshot"])

    def test_open_passes_supply_phase_workers_without_panes(self) -> None:
        events: list[dict[str, object]] = [
            {"event_type": "phase_started", "phase_id": "8", "phase_instance_id": "eight"},
            {"event_type": "pass_started", "phase_instance_id": "eight", "pass_instance_id": "closed",
             "team_slot": "reviewer", "worker_id": "old-reviewer", "pass_kind": "review",
             "pass_started_at": 900},
            {"event_type": "pass_finished", "phase_instance_id": "eight", "pass_instance_id": "closed"},
            {"event_type": "pass_started", "phase_instance_id": "eight", "pass_instance_id": "repair",
             "team_slot": "repair", "worker_id": "worker-one", "pass_kind": "fix", "pass_started_at": 1_000},
            {"event_type": "pass_started", "phase_instance_id": "ten", "pass_instance_id": "verify",
             "team_slot": "verifier", "worker_id": "worker-two", "pass_kind": "test", "pass_started_at": 1_000},
        ]
        for detached in (False, True):
            with self.subTest(detached=detached):
                if detached:
                    _ = self.run_record(worker="worker-one")
                rows = worker_runs.phase_workers(self.session, events, "eight",
                    worker_runs.WorkerRuns(1_100, process_reader=lambda: {120}))
                self.assertEqual([(row["slot"], row["state"]) for row in rows],
                                 [("repair", "waiting on run" if detached else "working")])
                if detached:
                    self.assertEqual(rows[0]["run"], {"label": "Check tests", "started_at": 1_000})
        events.append({"event_type": "pass_finished", "phase_instance_id": "eight", "pass_instance_id": "repair"})
        self.assertEqual(worker_runs.phase_workers(self.session, events, "eight",
                         worker_runs.WorkerRuns(1_100, process_reader=set)), [])

    def test_phase_waits_keep_open_waits_and_the_latest_named_holder(self) -> None:
        events: list[dict[str, object]] = [
            {"event_type": "wait_started", "phase_instance_id": "eight", "pass_instance_id": "repair",
             "wait_id": "worker", "wait_reason": "memory admission", "wait_started_epoch": 1_000,
             "holder": {"state": "HolderNotNamed", "reason": "producer named no holder"}},
            {"event_type": "wait_started", "phase_instance_id": "ten", "wait_id": "unrelated",
             "wait_reason": "build queue", "wait_started_epoch": 900},
            {"event_type": "wait_started", "phase_instance_id": "eight", "wait_id": "director",
             "wait_reason": "build queue", "wait_started_epoch": 1_050,
             "holder": {"state": "NamedHolder", "resource_kind": "slot", "resource": "build",
                 "holder": {"state": "UnitHolder", "unit": "builder"}}},
        ]
        waits = worker_runs.phase_waits(events, "eight")
        self.assertEqual([(wait.reason, wait.started) for wait in waits],
                         [("memory admission", 1_000), ("build queue", 1_050)])
        self.assertIsInstance(waits[0].holder, worker_runs.UnnamedWaitHolder)
        self.assertIsInstance(waits[1].holder, worker_runs.NamedWaitHolder)
        assert isinstance(waits[1].holder, worker_runs.NamedWaitHolder)
        self.assertIn("builder", waits[1].holder.label)
        events.append({"event_type": "wait_holder_seen", "phase_instance_id": "eight", "wait_id": "worker",
            "holder": {"state": "NamedHolder", "resource_kind": "slot", "resource": "memory",
                "holder": {"state": "SessionHolder", "session_id": "builder-session"}}})
        waits = worker_runs.phase_waits(events, "eight")
        self.assertIsInstance(waits[0].holder, worker_runs.NamedWaitHolder)
        assert isinstance(waits[0].holder, worker_runs.NamedWaitHolder)
        self.assertIn("builder-session", waits[0].holder.label)
        events.extend([
            {"event_type": "wait_finished", "phase_instance_id": "eight", "wait_id": "worker"},
            {"event_type": "wait_unclosed", "phase_instance_id": "eight", "wait_id": "director"},
        ])
        self.assertEqual(worker_runs.phase_waits(events, "eight"), [])

    def test_a_failed_process_read_keeps_the_running_result_visible(self) -> None:
        _ = self.run_record()
        runs = worker_runs.WorkerRuns(1_100,
            process_reader=lambda: worker_runs.ProcessSnapshotFailed("fixture ps failed"))
        rows = worker_runs.observe_session(self.session, {"worker-one": "idle"}, {}, runs)
        self.assertEqual(rows["worker-one"], {"state": "waiting on run",
            "run": {"label": "Check tests", "started_at": 1_000}})


class PaneWorkerTests(StaffTestCase):
    def test_five_pane_states_keep_the_pane_and_the_reason(self) -> None:
        parent = fixture_member("unit", name="unit", parent=fixture_member("showrunner", name="runner").id,
            worktree=staff.PresentWorktree(remote.local_machine(), self.root))
        worker = fixture_worker("reader", parent.id)
        record = staff.snapshot()
        cases: tuple[tuple[worker_panes.WorkerPane, str, str], ...] = (
            (worker_panes.OpenWorker(worker, "pane-live", "terminal", herdr.AgentStatus.WORKING), "working", "pane-live"),
            (worker_panes.StoppedWorker(worker, "pane-stop", "terminal"), "stopped", "pane-stop"),
            (worker_panes.ClosedWorker(worker), "closed", ""),
            (worker_panes.ProcessUnknownWorker(worker, "pane-unknown", "terminal", "process unreadable"), "unknown", "process unreadable"),
            (worker_panes.UncertainWorker(worker, "pane read incomplete"), "incomplete", "pane read incomplete"),
        )
        for pane, expected, detail in cases:
            with (self.subTest(state=expected),
                  patch.object(worker_panes, "observe_parents", return_value={parent.id: [pane]}),
                  redirect_stderr(io.StringIO())):
                rows = worker_runs.pane_worker_rows(record)[parent.id]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["slot"], "reader")
            self.assertEqual(rows[0]["state"], expected)
            if detail:
                self.assertIn(detail, str(rows[0]["doing"]))

    def test_many_parents_use_one_pane_read_without_a_pin_write(self) -> None:
        from scripts.herdr.test_worker_panes import PaneWorld

        runner = fixture_member("showrunner", name="runner")
        world = PaneWorld()
        for index in range(2):
            parent = fixture_member("unit", name=f"unit-{index}", parent=runner.id,
                                    worktree=staff.PresentWorktree(remote.local_machine(), self.root / str(index)))
            worker = fixture_worker("reader", parent.id)
            world.add(worker, f"w1:p{index}")
            world.locations[f"w1:p{index}"]["agent_status"] = herdr.AgentStatus.WORKING
        record = staff.snapshot()
        with (patch.object(worker_panes, "_client", return_value=world),
              patch.object(herdr, "native_process", side_effect=world.native_process),
              patch.object(world, "panes", wraps=world.panes) as panes):
            rows = worker_runs.pane_worker_rows(record)
        self.assertEqual(len(rows), 2)
        self.assertEqual({str(row["doing"]) for group in rows.values() for row in group}, {"w1:p0", "w1:p1"})
        self.assertEqual({str(row["state"]) for group in rows.values() for row in group}, {"working"})
        self.assertEqual(panes.call_count, 1)

    def test_a_queued_result_follows_the_workers_staff_id_and_next_pass(self) -> None:
        parent = fixture_member("unit", name="unit", parent=fixture_member("showrunner", name="runner").id,
            worktree=staff.PresentWorktree(remote.local_machine(), self.root))
        worker = fixture_worker("reader", parent.id)
        session = self.root / "session"
        directory = session / "detached_runs"
        directory.mkdir(parents=True)
        record: RunRecord = {"run_id": "result", "status": "finished", "session_dir": str(session),
            "worker": "old-name", "staff_id": str(worker.id), "label": "Check", "command": ["true"],
            "log": str(directory / "result.log"), "started_at": 1000, "pid": 120,
            "director_dir": "", "done_message_file": "", "finished_at": datetime.fromtimestamp(1090, UTC).isoformat(),
            "exit_code": 0, "delivery": "queued: no live holder"}
        _ = (directory / "result.json").write_text(json.dumps(record))
        for later, expected in ((1080, "result queued"), (1095, "idle")):
            with self.subTest(later=later):
                rows = worker_runs.observe_pane_rows(session, [{"slot": "reader", "state": "idle"}], staff.snapshot(),
                    parent.id, [{"event_type": "pass_started", "worker_id": str(worker.id), "pass_started_at": later}],
                    worker_runs.WorkerRuns(1100, process_reader=set))
                self.assertEqual(rows[0]["state"], expected)
