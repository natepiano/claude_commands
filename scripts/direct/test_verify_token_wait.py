"""Real verify.sh token wait behavior."""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import cast

from scripts.direct import test_verify_step_routes as routes
from scripts.direct.verify_token_wait_test_support import VerifyTokenWaitFixture, Record


class VerifyTokenWaitTests(VerifyTokenWaitFixture):
    def test_mac_routing_under_hold_skips_initial_token_acquisition(self) -> None:
        holders = Path(self.environment["BUILD_HOLD_DIR"])
        holders.mkdir()
        _ = (holders / "other").write_text("fixture hold")
        targets = self.root / "targets.py"
        runner = self.root / "mac-runner.py"
        _ = targets.write_text(routes.TARGETS)
        _ = runner.write_text(routes.RUNNER)
        self.environment.update({
            "BUILD_TARGETS_SCRIPT": str(targets), "VERIFY_MAC_RUNNER": str(runner),
            "TEST_ROUTE_EVENTS": str(self.root / "route-events"),
            "TEST_MAC_EVENTS": str(self.root / "mac-events"),
            "MAC_TEST_CONFIG": str(self.root / "mac-settings"), "TEST_MAC_ALLOWED": "1",
        })
        result = self.verify(self.environment)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = [cast(Record, json.loads(line)) for line in (self.root / "mac-events").read_text().splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(cast(Record, rows[0]["record"])["state"], "on_mac")
        self.assertFalse((self.root / "cargo-calls").exists())
        self.assertFalse((self.root / "session" / "locks" / "cargo.d").exists())
        self.assertEqual(self.records()[-1]["token_wait_s"], 0)

    def test_no_token_records_zero(self) -> None:
        # A session dir alone makes this a director's run, which takes the token.
        environment = {name: value for name, value in self.environment.items()
                       if name != "UNIT_SESSION_DIR"}
        result = self.verify(environment)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(self.records()[0]["token_wait_s"], 0)
        self.assertEqual((self.root / "metadata-calls").read_text().splitlines(), ["metadata"])
        self.assertTrue((self.root / "cargo-calls").read_text().startswith("check "))

    def test_director_run_takes_the_token_as_slot_director(self) -> None:
        clock = self.root / "director-clock"
        _ = clock.write_text("100\n")
        session = Path(self.environment["UNIT_SESSION_DIR"])
        result = self.verify({**self.environment, "UNIT_TOKEN_CLOCK_FILE": str(clock)})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.records()[0]["token_wait_s"], 0)
        self.assertIn("[director] claim: token cargo acquired", (session / "board.log").read_text())
        self.assertFalse((session / "locks" / "cargo.d").exists(), "the director's token was not released")

    def test_token_wait_uses_elapsed_seconds_when_no_clock_override_is_present(self) -> None:
        # Advance Bash's elapsed clock at the two reads, without the file
        # override. A real second passing is irrelevant to the subtraction.
        clock_reads = self.root / "elapsed-clock-reads"
        shell = self.root / "elapsed-clock.sh"
        _ = shell.write_text('''set -T
advance_elapsed_clock() {
    if [[ "$BASH_COMMAND" == 'TOKEN_CLOCK_NOW=$SECONDS' ]]; then
        if [[ -e "$TEST_CLOCK_READS" ]]; then SECONDS=120; else SECONDS=100; fi
        : > "$TEST_CLOCK_READS"
    fi
}
trap advance_elapsed_clock DEBUG
''')
        environment = {**self.environment, "BASH_ENV": str(shell),
                       "TEST_CLOCK_READS": str(clock_reads),
                       "UNIT_RUN_DIR": str(self.root / "clock-board"),
                       "UNIT_TEAM_ROLE": "clock"}
        for key in ("UNIT_WORKER_POLL_SECS", "UNIT_WORKER_WAIT_ATTEMPTS",
                    "UNIT_EARLY_POLL_SECS", "UNIT_TOKEN_CLOCK_FILE"):
            _ = environment.pop(key, None)
        result = self.verify(environment)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(self.records()[0]["token_wait_s"], 20)



if __name__ == "__main__":
    _ = unittest.main()
