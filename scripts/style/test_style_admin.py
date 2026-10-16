"""Renaming a rule updates its file, references, and recorded guideline IDs."""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from typing import final
from unittest import mock

import style_admin


@final
class StyleAdminTests(unittest.TestCase):
    def test_rename_updates_rule_links_and_history_without_changing_other_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rule_dir = root / "rust"
            history_dir = root / ".history"
            rule_dir.mkdir()
            history_dir.mkdir()
            original = rule_dir / "old-rule.md"
            _ = original.write_text("# Rule\nSee [[old-rule]] and [[related]].\n")
            linked = root / "overview.md"
            _ = linked.write_text(
                '---\nsee_also: "[[old-rule]]"\n---\n'
                + "Read [[old-rule|the rule]] and [[old-rule-extra]].\n"
            )
            history = history_dir / "sample.jsonl"
            _ = history.write_text(
                '{"start_time":"2026-10-01T00:00:00Z","reviewed_units":['
                + '{"guideline_id":"rust/old-rule.md","outcome":{"status":"clean"}},'
                + '{"guideline_id":"rust/related.md","outcome":{"status":"finding"}}]}\n'
            )
            out = io.StringIO()

            with (
                mock.patch.object(style_admin, "NATE_STYLE_DIR", root),
                mock.patch.object(style_admin, "RUST_STYLE_DIR", rule_dir),
                mock.patch.object(style_admin, "HISTORY_DIR", history_dir),
                contextlib.redirect_stdout(out),
            ):
                style_admin.run_rename("old-rule", "new-rule")

            self.assertFalse(original.exists())
            self.assertEqual(
                (rule_dir / "new-rule.md").read_text(),
                "# Rule\nSee [[new-rule]] and [[related]].\n",
            )
            self.assertEqual(
                linked.read_text(),
                '---\nsee_also: "[[new-rule]]"\n---\n'
                + "Read [[new-rule|the rule]] and [[old-rule-extra]].\n",
            )
            self.assertEqual(
                style_admin.load_jsonl(history),
                [{
                    "start_time": "2026-10-01T00:00:00Z",
                    "reviewed_units": [
                        {"guideline_id": "rust/new-rule.md", "outcome": {"status": "clean"}},
                        {"guideline_id": "rust/related.md", "outcome": {"status": "finding"}},
                    ],
                }],
            )
            self.assertEqual(
                out.getvalue(),
                "Renamed: old-rule.md -> new-rule.md\n.history files updated: 1\n"
                + ".history entries updated: 1\n"
                + "Obsidian files updated: 2 (covers see_also frontmatter)\n",
            )


if __name__ == "__main__":
    _ = unittest.main()
