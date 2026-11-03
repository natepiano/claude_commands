"""Real verification hold waits: steps."""

from __future__ import annotations

import json
import unittest
from datetime import datetime
from pathlib import Path
from typing import cast

from scripts.direct.verify_hold_wait_test_support import (
    StepObservation,
    VerifyHoldWaitFixture,
    WaitObservation,
    grant_call,
    read_cycle_file,
)


class VerifyHoldWaitTests(VerifyHoldWaitFixture):
    def test_hold_between_steps_is_waited_and_parent_records_its_seconds(self) -> None:
        self.hold_after_first_step()
        observation = self.observe_waits()
        process = self.start("final")
        _ = self.wait_for_call(process)
        self.assertEqual(self.fixture.local_runs(), [])
        grant_call(self.cycle_file)
        code, output = self.finish(process)
        self.assertEqual(code, 0, output)
        self.assertIn("held by hana for between steps", output)
        observed = cast(WaitObservation, json.loads(observation.read_text()))
        self.assertGreater(float(observed["total"]), 0)
        self.assertAlmostEqual(float(observed["total"]), sum(float(value) for value in observed["waits"]), places=5)
        self.assertFalse(Path(observed["directory"]).exists())
        self.assertEqual(len(self.fixture.local_runs()), 1)

    def test_release_frees_every_step(self) -> None:
        self.hold()
        process = self.start("final")
        _ = self.wait_for_call(process)
        grant_call(self.cycle_file)
        code, output = self.finish(process)
        self.assertEqual(code, 0, output)
        self.assertEqual(len(self.fixture.local_runs()), 1)
        commands = [event["args"][0] for event in self.fixture.cargo_events()]
        self.assertIn("check", commands)
        self.assertIn("nextest", commands)
        self.assertEqual(output.count("; waiting (tests that can run on the Mac"), 1)

    def test_step_start_and_memory_wait_exclude_the_hold_wait(self) -> None:
        self.hold_after_first_step()
        observation = self.observe_waits()
        self.fixture.environment.update({"BUILDLOG_OFF": "0", "BUILDLOG_SYNC": "1"})
        process = self.start("final")
        _ = self.wait_for_call(process)
        grant_call(self.cycle_file)
        release = datetime.fromisoformat(read_cycle_file(self.cycle_file)["release_started_at"]).timestamp()
        code, output = self.finish(process)
        self.assertEqual(code, 0, output)
        records = [cast(StepObservation, json.loads(line)) for line in (self.fixture.root / "step-observations").read_text().splitlines()]
        nextest = [record for record in records if record["arguments"][:2] == ["cargo", "nextest"]]
        self.assertEqual(len(nextest), 1)
        self.assertGreaterEqual(float(nextest[0]["start"]), release)
        self.assertEqual(float(nextest[0]["mem_wait"]), 0)
        observed = cast(WaitObservation, json.loads(observation.read_text()))
        self.assertGreater(float(observed["total"]), 0)
        self.assertAlmostEqual(float(observed["total"]), sum(float(value) for value in observed["waits"]), places=5)


if __name__ == "__main__":
    _ = unittest.main()
