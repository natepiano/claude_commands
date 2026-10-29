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
            scratch.HOLDS_ENV: str(self.base / "holds.json"), "CLAUDE_CODE_SESSION_ID": "maker-session"}))

    def git(self, repo: Path, *arguments: str) -> None:
        _ = subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *arguments],
                           check=True, capture_output=True)

    def test_no_task_lasts_a_day_and_until_ends_at_its_time(self) -> None:
        path = scratch.new(now=1_000_000.0)
        task = scratch.task_of(path) or ""
        self.assertEqual(scratch.task_end(task), 1_000_000.0 + scratch.DEFAULT_LIFE_SECONDS - 40)
        self.assertFalse(scratch.ended(task, 1_000_000.0))
        self.assertTrue(scratch.ended(task, 1_000_000.0 + scratch.DEFAULT_LIFE_SECONDS))
        self.assertFalse(scratch.ended(RUN, 9e12))
        self.assertTrue(scratch.parsed_until("2026-10-28 09:30").startswith("until:2026-10-28T"))
        with self.assertRaises(scratch.ScratchError):
            _ = scratch.parsed_until("tomorrow")

    def test_a_held_folder_goes_with_its_task_unless_a_repo_in_it_has_unsaved_work(self) -> None:
        saved, unsaved = self.base / "saved", self.base / "unsaved" / "repo"
        for repo in (saved, unsaved):
            repo.mkdir(parents=True)
            self.git(repo, "init", "-q")
        _ = (unsaved / "a").write_text("x")
        self.git(unsaved, "add", "a")
        self.git(unsaved, "commit", "-qm", "a")
        task = "row:natedev-C9"
        for folder in (saved, unsaved.parent):
            _ = scratch.hold(folder, task, "change review C9")
        self.assertEqual(scratch.holds()[str(saved)]["maker"], "maker-session")
        self.assertEqual(scratch.unsaved_work(unsaved.parent), f"{unsaved}: it has commits and no remote")
        self.assertEqual(scratch.end(task), [saved])
        self.assertFalse(saved.exists())
        self.assertTrue(unsaved.is_dir())
        self.assertGreater(scratch.holds()[str(unsaved.parent)]["ended"], 0)
        self.assertTrue(scratch.release(unsaved.parent))
        self.assertEqual(scratch.holds(), {})

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

    def test_end_keep_predicate_preserves_folders_and_holds_until_the_command_finishes(self) -> None:
        kept = scratch.new(RUN, "live")
        removed = scratch.new(RUN, "old")
        held = self.base / "held-command"
        held.mkdir()
        _ = scratch.hold(held, RUN, "command cwd")
        before = scratch.holds()[str(held)].copy()
        other = scratch.new(ROW, "other")
        self.assertEqual(scratch.end(RUN, keep=lambda path: path in (kept, held)), [removed])
        self.assertTrue(kept.is_dir())
        self.assertTrue(held.is_dir())
        self.assertTrue(other.is_dir())
        self.assertEqual(scratch.holds()[str(held)], before)
        self.assertCountEqual(scratch.end(RUN), [kept, held])
        self.assertEqual(scratch.holds(), {})
        with self.assertRaises(scratch.ScratchError):
            _ = scratch.end("invalid-task", keep=lambda path: True)

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
