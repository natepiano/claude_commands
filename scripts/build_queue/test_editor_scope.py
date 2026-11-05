"""Editor scopes contribute measured memory without build reservations."""
from __future__ import annotations

import unittest

from scripts.build_queue import admission_protocol as wire
from scripts.build_queue.admission_test_support import AdmissionFixture
from scripts.build_queue.scope_activity import IdleService
from scripts.lint import memory_admit as gate


class EditorScopeTests(unittest.TestCase):
    def test_editor_scope_counts_measured_memory_once(self) -> None:
        with AdmissionFixture() as fixture:
            scope = fixture.root / "builds.slice/editor.scope"
            nested = scope / "nested.scope"
            nested.mkdir(parents=True)
            _ = (scope / "cgroup.procs").write_text("")
            _ = (nested / "cgroup.procs").write_text("")
            _ = (scope / "memory.current").write_text(str(4 * gate.GIB))
            _ = (scope / "memory.stat").write_text("inactive_file 0\n")
            _ = (nested / "memory.current").write_text(str(4 * gate.GIB))
            _ = (nested / "memory.stat").write_text("inactive_file 0\n")
            _ = fixture.server.run_pass()
            inputs = fixture.server.last_inputs
            assert isinstance(inputs, gate.QueueInputs)
            measured = [row.measured for row in inputs.running if isinstance(row.measured, gate.UnownedSliceMemory)]
            self.assertEqual(len(measured), 2)
            idle, = (memory for memory in measured if isinstance(memory.job_state, IdleService))
            self.assertEqual(idle.bytes, 0)
            self.assertEqual(sum(step.bytes for step in measured), 4 * gate.GIB)
            decision = gate.decide(60 * gate.GIB, 60 * gate.GIB, 18 * gate.GIB, [row.measured for row in inputs.running],
                                   limits=gate.AdmissionLimits(21 * gate.GIB, 3 * gate.GIB))
            self.assertEqual(decision.charged, 4 * gate.GIB)
            self.assertFalse(decision.budget_passes)
            self.assertFalse(fixture.server.waiters)
            self.assertFalse(fixture.server.reservations)
            self.assertEqual(fixture.history(), [])
            _ = (scope / "memory.current").write_text("0")
            _ = (nested / "memory.current").write_text("0")
            fixture.clock.advance(gate.read_memory_config().running_pass_seconds)
            fixture.server.run_once(0)
            reply = fixture.server.handle_request(wire.ViewRequest())[0]
            assert isinstance(reply, wire.ViewReply)
            remaining, = reply.reading.view.running
            self.assertIsInstance(remaining.measured, gate.UnownedSliceMemory)
            assert isinstance(remaining.measured, gate.UnownedSliceMemory)
            self.assertIsInstance(remaining.measured.job_state, IdleService)
            self.assertEqual(remaining.measured.bytes, 0)
            inputs = fixture.server.last_inputs
            assert isinstance(inputs, gate.QueueInputs)
            decision = gate.decide(60 * gate.GIB, 60 * gate.GIB, 18 * gate.GIB, [row.measured for row in inputs.running],
                                   limits=gate.AdmissionLimits(21 * gate.GIB, 3 * gate.GIB))
            self.assertTrue(decision.budget_passes)


if __name__ == "__main__":
    _ = unittest.main()
