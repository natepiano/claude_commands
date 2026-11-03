"""Prove scoped binding renames and retain reports for changed assertions."""
from __future__ import annotations

import re

from scripts.direct.assertion_guard_test_support import AssertionFindingFixture


class ScopedBindingRenameTests(AssertionFindingFixture):
    def rename_bindings(self, source: str, names: dict[str, str]) -> str:
        return re.sub(r"\b[A-Za-z_]\w*\b", lambda token: names.get(token[0], token[0]), source)

    def assert_rename_refused(self, before: str, after: str) -> str:
        self.change("answer.rs", before, after)
        result = self.guard()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertIn(" removed:", result.stdout)
        return result.stdout

    def shared_scopes(self) -> tuple[str, str]:
        before = self.rust(
            "let shared = Scope { path: read() };\n"
            + "    let added = Scope { path: other() };\n"
            + "    let revision = revision_of(vec![shared.clone(), added.clone()]);\n"
            + "    let answers = answers();\n"
            + "    for answer in &answers {\n"
            + "        for current in [&revision] {\n"
            + "            assert!(answer.covers(current, &shared));\n"
            + "            assert!(!answer.covers(current, &added));\n"
            + "        }\n    }",
            "answers_cover_shared_scopes_after_unrelated_widen")
        after = self.rename_bindings(before, {"shared": "shared_reservation_scope",
                                             "added": "added_reservation_scope",
                                             "current": "overlap_scope_revision"})
        after = after.replace("answer.covers(overlap_scope_revision, &shared_reservation_scope)",
                              "answer.covers(\n                overlap_scope_revision,\n"
                              + "                &shared_reservation_scope\n            )")
        return before, after

    def per_holder(self) -> tuple[str, str]:
        before = self.rust(
            "let shared = scope();\n"
            + "    let revision = revision_of(&shared);\n"
            + "    let answer = Answer { holders: vec![holder()] };\n"
            + "    assert!(answer.covers(&revision, &shared));\n"
            + "    assert!(!answer.covers(&revision, &other));",
            "a_sequence_per_holder_covers_each_holders_own_scope")
        after = self.rename_bindings(before, {"shared": "shared_reservation_scope",
                                             "revision": "overlap_scope_revision"})
        after = after.replace("answer.covers(&overlap_scope_revision, &shared_reservation_scope)",
                              "answer.covers(\n        &overlap_scope_revision,\n"
                              + "        &shared_reservation_scope\n    )")
        return before, after

    def sequence_edges(self) -> tuple[str, str]:
        before = self.rust(
            "let shared = scope();\n"
            + "    let one_holder = answer(Answer { holders: vec![first] }, &shared);\n"
            + "    assert!(matches!(one_holder, Answer::Sequence { blocker, .. } if blocker == first));\n"
            + "    let per_holder = answer(Answer { holders: vec![first, second] });\n"
            + "    let Answer::PerHolder { holders, .. } = per_holder else { return };\n"
            + "    assert_eq!(holders.len(), 2);",
            "a_sequence_answer_records_one_edge_per_holder")
        after = self.rename_bindings(before, {"shared": "shared_reservation_scope",
                                             "one_holder": "one_holder_conflict_authorization",
                                             "per_holder": "per_holder_conflict_authorization"})
        after = after.replace("= per_holder_conflict_authorization else",
                              "=\n        per_holder_conflict_authorization\n    else")
        return before, after

    def test_nested_loop_binding_and_rewrapped_assertions_are_proved(self) -> None:
        self.change("answer.rs", *self.shared_scopes())
        self.assert_clean(self.guard())
        self.check_removed_assertion()
        self.check_loop_visibility()

    def test_vector_in_a_field_value_does_not_refuse_a_plain_rename(self) -> None:
        self.change("answer.rs", *self.per_holder())
        self.assert_clean(self.guard())
        self.check_vector_macro_origins()

    def test_matches_subject_and_let_else_initializer_renames_are_proved(self) -> None:
        self.change("answer.rs", *self.sequence_edges())
        self.assert_clean(self.guard())
        self.check_changed_assertions()
        self.check_unproved_references()

    def test_each_test_proves_its_own_reused_binding_names(self) -> None:
        forms = (self.shared_scopes(), self.per_holder(), self.sequence_edges())
        self.change("answer.rs", "mod tests {\n" + "\n".join(old for old, _ in forms) + "}\n",
                    "mod tests {\n" + "\n".join(new for _, new in forms) + "}\n")
        self.assert_clean(self.guard())

    def check_changed_assertions(self) -> None:
        before, renamed = self.shared_scopes()
        for replacement in ("different_revision", "&added_reservation_scope"):
            with self.subTest(replacement=replacement):
                after = renamed.replace("overlap_scope_revision,\n", replacement + ",\n", 1)
                _ = self.assert_rename_refused(before, after)
        before, renamed = self.sequence_edges()
        for after in (renamed.replace("blocker == first", "blocker == second"),
                      renamed.replace("assert!(matches!", "debug_assert!(matches!"),
                      renamed.replace("holders.len(), 2", "holders.len(), 3")):
            with self.subTest(after=after):
                report = self.assert_rename_refused(before, after)
                if "holders.len(), 3" in after:
                    self.assertIn("holders.len(), 2", report)
                    self.assertIn("holders.len(), 3", report)

    def check_removed_assertion(self) -> None:
        before, renamed = self.shared_scopes()
        after = renamed.replace("            assert!(!answer.covers(overlap_scope_revision, &added_reservation_scope));\n", "")
        self.assertNotEqual(after, renamed)
        report = self.assert_rename_refused(before, after)
        self.assertIn("assert!(!answer.covers(current, &added));", report)

    def check_loop_visibility(self) -> None:
        for body in ("for value in values { consume(value); }\n    assert!(value.ok());",
                     "for value in value { assert!(value.ok()); }"):
            with self.subTest(body=body):
                _ = self.assert_rename_refused(self.rust(body), self.rust(self.rename_bindings(body, {"value": "renamed"})))

    def check_unproved_references(self) -> None:
        for body in ("let value = read();\n    assert!(matches!(other, value));",
                     "let value = read();\n    assert!(matches!(other, Some(_) if value.ok()));",
                     "let value = read();\n    assert!(observe!(value));",
                     "let value = read();\n    consume(Data { value });\n    assert!(value.ok());"):
            with self.subTest(body=body):
                _ = self.assert_rename_refused(self.rust(body), self.rust(body.replace("value", "renamed")))

    def check_vector_macro_origins(self) -> None:
        before, after = self.per_holder()
        for environment in ("use provider::vec;\n", "macro_rules! vec { ($($t:tt)*) => { other() } }\n"):
            with self.subTest(environment=environment):
                _ = self.assert_rename_refused(environment + before, environment + after)
