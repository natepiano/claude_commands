"""Parent changes use staff rows and preserve pending notices."""
from __future__ import annotations

from scripts.production.staff_test_support import recorded_showrunner

import io
import os
from contextlib import redirect_stdout
from pathlib import Path
from typing import cast
from unittest import mock


import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.production import staff
from scripts.production.staff_test_support import StaffTestCase
from scripts.production.rename_test_support import stop_at_staff_write
import showrunner_parent
from scripts.production import showrunners
from scripts.production import showrunner_tree


class ShowrunnerParentTests(StaffTestCase):
    def members(self) -> tuple[staff.StaffRow, staff.StaffRow]:
        child_doc = self.production("sdlc", workspace="tools")
        parent_doc = self.production("parent", workspace="umbrella")
        child = recorded_showrunner("tools", child_doc)
        parent = recorded_showrunner("umbrella", parent_doc)
        with staff.operation():
            pass
        return child, parent

    def source_files(self) -> dict[str, bytes]:
        return {str(path.relative_to(self.root)): path.read_bytes()
                for path in self.root.rglob("*") if path.is_file()}

    def test_attach_and_detach_subprocesses_find_staff_names(self) -> None:
        child, parent = self.members()
        script = Path(showrunners.__file__)
        _ = self.script(script, "attach", "tools", "umbrella")
        self.assertEqual(staff.snapshot().by_id(child.id), staff.StaffRow(child.id, "showrunner", "tools", staff.ReportsTo(parent.id), current_session=child.id, doc=child.doc))
        source = Path(os.environ["SHOWRUNNER_STATE_DIR"]) / child.id / "parent"
        self.assertFalse(source.exists())
        _ = self.script(script, "detach", "tools")
        self.assertEqual(staff.snapshot().by_id(child.id), child)
        self.assertFalse(source.exists())

    def test_attach_rerun(self) -> None:
        child, parent = self.members()
        with redirect_stdout(io.StringIO()), mock.patch.object(showrunners, "tell_others", return_value=showrunners.NoticeDelivery()):
            for after in (False, True):
                with self.subTest(after=after):
                    _ = staff.move(child.id, staff.StaffRoot())

                    with stop_at_staff_write("move", after=after), self.assertRaises(OSError):
                        showrunner_parent.attach_parent("tools", "umbrella")
                    showrunner_parent.attach_parent("tools", "umbrella")
                    expected = self.source_files()
                    finished = io.StringIO()
                    with redirect_stdout(finished), mock.patch.object(showrunners, "tell_others") as notices:
                        showrunner_parent.attach_parent("tools", "umbrella")
                    self.assertIn("done", finished.getvalue())
                    notices.assert_not_called()
                    self.assertEqual(self.source_files(), expected)
                    self.assertEqual(staff.snapshot().by_id(child.id), staff.StaffRow(child.id, "showrunner", "tools", staff.ReportsTo(parent.id), current_session=child.id, doc=child.doc))

    def test_detach_rerun(self) -> None:
        child, parent = self.members()
        with redirect_stdout(io.StringIO()), mock.patch.object(showrunners, "tell_others", return_value=showrunners.NoticeDelivery()):
            for after in (False, True):
                with self.subTest(after=after):
                    _ = staff.move(child.id, staff.ReportsTo(parent.id))

                    with stop_at_staff_write("move", after=after), self.assertRaises(OSError):
                        showrunner_parent.detach_parent("tools")
                    showrunner_parent.detach_parent("tools")
                    expected = self.source_files()
                    showrunner_parent.detach_parent("tools")
                    self.assertEqual(self.source_files(), expected)
                    self.assertEqual(staff.snapshot().by_id(child.id), child)

    def test_attach_loop_is_refused_before_either_source_changes(self) -> None:
        child, parent = self.members()
        _ = staff.move(parent.id, staff.ReportsTo(child.id))
        before = self.source_files()
        with self.assertRaises(staff.StaffRefused):
            showrunner_parent.attach_parent("tools", "umbrella")
        self.assertEqual(self.source_files(), before)

    def test_source_parent_loop_exits_two_without_changing_either_record(self) -> None:
        child, parent = self.members()
        _ = staff.move(parent.id, staff.ReportsTo(child.id))
        before = self.source_files()
        result = self.script(Path(showrunners.__file__), "attach", "tools", "umbrella", code=2)
        self.assertIn("StaffRefused", result.stderr)
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertEqual(self.source_files(), before)

    def test_notice_recovery_uses_delivery_status_instead_of_output_wording(self) -> None:
        child, _ = self.members()
        pending = Path(os.environ["SHOWRUNNER_STATE_DIR"]) / child.id / "parent_notice.json"
        with mock.patch.object(showrunners, "tell_others", return_value=showrunners.NoticeDelivery(
                ("delivery deferred",), failed=("tools",))), \
                redirect_stdout(io.StringIO()):
            showrunner_parent.attach_parent("tools", "umbrella")
        self.assertTrue(pending.exists())
        with mock.patch.object(showrunners, "tell_others", return_value=showrunners.NoticeDelivery(("delivered",))), \
                redirect_stdout(io.StringIO()):
            showrunner_parent.attach_parent("tools", "umbrella")
        self.assertFalse(pending.exists())

    def test_parent_change_retries_notices_after_the_source_step(self) -> None:
        child, parent = self.members()
        source = Path(os.environ["SHOWRUNNER_STATE_DIR"]) / child.id / "parent"
        pending = source.with_name("parent_notice.json")
        for action in ("attach", "detach"):
            with self.subTest(action=action), redirect_stdout(io.StringIO()):
                if action == "detach":
                    _ = staff.move(child.id, staff.ReportsTo(parent.id))
                arguments = ("tools", "umbrella") if action == "attach" else ("tools",)
                run = showrunner_parent.attach_parent if action == "attach" else showrunner_parent.detach_parent
                with mock.patch.object(showrunners, "tell_others", side_effect=OSError("before notices")), self.assertRaises(OSError):
                    run(*arguments)
                self.assertTrue(pending.exists())
                self.assertFalse(source.exists())
                with mock.patch.object(showrunners, "tell_others", return_value=showrunners.NoticeDelivery(("told tools", "told umbrella"))) as notices:
                    run(*arguments)
                self.assertEqual(set(cast(dict[str, str], notices.call_args.args[0])), {"tools", "umbrella"})
                self.assertFalse(pending.exists())
                self.assertEqual(staff.snapshot().by_id(child.id),
                                 staff.StaffRow(child.id, "showrunner", "tools", staff.ReportsTo(parent.id) if action == "attach" else staff.StaffRoot(), current_session=child.id, doc=child.doc))

    def test_wrap_capture_keeps_staff_edges_without_parent_files(self) -> None:
        child_row, parent_row = self.members()
        leaf_doc = self.production("leaf")
        leaf = recorded_showrunner("leaf", leaf_doc)
        _ = staff.move(leaf.id, staff.ReportsTo(child_row.id))
        _ = staff.move(child_row.id, staff.ReportsTo(parent_row.id))
        capture = showrunner_tree.edges_to_release("tools")
        self.assertEqual(capture, showrunner_tree.WrapRelease(showrunner_tree.ReportedTo("umbrella"), ("leaf",), ()))
        self.assertEqual(showrunner_tree.edges_to_release("tools"), capture)
        state = Path(os.environ["SHOWRUNNER_STATE_DIR"])
        self.assertEqual(list(state.glob("*/parent")), [])
