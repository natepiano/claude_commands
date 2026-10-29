"""Vault behavior from the Phase 1 spec; every issue lives in a temporary vault."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import override

from scripts.issue_score import vault


class VaultTests(unittest.TestCase):
    root: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def issue(self, name: str, *, fields: str = "", body: str = "A useful change.\n") -> Path:
        path = self.root / (name + ".md")
        _ = path.write_text(
            '---\nstatus: open\nproject: "[[hana]]"\n'
            + 'backlog_goal: "[[prioritization goals#1 - Ship Hana|1 - Ship Hana]]"\n'
            + 'backlog_alignment: ⭐⭐\nbacklog_impact: ⭐⭐\n'
            + 'backlog_urgency: ⭐\nbacklog_effort: ⭐\n'
            + 'category:\n  - "[[issue structure#feature|feature]]"\n'
            + 'area: "[[Rendering]]"\n' + fields + '---\n' + body,
            encoding="utf-8",
        )
        return path

    def test_wikilinks_keep_display_text_and_plain_targets(self) -> None:
        body = "Use [[render graph]] with [[issue structure#feature|a feature]] and [[ui|UI]]."
        self.assertEqual(vault.clean_body(body), "Use render graph with a feature and UI.")

    def test_area_definitions_read_normalized_table_rows_without_writing_the_file(self) -> None:
        path = self.root / "issue-areas.md"
        content = (
            "# Draft definitions for user review\n\n"
            "| area | definition | issues |\n| --- | --- | --- |\n"
            '| "[[Rendering]]" | Drawing scenes and managing render passes. | 12 |\n'
            "| TOOLING | Tools used to build and inspect the project. | 3 |\n"
        )
        _ = path.write_text(content, encoding="utf-8")
        before = path.stat()
        definitions = vault.load_area_definitions(path)
        self.assertEqual(definitions, {
            "rendering": "Drawing scenes and managing render passes.",
            "tooling": "Tools used to build and inspect the project.",
        })
        self.assertNotIn("unfiled", definitions)
        self.assertEqual(path.read_text(encoding="utf-8"), content)
        self.assertEqual(path.stat().st_mtime_ns, before.st_mtime_ns)

    def test_missing_area_definition_file_raises_with_its_path_without_creating_it(self) -> None:
        path = self.root / "conf" / "issue-areas.md"
        with self.assertRaises(ValueError) as raised:
            _ = vault.load_area_definitions(path)
        self.assertIn(str(path), str(raised.exception))
        self.assertIn("not found", str(raised.exception))
        self.assertFalse(path.parent.exists())

    def test_area_definitions_without_outer_pipes_load_every_row_and_stop_at_prose(self) -> None:
        path = self.root / "issue-areas.md"
        _ = path.write_text(
            "# Draft definitions\n\n"
            + "area | definition | issues\n--- | --- | ---\n"
            + '"[[Rendering]]" | Drawing scenes. | 12\n'
            + "TOOLING | Building and inspecting projects. | 3\n"
            + "Definitions end here.\n"
            + "ignored | Outside the definitions table. | 1\n",
            encoding="utf-8",
        )
        self.assertEqual(vault.load_area_definitions(path), {
            "rendering": "Drawing scenes.",
            "tooling": "Building and inspecting projects.",
        })

    def test_area_definition_file_without_usable_definitions_raises_with_its_path(self) -> None:
        path = self.root / "issue-areas.md"
        for content in (
            "# No table here.\n",
            "| area | definition | issues |\n| --- | --- | --- |\n",
            "area | definition | issues\n--- | --- | ---\nrendering | | 0\n",
        ):
            with self.subTest(content=content):
                _ = path.write_text(content, encoding="utf-8")
                with self.assertRaises(ValueError) as raised:
                    _ = vault.load_area_definitions(path)
                self.assertIn(str(path), str(raised.exception))
                self.assertIn("no area definitions", str(raised.exception))

    def test_long_body_cuts_before_the_section_that_would_cross_the_cap(self) -> None:
        kept = "# Problem\n" + "a" * 14_000 + "\n"
        discarded = "## Implementation\n" + "b" * 13_000
        cleaned = vault.clean_body(kept + discarded)
        self.assertEqual(cleaned.rstrip(), kept.rstrip())
        self.assertLessEqual(len(cleaned), 24_000)

    def test_cleaning_precedes_the_character_cap(self) -> None:
        body = "[[" + "target" * 100 + "|x]] "
        cleaned = vault.clean_body(body * 100)
        self.assertEqual(cleaned.strip(), ("x " * 100).strip())

    def test_area_normalization_handles_vault_quotes_links_and_case(self) -> None:
        for value in ('"[[UI]]"', "'[[ui]]'", "  UI  "):
            with self.subTest(value=value):
                self.assertEqual(vault.normalize_area(value), "ui")

    def test_open_loader_skips_closed_style_and_directory_entries(self) -> None:
        ordinary = self.issue("Ordinary issue", body="Read [[ui|the UI]].\n")
        _ = self.issue("Style issue", fields='style_rule: "global:rust/names.md"\n')
        _ = self.issue("Legacy style", fields='style_rules: ["global:rust/names.md"]\n')
        closed = self.issue("Closed issue")
        _ = closed.write_text(closed.read_text().replace("status: open", "status: closed"))
        (self.root / "Directory.md").mkdir()
        issues = vault.load_open_issues(self.root)
        self.assertEqual([issue.path for issue in issues], [ordinary])
        issue = issues[0]
        self.assertEqual(issue.title, "Ordinary issue")
        self.assertEqual(issue.project, "hana")
        self.assertEqual(issue.labels["backlog_goal"], "1 - Ship Hana")
        self.assertEqual(issue.labels["backlog_alignment"], "⭐⭐")
        self.assertEqual(issue.labels["category"], "feature")

    def test_area_vocabulary_normalizes_deduplicates_and_excludes_style(self) -> None:
        _ = self.issue("Rendering")
        duplicate = self.issue("Duplicate rendering")
        _ = duplicate.write_text(duplicate.read_text().replace("[[Rendering]]", "[[rendering]]"))
        tooling = self.issue("Tooling")
        _ = tooling.write_text(tooling.read_text().replace("[[Rendering]]", "[[Tooling]]"))
        style = self.issue("Style", fields='style_rule: "global:rust/names.md"\n')
        _ = style.write_text(style.read_text().replace("[[Rendering]]", "[[style-only]]"))
        self.assertEqual(vault.area_vocabulary(vault.load_open_issues(self.root)), ["rendering", "tooling"])

    def test_state_contains_issue_text_and_project_context_without_current_labels(self) -> None:
        _ = self.issue("A rendering issue", body="Repair [[render graph|the graph]].\n")
        issue = vault.load_open_issues(self.root)[0]
        state = vault.build_state(issue)
        self.assertEqual(set(state), {"issue"})
        self.assertEqual(set(state["issue"]), {"title", "project", "project_about", "body"})
        self.assertEqual(state["issue"]["title"], "A rendering issue")
        self.assertEqual(state["issue"]["project"], "hana")
        self.assertEqual(state["issue"]["body"].strip(), "Repair the graph.")
        self.assertTrue(state["issue"]["project_about"].strip())

    def test_rated_loader_excludes_unconfirmed_proposals_and_formula_style_labels(self) -> None:
        ordinary = self.issue("Confirmed ordinary")
        proposed = self.issue("Unconfirmed", body="Proposed ranking: goal 1 - Ship Hana · alignment ⭐⭐\n")
        default_style = self.issue("Formula style", fields=(
            'style_rule: "global:rust/names.md"\nstyle_count: 3\n'
        ), body="Kind: type\n")
        changed_style = self.issue("Reviewed style", fields=(
            'style_rule: "global:rust/names.md"\nstyle_count: 3\n'
        ), body="Kind: type\n")
        _ = changed_style.write_text(changed_style.read_text().replace("backlog_impact: ⭐⭐", "backlog_impact: ⭐⭐⭐"))
        missing = self.issue("Unrated")
        _ = missing.write_text(missing.read_text().replace("backlog_effort: ⭐", "backlog_effort:"))
        result = vault.load_rated_issues(self.root)
        self.assertEqual({issue.path for issue in result.issues}, {ordinary, changed_style})
        skipped = {entry.path: entry.reason for entry in result.skipped}
        self.assertEqual(set(skipped), {proposed, default_style, missing})
        for reason in skipped.values():
            self.assertTrue(reason.strip())

    def test_style_formula_uses_project_majority_goal_and_rename_count_boundaries(self) -> None:
        ordinary = self.issue("Project majority")
        _ = ordinary.write_text(ordinary.read_text().replace(
            "prioritization goals#1 - Ship Hana|1 - Ship Hana", "prioritization goals#2 - Find Collaborators|2 - Find Collaborators",
        ))
        for count, effort in ((3, "⭐"), (4, "⭐⭐"), (15, "⭐⭐"), (16, "⭐⭐⭐")):
            style = self.issue(f"Rename {count}", fields=(
                f'style_rule: "global:rust/names.md"\nstyle_count: {count}\n'
            ), body="Kind: rename\n")
            text = style.read_text().replace("1 - Ship Hana", "2 - Find Collaborators")
            text = text.replace("backlog_impact: ⭐⭐", "backlog_impact: ⭐")
            text = text.replace("backlog_effort: ⭐\n", f"backlog_effort: {effort}\n")
            _ = style.write_text(text)
        result = vault.load_rated_issues(self.root)
        self.assertEqual([issue.path for issue in result.issues], [ordinary])
        self.assertEqual(len(result.skipped), 4)

    def test_mixed_case_open_status_counts_toward_the_project_goal(self) -> None:
        ordinary = self.issue("Project majority")
        _ = ordinary.write_text(ordinary.read_text().replace("status: open", 'status: "[[Open]]"').replace(
            "1 - Ship Hana", "2 - Find Collaborators",
        ))
        style = self.issue("Formula style", fields='style_rule: "global:rust/names.md"\nstyle_count: 3\n',
                           body="Kind: type\n")
        _ = style.write_text(style.read_text().replace("1 - Ship Hana", "2 - Find Collaborators"))
        result = vault.load_rated_issues(self.root)
        self.assertEqual([issue.path for issue in result.issues], [ordinary])
        self.assertEqual([(entry.path, entry.reason) for entry in result.skipped],
                         [(style, "style ratings match proposal formula")])


if __name__ == "__main__":
    _ = unittest.main()
