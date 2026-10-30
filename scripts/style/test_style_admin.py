"""Renaming a rule updates its file, references, and recorded guideline IDs."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import final
from unittest import mock

import style_admin


@final
class StyleAdminTests(unittest.TestCase):
    def test_rename_reruns_after_source_move(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "rust").mkdir()
            (root / ".history").mkdir()
            _ = (root / "rust/old.md").write_text("[[old]]\n")
            history = root / ".history/run.jsonl"
            _ = history.write_text('{"reviewed_units":[{"guideline_id":"rust/old.md"}]}\n')
            with (mock.patch.object(style_admin, "NATE_STYLE_DIR", root),
                  mock.patch.object(style_admin, "RUST_STYLE_DIR", root / "rust"),
                  mock.patch.object(style_admin, "HISTORY_DIR", root / ".history"),
                  contextlib.redirect_stdout(io.StringIO())):
                with mock.patch.object(style_admin, "update_history_for_rename", side_effect=InterruptedError("stop after move")):
                    with self.assertRaises(InterruptedError):
                        style_admin.run_rename("old", "new")
                style_admin.run_rename("old", "new")
            self.assertEqual((root / "rust/new.md").read_text(), "[[new]]\n")
            self.assertIn("rust/new.md", history.read_text())

    def test_delete_reruns_after_source_unlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "rust").mkdir()
            (root / ".history").mkdir()
            _ = (root / "rust/old.md").write_text("rule\n")
            _ = (root / "links.md").write_text("[[old]]\n")
            history = root / ".history/run.jsonl"
            _ = history.write_text('{"reviewed_units":[{"guideline_id":"rust/old.md"}]}\n')
            with (mock.patch.object(style_admin, "NATE_STYLE_DIR", root),
                  mock.patch.object(style_admin, "RUST_STYLE_DIR", root / "rust"),
                  mock.patch.object(style_admin, "HISTORY_DIR", root / ".history"),
                  contextlib.redirect_stdout(io.StringIO())):
                with mock.patch.object(style_admin, "update_history_for_delete", side_effect=InterruptedError("stop after unlink")):
                    with self.assertRaises(InterruptedError):
                        style_admin.run_delete("old")
                style_admin.run_delete("old")
            self.assertFalse(history.exists())
            self.assertEqual((root / "links.md").read_text(), "old\n")

    def test_history_rewrite_stop_keeps_all_original_rows_for_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.jsonl"
            original = '{"reviewed_units":[{"guideline_id":"rust/old.md"}]}\n'
            _ = path.write_text(original)
            rows = style_admin.load_jsonl(path)
            assert "reviewed_units" in rows[0]
            rows[0]["reviewed_units"][0]["guideline_id"] = "rust/new.md"
            with mock.patch.object(json, "dumps", side_effect=InterruptedError("stop during serialization")):
                with self.assertRaises(InterruptedError):
                    style_admin.write_jsonl(path, rows)
            self.assertEqual(path.read_text(), original)
            style_admin.write_jsonl(path, rows)
            self.assertIn("rust/new.md", path.read_text())

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
