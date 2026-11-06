"""Status discovery retains healthy units when process evidence is incomplete."""
from __future__ import annotations

from contextlib import redirect_stderr
from dataclasses import replace
from io import StringIO
from unittest import mock

from scripts.production import session_staff as labels, staff
from scripts.production.staff_test_support import StaffTestCase, fixture_runtime_id, unit_membership
import herdr
from pane_launch import PaneLaunch


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
                                                            sources=labels.MemberSources((launch,)))
                self.assertEqual(len(errors.getvalue().splitlines()), 1)
                self.assertIn("anomaly:", errors.getvalue())
                states = grouped.status_units("show")
                self.assertIsInstance(states["broken"].claude, labels.ClaudeUnknown)
                self.assertIsInstance(states["healthy"].claude, labels.ClaudeNotRunning)

    def test_agent_children_select_the_agent_without_an_anomaly(self) -> None:
        row = unit_membership("show", "unit")
        row = staff.update_current_session(row.current_session, staff.native_id(fixture_runtime_id("claim-after-clear")))
        place = labels.Place("agents", "w1:p1")
        children = [labels.LabeledSession("", labels.ClaudeUnknown("child process"), place,
                    labels.ObservedProcess(910001 + index, command), labels.LiveSession(row.current_session))
                    for index, command in enumerate(("bevy_brp_mcp", "zsh", "python3", "sleep", "sh", "codex-code-mode-host"))]
        for command in ("claude", "codex"):
            with self.subTest(agent=command):
                agent = replace(children[0], process=labels.ObservedProcess(910099, command))
                errors = StringIO()
                with redirect_stderr(errors):
                    grouped = labels.group_labeled_sessions([*children, agent], record=staff.snapshot(),
                                                            sources=labels.MemberSources(()))
                self.assertEqual(errors.getvalue(), "")
                self.assertEqual(grouped.units, {"show": {row.name: replace(agent, unit=row.name)}})
