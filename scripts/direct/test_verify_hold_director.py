"""Real verification hold waits: director."""

from __future__ import annotations

import json
import unittest
from typing import cast

from scripts.direct.recorder_fixture import invoke_recorder
from scripts.direct.verify_hold_wait_test_support import VerifyHoldWaitFixture, grant_call


class VerifyHoldWaitTests(VerifyHoldWaitFixture):
    def test_director_verification_uses_its_claude_owner_and_single_active_phase(self) -> None:
        self.set_hold_routing("natedev_only")
        history = self.record_phase()
        self.fixture.environment.update({"CODEX_THREAD_ID": "", "CLAUDE_CODE_SESSION_ID": "claude-director"})
        self.hold()
        process = self.start("check", "sample")
        self.wait_for_call(process)
        grant_call(self.cycle_file)
        code, output = self.finish(process)
        self.assertEqual(code, 0, output)
        rows = [cast(dict[str, object], json.loads(line)) for line in history.read_text().splitlines()]
        wait, = [row for row in rows if row["event_type"] == "wait_started"]
        self.assertEqual((wait["owner_session"], wait["phase_id"]), ("claude-director", "6"))
        self.assertNotIn("pass_instance_id", wait)

    def test_director_verification_with_two_active_phases_records_one_miss_for_both(self) -> None:
        self.set_hold_routing("natedev_only")
        history = self.record_phase()
        session = self.fixture.environment["UNIT_RUN_DIR"]
        result = invoke_recorder(["start-phase", "--session-dir", session, "--phase-id", "7", "--phase-title", "Concurrent"],
                                 self.fixture.environment, self.fixture.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.fixture.environment.update({"CODEX_THREAD_ID": "", "CLAUDE_CODE_SESSION_ID": "claude-director"})
        self.hold()
        process = self.start("check", "sample")
        self.wait_for_call(process)
        grant_call(self.cycle_file)
        code, output = self.finish(process)
        self.assertEqual(code, 0, output)
        rows = [cast(dict[str, object], json.loads(line)) for line in history.read_text().splitlines()]
        miss, = [row for row in rows if row["event_type"] == "wait_not_recorded"]
        self.assertEqual(miss["owner_session"], "claude-director")
        attribution = cast(dict[str, object], miss["wait_attribution"])
        candidates = cast(list[dict[str, object]], attribution["candidates"])
        self.assertEqual({candidate["phase_id"] for candidate in candidates}, {"6", "7"})
        self.assertFalse(any(row["event_type"] == "wait_started" for row in rows))
        report = invoke_recorder(["waits", "--session-dir", session], self.fixture.environment, self.fixture.root)
        self.assertEqual(report.returncode, 0, report.stderr)
        phases = cast(list[dict[str, object]], json.loads(report.stdout)["phases"])
        self.assertEqual([phase["state"] for phase in phases], ["UncertainCoverage", "UncertainCoverage"])
        self.assertTrue(all("wait_n" not in phase for phase in phases))


if __name__ == "__main__":
    _ = unittest.main()
