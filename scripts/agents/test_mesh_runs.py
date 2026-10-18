"""Tests for mesh_runs.py: detached runs that message the worker who started them."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import NamedTuple, cast, override
from unittest.mock import MagicMock, patch

from scripts.agents import codex_mesh, mesh_runs

WORKER = "unit-impl"
DEAD_PID = 2 ** 22 + 12345


class DirectorNotice(NamedTuple):
    session_id: str
    summary: str
    text: str


class MeshRunsTests(unittest.TestCase):
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    session: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    stack: contextlib.ExitStack  # pyright: ignore[reportUninitializedInstanceVariable]
    sent: list[DirectorNotice]  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        self.stack = contextlib.ExitStack()
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.session = self.root / "unit" / "run-1"
        self.session.mkdir(parents=True)
        _ = self.stack.enter_context(patch.object(mesh_runs, "RUN_ROOTS", (self.root / "unit",)))
        self.stack.enter_context(patch.dict(os.environ, {"REVIEW_POOL_STATE": str(self.root / "pool")}))
        self.sent = []

        def send_to_director(session_id: str, summary: str, text: str) -> bool:
            self.sent.append(DirectorNotice(session_id, summary, text))
            return True

        _ = self.stack.enter_context(patch.object(mesh_runs, "send_to_director", send_to_director))
        self.write_roster("thread-1", "running", os.getpid(), port=4100)

    @override
    def tearDown(self) -> None:
        self.stack.close()

    def write_roster(self, thread_id: str, status: str, launcher_pid: int, port: int) -> None:
        roster = {WORKER: {"thread_id": thread_id, "status": status, "launcher_pid": launcher_pid,
                           "port": port, "turn_id": "turn-1"}}
        _ = (self.session / codex_mesh.ROSTER_FILE).write_text(json.dumps(roster), encoding="utf-8")

    def pending(self) -> dict[str, object]:
        path = self.session / f"{WORKER}.pending.json"
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        return cast(dict[str, object], json.loads(text)) if text.strip() else {}

    def record(self, command: list[str], **extra: str) -> Path:
        runs = self.session / codex_mesh.DETACHED_RUNS_DIR
        runs.mkdir(exist_ok=True)
        record: mesh_runs.RunRecord = {
            "run_id": "r1", "status": "running", "session_dir": str(self.session), "worker": WORKER,
            "thread_id": "thread-1", "label": "verify.sh check", "command": command, "cwd": str(self.root),
            "log": str(runs / "r1.log"), "started_at": time.time(), "pid": os.getpid(),
            "director_dir": str(self.session), "done_message_file": extra.get("done", ""),
        }
        path = runs / "r1.json"
        _ = path.write_text(json.dumps(record), encoding="utf-8")
        return path

    def supervise(self, path: Path) -> mesh_runs.RunRecord:
        self.assertEqual(mesh_runs.main(["supervise", str(path)]), 0)
        return cast(mesh_runs.RunRecord, json.loads(path.read_text(encoding="utf-8")))

    # Criterion 1: start returns at once with a detached supervisor and a running record.
    def test_start_detaches_a_supervisor_and_records_the_run(self) -> None:
        popen = MagicMock(return_value=SimpleNamespace(pid=4321))
        environment = {"CODEX_THREAD_ID": "thread-1", "CLAUDE_CODE_SESSION_ID": "director-session",
                       "PLAN_DELEGATE_TEAM_ROLE": "test"}
        with patch.dict(os.environ, environment), patch.object(subprocess, "Popen", popen), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            code = mesh_runs.main(["start", "--label", "verify.sh check", "--", "bash", "verify.sh", "check"])
        self.assertEqual(code, 0)
        kwargs = cast(dict[str, object], popen.call_args.kwargs)
        self.assertIs(kwargs["start_new_session"], True)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stderr"], subprocess.STDOUT)
        self.assertNotIn(kwargs["stdout"], (None, sys.stdout))
        records = list((self.session / codex_mesh.DETACHED_RUNS_DIR).glob("*.json"))
        self.assertEqual(len(records), 1)
        record = cast(dict[str, object], json.loads(records[0].read_text(encoding="utf-8")))
        self.assertEqual((record["status"], record["pid"], record["worker"], record["thread_id"]),
                         ("running", 4321, WORKER, "thread-1"))
        self.assertEqual(record["command"], ["bash", "verify.sh", "check"])
        self.assertNotIn("port", record)
        self.assertIn("next message", output.getvalue())

    def test_start_exits_3_for_a_caller_that_is_not_a_mesh_worker(self) -> None:
        popen = MagicMock()
        for environment in ({"CODEX_THREAD_ID": "thread-unknown"}, {"CODEX_THREAD_ID": ""}):
            with patch.dict(os.environ, environment), patch.object(subprocess, "Popen", popen):
                self.assertEqual(mesh_runs.main(["start", "--", "true"]), mesh_runs.NOT_A_MESH_WORKER)
        self.write_roster("thread-1", "ended", 0, port=4100)
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-1"}), patch.object(subprocess, "Popen", popen):
            self.assertEqual(mesh_runs.main(["start", "--", "true"]), mesh_runs.NOT_A_MESH_WORKER)
        popen.assert_not_called()

    # Criterion 2: the result is held for the seat, then the record is marked finished.
    def test_supervise_delivers_before_marking_finished(self) -> None:
        path = self.record([sys.executable, "-c", "import os; print('lint clean', os.environ['PLAN_DELEGATE_TEAM_ROLE'])"])
        hold = codex_mesh.hold_message
        statuses: list[object] = []

        def watched(session_dir: str, name: str, text: str, *, without_launcher: bool) -> codex_mesh.HoldOutcome:
            stored = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
            statuses.append(stored["status"])
            return hold(session_dir, name, text, without_launcher=without_launcher)

        with patch.object(codex_mesh, "hold_message", watched):
            record = self.supervise(path)
        self.assertEqual(statuses, ["running"])
        self.assertEqual((record["status"], record.get("exit_code")), ("finished", 0))
        messages = cast(list[str], self.pending()["messages"])
        self.assertEqual(len(messages), 1)
        self.assertIn("Run finished: verify.sh check\nResult: PASS (exit 0)", messages[0])
        self.assertIn("lint clean impl", messages[0])
        self.assertEqual(self.sent, [])

    def test_failure_and_done_message_rules(self) -> None:
        path = self.record([sys.executable, "-c", "raise SystemExit(4)"])
        record = self.supervise(path)
        self.assertIn("Result: FAIL (exit 4)", cast(list[str], self.pending()["messages"])[0])
        self.assertEqual(record.get("exit_code"), 4)
        done = self.root / "done.txt"
        _ = done.write_text("Stage 2: go", encoding="utf-8")
        (self.session / f"{WORKER}.pending.json").unlink()
        record = self.supervise(self.record([sys.executable, "-c", "raise SystemExit(1)"], done=str(done)))
        self.assertEqual(self.pending(), {})
        self.assertIn("nothing delivered", record.get("delivery", ""))
        _ = self.supervise(self.record([sys.executable, "-c", "pass"], done=str(done)))
        self.assertEqual(self.pending()["messages"], ["Stage 2: go"])

    def test_a_line_over_codexs_input_limit_delivers_cut_under_the_cap(self) -> None:
        script = "print('x' * 1_100_000)\nfor n in range(30): print(str(n) * 5_000)\nprint('last line')"
        _ = self.supervise(self.record([sys.executable, "-c", script]))
        message = cast(list[str], self.pending()["messages"])[0]
        self.assertLess(len(message), mesh_runs.RESULT_TAIL_CHARS + 1_000)
        self.assertIn(f"{'29' * 1_000} [cut: 8000 more characters]", message)
        self.assertTrue(message.endswith("\nlast line"))
        _ = self.supervise(self.record([sys.executable, "-c", "print('x' * 1_100_000)"]))
        message = cast(list[str], self.pending()["messages"])[-1]
        self.assertIn(f"{'x' * mesh_runs.RESULT_LINE_CHARS} [cut: 1098000 more characters]", message)
        self.assertLess(len(message), mesh_runs.RESULT_TAIL_CHARS)

    # Criterion 3: the server and thread change between start and finish.
    def test_result_reaches_the_seats_current_thread_after_a_server_change(self) -> None:
        path = self.record([sys.executable, "-c", "pass"])
        self.write_roster("thread-2", "running", os.getpid(), port=4200)
        _ = (self.session / codex_mesh.SERVER_FILE).write_text(json.dumps({"port": 4200, "pid": os.getpid()}))
        _ = self.supervise(path)
        self.assertEqual(self.pending()["thread_id"], "thread-2")
        self.assertEqual(len(cast(list[str], self.pending()["messages"])), 1)

    # Criterion 4: no launcher sends the result to the director; an attached launcher holds it.
    def test_no_launcher_sends_the_result_to_the_director_and_holds_nothing(self) -> None:
        active = self.root / "unit" / "active"
        active.mkdir()
        _ = (active / "director-session").write_text(f"{self.session}\n", encoding="utf-8")
        self.write_roster("thread-1", "done", DEAD_PID, port=4100)
        record = self.supervise(self.record([sys.executable, "-c", "raise SystemExit(2)"]))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0].session_id, "director-session")
        for part in (f"Seat: {WORKER}", "Command: ", "FAIL (exit 2)", "Log: "):
            self.assertIn(part, self.sent[0].text)
        self.assertEqual(self.pending(), {})
        self.assertIn("sent to director", record.get("delivery", ""))

    def test_closed_inbox_goes_to_the_director(self) -> None:
        active = self.root / "unit" / "active"
        active.mkdir()
        _ = (active / "director-session").write_text(f"{self.session}\n", encoding="utf-8")
        with codex_mesh._pending_file(str(self.session), WORKER) as handle:  # pyright: ignore[reportPrivateUsage]
            codex_mesh._write_pending(handle, "thread-1", [], accepting_messages=False)  # pyright: ignore[reportPrivateUsage]
        _ = self.supervise(self.record([sys.executable, "-c", "pass"]))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.pending()["messages"], [])

    def test_live_run_keeps_the_launcher_attached_until_the_result_is_held(self) -> None:
        session, thread = str(self.session), "thread-1"
        path = self.record([sys.executable, "-c", "pass"])
        self.assertFalse(codex_mesh._close_messages_if_empty(session, WORKER, thread))  # pyright: ignore[reportPrivateUsage]
        self.assertTrue(codex_mesh._waiting_on_detached_run(session, WORKER, thread))  # pyright: ignore[reportPrivateUsage]
        _ = self.supervise(path)
        self.assertFalse(codex_mesh._detached_run_live(session, WORKER))  # pyright: ignore[reportPrivateUsage]
        # The held result now opens the next turn: the launcher refuses to close on it.
        self.assertFalse(codex_mesh._close_messages_if_empty(session, WORKER, thread))  # pyright: ignore[reportPrivateUsage]
        self.assertFalse(codex_mesh._waiting_on_detached_run(session, WORKER, thread))  # pyright: ignore[reportPrivateUsage]
        (self.session / f"{WORKER}.pending.json").unlink()
        self.assertTrue(codex_mesh._close_messages_if_empty(session, WORKER, thread))  # pyright: ignore[reportPrivateUsage]

    def test_a_record_without_a_pid_counts_as_live_only_at_first(self) -> None:
        path = self.record(["true"])
        record = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
        record["pid"] = 0
        _ = path.write_text(json.dumps(record), encoding="utf-8")
        self.assertTrue(codex_mesh._detached_run_live(str(self.session), WORKER))  # pyright: ignore[reportPrivateUsage]
        record["started_at"] = time.time() - 120
        _ = path.write_text(json.dumps(record), encoding="utf-8")
        self.assertFalse(codex_mesh._detached_run_live(str(self.session), WORKER))  # pyright: ignore[reportPrivateUsage]

    def test_an_interrupted_turn_with_held_work_is_not_a_failure(self) -> None:
        session = str(self.session)
        params: dict[str, object] = {"turn": {"id": "turn-1", "status": "interrupted"}}
        check = codex_mesh._interrupted_for_held_work  # pyright: ignore[reportPrivateUsage]
        self.assertFalse(check("turn/completed", params, session, WORKER, "thread-1"))
        _ = codex_mesh.hold_message(session, WORKER, "next", without_launcher=True)
        self.assertTrue(check("turn/completed", params, session, WORKER, "thread-1"))
        failed: dict[str, object] = {"turn": {"id": "turn-1", "status": "interrupted", "error": {"message": "boom"}}}
        self.assertFalse(check("turn/completed", failed, session, WORKER, "thread-1"))

    def test_interrupt_holds_the_message_then_interrupts_the_turn(self) -> None:
        calls: list[tuple[str, dict[str, object]]] = []

        class FakeClient:
            def __init__(self, _port: int, _name: str) -> None:
                pass

            def call(self, method: str, params: dict[str, object], timeout: float = 0.0) -> codex_mesh.RpcMessage:
                del timeout
                calls.append((method, params))
                if method == "thread/read":
                    return {"result": {"thread": {"status": {"type": "active"},
                                                  "turns": [{"id": "turn-9", "status": "inProgress"}]}}}
                return {"result": {}}

            def close(self) -> None:
                pass

        with patch.object(codex_mesh, "Client", FakeClient), patch.object(codex_mesh, "_worker_port", return_value=1), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            code = codex_mesh.main(["interrupt", "--session-dir", str(self.session), "--to", WORKER,
                                    "--message", "Stop; the plan changed."])
        self.assertEqual(code, 0)
        self.assertEqual(self.pending()["messages"], ["Stop; the plan changed."])
        self.assertIn(("turn/interrupt", {"threadId": "thread-1", "turnId": "turn-9"}), calls)
        self.assertIn("interrupted", output.getvalue())


if __name__ == "__main__":
    _ = unittest.main()
