"""Status discovery retains healthy units when process evidence is incomplete."""
from __future__ import annotations

from contextlib import redirect_stderr
from dataclasses import replace
from io import StringIO
from unittest import mock

from scripts.production import session_staff as labels, staff
from scripts.production.staff_test_support import StaffTestCase, unit_membership
from scripts.herdr import herdr
from scripts.herdr.pane_launch import PaneLaunch


class StatusDiscoveryTests(StaffTestCase):
    def test_unknown_process_identity_preserves_healthy_unit_status(self) -> None:
        import unit_lookup

        broken = unit_membership("show", "broken")
        healthy = unit_membership("show", "healthy")
        place = labels.Place("agents", "w1:p1")
        launch = PaneLaunch(pane_id=place.pane, session_id=broken.current_session, config_dir="", cwd="",
                            name="", model="", remote_control=False, environment={}, flags_read=True, written_at="")
        for marks in ({}, {"CLAUDE_CODE_SESSION_ID": "unregistered"}):
            with self.subTest(marks=marks):
                processes = [herdr.HerdrProcess(910001, place, marks, "claude", ""),
                             herdr.HerdrProcess(910002, labels.Place("agents", "w2:p1"),
                                               {"CLAUDE_CODE_SESSION_ID": healthy.current_session}, "zsh", "")]
                errors = StringIO()
                with mock.patch.object(herdr, "live_sessions", return_value=[{"name": "agents"}]), redirect_stderr(errors):
                    observed = tuple(unit_lookup._herdr_labeled({}, unit_lookup.UnreadAt({}, unit_lookup.RecordsRead()), processes))  # pyright: ignore[reportPrivateUsage]
                    grouped = labels.group_labeled_sessions(observed, record=staff.snapshot(),
                                                            sources=labels.MemberSources({}, (launch,)))
                self.assertEqual(len(errors.getvalue().splitlines()), 1)
                self.assertIn("anomaly:", errors.getvalue())
                states = grouped.status_units("show")
                self.assertIsInstance(states["broken"].claude, labels.ClaudeUnknown)
                self.assertIsInstance(states["healthy"].claude, labels.ClaudeNotRunning)

    def test_duplicate_unknown_and_stopped_processes_have_no_selected_pane(self) -> None:
        from scripts.production import live_units

        row = unit_membership("show", "broken")
        healthy = unit_membership("show", "healthy")
        for state in (labels.ClaudeUnknown("missing record"), labels.ClaudeNotRunning()):
            with self.subTest(state=state):
                first = labels.LabeledSession("", state, labels.Place("agents", "w1:p1"),
                    labels.ObservedProcess(910001, "zsh"), labels.LiveSession(row.current_session))
                second = replace(first, place=labels.Place("agents", "w2:p1"), process=labels.ObservedProcess(910002, "zsh"))
                sibling = replace(first, place=labels.Place("agents", "w3:p1"), identity=labels.LiveSession(healthy.current_session))
                errors = StringIO()
                with redirect_stderr(errors):
                    grouped = labels.group_labeled_sessions([first, second, sibling], record=staff.snapshot(),
                                                            sources=labels.MemberSources({}, ()))
                self.assertEqual(len(errors.getvalue().splitlines()), 1)
                self.assertIn("broken", grouped.faults.get("show", {}), repr(grouped))
                states = {unit.unit: unit.session for unit in live_units._joined("show", grouped)}  # pyright: ignore[reportPrivateUsage]
                self.assertTrue(all(unit is not None for unit in states.values()))
                broken = states["broken"]
                sibling_state = states["healthy"]
                assert broken is not None and sibling_state is not None
                self.assertIsInstance(broken.claude, labels.ClaudeUnknown)
                self.assertEqual(broken.place, labels.Place("", ""))
                self.assertIsInstance(broken.process, labels.ProcessUnobserved)
                self.assertEqual(sibling_state.claude, state)
