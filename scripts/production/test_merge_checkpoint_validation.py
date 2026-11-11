"""Exercise checkpoint merges against disposable repositories and command stubs."""

from __future__ import annotations

from scripts.production import checkpoint_gate

from contextlib import redirect_stdout
from io import StringIO
import os
import sys
import tomllib
import unittest
from unittest import mock
from pathlib import Path
from typing import final

from scripts.production import merge_checkpoint
from scripts.production import staff
from scripts.production.production_doc import read_new_production

# Importable once merge_checkpoint has put scripts/lib on the path.


from scripts.production.merge_checkpoint_test_support import DELIVERS, MergeCheckpointFixture, PARALLEL, SCRIPT, STARTED, STUB, TRIAL, configure_test_environment


def setUpModule() -> None:
    configure_test_environment()


@final
class MergeCheckpointTests(MergeCheckpointFixture):
    def test_conflict_holds_with_no_merge_left_behind(self) -> None:
        self.write(self.checkout, "src/alpha.txt", "main side\n")
        _ = self.git("add", "src/alpha.txt")
        _ = self.git("commit", "-m", "other change")
        sha = self.checkpoint(content="unit side\n")
        result = self.run_checkpoint(sha)
        self.assert_stopped_before_merge(result, "held", "conflict")
        self.assertIn("new hash", result.process.stdout)
    def test_other_unit_overlap_is_reported_as_data(self) -> None:
        self.write(self.other, "shared.txt", "beta branch\n")
        _ = self.git("add", "shared.txt", cwd=self.other)
        _ = self.git("commit", "-m", "beta shared edit", cwd=self.other)
        _ = self.git("push", "origin", "beta-branch", cwd=self.other)
        self.write(self.other, "shared.txt", "beta uncommitted\n")
        sha = self.checkpoint("shared.txt", "alpha shared\n")
        result = self.run_checkpoint(sha)
        self.assertEqual(result.process.returncode, 0, result.process.stderr + result.process.stdout)
        self.assertIn("beta", result.process.stdout)
        self.assertIn("shared.txt", result.process.stdout)
    def test_changed_example_gets_its_own_verify_call(self) -> None:
        self.write(self.unit, "crates/alpha/Cargo.toml", '[package]\nname = "alpha"\nversion = "0.1.0"\n')
        self.write(self.unit, "crates/alpha/examples/demo.rs", "fn main() {}\n")
        _ = self.git("add", "crates/alpha", cwd=self.unit)
        _ = self.git("commit", "-m", "add example", cwd=self.unit)
        sha = self.git("rev-parse", "HEAD", cwd=self.unit)
        _ = self.git("push", "origin", "alpha-branch", cwd=self.unit)
        result = self.run_checkpoint(sha)
        self.assertEqual(result.process.returncode, 0, result.process.stderr + result.process.stdout)
        calls = self.events("verify.sh")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0], "test")
        self.assertEqual(calls[1][0], "example")
        self.assertIn("alpha", calls[1])
        self.assertIn("demo", calls[1])
    def test_default_validate_and_push_failure_undoes_unpushed_merge(self) -> None:
        self.push = ""
        self.production_doc()
        sha = self.checkpoint()
        _ = (self.state / "fail-validate_and_push.sh").touch()
        result = self.run_checkpoint(sha)
        self.assertIn("failed", result.process.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD"), result.before)
        self.assertEqual(self.git("rev-parse", "refs/heads/production", cwd=self.origin), result.before)
        self.assertEqual(self.events("validate_and_push.sh"), [[
            "--quick", "--to", "production", "--fix-commit",
            "ci(example): format fixes after alpha phase 2",
        ]])
        self.assertEqual(self.events("review_regime.py"), [])
    def test_cancel_prior_reaches_validate_and_push_only_when_requested(self) -> None:
        self.push = "validate_and_push"
        self.production_doc()
        self.executable(self.bin / "validate_and_push.sh", STUB + '''
with (state / "validation-locks").open("a") as output:
    output.write(os.environ.get("MAIN_PUSH_LOCK_HELD", "missing") + "\\n")
''')
        first = self.checkpoint()
        first_result = self.run_checkpoint(first)
        self.assertEqual(first_result.process.returncode, 0, first_result.process.stderr + first_result.process.stdout)
        second = self.checkpoint(content="second\n")
        second_result = self.run_checkpoint(second, "--cancel-prior", phase="3")
        self.assertEqual(second_result.process.returncode, 0,
                         second_result.process.stderr + second_result.process.stdout)
        calls = self.events("validate_and_push.sh")
        self.assertEqual(len(calls), 2)
        self.assertNotIn("--cancel-prior", calls[0])
        self.assertIn("--cancel-prior", calls[1])
        common = Path(self.git("rev-parse", "--path-format=absolute", "--git-common-dir"))
        self.assertEqual((self.state / "validation-locks").read_text().splitlines(),
                         [str(common / "main-push.lock")] * len(calls))
    def test_cancel_prior_with_git_push_is_refused_before_merge(self) -> None:
        sha = self.checkpoint()
        result = self.run_checkpoint(sha, "--cancel-prior")
        self.assertEqual(result.process.returncode, 1)
        self.assertIn("input: failed", result.process.stdout)
        self.assertIn("--cancel-prior applies only to Push: validate_and_push", result.process.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD"), result.before)
        self.assertEqual(self.events("checkpoint-test"), [])
    def test_a_checkpoint_without_a_change_review_line_is_held_before_merge(self) -> None:
        from scripts.production import change_review

        sha = self.dated_checkpoint("checkpoint(alpha): grid fades")
        result = self.run_checkpoint(sha)
        self.assertEqual(result.process.returncode, 1)
        self.assert_stopped_before_merge(result, "held", "checkpoint(alpha): grid fades: no Change-Review line")
        self.assertIn(sha[:change_review.SHORT], result.process.stdout)
        self.assertIn("a plain commit, not a merge: a merge may carry only `Change-Review: none` lines", result.process.stdout)
        self.assertIn(change_review.COVERS, result.process.stdout)
    def test_a_merge_adds_a_row_per_visible_change_to_the_named_list(self) -> None:
        self.promotions = str(self.promote)
        review = Path(self.env["HOME"]) / "rust/hanadocs/change-review/change-review - example.md"
        _ = review.write_text("## To review\n\n| # | Change | Merged | On the Mac build | How to check |\n"
                              + "| --- | --- | --- | --- | --- |\n| C7 | old | 2026-10-14 | yes | - |\n", encoding="utf-8")
        self.production_doc(change_review=review)
        sha = self.dated_checkpoint("checkpoint(alpha): grid fades\n\n"
                                    + "Change-Review: The grid fades gently | Run `hana` in a natedev terminal | The grid lines fade out farther away\n"
                                    + "Change-Review: none — the merge script")
        result = self.run_checkpoint(sha)
        self.assertEqual(result.process.returncode, 0, result.process.stderr + result.process.stdout)
        merged = self.git("rev-parse", "--short=8", "HEAD")
        with mock.patch.dict(os.environ, self.env):
            member = next(row for row in staff.snapshot().find(kind="unit") if row.name == "alpha")
            producer = member.id
            assert isinstance(member.parent, staff.ReportsTo)
            production = member.parent.id
        self.assertRegex(review.read_text(encoding="utf-8").splitlines()[-1],
                         rf"^\| C8 \| The grid fades gently \| \d{{4}}-\d\d-\d\d · {merged} \| not yet \| Run `hana` in a natedev terminal\. You see: The grid lines fade out farther away \| {production} \| {producer} \|$")
        self.assertIn("change review: ok — 1 row(s) added", result.process.stdout)
        self.assertNotIn("change review: drift", result.process.stdout)
        self.assertEqual(self.git("status", "--porcelain"), "")
    def test_a_production_without_a_list_writes_no_rows(self) -> None:
        sha = self.dated_checkpoint("checkpoint(alpha): grid fades\n\nChange-Review: none — the merge script")
        result = self.run_checkpoint(sha)
        self.assertEqual(result.process.returncode, 0, result.process.stderr + result.process.stdout)
        self.assertIn("change review: ok — the production doc has no Change review line; no rows written",
                      result.process.stdout)
    def test_absent_optional_fields_default_to_validate_push_without_promotion(self) -> None:
        self.push = ""
        self.production_doc()
        sha = self.checkpoint()
        result = self.run_checkpoint(sha)
        self.assertEqual(result.process.returncode, 0, result.process.stderr + result.process.stdout)
        self.assertEqual(len(self.events("validate_and_push.sh")), 1)
        self.assertIn("promote: ok — no promotion configured", result.process.stdout)
        self.assertEqual(self.events("ssh"), [])
    def test_delivers_line_is_merge_body_before_trailer(self) -> None:
        sha = self.checkpoint()
        result = self.run_checkpoint(sha, delivers="Delivers one reusable checkpoint command.")
        self.assertEqual(result.process.returncode, 0, result.process.stderr + result.process.stdout)
        subject = f"Merge alpha phase 2 ({sha[:7]}) into production"
        self.assertEqual(self.git("log", "-1", "--format=%B"),
                         subject + "\n\nDelivers one reusable checkpoint command.\n\n"
                         + "Co-authored-by: Test <test@example.invalid>")
    def test_missing_or_empty_delivers_is_refused_before_merge(self) -> None:
        sha = self.checkpoint()
        for delivers in (None, ""):
            with self.subTest(delivers=delivers):
                result = self.run_checkpoint(sha, delivers=delivers)
                self.assertEqual(result.process.returncode, 1)
                self.assertIn("input: failed", result.process.stdout)
                self.assertIn("--delivers", result.process.stdout)
                self.assertEqual(self.git("rev-parse", "HEAD"), result.before)
        self.assertEqual(self.events("checkpoint-test"), [])
    def test_git_push_rejection_restores_merge_branch(self) -> None:
        sha = self.checkpoint()
        self.executable(self.origin / "hooks/pre-receive", "#!/bin/sh\nexit 1\n")
        result = self.run_checkpoint(sha)
        self.assertIn("push: failed", result.process.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD"), result.before)
        self.assertEqual(self.git("rev-parse", "refs/heads/production", cwd=self.origin), result.before)
        self.assertEqual(self.events("review_regime.py"), [])
        self.assertEqual(self.events("style_sync.py"), [])
    def test_style_notification_observes_the_successfully_pushed_tip(self) -> None:
        observer = STUB + '''
import fcntl
import subprocess
tip = subprocess.run(["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
common = subprocess.run(["git", "rev-parse", "--git-common-dir"], check=True, capture_output=True, text=True).stdout.strip()
with (Path(common) / "main-push.lock").open("a") as lock:
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_state = "held"
    else:
        lock_state = "free"
with (state / "refreshed-test-locks").open("a") as output:
    output.write(lock_state + "\\n")
(state / "refreshed-test-tip").write_text(tip)
'''
        for name in ("verify.sh", "checkpoint-test"):
            self.executable(self.bin / name, observer)
        result = self.run_checkpoint(self.checkpoint())
        self.assertEqual(result.process.returncode, 0, result.process.stdout + result.process.stderr)
        tip = self.git("rev-parse", "HEAD")
        self.assertEqual(self.events("style_sync.py"), [[
            "notify", "--repo-dir", str(self.checkout), "--branch", "production", "--tip", tip,
        ]])
        self.assertEqual((self.state / "notified-tip").read_text(), tip)
        self.assertEqual((self.state / "refreshed-test-locks").read_text().splitlines(), ["free"])
        with self.subTest(refresh="main moved"):
            main = self.push_main_change("crates/main-proof/Cargo.toml",
                                         '[package]\nname = "main-proof"\nversion = "0.1.0"\n')
            production = read_new_production(self.doc, "example")
            with mock.patch.dict(os.environ, self.env):
                refreshed = merge_checkpoint.refresh_pushed_tip(production, tip)
            self.assertEqual((self.state / "refreshed-test-locks").read_text().splitlines(), ["free", "held", "held"])
            self.assertEqual(refreshed, (self.state / "refreshed-test-tip").read_text())
            self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), refreshed)
            self.assertEqual(self.git("merge-base", "--is-ancestor", main, refreshed), "")
            self.assertIn(["test", "main-proof"], self.events("verify.sh"))
        with self.subTest(refresh="main has a malformed manifest"):
            before_refresh = self.git("rev-parse", "HEAD")
            writer = self.root / "main-fix-writer"
            self.write(writer, "crates/main-proof/Cargo.toml", "[package\n")
            _ = self.git("add", "crates/main-proof/Cargo.toml", cwd=writer)
            _ = self.git("commit", "-m", "malformed manifest on main", cwd=writer)
            _ = self.git("push", "origin", "main", cwd=writer)
            with mock.patch.dict(os.environ, self.env), self.assertRaises(tomllib.TOMLDecodeError):
                _ = merge_checkpoint.refresh_pushed_tip(production, before_refresh)
            self.assertEqual(self.git("rev-parse", "HEAD"), before_refresh)
            self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), before_refresh)
            self.assertEqual(self.git("status", "--porcelain"), "")
    def test_style_notification_failure_does_not_fail_a_pushed_checkpoint(self) -> None:
        _ = (self.state / "fail-style_sync.py").touch()
        result = self.run_checkpoint(self.checkpoint())
        self.assertEqual(result.process.returncode, 0, result.process.stdout + result.process.stderr)
        self.assertIn("style sync: exit 1", result.process.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.git("rev-parse", "production", cwd=self.origin))
    def test_notification_value_error_leaves_the_completed_checkpoint_successful(self) -> None:
        sha = self.checkpoint()
        original_notify = merge_checkpoint.notify_style

        def malformed_notification(checkout: Path, branch: str, tip: str) -> bool:
            with mock.patch("scripts.production.merge_checkpoint.subprocess.run", side_effect=ValueError("invalid production")):
                return original_notify(checkout, branch, tip)

        arguments = [str(SCRIPT), "--production", "example", "alpha", "2", sha,
                     "--started", STARTED, "--scratch", str(self.root / "scratch"),
                     "--review-trial", TRIAL, "--delivers", DELIVERS, "--parallel", PARALLEL]
        output = StringIO()
        with mock.patch.dict(os.environ, self.env), mock.patch.object(sys, "argv", arguments), mock.patch.object(
            merge_checkpoint, "notify_style", side_effect=malformed_notification,
        ), redirect_stdout(output):
            code = merge_checkpoint.main()
        self.assertEqual(code, 0, output.getvalue())
        self.assertIn("style sync: invalid production", output.getvalue())
        self.assertEqual(self.git("rev-parse", "HEAD"), self.git("rev-parse", "production", cwd=self.origin))
    def test_diverged_main_conflict_restores_clean_merge_branch(self) -> None:
        sha = self.checkpoint(content="unit side\n")
        main_writer = self.root / "main-writer"
        _ = self.git("clone", "-b", "main", str(self.origin), str(main_writer), cwd=self.root)
        _ = self.git("config", "user.name", "Checkpoint Test", cwd=main_writer)
        _ = self.git("config", "user.email", "checkpoint@example.invalid", cwd=main_writer)
        self.write(main_writer, "src/alpha.txt", "main side\n")
        _ = self.git("add", "src/alpha.txt", cwd=main_writer)
        _ = self.git("commit", "-m", "conflicting main edit", cwd=main_writer)
        _ = self.git("push", "origin", "main", cwd=main_writer)
        result = self.run_checkpoint(sha)
        self.assertIn("push: held", result.process.stdout)
        self.assertIn("src/alpha.txt", result.process.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD"), result.before)
        self.assertNotEqual(merge_checkpoint.git(self.checkout, "rev-parse", "-q", "--verify", "MERGE_HEAD").returncode, 0)
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual(self.git("rev-parse", "refs/heads/production", cwd=self.origin), result.before)

    def test_push_main_conflict_preserves_checkout_for_both_rollback_policies(self) -> None:
        _ = self.checkpoint(content="unit alpha\n")
        sha = self.checkpoint("src/beta.txt", "unit beta\n")
        before_checkpoint_merge = self.git("rev-parse", "HEAD")
        original_files = {path: (self.checkout / path).read_bytes() for path in ("src/alpha.txt", "src/beta.txt")}
        _ = self.git("merge", "--no-ff", "-m", "merge checkpoint", sha)
        _ = self.git("push", "origin", "production")
        for path in ("src/alpha.txt", "src/beta.txt"):
            self.write(self.promote, path, "main side\n")
        _ = self.git("add", "src", cwd=self.promote)
        _ = self.git("commit", "-m", "conflicting main edits", cwd=self.promote)
        _ = self.git("push", "origin", "main", cwd=self.promote)
        production = read_new_production(self.doc, "example")
        before = self.git("rev-parse", "HEAD")
        before_files = {path: (self.checkout / path).read_bytes() for path in ("src/alpha.txt", "src/beta.txt")}
        for may_undo in (False, True):
            with self.subTest(may_undo=may_undo):
                with self.assertRaises(checkpoint_gate.Stop) as stopped:
                    if may_undo:
                        _ = merge_checkpoint.prepare_checkpoint(production, before)
                        _ = merge_checkpoint.push_git(production, True)
                    else:
                        _ = merge_checkpoint.refresh_pushed_tip(production, before)
                error = stopped.exception
                output = StringIO()
                with redirect_stdout(output):
                    checkpoint_gate.report(error.step, error.state, error.detail)
                self.assertIn("push: held", output.getvalue())
                self.assertIn("src/alpha.txt, src/beta.txt", output.getvalue())
                self.assertIn("merge origin/main into the merge branch", error.message)
                self.assertIn("run the push again", error.message)
                self.assertNotEqual(merge_checkpoint.git(self.checkout, "rev-parse", "-q", "--verify", "MERGE_HEAD").returncode, 0)
                self.assertEqual(self.git("rev-parse", "HEAD"), before_checkpoint_merge if may_undo else before)
                self.assertEqual(self.git("status", "--porcelain"), "")
                if may_undo:
                    for path, content in original_files.items():
                        self.assertEqual((self.checkout / path).read_bytes(), content)
                else:
                    for path, content in before_files.items():
                        self.assertEqual((self.checkout / path).read_bytes(), content)


    def test_red_package_resets_merge_and_known_flake_continues(self) -> None:
        self.write(self.unit, "crates/alpha/Cargo.toml", '[package]\nname = "alpha"\nversion = "0.1.0"\n')
        self.write(self.unit, "crates/alpha/src/lib.rs", "pub fn value() -> i32 { 1 }\n")
        _ = self.git("add", "crates/alpha", cwd=self.unit)
        _ = self.git("commit", "-m", "add package", cwd=self.unit)
        sha = self.git("rev-parse", "HEAD", cwd=self.unit)
        _ = self.git("push", "origin", "alpha-branch", cwd=self.unit)
        _ = (self.state / "fail-always-test").touch()
        failed = self.run_checkpoint(sha)
        self.assertIn("test: ", failed.process.stdout)
        self.assertIn("red: failed", failed.process.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD"), failed.before)
        self.assertEqual(len(self.events("verify.sh")), 2)
        (self.state / "fail-always-test").unlink()
        _ = (self.state / "fail-once-test").touch()
        self.known_flakes = "alpha"
        self.production_doc()
        passed = self.run_checkpoint(sha)
        self.assertEqual(passed.process.returncode, 0, passed.process.stderr + passed.process.stdout)
        self.assertEqual(len(self.events("verify.sh")), 4)
        self.assertIn("flake", self.log.read_text().lower())
    def test_a_docs_path_under_a_workspace_root_runs_no_package_test(self) -> None:
        self.write(self.unit, "crates/alpha/Cargo.toml", '[workspace]\nmembers = ["crates/*"]\n')
        self.write(self.unit, "crates/alpha/docs/as-built/pane.md", "# Pane\n")
        _ = self.git("add", "crates/alpha", cwd=self.unit)
        _ = self.git("commit", "-m", "docs only", cwd=self.unit)
        sha = self.git("rev-parse", "HEAD", cwd=self.unit)
        _ = self.git("push", "origin", "alpha-branch", cwd=self.unit)
        _ = (self.state / "fail-always-test").touch()
        result = self.run_checkpoint(sha)
        self.assertEqual(result.process.returncode, 0, result.process.stderr + result.process.stdout)
        self.assertEqual(self.events("verify.sh"), [])
    def test_two_backticked_known_flakes_each_count_green_alone(self) -> None:
        for package in ("crate-a", "crate-b"):
            self.write(self.unit, f"crates/alpha/{package}/Cargo.toml",
                       f'[package]\nname = "{package}"\nversion = "0.1.0"\n')
            self.write(self.unit, f"crates/alpha/{package}/src/lib.rs", "pub fn value() {}\n")
            _ = (self.state / f"fail-once-test-{package}").touch()
        _ = self.git("add", "crates/alpha", cwd=self.unit)
        _ = self.git("commit", "-m", "two packages", cwd=self.unit)
        sha = self.git("rev-parse", "HEAD", cwd=self.unit)
        _ = self.git("push", "origin", "alpha-branch", cwd=self.unit)
        self.known_flakes = "`crate-a`, `crate-b`"
        self.production_doc()
        result = self.run_checkpoint(sha)
        self.assertEqual(result.process.returncode, 0, result.process.stderr + result.process.stdout)
        self.assertIn("known flake crate-a", self.log.read_text(encoding="utf-8"))
        self.assertIn("known flake crate-b", self.log.read_text(encoding="utf-8"))
        self.assertEqual(len(self.events("verify.sh")), 4)
    def test_known_flaky_tests_rerun_alone_and_an_unlisted_failure_stays_red(self) -> None:
        """cargo-liner-style: a whole verify.sh merge test where two known flaky tests failed."""
        reader = "shim_registration::reader_scenarios::reader_attributes_compiler_cache"
        locale = "shim_registration::reader_scenarios::reader_accepts_live_writer_under_different_locale"
        rows = "".join(f"        FAIL [  12.202s] ({count}/1067) cargo-tile::unit_tests {test}\n"
                       for count, test in ((1012, reader), (1013, locale)))
        # nextest prints each failure again after its Summary line.
        _ = (self.state / "fail-rows").write_text(rows + "     Summary [ 80.1s] 1067 tests run\n" + rows)
        sha = self.checkpoint()
        self.known_flaky_tests = f"`{reader}`"
        self.production_doc(merge_tests_line="- **Merge tests:** `verify.sh test cargo-tile`")
        failed = self.run_checkpoint(sha)
        self.assertIn("red: failed", failed.process.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD"), failed.before)
        self.assertEqual(self.events("verify.sh"), [["test", "cargo-tile"], ["test", "cargo-tile"]])
        (self.state / "events.jsonl").unlink()
        self.known_flaky_tests = f"`{reader}`, `{locale}` — red only under load"
        self.production_doc(merge_tests_line="- **Merge tests:** `verify.sh test cargo-tile`")
        passed = self.run_checkpoint(sha)
        self.assertEqual(passed.process.returncode, 0, passed.process.stderr + passed.process.stdout)
        self.assertEqual(self.events("verify.sh"), [
            ["test", "cargo-tile"],
            ["test", "cargo-tile", "--filter", locale, "--filter", reader],
        ])
        self.assertIn(f"known flaky tests {locale}, {reader}: red in merge test 1, green alone",
                      self.log.read_text(encoding="utf-8"))


if __name__ == "__main__":
    _ = unittest.main()
