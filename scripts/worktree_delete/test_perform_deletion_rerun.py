"""Preserve work during deletion and complete deletion retries."""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import cast


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
            self.assertEqual(git("branch", "--list", "topic", "--format=%(refname:short)").strip(), "topic")
            self.assertIn("refs/heads/topic", git("ls-remote", "--heads", "origin", "topic"))

    def test_remote_failure_reruns_with_missing_local_branch(self) -> None:
        for state in ("confirmed-rerun", "cleanup", "unlanded", "cleanup-changed", "origin-race",
                      "local-race", "confirmed-no-origin", "without-branch-config", "config-error"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as directory:
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
                if state == "confirmed-rerun":
                    _ = hook.write_text("#!/bin/sh\nexit 1\n")
                    hook.chmod(0o755)
                else:
                    _ = (worktree / "code.txt").write_text("topic work\n")
                    _ = git("-C", str(worktree), "add", "code.txt")
                    _ = git("-C", str(worktree), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                            "-c", "commit.gpgsign=false", "commit", "-qm", "Change code.txt")
                    _ = git("push", "-q", "origin", "topic")
                    if state != "unlanded":
                        _ = git("merge", "--ff-only", "topic")
                    _ = git("push", "-q", "origin", "main")
                    _ = git("remote", "set-head", "origin", "main")
                if state == "confirmed-no-origin":
                    _ = git("remote", "remove", "origin")
                scripts = Path(__file__).resolve().parent
                environment = {**os.environ, "WORKTREES_STATE_DIR": str(root / "worktrees-state")}

                def attempt() -> subprocess.CompletedProcess[str]:
                    if state in ("confirmed-rerun", "confirmed-no-origin"):
                        _ = subprocess.run(["bash", str(scripts / "record_confirmation.sh"), str(worktree), "topic"],
                                           cwd=repo, capture_output=True, text=True, check=True)
                    return subprocess.run(["bash", str(scripts / "perform_deletion.sh"), str(worktree), "topic"],
                                          cwd=repo, env=environment, capture_output=True, text=True, check=False)

                if state not in ("confirmed-rerun", "confirmed-no-origin"):
                    validation = subprocess.run(
                        ["bash", str(scripts / "worktree_delete_validation.sh"), str(worktree)],
                        cwd=repo, env=environment, capture_output=True, text=True, check=True)
                    fields = cast(dict[str, object], json.loads(validation.stdout))
                    if state in ("cleanup", "unlanded"):
                        self.assertEqual(fields["cleanup"], state == "cleanup")
                    else:
                        self.assertTrue(fields["cleanup"])
                    self.assertTrue(fields["cleanup_reason"])
                origin_commit = ""
                local_commit = ""
                if state in ("cleanup-changed", "origin-race", "local-race", "config-error"):
                    real_git = shutil.which("git")
                    assert real_git is not None
                    shim_dir = root / "bin"
                    shim_dir.mkdir()
                    marker = root / "race-injected"
                    if state == "config-error":
                        injection = [
                            'if [ "$1" = config ] && [ "$3" = --remove-section ]; then',
                            "    echo 'injected config failure' >&2",
                            "    exit 3",
                            "fi",
                        ]
                    elif state == "cleanup-changed":
                        injection = [
                            'if [ "$1" = worktree ] && [ "$2" = remove ]; then',
                            f"    if [ ! -f {shlex.quote(str(marker))} ]; then",
                            f"        : > {shlex.quote(str(marker))}",
                            f"        printf 'edit after cleanup\\n' > {shlex.quote(str(worktree / 'code.txt'))}",
                            "    fi",
                            "fi",
                        ]
                    elif state == "origin-race":
                        clone = root / "origin-writer"
                        _ = git("clone", "--branch", "topic", str(remote), str(clone))
                        _ = (clone / "origin.txt").write_text("concurrent origin work\n")
                        _ = git("-C", str(clone), "add", "origin.txt")
                        _ = git("-C", str(clone), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                                "-c", "commit.gpgsign=false", "commit", "-qm", "Change origin.txt")
                        origin_commit = git("-C", str(clone), "rev-parse", "HEAD").strip()
                        injection = [
                            "for argument do",
                            '    if [ "$argument" = --delete ]; then',
                            f"        if [ ! -f {shlex.quote(str(marker))} ]; then",
                            f"            : > {shlex.quote(str(marker))}",
                            f"            {shlex.quote(real_git)} -C {shlex.quote(str(clone))} push origin HEAD:refs/heads/topic >&2 || exit 1",
                            "        fi",
                            "    fi",
                            "done",
                        ]
                    else:
                        clone = root / "local-writer"
                        _ = git("clone", "--branch", "topic", str(remote), str(clone))
                        _ = (clone / "code.txt").write_text("concurrent local work\n")
                        _ = git("-C", str(clone), "add", "code.txt")
                        _ = git("-C", str(clone), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                                "-c", "commit.gpgsign=false", "commit", "-qm", "Change code.txt")
                        local_commit = git("-C", str(clone), "rev-parse", "HEAD").strip()
                        _ = git("fetch", str(clone), "topic")
                        injection = [
                            'if [ "$1" = worktree ] && [ "$2" = remove ]; then',
                            f"    if [ ! -f {shlex.quote(str(marker))} ]; then",
                            f"        : > {shlex.quote(str(marker))}",
                            f"        {shlex.quote(real_git)} -C {shlex.quote(str(worktree))} reset --hard {local_commit} >&2 || exit 1",
                            "    fi",
                            "fi",
                        ]
                    shim = shim_dir / "git"
                    _ = shim.write_text("\n".join([
                        "#!/bin/sh", *injection, f'exec {shlex.quote(real_git)} "$@"', "",
                    ]))
                    shim.chmod(0o755)
                    environment["PATH"] = str(shim_dir) + os.pathsep + os.environ["PATH"]
                _ = git("config", "--local", "branch.topic.merge", "refs/heads/topic")
                if state == "without-branch-config":
                    _ = git("config", "--local", "--remove-section", "branch.topic")
                before_branches = git("for-each-ref", "--format=%(refname) %(objectname)", "refs/heads")
                before_origin = "" if state == "confirmed-no-origin" else git("ls-remote", "--heads", "origin")
                before_worktrees = git("worktree", "list", "--porcelain")

                first = attempt()

                if state == "confirmed-rerun":
                    self.assertNotEqual(first.returncode, 0)
                    self.assertTrue(worktree.exists())
                    self.assertEqual(git("branch", "--list", "topic", "--format=%(refname:short)").strip(), "topic")
                    self.assertIn("refs/heads/topic", git("ls-remote", "--heads", "origin", "topic"))
                    hook.unlink()
                    second = attempt()
                    self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
                    self.assertFalse(worktree.exists())
                    self.assertEqual(git("branch", "--list", "topic"), "")
                    self.assertEqual(git("ls-remote", "--heads", "origin", "topic"), "")
                elif state in ("cleanup", "without-branch-config"):
                    self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
                    self.assertFalse(worktree.exists())
                    self.assertEqual(git("branch", "--list", "topic"), "")
                    self.assertEqual(git("ls-remote", "--heads", "origin", "topic"), "")
                elif state == "confirmed-no-origin":
                    self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
                    self.assertFalse(worktree.exists())
                    self.assertEqual(git("branch", "--list", "topic"), "")
                    self.assertNotIn("origin", git("remote").splitlines())
                elif state == "config-error":
                    self.assertEqual(first.returncode, 3, first.stdout + first.stderr)
                    self.assertIn("injected config failure", first.stderr)
                    self.assertFalse(worktree.exists())
                    self.assertEqual(git("branch", "--list", "topic"), "")
                    self.assertEqual(git("config", "--local", "--get", "branch.topic.merge"), "refs/heads/topic\n")
                    (root / "bin/git").unlink()
                    _ = subprocess.run(["bash", str(scripts / "record_confirmation.sh"), str(worktree), "topic"],
                                       cwd=repo, capture_output=True, text=True, check=True)

                    second = attempt()

                    self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
                    self.assertFalse(worktree.exists())
                    self.assertEqual(git("branch", "--list", "topic"), "")
                    self.assertEqual(git("ls-remote", "--heads", "origin", "topic"), "")
                elif state == "cleanup-changed":
                    self.assertNotEqual(first.returncode, 0)
                    self.assertIn("the tree changed after the cleanup check", first.stdout)
                    self.assertEqual((worktree / "code.txt").read_text(), "edit after cleanup\n")
                    self.assertEqual(git("for-each-ref", "--format=%(refname) %(objectname)", "refs/heads"), before_branches)
                    self.assertEqual(git("worktree", "list", "--porcelain"), before_worktrees)
                elif state == "origin-race":
                    self.assertNotEqual(first.returncode, 0)
                    self.assertIn("origin branch deletion failed", first.stdout)
                    self.assertIn("stale info", first.stderr)
                    self.assertEqual(git("ls-remote", "--heads", "origin", "topic").split()[0], origin_commit)
                    self.assertEqual((worktree / "code.txt").read_text(), "topic work\n")
                    self.assertEqual(git("for-each-ref", "--format=%(refname) %(objectname)", "refs/heads"), before_branches)
                    self.assertEqual(git("worktree", "list", "--porcelain"), before_worktrees)
                elif state == "local-race":
                    self.assertNotEqual(first.returncode, 0)
                    self.assertIn("the local branch changed after the cleanup check", first.stdout)
                    self.assertEqual(git("rev-parse", "refs/heads/topic").strip(), local_commit)
                    self.assertFalse(worktree.exists())
                    self.assertEqual(git("show", "topic:code.txt"), "concurrent local work\n")
                    self.assertEqual(git("rev-parse", "topic").strip(), local_commit)
                else:
                    self.assertNotEqual(first.returncode, 0)
                    self.assertIn("no confirmation nonce", first.stdout)
                    self.assertIn("work has not landed", first.stdout)
                    self.assertTrue(worktree.exists())
                    self.assertEqual((worktree / "code.txt").read_text(), "topic work\n")
                    self.assertEqual(git("for-each-ref", "--format=%(refname) %(objectname)", "refs/heads"), before_branches)
                    self.assertEqual(git("ls-remote", "--heads", "origin"), before_origin)
                    self.assertEqual(git("worktree", "list", "--porcelain"), before_worktrees)
                if state in ("confirmed-rerun", "cleanup", "confirmed-no-origin", "without-branch-config", "config-error"):
                    config = subprocess.run(["git", "-C", str(repo), "config", "--local", "--get", "branch.topic.merge"],
                                            capture_output=True, text=True, check=False)
                    self.assertEqual(config.returncode, 1, config.stdout + config.stderr)
                    self.assertEqual(config.stdout, "")


if __name__ == "__main__":
    _ = unittest.main()
