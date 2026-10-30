"""Exercise checkpoint merges against disposable repositories and command stubs."""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from unittest import mock
from pathlib import Path
from typing import cast, final

sys.path.insert(0, str(Path(__file__).resolve().parent))
import merge_checkpoint
import showrunners
from scripts.production import production_doc
from merge_checkpoint import check_parallel

# Importable once merge_checkpoint has put scripts/lib on the path.
import scratch

from scripts.production.merge_checkpoint_test_support import MergeCheckpointFixture, REAL_MERGE_TESTS, REAL_OWNS, STUB, configure_test_environment


def setUpModule() -> None:
    configure_test_environment()


@final
class MergeCheckpointTests(MergeCheckpointFixture):
    def test_same_checkpoint_rerun_skips_another_productions_reviewed_commits(self) -> None:
        import change_review

        self.write(self.other, "crates/alpha/imported.txt", "other production\n")
        _ = self.git("add", "crates/alpha/imported.txt", cwd=self.other)
        _ = self.git("commit", "-m", "checkpoint(other): already on another production", cwd=self.other)
        foreign = self.git("rev-parse", "HEAD", cwd=self.other)
        _ = self.git("push", "origin", "HEAD:refs/heads/other-production", cwd=self.other)
        _ = self.git("fetch", "origin", cwd=self.unit)
        _ = self.git("merge", "--no-ff", "-m", "merge other production", "origin/other-production", cwd=self.unit)
        sha = self.checkpoint(subject="checkpoint(alpha): own work\n\nChange-Review: none — internal check")
        other_doc = self.root / "other-production.md"
        _ = other_doc.write_text(f"- **Repository:** `{self.checkout}`\n"
                                 + "- **Merge branch:** `other-production`\n"
                                 + f"- **Change review:** `{self.root / 'other-review.md'}`\n")
        with mock.patch.object(showrunners, "timer_docs", return_value={}):
            held = self.run_checkpoint(sha)
        self.assert_stopped_before_merge(held, "held", "change review: held")
        self.assertIn(foreign[:change_review.SHORT], held.process.stdout)
        with mock.patch.object(showrunners, "timer_docs", return_value={"other": other_doc}):
            accepted = self.run_checkpoint(sha)
        self.assertEqual(accepted.process.returncode, 0, accepted.process.stdout + accepted.process.stderr)
        self.assertIn("change review: ok — 1 unit commit(s)", accepted.process.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD^2"), sha)

    def test_missing_hash_and_unpushed_checkpoint_stop_at_ancestry(self) -> None:
        missing = self.run_checkpoint("0" * 40)
        self.assert_stopped_before_merge(missing, "failed", "commit")
        self.assertIn("push", missing.process.stdout)
        sha = self.checkpoint(push=False)
        unpushed = self.run_checkpoint(sha)
        self.assert_stopped_before_merge(unpushed, "held", "origin")
        self.assertIn("push", unpushed.process.stdout)
    def test_never_pushed_unit_branch_is_held_like_an_unpushed_checkpoint(self) -> None:
        sha = self.checkpoint(push=False)
        unpushed = self.run_checkpoint(sha)
        _ = self.git("push", "origin", "--delete", "alpha-branch", cwd=self.unit)
        never_pushed = self.run_checkpoint(sha)
        self.assert_stopped_before_merge(never_pushed, "held", "ancestry: held — commit is not on origin")
        self.assertIn("send alpha-unit: push your branch to origin and resend the checkpoint",
                      never_pushed.process.stdout)
        self.assertEqual(never_pushed.process.stdout, unpushed.process.stdout)
    def test_checkpoint_on_second_origin_branch_merges_the_checkpoint_and_names_branch(self) -> None:
        _ = self.git("checkout", "-b", "alpha-phase", cwd=self.unit)
        sha = self.checkpoint(push=False)
        _ = self.git("push", "origin", "alpha-phase", cwd=self.unit)
        # The accepted checkpoint need not be the containing branch's tip.
        later = self.checkpoint("outside.txt", "later work\n", push=False)
        _ = self.git("push", "origin", "alpha-phase", cwd=self.unit)
        _ = self.git("config", "remote.origin.fetch",
                     "+refs/heads/alpha-branch:refs/remotes/origin/alpha-branch")
        result = self.run_checkpoint(sha)
        self.assertEqual(result.process.returncode, 0, result.process.stdout + result.process.stderr)
        self.assertIn("ancestry: ok — checkpoint exists, follows history, and is on origin/alpha-phase\n",
                      result.process.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD^2"), sha)
        self.assertNotEqual(self.git("rev-parse", "HEAD^2"), later)
        self.assertFalse((self.checkout / "outside.txt").exists())
        self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), self.git("rev-parse", "HEAD"))

    def test_checkpoint_on_no_origin_branch_checks_all_heads_and_still_holds(self) -> None:
        sha = self.checkpoint(push=False)
        # Simulate a deleted alternate branch with a stale local tracking ref.
        _ = self.git("push", "origin", f"{sha}:refs/heads/alpha-phase", cwd=self.unit)
        _ = self.git("update-ref", "refs/remotes/origin/alpha-phase", sha)
        _ = self.git("update-ref", "-d", "refs/heads/alpha-phase", cwd=self.origin)
        with mock.patch.object(merge_checkpoint, "git", wraps=merge_checkpoint.git) as commands:
            result = self.run_checkpoint(sha)
        self.assert_stopped_before_merge(result, "held", "ancestry: held — commit is not on origin")
        self.assertIn("send alpha-unit: push your branch to origin and resend the checkpoint", result.process.stdout)
        commands.assert_any_call(self.checkout, "fetch", "--prune", "origin",
                                 "+refs/heads/*:refs/remotes/origin/*")
        self.assertEqual(self.git("for-each-ref", "refs/remotes/origin/alpha-phase"), "")

    def test_second_origin_branch_is_accepted_when_units_row_branch_was_deleted(self) -> None:
        _ = self.git("checkout", "-b", "alpha-phase", cwd=self.unit)
        sha = self.checkpoint(push=False)
        _ = self.git("push", "origin", "alpha-phase", cwd=self.unit)
        _ = self.git("push", "origin", "--delete", "alpha-branch", cwd=self.unit)
        result = self.run_checkpoint(sha)
        self.assertEqual(result.process.returncode, 0, result.process.stdout + result.process.stderr)
        self.assertIn("is on origin/alpha-phase", result.process.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD^2"), sha)

    def test_units_row_origin_branch_uses_the_fast_path(self) -> None:
        sha = self.checkpoint()
        with mock.patch.object(merge_checkpoint, "git", wraps=merge_checkpoint.git) as commands:
            result = self.run_checkpoint(sha)
        self.assertEqual(result.process.returncode, 0, result.process.stdout + result.process.stderr)
        self.assertIn("is on origin/alpha-branch", result.process.stdout)
        self.assertFalse(any(call.args[1:3] == ("fetch", "--prune") for call in commands.call_args_list))

    def test_unreachable_origin_still_fails_at_ancestry(self) -> None:
        sha = self.checkpoint()
        _ = self.git("remote", "set-url", "origin", str(self.root / "absent.git"))
        result = self.run_checkpoint(sha)
        self.assert_stopped_before_merge(result, "failed", "ancestry: failed")
        self.assertIn("send alpha-unit: the showrunner will inspect the failed command", result.process.stdout)
    def test_scope_holds_unowned_path_and_allows_explicit_also(self) -> None:
        sha = self.checkpoint("outside.txt", "unowned\n")
        held = self.run_checkpoint(sha)
        self.assert_stopped_before_merge(held, "held", "outside.txt")
        accepted = self.run_checkpoint(sha, "--also", "outside.txt")
        self.assertEqual(accepted.process.returncode, 0, accepted.process.stderr + accepted.process.stdout)
        self.assertIn("scope: ok", accepted.process.stdout)
    def test_a_code_checkpoint_removes_its_phases_scratch_folders(self) -> None:
        with mock.patch.dict(os.environ, self.env):
            done = [scratch.new("phase:alpha-unit/2", word) for word in ("gate", "copy")]
            later = scratch.new("phase:alpha-unit/3")
        result = self.run_checkpoint(self.checkpoint())
        self.assertEqual(result.process.returncode, 0, result.process.stderr + result.process.stdout)
        self.assertIn("scratch: ok — removed 2 folder(s) of phase:alpha-unit/2: ", result.process.stdout)
        self.assertFalse(any(path.exists() for path in done))
        self.assertTrue(later.is_dir())
    def test_real_owns_cell_allows_produce_path_after_commentary(self) -> None:
        self.assert_real_owns_path_allowed("commands/showrunner/produce.md")
    def test_real_owns_cell_allows_explicit_add_unit_path_in_second_group(self) -> None:
        self.assert_real_owns_path_allowed("scripts/production/add_unit.py")
    def test_real_owns_cell_reads_a_bare_name_beside_the_path_before_it(self) -> None:
        self.assert_real_owns_path_allowed("scripts/production/merge_checkpoint.py")
    def test_real_owns_cell_reads_a_second_bare_name_beside_the_same_path(self) -> None:
        self.assert_real_owns_path_allowed("scripts/production/test_merge_checkpoint.py")
    def test_real_owns_cell_still_reads_a_bare_name_as_a_root_file(self) -> None:
        self.assert_real_owns_path_allowed("merge_checkpoint.py")
    def test_real_owns_cell_allows_a_path_in_the_last_group(self) -> None:
        self.assert_real_owns_path_allowed("commands/unit/direct.md")
    def test_real_owns_cell_holds_an_outside_path(self) -> None:
        self.production_doc(owns_cell=REAL_OWNS, hub_files=False)
        outside = self.checkpoint("scripts/other/outside.py", "outside\n")
        held = self.run_checkpoint(outside)
        self.assertEqual(held.process.returncode, 1)
        self.assertIn("scope: held", held.process.stdout)
        self.assertIn("scripts/other/outside.py", held.process.stdout)
    def test_real_merge_tests_run_only_the_two_commands_before_commentary(self) -> None:
        self.production_doc(merge_tests_line=REAL_MERGE_TESTS)
        for name in ("python3", "basedpyright"):
            self.executable(self.bin / name, STUB)
        sha = self.checkpoint()
        result = self.run_checkpoint(sha)
        self.assertEqual(result.process.returncode, 0, result.process.stderr + result.process.stdout)
        self.assertEqual(self.events("python3"), [
            ["-m", "unittest", "discover", "-s", "scripts/buildlog", "-p", "test_*.py"],
            [str(self.bin / "style_sync.py"), "notify", "--repo-dir", str(self.checkout),
             "--branch", "production", "--tip", self.git("rev-parse", "HEAD")],
        ])
        self.assertEqual(self.events("basedpyright"), [["scripts/buildlog"]])
        self.assertEqual(self.events("0"), [])
        all_args = [arg for event in (self.events("python3") + self.events("basedpyright")) for arg in event]
        self.assertFalse(any(".venv" in arg for arg in all_args))
        self.assertIn("test: ok", result.process.stdout)
    def test_merge_tests_that_are_not_commands_are_refused_by_name_before_the_merge(self) -> None:
        """cargo-liner wrote packages and a crate directory where the line wants commands."""
        (self.checkout / "crates/tui_pane").mkdir(parents=True)
        sha = self.checkpoint()
        for entry, detail in (("crates/tui_pane", "crates/tui_pane is neither on PATH"),
                              ("tui-pane-package", "tui-pane-package is neither on PATH"),
                              ("checkpoint-executable-that-does-not-exist", "does-not-exist is neither on PATH"),
                              ("$MERGE_TEST_UNSET/bin/tool --all", "names $MERGE_TEST_UNSET, which is not set")):
            with self.subTest(entry=entry):
                self.production_doc(merge_tests_line=f"- **Merge tests:** `checkpoint-test`, `{entry}`")
                result = self.run_checkpoint(sha)
                self.assertEqual(result.process.returncode, 1)
                self.assert_stopped_before_merge(result, "failed", f"Merge tests entry `{entry}`")
                self.assertIn(detail, result.process.stdout)
    def test_merge_tests_expand_home_and_variables(self) -> None:
        """The commands run with no shell, so ~ and $VAR are expanded before they run."""
        tools = Path(self.env["HOME"]) / "tools"
        tools.mkdir()
        self.executable(tools / "merge-tool", STUB)
        self.env["MERGE_TEST_DIR"] = "scripts/buildlog"
        self.production_doc(merge_tests_line="- **Merge tests:** `~/tools/merge-tool $MERGE_TEST_DIR`, "
                                             + "`$HOME/tools/merge-tool ${MERGE_TEST_DIR}/one ~/two`")
        sha = self.checkpoint()
        result = self.run_checkpoint(sha)
        self.assertEqual(result.process.returncode, 0, result.process.stderr + result.process.stdout)
        self.assertEqual(self.events("merge-tool"),
                         [["scripts/buildlog"], ["scripts/buildlog/one", str(tools.parent / "two")]])
    def test_merge_tests_field_is_required_but_dash_runs_no_merge_test(self) -> None:
        sha = self.checkpoint()
        self.production_doc(merge_tests_line=None)
        absent = self.run_checkpoint(sha)
        self.assertEqual(absent.process.returncode, 1)
        self.assertIn("input: failed", absent.process.stdout)
        self.assertIn("production doc lacks Merge tests", absent.process.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD"), absent.before)
        self.production_doc(merge_tests_line="- **Merge tests:** —")
        empty = self.run_checkpoint(sha)
        self.assertEqual(empty.process.returncode, 0, empty.process.stderr + empty.process.stdout)
        self.assertEqual(self.events("checkpoint-test"), [])
        self.assertIn("test: ok — 0 commands", empty.process.stdout)
    def test_later_phase_uses_last_unit_checkpoint_for_ancestry(self) -> None:
        first_sha = self.checkpoint()
        first = self.run_checkpoint(first_sha)
        self.assertEqual(first.process.returncode, 0, first.process.stderr + first.process.stdout)
        self.assertIn(f"({first_sha[:7]})", self.git("log", "-1", "--format=%s"))
        next_sha = self.checkpoint(content="phase three\n")
        later = self.run_checkpoint(next_sha, phase="3")
        self.assertEqual(later.process.returncode, 0, later.process.stderr + later.process.stdout)
        self.assertIn("ancestry: ok", later.process.stdout)
    def test_code_checkpoint_requires_review_trial_before_merge(self) -> None:
        sha = self.checkpoint()
        result = self.run_checkpoint(sha, review=False)
        self.assertIn("failed", result.process.stdout)
        self.assertIn("--review-trial", result.process.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD"), result.before)
        self.assertEqual(self.events("checkpoint-test"), [])
    def test_code_checkpoint_requires_a_known_parallel_line(self) -> None:
        sha = self.checkpoint()
        for parallel, expected in (
            (None, "requires --parallel"),
            ("Parallel: unknown — Phase 5 has no **Depends on:** line; add one under each phase heading and rerun",
             "adds a **Depends on:** line"),
            ("Parallel: maybe later", "--parallel must read"),
        ):
            result = self.run_checkpoint(sha, parallel=parallel)
            self.assertEqual(result.process.returncode, 1)
            self.assert_stopped_before_merge(result, "failed", expected)
        for parallel in ("Parallel: none — Phase 4 needs Phase 3 (running)", "Parallel: Phase 4, Phase 6 could start"):
            with self.subTest(parallel=parallel):
                check_parallel(parallel)
    def test_alternate_review_trial_is_refused_and_canonical_numbers_reach_record(self) -> None:
        sha = self.checkpoint()
        alternate = self.run_checkpoint(sha, trial="review trial: UX 2, code 3, review 14 min, UX check 4 min, UX repair 1 min")
        self.assertEqual(alternate.process.returncode, 1)
        self.assert_stopped_before_merge(alternate, "failed", "review trial: ux <N> findings")
        self.assertIn("input: failed", alternate.process.stdout)
        result = self.run_checkpoint(sha)
        self.assertEqual(result.process.returncode, 0, result.process.stderr + result.process.stdout)
        review = self.events("review_regime.py")
        self.assertEqual(len(review), 1)
        for flag, value in (("--ux-findings", "2"), ("--code-findings", "3"),
                            ("--review-minutes", "14"), ("--ux-check-minutes", "4"),
                            ("--ux-repair-minutes", "1")):
            self.assertEqual(review[0][review[0].index(flag) + 1], value)


@final
class TestPassedTests(unittest.TestCase):
    VENV = "venv .venv subdirectory not found in venv path /checkout.\n"

    def judge(self, status: int, stdout: str, stderr: str = "") -> bool:
        result = subprocess.CompletedProcess(["basedpyright"], status, stdout, stderr)
        return merge_checkpoint.test_passed(["/nix/bin/basedpyright", "scripts/buildlog"], result)

    def test_clean_basedpyright_passes_with_trailing_venv_line_on_stderr(self) -> None:
        self.assertTrue(self.judge(3, "0 errors, 0 warnings, 0 notes\n", self.VENV))

    def test_basedpyright_with_errors_fails(self) -> None:
        self.assertFalse(self.judge(1, "/checkout/a.py:1:1 - error: x\n2 errors, 0 warnings, 0 notes\n", self.VENV))
        self.assertFalse(self.judge(3, "2 errors, 0 warnings, 0 notes\n", self.VENV))

    def test_other_commands_pass_only_on_status_zero(self) -> None:
        result = subprocess.CompletedProcess(["python3"], 3, "0 errors, 0 warnings, 0 notes\n", "")
        self.assertFalse(merge_checkpoint.test_passed(["python3", "-m", "unittest"], result))
        self.assertTrue(merge_checkpoint.test_passed(["python3"], subprocess.CompletedProcess(["python3"], 0, "", "")))


@final
class MacPromotionPathTests(unittest.TestCase):
    def remote_command(self, promote_line: str) -> str:
        promotion = production_doc.promotion_from_doc([f"- **Promote:** {promote_line}"])
        assert isinstance(promotion, production_doc.PromoteTo)
        assert isinstance(promotion.mac, production_doc.MacCheckout)
        done = subprocess.CompletedProcess(["ssh"], 0, "rc=0\n", "")
        with mock.patch("merge_checkpoint.subprocess.run", return_value=done) as run:
            merge_checkpoint.pull_mac_checkout(promotion.mac.path, Path("/home/natepiano/.claude"))
        argv = cast(list[str], run.call_args.args[0])
        self.assertEqual(argv[:2], ["ssh", "mac"])
        return argv[2]

    def test_tilde_mac_path_is_expanded_by_the_mac_shell(self) -> None:
        remote = self.remote_command("mac `~/.claude`")
        self.assertTrue(remote.startswith('cd "$HOME"/.claude && git pull'), remote)
        self.assertNotIn(str(Path.home()) + "/.claude &&", remote)

    def test_absolute_mac_path_is_kept(self) -> None:
        remote = self.remote_command("/home/natepiano/.claude, mac /Users/someone/my repo")
        self.assertTrue(remote.startswith("cd '/Users/someone/my repo' && git pull"), remote)


if __name__ == "__main__":
    _ = unittest.main()
