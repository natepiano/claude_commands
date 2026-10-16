"""Exercise detached readiness and cleanup with disposable Python children."""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
from typing import cast, override
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.lib import detached


class DetachedTests(unittest.TestCase):
    root: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()

    def stop(self, pid: int) -> None:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            _ = os.waitpid(pid, 0)
        except ChildProcessError:
            pass

    def test_ready_child_has_private_session_null_input_environment_and_appended_output(self) -> None:
        ready = self.root / "ready"
        log = self.root / "child.log"
        _ = log.write_text("previous output\n")
        code = (
            "import json,os,sys,time; from pathlib import Path; "
            "print('stdout',flush=True); print('stderr',file=sys.stderr,flush=True); "
            f"Path({str(ready.with_suffix('.tmp'))!r}).write_text(json.dumps([os.getpid(),os.getpgrp(),os.getsid(0),"
            "sys.stdin.read(),os.environ['DETACHED_MARKER'],'DETACHED_PARENT_ONLY' in os.environ])); "
            f"os.replace({str(ready.with_suffix('.tmp'))!r},{str(ready)!r}); time.sleep(30)"
        )
        with patch.dict(os.environ, {"DETACHED_PARENT_ONLY": "not passed"}):
            pid = detached.start_detached([sys.executable, "-c", code], ready=ready.exists,
                within=10, output=detached.AppendLog(log),
                environment=detached.ExplicitEnvironment({"DETACHED_MARKER": "kept"}))
        self.addCleanup(self.stop, pid)
        self.assertEqual(cast(object, json.loads(ready.read_text())), [pid, pid, pid, "", "kept", False])
        os.kill(pid, 0)
        self.assertEqual(log.read_text(), "previous output\nstdout\nstderr\n")

    def test_discarded_output_and_inherited_environment_still_allow_readiness(self) -> None:
        ready = self.root / "ready"
        code = ("import json,os,stat,sys,time; from pathlib import Path; "
                "print('discarded',flush=True); print('discarded error',file=sys.stderr,flush=True); "
                f"Path({str(ready)!r}).write_text(json.dumps([os.environ['DETACHED_MARKER'],"
                "all(stat.S_ISCHR(os.fstat(fd).st_mode) and os.fstat(fd).st_rdev == os.stat(os.devnull).st_rdev "
                "for fd in (0,1,2))])); time.sleep(30)")
        with patch.dict(os.environ, {"DETACHED_MARKER": "inherited"}):
            pid = detached.start_detached([sys.executable, "-c", code], ready=ready.exists,
                within=10, output=detached.DiscardOutput(), environment=detached.InheritEnvironment())
        self.addCleanup(self.stop, pid)
        self.assertEqual(cast(object, json.loads(ready.read_text())), ["inherited", True])

    def test_child_exit_reports_exit_status_instead_of_waiting_for_readiness(self) -> None:
        with self.assertRaisesRegex(detached.DetachedStartFailure, "did not start: exited with status 23"):
            _ = detached.start_detached([sys.executable, "-c", "raise SystemExit(23)"],
                                       ready=lambda: False, within=10)

    def test_timeout_terminates_the_private_group_including_grandchild(self) -> None:
        ready = self.root / "group"
        code = (
            "import os,signal,time; from pathlib import Path\n"
            "child=os.fork()\n"
            "def stop(number,frame):\n"
            f" Path({str(self.root)!r},str(os.getpid())+'.stopped').touch()\n"
            " if child: os.waitpid(child,0)\n"
            " raise SystemExit(0)\n"
            "signal.signal(signal.SIGTERM,stop)\n"
            "if child:\n"
            f" Path({str(ready.with_suffix('.tmp'))!r}).write_text(str(os.getpid())+' '+str(child))\n"
            f" os.replace({str(ready.with_suffix('.tmp'))!r},{str(ready)!r})\n"
            "else:\n"
            f" Path({str(self.root / 'grandchild-ready')!r}).touch()\n"
            "while True: time.sleep(.01)\n"
        )
        # A logical clock keeps the startup budget deterministic under machine load.
        # The real readiness callback waits for both processes before expiring it.
        elapsed = 0.0
        real_clock = time.monotonic
        real_deadline = real_clock() + 10

        def not_ready() -> bool:
            nonlocal elapsed
            if (ready.exists() and (self.root / "grandchild-ready").exists()) or real_clock() >= real_deadline:
                elapsed = 1.0
            return False

        with (patch.object(time, "monotonic", side_effect=lambda: elapsed),
              self.assertRaisesRegex(detached.DetachedStartFailure, "no readiness within 1 seconds")):
            _ = detached.start_detached([sys.executable, "-c", code], ready=not_ready, within=1)
        pids = [int(value) for value in ready.read_text().split()]
        for pid in pids:
            self.assertTrue((self.root / f"{pid}.stopped").exists(), f"group member {pid} missed termination")
        with self.assertRaises(ChildProcessError):
            _ = os.waitpid(pids[0], os.WNOHANG)

    def test_unstartable_program_has_plain_failure(self) -> None:
        program = str(self.root / "missing program")
        with self.assertRaisesRegex(detached.DetachedStartFailure, "missing program did not start"):
            _ = detached.start_detached([program], ready=lambda: False, within=1)

    def test_readiness_cannot_accept_a_child_that_exits_during_the_probe(self) -> None:
        ready = self.root / "pid"
        code = f"import os; from pathlib import Path; Path({str(ready)!r}).write_text(str(os.getpid()))"

        def stale_readiness() -> bool:
            if not ready.exists() or not ready.read_text():
                return False
            _ = os.waitpid(int(ready.read_text()), 0)
            return True

        with self.assertRaisesRegex(detached.DetachedStartFailure, "exited with status 0"):
            _ = detached.start_detached([sys.executable, "-c", code], ready=stale_readiness, within=10)


if __name__ == "__main__":
    _ = unittest.main()
