"""Real verify step routing: admission."""
from __future__ import annotations

import json
from pathlib import Path
import unittest
from typing import cast

from scripts.direct.verify_step_routes_test_support import VerifyStepRoutesFixture, TARGETS as TARGETS, RUNNER as RUNNER


class VerifyStepRoutesTests(VerifyStepRoutesFixture):
    def test_check_uses_mac_during_hold_and_removes_record(self) -> None:
        result = self.run_verify("check", "sample")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = self.mac_rows()
        self.assertEqual(len(rows), 1)
        record = cast(dict[str, object], rows[0]["record"])
        self.assertEqual((record["kind"], record["state"]), ("check", "on_mac"))
        self.assertEqual(record["runs_on"], {"kind": "mac_allowed"})
        self.assertEqual([event for event in self.fixture.cargo_events()
                          if event["args"][:1] != ["metadata"]], [])
        decisions = [cast(dict[str, object], json.loads(line)) for line in self.events.read_text().splitlines()
                     if cast(dict[str, object], json.loads(line))["command"] == "eligible"]
        self.assertTrue(decisions)
        self.assertTrue(all(row["call_id"] == "" for row in decisions))
        self.assert_removed()

    def test_natedev_only_waits_for_release_before_running(self) -> None:
        self.fixture.environment["TEST_MAC_ALLOWED"] = "0"
        result = self.run_verify("check", "sample")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        commands = [cast(dict[str, object], json.loads(line))["command"] for line in self.events.read_text().splitlines()]
        self.assertIn("wait-local", commands)
        self.assertEqual(commands.count("eligible"), 1)
        self.assertEqual(self.mac_rows(), [])
        self.assertTrue(any(event["args"][:1] == ["check"] for event in self.fixture.cargo_events()))
        self.assert_removed()

    def test_unreadable_record_refuses_before_local_or_mac_compile(self) -> None:
        script = Path(self.fixture.environment["BUILD_TARGETS_SCRIPT"])
        # Damage the record in a check that still reports the active hold.
        source = TARGETS.replace('raise SystemExit(10 if any(holders.iterdir()) else 0)', '''
    if command == "register-local":
        for record in Path(os.environ["VERIFY_STEPS_DIR"]).glob("*.json"):
            record.write_text("{")
    raise SystemExit(10 if any(holders.iterdir()) else 0)''')
        _ = script.write_text(source)
        result = self.run_verify("check", "sample")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("step refused", result.stderr)
        self.assertEqual(self.mac_rows(), [])
        self.assertFalse(any(event["args"][:1] == ["check"] for event in self.fixture.cargo_events()))
        self.assert_removed()

if __name__ == "__main__":
    _ = unittest.main()
