"""Real verify entry point cancellation with private Cargo and Git fixtures."""
from __future__ import annotations

import subprocess
import unittest
import sys
from pathlib import Path

from scripts.direct.verify_tree_cancel_test_support import CANCEL_LINE, VerifyTreeCancelFixture
from scripts.direct.verify_token_wait_test_support import BOARD
from scripts.direct.wait_for_test_state import wait_until


class VerifyTreeCancelTests(VerifyTreeCancelFixture):
    def test_cancel_during_reacquisition_removes_own_queue_entry_and_keeps_peer(self) -> None:
        memory = Path(self.environment["BUILDLOG_MEMINFO"])
        _ = memory.write_text("MemAvailable: 0 kB\n")
        admission = self.root / "buildlog/admission"
        admission.mkdir(parents=True)
        process = self.start_verify("test", BUILDLOG_MEM_POLL_S="1", TEST_MEMORY_CHANGE_EVENT="1")
        lock = self.board / "locks/cargo.d"
        wait_until(lambda: (bool(list(admission.glob("*.waiting"))) and not lock.exists())
                   or process.poll() is not None, admission, timeout=60, processes=(process,))
        self.assertIsNone(process.poll(), "verify never entered memory admission")
        # The queue prunes live PIDs whose argv does not identify a verifier.
        # Advertise this fake peer before publishing its entry, so the count
        # loop cannot race cancellation and discard it as a stale caller.
        holder = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()", "verify.sh"],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, start_new_session=True)
        self.started_groups.add(holder.pid)
        self.addCleanup(self.finish_process, holder)
        taken = subprocess.run(["bash", str(BOARD), "acquire", str(self.board), "peer", "cargo",
                                "--pid", str(holder.pid), "--hold", "3600", "--wait", "0"],
                               capture_output=True, text=True, check=False, timeout=10)
        self.assertEqual(taken.returncode, 0, taken.stdout + taken.stderr)
        waiters = self.board / "token_waiters"
        waiters.mkdir(exist_ok=True)
        _ = memory.write_text("MemAvailable: 50331648 kB\n")
        wait_until(lambda: bool(list(waiters.glob(f"*.{process.pid}.again")))
                   or process.poll() is not None, waiters, timeout=60, processes=(process,))
        own_entries = list(waiters.glob(f"*.{process.pid}.again"))
        self.assertEqual(len(own_entries), 1, "fixture never reached subshell reacquisition")
        # Keep a peer's entry present too: cancellation must remove only its own files.
        peer_entry = waiters / f"peer.0123456789abcdef.9999999999.{holder.pid}.again"
        peer_entry.touch()
        self.edit_source()
        output, errors = process.communicate(timeout=30)
        self.assertEqual(process.returncode, 77, output + errors)
        self.assertEqual((output + errors).splitlines().count(CANCEL_LINE.format(cmd="test")), 1)
        self.assertEqual(sorted(waiters.iterdir()), [peer_entry], "cancel left its reacquisition entry")
        self.assertEqual((lock / "holder_pid").read_text(), str(holder.pid), "cancel released a peer's claim")
        self.assertIsNone(holder.poll(), "cancel killed its peer")
        self.assertEqual(list(admission.glob("*.reservation")), [])
        self.assertEqual(list(admission.glob("*.waiting")), [])
        self.assertEqual(list((self.root / "verify-steps").glob("*.json")), [])
        self.assert_no_cache_verdict()
        self.assertEqual([args for args in self.cargo_calls() if args[:2] == ["nextest", "run"]], [])
        records = self.records()
        self.assertEqual([(row.get("outcome"), row.get("status")) for row in records], [("cancelled", 77)])
        released = subprocess.run(["bash", str(BOARD), "release", str(self.board), "peer", "cargo",
                                   "--pid", str(holder.pid)], capture_output=True, text=True,
                                  check=False, timeout=10)
        self.assertEqual(released.returncode, 0, released.stdout + released.stderr)


if __name__ == "__main__":
    _ = unittest.main()
