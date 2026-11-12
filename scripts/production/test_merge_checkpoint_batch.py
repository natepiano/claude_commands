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
    DELIVERS, MergeCheckpointFixture, STARTED, STUB, TRIAL, configure_test_environment,
)


def setUpModule() -> None:
    configure_test_environment()


@final
class MergeCheckpointBatchTests(MergeCheckpointFixture):
    def batch_line(self, unit: str, phase: str, checkpoint: str, *flags: str) -> str:
        code_flags = [] if "--shrink" in flags or "--as-built" in flags else ["--delivers", DELIVERS]
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
        for preexisting_merge, held_case in ((False, ""), (True, ""), (False, "two holds"), (False, "later item")):
            with self.subTest(preexisting_merge=preexisting_merge, held_case=held_case):
                shutil.rmtree(self.root)
                _ = shutil.copytree(self.fixture_snapshot, self.root)
                self.bind_fixture(self.root)
                self.promotions = str(self.promote)
                self.production_doc()
                start = self.git("rev-parse", "HEAD")
                if held_case:
                    if held_case == "two holds":
                        first = self.checkpoint(content="alpha clean item\n")
                        second = self.beta_checkpoint("src/beta.txt", "beta invalid shrink\n")
                        third = self.checkpoint("docs/wrong.md", "alpha invalid shrink\n")
                        lines = [self.batch_line("alpha", "2", first),
                                 self.batch_line("beta", "2", second, "--shrink"),
                                 self.batch_line("alpha", "3", third, "--shrink")]
                        landed, retries = [first], 2
                    else:
                        first = self.checkpoint("docs/wrong.md", "alpha invalid shrink\n")
                        second = self.checkpoint("src/later.txt", "alpha later item\n")
                        third = self.beta_checkpoint("src/beta.txt", "beta clean item\n")
                        lines = [self.batch_line("alpha", "2", first, "--shrink"),
                                 self.batch_line("alpha", "3", second), self.batch_line("beta", "2", third)]
                        landed, retries = [third], 1
                    result = self.land_batch(lines)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    _ = self.assert_landed(start, landed)
                    self.assertEqual(result.stdout.count("batch: retry"), retries)
                    self.assertIn("scope: held", result.stdout)
                    self.assertEqual(self.events("checkpoint-test"), [[]])
                    self.assertNotIn("batch retry stopped", result.stdout)
                    if held_case == "two holds":
                        self.assertIn("send alpha: phase 2 merged", result.stdout)
                        self.assertNotIn("send beta: phase 2 merged", result.stdout)
                        self.assertNotIn("send alpha: phase 3 merged", result.stdout)
                        self.assertEqual(result.stdout.count("scope: held"), 2)
                    else:
                        self.assertIn(f"later item follows alpha phase 2 ({first[:12]})", result.stdout)
                        self.assertIn(f"send alpha: resolve alpha phase 2 ({first[:12]}), then resend this checkpoint",
                                      result.stdout)
                        self.assertIn("send beta: phase 2 merged", result.stdout)
                        self.assertNotIn("send alpha: phase 3 merged", result.stdout)
                        self.assertEqual(result.stdout.count("scope: held"), 2)
                    continue
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
        for interrupted, shrink, shared_red, probe_case in ((False, False, False, ""), (True, False, False, ""),
                                                           (True, True, False, ""), (False, False, True, ""),
                                                           (False, False, False, "main"), (False, False, False, "rename")):
            with self.subTest(interrupted=interrupted, shrink=shrink, shared_red=shared_red, probe_case=probe_case):
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
                        self.assertIn(f"send alpha: resolve alpha phase 2 ({first[:12]}), then resend this checkpoint",
                                      result.stdout)
                        self.assertNotIn("red: failed", result.stdout)
                        self.assertEqual(result.stdout.count("batch: retry"), 0)
                    else:
                        self.assertIn("send beta: repair package alpha-proof", result.stdout)
                        self.assertIn("red: failed", result.stdout)
                        self.assertEqual(result.stdout.count("batch: retry"), 1)
                    self.assertEqual(self.git("rev-parse", "HEAD"), unpublished)
                    for branch in ("production", "main"):
                        self.assertEqual(self.git("rev-parse", branch, cwd=self.origin), start)
                    self.assertEqual(self.events("verify.sh"), [["test", "alpha-proof"]] * (1 if shrink else 3))
                    self.assertEqual(self.events("checkpoint-test"), [[]] * (1 if shrink else 2))
                    self.assertEqual(self.receipts(), [])
                elif probe_case == "main":
                    self.promotions = str(self.promote)
                    self.production_doc()
                    start = self.git("rev-parse", "HEAD")
                    first = self.beta_checkpoint("src/main-sensitive.txt", "fails with new main\n")
                    second = self.checkpoint(content="alpha batch side\n")
                    third = self.checkpoint("src/third.txt", "alpha third change\n")
                    writer = self.root / "main-writer"
                    _ = self.git("clone", "-b", "main", str(self.origin), str(writer))
                    _ = self.git("config", "user.name", "Checkpoint Test", cwd=writer)
                    _ = self.git("config", "user.email", "checkpoint@example.invalid", cwd=writer)
                    self.write(writer, "main-ready.txt", "main dependency\n")
                    _ = self.git("add", "main-ready.txt", cwd=writer)
                    _ = self.git("commit", "-m", "new main dependency", cwd=writer)
                    main_commit = self.git("rev-parse", "HEAD", cwd=writer)
                    self.executable(self.checkout / ".git/hooks/post-merge", f'''#!/usr/bin/env python3
import subprocess
parent = subprocess.run(["git", "rev-parse", "HEAD^2"], capture_output=True, text=True,
                        check=True, timeout={TEST_HANG_GUARD_SECS}).stdout.strip()
if parent == {third!r}:
    subprocess.run(["git", "-C", {str(writer)!r}, "push", "origin", "main"],
                   check=True, timeout={TEST_HANG_GUARD_SECS})
''')
                    self.executable(self.bin / "checkpoint-test", STUB + '''
import subprocess
tip = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                     check=True, timeout=TEST_HANG_GUARD_SECS).stdout.strip()
with (state / "main-probe-tips").open("a") as output:
    output.write(tip + "\\n")
red = Path("main-ready.txt").is_file() and Path("src/main-sensitive.txt").is_file()
print("main probe " + tip)
raise SystemExit(1 if red else 0)
''')
                    result = self.land_batch([self.batch_line("beta", "2", first), self.batch_line("alpha", "2", second),
                                              self.batch_line("alpha", "3", third)])
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    tip = self.git("rev-parse", "HEAD")
                    for branch in ("production", "main"):
                        self.assertEqual(self.git("rev-parse", branch, cwd=self.origin), tip)
                    merges = self.git("rev-list", "--reverse", "--first-parent", "--merges", start + ".." + tip).splitlines()
                    self.assertEqual([self.git("rev-parse", merge + "^2") for merge in merges], [second, third, main_commit])
                    repairs = [line.split(":", 1)[0] for line in result.stdout.splitlines()
                               if line.startswith("send ") and ": repair " in line]
                    self.assertEqual(repairs, ["send beta"])
                    self.assertIn("send alpha: phase 2 merged", result.stdout)
                    self.assertIn("send alpha: phase 3 merged", result.stdout)
                    self.assertEqual(result.stdout.count("batch: retry"), 1)
                    self.assertEqual(self.git("ls-tree", "HEAD", "--", "src/main-sensitive.txt"), "")
                    tested = (self.state / "main-probe-tips").read_text().splitlines()
                    self.assertEqual(len(tested), 7)
                    for tested_tip in set(tested):
                        self.assertEqual(self.git("merge-base", "--is-ancestor", main_commit, tested_tip), "")
                elif probe_case == "rename":
                    self.promotions = str(self.promote)
                    self.production_doc()
                    self.write(self.checkout, "crates/shared/Cargo.toml",
                               '[package]\nname = "old-proof"\nversion = "0.1.0"\n')
                    self.write(self.checkout, "crates/shared/src/lib.rs", "pub fn initial() {}\n")
                    _ = self.git("add", "crates/shared")
                    _ = self.git("commit", "-m", "initial package")
                    _ = self.git("push", "origin", "HEAD:production", "HEAD:main")
                    start = self.git("rev-parse", "HEAD")
                    for worktree in (self.unit, self.other):
                        _ = self.git("merge", "--ff-only", "production", cwd=worktree)
                    first = self.checkpoint("crates/shared/src/lib.rs", "pub fn first() {}\n")
                    second = self.beta_checkpoint("crates/shared/Cargo.toml",
                                                  '[package]\nname = "new-proof"\nversion = "0.1.0"\n')
                    third = self.checkpoint("src/third.txt", "alpha third change\n")
                    (self.state / "fail-always-test-new-proof").touch()
                    result = self.land_batch([self.batch_line("alpha", "2", first), self.batch_line("beta", "2", second),
                                              self.batch_line("alpha", "3", third)])
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    _ = self.assert_landed(start, [first, third])
                    repairs = [line.split(":", 1)[0] for line in result.stdout.splitlines()
                               if line.startswith("send ") and ": repair " in line]
                    self.assertEqual(repairs, ["send beta"])
                    self.assertEqual(self.events("verify.sh"), [["test", "new-proof"], ["test", "new-proof"],
                                                                 ["test", "old-proof"]])
                    self.assertEqual(self.events("checkpoint-test"), [[], []])
                    self.assertEqual(result.stdout.count("bisect: green"), 1)
                    self.assertEqual(result.stdout.count("batch: retry"), 1)
                    self.assertEqual(self.git("show", "HEAD:crates/shared/Cargo.toml"),
                                     '[package]\nname = "old-proof"\nversion = "0.1.0"')
                elif shared_red:
                    self.promotions = str(self.promote)
                    self.production_doc()
                    start = self.git("rev-parse", "HEAD")
                    first = self.checkpoint(content="alpha batch side\n")
                    second = self.beta_checkpoint("src/broken.txt", "broken shared test import\n")
                    third = self.checkpoint("src/third.txt", "alpha third change\n")
                    self.executable(self.bin / "checkpoint-test", STUB + '''
import subprocess
tip = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                     text=True, check=True, timeout=TEST_HANG_GUARD_SECS).stdout.strip()
with (state / "checkpoint-tips").open("a") as output:
    output.write(tip + "\\n")
red = Path("src/broken.txt").is_file()
print("checkpoint proof " + tip + (": red" if red else ": green"))
raise SystemExit(1 if red else 0)
''')
                    result = self.land_batch([self.batch_line("alpha", "2", first), self.batch_line("beta", "2", second),
                                              self.batch_line("alpha", "3", third)])
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    _ = self.assert_landed(start, [first, third])
                    self.assertEqual(result.stdout.count("batch: retry"), 1)
                    repairs = [line.split(":", 1)[0] for line in result.stdout.splitlines()
                               if line.startswith("send ") and ": repair " in line]
                    self.assertEqual(repairs, ["send beta"])
                    self.assertNotIn("send beta: phase 2 merged", result.stdout)
                    self.assertIn("send alpha: phase 2 merged", result.stdout)
                    self.assertIn("send alpha: phase 3 merged", result.stdout)
                    tips = (self.state / "checkpoint-tips").read_text().splitlines()
                    self.assertEqual(self.events("checkpoint-test"), [[]] * 6)
                    self.assertEqual([self.git("rev-parse", tip + "^2") for tip in tips[2:-1]], [second, second, first])
                    probes = [line for line in result.stdout.splitlines() if line.startswith("bisect:")]
                    self.assertEqual([line.split(" — ", 1)[0] for line in probes], ["bisect: red", "bisect: green"])
                    self.assertIn(f"beta phase 2 at {tips[2]}", probes[0])
                    self.assertIn(f"alpha phase 2 at {tips[4]}", probes[1])
                    log = (self.state / f"merge_{first[:7]}_test.log").read_text()
                    for tip in set(tips[2:-1]):
                        self.assertIn("checkpoint proof " + tip, log)
                    self.assertEqual(self.events("verify.sh"), [])
                    self.assertEqual(self.git("ls-tree", "HEAD", "--", "src/broken.txt"), "")
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
