"""Real verify entry point cancellation with private Cargo and Git fixtures."""
from __future__ import annotations

import subprocess
import unittest
import sys
import json
from datetime import UTC, datetime
from pathlib import Path

from scripts.build_queue import placement

from scripts.direct.verify_tree_cancel_test_support import cancellation_lines, VerifyTreeCancelFixture
from scripts.direct.verify_token_wait_test_support import BOARD
from scripts.direct.wait_for_test_state import wait_until
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


class VerifyTreeCancelTests(VerifyTreeCancelFixture):
    def test_cancel_during_reacquisition_after_a_hold_removes_own_entry_and_keeps_peer(self) -> None:
        ready = self.root / "hold-ready"
        holds = Path(self.environment["BUILD_HOLD_DIR"])
        holds.mkdir(exist_ok=True)
        seed = self.root / "hold-seed"
        _ = seed.write_text(json.dumps({"holder": "hana", "since": datetime.now(UTC).isoformat(),
                                       "for": "reacquisition test", "release_eta": "unknown"}))
        shell = self.root / "hold-at-local-step.sh"
        _ = shell.write_text('set -T\nhold_at_local_step() {\n    if [[ "$BASH_COMMAND" == check_routing_hold && "${TOKEN_HELD:-0}" == 1 && ! -e "$TEST_HOLD_READY" ]]; then\n        cp "$TEST_HOLD_SEED" "$BUILD_HOLD_DIR/hana"\n        printf \'%s\\n%s\\n\' "$BUILDLOG_PLACEMENT_EPISODE" "$(< "$BOARD_DIR/locks/cargo.d/holder_pid")" > "$TEST_HOLD_READY"\n    fi\n}\ntrap hold_at_local_step DEBUG\n')
        process = self.start_verify("test", BASH_ENV=str(shell),
            TEST_HOLD_SEED=str(seed), TEST_HOLD_READY=str(ready))
        lock = self.board / "locks/cargo.d"
        hold = holds / "hana"
        wait_until(lambda: ready.exists() and len(ready.read_text().splitlines()) == 2,
                   self.root, processes=(process,))
        episode, holder_pid = ready.read_text().splitlines()
        self.assertEqual(holder_pid, str(process.pid))

        def blocked_episode() -> bool:
            with self.admission.loop_turn():
                return episode in self.admission.server.placement_requests and any(
                    candidate.destination.provider == "local"
                    and isinstance(candidate.eligibility, placement.TemporaryExclusion)
                    and candidate.eligibility.reason == "blocked"
                    for candidate in self.admission.server.placement_eligibility.get(episode, ()))

        wait_until(lambda: blocked_episode() and not lock.exists(), self.root, processes=(process,))
        self.assertEqual(self.admission.server.placement_requests[episode].episode, episode)
        holder = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()", "verify.sh"],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, start_new_session=True)
        self.started_groups.add(holder.pid)
        self.addCleanup(self.finish_process, holder)
        taken = subprocess.run(["bash", str(BOARD), "acquire", str(self.board), "peer", "cargo",
                                "--pid", str(holder.pid), "--hold", "3600", "--wait", "0"],
                               capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(taken.returncode, 0, taken.stdout + taken.stderr)
        hold.unlink()
        waiters = self.board / "token_waiters"
        wait_until(lambda: bool(list(waiters.glob(f"*.{process.pid}.again"))),
                   waiters, processes=(process,))
        self.assertEqual(len(list(waiters.glob(f"*.{process.pid}.again"))), 1,
                         "fixture never reached reacquisition after the hold")
        peer_entry = waiters / f"peer.0123456789abcdef.9999999999.{holder.pid}.again"
        peer_entry.touch()
        self.edit_source()
        output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, 77, output + errors)
        self.assertEqual(len(cancellation_lines(output + errors, "test")), 1, output + errors)
        self.assertEqual(sorted(waiters.iterdir()), [peer_entry], "cancel left its reacquisition entry")
        self.assertEqual((lock / "holder_pid").read_text(), str(holder.pid), "cancel released a peer's claim")
        self.assertIsNone(holder.poll(), "cancel killed its peer")
        self.assertEqual(self.admission.server.reservations, {})
        self.assertEqual(self.admission.server.waiters, {})
        self.assertEqual(list((self.root / "verify-steps").glob("*.json")), [])
        self.assert_no_cache_verdict()
        self.assertEqual([args for args in self.cargo_calls() if args[:2] == ["nextest", "run"]], [])
        records = self.records()
        self.assertEqual([(row.get("outcome"), row.get("status")) for row in records], [("cancelled", 77)])
        released = subprocess.run(["bash", str(BOARD), "release", str(self.board), "peer", "cargo",
                                   "--pid", str(holder.pid)], capture_output=True, text=True,
                                  check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(released.returncode, 0, released.stdout + released.stderr)

    def test_tree_cancel_during_token_wait_removes_own_entry_and_keeps_peer(self) -> None:
        # Keep the peer alive before the caller publishes its token entry.
        holder = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()", "verify.sh"],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, start_new_session=True)
        self.started_groups.add(holder.pid)
        self.addCleanup(self.finish_process, holder)
        taken = subprocess.run(["bash", str(BOARD), "acquire", str(self.board), "peer", "cargo",
                                "--pid", str(holder.pid), "--hold", "3600", "--wait", "0"],
                               capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(taken.returncode, 0, taken.stdout + taken.stderr)
        shell = self.root / "local-attempt.sh"
        _ = shell.write_text('''set -T
probe_local_attempt() {
    if [[ "$BASH_COMMAND" == '[[ "$STEP_DEFER_LOCAL" == 0 ]]' ]]; then STEP_DEFER_LOCAL=1; fi
}
trap probe_local_attempt DEBUG
''')
        # Defer acquisition until the local step starts with its tree watcher.
        process = self.start_verify("test", BASH_ENV=str(shell))
        lock = self.board / "locks/cargo.d"
        waiters = self.board / "token_waiters"
        wait_until(lambda: bool(list(waiters.glob(f"*.{process.pid}")))
                   or process.poll() is not None, waiters, timeout=TEST_HANG_GUARD_SECS, processes=(process,))
        self.assertIsNone(process.poll(), "verify never entered the token wait")
        own_entries = list(waiters.glob(f"*.{process.pid}"))
        self.assertEqual(len(own_entries), 1, "fixture never reached the local token wait")
        self.assertEqual(self.admission.server.reservations, {})
        self.assertEqual(self.admission.server.waiters, {})
        # Keep a peer's entry present too: cancellation must remove only its own files.
        peer_entry = waiters / f"peer.0123456789abcdef.9999999999.{holder.pid}.again"
        peer_entry.touch()
        self.edit_source()
        output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, 77, output + errors)
        self.assertEqual(len(cancellation_lines(output + errors, "test")), 1)
        self.assertEqual(sorted(waiters.iterdir()), [peer_entry], "cancel left its token entry")
        self.assertEqual((lock / "holder_pid").read_text(), str(holder.pid), "cancel released a peer's claim")
        self.assertIsNone(holder.poll(), "cancel killed its peer")
        self.assertEqual(self.admission.server.reservations, {})
        self.assertEqual(self.admission.server.waiters, {})
        self.assertEqual(list((self.root / "verify-steps").glob("*.json")), [])
        self.assert_no_cache_verdict()
        self.assertEqual([args for args in self.cargo_calls() if args[:2] == ["nextest", "run"]], [])
        records = self.records()
        self.assertEqual([(row.get("outcome"), row.get("status")) for row in records], [("cancelled", 77)])
        released = subprocess.run(["bash", str(BOARD), "release", str(self.board), "peer", "cargo",
                                   "--pid", str(holder.pid)], capture_output=True, text=True,
                                  check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(released.returncode, 0, released.stdout + released.stderr)


if __name__ == "__main__":
    _ = unittest.main()
