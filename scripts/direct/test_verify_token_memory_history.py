"""Real verify.sh token memory history behavior."""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import unittest
from pathlib import Path
from unittest import mock
from scripts.build_queue import admission_protocol as wire, admission_service as service
from scripts.build_queue.admission_test_support import caller_admission
from scripts.direct.wait_for_test_state import wait_until
from scripts.direct.verify_token_wait_test_support import VerifyTokenWaitFixture, VERIFY, BOARD
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


class VerifyTokenMemoryHistoryTests(VerifyTokenWaitFixture):
    def test_a_memory_wait_gives_the_token_to_a_peer_and_takes_it_back_before_its_step(self) -> None:
        # The memory waiter keeps the token until its step ends.
        board = self.root / "board"
        self.record_phase(board)
        lock = board / "locks" / "cargo.d"
        meminfo = Path(self.environment["BUILDLOG_MEMINFO"])
        _ = meminfo.write_text("MemAvailable: 0 kB\n")
        environment = {**self.environment, "UNIT_RUN_DIR": str(board),
                       "UNIT_TEAM_ROLE": "impl", "BUILDLOG_MEM_POLL_S": "1"}
        phase_16_errors = self.root / "phase-16.err"
        phase_16 = self.start_memory_waiter({**environment, "TEST_BLOCK": "1"}, phase_16_errors)
        resume_waiter = self.defer_memory_waiter_for_peer()
        phase_15: subprocess.Popen[bytes] | None = None
        try:
            self.assertEqual((lock / "holder_pid").read_text(), str(phase_16.pid))
            peer_errors = self.root / "phase-15.err"
            with peer_errors.open("wb") as stream:
                phase_15 = subprocess.Popen(["bash", str(VERIFY), "check", "sample", "--no-cache"],
                                            cwd=self.root,
                                            env={**environment, "TEST_BLOCK": "1"},
                                            stdout=stream, stderr=subprocess.STDOUT,
                                            start_new_session=True)
            self.started_groups.add(phase_15.pid)
            self.wait_for_output(peer_errors, "waiting for the cargo token since", phase_15)
            self.assertEqual((lock / "holder_pid").read_text(), str(phase_16.pid))
            launcher_release = subprocess.run(["bash", str(BOARD), "release", str(board), "impl",
                                               "cargo"], capture_output=True, text=True,
                                              check=False, timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(launcher_release.returncode, 1, launcher_release.stdout)
            self.assertIn(f"held by live pid {phase_16.pid}", launcher_release.stderr)

            resume_waiter()
            self.wait_for_running(self.root / "running", phase_16)
            self.assertIsNone(phase_16.poll(), phase_16_errors.read_text())
            self.assertEqual((self.root / "cargo-calls").read_text().count("check"), 1)
            self.assertEqual((lock / "holder_pid").read_text(), str(phase_16.pid))

            _ = (self.root / "release").touch()
            self.assertEqual(phase_15.wait(timeout=TEST_HANG_GUARD_SECS), 0)
            self.assertEqual(phase_16.wait(timeout=TEST_HANG_GUARD_SECS), 0, phase_16_errors.read_text())
            self.assertNotIn("Traceback", phase_16_errors.read_text())
            self.assertEqual((self.root / "cargo-calls").read_text().count("check"), 2)
            self.assertFalse(lock.exists())
            starts = [row for row in self.phase_waits(board) if row["event_type"] == "wait_started"]
            ends = [row for row in self.phase_waits(board) if row["event_type"] == "wait_finished"]
            self.assertIn("memory admission", {row["wait_reason"] for row in starts})
            self.assertEqual({row["wait_id"] for row in starts}, {row["wait_id"] for row in ends})
        finally:
            _ = meminfo.write_text("MemAvailable: 50331648 kB\n")
            _ = (self.root / "release").touch()
            for process in (phase_16, phase_15):
                if process is not None:
                    if process.poll() is None:
                        self.end_group(process.pid)
                    _ = process.wait(timeout=TEST_HANG_GUARD_SECS)

    def test_memory_wait_renews_the_token_after_its_original_lease_ends(self) -> None:
        board = self.root / "board"
        clock = self.root / "board-clock"
        _ = clock.write_text("100")
        shell = self.root / "board-clock.sh"
        _ = shell.write_text('export UNIT_NOW_EPOCH="$(< "$TEST_BOARD_CLOCK")"\n')
        admission = caller_admission(self, self.root, self.environment)
        _ = Path(self.environment["BUILDLOG_MEMINFO"]).write_text("MemAvailable: 0 kB\n")
        errors = self.root / "renew.err"
        process = self.start_memory_waiter({**self.environment, "UNIT_RUN_DIR": str(board),
            "UNIT_TEAM_ROLE": "impl", "BASH_ENV": str(shell), "TEST_BOARD_CLOCK": str(clock)}, errors)
        lock = board / "locks/cargo.d"
        _ = clock.write_text("3701")
        wait_until(lambda: int((lock / "expires").read_text() or "0") > 3701,
                   lock, processes=(process,))
        peer = subprocess.run(["bash", str(BOARD), "acquire", str(board), "peer", "cargo",
            "--pid", str(os.getpid()), "--wait", "0"], env={**self.environment, "UNIT_NOW_EPOCH": "3701"},
            capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(peer.returncode, 1, peer.stdout + peer.stderr)
        self.assertEqual((lock / "holder_pid").read_text(), str(process.pid))
        admission.free(48 * 1024 ** 3)
        self.assertEqual(process.wait(timeout=TEST_HANG_GUARD_SECS), 0, errors.read_text())
        self.assertEqual((self.root / "cargo-calls").read_text().count("check"), 1)

    def test_cancel_during_memory_wait_releases_token_and_waiter(self) -> None:
        board = self.root / "board"
        admission = caller_admission(self, self.root, self.environment)
        _ = Path(self.environment["BUILDLOG_MEMINFO"]).write_text("MemAvailable: 0 kB\n")
        process = self.start_memory_waiter({**self.environment, "UNIT_RUN_DIR": str(board),
            "UNIT_TEAM_ROLE": "impl"}, self.root / "cancel.err")
        os.kill(process.pid, signal.SIGTERM)
        self.assertNotEqual(process.wait(timeout=TEST_HANG_GUARD_SECS), 0)

        wait_until(admission.callers_released, admission.state_dir)
        self.assertFalse((board / "locks/cargo.d").exists())
        self.assertEqual(self.live_group_processes(process.pid), [])
        self.assertFalse((self.root / "cargo-calls").exists())

    def test_connection_wait_renews_the_token_before_the_first_reply(self) -> None:
        admission = caller_admission(self, self.root, self.environment)
        admission.configure(unreachable_limit_seconds=12)
        board = self.root / "board"
        clock = self.root / "board-clock"
        _ = clock.write_text("100")
        shell = self.root / "board-clock.sh"
        _ = shell.write_text('export UNIT_NOW_EPOCH="$(< "$TEST_BOARD_CLOCK")"\n')
        hidden = admission.socket_path.with_suffix(".unavailable")
        handle_request = admission.server.handle_request
        interrupted = False

        def interrupt_first_placement(request: wire.Request,
                connection: service.Connection | service.NoConnection = service.NO_CONNECTION) -> list[wire.Reply]:
            nonlocal interrupted
            if isinstance(request, wire.PlacementRequest) and not interrupted:
                interrupted = True
                _ = admission.socket_path.rename(hidden)
                assert isinstance(connection, service.Connection)
                connection.socket.shutdown(socket.SHUT_RDWR)
                return []
            return handle_request(request, connection)

        _ = self.enterContext(mock.patch.object(admission.server, "handle_request", side_effect=interrupt_first_placement))
        errors = self.root / "connection.err"
        with errors.open("wb") as stream:
            process = subprocess.Popen(["bash", str(VERIFY), "check", "sample", "--local", "connection renewal fixture", "--no-cache"], cwd=self.root,
                env={**self.environment, "UNIT_RUN_DIR": str(board), "UNIT_TEAM_ROLE": "impl",
                     "BASH_ENV": str(shell), "TEST_BOARD_CLOCK": str(clock)},
                stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        self.started_groups.add(process.pid)
        try:
            self.wait_for_output(errors, "build queue not reachable; trying for", process)
            with admission.loop_turn():
                self.assertEqual(admission.server.waiters, {})
                self.assertEqual(admission.server.reservations, {})
            lock = board / "locks/cargo.d"
            _ = clock.write_text("3701")
            wait_until(lambda: int((lock / "expires").read_text() or "0") > 3701,
                       lock, processes=(process,))
            peer = subprocess.run(["bash", str(BOARD), "acquire", str(board), "peer", "cargo",
                "--pid", str(os.getpid()), "--wait", "0"],
                env={**self.environment, "UNIT_NOW_EPOCH": "3701"},
                capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(peer.returncode, 1, peer.stdout + peer.stderr)
            self.assertEqual((lock / "holder_pid").read_text(), str(process.pid))
            _ = hidden.rename(admission.socket_path)
            self.assertEqual(process.wait(timeout=TEST_HANG_GUARD_SECS), 0, errors.read_text())
            self.assertEqual((self.root / "cargo-calls").read_text().count("check"), 1)
        finally:
            if hidden.exists():
                _ = hidden.rename(admission.socket_path)

    def test_unreachable_queue_stops_and_releases_the_token_without_running_cargo(self) -> None:
        admission = caller_admission(self, self.root, self.environment)
        admission.configure(unreachable_limit_seconds=0.2)
        board = self.root / "board"
        hidden = admission.socket_path.with_suffix(".unavailable")
        _ = admission.socket_path.rename(hidden)
        errors = self.root / "unreachable.err"
        try:
            with errors.open("wb") as stream:
                process = subprocess.Popen(["bash", str(VERIFY), "check", "sample", "--no-cache"], cwd=self.root,
                    env={**self.environment, "UNIT_RUN_DIR": str(board), "UNIT_TEAM_ROLE": "impl"},
                    stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
            self.started_groups.add(process.pid)
            self.assertEqual(process.wait(timeout=TEST_HANG_GUARD_SECS), 2, errors.read_text())
            lines = errors.read_text().splitlines()
            self.assertEqual(lines.count("build queue not reachable; trying for 0.2 s"), 1)
            self.assertEqual(lines.count("build queue unreachable for 0.2 s; step stopped; cargo token given back"), 1)
            self.assertFalse((board / "locks/cargo.d").exists())
            self.assertEqual(list((board / "token_waiters").glob(f"*.{process.pid}*")), [])
            self.assertFalse((self.root / "cargo-calls").exists())
            with admission.loop_turn():
                self.assertEqual(admission.server.waiters, {})
                self.assertEqual(admission.server.reservations, {})
        finally:
            _ = hidden.rename(admission.socket_path)

    def test_unreachable_estimate_refresh_releases_the_token_without_running_cargo(self) -> None:
        admission = caller_admission(self, self.root, self.environment)
        admission.configure(unreachable_limit_seconds=0.2)
        environment = self.build_stub([{"name": "sample-app", "kind": ["bin"]}])
        board = self.root / "board"
        hidden = admission.socket_path.with_suffix(".unavailable")
        handle_request = admission.server.handle_request
        generations = 0

        def disconnect_refresh(request: wire.Request,
                               connection: service.Connection | service.NoConnection = service.NO_CONNECTION
                               ) -> list[wire.Reply]:
            nonlocal generations
            if isinstance(request, wire.BuildHistoryRequest):
                generations += 1
                if generations == 2:
                    _ = admission.socket_path.rename(hidden)
                    assert isinstance(connection, service.Connection)
                    connection.socket.shutdown(socket.SHUT_RDWR)
                    return []
            elif isinstance(request, (wire.AdmitRequest, wire.PlacementRequest)):
                admission.server.build_history_generation += 1
            return handle_request(request, connection)

        errors = self.root / "refresh-unreachable.err"
        try:
            with mock.patch.object(admission.server, "handle_request", side_effect=disconnect_refresh), \
                    errors.open("wb") as stream:
                process = subprocess.Popen(["bash", str(VERIFY), "build", "sample"], cwd=self.root,
                    env=environment, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
                self.started_groups.add(process.pid)
                self.assertEqual(process.wait(timeout=TEST_HANG_GUARD_SECS), 2, errors.read_text())
            self.assertEqual(generations, 2)
            lines = errors.read_text().splitlines()
            self.assertEqual(lines.count("build queue not reachable; trying for 0.2 s"), 1)
            self.assertEqual(lines.count("build queue unreachable for 0.2 s; step stopped; cargo token given back"), 1)
            self.assertFalse((board / "locks/cargo.d").exists())
            self.assertEqual(list((board / "token_waiters").glob(f"*.{process.pid}*")), [])
            self.assertFalse((self.root / "cargo-calls").exists())

            wait_until(admission.callers_released, admission.state_dir)
        finally:
            if hidden.exists():
                _ = hidden.rename(admission.socket_path)

    def test_lost_token_refuses_execution_before_retry_after_memory_admission(self) -> None:
        admission = caller_admission(self, self.root, self.environment)
        board = self.root / "board"
        _ = Path(self.environment["BUILDLOG_MEMINFO"]).write_text("MemAvailable: 0 kB\n")
        errors = self.root / "lost-token.err"
        attempts = self.root / "attempt-statuses"
        shell = self.root / "observe-attempts.sh"
        _ = shell.write_text('''set -T
observe_attempt_status() {
    if [[ "${FUNCNAME[1]:-}" == run_step_attempt && "$BASH_COMMAND" == 'buildlog_retry_unstarted_step "$@"' ]]; then
        printf '%s\n' "$status" >> "$TEST_ATTEMPT_STATUSES"
    fi
}
trap observe_attempt_status DEBUG
''')
        process = self.start_memory_waiter({**self.environment, "UNIT_RUN_DIR": str(board),
            "UNIT_TEAM_ROLE": "impl", "BASH_ENV": str(shell),
            "TEST_ATTEMPT_STATUSES": str(attempts)}, errors)
        lock = board / "locks/cargo.d"
        released = subprocess.run(["bash", str(BOARD), "release", str(board), "impl", "cargo",
            "--pid", str(process.pid)], capture_output=True, text=True,
            check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(released.returncode, 0, released.stdout + released.stderr)
        peer = subprocess.run(["bash", str(BOARD), "acquire", str(board), "peer", "cargo",
            "--pid", str(os.getpid()), "--wait", "0"], capture_output=True, text=True,
            check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(peer.returncode, 0, peer.stdout + peer.stderr)
        admission.free(48 * 1024 ** 3)
        self.wait_for_output(errors, "waiting for the cargo token since", process)
        self.assertEqual(attempts.read_text().splitlines(), ["125"])
        self.assertIn("lost the cargo token before its step started", errors.read_text())
        self.assertFalse((self.root / "cargo-calls").exists())
        self.assertEqual((lock / "holder_pid").read_text(), str(os.getpid()))
        with admission.loop_turn():
            # Release the fixture host before checking all reservations.
            for reservation in admission.scenery_reservations:
                replies = admission.server.handle_request(wire.ReleaseRequest("finish-host", reservation, 0))
                self.assertEqual(replies, [wire.Ok()])
            self.assertEqual(admission.caller_reservations(), {})
            self.assertEqual(admission.server.reservations, {})
        released = subprocess.run(["bash", str(BOARD), "release", str(board), "peer", "cargo",
            "--pid", str(os.getpid())], capture_output=True, text=True,
            check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(released.returncode, 0, released.stdout + released.stderr)
        self.assertEqual(process.wait(timeout=TEST_HANG_GUARD_SECS), 0, errors.read_text())
        self.assertEqual(attempts.read_text().splitlines(), ["125", "0"])
        self.assertEqual((self.root / "cargo-calls").read_text().count("check"), 1)
        self.assertFalse(lock.exists())
        with admission.loop_turn():
            self.assertEqual(admission.server.reservations, {})

    def test_two_boards_share_memory_without_a_resource_cycle(self) -> None:
        admission = caller_admission(self, self.root, self.environment)
        admission.free(20 * 1024 ** 3)
        first_board = self.root / "first-board"
        second_board = self.root / "second-board"
        errors = self.root / "first.err"
        with errors.open("wb") as stream:
            first = subprocess.Popen(["bash", str(VERIFY), "check", "sample", "--no-cache"], cwd=self.root,
                env={**self.environment, "UNIT_RUN_DIR": str(first_board), "UNIT_TEAM_ROLE": "impl", "TEST_BLOCK": "1"},
                stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        self.started_groups.add(first.pid)
        self.wait_for_running(self.root / "running", first)
        second_errors = self.root / "second.err"
        second = self.start_memory_waiter({**self.environment, "UNIT_RUN_DIR": str(second_board),
            "UNIT_TEAM_ROLE": "impl"}, second_errors)
        self.assertEqual((first_board / "locks/cargo.d/holder_pid").read_text(), str(first.pid))
        self.assertEqual((second_board / "locks/cargo.d/holder_pid").read_text(), str(second.pid))
        _ = (self.root / "release").touch()
        self.assertEqual(first.wait(timeout=TEST_HANG_GUARD_SECS), 0, errors.read_text())
        self.assertEqual(second.wait(timeout=TEST_HANG_GUARD_SECS), 0, second_errors.read_text())
        self.assertEqual((self.root / "cargo-calls").read_text().count("check"), 2)
        self.assertEqual(admission.caller_reservations(), {})



if __name__ == "__main__":
    _ = unittest.main()
