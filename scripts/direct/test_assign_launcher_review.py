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
from scripts.astra.design_role_test_support import HeldRolePaneCase
from scripts.direct import assign, recorder_state
from scripts.production import staff, worker
from scripts.production.cli_fixture import invoke_cli
from scripts.direct.assign_launcher_test_support import ROOT, read_object

setUpModule = codex_fixture.setUpModule



@final
class PaneAstraReviewTests(HeldRolePaneCase):
    """Run the assignment entry point with real worker and transcript paths."""

    @override
    def setUp(self) -> None:
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"HERDR_BIN": sys.executable})))
        super().setUp()
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": ""})))
        self.panes.command_case.agent.final = "Verdict: clear."
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
        self.assertEqual(result.returncode, worker.ExitCode.NO_DONE if failed else 0, result.stdout + result.stderr)
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(records), 1)
        row = rows[0]
        self.assertEqual((row.name, row.parent), (
            "astra-reviewer", staff.ReportsTo(self.panes.command_case.fixture.parent.id)))
        self.assertEqual({key: value for key, value in records[0].items() if key not in ("held_at", "pid_start")}, {
            "staff_id": row.id, "pid": os.getpid(), "session_dir": None, "worker": None})
        self.assertEqual(len(self.panes.world.launches), 1)
        self.assertEqual(len(self.panes.command_case.agent.submitted), 1)
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
        else:
            self.assertEqual(status, "error")
            self.assertIn("pending input is retained", result.stdout + result.stderr)

    def test_pane_reviewer_writes_the_reply_and_ends_its_row_and_hold(self) -> None:
        self.assert_review_lifetime(False)

    def test_failed_pane_review_ends_its_row_and_hold(self) -> None:
        self.assert_review_lifetime(True)

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
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("recording-refused", result.stderr)
                if dispatch_failed:
                    self.assertIn("pending input is retained", result.stdout)
                    self.assertEqual(result.returncode, worker.ExitCode.NO_DONE)
                state = read_object(self.panes.root / run_name / "progress_history_state.json")
                self.assertEqual(recorder_state.open_phase_ids(state), [])
                self.assertEqual(state["status"], "active" if operation == "finish-run" else "error")

    def test_failed_pane_end_returns_one_and_keeps_the_hold(self) -> None:
        def lose_process_evidence(pane: str) -> None:
            self.panes.world.unknown[pane] = "reviewer process is unreadable"
        self.panes.command_case.agent.after_complete = lose_process_evidence
        result = self.run_review()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        row, = staff.snapshot().find(kind="worker")
        record = read_object(self.panes.root / "holds" / (str(row.id) + ".json"))
        self.assertEqual(record["staff_id"], row.id)
        self.assertEqual(self.panes.world.closed, [])
        self.assertEqual(len(self.panes.command_case.agent.submitted), 1)
        state = read_object(self.panes.root / "astra-run/progress_history_state.json")
        self.assertEqual(recorder_state.open_phase_ids(state), [])
        self.assertEqual(state["status"], "error")
