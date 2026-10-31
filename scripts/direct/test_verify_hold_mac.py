"""Real verification hold waits: mac."""

from __future__ import annotations

import json
import subprocess
import unittest
from typing import cast

from scripts.direct.verify_hold_wait_test_support import (
    INVOKE,
    LINT_STEP,
    TEST_HANG_GUARD_SECS,
    VerifyHoldWaitFixture,
)


class VerifyHoldWaitTests(VerifyHoldWaitFixture):
    def test_mac_allowed_check_during_hold_starts_no_phase_hold_wait(self) -> None:
        history = self.record_phase()
        self.set_hold_routing("mac_allowed")
        self.hold()
        before = self.cycle_file.read_bytes()
        self.fixture.environment.update({"TEST_RUNNER_MAC": "passed", "TEST_RUNNER_STATUS": "0"})
        result = self.fixture.verify("check", "sample")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.fixture.runner_events()), 1)
        self.assertEqual(self.fixture.local_runs(), [])
        self.assertEqual(self.cycle_file.read_bytes(), before)
        rows = [cast(dict[str, object], json.loads(line)) for line in history.read_text().splitlines()]
        self.assertEqual([row for row in rows if str(row["event_type"]).startswith("wait_")], [])

    def test_mac_filter_pass_during_local_hold_needs_no_local_admission(self) -> None:
        self.hold()
        before = self.cycle_file.read_bytes()
        self.fixture.environment.update({"TEST_RUNNER_MAC": "passed", "TEST_RUNNER_STATUS": "0"})
        result = self.fixture.verify("test", "sample", "--filter", "smoke")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.fixture.local_runs(), [])
        self.assertEqual(len(self.fixture.runner_events()), 1)
        self.assertEqual(self.cycle_file.read_bytes(), before)
        self.assertNotIn("natedev is held", result.stdout + result.stderr)

    def test_mac_runner_does_not_register_a_local_waiter(self) -> None:
        self.hold()
        before = self.cycle_file.read_bytes()
        environment = {**self.fixture.environment, "BUILDLOG_CALLER": "verify-mac", "BUILDLOG_CALL_ID": "remote-call"}
        result = subprocess.run(
            ["bash", "-c", LINT_STEP, "bash", str(INVOKE)],
            cwd=self.fixture.root, env=environment, capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.cycle_file.read_bytes(), before)
        self.assertEqual([event["args"][0] for event in self.fixture.cargo_events()], ["check"])


if __name__ == "__main__":
    _ = unittest.main()
