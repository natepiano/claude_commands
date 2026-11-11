"""Land ordered checkpoint batches against private Git repositories."""

from __future__ import annotations

from pathlib import Path
import shlex
import shutil
import subprocess
import unittest
from typing import final

from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.production.merge_checkpoint_test_support import (
    DELIVERS, MergeCheckpointFixture, PARALLEL, STARTED, STUB, TRIAL, configure_test_environment,
)


def setUpModule() -> None:
    configure_test_environment()


@final
class MergeCheckpointBatchTests(MergeCheckpointFixture):
    def batch_line(self, unit: str, phase: str, checkpoint: str, *flags: str) -> str:
        code_flags = [] if "--shrink" in flags or "--as-built" in flags else ["--parallel", PARALLEL, "--delivers", DELIVERS]
        return shlex.join([unit, phase, checkpoint, "--started", STARTED,
                           "--review-trial", TRIAL, *code_flags, "--scratch", str(self.state), *flags])

    def beta_checkpoint(self, relative: str, content: str, *, visible: bool = False) -> str:
        self.write(self.other, relative, content)
        _ = self.git("add", relative, cwd=self.other)
        review = "Beta changes | Open beta | The beta change appears" if visible else "none — fixture code only"
        _ = self.git("commit", "-m", "checkpoint(beta): batch change\n\nChange-Review: " + review, cwd=self.other)
        checkpoint = self.git("rev-parse", "HEAD", cwd=self.other)
        _ = self.git("push", "origin", "beta-branch", cwd=self.other)
        return checkpoint

    def land_batch(self, lines: list[str]) -> subprocess.CompletedProcess[str]:
        batch = self.root / "batch.txt"
        _ = batch.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.executable(self.origin / "hooks/post-receive", '''#!/usr/bin/env python3
from pathlib import Path
import sys
with Path(''' + repr(str(self.state / "pushed-refs")) + ''').open("a") as output:
    output.write(sys.stdin.read())
''')
        return self.invoke(["--production", "example", "--batch", str(batch)])

    def assert_landed(self, start: str, checkpoints: list[str]) -> list[str]:
        tip = self.git("rev-parse", "HEAD")
        self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), tip)
        self.assertEqual(self.git("rev-parse", "main", cwd=self.origin), tip)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.promote), tip)
        merges = self.git("rev-list", "--reverse", "--first-parent", "--merges", start + ".." + tip).splitlines()
        self.assertEqual([self.git("rev-parse", merge + "^2") for merge in merges], checkpoints)
        self.assertEqual([line.split()[2] for line in (self.state / "pushed-refs").read_text().splitlines()],
                         ["refs/heads/production", "refs/heads/main"])
        receipts = self.receipts()
        self.assertEqual([(row["merge_commit"], row["source_checkpoint"]) for row in receipts],
                         list(zip(merges, checkpoints)))
        self.assertEqual(len(self.events("style_sync.py")), 1)
        return merges

    def test_two_code_items_share_tests_push_and_promotion(self) -> None:
        for wrapper_fails in (False, True):
            with self.subTest(wrapper_fails=wrapper_fails):
                shutil.rmtree(self.root)
                _ = shutil.copytree(self.fixture_snapshot, self.root)
                self.bind_fixture(self.root)
                self.promotions = str(self.promote)
                review = Path(self.env["HOME"]) / "rust/hanadocs/change-review/change-review - example.md"
                self.production_doc(change_review=review)
                start = self.git("rev-parse", "HEAD")
                first = self.checkpoint(subject="checkpoint(alpha): batch change\n\n"
                                        + "Change-Review: Alpha changes | Open alpha | The alpha change appears")
                second = self.beta_checkpoint("src/beta.txt", "beta batch change\n", visible=True)
                if wrapper_fails:
                    self.push = "validate_and_push"
                    self.production_doc(change_review=review)
                    self.executable(self.bin / "validate_and_push.sh", STUB + """
import subprocess
origin = subprocess.run(["git", "remote", "get-url", "origin"], capture_output=True,
                        text=True, check=True, timeout=TEST_HANG_GUARD_SECS).stdout.strip()
subprocess.run(["git", "push", origin, "HEAD:refs/heads/production"],
               check=True, timeout=TEST_HANG_GUARD_SECS)
raise SystemExit(1)
""")
                result = self.land_batch([self.batch_line("alpha", "2", first), self.batch_line("beta", "2", second)])
                if wrapper_fails:
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    tip = self.git("rev-parse", "production", cwd=self.origin)
                    self.assertNotEqual(tip, start)
                    self.assertEqual(self.git("rev-parse", "HEAD"), tip)
                    self.assertEqual(self.git("rev-parse", "main", cwd=self.origin), start)
                    self.assertEqual([self.git("rev-parse", "HEAD~1^2"), self.git("rev-parse", "HEAD^2")],
                                     [first, second])
                    self.assertEqual(self.events("checkpoint-test"), [[]])
                    self.assertEqual(len(self.events("validate_and_push.sh")), 1)
                    self.assertEqual([line.split()[2] for line in (self.state / "pushed-refs").read_text().splitlines()],
                                     ["refs/heads/production"])
                    self.assertNotIn("batch: retry", result.stdout)
                    for unit in ("alpha", "beta"):
                        self.assertIn(f"send {unit}: repair the push failure", result.stdout)
                    self.assertIn("push: failed", result.stdout)
                else:
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    merges = self.assert_landed(start, [first, second])
                    self.assertEqual(self.events("checkpoint-test"), [[]])
                    for unit, merge in zip(("alpha", "beta"), merges):
                        self.assertEqual(result.stdout.count(f"send {unit}: phase 2 merged and pushed as {merge};"), 1)
                    self.assertEqual([call[call.index("--unit") + 1] for call in self.events("review_regime.py")],
                                     ["alpha", "beta"])
                    self.assertIn("Alpha changes", review.read_text())
                    self.assertIn("Beta changes", review.read_text())
                    self.assertIn("alpha phase 2", self.log.read_text())
                    self.assertIn("beta phase 2", self.log.read_text())

    def test_conflicting_second_item_drops_before_the_remaining_items_retry(self) -> None:
        for preexisting_merge in (False, True):
            with self.subTest(preexisting_merge=preexisting_merge):
                shutil.rmtree(self.root)
                _ = shutil.copytree(self.fixture_snapshot, self.root)
                self.bind_fixture(self.root)
                self.promotions = str(self.promote)
                self.production_doc()
                start = self.git("rev-parse", "HEAD")
                first = self.checkpoint(content="alpha batch side\n")
                second = self.beta_checkpoint("src/alpha.txt", "beta conflicting side\n")
                third = self.checkpoint("src/third.txt", "alpha third change\n")
                if preexisting_merge:
                    _ = self.git("merge", "--no-ff", "--no-commit", first)
                    self.write(self.checkout, "src/alpha.txt", "saved merge resolution\n")
                    _ = self.git("add", "src/alpha.txt")
                    saved = (self.git("rev-parse", "MERGE_HEAD"), self.git("write-tree"),
                             self.git("diff", "--cached", "--binary"))
                    result = self.land_batch([self.batch_line("alpha", "2", first), self.batch_line("beta", "2", second),
                                              self.batch_line("alpha", "3", third)])
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertIn("scope: held", result.stdout)
                    self.assertEqual(self.git("rev-parse", "HEAD"), start)
                    self.assertEqual((self.git("rev-parse", "MERGE_HEAD"), self.git("write-tree"),
                                      self.git("diff", "--cached", "--binary")), saved)
                    self.assertEqual((self.checkout / "src/alpha.txt").read_text(), "saved merge resolution\n")
                    for branch in ("production", "main"):
                        self.assertEqual(self.git("rev-parse", branch, cwd=self.origin), start)
                    self.assertEqual(self.events("checkpoint-test"), [])
                    self.assertEqual(self.receipts(), [])
                else:
                    result = self.land_batch([self.batch_line("alpha", "2", first), self.batch_line("beta", "2", second),
                                              self.batch_line("alpha", "3", third)])
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    _ = self.assert_landed(start, [first, third])
                    self.assertEqual(result.stdout.count("batch: retry"), 1)
                    self.assertIn("conflicts: held", result.stdout)
                    self.assertIn("send beta:", result.stdout)
                    self.assertNotIn("send beta: phase 2 merged", result.stdout)
                    self.assertIn("send alpha: phase 2 merged", result.stdout)
                    self.assertIn("send alpha: phase 3 merged", result.stdout)
                    self.assertEqual(self.events("checkpoint-test"), [[]])
                    self.assertEqual(self.git("show", "HEAD:src/alpha.txt"), "alpha batch side")
                    absent = subprocess.run(["git", "merge-base", "--is-ancestor", second, "HEAD"],
                                            cwd=self.checkout, env=self.env, capture_output=True, text=True, check=False,
                                            timeout=TEST_HANG_GUARD_SECS)
                    self.assertEqual(absent.returncode, 1, absent.stdout + absent.stderr)

    def test_red_package_drops_only_the_item_that_selects_its_command(self) -> None:
        for interrupted, shrink in ((False, False), (True, False), (True, True)):
            with self.subTest(interrupted=interrupted, shrink=shrink):
                shutil.rmtree(self.root)
                _ = shutil.copytree(self.fixture_snapshot, self.root)
                self.bind_fixture(self.root)
                if interrupted:
                    self.promotions = str(self.promote)
                    self.production_doc()
                    start = self.git("rev-parse", "HEAD")
                    first = self.checkpoint("crates/alpha-proof/Cargo.toml",
                                            '[package]\nname = "alpha-proof"\nversion = "0.1.0"\n')
                    if shrink:
                        second = self.checkpoint("docs/plans/alpha.md", "shrink plan\n", subject="shrink")
                        second_line = self.batch_line("alpha", "2", second, "--shrink")
                    else:
                        second = self.beta_checkpoint("src/beta.txt", "beta batch change\n")
                        second_line = self.batch_line("beta", "2", second)
                    self.executable(self.bin / "checkpoint-test", STUB + "raise KeyboardInterrupt\n")
                    with self.assertRaises(KeyboardInterrupt):
                        _ = self.land_batch([self.batch_line("alpha", "2", first)])
                    unpublished = self.git("rev-parse", "HEAD")
                    self.assertNotEqual(unpublished, start)
                    self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), start)
                    self.assertEqual(self.git("rev-parse", "HEAD^2"), first)
                    self.executable(self.bin / "checkpoint-test", STUB)
                    (self.state / "fail-always-test-alpha-proof").touch()
                    result = self.land_batch([self.batch_line("alpha", "2", first), second_line])
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertIn("resume it alone with the single command", result.stdout)
                    if shrink:
                        self.assertIn("send alpha: repair package alpha-proof", result.stdout)
                    else:
                        self.assertIn("send beta: repair package alpha-proof", result.stdout)
                    self.assertIn("red: failed", result.stdout)
                    self.assertEqual(result.stdout.count("batch: retry"), 1)
                    self.assertEqual(self.git("rev-parse", "HEAD"), unpublished)
                    for branch in ("production", "main"):
                        self.assertEqual(self.git("rev-parse", branch, cwd=self.origin), start)
                    self.assertEqual(self.events("verify.sh"), [["test", "alpha-proof"]] * 3)
                    self.assertEqual(self.events("checkpoint-test"), [[], []])
                    self.assertEqual(self.receipts(), [])
                else:
                    self.promotions = str(self.promote)
                    self.production_doc()
                    start = self.git("rev-parse", "HEAD")
                    first = self.checkpoint(content="alpha batch side\n")
                    second = self.beta_checkpoint("crates/beta-proof/Cargo.toml",
                                                  '[package]\nname = "beta-proof"\nversion = "0.1.0"\n')
                    third = self.checkpoint("src/third.txt", "alpha third change\n")
                    (self.state / "fail-always-test-beta-proof").touch()
                    result = self.land_batch([self.batch_line("alpha", "2", first), self.batch_line("beta", "2", second),
                                              self.batch_line("alpha", "3", third)])
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    _ = self.assert_landed(start, [first, third])
                    self.assertEqual(result.stdout.count("batch: retry"), 1)
                    self.assertIn("red: failed", result.stdout)
                    self.assertIn("send beta: repair package beta-proof", result.stdout)
                    self.assertNotIn("send beta: phase 2 merged", result.stdout)
                    self.assertIn("send alpha: phase 2 merged", result.stdout)
                    self.assertIn("send alpha: phase 3 merged", result.stdout)
                    self.assertEqual(self.events("verify.sh"), [["test", "beta-proof"], ["test", "beta-proof"]])
                    self.assertEqual(self.events("checkpoint-test"), [[], []])
                    self.assertEqual(self.git("ls-tree", "HEAD", "--", "crates/beta-proof/Cargo.toml"), "")


if __name__ == "__main__":
    _ = unittest.main()
