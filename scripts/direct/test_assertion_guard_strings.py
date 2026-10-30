"""Check assertion-guard behavior against real Git repositories."""
from __future__ import annotations

import unittest

from scripts.direct.assertion_guard_test_support import AssertionFindingFixture

class AssertionStringTests(AssertionFindingFixture):
    def test_removed_python_assertions_and_multiline_first_line(self) -> None:
        for assertion in ("assert actual == expected", "self.assertEqual(actual, expected)",
                          "self.assertTrue(actual)", "pytest.raises(ValueError)", "self.fail('missing')",
                          "self.assertEqual(\n        actual,\n        expected,\n    )"):
            with self.subTest(assertion=assertion):
                self.change("test_value.py", self.python(assertion), self.python("pass"))
                _ = self.assert_finding(self.guard(), "test_value.py", "removed")


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


    def test_added_python_string_markers_do_not_turn_tests_off(self) -> None:
        strings = [
            '''    single = '@unittest.skip("later")' ''',
            '''    double = "@pytest.mark.skip" ''',
            '''    triple_single = ''' + "'''self.skipTest('later')'''",
            '''    triple_double = """pytest.skip("later")""" ''',
            '''    formatted = f"@unittest.expectedFailure {value}" ''',
            r'''    escaped = "say \" @unittest.skip('later')" ''',
            '''    markers = ("#[ignore]", "@pytest.mark.xfail")''',
        ]
        before = self.python("pass")
        after = before + "\n".join(strings) + "\n"
        self.change("test_value.py", before, after)
        self.assert_clean(self.guard())


    def test_added_rust_string_markers_do_not_turn_tests_off(self) -> None:
        strings = [
            'let normal = "#[ignore]";',
            'let raw = r"#[ignore]";',
            'let hashed = r#"#[ignore = "later"]"#;',
            'let hashes = r##"embedded "# #[ignore]"##;',
            r'''let escaped = "say \" #[ignore]";''',
        ]
        self.change("value.rs", self.rust("helper();"), self.rust("\n    ".join(strings)))
        self.assert_clean(self.guard())


    def test_markers_added_inside_multiline_strings_have_no_finding(self) -> None:
        for path, before, after in (
            ("test_value.py", self.python('text = """\n        plain\n    """'),
             self.python('text = """\n        @unittest.skip("later")\n    """')),
            ("value.rs", self.rust('let text = r#"\n        plain\n    "#;'),
             self.rust('let text = r#"\n        #[ignore = "later"]\n    "#;')),
        ):
            with self.subTest(path=path):
                self.change(path, before, after)
                self.assert_clean(self.guard())


    def test_removed_assertion_text_inside_strings_has_no_finding(self) -> None:
        for path, before, after in (
            ("test_value.py", self.python(
                '''text = "assert actual == expected; self.assertEqual(a, b); pytest.raises(ValueError)"\n'''
                + '''    triple = ''' + "'''self.fail('missing')'''\n"
                + '''    formatted = f"self.assertTrue({value})"\n    assert ok'''), self.python("assert ok")),
            ("value.rs", self.rust(
                'let text = "assert_eq!(a, b);";\n'
                + '    let raw = r#"assert!(actual == expected); "quotes""#;\n    assert!(ok);'),
             self.rust("assert!(ok);")),
        ):
            with self.subTest(path=path):
                self.change(path, before, after)
                self.assert_clean(self.guard())


    def test_real_skip_decorator_after_string_markers_still_turns_test_off(self) -> None:
        before = 'marker = "@pytest.mark.skip"\n' + self.python("pass")
        after = 'marker = "@pytest.mark.skip"\n@unittest.skip("later")\n' + self.python("pass")
        self.change("test_value.py", before, after)
        line = self.assert_finding(self.guard(), "test_value.py", "test turned off", "?")
        self.assertIn('@unittest.skip("later")', line)


    def test_real_assertion_after_a_string_on_the_same_line_is_still_removed(self) -> None:
        before = self.python('label = "assert fake"; assert actual == expected')
        after = self.python('label = "assert fake"')
        self.change("test_value.py", before, after)
        line = self.assert_finding(self.guard(), "test_value.py", "removed")
        self.assertIn("assert actual == expected -> (none)", line)


    def test_escaped_newline_strings_do_not_hide_real_assertion_removals(self) -> None:
        for path, before, after in (
            ("test_value.py", self.python('text = "continued\\\n        @pytest.mark.skip"\n    assert ok'),
             self.python('text = "continued\\\n        @pytest.mark.skip"')),
            ("value.rs", self.rust('let text = "continued\\\n        #[ignore]";\n    assert!(ok);'),
             self.rust('let text = "continued\\\n        #[ignore]";')),
        ):
            with self.subTest(path=path):
                self.change(path, before, after)
                _ = self.assert_finding(self.guard(), path, "removed")


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



if __name__ == "__main__":
    _ = unittest.main()
