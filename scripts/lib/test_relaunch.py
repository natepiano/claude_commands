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
TEST_HANG_GUARD_SECS = 300

sys.path.insert(0, str(Path(__file__).parents[1] / "shutdown"))
sys.path.insert(0, str(Path(__file__).parents[2]))
from scripts.shutdown.test_support import ShutdownTestCase
from scripts.lib import relaunch

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
            os.killpg(process.pid, signal.SIGKILL)
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

    def test_no_request_preserves_the_child_exit_code(self) -> None:
        process = self.start("raise SystemExit(23)")
        stdout, stderr = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, 23, stderr)
        self.assertEqual(stdout, "")

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
        os.killpg(process.pid, signal.SIGINT)
        self.assert_relaunched(process, request)
        self.assertEqual((self.root / "interrupted").read_text(), str(signal.SIGINT))


if __name__ == "__main__":
    _ = unittest.main()
