"""Real verify.sh Mac configuration errors with disposable tools."""
from __future__ import annotations

import unittest
from pathlib import Path
from typing import final
from scripts.direct.verify_mac_offload_test_support import VerifyMacOffloadFixture


@final
class VerifyMacConfigurationErrorsTests(VerifyMacOffloadFixture):
    def test_unreadable_local_first_route_refuses_before_either_machine_starts(self) -> None:
        config = Path(self.environment["BUILD_TARGETS_CONFIG"])
        _ = config.write_text(config.read_text().replace("targets=mac,natedev", "targets=natedev,mac"))
        damaged = self.root / "damaged-holders"
        _ = damaged.write_text("a file cannot be a hold directory\n")
        self.environment["BUILD_HOLD_DIR"] = str(damaged)
        for outcome, reason, status in (("passed", "", "0"), ("declined", "busy", "75")):
            with self.subTest(outcome=outcome):
                self.clear_observations()
                result = self.verify("test", "sample", "--filter", "alpha", "--no-cache", extra={
                    "TEST_RUNNER_MAC": outcome, "TEST_RUNNER_REASON": reason, "TEST_RUNNER_STATUS": status})
                self.assertEqual(result.returncode, 12, result.stdout + result.stderr)
                self.assertIn("build hold check register-local failed (exit 12); step refused", result.stderr)
                self.assertEqual(self.runner_events(), [])
                self.assertEqual([event for event in self.cargo_events()
                                  if event["args"][:1] != ["metadata"]], [])
                self.assertIn("natedev's hold state cannot be read", result.stderr)

    def test_unreadable_local_first_route_without_eligible_mac_exits_twelve(self) -> None:
        config = Path(self.environment["BUILD_TARGETS_CONFIG"])
        _ = config.write_text(config.read_text().replace("targets=mac,natedev", "targets=natedev,mac"))
        _ = Path(self.environment["MAC_TEST_CONFIG"]).write_text(f"linux_only.{self.root.name}=sample\n")
        damaged = self.root / "damaged-holders"
        _ = damaged.write_text("a file cannot be a hold directory\n")
        self.environment["BUILD_HOLD_DIR"] = str(damaged)
        result = self.verify("test", "sample", "--filter", "alpha", "--no-cache")
        self.assertEqual(result.returncode, 12, result.stdout + result.stderr)
        self.assertEqual(self.runner_events(), [])
        self.assertEqual(self.local_runs(), [])
        self.assertIn("natedev's hold state cannot be read", result.stderr)


if __name__ == "__main__":
    _ = unittest.main()
