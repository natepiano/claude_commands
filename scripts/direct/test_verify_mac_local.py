"""Real verify.sh Mac local with disposable tools."""
from __future__ import annotations

import unittest
from typing import final
from scripts.direct.verify_mac_offload_test_support import VerifyMacOffloadFixture


@final
class VerifyMacLocalTests(VerifyMacOffloadFixture):
    def test_local_flag_skips_runner_records_reason_and_shares_pass_key(self) -> None:
        first_reason = 'needs Linux path handling with a "quoted" mount'
        first = self.verify(
            "test",
            "sample",
            "--local",
            first_reason,
            extra={"TEST_RUNNER_MAC": "failed", "TEST_RUNNER_STATUS": "42"},
        )
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual(self.runner_events(), [])
        self.assertEqual(len(self.local_runs()), 1)
        first_record = self.last_call()
        self.assertEqual(first_record["command"], "test sample")
        self.assertEqual(first_record["mac"], "declined")
        self.assertEqual(first_record["mac_reason"], "local_flag")
        self.assertEqual(first_record["local_reason"], first_reason)
        self.assertNotIn(first_reason, first_record["command"])
        saved_results = self.saved_results()
        self.assertEqual(len(saved_results), 1)

        self.clear_observations()
        without_flag = self.verify("test", "sample")
        self.assertEqual(
            without_flag.returncode, 0, without_flag.stdout + without_flag.stderr
        )
        self.assertIn("PASS (recorded)", without_flag.stdout)
        self.assertEqual(self.runner_events(), [])
        self.assertEqual(self.local_runs(), [])
        self.assertEqual(self.saved_results(), saved_results)

        self.clear_observations()
        second_reason = "requires Linux-only process behavior"
        with_flag = self.verify("test", "sample", "--local", second_reason)
        self.assertEqual(with_flag.returncode, 0, with_flag.stdout + with_flag.stderr)
        self.assertIn("PASS (recorded)", with_flag.stdout)
        self.assertEqual(self.runner_events(), [])
        self.assertEqual(self.local_runs(), [])
        self.assertEqual(self.saved_results(), saved_results)
        second_record = self.last_call()
        self.assertEqual(second_record["local_reason"], second_reason)
        self.assertEqual(second_record["command"], "test sample")


if __name__ == "__main__":
    _ = unittest.main()
