"""Offload cleanup takeover contracts."""

from __future__ import annotations

from typing import TYPE_CHECKING
from datetime import datetime, timedelta, timezone
import json
import unittest
from offload_cleanup_test_support import SeededCleanupFixture
if TYPE_CHECKING:
    from ..lib import test_hang_guard
else:
    from scripts.lib import test_hang_guard


class OffloadCommandTests(SeededCleanupFixture):
    def test_takeover_ends_leftovers_on_recorded_host_then_runs_on_settings_host(self) -> None:
        _ = self.write_kept_claim()
        alternate = self.root / "alternate-settings.conf"
        values = {**self.config, "host": "next-mac", "connect_timeout_s": "2"}
        _ = alternate.write_text("".join(f"{key}={value}\n" for key, value in values.items()))
        result = self.command("--no-fail-fast", settings=alternate)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assert_wire("passed", "")
        self.assertFalse(self.claim_file.exists())
        events = self.events()
        self.assertEqual([event["command"] for event in events], ["ssh", "ssh", "rsync", "ssh"])
        cleanup = events[0]
        self.assertEqual(cleanup["args"][-2], "previous-mac")
        self.assertIn("ConnectTimeout=2", cleanup["args"])
        self.assert_leftover_selectors(cleanup["args"][-1])
        self.assertTrue(cleanup["claimed"])
        for event in self.events_for("ssh")[1:]:
            self.assertEqual(event["args"][-2], "next-mac")
        self.assert_claimed_commands()

    def test_takeover_with_remaining_leftovers_declines_busy_and_preserves_job(self) -> None:
        old = self.write_kept_claim()
        self.set_leftover_answer("remaining")
        result = self.process_command(separate_stderr=True)
        self.assertEqual(result.returncode, 75, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "mac_test: not running on the Mac (busy)\n")
        self.assert_wire("declined", "busy")
        self.assertEqual(self.wire_result().get("holder"), "leftovers")
        self.assert_leftover_diagnostic(result.stderr, "remaining")
        self.assert_claim_is_kept("previous-mac")
        self.assertEqual(self.remote_claim()["what"], old["what"])
        self.assertEqual(self.remote_claim()["since"], old["since"])
        self.assertEqual(self.remote_claim()["worktree"], old["worktree"])
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(self.leftover_events()[0]["args"][-2], "previous-mac")

    def test_second_takeover_declines_busy_without_releasing_the_new_kept_claim(self) -> None:
        process = self.start_command(script=self.claim_stub_runner(), extra={"MAC_TEST_TAKEOVER_TWICE": "1"})
        try:
            output, _ = process.communicate(timeout=test_hang_guard.TEST_HANG_GUARD_SECS)
        finally:
            if process.poll() is None:
                process.kill()
                _ = process.communicate(timeout=test_hang_guard.TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, 75, output)
        self.assert_wire("declined", "busy")
        self.assertEqual(self.wire_result().get("holder"), "leftovers")
        self.assertTrue(self.claim_file.exists())
        self.assertEqual((self.stub_directory / "state-events").read_text().splitlines(), ["claim", "release", "claim"])
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(self.leftover_events()[0]["args"][-2], "previous-mac")

    def test_takeover_check_failure_declines_unreachable_keeps_claim_and_sets_backoff(self) -> None:
        _ = self.write_kept_claim()
        self.set_leftover_answer("ssh_error")
        result = self.process_command(separate_stderr=True)
        self.assertEqual(result.returncode, 75, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "mac_test: not running on the Mac (unreachable)\n")
        self.assert_wire("declined", "unreachable")
        self.assert_leftover_diagnostic(result.stderr, "ssh_error")
        self.assert_claim_is_kept("previous-mac")
        self.assertTrue((self.state_directory / "unreachable_until").exists())
        self.assertEqual(len(self.events()), 1)
        self.clear_events()
        repeated = self.process_command()
        self.assertEqual(repeated.returncode, 75, repeated.stdout)
        self.assert_wire("declined", "backoff")
        self.assertEqual(self.events(), [])
        self.assert_claim_is_kept("previous-mac")

    def test_takeover_under_block_ends_leftovers_before_declining_blocked(self) -> None:
        _ = self.write_kept_claim()
        now = datetime.now(timezone.utc)
        block = {"version": 2, "holder": "alice", "for": "restore CI first",
                 "since": now.isoformat(), "expires": (now + timedelta(hours=1)).isoformat(),
                 "session": None, "showrunner": None, "state": "active", "ci": "not_configured",
                 "free_message": {"kind": "not_needed"}}
        path = self.state_directory / "block.json"
        _ = path.write_text(json.dumps(block))
        before = path.read_bytes()
        result = self.command(separate_stderr=True)
        self.assertEqual(result.returncode, 75, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "mac_test: not running on the Mac (blocked)\n")
        self.assert_wire("declined", "blocked")
        self.assertIn("blocked by alice: restore CI first", result.stderr)
        self.assertFalse(self.claim_file.exists())
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(len(self.events_for("ssh")), 1)
        self.assertEqual(self.events_for("rsync"), [])
        self.assertEqual(self.leftover_events()[0]["args"][-2], "previous-mac")

    def test_repeated_takeover_decline_suppresses_leftover_diagnostic(self) -> None:
        for outcome, previous_reason in (("remaining", "busy"), ("missing_status", "blocked")):
            with self.subTest(outcome=outcome):
                self.reset_leftover_case()
                _ = self.write_kept_claim()
                self.set_leftover_answer(outcome)
                result = self.process_command(last_decline=previous_reason, separate_stderr=True)
                self.assertEqual(result.returncode, 75, result.stdout + result.stderr)
                reason = "busy" if outcome == "remaining" else "unreachable"
                self.assert_wire("declined", reason)
                if reason == previous_reason:
                    self.assertEqual((result.stdout, result.stderr), ("", ""))
                else:
                    self.assert_leftover_diagnostic(result.stderr, outcome)
                self.assert_claim_is_kept("previous-mac")



if __name__ == "__main__":
    _ = unittest.main()
