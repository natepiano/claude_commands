"""Real verify.sh Mac configuration with disposable tools."""
from __future__ import annotations

import unittest
from pathlib import Path
from typing import final
from scripts.direct.verify_mac_offload_test_support import VerifyMacOffloadFixture


@final
class VerifyMacConfigurationTests(VerifyMacOffloadFixture):
    def test_local_first_target_runs_without_trying_free_mac(self) -> None:
        config = Path(self.environment["BUILD_TARGETS_CONFIG"])
        _ = config.write_text(config.read_text().replace("targets=mac,natedev", "targets=natedev,mac"))
        result = self.verify("test", "sample", "--no-cache")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.runner_events(), [])
        self.assertEqual(len(self.local_runs()), 1)
        self.assertIn("verify.sh: running on natedev", result.stdout)
        self.assertEqual(self.routing_record()["target"], "natedev")

    def test_alternate_target_settings_are_passed_to_runner(self) -> None:
        alternate = self.root / "alternate Mac.conf"
        _ = alternate.write_text(f"offload=on\nlinux_only.{self.root.name}=\n")
        config = Path(self.environment["BUILD_TARGETS_CONFIG"])
        _ = config.write_text(config.read_text().replace("mac settings.conf", alternate.name))
        result = self.verify("test", "sample", "--filter", "alpha", "--no-cache",
                             extra={"TEST_RUNNER_MAC": "passed", "TEST_RUNNER_STATUS": "0"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        arguments = self.runner_events()[0]["args"]
        self.assertEqual(arguments[arguments.index("--settings") + 1], str(alternate))
        self.assertIn("mac_test: running on the Mac (macOS, host Mac); tree copied in 0.0 s", result.stdout)
        self.assertNotIn("verify.sh: running on the Mac", result.stdout + result.stderr)
        self.assertEqual(self.routing_record()["target"], "mac")


if __name__ == "__main__":
    _ = unittest.main()
