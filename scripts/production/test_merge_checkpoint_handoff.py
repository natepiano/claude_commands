"""Exercise checkpoint merges against disposable repositories and command stubs."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from typing import cast, final

sys.path.insert(0, str(Path(__file__).resolve().parent))
from merge_checkpoint import ChildHandoffEntry, CodeCheckpoint, MergeEntry, NoMerge, ShrinkCommit, merge_branch_history

# Importable once merge_checkpoint has put scripts/lib on the path.


from scripts.production.merge_checkpoint_test_support import MergeCheckpointFixture, SCRIPT, STARTED, configure_test_environment


def setUpModule() -> None:
    configure_test_environment()


@final
class MergeCheckpointTests(MergeCheckpointFixture):
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
    def test_child_checkpoint_must_descend_from_last_merged_handoff(self) -> None:
        _ = self.child_production()
        first = self.checkpoint()
        self.assertEqual(self.run_handoff(first).returncode, 0)
        self.write(self.other, "src/beta.txt", "divergent history\n")
        _ = self.git("commit", "-am", "Divergent child", cwd=self.other)
        divergent = self.git("rev-parse", "HEAD", cwd=self.other)
        _ = self.git("reset", "--hard", divergent, cwd=self.unit)
        before, notices = self.git("rev-parse", "HEAD"), self.events("send.py")
        result = self.run_handoff(divergent)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not descend from example-style's last merged hand-off", result.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertEqual(self.events("send.py"), notices)
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
        self.production_doc()
        _ = self.child_production()
        checkpoint = self.checkpoint()
        self.assertEqual(self.run_handoff(checkpoint).returncode, 0)
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
    def test_locally_merged_handoff_is_pushed_before_acknowledgment_without_merging_again(self) -> None:
        _ = self.child_production()
        checkpoint = self.checkpoint()
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
    def test_former_child_slug_resolves_to_the_canonical_doc_and_merge_subject(self) -> None:
        doc = self.child_production()
        _ = doc.write_text(doc.read_text().replace(
            "# Style production", "# Style production\n\n## Production Context\n- **Formerly:** `old-style`",
        ))
        timer = self.root / "notifier/showrunner-example-style/conf"
        timer.parent.mkdir(parents=True)
        _ = timer.write_text(f"TARGET=session:fake-child\nCHECK=zsh /unused/production_check.sh {doc}\n")
        checkpoint = self.checkpoint()
        result = subprocess.run([
            sys.executable, str(SCRIPT), "--from-child", "old-style", "--checkpoint", checkpoint,
            "--production", str(self.doc), "--started", STARTED,
        ], cwd=self.checkout, env=self.env, capture_output=True, text=True, check=False, timeout=300)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.git("log", "-1", "--format=%s"),
                         f"Merge example-style hand-off ({checkpoint[:7]}) into production")
        entry = merge_branch_history(self.checkout, "production").last_merge()
        self.assertIsInstance(entry, ChildHandoffEntry)
        if not isinstance(entry, ChildHandoffEntry):
            self.fail("canonical child's hand-off was not recorded")
        self.assertEqual(entry.child, "example-style")
        self.assertEqual(len(self.events("send.py")), 1)
        self.assertNotIn("old-style", str(self.events("send.py")))
    def test_handoff_refuses_unit_only_hold_metadata(self) -> None:
        _ = self.child_production()
        before = self.git("rev-parse", "HEAD")
        result = self.run_handoff(self.checkpoint(), "--holds", "1")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout.strip(), "merge_checkpoint: --from-child is separate from unit checkpoint arguments")
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertEqual(self.events("send.py"), [])
    def test_style_unit_none_owns_needs_explicit_ticket_file_allowance(self) -> None:
        self.production_doc(owns_cell="none — takes what the board leaves free")
        checkpoint = self.checkpoint(relative="src/lib.rs", content="fn fixed() {}\n")
        refused = self.run_checkpoint(checkpoint)
        self.assertNotEqual(refused.process.returncode, 0)
        self.assertIn("outside Owns: src/lib.rs", refused.process.stdout)
        permitted = self.run_checkpoint(checkpoint, "--also", "src/lib.rs")
        self.assertEqual(permitted.process.returncode, 0, permitted.process.stdout + permitted.process.stderr)
        self.assertIn("scope: ok", permitted.process.stdout)
    def test_mixed_history_keeps_child_identity_separate_from_renamed_unit_phases(self) -> None:
        subjects = (
            "Merge old-unit phase 1 (aaaaaaa) into production",
            "Merge old-unit hand-off (bbbbbbb) into production",
            "Merge old-unit phase 1 shrink (ccccccc) into production",
            "chore(example): rename unit old-unit to new-unit",
            "Merge style-child hand-off (ddddddd) into production",
            "Merge new-unit phase 2 (eeeeeee) into production",
            "Merge old-unit hand-off (fffffff) into production",
        )
        hashes: list[str] = []
        for subject in subjects:
            _ = self.git("commit", "--allow-empty", "-m", subject)
            hashes.append(self.git("rev-parse", "HEAD"))
        history = merge_branch_history(self.checkout, "production")
        self.assertEqual(history.entries, (
            ChildHandoffEntry("old-unit", "fffffff", "production", hashes[6]),
            MergeEntry("new-unit", "2", "eeeeeee", "production", hashes[5], CodeCheckpoint()),
            ChildHandoffEntry("style-child", "ddddddd", "production", hashes[4]),
            MergeEntry("new-unit", "1", "ccccccc", "production", hashes[2], ShrinkCommit()),
            ChildHandoffEntry("old-unit", "bbbbbbb", "production", hashes[1]),
            MergeEntry("new-unit", "1", "aaaaaaa", "production", hashes[0], CodeCheckpoint()),
        ))
        self.assertEqual(history.last_merge(), history.entries[0])
        self.assertEqual(history.last_for_unit("new-unit"), history.entries[1])
        self.assertEqual(history.last_code_for_unit("new-unit"), history.entries[1])
        self.assertEqual(history.last_for_unit("old-unit"), NoMerge())
        self.assertEqual(history.last_code_for_unit("style-child"), NoMerge())
        self.assertEqual(history.code_merge_count(), 2)
        self.assertTrue(history.has_code_merge("new-unit", "1"))
        self.assertFalse(history.has_code_merge("old-unit", "style hand-off"))
        self.assertEqual(history.last_handoff_for_child("old-unit"), history.entries[0])
        self.assertEqual(history.last_handoff_for_child("new-unit"), NoMerge())


if __name__ == "__main__":
    _ = unittest.main()
