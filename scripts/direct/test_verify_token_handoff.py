"""Real verify.sh token handoff behavior."""
from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from scripts.direct.verify_token_wait_test_support import VerifyTokenWaitFixture, VERIFY, BOARD


class VerifyTokenHandoffTests(VerifyTokenWaitFixture):
    def test_a_memory_wait_gives_the_token_to_a_peer_and_takes_it_back_before_its_step(self) -> None:
        # Phases 15 and 16 of one unit share the session's board and its role
        # names. Phase 16 waits for memory without the token, so phase 15
        # builds meanwhile. Phase 16's step then waits for the token, and phase
        # 15's launcher ending cannot free it.
        board = self.root / "board"
        lock = board / "locks" / "cargo.d"
        meminfo = Path(self.environment["BUILDLOG_MEMINFO"])
        _ = meminfo.write_text("MemAvailable: 0 kB\n")
        peer_meminfo = self.root / "peer-meminfo"
        _ = peer_meminfo.write_text("MemAvailable: 50331648 kB\n")
        environment = {**self.environment, "UNIT_RUN_DIR": str(board),
                       "UNIT_TEAM_ROLE": "impl", "BUILDLOG_MEM_POLL_S": "1"}
        phase_16_errors = self.root / "phase-16.err"
        phase_16 = self.start_memory_waiter(environment, phase_16_errors)
        phase_15: subprocess.Popen[bytes] | None = None
        try:
            self.assertFalse(lock.exists(), "the memory wait kept the token")
            phase_15 = subprocess.Popen(["bash", str(VERIFY), "check", "sample", "--no-cache"],
                                        cwd=self.root,
                                        env={**environment, "BUILDLOG_MEMINFO": str(peer_meminfo),
                                             "TEST_BLOCK": "1"},
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                        start_new_session=True)
            self.started_groups.add(phase_15.pid)
            self.wait_for_running(self.root / "running", phase_15)
            self.assertEqual((lock / "holder_pid").read_text(), str(phase_15.pid))
            launcher_release = subprocess.run(["bash", str(BOARD), "release", str(board), "impl",
                                               "cargo"], capture_output=True, text=True,
                                              check=False, timeout=300)
            self.assertEqual(launcher_release.returncode, 1, launcher_release.stdout)
            self.assertIn(f"held by live pid {phase_15.pid}", launcher_release.stderr)

            _ = meminfo.write_text("MemAvailable: 50331648 kB\n")
            self.wait_for_output(phase_16_errors, "memory free after", phase_16)
            self.assertIsNone(phase_16.poll(), phase_16_errors.read_text())
            self.assertEqual((self.root / "cargo-calls").read_text().count("check"), 1)
            self.assertEqual((lock / "holder_pid").read_text(), str(phase_15.pid))

            _ = (self.root / "release").touch()
            self.assertEqual(phase_15.wait(timeout=300), 0)
            self.assertEqual(phase_16.wait(timeout=300), 0, phase_16_errors.read_text())
            self.assertNotIn("Traceback", phase_16_errors.read_text())
            self.assertEqual((self.root / "cargo-calls").read_text().count("check"), 2)
            self.assertFalse(lock.exists())
        finally:
            _ = meminfo.write_text("MemAvailable: 50331648 kB\n")
            _ = (self.root / "release").touch()
            for process in (phase_16, phase_15):
                if process is not None:
                    if process.poll() is None:
                        self.end_group(process.pid)
                    _ = process.wait(timeout=300)

    def test_a_hold_wait_gives_the_token_to_a_peer_and_takes_it_back_before_its_step(self) -> None:
        board = self.root / "board"
        lock = board / "locks" / "cargo.d"
        environment = self.hold_environment(board, 0)
        output = self.root / "held.out"
        held = self.start_hold_waiter(environment, output)
        peer: subprocess.Popen[bytes] | None = None
        try:
            self.assertFalse(lock.exists(), "the hold wait kept the token")
            peer_holds = self.root / "peer-holds"
            peer_holds.mkdir()
            peer = subprocess.Popen(["bash", str(VERIFY), "check", "sample", "--no-cache"],
                                    cwd=self.root,
                                    env={**environment, "BUILD_HOLD_DIR": str(peer_holds), "TEST_BLOCK": "1"},
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    start_new_session=True)
            self.started_groups.add(peer.pid)
            self.wait_for_running(self.root / "running", peer)
            self.assertEqual((lock / "holder_pid").read_text(), str(peer.pid))

            (self.root / "hold-release").touch()
            self.assertEqual((self.root / "cargo-calls").read_text().count("check"), 1)
            _ = (self.root / "release").touch()
            self.assertEqual(peer.wait(timeout=300), 0)
            self.assertEqual(held.wait(timeout=300), 0, output.read_text())
            self.assertEqual((self.root / "cargo-calls").read_text().count("check"), 2)
            self.assertFalse(lock.exists())
        finally:
            for marker in ("hold-release", "release"):
                (self.root / marker).touch()
            for process in (held, peer):
                if process is not None:
                    if process.poll() is None:
                        self.end_group(process.pid)
                    _ = process.wait(timeout=300)

    def test_a_hold_wait_that_leaves_for_the_mac_does_not_take_the_token_again(self) -> None:
        board = self.root / "board"
        environment = self.hold_environment(board, 75)
        output = self.root / "held.out"
        held = self.start_hold_waiter(environment, output)
        try:
            (self.root / "hold-release").touch()
            _ = held.wait(timeout=300)
        finally:
            if held.poll() is None:
                self.end_group(held.pid)
                _ = held.wait(timeout=300)
        log = (board / "board.log").read_text()
        self.assertEqual(log.count("token cargo acquired"), 1, log)
        self.assertFalse((board / "locks" / "cargo.d").exists())
        self.assertFalse((self.root / "cargo-calls").exists(), "the step ran on natedev")

    def test_no_hold_gives_nothing_back(self) -> None:
        board = self.root / "board"
        (self.root / "build-hold").mkdir()
        result = self.verify({**self.environment, "UNIT_RUN_DIR": str(board),
                              "UNIT_TEAM_ROLE": "impl"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        log = (board / "board.log").read_text()
        # The first take, then the memory gate's give-back and take; the hold wait adds none.
        self.assertEqual((log.count("token cargo acquired"), log.count("token cargo released")), (2, 2), log)



if __name__ == "__main__":
    _ = unittest.main()
