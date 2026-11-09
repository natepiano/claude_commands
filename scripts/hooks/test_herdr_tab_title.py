"""The title hook uses native staff identities and preserves ad hoc title behavior."""

from __future__ import annotations

import io
import json
import math
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from typing import override
from unittest.mock import patch

from scripts.lib import names
from scripts.lib.test_names import fixture_names
from scripts.lib.atomic_file import write_text_atomic
from scripts.production import staff
from scripts.production.staff_test_support import fixture_member, fixture_runtime_id, fixture_worker, isolated_staff_file
from scripts.shutdown import pane_record
from scripts.herdr import herdr  # noqa: E402
from scripts.message import sessions  # noqa: E402
from scripts.hooks import herdr_tab_title  # noqa: E402
from scripts.hooks.herdr_tab_title import StopPayload  # noqa: E402

REAL_RETITLE = herdr.Herdr.retitle
REAL_PANES = herdr.Herdr.panes


class HerdrTabTitleTests(unittest.TestCase):
    records: Path = Path()
    staff_file: Path = Path()
    retitled: list[tuple[str, str]] = []
    fault: bool = False

    @override
    def setUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.staff_file = root / "staff.json"
        _ = self.enterContext(isolated_staff_file(self.staff_file, {}))
        self.records = root / "sessions"
        self.records.mkdir()
        _ = self.enterContext(patch.object(pane_record, "RECORD_DIR", root / "panes"))
        (root / "config/herdr").mkdir(parents=True)
        _ = (root / "config/herdr/shared-session").write_text("agents\n")
        self.enterContext(patch.dict("os.environ", {
            "HOME": str(root / "home"),
            "NOTIFIER_SESSIONS_DIR": str(self.records), "NOTIFIER_STATE_DIR": str(root / "notifier"),
            "HERDR_BIN": "/nonexistent/herdr",
            "XDG_CONFIG_HOME": str(root / "config"), "XDG_STATE_HOME": str(root / "state"),
            "SHOWRUNNER_" + "UNIT": "", "SHOWRUNNER_" + "UNIT_ID": ""}))
        _ = fixture_member("showrunner", name="fixture-owner")
        _ = self.enterContext(patch.object(names, "runtime_names", return_value=fixture_names()))
        self.record(41, "session-a", "showrunner-fixer")
        self.record(42, "session-b", "send-relay", entrypoint="sdk-cli")
        self.retitled, self.fault = [], False
        _ = self.enterContext(patch.object(herdr.Herdr, "panes", return_value=[{
            "pane_id": "w3:p7", "tab_id": "w3:t2", "workspace_id": "w3", "label": "original"}]))
        _ = self.enterContext(patch.object(herdr.Herdr, "retitle", autospec=True, side_effect=self.fake_retitle))

    def fake_retitle(self, _client: herdr.Herdr, pane: str, title: str, *, tab: bool = True) -> bool:
        self.assertTrue(tab)
        self.retitled.append((pane, title))
        if self.fault:
            raise herdr.HerdrFailure("server gone")
        return True

    def record(self, pid: int, session_id: str, name: str, entrypoint: str = "cli", *, proc_start: str = "") -> None:
        _ = (self.records / f"{pid}.json").write_text(json.dumps({
            "pid": pid, "sessionId": session_id, "name": name, "messagingSocketPath": f"/tmp/{pid}.sock",
            "updatedAt": 1, "entrypoint": entrypoint, "procStart": proc_start}))

    def retained_launch(self, session_id: str) -> Path:
        entry = pane_record.launch("w3:p7", session_id, str(self.records.parent), {}, ["claude"])
        entry.update(pid=41, proc_start="fixture-start")
        path = pane_record.record_path("agents")
        write_text_atomic(path, json.dumps({"w3:p7": entry}))
        return path

    def renamed_unit(self) -> staff.StaffRow:
        parent = fixture_member("showrunner", name="tools")
        unit = fixture_member("unit", name="roles", parent=parent.id)
        unit = staff.rename(unit.id, "render")
        return staff.update_current_session(unit.current_session, staff.native_id(fixture_runtime_id("unit-after-clear")))

    def test_unit_mismatch_uses_the_current_native_row_and_prints_the_rename_command(self) -> None:
        unit = self.renamed_unit()
        self.assertNotEqual(unit.id, unit.current_session)
        self.record(41, unit.current_session, "typed-name")
        environment = {"HERDR_PANE_ID": "w3:p7", "HERDR_SESSION": "agents",
                       "STAFF_ID": "inherited-worker", "SHOWRUNNER_" + "UNIT_ID": "old-unit",
                       "CLAUDE_CODE_SESSION_ID": "inherited-session"}
        before = self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns
        with (redirect_stderr(io.StringIO()) as errors,
              patch.object(staff, "snapshot", wraps=staff.snapshot) as reads):
            self.assertTrue(herdr_tab_title.sync({"session_id": unit.current_session}, environment))
        self.assertEqual(reads.call_count, 1)
        self.assertEqual(self.retitled, [("w3:p7", "render")])
        self.assertIn("/agents rename", errors.getvalue())
        self.assertIn("typed-name", errors.getvalue())
        self.assertIn("render", errors.getvalue())
        self.assertEqual(len(errors.getvalue().splitlines()), 1)
        self.assertEqual(herdr_tab_title.current_name(unit.current_session), "typed-name")
        self.assertEqual((self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns), before)

    def test_a_matching_unit_name_prints_no_rename_command(self) -> None:
        unit = self.renamed_unit()
        self.record(41, unit.current_session, unit.name)
        before = self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns
        with redirect_stderr(io.StringIO()) as errors:
            self.assertTrue(herdr_tab_title.sync({"session_id": unit.current_session},
                                               {"HERDR_PANE_ID": "w3:p7", "HERDR_SESSION": "agents"}))
        self.assertEqual(self.retitled, [("w3:p7", unit.name)])
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual((self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns), before)

    def test_showrunner_titles_keep_the_row_name_despite_inherited_session_labels(self) -> None:
        runner = fixture_member("showrunner", name="tools")
        runner = staff.update_current_session(runner.current_session, staff.native_id(fixture_runtime_id("runner-after-clear")))
        self.record(41, runner.current_session, "typed-production")
        before = self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns
        with redirect_stderr(io.StringIO()) as errors, patch.object(herdr.Herdr, "panes", return_value=[
                {"pane_id": "w3:p7", "tab_id": "w3:t2"}, {"pane_id": "w3:p8", "tab_id": "w3:t2"}]):
            self.assertTrue(herdr_tab_title.sync({"session_id": runner.current_session},
                {"HERDR_PANE_ID": "w3:p7", "HERDR_SESSION": "agents", "SHOWRUNNER_" + "UNIT_ID": "wrong-unit"}))
        self.assertEqual(self.retitled, [("w3:p7", runner.name)])
        self.assertNotIn("/agents rename", errors.getvalue())
        self.assertEqual(herdr_tab_title.current_name(runner.current_session), "typed-production")
        self.assertEqual((self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns), before)

    def test_nonstaff_pane_in_a_shared_tab_keeps_the_pane_and_tab_labels(self) -> None:
        from scripts.herdr.test_worker_panes import PaneWorld

        world = PaneWorld()
        world.locations["w3:p7"] = {"pane_id": "w3:p7", "tab_id": "w3:t2", "workspace_id": "w3",
                                    "terminal_id": "friend-terminal", "label": "friend"}
        world.locations["w3:p8"] = {"pane_id": "w3:p8", "tab_id": "w3:t2", "workspace_id": "w3",
                                    "terminal_id": "caller-terminal", "label": "caller"}
        before = {key: value.copy() for key, value in world.locations.items()}
        with patch.object(herdr, "Herdr", return_value=world), patch.object(
                world, "panes", side_effect=lambda: REAL_PANES(world)):
            changed = herdr_tab_title.sync({"session_id": "session-a"},
                {"HERDR_PANE_ID": "w3:p7", "HERDR_SESSION": "agents"})
        self.assertFalse(changed)
        self.assertEqual(world.locations, before)
        self.assertEqual(world.commands, [])
        self.assertEqual(self.retitled, [])

    def test_worker_renames_only_its_pane_and_a_rerun_changes_nothing(self) -> None:
        unit = self.renamed_unit()
        worker = fixture_worker("impl1", unit.id)
        worker = staff.update_current_session(worker.current_session, staff.native_id(fixture_runtime_id("worker-after-clear")))
        self.record(41, worker.current_session, "typed-worker")
        environment = {"HERDR_PANE_ID": "w3:p7", "HERDR_SESSION": "agents",
                       "STAFF_ID": str(unit.id), "SHOWRUNNER_" + "UNIT_ID": "render-unit"}
        pane: herdr.PaneIdentity = {"pane_id": "w3:p7", "label": "typed-worker",
                                   "workspace_id": "w3", "session_id": worker.current_session}
        before = self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns

        def rename(_client: herdr.Herdr, *arguments: str, deadline: float = math.inf) -> str:
            del deadline
            self.assertEqual(arguments, ("pane", "rename", "w3:p7", "render-impl1"))
            pane["label"] = arguments[-1]
            return ""

        def result(_client: herdr.Herdr, *arguments: str, deadline: float = math.inf) -> dict[str, object]:
            del deadline
            self.assertEqual(arguments, ("pane", "list"))
            return {"panes": [pane]}

        with (patch.object(herdr.Herdr, "retitle", new=REAL_RETITLE),
              patch.object(herdr.Herdr, "result", autospec=True, side_effect=result),
              patch.object(herdr.Herdr, "save_layout"),
              patch.object(herdr.Herdr, "run", autospec=True, side_effect=rename) as writes,
              redirect_stderr(io.StringIO()) as errors):
            self.assertTrue(herdr_tab_title.sync({"session_id": worker.current_session}, environment))
            self.assertFalse(herdr_tab_title.sync({"session_id": worker.current_session}, environment))
        self.assertEqual(pane["label"], "render-impl1")
        self.assertEqual(writes.call_count, 1)
        self.assertEqual(self.retitled, [])
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual((self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns), before)

    def test_missing_row_keeps_ad_hoc_behavior_without_a_problem_line(self) -> None:
        before = self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns
        with (redirect_stderr(io.StringIO()) as errors,
              patch.object(staff, "snapshot", wraps=staff.snapshot) as reads):
            self.assertTrue(herdr_tab_title.sync({"session_id": "session-a"},
                {"HERDR_PANE_ID": "w3:p7", "HERDR_SESSION": "agents",
                 "STAFF_ID": "inherited-unit", "CLAUDE_CODE_SESSION_ID": "inherited-session"}))
        self.assertEqual(reads.call_count, 1)
        self.assertEqual(self.retitled, [("w3:p7", "showrunner-fixer")])
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual((self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns), before)

    def test_delayed_clear_reports_a_missing_row_until_the_native_handoff_finishes(self) -> None:
        unit = self.renamed_unit()
        pending = staff.native_id(fixture_runtime_id("pending-clear"))
        self.record(41, pending, "typed-name", proc_start="fixture-start")
        retained = self.retained_launch(unit.id)
        retained_before = retained.read_bytes(), retained.stat().st_mtime_ns
        environment = {"HERDR_PANE_ID": "w3:p7", "HERDR_SESSION": "agents",
                       "STAFF_ID": str(unit.id), "CLAUDE_CODE_SESSION_ID": unit.current_session}
        before = self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns
        with (redirect_stderr(io.StringIO()) as errors,
              patch.object(sessions, "process_start", return_value="fixture-start"),
              patch.object(staff, "snapshot", wraps=staff.snapshot) as reads):
            self.assertTrue(herdr_tab_title.sync({"session_id": pending}, environment))
        self.assertEqual(reads.call_count, 1)
        self.assertIn("no staff row", errors.getvalue())
        self.assertIn(pending, errors.getvalue())
        self.assertNotIn("/agents rename", errors.getvalue())
        self.assertEqual(self.retitled, [("w3:p7", "typed-name")])
        self.assertEqual((self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns), before)
        self.assertEqual((retained.read_bytes(), retained.stat().st_mtime_ns), retained_before)
        _ = staff.update_current_session(unit.current_session, pending)
        completed = self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns
        with redirect_stderr(io.StringIO()) as errors:
            self.assertTrue(herdr_tab_title.sync({"session_id": pending}, environment))
        self.assertEqual(self.retitled[-1], ("w3:p7", unit.name))
        self.assertIn("/agents rename", errors.getvalue())
        self.assertEqual((self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns), completed)
        self.assertEqual((retained.read_bytes(), retained.stat().st_mtime_ns), retained_before)

    def test_a_different_process_does_not_report_an_incomplete_clear(self) -> None:
        unit = self.renamed_unit()
        pending = fixture_runtime_id("unrelated-process")
        retained = self.retained_launch(unit.id)
        before = self.staff_file.read_bytes(), retained.read_bytes()
        cases = (
            (42, "fixture-start", "fixture-start"),
            (41, "different-start", "different-start"),
            (41, "fixture-start", "different-start"),
            (41, "", "fixture-start"),
        )
        for pid, registry_start, live_start in cases:
            with (self.subTest(pid=pid, registry_start=registry_start, live_start=live_start),
                  redirect_stderr(io.StringIO()) as errors,
                  patch.object(sessions, "process_start", return_value=live_start)):
                self.record(pid, pending, "ad-hoc", proc_start=registry_start)
                self.assertTrue(herdr_tab_title.sync({"session_id": pending},
                    {"HERDR_PANE_ID": "w3:p7", "HERDR_SESSION": "agents"}))
                self.assertEqual(errors.getvalue(), "")
                self.assertEqual(self.retitled[-1], ("w3:p7", "ad-hoc"))
        self.assertEqual((self.staff_file.read_bytes(), retained.read_bytes()), before)

    def test_an_unstaffed_clear_keeps_ad_hoc_titles_without_a_problem_line(self) -> None:
        pending = fixture_runtime_id("unstaffed-clear")
        self.record(41, pending, "ad-hoc", proc_start="fixture-start")
        retained = self.retained_launch(fixture_runtime_id("unstaffed-launch"))
        before = self.staff_file.read_bytes(), retained.read_bytes()
        with (redirect_stderr(io.StringIO()) as errors,
              patch.object(sessions, "process_start", return_value="fixture-start")):
            self.assertTrue(herdr_tab_title.sync({"session_id": pending},
                {"HERDR_PANE_ID": "w3:p7", "HERDR_SESSION": "agents"}))
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(self.retitled, [("w3:p7", "ad-hoc")])
        self.assertEqual((self.staff_file.read_bytes(), retained.read_bytes()), before)

    def test_an_absent_staff_file_keeps_ad_hoc_titles_without_creating_state(self) -> None:
        self.staff_file.unlink()
        with redirect_stderr(io.StringIO()) as errors:
            self.assertTrue(herdr_tab_title.sync({"session_id": "session-a"},
                                               {"HERDR_PANE_ID": "w3:p7", "HERDR_SESSION": "agents"}))
        self.assertEqual(self.retitled, [("w3:p7", "showrunner-fixer")])
        self.assertEqual(errors.getvalue(), "")
        self.assertFalse(self.staff_file.exists())

    def test_a_fixed_member_id_cannot_select_its_new_native_session(self) -> None:
        unit = self.renamed_unit()
        self.record(41, unit.id, "old-session")
        before = self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns
        with redirect_stderr(io.StringIO()) as errors:
            self.assertTrue(herdr_tab_title.sync({"session_id": unit.id},
                                               {"HERDR_PANE_ID": "w3:p7", "HERDR_SESSION": "agents"}))
        self.assertEqual(self.retitled, [("w3:p7", "old-session")])
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual((self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns), before)

    def test_an_unreadable_staff_record_reports_the_problem_without_changing_a_title(self) -> None:
        write_text_atomic(self.staff_file, "{broken")
        before = self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns
        with (patch.dict("os.environ", {"HERDR_PANE_ID": "w3:p7", "HERDR_SESSION": "agents"}),
              patch("sys.stdin", io.StringIO(json.dumps({"session_id": "session-a"}))),
              redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(herdr_tab_title.main(), 0)
        self.assertIn("cannot read staff", errors.getvalue())
        self.assertEqual(len(errors.getvalue().splitlines()), 1)
        self.assertEqual(self.retitled, [])
        self.assertEqual((self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns), before)

    def test_a_shared_pane_takes_its_session_name(self) -> None:
        pane = {"HERDR_PANE_ID": "w3:p7", "HERDR_SESSION": "agents"}
        self.assertTrue(herdr_tab_title.sync({"session_id": "session-a"}, pane))
        self.assertEqual(self.retitled, [("w3:p7", "showrunner-fixer")])

    def test_a_pane_of_the_default_shared_session_takes_its_session_name_with_no_file(self) -> None:
        with patch.dict("os.environ", {"XDG_CONFIG_HOME": str(self.records.parent / "unset")}):
            self.assertTrue(herdr_tab_title.sync({"session_id": "session-a"},
                                                 {"HERDR_PANE_ID": "w3:p7", "HERDR_SESSION": herdr.SHARED_SESSION}))
        self.assertEqual(self.retitled, [("w3:p7", "showrunner-fixer")])

    def test_other_places_and_unknown_sessions_change_nothing(self) -> None:
        cases: tuple[tuple[StopPayload, dict[str, str]], ...] = (
            ({"session_id": "session-a"}, {}),
            ({"session_id": "session-a"}, {"HERDR_PANE_ID": "w1:p1", "HERDR_SESSION": "own-session"}),
            ({"session_id": "session-a"}, {"HERDR_PANE_ID": "w1:p1", "HERDR_SESSION": "agents", "TMUX_PANE": "%1"}),
            ({"session_id": "session-b"}, {"HERDR_PANE_ID": "w1:p1", "HERDR_SESSION": "agents"}),
            ({"session_id": "session-z"}, {"HERDR_PANE_ID": "w1:p1", "HERDR_SESSION": "agents"}),
            ({}, {"HERDR_PANE_ID": "w1:p1", "HERDR_SESSION": "agents"}),
        )
        for payload, environment in cases:
            with self.subTest(payload=payload, environment=environment):
                self.assertFalse(herdr_tab_title.sync(payload, environment))
        self.assertEqual(self.retitled, [])

    def test_a_herdr_fault_never_stops_the_turn(self) -> None:
        self.fault = True
        with (patch.dict("os.environ", {"HERDR_PANE_ID": "w3:p7", "HERDR_SESSION": "agents"}),
              patch("sys.stdin", io.StringIO(json.dumps({"session_id": "session-a"})))):
            self.assertEqual(herdr_tab_title.main(), 0)
        self.assertEqual(self.retitled, [("w3:p7", "showrunner-fixer")])

    def test_turn_end_preserves_session_name_and_rename_updates_tab_and_pane(self) -> None:
        environment = {"HERDR_PANE_ID": "w3:p7", "HERDR_SESSION": "agents",
                       "SHOWRUNNER_" + "UNIT": "sdlc", "SHOWRUNNER_" + "UNIT_ID": "roles-unit"}
        pane: dict[str, object] = {"pane_id": "w3:p7", "tab_id": "w3:t2", "workspace_id": "w3",
                                   "label": "showrunner-fixer"}
        tab: dict[str, object] = {"tab_id": "w3:t2", "label": "showrunner-fixer"}

        def result(_client: herdr.Herdr, *arguments: str, deadline: float = math.inf) -> dict[str, object]:
            del deadline
            return {"panes": [pane]} if arguments == ("pane", "list") else {"tabs": [tab]}

        def run(_client: herdr.Herdr, *arguments: str, deadline: float = math.inf) -> str:
            del deadline
            target = pane if arguments[0] == "pane" else tab
            target["label"] = arguments[-1]
            return ""

        with (patch.object(herdr.Herdr, "retitle", new=REAL_RETITLE),
              patch.object(herdr.Herdr, "result", autospec=True, side_effect=result) as reads,
              patch.object(herdr.Herdr, "run", autospec=True, side_effect=run) as writes,
              patch.object(herdr.Herdr, "order_tabs", return_value={}) as ordered,
              patch.object(herdr.Herdr, "save_layout") as saved,
              patch.object(herdr, "saved_layout", return_value=herdr.SavedLayout(at=0.0, workspaces=[]))):
            self.assertFalse(herdr_tab_title.sync({"session_id": "session-a"}, environment))
            self.assertEqual(reads.call_count, 2)
            writes.assert_not_called()
            ordered.assert_not_called()
            saved.assert_not_called()
            self.record(41, "session-a", "renamed-director")
            self.assertTrue(herdr_tab_title.sync({"session_id": "session-a"}, environment))
            self.assertEqual(pane["label"], "renamed-director")
            self.assertEqual(tab["label"], "renamed-director")
            self.assertEqual(writes.call_count, 2)
            ordered.assert_called_once()
            saved.assert_called_once()

    def test_showrunner_title_uses_session_name_after_session_rename(self) -> None:
        state = self.records.parent / "notifier"
        instance = state / "showrunner-sdlc"
        instance.mkdir(parents=True)
        _ = (instance / "conf").write_text(
            "TARGET=session:session-a\nCHECK=/scripts/production_check.sh fixture-owner\n")
        environment = {"HERDR_PANE_ID": "w3:p7", "HERDR_SESSION": "agents", "NOTIFIER_STATE_DIR": str(state)}
        self.record(41, "session-a", "renamed-showrunner")
        self.assertTrue(herdr_tab_title.sync({"session_id": "session-a"}, environment))
        self.assertEqual(self.retitled, [("w3:p7", "renamed-showrunner")])

    def test_showrunner_unit_director_and_plain_tabs_use_only_the_session_name(self) -> None:
        state = self.records.parent / "notifier/showrunner-sdlc"
        state.mkdir(parents=True)
        _ = (state / "conf").write_text(
            "TARGET=session:showrunner-id\nCHECK=/scripts/production_check.sh fixture-owner\n")
        cases: tuple[tuple[str, str, dict[str, str]], ...] = (
            ("showrunner-id", "control", {}),
            ("director-id", "roles", {"SHOWRUNNER_" + "UNIT": "sdlc", "SHOWRUNNER_" + "UNIT_ID": "roles-unit"}),
            ("plain-id", "scratch", {}))
        for pid, (session_id, name, marks) in enumerate(cases, 50):
            with self.subTest(session=session_id):
                self.record(pid, session_id, name)
                environment = {"HERDR_PANE_ID": "w3:p7", "HERDR_SESSION": "agents", **marks}
                self.assertTrue(herdr_tab_title.sync({"session_id": session_id}, environment))
                title = self.retitled[-1][1]
                self.assertEqual(title, name)
                for added in (" · ", "sdlc", "showrunner", "unit director"):
                    self.assertNotIn(added, title)


if __name__ == "__main__":
    _ = unittest.main()
