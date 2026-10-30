"""Check assertion-guard behavior against real Git repositories."""
from __future__ import annotations

import resource
import time
import unittest
from pathlib import Path
from typing import override

from scripts.direct.assertion_guard import commit_bases, default_base
from scripts.direct.assertion_guard_test_support import AssertionFindingFixture, SeededGitRepositoryFixture, SeededMergeInProgressFixture, CLEAN, TILE_OLD, TILE_NEW

class AssertionRevisionTests(AssertionFindingFixture):
    def test_executable_matches_in_process_output_and_status(self) -> None:
        for arguments, weak in (((), False), ((), True), (("--base", "absent"), True)):
            with self.subTest(arguments=arguments, weak=weak):
                self.change("value.rs", self.rust(TILE_OLD), self.rust(TILE_NEW if weak else TILE_OLD))
                expected = self.guard(*arguments)
                actual = self.guard_process(*arguments)
                self.assertEqual((actual.returncode, actual.stdout, actual.stderr),
                                 (expected.returncode, expected.stdout, expected.stderr))


    def test_rulings_from_file_directory_stdin_and_commit_message(self) -> None:
        self.change("value.rs", self.rust(TILE_OLD), self.rust(TILE_NEW))
        ruling = "Assertion-Change: value.rs::test_value — Bounds are the approved contract."
        file = self.root / "ruling.txt"
        _ = file.write_text(ruling + "\n", encoding="utf-8")
        folder = self.root / "orders" / "nested"
        folder.mkdir(parents=True)
        _ = (folder / "spec.md").write_text(ruling.replace("—", "-") + "\n", encoding="utf-8")
        for arguments, stdin in ((("--rulings", str(file)), ""),
                                  (("--rulings", str(folder.parent)), ""), (("--rulings", "-"), ruling)):
            with self.subTest(arguments=arguments):
                line = self.assert_finding(self.guard(*arguments, stdin=stdin), "value.rs", "weaker", status=0)
                self.assertIn("ruled: Bounds are the approved contract.", line)
        _ = self.git("add", "value.rs")
        _ = self.git("commit", "-q", "-m", "approved change\n\n" + ruling)
        line = self.assert_finding(self.guard("--base", self.base, "--head", "HEAD"), "value.rs", "weaker", status=0)
        self.assertIn("ruled: Bounds are the approved contract.", line)


    def test_rulings_for_another_test_or_path_or_without_a_reason_do_not_clear_findings(self) -> None:
        self.change("value.rs", self.rust(TILE_OLD), self.rust(TILE_NEW))
        for ruling in ("Assertion-Change: value.rs::test_other — Approved.",
                        "Assertion-Change: other.rs::test_value — Approved.",
                        "Assertion-Change: value.rs::test_value —   "):
            with self.subTest(ruling=ruling):
                _ = self.assert_finding(self.guard("--rulings", "-", stdin=ruling), "value.rs", "weaker")


    def test_working_tree_staged_and_head_modes_read_the_right_side(self) -> None:
        before, after = self.rust(TILE_OLD), self.rust(TILE_NEW)
        self.change("value.rs", before, after)
        _ = self.assert_finding(self.guard(), "value.rs", "weaker")
        self.assert_clean(self.guard("--staged"))
        _ = self.git("add", "value.rs")
        _ = self.save("value.rs", before)
        self.assert_clean(self.guard())
        _ = self.assert_finding(self.guard("--staged"), "value.rs", "weaker")
        _ = self.git("commit", "-q", "-m", "weaken assertion")
        _ = self.assert_finding(self.guard("--base", self.base, "--head", "HEAD"), "value.rs", "weaker")
        self.assert_clean(self.guard("--base", self.base))


    def test_local_main_without_remote_candidates_requires_explicit_base(self) -> None:
        before = "assert_eq!(actual, expected);\n    assert!(ok);"
        self.change("value.rs", self.rust(before), self.rust("assert!(ok);"))
        _ = self.git("branch", "-m", "main")
        _ = self.git("remote", "remove", "origin")
        result = self.guard()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)
        self.assertIn("VERIFY_ASSERTION_BASE", result.stderr)
        _ = self.assert_finding(self.guard("--base", self.base), "value.rs", "removed")


    def test_missing_branch_base_refuses_with_explicit_override_guidance(self) -> None:
        _ = self.git("remote", "remove", "origin")
        result = self.guard()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)
        self.assertIn("VERIFY_ASSERTION_BASE", result.stderr)
        self.assertNotIn(CLEAN, result.stdout)


    def test_no_default_base_and_invalid_revisions_fail_closed(self) -> None:
        _ = self.git("remote", "remove", "origin")
        for arguments in ((), ("--base", "absent"), ("--base", self.base, "--head", "absent"),
                          ("--base", self.base, "--head", "HEAD", "--staged")):
            with self.subTest(arguments=arguments):
                result = self.guard(*arguments)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)
                self.assertNotIn(CLEAN, result.stdout)


    def test_repeated_bases_do_not_combine_different_assertions_in_one_test(self) -> None:
        _ = self.save("test_value.py", self.python("self.assertEqual(first, expected)"))
        self.commit_base()
        first_base = self.base
        _ = self.save("test_value.py", self.python("self.assertEqual(second, expected)"))
        _ = self.git("add", "test_value.py")
        _ = self.git("commit", "-q", "-m", "other comparison base")
        _ = self.save("test_value.py", self.python("pass"))
        _ = self.git("add", "test_value.py")
        self.assert_clean(self.guard("--base", first_base, "--base", "HEAD", "--staged"))


    def test_repeated_bases_count_only_duplicate_assertions_present_in_every_base(self) -> None:
        assertion = "self.assertEqual(actual, expected)"
        _ = self.save("test_value.py", self.python(assertion))
        self.commit_base()
        first_base = self.base
        _ = self.save("test_value.py", self.python(assertion + "\n    " + assertion))
        _ = self.git("add", "test_value.py")
        _ = self.git("commit", "-q", "-m", "duplicate assertion in second base")
        _ = self.save("test_value.py", self.python("pass"))
        _ = self.git("add", "test_value.py")
        _ = self.assert_finding(self.guard("--base", first_base, "--base", "HEAD", "--staged"),
                                "test_value.py", "removed")


    def test_untracked_files_and_other_extensions_have_no_findings(self) -> None:
        self.change("notes.txt", self.python("assert actual == expected"), self.python("pass"))
        _ = self.save("untracked.py", "@pytest.mark.skip\ndef test_untracked():\n    pass\n")
        self.assert_clean(self.guard())


    def test_large_diff_guard_call_uses_less_than_one_cpu_second(self) -> None:
        before = self.rust(TILE_OLD) + "".join(f"// old {number}\n" for number in range(43499))
        after = self.rust(TILE_NEW) + "".join(f"// new {number}\n" for number in range(43499))
        self.change("value.rs", before, after)
        children_before = resource.getrusage(resource.RUSAGE_CHILDREN)
        started = time.process_time()
        result = self.guard_process()
        cpu_seconds = time.process_time() - started
        children_after = resource.getrusage(resource.RUSAGE_CHILDREN)
        cpu_seconds += (children_after.ru_utime + children_after.ru_stime
                        - children_before.ru_utime - children_before.ru_stime)
        _ = self.assert_finding(result, "value.rs", "weaker")
        self.assertLess(cpu_seconds, 1.0, f"guard used {cpu_seconds:.3f} CPU seconds")



class MergeAssertionTests(SeededMergeInProgressFixture):
    def test_commit_bases_include_head_and_the_in_progress_merge_parent(self) -> None:
        self.assertEqual(commit_bases(self.repo),
                         (self.git("rev-parse", "HEAD"), self.git("rev-parse", "MERGE_HEAD")))

    def test_repeated_bases_exclude_the_other_parents_assertion_removal(self) -> None:
        # A HEAD-only comparison really does see the imported removal.
        single = self.guard("--base", "HEAD", "--staged")
        self.assertEqual(single.returncode, 1, single.stdout + single.stderr)
        self.assertIn("test_incoming removed:", single.stdout)
        for first, second in (("HEAD", "MERGE_HEAD"), ("MERGE_HEAD", "HEAD")):
            with self.subTest(first=first):
                result = self.guard("--base", first, "--base", second, "--staged")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(result.stdout.strip(), CLEAN)
                self.assertEqual(result.stderr, "")

    def test_removal_during_merge_blocks_only_the_assertion_absent_against_both_parents(self) -> None:
        _ = self.save("test_merge.py", self.merge_source(incoming=False, ours=False))
        _ = self.git("add", "test_merge.py")
        for first, second in (("HEAD", "MERGE_HEAD"), ("MERGE_HEAD", "HEAD")):
            with self.subTest(first=first):
                result = self.guard("--base", first, "--base", second, "--staged")
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("test_ours removed:", result.stdout)
                self.assertNotIn("test_incoming", result.stdout)
                self.assertIn("assertion guard: 1 blocking;", result.stdout)
                self.assertEqual(result.stderr, "")

    def test_completed_merge_default_base_excludes_the_other_parents_removals(self) -> None:
        _ = self.git("commit", "-q", "-m", "merge incoming")
        explicit = self.guard("--base", "HEAD^1")
        self.assertEqual(explicit.returncode, 1, explicit.stdout + explicit.stderr)
        self.assertIn("test_incoming removed:", explicit.stdout)
        result = self.guard()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), CLEAN)
        self.assertEqual(result.stderr, "")

    def test_completed_merge_still_blocks_our_new_assertion_removal(self) -> None:
        _ = self.git("commit", "-q", "-m", "merge incoming")
        _ = self.save("test_merge.py", self.merge_source(incoming=False, ours=False))
        result = self.guard()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("test_ours removed:", result.stdout)
        self.assertNotIn("test_incoming", result.stdout)
        self.assertIn("assertion guard: 1 blocking;", result.stdout)



class BranchBaseTests(SeededGitRepositoryFixture):
    repo: Path = Path()

    @override
    def build_fixture(self) -> None:
        super().build_fixture()
        _ = self.save("value.rs", self.rust(TILE_OLD))
        self.commit_base()
        _ = self.git("push", "-q", "origin", "HEAD:refs/heads/main")
        _ = self.save("value.rs", self.rust(TILE_NEW))
        self.commit_base()
        # Cloning our own remote creates origin/HEAD naturally. Never rewrite it.
        checkout = self.root / "checkout"
        _ = self.git("clone", "-q", str(self.root / "remote.git"), str(checkout))
        self.repo = checkout
        _ = self.git("checkout", "-q", "-b", "topic", "--track", "origin/unit")

    def commit_file(self, path: str) -> str:
        _ = self.save(path, "branch change\n")
        _ = self.git("add", path)
        _ = self.git("commit", "-q", "-m", "branch change")
        return self.git("rev-parse", "HEAD")

    def assert_refusal(self) -> None:
        result = self.guard()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr.splitlines(), [
            "assertion guard: cannot determine branch base; set VERIFY_ASSERTION_BASE (or --base)",
        ])

    def test_non_main_upstream_precedes_a_nearer_remote_and_ignores_old_assertion_changes(self) -> None:
        tip = self.commit_file("local.txt")
        _ = self.git("push", "-q", "origin", "HEAD:refs/heads/nearer")
        self.assertNotEqual(tip, self.base)
        self.assertEqual(default_base(self.repo), self.base)
        result = self.guard()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), CLEAN)

    def test_upstream_uses_the_merge_base_when_local_and_remote_have_diverged(self) -> None:
        _ = self.git("checkout", "-q", "--detach", "origin/unit")
        remote_tip = self.commit_file("remote.txt")
        _ = self.git("push", "-q", "origin", "HEAD:refs/heads/unit")
        _ = self.git("checkout", "-q", "topic")
        local_tip = self.commit_file("local.txt")
        self.assertNotEqual(local_tip, remote_tip)
        self.assertEqual(default_base(self.repo), self.base)

    def test_detached_feature_head_uses_its_nearest_remote_branch(self) -> None:
        _ = self.git("checkout", "-q", "--detach")
        _ = self.commit_file("detached.txt")
        self.assertEqual(default_base(self.repo), self.base)
        result = self.guard()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), CLEAN)

    def test_branch_without_upstream_uses_nearest_remote_merge_base(self) -> None:
        _ = self.git("branch", "--unset-upstream")
        _ = self.git("push", "-q", "origin", "origin/main:refs/heads/distant")
        _ = self.commit_file("local.txt")
        self.assertEqual(default_base(self.repo), self.base)

    def test_equal_nearest_remote_branches_with_one_merge_base_are_accepted(self) -> None:
        _ = self.git("push", "-q", "origin", "HEAD:refs/heads/alias")
        _ = self.git("checkout", "-q", "--detach")
        _ = self.commit_file("local.txt")
        self.assertEqual(default_base(self.repo), self.base)

    def test_equal_nearest_remote_branches_with_different_merge_bases_refuse(self) -> None:
        _ = self.git("checkout", "-q", "--detach", "origin/main")
        _ = self.commit_file("alternate.txt")
        _ = self.git("push", "-q", "origin", "HEAD:refs/heads/alternate")
        _ = self.git("checkout", "-q", "topic")
        _ = self.git("merge", "-q", "--no-ff", "origin/alternate", "-m", "join branches")
        _ = self.git("branch", "--unset-upstream")
        self.assertEqual(self.git("rev-list", "--count", "origin/unit..HEAD"),
                         self.git("rev-list", "--count", "origin/alternate..HEAD"))
        self.assertNotEqual(self.git("merge-base", "HEAD", "origin/unit"),
                            self.git("merge-base", "HEAD", "origin/alternate"))
        self.assert_refusal()

    def test_remote_head_and_its_main_target_are_not_default_candidates(self) -> None:
        _ = self.git("checkout", "-q", "--detach")
        _ = self.git("update-ref", "-d", "refs/remotes/origin/unit")
        self.assertEqual(self.git("symbolic-ref", "refs/remotes/origin/HEAD"), "refs/remotes/origin/main")
        self.assert_refusal()
        self.assertEqual(self.git("symbolic-ref", "refs/remotes/origin/HEAD"), "refs/remotes/origin/main")

    def test_explicit_base_wins_over_the_non_main_upstream(self) -> None:
        result = self.guard("--base", "origin/main")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("value.rs:3 test_value weaker:", result.stdout)
        self.assertEqual(result.stderr, "")

    def test_explicit_base_works_when_no_remote_candidate_remains(self) -> None:
        _ = self.git("checkout", "-q", "--detach")
        _ = self.git("update-ref", "-d", "refs/remotes/origin/unit")
        self.assert_refusal()
        result = self.guard("--base", self.base)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), CLEAN)



if __name__ == "__main__":
    _ = unittest.main()
