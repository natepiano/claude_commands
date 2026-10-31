"""Rename retains the director terminal through a clean stop and shared resume."""
import json
from dataclasses import asdict
from pathlib import Path
from typing import cast
from unittest import mock

from scripts.production.rename_unit_test_support import UnitRenameFixture

import herdr
import rename_unit
from rename_state import RenameRefused
import unit_lookup
import unit_names
from scripts.shutdown import stop_resume
from scripts.herdr.fake_herdr import CommandResponse
from scripts.production import staff
from scripts.production import fake_unit_host

from scripts.production.fake_unit_host import PaneShellFixture, record_shutdown, assert_shared_stop_record


class RenamePaneShellTests(UnitRenameFixture):
    def test_seed_inherits_shared_stop_record_defaults(self) -> None:
        assert_shared_stop_record(self, self.seed_record,
            self.home / '.local/state/shutdown/owner@example.com/sessions/sid-alpha.json')

    def pane_shell(self, *, live: bool) -> None:
        _ = self.seed_record(progress='ready' if live else 'stopped')
        self.director.configure("demo", "alpha-unit", self.rust / "claude-demo-alpha", live=live)
        pane = self.director.pane
        record_shutdown(self, pane, self.home / '.local/state/shutdown/owner@example.com/sessions/sid-alpha.json',
                        destination=('demo','gamma-unit','gamma'))
        self.assert_ok(self.rename('apply'))
        self.assert_ok(self.rename('verify'))
        self.assertEqual(pane.close_calls(), [])
        rows = herdr.Herdr("agents").panes()
        self.assertEqual((rows[0]["pane_id"], rows[0].get("terminal_id"), rows[0].get("tab_id")),
                         ("w1:p1", "terminal-alpha", "w1:t1"))
        self.assertEqual(sum(call.startswith('resume') for call in pane.stop_calls), 1)
        self.assertTrue((self.home / 'worktrees/gamma').is_dir())
        self.assertIn('| gamma-unit |', self.doc.read_text())
        self.assertIs(self.journal()['done'], True)

    def recovery_panes(self) -> tuple[PaneShellFixture, rename_unit.UnitRename]:
        _ = self.seed_record(progress="ready")
        self.director.configure("demo", "alpha-unit", self.rust / "claude-demo-alpha", live=True)
        pane = self.director.pane
        renamed = rename_unit.plan("demo", "alpha-unit", "gamma-unit", self.root / "scratch")
        renamed.session_id = "sid-alpha"
        row = next(row for row in staff.find(kind="unit") if row.name == "alpha")
        renamed.staff_id = row.id
        _ = staff.rename(row.id, "gamma")
        record_shutdown(self, pane, self.home / ".local/state/shutdown/owner@example.com/sessions/sid-alpha.json",
                        destination=("demo", "gamma-unit", "gamma"))
        return pane, renamed

    def publish_stopped_panes(self, pane: PaneShellFixture, renamed: rename_unit.UnitRename,
                              conversations: tuple[str | None, str | None], *,
                              panes: list[dict[str, object]] | None = None) -> unit_lookup.MarkedUnit:
        _ = self.enterContext(mock.patch.object(herdr.Herdr, "result", fake_unit_host.PANE_RESULT))
        fake_unit_host.write(self.env, [
            fake_unit_host.HostedUnit("agents", "w1:p1", pane.marks(), label="alpha"),
            fake_unit_host.HostedUnit("agents", "w1:p2", pane.marks(), label="alpha"),
        ])
        responses_path = Path(self.env["FAKE_HERDR_RESPONSES"])
        responses = cast(dict[str, CommandResponse], json.loads(responses_path.read_text()))
        listed = panes if panes is not None else [
            {"pane_id": ident, "workspace_id": "w1", "label": "alpha",
             "agent_session": {"value": conversation} if conversation else None,
             "terminal_id": renamed.terminal_id if ident == "w1:p1" else "terminal-unrelated",
             "tab_id": renamed.tab_id if ident == "w1:p1" else "w1:t2"}
            for ident, conversation in zip(("w1:p1", "w1:p2"), conversations)
        ]
        for listed_pane in listed:
            _ = listed_pane.setdefault("tab_id", renamed.tab_id if listed_pane.get("pane_id") == "w1:p1" else "w1:t2")
        responses["--session agents pane list"] = {"code": 0, "stdout": json.dumps({"result": {"panes": listed}})}
        responses["--session agents workspace list"] = {"code": 0, "stdout": json.dumps({"result": {
            "workspaces": [{"workspace_id": "w1", "label": "demo"}]}})}
        _ = responses_path.write_text(json.dumps(responses))
        return unit_lookup.MarkedUnit("alpha-unit", unit_lookup.ClaudeNotRunning(), herdr.HerdrPlace("agents", "w1:p2"))

    def interrupt_after_stop(self, renamed: rename_unit.UnitRename) -> None:
        stopped = stop_resume.stop
        def interrupted(identity: str) -> stop_resume.Stopped:
            result = stopped(identity)
            self.assertIsInstance(result, stop_resume.Stopped)
            raise InterruptedError("interrupted before pane close")
        with mock.patch.object(stop_resume, "stop", side_effect=interrupted):
            with self.assertRaisesRegex(InterruptedError, "interrupted before pane close"):
                rename_unit.stop_unit(renamed, lambda: None)

    def write_identity_journal(self, renamed: rename_unit.UnitRename, *, legacy: bool, step: int) -> None:
        """Write the old JSON shape without passing through the current journal writer."""
        saved = cast(dict[str, object], asdict(renamed))
        _ = saved.pop("stop_identity", None)
        if isinstance(renamed.staff_id, str):
            saved["staff_id"] = renamed.staff_id
        else:
            member = staff.by_path(f"{renamed.slug}/{unit_names.name(renamed.old)}")
            assert member is not None
            saved["staff_id"] = member.id
        saved["step"] = step
        saved["done"] = False
        if legacy:
            for key in ("pane_session", "pane_id", "terminal_id"):
                del saved[key]
        path = self.state / "demo/rename_unit.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(json.dumps(saved))

    def test_retry_refuses_a_competing_conversation_while_its_terminal_survives(self) -> None:
        pane, renamed = self.recovery_panes()
        self.interrupt_after_stop(renamed)
        for other in ("w1:p2", "w2:p9"):
            with self.subTest(other=other):
                _ = self.publish_stopped_panes(pane, renamed, (None, None), panes=[
                    {"pane_id": "w1:p1", "terminal_id": renamed.terminal_id, "agent_session": None},
                    {"pane_id": other, "terminal_id": "terminal-competitor",
                     "agent_session": {"value": "sid-alpha"}},
                ])
                with self.assertRaisesRegex(RenameRefused, "conversation.*another terminal"):
                    rename_unit.stop_unit(renamed, lambda: None)
                self.assertEqual(pane.close_calls(), [])

    def test_retry_refuses_an_inventory_with_duplicate_journaled_terminal_ids(self) -> None:
        pane, renamed = self.recovery_panes()
        self.interrupt_after_stop(renamed)
        self.assertTrue(renamed.terminal_id)
        _ = self.publish_stopped_panes(pane, renamed, (None, None), panes=[
            {"pane_id": "w1:p1", "terminal_id": renamed.terminal_id, "agent_session": None},
            {"pane_id": "w2:p9", "terminal_id": renamed.terminal_id, "agent_session": None},
        ])
        with self.assertRaisesRegex(RenameRefused, "journaled terminal identity is ambiguous"):
            rename_unit.stop_unit(renamed, lambda: None)
        self.assertEqual(pane.close_calls(), [])

    def test_retry_refuses_an_unrelated_conversation_on_the_journaled_terminal(self) -> None:
        pane, renamed = self.recovery_panes()
        self.interrupt_after_stop(renamed)
        _ = self.publish_stopped_panes(pane, renamed, (None, None), panes=[
            {"pane_id": "w1:p1", "terminal_id": renamed.terminal_id,
             "agent_session": {"value": "sid-unrelated"}},
        ])
        with self.assertRaisesRegex(RenameRefused, "terminal holds another conversation"):
            rename_unit.stop_unit(renamed, lambda: None)
        self.assertEqual(pane.close_calls(), [])

    def test_retry_refuses_foreground_work_on_the_cleared_journaled_terminal(self) -> None:
        pane, renamed = self.recovery_panes()
        self.interrupt_after_stop(renamed)
        _ = self.publish_stopped_panes(pane, renamed, (None, None), panes=[
            {"pane_id": "w1:p1", "terminal_id": renamed.terminal_id, "agent_session": None},
        ])
        responses_path = Path(self.env["FAKE_HERDR_RESPONSES"])
        responses = cast(dict[str, CommandResponse], json.loads(responses_path.read_text()))
        responses["--session agents pane process-info --pane w1:p1"] = {
            "code": 0, "stdout": json.dumps({"result": {"process_info": {
                "shell_pid": pane.shell, "foreground_process_group_id": 998001,
                "foreground_processes": [{"name": "editor"}]}}})}
        _ = responses_path.write_text(json.dumps(responses))
        with self.assertRaisesRegex(RenameRefused, "foreground work"):
            rename_unit.stop_unit(renamed, lambda: None)
        self.assertEqual(pane.close_calls(), [])

    def test_retry_refuses_unreadable_activity_on_the_cleared_journaled_terminal(self) -> None:
        pane, renamed = self.recovery_panes()
        self.interrupt_after_stop(renamed)
        responses_path = Path(self.env["FAKE_HERDR_RESPONSES"])
        responses = cast(dict[str, CommandResponse], json.loads(responses_path.read_text()))
        responses["--session agents pane process-info --pane w1:p1"] = {
            "code": 1, "stdout": "", "stderr": "activity unavailable"}
        _ = responses_path.write_text(json.dumps(responses))
        with self.assertRaisesRegex(RenameRefused, "activity unavailable"):
            rename_unit.stop_unit(renamed, lambda: None)
        self.assertEqual(pane.close_calls(), [])

    def test_retry_refuses_incomplete_terminal_identity_in_a_readable_pane_list(self) -> None:
        pane, renamed = self.recovery_panes()
        self.interrupt_after_stop(renamed)
        _ = self.publish_stopped_panes(pane, renamed, (None, None), panes=[
            {"pane_id": "w1:p1", "terminal_id": renamed.terminal_id, "agent_session": None},
            {"pane_id": "w2:p9", "terminal_id": "", "agent_session": None},
        ])
        with self.assertRaisesRegex(RenameRefused, "incomplete terminal identity"):
            rename_unit.stop_unit(renamed, lambda: None)
        self.assertEqual(pane.close_calls(), [])

    def test_legacy_json_without_pane_keys_refuses_a_pending_stop_after_claude_exits(self) -> None:
        pane, renamed = self.recovery_panes()
        self.interrupt_after_stop(renamed)
        self.write_identity_journal(renamed, legacy=True, step=0)
        self.assert_refused(self.rename("apply"), "journal has no terminal identity; settle the pane by hand")
        self.assertEqual(pane.close_calls(), [])
        self.assertTrue((Path(self.env["HERDR_PROC"]) / str(pane.shell)).exists())

    def test_legacy_json_without_pane_keys_refuses_before_changes(self) -> None:
        pane, renamed = self.recovery_panes()
        self.interrupt_after_stop(renamed)
        rename_unit.stop_unit(renamed, lambda: None)
        closed = pane.close_calls()
        self.assertEqual(closed, [])
        self.write_identity_journal(renamed, legacy=True, step=1)
        before = self.doc.read_bytes()
        self.assert_refused(self.rename("apply"), "journal has no terminal identity; settle the pane by hand")
        self.assertEqual(pane.close_calls(), closed)
        self.assertEqual(self.doc.read_bytes(), before)
        self.assertIs(self.journal()["done"], False)

    def test_explicit_empty_pane_identity_refuses_before_changes(self) -> None:
        renamed = rename_unit.plan("demo", "alpha-unit", "gamma-unit", self.root / "scratch")
        renamed.pane_session = renamed.pane_id = renamed.terminal_id = renamed.tab_id = ""
        self.write_identity_journal(renamed, legacy=False, step=0)
        before = self.doc.read_bytes()
        self.assert_refused(self.rename("apply"), "alpha-unit: retained pane is absent; a legacy rename may have closed it")
        self.assertEqual(self.doc.read_bytes(), before)
        self.assertIs(self.journal()["done"], False)

    def test_retry_after_director_exit_retains_the_journaled_conversation_pane(self) -> None:
        pane, renamed = self.recovery_panes()
        self.interrupt_after_stop(renamed)
        competitor = self.publish_stopped_panes(pane, renamed, ("sid-alpha", "sid-unrelated"))
        with (mock.patch.object(unit_lookup, "marked_unit", return_value=competitor),
              mock.patch.object(unit_lookup, "observed_unit_marks", return_value=(("demo", competitor),))):
            rename_unit.stop_unit(renamed, lambda: None)
        self.assertEqual(pane.close_calls(), [])
        self.assertTrue((Path(self.env["HERDR_PROC"]) / "991003").exists())

    def test_retry_after_an_interrupted_stop_keeps_the_same_conversation_authority(self) -> None:
        pane, renamed = self.recovery_panes()
        self.interrupt_after_stop(renamed)
        competitor = self.publish_stopped_panes(pane, renamed, ("sid-alpha", "sid-unrelated"))
        with mock.patch.object(unit_lookup, "marked_unit", return_value=competitor):
            rename_unit.stop_unit(renamed, lambda: None)
        self.assertEqual(pane.close_calls(), [])
        self.assertTrue((Path(self.env["HERDR_PROC"]) / "991003").exists())

    def test_recovery_refuses_unknown_or_ambiguous_stopped_conversation_identity(self) -> None:
        pane, renamed = self.recovery_panes()
        self.interrupt_after_stop(renamed)
        terminal = renamed.terminal_id
        for conversations in ((None, "sid-unrelated"), ("sid-alpha", "sid-alpha")):
            with self.subTest(conversations=conversations):
                renamed.terminal_id = terminal if conversations[0] is not None else ""
                competitor = self.publish_stopped_panes(pane, renamed, conversations)
                with (mock.patch.object(unit_lookup, "marked_unit", return_value=competitor),
                      self.assertRaises(RenameRefused)):
                    rename_unit.stop_unit(renamed, lambda: None)
                self.assertEqual(pane.close_calls(), [])

    def test_retry_retains_the_unlabelled_terminal_after_its_conversation_clears(self) -> None:
        pane, renamed = self.recovery_panes()
        live = herdr.Herdr("agents").panes()[0]
        self.assertEqual(live["session_id"], "sid-alpha")
        self.interrupt_after_stop(renamed)
        competitor = self.publish_stopped_panes(pane, renamed, (None, None), panes=[
            {"pane_id": "w1:p1", "label": None, "terminal_id": renamed.terminal_id},
            {"pane_id": "w1:p2", "label": "alpha", "terminal_id": "terminal-unrelated"},
        ])
        stopped = herdr.Herdr("agents").panes()[0]
        self.assertIsNone(stopped["session_id"])
        terminal = live.get("terminal_id", "")
        self.assertTrue(terminal)
        self.assertEqual(stopped.get("terminal_id"), terminal)
        self.assertEqual(renamed.terminal_id, terminal)
        with mock.patch.object(unit_lookup, "marked_unit", return_value=competitor):
            rename_unit.stop_unit(renamed, lambda: None)
        self.assertEqual(pane.close_calls(), [])
        self.assertTrue((Path(self.env["HERDR_PROC"]) / "991003").exists())

    def test_retry_refuses_the_same_conversation_on_another_workspace_terminal(self) -> None:
        pane, renamed = self.recovery_panes()
        self.interrupt_after_stop(renamed)
        competitor = self.publish_stopped_panes(pane, renamed, (None, None), panes=[
            {"pane_id": "w2:p9", "workspace_id": "w2", "label": "alpha",
             "terminal_id": "another-terminal", "agent_session": {"value": "sid-alpha"}},
        ])
        with (mock.patch.object(unit_lookup, "marked_unit", return_value=competitor),
              self.assertRaisesRegex(RenameRefused, "conversation.*another terminal")):
            rename_unit.stop_unit(renamed, lambda: None)
        self.assertEqual(pane.close_calls(), [])

    def test_retry_refuses_the_journaled_terminal_after_a_cross_workspace_move(self) -> None:
        pane, renamed = self.recovery_panes()
        self.interrupt_after_stop(renamed)
        responses_path = Path(self.env["FAKE_HERDR_RESPONSES"])
        responses = cast(dict[str, CommandResponse], json.loads(responses_path.read_text()))
        responses["--session agents pane move w1:p1 --new-tab --workspace w2"] = {
            "code": 0, "stdout": json.dumps({"result": {"move_result": {
                "created_tab": {"tab_id": "w2:t3"}, "pane": {"pane_id": "w2:p9"}}}})}
        _ = responses_path.write_text(json.dumps(responses))
        _ = herdr.Herdr("agents").run("pane", "move", "w1:p1", "--new-tab", "--workspace", "w2")
        competitor = self.publish_stopped_panes(pane, renamed, (None, None), panes=[
            {"pane_id": "w2:p9", "workspace_id": "w2", "label": None, "agent_session": None,
             "terminal_id": renamed.terminal_id, "tab_id": "w2:t3"},
        ])
        moved = herdr.Herdr("agents").panes()[0]
        self.assertTrue(renamed.terminal_id)
        self.assertEqual(moved.get("terminal_id"), renamed.terminal_id)
        with (mock.patch.object(unit_lookup, "marked_unit", return_value=competitor),
              self.assertRaisesRegex(RenameRefused, "terminal moved.*w2:p9")):
            rename_unit.stop_unit(renamed, lambda: None)
        self.assertEqual(pane.close_calls(), [])

    def test_closed_original_pane_refuses_without_closing_another_terminal(self) -> None:
        pane, renamed = self.recovery_panes()
        self.interrupt_after_stop(renamed)
        rename_unit.stop_unit(renamed, lambda: None)
        closed = pane.close_calls()
        self.assertEqual(closed, [])
        competitor = self.publish_stopped_panes(pane, renamed, (None, "sid-unrelated"), panes=[
            {"pane_id": "w1:p2", "label": "alpha", "agent_session": {"value": "sid-unrelated"}},
        ])
        with (mock.patch.object(unit_lookup, "marked_unit", return_value=competitor),
              self.assertRaisesRegex(RenameRefused, "alpha-unit: retained pane is absent; a legacy rename may have closed it")):
            rename_unit.stop_unit(renamed, lambda: None)
        self.assertEqual(pane.close_calls(), closed)
        self.assertTrue((Path(self.env["HERDR_PROC"]) / "991003").exists())

    def test_retry_refuses_when_the_full_pane_list_is_unreadable(self) -> None:
        pane, renamed = self.recovery_panes()
        self.interrupt_after_stop(renamed)
        responses_path = Path(self.env["FAKE_HERDR_RESPONSES"])
        responses = cast(dict[str, CommandResponse], json.loads(responses_path.read_text()))
        responses["--session agents pane list"] = {"code": 1, "stdout": "", "stderr": "pane list unavailable"}
        _ = responses_path.write_text(json.dumps(responses))
        with self.assertRaisesRegex(RenameRefused, "pane list unavailable"):
            rename_unit.stop_unit(renamed, lambda: None)
        self.assertEqual(pane.close_calls(), [])

    def test_retry_refuses_an_old_journal_without_a_terminal_identity(self) -> None:
        pane, renamed = self.recovery_panes()
        self.interrupt_after_stop(renamed)
        renamed.terminal_id = ""
        _ = self.publish_stopped_panes(pane, renamed, ("sid-alpha", "sid-unrelated"))
        with self.assertRaisesRegex(RenameRefused, "no journaled terminal identity"):
            rename_unit.stop_unit(renamed, lambda: None)
        self.assertEqual(pane.close_calls(), [])

    def test_live_director_stops_and_resumes_in_the_same_pane(self) -> None:
        self.pane_shell(live=True)

    def test_exited_director_resumes_in_the_same_pane(self) -> None:
        self.pane_shell(live=False)
