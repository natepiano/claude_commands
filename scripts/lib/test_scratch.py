#!/usr/bin/env python3
"""Tests for scratch.py: folders that belong to a task and go when it ends."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import TYPE_CHECKING, cast, override
from unittest import mock

if TYPE_CHECKING:
    from . import scratch
else:
    import scratch

RUN = "run:2a858b68-0f9a-4af2-b3cf-a776e6b180cb"
ROW = "row:cargo-liner-C54"


class ScratchTests(unittest.TestCase):
    base: Path = Path()

    @override
    def setUp(self) -> None:
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.enterContext(mock.patch.dict(os.environ, {
            scratch.ROOT_ENV: str(self.base / "scratch"), scratch.VISIBLE_ROOT_ENV: str(self.base / "repro"),
            "CLAUDE_CODE_SESSION_ID": "maker-session"}))

    def test_new_makes_a_recorded_folder_for_its_task(self) -> None:
        path = scratch.new(RUN, "gate")
        self.assertEqual(path.parent, self.base / "scratch")
        self.assertRegex(path.name, r"^run-2a858b68-0f9a-4af2-b3cf-a776e6b180cb-gate-[a-z0-9]{6}$")
        record = cast(dict[str, object], json.loads((path / scratch.RECORD).read_text()))
        self.assertEqual((record["task"], record["maker"]), (RUN, "maker-session"))

    def test_a_visible_repro_goes_where_cargo_port_lists_it(self) -> None:
        self.assertEqual(scratch.new(ROW, "demo", visible=True), self.base / "repro" / "cargo-liner-C54-demo")
        with self.assertRaises(scratch.ScratchError):
            _ = scratch.new(RUN, "x", visible=True)

    def test_a_malformed_task_or_name_is_refused(self) -> None:
        for task in ("run", "session:abc", "row:cargo-liner", "row:a/C1", "phase:"):
            with self.assertRaises(scratch.ScratchError, msg=task):
                _ = scratch.new(task)
        with self.assertRaises(scratch.ScratchError):
            _ = scratch.new(RUN, "../out")

    def test_end_removes_only_the_tasks_folders(self) -> None:
        mine = [scratch.new(ROW, "demo", visible=True), scratch.new(ROW, "copy")]
        other = scratch.new("phase:builds-unit/7")
        loose = self.base / "scratch" / "loose"
        loose.mkdir()
        self.assertEqual(sorted(scratch.end(ROW)), sorted(mine))
        for path in mine:
            self.assertFalse(path.exists())
        self.assertTrue(other.is_dir())
        self.assertTrue(loose.is_dir())

    def test_adopt_moves_a_loose_folder_in(self) -> None:
        loose = self.base / "rust" / "c54demo"
        (loose / "src").mkdir(parents=True)
        path = scratch.adopt(loose, ROW, "demo", visible=True)
        self.assertFalse(loose.exists())
        self.assertTrue((path / "src").is_dir())
        self.assertEqual(scratch.task_of(path), ROW)

    def test_renew_moves_the_records_time(self) -> None:
        path = scratch.new(RUN)
        scratch.renew(path, now=1_000.0)
        self.assertEqual((path / scratch.RECORD).stat().st_mtime, 1_000.0)
        with self.assertRaises(scratch.ScratchError):
            scratch.renew(self.base)

    def test_end_removes_a_worktree_through_its_source_repo(self) -> None:
        source = self.base / "source"
        environment = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}

        def git(*arguments: str) -> None:
            _ = subprocess.run(["git", "-C", str(source), *arguments], check=True, capture_output=True,
                               env=environment)

        source.mkdir()
        git("init", "-q")
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "c")
        path = scratch.target(RUN, "wt", False)
        git("worktree", "add", "-q", str(path))
        scratch.write_record(path, RUN, 0.0)
        _ = scratch.end(RUN)
        self.assertFalse(path.exists())
        listed = subprocess.run(["git", "-C", str(source), "worktree", "list"], check=True, capture_output=True,
                                text=True, env=environment).stdout
        self.assertNotIn(str(path), listed)


if __name__ == "__main__":
    _ = unittest.main()
