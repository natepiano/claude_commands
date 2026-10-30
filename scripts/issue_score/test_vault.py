"""Vault behavior from the Phase 1 spec; every issue lives in a temporary vault."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import override
from unittest import mock

from scripts.feature_log import feature_log
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
            "| area | definition |\n| --- | --- |\n"
            '| "[[Rendering]]" | Drawing scenes and managing render passes. |\n'
            "| TOOLING | Tools used to build and inspect the project. |\n"
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

    def test_legacy_three_column_area_definitions_ignore_only_the_issues_cell(self) -> None:
        path = self.root / "issue-areas.md"
        _ = path.write_text(
            "| area | definition | issues |\n| --- | --- | --- |\n"
            + "| rendering | Drawing scenes. | Not a live count |\n"
            + "| tooling | Building projects. | 999 |\n"
            + "| invalid | Missing third cell. |\n", encoding="utf-8")
        self.assertEqual(vault.load_area_definitions(path), {
            "rendering": "Drawing scenes.", "tooling": "Building projects.",
        })

    def test_area_definition_rows_must_match_the_header_column_count(self) -> None:
        path = self.root / "issue-areas.md"
        _ = path.write_text("area | definition\n--- | ---\nrendering | Drawing scenes.\n"
                            + "tooling | Extra cell. | 5\n", encoding="utf-8")
        self.assertEqual(vault.load_area_definitions(path), {"rendering": "Drawing scenes."})

    def test_area_definitions_without_outer_pipes_load_every_row_and_stop_at_prose(self) -> None:
        path = self.root / "issue-areas.md"
        _ = path.write_text(
            "# Draft definitions\n\n"
            + "area | definition\n--- | ---\n"
            + '"[[Rendering]]" | Drawing scenes.\n'
            + "TOOLING | Building and inspecting projects.\n"
            + "Definitions end here.\n"
            + "ignored | Outside the definitions table.\n",
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
            "| area | definition |\n| --- | --- |\n",
            "area | definition\n--- | ---\nrendering |\n",
        ):
            with self.subTest(content=content):
                _ = path.write_text(content, encoding="utf-8")
                with self.assertRaises(ValueError) as raised:
                    _ = vault.load_area_definitions(path)
                self.assertIn(str(path), str(raised.exception))
                self.assertIn("no area definitions", str(raised.exception))
                self.assertIn("area | definition", str(raised.exception))

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

    def test_scoring_loader_builds_style_rule_state_and_names_missing_rules(self) -> None:
        rules = self.root / "rules"
        (rules / "rust").mkdir(parents=True)
        _ = (rules / "rust" / "names.md").write_text(
            '---\nsummary: "Name each type for its guarantee."\n---\n# Names\nLong explanatory body.\n')
        single = self.issue("Single rule", fields=(
            'style_rule: "global:rust/names.md"\nstyle_crate: renderer\nstyle_count: 7\n'), body="")
        multiple = self.issue("Multiple rules", fields=(
            'style_rules:\n  - "global:rust/names.md"\n  - "global:rust/missing.md"\n'
            + 'style_crate: widgets\nstyle_findings:\n  - first\n  - second\n'), body="")
        closed = self.issue("Closed style", fields='style_rule: "global:rust/names.md"\n')
        _ = closed.write_text(closed.read_text().replace("status: open", "status: closed"))
        loaded = {issue.path: issue for issue in vault.load_scoring_issues(self.root, rules_dirs=(rules,))}
        self.assertEqual(set(loaded), {single, multiple})
        self.assertEqual(loaded[single].style_inputs,
                         vault.StyleInputs((vault.RuleSummary("global:rust/names.md", "Name each type for its guarantee."),),
                                           "renderer", 7))
        state = vault.build_state(loaded[multiple])["issue"]
        self.assertEqual(state.get("style_rules"), [
            {"name": "global:rust/names.md", "summary": "Name each type for its guarantee.", "status": "available"},
            {"name": "global:rust/missing.md", "status": "missing"},
        ])
        self.assertEqual(state.get("style_crate"), "widgets")
        self.assertEqual(state.get("finding_count"), 2)
        self.assertEqual(state["title"], "Multiple rules")
        self.assertEqual(state["project"], "hana")
        self.assertTrue(set(state).isdisjoint(vault.LABEL_FIELDS))

    def test_project_names_follow_only_present_base_files_and_their_project_filters(self) -> None:
        bases = self.root / "home-base"
        bases.mkdir()
        base = bases / "issues - freshly-added.base"
        _ = base.write_text('filters:\n  and:\n    - project.contains(link("hana_diegetic"))\n')
        _ = (bases / "other.base").write_text('project.contains(link("not-a-project-base"))\n')
        self.assertEqual(vault.load_project_names(bases), {"freshly-added": "hana_diegetic"})
        _ = base.write_text('filters:\n  and:\n    - project.contains(link("hana"))\n')
        self.assertEqual(vault.load_project_names(bases), {"freshly-added": "hana"})
        base.unlink()
        self.assertEqual(vault.load_project_names(bases), {})

    def test_local_style_rule_uses_the_owning_repository_for_project_and_member_crate(self) -> None:
        owner = self.root / "owning-checkout"
        global_rules = self.root / "global-rules"
        relative = Path("docs/member/style/adding-a-keybinding.md")
        rule = owner / relative
        rule.parent.mkdir(parents=True)
        _ = rule.write_text('---\nsummary: "Document the new shortcut and its action."\n---\n')
        decoy = global_rules / relative
        decoy.parent.mkdir(parents=True)
        _ = decoy.write_text('---\nsummary: "This belongs to a different repository."\n---\n')
        registry = {"new-owner": feature_log.ProjectSpec("fixture/new-owner", frozenset({"new-member"}),
                                                       frozenset(), (owner,))}
        with mock.patch.dict(feature_log.PROJECTS, registry, clear=True):
            for project in ("new-owner", "new-member"):
                with self.subTest(project=project):
                    target = self.issue("Repository local rule", fields=(
                        'style_rule: "local:' + relative.as_posix() + '"\nstyle_crate: new-member\nstyle_count: 1\n'))
                    _ = target.write_text(target.read_text().replace('project: "[[hana]]"', 'project: "[[' + project + ']]"'))
                    before = {path: path.read_bytes() for path in (target, rule, decoy)}
                    loaded = vault.load_scoring_issues(self.root, rules_dirs=(global_rules,))
                    self.assertEqual([issue.path for issue in loaded], [target])
                    self.assertEqual(loaded[0].style_inputs, vault.StyleInputs((
                        vault.RuleSummary("local:" + relative.as_posix(), "Document the new shortcut and its action."),
                    ), "new-member", 1))
                    self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_github_style_rule_url_uses_the_owning_repository_and_preserves_the_reference(self) -> None:
        owner = self.root / "owning-url-checkout"
        fallback = self.root / "fallback-rules"
        relative = Path("docs/cargo-port/style/adding-a-keybinding.md")
        rule = owner / relative
        rule.parent.mkdir(parents=True)
        _ = rule.write_text('---\nsummary: "Document a shortcut alongside its action."\n---\n')
        decoy = fallback / relative
        decoy.parent.mkdir(parents=True)
        _ = decoy.write_text('---\nsummary: "This summary belongs to another repository."\n---\n')
        reference = "https://github.com/fixture/new-url-owner/blob/main/" + relative.as_posix()
        target = self.issue("Repository URL rule", fields=(
            'style_rules:\n  - "' + reference + '"\nstyle_crate: new-url-member\nstyle_count: 3\n'))
        _ = target.write_text(target.read_text().replace('project: "[[hana]]"', 'project: "[[new-url-member]]"'))
        registry = {"new-url-owner": feature_log.ProjectSpec("fixture/new-url-owner", frozenset({"new-url-member"}),
                                                           frozenset(), (owner,))}
        before = {path: path.read_bytes() for path in (target, rule, decoy)}
        with mock.patch.dict(feature_log.PROJECTS, registry, clear=True):
            loaded = vault.load_scoring_issues(self.root, rules_dirs=(fallback,))
        self.assertEqual([issue.path for issue in loaded], [target])
        self.assertEqual(loaded[0].style_inputs, vault.StyleInputs((
            vault.RuleSummary(reference, "Document a shortcut alongside its action."),
        ), "new-url-member", 3))
        self.assertEqual({path: path.read_bytes() for path in before}, before)

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
