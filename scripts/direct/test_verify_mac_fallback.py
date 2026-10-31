"""Real verify.sh Mac fallback with disposable tools."""
from __future__ import annotations

import unittest
from pathlib import Path
from typing import final
from scripts.direct.verify_mac_offload_test_support import VerifyMacOffloadFixture


@final
class VerifyMacFallbackTests(VerifyMacOffloadFixture):
    def test_excluded_mac_reason_is_recorded_without_calling_offload(self) -> None:
        for settings, reason in (("offload=on\n", "repo"), (f"offload=on\nlinux_only.{self.root.name}=sample\n", "linux_only")):
            with self.subTest(reason=reason):
                self.clear_observations()
                _ = Path(self.environment["MAC_TEST_CONFIG"]).write_text(settings)
                result = self.verify("test", "sample", "--no-cache")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.runner_events(), [])
                self.assertEqual(self.last_call()["local_reason"], reason)
                self.assertEqual(self.routing_record()["target"], "natedev")


if __name__ == "__main__":
    _ = unittest.main()
