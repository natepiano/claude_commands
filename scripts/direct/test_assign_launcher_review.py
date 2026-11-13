"""Check named assignment review with the shared loopback fixture."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from collections.abc import Callable
from typing import cast, final, override
from unittest.mock import patch
from scripts.agents import codex_test_support as codex_fixture
from scripts.astra import astra_roles
from scripts.astra.design_role_test_support import HeldRolePaneCase
from scripts.direct import assign, progress_history, recorder_state
from scripts.production import staff, worker
from scripts.herdr import turn_end, worker_panes
from scripts.production.cli_fixture import invoke_cli
from scripts.direct.assign_launcher_test_support import ROOT, read_object
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.git_test_support import commit_repository

setUpModule = codex_fixture.setUpModule



@final
class PaneAstraReviewTests(HeldRolePaneCase):
    """Run the assignment entry point with real worker and transcript paths."""

    @override
    def setUp(self) -> None:
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"HERDR_BIN": sys.executable})))
        super().setUp()
        from scripts.lib import unit_runs

        _ = self.enterContext(patch.object(unit_runs, "ASTRA_RUN_ROOT", self.panes.root))
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"ASTRA_REVIEW_DIR": str(self.panes.root)})))
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": ""})))
        self.panes.command_case.agent.final = "Verdict: clear."
        _ = self.enterContext(self.panes.world.process_starts())
        run_process = cast(Callable[..., subprocess.CompletedProcess[str]], subprocess.run)

        def run_worker_command(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
            if len(command) > 1 and Path(command[1]).resolve() == ROOT / "scripts/production/worker.py":
                result = invoke_cli(worker, lambda: worker.main(command[2:]), command[2:],
                                    cast(dict[str, str], options["env"]), ROOT)
                if not options.get("capture_output"):
                    print(result.stdout + result.stderr, end="")
                return result
            return run_process(command, **options)

        _ = self.enterContext(patch.object(subprocess, "run", side_effect=run_worker_command))
        _ = self.enterContext(patch.object(assign, "require_live_director"))

    def run_review(self, run_name: str = "astra-run") -> subprocess.CompletedProcess[str]:
        run = self.panes.root / run_name
        run.mkdir(exist_ok=True)
        return invoke_cli(assign, assign.main, ["--role", "review", "--operation", "astra-review",
            "--new-worker", "astra-reviewer", "--session-dir", str(run), "--cwd", str(self.panes.root),
            "--prompt", str(self.panes.command_case.prompt), "--summary", str(run / "review.md")],
            os.environ.copy(), ROOT)

    def test_named_review_done_records_the_status_style_and_one_notice(self) -> None:
        project = self.panes.root / "project"
        project.mkdir()
        _ = subprocess.run(["git", "init", "-q", str(project)], check=True, capture_output=True)
        write_text_atomic(project / "design.md", "The first design.\n")
        baseline = commit_repository(project, "Record the first design.", "design.md")
        write_text_atomic(project / "design.md", "The next design.\n")
        case = self.panes.command_case
        write_text_atomic(case.prompt, "Phase 8 — Workers\nReview the assigned change.\n")
        opened = case.opened()
        _ = progress_history.begin_dispatch(case.run_dir, slot="impl", phase="8", role="impl",
            worker_name="impl", worker_id=opened.row.id, launcher_command=["implementation"],
            prompt=case.prompt, kind="impl", lens="")
        for lens, style in (("adversary", False), ("contract", True)):
            with self.subTest(lens=lens, style=style):
                name = "models-review-" + lens
                case.agent.final = ("<!-- phase-style-review:start -->\nStyle review: Pass\n"
                                    "<!-- phase-style-review:end -->\nAPPROVE")
                arguments = ["--role", "review", "--to", name, "--lens", lens, "--pass", "1",
                             "--phase", "8", "--session-dir", str(case.run_dir), "--cwd", str(project),
                             "--prompt", str(case.prompt)]
                if style:
                    arguments += ["--style-since", baseline]
                notices = len(case.agent.notices)
                result = invoke_cli(assign, assign.main, arguments, os.environ.copy(), project)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                pane = case.agent.submitted[-1][0]
                row = case.agent.row(pane)
                active = progress_history.active_dispatch(row.id, case.run_dir)
                assert isinstance(active, progress_history.ActiveDispatch)
                for expected in (turn_end.TurnOutcome.DONE, turn_end.TurnOutcome.IGNORED):
                    self.assertEqual(case.finish(name), expected)
                    self.assertEqual(len(case.agent.notices), notices + 1)
                passed = worker_panes.worker_pass(row, case.run_dir)
                self.assertEqual(passed["status"], "completed")
                delivery = progress_history.dispatch_delivery(passed)
                self.assertEqual(Path(delivery.actions.status_file).read_text(), "done\n")
                self.assertTrue(Path(delivery.reply_file).read_text().endswith(f"DONE {active.instance_id}\n"))
                self.assertIn(f"{name}: phase 8: done", case.agent.notices[-1])
                if style:
                    saved = read_object(case.run_dir / "phases/8/style_review_state.json")
                    self.assertEqual(saved["state"], "complete")
                    self.assertEqual(saved["in_flight"], False)

    def assert_review_lifetime(self, failed: bool) -> None:
        records: list[dict[str, object]] = []
        rows: list[staff.StaffRow] = []
        self.panes.command_case.agent.without_done = failed

        def observe() -> None:
            rows.extend(staff.find(kind="worker"))
            records.extend(cast(dict[str, object], json.loads(path.read_text()))
                           for path in (self.panes.root / "holds").glob("*.json"))

        self.panes.command_case.agent.during_prompt = observe
        if failed:
            def exit_after_reply(pane: str) -> None:
                _ = self.panes.world.foreground.pop(pane)
            self.panes.command_case.agent.after_complete = exit_after_reply
        holder_state = (self.panes.command_case.run_dir / "progress_history_state.json").read_bytes()
        result = self.run_review()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(records), 1)
        row = rows[0]
        self.assertEqual((row.name, row.parent), (
            "astra-reviewer", staff.ReportsTo(self.panes.command_case.fixture.parent.id)))
        self.assertEqual({key: value for key, value in records[0].items() if key not in ("held_at", "pid_start")}, {
            "staff_id": row.id, "pid": next(pid for pid, process in self.panes.world.processes.items()
                                            if process.identity.id == row.current_session)})
        self.assertEqual(len(self.panes.world.launches), 1)
        self.assertEqual(len(self.panes.command_case.agent.submitted), 1)
        run = self.panes.root / "astra-run"
        self.assertEqual(staff.by_id(row.id), row)
        self.assertEqual(self.panes.world.closed, [])
        self.assertEqual(len(list((self.panes.root / "holds").glob("*.json"))), 1)
        self.assertIsInstance(progress_history.active_dispatch(row.id, run), progress_history.ActiveDispatch)
        end = worker.end
        endings: list[str] = []

        def check_end(parent: staff.StaffId, name: staff.WorkerName, *,
                      lock: worker_panes.WorkerLockHold | worker_panes.WorkerLockAtUse = worker_panes.WorkerLockAtUse.ACQUIRE,
                      retire: bool = True) -> worker.ExitCode:
            passed = read_object(self.panes.root / "astra-run/progress_history_state.json")
            status = assign.object_map(assign.object_map(passed["pass"])[row.name])["status"]
            endings.append(str(status))
            self.assertEqual(status, "error" if failed else "completed")
            self.assertEqual(len(list((self.panes.root / "holds").glob("*.json"))), 1)
            return end(parent, name, lock=lock, retire=retire)

        with patch.object(worker, "end", side_effect=check_end):
            outcome = self.panes.command_case.finish(row.name)
        self.assertEqual(endings, ["error" if failed else "completed"])
        self.assertEqual(outcome, turn_end.TurnOutcome.NO_DONE if failed else turn_end.TurnOutcome.DONE)
        self.assertFalse(list((self.panes.root / "holds").glob("*.json")))
        self.assertIsNone(staff.by_id(row.id))
        self.assertEqual(len(self.panes.world.closed), 1)
        self.assertEqual((self.panes.command_case.run_dir / "progress_history_state.json").read_bytes(), holder_state)
        state = read_object(self.panes.root / "astra-run/progress_history_state.json")
        status = assign.object_map(assign.object_map(state["pass"])[row.name])["status"]
        run_status = "error" if failed else "completed"
        self.assertEqual(recorder_state.open_phase_ids(state), [])
        self.assertEqual(state["status"], run_status)
        events = [assign.object_map(cast(object, json.loads(line)))
                  for line in Path(assign.text(state["history_file"])).read_text().splitlines()]
        self.assertEqual([(event["event_type"], event["status"]) for event in events
                          if event["event_type"] in ("phase_finished", "run_finished")],
                         [("phase_finished", run_status), ("run_finished", run_status)])
        self.assertEqual([event["status"] for event in events if event["event_type"] == "pass_finished"],
                         [run_status])
        if not failed:
            reply = (self.panes.root / "astra-run/review.md").read_text().splitlines()
            self.assertEqual(reply[0], "Verdict: clear.")
            self.assertTrue(reply[-1].startswith("DONE "))
            self.assertEqual(status, "completed")
            repeated = self.run_review()
            self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
            self.assertEqual(len(self.panes.world.launches), 1)
            self.assertEqual(len(self.panes.command_case.agent.submitted), 1)
            self.assertEqual(staff.snapshot().find(kind="worker"), [])
            self.assertFalse(list((self.panes.root / "holds").glob("*.json")))
        else:
            self.assertEqual(status, "error")
            self.assertIn("no DONE", self.panes.command_case.agent.notices[-1])

    def test_pane_reviewer_writes_the_reply_and_ends_its_row_and_hold(self) -> None:
        self.assert_review_lifetime(False)

    def test_failed_pane_review_ends_its_row_and_hold(self) -> None:
        self.assert_review_lifetime(True)

    def test_failed_reviewer_setup_closes_only_its_new_pane(self) -> None:
        for failure, existing in (("hold error", False), ("hold unavailable", False),
                                  ("hold error", True), ("hold unavailable", True),
                                  ("close", False), ("dispatch", False), ("dispatch", True), ("incomplete", False)):
            with self.subTest(failure=failure, existing=existing):
                case = PaneAstraReviewTests("runTest")
                case.setUp()
                try:
                    commands = case.panes.command_case
                    world = case.panes.world
                    if existing:
                        opened = commands.opened("astra-reviewer", "astra-reviewer")
                        case.assertIsNotNone(staff.by_id(opened.row.id))
                    before = staff.snapshot()
                    rows: list[staff.StaffRow] = []
                    def hold(role: astra_roles.AstraRole, lifetime: astra_roles.ExecProcess,
                             staff_id: str = "") -> astra_roles.HeldRole | astra_roles.HoldWorkerUnavailable:
                        rows.extend(staff.find(kind="worker"))
                        if failure == "dispatch":
                            return real_hold(role, lifetime, staff_id)
                        if failure == "hold unavailable":
                            print("TEST hold unavailable", file=sys.stderr)
                            return astra_roles.HoldWorkerUnavailable("TEST hold unavailable")
                        raise ValueError("TEST hold unavailable")
                    real_hold = astra_roles.hold
                    real_launch = assign.launch
                    def launch(target: worker_panes.StaffWorker | worker_panes.DurableDesignRole,
                               policy: worker_panes.LaunchPolicy, *,
                               lock: worker_panes.WorkerLockHold | worker_panes.WorkerLockAtUse = worker_panes.WorkerLockAtUse.ACQUIRE,
                               ) -> worker_panes.WorkerStart:
                        result = real_launch(target, policy, lock=lock)
                        if failure == "incomplete":
                            assert isinstance(result, worker_panes.OpenWorker)
                            rows.append(result.row)
                            return worker_panes.IncompleteStart(result.row, result.pane, "TEST hold unavailable", result.created_pane)
                        return result
                    with (patch.object(astra_roles, "hold", side_effect=hold),
                          patch.object(assign, "launch", side_effect=launch)):
                        if failure == "close":
                            with (patch.object(world, "close_tab_confirmed", return_value=False),
                                  patch.object(world, "close_terminal_pane", return_value=False)):
                                result = case.run_review()
                        elif failure == "dispatch":
                            with patch.object(worker, "dispatch", return_value=worker.ExitCode.REFUSED):
                                result = case.run_review()
                        else:
                            result = case.run_review()
                    case.assertEqual(result.returncode, 2 if failure == "dispatch" else 8 if failure == "incomplete" else 1, result.stdout + result.stderr)
                    row, = rows
                    case.assertEqual(commands.agent.submitted, [])
                    if existing or failure == "close":
                        case.assertEqual(staff.by_id(row.id), before.by_id(row.id) if existing else row)
                        case.assertEqual(world.closed, [])
                    else:
                        case.assertIsNone(staff.by_id(row.id))
                        case.assertEqual(len(world.closed), 1)
                    if failure != "dispatch":
                        case.assertIn("TEST hold unavailable", result.stderr)
                    if failure == "close":
                        state = worker_panes.observe_worker(commands.fixture.parent.id, row, staff.snapshot())
                        assert isinstance(state, worker_panes.OpenWorker)
                        case.assertIn(f"anomaly: {state.pane}: cleanup failed:", result.stderr)
                        case.assertEqual(result.stderr.count("anomaly:"), 1)
                        case.assertIn("pane close is incomplete", result.stderr)
                finally:
                    case.doCleanups()

    def test_recording_failure_returns_an_error_and_attempts_remaining_finishes(self) -> None:
        run_process = cast(Callable[..., subprocess.CompletedProcess[str]], subprocess.run)
        cases = (("finish-phase", False), ("finish-phase", True), ("finish-run", False))
        for operation, dispatch_failed in cases:
            with self.subTest(operation=operation, dispatch_failed=dispatch_failed):
                def refuse_record(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
                    if len(command) > 2 and command[1] == assign.PROGRESS and command[2] == operation:
                        command = [*command, "--status", "recording-refused"]
                    return run_process(command, **options)

                def complete(pane: str) -> None:
                    if dispatch_failed:
                        _ = self.panes.world.foreground.pop(pane)

                self.panes.command_case.agent.without_done = dispatch_failed
                self.panes.command_case.agent.after_complete = complete
                run_name = operation + ("-dispatch-error" if dispatch_failed else "-review")
                with patch.object(subprocess, "run", side_effect=refuse_record):
                    result = self.run_review(run_name)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(self.panes.command_case.finish("astra-reviewer"), turn_end.TurnOutcome.ERROR)
                self.assertIn("recording-refused", self.panes.command_case.agent.notices[-1])
                if dispatch_failed:
                    self.assertIn("no DONE", self.panes.command_case.agent.notices[-1])
                state = read_object(self.panes.root / run_name / "progress_history_state.json")
                self.assertEqual(recorder_state.open_phase_ids(state), [])
                self.assertEqual(state.get("status"), None if operation == "finish-run" else "error")

    def test_failed_pane_end_returns_one_and_keeps_the_hold(self) -> None:
        result = self.run_review()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        row, = staff.snapshot().find(kind="worker")
        with patch.object(self.panes.world, "close_terminal_pane", return_value=False):
            self.assertEqual(self.panes.command_case.finish(row.name), turn_end.TurnOutcome.DONE)
        record = read_object(self.panes.root / "holds" / (str(row.id) + ".json"))
        self.assertEqual(record["staff_id"], row.id)
        self.assertEqual(self.panes.world.closed, [])
        self.assertEqual(len(self.panes.command_case.agent.submitted), 1)
        state = read_object(self.panes.root / "astra-run/progress_history_state.json")
        self.assertEqual(recorder_state.open_phase_ids(state), [])
        self.assertEqual(state["status"], "error")
        self.assertIn("pane close is incomplete", self.panes.command_case.agent.notices[-1])
