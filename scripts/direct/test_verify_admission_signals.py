"""Stop a real doc call during a memory wait or a service outage."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import unittest

from scripts.build_queue.admission_test_support import caller_admission
from scripts.build_queue import admission_protocol as wire
from scripts.direct.verify_doc_test_support import VERIFY, VerifyDocFixture
from scripts.direct.verify_mac_offload_test_support import processes_carrying, reap
from scripts.direct.wait_for_test_state import wait_until


CONNECT_OBSERVER = '''import os, runpy, sys
from pathlib import Path
if len(sys.argv) > 1 and sys.argv[1].endswith("verify_helper.py"):
    def connected(event, arguments):
        if event == "socket.connect" and arguments[1] == os.environ.get("ADMISSION_SOCKET"):
            Path(os.environ["TEST_ADMISSION_CONNECT"]).touch()
    sys.addaudithook(connected)
    sys.argv = sys.argv[1:]
    runpy.run_path(sys.argv[0], run_name="__main__")
else:
    os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
'''


class VerifyAdmissionSignalsTests(VerifyDocFixture):
    def test_sigterm_ends_admission_and_reaps_the_doc_step(self) -> None:
        admission = caller_admission(self, self.root, self.environment)
        _ = admission.occupy_host()
        observer = self.root / "observe-connect"
        self.executable(observer, CONNECT_OBSERVER)
        for unavailable in (False, True):
            with self.subTest(unavailable=unavailable):
                marker = f"TEST_ADMISSION_SIGNAL_CASE={self.root}:{unavailable}"
                ready = self.root / f"admission-ready-{unavailable}"
                environment = {**self.environment, "TEST_ADMISSION_SIGNAL_CASE": marker.split("=", 1)[1],
                    "CLAUDE_PYTHON": str(observer), "TEST_ADMISSION_CONNECT": str(ready)}

                def observed_wait(reply: wire.Reply) -> wire.Reply:
                    if isinstance(reply, wire.PlacementWait) and isinstance(reply.wait, wire.Wait):
                        ready.touch()
                    return reply

                if unavailable:
                    environment["ADMISSION_SOCKET"] = str(admission.socket_dir / "absent.sock")
                else:
                    admission.server.reply_filter = observed_wait
                    _ = Path(environment["BUILDLOG_MEMINFO"]).write_text(
                        "MemTotal: 67108864 kB\nMemAvailable: 0 kB\n")
                process = subprocess.Popen(["bash", str(VERIFY), "doc"], cwd=self.repo,
                                           env=environment, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, text=True, start_new_session=True)
                self.addCleanup(process.wait, timeout=5)
                self.addCleanup(reap, marker)
                wait_until(ready.exists, self.root, processes=(process,))
                process.terminate()
                output, errors = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 143, output + errors)
                self.assertEqual(processes_carrying(marker), set())
                self.assertEqual(self.cargo_calls() if self.events.exists() else [], [])
                self.assertNotEqual(process.pid, os.getpid())


if __name__ == "__main__":
    _ = unittest.main()
