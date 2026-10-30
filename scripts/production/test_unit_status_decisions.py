"""Unit-status integration tests, grouped by subject."""
from __future__ import annotations

from scripts.production.unit_status_test_support import UnitStatusFixture, setUpModule as setUpModule
from dailies_input import StillWaiting


class UnitStatusTests(UnitStatusFixture):
    def test_gate_first_sight_prints_decision_and_records_seen_key(self) -> None:
        gate = "● Choose the release path\n\n— gate: approve the blue path\n"
        outputs, _, seen = self.run_statuses(None, "ok", 0, panes=({"hook": gate},))
        self.assertIn("=== DECISION for you from hook ===", outputs[0])
        self.assertIn("— gate: approve the blue path", outputs[0])
        self.assertIn("=== end hook ===", outputs[0])
        self.assertEqual(seen, "hook|— gate: approve the blue path\n")

    def test_repeated_gate_prints_still_waiting(self) -> None:
        gate = "● Choose the release path\n\n— gate: approve the blue path\n"
        outputs, _, _ = self.run_statuses(
            None, "ok", 0, panes=({"hook": gate}, {"hook": gate})
        )
        self.assertIn("=== DECISION for you from hook ===", outputs[0])
        self.assertIn("STILL WAITING on you, hook:— gate: approve the blue path", outputs[1])
        self.assertNotIn("=== DECISION", outputs[1])

    def test_a_pause_question_after_an_open_gate_leaves_the_gate_waiting(self) -> None:
        gate = ("● The showrunner has closed this run on its side.\n\n"
                "  The question above is still open: the repair-round count, next / drop /\n  elaborate.\n\n"
                "  — gate: add-on review, item 1 of 2\n\n✻ Worked for 1s · done 16:46\n")
        paused = gate + ("\n› Message from @conversation-pause: conversation-pause: the user has been quiet\n"
                         "here for 15 minutes. Ask them this, word for word, and nothing else: Return…\n"
                         "(ctrl+o to expand)\n\n● Return to automatic updates? (yes / no)\n\n"
                         "✻ Cooked for 9s · done 17:02\n")
        outputs, _, _ = self.run_statuses(None, "ok", 0, panes=({"hook": gate}, {"hook": paused}))
        self.assertIn("=== DECISION for you from hook ===", outputs[0])
        self.assertIn("STILL WAITING on you, hook:  — gate: add-on review, item 1 of 2", outputs[1])
        waiting = self.parse_status(outputs[1])[0].flags
        self.assertEqual(waiting, (StillWaiting("— gate: add-on review, item 1 of 2"),))

    def test_holding_after_gate_prints_no_user_wait(self) -> None:
        gate = "● Choose the release path\n\n— gate: approve the blue path\n"
        outputs, _, _ = self.run_statuses(
            None, "ok", 0,
            panes=({"hook": gate}, {"hook": "— holding: preparing the release\n"}),
        )
        self.assertNotIn("WAITING on you", outputs[1])
        self.assertNotIn("=== DECISION", outputs[1])
        self.assertNotIn("BLOCK in", outputs[1])

    def test_holding_text_that_mentions_gate_clears_user_wait(self) -> None:
        gate = "● Choose the release path\n\n— gate: approve the blue path\n"
        outputs, _, _ = self.run_statuses(
            None, "ok", 0,
            panes=({"hook": gate}, {"hook": "— holding: documenting gate: handling\n"}),
        )
        self.assertNotIn("WAITING on you", outputs[1])
        self.assertNotIn("=== DECISION", outputs[1])
        self.assertNotIn("BLOCK in", outputs[1])
