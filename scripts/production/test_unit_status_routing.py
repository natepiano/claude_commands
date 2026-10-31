"""Unit-status integration tests, grouped by subject."""
from __future__ import annotations

from scripts.production.unit_status_test_support import UnitStatusFixture, setUpModule as setUpModule


class UnitStatusTests(UnitStatusFixture):
    def test_showrunner_gate_text_that_mentions_blocked_prints_no_block(self) -> None:
        gate = "— gate: showrunner must review the blocked: wording\n"
        outputs, _, seen = self.run_statuses(None, "ok", 0, panes=({"hook": gate},))
        self.assertNotIn("WAITING on you", outputs[0])
        self.assertNotIn("=== DECISION", outputs[0])
        self.assertNotIn("BLOCK in", outputs[0])
        self.assertEqual(seen, "")

    def test_gate_naming_showrunner_stays_activity_without_user_wait(self) -> None:
        gate = "— gate: showrunner must merge the checkpoint\n"
        outputs, _, seen = self.run_statuses(None, "ok", 0, panes=({"hook": gate},))
        self.assertNotIn("WAITING on you", outputs[0])
        self.assertNotIn("=== DECISION", outputs[0])
        self.assertNotIn("BLOCK in", outputs[0])
        self.assertIn("gate: showrunner must merge the checkpoint", outputs[0])
        self.assertEqual(seen, "")

    def test_gate_naming_another_unit_stays_activity_without_user_wait(self) -> None:
        gate = "— gate: peer must publish its checkpoint\n"
        outputs, _, seen = self.run_statuses(
            None, "ok", 0,
            panes=({"hook": gate, "peer": "— holding: publishing\n"},),
            units=("hook", "peer"),
        )
        hook_output = outputs[0].partition("== peer")[0]
        self.assertNotIn("WAITING on you", hook_output)
        self.assertNotIn("=== DECISION", hook_output)
        self.assertNotIn("BLOCK in", hook_output)
        self.assertIn("gate: peer must publish its checkpoint", hook_output)
        self.assertEqual(seen, "")

    def test_peer_name_routes_gate_only_as_a_whole_word(self) -> None:
        outputs, _, _ = self.run_statuses(
            None, "ok", 0,
            panes=(
                {"writer": "— gate: approve the webhook wording\n"},
                {"writer": "— gate: hook must approve the wording\n"},
            ),
            units=("writer", "hook"),
        )
        writer_output = outputs[0].partition("== hook")[0]
        self.assertIn("=== DECISION for you from writer ===", writer_output)
        routed_writer_output = outputs[1].partition("== hook")[0]
        self.assertNotIn("WAITING on you", routed_writer_output)
        self.assertNotIn("=== DECISION", routed_writer_output)

    def test_showrunner_name_routes_gate_only_as_a_whole_word(self) -> None:
        gate = "— gate: approve the showrunnerish wording\n"
        outputs, _, _ = self.run_statuses(None, "ok", 0, panes=({"hook": gate},))
        self.assertIn("=== DECISION for you from hook ===", outputs[0])
