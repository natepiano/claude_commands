"""The migrated vault committer keeps unrelated changes out of its commits."""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import tempfile
import unittest
from typing import override


from scripts.maintenance.vault_commit import main as tool_main, subject


def sh(repo: str, *args: str) -> str:
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, check=True).stdout


def write(repo: str, rel: str, text: str) -> str:
    path = os.path.join(repo, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        _ = handle.write(text)
    return path


class Vault(unittest.TestCase):
    """A repo with two generated notes and one hand-edited note, all committed."""

    repo: str = ""
    a: str = ""
    b: str = ""

    @override
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = os.path.realpath(tmp.name)
        _ = sh(self.repo, "init", "-q", "-b", "main")
        _ = sh(self.repo, "config", "user.name", "test")
        _ = sh(self.repo, "config", "user.email", "test@example.invalid")
        self.a = write(self.repo, "gen/a b.md", "date_modified: 1\nbody a\n")
        self.b = write(self.repo, "gen/b.md", "date_modified: 1\nbody b\n")
        _ = write(self.repo, "hand.md", "mine\n")
        _ = sh(self.repo, "add", "-A")
        _ = sh(self.repo, "commit", "-q", "-m", "start")

    def run_tool(self, *paths: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = tool_main(["--job", "job", "--timestamp", r"^date_modified:", "--", *paths])
        return code, out.getvalue(), err.getvalue()

    def commits(self) -> int:
        return int(sh(self.repo, "rev-list", "--count", "HEAD"))


class Tool(Vault):
    def test_timestamp_only_change_is_not_committed(self) -> None:
        _ = write(self.repo, "gen/a b.md", "date_modified: 2\nbody a\n")
        code, out, _ = self.run_tool(self.a, self.b)
        self.assertEqual((code, self.commits()), (0, 1))
        self.assertIn("nothing to commit", out)

    def test_commits_only_changed_job_paths_and_leaves_the_rest(self) -> None:
        _ = write(self.repo, "gen/a b.md", "date_modified: 2\nbody a, changed\n")
        _ = write(self.repo, "gen/b.md", "date_modified: 2\nbody b\n")
        _ = write(self.repo, "hand.md", "mine, unstaged\n")
        _ = write(self.repo, "staged.md", "someone staged this\n")
        _ = sh(self.repo, "add", "staged.md")
        code, out, _ = self.run_tool(self.a, self.b)
        self.assertEqual((code, self.commits()), (0, 2))
        self.assertEqual(sh(self.repo, "log", "-1", "--format=%s").strip(), "job: update gen/a b")
        self.assertEqual(sh(self.repo, "show", "--name-only", "--format=", "HEAD").split("\n")[0], "gen/a b.md")
        status = sh(self.repo, "status", "--porcelain").splitlines()
        self.assertEqual(sorted(status), sorted([" M gen/b.md", " M hand.md", "A  staged.md"]))
        self.assertIn("job: update gen/a b", out)

    def test_new_and_deleted_notes_are_committed(self) -> None:
        new = write(self.repo, "gen/c.md", "date_modified: 1\nbody c\n")
        os.remove(self.b)
        code, _, _ = self.run_tool(self.a, self.b, new)
        self.assertEqual(code, 0)
        self.assertEqual(sh(self.repo, "status", "--porcelain"), "")
        self.assertEqual(sh(self.repo, "ls-files", "gen").splitlines(), ["gen/a b.md", "gen/c.md"])

    def test_a_held_lock_fails_softly_and_the_next_run_commits(self) -> None:
        _ = write(self.repo, "gen/b.md", "date_modified: 1\nbody b, changed\n")
        lock = os.path.join(self.repo, ".git", "index.lock")
        with open(lock, "w", encoding="utf-8"):
            pass
        code, _, err = self.run_tool(self.a, self.b)
        self.assertEqual((code, self.commits()), (0, 1))
        self.assertIn("commit failed, next run retries", err)
        os.remove(lock)
        code, _, _ = self.run_tool(self.a, self.b)
        self.assertEqual((code, self.commits()), (0, 2))

    def test_outside_a_repo_does_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as elsewhere:
            path = write(elsewhere, "x.md", "x\n")
            code, _, err = self.run_tool(path)
        self.assertEqual(code, 0)
        self.assertIn("not in a git repo", err)


class Subject(unittest.TestCase):
    def test_names_a_few_then_counts(self) -> None:
        self.assertEqual(subject("j", ["a.md", "b.md"]), "j: update a, b")
        five = [f"{n}.md" for n in "abcde"]
        self.assertEqual(subject("j", five), "j: update a, b, c, d, e")
        self.assertEqual(subject("j", [*five, "f.md"]), "j: update a, b, c, d, 2 more")


if __name__ == "__main__":
    _ = unittest.main()
