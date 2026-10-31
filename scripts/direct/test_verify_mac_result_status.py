"""Real verify.sh Mac result status with disposable tools."""
from __future__ import annotations

import unittest
from typing import final
from scripts.direct.verify_mac_offload_test_support import VerifyMacOffloadFixture


@final
class VerifyMacResultStatusTests(VerifyMacOffloadFixture):
    def test_inconsistent_runner_status_continues_locally(self) -> None:
        cases = (('other', '75'), ('passed', '1'))
        for mode, status in cases:
            with self.subTest(mode=mode, status=status):
                self.clear_observations()
                result = self.verify(
                    "test",
                    "sample",
                    "--no-cache",
                    extra={"TEST_RUNNER_MAC": mode, "TEST_RUNNER_STATUS": status},
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(len(self.local_runs()), 1)
                record = self.last_call()
                self.assertEqual(record["mac"], "declined")
                self.assertEqual(record["mac_reason"], "runner")
                self.assertIsNone(record["mac_s"])


if __name__ == "__main__":
    _ = unittest.main()
