"""Exercise native hook payloads against private staff and transcripts."""
from __future__ import annotations

import json
import io
import os
import subprocess
import sys
import unittest
from tempfile import TemporaryDirectory
from pathlib import Path
from dataclasses import replace
from typing import cast, final, override
from unittest.mock import patch

from scripts.astra import astra_roles
from scripts.direct import assign, progress_history
from scripts.herdr import herdr, side_pane, turn_end, worker_panes
from scripts.lib import atomic_file, process_start
from scripts.message import send
from scripts.lib.test_hang_guard import LauncherHangGuard
from scripts.lib.test_models_registry import write_registry
from scripts.production import staff, test_worker, worker


class NativeHookEntryTests(unittest.TestCase):
    def test_native_entry_starts_a_separate_handler_without_calling_handle(self) -> None:
        for family in ("codex", "claude"):
            with self.subTest(family=family):
                reply = 'The work is complete.\nDONE "dispatch"'
                value = ({"type": "agent-turn-complete", "thread-id": "native-thread",
                          "last-assistant-message": reply} if family == "codex" else
                         {"hook_event_name": "Stop", "session_id": "native-thread", "last_assistant_message": reply})
                payload = json.dumps(value)
                arguments = [payload] if family == "codex" else []
                with patch.object(turn_end, "handle") as handler, \
                        patch.object(subprocess, "Popen") as start, \
                        patch.object(sys, "stdin", io.StringIO(payload)):
                    self.assertEqual(turn_end.main(arguments), 0)
                handler.assert_not_called()
                start.assert_called_once_with(
                    [sys.executable, str(Path(turn_end.__file__).resolve()), "--handle-hook", payload],
                    start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def test_separate_invocation_calls_handle_once_without_starting_another_process(self) -> None:
        payload = json.dumps({"type": "agent-turn-complete", "thread-id": "native-thread",
                              "last-assistant-message": "The work is complete."})
        with patch.object(turn_end, "handle", return_value=turn_end.TurnOutcome.DONE) as handler, \
                patch.object(subprocess, "Popen") as start:
            self.assertEqual(turn_end.main(["--handle-hook", payload]), 0)
        handler.assert_called_once_with(turn_end.WorkerEvent("native-thread", reply="The work is complete."))
        start.assert_not_called()


@final
class TurnEndTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.case = test_worker.WorkerCommandTests("runTest")

    @override
    def setUp(self) -> None:
        self.case = test_worker.WorkerCommandTests("runTest")
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def test_native_hook_inputs_complete_each_family_once_and_close_its_pane(self) -> None:
        cases = [(family, close, "current") for family in ("codex", "claude")
                 for close in ("closed", "kept", "outside")]
        cases.extend((family, "closed", transcript) for family in ("codex", "claude")
                     for transcript in ("no final reply", "older reply"))
        for family, close, transcript in cases:
            with self.subTest(family=family, close=close, transcript=transcript):
                _ = write_registry(self.case.fixture.root / "conf/models.conf", {"assignments": {"direct": family}})
                name = "impl-" + family + "-" + close + "-" + transcript.replace(" ", "-")
                self.case.agent.finish_after = int(transcript != "current")
                self.assertIs(self.case.dispatch(name), worker.DispatchSubmission.SUBMITTED)
                pane = self.case.agent.submitted[-1][0]
                row = self.case.agent.row(pane)
                active = progress_history.active_dispatch(row.id, self.case.run_dir)
                self.assertIsInstance(active, progress_history.ActiveDispatch)
                assert isinstance(active, progress_history.ActiveDispatch)
                if transcript != "current":
                    if transcript == "older reply":
                        self.case.agent.append(pane, "assistant", "The previous result is pending.")
                    self.case.agent.replies.append("The work is complete.\nDONE " + active.instance_id)
                    self.case.fixture.world.locations[pane]["agent_status"] = "done"
                payload: dict[str, object] = ({"type": "agent-turn-complete", "thread-id": str(row.current_session),
                    "turn-id": "turn-1", "cwd": str(self.case.fixture.root), "input-messages": [self.case.agent.submitted[-1][1]],
                    "last-assistant-message": self.case.agent.replies[-1]} if family == "codex" else
                    {"hook_event_name": "Stop", "session_id": str(row.current_session), "transcript_path": "unused",
                     "stop_hook_active": False, "last_assistant_message": self.case.agent.replies[-1]})
                if close == "kept":
                    self.case.fixture.world.unknown[pane] = "worker process is unreadable"
                elif close == "outside":
                    self.case.fixture.world.locations[pane]["tab_id"] = "w1:outside"
                notices = len(self.case.agent.notices)
                self.assertEqual(turn_end.handle(turn_end.hook_event(payload)), turn_end.TurnOutcome.DONE)
                if close == "closed":
                    self.assertEqual(turn_end.handle(turn_end.hook_event(payload)), turn_end.TurnOutcome.IGNORED)
                self.assertEqual(len(self.case.agent.notices), notices + 1)
                self.assertIn(f"{name}: phase 8: done; reply {self.case.reply}", self.case.agent.notices[-1])
                self.assertEqual(self.case.reply.read_text().strip(), self.case.agent.replies[-1])
                self.assertIsInstance(progress_history.active_dispatch(row.id, self.case.run_dir), progress_history.NoActiveDispatch)
                self.assertEqual(worker_panes.worker_pass(row, self.case.run_dir)["status"], "completed")
                if close == "closed":
                    self.assertIn((pane, "terminal-" + pane), self.case.fixture.world.closed)
                else:
                    self.assertIn(pane, self.case.fixture.world.locations)
                    self.assertNotIn((pane, "terminal-" + pane), self.case.fixture.world.closed)
                    reason = "worker process is unreadable" if close == "kept" else "outside its parent's tabs"
                    self.assertIn("anomaly:", self.case.agent.notices[-1])
                    self.assertIn(reason, self.case.agent.notices[-1])
                    self.assertIn(f"kept pane {pane}", self.case.agent.notices[-1])
                    _ = self.case.fixture.world.unknown.pop(pane, None)
                    self.case.fixture.world.locations[pane]["tab_id"] = "w1:t1"
                    self.assertEqual(turn_end.handle(turn_end.hook_event(payload)), turn_end.TurnOutcome.DONE)
                    self.assertNotIn(pane, self.case.fixture.world.locations)
                    self.assertEqual(len(self.case.agent.notices), notices + 1)
                    self.assertEqual(turn_end.handle(turn_end.hook_event(payload)), turn_end.TurnOutcome.IGNORED)
                self.assertEqual(staff.by_id(row.id), row)
                log = self.case.fixture.root / "state/herdr/turn-end-ignored.jsonl"
                entry = cast(dict[str, object], json.loads(log.read_text().splitlines()[-1]))
                self.assertEqual((entry["thread_id"], entry["worker_name"], entry["reason"]),
                    (row.current_session, name, "the hook completion is already finished"))

    def test_turn_end_completes_a_dispatch_outside_the_run_root(self) -> None:
        opened = self.case.opened()
        original_run = self.case.run_dir
        original_state = (original_run / "progress_history_state.json").read_bytes()
        outside = Path(self.enterContext(TemporaryDirectory(prefix="outside-worker-run-")))
        atomic_file.write_text_atomic(outside / "progress_history_state.json", original_state.decode())
        self.case.run_dir = outside
        self.assertIs(self.case.dispatch(), worker.DispatchSubmission.SUBMITTED)
        self.assertEqual(worker_panes.worker_run(opened.row), outside)
        self.assertEqual(len(self.case.fixture.world.launches), 1)
        self.assertEqual(self.case.finish(), turn_end.TurnOutcome.DONE)
        self.assertEqual(worker_panes.worker_pass(opened.row, outside)["status"], "completed")
        self.assertEqual((original_run / "progress_history_state.json").read_bytes(), original_state)
        self.assertEqual(len(self.case.agent.notices), 1)
        self.assertIn("phase 8: done", self.case.agent.notices[0])
        self.assertNotIn(opened.pane, self.case.fixture.world.locations)

    def test_completed_rerun_repeats_the_saved_kept_pane_anomaly(self) -> None:
        for family in ("codex", "claude"):
            name = "impl-" + family
            completions: list[str] = []
            for pass_number in (1, 2):
                with self.subTest(family=family, pass_number=pass_number):
                    prompt = self.case.run_dir / f"{name}-{pass_number}.md"
                    atomic_file.write_text_atomic(prompt, "Read the source and preserve its behavior.\n")
                    self.case.prompt = prompt
                    self.case.fixture.world.unknown.clear()
                    _ = write_registry(self.case.fixture.root / "conf/models.conf", {"assignments": {"direct": family}})
                    submitted = len(self.case.agent.submitted)
                    self.assertIs(self.case.dispatch(name), worker.DispatchSubmission.SUBMITTED)
                    pane = self.case.agent.submitted[-1][0]
                    row = self.case.agent.row(pane)
                    sender = (send.CodexSender(str(row.current_session)) if family == "codex"
                              else send.ClaudeSender(str(row.current_session)))
                    notices = len(self.case.agent.notices)
                    self.case.fixture.world.unknown[pane] = "worker process is unreadable"
                    self.assertEqual(self.case.finish(name), turn_end.TurnOutcome.DONE)
                    notice = self.case.agent.notices[-1]
                    passed = worker_panes.worker_pass(row, self.case.run_dir)
                    completions.append(notice)
                    self.assertIn(f"; pass {passed['instance_id']}", notice)
                    self.assertIn(f"anomaly: {name}: worker process is unreadable; kept pane {pane}", notice)
                    reply = self.case.reply.read_bytes(), self.case.reply.stat().st_mtime_ns
                    self.assertIs(self.case.dispatch(name), worker.DispatchSubmission.SUBMITTED)
                    self.assertEqual(self.case.agent.notices[notices:], [notice, notice])
                    self.assertEqual(self.case.agent.notice_senders[notices:], [sender, sender])
                    self.assertEqual(len(self.case.agent.submitted), submitted + 1)
                    self.assertIn(pane, self.case.fixture.world.locations)
                    self.assertEqual((self.case.reply.read_bytes(), self.case.reply.stat().st_mtime_ns), reply)
            self.assertNotEqual(*completions)

    def test_a_director_or_an_idle_worker_hook_changes_no_pass(self) -> None:
        row = self.case.opened().row
        before = (self.case.run_dir / "progress_history_state.json").read_bytes()
        log = self.case.fixture.root / "state/herdr/turn-end-ignored.jsonl"
        for identity, name, reason in (
                (self.case.fixture.parent.current_session, self.case.fixture.parent.name, "the staff row is not a worker"),
                (row.current_session, row.name, "no active dispatch or recoverable completion exists"),
                ("unknown-thread", "unknown", "no staff row for the current thread"),
                ("", "unknown", "no staff row for the current thread")):
            with self.subTest(identity=identity):
                self.assertEqual(turn_end.handle(turn_end.WorkerEvent(str(identity))), turn_end.TurnOutcome.IGNORED)
                entry = cast(dict[str, object], json.loads(log.read_text().splitlines()[-1]))
                self.assertEqual((entry["thread_id"], entry["worker_name"], entry["reason"]), (identity, name, reason))
        self.assertEqual(len(log.read_text().splitlines()), 4)
        self.assertEqual((self.case.run_dir / "progress_history_state.json").read_bytes(), before)
        self.assertEqual(self.case.agent.notices, [])
        self.assertEqual(self.case.agent.submitted, [])

    def test_both_completion_events_apply_the_finished_run_exception(self) -> None:
        cases = [(event, reply_state) for event in (turn_end.EventKind.TURN_END, turn_end.EventKind.RUN_FINISHED)
                 for reply_state in ("done", "waiting for delivery", "waiting for reply")]
        cases.append((turn_end.EventKind.RUN_FINISHED, "empty reply"))
        for index, (event, reply_state) in enumerate(cases):
            with self.subTest(event=event, reply_state=reply_state):
                name = f"impl{index + 2}"
                self.case.reply = self.case.run_dir / f"{name}-reply.md"
                self.case.agent.without_done = reply_state != "done"
                self.case.agent.finish_after = int(reply_state == "empty reply")
                self.assertIs(self.case.dispatch(name), worker.DispatchSubmission.SUBMITTED)
                pane = self.case.agent.submitted[-1][0]
                row = self.case.agent.row(pane)
                active = progress_history.active_dispatch(row.id, self.case.run_dir)
                if reply_state == "done":
                    path, record = self.case.detached_record(row, "private-check")
                    record["delivery"] = f"sent: worker {row.name} at {pane}"
                    atomic_file.write_text_atomic(path, json.dumps(record))
                    self.assertEqual(self.case.finish(name, event), turn_end.TurnOutcome.DONE)
                    anomaly = "run private-check finished, but its result is not in the transcript"
                    self.assertEqual(self.case.agent.notices[-1].count(anomaly), 1)
                    self.assertIn("DONE ", self.case.reply.read_text())
                else:
                    notices: list[str] = []
                    for number in (1, 2):
                        run_id = f"{name}-check-{number}"
                        path, record = self.case.detached_record(row, run_id)
                        record["delivery"] = f"sent: worker {row.name} at {pane}"
                        atomic_file.write_text_atomic(path, json.dumps(record))
                        if reply_state != "waiting for delivery":
                            self.case.agent.append(pane, "user", f"Detached run: {run_id}\nRun finished: private verification")
                        before = len(self.case.agent.notices)
                        for current_event in (event, event):
                            self.assertEqual(self.case.finish(name, current_event), turn_end.TurnOutcome.WAITING_ON_REPLY)
                            self.assertEqual(len(self.case.agent.notices), before + 1)
                        notice = self.case.agent.notices[-1]
                        self.assertIn("; runs ", notice)
                        self.assertIn(run_id, notice.partition("; runs ")[2].splitlines()[0])
                        notices.append(notice)
                        turn_path = self.case.reply.with_name(self.case.reply.stem + "_turn" + self.case.reply.suffix)
                        if reply_state == "empty reply":
                            self.assertFalse(turn_path.exists())
                            self.assertNotIn("; reply ", notice)
                        else:
                            self.assertIn(f"; reply {turn_path}", notice)
                            self.assertEqual(turn_path.read_text().strip(), self.case.agent.final)
                        self.assertEqual(progress_history.active_dispatch(row.id, self.case.run_dir), active)
                        if reply_state == "waiting for delivery":
                            self.case.agent.append(pane, "user", f"Detached run: {run_id}\nRun finished: private verification")
                        if reply_state != "empty reply":
                            self.case.agent.append(pane, "assistant", self.case.agent.final)
                    self.assertNotEqual(*notices)
                    self.case.agent.without_done = False
                    self.case.agent.complete(pane)
                    self.assertEqual(self.case.finish(name), turn_end.TurnOutcome.DONE)

    def test_a_live_run_keeps_done_active_for_both_completion_events(self) -> None:
        for receipt in ("done", "awaiting reply", "launcher error"):
            with self.subTest(receipt=receipt):
                name = "impl-" + receipt.replace(" ", "-")
                self.assertIs(self.case.dispatch(name), worker.DispatchSubmission.SUBMITTED)
                pane = self.case.agent.submitted[-1][0]
                row = self.case.agent.row(pane)
                active = progress_history.active_dispatch(row.id, self.case.run_dir)
                assert isinstance(active, progress_history.ActiveDispatch)
                path, record = self.case.detached_record(row, "private-check-" + name)
                notices = len(self.case.agent.notices)
                reply = self.case.agent.replies[-1]
                if receipt == "awaiting reply":
                    self.case.agent.append(pane, "user", "Detached run: earlier-check\nRun finished: an earlier check")
                with subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()", "detached_runs.py", "supervise", str(path)],
                                      stdin=subprocess.PIPE, text=True) as supervisor:
                    record.update(status="running", pid=supervisor.pid)
                    atomic_file.write_text_atomic(path, json.dumps(record))
                    for event in (turn_end.EventKind.TURN_END, turn_end.EventKind.RUN_FINISHED):
                        self.assertEqual(self.case.finish(name, event), turn_end.TurnOutcome.WAITING_ON_RUNS)
                        self.assertEqual(progress_history.active_dispatch(row.id, self.case.run_dir), active)
                        self.assertNotIn(pane, [closed[0] for closed in self.case.fixture.world.closed])
                        turn_path = self.case.reply.with_name(self.case.reply.stem + "_turn" + self.case.reply.suffix)
                        self.assertEqual(turn_path.read_text().strip(), reply)
                        self.assertEqual(len(self.case.agent.notices), notices + 1)
                    if receipt == "launcher error":
                        progress_history.complete_dispatch(self.case.run_dir, "impl", active.instance_id, status="error")
                    if receipt == "awaiting reply":
                        reply = "The final check is still running."
                        self.case.agent.append(pane, "assistant", reply)
                        for event in (turn_end.EventKind.TURN_END, turn_end.EventKind.RUN_FINISHED):
                            self.assertEqual(self.case.finish(name, event), turn_end.TurnOutcome.WAITING_ON_RUNS)
                            self.assertEqual(len(self.case.agent.notices), notices + 2)
                        self.case.agent.append(pane, "user", f"Detached run: {record['run_id']}\nRun finished: the final check")
                    record.update(status="finished", delivery=f"sent: worker {row.name} at {pane}")
                    atomic_file.write_text_atomic(path, json.dumps(record))
                    if receipt == "awaiting reply":
                        for event in (turn_end.EventKind.RUN_FINISHED, turn_end.EventKind.RUN_FINISHED):
                            self.assertEqual(self.case.finish(name, event), turn_end.TurnOutcome.WAITING_ON_REPLY)
                            self.assertEqual(len(self.case.agent.notices), notices + 3)
                            self.assertEqual(turn_path.read_text().strip(), reply)
                        self.case.agent.complete(pane)
                    self.assertEqual(self.case.finish(name, turn_end.EventKind.RUN_FINISHED), turn_end.TurnOutcome.DONE)
                    self.assertEqual(worker_panes.worker_pass(row, self.case.run_dir)["status"], "completed")
                    self.assertNotIn(pane, self.case.fixture.world.locations)
                    assert supervisor.stdin is not None
                    supervisor.stdin.close()
                    _ = supervisor.wait(timeout=LauncherHangGuard("private run exit").remaining())
        self.assertEqual(len(self.case.agent.submitted), 3)
        self.assertEqual(self.case.agent.waited, [])

    def test_a_friend_reply_completes_the_same_pass_without_another_prompt(self) -> None:
        self.case.agent.without_done = True
        self.assertIs(self.case.dispatch(), worker.DispatchSubmission.SUBMITTED)
        pane = self.case.agent.submitted[-1][0]
        row = self.case.agent.row(pane)
        active = progress_history.active_dispatch(row.id, self.case.run_dir)
        with patch.dict(os.environ, {"HERDR_PANE_ID": pane, "CODEX_THREAD_ID": str(row.current_session)}):
            friend = side_pane.open("friend-private", worker_panes.launch_policy("impl"), cwd=self.case.fixture.root)
        self.assertIsInstance(friend, side_pane.OpenSidePane)
        assert isinstance(friend, side_pane.OpenSidePane)
        pid = self.case.fixture.world.foreground[friend.pane][0]
        process = self.case.fixture.world.processes[pid]
        self.case.fixture.world.processes[pid] = replace(process, identity=herdr.PaneSessionReport(process.identity.id))
        self.case.fixture.world.locations[friend.pane]["agent_status"] = "working"
        for address, expected in (("codex:another-worker", turn_end.TurnOutcome.NO_DONE),
                                  (f"codex:{row.current_session}", turn_end.TurnOutcome.WAITING_ON_FRIEND)):
            with self.subTest(recipient=address):
                self.case.agent.append(friend.pane, "user", f'Review the change. Reply with send.py --to {address} --text "<answer>".')
                self.assertEqual(self.case.finish(), expected)
        self.assertIs(self.case.dispatch(), worker.DispatchSubmission.SUBMITTED)
        self.assertEqual(progress_history.active_dispatch(row.id, self.case.run_dir), active)
        self.assertEqual(len(self.case.agent.submitted), 1)
        self.assertNotIn("question", self.case.agent.notices[-1])
        self.assertNotIn("no DONE", self.case.agent.notices[-1])
        self.case.fixture.world.locations[friend.pane]["agent_status"] = "done"
        self.case.agent.append(pane, "user", "Message from friend-private: The design is sound.")
        self.case.agent.without_done = False
        self.case.agent.complete(pane)
        self.assertEqual(self.case.finish(), turn_end.TurnOutcome.DONE)
        assert isinstance(active, progress_history.ActiveDispatch)
        self.assertTrue(self.case.reply.read_text().endswith(f"DONE {active.instance_id}\n"))

    def test_a_final_diff_event_before_the_review_reply_keeps_the_same_pass(self) -> None:
        self.case.agent.finish_after = 1
        for ready_first, hook_only in ((True, False), (False, False), (True, True)):
            with self.subTest(ready_first=ready_first, hook_only=hook_only):
                name = "review-hook" if hook_only else "review-first" if ready_first else "review-last"
                ready = self.case.run_dir / f"{name}.ready"
                code = worker.dispatch(self.case.fixture.parent.id, staff.WorkerName.parse(name), "review", self.case.prompt,
                    session_dir=self.case.run_dir, slot=name, phase="8", kind="review", launcher_command=[name],
                    early_ready=ready, reply=self.case.reply)
                self.assertIs(code, worker.DispatchSubmission.SUBMITTED)
                pane = self.case.agent.submitted[-1][0]
                row = self.case.agent.row(pane)
                active = progress_history.active_dispatch(row.id, self.case.run_dir)
                before = len(self.case.agent.submitted)
                notices = len(self.case.agent.notices)
                if ready_first:
                    atomic_file.write_text_atomic(ready, "ready\n")
                    with patch.dict(os.environ, {"CODEX_THREAD_ID": str(self.case.fixture.parent.current_session)}):
                        self.assertEqual(turn_end.main(["stage-ready", name]), 0)
                    self.assertEqual(self.case.agent.notices[notices:], [])
                else:
                    self.case.agent.complete(pane)
                    self.assertEqual(self.case.finish(name), turn_end.TurnOutcome.WAITING_ON_STAGE_TWO)
                    atomic_file.write_text_atomic(ready, "ready\n")
                self.assertEqual(progress_history.active_dispatch(row.id, self.case.run_dir), active)
                self.assertEqual(len(self.case.agent.submitted), before)
                if ready_first:
                    if hook_only:
                        assert isinstance(active, progress_history.ActiveDispatch)
                        self.case.agent.last_replies[str(row.current_session)] = "The review is complete.\nDONE " + active.instance_id
                        self.case.fixture.world.locations[pane]["agent_status"] = "done"
                    else:
                        self.case.agent.complete(pane)
                    self.assertEqual(self.case.finish(name), turn_end.TurnOutcome.SUBMITTED)
                else:
                    with patch.dict(os.environ, {"CODEX_THREAD_ID": str(self.case.fixture.parent.current_session)}):
                        self.assertEqual(turn_end.main(["stage-ready", name]), 0)
                self.assertEqual(len(self.case.agent.submitted), before + 1)
                with patch.dict(os.environ, {"CODEX_THREAD_ID": str(self.case.fixture.parent.current_session)}):
                    self.assertEqual(turn_end.main(["stage-ready", name]), 0)
                self.assertEqual(len(self.case.agent.submitted), before + 1)
                self.case.agent.complete(pane)
                self.assertEqual(self.case.finish(name), turn_end.TurnOutcome.DONE)

    def test_a_turn_ending_question_reaches_the_director_without_ending_the_pass(self) -> None:
        for name, reply in (("question", "Which output format must I use? JSON or Markdown?"),
                            ("recorder", "The two file-clock changes are complete. Results are pending.")):
            with self.subTest(name=name):
                self.case.agent.without_done = True
                self.case.agent.final = reply
                notices = len(self.case.agent.notices)
                self.assertIs(self.case.dispatch(name), worker.DispatchSubmission.SUBMITTED)
                pane = self.case.agent.submitted[-1][0]
                row = self.case.agent.row(pane)
                active = progress_history.active_dispatch(row.id, self.case.run_dir)
                self.assertEqual(self.case.finish(name), turn_end.TurnOutcome.NO_DONE)
                self.assertEqual(self.case.finish(name), turn_end.TurnOutcome.NO_DONE)
                self.assertEqual(len(self.case.agent.notices), notices + 1)
                self.assertIn(self.case.agent.final, self.case.agent.notices[-1])
                self.assertEqual(progress_history.active_dispatch(row.id, self.case.run_dir), active)
                self.assertIn(pane, self.case.fixture.world.locations)
                self.case.agent.without_done = False
                self.case.agent.complete(pane)
                self.assertEqual(self.case.finish(name), turn_end.TurnOutcome.DONE)

    def test_a_failed_notice_recovers_the_completed_pass_without_another_close(self) -> None:
        result = worker.dispatch(self.case.fixture.parent.id, staff.WorkerName.parse("impl"), "impl", self.case.prompt,
            session_dir=self.case.run_dir, slot="impl", phase="8", kind="impl", launcher_command=["assign.sh"],
            reply=self.case.reply, completion_actions=progress_history.CompletionActions(end_worker=True))
        self.assertIs(result, worker.DispatchSubmission.SUBMITTED)
        pane = self.case.agent.submitted[-1][0]
        row = self.case.agent.row(pane)
        self.case.agent.notice_failures = 1
        with self.assertRaisesRegex(ValueError, "director message failed"):
            _ = self.case.finish()
        passed = worker_panes.worker_pass(row, self.case.run_dir)
        self.assertEqual(passed["status"], "completed")
        self.assertNotIn("hook_notice", passed)
        self.assertEqual(staff.by_id(row.id), row)
        self.assertEqual(self.case.finish(), turn_end.TurnOutcome.DONE)
        self.assertEqual(len(self.case.agent.notices), 1)
        self.assertEqual(self.case.fixture.world.closed, [(pane, "terminal-" + pane)])
        self.assertIsNone(staff.by_id(row.id))
        self.assertEqual(turn_end.handle(turn_end.WorkerEvent(str(row.current_session))), turn_end.TurnOutcome.IGNORED)

    def test_stage_two_recovers_busy_and_interrupted_submission(self) -> None:
        for reason in ("busy", "interrupt_before_submit", "interrupt_stage_two"):
            with self.subTest(reason=reason):
                name = "review-" + reason.replace("_", "-")
                ready = self.case.run_dir / f"{name}.ready"
                result = worker.dispatch(self.case.fixture.parent.id, staff.WorkerName.parse(name), "review", self.case.prompt,
                    session_dir=self.case.run_dir, slot=name, phase="8", kind="review", launcher_command=[name],
                    early_ready=ready, reply=self.case.reply)
                self.assertIs(result, worker.DispatchSubmission.SUBMITTED)
                pane = self.case.agent.submitted[-1][0]
                before = len(self.case.agent.submitted)
                atomic_file.write_text_atomic(ready, "ready\n")
                if reason == "busy":
                    self.case.fixture.world.locations[pane]["agent_status"] = "working"
                    self.case.fixture.world.first_turn_timeout = True
                    self.assertEqual(self.case.finish(name, turn_end.EventKind.STAGE_READY), turn_end.TurnOutcome.BUSY)
                    self.assertIn("busy", self.case.agent.notices[-1])
                    self.case.fixture.world.first_turn_timeout = False
                elif reason == "interrupt_stage_two":
                    self.case.agent.interrupt = "prompt"
                    self.assertEqual(self.case.finish(name, turn_end.EventKind.STAGE_READY), turn_end.TurnOutcome.UNCERTAIN)
                    self.assertIn("prompt delivered to pane", self.case.agent.notices[-1])
                else:
                    save = progress_history.save_dispatch_delivery

                    def interrupt(run: Path, instance: str, delivery: progress_history.DispatchDelivery) -> None:
                        save(run, instance, delivery)
                        raise OSError("interrupted after Stage 2 save")

                    with patch.object(progress_history, "save_dispatch_delivery", side_effect=interrupt):
                        with self.assertRaisesRegex(OSError, "Stage 2 save"):
                            _ = self.case.finish(name, turn_end.EventKind.STAGE_READY)
                self.assertEqual(len(self.case.agent.submitted), before + (reason == "interrupt_stage_two"))
                self.assertEqual(self.case.finish(name, turn_end.EventKind.STAGE_READY), turn_end.TurnOutcome.SUBMITTED)
                self.assertEqual(len(self.case.agent.submitted), before + 1)
                if reason == "interrupt_stage_two":
                    self.case.agent.complete(pane)
                self.assertEqual(self.case.finish(name), turn_end.TurnOutcome.DONE)
                self.assertEqual(self.case.finish(name, turn_end.EventKind.STAGE_READY), turn_end.TurnOutcome.IGNORED)
                self.assertEqual(len(self.case.agent.submitted), before + 1)

    def test_a_late_hook_does_not_end_the_next_managed_review(self) -> None:
        name = "astra-reviewer"
        self.assertIs(self.case.dispatch(name, "astra-reviewer"), worker.DispatchSubmission.SUBMITTED)
        old_reply = self.case.agent.replies[-1]
        self.assertEqual(self.case.finish(name), turn_end.TurnOutcome.DONE)
        current = self.case.opened(name, "astra-reviewer")
        following = self.case.prompt.with_name("review-b.md")
        atomic_file.write_text_atomic(following, "Review B.")
        self.case.agent.finish_after = 1
        with (patch.dict(os.environ, {"ASTRA_ROLES_STATE": str(self.case.fixture.root / "holds")}),
              self.case.fixture.world.process_starts()):
            pid = self.case.fixture.world.foreground[current.pane][0]
            held = astra_roles.hold("reviewer", astra_roles.ExecProcess(pid), str(current.row.id))
            self.assertNotIsInstance(held, astra_roles.HoldWorkerUnavailable)
            result = worker.dispatch(self.case.fixture.parent.id, staff.WorkerName.parse(name), "astra-reviewer", following,
                session_dir=self.case.run_dir, slot="impl", phase="8", kind="astra", launcher_command=["review-b"],
                reply=self.case.reply, started=current,
                completion_actions=progress_history.CompletionActions(end_worker=True, held_role=str(current.row.id)))
            self.assertIs(result, worker.DispatchSubmission.SUBMITTED)
            active = progress_history.active_dispatch(current.row.id, self.case.run_dir)
            event = turn_end.hook_event({"type": "agent-turn-complete", "thread-id": str(current.row.current_session),
                                         "last-assistant-message": old_reply})
            stops = self.case.fixture.world.stops.copy()
            cases = (("", old_reply), ("Still reviewing.", old_reply),
                     ("The older result is pending.", "The work is complete.\nDONE another-dispatch"))
            for partial, hook_reply in cases:
                with self.subTest(partial=partial, hook_reply=hook_reply):
                    event = replace(event, reply=hook_reply)
                    if partial:
                        self.case.agent.append(current.pane, "assistant", partial)
                    log = self.case.fixture.root / "state/herdr/turn-end-ignored.jsonl"
                    previous = len(log.read_text().splitlines()) if log.is_file() else 0
                    self.assertEqual(turn_end.handle(event), turn_end.TurnOutcome.IGNORED)
                    entries = [cast(dict[str, object], json.loads(line)) for line in log.read_text().splitlines()]
                    self.assertEqual(len(entries), previous + 1)
                    entry = entries[-1]
                    self.assertEqual((entry["thread_id"], entry["worker_name"]), (current.row.current_session, name))
                    reason = ("the transcript reply differs from the turn reply" if partial
                              else "the submitted dispatch has no final reply")
                    self.assertEqual(entry["reason"], reason)
                    self.assertEqual(progress_history.active_dispatch(current.row.id, self.case.run_dir), active)
                    self.assertIn(current.pane, self.case.fixture.world.locations)
                    self.assertEqual(staff.by_id(current.row.id), current.row)
                    live, = astra_roles.live_roles(staff.snapshot())
                    self.assertEqual(live["staff_id"], current.row.id)
                    self.assertEqual(self.case.fixture.world.stops, stops)

    def test_a_failed_repair_action_records_error_and_sends_the_director_its_reason(self) -> None:
        status = self.case.run_dir / "fix.status"
        original = self.case.agent.run_process

        def fail_landed(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
            if len(command) > 2 and command[1].endswith("/findings.py") and command[2] == "landed":
                return subprocess.CompletedProcess(command, 2, "", "ledger write failed")
            return original(command, **options)

        self.case.agent.run_process = fail_landed
        result = worker.dispatch(self.case.fixture.parent.id, staff.WorkerName.parse("fix"), "fix", self.case.prompt,
            session_dir=self.case.run_dir, slot="fix", phase="8", kind="fix", launcher_command=["fix"], reply=self.case.reply,
            fix_cause="Repair the failed gate",
            completion_actions=progress_history.CompletionActions(status_file=str(status), resolve_fix=True))
        self.assertIs(result, worker.DispatchSubmission.SUBMITTED)
        row = self.case.agent.row(self.case.agent.submitted[-1][0])
        self.assertEqual(self.case.finish("fix"), turn_end.TurnOutcome.ERROR)
        self.assertEqual(worker_panes.worker_pass(row, self.case.run_dir)["status"], "error")
        self.assertEqual(status.read_text(), "error\n")
        self.assertIn("ledger write failed", self.case.agent.notices[-1])
        self.assertEqual(len(self.case.agent.notices), 1)

    def test_submission_keeps_the_held_role_until_the_hook_completes(self) -> None:
        self.case.agent.finish_after = 1
        row = self.case.opened("astra-reviewer", "astra-reviewer").row
        process = self.case.fixture.world.foreground[next(pane for pane in self.case.fixture.world.foreground
            if self.case.fixture.world.processes[self.case.fixture.world.foreground[pane][0]].current_identity == row.current_session)][0]
        with patch.dict(os.environ, {"ASTRA_ROLES_STATE": str(self.case.fixture.root / "holds")}):
            held = astra_roles.hold("reviewer", astra_roles.ExecProcess(process, process_start.Started("private-start")), str(row.id))
            self.assertNotIsInstance(held, astra_roles.HoldWorkerUnavailable)
            result = worker.dispatch(self.case.fixture.parent.id, staff.WorkerName.parse(row.name), "astra-reviewer", self.case.prompt,
                session_dir=self.case.run_dir, slot=row.name, phase="8", kind="astra", launcher_command=["assign.sh"],
                reply=self.case.reply, completion_actions=progress_history.CompletionActions(end_worker=True, held_role=str(row.id)))
            self.assertIs(result, worker.DispatchSubmission.SUBMITTED)
            hold = self.case.fixture.root / "holds" / f"{row.id}.json"
            before = hold.read_bytes()
            self.assertIsInstance(progress_history.active_dispatch(row.id, self.case.run_dir), progress_history.ActiveDispatch)
            self.assertEqual(hold.read_bytes(), before)
            self.assertEqual(staff.by_id(row.id), row)
            self.case.agent.complete(self.case.agent.submitted[-1][0])
            self.assertEqual(self.case.finish(row.name), turn_end.TurnOutcome.DONE)
            self.assertFalse(hold.exists())
            self.assertIsNone(staff.by_id(row.id))

    def test_the_astra_launcher_returns_with_an_active_pass_and_a_worker_process_hold(self) -> None:
        router = test_worker.PaneCommandRouter()
        router.setup(self, self.case.fixture.root)
        args = assign.Arguments(new_worker="astra-reviewer", summary=str(self.case.reply), policy="astra-reviewer")
        with (patch.dict(os.environ, {"ASTRA_ROLES_STATE": str(self.case.fixture.root / "holds")}),
              self.case.fixture.world.process_starts()):
            code = assign.execute_astra_review_herdr(args, self.case.run_dir, self.case.fixture.root,
                self.case.prompt, dict(os.environ))
            self.assertIs(code, worker.DispatchSubmission.SUBMITTED)
            pane = self.case.agent.submitted[-1][0]
            row = self.case.agent.row(pane)
            hold = self.case.fixture.root / "holds" / f"{row.id}.json"
            record = cast(dict[str, object], json.loads(hold.read_text()))
            self.assertEqual(record["pid"], self.case.fixture.world.foreground[pane][0])
            live, = astra_roles.live_roles(staff.snapshot())
            self.assertEqual((live["staff_id"], live["thread_id"]), (row.id, row.current_session))
            self.assertIsInstance(progress_history.active_dispatch(row.id, self.case.run_dir), progress_history.ActiveDispatch)
            self.assertEqual(staff.by_id(row.id), row)
            self.assertEqual(self.case.finish(row.name), turn_end.TurnOutcome.DONE)
            self.assertFalse(hold.exists())
            self.assertIsNone(staff.by_id(row.id))


if __name__ == "__main__":
    _ = unittest.main()
