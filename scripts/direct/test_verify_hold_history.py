"""Real verification hold waits: history."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import cast

from scripts.direct.recorder_fixture import invoke_recorder
from scripts.direct.verify_hold_wait_test_support import VerifyHoldWaitFixture, grant_call


class VerifyHoldWaitTests(VerifyHoldWaitFixture):
    def test_reused_server_wait_uses_the_slots_open_phase_after_its_old_phase_closed(self) -> None:
        self.set_hold_routing("natedev_only")
        history = self.record_phase()
        session = Path(self.fixture.environment["UNIT_RUN_DIR"])
        for arguments in (
            ["finish-pass", "--session-dir", str(session), "--status", "completed"],
            ["finish-phase", "--session-dir", str(session), "--phase-id", "6", "--status", "completed"],
            ["start-phase", "--session-dir", str(session), "--phase-id", "7", "--phase-title", "Next"],
            ["start-pass", "--session-dir", str(session), "--phase-id", "7", "--pass-kind", "impl",
             "--activity", "verify", "--called-task", "direct.impl", "--called-family", "codex",
             "--called-model", "fixture", "--worker-name", "fixture-impl"],
        ):
            result = invoke_recorder(arguments, self.fixture.environment, self.fixture.root)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.fixture.environment["UNIT_PHASE_ID"] = "6"
        self.hold()
        process = self.start("check", "sample")
        self.wait_for_call(process)
        grant_call(self.cycle_file)
        code, output = self.finish(process)
        self.assertEqual(code, 0, output)
        rows = [cast(dict[str, object], json.loads(line)) for line in history.read_text().splitlines()]
        phase, = [row for row in rows if row["event_type"] == "phase_started" and row["phase_id"] == "7"]
        wait, = [row for row in rows if row["event_type"] == "wait_started"]
        self.assertEqual((wait["phase_id"], wait["phase_instance_id"]), ("7", phase["phase_instance_id"]))

    def test_review_with_an_empty_slot_records_its_active_pass_wait(self) -> None:
        self.set_hold_routing("natedev_only")
        history = self.record_phase()
        self.fixture.environment.update({"UNIT_TEAM_ROLE": "", "CODEX_THREAD_ID": "thread-review"})
        _ = (Path(self.fixture.environment["UNIT_RUN_DIR"]) / "mesh_roster.json").write_text(json.dumps({
            "fixture-impl": {"thread_id": "thread-impl"}, "fixture-review": {"thread_id": "thread-review"}}))
        session = self.fixture.environment["UNIT_RUN_DIR"]
        result = invoke_recorder([
            "start-pass", "--session-dir", session, "--phase-id", "6", "--pass-kind", "review",
            "--activity", "verify", "--called-task", "direct.review", "--called-family", "codex",
            "--called-model", "fixture", "--worker-name", "fixture-review",
        ], self.fixture.environment, self.fixture.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.hold()
        process = self.start("check", "sample")
        self.wait_for_call(process)
        grant_call(self.cycle_file)
        code, output = self.finish(process)
        self.assertEqual(code, 0, output)
        rows = [cast(dict[str, object], json.loads(line)) for line in history.read_text().splitlines()]
        phase, = [row for row in rows if row["event_type"] == "phase_started"]
        wait, = [row for row in rows if row["event_type"] == "wait_started"]
        self.assertEqual(wait["phase_instance_id"], phase["phase_instance_id"])

    def test_probe_verification_waits_without_phase_wait_events(self) -> None:
        self.set_hold_routing("natedev_only")
        history = self.record_phase()
        self.fixture.environment["CLAUDE_PROBE_SESSION"] = "1"
        self.hold()
        process = self.start("check", "sample")
        self.wait_for_call(process)
        grant_call(self.cycle_file)
        code, output = self.finish(process)
        self.assertEqual(code, 0, output)
        rows = [cast(dict[str, object], json.loads(line)) for line in history.read_text().splitlines()]
        self.assertEqual([row for row in rows if str(row["event_type"]).startswith("wait_")], [])


if __name__ == "__main__":
    _ = unittest.main()
