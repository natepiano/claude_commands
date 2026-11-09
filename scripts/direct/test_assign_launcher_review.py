"""Check named assignment review with the shared loopback fixture."""
from __future__ import annotations

from scripts.lib.test_models_registry import write_registry
import fcntl
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from collections.abc import Callable
from typing import cast, final, override
from unittest.mock import patch
from scripts.agents import codex_mesh, test_codex_mesh as mesh_fixture
from scripts.astra import astra_roles
from scripts.astra.design_role_test_support import HeldRolePaneCase
from scripts.direct import assign, workers
from scripts.production import session_staff, staff, worker
from scripts.production.cli_fixture import invoke_cli
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib import process_start
from scripts.production.staff_test_support import fixture_runtime_id, fixture_worker
from scripts.direct.assign_launcher_test_support import ROOT, read_object
from scripts.direct import assign_launcher_test_support as launcher_fixture

setUpModule = mesh_fixture.setUpModule

@final
class AssignReviewTests(launcher_fixture.AssignLauncherFixture):
    def test_account_move_reports_a_missing_dispatch_through_the_shared_reader(self) -> None:
        member = fixture_worker("recovering", staff.StaffId(os.environ["CODEX_THREAD_ID"]),
                                runtime_id=fixture_runtime_id("recovery-without-dispatch"))
        self.write_pool_roster({member.name: {"staff_id": member.id, "status": "failed"}})
        result = self.launch("review", "--operation", "resume-account-move", "--to", member.name, execution=launcher_fixture.InProcessAssignment())
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("resume refused: worker has no recorded dispatch", result.stderr)
        self.assertEqual(self.turn_starts, 0)

    def managed_astra_review(self) -> subprocess.CompletedProcess[str]:
        Path(os.environ["NOTIFIER_SESSIONS_DIR"]).mkdir(parents=True, exist_ok=True)
        with patch.dict(os.environ, {"PROCESS_TABLE_PS": "true"}):
            return self.launch("review", "--operation", "astra-review", "--new-worker", "astra-reviewer",
                               "--model", "gpt-6-astra", "--effort", "xhigh",
                               "--summary", str(self.session_dir / "astra-review.txt"),
                               "--log", str(self.session_dir / "astra-turn.log"), execution=launcher_fixture.InProcessAssignment())

    def test_a_managed_astra_review_publishes_and_holds_one_read_only_reviewer(self) -> None:
        original = self.server.respond
        held: list[astra_roles.LiveRole] = []
        published: list[staff.StaffRow] = []
        before = self.events()

        @self.hang_guard.callback
        def observe_review(request: dict[str, object]) -> list[dict[str, object]] | None:
            frames = original(request)
            if request.get("method") == "turn/start":
                held.extend(astra_roles.live_roles(staff.snapshot()))
                published.extend(row for row in staff.snapshot().find(kind="worker") if row.name == "astra-reviewer")
                if frames is not None:
                    params = cast(dict[str, object], request["params"])
                    frames.insert(-1, {"jsonrpc": "2.0", "method": "item/completed", "params": {
                        "threadId": params["threadId"], "turnId": "turn-1",
                        "item": {"id": "review", "type": "agentMessage", "text": "The document passes."},
                    }})
            return frames

        self.server.respond = observe_review
        with patch.dict(os.environ, {"ASTRA_ROLES_STATE": str(self.session_dir / "astra-roles")}):
            result = self.managed_astra_review()
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(astra_roles.live_roles(staff.snapshot()), [])
        member, = published
        role, = held
        self.assertEqual((role["role"], role["staff_id"], role["thread_id"]),
                         ("reviewer", member.id, member.current_session))
        self.assertEqual(member.parent, staff.ReportsTo(staff.StaffId(os.environ["CODEX_THREAD_ID"])))
        self.assertEqual(self.events(), before)
        self.assertEqual(len(self.methods("thread/start")), 1)
        self.assertEqual(self.turn_starts, 1)
        self.assertEqual(cast(dict[str, object], self.methods("thread/start")[0]["params"])["sandbox"], "read-only")
        self.assertEqual(cast(dict[str, object], self.methods("turn/start")[0]["params"])["sandboxPolicy"],
                         {"type": "readOnly", "networkAccess": False})
        self.assertEqual(self.assignment_records()["astra-reviewer"]["status"], "ended")
        self.assertEqual((self.session_dir / "astra-review.txt").read_text().strip(), "The document passes.")

    def test_a_failed_managed_astra_turn_ends_its_worker_and_releases_its_role(self) -> None:
        self.outcomes = ["failed"]
        with patch.dict(os.environ, {"ASTRA_ROLES_STATE": str(self.session_dir / "astra-roles")}):
            result = self.managed_astra_review()
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertEqual(astra_roles.live_roles(staff.snapshot()), [])
        self.assertEqual(self.assignment_records()["astra-reviewer"]["status"], "ended")
        self.assertEqual(self.turn_starts, 1)

    def test_a_managed_astra_role_failure_ends_the_published_worker_before_any_turn(self) -> None:
        blocked = self.session_dir / "blocked-role-store"
        _ = blocked.write_text("The role store is unavailable.\n")
        with patch.dict(os.environ, {"ASTRA_ROLES_STATE": str(blocked)}):
            result = self.managed_astra_review()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("reviewer role unavailable", result.stderr)
        self.assertEqual(self.turn_starts, 0)
        self.assertEqual(self.assignment_records()["astra-reviewer"]["status"], "ended")

    def test_a_claude_review_for_a_named_worker_omits_the_follow_option(self) -> None:
        args = assign.Arguments(role="review", to="project-review", lens="contract", pass_number=2)
        environment = os.environ.copy()
        with patch("scripts.direct.assign.subprocess.run", return_value=subprocess.CompletedProcess([], 0)) as launched:
            code = assign.execute_claude(args, self.session_dir, "7", self.project, self.prompt, environment, "opus", "xhigh")
        self.assertEqual(code, 0)
        launched.assert_called_once_with(
            ["bash", str(assign.SCRIPTS / "review.sh"), "--phase", "7", str(self.session_dir),
             str(self.project), str(self.prompt), "review", "review", "review", "2", "contract", ""],
            env=environment, check=False)

    def test_a_follow_claim_publishes_only_its_launcher_and_worker(self) -> None:
        self.review_launcher("--new-worker", "project-review", "1")
        name = next(iter(self.assignment_records()))
        original = self.server.respond
        claims: list[dict[str, object]] = []
        starts: list[str] = []

        @self.hang_guard.callback
        def read_claim(request: dict[str, object]) -> list[dict[str, object]] | None:
            if request.get("method") == "turn/start":
                for path in self.session_dir.glob("impl_claim_*.json"):
                    claim = read_object(path)
                    claims.append(claim)
                    pid = claim["pid"]
                    assert isinstance(pid, int)
                    started = process_start.process_start(pid)
                    assert isinstance(started, process_start.Started)
                    starts.append(started.start)
            return original(request)

        self.server.respond = read_claim
        self.review_launcher("--to", name, "2")
        claim, = claims
        self.assertEqual(set(claim), {"pid", "started", "worker"})
        started, = starts
        self.assertEqual(claim["started"], started)
        self.assertEqual(claim["worker"], name)
        self.assertIsInstance(claim["pid"], int)

    def watch(self, path: Path) -> codex_mesh.LinuxFileChanges | codex_mesh.KqueueFileChanges:
        if sys.platform.startswith("linux"):
            return codex_mesh.LinuxFileChanges({path})
        return codex_mesh.KqueueFileChanges({path})

    def observe_follow_settlement(self, settled: Path, transport_override: str = "") -> None:
        """Mark the return from the real follow call."""
        self.instrument_launcher(
            "from pathlib import Path\nfrom scripts.direct import assign\n" +
            transport_override +
            "real_follow = assign.Dispatch.follow\n" +
            "def follow(dispatch, command, shown, log):\n" +
            "    result = real_follow(dispatch, command, shown, log)\n" +
            f"    Path({str(settled)!r}).touch()\n" +
            "    return result\n" +
            "assign.Dispatch.follow = follow\n")

    def review_after_stage_one(self) -> subprocess.CompletedProcess[str]:
        """Omit Stage 2 delivery after the first follow completes."""
        ready = self.session_dir.parent / "handoff.ready"
        settled = self.session_dir.parent / "stage-one-settled"
        reply = self.session_dir / "phases/7/project-review_reply_contract_1.txt"
        # A successful transport acknowledgment alone must not complete a review.
        # Keep the recorder, follower, and server real. Omit the send.
        transport = self.session_dir.parent / "codex_mesh.py"
        _ = transport.write_text(
            "import runpy, sys\n" +
            "if sys.argv[1] == 'send':\n" +
            "    print('queued transport acknowledgment without delivery')\n" +
            "else:\n" +
            f"    runpy.run_path({str(ROOT / 'scripts/agents/codex_mesh.py')!r}, run_name='__main__')\n")
        transport_override = f"assign.MESH = {str(transport)!r}\n"
        self.observe_follow_settlement(settled, transport_override)
        original = self.server.respond

        @self.hang_guard.callback
        def provisional_reply(request: dict[str, object]) -> list[dict[str, object]] | None:
            frames = original(request)
            if request.get("method") == "turn/start" and frames is not None:
                params = cast(dict[str, object], request["params"])
                frames.insert(-1, {"jsonrpc": "2.0", "method": "item/completed", "params": {
                    "threadId": params["threadId"], "turnId": "turn-1",
                    "item": {"id": "provisional", "type": "agentMessage", "text": "Stage 1 only."},
                }})
            return frames

        self.server.respond = provisional_reply
        roster_path = self.session_dir / codex_mesh.ROSTER_FILE
        watch = self.watch(settled)
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                launched = executor.submit(self.launch, "review", "--lens", "contract", "--pass", "1",
                                           "--early-ready", str(ready),
                                           execution=launcher_fixture.LauncherScript(launcher_fixture.ASSIGN))
                try:
                    while not settled.exists():
                        self.assertFalse(launched.done(), "Stage 1 completed the entire review")
                        event = self.hang_guard.change(watch.wait, self.lifecycle_clock.monotonic)
                        if event == codex_mesh.WaitDeadlineExpired.EXPIRED:
                            ready.touch()
                            stopped = self.hang_guard.result(launched)
                            self.fail(f"Stage 1 never settled: {stopped.stdout}{stopped.stderr}; " +
                                      f"roster={roster_path.read_text() if roster_path.exists() else 'missing'}")
                    self.assertFalse(launched.done(), "Stage 1 completed the entire review")
                    self.assertIn("Stage 1 only.", reply.read_text())
                    roster = self.assignment_records()
                    self.assertTrue(roster)
                    self.assertTrue(all(record.get("status") == "done" for record in roster.values()), roster)
                    self.assertEqual(self.turn_starts, 1)
                    self.assertFalse(any(event.get("event_type") == "pass_finished" for event in self.events()))
                    ready.touch()
                    result = self.hang_guard.result(launched)
                finally:
                    ready.touch()
        finally:
            watch.close()
        return result

    def test_an_early_review_receives_its_final_diff_once_after_the_ready_event(self) -> None:
        ready = self.session_dir.parent / "final.ready"
        self.outcomes = ["disconnect"]
        self.server.hold_turn_completion = True
        self.server.answer_requests_during_held_turn = True
        self.server.held_turn_completes_successfully = True
        original = self.server.respond
        deliveries: list[str] = []

        @self.hang_guard.callback
        def final_diff(request: dict[str, object]) -> list[dict[str, object]] | None:
            params = cast(dict[str, object], request.get("params", {}))
            if request.get("method") == "turn/start" and "Stage 2" in json.dumps(params):
                self.assertTrue(ready.exists(), "final diff was sent before its ready event")
                deliveries.append(json.dumps(params))
            frames = original(request)
            if request.get("method") == "turn/start" and self.turn_starts == 1 and frames is not None:
                self.live_turn = f"turn-{self.turn_starts}"
                frames.append({"jsonrpc": "2.0", "method": "item/completed", "params": {
                    "threadId": params["threadId"], "turnId": self.live_turn,
                    "item": {"id": "review-answer", "type": "agentMessage", "text": "Review complete."},
                }})
            return frames

        self.server.respond = final_diff
        with ThreadPoolExecutor(max_workers=1) as executor:
            launched = executor.submit(self.launch, "review", "--lens", "contract", "--pass", "1",
                                       "--early-ready", str(ready),
                                       execution=launcher_fixture.LauncherScript(launcher_fixture.ASSIGN))
            try:
                self.hang_guard.event(self.server.held_turn_ready)
                self.assertEqual(deliveries, [])
                phase = self.session_dir / "phases/7"
                self.assertEqual((phase / "project-review_status_contract").read_text().strip(), "reviewing")
                launcher_pid = int((phase / "project-review_pid_contract").read_text())
                os.kill(launcher_pid, 0)
                address = next(iter(self.assignment_records()))
                pending = session_staff.worker_inbox_path(self.session_dir, address)
                watch = self.watch(pending)
                try:
                    ready.touch()
                    while True:
                        if pending.exists():
                            with pending.open() as inbox:
                                fcntl.flock(inbox, fcntl.LOCK_SH)
                                if "Stage 2" in inbox.read():
                                    break
                        event = self.hang_guard.change(watch.wait, self.lifecycle_clock.monotonic)
                        self.assertNotEqual(event, codex_mesh.WaitDeadlineExpired.EXPIRED,
                                            "ready event delivered no Stage 2 message")
                finally:
                    watch.close()
                self.live_turn = ""
                self.server.hold_turn_completion = False
                self.server.release_held_turn.set()
                result = self.hang_guard.result(launched)
            finally:
                self.server.release_held_turn.set()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.session_dir / "phases/7/project-review_status_contract").read_text().strip(), "reviewed")
        self.assertEqual(len(deliveries), 1, deliveries)
        self.assertIn("Stage 2", deliveries[0])
        self.assertIn(str(ready.with_suffix(".diff")), deliveries[0])

    def test_an_early_review_that_finishes_stage_one_waits_for_stage_two_to_finish(self) -> None:
        ready = self.session_dir.parent / "final.ready"
        reply = self.session_dir / "phases/7/project-review_reply_contract_1.txt"
        status = reply.with_name("project-review_status_contract_1")
        settled = self.session_dir.parent / "stage-one-settled"
        self.observe_follow_settlement(settled)
        original = self.server.respond
        deliveries: list[str] = []

        @self.hang_guard.callback
        def hold_final_review(request: dict[str, object]) -> list[dict[str, object]] | None:
            params = cast(dict[str, object], request.get("params", {}))
            stage_two = request.get("method") == "turn/start" and "Stage 2:" in json.dumps(params)
            if stage_two:
                self.assertTrue(ready.exists())
                deliveries.append(json.dumps(params))
                self.outcomes = ["disconnect"]
                self.server.hold_turn_completion = True
                self.server.answer_requests_during_held_turn = True
                self.server.held_turn_completes_successfully = True
            frames = original(request)
            if stage_two and frames is not None:
                self.live_turn = f"turn-{self.turn_starts}"
                frames.append({"jsonrpc": "2.0", "method": "item/completed", "params": {
                    "threadId": params["threadId"], "turnId": self.live_turn,
                    "item": {"id": "final-review-answer", "type": "agentMessage", "text": "Final review."},
                }})
            elif request.get("method") == "turn/start" and self.turn_starts == 1 and frames is not None:
                frames.insert(-1, {"jsonrpc": "2.0", "method": "item/completed", "params": {
                    "threadId": params["threadId"], "turnId": "turn-1",
                    "item": {"id": "provisional-answer", "type": "agentMessage", "text": "Provisional review."},
                }})
            return frames

        self.server.respond = hold_final_review
        watch = self.watch(settled)
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                launched = executor.submit(self.launch, "review", "--lens", "contract", "--pass", "1",
                                           "--early-ready", str(ready),
                                           execution=launcher_fixture.LauncherScript(launcher_fixture.ASSIGN))
                try:
                    while not settled.exists():
                        self.assertFalse(launched.done(), "review exited before its final diff existed")
                        event = self.hang_guard.change(watch.wait, self.lifecycle_clock.monotonic)
                        self.assertNotEqual(event, codex_mesh.WaitDeadlineExpired.EXPIRED)
                    self.assertFalse(launched.done(), "Stage 1 alone completed the review")
                    self.assertEqual(deliveries, [])
                    self.assertIn("Provisional review.", reply.read_text())
                    self.assertFalse(any(event.get("event_type") in {"pass_started", "pass_finished"}
                                         for event in self.events()))
                    ready.touch()
                    self.hang_guard.event(self.server.held_turn_ready)
                    self.assertEqual(len([event for event in self.events()
                                          if event.get("event_type") == "pass_started"]), 1)
                    self.assertFalse(any(event.get("event_type") == "pass_finished" for event in self.events()))
                    self.assertEqual(status.read_text().strip(), "running")
                    self.live_turn = ""
                    self.server.release_held_turn.set()
                    result = self.hang_guard.result(launched)
                finally:
                    ready.touch()
                    self.server.release_held_turn.set()
        finally:
            watch.close()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(deliveries), 1, deliveries)
        self.assertEqual(self.turn_starts, 2)
        finished = [event for event in self.events() if event.get("event_type") == "pass_finished"]
        self.assertEqual(len(finished), 1, self.events())
        self.assertEqual(finished[0]["status"], "completed")
        self.assertEqual(status.read_text().strip(), "done")
        self.assertIn("Final review.", status.with_name("project-review_summary_contract_1.txt").read_text())

    def test_an_early_review_without_stage_two_delivery_records_error_instead_of_done(self) -> None:
        result = self.review_after_stage_one()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        folder = self.session_dir / "phases/7"
        self.assertEqual((folder / "project-review_status_contract_1").read_text().strip(), "error")
        self.assertEqual(self.turn_starts, 1)
        finished = [event for event in self.events() if event.get("event_type") == "pass_finished"]
        self.assertEqual(len(finished), 1, self.events())
        self.assertEqual(finished[0]["status"], "error")
        self.assertFalse(any(record.get("adopted_to") for record in self.assignment_records().values()))

    def test_a_review_launcher_follow_uses_the_current_review_row_instead_of_history(self) -> None:
        self.commit_project()
        _ = self.successful("review", "--new-worker", "project-review", "--lens", "contract", "--pass", "1",
                            execution=launcher_fixture.InProcessAssignment())
        name, record = next(iter(self.assignment_records().items()))
        self.assertEqual(record["turn_model"], "gpt-kept-sol")
        identity_path = self.identity_path(name)
        identity = read_object(identity_path)
        self.assertEqual(identity["model"], "gpt-kept-sol")
        self.assertEqual(workers.resolve(self.session_dir, "review", working_dir=self.project), name)
        reservation = workers.allocation_for_follow(self.session_dir, name)
        workers.release_allocation(self.session_dir, name, reservation)
        self.assertTrue(self.assignment_records()[name].get("unit_ledger"))
        _ = write_registry(self.registry, {"direct.codex": {"review": "gpt-other-sol:high"}}, update=True)
        _ = self.successful("review", "--to", name, "--lens", "contract", "--pass", "2",
                            execution=launcher_fixture.InProcessAssignment())
        self.assertEqual(self.assignment_records()[name]["turn_model"], "gpt-other-sol")
        turn = cast(dict[str, object], self.methods("turn/start")[-1]["params"])
        self.assertEqual(turn["model"], "gpt-other-sol")
        self.assertEqual(self.turn_starts, 2)
        self.assertEqual(read_object(identity_path), identity)


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

    def run_review(self) -> subprocess.CompletedProcess[str]:
        run = self.panes.root / "astra-run"
        run.mkdir(exist_ok=True)
        write_text_atomic(run / "worker_transport", "herdr\n")
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
