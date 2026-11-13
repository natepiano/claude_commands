"""The Git file reader gives the answers Git gives."""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import override

from scripts.lib import git_files


def git(folder: Path, *arguments: str) -> str:
    return subprocess.run(["git", "-C", str(folder), *arguments], capture_output=True, text=True,
                          check=True).stdout.strip()


class GitFilesTests(unittest.TestCase):
    root: Path = Path()
    main: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.main = self.root / "main"
        self.main.mkdir()
        _ = git(self.main, "init", "-q", "-b", "trunk")
        _ = git(self.main, "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
                "commit", "-q", "--allow-empty", "-m", "start")

    def assert_same_as_git(self, folder: Path) -> None:
        checkout = git_files.locate(folder)
        self.assertEqual(checkout.toplevel, Path(git(folder, "rev-parse", "--show-toplevel")))
        self.assertEqual(checkout.git_dir, Path(git(folder, "rev-parse", "--absolute-git-dir")))
        self.assertEqual(checkout.common_dir,
                         Path(git(folder, "rev-parse", "--path-format=absolute", "--git-common-dir")))
        self.assertEqual(git_files.current_branch(folder), git(folder, "branch", "--show-current"))

    def test_main_checkout_and_its_subfolder(self) -> None:
        (self.main / "sub").mkdir()
        for folder in (self.main, self.main / "sub"):
            with self.subTest(folder=folder):
                self.assert_same_as_git(folder)
        self.assertEqual(git_files.current_branch(self.main), "trunk")

    def test_worktree_file_names_git_dir_and_common_dir(self) -> None:
        worktree = self.root / "linked"
        _ = git(self.main, "worktree", "add", "-q", "-b", "side", str(worktree))
        self.assertTrue((worktree / ".git").is_file())
        self.assert_same_as_git(worktree)
        checkout = git_files.locate(worktree)
        self.assertEqual(checkout.common_dir, self.main / ".git")
        self.assertEqual(checkout.git_dir.parent, self.main / ".git" / "worktrees")
        self.assertEqual(git_files.current_branch(worktree), "side")

    def test_moved_worktree_reads_the_new_folder(self) -> None:
        worktree = self.root / "before"
        _ = git(self.main, "worktree", "add", "-q", "-b", "side", str(worktree))
        moved = self.root / "after"
        _ = git(self.main, "worktree", "move", str(worktree), str(moved))
        self.assert_same_as_git(moved)

    def test_detached_head_has_no_branch(self) -> None:
        worktree = self.root / "detached"
        _ = git(self.main, "worktree", "add", "-q", "--detach", str(worktree))
        _ = git(self.main, "switch", "-q", "--detach")
        for folder in (self.main, worktree):
            with self.subTest(folder=folder):
                self.assert_same_as_git(folder)
                self.assertEqual(git_files.current_branch(folder), "")

    def test_folder_outside_a_checkout_is_refused(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        for folder in (outside, self.root / "missing"):
            with self.subTest(folder=folder), self.assertRaises(git_files.NotACheckout):
                _ = git_files.locate(folder)
        with self.assertRaises(git_files.NotACheckout):
            _ = git_files.current_branch(outside)

    def test_worktree_file_with_a_lost_git_dir_is_refused(self) -> None:
        worktree = self.root / "lost"
        _ = git(self.main, "worktree", "add", "-q", "-b", "side", str(worktree))
        _ = (worktree / ".git").write_text("gitdir: /nonexistent/worktrees/lost\n", encoding="utf-8")
        with self.assertRaises(git_files.NotACheckout):
            _ = git_files.locate(worktree)

