"""Run cleanup selects staff workers from dispatch records."""
from __future__ import annotations

import io
import os
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from functools import cached_property
from typing import cast, override
from unittest.mock import patch
import unittest

from scripts.direct import remove_workers
from scripts.production import staff
from scripts.production.staff_test_support import StaffTestCase, fixture_worker, unit_membership


class RemoveWorkersTests(StaffTestCase):
    run_dir: Path = Path()

    @cached_property
    def parent(self) -> staff.StaffRow:
        return unit_membership("cleanup", "selected")

    @cached_property
    def first(self) -> staff.StaffRow:
        return fixture_worker("first", self.parent.id)

    @cached_property
    def second(self) -> staff.StaffRow:
        return fixture_worker("second", self.parent.id)

    @cached_property
    def other_parent(self) -> staff.StaffRow:
        return unit_membership("cleanup", "other")

    @cached_property
    def other(self) -> staff.StaffRow:
        return fixture_worker("other", self.other_parent.id)

    @override
    def setUp(self) -> None:
        super().setUp()
        self.run_dir = self.root / "run"
        self.run_dir.mkdir()
        self.write_json(self.run_dir / "progress_history_state.json", {
            "owner_staff_id": self.parent.id,
            "pass": {"impl": {"instance_id": "dispatch", "worker_name": self.first.name}},
        })

    def invoke(self, caller: str, runs: list[Path], code: int = 0) -> tuple[int, list[str], str]:
        arguments = ["remove_workers.py"]
        for run in runs:
            arguments.extend(["--session-dir", str(run)])
        errors = io.StringIO()
        output = io.StringIO()
        with (patch.object(sys, "argv", arguments),
              patch.dict(os.environ, {"CODEX_THREAD_ID": caller, "CLAUDE_CODE_SESSION_ID": ""}),
              patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], code)) as end,
              redirect_stderr(errors), redirect_stdout(output)):
            result = remove_workers.main()
        names = [cast(list[str], call.args[0])[-1] for call in end.call_args_list]
        return result, names, errors.getvalue()

    def test_only_dispatched_members_of_the_caller_are_selected(self) -> None:
        self.write_json(self.run_dir / "impl_worker_other.json", {"staff_id": self.other.id})
        second, other = self.second, self.other
        code, names, errors = self.invoke(self.parent.current_session, [self.run_dir])
        self.assertEqual((code, names, errors), (0, [self.first.name], ""))
        self.assertEqual(staff.by_id(second.id), second)
        self.assertEqual(staff.by_id(other.id), other)

    def test_a_launch_record_selects_the_same_member_after_a_rename(self) -> None:
        self.write_json(self.run_dir / "impl_worker_second.json", {"staff_id": self.second.id})
        renamed = staff.rename(self.second.id, "renamed")
        code, names, errors = self.invoke(self.parent.current_session, [self.run_dir])
        self.assertEqual(code, 0, errors)
        self.assertEqual(set(names), {self.first.name, renamed.name})

    def test_another_native_caller_cannot_select_the_run(self) -> None:
        code, names, errors = self.invoke(self.other_parent.current_session, [self.run_dir])
        self.assertEqual((code, names), (2, []))
        self.assertIn("does not belong to the native caller", errors)

    def test_a_failed_worker_end_returns_its_status(self) -> None:
        code, names, errors = self.invoke(self.parent.current_session, [self.run_dir], 8)
        self.assertEqual((code, names, errors), (8, [self.first.name], ""))

    def test_retirement_retries_after_the_staff_publication_is_interrupted(self) -> None:
        selected = self.first

        def end_then_interrupt(arguments: list[str], *, check: bool) -> subprocess.CompletedProcess[str]:
            self.assertFalse(check)
            self.assertEqual(arguments[-1], selected.name)
            self.assertEqual(staff.retire(selected.id), selected)
            raise InterruptedError("lost staff acknowledgement")

        with (patch.object(sys, "argv", ["remove_workers.py", "--session-dir", str(self.run_dir)]),
              patch.dict(os.environ, {"CODEX_THREAD_ID": self.parent.current_session, "CLAUDE_CODE_SESSION_ID": ""}),
              patch.object(subprocess, "run", side_effect=end_then_interrupt),
              self.assertRaisesRegex(InterruptedError, "lost staff acknowledgement")):
            result = remove_workers.main()
            self.fail(f"worker end returned {result} without the acknowledgement failure")
        self.assertIsNone(staff.by_id(selected.id))
        before = self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns
        self.assertEqual(self.invoke(self.parent.current_session, [self.run_dir]), (0, [], ""))
        self.assertEqual((self.staff_file.read_bytes(), self.staff_file.stat().st_mtime_ns), before)

    def test_a_worker_caller_cannot_remove_itself_or_its_peers(self) -> None:
        code, names, errors = self.invoke(self.first.current_session, [self.run_dir])
        self.assertEqual((code, names), (2, []))
        self.assertIn("does not belong to the native caller", errors)
        self.assertEqual(staff.by_id(self.first.id), self.first)

    def test_no_given_run_selects_no_workers(self) -> None:
        self.assertEqual(self.invoke(self.parent.current_session, []), (0, [], ""))


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
