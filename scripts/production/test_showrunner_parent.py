"""Parent writers keep staff identities and source edges in agreement."""
from __future__ import annotations

import io
import os
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import cast
from unittest import mock


import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.lib.atomic_file import write_text_atomic
from scripts.production import staff
from scripts.production.staff_test_support import StaffTestCase
from scripts.production.rename_test_support import stop_at_staff_write
import showrunner_parent
from scripts.production import showrunners
import showrunner_tree


class ShowrunnerParentTests(StaffTestCase):
    def members(self) -> tuple[staff.StaffRow, staff.StaffRow]:
        _ = self.production("sdlc", workspace="tools")
        _ = self.production("parent", workspace="umbrella")
        child = staff.ensure("showrunner", name="tools")
        parent = staff.ensure("showrunner", name="umbrella")
        with staff.operation():
            pass
        return child, parent

    def source_files(self) -> dict[str, bytes]:
        return {str(path.relative_to(self.root)): path.read_bytes()
                for path in self.root.rglob("*") if path.is_file()}

    def test_attach_and_detach_subprocesses_find_workspace_names(self) -> None:
        child, parent = self.members()
        script = Path(showrunners.__file__)
        _ = self.script(script, "attach", "sdlc", "parent")
        self.assertEqual(staff.snapshot().by_id(child.id), staff.StaffRow(child.id, "showrunner", "tools", parent.id))
        source = Path(os.environ["SHOWRUNNER_STATE_DIR"]) / "sdlc/parent"
        self.assertEqual(source.read_text(), "parent\n")
        _ = self.script(script, "detach", "sdlc")
        self.assertEqual(staff.snapshot().by_id(child.id), child)
        self.assertFalse(source.exists())

    def test_attach_rerun(self) -> None:
        child, parent = self.members()
        with redirect_stdout(io.StringIO()), mock.patch.object(showrunners, "tell_others", return_value=showrunners.NoticeDelivery()):
            for after in (False, True):
                with self.subTest(after=after):
                    _ = staff.move(child.id, None)
                    _ = showrunner_parent.detach("sdlc")

                    with stop_at_staff_write("move", after=after), self.assertRaises(OSError):
                        showrunner_parent.attach_parent("sdlc", "parent")
                    showrunner_parent.attach_parent("sdlc", "parent")
                    expected = self.source_files()
                    finished = io.StringIO()
                    with redirect_stdout(finished), mock.patch.object(showrunners, "tell_others") as notices:
                        showrunner_parent.attach_parent("sdlc", "parent")
                    self.assertIn("done", finished.getvalue())
                    notices.assert_not_called()
                    self.assertEqual(self.source_files(), expected)
                    self.assertEqual(staff.snapshot().by_id(child.id), staff.StaffRow(child.id, "showrunner", "tools", parent.id))

    def test_detach_rerun(self) -> None:
        child, parent = self.members()
        with redirect_stdout(io.StringIO()), mock.patch.object(showrunners, "tell_others", return_value=showrunners.NoticeDelivery()):
            for after in (False, True):
                with self.subTest(after=after):
                    _ = staff.move(child.id, parent.id)
                    showrunner_parent.attach("sdlc", "parent")

                    with stop_at_staff_write("move", after=after), self.assertRaises(OSError):
                        showrunner_parent.detach_parent("sdlc")
                    showrunner_parent.detach_parent("sdlc")
                    expected = self.source_files()
                    showrunner_parent.detach_parent("sdlc")
                    self.assertEqual(self.source_files(), expected)
                    self.assertEqual(staff.snapshot().by_id(child.id), child)

    def test_attach_loop_is_refused_before_either_source_changes(self) -> None:
        child, parent = self.members()
        _ = staff.move(parent.id, child.id)
        before = self.source_files()
        with self.assertRaises(staff.StaffRefused):
            showrunner_parent.attach_parent("sdlc", "parent")
        self.assertEqual(self.source_files(), before)

    def test_source_parent_loop_exits_two_without_changing_either_record(self) -> None:
        child, parent = self.members()
        _ = staff.move(parent.id, child.id)
        showrunner_parent.attach("parent", "sdlc")
        before = self.source_files()
        result = self.script(Path(showrunners.__file__), "attach", "sdlc", "parent", code=2)
        self.assertIn("StaffRefused", result.stderr)
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertEqual(self.source_files(), before)

    def test_notice_recovery_uses_delivery_status_instead_of_output_wording(self) -> None:
        _ = self.members()
        pending = Path(os.environ["SHOWRUNNER_STATE_DIR"]) / "sdlc/parent_notice.json"
        with mock.patch.object(showrunners, "tell_others", return_value=showrunners.NoticeDelivery(
                ("delivery deferred",), failed=("sdlc",))), \
                redirect_stdout(io.StringIO()):
            showrunner_parent.attach_parent("sdlc", "parent")
        self.assertTrue(pending.exists())
        with mock.patch.object(showrunners, "tell_others", return_value=showrunners.NoticeDelivery(("delivered",))), \
                redirect_stdout(io.StringIO()):
            showrunner_parent.attach_parent("sdlc", "parent")
        self.assertFalse(pending.exists())

    def test_parent_change_retries_notices_after_the_source_step(self) -> None:
        child, parent = self.members()
        source = Path(os.environ["SHOWRUNNER_STATE_DIR"]) / "sdlc/parent"
        pending = source.with_name("parent_notice.json")
        for action in ("attach", "detach"):
            with self.subTest(action=action), redirect_stdout(io.StringIO()):
                if action == "detach":
                    _ = staff.move(child.id, parent.id)
                    showrunner_parent.attach("sdlc", "parent")
                arguments = ("sdlc", "parent") if action == "attach" else ("sdlc",)
                run = showrunner_parent.attach_parent if action == "attach" else showrunner_parent.detach_parent
                with mock.patch.object(showrunners, "tell_others", side_effect=OSError("before notices")), self.assertRaises(OSError):
                    run(*arguments)
                self.assertTrue(pending.exists())
                self.assertEqual(source.exists(), action == "attach")
                with mock.patch.object(showrunners, "tell_others", return_value=showrunners.NoticeDelivery(("told sdlc", "told parent"))) as notices:
                    run(*arguments)
                self.assertEqual(set(cast(dict[str, str], notices.call_args.args[0])), {"sdlc", "parent"})
                self.assertFalse(pending.exists())
                self.assertEqual(staff.snapshot().by_id(child.id),
                                 staff.StaffRow(child.id, "showrunner", "tools", parent.id if action == "attach" else None))

    def test_release_uses_the_capture_after_edges_have_been_removed(self) -> None:
        child_row, parent_row = self.members()
        _ = self.production("leaf")
        _ = staff.ensure("showrunner", name="leaf", parent=child_row.id)
        _ = staff.move(child_row.id, parent_row.id)
        state = Path(os.environ["SHOWRUNNER_STATE_DIR"])
        for child, parent in (("sdlc", "parent"), ("leaf", "sdlc")):
            path = state / child / "parent"
            path.parent.mkdir(parents=True, exist_ok=True)
            write_text_atomic(path, parent + "\n")
        capture = showrunner_tree.edges_to_release("sdlc")
        self.assertEqual(capture, showrunner_tree.WrapRelease(showrunner_tree.ReportedTo("parent"), ("leaf",), ()))
        showrunner_tree.release_edges("sdlc", capture)
        showrunner_tree.release_edges("sdlc", capture)
        self.assertFalse((state / "sdlc/parent").exists())
        self.assertFalse((state / "leaf/parent").exists())
        self.assertEqual(capture.former_children, ("leaf",))

    def test_release_retry_preserves_edges_attached_after_capture(self) -> None:
        child_row, parent_row = self.members()
        _ = self.production("leaf")
        leaf_row = staff.ensure("showrunner", name="leaf", parent=child_row.id)
        _ = staff.move(child_row.id, parent_row.id)
        state = Path(os.environ["SHOWRUNNER_STATE_DIR"])
        original = {"sdlc": "parent", "leaf": "sdlc"}
        for child, parent in original.items():
            write_text_atomic(state / child / "parent", parent + "\n")
        capture = showrunner_tree.edges_to_release("sdlc")
        showrunner_tree.release_edges("sdlc", capture)
        replacement = staff.ensure("showrunner", name="replacement")
        _ = staff.move(child_row.id, replacement.id)
        _ = staff.move(leaf_row.id, replacement.id)
        for child in original:
            write_text_atomic(state / child / "parent", "replacement\n")
        errors = io.StringIO()
        with redirect_stderr(errors):
            showrunner_tree.release_edges("sdlc", capture)
        self.assertEqual({child: (state / child / "parent").read_text() for child in original},
                         {child: "replacement\n" for child in original})
        self.assertEqual(len(errors.getvalue().splitlines()), 2)
        for child in original:
            self.assertIn(child, errors.getvalue())
