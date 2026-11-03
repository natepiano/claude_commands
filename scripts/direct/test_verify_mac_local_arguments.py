"""Real verify.sh Mac local arguments with disposable tools."""
from __future__ import annotations

import unittest
from typing import final
from scripts.direct.verify_mac_offload_test_support import VerifyMacOffloadFixture


@final
class VerifyMacLocalArgumentsTests(VerifyMacOffloadFixture):
    def test_local_flag_accepts_linux_reasons_that_contain_reservation_letters(self) -> None:
        for reason in ("uses a threshold only Linux reports", "needs Wayland; the household fixture is Linux only"):
            with self.subTest(reason=reason):
                result = self.verify("test", "sample", "--local", reason, "--no-cache")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(len(self.local_runs()), 1)
                self.clear_observations()

    def test_local_flag_parses_before_and_after_filter(self) -> None:
        reason = "uses Linux filesystem semantics"
        cases = (
            ("test", "sample", "--local", reason, "--filter", "alpha"),
            ("test", "sample", "--filter", "alpha", "--local", reason),
        )

        for arguments in cases:
            with self.subTest(arguments=arguments):
                self.clear_observations()
                result = self.verify(*arguments, "--no-cache")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.runner_events(), [])
                local = self.local_runs()
                self.assertEqual(len(local), 1)
                self.assertNotIn("--local", local[0]["args"])
                self.assertNotIn(reason, local[0]["args"])
                record = self.last_call()
                self.assertEqual(record["command"], "test sample --filter alpha")
                self.assertEqual(record["mac_reason"], "local_flag")
                self.assertEqual(record["local_reason"], reason)


if __name__ == "__main__":
    _ = unittest.main()
