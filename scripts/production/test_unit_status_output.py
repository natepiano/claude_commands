"""Unit-status integration tests, grouped by subject."""
from __future__ import annotations

from scripts.production.unit_status_test_support import UnitStatusFixture, setUpModule as setUpModule
from scripts.production.unit_status import Decision, StillWaiting


class UnitStatusTests(UnitStatusFixture):
    def test_the_last_three_distinct_eta_lines_are_printed_in_pane_order(self) -> None:
        pane = "\n".join(("ETA 09:00", "ETA 10:00 PDT", "  Phase ETA: 12:55 EDT", "ETA 11:00",
                          "From the user: what is the ETA", "ETA 10:00 PDT", "— holding: waiting on x")) + "\n"
        outputs, _, _ = self.run_statuses(None, "ok", 0, panes=({"hook": pane},))
        stated = [line for line in outputs[0].splitlines() if "ETA" in line]
        self.assertEqual(stated, ["Phase ETA: 12:55 EDT", "ETA 11:00", "ETA 10:00 PDT"])

    def test_real_gate_outputs_become_status_flags_or_activity(self) -> None:
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
        peer_routed = self.status_block(peer_outputs[0], "hook", ("hook", "peer"))
        self.assertEqual(peer_routed.flags, ())
        self.assertTrue(any("gate: peer" in line.text for line in peer_routed.activity))
