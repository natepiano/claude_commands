"""Exercise native hook payloads against private staff and transcripts."""
from __future__ import annotations

import json
import io
import os
import shutil
import subprocess
import sys
import unittest
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import ExitStack, redirect_stderr
from datetime import datetime
from tempfile import TemporaryDirectory
from pathlib import Path
from dataclasses import replace
from typing import cast, final, override
from unittest.mock import patch

from scripts.accounts import refusals
from scripts.astra import astra_roles
from scripts.direct import assign, progress_history
from scripts.herdr import herdr, side_pane, turn_end, worker_panes
from scripts.lib import atomic_file, process_start, scratch
from scripts.message import send
from scripts.lib.test_hang_guard import LauncherHangGuard, TEST_HANG_GUARD_SECS
from scripts.lib.test_models_registry import write_registry
from scripts.production import staff, test_worker, worker, worker_admission
from scripts.production.staff_test_support import fixture_worker
from scripts.whoami import agent_accounts, note_file, quota_alert


class NativeHookEntryTests(unittest.TestCase):
    def test_native_entry_starts_a_separate_handler_without_calling_handle(self) -> None:
        real_popen = subprocess.Popen
        for family, size in (("codex", 1), ("claude", 1), ("codex", 5_000)):
            with self.subTest(family=family, size=size), TemporaryDirectory() as folder:
                reply = 'The work is complete.\nDONE "dispatch"' * size
                value: dict[str, object] = {"hook_event_name": "Stop", "session_id": "native-thread", "last_assistant_message": reply}
                if family == "codex":
                    value.update({"turn_id": "turn-1", "model": "sol", "transcript_path": None})
                payload = json.dumps(value)
                receiver = Path(folder) / "receive.py"
                receipt = Path(folder) / "receipt.json"
                atomic_file.write_text_atomic(receiver, "\n".join((
                    "from dataclasses import asdict",
                    "import json, sys",
                    "from pathlib import Path",
                    "from scripts.herdr import turn_end",
                    "from scripts.lib.atomic_file import write_text_atomic",
                    "turn_end.handle = lambda event: write_text_atomic(Path(sys.argv[1]), json.dumps(asdict(event)))",
                    "raise SystemExit(turn_end.main(sys.argv[2:]))", "")))
                children: list[subprocess.Popen[bytes]] = []

                def receive(arguments: list[str], *, start_new_session: bool, stdin: int,
                            stdout: int, stderr: int) -> subprocess.Popen[bytes]:
                    child = real_popen([arguments[0], str(receiver), str(receipt), *arguments[2:]],
                        start_new_session=start_new_session, stdin=stdin, stdout=stdout, stderr=stderr)
                    self.addCleanup(child.wait, timeout=TEST_HANG_GUARD_SECS)
                    self.addCleanup(child.kill)
                    children.append(child)
                    return child

                output = io.StringIO()
                with (patch.object(turn_end, "handle") as handler,
                      patch.object(subprocess, "Popen", side_effect=receive) as start,
                      patch.object(sys, "stdin", io.StringIO(payload)),
                      patch.object(sys, "stdout", output)):
                    self.assertEqual(turn_end.main([]), 0)
                handler.assert_not_called()
                start.assert_called_once_with(
                    [sys.executable, str(Path(turn_end.__file__).resolve()), "--handle-hook"],
                    start_new_session=True, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.assertEqual(children[0].wait(timeout=TEST_HANG_GUARD_SECS), 0)
                self.assertEqual(json.loads(receipt.read_text()),
                                 {"thread_id": "native-thread", "kind": "turn end", "reply": reply})
                self.assertEqual(output.getvalue(), "")

    def test_separate_invocation_calls_handle_once_without_starting_another_process(self) -> None:
        cases: tuple[tuple[dict[str, object], turn_end.WorkerEvent], ...] = (
            ({"hook_event_name": "Stop", "session_id": "native-thread", "last_assistant_message": "The work is complete."},
             turn_end.WorkerEvent("native-thread", reply="The work is complete.")),
            ({"hook_event_name": "Stop", "session_id": "native-thread", "last_assistant_message": None},
             turn_end.WorkerEvent("native-thread")),
            ({"type": "agent-turn-complete", "thread-id": "native-thread", "last-assistant-message": "The work is complete."},
             turn_end.WorkerEvent("")),
        )
        for value, expected in cases:
            with (self.subTest(value=value),
                  patch.object(turn_end, "handle", return_value=turn_end.TurnOutcome.DONE) as handler,
                  patch.object(subprocess, "Popen") as start,
                  patch.object(sys, "stdin", io.StringIO(json.dumps(value))),
                  patch.object(sys, "stdout", io.StringIO()) as output):
                self.assertEqual(turn_end.main(["--handle-hook"]), 0)
            handler.assert_called_once_with(expected)
            start.assert_not_called()
            self.assertEqual(output.getvalue(), "")


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
                 for close in ("closed", "setup", "outside")]
        cases.extend((family, "closed", transcript) for family in ("codex", "claude")
                     for transcript in ("no final reply", "older reply"))
        cases.append(("codex", "scratch", "current"))
        cases.append(("codex", "closed", "trailing period"))
        cases.extend((family, "working", "current") for family in ("codex", "claude"))
        cases.append(("codex", "held-lock", "current"))
        for family, close, transcript in cases:
            with self.subTest(family=family, close=close, transcript=transcript):
                _ = write_registry(self.case.fixture.root / "conf/models.conf", {"assignments": {"direct": family}})
                name = "impl-" + family + "-" + close + "-" + transcript.replace(" ", "-")
                self.case.agent.finish_after = int(transcript != "current")
                self.assertIs(self.case.dispatch(name), worker.DispatchSubmission.SUBMITTED)
                pane = self.case.agent.submitted[-1][0]
                row = self.case.agent.row(pane)
                kept_folders: list[Path] = []
                if close == "scratch":
                    peer = fixture_worker("scratch-peer", self.case.fixture.parent.id)
                    with patch.dict(os.environ, {"CODEX_THREAD_ID": str(row.current_session)}):
                        folder = scratch.new_worker()
                        atomic_file.write_text_atomic(folder / "trial.txt", "worker output")
                    with patch.dict(os.environ, {"CODEX_THREAD_ID": str(peer.current_session)}):
                        kept = scratch.new_worker()
                        atomic_file.write_text_atomic(kept / "peer.txt", "peer output")
                        kept_folders.append(kept)
                active = progress_history.active_dispatch(row.id, self.case.run_dir)
                self.assertIsInstance(active, progress_history.ActiveDispatch)
                assert isinstance(active, progress_history.ActiveDispatch)
                if transcript != "current":
                    if transcript == "older reply":
                        self.case.agent.append(pane, "assistant", "The previous result is pending.")
                    self.case.agent.replies.append("The work is complete.\nDONE " + active.instance_id
                                                  + ("." if transcript == "trailing period" else ""))
                    self.case.fixture.world.locations[pane]["agent_status"] = "done"
                payload: dict[str, object] = {
                    "hook_event_name": "Stop", "session_id": str(row.current_session), "transcript_path": "unused",
                    "stop_hook_active": False, "last_assistant_message": self.case.agent.replies[-1]}
                if family == "codex":
                    payload.update({"turn_id": "turn-1", "cwd": str(self.case.fixture.root), "model": "sol"})
                if close == "working":
                    self.case.fixture.world.locations[pane]["agent_status"] = "working"
                if close == "setup":
                    self.case.fixture.world.foreground[pane] = (998598, "direnv", ["direnv", "export", "zsh"])
                elif close == "outside":
                    self.case.fixture.world.remove_leaf(pane)
                    self.case.fixture.world.layouts["w1:outside"] = {"type": "pane", "pane_id": pane}
                    self.case.fixture.world.locations[pane]["tab_id"] = "w1:outside"
                notices = len(self.case.agent.notices)
                if close == "held-lock":
                    with ThreadPoolExecutor(max_workers=1) as hooks:
                        with worker_panes.worker_lock(self.case.fixture.parent.id, staff.WorkerName.parse(name)) as hold:
                            self.assertIsInstance(hold, worker_panes.WorkerLockHold)
                            pending = hooks.submit(turn_end.handle, turn_end.hook_event(payload))
                            with self.assertRaises(TimeoutError):
                                _ = pending.result(timeout=0.05)
                        self.assertEqual(pending.result(timeout=LauncherHangGuard("queued DONE hook").remaining()),
                                         turn_end.TurnOutcome.DONE)
                else:
                    self.assertEqual(turn_end.handle(turn_end.hook_event(payload)), turn_end.TurnOutcome.DONE)
                    if transcript != "current":
                        self.assertNotIn((pane, "terminal-" + pane), self.case.fixture.world.closed)
                        self.assertIn(pane, self.case.fixture.world.locations)
                        self.case.agent.append(pane, "assistant", self.case.agent.replies[-1])
                        self.assertEqual(turn_end.handle(turn_end.hook_event(payload)), turn_end.TurnOutcome.DONE)
                self.assertEqual(turn_end.handle(turn_end.hook_event(payload)), turn_end.TurnOutcome.IGNORED)
                self.assertEqual(len(self.case.agent.notices), notices + 1)
                self.assertIn(f"{name}: phase 8: done; reply {self.case.reply}", self.case.agent.notices[-1])
                self.assertEqual(self.case.reply.read_text().strip(), self.case.agent.replies[-1])
                self.assertIsInstance(progress_history.active_dispatch(row.id, self.case.run_dir), progress_history.NoActiveDispatch)
                self.assertEqual(worker_panes.worker_pass(row, self.case.run_dir)["status"], "completed")
                self.assertIn((pane, "terminal-" + pane), self.case.fixture.world.closed)
                self.assertNotIn(pane, self.case.fixture.world.locations)
                if close == "scratch":
                    self.assertEqual(list(Path(os.environ["TMPDIR"]).iterdir()), kept_folders)
                    self.assertEqual((kept_folders[0] / "peer.txt").read_text(), "peer output")
                self.assertEqual(staff.by_id(row.id), row)
                log = self.case.fixture.root / "state/herdr/turn-end-ignored.jsonl"
                entry = cast(dict[str, object], json.loads(log.read_text().splitlines()[-1]))
                self.assertEqual((entry["thread_id"], entry["worker_name"], entry["reason"]),
                    (row.current_session, name, "the hook completion is already finished"))

    def test_a_codex_quota_refusal_is_reported_once_with_its_stopped_command(self) -> None:
        for shared_sessions in (False, True):
            with self.subTest(shared_sessions=shared_sessions):
                _ = write_registry(self.case.fixture.root / "conf/models.conf", {"assignments": {"direct": "codex"}})
                _ = shutil.copyfile(Path(__file__).resolve().parents[2] / "config/accounts.conf",
                                    self.case.fixture.root / "conf/accounts.conf")
                self.case.agent.finish_after = 1
                self.assertIs(self.case.dispatch("impl-shared" if shared_sessions else "impl"), worker.DispatchSubmission.SUBMITTED)
                row = self.case.agent.row(self.case.agent.submitted[-1][0])
                backend = staff.backend(row)
                assert isinstance(backend, staff.CodexBackend)
                account_home = backend.codex_home
                if shared_sessions:
                    shared = Path.home() / ".codex" / "sessions"
                    shared.mkdir(parents=True, exist_ok=True)
                    transcript = worker_admission.transcript_source(row, backend)
                    if transcript.parent != shared:
                        _ = shutil.copyfile(transcript, shared / transcript.name)
                        transcript.unlink()
                    account_home = Path.home() / ".codex1"
                    account_home.mkdir(exist_ok=True)
                    folder = account_home / "sessions"
                    if folder.is_dir() and not folder.is_symlink():
                        shutil.rmtree(folder)
                    if not folder.is_symlink():
                        folder.symlink_to(shared, target_is_directory=True)
                    row = staff.set_home(row.id, staff.SessionHome(account_home))
                    backend = staff.CodexBackend(account_home)
                account = agent_accounts.KnownCodexAccount(account_home, "worker@example.com", "codex1")
                refused_at = "2026-11-10T19:30:38.842000+00:00"
                def account_at(home: Path) -> agent_accounts.KnownCodexAccount | agent_accounts.UnknownCodexAccount:
                    return account if home == account_home else agent_accounts.UnknownCodexAccount(home, "fixture has no sign-in")

                with (patch.object(quota_alert, "blocked", return_value=["workers moved"]) as blocked,
                      patch.object(agent_accounts, "codex_account", side_effect=account_at),
                      patch.dict(os.environ, {"CODEX_HOME": str(Path.home() / ".codex") if shared_sessions else str(account_home)}),
                      patch.object(note_file, "read_notes", return_value=iter(()))):
                    self.assertEqual(turn_end.report_quota_refusals(), [])
                    # A later refusal in the same pass sends no second notice.
                    for timestamp, expected in ((refused_at, ["workers moved"]), ("2026-11-10T19:35:00+00:00", [])):
                        with worker_admission.transcript_source(row, backend).open("a", encoding="utf-8") as stream:
                            _ = stream.write(json.dumps({"type": "event_msg", "timestamp": timestamp, "payload": {
                                "type": "task_complete", "last_agent_message": None, "error": {
                                    "message": "You’ve hit your usage limit. Try again at Nov 15th, 2026 1:34 PM.",
                                    "codex_error_info": "usage_limit_exceeded"}}}) + "\n")
                        self.assertEqual(turn_end.report_quota_refusals(), expected)
                        self.assertEqual(turn_end.report_quota_refusals(), [])
                blocked.assert_called_once_with([], account, window=refusals.UnknownWindow(),
                    refused_at=quota_alert.RefusedAt(datetime.fromisoformat(refused_at)),
                    stopped=quota_alert.StoppedWorker(str(self.case.fixture.parent.current_session), row.name))

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
                    with patch.object(self.case.fixture.world, "close_terminal_pane", return_value=False):
                        self.assertEqual(self.case.finish(name), turn_end.TurnOutcome.DONE)
                    notice = self.case.agent.notices[-1]
                    passed = worker_panes.worker_pass(row, self.case.run_dir)
                    completions.append(notice)
                    self.assertIn(f"; pass {passed['instance_id']}", notice)
                    self.assertIn(f"anomaly: {name}: pane close is incomplete; kept pane {pane}", notice)
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
                (turn_end.hook_event({"type": "agent-turn-complete", "thread-id": str(row.current_session)}).thread_id,
                 "unknown", "no staff row for the current thread"),
                ("", "unknown", "no staff row for the current thread")):
            with self.subTest(identity=identity):
                self.assertEqual(turn_end.handle(turn_end.WorkerEvent(str(identity))), turn_end.TurnOutcome.IGNORED)
                entry = cast(dict[str, object], json.loads(log.read_text().splitlines()[-1]))
                self.assertEqual((entry["thread_id"], entry["worker_name"], entry["reason"]), (identity, name, reason))
        self.assertEqual(len(log.read_text().splitlines()), 5)
        self.assertEqual((self.case.run_dir / "progress_history_state.json").read_bytes(), before)
        self.assertEqual(self.case.agent.notices, [])
        self.assertEqual(self.case.agent.submitted, [])

        for cause, event, closes in (
                ("empty delivery", turn_end.EventKind.TURN_END, True),
                ("missing prompt", turn_end.EventKind.TURN_END, False),
                ("ended pass", turn_end.EventKind.TURN_END, True),
                ("working turn end", turn_end.EventKind.TURN_END, True),
                ("live run", turn_end.EventKind.TURN_END, False),
                ("pending bootstrap", turn_end.EventKind.TURN_END, False),
                ("no pending assignment", turn_end.EventKind.TURN_END, True),
                ("live launcher", turn_end.EventKind.TURN_END, False),
                ("pending assignment", turn_end.EventKind.TURN_END, False),
                ("empty delivery", turn_end.EventKind.RUN_FINISHED, False),
                ("empty delivery", turn_end.EventKind.STAGE_READY, False)):
            with self.subTest(cause=cause, event=event), ExitStack() as cleanup:
                name = (cause + "-" + event.value).replace(" ", "-")
                opened = self.case.opened(name)
                if cause == "pending assignment":
                    self.case.agent.finish_after = 100
                    self.assertIs(self.case.dispatch(name), worker.DispatchSubmission.SUBMITTED)
                elif cause not in ("pending bootstrap", "no pending assignment"):
                    instance = progress_history.begin_dispatch(self.case.run_dir, slot=name, phase="8", role="impl",
                        worker_name=name, worker_id=opened.row.id, launcher_command=["assign.sh"], prompt=self.case.prompt, kind="impl", lens="")
                    if cause == "missing prompt":
                        progress_history.save_dispatch_delivery(self.case.run_dir, instance,
                            progress_history.DispatchDelivery(f"Read the task. DONE {instance}", instance,
                                                              str(self.case.reply)))
                    elif cause == "ended pass":
                        progress_history.complete_dispatch(self.case.run_dir, name, instance, status="error")
                    elif cause == "live launcher":
                        supervisor = cleanup.enter_context(subprocess.Popen(
                            [sys.executable, "-c", "import sys; sys.stdin.read()", "/fixture/assign.py", "impl"],
                            stdin=subprocess.PIPE, text=True))
                        _ = cleanup.callback(supervisor.kill)
                        assert supervisor.stdin is not None
                        _ = cleanup.callback(supervisor.stdin.close)
                        path = self.case.run_dir / "progress_history_state.json"
                        fields = cast(dict[str, object], json.loads(path.read_text()))
                        passed = cast(dict[str, dict[str, object]], fields["pass"])[name]
                        passed.update(launcher_pid=supervisor.pid, launcher_command=["bash", "/fixture/assign.sh", "impl"])
                        atomic_file.write_text_atomic(path, json.dumps(fields))
                    elif cause == "live run":
                        path, record = self.case.detached_record(opened.row, "idle-live-run")
                        supervisor = cleanup.enter_context(subprocess.Popen(
                            [sys.executable, "-c", "import sys; sys.stdin.read()", "detached_runs.py", "supervise", str(path)],
                            stdin=subprocess.PIPE, text=True))
                        _ = cleanup.callback(supervisor.kill)
                        assert supervisor.stdin is not None
                        _ = cleanup.callback(supervisor.stdin.close)
                        record.update(status="running", pid=supervisor.pid)
                        atomic_file.write_text_atomic(path, json.dumps(record))
                self.case.fixture.world.locations[opened.pane]["agent_status"] = (
                    "working" if cause == "working turn end" else "idle")
                notices = list(self.case.agent.notices)
                if cause == "pending bootstrap":
                    with ThreadPoolExecutor(max_workers=1) as hooks:
                        with worker_panes.worker_lock(self.case.fixture.parent.id, staff.WorkerName.parse(name)) as hold:
                            self.assertIsInstance(hold, worker_panes.WorkerLockHold)
                            pending = hooks.submit(turn_end.handle, turn_end.WorkerEvent(str(opened.row.current_session), reply="ready"))
                            with self.assertRaises(TimeoutError):
                                _ = pending.result(timeout=0.05)
                            instance = progress_history.begin_dispatch(self.case.run_dir, slot=name, phase="8", role="impl",
                                worker_name=name, worker_id=opened.row.id, launcher_command=["assign.sh"],
                                prompt=self.case.prompt, kind="impl", lens="")
                            progress_history.save_dispatch_delivery(self.case.run_dir, instance,
                                progress_history.DispatchDelivery("The pending assignment.", instance, str(self.case.reply)))
                            before = (self.case.run_dir / "progress_history_state.json").read_bytes()
                        self.assertEqual(pending.result(timeout=LauncherHangGuard("queued bootstrap hook").remaining()),
                                         turn_end.TurnOutcome.IGNORED)
                else:
                    before = (self.case.run_dir / "progress_history_state.json").read_bytes()
                    self.assertEqual(self.case.finish(name, event), turn_end.TurnOutcome.IGNORED)
                self.assertEqual(opened.pane not in self.case.fixture.world.locations, closes)
                self.assertEqual((self.case.run_dir / "progress_history_state.json").read_bytes(), before)
                self.assertEqual(self.case.agent.notices, notices)
                self.assertEqual(staff.by_id(opened.row.id), opened.row)

        with self.subTest(cause="consultation awaits its reply"):
            caller = self.case.opened("consultation-caller")
            with patch.dict(os.environ, {"HERDR_PANE_ID": caller.pane, "CODEX_THREAD_ID": str(caller.row.current_session)}):
                consultation = side_pane.open("private-consultation", worker_panes.launch_policy("impl"),
                                              cwd=self.case.fixture.root)
            self.assertIsInstance(consultation, side_pane.OpenSidePane)
            assert isinstance(consultation, side_pane.OpenSidePane)
            pid = self.case.fixture.world.foreground[consultation.pane][0]
            process = self.case.fixture.world.processes[pid]
            process = replace(process, identity=herdr.PaneSessionReport(process.identity.id))
            self.case.fixture.world.processes[pid] = process
            self.assertEqual(turn_end.handle(turn_end.WorkerEvent(str(process.current_identity))), turn_end.TurnOutcome.IGNORED)
            self.assertIn(consultation.pane, self.case.fixture.world.locations)

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
        for ready_first, hook_only, instruction in (
                (True, False, "new"), (False, False, "new"), (True, True, "new"), (True, False, "old")):
            with self.subTest(ready_first=ready_first, hook_only=hook_only, instruction=instruction):
                name = ("review-hook" if hook_only else "review-first" if ready_first else "review-last") + "-" + instruction
                ready = self.case.run_dir / f"{name}.ready"
                code = worker.dispatch(self.case.fixture.parent.id, staff.WorkerName.parse(name), "review", self.case.prompt,
                    session_dir=self.case.run_dir, slot=name, phase="8", kind="review", launcher_command=[name],
                    early_ready=ready, reply=self.case.reply)
                self.assertIs(code, worker.DispatchSubmission.SUBMITTED)
                pane = self.case.agent.submitted[-1][0]
                row = self.case.agent.row(pane)
                active = progress_history.active_dispatch(row.id, self.case.run_dir)
                assert isinstance(active, progress_history.ActiveDispatch)
                if instruction == "old":
                    passed = worker_panes.worker_pass(row, self.case.run_dir)
                    delivery = progress_history.dispatch_delivery(passed)
                    old_line = (f'Read the unchanged prompt file "{self.case.prompt}". '
                                f"End your final reply with DONE {active.instance_id}. "
                                "A reply that waits on a detached run is not final.")
                    progress_history.save_dispatch_delivery(self.case.run_dir, active.instance_id,
                                                           replace(delivery, line=old_line))
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
                stage_line = self.case.agent.submitted[-1][1]
                ending = ". " if instruction == "old" else "\n"
                self.assertIn(f"DONE {active.instance_id}-2{ending}", stage_line)
                with patch.dict(os.environ, {"CODEX_THREAD_ID": str(self.case.fixture.parent.current_session)}):
                    self.assertEqual(turn_end.main(["stage-ready", name]), 0)
                self.assertEqual(len(self.case.agent.submitted), before + 1)
                self.case.agent.complete(pane)
                self.assertEqual(self.case.finish(name), turn_end.TurnOutcome.DONE)

    def test_a_turn_ending_question_reaches_the_director_without_ending_the_pass(self) -> None:
        for name, reply in (("question", "Which output format must I use? JSON or Markdown?"),
                            ("recorder", "The two file-clock changes are complete. Results are pending."),
                            ("subagent", "The worker result is pending.")):
            with self.subTest(name=name):
                self.case.agent.without_done = True
                self.case.agent.final = reply
                notices = len(self.case.agent.notices)
                self.assertIs(self.case.dispatch(name), worker.DispatchSubmission.SUBMITTED)
                pane = self.case.agent.submitted[-1][0]
                row = self.case.agent.row(pane)
                active = progress_history.active_dispatch(row.id, self.case.run_dir)
                event = turn_end.EventKind.TURN_END
                if name == "subagent":
                    assert isinstance(active, progress_history.ActiveDispatch)
                    backend = staff.backend(row)
                    assert isinstance(backend, (staff.CodexBackend, staff.ClaudeBackend))
                    source = worker_admission.transcript_source(row, backend)
                    with source.open("a") as stream:
                        _ = stream.write(json.dumps({"type": "response_item", "payload": {
                            "type": "agent_message", "author": "/root/reviewer", "recipient": "/root",
                            "content": [{"type": "input_text", "text": "The review is complete.\nDONE " + active.instance_id}],
                        }}) + "\n")
                    event = turn_end.EventKind.RUN_FINISHED
                self.assertEqual(self.case.finish(name, event), turn_end.TurnOutcome.NO_DONE)
                self.assertEqual(self.case.finish(name, event), turn_end.TurnOutcome.NO_DONE)
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

        for recovery in ("plain", "same hold", "failed notice"):
            with (self.subTest(recovery="dispatch in another run", case=recovery),
                  patch.dict(os.environ, {"ASTRA_ROLES_STATE": str(self.case.fixture.root / "holds")}),
                  self.case.fixture.world.process_starts()):
                name = "late-" + recovery.replace(" ", "-")
                role: staff.WorkerRole = "astra-reviewer" if recovery == "same hold" else "impl"
                self.case.agent.finish_after = 0
                opened = self.case.opened(name, role)
                row = opened.row
                actions = (progress_history.CompletionActions(end_worker=True, held_role=str(row.id))
                           if recovery == "same hold" else progress_history.NO_COMPLETION_ACTIONS)
                if recovery == "same hold":
                    pid = self.case.fixture.world.foreground[opened.pane][0]
                    held = astra_roles.hold("reviewer", astra_roles.ExecProcess(pid), str(row.id))
                    self.assertNotIsInstance(held, astra_roles.HoldWorkerUnavailable)
                first_run = self.case.run_dir
                next_run = self.case.fixture.root / ("next-run-" + recovery.replace(" ", "-"))
                next_state = cast(dict[str, object], json.loads((first_run / "progress_history_state.json").read_text()))
                next_state["history_file"] = str(next_run / "history.jsonl")
                atomic_file.write_text_atomic(next_run / "progress_history_state.json", json.dumps(next_state))
                first_reply = first_run / (name + "-reply.md")
                self.assertIs(worker.dispatch(self.case.fixture.parent.id, staff.WorkerName.parse(name), role, self.case.prompt,
                    session_dir=first_run, slot=name, phase="8", kind=role, launcher_command=["first-assign"],
                    reply=first_reply, completion_actions=actions), worker.DispatchSubmission.SUBMITTED)
                pane = self.case.agent.submitted[-1][0]
                row = self.case.agent.row(pane)
                passed = worker_panes.worker_pass(row, first_run)
                instance = str(passed["instance_id"])
                worker.save_reply(first_reply, self.case.agent.replies[-1])
                progress_history.save_hook_completion(first_run, instance,
                    progress_history.HookCompletion(turn_end.TurnOutcome.DONE, str(first_reply), role_released=False))
                self.assertEqual(worker_panes.worker_run(row), first_run)
                notices = len(self.case.agent.notices)
                closed = self.case.fixture.world.closed.copy()
                self.case.agent.finish_after = 1
                self.case.agent.notice_failures = int(recovery == "failed notice")
                if recovery == "same hold":
                    pid = self.case.fixture.world.foreground[pane][0]
                    held = astra_roles.hold("reviewer", astra_roles.ExecProcess(pid), str(row.id))
                    self.assertNotIsInstance(held, astra_roles.HoldWorkerUnavailable)
                with redirect_stderr(io.StringIO()) as errors:
                    self.assertIs(worker.dispatch(self.case.fixture.parent.id, staff.WorkerName.parse(name), role, self.case.prompt,
                        session_dir=next_run, slot=name, phase="8", kind=role, launcher_command=["next-assign"],
                        reply=next_run / "reply.md", completion_actions=actions), worker.DispatchSubmission.SUBMITTED)
                if recovery == "failed notice":
                    self.assertIn(f"late result of pass {instance} was not reported", errors.getvalue())
                    self.assertIn(str(first_reply), errors.getvalue())
                else:
                    self.assertEqual(len(self.case.agent.notices), notices + 1)
                    notice = self.case.agent.notices[-1]
                    self.assertIn(f"; pass {instance}", notice)
                    self.assertIn(f"late result of pass {instance}", notice)
                if recovery == "same hold":
                    live, = astra_roles.live_roles(staff.snapshot())
                    self.assertEqual(live["staff_id"], row.id)
                    self.assertIsNotNone(staff.by_id(row.id))
                    self.assertEqual(errors.getvalue(), "")
                self.assertIn(pane, self.case.fixture.world.locations)
                self.assertEqual(self.case.fixture.world.closed, closed)
                self.assertEqual(worker_panes.worker_run(row), next_run)

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
            event = turn_end.hook_event({"hook_event_name": "Stop", "session_id": str(current.row.current_session),
                                         "last_assistant_message": old_reply})
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
            self.assertEqual(staff.by_id(row.id), replace(row, role=staff.KnownWorkerRole("astra-reviewer")))
            self.case.agent.complete(self.case.agent.submitted[-1][0])
            self.assertEqual(self.case.finish(row.name), turn_end.TurnOutcome.DONE)
            self.assertFalse(hold.exists())
            self.assertIsNone(staff.by_id(row.id))

    def test_the_astra_launcher_returns_with_an_active_pass_and_a_worker_process_hold(self) -> None:
        router = test_worker.PaneCommandRouter()
        router.setup(self, self.case.fixture.root)
        args = assign.Arguments(new_worker="astra-reviewer", summary=str(self.case.reply), policy="astra-reviewer")
        hold_role = astra_roles.hold
        bootstrap_hooks: list[Future[turn_end.TurnOutcome]] = []

        def bootstrap_during_hold(role: astra_roles.AstraRole, lifetime: astra_roles.ExecProcess | astra_roles.HeldDesignRole,
                                  staff_id: str = "") -> astra_roles.HeldRole | astra_roles.HoldWorkerUnavailable:
            row = staff.by_id(staff.StaffId(staff_id))
            assert row is not None
            state = worker_panes.observe_worker(self.case.fixture.parent.id, row, staff.snapshot())
            assert isinstance(state, worker_panes.OpenWorker)
            self.assertIsInstance(progress_history.active_dispatch(row.id, self.case.run_dir), progress_history.NoActiveDispatch)
            with worker_panes.worker_lock(self.case.fixture.parent.id, staff.WorkerName.parse(row.name)) as competing:
                self.assertIsInstance(competing, worker_panes.Kept)
            bootstrap_hooks.append(hooks.submit(turn_end.handle, turn_end.WorkerEvent(str(row.current_session), reply="ready")))
            self.assertIn(state.pane, self.case.fixture.world.locations)
            return hold_role(role, lifetime, staff_id)

        with (ThreadPoolExecutor(max_workers=1) as hooks,
              patch.dict(os.environ, {"ASTRA_ROLES_STATE": str(self.case.fixture.root / "holds")}),
              patch.object(astra_roles, "hold", side_effect=bootstrap_during_hold),
              self.case.fixture.world.process_starts()):
            code = assign.execute_astra_review_herdr(args, self.case.run_dir, self.case.fixture.root,
                self.case.prompt, dict(os.environ))
            self.assertIs(code, worker.DispatchSubmission.SUBMITTED)
            self.assertEqual(bootstrap_hooks.pop().result(timeout=LauncherHangGuard("review bootstrap hook").remaining()),
                             turn_end.TurnOutcome.IGNORED)
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
