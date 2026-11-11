"""Worker commands preserve native identity and use transcripts as dispatch receipts."""
from __future__ import annotations

import io
import fcntl
import json
import math
import os
from pathlib import Path
from datetime import datetime, timezone
import subprocess
import sys
import time
from threading import Timer
from collections.abc import Callable, Iterator
from dataclasses import replace
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from typing import Literal, TextIO, cast, final, override
import unittest
from unittest.mock import patch

from scripts.agents import detached_runs, worker_lifecycle
from scripts.herdr import herdr_types
from scripts.accounts import accounts
from scripts.herdr import test_worker_panes as pane_fixtures
from scripts.herdr import turn_end, worker_panes
from scripts.direct import assign, progress_history
from scripts.direct.recorder_fixture import invoke_recorder
from scripts.production.cli_fixture import invoke_cli
from scripts.production import fake_unit_host, staff, worker, worker_admission
from scripts.production.staff_test_support import fixture_member, fixture_session, record_fixture_claude_start, fixture_runtime_id, fixture_worker
from scripts.lib import atomic_file, unit_runs
from scripts.lib.test_models_registry import write_registry
from scripts.lib.test_hang_guard import LauncherHangGuard
from scripts.message import send, sessions
from scripts.usage import agents_log

from scripts.herdr import herdr


@final
class DispatchAgent:
    """A private agent writes real transcript records at the process boundary."""

    def __init__(self, fixture: pane_fixtures.WorkerPaneTests) -> None:
        self.fixture = fixture
        self.submitted: list[tuple[str, str]] = []
        self.compactions: list[tuple[str, int]] = []
        self.compacting: set[str] = set()
        self.compact_error = ""
        self.notice_failures = 0
        self.members: dict[str, staff.StaffId] = {}
        self.replies: list[str] = []
        self.last_replies: dict[str, str] = {}
        self.notices: list[str] = []
        self.notice_senders: list[send.SenderStamp] = []
        self.questions: dict[str, str | herdr_types.HerdrFailure] = {}
        self.answers: list[tuple[str, str]] = []
        self.answer_inputs: dict[str, str] = {}
        self.answer_enters: dict[str, int] = {}
        self.waited: list[str] = []
        self.defer_prompt_transcript = False
        self.finish_after = 0
        self.wait_count = 0
        self.final = "The work is complete."
        self.error = ""
        self.interrupt = ""
        self.during_prompt: Callable[[], None] = lambda: None
        self.without_done = False
        self.after_complete: Callable[[str], None] = lambda pane: None
        self.consultation_delivery: Literal["prompt", "second wait", "never"] = "prompt"
        self.run_process = cast(Callable[..., subprocess.CompletedProcess[str]], subprocess.run)

    def row(self, pane: str) -> staff.StaffRow:
        record = staff.snapshot()
        if pane in self.fixture.world.foreground:
            pid = self.fixture.world.foreground[pane][0]
            identity = self.fixture.world.processes[pid].current_identity
            row = record.by_current_session(identity)
        else:
            row = record.by_id(self.members[pane])
        assert row is not None
        return row

    def append(self, pane: str, role: str, text: str) -> None:
        pid, family, _ = self.fixture.world.foreground[pane]
        process = self.fixture.world.processes[pid]
        row = staff.snapshot().by_current_session(process.current_identity)
        if row is None:
            backend = staff.CodexBackend(Path(process.environment["CODEX_HOME"])) if family == "codex" else staff.ClaudeBackend()
            paths = (list((backend.codex_home / "sessions").rglob(f"*{process.current_identity}.jsonl"))
                     if isinstance(backend, staff.CodexBackend) else sessions.transcript_locations(process.current_identity))
            path = max(paths, key=lambda source: source.stat().st_mtime)
        else:
            self.members[pane] = row.id
            backend = staff.backend(row)
            assert isinstance(backend, (staff.ClaudeBackend, staff.CodexBackend))
            if isinstance(backend, staff.ClaudeBackend) and not sessions.transcript_locations(row.current_session):
                fixture_session(row.current_session, backend)
            path = worker_admission.transcript_source(row, backend)
        record = ({"type": "event_msg", "payload": {"type": "user_message" if role == "user" else "agent_message", "message": text}}
                  if isinstance(backend, staff.CodexBackend) else
                  {"type": role, "message": {"role": role, "content": [{"type": "text", "text": text}]}})
        with path.open("a", encoding="utf-8") as stream:
            _ = stream.write(json.dumps(record) + "\n")
        if role == "assistant":
            self.last_replies[str(process.current_identity)] = text

    def complete(self, pane: str) -> None:
        line = next(text for target, text in reversed(self.submitted) if target == pane)
        dispatch_id = line.split("DONE ")[-1].split()[0].removesuffix(".").strip()
        response = self.final if self.without_done else self.final + "\nDONE " + dispatch_id
        self.replies.append(response)
        self.append(pane, "assistant", response)
        self.fixture.world.locations[pane]["agent_status"] = "done"
        self.after_complete(pane)

    def pane_commands(self, case: unittest.TestCase) -> None:
        run = self.fixture.world.run
        composer = self.fixture.world.composer

        def notify(options: send.Options, *, membership: staff.StaffSnapshot) -> send.Result:
            del membership
            if self.notice_failures:
                self.notice_failures -= 1
                return send.Result("failed", "director message failed")
            self.notices.append(send.body_text(options))
            origin = options.sender_origin
            self.notice_senders.append(origin.stamp if isinstance(origin, send.ForwardedSender) else send.sender_stamp())
            return send.Result("sent", "private director")

        _ = case.enterContext(patch.object(send, "_send", side_effect=notify))

        def accept(pane: str, text: str) -> None:
            del self.questions[pane]
            _ = self.answer_inputs.pop(pane, None)
            _ = self.answer_enters.pop(pane, None)
            self.answers.append((pane, text))
            self.append(pane, "user", text)
            self.error = ""
            self.fixture.world.locations[pane]["agent_status"] = "working"

        def question_input(pane: str) -> herdr.ComposerInput | herdr.NoComposer:
            return herdr.ComposerInput(self.answer_inputs.get(pane, "")) if pane in self.questions else composer(pane)

        def command(*arguments: str, deadline: float = math.inf) -> str:
            result = run(*arguments, deadline=deadline)
            if arguments[:2] == ("pane", "read"):
                question = self.questions.get(arguments[2], result)
                if isinstance(question, herdr_types.HerdrFailure):
                    raise question
                return question
            if arguments[:2] == ("agent", "prompt") and arguments[2] in self.questions:
                accept(*arguments[2:4])
            elif arguments[:2] == ("pane", "send-text") and arguments[2] in self.questions:
                self.answer_inputs[arguments[2]] = arguments[3]
            elif arguments[:2] == ("pane", "send-keys") and arguments[2] in self.questions:
                pane, key = arguments[2:4]
                if key == "shift+left":
                    self.answer_inputs[pane] = ""
                    self.answer_enters[pane] = 0
                elif key == "enter":
                    self.answer_enters[pane] += 1
                    if self.answer_enters[pane] == 2:
                        accept(pane, self.answer_inputs[pane])
            return result

        _ = case.enterContext(patch.object(self.fixture.world, "run", side_effect=command))
        _ = case.enterContext(patch.object(self.fixture.world, "composer", side_effect=question_input))

    def consultations(self, case: unittest.TestCase) -> None:
        run = self.fixture.world.run
        wait_turn_end = self.fixture.world.wait_turn_end

        def submit(*arguments: str, deadline: float = math.inf) -> str:
            result = run(*arguments, deadline=deadline)
            if arguments[:2] == ("agent", "prompt"):
                pane, line = arguments[2:4]
                pid = self.fixture.world.foreground[pane][0]
                if staff.snapshot().by_current_session(self.fixture.world.processes[pid].current_identity) is None:
                    self.submitted.append((pane, line))
                    self.during_prompt()
                    if self.error:
                        raise herdr_types.HerdrFailure(self.error)
                    if self.consultation_delivery == "prompt":
                        self.append(pane, "user", line)
                        self.complete(pane)
            return result

        def wait(pane: str, timeout_ms: int) -> herdr.ObservedAgentState | herdr.WaitTimedOut:
            self.waited.append(pane)
            self.wait_count += 1
            if self.consultation_delivery != "prompt":
                self.fixture.world.turn_end_status = herdr.AgentStatus.IDLE
                if self.consultation_delivery == "second wait" and self.wait_count == 2:
                    line = next(text for target, text in reversed(self.submitted) if target == pane)
                    self.append(pane, "user", line)
                    self.complete(pane)
                    self.fixture.world.turn_end_status = herdr.AgentStatus.DONE
            return wait_turn_end(pane, timeout_ms)

        _ = case.enterContext(patch.object(self.fixture.world, "run", side_effect=submit))
        _ = case.enterContext(patch.object(self.fixture.world, "wait_turn_end", side_effect=wait))
        _ = case.enterContext(patch.object(os, "kill", side_effect=self.fixture.world.signal))

    def run(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args[0] != "/nonexistent/herdr":
            return self.run_process(args, **kwargs)
        action, pane = args[4:6]
        self.fixture.assertNotIn("--timeout", args)
        if "--until" in args:
            self.fixture.assertEqual(args[-2:], ["--until", "working"])
        if action == "prompt":
            line = args[6]
            if line == "/compact":
                self.fixture.assertEqual(args[-1], "--wait")
                if self.compact_error:
                    return subprocess.CompletedProcess(args, 1, json.dumps({"error": {
                        "code": "compact_failed", "message": self.compact_error}}), "")
                self.compacting.add(pane)
                self.fixture.world.locations[pane]["agent_status"] = "working"
                self.compactions.append((pane, len(self.submitted)))
                return subprocess.CompletedProcess(args, 0, "{}", "")
            if pane in self.compacting:
                self.fixture.assertIn(self.fixture.world.locations[pane]["agent_status"], ("idle", "done"))
                self.compacting.remove(pane)
            self.fixture.assertNotIn("--wait", args)
            self.fixture.assertNotIn("--until", args)
            self.submitted.append((pane, line))
            if not self.defer_prompt_transcript:
                self.append(pane, "user", line)
            self.during_prompt()
            if self.interrupt == "prompt":
                self.interrupt = ""
                raise OSError("interrupted after prompt")
            self.fixture.world.locations[pane]["agent_status"] = "idle" if self.error == "agent_prompt_stalled" else "working"
            if self.finish_after == 0 and not self.error and not self.defer_prompt_transcript:
                self.complete(pane)
        elif action == "wait":
            self.waited.append(pane)
            self.wait_count += 1
            if self.wait_count >= self.finish_after:
                self.complete(pane)
        else:
            raise AssertionError(args)
        if self.error:
            if self.error == "agent_blocked":
                self.fixture.world.locations[pane]["agent_status"] = "blocked"
            return subprocess.CompletedProcess(args, 1, json.dumps({"error": {"code": self.error, "message": self.error}}), "")
        return subprocess.CompletedProcess(args, 0, "{}", "")


@final
class PaneCommandRouter:
    """Run private pane commands through their real parsers and handlers."""

    def __init__(self) -> None:
        self.cwd = Path()
        self.commands: list[list[str]] = []
        self.worker_commands: list[list[str]] = []
        self.recorder_commands: list[list[str]] = []
        self.run_process = cast(Callable[..., subprocess.CompletedProcess[str]], subprocess.run)

    def setup(self, case: unittest.TestCase, cwd: Path) -> None:
        self.cwd = cwd
        self.run_process = cast(Callable[..., subprocess.CompletedProcess[str]], subprocess.run)
        _ = case.enterContext(patch.object(subprocess, "run", side_effect=self.run))
        _ = case.enterContext(patch.object(assign, "require_live_director"))

    def run(self, command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
        path = Path(command[1]).resolve() if len(command) > 1 else Path()
        root = Path(__file__).resolve().parents[2]
        arguments = command[2:]
        environment = cast(dict[str, str], options.get("env", os.environ.copy()))
        cwd = Path(cast(str | Path, options.get("cwd", self.cwd)))
        if path == root / "scripts/direct/assign.sh":
            self.commands.append(arguments.copy())
            result = invoke_cli(assign, assign.main, arguments, environment, cwd)
        elif path == root / "scripts/production/worker.py":
            self.worker_commands.append(arguments.copy())
            result = invoke_cli(worker, lambda: worker.main(arguments), arguments, environment, cwd)
        elif path == root / "scripts/direct/progress_history.py":
            self.recorder_commands.append(arguments.copy())
            result = invoke_recorder(arguments, environment, cwd)
        else:
            return self.run_process(command, **options)
        output = options.get("stdout")
        if output is not None:
            _ = cast(TextIO, output).write(result.stdout + result.stderr)
        elif not options.get("capture_output"):
            print(result.stdout, end="")
            print(result.stderr, end="", file=sys.stderr)
        return result


@final
class WorkerCommandTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.fixture = pane_fixtures.WorkerPaneTests("runTest")
        self.agent = DispatchAgent(self.fixture)
        self.run_dir = Path()
        self.prompt = Path()
        self.reply = Path()

    @override
    def setUp(self) -> None:
        self.fixture = pane_fixtures.WorkerPaneTests("runTest")
        self.fixture.setUp()
        _ = (self.fixture.root / "conf/build_targets.conf").write_text("targets=natedev,mac\nkind.natedev=local\nkind.mac=ssh\n")
        self.fixture.world.binary = "/nonexistent/herdr"
        self.addCleanup(self.fixture.doCleanups)
        config = self.fixture.root / "conf/direct.conf"
        atomic_file.write_text_atomic(config, config.read_text()
                                      + "# range: 1..600\nWORKER_SETTLE_SECONDS=30\n")
        atomic_file.write_text_atomic(config.with_name("send.conf"),
                                      (Path(__file__).resolve().parents[2] / "config/send.conf").read_text())
        atomic_file.write_text_atomic(config.with_name("scratch.conf"),
                                      (Path(__file__).resolve().parents[2] / "config/scratch.conf").read_text())
        root = self.fixture.root
        (root / "worker-tmp").mkdir()
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "TMPDIR": str(root / "worker-tmp"),
            "SCRATCH_ROOT": str(root / "scratch"),
            "SCRATCH_VISIBLE_ROOT": str(root / "visible-scratch"),
            "SCRATCH_COPIES_ROOT": str(root / "copies"),
            "SCRATCH_HOLDS": str(root / "scratch-holds.json"),
        })))
        self.agent = DispatchAgent(self.fixture)
        self.agent.pane_commands(self)
        self.run_dir = self.fixture.root / "run"
        self.run_dir.mkdir(exist_ok=True)
        atomic_file.write_text_atomic(Path.home() / ".codex/sessions" / f"{self.fixture.parent.current_session}.jsonl",
                                      json.dumps({"type": "turn_context", "payload": {
                                          "model": "fixture", "effort": "high"}}) + "\n")
        self.prompt = self.run_dir / "prompt.md"
        atomic_file.write_text_atomic(self.prompt, "Read the source and preserve its behavior.\n")
        self.reply = self.run_dir / "reply.md"
        atomic_file.write_text_atomic(self.run_dir / "progress_history_state.json", json.dumps({
            "phase": {"id": "8", "title": "Workers", "instance_id": "phase-eight", "status": "active", "started_at": 1},
            "pass": {}, "events": [], "project": {}, "unit": "unit",
            "history_file": str(self.run_dir / "history.jsonl"),
            "main_agent": {"session_id": str(self.fixture.parent.current_session),
                           "family": "claude", "model": "fixture", "effort": "high"},
        }))
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"CODEX_THREAD_ID": str(self.fixture.parent.current_session),
            "UNIT_PASS_OWNER": "launcher", "UNIT_TEAM_ROLE": "impl", "UNIT_SYNC_WRITES": "0"})))
        _ = self.enterContext(patch.object(subprocess, "run", side_effect=self.agent.run))

    def opened(self, name: str = "impl", role: staff.WorkerRole = "impl") -> worker_panes.OpenWorker:
        result = worker_panes.start_or_resume(self.fixture.parent.id, staff.WorkerName.parse(name), role)
        self.assertIsInstance(result, worker_panes.OpenWorker)
        assert isinstance(result, worker_panes.OpenWorker)
        return result

    def dispatch(self, name: str = "impl", role: staff.WorkerRole = "impl", dispatch_id: str = "") -> worker.DispatchSubmission | worker.ExitCode:
        return worker.dispatch(self.fixture.parent.id, staff.WorkerName.parse(name), role, self.prompt, dispatch_id,
            session_dir=self.run_dir, slot="impl", phase="8", kind=role,
            launcher_command=["assign.sh", "--phase", "8", "--prompt", str(self.prompt)], reply=self.reply)

    def unfinished_turn(self, role: staff.WorkerRole = "impl") -> None:
        """Prepare a submitted reply before the hook completes its pass."""
        self.agent.interrupt = "prompt"
        with self.assertRaisesRegex(OSError, "interrupted after prompt"):
            _ = self.dispatch(role=role)
        pane = self.agent.submitted[-1][0]
        self.agent.complete(pane)
        atomic_file.write_text_atomic(self.reply, self.agent.replies[-1] + "\n")

    def assert_pass_completed(self, previous: bytes) -> None:
        before = cast(dict[str, dict[str, dict[str, object]]], json.loads(previous))["pass"]["impl"]
        after = cast(dict[str, dict[str, dict[str, object]]],
                     json.loads((self.run_dir / "progress_history_state.json").read_text()))["pass"]["impl"]
        changed = {"status", "finished_at", "hook_notice", "hook_completion"}
        self.assertEqual({key: value for key, value in after.items() if key not in changed},
                         {key: value for key, value in before.items() if key not in changed})
        self.assertEqual(after["status"], "completed")

    def finish(self, name: str = "impl", kind: turn_end.EventKind = turn_end.EventKind.TURN_END) -> turn_end.TurnOutcome:
        row = next(row for row in staff.snapshot().find(kind="worker", parent=self.fixture.parent.id) if row.name == name)
        return turn_end.handle(turn_end.WorkerEvent(str(row.current_session), kind,
            self.agent.last_replies.get(str(row.current_session), "") if kind == turn_end.EventKind.TURN_END else ""))

    def receipt(self, name: str = "impl") -> worker.DispatchReceipt:
        row = next(row for row in staff.snapshot().find(kind="worker", parent=self.fixture.parent.id) if row.name == name)
        delivery = progress_history.dispatch_delivery(worker_panes.worker_pass(row, self.run_dir))
        return worker.dispatch_receipt(row, delivery.line, delivery.completion_id, run=self.run_dir)

    def command(self, *args: str) -> tuple[int, str, str]:
        with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
            code = worker.main(list(args))
        return code, output.getvalue(), errors.getvalue()

    def test_restore_reuses_given_runs_for_two_workers(self) -> None:
        first = self.fixture.worker_run("a-first", "unit", self.fixture.parent.current_session,
                                       director_run=True)
        for state in (self.opened("impl"), self.opened("review", "review")):
            _ = progress_history.begin_dispatch(self.run_dir, slot=state.row.name, phase="8", role="impl",
                worker_name=state.row.name, worker_id=state.row.id, launcher_command=["assign.sh"],
                prompt=self.prompt, kind="impl", lens="")
            _ = self.fixture.world.stop(state.row, self.fixture.world)
        record = staff.snapshot()
        runs = {str(self.fixture.parent.current_session): [str(first), str(self.run_dir)]}
        with (patch.object(unit_runs, "held_by_session", return_value=runs) as searches,
              patch.object(progress_history, "active_dispatch", wraps=progress_history.active_dispatch) as active,
              patch.object(worker, "_resume_launcher", return_value=worker.ExitCode.OK),
              redirect_stdout(io.StringIO())):
            self.assertEqual(worker.restore(self.fixture.parent.id, record=record, runs=runs), worker.ExitCode.OK)
        self.assertEqual(active.call_count, 2)
        self.assertEqual(searches.call_count, 0)
        self.assertEqual([call.args[1] for call in active.call_args_list], [self.run_dir, self.run_dir])

    def test_restore_starts_only_the_worker_with_an_active_dispatch(self) -> None:
        idle = self.opened("review", "review")
        _ = self.fixture.world.stop(idle.row, self.fixture.world)
        self.assertIsInstance(worker_panes.close(self.fixture.parent.id, staff.WorkerName.parse(idle.row.name)),
                              worker_panes.Closed)
        busy = self.opened()
        dispatch_id = progress_history.begin_dispatch(self.run_dir, slot="impl", phase="8", role="impl",
            worker_name=busy.row.name, worker_id=busy.row.id, launcher_command=["assign.sh", "--to", "impl"],
            prompt=self.prompt, kind="impl", lens="")
        _ = self.fixture.world.stop(busy.row, self.fixture.world)
        before = self.fixture.staff_state(), len(self.fixture.world.launches)
        with patch.object(worker, "_resume_launcher", return_value=worker.ExitCode.OK) as resume:
            self.assertEqual(self.command("restore"), (0, f"{self.fixture.parent.name}: workers restored\n", ""))
        resume.assert_called_once_with(replace(busy.row, role=staff.KnownWorkerRole("impl")),
            progress_history.ActiveDispatch(dispatch_id, ("assign.sh", "--to", "impl")))
        self.assertEqual(self.fixture.staff_state(), before[0])
        self.assertEqual(len(self.fixture.world.launches), before[1] + 1)
        states = {state.row.id: state for state in worker_panes.observe(self.fixture.parent.id, staff.snapshot())}
        self.assertIsInstance(states[busy.row.id], worker_panes.OpenWorker)
        resumed = states[busy.row.id]
        assert isinstance(resumed, worker_panes.OpenWorker)
        self.assertEqual(resumed.pane, busy.pane)
        self.assertEqual(resumed.terminal, busy.terminal)
        self.assertIsInstance(states[idle.row.id], worker_panes.ClosedWorker)
        self.assertEqual(states[busy.row.id].row.current_session, busy.row.current_session)
        self.assertEqual(states[idle.row.id].row.current_session, idle.row.current_session)

    def test_restore_rerun_after_agent_resume(self) -> None:
        opened = self.opened()
        _ = progress_history.begin_dispatch(self.run_dir, slot="impl", phase="8", role="impl",
            worker_name="impl", worker_id=opened.row.id, launcher_command=["bash", "assign.sh", "saved argument"],
            prompt=self.prompt, kind="impl", lens="")
        active = progress_history.active_dispatch(opened.row.id, self.run_dir)
        self.assertIsInstance(active, progress_history.ActiveDispatch)
        assert isinstance(active, progress_history.ActiveDispatch)
        _ = self.fixture.world.stop(opened.row, self.fixture.world)
        with patch.object(worker, "_resume_launcher", side_effect=OSError("interrupted before launcher")):
            self.assertEqual(worker.restore(self.fixture.parent.id), worker.ExitCode.UNCERTAIN)
        launches = len(self.fixture.world.launches)
        with patch.object(worker, "_resume_launcher", return_value=worker.ExitCode.OK) as resume:
            self.assertEqual(worker.restore(self.fixture.parent.id), worker.ExitCode.OK)
        resume.assert_called_once_with(replace(opened.row, role=staff.KnownWorkerRole("impl")), active)
        self.assertEqual(len(self.fixture.world.launches), launches)
        self.assertEqual(self.agent.submitted, [])
        self.assertEqual(progress_history.active_dispatch(opened.row.id, self.run_dir), active)

    def test_launcher_recovery_uses_the_real_lock_and_delivers_once(self) -> None:
        opened = self.opened()
        active = progress_history.ActiveDispatch("saved-pass", ("bash", "assign.sh", "argument with spaces"))
        self.fixture.dispatch(opened.row, "impl")
        opened = replace(opened, row=staff.set_worker_role(opened.row.id, staff.KnownWorkerRole("impl")))
        director = herdr.TabSession("director-pane", "unit", "workspace", str(self.fixture.parent.current_session))
        lock_path = staff.record_path().parent / "locks" / f"{opened.row.id}.lock"
        atomic_file.write_text_atomic(lock_path, "")
        for held in (True, False):
            _ = self.fixture.world.stop(opened.row, self.fixture.world)
            with (self.subTest(held=held), lock_path.open("r") as lock,
                  patch.object(self.fixture.world, "tab_sessions", return_value=[director]),
                  patch.object(progress_history, "active_dispatch", return_value=active),
                  patch.object(self.fixture.world, "prompt") as prompt,
                  patch.object(herdr, "check_target", return_value=True),
                  patch.object(subprocess, "run", wraps=self.agent.run) as start):
                if held:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertEqual(worker.restore(self.fixture.parent.id), worker.ExitCode.REFUSED if held else worker.ExitCode.OK)
                if held:
                    prompt.assert_not_called()
                    self.assertFalse(any("assign.sh" in str(call.args) for call in start.call_args_list))
                else:
                    prompt.assert_called_once()
                    self.assertFalse(any("assign.sh" in str(call.args) for call in start.call_args_list))
                    self.assertEqual(prompt.call_args.args[0], director.pane_id)
                    text = cast(str, prompt.call_args.args[1])
                    self.assertIn("Run this saved command from your session shell: bash assign.sh 'argument with spaces'", text)
                    self.assertIn("saved-pass", text)
                    self.assertIn(str(opened.row.id), text)
                    self.assertNotIn('<system-reminder source="script">', text)
                    self.assertEqual(prompt.call_args.kwargs, {"target": str(self.fixture.parent.current_session)})
                    with lock_path.open("r") as available:
                        fcntl.flock(available, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_launcher_delivery_failure_keeps_restore_uncertain(self) -> None:
        opened = self.opened()
        active = progress_history.ActiveDispatch("saved-pass", ("bash", "assign.sh"))
        self.fixture.dispatch(opened.row, "impl")
        opened = replace(opened, row=staff.set_worker_role(opened.row.id, staff.KnownWorkerRole("impl")))
        _ = self.fixture.world.stop(opened.row, self.fixture.world)
        director = herdr.TabSession("director-pane", "unit", "workspace", str(self.fixture.parent.current_session))
        with (patch.object(self.fixture.world, "tab_sessions", return_value=[director]),
              patch.object(progress_history, "active_dispatch", return_value=active),
              patch.object(self.fixture.world, "prompt", side_effect=herdr_types.HerdrFailure("delivery failed")),
              patch.object(herdr, "check_target", return_value=True),
              patch.object(subprocess, "run", side_effect=self.agent.run),
              redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(worker.restore(self.fixture.parent.id), worker.ExitCode.UNCERTAIN)
        self.assertNotIn("unchanged", output.getvalue())
        self.assertEqual(errors.getvalue(), "anomaly: impl: delivery failed\n")
        self.assertEqual(staff.by_id(opened.row.id), opened.row)

    def test_launcher_recovery_reports_an_absent_director(self) -> None:
        opened = self.opened()
        with (patch.object(self.fixture.world, "tab_sessions", return_value=[]),
              patch.object(self.fixture.world, "prompt") as prompt, redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(worker._resume_launcher(opened.row, progress_history.ActiveDispatch("saved-pass", ("assign.sh",))),  # pyright: ignore[reportPrivateUsage]
                             worker.ExitCode.UNCERTAIN)
        prompt.assert_not_called()
        self.assertEqual(errors.getvalue(), "anomaly: impl: director session is unavailable\n")

    def test_resume_workers_names_the_active_pass_and_idle_worker(self) -> None:
        from scripts.lib.resume_message import UnsubmittedAssignment, IdleWorker, PaneWorkerResume

        busy = self.opened()
        idle = self.opened("review", "review")
        self.fixture.dispatch(busy.row, "impl")
        path = self.run_dir / "progress_history_state.json"
        fields = {"pass": {"impl": {"status": "active", "instance_id": "dispatch-impl", "worker_name": "impl", "launcher_command": ["assign.sh", "same command"]}}}
        atomic_file.write_text_atomic(path, json.dumps(fields))
        with patch.object(unit_runs, "held", return_value=[str(self.run_dir)]):
            result = worker.resume_workers(self.fixture.parent.id, staff.snapshot())
        self.assertCountEqual(result.panes, [PaneWorkerResume(busy.row.id, UnsubmittedAssignment("dispatch-impl", ("assign.sh", "same command"))),
                                             PaneWorkerResume(idle.row.id, IdleWorker())])
        self.assertEqual(result.results, [])

    def test_resume_workers_keeps_a_claude_child_launcher_without_a_native_pass(self) -> None:
        from scripts.lib.resume_message import BusyPass, IdleWorker, PaneWorkerResume, Resumable, Unavailable, resume_message

        child = self.fixture.worker("legacy-child", family="claude")
        unavailable = self.fixture.worker("unavailable-child", family="claude")
        native = self.fixture.worker("native-child", family="claude")
        idle = self.fixture.worker("native-idle", family="claude")
        command = ["bash", str(self.fixture.root / "assign.sh"), "--role", "implement",
                   "--phase", "8", "--session-dir", str(self.run_dir), "--cwd", str(self.fixture.root),
                   "--prompt", str(self.prompt)]
        process = self.fixture.root / "proc" / str(os.getpid())
        process.mkdir(parents=True)
        _ = (process / "cmdline").write_bytes(b"\0".join(os.fsencode(word) for word in command) + b"\0")
        atomic_file.write_text_atomic(process / "stat", f"{os.getpid()} (private launcher) S 1 0 0")
        atomic_file.write_text_atomic(self.run_dir / "impl_worker_legacy-child.json", json.dumps({
            "staff_id": child.id, "slot": "legacy-child", "launcher_pid": os.getpid()}))
        atomic_file.write_text_atomic(self.run_dir / "impl_status_legacy-child", "implementing")
        for row in (unavailable, native):
            atomic_file.write_text_atomic(self.run_dir / f"impl_worker_{row.name}.json", json.dumps({
                "staff_id": row.id, "slot": row.name, "launcher_pid": 0}))
        atomic_file.write_text_atomic(self.run_dir / "impl_status_native-child", "implementing")
        with patch.dict(os.environ, {"USAGE_STATE_DIR": str(self.fixture.root / "usage")}):
            dispatch = progress_history.begin_dispatch(self.run_dir, slot="native-child", phase="8", role="impl",
                worker_name=native.name, worker_id=native.id, launcher_command=["assign.sh"], prompt=self.prompt,
                kind="impl", lens="")
            backend = staff.backend(native)
            assert isinstance(backend, staff.ClaudeBackend)
            source = worker_admission.transcript_source(native, backend)
            atomic_file.write_text_atomic(source, json.dumps({"type": "user", "message": {
                "role": "user", "content": f"End your final reply with DONE {dispatch}."}}) + "\n")
            agents_log.append(child.current_session, "impl", None, datetime.now(timezone.utc))
            agents_log.append(native.current_session, "impl", None, datetime.now(timezone.utc))
            result = worker.resume_workers(self.fixture.parent.id, staff.snapshot())
        saved = next(item for item in result.results if isinstance(item, Resumable))
        self.assertEqual(saved.seat["staff_id"], child.id)
        self.assertEqual(saved.seat["launcher_command"], command)
        self.assertTrue(saved.seat["turn_live"])
        self.assertEqual(result.cwd, str(self.fixture.root))
        missing = next(item for item in result.results if isinstance(item, Unavailable))
        self.assertEqual(missing.staff_id, unavailable.id)
        self.assertIn("last assignment is unavailable", missing.reason)
        child_ids = {staff.StaffId(item.staff_id if isinstance(item, Unavailable) else item.seat["staff_id"])
                     for item in result.results}
        self.assertEqual(child_ids, {child.id, unavailable.id})
        self.assertTrue(child_ids.isdisjoint(pane.staff_id for pane in result.panes))
        self.assertIn(PaneWorkerResume(native.id, BusyPass(dispatch)), result.panes)
        self.assertIn(PaneWorkerResume(idle.id, IdleWorker()), result.panes)
        text = resume_message("a shutdown", workers=result)
        self.assertIn("legacy-child (impl, was working):", text)
        self.assertIn(str(self.run_dir / "legacy-child.resume-prompt.md"), text)
        self.assertIn(missing.reason, text)

    def test_own_pass_reads_the_workers_parent_and_names_only_its_dispatch(self) -> None:
        from scripts.lib.resume_message import UnsubmittedAssignment, NO_OWN_PASS

        opened = self.opened()
        for active, expected in ((progress_history.ActiveDispatch("own-dispatch", ("assign.sh",)), UnsubmittedAssignment("own-dispatch", ("assign.sh",))),
                                 (progress_history.NoActiveDispatch(), NO_OWN_PASS),
                                 (progress_history.ActiveDispatchUnavailable("record unreadable"), NO_OWN_PASS)):
            with (self.subTest(active=active),
                  patch.object(worker_panes, "worker_run", return_value=self.run_dir) as run,
                  patch.object(progress_history, "active_dispatch", return_value=active) as read,
                  redirect_stderr(io.StringIO()) as errors):
                self.assertEqual(worker.own_pass(opened.row, staff.snapshot()), expected)
            run.assert_called_once_with(opened.row, record=staff.snapshot())
            read.assert_called_once_with(opened.row.id, self.run_dir)
            self.assertEqual(errors.getvalue(), "anomaly: impl: record unreadable\n"
                             if isinstance(active, progress_history.ActiveDispatchUnavailable) else "")

    def test_later_user_message_keeps_the_submitted_assignment_receipt(self) -> None:
        self.agent.interrupt = "prompt"
        with self.assertRaisesRegex(OSError, "after prompt"):
            _ = self.dispatch()
        pane = self.agent.submitted[0][0]
        self.agent.append(pane, "user", "Preserve the existing public interface too.")
        self.fixture.world.locations[pane]["agent_status"] = "working"
        self.assertIs(self.dispatch(), worker.DispatchSubmission.SUBMITTED)
        self.assertEqual(len(self.agent.submitted), 1)
        self.assertEqual(self.agent.waited, [])
        self.agent.complete(pane)
        self.assertEqual(self.finish(), turn_end.TurnOutcome.DONE)
        self.assertIn(self.agent.final, self.reply.read_text())

    def test_claude_done_reply_after_an_api_request_blob_completes_the_receipt(self) -> None:
        dispatch_id = "phase-28d"
        source = self.run_dir / "claude-transcript.jsonl"
        for instruction, lookup, suffix, completed in (
                ("old", "exact prompt", "", True), ("old", "dispatch id", "", True),
                ("new", "dispatch id", "", True), ("new", "dispatch id", ".", True),
                ("new", "dispatch id", "..", False), ("new", "dispatch id", " extra", False),
                ("new", "dispatch id", "\nLater text", False), ("new", "dispatch id", "-another", False)):
            with self.subTest(instruction=instruction, lookup=lookup, suffix=suffix):
                prompt = (f'Read the unchanged prompt file "{self.prompt}". ' + (
                    f"End your final reply with DONE {dispatch_id}." if instruction == "old" else
                    f"End your final reply with this line alone: DONE {dispatch_id}\n"
                    + "A reply that waits on a detached run is not final."))
                reply = "The work is complete.\nDONE " + dispatch_id + suffix
                records = [
                    {"type": "user", "message": {"role": "user", "content": prompt}},
                    {"type": "api-request-blob", "message": {"role": "user", "content": [{
                        "type": "text", "text": json.dumps({"messages": [{"role": "user", "content": prompt}]}),
                    }]}},
                    {"type": "assistant", "message": {"role": "assistant", "content": reply}},
                ]
                _ = source.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
                receipt = worker.transcript_receipt(worker_admission.transcript_entries(source, set()),
                    prompt if lookup == "exact prompt" else worker.DispatchPrompt(dispatch_id), worker.DoneReply(dispatch_id))
                expected = worker.DispatchComplete(reply) if completed else worker.DispatchSubmitted(reply)
                self.assertEqual(receipt, worker.TranscriptReceipt(expected, frozenset()))

    def test_agent_exit_after_done_still_publishes_the_reply(self) -> None:
        opened = self.opened()
        self.agent.finish_after = 1
        def exit_after_reply(pane: str) -> None:
            self.assertEqual(pane, opened.pane)
            _ = self.fixture.world.stop(opened.row, self.fixture.world)
        self.agent.after_complete = exit_after_reply
        with redirect_stderr(io.StringIO()) as errors:
            code = self.dispatch()
        self.assertEqual(code, worker.ExitCode.OK, errors.getvalue())
        self.agent.complete(opened.pane)
        self.assertEqual(self.finish(), turn_end.TurnOutcome.DONE)
        self.assertIn(self.agent.final, self.reply.read_text())
        self.assertNotIn(opened.pane, self.fixture.world.foreground)
        self.assertEqual(len(self.agent.submitted), 1)

    def test_named_assign_depends_on_its_own_pane_and_keeps_sibling_list_states(self) -> None:
        opened = self.opened("impl4")
        closed = [self.fixture.worker(name) for name in ("impl3", "fix1", "rev10a", "rev10c", "fix2", "port6", "ft7", "cr7")]
        for pane, reason in (("w1:p9", "agent argv is unavailable"), ("w2:p9", "pane_not_found"),
                             ("w2:p8", "current Codex rollout changed")):
            foreign = fixture_member("producer", name="foreign-" + pane.replace(":", "-"))
            self.fixture.world.add(foreign, pane, workspace=pane.split(":")[0],
                                   tab="w1:t1" if pane.startswith("w1:") else "w2:t2")
            self.fixture.world.unknown[pane] = reason
        self.fixture.world.process_reads.clear()
        self.fixture.world.reads.clear()
        panes_before = set(self.fixture.world.locations)
        self.agent.finish_after = 1
        with redirect_stderr(io.StringIO()) as errors:
            code = worker.assign(self.fixture.parent.id, "impl", self.prompt, session_dir=self.run_dir,
                slot="impl4", phase="8", kind="impl", name="impl4", launcher_command=["assign.sh"], reply=self.reply)
        self.assertEqual(code, worker.ExitCode.OK, errors.getvalue())
        self.assertEqual(len(self.agent.submitted), 1)
        self.agent.complete(opened.pane)
        self.assertIsInstance(self.receipt("impl4"), worker.DispatchComplete)
        self.assertEqual(set(self.fixture.world.process_reads), panes_before)
        self.assertIn(("agent", "list"), self.fixture.world.reads)
        self.assertIn("agent argv is unavailable", errors.getvalue())
        for row in closed:
            self.assertNotIn(f"anomaly: {row.name}:", errors.getvalue())
        opened = self.opened("impl4")
        self.fixture.world.unknown[opened.pane] = "selected pane evidence is unavailable"
        for label in ("impl4", "unit-impl4"):
            with self.subTest(label=label), redirect_stderr(io.StringIO()) as errors:
                self.fixture.world.locations[opened.pane]["label"] = label
                code = worker.assign(self.fixture.parent.id, "impl", self.prompt, session_dir=self.run_dir,
                    slot="impl4", phase="8", kind="impl", name="impl4", launcher_command=["assign.sh"], reply=self.reply)
            self.assertEqual(code, worker.ExitCode.UNCERTAIN)
            self.assertIn("selected pane evidence is unavailable", errors.getvalue())
            self.assertEqual(len(self.agent.submitted), 1)
        del self.fixture.world.unknown[opened.pane]
        code, output, errors_text = self.command("list", "--json")
        self.assertEqual(code, worker.ExitCode.OK, errors_text)
        listing = {str(row["name"]): row for row in cast(list[dict[str, object]], json.loads(output))}
        self.assertEqual(listing[opened.row.name]["state"], "OpenWorker")
        for row in closed:
            self.assertEqual(listing[row.name]["state"], "UncertainWorker")
        self.fixture.world.remove_leaf(opened.pane)
        del self.fixture.world.locations[opened.pane]
        del self.fixture.world.foreground[opened.pane]
        other = fixture_member("unit", name="other-unit", parent=self.fixture.parent.parent)
        foreign = fixture_member("worker", name="impl4", parent=other.id)
        self.fixture.world.add(foreign, "w2:p7", workspace="w2", tab="w2:t2")
        launches = len(self.fixture.world.launches)
        with redirect_stderr(io.StringIO()) as errors:
            code = worker.assign(self.fixture.parent.id, "impl", self.prompt, session_dir=self.run_dir,
                slot="impl4", phase="8", kind="impl", name="impl4", launcher_command=["assign.sh"], reply=self.reply)
        self.assertEqual(code, worker.ExitCode.OK, errors.getvalue())
        self.assertEqual(len(self.fixture.world.launches), launches + 1)
        self.assertEqual(self.agent.row("w2:p7"), foreign)

    def test_named_assign_resumes_with_its_reported_codex_thread(self) -> None:
        row = self.fixture.worker("impl", family="codex")
        self.fixture.world.add(row, "w1:p1", live=False)
        native_process = self.fixture.world.native_process
        published: list[str] = []

        def read_process(pid: int, kind: str, reported: str = "") -> herdr.NativeAgentProcess:
            observed = native_process(pid, kind, reported)
            if kind == "codex" and observed.current_identity not in published:
                published.append(observed.current_identity)
            return observed

        with (patch.object(herdr, "native_process", side_effect=read_process),
              redirect_stderr(io.StringIO()) as errors):
            code = worker.assign(self.fixture.parent.id, "impl", self.prompt, session_dir=self.run_dir,
                slot="impl", phase="8", kind="impl", name="impl", launcher_command=["assign.sh"], reply=self.reply)
        self.assertEqual(code, worker.ExitCode.OK, errors.getvalue())
        self.assertEqual(published, [str(row.current_session)])
        self.assertEqual(len(self.agent.submitted), 1)
        self.assertIsInstance(self.receipt("impl"), worker.DispatchComplete)
        self.assertEqual(staff.by_id(row.id), replace(row, role=staff.KnownWorkerRole("impl")))

    def test_successful_delivery_returns_before_a_later_process_read(self) -> None:
        opened = self.opened()
        def lose_process_evidence() -> None:
            self.fixture.world.unknown[opened.pane] = "dispatch process reader unavailable"
        self.agent.during_prompt = lose_process_evidence
        self.agent.finish_after = 1
        with redirect_stderr(io.StringIO()) as errors:
            code = self.dispatch()
        self.assertIs(code, worker.DispatchSubmission.SUBMITTED)
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(len(self.agent.submitted), 1)
        self.assertEqual(self.agent.waited, [])

    def test_unknown_assignment_evidence_stays_uncertain_without_creating_workers(self) -> None:
        opened = self.opened()
        _ = progress_history.begin_dispatch(self.run_dir, slot="impl", phase="8", role="impl",
            worker_name=opened.row.name, worker_id=opened.row.id, launcher_command=["prior-assign.sh"], prompt=self.prompt, kind="impl", lens="")
        before = self.fixture.staff_state(), len(self.fixture.world.launches)
        for evidence in ("process", "structure"):
            with self.subTest(evidence=evidence):
                if evidence == "process":
                    self.fixture.world.unknown[opened.pane] = "assignment process reader unavailable"
                else:
                    self.fixture.world.unknown.clear()
                    self.fixture.world.locations[opened.pane]["terminal_id"] = ""
                with redirect_stderr(io.StringIO()) as errors:
                    code = worker.assign(self.fixture.parent.id, "impl", self.prompt, session_dir=self.run_dir,
                        slot="impl", phase="8", kind="impl", launcher_command=["assign.sh"])
                self.assertEqual(code, worker.ExitCode.UNCERTAIN, errors.getvalue())
                self.assertEqual(len(errors.getvalue().splitlines()), 1, errors.getvalue())
                self.assertEqual((self.fixture.staff_state(), len(self.fixture.world.launches)), before)
                self.assertEqual(self.agent.submitted, [])

    def test_restore_continues_after_one_worker_layout_failure(self) -> None:
        for name in ("impl", "impl2"):
            opened = self.opened(name)
            _ = progress_history.begin_dispatch(self.run_dir, slot=name, phase="8", role="impl",
                worker_name=name, worker_id=opened.row.id, launcher_command=["assign.sh", name], prompt=self.prompt, kind="impl", lens="")
            _ = self.fixture.world.stop(opened.row, self.fixture.world)
        states = worker_panes.observe(self.fixture.parent.id, staff.snapshot())
        first, second = states
        request = self.fixture.world.socket_request
        failed = False
        def fail_one_layout(method: str, **params: object) -> dict[str, object]:
            nonlocal failed
            if method == "layout.export" and not failed:
                failed = True
                raise herdr_types.HerdrFailure("restore layout unavailable")
            return request(method, **params)
        with patch.object(self.fixture.world, "socket_request", side_effect=fail_one_layout), patch.object(worker, "_resume_launcher", return_value=worker.ExitCode.OK):
            with redirect_stderr(io.StringIO()) as errors:
                code = worker.restore(self.fixture.parent.id)
        self.assertEqual(code, worker.ExitCode.UNCERTAIN)
        self.assertEqual(errors.getvalue().splitlines(), [f"anomaly: {first.row.name}: restore layout unavailable"])
        resumed = {state.row.id for state in worker_panes.observe(self.fixture.parent.id, staff.snapshot())
                   if isinstance(state, worker_panes.OpenWorker)}
        self.assertIn(second.row.id, resumed)
        before = len(self.fixture.world.launches)
        with patch.object(worker, "_resume_launcher", return_value=worker.ExitCode.OK):
            self.assertEqual(self.command("restore"), (0, f"{self.fixture.parent.name}: workers unchanged\n", ""))
        self.assertEqual(len(self.fixture.world.launches), before)

    def test_herdr_dispatch_records_the_worker_staff_id(self) -> None:
        for foreign_tab in (False, True):
            with self.subTest(foreign_tab=foreign_tab):
                opened = self.opened("foreign-tab" if foreign_tab else "impl")
                def relocate(pane: str) -> None:
                    if foreign_tab:
                        self.fixture.world.remove_leaf(pane)
                        self.fixture.world.layouts["w1:t5"] = {"type": "pane", "pane_id": pane}
                        self.fixture.world.locations[pane]["tab_id"] = "w1:t5"
                self.agent.after_complete = relocate
                with redirect_stderr(io.StringIO()) as errors:
                    self.assertEqual(self.dispatch(opened.row.name), worker.ExitCode.OK)
                    self.assertEqual(worker_panes.worker_pass(opened.row, self.run_dir)["status"], "active")
                    self.assertEqual(self.finish(opened.row.name), turn_end.TurnOutcome.DONE)
                state = cast(dict[str, dict[str, dict[str, object]]],
                             json.loads((self.run_dir / "progress_history_state.json").read_text()))
                self.assertEqual(state["pass"]["impl"]["worker_id"], opened.row.id)
                self.assertEqual(state["pass"]["impl"]["status"], "completed")
                self.assertEqual(staff.snapshot().by_id(opened.row.id), replace(opened.row, role=staff.KnownWorkerRole("impl")))
                self.assertIn(self.agent.final, self.reply.read_text())
                self.assertIsInstance(self.fixture.observation(opened.row), worker_panes.ClosedWorker)
                self.assertNotIn(opened.pane, self.fixture.world.locations)
                self.assertEqual(errors.getvalue(), "")

    def test_fast_dispatch_writes_reply_without_changing_the_prompt(self) -> None:
        before = self.prompt.read_bytes(), self.prompt.stat().st_mtime_ns
        self.assertEqual(self.dispatch(), worker.ExitCode.OK)
        self.assertEqual(len(self.agent.submitted), 1)
        self.assertEqual(self.finish(), turn_end.TurnOutcome.DONE)
        self.assertIn(self.agent.final, self.reply.read_text())
        self.assertIn("DONE ", self.reply.read_text())
        self.assertEqual((self.prompt.read_bytes(), self.prompt.stat().st_mtime_ns), before)
        self.assertNotIn("--until", self.agent.submitted[0][1])
        for summary in ("", str(self.run_dir / "durable-reply.md")):
            with self.subTest(durable_summary=summary):
                name = "reviewer-default" if not summary else "reviewer-summary"
                identities: list[str] = []
                for round_number in (1, 2):
                    prompt = self.prompt.with_name(f"{name}-{round_number}.md")
                    atomic_file.write_text_atomic(prompt, f"Review round {round_number}.")
                    destination = Path(summary).with_name(f"reply-{name}-{round_number}.md") if summary else prompt.with_name(prompt.stem + "_reply.txt")
                    args = assign.Arguments(target="durable-design-role", parent=str(self.fixture.parent.id),
                        to=name, policy="astra-reviewer", prompt=str(prompt), summary=str(destination) if summary else "",
                        session_dir=str(self.run_dir), phase="8")
                    router = PaneCommandRouter()
                    with (patch.object(subprocess, "run", side_effect=router.run),
                          patch.object(sys, "argv", ["assign.py", "--prompt", str(prompt)])):
                        self.assertIs(assign.execute_durable(args), worker.DispatchSubmission.SUBMITTED)
                    pane = self.agent.submitted[-1][0]
                    row = self.agent.row(pane)
                    identities.append(row.current_session)
                    self.assertEqual(router.recorder_commands, [])
                    self.assertFalse(destination.exists())
                    self.assertEqual(worker_panes.worker_pass(row, self.run_dir)["status"], "active")
                    self.assertEqual(self.finish(name), turn_end.TurnOutcome.DONE)
                    self.assertIn(self.agent.final, destination.read_text())
                    self.assertTrue(progress_history.reply_completed(self.run_dir, destination))
                    self.assertEqual(worker_panes.worker_pass(row, self.run_dir)["status"], "completed")
                    self.assertEqual(staff.snapshot().by_id(row.id), row)
                    self.assertNotIn(pane, self.fixture.world.locations)
                self.assertEqual(identities[0], identities[1])

    def test_closed_conversation_compacts_before_the_next_dispatch(self) -> None:
        for index, (tokens, failure) in enumerate(((43000, ""), (43001, ""), (43001, "compact unavailable"))):
            with self.subTest(tokens=tokens, failure=failure):
                self.agent.compact_error = failure
                name = f"impl-compact-{index}"
                self.assertIs(self.dispatch(name), worker.DispatchSubmission.SUBMITTED)
                pane = self.agent.submitted[-1][0]
                row = self.agent.row(pane)
                backend = staff.backend(row)
                assert isinstance(backend, staff.CodexBackend)
                source = worker_admission.transcript_source(row, backend)
                with source.open("a") as stream:
                    _ = stream.write(json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {
                        "last_token_usage": {"input_tokens": tokens, "cached_input_tokens": 0, "output_tokens": 0}}}}) + "\n")
                self.assertEqual(self.finish(name), turn_end.TurnOutcome.DONE)
                self.assertNotIn(pane, self.fixture.world.locations)
                following = self.prompt.with_name(f"next-{index}.md")
                atomic_file.write_text_atomic(following, "Review the next change.")
                before = len(self.agent.submitted)
                with redirect_stderr(io.StringIO()) as errors:
                    result = worker.dispatch(self.fixture.parent.id, staff.WorkerName.parse(name), "impl", following,
                        session_dir=self.run_dir, slot="impl", phase="8", kind="impl", launcher_command=["assign.sh", str(following)],
                        reply=self.reply)
                if failure:
                    self.assertIs(result, worker.DispatchSubmission.SUBMITTED)
                    self.assertEqual(len(self.agent.submitted), before + 1)
                    self.assertIn(failure, errors.getvalue())
                    self.assertIn(f"{name}: compaction failed:", errors.getvalue())
                    self.assertIn("prompt is sent without compaction", errors.getvalue())
                    self.assertEqual(len(errors.getvalue().splitlines()), 1)
                    self.assertNotIn("prompt delivered to pane", errors.getvalue())
                self.assertIs(result, worker.DispatchSubmission.SUBMITTED)
                reopened = self.agent.submitted[-1][0]
                self.assertNotEqual(reopened, pane)
                self.assertEqual(self.agent.row(reopened).current_session, row.current_session)
                expected = [(reopened, before)] if tokens > 43000 and not failure else []
                self.assertEqual(self.agent.compactions, expected)
                self.assertEqual(self.finish(name), turn_end.TurnOutcome.DONE)
                self.agent.compactions.clear()

    def test_long_dispatch_waits_without_submitting_again(self) -> None:
        self.agent.finish_after = 3
        self.assertIs(self.dispatch(), worker.DispatchSubmission.SUBMITTED)
        pane = self.agent.submitted[0][0]
        active = progress_history.active_dispatch(self.agent.row(pane).id, self.run_dir)
        self.assertIsInstance(active, progress_history.ActiveDispatch)
        self.assertIs(self.dispatch(), worker.DispatchSubmission.SUBMITTED)
        self.assertEqual(len(self.agent.submitted), 1)
        self.assertEqual(self.agent.waited, [])
        self.assertEqual(progress_history.active_dispatch(self.agent.row(pane).id, self.run_dir), active)

    def test_dispatch_rerun_after_prompt_uses_the_same_pass(self) -> None:
        self.agent.interrupt = "prompt"
        with self.assertRaisesRegex(OSError, "after prompt"):
            _ = self.dispatch()
        state = (self.run_dir / "progress_history_state.json").read_bytes()
        pane = self.agent.submitted[0][0]
        self.fixture.world.locations[pane]["agent_status"] = "working"
        self.agent.finish_after = 1
        self.assertEqual(self.dispatch(), worker.ExitCode.OK)
        self.assertEqual(len(self.agent.submitted), 1)
        self.assertEqual((self.run_dir / "progress_history_state.json").read_bytes(), state)
        self.agent.complete(pane)
        self.assertEqual(self.finish(), turn_end.TurnOutcome.DONE)
        self.assert_pass_completed(state)

    def test_dispatch_rerun_after_done_restores_the_reply_once(self) -> None:
        self.assertIs(self.dispatch(), worker.DispatchSubmission.SUBMITTED)
        write = atomic_file.write_text_atomic
        def interrupted(path: Path, text: str) -> None:
            if path == self.reply:
                raise OSError("interrupted before reply publication")
            write(path, text)
        with patch.object(atomic_file, "write_text_atomic", side_effect=interrupted):
            with self.assertRaisesRegex(OSError, "before reply"):
                _ = self.finish()
        self.assertEqual(self.finish(), turn_end.TurnOutcome.DONE)
        before = self.reply.read_bytes(), self.reply.stat().st_mtime_ns, self.fixture.staff_state()
        self.assertEqual(self.finish(), turn_end.TurnOutcome.IGNORED)
        self.assertEqual((self.reply.read_bytes(), self.reply.stat().st_mtime_ns, self.fixture.staff_state()), before)
        self.assertEqual(len(self.agent.submitted), 1)

    def test_completed_dispatch_rerun_repeats_its_notice_without_typing(self) -> None:
        self.assertIs(self.dispatch(), worker.DispatchSubmission.SUBMITTED)
        pane = self.agent.submitted[-1][0]
        self.assertEqual(self.finish(), turn_end.TurnOutcome.DONE)
        notice, = self.agent.notices
        path = self.run_dir / "progress_history_state.json"
        before = path.read_bytes(), path.stat().st_mtime_ns, self.fixture.staff_state()
        reply = self.reply.read_bytes(), self.reply.stat().st_mtime_ns
        for repeated in (1, 2):
            with self.subTest(repeated=repeated):
                self.assertIs(self.dispatch(), worker.DispatchSubmission.SUBMITTED)
                self.assertEqual(self.agent.notices, [notice] * (repeated + 1))
                self.assertEqual(len(self.agent.submitted), 1)
                self.assertEqual(len(self.fixture.world.launches), 1)
                self.assertNotIn(pane, self.fixture.world.locations)
                self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns, self.fixture.staff_state()), before)
                self.assertEqual((self.reply.read_bytes(), self.reply.stat().st_mtime_ns), reply)
        self.assertEqual(self.finish(), turn_end.TurnOutcome.IGNORED)
        self.assertEqual(len(self.agent.notices), 3)

    def test_dispatch_rerun_before_prompt_records_one_submission(self) -> None:
        target = herdr.check_target
        def interrupted(identity: str | herdr.AgentProcessIdentity, **kwargs: object) -> bool:
            del identity, kwargs
            raise OSError("interrupted before prompt")
        with patch.object(herdr, "check_target", side_effect=interrupted):
            with self.assertRaisesRegex(OSError, "before prompt"):
                _ = self.dispatch()
        self.assertEqual(self.agent.submitted, [])
        self.assertEqual(self.dispatch(), worker.ExitCode.OK)
        self.assertEqual(len(self.agent.submitted), 1)
        self.assertIs(herdr.check_target, target)

    def test_an_earlier_done_line_does_not_complete_another_dispatch(self) -> None:
        opened = self.opened()
        self.agent.append(opened.pane, "assistant", "An old result.\nDONE another-pass")
        self.assertEqual(self.dispatch(), worker.ExitCode.OK)
        self.assertEqual(len(self.agent.submitted), 1)
        self.assertEqual(self.finish(), turn_end.TurnOutcome.DONE)
        self.assertNotIn("old result", self.reply.read_text())

    def test_settled_without_done_preserves_the_existing_report(self) -> None:
        atomic_file.write_text_atomic(self.reply, "Full report from the prior turn.\n")
        self.agent.without_done = True
        self.assertIs(self.dispatch(), worker.DispatchSubmission.SUBMITTED)
        self.assertEqual(self.finish(), turn_end.TurnOutcome.NO_DONE)
        self.assertEqual(self.reply.read_text(), "Full report from the prior turn.\n")
        self.assertIn("no DONE", self.agent.notices[-1])

    def assert_detached_dispatch(self, initial_done: bool, *, closed: bool = False) -> None:
        opened = self.opened()
        self.agent.without_done = not initial_done
        path, record = self.detached_record(opened.row, "verification")
        with subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()",
                               "detached_runs.py", "supervise", str(path)], stdin=subprocess.PIPE, text=True) as supervisor:
            record.update(status="running", pid=supervisor.pid)
            self.agent.during_prompt = lambda: atomic_file.write_text_atomic(path, json.dumps(record))
            self.assertIs(self.dispatch(), worker.DispatchSubmission.SUBMITTED)
            active = progress_history.active_dispatch(opened.row.id, self.run_dir)
            self.assertIsInstance(self.receipt(), worker.DispatchWaitingForRuns)
            self.assertEqual(self.finish(), turn_end.TurnOutcome.WAITING_ON_RUNS)
            self.assertEqual(progress_history.active_dispatch(opened.row.id, self.run_dir), active)
            if closed:
                _ = self.fixture.world.stop(opened.row, self.fixture.world)
            record.update(status="finished", delivery=f"sent: worker {opened.row.name} at {opened.pane}")
            atomic_file.write_text_atomic(path, json.dumps(record))
            if not closed:
                self.agent.append(opened.pane, "user", "Detached run: verification\nRun finished: private verification")
                self.assertEqual(self.finish(kind=turn_end.EventKind.RUN_FINISHED), turn_end.TurnOutcome.WAITING_ON_REPLY)
                self.agent.without_done = False
                self.agent.complete(opened.pane)
                self.assertEqual(self.finish(), turn_end.TurnOutcome.DONE)
            else:
                self.assertEqual(self.finish(kind=turn_end.EventKind.RUN_FINISHED), turn_end.TurnOutcome.DONE)
            assert supervisor.stdin is not None
            supervisor.stdin.close()
            _ = supervisor.wait(timeout=LauncherHangGuard("private supervisor exit").remaining())
        self.assertIn("DONE ", self.reply.read_text())
        self.assertEqual(len(self.agent.submitted), 1)
        self.assertEqual(self.agent.waited, [])

    def test_done_with_a_live_run_waits_for_delivery_and_a_later_done_reply(self) -> None:
        self.assert_detached_dispatch(True)

    def test_a_reply_without_done_with_a_live_run_does_not_return_no_done(self) -> None:
        self.assert_detached_dispatch(False)

    def test_a_closed_worker_after_a_run_result_ends_the_launcher_with_a_live_run(self) -> None:
        self.assert_detached_dispatch(True, closed=True)

    def test_done_after_a_delivered_result_without_a_reply_returns_no_done_after_one_quiet_wait(self) -> None:
        atomic_file.write_text_atomic(self.reply, "The previous full report.\n")
        self.assertIs(self.dispatch(), worker.DispatchSubmission.SUBMITTED)
        pane, line = self.agent.submitted[-1]
        self.agent.append(pane, "user", "Detached run: private-check\nRun finished: private verification")
        self.assertEqual(self.finish(), turn_end.TurnOutcome.WAITING_ON_REPLY)
        self.assertEqual(self.reply.read_text(), "The previous full report.\n")
        self.assertIs(self.dispatch(), worker.DispatchSubmission.SUBMITTED)
        self.assertEqual(self.agent.submitted, [(pane, line)])
        self.assertEqual(self.agent.waited, [])
        self.assertEqual(self.reply.read_text(), "The previous full report.\n")
        self.assertIsInstance(progress_history.active_dispatch(self.agent.row(pane).id, self.run_dir), progress_history.ActiveDispatch)

    def test_a_delivered_result_can_start_work_after_a_done_state_read(self) -> None:
        self.assertIs(self.dispatch(), worker.DispatchSubmission.SUBMITTED)
        pane, line = self.agent.submitted[-1]
        self.agent.append(pane, "user", "Detached run: private-check\nRun finished: private verification")
        self.fixture.world.locations[pane]["agent_status"] = "working"
        self.assertEqual(self.finish(kind=turn_end.EventKind.RUN_FINISHED), turn_end.TurnOutcome.WAITING_ON_REPLY)
        self.agent.complete(pane)
        self.assertEqual(self.finish(), turn_end.TurnOutcome.DONE)
        self.assertEqual(self.agent.submitted, [(pane, line)])
        self.assertIn("DONE ", self.reply.read_text())

    def test_a_file_change_starts_another_wait_before_no_done(self) -> None:
        atomic_file.write_text_atomic(self.reply, "The previous full report.\n")
        previous = self.reply.read_text()
        self.assertIs(self.dispatch(), worker.DispatchSubmission.SUBMITTED)
        pane, line = self.agent.submitted[-1]
        for run_id in ("first-check", "second-check"):
            self.agent.append(pane, "user", f"Detached run: {run_id}\nRun finished: private verification")
            self.assertEqual(self.finish(kind=turn_end.EventKind.RUN_FINISHED), turn_end.TurnOutcome.WAITING_ON_REPLY)
        self.assertEqual(self.agent.submitted, [(pane, line)])
        self.assertEqual(self.agent.waited, [])
        self.assertEqual(self.reply.read_text(), previous)
        self.assertNotIn("no DONE", "\n".join(self.agent.notices))

    def test_done_with_a_result_absent_from_the_transcript_completes_after_its_run_finishes(self) -> None:
        self.assertIs(self.dispatch(), worker.DispatchSubmission.SUBMITTED)
        pane, line = self.agent.submitted[-1]
        row = self.agent.row(pane)
        path, record = self.detached_record(row, "private-check")
        record["delivery"] = f"sent: worker {row.name} at {pane}"
        atomic_file.write_text_atomic(path, json.dumps(record))
        receipt = self.receipt()
        self.assertIsInstance(receipt, worker.DispatchComplete)
        assert isinstance(receipt, worker.DispatchComplete)
        self.assertEqual(receipt.anomalies, ("run private-check finished, but its result is not in the transcript",))
        self.assertEqual(self.finish(kind=turn_end.EventKind.RUN_FINISHED), turn_end.TurnOutcome.DONE)
        self.assertEqual(self.agent.notices[-1].count(receipt.anomalies[0]), 1)
        self.assertEqual(self.agent.submitted, [(pane, line)])
        self.assertIn("DONE ", self.reply.read_text())

    def test_a_reply_without_done_with_a_finished_run_absent_from_the_transcript_returns_no_done(self) -> None:
        atomic_file.write_text_atomic(self.reply, "The previous report.\n")
        self.agent.without_done = True
        self.assertIs(self.dispatch(), worker.DispatchSubmission.SUBMITTED)
        pane, line = self.agent.submitted[-1]
        row = self.agent.row(pane)
        path, record = self.detached_record(row, "private-check")
        record["delivery"] = f"sent: worker {row.name} at {pane}"
        atomic_file.write_text_atomic(path, json.dumps(record))
        self.assertEqual(self.finish(kind=turn_end.EventKind.RUN_FINISHED), turn_end.TurnOutcome.WAITING_ON_REPLY)
        self.assertEqual(self.reply.read_text(), "The previous report.\n")
        self.assertEqual(self.agent.submitted, [(pane, line)])
        self.assertEqual(self.agent.waited, [])

    def test_a_reply_after_the_state_read_completes_the_dispatch(self) -> None:
        self.assertIs(self.dispatch(), worker.DispatchSubmission.SUBMITTED)
        pane, line = self.agent.submitted[-1]
        dispatch_id = line.split("DONE ")[-1].split()[0].removesuffix(".")
        self.agent.append(pane, "user", "Detached run: private-check\nRun finished: private verification")
        final = "The result is verified.\nDONE " + dispatch_id
        observe = worker_panes.observe_worker
        def observe_then_reply(parent: staff.StaffId, selected: staff.StaffRow,
                               record: staff.StaffSnapshot) -> worker_panes.WorkerPane | worker_panes.WorkerNotObserved:
            observed = observe(parent, selected, record)
            self.agent.append(pane, "assistant", final)
            return observed
        row = self.agent.row(pane)
        with patch.object(worker_panes, "observe_worker", side_effect=observe_then_reply):
            event = turn_end.WorkerEvent(str(row.current_session), reply=final)
            self.assertEqual(turn_end.handle(event), turn_end.TurnOutcome.DONE)
        self.assertEqual(self.reply.read_text().strip(), final)
        self.assertEqual(self.agent.submitted, [(pane, line)])
        self.assertEqual(self.agent.waited, [])

    def detached_record(self, row: staff.StaffRow, run_id: str) -> tuple[Path, detached_runs.RunRecord]:
        directory = self.run_dir / worker_lifecycle.DETACHED_RUNS_DIR
        directory.mkdir(exist_ok=True)
        record: detached_runs.RunRecord = {"run_id": run_id, "status": "finished", "worker": row.name,
            "staff_id": row.id, "session_dir": str(self.run_dir), "label": "private verification", "command": [],
            "log": str(directory / f"{run_id}.log"), "started_at": time.time(), "pid": 0,
            "director_dir": str(self.run_dir), "done_message_file": ""}
        return directory / f"{run_id}.json", record

    def test_stage_two_accepts_a_run_result_delivered_during_stage_one(self) -> None:
        self.unfinished_turn("review")
        pane, line = self.agent.submitted[-1]
        row = self.agent.row(pane)
        dispatch_id = line.split("DONE ")[-1].split()[0].removesuffix(".")
        path, record = self.detached_record(row, "stage-one-check")
        record["delivery"] = f"sent: worker {row.name} at {pane}"
        atomic_file.write_text_atomic(path, json.dumps(record))
        self.agent.append(pane, "user", "Detached run: stage-one-check\nRun finished: private verification")
        second_id = dispatch_id + "-2"
        second_line = "Stage 2: " + line.replace(dispatch_id, second_id)
        self.agent.append(pane, "user", second_line)
        final = "The second review is complete.\nDONE " + second_id
        self.agent.append(pane, "assistant", final)
        self.assertEqual(worker.dispatch_receipt(row, second_line, second_id, run=self.run_dir), worker.DispatchComplete(final))

    def test_only_a_live_run_or_a_result_sent_to_this_worker_can_hold_its_receipt(self) -> None:
        self.assertEqual(self.dispatch(), worker.ExitCode.OK)
        pane, line = self.agent.submitted[-1]
        row = self.agent.row(pane)
        dispatch_id = line.split("DONE ")[-1].split()[0].removesuffix(".")
        path, record = self.detached_record(row, "private-check")
        for status, delivery, pending in (
            ("finished", "nothing delivered: exit 1 with a done message", False),
            ("finished", "finished: worker retired", False),
            ("finished", "nothing delivered: the supervisor is gone and a newer run answers", False),
            ("finished", "sent to director private-director", False),
            ("finished", "sent: holder received the result", False),
            ("running", "", False),
            ("finished", f"sent: worker {row.name} at {pane}", True),
            ("finished", "already delivered: private-check", True),
        ):
            with self.subTest(status=status, delivery=delivery):
                record["status"] = cast(Literal["running", "finished"], status)
                record["delivery"] = delivery
                atomic_file.write_text_atomic(path, json.dumps(record))
                receipt = worker.dispatch_receipt(row, line, dispatch_id, run=self.run_dir)
                self.assertIsInstance(receipt, worker.DispatchComplete)
                assert isinstance(receipt, worker.DispatchComplete)
                self.assertEqual(bool(receipt.anomalies), pending)

    def test_failed_delivery_preserves_its_receipt_and_never_retypes(self) -> None:
        self.agent.error = "agent_prompt_stalled"
        with redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(self.dispatch(), worker.ExitCode.UNCERTAIN)
        pane = self.agent.submitted[-1][0]
        self.assertIn("agent_prompt_stalled", errors.getvalue())
        self.assertTrue(all(f"prompt delivered to pane {pane}" in line for line in errors.getvalue().splitlines()))
        self.assertEqual(len(self.agent.submitted), 1)
        self.agent.error = ""
        self.agent.finish_after = 1
        self.assertEqual(self.dispatch(), worker.ExitCode.OK)
        self.assertEqual(len(self.agent.submitted), 1)

        for exited in (False, True):
            with self.subTest(exited=exited):
                case = WorkerCommandTests("runTest")
                case.setUp()
                try:
                    config = case.fixture.root / "conf/direct.conf"
                    atomic_file.write_text_atomic(config, config.read_text().replace("WORKER_SETTLE_SECONDS=30", "WORKER_SETTLE_SECONDS=1"))
                    opened = case.opened()
                    def queued_without_delivery(arguments: list[str], **options: object) -> subprocess.CompletedProcess[str]:
                        if arguments[0] == "/nonexistent/herdr" and arguments[4:6] == ["prompt", opened.pane]:
                            if exited:
                                pid, _, _ = case.fixture.world.foreground.pop(opened.pane)
                                del case.fixture.world.processes[pid]
                                case.fixture.world.locations[opened.pane]["agent_status"] = "exited"
                            return subprocess.CompletedProcess(arguments, 0, "{}", "")
                        return case.agent.run(arguments, **options)
                    with (patch.object(subprocess, "run", side_effect=queued_without_delivery),
                          redirect_stderr(io.StringIO()) as errors):
                        code = worker.assign(case.fixture.parent.id, "impl", case.prompt, session_dir=case.run_dir,
                            slot="impl", phase="8", kind="impl", name="impl", launcher_command=["assign.sh"], reply=case.reply)
                    case.assertEqual(code, worker.ExitCode.UNCERTAIN)
                    case.assertEqual(case.agent.submitted, [])
                    case.assertIsInstance(case.receipt(), worker.DispatchNotSubmitted)
                    case.assertEqual(errors.getvalue(),
                        "anomaly: impl: dispatch prompt is not in the transcript; submission is unproved\n")
                    if exited:
                        case.assertIsInstance(worker_panes.observe_worker(case.fixture.parent.id, opened.row, staff.snapshot()),
                                              worker_panes.StoppedWorker)
                        case.fixture.world.locations[opened.pane]["agent_status"] = "idle"
                    case.assertIs(case.dispatch(), worker.DispatchSubmission.SUBMITTED)
                    case.assertEqual(len(case.agent.submitted), 1)
                    case.assertEqual(case.agent.row(case.agent.submitted[-1][0]).current_session, opened.row.current_session)
                finally:
                    case.doCleanups()

        with self.subTest(delivery="delayed transcript"):
            case = WorkerCommandTests("runTest")
            case.setUp()
            opened = case.opened()
            case.agent.defer_prompt_transcript = True
            config = case.fixture.root / "conf/direct.conf"
            atomic_file.write_text_atomic(config, config.read_text().replace("WORKER_SETTLE_SECONDS=30", "WORKER_SETTLE_SECONDS=1"))
            def record_prompt() -> None:
                pane, line = case.agent.submitted[-1]
                case.agent.append(pane, "user", line)
            delivery = Timer(0.1, record_prompt)
            case.agent.during_prompt = delivery.start
            try:
                with redirect_stderr(io.StringIO()) as errors:
                    case.assertIs(case.dispatch(), worker.DispatchSubmission.SUBMITTED)
                case.assertEqual(errors.getvalue(), "")
                case.assertEqual(len(case.agent.submitted), 1)
                case.assertEqual(case.agent.submitted[0][0], opened.pane)
                case.assertIsInstance(case.receipt(), worker.DispatchSubmitted)
            finally:
                delivery.join()
                case.doCleanups()

    def test_blocked_dispatch_returns_blocked(self) -> None:
        opened = self.opened()
        question = "Which output format must I use?\nJSON or Markdown?\n"
        cases = (("before", question), ("after", question),
                 ("unreadable", herdr_types.HerdrFailure("pane read failed\nterminal unavailable")))
        for stage, visible in cases:
            with self.subTest(stage=stage):
                self.agent.questions[opened.pane] = visible
                self.agent.error = "" if stage == "before" else "agent_blocked"
                self.fixture.world.locations[opened.pane]["agent_status"] = "idle" if stage == "after" else "blocked"
                with redirect_stderr(io.StringIO()) as errors:
                    self.assertEqual(self.dispatch(), worker.ExitCode.BLOCKED)
                expected = visible if isinstance(visible, str) else "anomaly: impl: pane read failed terminal unavailable\n"
                self.assertEqual(errors.getvalue(), f"question: impl pane {opened.pane}\n" + expected)
                self.assertEqual(len(self.agent.submitted), 0 if stage == "before" else 1)
                self.assertIn(opened.pane, self.fixture.world.locations)
                self.assertEqual(staff.snapshot().by_id(opened.row.id), replace(opened.row, role=staff.KnownWorkerRole("impl")))
        self.assertIn(("pane", "read", opened.pane, "--source", "recent", "--lines", "40"), self.fixture.world.commands)

    def test_answer_continues_a_blocked_dispatch_and_refuses_another_parents_worker(self) -> None:
        opened = self.opened()
        self.agent.questions[opened.pane] = "Which output format must I use?"
        self.agent.error = "agent_blocked"
        with redirect_stderr(io.StringIO()):
            self.assertEqual(self.dispatch(), worker.ExitCode.BLOCKED)
        active = progress_history.active_dispatch(opened.row.id, self.run_dir)
        self.assertIsInstance(active, progress_history.ActiveDispatch)
        answer_text = "Use JSON."
        for index, reported in enumerate((True, False), start=1):
            with self.subTest(reported=reported):
                self.agent.questions[opened.pane] = "Which output format must I use?"
                self.fixture.world.locations[opened.pane]["agent_status"] = "blocked"
                if not reported:
                    _ = self.fixture.world.locations[opened.pane].pop("agent_session", None)
                    pid = self.fixture.world.foreground[opened.pane][0]
                    process = self.fixture.world.processes[pid]
                    self.fixture.world.processes[pid] = replace(process, identity=herdr.UnreportedResume(opened.row.current_session))
                self.assertEqual(self.command("answer", "impl", "--text", answer_text), (0, "done\n", ""))
                self.assertEqual(len(self.agent.answers), index)
                self.assertEqual(self.agent.answers[-1][0], opened.pane)
                self.assertIn(answer_text, self.agent.answers[-1][1])
                self.assertEqual(self.fixture.world.locations[opened.pane]["agent_status"], "working")
        answer_commands = [command for command in self.fixture.world.commands
                           if command[:2] in (("pane", "send-keys"), ("pane", "send-text"))]
        self.assertEqual(answer_commands, [("pane", "send-keys", opened.pane, "shift+left"),
            ("pane", "send-text", opened.pane, answer_text), ("pane", "send-keys", opened.pane, "enter"),
            ("pane", "send-keys", opened.pane, "enter")] * 2)
        self.assertEqual(self.dispatch(), worker.ExitCode.OK)
        self.assertEqual(len(self.agent.submitted), 1)
        self.assertEqual(self.agent.waited, [])
        self.agent.complete(opened.pane)
        self.assertEqual(progress_history.active_dispatch(opened.row.id, self.run_dir), active)
        self.assertEqual(self.finish(), turn_end.TurnOutcome.DONE)
        self.assertIn(self.agent.final, self.reply.read_text())
        assert isinstance(active, progress_history.ActiveDispatch)
        self.assertIn("DONE " + active.instance_id, self.reply.read_text())
        self.assertIsInstance(progress_history.active_dispatch(opened.row.id, self.run_dir), progress_history.NoActiveDispatch)
        other = fixture_member("unit", name="other-unit", parent=self.fixture.parent.parent)
        foreign = fixture_member("worker", name="foreign", parent=other.id)
        self.fixture.world.add(foreign, "w2:p7", workspace="w2", tab="w2:t2")
        self.agent.questions["w2:p7"] = "Which output format must I use?"
        self.assertEqual(self.command("answer", foreign.name, "--text", "Use Markdown."),
                         (worker.ExitCode.REFUSED, "", "foreign: no worker staff row\n"))
        self.assertEqual(len(self.agent.answers), 2)

    def test_answer_uses_prompt_for_an_idle_worker(self) -> None:
        opened = self.opened()
        self.agent.questions[opened.pane] = "Which format?"
        self.fixture.world.locations[opened.pane]["agent_status"] = "idle"
        with patch.object(self.fixture.world, "answer_question", wraps=self.fixture.world.answer_question) as answer:
            self.assertEqual(self.command("answer", "impl", "--text", "Use JSON."), (0, "done\n", ""))
        answer.assert_not_called()
        self.assertEqual(len(self.agent.answers), 1)
        self.assertIn("Use JSON.", self.agent.answers[0][1])

    def test_answer_keeps_prompt_for_a_blocked_claude_worker(self) -> None:
        _ = write_registry(self.fixture.root / "conf/models.conf", {"assignments": {"direct": "claude"}})
        opened = self.opened()
        self.agent.questions[opened.pane] = "Which format?"
        self.fixture.world.locations[opened.pane]["agent_status"] = "blocked"
        with patch.object(self.fixture.world, "answer_question", wraps=self.fixture.world.answer_question) as answer:
            self.assertEqual(self.command("answer", "impl", "--text", "Use JSON."), (0, "done\n", ""))
        answer.assert_not_called()
        self.assertEqual(len(self.agent.answers), 1)
        self.assertIn("Use JSON.", self.agent.answers[0][1])

    def test_claude_without_a_transcript_receives_its_first_dispatch_prompt(self) -> None:
        _ = write_registry(self.fixture.root / "conf/models.conf", {"assignments": {"direct": "claude"}})
        opened = self.opened()
        backend = staff.backend(opened.row)
        self.assertIsInstance(backend, staff.ClaudeBackend)
        assert isinstance(backend, staff.ClaudeBackend)
        source = worker_admission.transcript_source(opened.row, backend)
        pid = self.fixture.world.foreground[opened.pane][0]
        with fake_unit_host.private_process_registry():
            with patch.dict(os.environ, {"FAKE_CLAUDE_PID": str(pid)}):
                record_fixture_claude_start(["--resume", opened.row.current_session])
            source.unlink()
            self.assertIsInstance(worker.dispatch_receipt(opened.row, "first prompt", "first-dispatch", run=self.run_dir),
                                  worker.DispatchNotSubmitted)
            self.assertFalse(source.exists())
            self.assertEqual(self.dispatch(), worker.ExitCode.OK)
            self.assertEqual(len(self.agent.submitted), 1)
            self.assertEqual(self.finish(), turn_end.TurnOutcome.DONE)
            self.assertIn(self.agent.final, self.reply.read_text())
            self.assertTrue(source.exists())

    def test_first_codex_dispatch_keeps_its_identity_during_the_warmup_hook(self) -> None:
        world = self.fixture.world
        wait = world.wait_turn_end
        result = world.result
        native_process = world.native_process
        hook_running = False

        def warmup_ended(pane: str, timeout_ms: int) -> herdr.ObservedAgentState | herdr.WaitTimedOut:
            nonlocal hook_running
            ended = wait(pane, timeout_ms)
            hook_running = True
            return ended

        def fresh_codex(pid: int, kind: str, reported: str = "") -> herdr.NativeAgentProcess:
            if hook_running and kind == "codex" and not reported:
                raise ValueError("Codex launch identity is unavailable")
            return native_process(pid, kind, reported)

        def with_hook(*arguments: str, deadline: float = math.inf) -> dict[str, object]:
            value = result(*arguments, deadline=deadline)
            if hook_running and arguments[:2] == ("pane", "process-info") and arguments[-1] != "w1:p0":
                info = cast(dict[str, object], value["process_info"])
                foreground = cast(list[object], info["foreground_processes"])
                info["foreground_processes"] = [{"pid": 998088, "argv": [
                    "python3", str(Path(turn_end.__file__).resolve())]}, *foreground]
            return value

        with patch.object(world, "wait_turn_end", side_effect=warmup_ended), \
                patch.object(world, "result", side_effect=with_hook), \
                patch.object(herdr, "native_process", side_effect=fresh_codex):
            self.assertIs(self.dispatch(), worker.DispatchSubmission.SUBMITTED)
        self.assertTrue(hook_running)
        self.assertEqual(len(self.agent.submitted), 1)
        self.assertEqual(len(world.launches), 1)
        self.assertEqual(len([entry for entry in world.commands if entry[:2] == ("agent", "wait")]), 1)

    def test_claude_delivery_returns_before_its_prompt_enters_the_transcript(self) -> None:
        _ = write_registry(self.fixture.root / "conf/models.conf", {"assignments": {"direct": "claude"}})
        self.agent.defer_prompt_transcript = True
        identities: list[staff.StaffId] = []
        threads: list[str] = []
        for turn in range(2):
            with self.subTest(turn=turn):
                self.prompt = self.run_dir / f"delayed-transcript-{turn}.md"
                atomic_file.write_text_atomic(self.prompt, "Complete the assigned work.\n")
                with patch.object(worker, "dispatch_receipt", wraps=worker.dispatch_receipt) as receipts:
                    self.assertIs(self.dispatch(), worker.DispatchSubmission.SUBMITTED)
                self.assertEqual(receipts.call_count, 1)
                pane, line = self.agent.submitted[-1]
                row = self.agent.row(pane)
                identities.append(row.id)
                threads.append(str(row.current_session))
                backend = staff.backend(row)
                assert isinstance(backend, staff.ClaudeBackend)
                source = worker_admission.transcript_source(row, backend)
                self.assertNotIn(line, source.read_text() if source.exists() else "")
                self.assertIsInstance(worker.dispatch_receipt(row, line, "pending", run=self.run_dir),
                                      worker.DispatchNotSubmitted)
                self.assertFalse(self.reply.exists())
                self.assertEqual(self.agent.waited, [])
                self.agent.append(pane, "user", line)
                self.agent.complete(pane)
                self.assertEqual(self.finish(), turn_end.TurnOutcome.DONE)
                self.assertIn(self.agent.final, self.reply.read_text())
                self.reply.unlink()
        self.assertEqual(identities[0], identities[1])
        self.assertEqual(threads[0], threads[1])
        self.assertEqual(len(self.fixture.world.launches), 2)
        self.assertEqual(self.fixture.world.requests[-1][1][:2], ["--resume", threads[0]])

    def test_codex_without_a_transcript_keeps_its_receipt_unknown(self) -> None:
        opened = self.opened()
        backend = staff.backend(opened.row)
        assert isinstance(backend, staff.CodexBackend)
        for source in (backend.codex_home / "sessions").rglob(f"*{opened.row.current_session}.jsonl"):
            source.unlink()
        self.assertIsInstance(worker.dispatch_receipt(opened.row, "first prompt", "first-dispatch", run=self.run_dir),
                              worker.DispatchEvidenceUnknown)
        self.assertEqual(self.agent.submitted, [])

    def test_working_worker_that_reaches_done_receives_one_prompt(self) -> None:
        for state in (herdr.AgentStatus.DONE, herdr.AgentStatus.BLOCKED):
            with self.subTest(state=state):
                name = "impl-" + state.value
                opened = self.opened(name)
                self.fixture.world.commands.clear()
                self.fixture.world.locations[opened.pane]["agent_status"] = "working"
                self.fixture.world.turn_end_status = state
                question = "Which output format must I use? JSON or Markdown?"
                if state == herdr.AgentStatus.BLOCKED:
                    self.agent.questions[opened.pane] = question
                before = len(self.agent.submitted)
                with redirect_stderr(io.StringIO()) as errors:
                    result = self.dispatch(name)
                waits = [command for command in self.fixture.world.commands if command[:2] == ("agent", "wait")]
                self.assertEqual(len(waits), 1)
                self.assertEqual(waits[0][waits[0].index("--timeout") + 1], "30000")
                if state == herdr.AgentStatus.BLOCKED:
                    self.assertEqual(result, worker.ExitCode.BLOCKED)
                    self.assertEqual(len(self.agent.submitted), before)
                    self.assertIn(f"question: {name} pane {opened.pane}", errors.getvalue())
                    self.assertIn(question, errors.getvalue())
                    self.assertNotIn("prompt delivered to pane", errors.getvalue())
                else:
                    self.assertIs(result, worker.DispatchSubmission.SUBMITTED)
                    self.assertEqual(len(self.agent.submitted), before + 1)
                    self.assertEqual(self.agent.submitted[-1][0], opened.pane)

    def test_working_worker_that_reaches_idle_receives_one_prompt(self) -> None:
        opened = self.opened()
        self.fixture.world.commands.clear()
        self.fixture.world.locations[opened.pane]["agent_status"] = "working"
        self.fixture.world.turn_end_status = herdr.AgentStatus.IDLE
        self.assertIs(self.dispatch(), worker.DispatchSubmission.SUBMITTED)
        waits = [command for command in self.fixture.world.commands if command[:2] == ("agent", "wait")]
        self.assertEqual(len(waits), 1)
        self.assertEqual(waits[0][waits[0].index("--timeout") + 1], "30000")
        self.assertEqual(len(self.agent.submitted), 1)
        self.assertEqual(self.agent.submitted[0][0], opened.pane)

    def test_a_prompt_delivered_during_settle_keeps_its_follower(self) -> None:
        opened = self.opened()
        self.agent.interrupt = "prompt"
        with self.assertRaisesRegex(OSError, "after prompt"):
            _ = self.dispatch()
        self.fixture.world.locations[opened.pane]["agent_status"] = "working"
        self.assertIs(self.dispatch(), worker.DispatchSubmission.SUBMITTED)
        self.assertEqual(self.agent.waited, [])
        self.assertEqual(len(self.agent.submitted), 1)
        self.assertIsInstance(self.receipt(), worker.DispatchSubmitted)

        with self.subTest(cause="prompt enters the transcript during the wait while the worker stays working"):
            name = "accepted-during-settle"
            opened = self.opened(name)
            self.agent.defer_prompt_transcript = True
            self.agent.interrupt = "prompt"
            with self.assertRaisesRegex(OSError, "after prompt"):
                _ = self.dispatch(name)
            submitted = list(self.agent.submitted)
            pane, line = submitted[-1]
            self.fixture.world.locations[pane]["agent_status"] = "working"
            self.fixture.world.first_turn_timeout = True
            wait = self.fixture.world.wait_turn_end

            def accept_during_wait(pane: str, timeout_ms: int) -> herdr.ObservedAgentState | herdr.WaitTimedOut:
                self.agent.append(pane, "user", line)
                return wait(pane, timeout_ms)

            with patch.object(self.fixture.world, "wait_turn_end", side_effect=accept_during_wait):
                self.assertIs(self.dispatch(name), worker.DispatchSubmission.SUBMITTED)
            self.assertEqual(self.fixture.world.locations[opened.pane]["agent_status"], "working")
            self.assertEqual(self.agent.submitted, submitted)

    def test_done_worker_can_take_a_fix_while_its_recorder_pass_is_active(self) -> None:
        for effort in ("high", "low"):
            with self.subTest(fix_effort=effort):
                _ = write_registry(self.fixture.root / "conf/models.conf", {
                    "direct.codex": {"fix": f"gpt-test:{effort}"},
                }, update=True)
                submitted = len(self.agent.submitted)
                self.assertEqual(self.dispatch(), worker.ExitCode.OK)
                row = self.agent.row(self.agent.submitted[-1][0])
                self.assertEqual(worker_panes.worker_pass(row, self.run_dir)["status"], "active")
                launches = len(self.fixture.world.launches)
                self.assertEqual(worker.assign(self.fixture.parent.id, "fix", self.prompt, session_dir=self.run_dir,
                    slot="impl", phase="8", kind="fix", name="impl", launcher_command=["assign.sh"], reply=self.reply,
                    fix_cause="Repair the reported dispatch failure"), worker.ExitCode.OK)
                self.assertEqual(worker_panes.worker_pass(row, self.run_dir)["kind"], "fix")
                self.assertEqual(len(self.agent.submitted), submitted + 2)
                self.assertEqual(len(self.fixture.world.launches), launches + (effort == "low"))
                self.assertEqual(self.finish(), turn_end.TurnOutcome.DONE)

    def test_restore_keeps_an_open_busy_worker_without_sending_a_launcher(self) -> None:
        opened = self.opened()
        self.fixture.dispatch(opened.row, "review")
        self.fixture.world.locations[opened.pane]["agent_status"] = "working"
        before = self.fixture.staff_state(), len(self.fixture.world.launches)
        self.assertEqual(self.command("restore"), (0, "unit: workers unchanged\n", ""))
        self.assertEqual((self.fixture.staff_state(), len(self.fixture.world.launches)), before)
        self.assertEqual(self.fixture.world.stops, [])
        self.assertEqual(self.agent.submitted, [])
        self.assertEqual(self.fixture.world.locations[opened.pane]["agent_status"], "working")

    def test_busy_worker_receives_no_prompt(self) -> None:
        opened = self.opened()
        self.fixture.world.locations[opened.pane]["agent_status"] = "working"
        self.fixture.world.first_turn_timeout = True
        self.assertEqual(self.dispatch(), worker.ExitCode.BUSY)
        self.assertEqual(self.agent.submitted, [])

        with self.subTest(cause="busy assignment preserves the submitted pass"):
            self.fixture.world.first_turn_timeout = False
            self.agent.finish_after = 100
            self.assertIs(self.dispatch("submitted-worker"), worker.DispatchSubmission.SUBMITTED)
            pane = self.agent.submitted[-1][0]
            row = self.agent.row(pane)
            before = (self.run_dir / "progress_history_state.json").read_bytes()
            submitted = list(self.agent.submitted)
            self.prompt = self.run_dir / "next-prompt.md"
            atomic_file.write_text_atomic(self.prompt, "Check the next change.\n")
            self.fixture.world.first_turn_timeout = True
            self.assertEqual(self.dispatch("submitted-worker"), worker.ExitCode.BUSY)
            self.assertEqual(self.agent.submitted, submitted)
            self.assertEqual((self.run_dir / "progress_history_state.json").read_bytes(), before)
            self.agent.complete(pane)
            self.assertEqual(self.finish("submitted-worker"), turn_end.TurnOutcome.DONE)
            self.assert_pass_completed(before)
            self.assertNotIn(pane, self.fixture.world.locations)
            self.assertEqual(staff.by_id(row.id), row)

    def test_dispatch_lock_blocks_a_competing_dispatch_and_role_change(self) -> None:
        opened = self.opened()
        results: list[object] = []
        def concurrent() -> None:
            results.append(self.dispatch())
            results.append(worker_panes.start_or_resume(self.fixture.parent.id, staff.WorkerName.parse("impl"), "review"))
        self.agent.during_prompt = concurrent
        self.assertEqual(self.dispatch(), worker.ExitCode.OK)
        self.assertEqual(results[0], worker.ExitCode.BUSY)
        self.assertIsInstance(results[1], worker_panes.StartRefused)
        self.assertEqual(len(self.agent.submitted), 1)
        self.assertIsNotNone(staff.by_id(opened.row.id))

    def test_transcript_failure_reports_whether_the_prompt_was_delivered(self) -> None:
        config = self.fixture.root / "conf/direct.conf"
        atomic_file.write_text_atomic(config, config.read_text().replace("WORKER_SETTLE_SECONDS=30", "WORKER_SETTLE_SECONDS=1"))
        for delivered in (False, True):
            with self.subTest(delivered=delivered):
                name = "impl2" if delivered else "impl"
                opened = self.opened(name)
                backend = staff.backend(opened.row)
                assert isinstance(backend, (staff.ClaudeBackend, staff.CodexBackend))
                path = worker_admission.transcript_source(opened.row, backend)

                def tear_transcript() -> None:
                    atomic_file.write_text_atomic(path, '{"torn":')

                if delivered:
                    self.agent.during_prompt = tear_transcript
                    self.agent.finish_after = 1
                else:
                    tear_transcript()
                with redirect_stderr(io.StringIO()) as errors:
                    result = self.dispatch(name)
                if delivered:
                    self.assertEqual(result, worker.ExitCode.UNCERTAIN)
                    self.assertEqual(errors.getvalue(),
                        f"anomaly: {name}: worker transcript has incomplete records; submission is unproved\n")
                else:
                    self.assertEqual(result, worker.ExitCode.UNCERTAIN)
                    self.assertEqual(errors.getvalue(),
                        f"anomaly: {name}: worker transcript has incomplete records; no prompt sent\n")
                if delivered:
                    self.assertEqual([submission[0] for submission in self.agent.submitted], [opened.pane])
                else:
                    self.assertEqual(self.agent.submitted, [])

    def test_older_incomplete_record_keeps_the_valid_completion_receipt(self) -> None:
        opened = self.opened()
        self.agent.interrupt = "prompt"
        with self.assertRaisesRegex(OSError, "interrupted after prompt"):
            _ = self.dispatch()
        backend = staff.backend(opened.row)
        assert isinstance(backend, (staff.ClaudeBackend, staff.CodexBackend))
        path = worker_admission.transcript_source(opened.row, backend)
        atomic_file.write_text_atomic(path, '{"torn":\n' + path.read_text())
        self.agent.complete(opened.pane)
        self.assertEqual(self.dispatch(), worker.ExitCode.OK)
        self.assertEqual(self.finish(), turn_end.TurnOutcome.DONE)
        self.assertIn(self.agent.final, self.reply.read_text())
        self.assertEqual(len(self.agent.submitted), 1)

    def test_stopped_or_closed_worker_without_done_preserves_the_report(self) -> None:
        for closed in (False, True):
            with self.subTest(closed=closed):
                name = "impl2" if closed else "impl"
                opened = self.opened(name)
                atomic_file.write_text_atomic(self.reply, "The previous full report.\n")
                self.agent.finish_after = 1
                def exit_after_prompt(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                    result = self.agent.run(args, **kwargs)
                    if args[0] == "/nonexistent/herdr" and args[4] == "prompt":
                        del self.fixture.world.foreground[opened.pane]
                        self.fixture.world.locations[opened.pane]["agent_status"] = "idle"
                        if closed:
                            self.fixture.world.remove_leaf(opened.pane)
                            del self.fixture.world.locations[opened.pane]
                    return result
                with patch.object(subprocess, "run", side_effect=exit_after_prompt):
                    code = self.dispatch(name)
                self.assertIs(code, worker.DispatchSubmission.SUBMITTED)
                self.assertEqual(self.reply.read_text(), "The previous full report.\n")
                self.assertEqual(self.agent.waited, [])
                self.assertEqual(len(self.agent.submitted), 2 if closed else 1)

    def test_unpublished_start_closes_its_pane_and_records_no_pass(self) -> None:
        self.fixture.world.outcome = herdr.PromptNotSubmitted("startup hook failed")
        self.assertEqual(self.dispatch(), worker.ExitCode.UNCERTAIN)
        self.assertEqual(self.agent.submitted, [])
        self.assertEqual(len(self.fixture.world.launches), 1)
        self.assertEqual(json.loads((self.run_dir / "progress_history_state.json").read_text())["pass"], {})

    def test_failed_dispatch_preparation_ends_only_a_new_pane(self) -> None:
        failures = ("recorder", "dispatch id", "delivery", "receipt", "wait", "observation", "busy", "compact", "target", "incomplete")
        for failure in failures:
            for existing in (False, True, "closed"):
                with self.subTest(failure=failure, existing=existing):
                    case = WorkerCommandTests("runTest")
                    case.setUp()
                    try:
                        if existing:
                            opened = case.opened()
                            if existing == "closed":
                                case.assertTrue(case.fixture.world.close_terminal_pane(opened.pane, opened.terminal))
                        before = staff.snapshot().rows, set(case.fixture.world.locations)
                        closed = len(case.fixture.world.closed)
                        with ExitStack() as patches, redirect_stderr(io.StringIO()) as errors:
                            if failure == "incomplete":
                                start = cast(Callable[..., worker_panes.WorkerStart], worker_panes.start_or_resume)
                                def incomplete(*arguments: object, **options: object) -> worker_panes.WorkerStart:
                                    result = start(*arguments, **options)
                                    assert isinstance(result, worker_panes.OpenWorker)
                                    return worker_panes.IncompleteStart(result.row, result.pane, "TEST startup failed", result.created_pane)
                                _ = patches.enter_context(patch.object(worker_panes, "start_or_resume", side_effect=incomplete))
                            elif failure in ("target", "compact"):
                                if failure == "compact":
                                    _ = patches.enter_context(patch.object(worker, "_compact_before_dispatch", side_effect=OSError("TEST compact failed")))
                                check = cast(Callable[..., bool], herdr.check_target)
                                def refuse(*arguments: object, **options: object) -> bool:
                                    return False if options.get("action") == "keys" and options.get("text") else check(*arguments, **options)
                                _ = patches.enter_context(patch.object(herdr, "check_target", side_effect=refuse))
                            elif failure == "recorder":
                                _ = patches.enter_context(patch.object(progress_history, "begin_dispatch", side_effect=ValueError("TEST recorder failed")))
                            elif failure == "delivery":
                                _ = patches.enter_context(patch.object(progress_history, "save_dispatch_delivery", side_effect=OSError("TEST delivery failed")))
                            elif failure == "receipt":
                                _ = patches.enter_context(patch.object(worker, "dispatch_receipt", return_value=worker.DispatchEvidenceUnknown("TEST receipt failed")))
                            elif failure in ("wait", "observation"):
                                start = cast(Callable[..., worker_panes.WorkerStart], worker_panes.start_or_resume)
                                def working(*arguments: object, **options: object) -> worker_panes.WorkerStart:
                                    result = start(*arguments, **options)
                                    if failure == "wait":
                                        _ = patches.enter_context(patch.object(case.fixture.world, "wait_turn_end", side_effect=OSError("TEST wait failed")))
                                    return replace(result, status=herdr.AgentStatus.WORKING) if isinstance(result, worker_panes.OpenWorker) else result
                                _ = patches.enter_context(patch.object(worker_panes, "start_or_resume", side_effect=working))
                                if failure == "observation":
                                    _ = patches.enter_context(patch.object(worker_panes, "observe_worker", return_value=worker_panes.ClosedWorker(case.fixture.parent)))
                            elif failure == "busy":
                                start = cast(Callable[..., worker_panes.WorkerStart], worker_panes.start_or_resume)
                                def busy(*arguments: object, **options: object) -> worker_panes.WorkerStart:
                                    result = start(*arguments, **options)
                                    return replace(result, status=herdr.AgentStatus.UNKNOWN) if isinstance(result, worker_panes.OpenWorker) else result
                                _ = patches.enter_context(patch.object(worker_panes, "start_or_resume", side_effect=busy))
                            if failure in ("recorder", "delivery"):
                                with case.assertRaisesRegex((OSError, ValueError), "TEST .* failed"):
                                    case.assertIsInstance(case.dispatch(), worker.ExitCode)
                            else:
                                code = case.dispatch(dispatch_id="TEST-other-pass" if failure == "dispatch id" else "")
                                case.assertEqual(code, worker.ExitCode.REFUSED if failure == "dispatch id" else
                                                 worker.ExitCode.BUSY if failure == "busy" else worker.ExitCode.UNCERTAIN)
                        case.assertEqual(case.agent.submitted, [])
                        if failure == "compact":
                            case.assertIn("TEST compact failed; prompt is sent without compaction", errors.getvalue())
                            case.assertIn("dispatch target is unproved", errors.getvalue())
                        if existing:
                            case.assertEqual(staff.snapshot().rows, before[0])
                            case.assertEqual(set(case.fixture.world.locations), before[1])
                            case.assertEqual(len(case.fixture.world.closed), closed + (existing == "closed"))
                        else:
                            case.assertEqual(staff.find(kind="worker", parent=case.fixture.parent.id), [])
                            case.assertEqual(len(case.fixture.world.closed), 1)
                        case.assertNotIn("cleanup failed", errors.getvalue())
                    finally:
                        case.doCleanups()

    def test_failed_dispatch_closes_its_new_pane_with_a_named_target(self) -> None:
        for family in ("claude", "codex"):
            with self.subTest(family=family):
                case = WorkerCommandTests("runTest")
                case.setUp()
                try:
                    _ = write_registry(case.fixture.root / "conf/models.conf", {"assignments": {"direct": family}})
                    rows: list[staff.StaffRow] = []
                    targets: list[str] = []
                    close = case.fixture.world.close_tab_confirmed
                    def refuse_empty_target(pane: str, *, target: str = "", terminal: str = "") -> bool:
                        if pane in case.fixture.world.locations:
                            targets.append(target)
                            if not target:
                                raise herdr_types.HerdrFailure("TEST close needs a named target")
                        return close(pane, target=target, terminal=terminal)
                    def fail(*_arguments: object, **_options: object) -> str:
                        row, = staff.find(kind="worker", parent=case.fixture.parent.id)
                        rows.append(row)
                        if family == "claude":
                            path = case.fixture.root / "pane-records/agents.json"
                            entries = cast(dict[str, object], json.loads(path.read_text())) if path.exists() else {}
                            case.assertEqual(entries, {})
                        raise ValueError("TEST recorder failed")
                    with (patch.object(progress_history, "begin_dispatch", side_effect=fail),
                          patch.object(case.fixture.world, "close_tab_confirmed", side_effect=refuse_empty_target),
                          redirect_stderr(io.StringIO()) as errors):
                        with case.assertRaisesRegex(ValueError, "TEST recorder failed"):
                            _ = case.dispatch()
                    row, = rows
                    case.assertEqual(targets, [str(row.current_session)])
                    case.assertIsNone(staff.by_id(row.id))
                    case.assertEqual(len(case.fixture.world.closed), 1)
                    case.assertEqual(case.agent.submitted, [])
                    case.assertNotIn("cleanup failed", errors.getvalue())
                finally:
                    case.doCleanups()

    def test_dispatch_keeps_a_replacement_terminal_after_preparation_fails(self) -> None:
        def replace_terminal(*_arguments: object, **_options: object) -> str:
            row, = staff.find(kind="worker", parent=self.fixture.parent.id)
            state = worker_panes.observe_worker(self.fixture.parent.id, row, staff.snapshot())
            assert isinstance(state, worker_panes.OpenWorker)
            self.fixture.world.locations[state.pane]["terminal_id"] = "TEST-replacement"
            raise ValueError("TEST recorder failed")
        with (patch.object(progress_history, "begin_dispatch", side_effect=replace_terminal),
              redirect_stderr(io.StringIO()) as errors):
            with self.assertRaisesRegex(ValueError, "TEST recorder failed"):
                _ = self.dispatch()
        self.assertEqual(self.fixture.world.closed, [])
        self.assertEqual(len(staff.find(kind="worker", parent=self.fixture.parent.id)), 1)
        self.assertEqual(errors.getvalue().count("anomaly:"), 1)
        self.assertIn("terminal identity changed", errors.getvalue())

    def test_dispatch_uses_saved_start_ownership_without_a_pane_read(self) -> None:
        for incomplete in (False, True):
            with self.subTest(incomplete=incomplete):
                case = WorkerCommandTests("runTest")
                case.setUp()
                try:
                    start = cast(Callable[..., worker_panes.WorkerStart], worker_panes.start_or_resume)
                    arm = worker.arm_launch_cleanup
                    def launch(*arguments: object, **options: object) -> worker_panes.WorkerStart:
                        result = start(*arguments, **options)
                        if incomplete:
                            assert isinstance(result, worker_panes.OpenWorker)
                            return worker_panes.IncompleteStart(result.row, result.pane, "TEST startup failed", result.created_pane)
                        return result
                    def arm_without_read(cleanup: ExitStack, before: frozenset[staff.StaffId],
                                         result: worker_panes.WorkerStart) -> None:
                        with patch.object(worker, "_client", side_effect=AssertionError("no ownership pane read")):
                            arm(cleanup, before, result)
                    with (patch.object(worker_panes, "start_or_resume", side_effect=launch),
                          patch.object(worker, "arm_launch_cleanup", side_effect=arm_without_read),
                          redirect_stderr(io.StringIO()) as errors):
                        case.assertEqual(case.dispatch(), worker.ExitCode.UNCERTAIN if incomplete else worker.DispatchSubmission.SUBMITTED)
                    case.assertEqual(len(case.agent.submitted), 0 if incomplete else 1)
                    case.assertEqual(len(case.fixture.world.closed), int(incomplete))
                    if incomplete:
                        case.assertIn("TEST startup failed", errors.getvalue())
                finally:
                    case.doCleanups()

    def test_failed_first_assignment_ends_only_its_new_team_panes(self) -> None:
        for failure in ("selection", "dispatch"):
            with self.subTest(failure=failure):
                case = WorkerCommandTests("runTest")
                case.setUp()
                try:
                    with ExitStack() as patches, redirect_stderr(io.StringIO()) as errors:
                        if failure == "selection":
                            _ = patches.enter_context(patch.object(worker, "select_worker", return_value=worker_panes.StartRefused(
                                worker_panes.StartRefusal.BUSY_ROLE_CHANGE, "TEST selection failed")))
                        else:
                            _ = patches.enter_context(patch.object(worker, "dispatch", return_value=worker.ExitCode.REFUSED))
                        code = worker.assign(case.fixture.parent.id, "impl", case.prompt,
                            session_dir=case.run_dir, slot="impl", phase="8", kind="impl", launcher_command=["assign.sh"])
                    case.assertEqual(code, worker.ExitCode.REFUSED)
                    case.assertEqual(staff.find(kind="worker", parent=case.fixture.parent.id), [])
                    case.assertEqual(len(case.fixture.world.closed), 4)
                    case.assertEqual(case.agent.submitted, [])
                    case.assertNotIn("cleanup failed", errors.getvalue())
                finally:
                    case.doCleanups()

    def test_failed_restore_before_submission_ends_only_its_new_pane(self) -> None:
        for existing, incomplete, refused in ((False, False, False), (True, False, False),
                                               (False, True, False), (False, False, True)):
            with self.subTest(existing=existing, incomplete=incomplete, refused=refused):
                case = WorkerCommandTests("runTest")
                case.setUp()
                try:
                    opened = case.opened()
                    _ = progress_history.begin_dispatch(case.run_dir, slot="impl", phase="8", role="impl",
                        worker_name=opened.row.name, worker_id=opened.row.id, launcher_command=["assign.sh"],
                        prompt=case.prompt, kind="impl", lens="")
                    if not existing:
                        case.assertIsInstance(worker_panes.close(case.fixture.parent.id, staff.WorkerName.parse(opened.row.name)),
                                              worker_panes.Closed)
                    closed = len(case.fixture.world.closed)
                    director = herdr.TabSession("w1:p0", "unit", "w1", str(case.fixture.parent.current_session))
                    start = cast(Callable[..., worker_panes.WorkerStart], worker_panes.start_or_resume)
                    def launch(*arguments: object, **options: object) -> worker_panes.WorkerStart:
                        result = start(*arguments, **options)
                        if incomplete:
                            assert isinstance(result, worker_panes.OpenWorker)
                            return worker_panes.IncompleteStart(result.row, result.pane, "TEST resume read failed", result.created_pane)
                        return result
                    check = cast(Callable[..., bool], herdr.check_target)
                    def target(*arguments: object, **options: object) -> bool:
                        return False if refused and options.get("action") == "keys" else check(*arguments, **options)
                    step = (worker.UnsubmittedAssignment("TEST-pass", ("assign.sh",)) if refused else
                            worker.DispatchEvidenceUnknown("TEST resume read failed"))
                    with (patch.object(worker_panes, "start_or_resume", side_effect=launch),
                          patch.object(worker, "resume_step", return_value=step),
                          patch.object(herdr, "check_target", side_effect=target),
                          patch.object(case.fixture.world, "tab_sessions", return_value=[director]),
                          redirect_stderr(io.StringIO()) as errors):
                        code = worker.restore(case.fixture.parent.id)
                    case.assertEqual(code, worker.ExitCode.UNCERTAIN)
                    case.assertIn("would send keys to an unproved target" if refused else "TEST resume read failed", errors.getvalue())
                    case.assertEqual(case.agent.submitted, [])
                    if existing:
                        case.assertEqual(staff.by_id(opened.row.id), opened.row)
                        case.assertEqual(len(case.fixture.world.closed), closed)
                    else:
                        case.assertEqual(staff.by_id(opened.row.id), opened.row)
                        case.assertEqual(len(case.fixture.world.closed), closed + 1)
                finally:
                    case.doCleanups()

    def test_publication_failure_reports_that_its_new_pane_was_closed(self) -> None:
        with (patch.object(staff, "ensure_worker", side_effect=staff.StaffRefused("TEST admission refused")),
              redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(self.dispatch(), worker.ExitCode.UNCERTAIN)
        self.assertIn("pane w1:p1 closed", errors.getvalue())
        self.assertNotIn("retained", errors.getvalue())
        self.assertEqual(self.fixture.world.closed, [("w1:p1", "terminal-w1:p1")])
        self.assertEqual(self.agent.submitted, [])

    def test_publication_failure_reports_that_its_new_pane_close_failed(self) -> None:
        with (patch.object(staff, "ensure_worker", side_effect=staff.StaffRefused("TEST admission refused")),
              patch.object(self.fixture.world, "close_pane", return_value=False),
              redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(self.dispatch(), worker.ExitCode.UNCERTAIN)
        self.assertIn("pane w1:p1 close failed", errors.getvalue())
        self.assertEqual(sum(line.startswith("anomaly:") for line in errors.getvalue().splitlines()), 1)
        self.assertIn("w1:p1", self.fixture.world.locations)
        self.assertEqual(self.agent.submitted, [])

    def test_publication_failure_closes_the_backend_without_delivery(self) -> None:
        publish = staff.ensure_worker
        def failed(identity: staff.StaffId, name: str, parent: staff.StaffId) -> staff.StaffRow:
            del identity, name, parent
            raise staff.StaffRefused("publication unavailable")
        with patch.object(staff, "ensure_worker", side_effect=failed):
            self.assertEqual(self.dispatch(), worker.ExitCode.UNCERTAIN)
        self.assertIs(staff.ensure_worker, publish)
        self.assertEqual(self.agent.submitted, [])
        self.assertEqual(len(self.fixture.world.launches), 1)
        self.assertEqual(staff.snapshot().find(kind="worker", parent=self.fixture.parent.id), [])
        self.assertEqual(self.dispatch(), worker.ExitCode.OK)
        self.assertEqual(len(self.fixture.world.launches), 2)
        self.assertEqual(len(self.agent.submitted), 1)

    def test_admission_failure_has_the_exact_worker_refusal(self) -> None:
        parent = self.fixture.worker("parent-worker")
        with redirect_stderr(io.StringIO()) as errors:
            code = worker.dispatch(parent.id, staff.WorkerName.parse("child"), "impl", self.prompt,
                session_dir=self.run_dir, slot="impl", phase="8", kind="impl", launcher_command=["assign.sh"])
        self.assertEqual(code, worker.ExitCode.REFUSED)
        self.assertEqual(errors.getvalue(), "staff: child: not opened: only a unit or showrunner can admit a worker\n")
        self.assertEqual(self.agent.submitted, [])
        self.assertEqual(staff.snapshot().find(kind="worker", parent=parent.id), [])

    def test_unknown_process_evidence_and_structural_fault_never_deliver(self) -> None:
        opened = self.opened()
        self.fixture.world.unknown[opened.pane] = "native reader unavailable"
        before = self.fixture.staff_state()
        with redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(self.dispatch(), worker.ExitCode.UNCERTAIN)
        self.assertEqual(errors.getvalue().count("native reader unavailable"), 1)
        self.fixture.world.unknown.clear()
        self.fixture.world.locations[opened.pane]["terminal_id"] = ""
        self.assertEqual(self.dispatch(), worker.ExitCode.UNCERTAIN)
        self.assertEqual(self.agent.submitted, [])
        self.assertEqual(self.fixture.staff_state(), before)

    def test_start_refusal_retains_the_active_role_and_worker(self) -> None:
        opened = self.opened()
        self.fixture.dispatch(opened.row, "impl")
        before = self.fixture.staff_state()
        with (patch("scripts.agents.detached_runs.live_runs", return_value=[object()]),
              redirect_stderr(io.StringIO()) as errors):
            code = self.dispatch(role="review")
        self.assertEqual(int(code), 2)
        self.assertEqual(errors.getvalue(), "role change blocked by active work: a detached run is live\n")
        self.assertEqual(self.agent.submitted, [])
        self.assertEqual(self.fixture.staff_state(), before)

    def test_dispatch_follows_the_current_native_session_after_replacement(self) -> None:
        row = self.fixture.worker("impl", family="codex")
        self.assertNotEqual(row.id, row.current_session)
        self.fixture.world.add(row, "w1:p9", live=False)
        opened = self.opened()
        self.fixture.world.remove_leaf(opened.pane)
        location = self.fixture.world.locations.pop(opened.pane)
        self.fixture.world.locations["w2:p9"] = {**location, "pane_id": "w2:p9", "tab_id": "w2:t2", "workspace_id": "w2"}
        self.fixture.world.layouts["w2:t2"] = {"type": "pane", "pane_id": "w2:p9"}
        self.fixture.world.foreground["w2:p9"] = self.fixture.world.foreground.pop(opened.pane)
        self.assertEqual(self.dispatch(), worker.ExitCode.OK)
        self.assertEqual(self.agent.submitted[0][0], "w2:p9")
        self.assertEqual(self.agent.row("w2:p9").current_session, row.current_session)
        self.assertEqual(self.agent.row("w2:p9").id, row.id)

    def test_selector_uses_role_number_order_and_reuses_a_stopped_worker(self) -> None:
        second = self.opened("impl2")
        first = self.opened("impl")
        chosen = worker.select_worker(self.fixture.parent.id, "impl", staff.snapshot())
        self.assertEqual(chosen, staff.WorkerName.parse("impl"))
        self.fixture.world.locations[first.pane]["agent_status"] = "working"
        _ = self.fixture.world.stop(second.row, self.fixture.world)
        self.fixture.world.reads.clear()
        chosen = worker.select_worker(self.fixture.parent.id, "impl", staff.snapshot())
        self.assertEqual(chosen, staff.WorkerName.parse("impl2"))
        self.assertEqual(self.fixture.world.reads.count(("pane", "list")), 1)
        launches = len(self.fixture.world.launches)
        self.assertEqual(self.dispatch("impl2"), worker.ExitCode.OK)
        self.assertEqual(len(self.fixture.world.launches), launches + 1)
        self.assertEqual(self.agent.submitted[0][0], second.pane)
        self.assertEqual(self.agent.row(self.agent.submitted[0][0]).current_session, second.row.current_session)

    def test_all_busy_assignment_names_each_worker_and_refuses(self) -> None:
        for name in ("impl", "impl2"):
            opened = self.opened(name)
            self.fixture.world.locations[opened.pane]["agent_status"] = "working"
        with redirect_stderr(io.StringIO()) as errors:
            code = worker.assign(self.fixture.parent.id, "impl", self.prompt, session_dir=self.run_dir,
                slot="impl", phase="8", kind="impl", launcher_command=["assign.sh"])
        self.assertEqual(code, worker.ExitCode.REFUSED)
        self.assertIn("impl, impl2", errors.getvalue())
        self.assertEqual(self.agent.submitted, [])

    def test_unnamed_assignment_keeps_the_busy_workers_pass(self) -> None:
        def assign(launcher: str) -> worker.DispatchSubmission | worker.ExitCode:
            return worker.assign(self.fixture.parent.id, "review", self.prompt, session_dir=self.run_dir,
                slot="review", phase="8", kind="review", launcher_command=["assign.sh", launcher], reply=self.reply)

        def passes() -> dict[str, dict[str, object]]:
            return cast(dict[str, dict[str, dict[str, object]]],
                        json.loads((self.run_dir / "progress_history_state.json").read_text()))["pass"]

        self.assertEqual(assign("adversary"), worker.ExitCode.OK)
        busy = self.agent.submitted[0][0]
        self.fixture.world.locations[busy]["agent_status"] = "working"
        _ = self.opened("review2", "review")
        held = passes()["review"]
        self.assertEqual(assign("rerun"), worker.ExitCode.OK)
        self.assertNotEqual(self.agent.submitted[1][0], busy)
        self.assertEqual(passes()["review"], held)
        self.assertEqual((held["worker_name"], held["status"]), ("review", "active"))
        self.assertEqual((passes()["review2"]["worker_name"], passes()["review2"]["status"]), ("review2", "active"))

    def test_add_uses_the_lowest_free_name(self) -> None:
        _ = self.opened("impl")
        _ = self.opened("impl3")
        added = worker.add(self.fixture.parent.id, "impl")
        self.assertIsInstance(added, worker_panes.OpenWorker)
        assert isinstance(added, worker_panes.OpenWorker)
        self.assertEqual(added.row.name, "impl2")

    def test_first_standard_assignment_creates_the_default_team(self) -> None:
        code = worker.assign(self.fixture.parent.id, "impl", self.prompt, session_dir=self.run_dir,
            slot="impl", phase="8", kind="impl", launcher_command=["assign.sh"], reply=self.reply)
        self.assertEqual(code, worker.ExitCode.OK)
        self.assertEqual({row.name for row in staff.snapshot().find(kind="worker", parent=self.fixture.parent.id)},
                         {"impl", "fix", "review", "test"})
        self.assertEqual(len(self.agent.submitted), 1)

    def test_assignment_continues_after_a_default_split_interruption(self) -> None:
        row = self.fixture.worker("impl", family="codex")
        self.fixture.world.add(row, "w1:p1", family="codex", live=False)
        self.fixture.world.interrupt = "split"
        with redirect_stderr(io.StringIO()) as errors:
            result = worker.assign(self.fixture.parent.id, "impl", self.prompt, session_dir=self.run_dir,
                slot="impl", phase="8", kind="impl", launcher_command=["assign.sh"], reply=self.reply)
        self.assertEqual(result, worker.ExitCode.OK)
        self.assertEqual(len(errors.getvalue().splitlines()), 1, errors.getvalue())
        self.assertIn("fix", errors.getvalue())
        self.assertEqual({row.name for row in staff.snapshot().find(kind="worker", parent=self.fixture.parent.id)},
                         {"impl", "review", "test"})
        self.assertIsInstance(self.receipt("impl"), worker.DispatchComplete)
        splits = len(self.fixture.world.locations)
        code = worker.assign(self.fixture.parent.id, "fix", self.prompt, session_dir=self.run_dir,
            slot="fix", phase="8", kind="fix", launcher_command=["assign.sh"], reply=self.reply,
            name="fix", fix_cause="Resume the interrupted startup")
        self.assertEqual(code, worker.ExitCode.OK)
        self.assertEqual({row.name for row in staff.snapshot().find(kind="worker", parent=self.fixture.parent.id)},
                         {"impl", "fix", "review", "test"})
        self.assertEqual(len(self.fixture.world.locations), splits)
        self.assertEqual(self.finish("fix"), turn_end.TurnOutcome.DONE)
        self.assertEqual(len(self.fixture.world.locations), splits - 1)
        self.assertEqual(len(self.agent.submitted), 2)

    def test_named_json_add_reconciles_a_lost_acknowledgment(self) -> None:
        code, output, errors = self.command("add", "astra-designer", "--name", "astra-designer-held1", "--json",
                                            "--session-dir", str(self.run_dir))
        self.assertEqual(code, 0, errors)
        first = cast(dict[str, object], json.loads(output))
        launches = len(self.fixture.world.launches)
        code, output, errors = self.command("add", "astra-designer", "--name", "astra-designer-held1", "--json")
        self.assertEqual(code, 0, errors)
        self.assertEqual(json.loads(output), first)
        self.assertEqual(len(self.fixture.world.launches), launches)
        self.assertEqual(self.agent.submitted, [])

    def test_cli_accepts_json_launcher_arguments_and_reply_alias(self) -> None:
        code, output, errors = self.command("assign", "impl", str(self.prompt),
            "--session-dir", str(self.run_dir), "--slot", "impl", "--phase", "8", "--kind", "impl",
            "--to", "impl", "--reply-file", str(self.reply),
            "--launcher-command", json.dumps(["assign.sh", "--prompt", str(self.prompt)]))
        self.assertEqual((code, output), (0, "submitted\n"), errors)
        self.assertEqual(len(self.agent.submitted), 1)
        self.assertEqual(self.finish(), turn_end.TurnOutcome.DONE)
        self.assertIn(self.agent.final, self.reply.read_text())

    def test_early_review_uses_one_pass_and_two_transcript_receipts(self) -> None:
        ready = self.run_dir / "final.ready"
        atomic_file.write_text_atomic(self.reply, "The previous full report.\n")
        result = worker.dispatch(self.fixture.parent.id, staff.WorkerName.parse("review"), "review", self.prompt,
            session_dir=self.run_dir, slot="review", phase="8", kind="review", launcher_command=["review.sh"],
            early_ready=ready, reply=self.reply)
        self.assertIs(result, worker.DispatchSubmission.SUBMITTED)
        self.assertEqual(self.finish("review"), turn_end.TurnOutcome.WAITING_ON_STAGE_TWO)
        self.assertEqual(len(self.agent.submitted), 1)
        atomic_file.write_text_atomic(ready, "ready\n")
        self.assertEqual(self.finish("review", turn_end.EventKind.STAGE_READY), turn_end.TurnOutcome.SUBMITTED)
        first = self.agent.submitted[0][1].split("DONE ")[-1].split()[0].removesuffix(".")
        second = self.agent.submitted[1][1].split("DONE ")[-1].split()[0].removesuffix(".")
        self.assertEqual(second, first + "-2")
        self.assertEqual(self.reply.read_text(), "The previous full report.\n")
        self.assertEqual(self.finish("review"), turn_end.TurnOutcome.DONE)
        self.assertTrue(self.reply.read_text().endswith("DONE " + second + "\n"))
        self.assertEqual(self.finish("review"), turn_end.TurnOutcome.IGNORED)
        self.assertEqual(len(self.agent.submitted), 2)

    def test_unnamed_retry_after_launcher_loss_reuses_the_busy_submitted_worker(self) -> None:
        args = ("assign", "impl", str(self.prompt), "--session-dir", str(self.run_dir),
            "--slot", "impl", "--phase", "8", "--kind", "impl", "--reply", str(self.reply),
            "--launcher-command", json.dumps(["assign.sh", "--prompt", str(self.prompt)]))
        self.agent.interrupt = "prompt"
        code, output, errors = self.command(*args)
        self.assertEqual((code, output), (8, ""))
        self.assertIn("interrupted after prompt", errors)
        submitted_pane = self.agent.submitted[0][0]
        self.fixture.world.locations[submitted_pane]["agent_status"] = "working"
        idle = self.opened("impl2")
        before = self.fixture.staff_state(), len(self.fixture.world.launches), self.fixture.world.locations.copy()
        pass_state = (self.run_dir / "progress_history_state.json").read_bytes()
        self.agent.finish_after = 1
        code, output, errors = self.command(*args)
        self.assertEqual((code, output, errors), (0, "submitted\n", ""))
        self.assertEqual(len(self.agent.submitted), 1)
        self.assertEqual(self.agent.waited, [])
        self.assertNotEqual(submitted_pane, idle.pane)
        expected_panes = {pane: location for pane, location in before[2].items() if pane != submitted_pane}
        self.assertEqual((self.fixture.staff_state(), len(self.fixture.world.launches),
                          self.fixture.world.locations.copy()), before)
        self.assertEqual((self.run_dir / "progress_history_state.json").read_bytes(), pass_state)
        self.agent.complete(submitted_pane)
        self.assertEqual(self.finish(), turn_end.TurnOutcome.DONE)
        self.assertEqual(self.fixture.world.locations, expected_panes)
        self.assert_pass_completed(pass_state)
        self.assertIn(self.agent.final, self.reply.read_text())

    def test_unnamed_retry_after_completion_returns_the_recorded_reply(self) -> None:
        args = ("assign", "impl", str(self.prompt), "--session-dir", str(self.run_dir),
            "--slot", "impl", "--phase", "8", "--kind", "impl", "--reply", str(self.reply),
            "--launcher-command", json.dumps(["assign.sh", "--prompt", str(self.prompt)]))
        self.assertEqual(self.command(*args), (0, "submitted\n", ""))
        self.assertEqual(self.finish(), turn_end.TurnOutcome.DONE)
        reply = self.reply.read_bytes()
        self.reply.unlink()
        state = (self.run_dir / "progress_history_state.json").read_bytes()
        before = self.fixture.staff_state(), len(self.fixture.world.launches), self.fixture.world.locations.copy()
        self.assertEqual(self.command(*args), (0, "submitted\n", ""))
        self.assertEqual(self.reply.read_bytes(), reply)
        self.assertEqual((self.fixture.staff_state(), len(self.fixture.world.launches), self.fixture.world.locations.copy()), before)
        self.assertEqual(len(self.agent.submitted), 1)
        self.assertEqual(self.agent.waited, [])
        self.assertEqual((self.run_dir / "progress_history_state.json").read_bytes(), state)

    def test_role_changes_read_completion_before_recording_the_new_pass(self) -> None:
        _ = write_registry(self.fixture.root / "conf/models.conf", {
            "design_roles.claude": {"designer": "sonnet:high"},
            "design_roles.codex": {"designer": "gpt-test:low"},
        }, update=True)
        for role in ("review", "astra-designer"):
            with self.subTest(role=role):
                self.assertEqual(self.dispatch(), worker.ExitCode.OK)
                with patch.object(sys, "argv", ["progress_history.py", "finish-pass", "--session-dir", str(self.run_dir),
                                               "--status", "completed"]):
                    progress_history.main()
                before_roles: list[object] = []
                launch = cast(Callable[..., herdr.AgentLaunched], self.fixture.world.launch)
                def changing(*args: object, **kwargs: object) -> herdr.AgentLaunched:
                    state = cast(dict[str, object], json.loads((self.run_dir / "progress_history_state.json").read_text()))
                    passes = cast(dict[str, dict[str, object]], state["pass"])
                    before_roles.append(passes["impl"]["kind"])
                    return launch(*args, **kwargs)
                with patch.object(herdr, "launch_prepared_agent", side_effect=changing):
                    self.assertEqual(self.dispatch(role=role), worker.ExitCode.OK)
                self.assertEqual(before_roles, ["impl"])
                with patch.object(sys, "argv", ["progress_history.py", "finish-pass", "--session-dir", str(self.run_dir),
                                               "--status", "completed"]):
                    progress_history.main()

    def test_dispatch_role_survives_a_new_phase_with_an_empty_pass_map(self) -> None:
        self.assertEqual(self.dispatch(role="review"), worker.ExitCode.OK)
        row = self.agent.row(self.agent.submitted[-1][0])
        for arguments in (["finish-pass", "--status", "completed"],
                          ["finish-phase", "--status", "completed"],
                          ["start-phase", "--phase-id", "9", "--phase-title", "Next phase"]):
            with patch.object(sys, "argv", ["progress_history.py", *arguments, "--session-dir", str(self.run_dir)]):
                progress_history.main()
        state = cast(dict[str, object], json.loads((self.run_dir / "progress_history_state.json").read_text()))
        self.assertEqual(state["pass"], {})
        self.assertEqual(worker_panes.dispatch_role(row), "review")

    def test_dispatch_role_reads_the_workers_first_open_pass(self) -> None:
        opened = self.opened()
        dispatch_id = progress_history.begin_dispatch(self.run_dir, slot="impl", phase="8", role="review",
            worker_name=opened.row.name, worker_id=opened.row.id, launcher_command=["assign.sh"],
            prompt=self.prompt, kind="review", lens="")
        passed = worker_panes.worker_pass(opened.row, self.run_dir)
        self.assertEqual((passed["instance_id"], passed["status"]), (dispatch_id, "active"))
        current = staff.by_id(opened.row.id)
        assert current is not None
        self.assertEqual(worker_panes.dispatch_role(current), "review")

    def test_private_run_restore_uses_finished_dispatch_history(self) -> None:
        self.assertEqual(self.dispatch(role="review"), worker.ExitCode.OK)
        row = self.agent.row(self.agent.submitted[-1][0])
        with patch.object(sys, "argv", ["progress_history.py", "finish-pass", "--status", "completed",
                                      "--session-dir", str(self.run_dir)]):
            progress_history.main()
        private_root = self.fixture.root / "private"
        private_root.mkdir()
        self.run_dir = self.run_dir.rename(private_root / "held-run")
        _ = self.fixture.world.stop(row, self.fixture.world)
        before = len(self.fixture.world.launches)
        with patch.dict(os.environ, {"ROSTER_RUN_ROOT": str(private_root)}):
            code, output, errors = self.command("restore")
        self.assertEqual((code, output, errors), (0, f"{self.fixture.parent.name}: workers restored\n", ""))
        self.assertEqual(staff.snapshot().by_id(row.id), row)
        self.assertEqual(len(self.fixture.world.launches), before)
        self.assertNotIn(self.agent.submitted[-1][0], self.fixture.world.foreground)

    def test_herdr_list_shares_history_and_staff_reads_across_workers(self) -> None:
        expected: dict[str, str] = {}
        for name, role in (("impl", "impl"), ("review", "review"), ("test", "test")):
            opened = self.opened(name, role=cast(staff.WorkerRole, role))
            self.fixture.dispatch(opened.row, cast(staff.WorkerRole, role), completed=True)
            _ = staff.set_worker_role(opened.row.id, staff.KnownWorkerRole(cast(staff.WorkerRole, role)))
            # Preserve each finished pass in its own private run.
            saved = self.fixture.root / ("history-" + name)
            saved.mkdir()
            atomic_file.write_text_atomic(saved / "progress_history_state.json",
                (self.run_dir / "progress_history_state.json").read_text())
            expected[name] = role
        started = time.perf_counter()
        enumerated: list[Path] = []
        iterdir = Path.iterdir
        def read_directory(path: Path) -> Iterator[Path]:
            if path == self.fixture.root:
                enumerated.append(path)
            return iterdir(path)
        with patch.object(staff, "snapshot", wraps=staff.snapshot) as snapshots, \
                patch.object(staff, "local_snapshot", wraps=staff.local_snapshot) as local_snapshots, \
                patch.object(Path, "iterdir", read_directory), \
                patch.object(agents_log, "assignments", wraps=agents_log.assignments) as roles:
            code, output, errors = self.command("list", "--json")
        elapsed = time.perf_counter() - started
        self.assertEqual(code, 0, errors)
        rows = cast(list[dict[str, object]], json.loads(output))
        self.assertEqual({str(row["name"]): row["role"] for row in rows}, expected)
        self.assertEqual(snapshots.call_count, 1)
        self.assertEqual(local_snapshots.call_count, 0)
        self.assertEqual(enumerated, [self.fixture.root])
        self.assertEqual(roles.call_count, 1)
        print(f"herdr list with three workers: {elapsed:.6f} seconds")

    def test_assign_shares_role_history_reads_before_selecting_an_unknown_worker(self) -> None:
        for index, name in enumerate(("impl", "fix", "review", "test"), start=1):
            row = self.fixture.worker(name)
            pane = f"w1:p{index}"
            self.fixture.world.add(row, pane)
            self.fixture.world.unknown[pane] = "private process evidence is unavailable"
        enumerated: list[Path] = []
        iterdir = Path.iterdir
        def read_directory(path: Path) -> Iterator[Path]:
            if path == self.fixture.root:
                enumerated.append(path)
            return iterdir(path)
        with patch.object(staff, "local_snapshot", wraps=staff.local_snapshot) as snapshots, \
                patch.object(agents_log, "assignments", wraps=agents_log.assignments) as roles, \
                patch.object(Path, "iterdir", read_directory), redirect_stderr(io.StringIO()):
            result = worker.assign(self.fixture.parent.id, "impl", self.prompt, session_dir=self.run_dir,
                slot="impl", phase="8", kind="impl", launcher_command=["assign.sh"])
        self.assertEqual(result, worker.ExitCode.UNCERTAIN)
        self.assertEqual(snapshots.call_count, 0)
        self.assertEqual(roles.call_count, 1)
        self.assertEqual(enumerated, [self.fixture.root])
        self.assertEqual(self.agent.submitted, [])

    def recorded_list_fixture(self) -> tuple[staff.StaffRow, staff.StaffRow, list[Path]]:
        _ = self.enterContext(patch.object(worker_panes, "worker_run", return_value=self.run_dir))
        _ = self.enterContext(patch.object(worker_panes, "worker_dispatch_state",
                                          return_value=worker_panes.DispatchHoldsWorker("TEST active dispatch")))
        first = self.fixture.worker("same-name")
        manager = fixture_member("showrunner", name="TEST-other-manager")
        second = fixture_worker("same-name", manager.id)
        paths: list[Path] = []
        for row, owner, status, family in (
                (first, self.fixture.parent, "active", "codex"),
                (second, manager, "completed", "claude")):
            run = self.fixture.root / ("recorded-" + owner.name)
            path = run / unit_runs.STATE_FILE
            atomic_file.write_text_atomic(path, json.dumps({"owner_staff_id": owner.id, "pass": {
                row.name: {"worker_id": row.id, "worker_name": row.name, "kind": "review", "status": status,
                           "activity": "Check the saved work.", "started_at": 10, "called_agent": {"family": family}}}}))
            paths.append(path)
            _ = staff.set_worker_role(row.id, staff.KnownWorkerRole("review"))
            _ = staff.set_home(row.id, staff.SessionHome(Path.home() / (".codex2" if family == "codex" else ".claude2")))
        atomic_file.write_text_atomic(accounts.groups_file(), json.dumps({"units": "claude2"}))
        atomic_file.write_text_atomic(accounts.accounts_config_file(), json.dumps({"workers": "codex2"}))
        return first, second, paths

    def test_list_all_uses_staff_role_and_home_without_account_sources(self) -> None:
        first, second, paths = self.recorded_list_fixture()
        for member, role, folder in ((first, "fix", ".codex3"), (second, "test", ".claude4")):
            _ = staff.set_worker_role(member.id, staff.KnownWorkerRole(cast(staff.WorkerRole, role)))
            _ = staff.set_home(member.id, staff.SessionHome(self.fixture.root / folder))
        for path in paths:
            path.unlink()
        history = self.run_dir / "history.jsonl"
        atomic_file.write_text_atomic(history, "Unreadable history.\n")
        original_open = cast(Callable[..., object], Path.open)
        def unreadable(path: Path, *args: object, **kwargs: object) -> object:
            if path == history:
                raise PermissionError("TEST history is unreadable")
            return original_open(path, *args, **kwargs)
        with (patch.object(accounts, "read_role_map", side_effect=AssertionError("no role map")),
              patch.object(accounts, "roles_history_file", side_effect=AssertionError("no account history")),
              patch.object(Path, "open", autospec=True, side_effect=unreadable)):
            code, output, errors = self.command("list", "--json", "--all")
        self.assertEqual(code, 0, errors)
        rows = {str(row["id"]): row for row in cast(list[dict[str, object]], json.loads(output))}
        for member, role, label in ((first, "fix", "codex3"), (second, "test", "claude4")):
            self.assertEqual(rows[member.id]["role"], role)
            self.assertEqual(rows[member.id]["home"], str(self.fixture.root / ("." + label)))
            self.assertEqual(rows[member.id]["account"], label)

    def test_list_all_groups_pane_workers_by_manager_from_records(self) -> None:
        first, second, _paths = self.recorded_list_fixture()
        code, output, errors = self.command("list", "--json", "--all")
        self.assertEqual(code, 0, errors)
        rows = {str(row["id"]): row for row in cast(list[dict[str, object]], json.loads(output))}
        self.assertEqual(set(rows), {first.id, second.id})
        self.assertEqual([rows[row.id]["manager"] for row in (first, second)], [self.fixture.parent.name, "TEST-other-manager"])
        self.assertEqual([rows[row.id]["state"] for row in (first, second)], ["active", "completed"])
        self.assertEqual([rows[row.id]["work"] for row in (first, second)], ["busy", "idle"])
        self.assertEqual([rows[row.id]["account"] for row in (first, second)], ["codex2", "claude2"])
        for row, label in ((first, "codex2"), (second, "claude2")):
            self.assertEqual(rows[row.id]["role"], "review")
            self.assertEqual(rows[row.id]["activity"], "Check the saved work.")
            self.assertEqual(rows[row.id]["home"], str(Path.home() / ("." + label)))
        self.assertEqual(self.command("list", "--all"),
                         (0, "1 same-name review codex2 - busy\n2 same-name review claude2 - idle\n", ""))
        for hold, state in ((staff.Paused("TEST", "TEST", "Hold the worker."), "paused"),
                            (staff.Exited("TEST", "TEST", "Exit the worker."), "exited")):
            with self.subTest(state=state):
                _ = staff.set_hold(first.id, hold)
                self.assertEqual(self.command("list", "--all"),
                                 (0, f"1 same-name review codex2 - {state}\n2 same-name review claude2 - idle\n", ""))
        _ = staff.set_hold(first.id, staff.Active())
        record = staff.local_snapshot()
        cases: tuple[tuple[worker_panes.WorkerDispatchState | OSError | ValueError, str, str], ...] = (
            (worker_panes.ReleasedWorkerPass.NO_PROMPT_DELIVERED, "no prompt was delivered", "idle"),
            (worker_panes.ReleasedWorkerPass.STALE_ACTIVE, "the launcher ended without finishing the pass", "idle"),
            (worker_panes.ReleasedWorkerPass.ENDED, "the pass ended", "idle"),
            (worker_panes.DispatchHoldsWorker("a detached run is live"), "active", "busy"),
            (worker_panes.CompletedWorkerPass("TEST-dispatch"), "completed", "idle"),
            (OSError("TEST state unreadable\nsecond line"), "active", "busy"),
            (ValueError("TEST state invalid\nsecond line"), "active", "busy"),
        )
        for result, state, work in cases:
            with self.subTest(result=result):
                with (patch.object(worker_panes, "worker_run", return_value=self.run_dir) as located,
                      patch.object(worker_panes, "worker_dispatch_state", side_effect=[result]) as classified,
                      redirect_stderr(io.StringIO()) as anomalies):
                    listing = {str(row["id"]): row for row in worker.list_all_workers(record)}
                self.assertEqual((listing[first.id]["state"], listing[first.id]["work"]), (state, work))
                self.assertEqual((listing[second.id]["state"], listing[second.id]["work"]), ("completed", "idle"))
                located.assert_called_once_with(record.by_id(first.id), record=record)
                classified.assert_called_once_with(record.by_id(first.id), self.run_dir)
                self.assertEqual(anomalies.getvalue().splitlines(),
                    [f"anomaly: {first.name}: {' '.join(str(result).splitlines())}"]
                    if isinstance(result, (OSError, ValueError)) else [])
        parent = record.parent_of(first)
        assert parent is not None
        for machine, expected in (
                ("natedev", "1 same-name review codex2 - busy\n2 same-name review claude2 - idle\n"),
                ("mac", "1 same-name review codex2 mac - busy\n2 same-name review claude2 — - idle\n")):
            with self.subTest(machine=machine):
                current = staff.by_id(parent.id)
                assert current is not None
                _ = staff.set_worktree(parent.id, staff.PresentWorktree(machine, self.fixture.root / parent.name), expected=current.worktree)
                self.assertEqual(self.command("list", "--all"), (0, expected, ""))

    def test_list_all_keeps_a_refused_pass_over_an_equal_time_start_event(self) -> None:
        first, _second, paths = self.recorded_list_fixture()
        state = cast(dict[str, object], json.loads(paths[0].read_text()))
        slots = cast(dict[str, dict[str, object]], state["pass"])
        slots[first.name]["status"] = "refused"
        history = self.fixture.root / "refused-history.jsonl"
        state["history_file"] = str(history)
        atomic_file.write_text_atomic(paths[0], json.dumps(state))
        atomic_file.write_text_atomic(history, json.dumps({
            "event_type": "pass_started", "worker_id": first.id, "worker_name": first.name,
            "pass_kind": "review", "pass_activity": "Start the work.", "timestamp_epoch": 10,
            "called_agent": {"family": "codex"}}) + "\n")
        code, output, errors = self.command("list", "--json", "--all")
        self.assertEqual(code, 0, errors)
        rows = {str(row["id"]): row for row in cast(list[dict[str, object]], json.loads(output))}
        self.assertEqual((rows[first.id]["state"], rows[first.id]["work"], rows[first.id]["activity"]),
                         ("refused", "unknown", "Check the saved work."))

    def test_list_all_resolves_the_legacy_unit_account_once_from_stored_history(self) -> None:
        _first, second, _paths = self.recorded_list_fixture()
        atomic_file.write_text_atomic(accounts.groups_file(), json.dumps({"units": "default"}))
        atomic_file.write_text_atomic(accounts.roles_history_file(), json.dumps({
            "role": "unit", "label": "default", "account_label": "claude2", "at": "2026-10-08T00:00:00Z"}) + "\n")
        with patch.object(accounts, "resolve_history_label", wraps=accounts.resolve_history_label) as resolved:
            code, output, errors = self.command("list", "--json", "--all")
        self.assertEqual(code, 0, errors)
        rows = {str(row["id"]): row for row in cast(list[dict[str, object]], json.loads(output))}
        self.assertEqual(rows[second.id]["account"], "claude2")
        self.assertEqual(rows[second.id]["home"], str(Path.home() / ".claude2"))
        resolved.assert_not_called()

    def test_list_all_keeps_valid_history_events_beside_an_incomplete_line(self) -> None:
        first, second, paths = self.recorded_list_fixture()
        history = self.fixture.root / "saved-history.jsonl"
        for path in paths[:2]:
            state = cast(dict[str, object], json.loads(path.read_text()))
            state["history_file"] = str(history)
            state["pass"] = {}
            atomic_file.write_text_atomic(path, json.dumps(state))
        events = [{"event_type": "pass_finished", "worker_id": row.id, "worker_name": row.name,
                   "pass_kind": role, "status": "completed", "pass_activity": activity,
                   "timestamp_epoch": stamp, "called_agent": {"family": family}}
                  for row, role, activity, stamp, family in (
                      (first, "impl", "Earlier work.", 1, "codex"),
                      (first, "fix", "First saved activity.", 20, "codex"),
                      (second, "review", "Second saved activity.", 30, "claude"))]
        bad = '{"event_type":"pass_finished","worker_id":"' + str(first.id) + '",broken}\n'
        atomic_file.write_text_atomic(history, json.dumps(events[0]) + "\n" + bad
            + "".join(json.dumps(event) + "\n" for event in events[1:]) + bad)
        original_open = Path.open
        with (patch.object(json, "loads", wraps=json.loads) as decoded,
              patch.object(Path, "open", autospec=True, side_effect=original_open) as opened):
            code, output, errors = self.command("list", "--json", "--all")
        self.assertEqual(code, 0, errors)
        self.assertEqual(sum(isinstance(cast(object, call.args[0]), bytes) for call in decoded.call_args_list), 3)
        self.assertEqual(sum(cast(tuple[object, ...], call.args)[0] == history for call in opened.call_args_list), 1)
        rows = {str(row["id"]): row for row in cast(list[dict[str, object]], json.loads(output))}
        self.assertEqual([rows[row.id]["state"] for row in (first, second)], ["completed", "completed"])
        self.assertEqual([rows[row.id]["role"] for row in (first, second)], ["review", "review"])
        self.assertEqual([rows[row.id]["activity"] for row in (first, second)],
                         ["First saved activity.", "Second saved activity."])

    def test_list_all_resolves_a_fixed_manager_id_after_its_session_changes(self) -> None:
        first, _second, paths = self.recorded_list_fixture()
        state = cast(dict[str, object], json.loads(paths[0].read_text()))
        _ = state.pop("owner_staff_id")
        state["main_agent"] = {"session_id": self.fixture.parent.id}
        state["pass"] = {}
        history = self.fixture.root / "legacy-history.jsonl"
        state["history_file"] = str(history)
        atomic_file.write_text_atomic(paths[0], json.dumps(state))
        atomic_file.write_text_atomic(history, json.dumps({
            "event_type": "pass_finished", "worker_id": first.id, "pass_kind": "fix",
            "pass_activity": "Repair the saved work.", "status": "completed", "timestamp_epoch": 20,
            "called_agent": {"family": "codex"}}) + "\n")
        _ = staff.update_current_session(self.fixture.parent.current_session, staff.StaffId(fixture_runtime_id("TEST-new-manager-session")))
        code, output, errors = self.command("list", "--json", "--all")
        self.assertEqual(code, 0, errors)
        rows = {str(row["id"]): row for row in cast(list[dict[str, object]], json.loads(output))}
        self.assertEqual((rows[first.id]["state"], rows[first.id]["role"], rows[first.id]["activity"]),
                         ("completed", "review", "Repair the saved work."))

    def test_list_all_reads_only_the_needed_default_account_history_once(self) -> None:
        _first, second, _paths = self.recorded_list_fixture()
        atomic_file.write_text_atomic(accounts.groups_file(), json.dumps({"units": "default", "showrunners": "default"}))
        history = accounts.roles_history_file()
        atomic_file.write_text_atomic(history, "".join(json.dumps(row) + "\n" for row in (
            {"role": "unit", "label": "default", "account_label": "claude2", "at": "2026-10-08T00:00:00Z"},
            {"group": "showrunners", "label": "default", "account_label": "claude3", "at": "2026-10-08T00:00:00Z"})))
        original = Path.read_text
        reads: list[Path] = []
        def read(path: Path, *args: object, **kwargs: object) -> str:
            reads.append(path)
            return original(path, *args, **kwargs)  # pyright: ignore[reportArgumentType]
        with (patch.object(Path, "read_text", autospec=True, side_effect=read),
              patch.object(accounts, "resolve_history_label", wraps=accounts.resolve_history_label) as resolve):
            code, output, errors = self.command("list", "--json", "--all")
        self.assertEqual(code, 0, errors)
        rows = {str(row["id"]): row for row in cast(list[dict[str, object]], json.loads(output))}
        self.assertEqual(rows[second.id]["account"], "claude2")
        self.assertEqual(reads.count(history), 0)
        resolve.assert_not_called()

    def test_list_all_bounds_history_without_a_uuid_creation_time(self) -> None:
        first, _second, paths = self.recorded_list_fixture()
        absent = fixture_worker("absent-worker", self.fixture.parent.id,
                                runtime_id="00000000-0000-4000-8000-000000000001")
        ancient = self.fixture.worker("old-history-worker")
        other = self.fixture.worker("other-history-worker")
        state = cast(dict[str, object], json.loads(paths[0].read_text()))
        history = self.fixture.root / "bounded-history.jsonl"
        state["history_file"] = str(history)
        state["pass"] = {}
        atomic_file.write_text_atomic(paths[0], json.dumps(state))
        other_history = self.fixture.root / "newer-history.jsonl"
        other_run = self.fixture.root / "newer-run"
        atomic_file.write_text_atomic(other_run / unit_runs.STATE_FILE, json.dumps({
            "owner_staff_id": self.fixture.parent.id, "history_file": str(other_history), "pass": {}}))
        atomic_file.write_text_atomic(other_history, json.dumps({"event_type": "pass_started", "worker_id": other.id,
            "pass_kind": "review", "timestamp_epoch": 200, "called_agent": {"family": "codex"}}) + "\n")
        old = {"event_type": "pass_finished", "worker_id": ancient.id, "pass_kind": "impl",
               "status": "completed", "timestamp_epoch": 1}
        latest = {**old, "worker_id": first.id, "pass_kind": "fix", "timestamp_epoch": 20, "pass_activity": "Latest work."}
        content = (json.dumps(old) + "\n" + (json.dumps({"event_type": "pass_finished", "timestamp_epoch": 5,
                   "worker_id": fixture_runtime_id("unrelated-worker"), "text": "Earlier progress." * 100}) + "\n")
                   * 200 + json.dumps(latest) + "\n").encode()
        positions: list[int] = []
        lengths: list[int] = []
        class HistoryReads(io.BytesIO):
            @override
            def read(self, size: int | None = -1) -> bytes:
                positions.append(self.tell())
                data = super().read(size)
                lengths.append(len(data))
                return data
        original = cast(Callable[..., object], Path.open)
        def opened(path: Path, *args: object, **kwargs: object) -> object:
            return HistoryReads(content) if path == history else original(path, *args, **kwargs)
        with (patch.object(Path, "open", autospec=True, side_effect=opened),
              patch.object(json, "loads", wraps=json.loads) as decoded):
            code, output, errors = self.command("list", "--json", "--all")
        self.assertEqual(code, 0, errors)
        self.assertLessEqual(sum(lengths), 64 * 1024 + 1)
        self.assertEqual(sum(isinstance(cast(object, call.args[0]), bytes) for call in decoded.call_args_list), 2)
        rows = {str(row["id"]): row for row in cast(list[dict[str, object]], json.loads(output))}
        self.assertEqual((rows[first.id]["role"], rows[first.id]["activity"]), ("review", "Latest work."))
        self.assertEqual((rows[first.id]["work"], rows[first.id]["state"]), ("idle", "completed"))
        self.assertEqual(rows[ancient.id]["state"], "unknown")
        self.assertEqual(rows[absent.id]["state"], "unknown")
        self.assertEqual(rows[other.id]["state"], "active")
        self.assertTrue(positions)
        self.assertGreater(min(positions), 0)

    def test_list_all_needs_no_caller_staff_row(self) -> None:
        _ = self.recorded_list_fixture()
        with patch.object(staff, "runtime_id", side_effect=AssertionError("no caller lookup")):
            code, output, errors = self.command("list", "--json", "--all")
        self.assertEqual(code, 0, errors)
        self.assertEqual(len(cast(list[object], json.loads(output))), 2)

    def test_list_all_reads_each_record_once_without_live_calls(self) -> None:
        _first, _second, paths = self.recorded_list_fixture()
        reads: list[Path] = []
        original = Path.read_text
        def read(path: Path, *args: object, **kwargs: object) -> str:
            reads.append(path)
            return original(path, *args, **kwargs)  # pyright: ignore[reportArgumentType]
        with (patch.object(Path, "read_text", side_effect=None, autospec=True) as reader,
              patch.object(staff, "snapshot", side_effect=AssertionError("use the local record")),
              patch.object(staff, "_found_backends", side_effect=AssertionError("no backend scan")),
              patch.object(sessions, "running_sessions", side_effect=AssertionError("no process scan")),
              patch.object(herdr, "Herdr", side_effect=AssertionError("no pane read")),
              patch.object(herdr, "codex_conversations", side_effect=AssertionError("no thread scan")),
              patch.object(subprocess, "run", side_effect=AssertionError("no live command"))):
            reader.side_effect = read
            code, output, errors = self.command("list", "--json", "--all")
        self.assertEqual(code, 0, errors)
        self.assertEqual(len(cast(list[object], json.loads(output))), 2)
        for path in (*paths, staff.record_path()):
            self.assertEqual(reads.count(path), 1, str(path))
        for path in (accounts.accounts_config_file(), accounts.groups_file()):
            self.assertEqual(reads.count(path), 0, str(path))

    def test_text_list_numbers_rows_and_keeps_the_worker_fields(self) -> None:
        self.assertEqual(self.command("list"), (0, "", ""))
        self.assertEqual(self.dispatch("impl01"), worker.ExitCode.OK)
        self.assertEqual(self.finish("impl01"), turn_end.TurnOutcome.DONE)
        self.assertEqual(self.command("list"), (0, "1 impl01 impl - closed -\n", ""))
        for number in range(2, 11):
            self.assertEqual(self.dispatch(f"impl{number:02}"), worker.ExitCode.OK)
            self.assertEqual(self.finish(f"impl{number:02}"), turn_end.TurnOutcome.DONE)

        code, output, errors = self.command("list")

        self.assertEqual(code, 0, errors)
        members = staff.snapshot().find(kind="worker", parent=self.fixture.parent.id)
        numbers = [" 1", " 2", " 3", " 4", " 5", " 6", " 7", " 8", " 9", "10"]
        self.assertEqual(output.splitlines(), [
            f"{number} {row.name} impl - closed -" for number, row in zip(numbers, members, strict=True)])
        cases = (
            ("idle", "open idle"),
            ("working", "open busy"),
            ("stopped", "open exited"),
            ("paused", "open paused"),
            ("exited", "open exited"),
            ("unknown", "open unknown"),
        )
        columns = ["- closed -"] * len(members)
        _ = staff.set_home(members[6].id, staff.SessionHome(self.fixture.root / ".codex2"))
        for index, (status, expected) in enumerate(cases):
            row = members[index]
            pane = f"w1:list{index}"
            self.fixture.world.add(row, pane, live=status != "stopped", family="codex")
            self.fixture.world.locations[pane]["agent_status"] = status if status in ("working", "unknown") else "idle"
            if status == "paused":
                _ = staff.set_hold(row.id, staff.Paused("TEST", "TEST", "Hold the worker."))
            elif status == "exited":
                _ = staff.set_hold(row.id, staff.Exited("TEST", "TEST", "Exit the worker."))
            if index == 0:
                _ = staff.set_home(row.id, staff.SessionHome(self.fixture.root / ".codex2"))
            columns[index] = f"{'codex2' if index == 0 else 'codex'} {expected}"
        code, output, errors = self.command("list")
        self.assertEqual(code, 0, errors)
        self.assertEqual(output.splitlines(), [
            f"{number} {row.name} impl {column}"
            for number, row, column in zip(numbers, members, columns, strict=True)])
        self.fixture.world.add(members[0], "w1:duplicate", family="codex")
        self.fixture.world.unknown["w1:list1"] = "TEST process is unreadable"
        code, output, errors = self.command("list")
        self.assertEqual(code, 0, errors)
        self.assertIn(f" 1 {members[0].name} impl - unknown - duplicate native identities; panes retained", output.splitlines())
        self.assertIn(f" 2 {members[1].name} impl - unknown - TEST process is unreadable", output.splitlines())
        known: dict[str, object] = {"name": "known", "role": "impl", "account": "codex2", "pane": "open", "state": "idle"}
        unknown: dict[str, object] = {"name": "unknown", "role": {"state": "WorkerRoleUnavailable"},
                                      "account": {"state": "AccountUnavailable"}, "pane": "open", "state": "idle"}
        for fields, expected in (
                (unknown, "role unknown account unknown"),
                ({key: value for key, value in known.items() if key != "account"}, "impl account unknown"),
                ({key: value for key, value in known.items() if key != "role"}, "role unknown codex2")):
            with self.subTest(fields=fields), patch.object(worker, "list_workers", return_value=[known, fields]):
                self.assertEqual(self.command("list"),
                                 (0, f"1 known impl codex2 open idle\n2 {fields['name']} {expected} open idle\n", ""))
                code, output, errors = self.command("list", "--json")
                self.assertEqual(code, 0, errors)
                self.assertEqual(cast(list[dict[str, object]], json.loads(output)), [known, fields])
        code, json_before, errors = self.command("list", "--json")
        self.assertEqual(code, 0, errors)
        for machine in ("natedev", "mac"):
            with self.subTest(machine=machine):
                parent = self.fixture.parent
                _ = staff.set_worktree(parent.id, staff.PresentWorktree(machine, self.fixture.root), expected=parent.worktree)
                code, output, errors = self.command("list")
                self.assertEqual(code, 0, errors)
                column = " mac" if machine == "mac" else ""
                self.assertIn(f" 3 {members[2].name} impl codex{column} open exited", output.splitlines())
                code, output, errors = self.command("list", "--json")
                self.assertEqual(code, 0, errors)
                self.assertEqual(json.loads(output), json.loads(json_before))

    def test_list_keeps_dispatch_role_separate_from_observation(self) -> None:
        opened = self.opened("impl2", "review")
        self.fixture.dispatch(opened.row, "review", completed=True)
        _ = staff.set_worker_role(opened.row.id, staff.KnownWorkerRole("review"))
        code, output, errors = self.command("list", "--json")
        self.assertEqual(code, 0, errors)
        listing = cast(list[dict[str, object]], json.loads(output))
        self.assertEqual(listing[0]["name"], "impl2")
        self.assertEqual(listing[0]["role"], "review")
        self.assertEqual(listing[0]["state"], "OpenWorker")
        self.assertEqual(listing[0]["activity"], "done")

    def test_list_open_busy_worker_reports_work_account_and_home(self) -> None:
        row = self.fixture.worker("astra-designer", family="codex")
        self.fixture.world.add(row, "w1:p1", family="codex")
        self.fixture.world.locations["w1:p1"]["agent_status"] = "working"
        self.fixture.dispatch(row, "astra-designer")
        home = self.fixture.root / ".codex2"
        _ = staff.set_worker_role(row.id, staff.KnownWorkerRole("astra-designer"))
        _ = staff.set_home(row.id, staff.SessionHome(home))
        with patch.object(accounts, "role_account", return_value=accounts.CodexWorkersAccount("codex2", home)) as selected:
            code, output, errors = self.command("list", "--json")
        self.assertEqual(code, 0, errors)
        listing = cast(list[dict[str, object]], json.loads(output))
        self.assertEqual(len(listing), 1)
        self.assertEqual(listing[0]["state"], "OpenWorker")
        self.assertEqual(listing[0]["role"], "astra-designer")
        self.assertEqual({key: listing[0].get(key) for key in ("work", "account", "home")},
                         {"work": "busy", "account": "codex2", "home": str(home)})
        selected.assert_not_called()

    def test_list_closed_worker_reports_work_account_and_home(self) -> None:
        row = self.fixture.worker("astra-reviewer", family="claude")
        self.fixture.dispatch(row, "astra-reviewer", completed=True)
        home = self.fixture.root / ".claude2"
        _ = staff.set_worker_role(row.id, staff.KnownWorkerRole("astra-reviewer"))
        _ = staff.set_home(row.id, staff.SessionHome(home))
        with patch.object(accounts, "role_account", return_value=accounts.NamedAccount("claude2", home)) as selected:
            code, output, errors = self.command("list", "--json")
        self.assertEqual(code, 0, errors)
        listing = cast(list[dict[str, object]], json.loads(output))
        self.assertEqual(len(listing), 1)
        self.assertEqual(listing[0]["state"], "ClosedWorker")
        self.assertEqual(listing[0]["role"], "astra-reviewer")
        self.assertEqual({key: listing[0].get(key) for key in ("work", "account", "home")},
                         {"work": "idle", "account": "claude2", "home": str(home)})
        selected.assert_not_called()

    def test_list_worker_without_backend_reports_account_unavailable(self) -> None:
        row = staff.ensure_worker(staff.native_id(fixture_runtime_id("missing-backend")),
                                  "missing-backend", self.fixture.parent.id)
        self.fixture.dispatch(row, "impl", completed=True)
        self.assertNotIn(row.id, staff.snapshot().backends)
        code, output, errors = self.command("list", "--json")
        self.assertEqual(code, 0, errors)
        listing = cast(list[dict[str, object]], json.loads(output))
        self.assertEqual(len(listing), 1)
        self.assertEqual(listing[0]["account"],
                         {"state": "AccountUnavailable", "reason": "worker home is unavailable"})
        self.assertNotIn("home", listing[0])
        self.assertEqual(listing[0]["role"],
                         {"state": "WorkerRoleUnavailable", "reason": "dispatch role is unavailable"})
        code, output, errors = self.command("list")
        self.assertEqual(code, 0, errors)
        self.assertIn("role unknown", output)
        self.assertEqual(output, "1 missing-backend role unknown - closed -\n")
        self.assertNotIn("{", output)

    def test_list_scans_runs_once_and_prints_each_run_anomaly_once(self) -> None:
        unit_runs.report_anomaly.cache_clear()
        self.addCleanup(unit_runs.report_anomaly.cache_clear)
        rows = [self.fixture.worker(name) for name in ("impl", "review", "test")]
        for index, row in enumerate(rows, 1):
            self.fixture.world.add(row, f"w1:p{index}")
        runs = self.fixture.root
        for name in ("ownerless-a", "ownerless-b"):
            run = runs / name
            run.mkdir(parents=True)
            atomic_file.write_text_atomic(run / unit_runs.STATE_FILE, '{}')
            atomic_file.write_text_atomic(run / "fixture-impl_status", "running\n")
        with patch.object(unit_runs, "held_by_session", wraps=unit_runs.held_by_session) as scans:
            code, output, errors = self.command("list", "--json")
        self.assertEqual(code, 0, errors)
        self.assertEqual(scans.call_count, 1)
        listing = cast(list[dict[str, object]], json.loads(output))
        self.assertEqual({item["name"] for item in listing}, {row.name for row in rows})
        self.assertEqual(errors.splitlines(), [
            f"anomaly: {runs / name}: this run has no owner because {unit_runs.STATE_FILE} names no staff row"
            for name in ("ownerless-a", "ownerless-b")])

    def test_restore_retains_the_same_native_session_and_reruns_unchanged(self) -> None:
        opened = self.opened()
        self.fixture.dispatch(opened.row, "impl", completed=True)
        _ = self.fixture.world.stop(opened.row, self.fixture.world)
        before = self.fixture.staff_state(), len(self.fixture.world.launches)
        self.assertEqual(self.command("restore"), (0, f"{self.fixture.parent.name}: workers restored\n", ""))
        code, output, errors = self.command("restore")
        self.assertEqual((code, output, errors), (0, f"{self.fixture.parent.name}: workers unchanged\n", ""))
        self.assertEqual((self.fixture.staff_state(), len(self.fixture.world.launches)), before)
        self.assertNotIn(opened.pane, self.fixture.world.locations)
        self.assertEqual(staff.snapshot().by_id(opened.row.id), opened.row)
        self.assertNotIn(opened.pane, self.fixture.world.foreground)

    def test_restore_uses_the_known_row_role_without_history(self) -> None:
        opened = self.opened()
        _ = staff.set_worker_role(opened.row.id, staff.KnownWorkerRole("impl"))
        _ = self.fixture.world.stop(opened.row, self.fixture.world)
        active = progress_history.ActiveDispatch("saved-pass", ("assign.sh",))
        with (patch.object(progress_history, "last_dispatch", return_value=progress_history.NoDispatchRecorded()),
              patch.object(progress_history, "active_dispatch", return_value=active),
              patch.object(worker, "_resume_launcher", return_value=worker.ExitCode.OK)):
            code, _output, errors = self.command("restore")
        self.assertEqual(code, 0, errors)
        self.assertEqual(len(self.fixture.world.launches), 2)

    def test_restore_reports_the_unavailable_role_and_continues_other_workers(self) -> None:
        missing = self.opened("impl")
        retained = self.opened("review", "review")
        _ = self.fixture.world.stop(missing.row, self.fixture.world)
        _ = self.fixture.world.stop(retained.row, self.fixture.world)
        self.fixture.dispatch(retained.row, "review", completed=True)
        before = self.fixture.staff_state(), len(self.fixture.world.launches)
        self.assertEqual(self.command("restore"), (0, f"{self.fixture.parent.name}: workers restored\n", ""))
        self.assertEqual((self.fixture.staff_state(), len(self.fixture.world.launches)), before)
        self.assertNotIn(retained.pane, self.fixture.world.foreground)
        self.assertNotIn(missing.pane, self.fixture.world.foreground)

    def test_end_closes_a_busy_worker_and_retires_its_row(self) -> None:
        opened = self.opened()
        self.fixture.world.locations[opened.pane]["agent_status"] = "working"
        with patch.object(sessions, "session_liveness", return_value=sessions.SessionNotLive()), \
             patch("scripts.production.staff_sources.require_worker_stopped"):
            code, output, errors = self.command("end", "impl")
        self.assertEqual((code, output, errors), (0, "done\n", ""))
        self.assertIsNone(staff.by_id(opened.row.id))
        self.assertNotIn(opened.pane, self.fixture.world.locations)
        self.assertEqual(self.fixture.world.stops, [])

    def test_end_removes_an_idle_worker_with_an_interrupted_pass(self) -> None:
        opened = self.opened()
        self.fixture.dispatch(opened.row, "impl")
        state = self.run_dir / "progress_history_state.json"
        atomic_file.write_text_atomic(state, state.read_text().replace('"active"', '"interrupted"'))
        self.fixture.world.locations[opened.pane]["agent_status"] = "idle"
        with patch.object(sessions, "session_liveness", return_value=sessions.SessionNotLive()), \
             patch("scripts.production.staff_sources.require_worker_stopped"):
            self.assertEqual(worker.end(self.fixture.parent.id, staff.WorkerName.parse("impl")), worker.ExitCode.OK)
        self.assertIsNone(staff.by_id(opened.row.id))
        self.assertNotIn(opened.pane, self.fixture.world.locations)
        self.assertIn("w1:p0", self.fixture.world.locations)

    def test_end_removes_only_the_confirmed_closed_worker(self) -> None:
        opened = self.opened()
        self.fixture.completed_dispatch(opened.row, "impl")
        _ = self.fixture.world.stop(opened.row, self.fixture.world)
        with patch.object(sessions, "session_liveness", return_value=sessions.SessionNotLive()), \
             patch("scripts.production.staff_sources.require_worker_stopped"):
            self.assertEqual(worker.end(self.fixture.parent.id, staff.WorkerName.parse("impl")), worker.ExitCode.OK)
        self.assertIsNone(staff.by_id(opened.row.id))
        self.assertNotIn(opened.pane, self.fixture.world.locations)
        self.assertIn("w1:p0", self.fixture.world.locations)
        self.assertEqual(worker.end(self.fixture.parent.id, staff.WorkerName.parse("impl")), worker.ExitCode.OK)

    def test_check_distinguishes_resumable_workers_from_unknown_evidence(self) -> None:
        opened = self.opened()
        self.fixture.dispatch(opened.row, "impl", completed=True)
        opened = replace(opened, row=staff.set_worker_role(opened.row.id, staff.KnownWorkerRole("impl")))
        self.assertEqual(self.command("check")[0], 0)
        _ = self.fixture.world.stop(opened.row, self.fixture.world)
        self.assertEqual(self.command("check")[0], 0)
        resumed = self.opened()
        self.fixture.world.unknown[resumed.pane] = "process evidence lost"
        code, output, errors = self.command("check")
        self.assertEqual(code, 1)
        self.assertIn("impl", output)
        self.assertIn("process evidence lost", errors)

    def test_native_identity_refuses_a_worker_caller_and_a_supplied_parent(self) -> None:
        opened = self.opened()
        with patch.dict(os.environ, {"CODEX_THREAD_ID": str(opened.row.current_session)}):
            code, output, errors = self.command("add", "review")
        self.assertEqual((code, output), (2, ""))
        self.assertIn("caller is not a unit or showrunner", errors)
        self.assertEqual(len(staff.snapshot().find(kind="worker", parent=self.fixture.parent.id)), 1)

    def test_showrunner_native_identity_selects_only_its_own_children(self) -> None:
        other = fixture_member("showrunner", name="other")
        with patch.dict(os.environ, {"CODEX_THREAD_ID": str(other.current_session)}):
            self.assertEqual(self.command("list", "--json")[:2], (0, "[]\n"))
        self.assertEqual(self.agent.submitted, [])


if __name__ == "__main__":
    _ = unittest.main()
