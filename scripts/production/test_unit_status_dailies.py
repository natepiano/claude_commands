"""Unit-status integration tests, grouped by subject."""
from __future__ import annotations

from scripts.production.unit_status_test_support import UnitStatusFixture, setUpModule as setUpModule
from dailies_input import ClaudeNotRunning, Decision, SessionGone, StillWaiting


class UnitStatusTests(UnitStatusFixture):
    def test_the_last_three_distinct_eta_lines_are_printed_in_pane_order(self) -> None:
        pane = "\n".join(("ETA 09:00", "ETA 10:00 PDT", "  Phase ETA: 12:55 EDT", "ETA 11:00",
                          "From the user: what is the ETA", "ETA 10:00 PDT", "— holding: waiting on x")) + "\n"
        outputs, _, _ = self.run_statuses(None, "ok", 0, panes=({"hook": pane},))
        stated = [line for line in outputs[0].splitlines() if "ETA" in line]
        self.assertEqual(stated, ["Phase ETA: 12:55 EDT", "ETA 11:00", "ETA 10:00 PDT"])

    def test_real_gate_outputs_become_dailies_flags_or_activity(self) -> None:
        gate = "● Choose the release path\n\n— gate: approve the blue path\n"
        outputs, _, _ = self.run_statuses(
            None, "ok", 0,
            panes=(
                {"hook": gate},
                {"hook": gate},
                {"hook": "— holding: preparing the release\n"},
                {"hook": "— gate: showrunner must merge the checkpoint\n"},
            ),
        )
        first = self.parse_status(outputs[0])[0]
        repeated = self.parse_status(outputs[1])[0]
        holding = self.parse_status(outputs[2])[0]
        routed = self.parse_status(outputs[3])[0]
        self.assertTrue(any(isinstance(flag, Decision) for flag in first.flags))
        self.assertTrue(any(isinstance(flag, StillWaiting) for flag in repeated.flags))
        self.assertEqual(holding.flags, ())
        self.assertEqual(routed.flags, ())
        self.assertTrue(any("gate: showrunner" in line.text for line in routed.activity))
        peer_outputs, _, _ = self.run_statuses(
            None, "ok", 0,
            panes=({"hook": "— gate: peer must publish its checkpoint\n",
                    "peer": "— holding: publishing\n"},),
            units=("hook", "peer"),
        )
        peer_routed = self.parse_status(peer_outputs[0], ("hook", "peer"))[0]
        self.assertEqual(peer_routed.flags, ())
        self.assertTrue(any("gate: peer" in line.text for line in peer_routed.activity))

    def test_a_unit_with_no_marked_session_reads_gone_while_a_marked_one_lives(self) -> None:
        outputs, _, _ = self.run_statuses(None, "ok", 0, panes=({},), units=("hook", "hookworm"),
                                          missing_units=("hook",), stopped_units=("hookworm",))
        gone, live = self.parse_status(outputs[0], ("hook", "hookworm"))
        self.assertIsInstance(gone.state, SessionGone, outputs[0])
        self.assertIsInstance(live.state, ClaudeNotRunning, outputs[0])

    def test_herdr_units_report_activity_and_tick_health(self) -> None:
        outputs, calls, seen = self.run_statuses(
            "/tmp/native-run\n", "failing: no instance", 1,
            units=("hook", "native"),
            panes=({"hook": "● Check hook progress\nPhase ETA: 12:00 EDT\n— holding: running\n",
                    "native": "● Check herdr progress\nPhase ETA: 13:00 EDT\n— gate: approve release\n"},),
        )
        output = outputs[0]
        self.assertIn("== hook", output)
        self.assertIn("● Check hook progress", output)
        self.assertIn("== native", output)
        self.assertIn("● Check herdr progress", output)
        self.assertIn("Phase ETA: 13:00 EDT", output)
        self.assertIn("=== DECISION for you from native ===", output)
        self.assertNotIn("CLAUDE NOT RUNNING", output)
        self.assertNotIn("SESSION GONE", output)
        self.assertEqual(calls, "health unit-native-run\n" * 2)
        self.assertEqual(seen, "native|— gate: approve release\n")
