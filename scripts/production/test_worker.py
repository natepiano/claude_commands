"""Worker commands preserve native identity and use transcripts as dispatch receipts."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from typing import cast, final, override
import unittest
from unittest.mock import patch

from scripts.herdr import test_worker_panes as pane_fixtures
from scripts.herdr import worker_panes
from scripts.direct import progress_history
from scripts.production import staff, worker, worker_admission
from scripts.production.staff_test_support import fixture_member
from scripts.lib import atomic_file, unit_runs
from scripts.lib.test_models_registry import write_registry
from scripts.message import sessions

import herdr


@final
class DispatchAgent:
    """A private agent writes real transcript records at the process boundary."""

    def __init__(self, fixture: pane_fixtures.WorkerPaneTests) -> None:
        self.fixture = fixture
        self.submitted: list[tuple[str, str]] = []
        self.waited: list[str] = []
        self.finish_after = 0
        self.wait_count = 0
        self.final = "The work is complete."
        self.error = ""
        self.interrupt = ""
        self.during_prompt: Callable[[], None] = lambda: None
        self.without_done = False
        self.after_complete: Callable[[str], None] = lambda pane: None
        self.run_process = cast(Callable[..., subprocess.CompletedProcess[str]], subprocess.run)

    def row(self, pane: str) -> staff.StaffRow:
        pid = self.fixture.world.foreground[pane][0]
        identity = self.fixture.world.processes[pid].current_identity
        row = staff.snapshot().by_current_session(identity)
        assert row is not None
        return row

    def append(self, pane: str, role: str, text: str) -> None:
        row = self.row(pane)
        backend = staff.backend(row)
        assert isinstance(backend, (staff.ClaudeBackend, staff.CodexBackend))
        path = worker_admission.transcript_source(row, backend)
        record = ({"type": "event_msg", "payload": {"type": "user_message" if role == "user" else "agent_message", "message": text}}
                  if isinstance(backend, staff.CodexBackend) else
                  {"type": role, "message": {"role": role, "content": [{"type": "text", "text": text}]}})
        with path.open("a", encoding="utf-8") as stream:
            _ = stream.write(json.dumps(record) + "\n")

    def complete(self, pane: str) -> None:
        line = next(text for target, text in reversed(self.submitted) if target == pane)
        dispatch_id = line.split("DONE ")[-1].split(".")[0].strip()
        self.append(pane, "assistant", self.final if self.without_done else self.final + "\nDONE " + dispatch_id)
        self.fixture.world.locations[pane]["agent_status"] = "done"
        self.after_complete(pane)

    def run(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args[0] != "/nonexistent/herdr":
            return self.run_process(args, **kwargs)
        self.fixture.assertNotIn("--timeout", args)
        self.fixture.assertNotIn("--until", args)
        action, pane = args[4:6]
        if action == "prompt":
            line = args[6]
            self.fixture.assertEqual(args[-1], "--wait")
            self.submitted.append((pane, line))
            self.append(pane, "user", line)
            self.during_prompt()
            if self.interrupt == "prompt":
                self.interrupt = ""
                raise OSError("interrupted after prompt")
            self.fixture.world.locations[pane]["agent_status"] = "working"
            if self.finish_after == 0 and not self.error:
                self.complete(pane)
        elif action == "wait":
            self.waited.append(pane)
            self.wait_count += 1
            if self.wait_count >= self.finish_after:
                self.complete(pane)
        else:
            raise AssertionError(args)
        if self.error:
            return subprocess.CompletedProcess(args, 1, json.dumps({"error": {"code": self.error, "message": self.error}}), "")
        return subprocess.CompletedProcess(args, 0, "{}", "")


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
        self.addCleanup(self.fixture.doCleanups)
        self.agent = DispatchAgent(self.fixture)
        self.run_dir = self.fixture.root / "run"
        self.run_dir.mkdir(exist_ok=True)
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

    def dispatch(self, name: str = "impl", role: staff.WorkerRole = "impl", dispatch_id: str = "") -> worker.ExitCode:
        return worker.dispatch(self.fixture.parent.id, staff.WorkerName.parse(name), role, self.prompt, dispatch_id,
            session_dir=self.run_dir, slot="impl", phase="8", kind=role,
            launcher_command=["assign.sh", "--phase", "8", "--prompt", str(self.prompt)], reply=self.reply)

    def command(self, *args: str) -> tuple[int, str, str]:
        with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
            code = worker.main(list(args))
        return code, output.getvalue(), errors.getvalue()

    def test_later_user_message_keeps_the_submitted_assignment_receipt(self) -> None:
        self.agent.interrupt = "prompt"
        with self.assertRaisesRegex(OSError, "after prompt"):
            _ = self.dispatch()
        pane = self.agent.submitted[0][0]
        self.agent.append(pane, "user", "Preserve the existing public interface too.")
        self.fixture.world.locations[pane]["agent_status"] = "working"
        self.agent.finish_after = 1
        self.assertEqual(self.dispatch(), worker.ExitCode.OK)
        self.assertEqual(len(self.agent.submitted), 1)
        self.assertEqual(self.agent.waited, [pane])
        self.assertIn(self.agent.final, self.reply.read_text())

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
        self.assertIn(self.agent.final, self.reply.read_text())
        self.assertNotIn(opened.pane, self.fixture.world.foreground)
        self.assertEqual(len(self.agent.submitted), 1)

    def test_unknown_evidence_during_dispatch_prints_one_scan_diagnostic(self) -> None:
        opened = self.opened()
        def lose_process_evidence() -> None:
            self.fixture.world.unknown[opened.pane] = "dispatch process reader unavailable"
        self.agent.during_prompt = lose_process_evidence
        self.agent.finish_after = 1
        with redirect_stderr(io.StringIO()) as errors:
            code = self.dispatch()
        self.assertEqual(code, worker.ExitCode.UNCERTAIN)
        self.assertEqual(len(errors.getvalue().splitlines()), 1, errors.getvalue())
        self.assertIn("dispatch process reader unavailable", errors.getvalue())
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
                raise herdr.HerdrFailure("restore layout unavailable")
            return request(method, **params)
        with patch.object(self.fixture.world, "socket_request", side_effect=fail_one_layout):
            with redirect_stderr(io.StringIO()) as errors:
                code = worker.restore(self.fixture.parent.id)
        self.assertEqual(code, worker.ExitCode.UNCERTAIN)
        self.assertEqual(errors.getvalue().splitlines(), [f"anomaly: {first.row.name}: restore layout unavailable"])
        resumed = {state.row.id for state in worker_panes.observe(self.fixture.parent.id, staff.snapshot())
                   if isinstance(state, worker_panes.OpenWorker)}
        self.assertIn(second.row.id, resumed)
        before = len(self.fixture.world.launches)
        self.assertEqual(self.command("restore"), (0, "unchanged\n", ""))
        self.assertEqual(len(self.fixture.world.launches), before)

    def test_herdr_dispatch_records_the_worker_staff_id(self) -> None:
        self.assertEqual(self.dispatch(), worker.ExitCode.OK)
        pane = self.agent.submitted[0][0]
        state = cast(dict[str, dict[str, dict[str, object]]],
                     json.loads((self.run_dir / "progress_history_state.json").read_text()))
        self.assertEqual(state["pass"]["impl"]["worker_id"], self.agent.row(pane).id)

    def test_fast_dispatch_writes_reply_without_changing_the_prompt(self) -> None:
        before = self.prompt.read_bytes(), self.prompt.stat().st_mtime_ns
        self.assertEqual(self.dispatch(), worker.ExitCode.OK)
        self.assertEqual(len(self.agent.submitted), 1)
        self.assertIn(self.agent.final, self.reply.read_text())
        self.assertIn("DONE ", self.reply.read_text())
        self.assertEqual((self.prompt.read_bytes(), self.prompt.stat().st_mtime_ns), before)
        self.assertNotIn("--until", self.agent.submitted[0][1])

    def test_long_dispatch_waits_without_submitting_again(self) -> None:
        self.agent.finish_after = 3
        self.assertEqual(self.dispatch(), worker.ExitCode.OK)
        self.assertEqual(len(self.agent.submitted), 1)
        self.assertEqual(len(self.agent.waited), 3)

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

    def test_dispatch_rerun_after_done_restores_the_reply_once(self) -> None:
        write = atomic_file.write_text_atomic
        def interrupted(path: Path, text: str) -> None:
            if path == self.reply:
                raise OSError("interrupted before reply publication")
            write(path, text)
        with patch.object(atomic_file, "write_text_atomic", side_effect=interrupted):
            with self.assertRaisesRegex(OSError, "before reply"):
                _ = self.dispatch()
        self.assertEqual(self.dispatch(), worker.ExitCode.OK)
        before = self.reply.read_bytes(), self.reply.stat().st_mtime_ns, self.fixture.staff_state()
        self.assertEqual(self.dispatch(), worker.ExitCode.OK)
        self.assertEqual((self.reply.read_bytes(), self.reply.stat().st_mtime_ns, self.fixture.staff_state()), before)
        self.assertEqual(len(self.agent.submitted), 1)

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
        self.assertNotIn("old result", self.reply.read_text())

    def test_settled_without_done_preserves_the_existing_report(self) -> None:
        atomic_file.write_text_atomic(self.reply, "Full report from the prior turn.\n")
        opened = self.opened()
        self.agent.error = ""
        def final_without_done() -> None:
            self.agent.append(opened.pane, "assistant", "A detached run will report later.")
            self.fixture.world.locations[opened.pane]["agent_status"] = "idle"
        self.agent.during_prompt = final_without_done
        self.agent.finish_after = 1
        # The process finishes without a final receipt.
        self.agent.without_done = True
        self.agent.finish_after = 0
        self.assertEqual(self.dispatch(), worker.ExitCode.NO_DONE)
        self.assertEqual(self.reply.read_text(), "Full report from the prior turn.\n")

    def test_stalled_dispatch_returns_stalled_and_never_retypes(self) -> None:
        self.agent.error = "agent_prompt_stalled"
        self.assertEqual(self.dispatch(), worker.ExitCode.STALLED)
        self.assertEqual(len(self.agent.submitted), 1)
        self.agent.error = ""
        self.agent.finish_after = 1
        self.assertEqual(self.dispatch(), worker.ExitCode.OK)
        self.assertEqual(len(self.agent.submitted), 1)

    def test_blocked_dispatch_returns_blocked(self) -> None:
        self.agent.error = "agent_blocked"
        self.assertEqual(self.dispatch(), worker.ExitCode.BLOCKED)
        self.assertEqual(len(self.agent.submitted), 1)

    def test_busy_worker_receives_no_prompt(self) -> None:
        opened = self.opened()
        self.fixture.world.locations[opened.pane]["agent_status"] = "working"
        self.assertEqual(self.dispatch(), worker.ExitCode.BUSY)
        self.assertEqual(self.agent.submitted, [])

    def test_dispatch_lock_blocks_a_competing_dispatch_and_close(self) -> None:
        opened = self.opened()
        results: list[object] = []
        def concurrent() -> None:
            results.append(self.dispatch())
            results.append(worker.end(self.fixture.parent.id, staff.WorkerName.parse("impl")))
            results.append(worker_panes.start_or_resume(self.fixture.parent.id, staff.WorkerName.parse("impl"), "review"))
        self.agent.during_prompt = concurrent
        self.assertEqual(self.dispatch(), worker.ExitCode.OK)
        self.assertEqual(results[0], worker.ExitCode.BUSY)
        self.assertEqual(results[1], worker.ExitCode.KEPT)
        self.assertIsInstance(results[2], worker_panes.StartRefused)
        self.assertEqual(len(self.agent.submitted), 1)
        self.assertIsNotNone(staff.by_id(opened.row.id))

    def test_torn_transcript_keeps_the_worker_without_delivery(self) -> None:
        opened = self.opened()
        backend = staff.backend(opened.row)
        assert isinstance(backend, (staff.ClaudeBackend, staff.CodexBackend))
        path = worker_admission.transcript_source(opened.row, backend)
        atomic_file.write_text_atomic(path, '{"torn":')
        self.assertEqual(self.dispatch(), worker.ExitCode.UNCERTAIN)
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
                self.assertEqual(code, worker.ExitCode.NO_DONE)
                self.assertEqual(self.reply.read_text(), "The previous full report.\n")
                self.assertEqual(self.agent.waited, [])
                self.assertEqual(len(self.agent.submitted), 2 if closed else 1)

    def test_incomplete_start_keeps_its_pane_and_records_no_pass(self) -> None:
        self.fixture.world.outcome = herdr.PromptNotSubmitted("startup hook failed")
        self.assertEqual(self.dispatch(), worker.ExitCode.UNCERTAIN)
        self.assertEqual(self.agent.submitted, [])
        self.assertEqual(len(self.fixture.world.launches), 1)
        self.assertEqual(json.loads((self.run_dir / "progress_history_state.json").read_text())["pass"], {})

    def test_publication_failure_retains_the_backend_without_delivery(self) -> None:
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
        self.assertEqual(len(self.fixture.world.launches), 1)
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
        with redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(self.dispatch(role="review"), worker.ExitCode.REFUSED)
        self.assertIn("role change blocked by active work", errors.getvalue())
        self.assertIn("active dispatch has another role", errors.getvalue())
        self.assertEqual(self.agent.submitted, [])
        self.assertIsNotNone(staff.by_id(opened.row.id))

    def test_dispatch_follows_the_current_native_session_after_replacement(self) -> None:
        row = self.fixture.worker("impl")
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
        chosen = worker.select_worker(self.fixture.parent.id, "impl", staff.snapshot())
        self.assertEqual(chosen, staff.WorkerName.parse("impl2"))
        launches = len(self.fixture.world.launches)
        self.assertEqual(self.dispatch("impl2"), worker.ExitCode.OK)
        self.assertEqual(len(self.fixture.world.launches), launches + 1)
        self.assertEqual(self.agent.submitted[0][0], second.pane)

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
        _ = self.opened("impl")
        self.fixture.world.interrupt = "split"
        with redirect_stderr(io.StringIO()) as errors:
            result = worker.assign(self.fixture.parent.id, "impl", self.prompt, session_dir=self.run_dir,
                slot="impl", phase="8", kind="impl", launcher_command=["assign.sh"], reply=self.reply)
        self.assertEqual(result, worker.ExitCode.OK)
        self.assertEqual(len(errors.getvalue().splitlines()), 1, errors.getvalue())
        self.assertIn("fix", errors.getvalue())
        self.assertEqual({row.name for row in staff.snapshot().find(kind="worker", parent=self.fixture.parent.id)},
                         {"impl", "review", "test"})
        self.assertIn(self.agent.final, self.reply.read_text())
        splits = len(self.fixture.world.locations)
        code = worker.assign(self.fixture.parent.id, "fix", self.prompt, session_dir=self.run_dir,
            slot="fix", phase="8", kind="fix", launcher_command=["assign.sh"], reply=self.reply,
            name="fix", fix_cause="Resume the interrupted startup")
        self.assertEqual(code, worker.ExitCode.OK)
        self.assertEqual({row.name for row in staff.snapshot().find(kind="worker", parent=self.fixture.parent.id)},
                         {"impl", "fix", "review", "test"})
        self.assertEqual(len(self.fixture.world.locations), splits)
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
        self.assertEqual((code, output), (0, "done\n"), errors)
        self.assertEqual(len(self.agent.submitted), 1)
        self.assertIn(self.agent.final, self.reply.read_text())

    def test_early_review_uses_one_pass_and_two_transcript_receipts(self) -> None:
        ready = self.run_dir / "final.ready"
        atomic_file.write_text_atomic(self.reply, "The previous full report.\n")
        reports_during_stage_two: list[str] = []
        def ready_after_first() -> None:
            if len(self.agent.submitted) == 1:
                atomic_file.write_text_atomic(ready, "ready\n")
            else:
                reports_during_stage_two.append(self.reply.read_text())
        self.agent.during_prompt = ready_after_first
        def early() -> worker.ExitCode:
            return worker.dispatch(self.fixture.parent.id, staff.WorkerName.parse("review"), "review", self.prompt,
                session_dir=self.run_dir, slot="review", phase="8", kind="review", launcher_command=["review.sh"],
                early_ready=ready, reply=self.reply)
        self.assertEqual(early(), worker.ExitCode.OK)
        self.assertEqual(len(self.agent.submitted), 2)
        first = self.agent.submitted[0][1].split("DONE ")[-1].split(".")[0]
        second = self.agent.submitted[1][1].split("DONE ")[-1].split(".")[0]
        self.assertEqual(second, first + "-2")
        self.assertEqual(reports_during_stage_two, ["The previous full report.\n"])
        self.assertEqual(early(), worker.ExitCode.OK)
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
        self.assertEqual((code, output, errors), (0, "done\n", ""))
        self.assertEqual(len(self.agent.submitted), 1)
        self.assertEqual(self.agent.waited, [submitted_pane])
        self.assertNotEqual(submitted_pane, idle.pane)
        self.assertEqual((self.fixture.staff_state(), len(self.fixture.world.launches),
                          self.fixture.world.locations.copy()), before)
        self.assertEqual((self.run_dir / "progress_history_state.json").read_bytes(), pass_state)
        self.assertIn(self.agent.final, self.reply.read_text())

    def test_unnamed_retry_after_completion_returns_the_recorded_reply(self) -> None:
        args = ("assign", "impl", str(self.prompt), "--session-dir", str(self.run_dir),
            "--slot", "impl", "--phase", "8", "--kind", "impl", "--reply", str(self.reply),
            "--launcher-command", json.dumps(["assign.sh", "--prompt", str(self.prompt)]))
        self.assertEqual(self.command(*args), (0, "done\n", ""))
        reply = self.reply.read_bytes()
        self.reply.unlink()
        with patch.object(sys, "argv", ["progress_history.py", "finish-pass", "--session-dir", str(self.run_dir),
                                       "--status", "completed"]):
            progress_history.main()
        submitted_pane = self.agent.submitted[0][0]
        self.fixture.world.locations[submitted_pane]["agent_status"] = "working"
        _ = self.opened("impl2")
        before = self.fixture.staff_state(), len(self.fixture.world.launches), self.fixture.world.locations.copy()
        pass_state = (self.run_dir / "progress_history_state.json").read_bytes()
        self.assertEqual(self.command(*args), (0, "done\n", ""))
        self.assertEqual(self.reply.read_bytes(), reply)
        self.assertEqual(len(self.agent.submitted), 1)
        self.assertEqual(self.agent.waited, [])
        self.assertEqual((self.fixture.staff_state(), len(self.fixture.world.launches),
                          self.fixture.world.locations.copy()), before)
        self.assertEqual((self.run_dir / "progress_history_state.json").read_bytes(), pass_state)

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

    def test_list_keeps_dispatch_role_separate_from_observation(self) -> None:
        opened = self.opened("impl2", "review")
        self.fixture.dispatch(opened.row, "review", completed=True)
        code, output, errors = self.command("list", "--json")
        self.assertEqual(code, 0, errors)
        listing = cast(list[dict[str, object]], json.loads(output))
        self.assertEqual(listing[0]["name"], "impl2")
        self.assertEqual(listing[0]["role"], "review")
        self.assertEqual(listing[0]["state"], "OpenWorker")
        self.assertEqual(listing[0]["activity"], "idle")

    def test_list_scans_runs_once_and_prints_each_run_anomaly_once(self) -> None:
        unit_runs.report_anomaly.cache_clear()
        self.addCleanup(unit_runs.report_anomaly.cache_clear)
        rows = [self.fixture.worker(name) for name in ("impl", "review", "test")]
        for index, row in enumerate(rows, 1):
            self.fixture.world.add(row, f"w1:p{index}")
        runs = self.fixture.root / "unit-runs"
        for name in ("ownerless-a", "ownerless-b"):
            run = runs / name
            run.mkdir(parents=True)
            atomic_file.write_text_atomic(run / unit_runs.STATE_FILE, '{}')
        atomic_file.write_text_atomic(self.run_dir / unit_runs.STATE_FILE, json.dumps({"pass": {
            row.name: {"instance_id": "dispatch-" + row.name, "worker_name": row.name, "kind": row.name}
            for row in rows}}))
        with patch.object(unit_runs, "held_by_session", wraps=unit_runs.held_by_session) as scans:
            code, output, errors = self.command("list", "--json")
        self.assertEqual(code, 0, errors)
        self.assertEqual(scans.call_count, 1)
        listing = cast(list[dict[str, object]], json.loads(output))
        self.assertEqual({item["name"]: item["role"] for item in listing}, {row.name: row.name for row in rows})
        self.assertEqual(errors.splitlines(), [
            f"anomaly: {runs / name}: this run has no owner because {unit_runs.STATE_FILE} and mesh_roster.json name no unit, production or staff row"
            for name in ("ownerless-a", "ownerless-b")])

    def test_restore_retains_the_same_native_session_and_reruns_unchanged(self) -> None:
        opened = self.opened()
        self.fixture.dispatch(opened.row, "impl", completed=True)
        _ = self.fixture.world.stop(opened.row, self.fixture.world)
        self.assertEqual(worker.restore(self.fixture.parent.id), worker.ExitCode.OK)
        before = self.fixture.staff_state(), len(self.fixture.world.launches), self.fixture.world.locations.copy()
        code, output, errors = self.command("restore")
        self.assertEqual((code, output, errors), (0, "unchanged\n", ""))
        self.assertEqual((self.fixture.staff_state(), len(self.fixture.world.launches), self.fixture.world.locations.copy()), before)
        self.assertEqual(self.fixture.world.native_session(opened.pane, self.fixture.world.foreground[opened.pane][1], 1), opened.row.current_session)

    def test_restore_reports_the_unavailable_role_and_continues_other_workers(self) -> None:
        missing = self.opened("impl")
        retained = self.opened("review", "review")
        _ = self.fixture.world.stop(missing.row, self.fixture.world)
        _ = self.fixture.world.stop(retained.row, self.fixture.world)
        self.fixture.dispatch(retained.row, "review", completed=True)
        with redirect_stderr(io.StringIO()) as errors:
            code = worker.restore(self.fixture.parent.id)
        self.assertEqual(code, worker.ExitCode.REFUSED)
        self.assertIn("dispatch role unavailable", errors.getvalue())
        self.assertIn(retained.pane, self.fixture.world.foreground)
        self.assertNotIn(missing.pane, self.fixture.world.foreground)

    def test_end_keeps_a_busy_worker_and_reports_the_blocker(self) -> None:
        opened = self.opened()
        self.fixture.world.locations[opened.pane]["agent_status"] = "working"
        before = self.fixture.staff_state()
        code, output, errors = self.command("end", "impl")
        self.assertEqual((code, output), (7, ""))
        self.assertIn("working", errors)
        self.assertEqual(self.fixture.staff_state(), before)
        self.assertIn(opened.pane, self.fixture.world.foreground)

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
        self.assertEqual(self.command("check")[0], 0)
        _ = self.fixture.world.stop(opened.row, self.fixture.world)
        self.assertEqual(self.command("check")[0], 0)
        self.fixture.world.unknown[opened.pane] = "process evidence lost"
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
