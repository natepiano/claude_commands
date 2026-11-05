"""Status discovery retains healthy units when process evidence is incomplete."""
from __future__ import annotations

from contextlib import redirect_stderr
from dataclasses import replace
import os
from pathlib import Path
import re
from io import StringIO
from unittest import mock

from scripts.lib.atomic_file import write_text_atomic
from scripts.production import session_staff as labels, staff, staff_sources
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
                                                            sources=labels.MemberSources({}, (launch,)))
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
                                                            sources=labels.MemberSources({}, ()))
                self.assertEqual(errors.getvalue(), "")
                self.assertEqual(grouped.units, {"show": {row.name: replace(agent, unit=row.name)}})

    def test_configured_morty_thread_is_placed_but_another_codex_thread_is_an_anomaly(self) -> None:
        self.write_json(self.staff_file, {"schema": 2, "rows": []})
        morty = fixture_runtime_id("morty-thread")
        other = fixture_runtime_id("other-codex-thread")
        config = self.root / "config"
        config.mkdir()
        source = Path(__file__).resolve().parents[2] / "config/send.conf"
        text = re.sub(r"(?m)^MORTY_NATIVE_ID=.*$", f"MORTY_NATIVE_ID=codex:{morty}", source.read_text())
        write_text_atomic(config / "send.conf", text)
        observed = [labels.LabeledSession("", labels.ClaudeUnknown("no registered Claude"),
                    labels.Place("agents", f"w{index}:p1"), labels.ObservedProcess(910100 + index, "codex"),
                    labels.LiveSession(identity)) for index, identity in enumerate((morty, other))]
        errors = StringIO()
        with (mock.patch.dict(os.environ, {"CONF_DIR": str(config)}), redirect_stderr(errors),
              labels.anomalies(staff_sources.CodexProcessThreads().check) as lines):
            grouped = labels.group_labeled_sessions(observed, record=staff.snapshot(),
                                                    sources=labels.MemberSources({}, ()))
        self.assertNotIn(morty, "\n".join(lines))
        self.assertIn(other, "\n".join(lines))
        self.assertEqual(len(lines), 1)
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(grouped.units, {})
        with (mock.patch.dict(os.environ, {"CONF_DIR": str(config)}), redirect_stderr(errors),
              mock.patch.object(staff_sources.CodexProcessThreads, "thread") as recover):
            ordinary = labels.group_labeled_sessions(observed, record=staff.snapshot(),
                                                      sources=labels.MemberSources({}, ()))
        self.assertIn(morty, errors.getvalue())
        self.assertIn(other, errors.getvalue())
        self.assertEqual(len(errors.getvalue().splitlines()), 2)
        self.assertEqual(ordinary.units, {})
        recover.assert_not_called()

    def test_morty_without_environment_identity_never_uses_the_retained_unit_launch(self) -> None:
        row = unit_membership("show", "unit")
        place = labels.Place("agents", "w2C")
        launch = PaneLaunch(pane_id=place.pane, session_id=row.current_session, config_dir="", cwd="",
                            name="", model="", remote_control=False, environment={}, flags_read=True, written_at="")
        unknown = labels.LabeledSession("", labels.ClaudeUnknown("no identity"), place,
                                        labels.ObservedProcess(198033, "codex"))
        morty = fixture_runtime_id("retained-pane-morty")
        from scripts.message import codex_daemon

        sessions = [codex_daemon.DaemonSession(morty, "morty", str(self.root), "idle")]

        def configured_morty(identity: str) -> bool:
            return identity == morty

        with (mock.patch.object(herdr, "shared_session", return_value=place.session),
              mock.patch.object(herdr, "process_environment", return_value={}),
              mock.patch.object(herdr, "herdr_place_of", return_value=place),
              mock.patch.object(herdr.Herdr, "result", return_value={"pane": {"terminal_title_stripped": "morty | idle"}}),
              mock.patch.object(codex_daemon, "loaded_sessions", return_value=sessions),
              mock.patch.object(labels, "morty_thread", side_effect=configured_morty),
              labels.anomalies(staff_sources.CodexProcessThreads().check) as lines):
            grouped = labels.group_labeled_sessions([unknown], record=staff.snapshot(),
                                                    sources=labels.MemberSources({}, (launch,)))
        self.assertEqual(grouped.status_units("show"), {})
        self.assertEqual(lines, [])

    def test_observations_from_both_readers_share_one_process_line(self) -> None:
        from scripts.message import process_table

        self.write_json(self.staff_file, {"schema": 2, "rows": []})
        other = fixture_runtime_id("two-readers-codex")
        unknown = labels.LabeledSession("", labels.ClaudeUnknown("no identity"), labels.Place("agents", "w2C"),
                                        labels.ObservedProcess(294399, "codex"), labels.LiveSession(other))
        table = {294399: process_table.Process(294399, 1, "codex", "codex")}
        with (mock.patch.object(process_table, "snapshot", return_value=table),
              mock.patch.object(herdr, "process_environment", return_value={"CODEX_THREAD_ID": other}),
              labels.anomalies() as lines):
            _ = labels.group_labeled_sessions([unknown], record=staff.snapshot(), sources=labels.MemberSources({}, ()))
            staff_sources.unplaced_processes(staff.snapshot(), labels.MemberSources({}, ()))
            labels.report_anomaly("registry is unreadable")
        self.assertEqual(len(lines), 2, lines)
        self.assertIn("in herdr:agents:w2C", lines[0])
        self.assertIn(f"pid 294399 (codex): {other}: no current staff row", lines[0])
        self.assertEqual(lines[1], "anomaly: registry is unreadable")

    def test_two_orphan_processes_have_separate_lines(self) -> None:
        row = replace(unit_membership("show", "unit"), parent=staff.StaffRoot())
        record = staff.StaffSnapshot((row,))
        observed = [labels.LabeledSession("", labels.ClaudeUnknown("not registered"), labels.Place("agents", "w2C"),
                    labels.ObservedProcess(pid, "codex"), labels.LiveSession(row.current_session)) for pid in (1001, 1002)]
        with labels.anomalies() as lines:
            _ = labels.group_labeled_sessions(observed, record=record, sources=labels.MemberSources({}, ()))
        self.assertEqual(len(lines), 2)
        self.assertTrue(all("no showrunner staff parent" in line for line in lines))
