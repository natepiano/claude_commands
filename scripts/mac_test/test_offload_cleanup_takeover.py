"""Offload cleanup takeover contracts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import unittest
from scripts.mac_test.offload_cleanup_test_support import SeededCleanupFixture
from scripts.mac_test import claim_state, offload
from scripts.lint.memory_test_support import write_config_fixture


class OffloadCommandTests(SeededCleanupFixture):
    def test_takeover_ends_leftovers_on_recorded_host_then_runs_on_settings_host(self) -> None:
        for unacknowledged in (False, True):
            with self.subTest(unacknowledged=unacknowledged):
                self.reset_leftover_case()
                pending_identity = None
                alternate = self.root / "alternate-settings.conf"
                values = {**self.config, "host": "next-mac", "connect_timeout_s": "2"}
                _ = write_config_fixture(alternate, "".join(f"{key}={value}\n" for key, value in values.items()),
                                         template="mac_test")
                settings = self.config_path if unacknowledged else alternate
                assigned_options = tuple(self.assignment_options(self.repository, "nextest", settings, ()))
                if unacknowledged:
                    self.fixture["run_status_reported"] = False
                    self.sync_fixture()
                    interrupted = self.process_command(runner_options=assigned_options)
                    self.assertEqual(interrupted.returncode, 75, interrupted.stdout)
                    self.assertTrue(self.claim_file.exists())
                    pending_identity = self.remote_claim()
                    cleanup, = self.leftover_events()
                    exact = claim_state.ClaimIdentity(pending_identity["claim_id"], pending_identity["episode"],
                                                     pending_identity["attempt"])
                    self.assertIn(offload.pending_start_key(exact), cleanup["args"][-1])
                    self.assertTrue(cleanup["claimed"])
                    previous_host = self.remote_claim()["remote_host"]
                    self.clear_events()
                    self.fixture["run_status_reported"] = True
                    self.sync_fixture()
                else:
                    _ = self.write_kept_claim()
                    previous_host = "previous-mac"
                cleanup_result = self.command("--no-fail-fast", settings=alternate, subprocess_cli=unacknowledged,
                                              runner_options=assigned_options)
                self.assertEqual(cleanup_result.returncode, 75, cleanup_result.stdout)
                self.assert_wire("declined", "busy")
                if unacknowledged:
                    self.assertTrue(self.claim_file.exists(), cleanup_result.stdout + repr(self.events()))
                    assert pending_identity is not None
                    retained = self.remote_claim()
                    self.assertEqual((retained["claim_id"], retained["episode"], retained["attempt"], retained["remote_host"]),
                                     (pending_identity["claim_id"], pending_identity["episode"],
                                      pending_identity["attempt"], pending_identity["remote_host"]))
                    self.assertEqual(self.events_for("rsync"), [])
                    self.clear_events()
                    exact = claim_state.ClaimIdentity(pending_identity["claim_id"], pending_identity["episode"],
                                                     pending_identity["attempt"])
                    self.fixture["cleanup_lines"] = ["dispatch_start_settled=" + offload.pending_start_key(exact),
                                                     "leftovers=0", "rc=0"]
                    self.sync_fixture()
                    cleanup_result = self.command("--no-fail-fast", settings=alternate, subprocess_cli=unacknowledged,
                                                  runner_options=assigned_options)
                    self.assertEqual(cleanup_result.returncode, 75, cleanup_result.stdout)
                    self.assert_wire("declined", "busy")
                self.assertFalse(self.claim_file.exists(), cleanup_result.stdout + repr(self.events()))
                self.assertEqual(len(self.leftover_events()), 1)
                result = self.command("--no-fail-fast", settings=alternate)
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assert_wire("passed", "")
                self.assertFalse(self.claim_file.exists())
                events = self.events()
                self.assertEqual([event["command"] for event in events], ["ssh", "ssh", "rsync", "ssh"])
                cleanup = events[0]
                self.assertEqual(cleanup["args"][-2], previous_host)
                self.assertIn("ConnectTimeout=2", cleanup["args"])
                self.assert_leftover_selectors(cleanup["args"][-1])
                self.assertTrue(cleanup["claimed"])
                for event in self.events_for("ssh")[1:]:
                    self.assertEqual(event["args"][-2], "next-mac")
                self.assert_claimed_commands()

    def test_takeover_with_remaining_leftovers_declines_busy_and_preserves_job(self) -> None:
        old = self.write_kept_claim()
        self.set_leftover_answer("remaining")
        result = self.process_command(separate_stderr=True, assigned_adapter=True)
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

    def test_takeover_check_failure_declines_unreachable_keeps_claim_and_sets_backoff(self) -> None:
        _ = self.write_kept_claim()
        self.set_leftover_answer("ssh_error")
        result = self.process_command(separate_stderr=True, assigned_adapter=True)
        self.assertEqual(result.returncode, 75, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "mac_test: not running on the Mac (unreachable)\n")
        self.assert_wire("declined", "unreachable")
        self.assert_leftover_diagnostic(result.stderr, "ssh_error")
        self.assert_claim_is_kept("previous-mac")
        self.assertTrue((self.state_directory / "unreachable_until").exists())
        self.assertEqual(len(self.events()), 1)
        self.clear_events()
        repeated = self.process_command(assigned_adapter=True)
        self.assertEqual(repeated.returncode, 75, repeated.stdout)
        self.assert_wire("declined", "backoff")
        self.assertEqual(self.events(), [])
        self.assert_claim_is_kept("previous-mac")

    def test_takeover_under_block_ends_leftovers_before_declining_blocked(self) -> None:
        assigned_options = tuple(self.assignment_options(self.repository, "nextest", self.config_path, ()))
        _ = self.write_kept_claim()
        now = datetime.now(timezone.utc)
        block = {"version": 2, "holder": "alice", "for": "restore CI first",
                 "since": now.isoformat(), "expires": (now + timedelta(hours=1)).isoformat(),
                 "session": None, "showrunner": None, "state": "active", "ci": "not_configured",
                 "free_message": {"kind": "not_needed"}}
        path = self.state_directory / "block.json"
        _ = path.write_text(json.dumps(block))
        before = path.read_bytes()
        cleanup_result = self.command(separate_stderr=True, runner_options=assigned_options)
        self.assertEqual(cleanup_result.returncode, 75, cleanup_result.stdout + cleanup_result.stderr)
        self.assert_wire("declined", "busy")
        result = self.command(separate_stderr=True, runner_options=assigned_options)
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
                result = self.process_command(last_decline=previous_reason, separate_stderr=True, assigned_adapter=True)
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
