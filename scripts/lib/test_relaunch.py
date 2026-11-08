"""Exercise terminal relaunches using short-lived Python children and temporary state."""

from __future__ import annotations

import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import tempfile
import time
from typing import TypedDict, cast, override
import unittest
from unittest.mock import patch


# Hang guards allow for load 136 on natedev (2026-10-29).

from scripts.lib.process_group import send_process_group
from scripts.shutdown.test_support import ShutdownTestCase
from scripts.lib import relaunch
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS

SUPERVISOR = Path(__file__).with_name("relaunch.py")


class RelaunchObservation(TypedDict):
    parent: int
    process_group: int
    cwd: str
    marker: str
    removed: bool


class FinishedChild:
    def wait(self) -> int:
        return 23


class RelaunchTests(ShutdownTestCase):
    root: Path = Path()
    state: Path = Path()

    @override
    def setUp(self) -> None:
        super().setUp()
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.state = self.root / "state"
        (self.state / "relaunch").mkdir(parents=True)

    def start(self, child: str) -> subprocess.Popen[str]:
        process = subprocess.Popen(
            [sys.executable, str(SUPERVISOR), "run", "--", sys.executable, "-c", child],
            cwd=self.root, env={"PATH": os.defpath, "HOME": str(self.root),
                "SHUTDOWN_STATE_DIR": str(self.state), "REMOVE_ON_RELAUNCH": "old"},
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True,
        )
        self.addCleanup(self.reap, process)
        return process

    def reap(self, process: subprocess.Popen[str]) -> None:
        try:
            send_process_group(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)

    def wait_for_file(self, path: Path, process: subprocess.Popen[str]) -> None:
        deadline = time.monotonic() + TEST_HANG_GUARD_SECS
        while not path.exists():
            self.assertIsNone(process.poll(), "supervisor exited before its child became ready")
            if time.monotonic() >= deadline:
                self.fail(f"child did not create {path.name}")
            time.sleep(0.01)

    def waiting_child(self) -> str:
        return (
            "import os,time; from pathlib import Path; "
            "Path('ready.tmp').write_text(str(os.getppid())); os.replace('ready.tmp', 'ready'); "
            "\nwhile not Path('release').exists(): time.sleep(.01)\n"
            "raise SystemExit(7)"
        )

    def request(self, pid: int) -> Path:
        destination = self.root / "next cwd"
        destination.mkdir(exist_ok=True)
        output = self.root / "observation.json"
        child = (
            "import json,os; from pathlib import Path; "
            f"Path({str(output)!r}).write_text(json.dumps({{'parent':os.getppid(),"
            "'process_group':os.getpgrp(),"
            "'cwd':os.getcwd(),'marker':os.environ.get('RELAUNCH_MARKER',''),"
            "'removed':'REMOVE_ON_RELAUNCH' not in os.environ})); raise SystemExit(11)"
        )
        request = self.state / "relaunch" / f"{pid}.json"
        _ = request.write_text(json.dumps({"argv": [sys.executable, "-c", child],
            "cwd": str(destination), "environment": {"RELAUNCH_MARKER": "new account"},
            "unset": ["REMOVE_ON_RELAUNCH"], "written_at": "2026-10-14T18:22:35+00:00",
            "note": "restarting fixture on its target account"}), encoding="utf-8")
        return request

    def assert_relaunched(self, process: subprocess.Popen[str], request: Path) -> None:
        stdout, stderr = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, 11, stderr)
        observed = cast(RelaunchObservation, json.loads((self.root / "observation.json").read_text()))
        self.assertEqual(observed["parent"], process.pid)
        self.assertEqual(observed["process_group"], process.pid)
        self.assertEqual(observed["cwd"], str(self.root / "next cwd"))
        self.assertEqual(observed["marker"], "new account")
        self.assertTrue(observed["removed"])
        self.assertEqual(stdout.count("restarting fixture on its target account\n"), 1)
        self.assertFalse(request.exists(), "a consumed request could launch twice")

    def test_resume_message_names_busy_dispatch(self) -> None:
        from scripts.lib.resume_message import AccountMove, BusyPass, IdleWorker, PaneWorkerResume, ResumeWorkers, resume_message
        from scripts.production import staff

        workers = ResumeWorkers(panes=(PaneWorkerResume(staff.StaffId("busy-id"), BusyPass("dispatch-id")),
                                      PaneWorkerResume(staff.StaffId("idle-id"), IdleWorker())))
        text = resume_message("a shutdown", move=AccountMove("source", "destination"), workers=workers)
        self.assertIn("for messages kept while you were down, then continue from there; if you were waiting on the user, keep waiting.", text)
        self.assertIn("moved from source to destination", text)
        self.assertEqual(text.count("Worker busy-id: busy dispatch dispatch-id"), 1)
        self.assertEqual(text.count("Worker idle-id: idle"), 1)
        self.assertNotIn("implement.sh", resume_message("a shutdown"))

    def test_resume_message_lists_every_idle_director_worker(self) -> None:
        from scripts.lib.resume_message import IdleWorker, PaneWorkerResume, ResumeWorkers, resume_message
        from scripts.production import staff

        text = resume_message("a shutdown", workers=ResumeWorkers(panes=[
            PaneWorkerResume(staff.StaffId(name), IdleWorker()) for name in ("impl-id", "review-id")]))
        self.assertIn("You were stopped and resumed because a shutdown.", text)
        self.assertIn("for messages kept while you were down", text)
        for name in ("impl-id", "review-id"):
            self.assertEqual(text.count(f"Worker {name}: idle"), 1)
        self.assertNotIn("Continue pass", text)

    def test_director_note_keeps_the_saved_assignment_prompt_and_uses_one_receipt(self) -> None:
        import shlex
        from scripts.herdr.test_worker_panes import WorkerPaneTests
        from scripts.herdr import worker_panes
        from scripts.lib import atomic_file, unit_runs
        from scripts.lib.resume_message import resume_message
        from scripts.production import staff, worker, worker_admission

        fixture = WorkerPaneTests("runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        row, source, dispatch = fixture.resume_fixture()
        state_path = fixture.root / "run/progress_history_state.json"
        state = cast(dict[str, object], json.loads(state_path.read_text()))
        passes = cast(dict[str, dict[str, object]], state["pass"])
        command = ("bash", "assign.sh", "--role", "impl", "--prompt", str(fixture.root / "saved prompt.md"))
        passes[row.name]["launcher_command"] = list(command)
        for status in ("unsubmitted", "submitted", "done", "completed"):
            with self.subTest(status=status):
                passes[row.name]["status"] = "completed" if status == "completed" else "active"
                atomic_file.write_text_atomic(state_path, json.dumps(state))
                entry = {"type": "user", "message": {"role": "user",
                         "content": f"End your final reply with DONE {dispatch}."}}
                text = "" if status == "unsubmitted" else json.dumps(entry) + "\n"
                if status == "done":
                    text += json.dumps({"type": "assistant", "message": {"role": "assistant", "content": "DONE " + dispatch}}) + "\n"
                atomic_file.write_text_atomic(source, text)
                with (patch.object(unit_runs, "held", return_value=[str(fixture.root / "run")]),
                      patch.object(worker_panes, "dispatch_role", wraps=worker_panes.dispatch_role) as roles,
                      patch.object(worker_admission, "transcript_entries", wraps=worker_admission.transcript_entries) as reads):
                    note = resume_message("a shutdown", workers=worker.resume_workers(fixture.parent.id, staff.snapshot()))
                if status == "unsubmitted":
                    self.assertIn(shlex.join(command), note)
                    self.assertNotIn("resume-prompt.md", note)
                    self.assertNotIn("Continue pass", note)
                elif status == "submitted":
                    self.assertIn("Continue pass " + dispatch, note)
                    self.assertNotIn("assign.sh", note)
                else:
                    self.assertNotIn(str(row.id), note)
                    self.assertNotIn(dispatch, note)
                self.assertEqual(reads.call_count, int(status != "completed"))
                roles.assert_not_called()

    def test_resume_message_continues_only_the_recipients_own_pass(self) -> None:
        from scripts.lib.resume_message import AccountMove, BusyPass, resume_message

        text = resume_message("quota", move=AccountMove("source", "destination"), own_pass=BusyPass("own-dispatch"))
        self.assertIn("You were stopped and resumed because quota.", text)
        self.assertIn("moved from source to destination", text)
        self.assertIn("Continue pass own-dispatch in this conversation. Keep the same pass id.", text)
        self.assertNotIn("Worker ", text)
        self.assertEqual(text.count('<system-reminder source="script">'), 1)


    def test_child_environment_marks_supervision_without_changing_the_parent(self) -> None:
        program = self.root / ".local/bin/claude"
        program.parent.mkdir(parents=True)
        _ = program.write_text("#!/bin/sh\nexit 99\n")
        program.chmod(0o700)
        with (patch.dict(os.environ, {"HOME": str(self.root),
                  "SHUTDOWN_STATE_DIR": str(self.state), "PRESERVED_MARKER": "keep"}, clear=True),
              patch.object(subprocess, "Popen", return_value=FinishedChild()) as launch):
            self.assertEqual(relaunch.run(["claude", "--resume", "conversation-id"]), 23)
            self.assertNotIn("RELAUNCH_SUPERVISED", os.environ)
        launch.assert_called_once()
        self.assertEqual(launch.call_args.args[0], [str(program), "--resume", "conversation-id"])
        environment = cast(dict[str, str], launch.call_args.kwargs["env"])
        self.assertEqual(environment["RELAUNCH_SUPERVISED"], "1")
        self.assertEqual(environment["PRESERVED_MARKER"], "keep")

    def test_agent_launch_inside_a_staff_operation_removes_its_descriptor(self) -> None:
        from scripts.production import staff

        command = [sys.executable, "-c", "import os; print('STAFF_OPERATION_FD' in os.environ)"]
        shell = str(SUPERVISOR.parents[1] / "agents/models_config.sh")
        with (patch.dict(os.environ, {"HOME": str(self.root), "SHUTDOWN_STATE_DIR": str(self.state)}),
              staff.operation()):
            descriptor = os.environ["STAFF_OPERATION_FD"]
            for argv in ([sys.executable, str(SUPERVISOR), "run", "--", *command],
                         ["bash", "-c", 'source "$1"; models_agent_environment; shift; exec "$@"', "launch", shell, *command]):
                with self.subTest(launcher=argv[0]):
                    child = subprocess.run(argv, cwd=self.root, capture_output=True, text=True,
                                           timeout=TEST_HANG_GUARD_SECS, check=False)
                    self.assertEqual(child.returncode, 0, child.stderr)
                    self.assertEqual(child.stdout, "False\n")
            self.assertEqual(os.environ["STAFF_OPERATION_FD"], descriptor)

    def test_director_resume_without_a_pane_marker_names_its_workers(self) -> None:
        from scripts.production import staff
        from scripts.production.staff_test_support import fixture_member, fixture_worker
        from scripts.shutdown import pane_record

        with patch.dict(os.environ, {"HOME": str(self.root), "ROSTER_RUN_ROOT": str(self.root / "runs"),
                                     "UNIT_RUN_DIR": "", "PLAN_DELEGATE_BOARD_DIR": ""}):
            runner = fixture_member("showrunner", name="TEST-showrunner")
            director = fixture_member("unit", name="TEST-director", parent=runner.id,
                                      worktree=staff.PresentWorktree("natedev", self.root))
            workers = [fixture_worker(name, director.id) for name in ("impl", "review")]
            entry = pane_record.launch("w1:p2", director.current_session, str(self.root), {}, ["claude"])
            with patch.object(pane_record, "RECORD_DIR", self.root / "panes"):
                pane_record.write("agents", entry)
                arguments, _ = relaunch.restored_launch(["claude", "--resume", director.current_session],
                    {"HERDR_ENV": "1", "HERDR_SESSION": "agents", "HERDR_PANE_ID": "w1:p2"})
        prompt = arguments[-1]
        self.assertIn("You were stopped and resumed", prompt)
        for row in workers:
            self.assertIn(f"Worker {row.id}: idle", prompt)
        self.assertEqual(prompt.count('<system-reminder source="script">'), 1)

    def test_no_request_preserves_the_child_exit_code(self) -> None:
        process = self.start("raise SystemExit(23)")
        stdout, stderr = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, 23, stderr)
        self.assertEqual(stdout, "")

    def test_herdr_relaunch_restores_its_conversation_marks_and_one_resume_note(self) -> None:
        from scripts.shutdown import pane_record
        from scripts.shutdown.stop_resume import resume_message

        program = self.root / ".local/bin/claude"
        program.parent.mkdir(parents=True)
        output = self.root / "child.json"
        _ = program.write_text(f"#!{sys.executable}\nimport json,os,sys\n"
            + f"open({str(output)!r},'w').write(json.dumps({{'argv':sys.argv[1:],'marks':"
            + "{k:v for k,v in os.environ.items() if k.startswith('SHOWRUNNER_') or k in ('STAFF_ID','CLAUDE_CODE_SESSION_ID')}}))\n")
        program.chmod(0o700)
        environment = {"HOME": str(self.root), "PATH": os.defpath, "HERDR_ENV": "1",
            "HERDR_SESSION": "agents", "HERDR_PANE_ID": "w1:p2", "HERDR_BIN": "/nonexistent/herdr",
            "HERDR_PANE_RECORD_DIR": str(self.root / "panes"), "SHUTDOWN_STATE_DIR": str(self.state)}
        native = {"CLAUDE_CODE_SESSION_ID": "2d058b83-b9da-46fd-9e87-877cd3f50a52"}
        marks = {"SHOWRUNNER_UNIT": "demo", "SHOWRUNNER_UNIT_ID": "alpha", **native}
        retained = pane_record.launch("w1:p2", "2d058b83-b9da-46fd-9e87-877cd3f50a52", str(self.root), {}, ["claude"])
        retained["environment"] = marks
        with patch.object(pane_record, "RECORD_DIR", self.root / "panes"):
            pane_record.write("agents", retained)
        for identity, prompt in (("2d058b83-b9da-46fd-9e87-877cd3f50a52", []), ("2d058b83-b9da-46fd-9e87-877cd3f50a52", ["existing resume prompt"]),
                                 ("8b6ff791-f212-484b-828b-8970d9524043", []), ("2d058b83-b9da-46fd-9e87-877cd3f50a52", [])):
            with self.subTest(identity=identity, prompt=prompt):
                arguments = ["claude", "--remote-control", "alpha 2026-10-31", "--settings", "{}", "--resume", identity, *prompt]
                child = subprocess.run([sys.executable, str(SUPERVISOR), "run", "--", *arguments],
                    cwd=self.root, env=environment, capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(child.returncode, 0, child.stderr)
                observed = cast(dict[str, object], json.loads(output.read_text()))
                expected = arguments[1:]
                if identity == "2d058b83-b9da-46fd-9e87-877cd3f50a52" and not prompt:
                    expected = [*expected, resume_message("you were stopped and resumed")]
                self.assertEqual(observed["argv"], expected)
                self.assertEqual(observed["marks"], native if identity == "2d058b83-b9da-46fd-9e87-877cd3f50a52" else {})

    def test_explicit_move_account_replaces_the_saved_launch_account(self) -> None:
        from scripts.shutdown import pane_record
        from scripts.production import session_staff

        entry = pane_record.launch("w1:p2", "session", str(self.root),
            {"CLAUDE_CONFIG_DIR": str(self.root / ".claude2"), "ENABLE_TOOL_SEARCH": "true"}, ["claude"])
        environment = {"HERDR_ENV": "1", "HERDR_PANE_ID": "w1:p2", "RELAUNCH_PROMPT_DELIVERY": "pane",
            "CLAUDE_CONFIG_DIR": str(self.root / ".claude5"),
            "CLAUDE_CODE_REMOTE_MEMORY_DIR": str(self.root / ".claude5/memory")}
        with (patch.dict(os.environ, {"HOME": str(self.root)}),
              patch.object(pane_record, "matching_launch", return_value=entry),
              patch.object(session_staff, "record_membership", return_value=entry)):
            _, restored = relaunch.restored_launch(["claude", "--resume", "session"], environment)
        self.assertEqual(restored["CLAUDE_CONFIG_DIR"], environment["CLAUDE_CONFIG_DIR"])
        self.assertEqual(restored["CLAUDE_CODE_REMOTE_MEMORY_DIR"], environment["CLAUDE_CODE_REMOTE_MEMORY_DIR"])
        self.assertEqual(restored["ENABLE_TOOL_SEARCH"], "true")

    def test_pane_delivery_does_not_add_a_second_prompt(self) -> None:
        from scripts.shutdown import pane_record
        from scripts.production import session_staff

        entry = pane_record.launch("w1:p2", "session", str(self.root), {}, ["claude"])
        environment = {"HERDR_ENV": "1", "HERDR_PANE_ID": "w1:p2", "RELAUNCH_PROMPT_DELIVERY": "pane"}
        with (patch.object(pane_record, "matching_launch", return_value=entry),
              patch.object(session_staff, "record_membership", return_value=entry)):
            arguments, _ = relaunch.restored_launch(["claude", "--resume", "session"], environment)
        self.assertEqual(arguments, ["claude", "--resume", "session"])

    def test_enveloped_resume_needs_no_source_record(self) -> None:
        from scripts.shutdown import pane_record
        from scripts.production import session_staff
        from scripts.lib import script_prompts

        entry = pane_record.launch("w1:p2", "session", str(self.root), {}, ["claude"])
        with (patch.object(pane_record, "matching_launch", return_value=entry),
              patch.object(session_staff, "record_membership", return_value=entry),
              patch.object(script_prompts, "record", side_effect=AssertionError("recorded resume"))):
            arguments, _ = relaunch.restored_launch(["claude", "--resume", "session"],
                {"HERDR_ENV": "1", "HERDR_PANE_ID": "w1:p2"})
        self.assertTrue(arguments[-1].startswith('<system-reminder source="script">'))

    def test_staged_request_relaunches_with_same_parent_pid_and_new_environment(self) -> None:
        process = self.start(self.waiting_child())
        self.wait_for_file(self.root / "ready", process)
        self.assertEqual(int((self.root / "ready").read_text()), process.pid)
        request = self.request(process.pid)
        _ = (self.root / "release").touch()
        self.assert_relaunched(process, request)

    def test_hold_keeps_supervisor_alive_until_a_late_request_arrives(self) -> None:
        process = self.start(self.waiting_child())
        self.wait_for_file(self.root / "ready", process)
        hold = self.state / "relaunch" / f"{process.pid}.hold"
        hold.touch()
        (self.root / "release").touch()
        assert process.stdout is not None
        with selectors.DefaultSelector() as selector:
            _ = selector.register(process.stdout, selectors.EVENT_READ)
            self.assertTrue(selector.select(timeout=TEST_HANG_GUARD_SECS), "hold did not announce its wait")
            line = cast(str, process.stdout.readline())
        self.assertEqual(line, "claude stopped for a restart; waiting for it here\n")
        self.assertIsNone(process.poll())
        request = self.request(process.pid)
        self.assert_relaunched(process, request)

    def test_hold_expiry_frees_the_window_and_preserves_child_exit_code(self) -> None:
        process = self.start(self.waiting_child())
        self.wait_for_file(self.root / "ready", process)
        hold = self.state / "relaunch" / f"{process.pid}.hold"
        hold.touch()
        expires_soon = time.time() - 299
        os.utime(hold, (expires_soon, expires_soon))
        (self.root / "release").touch()
        stdout, stderr = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, 7, stderr)
        self.assertEqual(stdout.count("claude stopped for a restart; waiting for it here\n"), 1)
        self.assertIn("no restart came; this window is free\n", stdout)
        self.assertFalse(hold.exists())

    def test_terminal_interrupts_do_not_kill_the_supervisor(self) -> None:
        for terminal_signal in (signal.SIGINT, signal.SIGQUIT, signal.SIGTSTP):
            with self.subTest(signal=terminal_signal):
                (self.root / "ready").unlink(missing_ok=True)
                (self.root / "release").unlink(missing_ok=True)
                process = self.start(self.waiting_child())
                self.wait_for_file(self.root / "ready", process)
                os.kill(process.pid, terminal_signal)
                request = self.request(process.pid)
                (self.root / "release").touch()
                self.assert_relaunched(process, request)

    def test_termination_and_hangup_are_forwarded_to_the_child(self) -> None:
        for forwarded in (signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=forwarded):
                (self.root / "ready").unlink(missing_ok=True)
                received = self.root / "received"
                received.unlink(missing_ok=True)
                child = (
                    "import signal,time; from pathlib import Path\n"
                    "def received(number,frame):\n"
                    " Path('received').write_text(str(number)); raise SystemExit(19)\n"
                    "signal.signal(signal.SIGTERM,received); signal.signal(signal.SIGHUP,received)\n"
                    "Path('ready').touch()\nwhile True: time.sleep(.01)\n"
                )
                process = self.start(child)
                self.wait_for_file(self.root / "ready", process)
                os.kill(process.pid, forwarded)
                _, stderr = process.communicate(timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(process.returncode, 19, stderr)
                self.assertEqual(received.read_text(), str(forwarded))

    def test_terminal_group_interrupt_reaches_child_and_supervisor_can_relaunch(self) -> None:
        child = (
            "import os,signal,time; from pathlib import Path\n"
            "def interrupted(number,frame):\n"
            " Path('interrupted').write_text(str(number)); raise SystemExit(17)\n"
            "signal.signal(signal.SIGINT,interrupted)\n"
            "Path('ready.tmp').write_text(str(os.getpgrp())); os.replace('ready.tmp', 'ready')\n"
            "while True: time.sleep(.01)\n"
        )
        process = self.start(child)
        self.wait_for_file(self.root / "ready", process)
        self.assertEqual(int((self.root / "ready").read_text()), process.pid)
        request = self.request(process.pid)
        send_process_group(process.pid, signal.SIGINT)
        self.assert_relaunched(process, request)
        self.assertEqual((self.root / "interrupted").read_text(), str(signal.SIGINT))

    def test_resume_message_keeps_busy_and_idle_claude_children(self) -> None:
            from scripts.lib.resume_message import Idle, Resumable, ResumeWorkers, resume_message
            from scripts.lib.resume_message import ClaudeChildResume

            seats = [ClaudeChildResume(run_dir=str(self.root), seat=name, role="impl", staff_id=name,
                                turn_live=busy, launcher_command=[]) for name, busy in (("busy", True), ("idle", False))]
            with patch("scripts.lib.resume_message.resume_worker_command", return_value="saved assignment") as command:
                text = resume_message("a shutdown", workers=ResumeWorkers(results=[Resumable(seats[0]), Idle(seats[1])], cwd=str(self.root)))
            command.assert_called_once_with(seats[0], str(self.root))
            self.assertIn("busy (impl, was working): saved assignment", text)
            self.assertIn("idle (impl, idle): paused on its thread", text)
            self.assertIn("for messages kept while you were down", text)


if __name__ == "__main__":
    _ = unittest.main()
