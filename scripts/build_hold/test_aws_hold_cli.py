"""Check the documented AWS hold with committed settings and private state."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import cast
import unittest

from scripts.build_hold.build_hold_test_support import IsolatedBuildHoldTest, SCRIPT
from scripts.lib import test_hang_guard


class AwsHoldCliTests(IsolatedBuildHoldTest):
    def test_documented_aws_hold_and_release_use_the_committed_config(self) -> None:
        config = SCRIPT.parents[2] / "config"
        systemd = self.scratch / "tools/systemd-run"
        _ = systemd.write_text("#!/bin/sh\nexit 0\n")
        systemd.chmod(0o755)
        environment = {
            **os.environ,
            "CONF_DIR": str(config),
            "BUILD_TARGETS_CONFIG": str(config / "build_targets.conf"),
        }
        block = self.scratch / ".local/state/build-targets/aws/block.json"
        held = subprocess.run(
            [sys.executable, str(SCRIPT), "hold", "--for", "test AWS hold", "--target", "aws",
             "--holder", "aws-hold-fixture"],
            env=environment, capture_output=True, text=True, check=False, timeout=test_hang_guard.TEST_HANG_GUARD_SECS)
        self.assertEqual(held.returncode, 0, held.stdout + held.stderr)
        self.assertTrue(block.is_file())
        self.assertEqual(cast(dict[str, object], json.loads(block.read_text()))["holder"], "aws-hold-fixture")
        released = subprocess.run(
            [sys.executable, str(SCRIPT), "release", "--target", "aws", "--holder", "aws-hold-fixture"],
            env=environment, capture_output=True, text=True, check=False, timeout=test_hang_guard.TEST_HANG_GUARD_SECS)
        self.assertEqual(released.returncode, 0, released.stdout + released.stderr)
        self.assertFalse(block.exists())
        self.assertFalse((self.scratch / "holders").exists())
        self.assertFalse((self.scratch / "mac-state/block.json").exists())


if __name__ == "__main__":
    _ = unittest.main()
