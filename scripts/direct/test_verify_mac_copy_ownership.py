"""Real verify.sh Mac copy ownership with disposable tools."""
from __future__ import annotations

import unittest
import subprocess
from typing import final
from scripts.direct.verify_mac_offload_test_support import VerifyMacOffloadFixture, VERIFY as VERIFY
import os
import signal
from scripts.direct.verify_mac_offload_test_support import processes_carrying, invoke_mac_claim
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


@final
class VerifyMacCopyOwnershipTests(VerifyMacOffloadFixture):
    def test_a_stopped_call_leaves_no_step_or_copy_running(self) -> None:
        process, copy = self.start_copy_call(copy={"TEST_COPY_WAIT": "1"})
        cargo = int((self.runner_state / "cargo-compiling").read_text())
        # The step runs in its own process group, so a kill of verify's group alone misses it.
        self.assertNotEqual(os.getpgid(cargo), process.pid)
        self.assertTrue({cargo, copy} <= processes_carrying(self.child_marker()))
        # Both folders carry the run's pid, and the copy folder sits in the hold folder, so
        # tmp_sweep.py can tie a folder a kill leaves behind to its run.
        holds = list((self.root / "tmp").glob(f"verify-hold.{process.pid}.*"))
        self.assertEqual(len(holds), 1, list((self.root / "tmp").iterdir()))
        self.assertEqual(len(list(holds[0].glob(f"verify-copy.{process.pid}.*"))), 1)
        self.stop_verify(process)
        self.assertEqual(processes_carrying(self.child_marker()), set())

    def test_natedev_finishing_first_stops_the_copy_and_its_result_stands(self) -> None:
        process, _ = self.start_copy_call(copy={"TEST_COPY_WAIT": "1"})
        _ = (self.runner_state / "cargo-release").write_text("release\n")
        output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertEqual((self.runner_state / "copy-signal").read_text(), str(signal.SIGTERM))
        self.assertFalse((self.runner_state / "cargo-stopped").exists())
        self.assertEqual(len(self.saved_results()), 1)
        self.assert_copy_record("natedev_won", "natedev")
        record = self.last_call()
        self.assertEqual((record["status"], record["mac"], record["mac_reason"]), (0, "declined", "busy"))

    def test_new_test_claim_makes_the_copy_give_way_and_natedev_continues(self) -> None:
        mac_test_script = str(VERIFY.parent.parent / "mac_test" / "mac_test.py")
        # No second copy after the first gives way: the next check is an hour off.
        self.environment["BUILD_TARGETS_POLL_S"] = "3600"
        process, copy = self.start_copy_call(copy={"TEST_COPY_WAIT": "1", "TEST_COPY_CLAIM": mac_test_script})

        def claim(*words: str) -> subprocess.CompletedProcess[str]:
            return invoke_mac_claim([*words, "--pid", str(os.getpid())],
                              self.environment, self.root)

        new_test = claim("claim", "--what", "nextest other", "--worktree", str(self.root))
        self.addCleanup(claim, "release")
        self.assertEqual(new_test.returncode, 0, new_test.stdout + new_test.stderr)
        self.assertIn("claimed", new_test.stdout)
        # The claim signals the copy; its handler records the signal some time later on a loaded machine.
        self.wait_for_path(self.runner_state / "copy-signal", (process,))
        self.assertEqual((self.runner_state / "copy-signal").read_text(), str(signal.SIGTERM))
        self.wait_for_reaped(copy, (process,))
        self.assertIsNone(process.poll())
        _ = (self.runner_state / "cargo-release").write_text("release\n")
        output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertFalse((self.runner_state / "cargo-stopped").exists())
        self.assertEqual(len(self.saved_results()), 1)
        self.assert_copy_record("gave_way", "natedev")
        self.assertEqual(claim("release").returncode, 0)


if __name__ == "__main__":
    _ = unittest.main()
