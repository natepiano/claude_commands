"""Check assertion-guard behavior against real Git repositories."""
from __future__ import annotations

import re
import unittest

from scripts.direct.assertion_guard_test_support import GitRepositoryFixture as GitRepositoryFixture, MergeInProgressFixture as MergeInProgressFixture, AssertionFindingFixture, TILE_OLD as TILE_OLD, TILE_NEW as TILE_NEW

class AssertionGuardTests(AssertionFindingFixture):
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



if __name__ == "__main__":
    _ = unittest.main()
