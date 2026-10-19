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
            metadata,
            {"tags": ["rust", "non-negotiable"], "see_also": ["related", "another"]},
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
