"""Rule metadata and focused units retain their existing selection behavior."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import final
from unittest import mock

import style_rules


@final
class StyleRuleTests(unittest.TestCase):
    def test_frontmatter_rejects_unowned_lines_and_incomplete_values(self) -> None:
        invalid = (
            ("tier: C\nfamily: names\n- orphan", "block"),
            ("- orphan", "block"),
            ("  tier: C", "indentation"),
            ("other:\n  first: one\n second: two", "indentation"),
            ("tags:\n  - one\n   - two", "indentation"),
            ("other:\n  nested:\n    item: one\n   stray: two", "indentation"),
            ("tags: [", "unterminated"),
            ("other: [", "unterminated"),
            ("candidates:\n  kind: [", "unterminated"),
            ("tags: [one # comment", "unterminated"),
            ("tags: [one, 'two]", "quote"),
            ("other: 'unclosed", "quote"),
            ('other: "unclosed', "quote"),
            ("other: 'closed' trailing", "after"),
            ("other:\n\tchild: one", "tab indentation"),
            ("unowned text", "block"),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rule.md"
            for frontmatter, reason in invalid:
                with self.subTest(frontmatter=frontmatter):
                    _ = path.write_text(f"---\n{frontmatter}\n---\n# Body\n")
                    with self.assertRaisesRegex(ValueError, reason) as raised:
                        _ = style_rules.parse_frontmatter(path)
                    self.assertIn(str(path), str(raised.exception))
            _ = path.write_text("---\ntags: [one]\n")
            with self.assertRaisesRegex(ValueError, "unterminated frontmatter"):
                _ = style_rules.parse_frontmatter(path)

    def test_frontmatter_preserves_lists_comments_and_candidate_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rule.md"
            for tags in ("tags: [one, 'two, three', \"# four\"]   # comment",
                         "tags: # comment\n- one\n- 'two, three'\n- \"# four\"",
                         "tags:\n  - one # comment\n  # between items\n  - 'two, three'\n  - \"# four\""):
                with self.subTest(tags=tags):
                    _ = path.write_text(
                        f"---\n{tags}\n# comment\ntier: 'C'   # comment\nfamily: names # comment\n"
                        + "candidates:\n  kind: regex\n  pattern: 'a plain value'\n"
                        + "see_also: ['[[related]]', \"[[another]]\"] # comment\n---\n# Body\n"
                    )
                    self.assertEqual(style_rules.parse_frontmatter(path).fields, {
                        "tags": ["one", "two, three", "# four"], "see_also": ["[[related]]", "[[another]]"],
                        "tier": "C", "family": "names",
                        "candidates": {"kind": "regex", "pattern": "a plain value"},
                    })

    def test_closed_grammar_refuses_every_unsupported_structure(self) -> None:
        invalid = (
            "other: - orphan", "other: ? orphan", "other: : value", "other: &anchor value",
            "other: *alias", "other: !tag value", "other: |", "other: >", "other: %directive",
            "other: @reserved", "other: `reserved", "other: {key: value}", "<<: *alias",
            "other: one\nother: two", "candidates:\n  kind: regex\n  kind: struct_fields",
            "other:\n  nested: value", "candidates:\n  nested:\n    key: value",
            "other: scalar\n  continuation", "tags:\n\t- one", "tags: [one]\n  ---",
            "tags: [one,]", "tags: [[one]]", "tags: [one, ? invalid]", "other: text: value",
            "tags: [one] trailing", 'other: "closed" trailing', "other: scalar\n\ufeff",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rule.md"
            for metadata in invalid:
                with self.subTest(metadata=metadata):
                    _ = path.write_text(f"---\n{metadata}\n---\n# Body\n")
                    with self.assertRaises(ValueError):
                        _ = style_rules.parse_frontmatter(path)
            for text in ("\ufeff---\ntags: [one]\n---\n", "  ---\ntags: [one]\n---\n"):
                _ = path.write_text(text)
                with self.assertRaises(ValueError):
                    _ = style_rules.parse_frontmatter(path)

    def test_all_fields_and_value_shapes_survive_parsing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rule.md"
            _ = path.write_text(
                "---\nunknown: value # comment\nsee_also: 'unrecognized binding'\n"
                + "single: [value]\nempty: []\nquoted: 'it''s # literal'\n"
                + 'double: "escaped \\"quote\\""\n'
                + "candidates:\n  kind: regex # comment\n  unknown: [one, two]\n---\n"
            )
            self.assertEqual(style_rules.parse_frontmatter(path).fields, {
                "unknown": "value", "see_also": "unrecognized binding", "single": ["value"], "empty": [],
                "quoted": "it's # literal", "double": 'escaped "quote"',
                "candidates": {"kind": "regex", "unknown": ["one", "two"]},
            })

    def test_frontmatter_reads_metadata_around_candidates_block(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            rule = Path(directory) / "rule.md"
            _ = rule.write_text(
                "---\ntags:\n  - rust\n  - non-negotiable\n"
                + "candidates:\n  kind: struct_fields\n  structs: [Settings]\n"
                + "  field_type_pattern: 'bool'\n"
                + "see_also:\n  - '[[related]]'\n  - \"[[another]]\"\n"
                + "---\n# Sample rule\nsee_also: '[[body-only]]'\n"
            )

            metadata = style_rules.parse_frontmatter(rule)

        self.assertEqual(
            metadata.fields,
            {"tags": ["rust", "non-negotiable"], "see_also": ["[[related]]", "[[another]]"],
             "candidates": {"kind": "struct_fields", "structs": ["Settings"], "field_type_pattern": "bool"}},
        )

    def test_focused_units_deduplicate_requests_and_resolve_links_outside_focus(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rule_dir = root / "nate_style" / "rust"
            rule_dir.mkdir(parents=True)
            primary = rule_dir / "primary.md"
            related = rule_dir / "related.md"
            ordinary = rule_dir / "ordinary.md"
            _ = primary.write_text(
                "---\ntags:\n  - non-negotiable\n"
                + "see_also:\n  - '[[related]]'\n  - '[[related]]'\n  - '[[missing]]'\n"
                + "pre_filter: 'Option<'\n---\n# Primary rule\n"
            )
            _ = related.write_text("# Related rule\n")
            _ = ordinary.write_text("# Ordinary rule\n")
            with (
                mock.patch.object(style_rules, "NATE_STYLE_DIR", root / "nate_style"),
                mock.patch.object(style_rules, "list_style_files", return_value=[related, ordinary, primary]),
            ):
                units = style_rules.focused_units(
                    root, ["primary", "rust/ordinary.md", "primary.md", str(primary)]
                )

        self.assertEqual([unit.unit_id for unit in units], ["rust/primary.md", "rust/ordinary.md"])
        self.assertEqual([unit.checklist_index for unit in units], [1, 2])
        self.assertEqual([unit.budget_cost for unit in units], [0, 1])
        self.assertEqual([unit.display_name for unit in units], ["Primary rule", "Ordinary rule"])
        self.assertEqual(units[0].guideline_ids, ("rust/primary.md",))
        self.assertEqual(units[0].see_also_guideline_ids, ("rust/related.md",))
        self.assertEqual(units[0].pre_filter, "Option<")


if __name__ == "__main__":
    _ = unittest.main()
