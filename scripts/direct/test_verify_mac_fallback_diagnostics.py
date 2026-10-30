"""Real verify.sh Mac fallback diagnostics with disposable tools."""
from __future__ import annotations

import unittest
from typing import final
from scripts.direct.verify_mac_offload_test_support import VerifyMacOffloadFixture


@final
class VerifyMacFallbackDiagnosticsTests(VerifyMacOffloadFixture):
    def test_mac_state_and_probe_diagnostics_survive_local_fallback(self) -> None:
        for reason, diagnostic in (("state", "mac state cannot be read: invalid block"),
                                   ("blocked", "retained expired block; waiting for removal"),
                                   ("unreachable", "ssh probe failed")):
            with self.subTest(reason=reason):
                self.clear_observations()
                result = self.verify("test", "sample", "--no-cache", extra={
                    "TEST_RUNNER_REASON": reason, "TEST_RUNNER_OUTPUT": diagnostic})
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(diagnostic, result.stdout + result.stderr)
                self.assertEqual(len(self.local_runs()), 1)
                self.assertEqual(self.last_call()["mac_reason"], reason)
                self.assertEqual(self.routing_record()["target"], "natedev")


if __name__ == "__main__":
    _ = unittest.main()
