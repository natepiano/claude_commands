"""Approved-field edits use a real temporary review and never the user's vault."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
from typing import cast
from unittest import mock

from scripts.issue_score import apply_labels, records, score_issues, vault
from scripts.issue_score.test_reassess import ReassessmentFixture


class ApplyLabelsTests(ReassessmentFixture):
    def approvals(self, rows: list[records.ScoredIssue], approvals: list[dict[str, object]]) -> Path:
        run_id = self.run_file("approved", rows)
        review = score_issues.reassess(run_id, issues_dir=self.issues, state_dir=self.state,
                                       area_definitions_path=self.definitions_path, rules_dirs=(self.root / "rules",))
        path = self.root / "approvals.json"
        _ = path.write_text(json.dumps({"review_path": str(review.with_suffix(".json")), "approvals": approvals}))
        return path

    def apply(self, approvals: Path) -> list[Path]:
        return apply_labels.apply_labels(approvals, issues_dir=self.issues,
                                          area_definitions_path=self.definitions_path, rules_dirs=(self.root / "rules",))

    def test_only_explicitly_approved_frontmatter_fields_change_in_template_format(self) -> None:
        target = self.issue("Approved", extra='rank: 17\ntags:\n  - keep-me\n# Keep this comment.\n',
                            body="Keep [[this body]] exactly.\nProposed ranking: original user context\n")
        untouched = self.issue("Unapproved issue")
        target.chmod(0o640)
        before, other_before = target.read_text(), untouched.read_bytes()
        fields = {"backlog_goal": "2 - Find Collaborators", "backlog_alignment": "⭐⭐⭐⭐",
                  "backlog_impact": "⭐⭐⭐", "backlog_urgency": "⭐⭐", "backlog_effort": "⭐⭐⭐⭐⭐",
                  "category": "feature", "area": "tooling"}
        path = self.approvals([self.judgment(target), self.judgment(untouched)], [
            {"issue_path": str(target), "fields": fields}])
        expected = before
        for field in ("backlog_goal", "backlog_alignment", "backlog_impact", "backlog_urgency", "backlog_effort"):
            old = next(line for line in before.splitlines() if line.startswith(field + ":"))
            expected = expected.replace(old, field + ": " + json.dumps(fields[field], ensure_ascii=False))
        expected = expected.replace("category: idea\n", 'category:\n  - "[[issue structure#feature|feature]]"\n')
        expected = expected.replace('area: "[[rendering]]"', 'area: "[[tooling]]"')
        self.assertEqual(self.apply(path), [target])
        self.assertEqual(target.read_text(), expected)
        self.assertEqual(untouched.read_bytes(), other_before)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)

    def test_quoted_and_unquoted_current_goal_can_be_approved(self) -> None:
        for quoted in (True, False):
            with self.subTest(quoted=quoted):
                target = self.issue("Goal format")
                if not quoted:
                    _ = target.write_text(target.read_text().replace('backlog_goal: "1 - Ship Hana"', 'backlog_goal: 1 - Ship Hana'))
                path = self.approvals([self.judgment(target)], [
                    {"issue_path": str(target), "fields": {"backlog_goal": "2 - Find Collaborators"}}])
                self.assertEqual(self.apply(path), [target])
                self.assertIn('backlog_goal: "2 - Find Collaborators"\n', target.read_text())

    def test_changed_issue_content_or_area_definition_refuses_with_hash_and_rescore_diagnostic(self) -> None:
        for change in ("body", "definition"):
            with self.subTest(change=change):
                target = self.issue("Stale content")
                path = self.approvals([self.judgment(target)], [
                    {"issue_path": str(target), "fields": {"backlog_impact": "⭐⭐⭐"}}])
                if change == "body":
                    _ = target.write_text(target.read_text() + "A new requirement.\n")
                else:
                    _ = self.definitions_path.write_text(self.definitions_path.read_text().replace("Drawing scenes.", "Drawing new scenes."))
                before = target.read_bytes()
                with self.assertRaises(ValueError) as caught:
                    _ = self.apply(path)
                self.assertIn(str(target), str(caught.exception))
                self.assertIn("hash changed", str(caught.exception))
                self.assertIn("rescore", str(caught.exception))
                self.assertEqual(target.read_bytes(), before)

    def test_changed_approved_label_refuses_all_edits_before_any_issue_is_written(self) -> None:
        first, stale = self.issue("First valid"), self.issue("Second stale label")
        path = self.approvals([self.judgment(first), self.judgment(stale)], [
            {"issue_path": str(issue), "fields": {"backlog_impact": "⭐⭐⭐"}} for issue in (first, stale)])
        _ = stale.write_text(stale.read_text().replace('backlog_impact: "⭐"', 'backlog_impact: "⭐⭐"'))
        before = {issue: issue.read_bytes() for issue in (first, stale)}
        with self.assertRaises(ValueError) as caught:
            _ = self.apply(path)
        self.assertIn("backlog_impact", str(caught.exception))
        self.assertIn("changed", str(caught.exception))
        self.assertEqual({issue: issue.read_bytes() for issue in before}, before)

    def test_unapproved_label_change_is_preserved_when_another_label_is_approved(self) -> None:
        target = self.issue("Independent user effort")
        path = self.approvals([self.judgment(target)], [
            {"issue_path": str(target), "fields": {"backlog_impact": "⭐⭐⭐"}}])
        _ = target.write_text(target.read_text().replace('backlog_effort: "⭐"', 'backlog_effort: "⭐⭐⭐⭐"'))
        before = target.read_text()
        self.assertEqual(self.apply(path), [target])
        self.assertEqual(target.read_text(), before.replace('backlog_impact: "⭐"', 'backlog_impact: "⭐⭐⭐"'))

    def test_nonlabel_or_invalid_approval_refuses_without_writing(self) -> None:
        target = self.issue("Reject unsupported edit")
        row = self.judgment(target)
        for fields in ({"backlog_score": "100"}, {"rank": "1"}, {"backlog_impact": "⭐⭐⭐⭐⭐⭐"},
                       {"category": "unknown"}, {"area": "unknown"}):
            with self.subTest(fields=fields):
                before = target.read_bytes()
                path = self.approvals([row], [{"issue_path": str(target), "fields": fields}])
                with self.assertRaises(ValueError):
                    _ = self.apply(path)
                self.assertEqual(target.read_bytes(), before)

    def test_empty_approvals_leave_all_reviewed_labels_unchanged(self) -> None:
        target = self.issue("Reviewed without approval")
        before = target.read_bytes()
        path = self.approvals([self.judgment(target)], [])
        self.assertEqual(self.apply(path), [])
        self.assertEqual(target.read_bytes(), before)

    def test_partial_batch_rerun_skips_applied_values_and_finishes_remaining_issues(self) -> None:
        first, second = self.issue("Applied before stop"), self.issue("Stopped before replace")
        before = {issue: issue.read_text() for issue in (first, second)}
        path = self.approvals([self.judgment(issue) for issue in before], [
            {"issue_path": str(issue), "fields": {"backlog_impact": "⭐⭐⭐"}} for issue in before])
        replace = os.replace

        def stop_on_second(source: Path, destination: Path) -> None:
            if destination == second:
                raise OSError("fixture stops the second issue replacement")
            replace(source, destination)

        with mock.patch.object(os, "replace", side_effect=stop_on_second):
            with self.assertRaisesRegex(OSError, "second issue replacement"):
                _ = self.apply(path)
        expected_first = before[first].replace('backlog_impact: "⭐"', 'backlog_impact: "⭐⭐⭐"')
        self.assertEqual(first.read_text(), expected_first)
        self.assertEqual(second.read_text(), before[second])
        first_modified = first.stat().st_mtime_ns
        with mock.patch.object(os, "replace", wraps=replace) as replaced:
            self.assertEqual(self.apply(path), [second])
        replaced.assert_called_once_with(mock.ANY, second)
        self.assertEqual(first.read_text(), expected_first)
        self.assertEqual(first.stat().st_mtime_ns, first_modified)
        self.assertEqual(second.read_text(), before[second].replace('backlog_impact: "⭐"', 'backlog_impact: "⭐⭐⭐"'))
        with mock.patch.object(os, "replace", wraps=replace) as replaced:
            self.assertEqual(self.apply(path), [])
        replaced.assert_not_called()

    def test_area_approval_rerun_uses_the_scored_vocabulary_after_a_partial_batch(self) -> None:
        area = "documentation"
        self.definitions[area] = "Writing user and contributor documentation."
        _ = self.definitions_path.write_text(self.definitions_path.read_text()
                                            + area + " | " + self.definitions[area] + "\n")
        first, second = self.issue("First new area"), self.issue("Second new area")
        before = {issue: issue.read_text() for issue in (first, second)}
        scored_areas = vault.area_vocabulary(vault.load_open_issues(self.issues))
        self.assertEqual(scored_areas, ["rendering"])
        path = self.approvals([self.judgment(issue) for issue in before], [
            {"issue_path": str(issue), "fields": {"area": area}} for issue in before])
        review_path = self.state / "reports" / "approved-reassess.json"
        review_bytes = review_path.read_bytes()
        review = cast(records.Reassessment, json.loads(review_bytes))
        self.assertEqual(review["area_vocabulary"], scored_areas)
        self.assertEqual(review["area_definitions"], self.definitions)
        replace = os.replace

        def stop_on_second(source: Path, destination: Path) -> None:
            if destination == second:
                raise OSError("fixture stops the new-area batch before its second replacement")
            replace(source, destination)

        with mock.patch.object(os, "replace", side_effect=stop_on_second):
            with self.assertRaisesRegex(OSError, "new-area batch"):
                _ = self.apply(path)
        expected = {issue: text.replace('area: "[[rendering]]"', 'area: "[[documentation]]"')
                    for issue, text in before.items()}
        self.assertEqual(first.read_text(), expected[first])
        self.assertEqual(second.read_text(), before[second])
        self.assertEqual(vault.area_vocabulary(vault.load_open_issues(self.issues)), [area, "rendering"])
        first_modified = first.stat().st_mtime_ns
        with mock.patch.object(os, "replace", wraps=replace) as replaced:
            self.assertEqual(self.apply(path), [second])
        replaced.assert_called_once_with(mock.ANY, second)
        self.assertEqual({issue: issue.read_text() for issue in before}, expected)
        self.assertEqual(first.stat().st_mtime_ns, first_modified)
        self.assertEqual(vault.area_vocabulary(vault.load_open_issues(self.issues)), [area])
        with mock.patch.object(os, "replace", wraps=replace) as replaced:
            self.assertEqual(self.apply(path), [])
        replaced.assert_not_called()
        self.assertEqual(review_path.read_bytes(), review_bytes)
        _ = self.definitions_path.write_text(self.definitions_path.read_text().replace(
            self.definitions[area], "Writing and reviewing documentation."))
        with self.assertRaises(ValueError) as caught:
            _ = self.apply(path)
        self.assertIn(str(first), str(caught.exception))
        self.assertIn("hash changed", str(caught.exception))
        self.assertIn("rescore", str(caught.exception))
        self.assertEqual({issue: issue.read_text() for issue in before}, expected)

    def test_indentless_frontmatter_lists_keep_style_inputs_and_replace_only_the_approved_list(self) -> None:
        ordinary = self.issue("Ordinary issue supplies global area vocabulary")
        ordinary_before = ordinary.read_bytes()
        reference = "local:docs/style/names.md"
        rule = self.root / "rules" / "docs" / "style" / "names.md"
        rule.parent.mkdir(parents=True)
        _ = rule.write_text('---\nsummary: "Name each type for its guarantee."\n---\n')
        target = self.issue("Indentless style and category", extra=(
            "style_rules:\n- " + reference + "\nstyle_crate: renderer\nstyle_count: 2\n"))
        old_category = 'category:\n- "[[https://example.test/issue-structure#idea|idea]]"\n'
        before = target.read_text().replace("category: idea\n", old_category)
        _ = target.write_text(before)
        document = vault.parse_document(target, before)
        self.assertEqual(document.fields["style_rules"], [reference])
        self.assertEqual(document.issue().labels["category"], "idea")
        self.assertTrue(document.is_style)
        with mock.patch.object(vault, "project_rule_roots", return_value=(self.root / "rules",)):
            self.assertEqual(self.loaded(target).style_inputs, vault.StyleInputs((
                vault.RuleSummary(reference, "Name each type for its guarantee."),
            ), "renderer", 2))
            path = self.approvals([self.judgment(target)], [
                {"issue_path": str(target), "fields": {"category": "feature"}}])
            self.assertEqual(self.apply(path), [target])
            self.assertEqual(self.loaded(target).style_inputs, vault.StyleInputs((
                vault.RuleSummary(reference, "Name each type for its guarantee."),
            ), "renderer", 2))
        self.assertEqual(target.read_text(), before.replace(
            old_category, 'category:\n  - "[[issue structure#feature|feature]]"\n'))
        self.assertEqual(ordinary.read_bytes(), ordinary_before)

    def test_dash_prefixed_frontmatter_key_preserves_its_list_when_a_rating_is_approved(self) -> None:
        target = self.issue("Keep dash-prefixed tags")
        before = target.read_text().replace(
            'backlog_impact: "⭐"\n', 'backlog_impact: "⭐"\n-tags:\n  - keep-me\n')
        _ = target.write_text(before)
        path = self.approvals([self.judgment(target)], [
            {"issue_path": str(target), "fields": {"backlog_impact": "⭐⭐⭐"}}])
        expected = before.replace('backlog_impact: "⭐"', 'backlog_impact: "⭐⭐⭐"').encode("utf-8")
        self.assertEqual(self.apply(path), [target])
        self.assertEqual(target.read_bytes(), expected)
        document = vault.parse_document(target, target.read_text())
        self.assertEqual(document.fields["-tags"], ["keep-me"])
        self.assertEqual(document.issue().labels["backlog_impact"], "⭐⭐⭐")
        with mock.patch.object(os, "replace", wraps=os.replace) as replaced:
            self.assertEqual(self.apply(path), [])
        replaced.assert_not_called()
        self.assertEqual(target.read_bytes(), expected)

    def test_quoted_frontmatter_key_ends_the_approved_fields_continuation(self) -> None:
        for key in ('"tags"', "'tags'"):
            with self.subTest(key=key):
                target = self.issue("Keep quoted tags")
                before = target.read_text().replace('backlog_impact: "⭐"\n',
                                                    'backlog_impact: "⭐"\n' + key + ':\n  - keep-me\n')
                _ = target.write_text(before)
                path = self.approvals([self.judgment(target)], [
                    {"issue_path": str(target), "fields": {"backlog_impact": "⭐⭐⭐"}}])
                self.assertEqual(self.apply(path), [target])
                self.assertEqual(target.read_text(), before.replace('backlog_impact: "⭐"', 'backlog_impact: "⭐⭐⭐"'))

    def test_change_after_issue_loading_is_checked_on_the_bytes_to_be_edited(self) -> None:
        load = vault.load_scoring_issues
        for change in ("approved value", "content"):
            with self.subTest(change=change):
                target = self.issue("Snapshot " + change)
                path = self.approvals([self.judgment(target)], [
                    {"issue_path": str(target), "fields": {"backlog_impact": "⭐⭐⭐"}}])
                before = target.read_text()
                changed = (before.replace('backlog_impact: "⭐"', 'backlog_impact: "⭐⭐"')
                           if change == "approved value" else before + "Concurrent new requirement.\n")

                def change_after_load(issues_dir: Path, *, rules_dirs: tuple[Path, ...]) -> list[vault.Issue]:
                    issues = load(issues_dir, rules_dirs=rules_dirs)
                    _ = target.write_text(changed)
                    return issues

                with mock.patch.object(vault, "load_scoring_issues", side_effect=change_after_load):
                    with self.assertRaises(ValueError) as caught:
                        _ = self.apply(path)
                self.assertIn(str(target), str(caught.exception))
                self.assertIn("changed", str(caught.exception))
                if change == "approved value":
                    self.assertIn("backlog_impact", str(caught.exception))
                else:
                    self.assertIn("hash", str(caught.exception))
                    self.assertIn("rescore", str(caught.exception))
                self.assertEqual(target.read_text(), changed)
