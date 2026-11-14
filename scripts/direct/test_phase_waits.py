"""Open waits in a phase instance feed the Units report Notes."""
from __future__ import annotations

import unittest

from scripts.direct import phase_waits


class PhaseWaitsTests(unittest.TestCase):
    def test_phase_waits_keep_open_waits_and_the_latest_named_holder(self) -> None:
        events: list[dict[str, object]] = [
            {"event_type": "wait_started", "phase_instance_id": "eight", "pass_instance_id": "repair",
             "wait_id": "worker", "wait_reason": "memory admission", "wait_started_epoch": 1_000,
             "holder": {"state": "HolderNotNamed", "reason": "producer named no holder"}},
            {"event_type": "wait_started", "phase_instance_id": "ten", "wait_id": "unrelated",
             "wait_reason": "build queue", "wait_started_epoch": 900},
            {"event_type": "wait_started", "phase_instance_id": "eight", "wait_id": "director",
             "wait_reason": "build queue", "wait_started_epoch": 1_050,
             "holder": {"state": "NamedHolder", "resource_kind": "slot", "resource": "build",
                 "holder": {"state": "UnitHolder", "unit": "builder"}}},
        ]
        waits = phase_waits.phase_waits(events, "eight")
        self.assertEqual([(wait.reason, wait.started) for wait in waits],
                         [("memory admission", 1_000), ("build queue", 1_050)])
        self.assertIsInstance(waits[0].holder, phase_waits.UnnamedWaitHolder)
        self.assertIsInstance(waits[1].holder, phase_waits.NamedWaitHolder)
        assert isinstance(waits[1].holder, phase_waits.NamedWaitHolder)
        self.assertIn("builder", waits[1].holder.label)
        events.append({"event_type": "wait_holder_seen", "phase_instance_id": "eight", "wait_id": "worker",
            "holder": {"state": "NamedHolder", "resource_kind": "slot", "resource": "memory",
                "holder": {"state": "SessionHolder", "session_id": "builder-session"}}})
        waits = phase_waits.phase_waits(events, "eight")
        self.assertIsInstance(waits[0].holder, phase_waits.NamedWaitHolder)
        assert isinstance(waits[0].holder, phase_waits.NamedWaitHolder)
        self.assertIn("builder-session", waits[0].holder.label)
        events.extend([
            {"event_type": "wait_finished", "phase_instance_id": "eight", "wait_id": "worker"},
            {"event_type": "wait_unclosed", "phase_instance_id": "eight", "wait_id": "director"},
        ])
        self.assertEqual(phase_waits.phase_waits(events, "eight"), [])


if __name__ == "__main__":
    _ = unittest.main()
