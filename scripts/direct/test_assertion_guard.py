"""Check assertion changes against real git revisions, indexes, and working trees."""
from __future__ import annotations

import os
import re
import resource
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import override

GUARD = Path(__file__).with_name("assertion_guard.py")
TILE_OLD = "assert_eq!(foot_cell.clip().bottom(), COLUMN_FOOT_TEST_AREA.bottom());"
TILE_NEW = "assert!(foot_cell.clip().bottom() <= COLUMN_FOOT_TEST_AREA.bottom());"
CLEAN = "assertion guard: no removed or weaker assertions"


class GitRepositoryFixture(unittest.TestCase):
    root: Path = Path()
    repo: Path = Path()
    base: str = ""
    environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.environment = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull,
                            "GIT_CONFIG_NOSYSTEM": "1", "PYTHONDONTWRITEBYTECODE": "1"}
        _ = self.git("init", "-q", "-b", "unit")
        _ = self.save("README.md", "Fixture repository.\n")
        self.commit_base()

    def git(self, *arguments: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.repo), "-c", "user.name=t", "-c", "user.email=t@t",
             "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *arguments],
            env=self.environment, capture_output=True, text=True, check=True, timeout=5,
        )
        return result.stdout.strip()

    def save(self, name: str, text: str) -> Path:
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(text, encoding="utf-8")
        return path

    def commit_base(self) -> None:
        _ = self.git("add", "--all")
        _ = self.git("commit", "-q", "--allow-empty", "-m", "fixture base")
        self.base = self.git("rev-parse", "HEAD")
        _ = self.git("update-ref", "refs/remotes/origin/main", self.base)

    def guard(self, *arguments: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(GUARD), "--repo", str(self.repo), *arguments],
            env=self.environment, input=stdin, capture_output=True, text=True, check=False, timeout=5,
        )

    def change(self, path: str, before: str, after: str) -> None:
        _ = self.save(path, before)
        self.commit_base()
        _ = self.save(path, after)

    def rust(self, assertion: str, name: str = "test_value") -> str:
        return f"#[test]\nfn {name}() {{\n    {assertion}\n}}\n"

    def python(self, assertion: str, name: str = "test_value") -> str:
        return f"def {name}(self):\n    {assertion}\n"


class AssertionGuardTests(GitRepositoryFixture):
    def assert_clean(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), CLEAN)
        self.assertEqual(result.stderr, "")

    def assert_finding(self, result: subprocess.CompletedProcess[str], path: str, kind: str,
                       test: str = "test_value", status: int = 1) -> str:
        self.assertEqual(result.returncode, status, result.stdout + result.stderr)
        prefix = "note: " if kind == "changed" else ""
        findings = [line for line in result.stdout.splitlines() if not line.startswith("assertion guard:")]
        self.assertEqual(len(findings), 1, result.stdout)
        self.assertRegex(findings[0], rf"^{re.escape(prefix + path)}:\d+ {re.escape(test)} {kind}: ")
        self.assertEqual(result.stderr, "")
        if status == 1:
            self.assertIn("assertion guard: 1 blocking;", result.stdout.splitlines()[-1])
        return findings[0]

    def test_tile_weaker_assertion_is_found_among_six_thousand_other_changes(self) -> None:
        path = "crates/tui_pane/src/tiles/draw.rs"
        before = self.rust(TILE_OLD, "column_foot_tracks_area_bottom")
        after = self.rust(TILE_NEW, "column_foot_tracks_area_bottom")
        before += "".join(f"// old tile {number}\n" for number in range(3000))
        after += "".join(f"// new tile {number}\n" for number in range(3000))
        self.change(path, before, after)
        line = self.assert_finding(self.guard(), path, "weaker", "column_foot_tracks_area_bottom")
        self.assertIn(TILE_OLD + " -> " + TILE_NEW, line)
        self.assertTrue(line.startswith(path + ":3 "), line)

    def test_removed_rust_assertion_macros(self) -> None:
        for macro in ("assert", "assert_eq", "assert_ne", "debug_assert", "debug_assert_eq",
                      "debug_assert_ne", "assert_matches", "prop_assert", "prop_assert_eq", "prop_assert_ne"):
            with self.subTest(macro=macro):
                assertion = macro + "!(actual, expected);"
                self.change("value.rs", self.rust(assertion), self.rust("let kept = actual;"))
                line = self.assert_finding(self.guard(), "value.rs", "removed")
                self.assertIn(assertion + " -> (none)", line)

    def test_removed_python_assertions_and_multiline_first_line(self) -> None:
        for assertion in ("assert actual == expected", "self.assertEqual(actual, expected)",
                          "self.assertTrue(actual)", "pytest.raises(ValueError)", "self.fail('missing')",
                          "self.assertEqual(\n        actual,\n        expected,\n    )"):
            with self.subTest(assertion=assertion):
                self.change("test_value.py", self.python(assertion), self.python("pass"))
                _ = self.assert_finding(self.guard(), "test_value.py", "removed")

    def test_exact_rust_assertions_replaced_with_bounds_ranges_or_matches_are_weaker(self) -> None:
        for before, after in (("assert_eq!(actual, expected);", "assert!(actual <= expected);"),
                              ("assert_ne!(actual, wrong);", "assert!(actual);"),
                              ("debug_assert_eq!(actual, expected);", "debug_assert!((0..10).contains(&actual));"),
                              ("prop_assert_eq!(actual, expected);", "prop_assert!(matches!(actual, Some(_)));")):
            with self.subTest(before=before, after=after):
                self.change("value.rs", self.rust(before), self.rust(after))
                _ = self.assert_finding(self.guard(), "value.rs", "weaker")

    def test_exact_python_assertions_replaced_with_weaker_checks(self) -> None:
        exact = ("assertEqual", "assertNotEqual", "assertIs", "assertListEqual", "assertDictEqual",
                 "assertTupleEqual", "assertSetEqual", "assertCountEqual", "assertMultiLineEqual", "assertSequenceEqual")
        for method in exact:
            with self.subTest(method=method):
                self.change("test_value.py", self.python(f"self.{method}(actual, expected)"),
                            self.python("self.assertTrue(actual)"))
                _ = self.assert_finding(self.guard(), "test_value.py", "weaker")
        for before, after in (("assert actual == expected", "assert actual <= expected"),
                              ("assert actual != wrong", "assert actual"),
                              ("assert actual is expected", "self.assertFalse(actual)"),
                              ("self.assertEqual(actual, expected)", "self.assertLessEqual(actual, expected)"),
                              ("self.assertEqual(actual, expected)", "self.assertGreater(actual, 0)"),
                              ("self.assertEqual(actual, expected)", "self.assertIn(actual, expected)"),
                              ("self.assertEqual(actual, expected)", "self.assertAlmostEqual(actual, expected)"),
                              ("self.assertEqual(actual, expected)", "self.assertRegex(actual, expected)")):
            with self.subTest(before=before, after=after):
                self.change("test_value.py", self.python(before), self.python(after))
                _ = self.assert_finding(self.guard(), "test_value.py", "weaker")

    def test_changed_expected_values_are_notes_and_do_not_block(self) -> None:
        for path, before, after in (("value.rs", self.rust("assert_eq!(bottom, 40);"),
                                     self.rust("assert_eq!(bottom, 32);")),
                                    ("test_value.py", self.python("self.assertEqual(bottom, 40)"),
                                     self.python("self.assertEqual(bottom, 32)"))):
            with self.subTest(path=path):
                self.change(path, before, after)
                _ = self.assert_finding(self.guard(), path, "changed", status=0)

    def test_changed_helpers_and_membership_checks_share_any_operand(self) -> None:
        for before, after in (('self.assert_log("0", "t", "a", "x")',
                               'self.assert_log("0", "t", "b", "x")'),
                              ('self.assertIn("last", reason.lower())',
                               'self.assertIn("final", reason.lower())')):
            with self.subTest(before=before, after=after):
                self.change("test_value.py", self.python(before), self.python(after))
                line = self.assert_finding(self.guard(), "test_value.py", "changed", status=0)
                self.assertIn(before + " -> " + after, line)

    def test_bare_assertions_without_a_shared_operand_stay_removed(self) -> None:
        self.change("test_value.py", self.python("assert ok"), self.python("assert True"))
        line = self.assert_finding(self.guard(), "test_value.py", "removed")
        self.assertIn("assert ok -> (none)", line)

    def test_same_rust_macro_with_a_weaker_comparison_is_not_a_changed_note(self) -> None:
        self.change("value.rs", self.rust("assert!(a == b);"), self.rust("assert!(a <= b);"))
        line = self.assert_finding(self.guard(), "value.rs", "weaker")
        self.assertIn("assert!(a == b); -> assert!(a <= b);", line)

    def test_unrelated_subjects_do_not_pair_as_weaker_assertions(self) -> None:
        self.change("test_value.py", self.python("self.assertEqual(lean(x), 1.0)"),
                    self.python("self.assertLess(rate(x), 2)"))
        line = self.assert_finding(self.guard(), "test_value.py", "removed")
        self.assertIn("self.assertEqual(lean(x), 1.0) -> (none)", line)

    def test_subject_identifier_prefix_does_not_pair_as_weaker(self) -> None:
        self.change("value.rs", self.rust("assert_eq!(actual, 3);"), self.rust("assert!(actual_total > 0);"))
        line = self.assert_finding(self.guard(), "value.rs", "removed")
        self.assertIn("assert_eq!(actual, 3); -> (none)", line)

    def test_reflowed_python_assertion_with_the_same_tokens_has_no_finding(self) -> None:
        before = "self.assertEqual(\n        actual,\n        expected)"
        after = "self.assertEqual(actual,\n        expected)"
        self.change("test_value.py", self.python(before), self.python(after))
        self.assert_clean(self.guard())

    def test_multiline_python_assertion_with_an_unchanged_expected_line_is_changed(self) -> None:
        before = ('self.assertEqual([(event["suite"], event["module"]) for event in starts[:2]],\n'
                  '        [("lib", "test_second.py"), ("direct", "test_first.py")])')
        after = ('self.assertEqual([(suite, path.name) for suite, path in ordered[:2]],\n'
                 '        [("lib", "test_second.py"), ("direct", "test_first.py")])')
        self.change("test_value.py", self.python(before), self.python(after))
        _ = self.assert_finding(self.guard(), "test_value.py", "changed", status=0)

    def test_multiline_rust_assertion_with_only_the_expected_line_changed_is_changed(self) -> None:
        before = "assert_eq!(actual,\n        40\n    );"
        after = "assert_eq!(actual,\n        32\n    );"
        self.change("value.rs", self.rust(before), self.rust(after))
        _ = self.assert_finding(self.guard(), "value.rs", "changed", status=0)

    def test_multiline_rust_exact_assertion_replaced_with_a_bound_is_weaker(self) -> None:
        before = "assert_eq!(x,\n        y\n    );"
        after = "assert!(\n        x <= y);"
        self.change("value.rs", self.rust(before), self.rust(after))
        _ = self.assert_finding(self.guard(), "value.rs", "weaker")

    def test_multiline_rust_comparison_changed_on_its_own_line_is_weaker(self) -> None:
        before = "assert!(\n        a == b\n    );"
        after = "assert!(\n        a <= b\n    );"
        self.change("value.rs", self.rust(before), self.rust(after))
        _ = self.assert_finding(self.guard(), "value.rs", "weaker")

    def test_multiline_python_expected_value_changed_on_its_own_line_is_changed(self) -> None:
        before = "self.assertEqual(\n        total,\n        4,\n    )"
        after = "self.assertEqual(\n        total,\n        3,\n    )"
        self.change("test_value.py", self.python(before), self.python(after))
        _ = self.assert_finding(self.guard(), "test_value.py", "changed", status=0)

    def test_multiline_python_comparison_replaced_with_its_subject_is_weaker(self) -> None:
        before = "assert (\n        x == y\n    )"
        after = "assert (\n        x\n    )"
        self.change("test_value.py", self.python(before), self.python(after))
        _ = self.assert_finding(self.guard(), "test_value.py", "weaker")

    def test_changed_statement_between_assertions_has_no_finding(self) -> None:
        before = "assert!(ok);\n    let v = 2;\n    assert_eq!(v, expected);"
        after = "assert!(ok);\n    let v = 3;\n    assert_eq!(v, expected);"
        self.change("value.rs", self.rust(before), self.rust(after))
        self.assert_clean(self.guard())

    def test_shared_set_subject_with_a_bound_and_extra_condition_stays_weaker(self) -> None:
        before = "assert set(actual) == expected"
        after = "assert set(actual) <= expected and ok"
        self.change("test_value.py", self.python(before), self.python(after))
        line = self.assert_finding(self.guard(), "test_value.py", "weaker")
        self.assertIn(before + " -> " + after, line)

    def test_deleted_test_combines_three_assertions_and_one_ruling_clears_it(self) -> None:
        for path, before in (("value.rs", self.rust("assert_eq!(a, 1);\n    assert_eq!(b, 2);\n    assert!(ok);")),
                             ("test_value.py", self.python("assert a == 1\n    self.assertEqual(b, 2)\n    assert ok"))):
            with self.subTest(path=path):
                self.change(path, before, "// gone\n" if path.endswith(".rs") else "# gone\n")
                for ruled in (False, True):
                    with self.subTest(ruled=ruled):
                        ruling = f"Assertion-Change: {path}::test_value — The test is no longer needed."
                        result = self.guard("--rulings", "-", stdin=ruling) if ruled else self.guard()
                        self.assertEqual(result.returncode, 0 if ruled else 1, result.stdout + result.stderr)
                        self.assertEqual(result.stderr, "")
                        findings = [line for line in result.stdout.splitlines()
                                    if not line.startswith("assertion guard:")]
                        self.assertEqual(len(findings), 1, result.stdout)
                        self.assertRegex(findings[0], rf"^{re.escape(path)}:\d+ test_value test removed \(3 assertions\): ")
                        self.assertNotIn(" test_value removed:", result.stdout)
                        if ruled:
                            self.assertIn("ruled: The test is no longer needed.", findings[0])
                        else:
                            self.assertIn("assertion guard: 1 blocking;", result.stdout)

    def test_removed_test_functions_in_both_languages(self) -> None:
        for annotation in ("#[test]", "#[tokio::test]", "#[rstest]"):
            with self.subTest(annotation=annotation):
                self.change("value.rs", annotation + "\n// detail\nfn test_value() { helper(); }\n", "// gone\n")
                _ = self.assert_finding(self.guard(), "value.rs", "test removed")
        self.change("test_value.py", self.python("pass"), "# gone\n")
        _ = self.assert_finding(self.guard(), "test_value.py", "test removed")

    def test_added_skip_and_expected_failure_markers_turn_tests_off(self) -> None:
        for marker in ("#[ignore]", '#[ignore = "later"]'):
            with self.subTest(marker=marker):
                self.change("value.rs", self.rust("helper();"), marker + "\n" + self.rust("helper();"))
                _ = self.assert_finding(self.guard(), "value.rs", "test turned off", "?")
        for marker in ("@unittest.skip('later')", "@unittest.skipIf(True, 'later')",
                       "@unittest.skipUnless(False, 'later')", "@unittest.expectedFailure",
                       "@pytest.mark.skip", "@pytest.mark.xfail"):
            with self.subTest(marker=marker):
                self.change("test_value.py", self.python("pass"), marker + "\n" + self.python("pass"))
                _ = self.assert_finding(self.guard(), "test_value.py", "test turned off", "?")
        for marker in ("self.skipTest('later')", "pytest.skip('later')"):
            with self.subTest(marker=marker):
                self.change("test_value.py", self.python("pass"), self.python(marker))
                _ = self.assert_finding(self.guard(), "test_value.py", "test turned off")

    def test_moved_assertions_and_test_functions_are_not_findings(self) -> None:
        before = self.rust("assert_eq!(actual, expected);")
        _ = self.save("first.rs", before)
        _ = self.save("second.rs", "// destination\n")
        self.commit_base()
        _ = self.save("first.rs", "// moved\n")
        _ = self.save("second.rs", before)
        self.assert_clean(self.guard())
        self.change("test_value.py", self.python("assert actual == expected") + "\n" * 20 + self.python("pass", "test_other"),
                    self.python("pass", "test_other") + "\n" * 20 + self.python("assert actual == expected"))
        self.assert_clean(self.guard())

    def test_whitespace_changes_and_stricter_assertions_are_not_findings(self) -> None:
        for path, before, after in (("value.rs", self.rust("assert_eq!( actual , expected );"),
                                     self.rust("assert_eq!(actual,expected);")),
                                    ("test_value.py", self.python("self.assertEqual( actual , expected )"),
                                     self.python("self.assertEqual(actual,expected)")),
                                    ("value.rs", self.rust("assert!(actual);"), self.rust("assert_eq!(actual, expected);")),
                                    ("test_value.py", self.python("assert actual"),
                                     self.python("self.assertEqual(actual, expected)"))):
            with self.subTest(path=path, before=before):
                self.change(path, before, after)
                self.assert_clean(self.guard())

    def test_duplicate_assertions_are_compared_as_multisets(self) -> None:
        _ = self.save("value.rs", self.rust("assert_eq!(a, b);\n    assert_eq!(a, b);"))
        _ = self.save("moved.rs", "// destination\n")
        self.commit_base()
        _ = self.save("value.rs", "// moved\n")
        _ = self.save("moved.rs", self.rust("assert_eq!(a, b);"))
        _ = self.assert_finding(self.guard(), "value.rs", "removed")

    def test_one_weaker_addition_cannot_clear_two_removed_assertions(self) -> None:
        self.change("value.rs", self.rust("assert_eq!(a, 4);\n    assert_eq!(b, 4);"), self.rust("assert!(a <= 4);"))
        result = self.guard()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        findings = [line for line in result.stdout.splitlines() if line.startswith("value.rs:")]
        self.assertEqual(len(findings), 2, result.stdout)
        self.assertEqual(sum(" weaker:" in line for line in findings), 1)
        self.assertEqual(sum(" removed:" in line for line in findings), 1)
        self.assertIn("assertion guard: 2 blocking;", result.stdout)

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

    def test_local_main_without_a_remote_judges_working_tree_assertion_removal(self) -> None:
        before = "assert_eq!(actual, expected);\n    assert!(ok);"
        self.change("value.rs", self.rust(before), self.rust("assert!(ok);"))
        _ = self.git("branch", "-m", "main")
        _ = self.git("update-ref", "-d", "refs/remotes/origin/main")
        line = self.assert_finding(self.guard(), "value.rs", "removed")
        self.assertIn("assert_eq!(actual, expected); -> (none)", line)

    def test_missing_remote_and_local_defaults_names_all_five_refs(self) -> None:
        _ = self.git("update-ref", "-d", "refs/remotes/origin/main")
        result = self.guard()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)
        for reference in ("origin/HEAD", "origin/main", "origin/master", "main", "master"):
            with self.subTest(reference=reference):
                self.assertRegex(result.stderr, rf"(?<![\w/]){re.escape(reference)}(?![\w/])")
        self.assertNotIn(CLEAN, result.stdout)

    def test_no_default_base_and_invalid_revisions_fail_closed(self) -> None:
        _ = self.git("update-ref", "-d", "refs/remotes/origin/main")
        for arguments in ((), ("--base", "absent"), ("--base", self.base, "--head", "absent"),
                          ("--base", self.base, "--head", "HEAD", "--staged")):
            with self.subTest(arguments=arguments):
                result = self.guard(*arguments)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)
                self.assertNotIn(CLEAN, result.stdout)

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
        result = self.guard()
        cpu_seconds = time.process_time() - started
        children_after = resource.getrusage(resource.RUSAGE_CHILDREN)
        cpu_seconds += (children_after.ru_utime + children_after.ru_stime
                        - children_before.ru_utime - children_before.ru_stime)
        _ = self.assert_finding(result, "value.rs", "weaker")
        self.assertLess(cpu_seconds, 1.0, f"guard used {cpu_seconds:.3f} CPU seconds")


if __name__ == "__main__":
    _ = unittest.main()
