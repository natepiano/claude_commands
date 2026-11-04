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
from scripts.direct.assign_launcher_test_support import ROOT, read_object
from scripts.direct import assign_launcher_test_support as launcher_fixture


setUpModule = mesh_fixture.setUpModule


@final
class AssignReviewTests(launcher_fixture.AssignLauncherFixture):
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

    def review_after_stage_one(self, *, adopt: bool) -> tuple[subprocess.CompletedProcess[str], Path]:
        """Change delivery ownership after the first follower releases its turn."""
        ready = self.session_dir.parent / "handoff.ready"
        settled = self.session_dir.parent / "stage-one-settled"
        destination = self.session_dir.parent / "taking-run"
        reply = self.session_dir / "phases/7/review_reply_contract_1.txt"
        transport_override = ""
        if not adopt:
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
                                           "--early-ready", str(ready))
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
                    if adopt:
                        with roster_path.open("r+") as handle:
                            fcntl.flock(handle, fcntl.LOCK_EX)
                            roster = cast(dict[str, dict[str, object]], json.load(handle))
                            name, record = next(iter(roster.items()))
                            destination.mkdir()
                            _ = (destination / codex_mesh.ROSTER_FILE).write_text(json.dumps({name: record.copy()}))
                            record["adopted_to"] = str(destination)
                            codex_mesh.advance_worker_roster_revision(record)
                            _ = handle.seek(0)
                            _ = handle.write(json.dumps(roster))
                            _ = handle.truncate()
                    ready.touch()
                    result = self.hang_guard.result(launched)
                finally:
                    ready.touch()
        finally:
            watch.close()
        return result, destination

    def review_adoption_at_boundary(self, boundary: str) -> subprocess.CompletedProcess[str]:
        """Change ownership at coordinator boundaries around real followers."""
        ready = self.session_dir.parent / "boundary.ready"
        trace = self.session_dir.parent / "follow-boundaries.jsonl"
        delivered = self.session_dir.parent / "boundary-delivered"
        destination = self.session_dir.parent / "boundary-destination"
        # The injected code changes the ownership boundary.
        # Real followers submit input, write receipts, and complete loopback server turns.
        self.instrument_launcher(
            "import fcntl, json\nfrom pathlib import Path\nfrom scripts.direct import assign\nfrom scripts.agents import worker_lifecycle\n" +
            f"boundary = {boundary!r}\nready = Path({str(ready)!r})\n" +
            f"trace = Path({str(trace)!r})\ndestination = Path({str(destination)!r})\n" +
            f"delivered = Path({str(delivered)!r})\n" +
            "real_follow = assign.Dispatch.follow\nreal_claim = assign.Dispatch.claim_follow\n" +
            "real_mesh_call = assign.Dispatch.mesh_call\n" +
            "real_read = assign.read_roster\nactive_dispatch = None\n" +
            "follow_count = 0\nclaim_count = 0\nadopted = False\n" +
            "def adopt(dispatch):\n" +
            "    global adopted\n" +
            "    if adopted: return\n" +
            "    adopted = True\n" +
            "    destination.mkdir()\n" +
            "    with worker_lifecycle.locked_state(dispatch.run / 'mesh_roster.json') as handle:\n" +
            "        roster = handle.read_object()\n" +
            "        record = roster[dispatch.worker]\n" +
            "        (destination / 'mesh_roster.json').write_text(json.dumps({dispatch.worker: record.copy()}))\n" +
            "        record['adopted_to'] = str(destination)\n" +
            "        assign.codex_mesh.advance_worker_roster_revision(record)\n" +
            "        handle.publish(json.dumps(roster))\n" +
            "    if boundary == 'before_inbox':\n" +
            "        pending = dispatch.run / (dispatch.worker + '.pending.json')\n" +
            "        with worker_lifecycle.locked_state(pending) as handle:\n" +
            "            (destination / pending.name).write_text(handle.read())\n" +
            "            handle.publish(json.dumps(worker_lifecycle.empty_inbox()))\n" +
            "    with trace.open('a') as handle: handle.write(json.dumps({'adopted': True}) + '\\n')\n" +
            "def follow(dispatch, command, shown, log):\n" +
            "    global follow_count, active_dispatch\n" +
            "    active_dispatch = dispatch\n" +
            "    follow_count += 1\n" +
            "    if boundary == 'after_claim' and follow_count == 2: adopt(dispatch)\n" +
            "    if boundary == 'first_claim' and follow_count == 1: adopt(dispatch)\n" +
            "    result = real_follow(dispatch, command, shown, log)\n" +
            "    with trace.open('a') as handle:\n" +
            "        handle.write(json.dumps({'follow': follow_count, 'code': result}) + '\\n')\n" +
            "    if result == 0:\n" +
            "        if boundary == 'combined' or boundary == 'completed' and follow_count == 2: adopt(dispatch)\n" +
            "        if follow_count == 1 and boundary != 'combined': ready.touch()\n" +
            "    return result\n" +
            "def claim(dispatch):\n" +
            "    global claim_count\n" +
            "    claim_count += 1\n" +
            "    if boundary == 'before_claim' and claim_count == 2: adopt(dispatch)\n" +
            "    return real_claim(dispatch)\n" +
            "def read(run, name):\n" +
            "    record = real_read(run, name)\n" +
            "    if boundary == 'before_inbox' and follow_count == 1 and active_dispatch is not None:\n" +
            "        adopt(active_dispatch)\n" +
            "    return record\n" +
            "def mesh_call(dispatch, verb, *options, **kwargs):\n" +
            "    result = real_mesh_call(dispatch, verb, *options, **kwargs)\n" +
            "    if verb == 'send' and result.returncode == 0: delivered.touch()\n" +
            "    return result\n" +
            "assign.Dispatch.follow = follow\nassign.Dispatch.claim_follow = claim\n" +
            "assign.read_roster = read\n" +
            "assign.Dispatch.mesh_call = mesh_call\n")
        original = self.server.respond

        @self.hang_guard.callback
        def final_reply(request: dict[str, object]) -> list[dict[str, object]] | None:
            frames = original(request)
            if request.get("method") == "turn/start" and frames is not None:
                params = cast(dict[str, object], request["params"])
                stage_two = "Stage 2:" in json.dumps(params)
                if boundary == "combined" and self.turn_starts == 1:
                    self.live_turn = "turn-1"
                answer: dict[str, object] = {"jsonrpc": "2.0", "method": "item/completed", "params": {
                    "threadId": params["threadId"], "turnId": f"turn-{self.turn_starts}",
                    "item": {"id": f"answer-{self.turn_starts}", "type": "agentMessage",
                             "text": "Final boundary review." if stage_two else "Provisional boundary review."}}}
                frames.insert(len(frames) if boundary == "combined" and self.turn_starts == 1 else -1, answer)
            return frames

        self.server.respond = final_reply
        if boundary == "combined":
            self.outcomes = ["disconnect"]
            self.server.hold_turn_completion = True
            self.server.answer_requests_during_held_turn = True
            self.server.held_turn_completes_successfully = True
            with ThreadPoolExecutor(max_workers=1) as executor:
                launched = executor.submit(self.launch, "review", "--lens", "contract", "--pass", "1",
                                           "--early-ready", str(ready))
                try:
                    self.hang_guard.event(self.server.held_turn_ready)
                    watch = self.watch(delivered)
                    try:
                        ready.touch()
                        while not delivered.exists():
                            event = self.hang_guard.change(watch.wait, self.lifecycle_clock.monotonic)
                            self.assertNotEqual(event, codex_mesh.WaitDeadlineExpired.EXPIRED)
                    finally:
                        watch.close()
                    self.live_turn = ""
                    self.server.hold_turn_completion = False
                    self.server.release_held_turn.set()
                    result = self.hang_guard.result(launched)
                finally:
                    ready.touch()
                    self.server.release_held_turn.set()
        else:
            result = self.launch("review", "--lens", "contract", "--pass", "1",
                                 "--early-ready", str(ready))
        transitions = [cast(dict[str, object], json.loads(line)) for line in trace.read_text().splitlines()]
        self.assertEqual(len([entry for entry in transitions if entry.get("adopted")]), 1, transitions)
        follows = [entry for entry in transitions if "follow" in entry]
        expected_codes = ([2] if boundary == "first_claim" else
                          [0] if boundary in {"combined", "before_claim", "before_inbox"} else
                          [0, 2] if boundary == "after_claim" else [0, 0])
        self.assertEqual([entry["code"] for entry in follows], expected_codes, transitions)
        return result

    def assert_completed_boundary_review(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        folder = self.session_dir / "phases/7"
        self.assertEqual((folder / "review_status_contract_1").read_text().strip(), "done")
        self.assertIn("Final boundary review.", (folder / "review_summary_contract_1.txt").read_text())
        self.assertNotIn("completion unknown", result.stdout + result.stderr)
        receipts = [cast(dict[str, object], json.loads(line)) for line in
                    (folder / "dispatch_1_submissions.jsonl").read_text().splitlines()]
        self.assertTrue(any("Stage 2:" in json.dumps(receipt.get("messages")) for receipt in receipts), receipts)
        finished = [event for event in self.events() if event.get("event_type") == "pass_finished"]
        self.assertEqual(len(finished), 1, self.events())
        self.assertEqual(finished[0]["status"], "completed")
        self.assertEqual(self.turn_starts, 2)

    def assert_handed_off_boundary_review(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        folder = self.session_dir / "phases/7"
        self.assertEqual((folder / "review_status_contract_1").read_text().strip(), "handed_off")
        self.assertIn(str(self.session_dir.parent / "boundary-destination"),
                      (folder / "review_summary_contract_1.txt").read_text())
        self.assertIn("completion unknown", result.stdout + result.stderr)
        self.assertEqual(self.turn_starts, 1)
        self.assertFalse(any(event.get("event_type") == "pass_finished" and event.get("status") == "completed"
                             for event in self.events()))

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
                                       "--early-ready", str(ready))
            try:
                self.hang_guard.event(self.server.held_turn_ready)
                self.assertEqual(deliveries, [])
                phase = self.session_dir / "phases/7"
                self.assertEqual((phase / "review_status_contract").read_text().strip(), "reviewing")
                launcher_pid = int((phase / "review_pid_contract").read_text())
                os.kill(launcher_pid, 0)
                address = next(iter(self.assignment_records()))
                pending = self.session_dir / f"{address}.pending.json"
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
                                           "--early-ready", str(ready))
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

    def test_an_early_review_adopted_after_stage_one_reports_handoff_without_a_second_prompt(self) -> None:
        result, destination = self.review_after_stage_one(adopt=True)
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        folder = self.session_dir / "phases/7"
        self.assertEqual((folder / "review_status_contract_1").read_text().strip(), "handed_off")
        self.assertIn(str(destination), (folder / "review_summary_contract_1.txt").read_text())
        name = next(iter(self.assignment_records()))
        self.assertIn(f"handed off: {name} to {destination}; completion unknown", result.stdout + result.stderr)
        self.assertEqual(self.turn_starts, 1)
        self.assertFalse(any(event.get("event_type") == "pass_finished" and
                             event.get("status") == "completed" for event in self.events()))
        delivered = read_object(destination / f"{name}.pending.json")
        messages = cast(list[str], delivered["messages"])
        self.assertEqual(len(messages), 1, delivered)
        self.assertIn("Stage 2:", messages[0])

    def test_an_early_review_without_stage_two_delivery_records_error_instead_of_done(self) -> None:
        result = self.review_after_stage_one(adopt=False)[0]
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        folder = self.session_dir / "phases/7"
        self.assertEqual((folder / "review_status_contract_1").read_text().strip(), "error")
        self.assertEqual(self.turn_starts, 1)
        finished = [event for event in self.events() if event.get("event_type") == "pass_finished"]
        self.assertEqual(len(finished), 1, self.events())
        self.assertEqual(finished[0]["status"], "error")
        self.assertFalse(any(record.get("adopted_to") for record in self.assignment_records().values()))

    def test_adoption_after_the_second_review_follow_keeps_its_completed_result(self) -> None:
        self.assert_completed_boundary_review(self.review_adoption_at_boundary("completed"))

    def test_adoption_after_one_follow_consumes_both_review_stages_keeps_its_completed_result(self) -> None:
        self.assert_completed_boundary_review(self.review_adoption_at_boundary("combined"))

    def test_adoption_before_the_second_review_claim_settles_as_handoff(self) -> None:
        self.assert_handed_off_boundary_review(self.review_adoption_at_boundary("before_claim"))

    def test_adoption_after_the_roster_read_before_the_inbox_read_settles_as_handoff(self) -> None:
        self.assert_handed_off_boundary_review(self.review_adoption_at_boundary("before_inbox"))

    def test_adoption_after_the_second_claim_before_the_child_check_settles_as_handoff(self) -> None:
        self.assert_handed_off_boundary_review(self.review_adoption_at_boundary("after_claim"))

    def test_adoption_after_the_first_claim_before_the_child_check_settles_as_handoff(self) -> None:
        result = self.review_adoption_at_boundary("first_claim")
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        status = self.session_dir / "phases/7/review_status_contract_1"
        self.assertEqual(status.read_text().strip(), "handed_off")
        self.assertEqual(self.turn_starts, 0)
        self.assertFalse(any(event.get("event_type") == "pass_finished" and event.get("status") == "completed"
                             for event in self.events()))

    def test_a_review_launcher_follow_uses_the_current_review_row_instead_of_history(self) -> None:
        self.commit_project()
        self.review_launcher("--new-worker", "project-review", "1")
        name, record = next(iter(self.assignment_records().items()))
        self.assertEqual(record["turn_model"], "gpt-kept-sol")
        identity_path = self.identity_path(name)
        identity = read_object(identity_path)
        self.assertEqual(identity["model"], "gpt-kept-sol")
        self.assertEqual(workers.resolve(self.session_dir, "review"), name)
        reservation = workers.allocation_for_follow(self.session_dir, name)
        workers.release_allocation(self.session_dir, name, reservation)
        self.assertTrue(self.assignment_records()[name].get("unit_ledger"))
        _ = write_registry(self.registry, {"direct.codex": {"review": "gpt-other-sol:high"}}, update=True)
        self.review_launcher("--to", name, "2")
        self.assertEqual(self.assignment_records()[name]["turn_model"], "gpt-other-sol")
        turn = cast(dict[str, object], self.methods("turn/start")[-1]["params"])
        self.assertEqual(turn["model"], "gpt-other-sol")
        self.assertEqual(self.turn_starts, 2)
        self.assertEqual(read_object(identity_path), identity)
