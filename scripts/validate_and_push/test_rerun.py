"""Validation and merged-branch recovery use only a local bare remote."""
from __future__ import annotations

from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.lib.rerun_test_support import ScriptRepositoryTestCase


class ValidationRerunTests(ScriptRepositoryTestCase):
    def install_gh(self) -> None:
        _ = self.stub("gh", '''import subprocess, sys
args = sys.argv[1:]
if args[:2] == ["repo", "view"]:
    print("main")
elif args[:2] == ["pr", "view"]:
    if "mergeCommit" in args:
        subprocess.run(["git", "rev-parse", "main"], check=True)
    else:
        print(7)
elif args[:2] == ["pr", "checks"]:
    print("checks pass")
elif args[:2] == ["pr", "merge"]:
    subprocess.run(["git", "switch", "main"], check=True)
    subprocess.run(["git", "merge", "--ff-only", "retry-pr"], check=True)
    subprocess.run(["git", "push", "origin", "main"], check=True)
else:
    sys.exit("unexpected gh call")
''')

    def install_autofix(self) -> None:
        lint = self.stub("lint", '''import os, sys
from pathlib import Path
if sys.argv[1] == "fmt":
    Path("seed.txt").write_text("formatted\\n")
    stop = Path(os.environ["HOME"]) / "stop-formatter"
    if stop.exists():
        stop.unlink()
        sys.exit(86)
''')
        target = self.home / ".claude/scripts/lint/lint"
        target.parent.mkdir(parents=True)
        _ = shutil.copy2(lint, target)
        _ = self.stub("taplo", "pass\n")
        self.environment["VALIDATE_QUICK"] = "1"

    def test_a_synced_default_branch_retries_its_failed_post_push_hook(self) -> None:
        hook = '''#!/usr/bin/env bash
set -euo pipefail
echo "$LANDED_SHA" >> .git/hook-runs
if [[ ! -f hook-started ]]; then touch hook-started; exit 9; fi
touch hook-finished
'''
        _ = self.write(self.repo / ".claude/config/post_push.sh", hook)
        self.seed()
        _ = self.write(self.repo / "seed.txt", "next\n")
        _ = self.git("commit", "-am", "next")
        self.install_gh()
        stopped = self.script("validate_and_push/push_pr_branch_and_merge.sh", "retry-pr")
        self.assertNotEqual(stopped.returncode, 0)
        self.assertEqual(self.git("rev-parse", "HEAD").stdout, self.git("rev-parse", "origin/main").stdout)
        landed = self.git("rev-parse", "HEAD").stdout
        finished = self.script("validate_and_push/push_pr_branch_and_merge.sh", "retry-pr")
        self.assertEqual(finished.returncode, 0, finished.stderr)
        self.assertTrue((self.repo / "hook-finished").exists())
        self.assertEqual(self.git("rev-parse", "HEAD").stdout, landed)
        self.assertEqual(self.git("rev-list", "--count", "HEAD").stdout.strip(), "2")
        repeated = self.script("validate_and_push/push_pr_branch_and_merge.sh", "retry-pr")
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertEqual((self.repo / ".git/hook-runs").read_text().splitlines(), [landed.strip()] * 2)

    def test_a_successful_post_push_hook_runs_once_per_landed_commit(self) -> None:
        _ = self.write(self.repo / ".claude/config/post_push.sh", 'echo "$LANDED_SHA" >> .git/hook-runs\n')
        self.seed()
        self.install_gh()
        first = self.git("rev-parse", "HEAD").stdout.strip()
        # Stop after publishing the completion, before the script exits.
        self.stop_command("mv", target=".git/post-push-completed", after=True)
        stopped = self.script("validate_and_push/push_pr_branch_and_merge.sh", "retry-pr")
        self.assertNotEqual(stopped.returncode, 0)
        repeated = self.script("validate_and_push/push_pr_branch_and_merge.sh", "retry-pr")
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertEqual((self.repo / ".git/hook-runs").read_text().splitlines(), [first])
        _ = self.write(self.repo / "seed.txt", "next\n")
        _ = self.git("commit", "-am", "next")
        _ = self.git("push", "origin", "main")
        second = self.git("rev-parse", "HEAD").stdout.strip()
        result = self.script("validate_and_push/push_pr_branch_and_merge.sh", "retry-pr")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.repo / ".git/hook-runs").read_text().splitlines(), [first, second])

    def test_a_pending_hook_fast_forwards_before_reading_its_landed_commit(self) -> None:
        _ = self.write(self.repo / ".claude/config/post_push.sh", 'echo "$LANDED_SHA" >> .git/hook-runs\n')
        self.seed()
        self.install_gh()
        before = self.git("rev-parse", "HEAD").stdout.strip()
        _ = self.write(self.repo / "seed.txt", "remote tip\n")
        _ = self.git("commit", "-am", "remote tip")
        _ = self.git("push", "origin", "main")
        remote = self.git("rev-parse", "HEAD").stdout.strip()
        _ = self.git("reset", "--hard", before)
        result = self.script("validate_and_push/push_pr_branch_and_merge.sh", "retry-pr")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(), remote)
        self.assertEqual((self.repo / ".git/hook-runs").read_text().splitlines(), [remote])

    def test_validation_finishes_its_failed_autofix_commit_on_rerun(self) -> None:
        self.seed()
        self.install_autofix()
        self.fail_git("commit")
        stopped = self.script("validate_and_push/validate_ci.sh")
        self.assertNotEqual(stopped.returncode, 0)
        self.assertTrue((self.repo / ".git/validate-autofix").exists())
        self.assertNotEqual(self.git("status", "--porcelain").stdout, "")
        finished = self.script("validate_and_push/validate_ci.sh")
        self.assertEqual(finished.returncode, 0, finished.stdout + finished.stderr)
        self.assertEqual(self.git("status", "--porcelain").stdout, "")
        self.assertEqual((self.repo / "seed.txt").read_text(), "formatted\n")
        self.assertEqual(self.git("rev-list", "--count", "HEAD").stdout.strip(), "1")
        self.assertFalse((self.repo / ".git/validate-autofix").exists())
        self.assertEqual(self.git("show", "--format=", "--name-only", "HEAD").stdout.strip(), "seed.txt")

    def test_a_marker_saved_before_formatting_does_not_claim_later_user_edits(self) -> None:
        self.seed()
        self.install_autofix()
        before = self.git("rev-parse", "HEAD").stdout
        self.stop_command("mv", target=".git/validate-autofix", after=True)
        stopped = self.script("validate_and_push/validate_ci.sh")
        self.assertNotEqual(stopped.returncode, 0)
        _ = self.write(self.repo / "user.txt", "user bytes\n")
        self.assert_user_edit_refused(before)
        self.assertEqual((self.repo / "seed.txt").read_text(), "seed\n")

    def test_a_user_edit_after_the_saved_autofix_output_is_refused(self) -> None:
        self.seed()
        self.install_autofix()
        before = self.git("rev-parse", "HEAD").stdout
        self.fail_git("commit")
        stopped = self.script("validate_and_push/validate_ci.sh")
        self.assertNotEqual(stopped.returncode, 0)
        _ = self.write(self.repo / "user.txt", "user bytes\n")
        self.assert_user_edit_refused(before)
        self.assertEqual((self.repo / "seed.txt").read_text(), "formatted\n")

    def test_a_force_added_ignored_file_is_outside_the_saved_autofix_output(self) -> None:
        _ = self.write(self.repo / ".gitignore", "user.txt\n")
        self.seed()
        self.install_autofix()
        before = self.git("rev-parse", "HEAD").stdout
        self.fail_git("commit")
        stopped = self.script("validate_and_push/validate_ci.sh")
        self.assertNotEqual(stopped.returncode, 0)
        _ = self.write(self.repo / "user.txt", "user bytes\n")
        _ = self.git("add", "-f", "user.txt")
        self.assert_user_edit_refused(before)
        self.assertIn("user.txt", self.git("diff", "--cached", "--name-only").stdout)

    def test_an_interrupted_formatter_preserves_its_unrecorded_output_and_explains_recovery(self) -> None:
        self.seed()
        self.install_autofix()
        before = self.git("rev-parse", "HEAD").stdout
        index = (self.repo / ".git/index").read_bytes()
        _ = self.write(self.home / "stop-formatter", "stop\n")
        stopped = self.script("validate_and_push/validate_ci.sh")
        self.assertNotEqual(stopped.returncode, 0)
        result = self.script("validate_and_push/validate_ci.sh")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("formatter stopped before its output was recorded", result.stderr)
        self.assertIn("seed.txt", result.stderr)
        self.assertIn("Inspect git diff", result.stderr)
        self.assertIn("git checkout -- <paths> and rerun", result.stderr)
        self.assertIn("commit the changes yourself", result.stderr)
        self.assertEqual((self.repo / "seed.txt").read_text(), "formatted\n")
        self.assertEqual(self.git("rev-parse", "HEAD").stdout, before)
        self.assertEqual((self.repo / ".git/index").read_bytes(), index)
        self.assertEqual(self.git("diff", "--cached", "--name-only").stdout, "")

    def test_autofix_content_proofs_do_not_run_index_hooks(self) -> None:
        self.seed()
        self.install_autofix()
        hook_log = self.root / "index-hook-runs"
        self.environment["TEST_INDEX_HOOK_LOG"] = str(hook_log)
        hook = self.write(self.repo / ".git/hooks/post-index-change", '''#!/bin/sh
printf '%s\\n' "${GIT_INDEX_FILE:-real}" >> "$TEST_INDEX_HOOK_LOG"
''')
        hook.chmod(0o755)
        result = self.script("validate_and_push/validate_ci.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = hook_log.read_text().splitlines()
        self.assertTrue(calls, "The real commit still runs repository hooks")
        self.assertFalse(any("validate-index." in call for call in calls), calls)
        self.assertEqual((self.repo / "seed.txt").read_text(), "formatted\n")

    def assert_user_edit_refused(self, before: str) -> None:
        index = (self.repo / ".git/index").read_bytes()
        result = self.script("validate_and_push/validate_ci.sh")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("user.txt", result.stderr)
        self.assertEqual((self.repo / "user.txt").read_text(), "user bytes\n")
        self.assertEqual(self.git("rev-parse", "HEAD").stdout, before)
        self.assertEqual((self.repo / ".git/index").read_bytes(), index)

    def test_validation_still_refuses_unowned_dirty_work(self) -> None:
        self.seed()
        _ = self.write(self.repo / "seed.txt", "user edit\n")
        result = self.script("validate_and_push/validate_ci.sh")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("uncommitted changes", result.stdout)
