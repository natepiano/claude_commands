"""Real verify.sh Mac results with disposable tools."""
from __future__ import annotations

import unittest
from typing import final
from scripts.direct.verify_mac_offload_test_support import VerifyMacOffloadFixture


@final
class VerifyMacResultsTests(VerifyMacOffloadFixture):
    def test_lost_run_continues_locally(self) -> None:
        result = self.verify(
            "test",
            "sample",
            "--no-cache",
            extra={"TEST_RUNNER_MAC": "lost", "TEST_RUNNER_STATUS": "75"},
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.local_runs()), 1)
        record = self.last_call()
        self.assertEqual(record["mac"], "lost")
        self.assertEqual(record["mac_s"], 3.25)

    def test_mac_failure_stops_before_token_and_local_run(self) -> None:
        result = self.verify(
            "test",
            "sample",
            "--no-cache",
            extra={"TEST_RUNNER_MAC": "failed", "TEST_RUNNER_STATUS": "42"},
        )
        self.assertEqual(result.returncode, 42, result.stdout + result.stderr)
        self.assertEqual(self.local_runs(), [])
        runners = self.runner_events()
        self.assertEqual(len(runners), 1)
        self.assertFalse(runners[0]["token_held"])
        self.assertEqual(self.saved_results(), [])
        record = self.last_call()
        self.assertEqual(record["status"], 42)
        self.assertEqual(record["mac"], "failed")
        self.assertEqual(record["mac_reason"], "")
        self.assertEqual(record["mac_s"], 3.25)

    def test_full_mac_pass_ends_the_call_with_a_mac_pass_record(self) -> None:
        result = self.verify(
            "test",
            "sample",
            "--no-cache",
            extra={"TEST_RUNNER_MAC": "passed", "TEST_RUNNER_STATUS": "0"},
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS on the Mac (macOS), so nothing ran on natedev.", result.stdout)
        self.assertEqual(self.local_runs(), [])
        saved = self.saved_results()
        self.assertEqual(len(saved), 1)
        self.assertIn("passed on the Mac", saved[0].read_text())
        self.assertIn("host=mac\n", saved[0].read_text())
        record = self.last_call()
        self.assertEqual((record["status"], record["mac"], record["mac_s"]), (0, "passed_full", 3.25))

        self.clear_observations()
        again = self.verify("test", "sample", extra={"TEST_RUNNER_MAC": "failed", "TEST_RUNNER_STATUS": "42"})
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertIn("PASS (recorded)", again.stdout)
        self.assertEqual(self.runner_events(), [])

        # --local asks for Linux, so the Mac's pass does not answer it.
        self.clear_observations()
        local = self.verify("test", "sample", "--local", "needs Linux process behavior")
        self.assertEqual(local.returncode, 0, local.stdout + local.stderr)
        self.assertNotIn("PASS (recorded)", local.stdout)
        self.assertEqual(len(self.local_runs()), 1)
        saved = self.saved_results()
        self.assertEqual(len(saved), 1)
        self.assertNotIn("host=mac", saved[0].read_text())

    def test_filtered_mac_pass_ends_the_call_with_a_mac_pass_record(self) -> None:
        result = self.verify(
            "test",
            "sample",
            "--filter",
            "alpha",
            "--no-cache",
            extra={"TEST_RUNNER_MAC": "passed", "TEST_RUNNER_STATUS": "0"},
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS on the Mac (macOS), so nothing ran on natedev.", result.stdout)
        self.assertEqual(self.local_runs(), [])
        saved = self.saved_results()
        self.assertEqual(len(saved), 1)
        self.assertIn("host=mac\n", saved[0].read_text())
        record = self.last_call()
        self.assertEqual(record["status"], 0)
        self.assertEqual(record["mac"], "passed_filter")
        self.assertEqual(record["mac_s"], 3.25)


if __name__ == "__main__":
    _ = unittest.main()
