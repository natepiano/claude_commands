"""Move staff membership and a live tab without relaunching its director."""
from __future__ import annotations

import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import cast, override
from unittest import mock

from scripts.production import staff, staff_move, staff_sources
from scripts.production.staff_test_support import CHECKOUT, StaffTestCase
import unit_lookup
import herdr


class StaffMoveTests(StaffTestCase):
    responses: dict[str, object] = {}
    responses_path: Path = Path()
    log: Path = Path()
    members: dict[str, staff.StaffRow] = {}
    destination_panes: list[dict[str, object]] = []

    @property
    def source(self) -> staff.StaffRow:
        return self.members["source"]

    @property
    def destination(self) -> staff.StaffRow:
        return self.members["destination"]

    @property
    def unit(self) -> staff.StaffRow:
        return self.members["unit"]

    @property
    def worker(self) -> staff.StaffRow:
        return self.members["worker"]

    @override
    def setUp(self) -> None:
        super().setUp()
        _ = self.production("sdlc", "models-unit", workspace="tools")
        _ = self.production("target", workspace="new-tools")
        self.members = {}
        self.destination_panes = []
        self.members["source"] = staff.ensure("showrunner", name="tools")
        self.members["destination"] = staff.ensure("showrunner", name="new-tools")
        self.members["unit"] = staff.ensure("unit", name="panes", parent=self.source.id)
        self.members["worker"] = staff.ensure("worker", name="impl1", parent=self.unit.id)
        self.pane("w1:p1", session_id="director-session", environment={
            "STAFF_ID": self.unit.id, "SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "models-unit",
        })
        self.log = self.root / "herdr.jsonl"
        self.responses_path = self.root / "herdr-responses.json"
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            "HERDR_BIN": str(CHECKOUT / "scripts/herdr/fake_herdr.py"),
            "FAKE_HERDR_LOG": str(self.log), "FAKE_HERDR_RESPONSES": str(self.responses_path),
            "XDG_STATE_HOME": str(self.root / "state"),
        })))
        self.responses = {"default": {"stdout": "", "stderr": "unexpected herdr command", "code": 97}}
        self.reply(["pane", "process-info", "--pane", "w1:p1"], {"process_info": {"shell_pid": 100}})
        self.reply(["workspace", "list"], {"workspaces": [
            {"workspace_id": "w1", "label": "tools"}, {"workspace_id": "w2", "label": "new-tools"},
        ]})
        self.reply(["pane", "list"], {"panes": [
            {"pane_id": "w1:p1", "workspace_id": "w1", "tab_id": "w1:t1", "label": "panes"},
        ]})
        self.reply(["pane", "move", "w1:p1", "--new-tab", "--workspace", "w2"], {
            "move_result": {"created_tab": {"tab_id": "w2:t2"}, "pane": {"pane_id": "w1:p1"}},
        })
        self.reply(["tab", "rename", "w2:t2", "panes"], {})
        self.reply(["tab", "list", "--workspace", "w2"], {"tabs": [
            {"tab_id": "w2:t2", "label": "panes", "pane_count": 1},
        ]})
        self.reply(["tab", "list"], {"tabs": [
            {"tab_id": "w1:t0", "workspace_id": "w1", "label": "tools"},
            {"tab_id": "w2:t2", "workspace_id": "w2", "label": "panes"},
        ]})

    def reply(self, arguments: list[str], result: dict[str, object], *, code: int = 0, stderr: str = "") -> None:
        if self.responses_path.exists():
            self.responses = cast(dict[str, object], json.loads(self.responses_path.read_text()))
        self.responses[" ".join(["--session", "agents", *arguments])] = {
            "stdout": json.dumps({"result": result}), "code": code, "stderr": stderr,
        }
        move = self.responses.get("--session agents pane move w1:p1 --new-tab --workspace w2")
        if isinstance(move, dict):
            move = cast(dict[str, object], move)
            after = {**self.responses, "--session agents pane list": {"code": 0, "stdout": json.dumps({"result": {
                "panes": [{"pane_id": "w1:p1", "workspace_id": "w2", "tab_id": "w2:t2", "label": self.unit.name},
                          *self.destination_panes],
            }})}}
            moved = {key: value for key, value in move.items() if key != "write_files"}
            after["--session agents pane move w1:p1 --new-tab --workspace w2"] = moved
            if move["code"] == 0:
                move["write_files"] = {str(self.responses_path): json.dumps(after)}
            else:
                _ = move.pop("write_files", None)
        self.write_json(self.responses_path, self.responses)

    def commands(self) -> list[list[str]]:
        return [cast(list[str], json.loads(line)) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def sorting_commands(self) -> list[list[str]]:
        return [["--session", "agents", *arguments] for arguments in (
            ["workspace", "list"], ["tab", "list"], ["tab", "list", "--workspace", "w2"],
            ["workspace", "list"], ["tab", "list", "--workspace", "w2"],
            ["workspace", "list"], ["tab", "list"],
        )]

    def assert_unit_tab_is_sorted(self, *, already_placed: bool) -> None:
        for name in ("devtools", "herdr"):
            _ = staff.ensure("unit", name=name, parent=self.destination.id)
        self.members["unit"] = staff.rename(self.unit.id, "style")
        tabs = [
            {"tab_id": "w2:t0", "workspace_id": "w2", "label": "new-tools", "pane_count": 1},
            {"tab_id": "w2:t1", "workspace_id": "w2", "label": "devtools", "pane_count": 1},
            {"tab_id": "w2:t2", "workspace_id": "w2", "label": "style", "pane_count": 1},
            {"tab_id": "w2:t3", "workspace_id": "w2", "label": "herdr", "pane_count": 1},
        ]
        self.destination_panes = [
            {"pane_id": f"w2:p{index}", "workspace_id": "w2", "tab_id": tab["tab_id"], "label": tab["label"]}
            for index, tab in enumerate(tabs) if index != 2
        ]
        self.reply(["pane", "list"], {"panes": [
            {"pane_id": "w1:p1", "workspace_id": "w2" if already_placed else "w1",
             "tab_id": "w2:t2" if already_placed else "w1:t1", "label": "style"}, *self.destination_panes,
        ]})
        self.reply(["tab", "list", "--workspace", "w2"], {"tabs": tabs})
        self.reply(["tab", "rename", "w2:t2", "style"], {})
        self.reply(["pane", "move", "w2:p3", "--new-tab", "--workspace", "w2"], {
            "move_result": {"created_tab": {"tab_id": "w2:t3"}, "pane": {"pane_id": "w2:p3"}},
        })
        self.reply(["tab", "rename", "w2:t3", "herdr"], {})
        sorted_tabs = [tabs[index] for index in (0, 1, 3, 2)]
        self.responses["--session agents tab list"] = [
            {"stdout": json.dumps({"result": {"tabs": shown}}), "code": 0}
            for shown in ([tabs, sorted_tabs] if already_placed else [tabs, tabs, sorted_tabs])
        ]
        self.write_json(self.responses_path, self.responses)
        # Refresh the fake move's state transition with the complete tab responses.
        self.reply(["tab", "rename", "w2:t2", "style"], {})
        self.write_json(herdr.layout_path("agents"), {
            "at": 0, "workspaces": [{"name": "new-tools", "seen": 0, "tabs": [
                {"name": tab["label"], "seen": 0} for tab in tabs
            ]}],
        })
        result = staff_move.move_unit("sdlc", "models-unit", "target")
        self.assertIsInstance(result.tab, staff_move.TabAlreadyPlaced if already_placed else staff_move.TabMoved)
        moves = [command[4] for command in self.commands() if command[2:4] == ["pane", "move"]]
        self.assertEqual(moves, ["w2:p3", "w1:p1"] if already_placed else ["w1:p1", "w2:p3", "w1:p1"])
        self.assertEqual([tab["name"] for space in herdr.saved_layout("agents")["workspaces"]
                          if space["name"] == "new-tools" for tab in space["tabs"]],
                         ["new-tools", "devtools", "herdr", "style"])
        self.assertFalse(any(command[2:] == ["tab", "list", "--workspace", "w1"] for command in self.commands()))

    def test_newly_moved_tab_sorts_units_even_when_saved_order_is_wrong(self) -> None:
        self.assert_unit_tab_is_sorted(already_placed=False)

    def test_already_present_tab_sorts_units_even_when_saved_order_is_wrong(self) -> None:
        self.assert_unit_tab_is_sorted(already_placed=True)

    def test_move_changes_only_parent_and_moves_running_pane_to_parent_workspace(self) -> None:
        before = staff.snapshot()
        with mock.patch.object(staff, "snapshot", wraps=staff.snapshot) as snapshot:
            result = staff_move.move_unit("sdlc", "models-unit", "target")
        snapshot.assert_called_once_with()
        self.assertEqual(result.row, replace(self.unit, parent=self.destination.id))
        self.assertTrue(result.membership_changed)
        self.assertIsInstance(result.tab, staff_move.TabMoved)
        self.assertEqual(staff.snapshot(), replace(before, rows=tuple(
            result.row if row.id == self.unit.id else row for row in before.rows)))
        self.assertEqual(self.commands(), [["--session", "agents", *arguments] for arguments in (
            ["pane", "list"], ["pane", "list"], ["workspace", "list"],
            ["workspace", "list"],
            ["pane", "move", "w1:p1", "--new-tab", "--workspace", "w2"], ["tab", "rename", "w2:t2", "panes"],
            ["workspace", "list"], ["tab", "list", "--workspace", "w2"], ["workspace", "list"], ["tab", "list"],
        )] + self.sorting_commands())
        with self.assertRaises(FrozenInstanceError):
            setattr(result, "row", self.unit)

    def test_staff_move_rerun_reports_done_without_a_second_move_or_file_change(self) -> None:
        _ = staff_move.move_unit("sdlc", "models-unit", "target")
        before = self.staff_file.read_bytes()
        with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
            code = staff.main(["move", "sdlc/models-unit", "target"])
        self.assertEqual(code, 0, errors.getvalue())
        self.assertIn("done", output.getvalue())
        self.assertIn("unchanged", errors.getvalue())
        self.assertEqual(self.staff_file.read_bytes(), before)
        self.assertEqual(sum(command[2:4] == ["pane", "move"] for command in self.commands()), 1)

    def test_no_live_pane_keeps_parent_change_and_reports_the_unmoved_tab_once(self) -> None:
        self.reply(["pane", "list"], {"panes": []})
        with mock.patch.object(unit_lookup, "marked_unit", return_value=None):
            with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
                code = staff.main(["move", "sdlc/models-unit", "target"])
        self.assertEqual(code, 0, errors.getvalue())
        self.assertEqual(staff.by_id(self.unit.id), replace(self.unit, parent=self.destination.id))
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        self.assertIn("tab was not moved", output.getvalue())
        self.assertEqual(self.commands(), [["--session", "agents", "pane", "list"]])

    def test_pane_exiting_after_lookup_keeps_parent_change_and_reports_no_live_tab(self) -> None:
        command = "--session agents pane list"
        self.responses[command] = [self.responses[command], {
            "code": 0, "stdout": json.dumps({"result": {"panes": []}}),
        }]
        self.write_json(self.responses_path, self.responses)
        with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
            code = staff.main(["move", "sdlc/models-unit", "target"])
        self.assertEqual(code, 0, errors.getvalue())
        self.assertEqual(staff.by_id(self.unit.id), replace(self.unit, parent=self.destination.id))
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        self.assertIn("tab was not moved (no live pane)", output.getvalue())
        self.assertEqual(self.commands(), [["--session", "agents", "pane", "list"]] * 2)

    def test_hand_resumed_unmarked_director_moves_by_its_pane_label(self) -> None:
        self.reply(["pane", "list"], {"panes": [
            {"pane_id": "w1:p1", "workspace_id": "w1", "tab_id": "w1:t1", "label": "panes"},
        ]})
        with mock.patch.object(unit_lookup, "marked_unit", return_value=None) as marks:
            result = staff_move.move_unit("sdlc", "models-unit", "target")
        marks.assert_not_called()
        self.assertIsInstance(result.tab, staff_move.TabMoved)
        self.assertEqual(result.row.parent, self.destination.id)
        self.assertIn(["--session", "agents", "pane", "move", "w1:p1", "--new-tab", "--workspace", "w2"],
                      self.commands())
        self.assertFalse(any(command[2:4] in (["pane", "process-info"], ["pane", "close"], ["agent", "start"])
                             for command in self.commands()))

    def test_hand_resumed_tab_moves_on_retry_after_parent_already_changed(self) -> None:
        _ = staff.move(self.unit.id, self.destination.id)
        before = self.staff_file.read_bytes()
        self.reply(["pane", "list"], {"panes": [
            {"pane_id": "w1:p1", "workspace_id": "w1", "tab_id": "w1:t1", "label": "panes"},
        ]})
        with mock.patch.object(unit_lookup, "marked_unit", return_value=None) as marks:
            result = staff_move.move_unit("sdlc", "models-unit", "target")
        marks.assert_not_called()
        self.assertFalse(result.membership_changed)
        self.assertIsInstance(result.tab, staff_move.TabMoved)
        self.assertEqual(self.staff_file.read_bytes(), before)
        self.assertIn(["--session", "agents", "pane", "move", "w1:p1", "--new-tab", "--workspace", "w2"],
                      self.commands())

    def test_exec_launched_director_moves_while_its_process_stays_running(self) -> None:
        self.reply(["pane", "process-info", "--pane", "w1:p1"], {"process_info": {"shell_pid": 101}})
        result = staff_move.move_unit("sdlc", "models-unit", "target")
        self.assertIsInstance(result.tab, staff_move.TabMoved)
        self.assertEqual(result.row.parent, self.destination.id)
        self.assertIn(["--session", "agents", "pane", "move", "w1:p1", "--new-tab", "--workspace", "w2"],
                      self.commands())
        self.assertTrue(all(command[2:4] in (["pane", "process-info"], ["pane", "list"], ["workspace", "list"],
                                            ["pane", "move"], ["tab", "rename"], ["tab", "list"]) for command in self.commands()))

    def test_name_only_unit_rerun_resolves_destination_after_source_row_is_removed(self) -> None:
        # Before pane identity is recorded, member uses the tab's source name.
        self.pane("w1:p1", session_id="director-session", environment={
            "SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "models-unit",
        })
        self.members["unit"] = staff.rename(self.unit.id, "models")
        self.reply(["pane", "list"], {"panes": [
            {"pane_id": "w1:p1", "workspace_id": "w1", "tab_id": "w1:t1", "label": "models"},
        ]})
        self.reply(["tab", "rename", "w2:t2", "models"], {})
        self.reply(["tab", "list", "--workspace", "w2"], {"tabs": [
            {"tab_id": "w2:t2", "label": "models", "pane_count": 1},
        ]})
        _ = staff_move.move_unit("sdlc", "models-unit", "target")
        source_doc = self.source_root / "sdlc-production.md"
        self.units_doc(source_doc, (), workspace="tools")
        before = self.staff_file.read_bytes()
        repeated = staff_move.move_unit("sdlc", "models-unit", "target")
        self.assertEqual(repeated.row.id, self.unit.id)
        self.assertFalse(repeated.membership_changed)
        self.assertIsInstance(repeated.tab, staff_move.TabAlreadyPlaced)
        self.assertEqual(self.staff_file.read_bytes(), before)
        self.assertEqual(sum(command[2:4] == ["pane", "move"] for command in self.commands()), 1)

    def test_pinned_identity_keeps_renamed_unit_across_source_removal(self) -> None:
        self.pane("w1:p1", session_id="director-session", environment={})
        identity = staff_move.PinnedUnit(self.unit.id, self.destination.id)
        first = staff_move.move_unit("sdlc", "models-unit", "target", identity=identity)
        self.units_doc(self.source_root / "sdlc-production.md", (), workspace="tools")
        # A relocated checkout can leave the destination timer pointing at its old doc.
        (self.source_root / "target-production.md").unlink()
        before = self.staff_file.read_bytes()
        repeated = staff_move.move_unit("sdlc", "models-unit", "target", identity=identity)
        self.assertEqual(repeated.row, first.row)
        self.assertEqual(self.staff_file.read_bytes(), before)
        self.assertIsInstance(repeated.tab, staff_move.TabAlreadyPlaced)
        self.assertEqual(sum(command[2:4] == ["pane", "move"] for command in self.commands()), 1)

    def test_missing_pinned_identity_never_selects_destination_name(self) -> None:
        replacement = staff.ensure("unit", name="models", parent=self.destination.id)
        before = self.staff_file.read_bytes()
        with self.assertRaises(staff.StaffRefused):
            _ = staff_move.move_unit("sdlc", "models-unit", "target",
                                     identity=staff_move.PinnedUnit(staff.StaffId("s00000000000"), self.destination.id))
        self.assertEqual(staff.by_id(replacement.id), replacement)
        self.assertEqual(self.staff_file.read_bytes(), before)
        self.assertEqual(self.commands(), [])

    def test_missing_or_wrong_pinned_parent_refuses_before_any_change(self) -> None:
        before = self.staff_file.read_bytes()
        for parent_id in (staff.StaffId("s00000000000"), self.unit.id, self.worker.id):
            with self.subTest(parent_id=parent_id):
                with self.assertRaisesRegex(staff.StaffRefused, "no matching showrunner"):
                    _ = staff_move.move_unit("sdlc", "models-unit", "target",
                                             identity=staff_move.PinnedUnit(self.unit.id, parent_id))
                self.assertEqual(self.staff_file.read_bytes(), before)
        self.assertEqual(self.commands(), [])

    def test_missing_member_refuses_before_any_staff_or_tab_change(self) -> None:
        before = self.staff_file.read_bytes()
        for target, destination, cause in (("sdlc/missing", "target", "sdlc/missing"),
                                            ("sdlc/models-unit", "missing", "missing")):
            with self.subTest(target=target, destination=destination):
                completed = self.cli("move", target, destination, code=2)
                self.assertIn(cause, completed.stderr)
                self.assertEqual(len(completed.stderr.splitlines()), 1)
                self.assertEqual(self.staff_file.read_bytes(), before)
        self.assertEqual(self.commands(), [])

    def test_wrong_member_kinds_refuse_before_any_change(self) -> None:
        before = self.staff_file.read_bytes()
        for unit, parent in ((self.source, self.destination), (self.unit, self.worker)):
            with self.subTest(unit=unit.kind, parent=parent.kind):
                with mock.patch.object(staff_sources, "member", side_effect=[
                    staff_sources.MemberFound(unit), staff_sources.MemberFound(parent),
                ]):
                    with self.assertRaises(staff.StaffRefused):
                        _ = staff_move.move_unit("sdlc", "models-unit", "target")
                self.assertEqual(self.staff_file.read_bytes(), before)
        self.assertEqual(self.commands(), [])

    def test_mac_moves_owner_row_and_local_tab_without_a_local_staff_copy(self) -> None:
        with self.machine("mac"):
            _ = self.production("sdlc", "models-unit", workspace="tools")
            _ = self.production("target", workspace="new-tools")
            self.pane("w1:p1", session_id="director-session", environment={
                "STAFF_ID": self.unit.id, "SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "models-unit",
            })
            result = staff_move.move_unit("sdlc", "models-unit", "target")
            self.assertEqual(result.row, replace(self.unit, parent=self.destination.id))
            self.assertFalse(self.mac_file.exists())
        self.assertEqual(self.ssh_commands(), [
            ["show", "--snapshot", "--json"], ["move", self.unit.id, f"--parent={self.destination.id}", "--json"],
        ])
        self.assertEqual(staff.by_id(self.unit.id), result.row)
        self.assertIn(["--session", "agents", "pane", "move", "w1:p1", "--new-tab", "--workspace", "w2"],
                      self.commands())

    def test_taken_unit_name_refuses_before_tab_move(self) -> None:
        _ = staff.ensure("unit", name="legacy-panes", parent=self.destination.id)
        _ = self.staff_file.write_text(self.staff_file.read_text().replace('"name": "legacy-panes"', '"name": "panes"'))
        before = self.staff_file.read_bytes()
        with self.assertRaises(staff.StaffRefused):
            _ = staff_move.move_unit("sdlc", "models-unit", "target")
        self.assertEqual(self.staff_file.read_bytes(), before)
        self.assertEqual(self.commands(), [])

    def test_retry_finishes_tab_title_after_move_already_succeeded(self) -> None:
        self.reply(["tab", "rename", "w2:t2", "panes"], {}, code=1, stderr="rename refused")
        self.reply(["tab", "list", "--workspace", "w2"], {"tabs": [
            {"tab_id": "w2:t2", "label": "untitled", "pane_count": 1},
        ]})
        with self.assertRaisesRegex(staff.StaffUnreadable, "rename refused"):
            _ = staff_move.move_unit("sdlc", "models-unit", "target")
        before = self.staff_file.read_bytes()
        self.reply(["tab", "rename", "w2:t2", "panes"], {})
        _ = staff_move.move_unit("sdlc", "models-unit", "target")
        self.assertEqual(self.staff_file.read_bytes(), before)
        self.assertEqual(sum(item[2:4] == ["pane", "move"] for item in self.commands()), 1)
        self.assertEqual(self.commands()[-8:], [
            ["--session", "agents", "tab", "rename", "w2:t2", "panes"], *self.sorting_commands(),
        ])

    def test_retry_after_tab_failure_reconciles_tab_without_repeating_staff_change(self) -> None:
        self.reply(["pane", "move", "w1:p1", "--new-tab", "--workspace", "w2"], {}, code=1, stderr="move refused")
        with self.assertRaisesRegex(staff.StaffUnreadable, "tab was not moved"):
            _ = staff_move.move_unit("sdlc", "models-unit", "target")
        before = self.staff_file.read_bytes()
        self.reply(["pane", "move", "w1:p1", "--new-tab", "--workspace", "w2"], {
            "move_result": {"created_tab": {"tab_id": "w2:t2"}, "pane": {"pane_id": "w1:p1"}},
        })
        result = staff_move.move_unit("sdlc", "models-unit", "target")
        self.assertFalse(result.membership_changed)
        self.assertIsInstance(result.tab, staff_move.TabMoved)
        self.assertEqual(self.staff_file.read_bytes(), before)

    def test_already_placed_tab_is_read_by_its_workspace_id(self) -> None:
        _ = staff_move.move_unit("sdlc", "models-unit", "target")
        previous_commands = len(self.commands())
        result = staff_move.move_unit("sdlc", "models-unit", "target")
        self.assertIsInstance(result.tab, staff_move.TabAlreadyPlaced)
        self.assertEqual(self.commands()[previous_commands:], [["--session", "agents", *arguments] for arguments in (
            ["pane", "list"], ["pane", "list"], ["workspace", "list"],
            ["tab", "list", "--workspace", "w2"],
        )] + self.sorting_commands())

    def test_missing_workspace_keeps_staff_parent_and_reports_the_cause_once(self) -> None:
        self.reply(["workspace", "list"], {"workspaces": [{"workspace_id": "w1", "label": "tools"}]})
        before = staff.snapshot()
        with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
            code = staff.main(["move", "sdlc/models-unit", "target"])
        self.assertEqual(code, 2)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(len(errors.getvalue().splitlines()), 1)
        self.assertIn("no workspace named 'new-tools'", errors.getvalue())
        self.assertEqual(staff.snapshot(), replace(before, rows=tuple(
            replace(row, parent=self.destination.id) if row.id == self.unit.id else row for row in before.rows)))
        self.assertTrue(all(command[2:4] not in (["pane", "move"], ["workspace", "create"])
                            for command in self.commands()))


if __name__ == "__main__":
    _ = unittest.main()
