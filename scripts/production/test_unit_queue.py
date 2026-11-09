"""Tests for a unit director's ordered queue."""
from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from typing import override
from unittest.mock import patch

from scripts.production import staff, unit_queue
from scripts.lib import atomic_file
from scripts.production.staff_test_support import fixture_member, isolated_staff_file


class UnitQueueTests(unittest.TestCase):
    @property
    def member(self) -> staff.StaffRow:
        return next(row for row in staff.local_snapshot().rows if row.kind == "unit" and row.name == "messaging")

    @override
    def setUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = self.enterContext(isolated_staff_file(root / "staff.json", {}))
        owner = fixture_member("showrunner", name="owner")
        member = fixture_member("unit", name="messaging", parent=owner.id)
        self.enterContext(patch.dict(os.environ, {
            "UNIT_QUEUE_STATE_DIR": str(root / "queues"), "CODEX_THREAD_ID": "",
            "CLAUDE_CODE_SESSION_ID": member.current_session}))

    def test_commands_keep_the_queue_in_order_between_calls(self) -> None:
        cases: list[tuple[list[str], str, list[str], bool]] = [
            (["show"], "—\n", [], False),
            (["add", "--text", "Review delivery"], "1. Review delivery\n", ["Review delivery"], False),
            (["add", "--text", "Clean old records"], "1. Review delivery\n2. Clean old records\n",
             ["Review delivery", "Clean old records"], False),
            (["show"], "1. Review delivery\n2. Clean old records\n", ["Review delivery", "Clean old records"], False),
            (["done"], "1. Clean old records\n", ["Clean old records"], True),
            (["add", "--text", "Next item"], "1. Clean old records\n2. Next item\n", ["Clean old records", "Next item"], False),
            (["clear"], "—\n", [], False),
            (["done"], "—\n", [], False),
            (["show"], "—\n", [], False),
        ]
        for arguments, expected_output, expected_items, sync_fails in cases:
            with self.subTest(arguments=arguments):
                output = io.StringIO()
                errors = io.StringIO()
                sync = (patch.object(atomic_file, "sync_folder", side_effect=OSError("directory sync failed"))
                        if sync_fails else contextlib.nullcontext())
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors), sync:
                    self.assertEqual(unit_queue.main(arguments), 0)
                self.assertEqual(output.getvalue(), expected_output)
                self.assertEqual(unit_queue.read(self.member.id), expected_items)
                if sync_fails:
                    self.assertEqual(len(errors.getvalue().splitlines()), 1)
                    self.assertIn("unit_queue: warning:", errors.getvalue())
                    self.assertIn("durability unconfirmed", errors.getvalue())
                else:
                    self.assertEqual(errors.getvalue(), "")

    def test_each_director_changes_only_its_own_queue(self) -> None:
        second = fixture_member("unit", name="second", parent=self.member.parent)
        for identity, text in ((self.member.current_session, "First work"), (second.current_session, "Second work")):
            with (patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": identity}),
                  contextlib.redirect_stdout(io.StringIO())):
                self.assertEqual(unit_queue.main(["add", "--text", text]), 0)
        self.assertEqual(unit_queue.read(self.member.id), ["First work"])
        self.assertEqual(unit_queue.read(second.id), ["Second work"])

    def test_unreadable_queue_is_not_replaced_by_an_edit(self) -> None:
        path = unit_queue.state_path(self.member.id)
        path.parent.mkdir(parents=True)
        for content in ('{"items": "lost"}', '["saved", 2]', 'not json'):
            with self.subTest(content=content):
                _ = path.write_text(content)
                error = io.StringIO()
                with contextlib.redirect_stderr(error):
                    self.assertEqual(unit_queue.main(["add", "--text", "New work"]), 1)
                self.assertIn("unit_queue:", error.getvalue())
                self.assertEqual(path.read_text(), content)
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(unit_queue.main(["clear"]), 0)
                self.assertEqual(output.getvalue(), "—\n")
                self.assertEqual(unit_queue.read(self.member.id), [])


if __name__ == "__main__":
    _ = unittest.main()
