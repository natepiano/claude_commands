"""Real verify.sh Mac fallback announcements with disposable tools."""
from __future__ import annotations

import unittest
from typing import final
from scripts.direct.verify_mac_offload_test_support import VerifyMacOffloadFixture


@final
class VerifyMacFallbackAnnouncementsTests(VerifyMacOffloadFixture):
    def test_declined_mac_attempt_announces_only_local_execution(self) -> None:
        for reason in ("busy", "blocked", "disk"):
            with self.subTest(reason=reason):
                self.clear_observations()
                result = self.verify("test", "sample", "--no-cache", extra={
                    "TEST_RUNNER_MAC": "declined", "TEST_RUNNER_REASON": reason, "TEST_RUNNER_STATUS": "75"})
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn("verify.sh: running on the Mac", result.stdout + result.stderr)
                self.assertNotIn("mac_test: running on the Mac", result.stdout + result.stderr)
                self.assertIn("verify.sh: running on natedev", result.stdout)
                self.assertEqual(len(self.local_runs()), 1)


if __name__ == "__main__":
    _ = unittest.main()
