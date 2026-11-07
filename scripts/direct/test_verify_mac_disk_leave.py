"""Keep a Mac retry available while disk pressure holds a local step."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
import unittest
from typing import final
from unittest import mock

from scripts.direct import step_record
from scripts.direct.verify_mac_memory_retry_test_support import VerifyMacMemoryRetryFixture
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lint.memory_test_support import write_config_fixture


@final
class VerifyMacDiskLeaveTests(VerifyMacMemoryRetryFixture):
    def test_disk_decline_during_eligibility_leaves_memory_wait_for_a_free_mac(self) -> None:
        self.assert_disk_decline_leaves_for_mac()

    def test_mac_disk_with_linux_off_leaves_memory_wait_for_a_free_mac(self) -> None:
        config = self.root / "build_targets.conf"
        _ = config.write_text(config.read_text().replace("targets=mac,natedev", "targets=mac,natedev,linux")
                              + "kind.linux=ssh\nplatform.linux=linux\nsettings.linux=linux.conf\n")
        _ = write_config_fixture(self.root / "linux.conf", "offload=off\n", template="aws_target")
        self.assert_disk_decline_leaves_for_mac()

    def assert_disk_decline_leaves_for_mac(self) -> None:
        _ = self.hold_memory()
        available = self.runner_state / "available-gib"
        _ = available.write_text("0\n")
        self.write_executable(self.bin_directory / "ssh", f'''#!/bin/sh
printf 'free_gib=%s\\nhost=test-mac\\nrc=0\\n' "$(cat '{available}')"
''')
        process = self.start_verify("test", "sample", "--no-cache", extra={
            "TEST_RUNNER_BUSY_UNTIL": str(self.runner_state / "mac-free"),
            "TEST_RUNNER_BUSY_REASON": "disk",
            "TEST_RUNNER_MAC": "passed", "TEST_RUNNER_STATUS": "0"})
        self.addCleanup(self.stop_verify, process)
        self.wait_for_memory_wait(process)
        paths = list((self.root / "verify-steps").glob("*.json"))
        self.assertEqual(len(paths), 1)
        with mock.patch.dict(os.environ, {"VERIFY_STEPS_DIR": str(self.root / "verify-steps")}):
            record = step_record.read(paths[0].stem)
        if not isinstance(record, step_record.StepRecord):
            self.fail(f"Expected a live step record, got {record!r}")
        self.assertEqual(record.runs_on, step_record.AnyTarget(("mac", "natedev"), ("mac",)))
        self.assertEqual(len(list((self.root / "mac-idle/calls").glob("*.json"))), 1)
        self.assertEqual(len(self.runner_events()), 1)
        _ = available.write_text("1000\n")
        _ = (self.runner_state / "mac-free").write_text("free\n")
        output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertEqual(self.local_runs(), [])
        self.assertEqual(len(self.runner_events()), 2)
        self.assertEqual(self.routing_record()["target"], "mac")

    def test_runner_disk_decline_retries_as_soon_as_the_mac_is_free(self) -> None:
        _ = self.hold_memory()
        _ = self.control_routing_clock()
        process = self.start_verify("test", "sample", "--no-cache", extra={
            "TEST_RUNNER_BUSY_UNTIL": str(self.runner_state / "mac-free"),
            "TEST_RUNNER_BUSY_REASON": "disk",
            "TEST_RUNNER_MAC": "passed", "TEST_RUNNER_STATUS": "0"})
        self.addCleanup(self.stop_verify, process)
        self.wait_for_memory_wait(process)
        self.assertEqual(len(self.runner_events()), 1)
        _ = (self.runner_state / "mac-free").write_text("free\n")
        output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertEqual(self.local_runs(), [])
        self.assertEqual(len(self.runner_events()), 2)
        self.assertNotIn("waiting 180 s before each retry", errors)
        self.assertEqual(self.routing_record()["target"], "mac")


class StepRetryPermissionTests(unittest.TestCase):
    def test_retry_permission_uses_all_target_rows_in_every_order(self) -> None:
        source = Path(__file__).with_name("verify.sh").read_text()
        start = source.index("SSH_RETRYABLE_REASON=")
        predicate = source[start:source.index('\nMAC_LEAVE_SETTINGS=""', start)]
        cases = (
            (("mac", "disk"),),
            (("linux", "disk"),),
            (("mac", "disk"), ("linux", "off")),
            (("mac", "off"), ("linux", "disk")),
            (("mac", "off"), ("linux", "repo")),
        )
        for rows in cases:
            for ordered in (rows, tuple(reversed(rows))):
                table = "\n".join(f"{name}\texcluded\t{reason}\t/{name}.conf\t{'macos' if name == 'mac' else 'linux'}"
                                  for name, reason in ordered)
                for local_only, kind, writes_tree in (("0", "test", "0"), ("1", "test", "0"),
                                                      ("0", "build", "0"), ("0", "final", "0"),
                                                      ("0", "lint", "1"), ("0", "test", "1")):
                    with self.subTest(rows=ordered, local_only=local_only, kind=kind, writes_tree=writes_tree):
                        result = subprocess.run(
                            ["bash", "-c", predicate + "\nstep_can_retry"], capture_output=True, text=True,
                            env={**os.environ, "LOCAL_ONLY": local_only, "CMD": kind,
                                 "STEP_RUNS_ON": "natedev_only", "STEP_WRITES_TREE": writes_tree,
                                 "STEP_TARGET_TABLE": table}, timeout=TEST_HANG_GUARD_SECS,
                        )
                        expected = (local_only == "0" and kind == "test" and writes_tree == "0"
                                    and any(reason == "disk" for _, reason in rows))
                        self.assertEqual(result.returncode, 0 if expected else 1, result.stdout + result.stderr)


if __name__ == "__main__":
    _ = unittest.main()
