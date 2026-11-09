"""Exercise checkpoint merges against disposable repositories and command stubs."""

from __future__ import annotations

from scripts.production.cli_fixture import process_boundary

import json
import shutil
import unittest
from pathlib import Path
from typing import cast, final, override

from scripts.production import change_review
from scripts.production.staff_test_support import isolated_staff_file


# Importable once merge_checkpoint has put scripts/lib on the path.


from scripts.production.merge_checkpoint_test_support import MergeCheckpointFixture, configure_test_environment


def setUpModule() -> None:
    configure_test_environment()


@final
class MergeCheckpointTests(MergeCheckpointFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        staff_file = self.root / "staff.json"
        _ = Path(self.env["STAFF_FILE"]).rename(staff_file)
        _ = self.enterContext(isolated_staff_file(staff_file, self.env))

    # Keep the child CLI process boundary.
    @process_boundary
    def test_attached_child_handoff_merges_without_a_units_row_and_acknowledges_after_push(self) -> None:
        _ = self.child_production()
        checkpoint = self.checkpoint()
        result = self.run_handoff(checkpoint)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        merge = self.git("rev-parse", "HEAD")
        self.assertEqual(self.git("log", "-1", "--format=%s"),
                         f"Merge example-style hand-off ({checkpoint[:7]}) into production")
        self.assertEqual(self.git("rev-parse", "HEAD^2"), checkpoint)
        self.assertEqual(self.git("ls-remote", "origin", "refs/heads/production").split()[0], merge)
        self.assertEqual((self.state / "notified-tip").read_text(), merge)
        self.assertEqual(self.events("send.py"), [[
            "--to", "session:fake-child", "--from", "example", "--text",
            f"From the showrunner of example: hand-off {checkpoint[:7]} merged as {merge[:7]}.", "--timeout", "10",
        ]])
        rows = [cast(dict[str, object], json.loads(line)) for line in (self.state / "events.jsonl").read_text().splitlines()]
        commands = [row["command"] for row in rows]
        self.assertLess(commands.index("style_sync.py"), commands.index("send.py"))
        self.assertLess(commands.index("style_sync.py"), commands.index("style_ledger.py"))
        self.assertLess(commands.index("style_ledger.py"), commands.index("send.py"))
        self.assertEqual(self.events("style_ledger.py"), [[
            "handoff", "merged", "--repo", str(self.unit), "--checkpoint", checkpoint,
            "--merge-commit", merge,
        ]])
    def test_unattached_child_is_refused_before_merge_push_or_acknowledgment(self) -> None:
        _ = self.child_production(parent="someone-else")
        checkpoint = self.checkpoint()
        before = self.git("rev-parse", "HEAD")
        remote = self.git("ls-remote", "origin", "refs/heads/production")
        result = self.run_handoff(checkpoint)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("example-style is not attached under example", result.stdout)
        self.assertEqual(len(result.stdout.splitlines()), 1)
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertEqual(self.git("ls-remote", "origin", "refs/heads/production"), remote)
        self.assertEqual(self.events("send.py"), [])
        self.assertEqual(self.events("style_sync.py"), [])
    def test_child_checkpoint_off_its_merge_branch_is_refused(self) -> None:
        _ = self.child_production()
        self.write(self.other, "src/beta.txt", "unrelated checkpoint\n")
        _ = self.git("commit", "-am", "Other work", cwd=self.other)
        checkpoint = self.git("rev-parse", "HEAD", cwd=self.other)
        before = self.git("rev-parse", "HEAD")
        result = self.run_handoff(checkpoint)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"checkpoint {checkpoint[:7]} is not on example-style's merge branch alpha-branch", result.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertEqual(self.events("send.py"), [])
    def test_root_style_handoff_merges_itself_without_an_attachment(self) -> None:
        _ = self.child_production(parent="")
        checkpoint = self.checkpoint()
        refused = self.run_handoff(checkpoint)
        self.assertNotEqual(refused.returncode, 0)
        result = self.run_handoff(checkpoint, "--root-self")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD^2"), checkpoint)
        self.assertEqual(self.events("send.py"), [])
        self.assertEqual(self.events("style_ledger.py"), [])
        self.assertIn(f"root: run style_ledger.py reconcile to mark {checkpoint[:7]} merged", result.stdout)
    def test_duplicate_child_notice_acknowledges_without_another_merge_push_or_notify(self) -> None:
        self.promotions = str(self.promote)
        review = Path(self.env["HOME"]) / "rust/hanadocs/change-review/change-review - example.md"
        self.production_doc(change_review=review)
        _ = self.child_production()
        checkpoint = self.dated_checkpoint("checkpoint(alpha): handoff\n\nChange-Review: Child ready | Read | Listed")
        self.assertEqual(self.run_handoff(checkpoint).returncode, 0)
        self.assertEqual([row["Change"] for row in change_review.pending(review)], ["Child ready"])
        written = review.read_bytes()
        merge = self.git("rev-parse", "HEAD")
        count = self.git("rev-list", "--count", "HEAD")
        notifications = self.events("style_sync.py")
        self.write(self.promote, "src/alpha.txt", "local changes must not be promoted over\n")
        real_git = shutil.which("git")
        if real_git is None:
            self.fail("git is required for the checkpoint fixture")
        self.executable(self.bin / "git", "\n".join([
            "#!/usr/bin/env python3", "import os,sys", "from pathlib import Path",
            "if 'push' in sys.argv[1:]:",
            f" Path({str(self.state / 'unexpected-push')!r}).touch()",
            " raise SystemExit(91)", f"os.execv({real_git!r}, [{real_git!r}, *sys.argv[1:]])", "",
        ]))
        result = self.run_handoff(checkpoint)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), f"hand-off {checkpoint[:7]} already merged as {merge[:7]}")
        self.assertEqual(self.git("rev-list", "--count", "HEAD"), count)
        self.assertEqual(self.git("rev-parse", "HEAD"), merge)
        self.assertEqual(self.events("style_sync.py"), notifications)
        self.assertFalse((self.state / "unexpected-push").exists())
        self.assertEqual((self.promote / "src/alpha.txt").read_text(), "local changes must not be promoted over\n")
        self.assertEqual(len(self.events("send.py")), 2)
        self.assertEqual(self.events("send.py")[0], self.events("send.py")[1])
        self.assertEqual(review.read_bytes(), written)
    def test_failed_handoff_push_rolls_back_and_retry_acknowledges_only_after_push(self) -> None:
        _ = self.child_production()
        checkpoint = self.checkpoint()
        before = self.git("rev-parse", "HEAD")
        marker = self.state / "reject-once"
        marker.touch()
        self.executable(self.origin / "hooks/pre-receive", (
            "#!/bin/sh\nif [ -e '" + str(marker) + "' ]; then\n"
            + " rm '" + str(marker) + "'\n exit 1\nfi\ncat >/dev/null\n"
        ))
        failed = self.run_handoff(checkpoint)
        self.assertNotEqual(failed.returncode, 0, failed.stdout + failed.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), before)
        self.assertEqual(self.events("send.py"), [])
        self.assertEqual(self.events("style_sync.py"), [])
        self.assertEqual(self.receipts(), [])
        retried = self.run_handoff(checkpoint)
        self.assertEqual(retried.returncode, 0, retried.stdout + retried.stderr)
        merge = self.git("rev-parse", "HEAD")
        self.assertEqual(self.git("rev-parse", "HEAD^1"), before)
        self.assertEqual(self.git("rev-parse", "HEAD^2"), checkpoint)
        self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), merge)
        self.assertEqual((self.state / "notified-tip").read_text(), merge)
        self.assertEqual(len(self.events("send.py")), 1)
        self.assertLess(retried.stdout.index("push: ok"), retried.stdout.index("hand-off "))
    def assert_child_push_conflict_is_held(self, *, already_pushed: bool) -> None:
        self.promotions = str(self.promote)
        self.production_doc()
        _ = self.child_production()
        _ = self.checkpoint(content="child alpha\n")
        checkpoint = self.checkpoint("src/beta.txt", "child beta\n")
        if already_pushed:
            _ = self.git("merge", "--no-ff", "-m", "Merge child handoff", checkpoint)
            _ = self.git("push", "origin", "production")
        before = self.git("rev-parse", "HEAD")
        remote_before = self.git("rev-parse", "production", cwd=self.origin)
        for path in ("src/alpha.txt", "src/beta.txt"):
            self.write(self.promote, path, "main side\n")
        _ = self.git("add", "src", cwd=self.promote)
        _ = self.git("commit", "-m", "Conflicting main edits", cwd=self.promote)
        _ = self.git("push", "origin", "main", cwd=self.promote)
        main_before = self.git("rev-parse", "HEAD", cwd=self.promote)

        result = self.run_handoff(checkpoint)

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("push: held", result.stdout)
        self.assertIn("src/alpha.txt, src/beta.txt", result.stdout)
        self.assertIn("merge origin/main into the merge branch, resolve the conflict, and run the push again",
                      result.stdout)
        merge_head = self.checkout / self.git("rev-parse", "--git-path", "MERGE_HEAD")
        self.assertFalse(merge_head.exists())
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), remote_before)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.promote), main_before)
        self.assertEqual(self.git("rev-parse", "main", cwd=self.origin), main_before)
        self.assertFalse(self.log.exists(), "a held push must not record completion")
        self.assertEqual(self.events("style_ledger.py"), [])
        self.assertEqual(self.events("send.py"), [])

    def test_fresh_child_push_conflict_reports_held_without_completion(self) -> None:
        self.assert_child_push_conflict_is_held(already_pushed=False)

    def test_pushed_child_awaiting_promotion_reports_main_conflict_as_held(self) -> None:
        self.assert_child_push_conflict_is_held(already_pushed=True)

    def test_locally_merged_handoff_is_pushed_before_acknowledgment_without_merging_again(self) -> None:
        self.promotions = str(self.promote)
        review = Path(self.env["HOME"]) / "rust/hanadocs/change-review/change-review - example.md"
        self.production_doc(change_review=review)
        _ = self.child_production()
        checkpoint = self.dated_checkpoint("checkpoint(alpha): recovered handoff\n\nChange-Review: Recovered | Read | Listed")
        _ = self.git("merge", "--no-ff", "-m",
                     f"Merge example-style hand-off ({checkpoint[:7]}) into production", checkpoint)
        merge = self.git("rev-parse", "HEAD")
        count = self.git("rev-list", "--count", "HEAD")
        self.assertNotEqual(self.git("rev-parse", "production", cwd=self.origin), merge)
        result = self.run_handoff(checkpoint)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD"), merge)
        self.assertEqual(self.git("rev-list", "--count", "HEAD"), count)
        self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), merge)
        self.assertEqual((self.state / "notified-tip").read_text(), merge)
        self.assertEqual(len(self.events("send.py")), 1)
        self.assertNotIn("already merged", result.stdout)
        self.assertEqual([row["Change"] for row in change_review.pending(review)], ["Recovered"])
        written = review.read_bytes()
        retried = self.run_handoff(checkpoint)
        self.assertEqual(retried.returncode, 0, retried.stdout + retried.stderr)
        self.assertEqual(review.read_bytes(), written)
    def test_handoff_runs_package_tests_and_writes_the_checkpoint_test_log(self) -> None:
        _ = self.child_production()
        self.write(self.unit, "crates/widget/Cargo.toml", '[package]\nname="widget"\nversion="0.1.0"\n')
        _ = self.git("add", "crates/widget/Cargo.toml", cwd=self.unit)
        checkpoint = self.checkpoint("crates/widget/src/lib.rs", "fn checked() {}\n")
        result = self.run_handoff(checkpoint)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.events("verify.sh"), [["test", "widget"]])
        log = self.state / f"merge_{checkpoint[:7]}_test.log"
        self.assertIn("package_widget_EXIT=0", log.read_text())
        self.assertIn("merge_test_1_EXIT=0", log.read_text())
    def test_red_handoff_package_test_restores_the_pre_merge_tip(self) -> None:
        _ = self.child_production()
        self.write(self.unit, "crates/widget/Cargo.toml", '[package]\nname="widget"\nversion="0.1.0"\n')
        _ = self.git("add", "crates/widget/Cargo.toml", cwd=self.unit)
        checkpoint = self.checkpoint("crates/widget/src/lib.rs", "fn checked() {}\n")
        before = self.git("rev-parse", "HEAD")
        (self.state / "fail-always-test-widget").touch()
        result = self.run_handoff(checkpoint)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.events("verify.sh"), [["test", "widget"], ["test", "widget"]])
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), before)
        log = (self.state / f"merge_{checkpoint[:7]}_test.log").read_text()
        self.assertIn("package_widget_EXIT=1", log)
        self.assertIn("package_widget_RERUN_EXIT=1", log)
        self.assertEqual(self.events("send.py"), [])
        self.assertEqual(self.events("style_sync.py"), [])
    def test_root_self_refuses_a_child_that_still_reports_to_a_parent(self) -> None:
        _ = self.child_production()
        checkpoint = self.checkpoint()
        before = self.git("rev-parse", "HEAD")
        result = self.run_handoff(checkpoint, "--root-self")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout.strip(),
                         "merge_checkpoint: example-style reports to example; --root-self needs a root")
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertEqual(self.events("send.py"), [])
    def test_untracked_parent_file_does_not_refuse_a_handoff(self) -> None:
        _ = self.child_production()
        checkpoint = self.checkpoint()
        self.write(self.checkout, "untracked.txt", "unrelated local note\n")
        result = self.run_handoff(checkpoint)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.checkout / "untracked.txt").read_text(), "unrelated local note\n")
        self.assertEqual(len(self.events("send.py")), 1)
    def test_staged_parent_file_refuses_a_handoff_before_merge_or_push(self) -> None:
        _ = self.child_production()
        checkpoint = self.checkpoint()
        self.write(self.checkout, "staged.txt", "unrelated staged change\n")
        _ = self.git("add", "staged.txt")
        before = self.git("rev-parse", "HEAD")
        result = self.run_handoff(checkpoint)
        self.assertEqual(result.returncode, 1)
        self.assertIn("staged paths in merge checkout: staged.txt", result.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), before)
        self.assertEqual(self.events("send.py"), [])
    def test_handoff_promotes_after_push_before_acknowledgment(self) -> None:
        self.promotions = str(self.promote)
        self.production_doc()
        _ = self.child_production()
        checkpoint = self.checkpoint()
        sender = (self.bin / "send.py").read_text()
        # Observe promotion at delivery time, before recording the acknowledgment.
        self.executable(self.bin / "send.py", sender.replace(
            'args = sys.argv[1:]',
            'args = sys.argv[1:]\nimport subprocess\n'
            + f'promoted = subprocess.check_output(["git", "-C", {str(self.promote)!r}, "rev-parse", "HEAD"], text=True).strip()\n'
            + f'remote = subprocess.check_output(["git", "-C", {str(self.origin)!r}, "rev-parse", "production"], text=True).strip()\n'
            + 'assert promoted == remote\n'
            + f'assert subprocess.call(["git", "-C", {str(self.promote)!r}, "merge-base", "--is-ancestor", {checkpoint!r}, promoted]) == 0',
        ))
        result = self.run_handoff(checkpoint)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        tip = self.git("rev-parse", "HEAD")
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.promote), tip)
        self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), tip)
        self.assertEqual((self.state / "notified-tip").read_text(), tip)
        self.assertLess(result.stdout.index("push: ok"), result.stdout.index("promote: ok"))
        self.assertEqual(len(self.events("send.py")), 1)
        self.assertNotIn("hand-off acknowledgment:", result.stdout)
    def test_handoff_refuses_unit_only_hold_metadata(self) -> None:
        _ = self.child_production()
        before = self.git("rev-parse", "HEAD")
        result = self.run_handoff(self.checkpoint(), "--holds", "1")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout.strip(), "merge_checkpoint: --from-child is separate from unit checkpoint arguments")
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertEqual(self.events("send.py"), [])


if __name__ == "__main__":
    _ = unittest.main()
