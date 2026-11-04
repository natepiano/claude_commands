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
from typing import cast, final
from scripts.agents import codex_mesh, test_codex_mesh as mesh_fixture
from scripts.direct import workers
from scripts.production import session_staff
from scripts.direct.assign_launcher_test_support import ASSIGN, ROOT, read_object
from scripts.direct import assign_launcher_test_support as launcher_fixture

setUpModule = mesh_fixture.setUpModule

@final
class AssignReviewTests(launcher_fixture.AssignLauncherFixture):
    def watch(self, path: Path) -> codex_mesh.LinuxFileChanges | codex_mesh.KqueueFileChanges:
        if sys.platform.startswith("linux"):
            return codex_mesh.LinuxFileChanges({path})
        return codex_mesh.KqueueFileChanges({path})

    def observe_follow_settlement(self, settled: Path, transport_override: str = "") -> Path:
        """Mark the return from the real follow call."""
        script = self.prepare_launcher()[0]
        _ = script.with_suffix(".py").write_text(
            "from pathlib import Path\nfrom scripts.direct import assign\n" +
            transport_override +
            "real_follow = assign.Dispatch.follow\n" +
            "def follow(dispatch, command, shown, log):\n" +
            "    result = real_follow(dispatch, command, shown, log)\n" +
            f"    Path({str(settled)!r}).touch()\n" +
            "    return result\n" +
            "assign.Dispatch.follow = follow\n" +
            "raise SystemExit(assign.main())\n")
        return script

    def review_after_stage_one(self) -> subprocess.CompletedProcess[str]:
        """Omit Stage 2 delivery after the first follow completes."""
        ready = self.session_dir.parent / "handoff.ready"
        settled = self.session_dir.parent / "stage-one-settled"
        reply = self.session_dir / "phases/7/review_reply_contract_1.txt"
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
        script = self.observe_follow_settlement(settled, transport_override)
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
                                           "--early-ready", str(ready), script=script)
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
                                       "--early-ready", str(ready), script=ASSIGN)
            try:
                self.hang_guard.event(self.server.held_turn_ready)
                self.assertEqual(deliveries, [])
                phase = self.session_dir / "phases/7"
                self.assertEqual((phase / "review_status_contract").read_text().strip(), "reviewing")
                launcher_pid = int((phase / "review_pid_contract").read_text())
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
        self.assertEqual((self.session_dir / "phases/7/review_status_contract").read_text().strip(), "reviewed")
        self.assertEqual(len(deliveries), 1, deliveries)
        self.assertIn("Stage 2", deliveries[0])
        self.assertIn(str(ready.with_suffix(".diff")), deliveries[0])

    def test_an_early_review_that_finishes_stage_one_waits_for_stage_two_to_finish(self) -> None:
        ready = self.session_dir.parent / "final.ready"
        reply = self.session_dir / "phases/7/review_reply_contract_1.txt"
        status = reply.with_name("review_status_contract_1")
        settled = self.session_dir.parent / "stage-one-settled"
        script = self.observe_follow_settlement(settled)
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
                                           "--early-ready", str(ready), script=script)
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
        self.assertIn("Final review.", status.with_name("review_summary_contract_1.txt").read_text())

    def test_an_early_review_without_stage_two_delivery_records_error_instead_of_done(self) -> None:
        result = self.review_after_stage_one()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        folder = self.session_dir / "phases/7"
        self.assertEqual((folder / "review_status_contract_1").read_text().strip(), "error")
        self.assertEqual(self.turn_starts, 1)
        finished = [event for event in self.events() if event.get("event_type") == "pass_finished"]
        self.assertEqual(len(finished), 1, self.events())
        self.assertEqual(finished[0]["status"], "error")
        self.assertFalse(any(record.get("adopted_to") for record in self.assignment_records().values()))

    def test_a_review_launcher_follow_uses_the_current_review_row_instead_of_history(self) -> None:
        _ = write_registry(self.registry, {"direct.codex": {"review": "gpt-other-sol:high"}}, update=True)
        self.commit_project()
        self.review_launcher("--slot", "review", "1")
        name, record = next(iter(self.assignment_records().items()))
        self.assertEqual(record["turn_model"], "gpt-other-sol")
        identity_path = self.identity_path(name)
        identity = read_object(identity_path)
        self.assertEqual(identity["model"], "gpt-kept-sol")
        self.assertEqual(workers.resolve(self.session_dir, "review"), name)
        reservation = workers.allocation_for_follow(self.session_dir, name)
        workers.release_allocation(self.session_dir, name, reservation)
        self.assertTrue(self.assignment_records()[name].get("unit_ledger"))
        self.review_launcher("--to", name, "2")
        turn = cast(dict[str, object], self.methods("turn/start")[-1]["params"])
        self.assertEqual(turn["model"], "gpt-other-sol")
        self.assertEqual(self.turn_starts, 2)
        self.assertEqual(read_object(identity_path), identity)
