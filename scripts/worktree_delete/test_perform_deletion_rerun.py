"""Retry remote deletion after the confirmed local deletion already finished."""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


class DeletionRerunTests(unittest.TestCase):
    def test_branch_lookup_error_stops_before_remote_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, remote, worktree = root / "repo", root / "remote.git", root / "topic"
            _ = subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
            _ = subprocess.run(["git", "init", "-q", "--initial-branch=main", str(repo)], check=True)

            def git(*args: str) -> str:
                return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout

            _ = git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false",
                    "commit", "--allow-empty", "-qm", "fixture")
            _ = git("remote", "add", "origin", str(remote))
            _ = git("worktree", "add", "-qb", "topic", str(worktree))
            _ = git("push", "-q", "origin", "topic")
            scripts = Path(__file__).resolve().parent
            _ = subprocess.run(["bash", str(scripts / "record_confirmation.sh"), str(worktree), "topic"],
                               cwd=repo, capture_output=True, text=True, check=True)

            real_git = shutil.which("git")
            assert real_git is not None
            shim_dir = root / "bin"
            shim_dir.mkdir()
            remote_reached = root / "remote-reached"
            shim = shim_dir / "git"
            _ = shim.write_text("\n".join([
                "#!/bin/sh",
                "if [ \"$1\" = show-ref ]; then",
                "    echo 'fatal: injected branch lookup failure' >&2",
                "    exit 128",
                "fi",
                "if [ \"$1\" = ls-remote ] || [ \"$1\" = push ]; then",
                f"    : > {shlex.quote(str(remote_reached))}",
                "fi",
                f"exec {shlex.quote(real_git)} \"$@\"",
                "",
            ]))
            shim.chmod(0o755)
            result = subprocess.run(
                ["bash", str(scripts / "perform_deletion.sh"), str(worktree), "topic"],
                cwd=repo, capture_output=True, text=True, check=False,
                env={**os.environ, "PATH": str(shim_dir) + os.pathsep + os.environ["PATH"]},
            )
            self.assertEqual(result.returncode, 128, result.stdout + result.stderr)
            self.assertIn("fatal: injected branch lookup failure", result.stderr)
            self.assertFalse(remote_reached.exists())
            self.assertEqual(git("branch", "--list", "topic").strip(), "topic")
            self.assertIn("refs/heads/topic", git("ls-remote", "--heads", "origin", "topic"))

    def test_remote_failure_reruns_with_missing_local_branch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, remote, worktree = root / "repo", root / "remote.git", root / "topic"
            _ = subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
            _ = subprocess.run(["git", "init", "-q", "--initial-branch=main", str(repo)], check=True)

            def git(*args: str) -> str:
                return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout

            _ = git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false",
                    "commit", "--allow-empty", "-qm", "fixture")
            _ = git("remote", "add", "origin", str(remote))
            _ = git("worktree", "add", "-qb", "topic", str(worktree))
            _ = git("push", "-q", "origin", "topic")
            hook = remote / "hooks/pre-receive"
            _ = hook.write_text("#!/bin/sh\nexit 1\n")
            hook.chmod(0o755)
            scripts = Path(__file__).resolve().parent

            def attempt() -> subprocess.CompletedProcess[str]:
                _ = subprocess.run(["bash", str(scripts / "record_confirmation.sh"), str(worktree), "topic"],
                                   cwd=repo, capture_output=True, text=True, check=True)
                return subprocess.run(["bash", str(scripts / "perform_deletion.sh"), str(worktree), "topic"],
                                      cwd=repo, capture_output=True, text=True, check=False)

            first = attempt()
            self.assertNotEqual(first.returncode, 0)
            self.assertFalse(worktree.exists())
            self.assertEqual(git("branch", "--list", "topic"), "")
            self.assertIn("refs/heads/topic", git("ls-remote", "--heads", "origin", "topic"))
            hook.unlink()
            second = attempt()
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            self.assertEqual(git("ls-remote", "--heads", "origin", "topic"), "")


if __name__ == "__main__":
    _ = unittest.main()
