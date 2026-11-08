"""Stop a real doc call during a memory wait or a service outage."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import unittest

from scripts.build_queue.admission_test_support import caller_admission
from scripts.direct.verify_doc_test_support import VERIFY, VerifyDocFixture
from scripts.direct.verify_mac_offload_test_support import processes_carrying, reap
from scripts.direct.wait_for_test_state import wait_until


class VerifyAdmissionSignalsTests(VerifyDocFixture):
    def test_sigterm_ends_admission_and_reaps_the_doc_step(self) -> None:
        admission = caller_admission(self, self.root, self.environment)
        for unavailable in (False, True):
            with self.subTest(unavailable=unavailable):
                marker = f"TEST_ADMISSION_SIGNAL_CASE={self.root}:{unavailable}"
                self.addCleanup(reap, marker)
                environment = {**self.environment, "TEST_ADMISSION_SIGNAL_CASE": marker.split("=", 1)[1]}
                if unavailable:
                    environment["ADMISSION_SOCKET"] = str(admission.socket_dir / "absent.sock")
                else:
                    _ = Path(environment["BUILDLOG_MEMINFO"]).write_text(
                        "MemTotal: 67108864 kB\nMemAvailable: 0 kB\n")
                process = subprocess.Popen(["bash", str(VERIFY), "doc"], cwd=self.repo,
                                           env=environment, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, text=True, start_new_session=True)
                self.addCleanup(process.wait, timeout=5)

                def client_started() -> bool:
                    for pid in processes_carrying(marker):
                        try:
                            command = Path(f"/proc/{pid}/cmdline").read_bytes()
                        except OSError:
                            continue
                        if b"admission_client.py" in command:
                            return True
                    return False

                wait_until(client_started, self.root, processes=(process,))
                process.terminate()
                output, errors = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 143, output + errors)
                self.assertEqual(processes_carrying(marker), set())
                self.assertEqual(self.cargo_calls() if self.events.exists() else [], [])
                self.assertNotEqual(process.pid, os.getpid())


if __name__ == "__main__":
    _ = unittest.main()
