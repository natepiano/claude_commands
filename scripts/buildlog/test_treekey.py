#!/usr/bin/env python3
"""Tests for treekey.py: the key follows the files, from any folder of the worktree, in any clone."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import override

import treekey
from test_index import use_test_log

use_test_log()

# No signing, hooks or identity from the user's own git config, for the
# test's commits and for treekey's own git calls alike.
GIT_ENVIRONMENT = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "test",
    "GIT_COMMITTER_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def git(*args: str, cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def key(directory: Path) -> str:
    found = treekey.tree_key(str(directory))
    assert found is not None, f"no key for {directory}"
    return found


class TreeKeyTests(unittest.TestCase):
    base: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    repo: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    clone: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    clean: str  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        for name, value in GIT_ENVIRONMENT.items():
            previous = os.environ.get(name)
            os.environ[name] = value
            self.addCleanup(self.restore, name, previous)
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.repo = self.base / "repo"
        (self.repo / "src").mkdir(parents=True)
        _ = (self.repo / "src" / "lib.rs").write_text("pub fn one() {}\n")
        _ = (self.repo / "README").write_text("readme\n")
        _ = (self.repo / ".gitignore").write_text("target/\n")
        (self.repo / "link").symlink_to("README")
        _ = git("init", "-q", "-b", "main", cwd=self.repo)
        _ = git("add", "-A", cwd=self.repo)
        _ = git("commit", "-q", "-m", "first", cwd=self.repo)
        self.clone = self.base / "clone"
        _ = git("clone", "-q", str(self.repo), str(self.clone), cwd=self.base)
        self.clean = key(self.repo)

    @staticmethod
    def restore(name: str, previous: str | None) -> None:
        if previous is None:
            _ = os.environ.pop(name, None)
        else:
            os.environ[name] = previous

    def test_clean_clones_agree_from_any_folder(self) -> None:
        self.assertRegex(self.clean, r"^[0-9a-f]{64}$")
        self.assertEqual(key(self.clone), self.clean)
        self.assertEqual(key(self.repo / "src"), self.clean)

    def test_modified_file(self) -> None:
        source = self.repo / "src" / "lib.rs"
        _ = source.write_text("pub fn two() {}\n")
        self.assertNotEqual(key(self.repo), self.clean)
        _ = (self.clone / "src" / "lib.rs").write_text("pub fn two() {}\n")
        self.assertEqual(key(self.clone), key(self.repo))
        _ = source.write_text("pub fn one() {}\n")
        self.assertEqual(key(self.repo), self.clean)

    def test_staged_and_unstaged_alike(self) -> None:
        _ = (self.repo / "README").write_text("changed\n")
        _ = git("add", "README", cwd=self.repo)
        _ = (self.clone / "README").write_text("changed\n")
        self.assertNotEqual(key(self.repo), self.clean)
        self.assertEqual(key(self.repo), key(self.clone))

    def test_deleted_file(self) -> None:
        (self.repo / "README").unlink()
        deleted = key(self.repo)
        self.assertNotEqual(deleted, self.clean)
        _ = git("rm", "-q", "README", cwd=self.clone)
        self.assertEqual(key(self.clone), deleted)

    def test_untracked_counts_and_ignored_does_not(self) -> None:
        (self.repo / "target").mkdir()
        _ = (self.repo / "target" / "out.rlib").write_text("build output\n")
        self.assertEqual(key(self.repo), self.clean)
        _ = (self.repo / "src" / "new.rs").write_text("\n")
        _ = (self.repo / "README").write_text("changed\n")
        self.assertNotEqual(key(self.repo), self.clean)
        self.assertEqual(key(self.repo / "src"), key(self.repo))

    def test_symlink_target(self) -> None:
        link = self.repo / "link"
        link.unlink()
        link.symlink_to("src/lib.rs")
        self.assertNotEqual(key(self.repo), self.clean)

    def test_index_is_left_alone(self) -> None:
        # A new mtime with the same bytes makes the index's stat data stale;
        # a key that refreshed it would rewrite .git/index under another session.
        index = self.repo / ".git" / "index"
        before = index.stat().st_mtime_ns
        source = self.repo / "src" / "lib.rs"
        os.utime(source, ns=(source.stat().st_atime_ns, source.stat().st_mtime_ns + 5_000_000_000))
        self.assertEqual(key(self.repo), self.clean)
        self.assertEqual(index.stat().st_mtime_ns, before)
        self.assertFalse((self.repo / ".git" / "index.lock").exists())

    def test_no_key_outside_git_or_before_a_commit(self) -> None:
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        self.assertIsNone(treekey.tree_key(str(elsewhere)))
        self.assertIsNone(treekey.tree_key(str(self.base / "missing")))
        _ = git("init", "-q", cwd=elsewhere)
        self.assertIsNone(treekey.tree_key(str(elsewhere)))

    def test_worktree_is_the_top_folder(self) -> None:
        self.assertEqual(treekey.worktree(str(self.repo / "src")), str(self.repo))
        self.assertIsNone(treekey.worktree(str(self.base)))


if __name__ == "__main__":
    _ = unittest.main()
